from datetime import date

import pytest

from dhan_autotrader.broker import DhanBroker, PaperBroker
from dhan_autotrader.options import (PRESETS, IndexOptionsBot, OptionChainClient, OptionSettings, OptionsRunner,
                                     parse_indices)
from dhan_autotrader.strategy import EmaCrossover

from test_bot import Clock, FakeDhan, make_cfg

BULL = [25000 - i * 10 for i in range(20)] + [25100]   # bullish cross on last bar
BEAR = [25000 + i * 10 for i in range(20)] + [24900]   # bearish cross on last bar


class FakeOptDhan(FakeDhan):
    def __init__(self):
        super().__init__()
        self.spot = 25112.0
        self.prices = {"NSE_FNO": {}, "BSE_FNO": {}, "IDX_I": {}}
        self.chains = {}       # security_id -> (spot, oc) for indices other than NIFTY
        self.closes_by = {}    # security_id -> closes (index candles)
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
        spot, oc = self.chains.get(str(sid), (self.spot, self.oc))
        return {"status": "success", "remarks": "", "data": {"data": {"last_price": spot, "oc": oc}}}

    def intraday_minute_data(self, **kw):
        saved = self.closes
        self.closes = self.closes_by.get(str(kw["security_id"]), saved)
        try:
            return super().intraday_minute_data(**kw)
        finally:
            self.closes = saved

    def add_index(self, sid, seg, spot, strikes, ce=200.0, pe=190.0):
        oc = {}
        for k in strikes:
            c, p = f"7{sid}{k}", f"8{sid}{k}"
            oc[f"{k}.000000"] = {"ce": {"security_id": int(c), "last_price": ce},
                                 "pe": {"security_id": int(p), "last_price": pe}}
            self.prices[seg][c], self.prices[seg][p] = ce, pe
        self.chains[str(sid)] = (spot, oc)

    def ticker_data(self, securities):
        (seg, ids), = securities.items()
        sid = str(ids[0])
        return {"status": "success", "remarks": "", "data": {"data": {seg: {sid: {"last_price": self.prices[seg][sid]}}}}}

    def place_order(self, **kw):
        self.ltp = self.prices[kw["exchange_segment"]][str(kw["security_id"])]
        return super().place_order(**kw)


def make_bot(fake, clock, live=True, **opt):
    opt.setdefault("strike_offset", 0)
    opt.setdefault("trail_trigger_pct", 0)
    cfg = make_cfg(capital_per_trade=20000, max_daily_loss=5000, mode="live" if live else "paper")
    s = OptionSettings(**opt)
    s.validate()
    broker = (DhanBroker if live else PaperBroker)(cfg, client=fake)
    return IndexOptionsBot(cfg, s, PRESETS["NIFTY"], broker, EmaCrossover(3, 6), clock=clock)


def test_strike_selection_atm_otm_and_expiry_roll():
    fake = FakeOptDhan()
    bot = make_bot(fake, Clock(), strike_offset=0)
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
    c = make_bot(fake, Clock(), strike_offset=0).chain.select("CE", date(2026, 11, 19))
    assert c.strike in (25050, 25150)


def test_bullish_cross_buys_call_and_target_exits():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock, max_lots=3)
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
    bot = make_bot(fake, Clock(), max_lots=1)
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
        OptionSettings(max_lots=0).validate()


def test_paper_mode_places_no_real_orders():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock, live=False)
    bot.tick()
    assert bot.position and bot.position.pos.entry == 120.0
    assert fake.orders == []


def test_otm_is_default_and_never_falls_back_to_itm():
    assert OptionSettings().strike_offset == 1
    fake = FakeOptDhan()                       # spot 25112
    bot = make_bot(fake, Clock(), strike_offset=1)
    today = date(2026, 11, 19)
    assert bot.chain.select("CE", today).strike == 25150   # above spot
    assert bot.chain.select("PE", today).strike == 25050   # below spot

    # 25150 CE illiquid -> must move further OTM (25200), not ITM (25100)
    fake.oc["25150.000000"]["ce"]["last_price"] = 1.0
    assert bot.chain.select("CE", today).strike == 25200
    # 25050 PE illiquid -> 25000, never 25100
    fake.oc["25050.000000"]["pe"]["last_price"] = 1.0
    assert bot.chain.select("PE", today).strike == 25000


def test_otm_trade_end_to_end():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock, strike_offset=2)
    bot.tick()
    assert bot.position.contract.kind == "CE" and bot.position.contract.strike == 25200

    fake.closes = BULL + [24700]                 # next bar: bearish cross
    bot.tick()
    assert bot.position.contract.kind == "PE" and bot.position.contract.strike == 25000  # 2 strikes below 25100
    assert [o["transaction_type"] for o in fake.orders] == ["BUY", "SELL", "BUY"]


