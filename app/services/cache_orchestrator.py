"""Background cache warmup aligned with :mod:`scripts.build_cache`."""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from pathlib import Path

from app.auth import PROJECT_ROOT

logger = logging.getLogger(__name__)


def _load_build_cache_module():
    path = PROJECT_ROOT / "scripts" / "build_cache.py"
    spec = importlib.util.spec_from_file_location("build_cache_script", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load build_cache from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_startup_cache_warmup() -> None:
    """Fire-and-forget reference + mfdata + MarketSmith warmup (not full yfinance universe)."""
    if os.getenv("MOMENTUM_SKIP_CACHE_WARMUP", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        logger.info("Skipping startup cache warmup (MOMENTUM_SKIP_CACHE_WARMUP).")
        return

    def _job() -> None:
        try:
            bc = _load_build_cache_module()
        except Exception:
            logger.exception("Could not load scripts/build_cache.py for warmup")
            return
        try:
            logger.info("Startup cache warmup: reference + mfdata + MarketSmith")
            bc.warm_reference_cache()
            bc.warm_mfdata_cache()
            bc.warm_marketsmith_cache()
        except Exception:
            logger.exception("Startup cache warmup failed")

    threading.Thread(target=_job, daemon=True).start()
