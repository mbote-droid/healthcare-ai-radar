"""Source fetchers: RSS feeds, the arXiv Atom API and PubMed E-utilities.

Design rules:
  * Every fetcher is total: on any failure (network down, bad payload, a source
    changing its schema) it logs a warning and returns an empty list rather than
    raising. One dead source must never sink the whole run.
  * Fetchers only *retrieve and normalise*. Filtering, classification, scoring
    and summarising happen downstream, so these functions stay easy to test.
"""

from __future__ import annotations

import calendar
import time
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests
from dateutil import parser as date_parser

from config import settings
from radar.logconf import log
from radar.models import Article

# Optional: browser TLS impersonation for Cloudflare-protected feeds. If the
# package is absent the fetcher still works and simply degrades on such sources.
try:
    from curl_cffi import requests as _curl_requests  # type: ignore
    _HAS_CURL_CFFI = True
except Exception:  # pragma: no cover - import guard
    _curl_requests = None
    _HAS_CURL_CFFI = False

ARXIV_API = "http://export.arxiv.org/api/query"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Only 403 is a Cloudflare-style challenge that browser TLS impersonation can
# actually defeat. 429 (rate-limit) and 503 can't be helped by impersonation, so
# they go through normal backoff/graceful-fail instead of a doomed 30s retry.
_BLOCK_STATUSES = {403}
# Server-side / rate-limit statuses worth a plain backoff retry.
_RETRY_STATUSES = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def validate_sources() -> None:
    """Fail fast on a misconfigured source (unknown credibility tier / no id).

    Called once at startup so typos in ``config.settings`` surface immediately
    instead of silently mis-scoring articles.
    """
    for group, key in (
        (settings.RSS_SOURCES, "url"),
        (settings.ARXIV_SOURCES, "query"),
        (settings.PUBMED_SOURCES, "term"),
    ):
        for src in group:
            if not src.get("name") or not src.get(key):
                raise ValueError(f"Source missing name/{key}: {src!r}")
            tier = src.get("credibility_tier")
            if tier not in settings.CREDIBILITY:
                raise ValueError(
                    f"Source {src.get('name')!r} has unknown credibility_tier {tier!r}"
                )


def _impersonated_get(url: str, params: dict[str, Any] | None = None):
    """Retry a blocked request with a browser TLS fingerprint via curl_cffi.

    Returns a response object (API-compatible with requests: ``.content``,
    ``.json()``) or None if curl_cffi is unavailable or the retry also fails.
    """
    if not _HAS_CURL_CFFI:
        log.warning(
            f"{url} is bot-blocked and curl_cffi is not installed; skipping. "
            "Install curl_cffi to fetch Cloudflare-protected feeds."
        )
        return None
    try:
        resp = _curl_requests.get(
            url,
            params=params,
            timeout=settings.HTTP_TIMEOUT,
            impersonate=settings.IMPERSONATE_PROFILE,
        )
        resp.raise_for_status()
        log.info(f"{url}: fetched via TLS impersonation")
        return resp
    except Exception as exc:  # curl_cffi raises its own exception hierarchy
        log.warning(f"Impersonated GET failed for {url}: {exc}")
        return None


