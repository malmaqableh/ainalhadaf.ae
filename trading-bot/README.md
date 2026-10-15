# Trading Bot

An autonomous crypto trading bot. Once started, it runs on its own, 24/7:

1. Every `poll_seconds` it checks the price.
2. When a candle closes, it asks the strategy: **buy**, **sell**, or **hold**.
3. It sizes the trade from your risk settings and places the order.
4. It watches the stop-loss and take-profit of the open position.
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
| Stop now | `Ctrl+C`, or create the kill file: `touch STOP` (delete it to allow restart). Stopping does **not** sell an open position. |
| Daily loss limit | Down `max_daily_loss_pct` since the start of the UTC day: no new trades until tomorrow. |
| Kill switch | Down `max_drawdown_pct` from the peak: sells everything and **halts**. It stays halted, even after a restart, until you run `python -m bot resume`. |
| Errors | Network errors are logged and retried with backoff, so stops keep being watched. Wrong API keys stop the bot. |

Logs: `logs/bot.log`. State: `state/bot_state.json`.

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

- **Stop-loss is watched by the bot, not placed on the exchange.** If the bot or
  server is down, the stop isn't enforced. Placing exchange-side stop orders is a
  planned improvement before trading meaningful size.
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
