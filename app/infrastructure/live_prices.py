"""Live market prices via Kite WebSocket (KiteTicker).

This module keeps a single websocket connection per process and exposes a
thread-safe interface to:

* subscribe to instrument tokens shown on the dashboard, and
* read the latest streamed LTP for those tokens.

Reference:
https://kite.trade/docs/connect/v3/websocket/
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from kiteconnect import KiteTicker

from app.env_util import dashboard_ws_debug_enabled, log_dashboard_ws_debug_exception
from app.infrastructure.state_store import state_store
from app.infrastructure.tick_hub import tick_hub

logger = logging.getLogger(__name__)


if not dashboard_ws_debug_enabled():
    # pykiteconnect logs every failed/retry at ERROR on kiteconnect.ticker — suppress unless debug.
    logging.getLogger("kiteconnect.ticker").setLevel(logging.CRITICAL)

CacheRefreshListener = Callable[[], None]


def _positive_instrument_tokens(tokens: set[int]) -> set[int]:
    return {int(t) for t in tokens if int(t) > 0}


def _kite_ws_reason_text(reason: object) -> str:
    if reason is None:
        return ""
    if isinstance(reason, (bytes, bytearray)):
        return bytes(reason).decode("utf-8", errors="replace")
    return str(reason)


def _kite_ws_auth_failure(code: object, reason: object) -> bool:
    """True if Kite WebSocket failure is auth / token (stop reconnect storm)."""
    text = f"{code} {_kite_ws_reason_text(reason)}".strip()
    lower = text.lower()
    return (
        "403" in text
        or "forbidden" in lower
        or ("invalid" in lower and "token" in lower)
        or ("incorrect" in lower and "access_token" in lower)
    )


class LivePriceStream:
    """Process-wide websocket manager for streaming LTPs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ticker: KiteTicker | None = None
        self._api_key: str | None = None
        self._access_token: str | None = None
        self._subscribed_tokens: set[int] = set()
        self._connected = False
        self._cache_refresh_listeners: list[CacheRefreshListener] = []

    def _halt_ticker_on_auth_failure(self, ticker: KiteTicker, detail: str = "") -> None:
        """Stop KiteTicker reconnect loop and drop client so a new login can reconnect."""
        with self._lock:
            if self._ticker is not ticker:
                return
        try:
            ticker.stop_retry()
        except Exception:
            pass
        try:
            ticker.close()
        except Exception:
            pass
        with self._lock:
            if self._ticker is not ticker:
                return
            logger.warning(
                "Kite ticker WebSocket rejected the session (expired or invalid access token). "
                "Stopping live quote reconnects; log in again to refresh. Reason: %s",
                (detail[:300] if detail else "auth failure"),
            )
            self._ticker = None
            self._api_key = None
            self._access_token = None
            self._subscribed_tokens.clear()
            self._connected = False

    def ensure_running(self, api_key: str, access_token: str) -> None:
        """Start websocket (or rotate it) for the current access token."""
        with self._lock:
            same_session = (
                self._ticker is not None
                and self._api_key == api_key
                and self._access_token == access_token
            )
            if same_session:
                return

            self._close_locked()
            self._api_key = api_key
            self._access_token = access_token
            self._connected = False
            # Cap auto-retries so a bad token cannot spam the reactor for long if halt lags.
            self._ticker = KiteTicker(
                api_key,
                access_token,
                reconnect_max_tries=8,
                reconnect_max_delay=30,
            )
            self._wire_callbacks_locked(self._ticker)
            self._ticker.connect(threaded=True)

    def subscribe(self, instrument_tokens: set[int]) -> None:
        """Subscribe websocket for additional instrument tokens."""
        if not instrument_tokens:
            return

        with self._lock:
            if self._ticker is None:
                return
            fresh = _positive_instrument_tokens(instrument_tokens) - self._subscribed_tokens
            if not fresh:
                return
            self._subscribed_tokens.update(fresh)
            if self._connected:
                self._ticker.subscribe(list(fresh))
                self._ticker.set_mode(self._ticker.MODE_LTP, list(fresh))

    def set_subscriptions(self, desired_tokens: set[int]) -> None:
        """Reconcile subscriptions, adding missing and removing stale tokens."""
        wanted = _positive_instrument_tokens(desired_tokens)
        with self._lock:
            if self._ticker is None:
                return
            to_add = wanted - self._subscribed_tokens
            to_remove = self._subscribed_tokens - wanted

            if to_add:
                self._subscribed_tokens.update(to_add)
                if self._connected:
                    self._ticker.subscribe(list(to_add))
                    self._ticker.set_mode(self._ticker.MODE_LTP, list(to_add))
            
            if to_remove:
                self._subscribed_tokens.difference_update(to_remove)
                if self._connected:
                    self._ticker.unsubscribe(list(to_remove))

    def snapshot_ltp(self, instrument_tokens: set[int], wait_seconds: float = 0.6) -> dict[int, float]:
        """Return latest known LTP values for tokens.

        Waits briefly for at least one tick update when needed so first-load
        dashboard requests can receive streamed prices.
        """
        wanted = _positive_instrument_tokens(instrument_tokens)
        if not wanted:
            return {}

        return state_store.get_ltp(wanted)

    def close(self) -> None:
        """Close websocket and clear state."""
        with self._lock:
            self._close_locked()

    def _wire_callbacks_locked(self, ticker: KiteTicker) -> None:
        def _on_connect(ws, _response):
            with self._lock:
                self._connected = True
                tokens = list(self._subscribed_tokens)
            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_LTP, tokens)

        def _on_ticks(_ws, ticks):
            if not ticks:
                return
            updates: dict[int, float] = {}
            for tick in ticks:
                token = int(tick.get("instrument_token") or 0)
                ltp = tick.get("last_price")
                if token > 0 and ltp is not None:
                    fv = float(ltp)
                    updates[token] = fv
            if updates:
                tick_hub.publish(updates)

        def _on_disconnect(_ws, code, reason):
            with self._lock:
                self._connected = False
            if _kite_ws_auth_failure(code, reason):
                detail = _kite_ws_reason_text(reason) or str(code)
                self._halt_ticker_on_auth_failure(ticker, detail)

        ticker.on_connect = _on_connect
        ticker.on_ticks = _on_ticks
        ticker.on_close = _on_disconnect
        ticker.on_error = _on_disconnect

    def _close_locked(self) -> None:
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception:
                pass
        self._ticker = None
        self._api_key = None
        self._access_token = None
        self._connected = False
        self._subscribed_tokens.clear()



    def add_cache_refresh_listener(self, callback: CacheRefreshListener) -> None:
        """Register a callback invoked when on-disk / reference caches finish refreshing."""
        with self._lock:
            self._cache_refresh_listeners.append(callback)

    def remove_cache_refresh_listener(self, callback: CacheRefreshListener) -> None:
        with self._lock:
            try:
                self._cache_refresh_listeners.remove(callback)
            except ValueError:
                pass

    def notify_cache_refresh(self) -> None:
        """Signal listeners (e.g. dashboard WebSocket) to reload cached-derived HTML."""
        with self._lock:
            listeners = tuple(self._cache_refresh_listeners)
        for fn in listeners:
            try:
                fn()
            except Exception:
                log_dashboard_ws_debug_exception(logger, "Cache refresh listener callback failed")


live_price_stream = LivePriceStream()


def notify_dashboard_cache_refresh() -> None:
    """Backward-compatible alias for cache refresh emission."""
    from app.events import emit_cache_refresh

    emit_cache_refresh()
