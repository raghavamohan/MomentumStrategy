"""Debounced reference-cache refresh notifications (revision bump + emit_cache_refresh)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.events import emit_cache_refresh

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_timer: threading.Timer | None = None
_revision_bump: Callable[[], None] | None = None


def register_reference_revision_bump(fn: Callable[[], None]) -> None:
    """Called once from :mod:`app.domain.reference_snapshot` on import."""
    global _revision_bump
    _revision_bump = fn


def notify_reference_cache_refresh(debounce_ms: float = 80.0) -> None:
    """Coalesce rapid provider updates into one subscriber notification."""
    global _timer

    def fire() -> None:
        global _timer
        with _lock:
            _timer = None
        try:
            if _revision_bump:
                _revision_bump()
            emit_cache_refresh()
        except Exception:
            logger.exception("notify_reference_cache_refresh failed")

    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(debounce_ms / 1000.0, fire)
        _timer.daemon = True
        _timer.start()
