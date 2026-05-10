"""mfdata.in HTTP API + in-memory/disk cache for MF search and family holdings."""

from __future__ import annotations

import json
import re
import threading
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

from app.domain.reference_notifications import notify_reference_cache_refresh
from app.infrastructure.cache.model_cache_store import (
    current_effective_day_ist,
    next_cutoff_epoch_ist,
    read_section,
    update_section,
)
from app.domain.reference_context import WarmupContext

MFDATA_BASE_URL = "https://mfdata.in"
MFDATA_HTTP_TIMEOUT_SECONDS = 20

_MFDATA_CACHE_LOCK = threading.Lock()
_MFDATA_SEARCH_CACHE: dict[str, list[dict[str, Any]]] = {}
_MFDATA_HOLDINGS_CACHE: dict[int, dict[str, Any] | None] = {}
_MFDATA_DISK_CACHE_LOADED = False
_MFDATA_DISK_CACHE: dict[str, Any] = {"meta": {"cache_day": ""}, "search": {}, "holdings": {}}
_MFDATA_DISK_CACHE_DIRTY = False


def normalize_match_text(value: str) -> str:
    """Lowercase alphanumeric tokens for fuzzy scheme matching (shared with portfolio_model)."""
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


def _current_cache_day_token() -> str:
    return current_effective_day_ist(cutoff_hour=9)


def _save_mfdata_disk_cache_locked() -> None:
    update_section("mfdata", lambda _: dict(_MFDATA_DISK_CACHE))


def _prepare_mfdata_cache_locked() -> None:
    global _MFDATA_DISK_CACHE_LOADED, _MFDATA_DISK_CACHE, _MFDATA_DISK_CACHE_DIRTY
    if not _MFDATA_DISK_CACHE_LOADED:
        loaded = read_section("mfdata")
        if isinstance(loaded, dict):
            _MFDATA_DISK_CACHE = {
                "meta": loaded.get("meta") if isinstance(loaded.get("meta"), dict) else {"cache_day": ""},
                "search": loaded.get("search") if isinstance(loaded.get("search"), dict) else {},
                "holdings": loaded.get("holdings") if isinstance(loaded.get("holdings"), dict) else {},
            }
        _MFDATA_DISK_CACHE_LOADED = True

    current_day = _current_cache_day_token()
    cached_day = str((_MFDATA_DISK_CACHE.get("meta") or {}).get("cache_day") or "")
    if cached_day == current_day:
        return
    _MFDATA_DISK_CACHE["meta"] = {"cache_day": current_day}
    _MFDATA_DISK_CACHE["search"] = {}
    _MFDATA_DISK_CACHE["holdings"] = {}
    _MFDATA_SEARCH_CACHE.clear()
    _MFDATA_HOLDINGS_CACHE.clear()
    _MFDATA_DISK_CACHE_DIRTY = True
    try:
        _save_mfdata_disk_cache_locked()
        _MFDATA_DISK_CACHE_DIRTY = False
    except Exception:
        pass


def flush_mfdata_disk_cache() -> bool:
    """Persist dirty mfdata section if needed; returns whether a write occurred."""
    global _MFDATA_DISK_CACHE_DIRTY
    wrote = False
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        if not _MFDATA_DISK_CACHE_DIRTY:
            return False
        try:
            _save_mfdata_disk_cache_locked()
            _MFDATA_DISK_CACHE_DIRTY = False
            wrote = True
        except Exception:
            return False
    if wrote:
        notify_reference_cache_refresh()
    return True


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
    key = normalize_match_text(fund_name)
    if not key:
        return []
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        cached_mem = _MFDATA_SEARCH_CACHE.get(key)
        if cached_mem is not None:
            return list(cached_mem)
        cached_disk = (_MFDATA_DISK_CACHE.get("search") or {}).get(key)
        if isinstance(cached_disk, list):
            rows = [row for row in cached_disk if isinstance(row, dict)]
            _MFDATA_SEARCH_CACHE[key] = rows
            return list(rows)
    try:
        payload = _mfdata_json_get("/api/v1/search", {"q": fund_name})
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        payload = {}
    rows = payload.get("data") if isinstance(payload, dict) else []
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    global _MFDATA_DISK_CACHE_DIRTY
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        _MFDATA_SEARCH_CACHE[key] = rows
        (_MFDATA_DISK_CACHE.get("search") or {})[key] = rows
        _MFDATA_DISK_CACHE_DIRTY = True
    return rows


