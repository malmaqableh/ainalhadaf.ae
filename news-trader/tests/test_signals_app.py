import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from newsbot.analysis.claude import ClaudeAnalyzer
from newsbot.app import NewsBot
from newsbot.config import ConfirmationConfig, SourcesConfig
from newsbot.execution.sim import SimulatedBroker
from newsbot.ingestion.base import RawArticle
from newsbot.ingestion.poller import Ingestor
from newsbot.monitoring.alerts import Alerter
from newsbot.signals.market_confirm import confirm
from newsbot.storage.db import Bracket, Decision, Event
from conftest import T0, Clock
from fakes import FakeFeed

CONF = ConfirmationConfig()


def make_bars(momentum=0.004, spike=3.0, n=65, start=100.0, end_ts=None):
    """65 one-minute bars: flat, then the last 15 rise by `momentum`; last 5 have `spike`x volume."""
    end_ts = end_ts or int(T0.timestamp() * 1000)
    bars = []
    for i in range(n):
        k = i - (n - 16)
        price = start * (1 + momentum * max(0, k) / 15)
        vol = 100 * (spike if i >= n - 5 else 1)
        bars.append([end_ts - (n - i) * 60_000, price, price, price, price, vol])
    return bars


# ---- market confirmation ------------------------------------------------------

def test_confirmation_needs_momentum_and_volume():
    assert confirm(make_bars(), True, CONF).ok
    assert not confirm(make_bars(momentum=0.001), True, CONF).ok           # too little momentum
    assert not confirm(make_bars(spike=1.2), True, CONF).ok                # no volume spike
    assert not confirm(make_bars(), False, CONF).ok                        # price going the wrong way
    assert confirm(make_bars(momentum=-0.004), False, CONF).ok             # bearish confirmed
    assert "not enough price data" in confirm(make_bars(n=10), True, CONF).reason


# ---- full loop with fakes -----------------------------------------------------------

class FakeClaude:
    def __init__(self, direction="bullish", confidence=0.95):
        self.direction, self.confidence = direction, confidence
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        payload = {"market_relevant": True, "assets": [{"asset": "BTC", "asset_class": "crypto",
                   "direction": self.direction, "confidence": self.confidence, "horizon": "hours",
                   "reason": "test story"}]}
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                               content=[SimpleNamespace(type="text", text=json.dumps(payload))])


class FakeIngestor:
    """Feeds prepared articles into the real Ingestor storage/dedup logic."""

    def __init__(self, db, settings, clock):
        self.inner = Ingestor(db, settings, SourcesConfig(sources=[]), clock=clock)
        self.queue = []

    def poll_due(self):
        batch, self.queue = self.queue, []
        return self.inner.ingest(batch)


def bot(db, settings, assets, sources_cfg, tmp_path, direction="bullish", confidence=0.95, bars=None):
    clock = Clock()
    feed = FakeFeed({"BTC/USDT": 100.4, "ETH/USDT": 100.0, "SOL/USDT": 1, "XRP/USDT": 1, "BNB/USDT": 1})
    feed.bar_data["BTC/USDT"] = bars if bars is not None else make_bars()
    broker = SimulatedBroker(feed, settings.costs, 10_000, supports_short=False, clock=clock)
    alerts = []
    alerter = Alerter(settings.alerts, clock=clock)
    alerter.send = lambda kind, msg: alerts.append((kind, msg))
    ing = FakeIngestor(db, settings, clock)
    nb = NewsBot(settings, sources_cfg, assets, db, broker,
                 ClaudeAnalyzer(settings.analysis, assets, client=FakeClaude(direction, confidence)),
                 alerter, ingestor=ing, clock=clock, sleep=lambda s: None)
    return nb, ing, feed, clock, alerts


def two_outlets(minutes_ago=2):
    t = T0 - timedelta(minutes=minutes_ago)
    return [RawArticle("a", "SEC approves spot bitcoin ETF applications", "https://www.reuters.com/x", t),
            RawArticle("b", "SEC approves spot bitcoin ETFs in landmark decision", "https://www.cnbc.com/y", t)]


def decisions(db, action):
    with db.session() as s:
        return [d.reason for d in s.scalars(select(Decision).where(Decision.action == action))]


def test_confirmed_news_becomes_a_paper_trade(db, settings, assets, sources_cfg, tmp_path):
    nb, ing, feed, clock, alerts = bot(db, settings, assets, sources_cfg, tmp_path)
    ing.queue = two_outlets()
    nb.tick()
    with db.session() as s:
        b = s.scalars(select(Bracket)).one()
    assert b.symbol == "BTC/USDT" and b.side == "long" and b.status == "pending"
    assert "2 sources" in b.reason and "market confirms" in b.reason
    nb.tick()                                                       # fills at the limit
    assert db.active_brackets()[0].status == "open"
    assert any(k == "trade" and "FILLED" in m for k, m in alerts)
    nb.tick()
    with db.session() as s:
        assert s.scalar(select(Bracket.id).where(Bracket.id > 1)) is None   # no second trade on the same news


def test_single_source_waits_for_confirmation(db, settings, assets, sources_cfg, tmp_path):
    nb, ing, *_ = bot(db, settings, assets, sources_cfg, tmp_path)
    ing.queue = two_outlets()[:1]
    nb.tick()
    assert db.active_brackets() == []
    assert decisions(db, "awaiting_sources")


