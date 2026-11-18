"""Entry point:  python -m dhan_autotrader [--strategy equity|options] [--check] [--confirm-live]"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .broker import BrokerError, DhanBroker, PaperBroker
from .bot import TradingBot
from .config import Config
from .notify import build_notifier
from .options import OptionSettings, OptionsRunner
from .strategy import SIGNALS, build_strategy


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dhan_autotrader", description="Dhan auto-trading bot")
    p.add_argument("--strategy", choices=["equity", "options", "nifty-options"],
                   default=os.environ.get("STRATEGY", "equity"),
                   help="equity = trade stocks; options = buy OTM CE/PE on NIFTY/BANKNIFTY/... (nifty-options = alias)")
    p.add_argument("--signal", choices=SIGNALS, help="entry signal (overrides SIGNAL in .env)")
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
        if args.signal:
            os.environ["SIGNAL"] = args.signal
        cfg = Config.from_env()
        opt = OptionSettings.from_env() if args.strategy in ("options", "nifty-options") else None
    except ValueError as exc:
        log.error("Config error: %s", exc)
        return 2

    if cfg.is_live and not args.confirm_live:
        log.error("TRADING_MODE=live requires --confirm-live. Real money will be used.")
        return 2

    broker = DhanBroker(cfg) if cfg.is_live else PaperBroker(cfg)
    try:
        broker.check_connection()
    except BrokerError as exc:
        log.error("Could not connect to Dhan: %s", exc)
        return 1
    if args.check:
        return 0

    strategy = build_strategy(cfg)
    notifier = build_notifier(cfg)
    if opt:
        OptionsRunner(cfg, opt, broker, strategy, notifier=notifier).run()
    else:
        TradingBot(cfg, broker, strategy, notifier=notifier).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
