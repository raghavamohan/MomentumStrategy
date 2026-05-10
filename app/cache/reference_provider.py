"""Typing helpers for reference cache provider modules (contributor contract)."""

from __future__ import annotations

from typing import Any, Protocol

from app.reference_context import WarmupContext


class ReferenceWarmupProvider(Protocol):
    """Minimum surface for modules registered in ``REFERENCE_PROVIDER_WARMUPS``."""

    def warmup(self, ctx: WarmupContext) -> Any: ...
