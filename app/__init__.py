"""MomentumStrategy package.

A small dual-interface app that reports the current state of a Zerodha
account using the Kite Connect API:

* Equity holdings (long-term stocks held in demat).
* Mutual fund holdings (units held per folio with NAV and P&L).
* Open net positions, separated into equity and F&O / derivatives.
* Equity segment cash balance (available, live, utilised).
* Overall portfolio summary totals.

Entry points
------------
``python -m app.server``
    Starts the local FastAPI server at http://127.0.0.1:5000/ (dashboard +
    ``/api/v1`` JSON APIs). ``python -m app.web`` is a compatibility alias.

``python -m app.cli_client``
    Command-line HTTP client; prints snapshot sections as ASCII tables. Requires
    the server to be running and a cached Kite token (log in via the browser once).

Both the CLI and dashboard share authentication via ``app.infrastructure.auth``
and the same on-disk access-token cache after browser login.

Package layout (approximate layers)
-----------------------------------
``app.domain`` — portfolio model and reference snapshots.
``app.application`` — dashboard view-model assembly.
``app.infrastructure`` — Kite auth, WebSocket prices, cache providers, orchestration.
``app.presentation.http`` — FastAPI routers, Jinja environment, session config.
Thin modules at the ``app`` package root (``server``, ``web``, ``cli_client``)
wire these layers together.

External dependencies / references
----------------------------------
* Kite Connect HTTP API documentation: https://kite.trade/docs/connect/v3/
* Official Python client (``pykiteconnect``):
  https://github.com/zerodha/pykiteconnect
* ``pykiteconnect`` v4 reference: https://kite.trade/docs/pykiteconnect/v4/
* Kite Connect developer console (manage app, API key/secret, redirect URL):
  https://developers.kite.trade/
* FastAPI: https://fastapi.tiangolo.com/
"""
