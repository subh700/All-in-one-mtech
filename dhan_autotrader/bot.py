"""Equity trading loop + shared session runner."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime

from .broker import IST, BrokerError, DhanBroker
from .config import Config, Instrument
from .costs import trade_charges
from .journal import (NullJournal, NullStore, StateStore, TradeJournal, position_from_dict, position_to_dict,
                      risk_from_dict, risk_to_dict)
from .notify import Notifier
from .risk import Position, RiskManager
from .strategy import Signal, Strategy

log = logging.getLogger(__name__)


def verify_restored(broker, security_id: str, pos: Position) -> bool:
    """In live mode, make sure a restored position still exists at Dhan (adjust qty if it differs)."""
    try:
        n = broker.net_quantity(security_id)
    except BrokerError as exc:
        log.warning("Could not verify restored %s with Dhan (%s) — keeping it", pos.symbol, exc)
        return True
    if n is None:
        return True
    if n == 0:
        log.warning("Restored %s is no longer open at Dhan — dropping it", pos.symbol)
        return False
    if abs(n) != pos.qty:
        log.warning("Restored %s qty %d differs from Dhan net qty %d — using Dhan's", pos.symbol, pos.qty, n)
        pos.qty = abs(n)
    return True


HELP = ("Commands:\n/status — positions, P&L, state\n/pnl — today's P&L\n/pause — no new entries\n"
        "/resume — allow entries again\n/squareoff — close all positions now (keeps trading)\n"
        "/stop — close all and stop trading for today\n/help")


def handle_command(bot, cmd: str) -> str:
    """Execute a remote command against a TradingBot or OptionsRunner and return the reply."""
    risk = bot.risk
    if cmd in ("/status", "/start"):
        state = ("HALTED: " + risk.halt_reason) if risk.halted else ("PAUSED" if risk.paused else "running")
        lines = [f"{bot.cfg.mode.upper()} | {state}",
                 f"Trades today: {risk.trades_today}/{bot.cfg.max_trades_per_day} | realized ₹{risk.realized_pnl:,.2f}"]
        lines += bot.status_lines() or ["No open positions"]
        return "\n".join(lines)
    if cmd == "/pnl":
        try:
            unreal = bot.unrealized_total()
        except BrokerError:
            unreal = 0.0
        return (f"Realized ₹{risk.realized_pnl:,.2f} | open ₹{unreal:,.2f} | "
                f"total ₹{risk.realized_pnl + unreal:,.2f}")
    if cmd == "/pause":
        risk.paused = True
        bot.save_state()
        return "⏸ Paused — no new entries. Open positions are still managed (SL/target/trailing)."
    if cmd == "/resume":
        risk.paused = False
        bot.save_state()
        return "▶️ Resumed" + (f" (but halted for today: {risk.halt_reason})" if risk.halted else "")
    if cmd == "/squareoff":
        bot.square_off_all("Telegram /squareoff")
        return "Squared off all positions. Still trading — send /pause or /stop to stop new entries."
    if cmd == "/stop":
        risk.halt("stopped via Telegram")
        bot.square_off_all("Telegram /stop")
        bot.save_state()
        return "🛑 Stopped for today — all positions closed."
    return HELP


def run_session(bot, title: str, commands=None) -> bool:
    """Poll `bot.tick()` until square-off time; square off on Ctrl+C; send a daily summary.

    `commands` (TelegramCommands) are handled between ticks.
    Returns True if the session was interrupted with Ctrl+C.
    """
    interrupted = False
    cfg, clock, notifier = bot.cfg, bot.clock, bot.notifier
    log.info("Starting %s", title)
    restored = bot.restore()
    notifier.send(f"🤖 Started: {title}" + (f" — resumed {restored} open position(s)" if restored else ""))
    try:
        while True:
            now = clock()
            if now.weekday() >= 5:
                log.info("Weekend — market closed. Exiting.")
                break
            bot.tick()
            if bot.risk.must_square_off(now.time()):
                log.info("Square-off time reached. Done for the day.")
                break
            if commands is None:
                _time.sleep(cfg.poll_seconds)
                continue
            deadline = _time.monotonic() + cfg.poll_seconds
            while (left := deadline - _time.monotonic()) > 0:
                cmd = commands.get(timeout=left)
                if cmd:
                    log.info("Telegram command %s", cmd)
                    try:
                        notifier.send(handle_command(bot, cmd))
                    except BrokerError as exc:
                        notifier.send(f"⚠️ {cmd} failed: {exc}")
    except KeyboardInterrupt:
        interrupted = True
        log.warning("Interrupted — squaring off open positions")
        bot.square_off_all("manual stop")
    finally:
        summary = (f"📊 Day summary: {bot.risk.trades_today} trades, "
                   f"realized P&L ₹{bot.risk.realized_pnl:,.2f}"
                   + (f" (halted: {bot.risk.halt_reason})" if bot.risk.halted else ""))
        log.info(summary)
        notifier.send(summary)
        notifier.close()
        if commands is not None:
            commands.close()
    return interrupted


class TradingBot:
    def __init__(self, cfg: Config, broker: DhanBroker, strategy: Strategy, clock=None,
                 notifier: Notifier | None = None, journal: TradeJournal | None = None,
                 store: StateStore | None = None):
        self.cfg, self.broker, self.strategy = cfg, broker, strategy
        self.journal = journal or NullJournal()
        self.store = store or NullStore()
        self.risk = RiskManager(cfg)
        self.positions: dict[str, Position] = {}
        self.clock = clock or (lambda: datetime.now(IST))
        self.notifier = notifier or Notifier()
        self.risk.on_halt = lambda reason: self.notifier.send(f"🛑 Halted for today: {reason}")
        self._last_bar: dict[str, datetime] = {}

    # ---- persistence ---------------------------------------------------
    def save_state(self) -> None:
        self.store.save(self.clock().date(), {
            "kind": "equity", "risk": risk_to_dict(self.risk),
            "positions": {k: position_to_dict(p) for k, p in self.positions.items()},
            "last_bar": {k: v.isoformat() for k, v in self._last_bar.items()},
        })

    def restore(self) -> int:
        today = self.clock().date()
        data = self.store.load(today)
        if not data or data.get("kind") != "equity":
            return 0
        risk_from_dict(self.risk, data.get("risk", {}), today)
        # don't re-act on a candle that was already traded before the restart
        self._last_bar.update({k: datetime.fromisoformat(v) for k, v in data.get("last_bar", {}).items()})
        by_symbol = {i.symbol: i for i in self.cfg.instruments}
        for sym, d in data.get("positions", {}).items():
            inst = by_symbol.get(sym)
            pos = position_from_dict(d)
            if inst is None:
                log.warning("Restored %s is not in INSTRUMENTS — close it manually in Dhan", sym)
                continue
            if verify_restored(self.broker, inst.security_id, pos):
                self.positions[sym] = pos
                log.info("Resumed %s %s x%d @ %.2f | SL %.2f", pos.side, sym, pos.qty, pos.entry, pos.stop)
        self.save_state()
        return len(self.positions)

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
            self.square_off_all(self.risk.halt_reason)
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
        old_stop = pos.stop
        if pos.update_trail(price, self.cfg.trail_trigger_pct, self.cfg.trail_pct):
            log.info("%s trailing stop active, SL now %.2f", inst.symbol, pos.stop)
            self.notifier.send(f"🔒 {inst.symbol} trailing stop active — SL {pos.stop:.2f}")
        if pos.stop != old_stop:
            self.save_state()  # remember the ratcheted stop across restarts
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
        self.save_state()

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
        ok, why = self.risk.can_open(len(self.positions), self._unrealized(), now=self.clock())
        if not ok:
            log.info("Skip %s %s: %s", side, inst.symbol, why)
            return
        price = self.broker.ltp(inst) or ref_price  # size on live price, not the stale close
        qty = self.risk.cap_for_risk(self.risk.quantity(price), price * self.cfg.stop_loss_pct / 100)
        if qty < 1:
            log.info("Skip %s %s: CAPITAL_PER_TRADE / MAX_RISK_PER_TRADE too small for price %.2f",
                     side, inst.symbol, price)
            return
        fill = self.broker.market_order(inst, side, qty, tag=f"bot-{inst.symbol}-open")
        stop, target = self.risk.brackets(side, fill.price)
        self.positions[inst.symbol] = Position(inst.symbol, side, qty, fill.price, stop, target, self.clock())
        self.risk.record_entry()
        self.save_state()
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
        self.risk.record_exit(pnl, now=self.clock())
        charges = trade_charges(inst.exchange_segment, pos.side, pos.entry, fill.price, pos.qty)
        self.journal.record(pos, fill.price, pnl, reason, self.clock(), charges=charges)
        self.save_state()
        msg = (f"{inst.symbol} x{pos.qty} @ {fill.price:.2f} ({reason}) "
               f"P&L ₹{pnl:,.2f} (net ≈ ₹{pnl - charges:,.2f}) | day ₹{self.risk.realized_pnl:,.2f}")
        log.info("CLOSE %s", msg)
        self.notifier.send(f"{'✅' if pnl >= 0 else '🔴'} CLOSE {msg}")

    def square_off_all(self, reason: str) -> None:
        for inst in self.cfg.instruments:
            if inst.symbol in self.positions:
                try:
                    self._close(inst, reason)
                except BrokerError as exc:
                    self._error(f"Square-off {inst.symbol} failed: {exc} — using exit_all", key="sqoff")
                    self.broker.exit_all()
                    self.positions.clear()
                    self.save_state()
                    return

    def status_lines(self) -> list[str]:
        out = []
        for inst in self.cfg.instruments:
            pos = self.positions.get(inst.symbol)
            if not pos:
                continue
            try:
                ltp = self.broker.ltp(inst)
                pnl = f"LTP {ltp:.2f} | P&L ₹{pos.pnl(ltp):,.2f}"
            except BrokerError:
                pnl = "LTP n/a"
            out.append(f"{pos.side} {pos.symbol} x{pos.qty} @ {pos.entry:.2f} | {pnl} | SL {pos.stop:.2f}"
                       + (" (trailing)" if pos.trailing else ""))
        return out

    def unrealized_total(self) -> float:
        return self._unrealized()

    def run(self, commands=None) -> bool:
        return run_session(self, f"equity bot ({self.cfg.mode.upper()}) signal={self.strategy.name} on "
                          + ", ".join(i.symbol for i in self.cfg.instruments), commands=commands)
