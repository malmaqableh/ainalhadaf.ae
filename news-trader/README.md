# News Trader

A news-driven automated trading bot for crypto (spot). It watches news sources,
uses Claude to judge how each story affects specific assets, waits for the
market to confirm, and places trades inside strict risk limits.

**It starts in PAPER mode** (real prices, simulated money, no API keys, no orders
on any exchange). Real-money trading needs two deliberate changes - see
[Live trading](#4-live-trading-real-money).

> **Read this first.**
> - Most automated trading strategies lose money. This bot has **no proven edge**.
>   Backtest it, then paper-trade it for weeks, before risking anything.
> - **Speed.** Professional firms react to news in milliseconds. This bot polls every
>   1-5 minutes, so it usually trades *after* the first move. Market confirmation
>   protects against bad entries but doesn't create an edge.
> - Nothing here is financial advice. Check that your exchange is licensed where you
>   live (in Dubai, the VARA register).

## How it works

```
sources.yaml --> [Ingestion]  RSS / GDELT / NewsAPI, each with its own backoff + reconnect
                     |         articles stored; same story from several outlets -> one event
                 [Analysis]    Claude (or keyword baseline) -> per asset: direction, confidence,
                     |         horizon, one-line reason. Stories older than 30 min are skipped.
                 [Signals]     weighted confidence >= 0.75  AND  >= 2 independent sources
                     |         AND price momentum + volume spike agree   -> trade intent
                 [Risk gate]   the ONLY thing that can approve an order: sizing, exposure,
                     |         daily loss, trades/day, session window, kill switch
                 [Execution]   limit entry -> stop-loss + take-profit (OCO on Binance)
                 [Monitoring]  dashboard, decision log, Telegram/email alerts
```

**Weighted confidence** = model confidence x (0.5 + 0.5 x best publisher reliability)
x confirmation weight (0.85 for 1 source, 0.95 for 2, 1.0 for 3+). With the defaults,
a story confirmed by CNBC and one other outlet needs about 88% model confidence.

**Bearish news** closes a long you hold (it needs the same confidence and sources,
but not market confirmation - reducing risk is always allowed). Spot crypto can't be
shorted, so bearish news never opens a position.

## Setup

Requires Python 3.10+.

```bash
cd news-trader
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp config/settings.example.yaml config/settings.yaml
cp .env.example .env          # add ANTHROPIC_API_KEY (and others as needed)
pytest                        # 107 tests, no network or keys needed
```

### Configuration

| File | What it controls |
|---|---|
| `config/settings.yaml` | mode, polling, thresholds, risk limits, alerts |
| `config/sources.yaml` | news feeds and publisher reliability - add/remove feeds here, no code changes |
| `config/assets.yaml` | tradable symbols, the tickers/names that map to them, relevance keywords |
| `.env` | all API keys and passwords (never committed) |

**News sources: test them first.** The feed URLs in `sources.yaml` could not be tested
where this was built. On your machine:

```bash
python -m newsbot check-sources
```

Disable any that fail. Notes: Reuters closed its public RSS feeds (around 2020, as far
as I know); Bloomberg and FT are paywalled (included but disabled); GDELT is free but
updates only every ~15 minutes; NewsAPI's free plan delays articles (check
newsapi.org/pricing), which defeats the 30-minute freshness rule.

**Claude cost control.** `analysis.prefilter` skips stories that mention no tracked
asset or macro keyword, stale stories are skipped before any call, and
`analysis.max_calls_per_day` is a hard cap. The default model is `claude-opus-5-5`
at `effort: low`; you can set a cheaper model in `analysis.model`.

## 1. Paper trading

```bash
python -m newsbot run          # runs until stopped; logs to logs/newsbot.log
python -m newsbot status       # another terminal: positions, P&L, risk state
python -m newsbot dashboard    # web dashboard on http://localhost:8501
```

The paper account starts with `risk.starting_cash` and survives restarts
(`data/paper_state.json`). Prices are real (Binance public data).

## 2. Backtesting

The backtest runs the **same** bot - analysis, signals, risk gate, executor and fill
rules - on a simulated clock, one tick per 1-minute bar.

```bash
# 1. prices: 1m candles for each asset (needs internet)
python -m newsbot download-prices --start 2026-07-01 --end 2026-08-01
# 2. news: a CSV or JSONL with published_at (ISO 8601, UTC), headline, url[, summary]
# 3. run
python -m newsbot backtest --news data/news.csv --start 2026-07-01 --end 2026-08-01 \
    --engine claude --llm-cutoff YYYY-MM-DD --trades-csv data/trades.csv
```

Reports total return, max drawdown, Sharpe ratio (daily, annualised with
`--periods-per-year`, 365 for crypto), win rate, average win/loss, profit factor,
number of trades and fees.

**No look-ahead:**
- an article becomes visible only at `published_at + --analysis-delay` (default 60 s,
  for fetching and LLM latency);
