"""HTTP/WebSocket server for the Zerodha portfolio dashboard and JSON API.

Run with::

    python -m app.server
    # or:
    uvicorn app.server:app --host 127.0.0.1 --port 5000

``python -m app.server`` starts Uvicorn and opens the dashboard URL in your
default browser. ``python -m app.web`` remains a compatibility shim for the
same ASGI ``app``.

Routes
------
``GET /``
    Landing page. If the session is authenticated (or the on-disk Kite
    access token is still valid—same cache as the CLI—restores the
    session and redirects to ``/dashboard``); otherwise shows a "Login
    with Zerodha" button.

``GET /login``
    Redirects the browser to Kite's login URL (``KiteConnect.login_url()``).
    See https://kite.trade/docs/connect/v3/user/#login-flow

``GET /callback``
    Receives Zerodha's redirect carrying the one-time ``request_token``,
    exchanges it for an ``access_token`` via
    `KiteConnect.generate_session <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.generate_session>`_,
    caches the access token to disk, marks the session authenticated,
    and redirects to ``/dashboard``.

    The Kite Connect app's **Redirect URL** must be set to
    ``http://127.0.0.1:5000/callback`` (or whatever host:port this app
    is bound to) at https://developers.kite.trade.

``GET /dashboard``
    Authenticated. Fetches holdings, mutual funds (optional module),
    open positions, cash margins, profile data, and watch-list quote
    data; then renders ``templates/dashboard.html``.

    The page has three section views:
    * Profile
    * Portfolio (with in-page tabs for Equity Holdings, Mutual Funds,
      Equity Positions, F&O Positions, Cash Balance)
    * Watch List (Nifty 50 constituents with live updates)

    Kite endpoints called per request:

    * ``KiteConnect.holdings()``     -> ``GET /portfolio/holdings``
    * ``KiteConnect.mf_holdings()``  -> ``GET /mf/holdings`` (optional;
      ``PermissionException`` is caught and surfaced as a notice).
    * ``KiteConnect.positions()``    -> ``GET /portfolio/positions``
    * ``KiteConnect.margins("equity")`` -> ``GET /user/margins/equity``
    * ``KiteTicker`` (WebSocket)     -> ``wss://ws.kite.trade`` for live
      LTP snapshots on equity/F&O instrument tokens used in the current view.
    * ``KiteConnect.quote`` (NSE indices) -> previous close and instrument tokens
      for header index tickers from ``KITE_DASHBOARD_INDICES`` (defaults to
      NIFTY 50, NIFTY BANK, NIFTY IT, NIFTY FIN SERVICE, NIFTY METAL).
    * MarketSmith India ``getMarketHistory.json`` (once per calendar day;
      memory + disk cache in :mod:`app.domain.portfolio_model`) -> current market
      regime banner and ``dashboard-bootstrap.marketCondition``.

    Live LTP updates are pushed to the browser over ``WS /ws/live-prices``
    (fed by the existing KiteTicker stream). A separate **slow** full-page
    snapshot uses ``GET /dashboard`` on an interval controlled by
    ``DASHBOARD_SNAPSHOT_SECONDS`` (defaults to 120s, minimum 10s); see also
    legacy ``DASHBOARD_REFRESH_SECONDS`` which maps to the same snapshot
    interval when ``DASHBOARD_SNAPSHOT_SECONDS`` is unset.

    If the cached token has expired (``TokenException``), the session is
    cleared and the user is bounced back to ``/`` to log in again.

``GET /dashboard/stock-history``
    Authenticated JSON. Daily OHLCV candles from
    ``KiteConnect.historical_data`` for the stock chart page.

``GET /dashboard/stock-chart``
    Authenticated HTML page with candlestick + volume chart (static Chart.js
    under ``/static/``). Opened from Equity Holdings / Watch List links.

``GET /logout``
    Clears the session cookie, deletes the on-disk Kite access token
    (``.access_token.json``), and returns to ``/`` so the user must log in
    to Zerodha again (shared with the CLI).

References
----------
* Kite Connect HTTP API:    https://kite.trade/docs/connect/v3/
* pykiteconnect v4 API ref: https://kite.trade/docs/pykiteconnect/v4/
* Kite WebSocket streaming: https://kite.trade/docs/connect/v3/websocket/
* FastAPI:                  https://fastapi.tiangolo.com/
* Starlette SessionMiddleware:
  https://www.starlette.io/middleware/#sessionmiddleware
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import webbrowser

from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.events import subscribe_cache_refresh
from app.infrastructure.live_prices import live_price_stream
from app.presentation.http.routes.api_v1 import router as api_v1_router
from app.presentation.http.routes.auth_pages import router as auth_pages_router
from app.presentation.http.routes.dashboard_routes import router as dashboard_router
from app.presentation.http.routes.ws_live_prices import router as ws_live_prices_router
from app.presentation.http.server_config import (
    DASHBOARD_DISPLAY_NAME,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    SESSION_SECRET,
    STATIC_DIR,
)
from app.infrastructure.services.cache_orchestrator import run_startup_cache_warmup

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    try:
        subscribe_cache_refresh(live_price_stream.notify_cache_refresh)
        run_startup_cache_warmup()
        logger.info(
            "Startup note: live dashboard prices require Kite WebSocket market data "
            "to be enabled for this API key in developers.kite.trade."
        )
        try:
            yield
        except asyncio.CancelledError:
            pass
    finally:
        live_price_stream.close()


app = FastAPI(
    title=f"{DASHBOARD_DISPLAY_NAME} Dashboard",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_pages_router)
app.include_router(api_v1_router)
app.include_router(dashboard_router)
app.include_router(ws_live_prices_router)


def main() -> None:
    """Run the server with ``uvicorn`` on the configured host and port."""
    import uvicorn

    url = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/"

    def _open_browser() -> None:
        time.sleep(1.0)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    uvicorn.run(
        "app.server:app",
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        reload=False,
        log_level="info",
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
