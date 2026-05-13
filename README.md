# MomentumStrategy

A Python application that provides a local web dashboard and CLI to visualize your Zerodha account state using the official [Kite Connect](https://kite.trade) API.

## Features

- **Consolidated Portfolio**: View equity holdings, mutual funds (with P&L), open positions (Equity & F&O), and cash balances in one place.
- **Live Updates**: Equity and F&O prices are updated live via Kite WebSocket (`KiteTicker`).
- **Rich Insights**: 
  - Resolves equity sectors using NSE archives and yfinance.
  - Enriches Mutual Funds with underlying stock and sector composition via `mfdata.in`.
  - Displays the current India market regime via MarketSmith India.
- **Dual Interfaces**: Choose between a visual, tabbed local web dashboard or a fast CLI report.

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
Copy `.env.example` to `.env` and configure your credentials:
```env
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
KITE_DASHBOARD_NAME=My Portfolio
```

## Running the App

### Web Dashboard (Recommended)
Provides a rich, live-updating tabbed UI.

```powershell
python -m app.server
```
The server binds to `127.0.0.1:5000` and automatically opens your default browser. Click **Login with Zerodha** to authenticate. The session and access token are cached locally for the trading day.

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
  app/server.py (Web) & app/cli_client.py (CLI)
```

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
