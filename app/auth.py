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

Two callers use this module
---------------------------
* The CLI entry point (:mod:`app.main`) calls :func:`get_kite_client`,
  which runs the full interactive flow on the terminal: prints the
  login URL, asks the user to paste the ``request_token``.
* The web dashboard (:mod:`app.web`) wires the same flow to HTTP
  routes; it uses the lower-level helpers exported here
  (:func:`load_credentials`, :func:`load_cached_access_token`,
  :func:`save_cached_access_token`, :func:`build_authenticated_client`,
  :func:`validate_kite_session`) and provides its own ``/callback``
  handler instead of the terminal prompt.

Both callers share the same on-disk token cache so logging in via either
interface satisfies both for the rest of the trading day.

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

Security notes
--------------
* ``.env`` and ``.access_token.json`` are listed in ``.gitignore`` and
  must never be committed. They contain credentials that, together,
  grant full read/trade access to the Zerodha account.
* The access token is stored in plaintext on disk because Kite Connect
  itself rotates it daily. For a hardened deployment, replace
  :func:`load_cached_access_token` / :func:`save_cached_access_token`
  with an OS keychain (e.g. ``keyring``) or an encrypted store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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


def load_credentials() -> tuple[str, str]:
    """Read ``KITE_API_KEY`` and ``KITE_API_SECRET`` from ``.env``.

    Returns
    -------
    tuple[str, str]
        ``(api_key, api_secret)``.

    Raises
    ------
    SystemExit
        If either credential is missing or blank in ``.env``.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise SystemExit(
            "KITE_API_KEY and/or KITE_API_SECRET are not set. "
            f"Edit {PROJECT_ROOT / '.env'} and fill them in."
        )
    return api_key, api_secret


def load_cached_access_token() -> str | None:
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


def save_cached_access_token(access_token: str) -> None:
    """Persist a freshly generated access token to disk for reuse."""
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"access_token": access_token}, indent=2),
        encoding="utf-8",
    )


def clear_cached_access_token() -> None:
    """Remove the on-disk access token file, if present.

    Used by the web ``/logout`` flow so a full Zerodha login is required
    again. The CLI will also need to re-authenticate after this.
    """
    try:
        TOKEN_CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def build_authenticated_client(api_key: str, access_token: str) -> KiteConnect:
    """Construct a :class:`KiteConnect` client with the access token attached.

    Does not make any network calls; pair with :func:`validate_kite_session`
    if you need to confirm the token is still alive.
    """
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def validate_kite_session(kite: KiteConnect) -> bool:
    """Return ``True`` iff ``kite``'s access token is still valid.

    Issues a cheap, idempotent
    `KiteConnect.profile <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.profile>`_
    call. ``profile`` is used as the validation probe because it is
    read-only, has no side effects, and returns immediately for a
    healthy session. If Kite returns an auth error, pykiteconnect raises
    ``TokenException`` and this helper returns ``False``.
    """
    try:
        kite.profile()
        return True
    except TokenException:
        return False


def _interactive_login(kite: KiteConnect, api_secret: str) -> str:
    """Run the interactive Kite Connect login on the terminal (CLI flow).

    Prints the login URL produced by
    `KiteConnect.login_url <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.login_url>`_,
    attempts to auto-capture the ``request_token`` from a local HTTP
    callback (configured via ``KITE_REDIRECT_URL``; defaults to
    ``http://127.0.0.1:5000/callback``), then falls back to manual paste
    if auto-capture is unavailable, and exchanges the token for an ``access_token`` via
    `KiteConnect.generate_session <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.generate_session>`_.

    The new token is cached via :func:`save_cached_access_token` so that
    subsequent runs (CLI or web) within the same trading day skip the
    browser step.

    Raises
    ------
    SystemExit
        If the user submits an empty ``request_token``.
    """
    login_url = kite.login_url()
    redirect_url = os.getenv("KITE_REDIRECT_URL", "http://127.0.0.1:5000/callback").strip()

    def _capture_request_token_from_redirect(timeout_seconds: int = 180) -> str | None:
        parsed = urlparse(redirect_url)
        if parsed.scheme.lower() != "http":
            return None
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            return None

        listen_host = parsed.hostname or "127.0.0.1"
        listen_port = parsed.port or 80
        expected_path = parsed.path or "/"
        received: dict[str, str | None] = {"request_token": None, "status": None}
        done = threading.Event()

        class _CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler signature
                req = urlparse(self.path)
                if req.path != expected_path:
                    self.send_response(404)
                    self.end_headers()
                    return

                query = parse_qs(req.query)
                received["request_token"] = (query.get("request_token") or [None])[0]
                received["status"] = (query.get("status") or [None])[0]

                has_token = bool(received["request_token"])
                self.send_response(200 if has_token else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                if has_token:
                    self.wfile.write(
                        b"Authentication complete. You can close this tab and return to the terminal."
                    )
                else:
                    self.wfile.write(
                        b"Request token missing in callback. Return to terminal for fallback login."
                    )
                done.set()

            def log_message(self, _format: str, *args) -> None:
                return

        try:
            server = ThreadingHTTPServer((listen_host, listen_port), _CallbackHandler)
        except OSError:
            return None

        try:
            server.timeout = 0.5
            deadline = time.time() + max(1, timeout_seconds)
            while time.time() < deadline and not done.is_set():
                server.handle_request()
        finally:
            server.server_close()

        if received["status"] and received["status"] != "success":
            return None
        token = received["request_token"]
        return token.strip() if isinstance(token, str) and token.strip() else None

    print()
    print("Kite Connect login required.")
    print("1) Open this URL in your browser and log in to Zerodha:")
    print(f"   {login_url}")
    try:
        webbrowser.open(login_url)
        print("   (Opened automatically in your default browser.)")
    except Exception:
        pass
    print()
    print(f"Redirect URL for CLI auto-capture: {redirect_url}")
    print("Attempting automatic request_token capture...")
    request_token = _capture_request_token_from_redirect()

    if not request_token:
        print()
        print("Automatic capture unavailable. Falling back to manual entry.")
        print("2) After login Zerodha redirects to your app's Redirect URL.")
        print("   Copy the value of the `request_token` query parameter from")
        print("   that redirected URL (it looks like ?request_token=XXXX&...).")
        print()
        request_token = input("Paste request_token here: ").strip()
        if not request_token:
            raise SystemExit("No request_token provided. Aborting.")

    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session["access_token"]
    save_cached_access_token(access_token)
    return access_token


def get_kite_client() -> KiteConnect:
    """Return an authenticated :class:`KiteConnect` client (CLI flow).

    Behaviour:

    1. Loads ``KITE_API_KEY`` and ``KITE_API_SECRET`` via
       :func:`load_credentials`.
    2. If a cached access token exists, attaches it via
       :func:`build_authenticated_client` and probes with
       :func:`validate_kite_session`.
    3. If validation fails (token expired overnight) the interactive
       terminal login runs, the new token is cached, and a client is
       returned with the fresh token attached.

    The web dashboard does **not** use this function (it cannot prompt
    on a terminal); see :mod:`app.web` for the HTTP equivalent.

    Raises
    ------
    SystemExit
        If credentials are missing in ``.env`` or the user aborts the
        interactive login.
    """
    api_key, api_secret = load_credentials()

    cached_token = load_cached_access_token()
    if cached_token:
        kite = build_authenticated_client(api_key, cached_token)
        if validate_kite_session(kite):
            return kite

    kite = KiteConnect(api_key=api_key)
    access_token = _interactive_login(kite, api_secret)
    kite.set_access_token(access_token)
    return kite
