"""Trading strategies. A strategy turns a list of closing prices into a signal."""
from __future__ import annotations

from enum import Enum


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average; output has the same length as input."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


class Strategy:
    name = "base"
    min_bars = 1

    def signal(self, closes: list[float]) -> Signal:  # pragma: no cover - interface
        raise NotImplementedError


class EmaCrossover(Strategy):
    """BUY when fast EMA crosses above slow EMA, SELL when it crosses below.

    Only the crossover bar produces a signal, so the bot does not re-enter
    repeatedly while the trend persists.
    """

    name = "ema_crossover"

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
