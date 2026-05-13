"""mfdata.in HTTP API + in-memory/disk cache for MF search and family holdings."""

from __future__ import annotations

import json
import logging
import re
import threading
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

from app.domain.reference_notifications import notify_reference_cache_refresh
from app.infrastructure.cache.model_cache_store import (
    BaseCache,
    current_effective_day_ist,
    get_source_label,
    next_cutoff_epoch_ist,
)
from app.domain.reference_context import WarmupContext

logger = logging.getLogger(__name__)

MFDATA_BASE_URL = "https://mfdata.in"
MFDATA_HTTP_TIMEOUT_SECONDS = 20

# --- Provider State ---
_MFDATA_CACHE = BaseCache("mfdata_provider")
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_CACHE_DIRTY = False

_CACHE_DAY = ""
_SOURCE_LABEL = "unknown"
_EXPIRES_AT = 0.0

_SEARCH_CACHE: dict[str, list[dict[str, Any]]] = {}
_HOLDINGS_CACHE: dict[int, dict[str, Any] | None] = {}


def normalize_match_text(value: str) -> str:
    """Lowercase alphanumeric tokens for fuzzy scheme matching."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return " ".join(cleaned.split())


def canonicalize_mf_scheme_name(value: str) -> str:
    normalized = normalize_match_text(value)
    drop_tokens = {
        "direct", "regular", "growth", "plan", "option", "idcw", "dividend", "payout",
        "reinvestment", "reinvest", "bonus", "inst", "institutional",
    }
    kept = [token for token in normalized.split() if token not in drop_tokens]
    return " ".join(kept)


def _load_cache_if_needed_unlocked() -> None:
    global _CACHE_LOADED, _CACHE_DAY, _SOURCE_LABEL, _EXPIRES_AT
    if _CACHE_LOADED:
        return

    payload = _MFDATA_CACHE.read_section("mfdata")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    _CACHE_DAY = str(meta.get("cache_day") or "").strip()

    search_data = payload.get("search")
    if isinstance(search_data, dict):
        for k, v in search_data.items():
            if isinstance(v, list):
                _SEARCH_CACHE[k] = [row for row in v if isinstance(row, dict)]

    holdings_data = payload.get("holdings")
    if isinstance(holdings_data, dict):
        for k, v in holdings_data.items():
            try:
                fid = int(k)
                _HOLDINGS_CACHE[fid] = v if isinstance(v, dict) else None
            except (ValueError, TypeError):
                continue

    current_day = current_effective_day_ist(cutoff_hour=9)
    if _CACHE_DAY != current_day:
        # Day mismatch: keep memory but mark for eventual flush if new data comes in
        _SEARCH_CACHE.clear()
        _HOLDINGS_CACHE.clear()
        _CACHE_DAY = current_day

    _SOURCE_LABEL = "disk" if _CACHE_DAY else "cold"
    _EXPIRES_AT = next_cutoff_epoch_ist(9)
    _CACHE_LOADED = True


def _persist_cache_unlocked() -> None:
    global _CACHE_DIRTY
    if not _CACHE_DIRTY:
        return

    def _updater(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "meta": {"cache_day": _CACHE_DAY},
            "search": dict(_SEARCH_CACHE),
            "holdings": {str(k): v for k, v in _HOLDINGS_CACHE.items()},
        }

    _MFDATA_CACHE.update_section("mfdata", _updater)
    _CACHE_DIRTY = False
    notify_reference_cache_refresh()


def flush_mfdata_disk_cache() -> bool:
    """External hook to persist dirty cache (e.g. after a series of fetches)."""
    with _CACHE_LOCK:
        if _CACHE_DIRTY:
            _persist_cache_unlocked()
            return True
    return False


def _mfdata_json_get(path: str, query: dict[str, Any] | None = None) -> Any:
    url = f"{MFDATA_BASE_URL}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    req = URLRequest(
        url,
        headers={"Accept": "application/json", "User-Agent": "MomentumStrategy/1.0 (+mfdata_provider)"},
    )
    with urlopen(req, timeout=MFDATA_HTTP_TIMEOUT_SECONDS) as resp:
        payload = resp.read().decode("utf-8", errors="replace")
    return json.loads(payload)


def mfdata_search_fund(fund_name: str) -> list[dict[str, Any]]:
    global _CACHE_DIRTY
    key = normalize_match_text(fund_name)
    if not key:
        return []

    with _CACHE_LOCK:
        _load_cache_if_needed_unlocked()
        if key in _SEARCH_CACHE:
            return list(_SEARCH_CACHE[key])

    try:
        payload = _mfdata_json_get("/api/v1/search", {"q": fund_name})
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        payload = {}

    rows = payload.get("data") if isinstance(payload, dict) else []
    rows = [row for row in (rows or []) if isinstance(row, dict)]

    with _CACHE_LOCK:
        _SEARCH_CACHE[key] = rows
        _CACHE_DIRTY = True
        # We don't auto-persist on every search to avoid heavy I/O
    return rows


def mfdata_holdings_for_family(family_id: int) -> dict[str, Any] | None:
    global _CACHE_DIRTY
    with _CACHE_LOCK:
        _load_cache_if_needed_unlocked()
        if family_id in _HOLDINGS_CACHE:
            return _HOLDINGS_CACHE[family_id]

    try:
        payload = _mfdata_json_get(f"/api/v1/families/{family_id}/holdings")
    except HTTPError as exc:
        payload = {} if exc.code == 404 else None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        payload = {}

    if payload is None: return None # Network error

    data = payload.get("data") if isinstance(payload, dict) else None
    result = data if isinstance(data, dict) else None

    with _CACHE_LOCK:
        _HOLDINGS_CACHE[family_id] = result
        _CACHE_DIRTY = True
    return result


def mfdata_disk_table_snapshot() -> tuple[str, int, int]:
    with _CACHE_LOCK:
        _load_cache_if_needed_unlocked()
        return _CACHE_DAY, len(_SEARCH_CACHE), len(_HOLDINGS_CACHE)


def warmup(_ctx: WarmupContext) -> None:
    with _CACHE_LOCK:
        _load_cache_if_needed_unlocked()


def mfdata_reference_debug_snapshot(now: float) -> dict[str, Any]:
    with _CACHE_LOCK:
        _load_cache_if_needed_unlocked()
        cur_day = current_effective_day_ist(cutoff_hour=9)
        src = get_source_label(
            memory_warm=_CACHE_LOADED and bool(_SEARCH_CACHE or _HOLDINGS_CACHE),
            disk_day=_CACHE_DAY,
            current_day=cur_day,
            refresh_in_progress=False
        )
        if _CACHE_DIRTY:
            src = f"{src}_dirty"

        return {
            "source": src,
            "expires_in_ms": max(0.0, (next_cutoff_epoch_ist(9) - now) * 1000.0),
            "refresh_in_progress": False,
            "search_keys_cached": len(_SEARCH_CACHE),
            "holdings_families_cached": len(_HOLDINGS_CACHE),
            "cache_day": _CACHE_DAY,
        }


def rank_mfdata_variants(fund_name: str, variants: list[dict[str, Any]]) -> list[int]:
    canonical_fund = canonicalize_mf_scheme_name(fund_name)
    if not canonical_fund:
        return []
    fund_tokens = set(canonical_fund.split())
    scored: list[tuple[float, int]] = []
    seen_family_ids: set[int] = set()
    for row in variants:
        family_id = int(row.get("family_id") or 0)
        if family_id <= 0 or family_id in seen_family_ids:
            continue
        seen_family_ids.add(family_id)
        candidate_name = str(row.get("name") or "").strip()
        candidate = canonicalize_mf_scheme_name(candidate_name)
        if not candidate:
            continue
        candidate_tokens = set(candidate.split())
        overlap = len(fund_tokens & candidate_tokens)
        score = float(overlap * 10)
        if canonical_fund in candidate or candidate in canonical_fund:
            score += 5
        score += SequenceMatcher(None, canonical_fund, candidate).ratio()
        if "growth" in candidate_name.lower() or "growth" in str(row.get("option_type") or "").lower():
            score += 0.25
        if str(row.get("plan_type") or "").lower() == "direct":
            score += 0.05
        scored.append((score, family_id))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [family_id for _, family_id in scored]


__all__ = [
    "mfdata_reference_debug_snapshot",
    "mfdata_disk_table_snapshot",
    "canonicalize_mf_scheme_name",
    "flush_mfdata_disk_cache",
    "mfdata_holdings_for_family",
    "mfdata_search_fund",
    "normalize_match_text",
    "rank_mfdata_variants",
    "warmup",
]
