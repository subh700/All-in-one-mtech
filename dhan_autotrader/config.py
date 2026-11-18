"""Configuration loaded from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional
    pass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    security_id: str
    exchange_segment: str = "NSE_EQ"

    @property
    def instrument_type(self) -> str:
        return "INDEX" if self.exchange_segment == "IDX_I" else "EQUITY"


def _parse_time(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_instruments(raw: str) -> list[Instrument]:
    out = []
    for item in filter(None, (s.strip() for s in raw.split(","))):
        parts = item.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"Bad instrument '{item}', expected SYMBOL:SECURITY_ID[:SEGMENT]")
        out.append(Instrument(parts[0].upper(), parts[1], parts[2].upper() if len(parts) == 3 else "NSE_EQ"))
    if not out:
        raise ValueError("INSTRUMENTS is empty")
    return out


@dataclass
class Config:
    client_id: str = ""
    access_token: str = ""
    mode: str = "paper"
    instruments: list[Instrument] = field(default_factory=list)

    signal: str = "ema"
    candle_interval: int = 5
    fast_ema: int = 9
    slow_ema: int = 21
    st_period: int = 10
    st_mult: float = 3.0
    rsi_period: int = 14
    rsi_buy: float = 60.0
    rsi_sell: float = 40.0
    orb_minutes: int = 15
    allow_short: bool = False

    capital_per_trade: float = 10000.0
    stop_loss_pct: float = 1.0
    target_pct: float = 2.0
    max_trades_per_day: int = 6
    max_daily_loss: float = 1000.0
    max_open_positions: int = 2
    trail_trigger_pct: float = 0.0   # 0 = trailing off (equity)
    trail_pct: float = 0.0

    telegram_token: str = ""
    telegram_chat_id: str = ""

    entry_start: time = time(9, 30)
    entry_end: time = time(14, 45)
    square_off: time = time(15, 15)
    poll_seconds: int = 20

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        cfg = cls(
            client_id=e("DHAN_CLIENT_ID", ""),
            access_token=e("DHAN_ACCESS_TOKEN", ""),
            mode=e("TRADING_MODE", "paper").strip().lower(),
            instruments=parse_instruments(e("INSTRUMENTS", "HDFCBANK:1333:NSE_EQ")),
            signal=e("SIGNAL", "ema").strip().lower(),
            candle_interval=int(e("CANDLE_INTERVAL_MIN", "5")),
            st_period=int(e("ST_PERIOD", "10")),
            st_mult=float(e("ST_MULT", "3")),
            rsi_period=int(e("RSI_PERIOD", "14")),
            rsi_buy=float(e("RSI_BUY", "60")),
            rsi_sell=float(e("RSI_SELL", "40")),
            orb_minutes=int(e("ORB_MINUTES", "15")),
            fast_ema=int(e("FAST_EMA", "9")),
            slow_ema=int(e("SLOW_EMA", "21")),
            allow_short=_bool(e("ALLOW_SHORT", "false")),
            capital_per_trade=float(e("CAPITAL_PER_TRADE", "10000")),
            stop_loss_pct=float(e("STOP_LOSS_PCT", "1.0")),
            target_pct=float(e("TARGET_PCT", "2.0")),
            max_trades_per_day=int(e("MAX_TRADES_PER_DAY", "6")),
            max_daily_loss=float(e("MAX_DAILY_LOSS", "1000")),
            max_open_positions=int(e("MAX_OPEN_POSITIONS", "2")),
            trail_trigger_pct=float(e("TRAIL_TRIGGER_PCT", "0")),
            trail_pct=float(e("TRAIL_PCT", "0")),
            telegram_token=e("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=e("TELEGRAM_CHAT_ID", "").strip(),
            entry_start=_parse_time(e("ENTRY_START", "09:30")),
            entry_end=_parse_time(e("ENTRY_END", "14:45")),
            square_off=_parse_time(e("SQUARE_OFF", "15:15")),
            poll_seconds=int(e("POLL_SECONDS", "20")),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in {"paper", "live"}:
            raise ValueError("TRADING_MODE must be 'paper' or 'live'")
        if self.candle_interval not in {1, 5, 15, 25, 60}:
            raise ValueError("CANDLE_INTERVAL_MIN must be one of 1, 5, 15, 25, 60")
        from .strategy import SIGNALS

        if self.signal not in SIGNALS:
            raise ValueError(f"SIGNAL must be one of {', '.join(SIGNALS)}")
        if self.signal == "orb" and self.orb_minutes % self.candle_interval:
            raise ValueError("ORB_MINUTES must be a multiple of CANDLE_INTERVAL_MIN")
        if self.rsi_sell >= self.rsi_buy:
            raise ValueError("RSI_SELL must be below RSI_BUY")
        if self.trail_trigger_pct < 0 or not (0 <= self.trail_pct < 100):
            raise ValueError("TRAIL_TRIGGER_PCT must be >= 0 and TRAIL_PCT in [0, 100)")
        if self.fast_ema >= self.slow_ema:
            raise ValueError("FAST_EMA must be smaller than SLOW_EMA")
        if not (self.entry_start < self.entry_end < self.square_off):
            raise ValueError("Require ENTRY_START < ENTRY_END < SQUARE_OFF")
        if self.stop_loss_pct <= 0 or self.target_pct <= 0:
            raise ValueError("STOP_LOSS_PCT and TARGET_PCT must be positive")
        if not self.client_id or not self.access_token:
            raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required (even paper mode reads live market data)")
