# Dhan Auto Trader

An intraday auto-trading bot for **Dhan** (DhanHQ API v2), built on the official `dhanhq` Python SDK.

- **Two modes:** buy **OTM call and put options** on NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY and SENSEX, or trade **stocks**.
- **5 entry signals:** EMA crossover, Supertrend, RSI momentum, VWAP cross, and opening-range breakout.
- **Risk controls:**
  - % stop-loss, % target, and a **trailing stop-loss**
  - Daily loss limit across everything: when it's hit, the bot closes all positions and stops trading for the day
  - Caps on trades per day and on open positions
  - An entry time window and an automatic square-off at 15:15 IST
- **Telegram alerts** for every entry, exit, trailing-stop activation and error, plus a daily P&L summary.
- **Paper mode by default.** It uses real Dhan market data and simulates the fills. Live orders need `TRADING_MODE=live` **and** the `--confirm-live` flag.

## 1. Get Dhan API access

1. Log in at **web.dhan.co**, then go to **My Profile → Access DhanHQ APIs**.
2. Generate an **Access Token** and copy it along with your **Client ID**.
3. **Static IP:** Dhan requires a whitelisted static IP before the API will accept *orders*. Register the public IP of the machine or VPS that runs the bot. Market data and paper mode work without this.
4. **Options:** F&O trading must be activated on your Dhan account.

> Access tokens expire. Generate a new one in Dhan when the bot reports an auth error.

