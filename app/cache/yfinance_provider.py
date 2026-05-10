"""yfinance-backed sector/industry cache (disk section ``yfinance`` in ``model_cache.json``)."""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.reference_context import WarmupContext
from app.reference_notifications import notify_reference_cache_refresh
from app.cache.model_cache_store import (
    load_model_cache,
    read_section,
    start_background_refresh_job,
    update_section,
)
from app.cache.reference_cache_internal import _current_reference_day_token, _next_reference_cutoff_epoch

logger = logging.getLogger(__name__)

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover - yfinance is optional at runtime
    yf = None

_EQUITY_EXCHANGES = ("NSE", "BSE")

_CACHED_YFINANCE_KEY_TO_INDUSTRY: dict[tuple[str, str], str] = {}
_CACHED_YFINANCE_KEY_TO_SECTOR: dict[tuple[str, str], str] = {}

_YFINANCE_CACHE_LOCK = threading.Lock()
_YFINANCE_CACHE_LOADED = False
_YFINANCE_CACHE_REFRESH_IN_PROGRESS = False
_YFINANCE_REFRESH_THREAD_STARTED = False
_YFINANCE_SYMBOL_REFRESH_IN_PROGRESS: set[tuple[str, str]] = set()
_YFINANCE_CACHE_LAST_SOURCE = "unknown"

_YFINANCE_CACHE_DAY = ""


def _normalise_name(raw: Any) -> str:
    return str(raw or "").strip()


def _normalise_symbol(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _yfinance_cache_key(exchange: str, symbol: str) -> tuple[str, str]:
    return (exchange.upper().strip(), _normalise_symbol(symbol))


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
            all_keys = set(_CACHED_YFINANCE_KEY_TO_INDUSTRY) | set(_CACHED_YFINANCE_KEY_TO_SECTOR)
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
        global _YFINANCE_CACHE_REFRESH_IN_PROGRESS, _YFINANCE_CACHE_DAY, _YFINANCE_REFRESH_THREAD_STARTED
        global _YFINANCE_CACHE_LAST_SOURCE
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
                    continue

            _YFINANCE_CACHE_DAY = _current_reference_day_token()
            if updated:
                logger.info("yfinance cache refreshed for %d symbols", updated)
            _persist_yfinance_cache()
            _YFINANCE_CACHE_LAST_SOURCE = "network_bg_refresh"
            notify_reference_cache_refresh()
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
    if yf is None or clean_exchange not in _EQUITY_EXCHANGES or not clean_symbol:
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
            notify_reference_cache_refresh()
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


def get_yfinance_mapping_snapshot() -> tuple[str, dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """Return ``(cache_day, key_to_sector, key_to_industry)`` copies for reference snapshots."""
    _load_yfinance_cache_if_needed()
    with _YFINANCE_CACHE_LOCK:
        return (
            str(globals().get("_YFINANCE_CACHE_DAY") or "").strip(),
            dict(_CACHED_YFINANCE_KEY_TO_SECTOR),
            dict(_CACHED_YFINANCE_KEY_TO_INDUSTRY),
        )


def warmup(_ctx: WarmupContext) -> None:
    """Load disk mapping and start monthly refresh when configured."""
    _load_yfinance_cache_if_needed()
    _maybe_start_monthly_yfinance_refresh()


def lookup_yfinance_sector_labels(
    exchange: str,
    symbol: str,
) -> tuple[str, str, tuple[str, str] | None, tuple[str, str] | None]:
    """Resolve cached sector/industry for one cash equity using exchange-scoped keys and NSE/BSE fallback.

    Runs monthly staleness refresh, performs the lookup, and queues a single-symbol background
    fetch when both sector and industry are missing (same policy as
    :func:`app.portfolio_model.resolve_equity_sector`).
    """
    _maybe_start_monthly_yfinance_refresh()
    clean_exchange = str(exchange or "").strip().upper()
    clean_symbol = _normalise_symbol(symbol)
    if not clean_symbol or clean_exchange not in _EQUITY_EXCHANGES:
        return "", "", None, None

    yfinance_keys = [_yfinance_cache_key(clean_exchange, clean_symbol)]
    if clean_exchange == "NSE":
        yfinance_keys.append(_yfinance_cache_key("BSE", clean_symbol))
    elif clean_exchange == "BSE":
        yfinance_keys.append(_yfinance_cache_key("NSE", clean_symbol))

    cached_sec, cached_ind, matched_key, matched_ind_key = _read_cached_sector_industry(yfinance_keys)
    if not cached_sec and not cached_ind:
        _maybe_start_single_symbol_yfinance_refresh(clean_exchange, clean_symbol)
    return cached_sec, cached_ind, matched_key, matched_ind_key


def _read_cached_sector_industry(
    yfinance_keys: list[tuple[str, str]],
) -> tuple[str, str, tuple[str, str] | None, tuple[str, str] | None]:
    """Return cached sector, industry, and optional matched keys for logging."""
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
        return cached_sec, cached_ind, matched_key, matched_ind_key


def get_cache_debug_snapshot(now: float) -> dict[str, Any]:
    """Metadata row for :func:`app.portfolio_model.get_reference_cache_debug_snapshot`."""
    return {
        "source": _YFINANCE_CACHE_LAST_SOURCE,
        "expires_in_ms": max(0.0, (_next_reference_cutoff_epoch() - now) * 1000.0),
        "refresh_in_progress": (
            _YFINANCE_CACHE_REFRESH_IN_PROGRESS or bool(_YFINANCE_SYMBOL_REFRESH_IN_PROGRESS)
        ),
    }


__all__ = [
    "get_cache_debug_snapshot",
    "get_yfinance_mapping_snapshot",
    "lookup_yfinance_sector_labels",
    "warmup",
]
