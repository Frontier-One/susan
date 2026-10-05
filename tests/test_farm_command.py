"""`/susan farm` and `/susan babysit` through the real slash route.

The route triggers background tasks, so the fetch and notify functions are
stubbed instead of poking a live farm or Slack. No network is hit.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from unittest import mock

import pytest
from fastapi.testclient import TestClient

PR_FARM = "http://pr-farm.example:8787"
ISSUE_FARM = "http://issue-farm.example:8787"


def _status(**over) -> dict:
    body = {
        "in_flight": [{"name": "Frontier-One/cloud-infra#954", "elapsed_s": 34.0}],
        "last_pass": [
            "Frontier-One/cloud-infra#954 green, parked on author: approve",
        ],
        "config": {"max_concurrent": 4, "interval_s": 600},
        "uptime_s": 12345.6,
    }
    body.update(over)
    return body


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Both farms configured; a fresh app per test."""
    monkeypatch.setenv("FARM_BASE_URL", PR_FARM)
    monkeypatch.setenv("ISSUE_FARM_BASE_URL", ISSUE_FARM)

    from app.routes import app

    return TestClient(app)


def _sign_slack(body: bytes, ts: str, secret: str = "test-secret") -> str:
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _slash_post(client: TestClient, text: str, *, user: str = "U1", channel: str = "C1") -> dict:
    body = urllib.parse.urlencode(
        {"text": text, "user_id": user, "channel_id": channel, "channel_name": "general"}
    ).encode()
    ts = str(int(time.time()))
    sig = _sign_slack(body, ts)
    r = client.post(
        "/susan",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_susan_babysit_still_works_with_pr_farm_only_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing `/susan babysit` path resolves with just `FARM_BASE_URL` set."""
    monkeypatch.delenv("ISSUE_FARM_BASE_URL", raising=False)
    with mock.patch("app.routes.process_babysit", new=mock.AsyncMock()) as pb:
        j = _slash_post(client, "babysit")

    assert j["response_type"] == "ephemeral"
    assert "PR farm" in j["text"]
    pb.assert_awaited_once()


def test_farm_status_reports_both_farms(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_fetch(base_url: str):
        return _status()

    async def fake_notify(channel: str, user: str, text: str, *args, **kwargs) -> None:
        sent.append(text)

    monkeypatch.setattr("app.routes.fetch_farm_status", fake_fetch)
    monkeypatch.setattr("app.routes.notify_user_ephemeral", fake_notify)

    j = _slash_post(client, "farm status")

    assert j["response_type"] == "ephemeral"
    assert "checking both farms" in j["text"]
    assert len(sent) == 1
    report = sent[0]
    assert "*PR farm*: up" in report
    assert "*Issue farm*: up" in report
    assert "Frontier-One/cloud-infra#954 (34s)" in report


def test_farm_status_with_one_farm_unreachable_still_reports_the_other(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_fetch(base_url: str):
        if base_url.startswith(ISSUE_FARM):
            return None
        return _status()

    async def fake_notify(channel: str, user: str, text: str, *args, **kwargs) -> None:
        sent.append(text)

    monkeypatch.setattr("app.routes.fetch_farm_status", fake_fetch)
    monkeypatch.setattr("app.routes.notify_user_ephemeral", fake_notify)

    _slash_post(client, "farm status")

    assert len(sent) == 1
    report = sent[0]
    assert "*Issue farm*: unreachable" in report
    assert "*PR farm*: up" in report


def test_farm_status_with_unconfigured_issue_farm_reports_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ISSUE_FARM_BASE_URL", raising=False)
    sent: list[str] = []

    async def fake_fetch(base_url: str):
        return _status()

    async def fake_notify(channel: str, user: str, text: str, *args, **kwargs) -> None:
        sent.append(text)

    monkeypatch.setattr("app.routes.fetch_farm_status", fake_fetch)
    monkeypatch.setattr("app.routes.notify_user_ephemeral", fake_notify)

    _slash_post(client, "farm status")

    assert len(sent) == 1
    report = sent[0]
    assert "*Issue farm*: not configured" in report
    assert "*PR farm*: up" in report
    assert "unreachable" not in report


def test_farm_status_with_unreachable_issue_farm_reports_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_fetch(base_url: str):
        if base_url.startswith(ISSUE_FARM):
            return None
        return _status()

    async def fake_notify(channel: str, user: str, text: str, *args, **kwargs) -> None:
        sent.append(text)

    monkeypatch.setattr("app.routes.fetch_farm_status", fake_fetch)
    monkeypatch.setattr("app.routes.notify_user_ephemeral", fake_notify)

    _slash_post(client, "farm status")

    assert len(sent) == 1
    report = sent[0]
    assert "*Issue farm*: unreachable" in report
    assert "*PR farm*: up" in report


def test_farm_no_subcommand_says_what_it_accepts(client: TestClient) -> None:
    j = _slash_post(client, "farm")

    assert "Unknown `farm` subcommand" in j["text"]
    assert "farm status" in j["text"]


def test_farm_unknown_subcommand_says_what_it_accepts(client: TestClient) -> None:
    j = _slash_post(client, "farm nonsense")

    assert "Unknown `farm` subcommand" in j["text"]
    assert "farm status" in j["text"]


def test_farm_status_with_extra_token_is_unknown(client: TestClient) -> None:
    j = _slash_post(client, "farm status extra")

    assert "Unknown `farm` subcommand" in j["text"]
    assert "farm status" in j["text"]


def test_farm_multiple_spaces_still_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_fetch(base_url: str):
        return _status()

    async def fake_notify(channel: str, user: str, text: str, *args, **kwargs) -> None:
        sent.append(text)

    monkeypatch.setattr("app.routes.fetch_farm_status", fake_fetch)
    monkeypatch.setattr("app.routes.notify_user_ephemeral", fake_notify)

    _slash_post(client, "farm  status")

    assert len(sent) == 1
    assert "*PR farm*: up" in sent[0]


def test_pr_farm_status_reports_status_not_babysit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_fetch(base_url: str):
        return _status()

    async def fake_notify(channel: str, user: str, text: str, *args, **kwargs) -> None:
        sent.append(text)

    monkeypatch.setattr("app.routes.fetch_farm_status", fake_fetch)
    monkeypatch.setattr("app.routes.notify_user_ephemeral", fake_notify)

    j = _slash_post(client, "pr farm status")

    assert "checking both farms" in j["text"]
    assert len(sent) == 1
    assert "*PR farm*: up" in sent[0]


def test_bare_pr_farm_still_babysits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with mock.patch("app.routes.process_babysit", new=mock.AsyncMock()) as pb:
        j = _slash_post(client, "pr farm")

    assert j["response_type"] == "ephemeral"
    assert "babysit" in j["text"]
    pb.assert_awaited_once()
