"""WebSocket stream for full MODE_FULL tick data for the live stock chart.

Endpoint: GET /ws/chart-ticks?instrument_token=TOKEN

Pushes every KiteTicker tick for the requested token as a JSON payload with
all available MODE_FULL fields (LTP, OHLC, volume, depth, etc.).  The chart
page uses this to update the live current candle, market depth overlay, and
any other real-time overlays.

Payload shape sent to browser::

    {
        "t": "tick",
        "token": 738561,
        "ltp": 23450.50,
        "ltq": 10,
        "avg": 23420.10,
        "vol": 5123456,
        "buyQty": 84230,
        "sellQty": 91045,
        "change": 0.76,
        "ohlc": {"o": 23300, "h": 23510, "l": 23260, "c": 23380},
        "depth": {
            "buy":  [{"p": 23450, "q": 150, "ord": 5}, ...],
            "sell": [{"p": 23451, "q": 200, "ord": 8}, ...]
        },
        "ts": 1715676312000   // last_trade_time as Unix ms (UTC)
    }
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.env_util import log_dashboard_ws_debug_exception
from app.infrastructure.live_prices import live_price_stream
from app.infrastructure.tick_hub import tick_hub
from app.presentation.http.server_auth import kite_for_request, restore_session_if_token_valid_session

logger = logging.getLogger(__name__)

router = APIRouter()

# Coalesce window: flush at most every N milliseconds per token
_FLUSH_MS = 100


def _serialize_depth(depth: object) -> dict:
    """Serialize Kite depth dict to compact JSON-safe form."""
    if not isinstance(depth, dict):
        return {"buy": [], "sell": []}

    def _side(entries: object) -> list[dict]:
        if not isinstance(entries, list):
            return []
        out = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            out.append({
                "p": float(e.get("price") or 0),
                "q": int(e.get("quantity") or 0),
                "ord": int(e.get("orders") or 0),
            })
        return out

    return {
        "buy": _side(depth.get("buy")),
        "sell": _side(depth.get("sell")),
    }


def _serialize_ohlc(ohlc: object) -> dict:
    if not isinstance(ohlc, dict):
        return {"o": 0, "h": 0, "l": 0, "c": 0}
    return {
        "o": float(ohlc.get("open") or 0),
        "h": float(ohlc.get("high") or 0),
        "l": float(ohlc.get("low") or 0),
        "c": float(ohlc.get("close") or 0),
    }


def _ts_ms(last_trade_time: object) -> int:
    """Convert Kite last_trade_time (datetime or None) to Unix ms (UTC)."""
    if isinstance(last_trade_time, datetime):
        # Make timezone-aware if naive (Kite returns IST aware or naive)
        if last_trade_time.tzinfo is None:
            last_trade_time = last_trade_time.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return int(last_trade_time.astimezone(timezone.utc).timestamp() * 1000)
    # Fallback: current time
    return int(time.time() * 1000)


def _build_chart_payload(token: int, tick: dict) -> dict:
    """Convert a raw KiteTicker tick dict to the compact chart WebSocket payload."""
    return {
        "t": "tick",
        "token": token,
        "ltp": float(tick.get("last_price") or 0),
        "ltq": int(tick.get("last_quantity") or 0),
        "avg": float(tick.get("average_price") or 0),
        "vol": int(tick.get("volume") or 0),
        "buyQty": int(tick.get("buy_quantity") or 0),
        "sellQty": int(tick.get("sell_quantity") or 0),
        "change": float(tick.get("change") or 0),
        "ohlc": _serialize_ohlc(tick.get("ohlc")),
        "depth": _serialize_depth(tick.get("depth")),
        "ts": _ts_ms(tick.get("last_trade_time")),
    }


@router.websocket("/ws/chart-ticks")
async def chart_ticks_websocket(
    websocket: WebSocket,
    instrument_token: int = Query(..., ge=1),
) -> None:
    """Push MODE_FULL tick data for one instrument to the live stock chart."""
    if not restore_session_if_token_valid_session(websocket.session):
        await websocket.close(code=1008)
        return
    if kite_for_request() is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    # Ensure this token is subscribed on the shared Kite WebSocket (MODE_FULL)
    live_price_stream.subscribe({instrument_token})

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)

    # Coalescer: keep only the latest tick within the flush window
    class _Coalescer:
        __slots__ = ("pending", "flush_scheduled")
        def __init__(self) -> None:
            self.pending: dict | None = None
            self.flush_scheduled = False

    coal = _Coalescer()

    def _flush() -> None:
        coal.flush_scheduled = False
        payload = coal.pending
        coal.pending = None
        if payload is None:
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def _on_tick(updates: dict[int, dict]) -> None:
        tick = updates.get(instrument_token)
        if tick is None:
            return
        payload = _build_chart_payload(instrument_token, tick)

        def _merge() -> None:
            coal.pending = payload
            if coal.flush_scheduled:
                return
            coal.flush_scheduled = True
            loop.call_later(_FLUSH_MS / 1000.0, _flush)

        loop.call_soon_threadsafe(_merge)

    tick_hub.subscribe(_on_tick)
    try:
        while True:
            try:
                payload = await queue.get()
            except asyncio.CancelledError:
                break
            try:
                await websocket.send_json(payload)
            except WebSocketDisconnect:
                raise
            except asyncio.CancelledError:
                break
            except Exception:
                log_dashboard_ws_debug_exception(
                    logger, "chart-ticks send_json failed; ending stream"
                )
                break
    except WebSocketDisconnect:
        pass
    finally:
        tick_hub.unsubscribe(_on_tick)
        try:
            await websocket.close(code=1001)
        except (Exception, asyncio.CancelledError):
            pass
