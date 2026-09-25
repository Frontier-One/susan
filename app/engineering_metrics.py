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


_HOTFIX_RE = re.compile(r"^\s*(hotfix|fix)\s*(\(|:|/)", re.IGNORECASE)


def _is_hotfix(pr: dict) -> bool:
    return bool(_HOTFIX_RE.match(str(pr.get("title") or "")))


def _pr_key(pr: dict) -> str | None:
    """`owner/repo#number` from a search-API item, for joining signals back on."""
    num = pr.get("number")
    url = str(pr.get("repository_url") or pr.get("html_url") or "")
    if num is None or not url:
        return None
    if "/repos/" in url:
        repo = url.split("/repos/", 1)[1]
    else:
        parts = url.split("/")
        repo = "/".join(parts[3:5]) if len(parts) > 5 else ""
    return f"{repo}#{num}" if repo else None


def pr_key(pr: dict) -> str | None:
    """Public alias — the caller builds the signals map with the same key."""
    return _pr_key(pr)


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
    iso_week: int | None = None
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
    # ── AI-native signals (need per-PR review data) ──
    change_failure_pct: float | None = None
    review_latency_median_h: float | None = None      # open -> a human first touches it
    human_touch_median_h: float | None = None         # first human touch -> merge
    human_touches_per_change: float | None = None
    first_pass_agent_pct: float | None = None         # agent PRs merged with no P0/P1/P2 finding
    blocking_findings_per_change: float | None = None
    signals_covered: int = 0                          # PRs we could read review data for
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
            "change_failure_pct": self.change_failure_pct,
            "review_latency_median_h": self.review_latency_median_h,
            "human_touch_median_h": self.human_touch_median_h,
            "human_touches_per_change": self.human_touches_per_change,
            "first_pass_agent_pct": self.first_pass_agent_pct,
            "blocking_findings_per_change": self.blocking_findings_per_change,
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
    signals: dict[str, dict] | None = None,
) -> EngineeringMetrics:
    """Team-level figures for one window. No per-author anything, by design."""
    now = now or datetime.now(timezone.utc)
    merged = [p for p in merged if isinstance(p, dict)]
    opened = [p for p in opened if isinstance(p, dict)]
    m = EngineeringMetrics(window_days=window_days, repos=repos, stale_days=_stale_days(),
                           iso_week=now.isocalendar().week)
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

    # Change failure: a revert, or a fix merged within the window that follows a change
    # in it. Narrower than rework on purpose — rework counts any fix, this counts the
    # ones that plausibly follow one of THIS window's deliveries.
    if merged:
        m.change_failure_pct = _pct(sum(1 for p in merged if is_revert(p) or _is_hotfix(p)), len(merged))

    # ── human-in-the-loop, only where we could read the reviews ──
    if signals:
        lat, touch, touches, findings = [], [], [], []
        agent_total = agent_clean = 0
        covered = 0
        for p in merged:
            key = _pr_key(p)
            sig = signals.get(key) if key else None
            if sig is None:
                continue
            covered += 1
            c = _iso(p.get("created_at"))
            mg = _iso((p.get("pull_request") or {}).get("merged_at"))
            first = _iso(sig.get("first_human_touch"))
            touches.append(int(sig.get("human_touches") or 0))
            blocking = int(sig.get("blocking_findings") or 0)
            findings.append(blocking)
            if c and first and first >= c:
                lat.append((first - c).total_seconds() / 3600.0)
            if first and mg and mg >= first:
                touch.append((mg - first).total_seconds() / 3600.0)
            if is_agent_authored(p):
                agent_total += 1
                # First pass = the reviewers found nothing that GATES a merge. A
                # formal CHANGES_REQUESTED counts, and so does any P0/P1/P2 finding,
                # because that is what our reviewers actually emit. P3 does not gate
                # (D-F23) and so does not spoil a first pass.
                if not blocking and not int(sig.get("changes_requested") or 0):
                    agent_clean += 1
        m.signals_covered = covered
        if lat:
            m.review_latency_median_h = round(statistics.median(lat), 1)
        if touch:
            m.human_touch_median_h = round(statistics.median(touch), 1)
        if touches:
            m.human_touches_per_change = round(statistics.mean(touches), 1)
        if findings:
            m.blocking_findings_per_change = round(statistics.mean(findings), 1)
        if agent_total:
            m.first_pass_agent_pct = _pct(agent_clean, agent_total)
    return m


# ── rendering ─────────────────────────────────────────────────────────────────────────

def _fmt_delta(now_v, prev_v, *, unit: str, lower_is_better: bool) -> str:
    """`↑18%` / `↓1.1pp` plus ⚠️ when the move is the bad direction.

    `pp` for a metric that is itself a percentage — a rework rate going 5% -> 7% moved
    two POINTS, not forty percent, and reporting the relative change there is the
    classic way a dashboard makes a small drift look like a crisis (or hides a real one).
    """
    if now_v is None or prev_v is None:
        return ""
    now_f, prev_f = float(now_v), float(prev_v)
    if unit == "pp":
        diff = round(now_f - prev_f, 1)
        if diff == 0:
            return " flat"
        body = f"{abs(diff):g}pp"
    else:
        if prev_f == 0:
            return ""
        diff = now_f - prev_f
        if round(100.0 * diff / prev_f) == 0:
            return " flat"
        body = f"{abs(round(100.0 * diff / prev_f)):g}%"
    up = diff > 0
    arrow = "↑" if up else "↓"
    good = (not up) if lower_is_better else up
    return f" {arrow}{body}" + ("" if good else " ⚠️")


