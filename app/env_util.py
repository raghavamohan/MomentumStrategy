"""Environment toggle helpers shared by the dashboard and live price stack.

Dashboard code and :mod:`app.live_prices` import from here so boolean env flags stay
consistent (``1`` / ``true`` / ``yes``, case-insensitive). When you add another
on/off toggle, use :func:`env_truthy` or a small wrapper that calls it—do not copy
the membership check into ``web.py``, ``live_prices.py``, or elsewhere.

``DASHBOARD_DEBUG_WS``-gated exception logging for WebSocket/tick paths goes through
:func:`log_dashboard_ws_debug_exception` so the env read stays in one place.
"""

from __future__ import annotations

import logging
import os

_TRUTHY = frozenset({"1", "true", "yes"})


def env_truthy(name: str, *, default: str = "") -> bool:
    """True if env ``name`` is a common truthy string (1, true, yes), case-insensitive."""
    return os.getenv(name, default).strip().lower() in _TRUTHY


def dashboard_ws_debug_enabled() -> bool:
    """True when ``DASHBOARD_DEBUG_WS`` requests verbose WebSocket/tick diagnostics."""
    return env_truthy("DASHBOARD_DEBUG_WS")


def log_dashboard_ws_debug_exception(logger: logging.Logger, msg: str) -> None:
    """Call ``logger.exception(msg)`` only when :func:`dashboard_ws_debug_enabled` is true."""
    if dashboard_ws_debug_enabled():
        logger.exception(msg)
