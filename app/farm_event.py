"""`POST /farm/event`: tell a person when a farm stops on them.

The issue farm (Brokkr) parks an issue on its author, parks a build after
repeated timeouts, or stops at capacity; the PR farm leaves a PR green except
for a human approval. Until now the only signal was a GitHub @mention, so an
author found out by asking (susan#32 was parked twice).

Susan is a relay here, nothing more. The farm POSTs what happened; Susan maps
the GitHub logins to Slack through the `people` table of the Forseti rota
(`policy/forseti-rotation.yaml` on dev-tools' default branch), DMs the author
when mapped, DMs the Forseti always, and posts one line to the alerts channel.
Susan holds no farm credential and never calls the farm back.

The body is a contract shared with dev-tools#152: do not rename fields.

Env:
  FARM_EVENT_TOKEN              bearer the farm sends (required; unset = every call is 401)
  SUSAN_FARM_EVENT_CHANNEL      channel for the one-line notice (default #team-tech-dev-alerts)
  SUSAN_FARM_EVENT_GITHUB_USER  Slack user whose GitHub connect reads the rota when
                                `GITHUB_TOKEN` is unset
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, Field

from app.slack_api import post_message, resolve_slack_post_channel

logger = logging.getLogger("susan")

ROTA_REPO = "Frontier-One/dev-tools"
ROTA_PATH = "policy/forseti-rotation.yaml"
# The rota changes by PR a few times a month; ten minutes keeps a burst of
# events off the GitHub API without serving a swap for long.
ROTA_TTL_SECONDS = 600.0
# A farm retries a POST it thinks failed. Same (kind, repo, number) inside this
# window is dropped so one park is one notification. In-process, so a second
# Susan replica would not share it: Susan runs as one process today.
DEDUPE_SECONDS = 600.0
SUMMARY_MAX_CHARS = 2000
# #team-tech-dev-alerts, the channel the rota's daily Forseti post goes to.
DEFAULT_CHANNEL = "C0BD2V0KPN2"

_KIND_LABELS = {
    "parked": "parked on its author",
    "timeout-parked": "parked after repeated timeouts",
    "needs-approval": "green except for a human approval",
    "capacity": "stopped at capacity",
}

_GITHUB_LOGIN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"

_rota_cache: tuple[float, dict[str, str]] | None = None
_seen: dict[tuple[str, str, int], float] = {}


class FarmEvent(BaseModel):
    kind: Literal["parked", "timeout-parked", "needs-approval", "capacity"]
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    number: int = Field(ge=1)
    title: str = Field(max_length=500)
    # Only a GitHub link: it goes into Slack's `<url|text>`, where `|` or `>` would
    # break out of the link and anything else would be a link the farm never meant.
    url: str = Field(pattern=r"^https://github\.com/[^\s<>|]+$")
    # Absent on a capacity stop, which has no author (dev-tools#152).
    author_github: str | None = Field(default=None, pattern=_GITHUB_LOGIN)
    forseti_github: str = Field(pattern=_GITHUB_LOGIN)
    summary: str = ""


def farm_event_token() -> str:
    return (os.environ.get("FARM_EVENT_TOKEN") or "").strip()


def farm_event_channel() -> str:
    return (os.environ.get("SUSAN_FARM_EVENT_CHANNEL") or DEFAULT_CHANNEL).strip()


def bearer_ok(authorization: str) -> bool:
    """Constant-time bearer check. No token configured fails closed."""
    expected = farm_event_token()
    scheme, _, presented = (authorization or "").partition(" ")
    if not expected or scheme.lower() != "bearer" or not presented.strip():
        return False
    return secrets.compare_digest(presented.strip().encode(), expected.encode())


def is_duplicate(event: FarmEvent, now: float | None = None) -> bool:
    """True when this (kind, repo, number) was accepted inside the window; records it otherwise."""
    now = time.monotonic() if now is None else now
    for k, ts in list(_seen.items()):
        if now - ts >= DEDUPE_SECONDS:
            del _seen[k]
    key = (event.kind, event.repo.lower(), event.number)
    if key in _seen:
        return True
    _seen[key] = now
    return False


async def _rota_token() -> str:
    from db import get_github_token

    return await get_github_token((os.environ.get("SUSAN_FARM_EVENT_GITHUB_USER") or "").strip())


async def _fetch_rota_text() -> str:
    """The rota from dev-tools' default branch (no `ref`: the merged rota, never a branch's)."""
    token = await _rota_token()
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"https://api.github.com/repos/{ROTA_REPO}/contents/{ROTA_PATH}", headers=hdrs)
    r.raise_for_status()
    return r.text


def parse_people(rota_text: str) -> dict[str, str]:
    """GitHub login (lower case) -> Slack member id, from the rota's `people` table."""
    data = yaml.safe_load(rota_text) or {}
    out: dict[str, str] = {}
    for person in (data.get("people") or {}).values():
        if not isinstance(person, dict):
            continue
        login = str(person.get("github") or "").strip().lower()
        slack_id = str(person.get("slack_id") or "").strip()
        if login and slack_id:
            out[login] = slack_id
    return out


