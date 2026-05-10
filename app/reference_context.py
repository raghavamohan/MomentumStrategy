"""Shared warmup context for reference cache providers (avoids circular imports)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WarmupContext:
    """Parameters for :func:`app.reference_snapshot.warm_reference_snapshot`."""

    kite: Any | None = None
    force_refresh: bool = False
    marketsmith_force_sync: bool = False
