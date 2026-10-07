"""Susan's environment status page: facts from the nightly snapshot, prose from our model."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

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


def test_render_surfaces_the_run_it_read_not_the_render_time() -> None:
    """The page names the artifact it actually read: run id and completion time from the
    fetched object, and the snapshot's own generated stamp, not the render time."""
    html = sp.render_status_page(sp.env_facts(SNAP), None,
                                 snapshot_when="2026-09-21T04:45:00Z", run_url="https://run",
                                 generated_at=datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc),
                                 model_name=None, run_id="9876543",
                                 run_completed_at="2026-09-21T04:52:00Z")
    assert "9876543" in html
    assert "2026-09-21T04:52:00Z" in html
    assert "2026-09-21T04:45:00Z" in html
    # the render time is the Page built stamp only, never presented as the run's identity
    assert "2026-10-07" in html
    assert "Page built" in html


def test_render_flags_a_stale_snapshot() -> None:
    """A snapshot older than one nightly interval gets a banner so a reader need not
    compare the two stamps themselves."""
    html = sp.render_status_page(sp.env_facts(SNAP), None,
                                 snapshot_when="2026-09-21T04:45:00Z", run_url="",
                                 generated_at=datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc),
                                 model_name=None)
    assert "nights old" in html
    assert "should not be read as fresh" in html


def test_render_no_stale_banner_when_recent() -> None:
    """A snapshot within the nightly interval is presented without a stale notice."""
    html = sp.render_status_page(sp.env_facts(SNAP), None,
                                 snapshot_when="2026-10-07T04:45:00Z", run_url="",
                                 generated_at=datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc),
                                 model_name=None)
    assert "nights old" not in html
    assert "should not be read as fresh" not in html


def test_render_no_stale_banner_when_unparseable() -> None:
    """A missing or unparseable generated stamp must not raise and must not invent staleness."""
    for when in ("", "not-a-date", "x"):
        html = sp.render_status_page(sp.env_facts(SNAP), None, snapshot_when=when, run_url="",
                                     generated_at=datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc),
                                     model_name=None)
        assert "nights old" not in html


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
        stored[f"slug:{slug}"] = {"kind": kind, "html": html, **kw}

    async def prune(kind, older_than, protected_slug=None):
        return 0

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

    seen_cleared: dict[str, object] = {}

    async def prev(before_key: str):
        seen_cleared["before_key"] = before_key
        return None

    async def snap_store(key: str, payload: list) -> None:
        seen_cleared["stored"] = payload

    monkeypatch.setattr(sp, "previous_status_snapshot", prev)
    monkeypatch.setattr(sp, "upsert_status_snapshot", snap_store)
    monkeypatch.setattr(sp, "prune_published_pages", prune)

    await sp.process_status_page("status page --no-approval", "C1", "U1", None, None)

    assert seen["route"] == "sovereign"                       # our own models, by design
    assert "do not restate numbers" in str(seen["prompt"])
    stable = stored["slug:env-status"]
    archive = stored["slug:env-status-2026-09-24"]
    assert stable["kind"] == "env-status" and archive["kind"] == "env-status"
    assert "<b>53</b>" in str(stable["html"]) and "#1563" in str(stable["html"])
    assert stable["html"] == archive["html"]                  # both carry the same rendered page
    assert posted["channel"] == "C1"
    assert f"/status/env-status?k=tok" in str(posted["text"])  # Slack links to the stable slug
    assert seen_cleared["before_key"] == "2026-09-24"         # today's snapshot date feeds the prior lookup
    assert seen_cleared["stored"] == sp.env_facts(SNAP)       # today's facts are stored for tomorrow


