import csv
import json
from datetime import date, datetime, time, timedelta

import pytest

from dhan_autotrader.backtest import BtParams, BtResult, BtTrade, format_table, run_backtest, simulate
from dhan_autotrader.bot import TradingBot
from dhan_autotrader.broker import IST, Candle, DhanBroker, PaperBroker
from dhan_autotrader.journal import StateStore, TradeJournal
from dhan_autotrader.login import get_access_token, totp
from dhan_autotrader.options import OptionSettings, OptionsRunner, parse_indices
from dhan_autotrader.strategy import Confirmed, EmaCrossover, RsiMomentum, Signal, Strategy, build_strategy

from test_bot import INST, Clock, FakeDhan, make_cfg
from test_options import BULL, FakeOptDhan

D0 = datetime(2026, 11, 18, 9, 15, tzinfo=IST)


# ================================================================ auto-login
def test_totp_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890")
    assert totp(secret, at=59, digits=8) == "94287082"
    assert totp(secret, at=1111111109, digits=8) == "07081804"
    assert totp(secret.lower(), at=59) == "287082"


def test_access_token_generated_then_cached(tmp_path, monkeypatch):
    import dhan_autotrader.login as login_mod

    monkeypatch.setattr(login_mod.time, "sleep", lambda s: None)
    calls = []

    class FakeLogin:
        def generate_token(self, pin, code):
            calls.append((pin, code))
            return {"accessToken": "TOKEN-1", "expiryTime": "..."}

    cache = tmp_path / "token.json"
    now = datetime(2026, 11, 18, 9, 0, tzinfo=IST)
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert get_access_token("111", "1234", secret, str(cache), login=FakeLogin(), now=now) == "TOKEN-1"
    assert get_access_token("111", "1234", secret, str(cache), login=FakeLogin(), now=now) == "TOKEN-1"
    assert len(calls) == 1 and calls[0][0] == "1234" and len(calls[0][1]) == 6
    assert oct(cache.stat().st_mode)[-3:] == "600"
    # next day -> fresh login
    get_access_token("111", "1234", secret, str(cache), login=FakeLogin(), now=now + timedelta(days=1))
    assert len(calls) == 2


def test_config_accepts_totp_instead_of_token():
    make_cfg(access_token="", pin="1234", totp_secret="ABC")
    with pytest.raises(ValueError):
        make_cfg(access_token="", pin="1234")


# ================================================================ confirmation
class Fixed(Strategy):
    def __init__(self, sig=Signal.HOLD, b=0, name="x"):
        self.sig, self.b, self.name = sig, b, name

    def on_candles(self, candles):
        return self.sig

    def bias(self, candles):
        return self.b


def test_confirmed_requires_all_filters_to_agree():
    c = Confirmed(Fixed(Signal.BUY, name="st"), [Fixed(b=1, name="rsi"), Fixed(b=1, name="vwap")])
    assert c.on_candles([]) is Signal.BUY and c.name == "st+rsi+vwap"
    assert Confirmed(Fixed(Signal.BUY), [Fixed(b=1), Fixed(b=-1)]).on_candles([]) is Signal.HOLD
    assert Confirmed(Fixed(Signal.SELL), [Fixed(b=-1)]).on_candles([]) is Signal.SELL
    assert Confirmed(Fixed(Signal.SELL), [Fixed(b=0)]).on_candles([]) is Signal.HOLD


def test_build_combined_strategy_and_bias():
    s = build_strategy(make_cfg(signal="supertrend, rsi"))
    assert isinstance(s, Confirmed) and s.name == "supertrend+rsi"
    with pytest.raises(ValueError):
        make_cfg(signal="ema,bogus")
    up = [Candle(D0 + timedelta(minutes=5 * i), 100 + i, 101 + i, 99 + i, 100 + i, 0) for i in range(60)]
    assert EmaCrossover(3, 6).bias(up) == 1 and RsiMomentum().bias(up) == 1


