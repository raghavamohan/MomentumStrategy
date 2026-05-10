"""Background cache warmup after the HTTP server process starts."""

from __future__ import annotations

import logging
import os
import threading

from app.services.cache_warmup import run_startup_cache_warmup_sync

logger = logging.getLogger(__name__)


def run_startup_cache_warmup() -> None:
    """Fire-and-forget reference + mfdata holdings warmup (not full yfinance universe)."""
    if os.getenv("MOMENTUM_SKIP_CACHE_WARMUP", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        logger.info("Skipping startup cache warmup (MOMENTUM_SKIP_CACHE_WARMUP).")
        return

    def _job() -> None:
        try:
            run_startup_cache_warmup_sync()
        except Exception:
            logger.exception("Startup cache warmup failed")

    threading.Thread(target=_job, daemon=True).start()
