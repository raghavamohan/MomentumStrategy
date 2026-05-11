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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_CACHE_FILE = PROJECT_ROOT / ".cache" / "model_cache.json"
_MODEL_CACHE_LOCK = threading.Lock()
_IST = ZoneInfo("Asia/Kolkata")
REFERENCE_CUTOFF_HOUR = 9

_BACKGROUND_REFRESH_LOCK = threading.Lock()
_BACKGROUND_REFRESH_RUNNING: set[str] = set()


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


def load_model_cache() -> dict[str, Any]:
    if not MODEL_CACHE_FILE.exists():
        return {}
    try:
        loaded = json.loads(MODEL_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_model_cache(payload: dict[str, Any]) -> None:
    MODEL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    tmp.replace(MODEL_CACHE_FILE)


def read_section(section: str) -> dict[str, Any]:
    with _MODEL_CACHE_LOCK:
        root = load_model_cache()
    value = root.get(section)
    return value if isinstance(value, dict) else {}


def update_section(section: str, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    with _MODEL_CACHE_LOCK:
        root = load_model_cache()
        current = root.get(section)
        base = current if isinstance(current, dict) else {}
        next_section = updater(dict(base))
        root[section] = next_section
        save_model_cache(root)
    return next_section


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
