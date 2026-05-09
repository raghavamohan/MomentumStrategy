"""Instrument metadata helpers for display labels and industry classification.

Builds a cached lookup of cash-equity company names from
``kite.instruments("NSE")`` and ``kite.instruments("BSE")``, and resolves
**industry** using Kite instrument rows, merged NSE index CSVs (Industry +
ISIN), and ISIN alignment for BSE cash equities.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
import logging
from app.live_prices import notify_dashboard_cache_refresh
from app.model_cache_store import (
    current_effective_day_ist,
    load_model_cache,
    next_cutoff_epoch_ist,
    read_section,
    start_background_refresh_job,
    update_section,
)


EQUITY_EXCHANGES = ("NSE", "BSE")
_CACHE_LOCK = threading.Lock()
_CACHE_EXPIRES_AT = 0.0
_CACHED_TOKEN_TO_NAME: dict[int, str] = {}
_CACHED_SYMBOL_TO_NAME: dict[tuple[str, str], str] = {}
_CACHED_SYMBOL_TO_TOKEN: dict[tuple[str, str], int] = {}
_CACHED_TOKEN_TO_INDUSTRY: dict[int, str] = {}
_CACHED_SYMBOL_TO_INDUSTRY: dict[tuple[str, str], str] = {}
_CACHED_TOKEN_TO_KITE_SECTOR: dict[int, str] = {}
_CACHED_SYMBOL_TO_KITE_SECTOR: dict[tuple[str, str], str] = {}
_CACHED_TOKEN_TO_ISIN: dict[int, str] = {}
_CACHED_SYMBOL_TO_ISIN: dict[tuple[str, str], str] = {}

_NIFTY50_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
_NSE_INDUSTRY_CSV_URLS: tuple[str, ...] = (
    "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap50list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftylargemidcap250list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftymidsmallcap400list.csv",
)
_NIFTY50_CACHE_EXPIRES_AT = 0.0
_CACHED_NIFTY50_SYMBOLS: list[str] = []
_NSE_MERGED_INDUSTRY_EXPIRES_AT = 0.0
_CACHED_NSE_SYMBOL_TO_INDUSTRY: dict[str, str] = {}
_CACHED_ISIN_TO_INDUSTRY: dict[str, str] = {}
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}

logger = logging.getLogger(__name__)

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover - yfinance is optional at runtime
    yf = None

_CACHED_YFINANCE_KEY_TO_INDUSTRY: dict[tuple[str, str], str] = {}
_CACHED_YFINANCE_KEY_TO_SECTOR: dict[tuple[str, str], str] = {}

_YFINANCE_CACHE_LOCK = threading.Lock()
_YFINANCE_CACHE_LOADED = False
_YFINANCE_CACHE_REFRESH_IN_PROGRESS = False
_YFINANCE_REFRESH_THREAD_STARTED = False
_YFINANCE_SYMBOL_REFRESH_IN_PROGRESS: set[tuple[str, str]] = set()
_YFINANCE_CACHE_LAST_SOURCE = "unknown"

_YFINANCE_CACHE_DAY = ""
_REFERENCE_DISK_CACHE_LOADED = False
_REFERENCE_DISK_CACHE: dict[str, Any] = {}
_REFERENCE_DISK_CACHE_DIRTY = False

_CASH_EQUITY_REFRESH_IN_PROGRESS = False
_NSE_MERGED_REFRESH_IN_PROGRESS = False
_NIFTY50_REFRESH_IN_PROGRESS = False
_REFERENCE_CACHE_LAST_SOURCE: dict[str, str] = {
    "cash_equity": "unknown",
    "nse_merged_industry": "unknown",
    "nifty50_symbols": "unknown",
}


def _yfinance_cache_key(exchange: str, symbol: str) -> tuple[str, str]:
    return (exchange.upper().strip(), _normalise_symbol(symbol))


def _current_reference_day_token() -> str:
    return current_effective_day_ist(cutoff_hour=9)


def _next_reference_cutoff_epoch() -> float:
    return next_cutoff_epoch_ist(cutoff_hour=9)


def _prepare_reference_disk_cache_unlocked() -> None:
    """Load shared reference cache file lazily (expects _CACHE_LOCK held)."""
    global _REFERENCE_DISK_CACHE_LOADED, _REFERENCE_DISK_CACHE
    if _REFERENCE_DISK_CACHE_LOADED:
        return
    loaded = read_section("reference_data")
    if isinstance(loaded, dict):
        _REFERENCE_DISK_CACHE = loaded
    _REFERENCE_DISK_CACHE_LOADED = True


def _save_reference_disk_cache_unlocked() -> None:
    """Persist shared reference cache file (expects _CACHE_LOCK held)."""
    global _REFERENCE_DISK_CACHE_DIRTY
    if not _REFERENCE_DISK_CACHE_DIRTY:
        return
    try:
        update_section("reference_data", lambda _: dict(_REFERENCE_DISK_CACHE))
        _REFERENCE_DISK_CACHE_DIRTY = False
    except Exception as exc:
        logger.warning("Failed to persist shared reference cache: %s", exc)


def _reference_cache_get_entry_unlocked(section: str) -> tuple[dict[str, Any], str]:
    """Return (payload, cache_day) for one shared cache section."""
    _prepare_reference_disk_cache_unlocked()
    entry = _REFERENCE_DISK_CACHE.get(section)
    if not isinstance(entry, dict):
        return ({}, "")
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    cache_day = str(entry.get("cache_day") or "").strip()
    if not cache_day:
        # Backward compatibility for old TTL-based rows.
        legacy_expires = float(entry.get("expires_at") or 0.0)
        if legacy_expires > time.time():
            cache_day = _current_reference_day_token()
    return (payload, cache_day)


def _reference_cache_set_entry_unlocked(section: str, payload: dict[str, Any]) -> None:
    """Store one shared cache section for the current cache day."""
    global _REFERENCE_DISK_CACHE_DIRTY
    _prepare_reference_disk_cache_unlocked()
    _REFERENCE_DISK_CACHE[section] = {
        "cache_day": _current_reference_day_token(),
        "payload": payload,
    }
    _REFERENCE_DISK_CACHE_DIRTY = True
    _save_reference_disk_cache_unlocked()


def _set_reference_cache_source_unlocked(section: str, source: str) -> None:
    _REFERENCE_CACHE_LAST_SOURCE[section] = source


def _encode_int_key_dict(data: dict[int, Any]) -> dict[str, Any]:
    return {str(int(k)): v for k, v in data.items() if int(k) > 0}


def _decode_int_key_dict(data: Any, value_cast: Any = str) -> dict[int, Any]:
    out: dict[int, Any] = {}
    if not isinstance(data, dict):
        return out
    for raw_key, raw_val in data.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            continue
        if key <= 0:
            continue
        try:
            out[key] = value_cast(raw_val)
        except (TypeError, ValueError):
            continue
    return out


def _encode_symbol_key_dict(data: dict[tuple[str, str], Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for (exchange, symbol), value in data.items():
        exch = str(exchange or "").strip().upper()
        sym = _normalise_symbol(symbol)
        if exch and sym:
            out[f"{exch}|{sym}"] = value
    return out


def _decode_symbol_key_dict(data: Any, value_cast: Any = str) -> dict[tuple[str, str], Any]:
    out: dict[tuple[str, str], Any] = {}
    if not isinstance(data, dict):
        return out
    for raw_key, raw_val in data.items():
        if not isinstance(raw_key, str) or "|" not in raw_key:
            continue
        exch, sym = raw_key.split("|", 1)
        exch = exch.strip().upper()
        sym = _normalise_symbol(sym)
        if not exch or not sym:
            continue
        try:
            out[(exch, sym)] = value_cast(raw_val)
        except (TypeError, ValueError):
            continue
    return out


def _load_yfinance_cache_if_needed() -> None:
    global _YFINANCE_CACHE_LOADED
    if _YFINANCE_CACHE_LOADED:
        return

    with _YFINANCE_CACHE_LOCK:
        if _YFINANCE_CACHE_LOADED:
            return

        try:
            payload = read_section("yfinance")
            mapping = payload.get("mapping") if isinstance(payload, dict) else {}
            cache_day = str(payload.get("cache_day") or "").strip() if isinstance(payload, dict) else ""
            if not mapping:
                root = load_model_cache()
                if isinstance(root, dict) and isinstance(root.get("mapping"), dict):
                    mapping = root["mapping"]
                    logger.debug(
                        "Yfinance mappings loaded from legacy root-level mapping key "
                        "(re-run scripts/build_cache.py to normalize model_cache.json)"
                    )
            for k, v in (mapping or {}).items():
                # key is stored as "EXCHANGE|SYMBOL"
                try:
                    exch, sym = str(k).split("|", 1)
                except ValueError:
                    continue
                key = _yfinance_cache_key(exch, sym)
                if isinstance(v, dict):
                    ind = _normalise_name(v.get("industry"))
                    sec = _normalise_name(v.get("sector"))
                else:
                    ind = _normalise_name(v)
                    sec = ""
                _CACHED_YFINANCE_KEY_TO_INDUSTRY[key] = ind
                _CACHED_YFINANCE_KEY_TO_SECTOR[key] = sec
            globals()["_YFINANCE_CACHE_DAY"] = cache_day
        except Exception as exc:
            logger.warning("Failed to read yfinance cache file: %s", exc)

        _YFINANCE_CACHE_LOADED = True


def _persist_yfinance_cache() -> None:
    """Persist cached yfinance mapping to disk (best-effort)."""
    with _YFINANCE_CACHE_LOCK:
        try:
            mapping: dict[str, dict[str, str]] = {}
            all_keys = set(_CACHED_YFINANCE_KEY_TO_INDUSTRY) | set(
                _CACHED_YFINANCE_KEY_TO_SECTOR
            )
            for exch, sym in all_keys:
                ind = _CACHED_YFINANCE_KEY_TO_INDUSTRY.get((exch, sym), "")
                sec = _CACHED_YFINANCE_KEY_TO_SECTOR.get((exch, sym), "")
                mapping[f"{exch}|{sym}"] = {
                    "industry": ind,
                    "sector": sec,
                }

            payload = {
                "mapping": mapping,
                "cache_day": _YFINANCE_CACHE_DAY,
            }
            update_section("yfinance", lambda _: dict(payload))
        except Exception as exc:
            logger.warning("Failed to persist yfinance cache: %s", exc)


def _maybe_start_monthly_yfinance_refresh() -> None:
    """Start a daily background refresh aligned to the 09:00 IST cache day."""
    global _YFINANCE_REFRESH_THREAD_STARTED, _YFINANCE_CACHE_REFRESH_IN_PROGRESS, _YFINANCE_CACHE_LAST_SOURCE

    _load_yfinance_cache_if_needed()
    with _YFINANCE_CACHE_LOCK:
        if _YFINANCE_REFRESH_THREAD_STARTED:
            return

        cache_has_entries = bool(_CACHED_YFINANCE_KEY_TO_INDUSTRY)
        if not cache_has_entries:
            return

        if _YFINANCE_CACHE_DAY == _current_reference_day_token():
            _YFINANCE_CACHE_LAST_SOURCE = "memory"
            return

        _YFINANCE_CACHE_LAST_SOURCE = "disk_stale_bg_refresh"
        _YFINANCE_REFRESH_THREAD_STARTED = True

    def _refresh_job() -> None:
        global _YFINANCE_CACHE_REFRESH_IN_PROGRESS, _YFINANCE_CACHE_DAY, _YFINANCE_REFRESH_THREAD_STARTED, _YFINANCE_CACHE_LAST_SOURCE
        with _YFINANCE_CACHE_LOCK:
            if _YFINANCE_CACHE_REFRESH_IN_PROGRESS:
                return
            _YFINANCE_CACHE_REFRESH_IN_PROGRESS = True

        try:
            if yf is None:
                return

            with _YFINANCE_CACHE_LOCK:
                keys = list(_CACHED_YFINANCE_KEY_TO_INDUSTRY.keys())

            updated = 0
            for exch, sym in keys:
                if not sym:
                    continue
                yf_symbol = sym
                if "." not in yf_symbol:
                    suffix = ".NS" if exch == "NSE" else ".BO"
                    yf_symbol = f"{yf_symbol}{suffix}"

                try:
                    info = yf.Ticker(yf_symbol).info or {}
                    ind = _normalise_name(info.get("industry") or info.get("sector"))
                    sec = _normalise_name(info.get("sector") or "")
                    if not sec:
                        sec = ind
                    with _YFINANCE_CACHE_LOCK:
                        _CACHED_YFINANCE_KEY_TO_INDUSTRY[(exch, sym)] = ind
                        _CACHED_YFINANCE_KEY_TO_SECTOR[(exch, sym)] = sec
                    if ind:
                        updated += 1
                except Exception:
                    # Ignore per-ticker failures in background refresh.
                    continue

            _YFINANCE_CACHE_DAY = _current_reference_day_token()
            if updated:
                logger.info("yfinance cache refreshed for %d symbols", updated)
            _persist_yfinance_cache()
            _YFINANCE_CACHE_LAST_SOURCE = "network_bg_refresh"
            notify_dashboard_cache_refresh()
        finally:
            with _YFINANCE_CACHE_LOCK:
                _YFINANCE_CACHE_REFRESH_IN_PROGRESS = False
                _YFINANCE_REFRESH_THREAD_STARTED = False

    if not start_background_refresh_job("yfinance-daily", _refresh_job):
        with _YFINANCE_CACHE_LOCK:
            _YFINANCE_REFRESH_THREAD_STARTED = False


def _maybe_start_single_symbol_yfinance_refresh(exchange: str, symbol: str) -> None:
    """Best-effort async refresh for one symbol when cache row is missing."""
    clean_exchange = str(exchange or "").strip().upper()
    clean_symbol = _normalise_symbol(symbol)
    if yf is None or clean_exchange not in EQUITY_EXCHANGES or not clean_symbol:
        return

    key = _yfinance_cache_key(clean_exchange, clean_symbol)
    with _YFINANCE_CACHE_LOCK:
        if key in _YFINANCE_SYMBOL_REFRESH_IN_PROGRESS:
            return
        _YFINANCE_SYMBOL_REFRESH_IN_PROGRESS.add(key)

    def _refresh_one() -> None:
        try:
            yf_symbol = clean_symbol
            if "." not in yf_symbol:
                suffix = ".NS" if clean_exchange == "NSE" else ".BO"
                yf_symbol = f"{yf_symbol}{suffix}"
            info = yf.Ticker(yf_symbol).info or {}
            y_ind = _normalise_name(info.get("industry") or info.get("sector"))
            y_sec = _normalise_name(info.get("sector") or "")
            if not y_sec:
                y_sec = y_ind

            global _YFINANCE_CACHE_DAY
            with _YFINANCE_CACHE_LOCK:
                _CACHED_YFINANCE_KEY_TO_INDUSTRY[key] = y_ind
                _CACHED_YFINANCE_KEY_TO_SECTOR[key] = y_sec
                _YFINANCE_CACHE_DAY = _current_reference_day_token()
                globals()["_YFINANCE_CACHE_LAST_SOURCE"] = "network_bg_refresh"
            _persist_yfinance_cache()
            notify_dashboard_cache_refresh()
        except Exception as exc:
            logger.warning(
                "background yfinance refresh failed for %s:%s: %s",
                clean_exchange,
                clean_symbol,
                exc,
            )
        finally:
            with _YFINANCE_CACHE_LOCK:
                _YFINANCE_SYMBOL_REFRESH_IN_PROGRESS.discard(key)

    start_background_refresh_job(f"yfinance-symbol-{clean_exchange}-{clean_symbol}", _refresh_one)


def _normalise_name(raw: Any) -> str:
    """Return a trimmed company name or empty string."""
    return str(raw or "").strip()


def _normalise_symbol(raw: Any) -> str:
    """Return a trimmed symbol in uppercase or empty string."""
    return str(raw or "").strip().upper()


def _normalise_isin(raw: Any) -> str:
    s = str(raw or "").strip().upper().replace(" ", "")
    return s


def _kite_row_is_equity_cash(row: dict) -> bool:
    return str(row.get("instrument_type") or "").strip().upper() == "EQ"


def _kite_row_industry(row: dict) -> str:
    """Industry / sector string from a Kite instrument row (EQ only)."""
    if not _kite_row_is_equity_cash(row):
        return ""
    return _normalise_name(
        row.get("industry")
        or row.get("Industry")
        or row.get("sector")
        or row.get("Sector")
    )


def _kite_row_sector_only(row: dict) -> str:
    """Exchange ``sector`` field from Kite instrument row (EQ only)."""
    if not _kite_row_is_equity_cash(row):
        return ""
    return _normalise_name(row.get("sector") or row.get("Sector"))


def _build_cash_equity_maps(kite) -> tuple[
    dict[int, str],
    dict[tuple[str, str], str],
    dict[tuple[str, str], int],
    dict[int, str],
    dict[tuple[str, str], str],
    dict[int, str],
    dict[tuple[str, str], str],
    dict[int, str],
    dict[tuple[str, str], str],
]:
    """Build fresh cash-equity lookup maps from Kite instruments API."""
    token_to_name: dict[int, str] = {}
    symbol_to_name: dict[tuple[str, str], str] = {}
    symbol_to_token: dict[tuple[str, str], int] = {}
    token_to_industry: dict[int, str] = {}
    symbol_to_industry: dict[tuple[str, str], str] = {}
    token_to_kite_sector: dict[int, str] = {}
    symbol_to_kite_sector: dict[tuple[str, str], str] = {}
    token_to_isin: dict[int, str] = {}
    symbol_to_isin: dict[tuple[str, str], str] = {}

    for exchange in EQUITY_EXCHANGES:
        try:
            instruments = kite.instruments(exchange) or []
        except Exception:
            continue

        for row in instruments:
            symbol = _normalise_symbol(row.get("tradingsymbol"))
            token = int(row.get("instrument_token") or 0)
            if symbol and token > 0:
                symbol_to_token[(exchange, symbol)] = token

            k_ind = _kite_row_industry(row)
            if k_ind:
                if token > 0:
                    token_to_industry[token] = k_ind
                if symbol:
                    symbol_to_industry[(exchange, symbol)] = k_ind

            k_sec = _kite_row_sector_only(row)
            if k_sec:
                if token > 0:
                    token_to_kite_sector[token] = k_sec
                if symbol:
                    symbol_to_kite_sector[(exchange, symbol)] = k_sec

            if _kite_row_is_equity_cash(row):
                isin = _normalise_isin(row.get("isin") or row.get("ISIN"))
                if isin:
                    if token > 0:
                        token_to_isin[token] = isin
                    if symbol:
                        symbol_to_isin[(exchange, symbol)] = isin

            name = _normalise_name(row.get("name"))
            if not name:
                continue

            if symbol:
                symbol_to_name[(exchange, symbol)] = name
            if token > 0:
                token_to_name[token] = name

    return (
        token_to_name,
        symbol_to_name,
        symbol_to_token,
        token_to_industry,
        symbol_to_industry,
        token_to_kite_sector,
        symbol_to_kite_sector,
        token_to_isin,
        symbol_to_isin,
    )


def _apply_cash_equity_payload_unlocked(payload: dict[str, Any], expires_at: float) -> bool:
    """Hydrate in-memory cash-equity caches from disk payload."""
    global _CACHE_EXPIRES_AT, _CACHED_TOKEN_TO_NAME, _CACHED_SYMBOL_TO_NAME, _CACHED_SYMBOL_TO_TOKEN
    global _CACHED_TOKEN_TO_INDUSTRY, _CACHED_SYMBOL_TO_INDUSTRY
    global _CACHED_TOKEN_TO_KITE_SECTOR, _CACHED_SYMBOL_TO_KITE_SECTOR
    global _CACHED_TOKEN_TO_ISIN, _CACHED_SYMBOL_TO_ISIN
    token_to_name = _decode_int_key_dict(payload.get("token_to_name"), str)
    symbol_to_name = _decode_symbol_key_dict(payload.get("symbol_to_name"), str)
    symbol_to_token = _decode_symbol_key_dict(payload.get("symbol_to_token"), int)
    token_to_industry = _decode_int_key_dict(payload.get("token_to_industry"), str)
    symbol_to_industry = _decode_symbol_key_dict(payload.get("symbol_to_industry"), str)
    token_to_kite_sector = _decode_int_key_dict(payload.get("token_to_kite_sector"), str)
    symbol_to_kite_sector = _decode_symbol_key_dict(payload.get("symbol_to_kite_sector"), str)
    token_to_isin = _decode_int_key_dict(payload.get("token_to_isin"), str)
    symbol_to_isin = _decode_symbol_key_dict(payload.get("symbol_to_isin"), str)
    if not token_to_name or not symbol_to_name:
        return False

    _CACHED_TOKEN_TO_NAME = token_to_name
    _CACHED_SYMBOL_TO_NAME = symbol_to_name
    _CACHED_SYMBOL_TO_TOKEN = symbol_to_token
    _CACHED_TOKEN_TO_INDUSTRY = token_to_industry
    _CACHED_SYMBOL_TO_INDUSTRY = symbol_to_industry
    _CACHED_TOKEN_TO_KITE_SECTOR = token_to_kite_sector
    _CACHED_SYMBOL_TO_KITE_SECTOR = symbol_to_kite_sector
    _CACHED_TOKEN_TO_ISIN = token_to_isin
    _CACHED_SYMBOL_TO_ISIN = symbol_to_isin
    _CACHE_EXPIRES_AT = expires_at
    return True


def _cash_equity_payload_unlocked() -> dict[str, Any]:
    return {
        "token_to_name": _encode_int_key_dict(_CACHED_TOKEN_TO_NAME),
        "symbol_to_name": _encode_symbol_key_dict(_CACHED_SYMBOL_TO_NAME),
        "symbol_to_token": _encode_symbol_key_dict(_CACHED_SYMBOL_TO_TOKEN),
        "token_to_industry": _encode_int_key_dict(_CACHED_TOKEN_TO_INDUSTRY),
        "symbol_to_industry": _encode_symbol_key_dict(_CACHED_SYMBOL_TO_INDUSTRY),
        "token_to_kite_sector": _encode_int_key_dict(_CACHED_TOKEN_TO_KITE_SECTOR),
        "symbol_to_kite_sector": _encode_symbol_key_dict(_CACHED_SYMBOL_TO_KITE_SECTOR),
        "token_to_isin": _encode_int_key_dict(_CACHED_TOKEN_TO_ISIN),
        "symbol_to_isin": _encode_symbol_key_dict(_CACHED_SYMBOL_TO_ISIN),
    }


def _maybe_start_cash_equity_refresh_unlocked(kite) -> None:
    """Refresh cash-equity mappings in background when stale."""
    global _CASH_EQUITY_REFRESH_IN_PROGRESS
    if _CASH_EQUITY_REFRESH_IN_PROGRESS:
        return
    _CASH_EQUITY_REFRESH_IN_PROGRESS = True

    def _job() -> None:
        global _CACHE_EXPIRES_AT, _CASH_EQUITY_REFRESH_IN_PROGRESS
        ok = False
        try:
            (
                token_to_name,
                symbol_to_name,
                symbol_to_token,
                token_to_industry,
                symbol_to_industry,
                token_to_kite_sector,
                symbol_to_kite_sector,
                token_to_isin,
                symbol_to_isin,
            ) = _build_cash_equity_maps(kite)
            if not token_to_name or not symbol_to_name:
                return
            with _CACHE_LOCK:
                _CACHED_TOKEN_TO_NAME.clear()
                _CACHED_TOKEN_TO_NAME.update(token_to_name)
                _CACHED_SYMBOL_TO_NAME.clear()
                _CACHED_SYMBOL_TO_NAME.update(symbol_to_name)
                _CACHED_SYMBOL_TO_TOKEN.clear()
                _CACHED_SYMBOL_TO_TOKEN.update(symbol_to_token)
                _CACHED_TOKEN_TO_INDUSTRY.clear()
                _CACHED_TOKEN_TO_INDUSTRY.update(token_to_industry)
                _CACHED_SYMBOL_TO_INDUSTRY.clear()
                _CACHED_SYMBOL_TO_INDUSTRY.update(symbol_to_industry)
                _CACHED_TOKEN_TO_KITE_SECTOR.clear()
                _CACHED_TOKEN_TO_KITE_SECTOR.update(token_to_kite_sector)
                _CACHED_SYMBOL_TO_KITE_SECTOR.clear()
                _CACHED_SYMBOL_TO_KITE_SECTOR.update(symbol_to_kite_sector)
                _CACHED_TOKEN_TO_ISIN.clear()
                _CACHED_TOKEN_TO_ISIN.update(token_to_isin)
                _CACHED_SYMBOL_TO_ISIN.clear()
                _CACHED_SYMBOL_TO_ISIN.update(symbol_to_isin)
                _CACHE_EXPIRES_AT = _next_reference_cutoff_epoch()
                _reference_cache_set_entry_unlocked(
                    "cash_equity",
                    _cash_equity_payload_unlocked(),
                )
                _set_reference_cache_source_unlocked("cash_equity", "network_bg_refresh")
                ok = True
        except Exception:
            pass
        finally:
            with _CACHE_LOCK:
                if not ok:
                    _set_reference_cache_source_unlocked("cash_equity", "network_bg_refresh_failed")
                _CASH_EQUITY_REFRESH_IN_PROGRESS = False
            if ok:
                notify_dashboard_cache_refresh()

    start_background_refresh_job("reference-cash-equity", _job)


def _refresh_cash_equity_cache(kite) -> None:
    """Refresh equity instrument caches (disk-backed + background refresh)."""
    global _CACHE_EXPIRES_AT, _CACHED_TOKEN_TO_NAME, _CACHED_SYMBOL_TO_NAME, _CACHED_SYMBOL_TO_TOKEN
    global _CACHED_TOKEN_TO_INDUSTRY, _CACHED_SYMBOL_TO_INDUSTRY
    global _CACHED_TOKEN_TO_KITE_SECTOR, _CACHED_SYMBOL_TO_KITE_SECTOR
    global _CACHED_TOKEN_TO_ISIN, _CACHED_SYMBOL_TO_ISIN
    now = time.time()
    if now < _CACHE_EXPIRES_AT and _CACHED_TOKEN_TO_NAME and _CACHED_SYMBOL_TO_NAME:
        _set_reference_cache_source_unlocked("cash_equity", "memory")
        return

    day = _current_reference_day_token()
    payload, cache_day = _reference_cache_get_entry_unlocked("cash_equity")
    if payload and _apply_cash_equity_payload_unlocked(payload, _next_reference_cutoff_epoch()):
        if cache_day != day:
            _set_reference_cache_source_unlocked("cash_equity", "disk_stale_bg_refresh")
            _maybe_start_cash_equity_refresh_unlocked(kite)
        else:
            _set_reference_cache_source_unlocked("cash_equity", "disk")
        return

    _set_reference_cache_source_unlocked("cash_equity", "cold_start_bg_refresh")
    _maybe_start_cash_equity_refresh_unlocked(kite)


def get_cash_equity_name_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Return cached (token->name, (exchange,symbol)->name) mappings.

    The cache is refreshed at most once per hour to avoid re-downloading
    the large instrument master on every dashboard refresh.
    """
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return (dict(_CACHED_TOKEN_TO_NAME), dict(_CACHED_SYMBOL_TO_NAME))


