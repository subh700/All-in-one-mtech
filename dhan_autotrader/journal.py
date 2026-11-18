"""Trade journal (CSV) and same-day state persistence for crash recovery."""
from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import asdict
from datetime import date, datetime

from .risk import Position

log = logging.getLogger(__name__)

JOURNAL_FIELDS = ["date", "mode", "strategy", "symbol", "side", "qty", "entry", "exit", "pnl", "charges",
                  "net_pnl", "reason", "opened_at", "closed_at"]


class TradeJournal:
    """Appends one row per closed trade to a CSV file (opens fine in Excel / Google Sheets)."""

    def __init__(self, path: str = "logs/trades.csv", mode: str = "paper", strategy: str = ""):
        self.path, self.mode, self.strategy = path, mode, strategy

    def _rotate_if_old_format(self) -> None:
        try:
            with open(self.path, newline="") as f:
                header = next(csv.reader(f), [])
        except OSError:
            return
        if header and header != JOURNAL_FIELDS:
            os.replace(self.path, self.path.replace(".csv", "") + f"_old_{datetime.now():%Y%m%d%H%M%S}.csv")

    def record(self, pos: Position, exit_price: float, pnl: float, reason: str, closed_at: datetime,
               charges: float = 0.0) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path):
                self._rotate_if_old_format()
            new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
            with open(self.path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
                if new:
                    w.writeheader()
                w.writerow({
                    "date": closed_at.date().isoformat(), "mode": self.mode, "strategy": self.strategy,
                    "symbol": pos.symbol, "side": pos.side, "qty": pos.qty, "entry": round(pos.entry, 2),
                    "exit": round(exit_price, 2), "pnl": round(pnl, 2), "charges": round(charges, 2),
                    "net_pnl": round(pnl - charges, 2), "reason": reason,
                    "opened_at": pos.opened_at.isoformat(timespec="seconds"),
                    "closed_at": closed_at.isoformat(timespec="seconds"),
                })
        except OSError as exc:  # never let bookkeeping break trading
            log.warning("Could not write trade journal: %s", exc)


class NullJournal(TradeJournal):
    def __init__(self):
        super().__init__(path="")

    def record(self, *a, **kw) -> None:
        pass


def position_to_dict(p: Position) -> dict:
    d = asdict(p)
    d["opened_at"] = p.opened_at.isoformat()
    d["target"] = None if p.target == float("inf") else p.target
    return d


def position_from_dict(d: dict) -> Position:
    d = dict(d)
    d["opened_at"] = datetime.fromisoformat(d["opened_at"])
    d["target"] = float("inf") if d.get("target") is None else d["target"]
    return Position(**d)


class StateStore:
    """Saves open positions + daily risk counters so a restart the same day resumes cleanly."""

    def __init__(self, path: str):
        self.path = path

    def save(self, today: date, payload: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"date": today.isoformat(), **payload}, f, indent=2)
            os.replace(tmp, self.path)  # atomic
        except OSError as exc:
            log.warning("Could not save state: %s", exc)

    def load(self, today: date) -> dict | None:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if data.get("date") != today.isoformat():
            return None  # yesterday's intraday state is meaningless today
        return data


class NullStore(StateStore):
    def __init__(self):
        super().__init__("")

    def save(self, *a, **kw) -> None:
        pass

    def load(self, today):
        return None


def risk_to_dict(risk) -> dict:
    return {"trades_today": risk.trades_today, "realized_pnl": risk.realized_pnl, "halted": risk.halted,
            "halt_reason": risk.halt_reason, "paused": risk.paused, "consecutive_losses": risk.consecutive_losses,
            "cooldown_until": risk.cooldown_until.isoformat() if risk.cooldown_until else None}


def risk_from_dict(risk, d: dict, today: date) -> None:
    risk.roll_day(today)
    risk.trades_today = int(d.get("trades_today", 0))
    risk.realized_pnl = float(d.get("realized_pnl", 0.0))
    risk.halted = bool(d.get("halted", False))
    risk.halt_reason = d.get("halt_reason", "") or ("restored" if risk.halted else "")
    risk.paused = bool(d.get("paused", False))
    risk.consecutive_losses = int(d.get("consecutive_losses", 0))
    cu = d.get("cooldown_until")
    risk.cooldown_until = datetime.fromisoformat(cu) if cu else None
