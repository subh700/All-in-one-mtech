import csv
from datetime import datetime, timedelta

import pytest

import dhan_autotrader.__main__ as main_mod
from dhan_autotrader.backtest import BtParams, run_backtest
from dhan_autotrader.bot import TradingBot, handle_command, run_session
from dhan_autotrader.broker import IST, PaperBroker
from dhan_autotrader.costs import round_trip_charges, trade_charges
from dhan_autotrader.journal import JOURNAL_FIELDS, TradeJournal
from dhan_autotrader.notify import Notifier, TelegramCommands
from dhan_autotrader.options import OptionSettings, OptionsRunner, parse_indices
from dhan_autotrader.risk import Position, RiskManager
from dhan_autotrader.strategy import EmaCrossover

from test_bot import Clock, FakeDhan, make_cfg
from test_options import BULL, FakeOptDhan

NOW = datetime(2026, 11, 18, 11, 0, tzinfo=IST)
BUY_SIGNAL = [100 - i * 0.5 for i in range(20)] + [95]


class Recorder(Notifier):
    def __init__(self):
        self.msgs = []

    def send(self, text, key=None):
        self.msgs.append(text)


# ================================================================ risk guards
def test_daily_profit_target_halts_once_with_alert():
    rm = RiskManager(make_cfg(daily_profit_target=1000))
    seen = []
    rm.on_halt = seen.append
    rm.record_exit(600, NOW)
    assert not rm.halted
    rm.record_exit(500, NOW)
    assert rm.halted and "profit target" in rm.halt_reason
    rm.record_exit(-50, NOW)          # already halted -> no second alert
    assert len(seen) == 1 and not rm.can_open(0)[0]


def test_consecutive_losses_and_cooldown():
    rm = RiskManager(make_cfg(max_consecutive_losses=3, cooldown_after_loss_min=15, max_daily_loss=99999))
    rm.record_exit(-10, NOW)
    ok, why = rm.can_open(0, now=NOW + timedelta(minutes=5))
    assert not ok and "cooling down" in why
    assert rm.can_open(0, now=NOW + timedelta(minutes=16))[0]
    rm.record_exit(50, NOW)           # a win resets the streak
    rm.record_exit(-1, NOW)
    rm.record_exit(-1, NOW)
    assert not rm.halted
    rm.record_exit(-1, NOW)
    assert rm.halted and rm.halt_reason == "3 losses in a row"


def test_pause_blocks_entries_and_new_day_resets():
    rm = RiskManager(make_cfg())
    rm.roll_day(NOW.date())
    rm.paused = True
    assert "paused" in rm.can_open(0)[1]
    rm.halt("x")
    rm.roll_day(NOW.date() + timedelta(days=1))
    assert not rm.halted and rm.consecutive_losses == 0


def test_max_risk_per_trade_caps_quantity():
    rm = RiskManager(make_cfg(max_risk_per_trade=500))
    assert rm.cap_for_risk(1000, 2.0) == 250
    assert rm.cap_for_risk(5 * 65, 30.0, lot=65) == 0       # 1 lot risks 1950 > 500
    assert rm.cap_for_risk(5 * 65, 3.0, lot=65) == 130      # 2 lots = 390 risk
    assert RiskManager(make_cfg()).cap_for_risk(7, 100) == 7


def test_options_lots_reduced_by_risk_cap():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    cfg = make_cfg(capital_per_trade=50000, max_risk_per_trade=4000)
    s = OptionSettings(indices=tuple(parse_indices("NIFTY", env={})), max_lots=5, strike_offset=1)
    r = OptionsRunner(cfg, s, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock)
    r.tick()
    # premium 120, SL 25% -> 30/unit -> 1950/lot -> 2 lots under ₹4000
    assert r.bots[0].position.pos.qty == 130


# ================================================================ costs
def test_charges_match_typical_broker_calculators():
    assert trade_charges("NSE_FNO", "BUY", 120, 150, 65) == pytest.approx(64.46, abs=0.05)
    assert trade_charges("NSE_EQ", "BUY", 1000, 1010, 100) == pytest.approx(82.73, abs=0.05)
    # short equity: sell first, buy back — STT still on the sell value
    assert trade_charges("NSE_EQ", "SELL", 1010, 1000, 100) == pytest.approx(82.73, abs=0.05)
    assert round_trip_charges("BSE_FNO", 1000, 1000) < round_trip_charges("NSE_FNO", 1000, 1000)


def test_journal_records_charges_and_rotates_old_format(tmp_path):
    path = tmp_path / "trades.csv"
    path.write_text("date,mode,symbol,pnl\n2026-01-01,paper,X,1\n")
    j = TradeJournal(str(path))
    p = Position("X", "BUY", 10, 100, 99, 102, NOW)
    j.record(p, 102, 20.0, "target", NOW, charges=4.5)
    rows = list(csv.DictReader(open(path)))
    assert list(rows[0].keys()) == JOURNAL_FIELDS
    assert float(rows[0]["net_pnl"]) == 15.5
    assert len(list(tmp_path.glob("trades_old_*.csv"))) == 1


def test_close_alert_shows_net_pnl():
    fake, clock, rec = FakeDhan(), Clock(), Recorder()
    fake.closes, fake.ltp = BUY_SIGNAL, 100.0
    cfg = make_cfg()
    bot = TradingBot(cfg, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock, notifier=rec)
    bot.tick()
    fake.ltp = 103.0
    bot.tick()
    close = next(m for m in rec.msgs if "CLOSE" in m)
    assert "net ≈" in close


