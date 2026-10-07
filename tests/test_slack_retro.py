"""Incident-retro: automatic trigger on outage threads and manual `/susan retro`.

The event callback and the orchestrator are exercised through their real entry
points; Slack and GitHub SDK calls are stubbed so no network is hit.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import db

from app.retro import dispatch_incident_retro
from app.slack_events import handle_slack_event_callback
from app.retro import process_retro_command, start_retro

CHANNEL = "C12345"
THREAD_TS = "1712345678.123456"
ROOT_TS = "1712345678.000000"
PERMALINK = f"https://frontier-one.slack.com/archives/{CHANNEL}/p1712345678123456"


def _resolved_reply_payload(text: str = "*Resolved* :large_green_circle:") -> dict:
    return {
        "event": {
            "type": "message",
            "channel": CHANNEL,
            "user": "U1",
            "text": text,
            "thread_ts": THREAD_TS,
        }
    }


def _outage_history() -> str:
    # fetch_slack_history reverses messages, root last.
    return f"U2: some reply\nU1: :warning: outage declared in prod"


def _normal_history() -> str:
    return "U2: just chatting\nU1: all good here"


def _stub_event_leaves(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the socket layer under the event callback; return the mocks."""
    fetch = mock.AsyncMock(return_value=_outage_history())
    event_get = mock.AsyncMock(return_value=None)
    retro_get = mock.AsyncMock(return_value=None)
    post = mock.AsyncMock()
    dispatch = mock.AsyncMock(return_value=f"Retro started: {PERMALINK}")
    create = mock.AsyncMock(return_value="rid-1")

    monkeypatch.setattr("app.slack_events.fetch_slack_history", fetch)
    monkeypatch.setattr("app.slack_events.get_incident_retro", event_get)
    monkeypatch.setattr("app.retro.get_incident_retro", retro_get)
    monkeypatch.setattr("app.retro.post_message", post)
    monkeypatch.setattr("app.slack_events.post_message", post)
    monkeypatch.setattr("app.retro.dispatch_incident_retro", dispatch)
    monkeypatch.setattr("app.retro.create_incident_retro", create)
    return {"fetch": fetch, "event_get": event_get, "retro_get": retro_get,
            "post": post, "dispatch": dispatch, "create": create}


@pytest.mark.asyncio
async def test_resolved_in_outage_thread_dispatches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _stub_event_leaves(monkeypatch)

    await handle_slack_event_callback(_resolved_reply_payload())

    mocks["fetch"].assert_awaited_once()
    mocks["dispatch"].assert_awaited_once_with(
        channel=CHANNEL,
        thread_ts=THREAD_TS,
        permalink=PERMALINK,
        text=_outage_history(),
        slack_user_id="U1",
    )
    # kickoff posted into the thread
    call = mocks["post"].await_args
    assert call is not None
    assert call.args[0] == CHANNEL
    assert call.kwargs.get("thread_ts") == THREAD_TS
    assert "Outage postmortem" in call.args[1]
    assert "*Root cause*" in call.args[1]
    mocks["create"].assert_awaited_once_with(CHANNEL, THREAD_TS, PERMALINK)


