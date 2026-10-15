"""Common types for news sources."""

from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..config import SourceConfig

USER_AGENT = "newsbot/1.0 (news-driven paper trading research)"


@dataclass
class RawArticle:
    source_name: str
    headline: str
    url: str
    published_at: datetime          # timezone-aware UTC
    summary: str = ""
    text: str = ""


class SourceError(RuntimeError):
    pass


class NewsSource(ABC):
    def __init__(self, cfg: SourceConfig, http: httpx.Client):
        self.cfg = cfg
        self.http = http

    @property
    def name(self) -> str:
        return self.cfg.name

    @abstractmethod
    def fetch(self, since: datetime) -> list[RawArticle]:
        """Articles published after `since` (best effort; callers dedupe anyway)."""


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(text: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()
