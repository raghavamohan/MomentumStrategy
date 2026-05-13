"""Jinja2 environment, filters, and small JSON helpers for chart/history routes."""

from __future__ import annotations

import json
from urllib.parse import quote

from markupsafe import Markup
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.presentation.http.server_config import TEMPLATES_DIR

KITE_STOCK_HISTORY_HELP_URLS: tuple[str, ...] = (
    "https://kite.trade/docs/connect/v3/market-quotes/#historical-data-candles",
    "https://kite.trade/docs/pykiteconnect/v4/#kiteconnect.KiteConnect.historical_data",
    "https://developers.kite.trade/",
)


def stock_history_json_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {
            "error": message,
            "kite_help_urls": list(KITE_STOCK_HISTORY_HELP_URLS),
        },
        status_code=status_code,
    )


def _url_quote_filter(value: object) -> str:
    """Percent-encode values used in query strings for chart links."""
    return quote(str(value if value is not None else ""), safe="")


def _tojson_filter(value: object) -> Markup:
    """Embed JSON in HTML/JS (escaped)."""
    return Markup(json.dumps(value))


def _format_inr(value: float | int | None) -> str:
    """Format a number with comma separators and 2 decimals (no currency sign)."""
    if value is None:
        return "-"
    return f"{float(value):,.2f}"


def _format_units(value: float | int | None) -> str:
    """Format a quantity / NAV with 4 decimals (used for MF rows)."""
    if value is None:
        return "-"
    return f"{float(value):,.4f}"


def _format_pct(value: float | int | None) -> str:
    """Format a percentage with 2 decimals."""
    if value is None:
        return "-"
    return f"{float(value):,.2f}%"


def _sign_class(value: float | int | None) -> str:
    """Return ``positive`` / ``negative`` / ``neutral`` for CSS colouring."""
    if value is None:
        return "neutral"
    v = float(value)
    if v > 0:
        return "positive"
    if v < 0:
        return "negative"
    return "neutral"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

templates.env.filters["inr"] = _format_inr
templates.env.filters["units"] = _format_units
templates.env.filters["pct"] = _format_pct
templates.env.filters["sign_class"] = _sign_class
templates.env.filters["urlquote"] = _url_quote_filter
templates.env.filters["tojson"] = _tojson_filter
