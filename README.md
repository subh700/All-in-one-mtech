# Dhan Auto Trader

An intraday auto-trading bot for **Dhan** (DhanHQ API v2), built on the official `dhanhq` Python SDK.

- **Paper mode by default.** It uses real Dhan market data and simulates the fills. Live orders need `TRADING_MODE=live` **and** the `--confirm-live` flag.
- **Strategy:** an EMA crossover (default 9/21) on closed candles (default 5-minute). The strategy is pluggable in `dhan_autotrader/strategy.py`.
- **Risk controls:** a fixed amount of capital per trade, a % stop-loss and % target, a cap on trades per day, a daily loss limit (trading halts and positions close when it's hit), a cap on open positions, an entry time window, and an automatic square-off at 15:15 IST.
- Orders are `INTRADAY` `MARKET` orders. The bot polls until each order fills and treats rejections as errors.

## 1. Get Dhan API access

1. Log in at **web.dhan.co**, then go to **My Profile → Access DhanHQ APIs**.
2. Generate an **Access Token** and copy it along with your **Client ID**.
3. **Static IP:** Dhan requires a whitelisted static IP before the API will accept *orders*. Register the public IP of the machine or VPS that runs the bot in the same section. Market data and paper mode work without this.

> Access tokens expire. Generate a new one in Dhan when the bot reports an auth error.

## 2. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then edit .env with your Client ID, token and instruments
```

To find security IDs, look up the `SEM_SMST_SECURITY_ID` column in Dhan's scrip master:
https://images.dhan.co/api-data/api-scrip-master.csv

## 3. Run

```bash
python -m dhan_autotrader --check     # verify connection + show balance, then exit
python -m dhan_autotrader             # paper trading (default)
python -m dhan_autotrader -v          # verbose: logs each candle's signal
```

Run it for a few days in paper mode and read `logs/bot.log`. Once you're happy with the results:

```bash
# in .env: TRADING_MODE=live
python -m dhan_autotrader --confirm-live
```

The bot stops on its own after square-off time and on weekends. Pressing `Ctrl+C` squares off any open positions first.
To run it every trading day, schedule it on your VPS, for example with cron at 09:20 IST from Monday to Friday:

```
20 9 * * 1-5 cd /path/to/All-in-one-mtech && .venv/bin/python -m dhan_autotrader --confirm-live >> logs/cron.log 2>&1
```

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `INSTRUMENTS` | — | `SYMBOL:SECURITY_ID:SEGMENT`, separated by commas (`NSE_EQ`, `BSE_EQ`, …) |
| `CANDLE_INTERVAL_MIN` | 5 | 1, 5, 15, 25 or 60 |
| `FAST_EMA` / `SLOW_EMA` | 9 / 21 | EMA periods |
| `ALLOW_SHORT` | false | allow intraday short entries on a bearish cross |
| `CAPITAL_PER_TRADE` | 10000 | ₹ per position (qty = capital ÷ LTP) |
| `STOP_LOSS_PCT` / `TARGET_PCT` | 1.0 / 2.0 | exit levels, as % of the entry price |
| `MAX_TRADES_PER_DAY` | 6 | entries per day |
| `MAX_DAILY_LOSS` | 1000 | ₹; trading halts and positions close when it's hit |
| `MAX_OPEN_POSITIONS` | 2 | open positions at once |
| `ENTRY_START` / `ENTRY_END` / `SQUARE_OFF` | 09:30 / 14:45 / 15:15 | IST |

## Layout

```
dhan_autotrader/
  config.py    env config + validation
  broker.py    DhanBroker (live orders) and PaperBroker (simulated fills)
  strategy.py  EMA crossover (add your own Strategy subclass)
  risk.py      positions, sizing, SL/target, daily limits
  bot.py       trading loop
tests/         unit tests using a fake Dhan client (pytest)
```

## Disclaimer

Trading involves substantial risk of loss. This software comes with no warranty, and past or paper results don't guarantee future returns. You are responsible for every order it places.