# ------------------------------------------------------------- trailing stop
def test_trailing_stop_locks_profit():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock, trail_trigger_pct=20, trail_pct=15, target_pct=0)
    bot.tick()
    pos = bot.position.pos
    assert pos.entry == 120 and pos.stop == 90 and pos.target == float("inf")

    fake.prices["NSE_FNO"]["125100"] = 150.0      # +25% -> trail active
    bot.tick()
    assert pos.trailing and pos.stop == 127.5     # 150 * 0.85
    fake.prices["NSE_FNO"]["125100"] = 200.0
    bot.tick()
    assert pos.stop == 170.0
    fake.prices["NSE_FNO"]["125100"] = 180.0      # pullback doesn't lower the stop
    bot.tick()
    assert bot.position and pos.stop == 170.0
    fake.prices["NSE_FNO"]["125100"] = 169.0
    bot.tick()
    assert bot.position is None
    assert bot.risk.realized_pnl == pytest.approx((169 - 120) * 65)


def test_trailing_moves_to_breakeven_first():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    bot = make_bot(fake, clock, trail_trigger_pct=20, trail_pct=30)
    bot.tick()
    fake.prices["NSE_FNO"]["125100"] = 145.0      # +20.8%; 145*0.7=101.5 < entry -> breakeven
    bot.tick()
    assert bot.position.pos.stop == 120.0


# ------------------------------------------------------------- multi-index
def test_parse_indices_and_overrides():
    specs = parse_indices("nifty, BANKNIFTY,SENSEX", env={"OPT_LOT_SIZE_BANKNIFTY": "35"})
    assert [s.name for s in specs] == ["NIFTY", "BANKNIFTY", "SENSEX"]
    assert specs[1].lot_size == 35 and specs[1].strike_step == 100
    assert specs[2].fno_segment == "BSE_FNO"
    with pytest.raises(ValueError):
        parse_indices("DOWJONES")


def test_runner_trades_multiple_indices_with_shared_limits():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL                                    # NIFTY bullish
    fake.add_index(25, "NSE_FNO", 56040.0, range(55800, 56400, 100), pe=150.0)
    fake.closes_by["25"] = BEAR                          # BANKNIFTY bearish
    fake.add_index(51, "BSE_FNO", 82030.0, range(81800, 82400, 100))
    fake.closes_by["51"] = BULL                          # SENSEX bullish
    cfg = make_cfg(capital_per_trade=20000, max_daily_loss=50000, mode="live", max_open_positions=2)
    s = OptionSettings(indices=tuple(parse_indices("NIFTY,BANKNIFTY,SENSEX", env={})), strike_offset=1, max_lots=5)
    runner = OptionsRunner(cfg, s, DhanBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock)
    runner.tick()

    nifty, bank, sensex = runner.bots
    assert nifty.position.contract.strike == 25150 and nifty.position.contract.kind == "CE"
    assert bank.position.contract.kind == "PE" and bank.position.contract.strike == 55900   # OTM put
    assert bank.position.pos.qty == 30 * 4                                                  # 20000/(150*30)=4 lots
    assert sensex.position is None                       # MAX_OPEN_POSITIONS=2 shared
    assert runner.risk.trades_today == 2


def test_sensex_options_use_bse_fno():
    fake, clock = FakeOptDhan(), Clock()
    fake.add_index(51, "BSE_FNO", 82030.0, range(81800, 82400, 100))
    fake.closes_by["51"] = BULL
    cfg = make_cfg(capital_per_trade=20000, mode="live")
    s = OptionSettings(indices=(PRESETS["SENSEX"],), strike_offset=1)
    runner = OptionsRunner(cfg, s, DhanBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock)
    runner.tick()
    op = runner.bots[0].position
    assert op.contract.strike == 82100 and op.contract.inst.exchange_segment == "BSE_FNO"
    assert fake.orders[-1]["exchange_segment"] == "BSE_FNO" and op.pos.qty == 20 * 1


def test_runner_combined_daily_loss_squares_off_everything():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    fake.add_index(25, "NSE_FNO", 56040.0, range(55800, 56400, 100), pe=150.0)
    fake.closes_by["25"] = BEAR
    cfg = make_cfg(capital_per_trade=20000, max_daily_loss=3000, mode="live", max_open_positions=3)
    s = OptionSettings(indices=tuple(parse_indices("NIFTY,BANKNIFTY", env={})), strike_offset=1, max_lots=5,
                       sl_pct=90, trail_trigger_pct=0)
    runner = OptionsRunner(cfg, s, DhanBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock)
    runner.tick()
    assert runner.open_count() == 2
    # each loses less than the limit alone, but together exceed it
    fake.prices["NSE_FNO"]["125150"] = 100.0            # -20*130 = -2600
    fake.prices["NSE_FNO"]["82555900"] = 145.0          # -5*120  = -600
    runner.tick()
    assert runner.open_count() == 0 and runner.risk.halted
