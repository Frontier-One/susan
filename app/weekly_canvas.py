"""Publish weekly status to a Slack Canvas and announce with a short channel link."""
from __future__ import annotations

import os
import re

from app.config import logger
from app.slack_api import (
    post_message,
    post_pr_summary_to_channel,
    slack_api_canvases_create,
    slack_api_files_permalink,
)

_SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_SLACK_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")
_SLACK_BOLD_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
# A line of only dashes/asterisks/underscores/equals renders as a full-width rule in Canvas,
# and directly under a text line it turns that line into a setext heading.
_RULE_LINE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,}|={3,}|\u2014{3,})[ \t]*$", re.MULTILINE)
_EXTRA_BLANK_LINES_RE = re.compile(r"\n{3,}")


def weekly_status_use_canvas() -> bool:
    raw = (os.environ.get("WEEKLY_STATUS_USE_CANVAS") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


# ── line shapes the weekly update emits, so the Canvas can get real structure ──────────
# Slack mrkdwn has no headings and no list syntax, so the model writes a section as a
# bold line (`*Theme* — people`) and a bullet as a literal `•`. Pasted into a Canvas
# unchanged that is a wall of bold paragraphs and bullet CHARACTERS — no headings, no
# lists, nothing collapsible, nothing scannable. These turn the known shapes into the
# markdown a Canvas actually renders.
#
# `*Theme* — <@U…> @login` -> `## Theme — …`  (bold run at the START of a line, with more
# after it). Anchored at ^ so a mid-sentence emphasis is never mistaken for a heading.
_SECTION_WITH_TAIL_RE = re.compile(r"^\*([^*\n]+)\*(\s*(?:[—–-]|\().*)$")
# `*Decisions*` — the whole line is one bold run.
_SECTION_BARE_RE = re.compile(r"^\*([^*\n]+)\*[ \t]*$")
# `• text`, `- text`, `· text`, optionally indented one level.
_BULLET_RE = re.compile(r"^(\s*)[•·‣▪][ \t]+(.*)$")
# The update's closing line: `Decisions this week: a; b; c`.
_DECISIONS_RE = re.compile(r"^(decisions(?:\s+this\s+week)?)\s*:\s*(.+)$", re.IGNORECASE)


def _canvas_structure(text: str) -> str:
    """Line-level Slack-shape → Canvas markdown: headings, lists, a decisions section.

    Runs BEFORE the inline conversions so a heading's own asterisks are consumed here
    and never re-emitted as bold. The first bold line is treated as a standfirst rather
    than a heading: the Canvas already carries an H1 title, and two headings stacked on
    top of each other read as a mistake.
    """
    lines = [raw.rstrip() for raw in (text or "").splitlines()]
    out: list[str] = []
    seen_content = False
    for idx, line in enumerate(lines):
        if not line.strip():
            out.append("")
            continue

        m = _BULLET_RE.match(line)
        if m:
            indent = "  " if m.group(1) else ""
            out.append(f"{indent}- {m.group(2).strip()}")
            seen_content = True
            continue

        m = _DECISIONS_RE.match(line.strip())
        if m:
            items = [i.strip(" .;") for i in re.split(r";\s*", m.group(2)) if i.strip(" .;")]
            if items:
                head = m.group(1).strip()
                out.extend(["", f"## {head[:1].upper()}{head[1:]}", ""])
                out.extend(f"- {i}" for i in items)
                seen_content = True
                continue

        m = _SECTION_BARE_RE.match(line) or _SECTION_WITH_TAIL_RE.match(line)
        if m:
            head = m.group(1).strip()
            tail = (m.group(2) if m.lastindex and m.lastindex >= 2 else "").rstrip()
            # A standfirst, not a second title — but ONLY when it looks like one: the
            # update opens with a bold title line followed by a BLANK line, then the
            # sections. A body that opens straight into a section (bold line, then its
            # bullets) keeps its heading, or the first section of every such update
            # would silently lose its structure.
            nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
            if not seen_content and not nxt.strip():
                out.append(f"_{head}{tail}_")
            else:
                out.extend(["", f"## {head}{tail}", ""])
            seen_content = True
            continue

        out.append(line)
        seen_content = True
    return "\n".join(out)


def slack_mrkdwn_to_canvas_markdown(text: str) -> str:
    """Slack mrkdwn → Canvas markdown: structure first, then inline links and bold."""
    s = (text or "").strip()
    if not s:
        return ""

    def link_sub(m: re.Match[str]) -> str:
        return f"[{m.group(2).strip()}]({m.group(1).strip()})"

    s = _canvas_structure(s)
    s = _SLACK_LINK_RE.sub(link_sub, s)
    s = _SLACK_BARE_LINK_RE.sub(lambda m: m.group(1), s)
    s = _SLACK_BOLD_RE.sub(r"**\1**", s)
    s = _RULE_LINE_RE.sub("", s)
    s = _EXTRA_BLANK_LINES_RE.sub("\n\n", s)
    return s.strip()


def _canvas_document_markdown(title: str, body: str) -> str:
    converted = slack_mrkdwn_to_canvas_markdown(body)
    title_line = (title or "Weekly status").strip()
    parts = [f"# {title_line}", "", converted, "", "---", "_Posted via Susan_"]
    return "\n".join(p for p in parts if p is not None)


async def publish_weekly_status(
    channel: str,
    thread_ts: str | None,
    title: str,
    body: str,
    *,
    model_route: str | None = None,
    model_name: str | None = None,
) -> None:
    """Post weekly status to Canvas when enabled; otherwise fall back to long channel messages."""
    if weekly_status_use_canvas():
        try:
            await _publish_weekly_status_to_canvas(
                channel,
                thread_ts,
                title,
                body,
                model_route=model_route,
                model_name=model_name,
            )
            return
        except Exception as e:
            logger.warning(
                "Weekly status canvas publish failed (%s); falling back to channel message",
                e,
            )
    await post_pr_summary_to_channel(
        channel, thread_ts, title, body, model_route=model_route, model_name=model_name
    )


async def _publish_weekly_status_to_canvas(
    channel: str,
    thread_ts: str | None,
    title: str,
    body: str,
    *,
    model_route: str | None = None,
    model_name: str | None = None,
) -> None:
    markdown = _canvas_document_markdown(title, body)
    canvas_id = await slack_api_canvases_create(
        title=(title or "Weekly status")[:150],
        markdown=markdown,
        channel_id=channel,
    )
    permalink = await slack_api_files_permalink(canvas_id)
    link_label = "Open weekly update in Canvas"
    announce = (
        f"*{title.strip()}*\n"
        f"<{permalink}|{link_label}> · _Posted via Susan_"
    )
    await post_message(
        channel,
        announce,
        thread_ts=thread_ts,
        unfurl_links=False,
        unfurl_media=False,
        model_route=model_route,
        model_name=model_name,
    )
