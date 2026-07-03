"""Command-line entry point for Healthcare AI Radar.

Usage:
    python main.py run                       # fetch, rank and write today's digest
    python main.py run --top-n 15            # more stories in the Top Scoops list
    python main.py run --no-llm              # deterministic extractive summaries
    python main.py run --new-only --email    # email only stories new since last run
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from radar import email_render, emailer, pipeline, render
from radar.logconf import configure_logging, log
from radar.models import Article, Digest
from radar.state import SeenStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="healthcare-ai-radar",
        description="Track, rank and summarise healthcare-AI news into a digest.",
    )
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="run the pipeline and write a digest")
    run_p.add_argument("--top-n", type=int, default=10,
                       help="number of stories in the Top Scoops section")
    run_p.add_argument("--llm-limit", type=int, default=12,
                       help="max stories to summarise with the LLM (if configured)")
    run_p.add_argument("--no-llm", action="store_true",
                       help="force deterministic extractive summaries")
    run_p.add_argument("--new-only", action="store_true",
                       help="only consider stories not seen in a previous run")
    run_p.add_argument("--email", action="store_true",
                       help="email the digest (requires EMAIL_* env vars)")
    return parser


def _deliver(digest: Digest, new_only: bool, do_email: bool,
             now: datetime) -> None:
    """Apply the new-since-last-run filter and email delivery.

    The written digest file is always the full ranking; the *email* is what's
    new. State is only updated after a successful send, so a failed email is
    retried on the next run rather than silently swallowed.
    """
    store: SeenStore | None = None
    new_articles: list[Article] = digest.articles
    if new_only:
        store = SeenStore().load()
        new_articles = store.filter_new(digest.articles)

    if not do_email:
        return

    config = emailer.email_config_from_settings()
    if config is None:
        log.warning("--email set but EMAIL_* not configured; skipping email")
        return
    if not new_articles:
        log.info("no new stories to email; nothing sent")
        return

    subject = f"Healthcare AI Radar: {len(new_articles)} new " \
              f"stor{'y' if len(new_articles) == 1 else 'ies'} " \
              f"({now.strftime('%Y-%m-%d %H:%M UTC')})"
    html = email_render.render_email_html(new_articles, now)
    sent = emailer.send_email(subject, html, config)

    if sent and store is not None:
        store.record(new_articles, now)
        store.prune(now)
        store.save()
        log.info(f"seen-store updated ({len(store)} keys tracked)")


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Default to 'run' when invoked with no subcommand.
    if args.command not in ("run", None):
        parser.print_help()
        return 2

    top_n = getattr(args, "top_n", 10)
    llm_limit = 0 if getattr(args, "no_llm", False) else getattr(args, "llm_limit", 12)
    new_only = getattr(args, "new_only", False)
    do_email = getattr(args, "email", False)
    now = datetime.now(timezone.utc)

    try:
        digest = pipeline.run(llm_limit=llm_limit, now=now)
    except Exception as exc:  # never dump a raw traceback at the user
        log.error(f"Run failed: {exc}")
        return 1

    paths = render.write_outputs(digest, top_n=top_n)
    log.info("Top stories this run:")
    for art in digest.top(min(top_n, 5)):
        log.info(f"  [{art.score:>3.0f}] {art.title[:80]} ({art.source})")
    log.info(f"Digest written: {paths['latest']}")

    try:
        _deliver(digest, new_only=new_only, do_email=do_email, now=now)
    except Exception as exc:  # delivery must never crash a successful run
        log.error(f"Delivery step failed: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
