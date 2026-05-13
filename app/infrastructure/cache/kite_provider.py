"""Kite Connect instrument master cache (NSE/BSE cash equity lookups)."""

from __future__ import annotations

import logging
import threading
from typing import Any

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

# --- Provider State ---
_KITE_CACHE = BaseCache("kite_provider")
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_REFRESH_IN_PROGRESS = False

_CACHE_DAY = ""
_SOURCE_LABEL = "unknown"
_EXPIRES_AT = 0.0

_TOKEN_TO_NAME: dict[int, str] = {}
_SYMBOL_TO_NAME: dict[tuple[str, str], str] = {}
_SYMBOL_TO_TOKEN: dict[tuple[str, str], int] = {}
_TOKEN_TO_INDUSTRY: dict[int, str] = {}
_SYMBOL_TO_INDUSTRY: dict[tuple[str, str], str] = {}
_TOKEN_TO_KITE_SECTOR: dict[int, str] = {}
_SYMBOL_TO_KITE_SECTOR: dict[tuple[str, str], str] = {}
_TOKEN_TO_ISIN: dict[int, str] = {}
_SYMBOL_TO_ISIN: dict[tuple[str, str], str] = {}

EQUITY_EXCHANGES = ("NSE", "BSE")


def _encode_int_key_dict(data: dict[int, Any]) -> dict[str, Any]:
    return {str(int(k)): v for k, v in data.items() if int(k) > 0}


def _decode_int_key_dict(data: Any, value_cast: Any = str) -> dict[int, Any]:
    out: dict[int, Any] = {}
    if not isinstance(data, dict):
        return out
    for raw_key, raw_val in data.items():
        try:
            key = int(raw_key)
            if key > 0:
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
        if isinstance(raw_key, str) and "|" in raw_key:
            exch, sym = raw_key.split("|", 1)
            exch = exch.strip().upper()
            sym = normalise_symbol(sym)
            if exch and sym:
                try:
                    out[(exch, sym)] = value_cast(raw_val)
                except (TypeError, ValueError):
                    continue
    return out


def _load_cache_unlocked() -> None:
    global _CACHE_LOADED, _CACHE_DAY, _SOURCE_LABEL, _EXPIRES_AT
    if _CACHE_LOADED:
        return

    payload = _KITE_CACHE.read_section("kite")
    entry = payload.get("cash_equity")
    if not isinstance(entry, dict):
        _CACHE_LOADED = True
        return

    data = entry.get("payload") or {}
    _CACHE_DAY = str(entry.get("cache_day") or "").strip()

    _TOKEN_TO_NAME.update(_decode_int_key_dict(data.get("token_to_name"), str))
    _SYMBOL_TO_NAME.update(_decode_symbol_key_dict(data.get("symbol_to_name"), str))
    _SYMBOL_TO_TOKEN.update(_decode_symbol_key_dict(data.get("symbol_to_token"), int))
    _TOKEN_TO_INDUSTRY.update(_decode_int_key_dict(data.get("token_to_industry"), str))
    _SYMBOL_TO_INDUSTRY.update(_decode_symbol_key_dict(data.get("symbol_to_industry"), str))
    _TOKEN_TO_KITE_SECTOR.update(_decode_int_key_dict(data.get("token_to_kite_sector"), str))
    _SYMBOL_TO_KITE_SECTOR.update(_decode_symbol_key_dict(data.get("symbol_to_kite_sector"), str))
    _TOKEN_TO_ISIN.update(_decode_int_key_dict(data.get("token_to_isin"), str))
    _SYMBOL_TO_ISIN.update(_decode_symbol_key_dict(data.get("symbol_to_isin"), str))

    if _TOKEN_TO_NAME:
        _SOURCE_LABEL = "disk"
        _EXPIRES_AT = next_cutoff_epoch_ist()

    _CACHE_LOADED = True


def _persist_cache_unlocked() -> None:
    payload = {
        "token_to_name": _encode_int_key_dict(_TOKEN_TO_NAME),
        "symbol_to_name": _encode_symbol_key_dict(_SYMBOL_TO_NAME),
        "symbol_to_token": _encode_symbol_key_dict(_SYMBOL_TO_TOKEN),
        "token_to_industry": _encode_int_key_dict(_TOKEN_TO_INDUSTRY),
        "symbol_to_industry": _encode_symbol_key_dict(_SYMBOL_TO_INDUSTRY),
        "token_to_kite_sector": _encode_int_key_dict(_TOKEN_TO_KITE_SECTOR),
        "symbol_to_kite_sector": _encode_symbol_key_dict(_SYMBOL_TO_KITE_SECTOR),
        "token_to_isin": _encode_int_key_dict(_TOKEN_TO_ISIN),
        "symbol_to_isin": _encode_symbol_key_dict(_SYMBOL_TO_ISIN),
    }
    _KITE_CACHE.update_section(
        "kite",
        lambda root: {
            **root,
            "cash_equity": {
                "cache_day": _CACHE_DAY,
                "payload": payload,
            },
        },
    )