def mfdata_holdings_for_family(family_id: int) -> dict[str, Any] | None:
    family_key = str(int(family_id))
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        if family_id in _MFDATA_HOLDINGS_CACHE:
            return _MFDATA_HOLDINGS_CACHE[family_id]
        holdings_disk = (_MFDATA_DISK_CACHE.get("holdings") or {})
        if family_key in holdings_disk:
            cached_disk = holdings_disk.get(family_key)
            result = cached_disk if isinstance(cached_disk, dict) else None
            _MFDATA_HOLDINGS_CACHE[family_id] = result
            return result
    try:
        payload = _mfdata_json_get(f"/api/v1/families/{family_id}/holdings")
    except HTTPError as exc:
        if exc.code == 404:
            payload = {}
        else:
            raise
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        payload = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    result = data if isinstance(data, dict) else None
    global _MFDATA_DISK_CACHE_DIRTY
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        _MFDATA_HOLDINGS_CACHE[family_id] = result
        (_MFDATA_DISK_CACHE.get("holdings") or {})[family_key] = result
        _MFDATA_DISK_CACHE_DIRTY = True
    return result


def mfdata_disk_table_snapshot() -> tuple[str, int, int]:
    """Return ``(cache_day, search_key_count, holdings_family_count)`` from disk-backed cache."""
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        meta = _MFDATA_DISK_CACHE.get("meta") if isinstance(_MFDATA_DISK_CACHE.get("meta"), dict) else {}
        day = str(meta.get("cache_day") or "")
        search = _MFDATA_DISK_CACHE.get("search") if isinstance(_MFDATA_DISK_CACHE.get("search"), dict) else {}
        holdings = _MFDATA_DISK_CACHE.get("holdings") if isinstance(_MFDATA_DISK_CACHE.get("holdings"), dict) else {}
        return day, len(search), len(holdings)


def warmup(_ctx: WarmupContext) -> None:
    """Ensure mfdata disk section is loaded and aligned to the effective cache day."""
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()


def mfdata_reference_debug_snapshot(now: float) -> dict[str, Any]:
    """Metadata row for :func:`app.domain.portfolio_model.get_reference_cache_debug_snapshot`."""
    expires_ms = max(0.0, (next_cutoff_epoch_ist(9) - now) * 1000.0)
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        meta = (
            _MFDATA_DISK_CACHE.get("meta")
            if isinstance(_MFDATA_DISK_CACHE.get("meta"), dict)
            else {}
        )
        cached_day = str(meta.get("cache_day") or "").strip()
        cur = _current_cache_day_token()
        dirty = _MFDATA_DISK_CACHE_DIRTY
        search = (
            _MFDATA_DISK_CACHE.get("search")
            if isinstance(_MFDATA_DISK_CACHE.get("search"), dict)
            else {}
        )
        holdings = (
            _MFDATA_DISK_CACHE.get("holdings")
            if isinstance(_MFDATA_DISK_CACHE.get("holdings"), dict)
            else {}
        )
        prefix = "aligned" if cached_day == cur else "day_mismatch"
        source = f"{prefix}_dirty" if dirty else prefix
        return {
            "source": source,
            "expires_in_ms": expires_ms,
            "refresh_in_progress": False,
            "search_keys_cached": len(search),
            "holdings_families_cached": len(holdings),
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
