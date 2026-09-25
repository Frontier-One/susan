"""Route Susan commands to commercial (Anthropic) vs sovereign (F1 self-hosted) models."""
from __future__ import annotations

import os

from app.config import ANTHROPIC_MODEL

# Flows that explicitly require a commercial model.
COMMERCIAL_ACTIONS = frozenset(
    {
        "sales_prep",
        "granola_cmd",
        "action_items_cmd",
        "surface_standups",
        "surface_failures",
        "surface_reviews",
        # Roadmap board questions: large board + epic + activity context, and the answers
        # get repeated to customers and investors.
        "roadmap_status",
        "roadmap_pack",
        "roadmap_risks",
        "roadmap_claims",
        "roadmap_customer",
        "roadmap_ask",
        "roadmap_add",
    }
)

# Default Anthropic model per commercial action (override via env, e.g. SALES_PREP_ANTHROPIC_MODEL).
COMMERCIAL_ACTION_MODELS: dict[str, str] = {
    "sales_prep": "claude-opus-4-6",
    "roadmap_status": "claude-opus-4-6",
    "roadmap_pack": "claude-opus-4-6",
}


def is_commercial_action(action: str | None, model_route: str | None = None) -> bool:
    if (model_route or "").strip().lower() == "commercial":
        return True
    return action in COMMERCIAL_ACTIONS


def route_for_action(action: str | None) -> str:
    """Return the configured ``commercial``, ``sovereign``, or ``default`` route."""
    if action in COMMERCIAL_ACTIONS:
        return "commercial"
    mode = (os.environ.get("SUSAN_DEFAULT_MODEL_ROUTE") or "default").strip().lower()
    if mode in ("sovereign", "local"):
        return "sovereign"
    return "default"


def resolve_model(*, action: str | None = None, model_route: str | None = None) -> str:
    """Pick the model id for an Anthropic Messages API call."""
    route = (model_route or route_for_action(action)).strip().lower()
    default_model = (os.environ.get("ANTHROPIC_MODEL") or ANTHROPIC_MODEL).strip()
    if route == "commercial":
        if action and action in COMMERCIAL_ACTION_MODELS:
            env_key = f"{action.upper()}_ANTHROPIC_MODEL"
            override = (os.environ.get(env_key) or "").strip()
            if override:
                return override
            return COMMERCIAL_ACTION_MODELS[action]
        return (os.environ.get("ANTHROPIC_COMMERCIAL_MODEL") or default_model).strip()
    if route in ("sovereign", "local"):
        sovereign = (os.environ.get("SOVEREIGN_MODEL") or "").strip()
        if sovereign:
            return sovereign
        return default_model
    return default_model


# ── per-action sovereign endpoint (2026-09-25) ────────────────────────────────────────
# Not every task wants the same self-hosted model. The weekly update is long-context
# synthesis and reads better on GLM-5.3-Flash; the digests are fine on the default.
# Two env vars, both `action=value` pairs, so a task can be moved without a deploy:
#
#   SUSAN_ACTION_MODELS="weekly_status=glm-5.3-flash,status_page=glm-5.3-flash"
#   SUSAN_ACTION_MODEL_BASE_URLS="weekly_status=https://glm.frontierone.dev/v1"
#
# A base URL is usually needed WITH the model: our models sit behind per-model gateways,
# so naming a model the default endpoint does not serve is a 404, not a fallback. When
# only the model is overridden the default endpoint is used and that is the caller's
# problem to get right.


def _action_map(var: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in (os.environ.get(var) or "").split(","):
        if "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def sovereign_override(action: str | None) -> tuple[str | None, str | None]:
    """(model, base_url) for this action, or (None, None) to use the defaults."""
    if not action:
        return None, None
    return (
        _action_map("SUSAN_ACTION_MODELS").get(action),
        _action_map("SUSAN_ACTION_MODEL_BASE_URLS").get(action),
    )
