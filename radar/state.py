"""Persistent "already seen" store so scheduled runs email only new stories.

Keyed by ``Article.dedup_key`` (stable across runs). Stored as a small JSON map
of ``key -> first-seen ISO timestamp`` and pruned by age so it can't grow
unbounded. Every method degrades gracefully: a missing or corrupt file is
treated as "nothing seen yet" rather than an error.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings
from radar.logconf import log
from radar.models import Article


class SeenStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.SEEN_FILE
        self._seen: dict[str, str] = {}

    def load(self) -> "SeenStore":
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                seen = data.get("seen", {}) if isinstance(data, dict) else {}
                self._seen = {k: v for k, v in seen.items() if isinstance(v, str)}
        except (ValueError, OSError) as exc:
            log.warning(f"seen-store unreadable ({exc}); starting fresh")
            self._seen = {}
        return self

    def is_new(self, article: Article) -> bool:
        return article.dedup_key not in self._seen

    def filter_new(self, articles: list[Article]) -> list[Article]:
        new = [a for a in articles if self.is_new(a)]
        log.info(f"new-since-last-run: {len(new)}/{len(articles)} articles are new")
        return new

    def record(self, articles: list[Article], now: datetime | None = None) -> None:
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        for a in articles:
            # Preserve the original first-seen time if already present.
            self._seen.setdefault(a.dedup_key, stamp)

    def prune(self, now: datetime | None = None, days: int | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        horizon = now - timedelta(days=days or settings.SEEN_PRUNE_DAYS)
        kept = {}
        for key, iso in self._seen.items():
            try:
                seen_at = datetime.fromisoformat(iso)
                if seen_at.tzinfo is None:
                    seen_at = seen_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue  # drop unparseable entries
            if seen_at >= horizon:
                kept[key] = iso
        self._seen = kept

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"seen": self._seen}, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning(f"could not write seen-store: {exc}")

    def __len__(self) -> int:
        return len(self._seen)
