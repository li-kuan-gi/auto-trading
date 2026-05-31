from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import types
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.modules.setdefault("requests", types.ModuleType("requests"))

from backtest_intraday import IntradaySettings, run_backtest  # noqa: E402


def bar(ts: str, open_: str, high: str, low: str, close: str) -> dict[str, str]:
    return {"t": ts, "o": open_, "h": high, "l": low, "c": close}


def settings(end_mode: str) -> IntradaySettings:
    return IntradaySettings(
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        paper=True,
        watchlist=["XYZ"],
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
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        end_mode=end_mode,
        log_level="INFO",
    )


def trend_cache() -> dict[str, dict[date, tuple[bool, Decimal]]]:
    return {
        "QQQ": {
            date(2025, 1, 2): (True, Decimal("0.01")),
            date(2025, 1, 3): (True, Decimal("0.01")),
        },
        "XYZ": {
            date(2025, 1, 2): (True, Decimal("0.01")),
            date(2025, 1, 3): (True, Decimal("0.01")),
        },
    }


class IntradayEndModeTests(unittest.TestCase):
    def test_window_mode_does_not_exit_on_post_end_next_bar(self) -> None:
        bars = [
            bar("2025-01-01T14:30:00Z", "10", "10", "10", "10"),
            bar("2025-01-01T16:30:00Z", "9", "9", "9", "9"),
            bar("2025-01-02T14:30:00Z", "11", "11", "11", "11"),
            bar("2025-01-03T14:30:00Z", "12", "12.1", "11.9", "9.5"),
            bar("2025-01-04T14:30:00Z", "9.4", "9.4", "9.4", "9.4"),
        ]

        result = run_backtest(
            settings("window"),
            {"QQQ": [], "XYZ": []},
            {"XYZ": bars},
            daily_trend_cache=trend_cache(),
        )

        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "end_of_backtest")
        self.assertEqual(trade["exit_time"].date(), date(2025, 1, 3))
        self.assertEqual(trade["exit_price"], Decimal("9.50"))

    def test_signal_cohort_mode_allows_post_end_natural_exit(self) -> None:
        bars = [
            bar("2025-01-01T14:30:00Z", "10", "10", "10", "10"),
            bar("2025-01-01T16:30:00Z", "9", "9", "9", "9"),
            bar("2025-01-02T14:30:00Z", "11", "11", "11", "11"),
            bar("2025-01-03T14:30:00Z", "12", "12.1", "11.9", "9.5"),
            bar("2025-01-04T14:30:00Z", "9.4", "9.4", "9.4", "9.4"),
        ]

        result = run_backtest(
            settings("signal_cohort"),
            {"QQQ": [], "XYZ": []},
            {"XYZ": bars},
            daily_trend_cache=trend_cache(),
        )

        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "intraday_sma_exit")
        self.assertEqual(trade["exit_time"].date(), date(2025, 1, 4))
        self.assertEqual(trade["exit_price"], Decimal("9.40"))

    def test_signal_cohort_mode_rejects_entries_after_end(self) -> None:
        bars = [
            bar("2025-01-01T14:30:00Z", "10", "10", "10", "10"),
            bar("2025-01-02T14:30:00Z", "9", "9", "9", "9"),
            bar("2025-01-03T14:30:00Z", "11", "11", "11", "11"),
            bar("2025-01-04T14:30:00Z", "12", "12", "12", "12"),
        ]

        result = run_backtest(
            settings("signal_cohort"),
            {"QQQ": [], "XYZ": []},
            {"XYZ": bars},
            daily_trend_cache=trend_cache(),
        )

        self.assertEqual(result["trades"], [])


if __name__ == "__main__":
    unittest.main()
