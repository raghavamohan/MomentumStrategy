"""NSE index CSV access and merged industry / Nifty50 symbol caches (provider)."""

from __future__ import annotations

import csv
import io
import logging
import threading
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.domain.reference_context import WarmupContext
from app.domain.reference_notifications import notify_reference_cache_refresh
from app.infrastructure.cache.text_normalize import normalise_isin, normalise_name, normalise_symbol
from app.infrastructure.cache.model_cache_store import (
    BaseCache,
    current_effective_day_ist,
    next_cutoff_epoch_ist,
    start_background_refresh_job,
)

logger = logging.getLogger(__name__)

# --- Constants ---
_NIFTY50_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
_NSE_INDUSTRY_CSV_URLS = (
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
_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}

# --- Provider State ---
_NSE_CACHE = BaseCache("nse_provider")
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False

_MERGED_REFRESH_IN_PROGRESS = False
_NIFTY_REFRESH_IN_PROGRESS = False

_MERGED_CACHE_DAY = ""
_NIFTY_CACHE_DAY = ""
_MERGED_SOURCE = "unknown"
_NIFTY_SOURCE = "unknown"
_EXPIRES_AT = 0.0

_SYMBOL_TO_INDUSTRY: dict[str, str] = {}
_ISIN_TO_INDUSTRY: dict[str, str] = {}
_NIFTY50_SYMBOLS: list[str] = []


def _load_cache_unlocked() -> None:
    global _CACHE_LOADED, _MERGED_CACHE_DAY, _NIFTY_CACHE_DAY, _MERGED_SOURCE, _NIFTY_SOURCE, _EXPIRES_AT
    if _CACHE_LOADED:
        return

    payload = _NSE_CACHE.read_section("nse")

    # Merged Industry
    m_entry = payload.get("nse_merged_industry")
    if isinstance(m_entry, dict):
        _MERGED_CACHE_DAY = str(m_entry.get("cache_day") or "").strip()
        p = m_entry.get("payload") or {}
        _SYMBOL_TO_INDUSTRY.update(p.get("nse_symbol_to_industry") or {})
        _ISIN_TO_INDUSTRY.update(p.get("isin_to_industry") or {})
        if _SYMBOL_TO_INDUSTRY:
            _MERGED_SOURCE = "disk"

    # Nifty 50
    n_entry = payload.get("nifty50_symbols")
    if isinstance(n_entry, dict):
        _NIFTY_CACHE_DAY = str(n_entry.get("cache_day") or "").strip()
        _NIFTY50_SYMBOLS.extend((n_entry.get("payload") or {}).get("symbols") or [])
        if _NIFTY50_SYMBOLS:
            _NIFTY_SOURCE = "disk"

    _EXPIRES_AT = next_cutoff_epoch_ist()
    _CACHE_LOADED = True


def _persist_merged_unlocked() -> None:
    _NSE_CACHE.update_section("nse", lambda root: {
        **root,
        "nse_merged_industry": {
            "cache_day": _MERGED_CACHE_DAY,
            "payload": {
                "nse_symbol_to_industry": _SYMBOL_TO_INDUSTRY,
                "isin_to_industry": _ISIN_TO_INDUSTRY,
            }
        }
    })


def _persist_nifty_unlocked() -> None:
    _NSE_CACHE.update_section("nse", lambda root: {
        **root,
        "nifty50_symbols": {
            "cache_day": _NIFTY_CACHE_DAY,
            "payload": {"symbols": _NIFTY50_SYMBOLS}
        }
    })


def _fetch_csv(url: str) -> str | None:
    try:
        req = Request(url, headers=_NSE_HEADERS)
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to fetch NSE CSV %s: %s", url, exc)
        return None


def _maybe_start_merged_refresh_unlocked() -> None:
    global _MERGED_REFRESH_IN_PROGRESS, _MERGED_SOURCE
    if _MERGED_REFRESH_IN_PROGRESS: return
    cur = current_effective_day_ist()
    if _MERGED_CACHE_DAY == cur and _SYMBOL_TO_INDUSTRY: return

    _MERGED_SOURCE = "disk_stale_bg_refresh" if _SYMBOL_TO_INDUSTRY else "cold_start_bg_refresh"
    _MERGED_REFRESH_IN_PROGRESS = True

    def _job() -> None:
        global _MERGED_REFRESH_IN_PROGRESS, _MERGED_CACHE_DAY, _MERGED_SOURCE
        ok = False
        new_sym, new_isin = {}, {}
        for url in _NSE_INDUSTRY_CSV_URLS:
            body = _fetch_csv(url)
            if not body: continue
            reader = csv.DictReader(io.StringIO(body))
            for row in reader:
                s = normalise_symbol(row.get("Symbol"))
                i = normalise_isin(row.get("ISIN Code") or row.get("ISIN"))
                ind = normalise_name(row.get("Industry"))
                if ind:
                    if s: new_sym[s] = ind
                    if i: new_isin[i] = ind

        if new_sym:
            with _CACHE_LOCK:
                _SYMBOL_TO_INDUSTRY.clear(); _SYMBOL_TO_INDUSTRY.update(new_sym)
                _ISIN_TO_INDUSTRY.clear(); _ISIN_TO_INDUSTRY.update(new_isin)
                _MERGED_CACHE_DAY = current_effective_day_ist()
                _MERGED_SOURCE = "network_bg_refresh"
                _persist_merged_unlocked()
                ok = True

        with _CACHE_LOCK:
            _MERGED_REFRESH_IN_PROGRESS = False
            if not ok: _MERGED_SOURCE = "network_bg_refresh_failed"
        if ok: notify_reference_cache_refresh()

    start_background_refresh_job("reference-nse-merged", _job)