def _is_transient(exc: Exception, status: int | None) -> bool:
    """A failure worth retrying as-is (not a bot-block, not a hard 4xx)."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    return status in _RETRY_STATUSES


def _http_get(url: str, params: dict[str, Any] | None = None):
    """GET with timeout, retries and headers; returns None once all avenues fail.

    Failure handling, in order:
      * bot-block (403/429/503) -> retry once with browser TLS impersonation;
      * transient error (read timeout, connection reset, 5xx) -> retry with a
        linear backoff up to ``HTTP_RETRIES`` times;
      * anything else (hard 4xx, etc.) -> give up and return None.
    """
    attempts = max(1, settings.HTTP_RETRIES + 1)
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=settings.HTTP_HEADERS,
                timeout=settings.HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)

            if status in _BLOCK_STATUSES:
                log.info(f"{url} returned {status}; retrying with browser impersonation")
                alt = _impersonated_get(url, params)
                if alt is not None:
                    return alt
                log.warning(f"HTTP GET failed for {url}: {exc}")
                return None

            if _is_transient(exc, status) and attempt < attempts:
                backoff = settings.HTTP_BACKOFF * attempt
                log.info(
                    f"{url} transient failure ({exc}); "
                    f"retry {attempt}/{attempts - 1} in {backoff:.1f}s"
                )
                time.sleep(backoff)
                continue

            log.warning(f"HTTP GET failed for {url}: {exc}")
            return None
    return None


def _parse_date(value: Any) -> datetime | None:
    """Best-effort parse of the many date shapes feeds emit; None on failure."""
    if not value:
        return None
    # feedparser hands back a time.struct_time in *_parsed fields.
    if isinstance(value, time.struct_time):
        try:
            # feedparser emits *_parsed struct_times in UTC; timegm treats the
            # tuple as UTC (mktime would wrongly apply the local offset).
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
        except (OverflowError, ValueError, OSError):
            return None
    if isinstance(value, str):
        try:
            dt = date_parser.parse(value, fuzzy=True)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError, TypeError):
            return None
    return None


def _entry_summary(entry: Any) -> str:
    """Extract a plausible description/abstract from a feedparser entry."""
    for attr in ("summary", "description"):
        text = getattr(entry, attr, "") or ""
        if text:
            return _strip_html(text)
    return ""


def _strip_html(text: str) -> str:
    """Crude tag stripper - feeds often embed HTML in summaries. Good enough for
    keyword scanning and short digest snippets without pulling a parser dep."""
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


# --------------------------------------------------------------------------- #
# Fetchers
# --------------------------------------------------------------------------- #
def fetch_rss(source: dict[str, Any]) -> list[Article]:
    name = source.get("name", "rss")
    resp = _http_get(source["url"])
    if resp is None:
        return []
    try:
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # feedparser is defensive but never trust input
        log.warning(f"Failed to parse feed {name}: {exc}")
        return []

    articles: list[Article] = []
    for entry in parsed.entries[: settings.MAX_ITEMS_PER_SOURCE]:
        title = _strip_html(getattr(entry, "title", "") or "")
        link = getattr(entry, "link", "") or ""
        if not title or not link:
            continue
        published = _parse_date(
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
            or getattr(entry, "published", None)
        )
        articles.append(
            Article(
                title=title,
                url=link,
                source=name,
                credibility_tier=source["credibility_tier"],
                published=published,
                summary_raw=_entry_summary(entry),
            )
        )
    log.info(f"{name}: fetched {len(articles)} items")
    return articles


def fetch_arxiv(source: dict[str, Any]) -> list[Article]:
    name = source.get("name", "arxiv")
    params = {
        "search_query": source["query"],
        "start": 0,
        "max_results": settings.MAX_ITEMS_PER_SOURCE,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    resp = _http_get(ARXIV_API, params=params)
    if resp is None:
        return []
    try:
        parsed = feedparser.parse(resp.content)
    except Exception as exc:
        log.warning(f"Failed to parse arXiv feed {name}: {exc}")
        return []

    articles: list[Article] = []
    for entry in parsed.entries[: settings.MAX_ITEMS_PER_SOURCE]:
        title = " ".join((getattr(entry, "title", "") or "").split())
        link = getattr(entry, "link", "") or ""
        if not title or not link:
            continue
        published = _parse_date(
            getattr(entry, "published_parsed", None)
            or getattr(entry, "published", None)
        )
        articles.append(
            Article(
                title=title,
                url=link,
                source=name,
                credibility_tier=source["credibility_tier"],
                published=published,
                summary_raw=_strip_html(getattr(entry, "summary", "") or ""),
            )
        )
    log.info(f"{name}: fetched {len(articles)} items")
    return articles


def fetch_pubmed(source: dict[str, Any]) -> list[Article]:
    name = source.get("name", "pubmed")
    search_params = {
        "db": "pubmed",
        "term": source["term"],
        "retmax": settings.MAX_ITEMS_PER_SOURCE,
        "sort": "date",
        "retmode": "json",
    }
    if settings.ENTREZ_EMAIL:
        search_params["email"] = settings.ENTREZ_EMAIL

    search_resp = _http_get(f"{EUTILS}/esearch.fcgi", params=search_params)
    if search_resp is None:
        return []
    try:
        ids = search_resp.json().get("esearchresult", {}).get("idlist", [])
    except ValueError:
        log.warning(f"{name}: esearch returned non-JSON")
        return []
    if not ids:
        log.info(f"{name}: no PubMed ids returned")
        return []

    summary_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    if settings.ENTREZ_EMAIL:
        summary_params["email"] = settings.ENTREZ_EMAIL
    summary_resp = _http_get(f"{EUTILS}/esummary.fcgi", params=summary_params)
    if summary_resp is None:
        return []
    try:
        result = summary_resp.json().get("result", {})
    except ValueError:
        log.warning(f"{name}: esummary returned non-JSON")
        return []

    articles: list[Article] = []
    for uid in result.get("uids", []):
        rec = result.get(uid, {})
        title = (rec.get("title", "") or "").strip()
        if not title:
            continue
        journal = (rec.get("fulljournalname") or rec.get("source") or "").strip()
        published = _parse_date(rec.get("sortpubdate") or rec.get("pubdate"))
        articles.append(
            Article(
                title=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                source=name,
                credibility_tier=source["credibility_tier"],
                published=published,
                summary_raw=f"{journal}. {title}" if journal else title,
            )
        )
    log.info(f"{name}: fetched {len(articles)} items")
    return articles


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def fetch_all() -> tuple[list[Article], list[str], list[str]]:
    """Run every configured source. Returns (articles, ok_names, failed_names).

    A source that raises unexpectedly is recorded as failed but never aborts the
    run - graceful degradation is the whole point.

    The fetcher table is built here (not at import) and looks the functions up
    on the module, so both the source lists and the fetchers stay patchable in
    tests.
    """
    validate_sources()
    plan = (
        ("rss", settings.RSS_SOURCES, fetch_rss),
        ("arxiv", settings.ARXIV_SOURCES, fetch_arxiv),
        ("pubmed", settings.PUBMED_SOURCES, fetch_pubmed),
    )
    all_articles: list[Article] = []
    ok: list[str] = []
    failed: list[str] = []

    for kind, group, fetcher in plan:
        for src in group:
            name = src.get("name", kind)
            try:
                items = fetcher(src)
            except Exception as exc:  # last-resort guard around each source
                log.warning(f"Source {name} crashed: {exc}")
                failed.append(name)
                continue
            if items:
                all_articles.extend(items)
                ok.append(name)
            else:
                failed.append(name)

    log.info(
        f"fetch_all: {len(all_articles)} raw items from "
        f"{len(ok)} ok / {len(failed)} empty-or-failed sources"
    )
    return all_articles, ok, failed
