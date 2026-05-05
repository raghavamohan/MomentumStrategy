# MomentumStrategy

A small Python CLI that lists all equity holdings, current open positions
(equity and F&O), and the equity cash balance in your Zerodha account
using the official [Kite Connect](https://kite.trade) Python client
([pykiteconnect](https://github.com/zerodha/pykiteconnect)).

## What it does

- Authenticates against Zerodha using your Kite Connect API key + secret.
- Calls `kite.holdings()` and prints a table of every stock you own with:
  symbol, exchange, quantity, average price, last price, invested value,
  current value, P&L, and day change %.
- Calls `kite.positions()` and prints two separate tables for currently
  open positions (qty != 0):
    - **Equity** positions on NSE / BSE.
    - **F&O / derivatives** positions on NFO / BFO / CDS / BCD / MCX.
- Calls `kite.margins(segment="equity")` and prints the available cash,
  live balance, and utilised margin for the equity segment.
- Caches the daily access token in `.access_token.json` so you don't have to
  log in again until it expires (Kite tokens die at ~6 AM the next trading day).

## Prerequisites

- Python 3.10 or newer.
- A Kite Connect app on <https://developers.kite.trade>.
  - Note your **API key** and **API secret**.
  - Set the **Redirect URL** to anything you control. The app does not run a
    web server; you just need to be able to read the `request_token` query
    parameter from the URL the browser is redirected to. Something like
    `https://127.0.0.1` is fine.

## Setup

From the project root (`E:\MomentumStrategy`):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Then edit [`.env`](.env) and fill in your credentials:

```
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
```

`.env` is gitignored, so it will not be committed.

## Run

```powershell
python -m app.main
```

On the first run (and once per day after the access token expires), you will
see something like:

```
Kite Connect login required.
1) Open this URL in your browser and log in to Zerodha:
   https://kite.trade/connect/login?api_key=...&v=3

2) After login Zerodha redirects to your app's Redirect URL.
   Copy the value of the `request_token` query parameter from
   that redirected URL (it looks like ?request_token=XXXX&...).

Paste request_token here:
```

Open the URL, log in to Zerodha, copy the `request_token` from the redirect
URL's query string, and paste it back into the terminal. After that, your
holdings table is printed.

## Project layout

```
.
├── .env                  # your Kite API key + secret (gitignored)
├── .env.example          # template for the above
├── .gitignore
├── README.md
├── requirements.txt
├── .access_token.json    # cached daily access token (gitignored, auto-created)
└── app/
    ├── __init__.py
    ├── auth.py           # Kite login flow + token caching
    └── main.py           # entry point: lists equity holdings
```

## Notes

- This script only reads. It calls `kite.holdings()`, `kite.positions()`,
  `kite.margins()` and `kite.profile()` (the last only to validate the
  cached token). It does not place, modify, or cancel any orders.
- For positions, the script uses the `net` view (consolidated current
  positions) and filters out closed/zero-quantity rows. If you also want to
  see intraday round-trips that net to zero, switch to `positions["day"]`
  inside `_print_positions`.
- Mutual fund holdings are not included; use `kite.mf_holdings()` if you want
  those too.