@pytest.mark.asyncio
async def test_process_cleared_list_is_computed_from_stored_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-healthy environment that recovers is listed as cleared through the real entry point."""
    from app.claude_client import ModelCompletion

    monkeypatch.setenv("SUSAN_STATUS_PAGE_TOKEN", "tok")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://susan.example")

    # dev is degraded in SNAP; flip its data so env_facts derives a healthy state today.
    current = {
        "generated": SNAP["generated"],
        "environments": {
            **SNAP["environments"],
            "dev": {
                "apps": {"synced_healthy": 58, "drifted": 0, "progressing": 0, "degraded": 0, "missing": 0},
                "clusters": {"control": "up", "asgard-cluster": "up", "spark-office": "up"},
            },
        },
    }

    async def token(user: str) -> str:
        return "gh"

    async def snap(tok: str) -> dict:
        return {"status_json": current, "status_html": "", "run_url": "https://run", "run_created_at": "2026-09-24T04:40:00Z"}

    async def ctx(user: str, tok: str) -> tuple[str, str]:
        return "alerts…", "- cloud-infra#1563 [2026-09-24] scrape 401"

    async def completion(system, user_prompt, **kw) -> ModelCompletion:
        return ModelCompletion(json.dumps({"standfirst": "One sentence.", "environments": {}, "blockers": [], "cleared": [], "watch": []}),
            model_route="sovereign", model_name="glm-5.3-flash")

    stored: dict[str, object] = {}

    async def upsert(slug, kind, title, html, **kw):
        stored[f"slug:{slug}"] = {"kind": kind, "html": html, **kw}

    async def prune(kind, older_than, protected_slug=None):
        return 0

    async def notify(*a, **k):
        return None

    async def post(channel, text, **kw):
        return None

    monkeypatch.setattr(sp, "get_github_token", token)
    monkeypatch.setattr(sp, "fetch_latest_status_snapshot", snap)
    monkeypatch.setattr(sp, "gather_context", ctx)
    monkeypatch.setattr(sp, "call_claude", completion)
    monkeypatch.setattr(sp, "upsert_published_page", upsert)
    monkeypatch.setattr(sp, "post_message", post)
    monkeypatch.setattr(sp, "notify_user_ephemeral", notify)

    async def prev(before_key: str):
        return [{"name": "dev", "state": "degraded"}]

    async def snap_store(key: str, payload: list) -> None:
        return None

    monkeypatch.setattr(sp, "previous_status_snapshot", prev)
    monkeypatch.setattr(sp, "upsert_status_snapshot", snap_store)
    monkeypatch.setattr(sp, "prune_published_pages", prune)

    await sp.process_status_page("status page --no-approval", "C1", "U1", None, None)

    # the model named no cleared items; dev appears only because the facts arithmetic cleared it
    stable = stored["slug:env-status"]
    archive = stored["slug:env-status-2026-09-24"]
    for page in (stable, archive):
        assert "Cleared since last time" in str(page["html"])
        assert "<li>dev</li>" in str(page["html"])


def test_schedule_add_parses_status_page() -> None:
    from app.scheduler import parse_schedule_add

    parsed = parse_schedule_add("add status page every monday at 09:00 in C0ANY6ASRB5",
                                slash_channel_id="C1", slash_channel_name="general")
    assert parsed is not None and parsed.job_type == "status_page"
    assert (parsed.hour, parsed.minute) == (9, 0)


def test_snapshot_date_key_requires_a_real_calendar_date() -> None:
    """A malformed date like 2026-99-99 must fall back to today; a valid one is kept."""
    today = datetime.now(timezone.utc).date().isoformat()
    assert sp._snapshot_date_key("2026-09-24T04:45:00Z") == "2026-09-24"
    assert sp._snapshot_date_key("2026-99-99T04:45:00Z") == today
    assert sp._snapshot_date_key("") == today


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


@pytest.mark.asyncio
async def test_stable_and_archive_publish_and_prune_through_the_real_db_layer(tmp_path, monkeypatch) -> None:
    """Stable slug overwrites each run, the dated archive is kept, and pruning removes
    only archives older than the cutoff. Drives the real db helpers."""
    import sys

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    sys.modules.pop("db", None)
    import db as dbmod

    await dbmod.init_db()

    # a run writes the stable slug plus a dated archive, both of the same kind
    await dbmod.upsert_published_page("env-status", "env-status", "T", "<html>NOV</html>")
    await dbmod.upsert_published_page("env-status-2026-10-01", "env-status", "T", "<html>OCT</html>")
    await dbmod.upsert_published_page("env-status-2026-09-25", "env-status", "T", "<html>SEP</html>")

    assert (await dbmod.get_published_page("env-status"))["kind"] == "env-status"
    assert (await dbmod.get_published_page("env-status-2026-10-01"))["kind"] == "env-status"
    assert (await dbmod.get_published_page("env-status-2026-09-25"))["kind"] == "env-status"

    # a second run overwrites the stable slug without duplicating it
    await dbmod.upsert_published_page("env-status", "env-status", "T", "<html>NOV2</html>")
    assert (await dbmod.get_published_page("env-status"))["html"] == "<html>NOV2</html>"

    # latest resolves to the newest of the kind
    latest = await dbmod.latest_published_page("env-status")
    assert latest is not None and latest["kind"] == "env-status" and latest["html"] == "<html>NOV2</html>"

    # backdate the two archives so pruning is deterministic
    now = datetime.now(timezone.utc)
    async with dbmod.SessionLocal() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(dbmod.PublishedPage))).scalars().all()
        for r in rows:
            if r.slug == "env-status-2026-10-01":
                r.created_at = now - timedelta(days=5)
            else:
                r.created_at = now - timedelta(days=40)   # backdate the stable slug too, so prune must spare it by rule
        await session.commit()

    # prune everything older than 30 days: only the 40-day-old archive goes
    removed = await dbmod.prune_published_pages("env-status", now - timedelta(days=30),
                                                protected_slug="env-status")
    assert removed == 1
    assert await dbmod.get_published_page("env-status") is not None       # stable stays
    assert await dbmod.get_published_page("env-status-2026-10-01") is not None
    assert await dbmod.get_published_page("env-status-2026-09-25") is None

    # a different kind is untouched
    await dbmod.upsert_published_page("weekly/foo", "weekly", "T", "<html>W</html>")
    await dbmod.prune_published_pages("env-status", now - timedelta(days=365),
                                      protected_slug="env-status")
    assert (await dbmod.get_published_page("weekly/foo")) is not None


def test_refuses_stale_publish() -> None:
    newer = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    older = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)
    assert sp.refuses_stale_publish(older, newer) is True      # fetched strictly older
    assert sp.refuses_stale_publish(newer, older) is False     # fetched newer
    assert sp.refuses_stale_publish(newer, newer) is False     # fetched equal
    assert sp.refuses_stale_publish(None, newer) is False      # no fetched evidence
    assert sp.refuses_stale_publish(older, None) is False      # no published evidence
    assert sp.refuses_stale_publish(None, None) is False


def test_refuses_stale_publish_tolerates_a_naive_published_stamp() -> None:
    """A SQLite-held DB reads generated_at back naive; the comparator must not raise on
    the mixed naive/aware pair and must still judge UTC equality."""
    fetched_older = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)
    fetched_newer = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    published_naive_newer = datetime(2026, 10, 6, 4, 0)        # naive, read back as UTC
    published_naive_older = datetime(2026, 9, 14, 4, 0)        # naive, read back as UTC
    # fetched older than the naive published stamp → refuse
    assert sp.refuses_stale_publish(fetched_older, published_naive_newer) is True
    # fetched newer than the naive published stamp → do not refuse
    assert sp.refuses_stale_publish(fetched_newer, published_naive_older) is False
    # fetched equal to the naive published stamp → do not refuse
    assert sp.refuses_stale_publish(fetched_older, published_naive_older) is False


@pytest.mark.asyncio
async def test_process_refuses_to_replace_a_fresher_page_with_a_staler_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run whose snapshot predates the live page's generated stamp must publish nothing
    and tell the user why, so fresher data is never replaced by staler."""
    from app.claude_client import ModelCompletion

    monkeypatch.setenv("SUSAN_STATUS_PAGE_TOKEN", "tok")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://susan.example")

    stale_snap = dict(SNAP, generated="2026-09-14T04:45:00Z")

    async def token(user: str) -> str:
        return "gh"

    async def snap(tok: str) -> dict:
        return {"status_json": stale_snap, "status_html": "", "run_url": "https://run",
                "run_created_at": "2026-09-14T04:40:00Z"}

    async def ctx(user: str, tok: str) -> tuple[str, str]:
        return "alerts…", "- cloud-infra#1563"

    async def completion(system, user_prompt, **kw) -> ModelCompletion:
        return ModelCompletion(json.dumps({"standfirst": "s", "environments": {}, "blockers": [], "cleared": [], "watch": []}),
                               model_route="sovereign", model_name="glm-5.3-flash")

    async def live_page(slug: str):
        return {"slug": slug, "kind": "env-status", "title": "T", "html": "<html>LIVE</html>",
                "created_at": None, "generated_at": datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)}

    published: list[str] = []
    snap_stored: list[str] = []

    async def upsert(slug, kind, title, html, **kw):
        published.append(slug)

    async def snap_store(key: str, payload: list) -> None:
        snap_stored.append(key)

    async def prune(kind, older_than, protected_slug=None):
        return 0

    async def prev(before_key: str):
        return None

    notified: dict[str, str] = {}

    async def notify(channel, user, text, blocks=None, response_url=None, **kw):
        notified["text"] = text

    async def post(channel, text, **kw):
        return None

    monkeypatch.setattr(sp, "get_github_token", token)
    monkeypatch.setattr(sp, "fetch_latest_status_snapshot", snap)
    monkeypatch.setattr(sp, "gather_context", ctx)
    monkeypatch.setattr(sp, "call_claude", completion)
    monkeypatch.setattr(sp, "get_published_page", live_page)
    monkeypatch.setattr(sp, "upsert_published_page", upsert)
    monkeypatch.setattr(sp, "upsert_status_snapshot", snap_store)
    monkeypatch.setattr(sp, "prune_published_pages", prune)
    monkeypatch.setattr(sp, "previous_status_snapshot", prev)
    monkeypatch.setattr(sp, "notify_user_ephemeral", notify)
    monkeypatch.setattr(sp, "post_message", post)

    await sp.process_status_page("status page --no-approval", "C1", "U1", None, None)

    assert published == []                        # no stable or archive publish
    assert snap_stored == []                      # no snapshot persisted either
    assert "older" in notified["text"]
    assert "nothing was replaced" in notified["text"]


