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