def get_cash_equity_kite_sector_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Cached Kite ``sector`` column only (EQ rows)."""
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return (dict(_CACHED_TOKEN_TO_KITE_SECTOR), dict(_CACHED_SYMBOL_TO_KITE_SECTOR))


def get_cash_equity_isin_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Cached (token->ISIN, (exchange,symbol)->ISIN) for cash EQ instruments."""
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return (dict(_CACHED_TOKEN_TO_ISIN), dict(_CACHED_SYMBOL_TO_ISIN))


def get_nse_symbol_to_token_lookup(kite) -> dict[str, int]:
    """Return cached NSE ``tradingsymbol -> instrument_token`` mapping."""
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return {
            symbol: token
            for (exchange, symbol), token in _CACHED_SYMBOL_TO_TOKEN.items()
            if exchange == "NSE" and token > 0
        }


def _fetch_nse_industry_csv_body(url: str) -> str | None:
    req = Request(url, headers=_NSE_HEADERS)
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("Failed to fetch NSE industry CSV %s: %s", url, exc)
        return None


def _merge_industry_rows(body: str, nse_symbol_out: dict[str, str], isin_out: dict[str, str]) -> None:
    reader = csv.DictReader(io.StringIO(body))
    for row in reader:
        r = row or {}
        symbol = _normalise_symbol(r.get("Symbol"))
        industry = _normalise_name(r.get("Industry"))
        isin = _normalise_isin(r.get("ISIN Code") or r.get("ISIN"))
        if symbol and industry:
            nse_symbol_out[symbol] = industry
        if isin and industry:
            isin_out[isin] = industry


