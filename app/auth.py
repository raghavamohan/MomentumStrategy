"""Kite Connect authentication helpers.

Kite Connect uses an interactive login flow: the user must open a login URL
in a browser, log in to Zerodha, and Zerodha redirects to the app's
registered Redirect URL with a one-time `request_token`. That token is
exchanged (with the API secret) for an `access_token` that is valid until
roughly 6 AM the next trading day.

This module wraps that flow for a CLI script:
- Reads `KITE_API_KEY` / `KITE_API_SECRET` from `.env`.
- Reuses a cached `access_token` from `.access_token.json` if it still works.
- Otherwise prints the login URL, waits for the user to paste the
  `request_token`, generates a session, and caches the new token.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_CACHE_PATH = PROJECT_ROOT / ".access_token.json"


def _load_cached_access_token() -> str | None:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    token = data.get("access_token")
    return token if isinstance(token, str) and token else None


def _save_cached_access_token(access_token: str) -> None:
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"access_token": access_token}, indent=2),
        encoding="utf-8",
    )


def _interactive_login(kite: KiteConnect, api_secret: str) -> str:
    print()
    print("Kite Connect login required.")
    print("1) Open this URL in your browser and log in to Zerodha:")
    print(f"   {kite.login_url()}")
    print()
    print("2) After login Zerodha redirects to your app's Redirect URL.")
    print("   Copy the value of the `request_token` query parameter from")
    print("   that redirected URL (it looks like ?request_token=XXXX&...).")
    print()
    request_token = input("Paste request_token here: ").strip()
    if not request_token:
        raise SystemExit("No request_token provided. Aborting.")

    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session["access_token"]
    _save_cached_access_token(access_token)
    return access_token


def get_kite_client() -> KiteConnect:
    """Return an authenticated KiteConnect client, prompting for login if needed."""
    load_dotenv(PROJECT_ROOT / ".env")

    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()

    if not api_key or not api_secret:
        raise SystemExit(
            "KITE_API_KEY and/or KITE_API_SECRET are not set. "
            f"Edit {PROJECT_ROOT / '.env'} and fill them in."
        )

    kite = KiteConnect(api_key=api_key)

    cached_token = _load_cached_access_token()
    if cached_token:
        kite.set_access_token(cached_token)
        try:
            # Cheap call to validate the cached token.
            kite.profile()
            return kite
        except TokenException:
            # Cached token has expired (Kite tokens die ~6 AM next trading day).
            pass

    access_token = _interactive_login(kite, api_secret)
    kite.set_access_token(access_token)
    return kite
