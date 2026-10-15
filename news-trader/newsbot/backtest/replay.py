"""Replay historical news against historical prices through the real bot.

The same NewsBot loop, risk manager, signals and simulated fills run on a
simulated clock, one tick per one-minute bar. Look-ahead protection:
- an article becomes visible only at published_at + analysis_delay;
- only closed bars are visible to signals; fills use bars after the order;
- stories older than news.max_age_minutes are skipped, as live.
"""

from __future__ import annotations

import bisect
import copy
import csv
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from ..analysis.base import AnalysisResult, Analyzer, AssetImpact, Story
from ..app import NewsBot
from ..config import AssetsConfig, Settings, SourcesConfig
from ..execution.sim import PriceFeed, PriceRange, SimulatedBroker
from ..ingestion.base import RawArticle
from ..ingestion.poller import Ingestor
from ..monitoring.alerts import Alerter
from ..storage.db import Bracket, Database
from .metrics import Report, build_report

log = logging.getLogger(__name__)
BAR_MS = 60_000


class LookAheadError(ValueError):
    pass


# ---- data -----------------------------------------------------------------------

def load_news(path: str | Path) -> list[RawArticle]:
    """CSV or JSONL with published_at (ISO 8601), headline, url and optional summary."""
    path = Path(path)
    rows = ([json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if path.suffix in (".jsonl", ".json") else list(csv.DictReader(path.open())))
    out = []
    for r in rows:
        ts = datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(RawArticle(r.get("source", "history"), r["headline"], r["url"], ts.astimezone(timezone.utc),
                              summary=r.get("summary", "") or ""))
    return sorted(out, key=lambda a: a.published_at)


def load_prices(prices_dir: str | Path, symbols: list[str]) -> dict[str, list[list]]:
    """One CSV per symbol named like BTC_USDT.csv: timestamp(ms),open,high,low,close,volume."""
    out = {}
    for sym in symbols:
        f = Path(prices_dir) / (sym.replace("/", "_") + ".csv")
        if not f.exists():
            continue
        with f.open() as fh:
            reader = csv.reader(fh)
            next(reader)
            out[sym] = sorted([int(float(r[0]))] + [float(x) for x in r[1:6]] for r in reader if r)
    return out


def save_prices(bars: list[list], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        w.writerows(bars)


# ---- historical price feed ---------------------------------------------------------

class HistoricalFeed(PriceFeed):
    def __init__(self, bars: dict[str, list[list]], clock):
        self.data = bars
        self.ts = {s: [b[0] for b in rows] for s, rows in bars.items()}
        self.clock = clock

    def _closed(self, symbol: str) -> int:
        """Number of bars closed at the current time."""
        now_ms = int(self.clock().timestamp() * 1000)
        return bisect.bisect_right(self.ts[symbol], now_ms - BAR_MS)

    def last_price(self, symbol: str) -> float:
        if symbol not in self.data:
            raise KeyError(f"no price history for {symbol}")
        n = self._closed(symbol)
        if n == 0:
            raise RuntimeError(f"no {symbol} bar closed yet at {self.clock()}")
        return self.data[symbol][n - 1][4]

    def bars(self, symbol: str, timeframe: str, limit: int) -> list[list]:
        if timeframe != "1m":
            raise ValueError("backtests use 1m bars")
        if symbol not in self.data:
            return []
        n = self._closed(symbol)
        return self.data[symbol][max(0, n - limit):n]

    def range_since(self, symbol: str, since: datetime) -> PriceRange | None:
        since_ms = int(since.timestamp() * 1000)
        lo = bisect.bisect_left(self.ts[symbol], since_ms)
        rows = self.data[symbol][lo:self._closed(symbol)]
        if not rows:
            return None
        return PriceRange(rows[0][1], max(r[2] for r in rows), min(r[3] for r in rows), rows[-1][4])


# ---- analysis cache (re-runs don't pay for the same LLM call twice) -------------------

class CachedAnalyzer(Analyzer):
    def __init__(self, inner: Analyzer, cache_file: str | Path):
        self.inner = inner
        self.name = inner.name
        self.path = Path(cache_file)
        self.cache = json.loads(self.path.read_text()) if self.path.exists() else {}

    def analyze(self, story: Story) -> AnalysisResult:
        key = hashlib.sha256(json.dumps([self.inner.name, story.headline, story.other_headlines, story.body])
                             .encode()).hexdigest()
        if key not in self.cache:
            result = self.inner.analyze(story)
            self.cache[key] = {"model": result.model, "raw": result.raw,
                               "impacts": [asdict(i) for i in result.impacts]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.cache))
        c = self.cache[key]
        return AnalysisResult([AssetImpact(**i) for i in c["impacts"]], c["model"], c["raw"])


# ---- the replay ------------------------------------------------------------------------

class _Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class _NewsTap:
    """Releases each article only once the simulated clock passes published_at + delay."""

    def __init__(self, ingestor: Ingestor, news: list[RawArticle], delay: timedelta, clock: _Clock):
        self.ingestor, self.news, self.delay, self.clock = ingestor, news, delay, clock
        self.i = 0

    def pending_until(self, t: datetime) -> bool:
        return self.i < len(self.news) and self.news[self.i].published_at + self.delay <= t

    def poll_due(self) -> int:
        batch = []
        while self.pending_until(self.clock()):
            batch.append(self.news[self.i])
            self.i += 1
        return self.ingestor.ingest(batch) if batch else 0


def run_backtest(settings: Settings, sources: SourcesConfig, assets: AssetsConfig, analyzer: Analyzer,
                 news: list[RawArticle], prices: dict[str, list[list]], start: datetime, end: datetime,
                 analysis_delay_seconds: int = 60, llm_cutoff: date | None = None,
                 periods_per_year: int = 365) -> tuple[Report, list[Bracket]]:
    if analyzer.name == "claude":
        if llm_cutoff is None:
            raise LookAheadError(
                "Backtesting with an LLM needs --llm-cutoff (the model's training-data cutoff date). "
                "The model may already know how older news played out, which would make results look "
                "better than they are. Use news after that date, or --engine keyword for older history.")
        if start.date() <= llm_cutoff:
            raise LookAheadError(f"backtest starts {start.date()}, on or before the LLM cutoff {llm_cutoff}: "
                                 "results would be contaminated by look-ahead")
    if not prices:
        raise ValueError("no price data for any configured asset")
    s = copy.deepcopy(settings)
    window = [a for a in news if start - timedelta(minutes=s.news.max_age_minutes) <= a.published_at < end]
    if analyzer.name == "claude" and any(a.published_at.date() <= llm_cutoff for a in window):
        raise LookAheadError(f"some news in the window is dated on or before the LLM cutoff {llm_cutoff}")
    s.mode = "paper"
    s.database_url = "sqlite:///:memory:"
    s.paths.kill_file = "/nonexistent/newsbot-backtest-KILL"
    s.alerts.telegram = s.alerts.email = False
    s.signals.confirmation.timeframe = "1m"
    traded = AssetsConfig(assets=[a for a in assets.assets if a.symbol in prices],
                          macro_keywords=assets.macro_keywords)

    clock = _Clock(start)
    db = Database(s.database_url)
    feed = HistoricalFeed(prices, clock)
    broker = SimulatedBroker(feed, s.costs, s.risk.starting_cash, supports_short=s.risk.allow_short, clock=clock)
    alerter = Alerter(s.alerts, mode="backtest", clock=clock)
    alerter.send = lambda kind, msg: None
    tap =_NewsTap(Ingestor(db, s, SourcesConfig(sources=[]), clock=clock), window,
                   timedelta(seconds=analysis_delay_seconds), clock)
    bot = NewsBot(s, sources, traded, db, broker, analyzer, alerter, ingestor=tap, clock=clock, sleep=lambda x: None)

    first = max(rows[0][0] for rows in prices.values()) + BAR_MS
    clock.t = max(start, datetime.fromtimestamp(first / 1000, timezone.utc))
    curve: list[tuple[datetime, float]] = []
    fresh_until = clock.t
    step = timedelta(minutes=1)
    while clock.t < end:
        if tap.pending_until(clock.t):
            fresh_until = clock.t + timedelta(minutes=s.news.max_age_minutes + 1)
        busy = clock.t <= fresh_until or db.active_brackets()
        if busy or not curve or clock.t.date() != curve[-1][0].date():
            bot.tick()  # quiet minutes with no news and no positions are skipped (nothing can happen)
            curve.append((clock.t, broker.account().equity))
        clock.t += step

    bot.executor.flatten_all("end of backtest", [a.symbol for a in traded.assets])
    curve.append((clock.t, broker.account().equity))
    with db.session() as sess:
        closed = list(sess.scalars(select(Bracket).where(Bracket.status == "closed").order_by(Bracket.closed_at)))
    report = build_report(curve, [b.pnl for b in closed], sum(b.entry_fee + b.exit_fee for b in closed),
                          periods_per_year)
    return report, closed
