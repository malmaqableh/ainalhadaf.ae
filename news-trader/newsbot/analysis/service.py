"""Turns new events into scored asset impacts: freshness, prefilter, budget, analysis, storage."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select

from ..config import AssetsConfig, ScoringConfig, Settings, SourcesConfig
from ..storage.db import Analysis, Article, Database, Event, Impact, utcnow
from .base import AnalysisError, Analyzer, Story

log = logging.getLogger(__name__)

BODY_CHARS = 4000        # cost cap: text sent per story
MAX_RETRIES = 3
BUDGET_KEY = "llm_budget"


def reliability_factor(publishers: list[str], sources: SourcesConfig, scoring: ScoringConfig) -> float:
    best = max((sources.reliability_of(p, scoring.default_reliability) for p in publishers),
               default=scoring.default_reliability)
    return 0.5 + 0.5 * best


def confirmation_weight(n_sources: int, scoring: ScoringConfig) -> float:
    w = scoring.confirmation_weights
    return w[min(max(n_sources, 1), len(w)) - 1]


def weighted_confidence(confidence: float, publishers: list[str], n_sources: int,
                        sources: SourcesConfig, scoring: ScoringConfig) -> float:
    return confidence * reliability_factor(publishers, sources, scoring) * confirmation_weight(n_sources, scoring)


class AnalysisService:
    def __init__(self, db: Database, settings: Settings, assets: AssetsConfig, analyzer: Analyzer,
                 clock: Callable[[], datetime] = utcnow, on_error: Callable[[str], None] | None = None):
        self.db = db
        self.settings = settings
        self.assets = assets
        self.analyzer = analyzer
        self.clock = clock
        self.on_error = on_error or (lambda msg: None)
        self.attempts: dict[int, int] = {}
        terms = {k.lower() for a in assets.assets for k in (*a.keywords, *a.aliases, a.symbol.split("/")[0])}
        terms |= {k.lower() for k in assets.macro_keywords}
        self._relevant = re.compile(r"\b(" + "|".join(sorted(map(re.escape, terms), key=len, reverse=True)) + r")\b")

    def _use_budget(self) -> bool:
        if self.analyzer.name == "keyword":
            return True
        today = self.clock().date().isoformat()
        b = self.db.get_kv(BUDGET_KEY) or {}
        if b.get("day") != today:
            b = {"day": today, "calls": 0}
        if b["calls"] >= self.settings.analysis.max_calls_per_day:
            return False
        b["calls"] += 1
        self.db.set_kv(BUDGET_KEY, b)
        return True

    def process_pending(self, limit: int = 25) -> int:
        """Analyse new events. Returns how many were analysed."""
        now = self.clock()
        max_age = timedelta(minutes=self.settings.news.max_age_minutes)
        done = 0
        with self.db.session() as s:
            events = s.scalars(select(Event).where(Event.analysis_status == "pending")
                               .order_by(Event.first_published_at.desc()).limit(limit)).all()
            for ev in events:
                age = now - ev.first_published_at
                if age > max_age:
                    ev.analysis_status = "stale"
                    s.commit()
                    self.db.log_decision("analysis", "skipped_stale",
                                         f"first reported {age.total_seconds() / 60:.0f} min ago "
                                         f"(limit {self.settings.news.max_age_minutes}): {ev.headline}")
                    continue
                arts = s.scalars(select(Article).where(Article.event_id == ev.id)
                                 .order_by(Article.published_at)).all()
                story = Story(headline=ev.headline, published_at=ev.first_published_at, publishers=ev.publishers,
                              other_headlines=sorted({a.headline for a in arts} - {ev.headline})[:5],
                              body=next((a.text or a.summary for a in arts if a.text or a.summary), "")[:BODY_CHARS])
                if self.settings.analysis.prefilter and not self._relevant.search(
                        " ".join([story.headline, *story.other_headlines, story.body]).lower()):
                    ev.analysis_status = "filtered"
                    s.commit()
                    continue
                if not self._use_budget():
                    ev.analysis_status = "budget"
                    s.commit()
                    self.db.log_decision("analysis", "skipped_budget",
                                         f"daily LLM call cap ({self.settings.analysis.max_calls_per_day}) reached: "
                                         f"{ev.headline}")
                    continue
                try:
                    result = self.analyzer.analyze(story)
                except AnalysisError as e:
                    n = self.attempts.get(ev.id, 0) + 1
                    self.attempts[ev.id] = n
                    if e.retryable and n < MAX_RETRIES:
                        log.warning("analysis of event %s failed (try %d), will retry: %s", ev.id, n, e)
                        continue
                    ev.analysis_status = "failed"
                    s.add(Analysis(event_id=ev.id, engine=self.analyzer.name, error=str(e), created_at=now))
                    s.commit()
                    self.db.log_decision("analysis", "failed", f"{e}: {ev.headline}")
                    if not e.retryable:
                        self.on_error(f"News analysis failed: {e}")
                    continue
                analysis = Analysis(event_id=ev.id, engine=self.analyzer.name, model=result.model,
                                    raw=result.to_json(), created_at=now)
                s.add(analysis)
                s.flush()
                for imp in result.impacts:
                    asset = self.assets.resolve(imp.asset)
                    s.add(Impact(analysis_id=analysis.id, event_id=ev.id, asset=imp.asset,
                                 asset_class=imp.asset_class, symbol=asset.symbol if asset else None,
                                 direction=imp.direction, confidence=imp.confidence, horizon=imp.horizon,
                                 reason=imp.reason, created_at=now))
                ev.analysis_status = "done"
                s.commit()
                done += 1
                self.db.log_decision("analysis", "analyzed", ev.headline, data=result.to_json())
                log.info("analyzed event %s: %s", ev.id, result.to_json()["impacts"])
        return done
