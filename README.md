# MomentumStrategy

A small Python app (CLI **and** local web dashboard) that surfaces the
current state of your Zerodha account using the official
[Kite Connect](https://kite.trade) Python client
([pykiteconnect](https://github.com/zerodha/pykiteconnect)). It shows:

- Equity holdings (long-term stocks held in demat).
- Mutual fund holdings (units per folio with NAV and P&L).
- Open positions, separated into equity (NSE / BSE) and F&O / derivatives.
- Equity segment cash balance (available cash, live balance, utilised).
- Overall portfolio summary (invested/current totals and consolidated P&L
  across holdings, mutual funds, and open positions).

There are two interfaces, sharing the same authentication flow and token cache:

- **CLI** — `python -m app.cli_client` (or `python -m app.cli_client` shim). Talks to
  the local server over HTTP and prints the same sections as ASCII tables. The
  server must be running and you must be logged in via the browser once so the
  Kite access token is on disk. Use `Authorization: Bearer` under the hood.
- **Web dashboard** — `python -m app.server` (or `python -m app.server` shim).
  Starts a FastAPI server on http://127.0.0.1:5000/ with portfolio summary
  cards, tabs, live equity/F&O prices via Kite WebSocket (`KiteTicker`), and
  JSON APIs under `/api/v1/`.

Portfolio normalization and aggregates run on the server in
`app/portfolio_model.py`. The CLI calls the REST API (`/api/v1/...`) implemented
there; it does not import `portfolio_model` directly.

## Architecture

The app is organized in layers so **per-source I/O** stays in small modules,
**disk persistence** is centralized, and **portfolio-facing APIs** stay in one place.

1. **Per-source providers** (`app/cache/*_provider.py`)
   - **`kite_provider`** — cash-equity instrument maps (names, ISIN, Kite `sector`,
     NSE symbol→token) from `KiteConnect.instruments()` when a session exists.
   - **`nse_provider`** — NSE index CSVs (Nifty 50 universe, symbol/ISIN→`Industry` maps).
   - **`yfinance_provider`** — equity `industry`/`sector` labels persisted under the
     `yfinance` section of the model cache.
   - **`mfdata_provider`** — mfdata.in search + family holdings metadata (`mfdata` section).
   - **`marketsmith_provider`** — daily MarketSmith India regime snapshot (`marketsmith` section).

   **Contributors:** Follow **`app/cache/REFERENCE_PROVIDERS.md`** when adding or
   changing providers (persistence family, warmup, background jobs, notifications,
   `*_reference_debug_snapshot`). Optional typing: **`app/cache/reference_provider.py`**
   (`ReferenceWarmupProvider`). Shared symbol/name/ISIN normalization:
   **`app/cache/text_normalize.py`**.

2. **Persistence and coordination**
   - **`app/cache/model_cache_store.py`** — single file `.cache/model_cache.json`;
     typed helpers load/update sections (`yfinance`, `reference_data`, `mfdata`,
     `marketsmith`, …).
   - **`app/cache/reference_cache_internal.py`** — shared `reference_data` disk cache,
     source labels (`REFERENCE_CACHE_LAST_SOURCE`, including **`yfinance`** for
     unified provenance via **`set_reference_last_source`**), and locking so NSE +
     Kite-derived maps stay consistent.

3. **Reference snapshots and warmup**
   - **`app/reference_snapshot.py`** — builds an immutable **`ReferenceSnapshot`**
     (`build_reference_snapshot`) from provider caches for each dashboard render;
     **`warm_reference_snapshot`** invokes each module’s **`warmup(ctx)`** in the
     order defined by **`REFERENCE_PROVIDER_WARMUPS`** (currently NSE → yfinance →
     mfdata → MarketSmith → Kite). **`app/reference_context.py`** supplies
     **`WarmupContext`**; see its docstring for which providers read **`kite`**,
     **`force_refresh`**, **`marketsmith_force_sync`**, and related flags.
   - **`app/reference_notifications.py`** — debounced revision bump + dashboard
     cache-refresh signal when providers update disk state (including after mfdata’s
     **`flush_mfdata_disk_cache`** successfully persists).
   - **`app/services/cache_warmup.py`** — **`run_startup_cache_warmup_sync`** loads a Kite
     client when a valid cached token exists, runs **`warm_reference_snapshot`** once
     (`force_refresh` when Kite is present), then **`warm_mfdata_holdings_cache`**.
   - **`app/services/cache_orchestrator.py`** — **`run_startup_cache_warmup`** starts that
     sync routine on a daemon thread unless **`MOMENTUM_SKIP_CACHE_WARMUP`** is set.
     **`app/server.py`** invokes it from lifespan and again after **`/callback`** login.

4. **Shared model layer** (`app/portfolio_model.py`)
   - Normalized holdings/MF/positions entities and portfolio aggregates.
   - Sector resolution (`resolve_equity_sector`) orchestrating yfinance cache → Kite → NSE → ISIN.
   - **`warm_reference_caches()`** delegates to **`warm_reference_snapshot`**.
   - **`get_reference_cache_debug_snapshot()`** merges per-provider
     **`*_reference_debug_snapshot(now)`** rows (cash equity, NSE, yfinance,
     mfdata, MarketSmith, etc.) for dashboard timing/API introspection.
   - Re-exports such as **`get_marketsmith_market_condition`** from
     **`app/cache/marketsmith_provider`** so **`app/server.py`** imports most of this
     surface from one module.

5. **Presentation**
   - CLI: `app/cli_client.py` (HTTP client, ASCII tables).
   - Web: `app/server.py` + `templates/` (FastAPI, HTML, **`app/live_prices.py`** WebSocket feed).

Supporting modules unchanged in role: **`app/auth.py`** (Kite login + token cache),
**`app/events.py`** (dashboard refresh hooks).

```text
External APIs (Kite REST/WS, NSE CSVs, yfinance, mfdata.in, MarketSmith)
          |
          v
  app/cache/*_provider.py  +  model_cache_store / reference_cache_internal
          |
          v
  app/reference_snapshot.py (ReferenceSnapshot, warm_reference_snapshot, REFERENCE_PROVIDER_WARMUPS)
          |
          v
  app/portfolio_model.py   <- normalization, sector resolution, aggregates
      |             |
      v             v
  app/cli_client.py     app/server.py (+ live_prices / cache orchestrator)
```

Design intent: UI code stays independent from business/data-model logic; both
CLI and dashboard should only format/render model outputs. Add new reference
sources as **`app/cache/<name>_provider.py`**, append **`warmup`** to
**`REFERENCE_PROVIDER_WARMUPS`** in **`reference_snapshot`**, expose any needed
reads via **`portfolio_model`** (and register **`*_reference_debug_snapshot`** in
**`get_reference_cache_debug_snapshot`** when observability matters). See **`REFERENCE_PROVIDERS.md`**.

Contribution guideline: add or change portfolio calculations/normalization in
`app/portfolio_model.py` first, then adapt CLI/web rendering if needed.

## What it does

- Shows your full portfolio in one place across:
  - Equity holdings
  - Mutual fund holdings
  - Open positions (equity and F&O)
  - Cash/margin balance
- Adds account profile and Nifty 50 watch list snapshots in the CLI.
- Gives both a **CLI view** and a **local web dashboard**, so you can choose
  terminal output or a visual tabbed UI.
- Displays live market movement in the dashboard, including refreshed
  holdings/positions values and P&L.
- Provides a portfolio summary with key totals such as invested value,
  current value, and overall profit/loss.
- Adds mutual fund underlying insights (stock/sector breakdown and weights)
  in both dashboard and CLI when holdings data is available from `mfdata.in`.
- Shows the India **market regime** from MarketSmith India (Confirmed Uptrend,
  Correction, etc.) on the dashboard; the value is fetched via
  `app.cache.marketsmith_provider.get_marketsmith_market_condition()` (also
  importable from `app.portfolio_model`) and surfaced in HTML and in
  ``dashboard-bootstrap`` as ``marketCondition`` for client scripts on load.
- Reuses login state between runs, so day-to-day usage usually requires less
  repeated sign-in.

## Prerequisites

- Python 3.10 or newer.
- A Kite Connect app on <https://developers.kite.trade>.
  - Note your **API key** and **API secret**.
  - **Redirect URL** depends on which interface you use most:
    - For the **web dashboard** (recommended), set the Redirect URL in
      your Kite Connect app to **`http://127.0.0.1:5000/callback`**.
      Zerodha allows `http://127.0.0.1` and `http://localhost` URLs for
      development.
    - For the **CLI**, use a local HTTP redirect URL (for automatic token
      capture), for example **`http://127.0.0.1:5000/callback`**.
      You can override it with `KITE_REDIRECT_URL` in `.env`.
      If auto-capture is unavailable, CLI falls back to manual paste.

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
KITE_DASHBOARD_NAME=Raghava's Portfolio
# Optional: live index quotes under the dashboard title (NSE). Default: NIFTY50,BANKNIFTY,NIFTYIT,NIFTYFINSERVICE,NIFTYMET
# KITE_DASHBOARD_INDICES=NIFTY50,BANKNIFTY,NIFTYIT,NIFTYFINSERVICE,NIFTYMET
DASHBOARD_SNAPSHOT_SECONDS=120
# optional: skip dashboard cache warmup thread (1/true/yes); see "## Cache warmup".
# MOMENTUM_SKIP_CACHE_WARMUP=1
# optional for CLI auto request_token capture
KITE_REDIRECT_URL=http://127.0.0.1:5000/callback
```

Optional for the web dashboard: `SESSION_SECRET` (stable cookie signing across
restarts). If omitted, the app prefers the OS keychain; see `.env.example`.

`.env` is gitignored, so it will not be committed.

## Run — Web dashboard (recommended)

Best for day-to-day use if you want a visual portfolio view with live updates.
You get summary cards, tabbed sections, and automatic refresh behavior in one
local page.

```powershell
python -m app.server
```

`python -m app.server` sets a **5 second** graceful shutdown timeout so Ctrl+C
does not wait indefinitely on open WebSocket connections. It also **opens your
default browser** to the dashboard URL after about one second (only when using
``python -m app.server``). If you start Uvicorn yourself
(``uvicorn app.server:app --host 127.0.0.1 --port 5000``), open the URL manually
and pass ``--timeout-graceful-shutdown 5`` (or similar) for the same shutdown
behaviour.

This starts a FastAPI server on http://127.0.0.1:5000/. If the browser did not
open, visit that URL, click **Login with Zerodha**, complete the Zerodha
sign-in, and you'll be redirected to the dashboard.

The dashboard is a single page: **summary cards** at the top (total invested,
current value, overall P&L, open positions P&L), then five tabs:

1. **Equity Holdings** — table of demat stocks with totals.
2. **Mutual Funds** — MF folio table with NAVs/totals plus an aggregated
   underlying holdings breakdown (instrument, sector, overall weight).
3. **Equity Positions** — open NSE / BSE positions.
4. **F&O Positions** — open NFO / BFO / CDS / BCD / MCX positions.
5. **Cash Balance** — available cash, live balance, utilised.

The page automatically re-fetches a full HTML snapshot at the interval set by
``DASHBOARD_SNAPSHOT_SECONDS`` (mutual funds, cash, margins, and structural
changes); live LTP and row-level P&L updates arrive over WebSocket between
those snapshots. Click **Logout** to clear the browser session **and** delete
the on-disk token cache so both dashboard and CLI require a fresh Zerodha login.

## Sector data source and cache

The dashboard's **Sector** column for equities (holdings, equity positions, and
watch list) follows `app.portfolio_model.resolve_equity_sector` in this order:

1. **ETF override**: if the symbol or instrument/company name contains `ETF`,
   sector is forced to `ETF`.
2. **yfinance disk cache** (`.cache/model_cache.json`, `yfinance` section): prefer
   cached `sector`, else cached `industry` for the same lookup keys.
3. **Kite instruments `sector`** (instrument token, then exchange+symbol pair).
4. **NSE CSV `Industry`** by symbol (also used when BSE symbol matches NSE).
5. **ISIN → Industry** from merged NSE lists (including cross-exchange ISIN hints).

Notes:
- On cache miss, a **background yfinance refresh** may be queued (non-blocking
  for the request path); refreshed values are written back to the model cache.
- `Industry` may appear as fallback text if a true sector is unavailable.
- ETFs are intentionally mapped to `ETF` because exchange/API sector metadata is
  commonly missing or inconsistent for ETFs.
- yfinance map refresh runs in the background when the effective IST cache day
  rolls over (09:00 Asia/Kolkata), not on a separate multi-week timer.
- The dashboard also warms caches in the background on startup and after successful login
  (`/callback`) via **`run_startup_cache_warmup`** (`warm_reference_snapshot` +
  **`warm_mfdata_holdings_cache`**). **`reference_data`** memory/disk TTL is driven by **the next
  09:00 IST cutoff** (`model_cache_store.next_cutoff_epoch_ist`), not a separate seconds-based env knob.

## Dashboard timing logs

Per-request dashboard timing is written to:

- `.cache/dashboard_timing.log`

Each line includes total duration and cumulative stage marks (for example:
`kite_data_fetch_parallel`, `instrument_and_reference_lookups`,
`live_price_stream_bootstrap`) to help isolate bottlenecks.

### Cache warmup

After **`app/server.py`** starts (and again after **`/callback`** when you log in),
**`run_startup_cache_warmup()`** (**`cache_orchestrator`**) runs **`run_startup_cache_warmup_sync`**
on a daemon thread: validate cached access token → **`warm_reference_snapshot`**
(with **`force_refresh`** when a Kite client is available) → **`warm_mfdata_holdings_cache`**.
Set **`MOMENTUM_SKIP_CACHE_WARMUP=1`** to skip this entire routine.

There is **no offline cache-build CLI**; **`yfinance`** fills from lookups and the
provider’s IST-day background refresh.

See [`.env.example`](.env.example) for **`MOMENTUM_SKIP_CACHE_WARMUP`**.

## Run — CLI

Best for quick terminal checks, scripting, or remote-shell workflows. It prints
the same core portfolio sections as readable console output after login.

```powershell
python -m app.cli_client
```

Show only selected sections:

```powershell
python -m app.cli_client --sections profile equity mf summary
```

Hide selected sections:

```powershell
python -m app.cli_client --exclude-sections watchlist cash
```

Skip MF underlying enrichment (faster run, no `mfdata.in` aggregation call):

```powershell
python -m app.cli_client --no-mf-underlyings
```

List all available options (including section keys):

```powershell
python -m app.cli_client --help
```

On the first run (and once per day after the access token expires), CLI
opens the login URL in your default browser and tries to capture
`request_token` automatically from the local callback endpoint configured
by `KITE_REDIRECT_URL` (default: `http://127.0.0.1:5000/callback`).

You will see something like:

```
Kite Connect login required.
1) Open this URL in your browser and log in to Zerodha:
   https://kite.trade/connect/login?api_key=...&v=3
   (Opened automatically in your default browser.)

Redirect URL for CLI auto-capture: http://127.0.0.1:5000/callback
Attempting automatic request_token capture...

# if auto-capture fails, CLI falls back to:
Automatic capture unavailable. Falling back to manual entry.
2) After login Zerodha redirects to your app's Redirect URL.
   Copy the value of the `request_token` query parameter from
   that redirected URL (it looks like ?request_token=XXXX&...).

Paste request_token here:
```

After authentication succeeds, all sections are printed:

1. User Profile
2. Equity Holdings
3. Mutual Fund Holdings
4. MF Underlying Breakdown (via `mfdata.in`, unless `--no-mf-underlyings` is used)
5. Open Positions (Equity and F&O)
6. Equity Cash Balance
7. Watch List (Nifty 50 snapshot)
8. Overall Portfolio Summary

Section keys accepted by `--sections` / `--exclude-sections`:
`profile`, `equity`, `mf`, `positions`, `cash`, `watchlist`, `summary`.

## Project layout

```
.
├── .env                  # your Kite API key + secret (gitignored)
├── .env.example          # template for the above
├── .gitignore
├── README.md
├── requirements.txt
├── .access_token.json    # cached daily access token (gitignored, auto-created)
├── .cache/               # local runtime caches (gitignored)
│   ├── dashboard_timing.log
│   └── model_cache.json  # yfinance, reference_data, mfdata, marketsmith, …
├── scripts/
│   └── line_classification.py  # ancillary script (not used by dashboard warmup)
├── app/
│   ├── __init__.py       # package docstring + entry point index
│   ├── auth.py           # Kite login flow + token caching (shared by CLI + web)
│   ├── live_prices.py    # KiteTicker websocket manager for live LTP snapshots
│   ├── cli_client.py     # CLI: HTTP client to local server
│   ├── main.py           # shim → cli_client
│   ├── server.py         # FastAPI app (dashboard + /api/v1)
│   ├── portfolio_model.py # model + sector resolution; facade over cache providers
│   ├── reference_snapshot.py   # ReferenceSnapshot + warm_reference_snapshot + REFERENCE_PROVIDER_WARMUPS
│   ├── reference_context.py    # WarmupContext for provider warmup
│   ├── reference_notifications.py  # debounced cache refresh signaling
│   ├── events.py         # dashboard cache refresh hooks
│   ├── web.py            # shim → server (ASGI app)
│   ├── cache/
│   │   ├── REFERENCE_PROVIDERS.md   # contributor contract for *_provider modules
│   │   ├── model_cache_store.py      # single-file JSON sections + IST cache day
│   │   ├── reference_cache_internal.py  # reference_data coordination + locks
│   │   ├── reference_provider.py    # Protocol typing for warmup (optional)
│   │   ├── text_normalize.py        # shared symbol/ISIN/name normalization
│   │   ├── kite_provider.py
│   │   ├── nse_provider.py
│   │   ├── yfinance_provider.py
│   │   ├── mfdata_provider.py
│   │   └── marketsmith_provider.py
│   └── services/
│       ├── cache_warmup.py       # synchronous startup warmup steps
│       └── cache_orchestrator.py # background thread + MOMENTUM_SKIP_CACHE_WARMUP
└── templates/
    ├── base.html         # shared layout + CSS
    ├── index.html        # login landing page
    └── dashboard.html    # tabbed dashboard
```

## Data Sources

This app combines several external sources:

- **Kite Connect** for account data and live prices.
- **NSE India archives** for index constituents and `Industry`/`ISIN` reference data.
- **yfinance** for equity metadata enrichment (`industry`/`sector`).
- **mfdata.in** for mutual-fund underlying holdings aggregation.
- **MarketSmith India** (William O'Neil India) for the published **current market
  regime** (via their public `getMarketHistory.json` gateway, same data as
  [Market Condition History](https://marketsmithindia.com/mstool/marketconditionhistory.jsp)).
  The dashboard does not send your Kite credentials to MarketSmith; only the
  optional `MARKETSMITH_MS_AUTH` query override in `.env` is ever sent if you set it.

### Zerodha Kite Connect (account and live prices)

Kite Connect is the primary source for account data and live market prices.

External documentation:

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
| `app/auth.py` / `app/server.py:/login` | `KiteConnect.login_url()`                    | (URL builder, no HTTP call)    | <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.login_url>                                                                              |
| `app/auth.py` / `app/server.py:/callback` | `KiteConnect.generate_session()`          | `POST /session/token`          | <https://kite.trade/docs/connect/v3/user/#login-flow> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.generate_session>               |
| `app/auth.py`                       | `KiteConnect.set_access_token()`             | (in-memory)                    | <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.set_access_token>                                                                       |
| `app/auth.py` (validation)          | `KiteConnect.profile()`                      | `GET /user/profile`            | <https://kite.trade/docs/connect/v3/user/#user-profile> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.profile>                      |
| `app/auth.py` (error)               | `kiteconnect.exceptions.TokenException`      | (raised on 403 token errors)   | <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.exceptions.TokenException>                                                                          |
| `app/server.py` (`/dashboard`, `/api/v1/portfolio/snapshot`, MF JSON routes) | `KiteConnect.holdings()`                  | `GET /portfolio/holdings`      | <https://kite.trade/docs/connect/v3/portfolio/#holdings> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.holdings>                    |
| `app/server.py` (same) | `KiteConnect.mf_holdings()`            | `GET /mf/holdings`             | <https://kite.trade/docs/connect/v3/mutual-funds/#mutual-fund-holdings> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.mf_holdings>  |
| `app/server.py` (same) | `KiteConnect.positions()`                | `GET /portfolio/positions`     | <https://kite.trade/docs/connect/v3/portfolio/#positions> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.positions>                  |
| `app/server.py` (same) | `KiteConnect.margins("equity")`       | `GET /user/margins/equity`     | <https://kite.trade/docs/connect/v3/user/#funds-and-margins> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.margins>                 |
| `app/live_prices.py` · `app/server.py:/dashboard` | `KiteTicker.subscribe()` / `set_mode("ltp")` | `wss://ws.kite.trade` | <https://kite.trade/docs/connect/v3/websocket/> · <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteTicker> |
| `app/server.py` WebSocket `/ws/live-prices` | (ticks from existing `KiteTicker` stream) | Browser ← JSON LTP deltas | Same WebSocket docs as above (feed is shared with server-side LTP cache). |
| `app/server.py:/favicon.ico` | (no Kite call) | `GET /favicon.ico` → **204** | Browsers request this automatically; empty response avoids 404 noise. |
| MF section (error)                  | `kiteconnect.exceptions.PermissionException` | (raised on 403 when MF API not enabled) | <https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.exceptions.PermissionException>                                                            |

### Login flow used by `app/server.py`

```mermaid
sequenceDiagram
    participant U as User browser
    participant W as MomentumStrategy web
    participant Z as Zerodha kite
    U->>W: Open root page
    W-->>U: Show login page
    U->>W: GET /login
    W->>U: Redirect to Kite login URL
    U->>Z: Sign in and approve access
    Z->>U: Redirect to callback with request token
    U->>W: Call callback endpoint
    W->>Z: Exchange request token for access token
    Z->>W: Return access token
    W->>W: Save cached token and mark session authenticated
    W->>U: Redirect to dashboard
    U->>W: GET /dashboard
    W->>Z: Fetch holdings MF positions margins
    W->>Z: Subscribe to live price stream
    Z->>W: Return snapshot data
    Z-->>W: Stream live ticks
    W-->>U: Render dashboard with live updates
```

### CLI flow (`app/cli_client.py`)

The CLI is an **HTTP client** to the local server. Log in once via the browser
dashboard so the access token is cached; the CLI then sends
`Authorization: Bearer` and prints tables from JSON.

```mermaid
sequenceDiagram
    participant T as Terminal_cli_client
    participant S as app_server
    participant K as Kite_API
    T->>S: GET /api/v1/portfolio/snapshot Bearer token
    S->>K: REST portfolio and quotes
    K->>S: Data
    S->>T: JSON snapshot
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

### yfinance (equity metadata enrichment)

The app uses `yfinance` only for equity metadata enrichment (`industry` and
`sector`) and does **not** use it for order/trade operations.

- **yfinance documentation** — <https://ranaroussi.github.io/yfinance/>
- **`Ticker` API reference** — <https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.html>
- **`Ticker.info` reference** — <https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.info.html>
- **PyPI package** — <https://pypi.org/project/yfinance/>

Used in:

- `app/cache/yfinance_provider.py` (called from `app/portfolio_model.resolve_equity_sector`)
  — `yf.Ticker(...).info` for cache-backed industry and sector lookup.

### NSE India (constituents and industry reference data)

The app fetches NSE reference CSVs from `nsearchives.nseindia.com` to power:

- Nifty 50 watch-list symbol universe (`Symbol` column)
- Symbol/ISIN-to-industry fallback maps used in sector resolution

Method used in code:

- HTTP GET with Python `urllib.request.Request` + browser-like headers
  (`User-Agent`, `Accept`, `Referer`) in `app/cache/nse_provider.py`
- Parse CSV using `csv.DictReader`
- Merge selected files (for example Nifty 50/100/200/500 and mid/small-cap lists)
  into in-memory maps
- Persist and reuse via `.cache/model_cache.json` (`reference_data` section); in-memory
  expiry uses the next **09:00 IST** boundary (see **`model_cache_store.next_cutoff_epoch_ist`**).

### mfdata.in (MF underlying aggregation)

`mfdata.in` is used in the dashboard's Mutual Funds tab and in the CLI report
to enrich folio-level MF holdings with underlying equity composition and sector
weights.

- **Website** — <https://mfdata.in/>
- **API base** — `https://mfdata.in`
- **Fund/scheme search** — `GET /api/v1/search?q=<fund_name>`
- **Family holdings** — `GET /api/v1/families/{family_id}/holdings`
- **Fields used by this app** — `equity_holdings` entries with
  `stock_name`, `sector`, and `weight_pct` to compute overall underlying
  weights across all aggregated funds.
- **Coverage behavior** — schemes/families without holdings coverage are
  skipped from aggregation and shown as "Not aggregated" in the dashboard.
- **Local metadata cache** — mfdata search + family-holdings responses are
  cached to `.cache/model_cache.json` (`mfdata` section, shared by CLI and dashboard).
  Live market quotes/ticks are not persisted there.

### MarketSmith India (market regime / dashboard banner)

- **Tool page** —
  <https://marketsmithindia.com/mstool/marketconditionhistory.jsp>
- **HTTP** — `GET https://marketsmithindia.com/gateway/simple-api/ms-india/mshkSubscription/getMarketHistory.json?ms-auth=<token>`
- **Used in code** — `app.cache.marketsmith_provider.get_marketsmith_market_condition()`
  returns the first ``marketHistory`` row (current regime, Nifty 50 move in
  regime, etc.), then `app/server.py:/dashboard` passes it into the template and
  into ``dashboard-bootstrap`` as ``marketCondition`` (camelCase) so browser
  code can read it as soon as `readBootstrap()` runs.

**Caching (aligned with other “day keyed” flows):** like MF holdings and MF
underlying payloads in `app/server.py`, the regime snapshot is keyed by **business
day (IST, rolling at 09:00)**. Warm responses are kept in memory for the process
lifetime; `.cache/model_cache.json` (`marketsmith` section) stores the same payload
so a **restart on the same day** does not refetch. Errors from the gateway are cached for that day as well so
a broken response does not retry on every `/dashboard` refresh. Optional
``.env``: ``MARKETSMITH_MS_AUTH`` only (see [.env.example](.env.example)).

| Where used | Call | Endpoint / artifact |
| ---------- | ---- | ------------------- |
| `app/cache/marketsmith_provider.py` | urllib `GET` | `…/getMarketHistory.json` |
| `app/server.py:/dashboard` | `get_marketsmith_market_condition()` | Banner HTML + bootstrap JSON |

## Notes

- This app only reads. It calls `kite.holdings()`, `kite.mf_holdings()`,
  `kite.positions()`, `kite.margins()`, `kite.profile()`, and (for web)
  subscribes to live quote ticks via `KiteTicker`. It does not place,
  modify, or cancel any orders.
- The web dashboard binds to `127.0.0.1` only, so it is **not**
  reachable from other machines on the network. To expose it elsewhere,
  change the `host` argument in `app.server.main`.
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
  with browser-like headers to fetch it server-side). See `## Data Sources`
  for the source summary used by this app.
