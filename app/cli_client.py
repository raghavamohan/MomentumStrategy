"""CLI client: prints portfolio tables using the running server's HTTP API.

Requires ``python -m app.server`` (or uvicorn) and a valid Kite token on disk
(same as the browser after login). Authenticates with:

``Authorization: Bearer <access_token>`` from ``.access_token.json``.

Environment:

``MOMENTUM_SERVER_URL``
    Base URL (default ``http://127.0.0.1:5000``).
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from typing import Any

from tabulate import tabulate

from app.auth import load_cached_access_token

SECTION_KEYS: tuple[str, ...] = (
    "profile",
    "equity",
    "mf",
    "positions",
    "cash",
    "watchlist",
    "summary",
)


def _server_url() -> str:
    return os.getenv("MOMENTUM_SERVER_URL", "http://127.0.0.1:5000").rstrip("/")


def _fetch_json(path: str, token: str, *, timeout: float = 120.0) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{_server_url()}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Portfolio CLI (HTTP client to app.server).")
    p.add_argument(
        "--sections",
        type=str,
        default="",
        help=f"Comma-separated subset of: {','.join(SECTION_KEYS)}",
    )
    p.add_argument(
        "--exclude-sections",
        type=str,
        default="",
        help="Comma-separated sections to skip.",
    )
    p.add_argument(
        "--no-mf-underlyings",
        action="store_true",
        help="Skip MF underlying aggregation when printing mf section.",
    )
    return p.parse_args()


def _resolve_sections(args: argparse.Namespace) -> set[str]:
    if args.sections.strip():
        keys = {x.strip().lower() for x in args.sections.split(",") if x.strip()}
        bad = keys - set(SECTION_KEYS)
        if bad:
            raise SystemExit(f"Unknown section keys: {sorted(bad)}")
        return keys
    base = set(SECTION_KEYS)
    if args.exclude_sections.strip():
        ex = {x.strip().lower() for x in args.exclude_sections.split(",") if x.strip()}
        base -= ex
    return base


def _print_profile(data: dict[str, Any]) -> None:
    prof = data.get("user_profile") or {}
    print()
    print("=== User Profile ===")
    rows = [
        ["Name", prof.get("name", "")],
        ["User ID", prof.get("user_id", "")],
        ["Email", prof.get("email", "")],
        ["Broker", prof.get("broker", "")],
        ["User type", prof.get("user_type", "")],
    ]
    print(tabulate(rows, tablefmt="github"))


def _print_equity(data: dict[str, Any]) -> None:
    rows = data.get("equity_holdings") or []
    print()
    print("=== Equity Holdings ===")
    if not rows:
        print("No equity holdings.")
        return
    table = []
    for r in rows:
        table.append(
            [
                r.get("symbol_label") or r.get("tradingsymbol"),
                r.get("sector", ""),
                r.get("quantity"),
                r.get("average_price"),
                r.get("last_price"),
                r.get("invested"),
                r.get("current"),
                r.get("pnl"),
                r.get("day_change_percentage"),
            ]
        )
    print(
        tabulate(
            table,
            headers=[
                "Company",
                "Sector",
                "Qty",
                "Avg",
                "LTP",
                "Invested",
                "Current",
                "P&L",
                "Day %",
            ],
            tablefmt="github",
            floatfmt=",.2f",
        )
    )


def _print_positions(data: dict[str, Any]) -> None:
    eq = data.get("equity_positions") or []
    fno = data.get("fno_positions") or []
    print()
    print("=== Equity Positions ===")
    if eq:
        print(
            tabulate(
                [
                    [
                        r.get("symbol_label") or r.get("tradingsymbol"),
                        r.get("sector"),
                        r.get("exchange"),
                        r.get("quantity"),
                        r.get("last_price"),
                        r.get("pnl"),
                        r.get("m2m"),
                    ]
                    for r in eq
                ],
                headers=["Company", "Sector", "Exch", "Qty", "LTP", "P&L", "M2M"],
                tablefmt="github",
                floatfmt=",.2f",
            )
        )
    else:
        print("No open equity positions.")
    print()
    print("=== F&O Positions ===")
    if fno:
        print(
            tabulate(
                [
                    [
                        r.get("symbol_label") or r.get("tradingsymbol"),
                        r.get("sector"),
                        r.get("exchange"),
                        r.get("product"),
                        r.get("quantity"),
                        r.get("last_price"),
                        r.get("pnl"),
                        r.get("m2m"),
                    ]
                    for r in fno
                ],
                headers=["Company", "Sector", "Exch", "Prod", "Qty", "LTP", "P&L", "M2M"],
                tablefmt="github",
                floatfmt=",.2f",
            )
        )
    else:
        print("No open F&O positions.")


def _print_cash(data: dict[str, Any]) -> None:
    c = data.get("cash") or {}
    print()
    print("=== Cash Balance ===")
    print(
        tabulate(
            [
                ["Available cash", c.get("available_cash")],
                ["Live balance", c.get("live_balance")],
                ["Utilised", c.get("utilised")],
            ],
            tablefmt="github",
            floatfmt=",.2f",
        )
    )


def _print_watchlist(data: dict[str, Any]) -> None:
    wl = data.get("watch_list") or []
    print()
    print("=== Watch List (Nifty 50) ===")
    if not wl:
        print("No rows.")
        return
    print(
        tabulate(
            [
                [
                    r.get("label"),
                    r.get("symbol"),
                    r.get("sector"),
                    r.get("last_price"),
                    r.get("change"),
                    r.get("change_pct"),
                ]
                for r in wl
            ],
            headers=["Company", "Symbol", "Sector", "LTP", "Change", "Change %"],
            tablefmt="github",
            floatfmt=",.2f",
        )
    )


def _print_summary(data: dict[str, Any]) -> None:
    ps = data.get("portfolio_summary") or {}
    print()
    print("=== Portfolio Summary ===")
    print(
        tabulate(
            [
                ["Total invested", ps.get("total_invested")],
                ["Total current", ps.get("total_current")],
                ["Holdings P&L", ps.get("holdings_pnl")],
                ["Positions P&L", ps.get("positions_pnl")],
                ["Overall P&L", ps.get("overall_pnl")],
            ],
            tablefmt="github",
            floatfmt=",.2f",
        )
    )


def _print_mf_from_server(token: str, *, include_underlyings: bool) -> None:
    payload = _fetch_json("/dashboard/mf-holdings", token)
    print()
    print("=== Mutual Fund Holdings ===")
    holdings = payload.get("rows") or payload.get("holdings") or payload.get("mf_holdings") or []
    if isinstance(holdings, list) and holdings:
        rows = [
            [
                r.get("fund") or r.get("scheme_name"),
                r.get("folio"),
                r.get("units"),
                r.get("average_price"),
                r.get("last_price"),
                r.get("invested"),
                r.get("current"),
                r.get("pnl"),
            ]
            for r in holdings
        ]
        print(
            tabulate(
                rows,
                headers=["Fund", "Folio", "Units", "Avg", "NAV", "Invested", "Current", "P&L"],
                tablefmt="github",
                floatfmt=",.2f",
            )
        )
    else:
        print(payload.get("message") or "No MF holdings or unavailable.")
    if include_underlyings:
        try:
            und = _fetch_json("/dashboard/mf-underlyings?tone=all", token)
            print()
            print("=== MF Underlyings (aggregated) ===")
            print(json.dumps(und, indent=2, default=str)[:4000])
        except urllib.error.HTTPError:
            print("(Underlying aggregation unavailable.)")


def main() -> None:
    args = _parse_args()
    selected = _resolve_sections(args)
    token = load_cached_access_token()
    if not token:
        raise SystemExit(
            "No cached Kite access token. Start the server (`python -m app.server`), "
            "open the dashboard, and complete Zerodha login."
        )

    try:
        snap = _fetch_json("/api/v1/portfolio/snapshot", token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit(
                "Unauthorized — token may be expired. Log in again via the browser dashboard."
            ) from exc
        raise SystemExit(f"Server error: {exc}") from exc
    except OSError as exc:
        raise SystemExit(
            f"Cannot reach server at {_server_url()}. Is `python -m app.server` running?"
        ) from exc

    if "profile" in selected:
        _print_profile(snap)
    if "equity" in selected:
        _print_equity(snap)
    if "positions" in selected:
        _print_positions(snap)
    if "cash" in selected:
        _print_cash(snap)
    if "watchlist" in selected:
        _print_watchlist(snap)
    if "summary" in selected:
        _print_summary(snap)
    if "mf" in selected:
        _print_mf_from_server(token, include_underlyings=not args.no_mf_underlyings)


if __name__ == "__main__":
    main()
