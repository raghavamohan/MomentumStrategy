# MomentumStrategy

A small Python app (CLI **and** local web dashboard) that surfaces the
current state of your Zerodha account using the official
[Kite Connect](https://kite.trade) Python client
([pykiteconnect](https://github.com/zerodha/pykiteconnect)). It shows:

- Equity holdings (long-term stocks held in demat).
- Mutual fund holdings (units per folio with NAV and P&L).
- Open positions, separated into equity (NSE / BSE) and F&O / derivatives.
- Equity segment cash balance (available cash, live balance, utilised).

There are two interfaces, sharing the same authentication flow and token cache:

- **CLI** — `python -m app.main`. Prints the four sections as ASCII
  tables, suitable for terminals and scripting.
- **Web dashboard** — `python -m app.web`. Starts a FastAPI server on
  http://127.0.0.1:5000/ with one tab per category in a single page and
  overlays live equity/F&O prices via Kite WebSocket (`KiteTicker`).

## What it does

- Authenticates against Zerodha using your Kite Connect API key + secret.
- Calls `kite.holdings()` and renders every stock you own with: symbol,
  exchange, quantity, average price, last price, invested value, current
  value, P&L, and day change %.
- Calls `kite.mf_holdings()` and renders every mutual fund you own with:
  fund name, folio, units, average NAV, latest NAV, invested value,
  current value, and P&L. If the Mutual Funds module isn't enabled on
  your Kite Connect app, a friendly notice is shown instead.
- Calls `kite.positions()` and shows two tables for currently open
  positions (`quantity != 0`):
    - **Equity** positions on NSE / BSE.
    - **F&O / derivatives** positions on NFO / BFO / CDS / BCD / MCX.
- Calls `kite.margins(segment="equity")` and shows available cash, live
  balance, and utilised margin for the equity segment.
- For the **web dashboard**, subscribes to equity/F&O instrument tokens
  over WebSocket (`KiteTicker`) and overlays live LTPs before rendering
  holdings/positions so current value, P&L, and day change are refreshed
  from streamed prices.
- Caches the daily access token in `.access_token.json` so you don't
  have to log in again until it expires (Kite tokens die at ~6 AM IST
  the next trading day). The CLI and web dashboard share this cache.
- Auto-refreshes the dashboard every `DASHBOARD_REFRESH_SECONDS`
  (default `2` seconds; minimum `1` second).

## Prerequisites

- Python 3.10 or newer.
- A Kite Connect app on <https://developers.kite.trade>.
  - Note your **API key** and **API secret**.
  - **Redirect URL** depends on which interface you use most:
    - For the **web dashboard** (recommended), set the Redirect URL in
      your Kite Connect app to **`http://127.0.0.1:5000/callback`**.
      Zerodha allows `http://127.0.0.1` and `http://localhost` URLs for
      development.
    - For the **CLI** alone, the Redirect URL can be anything you
      control (e.g. `https://127.0.0.1`); you just copy the
      `request_token` from the redirected URL and paste it back into
      the terminal.

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
DASHBOARD_REFRESH_SECONDS=2
```

`.env` is gitignored, so it will not be committed.

## Run — Web dashboard (recommended)

```powershell
python -m app.web
```

This starts a FastAPI server on http://127.0.0.1:5000/. Open that URL
in your browser, click **Login with Zerodha**, complete the Zerodha
sign-in, and you'll be redirected to the dashboard.

The dashboard is a single page with five tabs:

1. **Equity Holdings** — table of demat stocks with totals.
2. **Mutual Funds** — table of MF folios with NAVs and totals.
3. **Equity Positions** — open NSE / BSE positions.
4. **F&O Positions** — open NFO / BFO / CDS / BCD / MCX positions.
5. **Cash Balance** — available cash, live balance, utilised.

Click **Refresh** to force an immediate re-fetch. The page also
auto-refreshes at the configured interval. Click **Logout** to clear the
browser session **and** delete the on-disk token cache so both dashboard
and CLI require a fresh Zerodha login.

## Run — CLI

```powershell
python -m app.main
```

On the first run (and once per day after the access token expires), you
will see something like:

```
Kite Connect login required.
1) Open this URL in your browser and log in to Zerodha:
   https://kite.trade/connect/login?api_key=...&v=3

2) After login Zerodha redirects to your app's Redirect URL.
   Copy the value of the `request_token` query parameter from
   that redirected URL (it looks like ?request_token=XXXX&...).

Paste request_token here:
```

Open the URL, log in to Zerodha, copy the `request_token` from the
redirect URL's query string, and paste it back into the terminal. After
that, the four sections are printed.

## Project layout

```
.
├── .env                  # your Kite API key + secret (gitignored)
├── .env.example          # template for the above
├── .gitignore
├── README.md
├── requirements.txt
├── .access_token.json    # cached daily access token (gitignored, auto-created)
├── app/
│   ├── __init__.py       # package docstring + entry point index
│   ├── auth.py           # Kite login flow + token caching (shared by CLI + web)
│   ├── live_prices.py    # KiteTicker websocket manager for live LTP snapshots
│   ├── main.py           # CLI entry point: holdings, MF, positions, cash
│   └── web.py            # FastAPI dashboard entry point (tabs)
└── templates/
    ├── base.html         # shared layout + CSS
    ├── index.html        # login landing page
    └── dashboard.html    # tabbed dashboard
```

## Kite Connect API reference

External documentation for everything this app talks to:

- **Kite Connect HTTP API (v3) overview** —
  <https://kite.trade/docs/connect/v3/>
- **Official Python client (`pykiteconnect`) source** —
  <https://github.com/zerodha/pykiteconnect>
- **`pykiteconnect` v4 API reference** —
  <https://kite.trade/docs/pykiteconnect/v4/>
- **Kite Connect developer console** (where you create the app, set the
  Redirect URL, and get your `api_key` / `api_secret`) —
  <https://developers.kite.trade/>

### Endpoints used by this app

| Where used                          | `pykiteconnect` method                       | HTTP endpoint                  | Docs                                                                                                                                                       |
| ----------------------------------- | -------------------------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/auth.py` / `app/web.py:/login` | `KiteConnect.login_url()`                    | (URL builder, no HTTP call)    | <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.login_url>                                                                              |
| `app/auth.py` / `app/web.py:/callback` | `KiteConnect.generate_session()`          | `POST /session/token`          | <https://kite.trade/docs/connect/v3/user/#login-flow> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.generate_session>               |
| `app/auth.py`                       | `KiteConnect.set_access_token()`             | (in-memory)                    | <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.set_access_token>                                                                       |
| `app/auth.py` (validation)          | `KiteConnect.profile()`                      | `GET /user/profile`            | <https://kite.trade/docs/connect/v3/user/#user-profile> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.profile>                      |
| `app/auth.py` (error)               | `kiteconnect.exceptions.TokenException`      | (raised on 403 token errors)   | <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.exceptions.TokenException>                                                                          |
| `app/main.py:_print_holdings` · `app/web.py:/dashboard` | `KiteConnect.holdings()`                  | `GET /portfolio/holdings`      | <https://kite.trade/docs/connect/v3/portfolio/#holdings> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.holdings>                    |
| `app/main.py:_print_mf_holdings` · `app/web.py:/dashboard` | `KiteConnect.mf_holdings()`            | `GET /mf/holdings`             | <https://kite.trade/docs/connect/v3/mutual-funds/#mutual-fund-holdings> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.mf_holdings>  |
| `app/main.py:_print_positions` · `app/web.py:/dashboard` | `KiteConnect.positions()`                | `GET /portfolio/positions`     | <https://kite.trade/docs/connect/v3/portfolio/#positions> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.positions>                  |
| `app/main.py:_print_cash_balance` · `app/web.py:/dashboard` | `KiteConnect.margins("equity")`       | `GET /user/margins/equity`     | <https://kite.trade/docs/connect/v3/user/#funds-and-margins> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.margins>                 |
| `app/live_prices.py` · `app/web.py:/dashboard` | `KiteTicker.subscribe()` / `set_mode("ltp")` | `wss://ws.kite.trade` | <https://kite.trade/docs/connect/v3/websocket/> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteTicker> |
| MF section (error)                  | `kiteconnect.exceptions.PermissionException` | (raised on 403 when MF API not enabled) | <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.exceptions.PermissionException>                                                            |

### Login flow used by `app/web.py`

```mermaid
sequenceDiagram
    participant U as User (browser)
    participant W as MomentumStrategy web (FastAPI)
    participant Z as Zerodha (kite.trade)
    U->>W: GET /
    W-->>U: index.html (Login button)
    U->>W: GET /login
    W->>U: 303 redirect to KiteConnect.login_url()
    U->>Z: GET https://kite.trade/connect/login?api_key=...
    U->>Z: log in + approve access
    Z->>U: 302 redirect to http://127.0.0.1:5000/callback?request_token=XXXX
    U->>W: GET /callback?request_token=XXXX
    W->>Z: POST /session/token (request_token + api_secret)
    Z->>W: { access_token, ... }
    W->>W: save_cached_access_token + set session["authenticated"]
    W->>U: 303 redirect to /dashboard
    U->>W: GET /dashboard
    W->>Z: GET /portfolio/holdings, /mf/holdings, /portfolio/positions, /user/margins/equity
    W->>Z: WebSocket subscribe (equity/F&O instrument tokens)
    Z->>W: JSON responses
    Z-->>W: live LTP ticks
    W-->>U: dashboard.html (tabs rendered with live LTP overlays)
```

### Login flow used by `app/main.py` (CLI)

```mermaid
sequenceDiagram
    participant U as User (browser + terminal)
    participant C as MomentumStrategy CLI
    participant Z as Zerodha (kite.trade)
    C->>C: load KITE_API_KEY / KITE_API_SECRET from .env
    C->>U: print login_url(api_key)
    U->>Z: GET https://kite.trade/connect/login?api_key=...
    U->>Z: log in + approve access
    Z->>U: 302 redirect to Redirect URL?request_token=XXXX
    U->>C: paste request_token into terminal
    C->>Z: POST /session/token (request_token + api_secret)
    Z->>C: { access_token, ... }
    C->>C: cache access_token in .access_token.json
    C->>Z: GET /portfolio/holdings, /mf/holdings, /portfolio/positions, /user/margins/equity
```

The `access_token` returned by `generate_session` is valid until
approximately **6 AM IST the next trading day**. After expiry, the next
call surfaces a
[`TokenException`](https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.exceptions.TokenException),
the cache is discarded, and the interactive login runs again.

### Field reference for the data shown

For complete schemas of every field returned by these endpoints (we use
only a subset), refer to the Kite Connect HTTP API docs:

- **Holdings entry fields** —
  <https://kite.trade/docs/connect/v3/portfolio/#holdings>
- **MF holdings entry fields** —
  <https://kite.trade/docs/connect/v3/mutual-funds/#mutual-fund-holdings>
- **Positions entry fields** (with `net` vs `day` semantics) —
  <https://kite.trade/docs/connect/v3/portfolio/#positions>
- **Margins / funds response** (per segment) —
  <https://kite.trade/docs/connect/v3/user/#funds-and-margins>
- **Exchange & segment codes** (NSE / BSE / NFO / BFO / CDS / BCD / MCX) —
  <https://kite.trade/docs/connect/v3/exchange/>
- **Mutual Funds API overview** (orders, SIPs, holdings, instruments) —
  <https://kite.trade/docs/connect/v3/mutual-funds/>
- **Order constants** (variety, product, order_type, validity) —
  <https://kite.trade/docs/connect/v3/orders/#constants>
  (not used yet, but referenced by the pykiteconnect class constants
  `kite.PRODUCT_*`, `kite.EXCHANGE_*`, etc.)
- **WebSocket streaming** (live quotes/LTP) —
  <https://kite.trade/docs/connect/v3/websocket/>

## Notes

- This app only reads. It calls `kite.holdings()`, `kite.mf_holdings()`,
  `kite.positions()`, `kite.margins()`, `kite.profile()`, and (for web)
  subscribes to live quote ticks via `KiteTicker`. It does not place,
  modify, or cancel any orders.
- The web dashboard binds to `127.0.0.1` only, so it is **not**
  reachable from other machines on the network. To expose it elsewhere,
  change the `host` argument in `app.web.main`.
- For positions, the app uses the `net` view (consolidated current
  positions) and filters out closed/zero-quantity rows. If you also
  want to see intraday round-trips that net to zero, switch to
  `positions["day"]` inside the `dashboard` route / `_print_positions`.
- The mutual funds section requires the Mutual Funds module to be
  enabled on your Kite Connect app (toggle it in the
  [developer console](https://developers.kite.trade)). If it isn't,
  both the CLI and the dashboard catch the resulting
  [`PermissionException`](https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.exceptions.PermissionException)
  and surface a notice rather than failing.
- Index constituents (e.g. NIFTY 50) are **not** part of Kite Connect.
  Use the NSE Indices CSV at
  <https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv>
  (note: NSE applies anti-bot protection; you'll need a session warm-up
  with browser-like headers to fetch it server-side).