def _hours(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v < 1:
        return f"{round(v * 60)} min"
    return f"{v:g}h"


def render(m: EngineeringMetrics, prev: dict[str, Any] | None = None) -> str:
    """The Slack-mrkdwn block appended to the weekly update.

    Written here rather than by the model: every figure is arithmetic, and a model that
    can restate a number can restate it wrong. A metric we cannot compute says so by
    name — an omitted line reads as "nothing to report", which is the opposite of the
    truth for something that is simply not instrumented yet.
    """
    p = prev or {}
    # Each metric is its OWN bullet. A bare newline between `*Label:* value` lines is
    # not a line break in markdown, so in a Canvas the whole block ran together as one
    # paragraph (reported 2026-09-25). A bullet survives both renderers: Slack shows
    # the •, and the Canvas converter turns it into a real list item.
    L: list[str] = [f"*ENGINEERING — WEEK {m.iso_week}*" if m.iso_week else "*ENGINEERING*", ""]

    L.append(f"• *Shipped:* {m.merged} changes{_fmt_delta(m.merged, p.get('merged'), unit='pct', lower_is_better=False)}"
             + (f" across {m.repos} repos" if m.repos else ""))
    L.append(f"• *Intent → Prod:* {_hours(m.lead_time_median_h)} median"
             f"{_fmt_delta(m.lead_time_median_h, p.get('lead_time_median_h'), unit='pct', lower_is_better=True)}"
             f" · p90 {_hours(m.lead_time_p90_h)}")
    L.append(f"• *Change Failure:* {'n/a' if m.change_failure_pct is None else f'{m.change_failure_pct:g}%'}"
             f"{_fmt_delta(m.change_failure_pct, p.get('change_failure_pct'), unit='pp', lower_is_better=True)}")
    L.append(f"• *Rework:* {'n/a' if m.rework_pct is None else f'{m.rework_pct:g}%'}"
             f"{_fmt_delta(m.rework_pct, p.get('rework_pct'), unit='pp', lower_is_better=True)}")
    L.append(f"• *Agent-executed:* {'n/a' if m.agent_authored_pct is None else f'{m.agent_authored_pct:g}%'}"
             f"{_fmt_delta(m.agent_authored_pct, p.get('agent_authored_pct'), unit='pp', lower_is_better=False)}"
             " — context, not a score")
    L.append(f"• *Human Attention / Change:* {_hours(m.human_touch_median_h)} median"
             f"{_fmt_delta(m.human_touch_median_h, p.get('human_touch_median_h'), unit='pct', lower_is_better=True)}"
             f" · {'n/a' if m.human_touches_per_change is None else f'{m.human_touches_per_change:g}'} human touches each")
    L.append(f"• *Review latency:* {_hours(m.review_latency_median_h)} to first human touch"
             f"{_fmt_delta(m.review_latency_median_h, p.get('review_latency_median_h'), unit='pct', lower_is_better=True)}")
    L.append(f"• *First-pass Agent Success:* {'n/a' if m.first_pass_agent_pct is None else f'{m.first_pass_agent_pct:g}%'}"
             f"{_fmt_delta(m.first_pass_agent_pct, p.get('first_pass_agent_pct'), unit='pp', lower_is_better=False)}"
             " — agent PRs merged with no P0/P1/P2 finding")
    L.append(f"• *Blocking findings / change:* "
             f"{'n/a' if m.blocking_findings_per_change is None else f'{m.blocking_findings_per_change:g}'}"
             f"{_fmt_delta(m.blocking_findings_per_change, p.get('blocking_findings_per_change'), unit='pct', lower_is_better=True)}"
             " — P0–P2 only; P3 does not gate")
    L.append("• *AI Cost / Shipped Change:* _not instrumented_ — needs a per-run token ledger "
             "from the gateway; nothing in this estate records it yet")
    L.append(f"• *Flow:* {m.open_backlog} open, {m.stale_open} older than {m.stale_days}d")

    if m.signals_covered and m.merged and m.signals_covered < m.merged:
        m.notes.append(f"human-attention figures cover {m.signals_covered} of {m.merged} merged PRs "
                       "(the rest could not be read)")
    if m.notes:
        L.extend(["", "_" + "; ".join(m.notes) + "_"])
    L.extend(["", 
        "_Team-level only and deliberately not per person: delivery metrics applied to "
        "individuals reward split batches and inflated counts. Intent→Prod is PR open→merge, "
        "and Human Attention is ELAPSED time from a person's first touch to merge — both are "
        "proxies, and neither measures time actually spent._"])
    return "\n".join(L)


def observations_prompt(block: str) -> str:
    """What the MODEL is asked to add: interpretation, never arithmetic."""
    return (
        "### Engineering metrics — already computed, already formatted\n"
        f"{block}\n\n"
        "This block is appended to your update verbatim; do NOT reproduce it, reformat it, "
        "or restate any figure from it in your own sections. Your job is three short lines "
        "that go directly under it, each one sentence, in this order and with these labels:\n"
        "  *What changed:* the single movement that explains the others, naming the two "
        "figures that connect (e.g. throughput rose because review latency fell).\n"
        "  *Where we're losing time:* the biggest identifiable sink, grounded in the block "
        "or in the Slack/meeting sources — a specific stage, repo or failure class.\n"
        "  *Action:* one concrete thing to do next week, with an owner if the sources name one.\n"
        "Write nothing if the block has too little movement to explain — three honest lines "
        "or none, never filler. If a figure reads n/a, do not speculate about it."
    )
