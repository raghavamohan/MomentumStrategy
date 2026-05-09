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
      memory + disk cache in :mod:`app.portfolio_model`) -> current market
      regime banner and ``dashboard-bootstrap.marketCondition``.

    Live LTP updates are pushed to the browser over ``WS /ws/live-prices``
    (fed by the existing KiteTicker stream). A separate **slow** full-page
    snapshot uses ``GET /dashboard`` on an interval controlled by
    ``DASHBOARD_SNAPSHOT_SECONDS`` (defaults to 120s, minimum 10s); see also
    legacy ``DASHBOARD_REFRESH_SECONDS`` which maps to the same snapshot
    interval when ``DASHBOARD_SNAPSHOT_SECONDS`` is unset.

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

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import json
import logging
import os
import re
from collections.abc import MutableMapping
from contextlib import asynccontextmanager
from typing import Any
import secrets
import threading
import time
import warnings
import webbrowser

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
import keyring
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
from app.instruments import (
    get_cash_equity_isin_lookups,
    get_cash_equity_kite_sector_lookups,
    get_cash_equity_name_lookups,
    get_reference_cache_debug_snapshot,
    get_isin_to_industry,
    get_nifty50_symbols,
    get_nse_symbol_to_industry,
    get_nse_symbol_to_token_lookup,
    resolve_equity_sector,
    warm_reference_caches,
)
from app.env_util import log_dashboard_ws_debug_exception
from app.live_prices import live_price_stream, notify_dashboard_cache_refresh
from app.model_cache_store import current_effective_day_ist, start_background_refresh_job
from app.portfolio_model import (
    EQUITY_EXCHANGES,
    FNO_EXCHANGES,
    build_equity_holding,
    build_mf_holding,
    build_mf_underlying_breakdown,
    build_position,
    equity_sector_breakdown,
    get_marketsmith_market_condition,
    marketsmith_market_condition_bootstrap,
    normalize_equity_sector,
    overlay_live_ltp,
    summarise,
    summarise_equity_by_sector,
)


logger = logging.getLogger(__name__)
_DASHBOARD_TIMING_LOGGER = logging.getLogger("app.dashboard.timing")


TEMPLATES_DIR = PROJECT_ROOT / "templates"
SESSION_SECRET_FILE = PROJECT_ROOT / ".session_secret"
SESSION_SECRET_KEYRING_SERVICE = "MomentumStrategy"
SESSION_SECRET_KEYRING_ACCOUNT = "dashboard-session-secret"
DASHBOARD_TIMING_LOG_FILE = PROJECT_ROOT / ".cache" / "dashboard_timing.log"
_REFERENCE_WARMUP_LOCK = threading.Lock()
_REFERENCE_WARMUP_IN_PROGRESS = False
_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE_TTL_SECONDS = 600.0
_PROFILE_CACHE_VALUE: dict[str, Any] = {}
_PROFILE_CACHE_EXPIRES_AT = 0.0
_QUOTE_CACHE_LOCK = threading.Lock()
_QUOTE_CACHE_DAY = ""
_QUOTE_CACHE: dict[str, dict[str, Any]] = {}
_MF_CACHE_LOCK = threading.Lock()
_MF_HOLDINGS_CACHE_DAY = ""
_MF_HOLDINGS_CACHE_PAYLOAD: dict[str, Any] | None = None
_MF_HOLDINGS_REFRESH_IN_PROGRESS = False
_MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = 0.0
_MF_UNDERLYINGS_CACHE_DAY_BY_TONE: dict[str, str] = {}
_MF_UNDERLYINGS_CACHE_PAYLOADS: dict[str, dict[str, Any]] = {}
_MF_UNDERLYINGS_REFRESH_IN_PROGRESS: set[str] = set()
_MF_HOLDINGS_REFRESH_TIMEOUT_SECONDS = 25.0
_MF_HOLDINGS_STUCK_RESET_SECONDS = max(45.0, _MF_HOLDINGS_REFRESH_TIMEOUT_SECONDS + 15.0)

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5000


