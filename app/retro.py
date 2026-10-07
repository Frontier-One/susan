"""Incident postmortem orchestration: start a retro for an outage thread, once.

Automatic (a resolved reply on an outage thread) and manual (`/susan retro
<permalink>`) both converge here. Idempotency is keyed on (channel,
thread_root_ts), so a second trigger reports the existing retro instead of
posting a second kickoff or re-dispatching the workflow.
"""
from __future__ import annotations

from db import create_incident_retro, get_incident_retro

from app.github_actions import dispatch_incident_retro
from app.slack_api import (
    extract_slack_archives_link,
    fetch_slack_history,
    notify_user_ephemeral,
    post_message,
    resolve_thread_root_ts,
)

_RETRO_SECTIONS = (
    ("What happened", "incident commander: a one-paragraph summary of the outage"),
    ("Timeline", "responders: key events with timestamps"),
    ("Impact", "support and duty holders: affected users, services, and duration"),
    ("Root cause", "engineering lead: the underlying cause and contributing factors"),
    ("What went well", "all responders: what worked during the response"),
    ("What went wrong", "all responders: what hindered the response"),
    ("Actions", "owners: concrete follow-up tasks with owners and dates"),
    ("Follow-up", "incident commander: next steps, review date, and owner"),
)


def _build_retro_kickoff(thread_text: str) -> str:
    """The kickoff post: a title line and the eight postmortem sections."""
    lines = ["*Outage postmortem*", ""]
    lines.append("_Review the thread and reply in this thread with the points you own._")
    lines.append("")
    for title, ask in _RETRO_SECTIONS:
        lines.append(f"• *{title}* — {ask}")
    return "\n".join(lines)


async def start_retro(
    *,
    channel: str,
    thread_root_ts: str,
    permalink: str,
    thread_text: str,
    slack_user_id: str | None = None,
) -> str:
    """Start (or report an existing) retro for an outage thread.

    Posts the kickoff into the thread, dispatches the dev-tools workflow, and
    records the retro. Returns a short human status string.
    """
    existing = await get_incident_retro(channel, thread_root_ts)
    if existing:
        return f"Retro already started: {existing['permalink']}"

    kickoff = _build_retro_kickoff(thread_text)
    await post_message(channel, kickoff, thread_ts=thread_root_ts)

    result = await dispatch_incident_retro(
        channel=channel,
        thread_ts=thread_root_ts,
        permalink=permalink,
        text=thread_text,
        slack_user_id=slack_user_id,
    )
    # Only record the retro when the workflow dispatch succeeded, so a failed
    # dispatch leaves the thread retryable instead of answering "started".
    if result.startswith("Retro started"):
        await create_incident_retro(channel, thread_root_ts, permalink)
    return result


async def process_retro_command(
    permalink: str,
    channel: str,
    user: str,
    response_url: str | None = None,
) -> None:
    """Run a ``/susan retro <permalink>``: parse, fetch the thread, start the retro, notify."""
    link_ch, link_ts = extract_slack_archives_link(permalink)
    if not link_ch or not link_ts:
        await notify_user_ephemeral(
            channel,
            user,
            "I couldn't parse a valid Slack thread link from that. "
            "Paste a message permalink (⋯ → Copy link), e.g. `/susan retro https://…/archives/C…/p…`.",
            None,
            response_url,
        )
        return
    thread_text = await fetch_slack_history(link_ch, link_ts, user)
    # Resolve the pasted message (which may be a reply) to its thread root so
    # the idempotency key and post target are the same key the automatic path
    # uses, and two permalinks for one thread cannot start two retros.
    root_ts = await resolve_thread_root_ts(link_ch, link_ts)
    result = await start_retro(
        channel=link_ch,
        thread_root_ts=root_ts,
        permalink=permalink,
        thread_text=thread_text,
        slack_user_id=user,
    )
    await notify_user_ephemeral(channel, user, result, None, response_url)
