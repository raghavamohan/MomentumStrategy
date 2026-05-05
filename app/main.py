"""List all equity holdings, current open positions (equity and F&O),
and the equity cash balance in the user's Zerodha account.

Run with:
    python -m app.main
"""

from __future__ import annotations

from tabulate import tabulate

from app.auth import get_kite_client


def _row(holding: dict) -> list:
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


EQUITY_EXCHANGES = {"NSE", "BSE"}
FNO_EXCHANGES = {"NFO", "BFO", "CDS", "BCD", "MCX"}


def _position_row(position: dict) -> list:
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


def _print_cash_balance(kite) -> None:
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
    kite = get_kite_client()
    _print_holdings(kite)
    _print_positions(kite)
    _print_cash_balance(kite)


if __name__ == "__main__":
    main()
