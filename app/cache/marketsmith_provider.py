"""MarketSmith gateway snapshot — delegates to :mod:`app.portfolio_model` until split."""

from app.portfolio_model import get_marketsmith_market_condition

__all__ = ["get_marketsmith_market_condition"]