@pytest.mark.asyncio
async def test_process_refuses_with_a_naive_live_generated_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SQLite-held DB returns generated_at naive; the refusal path must read it as UTC,
    refuse a staler fetch, and not raise on the mixed naive/aware comparison."""
    from app.claude_client import ModelCompletion

    monkeypatch.setenv("SUSAN_STATUS_PAGE_TOKEN", "tok")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://susan.example")

    stale_snap = dict(SNAP, generated="2026-09-14T04:45:00Z")

    async def token(user: str) -> str:
        return "gh"

    async def snap(tok: str) -> dict:
        return {"status_json": stale_snap, "status_html": "", "run_url": "https://run",
                "run_created_at": "2026-09-14T04:40:00Z"}

    async def ctx(user: str, tok: str) -> tuple[str, str]:
        return "alerts…", "- cloud-infra#1563"

    async def completion(system, user_prompt, **kw) -> ModelCompletion:
        return ModelCompletion(json.dumps({"standfirst": "s", "environments": {}, "blockers": [], "cleared": [], "watch": []}),
                               model_route="sovereign", model_name="glm-5.3-flash")

    async def live_page(slug: str):
        return {"slug": slug, "kind": "env-status", "title": "T", "html": "<html>LIVE</html>",
                "created_at": None, "generated_at": datetime(2026, 10, 6, 4, 0)}

    published: list[str] = []
    snap_stored: list[str] = []

    async def upsert(slug, kind, title, html, **kw):
        published.append(slug)

    async def snap_store(key: str, payload: list) -> None:
        snap_stored.append(key)

    async def prune(kind, older_than, protected_slug=None):
        return 0

    async def prev(before_key: str):
        return None

    notified: dict[str, str] = {}

    async def notify(channel, user, text, blocks=None, response_url=None, **kw):
        notified["text"] = text

    async def post(channel, text, **kw):
        return None

    monkeypatch.setattr(sp, "get_github_token", token)
    monkeypatch.setattr(sp, "fetch_latest_status_snapshot", snap)
    monkeypatch.setattr(sp, "gather_context", ctx)
    monkeypatch.setattr(sp, "call_claude", completion)
    monkeypatch.setattr(sp, "get_published_page", live_page)
    monkeypatch.setattr(sp, "upsert_published_page", upsert)
    monkeypatch.setattr(sp, "upsert_status_snapshot", snap_store)
    monkeypatch.setattr(sp, "prune_published_pages", prune)
    monkeypatch.setattr(sp, "previous_status_snapshot", prev)
    monkeypatch.setattr(sp, "notify_user_ephemeral", notify)
    monkeypatch.setattr(sp, "post_message", post)

    await sp.process_status_page("status page --no-approval", "C2", "U2", None, None)

    assert published == []                        # no stable or archive publish
    assert snap_stored == []                      # no snapshot persisted either
    assert "older" in notified["text"]
    assert "nothing was replaced" in notified["text"]


@pytest.mark.asyncio
async def test_process_publishes_fresh_snapshot_even_when_an_older_page_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot newer than the live page's generated stamp publishes normally, carrying
    the fetched generated through to both stable and archive pages."""
    from app.claude_client import ModelCompletion

    monkeypatch.setenv("SUSAN_STATUS_PAGE_TOKEN", "tok")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://susan.example")

    fresh_snap = dict(SNAP, generated="2026-09-26T04:45:00Z")

    async def token(user: str) -> str:
        return "gh"

    async def snap(tok: str) -> dict:
        return {"status_json": fresh_snap, "status_html": "", "run_url": "https://run",
                "run_created_at": "2026-09-26T04:40:00Z", "run_id": "11", "run_completed_at": "2026-09-26T04:52:00Z"}

    async def ctx(user: str, tok: str) -> tuple[str, str]:
        return "alerts…", "- cloud-infra#1563"

    async def completion(system, user_prompt, **kw) -> ModelCompletion:
        return ModelCompletion(json.dumps({"standfirst": "s", "environments": {}, "blockers": [], "cleared": [], "watch": []}),
                               model_route="sovereign", model_name="glm-5.3-flash")

    async def live_page(slug: str):
        return {"slug": slug, "kind": "env-status", "title": "T", "html": "<html>OLD</html>",
                "created_at": None, "generated_at": datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc)}

    published: dict[str, object] = {}

    async def upsert(slug, kind, title, html, **kw):
        published[slug] = kw

    async def snap_store(key: str, payload: list) -> None:
        return None

    async def prune(kind, older_than, protected_slug=None):
        return 0

    async def prev(before_key: str):
        return None

    async def notify(*a, **k):
        return None

    async def post(channel, text, **kw):
        return None

    monkeypatch.setattr(sp, "get_github_token", token)
    monkeypatch.setattr(sp, "fetch_latest_status_snapshot", snap)
    monkeypatch.setattr(sp, "gather_context", ctx)
    monkeypatch.setattr(sp, "call_claude", completion)
    monkeypatch.setattr(sp, "get_published_page", live_page)
    monkeypatch.setattr(sp, "upsert_published_page", upsert)
    monkeypatch.setattr(sp, "upsert_status_snapshot", snap_store)
    monkeypatch.setattr(sp, "prune_published_pages", prune)
    monkeypatch.setattr(sp, "previous_status_snapshot", prev)
    monkeypatch.setattr(sp, "notify_user_ephemeral", notify)
    monkeypatch.setattr(sp, "post_message", post)

    await sp.process_status_page("status page --no-approval", "C1", "U1", None, None)

    assert "env-status" in published and "env-status-2026-09-26" in published
    expected_generated = sp._parse_generated("2026-09-26T04:45:00Z")
    for slug in ("env-status", "env-status-2026-09-26"):
        assert published[slug]["generated_at"] == expected_generated


