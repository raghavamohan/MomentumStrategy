"""Joined equity metadata resolution (NSE + Kite + yfinance).

This provider implements the resolution hierarchy for equity names, sectors, and industries
by joining data from multiple sources. It allows consuming code to use a unified
interface instead of manual fallbacks across Kite, NSE, and yfinance.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.domain.reference_context import WarmupContext
from app.infrastructure.cache.text_normalize import normalise_isin, normalise_name, normalise_symbol
from app.infrastructure.cache.yfinance_provider import lookup_yfinance_sector_labels
from app.infrastructure.cache.model_cache_store import (
    current_effective_day_ist,
    next_cutoff_epoch_ist,
)

logger = logging.getLogger(__name__)

_METADATA_LOCK = threading.Lock()

@dataclass(frozen=True)
class EquityMetadata:
    symbol: str
    exchange: str
    name: str = ""
    industry: str = ""
    sector: str = ""
    isin: str = ""
    source: str = "unknown"


def resolve_metadata(
    symbol: str,
    exchange: str | None,
    instrument_token: int | None = None,
    *,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> EquityMetadata:
    """Implement the cross-provider resolution hierarchy for a single equity."""
    clean_symbol = normalise_symbol(symbol)
    clean_exchange = str(exchange or "").strip().upper()
    token = int(instrument_token or 0)

    # 1. Resolve Name (priority: Kite)
    name = ""
    if token > 0:
        name = normalise_name(token_to_name.get(token))
    if not name and clean_symbol:
        name = normalise_name(symbol_to_name.get((clean_exchange, clean_symbol)))

    # ETF check (mirrors portfolio_model logic)
    if "ETF" in clean_symbol or "ETF" in name.upper():
        return EquityMetadata(
            symbol=clean_symbol,
            exchange=clean_exchange,
            name=name,
            industry="ETF",
            sector="ETF",
            source="etf_rule"
        )

    # 2. Resolve Sector/Industry Fallback Chain
    sec = ""
    source = "unknown"

    # Priority A: yfinance
    if clean_symbol and clean_exchange in ("NSE", "BSE"):
        y_sec, y_ind, _, _ = lookup_yfinance_sector_labels(clean_exchange, clean_symbol)
        if y_sec:
            sec = y_sec
            source = "yfinance"
        elif y_ind:
            sec = y_ind
            source = "yfinance_industry"

    # Priority B: Kite Sector
    if not sec and token > 0:
        sec = normalise_name(token_to_kite_sector.get(token))
        if sec:
            source = "kite_token"
    if not sec and clean_symbol:
        sec = normalise_name(symbol_to_kite_sector.get((clean_exchange, clean_symbol)))
        if sec:
            source = "kite_symbol"

    # Priority C: NSE CSV Industry by Symbol
    if not sec and clean_symbol:
        sec = normalise_name(nse_symbol_to_industry.get(clean_symbol))
        if sec:
            source = "nse_csv_symbol"

    # Priority D: ISIN mapping
    isin = ""
    if not sec:
        if token > 0:
            isin = normalise_isin(token_to_isin.get(token))
        if not isin and clean_symbol:
            isin = normalise_isin(symbol_to_isin.get((clean_exchange, clean_symbol)))
        if not isin and clean_symbol:
            if clean_exchange == "BSE":
                isin = normalise_isin(symbol_to_isin.get(("NSE", clean_symbol)))
            elif clean_exchange == "NSE":
                isin = normalise_isin(symbol_to_isin.get(("BSE", clean_symbol)))

        if isin:
            sec = normalise_name(isin_to_industry.get(isin))
            if sec:
                source = "nse_csv_isin"

    # If still no ISIN, try to find it for the metadata object anyway
    if not isin:
        if token > 0:
            isin = normalise_isin(token_to_isin.get(token))
        if not isin and clean_symbol:
            isin = normalise_isin(symbol_to_isin.get((clean_exchange, clean_symbol)))

    return EquityMetadata(
        symbol=clean_symbol,
        exchange=clean_exchange,
        name=name,
        industry=sec,
        sector=sec,
        isin=isin,
        source=source,
    )


def warmup(ctx: WarmupContext) -> None:
    """Populate resolved metadata if required (currently stateless)."""
    pass


def equity_metadata_reference_debug_snapshot(now: float) -> dict[str, Any]:
    """Metadata row for debug snapshots."""
    return {
        "source": "resolved_aggregate",
        "expires_in_ms": max(0.0, (next_cutoff_epoch_ist(9) - now) * 1000.0),
        "refresh_in_progress": False,
    }


__all__ = [
    "EquityMetadata",
    "resolve_metadata",
    "warmup",
    "equity_metadata_reference_debug_snapshot",
]
