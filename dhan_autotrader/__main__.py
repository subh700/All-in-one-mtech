"""Entry point:  python -m dhan_autotrader [--check] [--confirm-live]"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .broker import BrokerError, DhanBroker, PaperBroker
from .bot import TradingBot
from .config import Config
from .strategy import EmaCrossover


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dhan_autotrader", description="Dhan auto-trading bot")
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
        cfg = Config.from_env()
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

    TradingBot(cfg, broker, EmaCrossover(cfg.fast_ema, cfg.slow_ema)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
