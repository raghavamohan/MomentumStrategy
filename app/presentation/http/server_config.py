"""Dashboard HTTP server configuration: paths, env-derived settings, session secret."""

from __future__ import annotations

import logging
import os
import secrets
import warnings
from pathlib import Path

import keyring
from dotenv import load_dotenv

from app.infrastructure.auth import PROJECT_ROOT

logger = logging.getLogger(__name__)

TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR: Path = PROJECT_ROOT / "static"
SESSION_SECRET_FILE = PROJECT_ROOT / ".session_secret"
SESSION_SECRET_KEYRING_SERVICE = "MomentumStrategy"
SESSION_SECRET_KEYRING_ACCOUNT = "dashboard-session-secret"
DASHBOARD_TIMING_LOG_FILE = PROJECT_ROOT / ".cache" / "dashboard_timing.log"

_DASHBOARD_TIMING_LOGGER = logging.getLogger("app.dashboard.timing")


def setup_dashboard_timing_logger() -> None:
    """Attach a dedicated file handler for dashboard timing lines."""
    if _DASHBOARD_TIMING_LOGGER.handlers:
        return
    DASHBOARD_TIMING_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(DASHBOARD_TIMING_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _DASHBOARD_TIMING_LOGGER.addHandler(file_handler)
    _DASHBOARD_TIMING_LOGGER.setLevel(logging.INFO)
    _DASHBOARD_TIMING_LOGGER.propagate = False


def dashboard_timing_logger() -> logging.Logger:
    return _DASHBOARD_TIMING_LOGGER


setup_dashboard_timing_logger()


def dashboard_snapshot_interval_ms() -> int:
    """Full HTML snapshot interval (REST refresh for MF/cash/structure)."""
    raw = (
        os.getenv("DASHBOARD_SNAPSHOT_SECONDS", "").strip()
        or os.getenv("DASHBOARD_REFRESH_SECONDS", "").strip()
        or "120"
    )
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 120.0
    seconds = max(10.0, seconds)
    return int(seconds * 1000)


DASHBOARD_SNAPSHOT_INTERVAL_MS = dashboard_snapshot_interval_ms()


def dashboard_display_name() -> str:
    """Dashboard/product display name from env with a friendly default."""
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv("KITE_DASHBOARD_NAME", "").strip() or "Raghava's Portfolio"


DASHBOARD_DISPLAY_NAME = dashboard_display_name()

# Compact keys (spaces / punctuation stripped, uppercased) -> NSE index tradingsymbol for Kite quote keys.
_INDEX_COMPACT_TO_TRADINGSYMBOL: dict[str, str] = {
    "NIFTY50": "NIFTY 50",
    "NIFTY_50": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "NIFTYBANK": "NIFTY BANK",
    "NIFTYIT": "NIFTY IT",
    "NIFTY_IT": "NIFTY IT",
    "NIFTYFINSERVICE": "NIFTY FIN SERVICE",
    "NIFTY_FIN_SERVICE": "NIFTY FIN SERVICE",
    "NIFTYMET": "NIFTY METAL",
    "NIFTY_METAL": "NIFTY METAL",
}


def _resolve_index_tradingsymbol(label: str) -> str:
    """Map a dashboard env token (e.g. ``NIFTY50``) to an NSE index tradingsymbol."""
    stripped = label.strip()
    if not stripped:
        return ""
    compact = "".join(stripped.split()).upper().replace("-", "")
    return _INDEX_COMPACT_TO_TRADINGSYMBOL.get(compact, stripped)


def dashboard_index_entries() -> list[tuple[str, str]]:
    """Ordered unique (env label, NSE tradingsymbol) pairs for header index quotes."""
    load_dotenv(PROJECT_ROOT / ".env")
    raw = os.getenv("KITE_DASHBOARD_INDICES", "").strip()
    labels = [p.strip() for p in raw.split(",") if p.strip()]
    if not labels:
        labels = ["NIFTY50", "BANKNIFTY", "NIFTYIT", "NIFTYFINSERVICE", "NIFTYMET"]
    seen_ts: set[str] = set()
    out: list[tuple[str, str]] = []
    for label in labels:
        ts = _resolve_index_tradingsymbol(label)
        if not ts or ts in seen_ts:
            continue
        seen_ts.add(ts)
        out.append((label, ts))
    return out


DASHBOARD_INDEX_ENTRIES = dashboard_index_entries()

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5000


def dashboard_reload_enabled() -> bool:
    """Enable Uvicorn auto-reload for local development when explicitly requested."""
    raw = os.getenv("DASHBOARD_RELOAD", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


DASHBOARD_RELOAD = dashboard_reload_enabled()


def session_secret() -> str:
    """Stable signing key so session cookies survive server restarts.

    Prefer ``SESSION_SECRET`` in the environment; otherwise read/store the
    secret in the OS keychain using ``keyring``.

    Legacy support: if an older plaintext ``.session_secret`` file exists,
    migrate it into keychain and remove the file.
    """
    env = os.getenv("SESSION_SECRET", "").strip()
    if env:
        return env

    if SESSION_SECRET_FILE.exists():
        try:
            raw = SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
            if raw:
                keyring.set_password(
                    SESSION_SECRET_KEYRING_SERVICE,
                    SESSION_SECRET_KEYRING_ACCOUNT,
                    raw,
                )
                SESSION_SECRET_FILE.unlink(missing_ok=True)
                return raw
        except Exception:
            pass

    try:
        stored = keyring.get_password(
            SESSION_SECRET_KEYRING_SERVICE,
            SESSION_SECRET_KEYRING_ACCOUNT,
        )
        if stored:
            return stored.strip()
    except Exception:
        pass

    secret = secrets.token_hex(32)
    try:
        keyring.set_password(
            SESSION_SECRET_KEYRING_SERVICE,
            SESSION_SECRET_KEYRING_ACCOUNT,
            secret,
        )
    except Exception:
        warnings.warn(
            "No usable OS keyring backend found; using non-persistent session "
            "secret for this run. Set SESSION_SECRET or install a keyring backend "
            "to persist login sessions across restarts.",
            RuntimeWarning,
        )
    return secret


SESSION_SECRET = session_secret()
