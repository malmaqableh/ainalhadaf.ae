"""Combine fresh, confirmed news impacts per asset into trade intents and close requests."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select

from ..analysis.service import weighted_confidence
from ..config import Settings, SourcesConfig
from ..execution.broker import Broker
from ..ingestion.dedup import count_independent
from ..risk.manager import OrderIntent
from ..storage.db import Article, Bracket, Database, Event, Impact, utcnow
from .market_confirm import bars_needed, confirm

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    symbol: str
    direction: str                   # bullish | bearish
    raw_confidence: float            # best model confidence among agreeing stories
    weighted: float                  # after source reliability and confirmation weighting
    horizon: str
    publishers: list[str]
    n_independent: int
    event_ids: list[int]
    reason: str


@dataclass
class CloseRequest:
    symbol: str
    reason: str


@dataclass
class SignalOutput:
    intents: list[OrderIntent] = field(default_factory=list)
    closes: list[CloseRequest] = field(default_factory=list)


class SignalGenerator:
    def __init__(self, db: Database, settings: Settings, sources: SourcesConfig,
                 clock: Callable[[], datetime] = utcnow):
        self.db = db
        self.settings = settings
        self.sources = sources
        self.clock = clock
        self._logged: set[tuple] = set()   # log each distinct decision once, not every tick

    def _log_once(self, key: tuple, action: str, reason: str, symbol: str, data=None) -> None:
        if key not in self._logged:
            self._logged.add(key)
            self.db.log_decision("signal", action, reason, symbol, data)

    def candidates(self) -> list[Candidate]:
        cutoff = self.clock() - timedelta(minutes=self.settings.news.max_age_minutes)
        with self.db.session() as s:
            rows = s.execute(
                select(Impact, Event).join(Event, Impact.event_id == Event.id)
                .where(Event.first_published_at >= cutoff, Impact.symbol.is_not(None),
                       Impact.direction != "neutral")).all()
            groups: dict[tuple[str, str], list[tuple[Impact, Event]]] = {}
            for imp, ev in rows:
                groups.setdefault((imp.symbol, imp.direction), []).append((imp, ev))
            out = []
            for (symbol, direction), items in groups.items():
                event_ids = sorted({ev.id for _, ev in items})
                arts = s.execute(select(Article.publisher, Article.norm_headline)
                                 .where(Article.event_id.in_(event_ids))).all()
                publishers = sorted({p for p, _ in arts})
                n = count_independent([(p, h) for p, h in arts])
                best = max(items, key=lambda x: x[0].confidence)[0]
                out.append(Candidate(
                    symbol=symbol, direction=direction, raw_confidence=best.confidence,
                    weighted=weighted_confidence(best.confidence, publishers, n, self.sources, self.settings.scoring),
                    horizon=best.horizon, publishers=publishers, n_independent=n, event_ids=event_ids,
                    reason=best.reason))
        return out

    def _already_traded(self, event_ids: list[int]) -> bool:
        with self.db.session() as s:
            return s.scalar(select(Bracket.id).where(Bracket.event_id.in_(event_ids)).limit(1)) is not None

    def evaluate(self, broker: Broker, held_long: set[str]) -> SignalOutput:
        cfg = self.settings.signals
        out = SignalOutput()
        cands = self.candidates()
        strong = {(c.symbol, c.direction) for c in cands if c.weighted >= cfg.min_confidence}
        for c in cands:
            key = (c.symbol, c.direction, tuple(c.event_ids))
            info = {"weighted": round(c.weighted, 3), "raw": c.raw_confidence, "sources": c.n_independent,
                    "publishers": c.publishers, "events": c.event_ids}
            if c.weighted < cfg.min_confidence:
                self._log_once(key + ("low",), "below_threshold",
                               f"{c.direction} {c.weighted:.2f} < {cfg.min_confidence}: {c.reason}", c.symbol, info)
                continue
            if c.n_independent < cfg.min_independent_sources:
                self._log_once(key + ("sources", c.n_independent), "awaiting_sources",
                               f"{c.n_independent} independent source(s), need {cfg.min_independent_sources}",
                               c.symbol, info)
                continue
            if c.direction == "bearish" and c.symbol in held_long:
                # Reducing risk is always allowed, even when bullish news is also around.
                out.closes.append(CloseRequest(c.symbol, f"bearish news: {c.reason}"))
                self._log_once(key + ("close",), "close_long", f"bearish news ({c.weighted:.2f}): {c.reason}",
                               c.symbol, info)
                continue
            opposite = "bearish" if c.direction == "bullish" else "bullish"
            if (c.symbol, opposite) in strong:
                self._log_once(key + ("conflict",), "conflict",
                               "strong bullish and bearish news at the same time; standing aside", c.symbol, info)
                continue
            side = "long" if c.direction == "bullish" else "short"
            if side == "short" and not (self.settings.risk.allow_short and broker.supports_short):
                self._log_once(key + ("noshort",), "skip",
                               "bearish news but no position to close and shorting is off", c.symbol, info)
                continue
            if self._already_traded(c.event_ids):
                continue
            bars = broker.bars(c.symbol, cfg.confirmation.timeframe, bars_needed(cfg.confirmation))
            conf = confirm(bars, c.direction == "bullish", cfg.confirmation)
            info.update(momentum=round(conf.momentum, 5), volume_ratio=round(conf.volume_ratio, 3))
            if not conf.ok:
                # re-checked every tick while the news is fresh; logged when the reason changes
                self._log_once(key + ("market", conf.reason.split(":")[0]), "awaiting_market", conf.reason,
                               c.symbol, info)
                continue
            out.intents.append(OrderIntent(
                symbol=c.symbol, side=side, horizon=c.horizon, confidence=c.weighted,
                reason=f"{c.direction} news ({c.weighted:.2f}, {c.n_independent} sources): {c.reason}; {conf.reason}",
                event_id=c.event_ids[-1]))
            self._log_once(key + ("signal",), "signal", f"{side} signal: {conf.reason}", c.symbol, info)
        return out