def _setup_dashboard_timing_logger() -> None:
    """Attach a dedicated file handler for dashboard timing lines."""
    if _DASHBOARD_TIMING_LOGGER.handlers:
        return
    DASHBOARD_TIMING_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(DASHBOARD_TIMING_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _DASHBOARD_TIMING_LOGGER.addHandler(file_handler)
    _DASHBOARD_TIMING_LOGGER.setLevel(logging.INFO)
    _DASHBOARD_TIMING_LOGGER.propagate = False


_setup_dashboard_timing_logger()


def _dashboard_snapshot_interval_ms() -> int:
    """Full HTML snapshot interval (REST refresh for MF/cash/structure)."""
    raw = (
        os.getenv("DASHBOARD_SNAPSHOT_SECONDS", "").strip()
        or os.getenv("DASHBOARD_REFRESH_SECONDS", "").strip()
        or "120"
    )
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 120.0
    seconds = max(10.0, seconds)
    return int(seconds * 1000)


_DASHBOARD_SNAPSHOT_INTERVAL_MS = _dashboard_snapshot_interval_ms()


def _dashboard_display_name() -> str:
    """Dashboard/product display name from env with a friendly default."""
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv("KITE_DASHBOARD_NAME", "").strip() or "Raghava's Portfolio"


_DASHBOARD_DISPLAY_NAME = _dashboard_display_name()

# Compact keys (spaces / punctuation stripped, uppercased) -> NSE index tradingsymbol for Kite quote keys.
_INDEX_COMPACT_TO_TRADINGSYMBOL: dict[str, str] = {
    "NIFTY50": "NIFTY 50",
    "NIFTY_50": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "NIFTYBANK": "NIFTY BANK",
    "NIFTYIT": "NIFTY IT",
    "NIFTY_IT": "NIFTY IT",
    "NIFTYFINSERVICE": "NIFTY FIN SERVICE",
    "NIFTY_FIN_SERVICE": "NIFTY FIN SERVICE",
    "NIFTYMET": "NIFTY METAL",
    "NIFTY_METAL": "NIFTY METAL",
}


def _resolve_index_tradingsymbol(label: str) -> str:
    """Map a dashboard env token (e.g. ``NIFTY50``) to an NSE index tradingsymbol."""
    stripped = label.strip()
    if not stripped:
        return ""
    compact = "".join(stripped.split()).upper().replace("-", "")
    return _INDEX_COMPACT_TO_TRADINGSYMBOL.get(compact, stripped)


def _dashboard_index_entries() -> list[tuple[str, str]]:
    """Ordered unique (env label, NSE tradingsymbol) pairs for header index quotes."""
    load_dotenv(PROJECT_ROOT / ".env")
    raw = os.getenv("KITE_DASHBOARD_INDICES", "").strip()
    labels = [p.strip() for p in raw.split(",") if p.strip()]
    if not labels:
        labels = ["NIFTY50", "BANKNIFTY", "NIFTYIT", "NIFTYFINSERVICE", "NIFTYMET"]
    seen_ts: set[str] = set()
    out: list[tuple[str, str]] = []
    for label in labels:
        ts = _resolve_index_tradingsymbol(label)
        if not ts or ts in seen_ts:
            continue
        seen_ts.add(ts)
        out.append((label, ts))
    return out


_DASHBOARD_INDEX_ENTRIES = _dashboard_index_entries()

_MF_PERMISSION_ERROR = (
    "Mutual Funds API is not enabled on this Kite Connect app. "
    "Enable the MF module at https://developers.kite.trade if you "
    "want this section."
)


def _session_secret() -> str:
    """Stable signing key so session cookies survive server restarts.

    Prefer ``SESSION_SECRET`` in the environment; otherwise read/store the
    secret in the OS keychain using ``keyring``.

    Legacy support: if an older plaintext ``.session_secret`` file exists,
    migrate it into keychain and remove the file.
    """
    env = os.getenv("SESSION_SECRET", "").strip()
    if env:
        return env

    # Migrate legacy plaintext secret once, then remove the file.
    if SESSION_SECRET_FILE.exists():
        try:
            raw = SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
            if raw:
                keyring.set_password(
                    SESSION_SECRET_KEYRING_SERVICE,
                    SESSION_SECRET_KEYRING_ACCOUNT,
                    raw,
                )
                SESSION_SECRET_FILE.unlink(missing_ok=True)
                return raw
        except Exception:
            # Ignore migration failures; proceed with keyring lookup/generation.
            pass

    try:
        stored = keyring.get_password(
            SESSION_SECRET_KEYRING_SERVICE,
            SESSION_SECRET_KEYRING_ACCOUNT,
        )
        if stored:
            return stored.strip()
    except Exception:
        # If keyring backend is unavailable, fall through to an in-memory secret.
        pass

    secret = secrets.token_hex(32)
    try:
        keyring.set_password(
            SESSION_SECRET_KEYRING_SERVICE,
            SESSION_SECRET_KEYRING_ACCOUNT,
            secret,
        )
    except Exception:
        warnings.warn(
            "No usable OS keyring backend found; using non-persistent session "
            "secret for this run. Set SESSION_SECRET or install a keyring backend "
            "to persist login sessions across restarts.",
            RuntimeWarning,
        )
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

@asynccontextmanager
async def _lifespan(_: FastAPI):
    try:
        logger.info(
            "Startup note: live dashboard prices require Kite WebSocket market data "
            "to be enabled for this API key in developers.kite.trade."
        )
        _start_reference_cache_warmup()
        try:
            yield
        except asyncio.CancelledError:
            # Windows shutdown (Ctrl+C) can cancel lifespan receive while uvicorn
            # is tearing down; treat it as a normal shutdown path.
            pass
    finally:
        # Ensure websocket/ticker thread is closed when FastAPI exits.
        live_price_stream.close()


app = FastAPI(
    title=f"{_DASHBOARD_DISPLAY_NAME} Dashboard",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)

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


def _restore_session_if_token_valid_session(session: MutableMapping[str, Any]) -> bool:
    """Ensure ``session`` reflects a valid Kite token if one exists on disk.

    Shared by HTTP routes and WebSocket handlers (WebSocket cannot use
    :class:`~starlette.requests.Request`, which only accepts ``http`` scopes).
    """
    token = load_cached_access_token()
    if session.get("authenticated"):
        if token:
            return True
        session.clear()
        return False
    if not token:
        return False
    api_key, _ = load_credentials()
    kite = build_authenticated_client(api_key, token)
    if not validate_kite_session(kite):
        return False
    session["authenticated"] = True
    return True


def _restore_session_if_token_valid(request: Request) -> bool:
    """Ensure ``request.session`` reflects a valid Kite token if one exists on disk."""
    return _restore_session_if_token_valid_session(request.session)


def _decorate_holding(
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


def _decorate_mf(h: dict) -> dict:
    """Enrich a Kite mf_holdings entry with derived fields."""
    return build_mf_holding(h).to_dict()


def _build_mf_underlying_breakdown(
    mf_holdings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, list[str], int, int]:
    """Combine all MF holdings into one instrument/sector weighted view via mfdata."""
    return build_mf_underlying_breakdown(mf_holdings)


def _decorate_position(
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


def _dashboard_timing_mark(
    timings: list[tuple[str, float]],
    stage: str,
    start_time: float,
) -> None:
    """Append elapsed milliseconds since ``start_time`` for one stage."""
    timings.append((stage, (time.perf_counter() - start_time) * 1000.0))


def _today_cache_token() -> str:
    """Return 09:00-IST cache-day token for all model caches."""
    return current_effective_day_ist(cutoff_hour=9)


def _get_cached_quotes(kite, quote_keys: list[str]) -> dict[str, Any]:
    """Return quote payload from in-memory day cache, fetching only misses."""
    global _QUOTE_CACHE_DAY
    if not quote_keys:
        return {}
    day = _today_cache_token()
    with _QUOTE_CACHE_LOCK:
        if _QUOTE_CACHE_DAY != day:
            _QUOTE_CACHE_DAY = day
            _QUOTE_CACHE.clear()
        cached = {k: _QUOTE_CACHE.get(k, {}) for k in quote_keys if k in _QUOTE_CACHE}
    missing = [k for k in quote_keys if k not in cached]
    if not missing:
        return cached
    try:
        fetched = kite.quote(missing) or {}
    except Exception:
        fetched = {}
    with _QUOTE_CACHE_LOCK:
        if _QUOTE_CACHE_DAY != day:
            _QUOTE_CACHE_DAY = day
            _QUOTE_CACHE.clear()
        for k in missing:
            _QUOTE_CACHE[k] = fetched.get(k) or {}
            cached[k] = _QUOTE_CACHE[k]
    return cached


def _get_cached_profile(kite) -> dict[str, Any]:
    """Return profile payload cached briefly to avoid per-refresh profile calls."""
    global _PROFILE_CACHE_EXPIRES_AT
    now = time.time()
    with _PROFILE_CACHE_LOCK:
        if now < _PROFILE_CACHE_EXPIRES_AT and _PROFILE_CACHE_VALUE:
            return dict(_PROFILE_CACHE_VALUE)
    try:
        profile = kite.profile() or {}
    except Exception:
        profile = {}
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE_VALUE.clear()
        _PROFILE_CACHE_VALUE.update(profile)
        _PROFILE_CACHE_EXPIRES_AT = now + _PROFILE_CACHE_TTL_SECONDS
        return dict(_PROFILE_CACHE_VALUE)


def _mf_holdings_loading_payload(*, stale_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(stale_payload or {})
    payload.setdefault("rows", [])
    payload.setdefault("totals", {"invested": 0.0, "current": 0.0, "pnl": 0.0})
    payload.setdefault("count", len(payload.get("rows") or []))
    payload.setdefault("error", "")
    payload["loading"] = True
    payload["stale"] = stale_payload is not None
    return payload


def _fetch_mf_holdings_with_timeout(kite, timeout_seconds: float) -> list[dict[str, Any]]:
    """Fetch MF holdings with an upper bound to avoid indefinite loading state."""
    timeout = max(1.0, float(timeout_seconds))
    done = threading.Event()
    result_holder: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result_holder["result"] = kite.mf_holdings()
        except Exception as exc:  # pragma: no cover - passthrough from network client
            result_holder["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    if not done.wait(timeout):
        raise FuturesTimeoutError()
    error = result_holder.get("error")
    if error is not None:
        raise error
    return list(result_holder.get("result") or [])


def _reset_stuck_mf_holdings_refresh_unlocked(now_monotonic: float) -> bool:
    """Drop stale in-progress flag when refresh exceeded watchdog budget."""
    global _MF_HOLDINGS_REFRESH_IN_PROGRESS, _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC
    if not _MF_HOLDINGS_REFRESH_IN_PROGRESS:
        return False
    started = float(_MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC or 0.0)
    if started <= 0.0:
        return False
    elapsed = now_monotonic - started
    if elapsed < _MF_HOLDINGS_STUCK_RESET_SECONDS:
        return False
    _MF_HOLDINGS_REFRESH_IN_PROGRESS = False
    _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = 0.0
    logger.warning(
        "Reset stuck MF holdings refresh after %.1fs; scheduling a fresh attempt.",
        elapsed,
    )
    return True


def _start_mf_holdings_refresh(kite) -> None:
    global _MF_HOLDINGS_REFRESH_IN_PROGRESS, _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC
    with _MF_CACHE_LOCK:
        if _MF_HOLDINGS_REFRESH_IN_PROGRESS:
            return
        _MF_HOLDINGS_REFRESH_IN_PROGRESS = True
        _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = time.monotonic()

    def _job() -> None:
        global _MF_HOLDINGS_CACHE_PAYLOAD, _MF_HOLDINGS_CACHE_DAY, _MF_HOLDINGS_REFRESH_IN_PROGRESS, _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC
        try:
            day = _today_cache_token()
            with _MF_CACHE_LOCK:
                previous_payload = (
                    dict(_MF_HOLDINGS_CACHE_PAYLOAD)
                    if _MF_HOLDINGS_CACHE_PAYLOAD is not None
                    else None
                )
            try:
                mf_raw = _fetch_mf_holdings_with_timeout(
                    kite,
                    timeout_seconds=_MF_HOLDINGS_REFRESH_TIMEOUT_SECONDS,
                )
            except PermissionException:
                payload = {
                    "rows": [],
                    "totals": {"invested": 0.0, "current": 0.0, "pnl": 0.0},
                    "count": 0,
                    "error": _MF_PERMISSION_ERROR,
                }
            except TokenException:
                logger.warning("MF holdings refresh skipped: Kite token expired.")
                payload = {
                    "rows": [],
                    "totals": {"invested": 0.0, "current": 0.0, "pnl": 0.0},
                    "count": 0,
                    "error": "Kite session expired. Please log in again.",
                }
            except FuturesTimeoutError:
                logger.warning("MF holdings refresh timed out after %.1fs.", _MF_HOLDINGS_REFRESH_TIMEOUT_SECONDS)
                payload = {
                    "rows": [],
                    "totals": {"invested": 0.0, "current": 0.0, "pnl": 0.0},
                    "count": 0,
                    "error": "Timed out while loading mutual fund holdings. Please retry.",
                }
            except Exception:
                logger.warning("MF holdings refresh failed.", exc_info=True)
                payload = {
                    "rows": [],
                    "totals": {"invested": 0.0, "current": 0.0, "pnl": 0.0},
                    "count": 0,
                    "error": "Unable to load mutual fund holdings right now.",
                }
            else:
                mf_holdings = sorted((_decorate_mf(h) for h in mf_raw), key=lambda r: r["fund"])
                mf_totals = summarise(mf_holdings, "invested", "current", "pnl")
                payload = {
                    "rows": mf_holdings,
                    "totals": mf_totals,
                    "count": len(mf_holdings),
                    "error": "",
                }
            payload["loading"] = False
            payload["stale"] = False
            if (
                previous_payload
                and previous_payload.get("rows")
                and payload.get("error")
            ):
                # Keep stale-but-useful data when the latest refresh fails.
                payload = dict(previous_payload)
                payload["loading"] = False
                payload["stale"] = True
            with _MF_CACHE_LOCK:
                _MF_HOLDINGS_CACHE_PAYLOAD = dict(payload)
                _MF_HOLDINGS_CACHE_DAY = day
                _MF_UNDERLYINGS_CACHE_DAY_BY_TONE.clear()
            notify_dashboard_cache_refresh()
        finally:
            with _MF_CACHE_LOCK:
                _MF_HOLDINGS_REFRESH_IN_PROGRESS = False
                _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = 0.0

    if not start_background_refresh_job("mf-holdings", _job):
        with _MF_CACHE_LOCK:
            _MF_HOLDINGS_REFRESH_IN_PROGRESS = False
            _MF_HOLDINGS_REFRESH_STARTED_AT_MONOTONIC = 0.0
        logger.warning("MF holdings refresh already running; using existing background task.")


def _get_cached_mf_holdings_payload(kite) -> dict[str, Any]:
    """Return MF holdings payload while refreshing stale/missing cache in background."""
    day = _today_cache_token()
    with _MF_CACHE_LOCK:
        _reset_stuck_mf_holdings_refresh_unlocked(time.monotonic())
        cached = dict(_MF_HOLDINGS_CACHE_PAYLOAD) if _MF_HOLDINGS_CACHE_PAYLOAD is not None else None
        cached_day = _MF_HOLDINGS_CACHE_DAY
        refreshing = _MF_HOLDINGS_REFRESH_IN_PROGRESS

    if cached is not None and cached_day == day:
        cached["loading"] = False
        cached["stale"] = False
        return cached

    if cached is not None and cached_day != day:
        if not refreshing:
            _start_mf_holdings_refresh(kite)
        return _mf_holdings_loading_payload(stale_payload=cached)

    if not refreshing:
        _start_mf_holdings_refresh(kite)
    return _mf_holdings_loading_payload()


def _normalize_mf_underlying_tone(tone: str) -> str:
    value = str(tone or "").strip().lower()
    if value in {"gainers", "losers"}:
        return value
    return "all"


def _filter_mf_holdings_by_tone(
    rows: list[dict[str, Any]], tone: str
) -> list[dict[str, Any]]:
    if tone == "all":
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        pnl = float(row.get("pnl") or 0.0)
        if tone == "gainers" and pnl > 0.0:
            out.append(row)
        elif tone == "losers" and pnl < 0.0:
            out.append(row)
    return out


def _mf_underlyings_loading_payload(
    tone_key: str,
    *,
    stale_payload: dict[str, Any] | None = None,
    holdings_error: str = "",
) -> dict[str, Any]:
    payload = dict(stale_payload or {})
    payload.setdefault("rows", [])
    payload.setdefault("month", "")
    payload.setdefault("notAggregatedFunds", [])
    payload.setdefault("aggregatedFundCount", 0)
    payload.setdefault("totalFundCount", 0)
    payload["tone"] = tone_key
    payload["error"] = str(payload.get("error") or holdings_error or "")
    payload["loading"] = True
    payload["stale"] = stale_payload is not None
    return payload


def _start_mf_underlyings_refresh(kite, *, tone_key: str) -> None:
    with _MF_CACHE_LOCK:
        if tone_key in _MF_UNDERLYINGS_REFRESH_IN_PROGRESS:
            return
        _MF_UNDERLYINGS_REFRESH_IN_PROGRESS.add(tone_key)

    def _job() -> None:
        try:
            holdings_payload = _get_cached_mf_holdings_payload(kite)
            if holdings_payload.get("loading"):
                return
            if holdings_payload.get("error"):
                payload = {
                    "rows": [],
                    "month": "",
                    "notAggregatedFunds": [],
                    "aggregatedFundCount": 0,
                    "totalFundCount": 0,
                    "tone": tone_key,
                    "error": str(holdings_payload.get("error") or ""),
                    "loading": False,
                    "stale": False,
                }
            else:
                mf_holdings = list(holdings_payload.get("rows") or [])
                mf_holdings = _filter_mf_holdings_by_tone(mf_holdings, tone_key)
                rows, month, missing_funds, aggregated_count, total_count = _build_mf_underlying_breakdown(
                    mf_holdings
                )
                payload = {
                    "rows": rows,
                    "month": month,
                    "notAggregatedFunds": missing_funds,
                    "aggregatedFundCount": aggregated_count,
                    "totalFundCount": total_count,
                    "tone": tone_key,
                    "error": "",
                    "loading": False,
                    "stale": False,
                }
            with _MF_CACHE_LOCK:
                _MF_UNDERLYINGS_CACHE_PAYLOADS[tone_key] = dict(payload)
                _MF_UNDERLYINGS_CACHE_DAY_BY_TONE[tone_key] = _today_cache_token()
            notify_dashboard_cache_refresh()
        finally:
            with _MF_CACHE_LOCK:
                _MF_UNDERLYINGS_REFRESH_IN_PROGRESS.discard(tone_key)

    if not start_background_refresh_job(f"mf-underlyings-{tone_key}", _job):
        with _MF_CACHE_LOCK:
            _MF_UNDERLYINGS_REFRESH_IN_PROGRESS.discard(tone_key)


def _get_cached_mf_underlyings_payload(kite, *, tone: str = "all") -> dict[str, Any]:
    """Return MF underlyings payload while refreshing stale/missing cache in background."""
    tone_key = _normalize_mf_underlying_tone(tone)
    day = _today_cache_token()
    with _MF_CACHE_LOCK:
        cached = dict(_MF_UNDERLYINGS_CACHE_PAYLOADS.get(tone_key) or {})
        cached_day = _MF_UNDERLYINGS_CACHE_DAY_BY_TONE.get(tone_key, "")
        refreshing = tone_key in _MF_UNDERLYINGS_REFRESH_IN_PROGRESS

    if cached and cached_day == day:
        cached["loading"] = False
        cached["stale"] = False
        return cached

    holdings_payload = _get_cached_mf_holdings_payload(kite)
    holdings_error = str(holdings_payload.get("error") or "")
    if not refreshing:
        _start_mf_underlyings_refresh(kite, tone_key=tone_key)

    if cached and cached_day != day:
        return _mf_underlyings_loading_payload(tone_key, stale_payload=cached, holdings_error=holdings_error)
    return _mf_underlyings_loading_payload(tone_key, holdings_error=holdings_error)


def _start_reference_cache_warmup() -> None:
    """Warm/refresh reference caches in background on server startup."""
    global _REFERENCE_WARMUP_IN_PROGRESS
    with _REFERENCE_WARMUP_LOCK:
        if _REFERENCE_WARMUP_IN_PROGRESS:
            return
        _REFERENCE_WARMUP_IN_PROGRESS = True

    def _job() -> None:
        global _REFERENCE_WARMUP_IN_PROGRESS
        try:
            token = load_cached_access_token()
            kite = None
            if token:
                api_key, _ = load_credentials()
                candidate = build_authenticated_client(api_key, token)
                if validate_kite_session(candidate):
                    kite = candidate
            # Warm NSE/Nifty references regardless of auth state, and force
            # background refresh jobs so startup doesn't wait for first request.
            warm_reference_caches(kite, force_refresh=True)
        except Exception as exc:
            logger.info("Reference cache warmup skipped/failed: %s", exc)
        finally:
            with _REFERENCE_WARMUP_LOCK:
                _REFERENCE_WARMUP_IN_PROGRESS = False

    if not start_background_refresh_job("web-reference-warmup", _job):
        with _REFERENCE_WARMUP_LOCK:
            _REFERENCE_WARMUP_IN_PROGRESS = False


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
    return templates.TemplateResponse(
        request,
        "index.html",
        {"dashboard_name": _DASHBOARD_DISPLAY_NAME},
    )


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
            {
                "error": f"Login was cancelled (status={status}).",
                "dashboard_name": _DASHBOARD_DISPLAY_NAME,
            },
            status_code=400,
        )

    if not request_token:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "error": "Missing request_token in callback URL.",
                "dashboard_name": _DASHBOARD_DISPLAY_NAME,
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
                "dashboard_name": _DASHBOARD_DISPLAY_NAME,
            },
            status_code=401,
        )

    save_cached_access_token(session["access_token"])
    request.session["authenticated"] = True
    _start_reference_cache_warmup()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    """Clear the browser session and remove the cached Kite access token."""
    request.session.clear()
    clear_cached_access_token()
    live_price_stream.close()
    return RedirectResponse("/", status_code=303)


@app.websocket("/ws/live-prices")
async def live_prices_websocket(websocket: WebSocket) -> None:
    """Push KiteTicker LTP deltas to the dashboard (same stream as HTML snapshots)."""
    # 1008 = policy violation (RFC 6455); used here for "not authenticated".
    if not _restore_session_if_token_valid_session(websocket.session):
        await websocket.close(code=1008)
        return
    if _kite_for_request() is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()

    loop = asyncio.get_running_loop()
    # Batches of {token: ltp} and occasional "cache" sentinel for post-refresh HTML reload.
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=512)

    # Merge tick batches on the event-loop thread (ticker runs in another thread)
    # so we send fewer WebSocket frames under busy markets.
    class _LtpCoalescer:
        __slots__ = ("flush_scheduled", "pending")

        def __init__(self) -> None:
            self.pending: dict[int, float] = {}
            self.flush_scheduled = False

    coalesce = _LtpCoalescer()

    def try_put_to_queue(batch: dict[int, float]) -> None:
        if not batch:
            return

        def _try_put() -> None:
            try:
                queue.put_nowait(batch)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(batch)
                except asyncio.QueueFull:
                    pass

        loop.call_soon_threadsafe(_try_put)

    def flush_coalesced() -> None:
        coalesce.flush_scheduled = False
        if not coalesce.pending:
            return
        batch = dict(coalesce.pending)
        coalesce.pending.clear()
        try_put_to_queue(batch)

    def enqueue_updates(updates: dict[int, float]) -> None:
        if not updates:
            return

        def merge_on_loop() -> None:
            coalesce.pending.update(updates)
            if coalesce.flush_scheduled:
                return
            coalesce.flush_scheduled = True
            loop.call_soon(flush_coalesced)

        loop.call_soon_threadsafe(merge_on_loop)

    def notify_cache_refresh() -> None:
        def _put() -> None:
            try:
                queue.put_nowait("cache")
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait("cache")
                except asyncio.QueueFull:
                    pass

        loop.call_soon_threadsafe(_put)

    live_price_stream.add_tick_listener(enqueue_updates)
    live_price_stream.add_cache_refresh_listener(notify_cache_refresh)
    try:
        while True:
            try:
                item = await queue.get()
            except asyncio.CancelledError:
                # Server shutdown (Ctrl+C) cancels waiters; exit without logging as error.
                break
            try:
                if item == "cache":
                    await websocket.send_json({"cacheRefresh": True})
                elif isinstance(item, dict):
                    updates = item
                    await websocket.send_json(
                        {"ltp": {str(tok): price for tok, price in updates.items()}}
                    )
            except WebSocketDisconnect:
                raise
            except asyncio.CancelledError:
                break
            except Exception:
                log_dashboard_ws_debug_exception(
                    logger, "WebSocket send_json failed; ending live-prices stream"
                )
                break
    except WebSocketDisconnect:
        pass
    finally:
        live_price_stream.remove_tick_listener(enqueue_updates)
        live_price_stream.remove_cache_refresh_listener(notify_cache_refresh)
        try:
            # 1001 = "going away" — helps ASGI/uvicorn finish the connection on shutdown.
            await websocket.close(code=1001)
        except (Exception, asyncio.CancelledError):
            pass


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the tabbed dashboard with all account snapshot sections."""
    request_start = time.perf_counter()
    timings: list[tuple[str, float]] = []
    if not _restore_session_if_token_valid(request):
        return RedirectResponse("/", status_code=303)
    _dashboard_timing_mark(timings, "session_restore", request_start)

    kite = _kite_for_request()
    if kite is None:
        request.session.clear()
        return RedirectResponse("/", status_code=303)
    _dashboard_timing_mark(timings, "kite_client", request_start)

    mf_error: str | None = None
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_equity = pool.submit(kite.holdings)
        future_positions = pool.submit(kite.positions)
        future_margins = pool.submit(kite.margins, "equity")
        future_market_condition = pool.submit(get_marketsmith_market_condition)

        try:
            equity_raw = future_equity.result() or []
            positions_raw = future_positions.result() or {}
            margins_raw = future_margins.result() or {}
        except TokenException:
            request.session.clear()
            live_price_stream.close()
            return RedirectResponse("/", status_code=303)

        profile_raw = _get_cached_profile(kite)
        market_condition = future_market_condition.result()
    _dashboard_timing_mark(timings, "kite_data_fetch_parallel", request_start)

    net_positions = positions_raw.get("net", []) or []
    open_net = [p for p in net_positions if int(p.get("quantity") or 0) != 0]

    live_ltp_by_token: dict[int, float] = {}
    access_token = load_cached_access_token()
    index_quote_keys = [f"NSE:{ts}" for _, ts in _DASHBOARD_INDEX_ENTRIES]
    index_tokens: set[int] = set()
    index_quotes_bootstrap: list[dict[str, Any]] = []

    equity_token_to_name, equity_symbol_to_name = get_cash_equity_name_lookups(kite)
    _dashboard_timing_mark(timings, "lookup_cash_equity_names", request_start)
    equity_token_to_kite_sector, equity_symbol_to_kite_sector = (
        get_cash_equity_kite_sector_lookups(kite)
    )
    _dashboard_timing_mark(timings, "lookup_cash_equity_kite_sectors", request_start)
    equity_token_to_isin, equity_symbol_to_isin = get_cash_equity_isin_lookups(kite)
    _dashboard_timing_mark(timings, "lookup_cash_equity_isin", request_start)
    nse_symbol_to_industry = get_nse_symbol_to_industry()
    _dashboard_timing_mark(timings, "lookup_nse_symbol_industry", request_start)
    isin_to_industry = get_isin_to_industry()
    _dashboard_timing_mark(timings, "lookup_isin_industry", request_start)
    nse_symbol_to_token = get_nse_symbol_to_token_lookup(kite)
    _dashboard_timing_mark(timings, "lookup_nse_symbol_token", request_start)
    nifty50_symbols = get_nifty50_symbols()
    _dashboard_timing_mark(timings, "lookup_nifty50_symbols", request_start)
    reference_cache_debug = get_reference_cache_debug_snapshot()
    _dashboard_timing_mark(timings, "instrument_and_reference_lookups", request_start)
    watch_quote_keys = [f"NSE:{sym}" for sym in nifty50_symbols]
    watch_tokens = {
        int(nse_symbol_to_token.get(sym) or 0)
        for sym in nifty50_symbols
        if int(nse_symbol_to_token.get(sym) or 0) > 0
    }

    quote_keys = index_quote_keys + watch_quote_keys
    quote_batch: dict[str, Any] = _get_cached_quotes(kite, quote_keys)
    _dashboard_timing_mark(timings, "quote_batch", request_start)

    for env_label, ts in _DASHBOARD_INDEX_ENTRIES:
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
            # Avoid blocking dashboard HTML render waiting for first ticks;
            # websocket updates land immediately after page load.
            live_ltp_by_token = live_price_stream.snapshot_ltp(tokens, wait_seconds=0.0)
        except Exception:
            # Keep dashboard resilient if websocket setup fails.
            live_ltp_by_token = {}
    _dashboard_timing_mark(timings, "live_price_stream_bootstrap", request_start)

    for row in index_quotes_bootstrap:
        tok = int(row.get("token") or 0)
        if tok > 0 and tok in live_ltp_by_token:
            row["ltp"] = live_ltp_by_token[tok]

    equity_holdings = sorted(
        (
            _decorate_holding(
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
            _decorate_position(
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
            _decorate_position(
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
    _dashboard_timing_mark(timings, "decorate_holdings_positions", request_start)

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
            or _DASHBOARD_DISPLAY_NAME
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
    _dashboard_timing_mark(timings, "watchlist_build", request_start)

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
        "request": request,
        "dashboard_name": _DASHBOARD_DISPLAY_NAME,
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
        "snapshot_interval_ms": _DASHBOARD_SNAPSHOT_INTERVAL_MS,
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
    _dashboard_timing_mark(timings, "context_build", request_start)
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
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/dashboard/mf-underlyings")
async def dashboard_mf_underlyings(
    request: Request,
    tone: str = "all",
) -> JSONResponse:
    """Return MF underlying aggregation as JSON (loaded lazily by the UI)."""
    if not _restore_session_if_token_valid(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    kite = _kite_for_request()
    if kite is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = _get_cached_mf_underlyings_payload(kite, tone=tone)
    except TokenException:
        request.session.clear()
        live_price_stream.close()
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(payload)


@app.get("/dashboard/mf-holdings")
async def dashboard_mf_holdings(request: Request) -> JSONResponse:
    """Return mutual fund holdings/totals as JSON (loaded lazily by MF tab)."""
    if not _restore_session_if_token_valid(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    kite = _kite_for_request()
    if kite is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = _get_cached_mf_holdings_payload(kite)
    except TokenException:
        request.session.clear()
        live_price_stream.close()
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(payload)


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
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
