"""Unified LLM chat completion entry point (Anthropic Claude or Together rollback)."""

from __future__ import annotations

import os
import time
from typing import Any

from blog_automation.together_models import (
    EVALUATION_MODEL_FALLBACK_CHAIN,
    FACT_AUDIT_MODEL_FALLBACK_CHAIN,
    SCORER_MODEL_FALLBACK_CHAIN,
    WRITING_MODEL_FALLBACK_CHAIN,
)

LLM_PROVIDER_ENV = "LLM_PROVIDER"
DEFAULT_LLM_PROVIDER = "anthropic"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

WRITING_SYSTEM_PROMPT = (
    "You are a senior home-services content strategist. "
    "Return only the complete Markdown blog draft starting with one H1 title line. "
    "Do not include planning notes, analysis, or a thinking process."
)

_ROLE_FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
    "writing": WRITING_MODEL_FALLBACK_CHAIN,
    "evaluation": EVALUATION_MODEL_FALLBACK_CHAIN,
    "scorer": SCORER_MODEL_FALLBACK_CHAIN,
    "fact_audit": FACT_AUDIT_MODEL_FALLBACK_CHAIN,
}

_ANTHROPIC_CREDIT_ERROR_MARKERS = (
    "credit balance is too low",
    "purchase credits",
    "plans & billing",
    "monthly spend limit",
    "spending limit",
)


class LlmCreditsExhaustedError(EnvironmentError):
    """Raised when the active LLM provider cannot run due to billing/credits."""


def get_llm_provider() -> str:
    provider = os.getenv(LLM_PROVIDER_ENV, DEFAULT_LLM_PROVIDER).strip().lower()
    if provider in {"anthropic", "claude"}:
        return "anthropic"
    if provider == "together":
        return "together"
    raise ValueError(
        f"Unsupported {LLM_PROVIDER_ENV}={provider!r}. Use 'anthropic' or 'together'."
    )


def get_anthropic_client():
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: anthropic. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    api_key = os.getenv(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        raise EnvironmentError(f"{ANTHROPIC_API_KEY_ENV} is not set. Add it to .env.")
    return anthropic.Anthropic(api_key=api_key)


def _is_anthropic_credit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _ANTHROPIC_CREDIT_ERROR_MARKERS)


def ensure_llm_credits(*, log_prefix: str = "[llm]") -> None:
    """Fail fast if the active LLM provider cannot run — call before Tavily spend.

    Anthropic does not expose a public credit-balance endpoint, so this sends a
    1-token probe request. When the account is out of credits, abort before search.
    """
    provider = get_llm_provider()
    if provider == "together":
        from blog_automation.write_common import get_together_client

        get_together_client()
        print(f"{log_prefix} Preflight: Together API key present", flush=True)
        return

    client = get_anthropic_client()
    from blog_automation.claude_models import DEFAULT_EVALUATION_MODEL

    print(f"{log_prefix} Preflight: checking Anthropic API credits…", flush=True)
    try:
        client.messages.create(
            model=DEFAULT_EVALUATION_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:
        if _is_anthropic_credit_error(exc):
            raise LlmCreditsExhaustedError(
                "Anthropic API credit balance is too low (or spend limit reached). "
                "Add credits at https://console.anthropic.com/settings/billing "
                "before running search so Tavily credits are not wasted."
            ) from exc
        raise EnvironmentError(
            f"Anthropic API preflight failed before search: {exc}"
        ) from exc
    print(f"{log_prefix} Preflight: Anthropic API OK", flush=True)


def normalize_anthropic_usage(usage: Any) -> dict[str, int]:
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cached_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached_tokens": cached_tokens,
    }


def anthropic_omits_sampling_params(model: str) -> bool:
    """Opus 4.7+ returns 400 if temperature/top_p/top_k are sent."""
    normalized = model.lower()
    if "opus" not in normalized:
        return False
    return any(marker in normalized for marker in ("opus-4-7", "opus-4-8", "opus-4-9"))


def split_messages_for_anthropic(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    system_parts: list[str] = []
    conversation: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            raise ValueError(f"Unsupported Anthropic message role: {role!r}")
        conversation.append({"role": role, "content": content})
    if not conversation:
        raise ValueError("At least one user or assistant message is required.")
    system = "\n\n".join(system_parts).strip() or None
    return system, conversation


def _anthropic_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    response_format: str | None,
) -> tuple[str, dict[str, Any]]:
    from blog_automation.write_common import build_generation_report

    client = get_anthropic_client()
    system, conversation = split_messages_for_anthropic(messages)
    if response_format == "json" and system:
        system = f"{system}\n\nReturn strict JSON only. Do not include markdown fences or commentary."
    elif response_format == "json":
        system = "Return strict JSON only. Do not include markdown fences or commentary."

    started_at = time.monotonic()
    request_kwargs: dict[str, Any] = {
        "model": model,
        "messages": conversation,
        "max_tokens": max_tokens,
    }
    if not anthropic_omits_sampling_params(model):
        request_kwargs["temperature"] = temperature
    if system:
        request_kwargs["system"] = system

    response = client.messages.create(**request_kwargs)
    text_blocks = [
        block.text
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text" and getattr(block, "text", "")
    ]
    content = "\n".join(text_blocks).strip()
    usage = normalize_anthropic_usage(getattr(response, "usage", None))
    metadata = build_generation_report(
        model_requested=model,
        model_used=model,
        model_returned_by_api=getattr(response, "model", None),
        elapsed_seconds=time.monotonic() - started_at,
        usage=usage,
    )
    metadata["provider"] = "anthropic"
    return content, metadata


def _together_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    response_format: str | None,
    log_prefix: str,
    allow_fallback: bool,
    fallback_chain: tuple[str, ...] | None,
) -> tuple[str, dict[str, Any]]:
    from blog_automation.write_common import call_together_chat_with_fallback, get_together_client

    client = get_together_client()
    completion_kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format == "json":
        completion_kwargs["response_format"] = {"type": "json_object"}

    chain = fallback_chain or (model,)
    response, model_requested, model_used, metadata = call_together_chat_with_fallback(
        client,
        model=model,
        fallback_chain=chain,
        messages=messages,
        allow_fallback=allow_fallback,
        log_prefix=log_prefix,
        **completion_kwargs,
    )
    message = response.choices[0].message
    content = str(getattr(message, "content", None) or "").strip()
    metadata["provider"] = "together"
    metadata["model_requested"] = model_requested
    metadata["model_used"] = model_used
    return content, metadata


def chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.35,
    max_tokens: int = 3400,
    response_format: str | None = None,
    log_prefix: str = "[llm]",
    allow_fallback: bool = True,
    fallback_chain: tuple[str, ...] | None = None,
    role: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return assistant text and a generation metadata dict (usage, cost, timing)."""
    provider = get_llm_provider()
    if provider == "anthropic":
        return _anthropic_chat_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    chain = fallback_chain
    if chain is None and role:
        chain = _ROLE_FALLBACK_CHAINS.get(role, (model,))
    return _together_chat_completion(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        log_prefix=log_prefix,
        allow_fallback=allow_fallback,
        fallback_chain=chain,
    )
