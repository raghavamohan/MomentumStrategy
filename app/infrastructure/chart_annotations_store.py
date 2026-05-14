"""Persistent JSON store for per-stock chart annotations (trendlines + levels).

Storage: PROJECT_ROOT/.cache/chart_annotations.json

Structure::

    {
        "738561": {                        # instrument_token as string key
            "trendlines": [
                {
                    "id": "uuid-string",
                    "time1": 1715000000,   # Unix seconds
                    "price1": 23400.0,
                    "time2": 1715086400,
                    "price2": 23600.0,
                    "color": "#f59e0b",
                    "width": 1,
                    "label": "",
                    "extended": false
                },
                ...
            ],
            "levels": [
                {
                    "id": "uuid-string",
                    "price": 23500.0,
                    "color": "#22c55e",
                    "style": "dashed",
                    "width": 1,
                    "label": "Support"
                },
                ...
            ]
        }
    }
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from app.infrastructure.auth import PROJECT_ROOT

logger = logging.getLogger(__name__)

_ANNOTATIONS_FILE = PROJECT_ROOT / ".cache" / "chart_annotations.json"
_lock = threading.Lock()


def _load_all() -> dict[str, Any]:
    """Read the full annotations file; return empty dict on missing / corrupt."""
    if not _ANNOTATIONS_FILE.exists():
        return {}
    try:
        data = json.loads(_ANNOTATIONS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("chart_annotations_store: could not read %s: %s", _ANNOTATIONS_FILE, exc)
        return {}


def _save_all(data: dict[str, Any]) -> None:
    """Atomically write the full annotations dict to disk."""
    _ANNOTATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _ANNOTATIONS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        tmp.replace(_ANNOTATIONS_FILE)
    except OSError as exc:
        logger.error("chart_annotations_store: could not write %s: %s", _ANNOTATIONS_FILE, exc)
        raise


def load(token: int) -> dict[str, Any]:
    """Return saved annotations for ``token``.

    Returns a dict with keys ``trendlines`` (list) and ``levels`` (list).
    Returns empty lists when no annotations exist for this token.
    """
    with _lock:
        data = _load_all()
    entry = data.get(str(token), {})
    return {
        "trendlines": list(entry.get("trendlines") or []),
        "levels": list(entry.get("levels") or []),
    }


def save(token: int, trendlines: list[dict], levels: list[dict]) -> None:
    """Persist annotations for ``token``, replacing any previous data."""
    with _lock:
        data = _load_all()
        data[str(token)] = {
            "trendlines": list(trendlines or []),
            "levels": list(levels or []),
        }
        _save_all(data)


def delete(token: int) -> None:
    """Remove all annotations for ``token``."""
    with _lock:
        data = _load_all()
        data.pop(str(token), None)
        _save_all(data)