def _nse_merged_payload_unlocked() -> dict[str, Any]:
    return {
        "nse_symbol_to_industry": _CACHED_NSE_SYMBOL_TO_INDUSTRY,
        "isin_to_industry": _CACHED_ISIN_TO_INDUSTRY,
    }


def _apply_nse_merged_payload_unlocked(payload: dict[str, Any], expires_at: float) -> bool:
    global _NSE_MERGED_INDUSTRY_EXPIRES_AT, _CACHED_NSE_SYMBOL_TO_INDUSTRY, _CACHED_ISIN_TO_INDUSTRY
    nse_symbol_to_industry = payload.get("nse_symbol_to_industry")
    isin_to_industry = payload.get("isin_to_industry")
    if not isinstance(nse_symbol_to_industry, dict) or not isinstance(isin_to_industry, dict):
        return False
    clean_nse: dict[str, str] = {}
    clean_isin: dict[str, str] = {}
    for k, v in nse_symbol_to_industry.items():
        symbol = _normalise_symbol(k)
        industry = _normalise_name(v)
        if symbol and industry:
            clean_nse[symbol] = industry
    for k, v in isin_to_industry.items():
        isin = _normalise_isin(k)
        industry = _normalise_name(v)
        if isin and industry:
            clean_isin[isin] = industry
    if not clean_nse and not clean_isin:
        return False
    _CACHED_NSE_SYMBOL_TO_INDUSTRY = clean_nse
    _CACHED_ISIN_TO_INDUSTRY = clean_isin
    _NSE_MERGED_INDUSTRY_EXPIRES_AT = expires_at
    return True


