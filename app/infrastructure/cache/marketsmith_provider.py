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
    current_effective_day_ist,
    next_cutoff_epoch_ist,
    read_section,
    start_background_refresh_job,
    update_section,
)
from app.domain.reference_context import WarmupContext

logger = logging.getLogger(__name__)

_MARKETSMITH_TOOL_URL = "https://marketsmithindia.com/mstool/marketconditionhistory.jsp"
_MARKETSMITH_HISTORY_URL = (
    "https://marketsmithindia.com/gateway/simple-api/ms-india/"
    "mshkSubscription/getMarketHistory.json"
)
_MARKETSMITH_DEFAULT_MS_AUTH = (
    "0000+MarketSmithINDUID-0000000000000+MarketSmithINDUID-0000000000000"
)
_MARKETSMITH_HTTP_TIMEOUT_SECONDS = 12

_MARKET_CONDITION_LOCK = threading.Lock()
_MARKET_CONDITION_MEMORY_DAY: str = ""
_MARKET_CONDITION_MEMORY: dict[str, Any] | None = None

_MARKETSMITH_FETCH_LOCK = threading.Lock()
_MARKETSMITH_NETWORK_FETCH_IN_PROGRESS = False

_MARKETSMITH_ISO_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MARKETSMITH_ISO_DT_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[\sT].*)?$")

_MARKETSMITH_MONTH_LABELS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _marketsmith_calendar_day_token() -> str:
    """IST business day key, rolling over at 09:00."""
    return current_effective_day_ist(cutoff_hour=9)


def _marketsmith_ms_auth() -> str:
    raw = os.environ.get("MARKETSMITH_MS_AUTH", "").strip()
    return raw if raw else _MARKETSMITH_DEFAULT_MS_AUTH


def _marketsmith_tone_from_code(code: str) -> str:
    c = (code or "").strip().upper()
    if c == "R":
        return "uptrend"
    if c == "C":
        return "downtrend"
    if c in ("U", "A"):
        return "caution"
    return "unknown"


def _marketsmith_fmt_dd_mmm_yyyy(iso_day: str | None) -> str | None:
    """Format ``YYYY-MM-DD`` (or ISO date prefix) as ``dd-Mmm-yyyy``."""
    if not iso_day:
        return None
    day_part = iso_day.strip().split()[0].split("T")[0]
    parts = day_part.split("-")
    if len(parts) != 3:
        return None
    y_s, mo_s, d_s = parts[0], parts[1], parts[2]
    try:
        _ = datetime(int(y_s, 10), int(mo_s, 10), int(d_s, 10))
        dom = int(d_s, 10)
        mi = int(mo_s, 10) - 1
        year = int(y_s, 10)
        if not (0 <= mi < 12):
            return None
    except ValueError:
        return None
    return f"{dom:02d}-{_MARKETSMITH_MONTH_LABELS[mi]}-{year}"


def _marketsmith_fmt_modification_ts(raw: str | None) -> str | None:
    """Turn ``YYYY-MM-DD HH:MM:SS`` (and ``YYYY-MM-DDTHH:MM:SS``) into ``dd-Mmm-yyyy HH:MM:SS``.

    Leaves values that already begin with ``dd-Mmm-yyyy`` unchanged (no second pass).
    """
    if not raw:
        return None
    s = raw.strip().replace("T", " ", 1)
    date_token = s.split(None, 1)[0].split("T")[0]
    if not _MARKETSMITH_ISO_DATE_ONLY_RE.match(date_token):
        return s
    segs = s.split(None, 1)
    date_raw = segs[0]
    tail = segs[1].strip() if len(segs) > 1 else ""
    pretty = _marketsmith_fmt_dd_mmm_yyyy(date_raw)
    basis = pretty if pretty is not None else date_raw
    return f"{basis} {tail}".rstrip() if tail else basis


