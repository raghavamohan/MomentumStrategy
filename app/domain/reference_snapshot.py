"""Typed aggregate snapshots for reference data (providers → portfolio layer)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from app.infrastructure.cache import kite_provider
from app.infrastructure.cache import mfdata_provider
from app.infrastructure.cache import marketsmith_provider
from app.infrastructure.cache import nse_provider
from app.infrastructure.cache import yfinance_provider
from app.infrastructure.cache import equity_metadata_provider
from app.domain.reference_context import WarmupContext

logger = logging.getLogger(__name__)

REFERENCE_PROVIDER_WARMUPS: tuple[tuple[str, Callable[[WarmupContext], None]], ...] = (
    ("nse", nse_provider.warmup),
    ("yfinance", yfinance_provider.warmup),
    ("mfdata", mfdata_provider.warmup),
    ("marketsmith", marketsmith_provider.warmup),
    ("kite", kite_provider.warmup),
    ("equity_metadata", equity_metadata_provider.warmup),
)

_revision_lock = threading.Lock()
_reference_revision = 0


def _bump_reference_revision() -> None:
    global _reference_revision
    with _revision_lock:
        _reference_revision += 1


def get_reference_revision() -> int:
    """Monotonic counter bumped after debounced reference-cache notifications."""
    with _revision_lock:
        return _reference_revision


@dataclass(frozen=True)
class KiteCashEquitySnapshot:
    token_to_name: dict[int, str]
    symbol_to_name: dict[tuple[str, str], str]
    token_to_kite_sector: dict[int, str]
    symbol_to_kite_sector: dict[tuple[str, str], str]
    token_to_isin: dict[int, str]
    symbol_to_isin: dict[tuple[str, str], str]
    nse_symbol_to_token: dict[str, int]


@dataclass(frozen=True)
class NseReferenceSnapshot:
    symbol_to_industry: dict[str, str]
    isin_to_industry: dict[str, str]
    nifty50_symbols: tuple[str, ...]


@dataclass(frozen=True)
class YfinanceReferenceSnapshot:
    cache_day: str
    key_to_sector: dict[tuple[str, str], str]
    key_to_industry: dict[tuple[str, str], str]


@dataclass(frozen=True)
class MfdataDiskSnapshot:
    cache_day: str
    search_entries: int
    holdings_entries: int


@dataclass(frozen=True)
class MarketsmithReferenceSnapshot:
    market_condition: dict[str, Any]


@dataclass(frozen=True)
class ReferenceSnapshot:
    kite: KiteCashEquitySnapshot
    nse: NseReferenceSnapshot
    yfinance: YfinanceReferenceSnapshot
    mfdata: MfdataDiskSnapshot
    marketsmith: MarketsmithReferenceSnapshot
    revision: int


def _empty_kite_snapshot() -> KiteCashEquitySnapshot:
    return KiteCashEquitySnapshot(
        token_to_name={},
        symbol_to_name={},
        token_to_kite_sector={},
        symbol_to_kite_sector={},
        token_to_isin={},
        symbol_to_isin={},
        nse_symbol_to_token={},
    )


def build_reference_snapshot(
    kite: Any | None,
    *,
    market_condition: dict[str, Any] | None = None,
) -> ReferenceSnapshot:
    """Compose immutable snapshots from provider caches (single dashboard entry point).

    When ``market_condition`` is omitted, loads MarketSmith via
    :func:`app.infrastructure.cache.marketsmith_provider.get_marketsmith_market_condition` (non-blocking
    unless callers pass a pre-fetched dict from a parallel fetch).
    """
    revision = get_reference_revision()

    if kite is not None:
        t_name, s_name = kite_provider.get_cash_equity_name_lookups(kite)
        t_sec, s_sec = kite_provider.get_cash_equity_kite_sector_lookups(kite)
        t_isin, s_isin = kite_provider.get_cash_equity_isin_lookups(kite)
        nse_tok = kite_provider.get_nse_symbol_to_token_lookup(kite)
        kite_snap = KiteCashEquitySnapshot(
            token_to_name=dict(t_name),
            symbol_to_name=dict(s_name),
            token_to_kite_sector=dict(t_sec),
            symbol_to_kite_sector=dict(s_sec),
            token_to_isin=dict(t_isin),
            symbol_to_isin=dict(s_isin),
            nse_symbol_to_token=dict(nse_tok),
        )
    else:
        kite_snap = _empty_kite_snapshot()

    nse_snap = NseReferenceSnapshot(
        symbol_to_industry=dict(nse_provider.get_nse_symbol_to_industry()),
        isin_to_industry=dict(nse_provider.get_isin_to_industry()),
        nifty50_symbols=tuple(nse_provider.get_nifty50_symbols()),
    )

    y_day, y_sec, y_ind = yfinance_provider.get_yfinance_mapping_snapshot()
    y_snap = YfinanceReferenceSnapshot(
        cache_day=y_day,
        key_to_sector=dict(y_sec),
        key_to_industry=dict(y_ind),
    )

    mf_day, mf_s, mf_h = mfdata_provider.mfdata_disk_table_snapshot()
    mf_snap = MfdataDiskSnapshot(
        cache_day=mf_day,
        search_entries=mf_s,
        holdings_entries=mf_h,
    )

    mc = market_condition
    if mc is None:
        mc = marketsmith_provider.get_marketsmith_market_condition(force_sync_fetch=False)
    ms_snap = MarketsmithReferenceSnapshot(market_condition=dict(mc))

    return ReferenceSnapshot(
        kite=kite_snap,
        nse=nse_snap,
        yfinance=y_snap,
        mfdata=mf_snap,
        marketsmith=ms_snap,
        revision=revision,
    )


def warm_reference_snapshot(ctx: WarmupContext) -> None:
    """Best-effort warmup across providers (order: ``REFERENCE_PROVIDER_WARMUPS``)."""
    for tag, warmup_fn in REFERENCE_PROVIDER_WARMUPS:
        try:
            warmup_fn(ctx)
        except Exception as exc:
            logger.warning("warm_reference_snapshot %s failed: %s", tag, exc)


def _register_revision_with_notifications() -> None:
    from app.domain import reference_notifications as rn

    rn.register_reference_revision_bump(_bump_reference_revision)


_register_revision_with_notifications()
