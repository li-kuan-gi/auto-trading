"""
Backtest the sma_trend swing strategy against Alpaca daily bars.

The simulator is intentionally conservative:
- Signals are calculated after a confirmed daily close.
- Entries happen on the next trading day's open.
- If stop-loss and take-profit both touch on the same daily bar, stop-loss wins.
- Only one position is held at a time, matching the live trader guard.
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
    decimal_to_str,
    env_bool,
    env_decimal,
    env_int,
    env_str,
    parse_watchlist,
    round_price,
    round_qty,
    setup_logging,
)


@dataclass(frozen=True)
class BacktestSettings:
    alpaca_api_key: str
    alpaca_secret_key: str
    paper: bool
    watchlist: list[str]
    data_feed: str
    initial_equity: Decimal
    risk_fraction: Decimal
    reward_risk_ratio: Decimal
    stop_loss_pct: Decimal
    allow_fractional: bool
    sma_fast: int
    sma_slow: int
    start: date
    end: date
    log_level: str


@dataclass(frozen=True)
class BacktestSignal:
    symbol: str
    signal_date: date
    entry_date: date
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    selection_score: Decimal
    details: dict[str, Any]


@dataclass
class Position:
    symbol: str
    entry_date: date
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    qty: Decimal
    risk_amount: Decimal


@dataclass(frozen=True)
class TradeResult:
    symbol: str
    entry_date: date
    exit_date: date
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    pnl: Decimal
    return_pct: Decimal
    exit_reason: str


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def validate_backtest_settings(settings: BacktestSettings) -> None:
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
    if settings.sma_fast <= 1 or settings.sma_slow <= 1 or settings.sma_fast >= settings.sma_slow:
        raise ValueError("SMA_FAST must be > 1 and < SMA_SLOW")


def load_backtest_settings(args: argparse.Namespace) -> BacktestSettings:
    if load_dotenv is not None:
        load_dotenv()

    end = parse_date(args.end or env_str("BACKTEST_END", date.today().isoformat()))
    start = parse_date(args.start or env_str("BACKTEST_START", (end - timedelta(days=365)).isoformat()))

    settings = BacktestSettings(
        alpaca_api_key=env_str("ALPACA_API_KEY"),
        alpaca_secret_key=env_str("ALPACA_SECRET_KEY"),
        paper=env_bool("PAPER", True),
        watchlist=parse_watchlist(args.watchlist or env_str("WATCHLIST", "SPY")),
        data_feed=(args.data_feed or env_str("DATA_FEED", "iex")).lower(),
        initial_equity=d(args.initial_equity) if args.initial_equity else env_decimal("BACKTEST_INITIAL_EQUITY", "10000"),
        risk_fraction=d(args.risk_fraction) if args.risk_fraction else env_decimal("RISK_FRACTION", "0.01"),
        reward_risk_ratio=d(args.reward_risk_ratio) if args.reward_risk_ratio else env_decimal("REWARD_RISK_RATIO", "2.0"),
        stop_loss_pct=d(args.stop_loss_pct) if args.stop_loss_pct else env_decimal("STOP_LOSS_PCT", "0.05"),
        allow_fractional=env_bool("ALLOW_FRACTIONAL", False),
        sma_fast=int(args.sma_fast) if args.sma_fast else env_int("SMA_FAST", 20),
        sma_slow=int(args.sma_slow) if args.sma_slow else env_int("SMA_SLOW", 50),
        start=start,
        end=end,
        log_level=env_str("LOG_LEVEL", "INFO").upper(),
    )

    validate_backtest_settings(settings)
    return settings


def to_trader_settings(settings: BacktestSettings) -> Settings:
    return Settings(
        alpaca_api_key=settings.alpaca_api_key,
        alpaca_secret_key=settings.alpaca_secret_key,
        fmp_api_key="not-used-by-backtest",
        paper=settings.paper,
        enable_trading=False,
        watchlist=settings.watchlist,
        strategy="sma_trend",
        symbol_selection_method="best_signal",
        data_feed=settings.data_feed,
        risk_fraction=settings.risk_fraction,
        reward_risk_ratio=settings.reward_risk_ratio,
        stop_loss_pct=settings.stop_loss_pct,
        allow_fractional=settings.allow_fractional,
        max_position_count=1,
        earnings_block_days_before=1,
        earnings_block_days_after=1,
        manual_blackout_file=Path("config/manual_blackout_events.csv"),
        sma_fast=settings.sma_fast,
        sma_slow=settings.sma_slow,
        historical_lookback_days=365,
        log_level=settings.log_level,
    )


def bar_date(bar: dict[str, Any]) -> date:
    raw = str(bar.get("t", ""))[:10]
    return parse_date(raw)


def signal_for_index(settings: BacktestSettings, symbol: str, bars: list[dict[str, Any]], i: int) -> Optional[BacktestSignal]:
    if i < settings.sma_slow + 1 or i + 1 >= len(bars):
        return None

    history = bars[: i + 1]
    closes = [d(b["c"]) for b in history if "c" in b]
    if len(closes) < settings.sma_slow + 2:
        return None

    latest_close = closes[-1]
    prev_close = closes[-2]
    latest_fast = compute_sma(closes, settings.sma_fast)
    latest_slow = compute_sma(closes, settings.sma_slow)
    prev_fast = compute_sma(closes[:-1], settings.sma_fast)

    signal = latest_fast > latest_slow and latest_close > latest_fast and prev_close <= prev_fast
    if not signal:
        return None

    trend_strength = (latest_fast / latest_slow) - Decimal("1")
    breakout_strength = (latest_close / latest_fast) - Decimal("1")
    selection_score = trend_strength + breakout_strength

    entry_bar = bars[i + 1]
    entry_price = round_price(d(entry_bar["o"]))
    stop_price = round_price(entry_price * (Decimal("1") - settings.stop_loss_pct))
    take_profit_price = round_price(entry_price + (entry_price - stop_price) * settings.reward_risk_ratio)

    return BacktestSignal(
        symbol=symbol,
        signal_date=bar_date(bars[i]),
        entry_date=bar_date(entry_bar),
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        selection_score=selection_score,
        details={
            "latest_close": latest_close,
            "latest_fast": latest_fast,
            "latest_slow": latest_slow,
            "trend_strength": trend_strength,
            "breakout_strength": breakout_strength,
        },
    )


def exit_reason_for_bar(settings: BacktestSettings, position: Position, bars: list[dict[str, Any]], i: int) -> Optional[tuple[str, Decimal]]:
    bar = bars[i]
    open_price = d(bar["o"])
    low = d(bar["l"])
    high = d(bar["h"])

    if low <= position.stop_price:
        return "stop_loss", round_price(min(open_price, position.stop_price))
    if high >= position.take_profit_price:
        return "take_profit", position.take_profit_price

    closes = [d(b["c"]) for b in bars[: i + 1] if "c" in b]
    if len(closes) < settings.sma_slow:
        return None

    latest_close = closes[-1]
    latest_fast = compute_sma(closes, settings.sma_fast)
    latest_slow = compute_sma(closes, settings.sma_slow)
    if latest_fast < latest_slow or latest_close < latest_slow:
        return "sma_exit", round_price(latest_close)

    return None


def calculate_backtest_qty(settings: BacktestSettings, equity: Decimal, signal: BacktestSignal) -> Decimal:
    per_share_risk = signal.entry_price - signal.stop_price
    if per_share_risk <= 0:
        raise RuntimeError(f"Invalid stop for {signal.symbol}: entry={signal.entry_price} stop={signal.stop_price}")
    risk_qty = (equity * settings.risk_fraction) / per_share_risk
    cash_qty = equity / signal.entry_price
    return round_qty(min(risk_qty, cash_qty), settings.allow_fractional)


def update_drawdown(equity: Decimal, peak_equity: Decimal, max_drawdown: Decimal) -> tuple[Decimal, Decimal]:
    peak_equity = max(peak_equity, equity)
    drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else Decimal("0")
    return peak_equity, max(max_drawdown, drawdown)


def mark_position_equity(equity: Decimal, position: Position, price: Decimal) -> Decimal:
    return equity + ((price - position.entry_price) * position.qty)


def public_settings(settings: BacktestSettings) -> dict[str, Any]:
    result = dataclasses.asdict(settings)
    result.pop("alpaca_api_key", None)
    result.pop("alpaca_secret_key", None)
    return result


def fetch_historical_bars(settings: BacktestSettings, client: AlpacaRestClient) -> dict[str, list[dict[str, Any]]]:
    warmup_start = settings.start - timedelta(days=settings.sma_slow * 3)
    return {
        symbol: client.get_historical_daily_bars(symbol, warmup_start, settings.end)
        for symbol in settings.watchlist
    }


def run_backtest_on_bars(settings: BacktestSettings, bars_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    timeline = sorted({bar_date(bar) for bars in bars_by_symbol.values() for bar in bars if settings.start <= bar_date(bar) <= settings.end})

    equity = settings.initial_equity
    peak_equity = equity
    max_drawdown = Decimal("0")
    position: Optional[Position] = None
    trades: list[TradeResult] = []
    pending_entry: Optional[BacktestSignal] = None

    for current_date in timeline:
        if pending_entry and pending_entry.entry_date == current_date and position is None:
            qty = calculate_backtest_qty(settings, equity, pending_entry)
            if qty > 0:
                position = Position(
                    symbol=pending_entry.symbol,
                    entry_date=pending_entry.entry_date,
                    entry_price=pending_entry.entry_price,
                    stop_price=pending_entry.stop_price,
                    take_profit_price=pending_entry.take_profit_price,
                    qty=qty,
                    risk_amount=(pending_entry.entry_price - pending_entry.stop_price) * qty,
                )
            pending_entry = None

        if position is not None:
            bars = bars_by_symbol[position.symbol]
            indexes = [i for i, bar in enumerate(bars) if bar_date(bar) == current_date]
            if indexes:
                bar = bars[indexes[0]]
                exit_result = exit_reason_for_bar(settings, position, bars, indexes[0])
                if exit_result:
                    reason, exit_price = exit_result
                    drawdown_price = exit_price if reason == "stop_loss" else min(d(bar["l"]), exit_price)
                    marked_equity = mark_position_equity(equity, position, drawdown_price)
                    peak_equity, max_drawdown = update_drawdown(marked_equity, peak_equity, max_drawdown)
                    pnl = (exit_price - position.entry_price) * position.qty
                    equity += pnl
                    trades.append(TradeResult(
                        symbol=position.symbol,
                        entry_date=position.entry_date,
                        exit_date=current_date,
                        entry_price=position.entry_price,
                        exit_price=exit_price,
                        qty=position.qty,
                        pnl=pnl,
                        return_pct=(exit_price / position.entry_price) - Decimal("1"),
                        exit_reason=reason,
                    ))
                    position = None
                    peak_equity, max_drawdown = update_drawdown(equity, peak_equity, max_drawdown)
                else:
                    marked_equity = mark_position_equity(equity, position, d(bar["l"]))
                    peak_equity, max_drawdown = update_drawdown(marked_equity, peak_equity, max_drawdown)

        if position is not None or pending_entry is not None:
            continue

        candidates: list[BacktestSignal] = []
        for symbol, bars in bars_by_symbol.items():
            indexes = [i for i, bar in enumerate(bars) if bar_date(bar) == current_date]
            if indexes:
                signal = signal_for_index(settings, symbol, bars, indexes[0])
                if signal is not None:
                    candidates.append(signal)
        if candidates:
            pending_entry = max(candidates, key=lambda signal: signal.selection_score)

    if position is not None:
        bars = bars_by_symbol[position.symbol]
        last_bar = next((bar for bar in reversed(bars) if bar_date(bar) <= settings.end), None)
        if last_bar is not None:
            exit_price = round_price(d(last_bar["c"]))
            pnl = (exit_price - position.entry_price) * position.qty
            equity += pnl
            trades.append(TradeResult(
                symbol=position.symbol,
                entry_date=position.entry_date,
                exit_date=bar_date(last_bar),
                entry_price=position.entry_price,
                exit_price=exit_price,
                qty=position.qty,
                pnl=pnl,
                return_pct=(exit_price / position.entry_price) - Decimal("1"),
                exit_reason="end_of_backtest",
            ))
            peak_equity, max_drawdown = update_drawdown(equity, peak_equity, max_drawdown)

    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl <= 0]
    gross_profit = sum((trade.pnl for trade in wins), Decimal("0"))
    gross_loss = abs(sum((trade.pnl for trade in losses), Decimal("0")))

    return {
        "settings": public_settings(settings),
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
        },
        "trades": [dataclasses.asdict(trade) for trade in trades],
    }


def run_backtest(settings: BacktestSettings, client: AlpacaRestClient) -> dict[str, Any]:
    return run_backtest_on_bars(settings, fetch_historical_bars(settings, client))


def print_report(result: dict[str, Any]) -> None:
    summary = result["summary"]
    print("BACKTEST SUMMARY")
    print(f"initial_equity: {decimal_to_str(summary['initial_equity'])}")
    print(f"final_equity: {decimal_to_str(summary['final_equity'])}")
    print(f"total_return_pct: {decimal_to_str(summary['total_return_pct'] * Decimal('100'))}%")
    print(f"max_drawdown_pct: {decimal_to_str(summary['max_drawdown_pct'] * Decimal('100'))}%")
    print(f"trades: {summary['trades']} wins: {summary['wins']} losses: {summary['losses']}")
    print(f"win_rate_pct: {decimal_to_str(summary['win_rate_pct'] * Decimal('100'))}%")
    print(f"profit_factor: {summary['profit_factor'] if summary['profit_factor'] is not None else 'n/a'}")
    print()
    print("TRADES")
    for trade in result["trades"]:
        print(
            f"{trade['symbol']} {trade['entry_date']} -> {trade['exit_date']} "
            f"qty={trade['qty']} entry={trade['entry_price']} exit={trade['exit_price']} "
            f"pnl={decimal_to_str(trade['pnl'])} reason={trade['exit_reason']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the sma_trend strategy with Alpaca daily bars.")
    parser.add_argument("--start", help="Backtest start date, YYYY-MM-DD. Defaults to BACKTEST_START or one year before end.")
    parser.add_argument("--end", help="Backtest end date, YYYY-MM-DD. Defaults to BACKTEST_END or today.")
    parser.add_argument("--watchlist", help="Comma-separated symbols. Defaults to WATCHLIST.")
    parser.add_argument("--data-feed", help="Alpaca data feed, e.g. iex or sip. Defaults to DATA_FEED.")
    parser.add_argument("--initial-equity", help="Starting equity. Defaults to BACKTEST_INITIAL_EQUITY or 10000.")
    parser.add_argument("--risk-fraction", help="Risk per trade. Defaults to RISK_FRACTION.")
    parser.add_argument("--reward-risk-ratio", help="Take-profit R multiple. Defaults to REWARD_RISK_RATIO.")
    parser.add_argument("--stop-loss-pct", help="Stop distance from entry. Defaults to STOP_LOSS_PCT.")
    parser.add_argument("--sma-fast", help="Fast SMA period. Defaults to SMA_FAST.")
    parser.add_argument("--sma-slow", help="Slow SMA period. Defaults to SMA_SLOW.")
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of the text report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_backtest_settings(args)
    setup_logging(settings.log_level)
    client = AlpacaRestClient(to_trader_settings(settings))
    result = run_backtest(settings, client)
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
