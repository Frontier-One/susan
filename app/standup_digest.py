"""Daily update built from the offline standup channel AND the Granola standup meeting.

Since 2026-09-24 the team does a written standup in #team-tech-standups and a recorded
one (Granola) around 15:00; this digest replaces the standup notes. It is scheduled at
16:00 into #team-tech and stays HIGH LEVEL: major hits, major blockers and misses, what was
discussed, decisions — with a Granola link for the detail.

Skills (slash phrasing):
  /susan daily update [today|yesterday|<range>] [--no-approval]
  /susan daily status …   (older alias, same thing)
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from inspect import cleandoc
from typing import Any

import httpx

from app.claude_client import call_claude
from app.config import SUSAN_VOICE, logger
from app.granola_summarize import collect_granola_notes_matching_terms
from app.slack_api import fetch_slack_channel_history_since, notify_user_ephemeral, post_message
from app.weekly_context import parse_weekly_status_time_range, strip_weekly_status_auto_post_flags
from db import get_granola_token, user_has_granola_tokens

# Longer phrases first so "daily standup" is not consumed by "daily".
_DAILY_PREFIXES = (
    "daily standup digest",
    "daily update",
    "daily summary",
    "daily standup",
    "daily status",
    "daily digest",
    "standup digest",
    "standup status",
)


def parse_daily_standup_command(text: str) -> str | None:
    """If text is a daily-standup-digest command, return the remainder; else None."""
    raw = (text or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    for prefix in _DAILY_PREFIXES:
        if lower == prefix:
            return ""
        if lower.startswith(prefix + " "):
            return raw[len(prefix) :].strip()
    return None


def offline_standup_channel_id() -> str:
    """The channel where the written (offline) standup is posted each morning."""
    return (os.environ.get("SUSAN_OFFLINE_STANDUP_CHANNEL") or "C0C35UE399B").strip()


def granola_note_url(note: dict[str, Any]) -> str | None:
    """A link to the Granola note for the footer.

    Prefers a URL the API returned; otherwise builds one from the note id with
    SUSAN_GRANOLA_NOTE_URL_TEMPLATE (default: Granola's web app note path). Returns
    None when there is nothing to link, so the footer can say so instead of 404ing.
    """
    for key in ("url", "share_url", "public_url", "web_url"):
        v = note.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    nid = note.get("id")
    if not isinstance(nid, str) or not nid:
        return None
    template = (os.environ.get("SUSAN_GRANOLA_NOTE_URL_TEMPLATE") or "https://notes.granola.ai/d/{id}").strip()
    return template.replace("{id}", nid)


def _window_oldest_slack_ts(since_d: str) -> str:
    """Slack epoch string for 00:00 UTC on the window's first day."""
    d = datetime.strptime(since_d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return f"{int(d.timestamp())}.000000"


async def fetch_offline_standup_posts(since_d: str, user: str) -> str:
    """Everything posted in the offline standup channel in the window, threads included."""
    return await fetch_slack_channel_history_since(
        offline_standup_channel_id(),
        _window_oldest_slack_ts(since_d),
        user,
        include_thread_replies=True,
    )


def _offline_posts_present(blob: str) -> bool:
    b = (blob or "").strip()
    return bool(b) and not b.startswith("(No channel messages")


def standup_meeting_terms() -> list[str]:
    """Title/summary terms that identify the standup meeting in Granola."""
    raw = (os.environ.get("SUSAN_STANDUP_MEETING_TERMS") or "standup,stand-up,stand up").strip()
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    return terms or ["standup"]


def _max_standup_notes() -> int:
    n = int((os.environ.get("SUSAN_STANDUP_MAX_NOTES") or "3").strip() or "3")
    return max(1, min(10, n))


def _max_transcript_chars() -> int:
    """Transcript budget per run. F1_MODEL_MAX_PROMPT_CHARS is the real ceiling."""
    n = int((os.environ.get("SUSAN_STANDUP_MAX_TRANSCRIPT_CHARS") or "120000").strip() or "120000")
    return max(5_000, min(300_000, n))


def _standup_max_tokens() -> int:
    n = int((os.environ.get("SUSAN_STANDUP_MAX_TOKENS") or "8192").strip() or "8192")
    return max(1024, min(32_000, n))


def _note_attendee_names(note: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for a in note.get("attendees") or []:
        if isinstance(a, dict):
            name = (a.get("name") or a.get("email") or "").strip()
            if name:
                out.append(name)
    return out


def _transcript_speaker_names(note: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for seg in note.get("transcript") or []:
        if not isinstance(seg, dict):
            continue
        sp = seg.get("speaker")
        if isinstance(sp, dict) and (sp.get("name") or "").strip():
            names.add(sp["name"].strip())
    return names


def _recording_user_name(note: dict[str, Any]) -> str:
    """Granola labels the recorder's own audio as `attribution: me` with no name.

    The recorder is the attendee who never appears as a named speaker.
    """
    named = _transcript_speaker_names(note)
    unmatched = [
        a
        for a in _note_attendee_names(note)
        if not any(a.split()[0].lower() in n.lower() for n in named if n)
    ]
    return unmatched[0] if len(unmatched) == 1 else "Meeting host"


def format_standup_transcript(note: dict[str, Any], max_chars: int) -> str:
    """Speaker-labelled transcript, consecutive turns merged, oldest first."""
    segments = note.get("transcript") or []
    if not isinstance(segments, list) or not segments:
        return ""
    me = _recording_user_name(note)
    turns: list[tuple[str, list[str]]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        sp = seg.get("speaker") if isinstance(seg.get("speaker"), dict) else {}
        if (sp or {}).get("attribution") == "me":
            speaker = me
        else:
            speaker = ((sp or {}).get("name") or "Unknown speaker").strip()
        if turns and turns[-1][0] == speaker:
            turns[-1][1].append(text)
        else:
            turns.append((speaker, [text]))

    lines: list[str] = []
    total = 0
    dropped = 0
    for speaker, parts in turns:
        line = f"{speaker}: {' '.join(parts)}"
        if total + len(line) > max_chars:
            dropped += 1
            continue
        lines.append(line)
        total += len(line)
    if dropped:
        lines.append(f"_…{dropped} further turn(s) omitted (transcript budget)._")
    return "\n".join(lines)


def standup_notes_for_prompt(notes: list[dict[str, Any]], max_chars: int) -> str:
    """Granola's own summary plus the full speaker-labelled transcript per meeting."""
    budget = max_chars // max(1, len(notes))
    blocks: list[str] = []
    for i, n in enumerate(notes, 1):
        title = n.get("title") or "(untitled)"
        created = n.get("created_at") or ""
        attendees = ", ".join(_note_attendee_names(n)) or "(none listed)"
        summary = n.get("summary_markdown") or n.get("summary_text") or ""
        block = (
            f"### Meeting {i}: {title}\n"
            f"- created: {created}\n"
            f"- attendees: {attendees}\n\n"
            f"#### Granola's own summary\n{summary}\n"
        )
        transcript = format_standup_transcript(n, max(1000, budget - len(block)))
        if transcript:
            block += (
                "\n#### Full transcript (authoritative — the summary above may omit things)\n"
                f"{transcript}\n"
            )
        blocks.append(block)
    return "\n---\n".join(blocks)


def parse_standup_digest_window(remainder: str) -> tuple[str, str, str]:
    """Return (since, until, label). Defaults to today, since this runs after standup."""
    r = (remainder or "").strip().lower()
    today = datetime.now(timezone.utc).date()
    if not r or r in ("today", "this morning"):
        return today.isoformat(), today.isoformat(), "today"
    if r == "yesterday":
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat(), "yesterday"
    return parse_weekly_status_time_range(r)


def _standup_system_prompt() -> str:
    return cleandoc(
        f"""
        You are Susan. {SUSAN_VOICE}
        Write the team's DAILY UPDATE for the #team-tech channel. It replaces the standup
        notes, so it is read by people who were not in the standup and by people who
        skim: stay HIGH LEVEL. Outcomes, not activity; the detail lives in Granola.

        You get two sources for the same day: the written standup posts from the offline
        standup channel (one post per person, sometimes with thread replies) and the
        Granola notes plus transcript of the spoken standup. Merge them. The same item
        appearing in both is ONE item. Where they disagree, the transcript is the later
        word.

        Slack mrkdwn only: *single asterisks* for bold, never **double**. No # headings.
        Refer to people by the names used in the sources.

        Sections, in this order. Omit a section that has nothing real in it — never a
        heading followed by "none".

        *Major hits*
        • Things that shipped, landed, were proven or unblocked today. Name the owner.
          Only what a team lead would repeat to the CEO; skip routine progress.

        *Major blockers and misses*
        • What is stuck and on whom or what; what was promised and did not happen.
          Name the owner and the dependency. Include anything waiting on a person outside
          the team, an access request, a review or a decision.

        *Discussed*
        • Substantive topics that were talked through without reaching a decision, one
          line each, with who raised it. Skip status chatter already covered above.

        *Decisions*
        • What was decided and by whom, one line each, including reversals of earlier
          plans. Not opinions still being weighed.

        Rules:
        - Target 6 to 14 bullets in total. Cut words, never content that belongs in
          these four sections; drop everything that does not.
        - Never invent an item for someone who did not report or speak.
        - Ignore greetings, scheduling chatter and audio problems.
        - If both sources are too thin for a real update, say so in one line.
        - No preamble, no sign-off, no "here is".
        """
    )


async def build_standup_digest(
    notes: list[dict[str, Any]],
    range_label: str,
    offline_posts: str = "",
) -> str:
    """Summarize the offline posts + Granola notes into the channel-facing daily update."""
    bundle = standup_notes_for_prompt(notes, max_chars=_max_transcript_chars()) if notes else "(no Granola standup note in this window)"
    attendees = sorted({a for n in notes for a in _note_attendee_names(n)})
    roster = ", ".join(attendees) if attendees else "(not listed)"
    posts = (offline_posts or "").strip() or "(no posts in the offline standup channel in this window)"
    user_prompt = (
        f"Window: {range_label}.\n"
        f"Attendees of the spoken standup: {roster}.\n\n"
        f"--- Source 1: offline standup channel posts (one per person, threads included) ---\n{posts}\n\n"
        f"--- Source 2: Granola standup notes ({len(notes)} meeting(s)) ---\n{bundle}"
    )
    summary = await call_claude(
        _standup_system_prompt(),
        user_prompt,
        max_tokens=_standup_max_tokens(),
        action="standup_digest",
    )
    return summary


def daily_update_footer(notes: list[dict[str, Any]]) -> str:
    """The detail pointers under the update: Granola note link(s) + the offline channel."""
    links: list[str] = []
    for n in notes:
        url = granola_note_url(n)
        title = (n.get("title") or "standup").strip()
        links.append(f"<{url}|{title}>" if url else title)
    granola = ", ".join(links) if links else "no Granola note found for this window"
    return f"\n\n_Details: Granola — {granola} · written standups — <#{offline_standup_channel_id()}>_"


def _no_notes_message(range_label: str, scanned: int, terms: list[str]) -> str:
    term_list = ", ".join(f"`{t}`" for t in terms)
    return (
        f"No standup meeting found in Granola for *{range_label}* "
        f"(scanned {scanned} note(s), matching {term_list}), and nothing was posted in "
        f"<#{offline_standup_channel_id()}> either — nothing to summarize.\n"
        "_If your standup is titled differently, set `SUSAN_STANDUP_MEETING_TERMS` "
        "on the server to a comma-separated list of title words._"
    )


async def process_standup_digest(
    command_text: str,
    channel: str,
    user: str,
    thread_ts: str | None,
    response_url: str | None,
    *,
    auto_publish: bool = False,
) -> None:
    """Build the standup digest from Granola and post it, or preview it privately."""
    remainder = parse_daily_standup_command(command_text)
    remainder, auto_flag = strip_weekly_status_auto_post_flags(
        remainder if remainder is not None else command_text
    )
    auto_publish = auto_publish or auto_flag
    since_d, until_d, range_label = parse_standup_digest_window(remainder)

    if not await user_has_granola_tokens(user):
        await notify_user_ephemeral(
            channel,
            user,
            "Granola isn't connected. Run `/susan connect granola` (or set a shared "
            "`GRANOLA_API_KEY` on the server) so Susan can read standup notes.",
            None,
            response_url,
        )
        return

    offline_posts = ""
    try:
        offline_posts = await fetch_offline_standup_posts(since_d, user)
    except Exception as e:
        logger.warning("Offline standup channel fetch failed: %s", e)
        offline_posts = ""
    has_posts = _offline_posts_present(offline_posts)

    terms = standup_meeting_terms()
    try:
        bearer = await get_granola_token(user)
        notes, scanned = await collect_granola_notes_matching_terms(
            bearer,
            since_d,
            until_d,
            terms,
            max_detail_fetch=_max_standup_notes(),
            include_transcript=True,
        )
    except httpx.HTTPStatusError as e:
        logger.warning("Standup digest Granola fetch failed: %s", e)
        await notify_user_ephemeral(
            channel,
            user,
            f"Granola API error ({e.response.status_code}) while loading standup notes.",
            None,
            response_url,
        )
        return
    except Exception as e:
        logger.exception("Standup digest Granola fetch failed")
        await notify_user_ephemeral(
            channel, user, f"Could not load Granola notes: {e}", None, response_url
        )
        return

    if not notes and not has_posts:
        await notify_user_ephemeral(
            channel, user, _no_notes_message(range_label, scanned, terms), None, response_url
        )
        return

    try:
        summary = await build_standup_digest(notes, range_label, offline_posts if has_posts else "")
    except Exception as e:
        logger.exception("Standup digest summarization failed")
        await notify_user_ephemeral(
            channel,
            user,
            f"Loaded {len(notes)} standup note(s), but summarization failed: {e}",
            None,
            response_url,
        )
        return

    body = (str(summary) or "").strip()
    if not body:
        await notify_user_ephemeral(
            channel,
            user,
            f"Standup notes for *{range_label}* had nothing substantive to report.",
            None,
            response_url,
        )
        return

    model_route = getattr(summary, "model_route", None)
    model_name = getattr(summary, "model_name", None)
    header = f"*Daily update — {range_label}*\n\n"
    body = body + daily_update_footer(notes)

    if auto_publish:
        try:
            await post_message(
                channel,
                header + body,
                thread_ts=thread_ts,
                model_route=model_route,
                model_name=model_name,
            )
            await notify_user_ephemeral(
                channel,
                user,
                "✓ Posted the *daily update* to the channel — everyone here can see it.",
                None,
                response_url,
            )
        except Exception as e:
            logger.exception("Standup digest post failed")
            await notify_user_ephemeral(
                channel, user, f"Could not post standup digest: {e}", None, response_url
            )
        return

    preview = (
        header
        + body
        + "\n\n_Only you can see this. Post it with "
        + "`/susan daily update --no-approval`, or schedule it with "
        + "`/susan schedule add daily update every weekday at 16:00 in #team-tech`._"
    )
    await notify_user_ephemeral(
        channel,
        user,
        preview,
        None,
        response_url,
        model_route=model_route,
        model_name=model_name,
    )
