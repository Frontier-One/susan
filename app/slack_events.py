"""Slack Events API: thread replies on action-item digests update tracked status."""
from __future__ import annotations

import json

from db import get_digest_for_thread, get_incident_retro, list_active_action_items

from app.action_items import apply_status_reply_with_claude, format_status_ack
from app.config import logger
from app.retro import start_retro
from app.slack_api import (
    build_slack_message_permalink,
    fetch_slack_history,
    post_ephemeral,
    post_message,
)


def _carries_resolved_marker(text: str) -> bool:
    return ":large_green_circle:" in text and "*Resolved*" in text


async def _fetch_thread_history(channel: str, thread_ts: str, user: str) -> str | None:
    """Return the thread history, or None when it cannot be read."""
    try:
        return await fetch_slack_history(channel, thread_ts, user)
    except Exception:
        return None


def _history_carries_outage_marker(history: str) -> bool:
    """A resolved reply only starts a retro when the thread's root flagged an outage."""
    lines = history.splitlines()
    if not lines:
        return False
    return ":warning:" in lines[-1]


async def _maybe_start_retro(channel: str, user: str, text: str, thread_ts: str) -> None:
    if not _carries_resolved_marker(text):
        return
    history = await _fetch_thread_history(channel, thread_ts, user)
    if not history or not _history_carries_outage_marker(history):
        return
    existing = await get_incident_retro(channel, thread_ts)
    if existing:
        await post_message(
            channel,
            f"Retro already started: {existing['permalink']}",
            thread_ts=thread_ts,
        )
        return
    permalink = build_slack_message_permalink(channel, thread_ts)
    result = await start_retro(
        channel=channel,
        thread_root_ts=thread_ts,
        permalink=permalink,
        thread_text=history,
        slack_user_id=user,
    )
    if not result.startswith("Retro started"):
        logger.warning("Retro dispatch failed for %s: %s", permalink, result)


async def handle_slack_event_callback(payload: dict) -> None:
    event = payload.get("event") or {}
    if event.get("type") != "message":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    channel = (event.get("channel") or "").strip()
    user = (event.get("user") or "").strip()
    text = (event.get("text") or "").strip()
    thread_ts = (event.get("thread_ts") or "").strip()
    if not channel or not user or not text:
        return
    if not thread_ts:
        return

    # The retro path is independent of the action-item digest, so it runs before
    # the digest gate below (which returns early when there is no digest).
    await _maybe_start_retro(channel, user, text, thread_ts)

    digest = await get_digest_for_thread(channel, thread_ts)
    if not digest:
        return

    items = await list_active_action_items(channel)
    if not items:
        return

    try:
        updated = await apply_status_reply_with_claude(channel, user, text, items)
    except Exception as e:
        logger.exception("Action item status reply failed")
        try:
            await post_ephemeral(
                channel,
                user,
                f"Could not parse status update: {e}",
            )
        except Exception:
            pass
        return

    ack = format_status_ack(updated)
    if ack:
        try:
            await post_ephemeral(channel, user, ack)
        except Exception as e:
            logger.warning("Status ack ephemeral failed: %s", e)


def parse_events_body(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))
