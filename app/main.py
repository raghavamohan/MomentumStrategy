"""Entry point: render the current Zerodha account snapshot.

Run with::

    python -m app.main

Sections produced
-----------------
1. **Equity Holdings** — long-term stocks held in demat. Sourced from
   `KiteConnect.holdings <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.holdings>`_
   (HTTP: `GET /portfolio/holdings <https://kite.trade/docs/connect/v3/portfolio/#holdings>`_).

2. **Mutual Fund Holdings** — units held per folio, with NAV, invested
   value, current value, and P&L. Sourced from
   `KiteConnect.mf_holdings <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.mf_holdings>`_
   (HTTP: `GET /mf/holdings <https://kite.trade/docs/connect/v3/mutual-funds/#mutual-fund-holdings>`_).
   Requires the Mutual Funds module to be enabled on the Kite Connect
   app at https://developers.kite.trade; otherwise a friendly notice is
   printed instead of crashing.

3. **Open Positions** — current intraday / overnight open positions,
   split into two tables by exchange:

   * Equity (NSE / BSE).
   * F&O / derivatives (NFO / BFO / CDS / BCD / MCX).

   Sourced from
   `KiteConnect.positions <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.positions>`_
   (HTTP: `GET /portfolio/positions <https://kite.trade/docs/connect/v3/portfolio/#positions>`_).
   Closed positions (``quantity == 0``) are filtered out.

4. **Equity Cash Balance** — available cash, live balance, and utilised
   margin for the equity segment. Sourced from
   `KiteConnect.margins <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.margins>`_
   (HTTP: `GET /user/margins/{segment} <https://kite.trade/docs/connect/v3/user/#funds-and-margins>`_)
   called with ``segment="equity"``.

Authentication is handled by :mod:`app.auth`.

References
----------
* Kite Connect HTTP API:    https://kite.trade/docs/connect/v3/
* pykiteconnect v4 API ref: https://kite.trade/docs/pykiteconnect/v4/
* pykiteconnect source:     https://github.com/zerodha/pykiteconnect
* Exchange & segment codes: https://kite.trade/docs/connect/v3/exchange/
* Mutual Funds endpoints:   https://kite.trade/docs/connect/v3/mutual-funds/
"""

from __future__ import annotations

from tabulate import tabulate

from kiteconnect.exceptions import PermissionException

from app.auth import get_kite_client


# ---------------------------------------------------------------------------
# Equity holdings
# ---------------------------------------------------------------------------
#
# Each row in ``KiteConnect.holdings()`` is documented at:
#   https://kite.trade/docs/connect/v3/portfolio/#holdings
#
# The fields used here:
#   tradingsymbol           Symbol on the listed exchange (e.g. "INFY").
#   exchange                "NSE" or "BSE".
#   quantity                Settled quantity available for sale (T+2).
#   t1_quantity             Quantity bought today, still on T+1 path.
#   average_price           Buy-side weighted average per unit.
#   last_price              Latest traded price for the instrument.
#   pnl                     Realised + unrealised P&L on this holding.
#   day_change_percentage   Today's percentage change vs previous close.
# ---------------------------------------------------------------------------


def _row(holding: dict) -> list:
    """Map a single Kite ``holdings`` entry to a printable table row.

    The total quantity shown adds ``quantity`` (settled) and
    ``t1_quantity`` (today's buy still pending settlement) so that
    freshly bought shares are not invisible.
    """
    quantity = (holding.get("quantity") or 0) + (holding.get("t1_quantity") or 0)
    avg_price = float(holding.get("average_price") or 0.0)
    last_price = float(holding.get("last_price") or 0.0)
    pnl = float(holding.get("pnl") or 0.0)
    day_change_pct = float(holding.get("day_change_percentage") or 0.0)

    invested = avg_price * quantity
    current = last_price * quantity

    return [
        holding.get("tradingsymbol", ""),
        holding.get("exchange", ""),
        quantity,
        avg_price,
        last_price,
        invested,
        current,
        pnl,
        day_change_pct,
    ]


