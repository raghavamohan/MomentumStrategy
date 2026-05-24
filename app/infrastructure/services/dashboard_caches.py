"""In-process day caches for dashboard quotes, profile, and mutual fund payloads."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any

from kiteconnect.exceptions import PermissionException, TokenException

from app.infrastructure.live_prices import notify_dashboard_cache_refresh
from app.domain.portfolio_model import (
    build_mf_holding,
    build_mf_underlying_breakdown,
    current_effective_day_ist,
    start_background_refresh_job,
    summarise,
)

logger = logging.getLogger(__name__)

_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE_TTL_SECONDS = 600.0
_PROFILE_CACHE_VALUE: dict[str, Any] = {}
_PROFILE_CACHE_EXPIRES_AT = 0.0

_PORTFOLIO_CACHE_LOCK = threading.Lock()
_PORTFOLIO_CACHE_TTL_SECONDS = 15.0
_PORTFOLIO_CACHE_EQUITY_HOLDINGS: list[dict[str, Any]] | None = None
_PORTFOLIO_CACHE_EQUITY_HOLDINGS_EXPIRES_AT = 0.0
_PORTFOLIO_CACHE_POSITIONS: dict[str, Any] | None = None
_PORTFOLIO_CACHE_POSITIONS_EXPIRES_AT = 0.0
_PORTFOLIO_CACHE_MARGINS: dict[str, Any] | None = None
_PORTFOLIO_CACHE_MARGINS_EXPIRES_AT = 0.0
_QUOTE_CACHE_LOCK = threading.Lock()
_QUOTE_CACHE_DAY = ""
_QUOTE_CACHE: dict[str, dict[str, Any]] = {}
_QUOTE_CACHE_FETCHED_AT: dict[str, float] = {}
_QUOTE_CACHE_MISS_TTL_SECONDS = 45.0
_MF_CACHE_LOCK = threading.Lock()
_MF_HOLDINGS_CACHE_DAY = ""
_MF_HOLDINGS_CACHE_PAYLOAD: dict[str, Any] | None = None
_MF_HOLDINGS_REFRESH_IN_PROGRESS = False
_MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = 0.0
_MF_UNDERLYINGS_CACHE_DAY_BY_TONE: dict[str, str] = {}
_MF_UNDERLYINGS_CACHE_PAYLOADS: dict[str, dict[str, Any]] = {}
_MF_UNDERLYINGS_REFRESH_IN_PROGRESS: set[str] = set()
_MF_UNDERLYING_TONE_KEYS: tuple[str, ...] = ("all", "gainers", "losers")
_MF_UNDERLYINGS_BUILD_TIMEOUT_SECONDS = 120.0
_MF_UNDERLYINGS_HOLDINGS_WAIT_MAX = 40
_MF_UNDERLYINGS_HOLDINGS_WAIT: dict[str, int] = {}
_MF_HOLDINGS_REFRESH_TIMEOUT_SECONDS = 25.0
_MF_HOLDINGS_STUCK_RESET_SECONDS = max(45.0, _MF_HOLDINGS_REFRESH_TIMEOUT_SECONDS + 15.0)

MF_PERMISSION_ERROR = (
    "Mutual Funds API is not enabled on this Kite Connect app. "
    "Enable the MF module at https://developers.kite.trade if you "
    "want this section."
)


def today_cache_token() -> str:
    """Return 09:00-IST cache-day token for all model caches."""
    return current_effective_day_ist(cutoff_hour=9)


def _quote_cache_entry_usable(
    entry: dict[str, Any] | None,
    *,
    fetched_at: float,
    now: float,
) -> bool:
    """True when a cached quote can be reused without immediate refetch.

    For sparse/missing quote payloads, retain a short miss-TTL so repeated
    dashboard hits do not hammer Kite quote() for the same unresolved keys.
    """
    if not entry:
        return (now - fetched_at) < _QUOTE_CACHE_MISS_TTL_SECONDS
    raw_ltp = entry.get("last_price")
    if raw_ltp is not None:
        try:
            if float(raw_ltp) > 0:
                return True
        except (TypeError, ValueError):
            pass
    ohlc = entry.get("ohlc") or {}
    raw_prev = ohlc.get("close")
    if raw_prev is not None:
        try:
            if float(raw_prev) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return (now - fetched_at) < _QUOTE_CACHE_MISS_TTL_SECONDS


def get_cached_quotes(kite, quote_keys: list[str]) -> dict[str, Any]:
    """Return quote payload from in-memory day cache, fetching only misses."""
    global _QUOTE_CACHE_DAY
    if not quote_keys:
        return {}
    day = today_cache_token()
    now = time.time()
    with _QUOTE_CACHE_LOCK:
        if _QUOTE_CACHE_DAY != day:
            _QUOTE_CACHE_DAY = day
            _QUOTE_CACHE.clear()
            _QUOTE_CACHE_FETCHED_AT.clear()
        cached = {
            k: _QUOTE_CACHE[k]
            for k in quote_keys
            if k in _QUOTE_CACHE
            and _quote_cache_entry_usable(
                _QUOTE_CACHE.get(k),
                fetched_at=float(_QUOTE_CACHE_FETCHED_AT.get(k) or 0.0),
                now=now,
            )
        }
    missing = [k for k in quote_keys if k not in cached]
    if not missing:
        return cached
    try:
        fetched = kite.quote(missing) or {}
    except Exception:
        fetched = {}
    with _QUOTE_CACHE_LOCK:
        if _QUOTE_CACHE_DAY != day:
            _QUOTE_CACHE_DAY = day
            _QUOTE_CACHE.clear()
            _QUOTE_CACHE_FETCHED_AT.clear()
        for k in missing:
            _QUOTE_CACHE[k] = fetched.get(k) or {}
            _QUOTE_CACHE_FETCHED_AT[k] = now
            cached[k] = _QUOTE_CACHE[k]
    return cached


def get_cached_profile(kite) -> dict[str, Any]:
    """Return profile payload cached briefly to avoid per-refresh profile calls."""
    global _PROFILE_CACHE_EXPIRES_AT
    now = time.time()
    with _PROFILE_CACHE_LOCK:
        if now < _PROFILE_CACHE_EXPIRES_AT and _PROFILE_CACHE_VALUE:
            return dict(_PROFILE_CACHE_VALUE)
    try:
        profile = kite.profile() or {}
    except Exception:
        profile = {}
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE_VALUE.clear()
        _PROFILE_CACHE_VALUE.update(profile)
        _PROFILE_CACHE_EXPIRES_AT = now + _PROFILE_CACHE_TTL_SECONDS
        return dict(_PROFILE_CACHE_VALUE)


def get_cached_equity_holdings(kite) -> list[dict[str, Any]]:
    """Return equity holdings cached for a short TTL to speed up dashboard nav."""
    global _PORTFOLIO_CACHE_EQUITY_HOLDINGS_EXPIRES_AT, _PORTFOLIO_CACHE_EQUITY_HOLDINGS
    now = time.time()
    with _PORTFOLIO_CACHE_LOCK:
        if now < _PORTFOLIO_CACHE_EQUITY_HOLDINGS_EXPIRES_AT and _PORTFOLIO_CACHE_EQUITY_HOLDINGS is not None:
            return list(_PORTFOLIO_CACHE_EQUITY_HOLDINGS)
    try:
        val = kite.holdings() or []
    except Exception as exc:
        if isinstance(exc, (PermissionException, TokenException)):
            raise
        val = []
    with _PORTFOLIO_CACHE_LOCK:
        _PORTFOLIO_CACHE_EQUITY_HOLDINGS = list(val)
        _PORTFOLIO_CACHE_EQUITY_HOLDINGS_EXPIRES_AT = now + _PORTFOLIO_CACHE_TTL_SECONDS
        return list(_PORTFOLIO_CACHE_EQUITY_HOLDINGS)


def get_cached_positions(kite) -> dict[str, Any]:
    """Return positions cached for a short TTL to speed up dashboard nav."""
    global _PORTFOLIO_CACHE_POSITIONS_EXPIRES_AT, _PORTFOLIO_CACHE_POSITIONS
    now = time.time()
    with _PORTFOLIO_CACHE_LOCK:
        if now < _PORTFOLIO_CACHE_POSITIONS_EXPIRES_AT and _PORTFOLIO_CACHE_POSITIONS is not None:
            return dict(_PORTFOLIO_CACHE_POSITIONS)
    try:
        val = kite.positions() or {}
    except Exception as exc:
        if isinstance(exc, (PermissionException, TokenException)):
            raise
        val = {}
    with _PORTFOLIO_CACHE_LOCK:
        _PORTFOLIO_CACHE_POSITIONS = dict(val)
        _PORTFOLIO_CACHE_POSITIONS_EXPIRES_AT = now + _PORTFOLIO_CACHE_TTL_SECONDS
        return dict(_PORTFOLIO_CACHE_POSITIONS)


def get_cached_margins(kite, segment: str = "equity") -> dict[str, Any]:
    """Return margins cached for a short TTL to speed up dashboard nav."""
    global _PORTFOLIO_CACHE_MARGINS_EXPIRES_AT, _PORTFOLIO_CACHE_MARGINS
    now = time.time()
    with _PORTFOLIO_CACHE_LOCK:
        if now < _PORTFOLIO_CACHE_MARGINS_EXPIRES_AT and _PORTFOLIO_CACHE_MARGINS is not None:
            return dict(_PORTFOLIO_CACHE_MARGINS)
    try:
        val = kite.margins(segment) or {}
    except Exception as exc:
        if isinstance(exc, (PermissionException, TokenException)):
            raise
        val = {}
    with _PORTFOLIO_CACHE_LOCK:
        _PORTFOLIO_CACHE_MARGINS = dict(val)
        _PORTFOLIO_CACHE_MARGINS_EXPIRES_AT = now + _PORTFOLIO_CACHE_TTL_SECONDS
        return dict(_PORTFOLIO_CACHE_MARGINS)


def _decorate_mf_row(h: dict) -> dict:
    return build_mf_holding(h).to_dict()


def _mf_holdings_loading_payload(*, stale_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(stale_payload or {})
    payload.setdefault("rows", [])
    payload.setdefault("totals", {"invested": 0.0, "current": 0.0, "pnl": 0.0})
    payload.setdefault("count", len(payload.get("rows") or []))
    payload.setdefault("error", "")
    payload["loading"] = True
    payload["stale"] = stale_payload is not None
    return payload


def _fetch_mf_holdings_with_timeout(kite, timeout_seconds: float) -> list[dict[str, Any]]:
    """Fetch MF holdings with an upper bound to avoid indefinite loading state."""
    timeout = max(1.0, float(timeout_seconds))
    done = threading.Event()
    result_holder: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result_holder["result"] = kite.mf_holdings()
        except Exception as exc:  # pragma: no cover - passthrough from network client
            result_holder["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    if not done.wait(timeout):
        raise FuturesTimeoutError()
    error = result_holder.get("error")
    if error is not None:
        raise error
    return list(result_holder.get("result") or [])


def _reset_stuck_mf_holdings_refresh_unlocked(now_monotonic: float) -> bool:
    """Drop stale in-progress flag when refresh exceeded watchdog budget."""
    global _MF_HOLDINGS_REFRESH_IN_PROGRESS, _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC
    if not _MF_HOLDINGS_REFRESH_IN_PROGRESS:
        return False
    started = float(_MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC or 0.0)
    if started <= 0.0:
        return False
    elapsed = now_monotonic - started
    if elapsed < _MF_HOLDINGS_STUCK_RESET_SECONDS:
        return False
    _MF_HOLDINGS_REFRESH_IN_PROGRESS = False
    _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = 0.0
    logger.warning(
        "Reset stuck MF holdings refresh after %.1fs; scheduling a fresh attempt.",
        elapsed,
    )
    return True


def _start_mf_holdings_refresh(kite) -> None:
    global _MF_HOLDINGS_REFRESH_IN_PROGRESS, _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC
    with _MF_CACHE_LOCK:
        if _MF_HOLDINGS_REFRESH_IN_PROGRESS:
            return
        _MF_HOLDINGS_REFRESH_IN_PROGRESS = True
        _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = time.monotonic()

    def _job() -> None:
        global _MF_HOLDINGS_CACHE_PAYLOAD, _MF_HOLDINGS_CACHE_DAY, _MF_HOLDINGS_REFRESH_IN_PROGRESS, _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC
        try:
            day = today_cache_token()
            with _MF_CACHE_LOCK:
                previous_payload = (
                    dict(_MF_HOLDINGS_CACHE_PAYLOAD)
                    if _MF_HOLDINGS_CACHE_PAYLOAD is not None
                    else None
                )
            try:
                mf_raw = _fetch_mf_holdings_with_timeout(
                    kite,
                    timeout_seconds=_MF_HOLDINGS_REFRESH_TIMEOUT_SECONDS,
                )
            except PermissionException:
                payload = {
                    "rows": [],
                    "totals": {"invested": 0.0, "current": 0.0, "pnl": 0.0},
                    "count": 0,
                    "error": MF_PERMISSION_ERROR,
                }
            except TokenException:
                logger.warning("MF holdings refresh skipped: Kite token expired.")
                payload = {
                    "rows": [],
                    "totals": {"invested": 0.0, "current": 0.0, "pnl": 0.0},
                    "count": 0,
                    "error": "Kite session expired. Please log in again.",
                }
            except FuturesTimeoutError:
                logger.warning("MF holdings refresh timed out after %.1fs.", _MF_HOLDINGS_REFRESH_TIMEOUT_SECONDS)
                payload = {
                    "rows": [],
                    "totals": {"invested": 0.0, "current": 0.0, "pnl": 0.0},
                    "count": 0,
                    "error": "Timed out while loading mutual fund holdings. Please retry.",
                }
            except Exception:
                logger.warning("MF holdings refresh failed.", exc_info=True)
                payload = {
                    "rows": [],
                    "totals": {"invested": 0.0, "current": 0.0, "pnl": 0.0},
                    "count": 0,
                    "error": "Unable to load mutual fund holdings right now.",
                }
            else:
                mf_holdings = sorted((_decorate_mf_row(h) for h in mf_raw), key=lambda r: r["fund"])
                mf_totals = summarise(mf_holdings, "invested", "current", "pnl")
                payload = {
                    "rows": mf_holdings,
                    "totals": mf_totals,
                    "count": len(mf_holdings),
                    "error": "",
                }
            payload["loading"] = False
            payload["stale"] = False
            if (
                previous_payload
                and previous_payload.get("rows")
                and payload.get("error")
            ):
                payload = dict(previous_payload)
                payload["loading"] = False
                payload["stale"] = True
            with _MF_CACHE_LOCK:
                _MF_HOLDINGS_CACHE_PAYLOAD = dict(payload)
                _MF_HOLDINGS_CACHE_DAY = day
                _MF_UNDERLYINGS_CACHE_DAY_BY_TONE.clear()
            for tone_key in _MF_UNDERLYING_TONE_KEYS:
                _start_mf_underlyings_refresh(kite, tone_key=tone_key)
            notify_dashboard_cache_refresh()
        finally:
            with _MF_CACHE_LOCK:
                _MF_HOLDINGS_REFRESH_IN_PROGRESS = False
                _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = 0.0

    if not start_background_refresh_job("mf-holdings", _job):
        with _MF_CACHE_LOCK:
            _MF_HOLDINGS_REFRESH_IN_PROGRESS = False
            _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = 0.0
        logger.warning("MF holdings refresh already running; using existing background task.")


def get_cached_mf_holdings_payload(kite) -> dict[str, Any]:
    """Return MF holdings payload while refreshing stale/missing cache in background."""
    day = today_cache_token()
    with _MF_CACHE_LOCK:
        _reset_stuck_mf_holdings_refresh_unlocked(time.monotonic())
        cached = dict(_MF_HOLDINGS_CACHE_PAYLOAD) if _MF_HOLDINGS_CACHE_PAYLOAD is not None else None
        cached_day = _MF_HOLDINGS_CACHE_DAY
        refreshing = _MF_HOLDINGS_REFRESH_IN_PROGRESS

    if cached is not None and cached_day == day:
        cached["loading"] = False
        cached["stale"] = False
        return cached

    if cached is not None and cached_day != day:
        if not refreshing:
            _start_mf_holdings_refresh(kite)
        return _mf_holdings_loading_payload(stale_payload=cached)

    if not refreshing:
        _start_mf_holdings_refresh(kite)
    return _mf_holdings_loading_payload()


def _normalize_mf_underlying_tone(tone: str) -> str:
    value = str(tone or "").strip().lower()
    if value in {"gainers", "losers"}:
        return value
    return "all"


def _filter_mf_holdings_by_tone(
    rows: list[dict[str, Any]], tone: str
) -> list[dict[str, Any]]:
    if tone == "all":
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        pnl = float(row.get("pnl") or 0.0)
        if tone == "gainers" and pnl > 0.0:
            out.append(row)
        elif tone == "losers" and pnl < 0.0:
            out.append(row)
    return out


def _mf_underlyings_loading_payload(
    tone_key: str,
    *,
    stale_payload: dict[str, Any] | None = None,
    holdings_error: str = "",
) -> dict[str, Any]:
    payload = dict(stale_payload or {})
    payload.setdefault("rows", [])
    payload.setdefault("month", "")
    payload.setdefault("notAggregatedFunds", [])
    payload.setdefault("aggregatedFundCount", 0)
    payload.setdefault("totalFundCount", 0)
    payload["tone"] = tone_key
    payload["error"] = str(payload.get("error") or holdings_error or "")
    payload["loading"] = True
    payload["stale"] = stale_payload is not None
    return payload


def _start_mf_underlyings_refresh(kite, *, tone_key: str) -> None:
    with _MF_CACHE_LOCK:
        if tone_key in _MF_UNDERLYINGS_REFRESH_IN_PROGRESS:
            return
        _MF_UNDERLYINGS_REFRESH_IN_PROGRESS.add(tone_key)

    def _job() -> None:
        payload_to_store: dict[str, Any] | None = None
        try:
            holdings_payload = get_cached_mf_holdings_payload(kite)
            if holdings_payload.get("loading"):
                with _MF_CACHE_LOCK:
                    wait_n = _MF_UNDERLYINGS_HOLDINGS_WAIT.get(tone_key, 0) + 1
                    _MF_UNDERLYINGS_HOLDINGS_WAIT[tone_key] = wait_n
                if wait_n > _MF_UNDERLYINGS_HOLDINGS_WAIT_MAX:
                    payload_to_store = {
                        "rows": [],
                        "month": "",
                        "notAggregatedFunds": [],
                        "aggregatedFundCount": 0,
                        "totalFundCount": 0,
                        "tone": tone_key,
                        "error": (
                            "Mutual fund holdings are still loading. "
                            "Refresh the page or wait until MF holdings finish building."
                        ),
                        "loading": False,
                        "stale": False,
                    }
                else:
                    timer = threading.Timer(
                        1.5,
                        lambda k=kite, t=tone_key: _start_mf_underlyings_refresh(k, tone_key=t),
                    )
                    timer.daemon = True
                    timer.start()
                    return
            else:
                with _MF_CACHE_LOCK:
                    _MF_UNDERLYINGS_HOLDINGS_WAIT.pop(tone_key, None)

            if payload_to_store is None:
                if holdings_payload.get("error"):
                    payload_to_store = {
                        "rows": [],
                        "month": "",
                        "notAggregatedFunds": [],
                        "aggregatedFundCount": 0,
                        "totalFundCount": 0,
                        "tone": tone_key,
                        "error": str(holdings_payload.get("error") or ""),
                        "loading": False,
                        "stale": False,
                    }
                else:
                    mf_holdings = list(holdings_payload.get("rows") or [])
                    mf_holdings = _filter_mf_holdings_by_tone(mf_holdings, tone_key)
                    pool = ThreadPoolExecutor(max_workers=1)
                    try:
                        fut = pool.submit(build_mf_underlying_breakdown, mf_holdings)
                        rows, month, missing_funds, aggregated_count, total_count = fut.result(
                            timeout=_MF_UNDERLYINGS_BUILD_TIMEOUT_SECONDS
                        )
                    except FuturesTimeoutError:
                        logger.warning(
                            "MF underlyings build timed out after %.1fs (tone=%s).",
                            _MF_UNDERLYINGS_BUILD_TIMEOUT_SECONDS,
                            tone_key,
                        )
                        payload_to_store = {
                            "rows": [],
                            "month": "",
                            "notAggregatedFunds": [],
                            "aggregatedFundCount": 0,
                            "totalFundCount": 0,
                            "tone": tone_key,
                            "error": (
                                "Timed out while loading MF underlying data from mfdata.in. "
                                "Try again later or refresh the dashboard."
                            ),
                            "loading": False,
                            "stale": False,
                        }
                    except Exception:
                        logger.exception("MF underlyings build failed (tone=%s).", tone_key)
                        payload_to_store = {
                            "rows": [],
                            "month": "",
                            "notAggregatedFunds": [],
                            "aggregatedFundCount": 0,
                            "totalFundCount": 0,
                            "tone": tone_key,
                            "error": "Unable to aggregate MF underlyings right now.",
                            "loading": False,
                            "stale": False,
                        }
                    else:
                        payload_to_store = {
                            "rows": rows,
                            "month": month,
                            "notAggregatedFunds": missing_funds,
                            "aggregatedFundCount": aggregated_count,
                            "totalFundCount": total_count,
                            "tone": tone_key,
                            "error": "",
                            "loading": False,
                            "stale": False,
                        }
                    finally:
                        pool.shutdown(wait=False)

            if payload_to_store is not None:
                with _MF_CACHE_LOCK:
                    _MF_UNDERLYINGS_CACHE_PAYLOADS[tone_key] = dict(payload_to_store)
                    _MF_UNDERLYINGS_CACHE_DAY_BY_TONE[tone_key] = today_cache_token()
                    _MF_UNDERLYINGS_HOLDINGS_WAIT.pop(tone_key, None)
                notify_dashboard_cache_refresh()
        except Exception:
            logger.exception("MF underlyings refresh failed unexpectedly (tone=%s).", tone_key)
            payload_to_store = {
                "rows": [],
                "month": "",
                "notAggregatedFunds": [],
                "aggregatedFundCount": 0,
                "totalFundCount": 0,
                "tone": tone_key,
                "error": "Unable to refresh MF underlying breakdown.",
                "loading": False,
                "stale": False,
            }
            with _MF_CACHE_LOCK:
                _MF_UNDERLYINGS_CACHE_PAYLOADS[tone_key] = dict(payload_to_store)
                _MF_UNDERLYINGS_CACHE_DAY_BY_TONE[tone_key] = today_cache_token()
                _MF_UNDERLYINGS_HOLDINGS_WAIT.pop(tone_key, None)
            notify_dashboard_cache_refresh()
        finally:
            with _MF_CACHE_LOCK:
                _MF_UNDERLYINGS_REFRESH_IN_PROGRESS.discard(tone_key)

    if not start_background_refresh_job(f"mf-underlyings-{tone_key}", _job):
        with _MF_CACHE_LOCK:
            _MF_UNDERLYINGS_REFRESH_IN_PROGRESS.discard(tone_key)


def get_cached_mf_underlyings_payload(kite, *, tone: str = "all") -> dict[str, Any]:
    """Return MF underlyings payload while refreshing stale/missing cache in background."""
    tone_key = _normalize_mf_underlying_tone(tone)
    day = today_cache_token()
    with _MF_CACHE_LOCK:
        cached = dict(_MF_UNDERLYINGS_CACHE_PAYLOADS.get(tone_key) or {})
        cached_day = _MF_UNDERLYINGS_CACHE_DAY_BY_TONE.get(tone_key, "")
        refreshing = tone_key in _MF_UNDERLYINGS_REFRESH_IN_PROGRESS

    if cached and cached_day == day:
        cached["loading"] = False
        cached["stale"] = False
        return cached

    holdings_payload = get_cached_mf_holdings_payload(kite)
    holdings_error = str(holdings_payload.get("error") or "")
    if not refreshing:
        _start_mf_underlyings_refresh(kite, tone_key=tone_key)

    if cached and cached_day != day:
        return _mf_underlyings_loading_payload(tone_key, stale_payload=cached, holdings_error=holdings_error)
    return _mf_underlyings_loading_payload(tone_key, holdings_error=holdings_error)
