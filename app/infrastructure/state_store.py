"""Live state storage for LTP, full ticks, rolling buffers, and indicators."""

from __future__ import annotations

import collections
import threading
import time
from typing import Any

from app.infrastructure.tick_hub import tick_hub


class LiveStateStore:
    """Interface for querying and updating live dashboard state."""

    def get_ltp(self, tokens: set[int]) -> dict[int, float]:
        raise NotImplementedError

    def get_full_tick(self, token: int) -> dict | None:
        raise NotImplementedError

    def get_indicators(self, tokens: set[int]) -> dict[int, dict[str, Any]]:
        raise NotImplementedError

    def get_all_ring_buffers(self) -> dict[int, list[float]]:
        raise NotImplementedError

    def get_latest_ltps(self) -> dict[int, float]:
        raise NotImplementedError

    def save_indicators(self, indicators: dict[int, dict[str, Any]]) -> None:
        raise NotImplementedError

    def append_to_buffers(self, latest_prices: dict[int, float]) -> None:
        raise NotImplementedError


class InMemoryLiveStateStore(LiveStateStore):
    """In-memory implementation with 1000-price ring buffers.

    Receives full MODE_FULL tick dicts from tick_hub.
    Extracts LTP for backward-compatible access; also stores full tick for
    chart / market depth consumers.
    """

    def __init__(self, buffer_size: int = 1000) -> None:
        self._lock = threading.Lock()
        self._buffer_size = buffer_size
        self._ltp_by_token: dict[int, float] = {}
        self._full_ticks: dict[int, dict] = {}
        self._indicators: dict[int, dict[str, Any]] = {}
        self._ring_buffers: dict[int, collections.deque[float]] = {}

        # Subscribe to tick hub to keep LTPs and full ticks instantly up-to-date
        tick_hub.subscribe(self._on_ticks)

    def _on_ticks(self, updates: dict[int, dict]) -> None:
        """Handle full tick dicts from tick_hub."""
        if not updates:
            return
        with self._lock:
            for token, tick in updates.items():
                ltp = tick.get("last_price")
                if ltp is not None:
                    self._ltp_by_token[token] = float(ltp)
            self._full_ticks.update(updates)

    def get_ltp(self, tokens: set[int]) -> dict[int, float]:
        with self._lock:
            return {t: self._ltp_by_token[t] for t in tokens if t in self._ltp_by_token}

    def get_full_tick(self, token: int) -> dict | None:
        """Return the latest full MODE_FULL tick dict for a token, or None."""
        with self._lock:
            return self._full_ticks.get(token)

    def get_indicators(self, tokens: set[int]) -> dict[int, dict[str, Any]]:
        with self._lock:
            return {t: self._indicators[t] for t in tokens if t in self._indicators}

    def get_latest_ltps(self) -> dict[int, float]:
        with self._lock:
            return dict(self._ltp_by_token)

    def append_to_buffers(self, latest_prices: dict[int, float]) -> None:
        """Append the given latest prices to the ring buffers."""
        with self._lock:
            for token, ltp in latest_prices.items():
                if token not in self._ring_buffers:
                    self._ring_buffers[token] = collections.deque(maxlen=self._buffer_size)
                self._ring_buffers[token].append(ltp)

    def get_all_ring_buffers(self) -> dict[int, list[float]]:
        with self._lock:
            return {t: list(dq) for t, dq in self._ring_buffers.items()}

    def save_indicators(self, indicators: dict[int, dict[str, Any]]) -> None:
        with self._lock:
            self._indicators.update(indicators)


# Global singleton for the process
state_store = InMemoryLiveStateStore(buffer_size=1000)