def _maybe_start_nifty_refresh_unlocked() -> None:
    global _NIFTY_REFRESH_IN_PROGRESS, _NIFTY_SOURCE
    if _NIFTY_REFRESH_IN_PROGRESS: return
    cur = current_effective_day_ist()
    if _NIFTY_CACHE_DAY == cur and _NIFTY50_SYMBOLS: return

    _NIFTY_SOURCE = "disk_stale_bg_refresh" if _NIFTY50_SYMBOLS else "cold_start_bg_refresh"
    _NIFTY_REFRESH_IN_PROGRESS = True

    def _job() -> None:
        global _NIFTY_REFRESH_IN_PROGRESS, _NIFTY_CACHE_DAY, _NIFTY_SOURCE
        ok = False
        body = _fetch_csv(_NIFTY50_CSV_URL)
        if body:
            reader = csv.DictReader(io.StringIO(body))
            syms, seen = [], set()
            for row in reader:
                s = normalise_symbol(row.get("Symbol"))
                if s and s not in seen:
                    syms.append(s); seen.add(s)
            if syms:
                with _CACHE_LOCK:
                    _NIFTY50_SYMBOLS[:] = syms
                    _NIFTY_CACHE_DAY = current_effective_day_ist()
                    _NIFTY_SOURCE = "network_bg_refresh"
                    _persist_nifty_unlocked()
                    ok = True

        with _CACHE_LOCK:
            _NIFTY_REFRESH_IN_PROGRESS = False
            if not ok: _NIFTY_SOURCE = "network_bg_refresh_failed"
        if ok: notify_reference_cache_refresh()

    start_background_refresh_job("reference-nifty50", _job)


def get_nse_symbol_to_industry() -> dict[str, str]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        _maybe_start_merged_refresh_unlocked()
        return dict(_SYMBOL_TO_INDUSTRY)


def get_isin_to_industry() -> dict[str, str]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        _maybe_start_merged_refresh_unlocked()
        return dict(_ISIN_TO_INDUSTRY)


def get_nifty50_symbols() -> list[str]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        _maybe_start_nifty_refresh_unlocked()
        return list(_NIFTY50_SYMBOLS)


def warmup(ctx: WarmupContext) -> None:
    with _CACHE_LOCK:
        if ctx.force_refresh:
            global _MERGED_CACHE_DAY, _NIFTY_CACHE_DAY
            _MERGED_CACHE_DAY = _NIFTY_CACHE_DAY = ""
        _load_cache_unlocked()
        _maybe_start_merged_refresh_unlocked()
        _maybe_start_nifty_refresh_unlocked()


def nse_reference_debug_snapshot(now: float) -> dict[str, dict[str, Any]]:
    with _CACHE_LOCK:
        return {
            "nse_merged_industry": {
                "source": _MERGED_SOURCE,
                "expires_in_ms": max(0.0, (_EXPIRES_AT - now) * 1000.0),
                "refresh_in_progress": _MERGED_REFRESH_IN_PROGRESS,
                "cache_day": _MERGED_CACHE_DAY,
                "entries": len(_SYMBOL_TO_INDUSTRY),
            },
            "nifty50_symbols": {
                "source": _NIFTY_SOURCE,
                "expires_in_ms": max(0.0, (_EXPIRES_AT - now) * 1000.0),
                "refresh_in_progress": _NIFTY_REFRESH_IN_PROGRESS,
                "cache_day": _NIFTY_CACHE_DAY,
                "entries": len(_NIFTY50_SYMBOLS),
            },
        }


__all__ = [
    "warmup",
    "get_isin_to_industry",
    "get_nifty50_symbols",
    "get_nse_symbol_to_industry",
    "nse_reference_debug_snapshot",
]
