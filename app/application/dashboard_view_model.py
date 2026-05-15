"""Build the dashboard template context (holdings, watchlist, summaries)."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from kiteconnect import KiteConnect
from kiteconnect.exceptions import PermissionException, TokenException

from app.infrastructure.auth import load_cached_access_token, load_credentials
from app.infrastructure.live_prices import live_price_stream
from app.domain.portfolio_model import (
    EQUITY_EXCHANGES,
    FNO_EXCHANGES,
    build_equity_holding,
    build_position,
    build_reference_snapshot,
    equity_sector_breakdown,
    get_marketsmith_market_condition,
    get_reference_cache_debug_snapshot,
    marketsmith_market_condition_bootstrap,
    overlay_live_ltp,
    resolve_equity_sector,
    summarise,
    summarise_equity_by_sector,
)
from app.presentation.http.server_auth import authorized_browser_or_api, kite_for_request, restore_session_if_token_valid
from app.presentation.http.server_config import (
    DASHBOARD_DISPLAY_NAME,
    DASHBOARD_INDEX_ENTRIES,
    DASHBOARD_SNAPSHOT_INTERVAL_MS,
    dashboard_timing_logger,
)
from app.infrastructure.services.dashboard_caches import get_cached_profile, get_cached_quotes
from app.infrastructure.cache.nse_provider import get_nifty50_symbols

logger = logging.getLogger(__name__)
_DASHBOARD_TIMING_LOGGER = dashboard_timing_logger()

KITE_PORTFOLIO_PERMISSION_USER_MESSAGE = (
    "Zerodha rejected portfolio API calls (Insufficient permission). "
    "In https://developers.kite.trade open your Kite Connect app and enable the "
    "permissions needed to read profile, holdings, positions, and margins "
    "(wording varies by console version). Save the app, then use Logout here and "
    "sign in again."
)


def dashboard_timing_mark(
    timings: list[tuple[str, float]],
    stage: str,
    start_time: float,
) -> None:
    """Append elapsed milliseconds since ``start_time`` for one stage."""
    timings.append((stage, (time.perf_counter() - start_time) * 1000.0))


def decorate_holding(
    h: dict,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> dict:
    """Enrich a Kite holdings entry with derived fields used by the template."""
    model = build_equity_holding(
        h,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
        token_to_kite_sector=token_to_kite_sector,
        symbol_to_kite_sector=symbol_to_kite_sector,
        nse_symbol_to_industry=nse_symbol_to_industry,
        isin_to_industry=isin_to_industry,
        token_to_isin=token_to_isin,
        symbol_to_isin=symbol_to_isin,
    )
    return model.to_dict()


def decorate_position(
    p: dict,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> dict:
    """Enrich a Kite positions entry."""
    model = build_position(
        p,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
        token_to_kite_sector=token_to_kite_sector,
        symbol_to_kite_sector=symbol_to_kite_sector,
        nse_symbol_to_industry=nse_symbol_to_industry,
        isin_to_industry=isin_to_industry,
        token_to_isin=token_to_isin,
        symbol_to_isin=symbol_to_isin,
    )
    return model.to_dict()


# Maximum chunk sizes (days) per Kite historical API interval
_INTERVAL_CHUNK_DAYS: dict[str, int] = {
    "minute": 60,
    "3minute": 60,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}

# Allowed Kite interval strings
ALLOWED_KITE_INTERVALS = frozenset(_INTERVAL_CHUNK_DAYS.keys())

# Default max history days per interval (clamped at request time)
_INTERVAL_MAX_DAYS: dict[str, int] = {
    "minute": 60,
    "3minute": 60,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 3650,
}


def historical_candles_for_stock(
    kite: KiteConnect,
    instrument_token: int,
    days: int,
    interval: str = "day",
) -> list[dict[str, Any]]:
    """OHLCV candles via ``KiteConnect.historical_data``.

    Supports all Kite intervals (minute, 5minute, 15minute, 60minute, day).
    Chunks requests correctly per Kite's per-interval date range limits.
    Returns ISO-8601 datetime strings (with time for intraday, date-only for daily).
    """
    if days <= 0:
        return []

    kite_interval = interval if interval in ALLOWED_KITE_INTERVALS else "day"
    chunk_days = _INTERVAL_CHUNK_DAYS.get(kite_interval, 2000)
    is_intraday = kite_interval != "day"

    end = datetime.now()
    overall_start = end - timedelta(days=days)
    merged: dict[str, dict[str, Any]] = {}

    cursor_end = end
    max_iters = (days // max(chunk_days, 1)) + 20
    iters = 0
    while cursor_end > overall_start and iters < max_iters:
        iters += 1
        cursor_start = max(overall_start, cursor_end - timedelta(days=chunk_days))
        rows = kite.historical_data(
            instrument_token,
            cursor_start,
            cursor_end,
            kite_interval,
            continuous=False,
            oi=False,
        )
        for row in rows or []:
            dt = row["date"]
            if hasattr(dt, "strftime"):
                if is_intraday:
                    # Return full IST ISO-8601 string for intraday bars
                    date_key = dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")
                else:
                    date_key = dt.strftime("%Y-%m-%d")
            else:
                date_key = str(dt)[:19] if is_intraday else str(dt)[:10]

            merged[date_key] = {
                "date": date_key,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            }
        cursor_end = cursor_start

    return sorted(merged.values(), key=lambda r: r["date"])


async def build_dashboard_view_model(
    request: Request,
    *,
    allow_bearer: bool = False,
) -> tuple[dict[str, Any] | None, RedirectResponse | None]:
    """Build dashboard template context; returns ``(None, redirect)`` if unauthorized."""
    request_start = time.perf_counter()
    timings: list[tuple[str, float]] = []
    authorized = (
        authorized_browser_or_api(request)
        if allow_bearer
        else restore_session_if_token_valid(request)
    )
    if not authorized:
        return None, RedirectResponse("/", status_code=303)
    dashboard_timing_mark(timings, "session_restore", request_start)

    kite = kite_for_request()
    if kite is None:
        request.session.clear()
        return None, RedirectResponse("/", status_code=303)
    dashboard_timing_mark(timings, "kite_client", request_start)

    index_quote_keys = [f"NSE:{ts}" for _, ts in DASHBOARD_INDEX_ENTRIES]
    nifty50_symbols = list(get_nifty50_symbols())
    watch_quote_keys = [f"NSE:{sym}" for sym in nifty50_symbols]
    quote_keys = index_quote_keys + watch_quote_keys

    mf_error: str | None = None
    kite_api_permission_error: str | None = None
    with ThreadPoolExecutor(max_workers=6) as pool:
        future_equity = pool.submit(kite.holdings)
        future_positions = pool.submit(kite.positions)
        future_margins = pool.submit(kite.margins, "equity")
        future_market_condition = pool.submit(get_marketsmith_market_condition)
        future_profile = pool.submit(get_cached_profile, kite)
        future_quotes = pool.submit(get_cached_quotes, kite, quote_keys)

        try:
            equity_raw = future_equity.result() or []
            positions_raw = future_positions.result() or {}
            margins_raw = future_margins.result() or {}
        except TokenException:
            request.session.clear()
            live_price_stream.close()
            return None, RedirectResponse("/", status_code=303)
        except PermissionException as exc:
            logger.warning("Kite portfolio snapshot permission denied: %s", exc)
            kite_api_permission_error = KITE_PORTFOLIO_PERMISSION_USER_MESSAGE
            equity_raw = []
            positions_raw = {"net": []}
            margins_raw = {}

        profile_raw = future_profile.result() or {}
        market_condition = future_market_condition.result()
        quote_batch = future_quotes.result() or {}
    dashboard_timing_mark(timings, "kite_data_fetch_parallel", request_start)

    net_positions = positions_raw.get("net", []) or []
    open_net = [p for p in net_positions if int(p.get("quantity") or 0) != 0]

    live_ltp_by_token: dict[int, float] = {}
    access_token = load_cached_access_token()
    index_tokens: set[int] = set()
    index_quotes_bootstrap: list[dict[str, Any]] = []

    ref_snap = build_reference_snapshot(kite, market_condition=market_condition)
    equity_token_to_name = ref_snap.kite.token_to_name
    equity_symbol_to_name = ref_snap.kite.symbol_to_name
    equity_token_to_kite_sector = ref_snap.kite.token_to_kite_sector
    equity_symbol_to_kite_sector = ref_snap.kite.symbol_to_kite_sector
    equity_token_to_isin = ref_snap.kite.token_to_isin
    equity_symbol_to_isin = ref_snap.kite.symbol_to_isin
    nse_symbol_to_industry = ref_snap.nse.symbol_to_industry
    isin_to_industry = ref_snap.nse.isin_to_industry
    nse_symbol_to_token = ref_snap.kite.nse_symbol_to_token
    reference_cache_debug = get_reference_cache_debug_snapshot()
    dashboard_timing_mark(timings, "instrument_and_reference_lookups", request_start)
    watch_tokens = {
        int(nse_symbol_to_token.get(sym) or 0)
        for sym in nifty50_symbols
        if int(nse_symbol_to_token.get(sym) or 0) > 0
    }

    dashboard_timing_mark(timings, "quote_batch", request_start)

    for env_label, ts in DASHBOARD_INDEX_ENTRIES:
        key = f"NSE:{ts}"
        data = quote_batch.get(key) or {}
        token = int(data.get("instrument_token") or 0)
        if token > 0:
            index_tokens.add(token)
        ohlc = data.get("ohlc") or {}
        raw_prev = ohlc.get("close")
        prev_close: float | None = None
        if raw_prev is not None:
            try:
                prev_close = float(raw_prev)
            except (TypeError, ValueError):
                prev_close = None
        raw_ltp = data.get("last_price")
        rest_ltp: float | None = None
        if raw_ltp is not None:
            try:
                rest_ltp = float(raw_ltp)
            except (TypeError, ValueError):
                rest_ltp = None
        display = str(data.get("tradingsymbol") or ts or env_label)
        index_quotes_bootstrap.append(
            {
                "label": display,
                "token": token,
                "ltp": rest_ltp,
                "prevClose": prev_close,
            }
        )

    if access_token:
        try:
            api_key, _ = load_credentials()
            live_price_stream.ensure_running(api_key, access_token)
            tokens = {
                int(h.get("instrument_token") or 0)
                for h in equity_raw
            } | {
                int(p.get("instrument_token") or 0)
                for p in open_net
            }
            tokens |= index_tokens
            tokens |= watch_tokens
            tokens = {t for t in tokens if t > 0}
            live_price_stream.subscribe(tokens)
            live_ltp_by_token = live_price_stream.snapshot_ltp(tokens)
        except Exception:
            live_ltp_by_token = {}
    dashboard_timing_mark(timings, "live_price_stream_bootstrap", request_start)

    for row in index_quotes_bootstrap:
        tok = int(row.get("token") or 0)
        if tok > 0 and tok in live_ltp_by_token:
            row["ltp"] = live_ltp_by_token[tok]

    equity_holdings = sorted(
        (
            decorate_holding(
                overlay_live_ltp(h, live_ltp_by_token),
                equity_token_to_name,
                equity_symbol_to_name,
                equity_token_to_kite_sector,
                equity_symbol_to_kite_sector,
                nse_symbol_to_industry,
                isin_to_industry,
                equity_token_to_isin,
                equity_symbol_to_isin,
            )
            for h in equity_raw
        ),
        key=lambda r: r["tradingsymbol"],
    )
    mf_holdings: list[dict[str, Any]] = []

    equity_positions = sorted(
        (
            decorate_position(
                overlay_live_ltp(p, live_ltp_by_token),
                equity_token_to_name,
                equity_symbol_to_name,
                equity_token_to_kite_sector,
                equity_symbol_to_kite_sector,
                nse_symbol_to_industry,
                isin_to_industry,
                equity_token_to_isin,
                equity_symbol_to_isin,
            )
            for p in open_net
            if p.get("exchange") in EQUITY_EXCHANGES
        ),
        key=lambda r: r["tradingsymbol"],
    )
    fno_positions = sorted(
        (
            decorate_position(
                overlay_live_ltp(p, live_ltp_by_token),
                equity_token_to_name,
                equity_symbol_to_name,
                equity_token_to_kite_sector,
                equity_symbol_to_kite_sector,
                nse_symbol_to_industry,
                isin_to_industry,
                equity_token_to_isin,
                equity_symbol_to_isin,
            )
            for p in open_net
            if p.get("exchange") in FNO_EXCHANGES
        ),
        key=lambda r: r["tradingsymbol"],
    )
    dashboard_timing_mark(timings, "decorate_holdings_positions", request_start)

    available = margins_raw.get("available", {}) or {}
    utilised = margins_raw.get("utilised", {}) or {}
    cash = {
        "available_cash": float(available.get("cash") or 0.0),
        "live_balance": float(available.get("live_balance") or 0.0),
        "utilised": float(utilised.get("debits") or 0.0),
    }

    equity_totals = summarise(equity_holdings, "invested", "current", "pnl")
    equity_sector_info = equity_sector_breakdown(equity_holdings)
    equity_all_sector_summary = summarise_equity_by_sector(equity_holdings)
    mf_totals = {"invested": 0.0, "current": 0.0, "pnl": 0.0}
    equity_position_totals = summarise(equity_positions, "pnl", "m2m")
    fno_position_totals = summarise(fno_positions, "pnl", "m2m")

    total_invested = equity_totals["invested"] + mf_totals["invested"]
    total_current = equity_totals["current"] + mf_totals["current"]
    holdings_pnl = equity_totals["pnl"] + mf_totals["pnl"]
    positions_pnl = equity_position_totals["pnl"] + fno_position_totals["pnl"]
    overall_pnl = holdings_pnl + positions_pnl
    user_profile = {
        "name": str(
            profile_raw.get("user_name")
            or profile_raw.get("user_shortname")
            or DASHBOARD_DISPLAY_NAME
        ),
        "user_id": str(profile_raw.get("user_id") or "--"),
        "email": str(profile_raw.get("email") or "--"),
        "broker": str(profile_raw.get("broker") or "Zerodha"),
        "user_type": str(profile_raw.get("user_type") or "--"),
        "products": list(profile_raw.get("products") or []),
        "exchanges": list(profile_raw.get("exchanges") or []),
    }

    watch_list: list[dict[str, Any]] = []
    for symbol in nifty50_symbols:
        qkey = f"NSE:{symbol}"
        qrow = quote_batch.get(qkey) or {}
        qtoken = int(qrow.get("instrument_token") or 0)
        token = qtoken if qtoken > 0 else int(nse_symbol_to_token.get(symbol) or 0)
        ohlc = qrow.get("ohlc") or {}
        raw_prev = ohlc.get("close")
        raw_ltp = qrow.get("last_price")
        try:
            prev_close = float(raw_prev) if raw_prev is not None else None
        except (TypeError, ValueError):
            prev_close = None
        try:
            last_price = float(raw_ltp) if raw_ltp is not None else None
        except (TypeError, ValueError):
            last_price = None
        if token > 0 and token in live_ltp_by_token:
            last_price = float(live_ltp_by_token[token])

        if (
            last_price is not None
            and prev_close is not None
            and prev_close > 0
        ):
            change = last_price - prev_close
            change_pct = (change / prev_close) * 100.0
        else:
            change = 0.0
            change_pct = 0.0

        company_name = str(equity_symbol_to_name.get(("NSE", symbol)) or "").strip()
        label = company_name or symbol
        sector = resolve_equity_sector(
            symbol=symbol,
            exchange="NSE",
            instrument_token=token,
            token_to_name=equity_token_to_name,
            symbol_to_name=equity_symbol_to_name,
            token_to_kite_sector=equity_token_to_kite_sector,
            symbol_to_kite_sector=equity_symbol_to_kite_sector,
            nse_symbol_to_industry=nse_symbol_to_industry,
            isin_to_industry=isin_to_industry,
            token_to_isin=equity_token_to_isin,
            symbol_to_isin=equity_symbol_to_isin,
        )

        watch_list.append(
            {
                "label": label,
                "symbol": symbol,
                "sector": sector,
                "segment": "Equity",
                "instrument_token": token,
                "prev_close": prev_close,
                "last_price": last_price,
                "change": change,
                "change_pct": change_pct,
            }
        )
    dashboard_timing_mark(timings, "watchlist_build", request_start)

    dashboard_bootstrap = {
        "mfTotals": {
            "invested": mf_totals["invested"],
            "current": mf_totals["current"],
            "pnl": mf_totals["pnl"],
        },
        "portfolioInvestedTotal": total_invested,
        "equityInvestedTotal": equity_totals["invested"],
        "indexQuotes": index_quotes_bootstrap,
        "marketCondition": marketsmith_market_condition_bootstrap(market_condition),
        "referenceCache": reference_cache_debug,
    }
    context = {
        "dashboard_name": DASHBOARD_DISPLAY_NAME,
        "kite_api_permission_error": kite_api_permission_error,
        "equity_holdings": equity_holdings,
        "equity_totals": equity_totals,
        "equity_sector_summary": equity_sector_info["top_level"],
        "equity_subsector_summary": equity_sector_info["equity_subsectors"],
        "equity_all_sector_summary": equity_all_sector_summary,
        "mf_holdings": mf_holdings,
        "mf_totals": mf_totals,
        "mf_error": mf_error,
        "equity_positions": equity_positions,
        "equity_position_totals": equity_position_totals,
        "fno_positions": fno_positions,
        "fno_position_totals": fno_position_totals,
        "cash": cash,
        "snapshot_interval_ms": DASHBOARD_SNAPSHOT_INTERVAL_MS,
        "dashboard_bootstrap_json": json.dumps(dashboard_bootstrap),
        "portfolio_summary": {
            "total_invested": total_invested,
            "total_current": total_current,
            "holdings_pnl": holdings_pnl,
            "positions_pnl": positions_pnl,
            "overall_pnl": overall_pnl,
        },
        "user_profile": user_profile,
        "watch_list": watch_list,
        "market_condition": market_condition,
    }
    dashboard_timing_mark(timings, "context_build", request_start)
    total_ms = (time.perf_counter() - request_start) * 1000.0
    timings_str = ", ".join(f"{name}={ms:.1f}ms" for name, ms in timings)
    reference_cache_str = ", ".join(
        (
            f"{name}:source={meta.get('source','unknown')}"
            f"/expires_in_ms={float(meta.get('expires_in_ms') or 0.0):.1f}"
            f"/refreshing={bool(meta.get('refresh_in_progress'))}"
        )
        for name, meta in reference_cache_debug.items()
    )
    logger.info(
        "dashboard timing total=%.1fms | %s | reference_cache=%s",
        total_ms,
        timings_str,
        reference_cache_str,
    )
    _DASHBOARD_TIMING_LOGGER.info(
        "dashboard timing total=%.1fms | %s | reference_cache=%s",
        total_ms,
        timings_str,
        reference_cache_str,
    )
    return context, None
