"""Entry point:  python -m dhan_autotrader [--strategy equity|options] [--check] [--confirm-live]"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .broker import BrokerError, DhanBroker, PaperBroker
from .bot import TradingBot
from .config import Config
from .journal import StateStore, TradeJournal
from .login import get_access_token
from .notify import build_notifier
from .options import OptionSettings, OptionsRunner
from .strategy import SIGNALS, build_strategy


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
    p.add_argument("--check", action="store_true", help="only verify the Dhan connection and exit")
    p.add_argument("--confirm-live", action="store_true", help="required to place REAL orders when TRADING_MODE=live")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log")],
    )
    log = logging.getLogger("dhan_autotrader")

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

    if cfg.is_live and not args.confirm_live and not args.backtest:
        log.error("TRADING_MODE=live requires --confirm-live. Real money will be used.")
        return 2

    if cfg.pin and cfg.totp_secret:
        try:
            cfg.access_token = get_access_token(cfg.client_id, cfg.pin, cfg.totp_secret)
        except Exception as exc:
            log.error("Automatic Dhan login (PIN + TOTP) failed: %s", exc)
            if not cfg.access_token:
                return 1
            log.warning("Falling back to DHAN_ACCESS_TOKEN from .env")

    broker = DhanBroker(cfg) if cfg.is_live else PaperBroker(cfg)
    try:
        broker.check_connection()
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
        print("\nAll trades: logs/backtest_trades.csv  (no brokerage/slippage included)")
        return 0

    strategy = build_strategy(cfg)
    notifier = build_notifier(cfg)
    kind = "options" if opt else "equity"
    journal = TradeJournal("logs/trades.csv", mode=cfg.mode, strategy=f"{kind}:{strategy.name}")
    store = StateStore(f"state/{kind}_{cfg.mode}.json")
    if opt:
        OptionsRunner(cfg, opt, broker, strategy, notifier=notifier, journal=journal, store=store).run()
    else:
        TradingBot(cfg, broker, strategy, notifier=notifier, journal=journal, store=store).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
