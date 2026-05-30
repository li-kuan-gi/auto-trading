"""
Alpaca + FMP swing trader guard.

Design goals:
- GitHub Actions may run every hour, but this script must NOT submit an order every hour.
- At most one account-level position.
- At most one pending/open order lifecycle.
- Entry order is a bracket order: entry + take-profit + stop-loss.
- FMP economic calendar is NOT required; current version uses FMP earnings calendar and optional manual blackout CSV.
- Safe by default: ENABLE_TRADING=false and STRATEGY=disabled.
"""

from __future__ import annotations

import csv
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


TRADING_BASE_PAPER = "https://paper-api.alpaca.markets"
TRADING_BASE_LIVE = "https://api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
FMP_BASE = "https://financialmodelingprep.com/stable"


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    return Decimal(raw)


def parse_watchlist(raw: str) -> list[str]:
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        raise ValueError("WATCHLIST is empty")
    return symbols


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    fmp_api_key: str

    paper: bool
    enable_trading: bool

    watchlist: list[str]
    strategy: str
    symbol_selection_method: str
    data_feed: str

    risk_fraction: Decimal
    reward_risk_ratio: Decimal
    stop_loss_pct: Decimal
    allow_fractional: bool
    max_position_count: int

    earnings_block_days_before: int
    earnings_block_days_after: int
    manual_blackout_file: Path

    sma_fast: int
    sma_slow: int
    historical_lookback_days: int

    log_level: str

    @property
    def trading_base(self) -> str:
        return TRADING_BASE_PAPER if self.paper else TRADING_BASE_LIVE


def load_settings() -> Settings:
    if load_dotenv is not None:
        load_dotenv()

    s = Settings(
        alpaca_api_key=env_str("ALPACA_API_KEY"),
        alpaca_secret_key=env_str("ALPACA_SECRET_KEY"),
        fmp_api_key=env_str("FMP_API_KEY"),
        paper=env_bool("PAPER", True),
        enable_trading=env_bool("ENABLE_TRADING", False),
        watchlist=parse_watchlist(env_str("WATCHLIST", "SPY")),
        strategy=env_str("STRATEGY", "disabled").lower(),
        symbol_selection_method=env_str("SYMBOL_SELECTION_METHOD", "best_signal").lower(),
        data_feed=env_str("DATA_FEED", "iex").lower(),
        risk_fraction=env_decimal("RISK_FRACTION", "0.01"),
        reward_risk_ratio=env_decimal("REWARD_RISK_RATIO", "2.0"),
        stop_loss_pct=env_decimal("STOP_LOSS_PCT", "0.05"),
        allow_fractional=env_bool("ALLOW_FRACTIONAL", False),
        max_position_count=env_int("MAX_POSITION_COUNT", 1),
        earnings_block_days_before=env_int("EARNINGS_BLOCK_DAYS_BEFORE", 1),
        earnings_block_days_after=env_int("EARNINGS_BLOCK_DAYS_AFTER", 1),
        manual_blackout_file=Path(env_str("MANUAL_BLACKOUT_FILE", "config/manual_blackout_events.csv")),
        sma_fast=env_int("SMA_FAST", 20),
        sma_slow=env_int("SMA_SLOW", 50),
        historical_lookback_days=env_int("HISTORICAL_LOOKBACK_DAYS", 180),
        log_level=env_str("LOG_LEVEL", "INFO").upper(),
    )

    missing = []
    if not s.alpaca_api_key:
        missing.append("ALPACA_API_KEY")
    if not s.alpaca_secret_key:
        missing.append("ALPACA_SECRET_KEY")
    if not s.fmp_api_key:
        missing.append("FMP_API_KEY")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    if s.strategy not in {"disabled", "manual_once", "sma_trend"}:
        raise ValueError("STRATEGY must be one of: disabled, manual_once, sma_trend")
    if s.symbol_selection_method not in {"best_signal", "first_signal"}:
        raise ValueError("SYMBOL_SELECTION_METHOD must be one of: best_signal, first_signal")
    if s.risk_fraction <= 0 or s.risk_fraction > Decimal("0.10"):
        raise ValueError("RISK_FRACTION must be > 0 and <= 0.10")
    if s.reward_risk_ratio <= 0:
        raise ValueError("REWARD_RISK_RATIO must be > 0")
    if s.stop_loss_pct <= 0 or s.stop_loss_pct >= Decimal("0.50"):
        raise ValueError("STOP_LOSS_PCT must be > 0 and < 0.50")
    if s.sma_fast <= 1 or s.sma_slow <= 1 or s.sma_fast >= s.sma_slow:
        raise ValueError("SMA_FAST must be > 1 and < SMA_SLOW")
    if s.max_position_count != 1:
        raise ValueError("This version intentionally supports MAX_POSITION_COUNT=1 only")

    return s


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)sZ %(levelname)s %(message)s",
    )
    logging.Formatter.converter = __import__("time").gmtime


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_utc(value: str) -> datetime:
    value = value.strip()
    if not value:
        raise ValueError("empty datetime")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"datetime must contain timezone or Z suffix: {value}")
    return dt.astimezone(timezone.utc)


