"""Position tracking and risk limits."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

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
    peak: float = 0.0          # best price seen since entry (for trailing)
    trailing: bool = False     # True once the trailing stop has activated

    def __post_init__(self):
        if not self.peak:
            self.peak = self.entry

    def update_trail(self, price: float, trigger_pct: float, trail_pct: float) -> bool:
        """Ratchet the stop in the trade's favour. Returns True the first time trailing activates.

        Once the best price has moved `trigger_pct`% in our favour, the stop goes to at
        least breakeven, then follows the best price at a distance of `trail_pct`%.
        """
        if trigger_pct <= 0:
            return False
        long = self.side == "BUY"
        self.peak = max(self.peak, price) if long else min(self.peak, price)
        gain_pct = (self.peak - self.entry) / self.entry * 100 * (1 if long else -1)
        if gain_pct < trigger_pct:
            return False
        if long:
            new_stop = max(self.entry, round(self.peak * (1 - trail_pct / 100), 2))
            if new_stop > self.stop:
                self.stop = new_stop
        else:
            new_stop = min(self.entry, round(self.peak * (1 + trail_pct / 100), 2))
            if new_stop < self.stop:
                self.stop = new_stop
        first = not self.trailing
        self.trailing = True
        return first

    def pnl(self, price: float) -> float:
        sign = 1 if self.side == "BUY" else -1
        return sign * (price - self.entry) * self.qty

    def exit_reason(self, price: float) -> str | None:
        if self.side == "BUY":
            if price <= self.stop:
                return "trailing-stop" if self.trailing else "stop-loss"
            if price >= self.target:
                return "target"
        else:
            if price >= self.stop:
                return "trailing-stop" if self.trailing else "stop-loss"
            if price <= self.target:
                return "target"
        return None


class RiskManager:
    """Daily risk state shared by everything the bot trades."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.day: date | None = None
        self.trades_today = 0
        self.realized_pnl = 0.0
        self.halted = False
        self.halt_reason = ""
        self.paused = False                 # set from Telegram /pause
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None
        self.on_halt = None                 # callback(reason) — used for alerts

    def roll_day(self, today: date) -> None:
        if self.day != today:
            self.day, self.trades_today, self.realized_pnl = today, 0, 0.0
            self.halted, self.halt_reason, self.consecutive_losses, self.cooldown_until = False, "", 0, None

    def halt(self, reason: str) -> bool:
        """Stop new entries for the rest of the day. Returns True if this call caused the halt."""
        if self.halted:
            return False
        self.halted, self.halt_reason = True, reason
        if self.on_halt:
            self.on_halt(reason)
        return True

    # ---- sizing --------------------------------------------------------
    def quantity(self, price: float) -> int:
        return max(0, math.floor(self.cfg.capital_per_trade / price)) if price > 0 else 0

    def cap_for_risk(self, qty: int, risk_per_unit: float, lot: int = 1) -> int:
        """Reduce qty (in multiples of `lot`) so qty x risk_per_unit <= MAX_RISK_PER_TRADE."""
        limit = getattr(self.cfg, "max_risk_per_trade", 0) or 0
        if limit <= 0 or risk_per_unit <= 0:
            return qty
        allowed = math.floor(limit / (risk_per_unit * lot)) * lot
        return min(qty, max(0, allowed))

    def brackets(self, side: str, price: float) -> tuple[float, float]:
        sl, tg = self.cfg.stop_loss_pct / 100, self.cfg.target_pct / 100
        if side == "BUY":
            return round(price * (1 - sl), 2), round(price * (1 + tg), 2)
        return round(price * (1 + sl), 2), round(price * (1 - tg), 2)

    # ---- timing --------------------------------------------------------
    def in_entry_window(self, now: time) -> bool:
        return self.cfg.entry_start <= now <= self.cfg.entry_end

    def must_square_off(self, now: time) -> bool:
        return now >= self.cfg.square_off

    # ---- gates ---------------------------------------------------------
    def can_open(self, open_positions: int, unrealized: float = 0.0,
                 now: datetime | None = None) -> tuple[bool, str]:
        if self.halted:
            return False, f"halted: {self.halt_reason}"
        if self.paused:
            return False, "paused (send /resume to continue)"
        if self.realized_pnl + unrealized <= -abs(self.cfg.max_daily_loss):
            self.halt("daily loss limit")
            return False, "daily loss limit hit"
        if self.cooldown_until and now and now < self.cooldown_until:
            return False, f"cooling down after a loss until {self.cooldown_until:%H:%M}"
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False, "max trades per day reached"
        if open_positions >= self.cfg.max_open_positions:
            return False, "max open positions reached"
        return True, "ok"

    def record_entry(self) -> None:
        self.trades_today += 1

    def record_exit(self, pnl: float, now: datetime | None = None) -> None:
        self.realized_pnl += pnl
        cfg = self.cfg
        if pnl < 0:
            self.consecutive_losses += 1
            mins = getattr(cfg, "cooldown_after_loss_min", 0) or 0
            if mins and now:
                self.cooldown_until = now + timedelta(minutes=mins)
        else:
            self.consecutive_losses = 0
        if self.realized_pnl <= -abs(cfg.max_daily_loss):
            self.halt("daily loss limit")
        target = getattr(cfg, "daily_profit_target", 0) or 0
        if target and self.realized_pnl >= target:
            self.halt(f"daily profit target ₹{target:,.0f} reached")
        max_losses = getattr(cfg, "max_consecutive_losses", 0) or 0
        if max_losses and self.consecutive_losses >= max_losses:
            self.halt(f"{self.consecutive_losses} losses in a row")
