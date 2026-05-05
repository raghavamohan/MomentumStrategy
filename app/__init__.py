"""MomentumStrategy package.

A small CLI that reports the current state of a Zerodha account using the
Kite Connect API:

* Equity holdings (long-term stocks held in demat).
* Mutual fund holdings (units held per folio, with NAV and P&L).
* Open net positions, separated into equity and F&O / derivatives.
* Equity segment cash balance (available, live, utilised).

Modules
-------
``app.auth``
    Wraps the Kite Connect interactive login flow and caches the daily
    access token to disk so repeated runs do not require re-login.

``app.main``
    Entry point. Composes the three report sections and prints them as
    formatted tables.

External dependencies / references
----------------------------------
* Kite Connect HTTP API documentation: https://kite.trade/docs/connect/v3/
* Official Python client (``pykiteconnect``):
  https://github.com/zerodha/pykiteconnect
* ``pykiteconnect`` v4 reference: https://kite.trade/docs/pykiteconnect/v4/
* Kite Connect developer console (manage app, API key/secret, redirect URL):
  https://developers.kite.trade/
"""
