"""Recognise the same story reported by different outlets.

Two levels:
1. Exact: the same canonical URL is stored once.
2. Event: headlines with enough word overlap within a time window join one event.
   Headlines that move in opposite directions ("rises" vs "falls") never merge.

Independent sources = distinct publishers, except that publishers carrying an
identical headline count once (that's usually one wire story republished).
This is a heuristic: it can't detect rewritten wire copy, and can occasionally
merge two different stories with very similar wording.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "mod",
                   "taid", "ncid", "src", "smid", "guccounter", "ocid", "cid", "rss", "feed"}
TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "co.jp", "com.br",
                      "co.in", "com.sg", "co.nz", "co.za", "com.hk", "com.cn", "com.tr", "co.kr"}

STOPWORDS = set("""a an the and or but if of to in on at by for with from as is are was were be been
being it its this that these those after before over under into amid says said say will would could
may might new news report reports reported update live latest how why what who when where than then
about up down vs via its his her their our your""".split()) - {"up", "down"}

UP_WORDS = {"rise", "rises", "rising", "rose", "surge", "surges", "surged", "soar", "soars", "soared",
            "jump", "jumps", "jumped", "gain", "gains", "gained", "rally", "rallies", "rallied",
            "climb", "climbs", "climbed", "up", "higher", "record", "hike", "hikes", "hiked", "approve",
            "approves", "approved", "boost", "boosts"}
DOWN_WORDS = {"fall", "falls", "falling", "fell", "drop", "drops", "dropped", "plunge", "plunges",
              "plunged", "slump", "slumps", "slumped", "tumble", "tumbles", "tumbled", "crash", "crashes",
              "crashed", "sink", "sinks", "sank", "down", "lower", "cut", "cuts", "slash", "slashes",
              "reject", "rejects", "rejected", "ban", "bans", "banned", "hack", "hacked"}

_PUNCT = re.compile(r"[^\w\s$%.]")
_SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]{1,40}$")


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https" if parts.scheme in ("http", "https", "") else parts.scheme,
                       host, path, urlencode(query), ""))


def publisher_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def normalize_headline(headline: str) -> str:
    text = headline.strip()
    # Drop a trailing " - Publisher" / " | Site" tag that aggregators add.
    while True:
        stripped = _SUFFIX.sub("", text)
        if stripped == text or len(stripped.split()) < 4:
            break
        text = stripped
    text = re.sub(r"(?<=\d),(?=\d)", "", text.lower())  # 60,000 -> 60000
    text = _PUNCT.sub(" ", text)
    return " ".join(w.strip(".") for w in text.split() if w.strip("."))


def tokens(norm_headline: str) -> set[str]:
    return {w for w in norm_headline.split() if w not in STOPWORDS and len(w) > 1}


def _polarity(toks: set[str]) -> int:
    up, down = bool(toks & UP_WORDS), bool(toks & DOWN_WORDS)
    return 1 if up and not down else -1 if down and not up else 0


def same_event(a: str, b: str, threshold: float) -> bool:
    """Do two normalized headlines describe the same event?"""
    if a == b:
        return True
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    pa, pb = _polarity(ta), _polarity(tb)
    if pa and pb and pa != pb:
        return False
    shared = len(ta & tb)
    if shared < min(3, len(ta), len(tb)):
        return False
    return shared / min(len(ta), len(tb)) >= threshold


def count_independent(articles: list[tuple[str, str]]) -> int:
    """articles: (publisher, normalized headline). Publishers sharing an identical
    headline are treated as one source (syndicated copy)."""
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    first_by_headline: dict[str, str] = {}
    for publisher, headline in articles:
        find(publisher)
        if headline in first_by_headline:
            parent[find(publisher)] = find(first_by_headline[headline])
        else:
            first_by_headline[headline] = publisher
    return len({find(p) for p in parent})
