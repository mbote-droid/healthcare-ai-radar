"""De-duplication.

Two passes:
  1. Exact identity via ``Article.dedup_key`` (canonical URL, tracking params
     stripped) - catches the same article fetched twice or re-shared with a
     ``?utm_source=`` suffix.
  2. Cross-source headline match via the normalised title - catches the same
     story carried by several outlets under differing URLs.

When copies collide we keep the *best* one: most credible source, then the one
that actually has a date, then the one with the richer description.
"""

from __future__ import annotations

from config import settings
from radar.logconf import log
from radar.models import Article


def _rank_tuple(article: Article) -> tuple[int, int, int]:
    """Higher tuple == better copy to keep."""
    credibility = settings.CREDIBILITY.get(article.credibility_tier, 0)
    has_date = 1 if article.published is not None else 0
    return (credibility, has_date, len(article.summary_raw))


def _keep_better(a: Article, b: Article) -> Article:
    return a if _rank_tuple(a) >= _rank_tuple(b) else b


def deduplicate(articles: list[Article]) -> list[Article]:
    # Pass 1: exact key.
    by_key: dict[str, Article] = {}
    for art in articles:
        key = art.dedup_key
        existing = by_key.get(key)
        by_key[key] = _keep_better(existing, art) if existing else art

    # Pass 2: normalised headline (only when we actually have a title).
    by_title: dict[str, Article] = {}
    passthrough: list[Article] = []
    for art in by_key.values():
        nt = art.norm_title
        if not nt:
            passthrough.append(art)
            continue
        existing = by_title.get(nt)
        by_title[nt] = _keep_better(existing, art) if existing else art

    result = list(by_title.values()) + passthrough
    log.info(f"dedup: {len(articles)} -> {len(result)} unique articles")
    return result