def _fetch_nse_merged_industry_maps() -> tuple[dict[str, str], dict[str, str], bool]:
    nse_sym: dict[str, str] = {}
    isin_map: dict[str, str] = {}
    any_ok = False
    for url in _NSE_INDUSTRY_CSV_URLS:
        body = _fetch_nse_industry_csv_body(url)
        if not body:
            continue
        any_ok = True
        _merge_industry_rows(body, nse_sym, isin_map)
    return (nse_sym, isin_map, any_ok)


def _maybe_start_nse_merged_refresh_unlocked() -> None:
    global _NSE_MERGED_REFRESH_IN_PROGRESS
    if _NSE_MERGED_REFRESH_IN_PROGRESS:
        return
    _NSE_MERGED_REFRESH_IN_PROGRESS = True

    def _job() -> None:
        global _NSE_MERGED_REFRESH_IN_PROGRESS, _NSE_MERGED_INDUSTRY_EXPIRES_AT
        global _CACHED_NSE_SYMBOL_TO_INDUSTRY, _CACHED_ISIN_TO_INDUSTRY
        ok = False
        try:
            nse_sym, isin_map, any_ok = _fetch_nse_merged_industry_maps()
            if not any_ok:
                return
            with _CACHE_LOCK:
                _CACHED_NSE_SYMBOL_TO_INDUSTRY = nse_sym
                _CACHED_ISIN_TO_INDUSTRY = isin_map
                _NSE_MERGED_INDUSTRY_EXPIRES_AT = _next_reference_cutoff_epoch()
                _reference_cache_set_entry_unlocked(
                    "nse_merged_industry",
                    _nse_merged_payload_unlocked(),
                )
                _set_reference_cache_source_unlocked("nse_merged_industry", "network_bg_refresh")
                ok = True
        finally:
            with _CACHE_LOCK:
                if not ok:
                    _set_reference_cache_source_unlocked("nse_merged_industry", "network_bg_refresh_failed")
                _NSE_MERGED_REFRESH_IN_PROGRESS = False
            if ok:
                notify_dashboard_cache_refresh()

    start_background_refresh_job("reference-nse-merged-industry", _job)