def _maybe_start_refresh_unlocked(kite) -> None:
    global _REFRESH_IN_PROGRESS, _SOURCE_LABEL

    if _REFRESH_IN_PROGRESS:
        return

    current_day = current_effective_day_ist()
    if _CACHE_DAY == current_day and _TOKEN_TO_NAME:
        return

    _SOURCE_LABEL = "disk_stale_bg_refresh" if _TOKEN_TO_NAME else "cold_start_bg_refresh"
    _REFRESH_IN_PROGRESS = True

    def _job() -> None:
        global _REFRESH_IN_PROGRESS, _CACHE_DAY, _SOURCE_LABEL, _EXPIRES_AT
        ok = False
        try:
            results = _fetch_instruments(kite)
            if not results[0]: # token_to_name
                return

            with _CACHE_LOCK:
                _TOKEN_TO_NAME.clear(); _TOKEN_TO_NAME.update(results[0])
                _SYMBOL_TO_NAME.clear(); _SYMBOL_TO_NAME.update(results[1])
                _SYMBOL_TO_TOKEN.clear(); _SYMBOL_TO_TOKEN.update(results[2])
                _TOKEN_TO_INDUSTRY.clear(); _TOKEN_TO_INDUSTRY.update(results[3])
                _SYMBOL_TO_INDUSTRY.clear(); _SYMBOL_TO_INDUSTRY.update(results[4])
                _TOKEN_TO_KITE_SECTOR.clear(); _TOKEN_TO_KITE_SECTOR.update(results[5])
                _SYMBOL_TO_KITE_SECTOR.clear(); _SYMBOL_TO_KITE_SECTOR.update(results[6])
                _TOKEN_TO_ISIN.clear(); _TOKEN_TO_ISIN.update(results[7])
                _SYMBOL_TO_ISIN.clear(); _SYMBOL_TO_ISIN.update(results[8])

                _CACHE_DAY = current_effective_day_ist()
                _EXPIRES_AT = next_cutoff_epoch_ist()
                _SOURCE_LABEL = "network_bg_refresh"
                _persist_cache_unlocked()
                ok = True
        finally:
            with _CACHE_LOCK:
                _REFRESH_IN_PROGRESS = False
                if not ok:
                    _SOURCE_LABEL = "network_bg_refresh_failed"
            if ok:
                notify_reference_cache_refresh()

    start_background_refresh_job("reference-cash-equity", _job)


def _fetch_instruments(kite) -> tuple[dict, ...]:
    """Blocking fetch of all equity instruments."""
    t_name, s_name, s_tok, t_ind, s_ind, t_sec, s_sec, t_isin, s_isin = [{} for _ in range(9)]

    for exch in EQUITY_EXCHANGES:
        try:
            instruments = kite.instruments(exch) or []
            for row in instruments:
                sym = normalise_symbol(row.get("tradingsymbol"))
                tok = int(row.get("instrument_token") or 0)
                name = normalise_name(row.get("name"))
                if not sym or tok <= 0: continue

                s_tok[(exch, sym)] = tok
                if name:
                    t_name[tok] = name
                    s_name[(exch, sym)] = name

                # Industry/Sector
                is_eq = str(row.get("instrument_type") or "").strip().upper() == "EQ"
                ind = normalise_name(row.get("industry") or row.get("sector"))
                sec = normalise_name(row.get("sector"))
                if is_eq:
                    isin = normalise_isin(row.get("isin"))
                    if isin:
                        t_isin[tok] = isin
                        s_isin[(exch, sym)] = isin

                if ind:
                    t_ind[tok] = ind
                    s_ind[(exch, sym)] = ind
                if sec:
                    t_sec[tok] = sec
                    s_sec[(exch, sym)] = sec

        except Exception:
            continue
    return (t_name, s_name, s_tok, t_ind, s_ind, t_sec, s_sec, t_isin, s_isin)


def get_cash_equity_name_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        _maybe_start_refresh_unlocked(kite)
        return (dict(_TOKEN_TO_NAME), dict(_SYMBOL_TO_NAME))


def get_cash_equity_kite_sector_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        _maybe_start_refresh_unlocked(kite)
        return (dict(_TOKEN_TO_KITE_SECTOR), dict(_SYMBOL_TO_KITE_SECTOR))


def get_cash_equity_isin_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        _maybe_start_refresh_unlocked(kite)
        return (dict(_TOKEN_TO_ISIN), dict(_SYMBOL_TO_ISIN))


def get_nse_symbol_to_token_lookup(kite) -> dict[str, int]:
    with _CACHE_LOCK:
        _load_cache_unlocked()
        _maybe_start_refresh_unlocked(kite)
        return {s: t for (e, s), t in _SYMBOL_TO_TOKEN.items() if e == "NSE"}


def warmup(ctx: WarmupContext) -> None:
    if ctx.kite is not None:
        with _CACHE_LOCK:
            if ctx.force_refresh:
                global _CACHE_DAY
                _CACHE_DAY = ""
            _load_cache_unlocked()
            _maybe_start_refresh_unlocked(ctx.kite)


def kite_reference_debug_snapshot(now: float) -> dict[str, Any]:
    with _CACHE_LOCK:
        return {
            "source": _SOURCE_LABEL,
            "expires_in_ms": max(0.0, (_EXPIRES_AT - now) * 1000.0),
            "refresh_in_progress": _REFRESH_IN_PROGRESS,
            "cache_day": _CACHE_DAY,
            "entries": len(_TOKEN_TO_NAME),
        }


__all__ = [
    "warmup",
    "get_cash_equity_isin_lookups",
    "get_cash_equity_kite_sector_lookups",
    "get_cash_equity_name_lookups",
    "get_nse_symbol_to_token_lookup",
    "kite_reference_debug_snapshot",
]
