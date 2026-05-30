"""
Run a parameter grid over the intraday swing backtest.
"""

from __future__ import annotations

import argparse
import dataclasses
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import json
import logging
from typing import Any

from backtest import decimal_to_str
from backtest_intraday import (
    AlpacaRestClient,
    IntradaySettings,
    fetch_bars,
    load_settings,
    run_backtest,
    to_trader_settings,
    validate_settings,
)


@dataclass(frozen=True)
class IntradayOptimizationRow:
    daily_fast: int
    daily_slow: int
    intraday_sma: int
    breakout_lookback: int
    stop_loss_pct: Decimal
    reward_risk_ratio: Decimal
    risk_fraction: Decimal
    final_equity: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    trades: int
    wins: int
    losses: int
    win_rate_pct: Decimal
    profit_factor: Decimal | None
    day_trades: int
    max_day_trades_in_5_business_days: int
    same_day_exit_rate: Decimal
    avg_holding_hours: Decimal
    median_holding_hours: Decimal
    score: Decimal


def parse_int_list(raw: str) -> list[int]:
    values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not values:
        raise ValueError("integer list is empty")
    return values


def parse_decimal_list(raw: str) -> list[Decimal]:
    values = [Decimal(part.strip()) for part in raw.split(",") if part.strip()]
    if not values:
        raise ValueError("decimal list is empty")
    return values


def load_base_settings(args: argparse.Namespace) -> IntradaySettings:
    base_args = argparse.Namespace(
        start=args.start,
        end=args.end,
        watchlist=args.watchlist,
        market_symbol=args.market_symbol,
        data_feed=args.data_feed,
        initial_equity=args.initial_equity,
        risk_fraction=None,
        reward_risk_ratio=None,
        stop_loss_pct=None,
        intraday_timeframe=args.intraday_timeframe,
        daily_fast=None,
        daily_slow=None,
        intraday_sma=None,
        breakout_lookback=None,
    )
    return load_settings(base_args)


