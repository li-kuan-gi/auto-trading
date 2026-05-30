"""
Run a parameter grid over the sma_trend backtest.

This tool fetches Alpaca daily bars once, then reuses them for every parameter
combination so GitHub Actions runs stay fast and API usage stays bounded.
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

from backtest import (
    AlpacaRestClient,
    BacktestSettings,
    bar_date,
    decimal_to_str,
    load_backtest_settings,
    run_backtest_on_bars,
    to_trader_settings,
    validate_backtest_settings,
)


@dataclass(frozen=True)
class OptimizationRow:
    sma_fast: int
    sma_slow: int
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


def load_base_settings(args: argparse.Namespace) -> BacktestSettings:
    base_args = argparse.Namespace(
        start=args.start,
        end=args.end,
        watchlist=args.watchlist,
        data_feed=args.data_feed,
        initial_equity=args.initial_equity,
        risk_fraction=None,
        reward_risk_ratio=None,
        stop_loss_pct=None,
        sma_fast=None,
        sma_slow=None,
    )
    return load_backtest_settings(base_args)


def fetch_grid_bars(settings: BacktestSettings, client: AlpacaRestClient, max_sma_slow: int) -> dict[str, list[dict[str, Any]]]:
    warmup_start = settings.start - timedelta(days=max_sma_slow * 3)
    return {
        symbol: client.get_historical_daily_bars(symbol, warmup_start, settings.end)
        for symbol in settings.watchlist
    }


def score_result(summary: dict[str, Any], min_trades: int) -> Decimal:
    trades = int(summary["trades"])
    if trades < min_trades:
        return Decimal("-999999")

    profit_factor = summary["profit_factor"]
    pf = Decimal("10") if profit_factor is None and trades > 0 else Decimal(str(profit_factor or "0"))
    total_return = Decimal(str(summary["total_return_pct"]))
    max_drawdown = Decimal(str(summary["max_drawdown_pct"]))

    # Favor robust profit factor and return while penalizing drawdown.
    return pf + (total_return * Decimal("10")) - (max_drawdown * Decimal("5"))


def optimize(base: BacktestSettings, args: argparse.Namespace) -> list[OptimizationRow]:
    sma_fast_values = parse_int_list(args.sma_fast_values)
    sma_slow_values = parse_int_list(args.sma_slow_values)
    stop_loss_values = parse_decimal_list(args.stop_loss_values)
    reward_risk_values = parse_decimal_list(args.reward_risk_values)
    risk_fraction_values = parse_decimal_list(args.risk_fraction_values)

    max_sma_slow = max(sma_slow_values)
    client = AlpacaRestClient(to_trader_settings(dataclasses.replace(base, sma_slow=max_sma_slow)))
    bars_by_symbol = fetch_grid_bars(base, client, max_sma_slow)

    rows: list[OptimizationRow] = []
    for sma_fast in sma_fast_values:
        for sma_slow in sma_slow_values:
            if sma_fast >= sma_slow:
                logging.info("SKIP invalid SMA combo fast=%s slow=%s", sma_fast, sma_slow)
                continue
            for stop_loss_pct in stop_loss_values:
                for reward_risk_ratio in reward_risk_values:
                    for risk_fraction in risk_fraction_values:
                        settings = dataclasses.replace(
                            base,
                            sma_fast=sma_fast,
                            sma_slow=sma_slow,
                            stop_loss_pct=stop_loss_pct,
                            reward_risk_ratio=reward_risk_ratio,
                            risk_fraction=risk_fraction,
                        )
                        validate_backtest_settings(settings)
                        result = run_backtest_on_bars(settings, bars_by_symbol)
                        summary = result["summary"]
                        rows.append(OptimizationRow(
                            sma_fast=sma_fast,
                            sma_slow=sma_slow,
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
                            score=score_result(summary, args.min_trades),
                        ))

    return sorted(
        rows,
        key=lambda row: (
            row.score,
            row.profit_factor if row.profit_factor is not None else Decimal("999"),
            row.total_return_pct,
            -row.max_drawdown_pct,
            row.trades,
        ),
        reverse=True,
    )


def print_table(rows: list[OptimizationRow], limit: int) -> None:
    print("OPTIMIZATION RESULTS")
    print(
        "rank sma stop rr risk final return max_dd trades win_rate profit_factor score"
    )
    for rank, row in enumerate(rows[:limit], start=1):
        profit_factor = "n/a" if row.profit_factor is None else str(row.profit_factor)
        print(
            f"{rank} "
            f"{row.sma_fast}/{row.sma_slow} "
            f"{row.stop_loss_pct} "
            f"{row.reward_risk_ratio} "
            f"{row.risk_fraction} "
            f"{decimal_to_str(row.final_equity)} "
            f"{decimal_to_str(row.total_return_pct * Decimal('100'))}% "
            f"{decimal_to_str(row.max_drawdown_pct * Decimal('100'))}% "
            f"{row.trades} "
            f"{decimal_to_str(row.win_rate_pct * Decimal('100'))}% "
            f"{profit_factor} "
            f"{row.score}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize sma_trend backtest parameters.")
    parser.add_argument("--start", help="Backtest start date, YYYY-MM-DD. Defaults to BACKTEST_START.")
    parser.add_argument("--end", help="Backtest end date, YYYY-MM-DD. Defaults to BACKTEST_END.")
    parser.add_argument("--watchlist", help="Comma-separated symbols. Defaults to WATCHLIST.")
    parser.add_argument("--data-feed", help="Alpaca data feed. Defaults to DATA_FEED.")
    parser.add_argument("--initial-equity", help="Starting equity. Defaults to BACKTEST_INITIAL_EQUITY.")
    parser.add_argument("--sma-fast-values", default="10,20,30", help="Comma-separated fast SMA values.")
    parser.add_argument("--sma-slow-values", default="50,100,150,200", help="Comma-separated slow SMA values.")
    parser.add_argument("--stop-loss-values", default="0.02,0.03,0.05,0.08", help="Comma-separated stop-loss percentages.")
    parser.add_argument("--reward-risk-values", default="1.5,2.0,2.5,3.0", help="Comma-separated reward/risk ratios.")
    parser.add_argument("--risk-fraction-values", default="0.001", help="Comma-separated risk fractions.")
    parser.add_argument("--min-trades", type=int, default=8, help="Rows with fewer trades get a large score penalty.")
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
