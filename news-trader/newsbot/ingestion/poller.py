"""Poll every source on its schedule, store articles, group them into events."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

import httpx
from sqlalchemy import select

from ..config import Settings, SourcesConfig
from ..storage.db import Article, Database, Event, utcnow
from .base import NewsSource, RawArticle
from .dedup import canonical_url, count_independent, normalize_headline, publisher_domain, same_event
from .sources import make_http_client, make_source

log = logging.getLogger(__name__)

MAX_BACKOFF = timedelta(minutes=30)
FIRST_LOOKBACK = timedelta(hours=2)


class Ingestor:
    def __init__(self, db: Database, settings: Settings, sources: SourcesConfig,
                 http: httpx.Client | None = None, clock: Callable[[], datetime] = utcnow,
                 on_error: Callable[[str], None] | None = None):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.on_error = on_error or (lambda msg: None)
        self.http = http or make_http_client()
        self.sources: list[NewsSource] = [make_source(c, self.http) for c in sources.sources if c.enabled]
        self.next_poll: dict[str, datetime] = {}
        self.failures: dict[str, int] = {}
        self.last_seen: dict[str, datetime] = {}

    # ---- polling with per-source backoff (auto-reconnect) --------------

    def poll_due(self) -> int:
        """Poll every source whose turn it is. Returns the number of new articles."""
        now = self.clock()
        new = 0
        for src in self.sources:
            if self.next_poll.get(src.name, now) > now:
                continue
            since = self.last_seen.get(src.name, now - FIRST_LOOKBACK)
            try:
                articles = src.fetch(since)
            except Exception as e:  # any feed failure must not stop the others
                n = self.failures.get(src.name, 0) + 1
                self.failures[src.name] = n
                delay = min(timedelta(seconds=self.settings.news_poll_seconds * 2 ** n), MAX_BACKOFF)
                self.next_poll[src.name] = now + delay
                msg = f"news source {src.name} failed ({n} in a row): {e}; retrying in {delay}"
                log.warning(msg)
                if n in (3, 10):  # alert when a feed looks properly down, not on every blip
                    self.on_error(msg)
                continue
            if self.failures.pop(src.name, 0):
                log.info("news source %s recovered", src.name)
            self.next_poll[src.name] = now + timedelta(seconds=self.settings.news_poll_seconds)
            # Small overlap so articles that appear late in a feed aren't missed; duplicates are skipped.
            self.last_seen[src.name] = now - timedelta(minutes=10)
            new += self.ingest(articles)
        return new

    # ---- storage + event clustering ------------------------------------

    def ingest(self, raws: list[RawArticle]) -> int:
        added = 0
        now = self.clock()
        window = timedelta(hours=self.settings.news.dedup_window_hours)
        with self.db.session() as s:
            for raw in raws:
                url = canonical_url(raw.url)
                if s.scalar(select(Article.id).where(Article.url == url)):
                    continue
                published = min(raw.published_at, now)  # no articles from the future
                norm = normalize_headline(raw.headline)
                article = Article(source_name=raw.source_name, publisher=publisher_domain(raw.url),
                                  headline=raw.headline, norm_headline=norm, summary=raw.summary,
                                  text=raw.text, url=url, published_at=published, fetched_at=now)
                candidates = s.scalars(
                    select(Article).where(Article.published_at >= published - window,
                                          Article.published_at <= published + window,
                                          Article.event_id.is_not(None))
                    .order_by(Article.published_at.desc())).all()
                match = next((c for c in candidates
                              if same_event(norm, c.norm_headline, self.settings.news.dedup_similarity)), None)
                if match:
                    event = s.get(Event, match.event_id)
                else:
                    event = Event(headline=raw.headline, first_published_at=published,
                                  last_published_at=published, publishers=[], n_independent=0)
                    s.add(event)
                    s.flush()
                article.event_id = event.id
                s.add(article)
                s.flush()
                members = s.execute(select(Article.publisher, Article.norm_headline)
                                    .where(Article.event_id == event.id)).all()
                event.publishers = sorted({p for p, _ in members})
                event.n_independent = count_independent([(p, h) for p, h in members])
                event.first_published_at = min(event.first_published_at, published)
                event.last_published_at = max(event.last_published_at, published)
                added += 1
            s.commit()
        if added:
            log.info("stored %d new articles", added)
        return added