async def github_to_slack() -> dict[str, str] | None:
    """Cached login map. A failed refresh serves the last good map; None when there never was one."""
    global _rota_cache
    now = time.monotonic()
    if _rota_cache and now - _rota_cache[0] < ROTA_TTL_SECONDS:
        return _rota_cache[1]
    try:
        people = parse_people(await _fetch_rota_text())
    except Exception:  # noqa: BLE001
        logger.exception("farm event: could not read %s:%s", ROTA_REPO, ROTA_PATH)
        return _rota_cache[1] if _rota_cache else None
    _rota_cache = (now, people)
    return people


def _esc(s: str) -> str:
    """Slack mrkdwn escaping: the farm's text must not become a mention or a link."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(event: FarmEvent) -> str:
    return f"<{event.url}|{event.repo}#{event.number}> {_esc(event.title)}"


def _quote(summary: str) -> str:
    text = _esc(summary.strip()[:SUMMARY_MAX_CHARS])
    return "\n".join(f"> {line}" for line in text.splitlines()) if text else ""


async def _dm(slack_id: str, text: str) -> None:
    channel = await resolve_slack_post_channel("", slack_id) or slack_id
    await post_message(channel, text, unfurl_links=False, slack_user_id=slack_id)


async def process_farm_event(event: FarmEvent) -> None:
    """Send the DMs and the channel line. Each send is independent: one failure does not stop the rest."""
    label = _KIND_LABELS[event.kind]
    people = await github_to_slack()
    author_id = (people or {}).get((event.author_github or "").lower())
    forseti_id = (people or {}).get(event.forseti_github.lower())
    quote = _quote(event.summary)

    if author_id and author_id != forseti_id:
        text = f"The farm stopped on your work: {_link(event)} is *{label}*."
        if event.kind == "parked":
            text += " It needs an answer from you before it can carry on."
        try:
            await _dm(author_id, text + (f"\n{quote}" if quote else ""))
        except Exception:  # noqa: BLE001
            logger.exception("farm event: author DM failed for %s", event.author_github)

    if people is None:
        author_note = "The Forseti rota could not be read, so nobody could be mapped to Slack."
    elif not event.author_github:
        author_note = "This event has no author."
    elif author_id and author_id == forseti_id:
        author_note = "You are also the author."
    elif author_id:
        author_note = f"The author <@{author_id}> was DMed too."
    else:
        author_note = (
            f"The author `{_esc(event.author_github)}` could not be mapped to Slack "
            "(not in the rota's `people` table), so you are the only one told."
        )

    if forseti_id:
        text = f"Forseti: {_link(event)} is *{label}*. {author_note}"
        try:
            await _dm(forseti_id, text + (f"\n{quote}" if quote else ""))
        except Exception:  # noqa: BLE001
            logger.exception("farm event: Forseti DM failed for %s", event.forseti_github)
    else:
        logger.warning("farm event: Forseti %s is not in the rota's people table", event.forseti_github)

    owner = f"<@{forseti_id}>" if forseti_id else f"`{_esc(event.forseti_github)}` (not mapped to Slack)"
    try:
        await post_message(farm_event_channel(), f"Farm: {_link(event)} is {label}. Forseti: {owner}.", unfurl_links=False)
    except Exception:  # noqa: BLE001
        logger.exception("farm event: channel line failed")