def test_no_trade_without_market_confirmation(db, settings, assets, sources_cfg, tmp_path):
    nb, ing, *_ = bot(db, settings, assets, sources_cfg, tmp_path, bars=make_bars(spike=1.0))
    ing.queue = two_outlets()
    nb.tick()
    assert db.active_brackets() == []
    assert "volume" in decisions(db, "awaiting_market")[0]


def test_low_confidence_is_not_traded(db, settings, assets, sources_cfg, tmp_path):
    nb, ing, *_ = bot(db, settings, assets, sources_cfg, tmp_path, confidence=0.7)
    ing.queue = two_outlets()
    nb.tick()
    assert db.active_brackets() == [] and decisions(db, "below_threshold")


def test_stale_news_is_ignored(db, settings, assets, sources_cfg, tmp_path):
    nb, ing, *_ = bot(db, settings, assets, sources_cfg, tmp_path)
    ing.queue = two_outlets(minutes_ago=45)
    nb.tick()
    assert db.active_brackets() == []
    with db.session() as s:
        assert s.scalars(select(Event)).one().analysis_status == "stale"


def test_bearish_news_closes_held_long(db, settings, assets, sources_cfg, tmp_path):
    nb, ing, feed, clock, alerts = bot(db, settings, assets, sources_cfg, tmp_path)
    ing.queue = two_outlets()
    nb.tick(); nb.tick()
    assert db.active_brackets()[0].status == "open"
    nb.analysis.analyzer = ClaudeAnalyzer(settings.analysis, assets, client=FakeClaude("bearish"))
    t = T0 - timedelta(minutes=1)
    ing.queue = [RawArticle("a", "Major crypto exchange hacked, bitcoin withdrawals frozen", "https://www.reuters.com/h", t),
                 RawArticle("b", "Crypto exchange hacked; bitcoin withdrawals frozen", "https://www.cnbc.com/h", t)]
    nb.tick()
    with db.session() as s:
        b = s.get(Bracket, 1)
    assert b.status == "closed" and b.exit_reason.startswith("bearish news")


def test_kill_switch_flattens_and_blocks_new_trades(db, settings, assets, sources_cfg, tmp_path):
    nb, ing, feed, clock, alerts = bot(db, settings, assets, sources_cfg, tmp_path)
    ing.queue = two_outlets()
    nb.tick(); nb.tick()
    (tmp_path / "KILL").touch()
    nb.tick()
    assert db.active_brackets() == []
    assert any(k == "risk" and "KILL SWITCH" in m for k, m in alerts)
    ing.queue = [RawArticle("a", "Ethereum ETF approved by regulators", "https://www.reuters.com/e", T0),
                 RawArticle("b", "Regulators approve Ethereum ETF", "https://www.cnbc.com/e", T0)]
    nb.tick()
    with db.session() as s:
        assert s.scalar(select(Bracket.id).where(Bracket.symbol == "ETH/USDT")) is None


def test_daily_loss_limit_flattens_and_alerts(db, settings, assets, sources_cfg, tmp_path):
    settings.risk.max_daily_loss_pct = 0.001
    nb, ing, feed, clock, alerts = bot(db, settings, assets, sources_cfg, tmp_path)
    ing.queue = two_outlets()
    nb.tick(); nb.tick()
    feed.prices["BTC/USDT"] = 99.0              # small drop, but > 0.1% of equity on a 20% position
    nb.tick()
    assert db.active_brackets() == []
    assert any(k == "risk" and "DAILY LOSS" in m for k, m in alerts)


def test_kill_file_wakes_the_sleeping_loop(db, settings, assets, sources_cfg, tmp_path):
    nb, *_ = bot(db, settings, assets, sources_cfg, tmp_path)
    slept = []

    def sleep(s):
        slept.append(s)
        if len(slept) == 3:
            (tmp_path / "KILL").touch()
    nb.sleep = sleep
    nb._sleep_watching_kill_file(600)
    assert len(slept) == 3                       # returned right after the file appeared


def test_loop_survives_errors(db, settings, assets, sources_cfg, tmp_path):
    nb, *_ = bot(db, settings, assets, sources_cfg, tmp_path)
    calls = {"n": 0}

    def tick():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("exchange down")
        raise KeyboardInterrupt
    nb.tick = tick
    nb._sleep_watching_kill_file = lambda s: None
    nb.run_forever()
    assert calls["n"] == 2


def test_conflicting_news_blocks_new_entries(db, settings, assets, sources_cfg, tmp_path):
    nb, ing, *_ = bot(db, settings, assets, sources_cfg, tmp_path)
    ing.queue = two_outlets()
    nb.analysis.process_pending = lambda: 0          # hold analysis until both stories are in
    nb.tick()
    from newsbot.analysis.service import AnalysisService
    t = T0 - timedelta(minutes=1)
    ing.queue = [RawArticle("a", "Major crypto exchange hacked, bitcoin withdrawals frozen", "https://www.reuters.com/h", t),
                 RawArticle("b", "Crypto exchange hacked; bitcoin withdrawals frozen", "https://www.cnbc.com/h", t)]
    ing.poll_due()
    bull = AnalysisService(db, settings, assets, ClaudeAnalyzer(settings.analysis, assets, client=FakeClaude("bullish")), clock=nb.clock)
    bull.process_pending(limit=1)                     # newest first: the hack story gets "bullish" here ...
    bear = AnalysisService(db, settings, assets, ClaudeAnalyzer(settings.analysis, assets, client=FakeClaude("bearish")), clock=nb.clock)
    bear.process_pending()                            # ... and the ETF story "bearish": both strong
    out = nb.signals.evaluate(nb.broker, held_long=set())
    assert out.intents == [] and decisions(db, "conflict")
