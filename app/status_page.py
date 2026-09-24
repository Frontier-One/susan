"""The environment status page, produced by Susan on a schedule and hosted by her.

Reproduces the hand-made "Frontier One — Environment Status" artifact (2026-09-21) with
our own models, on a schedule, from sources that already exist — and with the numbers
never touching a model:

  1. DATA: cloud-infra's nightly `Scheduled · Nightly (env acceptance matrix)` already
     probes every environment and uploads `status.json` + `status.html` as the
     `environment-status-page` workflow artifact (scripts/probe-env-health.sh →
     scripts/gen-status-page.py). Susan downloads the latest one. She never holds a
     cluster credential: the probe ran where the credentials live.
  2. CONTEXT for the prose: the alert channels' last N days and the open roadmap issues.
  3. NARRATIVE: our sovereign model writes standfirst, per-environment readings and the
     blockers list — as strict JSON, which is HTML-escaped on render. Every count and bar
     on the page is computed in Python from status.json; the model is not allowed to
     state a number the data does not carry.
  4. HOSTING: the HTML is stored in Susan's DB and served at GET /status/{slug} behind
     SUSAN_STATUS_PAGE_TOKEN (the page carries codewords and private addresses, so it is
     not public — cloud-infra #635's reason for refusing GitHub Pages). The Slack post
     carries the tokened link.

Skills (slash phrasing):
  /susan status page [--no-approval]
  /susan schedule add status page every monday at 09:00 in #team-tech
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from inspect import cleandoc
from typing import Any

import httpx

from app.claude_client import call_claude
from app.config import SUSAN_VOICE, logger
from app.github_http import search_issues
from app.slack_api import fetch_slack_channel_history_since, notify_user_ephemeral, post_message
from app.weekly_context import strip_weekly_status_auto_post_flags
from db import get_github_token, upsert_published_page

STATUS_REPO = "Frontier-One/cloud-infra"
STATUS_WORKFLOW_FILE = "env-nightly.yml"
STATUS_ARTIFACT_NAME = "environment-status-page"
PAGE_KIND = "env-status"

_STATUS_PREFIXES = ("environment status page", "environment status", "status page", "env status")


def parse_status_page_command(text: str) -> str | None:
    raw = (text or "").strip()
    lower = raw.lower()
    for p in _STATUS_PREFIXES:
        if lower == p:
            return ""
        if lower.startswith(p + " "):
            return raw[len(p):].strip()
    return None


def status_page_token() -> str:
    return (os.environ.get("SUSAN_STATUS_PAGE_TOKEN") or "").strip()


def public_base_url() -> str:
    return (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def page_url(slug: str) -> str:
    base = public_base_url() or "http://localhost:8000"
    tok = status_page_token()
    return f"{base}/status/{slug}" + (f"?k={tok}" if tok else "")


# ── 1. the nightly snapshot ────────────────────────────────────────────────────────────


def extract_snapshot(zip_bytes: bytes) -> dict[str, Any]:
    """status.json (parsed) and status.html (text) out of the artifact zip.

    A missing status.json is NOT an error here — the nightly omits it when the probe
    failed, and gen-status-page renders "not measured". A status.json that does not
    parse IS an error (the generator's rule 2): a broken probe must never read as data.
    """
    out: dict[str, Any] = {"status_json": None, "status_html": None}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = set(z.namelist())
        if "status.json" in names:
            out["status_json"] = json.loads(z.read("status.json").decode("utf-8"))
            if not isinstance(out["status_json"], dict) or "environments" not in out["status_json"]:
                raise ValueError("status.json is not the probe's shape ({generated, environments})")
        if "status.html" in names:
            out["status_html"] = z.read("status.html").decode("utf-8", errors="replace")
    return out


async def fetch_latest_status_snapshot(token: str) -> dict[str, Any]:
    """The newest successful nightly run's artifact, plus the run's URL and time."""
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(
            f"https://api.github.com/repos/{STATUS_REPO}/actions/workflows/{STATUS_WORKFLOW_FILE}/runs",
            headers=hdrs, params={"status": "success", "per_page": 1},
        )
        r.raise_for_status()
        runs = (r.json() or {}).get("workflow_runs") or []
        if not runs:
            raise RuntimeError("no successful env-nightly run found")
        run = runs[0]
        r = await client.get(run["artifacts_url"], headers=hdrs)
        r.raise_for_status()
        arts = [a for a in (r.json() or {}).get("artifacts") or [] if a.get("name") == STATUS_ARTIFACT_NAME]
        if not arts:
            raise RuntimeError(f"run {run.get('id')} has no `{STATUS_ARTIFACT_NAME}` artifact")
        if arts[0].get("expired"):
            raise RuntimeError("the latest status artifact has expired (30-day retention)")
        r = await client.get(arts[0]["archive_download_url"], headers=hdrs)
        r.raise_for_status()
        snap = extract_snapshot(r.content)
    snap["run_url"] = run.get("html_url", "")
    snap["run_created_at"] = run.get("created_at", "")
    return snap


# ── 2. facts computed in Python (never by the model) ───────────────────────────────────

_APP_BUCKETS = ("synced_healthy", "drifted", "progressing", "degraded", "missing")


def env_facts(status_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One row per declared environment: measured?, app tallies, cluster verdicts."""
    envs = (status_json or {}).get("environments") or {}
    rows: list[dict[str, Any]] = []
    for name in sorted(envs):
        e = envs.get(name) or {}
        apps = e.get("apps") if isinstance(e.get("apps"), dict) else None
        clusters = e.get("clusters") if isinstance(e.get("clusters"), dict) else {}
        measured = bool(apps) or bool(clusters)
        tally = {b: int((apps or {}).get(b) or 0) for b in _APP_BUCKETS}
        total = sum(tally.values())
        up = sum(1 for v in clusters.values() if v == "up")
        down = sum(1 for v in clusters.values() if v == "down")
        unknown = len(clusters) - up - down
        if not measured:
            state = "unmeasured"
        elif down or tally["degraded"] or unknown:
            state = "degraded"
        elif tally["drifted"] or tally["missing"] or tally["progressing"]:
            state = "attention"
        else:
            state = "healthy"
        rows.append({
            "name": name, "measured": measured, "apps": tally, "apps_total": total,
            "clusters": clusters, "clusters_up": up, "clusters_down": down, "clusters_unknown": unknown,
            "state": state,
        })
    return rows


def facts_for_prompt(rows: list[dict[str, Any]]) -> str:
    lines = []
    for r in rows:
        if not r["measured"]:
            lines.append(f"- {r['name']}: NOT MEASURED (probe produced nothing for this environment)")
            continue
        a = r["apps"]
        cl = ", ".join(f"{k}={v}" for k, v in sorted(r["clusters"].items())) or "(none listed)"
        lines.append(
            f"- {r['name']}: apps total={r['apps_total']} synced_healthy={a['synced_healthy']} "
            f"drifted={a['drifted']} progressing={a['progressing']} degraded={a['degraded']} "
            f"missing={a['missing']}; clusters up={r['clusters_up']} down={r['clusters_down']} "
            f"unknown={r['clusters_unknown']} [{cl}]"
        )
    return "\n".join(lines)


# ── 3. narrative from our model, as strict JSON ────────────────────────────────────────

_NARRATIVE_SCHEMA = cleandoc(
    """
    {
      "standfirst": "one or two sentences: what this page is and the single most important reading right now",
      "environments": {"<env name>": "one sentence reading of that environment, grounded in the facts and the alerts"},
      "blockers": [
        {"severity": "crit|warn", "title": "one line, the consequence not the mechanism",
         "ref": "issue/PR refs like #1563, or 'unfiled'", "body": "2-4 sentences: what is true, why it matters, what unblocks it"}
      ],
      "cleared": ["things that were red last week and are verifiably fixed now, one line each"],
      "watch": ["one-line items that are not blockers yet but a lead should know"]
    }
    """
)


def _narrative_system_prompt() -> str:
    return cleandoc(
        f"""
        You are Susan. {SUSAN_VOICE}
        You write the prose of Frontier One's internal environment status page. The page
        is read by the whole company, including people who were not in any incident.

        You are given: (a) the measured facts per environment — app tallies and cluster
        reachability from last night's probe — (b) the alert channels' recent traffic,
        (c) the open roadmap issues. Write ONLY what these support.

        Hard rules:
        - Output ONE JSON object matching the schema you are given. No prose outside it,
          no markdown fences, no HTML.
        - Never state a number that is not in the facts. Counts, percentages and dates on
          the page are rendered from the data by code, not by you.
        - Codewords only (dev, freya, heimdall…). Never a customer name. Never an IP.
        - A blocker is something that would change what the team does this week if fixed.
          Order by what would change most. Severity crit = losing something we cannot get
          back, a customer-visible outage, or a security boundary open; warn = everything
          else worth a lead's attention. Cite the issue or PR when the alerts or issues
          give one; say "unfiled" when nothing owns it.
        - "cleared" only for things the sources show as fixed, not things merely quiet.
        - If the facts for an environment are NOT MEASURED, say so plainly; never infer health.
        - If the sources are thin, write less. An honest short page beats a padded one.
        """
    )


def parse_narrative(text: str) -> dict[str, Any] | None:
    """Strict JSON, tolerant only of a stray fence. None when it does not parse."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    try:
        d = json.loads(s)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    d.setdefault("standfirst", "")
    d["environments"] = d.get("environments") if isinstance(d.get("environments"), dict) else {}
    d["blockers"] = [b for b in (d.get("blockers") or []) if isinstance(b, dict)]
    d["cleared"] = [str(x) for x in (d.get("cleared") or []) if isinstance(x, (str, int, float))]
    d["watch"] = [str(x) for x in (d.get("watch") or []) if isinstance(x, (str, int, float))]
    return d


def _narrative_lookback_days() -> int:
    n = int((os.environ.get("SUSAN_STATUS_PAGE_LOOKBACK_DAYS") or "7").strip() or "7")
    return max(1, min(30, n))


async def gather_context(user: str, token: str) -> tuple[str, str]:
    """(alert channel digest, open roadmap issues) for the lookback window — best-effort."""
    from app.channel_surface import resolve_alert_channel_ids

    days = _narrative_lookback_days()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    oldest = f"{int(since.timestamp())}.000000"
    alerts_parts: list[str] = []
    try:
        ids, _missing = await resolve_alert_channel_ids()
        for cid in ids[:4]:
            try:
                blob = await fetch_slack_channel_history_since(cid, oldest, user)
                alerts_parts.append(f"## <#{cid}>\n{blob}")
            except Exception as e:  # one dead channel must not kill the page
                logger.warning("status page: alert channel %s unreadable: %s", cid, e)
    except Exception as e:
        logger.warning("status page: alert channels unresolved: %s", e)
    alerts = "\n\n".join(alerts_parts) or "(no alert channel traffic readable)"

    issues_txt = "(no roadmap issues readable)"
    try:
        q = f"org:{STATUS_REPO.split('/')[0]} is:issue is:open label:roadmap updated:>={since.date().isoformat()}"
        items = await search_issues(q, token, max_pages=2)
        lines = []
        for it in items[:60]:
            repo = (it.get("repository_url") or "").rsplit("/", 2)[-2:]
            ref = f"{'/'.join(repo)}#{it.get('number')}" if len(repo) == 2 else f"#{it.get('number')}"
            lines.append(f"- {ref} [{(it.get('updated_at') or '')[:10]}] {it.get('title', '')}")
        if lines:
            issues_txt = "\n".join(lines)
    except Exception as e:
        logger.warning("status page: roadmap issues unreadable: %s", e)
    return alerts, issues_txt


def _max_tokens() -> int:
    n = int((os.environ.get("SUSAN_STATUS_PAGE_MAX_TOKENS") or "6000").strip() or "6000")
    return max(1500, min(16000, n))


async def build_narrative(rows: list[dict[str, Any]], alerts: str, issues: str, snapshot_when: str):
    user_prompt = (
        f"Snapshot: last night's probe, run {snapshot_when or '(time unknown)'}.\n\n"
        f"### Measured facts (authoritative — do not restate numbers, code renders them)\n{facts_for_prompt(rows)}\n\n"
        f"### Alert channels, last {_narrative_lookback_days()} days\n{alerts[:60000]}\n\n"
        f"### Open roadmap issues updated in the window\n{issues[:12000]}\n\n"
        f"### Output schema\n{_NARRATIVE_SCHEMA}"
    )
    # Our own models, by design: this page is the sovereign-route showcase.
    return await call_claude(
        _narrative_system_prompt(), user_prompt, max_tokens=_max_tokens(),
        action="status_page", model_route="sovereign",
    )


# ── 4. render ─────────────────────────────────────────────────────────────────────────

_CSS = """
:root{--ground:#F3F5F7;--raised:#FFFFFF;--sunk:#E8ECEF;--ink:#16202B;--ink-soft:#3D4B59;--neutral:#5A6875;--neutral-dim:#8D98A4;--line:#DCE2E8;--line-soft:#E9EDF1;--accent:#1F5F7A;--accent-wash:#E4EEF3;--ok:#2E7D5B;--ok-wash:#E1EFE8;--warn:#B0781F;--warn-wash:#F6EEDA;--crit:#A93B32;--crit-wash:#F7E4E2;--idle:#6B7684;--idle-wash:#E5E9ED;--serif:"Newsreader","Iowan Old Style",Palatino,Georgia,serif;--sans:"Inter",ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;--mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#0D151E;--raised:#151E29;--sunk:#101924;--ink:#E4E9EE;--ink-soft:#BAC4CE;--neutral:#93A0AD;--neutral-dim:#6C7885;--line:#26323F;--line-soft:#1D2733;--accent:#68B2CB;--accent-wash:#132E39;--ok:#5FBF8F;--ok-wash:#13301F;--warn:#DBA94A;--warn-wash:#33260D;--crit:#E1706A;--crit-wash:#351917;--idle:#8A96A3;--idle-wash:#1C2530}}
:root[data-theme="dark"]{--ground:#0D151E;--raised:#151E29;--sunk:#101924;--ink:#E4E9EE;--ink-soft:#BAC4CE;--neutral:#93A0AD;--neutral-dim:#6C7885;--line:#26323F;--line-soft:#1D2733;--accent:#68B2CB;--accent-wash:#132E39;--ok:#5FBF8F;--ok-wash:#13301F;--warn:#DBA94A;--warn-wash:#33260D;--crit:#E1706A;--crit-wash:#351917;--idle:#8A96A3;--idle-wash:#1C2530}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:52px 20px 88px}.masthead{border-bottom:2px solid var(--ink);padding-bottom:20px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--neutral);margin:0 0 14px}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(30px,5vw,44px);line-height:1.08;letter-spacing:-.015em;margin:0;text-wrap:balance}
.standfirst{font-size:16px;color:var(--ink-soft);max-width:62ch;margin:16px 0 0}.stamp{display:flex;flex-wrap:wrap;gap:6px 22px;margin-top:22px;font-family:var(--mono);font-size:11.5px;color:var(--neutral)}.stamp b{color:var(--ink-soft);font-weight:600}
section{margin-top:50px}h2{font-family:var(--serif);font-size:22px;font-weight:600;letter-spacing:-.01em;margin:0 0 4px}.sub{color:var(--neutral);font-size:13.5px;margin:0 0 22px;max-width:76ch}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:16px}.card{background:var(--raised);border:1px solid var(--line);border-radius:3px;overflow:hidden;display:flex;flex-direction:column}
.stripe{height:3px;background:var(--idle)}.stripe.ok{background:var(--ok)}.stripe.warn{background:var(--warn)}.stripe.crit{background:var(--crit)}
.card-body{padding:20px;display:flex;flex-direction:column;gap:14px;flex:1}.card-top{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.env-name{font-family:var(--serif);font-size:27px;font-weight:600;letter-spacing:-.015em}
.pill{display:inline-flex;align-items:center;gap:6px;align-self:flex-start;font-family:var(--mono);font-size:11px;letter-spacing:.07em;text-transform:uppercase;font-weight:600;padding:4px 9px;border-radius:2px}
.pill.ok{background:var(--ok-wash);color:var(--ok)}.pill.warn{background:var(--warn-wash);color:var(--warn)}.pill.crit{background:var(--crit-wash);color:var(--crit)}.pill.idle{background:var(--idle-wash);color:var(--idle)}.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.headline-num{font-family:var(--mono);font-size:13px;color:var(--ink-soft);display:flex;align-items:baseline;gap:8px}.headline-num b{font-family:var(--serif);font-size:31px;font-weight:600;color:var(--ink);line-height:1}
.bar{display:flex;height:8px;border-radius:2px;overflow:hidden;background:var(--line-soft)}.bar span{display:block}.seg-ok{background:var(--ok)}.seg-drift{background:var(--accent)}.seg-deg{background:var(--crit)}.seg-miss{background:var(--neutral-dim)}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-family:var(--mono);font-size:11px;color:var(--neutral)}.legend i{font-style:normal;display:inline-flex;align-items:center;gap:5px}.legend i::before{content:"";width:8px;height:8px;border-radius:1px}.l-ok::before{background:var(--ok)}.l-drift::before{background:var(--accent)}.l-deg::before{background:var(--crit)}.l-miss::before{background:var(--neutral-dim)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:13px;margin:0}.kv dt{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--neutral);padding-top:2px}.kv dd{margin:0;color:var(--ink-soft)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:3px;background:var(--raised)}table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:520px}th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line-soft);vertical-align:top}
thead th{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--neutral);font-weight:600;background:var(--sunk);border-bottom:1px solid var(--line)}tbody tr:last-child td{border-bottom:0}td.mono{font-family:var(--mono);font-size:12.5px}
.tag{display:inline-block;font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;padding:2px 6px;border-radius:2px;font-weight:600}.tag.up{background:var(--ok-wash);color:var(--ok)}.tag.down{background:var(--crit-wash);color:var(--crit)}.tag.unk{background:var(--warn-wash);color:var(--warn)}
.blockers{display:grid;gap:10px}.blocker{background:var(--raised);border:1px solid var(--line);border-left:3px solid var(--neutral-dim);border-radius:3px;padding:15px 18px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 16px;align-items:start}
.blocker.crit{border-left-color:var(--crit)}.blocker.warn{border-left-color:var(--warn)}.blocker h3{margin:0;font-size:14.5px;font-weight:600;line-height:1.45}.blocker p{margin:0;grid-column:1/-1;font-size:13px;color:var(--ink-soft);max-width:86ch}
.ref{font-family:var(--mono);font-size:11.5px;color:var(--neutral);white-space:nowrap;border:1px solid var(--line);border-radius:2px;padding:2px 7px}
.cleared{background:var(--ok-wash);border:1px solid var(--line);border-radius:3px;padding:15px 20px;font-size:13.5px;color:var(--ink-soft);margin-bottom:20px}.cleared ul{margin:8px 0 0;padding-left:20px}
.note{background:var(--accent-wash);border:1px solid var(--line);border-radius:3px;padding:16px 20px;font-size:13.5px;color:var(--ink-soft)}
footer{margin-top:54px;padding-top:20px;border-top:1px solid var(--line);font-size:12.5px;color:var(--neutral)}footer p{margin:0 0 9px;max-width:86ch}a{color:var(--accent)}
"""


def _pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.1f}%" if total else "0%"


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


_STATE_UI = {
    "healthy": ("ok", "ok", "Live · healthy"),
    "attention": ("warn", "warn", "Live · needs attention"),
    "degraded": ("crit", "crit", "Live · degraded"),
    "unmeasured": ("", "idle", "Not measured"),
}


def render_env_card(row: dict[str, Any], reading: str) -> str:
    stripe, pill, label = _STATE_UI[row["state"]]
    a, total = row["apps"], row["apps_total"]
    if not row["measured"]:
        body = (
            '<div class="headline-num"><b>—</b><span>no probe result</span></div>'
            '<p class="sub" style="margin:0">The nightly probe produced nothing for this environment. '
            'That is a statement about the probe, not about the environment: neither healthy nor broken can be claimed.</p>'
        )
    else:
        clean = a["synced_healthy"]
        body = (
            f'<div class="headline-num"><b>{clean}</b><span>of {total} apps clean</span></div>'
            f'<div class="bar" role="img" aria-label="{clean} clean, {a["drifted"] + a["progressing"]} drifted or progressing, {a["degraded"]} degraded, {a["missing"]} missing">'
            f'<span class="seg-ok" style="width:{_pct(clean, total)}"></span>'
            f'<span class="seg-drift" style="width:{_pct(a["drifted"] + a["progressing"], total)}"></span>'
            f'<span class="seg-deg" style="width:{_pct(a["degraded"], total)}"></span>'
            f'<span class="seg-miss" style="width:{_pct(a["missing"], total)}"></span></div>'
            f'<div class="legend"><i class="l-ok">{clean} clean</i><i class="l-drift">{a["drifted"]} drifted · {a["progressing"]} progressing</i>'
            f'<i class="l-deg">{a["degraded"]} degraded</i><i class="l-miss">{a["missing"]} missing</i></div>'
            f'<dl class="kv"><dt>Clusters</dt><dd>{row["clusters_up"]} up · {row["clusters_down"]} down · {row["clusters_unknown"]} unknown</dd></dl>'
        )
    reading_html = f'<p class="sub" style="margin:0">{_esc(reading)}</p>' if reading else ""
    return (
        f'<article class="card"><div class="stripe {stripe}"></div><div class="card-body">'
        f'<div class="card-top"><span class="env-name">{_esc(row["name"])}</span></div>'
        f'<span class="pill {pill}">{label}</span>{body}{reading_html}</div></article>'
    )


def render_cluster_table(rows: list[dict[str, Any]]) -> str:
    trs = []
    for r in rows:
        for name, verdict in sorted(r["clusters"].items()):
            tag = {"up": "up", "down": "down"}.get(verdict, "unk")
            trs.append(f'<tr><td class="mono">{_esc(r["name"])}</td><td class="mono">{_esc(name)}</td>'
                       f'<td><span class="tag {tag}">{_esc(verdict)}</span></td></tr>')
    if not trs:
        return '<p class="sub">No cluster reachability in this snapshot.</p>'
    return ('<div class="scroll"><table><thead><tr><th>Environment</th><th>Cluster</th><th>Reachable</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>')


def render_status_page(
    rows: list[dict[str, Any]], narrative: dict[str, Any] | None, *,
    snapshot_when: str, run_url: str, generated_at: datetime, model_name: str | None,
) -> str:
    n = narrative or {}
    readings = n.get("environments") or {}
    cards = "".join(render_env_card(r, str(readings.get(r["name"], ""))) for r in rows)
    measured = [r for r in rows if r["measured"]]
    total_apps = sum(r["apps_total"] for r in measured)
    clean_apps = sum(r["apps"]["synced_healthy"] for r in measured)
    clusters = sum(len(r["clusters"]) for r in measured)
    up = sum(r["clusters_up"] for r in measured)

    blockers = "".join(
        f'<div class="blocker {"crit" if b.get("severity") == "crit" else "warn"}"><h3>{_esc(b.get("title"))}</h3>'
        f'<span class="ref">{_esc(b.get("ref") or "unfiled")}</span><p>{_esc(b.get("body"))}</p></div>'
        for b in (n.get("blockers") or [])
    )
    cleared = "".join(f"<li>{_esc(c)}</li>" for c in (n.get("cleared") or []))
    watch = "".join(f"<li>{_esc(w)}</li>" for w in (n.get("watch") or []))
    narrative_note = "" if narrative else (
        '<div class="note"><b>Narrative unavailable this run.</b> The model did not return a readable '
        'narrative, so this page carries the measured facts only. Nothing below is inferred.</div>'
    )
    standfirst = _esc(n.get("standfirst") or "Every environment we run and whether it was healthy at last night's probe. "
                      "Environments are named by codeword; the customer behind each codeword is deliberately not on this page.")
    who = f"narrative by <b>{_esc(model_name)}</b> (sovereign route)" if model_name else "narrative unavailable"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Frontier One — Environment Status</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>{_CSS}</style></head><body><div class="wrap">
<header class="masthead"><p class="eyebrow">Frontier One · Internal · generated by Susan</p><h1>Environment status</h1>
<p class="standfirst">{standfirst}</p>
<div class="stamp"><span><b>Snapshot</b> {_esc(snapshot_when or "unknown")}</span><span><b>Environments</b> {len(rows)} ({len(measured)} measured)</span>
<span><b>Apps clean</b> {clean_apps} of {total_apps}</span><span><b>Clusters reachable</b> {up} of {clusters}</span><span><b>Page built</b> {generated_at.strftime("%Y-%m-%d %H:%M UTC")}</span></div></header>
{narrative_note}
<section><h2>The environments</h2><p class="sub">App tallies and cluster reachability are last night's probe, rendered by code. An environment the probe could not read is shown as not measured, never as healthy.</p>
<div class="cards">{cards}</div></section>
<section><h2>Cluster reachability</h2>{render_cluster_table(rows)}</section>
<section><h2>What is holding us back</h2>
{f'<div class="cleared"><b>Cleared since last time.</b><ul>{cleared}</ul></div>' if cleared else ''}
<p class="sub">Ordered by what would change most if it were fixed. Written from the alert channels and the open roadmap issues of the last {_narrative_lookback_days()} days.</p>
<div class="blockers">{blockers or '<p class="sub">No blockers named this run.</p>'}</div>
{f'<h2 style="margin-top:34px">Worth watching</h2><ul class="sub">{watch}</ul>' if watch else ''}
</section>
<footer><p>Facts: cloud-infra <code>env-nightly</code> artifact <code>environment-status-page</code> (scripts/probe-env-health.sh → status.json){f' — <a href="{_esc(run_url)}">run</a>' if run_url else ''}. Numbers, bars and reachability are computed from that file; {who}.</p>
<p>Codewords only. Private addresses only. If this page and a cluster disagree, the cluster is right and the probe is the bug.</p></footer>
</div></body></html>"""


