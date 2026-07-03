"""Render a set of articles into a self-contained HTML email body.

Uses only inline styles and a narrow container so it renders consistently across
email clients (Gmail, Outlook, phone apps). All article-supplied text is HTML-
escaped so a stray angle bracket in a headline can never break the layout.
"""

from __future__ import annotations

import html
from datetime import datetime

from radar.models import Article
from radar.render import CATEGORY_LABELS

_CAT_COLOR = {
    "regulation": "#b5651d",
    "clinical_trial": "#2e7d32",
    "research": "#1565c0",
    "funding": "#6a1b9a",
    "product_launch": "#00838f",
    "drama": "#c62828",
    "general": "#546e7a",
}


def _story_block(art: Article) -> str:
    label = CATEGORY_LABELS.get(art.category, art.category.title())
    color = _CAT_COLOR.get(art.category, "#546e7a")
    date = art.published.strftime("%Y-%m-%d") if art.published else "undated"
    title = html.escape(art.title or "(untitled)")
    url = html.escape(art.url, quote=True)
    source = html.escape(art.source)
    summary = html.escape(art.best_summary())
    tags = ""
    if art.tags:
        tags = (
            f'<span style="color:#8a6d3b;font-size:12px;">&nbsp;· '
            f'{html.escape(", ".join(art.tags))}</span>'
        )
    return f"""
    <div style="margin:0 0 20px 0;padding:0 0 16px 0;border-bottom:1px solid #eee;">
      <div style="font-size:12px;color:#888;margin-bottom:4px;">
        <span style="display:inline-block;background:#111;color:#fff;border-radius:4px;
              padding:1px 7px;font-weight:bold;">{art.score:.0f}</span>
        &nbsp;<span style="color:{color};font-weight:bold;">{label}</span>
        &nbsp;· {source} · {date}{tags}
      </div>
      <a href="{url}" style="font-size:16px;color:#1a0dab;text-decoration:none;
         font-weight:bold;">{title}</a>
      <div style="font-size:14px;color:#333;line-height:1.45;margin-top:4px;">
        {summary}
      </div>
    </div>"""


def render_email_html(
    articles: list[Article], generated_at: datetime, top_n: int = 20
) -> str:
    date_str = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    shown = articles[: max(0, top_n)]
    if shown:
        body = "".join(_story_block(a) for a in shown)
    else:
        body = ('<p style="color:#777;">No new stories since the last run.</p>')

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f4f4f4;">
  <div style="max-width:640px;margin:0 auto;padding:24px;background:#fff;
       font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <h1 style="font-size:20px;margin:0 0 2px 0;color:#111;">Healthcare AI Radar</h1>
    <div style="font-size:13px;color:#888;margin-bottom:20px;">
      {len(shown)} new stor{'y' if len(shown) == 1 else 'ies'} · {date_str}
    </div>
    {body}
    <div style="font-size:11px;color:#aaa;margin-top:8px;">
      Scores blend source credibility, recency, story type and novelty.
      Summaries are drawn only from source text.
    </div>
  </div>
</body></html>"""
