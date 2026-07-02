"""Relevance filtering and rule-based categorisation.

No LLM is needed here: a curated keyword lexicon (config.settings) classifies
each article deterministically, which keeps the stage fast, offline, free and
100% testable. The optional LLM only ever *summarises* (see radar.summarise).
"""

from __future__ import annotations

import re

from config import settings
from radar.logconf import log
from radar.models import Article


def _compile_terms(terms: list[str]) -> re.Pattern[str]:
    """Build one alternation regex from a term list.

    Matching is length-aware to balance precision and recall:
      * short tokens (<= 3 chars, e.g. ``ai``, ``llm``, ``fda``) match as whole
        words, so ``ai`` never fires inside ``campaign`` or ``detail``;
      * longer tokens match as *prefixes* (left boundary only), so a stem like
        ``clinic`` catches ``clinical`` and ``regulat`` catches ``regulatory``.
    """
    parts = []
    for term in terms:
        term = term.strip().lower()
        if not term:
            continue
        esc = re.escape(term)
        left = r"\b" if term[0].isalnum() else ""
        if len(term) <= 3:
            right = r"\b" if term[-1].isalnum() else ""
            parts.append(f"{left}{esc}{right}")   # exact word
        else:
            parts.append(f"{left}{esc}")          # prefix
    if not parts:
        # Match-nothing pattern keeps callers branch-free.
        return re.compile(r"(?!x)x")
    return re.compile("|".join(parts), re.IGNORECASE)


# Precompile once at import; these never change during a run.
_AI_RE = _compile_terms(settings.RELEVANCE_TERMS_AI)
_HEALTH_RE = _compile_terms(settings.RELEVANCE_TERMS_HEALTH)
_CATEGORY_RES = {
    cat: _compile_terms(kws) for cat, kws in settings.CATEGORY_KEYWORDS.items()
}
_NOVELTY_RE = _compile_terms(settings.NOVELTY_SIGNALS)


def is_relevant(article: Article) -> bool:
    """Keep only stories that are about AI *and* about health/medicine."""
    text = article.haystack
    return bool(_AI_RE.search(text)) and bool(_HEALTH_RE.search(text))


def filter_relevant(articles: list[Article]) -> list[Article]:
    kept = [a for a in articles if is_relevant(a)]
    log.info(f"relevance filter: kept {len(kept)}/{len(articles)} articles")
    return kept


# Tie-break priority. A single incidental keyword (e.g. "privacy regulations"
# in an ML paper) should not outrank the article's true nature, so on an equal
# hit count we prefer the more likely default. Strong regulatory/clinical
# stories carry several matching keywords and still win on raw count.
_TIEBREAK_PRIORITY = {
    "research": 6,
    "funding": 5,
    "product_launch": 4,
    "regulation": 3,
    "clinical_trial": 3,
    "drama": 2,
    "general": 0,
}


def classify_article(article: Article) -> Article:
    """Assign the best-fitting category and attach novelty tags, in place.

    Each category scores by its count of distinct keyword hits; the highest
    count wins. Ties break on ``_TIEBREAK_PRIORITY`` so an incidental one-word
    match cannot mislabel an otherwise research-shaped article.
    """
    text = article.haystack
    best_cat = "general"
    best_hits = 0
    best_priority = 0
    for cat, pattern in _CATEGORY_RES.items():
        hits = len(set(m.group(0).lower() for m in pattern.finditer(text)))
        if hits == 0:
            continue
        priority = _TIEBREAK_PRIORITY.get(cat, 0)
        if (hits, priority) > (best_hits, best_priority):
            best_hits, best_priority, best_cat = hits, priority, cat

    article.category = best_cat if best_hits > 0 else "general"

    novelty = sorted({m.group(0).lower() for m in _NOVELTY_RE.finditer(text)})
    article.tags = novelty
    return article


def classify_all(articles: list[Article]) -> list[Article]:
    for art in articles:
        classify_article(art)
    return articles