# ================================================================ journal + state
def test_journal_and_crash_recovery_equity(tmp_path):
    fake, clock = FakeDhan(), Clock()
    fake.closes = [100 - i * 0.5 for i in range(20)] + [95]
    fake.ltp = 100.0
    cfg = make_cfg()
    journal = TradeJournal(str(tmp_path / "trades.csv"), mode="paper", strategy="equity:ema")
    store = StateStore(str(tmp_path / "state.json"))
    bot = TradingBot(cfg, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, journal=journal, store=store)
    bot.tick()
    assert "TEST" in bot.positions
    assert json.loads((tmp_path / "state.json").read_text())["positions"]["TEST"]["qty"] == 100

    # "crash" -> new bot instance restores the position and today's counters
    bot2 = TradingBot(cfg, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, journal=journal, store=store)
    assert bot2.restore() == 1 and bot2.risk.trades_today == 1
    fake.ltp = 98.5
    bot2.tick()                                    # stop-loss
    assert not bot2.positions
    rows = list(csv.DictReader(open(tmp_path / "trades.csv")))
    assert len(rows) == 1 and rows[0]["symbol"] == "TEST" and float(rows[0]["pnl"]) == -150.0
    assert rows[0]["reason"] == "stop-loss" and rows[0]["strategy"] == "equity:ema"

    # yesterday's state is ignored
    clock.now += timedelta(days=1)
    assert TradingBot(cfg, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, store=store).restore() == 0


def test_live_restore_drops_positions_closed_at_broker(tmp_path):
    fake, clock = FakeDhan(), Clock()
    fake.closes = [100 - i * 0.5 for i in range(20)] + [95]
    store = StateStore(str(tmp_path / "s.json"))
    cfg = make_cfg(mode="live")
    TradingBot(cfg, DhanBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, store=store).tick()

    fake.get_positions = lambda: {"status": "success", "remarks": "", "data": []}
    assert TradingBot(cfg, DhanBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, store=store).restore() == 0

    fake.closes = [100 - i * 0.5 for i in range(20)] + [95]
    TradingBot(cfg, DhanBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, store=store).tick()
    fake.get_positions = lambda: {"status": "success", "remarks": "",
                                  "data": [{"securityId": "1333", "netQty": 50}]}
    b = TradingBot(cfg, DhanBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, store=store)
    assert b.restore() == 1 and b.positions["TEST"].qty == 50


def test_options_runner_state_roundtrip(tmp_path):
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    cfg = make_cfg(capital_per_trade=20000)
    s = OptionSettings(indices=tuple(parse_indices("NIFTY", env={})), strike_offset=1)
    store = StateStore(str(tmp_path / "opt.json"))
    r = OptionsRunner(cfg, s, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, store=store)
    r.tick()
    assert r.open_count() == 1
    fake.prices["NSE_FNO"]["125150"] = 150.0       # trail activates -> stop saved
    r.tick()

    r2 = OptionsRunner(cfg, s, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, store=store)
    assert r2.restore() == 1
    op = r2.bots[0].position
    assert op.contract.strike == 25150 and op.contract.inst.exchange_segment == "NSE_FNO"
    assert op.pos.trailing and op.pos.stop == 127.5 and r2.risk.trades_today == 1


# ================================================================ backtest
def day_bars(day: date, closes, step=5, start=time(9, 15)):
    t0 = datetime.combine(day, start, IST)
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle(t0 + timedelta(minutes=step * i), o, max(o, c) + 0.5, min(o, c) - 0.5, c, 0))
    return out


class Scripted(Strategy):
    """Emits given signals at given bar timestamps."""
    name = "scripted"

    def __init__(self, at):
        self.at = at

    def on_candles(self, candles):
        return self.at.get(candles[-1].ts, Signal.HOLD)


def test_simulate_enters_next_open_and_hits_target_stop_and_squareoff():
    d = date(2026, 11, 17)
    closes = [100.0] * 4 + [100, 101, 102, 103] + [103] * 70
    bars = day_bars(d, closes)
    p = BtParams(sl_pct=1, target_pct=2, max_trades_per_day=5)
    st = Scripted({bars[3].ts: Signal.BUY})            # 09:30 bar closes -> enter at 09:35 open
    trades = simulate("X", bars, st, p, lambda price: 10)
    t = trades[0]
    assert t.entry_ts == bars[4].ts and t.entry == bars[4].open
    assert t.reason == "target" and t.exit == pytest.approx(102.0) and t.pnl == pytest.approx(20)

    falling = day_bars(d, [100.0] * 5 + [99.5, 98.5, 98] + [98] * 70)
    t = simulate("X", falling, Scripted({falling[3].ts: Signal.BUY}), p, lambda price: 10)[0]
    assert t.reason == "stop-loss" and t.exit == pytest.approx(99.0)

    flat = day_bars(d, [100.0] * 80)
    t = simulate("X", flat, Scripted({flat[3].ts: Signal.BUY}), BtParams(sl_pct=5, target_pct=5), lambda _: 1)[0]
    assert t.reason == "end-of-day square-off" and t.exit_ts.time() == time(15, 15)


