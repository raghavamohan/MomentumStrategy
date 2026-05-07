"""Instrument metadata helpers for display labels.

This module builds a cached lookup of cash-equity company names from
``kite.instruments("NSE")`` and ``kite.instruments("BSE")`` so callers
can render a friendly ``SYMBOL - Company Name`` label.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
import logging


EQUITY_EXCHANGES = ("NSE", "BSE")
_CACHE_TTL_SECONDS = 60 * 60
_CACHE_LOCK = threading.Lock()
_CACHE_EXPIRES_AT = 0.0
_CACHED_TOKEN_TO_NAME: dict[int, str] = {}
_CACHED_SYMBOL_TO_NAME: dict[tuple[str, str], str] = {}
_CACHED_SYMBOL_TO_TOKEN: dict[tuple[str, str], int] = {}

_NIFTY50_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
_NIFTY50_CACHE_TTL_SECONDS = 30 * 60
_NIFTY50_CACHE_EXPIRES_AT = 0.0
_CACHED_NIFTY50_SYMBOLS: list[str] = []
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}

logger = logging.getLogger(__name__)


def _normalise_name(raw: Any) -> str:
    """Return a trimmed company name or empty string."""
    return str(raw or "").strip()


def _normalise_symbol(raw: Any) -> str:
    """Return a trimmed symbol in uppercase or empty string."""
    return str(raw or "").strip().upper()


def _refresh_cash_equity_cache(kite) -> None:
    """Refresh equity instrument caches when TTL expires."""
    global _CACHE_EXPIRES_AT, _CACHED_TOKEN_TO_NAME, _CACHED_SYMBOL_TO_NAME, _CACHED_SYMBOL_TO_TOKEN
    now = time.time()
    if now < _CACHE_EXPIRES_AT and _CACHED_TOKEN_TO_NAME and _CACHED_SYMBOL_TO_NAME:
        return

    token_to_name: dict[int, str] = {}
    symbol_to_name: dict[tuple[str, str], str] = {}
    symbol_to_token: dict[tuple[str, str], int] = {}

    for exchange in EQUITY_EXCHANGES:
        try:
            instruments = kite.instruments(exchange) or []
        except Exception:
            continue

        for row in instruments:
            symbol = _normalise_symbol(row.get("tradingsymbol"))
            token = int(row.get("instrument_token") or 0)
            if symbol and token > 0:
                symbol_to_token[(exchange, symbol)] = token

            name = _normalise_name(row.get("name"))
            if not name:
                continue

            if symbol:
                symbol_to_name[(exchange, symbol)] = name
            if token > 0:
                token_to_name[token] = name

    _CACHED_TOKEN_TO_NAME = token_to_name
    _CACHED_SYMBOL_TO_NAME = symbol_to_name
    _CACHED_SYMBOL_TO_TOKEN = symbol_to_token
    _CACHE_EXPIRES_AT = now + _CACHE_TTL_SECONDS


def get_cash_equity_name_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Return cached (token->name, (exchange,symbol)->name) mappings.

    The cache is refreshed at most once per hour to avoid re-downloading
    the large instrument master on every dashboard refresh.
    """
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return (dict(_CACHED_TOKEN_TO_NAME), dict(_CACHED_SYMBOL_TO_NAME))


def get_nse_symbol_to_token_lookup(kite) -> dict[str, int]:
    """Return cached NSE ``tradingsymbol -> instrument_token`` mapping."""
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return {
            symbol: token
            for (exchange, symbol), token in _CACHED_SYMBOL_TO_TOKEN.items()
            if exchange == "NSE" and token > 0
        }


def get_nifty50_symbols() -> list[str]:
    """Fetch and cache Nifty50 constituents from NSE archive CSV."""
    global _NIFTY50_CACHE_EXPIRES_AT, _CACHED_NIFTY50_SYMBOLS
    now = time.time()
    with _CACHE_LOCK:
        if now < _NIFTY50_CACHE_EXPIRES_AT and _CACHED_NIFTY50_SYMBOLS:
            return list(_CACHED_NIFTY50_SYMBOLS)

        req = Request(_NIFTY50_CSV_URL, headers=_NSE_HEADERS)
        try:
            with urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, OSError) as exc:
            logger.warning("Failed to fetch Nifty50 CSV from NSE: %s", exc)
            return []

        reader = csv.DictReader(io.StringIO(body))
        ordered_unique: list[str] = []
        seen: set[str] = set()
        for row in reader:
            symbol = _normalise_symbol((row or {}).get("Symbol"))
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            ordered_unique.append(symbol)

        _CACHED_NIFTY50_SYMBOLS = ordered_unique
        _NIFTY50_CACHE_EXPIRES_AT = now + _NIFTY50_CACHE_TTL_SECONDS
        return list(_CACHED_NIFTY50_SYMBOLS)


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
