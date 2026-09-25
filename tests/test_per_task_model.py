"""A task can name its own self-hosted model.

The weekly update is long-context synthesis and reads better on GLM-5.3-Flash than on
the default sovereign model; the short digests do not need it. Both are `action=value`
maps so a task moves without a deploy.
"""
from __future__ import annotations

import pytest

from app.model_routing import sovereign_override


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SUSAN_ACTION_MODELS", raising=False)
    monkeypatch.delenv("SUSAN_ACTION_MODEL_BASE_URLS", raising=False)


def test_no_override_uses_the_defaults() -> None:
    assert sovereign_override("weekly_status") == (None, None)
    assert sovereign_override(None) == (None, None)


def test_an_action_can_name_its_model_and_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUSAN_ACTION_MODELS", "weekly_status=glm-5.3-flash,status_page=glm-5.3-flash")
    monkeypatch.setenv("SUSAN_ACTION_MODEL_BASE_URLS", "weekly_status=https://glm.frontierone.dev/v1")
    assert sovereign_override("weekly_status") == ("glm-5.3-flash", "https://glm.frontierone.dev/v1")
    # A model with no endpoint of its own falls back to the default endpoint.
    assert sovereign_override("status_page") == ("glm-5.3-flash", None)
    # An action nobody named is untouched.
    assert sovereign_override("standup_digest") == (None, None)


def test_whitespace_and_malformed_pairs_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUSAN_ACTION_MODELS", " weekly_status = glm-5.3-flash , junk, =x, y= ")
    assert sovereign_override("weekly_status") == ("glm-5.3-flash", None)


@pytest.mark.asyncio
async def test_the_override_reaches_the_request_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the feature: the named model and endpoint are what get called."""
    import app.claude_client as cc

    monkeypatch.setenv("SUSAN_ACTION_MODELS", "weekly_status=glm-5.3-flash")
    monkeypatch.setenv("SUSAN_ACTION_MODEL_BASE_URLS", "weekly_status=https://glm.example/v1")
    monkeypatch.setattr(cc, "F1_MODEL_BASE_URL", "https://default.example/v1")
    monkeypatch.setattr(cc, "F1_MODEL_NAME", "deepseek-default")
    monkeypatch.setattr(cc, "f1_model_active", lambda: True)
    seen: dict = {}

    async def fake(system, user, max_tokens=None, *, model=None, base_url=None):
        seen.update(model=model, base_url=base_url)
        return "ok", model or "deepseek-default"

    monkeypatch.setattr(cc, "_call_f1_sovereign", fake)
    out = await cc.call_claude("sys", "usr", action="weekly_status", model_route="sovereign")
    assert seen == {"model": "glm-5.3-flash", "base_url": "https://glm.example/v1"}
    assert out.model_name == "glm-5.3-flash"      # attribution follows what answered

    seen.clear()
    await cc.call_claude("sys", "usr", action="standup_digest", model_route="sovereign")
    assert seen == {"model": None, "base_url": None}
