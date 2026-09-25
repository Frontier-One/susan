"""Susan's environment status page: facts from the nightly snapshot, prose from our model."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import pytest

import app.status_page as sp


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


SNAP = {
    "generated": "2026-09-24T04:45:00Z",
    "environments": {
        "dev": {"apps": {"synced_healthy": 53, "drifted": 1, "progressing": 0, "degraded": 1, "missing": 3},
                "clusters": {"control": "up", "asgard-cluster": "up", "spark-office": "unknown"}},
        "freya": {"apps": {"synced_healthy": 16, "drifted": 0, "progressing": 0, "degraded": 0, "missing": 0},
                  "clusters": {"control": "up", "asgard-cluster": "up"}},
        "heimdall": {},
    },
}


def test_parse_command_prefixes() -> None:
    assert sp.parse_status_page_command("status page") == ""
    assert sp.parse_status_page_command("environment status --no-approval") == "--no-approval"
    assert sp.parse_status_page_command("weekly status") is None


def test_extract_snapshot_reads_both_files() -> None:
    out = sp.extract_snapshot(_zip({"status.json": json.dumps(SNAP), "status.html": "<html>x</html>"}))
    assert out["status_json"]["environments"]["dev"]["apps"]["synced_healthy"] == 53
    assert out["status_html"].startswith("<html>")


def test_extract_snapshot_missing_json_is_not_an_error_but_malformed_is() -> None:
    out = sp.extract_snapshot(_zip({"status.html": "<html>not measured</html>"}))
    assert out["status_json"] is None
    with pytest.raises(Exception):
        sp.extract_snapshot(_zip({"status.json": "{not json"}))
    with pytest.raises(ValueError):
        sp.extract_snapshot(_zip({"status.json": json.dumps({"nope": 1})}))


def test_env_facts_states_and_unmeasured() -> None:
    rows = {r["name"]: r for r in sp.env_facts(SNAP)}
    assert rows["dev"]["state"] == "degraded"          # 1 degraded app + 1 unknown cluster
    assert rows["dev"]["apps_total"] == 58
    assert rows["freya"]["state"] == "healthy"
    assert rows["heimdall"]["measured"] is False and rows["heimdall"]["state"] == "unmeasured"
    assert sp.env_facts(None) == []


def test_env_facts_attention_when_only_drift() -> None:
    snap = {"environments": {"x": {"apps": {"synced_healthy": 5, "drifted": 1, "progressing": 0, "degraded": 0, "missing": 0},
                                   "clusters": {"c": "up"}}}}
    assert sp.env_facts(snap)[0]["state"] == "attention"


def test_parse_narrative_tolerates_a_fence_and_rejects_garbage() -> None:
    good = '```json\n{"standfirst": "s", "environments": {"dev": "ok"}, "blockers": [{"severity": "crit", "title": "t", "ref": "#1", "body": "b"}], "cleared": ["c"], "watch": []}\n```'
    n = sp.parse_narrative(good)
    assert n["standfirst"] == "s" and n["blockers"][0]["severity"] == "crit"
    assert sp.parse_narrative("Here is your page: ...") is None
    assert sp.parse_narrative("[1,2]") is None
    # a partial object is normalised, not rejected
    assert sp.parse_narrative('{"standfirst": "x"}')["blockers"] == []


def test_render_numbers_come_from_data_and_model_text_is_escaped() -> None:
    rows = sp.env_facts(SNAP)
    narrative = {
        "standfirst": "<script>alert(1)</script> everything is fine",
        "environments": {"dev": "dev is <b>bold</b>"},
        "blockers": [{"severity": "crit", "title": "Backups <img src=x onerror=1>", "ref": "#1393", "body": "b"}],
        "cleared": ["c1"], "watch": ["w1"],
    }
    html = sp.render_status_page(rows, narrative, snapshot_when="2026-09-24T04:45:00Z", run_url="https://github.com/x/y/actions/runs/1",
                                 generated_at=datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc), model_name="glm-5.3-flash")
    assert "<b>53</b><span>of 58 apps clean</span>" in html
    assert "Apps clean</b> 69 of 74" in html          # heimdall unmeasured is excluded from the totals
    assert "Clusters reachable</b> 4 of 5" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "onerror" not in html.split("<h3>")[1].split("</h3>")[0].replace("&lt;img src=x onerror=1&gt;", "")
    assert "Not measured" in html and "produced nothing for this environment" in html
    assert "glm-5.3-flash" in html and 'class="blocker crit"' in html
    assert 'name="robots" content="noindex' in html


def test_render_without_narrative_says_so_and_still_carries_facts() -> None:
    html = sp.render_status_page(sp.env_facts(SNAP), None, snapshot_when="x", run_url="",
                                 generated_at=datetime(2026, 9, 24, tzinfo=timezone.utc), model_name=None)
    assert "Narrative unavailable this run" in html
    assert "No blockers named this run" in html
    assert "<b>53</b>" in html


def test_page_url_carries_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://susan.example/")
    monkeypatch.setenv("SUSAN_STATUS_PAGE_TOKEN", "tok123")
    assert sp.page_url("env-2026-09-24-1700") == "https://susan.example/status/env-2026-09-24-1700?k=tok123"


def test_slack_summary_counts_from_rows() -> None:
    text = sp.slack_summary(sp.env_facts(SNAP), {"standfirst": "Fine.", "blockers": [{}, {}]}, "https://u")
    assert "*dev* 53/58 apps clean · 2/3 clusters up" in text
    assert "*heimdall* not measured" in text
    assert "2 blocker(s) named" in text and "<https://u|environment status>" in text


@pytest.mark.asyncio
async def test_process_refuses_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUSAN_STATUS_PAGE_TOKEN", raising=False)
    sent: dict[str, str] = {}

    async def fake_notify(channel, user, text, blocks=None, response_url=None, **kw):
        sent["text"] = text

    monkeypatch.setattr(sp, "notify_user_ephemeral", fake_notify)
    await sp.process_status_page("status page", "C1", "U1", None, None)
    assert "SUSAN_STATUS_PAGE_TOKEN" in sent["text"]


@pytest.mark.asyncio
async def test_process_builds_stores_and_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.claude_client import ModelCompletion

    monkeypatch.setenv("SUSAN_STATUS_PAGE_TOKEN", "tok")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://susan.example")

    async def token(user: str) -> str:
        return "gh"

    async def snap(tok: str) -> dict:
        return {"status_json": SNAP, "status_html": "", "run_url": "https://run", "run_created_at": "2026-09-24T04:40:00Z"}

    async def ctx(user: str, tok: str) -> tuple[str, str]:
        return "alerts…", "- cloud-infra#1563 [2026-09-24] scrape 401"

    seen: dict[str, object] = {}

    async def completion(system, user_prompt, **kw) -> ModelCompletion:
        seen["route"] = kw.get("model_route")
        seen["prompt"] = user_prompt
        return ModelCompletion(json.dumps({"standfirst": "One sentence.", "environments": {}, "blockers": [
            {"severity": "warn", "title": "t", "ref": "#1563", "body": "b"}], "cleared": [], "watch": []}),
            model_route="sovereign", model_name="glm-5.3-flash")

    stored: dict[str, object] = {}

    async def upsert(slug, kind, title, html, **kw):
        stored.update(slug=slug, kind=kind, html=html, **kw)

    posted: dict[str, object] = {}

    async def post(channel, text, **kw):
        posted.update(channel=channel, text=text)

    async def notify(*a, **k):
        return None

    monkeypatch.setattr(sp, "get_github_token", token)
    monkeypatch.setattr(sp, "fetch_latest_status_snapshot", snap)
    monkeypatch.setattr(sp, "gather_context", ctx)
    monkeypatch.setattr(sp, "call_claude", completion)
    monkeypatch.setattr(sp, "upsert_published_page", upsert)
    monkeypatch.setattr(sp, "post_message", post)
    monkeypatch.setattr(sp, "notify_user_ephemeral", notify)

    await sp.process_status_page("status page --no-approval", "C1", "U1", None, None)

    assert seen["route"] == "sovereign"                       # our own models, by design
    assert "do not restate numbers" in str(seen["prompt"])
    assert stored["kind"] == "env-status" and stored["slug"].startswith("env-")
    assert "<b>53</b>" in str(stored["html"]) and "#1563" in str(stored["html"])
    assert posted["channel"] == "C1"
    assert f"/status/{stored['slug']}?k=tok" in str(posted["text"])


def test_schedule_add_parses_status_page() -> None:
    from app.scheduler import parse_schedule_add

    parsed = parse_schedule_add("add status page every monday at 09:00 in C0ANY6ASRB5",
                                slash_channel_id="C1", slash_channel_name="general")
    assert parsed is not None and parsed.job_type == "status_page"
    assert (parsed.hour, parsed.minute) == (9, 0)


def test_status_route_is_404_without_the_right_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrong, missing or unconfigured token must all be a plain 404 — the URL may not
    confirm a page exists. The right token serves the stored HTML with no-store."""
    from fastapi.testclient import TestClient

    import db as dbmod
    from app.routes import app

    async def latest(kind: str):
        return {"slug": "env-1", "kind": kind, "title": "t", "html": "<html>PAGE</html>", "created_at": None}

    async def one(slug: str):
        return None

    monkeypatch.setattr(dbmod, "latest_published_page", latest)
    monkeypatch.setattr(dbmod, "get_published_page", one)
    c = TestClient(app)

    monkeypatch.delenv("SUSAN_STATUS_PAGE_TOKEN", raising=False)
    assert c.get("/status/latest?k=anything").status_code == 404
    monkeypatch.setenv("SUSAN_STATUS_PAGE_TOKEN", "tok")
    assert c.get("/status/latest").status_code == 404
    assert c.get("/status/latest?k=wrong").status_code == 404
    r = c.get("/status/latest?k=tok")
    assert r.status_code == 200 and "PAGE" in r.text
    assert r.headers["cache-control"] == "private, no-store"
    assert c.get("/status/env-missing?k=tok").status_code == 404


@pytest.mark.asyncio
async def test_published_page_round_trips_through_the_real_db_layer(tmp_path, monkeypatch) -> None:
    """The accessors ran against a session factory that does not exist.

    Every earlier test stubbed `upsert_published_page`, so the first LIVE run in Slack
    was the first time the real code path ran — and it failed with
    `name 'async_session' is not defined`. This exercises the accessors themselves.
    """
    import sys

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    sys.modules.pop("db", None)
    import db as dbmod

    await dbmod.init_db()
    await dbmod.upsert_published_page("env-1", "env-status", "T", "<html>A</html>",
                                      model_route="sovereign", model_name="glm-5.3-flash")
    got = await dbmod.get_published_page("env-1")
    assert got["html"] == "<html>A</html>" and got["model_name"] == "glm-5.3-flash"
    await dbmod.upsert_published_page("env-2", "env-status", "T2", "<html>B</html>")
    assert (await dbmod.latest_published_page("env-status"))["slug"] == "env-2"
    assert await dbmod.get_published_page("nope") is None
