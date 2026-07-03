"""Offline test suite for Healthcare AI Radar.

Every test runs without network access or a live LLM: HTTP is monkeypatched,
feeds are parsed from in-memory XML, and the LLM tier is stubbed. Grouped by
module so each stage of the pipeline is exercised in isolation and end-to-end.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

import main
from config import settings
from radar import (
    classify, dedup, email_render, emailer, pipeline, render, score, sources,
    summarise,
)
from radar.models import Article, Digest, normalise_title
from radar.state import SeenStore

UTC = timezone.utc


def mk(
    title="AI model helps clinical diagnosis",
    url="https://example.com/story",
    tier="major_outlet",
    summary="An AI model was built for patients in a clinical setting.",
    published=None,
    source="TestSource",
):
    return Article(
        title=title, url=url, source=source, credibility_tier=tier,
        summary_raw=summary, published=published,
    )


# --------------------------------------------------------------------------- #
class TestModels:
    def test_normalise_title_strips_punct_and_case(self):
        assert normalise_title("  AI, Cancer & Health!! ") == "ai cancer health"

    def test_normalise_title_empty(self):
        assert normalise_title("") == ""
        assert normalise_title(None) == ""

    def test_dedup_key_ignores_tracking_params(self):
        a = mk(url="https://x.com/story?utm_source=twitter")
        b = mk(url="https://x.com/story")
        assert a.dedup_key == b.dedup_key

    def test_dedup_key_falls_back_to_title_when_no_url(self):
        a = mk(url="", title="Same Headline")
        b = mk(url="", title="same headline")
        assert a.dedup_key == b.dedup_key

    def test_naive_datetime_coerced_to_utc(self):
        art = mk(published=datetime(2025, 1, 1, 12, 0, 0))
        assert art.published.tzinfo is not None

    def test_best_summary_never_empty(self):
        art = mk(summary="", title="")
        assert art.best_summary() == "(no content)"

    def test_to_dict_is_json_shaped(self):
        art = mk(published=datetime(2025, 6, 1, tzinfo=UTC))
        d = art.to_dict()
        assert d["title"] and d["published"].startswith("2025-06-01")
        assert set(d) >= {"title", "url", "source", "score", "category"}


# --------------------------------------------------------------------------- #
class TestClassify:
    def test_relevant_requires_ai_and_health(self):
        assert classify.is_relevant(mk(title="AI model", summary="clinical trial"))

    def test_irrelevant_ai_only(self):
        art = mk(title="New AI chip announced", summary="faster gaming gpu")
        assert not classify.is_relevant(art)

    def test_irrelevant_health_only(self):
        art = mk(title="Hospital opens new wing", summary="more beds for patients")
        assert not classify.is_relevant(art)

    def test_word_boundary_no_false_ai_match(self):
        # 'campaign' / 'detail' contain the letters 'ai' but must not match.
        art = mk(title="Health campaign detail", summary="patient care campaign")
        assert not classify.is_relevant(art)

    def test_classify_regulation(self):
        art = mk(title="FDA clears AI diagnostic device",
                 summary="510(k) clearance granted for the algorithm")
        classify.classify_article(art)
        assert art.category == "regulation"

    def test_classify_funding(self):
        art = mk(title="AI health startup raises $50M Series B",
                 summary="the funding round values the venture highly")
        classify.classify_article(art)
        assert art.category == "funding"

    def test_classify_drama(self):
        art = mk(title="AI health firm faces lawsuit",
                 summary="controversy and backlash after the scandal")
        classify.classify_article(art)
        assert art.category == "drama"

    def test_classify_general_when_no_keywords(self):
        art = mk(title="Thoughts on the field", summary="a calm reflection today")
        classify.classify_article(art)
        assert art.category == "general"

    def test_novelty_tags_attached(self):
        art = mk(title="World-first AI breakthrough in clinical care",
                 summary="a landmark result")
        classify.classify_article(art)
        assert "breakthrough" in art.tags
        assert any("first" in t for t in art.tags)  # 'first' or 'world-first'
        assert "landmark" in art.tags

    def test_filter_relevant_counts(self):
        arts = [
            mk(title="AI model", summary="clinical patient"),
            mk(title="Sports news", summary="football score"),
        ]
        assert len(classify.filter_relevant(arts)) == 1


# --------------------------------------------------------------------------- #
class TestScore:
    def test_credibility_component_normalised(self):
        official = mk(tier="official")
        blog = mk(tier="blog")
        score.compute_score(official)
        score.compute_score(blog)
        assert official.score_breakdown["credibility"] == 1.0
        assert blog.score_breakdown["credibility"] < 1.0

    def test_recency_fresh_beats_old(self):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        fresh = mk(published=now)
        old = mk(published=now - timedelta(days=30))
        score.compute_score(fresh, now)
        score.compute_score(old, now)
        assert fresh.score_breakdown["recency"] > old.score_breakdown["recency"]

    def test_recency_undated_is_neutral(self):
        art = mk(published=None)
        score.compute_score(art, datetime(2025, 7, 1, tzinfo=UTC))
        assert art.score_breakdown["recency"] == 0.5

    def test_recency_future_dated_capped(self):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        art = mk(published=now + timedelta(days=5))
        score.compute_score(art, now)
        assert art.score_breakdown["recency"] == 1.0

    def test_score_within_bounds(self):
        art = mk(tier="official", published=datetime.now(UTC))
        classify.classify_article(art)
        score.compute_score(art)
        assert 0.0 <= art.score <= 100.0

    def test_rank_orders_best_first(self):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        weak = mk(title="weak", tier="social", published=now - timedelta(days=13))
        strong = mk(title="strong", tier="official", published=now)
        ranked = score.rank([weak, strong], now)
        assert ranked[0].title == "strong"

    def test_rank_is_stable_and_reproducible(self):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        arts = [mk(title=f"t{i}", tier="blog", published=now) for i in range(5)]
        first = [a.title for a in score.rank(list(arts), now)]
        second = [a.title for a in score.rank(list(arts), now)]
        assert first == second


# --------------------------------------------------------------------------- #
class TestDedup:
    def test_exact_url_dedup(self):
        arts = [mk(url="https://x.com/a?utm_source=y"), mk(url="https://x.com/a")]
        assert len(dedup.deduplicate(arts)) == 1

    def test_cross_source_title_dedup_keeps_higher_credibility(self):
        low = mk(title="Same Story", url="https://low.com/1", tier="blog")
        high = mk(title="Same Story", url="https://high.com/2", tier="official")
        result = dedup.deduplicate([low, high])
        assert len(result) == 1 and result[0].credibility_tier == "official"

    def test_distinct_stories_survive(self):
        arts = [mk(title="Story A", url="https://x.com/a"),
                mk(title="Story B", url="https://x.com/b")]
        assert len(dedup.deduplicate(arts)) == 2


# --------------------------------------------------------------------------- #
class TestSummarise:
    def test_extractive_never_empty(self):
        assert summarise.extractive_summary(mk(summary="", title="")) != ""

    def test_extractive_takes_lead_sentences(self):
        art = mk(summary="First sentence here. Second one. Third one.")
        out = summarise.extractive_summary(art, max_sentences=2)
        assert out.startswith("First sentence here.") and "Third" not in out

    def test_extractive_truncates_long_text(self):
        art = mk(summary="word " * 200)
        out = summarise.extractive_summary(art)
        assert len(out) <= 324  # cap + ellipsis

    def test_extract_json_whole(self):
        assert summarise._extract_json('{"summary": "hi"}') == {"summary": "hi"}

    def test_extract_json_embedded(self):
        raw = 'Sure!\n```json\n{"summary": "hi"}\n```'
        assert summarise._extract_json(raw) == {"summary": "hi"}

    def test_extract_json_invalid(self):
        assert summarise._extract_json("not json at all") is None

    def test_llm_summary_none_without_client(self, monkeypatch):
        monkeypatch.setattr(summarise, "_get_llm", lambda: None)
        assert summarise.llm_summary(mk()) is None

    def test_llm_summary_valid_json(self, monkeypatch):
        monkeypatch.setattr(summarise, "_get_llm",
                            lambda: lambda prompt: '{"summary": "clean summary"}')
        assert summarise.llm_summary(mk()) == "clean summary"

    def test_llm_summary_bad_json_falls_back(self, monkeypatch):
        monkeypatch.setattr(summarise, "_get_llm",
                            lambda: lambda prompt: "no json here")
        assert summarise.llm_summary(mk()) is None

    def test_summarise_all_extractive_when_no_llm(self, monkeypatch):
        monkeypatch.setattr(summarise, "_get_llm", lambda: None)
        arts = [mk(summary="A sentence about clinical AI."), mk(summary="Another.")]
        summarise.summarise_all(arts, llm_limit=0)
        assert all(a.summary for a in arts)

    def test_generation_params_are_hallucination_safe(self):
        # temperature 0 (deterministic), a hard token cap, and JSON output.
        assert summarise._GEN_PARAMS["temperature"] == 0.0
        assert summarise._GEN_PARAMS["max_output_tokens"] == settings.LLM_MAX_OUTPUT_TOKENS
        assert summarise._GEN_PARAMS["max_output_tokens"] > 0
        assert summarise._GEN_PARAMS["response_mime_type"] == "application/json"

    def test_summarise_all_respects_llm_limit(self, monkeypatch):
        calls = {"n": 0}

        def fake_call(prompt):
            calls["n"] += 1
            return '{"summary": "x"}'

        monkeypatch.setattr(summarise, "_get_llm", lambda: fake_call)
        arts = [mk(title=f"t{i}") for i in range(5)]
        summarise.summarise_all(arts, llm_limit=2)
        assert calls["n"] == 2


# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, content=b"", payload=None):
        self.content = content
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
<item>
  <title>AI model detects cancer in a clinical trial</title>
  <link>https://example.com/a?utm_source=x</link>
  <description>Researchers built an AI model for patients.</description>
  <pubDate>Tue, 01 Jul 2025 10:00:00 GMT</pubDate>
</item>
<item>
  <title>Health AI startup raises funding</title>
  <link>https://example.com/b</link>
  <description>Series B for hospital AI.</description>
  <pubDate>Mon, 30 Jun 2025 10:00:00 GMT</pubDate>
</item>
</channel></rss>"""


