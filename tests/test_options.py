from datetime import date

import pytest

from dhan_autotrader.broker import DhanBroker, PaperBroker
from dhan_autotrader.options import NiftyOptionsBot, OptionChainClient, OptionSettings
from dhan_autotrader.strategy import EmaCrossover

from test_bot import Clock, FakeDhan, make_cfg

BULL = [25000 - i * 10 for i in range(20)] + [25100]   # bullish cross on last bar
BEAR = [25000 + i * 10 for i in range(20)] + [24900]   # bearish cross on last bar


class FakeOptDhan(FakeDhan):
    def __init__(self):
        super().__init__()
        self.spot = 25112.0
        self.prices = {"NSE_FNO": {}, "IDX_I": {}}
        self.expiries = ["2026-11-18", "2026-11-25", "2026-12-02"]
        self.chain_expiry = None
        # strikes 25000..25200, CE ids 1xxxx, PE ids 2xxxx
        self.oc = {}
        for k in range(25000, 25250, 50):
            ce, pe = f"1{k}", f"2{k}"
            self.oc[f"{k}.000000"] = {"ce": {"security_id": int(ce), "last_price": 120.0},
                                     "pe": {"security_id": int(pe), "last_price": 110.0}}
            self.prices["NSE_FNO"][ce] = 120.0
            self.prices["NSE_FNO"][pe] = 110.0

    def expiry_list(self, sid, seg):
        return {"status": "success", "remarks": "", "data": {"data": self.expiries, "status": "success"}}

    def option_chain(self, sid, seg, expiry):
        self.chain_expiry = expiry
        return {"status": "success", "remarks": "", "data": {"data": {"last_price": self.spot, "oc": self.oc}}}

    def ticker_data(self, securities):
        (seg, ids), = securities.items()
        sid = str(ids[0])
        return {"status": "success", "remarks": "", "data": {"data": {seg: {sid: {"last_price": self.prices[seg][sid]}}}}}

    def place_order(self, **kw):
        self.ltp = self.prices[kw["exchange_segment"]][str(kw["security_id"])]
        return super().place_order(**kw)


def make_bot(fake, clock, live=True, **opt):
    cfg = make_cfg(capital_per_trade=20000, max_daily_loss=5000, mode="live" if live else "paper")
    s = OptionSettings(**opt)
    s.validate()
    broker = (DhanBroker if live else PaperBroker)(cfg, client=fake)
    chain = OptionChainClient(broker, s)
    chain.MIN_INTERVAL = 0
    return NiftyOptionsBot(cfg, s, broker, EmaCrossover(3, 6), chain=chain, clock=clock)


def test_strike_selection_atm_otm_and_expiry_roll():
    fake = FakeOptDhan()
    bot = make_bot(fake, Clock())
    today = date(2026, 11, 18)  # an expiry day -> rolls to next
    c = bot.chain.select("CE", today)
    assert c.strike == 25100 and c.kind == "CE" and c.inst.security_id == "125100"
    assert c.inst.exchange_segment == "NSE_FNO" and fake.chain_expiry == "2026-11-25"

    bot.s.strike_offset = 1
    assert bot.chain.select("CE", today).strike == 25150   # OTM call is higher
    assert bot.chain.select("PE", today).strike == 25050   # OTM put is lower

    bot.s.skip_expiry_day = False
    bot.chain.select("PE", today)
    assert fake.chain_expiry == "2026-11-18"


def test_illiquid_strike_skipped():
    fake = FakeOptDhan()
    fake.oc["25100.000000"]["ce"]["last_price"] = 2.0
    c = make_bot(fake, Clock()).chain.select("CE", date(2026, 11, 19))
    assert c.strike in (25050, 25150)


def test_bullish_cross_buys_call_and_target_exits():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock, lot_size=65, max_lots=3)
    bot.tick()
    op = bot.position
    assert op.contract.kind == "CE" and op.pos.qty == 130  # 20000 / (120*65) = 2 lots
    assert op.pos.stop == 90.0 and op.pos.target == 180.0
    assert fake.orders[-1]["transaction_type"] == "BUY"
    assert fake.orders[-1]["exchange_segment"] == "NSE_FNO"

    fake.prices["NSE_FNO"]["125100"] = 185.0
    bot.tick()
    assert bot.position is None
    assert fake.orders[-1]["transaction_type"] == "SELL"
    assert bot.risk.realized_pnl == pytest.approx((185 - 120) * 130)


def test_bearish_cross_buys_put_and_flip_on_opposite_signal():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BEAR
    bot = make_bot(fake, clock)
    bot.tick()
    assert bot.position.contract.kind == "PE"

    fake.closes = [25000 - i * 10 for i in range(21)] + [25150]
    bot.tick()
    assert bot.position.contract.kind == "CE"
    sides = [o["transaction_type"] for o in fake.orders]
    assert sides == ["BUY", "SELL", "BUY"]


def test_lot_cap_and_too_expensive():
    fake = FakeOptDhan()
    bot = make_bot(fake, Clock(), lot_size=65, max_lots=1)
    assert bot.lots_for(100) == 1
    bot.cfg.capital_per_trade = 5000
    assert bot.lots_for(100) == 0  # 1 lot = 6500 > 5000


def test_daily_loss_limit_exits_open_option():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock, max_lots=1, sl_pct=90)
    bot.cfg.max_daily_loss = 1000
    bot.tick()
    assert bot.position
    fake.prices["NSE_FNO"]["125100"] = 100.0   # -20 * 65 = -1300
    bot.tick()
    assert bot.position is None and bot.risk.halted


def test_square_off_end_of_day():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock)
    bot.tick()
    assert bot.position
    clock.now = clock.now.replace(hour=15, minute=16)
    bot.tick()
    assert bot.position is None


def test_settings_validation():
    with pytest.raises(ValueError):
        OptionSettings(sl_pct=100).validate()
    with pytest.raises(ValueError):
        OptionSettings(lot_size=0).validate()


def test_paper_mode_places_no_real_orders():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock, live=False)
    bot.tick()
    assert bot.position and bot.position.pos.entry == 120.0
    assert fake.orders == []
