"""Publication Scout.

Turns the fetched literature into *publication opportunities* tailored to the
author. It clusters recent articles into emergent themes, scores each theme for
how publishable it is for this author (expertise overlap x momentum x evidence
x how quick the output is), and emits a structured brief: theme, recommended
output type, target venues, a suggested title, an outline and the supporting
sources. An optional, strictly-guarded LLM step can draft a grounded abstract.

Everything except the optional LLM step is deterministic, offline and works
from the articles' own text - no fabricated sources, ever.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import settings
from radar.classify import _compile_terms
from radar.logconf import log
from radar.models import Article
from radar import summarise

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]+")

# Precompile the expertise lexicons once (prefix/exact aware).
_AREA_RES = {
    key: _compile_terms(area["keywords"])
    for key, area in settings.EXPERTISE_AREAS.items()
}
_EVIDENCE_TIERS = {"peer_reviewed", "top_journal", "preprint"}


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class Cluster:
    """A group of articles sharing a theme, anchored by a seed article."""

    articles: list[Article]
    seed_tokens: set[str] = field(default_factory=set)

    def size(self) -> int:
        return len(self.articles)

    def token_counts(self) -> Counter:
        counts: Counter = Counter()
        for art in self.articles:
            counts.update(_article_tokens(art))
        return counts

    def theme_tokens(self, n: int = 4) -> list[str]:
        # Most frequent tokens, ties broken alphabetically for determinism.
        ranked = sorted(self.token_counts().items(), key=lambda kv: (-kv[1], kv[0]))
        return [tok for tok, _ in ranked[:n]]

    def theme_label(self) -> str:
        toks = self.theme_tokens()
        return " ".join(t.replace("-", " ") for t in toks).title() if toks else "General"

    def top_article(self) -> Article:
        return max(self.articles, key=lambda a: a.score)


@dataclass
class PublicationBrief:
    """A single, ranked publication opportunity."""

    theme: str
    output_type: str
    output_label: str
    suggested_title: str
    target_venues: list[str]
    rationale: str
    expertise_areas: list[str]
    articles: list[Article]
    score: float
    score_breakdown: dict
    effort: int
    outline: list[str]
    abstract: str = ""

    def to_dict(self) -> dict:
        return {
            "theme": self.theme,
            "output_type": self.output_type,
            "output_label": self.output_label,
            "suggested_title": self.suggested_title,
            "target_venues": self.target_venues,
            "rationale": self.rationale,
            "expertise_areas": self.expertise_areas,
            "effort": self.effort,
            "score": round(self.score, 2),
            "score_breakdown": self.score_breakdown,
            "outline": self.outline,
            "abstract": self.abstract,
            "sources": [
                {"title": a.title, "url": a.url, "source": a.source} for a in self.articles
            ],
        }


# --------------------------------------------------------------------------- #
# Block C - tokenisation & clustering
# --------------------------------------------------------------------------- #
def _tokenize(text: str) -> set[str]:
    """Significant tokens (len >= 3, not a stopword), lowercased."""
    tokens = set()
    for m in _TOKEN_RE.finditer((text or "").lower()):
        tok = m.group(0).strip("-")
        if len(tok) >= 3 and tok not in settings.SCOUT_STOPWORDS:
            tokens.add(tok)
    return tokens


def _article_tokens(article: Article) -> set[str]:
    """Cluster and label on TITLE tokens only. Titles are dense topic signals;
    including abstract text bloats the token set and dilutes the overlap
    between related papers, which fragments everything into singletons."""
    return _tokenize(article.title)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def cluster_articles(articles: list[Article], threshold: float | None = None) -> list[Cluster]:
    """Greedy seed-linkage clustering.

    Articles are assumed to arrive ranked best-first, so each new cluster is
    anchored by a high-value article. A later article joins the cluster whose
    *seed* it most resembles (>= threshold); otherwise it seeds a new cluster.
    Seed-linkage (vs growing the vocabulary) keeps themes coherent and avoids
    one mega-cluster swallowing everything.
    """
    thr = settings.SCOUT_MIN_SIMILARITY if threshold is None else threshold
    clusters: list[Cluster] = []
    for art in articles:
        toks = _article_tokens(art)
        if len(toks) < settings.SCOUT_MIN_TOKENS:
            continue
        best: Cluster | None = None
        best_sim = 0.0
        for cl in clusters:
            sim = _jaccard(toks, cl.seed_tokens)
            if sim > best_sim:
                best_sim = sim
                best = cl
        if best is not None and best_sim >= thr:
            best.articles.append(art)
        else:
            clusters.append(Cluster(articles=[art], seed_tokens=toks))
    return clusters


# --------------------------------------------------------------------------- #
# Block D - scoring & brief generation
# --------------------------------------------------------------------------- #
def _expertise_match(cluster: Cluster) -> tuple[float, list[str]]:
    """Fraction of the author's areas the theme touches, plus their labels."""
    text = " ".join(a.haystack for a in cluster.articles)
    hits: list[tuple[int, str]] = []
    for key, pattern in _AREA_RES.items():
        n = len(set(m.group(0).lower() for m in pattern.finditer(text)))
        if n:
            hits.append((n, settings.EXPERTISE_AREAS[key]["label"]))
    hits.sort(key=lambda kv: (-kv[0], kv[1]))
    labels = [label for _, label in hits]
    # Three matched areas => saturated; deep single-area matches still score.
    score = min(1.0, len(labels) / 3.0) if labels else 0.0
    return score, labels


