"""Shared warmup context for reference cache providers (avoids circular imports)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WarmupContext:
    """Parameters for :func:`app.domain.reference_snapshot.warm_reference_snapshot`.

    Which fields each provider reads:

    - **nse_provider:** ``force_refresh`` — under ``instrument_reference_lock``, starts
      background refresh for merged industry CSVs and Nifty50 even when TTL is valid.
    - **yfinance_provider:** ``force_refresh`` — resets daily-refresh bookkeeping so the
      next lookup can schedule a background map refresh again.
    - **mfdata_provider:** no fields (preload / align mfdata section only).
    - **marketsmith_provider:** ``marketsmith_force_sync`` — passed to
      :func:`~app.infrastructure.cache.marketsmith_provider.get_marketsmith_market_condition` as
      ``force_sync_fetch`` for blocking network fetch (primarily tooling/tests).
    - **kite_provider:** ``kite`` — when set, hydrates cash-equity caches;
      ``force_refresh`` triggers background instrument refresh even if memory TTL is valid.
    """

    kite: Any | None = None
    force_refresh: bool = False
    marketsmith_force_sync: bool = False
