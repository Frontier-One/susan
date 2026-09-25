"""Weekly team/repo status (Slack + optional GitHub + Drive)."""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
from collections import Counter
from inspect import cleandoc

from db import (
    create_user_draft,
    get_github_token,
    get_granola_token,
    previous_metric_snapshot,
    upsert_metric_snapshot,
    user_has_granola_tokens,
)

from app.claude_client import ModelCompletion, call_claude
from app.config import ACTIONS, SUSAN_VOICE, logger
from app.github_http import (
    _pr_turnaround_hours,
    fetch_dependabot_alert_stats,
    fetch_merged_prs_for_repo_range,
    fetch_opened_prs_for_repo_range,
    fetch_pr_human_signals,
)
from app.slack_api import (
    fetch_slack_channel_history_since,
    notify_user_ephemeral,
    slack_channel_bookmarks_for_weekly,
)
from app.engineering_metrics import compute as compute_engineering_metrics
from app.granola_summarize import collect_granola_notes_for_window
from app.engineering_metrics import observations_prompt as metrics_observations_prompt
from app.engineering_metrics import pr_key as _pr_key
from app.engineering_metrics import render as render_engineering_metrics
from app.weekly_canvas import publish_weekly_status
from app.weekly_context import parse_weekly_status_time_range, utc_date_start_slack_ts
from app.weekly_drive import weekly_status_drive_activity_block

_WEEKLY_SLACK_MRKDN = cleandoc(
    """
    Slack Block Kit *mrkdwn* rules (not GitHub/CommonMark): use *single asterisks* for bold
    only — never **double-asterisk** bold. Do not use # or ## headings. Links must use
    Slack syntax: <https://example.com/path|short visible label>. Prefer real URLs from
    the prompt (GitHub PR/issue links, Google Doc/Drive links, bookmark links); do not invent URLs.
    Group related people with `<@U123>` mentions on the same theme line.
    """
)

_WEEKLY_ATTRIBUTION = cleandoc(
    """
    **Attribution rules** (who gets credit on each theme line):
    - **GitHub:** credit the **PR author/opener** (`@login` on each PR line in the data).
      Do **not** credit reviewers, commenters, or merge-by users unless they also opened PRs on that theme.
    - **Google Drive:** credit the **owner or last editor** shown on each Drive line (`by owner: …`).
      Do **not** credit people who only commented in Slack about a doc.
    - Include **documents shared in Slack this week** (the “Documents shared in Slack” section) in the
      relevant theme — even if the doc was not modified recently.
    - **Slack:** use `<@U…>` for people who posted substantive updates or decisions in the transcript.
    - Map GitHub `@login` or Drive names to `<@U…>` only when the same person clearly appears in the Slack transcript.
    """
)

_WEEKLY_STRUCTURE = cleandoc(
    """
    **Output shape** — tight, founder-ready. Jesse forwards this as-is and reads it in
    under a minute; it must fit on one Slack screen without scrolling.

    - First line: *Tech team update — <plain-language reporting window>*
      (or *Weekly update — …* for non-engineering channels).

    - **3–5 theme sections**, ordered by business impact. A theme is an initiative a
      non-engineer recognises (*Customer pilots*, *Inference platform*, *Reliability*),
      never a repository or a person.

    - Each section is exactly:
      *<Theme>* — <@U…> @login (only the people who shipped it)
      • *Shipped:* one or two bullets, outcome first, one sentence each. Say what
        changed for a customer, a pilot, cost, or risk — not what was edited. PRs are
        a count (*9 PRs*) with at most one link when a single change is the story.
      • *Blocked / at risk:* one bullet, only if real, naming who or what it waits on.
      • *Next:* one bullet, the single most important thing, with an owner.

    - Then *Decisions this week:* — semicolon-separated, each with who took it. This
      section is rendered as its own list, so put every decision in it and nowhere else.

      *Gather decisions from ALL FOUR sources, not just Slack:*
      1. **Meetings** — the Granola summaries block. Standups and team calls are where
         most decisions are actually taken, and they are usually stated once and never
         written down anywhere else. Read every meeting in the window, not just standup.
      2. **Slack** — a clear decision point in the transcript: someone says what will
         happen and nobody reopens it. Not a proposal, not a question, not a preference.
      3. **GitHub** — a merged PR or issue that RECORDS a decision: a decision-register
         entry (`D-F<n>`, `D-<name><n>`), a title or body saying *decided / DECIDED /
         we will / operator decision*, a policy or `docs/plans/**` change that settles
         an open question. A routine feature merge is not a decision.
      4. **The roadmap board** — an item moved to a decided state, or a decision issue.

      Rules: one line each, the decision then who took it. Deduplicate across sources —
      a decision taken in standup and then merged as a PR is ONE decision, cite the PR.
      Omit the section entirely if there were none; never write "none recorded".

    - Hard limits: **350–550 words total**, no more than 12 bullets across the whole
      update, no section longer than 5 lines. If you are over, cut the least
      consequential theme entirely rather than shortening every bullet into mush.

    - Leave out: dependency bumps, refactors, CI plumbing, doc edits, anything already
      in last week's update unless its status changed, and every number that does not
      change what a reader would do. No GitHub snapshot line, no author leaderboard.

    - Close with nothing that says the message is a private draft or ephemeral.
    """
)


