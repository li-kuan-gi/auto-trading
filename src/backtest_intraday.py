"""
Backtest an intraday swing pullback strategy with daily trend filters.

The model intentionally uses confirmed daily bars only: an intraday signal on a
given date can only see daily trend state through the prior trading day.
"""

from __future__ import annotations

import argparse
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import logging
from pathlib import Path
from statistics import median
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from swing_trader import (
    AlpacaRestClient,
    Settings,
    compute_sma,
    d,
    daily_trend_pass as _swing_daily_trend_pass,
    decimal_to_str,
    env_bool,
    env_decimal,
    env_int,
    env_str,
    parse_bar_time,
    parse_watchlist,
    round_price,
    round_qty,
    setup_logging,
)


DailyTrendCache = dict[str, dict[date, tuple[bool, Decimal]]]


@dataclass(frozen=True)
class IntradaySettings:
    alpaca_api_key: str
    alpaca_secret_key: str
    paper: bool
    watchlist: list[str]
    market_symbol: str
    data_feed: str
    initial_equity: Decimal
    risk_fraction: Decimal
    reward_risk_ratio: Decimal
    stop_loss_pct: Decimal
    allow_fractional: bool
    intraday_timeframe: str
    daily_fast: int
    daily_slow: int
    intraday_sma: int
    breakout_lookback: int
    start: date
    end: date
    end_mode: str
    log_level: str


@dataclass(frozen=True)
class IntradaySignal:
    symbol: str
    signal_time: datetime
    entry_time: datetime
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    selection_score: Decimal


@dataclass
class IntradayPosition:
    symbol: str
    entry_time: datetime
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class IntradayTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    pnl: Decimal
    return_pct: Decimal
    holding_hours: Decimal
    same_day_exit: bool
    exit_reason: str


@dataclass(frozen=True)
class IntradayExit:
    reason: str
    exit_time: datetime
    exit_price: Decimal


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def normalize_end_mode(value: str) -> str:
    return value.strip().lower().replace("-", "_")



def load_settings(args: argparse.Namespace) -> IntradaySettings:
    if load_dotenv is not None:
        load_dotenv()

    end = parse_date(args.end or env_str("BACKTEST_END", date.today().isoformat()))
    start = parse_date(args.start or env_str("BACKTEST_START", (end - timedelta(days=365)).isoformat()))

    settings = IntradaySettings(
        alpaca_api_key=env_str("ALPACA_API_KEY"),
        alpaca_secret_key=env_str("ALPACA_SECRET_KEY"),
        paper=env_bool("PAPER", True),
        watchlist=parse_watchlist(args.watchlist or env_str("WATCHLIST", "SPY")),
        market_symbol=(args.market_symbol or env_str("MARKET_SYMBOL", "QQQ")).upper(),
        data_feed=(args.data_feed or env_str("DATA_FEED", "iex")).lower(),
        initial_equity=d(args.initial_equity) if args.initial_equity else env_decimal("BACKTEST_INITIAL_EQUITY", "10000"),
        risk_fraction=d(args.risk_fraction) if args.risk_fraction else env_decimal("RISK_FRACTION", "0.001"),
        reward_risk_ratio=d(args.reward_risk_ratio) if args.reward_risk_ratio else env_decimal("REWARD_RISK_RATIO", "3.0"),
        stop_loss_pct=d(args.stop_loss_pct) if args.stop_loss_pct else env_decimal("STOP_LOSS_PCT", "0.03"),
        allow_fractional=env_bool("ALLOW_FRACTIONAL", False),
        intraday_timeframe=args.intraday_timeframe or env_str("INTRADAY_TIMEFRAME", "2Hour"),
        daily_fast=int(args.daily_fast) if args.daily_fast else env_int("DAILY_FAST", 20),
        daily_slow=int(args.daily_slow) if args.daily_slow else env_int("DAILY_SLOW", 100),
        intraday_sma=int(args.intraday_sma) if args.intraday_sma else env_int("INTRADAY_SMA", 20),
        breakout_lookback=int(args.breakout_lookback) if args.breakout_lookback else env_int("BREAKOUT_LOOKBACK", 5),
        start=start,
        end=end,
        end_mode=normalize_end_mode(getattr(args, "end_mode", None) or env_str("BACKTEST_END_MODE", "window")),
        log_level=env_str("LOG_LEVEL", "INFO").upper(),
    )
    validate_settings(settings)
    return settings