@pytest.mark.asyncio
async def test_init_db_migrates_generated_at_onto_a_pre_column_published_pages(tmp_path, monkeypatch) -> None:
    """A deployed published_pages that predates generated_at is upgraded by init_db."""
    import sys

    from sqlalchemy import text as _text

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    sys.modules.pop("db", None)
    import db as dbmod

    # hand-build the pre-column table exactly as create_all would have made it, then
    # populate a row the deployed page would have left behind
    async with dbmod.engine.begin() as conn:
        await conn.execute(_text(
            "CREATE TABLE published_pages ("
            "slug VARCHAR(120) NOT NULL, "
            "kind VARCHAR(40) NOT NULL, "
            "title VARCHAR(200) NOT NULL, "
            "html TEXT NOT NULL, "
            "model_route VARCHAR(40), "
            "model_name VARCHAR(120), "
            "created_at DATETIME NOT NULL, "
            "PRIMARY KEY (slug))"
        ))
        await conn.execute(_text(
            "INSERT INTO published_pages "
            "(slug, kind, title, html, model_route, model_name, created_at) "
            "VALUES ('env-1', 'env-status', 'T', '<html>OLD</html>', "
            "'sovereign', 'glm-5.3-flash', '2026-09-24 04:45:00')"
        ))

    # the migration path adds generated_at without disturbing the existing row
    await dbmod.init_db()

    got = await dbmod.get_published_page("env-1")
    assert got["html"] == "<html>OLD</html>"
    assert got["generated_at"] is None

    # generated_at reads and writes against the upgraded table
    g = datetime(2026, 9, 24, 4, 45, tzinfo=timezone.utc)
    await dbmod.upsert_published_page("env-1", "env-status", "T", "<html>NEW</html>", generated_at=g)
    got2 = await dbmod.get_published_page("env-1")
    stored = got2["generated_at"]
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)   # SQLite reads back naive
    assert stored == g

    # idempotent: a second init_db (column already present) does not raise
    await dbmod.init_db()


