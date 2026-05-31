from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.modules.setdefault("requests", types.ModuleType("requests"))

import backtest  # noqa: E402
import backtest_intraday  # noqa: E402


def daily_bar(day: str, open_: str = "10", high: str = "10", low: str = "10", close: str = "10") -> dict[str, str]:
    return {"t": day, "o": open_, "h": high, "l": low, "c": close}


def intraday_bar(ts: str, open_: str = "10", high: str = "10", low: str = "10", close: str = "10") -> dict[str, str]:
    return {"t": ts, "o": open_, "h": high, "l": low, "c": close}


def daily_settings() -> backtest.BacktestSettings:
    return backtest.BacktestSettings(
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        paper=True,
        watchlist=["AAA", "BBB"],
        data_feed="iex",
        initial_equity=Decimal("10000"),
        risk_fraction=Decimal("0.001"),
        reward_risk_ratio=Decimal("3"),
        stop_loss_pct=Decimal("0.03"),
        allow_fractional=False,
        sma_fast=2,
        sma_slow=3,
        start=date(2025, 1, 1),
        end=date(2025, 1, 4),
        log_level="INFO",
    )


def intraday_settings() -> backtest_intraday.IntradaySettings:
    return backtest_intraday.IntradaySettings(
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        paper=True,
        watchlist=["AAA", "BBB"],
        market_symbol="QQQ",
        data_feed="iex",
        initial_equity=Decimal("10000"),
        risk_fraction=Decimal("0.001"),
        reward_risk_ratio=Decimal("3"),
        stop_loss_pct=Decimal("0.03"),
        allow_fractional=False,
        intraday_timeframe="2Hour",
        daily_fast=2,
        daily_slow=3,
        intraday_sma=2,
        breakout_lookback=0,
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        end_mode="window",
        log_level="INFO",
    )


class DeferredExitTests(unittest.TestCase):
    def test_daily_deferred_exit_blocks_other_symbol_entries_until_exit_date(self) -> None:
        bars_by_symbol = {
            "AAA": [
                daily_bar("2025-01-01", "10"),
                daily_bar("2025-01-02", "11"),
                daily_bar("2025-01-04", "9"),
            ],
            "BBB": [
                daily_bar("2025-01-03", "20"),
                daily_bar("2025-01-04", "21"),
            ],
        }

        def fake_signal(settings, symbol, bars, closes, index):
            day = backtest.bar_date(bars[index])
            if symbol == "AAA" and day == date(2025, 1, 1):
                return backtest.BacktestSignal(
                    symbol="AAA",
                    signal_date=day,
                    entry_date=date(2025, 1, 2),
                    entry_price=Decimal("11"),
                    stop_price=Decimal("10"),
                    take_profit_price=Decimal("14"),
                    selection_score=Decimal("1"),
                    details={},
                )
            if symbol == "BBB" and day == date(2025, 1, 3):
                return backtest.BacktestSignal(
                    symbol="BBB",
                    signal_date=day,
                    entry_date=date(2025, 1, 4),
                    entry_price=Decimal("21"),
                    stop_price=Decimal("20"),
                    take_profit_price=Decimal("24"),
                    selection_score=Decimal("2"),
                    details={},
                )
            return None

        def fake_exit(settings, position, bars, closes, index):
            day = backtest.bar_date(bars[index])
            if position.symbol == "AAA" and day == date(2025, 1, 2):
                return backtest.ExitResult("sma_exit", date(2025, 1, 4), Decimal("9"))
            return None

        with (
            mock.patch("backtest.signal_for_index", side_effect=fake_signal),
            mock.patch("backtest.exit_reason_for_bar", side_effect=fake_exit),
        ):
            result = backtest.run_backtest_on_bars(daily_settings(), bars_by_symbol)

        self.assertEqual([trade["symbol"] for trade in result["trades"]], ["AAA"])
        self.assertEqual(result["trades"][0]["exit_date"], date(2025, 1, 4))

    def test_intraday_deferred_exit_blocks_other_symbol_entries_until_exit_time(self) -> None:
        bars_by_symbol = {
            "AAA": [
                intraday_bar("2025-01-01T14:30:00Z", "10"),
                intraday_bar("2025-01-01T16:30:00Z", "11"),
                intraday_bar("2025-01-03T14:30:00Z", "9"),
            ],
            "BBB": [
                intraday_bar("2025-01-02T14:30:00Z", "20"),
                intraday_bar("2025-01-03T14:30:00Z", "21"),
            ],
        }
        trend_cache = {
            "QQQ": {
                date(2025, 1, 1): (True, Decimal("0.01")),
                date(2025, 1, 2): (True, Decimal("0.01")),
                date(2025, 1, 3): (True, Decimal("0.01")),
            },
            "AAA": {
                date(2025, 1, 1): (True, Decimal("0.01")),
                date(2025, 1, 2): (True, Decimal("0.01")),
                date(2025, 1, 3): (True, Decimal("0.01")),
            },
            "BBB": {
                date(2025, 1, 1): (True, Decimal("0.01")),
                date(2025, 1, 2): (True, Decimal("0.01")),
                date(2025, 1, 3): (True, Decimal("0.01")),
            },
        }

        def fake_signal(settings, symbol, bars, closes, highs, index, trend_strength):
            current_time = backtest_intraday.parse_bar_time(bars[index])
            if symbol == "AAA" and current_time == backtest_intraday.parse_bar_time(bars_by_symbol["AAA"][0]):
                return backtest_intraday.IntradaySignal(
                    symbol="AAA",
                    signal_time=current_time,
                    entry_time=backtest_intraday.parse_bar_time(bars_by_symbol["AAA"][1]),
                    entry_price=Decimal("11"),
                    stop_price=Decimal("10"),
                    take_profit_price=Decimal("14"),
                    selection_score=Decimal("1"),
                )
            if symbol == "BBB" and current_time == backtest_intraday.parse_bar_time(bars_by_symbol["BBB"][0]):
                return backtest_intraday.IntradaySignal(
                    symbol="BBB",
                    signal_time=current_time,
                    entry_time=backtest_intraday.parse_bar_time(bars_by_symbol["BBB"][1]),
                    entry_price=Decimal("21"),
                    stop_price=Decimal("20"),
                    take_profit_price=Decimal("24"),
                    selection_score=Decimal("2"),
                )
            return None

        def fake_exit(settings, position, bar, bars, closes, index):
            current_time = backtest_intraday.parse_bar_time(bar)
            if position.symbol == "AAA" and current_time == backtest_intraday.parse_bar_time(bars_by_symbol["AAA"][1]):
                return backtest_intraday.IntradayExit(
                    "intraday_sma_exit",
                    backtest_intraday.parse_bar_time(bars_by_symbol["AAA"][2]),
                    Decimal("9"),
                )
            return None

        with (
            mock.patch("backtest_intraday.intraday_signal_for_index", side_effect=fake_signal),
            mock.patch("backtest_intraday.exit_for_bar", side_effect=fake_exit),
        ):
            result = backtest_intraday.run_backtest(
                intraday_settings(),
                {"QQQ": [], "AAA": [], "BBB": []},
                bars_by_symbol,
                daily_trend_cache=trend_cache,
            )

        self.assertEqual([trade["symbol"] for trade in result["trades"]], ["AAA"])
        self.assertEqual(result["trades"][0]["exit_time"], backtest_intraday.parse_bar_time(bars_by_symbol["AAA"][2]))


if __name__ == "__main__":
    unittest.main()