def validate_settings(settings: IntradaySettings) -> None:
    missing = []
    if not settings.alpaca_api_key:
        missing.append("ALPACA_API_KEY")
    if not settings.alpaca_secret_key:
        missing.append("ALPACA_SECRET_KEY")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    if settings.start >= settings.end:
        raise ValueError("BACKTEST_START must be before BACKTEST_END")
    if settings.initial_equity <= 0:
        raise ValueError("BACKTEST_INITIAL_EQUITY must be > 0")
    if settings.risk_fraction <= 0 or settings.risk_fraction > Decimal("0.10"):
        raise ValueError("RISK_FRACTION must be > 0 and <= 0.10")
    if settings.reward_risk_ratio <= 0:
        raise ValueError("REWARD_RISK_RATIO must be > 0")
    if settings.stop_loss_pct <= 0 or settings.stop_loss_pct >= Decimal("0.50"):
        raise ValueError("STOP_LOSS_PCT must be > 0 and < 0.50")
    if settings.daily_fast <= 1 or settings.daily_slow <= 1 or settings.daily_fast >= settings.daily_slow:
        raise ValueError("DAILY_FAST must be > 1 and < DAILY_SLOW")
    if settings.intraday_sma <= 1:
        raise ValueError("INTRADAY_SMA must be > 1")
    if settings.breakout_lookback < 0:
        raise ValueError("BREAKOUT_LOOKBACK must be >= 0")
    if settings.end_mode not in {"window", "signal_cohort"}:
        raise ValueError("BACKTEST_END_MODE must be one of: window, signal_cohort")


def to_trader_settings(settings: IntradaySettings) -> Settings:
    return Settings(
        alpaca_api_key=settings.alpaca_api_key,
        alpaca_secret_key=settings.alpaca_secret_key,
        fmp_api_key="not-used-by-intraday-backtest",
        paper=settings.paper,
        enable_trading=False,
        watchlist=settings.watchlist,
        strategy="intraday_swing",
        symbol_selection_method="best_signal",
        data_feed=settings.data_feed,
        market_symbol=settings.market_symbol,
        risk_fraction=settings.risk_fraction,
        reward_risk_ratio=settings.reward_risk_ratio,
        stop_loss_pct=settings.stop_loss_pct,
        allow_fractional=settings.allow_fractional,
        max_position_count=1,
        earnings_block_days_before=1,
        earnings_block_days_after=1,
        manual_blackout_file=Path("config/manual_blackout_events.csv"),
        sma_fast=settings.daily_fast,
        sma_slow=settings.daily_slow,
        historical_lookback_days=365,
        intraday_timeframe=settings.intraday_timeframe,
        daily_fast=settings.daily_fast,
        daily_slow=settings.daily_slow,
        intraday_sma=settings.intraday_sma,
        breakout_lookback=settings.breakout_lookback,
        log_level=settings.log_level,
    )


def daily_trend_pass(settings: IntradaySettings, bars: list[dict[str, Any]], as_of: date) -> tuple[bool, Decimal]:
    ok, strength, _ = _swing_daily_trend_pass(bars, as_of, settings.daily_fast, settings.daily_slow)
    return ok, strength


def intraday_signal_for_index(
    settings: IntradaySettings,
    symbol: str,
    bars: list[dict[str, Any]],
    closes: list[Decimal],
    highs: list[Decimal],
    i: int,
    trend_strength: Decimal,
) -> Optional[IntradaySignal]:
    if i < settings.intraday_sma or i + 1 >= len(bars):
        return None

    history = closes[: i + 1]
    prev_history = closes[:i]
    latest_sma = compute_sma(history, settings.intraday_sma)
    prev_sma = compute_sma(prev_history, settings.intraday_sma)
    latest_close = history[-1]
    prev_close = history[-2]

    crossed_up = latest_close > latest_sma and prev_close <= prev_sma
    if not crossed_up:
        return None

    if settings.breakout_lookback > 0:
        if i < settings.breakout_lookback:
            return None
        prior_high = max(highs[i - settings.breakout_lookback:i])
        if latest_close <= prior_high:
            return None

    entry_bar = bars[i + 1]
    entry_price = round_price(d(entry_bar["o"]))
    stop_price = round_price(entry_price * (Decimal("1") - settings.stop_loss_pct))
    take_profit_price = round_price(entry_price + (entry_price - stop_price) * settings.reward_risk_ratio)
    breakout_strength = (latest_close / latest_sma) - Decimal("1")

    return IntradaySignal(
        symbol=symbol,
        signal_time=parse_bar_time(bars[i]),
        entry_time=parse_bar_time(entry_bar),
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        selection_score=trend_strength + breakout_strength,
    )