def _print_holdings(kite) -> None:
    """Print the equity holdings table.

    Calls ``kite.holdings()`` once and renders the result. See:
    https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.holdings
    """
    holdings = kite.holdings()

    print()
    print("=== Equity Holdings ===")

    if not holdings:
        print("No equity holdings found in your Zerodha account.")
        return

    rows = [_row(h) for h in holdings]
    rows.sort(key=lambda r: r[0])

    headers = [
        "Symbol",
        "Exch",
        "Qty",
        "Avg",
        "LTP",
        "Invested",
        "Current",
        "P&L",
        "Day %",
    ]

    print()
    print(
        tabulate(
            rows,
            headers=headers,
            tablefmt="github",
            floatfmt=(".0f", "", ".0f", ",.2f", ",.2f", ",.2f", ",.2f", ",.2f", ",.2f"),
        )
    )

    total_invested = sum(r[5] for r in rows)
    total_current = sum(r[6] for r in rows)
    total_pnl = sum(r[7] for r in rows)

    print()
    print(f"Holdings: {len(rows)} stock(s)")
    print(f"Invested: {total_invested:>14,.2f}")
    print(f"Current : {total_current:>14,.2f}")
    print(f"P&L     : {total_pnl:>14,.2f}")


# ---------------------------------------------------------------------------
# Mutual fund holdings
# ---------------------------------------------------------------------------
#
# ``KiteConnect.mf_holdings()`` corresponds to the HTTP endpoint
# ``GET /mf/holdings`` documented at:
#   https://kite.trade/docs/connect/v3/mutual-funds/#mutual-fund-holdings
#
# Response shape (list of dicts), relevant subset:
#   [
#     {
#       "folio":            "1234/5678",            # AMC folio number
#       "fund":             "Axis Bluechip Fund - Direct Plan - Growth",
#       "tradingsymbol":    "INF846K01EW2",         # ISIN
#       "quantity":         123.456,                # units held
#       "average_price":    45.1234,                # weighted avg buy NAV
#       "last_price":       67.8910,                # latest NAV
#       "last_price_date":  "2026-04-15",
#       "pledged_quantity": 0,
#       "pnl":              1234.56                 # realised + unrealised
#     },
#     ...
#   ]
#
# The Mutual Funds module must be enabled for the Kite Connect app
# (https://developers.kite.trade). If it isn't, ``mf_holdings()`` raises
# ``kiteconnect.exceptions.PermissionException`` -- caught below so the
# rest of the report still renders.
# ---------------------------------------------------------------------------


def _mf_row(holding: dict) -> list:
    """Map a single Kite ``mf_holdings`` entry to a printable table row.

    The "Avg" and "LTP" columns are NAVs (per-unit prices). "Invested"
    and "Current" are NAV * units. P&L is taken straight from Kite.
    """
    units = float(holding.get("quantity") or 0.0)
    avg_nav = float(holding.get("average_price") or 0.0)
    last_nav = float(holding.get("last_price") or 0.0)
    pnl = float(holding.get("pnl") or 0.0)

    invested = avg_nav * units
    current = last_nav * units

    return [
        holding.get("fund", ""),
        holding.get("folio", ""),
        units,
        avg_nav,
        last_nav,
        invested,
        current,
        pnl,
    ]


