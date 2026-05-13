"""Background worker for computing expensive indicators on a fixed interval."""

from __future__ import annotations

import logging
import threading
import time

from app.infrastructure.state_store import state_store

logger = logging.getLogger(__name__)

class IndicatorWorker:
    """Computes rolling indicators (e.g. 1000-price) on a fixed interval using latest-wins compute queues."""

    def __init__(self, interval_seconds: float = 1.0) -> None:
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background worker thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="IndicatorWorker")
        self._thread.start()
        logger.info("IndicatorWorker started with %.1fs interval", self._interval_seconds)

    def stop(self) -> None:
        """Stop the background worker thread cleanly."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("IndicatorWorker stopped")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            start_ts = time.time()
            try:
                self._compute_cycle()
            except Exception:
                logger.exception("Error in IndicatorWorker compute cycle")
            
            elapsed = time.time() - start_ts
            sleep_time = max(0.0, self._interval_seconds - elapsed)
            
            # Use wait instead of sleep so we can wake up immediately on stop()
            self._stop_event.wait(timeout=sleep_time)

    def _compute_cycle(self) -> None:
        """Compute the indicators using a latest-wins pattern."""
        # 1. Fetch the latest known LTP for all subscribed tokens
        latest_prices = state_store.get_latest_ltps()
        if not latest_prices:
            return

        # 2. Append them to the rolling ring buffers (this ensures max 1 sample per interval window)
        state_store.append_to_buffers(latest_prices)

        # 3. Read the ring buffers to compute the indicators
        buffers = state_store.get_all_ring_buffers()
        
        computed_indicators = {}
        now = time.time()
        
        for token, prices in buffers.items():
            if not prices:
                continue
                
            # MVP Placeholder: Simple Moving Average
            sma = sum(prices) / len(prices)
            
            computed_indicators[token] = {
                "value": sma,
                "samples": len(prices),
                "ts": now
            }
            
        # 4. Save the computed values back to the shared state store
        if computed_indicators:
            state_store.save_indicators(computed_indicators)


# Global instance
indicator_worker = IndicatorWorker(interval_seconds=1.0)
