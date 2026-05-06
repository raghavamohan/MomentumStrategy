"""Instrument metadata helpers for display labels.

This module builds a cached lookup of cash-equity company names from
``kite.instruments("NSE")`` and ``kite.instruments("BSE")`` so callers
can render a friendly ``SYMBOL - Company Name`` label.
"""

from __future__ import annotations

import threading
import time
from typing import Any


EQUITY_EXCHANGES = ("NSE", "BSE")
_CACHE_TTL_SECONDS = 60 * 60
_CACHE_LOCK = threading.Lock()
_CACHE_EXPIRES_AT = 0.0
_CACHED_TOKEN_TO_NAME: dict[int, str] = {}
_CACHED_SYMBOL_TO_NAME: dict[tuple[str, str], str] = {}


def _normalise_name(raw: Any) -> str:
    """Return a trimmed company name or empty string."""
    return str(raw or "").strip()


def _normalise_symbol(raw: Any) -> str:
    """Return a trimmed symbol in uppercase or empty string."""
    return str(raw or "").strip().upper()


def get_cash_equity_name_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Return cached (token->name, (exchange,symbol)->name) mappings.

    The cache is refreshed at most once per hour to avoid re-downloading
    the large instrument master on every dashboard refresh.
    """
    global _CACHE_EXPIRES_AT, _CACHED_TOKEN_TO_NAME, _CACHED_SYMBOL_TO_NAME
    now = time.time()
    with _CACHE_LOCK:
        if now < _CACHE_EXPIRES_AT and _CACHED_TOKEN_TO_NAME and _CACHED_SYMBOL_TO_NAME:
            return (dict(_CACHED_TOKEN_TO_NAME), dict(_CACHED_SYMBOL_TO_NAME))

        token_to_name: dict[int, str] = {}
        symbol_to_name: dict[tuple[str, str], str] = {}

        for exchange in EQUITY_EXCHANGES:
            try:
                instruments = kite.instruments(exchange) or []
            except Exception:
                continue

            for row in instruments:
                name = _normalise_name(row.get("name"))
                if not name:
                    continue

                symbol = _normalise_symbol(row.get("tradingsymbol"))
                if symbol:
                    symbol_to_name[(exchange, symbol)] = name

                token = int(row.get("instrument_token") or 0)
                if token > 0:
                    token_to_name[token] = name

        _CACHED_TOKEN_TO_NAME = token_to_name
        _CACHED_SYMBOL_TO_NAME = symbol_to_name
        _CACHE_EXPIRES_AT = now + _CACHE_TTL_SECONDS
        return (dict(_CACHED_TOKEN_TO_NAME), dict(_CACHED_SYMBOL_TO_NAME))


def symbol_with_company_name(
    symbol: str,
    exchange: str | None,
    instrument_token: int | None,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
) -> str:
    """Return display label as ``Company Name`` for cash equities."""
    clean_symbol = _normalise_symbol(symbol)
    clean_exchange = str(exchange or "").strip().upper()
    token = int(instrument_token or 0)

    if clean_exchange not in EQUITY_EXCHANGES:
        return clean_symbol

    name = ""
    if token > 0:
        name = _normalise_name(token_to_name.get(token))
    if not name and clean_symbol:
        name = _normalise_name(symbol_to_name.get((clean_exchange, clean_symbol)))

    if name and name.lower() != clean_symbol.lower():
        return name
    return clean_symbol