def d(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Cannot parse Decimal from {value!r}") from exc


def decimal_to_str(value: Decimal, places: str = "0.01") -> str:
    q = Decimal(places)
    return str(value.quantize(q, rounding=ROUND_HALF_UP))


def round_price(value: Decimal) -> Decimal:
    if value >= Decimal("1"):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def round_qty(value: Decimal, allow_fractional: bool) -> Decimal:
    if allow_fractional:
        return value.quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    return value.quantize(Decimal("1"), rounding=ROUND_DOWN)


def json_log(label: str, payload: Any) -> None:
    logging.info("%s %s", label, json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True))


class _HttpError(RuntimeError):
    """RuntimeError subclass that carries the HTTP status code as a structured attribute."""
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class AlpacaRestClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        timeout: int = 30,
    ) -> Any:
        url = f"{base}{path}"
        resp = self.session.request(method, url, params=params, json=json_body, timeout=timeout)
        if resp.status_code >= 400:
            body = resp.text[:1000]
            raise _HttpError(resp.status_code, f"{method} {url} failed: HTTP {resp.status_code}: {body}")
        if not resp.text:
            return None
        return resp.json()

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", self.settings.trading_base, "/v2/account") or {}

    def get_clock(self) -> dict[str, Any]:
        return self._request("GET", self.settings.trading_base, "/v2/clock") or {}

    def get_positions(self) -> list[dict[str, Any]]:
        data = self._request("GET", self.settings.trading_base, "/v2/positions")
        return data if isinstance(data, list) else []

    def get_open_orders(self) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            self.settings.trading_base,
            "/v2/orders",
            params={"status": "open", "nested": "true", "limit": 100},
        )
        return data if isinstance(data, list) else []

    def get_asset(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", self.settings.trading_base, f"/v2/assets/{symbol}") or {}

    def submit_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self.settings.trading_base, "/v2/orders", json_body=order_payload) or {}

    def get_historical_daily_bars(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        bars: list[dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            params = {
                "symbols": symbol,
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": "all",
                "feed": self.settings.data_feed,
                "limit": 1000,
            }
            if page_token:
                params["page_token"] = page_token

            data = self._request("GET", DATA_BASE, "/v2/stocks/bars", params=params)
            if data is None:
                return bars

            bars_by_symbol = data.get("bars", {})
            if isinstance(bars_by_symbol, dict):
                bars.extend(bars_by_symbol.get(symbol, []) or [])

            page_token = data.get("next_page_token")
            if not page_token:
                return bars

    def get_latest_quote(self, symbol: str) -> Optional[dict[str, Any]]:
        data = self._request(
            "GET",
            DATA_BASE,
            "/v2/stocks/quotes/latest",
            params={"symbols": symbol, "feed": self.settings.data_feed},
        )
        if data is None:
            return None
        quotes = data.get("quotes", {})
        return quotes.get(symbol)

    def get_latest_bar(self, symbol: str) -> Optional[dict[str, Any]]:
        data = self._request(
            "GET",
            DATA_BASE,
            "/v2/stocks/bars/latest",
            params={"symbols": symbol, "feed": self.settings.data_feed},
        )
        if data is None:
            return None
        bars = data.get("bars", {})
        return bars.get(symbol)

    def close_position(self, symbol: str, *, cancel_orders: bool = False) -> Optional[dict[str, Any]]:
        """Close (liquidate) the position for *symbol* via a market order.

        When cancel_orders=True, Alpaca atomically cancels any associated open orders
        (bracket TP/SL legs) server-side before closing, eliminating the race window
        that would exist if orders were cancelled in a separate prior API call.
        """
        params: dict[str, Any] = {"cancel_orders": "true"} if cancel_orders else None
        return self._request("DELETE", self.settings.trading_base, f"/v2/positions/{symbol}", params=params)


class FmpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()

    def get_earnings_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        url = f"{FMP_BASE}/earnings-calendar"
        params = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "apikey": self.settings.fmp_api_key,
        }
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code in {401, 403}:
            raise _HttpError(resp.status_code, f"FMP earnings-calendar auth failed: HTTP {resp.status_code}: {resp.text[:500]}")
        if resp.status_code in {402, 404}:
            logging.warning("FMP earnings-calendar unavailable: HTTP %s: %s", resp.status_code, resp.text[:500])
            return []
        if resp.status_code >= 400:
            raise RuntimeError(f"FMP earnings-calendar failed: HTTP {resp.status_code}: {resp.text[:1000]}")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            logging.warning("FMP earnings-calendar returned non-JSON body: %s", resp.text[:500])
            return []
        if not isinstance(data, list):
            logging.warning("FMP earnings-calendar returned unexpected payload: %s", str(data)[:500])
            return []
        return data


@dataclass(frozen=True)
class BlockReason:
    source: str
    reason: str
    start_utc: datetime
    end_utc: datetime
    symbols: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reason": self.reason,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "symbols": self.symbols,
        }


