"""Live market prices via Kite WebSocket (KiteTicker).

This module keeps a single websocket connection per process and exposes a
thread-safe interface to:

* subscribe to instrument tokens shown on the dashboard, and
* read the latest streamed LTP for those tokens.

Reference:
https://kite.trade/docs/connect/v3/websocket/
"""

from __future__ import annotations

import threading

from kiteconnect import KiteTicker


class LivePriceStream:
    """Process-wide websocket manager for streaming LTPs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ticker: KiteTicker | None = None
        self._api_key: str | None = None
        self._access_token: str | None = None
        self._subscribed_tokens: set[int] = set()
        self._ltp_by_token: dict[int, float] = {}
        self._connected = False
        self._tick_event = threading.Event()

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
            self._ticker = KiteTicker(api_key, access_token)
            self._wire_callbacks_locked(self._ticker)
            self._ticker.connect(threaded=True)

    def subscribe(self, instrument_tokens: set[int]) -> None:
        """Subscribe websocket for additional instrument tokens."""
        if not instrument_tokens:
            return

        with self._lock:
            if self._ticker is None:
                return
            fresh = {int(t) for t in instrument_tokens if int(t) > 0} - self._subscribed_tokens
            if not fresh:
                return
            self._subscribed_tokens.update(fresh)
            if self._connected:
                self._ticker.subscribe(list(fresh))
                self._ticker.set_mode(self._ticker.MODE_LTP, list(fresh))

    def snapshot_ltp(self, instrument_tokens: set[int], wait_seconds: float = 0.6) -> dict[int, float]:
        """Return latest known LTP values for tokens.

        Waits briefly for at least one tick update when needed so first-load
        dashboard requests can receive streamed prices.
        """
        wanted = {int(t) for t in instrument_tokens if int(t) > 0}
        if not wanted:
            return {}

        with self._lock:
            missing = {t for t in wanted if t not in self._ltp_by_token}

        if missing:
            self._tick_event.wait(timeout=max(0.0, wait_seconds))

        with self._lock:
            return {t: self._ltp_by_token[t] for t in wanted if t in self._ltp_by_token}

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
            with self._lock:
                for tick in ticks:
                    token = int(tick.get("instrument_token") or 0)
                    ltp = tick.get("last_price")
                    if token > 0 and ltp is not None:
                        self._ltp_by_token[token] = float(ltp)
            self._tick_event.set()
            self._tick_event.clear()

        def _on_close(_ws, _code, _reason):
            with self._lock:
                self._connected = False

        def _on_error(_ws, _code, _reason):
            with self._lock:
                self._connected = False

        ticker.on_connect = _on_connect
        ticker.on_ticks = _on_ticks
        ticker.on_close = _on_close
        ticker.on_error = _on_error

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
        self._ltp_by_token.clear()


live_price_stream = LivePriceStream()
