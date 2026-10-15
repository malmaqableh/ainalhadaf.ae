"""RSS, GDELT and NewsAPI sources."""

from __future__ import annotations

import calendar
import logging
import os
from datetime import datetime, timezone

import feedparser
import httpx

from ..config import SourceConfig
from .base import NewsSource, RawArticle, SourceError, strip_html

log = logging.getLogger(__name__)


class RssSource(NewsSource):
    """Any RSS/Atom feed. Uses ETag/Last-Modified so unchanged feeds cost almost nothing."""

    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        self._etag: str | None = None
        self._modified: str | None = None

    def fetch(self, since: datetime) -> list[RawArticle]:
        headers = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._modified:
            headers["If-Modified-Since"] = self._modified
        resp = self.http.get(self.cfg.url, headers=headers)
        if resp.status_code == 304:
            return []
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}")
        self._etag = resp.headers.get("etag")
        self._modified = resp.headers.get("last-modified")
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            raise SourceError(f"unreadable feed: {feed.get('bozo_exception')}")
        out = []
        for e in feed.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if not ts or not e.get("link") or not e.get("title"):
                continue  # without a date we can't tell if it's fresh, so skip it
            published = datetime.fromtimestamp(calendar.timegm(ts), timezone.utc)
            if published < since:
                continue
            text = ""
            if e.get("content"):
                text = strip_html(e.content[0].get("value", ""))
            out.append(RawArticle(self.name, strip_html(e.title), e.link, published,
                                  summary=strip_html(e.get("summary", "")), text=text))
        return out


class GdeltSource(NewsSource):
    """GDELT DOC 2.0 API (free). GDELT refreshes about every 15 minutes."""

    URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    def fetch(self, since: datetime) -> list[RawArticle]:
        minutes = max(15, min(1440, int((datetime.now(timezone.utc) - since).total_seconds() // 60) + 1))
        query = self.cfg.query
        if self.cfg.language == "en":
            query += " sourcelang:english"
        resp = self.http.get(self.URL, params={
            "query": query, "mode": "artlist", "format": "json",
            "maxrecords": 250, "timespan": f"{minutes}min", "sort": "datedesc"})
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            # GDELT answers bad queries with a plain-text message
            raise SourceError(f"non-JSON reply: {resp.text[:200]}") from None
        out = []
        for a in data.get("articles") or []:
            try:
                published = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if published >= since and a.get("url") and a.get("title"):
                out.append(RawArticle(self.name, a["title"].strip(), a["url"], published))
        return out


class NewsApiSource(NewsSource):
    """newsapi.org /v2/everything. Needs NEWSAPI_KEY."""

    URL = "https://newsapi.org/v2/everything"

    def fetch(self, since: datetime) -> list[RawArticle]:
        key = os.environ.get("NEWSAPI_KEY")
        if not key:
            raise SourceError("NEWSAPI_KEY is not set")
        resp = self.http.get(self.URL, headers={"X-Api-Key": key}, params={
            "q": self.cfg.query, "language": self.cfg.language, "sortBy": "publishedAt",
            "from": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "pageSize": 100})
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or data.get("status") != "ok":
            raise SourceError(f"HTTP {resp.status_code}: {data.get('message') or resp.text[:200]}")
        out = []
        for a in data.get("articles") or []:
            try:
                published = datetime.strptime(a["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError, TypeError):
                continue
            if published >= since and a.get("url") and a.get("title"):
                out.append(RawArticle(self.name, a["title"].strip(), a["url"], published,
                                      summary=a.get("description") or "", text=a.get("content") or ""))
        return out


SOURCE_TYPES = {"rss": RssSource, "gdelt": GdeltSource, "newsapi": NewsApiSource}


def make_source(cfg: SourceConfig, http: httpx.Client) -> NewsSource:
    return SOURCE_TYPES[cfg.type](cfg, http)


def make_http_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=True,
                        headers={"User-Agent": "newsbot/1.0"})


