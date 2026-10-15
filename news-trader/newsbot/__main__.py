"""Command line.

  python -m newsbot run                 start the bot (paper mode unless configured otherwise)
  python -m newsbot status              positions, P&L, risk state
  python -m newsbot kill [--now]        kill switch: cancel all orders, close all positions, halt
  python -m newsbot resume              clear the kill switch
  python -m newsbot check-sources       fetch every enabled news source once
  python -m newsbot download-prices     save 1m candles for backtests
  python -m newsbot backtest            replay historical news against historical prices
  python -m newsbot dashboard           open the web dashboard
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import func, select

from .config import ConfigError, load_assets, load_settings, load_sources

ROOT = Path(__file__).resolve().parents[1]


def _configs(args):
    settings = load_settings(args.config)
    cfg_dir = Path(args.config).resolve().parent
    return settings, load_sources(cfg_dir / "sources.yaml"), load_assets(cfg_dir / "assets.yaml")


def _analyzer(settings, assets, engine: str | None = None):
    engine = engine or settings.analysis.engine
    if engine == "claude":
        from .analysis.claude import ClaudeAnalyzer
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            logging.warning("ANTHROPIC_API_KEY is not set; Claude calls will fail unless another "
                            "credential (e.g. an `ant auth login` profile) is configured")
        return ClaudeAnalyzer(settings.analysis, assets)
    from .analysis.keyword import KeywordAnalyzer
    return KeywordAnalyzer(assets)


def cmd_run(args) -> int:
    from .app import NewsBot
    from .execution.factory import LiveTradingLocked, make_broker
    from .monitoring.alerts import Alerter
    from .monitoring.logging_setup import setup_logging
    from .storage.db import Database
    settings, sources, assets = _configs(args)
    setup_logging(settings.paths.log_file)
    try:
        broker = make_broker(settings, assets)
    except LiveTradingLocked as e:
        logging.error("%s", e)
        return 2
    if settings.mode == "live":
        logging.warning("LIVE MODE: trading real money")
    db = Database(settings.database_url)
    alerter = Alerter(settings.alerts, mode=settings.mode)
    NewsBot(settings, sources, assets, db, broker, _analyzer(settings, assets), alerter).run_forever()
    return 0


def cmd_status(args) -> int:
    from .risk.manager import STATE_KEY
    from .storage.db import Bracket, Database, EquitySnapshot
    settings, _, _ = _configs(args)
    db = Database(settings.database_url)
    st = db.get_kv(STATE_KEY) or {}
    print(f"mode: {settings.mode}")
    print(f"kill switch: {'ON - ' + st.get('kill_reason', '') if st.get('killed') else 'off'}"
          f"{' (kill file present)' if Path(settings.paths.kill_file).exists() else ''}")
    print(f"daily-loss halt: {'ON for ' + st['halted_day'] if st.get('halted_day') else 'off'}")
    print(f"trades today: {st.get('trades_today', 0)} / {settings.risk.max_trades_per_day}")
    with db.session() as s:
        snap = s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).limit(1)).first()
        if snap:
            print(f"equity: {snap.equity:,.2f} (at {snap.ts:%Y-%m-%d %H:%M} UTC), exposure {snap.exposure:,.2f}")
        active = s.scalars(select(Bracket).where(Bracket.status.in_(("pending", "open")))).all()
        print(f"open/pending positions: {len(active)}")
        for b in active:
            print(f"  {b.status:7} {b.side} {b.qty:.6f} {b.symbol} entry {b.entry_price or b.limit_price:.4f} "
                  f"stop {b.stop_price:.4f} target {b.take_profit_price:.4f}")
        pnl = s.scalar(select(func.sum(Bracket.pnl)).where(Bracket.status == "closed")) or 0.0
        n = s.scalar(select(func.count()).select_from(Bracket).where(Bracket.status == "closed"))
        print(f"closed trades: {n}, realised P&L {pnl:+,.2f}")
    return 0


def cmd_kill(args) -> int:
    from .risk.manager import RiskManager
    from .storage.db import Database
    settings, _, assets = _configs(args)
    db = Database(settings.database_url)
    risk = RiskManager(settings.risk, settings.costs, db, settings.paths.kill_file)
    risk.kill(args.reason)
    print(f"Kill switch ON ({args.reason}). A running bot cancels all orders and closes all positions "
          f"within about a second, then stays halted until `python -m newsbot resume`.")
    if args.now:
        # Only when the bot is NOT running: flatten from here.
        from .execution.executor import Executor
        from .execution.factory import make_broker
        broker = make_broker(settings, assets)
        remaining = Executor(db, broker, risk).flatten_all("kill switch", [a.symbol for a in assets.assets])
        print("All positions closed." if not remaining else f"{remaining} position(s) could not be closed - check logs!")
    return 0


def cmd_resume(args) -> int:
    from .risk.manager import RiskManager
    from .storage.db import Database
    settings, _, _ = _configs(args)
    RiskManager(settings.risk, settings.costs, Database(settings.database_url), settings.paths.kill_file).resume()
    print("Kill switch cleared. Trading resumes on the next tick (daily-loss halts lift by themselves next day).")
    return 0


def cmd_check_sources(args) -> int:
    from .ingestion.sources import make_http_client, make_source
    settings, sources, _ = _configs(args)
    http = make_http_client()
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    ok = True
    for cfg in sources.sources:
        if not cfg.enabled:
            print(f"  -    {cfg.name}: disabled")
            continue
        try:
            items = make_source(cfg, http).fetch(since)
            print(f"  OK   {cfg.name}: {len(items)} articles in the last 6h"
                  + (f", newest: {max(items, key=lambda a: a.published_at).headline[:70]}" if items else ""))
        except Exception as e:
            ok = False
            print(f"  FAIL {cfg.name}: {e}")
    return 0 if ok else 1


def cmd_download_prices(args) -> int:
    from .backtest.replay import save_prices
    from .execution.factory import make_exchange
    settings, _, assets = _configs(args)
    ex = make_exchange(settings, authenticated=False)
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    symbols = args.symbols or [a.symbol for a in assets.assets]
    for sym in symbols:
        rows, cursor, end_ms = [], int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        while cursor < end_ms:
            batch = ex.fetch_ohlcv(sym, "1m", since=cursor, limit=1000)
            new = [b for b in batch if (not rows or b[0] > rows[-1][0]) and b[0] < end_ms]
            if not new:
                break
            rows += new
            cursor = rows[-1][0] + 60_000
        out = Path(args.out) / (sym.replace("/", "_") + ".csv")
        save_prices(rows, out)
        print(f"{sym}: {len(rows)} bars -> {out}")
    return 0


def cmd_backtest(args) -> int:
    from .backtest.replay import CachedAnalyzer, LookAheadError, load_news, load_prices, run_backtest
    settings, sources, assets = _configs(args)
    logging.basicConfig(level=logging.WARNING)
    news = load_news(args.news)
    prices = load_prices(args.prices, [a.symbol for a in assets.assets])
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    analyzer = _analyzer(settings, assets, args.engine)
    if analyzer.name == "claude":
        analyzer = CachedAnalyzer(analyzer, ROOT / "data" / "backtest_analysis_cache.json")
    try:
        report, trades = run_backtest(settings, sources, assets, analyzer, news, prices, start, end,
                                      analysis_delay_seconds=args.analysis_delay,
                                      llm_cutoff=date.fromisoformat(args.llm_cutoff) if args.llm_cutoff else None,
                                      periods_per_year=args.periods_per_year)
    except LookAheadError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(report.text())
    if args.trades_csv:
        with open(args.trades_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "side", "qty", "entry", "exit", "pnl", "opened", "closed", "exit_reason", "reason"])
            for b in trades:
                w.writerow([b.symbol, b.side, b.qty, b.entry_price, b.exit_price, round(b.pnl, 4),
                            b.filled_at, b.closed_at, b.exit_reason, b.reason])
        print(f"trades written to {args.trades_csv}")
    print("\nPast performance does not predict future results.")
    return 0


def cmd_dashboard(args) -> int:
    script = Path(__file__).resolve().parent / "monitoring" / "dashboard.py"
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(script), "--server.port",
                            str(args.port), "--", "--config", str(Path(args.config).resolve())])


def main(argv=None) -> int:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(prog="python -m newsbot", description="News-driven trading bot")
    p.add_argument("--config", default=str(ROOT / "config" / "settings.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="start the bot")
    sub.add_parser("status", help="positions, P&L and risk state")
    k = sub.add_parser("kill", help="kill switch: cancel orders, close positions, halt")
    k.add_argument("--reason", default="manual kill command")
    k.add_argument("--now", action="store_true", help="flatten from this command (only if the bot is NOT running)")
    sub.add_parser("resume", help="clear the kill switch")
    sub.add_parser("check-sources", help="test every enabled news source")
    d = sub.add_parser("download-prices", help="save 1m candles for backtests")
    d.add_argument("--start", required=True)
    d.add_argument("--end", required=True)
    d.add_argument("--symbols", nargs="*")
    d.add_argument("--out", default=str(ROOT / "data" / "prices"))
    b = sub.add_parser("backtest", help="replay historical news against historical prices")
    b.add_argument("--news", required=True, help="CSV/JSONL: published_at, headline, url[, summary]")
    b.add_argument("--prices", default=str(ROOT / "data" / "prices"), help="folder with SYMBOL.csv 1m bars")
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    b.add_argument("--engine", choices=["keyword", "claude"], help="override analysis.engine")
    b.add_argument("--llm-cutoff", help="model training-data cutoff (YYYY-MM-DD); required with --engine claude")
    b.add_argument("--analysis-delay", type=int, default=60, help="seconds from publication to a usable analysis")
    b.add_argument("--periods-per-year", type=int, default=365, help="365 for crypto, 252 for stocks")
    b.add_argument("--trades-csv")
    db_ = sub.add_parser("dashboard", help="open the web dashboard")
    db_.add_argument("--port", type=int, default=8501)
    args = p.parse_args(argv)
    if not Path(args.config).exists():
        print(f"config not found: {args.config}\nCopy config/settings.example.yaml to config/settings.yaml first.",
              file=sys.stderr)
        return 2
    try:
        return {"run": cmd_run, "status": cmd_status, "kill": cmd_kill, "resume": cmd_resume,
                "check-sources": cmd_check_sources, "download-prices": cmd_download_prices,
                "backtest": cmd_backtest, "dashboard": cmd_dashboard}[args.cmd](args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
