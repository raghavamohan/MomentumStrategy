"""Central event hub for streaming tick distribution.

This replaces the built-in listener list of `LivePriceStream` with a standalone
pub/sub hub so multiple decoupled components (StateStore, WebSocket coalescer, etc.)
can listen to tick events.

Tick payload shape (MODE_FULL from KiteTicker):
    {
        "instrument_token": int,
        "last_price": float,
        "last_quantity": int,
        "average_price": float,
        "volume": int,
        "buy_quantity": int,
        "sell_quantity": int,
        "ohlc": {"open": float, "high": float, "low": float, "close": float},
        "change": float,
        "last_trade_time": datetime | None,
        "oi": int,
        "depth": {
            "buy":  [{"price": float, "quantity": int, "orders": int}, ...],
            "sell": [{"price": float, "quantity": int, "orders": int}, ...],
        },
    }
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Full tick dict per token: dict[instrument_token, full_tick_dict]
TickListener = Callable[[dict[int, dict]], None]


class TickHub:
    """Protocol for tick distribution."""
    def publish(self, updates: dict[int, dict]) -> None:
        raise NotImplementedError

    def subscribe(self, listener: TickListener) -> None:
        raise NotImplementedError

    def unsubscribe(self, listener: TickListener) -> None:
        raise NotImplementedError


class InMemoryTickHub(TickHub):
    """In-memory thread-safe pub/sub for full tick deltas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: list[TickListener] = []

    def publish(self, updates: dict[int, dict]) -> None:
        if not updates:
            return
        with self._lock:
            listeners = tuple(self._listeners)
        for fn in listeners:
            try:
                fn(updates)
            except Exception:
                logger.exception("Tick listener callback failed in InMemoryTickHub")

    def subscribe(self, listener: TickListener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unsubscribe(self, listener: TickListener) -> None:
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass


# Global singleton for the process
tick_hub = InMemoryTickHub()
