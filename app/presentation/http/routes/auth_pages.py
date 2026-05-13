"""Landing page, Kite OAuth redirect/callback, logout, favicon."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from kiteconnect import KiteConnect

from app.infrastructure.auth import (
    clear_cached_access_token,
    load_credentials,
    save_cached_access_token,
)
from app.presentation.http.jinja_env import templates
from app.infrastructure.live_prices import live_price_stream
from app.presentation.http.server_auth import restore_session_if_token_valid
from app.presentation.http.server_config import DASHBOARD_DISPLAY_NAME
from app.infrastructure.services.cache_orchestrator import run_startup_cache_warmup

router = APIRouter()


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers request this automatically; no asset is shipped."""
    return Response(status_code=204)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page. Redirects to /dashboard if already authenticated."""
    if restore_session_if_token_valid(request):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"dashboard_name": DASHBOARD_DISPLAY_NAME},
    )


@router.get("/login")
async def login():
    """Redirect to Kite's hosted login URL."""
    api_key, _ = load_credentials()
    kite = KiteConnect(api_key=api_key)
    return RedirectResponse(kite.login_url(), status_code=303)


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    request: Request,
    request_token: str | None = None,
    status: str | None = None,
):
    """Handle Zerodha's redirect with the one-time ``request_token``."""
    if status and status != "success":
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "error": f"Login was cancelled (status={status}).",
                "dashboard_name": DASHBOARD_DISPLAY_NAME,
            },
            status_code=400,
        )

    if not request_token:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "error": "Missing request_token in callback URL.",
                "dashboard_name": DASHBOARD_DISPLAY_NAME,
            },
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
            {
                "error": f"Login failed: {exc}",
                "dashboard_name": DASHBOARD_DISPLAY_NAME,
            },
            status_code=401,
        )

    save_cached_access_token(session["access_token"])
    request.session["authenticated"] = True
    run_startup_cache_warmup()
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    """Clear the browser session and remove the cached Kite access token."""
    request.session.clear()
    clear_cached_access_token()
    live_price_stream.close()
    return RedirectResponse("/", status_code=303)
