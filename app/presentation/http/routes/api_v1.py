"""Versioned JSON API for external clients."""

from __future__ import annotations

import json
import threading
import time

from fastapi import APIRouter, Request, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.application.dashboard_view_model import (
    build_dashboard_view_model,
    build_watch_list_rows,
    normalise_watchlist_symbols,
    session_watchlist_symbols,
)
from app.domain.reference_snapshot import build_reference_snapshot
from app.infrastructure.auth import load_cached_access_token, load_credentials
from app.infrastructure.live_prices import live_price_stream
from app.infrastructure.services.dashboard_caches import get_cached_quotes
from app.presentation.http.server_auth import authorized_browser_or_api
from app.presentation.http.server_auth import kite_for_request

router = APIRouter()
_SEARCH_INDEX_TTL_SECONDS = 300.0
_SEARCH_INDEX_LOCK = threading.Lock()
_SEARCH_INDEX_EXPIRES_AT = 0.0
_SEARCH_INDEX_ROWS: list[dict[str, str]] = []

class WatchlistUpdate(BaseModel):
    symbols: list[str]


def _get_instrument_search_index(kite) -> list[dict[str, str]]:
    """Return cached lowercase instrument search rows for fast query matching."""
    global _SEARCH_INDEX_EXPIRES_AT, _SEARCH_INDEX_ROWS
    now = time.monotonic()
    with _SEARCH_INDEX_LOCK:
        if _SEARCH_INDEX_ROWS and now < _SEARCH_INDEX_EXPIRES_AT:
            return _SEARCH_INDEX_ROWS

    ref_snap = build_reference_snapshot(kite)
    rows: list[dict[str, str]] = []
    for (exchange, symbol), name in ref_snap.kite.symbol_to_name.items():
        exchange_u = str(exchange or "").strip().upper()
        symbol_s = str(symbol or "").strip().upper()
        name_s = str(name or "").strip()
        if exchange_u not in {"NSE", "BSE"} or not symbol_s:
            continue
        rows.append(
            {
                "exchange": exchange_u,
                "symbol": symbol_s,
                "name": name_s,
                "key": f"{exchange_u}:{symbol_s}",
                "symbol_lc": symbol_s.lower(),
                "name_lc": name_s.lower(),
            }
        )

    with _SEARCH_INDEX_LOCK:
        _SEARCH_INDEX_ROWS = rows
        _SEARCH_INDEX_EXPIRES_AT = time.monotonic() + _SEARCH_INDEX_TTL_SECONDS
    return _SEARCH_INDEX_ROWS


@router.get("/api/v1/health")
async def api_v1_health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/v1/portfolio/snapshot")
async def api_v1_portfolio_snapshot(request: Request) -> JSONResponse:
    """Same dashboard data as HTML page; use ``Authorization: Bearer <access_token>`` from CLI."""
    context, redirect = await build_dashboard_view_model(request, allow_bearer=True)
    if redirect is not None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    assert context is not None
    snap = {k: v for k, v in context.items() if k not in ("request", "dashboard_bootstrap_json")}
    raw_boot = context.get("dashboard_bootstrap_json")
    if isinstance(raw_boot, str):
        try:
            snap["dashboard_bootstrap"] = json.loads(raw_boot)
        except json.JSONDecodeError:
            snap["dashboard_bootstrap"] = {}
    return JSONResponse(jsonable_encoder(snap))

@router.get("/api/v1/instruments/search")
async def api_v1_instruments_search(request: Request, q: str = Query("")) -> JSONResponse:
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    q = q.strip().lower()
    if len(q) < 2:
        return JSONResponse([])

    kite = kite_for_request()
    if not kite:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    rows = _get_instrument_search_index(kite)
    symbol_prefix: list[dict[str, str]] = []
    symbol_contains: list[dict[str, str]] = []
    name_contains: list[dict[str, str]] = []
    max_results = 20

    for row in rows:
        if row["symbol_lc"].startswith(q):
            symbol_prefix.append(row)
        elif q in row["symbol_lc"]:
            symbol_contains.append(row)
        elif q in row["name_lc"]:
            name_contains.append(row)
        if len(symbol_prefix) >= max_results:
            break

    ranked = (symbol_prefix + symbol_contains + name_contains)[:max_results]
    results = [
        {
            "exchange": item["exchange"],
            "symbol": item["symbol"],
            "name": item["name"],
            "key": item["key"],
        }
        for item in ranked
    ]
    return JSONResponse(results)


@router.post("/api/v1/watchlist")
async def api_v1_update_watchlist(request: Request, data: WatchlistUpdate) -> JSONResponse:
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    kite = kite_for_request()
    if not kite:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    symbols = normalise_watchlist_symbols(data.symbols)
    if symbols:
        valid_keys = {row["key"] for row in _get_instrument_search_index(kite)}
        symbols = [key for key in symbols if key in valid_keys]
    request.session["watchlist"] = symbols
    return JSONResponse({"status": "ok", "symbols": symbols})


@router.get("/api/v1/watchlist")
async def api_v1_get_watchlist(request: Request) -> JSONResponse:
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    kite = kite_for_request()
    if not kite:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    custom_watchlist = session_watchlist_symbols(request.session)
    quote_batch = get_cached_quotes(kite, custom_watchlist) or {}
    ref_snap = build_reference_snapshot(kite)

    token_to_name = ref_snap.kite.token_to_name
    symbol_to_name = ref_snap.kite.symbol_to_name
    token_to_kite_sector = ref_snap.kite.token_to_kite_sector
    symbol_to_kite_sector = ref_snap.kite.symbol_to_kite_sector
    token_to_isin = ref_snap.kite.token_to_isin
    symbol_to_isin = ref_snap.kite.symbol_to_isin
    nse_symbol_to_industry = ref_snap.nse.symbol_to_industry
    isin_to_industry = ref_snap.nse.isin_to_industry
    nse_symbol_to_token = ref_snap.kite.nse_symbol_to_token

    watch_tokens = set()
    for item in custom_watchlist:
        exch, symbol = item.split(":", 1) if ":" in item else ("NSE", item)
        qkey = f"{exch}:{symbol}"
        tok = int((quote_batch.get(qkey) or {}).get("instrument_token") or 0)
        if tok <= 0 and exch == "NSE":
            tok = int(nse_symbol_to_token.get(symbol) or 0)
        if tok > 0:
            watch_tokens.add(tok)

    live_ltp_by_token: dict[int, float] = {}
    access_token = load_cached_access_token()
    if access_token and watch_tokens:
        try:
            api_key, _ = load_credentials()
            live_price_stream.ensure_running(api_key, access_token)
            live_price_stream.subscribe(watch_tokens)
            live_ltp_by_token = live_price_stream.snapshot_ltp(watch_tokens)
        except Exception:
            live_ltp_by_token = {}

    rows = build_watch_list_rows(
        custom_watchlist,
        quote_batch,
        live_ltp_by_token,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
        token_to_kite_sector=token_to_kite_sector,
        symbol_to_kite_sector=symbol_to_kite_sector,
        nse_symbol_to_industry=nse_symbol_to_industry,
        isin_to_industry=isin_to_industry,
        token_to_isin=token_to_isin,
        symbol_to_isin=symbol_to_isin,
        nse_symbol_to_token=nse_symbol_to_token,
    )
    return JSONResponse({"items": jsonable_encoder(rows), "symbols": custom_watchlist})