def load_manual_blackouts(path: Path, symbols: Iterable[str]) -> list[BlockReason]:
    if not path.exists():
        logging.info("Manual blackout file not found; skipping: %s", path)
        return []

    result: list[BlockReason] = []
    wanted = {s.upper() for s in symbols}

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader((row for row in f if not row.lstrip().startswith("#")))
        for row in reader:
            start_raw = (row.get("start_utc") or "").strip()
            end_raw = (row.get("end_utc") or "").strip()
            reason = (row.get("reason") or "manual blackout").strip()
            symbols_raw = (row.get("symbols") or "*").strip()

            if not start_raw or not end_raw:
                continue

            event_symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
            if not event_symbols:
                event_symbols = ["*"]

            if "*" not in event_symbols and wanted.isdisjoint(event_symbols):
                continue

            result.append(BlockReason(
                source="manual_csv",
                reason=reason,
                start_utc=parse_iso_utc(start_raw),
                end_utc=parse_iso_utc(end_raw),
                symbols=event_symbols,
            ))
    return result


def earnings_events_to_blackouts(
    earnings_events: list[dict[str, Any]],
    symbols: Iterable[str],
    before_days: int,
    after_days: int,
) -> list[BlockReason]:
    if ZoneInfo is None:
        ny_tz = timezone.utc
    else:
        ny_tz = ZoneInfo("America/New_York")

    wanted = {s.upper() for s in symbols}
    blackouts: list[BlockReason] = []

    for ev in earnings_events:
        symbol = str(ev.get("symbol", "")).upper().strip()
        if symbol not in wanted:
            continue

        date_raw = str(ev.get("date", "")).strip()
        if not date_raw:
            continue

        try:
            event_date = datetime.fromisoformat(date_raw[:10]).date()
        except ValueError:
            logging.warning("Skipping earnings event with bad date: %s", ev)
            continue

        start_local = datetime.combine(
            event_date - timedelta(days=before_days),
            datetime.min.time(),
            tzinfo=ny_tz,
        )
        end_local = datetime.combine(
            event_date + timedelta(days=after_days + 1),
            datetime.min.time(),
            tzinfo=ny_tz,
        )

        blackouts.append(BlockReason(
            source="fmp_earnings_calendar",
            reason=f"{symbol} earnings date {event_date.isoformat()}",
            start_utc=start_local.astimezone(timezone.utc),
            end_utc=end_local.astimezone(timezone.utc),
            symbols=[symbol],
        ))

    return blackouts


def find_active_block(now: datetime, symbol: str, blocks: list[BlockReason]) -> Optional[BlockReason]:
    symbol = symbol.upper()
    for block in blocks:
        applies = "*" in block.symbols or symbol in block.symbols
        if applies and block.start_utc <= now < block.end_utc:
            return block
    return None


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    side: str
    reason: str
    reference_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    selection_score: Decimal
    selection_details: dict[str, Any]


