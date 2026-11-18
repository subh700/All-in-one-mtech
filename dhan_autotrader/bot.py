"""The auto-trading loop."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime

from .broker import IST, BrokerError, DhanBroker
from .config import Config, Instrument
from .risk import Position, RiskManager
from .strategy import Signal, Strategy

log = logging.getLogger(__name__)


class TradingBot:
    def __init__(self, cfg: Config, broker: DhanBroker, strategy: Strategy, clock=None):
        self.cfg, self.broker, self.strategy = cfg, broker, strategy
        self.risk = RiskManager(cfg)
        self.positions: dict[str, Position] = {}
        self.clock = clock or (lambda: datetime.now(IST))
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
                log.error("%s exit check failed: %s", inst.symbol, exc)

        if self.risk.halted and self.positions:
            self.square_off_all("daily loss limit")
            return

        if not self.risk.in_entry_window(now.time()):
            return

        for inst in self.cfg.instruments:
            try:
                self._evaluate(inst)
            except BrokerError as exc:
                log.error("%s evaluation failed: %s", inst.symbol, exc)

    def _manage_exit(self, inst: Instrument) -> None:
        pos = self.positions.get(inst.symbol)
        if not pos:
            return
        price = self.broker.ltp(inst)
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

        sig = self.strategy.signal([c.close for c in candles])
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
        log.info("OPEN %s %s x%d @ %.2f | SL %.2f | TGT %.2f", side, inst.symbol, qty, fill.price, stop, target)

    def _close(self, inst: Instrument, reason: str) -> None:
        pos = self.positions.get(inst.symbol)
        if not pos:
            return
        exit_side = "SELL" if pos.side == "BUY" else "BUY"
        fill = self.broker.market_order(inst, exit_side, pos.qty, tag=f"bot-{inst.symbol}-close")
        pnl = pos.pnl(fill.price)
        del self.positions[inst.symbol]
        self.risk.record_exit(pnl)
        log.info("CLOSE %s x%d @ %.2f (%s) P&L %.2f | day P&L %.2f",
                 inst.symbol, pos.qty, fill.price, reason, pnl, self.risk.realized_pnl)

    def square_off_all(self, reason: str) -> None:
        for inst in self.cfg.instruments:
            if inst.symbol in self.positions:
                try:
                    self._close(inst, reason)
                except BrokerError as exc:
                    log.error("Square-off %s failed: %s — falling back to exit_all", inst.symbol, exc)
                    self.broker.exit_all()
                    self.positions.clear()
                    return

    # ---- main loop -----------------------------------------------------
    def run(self) -> None:
        log.info("Starting %s bot (%s) on %s", self.strategy.name, self.cfg.mode.upper(),
                 ", ".join(i.symbol for i in self.cfg.instruments))
        try:
            while True:
                now = self.clock()
                if now.weekday() >= 5:
                    log.info("Weekend — market closed. Exiting.")
                    return
                if self.risk.must_square_off(now.time()):
                    self.tick()  # squares off anything still open
                    log.info("Square-off time reached. Done for the day.")
                    return
                self.tick()
                _time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            log.warning("Interrupted — squaring off open positions")
            self.square_off_all("manual stop")
