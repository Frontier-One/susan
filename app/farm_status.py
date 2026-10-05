"""`/susan farm status` — report what both farms are doing.

Both farms expose the same `GET /status` shape (dev-tools PR farm and issue
farm). Susan is just a relay: it reads each farm's /status and formats it for
Slack. Susan holds no cluster credential; the farms' HTTP endpoints are all she
touches.

Env:
  FARM_BASE_URL         e.g. http://pr-farm-host:8787  (existing; PR farm)
  ISSUE_FARM_BASE_URL   e.g. http://issue-farm-host:8787 (issue farm)
  FARM_SERVE_TOKEN      optional bearer token both farms honour on GET /status
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("susan")

# Sentinels for how a farm's /status body is represented to the formatter.
# `None` means the farm is configured but did not answer (unreachable).
# NOT_CONFIGURED means the farm's base URL env var is not set at all; a user
# should be able to tell "not set up" apart from "down".
NOT_CONFIGURED = object()


def issue_farm_configured() -> bool:
    return bool(os.environ.get("ISSUE_FARM_BASE_URL"))


def _authorization_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    token = os.environ.get("FARM_SERVE_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_farm_status(base_url: str) -> dict | None:
    """GET a farm's /status. Returns the decoded body, or None on any failure.

    A network error, a non-200 or a non-JSON body all return None, which the
    formatter reports as unreachable. The endpoint is read-only and needs no
    argument beyond the base URL.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{base_url.rstrip('/')}/status", headers=_authorization_headers()
            )
        if r.status_code == 200:
            return r.json()
    except Exception:  # noqa: BLE001
        logger.exception("farm /status fetch failed for %s", base_url)
    return None


def _esc(s: str) -> str:
    """Slack mrkdwn escaping: a farm-supplied string stays plain text, not markup.

    A crafted farm name must not become a mention or a link in the report.
    Order matters: & first, then < and >, so an already-encoded value is not
    double-escaped out of shape.
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_number(x: object) -> bool:
    """True for a real number, not a bool (bool subclasses int and is rejected)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _work_item(item: object) -> str | None:
    """Render one in-flight item as `repo#n (123s)`, or None when malformed.

    A missing elapsed_s on an otherwise-valid dict is fine (elapsed is optional);
    a non-dict, a non-string name, or a non-numeric elapsed_s when present is a
    problem that the caller must surface rather than render as healthy.
    """
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not isinstance(name, str):
        return None
    elapsed = item.get("elapsed_s")
    if "elapsed_s" in item and not _is_number(elapsed):
        return None
    escaped = _esc(name)
    if _is_number(elapsed):
        return f"{escaped} ({elapsed:.0f}s)"
    return escaped


def _format_one(name: str, status: dict | None | object) -> str:
    """Render one farm's report: a one-line state, detail underneath.

    Missing or malformed fields degrade to an explicit problem rather than
    raising. An absent body (status is None) is reported unreachable by name;
    it never implies the other farm is down. A not-configured farm reports as
    such, apart from an unreachable one.
    """
    if status is NOT_CONFIGURED:
        return (
            f"*{name}*: not configured\n"
            "    an admin must set the farm's base URL env var on this server"
        )

    if status is None:
        return f"*{name}*: unreachable\n    could not read /status"

    if not isinstance(status, dict):
        return f"*{name}*: bad response\n    expected a JSON object"

    in_flight = status.get("in_flight")
    last_pass = status.get("last_pass")
    uptime_s = status.get("uptime_s")

    problems: list[str] = []
    if not isinstance(in_flight, list):
        problems.append("in_flight missing or not a list")
    else:
        for item in in_flight:
            if _work_item(item) is None:
                problems.append("in_flight contains a malformed item")
                break
    if not isinstance(last_pass, list):
        problems.append("last_pass missing or not a list")
    if not _is_number(uptime_s):
        problems.append("uptime_s missing or not a number")
    elif uptime_s <= 0:
        problems.append("uptime_s is not positive (farm freshly booted)")

    if problems:
        return f"*{name}*: problem\n    " + "\n    ".join(problems)

    if in_flight:
        doing = "working on: " + ", ".join(_work_item(i) for i in in_flight)
    else:
        doing = "working on: nothing in flight"

    return (
        f"*{name}*: up\n"
        f"    {doing}\n"
        f"    last pass: {len(last_pass)} decision(s)\n"
        f"    uptime: {uptime_s:.0f}s"
    )


def format_farm_status(farms: list[tuple[str, dict | None | object]]) -> str:
    """Render every farm's status as Slack text.

    farms is a list of (farm_name, status) where status is the decoded /status
    body, None when that farm did not answer, or NOT_CONFIGURED when the farm's
    base URL env var is unset. Each farm renders independently, so one
    unreachable or unconfigured farm never suppresses another.
    """
    return "\n\n".join(_format_one(name, status) for name, status in farms)
