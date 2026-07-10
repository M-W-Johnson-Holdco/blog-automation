"""LLM fact-audit pass for blog drafts against kept sources."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from blog_automation.llm_client import chat_completion
from blog_automation.llm_models import resolve_fact_audit_model
from blog_automation.paths import PROJECT_ROOT
from blog_automation.together_models import FACT_AUDIT_MODEL_FALLBACK_CHAIN
from blog_automation.write_common import format_sources_block
from blog_automation.company import render_template

FACT_AUDIT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "fact_audit.txt"


def load_fact_audit_prompt() -> str:
    return render_template(FACT_AUDIT_PROMPT_PATH.read_text(encoding="utf-8"))


def build_fact_audit_prompt(
    draft: str,
    selected_sources: list[dict[str, Any]],
    *,
    prompt_template: str | None = None,
) -> str:
    template = prompt_template or load_fact_audit_prompt()
    return (
        template.replace("{sources_block}", format_sources_block(selected_sources))
        .replace("{draft}", draft.strip())
    )


def parse_fact_audit_response(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Fact-audit response was not a JSON object.")

    passed = bool(parsed.get("passed"))
    issues = parsed.get("issues")
    if not isinstance(issues, list):
        issues = []
    revision_notes = str(parsed.get("revision_notes") or "").strip()
    if passed:
        issues = []
        revision_notes = ""
    return {
        "passed": passed,
        "issues": issues,
        "revision_notes": revision_notes,
    }


def audit_draft_against_sources(
    draft: str,
    selected_sources: list[dict[str, Any]],
    *,
    model: str | None = None,
    allow_fallback: bool = True,
) -> dict[str, Any]:
    """Run an LLM fact-audit against kept sources."""
    if not selected_sources:
        return {
            "passed": True,
            "issues": [],
            "revision_notes": "",
            "skipped": True,
        }

    active_model = resolve_fact_audit_model(model)
    prompt = build_fact_audit_prompt(draft, selected_sources)
    content, generation = chat_completion(
        model=active_model,
        messages=[
            {
                "role": "system",
                "content": "Return strict JSON only. Do not include markdown or commentary.",
            },
            {"role": "user", "content": prompt},
        ],
        allow_fallback=allow_fallback,
        fallback_chain=FACT_AUDIT_MODEL_FALLBACK_CHAIN,
        log_prefix="[fact_audit]",
        temperature=0.1,
        max_tokens=1200,
        response_format="json",
        role="fact_audit",
    )
    parsed = parse_fact_audit_response(content)
    return {
        **parsed,
        "skipped": False,
        "model_requested": generation.get("model_requested", active_model),
        "model_used": generation.get("model_used", active_model),
        "generation": generation,
    }