def calculate_qty(settings: IntradaySettings, equity: Decimal, signal: IntradaySignal) -> Decimal:
    per_share_risk = signal.entry_price - signal.stop_price
    if per_share_risk <= 0:
        raise RuntimeError(f"Invalid stop for {signal.symbol}: entry={signal.entry_price} stop={signal.stop_price}")
    risk_qty = (equity * settings.risk_fraction) / per_share_risk
    cash_qty = equity / signal.entry_price
    return round_qty(min(risk_qty, cash_qty), settings.allow_fractional)


def exit_for_bar(
    settings: IntradaySettings,
    position: IntradayPosition,
    bar: dict[str, Any],
    bars: list[dict[str, Any]],
    closes: list[Decimal],
    i: int,
) -> Optional[IntradayExit]:
    current_time = parse_bar_time(bar)
    open_price = d(bar["o"])
    low = d(bar["l"])
    high = d(bar["h"])
    close = d(bar["c"])

    if low <= position.stop_price:
        return IntradayExit("stop_loss", current_time, round_price(min(open_price, position.stop_price)))
    if high >= position.take_profit_price:
        return IntradayExit("take_profit", current_time, round_price(max(open_price, position.take_profit_price)))

    history = closes[: i + 1]
    if len(history) >= settings.intraday_sma and close < compute_sma(history, settings.intraday_sma):
        if i + 1 < len(bars):
            exit_bar = bars[i + 1]
            exit_time = parse_bar_time(exit_bar)
            if settings.end_mode == "signal_cohort" or exit_time.date() <= settings.end:
                return IntradayExit("intraday_sma_exit", exit_time, round_price(d(exit_bar["o"])))
            return None
        return IntradayExit("intraday_sma_exit", current_time, round_price(close))

    return None


def update_drawdown(equity: Decimal, peak_equity: Decimal, max_drawdown: Decimal) -> tuple[Decimal, Decimal]:
    peak_equity = max(peak_equity, equity)
    drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else Decimal("0")
    return peak_equity, max(max_drawdown, drawdown)


def close_position(
    equity: Decimal,
    position: IntradayPosition,
    exit_result: IntradayExit,
    trades: list[IntradayTrade],
) -> Decimal:
    pnl = (exit_result.exit_price - position.entry_price) * position.qty
    equity += pnl
    holding_hours = Decimal(str((exit_result.exit_time - position.entry_time).total_seconds() / 3600))
    trades.append(IntradayTrade(
        symbol=position.symbol,
        entry_time=position.entry_time,
        exit_time=exit_result.exit_time,
        entry_price=position.entry_price,
        exit_price=exit_result.exit_price,
        qty=position.qty,
        pnl=pnl,
        return_pct=(exit_result.exit_price / position.entry_price) - Decimal("1"),
        holding_hours=holding_hours,
        same_day_exit=position.entry_time.date() == exit_result.exit_time.date(),
        exit_reason=exit_result.reason,
    ))
    return equity


def build_daily_trend_cache(
    settings: IntradaySettings,
    daily_bars_by_symbol: dict[str, list[dict[str, Any]]],
    intraday_bars_by_symbol: dict[str, list[dict[str, Any]]],
) -> DailyTrendCache:
    trading_dates = sorted({
        dt
        for bars in intraday_bars_by_symbol.values()
        for bar in bars
        if settings.start <= (dt := parse_bar_time(bar).date()) <= settings.end
    })
    symbols = sorted(set(settings.watchlist + [settings.market_symbol]))
    return {
        symbol: {
            trading_date: daily_trend_pass(settings, daily_bars_by_symbol.get(symbol, []), trading_date)
            for trading_date in trading_dates
        }
        for symbol in symbols
    }


