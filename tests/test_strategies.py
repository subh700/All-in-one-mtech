import threading
from datetime import datetime, timedelta

import pytest

from dhan_autotrader.broker import IST, Candle
from dhan_autotrader.notify import Notifier, TelegramNotifier, build_notifier
from dhan_autotrader.risk import Position
from dhan_autotrader.strategy import (OpeningRangeBreakout, RsiMomentum, Signal, SupertrendStrategy, VwapCross,
                                      build_strategy, rsi, supertrend)

from test_bot import make_cfg

DAY = datetime(2026, 11, 18, 9, 15, tzinfo=IST)


def bars(closes, start=DAY, step=5, spread=2.0, volume=0):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle(start + timedelta(minutes=step * i), o, max(o, c) + spread, min(o, c) - spread, c, volume))
    return out


def first_signal(strategy, candles):
    """Walk bar by bar and return (index, signal) of the first non-HOLD signal."""
    for i in range(2, len(candles) + 1):
        s = strategy.on_candles(candles[:i])
        if s is not Signal.HOLD:
            return i - 1, s
    return None, Signal.HOLD


# ---------------------------------------------------------------- supertrend
def test_supertrend_flips_up_then_down():
    down = [100 - i for i in range(40)]
    up = [61 + i * 2 for i in range(30)]
    candles = bars(down + up)
    d = supertrend(candles, 10, 3)
    assert d[39] == -1 and d[-1] == 1
    idx, sig = first_signal(SupertrendStrategy(10, 3), candles)
    assert sig is Signal.BUY and idx > 40

    crash = [up[-1] - i * 3 for i in range(1, 30)]
    idx, sig = first_signal(SupertrendStrategy(10, 3), bars(up * 2 + crash)[30:])
    assert sig is Signal.SELL


# ---------------------------------------------------------------- RSI
def test_rsi_values_and_momentum_cross():
    assert rsi([float(i) for i in range(30)], 14)[-1] == 100.0
    flat = [100.0, 100.5] * 25
    surge = [100 + i * 1.5 for i in range(1, 10)]
    idx, sig = first_signal(RsiMomentum(14, 60, 40), bars(flat + surge))
    assert sig is Signal.BUY and idx >= len(flat)
    dump = [100 - i * 1.5 for i in range(1, 10)]
    assert first_signal(RsiMomentum(14, 60, 40), bars(flat + dump))[1] is Signal.SELL
    with pytest.raises(ValueError):
        RsiMomentum(14, 40, 60)


# ---------------------------------------------------------------- VWAP
def test_vwap_cross_uses_today_only_and_twap_for_indices():
    yesterday = bars([500.0] * 20, start=DAY - timedelta(days=1))  # must be ignored
    today = bars([100, 99, 98, 97, 96, 103])
    candles = yesterday + today
    assert VwapCross().on_candles(candles) is Signal.BUY
    assert VwapCross().on_candles(yesterday + bars([100, 101, 102, 103, 104, 96])) is Signal.SELL
    # zero volume -> equal weights
    v = VwapCross.vwap_series(bars([10, 20], spread=0))
    assert v[-1] == pytest.approx((10 + (20 + 10 + 20) / 3) / 2)   # typical price = (H+L+C)/3
    # with volume -> weighted
    tv = bars([10, 20], spread=0)
    tv[1].volume, tv[0].volume = 3, 1
    assert VwapCross.vwap_series(tv)[-1] == pytest.approx((10 * 1 + 50 / 3 * 3) / 4)


# ---------------------------------------------------------------- ORB
def test_opening_range_breakout():
    s = OpeningRangeBreakout(15)
    rng = bars([100, 104, 98], spread=1)  # 09:15-09:30 range: high 105, low 97
    assert s.on_candles(rng) is Signal.HOLD                          # range still forming
    assert s.on_candles(rng + bars([102], start=DAY + timedelta(minutes=15))) is Signal.HOLD
    up = rng + bars([102, 106], start=DAY + timedelta(minutes=15), spread=0)
    assert s.on_candles(up) is Signal.BUY
    assert s.on_candles(up + bars([107], start=DAY + timedelta(minutes=25), spread=0)) is Signal.HOLD  # no repeat
    down = rng + bars([96], start=DAY + timedelta(minutes=15), spread=0)
    assert s.on_candles(down) is Signal.SELL


def test_build_strategy_and_config_validation():
    for name, cls in [("supertrend", SupertrendStrategy), ("rsi", RsiMomentum), ("vwap", VwapCross),
                      ("orb", OpeningRangeBreakout)]:
        assert isinstance(build_strategy(make_cfg(signal=name)), cls)
    with pytest.raises(ValueError):
        make_cfg(signal="magic")
    with pytest.raises(ValueError):
        make_cfg(signal="orb", orb_minutes=20, candle_interval=15)


# ---------------------------------------------------------------- trailing (short side)
def test_short_trailing_stop():
    p = Position("X", "SELL", 10, 100.0, 101.0, 98.0 - 100, DAY)
    assert not p.update_trail(99.5, 1.0, 0.5)
    assert p.update_trail(98.0, 1.0, 0.5)          # +2% in our favour
    assert p.stop == 98.49 and p.exit_reason(98.6) == "trailing-stop"
    assert Position("Y", "BUY", 1, 100, 99, 200, DAY).update_trail(150, 0, 10) is False  # disabled


# ---------------------------------------------------------------- Telegram
def test_telegram_sends_async_and_dedupes():
    sent, done = [], threading.Event()

    def post(payload):
        sent.append(payload)
        if len(sent) == 2:
            done.set()

    n = TelegramNotifier("TOKEN", "42", prefix="[PAPER] ", post=post)
    n.send("hello")
    n.send("err", key="e1")
    n.send("err", key="e1")      # suppressed
    n.close()
    assert done.wait(2)
    assert [p["text"] for p in sent] == ["[PAPER] hello", "[PAPER] err"]
    assert sent[0]["chat_id"] == "42"
    assert "botTOKEN/sendMessage" in n.url


def test_telegram_failure_never_raises():
    def boom(payload):
        raise OSError("network down")

    n = TelegramNotifier("T", "1", post=boom)
    n.send("x")
    n.close()  # no exception


def test_build_notifier():
    assert type(build_notifier(make_cfg())) is Notifier
    n = build_notifier(make_cfg(telegram_token="t", telegram_chat_id="1", mode="live"))
    assert isinstance(n, TelegramNotifier) and n.prefix == "[LIVE] "
    n.close()
