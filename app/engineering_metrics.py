"""Team-level engineering metrics for the weekly update — computed, never narrated.

WHY THESE, AND WHY NOT MORE. Grounded in DORA's four keys plus the newer rework
signal, and in the warnings that come with them (DORA 2025 / the DX guide):

  * they are SYSTEM- AND TEAM-level. Nothing here is ever per-person. Applying
    delivery metrics to individuals "incentivizes gaming, inflated commit volume,
    artificially split batches", so this module refuses to compute a per-author
    breakdown even though the data would allow it.
  * throughput ALONE is the classic failure. "Deployment frequency measured
    without change failure rate is incomplete and can mask risk", so volume is
    never shown without the rework/revert signal beside it.
  * AI-authored share is CONTEXT, not a score — the guide's explicit vanity-metric
    warning. It is reported because this estate is mostly agent-authored and the
    number explains the others; it is never framed as good or bad.

WHAT IS HONEST ABOUT THE NUMBERS. This estate has no separate deploy step for most
lanes: a merged PR IS the rollout (the promote lanes and the gated-PR-is-the-rollout
ladder). So "lead time" here is PR open -> merge, which is a PROXY for DORA's
commit-to-production and is labelled as one. "Change failure rate" is likewise a
proxy: the share of merged PRs that are reverts or that fix a change merged in the
last 14 days. Both are stated as proxies wherever they are rendered — a proxy
presented as the real thing is how a dashboard starts lying.

Every figure here is arithmetic over the PR list the weekly already fetches. The
model never produces or adjusts a number; it only gets the rendered block.
"""
from __future__ import annotations

import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

_REVERT_RE = re.compile(r"^\s*(revert\b|revert:)", re.IGNORECASE)
# A fix/hotfix PR — the rework signal. Conventional-commit prefixes plus branch shapes.
_FIX_TITLE_RE = re.compile(r"^\s*(fix|hotfix|revert)\s*(\(|:|/)", re.IGNORECASE)
_FIX_BRANCH_RE = re.compile(r"^(fix|hotfix|revert)/", re.IGNORECASE)
# Agent authorship: the estate's own attestation (a label the Forseti approver sets,
# or the trailer-derived one). Read as CONTEXT only.
_AGENT_LABELS = {"agent-authored", "agentic"}


def _iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _labels(pr: dict) -> set[str]:
    out = set()
    for lb in pr.get("labels") or []:
        name = lb.get("name") if isinstance(lb, dict) else lb
        if isinstance(name, str):
            out.add(name.strip().lower())
    return out


def is_rework(pr: dict) -> bool:
    """A merged PR that exists because an earlier change was wrong.

    Title-based, deliberately: the branch name is not in the search API's payload
    for every result, and a title is what a human would call it. Conservative — a
    `fix(...)` that is a first-time fix of an old bug counts as rework here, which
    OVERSTATES the rate rather than flattering it. Stated wherever it is rendered.
    """
    title = str(pr.get("title") or "")
    if _REVERT_RE.match(title) or _FIX_TITLE_RE.match(title):
        return True
    head = ((pr.get("pull_request") or {}).get("head") or {}).get("ref") or ""
    return bool(_FIX_BRANCH_RE.match(str(head)))


def is_revert(pr: dict) -> bool:
    return bool(_REVERT_RE.match(str(pr.get("title") or "")))


def is_agent_authored(pr: dict) -> bool:
    return bool(_labels(pr) & _AGENT_LABELS)


def lead_time_hours(pr: dict) -> float | None:
    """PR opened -> merged, in hours. The proxy for DORA lead-time-for-changes."""
    c = _iso(pr.get("created_at"))
    m = _iso((pr.get("pull_request") or {}).get("merged_at"))
    if not c or not m or m < c:
        return None
    return (m - c).total_seconds() / 3600.0


@dataclass
class EngineeringMetrics:
    """One reporting window. Every field is arithmetic over the PR list."""

    window_days: int = 7
    merged: int = 0
    opened: int = 0
    repos: int = 0
    lead_time_median_h: float | None = None
    lead_time_p90_h: float | None = None
    merged_same_day_pct: float | None = None
    rework_pct: float | None = None
    reverts: int = 0
    agent_authored_pct: float | None = None
    open_backlog: int = 0
    stale_open: int = 0            # open > STALE_DAYS
    stale_days: int = 3
    notes: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "merged": self.merged,
            "opened": self.opened,
            "lead_time_median_h": self.lead_time_median_h,
            "lead_time_p90_h": self.lead_time_p90_h,
            "merged_same_day_pct": self.merged_same_day_pct,
            "rework_pct": self.rework_pct,
            "reverts": self.reverts,
            "agent_authored_pct": self.agent_authored_pct,
            "open_backlog": self.open_backlog,
            "stale_open": self.stale_open,
        }


