"""MomentumStrategy package.

A small dual-interface app that reports the current state of a Zerodha
account using the Kite Connect API:

* Equity holdings (long-term stocks held in demat).
* Mutual fund holdings (units held per folio, with NAV and P&L).
* Open net positions, separated into equity and F&O / derivatives.
* Equity segment cash balance (available, live, utilised).

Entry points
------------
``python -m app.main``
    Command-line interface. Prints the same data as ASCII tables.
    See :mod:`app.main`.

``python -m app.web``
    Starts the local FastAPI dashboard at http://127.0.0.1:5000/. The
    dashboard renders all four sections as tabs in a single page. See
    :mod:`app.web`.

Both entry points share the same authentication code (:mod:`app.auth`)
and the same on-disk access-token cache, so logging in via either
interface satisfies the other for the rest of the trading day.

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
