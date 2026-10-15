"""Analyzer interface: a news story in, asset impacts out."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime

DIRECTIONS = ("bullish", "bearish", "neutral")
HORIZONS = ("minutes", "hours", "days")
ASSET_CLASSES = ("stock", "index", "commodity", "currency", "crypto", "bond", "other")


@dataclass
class Story:
    headline: str
    published_at: datetime
    publishers: list[str]
    other_headlines: list[str] = field(default_factory=list)
    body: str = ""


@dataclass
class AssetImpact:
    asset: str
    asset_class: str
    direction: str
    confidence: float
    horizon: str
    reason: str


@dataclass
class AnalysisResult:
    impacts: list[AssetImpact]
    model: str
    raw: dict

    def to_json(self) -> dict:
        return {"model": self.model, "impacts": [asdict(i) for i in self.impacts]}


class AnalysisError(RuntimeError):
    """Analysis failed. retryable=True means try again later (network, rate limit, overload)."""

    def __init__(self, msg: str, retryable: bool):
        super().__init__(msg)
        self.retryable = retryable


class Analyzer(ABC):
    name = "analyzer"

    @abstractmethod
    def analyze(self, story: Story) -> AnalysisResult: ...


def clean_impacts(items: list[dict]) -> list[AssetImpact]:
    """Validate and normalise analyzer output; drop anything malformed."""
    out = []
    for it in items:
        try:
            direction = str(it["direction"]).lower()
            horizon = str(it["horizon"]).lower()
            asset_class = str(it.get("asset_class", "other")).lower()
            conf = float(it["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        if direction not in DIRECTIONS or horizon not in HORIZONS or not str(it.get("asset", "")).strip():
            continue
        out.append(AssetImpact(asset=str(it["asset"]).strip(),
                               asset_class=asset_class if asset_class in ASSET_CLASSES else "other",
                               direction=direction, confidence=min(max(conf, 0.0), 1.0),
                               horizon=horizon, reason=str(it.get("reason", ""))[:300]))
    return out