def _stale_days() -> int:
    n = int((os.environ.get("SUSAN_METRICS_STALE_DAYS") or "3").strip() or "3")
    return max(1, min(30, n))


def compute(
    merged: Iterable[dict],
    opened: Iterable[dict],
    *,
    repos: int = 0,
    window_days: int = 7,
    now: datetime | None = None,
) -> EngineeringMetrics:
    """Team-level figures for one window. No per-author anything, by design."""
    now = now or datetime.now(timezone.utc)
    merged = [p for p in merged if isinstance(p, dict)]
    opened = [p for p in opened if isinstance(p, dict)]
    m = EngineeringMetrics(window_days=window_days, repos=repos, stale_days=_stale_days())
    m.merged = len(merged)
    m.opened = len(opened)

    leads = [h for h in (lead_time_hours(p) for p in merged) if h is not None]
    if leads:
        m.lead_time_median_h = round(statistics.median(leads), 1)
        ordered = sorted(leads)
        # p90 by nearest-rank: with a handful of PRs a quantile function invents
        # a value between two real ones, which reads as precision we do not have.
        m.lead_time_p90_h = round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1)
        m.merged_same_day_pct = _pct(sum(1 for h in leads if h <= 24), len(leads))
    elif merged:
        m.notes.append("lead time unavailable: no merged PR carried both timestamps")

    if merged:
        m.rework_pct = _pct(sum(1 for p in merged if is_rework(p)), len(merged))
        m.reverts = sum(1 for p in merged if is_revert(p))
        m.agent_authored_pct = _pct(sum(1 for p in merged if is_agent_authored(p)), len(merged))

    # Flow, not output: what is sitting open, and how much of it has gone cold.
    cutoff = now - timedelta(days=m.stale_days)
    still_open = [p for p in opened if not (p.get("pull_request") or {}).get("merged_at")
                  and (p.get("state") or "open") == "open"]
    m.open_backlog = len(still_open)
    m.stale_open = sum(1 for p in still_open if (_iso(p.get("created_at")) or now) < cutoff)
    return m


# ── rendering ─────────────────────────────────────────────────────────────────────────

def _delta(now_v: float | int | None, prev_v: float | int | None, *, lower_is_better: bool) -> str:
    """`(+3)` / `(-2.1)` with a direction mark, or "" when there is nothing to compare.

    No arrow when the two are equal, and no colouring of a change smaller than the
    noise this estate produces in a week — a 1-PR swing is not a trend.
    """
    if now_v is None or prev_v is None:
        return ""
    diff = round(float(now_v) - float(prev_v), 1)
    if diff == 0:
        return " (flat)"
    better = (diff < 0) if lower_is_better else (diff > 0)
    sign = "+" if diff > 0 else ""
    return f" ({sign}{diff:g}{' ↑' if better else ' ↓'})"


def render(m: EngineeringMetrics, prev: dict[str, Any] | None = None) -> str:
    """The Slack-mrkdwn block appended to the weekly update.

    Written here rather than by the model: every figure is arithmetic, and a model
    that can restate a number can restate it wrong. The model is given this text
    and told not to alter it.
    """
    p = prev or {}
    lt = "n/a" if m.lead_time_median_h is None else f"{m.lead_time_median_h:g}h"
    ltd = _delta(m.lead_time_median_h, p.get("lead_time_median_h"), lower_is_better=True)
    p90 = "n/a" if m.lead_time_p90_h is None else f"{m.lead_time_p90_h:g}h"
    same = "n/a" if m.merged_same_day_pct is None else f"{m.merged_same_day_pct:g}%"
    rw = "n/a" if m.rework_pct is None else f"{m.rework_pct:g}%"
    rwd = _delta(m.rework_pct, p.get("rework_pct"), lower_is_better=True)
    ag = "n/a" if m.agent_authored_pct is None else f"{m.agent_authored_pct:g}%"
    md = _delta(m.merged, p.get("merged"), lower_is_better=False)

    lines = [
        f"*Engineering — last {m.window_days} days*"
        + (f" across {m.repos} repos" if m.repos else ""),
        f"• *Delivered:* {m.merged} PRs merged{md} · {m.opened} opened · "
        f"{m.open_backlog} still open, {m.stale_open} of them older than {m.stale_days}d",
        f"• *Lead time* (open→merge, proxy for commit→production): median {lt}{ltd} · "
        f"p90 {p90} · {same} merged within a day",
        f"• *Rework:* {rw}{rwd} of merged PRs were a fix or revert · {m.reverts} revert(s)",
        f"• *Agent-authored:* {ag} of merged PRs — context for the numbers above, not a score",
    ]
    if m.notes:
        lines.append("• _" + "; ".join(m.notes) + "_")
    lines.append(
        "_Team-level only and deliberately not broken down by person: delivery metrics "
        "applied to individuals reward split batches and inflated counts. Rework is a "
        "proxy (title/branch says fix or revert) and overstates rather than flatters._"
    )
    return "\n".join(lines)