def _refresh_nse_merged_industry_unlocked() -> bool:
    """Merge Industry + ISIN from multiple NSE index CSVs. Returns False if all failed."""
    global _NSE_MERGED_INDUSTRY_EXPIRES_AT, _CACHED_NSE_SYMBOL_TO_INDUSTRY, _CACHED_ISIN_TO_INDUSTRY
    now = time.time()
    if now < _NSE_MERGED_INDUSTRY_EXPIRES_AT and (
        _CACHED_NSE_SYMBOL_TO_INDUSTRY or _CACHED_ISIN_TO_INDUSTRY
    ):
        _set_reference_cache_source_unlocked("nse_merged_industry", "memory")
        return True

    day = _current_reference_day_token()
    payload, cache_day = _reference_cache_get_entry_unlocked("nse_merged_industry")
    if payload and _apply_nse_merged_payload_unlocked(payload, _next_reference_cutoff_epoch()):
        if cache_day != day:
            _set_reference_cache_source_unlocked("nse_merged_industry", "disk_stale_bg_refresh")
            _maybe_start_nse_merged_refresh_unlocked()
        else:
            _set_reference_cache_source_unlocked("nse_merged_industry", "disk")
        return True

    _set_reference_cache_source_unlocked("nse_merged_industry", "cold_start_bg_refresh")
    _maybe_start_nse_merged_refresh_unlocked()
    return bool(_CACHED_NSE_SYMBOL_TO_INDUSTRY or _CACHED_ISIN_TO_INDUSTRY)