## 2. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your Client ID and token, and choose your settings
```

## 3. Run

```bash
python -m dhan_autotrader --check                                   # verify connection + balance
python -m dhan_autotrader --strategy options                        # OTM options, paper
python -m dhan_autotrader --strategy options --signal supertrend    # use a different signal
python -m dhan_autotrader --strategy equity                         # stocks, paper
python -m dhan_autotrader -v                                        # log every candle's signal
```

Run it for a few days in paper mode and read `logs/bot.log` (or your Telegram alerts). Once you're happy with the results:

```bash
# in .env: TRADING_MODE=live
python -m dhan_autotrader --strategy options --confirm-live
```

The bot stops on its own after square-off time and on weekends. Pressing `Ctrl+C` squares off any open positions first.
To run it every trading day on a VPS, add a cron job (09:20 IST, Monday to Friday):

```
20 9 * * 1-5 cd /path/to/All-in-one-mtech && .venv/bin/python -m dhan_autotrader --confirm-live >> logs/cron.log 2>&1
```

## Entry signals (`SIGNAL=`)

Each signal fires only on the candle where its condition **changes**, never repeatedly while a trend continues.
**Bullish** means buy a CALL (or go long on a stock). **Bearish** means buy a PUT (or exit or short a stock).

| `SIGNAL` | Bullish when… | Bearish when… | Settings |
|---|---|---|---|
| `ema` (default) | the fast EMA crosses above the slow EMA | it crosses below | `FAST_EMA=9`, `SLOW_EMA=21` |
| `supertrend` | Supertrend flips up | it flips down | `ST_PERIOD=10`, `ST_MULT=3` |
| `rsi` | RSI crosses above `RSI_BUY` | RSI crosses below `RSI_SELL` | `RSI_PERIOD=14`, `RSI_BUY=60`, `RSI_SELL=40` |
| `vwap` | a candle closes above today's VWAP | a candle closes below it | — |
| `orb` | a candle first closes above the opening-range high | a candle closes below its low | `ORB_MINUTES=15` (range starts at 09:15) |

> **VWAP on indices:** NIFTY, BANKNIFTY and the other indices report **zero volume**, so for them the bot uses an equal-weighted average of today's prices (TWAP). For stocks it uses true volume weighting.
> **ORB:** the bot only enters after `ENTRY_START`, so keep `ENTRY_START` at or after the end of the range (09:30 for 15 minutes).

All signals run on closed candles of `CANDLE_INTERVAL_MIN` (1, 5, 15, 25 or 60 minutes).

## Options trading: OTM CE / PE buying (`STRATEGY=options`)

It **buys options only**, so the most you can lose on a trade is the premium you paid.

1. **Indices:** set `OPT_INDICES=NIFTY,BANKNIFTY,SENSEX` (any mix). Each index gets its own signal and position. The trades-per-day cap, daily loss limit and `MAX_OPEN_POSITIONS` are **shared** across all of them.
2. **Direction:** a bullish signal on the index buys a **CALL (CE)**. A bearish signal buys a **PUT (PE)**. An opposite signal exits and switches to the other side.
3. **Strike:** **OTM by default** (`OPT_STRIKE_OFFSET=1`). CE = ATM + offset × step, which is above spot. PE = ATM − offset × step, which is below spot. If that strike is illiquid (premium < `OPT_MIN_PREMIUM`), the bot moves further OTM. It **never falls back to an ATM or ITM strike**.
   *Example, NIFTY at 25,112:* offset 1 buys the 25150 CE or the 25050 PE, and offset 2 buys the 25200 CE or the 25000 PE.
4. **Expiry:** the nearest expiry, read from Dhan's option chain (weekly for NIFTY and SENSEX, monthly for the others). **On expiry day it uses the next expiry** (`OPT_SKIP_EXPIRY_DAY`).
5. **Size:** lots = `CAPITAL_PER_TRADE ÷ (premium × lot size)`, capped at `OPT_MAX_LOTS`.
6. **Exits:**
   - The premium falls by `OPT_SL_PCT` (25%)
   - The premium rises by `OPT_TARGET_PCT` (50%)
   - The trailing stop is hit
   - An opposite signal appears
   - The daily loss limit is hit
   - It's 15:15

| Index | Dhan security id | Options segment | Strike step | Lot size (default) |
|---|---|---|---|---|
| NIFTY | 13 | NSE_FNO | 50 | 65 |
| BANKNIFTY | 25 | NSE_FNO | 100 | 30 |
| FINNIFTY | 27 | NSE_FNO | 50 | 60 |
| MIDCPNIFTY | 442 | NSE_FNO | 25 | 120 |
| SENSEX | 51 | BSE_FNO | 100 | 20 |

> ⚠️ **Check the lot sizes** against current NSE/BSE circulars before you trade live, because exchanges revise them from time to time. Override with `OPT_LOT_SIZE_<INDEX>` (for example `OPT_LOT_SIZE_BANKNIFTY=35`) and `OPT_STRIKE_STEP_<INDEX>`.

> OTM options are cheaper (you get more lots for the same capital) but lose value faster and need a bigger move to profit. 1–2 strikes OTM is a common balance.

| Variable | Default | Meaning |
|---|---|---|
| `OPT_INDICES` | NIFTY | comma-separated list from the table above |
| `OPT_MAX_LOTS` | 1 | most lots per trade |
| `OPT_STRIKE_OFFSET` | 1 | n strikes OTM; 0 = ATM; −n = ITM |
| `OPT_EXPIRY_INDEX` | 0 | 0 = nearest expiry, 1 = the next one |
| `OPT_SKIP_EXPIRY_DAY` | true | on expiry day, use the next expiry |
| `OPT_SL_PCT` / `OPT_TARGET_PCT` | 25 / 50 | % of premium; target 0 = no fixed target |
| `OPT_TRAIL_TRIGGER_PCT` / `OPT_TRAIL_PCT` | 20 / 15 | see Trailing stop-loss |
| `OPT_MIN_PREMIUM` | 10 | skip contracts priced below this |

## Trailing stop-loss

Once the price has moved **`TRIGGER`%** in your favour, the stop-loss rises to **at least breakeven**. After that it follows the best price seen at a distance of **`TRAIL`%**, and it only ever moves up, never down.

Example with options defaults (trigger 20%, trail 15%), buying at a premium of ₹120:

| Premium | Stop-loss |
|---|---|
| 120 (entry) | 90 (−25%) |
| 150 (+25%, trailing starts) | 127.50 |
| 200 | 170 |
| pulls back to 180 | stays at 170 |
| falls to 169 | **exits**, +₹49 per unit |

Set `OPT_TARGET_PCT=0` to drop the fixed target and let the trailing stop ride big moves.
For stocks, use `TRAIL_TRIGGER_PCT` / `TRAIL_PCT` (off by default).

## Telegram alerts

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the **token**.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": …}`.
3. Put both in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=987654321
   ```

You get these messages, each tagged `[PAPER]` or `[LIVE]`:
- 🤖 start
- 🟢 entries, with SL, target and ₹ risk
- 🔒 trailing stop activated
- ✅/🔴 exits with P&L
- ⚠️ errors (the same error is sent at most once every 5 minutes)
- 🛑 daily loss limit hit
- 📊 end-of-day summary

Alerts are sent in the background, so a slow or failed send never delays an order.

## Shared and stock settings

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `STRATEGY` | `equity` | `equity` or `options` |
| `CAPITAL_PER_TRADE` | 10000 | ₹ per position |
| `MAX_TRADES_PER_DAY` | 6 | entries per day, across all instruments |
| `MAX_DAILY_LOSS` | 1000 | ₹, realized plus open P&L; the bot squares off and halts when it's hit |
| `MAX_OPEN_POSITIONS` | 2 | open positions at once |
| `ENTRY_START` / `ENTRY_END` / `SQUARE_OFF` | 09:30 / 14:45 / 15:15 | IST |
| `INSTRUMENTS` | — | stocks: `SYMBOL:SECURITY_ID:SEGMENT` ([scrip master](https://images.dhan.co/api-data/api-scrip-master.csv)) |
| `STOP_LOSS_PCT` / `TARGET_PCT` | 1.0 / 2.0 | stock exit levels, as % of the entry price |
| `ALLOW_SHORT` | false | allow intraday stock shorts on a bearish signal |

## Layout

```
dhan_autotrader/
  config.py    env config + validation
  broker.py    DhanBroker (live orders) and PaperBroker (simulated fills), rate limiting
  strategy.py  indicators + signals: EMA, Supertrend, RSI, VWAP, ORB
  risk.py      positions, sizing, SL/target, trailing stop, daily limits
  options.py   index presets, option chain, OTM strike/expiry selection, multi-index runner
  bot.py       stock trading loop + shared session loop (daily summary)
  notify.py    Telegram alerts
tests/         unit tests using a fake Dhan client (pytest)
```

## Disclaimer

Trading involves substantial risk of loss. This software comes with no warranty, and past or paper results don't guarantee future returns. You are responsible for every order it places.
