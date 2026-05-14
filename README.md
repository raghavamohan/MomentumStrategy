# MomentumStrategy

A Python application that provides a local web dashboard and CLI to visualize your Zerodha account state using the official [Kite Connect](https://kite.trade) API.

## Features

- **Consolidated Portfolio**: View equity holdings, mutual funds (with P&L), open positions (Equity & F&O), and cash balances in one place.
- **Live Updates**: Equity and F&O prices are updated live via Kite WebSocket (`KiteTicker`), pushed to the browser on `WS /ws/live-prices`. Your Kite app must have **WebSocket market data** enabled in the [developer console](https://developers.kite.trade) for live quotes to stream.
- **Live Stock Chart**: Full-page candlesticks from Kite historical data ([TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/)), with multi-interval views, optional indicators, trendlines and price levels. Live ticks and depth use `WS /ws/chart-ticks` (MODE_FULL). Annotations persist under the project `.cache/` directory (see `GET`/`POST`/`DELETE /dashboard/chart-annotations`).
- **Rich Insights**: 
  - Resolves equity sectors using NSE archives and yfinance.
  - Enriches Mutual Funds with underlying stock and sector composition via `mfdata.in`.
  - Displays the current India market regime via MarketSmith India.
- **Dual Interfaces**: Choose between a visual, tabbed local web dashboard or a fast CLI report.
- **JSON API**: `GET /api/v1/portfolio/snapshot` returns the same dashboard payload as JSON (Bearer token auth for scripts); `GET /api/v1/health` for a simple probe.

## Prerequisites & Setup

1. **Python 3.10+**
2. **Kite Connect App**: Get an API Key and Secret from the [developer console](https://developers.kite.trade). Set your Redirect URL to `http://127.0.0.1:5000/callback`.

**Installation**:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Configuration**:
Copy `.env.example` to `.env` and set at least `KITE_API_KEY` and `KITE_API_SECRET`. Optional variables (snapshot interval, header indices, session secret, cache warmup, etc.) are documented inline in `.env.example`.

## Running the App

### Web Dashboard (Recommended)
Provides a rich, live-updating tabbed UI.

```powershell
python -m app.server
```
The server binds to `127.0.0.1:5000` and automatically opens your default browser. Click **Login with Zerodha** to authenticate. The session and access token are cached locally for the trading day.

Same ASGI app is also importable as `app.server:app`. Equivalently you can run `python -m app.web` (shim) or, for example:

```powershell
uvicorn app.server:app --host 127.0.0.1 --port 5000
```

### CLI
Best for quick terminal checks or scripting.
```powershell
python -m app.cli_client
```
Use `--help` to see options for filtering specific sections (e.g., `--sections profile equity`).

## Architecture & Data Sources

The application uses a layered architecture, strictly separating data retrieval, domain modeling, and presentation:

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
  app/cli_client.py (CLI)
```

Front-end assets for the stock chart: templates under `templates/`; TradingView `lightweight-charts*.js` and the built bundle `static/js/stock_chart.js` under `static/js/`. Chart logic is authored as ES modules under `static/js/src/stock_chart/` and compiled with Vite (`npm install`, then `npm run build:chart`). Use `npm run watch:chart` while iterating on the chart.

**Data Sources Used**:
- **Zerodha Kite Connect**: Core account data, holdings, positions, and live WebSocket prices.
- **NSE India**: Reference data for Nifty 50 constituents and base industry mapping.
- **yfinance**: Fallback equity metadata enrichment (sectors and industries).
- **mfdata.in**: Mutual fund underlying asset aggregation.
- **MarketSmith India**: Daily market regime status.

## Important Notes

- **Read-Only**: The app only reads data. It **does not** place, modify, or cancel orders.
- **Local Network**: The dashboard runs exclusively on `localhost` (`127.0.0.1`) by default for security.
- **Mutual Funds Module**: Your Kite Connect app must have the Mutual Funds module enabled to view MF holdings. If disabled, the app will gracefully skip the section.
- **Chart annotations**: Saved annotations are written to `.cache/chart_annotations.json` (single file, not coordinated across multiple browser tabs or processes).