def run_backtest(
    settings: IntradaySettings,
    daily_bars_by_symbol: dict[str, list[dict[str, Any]]],
    intraday_bars_by_symbol: dict[str, list[dict[str, Any]]],
    daily_trend_cache: DailyTrendCache | None = None,
) -> dict[str, Any]:
    if settings.market_symbol not in daily_bars_by_symbol:
        raise ValueError(f"market_symbol {settings.market_symbol!r} not found in daily_bars_by_symbol")
    resolved_cache = daily_trend_cache if daily_trend_cache is not None else build_daily_trend_cache(
        settings, daily_bars_by_symbol, intraday_bars_by_symbol
    )
    time_index_by_symbol = {
        symbol: {parse_bar_time(bar): i for i, bar in enumerate(bars)}
        for symbol, bars in intraday_bars_by_symbol.items()
    }
    closes_by_symbol = {
        symbol: [d(bar["c"]) for bar in bars]
        for symbol, bars in intraday_bars_by_symbol.items()
    }
    highs_by_symbol = {
        symbol: [d(bar["h"]) for bar in bars]
        for symbol, bars in intraday_bars_by_symbol.items()
    }
    all_times = sorted({
        ts
        for index in time_index_by_symbol.values()
        for ts in index
        if settings.start <= ts.date()
    })
    if settings.end_mode == "window":
        timeline = [ts for ts in all_times if ts.date() <= settings.end]
    else:
        timeline = all_times

    equity = settings.initial_equity
    peak_equity = equity
    max_drawdown = Decimal("0")
    position: Optional[IntradayPosition] = None
    pending_entry: Optional[IntradaySignal] = None
    pending_exit: Optional[IntradayExit] = None
    trades: list[IntradayTrade] = []

    for current_time in timeline:
        in_entry_window = settings.start <= current_time.date() <= settings.end
        if not in_entry_window and position is None and pending_entry is None and pending_exit is None:
            break

        if position is not None and pending_exit is not None and pending_exit.exit_time <= current_time:
            equity = close_position(equity, position, pending_exit, trades)
            position = None
            pending_exit = None
            peak_equity, max_drawdown = update_drawdown(equity, peak_equity, max_drawdown)

        if pending_entry and pending_entry.entry_time == current_time and position is None:
            if settings.start <= pending_entry.entry_time.date() <= settings.end:
                qty = calculate_qty(settings, equity, pending_entry)
                if qty > 0:
                    position = IntradayPosition(
                        symbol=pending_entry.symbol,
                        entry_time=pending_entry.entry_time,
                        entry_price=pending_entry.entry_price,
                        stop_price=pending_entry.stop_price,
                        take_profit_price=pending_entry.take_profit_price,
                        qty=qty,
                    )
            pending_entry = None

        if position is not None and pending_exit is None:
            bars = intraday_bars_by_symbol[position.symbol]
            index = time_index_by_symbol[position.symbol].get(current_time)
            if index is not None:
                bar = bars[index]
                closes = closes_by_symbol[position.symbol]
                exit_result = exit_for_bar(settings, position, bar, bars, closes, index)
                mark_price = d(bar["l"])
                if exit_result:
                    reason = exit_result.reason
                    exit_time = exit_result.exit_time
                    exit_price = exit_result.exit_price
                    if reason == "stop_loss":
                        mark_price = exit_price
                    else:
                        mark_price = min(mark_price, exit_price)
                    marked_equity = equity + ((mark_price - position.entry_price) * position.qty)
                    peak_equity, max_drawdown = update_drawdown(marked_equity, peak_equity, max_drawdown)
                    if exit_time == current_time:
                        equity = close_position(equity, position, exit_result, trades)
                        position = None
                        peak_equity, max_drawdown = update_drawdown(equity, peak_equity, max_drawdown)
                    else:
                        pending_exit = exit_result
                        continue
                else:
                    marked_equity = equity + ((mark_price - position.entry_price) * position.qty)
                    peak_equity, max_drawdown = update_drawdown(marked_equity, peak_equity, max_drawdown)

        if position is not None or pending_entry is not None:
            continue
        if not in_entry_window:
            continue

        market_ok, _ = resolved_cache.get(settings.market_symbol, {}).get(current_time.date(), (False, Decimal("0")))
        if not market_ok:
            continue

        candidates: list[IntradaySignal] = []
        for symbol in settings.watchlist:
            bars = intraday_bars_by_symbol.get(symbol, [])
            index = time_index_by_symbol.get(symbol, {}).get(current_time)
            if index is None:
                continue
            symbol_ok, trend_strength = resolved_cache.get(symbol, {}).get(current_time.date(), (False, Decimal("0")))
            if not symbol_ok:
                continue
            signal = intraday_signal_for_index(
                settings,
                symbol,
                bars,
                closes_by_symbol[symbol],
                highs_by_symbol[symbol],
                index,
                trend_strength,
            )
            if signal is not None:
                if settings.start <= signal.signal_time.date() <= settings.end and signal.entry_time.date() <= settings.end:
                    candidates.append(signal)

        if candidates:
            pending_entry = max(candidates, key=lambda signal: signal.selection_score)

    if position is not None:
        bars = intraday_bars_by_symbol[position.symbol]
        last_bar = next(
            (
                bar
                for bar in reversed(bars)
                if settings.end_mode == "signal_cohort" or parse_bar_time(bar).date() <= settings.end
            ),
            None,
        )
        if last_bar is not None:
            exit_time = parse_bar_time(last_bar)
            exit_price = round_price(d(last_bar["c"]))
            pnl = (exit_price - position.entry_price) * position.qty
            equity += pnl
            holding_hours = Decimal(str((exit_time - position.entry_time).total_seconds() / 3600))
            trades.append(IntradayTrade(
                symbol=position.symbol,
                entry_time=position.entry_time,
                exit_time=exit_time,
                entry_price=position.entry_price,
                exit_price=exit_price,
                qty=position.qty,
                pnl=pnl,
                return_pct=(exit_price / position.entry_price) - Decimal("1"),
                holding_hours=holding_hours,
                same_day_exit=position.entry_time.date() == exit_time.date(),
                exit_reason="end_of_data" if settings.end_mode == "signal_cohort" else "end_of_backtest",
            ))
            peak_equity, max_drawdown = update_drawdown(equity, peak_equity, max_drawdown)

    return build_result(settings, equity, max_drawdown, trades, timeline)