def _weekly_meeting_cap() -> int:
    n = int((os.environ.get("WEEKLY_STATUS_MAX_MEETINGS") or "12").strip() or "12")
    return max(1, min(40, n))


def _weekly_meeting_chars() -> int:
    n = int((os.environ.get("WEEKLY_STATUS_MEETING_CHARS") or "2500").strip() or "2500")
    return max(400, min(20_000, n))


def _weekly_status_title_line(
    repos: list[str], range_label: str, *, include_github: bool
) -> str:
    if not include_github:
        return f"Weekly status — {range_label} — Slack"
    if len(repos) == 1:
        return f"Weekly status — {range_label} — `{repos[0]}`"
    shown = ", ".join(f"`{r}`" for r in repos[:5])
    if len(repos) > 5:
        shown += f", … (+{len(repos) - 5} more)"
    return f"Weekly status — {range_label} — {shown}"


async def process_weekly_status(
    repos: list[str],
    command_text: str,
    hist_channel: str,
    channel: str,
    user: str,
    thread_ts: str | None,
    response_url: str | None,
    *,
    include_github: bool,
    auto_publish: bool = False,
) -> None:
    since_d, until_d, range_label = parse_weekly_status_time_range(command_text)
    oldest_ts = utc_date_start_slack_ts(since_d)
    try:
        slack_digest = await fetch_slack_channel_history_since(
            hist_channel, oldest_ts, user
        )
    except Exception as e:
        logger.exception("Weekly status Slack fetch failed")
        await notify_user_ephemeral(
            channel, user, f"Susan error (Slack): {e}", None, response_url
        )
        return

    bookmark_google, bookmark_md = await slack_channel_bookmarks_for_weekly(hist_channel)
    drive_block = await weekly_status_drive_activity_block(
        user,
        since_d,
        until_d,
        slack_digest,
        extra_google_urls=bookmark_google,
    )
    bookmark_section = f"\n---\n{bookmark_md}\n" if bookmark_md else ""
    # Set by the GitHub branch below; the Slack-only branch has no PR data to count.
    metrics_block = ""

    # ── meetings: where decisions are actually taken ───────────────────────────────
    # The weekly read Slack, GitHub and Drive and NOT Granola, so "Decisions" was only
    # as good as whatever happened to be typed in a channel. Standups and team calls
    # are where most of them are made, so the whole window's meetings go in — not just
    # the standup. Best-effort: no Granola, no block, and the update still runs.
    meetings_block = ""
    try:
        if await user_has_granola_tokens(user):
            notes = await collect_granola_notes_for_window(
                await get_granola_token(user), since_d, until_d
            )
            if notes:
                parts = []
                for n in notes[: _weekly_meeting_cap()]:
                    body = (n.get("summary_markdown") or n.get("summary_text") or "").strip()
                    if not body:
                        continue
                    parts.append(
                        f"#### {n.get('title') or '(untitled)'} — {(n.get('created_at') or '')[:10]}\n"
                        f"{body[: _weekly_meeting_chars()]}"
                    )
                if parts:
                    meetings_block = (
                        "\n---\n### Meetings this window (Granola summaries — a primary "
                        "source for Decisions)\n" + "\n\n".join(parts) + "\n"
                    )
    except Exception as e:
        logger.warning("Weekly status Granola meetings unavailable: %s", e)

    if include_github:
        if not repos:
            await notify_user_ephemeral(
                channel, user, "No repositories configured.", None, response_url
            )
            return

        github_error: str | None = None
        per_repo: list[tuple[str, list[dict], list[dict], dict]] = []
        try:
            token = await get_github_token(user)

            async def one_repo(r: str) -> tuple[str, list[dict], list[dict], dict]:
                merged, opened, dep = await asyncio.gather(
                    fetch_merged_prs_for_repo_range(r, since_d, until_d, token),
                    fetch_opened_prs_for_repo_range(r, since_d, until_d, token),
                    fetch_dependabot_alert_stats(r, since_d, until_d, token),
                )
                return r, merged, opened, dep

            per_repo = await asyncio.gather(*[one_repo(r) for r in repos])
        except Exception as e:
            logger.exception("Weekly status GitHub fetch failed")
            github_error = str(e)
            logger.warning(
                "Weekly status continuing without GitHub enrichment: %s",
                github_error,
            )

        github_sections: list[str] = []
        if github_error:
            github_sections.append(
                "### GitHub enrichment unavailable\n"
                f"GitHub could not be queried for this run: {github_error}\n"
                "Generate the weekly update from Slack, bookmarks, and Drive; mention this "
                "source gap briefly and do not infer PR metrics."
            )
        for r, merged, opened, dep in per_repo:
            authors = Counter()
            hours: list[float] = []
            titles: list[str] = []
            for it in merged:
                login = (it.get("user") or {}).get("login") or "?"
                authors[login] += 1
                th = _pr_turnaround_hours(it)
                if th is not None:
                    hours.append(th)
                titles.append((it.get("title") or "").replace("\n", " "))
            avg_h = sum(hours) / len(hours) if hours else None
            top_authors = authors.most_common(6)
            if dep.get("ok"):
                dep_lines = (
                    f"Dependabot: {dep['open_total']} open alerts now; "
                    f"fixed in window {dep['fixed_in_window']}, dismissed in window "
                    f"{dep['dismissed_in_window']}, newly opened in window {dep['new_open_in_window']}."
                )
            else:
                dep_lines = f"Dependabot: unavailable — {dep.get('hint', 'unknown')}"

            merged_lines: list[str] = []
            for it in merged[:45]:
                num = it.get("number")
                pr_title = ((it.get("title") or "") or "").replace("\n", " ")[:220]
                url = ((it.get("html_url") or "") or "").strip()
                login = (it.get("user") or {}).get("login") or "?"
                if url:
                    merged_lines.append(f"- {url} — #{num} opener=@{login} | {pr_title}")
                elif num is not None:
                    merged_lines.append(f"- #{num} opener=@{login} | {pr_title}")
            opened_lines: list[str] = []
            for x in opened[:30]:
                num = x.get("number")
                t = ((x.get("title") or "") or "").replace("\n", " ")[:220]
                url = ((x.get("html_url") or "") or "").strip()
                login = (x.get("user") or {}).get("login") or "?"
                if url:
                    opened_lines.append(f"- {url} — #{num} opener=@{login} | {t}")
                elif num is not None:
                    opened_lines.append(f"- #{num} opener=@{login} | {t}")
            avg_part = (
                f"{avg_h:.1f}"
                if avg_h is not None
                else "n/a (no merged PRs with created+merged timestamps)"
            )
            merged_block = "\n".join(merged_lines) if merged_lines else "(none)"
            opened_block = "\n".join(opened_lines) if opened_lines else "(none)"
            github_sections.append(
                f"### `{r}`\n"
                f"{dep_lines}\n"
                f"PRs opened in window: {len(opened)}; merged in window: {len(merged)}.\n"
                f"Average merge turnaround (hours): {avg_part}.\n"
                f"Top merged-PR authors (openers — use for attribution, not reviewers): "
                f"{', '.join(f'@{a} ({c})' for a, c in top_authors) or 'none'}.\n"
                f"Merged PRs (titles also as quick scan): {'; '.join(titles[:50])}\n"
                f"Merged PRs with `html_url` (use for Slack links):\n{merged_block}\n"
                f"Opened PRs with `html_url`:\n{opened_block}\n"
            )

        facts = "\n\n".join(github_sections)

        # ── team-level engineering metrics: arithmetic, then a stored snapshot ──────
        # Computed here and rendered in Python because a model that can restate a
        # number can restate it wrong, and because a week-over-week delta the model
        # remembers is a delta the model invents. See app/engineering_metrics.py for
        # why these metrics and not others (DORA's keys, and its warnings).
        try:
            all_merged = [pr for _r, mg, _o, _d in per_repo for pr in mg]
            all_opened = [pr for _r, _m, op, _d in per_repo for pr in op]

            # Human-in-the-loop signals need one extra read per merged PR, so they are
            # bounded and best-effort: a PR we cannot read is simply not counted, and the
            # block says how many of the merged set the figures actually cover. A metric
            # computed over an unknown fraction is worse than one that names its fraction.
            signals: dict[str, dict] = {}
            cap = max(0, min(400, int(os.environ.get("WEEKLY_STATUS_MAX_SIGNAL_PRS", "150"))))
            async def one_signal(repo: str, pr: dict) -> None:
                key = _pr_key(pr)
                num = pr.get("number")
                if not key or num is None:
                    return
                try:
                    signals[key] = await fetch_pr_human_signals(repo, int(num), token)
                except Exception as e:
                    logger.warning("PR signals %s#%s: %s", repo, num, e)
            tasks = [one_signal(r, pr) for r, mg, _o, _d in per_repo for pr in mg][:cap]
            if tasks:
                await asyncio.gather(*tasks)

            em = compute_engineering_metrics(
                all_merged, all_opened, repos=len(per_repo), signals=signals,
                window_days=max(1, (
                    _dt.date.fromisoformat(until_d) - _dt.date.fromisoformat(since_d)
                ).days + 1),
            )
            snap_key = f"weekly:{until_d}"
            prev = await previous_metric_snapshot("weekly:", snap_key)
            metrics_block = render_engineering_metrics(em, prev)
            await upsert_metric_snapshot(snap_key, em.as_row())
        except Exception as e:  # metrics must never take the weekly down
            logger.warning("Weekly engineering metrics failed: %s", e)
            metrics_block = ""
        user_prompt = (
            f"Reporting window: {range_label}.\n"
            "Sources (synthesize into themed sections — do not dump raw tables or list every PR):\n"
            f"1) Slack channel messages in the window.\n"
            f"2) GitHub metrics and PR data per repo (below) — group by theme, cite counts not full lists.\n"
            f"3) Google Drive (below): docs **linked in channel messages** this week plus files modified under linked folders.\n\n"
            f"---\n### Slack transcript (opaque user ids U…; infer roles from content)\n{slack_digest}\n"
            f"{bookmark_section}"
            f"---\n### GitHub (all configured repos for this weekly run)\n{facts}"
            f"{drive_block}"
            f"{meetings_block}"
            + (f"\n---\n{metrics_observations_prompt(metrics_block)}\n" if metrics_block else "")
        )
        system = "\n\n".join(
            [
                cleandoc(
                    f"""
                    You are Susan, writing a weekly update for an **engineering / tech** channel
                    that leadership will read. {SUSAN_VOICE}

                    Ground the update in Slack, GitHub, and Drive data provided — but **synthesize**, and
                    keep it short: the reader is a founder skimming Slack, not an engineer auditing a week.
                    - Group merged/opened PRs by **theme**, not repository. Mention PR *counts* per theme;
                      link at most 1–2 exemplar PRs per theme.
                    - Credit **PR openers** (`opener=@login` on each PR line) on theme headers — not reviewers.
                    - Credit **Drive owners/editors** (`by owner: …` on each Drive line) — not Slack commenters.
                    - Weave Dependabot posture and turnaround hints into relevant themes only when material.
                    - Tag shippers per theme with `@github-login` and `<@U…>` when mappable from Slack.
                    - Outcomes and customer/delivery impact first; trim implementation jargon.
                    If a repo does not map to a theme, fold it into the closest theme or one short bullet.
                    If Dependabot data was unavailable, note briefly. If Drive block is empty, skip Drive content.
                    """
                ),
                _WEEKLY_ATTRIBUTION,
                _WEEKLY_SLACK_MRKDN,
                _WEEKLY_STRUCTURE,
            ]
        )
    else:
        user_prompt = (
            f"Reporting window: {range_label}.\n"
            "This channel does not use the full GitHub metrics bundle; rely on Slack, bookmarks, and Drive only.\n\n"
            f"---\n### Slack transcript\n{slack_digest}\n"
            f"{bookmark_section}"
            f"{drive_block}"
            f"{meetings_block}"
            + (f"\n---\n{metrics_observations_prompt(metrics_block)}\n" if metrics_block else "")
        )
        system = "\n\n".join(
            [
                cleandoc(
                    f"""
                    You are Susan, writing a weekly update for a **general team** channel
                    that leadership may forward to founders. {SUSAN_VOICE}

                    **Do not** lead with pull requests or repo lists unless the transcript clearly discusses them.
                    Group topics by theme; credit **Drive owners/editors** and people who **posted** substantive
                    Slack updates — not passive commenters. Use `<@U…>` for Slack; map Drive names when clear.
                    Outcomes and decisions first — minimal jargon. Use Drive/bookmark links only from provided data.
                    """
                ),
                _WEEKLY_ATTRIBUTION,
                _WEEKLY_SLACK_MRKDN,
                _WEEKLY_STRUCTURE,
            ]
        )

    max_tok = max(1500, min(32000, int(os.environ.get("WEEKLY_STATUS_MAX_TOKENS", "8192"))))
    try:
        logger.info(
            "Weekly status completion request prompt_chars=%s max_tokens=%s",
            len(user_prompt),
            max_tok,
        )
        summary = await call_claude(
            system,
            user_prompt,
            max_tokens=max_tok,
            action="weekly_status",
        )
    except Exception as e:
        logger.exception("Weekly status Claude failed")
        await notify_user_ephemeral(channel, user, f"Susan error: {e}", None, response_url)
        return

    title = _weekly_status_title_line(repos, range_label, include_github=include_github)
    model_route = summary.model_route
    model_name = summary.model_name
    if metrics_block:
        # The authoritative block is INSERTED, never left to the model to reproduce —
        # one paraphrase is one wrong number in a founder-facing update. The model is
        # given it read-only and asked for three interpretation lines, which belong
        # UNDER it, so the block goes in immediately before the first of them.
        body = str(summary).rstrip()
        cut = min(
            (i for i in (body.find(mk) for mk in ("*What changed:*", "*Where we're losing time:*", "*Action:*")) if i != -1),
            default=-1,
        )
        if cut > 0:
            body = f"{body[:cut].rstrip()}\n\n{metrics_block}\n\n{body[cut:].lstrip()}"
        else:
            body = f"{body}\n\n{metrics_block}"
        summary = ModelCompletion(body, model_route=model_route, model_name=model_name)
    if auto_publish:
        try:
            await publish_weekly_status(
                channel,
                thread_ts,
                title,
                summary,
                model_route=model_route,
                model_name=model_name,
            )
        except Exception as e:
            logger.exception("Weekly status auto-publish failed")
            await notify_user_ephemeral(
                channel,
                user,
                f"Susan could not post weekly status to the channel: {e}",
                None,
                response_url,
            )
            return
        await notify_user_ephemeral(
            channel,
            user,
            "✓ Weekly status was posted to the channel (_Canvas link; no approval step_).",
            None,
            response_url,
        )
        return

    meta = {
        "title": title,
        "body": summary,
        "channel_id": channel,
        "thread_ts": thread_ts,
        "repos": repos if include_github else [],
        "include_github": include_github,
        "model_route": model_route,
        "model_name": model_name,
    }
    draft_id = await create_user_draft(
        user, "weekly_status", json.dumps(meta, ensure_ascii=False)
    )
    display_truncated = summary[:2800] + ("..." if len(summary) > 2800 else "")
    hint = (
        "_Use *Approve & post to channel* to publish a Canvas link for everyone in this conversation, "
        "or *Cancel*._"
    )
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Susan preview — {ACTIONS['weekly_status'][0]}*\n_(Only visible to you)_\n{hint}",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{display_truncated}```"}},
        {
            "type": "actions",
            "block_id": f"susan_weekly_{channel}_{thread_ts or 'none'}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✓ Approve & post Canvas link"},
                    "style": "primary",
                    "action_id": "approve_weekly_status",
                    "value": draft_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✗ Cancel"},
                    "action_id": "cancel_susan",
                    "value": draft_id,
                },
            ],
        },
    ]
    if include_github:
        repo_hint = (
            f"`{repos[0]}`"
            if len(repos) == 1
            else f"{len(repos)} repos ({', '.join(repos[:3])}{'…' if len(repos) > 3 else ''})"
        )
        preview_note = f"Susan weekly status preview ready ({repo_hint})"
    else:
        preview_note = "Susan weekly status preview ready (Slack only — not a tech channel)"
    await notify_user_ephemeral(
        channel,
        user,
        preview_note,
        blocks,
        response_url,
        model_route=model_route,
        model_name=model_name,
    )