def _momentum(cluster: Cluster) -> float:
    n = cluster.size()
    recency = [a.score_breakdown.get("recency", 0.5) for a in cluster.articles]
    recency_avg = sum(recency) / len(recency) if recency else 0.5
    novelty_frac = sum(1 for a in cluster.articles if a.tags) / n
    size_factor = min(1.0, n / 3.0)
    return round(0.5 * recency_avg + 0.2 * novelty_frac + 0.3 * size_factor, 4)


def _evidence(cluster: Cluster) -> float:
    max_cred = max(settings.CREDIBILITY.values())
    vals = [settings.CREDIBILITY.get(a.credibility_tier, 0) for a in cluster.articles]
    return (sum(vals) / len(vals) / max_cred) if vals and max_cred else 0.0


def _choose_output_type(cluster: Cluster) -> str:
    size = cluster.size()
    peer = sum(1 for a in cluster.articles if a.credibility_tier in _EVIDENCE_TIERS)
    categories = {a.category for a in cluster.articles}
    has_novelty = any(a.tags for a in cluster.articles)

    if size >= 3 and peer >= 2:
        return "narrative_review"
    if peer >= 1 and size == 1:
        return "letter_to_editor"
    if "drama" in categories:
        return "commentary"
    if has_novelty:
        return "commentary"
    return "explainer"


_OUTLINES = {
    "narrative_review": ["Background", "Current evidence", "Gaps and controversies",
                         "Implications for practice", "Future directions"],
    "commentary": ["The development", "Why it matters", "Caveats and risks",
                   "What to watch next"],
    "letter_to_editor": ["Summary of the study", "Strengths", "Concerns / open questions",
                         "Conclusion"],
    "explainer": ["What happened", "Why it's a big deal",
                  "What it means for clinicians", "Bottom line"],
}


def _suggest_title(theme: str, output_type: str, cluster: Cluster) -> str:
    if output_type == "narrative_review":
        return f"{theme}: a narrative review of recent evidence"
    if output_type == "letter_to_editor":
        head = cluster.top_article().title.rstrip(".")
        return f"Re: {head[:90]}"
    if output_type == "commentary":
        return f"What recent advances in {theme.lower()} mean for clinical practice"
    return f"{theme}, explained: a clinician's view"


def score_opportunity(cluster: Cluster) -> PublicationBrief:
    """Turn a cluster into a scored, fully-populated PublicationBrief."""
    exp_score, areas = _expertise_match(cluster)
    momentum = _momentum(cluster)
    evidence = _evidence(cluster)
    output_type = _choose_output_type(cluster)
    spec = settings.OUTPUT_TYPES[output_type]
    effort = spec["effort"]
    effort_component = 1.0 - (effort - 1) / 2.0  # 1->1.0, 2->0.5, 3->0.0

    components = {
        "expertise": exp_score,
        "momentum": momentum,
        "evidence": evidence,
        "effort": effort_component,
    }
    weighted = sum(settings.SCOUT_WEIGHTS[k] * v for k, v in components.items())
    score = round(weighted * 100, 2)

    # Theme label leads with the top matched expertise area (always clean and
    # professional), followed by the most distinctive title tokens for specifics.
    phrase = cluster.theme_label()
    theme = f"{areas[0]} — {phrase}" if areas else (phrase or "Healthcare AI")
    ordered = sorted(cluster.articles, key=lambda a: -a.score)
    area_txt = ", ".join(areas[:3]) if areas else "healthcare AI"
    rationale = (
        f"Fits your expertise in {area_txt}. "
        f"{cluster.size()} recent item(s) on this theme; "
        f"suggested as a {spec['label'].lower()} because it is "
        f"{spec['when']}."
    )

    return PublicationBrief(
        theme=theme,
        output_type=output_type,
        output_label=spec["label"],
        suggested_title=_suggest_title(theme, output_type, cluster),
        target_venues=list(spec["venues"]),
        rationale=rationale,
        expertise_areas=areas,
        articles=ordered,
        score=score,
        score_breakdown={k: round(v, 3) for k, v in components.items()},
        effort=effort,
        outline=list(_OUTLINES[output_type]),
    )


