"""MarketSmith India — HTTPS access and on-disk cache for market regime snapshot."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

from app.domain.reference_notifications import notify_reference_cache_refresh
from app.infrastructure.cache.model_cache_store import (
    BaseCache,
    current_effective_day_ist,
    next_cutoff_epoch_ist,
    start_background_refresh_job,
)
from app.domain.reference_context import WarmupContext

logger = logging.getLogger(__name__)

_MARKETSMITH_TOOL_URL = "https://marketsmithindia.com/mstool/marketconditionhistory.jsp"
_MARKETSMITH_HISTORY_URL = "https://marketsmithindia.com/gateway/simple-api/ms-india/mshkSubscription/getMarketHistory.json"
_MARKETSMITH_DEFAULT_MS_AUTH = "0000+MarketSmithINDUID-0000000000000+MarketSmithINDUID-0000000000000"
_MARKETSMITH_HTTP_TIMEOUT_SECONDS = 12

# --- Provider State ---
_MARKETSMITH_CACHE = BaseCache("marketsmith_provider")
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_REFRESH_IN_PROGRESS = False

_CACHE_DAY = ""
_SOURCE_LABEL = "unknown"
_EXPIRES_AT = 0.0

_MARKET_CONDITION: dict[str, Any] | None = None


def _marketsmith_ms_auth() -> str:
    raw = os.environ.get("MARKETSMITH_MS_AUTH", "").strip()
    return raw if raw else _MARKETSMITH_DEFAULT_MS_AUTH


def _load_cache_unlocked() -> None:
    global _CACHE_LOADED, _CACHE_DAY, _SOURCE_LABEL, _EXPIRES_AT, _MARKET_CONDITION
    if _CACHE_LOADED: return

    payload = _MARKETSMITH_CACHE.read_section("marketsmith")
    meta = payload.get("meta") or {}
    _CACHE_DAY = str(meta.get("cached_day") or "").strip()
    _MARKET_CONDITION = payload.get("model")

    if _MARKET_CONDITION:
        _SOURCE_LABEL = "disk"

    _EXPIRES_AT = next_cutoff_epoch_ist(9)
    _CACHE_LOADED = True


def _persist_cache_unlocked() -> None:
    _MARKETSMITH_CACHE.update_section("marketsmith", lambda _: {
        "meta": {"cached_day": _CACHE_DAY},
        "model": _MARKET_CONDITION
    })


def _maybe_start_refresh_unlocked() -> None:
    global _REFRESH_IN_PROGRESS, _SOURCE_LABEL
    if _REFRESH_IN_PROGRESS: return
    cur = current_effective_day_ist(9)
    if _CACHE_DAY == cur and _MARKET_CONDITION: return

    _SOURCE_LABEL = "disk_stale_bg_refresh" if _MARKET_CONDITION else "cold_start_bg_refresh"
    _REFRESH_IN_PROGRESS = True

    def _job() -> None:
        global _REFRESH_IN_PROGRESS, _CACHE_DAY, _SOURCE_LABEL, _MARKET_CONDITION
        ok = False
        try:
            base = _fetch_from_network()
            if base.get("available"):
                with _CACHE_LOCK:
                    _MARKET_CONDITION = _attach_meta(base, current_effective_day_ist(9))
                    _CACHE_DAY = current_effective_day_ist(9)
                    _SOURCE_LABEL = "network_bg_refresh"
                    _persist_cache_unlocked()
                    ok = True
        finally:
            with _CACHE_LOCK:
                _REFRESH_IN_PROGRESS = False
                if not ok: _SOURCE_LABEL = "network_bg_refresh_failed"
            if ok: notify_reference_cache_refresh()

    start_background_refresh_job("marketsmith-daily", _job)


def _fetch_from_network() -> dict[str, Any]:
    qs = urlencode({"ms-auth": _marketsmith_ms_auth()})
    req = URLRequest(f"{_MARKETSMITH_HISTORY_URL}?{qs}", headers={
        "User-Agent": "MomentumStrategyDashboard/1.0",
        "Accept": "application/json",
        "Referer": _MARKETSMITH_TOOL_URL,
    })
    try:
        with urlopen(req, timeout=_MARKETSMITH_HTTP_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            wrapped = payload.get("response") or {}
            hist = wrapped.get("marketHistory")
            if isinstance(hist, list) and hist:
                row = hist[0]
                nifty_raw = row.get("nifty50Perc")
                try: nifty_pct = float(nifty_raw)
                except: nifty_pct = None

                return {
                    "available": True,
                    "tone": _tone_from_code(row.get("marketConditionCode")),
                    "headline": str(row.get("marketConditionDesc") or "Unknown"),
                    "code": str(row.get("marketConditionCode") or ""),
                    "nifty50_pct": nifty_pct,
                    "nifty50_display": _fmt_signed_pct(nifty_pct),
                    "regime_since_display": _fmt_iso_to_pretty(row.get("startDate")),
                    "modification_display": _fmt_iso_to_pretty(row.get("modificationDate")),
                    "source_url": _MARKETSMITH_TOOL_URL,
                    "error": None,
                }
    except Exception as exc:
        logger.warning("MarketSmith fetch failed: %s", exc)

    return {"available": False, "error": "Fetch failed", "tone": "unknown"}


def _attach_meta(model: dict[str, Any], day: str) -> dict[str, Any]:
    return {**model, "data_source": "MarketSmith India", "cached_day": day}


def _tone_from_code(code: str) -> str:
    c = str(code or "").strip().upper()
    return {"R": "uptrend", "C": "downtrend", "U": "caution", "A": "caution"}.get(c, "unknown")


def _fmt_signed_pct(v: float | None) -> str | None:
    if v is None: return None
    return f"{'+' if v > 0 else ''}{v:.2f}%"


def _fmt_iso_to_pretty(raw: str | None) -> str | None:
    if not raw: return None
    try:
        s = str(raw).strip().split()[0].split("T")[0]
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime("%d-%b-%Y")
    except: return str(raw)


def get_marketsmith_market_condition(*, force_sync_fetch: bool = False) -> dict[str, Any]:
    global _MARKET_CONDITION, _CACHE_DAY
    with _CACHE_LOCK:
        _load_cache_unlocked()
        if force_sync_fetch:
            res = _fetch_from_network()
            if res.get("available"):
                _MARKET_CONDITION = _attach_meta(res, current_effective_day_ist(9))
                _CACHE_DAY = current_effective_day_ist(9)
                _persist_cache_unlocked()
            return dict(_MARKET_CONDITION or {})

        _maybe_start_refresh_unlocked()
        if _MARKET_CONDITION:
            return dict(_MARKET_CONDITION)

        return _attach_meta({"available": False, "headline": "Refreshing...", "tone": "unknown"}, current_effective_day_ist(9))


def warmup(ctx: WarmupContext) -> None:
    get_marketsmith_market_condition(force_sync_fetch=ctx.marketsmith_force_sync)


def marketsmith_reference_debug_snapshot(now: float) -> dict[str, Any]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        return {
            "source": _SOURCE_LABEL,
            "expires_in_ms": max(0.0, (_EXPIRES_AT - now) * 1000.0),
            "refresh_in_progress": _REFRESH_IN_PROGRESS,
            "cache_day": _CACHE_DAY,
        }


def marketsmith_market_condition_bootstrap(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": bool(model.get("available")),
        "tone": str(model.get("tone") or "unknown"),
        "headline": str(model.get("headline") or ""),
        "code": str(model.get("code") or ""),
        "nifty50Pct": model.get("nifty50_pct"),
        "nifty50Display": model.get("nifty50_display"),
        "regimeSinceDisplay": model.get("regime_since_display"),
        "modificationDisplay": model.get("modification_display"),
        "sourceUrl": str(model.get("source_url") or _MARKETSMITH_TOOL_URL),
        "error": model.get("error"),
        "dataSource": str(model.get("data_source") or "MarketSmith India"),
        "cachedDay": model.get("cached_day"),
    }


__all__ = [
    "get_marketsmith_market_condition",
    "marketsmith_market_condition_bootstrap",
    "marketsmith_reference_debug_snapshot",
    "warmup",
]
