"""Typed data structures shared across the pipeline.

Keeping these as small, dependency-free dataclasses means every stage
(fetch -> filter -> classify -> score -> summarise -> render) speaks the same
language and is trivial to construct in tests without touching the network.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalise_title(title: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Used both for stable IDs and for near-duplicate detection, so two feeds
    carrying the same headline with different casing/punctuation collapse to
    the same normalised form.
    """
    if not title:
        return ""
    lowered = title.lower().strip()
    lowered = _NON_ALNUM.sub(" ", lowered)
    return _WHITESPACE.sub(" ", lowered).strip()


def _canonical_url(url: str) -> str:
    """Drop tracking query strings / fragments so the same article at two URLs
    (with and without ``?utm_source=...``) hashes identically."""
    if not url:
        return ""
    cleaned = url.strip()
    for sep in ("?", "#"):
        idx = cleaned.find(sep)
        if idx != -1:
            cleaned = cleaned[:idx]
    return cleaned.rstrip("/").lower()


@dataclass
class Article:
    """A single news / research item flowing through the pipeline."""

    title: str
    url: str
    source: str
    credibility_tier: str
    published: datetime | None = None
    summary_raw: str = ""          # original description / abstract
    category: str = "general"
    summary: str = ""              # final (LLM or extractive) summary
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Defensive normalisation: strings never None, tier always a str.
        self.title = (self.title or "").strip()
        self.url = (self.url or "").strip()
        self.source = (self.source or "unknown").strip()
        self.summary_raw = (self.summary_raw or "").strip()
        # Normalise any tz-aware/naive datetime to aware UTC for safe compares.
        if self.published is not None and self.published.tzinfo is None:
            self.published = self.published.replace(tzinfo=timezone.utc)

    @property
    def norm_title(self) -> str:
        return normalise_title(self.title)

    @property
    def dedup_key(self) -> str:
        """Stable identity used for exact de-duplication and cache keys.

        Prefers the canonical URL; falls back to the normalised title so items
        with no URL still dedupe sensibly.
        """
        basis = _canonical_url(self.url) or self.norm_title
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()

    @property
    def haystack(self) -> str:
        """Lowercased title + raw summary, for keyword scanning."""
        return f"{self.title} {self.summary_raw}".lower()

    def best_summary(self) -> str:
        """The text to render: the produced summary, else the raw text, else
        the title. Guarantees a non-empty string (zero empty outputs rule)."""
        return self.summary or self.summary_raw or self.title or "(no content)"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "credibility_tier": self.credibility_tier,
            "published": self.published.isoformat() if self.published else None,
            "category": self.category,
            "summary": self.best_summary(),
            "score": round(self.score, 2),
            "score_breakdown": self.score_breakdown,
            "tags": self.tags,
        }


@dataclass
class Digest:
    """The assembled newsletter payload."""

    generated_at: datetime
    articles: list[Article] = field(default_factory=list)
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)

    def top(self, n: int) -> list[Article]:
        return self.articles[: max(0, n)]

    def by_category(self) -> dict[str, list[Article]]:
        grouped: dict[str, list[Article]] = {}
        for art in self.articles:
            grouped.setdefault(art.category, []).append(art)
        return grouped

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at.isoformat(),
            "count": len(self.articles),
            "sources_ok": self.sources_ok,
            "sources_failed": self.sources_failed,
            "articles": [a.to_dict() for a in self.articles],
        }
