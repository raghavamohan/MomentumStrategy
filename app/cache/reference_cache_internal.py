"""Shared ``reference_data`` disk cache, source labels, and lock.

Cash-equity instrument maps (via :mod:`app.portfolio_model` / :mod:`app.cache.kite_provider`)
and NSE CSV reference
data (in :mod:`app.cache.nse_provider`) coordinate through this module so the
``reference_data`` on-disk section and in-memory snapshot stay consistent.

Sector/industry hints from yfinance use a separate ``yfinance`` disk section;
see :mod:`app.cache.yfinance_provider`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.cache.model_cache_store import (
    current_effective_day_ist,
    next_cutoff_epoch_ist,
    read_section,
    update_section,
)

logger = logging.getLogger(__name__)

instrument_reference_lock = threading.Lock()

_REFERENCE_DISK_CACHE_LOADED = False
_REFERENCE_DISK_CACHE: dict[str, Any] = {}
_REFERENCE_DISK_CACHE_DIRTY = False

REFERENCE_CACHE_LAST_SOURCE: dict[str, str] = {
    "cash_equity": "unknown",
    "nse_merged_industry": "unknown",
    "nifty50_symbols": "unknown",
}


def _current_reference_day_token() -> str:
    return current_effective_day_ist(cutoff_hour=9)


def _next_reference_cutoff_epoch() -> float:
    return next_cutoff_epoch_ist(cutoff_hour=9)


def _prepare_reference_disk_cache_unlocked() -> None:
    """Load shared reference cache file lazily (expects caller holds ``instrument_reference_lock``)."""
    global _REFERENCE_DISK_CACHE_LOADED, _REFERENCE_DISK_CACHE
    if _REFERENCE_DISK_CACHE_LOADED:
        return
    loaded = read_section("reference_data")
    if isinstance(loaded, dict):
        _REFERENCE_DISK_CACHE = loaded
    _REFERENCE_DISK_CACHE_LOADED = True


def _save_reference_disk_cache_unlocked() -> None:
    """Persist shared reference cache file (expects caller holds lock)."""
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
    REFERENCE_CACHE_LAST_SOURCE[section] = source
