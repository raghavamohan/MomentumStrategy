"""yfinance-backed sector/industry cache (disk section ``yfinance`` in ``model_cache.json``)."""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.domain.reference_context import WarmupContext
from app.domain.reference_notifications import notify_reference_cache_refresh
from app.infrastructure.cache.text_normalize import normalise_name, normalise_symbol
from app.infrastructure.cache.model_cache_store import (
    BaseCache,
    current_effective_day_ist,
    next_cutoff_epoch_ist,
    start_background_refresh_job,
)

logger = logging.getLogger(__name__)

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover
    yf = None

_EQUITY_EXCHANGES = ("NSE", "BSE")

# --- Provider State ---
_YFINANCE_CACHE = BaseCache("yfinance_provider")
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_REFRESH_IN_PROGRESS = False
_SYMBOL_REFRESH_IN_PROGRESS: set[tuple[str, str]] = set()

_CACHE_DAY = ""
_SOURCE_LABEL = "unknown"
_EXPIRES_AT = 0.0

_INDUSTRY_MAP: dict[tuple[str, str], str] = {}
_SECTOR_MAP: dict[tuple[str, str], str] = {}


def _yfinance_cache_key(exchange: str, symbol: str) -> tuple[str, str]:
    return (exchange.upper().strip(), normalise_symbol(symbol))


def _load_cache_unlocked() -> None:
    """Hydrate in-memory maps from the 'yfinance' disk section."""
    global _CACHE_LOADED, _SOURCE_LABEL, _CACHE_DAY, _EXPIRES_AT
    if _CACHE_LOADED:
        return

    try:
        payload = _YFINANCE_CACHE.read_section("yfinance")
        mapping = payload.get("mapping") if isinstance(payload, dict) else {}
        _CACHE_DAY = str(payload.get("cache_day") or "").strip() if isinstance(payload, dict) else ""

        for k, v in (mapping or {}).items():
            try:
                exch, sym = str(k).split("|", 1)
                key = _yfinance_cache_key(exch, sym)
                if isinstance(v, dict):
                    _INDUSTRY_MAP[key] = normalise_name(v.get("industry"))
                    _SECTOR_MAP[key] = normalise_name(v.get("sector"))
                else:
                    _INDUSTRY_MAP[key] = normalise_name(v)
                    _SECTOR_MAP[key] = ""
            except (ValueError, TypeError):
                continue

        if _INDUSTRY_MAP:
            _SOURCE_LABEL = "disk"
            _EXPIRES_AT = next_cutoff_epoch_ist()
    except Exception as exc:
        logger.warning("Failed to load yfinance cache: %s", exc)

    _CACHE_LOADED = True


def _persist_cache_unlocked() -> None:
    """Save current in-memory maps to the 'yfinance' disk section."""
    mapping: dict[str, dict[str, str]] = {}
    all_keys = set(_INDUSTRY_MAP.keys()) | set(_SECTOR_MAP.keys())
    for exch, sym in all_keys:
        mapping[f"{exch}|{sym}"] = {
            "industry": _INDUSTRY_MAP.get((exch, sym), ""),
            "sector": _SECTOR_MAP.get((exch, sym), ""),
        }

    _YFINANCE_CACHE.update_section(
        "yfinance",
        lambda _: {
            "mapping": mapping,
            "cache_day": _CACHE_DAY,
        },
    )


def _maybe_start_daily_refresh_unlocked() -> None:
    """Trigger background refresh if the cache day is stale."""
    global _SOURCE_LABEL, _REFRESH_IN_PROGRESS

    if _REFRESH_IN_PROGRESS:
        return

    current_day = current_effective_day_ist()
    if _CACHE_DAY == current_day and _INDUSTRY_MAP:
        return

    if _INDUSTRY_MAP:
        _SOURCE_LABEL = "disk_stale_bg_refresh"
    else:
        _SOURCE_LABEL = "cold_start_bg_refresh"

    _REFRESH_IN_PROGRESS = True

    def _job() -> None:
        global _REFRESH_IN_PROGRESS, _CACHE_DAY, _SOURCE_LABEL, _EXPIRES_AT
        ok = False
        try:
            if yf is None:
                return

            with _CACHE_LOCK:
                keys = list(_INDUSTRY_MAP.keys())

            updated = 0
            for exch, sym in keys:
                sym_up = sym.upper()
                if "NIFTY" in sym_up or "SENSEX" in sym_up or "BANK" in sym_up or "INDIAVIX" in sym_up:
                    continue
                    
                yf_symbol = sym if "." in sym else f"{sym}{'.NS' if exch == 'NSE' else '.BO'}"
                try:
                    info = yf.Ticker(yf_symbol).info or {}
                    ind = normalise_name(info.get("industry") or info.get("sector"))
                    sec = normalise_name(info.get("sector") or ind)
                    with _CACHE_LOCK:
                        _INDUSTRY_MAP[(exch, sym)] = ind
                        _SECTOR_MAP[(exch, sym)] = sec
                    if ind:
                        updated += 1
                except Exception:
                    continue

            with _CACHE_LOCK:
                _CACHE_DAY = current_effective_day_ist()
                _EXPIRES_AT = next_cutoff_epoch_ist()
                _persist_cache_unlocked()
                _SOURCE_LABEL = "network_bg_refresh"
                ok = True

            if updated:
                logger.info("yfinance cache refreshed for %d symbols", updated)
        finally:
            with _CACHE_LOCK:
                _REFRESH_IN_PROGRESS = False
                if not ok and _SOURCE_LABEL == "network_bg_refresh":
                    _SOURCE_LABEL = "network_bg_refresh_failed"
            if ok:
                notify_reference_cache_refresh()

    start_background_refresh_job("yfinance-daily", _job)


