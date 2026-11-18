"""Entry point:  python -m dhan_autotrader [--strategy equity|options] [--daemon] [--check] [--confirm-live]"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time as _time
from datetime import datetime, time, timedelta
from logging.handlers import TimedRotatingFileHandler

from .broker import IST, BrokerError, DhanBroker, PaperBroker
from .bot import TradingBot
from .config import Config
from .journal import StateStore, TradeJournal
from .live import LIVE
from .login import get_access_token
from .notify import build_commands, build_notifier
from .options import OptionSettings, OptionsRunner
from .strategy import SIGNALS, build_strategy

log = logging.getLogger("dhan_autotrader")
DAEMON_WAKE = time(9, 10)  # log in a few minutes before the 09:15 open


def connect(cfg: Config):
    """Log in (PIN + TOTP if configured) and return a connected broker, or raise."""
    if cfg.pin and cfg.totp_secret:
        try:
            cfg.access_token = get_access_token(cfg.client_id, cfg.pin, cfg.totp_secret)
        except Exception as exc:
            if not cfg.access_token:
                raise BrokerError(f"Automatic Dhan login (PIN + TOTP) failed: {exc}") from exc
            log.warning("Automatic login failed (%s) — falling back to DHAN_ACCESS_TOKEN", exc)
    broker = DhanBroker(cfg) if cfg.is_live else PaperBroker(cfg)
    broker.check_connection()
    return broker


def trade_one_day(cfg: Config, opt: OptionSettings | None, broker=None) -> bool:
    """Run one session; returns True if it was interrupted (Ctrl+C)."""
    broker = broker or connect(cfg)
    strategy = build_strategy(cfg)
    notifier = build_notifier(cfg)
    telegram = build_commands(cfg, hub=LIVE.hub)  # Telegram + dashboard commands share one queue
    kind = "options" if opt else "equity"
    journal = TradeJournal("logs/trades.csv", mode=cfg.mode, strategy=f"{kind}:{strategy.name}")
    store = StateStore(f"state/{kind}_{cfg.mode}.json")
    if opt:
        bot = OptionsRunner(cfg, opt, broker, strategy, notifier=notifier, journal=journal, store=store)
    else:
        bot = TradingBot(cfg, broker, strategy, notifier=notifier, journal=journal, store=store)
    try:
        return bot.run(commands=LIVE.hub)
    finally:
        if telegram is not None:
            telegram.close()


def next_wake(now: datetime) -> datetime:
    """Next weekday at DAEMON_WAKE (today if still before square-off is handled by the caller)."""
    d = now.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, DAEMON_WAKE, IST)


def run_daemon(cfg: Config, opt: OptionSettings | None, clock=None, sleep=_time.sleep, max_days=None) -> None:
    """Trade every weekday forever: wake at 09:10, fresh login, trade until square-off, sleep, repeat."""
    clock = clock or (lambda: datetime.now(IST))
    days = 0
    while max_days is None or days < max_days:
        now = clock()
        if now.weekday() < 5 and now.time() < cfg.square_off:
            if now.time() < DAEMON_WAKE:
                wake = datetime.combine(now.date(), DAEMON_WAKE, IST)
                log.info("Daemon: sleeping until %s", wake.strftime("%a %d %b %H:%M"))
                sleep((wake - now).total_seconds())
                continue
            try:
                if trade_one_day(cfg, opt):
                    log.info("Daemon: interrupted — exiting")
                    return
                days += 1
            except BrokerError as exc:
                log.error("Daemon: could not start today's session: %s — retrying in 5 minutes", exc)
                build_notifier(cfg).send(f"⚠️ Could not start session: {exc} — retrying in 5 min")
                sleep(300)
                continue
            except Exception:
                log.exception("Daemon: session crashed — restarting in 60 s (open positions will be restored)")
                sleep(60)
                continue
        now = clock()
        wake = next_wake(now)
        log.info("Daemon: market closed — next session %s", wake.strftime("%a %d %b %H:%M"))
        sleep(max(60.0, (wake - now).total_seconds()))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dhan_autotrader", description="Dhan auto-trading bot")
    p.add_argument("--strategy", choices=["equity", "options", "nifty-options"],
                   default=os.environ.get("STRATEGY", "equity"),
                   help="equity = trade stocks; options = buy OTM CE/PE on NIFTY/BANKNIFTY/... (nifty-options = alias)")
    p.add_argument("--signal", help=f"entry signal(s): {', '.join(SIGNALS)}; comma-separate to require "
                                    "confirmation, e.g. supertrend,rsi (overrides SIGNAL in .env)")
    p.add_argument("--backtest", type=int, metavar="DAYS",
                   help="backtest on the last DAYS of Dhan history instead of trading (use --signal all to compare)")
    p.add_argument("--bt-delta", type=float, default=0.4, help="options backtest: assumed option delta (default 0.4)")
    p.add_argument("--daemon", action="store_true",
                   help="run forever: trade every weekday 09:10-square-off with a fresh login each day")
    p.add_argument("--dashboard", action="store_true",
                   help="also serve the web dashboard (http://127.0.0.1:8050 by default; see DASHBOARD_* in .env)")
    p.add_argument("--dashboard-only", action="store_true",
                   help="serve the dashboard without trading (journal, backtests, logs, settings)")
    p.add_argument("--check", action="store_true", help="only verify the Dhan connection and exit")
    p.add_argument("--confirm-live", action="store_true", help="required to place REAL orders when TRADING_MODE=live")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    def _sigterm(signum, frame):  # docker stop / systemctl stop -> same as Ctrl+C (square off first)
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except ValueError:  # not in main thread (e.g. tests)
        pass

    os.makedirs("logs", exist_ok=True)
    file_handler = TimedRotatingFileHandler("logs/bot.log", when="midnight", backupCount=30, encoding="utf-8")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )

    try:
        compare = args.signal and args.signal.strip().lower() == "all"
        if args.signal and not compare:
            os.environ["SIGNAL"] = args.signal
        cfg = Config.from_env()
        opt = OptionSettings.from_env() if args.strategy in ("options", "nifty-options") else None
    except ValueError as exc:
        log.error("Config error: %s", exc)
        return 2

    if compare and not args.backtest:
        log.error("--signal all is only valid with --backtest")
        return 2

    if cfg.is_live and not args.confirm_live and not (args.backtest or args.check or args.dashboard_only):
        log.error("TRADING_MODE=live requires --confirm-live. Real money will be used.")
        return 2

    dashboard = None
    if args.dashboard or args.dashboard_only:
        from .dashboard import start_dashboard

        def broker_factory():
            return connect(cfg)

        try:
            dashboard = start_dashboard(cfg, opt, broker_factory)
        except (OSError, ValueError) as exc:
            log.error("Dashboard could not start: %s", exc)
            if args.dashboard_only:
                return 2
            log.warning("Continuing to trade without the dashboard")

    if args.dashboard_only:
        log.info("Dashboard-only mode — Ctrl+C to quit")
        try:
            while True:
                _time.sleep(3600)
        except KeyboardInterrupt:
            dashboard.stop()
        return 0

    if args.daemon:
        if args.backtest or args.check:
            log.error("--daemon cannot be combined with --backtest/--check")
            return 2
        log.info("Daemon mode (%s) — Ctrl+C to quit", cfg.mode.upper())
        try:
            run_daemon(cfg, opt)
        except KeyboardInterrupt:
            log.info("Daemon stopped")
        return 0

    try:
        broker = connect(cfg)
    except BrokerError as exc:
        log.error("Could not connect to Dhan: %s", exc)
        return 1
    if args.check:
        return 0

    if args.backtest:
        from .backtest import format_table, run_backtest

        names = list(SIGNALS) if compare else [cfg.signal]
        results = run_backtest(cfg, broker, args.backtest, names, option_settings=opt, delta=args.bt_delta)
        what = ("options (ESTIMATED: index points x delta x lot size)" if opt
                else f"equity (₹{cfg.capital_per_trade:,.0f} per trade)")
        print(f"\nBacktest — last {args.backtest} days, {cfg.candle_interval}-min candles, {what}\n")
        print(format_table(results))
        print(f"\nNet P&L is after estimated brokerage, taxes and {cfg.slippage_pct}% slippage per fill."
              "\nAll trades: logs/backtest_trades.csv")
        return 0

    trade_one_day(cfg, opt, broker)
    if dashboard is not None:
        log.info("Session over — dashboard stays up for review. Ctrl+C to quit.")
        try:
            while True:
                _time.sleep(3600)
        except KeyboardInterrupt:
            dashboard.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
