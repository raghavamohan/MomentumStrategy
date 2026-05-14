"""Neutral cache-refresh notifications (decoupled from WebSocket / dashboard).

Lower layers emit :func:`emit_cache_refresh`; the server lifespan wires subscribers
(e.g. :meth:`app.infrastructure.live_prices.LivePriceStream.notify_cache_refresh`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_CacheRefreshListener = Callable[[], None]
_listeners: list[_CacheRefreshListener] = []


def subscribe_cache_refresh(callback: _CacheRefreshListener) -> None:
    """Register a callback invoked when reference/cache data may have changed."""
    _listeners.append(callback)


def emit_cache_refresh() -> None:
    """Notify subscribers that cached-derived views should refresh."""
    for fn in tuple(_listeners):
        try:
            fn()
        except Exception:
            logger.exception("cache refresh listener failed")
