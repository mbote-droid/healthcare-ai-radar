"""Central configuration for Healthcare AI Radar.

All tunable behaviour lives here so the pipeline code stays free of magic
numbers and hardcoded paths. Values that a deployer might change are read from
the environment with safe defaults, so the tool runs out of the box with no
setup and degrades gracefully when optional services (an LLM, the network) are
unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths (never hardcode; everything hangs off the project root)
# --------------------------------------------------------------------------- #
BASE_DIR: Path = Path(__file__).resolve().parent.parent
OUTPUT_DIR: Path = BASE_DIR / "output"
LOG_DIR: Path = BASE_DIR / "logs"
# State is committed (unlike output/logs) so "new since last run" persists
# across ephemeral CI runners.
STATE_DIR: Path = BASE_DIR / "state"

for _d in (OUTPUT_DIR, LOG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Load a local .env (if present) BEFORE any os.getenv below, so the documented
# .env.example actually takes effect. Optional dependency: absent -> real env
# vars still work, we just don't parse a file.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover - convenience only
    pass


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to a default on garbage."""
    raw = os.getenv(name, "")
    try:
        value = int(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# HTTP / fetching behaviour
# --------------------------------------------------------------------------- #
HTTP_TIMEOUT: int = _env_int("RADAR_HTTP_TIMEOUT", 30)
# Transient failures (read timeouts, connection resets, 5xx) are retried with a
# linear backoff. arXiv in particular has slow, variable cold-start latency.
HTTP_RETRIES: int = _env_int("RADAR_HTTP_RETRIES", 2)
HTTP_BACKOFF: float = _env_float("RADAR_HTTP_BACKOFF", 1.5)
HTTP_HEADERS: dict[str, str] = {
    # A browser-like UA avoids naive 403 blocks on some publisher feeds while
    # still being a normal, honest request. Overridable via RADAR_USER_AGENT.
    "User-Agent": os.getenv(
        "RADAR_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}
MAX_ITEMS_PER_SOURCE: int = _env_int("RADAR_MAX_ITEMS_PER_SOURCE", 25)
LOOKBACK_DAYS: int = _env_int("RADAR_LOOKBACK_DAYS", 14)

# When a source answers with a Cloudflare-style block (403/429/503), the fetcher
# retries once using a browser TLS fingerprint (via curl_cffi, if installed).
# This is the profile it impersonates; any curl_cffi target works (chrome,
# chrome110, safari, edge, ...).
IMPERSONATE_PROFILE: str = os.getenv("RADAR_IMPERSONATE", "chrome").strip()

# NCBI asks that E-utilities callers identify themselves by email.
ENTREZ_EMAIL: str = os.getenv("ENTREZ_EMAIL", "").strip()

# --------------------------------------------------------------------------- #
# "New since last run" state + email delivery (used by the scheduled job)
# --------------------------------------------------------------------------- #
SEEN_FILE: Path = STATE_DIR / "seen.json"
# Forget stories older than this so the state file cannot grow without bound.
SEEN_PRUNE_DAYS: int = _env_int("RADAR_SEEN_PRUNE_DAYS", 30)

# Email (SMTP). Empty username/password => emailing is skipped gracefully.
EMAIL_HOST: str = os.getenv("EMAIL_HOST", "smtp.gmail.com").strip()
EMAIL_PORT: int = _env_int("EMAIL_PORT", 587)
EMAIL_USERNAME: str = os.getenv("EMAIL_USERNAME", "").strip()
# Gmail shows app passwords as 4 space-separated groups; the real secret has no
# spaces, so strip them to be forgiving of a copy-paste with spaces.
EMAIL_PASSWORD: str = os.getenv("EMAIL_PASSWORD", "").replace(" ", "")
EMAIL_FROM: str = os.getenv("EMAIL_FROM", "").strip() or EMAIL_USERNAME
EMAIL_TO: str = os.getenv("EMAIL_TO", "").strip() or EMAIL_USERNAME

# --------------------------------------------------------------------------- #
# Optional LLM summariser. Default "none" => deterministic extractive summary.
# Supported: "none", "google" (Google AI Studio via google-genai).
# --------------------------------------------------------------------------- #
LLM_PROVIDER: str = os.getenv("RADAR_LLM_PROVIDER", "none").strip().lower()
GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "").strip()
GOOGLE_MODEL: str = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash").strip()
LLM_MAX_OUTPUT_TOKENS: int = _env_int("RADAR_LLM_MAX_TOKENS", 512)
# temperature=0 => greedy, deterministic decoding: the primary control against
# the model drifting off the source text. Overridable but 0 is the safe default.
LLM_TEMPERATURE: float = _env_float("RADAR_LLM_TEMPERATURE", 0.0)

# --------------------------------------------------------------------------- #
# Credibility tiers (0-50). Mirrors editorial trust: official > peer-reviewed >
# major outlet > trade press > blog > social. Used by the scoring engine.
# --------------------------------------------------------------------------- #
CREDIBILITY = {
    "official": 50,      # regulators, government agencies
    "peer_reviewed": 40, # journals / preprint-with-review
    "top_journal": 35,   # Nature, Lancet, NEJM family
    "major_outlet": 30,  # Reuters-grade newswire / STAT
    "preprint": 22,      # arXiv, medRxiv (not yet reviewed)
    "trade_press": 18,   # industry trade publications
    "university": 20,    # institutional press releases
    "blog": 10,          # company / personal blogs
    "social": 2,         # anonymous social media
}

# Category weights feed the scoop score: regulatory approvals and clinical
# trials tend to be higher-signal for a healthcare-AI newsletter than generic
# opinion pieces.
CATEGORY_WEIGHTS = {
    "regulation": 1.30,
    "clinical_trial": 1.25,
    "research": 1.15,
    "funding": 1.10,
    "product_launch": 1.05,
    "drama": 1.00,
    "general": 0.90,
}

# Keyword lexicon for the rule-based classifier. Order matters only for ties;
# the classifier scores every category and picks the strongest match.
CATEGORY_KEYWORDS = {
    "regulation": [
        # NB: bare "recall" is intentionally excluded - it collides with the ML
        # precision/recall metric and mislabels research papers. FDA recalls are
        # still caught via the strong signals below plus "product recall".
        "fda", "ce mark", "approv", "clear", "regulat", "product recall",
        "guidance", "ema", "authoriz", "authoris", "de novo",
        "510(k)", "premarket",
    ],
    "clinical_trial": [
        "clinical trial", "phase i", "phase ii", "phase iii", "phase 1",
        "phase 2", "phase 3", "randomized", "randomised", "cohort",
        "enrolled", "endpoint", "efficacy", "prospective study",
    ],
    "funding": [
        "raise", "raised", "raises", "series a", "series b", "series c",
        "seed round", "funding", "investment", "valuation", "venture",
        "acquire", "acquisition", "ipo", "million", "billion",
    ],
    "product_launch": [
        "launch", "unveil", "release", "announce", "introduc", "rolls out",
        "now available", "partnership", "integrat", "deploy",
    ],
    "research": [
        "study", "researchers", "model", "algorithm", "dataset", "benchmark",
        "accuracy", "neural", "deep learning", "machine learning", "llm",
        "foundation model", "training", "arxiv", "preprint", "paper",
    ],
    "drama": [
        "lawsuit", "sued", "controvers", "backlash", "resign", "fired",
        "layoff", "scandal", "criticism", "concern", "warn", "fail",
        "shut down", "investigat",
    ],
}

# Signal terms that hint a story is genuinely novel / high-impact. Used as a
# small additive bonus in scoring, not a category.
NOVELTY_SIGNALS = [
    "first", "breakthrough", "world-first", "novel", "unprecedented",
    "landmark", "milestone", "exclusive", "revealed", "leaked",
]

# --------------------------------------------------------------------------- #
# Sources. Each entry is validated at load time (see radar.sources).
# credibility_tier must be a key of CREDIBILITY above.
# --------------------------------------------------------------------------- #
RSS_SOURCES = [
    {"name": "STAT News", "url": "https://www.statnews.com/feed/",
     "credibility_tier": "major_outlet"},
    {"name": "FDA Press Releases",
     "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
     "credibility_tier": "official"},
    {"name": "MobiHealthNews", "url": "https://www.mobihealthnews.com/feed",
     "credibility_tier": "trade_press"},
    {"name": "Fierce Healthcare",
     "url": "https://www.fiercehealthcare.com/rss/xml",
     "credibility_tier": "trade_press"},
    {"name": "Nature Medicine",
     "url": "https://www.nature.com/nm.rss",
     "credibility_tier": "top_journal"},
    {"name": "Healthcare IT News",
     "url": "https://www.healthcareitnews.com/home/feed",
     "credibility_tier": "trade_press"},
]

# arXiv Atom API. query is a raw search_query expression.
ARXIV_SOURCES = [
    {"name": "arXiv (AI in medicine)",
     "query": "cat:cs.AI AND (abs:clinical OR abs:medical OR abs:health OR abs:diagnosis)",
     "credibility_tier": "preprint"},
    {"name": "arXiv (ML in healthcare)",
     "query": "cat:cs.LG AND (abs:medical OR abs:healthcare OR abs:patient)",
     "credibility_tier": "preprint"},
]

# PubMed E-utilities. term is a raw PubMed query.
PUBMED_SOURCES = [
    {"name": "PubMed (AI clinical)",
     "term": "(artificial intelligence[Title/Abstract]) AND (clinical[Title/Abstract])",
     "credibility_tier": "peer_reviewed"},
]

# Relevance gate: an article must mention at least one of these to be kept.
# Keeps the digest on-topic (healthcare + AI), filtering generic feed noise.
RELEVANCE_TERMS_AI = [
    "ai", "a.i.", "artificial intelligence", "machine learning",
    "deep learning", "neural", "algorithm", "llm", "large language model",
    "foundation model", "generative", "chatbot", "automat",
]
RELEVANCE_TERMS_HEALTH = [
    "health", "clinic", "medic", "patient", "hospital", "diagnos",
    "disease", "drug", "therap", "care", "fda", "biotech", "pharma",
    "genomic", "oncolog", "radiolog", "patholog", "surg",
]

# Scoring weights (composite scoop score, 0-100 after normalisation).
SCORE_WEIGHTS = {
    "credibility": 0.35,
    "recency": 0.30,
    "category": 0.20,
    "novelty": 0.15,
}

# =========================================================================== #
# Publication Scout
# The author's areas of demonstrable expertise. The Scout scores each emergent
# literature theme by how well it overlaps these, so surfaced opportunities are
# ones the author is genuinely positioned to write about. Keyword matching is
# prefix/exact aware (see radar.classify._compile_terms).
# =========================================================================== #
EXPERTISE_AREAS = {
    "genomics_variants": {
        "label": "Genomics & variant interpretation",
        "keywords": ["tp53", "variant", "mutation", "genomic", "genome",
                     "germline", "somatic", "pathogenic", "clinvar", "hgvs",
                     "sequencing", "exome", "alphafold", "esm", "protein structure",
                     "oncogen", "tumor suppressor", "biomarker"],
    },
    "clinical_oncology": {
        "label": "Clinical oncology",
        "keywords": ["cancer", "oncolog", "tumor", "tumour", "carcinoma",
                     "chemotherap", "glioma", "prognos", "metasta", "malignan"],
    },
    "clinical_ai": {
        "label": "Clinical AI & decision support",
        "keywords": ["clinical decision", "diagnos", "triage", "screening",
                     "radiolog", "patholog", "ecg", "electronic health",
                     "clinical trial", "risk prediction", "sepsis"],
    },
    "llm_rag": {
        "label": "LLMs, RAG & agents",
        "keywords": ["llm", "large language model", "rag", "retrieval augmented",
                     "agent", "agentic", "foundation model", "prompt", "fine-tun",
                     "embedding", "multi-agent", "chatbot"],
    },
    "surgery": {
        "label": "Surgery",
        "keywords": ["surg", "operative", "perioperative", "laparoscop",
                     "resection", "theatre", "intraoperative"],
    },
    "global_health_africa": {
        "label": "Global health & Africa",
        "keywords": ["africa", "low resource", "lmic", "equity", "global health",
                     "kenya", "sub-saharan", "underserved", "disparit"],
    },
    "bioinformatics_methods": {
        "label": "Bioinformatics & ML methods",
        "keywords": ["pipeline", "benchmark", "dataset", "annotation", "alignment",
                     "deep learning", "machine learning", "neural network",
                     "validation", "reproducib", "open source"],
    },
}

# Output types the Scout can recommend, each with suggested venues + an effort
# rating (1 quick .. 3 substantial). Venues chosen to be ones employers and
# reviewers actually check.
OUTPUT_TYPES = {
    "narrative_review": {
        "label": "Narrative review",
        "effort": 3,
        "venues": ["medRxiv / bioRxiv preprint", "Cureus", "JMIR AI",
                   "PLOS Digital Health"],
        "when": "several peer-reviewed studies on one theme worth synthesising",
    },
    "commentary": {
        "label": "Commentary / perspective",
        "effort": 2,
        "venues": ["medRxiv preprint", "Cureus", "LinkedIn article"],
        "when": "an emerging or debated topic where an informed viewpoint adds value",
    },
    "letter_to_editor": {
        "label": "Letter to the editor",
        "effort": 1,
        "venues": ["the journal of the source paper", "Cureus"],
        "when": "a single high-impact paper worth a critical, timely response",
    },
    "explainer": {
        "label": "Explainer / thought piece",
        "effort": 1,
        "venues": ["Substack", "LinkedIn article", "personal blog"],
        "when": "a timely development worth translating for a broad audience",
    },
}

# Tokens too generic to help theme clustering (every item here is about AI in
# health, so those words carry no discriminating signal).
SCOUT_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with",
    "using", "use", "used", "based", "via", "from", "into", "study", "studies",
    "analysis", "results", "approach", "method", "methods", "new", "novel",
    "review", "paper", "research", "model", "models", "data", "system",
    "systems", "ai", "artificial", "intelligence", "health", "healthcare",
    "medical", "clinical", "patient", "patients", "care", "learning", "machine",
    "deep", "large", "language", "human", "human's", "toward", "towards",
    "evaluation", "assessment", "development", "application", "applications",
    "this", "that", "these", "those", "how", "what", "why", "can", "may",
    "are", "is", "was", "were", "be", "been", "being", "our", "their", "its",
    "clinically", "automatically", "characterization", "built", "extraction",
    "framework", "understanding", "enabled", "aware", "guided", "driven",
}

SCOUT_MIN_SIMILARITY: float = _env_float("RADAR_SCOUT_SIMILARITY", 0.12)
SCOUT_MIN_TOKENS: int = _env_int("RADAR_SCOUT_MIN_TOKENS", 3)
SCOUT_MAX_OPPORTUNITIES: int = _env_int("RADAR_SCOUT_MAX_OPPS", 8)

# Publishability score weights. 'effort' is inverted (quicker wins rank higher).
SCOUT_WEIGHTS = {
    "expertise": 0.40,   # overlap with the author's areas
    "momentum": 0.30,    # freshness + novelty + how many articles cluster
    "evidence": 0.20,    # credibility of the underlying sources
    "effort": 0.10,      # inverse effort of the recommended output
}