@pytest.mark.asyncio
async def test_published_page_generated_at_round_trips_through_the_real_db_layer(tmp_path, monkeypatch) -> None:
    """A page's source `generated` stamp persists through insert and the overwrite update."""
    import sys

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    sys.modules.pop("db", None)
    import db as dbmod

    await dbmod.init_db()
    g = datetime(2026, 9, 24, 4, 45, tzinfo=timezone.utc)
    await dbmod.upsert_published_page("env-1", "env-status", "T", "<html>A</html>", generated_at=g)
    got = await dbmod.get_published_page("env-1")
    stored = got["generated_at"]
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)   # SQLite reads back naive
    assert stored == g

    g2 = datetime(2026, 9, 25, 4, 45, tzinfo=timezone.utc)
    await dbmod.upsert_published_page("env-1", "env-status", "T", "<html>B</html>", generated_at=g2)
    got2 = await dbmod.get_published_page("env-1")
    stored2 = got2["generated_at"]
    if stored2.tzinfo is None:
        stored2 = stored2.replace(tzinfo=timezone.utc)
    assert stored2 == g2


# ── narrative extraction, hardened 2026-09-25 ─────────────────────────────────────────
# It parsed clean on GLM and failed on DeepSeek, which answered with the object wrapped
# in prose — so the live page rendered facts-only twice. A model that gave us the
# content and dressed it differently should not cost the page its narrative.