class TestSources:
    def test_parse_date_struct_time_is_utc(self):
        # gmtime is UTC; parser must not shift by the local offset.
        st = time.gmtime(1_700_000_000)
        dt = sources._parse_date(st)
        assert dt == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=1_700_000_000)

    def test_parse_date_string(self):
        dt = sources._parse_date("2025-07-01T10:00:00Z")
        assert dt.year == 2025 and dt.tzinfo is not None

    def test_parse_date_garbage_returns_none(self):
        assert sources._parse_date("not a date") is None
        assert sources._parse_date(None) is None

    def test_strip_html(self):
        assert sources._strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_validate_sources_rejects_bad_tier(self, monkeypatch):
        monkeypatch.setattr(settings, "RSS_SOURCES",
                            [{"name": "X", "url": "http://x", "credibility_tier": "??"}])
        with pytest.raises(ValueError):
            sources.validate_sources()

    def test_fetch_rss_parses_items(self, monkeypatch):
        monkeypatch.setattr(sources, "_http_get",
                            lambda url, params=None: FakeResp(content=RSS_XML))
        src = {"name": "Feed", "url": "http://x", "credibility_tier": "major_outlet"}
        arts = sources.fetch_rss(src)
        assert len(arts) == 2
        assert arts[0].title.startswith("AI model detects cancer")
        assert arts[0].published is not None

    def test_fetch_rss_graceful_on_network_failure(self, monkeypatch):
        monkeypatch.setattr(sources, "_http_get", lambda url, params=None: None)
        src = {"name": "Feed", "url": "http://x", "credibility_tier": "major_outlet"}
        assert sources.fetch_rss(src) == []

    def test_http_get_retries_impersonation_on_403(self, monkeypatch):
        import requests as rq

        def fake_get(url, **kw):
            resp = rq.Response()
            resp.status_code = 403
            resp.url = url
            return resp  # .raise_for_status() will raise HTTPError(response=403)

        sentinel = FakeResp(content=b"<rss></rss>")
        monkeypatch.setattr(sources.requests, "get", fake_get)
        monkeypatch.setattr(sources, "_impersonated_get",
                            lambda url, params=None: sentinel)
        assert sources._http_get("http://blocked") is sentinel

    def test_http_get_returns_none_when_impersonation_also_fails(self, monkeypatch):
        import requests as rq

        def fake_get(url, **kw):
            resp = rq.Response()
            resp.status_code = 403
            resp.url = url
            return resp

        monkeypatch.setattr(sources.requests, "get", fake_get)
        monkeypatch.setattr(sources, "_impersonated_get", lambda url, params=None: None)
        assert sources._http_get("http://blocked") is None

    def test_impersonated_get_none_without_curl_cffi(self, monkeypatch):
        monkeypatch.setattr(sources, "_HAS_CURL_CFFI", False)
        assert sources._impersonated_get("http://blocked") is None

    def test_http_get_retries_transient_then_succeeds(self, monkeypatch):
        import requests as rq
        calls = {"n": 0}
        good = rq.Response()
        good.status_code = 200
        good._content = b"<rss></rss>"

        def flaky_get(url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise rq.exceptions.ReadTimeout("arxiv cold start")
            return good

        monkeypatch.setattr(sources.requests, "get", flaky_get)
        monkeypatch.setattr(sources.time, "sleep", lambda s: None)  # no real waiting
        assert sources._http_get("http://arxiv") is good
        assert calls["n"] == 2  # failed once, retried, succeeded

    def test_http_get_gives_up_after_retries_exhausted(self, monkeypatch):
        import requests as rq
        calls = {"n": 0}

        def always_timeout(url, **kw):
            calls["n"] += 1
            raise rq.exceptions.ReadTimeout("persistently slow")

        monkeypatch.setattr(sources.requests, "get", always_timeout)
        monkeypatch.setattr(sources.time, "sleep", lambda s: None)
        assert sources._http_get("http://arxiv") is None
        assert calls["n"] == settings.HTTP_RETRIES + 1  # all attempts used

    def test_fetch_pubmed_parses(self, monkeypatch):
        search = {"esearchresult": {"idlist": ["111", "222"]}}
        summ = {
            "result": {
                "uids": ["111", "222"],
                "111": {"title": "AI in clinical care", "fulljournalname": "Nature Med",
                        "sortpubdate": "2025/06/20 00:00"},
                "222": {"title": "ML triage tool", "source": "Lancet",
                        "sortpubdate": "2025/06/18 00:00"},
            }
        }
        seq = iter([FakeResp(payload=search), FakeResp(payload=summ)])
        monkeypatch.setattr(sources, "_http_get", lambda url, params=None: next(seq))
        src = {"name": "PubMed", "term": "ai", "credibility_tier": "peer_reviewed"}
        arts = sources.fetch_pubmed(src)
        assert len(arts) == 2
        assert arts[0].url.endswith("/111/")

    def test_fetch_pubmed_graceful_when_no_ids(self, monkeypatch):
        monkeypatch.setattr(sources, "_http_get",
                            lambda url, params=None: FakeResp(payload={"esearchresult": {"idlist": []}}))
        src = {"name": "PubMed", "term": "ai", "credibility_tier": "peer_reviewed"}
        assert sources.fetch_pubmed(src) == []

    def test_fetch_all_aggregates_and_reports_status(self, monkeypatch):
        monkeypatch.setattr(sources, "fetch_rss",
                            lambda s: [mk(title="AI clinical model", source=s["name"])])
        monkeypatch.setattr(sources, "fetch_arxiv", lambda s: [])
        monkeypatch.setattr(sources, "fetch_pubmed", lambda s: [])
        arts, ok, failed = sources.fetch_all()
        assert len(arts) == len(settings.RSS_SOURCES)
        assert set(ok) == {s["name"] for s in settings.RSS_SOURCES}
        assert failed  # arxiv + pubmed produced nothing


# --------------------------------------------------------------------------- #
class TestRender:
    def _digest(self):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        arts = [
            mk(title="FDA clears AI tool", tier="official", published=now,
               summary="510(k) clearance for the algorithm"),
            mk(title="AI startup raises funds", tier="trade_press", published=now,
               summary="Series B funding round"),
        ]
        for a in arts:
            classify.classify_article(a)
        ranked = score.rank(arts, now)
        summarise.summarise_all(ranked, llm_limit=0)
        return Digest(generated_at=now, articles=ranked,
                      sources_ok=["STAT News"], sources_failed=["PubMed"])

    def test_render_markdown_has_core_sections(self):
        md = render.render_markdown(self._digest())
        assert "# Healthcare AI Radar" in md
        assert "Top" in md and "By Category" in md
        assert "FDA clears AI tool" in md

    def test_render_empty_digest_is_valid(self):
        d = Digest(generated_at=datetime(2025, 7, 1, tzinfo=UTC))
        md = render.render_markdown(d)
        assert md.strip().startswith("# Healthcare AI Radar")

    def test_write_outputs_creates_files(self, tmp_path):
        paths = render.write_outputs(self._digest(), out_dir=tmp_path, top_n=5)
        assert paths["markdown"].exists()
        assert paths["latest"].exists()
        assert paths["json"].exists()
        assert paths["latest"].read_text(encoding="utf-8").startswith("# Healthcare")


# --------------------------------------------------------------------------- #
class TestPipelineAndCLI:
    def test_run_end_to_end_offline(self, monkeypatch):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        raw = [
            mk(title="FDA clears AI diagnostic", tier="official", published=now,
               summary="510(k) clearance for the clinical AI algorithm"),
            mk(title="Football results", tier="blog", published=now,
               summary="league scores"),  # irrelevant, must be filtered
            mk(title="AI startup raises Series B", tier="trade_press", published=now,
               summary="funding for hospital patient AI"),
        ]
        monkeypatch.setattr(sources, "fetch_all",
                            lambda: (raw, ["StubSource"], []))
        monkeypatch.setattr(summarise, "_get_llm", lambda: None)
        digest = pipeline.run(llm_limit=0, now=now)
        titles = [a.title for a in digest.articles]
        assert "Football results" not in titles       # relevance gate worked
        assert digest.articles[0].category == "regulation"  # FDA ranks/labels
        assert all(a.summary for a in digest.articles)      # zero empty outputs

    def test_cli_run_returns_zero(self, monkeypatch, tmp_path):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        art = mk(title="AI clinical model", tier="official", published=now)
        classify.classify_article(art)
        score.compute_score(art, now)
        art.summary = "summary"
        digest = Digest(generated_at=now, articles=[art], sources_ok=["S"])
        monkeypatch.setattr(pipeline, "run", lambda **kw: digest)
        monkeypatch.setattr(
            render, "write_outputs",
            lambda d, **kw: {"latest": tmp_path / "latest.md",
                             "markdown": tmp_path / "d.md",
                             "json": tmp_path / "d.json"},
        )
        assert main.main(["run", "--no-llm"]) == 0


# --------------------------------------------------------------------------- #
class TestSeenStore:
    def test_filter_new_excludes_seen(self, tmp_path):
        store = SeenStore(tmp_path / "seen.json")
        a, b = mk(url="https://x.com/a"), mk(url="https://x.com/b")
        store.record([a])
        new = store.filter_new([a, b])
        assert [n.url for n in new] == ["https://x.com/b"]

    def test_persistence_round_trip(self, tmp_path):
        path = tmp_path / "seen.json"
        a = mk(url="https://x.com/a")
        s1 = SeenStore(path)
        s1.record([a])
        s1.save()
        s2 = SeenStore(path).load()
        assert not s2.is_new(a)  # remembered across instances

    def test_corrupt_file_starts_fresh(self, tmp_path):
        path = tmp_path / "seen.json"
        path.write_text("{not valid json", encoding="utf-8")
        store = SeenStore(path).load()
        assert len(store) == 0

    def test_prune_drops_old_entries(self, tmp_path):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        store = SeenStore(tmp_path / "seen.json")
        store.record([mk(url="https://x.com/old")], now=now - timedelta(days=60))
        store.record([mk(url="https://x.com/new")], now=now)
        store.prune(now=now, days=30)
        assert len(store) == 1

    def test_record_preserves_first_seen(self, tmp_path):
        store = SeenStore(tmp_path / "seen.json")
        a = mk(url="https://x.com/a")
        t1 = datetime(2025, 6, 1, tzinfo=UTC)
        store.record([a], now=t1)
        store.record([a], now=datetime(2025, 6, 15, tzinfo=UTC))
        assert store._seen[a.dedup_key] == t1.isoformat()


# --------------------------------------------------------------------------- #
class TestEmailRender:
    def test_html_contains_stories_and_scores(self):
        now = datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
        a = mk(title="FDA clears AI tool", tier="official", published=now,
               summary="Cleared for market")
        classify.classify_article(a)
        score.compute_score(a, now)
        html = email_render.render_email_html([a], now)
        assert "Healthcare AI Radar" in html
        assert "FDA clears AI tool" in html
        assert "1 new story" in html

    def test_html_escapes_injection(self):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        a = mk(title="Evil <script>alert(1)</script>", summary="x")
        html = email_render.render_email_html([a], now)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_html_handles_empty(self):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        html = email_render.render_email_html([], now)
        assert "No new stories" in html


# --------------------------------------------------------------------------- #
class _FakeSMTP:
    """Context-manager stand-in for smtplib.SMTP."""
    instances: list = []

    def __init__(self, host, port, timeout=None, fail_on=None):
        self.host, self.port = host, port
        self.logged_in = False
        self.sent = False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        self.logged_in = True

    def send_message(self, msg):
        self.sent = True


class TestEmailer:
    def test_config_none_without_credentials(self, monkeypatch):
        monkeypatch.setattr(settings, "EMAIL_USERNAME", "")
        monkeypatch.setattr(settings, "EMAIL_PASSWORD", "")
        assert emailer.email_config_from_settings() is None

    def test_config_built_when_credentials_present(self, monkeypatch):
        monkeypatch.setattr(settings, "EMAIL_USERNAME", "me@gmail.com")
        monkeypatch.setattr(settings, "EMAIL_PASSWORD", "app-password")
        monkeypatch.setattr(settings, "EMAIL_TO", "me@gmail.com")
        cfg = emailer.email_config_from_settings()
        assert cfg is not None and cfg.recipient == "me@gmail.com"

    def test_send_email_success(self, monkeypatch):
        _FakeSMTP.instances.clear()
        monkeypatch.setattr(emailer.smtplib, "SMTP", _FakeSMTP)
        cfg = emailer.EmailConfig("h", 587, "u", "p", "from@x", "to@x")
        ok = emailer.send_email("subj", "<b>hi</b>", cfg)
        assert ok is True
        assert _FakeSMTP.instances[-1].logged_in and _FakeSMTP.instances[-1].sent

    def test_send_email_failure_returns_false(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection refused")
        monkeypatch.setattr(emailer.smtplib, "SMTP", boom)
        cfg = emailer.EmailConfig("h", 587, "u", "p", "from@x", "to@x")
        assert emailer.send_email("subj", "<b>hi</b>", cfg) is False


# --------------------------------------------------------------------------- #
class TestDeliver:
    def test_new_only_emails_new_and_records_state(self, monkeypatch, tmp_path):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        seen_path = tmp_path / "seen.json"
        monkeypatch.setattr(settings, "SEEN_FILE", seen_path)

        old = mk(url="https://x.com/old", title="Old AI story")
        new = mk(url="https://x.com/new", title="New AI story")
        pre = SeenStore(seen_path)  # pre-seed 'old' as already seen
        pre.record([old], now)
        pre.save()

        digest = Digest(generated_at=now, articles=[old, new])
        cfg = emailer.EmailConfig("h", 587, "u", "p", "f@x", "t@x")
        monkeypatch.setattr(emailer, "email_config_from_settings", lambda: cfg)
        sent_to = {}
        monkeypatch.setattr(
            emailer, "send_email",
            lambda subject, html, config: sent_to.update(subject=subject, html=html) or True,
        )

        main._deliver(digest, new_only=True, do_email=True, now=now)

        # Only the new story is emailed, and it is now recorded as seen.
        assert "New AI story" in sent_to["html"]
        assert "Old AI story" not in sent_to["html"]
        after = SeenStore(seen_path).load()
        assert not after.is_new(new)

    def test_no_email_when_nothing_new(self, monkeypatch, tmp_path):
        now = datetime(2025, 7, 1, tzinfo=UTC)
        seen_path = tmp_path / "seen.json"
        monkeypatch.setattr(settings, "SEEN_FILE", seen_path)
        a = mk(url="https://x.com/a")
        pre = SeenStore(seen_path)
        pre.record([a], now)
        pre.save()

        monkeypatch.setattr(emailer, "email_config_from_settings",
                            lambda: emailer.EmailConfig("h", 1, "u", "p", "f", "t"))
        calls = {"n": 0}
        monkeypatch.setattr(emailer, "send_email",
                            lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or True)
        digest = Digest(generated_at=now, articles=[a])
        main._deliver(digest, new_only=True, do_email=True, now=now)
        assert calls["n"] == 0  # nothing new -> no send