def max_day_trades_in_5_business_days(trades: list[IntradayTrade], timeline: list[datetime]) -> int:
    trading_dates = sorted({ts.date() for ts in timeline})
    day_trade_counts: dict[date, int] = {}
    for trade in trades:
        if trade.same_day_exit:
            day_trade_counts[trade.exit_time.date()] = day_trade_counts.get(trade.exit_time.date(), 0) + 1

    max_count = 0
    for i, _ in enumerate(trading_dates):
        window = trading_dates[max(0, i - 4): i + 1]
        max_count = max(max_count, sum(day_trade_counts.get(dt, 0) for dt in window))
    return max_count


def build_result(
    settings: IntradaySettings,
    equity: Decimal,
    max_drawdown: Decimal,
    trades: list[IntradayTrade],
    timeline: list[datetime],
) -> dict[str, Any]:
    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl <= 0]
    gross_profit = sum((trade.pnl for trade in wins), Decimal("0"))
    gross_loss = abs(sum((trade.pnl for trade in losses), Decimal("0")))
    day_trades = [trade for trade in trades if trade.same_day_exit]
    holding_hours = [trade.holding_hours for trade in trades]
    exit_reasons: dict[str, int] = {}
    for trade in trades:
        exit_reasons[trade.exit_reason] = exit_reasons.get(trade.exit_reason, 0) + 1

    settings_payload = dataclasses.asdict(settings)
    settings_payload.pop("alpaca_api_key", None)
    settings_payload.pop("alpaca_secret_key", None)

    return {
        "settings": settings_payload,
        "summary": {
            "initial_equity": settings.initial_equity,
            "final_equity": equity,
            "total_return_pct": (equity / settings.initial_equity) - Decimal("1"),
            "max_drawdown_pct": max_drawdown,
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": (Decimal(len(wins)) / Decimal(len(trades))) if trades else Decimal("0"),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
            "day_trades": len(day_trades),
            "max_day_trades_in_5_business_days": max_day_trades_in_5_business_days(trades, timeline),
            "same_day_exit_rate": (Decimal(len(day_trades)) / Decimal(len(trades))) if trades else Decimal("0"),
            "avg_holding_hours": (sum(holding_hours, Decimal("0")) / Decimal(len(holding_hours))) if holding_hours else Decimal("0"),
            "median_holding_hours": Decimal(str(median(holding_hours))) if holding_hours else Decimal("0"),
            "exit_reasons": exit_reasons,
        },
        "trades": [dataclasses.asdict(trade) for trade in trades],
    }


