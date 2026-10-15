"""Command line: python -m bot {backtest,run,status,resume} --config config.yaml"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .backtest import run_backtest
from .broker import LiveTradingNotConfirmed, make_broker, make_exchange
from .config import ConfigError, load_config
from .data import fetch_history, load_csv, save_csv
from .engine import Engine
from .risk import RiskManager
from .state import StateStore
from .strategy import make_strategy


def setup_logging(log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_backtest(args, cfg) -> int:
    if args.csv:
        candles = load_csv(args.csv)
    else:
        exchange = make_exchange(cfg, authenticated=False)
        since = int((time.time() - args.days * 86400) * 1000)
        print(f"Downloading {args.days} days of {cfg.symbol} {cfg.timeframe} candles from {cfg.exchange}...")
        candles = fetch_history(exchange, cfg.symbol, cfg.timeframe, since)
        if args.save_csv:
            save_csv(candles, args.save_csv)
    result = run_backtest(candles, make_strategy(cfg.strategy), RiskManager(cfg.risk),
                          cfg.costs, cfg.risk.starting_cash)
    print(result.report())
    print("\nPast results do not guarantee future results.")
    return 0


def cmd_run(args, cfg) -> int:
    setup_logging(cfg.log_file)
    store = StateStore(cfg.state_file)
    state = store.load()
    if cfg.mode == "paper" and not state.initialized:
        state.cash = cfg.risk.starting_cash
    try:
        broker = make_broker(cfg, state)
    except LiveTradingNotConfirmed as e:
        logging.error("%s", e)
        return 2
    Engine(cfg, broker, make_strategy(cfg.strategy), RiskManager(cfg.risk), store, state).run()
    return 0


def cmd_status(args, cfg) -> int:
    state = StateStore(cfg.state_file).load()
    print(f"mode: {cfg.mode}  symbol: {cfg.symbol}  timeframe: {cfg.timeframe}")
    if not state.initialized:
        print("the bot has not traded yet")
    elif cfg.mode == "paper":
        print(f"paper cash: {state.cash:,.2f}")
    print(f"position: {state.position or 'none'}")
    print(f"peak equity: {state.peak_equity:,.2f}")
    print(f"halted: {state.halted} {state.halt_reason}")
    total = sum(t['pnl'] for t in state.trades)
    print(f"closed trades: {len(state.trades)}  total P&L: {total:,.2f}")
    for t in state.trades[-10:]:
        print(f"  {t['closed_at']}  {t['reason']:<20} pnl={t['pnl']:,.2f}")
    return 0


def cmd_resume(args, cfg) -> int:
    store = StateStore(cfg.state_file)
    state = store.load()
    if not state.halted:
        print("bot is not halted")
        return 0
    state.halted, state.halt_reason = False, ""
    state.peak_equity = 0.0  # re-baselined on the next check
    store.save(state)
    print("halt cleared; the drawdown peak resets to current equity on the next start")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot", description="Autonomous crypto trading bot")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    bt = sub.add_parser("backtest", help="test the strategy on past data")
    bt.add_argument("--days", type=int, default=365)
    bt.add_argument("--csv", help="use candles from this CSV instead of downloading")
    bt.add_argument("--save-csv", help="save downloaded candles to this CSV")
    sub.add_parser("run", help="start trading on its own (paper/testnet/live per config)")
    sub.add_parser("status", help="show position, P&L and halt state")
    sub.add_parser("resume", help="clear a kill-switch halt after you have reviewed it")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    return {"backtest": cmd_backtest, "run": cmd_run,
            "status": cmd_status, "resume": cmd_resume}[args.command](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
