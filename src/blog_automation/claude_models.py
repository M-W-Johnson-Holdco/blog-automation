"""Anthropic Claude model defaults and per-role env resolution."""

from __future__ import annotations

import os

DEFAULT_EVALUATION_MODEL = "claude-sonnet-4-6"
DEFAULT_WRITING_MODEL = "claude-opus-4-8"
DEFAULT_FACT_AUDIT_MODEL = "claude-sonnet-4-6"
DEFAULT_SCORER_MODEL = "claude-sonnet-4-6"

EVALUATION_MODEL_ENV = "CLAUDE_EVALUATION_MODEL"
WRITING_MODEL_ENV = "CLAUDE_WRITING_MODEL"
FACT_AUDIT_MODEL_ENV = "CLAUDE_FACT_AUDIT_MODEL"
SCORER_MODEL_ENV = "CLAUDE_SCORER_MODEL"


def resolve_model(env_var: str, default: str) -> str:
    return os.getenv(env_var, "").strip() or default


def resolve_writing_model(model: str | None = None) -> str:
    return model or resolve_model(WRITING_MODEL_ENV, DEFAULT_WRITING_MODEL)


def resolve_evaluation_model(model: str | None = None) -> str:
    return model or resolve_model(EVALUATION_MODEL_ENV, DEFAULT_EVALUATION_MODEL)


def resolve_scorer_model(model: str | None = None) -> str:
    return model or resolve_model(SCORER_MODEL_ENV, DEFAULT_SCORER_MODEL)


def resolve_fact_audit_model(model: str | None = None) -> str:
    return model or resolve_model(FACT_AUDIT_MODEL_ENV, DEFAULT_FACT_AUDIT_MODEL)
