# HOW IT WORKS - Healthcare AI Radar runtime flow

A numbered, plain-language trace of what actually happens when you run the tool,
from the command line back to a written digest. This describes the *pipeline*
(the cascade of events under the hood), not the *why* (see README.md).

Entry points: `python main.py run` (the news digest) and `python main.py scout`
(publication opportunities - see the Publication Scout section below).

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
24. **Scheduling**: a GitHub Actions workflow can run steps 1-23 on a timer in
    the cloud with `--new-only --email`. Between runs the `seen.json` state is
    persisted via the Actions **cache** (restored at the start, saved at the end)
    - not committed back - so history stays clean and runs never race on a push.
    No always-on server; each run is a one-shot fetch-and-send on a timer.

---

## Publication Scout (`python main.py scout`)
The Scout reuses stages 1-6 (fetch -> filter -> dedup -> classify -> rank), then
mines the ranked literature for things *you* could publish:

25. **Cluster** (`radar.scout.cluster_articles`): articles are grouped into
    emergent themes by title-token overlap (Jaccard, seed-linkage anchored by the
    highest-ranked article). Title-only tokens keep themes coherent; domain-
    generic words are stopworded so clusters form around specifics.
26. **Score each theme** (`score_opportunity`) on four normalised signals:
    **expertise** (overlap with the author's configured areas), **momentum**
    (freshness + novelty + cluster size), **evidence** (source credibility) and
    **effort** (inverse of the recommended output's effort). Weighted 0-100.
27. **Choose an output type**: several peer-reviewed papers on one theme -> a
    narrative review; a single strong paper -> a letter to the editor; a
    controversy or novel development -> a commentary; otherwise an explainer.
28. **Build a brief**: theme label (led by the matched expertise area), a
    suggested title, target venues, an outline, the matched expertise areas, a
    rationale, and the supporting sources (real fetched items only).
29. **Optional grounded abstract** (`--draft`): if an LLM is configured, a short
    abstract is drafted from the sources under the same strict JSON guardrail and
    no-fabrication prompt used for summaries; absent an LLM it is skipped.
30. **Render** (`radar.scout.write_report`): opportunities are ranked best-first,
    capped, and written to `output/publication_opportunities.md` (+ a `.json`
    sidecar). The report states plainly that it maps the terrain - you write the
    actual scholarship.

## Failure behaviour (graceful degradation, by design)
- No network -> every fetch returns empty, the run still completes and writes an
  (empty) digest rather than crashing.
- A single blocked/broken source -> logged as failed, others proceed.
- No LLM / no API key -> extractive summaries throughout.
- Malformed dates -> treated as undated (neutral recency), never an exception.