def fetch_bars(settings: IntradaySettings, client: AlpacaRestClient) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    daily_start = settings.start - timedelta(days=settings.daily_slow * 3)
    intraday_start = settings.start - timedelta(days=max(10, settings.intraday_sma * 2, settings.breakout_lookback * 2))
    intraday_end = settings.end + timedelta(days=1)
    if settings.end_mode == "signal_cohort" and settings.end < date.today():
        intraday_end = date.today()
    symbols = sorted(set(settings.watchlist + [settings.market_symbol]))
    daily = {
        symbol: client.get_historical_stock_bars(symbol, "1Day", daily_start, settings.end)
        for symbol in symbols
    }
    intraday = {
        symbol: client.get_historical_stock_bars(symbol, settings.intraday_timeframe, intraday_start, intraday_end)
        for symbol in settings.watchlist
    }
    return daily, intraday


def print_report(result: dict[str, Any]) -> None:
    summary = result["summary"]
    print("INTRADAY BACKTEST SUMMARY")
    print(f"initial_equity: {decimal_to_str(summary['initial_equity'])}")
    print(f"final_equity: {decimal_to_str(summary['final_equity'])}")
    print(f"total_return_pct: {decimal_to_str(summary['total_return_pct'] * Decimal('100'))}%")
    print(f"max_drawdown_pct: {decimal_to_str(summary['max_drawdown_pct'] * Decimal('100'))}%")
    print(f"trades: {summary['trades']} wins: {summary['wins']} losses: {summary['losses']}")
    print(f"win_rate_pct: {decimal_to_str(summary['win_rate_pct'] * Decimal('100'))}%")
    print(f"profit_factor: {summary['profit_factor'] if summary['profit_factor'] is not None else 'n/a'}")
    print(f"day_trades: {summary['day_trades']}")
    print(f"max_day_trades_in_5_business_days: {summary['max_day_trades_in_5_business_days']}")
    print(f"same_day_exit_rate: {decimal_to_str(summary['same_day_exit_rate'] * Decimal('100'))}%")
    print(f"avg_holding_hours: {decimal_to_str(summary['avg_holding_hours'])}")
    print(f"median_holding_hours: {decimal_to_str(summary['median_holding_hours'])}")
    print(f"exit_reasons: {summary['exit_reasons']}")
    print()
    print("TRADES")
    for trade in result["trades"]:
        print(
            f"{trade['symbol']} {trade['entry_time']} -> {trade['exit_time']} "
            f"qty={trade['qty']} entry={trade['entry_price']} exit={trade['exit_price']} "
            f"pnl={decimal_to_str(trade['pnl'])} same_day={trade['same_day_exit']} "
            f"reason={trade['exit_reason']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest an intraday swing strategy with daily trend filters.")
    parser.add_argument("--start", help="Backtest start date, YYYY-MM-DD.")
    parser.add_argument("--end", help="Backtest end date, YYYY-MM-DD.")
    parser.add_argument("--watchlist", help="Comma-separated symbols. Defaults to WATCHLIST.")
    parser.add_argument("--market-symbol", help="Market trend filter symbol. Defaults to MARKET_SYMBOL or QQQ.")
    parser.add_argument("--data-feed", help="Alpaca data feed. Defaults to DATA_FEED.")
    parser.add_argument("--initial-equity", help="Starting equity.")
    parser.add_argument("--risk-fraction", help="Risk per trade.")
    parser.add_argument("--reward-risk-ratio", help="Take-profit R multiple.")
    parser.add_argument("--stop-loss-pct", help="Stop distance from entry.")
    parser.add_argument("--intraday-timeframe", help="Intraday timeframe. Defaults to 2Hour.")
    parser.add_argument("--daily-fast", help="Daily fast SMA. Defaults to 20.")
    parser.add_argument("--daily-slow", help="Daily slow SMA. Defaults to 100.")
    parser.add_argument("--intraday-sma", help="Intraday SMA trigger. Defaults to 20.")
    parser.add_argument("--breakout-lookback", help="Require close above prior N intraday highs; defaults to 5.")
    parser.add_argument(
        "--end-mode",
        choices=["window", "signal-cohort"],
        help=(
            "window liquidates trades within the requested backtest window; "
            "signal-cohort limits entries to the window but lets exits occur after --end."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(args)
    setup_logging(settings.log_level)
    client = AlpacaRestClient(to_trader_settings(settings))
    daily, intraday = fetch_bars(settings, client)
    result = run_backtest(settings, daily, intraday)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    else:
        print_report(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        logging.error("FATAL %s: %s", type(exc).__name__, exc)
        raise SystemExit(1) from exc
