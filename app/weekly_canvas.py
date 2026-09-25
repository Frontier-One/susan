"""Publish weekly status to a Slack Canvas and announce with a short channel link."""
from __future__ import annotations

import os
import re

from app.config import logger
from app.slack_api import (
    post_message,
    slack_user_display_name,
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
# `• text`, `· text`, optionally indented — and a RUN of them. The model sometimes
# emits `• • text` (it copies the bullet from the prompt's example and adds its own),
# which used to render as a native list bullet FOLLOWED by a literal •.
_BULLET_RE = re.compile(r"^(\s*)(?:[•·‣▪][ \t]*)+(.*)$")
# `Next: …`, `Blocked / at risk: …` at the start of a list item. The update's own
# structure puts a label there, and the model drops the emphasis about half the time;
# bolding it here makes the shape consistent instead of depending on the model.
_ITEM_LABEL_RE = re.compile(r"^((?:\*\*)?)([A-Z][A-Za-z][A-Za-z /&'-]{0,28}?)(:)(?:\s|$)")
# Slack renders <@U123> as a mention; a Canvas does not, and shows the raw id.
_SLACK_MENTION_RE = re.compile(r"<@(U[A-Z0-9]{6,})(?:\|([^>]*))?>")
# The update's closing line: `Decisions this week: a; b; c`.
_DECISIONS_RE = re.compile(r"^(decisions(?:\s+this\s+week)?)\s*:\s*(.+)$", re.IGNORECASE)


def _bold_leading_label(item: str) -> str:
    """`Next: x` -> `**Next:** x`. Leaves an already-emphasised label alone."""
    if item.startswith("**") or item.startswith("*"):
        return item
    m = _ITEM_LABEL_RE.match(item)
    if not m:
        return item
    label = m.group(2)
    rest = item[m.end(3):].lstrip()
    return f"**{label}:** {rest}" if rest else f"**{label}:**"


def resolve_slack_mentions(text: str, names: dict[str, str] | None) -> str:
    """`<@U123>` -> `@Name`, because a Canvas shows the raw id otherwise.

    With no name for an id, the token is DROPPED rather than printed: the line
    already carries a readable handle beside it (`— <@U…> @sgorelik`), and a raw
    `U0ANAC8FBQ8` in a founder-facing page is worse than nothing. A trailing
    separator left behind by the drop is cleaned up.
    """
    names = {k.upper(): v for k, v in (names or {}).items()}

    def sub(m: re.Match[str]) -> str:
        label = names.get(m.group(1).upper()) or (m.group(2) or "").strip()
        return f"@{label.lstrip('@')}" if label else ""

    out = _SLACK_MENTION_RE.sub(sub, text)
    # "— , @x" / "—  @x" left by a dropped token
    out = re.sub(r"(—|-)\s*,\s*", r"\1 ", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"(—|-) +$", "", out, flags=re.MULTILINE).rstrip()


def slack_mention_ids(text: str) -> list[str]:
    """Every distinct U… id the body mentions, so the caller can resolve them once."""
    seen: list[str] = []
    for m in _SLACK_MENTION_RE.finditer(text or ""):
        uid = m.group(1).upper()
        if uid not in seen:
            seen.append(uid)
    return seen


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
            out.append(f"{indent}- {_bold_leading_label(m.group(2).strip())}")
            seen_content = True
            continue

        m = _DECISIONS_RE.match(line.strip())
        if m:
            body = m.group(2).strip()
            # Semicolons are the update's own separator; fall back to sentences so a
            # decisions paragraph still becomes a list instead of one long bullet.
            parts = re.split(r";\s*", body) if ";" in body else re.split(r"(?<=[.!?])\s+(?=[A-Z])", body)
            items = [i.strip(" .;") for i in parts if i.strip(" .;")]
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


def slack_mrkdwn_to_canvas_markdown(text: str, names: dict[str, str] | None = None) -> str:
    """Slack mrkdwn → Canvas markdown: mentions, then structure, then inline links/bold."""
    s = (text or "").strip()
    if not s:
        return ""
    s = resolve_slack_mentions(s, names)

    def link_sub(m: re.Match[str]) -> str:
        return f"[{m.group(2).strip()}]({m.group(1).strip()})"

    s = _canvas_structure(s)
    s = _SLACK_LINK_RE.sub(link_sub, s)
    s = _SLACK_BARE_LINK_RE.sub(lambda m: m.group(1), s)
    s = _SLACK_BOLD_RE.sub(r"**\1**", s)
    s = _RULE_LINE_RE.sub("", s)
    s = _EXTRA_BLANK_LINES_RE.sub("\n\n", s)
    return s.strip()


def _canvas_document_markdown(title: str, body: str, names: dict[str, str] | None = None) -> str:
    converted = slack_mrkdwn_to_canvas_markdown(body, names)
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
    names: dict[str, str] = {}
    for uid in slack_mention_ids(body):
        try:
            names[uid] = await slack_user_display_name(uid)
        except Exception as e:  # a name is cosmetic; never fail the publish for one
            logger.warning("canvas: could not resolve %s: %s", uid, e)
    markdown = _canvas_document_markdown(title, body, names)
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