def _print_mf_holdings(kite) -> None:
    """Print the mutual fund holdings table.

    Calls
    `KiteConnect.mf_holdings <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.mf_holdings>`_
    (HTTP: `GET /mf/holdings <https://kite.trade/docs/connect/v3/mutual-funds/#mutual-fund-holdings>`_).

    If the Kite Connect app isn't subscribed to the Mutual Funds module,
    Kite returns 403 and pykiteconnect raises
    :class:`kiteconnect.exceptions.PermissionException`; this function
    catches that so the rest of the report still runs.
    """
    print()
    print("=== Mutual Fund Holdings ===")

    try:
        holdings = kite.mf_holdings()
    except PermissionException:
        print(
            "Mutual Funds API not enabled on this Kite Connect app. "
            "Enable the MF module at https://developers.kite.trade if you "
            "want this section."
        )
        return

    if not holdings:
        print("No mutual fund holdings found in your Zerodha account.")
        return

    rows = [_mf_row(h) for h in holdings]
    rows.sort(key=lambda r: r[0])

    headers = ["Fund", "Folio", "Units", "Avg NAV", "LTP", "Invested", "Current", "P&L"]

    print()
    print(
        tabulate(
            rows,
            headers=headers,
            tablefmt="github",
            floatfmt=("", "", ",.4f", ",.4f", ",.4f", ",.2f", ",.2f", ",.2f"),
        )
    )

    total_invested = sum(r[5] for r in rows)
    total_current = sum(r[6] for r in rows)
    total_pnl = sum(r[7] for r in rows)

    print()
    print(f"MF holdings: {len(rows)} fund(s)")
    print(f"Invested   : {total_invested:>14,.2f}")
    print(f"Current    : {total_current:>14,.2f}")
    print(f"P&L        : {total_pnl:>14,.2f}")


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------
#
# ``KiteConnect.positions()`` returns:
#   {
#       "net": [...],   # consolidated open positions across the day
#       "day": [...],   # today's intraday breakdown (entries + exits)
#   }
#
# This module uses the "net" view and discards rows with ``quantity == 0``
# (closed intraday round-trips) so the output reflects what the user is
# currently exposed to.
#
# Exchange codes (full list at https://kite.trade/docs/connect/v3/exchange/):
#   NSE   - NSE equity (cash market).
#   BSE   - BSE equity (cash market).
#   NFO   - NSE futures and options (equity derivatives).
#   BFO   - BSE futures and options.
#   CDS   - NSE currency derivatives.
#   BCD   - BSE currency derivatives.
#   MCX   - Multi Commodity Exchange (commodity futures and options).
# ---------------------------------------------------------------------------

EQUITY_EXCHANGES = {"NSE", "BSE"}
"""Exchanges treated as cash-market equity positions in the output."""

FNO_EXCHANGES = {"NFO", "BFO", "CDS", "BCD", "MCX"}
"""Exchanges treated as F&O / derivatives positions in the output."""


def _position_row(position: dict) -> list:
    """Map a single Kite ``positions`` entry to a printable row.

    Fields used (full schema at
    https://kite.trade/docs/connect/v3/portfolio/#positions):

    ``tradingsymbol``
        Symbol with strike/expiry suffix for derivatives, e.g.
        ``NIFTY24DEC25000CE``.
    ``exchange``
        See module-level exchange code list.
    ``product``
        Product type: ``CNC`` (delivery), ``MIS`` (intraday),
        ``NRML`` (overnight derivatives).
    ``quantity``
        Net signed quantity. Positive = long, negative = short.
    ``average_price``
        Weighted average entry price for the open quantity.
    ``last_price``
        Latest traded price for the instrument.
    ``pnl``
        Total profit / loss on the position.
    ``m2m``
        Mark-to-market for the day (today's price movement only).
    """
    qty = int(position.get("quantity") or 0)
    avg = float(position.get("average_price") or 0.0)
    ltp = float(position.get("last_price") or 0.0)
    pnl = float(position.get("pnl") or 0.0)
    m2m = float(position.get("m2m") or 0.0)
    return [
        position.get("tradingsymbol", ""),
        position.get("exchange", ""),
        position.get("product", ""),
        qty,
        avg,
        ltp,
        pnl,
        m2m,
    ]