def _marketsmith_normalize_display_fields(model: dict[str, Any]) -> dict[str, Any]:
    """Ensure cached payloads get ``dd-Mmm-yyyy`` even when disk has legacy ISO strings."""
    out = dict(model)
    rs = out.get("regime_since_display")
    if isinstance(rs, str):
        rs_t = rs.strip()
        if _MARKETSMITH_ISO_DATE_ONLY_RE.match(rs_t):
            out["regime_since_display"] = _marketsmith_fmt_dd_mmm_yyyy(rs_t) or rs_t

    md = out.get("modification_display")
    if isinstance(md, str):
        md_t = md.strip().replace("T", " ", 1)
        if _MARKETSMITH_ISO_DT_START_RE.match(md_t):
            out["modification_display"] = _marketsmith_fmt_modification_ts(md_t) or md_t

    return out


def _marketsmith_finalize_model(model: dict[str, Any]) -> dict[str, Any]:
    return dict(_marketsmith_normalize_display_fields(model))


def _marketsmith_fmt_signed_pct(value: float | None) -> str | None:
    if value is None:
        return None
    v = float(value)
    mag = f"{abs(v):.2f}%"
    if v > 0:
        return f"+{mag}"
    if v < 0:
        return f"-{mag}"
    return "0.00%"


def _marketsmith_error_payload(message: str) -> dict[str, Any]:
    return {
        "available": False,
        "tone": "unknown",
        "headline": "",
        "code": "",
        "nifty50_pct": None,
        "nifty50_display": None,
        "regime_since_display": None,
        "modification_display": None,
        "source_url": _MARKETSMITH_TOOL_URL,
        "error": message,
    }


