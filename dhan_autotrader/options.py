"""NIFTY options strategy: trade index direction by BUYING options.

Signal comes from an EMA crossover on the NIFTY index candles:
  * bullish cross  -> buy ATM (or chosen offset) CALL
  * bearish cross  -> buy ATM (or chosen offset) PUT
An opposite signal exits the open option (and flips to the other side).
Exits are on option premium: stop-loss %, target %, daily loss limit and
end-of-day square-off. Max risk per trade is the premium paid.
"""
from __future__ import annotations

import logging
import math
import os
import time as _time
from dataclasses import dataclass
from datetime import date, datetime

from .broker import IST, BrokerError, DhanBroker, _unwrap
from .config import Config, Instrument, _bool, parse_instruments
from .risk import Position, RiskManager
from .strategy import Signal, Strategy

log = logging.getLogger(__name__)


@dataclass
class OptionSettings:
    underlying: Instrument = Instrument("NIFTY", "13", "IDX_I")
    lot_size: int = 65            # verify current NSE lot size before going live
    max_lots: int = 1
    strike_step: int = 50
    strike_offset: int = 1        # 1 = one strike OTM (default), 2 = two OTM, 0 = ATM, -1 = ITM
    expiry_index: int = 0         # 0 = nearest weekly expiry
    skip_expiry_day: bool = True  # on expiry day roll to the next expiry
    sl_pct: float = 25.0          # % of premium
    target_pct: float = 50.0      # % of premium
    min_premium: float = 10.0     # skip illiquid far-OTM contracts

    @classmethod
    def from_env(cls) -> "OptionSettings":
        e = os.environ.get
        s = cls(
            underlying=parse_instruments(e("OPT_UNDERLYING", "NIFTY:13:IDX_I"))[0],
            lot_size=int(e("OPT_LOT_SIZE", "65")),
            max_lots=int(e("OPT_MAX_LOTS", "1")),
            strike_step=int(e("OPT_STRIKE_STEP", "50")),
            strike_offset=int(e("OPT_STRIKE_OFFSET", "1")),
            expiry_index=int(e("OPT_EXPIRY_INDEX", "0")),
            skip_expiry_day=_bool(e("OPT_SKIP_EXPIRY_DAY", "true")),
            sl_pct=float(e("OPT_SL_PCT", "25")),
            target_pct=float(e("OPT_TARGET_PCT", "50")),
            min_premium=float(e("OPT_MIN_PREMIUM", "10")),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.lot_size < 1 or self.max_lots < 1 or self.strike_step < 1:
            raise ValueError("OPT_LOT_SIZE, OPT_MAX_LOTS, OPT_STRIKE_STEP must be >= 1")
        if not (0 < self.sl_pct < 100) or self.target_pct <= 0:
            raise ValueError("OPT_SL_PCT must be in (0, 100) and OPT_TARGET_PCT > 0")


@dataclass
class OptionContract:
    inst: Instrument
    kind: str        # "CE" / "PE"
    strike: float
    expiry: str
    ltp: float


class OptionChainClient:
    """Resolves expiries and strikes via Dhan's option-chain API."""

    MIN_INTERVAL = 3.1  # Dhan allows 1 option-chain request per 3 s

    def __init__(self, broker: DhanBroker, settings: OptionSettings):
        self.api, self.s = broker.api, settings
        self._last_call = 0.0
        self._expiries: tuple[date, list[str]] | None = None

    def _throttle(self) -> None:
        wait = self.MIN_INTERVAL - (_time.monotonic() - self._last_call)
        if wait > 0 and self._last_call:
            _time.sleep(wait)
        self._last_call = _time.monotonic()

    def expiries(self, today: date) -> list[str]:
        if self._expiries and self._expiries[0] == today:
            return self._expiries[1]
        self._throttle()
        u = self.s.underlying
        data = _unwrap(self.api.expiry_list(int(u.security_id), u.exchange_segment), "Expiry list")
        lst = sorted(data.get("data", data) if isinstance(data, dict) else data)
        self._expiries = (today, lst)
        return lst

    def pick_expiry(self, today: date) -> str:
        lst = [x for x in self.expiries(today) if x >= today.isoformat()]
        if self.s.skip_expiry_day and lst and lst[0] == today.isoformat():
            lst = lst[1:]
        if len(lst) <= self.s.expiry_index:
            raise BrokerError(f"No usable expiry in {lst}")
        return lst[self.s.expiry_index]

    def select(self, kind: str, today: date) -> OptionContract:
        expiry = self.pick_expiry(today)
        self._throttle()
        u = self.s.underlying
        resp = self.api.option_chain(int(u.security_id), u.exchange_segment, expiry)
        payload = _unwrap(resp, "Option chain")
        chain = payload.get("data", payload)
        spot = float(chain["last_price"])
        oc = chain.get("oc") or {}
        if not oc:
            raise BrokerError(f"Empty option chain for {expiry}")

        step = self.s.strike_step
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
                sym = f"{u.symbol} {expiry} {strike:g} {kind}"
                log.info("Selected %s (spot %.2f, premium %.2f)", sym, spot, ltp)
                return OptionContract(Instrument(sym, str(sid), "NSE_FNO"), kind, strike, expiry, ltp)
        raise BrokerError(f"No liquid {kind} near strike {wanted} for {expiry}")


@dataclass
class OptionPosition:
    contract: OptionContract
    pos: Position


class NiftyOptionsBot:
    def __init__(self, cfg: Config, settings: OptionSettings, broker: DhanBroker,
                 strategy: Strategy, chain: OptionChainClient | None = None, clock=None):
        self.cfg, self.s, self.broker, self.strategy = cfg, settings, broker, strategy
        self.chain = chain or OptionChainClient(broker, settings)
        self.risk = RiskManager(cfg)
        self.clock = clock or (lambda: datetime.now(IST))
        self.position: OptionPosition | None = None
        self._last_bar: datetime | None = None

    # ---- sizing / brackets -------------------------------------------
    def lots_for(self, premium: float) -> int:
        cost_per_lot = premium * self.s.lot_size
        if cost_per_lot <= 0:
            return 0
        return min(self.s.max_lots, math.floor(self.cfg.capital_per_trade / cost_per_lot))

    def brackets(self, premium: float) -> tuple[float, float]:
        return (round(premium * (1 - self.s.sl_pct / 100), 2),
                round(premium * (1 + self.s.target_pct / 100), 2))

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
                ltp = self.broker.ltp(self.position.contract.inst)
                reason = self.position.pos.exit_reason(ltp)
                if not reason and self.risk.realized_pnl + self.position.pos.pnl(ltp) <= -abs(self.cfg.max_daily_loss):
                    reason = "daily loss limit"
                    self.risk.halted = True
                if reason:
                    self._close(reason)
            except BrokerError as exc:
                log.error("Option exit check failed: %s", exc)

        if not self.risk.in_entry_window(now.time()):
            return
        try:
            self._evaluate(now)
        except BrokerError as exc:
            log.error("Options evaluation failed: %s", exc)

    def _evaluate(self, now: datetime) -> None:
        candles = self.broker.candles(self.s.underlying, self.cfg.candle_interval, now=now)
        if not candles or candles[-1].ts == self._last_bar:
            return
        self._last_bar = candles[-1].ts
        sig = self.strategy.signal([c.close for c in candles])
        log.debug("%s bar %s close %.2f -> %s", self.s.underlying.symbol, self._last_bar,
                  candles[-1].close, sig.value)
        if sig is Signal.HOLD:
            return
        kind = "CE" if sig is Signal.BUY else "PE"
        if self.position and self.position.contract.kind != kind:
            self._close(f"opposite signal {sig.value}")
        if self.position:
            return
        self._open(kind, now.date())

    def _open(self, kind: str, today: date) -> None:
        ok, why = self.risk.can_open(0)
        if not ok:
            log.info("Skip %s entry: %s", kind, why)
            return
        contract = self.chain.select(kind, today)
        premium = self.broker.ltp(contract.inst) or contract.ltp
        lots = self.lots_for(premium)
        if lots < 1:
            log.info("Skip %s: 1 lot costs %.0f > CAPITAL_PER_TRADE %.0f",
                     contract.inst.symbol, premium * self.s.lot_size, self.cfg.capital_per_trade)
            return
        qty = lots * self.s.lot_size
        fill = self.broker.market_order(contract.inst, "BUY", qty, tag="bot-nifty-opt-open")
        stop, target = self.brackets(fill.price)
        self.position = OptionPosition(contract, Position(contract.inst.symbol, "BUY", qty, fill.price,
                                                          stop, target, self.clock()))
        self.risk.record_entry()
        log.info("OPEN BUY %s x%d (%d lot) @ %.2f | SL %.2f | TGT %.2f | risk ≈ %.0f",
                 contract.inst.symbol, qty, lots, fill.price, stop, target, (fill.price - stop) * qty)

    def _close(self, reason: str) -> None:
        if not self.position:
            return
        op = self.position
        fill = self.broker.market_order(op.contract.inst, "SELL", op.pos.qty, tag="bot-nifty-opt-close")
        pnl = op.pos.pnl(fill.price)
        self.position = None
        self.risk.record_exit(pnl)
        log.info("CLOSE %s x%d @ %.2f (%s) P&L %.2f | day P&L %.2f",
                 op.contract.inst.symbol, op.pos.qty, fill.price, reason, pnl, self.risk.realized_pnl)

    def square_off_all(self, reason: str) -> None:
        try:
            self._close(reason)
        except BrokerError as exc:
            log.error("Square-off failed: %s — falling back to exit_all", exc)
            self.broker.exit_all()
            self.position = None

    def run(self) -> None:
        log.info("Starting NIFTY options bot (%s): %s EMA %d/%d on %dm, offset %+d, %d lot max",
                 self.cfg.mode.upper(), self.s.underlying.symbol, self.cfg.fast_ema, self.cfg.slow_ema,
                 self.cfg.candle_interval, self.s.strike_offset, self.s.max_lots)
        try:
            while True:
                now = self.clock()
                if now.weekday() >= 5:
                    log.info("Weekend — market closed. Exiting.")
                    return
                self.tick()
                if self.risk.must_square_off(now.time()):
                    log.info("Square-off time reached. Done for the day.")
                    return
                _time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            log.warning("Interrupted — squaring off open option position")
            self.square_off_all("manual stop")