def test_simulate_respects_shorts_window_and_opposite_signal():
    d = date(2026, 11, 17)
    bars = day_bars(d, [100.0] * 80)
    p = BtParams(sl_pct=5, target_pct=5, allow_short=False)
    assert simulate("X", bars, Scripted({bars[3].ts: Signal.SELL}), p, lambda _: 1) == []
    assert simulate("X", bars, Scripted({bars[0].ts: Signal.BUY}), p, lambda _: 1) == []   # 09:20 < ENTRY_START
    p.allow_short = True
    tr = simulate("X", bars, Scripted({bars[3].ts: Signal.BUY, bars[10].ts: Signal.SELL}), p, lambda _: 1)
    assert [t.side for t in tr[:2]] == ["BUY", "SELL"] and tr[0].reason == "opposite signal SELL"


def test_simulate_trailing_stop():
    d = date(2026, 11, 17)
    bars = day_bars(d, [100.0] * 4 + [100, 102, 104, 106, 103, 101] + [101] * 60)
    p = BtParams(sl_pct=1, target_pct=0, trail_trigger_pct=2, trail_pct=1.5)
    t = simulate("X", bars, Scripted({bars[3].ts: Signal.BUY}), p, lambda _: 1)[0]
    assert t.reason == "trailing-stop" and t.exit > 100


def test_result_metrics_and_table():
    mk = lambda pnl: BtTrade("X", "BUY", D0, 100, D0, 100 + pnl, "t", 1, pnl)
    r = BtResult("ema", [mk(100), mk(-50), mk(200), mk(-100)])
    assert r.total == 150 and r.win_rate == 50 and r.profit_factor == pytest.approx(2.0)
    assert r.max_drawdown == 100
    assert "profit factor" in format_table([r])


def test_run_backtest_end_to_end(tmp_path, monkeypatch):
    import dhan_autotrader.broker as broker_mod

    monkeypatch.setattr(broker_mod._time, "sleep", lambda s: None)
    class HistFake(FakeOptDhan):
        def intraday_minute_data(self, **kw):
            days = [date(2026, 11, 16) + timedelta(days=i) for i in range(2)]
            closes = []
            for _ in days:
                closes += [25000 - i * 8 for i in range(30)] + [24800 + i * 15 for i in range(45)]
            bars = []
            for d in days:
                bars += day_bars(d, closes[:75])
            return {"status": "success", "remarks": "", "data": {
                "timestamp": [int(b.ts.timestamp()) for b in bars], "open": [b.open for b in bars],
                "high": [b.high for b in bars], "low": [b.low for b in bars], "close": [b.close for b in bars],
                "volume": [0] * len(bars)}}

    cfg = make_cfg(capital_per_trade=20000)
    broker = PaperBroker(cfg, client=HistFake())
    s = OptionSettings(indices=tuple(parse_indices("NIFTY", env={})))
    out = tmp_path / "bt.csv"
    res = run_backtest(cfg, broker, 5, ["ema", "supertrend", "vwap"], option_settings=s, out_csv=str(out),
                       today=date(2026, 11, 18))
    assert [r.name for r in res] == ["ema", "supertrend", "vwap"]
    assert any(r.trades for r in res)
    t = next(t for r in res for t in r.trades)
    assert t.qty == pytest.approx(0.4 * 65)                 # delta x lot
    assert out.exists() and "signal,symbol" in out.read_text().splitlines()[0]


def test_cli_rejects_signal_all_without_backtest(monkeypatch, tmp_path):
    from dhan_autotrader.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "x")
    assert main(["--signal", "all"]) == 2
