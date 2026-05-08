"""CLI entry point: render the current Zerodha account snapshot as ASCII tables.

This module is the **command-line** entry point. The same data is also
available in a tabbed browser dashboard via :mod:`app.web`
(``python -m app.web``); both interfaces share authentication code
(:mod:`app.auth`) and the on-disk access-token cache.

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

5. **User Profile** — basic account/profile metadata from
   ``KiteConnect.profile()``.

6. **Watch List (Nifty 50)** — quote snapshot for Nifty 50 constituents
   with LTP, day change, and sector.

7. **Overall Portfolio Summary** — consolidated totals for invested,
   current, and overall P&L across equity holdings, mutual fund holdings,
   and open positions.

Authentication is handled by :mod:`app.auth` (specifically
:func:`app.auth.get_kite_client`, which runs the interactive terminal
login flow).

References
----------
* Kite Connect HTTP API:    https://kite.trade/docs/connect/v3/
* pykiteconnect v4 API ref: https://kite.trade/docs/pykiteconnect/v4/
* pykiteconnect source:     https://github.com/zerodha/pykiteconnect
* Exchange & segment codes: https://kite.trade/docs/connect/v3/exchange/
* Mutual Funds endpoints:   https://kite.trade/docs/connect/v3/mutual-funds/
* Web counterpart (FastAPI dashboard): :mod:`app.web`
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tabulate import tabulate

from kiteconnect.exceptions import (
    DataException,
    KiteException,
    NetworkException,
    PermissionException,
    TokenException,
)

from app.auth import get_kite_client
from app.instruments import (
    get_cash_equity_kite_sector_lookups,
    get_cash_equity_isin_lookups,
    get_cash_equity_name_lookups,
    get_isin_to_industry,
    get_nifty50_symbols,
    get_nse_symbol_to_token_lookup,
    get_nse_symbol_to_industry,
    resolve_equity_sector,
    symbol_with_company_name,
)
from app.portfolio_model import (
    EQUITY_EXCHANGES,
    FNO_EXCHANGES,
    build_equity_holding,
    build_mf_holding,
    build_mf_underlying_breakdown,
    build_position,
    normalize_equity_sector,
    split_top_level_allocations,
    summarise_equity_by_sector,
)

SECTION_KEYS: tuple[str, ...] = (
    "profile",
    "equity",
    "mf",
    "positions",
    "cash",
    "watchlist",
    "summary",
)


def _is_transient_service_unavailable(exc: Exception) -> bool:
    """Return True for transient upstream outages worth a quick retry."""
    code = getattr(exc, "code", None)
    msg = str(exc).lower()
    return code == 503 or "503" in msg or "service unavailable" in msg


def _call_with_transient_retry(
    fn: Callable[[], Any],
    *,
    retries: int = 1,
    retry_delay_seconds: float = 1.0,
) -> Any:
    """Call ``fn`` with a single transient-retry guard for 503-like failures."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except (NetworkException, DataException) as exc:
            if attempt < retries and _is_transient_service_unavailable(exc):
                time.sleep(retry_delay_seconds)
                continue
            raise


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


