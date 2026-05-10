"""Shared portfolio data model and transformations for CLI + web.

This module is the main entry for dashboard/CLI **reference data** and **sector
resolution**: Kite cash-equity instrument maps (:mod:`app.infrastructure.cache.kite_provider`),
NSE CSV merges (:mod:`app.infrastructure.cache.nse_provider`), and yfinance-backed labels
(:mod:`app.infrastructure.cache.yfinance_provider`), plus :mod:`app.infrastructure.cache.marketsmith_provider`
for market regime. MF metadata from mfdata.in is implemented in
:mod:`app.infrastructure.cache.mfdata_provider`.

Web code should import portfolio-facing helpers here (e.g. :mod:`app.server`) rather
than pulling from :mod:`app.cache` ad hoc, unless implementing a new provider.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import time
from typing import Any
from urllib.error import HTTPError, URLError

from app.infrastructure.cache.kite_provider import kite_reference_debug_snapshot
from app.infrastructure.cache.marketsmith_provider import (
    get_marketsmith_market_condition,
    marketsmith_market_condition_bootstrap,
    marketsmith_reference_debug_snapshot,
)
from app.infrastructure.cache.mfdata_provider import (
    flush_mfdata_disk_cache,
    mfdata_holdings_for_family,
    mfdata_reference_debug_snapshot,
    mfdata_search_fund,
    normalize_match_text as _normalize_match_text,
    rank_mfdata_variants,
)
from app.infrastructure.cache.nse_provider import nse_reference_debug_snapshot
from app.infrastructure.cache.yfinance_provider import (
    lookup_yfinance_sector_labels,
    yfinance_reference_debug_snapshot,
)
from app.domain.reference_context import WarmupContext
from app.domain.reference_notifications import notify_reference_cache_refresh
from app.domain.reference_snapshot import build_reference_snapshot, warm_reference_snapshot
from app.infrastructure.cache.reference_cache_internal import instrument_reference_lock
from app.infrastructure.cache.model_cache_store import (
    current_effective_day_ist,
    start_background_refresh_job,
)

logger = logging.getLogger(__name__)

EQUITY_EXCHANGES = {"NSE", "BSE"}
FNO_EXCHANGES = {"NFO", "BFO", "CDS", "BCD", "MCX"}

_REFERENCE_CACHE_LOCK = instrument_reference_lock


def _normalise_name(raw: Any) -> str:
    """Return a trimmed company name or empty string."""
    return str(raw or "").strip()


def _normalise_symbol(raw: Any) -> str:
    """Return a trimmed symbol in uppercase or empty string."""
    return str(raw or "").strip().upper()


def _normalise_isin(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(" ", "")


def get_reference_cache_debug_snapshot() -> dict[str, dict[str, Any]]:
    """Return cache-source/expiry metadata for dashboard timing logs."""
    now = time.time()
    with _REFERENCE_CACHE_LOCK:
        nse_rows = nse_reference_debug_snapshot(now)
        return {
            "cash_equity": kite_reference_debug_snapshot(now),
            "nse_merged_industry": nse_rows["nse_merged_industry"],
            "nifty50_symbols": nse_rows["nifty50_symbols"],
            "yfinance": yfinance_reference_debug_snapshot(now),
            "mfdata": mfdata_reference_debug_snapshot(now),
            "marketsmith": marketsmith_reference_debug_snapshot(now),
        }


def warm_reference_caches(kite=None, *, force_refresh: bool = False) -> None:
    """Best-effort warmup for heavy instrument/NSE reference lookups.

    If ``force_refresh`` is True, trigger background refresh jobs at startup
    even when existing cache entries are still within TTL.
    """
    warm_reference_snapshot(WarmupContext(kite=kite, force_refresh=force_refresh))


def resolve_equity_sector(
    symbol: str,
    exchange: str | None,
    instrument_token: int | None,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> str:
    """Resolve a display **sector** for NSE/BSE cash equities.

    Order: yfinance ``sector`` (else cached ``industry`` for the same key(s)),
    Kite ``sector`` column, then NSE CSV ``Industry`` mapped by symbol (also
    used for BSE when the symbol matches), then reference ISIN→industry maps.
    """
    clean_symbol = _normalise_symbol(symbol)
    clean_exchange = str(exchange or "").strip().upper()
    token = int(instrument_token or 0)

    if clean_exchange not in EQUITY_EXCHANGES:
        return ""

    instrument_name = ""
    if token > 0:
        instrument_name = _normalise_name(token_to_name.get(token))
    if not instrument_name and clean_symbol:
        instrument_name = _normalise_name(symbol_to_name.get((clean_exchange, clean_symbol)))

    # ETFs often lack reliable sector metadata. If symbol/name indicates ETF,
    # force a dedicated sector label.
    if "ETF" in clean_symbol or "ETF" in instrument_name.upper():
        return "ETF"

    sec = ""
    if clean_symbol:
        cached_sec, cached_ind, matched_key, matched_ind_key = lookup_yfinance_sector_labels(
            clean_exchange,
            clean_symbol,
        )

        if cached_sec:
            sec = cached_sec
            logger.debug(
                "Sector for %s resolved via yfinance cache (%s|%s): %s",
                f"{clean_exchange}:{clean_symbol}",
                matched_key[0] if matched_key else "?",
                matched_key[1] if matched_key else "?",
                sec,
            )
        elif cached_ind:
            sec = cached_ind
            logger.debug(
                "Sector for %s resolved via yfinance cache industry fallback (%s|%s): %s",
                f"{clean_exchange}:{clean_symbol}",
                matched_ind_key[0] if matched_ind_key else "?",
                matched_ind_key[1] if matched_ind_key else "?",
                sec,
            )

    if not sec and token > 0:
        sec = _normalise_name(token_to_kite_sector.get(token))
    if not sec and clean_symbol:
        sec = _normalise_name(symbol_to_kite_sector.get((clean_exchange, clean_symbol)))
    if not sec and clean_symbol:
        sec = _normalise_name(nse_symbol_to_industry.get(clean_symbol))
    if not sec:
        isin = ""
        if token > 0:
            isin = _normalise_isin(token_to_isin.get(token))
        if not isin and clean_symbol:
            isin = _normalise_isin(symbol_to_isin.get((clean_exchange, clean_symbol)))
        if not isin and clean_symbol:
            if clean_exchange == "BSE":
                isin = _normalise_isin(symbol_to_isin.get(("NSE", clean_symbol)))
            elif clean_exchange == "NSE":
                isin = _normalise_isin(symbol_to_isin.get(("BSE", clean_symbol)))
        if isin:
            sec = _normalise_name(isin_to_industry.get(isin))

    return sec


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


@dataclass
class EquityHolding:
    tradingsymbol: str
    symbol_label: str
    sector: str
    exchange: str
    instrument_token: int
    quantity: float
    average_price: float
    last_price: float
    close_price: float
    invested: float
    current: float
    pnl: float
    day_change_percentage: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MfHolding:
    fund: str
    folio: str
    units: float
    average_price: float
    last_price: float
    invested: float
    current: float
    pnl: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    tradingsymbol: str
    symbol_label: str
    sector: str
    exchange: str
    product: str
    instrument_token: int
    quantity: int
    average_price: float
    last_price: float
    buy_value: float
    sell_value: float
    multiplier: float
    close_price: float
    pnl: float
    m2m: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_equity_sector(symbol: str, sector: str) -> str:
    """Normalize sector labels and classify selected ETFs explicitly."""
    compact = "".join(ch for ch in symbol.upper() if ch.isalnum())
    normalized_sector = _normalize_match_text(sector or "")
    if compact in {"GOLDBEES", "GOLDETF", "GOLDSHARE"}:
        return "Gold"
    if compact in {"LIQUIDBEES", "LIQUIDBESS", "LIQUIDETF", "BHARATBOND"}:
        return "Debt"
    if _is_gold_sector_label(normalized_sector):
        return "Gold"
    if _is_debt_sector_label(normalized_sector):
        return "Debt"
    # Some APIs report category as plain "ETF". Reclassify common debt/gold ETF symbols.
    if normalized_sector == "etf":
        if any(token in compact for token in {"GOLD", "SILVER"}):
            return "Gold"
        if any(token in compact for token in {"LIQUID", "BOND", "GILT", "DEBT", "SDL", "TREPS"}):
            return "Debt"
    return (sector or "").strip() or "Uncategorized"


def overlay_live_ltp(row: dict[str, Any], live_ltp_by_token: dict[int, float]) -> dict[str, Any]:
    """Return row with websocket LTP overlaid when available."""
    out = dict(row)
    token = int(out.get("instrument_token") or 0)
    if token > 0 and token in live_ltp_by_token:
        out["last_price"] = float(live_ltp_by_token[token])
        out["_live_ltp_applied"] = True
    return out


def build_equity_holding(
    holding: dict[str, Any],
    *,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> EquityHolding:
    quantity = (holding.get("quantity") or 0) + (holding.get("t1_quantity") or 0)
    avg = float(holding.get("average_price") or 0.0)
    ltp = float(holding.get("last_price") or 0.0)
    close_price = float(holding.get("close_price") or 0.0)
    live_ltp_applied = bool(holding.get("_live_ltp_applied"))
    if live_ltp_applied and close_price > 0:
        day_change_percentage = ((ltp - close_price) / close_price) * 100.0
        pnl = (ltp - avg) * quantity
    else:
        day_change_percentage = float(holding.get("day_change_percentage") or 0.0)
        pnl = float(holding.get("pnl") or 0.0)

    symbol = str(holding.get("tradingsymbol", "")).strip()
    exchange = str(holding.get("exchange", ""))
    token = int(holding.get("instrument_token") or 0)
    symbol_label = symbol_with_company_name(
        symbol=symbol,
        exchange=exchange,
        instrument_token=token,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
    )
    sector = normalize_equity_sector(
        symbol,
        resolve_equity_sector(
            symbol=symbol,
            exchange=exchange,
            instrument_token=token,
            token_to_name=token_to_name,
            symbol_to_name=symbol_to_name,
            token_to_kite_sector=token_to_kite_sector,
            symbol_to_kite_sector=symbol_to_kite_sector,
            nse_symbol_to_industry=nse_symbol_to_industry,
            isin_to_industry=isin_to_industry,
            token_to_isin=token_to_isin,
            symbol_to_isin=symbol_to_isin,
        ),
    )
    return EquityHolding(
        tradingsymbol=symbol,
        symbol_label=symbol_label,
        sector=sector,
        exchange=exchange,
        instrument_token=token,
        quantity=quantity,
        average_price=avg,
        last_price=ltp,
        close_price=close_price,
        invested=avg * quantity,
        current=ltp * quantity,
        pnl=pnl,
        day_change_percentage=day_change_percentage,
    )


def build_mf_holding(holding: dict[str, Any]) -> MfHolding:
    units = float(holding.get("quantity") or 0.0)
    avg = float(holding.get("average_price") or 0.0)
    ltp = float(holding.get("last_price") or 0.0)
    invested = avg * units
    current = ltp * units
    api_pnl = holding.get("pnl")
    pnl = float(api_pnl) if api_pnl not in (None, "") and float(api_pnl) != 0.0 else (current - invested)
    return MfHolding(
        fund=str(holding.get("fund", "")),
        folio=str(holding.get("folio", "")),
        units=units,
        average_price=avg,
        last_price=ltp,
        invested=invested,
        current=current,
        pnl=pnl,
    )


def build_position(
    position: dict[str, Any],
    *,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> Position:
    qty = int(position.get("quantity") or 0)
    ltp = float(position.get("last_price") or 0.0)
    close_price = float(position.get("close_price") or 0.0)
    multiplier = float(position.get("multiplier") or 1.0)
    live_ltp_applied = bool(position.get("_live_ltp_applied"))
    if live_ltp_applied:
        buy_value = float(position.get("buy_value") or 0.0)
        sell_value = float(position.get("sell_value") or 0.0)
        pnl = (sell_value - buy_value) + (qty * ltp * multiplier)
        m2m = (ltp - close_price) * qty * multiplier if close_price > 0 else float(position.get("m2m") or 0.0)
    else:
        pnl = float(position.get("pnl") or 0.0)
        m2m = float(position.get("m2m") or 0.0)

    symbol = str(position.get("tradingsymbol", "")).strip()
    exchange = str(position.get("exchange", ""))
    token = int(position.get("instrument_token") or 0)
    symbol_label = symbol_with_company_name(
        symbol=symbol,
        exchange=exchange,
        instrument_token=token,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
    )
    if exchange in EQUITY_EXCHANGES:
        sector = normalize_equity_sector(
            symbol,
            resolve_equity_sector(
                symbol=symbol,
                exchange=exchange,
                instrument_token=token,
                token_to_name=token_to_name,
                symbol_to_name=symbol_to_name,
                token_to_kite_sector=token_to_kite_sector,
                symbol_to_kite_sector=symbol_to_kite_sector,
                nse_symbol_to_industry=nse_symbol_to_industry,
                isin_to_industry=isin_to_industry,
                token_to_isin=token_to_isin,
                symbol_to_isin=symbol_to_isin,
            ),
        )
    else:
        sector = ""

    return Position(
        tradingsymbol=symbol,
        symbol_label=symbol_label,
        sector=sector,
        exchange=exchange,
        product=str(position.get("product", "")),
        instrument_token=token,
        quantity=qty,
        average_price=float(position.get("average_price") or 0.0),
        last_price=ltp,
        buy_value=float(position.get("buy_value") or 0.0),
        sell_value=float(position.get("sell_value") or 0.0),
        multiplier=multiplier,
        close_price=close_price,
        pnl=pnl,
        m2m=m2m,
    )


def summarise(rows: list[dict[str, Any]], *fields: str) -> dict[str, float]:
    return {field: sum(float(r.get(field) or 0.0) for r in rows) for field in fields}


def summarise_equity_by_sector(rows: list[dict[str, Any]]) -> list[dict[str, float | str]]:
    bucket: dict[str, dict[str, float | str]] = {}
    for row in rows:
        sector = str(row.get("sector") or "Uncategorized").strip() or "Uncategorized"
        entry = bucket.setdefault(
            sector,
            {"sector": sector, "invested": 0.0, "current": 0.0, "pnl": 0.0},
        )
        entry["invested"] = float(entry["invested"]) + float(row.get("invested") or 0.0)
        entry["current"] = float(entry["current"]) + float(row.get("current") or 0.0)
        entry["pnl"] = float(entry["pnl"]) + float(row.get("pnl") or 0.0)
    return sorted(bucket.values(), key=lambda r: float(r["invested"]), reverse=True)


def _is_debt_sector_label(normalized_label: str) -> bool:
    label = str(normalized_label or "").strip()
    if not label:
        return False
    debt_markers = (
        "debt",
        "bond",
        "gilt",
        "liquid",
        "money market",
        "fixed income",
        "overnight",
        "treasury",
        "t bill",
        "tbill",
        "sdl",
    )
    return any(marker in label for marker in debt_markers)


def _is_gold_sector_label(normalized_label: str) -> bool:
    label = str(normalized_label or "").strip()
    if not label:
        return False
    # Keep this strict to avoid classifying ordinary equities by symbol/company names.
    if label == "gold":
        return True
    if "gold" in label and ("etf" in label or "commodity" in label or "precious" in label):
        return True
    return False


def split_top_level_allocations(
    sector_rows: list[dict[str, float | str]],
) -> list[dict[str, float | str]]:
    debt = {"sector": "Debt", "invested": 0.0, "current": 0.0, "pnl": 0.0}
    gold = {"sector": "Gold", "invested": 0.0, "current": 0.0, "pnl": 0.0}
    equity = {"sector": "Equity", "invested": 0.0, "current": 0.0, "pnl": 0.0}
    for row in sector_rows:
        key = _normalize_match_text(str(row.get("sector") or ""))
        if _is_debt_sector_label(key):
            debt["invested"] = float(debt["invested"]) + float(row.get("invested") or 0.0)
            debt["current"] = float(debt["current"]) + float(row.get("current") or 0.0)
            debt["pnl"] = float(debt["pnl"]) + float(row.get("pnl") or 0.0)
        elif _is_gold_sector_label(key):
            gold["invested"] = float(gold["invested"]) + float(row.get("invested") or 0.0)
            gold["current"] = float(gold["current"]) + float(row.get("current") or 0.0)
            gold["pnl"] = float(gold["pnl"]) + float(row.get("pnl") or 0.0)
        else:
            equity["invested"] = float(equity["invested"]) + float(row.get("invested") or 0.0)
            equity["current"] = float(equity["current"]) + float(row.get("current") or 0.0)
            equity["pnl"] = float(equity["pnl"]) + float(row.get("pnl") or 0.0)
    return [debt, gold, equity]


def equity_sector_breakdown(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, float | str]]]:
    sector_rows = summarise_equity_by_sector(rows)
    top_level = split_top_level_allocations(sector_rows)
    equity_subsectors = [
        r
        for r in sector_rows
        if not _is_debt_sector_label(_normalize_match_text(str(r.get("sector") or "")))
        and not _is_gold_sector_label(_normalize_match_text(str(r.get("sector") or "")))
    ]
    return {"top_level": top_level, "equity_subsectors": equity_subsectors}


def _parse_pct(value: Any) -> float:
    if value is None:
        return 0.0
    raw = str(value).strip().replace("%", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def build_mf_underlying_breakdown(
    mf_holdings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, list[str], int, int]:
    if not mf_holdings:
        return [], "", [], 0, 0
    fund_current_by_name: dict[str, float] = {}
    for row in mf_holdings:
        fund_name = str(row.get("fund") or "").strip()
        if not fund_name:
            continue
        fund_current_by_name[fund_name] = fund_current_by_name.get(fund_name, 0.0) + float(row.get("current") or 0.0)
    total_current = sum(fund_current_by_name.values())
    if total_current <= 0:
        return [], "", [], 0, len(fund_current_by_name)

    combined: dict[tuple[str, str], float] = {}
    used_months: set[str] = set()
    not_aggregated: list[str] = []
    aggregated_funds: set[str] = set()
    all_funds: set[str] = set(fund_current_by_name)

    for fund_name, fund_current in fund_current_by_name.items():
        if fund_current <= 0:
            not_aggregated.append(fund_name)
            continue
        variants = mfdata_search_fund(fund_name)
        family_candidates = rank_mfdata_variants(fund_name, variants)
        selected_holdings: dict[str, Any] | None = None
        for family_id in family_candidates:
            try:
                payload = mfdata_holdings_for_family(family_id)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                payload = None
            equity_rows = (payload or {}).get("equity_holdings") if isinstance(payload, dict) else []
            if equity_rows:
                selected_holdings = payload
                break
        if not selected_holdings:
            not_aggregated.append(fund_name)
            continue
        month = str(selected_holdings.get("month") or "").strip()
        if month:
            used_months.add(month)
        fund_weight = fund_current / total_current
        for row in selected_holdings.get("equity_holdings") or []:
            instrument = str(row.get("stock_name") or row.get("isin") or "").strip() or "Unknown"
            sector = str(row.get("sector") or "Unspecified").strip() or "Unspecified"
            instrument_weight = max(0.0, _parse_pct(row.get("weight_pct") or row.get("weight")))
            overall_weight = fund_weight * (instrument_weight / 100.0)
            if overall_weight <= 0:
                continue
            key = (instrument, sector)
            combined[key] = combined.get(key, 0.0) + overall_weight
        aggregated_funds.add(fund_name)

    table_rows = [
        {"instrument": instrument, "sector": sector, "overall_weight": weight * 100.0}
        for (instrument, sector), weight in combined.items()
    ]
    table_rows.sort(
        key=lambda row: (float(row.get("overall_weight") or 0.0), str(row.get("instrument") or "").lower()),
        reverse=True,
    )
    sorted_months = sorted(used_months, reverse=True)
    latest_month = sorted_months[0] if sorted_months else ""
    seen_missing: set[str] = set()
    missing_unique = [name for name in not_aggregated if not (name in seen_missing or seen_missing.add(name))]
    if flush_mfdata_disk_cache():
        notify_reference_cache_refresh()
    return table_rows, latest_month, missing_unique, len(aggregated_funds), len(all_funds)


__all__ = [
    "EQUITY_EXCHANGES",
    "FNO_EXCHANGES",
    "EquityHolding",
    "MfHolding",
    "Position",
    "build_equity_holding",
    "build_mf_holding",
    "build_mf_underlying_breakdown",
    "build_position",
    "build_reference_snapshot",
    "current_effective_day_ist",
    "equity_sector_breakdown",
    "get_marketsmith_market_condition",
    "get_reference_cache_debug_snapshot",
    "marketsmith_market_condition_bootstrap",
    "normalize_equity_sector",
    "overlay_live_ltp",
    "resolve_equity_sector",
    "start_background_refresh_job",
    "summarise",
    "summarise_equity_by_sector",
    "symbol_with_company_name",
    "warm_reference_caches",
]
