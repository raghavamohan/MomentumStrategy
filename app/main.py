"""List all equity holdings in the user's Zerodha account.

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


def main() -> None:
    kite = get_kite_client()
    holdings = kite.holdings()

    if not holdings:
        print("\nNo equity holdings found in your Zerodha account.")
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


if __name__ == "__main__":
    main()