def _row(
    holding: dict,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> list:
    """Map a single Kite ``holdings`` entry to a printable table row.

    The total quantity shown adds ``quantity`` (settled) and
    ``t1_quantity`` (today's buy still pending settlement) so that
    freshly bought shares are not invisible.
    """
    model = build_equity_holding(
        holding,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
        token_to_kite_sector=token_to_kite_sector,
        symbol_to_kite_sector=symbol_to_kite_sector,
        nse_symbol_to_industry=nse_symbol_to_industry,
        isin_to_industry=isin_to_industry,
        token_to_isin=token_to_isin,
        symbol_to_isin=symbol_to_isin,
    )
    sector_disp = model.sector if model.sector else "—"
    return [
        model.symbol_label,
        sector_disp,
        model.quantity,
        model.average_price,
        model.last_price,
        model.invested,
        model.current,
        model.pnl,
        model.day_change_percentage,
    ]


def _summarise_equity_by_sector(rows: list[list]) -> list[dict[str, float | str]]:
    """Aggregate holdings rows by sector and sort by invested descending."""
    model_rows = [
        {
            "sector": row[1],
            "invested": row[5],
            "current": row[6],
            "pnl": row[7],
        }
        for row in rows
    ]
    return summarise_equity_by_sector(model_rows)


def _split_top_level_allocations(
    sector_rows: list[dict[str, float | str]],
) -> list[dict[str, float | str]]:
    """Return Debt/Gold/Equity grouped allocation rows."""
    return split_top_level_allocations(sector_rows)


def _print_holdings(kite) -> tuple[float, float, float]:
    """Print the equity holdings table.

    Calls ``kite.holdings()`` once and renders the result. See:
    https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.holdings
    """
    print()
    print("=== Equity Holdings ===")
    try:
        holdings = _call_with_transient_retry(lambda: kite.holdings() or {})
    except KiteException as exc:
        print(f"Unable to fetch equity holdings right now: {exc}")
        return (0.0, 0.0, 0.0)
    except Exception as exc:  # noqa: BLE001 - final guard for CLI resilience
        print(f"Unexpected error while fetching equity holdings: {exc}")
        return (0.0, 0.0, 0.0)

    if not holdings:
        print("No equity holdings found in your Zerodha account.")
        return (0.0, 0.0, 0.0)

    token_to_name, symbol_to_name = get_cash_equity_name_lookups(kite)
    token_to_kite_sector, symbol_to_kite_sector = get_cash_equity_kite_sector_lookups(kite)
    token_to_isin, symbol_to_isin = get_cash_equity_isin_lookups(kite)
    nse_symbol_to_industry = get_nse_symbol_to_industry()
    isin_to_industry = get_isin_to_industry()
    rows = [
        _row(
            h,
            token_to_name,
            symbol_to_name,
            token_to_kite_sector,
            symbol_to_kite_sector,
            nse_symbol_to_industry,
            isin_to_industry,
            token_to_isin,
            symbol_to_isin,
        )
        for h in holdings
    ]
    rows.sort(key=lambda r: r[0])

    headers = [
        "Symbol",
        "Sector",
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
            floatfmt=(
                "",
                "",
                ".0f",
                ",.2f",
                ",.2f",
                ",.2f",
                ",.2f",
                ",.2f",
                ",.2f",
            ),
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

    sector_rows = _summarise_equity_by_sector(rows)
    top_level = _split_top_level_allocations(sector_rows)

    print()
    print("Sector-wise Allocation (Debt / Gold / Equity):")
    print(
        tabulate(
            [
                [
                    str(r["sector"]),
                    float(r["current"]),
                    (float(r["current"]) / total_current * 100.0) if total_current > 0 else 0.0,
                    float(r["pnl"]),
                ]
                for r in top_level
            ],
            headers=["Bucket", "Current", "Weight %", "P&L"],
            tablefmt="github",
            floatfmt=("", ",.2f", ",.2f", ",.2f"),
        )
    )

    print()
    print("Sector-wise Summary (all sectors):")
    print(
        tabulate(
            [
                [
                    str(r["sector"]),
                    float(r["invested"]),
                    float(r["current"]),
                    (float(r["current"]) / total_current * 100.0) if total_current > 0 else 0.0,
                    float(r["pnl"]),
                ]
                for r in sector_rows
            ],
            headers=["Sector", "Invested", "Current", "Weight %", "P&L"],
            tablefmt="github",
            floatfmt=("", ",.2f", ",.2f", ",.2f", ",.2f"),
        )
    )
    return (total_invested, total_current, total_pnl)


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
    model = build_mf_holding(holding)
    return [
        model.fund,
        model.folio,
        model.units,
        model.average_price,
        model.last_price,
        model.invested,
        model.current,
        model.pnl,
    ]


def _build_mf_underlying_breakdown(
    mf_rows: list[list[Any]],
) -> tuple[list[dict[str, Any]], str, list[str], int, int]:
    """Combine MF holdings into one instrument/sector weighted view via mfdata."""
    model_rows = [
        {
            "fund": row[0],
            "current": row[6],
        }
        for row in mf_rows
    ]
    return build_mf_underlying_breakdown(model_rows)


def _print_mf_underlying_breakdown(rows: list[list[Any]]) -> None:
    """Print aggregated MF underlying breakdown via mfdata.in."""
    print()
    print("=== MF Underlying Breakdown (mfdata.in) ===")
    try:
        table_rows, month, missing_funds, aggregated_count, total_count = _build_mf_underlying_breakdown(rows)
    except Exception as exc:  # noqa: BLE001 - best-effort enrichment only
        print(f"Unable to build MF underlying breakdown right now: {exc}")
        return

    if not table_rows:
        print("Underlying holdings are unavailable on mfdata.in for the current MF set.")
        print(f"Aggregated funds: {aggregated_count} / {total_count}")
        if missing_funds:
            print("Not aggregated:", ", ".join(missing_funds))
        return

    print()
    print(
        tabulate(
            [[r["instrument"], r["sector"], float(r["overall_weight"])] for r in table_rows],
            headers=["Underlying Instrument", "Sector", "Overall Weight (MF Portfolio) %"],
            tablefmt="github",
            floatfmt=("", "", ",.2f"),
        )
    )
    print()
    if month:
        print(f"Latest month: {month}")
    print(f"Aggregated funds: {aggregated_count} / {total_count}")
    if missing_funds:
        print("Not aggregated:", ", ".join(missing_funds))


def _print_mf_holdings(kite, *, include_underlyings: bool = True) -> tuple[float, float, float]:
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
        holdings = _call_with_transient_retry(lambda: kite.mf_holdings() or [])
    except PermissionException:
        print(
            "Mutual Funds API not enabled on this Kite Connect app. "
            "Enable the MF module at https://developers.kite.trade if you "
            "want this section."
        )
        return (0.0, 0.0, 0.0)
    except KiteException as exc:
        print(f"Unable to fetch mutual fund holdings right now: {exc}")
        return (0.0, 0.0, 0.0)
    except Exception as exc:  # noqa: BLE001 - final guard for CLI resilience
        print(f"Unexpected error while fetching mutual fund holdings: {exc}")
        return (0.0, 0.0, 0.0)

    if not holdings:
        print("No mutual fund holdings found in your Zerodha account.")
        return (0.0, 0.0, 0.0)

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
    if include_underlyings:
        _print_mf_underlying_breakdown(rows)
    else:
        print()
        print("MF underlying breakdown skipped (--no-mf-underlyings).")
    return (total_invested, total_current, total_pnl)


def _parse_args() -> argparse.Namespace:
    """Parse CLI flags for optional report sections."""
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="Render Zerodha account snapshot in terminal tables.",
        epilog=(
            "Section keys: "
            + ", ".join(SECTION_KEYS)
            + ". Example: python -m app.main --sections equity mf summary"
        ),
    )
    parser.add_argument(
        "--sections",
        nargs="+",
        choices=SECTION_KEYS,
        help=(
            "Show only these sections (space-separated). "
            "Default is all sections."
        ),
    )
    parser.add_argument(
        "--exclude-sections",
        nargs="+",
        choices=SECTION_KEYS,
        default=[],
        help="Hide specific sections from the output.",
    )
    parser.add_argument(
        "--no-mf-underlyings",
        action="store_true",
        help="Skip MF underlying breakdown enrichment from mfdata.in.",
    )
    return parser.parse_args()


def _resolve_sections(args: argparse.Namespace) -> set[str]:
    """Resolve final section set from include/exclude flags."""
    selected = set(args.sections) if args.sections else set(SECTION_KEYS)
    selected -= set(args.exclude_sections or [])
    return selected


def _equity_totals_only(kite) -> tuple[float, float, float]:
    """Fetch equity totals without printing section tables."""
    try:
        holdings = _call_with_transient_retry(lambda: kite.holdings() or {})
    except Exception:
        return (0.0, 0.0, 0.0)
    total_invested = 0.0
    total_current = 0.0
    total_pnl = 0.0
    for h in holdings:
        quantity = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
        avg = float(h.get("average_price") or 0.0)
        ltp = float(h.get("last_price") or 0.0)
        total_invested += avg * quantity
        total_current += ltp * quantity
        total_pnl += float(h.get("pnl") or 0.0)
    return (total_invested, total_current, total_pnl)


def _mf_totals_only(kite) -> tuple[float, float, float]:
    """Fetch MF totals without printing section tables."""
    try:
        holdings = _call_with_transient_retry(lambda: kite.mf_holdings() or [])
    except Exception:
        return (0.0, 0.0, 0.0)
    total_invested = 0.0
    total_current = 0.0
    total_pnl = 0.0
    for h in holdings:
        row = _mf_row(h)
        total_invested += float(row[5] or 0.0)
        total_current += float(row[6] or 0.0)
        total_pnl += float(row[7] or 0.0)
    return (total_invested, total_current, total_pnl)


def _positions_pnl_only(kite) -> float:
    """Fetch open-positions total P&L without printing tables."""
    try:
        positions = _call_with_transient_retry(lambda: kite.positions() or {})
    except Exception:
        return 0.0
    net_positions = positions.get("net", []) or []
    open_positions = [p for p in net_positions if int(p.get("quantity") or 0) != 0]
    return sum(float(p.get("pnl") or 0.0) for p in open_positions)


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

def _position_row(
    position: dict,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> list:
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
    model = build_position(
        position,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
        token_to_kite_sector=token_to_kite_sector,
        symbol_to_kite_sector=symbol_to_kite_sector,
        nse_symbol_to_industry=nse_symbol_to_industry,
        isin_to_industry=isin_to_industry,
        token_to_isin=token_to_isin,
        symbol_to_isin=symbol_to_isin,
    )
    ind_disp = model.sector if model.sector else "—"
    return [
        model.symbol_label,
        ind_disp,
        model.exchange,
        model.product,
        model.quantity,
        model.average_price,
        model.last_price,
        model.pnl,
        model.m2m,
    ]


def _print_position_table(
    title: str,
    positions: list[dict],
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> None:
    """Render one positions table with subtotals."""
    print()
    print(f"=== {title} ===")

    if not positions:
        print("(no open positions)")
        return

    rows = [
        _position_row(
            p,
            token_to_name,
            symbol_to_name,
            token_to_kite_sector,
            symbol_to_kite_sector,
            nse_symbol_to_industry,
            isin_to_industry,
            token_to_isin,
            symbol_to_isin,
        )
        for p in positions
    ]
    rows.sort(key=lambda r: r[0])

    headers = ["Symbol", "Industry", "Exch", "Prod", "Qty", "Avg", "LTP", "P&L", "M2M"]

    print()
    print(
        tabulate(
            rows,
            headers=headers,
            tablefmt="github",
            floatfmt=("", "", "", "", ".0f", ",.2f", ",.2f", ",.2f", ",.2f"),
        )
    )

    total_pnl = sum(r[7] for r in rows)
    total_m2m = sum(r[8] for r in rows)
    print()
    print(f"Open positions: {len(rows)}")
    print(f"Total P&L     : {total_pnl:>14,.2f}")
    print(f"Total M2M     : {total_m2m:>14,.2f}")


def _print_positions(kite) -> float:
    """Fetch and print open positions, separated by equity vs F&O.

    Uses
    `KiteConnect.positions <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.positions>`_
    which corresponds to
    `GET /portfolio/positions <https://kite.trade/docs/connect/v3/portfolio/#positions>`_.

    Only the ``net`` slice of the response is used; ``day`` is ignored.
    Closed positions are filtered out so the output reflects the user's
    current market exposure.
    """
    print()
    print("=== Open Positions ===")
    try:
        positions = _call_with_transient_retry(lambda: kite.positions() or {})
    except KiteException as exc:
        print(f"Unable to fetch positions right now: {exc}")
        return 0.0
    except Exception as exc:  # noqa: BLE001 - final guard for CLI resilience
        print(f"Unexpected error while fetching positions: {exc}")
        return 0.0
    net_positions = positions.get("net", []) or []

    open_positions = [p for p in net_positions if int(p.get("quantity") or 0) != 0]
    token_to_name, symbol_to_name = get_cash_equity_name_lookups(kite)
    token_to_kite_sector, symbol_to_kite_sector = get_cash_equity_kite_sector_lookups(kite)
    token_to_isin, symbol_to_isin = get_cash_equity_isin_lookups(kite)
    nse_symbol_to_industry = get_nse_symbol_to_industry()
    isin_to_industry = get_isin_to_industry()

    equity = [p for p in open_positions if p.get("exchange") in EQUITY_EXCHANGES]
    fno = [p for p in open_positions if p.get("exchange") in FNO_EXCHANGES]
    other = [
        p
        for p in open_positions
        if p.get("exchange") not in EQUITY_EXCHANGES and p.get("exchange") not in FNO_EXCHANGES
    ]

    common_kw = dict(
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
        token_to_kite_sector=token_to_kite_sector,
        symbol_to_kite_sector=symbol_to_kite_sector,
        nse_symbol_to_industry=nse_symbol_to_industry,
        isin_to_industry=isin_to_industry,
        token_to_isin=token_to_isin,
        symbol_to_isin=symbol_to_isin,
    )

    _print_position_table("Positions: Equity (NSE / BSE)", equity, **common_kw)
    _print_position_table(
        "Positions: F&O / Derivatives (NFO / BFO / CDS / BCD / MCX)",
        fno,
        **common_kw,
    )
    if other:
        _print_position_table("Positions: Other", other, **common_kw)

    return sum(float(p.get("pnl") or 0.0) for p in open_positions)


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
    try:
        margins = _call_with_transient_retry(lambda: kite.margins(segment="equity") or {})
    except KiteException as exc:
        print()
        print("=== Equity Cash Balance ===")
        print()
        print(
            "Cash balance is temporarily unavailable from Kite API "
            f"({exc}). Try again in a minute."
        )
        return
    except Exception as exc:  # noqa: BLE001 - final guard for CLI resilience
        print()
        print("=== Equity Cash Balance ===")
        print()
        print(f"Unexpected error while fetching cash balance: {exc}")
        return

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


def _print_overall_summary(
    total_invested: float,
    total_current: float,
    overall_pnl: float,
) -> None:
    """Print consolidated portfolio totals across holdings and positions."""
    print()
    print("=== Overall Portfolio Summary ===")
    print()
    print(f"Total invested: {total_invested:>14,.2f}")
    print(f"Current value : {total_current:>14,.2f}")
    print(f"Overall P&L   : {overall_pnl:>14,.2f}")


def _print_user_profile(kite) -> None:
    """Print basic account/profile details."""
    print()
    print("=== User Profile ===")
    try:
        profile = _call_with_transient_retry(lambda: kite.profile() or {})
    except KiteException as exc:
        print(f"Unable to fetch user profile right now: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - keep CLI resilient
        print(f"Unexpected error while fetching user profile: {exc}")
        return

    name = str(profile.get("user_name") or profile.get("user_shortname") or "--")
    user_id = str(profile.get("user_id") or "--")
    email = str(profile.get("email") or "--")
    broker = str(profile.get("broker") or "Zerodha")
    user_type = str(profile.get("user_type") or "--")
    products = ", ".join(profile.get("products") or []) or "--"
    exchanges = ", ".join(profile.get("exchanges") or []) or "--"

    print()
    print(f"Name             : {name}")
    print(f"User ID          : {user_id}")
    print(f"Email            : {email}")
    print(f"Broker           : {broker}")
    print(f"Account Type     : {user_type}")
    print(f"Enabled Products : {products}")
    print(f"Enabled Exchanges: {exchanges}")


def _print_watch_list(kite) -> None:
    """Print Nifty 50 watch list snapshot (same source as web dashboard)."""
    print()
    print("=== Watch List (Nifty 50) ===")

    try:
        symbols = get_nifty50_symbols()
    except Exception as exc:  # noqa: BLE001 - reference cache/network guard
        print(f"Unable to load Nifty 50 symbols right now: {exc}")
        return
    if not symbols:
        print("Watch list is empty (Nifty 50 symbols unavailable).")
        return

    token_to_name, symbol_to_name = get_cash_equity_name_lookups(kite)
    token_to_kite_sector, symbol_to_kite_sector = get_cash_equity_kite_sector_lookups(kite)
    token_to_isin, symbol_to_isin = get_cash_equity_isin_lookups(kite)
    nse_symbol_to_industry = get_nse_symbol_to_industry()
    isin_to_industry = get_isin_to_industry()
    nse_symbol_to_token = get_nse_symbol_to_token_lookup(kite)

    quote_keys = [f"NSE:{sym}" for sym in symbols]
    try:
        quote_batch = _call_with_transient_retry(lambda: kite.quote(quote_keys) if quote_keys else {})
    except KiteException as exc:
        print(f"Unable to fetch watch-list quotes right now: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - keep CLI resilient
        print(f"Unexpected error while fetching watch-list quotes: {exc}")
        return

    rows: list[list[Any]] = []
    for symbol in symbols:
        qkey = f"NSE:{symbol}"
        qrow = quote_batch.get(qkey) or {}
        token = int(qrow.get("instrument_token") or nse_symbol_to_token.get(symbol) or 0)
        ohlc = qrow.get("ohlc") or {}
        prev_close = float(ohlc.get("close") or 0.0)
        last_price = float(qrow.get("last_price") or 0.0)
        if prev_close > 0:
            change = last_price - prev_close
            change_pct = (change / prev_close) * 100.0
        else:
            change = 0.0
            change_pct = 0.0

        sector = normalize_equity_sector(
            symbol,
            resolve_equity_sector(
                symbol=symbol,
                exchange="NSE",
                instrument_token=token,
                token_to_name=token_to_name,
                symbol_to_name=symbol_to_name,
                token_to_kite_sector=token_to_kite_sector,
                symbol_to_kite_sector=symbol_to_kite_sector,
                nse_symbol_to_industry=nse_symbol_to_industry,
                isin_to_industry=isin_to_industry,
                token_to_isin=token_to_isin,
                symbol_to_isin=symbol_to_isin,
            ),
        )
        label = symbol_with_company_name(
            symbol=symbol,
            exchange="NSE",
            instrument_token=token,
            token_to_name=token_to_name,
            symbol_to_name=symbol_to_name,
        )
        rows.append([label, sector, last_price, change, change_pct])

    rows.sort(key=lambda r: r[4], reverse=True)
    print()
    print(
        tabulate(
            rows,
            headers=["Company", "Sector", "Live Price", "Change", "% Change Today"],
            tablefmt="github",
            floatfmt=("", "", ",.2f", ",.2f", ",.2f"),
        )
    )
    print()
    print(f"Watch list rows: {len(rows)}")


def main() -> None:
    """Authenticate and emit the account-snapshot sections."""
    args = _parse_args()
    selected_sections = _resolve_sections(args)
    try:
        kite = get_kite_client()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - show friendly startup failure
        raise SystemExit(f"Authentication failed: {exc}") from exc

    try:
        if "profile" in selected_sections:
            _print_user_profile(kite)

        if "equity" in selected_sections:
            eq_invested, eq_current, eq_pnl = _print_holdings(kite)
        else:
            eq_invested, eq_current, eq_pnl = _equity_totals_only(kite)

        if "mf" in selected_sections:
            mf_invested, mf_current, mf_pnl = _print_mf_holdings(
                kite,
                include_underlyings=not args.no_mf_underlyings,
            )
        else:
            mf_invested, mf_current, mf_pnl = _mf_totals_only(kite)

        if "positions" in selected_sections:
            positions_pnl = _print_positions(kite)
        else:
            positions_pnl = _positions_pnl_only(kite)

        if "cash" in selected_sections:
            _print_cash_balance(kite)
        if "watchlist" in selected_sections:
            _print_watch_list(kite)
        if "summary" in selected_sections:
            total_invested = eq_invested + mf_invested
            total_current = eq_current + mf_current
            overall_pnl = eq_pnl + mf_pnl + positions_pnl
            _print_overall_summary(total_invested, total_current, overall_pnl)
    except TokenException as exc:
        raise SystemExit(
            "Your Kite session expired during data fetch. "
            "Please rerun and login again. "
            f"Details: {exc}"
        ) from exc


if __name__ == "__main__":
    main()
