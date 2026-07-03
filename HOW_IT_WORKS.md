# HOW IT WORKS - Healthcare AI Radar runtime flow

A numbered, plain-language trace of what actually happens when you run the tool,
from the command line back to a written digest. This describes the *pipeline*
(the cascade of events under the hood), not the *why* (see README.md).

Entry point: `python main.py run`

---

## Stage 0 - Startup
1. `main.main()` parses the CLI (`run`, `--top-n`, `--llm-limit`, `--no-llm`).
2. `radar.logconf.configure_logging()` attaches a console sink and a rotating
   file sink (`logs/radar.log`). From here on, every event is logged - never
   printed.
3. `config.settings` is imported once. On import it creates `output/`,
   `output/cache/` and `logs/` if missing, and reads any environment overrides
   (timeouts, lookback window, optional LLM keys).

## Stage 1 - Fetch (radar.sources.fetch_all)
4. `validate_sources()` checks every configured source has a name, an endpoint
   and a known credibility tier. A typo here aborts the run immediately with a
   clear error, rather than silently mis-scoring later.
5. Each source group is fetched in turn:
   - **RSS feeds** (STAT, FDA, Nature Medicine, trade press): fetched with a
     timeout, parsed by `feedparser`, HTML stripped from titles/summaries.
   - **arXiv**: queried via the Atom API (newest first), parsed the same way.
   - **PubMed**: `esearch` returns article IDs, then `esummary` returns their
     metadata as JSON.
6. Every item becomes a normalised `Article` (title, url, source, credibility
   tier, published datetime coerced to UTC, raw summary).
7. Any source that errors (network down, 403, malformed payload) logs a warning
   and contributes nothing - the run continues. Returns `(articles, ok_sources,
   failed_sources)`.

## Stage 2 - Relevance gate (radar.classify.filter_relevant)
8. Each article's title+summary is scanned by two precompiled regexes: it is
   kept only if it mentions **both** an AI concept and a health/medical concept.
   Short tokens like `ai` match as whole words (so `campaign` is not a hit);
   longer stems like `clinic` match as prefixes (so `clinical` is a hit).

## Stage 3 - Recency gate (radar.pipeline._filter_recent)
9. Articles older than the lookback window (default 14 days) are dropped.
   Undated articles are kept and scored neutrally, so timestamp-less feeds still
   contribute.

## Stage 4 - De-duplication (radar.dedup.deduplicate)
10. Pass 1 collapses exact duplicates by canonical URL (tracking params like
    `?utm_source=` stripped first).
11. Pass 2 collapses the same headline carried by multiple outlets. When copies
    collide, the most credible one is kept (then whichever has a date, then the
    richer description).

## Stage 5 - Classification (radar.classify.classify_all)
12. Each article is scored against a keyword lexicon for six categories
    (Regulation, Clinical Trial, Research, Funding, Product Launch, Drama). The
    highest distinct-hit count wins; ties break toward the more likely default
    so one incidental word cannot mislabel an article.
13. Novelty cues ("first", "breakthrough", "landmark", ...) are attached as tags.

## Stage 6 - Scoring & ranking (radar.score.rank)
14. Every article gets a transparent 0-100 **Scoop Score** = a weighted blend of
    four normalised signals: source credibility, recency, category weight, and
    novelty. The per-signal breakdown is stored on the article.
15. Articles are sorted best-first (ties break on recency, then title, so the
    order is stable and reproducible).

## Stage 7 - Summarisation (radar.summarise.summarise_all)
16. For the top N articles, if an LLM is configured (`RADAR_LLM_PROVIDER=google`
    + `GOOGLE_API_KEY`), a one-to-two sentence summary is requested under a
    strict JSON guardrail and validated before use.
17. If no LLM is configured, the call fails, or the JSON is invalid, a
    deterministic **extractive** summary (the lead sentences of the source text)
    is used instead. Either way a non-empty, non-fabricated summary is produced.

## Stage 8 - Assemble & render (radar.render.write_outputs)
18. Ranked articles are packaged into a `Digest` (with the ok/failed source
    lists) and rendered to Markdown: a "Top Scoops" section, then sections
    grouped by category, then a footer noting source status and methodology.
19. Three files are written to `output/`: `digest_<date>.md`, `latest.md`
    (always the newest), and `digest_<date>.json` (machine-readable sidecar).
20. `main` logs the top few stories and the output paths, and exits 0.

---

## Stage 9 - Delivery (optional: `--new-only` / `--email`)
21. **New-only** (`radar.state.SeenStore`): the run loads a small committed
    `state/seen.json` of story keys already delivered and keeps only the ones not
    seen before. The written digest file stays the *full* ranking; only the email
    is filtered to what's new.
22. **Email** (`radar.emailer` + `radar.email_render`): if `EMAIL_*` is
    configured and there are new stories, an HTML email of the new items is sent
    over SMTP. Missing credentials or zero new stories -> no email, no error.
23. **State update**: only after a *successful* send are the new keys recorded
    (and old ones pruned by age) and the state saved. A failed email therefore
    retries those stories next run instead of losing them.
24. **Scheduling**: the GitHub Actions workflow runs steps 1-23 every 4 hours in
    the cloud with `--new-only --email`, then commits the updated `seen.json` so
    the next run knows what was already delivered. No always-on server; each run
    is a one-shot fetch-and-send on a timer.

## Failure behaviour (graceful degradation, by design)
- No network -> every fetch returns empty, the run still completes and writes an
  (empty) digest rather than crashing.
- A single blocked/broken source -> logged as failed, others proceed.
- No LLM / no API key -> extractive summaries throughout.
- Malformed dates -> treated as undated (neutral recency), never an exception.