def fetch_grid_bars(
    settings: IntradaySettings,
    client: AlpacaRestClient,
    max_daily_slow: int,
    max_intraday_sma: int,
    max_breakout_lookback: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    fetch_settings = dataclasses.replace(
        settings,
        daily_slow=max_daily_slow,
        intraday_sma=max(max_intraday_sma, max_breakout_lookback),
    )
    return fetch_bars(fetch_settings, client)


def score_result(summary: dict[str, Any], min_trades: int, max_day_trades_limit: int) -> Decimal:
    trades = int(summary["trades"])
    if trades < min_trades:
        return Decimal("-999999")

    profit_factor = summary["profit_factor"]
    pf = Decimal("10") if profit_factor is None and trades > 0 else Decimal(str(profit_factor or "0"))
    total_return = Decimal(str(summary["total_return_pct"]))
    max_drawdown = Decimal(str(summary["max_drawdown_pct"]))
    same_day_exit_rate = Decimal(str(summary["same_day_exit_rate"]))
    max_day_trades = int(summary["max_day_trades_in_5_business_days"])
    excess_day_trades = max(0, max_day_trades - max_day_trades_limit)

    return (
        pf
        + (total_return * Decimal("10"))
        - (max_drawdown * Decimal("5"))
        - (same_day_exit_rate * Decimal("1.5"))
        - (Decimal(excess_day_trades) * Decimal("0.75"))
    )


def optimize(base: IntradaySettings, args: argparse.Namespace) -> list[IntradayOptimizationRow]:
    daily_fast_values = parse_int_list(args.daily_fast_values)
    daily_slow_values = parse_int_list(args.daily_slow_values)
    intraday_sma_values = parse_int_list(args.intraday_sma_values)
    breakout_lookback_values = parse_int_list(args.breakout_lookback_values)
    stop_loss_values = parse_decimal_list(args.stop_loss_values)
    reward_risk_values = parse_decimal_list(args.reward_risk_values)
    risk_fraction_values = parse_decimal_list(args.risk_fraction_values)

    client = AlpacaRestClient(to_trader_settings(base))
    daily_bars, intraday_bars = fetch_grid_bars(
        base,
        client,
        max(daily_slow_values),
        max(intraday_sma_values),
        max(breakout_lookback_values),
    )

    rows: list[IntradayOptimizationRow] = []
    for daily_fast in daily_fast_values:
        for daily_slow in daily_slow_values:
            if daily_fast >= daily_slow:
                logging.info("SKIP invalid daily SMA combo fast=%s slow=%s", daily_fast, daily_slow)
                continue
            for intraday_sma in intraday_sma_values:
                for breakout_lookback in breakout_lookback_values:
                    for stop_loss_pct in stop_loss_values:
                        for reward_risk_ratio in reward_risk_values:
                            for risk_fraction in risk_fraction_values:
                                settings = dataclasses.replace(
                                    base,
                                    daily_fast=daily_fast,
                                    daily_slow=daily_slow,
                                    intraday_sma=intraday_sma,
                                    breakout_lookback=breakout_lookback,
                                    stop_loss_pct=stop_loss_pct,
                                    reward_risk_ratio=reward_risk_ratio,
                                    risk_fraction=risk_fraction,
                                )
                                validate_settings(settings)
                                result = run_backtest(settings, daily_bars, intraday_bars)
                                summary = result["summary"]
                                rows.append(IntradayOptimizationRow(
                                    daily_fast=daily_fast,
                                    daily_slow=daily_slow,
                                    intraday_sma=intraday_sma,
                                    breakout_lookback=breakout_lookback,
                                    stop_loss_pct=stop_loss_pct,
                                    reward_risk_ratio=reward_risk_ratio,
                                    risk_fraction=risk_fraction,
                                    final_equity=summary["final_equity"],
                                    total_return_pct=summary["total_return_pct"],
                                    max_drawdown_pct=summary["max_drawdown_pct"],
                                    trades=summary["trades"],
                                    wins=summary["wins"],
                                    losses=summary["losses"],
                                    win_rate_pct=summary["win_rate_pct"],
                                    profit_factor=summary["profit_factor"],
                                    day_trades=summary["day_trades"],
                                    max_day_trades_in_5_business_days=summary["max_day_trades_in_5_business_days"],
                                    same_day_exit_rate=summary["same_day_exit_rate"],
                                    avg_holding_hours=summary["avg_holding_hours"],
                                    median_holding_hours=summary["median_holding_hours"],
                                    score=score_result(summary, args.min_trades, args.max_day_trades),
                                ))

    return sorted(
        rows,
        key=lambda row: (
            row.score,
            row.profit_factor if row.profit_factor is not None else Decimal("999"),
            row.total_return_pct,
            -row.max_drawdown_pct,
            -row.max_day_trades_in_5_business_days,
            row.trades,
        ),
        reverse=True,
    )


def print_table(rows: list[IntradayOptimizationRow], limit: int) -> None:
    print("INTRADAY OPTIMIZATION RESULTS")
    print(
        "rank daily intraday_sma breakout stop rr risk final return max_dd trades "
        "win_rate profit_factor day_trades max_dt_5d same_day avg_hold_h score"
    )
    for rank, row in enumerate(rows[:limit], start=1):
        profit_factor = "n/a" if row.profit_factor is None else str(row.profit_factor)
        print(
            f"{rank} "
            f"{row.daily_fast}/{row.daily_slow} "
            f"{row.intraday_sma} "
            f"{row.breakout_lookback} "
            f"{row.stop_loss_pct} "
            f"{row.reward_risk_ratio} "
            f"{row.risk_fraction} "
            f"{decimal_to_str(row.final_equity)} "
            f"{decimal_to_str(row.total_return_pct * Decimal('100'))}% "
            f"{decimal_to_str(row.max_drawdown_pct * Decimal('100'))}% "
            f"{row.trades} "
            f"{decimal_to_str(row.win_rate_pct * Decimal('100'))}% "
            f"{profit_factor} "
            f"{row.day_trades} "
            f"{row.max_day_trades_in_5_business_days} "
            f"{decimal_to_str(row.same_day_exit_rate * Decimal('100'))}% "
            f"{decimal_to_str(row.avg_holding_hours)} "
            f"{row.score}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize intraday swing backtest parameters.")
    parser.add_argument("--start", help="Backtest start date, YYYY-MM-DD.")
    parser.add_argument("--end", help="Backtest end date, YYYY-MM-DD.")
    parser.add_argument("--watchlist", help="Comma-separated symbols. Defaults to WATCHLIST.")
    parser.add_argument("--market-symbol", help="Market trend filter symbol.")
    parser.add_argument("--data-feed", help="Alpaca data feed.")
    parser.add_argument("--initial-equity", help="Starting equity.")
    parser.add_argument("--intraday-timeframe", default="1Hour", help="Intraday timeframe.")
    parser.add_argument("--daily-fast-values", default="20,30", help="Comma-separated daily fast SMA values.")
    parser.add_argument("--daily-slow-values", default="100,150", help="Comma-separated daily slow SMA values.")
    parser.add_argument("--intraday-sma-values", default="10,20,30", help="Comma-separated intraday SMA values.")
    parser.add_argument("--breakout-lookback-values", default="0,3,5", help="Comma-separated breakout lookback values.")
    parser.add_argument("--stop-loss-values", default="0.02,0.03,0.05", help="Comma-separated stop-loss percentages.")
    parser.add_argument("--reward-risk-values", default="1.5,2.0,2.5,3.0", help="Comma-separated reward/risk ratios.")
    parser.add_argument("--risk-fraction-values", default="0.001", help="Comma-separated risk fractions.")
    parser.add_argument("--min-trades", type=int, default=12, help="Rows with fewer trades get a large score penalty.")
    parser.add_argument("--max-day-trades", type=int, default=3, help="Penalty starts above this 5-business-day day-trade count.")
    parser.add_argument("--top", type=int, default=20, help="Number of rows to print.")
    parser.add_argument("--json", action="store_true", help="Print JSON results.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base = load_base_settings(args)
    rows = optimize(base, args)
    if args.json:
        print(json.dumps([dataclasses.asdict(row) for row in rows], ensure_ascii=False, default=str, indent=2))
    else:
        print_table(rows, args.top)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        logging.error("FATAL %s: %s", type(exc).__name__, exc)
        raise SystemExit(1) from exc