def compute_sma(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values for SMA")
    return sum(values[-period:]) / Decimal(period)


def get_mid_or_last_price(client: AlpacaRestClient, symbol: str) -> Decimal:
    quote = client.get_latest_quote(symbol)
    if quote:
        bid_raw = quote.get("bp")
        ask_raw = quote.get("ap")
        try:
            bid = d(bid_raw)
            ask = d(ask_raw)
            if bid > 0 and ask > 0 and ask >= bid:
                return (bid + ask) / Decimal("2")
            if ask > 0:
                return ask
            if bid > 0:
                return bid
        except Exception:
            logging.warning("Cannot parse latest quote for %s: %s", symbol, quote)

    bar = client.get_latest_bar(symbol)
    if bar:
        c = d(bar.get("c"))
        if c > 0:
            return c

    raise RuntimeError(f"Cannot obtain latest price for {symbol}")


def build_signal(settings: Settings, client: AlpacaRestClient, symbol: str) -> Optional[TradeSignal]:
    strategy = settings.strategy

    if strategy == "disabled":
        return None

    if strategy == "manual_once":
        ref = get_mid_or_last_price(client, symbol)
        stop = round_price(ref * (Decimal("1") - settings.stop_loss_pct))
        take = round_price(ref + (ref - stop) * settings.reward_risk_ratio)
        return TradeSignal(
            symbol=symbol,
            side="buy",
            reason="manual_once test signal",
            reference_price=round_price(ref),
            stop_price=stop,
            take_profit_price=take,
            selection_score=Decimal("0"),
            selection_details={"strategy": "manual_once"},
        )

    if strategy == "sma_trend":
        end = now_utc().date()
        start = end - timedelta(days=settings.historical_lookback_days)
        bars = client.get_historical_daily_bars(symbol, start, end)
        if len(bars) < settings.sma_slow + 2:
            logging.info("NO_SIGNAL not enough bars for %s: got=%s need=%s", symbol, len(bars), settings.sma_slow + 2)
            return None

        closes = [d(b["c"]) for b in bars if "c" in b]
        if len(closes) < settings.sma_slow + 2:
            logging.info("NO_SIGNAL not enough close values for %s", symbol)
            return None

        latest_close = closes[-1]
        prev_close = closes[-2]
        latest_fast = compute_sma(closes, settings.sma_fast)
        latest_slow = compute_sma(closes, settings.sma_slow)
        prev_fast = compute_sma(closes[:-1], settings.sma_fast)

        signal = latest_fast > latest_slow and latest_close > latest_fast and prev_close <= prev_fast
        trend_strength = (latest_fast / latest_slow) - Decimal("1")
        breakout_strength = (latest_close / latest_fast) - Decimal("1")
        selection_score = trend_strength + breakout_strength

        json_log("SMA_STATUS", {
            "symbol": symbol,
            "latest_close": latest_close,
            "prev_close": prev_close,
            "latest_fast": latest_fast,
            "latest_slow": latest_slow,
            "prev_fast": prev_fast,
            "trend_strength": trend_strength,
            "breakout_strength": breakout_strength,
            "selection_score": selection_score,
            "signal": signal,
        })

        if not signal:
            return None

        ref = get_mid_or_last_price(client, symbol)
        stop = round_price(ref * (Decimal("1") - settings.stop_loss_pct))
        take = round_price(ref + (ref - stop) * settings.reward_risk_ratio)
        return TradeSignal(
            symbol=symbol,
            side="buy",
            reason=f"sma_trend fast={settings.sma_fast} slow={settings.sma_slow}",
            reference_price=round_price(ref),
            stop_price=stop,
            take_profit_price=take,
            selection_score=selection_score,
            selection_details={
                "strategy": "sma_trend",
                "latest_close": latest_close,
                "latest_fast": latest_fast,
                "latest_slow": latest_slow,
                "trend_strength": trend_strength,
                "breakout_strength": breakout_strength,
            },
        )

    raise ValueError(f"Unsupported strategy: {strategy}")


@dataclass(frozen=True)
class ExitSignal:
    symbol: str
    reason: str


def build_exit_signal(settings: Settings, client: AlpacaRestClient, symbol: str) -> Optional[ExitSignal]:
    """Return an ExitSignal when the strategy says the existing position should be closed.

    This is separate from stop-loss / take-profit (those are handled by Alpaca bracket legs).
    This fires on trend-reversal logic so the position can be exited *before* SL/TP is hit.
    """
    strategy = settings.strategy

    if strategy in {"disabled", "manual_once"}:
        # manual_once has no reversal concept; let the bracket order manage exit.
        return None

    if strategy == "sma_trend":
        # Use end = yesterday so closes[-1] is a confirmed daily close, not today's
        # partial in-progress bar whose 'c' field is the last traded price mid-session.
        end = now_utc().date() - timedelta(days=1)
        start = end - timedelta(days=settings.historical_lookback_days)
        bars = client.get_historical_daily_bars(symbol, start, end)
        if len(bars) < settings.sma_slow + 2:
            logging.info("NO_EXIT_SIGNAL not enough bars for %s: got=%s need=%s", symbol, len(bars), settings.sma_slow + 2)
            return None

        closes = [d(b["c"]) for b in bars if "c" in b]
        if len(closes) < settings.sma_slow + 2:
            logging.info("NO_EXIT_SIGNAL not enough close values for %s", symbol)
            return None

        latest_close = closes[-1]
        latest_fast = compute_sma(closes, settings.sma_fast)
        latest_slow = compute_sma(closes, settings.sma_slow)

        death_cross = latest_fast < latest_slow
        price_below_slow = latest_close < latest_slow

        json_log("SMA_EXIT_STATUS", {
            "symbol": symbol,
            "latest_close": latest_close,
            "latest_fast": latest_fast,
            "latest_slow": latest_slow,
            "death_cross": death_cross,
            "price_below_slow": price_below_slow,
        })

        if death_cross or price_below_slow:
            reasons: list[str] = []
            if death_cross:
                reasons.append(f"fast_sma({settings.sma_fast}) < slow_sma({settings.sma_slow}) (death cross)")
            if price_below_slow:
                reasons.append(f"close({latest_close}) < slow_sma({settings.sma_slow})({latest_slow})")
            return ExitSignal(symbol=symbol, reason=" AND ".join(reasons))

        return None

    raise ValueError(f"Unsupported strategy: {strategy}")


def calculate_qty(account: dict[str, Any], signal: TradeSignal, settings: Settings) -> Decimal:
    equity = d(account.get("equity"))
    if equity <= 0:
        raise RuntimeError(f"Invalid account equity: {equity}")

    if signal.side != "buy":
        raise ValueError("Current version only supports buy/long signals")

    per_share_risk = signal.reference_price - signal.stop_price
    if per_share_risk <= 0:
        raise RuntimeError(f"Invalid stop for long trade: reference={signal.reference_price} stop={signal.stop_price}")

    max_loss = equity * settings.risk_fraction
    raw_qty = max_loss / per_share_risk
    qty = round_qty(raw_qty, settings.allow_fractional)

    if qty <= 0:
        raise RuntimeError("Calculated qty is 0. Increase account size, risk fraction, or use ALLOW_FRACTIONAL=true.")

    return qty


def build_bracket_order(settings: Settings, account: dict[str, Any], signal: TradeSignal) -> dict[str, Any]:
    qty = calculate_qty(account, signal, settings)
    stamp = now_utc().strftime("%Y%m%d%H")
    client_order_id = f"swing-{signal.symbol.lower()}-{signal.side}-{stamp}"

    return {
        "symbol": signal.symbol,
        "qty": str(qty.normalize()) if settings.allow_fractional else str(int(qty)),
        "side": signal.side,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": decimal_to_str(signal.take_profit_price)},
        "stop_loss": {"stop_price": decimal_to_str(signal.stop_price)},
        "client_order_id": client_order_id,
    }


def ensure_asset_tradeable(client: AlpacaRestClient, symbol: str) -> bool:
    asset = client.get_asset(symbol)
    json_log("ASSET", {
        "symbol": symbol,
        "asset_class": asset.get("class"),
        "exchange": asset.get("exchange"),
        "status": asset.get("status"),
        "tradable": asset.get("tradable"),
        "shortable": asset.get("shortable"),
        "fractionable": asset.get("fractionable"),
    })
    return bool(asset.get("tradable")) and asset.get("status") == "active"


def select_trade_signal(
    settings: Settings,
    client: AlpacaRestClient,
    current_time: datetime,
    blocks: list[BlockReason],
) -> Optional[TradeSignal]:
    json_log("SYMBOL_SELECTION", {
        "method": settings.symbol_selection_method,
        "watchlist": settings.watchlist,
        "strategy": settings.strategy,
    })

    candidates: list[TradeSignal] = []

    for symbol in settings.watchlist:
        if not ensure_asset_tradeable(client, symbol):
            logging.info("SKIP_SYMBOL not tradable: %s", symbol)
            continue

        active_block = find_active_block(current_time, symbol, blocks)
        if active_block:
            json_log("NO_TRADE blocked_by_event", {"symbol": symbol, **active_block.to_dict()})
            continue

        signal = build_signal(settings, client, symbol)
        if signal is None:
            logging.info("NO_SIGNAL symbol=%s strategy=%s", symbol, settings.strategy)
            continue

        json_log("SYMBOL_CANDIDATE", dataclasses.asdict(signal))
        candidates.append(signal)

        if settings.symbol_selection_method == "first_signal":
            json_log("SELECTED_SYMBOL", {
                "symbol": signal.symbol,
                "reason": signal.reason,
                "selection_method": settings.symbol_selection_method,
                "selection_score": signal.selection_score,
            })
            return signal

    if not candidates:
        return None

    best = max(candidates, key=lambda s: s.selection_score)
    if settings.symbol_selection_method == "best_signal" and settings.strategy == "manual_once":
        logging.info("SYMBOL_SELECTION manual_once has no ranking signal; selection follows WATCHLIST order")
        best = candidates[0]

    json_log("SYMBOL_RANKING", [
        {
            "symbol": candidate.symbol,
            "selection_score": candidate.selection_score,
            "reason": candidate.reason,
        }
        for candidate in sorted(candidates, key=lambda s: s.selection_score, reverse=True)
    ])

    json_log("SELECTED_SYMBOL", {
        "symbol": best.symbol,
        "reason": best.reason,
        "selection_method": settings.symbol_selection_method,
        "selection_score": best.selection_score,
    })
    return best


def summarize_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for o in orders:
        summary.append({
            "id": o.get("id"),
            "client_order_id": o.get("client_order_id"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "type": o.get("type"),
            "order_class": o.get("order_class"),
            "status": o.get("status"),
            "qty": o.get("qty"),
            "filled_qty": o.get("filled_qty"),
            "legs": [
                {
                    "id": leg.get("id"),
                    "symbol": leg.get("symbol"),
                    "side": leg.get("side"),
                    "type": leg.get("type"),
                    "status": leg.get("status"),
                    "qty": leg.get("qty"),
                    "filled_qty": leg.get("filled_qty"),
                }
                for leg in (o.get("legs") or [])
            ],
        })
    return summary


def run() -> int:
    settings = load_settings()
    setup_logging(settings.log_level)

    logging.info(
        "START paper=%s enable_trading=%s strategy=%s symbol_selection_method=%s watchlist=%s feed=%s",
        settings.paper,
        settings.enable_trading,
        settings.strategy,
        settings.symbol_selection_method,
        settings.watchlist,
        settings.data_feed,
    )

    alpaca = AlpacaRestClient(settings)
    fmp = FmpClient(settings)

    current_time = now_utc()
    account = alpaca.get_account()
    clock = alpaca.get_clock()
    positions = alpaca.get_positions()
    open_orders = alpaca.get_open_orders()

    json_log("ACCOUNT", {
        "status": account.get("status"),
        "equity": account.get("equity"),
        "cash": account.get("cash"),
        "buying_power": account.get("buying_power"),
        "trading_blocked": account.get("trading_blocked"),
        "account_blocked": account.get("account_blocked"),
    })
    json_log("CLOCK", clock)
    json_log("POSITIONS", positions)
    json_log("OPEN_ORDERS", summarize_orders(open_orders))

    if account.get("trading_blocked") or account.get("account_blocked"):
        logging.warning("NO_TRADE account is blocked")
        return 0

    if not clock.get("is_open"):
        logging.info("NO_TRADE market is closed")
        return 0

    # --- Exit-signal check: runs even if bracket legs are still open ---
    if positions:
        for pos in positions:
            pos_symbol = str(pos.get("symbol", "")).upper()
            if not pos_symbol.strip():
                # Guard: Alpaca position missing symbol — skip to avoid hitting the
                # close-all-positions endpoint (DELETE /v2/positions/ with empty path).
                logging.warning("SKIP_POSITION position object has no symbol field: %s", pos)
                continue

            try:
                exit_sig = build_exit_signal(settings, alpaca, pos_symbol)
            except _HttpError as exc:
                if exc.status_code in (401, 403):
                    raise  # auth failure — hard stop, do not silently skip
                logging.warning("EXIT_SIGNAL_ERROR symbol=%s error=%s; skipping exit check", pos_symbol, exc)
                continue
            except (requests.exceptions.RequestException, ValueError) as exc:
                logging.warning("EXIT_SIGNAL_ERROR symbol=%s error=%s; skipping exit check", pos_symbol, exc)
                continue
            if exit_sig is not None:
                json_log("EXIT_SIGNAL", {"symbol": pos_symbol, "reason": exit_sig.reason})
                if settings.enable_trading:
                    try:
                        # cancel_orders=True makes Alpaca atomically cancel the bracket
                        # TP/SL legs and close the position in one server-side operation,
                        # eliminating the race window of a separate cancel-then-close.
                        closed = alpaca.close_position(pos_symbol, cancel_orders=True)
                        json_log("POSITION_CLOSED", closed if closed is not None else {"symbol": pos_symbol})
                    except _HttpError as exc:
                        if exc.status_code in (401, 403):
                            raise  # auth failure — hard stop
                        if exc.status_code in (404, 422):
                            # 404 = position already closed by a bracket fill that beat us;
                            # 422 = position already in the process of closing.
                            logging.warning(
                                "CLOSE_POSITION_FAILED symbol=%s error=%s; "
                                "position likely already closed by bracket order",
                                pos_symbol, exc,
                            )
                        else:
                            # 429 / 5xx = transient — log and continue, retry next run
                            logging.warning(
                                "CLOSE_POSITION_FAILED symbol=%s error=%s; "
                                "position left open, will retry next run",
                                pos_symbol, exc,
                            )
                    except requests.exceptions.RequestException as exc:
                        logging.warning("CLOSE_POSITION_NETWORK_ERROR symbol=%s error=%s; position left open, will retry next run", pos_symbol, exc)
                else:
                    logging.info(
                        "DRY_RUN exit signal fired for %s but enable_trading=false; position NOT closed",
                        pos_symbol,
                    )
            else:
                logging.info(
                    "HOLD position=%s no exit signal; bracket order continues to manage SL/TP",
                    pos_symbol,
                )
        logging.info("NO_NEW_ENTRY already has %d position(s)", len(positions))
        return 0
    # --- End exit-signal check ---

    if open_orders:
        logging.info("NO_TRADE already has open order(s)")
        return 0

    query_start = current_time.date() - timedelta(days=max(settings.earnings_block_days_before, 1))
    query_end = current_time.date() + timedelta(days=max(settings.earnings_block_days_after, 7))
    try:
        earnings = fmp.get_earnings_calendar(query_start, query_end)
    except _HttpError as exc:
        if exc.status_code in (401, 403):
            raise  # auth failure — propagate to __main__ for exit 1
        logging.error("EARNINGS_CALENDAR_FAILED error=%s; halting this run to preserve earnings blackout", exc)
        return 0
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        logging.error("EARNINGS_CALENDAR_FAILED error=%s; halting this run to preserve earnings blackout", exc)
        return 0
    earnings_blocks = earnings_events_to_blackouts(
        earnings,
        settings.watchlist,
        settings.earnings_block_days_before,
        settings.earnings_block_days_after,
    )
    manual_blocks = load_manual_blackouts(settings.manual_blackout_file, settings.watchlist)
    blocks = earnings_blocks + manual_blocks

    if blocks:
        json_log("BLACKOUT_WINDOWS", [b.to_dict() for b in blocks])

    signal = select_trade_signal(settings, alpaca, current_time, blocks)
    if signal is None:
        logging.info("END no eligible trade")
        return 0

    order_payload = build_bracket_order(settings, account, signal)
    json_log("SIGNAL", dataclasses.asdict(signal))
    json_log("ORDER_PAYLOAD", order_payload)

    if not settings.enable_trading:
        logging.info("DRY_RUN enable_trading=false; order was NOT submitted")
        return 0

    submitted = alpaca.submit_order(order_payload)
    json_log("ORDER_SUBMITTED", submitted)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except Exception as exc:
        logging.error("FATAL %s: %s", type(exc).__name__, exc)
        raise SystemExit(1) from exc
