"""Command-line entry point for Healthcare AI Radar.

Usage:
    python main.py run                 # fetch, rank and write today's digest
    python main.py run --top-n 15      # show more stories in the Top Scoops list
    python main.py run --no-llm        # force deterministic extractive summaries
"""

from __future__ import annotations

import argparse
import sys

from radar import pipeline, render
from radar.logconf import configure_logging, log


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
    return parser


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

    try:
        digest = pipeline.run(llm_limit=llm_limit)
    except Exception as exc:  # never dump a raw traceback at the user
        log.error(f"Run failed: {exc}")
        return 1

    paths = render.write_outputs(digest, top_n=top_n)
    log.info("Top stories this run:")
    for art in digest.top(min(top_n, 5)):
        log.info(f"  [{art.score:>3.0f}] {art.title[:80]} ({art.source})")
    log.info(f"Digest written: {paths['latest']}")
    log.info(f"Full markdown:  {paths['markdown']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
