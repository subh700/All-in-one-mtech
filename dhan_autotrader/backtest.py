"""Backtest entry signals on Dhan historical intraday candles.

    python -m dhan_autotrader --strategy options --backtest 60 --signal all

How the simulation works (same rules the live bot uses):
  * signals are computed on CLOSED candles; entries/exits happen at the NEXT candle's open
  * entries only between ENTRY_START and ENTRY_END, everything squared off at SQUARE_OFF
  * stop-loss / target / trailing stop are checked against each candle's high & low
    (if both could have been hit in one candle, the stop is assumed first — conservative)
  * an opposite signal exits (and reverses if allowed); MAX_TRADES_PER_DAY applies

Options mode: historical premiums for expired strikes are not used. Trades are simulated
on the INDEX (CE = long index, PE = short index) and option P&L is ESTIMATED as
index points x delta x lot size. Treat it as a comparison of signals, not a promise.
"""
from __future__ import annotations

import csv
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .broker import IST, Candle
from .risk import Position
from .strategy import Signal, Strategy

log = logging.getLogger(__name__)


@dataclass
class BtTrade:
    symbol: str
    side: str            # BUY (long / CE) or SELL (short / PE)
    entry_ts: datetime
    entry: float
    exit_ts: datetime
    exit: float
    reason: str
    qty: float           # units used for P&L
    pnl: float

    @property
    def points(self) -> float:
        return (self.exit - self.entry) * (1 if self.side == "BUY" else -1)


@dataclass
class BtParams:
    entry_start: time = time(9, 30)
    entry_end: time = time(14, 45)
    square_off: time = time(15, 15)
    sl_pct: float = 1.0
    target_pct: float = 2.0          # 0 = no fixed target
    trail_trigger_pct: float = 0.0
    trail_pct: float = 0.0
    allow_short: bool = False
    max_trades_per_day: int = 6
    lookback: int = 400              # bars of history passed to the strategy (speed)


@dataclass
class BtResult:
    name: str
    trades: list[BtTrade] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def wins(self) -> list[BtTrade]:
        return [t for t in self.trades if t.pnl > 0]

    @property
    def win_rate(self) -> float:
        return 100 * len(self.wins) / len(self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return gross_win / gross_loss if gross_loss else (math.inf if gross_win else 0.0)

    @property
    def max_drawdown(self) -> float:
        peak = eq = dd = 0.0
        for t in self.trades:
            eq += t.pnl
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        return dd

    def row(self) -> list:
        pf = self.profit_factor
        avg_w = sum(t.pnl for t in self.wins) / len(self.wins) if self.wins else 0.0
        losses = [t for t in self.trades if t.pnl <= 0]
        avg_l = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
        return [self.name, len(self.trades), f"{self.win_rate:.0f}%", f"{self.total:,.0f}",
                f"{avg_w:,.0f}", f"{avg_l:,.0f}", "inf" if math.isinf(pf) else f"{pf:.2f}",
                f"{self.max_drawdown:,.0f}"]


HEADER = ["signal", "trades", "win%", "net P&L", "avg win", "avg loss", "profit factor", "max DD"]


def simulate(symbol: str, candles: list[Candle], strategy: Strategy, p: BtParams, qty_fn) -> list[BtTrade]:
    """Walk candles bar by bar. `qty_fn(entry_price) -> units` sizes each trade."""
    trades: list[BtTrade] = []
    pos: Position | None = None
    pending: Signal | None = None            # signal from the previous closed bar
    day: date | None = None
    trades_today = 0

    def close(bar_ts, price, reason):
        nonlocal pos
        pnl = pos.pnl(price)
        trades.append(BtTrade(symbol, pos.side, pos.opened_at, pos.entry, bar_ts, price, reason, pos.qty, pnl))
        pos = None

    def open_(side, bar):
        nonlocal pos, trades_today
        qty = qty_fn(bar.open)
        if qty <= 0:
            return
        sl, tg = p.sl_pct / 100, p.target_pct / 100
        if side == "BUY":
            stop, target = bar.open * (1 - sl), (bar.open * (1 + tg) if tg else math.inf)
        else:
            stop, target = bar.open * (1 + sl), (bar.open * (1 - tg) if tg else -math.inf)
        pos = Position(symbol, side, qty, bar.open, stop, target, bar.ts)
        trades_today += 1

    for i, bar in enumerate(candles):
        t = bar.ts.time()
        if bar.ts.date() != day:
            if pos:  # data gap: never carry intraday positions overnight
                prev = candles[i - 1]
                close(prev.ts, prev.close, "end-of-day square-off")
            day, trades_today, pending = bar.ts.date(), 0, None

        # 1) square-off time: exit at this bar's open, no new trades
        if t >= p.square_off:
            if pos:
                close(bar.ts, bar.open, "end-of-day square-off")
            pending = None
            continue

        # 2) act on the previous bar's signal at this bar's open
        if pending is not None:
            if pos and pos.side != pending.value:
                close(bar.ts, bar.open, f"opposite signal {pending.value}")
            if (not pos and p.entry_start <= t <= p.entry_end and trades_today < p.max_trades_per_day
                    and (pending is Signal.BUY or p.allow_short)):
                open_(pending.value, bar)
            pending = None

        # 3) stop / target inside this bar
        if pos:
            long = pos.side == "BUY"
            if long and bar.low <= pos.stop:
                close(bar.ts, min(bar.open, pos.stop), "trailing-stop" if pos.trailing else "stop-loss")
            elif not long and bar.high >= pos.stop:
                close(bar.ts, max(bar.open, pos.stop), "trailing-stop" if pos.trailing else "stop-loss")
            elif long and bar.high >= pos.target:
                close(bar.ts, max(bar.open, pos.target), "target")
            elif not long and bar.low <= pos.target:
                close(bar.ts, min(bar.open, pos.target), "target")
            else:
                pos.update_trail(bar.high if long else bar.low, p.trail_trigger_pct, p.trail_pct)

        # 4) signal on this closed bar
        window = candles[max(0, i - p.lookback):i + 1]
        sig = strategy.on_candles(window)
        if sig is not Signal.HOLD:
            pending = sig

    if pos:
        last = candles[-1]
        close(last.ts, last.close, "end of data")
    return trades


def write_trades(path: str, results: list[BtResult]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["signal", "symbol", "side", "entry_time", "entry", "exit_time", "exit", "points", "qty",
                    "pnl", "reason"])
        for r in results:
            for t in r.trades:
                w.writerow([r.name, t.symbol, t.side, t.entry_ts.isoformat(timespec="minutes"), round(t.entry, 2),
                            t.exit_ts.isoformat(timespec="minutes"), round(t.exit, 2), round(t.points, 2),
                            round(t.qty, 2), round(t.pnl, 2), t.reason])


def format_table(results: list[BtResult]) -> str:
    rows = [HEADER] + [r.row() for r in results]
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(HEADER))]
    line = lambda r: "  ".join(str(v).rjust(w) if j else str(v).ljust(w) for j, (v, w) in enumerate(zip(r, widths)))
    return "\n".join([line(rows[0]), "  ".join("-" * w for w in widths)] + [line(r) for r in rows[1:]])


