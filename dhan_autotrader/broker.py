"""Broker layer: a live Dhan broker and a paper broker sharing one interface.

Both use real Dhan market data; only the paper broker fakes order execution.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .config import Config, Instrument

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


class BrokerError(RuntimeError):
    pass


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Fill:
    order_id: str
    price: float


def _unwrap(resp: dict, what: str):
    if not isinstance(resp, dict) or resp.get("status") != "success":
        remarks = resp.get("remarks") if isinstance(resp, dict) else resp
        raise BrokerError(f"{what} failed: {remarks}")
    return resp.get("data")


class DhanBroker:
    """Thin wrapper around the official `dhanhq` SDK."""

    LTP_MIN_INTERVAL = 1.05  # Dhan market-quote APIs allow ~1 request/second
    _last_ltp = 0.0

    def __init__(self, cfg: Config, client=None):
        self.cfg = cfg
        if client is None:
            from dhanhq import DhanContext, dhanhq

            client = dhanhq(DhanContext(cfg.client_id, cfg.access_token))
        self.api = client

    # ---- account -------------------------------------------------------
    def check_connection(self) -> dict:
        data = _unwrap(self.api.get_fund_limits(), "Fund limits")
        # Dhan's API really spells it "availabelBalance".
        bal = data.get("availabelBalance", data.get("availableBalance"))
        log.info("Connected to Dhan client %s, available balance: %s", self.cfg.client_id, bal)
        return data

    # ---- market data ---------------------------------------------------
    def candles(self, inst: Instrument, interval: int, days: int = 5, now: datetime | None = None) -> list[Candle]:
        now = now or datetime.now(IST)
        resp = self.api.intraday_minute_data(
            security_id=inst.security_id,
            exchange_segment=inst.exchange_segment,
            instrument_type=inst.instrument_type,
            from_date=(now - timedelta(days=days)).strftime("%Y-%m-%d"),
            to_date=(now + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval=interval,
        )
        d = _unwrap(resp, f"Candles {inst.symbol}") or {}
        ts = d.get("timestamp", [])
        candles = [
            Candle(datetime.fromtimestamp(t, IST), o, h, l, c, v)
            for t, o, h, l, c, v in zip(ts, d["open"], d["high"], d["low"], d["close"], d["volume"])
        ] if ts else []
        # Drop the candle that is still forming so signals use closed bars only.
        cutoff = now - timedelta(minutes=interval)
        return [c for c in candles if c.ts <= cutoff]

    def history(self, inst: Instrument, interval: int, start: date, end: date, chunk_days: int = 60) -> list[Candle]:
        """Intraday candles between two dates (inclusive), fetched in chunks Dhan accepts."""
        out: list[Candle] = []
        cur = start
        while cur <= end:
            stop = min(cur + timedelta(days=chunk_days), end + timedelta(days=1))
            resp = self.api.intraday_minute_data(
                security_id=inst.security_id, exchange_segment=inst.exchange_segment,
                instrument_type=inst.instrument_type, from_date=cur.isoformat(), to_date=stop.isoformat(),
                interval=interval,
            )
            d = _unwrap(resp, f"History {inst.symbol} {cur}..{stop}") or {}
            ts = d.get("timestamp", []) if isinstance(d, dict) else []
            out += [Candle(datetime.fromtimestamp(t, IST), o, h, l, c, v)
                    for t, o, h, l, c, v in zip(ts, d.get("open", []), d.get("high", []), d.get("low", []),
                                                d.get("close", []), d.get("volume", []))]
            cur = stop
            _time.sleep(0.3)  # stay well inside Dhan's data-API rate limit
        seen, uniq = set(), []
        for c in sorted(out, key=lambda c: c.ts):
            if c.ts not in seen:
                seen.add(c.ts)
                uniq.append(c)
        return uniq

    def ltp(self, inst: Instrument) -> float:
        wait = self.LTP_MIN_INTERVAL - (_time.monotonic() - self._last_ltp)
        if wait > 0:
            _time.sleep(wait)
        self._last_ltp = _time.monotonic()
        resp = self.api.ticker_data({inst.exchange_segment: [int(inst.security_id)]})
        data = _unwrap(resp, f"LTP {inst.symbol}")
        try:
            return float(data["data"][inst.exchange_segment][str(inst.security_id)]["last_price"])
        except (KeyError, TypeError) as exc:
            raise BrokerError(f"Unexpected LTP payload for {inst.symbol}: {data}") from exc

    # ---- orders --------------------------------------------------------
    def market_order(self, inst: Instrument, side: str, qty: int, tag: str) -> Fill:
        resp = self.api.place_order(
            security_id=inst.security_id,
            exchange_segment=inst.exchange_segment,
            transaction_type=side,
            quantity=qty,
            order_type="MARKET",
            product_type="INTRADAY",
            price=0,
            tag=tag[:25],
        )
        order_id = str(_unwrap(resp, f"{side} {qty} {inst.symbol}").get("orderId"))
        price = self._await_fill(order_id)
        log.info("LIVE %s %d %s filled @ %.2f (order %s)", side, qty, inst.symbol, price, order_id)
        return Fill(order_id, price)

    def _await_fill(self, order_id: str, timeout: float = 15.0) -> float:
        deadline = _time.monotonic() + timeout
        while True:
            data = _unwrap(self.api.get_order_by_id(order_id), f"Order {order_id}")
            if isinstance(data, list):
                data = data[0] if data else {}
            status = str(data.get("orderStatus", "")).upper()
            if status == "TRADED":
                return float(data.get("averageTradedPrice") or data.get("price") or 0)
            if status in {"REJECTED", "CANCELLED", "EXPIRED"}:
                raise BrokerError(f"Order {order_id} {status}: {data.get('omsErrorDescription', '')}")
            if _time.monotonic() > deadline:
                raise BrokerError(f"Order {order_id} not filled within {timeout}s (status {status})")
            _time.sleep(1)

    def net_quantity(self, security_id: str) -> int | None:
        """Open intraday quantity Dhan reports for a security (used to verify restored positions)."""
        data = _unwrap(self.api.get_positions(), "Positions") or []
        for p in data if isinstance(data, list) else []:
            if str(p.get("securityId")) == str(security_id):
                return int(p.get("netQty") or 0)
        return 0

    def exit_all(self) -> None:
        log.warning("Exiting ALL open positions via Dhan")
        _unwrap(self.api.exit_all_positions(), "Exit all positions")


class PaperBroker(DhanBroker):
    """Real market data, simulated fills at the last traded price."""

    def __init__(self, cfg: Config, client=None):
        super().__init__(cfg, client)
        self._n = 0

    def market_order(self, inst: Instrument, side: str, qty: int, tag: str) -> Fill:
        self._n += 1
        price = self.ltp(inst)
        log.info("PAPER %s %d %s @ %.2f", side, qty, inst.symbol, price)
        return Fill(f"PAPER-{self._n}", price)

    def net_quantity(self, security_id: str) -> int | None:
        return None  # nothing to verify against in paper mode

    def exit_all(self) -> None:
        log.info("PAPER exit_all (no-op; bot closes its own simulated positions)")