# ── 5. the command ────────────────────────────────────────────────────────────────────


def _slug(now: datetime) -> str:
    return "env-" + now.strftime("%Y-%m-%d-%H%M")


def slack_summary(rows: list[dict[str, Any]], narrative: dict[str, Any] | None, url: str) -> str:
    parts = []
    for r in rows:
        if not r["measured"]:
            parts.append(f"*{r['name']}* not measured")
        else:
            parts.append(f"*{r['name']}* {r['apps']['synced_healthy']}/{r['apps_total']} apps clean · {r['clusters_up']}/{len(r['clusters'])} clusters up")
    n_block = len((narrative or {}).get("blockers") or [])
    head = ((narrative or {}).get("standfirst") or "").strip()
    return (
        "*Environment status* — " + " · ".join(parts)
        + (f"\n{head}" if head else "")
        + f"\n{n_block} blocker(s) named. Full page: <{url}|environment status>"
    )


async def process_status_page(
    command_text: str, channel: str, user: str, thread_ts: str | None, response_url: str | None,
    *, auto_publish: bool = False,
) -> None:
    _remainder, auto_flag = strip_weekly_status_auto_post_flags(parse_status_page_command(command_text) or "")
    auto_publish = auto_publish or auto_flag

    if not status_page_token():
        await notify_user_ephemeral(channel, user,
            "The status page is not enabled: set `SUSAN_STATUS_PAGE_TOKEN` on the server (the page carries "
            "codewords and private addresses, so it is served only behind that token).", None, response_url)
        return
    try:
        token = await get_github_token(user)
    except Exception as e:
        await notify_user_ephemeral(channel, user, f"GitHub is not connected ({e}). Run `/susan connect github`.", None, response_url)
        return

    try:
        snap = await fetch_latest_status_snapshot(token)
    except Exception as e:
        logger.exception("status page: snapshot fetch failed")
        await notify_user_ephemeral(channel, user, f"Could not fetch last night's status snapshot from {STATUS_REPO}: {e}", None, response_url)
        return

    rows = env_facts(snap.get("status_json"))
    when = (snap.get("status_json") or {}).get("generated") or snap.get("run_created_at") or ""
    alerts, issues = await gather_context(user, token)

    narrative = None
    model_name = None
    model_route = None
    try:
        completion = await build_narrative(rows, alerts, issues, when)
        narrative = parse_narrative(str(completion))
        model_name = getattr(completion, "model_name", None)
        model_route = getattr(completion, "model_route", None)
        if narrative is None:
            logger.warning("status page: narrative did not parse as JSON; rendering facts only")
    except Exception as e:
        logger.exception("status page: narrative failed: %s", e)

    now = datetime.now(timezone.utc)
    page = render_status_page(rows, narrative, snapshot_when=str(when), run_url=snap.get("run_url") or "",
                              generated_at=now, model_name=model_name if narrative else None)
    slug = _slug(now)
    await upsert_published_page(slug, PAGE_KIND, "Frontier One — Environment Status", page,
                                model_route=model_route, model_name=model_name)
    url = page_url(slug)
    text = slack_summary(rows, narrative, url)
    if auto_publish:
        await post_message(channel, text, thread_ts=thread_ts, model_route=model_route, model_name=model_name)
        await notify_user_ephemeral(channel, user, f"✓ Posted the environment status. Latest is always at {page_url('latest')}", None, response_url)
    else:
        await notify_user_ephemeral(channel, user, text + "\n\n_Only you can see this. Post it with `/susan status page --no-approval`, "
                                    "or schedule it with `/susan schedule add status page every monday at 09:00 in #team-tech`._",
                                    None, response_url)
