# MomentumStrategy

A Python application that provides a local web dashboard and CLI to visualize your Zerodha account state using the official [Kite Connect](https://kite.trade) API.

## Features

- **Consolidated Portfolio**: View equity holdings, mutual funds (with P&L), open positions (Equity & F&O), and cash balances in one place.
- **Live Updates**: Equity and F&O prices are updated live via Kite WebSocket (`KiteTicker`), pushed to the browser on `WS /ws/live-prices`. Your Kite app must have **WebSocket market data** enabled in the [developer console](https://developers.kite.trade) for live quotes to stream.
- **Live Stock Chart**: Full-page chart at `GET /dashboard/stock-chart` — candlesticks from Kite historical data ([TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/)), with multi-interval views, optional indicators, trendlines and price levels. Live ticks and depth use `WS /ws/chart-ticks` (MODE_FULL). Annotations persist under the project `.cache/` directory (see `GET`/`POST`/`DELETE /dashboard/chart-annotations`).
- **Rich Insights**:
  - Resolves equity sectors using NSE archives and yfinance.
  - Enriches Mutual Funds with underlying stock and sector composition via `mfdata.in`.
  - Displays the current India market regime via MarketSmith India.
- **Dual Interfaces**: Choose between a visual, tabbed local web dashboard or a fast CLI report (CLI talks to the same running server over HTTP).
- **JSON API**: `GET /api/v1/portfolio/snapshot` returns the same dashboard payload as JSON. Send `Authorization: Bearer <kite_access_token>` (the Kite access token cached after browser login, same file the CLI reads). `GET /api/v1/health` returns `{"status":"ok"}`.

## Prerequisites

1. **Python 3.10+**
2. **Kite Connect app**: API key and secret from the [developer console](https://developers.kite.trade). Set the app **Redirect URL** to match what you use locally (default below).
3. **Node.js 18+** (optional): only needed if you change chart code under `static/js/src/stock_chart/` and rebuild with Vite. Running the dashboard uses the committed bundle in `static/js/`; Node is not required for `python -m app.server` alone.

## Installation

**Python (required)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

**Front-end toolchain (optional, chart development)**

From the repository root, after Python setup:

```powershell
npm install
```

Use `npm ci` instead if you want a clean install strictly from `package-lock.json` (for example in automation).

## Configuration

1. Copy `.env.example` to `.env`.
2. Set **`KITE_API_KEY`** and **`KITE_API_SECRET`** (required).
3. In the Kite developer console, set the app **Redirect URL** to the same value as **`KITE_REDIRECT_URL`** in `.env` (default: `http://127.0.0.1:5000/callback`). The dashboard listens on `127.0.0.1:5000` by default; the redirect URL must match or login will fail.
4. Optional variables (snapshot interval, header indices, session secret, cache warmup, MarketSmith, etc.) are documented inline in `.env.example`.

After a successful browser login, the Kite **access token** is stored locally (see `.access_token.json` in `.gitignore`) and reused until it expires (typically until the next trading session per Kite’s rules).

## Running the App

### Web dashboard (recommended)

```powershell
python -m app.server
```

The server binds to `127.0.0.1:5000` and opens `http://127.0.0.1:5000/` in your default browser after a short delay. Use **Login with Zerodha** to complete OAuth; the access token is cached for reuse by the server and CLI.

The ASGI app is `app.server:app`. Alternatives:

```powershell
python -m app.web
```

```powershell
uvicorn app.server:app --host 127.0.0.1 --port 5000
```

### CLI

The CLI is an **HTTP client**: it calls `GET /api/v1/portfolio/snapshot` on the running app. You must:

1. Keep **`python -m app.server`** (or uvicorn) running on the same machine (default base URL `http://127.0.0.1:5000`).
2. Have completed at least one **browser login** so a valid access token exists (see configuration above).

```powershell
python -m app.cli_client
```

Override the server base URL if needed:

```powershell
$env:MOMENTUM_SERVER_URL = "http://127.0.0.1:5000"
python -m app.cli_client
```

Use `--help` for section filters (for example `--sections profile equity`).

### Rebuilding the stock chart bundle

The shipped `static/js/stock_chart.js` is enough for production use. After editing modules under `static/js/src/stock_chart/`:

```powershell
npm run build:chart
```

For continuous rebuilds while developing:

```powershell
npm run watch:chart
```

Keep `static/js/lightweight-charts.standalone.production.js` in place; the template loads it before `stock_chart.js`.

## Architecture & data sources

```text
External APIs (Kite REST/WS, NSE, yfinance, mfdata.in, MarketSmith)
          |
  app/infrastructure/cache/*_provider.py (Per-source caching)
          |
  app/domain/portfolio_model.py (Normalization, sector resolution, aggregates)
          |
  app/application/dashboard_view_model.py (Dashboard + stock history context)
          |
  app/server.py (FastAPI app, static files, route wiring)
          + app/presentation/http/routes/*.py (HTML, JSON, WebSockets)
          |
  app/cli_client.py (CLI — HTTP to local server)
```

**Data sources used**

- **Zerodha Kite Connect**: Core account data, holdings, positions, and live WebSocket prices.
- **NSE India**: Reference data for Nifty 50 constituents and base industry mapping.
- **yfinance**: Fallback equity metadata enrichment (sectors and industries).
- **mfdata.in**: Mutual fund underlying asset aggregation.
- **MarketSmith India**: Daily market regime status.

## Important notes

- **Read-only**: The app only reads data. It **does not** place, modify, or cancel orders.
- **Local network**: The dashboard listens on `127.0.0.1` by default.
- **Mutual funds module**: Your Kite Connect app must have the Mutual Funds module enabled to view MF holdings. If disabled, the app skips that section.
- **Chart annotations**: Saved annotations are written to `.cache/chart_annotations.json` (single file; not coordinated across multiple browser tabs or processes).
