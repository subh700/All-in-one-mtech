"""Equity trading loop + shared session runner."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime

from .broker import IST, BrokerError, DhanBroker
from .config import Config, Instrument
from .notify import Notifier
from .risk import Position, RiskManager
from .strategy import Signal, Strategy

log = logging.getLogger(__name__)


def run_session(bot, title: str) -> None:
    """Poll `bot.tick()` until square-off time; square off on Ctrl+C; send a daily summary."""
    cfg, clock, notifier = bot.cfg, bot.clock, bot.notifier
    log.info("Starting %s", title)
    notifier.send(f"🤖 Started: {title}")
    try:
        while True:
            now = clock()
            if now.weekday() >= 5:
                log.info("Weekend — market closed. Exiting.")
                return
            bot.tick()
            if bot.risk.must_square_off(now.time()):
                log.info("Square-off time reached. Done for the day.")
                break
            _time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        log.warning("Interrupted — squaring off open positions")
        bot.square_off_all("manual stop")
    finally:
        summary = (f"📊 Day summary: {bot.risk.trades_today} trades, "
                   f"realized P&L ₹{bot.risk.realized_pnl:,.2f}"
                   + (" (halted: daily loss limit)" if bot.risk.halted else ""))
        log.info(summary)
        notifier.send(summary)
        notifier.close()


class TradingBot:
    def __init__(self, cfg: Config, broker: DhanBroker, strategy: Strategy, clock=None,
                 notifier: Notifier | None = None):
        self.cfg, self.broker, self.strategy = cfg, broker, strategy
        self.risk = RiskManager(cfg)
        self.positions: dict[str, Position] = {}
        self.clock = clock or (lambda: datetime.now(IST))
        self.notifier = notifier or Notifier()
        self._last_bar: dict[str, datetime] = {}

    # ---- one iteration -------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        self.risk.roll_day(now.date())

        if self.risk.must_square_off(now.time()):
            if self.positions:
                self.square_off_all("end-of-day square-off")
            return

        for inst in self.cfg.instruments:
            try:
                self._manage_exit(inst)
            except BrokerError as exc:
                self._error(f"{inst.symbol} exit check failed: {exc}", key=f"exit:{inst.symbol}")

        if self.risk.halted and self.positions:
            self.square_off_all("daily loss limit")
            return

        if not self.risk.in_entry_window(now.time()):
            return

        for inst in self.cfg.instruments:
            try:
                self._evaluate(inst)
            except BrokerError as exc:
                self._error(f"{inst.symbol} evaluation failed: {exc}", key=f"eval:{inst.symbol}")

    def _error(self, msg: str, key: str) -> None:
        log.error(msg)
        self.notifier.send(f"⚠️ {msg}", key=key)

    def _manage_exit(self, inst: Instrument) -> None:
        pos = self.positions.get(inst.symbol)
        if not pos:
            return
        price = self.broker.ltp(inst)
        if pos.update_trail(price, self.cfg.trail_trigger_pct, self.cfg.trail_pct):
            log.info("%s trailing stop active, SL now %.2f", inst.symbol, pos.stop)
            self.notifier.send(f"🔒 {inst.symbol} trailing stop active — SL {pos.stop:.2f}")
        reason = pos.exit_reason(price)
        if reason:
            self._close(inst, reason)

    def _evaluate(self, inst: Instrument) -> None:
        candles = self.broker.candles(inst, self.cfg.candle_interval, now=self.clock())
        if not candles:
            return
        last = candles[-1].ts
        if self._last_bar.get(inst.symbol) == last:
            return  # already acted on this bar
        self._last_bar[inst.symbol] = last

        sig = self.strategy.on_candles(candles)
        pos = self.positions.get(inst.symbol)
        log.debug("%s bar %s close %.2f -> %s", inst.symbol, last, candles[-1].close, sig.value)
        if sig is Signal.HOLD:
            return

        # Opposite signal closes an existing position.
        if pos and pos.side != sig.value:
            self._close(inst, f"opposite signal {sig.value}")
            pos = None
        if pos:
            return
        if sig is Signal.SELL and not self.cfg.allow_short:
            return
        self._open(inst, sig.value, candles[-1].close)

    # ---- order helpers -------------------------------------------------
    def _unrealized(self) -> float:
        total = 0.0
        for inst in self.cfg.instruments:
            pos = self.positions.get(inst.symbol)
            if pos:
                try:
                    total += pos.pnl(self.broker.ltp(inst))
                except BrokerError:
                    pass
        return total

    def _open(self, inst: Instrument, side: str, ref_price: float) -> None:
        ok, why = self.risk.can_open(len(self.positions), self._unrealized())
        if not ok:
            log.info("Skip %s %s: %s", side, inst.symbol, why)
            return
        price = self.broker.ltp(inst) or ref_price  # size on live price, not the stale close
        qty = self.risk.quantity(price)
        if qty < 1:
            log.info("Skip %s %s: CAPITAL_PER_TRADE too small for price %.2f", side, inst.symbol, price)
            return
        fill = self.broker.market_order(inst, side, qty, tag=f"bot-{inst.symbol}-open")
        stop, target = self.risk.brackets(side, fill.price)
        self.positions[inst.symbol] = Position(inst.symbol, side, qty, fill.price, stop, target, self.clock())
        self.risk.record_entry()
        msg = f"{side} {inst.symbol} x{qty} @ {fill.price:.2f} | SL {stop:.2f} | TGT {target:.2f}"
        log.info("OPEN %s", msg)
        self.notifier.send(f"🟢 OPEN {msg}")

    def _close(self, inst: Instrument, reason: str) -> None:
        pos = self.positions.get(inst.symbol)
        if not pos:
            return
        exit_side = "SELL" if pos.side == "BUY" else "BUY"
        fill = self.broker.market_order(inst, exit_side, pos.qty, tag=f"bot-{inst.symbol}-close")
        pnl = pos.pnl(fill.price)
        del self.positions[inst.symbol]
        self.risk.record_exit(pnl)
        msg = (f"{inst.symbol} x{pos.qty} @ {fill.price:.2f} ({reason}) "
               f"P&L ₹{pnl:,.2f} | day ₹{self.risk.realized_pnl:,.2f}")
        log.info("CLOSE %s", msg)
        self.notifier.send(f"{'✅' if pnl >= 0 else '🔴'} CLOSE {msg}")
        if self.risk.halted:
            self.notifier.send("🛑 Daily loss limit hit — no more entries today", key="halted")

    def square_off_all(self, reason: str) -> None:
        for inst in self.cfg.instruments:
            if inst.symbol in self.positions:
                try:
                    self._close(inst, reason)
                except BrokerError as exc:
                    self._error(f"Square-off {inst.symbol} failed: {exc} — using exit_all", key="sqoff")
                    self.broker.exit_all()
                    self.positions.clear()
                    return

    def run(self) -> None:
        run_session(self, f"equity bot ({self.cfg.mode.upper()}) signal={self.strategy.name} on "
                          + ", ".join(i.symbol for i in self.cfg.instruments))