def get_nse_symbol_to_industry() -> dict[str, str]:
    """NSE ``Symbol -> Industry`` merged from broad NSE index constituent CSVs."""
    with _CACHE_LOCK:
        if not _refresh_nse_merged_industry_unlocked():
            return {}
        return dict(_CACHED_NSE_SYMBOL_TO_INDUSTRY)


def get_isin_to_industry() -> dict[str, str]:
    """``ISIN -> Industry`` merged from the same NSE index CSVs (covers cross-listed names)."""
    with _CACHE_LOCK:
        if not _refresh_nse_merged_industry_unlocked():
            return {}
        return dict(_CACHED_ISIN_TO_INDUSTRY)


def _refresh_nifty50_cache_unlocked() -> bool:
    """Load Nifty50 symbol order from NSE CSV (watch list)."""
    global _NIFTY50_CACHE_EXPIRES_AT, _CACHED_NIFTY50_SYMBOLS
    now = time.time()
    if now < _NIFTY50_CACHE_EXPIRES_AT and _CACHED_NIFTY50_SYMBOLS:
        _set_reference_cache_source_unlocked("nifty50_symbols", "memory")
        return True

    day = _current_reference_day_token()
    payload, cache_day = _reference_cache_get_entry_unlocked("nifty50_symbols")
    symbols_payload = payload.get("symbols") if isinstance(payload, dict) else None
    if isinstance(symbols_payload, list):
        symbols: list[str] = []
        seen: set[str] = set()
        for raw in symbols_payload:
            symbol = _normalise_symbol(raw)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
        if symbols:
            _CACHED_NIFTY50_SYMBOLS = symbols
            _NIFTY50_CACHE_EXPIRES_AT = _next_reference_cutoff_epoch()
            if cache_day != day:
                _set_reference_cache_source_unlocked("nifty50_symbols", "disk_stale_bg_refresh")
                _maybe_start_nifty50_refresh_unlocked()
            else:
                _set_reference_cache_source_unlocked("nifty50_symbols", "disk")
            return True

    _set_reference_cache_source_unlocked("nifty50_symbols", "cold_start_bg_refresh")
    _maybe_start_nifty50_refresh_unlocked()
    return bool(_CACHED_NIFTY50_SYMBOLS)


