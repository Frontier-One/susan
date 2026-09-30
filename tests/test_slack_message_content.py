"""Everything a reader SEES in a Slack message reaches the model, not just `text`.

Written against a real failure: on 2026-09-29 the daily standup digest reported
"no standup content captured" on a day when four people had posted full written
standups. The standup app puts the placeholder in `text` and every question and
answer in `attachments[]`, so reading `text` alone handed the model four lines of
"*Name* posted an update for *Tech-Standups*" and nothing else. The digest was
not wrong about its input; the input was being thrown away before it got there.
"""
from __future__ import annotations

from app.slack_api import _message_text, _speaker_of

# The real shape, trimmed: placeholder text, content entirely in attachments.
STANDUP = {
    "bot_id": "B0C3ANXD1MY",
    "username": "Dmitry",
    "text": "*Stacy Gorelik* posted an update for *Tech-Standups*",
    "attachments": [
        {"title": "What have you done since yesterday?", "text": "Shipped Brokkr."},
        {"title": "What will you do today?", "text": "Testing the Brokkr farm."},
        {"title": "Anything blocking your progress?", "text": "Nothing."},
    ],
}




def test_a_human_message_is_attributed_to_its_user() -> None:
    assert _speaker_of({"user": "U0ANAC8FBQ8", "text": "hi"}) == "U0ANAC8FBQ8"


def test_a_bot_message_is_attributed_even_though_it_has_no_user_field() -> None:
    """The regression: every app post rendered as `unknown:`."""
    assert _speaker_of(STANDUP) == "Dmitry"


def test_bot_attribution_falls_back_through_profile_then_id() -> None:
    assert _speaker_of({"bot_id": "B1", "bot_profile": {"name": "Geekbot"}}) == "Geekbot"
    assert _speaker_of({"bot_id": "B1"}) == "B1"
    assert _speaker_of({}) == "unknown"


def test_attachment_questions_and_answers_all_survive() -> None:
    out = _message_text(STANDUP)
    assert "Shipped Brokkr." in out
    assert "Testing the Brokkr farm." in out
    assert "What have you done since yesterday?" in out
    # The placeholder is kept too: it names who the update belongs to.
    assert "posted an update" in out


def test_the_placeholder_alone_is_what_the_old_code_saw() -> None:
    """Pins the bug itself, so nobody 'simplifies' back to `text`."""
    assert _message_text(STANDUP) != STANDUP["text"]
    assert len(_message_text(STANDUP)) > len(STANDUP["text"]) * 2


def test_block_kit_content_is_read_too() -> None:
    """The modern shape. It had the identical bug and would have bitten next."""
    msg = {
        "user": "U1",
        "text": "",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Blocked on the DNS apply"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "since Friday"}]},
        ],
    }
    out = _message_text(msg)
    assert "Blocked on the DNS apply" in out
    assert "since Friday" in out


def test_an_unfamiliar_block_type_is_still_read() -> None:
    """Walked structurally, so a block type Slack adds later degrades to
    'we read it' rather than 'we silently dropped it'."""
    msg = {"user": "U1", "blocks": [{"type": "something_new_2027", "text": "still visible"}]}
    assert "still visible" in _message_text(msg)


def test_repeated_content_is_not_counted_twice() -> None:
    """`fallback` repeats the attachment, and Block Kit repeats `text`.

    Without dedup the digest sees each answer two or three times and weights it
    accordingly, which is its own quiet distortion.
    """
    msg = {
        "user": "U1",
        "text": "Deploy is red",
        "attachments": [{"text": "Deploy is red", "fallback": "Deploy is red"}],
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Deploy is red"}}],
    }
    assert _message_text(msg) == "Deploy is red"


def test_attachment_fields_are_read() -> None:
    msg = {"user": "U1", "attachments": [{"fields": [{"title": "Env", "value": "freya"}]}]}
    out = _message_text(msg)
    assert "Env" in out and "freya" in out


def test_a_plain_message_is_unchanged() -> None:
    assert _message_text({"user": "U1", "text": "just a message"}) == "just a message"


def test_an_empty_message_yields_empty_string_not_a_crash() -> None:
    assert _message_text({}) == ""
    assert _message_text({"text": None, "attachments": None, "blocks": None}) == ""


def test_malformed_attachments_and_blocks_do_not_crash() -> None:
    """Slack payloads are not ours; a digest must not die on an odd one."""
    msg = {"user": "U1", "text": "ok", "attachments": ["not a dict", None], "blocks": ["nope", 7]}
    assert _message_text(msg) == "ok"


def test_two_questions_with_the_same_answer_both_keep_it() -> None:
    """A field is half of a question-and-answer pair, and two standup questions
    answered "None" are two answers. A global dedup dropped the second, leaving
    its question in the digest with nothing under it — content loss dressed as
    tidiness (cubic P2 on PR #31)."""
    msg = {
        "user": "U1",
        "attachments": [{"fields": [
            {"title": "What did you do yesterday?", "value": "None"},
            {"title": "Any blockers?", "value": "None"},
        ]}],
    }
    out = _message_text(msg)
    assert out.count("None") == 2, out
    # and each question still sits immediately above its own answer
    assert out.index("What did you do yesterday?") < out.index("None")
    assert out.index("Any blockers?") < out.rindex("None")


def test_a_combined_fallback_does_not_duplicate_structured_content() -> None:
    """`fallback` is the plain-text rendering of the attachment. A fallback of
    "Q: A" is not string-equal to title "Q" or text "A", so dedup could not
    catch it and the model saw the same content twice (cubic P2)."""
    msg = {
        "user": "U1",
        "attachments": [{"title": "Deploy", "text": "red", "fallback": "Deploy: red"}],
    }
    out = _message_text(msg)
    assert "Deploy: red" not in out, out
    assert "Deploy" in out and "red" in out


def test_fallback_is_still_read_when_there_is_nothing_else() -> None:
    """The control. Gating the fallback must not silence an attachment that has
    only a fallback — which is the one case it was always for."""
    msg = {"user": "U1", "attachments": [{"fallback": "the only content there is"}]}
    assert "the only content there is" in _message_text(msg)