- signals only see closed bars; entry orders fill on bars after they were placed;
- **LLM look-ahead:** a model may already know how old news played out from its
  training data. With `--engine claude` you must give `--llm-cutoff` (the model's
  training-data cutoff - check Anthropic's model documentation), and the backtest
  refuses any news on or before it. For older history use `--engine keyword`.

Commission (`costs.fee_rate`) is charged on every fill; stop-loss and market exits
pay `costs.slippage`; limit fills don't. Claude analyses are cached in
`data/backtest_analysis_cache.json`, so re-runs don't pay twice.

## Risk rules (enforced in `newsbot/risk/manager.py`)

| Rule | Default | Behaviour |
|---|---|---|
| Risk per trade | 1% of equity | size so hitting the stop (with fees and slippage) loses <= 1% |
| Total exposure | 20% of equity | open + pending positions together |
| Stop-loss / take-profit | by horizon | minutes 1%/2% (60 min max hold), hours 2%/4% (8 h), days 4%/8% (3 d) |
| Daily loss limit | 3% | close everything, no trading until the next day (UTC) |
| Trades per day | 10 | counted at approval, survives restarts |
| One position per asset | - | |
| Session window | off (crypto is 24/7) | when enabled: no trades outside the session or in its first/last 5 minutes |
| Kill switch | - | cancel all orders, close all positions, halt until `resume` |

Signal code can't bypass these: it only produces an `OrderIntent`. Only the risk
manager can create an `ApprovedOrder`, and the executor submits an order only after
the risk manager confirms it issued that exact order, unchanged, unused and unexpired.
The tests in `tests/test_risk.py` cover every rule and these bypass attempts.

### Kill switch

```bash
python -m newsbot kill                 # or: touch data/KILL, or the dashboard button
python -m newsbot resume               # after you've reviewed what happened
```

A running bot reacts within about a second. If the bot is **not** running,
`python -m newsbot kill --now` closes everything from the command line.

## 3. Testnet (exchange paper money)

1. Create **Binance Spot Testnet** API keys.
2. In `.env`: `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET`.
3. In `config/settings.yaml`: `mode: testnet`.

On testnet/live, each filled entry is protected **on the exchange** by one OCO order
(take-profit `LIMIT_MAKER` above, `STOP_LOSS_LIMIT` below; when one fills Binance
cancels the other), so positions stay protected if the bot goes down. The bot also
watches the stop itself as a backup. Other exchanges get a stop-loss order only.

## 4. Live trading (real money)

Only after weeks of paper and testnet results you're satisfied with:

- [ ] API key with **trade permission only - never withdrawals**, IP-restricted if possible.
- [ ] A dedicated sub-account holding only the bot's money (equity = that account's balances).
- [ ] `costs.fee_rate` set to your real fee tier; start small.
- [ ] `mode: live` in `config/settings.yaml` **and** `NEWSBOT_CONFIRM_LIVE=YES` in `.env`.

Without both, the bot refuses to start in live mode.

## Running 24/7 with Docker

```bash
mkdir -p data logs && sudo chown 10001:10001 data logs   # the container runs as uid 10001
docker compose up -d --build
docker compose logs -f bot
```

- `bot` restarts automatically and has a health check (an equity snapshot in the last
  5 minutes; works with the default SQLite database).
- `dashboard` listens on the server's **localhost only** - it has no login. Open it
  through an SSH tunnel: `ssh -L 8501:localhost:8501 you@server`, then
  http://localhost:8501.
- The Docker image could not be built where this was written (no Docker daemon);
  build it on your server and check `docker compose logs` on first start.

## Alerts

Set `alerts.telegram: true` (with `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` from
@BotFather) and/or `alerts.email: true` (SMTP settings in `.env`). You get an alert for
every fill and close, the daily loss limit, the kill switch, and errors (the same error
at most once every `alerts.error_cooldown_minutes`).

## Adding a broker

Implement `newsbot/execution/broker.py:Broker` (market data, account, and the bracket
lifecycle: `submit_entry`, `refresh`, `cancel_entry`, `exit`) and return it from
`newsbot/execution/factory.py`. `SimulatedBroker` and `CcxtSpotBroker` are the two
existing examples; Alpaca or Interactive Brokers would follow the same shape.

## Known limitations

- Dedup is a headline-similarity heuristic: it can miss rewritten copies of one wire
  story (counting them as independent) or occasionally merge two similar stories.
- The keyword analyzer is a crude baseline for testing, not a strategy.
- Exchange holidays aren't modelled in the session window (not needed for crypto).
- Fees paid in a third currency (e.g. BNB) aren't counted in P&L.
- Exchange-side orders were built from ccxt's source and Binance's spot API docs and
  tested against a simulated exchange only - verify on testnet before live.
- In paper mode, positions are checked every `tick_seconds` using 1-minute candles; a
  stop can fill slightly differently than it would on the exchange.

## Project layout

```
config/            settings, sources, assets (YAML)
newsbot/
  ingestion/       sources (RSS, GDELT, NewsAPI), dedup, poller
  analysis/        Claude + keyword analyzers, scoring, analysis service
  signals/         market confirmation, signal generator
  risk/            risk manager (the gate), session window
  execution/       broker interface, simulated broker, ccxt spot broker, executor
  backtest/        replay, metrics
  monitoring/      dashboard, alerts, logging
  storage/         database models
  app.py           the 24/7 loop
  __main__.py      command line
tests/             107 tests (risk, ingestion, analysis, execution, signals, backtest, monitoring)
```
