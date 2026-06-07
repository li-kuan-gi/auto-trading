from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.modules.setdefault("requests", types.ModuleType("requests"))

import backtest_dow_theory as dow  # noqa: E402


def bar(day: str, open_: str = "10", high: str = "10", low: str = "10", close: str = "10") -> dict:
    return {"t": day, "o": open_, "h": high, "l": low, "c": close}


def settings(**kwargs) -> dow.DowSettings:
    defaults = dict(
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        paper=True,
        watchlist=["SYM"],
        data_feed="iex",
        initial_equity=Decimal("10000"),
        risk_fraction=Decimal("0.01"),
        reward_risk_ratio=Decimal("2"),
        stop_loss_pct=Decimal("0.05"),
        allow_fractional=False,
        swing_n=2,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        log_level="WARNING",
    )
    defaults.update(kwargs)
    return dow.DowSettings(**defaults)


class TestIsSwingHigh(unittest.TestCase):
    def _bars(self, highs: list[str]) -> list[dict]:
        return [bar(f"2024-01-{i+1:02d}", high=h) for i, h in enumerate(highs)]

    def test_center_is_highest(self):
        bars = self._bars(["10", "11", "15", "12", "13"])
        self.assertTrue(dow.is_swing_high(bars, 2, 2))

    def test_not_highest_on_left(self):
        bars = self._bars(["16", "11", "15", "12", "13"])
        self.assertFalse(dow.is_swing_high(bars, 2, 2))

    def test_not_highest_on_right(self):
        bars = self._bars(["10", "11", "15", "16", "13"])
        self.assertFalse(dow.is_swing_high(bars, 2, 2))

    def test_boundary_too_close_to_start(self):
        bars = self._bars(["15", "10", "10", "10", "10"])
        self.assertFalse(dow.is_swing_high(bars, 0, 2))

    def test_boundary_too_close_to_end(self):
        bars = self._bars(["10", "10", "10", "10", "15"])
        self.assertFalse(dow.is_swing_high(bars, 4, 2))


class TestIsSwingLow(unittest.TestCase):
    def _bars(self, lows: list[str]) -> list[dict]:
        return [bar(f"2024-01-{i+1:02d}", low=l) for i, l in enumerate(lows)]

    def test_center_is_lowest(self):
        bars = self._bars(["10", "9", "5", "8", "7"])
        self.assertTrue(dow.is_swing_low(bars, 2, 2))

    def test_not_lowest_on_right(self):
        bars = self._bars(["10", "9", "5", "8", "3"])
        self.assertFalse(dow.is_swing_low(bars, 2, 2))


class TestDowSignal(unittest.TestCase):
    def _make_bars(self) -> list[dict]:
        # Pattern that produces: SH at i=2 (high=20), SL at i=4 (low=8),
        #                        SH at i=6 (high=25), SL at i=8 (low=10)
        # HH: 25 > 20 ✓, HL: 10 > 8 ✓ → bull signal at i=10 (after confirmation)
        return [
            bar("2024-01-01", high="15", low="12"),  # 0
            bar("2024-01-02", high="18", low="13"),  # 1
            bar("2024-01-03", high="20", low="14"),  # 2  SH
            bar("2024-01-04", high="17", low="11"),  # 3
            bar("2024-01-05", high="16", low="8"),   # 4  SL
            bar("2024-01-06", high="18", low="9"),   # 5
            bar("2024-01-07", high="25", low="15"),  # 6  SH
            bar("2024-01-08", high="22", low="13"),  # 7
            bar("2024-01-09", high="21", low="10"),  # 8  SL
            bar("2024-01-10", high="23", low="12"),  # 9
            bar("2024-01-11", high="24", low="13"),  # 10 — confirm i=8 as SL; signal here, entry at i=11
            bar("2024-01-12", open_="24", high="26", low="23", close="25"),  # 11 entry bar
        ]

    def test_signal_detected(self):
        bars = self._make_bars()
        shs = [2, 6]
        sls = [4, 8]
        s = settings()
        signal = dow.dow_signal_for_index(s, "SYM", bars, 10, shs, sls)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.entry_date, date(2024, 1, 12))

    def test_no_signal_when_not_hh(self):
        bars = self._make_bars()
        shs = [6, 2]  # reversed: second SH < first SH
        sls = [4, 8]
        s = settings()
        signal = dow.dow_signal_for_index(s, "SYM", bars, 10, shs, sls)
        self.assertIsNone(signal)

    def test_no_signal_when_not_hl(self):
        bars = self._make_bars()
        shs = [2, 6]
        sls = [8, 4]  # reversed: second SL < first SL
        s = settings()
        signal = dow.dow_signal_for_index(s, "SYM", bars, 10, shs, sls)
        self.assertIsNone(signal)

    def test_no_signal_fewer_than_two_swings(self):
        bars = self._make_bars()
        s = settings()
        self.assertIsNone(dow.dow_signal_for_index(s, "SYM", bars, 10, [2], [4]))
        self.assertIsNone(dow.dow_signal_for_index(s, "SYM", bars, 10, [], []))


