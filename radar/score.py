"""The Scoop Score - a transparent 0-100 relevance/impact ranking.

The score is a weighted blend of four normalised signals so a reader can see
*why* a story ranked where it did (the breakdown is kept on the article):

    credibility  how trustworthy the source is        (0-1)
    recency      how fresh the story is                (0-1)
    category     editorial signal of the story type    (0-1)
    novelty      presence of "first/breakthrough" cues (0-1)

Deterministic and offline - no LLM, fully unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from config import settings
from radar.models import Article

_CAT_VALUES = settings.CATEGORY_WEIGHTS.values()
_CAT_MIN = min(_CAT_VALUES)
_CAT_MAX = max(_CAT_VALUES)
_MAX_CREDIBILITY = max(settings.CREDIBILITY.values())


def _credibility_component(article: Article) -> float:
    raw = settings.CREDIBILITY.get(article.credibility_tier, 0)
    return raw / _MAX_CREDIBILITY if _MAX_CREDIBILITY else 0.0


def _recency_component(article: Article, now: datetime) -> float:
    """1.0 for something published now, decaying linearly to 0 at LOOKBACK_DAYS.

    Undated articles get a neutral 0.5 so a missing timestamp neither buries nor
    unfairly boosts them.
    """
    if article.published is None:
        return 0.5
    age_days = (now - article.published).total_seconds() / 86400.0
    if age_days < 0:
        # Future-dated (feed clock skew) - treat as brand new, not invalid.
        return 1.0
    horizon = max(1, settings.LOOKBACK_DAYS)
    return max(0.0, 1.0 - age_days / horizon)


def _category_component(article: Article) -> float:
    weight = settings.CATEGORY_WEIGHTS.get(article.category, _CAT_MIN)
    span = _CAT_MAX - _CAT_MIN
    return (weight - _CAT_MIN) / span if span else 0.5


def _novelty_component(article: Article) -> float:
    # Each novelty cue adds 0.5, capped at 1.0 (two cues saturate).
    return min(1.0, len(article.tags) * 0.5)


def compute_score(article: Article, now: datetime | None = None) -> Article:
    """Populate ``article.score`` (0-100) and ``article.score_breakdown``."""
    now = now or datetime.now(timezone.utc)
    components = {
        "credibility": _credibility_component(article),
        "recency": _recency_component(article, now),
        "category": _category_component(article),
        "novelty": _novelty_component(article),
    }
    weighted = sum(settings.SCORE_WEIGHTS[k] * v for k, v in components.items())
    article.score = round(weighted * 100, 2)
    article.score_breakdown = {k: round(v, 3) for k, v in components.items()}
    return article


def rank(articles: list[Article], now: datetime | None = None) -> list[Article]:
    """Score every article and return a new list sorted best-first.

    Ties break on recency then title so ordering is stable and reproducible.
    """
    now = now or datetime.now(timezone.utc)
    for art in articles:
        compute_score(art, now)

    def sort_key(a: Article):
        published_ts = a.published.timestamp() if a.published else 0.0
        return (-a.score, -published_ts, a.title.lower())

    return sorted(articles, key=sort_key)
