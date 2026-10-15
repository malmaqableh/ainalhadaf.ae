# Trading Bot

An autonomous crypto trading bot. Once started, it runs on its own, 24/7:

1. Every `poll_seconds` it checks the price.
2. When a candle closes, it asks the strategy: **buy**, **sell**, or **hold**.
3. It sizes the trade from your risk settings and places the order.
4. It protects every position with a stop-loss and take-profit. In testnet/live mode
   these are real orders resting on the exchange, so they work even when the bot is offline.
5. It enforces a daily loss limit and a max-drawdown kill switch.
6. It saves its state to disk, so it picks up where it left off after a restart.

> **Warning.** Most trading bots lose money. This bot has **no proven edge**: the
> default strategy (moving-average crossover) is a simple baseline, not a money
> maker. Always backtest, then run in `paper` mode for weeks, before risking
> real money. Only trade money you can afford to lose. Nothing here is financial
> advice.

## Setup

Requires Python 3.10+.

```bash
cd trading-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp config.example.yaml config.yaml      # then edit config.yaml
pytest                                  # all tests should pass
```

## 1. Backtest (test on past data)

```bash
python -m bot backtest --days 365 --save-csv data/btc_1h.csv
python -m bot backtest --csv data/btc_1h.csv      # re-run offline on saved data
```

The backtest uses the same strategy and risk rules as the live bot. It includes
fees and slippage, fills each signal at the **next** candle's open (no peeking at
the future), and assumes the stop-loss hits first when a candle touches both the
stop and the target. Compare **Total return** with **Buy & hold return**: if the bot
can't beat simply holding, it isn't adding value.

## 2. Paper trading (real prices, fake money)

```bash
python -m bot run          # runs until you stop it
python -m bot status       # in another terminal: position, P&L, halt state
```

No API keys are needed. The paper account starts with `risk.starting_cash`.

## 3. Testnet (exchange's test network)

Create **testnet** API keys on your exchange (for example the Binance Spot Testnet),
set `mode: testnet`, then:

```bash
export BOT_API_KEY=...  BOT_API_SECRET=...
python -m bot run
```

## 4. Live (real money)

Only after the steps above. Checklist:

- [ ] Your exchange is licensed where you live (in Dubai, check the VARA register).
- [ ] API key has **trade** permission only. **Never enable withdrawals.** Restrict it to your server's IP if the exchange allows it.
- [ ] Use a **dedicated sub-account** holding only the money for the bot. In live mode, equity = all of that account's quote + base balance.
- [ ] Set `fee_rate` to your real fee tier.
- [ ] Start with a small amount.

```bash
# config.yaml: mode: live
export BOT_API_KEY=...  BOT_API_SECRET=...  BOT_CONFIRM_LIVE=YES
python -m bot run
```

The bot refuses to start in live mode without `BOT_CONFIRM_LIVE=YES`.

## Stopping and safety

| What | How |
|---|---|
| Stop now | `Ctrl+C`, or create the kill file: `touch STOP` (delete it to allow restart). Stopping does **not** sell an open position; in testnet/live its exchange stop-loss stays active. |
| Daily loss limit | Down `max_daily_loss_pct` since the start of the UTC day: no new trades until tomorrow. |
| Kill switch | Down `max_drawdown_pct` from the peak: sells everything and **halts**. It stays halted, even after a restart, until you run `python -m bot resume`. |
| Errors | Network errors are logged and retried with backoff, so stops keep being watched. Wrong API keys stop the bot. |

Logs: `logs/bot.log`. State: `state/bot_state.json`.

## Exchange-side stop-loss and take-profit (testnet/live)

Right after each buy, the bot places a **stop-loss sell order on the exchange**
(`risk.exchange_stop_loss: true`). The exchange then protects the position even if
the bot, the server or the internet goes down.

- **Stop-limit by default.** Binance spot does not accept stop-market orders, so the
  order is a stop-limit: it triggers at the stop price and sells no lower than
  `stop_limit_offset_pct` below it. In a very fast crash the price can jump past the
  limit and the order won't fill. The bot keeps watching the price too, and in that
  case cancels the order and sells at market. If your exchange supports stop-market
  orders, `stop_order_type: market` avoids this.
- **Before any other sell** (sell signal, take-profit, kill switch), the bot cancels
  the stop order first, because the order locks the coins. If the stop already filled,
  the bot records that and does not sell again.
- **Fills while offline are picked up.** On the next start, the bot sees that the stop
  filled and records the trade.
- **If the order is cancelled outside the bot** (for example in the exchange app), the
  bot places a new one on its next check.
- **If the exchange doesn't support stop-loss orders,** the bot logs an error and falls
  back to watching the stop itself.

**Take-profit on the exchange (Binance spot).** On spot, the stop-loss and the take-profit
both sell the same coins, so they can't be two separate orders: the first one locks the
coins. With `risk.exchange_take_profit: true` the bot places them together as one
**OCO order** ("one cancels the other"):

- a `LIMIT_MAKER` sell at the take-profit price (above the market), and
- the stop-loss (`STOP_LOSS_LIMIT`, or `STOP_LOSS` with `stop_order_type: market`) below it.

When one leg fills, Binance cancels the other. Cancelling one leg cancels both. If the
take-profit fills only partly, the bot cancels what's left and places a new OCO for the
remaining coins. If Binance's reply can't be read, the bot cancels those orders rather
than leave orders it can't track.

OCO uses Binance's own `orderList/oco` endpoint (ccxt has no standard OCO call for spot),
so it only works on **Binance**. On other exchanges, only the stop-loss goes on the
exchange and the bot watches the take-profit itself. Missing a take-profit while offline
means a missed gain, not a bigger loss.

## Running it 24/7

The bot only trades while it's running, so run it on an always-on server (a small
VPS), not a laptop. Example `systemd` unit (`/etc/systemd/system/trading-bot.service`):

```ini
[Unit]
Description=Trading bot
After=network-online.target

[Service]
WorkingDirectory=/opt/trading-bot
EnvironmentFile=/opt/trading-bot/.env
ExecStart=/opt/trading-bot/.venv/bin/python -m bot run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then: `sudo systemctl enable --now trading-bot` and `journalctl -u trading-bot -f`.

## Known limitations

- Exchange-side orders were built from ccxt's source and Binance's spot API docs,
  and are tested against a simulated exchange only. Other exchanges (Bybit, OKX…) may
  handle trigger orders differently. Test on **testnet** first.
- In paper mode the stop-loss is watched by the bot (there is no exchange order).
- Fees on exchange stop-loss/take-profit fills may not be reported by the exchange's
  order data, so P&L for those trades can leave out the fee.
- Spot, long-only, one symbol at a time. No leverage or shorting.
- Fees paid in a third currency (for example BNB) aren't counted in P&L.
- Backtests use candle data, so the exact order of prices inside a candle is unknown.

## Project layout

```
bot/
  config.py     load + validate config.yaml
  strategy.py   signals (moving-average crossover); add new strategies here
  risk.py       position sizing, daily loss, drawdown
  broker.py     paper / testnet / live execution via ccxt
  engine.py     the autonomous loop
  backtest.py   historical simulation
  data.py       download candles, CSV I/O
  state.py      state that survives restarts
tests/          offline tests (no network, no keys)
```
