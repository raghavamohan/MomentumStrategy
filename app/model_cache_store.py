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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_CACHE_FILE = PROJECT_ROOT / ".cache" / "model_cache.json"
_MODEL_CACHE_LOCK = threading.Lock()
_IST = ZoneInfo("Asia/Kolkata")


def current_effective_day_ist(cutoff_hour: int = 9) -> str:
    """Return effective IST cache day, rolling over at ``cutoff_hour``."""
    now = datetime.now(_IST)
    if now.hour < cutoff_hour:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


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

