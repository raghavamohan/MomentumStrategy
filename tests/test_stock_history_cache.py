"""Tests for stock history cache helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.infrastructure.services.stock_history_cache import (
    _merge_candle_lists,
    _slice_candles_by_days,
    initial_history_days,
)

_IST = ZoneInfo("Asia/Kolkata")


def test_initial_history_days_daily_is_capped() -> None:
    assert initial_history_days("day", 3650) == 730
    assert initial_history_days("week", 3650) == 730
    assert initial_history_days("month", 3650) == 730


def test_initial_history_days_intraday_uses_full_window() -> None:
    assert initial_history_days("minute", 60) == 60
    assert initial_history_days("5minute", 100) == 100


def test_slice_candles_by_days_keeps_recent_rows() -> None:
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    old = (datetime.now(_IST) - timedelta(days=900)).strftime("%Y-%m-%d")
    candles = [
        {"date": old, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"date": today, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
    ]
    sliced = _slice_candles_by_days(candles, 730, "day")
    assert len(sliced) == 1
    assert sliced[0]["date"] == today


def test_merge_candle_lists_deduplicates_by_date() -> None:
    merged = _merge_candle_lists(
        [{"date": "2024-01-01", "close": 1}],
        [{"date": "2024-01-01", "close": 2}, {"date": "2024-01-02", "close": 3}],
    )
    assert [row["date"] for row in merged] == ["2024-01-01", "2024-01-02"]
    assert merged[0]["close"] == 2