def _marketsmith_fetch_from_network() -> dict[str, Any]:
    """Parse gateway JSON into the dashboard dict shape; no ``data_source`` / ``cached_day``."""
    qs = urlencode({"ms-auth": _marketsmith_ms_auth()})
    url = f"{_MARKETSMITH_HISTORY_URL}?{qs}"
    req = URLRequest(
        url,
        headers={
            "User-Agent": "MomentumStrategyDashboard/1.0",
            "Accept": "application/json",
            "Referer": _MARKETSMITH_TOOL_URL,
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=_MARKETSMITH_HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except HTTPError as exc:
        logger.warning(
            "MarketSmith market condition HTTP error: %s %s", exc.code, exc.reason
        )
        return _marketsmith_error_payload(f"MarketSmith gateway returned HTTP {exc.code}.")
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("MarketSmith market condition fetch failed: %s", exc)
        return _marketsmith_error_payload("Could not reach MarketSmith India.")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("MarketSmith market condition JSON error: %s", exc)
        return _marketsmith_error_payload("MarketSmith response was not valid JSON.")

    wrapped = payload.get("response")
    hist: Any = wrapped.get("marketHistory") if isinstance(wrapped, dict) else None
    if not isinstance(hist, list) or not hist:
        return _marketsmith_error_payload(
            "No market regime history in MarketSmith response."
        )

    row = hist[0]
    if not isinstance(row, dict):
        return _marketsmith_error_payload("Unexpected MarketSmith payload shape.")

    code = str(row.get("marketConditionCode") or "").strip()
    headline = str(row.get("marketConditionDesc") or "").strip()
    start = row.get("startDate")
    start_s = str(start).strip() if start is not None else ""

    nifty_raw = row.get("nifty50Perc")
    try:
        nifty_pct = float(nifty_raw) if nifty_raw is not None else None
    except (TypeError, ValueError):
        nifty_pct = None

    mod = row.get("modificationDate")
    mod_s = str(mod).strip() if mod is not None else None

    return {
        "available": bool(headline),
        "tone": _marketsmith_tone_from_code(code),
        "headline": headline or "Unknown regime",
        "code": code,
        "nifty50_pct": nifty_pct,
        "nifty50_display": _marketsmith_fmt_signed_pct(nifty_pct),
        "regime_since_display": (_marketsmith_fmt_dd_mmm_yyyy(start_s) or start_s)
        if start_s
        else None,
        "modification_display": _marketsmith_fmt_modification_ts(mod_s)
        if mod_s
        else None,
        "source_url": _MARKETSMITH_TOOL_URL,
        "error": None
        if headline
        else "MarketSmith returned an empty regime label.",
    }


def _marketsmith_attach_meta(model: dict[str, Any], day: str) -> dict[str, Any]:
    out = dict(model)
    out["data_source"] = "MarketSmith India"
    out["cached_day"] = day
    return out


def _marketsmith_read_disk_for_day(day: str) -> dict[str, Any] | None:
    raw = read_section("marketsmith")
    meta = raw.get("meta") if isinstance(raw, dict) else None
    if not isinstance(meta, dict) or meta.get("cached_day") != day:
        return None
    inner = raw.get("model")
    return inner if isinstance(inner, dict) else None


def _marketsmith_write_disk(day: str, model: dict[str, Any]) -> None:
    try:
        update_section("marketsmith", lambda _: {"meta": {"cached_day": day}, "model": model})
    except Exception as exc:
        logger.warning("MarketSmith disk cache write failed: %s", exc)


def _marketsmith_read_disk_any_model() -> tuple[str, dict[str, Any] | None]:
    """Latest snapshot from disk (any ``cached_day``), for provisional UI while refreshing."""
    raw = read_section("marketsmith")
    if not isinstance(raw, dict):
        return ("", None)
    meta = raw.get("meta")
    d = str(meta.get("cached_day") or "").strip() if isinstance(meta, dict) else ""
    inner = raw.get("model")
    if isinstance(inner, dict):
        return (d, inner)
    return ("", None)


def _marketsmith_schedule_network_fetch(day: str) -> None:
    """HTTPS fetch + disk write on a daemon thread; does not block callers."""
    global _MARKETSMITH_NETWORK_FETCH_IN_PROGRESS
    with _MARKETSMITH_FETCH_LOCK:
        if _MARKETSMITH_NETWORK_FETCH_IN_PROGRESS:
            return
        _MARKETSMITH_NETWORK_FETCH_IN_PROGRESS = True

    def _job() -> None:
        global _MARKET_CONDITION_MEMORY_DAY, _MARKET_CONDITION_MEMORY
        global _MARKETSMITH_NETWORK_FETCH_IN_PROGRESS
        try:
            base = _marketsmith_fetch_from_network()
            model = _marketsmith_attach_meta(base, day)
            ready = _marketsmith_finalize_model(model)
            with _MARKET_CONDITION_LOCK:
                _MARKET_CONDITION_MEMORY_DAY = day
                _MARKET_CONDITION_MEMORY = ready
            _marketsmith_write_disk(day, ready)
        finally:
            with _MARKETSMITH_FETCH_LOCK:
                _MARKETSMITH_NETWORK_FETCH_IN_PROGRESS = False
            notify_reference_cache_refresh()

    start_background_refresh_job("marketsmith-daily", _job)


def warmup(ctx: WarmupContext) -> None:
    """Hydrate regime snapshot from disk/memory or trigger fetch per ``ctx.marketsmith_force_sync``."""
    get_marketsmith_market_condition(force_sync_fetch=ctx.marketsmith_force_sync)


def get_marketsmith_market_condition(*, force_sync_fetch: bool = False) -> dict[str, Any]:
    """Return today's MarketSmith India regime snapshot (first history row).

    Cached **once per local calendar day** (same convention as MF holdings /
    MF underlyings): serves from process memory when warm,
    else from the ``marketsmith`` section inside ``.cache/model_cache.json``,
    else a background HTTPS fetch for the day (dashboard) or a blocking fetch
    when ``force_sync_fetch`` is True (blocking refresh for callers that need disk filled immediately).
    Optional env ``MARKETSMITH_MS_AUTH`` overrides the gateway ``ms-auth`` query parameter.
    """
    global _MARKET_CONDITION_MEMORY_DAY, _MARKET_CONDITION_MEMORY
    day = _marketsmith_calendar_day_token()

    with _MARKET_CONDITION_LOCK:
        if _MARKET_CONDITION_MEMORY_DAY == day and _MARKET_CONDITION_MEMORY is not None:
            return _marketsmith_finalize_model(dict(_MARKET_CONDITION_MEMORY))

    disk = _marketsmith_read_disk_for_day(day)
    if disk is not None:
        filled = _marketsmith_attach_meta(disk, day)
        ready = _marketsmith_finalize_model(filled)
        with _MARKET_CONDITION_LOCK:
            _MARKET_CONDITION_MEMORY_DAY = day
            _MARKET_CONDITION_MEMORY = ready
        return dict(ready)

    if force_sync_fetch:
        base = _marketsmith_fetch_from_network()
        model = _marketsmith_attach_meta(base, day)
        ready = _marketsmith_finalize_model(model)
        with _MARKET_CONDITION_LOCK:
            _MARKET_CONDITION_MEMORY_DAY = day
            _MARKET_CONDITION_MEMORY = ready
        _marketsmith_write_disk(day, ready)
        return dict(ready)

    prev_day, fallback = _marketsmith_read_disk_any_model()
    _marketsmith_schedule_network_fetch(day)

    if fallback is not None:
        label = prev_day or day
        filled = _marketsmith_attach_meta(fallback, label)
        return dict(_marketsmith_finalize_model(filled))

    loading = _marketsmith_attach_meta(
        _marketsmith_error_payload("Refreshing market regime…"),
        day,
    )
    return _marketsmith_finalize_model(loading)


def marketsmith_reference_debug_snapshot(now: float) -> dict[str, Any]:
    """Metadata row for :func:`app.domain.portfolio_model.get_reference_cache_debug_snapshot`."""
    expires_ms = max(0.0, (next_cutoff_epoch_ist(9) - now) * 1000.0)
    day = _marketsmith_calendar_day_token()
    with _MARKET_CONDITION_LOCK:
        mem_day = _MARKET_CONDITION_MEMORY_DAY
        memory_ready = (
            mem_day == day
            and _MARKET_CONDITION_MEMORY is not None
        )
        mem_headline = ""
        if _MARKET_CONDITION_MEMORY is not None:
            headline = _MARKET_CONDITION_MEMORY.get("headline")
            if isinstance(headline, str):
                mem_headline = headline.strip()
            loading_placeholder = (_MARKET_CONDITION_MEMORY.get("error") or "").strip().startswith(
                "Refreshing market regime"
            )
        else:
            loading_placeholder = False
    with _MARKETSMITH_FETCH_LOCK:
        fetch_busy = _MARKETSMITH_NETWORK_FETCH_IN_PROGRESS
    disk_for_day_ok = _marketsmith_read_disk_for_day(day) is not None

    if memory_ready:
        if loading_placeholder and not mem_headline:
            source = "loading"
        else:
            source = "memory"
    elif disk_for_day_ok:
        source = "disk"
    elif fetch_busy:
        source = "network_refresh"
    elif _marketsmith_read_disk_any_model()[1] is not None:
        source = "disk_stale"
    else:
        source = "cold"

    return {
        "source": source,
        "expires_in_ms": expires_ms,
        "refresh_in_progress": fetch_busy,
        "cached_day_memory": mem_day if memory_ready else "",
    }


def marketsmith_market_condition_bootstrap(model: dict[str, Any]) -> dict[str, Any]:
    """CamelCase JSON projection for ``dashboard-bootstrap`` (client reads on load)."""
    raw_cached = model.get("cached_day")
    cached_disp = ""
    if isinstance(raw_cached, str) and raw_cached.strip():
        cached_disp = _marketsmith_fmt_dd_mmm_yyyy(raw_cached.strip()) or raw_cached.strip()

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
        "cachedDay": cached_disp,
    }


__all__ = [
    "get_marketsmith_market_condition",
    "marketsmith_market_condition_bootstrap",
    "marketsmith_reference_debug_snapshot",
    "warmup",
]
