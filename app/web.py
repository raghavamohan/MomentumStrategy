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
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
from collections.abc import MutableMapping
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from typing import Any
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen
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
    get_isin_to_industry,
    get_nifty50_symbols,
    get_nse_symbol_to_industry,
    get_nse_symbol_to_token_lookup,
    resolve_equity_sector,
    symbol_with_company_name,
    warm_reference_caches,
)
from app.live_prices import dashboard_ws_debug_enabled, live_price_stream


logger = logging.getLogger(__name__)
_DASHBOARD_TIMING_LOGGER = logging.getLogger("app.dashboard.timing")


TEMPLATES_DIR = PROJECT_ROOT / "templates"
SESSION_SECRET_FILE = PROJECT_ROOT / ".session_secret"
SESSION_SECRET_KEYRING_SERVICE = "MomentumStrategy"
SESSION_SECRET_KEYRING_ACCOUNT = "dashboard-session-secret"
MFDATA_BASE_URL = "https://mfdata.in"
MFDATA_HTTP_TIMEOUT_SECONDS = 20
MFDATA_MAX_FETCH_WORKERS = max(1, min(16, int(os.getenv("MFDATA_MAX_FETCH_WORKERS", "6") or "6")))
MFDATA_CACHE_FILE = PROJECT_ROOT / ".cache" / "mfdata_underlyings_cache.json"
DASHBOARD_TIMING_LOG_FILE = PROJECT_ROOT / ".cache" / "dashboard_timing.log"
_MFDATA_SEARCH_CACHE: dict[str, list[dict[str, Any]]] = {}
_MFDATA_HOLDINGS_CACHE: dict[int, dict[str, Any] | None] = {}
_MFDATA_DISK_CACHE_LOADED = False
_MFDATA_DISK_CACHE: dict[str, Any] = {"meta": {"cache_month": ""}, "search": {}, "holdings": {}}
_MFDATA_DISK_CACHE_DIRTY = False
_MFDATA_CACHE_LOCK = threading.Lock()
_REFERENCE_WARMUP_LOCK = threading.Lock()
_REFERENCE_WARMUP_IN_PROGRESS = False

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
        yield
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
    sector = _normalize_equity_sector(
        symbol,
        resolve_equity_sector(
        symbol=symbol,
        exchange=str(h.get("exchange", "")),
        instrument_token=int(h.get("instrument_token") or 0),
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
    return {
        "tradingsymbol": symbol,
        "symbol_label": symbol_label,
        "sector": sector,
        "exchange": h.get("exchange", ""),
        "instrument_token": int(h.get("instrument_token") or 0),
        "quantity": quantity,
        "average_price": avg,
        "last_price": ltp,
        "close_price": close_price,
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


def _normalize_match_text(value: str) -> str:
    """Lowercase text with compact spacing for fuzzy scheme-name matching."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return " ".join(cleaned.split())


def _canonicalize_mf_scheme_name(value: str) -> str:
    """Drop plan/option tokens so scheme names map reliably."""
    normalized = _normalize_match_text(value)
    drop_tokens = {
        "direct",
        "regular",
        "growth",
        "plan",
        "option",
        "idcw",
        "dividend",
        "payout",
        "reinvestment",
        "reinvest",
        "bonus",
        "inst",
        "institutional",
    }
    kept = [token for token in normalized.split() if token not in drop_tokens]
    return " ".join(kept)


def _parse_pct(value: Any) -> float:
    """Parse percentage-like strings to float; returns 0 on malformed input."""
    if value is None:
        return 0.0
    raw = str(value).strip().replace("%", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _current_month_token() -> str:
    """Return current month token used for mfdata cache invalidation."""
    return time.strftime("%Y-%m")


def _save_mfdata_disk_cache_locked() -> None:
    """Persist mfdata cache to local JSON file (expects lock held)."""
    MFDATA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MFDATA_CACHE_FILE.write_text(
        json.dumps(_MFDATA_DISK_CACHE, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _prepare_mfdata_cache_locked() -> None:
    """Load cache from disk and rotate monthly if stale (expects lock held)."""
    global _MFDATA_DISK_CACHE_LOADED, _MFDATA_DISK_CACHE, _MFDATA_DISK_CACHE_DIRTY
    if not _MFDATA_DISK_CACHE_LOADED:
        if MFDATA_CACHE_FILE.exists():
            try:
                loaded = json.loads(MFDATA_CACHE_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                _MFDATA_DISK_CACHE = {
                    "meta": loaded.get("meta") if isinstance(loaded.get("meta"), dict) else {"cache_month": ""},
                    "search": loaded.get("search") if isinstance(loaded.get("search"), dict) else {},
                    "holdings": loaded.get("holdings") if isinstance(loaded.get("holdings"), dict) else {},
                }
        _MFDATA_DISK_CACHE_LOADED = True

    current_month = _current_month_token()
    cached_month = str((_MFDATA_DISK_CACHE.get("meta") or {}).get("cache_month") or "")
    if cached_month == current_month:
        return

    _MFDATA_DISK_CACHE["meta"] = {"cache_month": current_month}
    _MFDATA_DISK_CACHE["search"] = {}
    _MFDATA_DISK_CACHE["holdings"] = {}
    _MFDATA_SEARCH_CACHE.clear()
    _MFDATA_HOLDINGS_CACHE.clear()
    _MFDATA_DISK_CACHE_DIRTY = True
    try:
        _save_mfdata_disk_cache_locked()
        _MFDATA_DISK_CACHE_DIRTY = False
    except OSError:
        pass


def _flush_mfdata_disk_cache() -> None:
    """Persist mfdata cache once if there are pending updates."""
    global _MFDATA_DISK_CACHE_DIRTY
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        if not _MFDATA_DISK_CACHE_DIRTY:
            return
        try:
            _save_mfdata_disk_cache_locked()
            _MFDATA_DISK_CACHE_DIRTY = False
        except OSError:
            pass


def _mfdata_json_get(path: str, query: dict[str, Any] | None = None) -> Any:
    """GET JSON payload from mfdata.in API."""
    url = f"{MFDATA_BASE_URL}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    req = URLRequest(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "MomentumStrategy/1.0 (+local-dashboard)",
        },
    )
    with urlopen(req, timeout=MFDATA_HTTP_TIMEOUT_SECONDS) as resp:
        payload = resp.read().decode("utf-8", errors="replace")
    return json.loads(payload)


def _mfdata_search_fund(fund_name: str) -> list[dict[str, Any]]:
    """Search fund variants on mfdata.in (monthly disk cache + memory cache)."""
    key = _normalize_match_text(fund_name)
    if not key:
        return []
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        cached_mem = _MFDATA_SEARCH_CACHE.get(key)
        if cached_mem is not None:
            return list(cached_mem)
        cached_disk = (_MFDATA_DISK_CACHE.get("search") or {}).get(key)
        if isinstance(cached_disk, list):
            rows = [row for row in cached_disk if isinstance(row, dict)]
            _MFDATA_SEARCH_CACHE[key] = rows
            return list(rows)
    try:
        payload = _mfdata_json_get("/api/v1/search", {"q": fund_name})
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        payload = {}
    rows = payload.get("data") if isinstance(payload, dict) else []
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    global _MFDATA_DISK_CACHE_DIRTY
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        _MFDATA_SEARCH_CACHE[key] = rows
        (_MFDATA_DISK_CACHE.get("search") or {})[key] = rows
        _MFDATA_DISK_CACHE_DIRTY = True
    return rows


def _mfdata_holdings_for_family(family_id: int) -> dict[str, Any] | None:
    """Fetch holdings for one mfdata family (monthly disk cache + memory cache)."""
    family_key = str(int(family_id))
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        if family_id in _MFDATA_HOLDINGS_CACHE:
            return _MFDATA_HOLDINGS_CACHE[family_id]
        holdings_disk = (_MFDATA_DISK_CACHE.get("holdings") or {})
        if family_key in holdings_disk:
            cached_disk = holdings_disk.get(family_key)
            result = cached_disk if isinstance(cached_disk, dict) else None
            _MFDATA_HOLDINGS_CACHE[family_id] = result
            return result
    try:
        payload = _mfdata_json_get(f"/api/v1/families/{family_id}/holdings")
    except HTTPError as exc:
        if exc.code == 404:
            payload = {}
        else:
            raise
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        payload = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    result = data if isinstance(data, dict) else None
    global _MFDATA_DISK_CACHE_DIRTY
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        _MFDATA_HOLDINGS_CACHE[family_id] = result
        (_MFDATA_DISK_CACHE.get("holdings") or {})[family_key] = result
        _MFDATA_DISK_CACHE_DIRTY = True
    return result


def _rank_mfdata_variants(fund_name: str, variants: list[dict[str, Any]]) -> list[int]:
    """Rank candidate mfdata family IDs for the broker fund name."""
    canonical_fund = _canonicalize_mf_scheme_name(fund_name)
    if not canonical_fund:
        return []
    fund_tokens = set(canonical_fund.split())
    scored: list[tuple[float, int]] = []
    seen_family_ids: set[int] = set()
    for row in variants:
        family_id = int(row.get("family_id") or 0)
        if family_id <= 0 or family_id in seen_family_ids:
            continue
        seen_family_ids.add(family_id)
        candidate_name = str(row.get("name") or "").strip()
        candidate = _canonicalize_mf_scheme_name(candidate_name)
        if not candidate:
            continue
        candidate_tokens = set(candidate.split())
        overlap = len(fund_tokens & candidate_tokens)
        score = float(overlap * 10)
        if canonical_fund in candidate or candidate in canonical_fund:
            score += 5
        score += SequenceMatcher(None, canonical_fund, candidate).ratio()
        # Prefer growth-like variants when tie-breakers are close.
        option_type = str(row.get("option_type") or "").lower()
        plan_type = str(row.get("plan_type") or "").lower()
        if "growth" in candidate_name.lower() or "growth" in option_type:
            score += 0.25
        if plan_type == "direct":
            score += 0.05
        scored.append((score, family_id))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [family_id for _, family_id in scored]


def _build_mf_underlying_breakdown(
    mf_holdings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, list[str], int, int]:
    """Combine all MF holdings into one instrument/sector weighted view via mfdata."""
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
        unique_count = len(fund_current_by_name)
        return [], "", [], 0, unique_count

    combined: dict[tuple[str, str], float] = {}
    used_months: set[str] = set()
    not_aggregated: list[str] = []
    aggregated_funds: set[str] = set()
    all_funds: set[str] = set(fund_current_by_name)

    def _resolve_fund_equity_rows(fund_name: str) -> dict[str, Any] | None:
        variants = _mfdata_search_fund(fund_name)
        family_candidates = _rank_mfdata_variants(fund_name, variants)
        for family_id in family_candidates:
            try:
                payload = _mfdata_holdings_for_family(family_id)
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
                payload = None
            equity_rows = (payload or {}).get("equity_holdings") if isinstance(payload, dict) else []
            if equity_rows:
                return payload
        return None

    fund_items = list(fund_current_by_name.items())
    fund_payload_by_name: dict[str, dict[str, Any] | None] = {}
    worker_count = min(MFDATA_MAX_FETCH_WORKERS, max(1, len(fund_items)))
    if fund_items:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            payloads = pool.map(lambda item: _resolve_fund_equity_rows(item[0]), fund_items)
            for (fund_name, _), payload in zip(fund_items, payloads, strict=False):
                fund_payload_by_name[fund_name] = payload

    for fund_name, fund_current in fund_items:
        if fund_current <= 0:
            not_aggregated.append(fund_name)
            continue

        selected_holdings = fund_payload_by_name.get(fund_name)
        if not selected_holdings:
            not_aggregated.append(fund_name)
            continue

        month = str(selected_holdings.get("month") or "").strip()
        if month:
            used_months.add(month)
        fund_weight = fund_current / total_current
        for row in selected_holdings.get("equity_holdings") or []:
            instrument = str(row.get("stock_name") or row.get("isin") or "").strip() or "Unknown"
            sector = str(
                row.get("sector") or "Unspecified"
            ).strip() or "Unspecified"
            instrument_weight = max(
                0.0,
                _parse_pct(
                    row.get("weight_pct")
                    or row.get("weight")
                ),
            )
            overall_weight = fund_weight * (instrument_weight / 100.0)
            if overall_weight <= 0:
                continue
            key = (instrument, sector)
            combined[key] = combined.get(key, 0.0) + overall_weight
        aggregated_funds.add(fund_name)

    table_rows = [
        {
            "instrument": instrument,
            "sector": sector,
            "overall_weight": weight * 100.0,
        }
        for (instrument, sector), weight in combined.items()
    ]
    table_rows.sort(
        key=lambda row: (float(row.get("overall_weight") or 0.0), str(row.get("instrument") or "").lower()),
        reverse=True,
    )
    sorted_months = sorted(used_months, reverse=True)
    latest_month = sorted_months[0] if sorted_months else ""
    # Deduplicate while preserving input order.
    seen_missing: set[str] = set()
    missing_unique = [name for name in not_aggregated if not (name in seen_missing or seen_missing.add(name))]
    _flush_mfdata_disk_cache()
    return table_rows, latest_month, missing_unique, len(aggregated_funds), len(all_funds)


def _normalize_equity_sector(symbol: str, sector: str) -> str:
    """Override sector for selected ETFs and keep a safe fallback."""
    compact = "".join(ch for ch in symbol.upper() if ch.isalnum())
    if compact in {"GOLDBEES", "GOLDETF", "GOLDSHARE"}:
        return "Gold"
    if compact in {"LIQUIDBEES", "LIQUIDBESS", "LIQUIDETF"}:
        return "Debt"
    return (sector or "").strip() or "Uncategorized"


def _summarise_equity_by_sector(rows: list[dict]) -> list[dict]:
    """Aggregate equity holdings by sector for dashboard display."""
    bucket: dict[str, dict[str, float]] = {}
    for row in rows:
        sector = str(row.get("sector") or "Uncategorized").strip() or "Uncategorized"
        entry = bucket.setdefault(
            sector,
            {"sector": sector, "invested": 0.0, "current": 0.0, "pnl": 0.0},
        )
        entry["invested"] += float(row.get("invested") or 0.0)
        entry["current"] += float(row.get("current") or 0.0)
        entry["pnl"] += float(row.get("pnl") or 0.0)
    return sorted(
        bucket.values(),
        key=lambda r: (float(r["invested"]), str(r["sector"]).lower()),
        reverse=True,
    )


def _equity_sector_breakdown(rows: list[dict]) -> dict[str, list[dict] | dict[str, float]]:
    """Split holdings into top buckets and equity subsectors.

    Top buckets are Gold, Debt, and Equity. Equity is the sum of all
    non-Gold/non-Debt sectors.
    """
    sector_rows = _summarise_equity_by_sector(rows)
    gold = {"sector": "Gold", "invested": 0.0, "current": 0.0, "pnl": 0.0}
    debt = {"sector": "Debt", "invested": 0.0, "current": 0.0, "pnl": 0.0}
    equity_subsectors: list[dict] = []

    for row in sector_rows:
        sector_name = str(row.get("sector") or "").strip().lower()
        if sector_name == "gold":
            gold = row
        elif sector_name == "debt":
            debt = row
        else:
            equity_subsectors.append(row)

    equity = _summarise(equity_subsectors, "invested", "current", "pnl")
    return {
        "top_level": [
            {"sector": "Debt", **debt},
            {"sector": "Gold", **gold},
            {"sector": "Equity", **equity},
        ],
        "equity_subsectors": equity_subsectors,
    }


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
    qty = int(p.get("quantity") or 0)
    ltp = float(p.get("last_price") or 0.0)
    close_price = float(p.get("close_price") or 0.0)
    multiplier = float(p.get("multiplier") or 1.0)
    live_ltp_applied = bool(p.get("_live_ltp_applied"))
    if live_ltp_applied:
        buy_value = float(p.get("buy_value") or 0.0)
        sell_value = float(p.get("sell_value") or 0.0)
        pnl = (sell_value - buy_value) + (qty * ltp * multiplier)
        m2m = (ltp - close_price) * qty * multiplier if close_price > 0 else float(p.get("m2m") or 0.0)
    else:
        pnl = float(p.get("pnl") or 0.0)
        m2m = float(p.get("m2m") or 0.0)
    symbol = str(p.get("tradingsymbol", "")).strip()
    symbol_label = symbol_with_company_name(
        symbol=symbol,
        exchange=str(p.get("exchange", "")),
        instrument_token=int(p.get("instrument_token") or 0),
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
    )
    sector = _normalize_equity_sector(
        symbol,
        resolve_equity_sector(
        symbol=symbol,
        exchange=str(p.get("exchange", "")),
        instrument_token=int(p.get("instrument_token") or 0),
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
    return {
        "tradingsymbol": symbol,
        "symbol_label": symbol_label,
        "sector": sector,
        "exchange": p.get("exchange", ""),
        "product": p.get("product", ""),
        "instrument_token": int(p.get("instrument_token") or 0),
        "quantity": qty,
        "average_price": float(p.get("average_price") or 0.0),
        "last_price": ltp,
        "buy_value": float(p.get("buy_value") or 0.0),
        "sell_value": float(p.get("sell_value") or 0.0),
        "multiplier": multiplier,
        "close_price": close_price,
        "pnl": pnl,
        "m2m": m2m,
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


def _dashboard_timing_mark(
    timings: list[tuple[str, float]],
    stage: str,
    start_time: float,
) -> None:
    """Append elapsed milliseconds since ``start_time`` for one stage."""
    timings.append((stage, (time.perf_counter() - start_time) * 1000.0))


def _start_reference_cache_warmup() -> None:
    """Warm heavy instrument/NSE caches in background when a token is available."""
    global _REFERENCE_WARMUP_IN_PROGRESS
    with _REFERENCE_WARMUP_LOCK:
        if _REFERENCE_WARMUP_IN_PROGRESS:
            return
        _REFERENCE_WARMUP_IN_PROGRESS = True

    def _job() -> None:
        global _REFERENCE_WARMUP_IN_PROGRESS
        try:
            token = load_cached_access_token()
            if not token:
                return
            api_key, _ = load_credentials()
            kite = build_authenticated_client(api_key, token)
            if not validate_kite_session(kite):
                return
            warm_reference_caches(kite)
        except Exception as exc:
            logger.info("Reference cache warmup skipped/failed: %s", exc)
        finally:
            with _REFERENCE_WARMUP_LOCK:
                _REFERENCE_WARMUP_IN_PROGRESS = False

    threading.Thread(target=_job, daemon=True).start()


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
    queue: asyncio.Queue[dict[int, float]] = asyncio.Queue(maxsize=512)

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

    live_price_stream.add_tick_listener(enqueue_updates)
    try:
        while True:
            try:
                updates = await queue.get()
            except asyncio.CancelledError:
                # Server shutdown (Ctrl+C) cancels waiters; exit without logging as error.
                break
            try:
                await websocket.send_json(
                    {"ltp": {str(tok): price for tok, price in updates.items()}}
                )
            except WebSocketDisconnect:
                raise
            except asyncio.CancelledError:
                break
            except Exception:
                if dashboard_ws_debug_enabled():
                    logger.exception("WebSocket send_json failed; ending live-prices stream")
                break
    except WebSocketDisconnect:
        pass
    finally:
        live_price_stream.remove_tick_listener(enqueue_updates)
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
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_equity = pool.submit(kite.holdings)
        future_positions = pool.submit(kite.positions)
        future_margins = pool.submit(kite.margins, "equity")
        future_mf = pool.submit(kite.mf_holdings)
        future_profile = pool.submit(kite.profile)

        try:
            equity_raw = future_equity.result() or []
            positions_raw = future_positions.result() or {}
            margins_raw = future_margins.result() or {}
        except TokenException:
            request.session.clear()
            live_price_stream.close()
            return RedirectResponse("/", status_code=303)

        try:
            mf_raw = future_mf.result() or []
        except PermissionException:
            mf_raw = []
            mf_error = (
                "Mutual Funds API is not enabled on this Kite Connect app. "
                "Enable the MF module at https://developers.kite.trade if you "
                "want this section."
            )
        except TokenException:
            request.session.clear()
            live_price_stream.close()
            return RedirectResponse("/", status_code=303)
        except Exception:
            mf_raw = []

        try:
            profile_raw = future_profile.result() or {}
        except Exception:
            profile_raw = {}
    _dashboard_timing_mark(timings, "kite_data_fetch_parallel", request_start)

    net_positions = positions_raw.get("net", []) or []
    open_net = [p for p in net_positions if int(p.get("quantity") or 0) != 0]

    live_ltp_by_token: dict[int, float] = {}
    access_token = load_cached_access_token()
    index_quote_keys = [f"NSE:{ts}" for _, ts in _DASHBOARD_INDEX_ENTRIES]
    index_tokens: set[int] = set()
    index_quotes_bootstrap: list[dict[str, Any]] = []

    equity_token_to_name, equity_symbol_to_name = get_cash_equity_name_lookups(kite)
    equity_token_to_kite_sector, equity_symbol_to_kite_sector = (
        get_cash_equity_kite_sector_lookups(kite)
    )
    equity_token_to_isin, equity_symbol_to_isin = get_cash_equity_isin_lookups(kite)
    nse_symbol_to_industry = get_nse_symbol_to_industry()
    isin_to_industry = get_isin_to_industry()
    nse_symbol_to_token = get_nse_symbol_to_token_lookup(kite)
    nifty50_symbols = get_nifty50_symbols()
    _dashboard_timing_mark(timings, "instrument_and_reference_lookups", request_start)
    watch_quote_keys = [f"NSE:{sym}" for sym in nifty50_symbols]
    watch_tokens = {
        int(nse_symbol_to_token.get(sym) or 0)
        for sym in nifty50_symbols
        if int(nse_symbol_to_token.get(sym) or 0) > 0
    }

    quote_keys = index_quote_keys + watch_quote_keys
    try:
        quote_batch: dict[str, Any] = kite.quote(quote_keys) if quote_keys else {}
    except Exception:
        quote_batch = {}
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
            live_ltp_by_token = live_price_stream.snapshot_ltp(tokens)
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
                _overlay_live_ltp(h, live_ltp_by_token),
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
                _overlay_live_ltp(p, live_ltp_by_token),
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

    equity_totals = _summarise(equity_holdings, "invested", "current", "pnl")
    equity_sector_breakdown = _equity_sector_breakdown(equity_holdings)
    equity_all_sector_summary = _summarise_equity_by_sector(equity_holdings)
    mf_totals = _summarise(mf_holdings, "invested", "current", "pnl")
    equity_position_totals = _summarise(equity_positions, "pnl", "m2m")
    fno_position_totals = _summarise(fno_positions, "pnl", "m2m")

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
    }
    context = {
        "request": request,
        "dashboard_name": _DASHBOARD_DISPLAY_NAME,
        "equity_holdings": equity_holdings,
        "equity_totals": equity_totals,
        "equity_sector_summary": equity_sector_breakdown["top_level"],
        "equity_subsector_summary": equity_sector_breakdown["equity_subsectors"],
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
    }
    _dashboard_timing_mark(timings, "context_build", request_start)
    total_ms = (time.perf_counter() - request_start) * 1000.0
    timings_str = ", ".join(f"{name}={ms:.1f}ms" for name, ms in timings)
    logger.info("dashboard timing total=%.1fms | %s", total_ms, timings_str)
    _DASHBOARD_TIMING_LOGGER.info("dashboard timing total=%.1fms | %s", total_ms, timings_str)
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/dashboard/mf-underlyings")
async def dashboard_mf_underlyings(request: Request) -> JSONResponse:
    """Return MF underlying aggregation as JSON (loaded lazily by the UI)."""
    if not _restore_session_if_token_valid(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    kite = _kite_for_request()
    if kite is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        mf_raw = kite.mf_holdings() or []
    except PermissionException:
        return JSONResponse(
            {
                "rows": [],
                "month": "",
                "notAggregatedFunds": [],
                "aggregatedFundCount": 0,
                "totalFundCount": 0,
                "error": (
                    "Mutual Funds API is not enabled on this Kite Connect app. "
                    "Enable the MF module at https://developers.kite.trade if you "
                    "want this section."
                ),
            }
        )
    except TokenException:
        request.session.clear()
        live_price_stream.close()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    mf_holdings = sorted(
        (_decorate_mf(h) for h in mf_raw),
        key=lambda r: r["fund"],
    )
    rows, month, missing_funds, aggregated_count, total_count = _build_mf_underlying_breakdown(
        mf_holdings
    )
    return JSONResponse(
        {
            "rows": rows,
            "month": month,
            "notAggregatedFunds": missing_funds,
            "aggregatedFundCount": aggregated_count,
            "totalFundCount": total_count,
            "error": "",
        }
    )


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
