"""`app/farm_status` formatter against fixture JSON (no live farm)."""
from __future__ import annotations

import httpx
import pytest

from app.farm_status import (
    NOT_CONFIGURED,
    _authorization_headers,
    fetch_farm_status,
    format_farm_status,
    issue_farm_configured,
)

PR = "PR farm"
ISSUE = "Issue farm"


def _status(**over) -> dict:
    body = {
        "in_flight": [{"name": "Frontier-One/cloud-infra#954", "elapsed_s": 34.0}],
        "last_pass": [
            "Frontier-One/cloud-infra#954 green, parked on author: approve",
            "Frontier-One/cloud-infra#955 timeouts, needs approval",
            "Frontier-One/f1-asgardOS#12 stopped at capacity",
        ],
        "config": {"max_concurrent": 4, "interval_s": 600},
        "uptime_s": 12345.6,
    }
    body.update(over)
    return body


def test_both_farms_healthy() -> None:
    text = format_farm_status([(PR, _status()), (ISSUE, _status())])

    for name in (PR, ISSUE):
        assert f"*{name}*: up" in text
    assert "Frontier-One/cloud-infra#954 (34s)" in text
    # Each farm's state on one line, detail underneath.
    assert f"*{PR}*: up\n    working on:" in text
    assert f"*{ISSUE}*: up\n    working on:" in text


def test_one_farm_unreachable_other_reports_normally() -> None:
    text = format_farm_status([(PR, None), (ISSUE, _status())])

    assert f"*{PR}*: unreachable" in text
    assert "could not read /status" in text
    # The reachable farm still reports its own health.
    assert f"*{ISSUE}*: up\n    working on: Frontier-One/cloud-infra#954 (34s)" in text


def test_both_farms_unreachable() -> None:
    text = format_farm_status([(PR, None), (ISSUE, None)])

    assert f"*{PR}*: unreachable" in text
    assert f"*{ISSUE}*: unreachable" in text


def test_nothing_in_flight() -> None:
    text = format_farm_status([(PR, _status(in_flight=[]))])

    assert f"*{PR}*: up" in text
    assert "working on: nothing in flight" in text


def test_last_pass_decisions_summarised_not_listed() -> None:
    decisions = [
        f"owner/repo#{i} decision text {i}" for i in range(1, 21)
    ]
    text = format_farm_status([(PR, _status(last_pass=decisions))])

    # A count, not every decision.
    assert "last pass: 20 decision(s)" in text
    for i in range(1, 21):
        assert f"decision text {i}" not in text


def test_missing_fields_degrade_not_raise() -> None:
    # Missing every key renders an explicit problem, never "fine".
    text = format_farm_status([(PR, {})])

    assert f"*{PR}*: problem" in text
    assert "in_flight missing or not a list" in text
    assert "last_pass missing or not a list" in text
    assert "uptime_s missing or not a number" in text


@pytest.mark.parametrize("over", [{"in_flight": None}, {"last_pass": {}}, {"uptime_s": "x"}])
def test_malformed_field_degrades_not_raise(over) -> None:
    text = format_farm_status([(PR, _status(**over))])

    assert f"*{PR}*: problem" in text


@pytest.mark.parametrize(
    "in_flight",
    [
        ["not-a-dict"],
        [{"elapsed_s": 1.0}],  # dict with no name
        [{"name": 42, "elapsed_s": 1.0}],  # non-string name
        [{"name": "owner/repo#1", "elapsed_s": "soon"}],  # non-numeric elapsed when present
    ],
)
def test_malformed_in_flight_member_is_not_fine(in_flight) -> None:
    text = format_farm_status([(PR, _status(in_flight=in_flight))])

    assert f"*{PR}*: problem" in text
    assert "malformed item" in text
    assert f"*{PR}*: up" not in text


def test_in_flight_missing_elapsed_is_still_fine() -> None:
    text = format_farm_status(
        [(PR, _status(in_flight=[{"name": "owner/repo#5", "elapsed_s": 1.0}, {"name": "owner/repo#6"}]))]
    )

    assert f"*{PR}*: up" in text
    assert "owner/repo#5 (1s)" in text
    assert "owner/repo#6" in text


def test_farm_name_mrkdwn_is_escaped() -> None:
    text = format_farm_status(
        [(PR, _status(in_flight=[{"name": "a<b>c&d", "elapsed_s": 1.0}]))]
    )

    assert f"*{PR}*: up" in text
    assert "a&lt;b&gt;c&amp;d (1s)" in text
    assert "<b>" not in text
    assert "&amp;" in text


def test_zero_uptime_is_not_healthy() -> None:
    text = format_farm_status([(PR, _status(uptime_s=0))])

    assert f"*{PR}*: problem" in text
    assert "freshly booted" in text
    assert f"*{PR}*: up" not in text


def test_not_configured_distinct_from_unreachable() -> None:
    text = format_farm_status([(PR, NOT_CONFIGURED), (ISSUE, None)])

    assert f"*{PR}*: not configured" in text
    assert "env var" in text
    assert f"*{ISSUE}*: unreachable" in text
    assert "could not read /status" in text


def test_bad_body_type_degrades_not_raise() -> None:
    text = format_farm_status([(PR, "not-a-dict")])

    assert f"*{PR}*: bad response" in text


def test_issue_farm_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ISSUE_FARM_BASE_URL", raising=False)
    assert issue_farm_configured() is False

    monkeypatch.setenv("ISSUE_FARM_BASE_URL", "http://issue.example:8787")
    assert issue_farm_configured() is True

    # FARM_BASE_URL does not imply the issue farm is configured.
    monkeypatch.delenv("ISSUE_FARM_BASE_URL")
    monkeypatch.setenv("FARM_BASE_URL", "http://pr.example:8787")
    assert issue_farm_configured() is False


@pytest.mark.asyncio
async def test_fetch_farm_status_uses_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers

            class _Response:
                status_code = 200

                def json(self):
                    return {"uptime_s": 42}

            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("FARM_SERVE_TOKEN", "tok")

    result = await fetch_farm_status("http://farm.example:8787")

    assert result == {"uptime_s": 42}
    assert captured["url"] == "http://farm.example:8787/status"
    assert captured["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_fetch_farm_status_no_token_no_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            captured["headers"] = headers

            class _Response:
                status_code = 200

                def json(self):
                    return {}

            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.delenv("FARM_SERVE_TOKEN", raising=False)

    await fetch_farm_status("http://farm.example:8787")

    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_fetch_farm_status_non_200_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            class _Response:
                status_code = 500

                def json(self):
                    return {}

            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    assert await fetch_farm_status("http://farm.example:8787") is None


def test_authorization_headers_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FARM_SERVE_TOKEN", raising=False)

    assert _authorization_headers() == {}
