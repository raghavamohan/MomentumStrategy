"""Kite Connect authentication helpers.

Background: how Kite Connect login works
----------------------------------------
The Kite Connect API uses a 3-legged interactive login. There is **no**
username/password flow available to third-party apps. The sequence is:

1. The app constructs a login URL containing its ``api_key`` and redirects
   the user to it. The user signs in to Zerodha in their browser.
2. After the user approves access, Zerodha redirects the browser back to
   the **Redirect URL** registered in the Kite Connect app on
   https://developers.kite.trade with a one-time ``request_token`` query
   parameter, e.g. ``https://your-redirect/?request_token=ABCD&action=login``.
3. The app exchanges that ``request_token`` (together with the
   ``api_secret``) for an ``access_token`` by calling
   ``KiteConnect.generate_session``. The access token is then attached to
   the client via ``KiteConnect.set_access_token`` for all subsequent
   API calls.
4. The ``access_token`` is valid until approximately **6 AM IST the next
   trading day**, after which a fresh login is required.

References
----------
* Login flow (HTTP):    https://kite.trade/docs/connect/v3/user/#login-flow
* User session (HTTP):  https://kite.trade/docs/connect/v3/user/
* ``KiteConnect`` class: https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect
* ``login_url``:         https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.login_url
* ``generate_session``:  https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.generate_session
* ``set_access_token``:  https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.set_access_token
* ``profile``:           https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.profile
* ``TokenException``:    https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.exceptions.TokenException
* Source of pykiteconnect: https://github.com/zerodha/pykiteconnect

What this module does
---------------------
Wraps the flow above for a CLI script:

* Reads ``KITE_API_KEY`` and ``KITE_API_SECRET`` from a local ``.env``
  file via ``python-dotenv``.
* Reuses a previously generated ``access_token`` cached at
  ``<project_root>/.access_token.json`` if it still works (validated by
  a cheap ``kite.profile()`` call).
* Otherwise prints the Kite login URL, prompts the user to paste the
  ``request_token`` from the redirected URL, exchanges it for a fresh
  ``access_token``, and updates the cache.

Security notes
--------------
* ``.env`` and ``.access_token.json`` are listed in ``.gitignore`` and
  must never be committed. They contain credentials that, together,
  grant full read/trade access to the Zerodha account.
* The access token is stored in plaintext on disk because Kite Connect
  itself rotates it daily. For a hardened deployment, replace
  ``_load_cached_access_token`` / ``_save_cached_access_token`` with an
  OS keychain (e.g. ``keyring``) or an encrypted store.
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
"""On-disk cache for the daily Kite Connect access token.

The file is JSON-encoded ``{"access_token": "<token>"}``. Lifetime is
controlled by Kite (token expires ~6 AM IST the next trading day); the
cache is invalidated lazily when a real API call raises
``TokenException``.
"""


def _load_cached_access_token() -> str | None:
    """Return the cached access token if one is on disk and parseable.

    Returns ``None`` if the cache file is missing, unreadable, malformed,
    or contains an empty/non-string ``access_token`` value.
    """
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    token = data.get("access_token")
    return token if isinstance(token, str) and token else None


def _save_cached_access_token(access_token: str) -> None:
    """Persist a freshly generated access token to disk for reuse."""
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"access_token": access_token}, indent=2),
        encoding="utf-8",
    )


def _interactive_login(kite: KiteConnect, api_secret: str) -> str:
    """Run the interactive Kite Connect login on the terminal.

    Prints the login URL produced by
    `KiteConnect.login_url <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.login_url>`_,
    waits for the user to paste the ``request_token`` returned by Zerodha
    on the redirect, and exchanges it for an ``access_token`` via
    `KiteConnect.generate_session <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.generate_session>`_.

    The new token is cached via :func:`_save_cached_access_token` so that
    subsequent runs in the same trading day skip the browser step.

    Parameters
    ----------
    kite:
        A :class:`KiteConnect` client already initialised with the
        ``api_key`` (no access token yet).
    api_secret:
        The matching API secret from
        https://developers.kite.trade. Required by ``generate_session``
        to derive the access token.

    Returns
    -------
    str
        The freshly generated access token.

    Raises
    ------
    SystemExit
        If the user submits an empty ``request_token``.
    """
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
    """Return an authenticated :class:`KiteConnect` client.

    The returned client has its ``access_token`` already set and is ready
    to call portfolio / market-data endpoints such as
    `holdings <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.holdings>`_,
    `positions <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.positions>`_,
    and
    `margins <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.margins>`_.

    Behaviour:

    1. Loads ``KITE_API_KEY`` and ``KITE_API_SECRET`` from the project's
       ``.env`` file using ``python-dotenv``.
    2. If a cached access token exists, attaches it and validates with a
       cheap call to
       `KiteConnect.profile <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.profile>`_.
       ``profile`` is used as the validation probe because it is read-only,
       has no side effects, and returns immediately for a healthy session.
    3. If validation fails with
       `TokenException <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.exceptions.TokenException>`_,
       (typically because the token has expired overnight) the
       interactive login flow runs, the new token is cached, and a
       client is returned with the fresh token attached.

    Raises
    ------
    SystemExit
        If ``KITE_API_KEY`` or ``KITE_API_SECRET`` is missing/blank in
        ``.env``, or if the user aborts the interactive login.
    """
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
            kite.profile()
            return kite
        except TokenException:
            # Cached token has expired. Fall through to interactive login.
            pass

    access_token = _interactive_login(kite, api_secret)
    kite.set_access_token(access_token)
    return kite