def test_narrative_survives_prose_around_the_object() -> None:
    n = sp.parse_narrative('Here is the status:\n\n{"standfirst": "ok", "blockers": [{"title": "t"}]}\n\nHope that helps.')
    assert n is not None and n["standfirst"] == "ok" and len(n["blockers"]) == 1


def test_narrative_survives_a_reasoning_scratchpad() -> None:
    assert sp.parse_narrative('<think>weighing it up</think>{"standfirst": "ok"}')["standfirst"] == "ok"
    assert sp.parse_narrative('<reasoning>hm</reasoning>\n{"standfirst": "b"}')["standfirst"] == "b"


def test_braces_inside_strings_do_not_end_the_object() -> None:
    n = sp.parse_narrative('{"standfirst": "nested {braces} in a \\"quote\\"", "environments": {"dev": "x"}}')
    assert n["environments"] == {"dev": "x"}
    assert "{braces}" in n["standfirst"]


def test_no_object_at_all_is_still_None() -> None:
    """The fallback must stay honest: no narrative means the page says so."""
    assert sp.parse_narrative("I could not read the cluster.") is None
    assert sp.parse_narrative("") is None
    assert sp.parse_narrative("[1, 2, 3]") is None


# ── day's diff: computed cleared list from stored snapshots ───────────────────────────


def test_cleared_facts_lists_envs_recovered_to_healthy() -> None:
    current = [
        {"name": "dev", "state": "healthy"},
        {"name": "freya", "state": "healthy"},
        {"name": "heimdall", "state": "unmeasured"},
    ]
    previous = [
        {"name": "dev", "state": "degraded"},
        {"name": "freya", "state": "healthy"},
        {"name": "heimdall", "state": "unmeasured"},
    ]
    # dev went from degraded to healthy; freya stayed healthy; heimdall is still unmeasured.
    assert sp.cleared_facts(current, previous) == ["dev"]


def test_cleared_facts_no_previous_snapshot_clears_nothing() -> None:
    assert sp.cleared_facts(sp.env_facts(SNAP), None) == []


def test_cleared_facts_lists_env_recovered_from_unmeasured_to_healthy() -> None:
    current = [{"name": "dev", "state": "healthy"}]
    previous = [{"name": "dev", "state": "unmeasured"}]
    # an environment the probe could not read last night that is healthy now counts as cleared.
    assert sp.cleared_facts(current, previous) == ["dev"]


def test_cleared_facts_purely_arithmetic_over_env_states() -> None:
    current = [
        {"name": "a", "state": "healthy"},
        {"name": "b", "state": "healthy"},
        {"name": "c", "state": "degraded"},
    ]
    previous = [
        {"name": "a", "state": "degraded"},
        {"name": "b", "state": "attention"},
        {"name": "c", "state": "healthy"},
    ]
    # a and b recovered to healthy; c went the other way and is not cleared.
    assert sp.cleared_facts(current, previous) == ["a", "b"]


def test_computed_cleared_lands_in_rendered_html() -> None:
    current = sp.env_facts(SNAP)
    html = sp.render_status_page(current, None, snapshot_when="2026-09-25T04:45:00Z", run_url="",
                                 generated_at=datetime(2026, 9, 25, 17, 0, tzinfo=timezone.utc),
                                 model_name=None, cleared=["dev"])
    assert "Cleared since last time" in html
    assert "<li>dev</li>" in html
    assert "2026-09-25" in html


