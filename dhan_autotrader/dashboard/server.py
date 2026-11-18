"""Web dashboard: live monitor, controls, trade journal, backtests, logs and settings.

Zero extra dependencies (stdlib http.server). Start with:
    python -m dhan_autotrader --strategy options --dashboard            # trade + dashboard
    python -m dhan_autotrader --strategy options --daemon --dashboard   # 24/7
    python -m dhan_autotrader --dashboard-only                          # journal/backtest/logs only
then open http://127.0.0.1:8050

Security:
  * binds to 127.0.0.1 by default. To expose it (e.g. on a VPS) set DASHBOARD_HOST=0.0.0.0
    AND DASHBOARD_PASSWORD — the server refuses to listen publicly without a password.
  * all write actions are POST + a custom header (blocks cross-site form/CSRF submissions).
"""
from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import logging
import os
import threading
import traceback
from dataclasses import fields, is_dataclass
from datetime import datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..live import LIVE

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
SECRET_FIELDS = {"access_token", "pin", "totp_secret", "telegram_token"}
COMMANDS = {"/pause", "/resume", "/squareoff", "/stop", "/status", "/pnl"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def mask(name: str, value):
    if name in SECRET_FIELDS:
        return "••••••" if value else ""
    if name == "client_id" and value:
        return str(value)[:2] + "•" * max(0, len(str(value)) - 4) + str(value)[-2:]
    return value


def to_jsonable(v):
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, datetime):
        return v.isoformat()
    if is_dataclass(v):
        return {f.name: to_jsonable(getattr(v, f.name)) for f in fields(v)}
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x) for x in v]
    if v in (float("inf"), float("-inf")):
        return None
    return v


def read_trades(path: str) -> list[dict]:
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    num = {"qty", "entry", "exit", "pnl", "charges", "net_pnl"}
    for r in rows:
        for k in num:
            if k in r and r[k] not in (None, ""):
                try:
                    r[k] = float(r[k])
                except ValueError:
                    pass
        if "net_pnl" not in r or r["net_pnl"] in ("", None):
            r["net_pnl"] = r.get("pnl", 0.0)
            r.setdefault("charges", 0.0)
    return rows


def tail(path: str, lines: int = 400, max_bytes: int = 400_000) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = data.splitlines()
    if size > max_bytes:
        out = out[1:]  # first line may be partial
    return out[-lines:]


class BacktestJob:
    """Runs one backtest at a time in a background thread."""

    def __init__(self, ctx: "DashboardContext"):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.state = {"status": "idle"}

    def start(self, days: int, signals: list[str]) -> dict:
        with self.lock:
            if self.state.get("status") == "running":
                return {"ok": False, "error": "A backtest is already running"}
            self.state = {"status": "running", "days": days, "signals": signals,
                          "started": datetime.now().isoformat(timespec="seconds")}
        threading.Thread(target=self._run, args=(days, signals), daemon=True, name="backtest").start()
        return {"ok": True}

    def _run(self, days: int, signals: list[str]) -> None:
        from dataclasses import replace

        from ..backtest import run_backtest
        from ..strategy import parse_signal

        try:
            cfg = self.ctx.cfg
            for s in signals:
                parse_signal(s)  # validate early
            broker = self.ctx.broker_factory()
            results = run_backtest(replace(cfg), broker, days, signals, option_settings=self.ctx.opt)
            out = []
            for r in results:
                eq, curve = 0.0, []
                for t in r.trades:
                    eq += t.net
                    curve.append([t.exit_ts.isoformat(timespec="minutes"), round(eq, 2)])
                pf = r.profit_factor
                out.append({
                    "signal": r.name, "trades": len(r.trades), "win_rate": round(r.win_rate, 1),
                    "gross": round(r.gross, 2), "costs": round(r.costs, 2), "net": round(r.total, 2),
                    "profit_factor": None if pf in (float("inf"),) else round(pf, 2),
                    "max_dd": round(r.max_drawdown, 2), "curve": curve,
                })
            with self.lock:
                self.state = {**self.state, "status": "done", "results": out,
                              "finished": datetime.now().isoformat(timespec="seconds"),
                              "estimate": self.ctx.opt is not None}
        except Exception as exc:
            log.error("Dashboard backtest failed: %s\n%s", exc, traceback.format_exc())
            with self.lock:
                self.state = {**self.state, "status": "error", "error": str(exc)}

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)


