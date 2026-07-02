"""End-to-end orchestration.

    fetch -> relevance filter -> recency gate -> dedup -> classify -> rank ->
    summarise -> assemble Digest

Each stage is a pure-ish function from the modules alongside; this file just
wires them together and owns the run-level bookkeeping (timing, source status).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import settings
from radar import classify, dedup, score, sources, summarise
from radar.logconf import configure_logging, log
from radar.models import Article, Digest


def _filter_recent(articles: list[Article], now: datetime) -> list[Article]:
    """Drop anything older than the lookback window; keep undated items.

    Undated stories are kept (and scored neutrally) rather than discarded, so a
    feed that omits timestamps still contributes.
    """
    horizon = now - timedelta(days=settings.LOOKBACK_DAYS)
    kept = [a for a in articles if a.published is None or a.published >= horizon]
    log.info(f"recency gate: kept {len(kept)}/{len(articles)} within "
             f"{settings.LOOKBACK_DAYS} days")
    return kept


def run(llm_limit: int = 12, now: datetime | None = None) -> Digest:
    """Execute the full pipeline and return an assembled Digest."""
    configure_logging()
    now = now or datetime.now(timezone.utc)
    log.info("=== Healthcare AI Radar run starting ===")

    raw, ok, failed = sources.fetch_all()
    relevant = classify.filter_relevant(raw)
    recent = _filter_recent(relevant, now)
    unique = dedup.deduplicate(recent)
    classify.classify_all(unique)
    ranked = score.rank(unique, now)
    summarise.summarise_all(ranked, llm_limit=llm_limit)

    digest = Digest(
        generated_at=now,
        articles=ranked,
        sources_ok=ok,
        sources_failed=failed,
    )
    log.info(f"=== run complete: {len(ranked)} articles in digest ===")
    return digest
