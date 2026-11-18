"""Index options strategy: trade index direction by BUYING OTM calls / puts.

Supported indices: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY (NSE) and SENSEX (BSE).
For each enabled index, a signal on the index candles (EMA / Supertrend / RSI /
VWAP / ORB — see strategy.py) drives:
  * bullish -> buy OTM (or chosen offset) CALL
  * bearish -> buy OTM (or chosen offset) PUT
An opposite signal exits the open option and flips to the other side.
Exits on premium: stop-loss %, target %, trailing stop, daily loss limit and
end-of-day square-off. Max risk per trade is the premium paid.
Risk limits (trades/day, daily loss, open positions) are shared across indices.
"""
from __future__ import annotations

import logging
import math
import os
import time as _time
from dataclasses import dataclass, replace
from datetime import date, datetime

from .broker import IST, BrokerError, DhanBroker, _unwrap
from .config import Config, Instrument, _bool
from .costs import trade_charges
from .journal import (NullJournal, NullStore, StateStore, TradeJournal, position_from_dict, position_to_dict,
                      risk_from_dict, risk_to_dict)
from .notify import Notifier
from .risk import Position, RiskManager
from .strategy import Signal, Strategy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexSpec:
    name: str
    security_id: str      # Dhan security id of the index (segment IDX_I)
    fno_segment: str      # where its options trade
    strike_step: int
    lot_size: int         # verify against the exchange before going live

    @property
    def underlying(self) -> Instrument:
        return Instrument(self.name, self.security_id, "IDX_I")


# Lot sizes as per NSE/BSE revisions effective 2026 — override with OPT_LOT_SIZE_<NAME> if they change.
PRESETS: dict[str, IndexSpec] = {
    "NIFTY": IndexSpec("NIFTY", "13", "NSE_FNO", 50, 65),
    "BANKNIFTY": IndexSpec("BANKNIFTY", "25", "NSE_FNO", 100, 30),
    "FINNIFTY": IndexSpec("FINNIFTY", "27", "NSE_FNO", 50, 60),
    "MIDCPNIFTY": IndexSpec("MIDCPNIFTY", "442", "NSE_FNO", 25, 120),
    "SENSEX": IndexSpec("SENSEX", "51", "BSE_FNO", 100, 20),
}


def parse_indices(raw: str, env=os.environ) -> list[IndexSpec]:
    out = []
    for name in filter(None, (s.strip().upper() for s in raw.split(","))):
        if name not in PRESETS:
            raise ValueError(f"Unknown index '{name}'. Choose from {', '.join(PRESETS)}")
        spec = PRESETS[name]
        lot = env.get(f"OPT_LOT_SIZE_{name}")
        step = env.get(f"OPT_STRIKE_STEP_{name}")
        spec = replace(spec, lot_size=int(lot) if lot else spec.lot_size,
                       strike_step=int(step) if step else spec.strike_step)
        out.append(spec)
    if not out:
        raise ValueError("OPT_INDICES is empty")
    return out


