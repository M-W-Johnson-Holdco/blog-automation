"""Together AI model defaults and per-role serverless fallback chains."""

from __future__ import annotations

import os

WRITING_MODEL_FALLBACK_CHAIN: tuple[str, ...] = (
    "Qwen/Qwen3.5-397B-A17B",
    "Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
    "openai/gpt-oss-120b",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
)

EVALUATION_MODEL_FALLBACK_CHAIN: tuple[str, ...] = (
    "Qwen/Qwen2.5-7B-Instruct-Turbo",
    "Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
    "openai/gpt-oss-120b",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
)

SCORER_MODEL_FALLBACK_CHAIN: tuple[str, ...] = (
    "Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
    "Qwen/Qwen3.5-397B-A17B",
    "openai/gpt-oss-120b",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
)

FACT_AUDIT_MODEL_FALLBACK_CHAIN: tuple[str, ...] = SCORER_MODEL_FALLBACK_CHAIN

DEFAULT_WRITING_MODEL = WRITING_MODEL_FALLBACK_CHAIN[0]
DEFAULT_EVALUATION_MODEL = EVALUATION_MODEL_FALLBACK_CHAIN[0]
DEFAULT_SCORER_MODEL = SCORER_MODEL_FALLBACK_CHAIN[0]
DEFAULT_FACT_AUDIT_MODEL = FACT_AUDIT_MODEL_FALLBACK_CHAIN[0]

WRITING_MODEL_ENV = "TOGETHER_WRITING_MODEL"
EVALUATION_MODEL_ENV = "TOGETHER_EVALUATION_MODEL"
SCORER_MODEL_ENV = "TOGETHER_SCORER_MODEL"
FACT_AUDIT_MODEL_ENV = "TOGETHER_FACT_AUDIT_MODEL"


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


def fallback_models_after(model: str, chain: tuple[str, ...]) -> list[str]:
    if model in chain:
        return list(chain[chain.index(model) + 1 :])
    return [candidate for candidate in chain if candidate != model]


def model_attempt_sequence(
    model: str,
    chain: tuple[str, ...],
    *,
    allow_fallback: bool = True,
) -> list[str]:
    sequence = [model]
    if allow_fallback:
        for candidate in fallback_models_after(model, chain):
            if candidate not in sequence:
                sequence.append(candidate)
    return sequence
