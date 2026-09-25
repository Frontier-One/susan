"""Team-level engineering metrics for the weekly update.

Grounded in DORA's four keys and the warnings that come with them: these are
system-level, throughput is never shown without the rework signal, and AI-authored
share is context rather than a score. The tests pin the arithmetic AND the refusals.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engineering_metrics import (
    EngineeringMetrics,
    compute,
    is_agent_authored,
    is_revert,
    is_rework,
    lead_time_hours,
    render,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def pr(title="feat: thing", hours=5.0, labels=(), merged=True, created=None, state="closed", head=None):
    c = created or (NOW - timedelta(hours=hours + 1))
    d = {
        "title": title,
        "created_at": c.isoformat().replace("+00:00", "Z"),
        "labels": [{"name": x} for x in labels],
        "state": state,
        "pull_request": {},
    }
    if merged:
        d["pull_request"]["merged_at"] = (c + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    if head:
        d["pull_request"]["head"] = {"ref": head}
    return d


def test_lead_time_is_open_to_merge_and_none_when_unmergeable() -> None:
    assert lead_time_hours(pr(hours=6)) == pytest.approx(6.0, abs=0.01)
    assert lead_time_hours(pr(merged=False)) is None
    # A merge stamped BEFORE the open is data corruption, not a negative lead time.
    bad = pr()
    bad["pull_request"]["merged_at"] = "2020-01-01T00:00:00Z"
    assert lead_time_hours(bad) is None


def test_rework_and_revert_detection() -> None:
    assert is_rework(pr(title="fix(auth): 401 on the scrape"))
    assert is_rework(pr(title="Revert \"feat: thing\""))
    assert is_rework(pr(title="feat: x", head="fix/1234-thing"))
    assert not is_rework(pr(title="feat(gateway): add glm edge"))
    # "fix" inside a sentence is not a fix PR.
    assert not is_rework(pr(title="feat: a permanent fix for the drift register"))
    assert is_revert(pr(title="Revert \"x\""))
    assert not is_revert(pr(title="fix(x): y"))


def test_agent_authored_reads_the_estate_label() -> None:
    assert is_agent_authored(pr(labels=["agent-authored"]))
    assert is_agent_authored(pr(labels=["Agentic"]))
    assert not is_agent_authored(pr(labels=["human-authored"]))


def test_compute_core_arithmetic() -> None:
    merged = [pr(hours=2), pr(hours=10), pr(hours=100), pr(title="fix(x): y", hours=4, labels=["agent-authored"])]
    m = compute(merged, [], repos=3, now=NOW)
    assert m.merged == 4 and m.repos == 3
    assert m.lead_time_median_h == 7.0            # median of 2,4,10,100
    assert m.lead_time_p90_h == 100.0
    assert m.merged_same_day_pct == 75.0          # 3 of 4 within 24h
    assert m.rework_pct == 25.0
    assert m.agent_authored_pct == 25.0
    assert m.reverts == 0


def test_backlog_counts_only_still_open_and_ages_them() -> None:
    fresh = pr(merged=False, state="open", created=NOW - timedelta(days=1))
    cold = pr(merged=False, state="open", created=NOW - timedelta(days=9))
    m = compute([], [fresh, cold, pr(hours=1)], now=NOW)
    assert m.open_backlog == 2
    assert m.stale_open == 1


def test_empty_window_reports_nothing_rather_than_zeroes_that_read_as_facts() -> None:
    m = compute([], [], now=NOW)
    assert m.merged == 0
    assert m.lead_time_median_h is None and m.rework_pct is None and m.agent_authored_pct is None
    out = render(m)
    assert "n/a" in out


def test_render_shows_throughput_with_its_failure_signals() -> None:
    """DORA's warning: deployment frequency without change failure rate masks risk."""
    out = render(compute([pr(), pr(title="fix(a): b")], [], repos=2, now=NOW))
    assert "*Shipped:*" in out
    assert "*Change Failure:*" in out and "*Rework:*" in out
    assert "not a score" in out          # the AI-share vanity-metric caveat
    assert "proxies" in out.lower()      # Intent→Prod and Human Attention are labelled


def test_an_uninstrumented_metric_says_so_by_name() -> None:
    """Omitting it would read as 'nothing to report' for something simply not measured."""
    out = render(compute([pr()], [], now=NOW))
    assert "AI Cost / Shipped Change" in out and "not instrumented" in out


def test_percentage_metrics_move_in_POINTS_not_relative_percent() -> None:
    """5% -> 7% rework moved two POINTS; reporting +40% is how a drift reads as a crisis."""
    now_m = EngineeringMetrics(iso_week=39, merged=10, rework_pct=7.0)
    out = render(now_m, {"merged": 10, "rework_pct": 5.0})
    assert "2pp" in out and "40%" not in out


def test_an_adverse_move_is_flagged_and_a_good_one_is_not() -> None:
    out = render(EngineeringMetrics(iso_week=39, merged=10, rework_pct=7.0, change_failure_pct=2.0),
                 {"merged": 10, "rework_pct": 5.0, "change_failure_pct": 4.0})
    rework = [x for x in out.splitlines() if x.startswith("*Rework:*")][0]
    failure = [x for x in out.splitlines() if x.startswith("*Change Failure:*")][0]
    assert "⚠️" in rework and "⚠️" not in failure


def test_human_attention_covers_what_it_could_read_and_says_so() -> None:
    merged = [pr(), pr(), pr()]
    for i, x in enumerate(merged):
        x["number"] = i + 1
        x["repository_url"] = "https://api.github.com/repos/o/r"
    sig = {"o/r#1": {"first_human_touch": merged[0]["created_at"], "human_touches": 2, "changes_requested": 0}}
    m = compute(merged, [], signals=sig, now=NOW)
    assert m.signals_covered == 1
    assert "cover 1 of 3" in render(m)


def test_render_never_names_a_person() -> None:
    """Applying delivery metrics to individuals rewards split batches and inflated counts."""
    merged = [pr(labels=["agent-authored"]), pr()]
    for p in merged:
        p["user"] = {"login": "sgorelik"}
    out = render(compute(merged, [], now=NOW))
    assert "sgorelik" not in out
    assert "per person" in out or "individuals" in out


def test_trend_deltas_point_the_right_way() -> None:
    now_m = EngineeringMetrics(iso_week=39, merged=20, lead_time_median_h=4.0, rework_pct=10.0)
    prev = {"merged": 15, "lead_time_median_h": 9.0, "rework_pct": 4.0}
    out = render(now_m, prev)
    assert "↑33%" in out            # 15 -> 20 merged, and more is better
    assert "↓56%" in out            # 9h -> 4h lead time, faster is better
    assert "↑6pp ⚠️" in out         # 4% -> 10% rework, worse


def test_no_previous_snapshot_means_no_invented_trend() -> None:
    out = render(EngineeringMetrics(merged=20, lead_time_median_h=4.0), None)
    # " (flat)" with the parens: the caveat line legitimately contains "flatters".
    assert "↑" not in out and "↓" not in out and " (flat)" not in out