class TestDowExit(unittest.TestCase):
    def _position(self, **kwargs) -> dow.DowPosition:
        defaults = dict(
            symbol="SYM",
            entry_date=date(2024, 1, 1),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            take_profit_price=Decimal("110"),
            qty=Decimal("10"),
            risk_amount=Decimal("50"),
            last_swing_low=Decimal("93"),
        )
        defaults.update(kwargs)
        return dow.DowPosition(**defaults)

    def test_stop_loss(self):
        pos = self._position()
        bars = [bar("2024-02-01", open_="96", high="97", low="94", close="95")]
        result = dow.dow_exit_for_bar(pos, bars, 0)
        self.assertEqual(result.reason, "stop_loss")
        self.assertEqual(result.exit_price, Decimal("95"))

    def test_take_profit(self):
        pos = self._position()
        bars = [bar("2024-02-01", open_="101", high="111", low="100", close="109")]
        result = dow.dow_exit_for_bar(pos, bars, 0)
        self.assertEqual(result.reason, "take_profit")
        self.assertEqual(result.exit_price, Decimal("110"))

    def test_stop_wins_over_take_profit(self):
        pos = self._position()
        bars = [bar("2024-02-01", open_="100", high="115", low="90", close="100")]
        result = dow.dow_exit_for_bar(pos, bars, 0)
        self.assertEqual(result.reason, "stop_loss")

    def test_trend_reversal_next_open(self):
        pos = self._position(last_swing_low=Decimal("98"))
        bars = [
            bar("2024-02-01", open_="100", high="101", low="97", close="97"),
            bar("2024-02-02", open_="96", high="98", low="95", close="96"),
        ]
        result = dow.dow_exit_for_bar(pos, bars, 0)
        self.assertEqual(result.reason, "trend_reversal")
        self.assertEqual(result.exit_date, date(2024, 2, 2))
        self.assertEqual(result.exit_price, Decimal("96"))

    def test_no_exit_within_bounds(self):
        pos = self._position()
        bars = [bar("2024-02-01", open_="100", high="105", low="96", close="103")]
        self.assertIsNone(dow.dow_exit_for_bar(pos, bars, 0))


class TestRunBacktest(unittest.TestCase):
    def _build_bars(self) -> list[dict]:
        # 20 bars: trending up to trigger HH+HL signal, then take profit hit
        prices = [
            ("10", "10.5", "9.8"),   # 0  open/high/low
            ("10.3", "10.8", "10.1"),  # 1
            ("10.5", "11.2", "10.3"),  # 2  SH candidate
            ("10.8", "11.0", "10.2"),  # 3
            ("10.4", "10.7", "9.9"),   # 4  SL candidate
            ("10.6", "11.5", "10.5"),  # 5
            ("11.2", "12.0", "11.0"),  # 6  SH candidate (HH)
            ("11.5", "11.8", "11.2"),  # 7
            ("11.0", "11.3", "10.8"),  # 8  SL candidate (HL)
            ("11.2", "11.6", "11.1"),  # 9
            ("11.4", "11.8", "11.2"),  # 10 confirm i=8 as SL → signal, entry at 11
            ("11.6", "20.0", "11.5"),  # 11 entry open=11.6; high=20 hits take profit
            ("12.0", "12.5", "11.8"),  # 12
        ]
        rows = []
        for i, (o, h, l) in enumerate(prices):
            d_str = f"2024-01-{i+1:02d}"
            rows.append(bar(d_str, open_=o, high=h, low=l, close=l))
        return rows

    def test_full_backtest_produces_trade(self):
        bars = self._build_bars()
        s = settings(
            start=date(2024, 1, 1),
            end=date(2024, 1, 13),
            stop_loss_pct=Decimal("0.10"),
            reward_risk_ratio=Decimal("2"),
        )
        result = dow.run_backtest_on_bars(s, {"SYM": bars})
        self.assertGreater(result["summary"]["trades"], 0)

    def test_empty_watchlist_produces_no_trades(self):
        s = settings(start=date(2024, 1, 1), end=date(2024, 1, 13))
        result = dow.run_backtest_on_bars(s, {"SYM": []})
        self.assertEqual(result["summary"]["trades"], 0)


if __name__ == "__main__":
    unittest.main()
