"""Provider-aware model resolution (Anthropic Claude or Together rollback)."""

from __future__ import annotations

from blog_automation.claude_models import (
    DEFAULT_EVALUATION_MODEL as CLAUDE_DEFAULT_EVALUATION_MODEL,
    DEFAULT_FACT_AUDIT_MODEL as CLAUDE_DEFAULT_FACT_AUDIT_MODEL,
    DEFAULT_SCORER_MODEL as CLAUDE_DEFAULT_SCORER_MODEL,
    DEFAULT_WRITING_MODEL as CLAUDE_DEFAULT_WRITING_MODEL,
    resolve_evaluation_model as resolve_claude_evaluation_model,
    resolve_fact_audit_model as resolve_claude_fact_audit_model,
    resolve_scorer_model as resolve_claude_scorer_model,
    resolve_writing_model as resolve_claude_writing_model,
)
from blog_automation.llm_client import get_llm_provider
from blog_automation.together_models import (
    DEFAULT_EVALUATION_MODEL as TOGETHER_DEFAULT_EVALUATION_MODEL,
    DEFAULT_FACT_AUDIT_MODEL as TOGETHER_DEFAULT_FACT_AUDIT_MODEL,
    DEFAULT_SCORER_MODEL as TOGETHER_DEFAULT_SCORER_MODEL,
    DEFAULT_WRITING_MODEL as TOGETHER_DEFAULT_WRITING_MODEL,
    resolve_evaluation_model as resolve_together_evaluation_model,
    resolve_fact_audit_model as resolve_together_fact_audit_model,
    resolve_scorer_model as resolve_together_scorer_model,
    resolve_writing_model as resolve_together_writing_model,
)


def normalize_writing_model_for_provider(model: str) -> str:
    """Map a stale Together/Claude model id to the active provider's default."""
    cleaned = str(model or "").strip()
    if not cleaned:
        return default_writing_model()
    lowered = cleaned.lower()
    if get_llm_provider() == "anthropic":
        if lowered.startswith("claude-"):
            return cleaned
        return default_writing_model()
    if lowered.startswith("claude-"):
        return default_writing_model()
    return cleaned


def resolve_writing_model(model: str | None = None) -> str:
    if model:
        return normalize_writing_model_for_provider(model)
    if get_llm_provider() == "anthropic":
        return resolve_claude_writing_model(None)
    return resolve_together_writing_model(None)


def resolve_evaluation_model(model: str | None = None) -> str:
    if get_llm_provider() == "anthropic":
        return resolve_claude_evaluation_model(model)
    return resolve_together_evaluation_model(model)


def resolve_scorer_model(model: str | None = None) -> str:
    if get_llm_provider() == "anthropic":
        return resolve_claude_scorer_model(model)
    return resolve_together_scorer_model(model)


def resolve_fact_audit_model(model: str | None = None) -> str:
    if get_llm_provider() == "anthropic":
        return resolve_claude_fact_audit_model(model)
    return resolve_together_fact_audit_model(model)


def default_writing_model() -> str:
    if get_llm_provider() == "anthropic":
        return CLAUDE_DEFAULT_WRITING_MODEL
    return TOGETHER_DEFAULT_WRITING_MODEL


def default_evaluation_model() -> str:
    if get_llm_provider() == "anthropic":
        return CLAUDE_DEFAULT_EVALUATION_MODEL
    return TOGETHER_DEFAULT_EVALUATION_MODEL


def default_scorer_model() -> str:
    if get_llm_provider() == "anthropic":
        return CLAUDE_DEFAULT_SCORER_MODEL
    return TOGETHER_DEFAULT_SCORER_MODEL


def default_fact_audit_model() -> str:
    if get_llm_provider() == "anthropic":
        return CLAUDE_DEFAULT_FACT_AUDIT_MODEL
    return TOGETHER_DEFAULT_FACT_AUDIT_MODEL
