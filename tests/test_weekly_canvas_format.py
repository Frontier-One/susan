"""The weekly update is written in Slack mrkdwn; a Canvas renders markdown.

Without a structural conversion the Canvas is a wall of bold paragraphs and literal `•`
characters — no headings, no lists, nothing scannable. These pin the shapes the update
actually emits (`*Theme* — people`, `• *Shipped:* …`, a trailing decisions line).
"""
from __future__ import annotations

from app.weekly_canvas import _canvas_document_markdown, slack_mrkdwn_to_canvas_markdown


def test_section_lines_become_headings_not_bold_paragraphs() -> None:
    out = slack_mrkdwn_to_canvas_markdown("intro\n\n*Inference platform* — @dev\n• did a thing")
    assert "## Inference platform — @dev" in out
    assert "**Inference platform**" not in out


def test_bullets_become_real_list_items() -> None:
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n*T* — x\n• first\n• second")
    assert "\n- first\n- second" in out
    assert "•" not in out


def test_nested_bullet_keeps_one_level_of_indent() -> None:
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n*T* — x\n• top\n  • child")
    assert "- top" in out and "\n  - child" in out


def test_first_bold_line_is_a_standfirst_not_a_second_title() -> None:
    """The Canvas already carries an H1; two stacked headings read as a mistake."""
    out = slack_mrkdwn_to_canvas_markdown("*Tech team update — 17–24 Sep*\n\n*Theme* — a\n• x")
    assert out.startswith("_Tech team update — 17–24 Sep_")
    assert "## Tech team update" not in out
    assert "## Theme — a" in out


def test_decisions_line_becomes_a_section_with_one_item_each() -> None:
    out = slack_mrkdwn_to_canvas_markdown("lead\n\nDecisions this week: alpha; beta; gamma.")
    assert "## Decisions this week" in out
    assert "\n- alpha\n- beta\n- gamma" in out


def test_decisions_without_items_is_left_alone() -> None:
    assert "## Decisions" not in slack_mrkdwn_to_canvas_markdown("lead\n\nDecisions this week:  ")


def test_links_and_inline_bold_still_convert() -> None:
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n*T* — x\n• *Shipped:* see <https://e.com/p/1|#1>")
    assert "[#1](https://e.com/p/1)" in out
    assert "- **Shipped:** see" in out


def test_mid_sentence_emphasis_is_not_promoted_to_a_heading() -> None:
    out = slack_mrkdwn_to_canvas_markdown("lead\n\nwe shipped *the thing* today — really")
    assert "## " not in out
    assert "**the thing**" in out


def test_document_has_one_h1_and_the_footer() -> None:
    doc = _canvas_document_markdown("Weekly status — X", "*Lead*\n\n*T* — a\n• b")
    assert doc.startswith("# Weekly status — X")
    assert doc.count("\n# ") == 0          # exactly one H1, the title
    assert doc.rstrip().endswith("_Posted via Susan_")


def test_empty_body_is_empty_not_an_exception() -> None:
    assert slack_mrkdwn_to_canvas_markdown("") == ""
    assert slack_mrkdwn_to_canvas_markdown("   \n\n ") == ""


# ── reported from a live Canvas, 2026-09-25 ───────────────────────────────────────────


def test_a_run_of_bullet_characters_becomes_ONE_list_item() -> None:
    """The model emits `• • text`; that rendered as a native bullet then a literal •."""
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n*T* — x\n• • did the thing")
    assert "- did the thing" in out
    assert "•" not in out


def test_a_leading_label_on_an_item_is_bolded() -> None:
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n*T* — x\n• Next: flip the gates\n• Blocked / at risk: a review")
    assert "- **Next:** flip the gates" in out
    assert "- **Blocked / at risk:** a review" in out


def test_an_already_emphasised_label_is_left_alone() -> None:
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n*T* — x\n• *Shipped:* a thing")
    assert "- **Shipped:** a thing" in out
    assert "****" not in out


def test_a_sentence_starting_with_a_capital_is_not_mistaken_for_a_label() -> None:
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n*T* — x\n• Gavin shipped the thing today")
    assert "- Gavin shipped the thing today" in out
    assert "**" not in out


def test_slack_mentions_resolve_to_names_and_raw_ids_never_survive() -> None:
    src = "lead\n\n*Review pipeline* — <@U0ANAC8FBQ8> @sgorelik\n• <@U0BUW8PN5E3> owns it"
    out = slack_mrkdwn_to_canvas_markdown(src, {"U0ANAC8FBQ8": "Stacy", "U0BUW8PN5E3": "Gavin"})
    assert "@Stacy" in out and "@Gavin" in out
    assert "U0ANAC8FBQ8" not in out and "<@" not in out


def test_an_unresolvable_mention_is_dropped_not_printed_raw() -> None:
    """A bare U0… id in a founder-facing page is worse than nothing; a handle is beside it."""
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n*T* — <@U0UNKNOWN9> @sgorelik\n• x", {})
    assert "U0UNKNOWN9" not in out
    assert "@sgorelik" in out


def test_mention_ids_are_listed_once_each_for_the_caller_to_resolve() -> None:
    from app.weekly_canvas import slack_mention_ids

    assert slack_mention_ids("<@U1AAAAAAA> a <@U2BBBBBBB> b <@U1AAAAAAA>") == ["U1AAAAAAA", "U2BBBBBBB"]


def test_decisions_paragraph_without_semicolons_still_becomes_a_list() -> None:
    out = slack_mrkdwn_to_canvas_markdown(
        "lead\n\nDecisions this week: We moved to vCluster. Standup goes async-first."
    )
    assert "## Decisions this week" in out
    assert "- We moved to vCluster" in out
    assert "- Standup goes async-first" in out


def test_canvas_footer_names_the_model_that_wrote_it() -> None:
    """The attribution rode the Slack message but not the Canvas, so moving the update
    into a Canvas silently dropped which model wrote it."""
    from app.weekly_canvas import _canvas_document_markdown as doc

    out = doc("W39", "*T* — x\n• a", {}, model_route="sovereign", model_name="glm-5.3-flash")
    assert "Secure Sovereign FrontierOne AI model (glm-5.3-flash)" in out
    assert out.rstrip().endswith("Posted via Susan_")
    # A commercial route keeps the plain footer.
    assert "Sovereign" not in doc("W39", "x", {}, model_route="commercial", model_name="claude")


def test_observation_lines_each_get_their_own_paragraph() -> None:
    from app.weekly_status import _separate_observation_lines as sep

    run_on = "*What changed:* a. *Where we're losing time:* b. *Action:* c."
    out = sep(run_on)
    for mk in ("*What changed:*", "*Where we're losing time:*", "*Action:*"):
        assert f"\n\n{mk}" in out or out.startswith(mk)
    # Idempotent — running it twice does not add more blank lines.
    assert sep(out) == out


def test_each_metric_is_its_own_list_item_in_the_canvas() -> None:
    """A bare newline is not a line break, so the block arrived as one paragraph."""
    from app.engineering_metrics import EngineeringMetrics, render

    block = render(EngineeringMetrics(iso_week=39, merged=10, rework_pct=5.0))
    out = slack_mrkdwn_to_canvas_markdown("lead\n\n" + block)
    assert "- **Shipped:**" in out
    assert "- **Rework:**" in out
    assert "- **Flow:**" in out