def run_backtest(cfg, broker, days: int, signal_names: list[str], option_settings=None, delta: float = 0.4,
                 premium_to_index: float = 0.012, out_csv: str = "logs/backtest_trades.csv",
                 today: date | None = None) -> list[BtResult]:
    """`premium_to_index` converts option-premium % levels into index % levels:
    a 1-strike-OTM NIFTY option (~₹120 premium, delta ~0.4, spot ~25,000) moves ~1% when the
    index moves 120 / (0.4 x 25,000) = 0.012% — so OPT_SL_PCT=25 ≈ a 0.3% index move.
    """
    """Fetch history once per instrument, then simulate every requested signal."""
    from dataclasses import replace

    from .strategy import build_strategy

    today = today or datetime.now(IST).date()
    start = today - timedelta(days=days)
    warmup = start - timedelta(days=7)  # extra bars so indicators are warmed up on day one

    if option_settings:
        o = option_settings
        targets = [(s.name, s.underlying, s.lot_size) for s in o.indices]
        k = premium_to_index
        params = BtParams(cfg.entry_start, cfg.entry_end, cfg.square_off, o.sl_pct * k, o.target_pct * k,
                          o.trail_trigger_pct * k, o.trail_pct * k, True, cfg.max_trades_per_day)
    else:
        targets = [(i.symbol, i, None) for i in cfg.instruments]
        params = BtParams(cfg.entry_start, cfg.entry_end, cfg.square_off, cfg.stop_loss_pct, cfg.target_pct,
                          cfg.trail_trigger_pct, cfg.trail_pct, cfg.allow_short, cfg.max_trades_per_day)

    data = {}
    for name, inst, _ in targets:
        log.info("Downloading %s %d-min candles %s → %s", name, cfg.candle_interval, warmup, today)
        data[name] = broker.history(inst, cfg.candle_interval, warmup, today)
        log.info("  %d candles", len(data[name]))

    results = []
    for sig in signal_names:
        strategy = build_strategy(replace(cfg, signal=sig))
        res = BtResult(sig)
        for name, _, lot in targets:
            candles = data[name]
            if lot:  # options estimate: points x delta x lot_size x lots
                lots = o.max_lots
                qty_fn = lambda price, lot=lot, lots=lots: delta * lot * lots
            else:
                qty_fn = lambda price: math.floor(cfg.capital_per_trade / price) if price > 0 else 0
            trades = simulate(name, candles, strategy, params, qty_fn)
            res.trades += [t for t in trades if t.entry_ts.date() >= start]
        res.trades.sort(key=lambda t: t.entry_ts)
        results.append(res)
    write_trades(out_csv, results)
    return results