def _maybe_start_symbol_refresh_unlocked(exchange: str, symbol: str) -> None:
    """Trigger background refresh for a single missing symbol."""
    if yf is None:
        return

    key = _yfinance_cache_key(exchange, symbol)
    if key in _SYMBOL_REFRESH_IN_PROGRESS:
        return
    _SYMBOL_REFRESH_IN_PROGRESS.add(key)

    def _job() -> None:
        global _SOURCE_LABEL, _CACHE_DAY, _EXPIRES_AT
        try:
            exch, sym = key
            sym_up = sym.upper()
            if "NIFTY" in sym_up or "SENSEX" in sym_up or "BANK" in sym_up or "INDIAVIX" in sym_up:
                with _CACHE_LOCK:
                    _SYMBOL_REFRESH_IN_PROGRESS.discard(key)
                return

            yf_symbol = sym if "." in sym else f"{sym}{'.NS' if exch == 'NSE' else '.BO'}"
            info = yf.Ticker(yf_symbol).info or {}
            ind = normalise_name(info.get("industry") or info.get("sector"))
            sec = normalise_name(info.get("sector") or ind)

            with _CACHE_LOCK:
                _INDUSTRY_MAP[key] = ind
                _SECTOR_MAP[key] = sec
                _CACHE_DAY = current_effective_day_ist()
                _EXPIRES_AT = next_cutoff_epoch_ist()
                _persist_cache_unlocked()
                _SOURCE_LABEL = "network_bg_refresh"
            notify_reference_cache_refresh()
        except Exception as exc:
            logger.warning("yfinance symbol refresh failed for %s: %s", key, exc)
        finally:
            with _CACHE_LOCK:
                _SYMBOL_REFRESH_IN_PROGRESS.discard(key)

    start_background_refresh_job(f"yfinance-symbol-{key[0]}-{key[1]}", _job)


def lookup_yfinance_sector_labels(exchange: str, symbol: str) -> tuple[str, str, Any, Any]:
    """Resolve cached sector/industry with fallback to background refresh."""
    with _CACHE_LOCK:
        _load_cache_unlocked()
        _maybe_start_daily_refresh_unlocked()

        clean_exchange = str(exchange or "").strip().upper()
        clean_symbol = normalise_symbol(symbol)
        if not clean_symbol or clean_exchange not in _EQUITY_EXCHANGES:
            return "", "", None, None

        # Check primary exchange then fallback
        keys = [_yfinance_cache_key(clean_exchange, clean_symbol)]
        if clean_exchange == "NSE":
            keys.append(_yfinance_cache_key("BSE", clean_symbol))
        else:
            keys.append(_yfinance_cache_key("NSE", clean_symbol))

        for k in keys:
            sec = _SECTOR_MAP.get(k, "")
            ind = _INDUSTRY_MAP.get(k, "")
            if sec or ind:
                return sec, ind, k, k

        _maybe_start_symbol_refresh_unlocked(clean_exchange, clean_symbol)
        return "", "", None, None


def get_yfinance_mapping_snapshot() -> tuple[str, dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        return (_CACHE_DAY, dict(_SECTOR_MAP), dict(_INDUSTRY_MAP))


def warmup(ctx: WarmupContext) -> None:
    global _CACHE_DAY
    with _CACHE_LOCK:
        if ctx.force_refresh:
            _CACHE_DAY = ""
        _load_cache_unlocked()
        _maybe_start_daily_refresh_unlocked()


def yfinance_reference_debug_snapshot(now: float) -> dict[str, Any]:
    with _CACHE_LOCK:
        return {
            "source": _SOURCE_LABEL,
            "expires_in_ms": max(0.0, (_EXPIRES_AT - now) * 1000.0),
            "refresh_in_progress": _REFRESH_IN_PROGRESS or bool(_SYMBOL_REFRESH_IN_PROGRESS),
            "cache_day": _CACHE_DAY,
            "entries": len(_INDUSTRY_MAP),
        }


__all__ = [
    "get_yfinance_mapping_snapshot",
    "lookup_yfinance_sector_labels",
    "warmup",
    "yfinance_reference_debug_snapshot",
]
