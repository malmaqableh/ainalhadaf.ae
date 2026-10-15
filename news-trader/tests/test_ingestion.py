from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from newsbot.config import SourceConfig, SourcesConfig
from newsbot.ingestion.dedup import (canonical_url, count_independent, normalize_headline,
                                     publisher_domain, same_event)
from newsbot.ingestion.poller import Ingestor
from newsbot.storage.db import Article, Event
from conftest import T0, Clock

FIX = Path(__file__).parent / "fixtures"


def mock_http(routes, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        for prefix, (status, body, ctype) in routes.items():
            if str(request.url).startswith(prefix):
                return httpx.Response(status, content=body, headers={"content-type": ctype})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


ROUTES = {
    "https://feed.example/rss": (200, (FIX / "feed.xml").read_bytes(), "application/rss+xml"),
    "https://api.gdeltproject.org": (200, (FIX / "gdelt.json").read_bytes(), "application/json"),
    "https://newsapi.org": (200, (FIX / "newsapi.json").read_bytes(), "application/json"),
}
SOURCES = SourcesConfig(sources=[
    SourceConfig(name="feed", type="rss", url="https://feed.example/rss"),
    SourceConfig(name="gdelt", type="gdelt", query="bitcoin"),
    SourceConfig(name="newsapi", type="newsapi", query="bitcoin"),
])


def test_canonical_url_and_publisher():
    assert canonical_url("HTTP://www.Example.com/a/b/?utm_source=x&id=7&fbclid=y#frag") == \
        "https://example.com/a/b?id=7"
    assert publisher_domain("https://edition.cnn.com/x") == "cnn.com"
    assert publisher_domain("https://www.bbc.co.uk/news/1") == "bbc.co.uk"


def test_normalize_strips_publisher_tag_and_keeps_numbers():
    assert normalize_headline("Bitcoin climbs above $70,000 on ETF inflows - Reuters") == \
        "bitcoin climbs above $70000 on etf inflows"


def test_same_event_matches_rewordings_but_not_opposites():
    a = normalize_headline("Bitcoin surges past $70,000 as ETF inflows jump")
    b = normalize_headline("Bitcoin tops $70,000 as ETF inflows surge")
    assert same_event(a, b, 0.6)
    up = normalize_headline("Bitcoin price rises above $60,000")
    down = normalize_headline("Bitcoin price falls below $60,000")
    assert not same_event(up, down, 0.6)
    assert not same_event(a, normalize_headline("Ethereum developers delay upgrade"), 0.6)


def test_syndicated_copies_count_once():
    arts = [("reuters.com", "fed cuts rates"), ("yahoo.com", "fed cuts rates"),
            ("cnbc.com", "federal reserve lowers rates by quarter point")]
    assert count_independent(arts) == 2
    assert count_independent([("a.com", "x"), ("a.com", "y")]) == 1


def test_ingest_from_all_source_types(db, settings, monkeypatch):
    monkeypatch.setenv("NEWSAPI_KEY", "k")
    calls = []
    ing = Ingestor(db, settings, SOURCES, http=mock_http(ROUTES, calls), clock=Clock(T0))
    assert ing.poll_due() == 3          # old + undated RSS items skipped, bad GDELT row skipped
    with db.session() as s:
        events = s.scalars(select(Event)).all()
        arts = s.scalars(select(Article)).all()
    assert len(events) == 1             # three outlets, one event
    ev = events[0]
    assert ev.n_independent == 3
    assert ev.publishers == ["cnbc.com", "example-news.com", "reuters.com"]
    assert ev.first_published_at == T0 - timedelta(minutes=5)
    rss = next(a for a in arts if a.source_name == "feed")
    assert rss.url == "https://example-news.com/markets/bitcoin-70k?id=7"
    assert rss.summary == "Bitcoin rallied on Monday."
    newsapi_req = next(r for r in calls if "newsapi" in str(r.url))
    assert newsapi_req.headers["X-Api-Key"] == "k"   # key sent as header, not in the URL
    assert "apiKey" not in str(newsapi_req.url)


def test_same_url_is_stored_once(db, settings):
    ing = Ingestor(db, settings, SOURCES, http=mock_http(ROUTES), clock=Clock(T0))
    raws = ing.sources[0].fetch(T0 - timedelta(hours=1))
    assert ing.ingest(raws) == 1
    assert ing.ingest(raws) == 0


def test_failing_source_backs_off_and_others_continue(db, settings, monkeypatch):
    monkeypatch.setenv("NEWSAPI_KEY", "k")
    routes = dict(ROUTES)
    routes["https://feed.example/rss"] = (500, b"oops", "text/plain")
    clock = Clock(T0)
    errors = []
    ing = Ingestor(db, settings, SOURCES, http=mock_http(routes), clock=clock, on_error=errors.append)
    assert ing.poll_due() == 2
    assert ing.failures == {"feed": 1}
    assert ing.next_poll["feed"] == T0 + timedelta(seconds=settings.news_poll_seconds * 2)
    clock.t = T0 + timedelta(seconds=settings.news_poll_seconds)
    ing.poll_due()
    assert ing.failures == {"feed": 1}   # not retried yet: still backing off
    for _ in range(3):
        clock.t = ing.next_poll["feed"]
        ing.poll_due()
    assert ing.failures["feed"] == 4
    assert len(errors) == 1              # alerted once, at the 3rd failure in a row
    routes["https://feed.example/rss"] = ROUTES["https://feed.example/rss"]
    ing.http = mock_http(routes)
    for src in ing.sources:
        src.http = ing.http
    clock.t = ing.next_poll["feed"]
    ing.poll_due()
    assert "feed" not in ing.failures    # reconnected


def test_gdelt_plain_text_error_is_reported(db, settings):
    routes = {"https://api.gdeltproject.org": (200, b"Your search contained invalid syntax", "text/html")}
    ing = Ingestor(db, settings, SourcesConfig(sources=[SourceConfig(name="g", type="gdelt", query="(")]),
                   http=mock_http(routes), clock=Clock(T0))
    ing.poll_due()
    assert ing.failures == {"g": 1}


def test_newsapi_without_key_fails_cleanly(db, settings, monkeypatch):
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    ing = Ingestor(db, settings, SourcesConfig(sources=[SourceConfig(name="n", type="newsapi", query="x")]),
                   http=mock_http(ROUTES), clock=Clock(T0))
    ing.poll_due()
    assert ing.failures == {"n": 1}
