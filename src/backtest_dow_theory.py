"""
Backtest the Dow Theory swing strategy against Alpaca daily bars.

Conservative assumptions:
- Swing points require N bars on each side to be confirmed (no look-ahead).
- Bull trend confirmed: two consecutive HH (Higher Highs) and HL (Higher Lows).
- Signal confirmed at close; entry fills at next open.
- Exit priority: stop-loss > take-profit > trend reversal (close < last swing low, exit next open).
- Last confirmed swing low is updated as the trend rises (trailing Dow support).
- One position at a time.
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
class DowSettings:
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
    swing_n: int
    start: date
    end: date
    log_level: str


@dataclass(frozen=True)
class DowSignal:
    symbol: str
    signal_date: date
    entry_date: date
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    last_swing_low: Decimal
    selection_score: Decimal


@dataclass
class DowPosition:
    symbol: str
    entry_date: date
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    qty: Decimal
    risk_amount: Decimal
    last_swing_low: Decimal


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


@dataclass(frozen=True)
class ExitResult:
    reason: str
    exit_date: date
    exit_price: Decimal


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def bar_date(bar: dict[str, Any]) -> date:
    raw = str(bar.get("t", ""))[:10]
    return parse_date(raw)


def is_swing_high(bars: list[dict[str, Any]], i: int, n: int) -> bool:
    if i < n or i + n >= len(bars):
        return False
    h = d(bars[i]["h"])
    return (
        all(h > d(bars[j]["h"]) for j in range(i - n, i))
        and all(h > d(bars[j]["h"]) for j in range(i + 1, i + n + 1))
    )


def is_swing_low(bars: list[dict[str, Any]], i: int, n: int) -> bool:
    if i < n or i + n >= len(bars):
        return False
    l = d(bars[i]["l"])
    return (
        all(l < d(bars[j]["l"]) for j in range(i - n, i))
        and all(l < d(bars[j]["l"]) for j in range(i + 1, i + n + 1))
    )


def dow_signal_for_index(
    settings: DowSettings,
    symbol: str,
    bars: list[dict[str, Any]],
    i: int,
    swing_highs: list[int],
    swing_lows: list[int],
) -> Optional[DowSignal]:
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    last_sh_price = d(bars[swing_highs[-1]]["h"])
    prev_sh_price = d(bars[swing_highs[-2]]["h"])
    last_sl_price = d(bars[swing_lows[-1]]["l"])
    prev_sl_price = d(bars[swing_lows[-2]]["l"])

    if last_sh_price <= prev_sh_price:
        return None
    if last_sl_price <= prev_sl_price:
        return None

    if i + 1 >= len(bars):
        return None

    entry_bar = bars[i + 1]
    entry_price = round_price(d(entry_bar["o"]))
    stop_price = round_price(entry_price * (Decimal("1") - settings.stop_loss_pct))
    take_profit_price = round_price(
        entry_price + (entry_price - stop_price) * settings.reward_risk_ratio
    )

    hh_strength = (last_sh_price / prev_sh_price) - Decimal("1")
    hl_strength = (last_sl_price / prev_sl_price) - Decimal("1")

    return DowSignal(
        symbol=symbol,
        signal_date=bar_date(bars[i]),
        entry_date=bar_date(entry_bar),
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        last_swing_low=last_sl_price,
        selection_score=hh_strength + hl_strength,
    )


def dow_exit_for_bar(
    position: DowPosition,
    bars: list[dict[str, Any]],
    i: int,
) -> Optional[ExitResult]:
    bar = bars[i]
    current_date = bar_date(bar)
    open_price = d(bar["o"])
    low = d(bar["l"])
    high = d(bar["h"])
    close = d(bar["c"])

    if low <= position.stop_price:
        return ExitResult("stop_loss", current_date, round_price(min(open_price, position.stop_price)))

    if high >= position.take_profit_price:
        return ExitResult("take_profit", current_date, round_price(max(open_price, position.take_profit_price)))

    if close < position.last_swing_low:
        if i + 1 < len(bars):
            exit_bar = bars[i + 1]
            return ExitResult("trend_reversal", bar_date(exit_bar), round_price(d(exit_bar["o"])))
        return ExitResult("trend_reversal", current_date, round_price(close))

    return None


def calculate_qty(settings: DowSettings, equity: Decimal, signal: DowSignal) -> Decimal:
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


def mark_position_equity(equity: Decimal, position: DowPosition, price: Decimal) -> Decimal:
    return equity + ((price - position.entry_price) * position.qty)


def close_position(
    equity: Decimal,
    position: DowPosition,
    exit_result: ExitResult,
    trades: list[TradeResult],
) -> Decimal:
    pnl = (exit_result.exit_price - position.entry_price) * position.qty
    equity += pnl
    trades.append(TradeResult(
        symbol=position.symbol,
        entry_date=position.entry_date,
        exit_date=exit_result.exit_date,
        entry_price=position.entry_price,
        exit_price=exit_result.exit_price,
        qty=position.qty,
        pnl=pnl,
        return_pct=(exit_result.exit_price / position.entry_price) - Decimal("1"),
        exit_reason=exit_result.reason,
    ))
    return equity


def public_settings(settings: DowSettings) -> dict[str, Any]:
    result = dataclasses.asdict(settings)
    result.pop("alpaca_api_key", None)
    result.pop("alpaca_secret_key", None)
    return result


def fetch_historical_bars(
    settings: DowSettings, client: AlpacaRestClient
) -> dict[str, list[dict[str, Any]]]:
    warmup_start = settings.start - timedelta(days=120)
    return {
        symbol: client.get_historical_daily_bars(symbol, warmup_start, settings.end)
        for symbol in settings.watchlist
    }


def run_backtest_on_bars(
    settings: DowSettings,
    bars_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    n = settings.swing_n

    date_index_by_symbol = {
        symbol: {bar_date(bar): i for i, bar in enumerate(bars)}
        for symbol, bars in bars_by_symbol.items()
    }

    timeline = sorted({
        dt
        for date_index in date_index_by_symbol.values()
        for dt in date_index
        if settings.start <= dt <= settings.end
    })

    swing_highs_by_symbol: dict[str, list[int]] = {s: [] for s in bars_by_symbol}
    swing_lows_by_symbol: dict[str, list[int]] = {s: [] for s in bars_by_symbol}

    # Pre-confirm swings from warmup bars (before settings.start).
    # The main loop only iterates over timeline dates, so warmup-period pivots
    # would never be confirmed without this pass.
    for symbol, bars in bars_by_symbol.items():
        warmup_end = next(
            (i for i, b in enumerate(bars) if bar_date(b) >= settings.start),
            len(bars),
        )
        # Extend n bars past warmup_end so pivots whose right-bracket reaches
        # into the main period are also confirmed before signal scanning starts.
        for i in range(min(warmup_end + n, len(bars))):
            confirm_idx = i - n
            shs = swing_highs_by_symbol[symbol]
            sls = swing_lows_by_symbol[symbol]
            if is_swing_high(bars, confirm_idx, n) and (not shs or shs[-1] != confirm_idx):
                shs.append(confirm_idx)
            if is_swing_low(bars, confirm_idx, n) and (not sls or sls[-1] != confirm_idx):
                sls.append(confirm_idx)

    equity = settings.initial_equity
    peak_equity = equity
    max_drawdown = Decimal("0")
    position: Optional[DowPosition] = None
    trades: list[TradeResult] = []
    pending_entry: Optional[DowSignal] = None
    pending_exit: Optional[ExitResult] = None

    for current_date in timeline:
        # Settle pending exit
        if position is not None and pending_exit is not None and pending_exit.exit_date <= current_date:
            equity = close_position(equity, position, pending_exit, trades)
            position = None
            pending_exit = None
            peak_equity, max_drawdown = update_drawdown(equity, peak_equity, max_drawdown)

        # Fill pending entry
        if pending_entry is not None and pending_entry.entry_date == current_date and position is None:
            qty = calculate_qty(settings, equity, pending_entry)
            if qty > 0:
                position = DowPosition(
                    symbol=pending_entry.symbol,
                    entry_date=pending_entry.entry_date,
                    entry_price=pending_entry.entry_price,
                    stop_price=pending_entry.stop_price,
                    take_profit_price=pending_entry.take_profit_price,
                    qty=qty,
                    risk_amount=(pending_entry.entry_price - pending_entry.stop_price) * qty,
                    last_swing_low=pending_entry.last_swing_low,
                )
            pending_entry = None

        # Confirm swing points (bar idx - n is now fully bracketed by n bars on each side)
        for symbol, bars in bars_by_symbol.items():
            idx = date_index_by_symbol[symbol].get(current_date)
            if idx is None:
                continue
            confirm_idx = idx - n
            if confirm_idx < n:
                continue
            shs = swing_highs_by_symbol[symbol]
            sls = swing_lows_by_symbol[symbol]
            if is_swing_high(bars, confirm_idx, n) and (not shs or shs[-1] != confirm_idx):
                shs.append(confirm_idx)
            if is_swing_low(bars, confirm_idx, n) and (not sls or sls[-1] != confirm_idx):
                sls.append(confirm_idx)

        # Check exit for current position (before trailing stop update so that
        # a newly confirmed swing low takes effect on the *next* bar, not this one)
        if position is not None and pending_exit is None:
            bars = bars_by_symbol[position.symbol]
            idx = date_index_by_symbol[position.symbol].get(current_date)
            if idx is not None:
                bar = bars[idx]
                exit_result = dow_exit_for_bar(position, bars, idx)
                if exit_result:
                    reason = exit_result.reason
                    drawdown_price = (
                        exit_result.exit_price
                        if reason == "stop_loss"
                        else min(d(bar["l"]), exit_result.exit_price)
                    )
                    marked_equity = mark_position_equity(equity, position, drawdown_price)
                    peak_equity, max_drawdown = update_drawdown(marked_equity, peak_equity, max_drawdown)
                    if exit_result.exit_date == current_date:
                        equity = close_position(equity, position, exit_result, trades)
                        position = None
                        peak_equity, max_drawdown = update_drawdown(equity, peak_equity, max_drawdown)
                    else:
                        pending_exit = exit_result
                        continue
                else:
                    marked_equity = mark_position_equity(equity, position, d(bar["l"]))
                    peak_equity, max_drawdown = update_drawdown(marked_equity, peak_equity, max_drawdown)

        # Trail the swing low: apply newly confirmed pivot as stop for the *next* bar onward.
        # Runs after exit check so the same bar's close is evaluated against the old stop.
        if position is not None and pending_exit is None:
            sls = swing_lows_by_symbol.get(position.symbol, [])
            if sls:
                latest_sl = d(bars_by_symbol[position.symbol][sls[-1]]["l"])
                if latest_sl > position.last_swing_low:
                    position.last_swing_low = latest_sl

        if position is not None or pending_entry is not None:
            continue

        # Scan for entry signals
        candidates: list[DowSignal] = []
        for symbol, bars in bars_by_symbol.items():
            idx = date_index_by_symbol[symbol].get(current_date)
            if idx is None:
                continue
            signal = dow_signal_for_index(
                settings,
                symbol,
                bars,
                idx,
                swing_highs_by_symbol[symbol],
                swing_lows_by_symbol[symbol],
            )
            if signal is not None:
                candidates.append(signal)

        if candidates:
            pending_entry = max(candidates, key=lambda s: s.selection_score)

    # Force close open position at end of backtest window
    if position is not None:
        bars = bars_by_symbol[position.symbol]
        last_bar = next((bar for bar in reversed(bars) if bar_date(bar) <= settings.end), None)
        if last_bar is not None:
            exit_price = round_price(d(last_bar["c"]))
            equity = close_position(
                equity, position,
                ExitResult("end_of_backtest", bar_date(last_bar), exit_price),
                trades,
            )
            peak_equity, max_drawdown = update_drawdown(equity, peak_equity, max_drawdown)

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum((t.pnl for t in wins), Decimal("0"))
    gross_loss = abs(sum((t.pnl for t in losses), Decimal("0")))

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


def run_backtest(settings: DowSettings, client: AlpacaRestClient) -> dict[str, Any]:
    return run_backtest_on_bars(settings, fetch_historical_bars(settings, client))


def print_report(result: dict[str, Any]) -> None:
    summary = result["summary"]
    print("DOW THEORY BACKTEST SUMMARY")
    print(f"initial_equity:   {decimal_to_str(summary['initial_equity'])}")
    print(f"final_equity:     {decimal_to_str(summary['final_equity'])}")
    print(f"total_return_pct: {decimal_to_str(summary['total_return_pct'] * Decimal('100'))}%")
    print(f"max_drawdown_pct: {decimal_to_str(summary['max_drawdown_pct'] * Decimal('100'))}%")
    print(f"trades: {summary['trades']}  wins: {summary['wins']}  losses: {summary['losses']}")
    print(f"win_rate_pct:     {decimal_to_str(summary['win_rate_pct'] * Decimal('100'))}%")
    print(f"profit_factor:    {summary['profit_factor'] if summary['profit_factor'] is not None else 'n/a'}")
    print()
    print("TRADES")
    for trade in result["trades"]:
        print(
            f"{trade['symbol']} {trade['entry_date']} -> {trade['exit_date']} "
            f"qty={trade['qty']} entry={trade['entry_price']} exit={trade['exit_price']} "
            f"pnl={decimal_to_str(trade['pnl'])} reason={trade['exit_reason']}"
        )


def validate_dow_settings(settings: DowSettings) -> None:
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
    if settings.swing_n < 1:
        raise ValueError("SWING_N must be >= 1")


def to_trader_settings(settings: DowSettings) -> Settings:
    return Settings(
        alpaca_api_key=settings.alpaca_api_key,
        alpaca_secret_key=settings.alpaca_secret_key,
        fmp_api_key="not-used-by-backtest",
        paper=settings.paper,
        enable_trading=False,
        watchlist=settings.watchlist,
        strategy="disabled",
        symbol_selection_method="best_signal",
        data_feed=settings.data_feed,
        market_symbol="SPY",
        risk_fraction=settings.risk_fraction,
        reward_risk_ratio=settings.reward_risk_ratio,
        stop_loss_pct=settings.stop_loss_pct,
        allow_fractional=settings.allow_fractional,
        max_position_count=1,
        earnings_block_days_before=1,
        earnings_block_days_after=1,
        manual_blackout_file=Path("config/manual_blackout_events.csv"),
        sma_fast=20,
        sma_slow=50,
        historical_lookback_days=365,
        intraday_timeframe="2Hour",
        daily_fast=20,
        daily_slow=100,
        intraday_sma=20,
        breakout_lookback=5,
        log_level=settings.log_level,
    )


def load_dow_settings(args: argparse.Namespace) -> DowSettings:
    if load_dotenv is not None:
        load_dotenv()

    end = parse_date(args.end or env_str("BACKTEST_END", date.today().isoformat()))
    start = parse_date(args.start or env_str("BACKTEST_START", (end - timedelta(days=365)).isoformat()))

    settings = DowSettings(
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
        swing_n=int(args.swing_n) if args.swing_n else env_int("SWING_N", 2),
        start=start,
        end=end,
        log_level=env_str("LOG_LEVEL", "INFO").upper(),
    )

    validate_dow_settings(settings)
    return settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the Dow Theory strategy with Alpaca daily bars.")
    parser.add_argument("--start", help="Backtest start date, YYYY-MM-DD.")
    parser.add_argument("--end", help="Backtest end date, YYYY-MM-DD.")
    parser.add_argument("--watchlist", help="Comma-separated symbols.")
    parser.add_argument("--data-feed", help="Alpaca data feed, e.g. iex or sip.")
    parser.add_argument("--initial-equity", help="Starting equity.")
    parser.add_argument("--risk-fraction", help="Risk per trade.")
    parser.add_argument("--reward-risk-ratio", help="Take-profit R multiple.")
    parser.add_argument("--stop-loss-pct", help="Stop distance from entry.")
    parser.add_argument("--swing-n", help="Bars on each side to confirm a swing point. Default 2.")
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of the text report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_dow_settings(args)
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
