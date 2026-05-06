"""Local web dashboard for the Zerodha account snapshot.

Run with::

    python -m app.web
    # or:
    uvicorn app.web:app --host 127.0.0.1 --port 5000

``python -m app.web`` starts Uvicorn and opens the dashboard URL in your
default browser. When using the ``uvicorn`` CLI, open the URL manually.

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
    Authenticated. Fetches all four account-snapshot data sets in one
    go and renders ``templates/dashboard.html``, a single page with
    in-page tabs for: Equity Holdings, Mutual Funds, Equity Positions,
    F&O Positions, Cash Balance.

    Kite endpoints called per request:

    * ``KiteConnect.holdings()``     -> ``GET /portfolio/holdings``
    * ``KiteConnect.mf_holdings()``  -> ``GET /mf/holdings`` (optional;
      ``PermissionException`` is caught and surfaced as a notice).
    * ``KiteConnect.positions()``    -> ``GET /portfolio/positions``
    * ``KiteConnect.margins("equity")`` -> ``GET /user/margins/equity``
    * ``KiteTicker`` (WebSocket)     -> ``wss://ws.kite.trade`` for live
      LTP snapshots on equity/F&O instrument tokens used in the current view.

    Dashboard auto-refresh interval is controlled by
    ``DASHBOARD_REFRESH_SECONDS`` (defaults to 1s, minimum 1s).

    If the cached token has expired (``TokenException``), the session is
    cleared and the user is bounced back to ``/`` to log in again.

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

import os
import secrets
import threading
import time
import webbrowser

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from kiteconnect import KiteConnect
from kiteconnect.exceptions import PermissionException, TokenException
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    PROJECT_ROOT,
    build_authenticated_client,
    clear_cached_access_token,
    load_cached_access_token,
    load_credentials,
    save_cached_access_token,
    validate_kite_session,
)
from app.instruments import get_cash_equity_name_lookups, symbol_with_company_name
from app.live_prices import live_price_stream


TEMPLATES_DIR = PROJECT_ROOT / "templates"
SESSION_SECRET_FILE = PROJECT_ROOT / ".session_secret"

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5000


def _dashboard_refresh_interval_ms() -> int:
    """Read dashboard auto-refresh interval from env (seconds -> milliseconds)."""
    raw = os.getenv("DASHBOARD_REFRESH_SECONDS", "1").strip()
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 1.0
    seconds = max(1.0, seconds)
    return int(seconds * 1000)


_DASHBOARD_REFRESH_INTERVAL_MS = _dashboard_refresh_interval_ms()


def _session_secret() -> str:
    """Stable signing key so session cookies survive server restarts.

    Prefer ``SESSION_SECRET`` in the environment; otherwise read or create
    ``.session_secret`` in the project root (gitignored).
    """
    env = os.getenv("SESSION_SECRET", "").strip()
    if env:
        return env
    if SESSION_SECRET_FILE.exists():
        raw = SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
        if raw:
            return raw
    secret = secrets.token_hex(32)
    SESSION_SECRET_FILE.write_text(secret + "\n", encoding="utf-8")
    return secret


_SESSION_SECRET = _session_secret()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Jinja2 filters
# ---------------------------------------------------------------------------


def _format_inr(value: float | int | None) -> str:
    """Format a number with comma separators and 2 decimals (no currency sign)."""
    if value is None:
        return "-"
    return f"{float(value):,.2f}"


def _format_units(value: float | int | None) -> str:
    """Format a quantity / NAV with 4 decimals (used for MF rows)."""
    if value is None:
        return "-"
    return f"{float(value):,.4f}"


def _format_pct(value: float | int | None) -> str:
    """Format a percentage with 2 decimals."""
    if value is None:
        return "-"
    return f"{float(value):,.2f}%"


def _sign_class(value: float | int | None) -> str:
    """Return ``positive`` / ``negative`` / ``neutral`` for CSS colouring."""
    if value is None:
        return "neutral"
    v = float(value)
    if v > 0:
        return "positive"
    if v < 0:
        return "negative"
    return "neutral"


templates.env.filters["inr"] = _format_inr
templates.env.filters["units"] = _format_units
templates.env.filters["pct"] = _format_pct
templates.env.filters["sign_class"] = _sign_class


# ---------------------------------------------------------------------------
# FastAPI app + session middleware
# ---------------------------------------------------------------------------

app = FastAPI(title="MomentumStrategy Dashboard", docs_url=None, redoc_url=None)

# Persisted secret so the signed session cookie remains valid across
# process restarts (see ``_session_secret``).
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    same_site="lax",
    https_only=False,
)


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------

EQUITY_EXCHANGES = {"NSE", "BSE"}
FNO_EXCHANGES = {"NFO", "BFO", "CDS", "BCD", "MCX"}


def _kite_for_request() -> KiteConnect | None:
    """Return an authenticated KiteConnect client or ``None`` if not logged in.

    Reads the cached access token written by ``/callback`` (or by the
    CLI). The session cookie only carries an ``authenticated`` flag;
    the actual token lives on disk so the CLI and web app share it.
    """
    token = load_cached_access_token()
    if not token:
        return None
    api_key, _ = load_credentials()
    return build_authenticated_client(api_key, token)


def _restore_session_if_token_valid(request: Request) -> bool:
    """Ensure ``request.session`` reflects a valid Kite token if one exists on disk.

    After a server restart the cookie may still be valid (persistent
    session secret); if not, a stored access token is probed with
    :func:`~app.auth.validate_kite_session` and the session is marked
    authenticated when Kite still accepts it (until daily expiry).
    """
    token = load_cached_access_token()
    if request.session.get("authenticated"):
        if token:
            return True
        request.session.clear()
        return False
    if not token:
        return False
    api_key, _ = load_credentials()
    kite = build_authenticated_client(api_key, token)
    if not validate_kite_session(kite):
        return False
    request.session["authenticated"] = True
    return True


def _decorate_holding(
    h: dict,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
) -> dict:
    """Enrich a Kite holdings entry with derived fields used by the template."""
    quantity = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
    avg = float(h.get("average_price") or 0.0)
    ltp = float(h.get("last_price") or 0.0)
    live_ltp_applied = bool(h.get("_live_ltp_applied"))
    pnl = (ltp - avg) * quantity if live_ltp_applied else float(h.get("pnl") or 0.0)
    close_price = float(h.get("close_price") or 0.0)
    if live_ltp_applied and close_price > 0:
        day_change_percentage = ((ltp - close_price) / close_price) * 100.0
    else:
        day_change_percentage = float(h.get("day_change_percentage") or 0.0)
    symbol = str(h.get("tradingsymbol", "")).strip()
    symbol_label = symbol_with_company_name(
        symbol=symbol,
        exchange=str(h.get("exchange", "")),
        instrument_token=int(h.get("instrument_token") or 0),
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
    )
    return {
        "tradingsymbol": symbol,
        "symbol_label": symbol_label,
        "exchange": h.get("exchange", ""),
        "quantity": quantity,
        "average_price": avg,
        "last_price": ltp,
        "invested": avg * quantity,
        "current": ltp * quantity,
        "pnl": pnl,
        "day_change_percentage": day_change_percentage,
    }


def _decorate_mf(h: dict) -> dict:
    """Enrich a Kite mf_holdings entry with derived fields."""
    units = float(h.get("quantity") or 0.0)
    avg = float(h.get("average_price") or 0.0)
    ltp = float(h.get("last_price") or 0.0)
    invested = avg * units
    current = ltp * units
    api_pnl = h.get("pnl")
    # Kite MF holdings can return pnl=0.0 even when NAV-based P&L is non-zero.
    # Prefer API pnl only when it is non-zero; otherwise derive from NAV values.
    pnl = (
        float(api_pnl)
        if api_pnl not in (None, "") and float(api_pnl) != 0.0
        else (current - invested)
    )
    return {
        "fund": h.get("fund", ""),
        "folio": h.get("folio", ""),
        "units": units,
        "average_price": avg,
        "last_price": ltp,
        "invested": invested,
        "current": current,
        "pnl": pnl,
    }


def _decorate_position(
    p: dict,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
) -> dict:
    """Enrich a Kite positions entry."""
    qty = int(p.get("quantity") or 0)
    ltp = float(p.get("last_price") or 0.0)
    live_ltp_applied = bool(p.get("_live_ltp_applied"))
    if live_ltp_applied:
        buy_value = float(p.get("buy_value") or 0.0)
        sell_value = float(p.get("sell_value") or 0.0)
        multiplier = float(p.get("multiplier") or 1.0)
        pnl = (sell_value - buy_value) + (qty * ltp * multiplier)
    else:
        pnl = float(p.get("pnl") or 0.0)
    symbol = str(p.get("tradingsymbol", "")).strip()
    symbol_label = symbol_with_company_name(
        symbol=symbol,
        exchange=str(p.get("exchange", "")),
        instrument_token=int(p.get("instrument_token") or 0),
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
    )
    return {
        "tradingsymbol": symbol,
        "symbol_label": symbol_label,
        "exchange": p.get("exchange", ""),
        "product": p.get("product", ""),
        "quantity": qty,
        "average_price": float(p.get("average_price") or 0.0),
        "last_price": ltp,
        "pnl": pnl,
        "m2m": float(p.get("m2m") or 0.0),
    }


def _summarise(rows: list[dict], *fields: str) -> dict:
    """Return a dict with sums of the named numeric fields across ``rows``."""
    return {field: sum(float(r.get(field) or 0.0) for r in rows) for field in fields}


def _overlay_live_ltp(row: dict, live_ltp_by_token: dict[int, float]) -> dict:
    """Return row with websocket LTP overlaid when available."""
    out = dict(row)
    token = int(out.get("instrument_token") or 0)
    if token > 0 and token in live_ltp_by_token:
        out["last_price"] = float(live_ltp_by_token[token])
        out["_live_ltp_applied"] = True
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers request this automatically; no asset is shipped."""
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page. Redirects to /dashboard if already authenticated."""
    if _restore_session_if_token_valid(request):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "index.html")


@app.get("/login")
async def login():
    """Redirect to Kite's hosted login URL.

    See https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.login_url
    """
    api_key, _ = load_credentials()
    kite = KiteConnect(api_key=api_key)
    return RedirectResponse(kite.login_url(), status_code=303)


@app.get("/callback", response_class=HTMLResponse)
async def callback(
    request: Request,
    request_token: str | None = None,
    status: str | None = None,
):
    """Handle Zerodha's redirect with the one-time ``request_token``.

    See https://kite.trade/docs/connect/v3/user/#login-flow
    """
    if status and status != "success":
        return templates.TemplateResponse(
            request,
            "index.html",
            {"error": f"Login was cancelled (status={status})."},
            status_code=400,
        )

    if not request_token:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"error": "Missing request_token in callback URL."},
            status_code=400,
        )

    api_key, api_secret = load_credentials()
    kite = KiteConnect(api_key=api_key)
    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:  # noqa: BLE001 - surface any Kite error to the user
        return templates.TemplateResponse(
            request,
            "index.html",
            {"error": f"Login failed: {exc}"},
            status_code=401,
        )

    save_cached_access_token(session["access_token"])
    request.session["authenticated"] = True
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    """Clear the browser session and remove the cached Kite access token."""
    request.session.clear()
    clear_cached_access_token()
    live_price_stream.close()
    return RedirectResponse("/", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the tabbed dashboard with all account snapshot sections."""
    if not _restore_session_if_token_valid(request):
        return RedirectResponse("/", status_code=303)

    kite = _kite_for_request()
    if kite is None:
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    try:
        equity_raw = kite.holdings() or []
        positions_raw = kite.positions() or {}
        margins_raw = kite.margins(segment="equity") or {}
    except TokenException:
        request.session.clear()
        live_price_stream.close()
        return RedirectResponse("/", status_code=303)

    mf_error: str | None = None
    try:
        mf_raw = kite.mf_holdings() or []
    except PermissionException:
        mf_raw = []
        mf_error = (
            "Mutual Funds API is not enabled on this Kite Connect app. "
            "Enable the MF module at https://developers.kite.trade if you "
            "want this section."
        )

    net_positions = positions_raw.get("net", []) or []
    open_net = [p for p in net_positions if int(p.get("quantity") or 0) != 0]

    live_ltp_by_token: dict[int, float] = {}
    access_token = load_cached_access_token()
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
            tokens = {t for t in tokens if t > 0}
            live_price_stream.subscribe(tokens)
            live_ltp_by_token = live_price_stream.snapshot_ltp(tokens)
        except Exception:
            # Keep dashboard resilient if websocket setup fails.
            live_ltp_by_token = {}

    equity_token_to_name, equity_symbol_to_name = get_cash_equity_name_lookups(kite)

    equity_holdings = sorted(
        (
            _decorate_holding(
                _overlay_live_ltp(h, live_ltp_by_token),
                equity_token_to_name,
                equity_symbol_to_name,
            )
            for h in equity_raw
        ),
        key=lambda r: r["tradingsymbol"],
    )
    mf_holdings = sorted(
        (_decorate_mf(h) for h in mf_raw),
        key=lambda r: r["fund"],
    )

    equity_positions = sorted(
        (
            _decorate_position(
                _overlay_live_ltp(p, live_ltp_by_token),
                equity_token_to_name,
                equity_symbol_to_name,
            )
            for p in open_net
            if p.get("exchange") in EQUITY_EXCHANGES
        ),
        key=lambda r: r["tradingsymbol"],
    )
    fno_positions = sorted(
        (
            _decorate_position(
                _overlay_live_ltp(p, live_ltp_by_token),
                equity_token_to_name,
                equity_symbol_to_name,
            )
            for p in open_net
            if p.get("exchange") in FNO_EXCHANGES
        ),
        key=lambda r: r["tradingsymbol"],
    )

    available = margins_raw.get("available", {}) or {}
    utilised = margins_raw.get("utilised", {}) or {}
    cash = {
        "available_cash": float(available.get("cash") or 0.0),
        "live_balance": float(available.get("live_balance") or 0.0),
        "utilised": float(utilised.get("debits") or 0.0),
    }

    equity_totals = _summarise(equity_holdings, "invested", "current", "pnl")
    mf_totals = _summarise(mf_holdings, "invested", "current", "pnl")
    equity_position_totals = _summarise(equity_positions, "pnl", "m2m")
    fno_position_totals = _summarise(fno_positions, "pnl", "m2m")

    total_invested = equity_totals["invested"] + mf_totals["invested"]
    total_current = equity_totals["current"] + mf_totals["current"]
    holdings_pnl = equity_totals["pnl"] + mf_totals["pnl"]
    positions_pnl = equity_position_totals["pnl"] + fno_position_totals["pnl"]
    overall_pnl = holdings_pnl + positions_pnl

    context = {
        "request": request,
        "equity_holdings": equity_holdings,
        "equity_totals": equity_totals,
        "mf_holdings": mf_holdings,
        "mf_totals": mf_totals,
        "mf_error": mf_error,
        "equity_positions": equity_positions,
        "equity_position_totals": equity_position_totals,
        "fno_positions": fno_positions,
        "fno_position_totals": fno_position_totals,
        "cash": cash,
        "refresh_interval_ms": _DASHBOARD_REFRESH_INTERVAL_MS,
        "portfolio_summary": {
            "total_invested": total_invested,
            "total_current": total_current,
            "holdings_pnl": holdings_pnl,
            "positions_pnl": positions_pnl,
            "overall_pnl": overall_pnl,
        },
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


def main() -> None:
    """Run the dashboard with ``uvicorn`` on the configured host and port.

    Opens the default browser to the entry URL after a short delay so the
    socket is listening (only for ``python -m app.web``, not ``uvicorn`` CLI).
    """
    import uvicorn

    url = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/"

    def _open_browser() -> None:
        time.sleep(1.0)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    uvicorn.run(
        "app.web:app",
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        reload=False,
        log_level="info",
    )


@app.on_event("shutdown")
async def _shutdown_live_price_stream() -> None:
    """Ensure websocket thread is closed when FastAPI exits."""
    live_price_stream.close()


if __name__ == "__main__":
    main()