def _print_position_table(title: str, positions: list[dict]) -> None:
    """Render one positions table with subtotals."""
    print()
    print(f"=== {title} ===")

    if not positions:
        print("(no open positions)")
        return

    rows = [_position_row(p) for p in positions]
    rows.sort(key=lambda r: r[0])

    headers = ["Symbol", "Exch", "Prod", "Qty", "Avg", "LTP", "P&L", "M2M"]

    print()
    print(
        tabulate(
            rows,
            headers=headers,
            tablefmt="github",
            floatfmt=("", "", "", ".0f", ",.2f", ",.2f", ",.2f", ",.2f"),
        )
    )

    total_pnl = sum(r[6] for r in rows)
    total_m2m = sum(r[7] for r in rows)
    print()
    print(f"Open positions: {len(rows)}")
    print(f"Total P&L     : {total_pnl:>14,.2f}")
    print(f"Total M2M     : {total_m2m:>14,.2f}")


def _print_positions(kite) -> None:
    """Fetch and print open positions, separated by equity vs F&O.

    Uses
    `KiteConnect.positions <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.positions>`_
    which corresponds to
    `GET /portfolio/positions <https://kite.trade/docs/connect/v3/portfolio/#positions>`_.

    Only the ``net`` slice of the response is used; ``day`` is ignored.
    Closed positions are filtered out so the output reflects the user's
    current market exposure.
    """
    positions = kite.positions() or {}
    net_positions = positions.get("net", []) or []

    open_positions = [p for p in net_positions if int(p.get("quantity") or 0) != 0]

    equity = [p for p in open_positions if p.get("exchange") in EQUITY_EXCHANGES]
    fno = [p for p in open_positions if p.get("exchange") in FNO_EXCHANGES]
    other = [
        p
        for p in open_positions
        if p.get("exchange") not in EQUITY_EXCHANGES and p.get("exchange") not in FNO_EXCHANGES
    ]

    _print_position_table("Positions: Equity (NSE / BSE)", equity)
    _print_position_table("Positions: F&O / Derivatives (NFO / BFO / CDS / BCD / MCX)", fno)
    if other:
        _print_position_table("Positions: Other", other)


# ---------------------------------------------------------------------------
# Cash balance (equity segment)
# ---------------------------------------------------------------------------
#
# ``KiteConnect.margins(segment="equity")`` corresponds to the HTTP
# endpoint ``GET /user/margins/equity`` documented at:
#   https://kite.trade/docs/connect/v3/user/#funds-and-margins
#
# Response shape (relevant subset):
#   {
#       "enabled": True,
#       "net": <float>,
#       "available": {
#           "adhoc_margin":   <float>,
#           "cash":           <float>,  # free cash available for new trades
#           "opening_balance":<float>,
#           "live_balance":   <float>,  # cash adjusted for intraday MTM
#           "collateral":     <float>,
#           "intraday_payin": <float>
#       },
#       "utilised": {
#           "debits":  <float>,         # total margin currently locked
#           ...other granular utilisation fields...
#       }
#   }
# ---------------------------------------------------------------------------


def _print_cash_balance(kite) -> None:
    """Print equity cash balance: available, live, and utilised.

    Calls ``kite.margins(segment="equity")``. See:
    https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.margins
    """
    margins = kite.margins(segment="equity") or {}
    available = margins.get("available", {}) or {}
    utilised = margins.get("utilised", {}) or {}

    available_cash = float(available.get("cash") or 0.0)
    live_balance = float(available.get("live_balance") or 0.0)
    utilised_debits = float(utilised.get("debits") or 0.0)

    print()
    print("=== Equity Cash Balance ===")
    print()
    print(f"Available cash: {available_cash:>14,.2f}")
    print(f"Live balance  : {live_balance:>14,.2f}")
    print(f"Utilised      : {utilised_debits:>14,.2f}")


def main() -> None:
    """Authenticate and emit the four account-snapshot sections."""
    kite = get_kite_client()
    _print_holdings(kite)
    _print_mf_holdings(kite)
    _print_positions(kite)
    _print_cash_balance(kite)


if __name__ == "__main__":
    main()