@pytest.mark.asyncio
async def test_second_resolved_does_not_dispatch_again(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _stub_event_leaves(monkeypatch)
    # A retro is already recorded for the thread.
    mocks["event_get"].return_value = {"permalink": PERMALINK}
    mocks["retro_get"].return_value = {"permalink": PERMALINK}

    await handle_slack_event_callback(_resolved_reply_payload())

    mocks["dispatch"].assert_not_awaited()
    mocks["create"].assert_not_awaited()
    # The automatic path posts the existing retro's link into the thread.
    mocks["post"].assert_awaited_once()
    call = mocks["post"].await_args
    assert call.args[0] == CHANNEL
    assert call.kwargs.get("thread_ts") == THREAD_TS
    assert call.args[1] == f"Retro already started: {PERMALINK}"


@pytest.mark.asyncio
async def test_retro_already_started_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """The orchestrator reports the existing link instead of re-dispatching."""
    retro_get = mock.AsyncMock(return_value={"permalink": PERMALINK})
    dispatch = mock.AsyncMock()
    post = mock.AsyncMock()
    monkeypatch.setattr("app.retro.get_incident_retro", retro_get)
    monkeypatch.setattr("app.retro.post_message", post)
    monkeypatch.setattr("app.retro.dispatch_incident_retro", dispatch)

    result = await start_retro(
        channel=CHANNEL, thread_root_ts=THREAD_TS, permalink=PERMALINK, thread_text="x"
    )

    assert result == f"Retro already started: {PERMALINK}"
    dispatch.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_dispatch_does_not_record_retro(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed workflow dispatch does not record the retro and surfaces the error."""
    retro_get = mock.AsyncMock(return_value=None)
    post = mock.AsyncMock()
    dispatch = mock.AsyncMock(return_value="Retro dispatch error (422): nope")
    create = mock.AsyncMock()
    monkeypatch.setattr("app.retro.get_incident_retro", retro_get)
    monkeypatch.setattr("app.retro.post_message", post)
    monkeypatch.setattr("app.retro.dispatch_incident_retro", dispatch)
    monkeypatch.setattr("app.retro.create_incident_retro", create)

    result = await start_retro(
        channel=CHANNEL, thread_root_ts=THREAD_TS, permalink=PERMALINK, thread_text="x"
    )

    assert result == "Retro dispatch error (422): nope"
    dispatch.assert_awaited_once()
    post.assert_awaited_once()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolved_in_non_outage_thread_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocks = _stub_event_leaves(monkeypatch)
    mocks["fetch"].return_value = _normal_history()

    await handle_slack_event_callback(_resolved_reply_payload())

    mocks["dispatch"].assert_not_awaited()
    mocks["post"].assert_not_awaited()
    mocks["create"].assert_not_awaited()


@pytest.mark.asyncio
async def test_resolved_marker_missing_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _stub_event_leaves(monkeypatch)
    await handle_slack_event_callback(
        _resolved_reply_payload(text="just a status update, no resolved marker")
    )
    mocks["dispatch"].assert_not_awaited()
    mocks["post"].assert_not_awaited()


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, headers: dict, json: dict) -> FakeResponse:
        self.calls.append((url, headers, json))
        return self.response


@pytest.mark.asyncio
async def test_dispatch_incident_retro_url_and_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(FakeResponse(204, {}))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("app.github_actions.httpx.AsyncClient", lambda **kw: client)

    result = await dispatch_incident_retro(
        channel=CHANNEL,
        thread_ts=THREAD_TS,
        permalink=PERMALINK,
        text="wow such outage",
        slack_user_id="U1",
    )

    assert result == f"Retro started: {PERMALINK}"
    assert len(client.calls) == 1
    url, headers, payload = client.calls[0]
    assert url == "https://api.github.com/repos/Frontier-One/dev-tools/actions/workflows/incident-retro.yml/dispatches"
    assert payload == {
        "ref": "main",
        "inputs": {
            "channel": CHANNEL,
            "thread_ts": THREAD_TS,
            "permalink": PERMALINK,
            "text": "wow such outage",
        },
    }
    assert headers["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_dispatch_incident_retro_custom_repo_and_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUSAN_RETRO_DISPATCH_REPO", "MyOrg/MyRepo")
    monkeypatch.setenv("GITHUB_BASE_BRANCH", "release")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    client = FakeClient(FakeResponse(204, {}))
    monkeypatch.setattr("app.github_actions.httpx.AsyncClient", lambda **kw: client)

    await dispatch_incident_retro(
        channel=CHANNEL, thread_ts=THREAD_TS, permalink=PERMALINK, text="t"
    )

    url, _headers, payload = client.calls[0]
    assert url == "https://api.github.com/repos/MyOrg/MyRepo/actions/workflows/incident-retro.yml/dispatches"
    assert payload["ref"] == "release"


@pytest.mark.asyncio
async def test_dispatch_incident_retro_error_returns_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(FakeResponse(422, {"errors": "nope"}))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("app.github_actions.httpx.AsyncClient", lambda **kw: client)

    result = await dispatch_incident_retro(
        channel=CHANNEL, thread_ts=THREAD_TS, permalink=PERMALINK, text="t"
    )

    assert "Retro dispatch error (422)" in result
    assert isinstance(result, str)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("GITHUB_BASE_BRANCH", raising=False)
    monkeypatch.delenv("SUSAN_RETRO_DISPATCH_REPO", raising=False)
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


def test_slash_retro_wires_background_task(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    process = mock.AsyncMock()
    monkeypatch.setattr("app.routes.process_retro_command", process)

    j = _slash_post(client, f"retro {PERMALINK}")

    assert j["response_type"] == "ephemeral"
    assert "postmortem" in j["text"]
    process.assert_awaited_once()
    call = process.await_args
    assert call.args[0] == f"retro {PERMALINK}"
    assert call.args[1] == "C1"
    assert call.args[2] == "U1"


def test_slash_retro_bad_link_says_so(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    process = mock.AsyncMock()
    monkeypatch.setattr("app.routes.process_retro_command", process)

    j = _slash_post(client, "retro not-a-link")

    assert "doesn’t understand" in j["text"] or "doesn't understand" in j["text"]
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_retro_command_drives_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = mock.AsyncMock(return_value="some thread text")
    notify = mock.AsyncMock()
    retro_get = mock.AsyncMock(return_value=None)
    post = mock.AsyncMock()
    dispatch = mock.AsyncMock(return_value=f"Retro started: {PERMALINK}")
    create = mock.AsyncMock(return_value="rid-2")
    resolve_root = mock.AsyncMock(return_value=ROOT_TS)

    monkeypatch.setattr("app.retro.fetch_slack_history", fetch)
    monkeypatch.setattr("app.retro.notify_user_ephemeral", notify)
    monkeypatch.setattr("app.retro.get_incident_retro", retro_get)
    monkeypatch.setattr("app.retro.post_message", post)
    monkeypatch.setattr("app.retro.dispatch_incident_retro", dispatch)
    monkeypatch.setattr("app.retro.create_incident_retro", create)
    monkeypatch.setattr("app.retro.resolve_thread_root_ts", resolve_root)

    await process_retro_command(PERMALINK, "C1", "U1", "resp-url")

    fetch.assert_awaited_once_with(CHANNEL, THREAD_TS, "U1")
    # The pasted permalink ts is resolved to the thread root before use.
    resolve_root.assert_awaited_once_with(CHANNEL, THREAD_TS)
    dispatch.assert_awaited_once()
    call = dispatch.await_args
    assert call.kwargs["channel"] == CHANNEL
    # The resolved root, not the pasted reply ts, is the idempotency key and
    # the thread_ts dispatched, so it matches the automatic path's root key.
    assert call.kwargs["thread_ts"] == ROOT_TS
    assert call.kwargs["permalink"] == PERMALINK
    notify.assert_awaited_once()
    assert notify.await_args.args[2] == f"Retro started: {PERMALINK}"


@pytest.mark.asyncio
async def test_resolve_thread_root_ts_uses_reply_thread_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A permalink to a reply resolves to the root's ts via thread_ts.
    replies = mock.AsyncMock(
        return_value={
            "ok": True,
            "messages": [
                {"ts": ROOT_TS, "user": "U1"},
                {"ts": THREAD_TS, "thread_ts": ROOT_TS, "user": "U2"},
            ],
        }
    )
    monkeypatch.setattr("app.slack_api._slack_conversations_replies_page", replies)

    from app.slack_api import resolve_thread_root_ts

    assert await resolve_thread_root_ts(CHANNEL, THREAD_TS) == ROOT_TS
    replies.assert_awaited_once_with(CHANNEL, THREAD_TS, None)


@pytest.mark.asyncio
async def test_resolve_thread_root_ts_falls_back_to_passed_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A message that is itself the root has no thread_ts anywhere.
    replies = mock.AsyncMock(
        return_value={
            "ok": True,
            "messages": [{"ts": THREAD_TS, "user": "U1"}],
        }
    )
    monkeypatch.setattr("app.slack_api._slack_conversations_replies_page", replies)

    from app.slack_api import resolve_thread_root_ts

    assert await resolve_thread_root_ts(CHANNEL, THREAD_TS) == THREAD_TS


@pytest.mark.asyncio
async def test_resolve_thread_root_ts_api_failure_returns_passed_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failed API call must not raise; the manual path proceeds with the key.
    replies = mock.AsyncMock(side_effect=RuntimeError("nope"))
    monkeypatch.setattr("app.slack_api._slack_conversations_replies_page", replies)

    from app.slack_api import resolve_thread_root_ts

    assert await resolve_thread_root_ts(CHANNEL, THREAD_TS) == THREAD_TS


@pytest.mark.asyncio
async def test_incident_retro_db_round_trip() -> None:
    await db.init_db()
    channel_id = "C-retro-test"
    thread_root_ts = THREAD_TS
    permalink = PERMALINK

    retro_id = await db.create_incident_retro(channel_id, thread_root_ts, permalink)
    assert retro_id

    found = await db.get_incident_retro(channel_id, thread_root_ts)
    assert found is not None
    assert found["permalink"] == permalink
