from datetime import date, datetime, timedelta, timezone

import pytest

from newsbot.analysis.base import AnalysisResult, Analyzer
from newsbot.analysis.keyword import KeywordAnalyzer
from newsbot.backtest.metrics import build_report, max_drawdown, sharpe_ratio
from newsbot.backtest.replay import CachedAnalyzer, HistoricalFeed, LookAheadError, load_news, run_backtest
from newsbot.ingestion.base import RawArticle
from conftest import Clock

S = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
MS = lambda t: int(t.timestamp() * 1000)  # noqa: E731


def price_path(rise_start_min=None, minutes=8 * 60, rise_per_min=0.0005, rise_len=150):
    bars, p = [], 100.0
    for i in range(minutes):
        rising = rise_start_min is not None and rise_start_min <= i < rise_start_min + rise_len
        o = p
        if rising:
            p *= 1 + rise_per_min
        vol = 400 if rising else 100
        bars.append([MS(S + timedelta(minutes=i)), o, max(o, p) * 1.0002, min(o, p) * 0.9998, p, vol])
    return bars


def story(minute, url_suffix=""):
    t = S + timedelta(minutes=minute)
    return [RawArticle("h", "Bitcoin ETF inflows surge to record high", f"https://www.reuters.com/a{url_suffix}", t),
            RawArticle("h", "Bitcoin ETF inflows surge to a record", f"https://www.cnbc.com/b{url_suffix}", t)]


def run(settings, sources_cfg, assets, news, bars, analyzer=None, **kw):
    return run_backtest(settings, sources_cfg, assets, analyzer or KeywordAnalyzer(assets), news,
                        {"BTC/USDT": bars}, S, S + timedelta(minutes=len(bars)), **kw)


# ---- metrics --------------------------------------------------------------------

def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)


def test_sharpe():
    assert sharpe_ratio([100, 101, 102.01], 365) is None            # zero variance
    s = sharpe_ratio([100, 101, 100.5, 102], 365)
    rets = [0.01, 100.5 / 101 - 1, 102 / 100.5 - 1]
    mean = sum(rets) / 3
    sd = (sum((r - mean) ** 2 for r in rets) / 2) ** 0.5
    assert s == pytest.approx(mean / sd * 365 ** 0.5)


def test_report_trade_stats():
    t = S
    r = build_report([(t, 100), (t + timedelta(days=1), 110)], [10, -4, 6, -2], fees=1.5)
    assert r.total_return == pytest.approx(0.10)
    assert r.win_rate == 0.5 and r.avg_win == 8 and r.avg_loss == -3
    assert r.profit_factor == pytest.approx(16 / 6) and r.trades == 4


def test_sharpe_counts_first_day_return():
    t = S
    r = build_report([(t, 100), (t + timedelta(hours=12), 110), (t + timedelta(days=1), 99)], [], 0)
    assert r.sharpe == pytest.approx(sharpe_ratio([100, 110, 99], 365))


# ---- historical feed: only closed bars ----------------------------------------------

def test_historical_feed_sees_only_closed_bars():
    bars = price_path(rise_start_min=0)                     # price changes every bar
    clock = Clock(S + timedelta(minutes=10, seconds=30))    # bar 10 (10:00-11:00) is still forming
    feed = HistoricalFeed({"BTC/USDT": bars}, clock)
    assert feed.last_price("BTC/USDT") == bars[9][4]        # bars 0..9 have closed
    visible = feed.bars("BTC/USDT", "1m", 100)
    assert len(visible) == 10 and visible[-1] == bars[9]
    r = feed.range_since("BTC/USDT", S + timedelta(minutes=8))
    assert (r.open, r.last) == (bars[8][1], bars[9][4])     # bars 8 and 9 only
    clock.t = S + timedelta(minutes=11)
    assert feed.last_price("BTC/USDT") == bars[10][4]


# ---- the replay ---------------------------------------------------------------------------

def test_news_with_confirming_move_is_traded_after_publication(settings, sources_cfg, assets):
    bars = price_path(rise_start_min=120)
    report, trades = run(settings, sources_cfg, assets, story(120), bars)
    assert report.trades == 1
    t = trades[0]
    published = S + timedelta(minutes=120)
    assert t.created_at >= published + timedelta(seconds=60)     # not before publish + analysis delay
    assert t.filled_at > t.created_at
    assert t.exit_reason == "take-profit" and t.pnl > 0
    assert report.end_equity > report.start_equity


def test_move_before_the_news_is_not_traded(settings, sources_cfg, assets):
    bars = price_path(rise_start_min=30, rise_len=60)               # rally ends 30 min before the story
    report, _ = run(settings, sources_cfg, assets, story(120), bars)
    assert report.trades == 0


def test_news_without_market_reaction_is_not_traded(settings, sources_cfg, assets):
    report, _ = run(settings, sources_cfg, assets, story(120), price_path())
    assert report.trades == 0 and report.total_return == 0


def test_single_source_story_is_not_traded(settings, sources_cfg, assets):
    report, _ = run(settings, sources_cfg, assets, story(120)[:1], price_path(rise_start_min=120))
    assert report.trades == 0


def test_longer_analysis_delay_enters_later(settings, sources_cfg, assets):
    bars = price_path(rise_start_min=120)
    _, fast = run(settings, sources_cfg, assets, story(120), bars, analysis_delay_seconds=60)
    _, slow = run(settings, sources_cfg, assets, story(120), bars, analysis_delay_seconds=600)
    assert slow[0].created_at >= S + timedelta(minutes=130)
    assert slow[0].entry_price > fast[0].entry_price               # later entry, worse price


class FakeLLM(Analyzer):
    name = "claude"

    def __init__(self):
        self.calls = 0

    def analyze(self, story):
        self.calls += 1
        return AnalysisResult([], "fake", {})


def test_llm_backtest_requires_cutoff_and_news_after_it(settings, sources_cfg, assets):
    bars = price_path()
    with pytest.raises(LookAheadError, match="llm-cutoff"):
        run(settings, sources_cfg, assets, story(120), bars, analyzer=FakeLLM())
    with pytest.raises(LookAheadError, match="on or before"):
        run(settings, sources_cfg, assets, story(120), bars, analyzer=FakeLLM(), llm_cutoff=date(2026, 8, 3))
    report, _ = run(settings, sources_cfg, assets, story(120), bars, analyzer=FakeLLM(), llm_cutoff=date(2026, 6, 1))
    assert report.trades == 0


def test_cached_analyzer_pays_once(tmp_path, assets):
    from newsbot.analysis.base import Story
    inner = FakeLLM()
    c = CachedAnalyzer(inner, tmp_path / "cache.json")
    st = Story("Bitcoin ETF approved", S, ["reuters.com"])
    c.analyze(st)
    CachedAnalyzer(inner, tmp_path / "cache.json").analyze(st)     # new run, same story
    assert inner.calls == 1


def test_load_news_csv_and_jsonl(tmp_path):
    (tmp_path / "n.csv").write_text("published_at,headline,url\n2026-08-03T02:00:00Z,B,https://x.com/b\n"
                                    "2026-08-03T01:00:00Z,A,https://x.com/a\n")
    (tmp_path / "n.jsonl").write_text('{"published_at": "2026-08-03T01:00:00+00:00", "headline": "A", "url": "u"}\n')
    assert [a.headline for a in load_news(tmp_path / "n.csv")] == ["A", "B"]
    assert load_news(tmp_path / "n.jsonl")[0].published_at == S + timedelta(hours=1)
