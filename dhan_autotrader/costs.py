"""Approximate Indian intraday trading charges (brokerage + statutory).

Rates are typical for Dhan as of 2025-26 and change from time to time — check
Dhan's charges page and adjust the constants below if needed. All values are
fractions of turnover unless noted.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChargeRates:
    brokerage_flat: float          # ₹ per executed order
    brokerage_pct: float           # cap as fraction of order value (0 = flat only)
    stt_sell: float                # Securities Transaction Tax on sell value
    exchange_txn: float            # exchange transaction charge on turnover
    sebi: float = 10 / 1e7         # ₹10 per crore
    stamp_buy: float = 0.00003     # 0.003% on buy value
    gst: float = 0.18              # on brokerage + exchange + SEBI


EQUITY_INTRADAY = ChargeRates(brokerage_flat=20, brokerage_pct=0.0003, stt_sell=0.00025, exchange_txn=0.0000297)
OPTIONS = ChargeRates(brokerage_flat=20, brokerage_pct=0, stt_sell=0.001, exchange_txn=0.0003503)
OPTIONS_BSE = ChargeRates(brokerage_flat=20, brokerage_pct=0, stt_sell=0.001, exchange_txn=0.000325)


def rates_for(segment: str) -> ChargeRates:
    seg = segment.upper()
    if seg == "BSE_FNO":
        return OPTIONS_BSE
    if seg.endswith("FNO"):
        return OPTIONS
    return EQUITY_INTRADAY


def _brokerage(r: ChargeRates, value: float) -> float:
    if value <= 0:
        return 0.0
    return min(r.brokerage_flat, value * r.brokerage_pct) if r.brokerage_pct else r.brokerage_flat


def round_trip_charges(segment: str, buy_value: float, sell_value: float) -> float:
    """Total charges in ₹ for one buy order + one sell order (values = price x qty)."""
    r = rates_for(segment)
    brokerage = _brokerage(r, buy_value) + _brokerage(r, sell_value)
    turnover = buy_value + sell_value
    exchange = turnover * r.exchange_txn
    sebi = turnover * r.sebi
    gst = (brokerage + exchange + sebi) * r.gst
    stt = sell_value * r.stt_sell
    stamp = buy_value * r.stamp_buy
    return round(brokerage + exchange + sebi + gst + stt + stamp, 2)


def trade_charges(segment: str, side: str, entry: float, exit_: float, qty: float) -> float:
    """Charges for a closed trade. `side` is the opening side (BUY = long, SELL = short)."""
    if side == "BUY":
        return round_trip_charges(segment, entry * qty, exit_ * qty)
    return round_trip_charges(segment, exit_ * qty, entry * qty)
