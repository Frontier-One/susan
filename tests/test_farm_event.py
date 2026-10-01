"""`POST /farm/event` through the real route: stubbed Slack, canned rota."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TOKEN = "farm-test-token"

# Shape of dev-tools policy/forseti-rotation.yaml, trimmed to what Susan reads.
ROTA = """
schema: 1
people:
  stacy:
    github: sgorelik
    slack_id: U0STACY
  dmitry:
    github: dmitryryabkov
    slack_id: U0DMITRY
weekdays:
  monday: stacy
"""


@pytest.fixture
def fetches() -> list[int]:
    """One entry per rota read from GitHub."""
    return []


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch, fetches: list[int]) -> list[tuple[str, str]]:
    """Every Slack post as (channel, text). A DM lands on `D-<member id>`."""
    import app.farm_event as fe

    calls: list[tuple[str, str]] = []

    async def fake_post(channel: str, text: str, *args, **kwargs) -> dict:
        calls.append((channel, text))
        return {"ok": True}

    async def fake_open(channel: str, slack_user_id: str | None = None) -> str:
        return f"D-{slack_user_id}"

    async def fake_rota() -> str:
        fetches.append(1)
        return ROTA

    monkeypatch.setattr(fe, "post_message", fake_post)
    monkeypatch.setattr(fe, "resolve_slack_post_channel", fake_open)
    monkeypatch.setattr(fe, "_fetch_rota_text", fake_rota)
    monkeypatch.setattr(fe, "_rota_cache", None)
    monkeypatch.setattr(fe, "_seen", {})
    monkeypatch.setenv("FARM_EVENT_TOKEN", TOKEN)
    monkeypatch.delenv("SUSAN_FARM_EVENT_CHANNEL", raising=False)
    return calls


@pytest.fixture
def client(sent) -> TestClient:
    from app.routes import app

    return TestClient(app)


def _event(**over) -> dict:
    body = {
        "kind": "parked",
        "repo": "Frontier-One/cloud-infra",
        "number": 954,
        "title": "Rotate the <gateway> key",
        "url": "https://github.com/Frontier-One/cloud-infra/issues/954",
        "author_github": "dmitryryabkov",
        "forseti_github": "sgorelik",
        "summary": "Which region should the bucket live in?",
    }
    body.update(over)
    return body


def _post(client: TestClient, body, token: str | None = TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/farm/event", json=body, headers=headers)


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("parked", "parked on its author"),
        ("timeout-parked", "parked after repeated timeouts"),
        ("needs-approval", "green except for a human approval"),
        ("capacity", "stopped at capacity"),
    ],
)
def test_each_kind_dms_author_and_forseti_and_posts_one_line(client, sent, kind, label) -> None:
    r = _post(client, _event(kind=kind))

    assert r.status_code == 202, r.text
    assert [c for c, _ in sent] == ["D-U0DMITRY", "D-U0STACY", "C0BD2V0KPN2"]
    author_dm, forseti_dm, line = (t for _, t in sent)
    link = "<https://github.com/Frontier-One/cloud-infra/issues/954|Frontier-One/cloud-infra#954>"
    for text in (author_dm, forseti_dm, line):
        assert link in text and label in text
    assert "Which region" in author_dm and "<@U0DMITRY> was DMed too" in forseti_dm
    assert "\n" not in line and "<@U0STACY>" in line
    # The farm's title is text, never markup.
    assert "&lt;gateway&gt;" in line and "<gateway>" not in line


def test_capacity_without_author_tells_forseti_only(client, sent) -> None:
    body = _event(kind="capacity")
    del body["author_github"]

    assert _post(client, body).status_code == 202
    assert [c for c, _ in sent] == ["D-U0STACY", "C0BD2V0KPN2"]
    assert "no author" in sent[0][1]


def test_unmapped_author_one_dm_to_forseti_saying_so(client, sent) -> None:
    assert _post(client, _event(author_github="someone-new")).status_code == 202

    assert [c for c, _ in sent] == ["D-U0STACY", "C0BD2V0KPN2"]
    assert "`someone-new` could not be mapped" in sent[0][1]


def test_author_is_forseti_gets_one_dm(client, sent) -> None:
    assert _post(client, _event(author_github="SGorelik")).status_code == 202

    assert [c for c, _ in sent] == ["D-U0STACY", "C0BD2V0KPN2"]
    assert "also the author" in sent[0][1]


def test_unmapped_forseti_still_posts_the_channel_line(client, sent) -> None:
    assert _post(client, _event(forseti_github="ghost")).status_code == 202

    assert [c for c, _ in sent] == ["D-U0DMITRY", "C0BD2V0KPN2"]
    assert "`ghost` (not mapped to Slack)" in sent[-1][1]


@pytest.mark.parametrize("token", [None, "wrong", ""])
def test_without_the_bearer_is_401_and_sends_nothing(client, sent, token) -> None:
    r = _post(client, _event(), token=token)

    assert r.status_code == 401
    assert sent == []


def test_unconfigured_token_fails_closed(client, sent, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FARM_EVENT_TOKEN")

    assert _post(client, _event(), token="anything").status_code == 401
    assert sent == []


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"kind": "done"}, "kind"),
        ({"number": "not-a-number"}, "number"),
        ({"repo": "no-slash"}, "repo"),
        ({"url": "https://evil.example/x"}, "url"),
        ({"forseti_github": None}, "forseti_github"),
    ],
)
def test_malformed_body_is_400_naming_the_field(client, sent, over, field) -> None:
    r = _post(client, _event(**over))

    assert r.status_code == 400
    assert f"`{field}`" in r.json()["detail"]
    assert sent == []


def test_missing_field_is_400_naming_it(client, sent) -> None:
    body = _event()
    del body["title"]

    r = _post(client, body)

    assert r.status_code == 400 and "`title`" in r.json()["detail"]


def test_not_json_is_400(client, sent) -> None:
    r = client.post("/farm/event", content=b"{nope", headers={"Authorization": f"Bearer {TOKEN}"})

    assert r.status_code == 400


def test_same_event_twice_in_ten_minutes_sends_nothing_the_second_time(client, sent) -> None:
    assert _post(client, _event()).status_code == 202
    first = len(sent)

    r = _post(client, _event(summary="the farm retried"))

    assert r.status_code == 202 and r.json().get("duplicate") is True
    assert len(sent) == first
    # A different kind on the same issue is a different event.
    assert _post(client, _event(kind="timeout-parked")).status_code == 202
    assert len(sent) == first * 2


def test_dedupe_window_expires(sent) -> None:
    import app.farm_event as fe

    ev = fe.FarmEvent.model_validate(_event())
    assert fe.is_duplicate(ev, now=1000.0) is False
    assert fe.is_duplicate(ev, now=1000.0 + fe.DEDUPE_SECONDS - 1) is True
    assert fe.is_duplicate(ev, now=1000.0 + fe.DEDUPE_SECONDS + 1) is False


def test_rota_is_fetched_once_per_ten_minutes(client, sent, fetches) -> None:
    _post(client, _event())
    _post(client, _event(number=955))

    assert len(fetches) == 1


def test_parse_people_reads_the_real_shape() -> None:
    from app.farm_event import parse_people

    assert parse_people(ROTA) == {"sgorelik": "U0STACY", "dmitryryabkov": "U0DMITRY"}
