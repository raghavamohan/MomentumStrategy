"""Kite Connect instrument master cache (NSE/BSE cash equity lookups)."""

from __future__ import annotations

import logging
import time
from typing import Any

from app.domain.reference_context import WarmupContext
from app.domain.reference_notifications import notify_reference_cache_refresh
from app.infrastructure.cache.text_normalize import normalise_isin, normalise_name, normalise_symbol
from app.infrastructure.cache.model_cache_store import start_background_refresh_job
from app.infrastructure.cache.reference_cache_internal import (
    REFERENCE_CACHE_LAST_SOURCE,
    instrument_reference_lock,
    _current_reference_day_token,
    _next_reference_cutoff_epoch,
    _reference_cache_get_entry_unlocked,
    _reference_cache_set_entry_unlocked,
    _set_reference_cache_source_unlocked,
)

logger = logging.getLogger(__name__)

_CACHE_LOCK = instrument_reference_lock

EQUITY_EXCHANGES = ("NSE", "BSE")

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

_CASH_EQUITY_REFRESH_IN_PROGRESS = False


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
        sym = normalise_symbol(symbol)
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
        sym = normalise_symbol(sym)
        if not exch or not sym:
            continue
        try:
            out[(exch, sym)] = value_cast(raw_val)
        except (TypeError, ValueError):
            continue
    return out


def _kite_row_is_equity_cash(row: dict) -> bool:
    return str(row.get("instrument_type") or "").strip().upper() == "EQ"


def _kite_row_industry(row: dict) -> str:
    """Industry / sector string from a Kite instrument row (EQ only)."""
    if not _kite_row_is_equity_cash(row):
        return ""
    return normalise_name(
        row.get("industry")
        or row.get("Industry")
        or row.get("sector")
        or row.get("Sector")
    )


def _kite_row_sector_only(row: dict) -> str:
    """Exchange ``sector`` field from Kite instrument row (EQ only)."""
    if not _kite_row_is_equity_cash(row):
        return ""
    return normalise_name(row.get("sector") or row.get("Sector"))


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
            symbol = normalise_symbol(row.get("tradingsymbol"))
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
                isin = normalise_isin(row.get("isin") or row.get("ISIN"))
                if isin:
                    if token > 0:
                        token_to_isin[token] = isin
                    if symbol:
                        symbol_to_isin[(exchange, symbol)] = isin

            name = normalise_name(row.get("name"))
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


def maybe_start_cash_equity_refresh_unlocked(kite) -> None:
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
                notify_reference_cache_refresh()

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
            maybe_start_cash_equity_refresh_unlocked(kite)
        else:
            _set_reference_cache_source_unlocked("cash_equity", "disk")
        return

    _set_reference_cache_source_unlocked("cash_equity", "cold_start_bg_refresh")
    maybe_start_cash_equity_refresh_unlocked(kite)


def get_cash_equity_name_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Return cached (token->name, (exchange,symbol)->name) mappings."""
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


def warmup(ctx: WarmupContext) -> None:
    """Populate or refresh cash-equity caches when a Kite client is available."""
    if ctx.kite is not None:
        try:
            get_cash_equity_name_lookups(ctx.kite)
            get_cash_equity_kite_sector_lookups(ctx.kite)
            get_cash_equity_isin_lookups(ctx.kite)
            get_nse_symbol_to_token_lookup(ctx.kite)
        except Exception as exc:
            logger.warning("Kite reference warmup failed: %s", exc)
    if ctx.force_refresh and ctx.kite is not None:
        try:
            with _CACHE_LOCK:
                maybe_start_cash_equity_refresh_unlocked(ctx.kite)
        except Exception as exc:
            logger.warning("Kite reference startup refresh failed: %s", exc)


def kite_reference_debug_snapshot(now: float) -> dict[str, Any]:
    """``cash_equity`` row for :func:`app.domain.portfolio_model.get_reference_cache_debug_snapshot`."""
    return {
        "source": REFERENCE_CACHE_LAST_SOURCE.get("cash_equity", "unknown"),
        "expires_in_ms": max(0.0, (_CACHE_EXPIRES_AT - now) * 1000.0),
        "refresh_in_progress": _CASH_EQUITY_REFRESH_IN_PROGRESS,
    }


__all__ = [
    "warmup",
    "get_cash_equity_isin_lookups",
    "get_cash_equity_kite_sector_lookups",
    "get_cash_equity_name_lookups",
    "get_nse_symbol_to_token_lookup",
    "kite_reference_debug_snapshot",
]