class DashboardContext:
    def __init__(self, cfg, opt, broker_factory, password: str = "", trades_path="logs/trades.csv",
                 log_path="logs/bot.log", command_timeout: float | None = None):
        self.cfg, self.opt, self.broker_factory = cfg, opt, broker_factory
        self.password = password
        self.trades_path, self.log_path = trades_path, log_path
        self.command_timeout = command_timeout or (cfg.poll_seconds + 10)
        self.backtest = BacktestJob(self)

    def config_view(self) -> dict:
        cfg = {f.name: mask(f.name, to_jsonable(getattr(self.cfg, f.name))) for f in fields(self.cfg)}
        opt = to_jsonable(self.opt) if self.opt else None
        return {"config": cfg, "options": opt, "strategy": "options" if self.opt else "equity"}


def make_handler(ctx: DashboardContext):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DhanAutoTrader"

        def log_message(self, fmt, *args):  # keep the trading log clean
            log.debug("dashboard %s - " + fmt, self.address_string(), *args)

        # ---- helpers -------------------------------------------------
        def _authorized(self) -> bool:
            if not ctx.password:
                return True
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                    if hmac.compare_digest(pw, ctx.password):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Dhan Auto Trader"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=to_jsonable).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        # ---- routes --------------------------------------------------
        def do_GET(self):
            if not self._authorized():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            path = url.path
            if path in ("/", "/index.html"):
                body = (STATIC / "index.html").read_bytes()
                return self._send(200, body, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if path == "/api/live":
                snap = LIVE.snapshot()
                return self._json(snap or {"running": False})
            if path == "/api/trades":
                return self._json({"trades": read_trades(ctx.trades_path)})
            if path == "/api/trades.csv":
                try:
                    body = Path(ctx.trades_path).read_bytes()
                except OSError:
                    body = b""
                return self._send(200, body, "text/csv",
                                  {"Content-Disposition": 'attachment; filename="trades.csv"'})
            if path == "/api/logs":
                n = min(int((q.get("lines") or ["400"])[0]), 3000)
                return self._json({"lines": tail(ctx.log_path, n)})
            if path == "/api/config":
                return self._json(ctx.config_view())
            if path == "/api/backtest":
                return self._json(ctx.backtest.status())
            if path == "/api/health":
                return self._json({"ok": True, "bot_running": LIVE.running})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._authorized():
                return
            if self.headers.get("X-Requested-With") != "dashboard":
                return self._json({"error": "missing X-Requested-With header"}, 403)
            path = urlparse(self.path).path
            body = self._body()
            if path == "/api/command":
                name = str(body.get("cmd", ""))
                if name not in COMMANDS:
                    return self._json({"ok": False, "error": f"unknown command {name}"}, 400)
                if not LIVE.running:
                    return self._json({"ok": False, "error": "The bot is not running right now"}, 409)
                reply = LIVE.hub.submit(name, "dashboard").wait(ctx.command_timeout)
                if reply is None:
                    return self._json({"ok": False, "error": "Timed out waiting for the bot"}, 504)
                return self._json({"ok": True, "reply": reply})
            if path == "/api/backtest":
                try:
                    days = max(1, min(int(body.get("days", 30)), 365))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "days must be a number"}, 400)
                signals = [str(s).strip().lower() for s in body.get("signals", []) if str(s).strip()][:8]
                if not signals:
                    return self._json({"ok": False, "error": "choose at least one signal"}, 400)
                return self._json(ctx.backtest.start(days, signals))
            self._json({"error": "not found"}, 404)

    return Handler


class DashboardServer:
    def __init__(self, ctx: DashboardContext, host: str = "127.0.0.1", port: int = 8050):
        if host not in LOCAL_HOSTS and not ctx.password:
            raise ValueError("Refusing to expose the dashboard on a public address without DASHBOARD_PASSWORD")
        self.httpd = ThreadingHTTPServer((host, port), make_handler(ctx))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="dashboard")

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> "DashboardServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def start_dashboard(cfg, opt, broker_factory) -> DashboardServer:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip()
    port = int(os.environ.get("DASHBOARD_PORT", "8050"))
    ctx = DashboardContext(cfg, opt, broker_factory, password=os.environ.get("DASHBOARD_PASSWORD", ""))
    srv = DashboardServer(ctx, host, port).start()
    log.info("Dashboard running at http://%s:%d%s", "127.0.0.1" if host in ("0.0.0.0", "::") else host,
             srv.port, " (password protected)" if ctx.password else "")
    return srv