def test_backtest_net_is_after_costs(tmp_path):
    from test_extras import day_bars

    class Hist(FakeDhan):
        def intraday_minute_data(self, **kw):
            d = datetime(2026, 11, 17).date()
            closes = [100 - i * 0.3 for i in range(30)] + [91 + i * 0.4 for i in range(45)]
            bars = day_bars(d, closes)
            return {"status": "success", "remarks": "", "data": {
                "timestamp": [int(b.ts.timestamp()) for b in bars], "open": [b.open for b in bars],
                "high": [b.high for b in bars], "low": [b.low for b in bars], "close": [b.close for b in bars],
                "volume": [1000] * len(bars)}}

    cfg = make_cfg(slippage_pct=0.05, stop_loss_pct=5, target_pct=50)
    res = run_backtest(cfg, PaperBroker(cfg, client=Hist()), 3, ["ema"], out_csv=str(tmp_path / "b.csv"),
                       today=datetime(2026, 11, 18).date())[0]
    assert res.trades and res.costs > 0
    assert res.total == pytest.approx(res.gross - res.costs)
    assert "net_pnl" in (tmp_path / "b.csv").read_text().splitlines()[0]


# ================================================================ Telegram commands
def make_equity_bot(fake=None, clock=None, rec=None):
    fake = fake or FakeDhan()
    fake.closes, fake.ltp = BUY_SIGNAL, 100.0
    cfg = make_cfg()
    return TradingBot(cfg, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock or Clock(),
                      notifier=rec or Recorder()), fake


def test_handle_commands():
    bot, fake = make_equity_bot()
    bot.tick()
    assert "No open positions" not in handle_command(bot, "/status")
    assert "BUY TEST x100 @ 100.00" in handle_command(bot, "/status")
    fake.ltp = 101.0
    assert "open ₹100.00" in handle_command(bot, "/pnl")

    assert "Paused" in handle_command(bot, "/pause") and bot.risk.paused
    assert "PAUSED" in handle_command(bot, "/status")
    assert "Resumed" in handle_command(bot, "/resume") and not bot.risk.paused

    handle_command(bot, "/squareoff")
    assert not bot.positions and not bot.risk.halted
    assert "Stopped" in handle_command(bot, "/stop")
    assert bot.risk.halted and "Telegram" in bot.risk.halt_reason
    assert "/squareoff" in handle_command(bot, "/whatever")


def test_telegram_commands_skip_backlog_and_foreign_chats():
    batches = [
        {"result": [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/stop"}}]},     # backlog -> ignored
        {"result": [{"update_id": 2, "message": {"chat": {"id": 666}, "text": "/stop"}},     # stranger
                    {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/status@MyBot extra"}},
                    {"update_id": 4, "message": {"chat": {"id": 42}, "text": "hello"}}]},
    ]
    offsets = []

    def get(params):
        offsets.append(params.get("offset"))
        if batches:
            return batches.pop(0)
        tc._stop.wait(0.05)
        return {"result": []}

    tc = TelegramCommands("T", "42", get=get, poll_timeout=0)
    assert tc.get(timeout=2) == "/status"
    assert tc.get(timeout=0.2) is None
    tc.close()
    assert offsets[1] == 2 and 5 in offsets


def test_run_session_handles_commands_between_ticks():
    clock, rec = Clock(13, 0), Recorder()
    bot, fake = make_equity_bot(clock=clock, rec=rec)
    bot.cfg.poll_seconds = 1

    class Cmds:
        def __init__(self):
            self.queue = ["/status", "/stop"]
            self.closed = False

        def get(self, timeout):
            if self.queue:
                return self.queue.pop(0)
            clock.now = clock.now.replace(hour=15, minute=20)   # end the day
            return None

        def close(self):
            self.closed = True

    cmds = Cmds()
    assert run_session(bot, "test", commands=cmds) is False
    assert any("BUY TEST" in m for m in rec.msgs)                 # /status reply
    assert any("Stopped for today" in m for m in rec.msgs)
    assert bot.risk.halted and not bot.positions and cmds.closed
    assert "halted: stopped via Telegram" in rec.msgs[-1]           # daily summary


# ================================================================ daemon
def test_daemon_schedule(monkeypatch):
    t = {"now": datetime(2026, 11, 20, 8, 0, tzinfo=IST)}   # Friday 08:00
    sleeps, sessions = [], []

    def sleep(sec):
        sleeps.append(sec)
        t["now"] += timedelta(seconds=sec)

    def fake_day(cfg, opt, broker=None):
        sessions.append(t["now"])
        t["now"] = t["now"].replace(hour=15, minute=16)
        return False

    monkeypatch.setattr(main_mod, "trade_one_day", fake_day)
    main_mod.run_daemon(make_cfg(), None, clock=lambda: t["now"], sleep=sleep, max_days=2)
    assert sessions[0] == datetime(2026, 11, 20, 9, 10, tzinfo=IST)   # waited until 09:10
    assert sessions[1] == datetime(2026, 11, 23, 9, 10, tzinfo=IST)   # skipped the weekend -> Monday
    assert sleeps[0] == 70 * 60


def test_daemon_retries_failed_login_and_stops_on_ctrl_c(monkeypatch):
    from dhan_autotrader.broker import BrokerError

    t = {"now": datetime(2026, 11, 18, 10, 0, tzinfo=IST)}
    calls = []

    def fake_day(cfg, opt, broker=None):
        calls.append(t["now"])
        if len(calls) == 1:
            raise BrokerError("token rejected")
        return True                       # interrupted -> daemon exits

    monkeypatch.setattr(main_mod, "trade_one_day", fake_day)
    sleeps = []
    main_mod.run_daemon(make_cfg(), None, clock=lambda: t["now"],
                        sleep=lambda s: (sleeps.append(s), t.update(now=t["now"] + timedelta(seconds=s))))
    assert len(calls) == 2 and sleeps == [300]
