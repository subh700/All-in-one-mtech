import base64
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from dhan_autotrader.bot import TradingBot, run_session
from dhan_autotrader.broker import PaperBroker
from dhan_autotrader.dashboard import DashboardContext, DashboardServer
from dhan_autotrader.dashboard.server import read_trades, tail
from dhan_autotrader.journal import TradeJournal
from dhan_autotrader.live import LIVE, CommandHub
from dhan_autotrader.options import OptionSettings, OptionsRunner, parse_indices
from dhan_autotrader.risk import Position
from dhan_autotrader.strategy import EmaCrossover

from test_bot import Clock, FakeDhan, make_cfg
from test_options import BULL, FakeOptDhan

BUY_SIGNAL = [100 - i * 0.5 for i in range(20)] + [95]


def call(srv, path, method="GET", body=None, headers=None, password=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if password:
        h["Authorization"] = "Basic " + base64.b64encode(f"me:{password}".encode()).decode()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


@pytest.fixture
def server(tmp_path):
    cfg = make_cfg(poll_seconds=1, access_token="SECRET-TOKEN", pin="9999", totp_secret="ABC")
    j = TradeJournal(str(tmp_path / "trades.csv"), mode="paper", strategy="equity:ema")
    from dhan_autotrader.broker import IST
    from datetime import datetime
    t = datetime(2026, 11, 17, 10, 0, tzinfo=IST)
    j.record(Position("TEST", "BUY", 10, 100, 99, 102, t), 102, 20.0, "target", t + timedelta(minutes=30), charges=5)
    (tmp_path / "bot.log").write_text("".join(f"2026-11-18 10:00:{i:02d} INFO x: line {i}\n" for i in range(50)))
    ctx = DashboardContext(cfg, None, lambda: PaperBroker(cfg, client=FakeDhan()),
                           trades_path=str(tmp_path / "trades.csv"), log_path=str(tmp_path / "bot.log"))
    srv = DashboardServer(ctx, "127.0.0.1", 0).start()
    yield srv, ctx, cfg
    srv.stop()
    LIVE.detach()


def test_index_and_read_endpoints(server):
    srv, ctx, cfg = server
    code, html = call(srv, "/")
    assert code == 200 and b"Dhan Auto Trader" in html
    assert call(srv, "/api/health")[1]["ok"] is True
    code, data = call(srv, "/api/trades")
    assert data["trades"][0]["symbol"] == "TEST" and data["trades"][0]["net_pnl"] == 15.0
    code, data = call(srv, "/api/logs?lines=5")
    assert data["lines"] == [f"2026-11-18 10:00:{i:02d} INFO x: line {i}" for i in range(45, 50)]
    assert call(srv, "/api/nope")[0] == 404


def test_config_masks_secrets(server):
    srv, *_ = server
    code, data = call(srv, "/api/config")
    raw = json.dumps(data)
    assert "SECRET-TOKEN" not in raw and "9999" not in raw
    assert data["config"]["access_token"] == "••••••" and data["config"]["entry_start"] == "09:30"


def test_post_requires_csrf_header_and_running_bot(server):
    srv, *_ = server
    assert call(srv, "/api/command", "POST", {"cmd": "/stop"})[0] == 403
    code, data = call(srv, "/api/command", "POST", {"cmd": "/stop"}, {"X-Requested-With": "dashboard"})
    assert code == 409 and "not running" in data["error"]
    code, _ = call(srv, "/api/command", "POST", {"cmd": "/rm -rf"}, {"X-Requested-With": "dashboard"})
    assert code == 400


def test_password_protection(tmp_path):
    cfg = make_cfg()
    ctx = DashboardContext(cfg, None, lambda: None, password="hunter2", trades_path=str(tmp_path / "t.csv"),
                           log_path=str(tmp_path / "l.log"))
    srv = DashboardServer(ctx, "127.0.0.1", 0).start()
    try:
        assert call(srv, "/api/health")[0] == 401
        assert call(srv, "/api/health", password="wrong")[0] == 401
        assert call(srv, "/api/health", password="hunter2")[0] == 200
    finally:
        srv.stop()


def test_refuses_public_bind_without_password():
    with pytest.raises(ValueError):
        DashboardServer(DashboardContext(make_cfg(), None, lambda: None), "0.0.0.0", 0)


def test_live_snapshot_and_command_roundtrip(server):
    srv, ctx, _ = server
    fake, clock = FakeDhan(), Clock(13, 0)
    fake.closes, fake.ltp = BUY_SIGNAL, 100.0
    cfg = make_cfg(poll_seconds=1)
    bot = TradingBot(cfg, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock)
    hub = LIVE.hub
    t = threading.Thread(target=run_session, args=(bot, "test"), kwargs={"commands": hub}, daemon=True)
    t.start()
    for _ in range(50):
        snap = call(srv, "/api/live")[1]
        if snap.get("running") and snap.get("positions"):
            break
        time.sleep(0.05)
    assert snap["state"] == "running" and snap["positions"][0]["symbol"] == "TEST"
    assert snap["market"][0]["signal"] == "BUY" and set(snap["market"][0]["biases"]) == {"ema", "supertrend", "rsi", "vwap", "orb"}
    assert snap["pnl_curve"]

    code, data = call(srv, "/api/command", "POST", {"cmd": "/pause"}, {"X-Requested-With": "dashboard"})
    assert code == 200 and "Paused" in data["reply"]
    assert call(srv, "/api/live")[1]["state"] == "paused"
    code, data = call(srv, "/api/command", "POST", {"cmd": "/stop"}, {"X-Requested-With": "dashboard"})
    assert "Stopped" in data["reply"]
    snap = call(srv, "/api/live")[1]
    assert snap["state"] == "halted" and snap["positions"] == []
    clock.now = clock.now.replace(hour=15, minute=20)   # end the session
    t.join(5)
    assert not t.is_alive()
    assert call(srv, "/api/live")[1]["running"] is False


def test_options_snapshot_has_indices():
    fake, clock = FakeOptDhan(), Clock()
    fake.closes = BULL
    cfg = make_cfg(capital_per_trade=20000)
    s = OptionSettings(indices=tuple(parse_indices("NIFTY", env={})), strike_offset=1)
    r = OptionsRunner(cfg, s, PaperBroker(cfg, client=fake), EmaCrossover(3, 6), clock=clock)
    r.tick()
    r.record_pnl_point()
    snap = r.snapshot()
    assert snap["kind"] == "options" and snap["indices"] == ["NIFTY"]
    assert snap["positions"][0]["symbol"].startswith("NIFTY") and snap["market"][0]["symbol"] == "NIFTY"
    assert snap["options"]["strike_offset"] == 1 and json.dumps(snap)


def test_backtest_job(server, monkeypatch):
    srv, ctx, _ = server
    import dhan_autotrader.backtest as bt
    from dhan_autotrader.backtest import BtResult, BtTrade
    from dhan_autotrader.broker import IST
    from datetime import datetime

    t0 = datetime(2026, 11, 17, 10, 0, tzinfo=IST)
    monkeypatch.setattr(bt, "run_backtest", lambda cfg, broker, days, sigs, option_settings=None: [
        BtResult(s, [BtTrade("X", "BUY", t0, 100, t0, 101, "target", 1, 100.0, 5.0)]) for s in sigs])
    hdr = {"X-Requested-With": "dashboard"}
    assert call(srv, "/api/backtest", "POST", {"days": 30, "signals": []}, hdr)[0] == 400
    assert call(srv, "/api/backtest", "POST", {"days": 30, "signals": ["ema", "supertrend,rsi"]}, hdr)[1]["ok"]
    for _ in range(100):
        st = call(srv, "/api/backtest")[1]
        if st["status"] != "running":
            break
        time.sleep(0.02)
    assert st["status"] == "done" and [r["signal"] for r in st["results"]] == ["ema", "supertrend,rsi"]
    assert st["results"][0]["net"] == 95.0 and st["results"][0]["curve"] == [["2026-11-17T10:00+05:30", 95.0]]
    call(srv, "/api/backtest", "POST", {"days": 30, "signals": ["bogus"]}, hdr)
    for _ in range(100):
        st = call(srv, "/api/backtest")[1]
        if st["status"] != "running":
            break
        time.sleep(0.02)
    assert st["status"] == "error"


def test_hub_and_helpers(tmp_path):
    hub = CommandHub()
    c = hub.submit("/status", "dashboard")
    assert hub.get(0.1) is c and hub.get(0.01) is None
    c.resolve("ok")
    assert c.wait(0.1) == "ok"
    pending = hub.submit("/pause", "dashboard")
    hub.drain("gone")
    assert pending.wait(0.1) == "gone"
    assert read_trades(str(tmp_path / "missing.csv")) == [] and tail(str(tmp_path / "missing.log")) == []