def test_computed_cleared_empty_renders_no_block() -> None:
    html = sp.render_status_page(sp.env_facts(SNAP), None, snapshot_when="x", run_url="",
                                 generated_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
                                 model_name=None, cleared=[])
    assert "Cleared since last time" not in html


@pytest.mark.asyncio
async def test_status_snapshot_round_trips_through_the_real_db_layer(tmp_path, monkeypatch) -> None:
    """Upsert/fetch-prior against a real temp SQLite DB, mirroring the published-page test."""
    import sys

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    sys.modules.pop("db", None)
    import db as dbmod

    await dbmod.init_db()
    await dbmod.upsert_status_snapshot("2026-09-24", [{"name": "dev", "state": "degraded"}])
    await dbmod.upsert_status_snapshot("2026-09-25", [{"name": "dev", "state": "healthy"}])
    # re-running the same day overwrites, never its own baseline
    await dbmod.upsert_status_snapshot("2026-09-25", [{"name": "dev", "state": "healthy"}])
    prev = await dbmod.previous_status_snapshot("2026-09-25")
    assert prev == [{"name": "dev", "state": "degraded"}]
    assert await dbmod.previous_status_snapshot("2026-09-24") is None


def test_status_page_keep_days_defaults_safely_on_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer SUSAN_STATUS_PAGE_KEEP_DAYS must not crash; fall back to 30 and clamp."""
    monkeypatch.delenv("SUSAN_STATUS_PAGE_KEEP_DAYS", raising=False)
    assert sp.status_page_keep_days() == 30
    monkeypatch.setenv("SUSAN_STATUS_PAGE_KEEP_DAYS", "abc")
    assert sp.status_page_keep_days() == 30
    monkeypatch.setenv("SUSAN_STATUS_PAGE_KEEP_DAYS", "60")
    assert sp.status_page_keep_days() == 60
    monkeypatch.setenv("SUSAN_STATUS_PAGE_KEEP_DAYS", "1000")
    assert sp.status_page_keep_days() == 365
    monkeypatch.setenv("SUSAN_STATUS_PAGE_KEEP_DAYS", "0")
    assert sp.status_page_keep_days() == 1


@pytest.mark.asyncio
async def test_latest_published_page_prefers_stable_slug_on_a_tie(tmp_path, monkeypatch) -> None:
    """The stable env-status slug and a dated archive can be stamped with the same
    created_at within one run; on a tie the stable slug must win so /status/latest
    never resolves to an archive."""
    import sys

    from sqlalchemy import select

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    sys.modules.pop("db", None)
    import db as dbmod

    await dbmod.init_db()
    await dbmod.upsert_published_page("env-status", "env-status", "T", "<html>STABLE</html>")
    await dbmod.upsert_published_page("env-status-2026-09-24", "env-status", "T", "<html>ARCHIVE</html>")

    # stamp both with the same created_at so the secondary sort must decide
    same = datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc)
    async with dbmod.SessionLocal() as session:
        for row in (await session.execute(select(dbmod.PublishedPage))).scalars().all():
            row.created_at = same
        await session.commit()

    latest = await dbmod.latest_published_page("env-status")
    assert latest is not None and latest["slug"] == "env-status" and latest["html"] == "<html>STABLE</html>"


@pytest.mark.asyncio
async def test_previous_status_snapshot_rejects_non_list_of_dicts(tmp_path, monkeypatch) -> None:
    """A stored snapshot whose payload is valid JSON but not a list of dicts is treated
    as absent rather than crashing cleared_facts."""
    import sys

    from sqlalchemy import select

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    sys.modules.pop("db", None)
    import db as dbmod

    await dbmod.init_db()
    await dbmod.upsert_status_snapshot("2026-09-23", [{"name": "dev", "state": "healthy"}])
    assert await dbmod.previous_status_snapshot("2026-09-24") == [{"name": "dev", "state": "healthy"}]

    # valid JSON, but not a list of dicts
    async with dbmod.SessionLocal() as session:
        row = (await session.execute(select(dbmod.StatusSnapshot))).scalars().first()
        row.payload = '"not-a-list"'
        await session.commit()
    assert await dbmod.previous_status_snapshot("2026-09-24") is None

    # a JSON list whose elements are not dicts is rejected too
    async with dbmod.SessionLocal() as session:
        row = (await session.execute(select(dbmod.StatusSnapshot))).scalars().first()
        row.payload = '[1, 2, 3]'
        await session.commit()
    assert await dbmod.previous_status_snapshot("2026-09-24") is None


@pytest.mark.asyncio
async def test_status_stable_slug_serves_overwrite_in_place_latest(tmp_path, monkeypatch) -> None:
    """The stable env-status URL serves the stable slug's own latest content, overwritten
    in place each run, never a dated archive; /status/latest resolves to the newest
    env-status row. Drives the real route and the real DB layer."""
    import sys

    from fastapi.testclient import TestClient

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SUSAN_STATUS_PAGE_TOKEN", "tok")
    sys.modules.pop("db", None)
    import db as dbmod

    from app.routes import app

    await dbmod.init_db()
    # a first run writes the stable slug, an older run wrote the dated archive, and the
    # latest run overwrites the stable slug in place without duplicating it.
    await dbmod.upsert_published_page("env-status", "env-status", "T", "<html>FIRST</html>")
    await dbmod.upsert_published_page("env-status-2026-09-24", "env-status", "T", "<html>ARCHIVE</html>",
                                      created_at=datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc))
    await dbmod.upsert_published_page("env-status", "env-status", "T", "<html>SECOND</html>")

    c = TestClient(app)
    r = c.get("/status/env-status?k=tok")
    assert r.status_code == 200
    assert r.text == "<html>SECOND</html>"                       # overwritten in place, not stale
    rl = c.get("/status/latest?k=tok")
    assert rl.status_code == 200
    assert rl.text == "<html>SECOND</html>"                      # newest env-status row
    # the dated archive is reachable by its own slug but is NOT what the stable URL serves
    assert c.get("/status/env-status-2026-09-24?k=tok").text == "<html>ARCHIVE</html>"


@pytest.mark.asyncio
async def test_fetch_latest_status_snapshot_reads_the_selected_runs_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fetched snapshot's run id, status.json and status.html all come from the run
    that was selected, not from the first artifact or a different run's artifact."""
    runs_url = "https://api.github.com/repos/Frontier-One/cloud-infra/actions/workflows/env-nightly.yml/runs"
    run_b_artifacts_url = "https://api.github.com/repos/Frontier-One/cloud-infra/actions/runs/42/artifacts"
    run_b_zip_url = "https://api.github.com/repos/Frontier-One/cloud-infra/actions/artifacts/7/zip"

    run_b_json = {
        "generated": "2026-09-24T04:45:00Z",
        "environments": {
            "freya": {"apps": {"synced_healthy": 16, "drifted": 0, "progressing": 0, "degraded": 0, "missing": 0},
                      "clusters": {"control": "up", "asgard-cluster": "up"}},
        },
    }
    run_b_zip = _zip({"status.json": json.dumps(run_b_json), "status.html": "<html>RUN_B</html>"})

    class _FakeResponse:
        def __init__(self, payload: object) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> object:
            return self._payload

        @property
        def content(self) -> object:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    def resolve(url: str, params: object) -> _FakeResponse:
        if url == runs_url:
            return _FakeResponse({
                "workflow_runs": [{
                    "id": "RUN_B",
                    "html_url": "https://github.com/Frontier-One/cloud-infra/actions/runs/42",
                    "created_at": "2026-09-24T04:40:00Z",
                    "updated_at": "2026-09-24T04:52:00Z",
                    "artifacts_url": run_b_artifacts_url,
                }],
            })
        if url == run_b_artifacts_url:
            # an unrelated artifact is listed first; a buggy "first artifact" pick would
            # grab it, but the code filters by the artifact name.
            return _FakeResponse({
                "artifacts": [
                    {"name": "debug-bundle", "expired": False, "archive_download_url": "https://example/debug.zip"},
                    {"name": "environment-status-page", "expired": False, "archive_download_url": run_b_zip_url},
                ],
            })
        if url == run_b_zip_url:
            return _FakeResponse(run_b_zip)
        raise AssertionError(f"unexpected URL {url}")

    class _FakeClient:
        def __init__(self, resolver) -> None:
            self._resolver = resolver

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, headers: object = None, params: object = None) -> _FakeResponse:
            return self._resolver(url, params)

    monkeypatch.setattr(sp.httpx, "AsyncClient", lambda **kw: _FakeClient(resolve))

    snap = await sp.fetch_latest_status_snapshot("gh")
    assert snap["run_id"] == "RUN_B"                          # the selected run, not any other
    assert snap["run_url"] == "https://github.com/Frontier-One/cloud-infra/actions/runs/42"
    assert snap["run_created_at"] == "2026-09-24T04:40:00Z"
    assert snap["run_completed_at"] == "2026-09-24T04:52:00Z"
    assert snap["status_json"] == run_b_json                  # content of the selected run's artifact
    assert snap["status_html"] == "<html>RUN_B</html>"
