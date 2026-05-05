"""Local web dashboard for the Zerodha account snapshot.

Run with::

    python -m app.web
    # or:
    uvicorn app.web:app --host 127.0.0.1 --port 5000

then open http://127.0.0.1:5000/ in your browser.

Routes
------
``GET /``
    Landing page. If the browser already has an authenticated session
    and a cached access token is on disk, redirects to ``/dashboard``;
    otherwise shows a "Login with Zerodha" button.

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

    If the cached token has expired (``TokenException``), the session is
    cleared and the user is bounced back to ``/`` to log in again.

``GET /logout``
    Clears the session cookie and returns to ``/``. Does **not** delete
    the cached access token (so the CLI can keep using it).

References
----------
* Kite Connect HTTP API:    https://kite.trade/docs/connect/v3/
* pykiteconnect v4 API ref: https://kite.trade/docs/pykiteconnect/v4/
* FastAPI:                  https://fastapi.tiangolo.com/
* Starlette SessionMiddleware:
  https://www.starlette.io/middleware/#sessionmiddleware
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from kiteconnect import KiteConnect
from kiteconnect.exceptions import PermissionException, TokenException
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    PROJECT_ROOT,
    build_authenticated_client,
    load_cached_access_token,
    load_credentials,
    save_cached_access_token,
)


TEMPLATES_DIR = PROJECT_ROOT / "templates"
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

# A new secret is generated on every process start, which means existing
# session cookies are invalidated on restart. That's fine for a local
# single-user dev tool; for a longer-lived deployment, persist this
# secret (e.g. SESSION_SECRET env var).
app.add_middleware(
    SessionMiddleware,
    secret_key=secrets.token_hex(32),
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


def _decorate_holding(h: dict) -> dict:
    """Enrich a Kite holdings entry with derived fields used by the template."""
    quantity = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
    avg = float(h.get("average_price") or 0.0)
    ltp = float(h.get("last_price") or 0.0)
    pnl = float(h.get("pnl") or 0.0)
    return {
        "tradingsymbol": h.get("tradingsymbol", ""),
        "exchange": h.get("exchange", ""),
        "quantity": quantity,
        "average_price": avg,
        "last_price": ltp,
        "invested": avg * quantity,
        "current": ltp * quantity,
        "pnl": pnl,
        "day_change_percentage": float(h.get("day_change_percentage") or 0.0),
    }


def _decorate_mf(h: dict) -> dict:
    """Enrich a Kite mf_holdings entry with derived fields."""
    units = float(h.get("quantity") or 0.0)
    avg = float(h.get("average_price") or 0.0)
    ltp = float(h.get("last_price") or 0.0)
    pnl = float(h.get("pnl") or 0.0)
    return {
        "fund": h.get("fund", ""),
        "folio": h.get("folio", ""),
        "units": units,
        "average_price": avg,
        "last_price": ltp,
        "invested": avg * units,
        "current": ltp * units,
        "pnl": pnl,
    }


def _decorate_position(p: dict) -> dict:
    """Enrich a Kite positions entry."""
    return {
        "tradingsymbol": p.get("tradingsymbol", ""),
        "exchange": p.get("exchange", ""),
        "product": p.get("product", ""),
        "quantity": int(p.get("quantity") or 0),
        "average_price": float(p.get("average_price") or 0.0),
        "last_price": float(p.get("last_price") or 0.0),
        "pnl": float(p.get("pnl") or 0.0),
        "m2m": float(p.get("m2m") or 0.0),
    }


def _summarise(rows: list[dict], *fields: str) -> dict:
    """Return a dict with sums of the named numeric fields across ``rows``."""
    return {field: sum(float(r.get(field) or 0.0) for r in rows) for field in fields}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page. Redirects to /dashboard if already authenticated."""
    if request.session.get("authenticated") and load_cached_access_token():
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
    """Clear the session cookie. Leaves the on-disk token cache intact."""
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the tabbed dashboard with all account snapshot sections."""
    if not request.session.get("authenticated"):
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

    equity_holdings = sorted(
        (_decorate_holding(h) for h in equity_raw),
        key=lambda r: r["tradingsymbol"],
    )
    mf_holdings = sorted(
        (_decorate_mf(h) for h in mf_raw),
        key=lambda r: r["fund"],
    )

    net_positions = positions_raw.get("net", []) or []
    open_net = [p for p in net_positions if int(p.get("quantity") or 0) != 0]
    equity_positions = sorted(
        (_decorate_position(p) for p in open_net if p.get("exchange") in EQUITY_EXCHANGES),
        key=lambda r: r["tradingsymbol"],
    )
    fno_positions = sorted(
        (_decorate_position(p) for p in open_net if p.get("exchange") in FNO_EXCHANGES),
        key=lambda r: r["tradingsymbol"],
    )

    available = margins_raw.get("available", {}) or {}
    utilised = margins_raw.get("utilised", {}) or {}
    cash = {
        "available_cash": float(available.get("cash") or 0.0),
        "live_balance": float(available.get("live_balance") or 0.0),
        "utilised": float(utilised.get("debits") or 0.0),
    }

    context = {
        "request": request,
        "equity_holdings": equity_holdings,
        "equity_totals": _summarise(equity_holdings, "invested", "current", "pnl"),
        "mf_holdings": mf_holdings,
        "mf_totals": _summarise(mf_holdings, "invested", "current", "pnl"),
        "mf_error": mf_error,
        "equity_positions": equity_positions,
        "equity_position_totals": _summarise(equity_positions, "pnl", "m2m"),
        "fno_positions": fno_positions,
        "fno_position_totals": _summarise(fno_positions, "pnl", "m2m"),
        "cash": cash,
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


def main() -> None:
    """Run the dashboard with ``uvicorn`` on http://127.0.0.1:5000/."""
    import uvicorn

    uvicorn.run(
        "app.web:app",
        host="127.0.0.1",
        port=5000,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