# --------------------------------------------------------------------------- #
# Block E - optional grounded LLM abstract
# --------------------------------------------------------------------------- #
_ABSTRACT_PROMPT = """You are helping a clinician-researcher plan a piece to \
publish. Using ONLY the source items below, draft a short, honest abstract \
(90-140 words) for a {output_label} titled provisionally "{title}". Do not \
invent findings, numbers or citations beyond what the sources state.

Return ONLY strict JSON of the exact form:
{{"title": "<a refined title>", "abstract": "<the abstract>"}}

THEME: {theme}
SOURCES:
{sources}
"""


def draft_abstract(brief: PublicationBrief) -> bool:
    """Fill brief.abstract (and refine the title) via the guarded LLM.

    Returns True if an abstract was produced. Fully optional: with no LLM
    configured it is a no-op and the brief keeps its deterministic title.
    """
    call = summarise._get_llm()
    if call is None:
        return False
    sources = "\n".join(
        f"- {a.title} ({a.source}): {a.summary_raw[:200]}" for a in brief.articles[:6]
    )
    prompt = _ABSTRACT_PROMPT.format(
        output_label=brief.output_label,
        title=brief.suggested_title,
        theme=brief.theme,
        sources=sources,
    )
    try:
        raw = call(prompt)
    except Exception as exc:
        log.warning(f"abstract draft failed for {brief.theme!r}: {exc}")
        return False

    data = summarise._extract_json(raw)
    if not data or not isinstance(data.get("abstract"), str):
        log.warning(f"abstract draft returned no valid JSON for {brief.theme!r}")
        return False
    brief.abstract = data["abstract"].strip()
    refined = data.get("title")
    if isinstance(refined, str) and refined.strip():
        brief.suggested_title = refined.strip()
    return bool(brief.abstract)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def scout(articles: list[Article], draft: bool = False,
          limit: int | None = None) -> list[PublicationBrief]:
    """Cluster, score and rank publication opportunities (best-first).

    ``draft=True`` LLM-drafts an abstract for each surfaced opportunity (if an
    LLM is configured). ``limit`` caps how many opportunities are returned.
    """
    limit = settings.SCOUT_MAX_OPPORTUNITIES if limit is None else limit
    clusters = cluster_articles(articles)
    briefs = [score_opportunity(c) for c in clusters]
    briefs.sort(key=lambda b: (-b.score, b.theme))
    briefs = briefs[: max(0, limit)]

    if draft:
        drafted = sum(1 for b in briefs if draft_abstract(b))
        log.info(f"scout: drafted {drafted}/{len(briefs)} abstracts via LLM")

    log.info(f"scout: {len(clusters)} themes -> {len(briefs)} opportunities")
    return briefs


# --------------------------------------------------------------------------- #
# Block F - rendering
# --------------------------------------------------------------------------- #
def render_report(briefs: list[PublicationBrief], generated_at: datetime) -> str:
    date_str = generated_at.strftime("%Y-%m-%d")
    lines: list[str] = [f"# Publication Radar - {date_str}", ""]
    if not briefs:
        lines += ["_No publication opportunities surfaced from the current "
                  "literature. Try again after the next fetch._", ""]
        return "\n".join(lines).rstrip() + "\n"

    lines += [f"_{len(briefs)} opportunities, ranked for your background._", ""]
    for i, b in enumerate(briefs, 1):
        lines.append(f"## {i}. {b.theme}  ·  `score {b.score:.0f}`")
        lines.append("")
        lines.append(f"- **Suggested output:** {b.output_label} "
                     f"(effort {b.effort}/3)")
        lines.append(f"- **Working title:** {b.suggested_title}")
        lines.append(f"- **Target venues:** {', '.join(b.target_venues)}")
        if b.expertise_areas:
            lines.append(f"- **Your angle:** {', '.join(b.expertise_areas)}")
        lines.append(f"- **Why:** {b.rationale}")
        if b.abstract:
            lines.append(f"- **Draft abstract:** {b.abstract}")
        lines.append("- **Outline:**")
        for section in b.outline:
            lines.append(f"  - {section}")
        lines.append("- **Sources:**")
        for a in b.articles:
            lines.append(f"  - [{a.title}]({a.url}) — {a.source}")
        lines.append("")

    lines.append("---")
    lines.append("_Opportunities are ranked by expertise fit, momentum, source "
                 "evidence and effort. Sources are real fetched items; write the "
                 "actual piece yourself - this maps the terrain, it does not "
                 "replace the scholarship._")
    return "\n".join(lines).rstrip() + "\n"


def write_report(briefs: list[PublicationBrief], generated_at: datetime,
                 out_dir: Path | None = None) -> dict[str, Path]:
    """Write the Markdown report and a JSON sidecar; return the paths."""
    out_dir = out_dir or settings.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "publication_opportunities.md"
    json_path = out_dir / "publication_opportunities.json"

    md_path.write_text(render_report(briefs, generated_at), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {"generated_at": generated_at.isoformat(),
             "count": len(briefs),
             "opportunities": [b.to_dict() for b in briefs]},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log.info(f"wrote {md_path.name} and {json_path.name} to {out_dir}")
    return {"markdown": md_path, "json": json_path}