def _refresh_nifty50_from_network_unlocked() -> bool:
    global _NIFTY50_CACHE_EXPIRES_AT
    body = _fetch_nse_industry_csv_body(_NIFTY50_CSV_URL)
    if not body:
        return False

    reader = csv.DictReader(io.StringIO(body))
    ordered_unique: list[str] = []
    seen: set[str] = set()
    for row in reader:
        symbol = _normalise_symbol((row or {}).get("Symbol"))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        ordered_unique.append(symbol)

    _CACHED_NIFTY50_SYMBOLS = ordered_unique
    _NIFTY50_CACHE_EXPIRES_AT = _next_reference_cutoff_epoch()
    _reference_cache_set_entry_unlocked(
        "nifty50_symbols",
        {"symbols": ordered_unique},
    )
    return True


def _maybe_start_nifty50_refresh_unlocked() -> None:
    global _NIFTY50_REFRESH_IN_PROGRESS
    if _NIFTY50_REFRESH_IN_PROGRESS:
        return
    _NIFTY50_REFRESH_IN_PROGRESS = True

    def _job() -> None:
        global _NIFTY50_REFRESH_IN_PROGRESS, _NIFTY50_CACHE_EXPIRES_AT
        ok = False
        try:
            body = _fetch_nse_industry_csv_body(_NIFTY50_CSV_URL)
            if not body:
                return
            reader = csv.DictReader(io.StringIO(body))
            ordered_unique: list[str] = []
            seen: set[str] = set()
            for row in reader:
                symbol = _normalise_symbol((row or {}).get("Symbol"))
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                ordered_unique.append(symbol)
            if not ordered_unique:
                return
            with _CACHE_LOCK:
                _CACHED_NIFTY50_SYMBOLS[:] = ordered_unique
                _NIFTY50_CACHE_EXPIRES_AT = _next_reference_cutoff_epoch()
                _reference_cache_set_entry_unlocked(
                    "nifty50_symbols",
                    {"symbols": ordered_unique},
                )
                _set_reference_cache_source_unlocked("nifty50_symbols", "network_bg_refresh")
                ok = True
        finally:
            with _CACHE_LOCK:
                if not ok:
                    _set_reference_cache_source_unlocked("nifty50_symbols", "network_bg_refresh_failed")
                _NIFTY50_REFRESH_IN_PROGRESS = False
            if ok:
                notify_dashboard_cache_refresh()

    start_background_refresh_job("reference-nifty50", _job)


def get_nifty50_symbols() -> list[str]:
    """Fetch and cache Nifty50 constituents from NSE archive CSV."""
    with _CACHE_LOCK:
        if not _refresh_nifty50_cache_unlocked():
            return []
        return list(_CACHED_NIFTY50_SYMBOLS)


def get_reference_cache_debug_snapshot() -> dict[str, dict[str, Any]]:
    """Return cache-source/expiry metadata for dashboard timing logs."""
    now = time.time()
    with _CACHE_LOCK:
        return {
            "cash_equity": {
                "source": _REFERENCE_CACHE_LAST_SOURCE.get("cash_equity", "unknown"),
                "expires_in_ms": max(0.0, (_CACHE_EXPIRES_AT - now) * 1000.0),
                "refresh_in_progress": _CASH_EQUITY_REFRESH_IN_PROGRESS,
            },
            "nse_merged_industry": {
                "source": _REFERENCE_CACHE_LAST_SOURCE.get("nse_merged_industry", "unknown"),
                "expires_in_ms": max(0.0, (_NSE_MERGED_INDUSTRY_EXPIRES_AT - now) * 1000.0),
                "refresh_in_progress": _NSE_MERGED_REFRESH_IN_PROGRESS,
            },
            "nifty50_symbols": {
                "source": _REFERENCE_CACHE_LAST_SOURCE.get("nifty50_symbols", "unknown"),
                "expires_in_ms": max(0.0, (_NIFTY50_CACHE_EXPIRES_AT - now) * 1000.0),
                "refresh_in_progress": _NIFTY50_REFRESH_IN_PROGRESS,
            },
            "yfinance": {
                "source": _YFINANCE_CACHE_LAST_SOURCE,
                "expires_in_ms": max(0.0, (_next_reference_cutoff_epoch() - now) * 1000.0),
                "refresh_in_progress": (
                    _YFINANCE_CACHE_REFRESH_IN_PROGRESS
                    or bool(_YFINANCE_SYMBOL_REFRESH_IN_PROGRESS)
                ),
            },
        }


