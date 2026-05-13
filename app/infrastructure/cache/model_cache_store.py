"""Shared on-disk model cache store.

All durable cache sections live in one file: ``.cache/model_cache.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any, Callable
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

from app.infrastructure.auth import PROJECT_ROOT
_IST = ZoneInfo("Asia/Kolkata")
REFERENCE_CUTOFF_HOUR = 9

_BACKGROUND_REFRESH_LOCK = threading.Lock()
_BACKGROUND_REFRESH_RUNNING: set[str] = set()


def get_source_label(
    memory_warm: bool,
    disk_day: str,
    current_day: str,
    refresh_in_progress: bool,
) -> str:
    """Standardized source label for provider debug snapshots."""
    if memory_warm:
        return "memory"
    if not disk_day:
        return "cold_start_bg_refresh" if refresh_in_progress else "cold"
    if disk_day == current_day:
        return "disk"
    return "disk_stale_bg_refresh" if refresh_in_progress else "disk_stale"


def current_effective_day_ist(cutoff_hour: int = REFERENCE_CUTOFF_HOUR) -> str:
    """Return effective IST cache day, rolling over at ``cutoff_hour``."""
    now = datetime.now(_IST)
    if now.hour < cutoff_hour:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def next_cutoff_epoch_ist(cutoff_hour: int = REFERENCE_CUTOFF_HOUR) -> float:
    """Epoch seconds for the next IST cutoff boundary."""
    now = datetime.now(_IST)
    cutoff = now.replace(hour=cutoff_hour, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        cutoff = cutoff + timedelta(days=1)
    return cutoff.timestamp()


class BaseCache:
    """Base class for robust per-provider JSON file caching."""
    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        self.cache_file = PROJECT_ROOT / ".cache" / f"{provider_name}.json"
        self._lock = threading.Lock()

    def read_section(self, section: str) -> dict[str, Any]:
        with self._lock:
            value = self._load().get(section)
            return value if isinstance(value, dict) else {}

    def update_section(self, section: str, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            root = self._load()
            current = root.get(section)
            base = current if isinstance(current, dict) else {}
            next_section = updater(dict(base))
            root[section] = next_section
            self._save(root)
            return next_section

    def _load(self) -> dict[str, Any]:
        if not self.cache_file.exists():
            return {}
        try:
            loaded = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, payload: dict[str, Any]) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.cache_file)


def start_background_refresh_job(name: str, job: Callable[[], None]) -> bool:
    """Run one named daemon refresh job at a time.

    Returns ``True`` when a new thread was started, ``False`` if the named job
    is already running.
    """
    task_name = str(name or "").strip().lower()
    if not task_name:
        return False
    with _BACKGROUND_REFRESH_LOCK:
        if task_name in _BACKGROUND_REFRESH_RUNNING:
            return False
        _BACKGROUND_REFRESH_RUNNING.add(task_name)

    def _runner() -> None:
        try:
            job()
        finally:
            with _BACKGROUND_REFRESH_LOCK:
                _BACKGROUND_REFRESH_RUNNING.discard(task_name)

    threading.Thread(target=_runner, daemon=True).start()
    return True
