"""Position tracking and risk limits."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time

from .config import Config


@dataclass
class Position:
    symbol: str
    side: str  # "BUY" (long) or "SELL" (short)
    qty: int
    entry: float
    stop: float
    target: float
    opened_at: datetime

    def pnl(self, price: float) -> float:
        sign = 1 if self.side == "BUY" else -1
        return sign * (price - self.entry) * self.qty

    def exit_reason(self, price: float) -> str | None:
        if self.side == "BUY":
            if price <= self.stop:
                return "stop-loss"
            if price >= self.target:
                return "target"
        else:
            if price >= self.stop:
                return "stop-loss"
            if price <= self.target:
                return "target"
        return None


class RiskManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.day: date | None = None
        self.trades_today = 0
        self.realized_pnl = 0.0
        self.halted = False

    def roll_day(self, today: date) -> None:
        if self.day != today:
            self.day, self.trades_today, self.realized_pnl, self.halted = today, 0, 0.0, False

    def quantity(self, price: float) -> int:
        return max(0, math.floor(self.cfg.capital_per_trade / price)) if price > 0 else 0

    def brackets(self, side: str, price: float) -> tuple[float, float]:
        sl, tg = self.cfg.stop_loss_pct / 100, self.cfg.target_pct / 100
        if side == "BUY":
            return round(price * (1 - sl), 2), round(price * (1 + tg), 2)
        return round(price * (1 + sl), 2), round(price * (1 - tg), 2)

    def in_entry_window(self, now: time) -> bool:
        return self.cfg.entry_start <= now <= self.cfg.entry_end

    def must_square_off(self, now: time) -> bool:
        return now >= self.cfg.square_off

    def can_open(self, open_positions: int, unrealized: float = 0.0) -> tuple[bool, str]:
        if self.halted:
            return False, "halted: daily loss limit hit"
        if self.realized_pnl + unrealized <= -abs(self.cfg.max_daily_loss):
            self.halted = True
            return False, "daily loss limit hit"
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False, "max trades per day reached"
        if open_positions >= self.cfg.max_open_positions:
            return False, "max open positions reached"
        return True, "ok"

    def record_entry(self) -> None:
        self.trades_today += 1

    def record_exit(self, pnl: float) -> None:
        self.realized_pnl += pnl
        if self.realized_pnl <= -abs(self.cfg.max_daily_loss):
            self.halted = True