def warm_reference_caches(kite=None, *, force_refresh: bool = False) -> None:
    """Best-effort warmup for heavy instrument/NSE reference lookups.

    If ``force_refresh`` is True, trigger background refresh jobs at startup
    even when existing cache entries are still within TTL.
    """
    if kite is not None:
        try:
            get_cash_equity_name_lookups(kite)
            get_cash_equity_kite_sector_lookups(kite)
            get_cash_equity_isin_lookups(kite)
        except Exception as exc:
            logger.warning("Instrument lookup warmup failed: %s", exc)
    try:
        get_nse_symbol_to_industry()
        get_isin_to_industry()
        get_nifty50_symbols()
    except Exception as exc:
        logger.warning("NSE reference lookup warmup failed: %s", exc)
    if force_refresh:
        try:
            with _CACHE_LOCK:
                if kite is not None:
                    _maybe_start_cash_equity_refresh_unlocked(kite)
                _maybe_start_nse_merged_refresh_unlocked()
                _maybe_start_nifty50_refresh_unlocked()
        except Exception as exc:
            logger.warning("Reference cache startup refresh failed: %s", exc)


def resolve_equity_sector(
    symbol: str,
    exchange: str | None,
    instrument_token: int | None,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> str:
    """Resolve a display **sector** for NSE/BSE cash equities.

    Order: yfinance ``sector`` (else cached ``industry`` for the same key(s)),
    Kite ``sector`` column, then NSE CSV ``Industry`` mapped by symbol (also
    used for BSE when the symbol matches), then reference ISIN→industry maps.
    """
    clean_symbol = _normalise_symbol(symbol)
    clean_exchange = str(exchange or "").strip().upper()
    token = int(instrument_token or 0)

    if clean_exchange not in EQUITY_EXCHANGES:
        return ""

    instrument_name = ""
    if token > 0:
        instrument_name = _normalise_name(token_to_name.get(token))
    if not instrument_name and clean_symbol:
        instrument_name = _normalise_name(symbol_to_name.get((clean_exchange, clean_symbol)))

    # ETFs often lack reliable sector metadata. If symbol/name indicates ETF,
    # force a dedicated sector label.
    if "ETF" in clean_symbol or "ETF" in instrument_name.upper():
        return "ETF"

    sec = ""
    if clean_symbol:
        _maybe_start_monthly_yfinance_refresh()
        # yfinance cache keys are exchange-scoped ("NSE|TITAN" vs "BSE|TITAN").
        # Offline warmup (build_cache) defaults to NSE; use sibling exchange as
        # fallback so BSE holdings still reuse the same fundamental sector label.
        yfinance_keys = [_yfinance_cache_key(clean_exchange, clean_symbol)]
        if clean_exchange == "NSE":
            yfinance_keys.append(_yfinance_cache_key("BSE", clean_symbol))
        elif clean_exchange == "BSE":
            yfinance_keys.append(_yfinance_cache_key("NSE", clean_symbol))

        _load_yfinance_cache_if_needed()
        with _YFINANCE_CACHE_LOCK:
            cached_sec = ""
            cached_ind = ""
            matched_key = None
            matched_ind_key = None
            for y_key in yfinance_keys:
                cs = _CACHED_YFINANCE_KEY_TO_SECTOR.get(y_key)
                if cs and not cached_sec:
                    cached_sec = cs
                    matched_key = y_key
                ci = _CACHED_YFINANCE_KEY_TO_INDUSTRY.get(y_key) or ""
                if ci and not cached_ind:
                    cached_ind = ci
                    matched_ind_key = y_key
                if cached_sec:
                    break

        if cached_sec:
            sec = cached_sec
            logger.debug(
                "Sector for %s resolved via yfinance cache (%s|%s): %s",
                f"{clean_exchange}:{clean_symbol}",
                matched_key[0] if matched_key else "?",
                matched_key[1] if matched_key else "?",
                sec,
            )
        elif cached_ind:
            sec = cached_ind
            logger.debug(
                "Sector for %s resolved via yfinance cache industry fallback (%s|%s): %s",
                f"{clean_exchange}:{clean_symbol}",
                matched_ind_key[0] if matched_ind_key else "?",
                matched_ind_key[1] if matched_ind_key else "?",
                sec,
            )
        else:
            # Do not block request path with yfinance network fetches.
            # Queue a best-effort background refresh and fall back to other sources.
            _maybe_start_single_symbol_yfinance_refresh(clean_exchange, clean_symbol)

    if not sec and token > 0:
        sec = _normalise_name(token_to_kite_sector.get(token))
    if not sec and clean_symbol:
        sec = _normalise_name(symbol_to_kite_sector.get((clean_exchange, clean_symbol)))
    if not sec and clean_symbol:
        sec = _normalise_name(nse_symbol_to_industry.get(clean_symbol))
    if not sec:
        isin = ""
        if token > 0:
            isin = _normalise_isin(token_to_isin.get(token))
        if not isin and clean_symbol:
            isin = _normalise_isin(symbol_to_isin.get((clean_exchange, clean_symbol)))
        if not isin and clean_symbol:
            if clean_exchange == "BSE":
                isin = _normalise_isin(symbol_to_isin.get(("NSE", clean_symbol)))
            elif clean_exchange == "NSE":
                isin = _normalise_isin(symbol_to_isin.get(("BSE", clean_symbol)))
        if isin:
            sec = _normalise_name(isin_to_industry.get(isin))

    return sec


def symbol_with_company_name(
    symbol: str,
    exchange: str | None,
    instrument_token: int | None,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
) -> str:
    """Return display label as ``Company Name`` for cash equities."""
    clean_symbol = _normalise_symbol(symbol)
    clean_exchange = str(exchange or "").strip().upper()
    token = int(instrument_token or 0)

    if clean_exchange not in EQUITY_EXCHANGES:
        return clean_symbol

    name = ""
    if token > 0:
        name = _normalise_name(token_to_name.get(token))
    if not name and clean_symbol:
        name = _normalise_name(symbol_to_name.get((clean_exchange, clean_symbol)))

    if name and name.lower() != clean_symbol.lower():
        return name
    return clean_symbol