@dataclass
class OptionSettings:
    indices: tuple[IndexSpec, ...] = (PRESETS["NIFTY"],)
    max_lots: int = 1
    strike_offset: int = 1        # 1 = one strike OTM (default), 2 = two OTM, 0 = ATM, -1 = ITM
    expiry_index: int = 0         # 0 = nearest expiry
    skip_expiry_day: bool = True  # on expiry day roll to the next expiry
    sl_pct: float = 25.0          # % of premium
    target_pct: float = 50.0      # % of premium; 0 = no fixed target (ride the trailing stop)
    min_premium: float = 10.0     # skip illiquid far-OTM contracts
    trail_trigger_pct: float = 20.0  # start trailing once premium is up this %; 0 = off
    trail_pct: float = 15.0          # trail this % below the highest premium

    @classmethod
    def from_env(cls) -> "OptionSettings":
        e = os.environ.get
        s = cls(
            indices=tuple(parse_indices(e("OPT_INDICES", "NIFTY"))),
            max_lots=int(e("OPT_MAX_LOTS", "1")),
            strike_offset=int(e("OPT_STRIKE_OFFSET", "1")),
            expiry_index=int(e("OPT_EXPIRY_INDEX", "0")),
            skip_expiry_day=_bool(e("OPT_SKIP_EXPIRY_DAY", "true")),
            sl_pct=float(e("OPT_SL_PCT", "25")),
            target_pct=float(e("OPT_TARGET_PCT", "50")),
            min_premium=float(e("OPT_MIN_PREMIUM", "10")),
            trail_trigger_pct=float(e("OPT_TRAIL_TRIGGER_PCT", "20")),
            trail_pct=float(e("OPT_TRAIL_PCT", "15")),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.max_lots < 1:
            raise ValueError("OPT_MAX_LOTS must be >= 1")
        if any(i.lot_size < 1 or i.strike_step < 1 for i in self.indices):
            raise ValueError("Lot size and strike step must be >= 1")
        if not (0 < self.sl_pct < 100) or self.target_pct < 0:
            raise ValueError("OPT_SL_PCT must be in (0, 100) and OPT_TARGET_PCT >= 0")
        if self.trail_trigger_pct < 0 or not (0 <= self.trail_pct < 100):
            raise ValueError("OPT_TRAIL_TRIGGER_PCT must be >= 0 and OPT_TRAIL_PCT in [0, 100)")


@dataclass
class OptionContract:
    inst: Instrument
    kind: str        # "CE" / "PE"
    strike: float
    expiry: str
    ltp: float


class OptionChainClient:
    """Resolves expiries and strikes via Dhan's option-chain API."""

    MIN_INTERVAL = 3.1    # Dhan allows 1 option-chain request per 3 s (per account)
    _last_call = 0.0      # shared across all indices

    def __init__(self, broker: DhanBroker, settings: OptionSettings, spec: IndexSpec):
        self.api, self.s, self.spec = broker.api, settings, spec
        self._expiries: tuple[date, list[str]] | None = None

    def _throttle(self) -> None:
        cls = OptionChainClient
        wait = self.MIN_INTERVAL - (_time.monotonic() - cls._last_call)
        if wait > 0:
            _time.sleep(wait)
        cls._last_call = _time.monotonic()

    def expiries(self, today: date) -> list[str]:
        if self._expiries and self._expiries[0] == today:
            return self._expiries[1]
        self._throttle()
        data = _unwrap(self.api.expiry_list(int(self.spec.security_id), "IDX_I"),
                       f"{self.spec.name} expiry list")
        lst = sorted(data.get("data", data) if isinstance(data, dict) else data)
        self._expiries = (today, lst)
        return lst

    def pick_expiry(self, today: date) -> str:
        lst = [x for x in self.expiries(today) if x >= today.isoformat()]
        if self.s.skip_expiry_day and lst and lst[0] == today.isoformat():
            lst = lst[1:]
        if len(lst) <= self.s.expiry_index:
            raise BrokerError(f"No usable {self.spec.name} expiry in {lst}")
        return lst[self.s.expiry_index]

    def select(self, kind: str, today: date) -> OptionContract:
        expiry = self.pick_expiry(today)
        self._throttle()
        resp = self.api.option_chain(int(self.spec.security_id), "IDX_I", expiry)
        payload = _unwrap(resp, f"{self.spec.name} option chain")
        chain = payload.get("data", payload)
        spot = float(chain["last_price"])
        oc = chain.get("oc") or {}
        if not oc:
            raise BrokerError(f"Empty {self.spec.name} option chain for {expiry}")

        step = self.spec.strike_step
        atm = round(spot / step) * step
        offset = self.s.strike_offset * step
        wanted = atm + offset if kind == "CE" else atm - offset
        strikes = sorted(oc, key=lambda k: abs(float(k) - wanted))
        if self.s.strike_offset > 0:  # OTM mode: never fall back to an ATM/ITM strike
            strikes = [k for k in strikes if (float(k) > spot if kind == "CE" else float(k) < spot)]
        side = kind.lower()
        for key in strikes[:5]:
            leg = oc[key].get(side) or {}
            sid, ltp = leg.get("security_id"), float(leg.get("last_price") or 0)
            if sid and ltp >= self.s.min_premium:
                strike = float(key)
                sym = f"{self.spec.name} {expiry} {strike:g} {kind}"
                log.info("Selected %s (spot %.2f, premium %.2f)", sym, spot, ltp)
                return OptionContract(Instrument(sym, str(sid), self.spec.fno_segment), kind, strike, expiry, ltp)
        raise BrokerError(f"No liquid {self.spec.name} {kind} near strike {wanted} for {expiry}")


@dataclass
class OptionPosition:
    contract: OptionContract
    pos: Position


class IndexOptionsBot:
    """Trades options on one index. Use OptionsRunner to run several with shared risk."""

    def __init__(self, cfg: Config, settings: OptionSettings, spec: IndexSpec, broker: DhanBroker,
                 strategy: Strategy, risk: RiskManager | None = None, chain: OptionChainClient | None = None,
                 clock=None, notifier: Notifier | None = None, open_count=None,
                 journal: TradeJournal | None = None, on_change=None):
        self.cfg, self.s, self.spec, self.broker, self.strategy = cfg, settings, spec, broker, strategy
        self.journal = journal or NullJournal()
        self.on_change = on_change or (lambda: None)
        self.risk = risk or RiskManager(cfg)
        self.chain = chain or OptionChainClient(broker, settings, spec)
        self.clock = clock or (lambda: datetime.now(IST))
        self.notifier = notifier or Notifier()
        self.open_count = open_count or (lambda: 1 if self.position else 0)
        self.position: OptionPosition | None = None
        self._last_bar: datetime | None = None

    # ---- sizing / brackets -------------------------------------------
    def lots_for(self, premium: float) -> int:
        cost_per_lot = premium * self.spec.lot_size
        if cost_per_lot <= 0:
            return 0
        return min(self.s.max_lots, math.floor(self.cfg.capital_per_trade / cost_per_lot))

    def brackets(self, premium: float) -> tuple[float, float]:
        stop = round(premium * (1 - self.s.sl_pct / 100), 2)
        target = round(premium * (1 + self.s.target_pct / 100), 2) if self.s.target_pct else math.inf
        return stop, target

    def unrealized(self) -> float:
        if not self.position:
            return 0.0
        return self.position.pos.pnl(self.broker.ltp(self.position.contract.inst))

    # ---- loop ----------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        self.risk.roll_day(now.date())

        if self.risk.must_square_off(now.time()):
            if self.position:
                self._close("end-of-day square-off")
            return

        if self.position:
            try:
                self._manage_exit()
            except BrokerError as exc:
                self._error(f"{self.spec.name} option exit check failed: {exc}", key=f"exit:{self.spec.name}")

        if self.risk.halted:
            if self.position:
                self._close(self.risk.halt_reason)
            return
        if not self.risk.in_entry_window(now.time()):
            return
        try:
            self._evaluate(now)
        except BrokerError as exc:
            self._error(f"{self.spec.name} evaluation failed: {exc}", key=f"eval:{self.spec.name}")

    def _error(self, msg: str, key: str) -> None:
        log.error(msg)
        self.notifier.send(f"⚠️ {msg}", key=key)

    def _manage_exit(self) -> None:
        op = self.position
        ltp = self.broker.ltp(op.contract.inst)
        old_stop = op.pos.stop
        if op.pos.update_trail(ltp, self.s.trail_trigger_pct, self.s.trail_pct):
            log.info("%s trailing stop active, SL now %.2f", op.contract.inst.symbol, op.pos.stop)
            self.notifier.send(f"🔒 {op.contract.inst.symbol} trailing stop active — SL {op.pos.stop:.2f}")
        if op.pos.stop != old_stop:
            self.on_change()
        reason = op.pos.exit_reason(ltp)
        if not reason and self.risk.realized_pnl + op.pos.pnl(ltp) <= -abs(self.cfg.max_daily_loss):
            reason = "daily loss limit"
            self.risk.halt("daily loss limit")
        if reason:
            self._close(reason)

    def _evaluate(self, now: datetime) -> None:
        candles = self.broker.candles(self.spec.underlying, self.cfg.candle_interval, now=now)
        if not candles or candles[-1].ts == self._last_bar:
            return
        self._last_bar = candles[-1].ts
        self.on_change()
        sig = self.strategy.on_candles(candles)
        log.debug("%s bar %s close %.2f -> %s", self.spec.name, self._last_bar, candles[-1].close, sig.value)
        if sig is Signal.HOLD:
            return
        kind = "CE" if sig is Signal.BUY else "PE"
        if self.position and self.position.contract.kind != kind:
            self._close(f"opposite signal {sig.value}")
        if self.position:
            return
        self._open(kind, now.date())

    def _open(self, kind: str, today: date) -> None:
        ok, why = self.risk.can_open(self.open_count(), now=self.clock())
        if not ok:
            log.info("Skip %s %s entry: %s", self.spec.name, kind, why)
            return
        contract = self.chain.select(kind, today)
        premium = self.broker.ltp(contract.inst) or contract.ltp
        lots = self.lots_for(premium)
        qty = self.risk.cap_for_risk(lots * self.spec.lot_size, premium * self.s.sl_pct / 100, self.spec.lot_size)
        lots = qty // self.spec.lot_size
        if lots < 1:
            log.info("Skip %s: 1 lot (premium %.2f x %d) exceeds CAPITAL_PER_TRADE ₹%.0f or MAX_RISK_PER_TRADE",
                     contract.inst.symbol, premium, self.spec.lot_size, self.cfg.capital_per_trade)
            return
        fill = self.broker.market_order(contract.inst, "BUY", qty, tag=f"bot-{self.spec.name}-open")
        stop, target = self.brackets(fill.price)
        self.position = OptionPosition(contract, Position(contract.inst.symbol, "BUY", qty, fill.price,
                                                          stop, target, self.clock()))
        self.risk.record_entry()
        self.on_change()
        msg = (f"BUY {contract.inst.symbol} x{qty} ({lots} lot) @ {fill.price:.2f} | SL {stop:.2f} | "
               f"TGT {'trail' if math.isinf(target) else f'{target:.2f}'} | risk ≈ ₹{(fill.price - stop) * qty:,.0f}")
        log.info("OPEN %s", msg)
        self.notifier.send(f"🟢 OPEN {msg}")

    def _close(self, reason: str) -> None:
        if not self.position:
            return
        op = self.position
        fill = self.broker.market_order(op.contract.inst, "SELL", op.pos.qty, tag=f"bot-{self.spec.name}-close")
        pnl = op.pos.pnl(fill.price)
        self.position = None
        self.risk.record_exit(pnl, now=self.clock())
        charges = trade_charges(op.contract.inst.exchange_segment, "BUY", op.pos.entry, fill.price, op.pos.qty)
        self.journal.record(op.pos, fill.price, pnl, reason, self.clock(), charges=charges)
        self.on_change()
        msg = (f"{op.contract.inst.symbol} x{op.pos.qty} @ {fill.price:.2f} ({reason}) "
               f"P&L ₹{pnl:,.2f} (net ≈ ₹{pnl - charges:,.2f}) | day ₹{self.risk.realized_pnl:,.2f}")
        log.info("CLOSE %s", msg)
        self.notifier.send(f"{'✅' if pnl >= 0 else '🔴'} CLOSE {msg}")

    def square_off_all(self, reason: str) -> None:
        try:
            self._close(reason)
        except BrokerError as exc:
            self._error(f"{self.spec.name} square-off failed: {exc} — using exit_all", key="sqoff")
            self.broker.exit_all()
            self.position = None
            self.on_change()


class OptionsRunner:
    """Runs one IndexOptionsBot per enabled index with shared risk limits."""

    def __init__(self, cfg: Config, settings: OptionSettings, broker: DhanBroker, strategy: Strategy,
                 clock=None, notifier: Notifier | None = None, chains: dict[str, OptionChainClient] | None = None,
                 journal: TradeJournal | None = None, store: StateStore | None = None):
        self.cfg, self.s, self.broker = cfg, settings, broker
        self.store = store or NullStore()
        self.clock = clock or (lambda: datetime.now(IST))
        self.notifier = notifier or Notifier()
        self.risk = RiskManager(cfg)
        self.risk.on_halt = lambda reason: self.notifier.send(f"🛑 Halted for today: {reason}")
        chains = chains or {}
        self.bots = [
            IndexOptionsBot(cfg, settings, spec, broker, strategy, risk=self.risk, chain=chains.get(spec.name),
                            clock=self.clock, notifier=self.notifier, open_count=self.open_count,
                            journal=journal, on_change=self.save_state)
            for spec in settings.indices
        ]

    # ---- persistence ---------------------------------------------------
    def save_state(self) -> None:
        positions = {}
        for b in self.bots:
            if b.position:
                c = b.position.contract
                positions[b.spec.name] = {
                    "contract": {"symbol": c.inst.symbol, "security_id": c.inst.security_id,
                                 "segment": c.inst.exchange_segment, "kind": c.kind, "strike": c.strike,
                                 "expiry": c.expiry, "ltp": c.ltp},
                    "pos": position_to_dict(b.position.pos),
                }
        last_bar = {b.spec.name: b._last_bar.isoformat() for b in self.bots if b._last_bar}
        self.store.save(self.clock().date(), {"kind": "options", "risk": risk_to_dict(self.risk),
                                              "positions": positions, "last_bar": last_bar})

    def restore(self) -> int:
        from .bot import verify_restored

        today = self.clock().date()
        data = self.store.load(today)
        if not data or data.get("kind") != "options":
            return 0
        risk_from_dict(self.risk, data.get("risk", {}), today)
        bots = {b.spec.name: b for b in self.bots}
        for name, ts in data.get("last_bar", {}).items():
            if name in bots:
                bots[name]._last_bar = datetime.fromisoformat(ts)
        for name, d in data.get("positions", {}).items():
            c, pos = d["contract"], position_from_dict(d["pos"])
            bot = bots.get(name)
            if bot is None:
                log.warning("Restored %s position but %s is not in OPT_INDICES — close it manually in Dhan",
                            c["symbol"], name)
                continue
            if not verify_restored(self.broker, c["security_id"], pos):
                continue
            contract = OptionContract(Instrument(c["symbol"], c["security_id"], c["segment"]), c["kind"],
                                      c["strike"], c["expiry"], c["ltp"])
            bot.position = OptionPosition(contract, pos)
            log.info("Resumed %s x%d @ %.2f | SL %.2f", c["symbol"], pos.qty, pos.entry, pos.stop)
        self.save_state()
        return self.open_count()

    def open_count(self) -> int:
        return sum(1 for b in self.bots if b.position)

    def tick(self) -> None:
        for bot in self.bots:
            bot.tick()
        # Daily loss across all indices (realized + every open position).
        if not self.risk.halted and self.open_count():
            try:
                total = self.risk.realized_pnl + sum(b.unrealized() for b in self.bots)
            except BrokerError:
                return
            if total <= -abs(self.cfg.max_daily_loss):
                self.risk.halt("daily loss limit (all indices)")
                self.square_off_all("daily loss limit (all indices)")

    def square_off_all(self, reason: str) -> None:
        for bot in self.bots:
            if bot.position:
                bot.square_off_all(reason)

    def status_lines(self) -> list[str]:
        out = []
        for b in self.bots:
            if not b.position:
                continue
            p = b.position.pos
            try:
                ltp = self.broker.ltp(b.position.contract.inst)
                pnl = f"LTP {ltp:.2f} | P&L ₹{p.pnl(ltp):,.2f}"
            except BrokerError:
                pnl = "LTP n/a"
            out.append(f"{p.symbol} x{p.qty} @ {p.entry:.2f} | {pnl} | SL {p.stop:.2f}"
                       + (" (trailing)" if p.trailing else ""))
        return out

    def unrealized_total(self) -> float:
        return sum(b.unrealized() for b in self.bots)

    def run(self, commands=None) -> bool:
        from .bot import run_session

        names = ", ".join(i.name for i in self.s.indices)
        return run_session(self, f"options bot ({self.cfg.mode.upper()}) {names} | signal={self.bots[0].strategy.name} "
                          f"| OTM offset {self.s.strike_offset:+d} | {self.s.max_lots} lot max", commands=commands)
