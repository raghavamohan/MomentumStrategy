"""In-process + on-disk cache for Kite stock history candles.

Entries are keyed by instrument token and interval, scoped to the current
09:00-IST cache day (same rollover as dashboard quote caches). When a cached
series covers at least the requested calendar-day window, Kite is not called.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

from app.application.dashboard_view_model import historical_candles_for_stock
from app.infrastructure.auth import PROJECT_ROOT
from app.infrastructure.services.dashboard_caches import today_cache_token

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_CACHE_FILE = PROJECT_ROOT / ".cache" / "stock_history.json"
_LOCK = threading.Lock()
_MEMORY: dict[str, dict[str, Any]] = {}
_MEMORY_DAY = ""

# First paint uses a shorter window for daily (and week/month) intervals.
INITIAL_HISTORY_DAYS_DAILY = 730


def initial_history_days(interval: str, max_days: int) -> int:
    """Return the fast initial history window for ``interval``."""
    normalized = _normalize_interval(interval)
    if normalized == "day":
        return min(INITIAL_HISTORY_DAYS_DAILY, max_days)
    return max_days


def resolve_history_interval(interval: str) -> str:
    """Map UI intervals (week/month) to the Kite interval used for fetching."""
    return _normalize_interval(interval)


def get_cached_stock_history(
    kite: KiteConnect,
    instrument_token: int,
    interval: str,
    days: int,
) -> list[dict[str, Any]]:
    """Return OHLCV candles, reusing cache when the day window is covered."""
    if days <= 0:
        return []

    kite_interval = _normalize_interval(interval)
    cache_day = today_cache_token()
    cache_key = f"{instrument_token}:{kite_interval}"

    with _LOCK:
        entry = _get_entry(cache_day, cache_key)

    if entry and int(entry.get("days_fetched") or 0) >= days:
        sliced = _slice_candles_by_days(entry.get("candles") or [], days, kite_interval)
        if sliced:
            return sliced

    candles = historical_candles_for_stock(
        kite,
        instrument_token,
        days,
        kite_interval,
    )

    with _LOCK:
        prev = _get_entry(cache_day, cache_key)
        if prev and int(prev.get("days_fetched") or 0) > days:
            merged = _merge_candle_lists(prev.get("candles") or [], candles)
            store_days = int(prev.get("days_fetched") or 0)
            candles_to_store = merged
        else:
            store_days = days
            candles_to_store = candles
        _store_entry(cache_day, cache_key, store_days, candles_to_store)

    return candles


def _normalize_interval(interval: str) -> str:
    raw = (interval or "day").strip()
    if raw in ("week", "month"):
        return "day"
    return raw if raw else "day"


def _get_entry(cache_day: str, cache_key: str) -> dict[str, Any] | None:
    global _MEMORY_DAY
    _ensure_memory_loaded(cache_day)
    if _MEMORY_DAY != cache_day:
        return None
    entry = _MEMORY.get(cache_key)
    return entry if isinstance(entry, dict) else None


def _store_entry(
    cache_day: str,
    cache_key: str,
    days_fetched: int,
    candles: list[dict[str, Any]],
) -> None:
    global _MEMORY_DAY
    _ensure_memory_loaded(cache_day)
    _MEMORY_DAY = cache_day
    _MEMORY[cache_key] = {
        "days_fetched": int(days_fetched),
        "candles": list(candles or []),
    }
    _persist(cache_day)


def _ensure_memory_loaded(cache_day: str) -> None:
    global _MEMORY, _MEMORY_DAY
    if _MEMORY_DAY == cache_day and _MEMORY:
        return
    _MEMORY = {}
    _MEMORY_DAY = ""
    if not _CACHE_FILE.exists():
        return
    try:
        payload = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("stock_history_cache: could not read %s: %s", _CACHE_FILE, exc)
        return
    if not isinstance(payload, dict):
        return
    if payload.get("cache_day") != cache_day:
        return
    entries = payload.get("entries")
    if isinstance(entries, dict):
        _MEMORY = {
            str(k): v
            for k, v in entries.items()
            if isinstance(v, dict)
        }
        _MEMORY_DAY = cache_day


def _persist(cache_day: str) -> None:
    payload = {
        "cache_day": cache_day,
        "entries": _MEMORY,
    }
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        tmp.replace(_CACHE_FILE)
    except OSError as exc:
        logger.warning("stock_history_cache: could not write %s: %s", _CACHE_FILE, exc)


def _slice_candles_by_days(
    candles: list[dict[str, Any]],
    days: int,
    interval: str,
) -> list[dict[str, Any]]:
    if not candles or days <= 0:
        return []
    cutoff = datetime.now(_IST) - timedelta(days=days)
    cutoff_key = cutoff.strftime("%Y-%m-%d")
    is_intraday = interval != "day"
    out: list[dict[str, Any]] = []
    for row in candles:
        date_val = str(row.get("date") or "")
        if not date_val:
            continue
        key = date_val[:19] if is_intraday else date_val[:10]
        if key >= cutoff_key:
            out.append(row)
    return out if out else list(candles)


def _merge_candle_lists(
    existing: list[dict[str, Any]],
    newer: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in existing or []:
        key = str(row.get("date") or "")
        if key:
            merged[key] = row
    for row in newer or []:
        key = str(row.get("date") or "")
        if key:
            merged[key] = row
    return sorted(merged.values(), key=lambda r: str(r.get("date") or ""))
