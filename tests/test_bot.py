from datetime import datetime, timedelta

import pytest

from dhan_autotrader.bot import TradingBot
from dhan_autotrader.broker import IST, BrokerError, DhanBroker, PaperBroker
from dhan_autotrader.config import Config, Instrument, parse_instruments
from dhan_autotrader.risk import RiskManager
from dhan_autotrader.strategy import EmaCrossover, Signal, ema

INST = Instrument("TEST", "1333", "NSE_EQ")


def make_cfg(**kw):
    base = dict(client_id="1", access_token="t", instruments=[INST], fast_ema=3, slow_ema=6,
                capital_per_trade=10000, max_daily_loss=500, max_trades_per_day=3)
    base.update(kw)
    cfg = Config(**base)
    cfg.validate()
    return cfg


class FakeDhan:
    """Mimics the dhanhq SDK response envelope."""

    def __init__(self):
        self.closes = []
        self.ltp = 100.0
        self.orders = []
        self.order_status = "TRADED"
        self.t0 = datetime(2026, 11, 18, 9, 15, tzinfo=IST)

    def get_fund_limits(self):
        return {"status": "success", "remarks": "", "data": {"availabelBalance": 50000}}

    def intraday_minute_data(self, **kw):
        ts = [int((self.t0 + timedelta(minutes=5 * i)).timestamp()) for i in range(len(self.closes))]
        return {"status": "success", "remarks": "", "data": {
            "timestamp": ts, "open": self.closes, "high": self.closes, "low": self.closes,
            "close": self.closes, "volume": [1] * len(self.closes)}}

    def ticker_data(self, securities):
        return {"status": "success", "remarks": "", "data": {"data": {"NSE_EQ": {"1333": {"last_price": self.ltp}}}}}

    def place_order(self, **kw):
        self.orders.append(kw)
        return {"status": "success", "remarks": "", "data": {"orderId": str(len(self.orders)), "orderStatus": "TRANSIT"}}

    def get_order_by_id(self, oid):
        return {"status": "success", "remarks": "", "data": {"orderStatus": self.order_status,
                                                             "averageTradedPrice": self.ltp}}

    def exit_all_positions(self):
        return {"status": "success", "remarks": "", "data": {}}


class Clock:
    def __init__(self, h=13, m=0):
        self.now = datetime(2026, 11, 18, h, m, tzinfo=IST)

    def __call__(self):
        return self.now


def test_ema_and_crossover():
    assert ema([1, 1, 1], 3) == [1, 1, 1]
    s = EmaCrossover(3, 6)
    down_then_up = [100 - i * 0.5 for i in range(20)] + [95]
    assert s.signal(down_then_up) is Signal.BUY
    up_then_down = [100 + i * 0.5 for i in range(20)] + [105]
    assert s.signal(up_then_down) is Signal.SELL
    assert s.signal([100.0] * 5) is Signal.HOLD


def test_parse_instruments_and_validation():
    assert parse_instruments("A:1, B:2:BSE_EQ") == [Instrument("A", "1", "NSE_EQ"), Instrument("B", "2", "BSE_EQ")]
    with pytest.raises(ValueError):
        make_cfg(fast_ema=10, slow_ema=5)
    with pytest.raises(ValueError):
        make_cfg(mode="yolo")


def test_risk_limits():
    r = RiskManager(make_cfg())
    assert r.quantity(250) == 40
    assert r.brackets("BUY", 100) == (99.0, 102.0)
    assert r.brackets("SELL", 100) == (101.0, 98.0)
    r.record_exit(-600)
    assert r.halted and not r.can_open(0)[0]


def _bot(fake, clock, **kw):
    cfg = make_cfg(**kw)
    return TradingBot(cfg, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock)


def test_buy_signal_opens_then_stop_loss_closes():
    fake, clock = FakeDhan(), Clock()
    fake.closes = [100 - i * 0.5 for i in range(20)] + [95]
    fake.ltp = 100.0
    bot = _bot(fake, clock)
    bot.tick()
    pos = bot.positions["TEST"]
    assert pos.side == "BUY" and pos.qty == 100 and pos.stop == 99.0

    bot.tick()  # same bar -> no duplicate entry
    assert bot.risk.trades_today == 1

    fake.ltp = 98.5
    bot.tick()
    assert "TEST" not in bot.positions
    assert bot.risk.realized_pnl == pytest.approx(-150)


def test_short_disabled_by_default():
    fake, clock = FakeDhan(), Clock()
    fake.closes = [100 + i * 0.5 for i in range(20)] + [105]
    bot = _bot(fake, clock)
    bot.tick()
    assert not bot.positions


def test_no_entries_outside_window_and_square_off():
    fake, clock = FakeDhan(), Clock(9, 20)
    fake.closes = [100 - i * 0.5 for i in range(20)] + [95]
    bot = _bot(fake, clock)
    bot.tick()
    assert not bot.positions  # before 09:30

    clock.now = clock.now.replace(hour=13, minute=0)
    bot.tick()
    assert "TEST" in bot.positions

    clock.now = clock.now.replace(hour=15, minute=16)
    bot.tick()
    assert not bot.positions


def test_live_broker_places_intraday_market_order_and_handles_reject():
    fake = FakeDhan()
    cfg = make_cfg(mode="live")
    broker = DhanBroker(cfg, client=fake)
    fill = broker.market_order(INST, "BUY", 5, tag="x")
    assert fill.price == 100.0
    o = fake.orders[0]
    assert o["product_type"] == "INTRADAY" and o["order_type"] == "MARKET" and o["quantity"] == 5

    fake.order_status = "REJECTED"
    with pytest.raises(BrokerError):
        broker.market_order(INST, "BUY", 5, tag="x")


def test_forming_candle_is_dropped():
    fake = FakeDhan()
    fake.t0 = datetime.now(IST) - timedelta(minutes=10)
    fake.closes = [1, 2, 3]  # last bar starts "now" -> still forming
    candles = PaperBroker(make_cfg(), client=fake).candles(INST, 5)
    assert len(candles) == 2
