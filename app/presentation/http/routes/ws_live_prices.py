"""WebSocket stream for live LTP updates."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.env_util import log_dashboard_ws_debug_exception
from app.infrastructure.live_prices import live_price_stream
from app.infrastructure.tick_hub import tick_hub
from app.presentation.http.server_auth import kite_for_request, restore_session_if_token_valid_session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/live-prices")
async def live_prices_websocket(websocket: WebSocket) -> None:
    """Push KiteTicker LTP deltas to the dashboard (same stream as HTML snapshots)."""
    if not restore_session_if_token_valid_session(websocket.session):
        await websocket.close(code=1008)
        return
    if kite_for_request() is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=512)

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

    tick_hub.subscribe(enqueue_updates)
    live_price_stream.add_cache_refresh_listener(notify_cache_refresh)
    try:
        while True:
            try:
                item = await queue.get()
            except asyncio.CancelledError:
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
        tick_hub.unsubscribe(enqueue_updates)
        live_price_stream.remove_cache_refresh_listener(notify_cache_refresh)
        try:
            await websocket.close(code=1001)
        except (Exception, asyncio.CancelledError):
            pass
