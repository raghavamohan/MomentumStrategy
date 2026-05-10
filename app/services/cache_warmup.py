"""Shared cache warmup used by the HTTP server startup path and ``scripts/build_cache``.

Keeps CLI/script behavior and dashboard background warmup aligned without dynamic imports.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kiteconnect.exceptions import PermissionException, TokenException

from app.auth import (
    build_authenticated_client,
    get_kite_client,
    load_cached_access_token,
    load_credentials,
    validate_kite_session,
)
from app.portfolio_model import (
    build_mf_holding,
    build_mf_underlying_breakdown,
)
from app.reference_context import WarmupContext
from app.reference_snapshot import warm_reference_snapshot

logger = logging.getLogger(__name__)


def load_authenticated_kite_client_for_scripts() -> Any:
    """Return a validated Kite client; fall back to interactive login when needed (CLI scripts)."""
    token = load_cached_access_token()
    if token:
        api_key, _ = load_credentials()
        candidate = build_authenticated_client(api_key, token)
        if validate_kite_session(candidate):
            return candidate
        print("Cached Kite token expired. Starting interactive login...")
        return get_kite_client()

    print("No cached Kite access token found. Starting interactive login...")
    return get_kite_client()


def warm_mfdata_holdings_cache(*, emit: Callable[[str], None] | None = None) -> None:
    """Populate mfdata cache sections from current MF holdings (same behavior as ``build_cache``)."""
    _emit = emit or print

    _emit("")
    _emit("Warming mfdata section in .cache/model_cache.json...")
    try:
        kite = load_authenticated_kite_client_for_scripts()
        try:
            mf_raw = kite.mf_holdings() or []
        except PermissionException:
            _emit("mfdata warmup skipped: Kite MF API permission is not enabled for this app.")
            return
        except TokenException:
            _emit("mfdata warmup skipped: Kite session expired during MF holdings fetch.")
            return
        mf_rows = sorted((build_mf_holding(h).to_dict() for h in mf_raw), key=lambda r: r["fund"])
        if not mf_rows:
            _emit("mfdata warmup skipped: no MF holdings in account.")
            return
        rows, month, missing_funds, aggregated_count, total_count = build_mf_underlying_breakdown(mf_rows)
        _emit(
            "mfdata warmup result: "
            f"funds={total_count}, aggregated={aggregated_count}, "
            f"rows={len(rows)}, month={month or '-'}, missing={len(missing_funds)}"
        )
    except Exception as exc:
        _emit(f"mfdata warmup FAILED: {exc}")


def run_startup_cache_warmup_sync() -> None:
    """Best-effort reference warmup then mfdata holdings walk (dashboard startup)."""
    logger.info("Startup cache warmup: reference + mfdata (holdings walk)")
    warm_reference_snapshot(WarmupContext(kite=None, force_refresh=False))
    warm_mfdata_holdings_cache(emit=lambda msg: logger.info("%s", msg))
