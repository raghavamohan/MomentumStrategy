"""Dashboard HTML, lazy-load JSON, and stock chart/history."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from kiteconnect.exceptions import KiteException, TokenException

from app.application.dashboard_view_model import build_dashboard_view_model, historical_candles_for_stock
from app.presentation.http.jinja_env import KITE_STOCK_HISTORY_HELP_URLS, stock_history_json_error, templates
from app.infrastructure.live_prices import live_price_stream
from app.domain.portfolio_model import EQUITY_EXCHANGES
from app.presentation.http.server_auth import authorized_browser_or_api, kite_for_request
from app.presentation.http.server_config import DASHBOARD_DISPLAY_NAME
from app.infrastructure.services.dashboard_caches import get_cached_mf_holdings_payload, get_cached_mf_underlyings_payload

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the tabbed dashboard with all account snapshot sections."""
    context, redirect = await build_dashboard_view_model(request, allow_bearer=False)
    if redirect is not None:
        return redirect
    assert context is not None
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/mf-underlyings")
async def dashboard_mf_underlyings(
    request: Request,
    tone: str = "all",
) -> JSONResponse:
    """Return MF underlying aggregation as JSON (loaded lazily by the UI)."""
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    kite = kite_for_request()
    if kite is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = get_cached_mf_underlyings_payload(kite, tone=tone)
    except TokenException:
        request.session.clear()
        live_price_stream.close()
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(payload)


@router.get("/dashboard/mf-holdings")
async def dashboard_mf_holdings(request: Request) -> JSONResponse:
    """Return mutual fund holdings/totals as JSON (loaded lazily by MF tab)."""
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    kite = kite_for_request()
    if kite is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = get_cached_mf_holdings_payload(kite)
    except TokenException:
        request.session.clear()
        live_price_stream.close()
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(payload)


@router.get(
    "/dashboard/stock-chart",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_stock_chart(
    request: Request,
    instrument_token: int = Query(..., ge=1),
    exchange: str = Query("NSE"),
    label: str = Query(""),
    days: int = Query(3650, ge=1, le=3650),
    ref: str = Query(""),
):
    """Full-page candlestick + volume chart for one cash equity instrument."""
    if not authorized_browser_or_api(request):
        return RedirectResponse("/", status_code=303)

    if kite_for_request() is None:
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    ex = exchange.strip().upper()
    if ex not in EQUITY_EXCHANGES:
        return RedirectResponse("/dashboard", status_code=303)

    days_clamped = max(365, min(int(days), 365 * 10))
    display_label = (label or "").strip() or f"{ex} #{instrument_token}"
    bootstrap = {
        "instrumentToken": instrument_token,
        "exchange": ex,
        "label": display_label,
        "days": days_clamped,
    }
    ref_clean = (ref or "").strip().lower()
    if ref_clean == "watchlist":
        bootstrap["focusContext"] = "watchlist"
    elif ref_clean in ("equity_holding", "holdings"):
        bootstrap["focusContext"] = "equity_holding"

    return templates.TemplateResponse(
        request,
        "stock_chart.html",
        {
            "dashboard_name": DASHBOARD_DISPLAY_NAME,
            "stock_title": display_label,
            "stock_chart_bootstrap_json": json.dumps(bootstrap),
            "kite_help_urls": list(KITE_STOCK_HISTORY_HELP_URLS),
        },
    )


@router.get("/dashboard/stock-history")
async def dashboard_stock_history(
    request: Request,
    instrument_token: int,
    exchange: str = "NSE",
    days: int = 3650,
) -> JSONResponse:
    """Return daily historical candles for one equity instrument (OHLCV)."""
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    kite = kite_for_request()
    if kite is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if instrument_token <= 0:
        return stock_history_json_error("invalid instrument_token", status_code=400)

    ex = exchange.strip().upper()
    if ex not in EQUITY_EXCHANGES:
        return stock_history_json_error("invalid exchange", status_code=400)

    days_clamped = max(365, min(int(days), 365 * 10))

    try:
        candles = await asyncio.to_thread(
            historical_candles_for_stock,
            kite,
            instrument_token,
            days_clamped,
        )
    except TokenException:
        request.session.clear()
        live_price_stream.close()
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    except KiteException as exc:
        logger.warning("stock-history Kite error: %s", exc)
        return stock_history_json_error(str(exc), status_code=400)
    except Exception as exc:  # noqa: BLE001 - surface unexpected Kite/network issues
        logger.exception("stock-history failed: %s", exc)
        return stock_history_json_error(
            "Failed to load historical data.",
            status_code=500,
        )

    return JSONResponse(
        {
            "instrument_token": instrument_token,
            "exchange": ex,
            "interval": "day",
            "days": days_clamped,
            "candles": candles,
        }
    )
