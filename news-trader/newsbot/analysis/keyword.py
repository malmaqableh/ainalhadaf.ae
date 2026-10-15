"""Free, offline baseline analyzer: asset keywords + a small sentiment word list.

Crude by design - useful for testing the pipeline and for backtests on news from
before an LLM's training cutoff. Don't expect it to trade well.
"""

from __future__ import annotations

import re

from ..config import AssetsConfig
from .base import AnalysisResult, Analyzer, AssetImpact, Story

BULLISH = {"approve", "approves", "approved", "approval", "inflows", "surge", "surges", "rally", "rallies",
           "record", "adoption", "adopts", "partnership", "upgrade", "launch", "launches", "buy", "buys",
           "bullish", "beat", "beats", "soar", "soars", "jump", "jumps", "gain", "gains", "rise", "rises",
           "cut", "cuts", "easing", "stimulus", "accumulate", "reserve"}
BEARISH = {"hack", "hacked", "exploit", "ban", "bans", "crackdown", "lawsuit", "sues", "sued", "charges",
           "fraud", "bankruptcy", "bankrupt", "collapse", "plunge", "plunges", "crash", "crashes", "outflows",
           "hike", "hikes", "selloff", "sell-off", "bearish", "delay", "delays", "reject", "rejects",
           "rejected", "liquidation", "liquidations", "drop", "drops", "fall", "falls", "slump", "slumps",
           "investigation", "probe", "halt", "halts", "freeze", "freezes"}
_WORD = re.compile(r"[a-z][a-z\-]+")


class KeywordAnalyzer(Analyzer):
    name = "keyword"

    def __init__(self, assets: AssetsConfig):
        self.assets = assets

    def analyze(self, story: Story) -> AnalysisResult:
        text = " ".join([story.headline, *story.other_headlines, story.body]).lower()
        words = _WORD.findall(text)
        score = sum(w in BULLISH for w in words) - sum(w in BEARISH for w in words)
        impacts = []
        if score:
            direction = "bullish" if score > 0 else "bearish"
            confidence = min(0.6 + 0.1 * abs(score), 0.95)
            for a in self.assets.assets:
                terms = [k.lower() for k in a.keywords] + [x.lower() for x in a.aliases]
                if any(re.search(rf"\b{re.escape(t)}\b", text) for t in terms):
                    impacts.append(AssetImpact(a.symbol.split("/")[0], a.asset_class, direction, confidence,
                                               "hours", f"keyword score {score:+d}"))
        return AnalysisResult(impacts=impacts, model="keyword", raw={"score": score})
