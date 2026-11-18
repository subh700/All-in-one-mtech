"""Entry signals. Every strategy turns a list of closed candles into BUY / SELL / HOLD.

BUY  = bullish (stocks: go long, options: buy CALL)
SELL = bearish (stocks: exit / short, options: buy PUT)

A signal is emitted only on the bar where the condition *changes* (a cross or
flip), so the bot does not re-enter while a trend simply continues.
"""
from __future__ import annotations

from datetime import time, timedelta
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .broker import Candle
    from .config import Config


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


# ---------------------------------------------------------------- indicators
def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average; output has the same length as input."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI. First `period` values are 50 (neutral warm-up)."""
    if len(closes) <= period:
        return [50.0] * len(closes)
    out = [50.0] * (period + 1)
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, period + 1)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, period + 1)]
    avg_g, avg_l = sum(gains) / period, sum(losses) / period

    def val(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[-1] = val(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(ch, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-ch, 0)) / period
        out.append(val(avg_g, avg_l))
    return out


def supertrend(candles: list["Candle"], period: int = 10, mult: float = 3.0) -> list[int]:
    """Supertrend direction per bar: +1 uptrend, -1 downtrend."""
    n = len(candles)
    if n == 0:
        return []
    trs = [candles[0].high - candles[0].low]
    for i in range(1, n):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    atr = [trs[0]]
    for i in range(1, n):  # Wilder smoothing
        atr.append((atr[-1] * (period - 1) + trs[i]) / period)

    direction = [1]
    f_up = f_dn = None
    for i in range(n):
        hl2 = (candles[i].high + candles[i].low) / 2
        up, dn = hl2 + mult * atr[i], hl2 - mult * atr[i]
        if i == 0:
            f_up, f_dn = up, dn
            continue
        prev_close = candles[i - 1].close
        f_up = up if (up < f_up or prev_close > f_up) else f_up
        f_dn = dn if (dn > f_dn or prev_close < f_dn) else f_dn
        d = direction[-1]
        if d == -1 and candles[i].close > f_up:
            d = 1
        elif d == 1 and candles[i].close < f_dn:
            d = -1
        direction.append(d)
    return direction


def _today(candles: list["Candle"]) -> list["Candle"]:
    if not candles:
        return []
    day = candles[-1].ts.date()
    return [c for c in candles if c.ts.date() == day]


# ---------------------------------------------------------------- strategies
class Strategy:
    name = "base"

    def on_candles(self, candles: list["Candle"]) -> Signal:
        return self.signal([c.close for c in candles])

    def signal(self, closes: list[float]) -> Signal:  # pragma: no cover - interface
        raise NotImplementedError


class EmaCrossover(Strategy):
    """BUY when fast EMA crosses above slow EMA, SELL when it crosses below."""

    name = "ema"

    def __init__(self, fast: int = 9, slow: int = 21):
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self.fast, self.slow = fast, slow
        self.min_bars = slow * 3  # let the EMA warm up

    def signal(self, closes: list[float]) -> Signal:
        if len(closes) < self.min_bars:
            return Signal.HOLD
        f, s = ema(closes, self.fast), ema(closes, self.slow)
        prev, curr = f[-2] - s[-2], f[-1] - s[-1]
        if prev <= 0 < curr:
            return Signal.BUY
        if prev >= 0 > curr:
            return Signal.SELL
        return Signal.HOLD


class SupertrendStrategy(Strategy):
    """BUY when Supertrend flips up, SELL when it flips down."""

    name = "supertrend"

    def __init__(self, period: int = 10, mult: float = 3.0):
        self.period, self.mult = period, mult

    def on_candles(self, candles):
        if len(candles) < self.period * 3:
            return Signal.HOLD
        d = supertrend(candles, self.period, self.mult)
        if d[-2] == -1 and d[-1] == 1:
            return Signal.BUY
        if d[-2] == 1 and d[-1] == -1:
            return Signal.SELL
        return Signal.HOLD


class RsiMomentum(Strategy):
    """Momentum RSI: BUY when RSI crosses above `buy_level`, SELL when it crosses below `sell_level`."""

    name = "rsi"

    def __init__(self, period: int = 14, buy_level: float = 60, sell_level: float = 40):
        if sell_level >= buy_level:
            raise ValueError("RSI_SELL must be below RSI_BUY")
        self.period, self.buy, self.sell = period, buy_level, sell_level

    def signal(self, closes):
        if len(closes) < self.period * 3:
            return Signal.HOLD
        r = rsi(closes, self.period)
        if r[-2] <= self.buy < r[-1]:
            return Signal.BUY
        if r[-2] >= self.sell > r[-1]:
            return Signal.SELL
        return Signal.HOLD


class VwapCross(Strategy):
    """BUY when price closes above today's VWAP, SELL when it closes below.

    Indices (NIFTY, BANKNIFTY …) report zero volume, so for them this falls
    back to an equal-weighted average of today's typical prices (TWAP).
    """

    name = "vwap"

    def __init__(self, min_bars_today: int = 3):
        self.min_bars = min_bars_today

    @staticmethod
    def vwap_series(today: list["Candle"]) -> list[float]:
        use_volume = sum(c.volume for c in today) > 0
        num = den = 0.0
        out = []
        for c in today:
            w = c.volume if use_volume else 1.0
            num += (c.high + c.low + c.close) / 3 * w
            den += w
            out.append(num / den if den else c.close)
        return out

    def on_candles(self, candles):
        today = _today(candles)
        if len(today) < max(self.min_bars, 2):
            return Signal.HOLD
        v = self.vwap_series(today)
        pc, cc = today[-2].close, today[-1].close
        if pc <= v[-2] and cc > v[-1]:
            return Signal.BUY
        if pc >= v[-2] and cc < v[-1]:
            return Signal.SELL
        return Signal.HOLD


class OpeningRangeBreakout(Strategy):
    """BUY on the first close above the opening-range high, SELL on a close below its low."""

    name = "orb"

    def __init__(self, minutes: int = 15, market_open: time = time(9, 15)):
        self.minutes, self.market_open = minutes, market_open

    def opening_range(self, today):
        if not today:
            return None
        start = today[0].ts.replace(hour=self.market_open.hour, minute=self.market_open.minute,
                                    second=0, microsecond=0)
        end = start + timedelta(minutes=self.minutes)
        rng = [c for c in today if start <= c.ts < end]
        if not rng or today[-1].ts < end:
            return None  # range still forming
        return max(c.high for c in rng), min(c.low for c in rng), end

    def on_candles(self, candles):
        today = _today(candles)
        r = self.opening_range(today)
        if not r:
            return Signal.HOLD
        hi, lo, end = r
        after = [c for c in today if c.ts >= end]
        if not after:
            return Signal.HOLD
        prev_close = today[today.index(after[-1]) - 1].close
        cc = after[-1].close
        if prev_close <= hi < cc:
            return Signal.BUY
        if prev_close >= lo > cc:
            return Signal.SELL
        return Signal.HOLD


SIGNALS = ("ema", "supertrend", "rsi", "vwap", "orb")


def build_strategy(cfg: "Config") -> Strategy:
    name = cfg.signal
    if name == "ema":
        return EmaCrossover(cfg.fast_ema, cfg.slow_ema)
    if name == "supertrend":
        return SupertrendStrategy(cfg.st_period, cfg.st_mult)
    if name == "rsi":
        return RsiMomentum(cfg.rsi_period, cfg.rsi_buy, cfg.rsi_sell)
    if name == "vwap":
        return VwapCross()
    if name == "orb":
        return OpeningRangeBreakout(cfg.orb_minutes)
    raise ValueError(f"Unknown SIGNAL '{name}', choose from {', '.join(SIGNALS)}")
