"""NSE index CSV access and merged industry / Nifty50 symbol caches (provider)."""

from __future__ import annotations

import csv
import io
import logging
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.reference_context import WarmupContext
from app.reference_notifications import notify_reference_cache_refresh
from app.cache.model_cache_store import start_background_refresh_job
from app.cache.reference_cache_internal import (
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

_NSE_MERGED_REFRESH_IN_PROGRESS = False
_NIFTY50_REFRESH_IN_PROGRESS = False


def _normalise_name(raw: Any) -> str:
    return str(raw or "").strip()


def _normalise_symbol(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _normalise_isin(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(" ", "")


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
                notify_reference_cache_refresh()

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
                notify_reference_cache_refresh()

    start_background_refresh_job("reference-nifty50", _job)


def get_nifty50_symbols() -> list[str]:
    """Fetch and cache Nifty50 constituents from NSE archive CSV."""
    with _CACHE_LOCK:
        if not _refresh_nifty50_cache_unlocked():
            return []
        return list(_CACHED_NIFTY50_SYMBOLS)


def nse_provider_cache_debug_snapshot(now: float) -> dict[str, dict[str, Any]]:
    """Entries merged into :func:`app.portfolio_model.get_reference_cache_debug_snapshot`."""
    return {
        "nse_merged_industry": {
            "source": REFERENCE_CACHE_LAST_SOURCE.get("nse_merged_industry", "unknown"),
            "expires_in_ms": max(0.0, (_NSE_MERGED_INDUSTRY_EXPIRES_AT - now) * 1000.0),
            "refresh_in_progress": _NSE_MERGED_REFRESH_IN_PROGRESS,
        },
        "nifty50_symbols": {
            "source": REFERENCE_CACHE_LAST_SOURCE.get("nifty50_symbols", "unknown"),
            "expires_in_ms": max(0.0, (_NIFTY50_CACHE_EXPIRES_AT - now) * 1000.0),
            "refresh_in_progress": _NIFTY50_REFRESH_IN_PROGRESS,
        },
    }


def warm_nse_provider_caches(*, force_refresh: bool = False) -> None:
    """Populate NSE CSV-backed lookups; optionally force background refresh jobs."""
    try:
        get_nse_symbol_to_industry()
        get_isin_to_industry()
        get_nifty50_symbols()
    except Exception as exc:
        logger.warning("NSE reference lookup warmup failed: %s", exc)
    if force_refresh:
        try:
            with _CACHE_LOCK:
                _maybe_start_nse_merged_refresh_unlocked()
                _maybe_start_nifty50_refresh_unlocked()
        except Exception as exc:
            logger.warning("NSE reference startup refresh failed: %s", exc)


def warmup(ctx: WarmupContext) -> None:
    """Populate NSE CSV-backed lookups; respects ``ctx.force_refresh``."""
    warm_nse_provider_caches(force_refresh=ctx.force_refresh)


__all__ = [
    "warmup",
    "get_isin_to_industry",
    "get_nifty50_symbols",
    "get_nse_symbol_to_industry",
    "nse_provider_cache_debug_snapshot",
    "warm_nse_provider_caches",
]
