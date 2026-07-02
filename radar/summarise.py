"""Summarisation.

Two-tier by design:
  * If an LLM is configured (RADAR_LLM_PROVIDER=google + GOOGLE_API_KEY) the top
    stories get a crisp one-to-two sentence summary, constrained by a strict
    JSON guardrail and validated before use.
  * Otherwise (and for every story past the LLM budget) a deterministic
    extractive summary is used - the lead sentences of the source text.

The pipeline therefore always produces a readable digest with zero external
dependencies, and *upgrades* transparently when a key is present. No summary is
ever fabricated: both tiers work only from the article's own text.
"""

from __future__ import annotations

import json
import re

from config import settings
from radar.logconf import log
from radar.models import Article

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MAX_EXTRACTIVE_CHARS = 320

# Lazily-initialised LLM handle: None = not yet tried, False = unavailable.
_llm_client: object | None = None

# Generation parameters chosen specifically to minimise hallucination:
#   temperature 0    -> greedy/deterministic decoding, no creative drift
#   max_output_tokens-> hard cap so a summary can't ramble into invention
#   response_mime_type application/json -> the API returns JSON, reinforcing the
#     prompt contract and the _extract_json guardrail below (belt and braces)
_GEN_PARAMS = {
    "temperature": settings.LLM_TEMPERATURE,
    "max_output_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
    "response_mime_type": "application/json",
}


# --------------------------------------------------------------------------- #
# Extractive fallback (always available)
# --------------------------------------------------------------------------- #
def extractive_summary(article: Article, max_sentences: int = 2) -> str:
    """First 1-2 sentences of the source text, length-capped. Never empty."""
    text = article.summary_raw.strip() or article.title.strip()
    if not text:
        return "(no content available)"
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    snippet = " ".join(sentences[:max_sentences]) if sentences else text
    if len(snippet) > _MAX_EXTRACTIVE_CHARS:
        snippet = snippet[:_MAX_EXTRACTIVE_CHARS].rsplit(" ", 1)[0].rstrip() + "..."
    return snippet


# --------------------------------------------------------------------------- #
# Optional LLM tier
# --------------------------------------------------------------------------- #
def _get_llm():
    """Return a callable(prompt)->str, or None if no LLM is available."""
    global _llm_client
    if _llm_client is not None:
        return _llm_client or None

    if settings.LLM_PROVIDER != "google" or not settings.GOOGLE_API_KEY:
        _llm_client = False
        return None
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore

        client = genai.Client(api_key=settings.GOOGLE_API_KEY)
        config = types.GenerateContentConfig(**_GEN_PARAMS)

        def _call(prompt: str) -> str:
            resp = client.models.generate_content(
                model=settings.GOOGLE_MODEL,
                contents=prompt,
                config=config,  # temperature=0, token cap, JSON output
            )
            return getattr(resp, "text", "") or ""

        _llm_client = _call
        log.info(
            f"LLM summariser active: google/{settings.GOOGLE_MODEL} "
            f"(temperature={_GEN_PARAMS['temperature']}, deterministic)"
        )
        return _call
    except Exception as exc:  # missing dep, bad key, import error
        log.warning(f"LLM unavailable, using extractive summaries: {exc}")
        _llm_client = False
        return None


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced JSON object out of a model response."""
    if not text:
        return None
    # Fast path: whole response is JSON.
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    # Fallback: locate the first {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


_PROMPT = """You are a healthcare-AI news editor. Summarise the item below in \
ONE or TWO plain sentences for a busy clinician-reader. Use only the facts given \
- do not add or invent anything. No hype, no marketing language.

Return ONLY strict JSON of the exact form:
{{"summary": "<your summary here>"}}

TITLE: {title}
SOURCE: {source}
TEXT: {text}
"""


def llm_summary(article: Article) -> str | None:
    """Return a validated LLM summary, or None to signal fallback."""
    call = _get_llm()
    if call is None:
        return None
    prompt = _PROMPT.format(
        title=article.title,
        source=article.source,
        text=(article.summary_raw or article.title)[:1500],
    )
    try:
        raw = call(prompt)
    except Exception as exc:
        log.warning(f"LLM call failed for {article.title[:60]!r}: {exc}")
        return None

    data = _extract_json(raw)
    if not data or not isinstance(data.get("summary"), str):
        log.warning("LLM returned no valid JSON summary; falling back")
        return None
    summary = data["summary"].strip()
    return summary or None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def summarise_all(articles: list[Article], llm_limit: int = 12) -> list[Article]:
    """Summarise every article, spending the LLM (if any) on the top ``llm_limit``.

    Assumes ``articles`` is already ranked best-first so the budget lands on the
    highest-value stories; the rest get the extractive summary.
    """
    llm_used = 0
    for idx, art in enumerate(articles):
        summary = None
        if idx < llm_limit:
            summary = llm_summary(art)
            if summary:
                llm_used += 1
        art.summary = summary or extractive_summary(art)
    if llm_used:
        log.info(f"summarise: {llm_used} LLM summaries, rest extractive")
    else:
        log.info("summarise: extractive summaries for all articles")
    return articles
