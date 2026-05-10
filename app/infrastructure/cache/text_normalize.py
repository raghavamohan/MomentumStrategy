"""Shared string normalisation for cache keys (symbols, ISIN, display names)."""

from __future__ import annotations

from typing import Any


def normalise_name(raw: Any) -> str:
    return str(raw or "").strip()


def normalise_symbol(raw: Any) -> str:
    return str(raw or "").strip().upper()


def normalise_isin(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(" ", "")
