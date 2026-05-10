"""Shared session and Kite client helpers for the dashboard HTTP server."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from fastapi import Request
from kiteconnect import KiteConnect

from app.infrastructure.auth import (
    build_authenticated_client,
    load_cached_access_token,
    load_credentials,
    validate_kite_session,
)


def kite_for_request() -> KiteConnect | None:
    """Return an authenticated KiteConnect client or ``None`` if not logged in."""
    token = load_cached_access_token()
    if not token:
        return None
    api_key, _ = load_credentials()
    return build_authenticated_client(api_key, token)


def restore_session_if_token_valid_session(session: MutableMapping[str, Any]) -> bool:
    """Ensure ``session`` reflects a valid Kite token if one exists on disk."""
    token = load_cached_access_token()
    if session.get("authenticated"):
        if token:
            return True
        session.clear()
        return False
    if not token:
        return False
    api_key, _ = load_credentials()
    kite = build_authenticated_client(api_key, token)
    if not validate_kite_session(kite):
        return False
    session["authenticated"] = True
    return True


def restore_session_if_token_valid(request: Request) -> bool:
    """Ensure ``request.session`` reflects a valid Kite token if one exists on disk."""
    return restore_session_if_token_valid_session(request.session)


def api_bearer_matches_cached_token(request: Request) -> bool:
    """True when ``Authorization: Bearer <token>`` matches on-disk Kite access token."""
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return False
    token = auth.removeprefix("Bearer ").strip()
    cached = load_cached_access_token()
    return bool(token and cached and token == cached)


def authorized_browser_or_api(request: Request) -> bool:
    """Session cookie (browser) or bearer token (CLI / API clients)."""
    return restore_session_if_token_valid(request) or api_bearer_matches_cached_token(
        request
    )
