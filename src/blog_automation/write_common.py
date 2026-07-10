"""Shared blog draft generation helpers for write_serverless.py."""

from __future__ import annotations

from blog_automation.paths import PROJECT_ROOT
from blog_automation.company import get_company_slug, get_profile

_PROFILE = get_profile()

import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from blog_automation.cli_progress import run_with_progress
from blog_automation.pipeline_costs import (
    consume_tavily_search_ran,
    load_pipeline_costs,
    print_inference_stage_cost,
    print_pipeline_run_cost_summary,
)
from blog_automation.used_sources import normalize_source_url, sources_used_payload, used_source_urls
from blog_automation.draft_pdf import save_draft_pdf
from blog_automation.together_models import (
    WRITING_MODEL_FALLBACK_CHAIN,
    fallback_models_after,
    model_attempt_sequence,
)
from blog_automation.writing_prompts import (
    DEFAULT_WRITING_PROMPT_ID,
    SUMMARY_HEADING_WHO,
    SUMMARY_HEADING_WHAT,
    SUMMARY_HEADING_WHEN,
    get_writing_prompt_variant,
    writing_prompt_metadata,
)



DEFAULT_INPUT_PATH = PROJECT_ROOT / "output" / "sources" / "kept_sources.json"
PROMPT_PATH = PROJECT_ROOT / "prompts" / "blog_geo.txt"
STYLE_NOTES_PATH = PROJECT_ROOT / "feedback" / get_company_slug() / "style_notes.txt"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "drafts"
DRAFTS_MD_DIRNAME = "drafts_md"
DRAFTS_PDF_DIRNAME = "drafts_pdf"
DRAFTS_JSON_DIRNAME = "drafts_json"

DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct-Turbo"
SERVERLESS_MODEL_FALLBACK_CHAIN = WRITING_MODEL_FALLBACK_CHAIN
# First fallback after 397B; kept for backward-compatible imports.
SERVERLESS_FALLBACK_MODEL = SERVERLESS_MODEL_FALLBACK_CHAIN[1]
DEFAULT_AUTHOR_NAME = "Jonathan Gil"
DEFAULT_AUTHOR_CREDENTIALS = _PROFILE.AUTHOR_CREDENTIALS

# Together serverless pricing (USD per 1M tokens). Source: docs.together.ai/docs/serverless/models
TOGETHER_MODEL_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": {"input": 1.04, "output": 1.04},
    "Qwen/Qwen3.5-397B-A17B": {"input": 0.60, "output": 3.60},
    "Qwen/Qwen3-235B-A22B-Instruct-2507-tput": {"input": 0.20, "output": 0.60},
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
    "Qwen/Qwen2.5-7B-Instruct-Turbo": {"input": 0.30, "output": 0.30},
    "Qwen/Qwen2.5-72B-Instruct-Turbo": {"input": 0.90, "output": 0.90},
}

# Anthropic API pricing (USD per 1M tokens). Source: docs.anthropic.com/en/docs/about-claude/pricing
CLAUDE_MODEL_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
}

METRO_LOCATIONS = list(_PROFILE.METRO_LOCATIONS)


def format_metro_locations_list() -> str:
    """Comma-separated allowlist injected into the write prompt; must match METRO_LOCATIONS."""
    return ", ".join(METRO_LOCATIONS)

GENERIC_OPENERS = [
    "In today's world",
    "As a homeowner",
    "When it comes to",
    "Storm season is here",
]

MIN_CITATION_COUNT = 2
MAX_CITATION_COUNT = 6

SOURCE_STRATEGIES = ("auto", "best", "combine")
WRITE_RUNNER_ENV = "WRITE_RUNNER"
DEFAULT_WRITE_RUNNER = "blog_automation.pipeline.write_serverless"


def write_log_prefix() -> str:
    runner = os.getenv(WRITE_RUNNER_ENV, DEFAULT_WRITE_RUNNER)
    if "write_serverless" in runner:
        return "[write_serverless]"
    return "[write]"


def write_runner_name() -> str:
    return os.getenv(WRITE_RUNNER_ENV, DEFAULT_WRITE_RUNNER)


def tag_generation_report(
    generation_report: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    generation_report["runner"] = write_runner_name()
    generation_report["mode"] = mode
    return generation_report


def load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list in {path}")

    return data


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_feedback_json(path: Path | None) -> str:
    return load_approval_rewrite_context(path)["approval_feedback"]


REVISION_MODE_EDITORIAL = "editorial"
REVISION_MODE_FACTUAL = "factual"
REVISION_MODES = (REVISION_MODE_EDITORIAL, REVISION_MODE_FACTUAL)

FEEDBACK_PREFIX_TO_MODE: dict[str, str] = {
    "edit": REVISION_MODE_EDITORIAL,
    "editorial": REVISION_MODE_EDITORIAL,
    "sources": REVISION_MODE_FACTUAL,
    "stats": REVISION_MODE_FACTUAL,
    "factual": REVISION_MODE_FACTUAL,
}

FACTUAL_FEEDBACK_PATTERNS = (
    r"\bstat(?:istic)?s?\b",
    r"\bcitations?\b",
    r"\bcite\b",
    r"\bcited\b",
    r"\bsources?\b",
    r"\barticles?\b",
    r"\bpercent(?:age)?\b",
    r"\bfigures?\b",
    r"\bnumbers?\b",
    r"\bdata\b",
    r"\bclaims?\b",
    r"\bfacts?\b",
    r"\bfrom the (?:story|article|source|report)\b",
    r"\badd(?:ing)? (?:a |another )?(?:stat|figure|number|citation)\b",
    r"\$\d",
    r"\b\d+\s*(?:%|percent)\b",
)

EDITORIAL_FEEDBACK_PATTERNS = (
    r"\btone\b",
    r"\bwordy\b",
    r"\bshorter\b",
    r"\blonger\b",
    r"\bconcise\b",
    r"\bopening\b",
    r"\bintro(?:duction)?\b",
    r"\bparagraph\b",
    r"\bheadings?\b",
    r"\bh2\b",
    r"\bfaq\b",
    r"\bsalesy\b",
    r"\bpromotional\b",
    r"\brephrase\b",
    r"\breword\b",
    r"\bwording\b",
    r"\bless repetitive\b",
    r"\brepetitive\b",
    r"\bgeneric\b",
    r"\bbyline\b",
    r"\bcta\b",
    r"\bvalidation\b",
)


def parse_feedback_prefix(text: str) -> tuple[str, str | None]:
    """Return cleaned feedback text and optional explicit revision mode from a prefix."""
    match = re.match(
        r"^(edit|editorial|sources|stats|factual)\s*:\s*(.*)$",
        text.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return text.strip(), None
    mode = FEEDBACK_PREFIX_TO_MODE[match.group(1).lower()]
    cleaned = match.group(2).strip()
    return cleaned or text.strip(), mode


def classify_revision_mode(feedback_texts: list[str]) -> tuple[str, str]:
    """Pick editorial vs factual revision mode from Slack feedback."""
    explicit_modes: list[str] = []
    cleaned_texts: list[str] = []
    for text in feedback_texts:
        cleaned, explicit_mode = parse_feedback_prefix(text)
        if cleaned:
            cleaned_texts.append(cleaned)
        if explicit_mode:
            explicit_modes.append(explicit_mode)

    if REVISION_MODE_FACTUAL in explicit_modes:
        return REVISION_MODE_FACTUAL, "explicit prefix (sources/stats/factual)"
    if explicit_modes:
        return REVISION_MODE_EDITORIAL, "explicit prefix (edit/editorial)"

    combined = "\n".join(cleaned_texts).lower()
    if not combined.strip():
        return REVISION_MODE_EDITORIAL, "default"

    factual_score = sum(1 for pattern in FACTUAL_FEEDBACK_PATTERNS if re.search(pattern, combined))
    editorial_score = sum(1 for pattern in EDITORIAL_FEEDBACK_PATTERNS if re.search(pattern, combined))

    if factual_score > editorial_score and factual_score > 0:
        return REVISION_MODE_FACTUAL, f"keyword match (factual={factual_score}, editorial={editorial_score})"
    if editorial_score > 0:
        return REVISION_MODE_EDITORIAL, f"keyword match (factual={factual_score}, editorial={editorial_score})"
    return REVISION_MODE_EDITORIAL, "default"


def feedback_items_to_texts(feedback: Any) -> list[str]:
    texts: list[str] = []
    if not isinstance(feedback, list):
        return texts
    for item in feedback:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
        else:
            text = str(item).strip()
        if text:
            texts.append(text)
    return texts


def format_approval_feedback_lines(feedback_texts: list[str]) -> str:
    lines: list[str] = []
    for index, text in enumerate(feedback_texts, start=1):
        cleaned, _ = parse_feedback_prefix(text)
        if cleaned:
            lines.append(f"{index}. {cleaned}")
    return "\n".join(lines)


def classify_revision_mode_from_record(record: dict[str, Any]) -> tuple[str, str]:
    approval = record.get("approval") if isinstance(record.get("approval"), dict) else {}
    feedback_source = approval.get("feedback") if approval.get("feedback") is not None else record.get("feedback", [])
    return classify_revision_mode(feedback_items_to_texts(feedback_source))


def update_record_revision_mode(record: dict[str, Any]) -> tuple[str, str]:
    mode, reason = classify_revision_mode_from_record(record)
    record["revision_mode"] = mode
    record["revision_mode_reason"] = reason
    return mode, reason


def load_approval_rewrite_context(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "approval_feedback": "",
            "previous_draft": "",
            "replace_draft_path": None,
            "revision_mode": "",
            "revision_mode_reason": "",
            "writing_prompt_id": DEFAULT_WRITING_PROMPT_ID,
        }
    if not path.exists():
        raise FileNotFoundError(f"Feedback JSON not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    approval = data.get("approval") if isinstance(data.get("approval"), dict) else {}
    feedback_source = approval.get("feedback") if approval.get("feedback") is not None else data.get("feedback", [])
    feedback_texts = feedback_items_to_texts(feedback_source)
    approval_feedback = format_approval_feedback_lines(feedback_texts)
    revision_mode, revision_mode_reason = classify_revision_mode(feedback_texts)

    replace_draft_path = resolve_replace_draft_path(data)
    previous_draft = ""
    if replace_draft_path and replace_draft_path.is_file():
        previous_draft = replace_draft_path.read_text(encoding="utf-8")
    elif replace_draft_path:
        print(
            f"{write_log_prefix()} Warning: Previous draft not found at {replace_draft_path}. "
            "Rewrite will use Slack feedback only."
        )

    return {
        "approval_feedback": approval_feedback,
        "previous_draft": previous_draft,
        "replace_draft_path": replace_draft_path,
        "revision_mode": revision_mode if approval_feedback else "",
        "revision_mode_reason": revision_mode_reason if approval_feedback else "",
        "writing_prompt_id": _writing_prompt_id_from_record(data),
    }


def _writing_prompt_id_from_record(record: dict[str, Any]) -> str:
    writing_prompt = record.get("writing_prompt")
    if isinstance(writing_prompt, dict):
        prompt_id = str(writing_prompt.get("id") or "").strip()
        if prompt_id:
            return prompt_id
    legacy = str(record.get("writing_prompt_id") or "").strip()
    return legacy or DEFAULT_WRITING_PROMPT_ID


def resolve_replace_draft_path(record: dict[str, Any]) -> Path | None:
    """Return the draft file that a rewrite should replace, if known."""
    approval = record.get("approval") if isinstance(record.get("approval"), dict) else {}
    for key in ("revision_draft_path",):
        draft_rel = str(approval.get(key, "")).strip()
        if draft_rel:
            draft_path = Path(draft_rel)
            if not draft_path.is_absolute():
                draft_path = PROJECT_ROOT / draft_path
            if draft_path.is_file():
                return draft_path
    for key in ("revision_draft_path", "draft_path"):
        draft_rel = str(record.get(key, "")).strip()
        if not draft_rel:
            continue
        draft_path = Path(draft_rel)
        if not draft_path.is_absolute():
            draft_path = PROJECT_ROOT / draft_path
        if draft_path.is_file():
            return draft_path
    return None


def persist_revision_mode_to_approval_json(path: Path, mode: str, reason: str) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    approval = data.setdefault("approval", {})
    if not isinstance(approval, dict):
        approval = {}
        data["approval"] = approval
    approval["revision_mode"] = mode
    approval["revision_mode_reason"] = reason
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def source_display_name(source: dict[str, Any]) -> str:
    domain = source.get("domain") or ""
    for marker, display_name in _PROFILE.OUTLET_NAME_BY_DOMAIN_MARKER.items():
        if marker in domain:
            return display_name
    return domain or "Source"


VENDOR_PRESS_RELEASE_HINT = (
    "Editorial note: vendor/industry press release — cite the news outlet as authority; "
    "do not promote the company named in the headline or quote its executives as the expert voice."
)

CTA_SENTENCE = _PROFILE.CTA_SENTENCE
BYLINE_SERVICE_SENTENCE = _PROFILE.BYLINE_SERVICE_SENTENCE

COMPETITOR_VENDOR_BRANDS = [
    "stormarmour",
    "storm armour",
]

ROOF_SERVICE_BRIDGE_PATTERNS = [
    r"\broof inspection\b",
    r"\broof replacement\b",
    r"\bflashing\b",
    r"\baging roof\b",
    r"\bimproperly installed roof\b",
    r"\bnew roof\b",
    r"\broof system\b",
    r"\bmissing shingle\b",
]


def _is_vendor_press_release_title(title: str) -> bool:
    return bool(
        re.search(
            r"\b(Calls for|announces|unveils|launches|warns|urges|Co-Founder|CEO of)\b",
            title,
            flags=re.IGNORECASE,
        )
    )


def format_sources_block(evaluated_sources: list[dict[str, Any]]) -> str:
    blocks = []

    for index, item in enumerate(evaluated_sources, start=1):
        source = item.get("source", item)
        content = str(source.get("content", "")).strip()
        excerpt = re.sub(r"\s+", " ", content)[:1800]
        title = source.get("title") or item.get("title", "")
        published_date = source.get("published_date", "")
        outlet = source_display_name(source)
        url = source.get("url", item.get("url", ""))
        lines = [
            f"Source {index}: {title}",
            f"Outlet: {outlet}",
            f"URL: {url}",
            f"Published: {published_date}",
            f"Strategy cluster: {item.get('strategy_cluster', source.get('strategy_cluster', ''))}",
            f"Pillar topic: {item.get('pillar_topic', source.get('pillar_topic', ''))}",
            f"Recommended angle: {item.get('recommended_angle', '')}",
            f"Evaluation reason: {item.get('reason', '')}",
            f"Citation format for this source: (Source: [{outlet}]({url}), Month Year)",
        ]
        if _is_vendor_press_release_title(title):
            lines.append(VENDOR_PRESS_RELEASE_HINT)
        lines.append(f"Excerpt: {excerpt}")
        blocks.append("\n".join(lines))

    body = "\n\n---\n\n".join(blocks)
    if len(evaluated_sources) >= 2:
        preamble = (
            f"SOURCES NOTE: {len(evaluated_sources)} sources below — use **exactly 3–6** linked "
            "citations total and include **at least one citation from each** outlet/domain.\n\n---\n\n"
        )
        return preamble + body
    return body


def source_sort_key(item: dict[str, Any]) -> tuple[float, int, int, int, int]:
    scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
    return (
        float(item.get("weighted_score") or 0),
        int(scores.get("roofing_relevance") or 0),
        int(scores.get("actionability") or 0),
        int(scores.get("source_authority") or 0),
        int(scores.get("local_relevance") or 0),
    )


def source_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("source", {}).get("title") or "Untitled source")


def select_sources_for_draft(
    sources: list[dict[str, Any]],
    strategy: str = "auto",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pick whether to combine sources or focus one draft on the best source."""
    if strategy not in SOURCE_STRATEGIES:
        raise ValueError(f"Unknown source strategy: {strategy}")
    if not sources:
        return [], {
            "strategy": strategy,
            "mode": "none",
            "reason": "No kept sources were provided.",
            "available_source_count": 0,
            "selected_source_count": 0,
            "selected_titles": [],
        }

    ranked = sorted(sources, key=source_sort_key, reverse=True)
    best = ranked[0]

    if strategy == "best":
        selected = [best]
        mode = "best"
        reason = "Forced best-source mode selected the strongest evaluated source."
    elif strategy == "combine":
        selected = ranked
        mode = "combine"
        reason = "Forced combine mode included all kept sources in one draft."
    elif len(ranked) == 1:
        selected = [best]
        mode = "best"
        reason = "Only one kept source was available."
    else:
        selected = ranked
        mode = "combine"
        reason = (
            f"Auto mode included all {len(selected)} kept source(s) "
            "so the draft cites every outlet evaluate kept."
        )

    decision = {
        "strategy": strategy,
        "mode": mode,
        "reason": reason,
        "available_source_count": len(sources),
        "selected_source_count": len(selected),
        "selected_titles": [source_title(source) for source in selected],
        "available_titles": [source_title(source) for source in ranked],
    }
    return selected, decision


def validation_checklist_block(
    author_name: str,
    author_credentials: str,
    *,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
) -> str:
    variant = get_writing_prompt_variant(writing_prompt_id)
    return (
        "- One H1 title at the top.\n"
        f"- Opening paragraph before the summary block is 50-120 words, {variant.opening_style}, news-anchored.\n"
        f"- Summary block after opening with {variant.summary_heading_markdown}, "
        f"**{SUMMARY_HEADING_WHO}:**, **{SUMMARY_HEADING_WHAT}:**, **{SUMMARY_HEADING_WHEN}:**.\n"
        "- Every ## heading is a question ending with ?, except the exact heading `## FAQ`.\n"
        "- At least one markdown comparison table.\n"
        "- Exactly 3–6 linked inline citations formatted `(Source: [Outlet Name](URL), Month Year)` using URLs from SOURCES — not 2, not 7+.\n"
        "- When SOURCES lists 2+ sources, include at least one linked citation from each outlet/domain while staying within 3–6 total.\n"
        f"- At least 6 different location names from this allowlist woven into the prose: {format_metro_locations_list()}.\n"
        "- Insurance/policy posts must bridge exclusions to roof inspection, flashing, maintenance, or replacement.\n"
        "- Exactly 8 FAQ questions as ### H3 headings under `## FAQ` (answers 3-5 sentences each).\n"
        f'- Exact author byline: "Written by {author_name}, {author_credentials}. {BYLINE_SERVICE_SENTENCE}"\n'
        f'- Exact CTA EXACTLY ONCE, at the very end before the author byline: "{CTA_SENTENCE}"\n'
        f"- Never promote competitor/vendor brands from SOURCES as the expert voice — {_PROFILE.COMPANY_SHORT} is the contractor authority.\n"
        "- No generic openers such as In today's world, As a homeowner, When it comes to, or Storm season is here."
    )


def build_first_draft_prompt(
    prompt_template: str,
    sources: list[dict[str, Any]],
    style_notes: str,
    author_name: str,
    author_credentials: str,
    approval_feedback: str = "",
    *,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
) -> str:
    sources_block = format_sources_block(sources)
    feedback_parts = [style_notes.strip()]
    if approval_feedback.strip():
        feedback_parts.append("Slack approval feedback:\n" + approval_feedback.strip())
    style_block = "\n\n".join(part for part in feedback_parts if part) or "No editor feedback recorded yet."

    base_prompt = prompt_template.format(
        sources_block=sources_block,
        today=date.today().isoformat(),
        author_name=author_name,
        author_credentials=author_credentials,
        metro_locations_list=format_metro_locations_list(),
    )

    checklist = validation_checklist_block(
        author_name,
        author_credentials,
        writing_prompt_id=writing_prompt_id,
    )
    indented_checklist = "\n".join(f"  {line}" for line in checklist.splitlines())
    return (
        f"{base_prompt}\n\n---\n\n"
        "RECENT EDITOR FEEDBACK TO APPLY:\n"
        f"{style_block}\n\n"
        "---\n\n"
        "IMPORTANT SOURCE USE RULES:\n"
        "- Do not invent facts, statistics, dates, credentials, review counts, project counts, or license numbers.\n"
        "- Do not name court cases, legislation, or policy exclusions unless they appear explicitly in SOURCES.\n"
        "- Do not introduce unrelated regulatory topics (e.g., firearms exclusions in a roof-insurance post).\n"
        "- Before outputting, silently re-read the draft against SOURCES and fix fabrications, uncited numbers, and story-type mismatches.\n"
        "- Include exactly 3–6 linked citations from SOURCES — not 2, not 7+; place most in body ## sections, not every FAQ.\n"
        "- When SOURCES lists 2+ sources, include at least one citation linked to each outlet/domain within the 3–6 total.\n"
        "- Keep all headings and FAQ questions in question format.\n"
        "- Use the exact FAQ format: ## FAQ, then exactly eight H3 question headings and 3-5 sentence answers.\n"
        "- After the opening paragraph, include the summary block with all four labeled lines.\n"
        "- Do not format the author byline as a heading.\n"
        "- Pass every automated validation check:\n"
        f"{indented_checklist}\n"
    )


def build_editorial_revision_prompt(
    *,
    style_notes: str,
    author_name: str,
    author_credentials: str,
    approval_feedback: str,
    previous_draft: str,
) -> str:
    checklist = validation_checklist_block(author_name, author_credentials)
    style_block = style_notes.strip() or "No standing style notes recorded yet."
    return (
        f"You are revising an existing {_PROFILE.COMPANY_NAME} blog draft for {_PROFILE.METRO_AREA} homeowners.\n\n"
        f"AUTHOR: {author_name}, {author_credentials}\n"
        f"TODAY'S DATE: {date.today().isoformat()}\n\n"
        "REVISION TYPE: EDITORIAL — fix wording, structure, tone, and validation issues only.\n\n"
        "EDITOR FEEDBACK (apply every item):\n"
        f"{approval_feedback.strip()}\n\n"
        "STANDING STYLE NOTES:\n"
        f"{style_block}\n\n"
        "GROUNDING RULES:\n"
        "- Do not invent new statistics, dates, credentials, or citations.\n"
        "- Keep existing facts and citations unless feedback explicitly asks to change them.\n"
        "- Preserve sections that already satisfy the feedback; change only what is needed.\n\n"
        "VALIDATION CHECKLIST (every item is required):\n"
        f"{checklist}\n\n"
        "Return the complete revised Markdown draft only. No preamble or meta-commentary.\n\n"
        "PREVIOUS DRAFT TO REVISE:\n"
        f"```\n{previous_draft.strip()}\n```\n"
    )


def build_factual_revision_prompt(
    sources: list[dict[str, Any]],
    style_notes: str,
    author_name: str,
    author_credentials: str,
    approval_feedback: str,
    previous_draft: str,
) -> str:
    sources_block = format_sources_block(sources)
    checklist = validation_checklist_block(author_name, author_credentials)
    style_block = style_notes.strip() or "No standing style notes recorded yet."
    return (
        f"You are revising an existing {_PROFILE.COMPANY_NAME} blog draft using source material.\n\n"
        f"AUTHOR: {author_name}, {author_credentials}\n"
        f"TODAY'S DATE: {date.today().isoformat()}\n\n"
        "REVISION TYPE: FACTUAL — update statistics, citations, and claims using the sources below.\n\n"
        "EDITOR FEEDBACK (apply every item):\n"
        f"{approval_feedback.strip()}\n\n"
        "STANDING STYLE NOTES:\n"
        f"{style_block}\n\n"
        "SOURCES TO DRAW FROM:\n"
        f"{sources_block}\n\n"
        "SOURCE USE RULES:\n"
        "- Update statistics and citations only where feedback requests it.\n"
        "- Do not invent facts, statistics, dates, credentials, or license numbers.\n"
        "- Use 3 to 5 cited statistics total, formatted `(Source: Outlet Name, Month Year)`.\n"
        "- Keep unchanged sections verbatim where possible; do not rewrite unrelated prose.\n\n"
        "VALIDATION CHECKLIST (every item is required):\n"
        f"{checklist}\n\n"
        "Return the complete revised Markdown draft only. No preamble or meta-commentary.\n\n"
        "PREVIOUS DRAFT TO REVISE:\n"
        f"```\n{previous_draft.strip()}\n```\n"
    )


def build_draft_prompt(
    prompt_template: str,
    sources: list[dict[str, Any]],
    style_notes: str,
    author_name: str,
    author_credentials: str,
    approval_feedback: str = "",
    previous_draft: str = "",
    revision_mode: str | None = None,
    *,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
) -> str:
    is_rewrite = bool(previous_draft.strip() and approval_feedback.strip())
    if not is_rewrite:
        return build_first_draft_prompt(
            prompt_template,
            sources,
            style_notes,
            author_name,
            author_credentials,
            approval_feedback,
            writing_prompt_id=writing_prompt_id,
        )

    mode = revision_mode or REVISION_MODE_EDITORIAL
    if mode == REVISION_MODE_FACTUAL:
        return build_factual_revision_prompt(
            sources,
            style_notes,
            author_name,
            author_credentials,
            approval_feedback,
            previous_draft,
        )
    return build_editorial_revision_prompt(
        style_notes=style_notes,
        author_name=author_name,
        author_credentials=author_credentials,
        approval_feedback=approval_feedback,
        previous_draft=previous_draft,
    )


def build_prompt(
    prompt_template: str,
    sources: list[dict[str, Any]],
    style_notes: str,
    author_name: str,
    author_credentials: str,
    approval_feedback: str = "",
    previous_draft: str = "",
    revision_mode: str | None = None,
    *,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
) -> str:
    return build_draft_prompt(
        prompt_template,
        sources,
        style_notes,
        author_name,
        author_credentials,
        approval_feedback,
        previous_draft,
        revision_mode,
        writing_prompt_id=writing_prompt_id,
    )


def get_together_client():
    try:
        from together import Together
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: together. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    api_key = os.getenv("TOGETHER_API_KEY")
    if not api_key:
        raise EnvironmentError("TOGETHER_API_KEY is not set. Add it to .env.")

    return Together(api_key=api_key)


def extract_usage(response) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
        }

    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        "cached_tokens": int(getattr(usage, "cached_tokens", 0) or 0),
    }


def estimate_token_cost_usd(model: str, usage: dict[str, int]) -> dict[str, Any]:
    pricing = CLAUDE_MODEL_PRICING_PER_MILLION.get(model)
    pricing_source = "anthropic_catalog"
    if not pricing:
        pricing = TOGETHER_MODEL_PRICING_PER_MILLION.get(model)
        pricing_source = "together_catalog"
    if not pricing:
        return {
            "total": None,
            "input": None,
            "output": None,
            "currency": "USD",
            "pricing_per_million_tokens": None,
            "pricing_source": "unavailable",
            "note": "No catalog pricing for this model. Dedicated endpoints may bill per minute separately.",
        }

    input_tokens = max(0, usage["prompt_tokens"] - usage["cached_tokens"])
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    cached_cost = (usage["cached_tokens"] / 1_000_000) * pricing.get("cached_input", pricing["input"])
    output_cost = (usage["completion_tokens"] / 1_000_000) * pricing["output"]
    total = input_cost + cached_cost + output_cost

    return {
        "total": round(total, 6),
        "input": round(input_cost + cached_cost, 6),
        "output": round(output_cost, 6),
        "currency": "USD",
        "pricing_per_million_tokens": pricing,
        "pricing_source": pricing_source,
    }


def build_generation_report(
    *,
    model_requested: str,
    model_used: str,
    model_returned_by_api: str | None,
    elapsed_seconds: float,
    usage: dict[str, int] | None,
    endpoint_session_seconds: float | None = None,
    endpoint_management_used: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "model_requested": model_requested,
        "model_used": model_used,
        "model_returned_by_api": model_returned_by_api,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "endpoint_management_used": endpoint_management_used,
    }

    if usage is not None:
        report["usage"] = usage
        report["estimated_cost_usd"] = {
            "tokens": estimate_token_cost_usd(model_used, usage),
        }
    else:
        report["usage"] = None
        report["estimated_cost_usd"] = None

    if endpoint_management_used and endpoint_session_seconds is not None:
        report["endpoint_session_seconds"] = round(endpoint_session_seconds, 2)
        per_minute = os.getenv("TOGETHER_ENDPOINT_COST_PER_MINUTE", "").strip()
        if per_minute:
            endpoint_cost = (endpoint_session_seconds / 60.0) * float(per_minute)
            report.setdefault("estimated_cost_usd", {})["endpoint"] = {
                "total": round(endpoint_cost, 4),
                "currency": "USD",
                "cost_per_minute": float(per_minute),
                "pricing_source": "env:TOGETHER_ENDPOINT_COST_PER_MINUTE",
                "note": "Endpoint uptime cost is separate from token usage.",
            }
            if report["estimated_cost_usd"].get("tokens", {}).get("total") is not None:
                report["estimated_cost_usd"]["combined_total"] = round(
                    report["estimated_cost_usd"]["tokens"]["total"] + endpoint_cost,
                    4,
                )

    return report


THINKING_LEAK_MARKERS = (
    "thinking process:",
    "analyze the request:",
    "verify internal checks",
)


def model_prefers_reasoning_disabled(model: str) -> bool:
    """Hybrid Together models that default to thinking/reasoning before answering."""
    normalized = model.lower()
    return any(
        marker in normalized
        for marker in (
            "qwen3.5",
            "qwen3.6",
            "glm-5",
            "kimi-k2",
            "nemotron-3-ultra",
            "deepseek-v4",
            "cogito-v2",
        )
    )


def together_chat_completion_kwargs(model: str) -> dict[str, Any]:
    """Disable thinking for hybrid models so blog drafts land in `content`."""
    if not model_prefers_reasoning_disabled(model):
        return {}

    return {
        "reasoning": {"enabled": False},
        "chat_template_kwargs": {"enable_thinking": False},
    }


def strip_embedded_thinking_blocks(text: str) -> str:
    think_block = re.compile(
        rf"{'<'}think{'>'}.*?{'<'}/think{'>'}",
        flags=re.DOTALL | re.IGNORECASE,
    )
    redacted_block = re.compile(
        rf"{'<'}redacted_thinking{'>'}.*?{'<'}/redacted_thinking{'>'}",
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = think_block.sub("", text).strip()
    return redacted_block.sub("", cleaned).strip()


def extract_markdown_draft(text: str) -> str:
    """Keep blog Markdown and drop leaked planning/thinking preambles."""
    cleaned = strip_embedded_thinking_blocks(text.strip())
    if not cleaned:
        return ""

    h1_match = re.search(r"^#\s+\S", cleaned, flags=re.MULTILINE)
    if h1_match:
        return cleaned[h1_match.start() :].strip()

    preview = cleaned[:300].lower()
    if any(marker in preview for marker in THINKING_LEAK_MARKERS):
        return ""

    return cleaned


def extract_assistant_draft(message: Any) -> str:
    content = str(getattr(message, "content", None) or "").strip()
    return extract_markdown_draft(content)


def is_together_model_not_available_error(exc: BaseException) -> bool:
    return "model_not_available" in str(exc)


def serverless_fallback_models(model: str) -> list[str]:
    """Models to try after ``model`` when Together reports ``model_not_available``."""
    return fallback_models_after(model, WRITING_MODEL_FALLBACK_CHAIN)


def serverless_model_attempt_sequence(model: str, *, allow_serverless_fallback: bool) -> list[str]:
    return model_attempt_sequence(
        model,
        WRITING_MODEL_FALLBACK_CHAIN,
        allow_fallback=allow_serverless_fallback,
    )


def call_together_chat_with_fallback(
    client: Any,
    *,
    model: str,
    fallback_chain: tuple[str, ...],
    messages: list[dict[str, str]],
    allow_fallback: bool = True,
    log_prefix: str = "[together]",
    **completion_kwargs: Any,
) -> tuple[Any, str, str, dict[str, Any]]:
    """Call Together chat completions, retrying down ``fallback_chain`` on model_not_available."""
    model_requested = model
    model_used = model
    models_to_try = model_attempt_sequence(model, fallback_chain, allow_fallback=allow_fallback)

    started_at = time.monotonic()
    response = None
    last_exc: Exception | None = None

    for attempt_index, active_model in enumerate(models_to_try):
        try:
            if attempt_index > 0:
                print(
                    f"{log_prefix} Model unavailable on Together serverless. "
                    f"Retrying with {active_model}."
                )
            response = client.chat.completions.create(
                model=active_model,
                messages=messages,
                **completion_kwargs,
                **together_chat_completion_kwargs(active_model),
            )
            model_used = active_model
            break
        except Exception as exc:
            last_exc = exc
            if not allow_fallback or not is_together_model_not_available_error(exc):
                raise
            if attempt_index >= len(models_to_try) - 1:
                raise

    if response is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Together chat call failed without a response.")

    metadata = build_generation_report(
        model_requested=model_requested,
        model_used=model_used,
        model_returned_by_api=getattr(response, "model", None),
        elapsed_seconds=time.monotonic() - started_at,
        usage=extract_usage(response),
    )
    return response, model_requested, model_used, metadata


def generate_draft(
    prompt: str,
    model: str,
    *,
    allow_serverless_fallback: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Generate a blog draft using the active LLM provider (Claude or Together)."""
    from blog_automation.llm_client import WRITING_SYSTEM_PROMPT, chat_completion, get_llm_provider

    log = write_log_prefix()
    max_tokens = resolve_write_max_tokens()
    if get_llm_provider() == "anthropic":
        content, metadata = chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": WRITING_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.35,
            max_tokens=max_tokens,
            log_prefix=log,
            role="writing",
        )
        draft = extract_markdown_draft(content)
        if not draft:
            print(
                f"{log} Warning: Model returned no Markdown draft starting with H1; "
                "validation will retry."
            )
        return draft, metadata
    return generate_with_together(
        prompt,
        model,
        allow_serverless_fallback=allow_serverless_fallback,
    )


def generate_with_together(
    prompt: str,
    model: str,
    *,
    allow_serverless_fallback: bool = True,
) -> tuple[str, dict[str, Any]]:
    client = get_together_client()
    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior home-services content strategist. "
                "Return only the complete Markdown blog draft starting with one H1 title line. "
                "Do not include planning notes, analysis, or a thinking process."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    def call_model(active_model: str):
        extra_kwargs = together_chat_completion_kwargs(active_model)
        if extra_kwargs:
            print(
                f"{write_log_prefix()} Disabled reasoning/thinking for {active_model} "
                "(draft-only response)."
            )
        return client.chat.completions.create(
            model=active_model,
            messages=messages,
            temperature=0.35,
            max_tokens=resolve_write_max_tokens(),
            **extra_kwargs,
        )

    estimated_seconds = 180.0
    started_at = time.monotonic()
    model_requested = model
    model_used = model
    models_to_try = serverless_model_attempt_sequence(
        model,
        allow_serverless_fallback=allow_serverless_fallback,
    )

    response = None
    last_exc: Exception | None = None
    for attempt_index, active_model in enumerate(models_to_try):
        try:
            if attempt_index > 0:
                print(
                    f"{write_log_prefix()} Model unavailable on Together serverless. "
                    f"Retrying with {active_model}."
                )
            progress_label = (
                f"{write_log_prefix()} Generating draft"
                if attempt_index == 0
                else f"{write_log_prefix()} Generating draft ({active_model})"
            )
            response = run_with_progress(
                progress_label,
                lambda active_model=active_model: call_model(active_model),
                estimated_seconds=estimated_seconds,
            )
            model_used = active_model
            break
        except Exception as exc:
            last_exc = exc
            if not allow_serverless_fallback or not is_together_model_not_available_error(exc):
                raise
            if attempt_index >= len(models_to_try) - 1:
                raise

    if response is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Together generation failed without a response.")

    elapsed_seconds = time.monotonic() - started_at
    usage = extract_usage(response)
    metadata = build_generation_report(
        model_requested=model_requested,
        model_used=model_used,
        model_returned_by_api=getattr(response, "model", None),
        elapsed_seconds=elapsed_seconds,
        usage=usage,
    )

    draft = extract_assistant_draft(response.choices[0].message)
    if not draft:
        print(
            f"{write_log_prefix()} Warning: Model returned planning/thinking text with no "
            "Markdown draft; validation will retry."
        )

    return draft, metadata


def draft_subdirs(output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path, Path]:
    """Return Markdown, PDF, and validation JSON directories under the drafts root."""
    return (
        output_dir / DRAFTS_MD_DIRNAME,
        output_dir / DRAFTS_PDF_DIRNAME,
        output_dir / DRAFTS_JSON_DIRNAME,
    )


def ensure_draft_subdirs(output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path, Path]:
    md_dir, pdf_dir, json_dir = draft_subdirs(output_dir)
    for directory in (md_dir, pdf_dir, json_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return md_dir, pdf_dir, json_dir


def resolve_drafts_root(path: Path) -> Path:
    if path.parent.name in {DRAFTS_MD_DIRNAME, DRAFTS_PDF_DIRNAME, DRAFTS_JSON_DIRNAME}:
        return path.parent.parent
    if path.parent.name == "drafts" or path.parent == DEFAULT_OUTPUT_DIR:
        return path.parent
    return DEFAULT_OUTPUT_DIR


def draft_stem_from_path(draft_path: Path) -> str:
    name = draft_path.name
    if name.endswith("-validation.json"):
        return name[: -len("-validation.json")]
    return draft_path.stem


def draft_run_id_from_path(draft_path: Path | str) -> str:
    """Return the six-digit HHMMSS run id from a draft filename (e.g. ``021058``)."""
    stem = draft_stem_from_path(Path(draft_path))
    match = re.match(r"^\d{4}-\d{2}-\d{2}-(\d{6})-", stem)
    if match:
        return match.group(1)
    return stem


def draft_pdf_path(markdown_path: Path) -> Path:
    stem = draft_stem_from_path(markdown_path)
    _, pdf_dir, _ = draft_subdirs(resolve_drafts_root(markdown_path))
    return pdf_dir / f"{stem}.pdf"


def draft_validation_json_path(draft_path: Path) -> Path:
    stem = draft_stem_from_path(draft_path)
    _, _, json_dir = draft_subdirs(resolve_drafts_root(draft_path))
    return json_dir / f"{stem}-validation.json"


def markdown_draft_paths(output_dir: Path = DEFAULT_OUTPUT_DIR) -> list[Path]:
    """List Markdown drafts in the typed subdir, with legacy flat-directory fallback."""
    md_dir, _, _ = draft_subdirs(output_dir)
    drafts = sorted(md_dir.glob("*.md")) if md_dir.exists() else []
    if drafts:
        return drafts
    return sorted(
        path
        for path in output_dir.glob("*.md")
        if path.is_file() and not path.name.endswith("-validation.md")
    )


def latest_markdown_draft(output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    drafts = markdown_draft_paths(output_dir)
    if not drafts:
        raise FileNotFoundError(f"No Markdown drafts found under {output_dir}")

    for draft_path in reversed(drafts):
        if draft_pdf_path(draft_path).is_file():
            return draft_path

    latest = drafts[-1]
    expected_pdf = draft_pdf_path(latest)
    raise FileNotFoundError(
        f"No PDF found for the latest draft under {output_dir}. "
        f"Expected {expected_pdf}. Run write_serverless first."
    )


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "blog-draft"


def first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "blog-draft"


def output_paths(markdown: str, output_dir: Path) -> tuple[Path, Path, Path]:
    md_dir, pdf_dir, json_dir = ensure_draft_subdirs(output_dir)
    run_stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    slug = slugify(first_heading(markdown))
    base = f"{run_stamp}-{slug}"
    return (
        md_dir / f"{base}.md",
        pdf_dir / f"{base}.pdf",
        json_dir / f"{base}-validation.json",
    )


def count_faq_pairs(markdown: str) -> int:
    faq_match = re.search(r"(^##+ FAQ\b.*)", markdown, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
    if not faq_match:
        return 0
    faq_section = faq_match.group(1)
    return len(re.findall(r"^#{3,6}\s+\S.*\?", faq_section, flags=re.MULTILINE))


def _summary_block_split_markers(writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID) -> tuple[str, ...]:
    variant = get_writing_prompt_variant(writing_prompt_id)
    return (
        variant.summary_heading_markdown,
        f"**{SUMMARY_HEADING_WHO}:**",
        f"**{SUMMARY_HEADING_WHAT}:**",
        f"**{SUMMARY_HEADING_WHEN}:**",
    )


def extract_opening_paragraph(
    markdown: str,
    *,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
) -> str:
    """First paragraph only — excludes summary block and body H2s."""
    body_without_title = re.sub(r"^# .+\n+", "", markdown).strip()
    before_first_h2 = re.split(r"\n^##\s+", body_without_title, maxsplit=1, flags=re.MULTILINE)[0]
    primary_marker = _summary_block_split_markers(writing_prompt_id)[0]
    if primary_marker in before_first_h2:
        return before_first_h2.split(primary_marker, maxsplit=1)[0].strip()
    for legacy_marker in (
        "**The short answer:**",
        "**Key takeaway:**",
        "**Bottom line:**",
    ):
        if legacy_marker in before_first_h2:
            return before_first_h2.split(legacy_marker, maxsplit=1)[0].strip()
    return before_first_h2.split("\n\n", maxsplit=1)[0].strip()


def has_summary_block(
    markdown: str,
    *,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
) -> bool:
    labels = _summary_block_split_markers(writing_prompt_id)
    return all(re.search(re.escape(label), markdown, flags=re.IGNORECASE) for label in labels)


def has_quick_answer_block(markdown: str, *, writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID) -> bool:
    """Backward-compatible alias for summary-block validation."""
    return has_summary_block(markdown, writing_prompt_id=writing_prompt_id)


def extract_citations(markdown: str) -> list[str]:
    return re.findall(r"\(Source:\s*[^)]+\)", markdown)


def extract_citation_domains(citations: list[str]) -> set[str]:
    domains: set[str] = set()
    for citation in citations:
        url_match = re.search(r"\]\((https?://[^)]+)\)", citation)
        if not url_match:
            continue
        normalized = normalize_source_url(url_match.group(1))
        if not normalized:
            continue
        domain = urlparse(normalized).netloc.lower()
        if domain:
            domains.add(domain)
    return domains


def source_item_domains(sources: list[dict[str, Any]]) -> set[str]:
    domains: set[str] = set()
    for item in sources:
        url = str(item.get("url") or item.get("source", {}).get("url") or "").strip()
        normalized = normalize_source_url(url)
        if not normalized:
            continue
        domain = urlparse(normalized).netloc.lower()
        if domain:
            domains.add(domain)
    return domains


def citations_span_multiple_outlets(
    citations: list[str],
    selected_sources: list[dict[str, Any]],
) -> bool:
    """When multiple sources were used, citations must reference every outlet domain."""
    required_domains = source_item_domains(selected_sources)
    if len(required_domains) < 2:
        return True
    cited_domains = extract_citation_domains(citations)
    return required_domains.issubset(cited_domains)


def citations_include_links(citations: list[str]) -> bool:
    if not citations:
        return False
    return all(re.search(r"\[[^\]]+\]\(https?://", citation) for citation in citations)


def source_item_urls(sources: list[dict[str, Any]]) -> set[str]:
    urls: set[str] = set()
    for item in sources:
        url = str(item.get("url") or item.get("source", {}).get("url") or "").strip()
        normalized = normalize_source_url(url)
        if normalized:
            urls.add(normalized)
    return urls


def extract_citation_urls(citations: list[str]) -> list[str]:
    urls: list[str] = []
    for citation in citations:
        url_match = re.search(r"\]\((https?://[^)]+)\)", citation)
        if not url_match:
            continue
        normalized = normalize_source_url(url_match.group(1))
        if normalized:
            urls.append(normalized)
    return urls


def citations_match_kept_source_urls(
    citations: list[str],
    selected_sources: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Every citation URL must normalize to a URL from selected_sources."""
    if not selected_sources:
        return True, []
    allowed = source_item_urls(selected_sources)
    if not allowed:
        return True, []

    invalid: list[str] = []
    seen: set[str] = set()
    for url in extract_citation_urls(citations):
        if url in allowed or url in seen:
            continue
        seen.add(url)
        invalid.append(url)
    return not invalid, invalid


PERCENTAGE_CLAIM_RE = re.compile(
    r"(?<![\d.])(?P<value>\d+(?:\.\d+)?)\s*(?:%|percent)(?!\w)",
    re.IGNORECASE,
)


def source_content_blob(sources: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in sources:
        for key in ("title", "content", "reason", "recommended_angle"):
            parts.append(str(item.get(key) or ""))
        nested = item.get("source")
        if isinstance(nested, dict):
            for key in ("title", "content"):
                parts.append(str(nested.get(key) or ""))
    return " ".join(parts)


def extract_percentage_claims(markdown: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in PERCENTAGE_CLAIM_RE.finditer(markdown):
        value = match.group("value")
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def percentage_value_in_source_blob(value: str, blob: str) -> bool:
    pattern = re.compile(
        rf"(?<![\d.]){re.escape(value)}\s*(?:%|percent)(?!\w)",
        re.IGNORECASE,
    )
    return bool(pattern.search(blob))


def percentage_claims_grounded_in_sources(
    markdown: str,
    selected_sources: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Every N% / N percent claim in the draft must appear in kept source text."""
    if not selected_sources:
        return True, []
    blob = source_content_blob(selected_sources)
    ungrounded: list[str] = []
    for value in extract_percentage_claims(markdown):
        if not percentage_value_in_source_blob(value, blob):
            ungrounded.append(f"{value}%")
    return not ungrounded, ungrounded


def count_cta_occurrences(markdown: str) -> int:
    return markdown.count(CTA_SENTENCE)


def competitor_brands_found(markdown: str) -> list[str]:
    lowered = markdown.lower()
    return [brand for brand in COMPETITOR_VENDOR_BRANDS if brand in lowered]


def count_roof_service_bridge_signals(markdown: str) -> int:
    return sum(
        1
        for pattern in ROOF_SERVICE_BRIDGE_PATTERNS
        if re.search(pattern, markdown, flags=re.IGNORECASE)
    )


def validate_draft(
    markdown: str,
    *,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
    selected_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_sources = selected_sources or []
    h2s = re.findall(r"^##\s+(.+)$", markdown, flags=re.MULTILINE)
    citations = extract_citations(markdown)
    citation_domains = sorted(extract_citation_domains(citations))
    required_domains = sorted(source_item_domains(selected_sources))
    tables = bool(re.search(r"^\|.+\|\s*$", markdown, flags=re.MULTILINE))
    locations = sorted({loc for loc in METRO_LOCATIONS if re.search(rf"\b{re.escape(loc)}\b", markdown)})
    opening_text = extract_opening_paragraph(markdown, writing_prompt_id=writing_prompt_id)
    opening_words = len(opening_text.split())
    generic_openers = [phrase for phrase in GENERIC_OPENERS if phrase.lower() in markdown[:250].lower()]
    cta_count = count_cta_occurrences(markdown)
    competitor_hits = competitor_brands_found(markdown)
    roof_bridge_count = count_roof_service_bridge_signals(markdown)
    citations_match_sources, invalid_citation_urls = citations_match_kept_source_urls(
        citations, selected_sources
    )
    percentages_grounded, ungrounded_percentage_claims = percentage_claims_grounded_in_sources(
        markdown, selected_sources
    )

    checks = {
        "has_h1": bool(re.search(r"^#\s+\S", markdown, flags=re.MULTILINE)),
        "answer_first_opening_roughly_50_to_120_words": 50 <= opening_words <= 120,
        "has_quick_answer_block": has_summary_block(markdown, writing_prompt_id=writing_prompt_id),
        "all_h2_headings_are_questions": bool(h2s) and all(h2.strip().endswith("?") or h2.strip().lower() == "faq" for h2 in h2s),
        "has_comparison_table": tables,
        "citation_count_3_to_6": MIN_CITATION_COUNT <= len(citations) <= MAX_CITATION_COUNT,
        "citations_include_links": citations_include_links(citations),
        "citations_span_multiple_outlets": citations_span_multiple_outlets(
            citations, selected_sources
        ),
        "citations_match_kept_source_urls": citations_match_sources,
        "percentage_claims_grounded_in_sources": percentages_grounded,
        "location_count_at_least_6": len(locations) >= 6,
        "bridges_to_roof_service": roof_bridge_count >= 2,
        "faq_exactly_8": count_faq_pairs(markdown) == 8,
        "has_author_byline": "Written by " in markdown and BYLINE_SERVICE_SENTENCE in markdown,
        "has_exactly_one_cta": cta_count == 1,
        "no_competitor_brand_voice": not competitor_hits,
        "no_generic_openers": not generic_openers,
    }

    return {
        "passed": all(checks.values()),
        "checks": checks,
        "h2_headings": h2s,
        "citation_count": len(citations),
        "citation_domains_found": citation_domains,
        "citation_urls_found": extract_citation_urls(citations),
        "required_source_domains": required_domains,
        "kept_source_urls": sorted(source_item_urls(selected_sources)),
        "invalid_citation_urls": invalid_citation_urls,
        "ungrounded_percentage_claims": ungrounded_percentage_claims,
        "cta_count": cta_count,
        "roof_bridge_signal_count": roof_bridge_count,
        "competitor_brands_found": competitor_hits,
        "opening_word_count": opening_words,
        "locations_found": locations,
        "faq_count": count_faq_pairs(markdown),
        "generic_openers_found": generic_openers,
        "writing_prompt": writing_prompt_metadata(get_writing_prompt_variant(writing_prompt_id)),
    }


DEFAULT_VALIDATION_MAX_ATTEMPTS = 2
DEFAULT_WRITE_MAX_TOKENS_ANTHROPIC = 6000
DEFAULT_WRITE_MAX_TOKENS_TOGETHER = 3400
WRITE_MAX_TOKENS_ENV = "WRITE_MAX_TOKENS"
CLAUDE_WRITING_MAX_TOKENS_ENV = "CLAUDE_WRITING_MAX_TOKENS"


def resolve_write_max_tokens() -> int:
    """Return max output tokens for blog draft generation for the active provider."""
    override = os.getenv(WRITE_MAX_TOKENS_ENV, "").strip()
    if override:
        return max(1, int(override))
    from blog_automation.llm_client import get_llm_provider

    if get_llm_provider() == "anthropic":
        anthropic_override = os.getenv(CLAUDE_WRITING_MAX_TOKENS_ENV, "").strip()
        if anthropic_override:
            return max(1, int(anthropic_override))
        return DEFAULT_WRITE_MAX_TOKENS_ANTHROPIC
    return DEFAULT_WRITE_MAX_TOKENS_TOGETHER


def summary_block_validation_hint(writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID) -> str:
    variant = get_writing_prompt_variant(writing_prompt_id)
    return (
        "Immediately after the opening paragraph, include "
        f"{variant.summary_heading_markdown}, "
        f"**{SUMMARY_HEADING_WHO}:**, **{SUMMARY_HEADING_WHAT}:**, and **{SUMMARY_HEADING_WHEN}:** "
        "on separate lines."
    )


VALIDATION_CHECK_HINTS: dict[str, str] = {
    "has_h1": "Start with one H1 title line: `# Your Title Here`.",
    "answer_first_opening_roughly_50_to_120_words": (
        "The opening paragraph before the summary block must be 50-120 words, news-anchored, answer-first."
    ),
    "has_quick_answer_block": summary_block_validation_hint(),
    "all_h2_headings_are_questions": (
        "Every ## H2 heading must be a question ending with ?, except the exact heading `## FAQ`."
    ),
    "has_comparison_table": "Include at least one markdown comparison table using | pipe | syntax.",
    "citation_count_3_to_6": (
        "Include exactly 3–6 linked inline citations formatted `(Source: [Outlet Name](URL), Month Year)` — "
        "not 2 or fewer, not 7 or more. Remove redundant FAQ/body cites if over 6."
    ),
    "citations_include_links": (
        "Every citation must include a markdown link to the source URL from SOURCES — "
        "e.g. `(Source: [Insurance Journal](https://...), June 2026)`."
    ),
    "citations_span_multiple_outlets": (
        "When SOURCES lists 2+ sources, include at least one linked citation from every outlet/domain "
        "in SOURCES while keeping the total between 3 and 6."
    ),
    "citations_match_kept_source_urls": (
        "Every citation URL must exactly match a URL from SOURCES — copy URLs verbatim; "
        "do not alter path segments or invent links."
    ),
    "percentage_claims_grounded_in_sources": (
        "Every percentage in the post (e.g. 33% or 33 percent) must appear in SOURCES text. "
        "Remove or rephrase ungrounded stats; use qualitative language instead."
    ),
    "llm_fact_audit_passed": (
        "Fix every factual accuracy issue flagged by the fact-audit pass — remove ungrounded stats, "
        "fix citation URLs to match SOURCES exactly, and delete invented local events or other-state framing."
    ),
    "bridges_to_roof_service": (
        "Connect insurance or policy content to roof condition — mention at least two of: "
        "roof inspection, roof replacement, flashing, aging roof, roof system, or similar."
    ),
    "location_count_at_least_6": (
        f"Name at least 6 different locations from this allowlist naturally in the prose: {format_metro_locations_list()}."
    ),
    "faq_exactly_8": (
        "Under the exact heading `## FAQ`, include exactly 8 H3 question headings ending with ? and 3-5 sentence answers."
    ),
    "has_exactly_one_cta": (
        f'Include this exact sentence EXACTLY ONCE, at the very end before the author byline: "{CTA_SENTENCE}"'
        " Remove any duplicate occurrences earlier in the post."
    ),
    "no_competitor_brand_voice": (
        "Do not promote competitor or vendor brands from SOURCES (e.g., StormArmour) as the expert voice — "
        f"attribute facts to the news outlet and write from {_PROFILE.CONTRACTOR_PERSPECTIVE_NAME}'s contractor perspective."
    ),
    "no_generic_openers": (
        "Do not use generic openers such as In today's world, As a homeowner, When it comes to, or Storm season is here in the first paragraph."
    ),
}


def format_failed_validation_feedback(
    report: dict[str, Any],
    *,
    author_name: str,
    author_credentials: str,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
) -> str:
    failed = [name for name, passed in report["checks"].items() if not passed]
    lines = [
        "The previous draft failed automated validation. Return the complete revised Markdown draft only.",
        "",
        "Fix every failed check below:",
    ]

    for name in failed:
        if name == "has_author_byline":
            hint = (
                f'Include this exact sentence as normal paragraph text, not as a heading: '
                f'"Written by {author_name}, {author_credentials}. {BYLINE_SERVICE_SENTENCE}"'
            )
        elif name == "has_quick_answer_block":
            hint = summary_block_validation_hint(writing_prompt_id)
        else:
            hint = VALIDATION_CHECK_HINTS.get(name, name.replace("_", " "))
        lines.append(f"- {hint}")

    if report.get("generic_openers_found"):
        lines.append(f"- Remove these generic openers: {', '.join(report['generic_openers_found'])}")
    if report.get("citation_count") is not None and not report["checks"].get("citation_count_3_to_6"):
        count = int(report["citation_count"])
        if count > MAX_CITATION_COUNT:
            hint = (
                f" Remove {count - MAX_CITATION_COUNT} redundant citation(s); keep cites in body H2s, "
                "not every FAQ answer."
            )
        elif count < MIN_CITATION_COUNT:
            hint = f" Add {MIN_CITATION_COUNT - count} linked citation(s) from SOURCES."
        else:
            hint = ""
        lines.append(
            f"- Current citation count: {count} (need exactly {MIN_CITATION_COUNT}–{MAX_CITATION_COUNT}).{hint}"
        )
    if not report["checks"].get("citations_span_multiple_outlets"):
        found = ", ".join(report.get("citation_domains_found") or []) or "none"
        required = ", ".join(report.get("required_source_domains") or []) or "unknown"
        lines.append(
            f"- Citation outlets cited: {found}. Required source domains: {required}. "
            "Include at least one linked citation per source when multiple sources were provided."
        )
    if not report["checks"].get("citations_match_kept_source_urls"):
        invalid = ", ".join(report.get("invalid_citation_urls") or []) or "unknown"
        allowed = ", ".join(report.get("kept_source_urls") or []) or "unknown"
        lines.append(
            f"- Invalid citation URL(s): {invalid}. Allowed URLs from SOURCES: {allowed}."
        )
    if not report["checks"].get("percentage_claims_grounded_in_sources"):
        ungrounded = ", ".join(report.get("ungrounded_percentage_claims") or []) or "unknown"
        lines.append(
            f"- Ungrounded percentage claim(s): {ungrounded}. "
            "Remove these stats or rewrite without numbers unless SOURCES contains the same figure."
        )
    fact_audit = report.get("fact_audit") or {}
    if not report["checks"].get("llm_fact_audit_passed"):
        revision_notes = str(fact_audit.get("revision_notes") or "").strip()
        if revision_notes:
            lines.append(f"- Fact-audit revision notes: {revision_notes}")
        issues = fact_audit.get("issues") or []
        error_details = [
            str(item.get("detail", "")).strip()
            for item in issues
            if isinstance(item, dict) and item.get("severity") == "error" and item.get("detail")
        ]
        if error_details:
            lines.append("- Fact-audit errors: " + "; ".join(error_details))
    if report.get("cta_count") is not None and not report["checks"].get("has_exactly_one_cta"):
        count = report["cta_count"]
        if count == 0:
            lines.append(
                f'- Current CTA count: 0 (need exactly 1 — place "{CTA_SENTENCE}" at the very end before the author byline).'
            )
        else:
            lines.append(
                f"- Current CTA count: {count} (need exactly 1 — remove {count - 1} duplicate(s), keep only the final instance immediately before the author byline)."
            )
    if report.get("competitor_brands_found"):
        lines.append(
            f"- Remove competitor/vendor brand mentions used as authority: {', '.join(report['competitor_brands_found'])}"
        )
    if report.get("roof_bridge_signal_count") is not None and not report["checks"].get("bridges_to_roof_service"):
        lines.append(
            f"- Roof-service bridge signals: {report['roof_bridge_signal_count']} (need at least 2 — "
            "tie insurance/policy content to inspection, flashing, or replacement)."
        )
    if report.get("opening_word_count") is not None and not report["checks"].get(
        "answer_first_opening_roughly_50_to_120_words"
    ):
        lines.append(f"- Current opening word count: {report['opening_word_count']} (need 50-120).")
    if report.get("faq_count") is not None and not report["checks"].get("faq_exactly_8"):
        lines.append(f"- Current FAQ question count: {report['faq_count']} (need exactly 8).")
    if report.get("locations_found") is not None and not report["checks"].get("location_count_at_least_6"):
        found = ", ".join(report["locations_found"]) or "none"
        lines.append(f"- Current named locations: {len(report['locations_found'])} ({found}). Need at least 6.")
    non_question_h2s = [
        heading
        for heading in report.get("h2_headings", [])
        if not heading.strip().endswith("?") and heading.strip().lower() != "faq"
    ]
    if non_question_h2s:
        lines.append(f"- These H2 headings are not questions: {', '.join(non_question_h2s)}")

    return "\n".join(lines)


def merge_generation_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        return {}
    if len(reports) == 1:
        return dict(reports[0])

    merged = dict(reports[-1])
    merged["validation_attempt"] = len(reports)
    merged["elapsed_seconds"] = round(
        sum(float(report.get("elapsed_seconds") or 0) for report in reports),
        2,
    )

    total_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }
    for report in reports:
        usage = report.get("usage") or {}
        for key in total_usage:
            total_usage[key] += int(usage.get(key) or 0)

    merged["usage"] = total_usage
    model_used = str(merged.get("model_used") or "")
    if model_used:
        merged["estimated_cost_usd"] = {
            "tokens": estimate_token_cost_usd(model_used, total_usage),
        }

    return merged


def _format_token_count(value: int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,}"


def print_generation_cost_summary(
    generation_report: dict[str, Any],
    *,
    model_used: str,
    log_prefix: str | None = None,
) -> None:
    """Print LLM token usage and estimated USD cost after a generation stage."""
    log = log_prefix or write_log_prefix()

    print(f"{log} Model: {generation_report.get('model_used', model_used)}")

    elapsed = generation_report.get("elapsed_seconds")
    api_calls = generation_report.get("validation_attempts") or generation_report.get("validation_attempt")
    if elapsed is not None:
        call_note = f" ({api_calls} API call{'s' if api_calls != 1 else ''})" if api_calls else ""
        print(f"{log} Generation time: {elapsed}s{call_note}")

    if generation_report.get("validation_passed") is False:
        print(f"{log} Validation passed on generation: no (see validation JSON)")
    elif generation_report.get("validation_passed") is True:
        print(f"{log} Validation passed on generation: yes")

    usage = generation_report.get("usage") or {}
    if usage:
        print(
            f"{log} Tokens: "
            f"{_format_token_count(usage.get('prompt_tokens'))} in / "
            f"{_format_token_count(usage.get('completion_tokens'))} out / "
            f"{_format_token_count(usage.get('total_tokens'))} total"
        )
        cached = int(usage.get("cached_tokens") or 0)
        if cached:
            print(f"{log} Cached input tokens: {_format_token_count(cached)}")

    cost_block = generation_report.get("estimated_cost_usd") or {}
    token_cost = cost_block.get("tokens") or {}
    total_usd = token_cost.get("total")
    if total_usd is not None:
        input_usd = float(token_cost.get("input") or 0)
        output_usd = float(token_cost.get("output") or 0)
        print(
            f"{log} Estimated inference cost: ${total_usd:.4f} USD "
            f"(input ${input_usd:.4f}, output ${output_usd:.4f})"
        )
    elif usage:
        note = token_cost.get("note") or "No catalog pricing for this model."
        print(f"{log} Estimated inference cost: unavailable — {note}")

    endpoint_cost = cost_block.get("endpoint") or {}
    if endpoint_cost.get("total") is not None:
        print(f"{log} Estimated endpoint uptime cost: ${endpoint_cost['total']:.4f} USD")

    combined = cost_block.get("combined_total")
    if combined is not None:
        print(f"{log} Estimated combined cost: ${combined:.4f} USD")


def format_multi_run_slack_lines(multi: dict[str, Any]) -> list[str]:
    """Slack lines for parallel template scoring (write_multi)."""
    if not isinstance(multi, dict):
        return []

    winner = str(multi.get("winner") or "").strip()
    scores = multi.get("scores") or {}
    lines: list[str] = []
    if scores:
        lines.append("• Template scores:")
        for template_id in ("geo", "scenario", "explainer"):
            entry = scores.get(template_id)
            if not isinstance(entry, dict):
                continue
            marker = " ✓ sent" if template_id == winner else ""
            lines.append(f"  – {template_id}: {entry.get('total', '?')}/62{marker}")
    if multi.get("reason"):
        lines.append(f"• Scorer: {multi['reason']}")
    if multi.get("tiebreaker_applied"):
        lines.append("• Tiebreaker applied (scores were tied)")
    if multi.get("scoring_skipped"):
        lines.append("• Scoring skipped (no templates passed validation)")

    template_costs = multi.get("template_generation_costs_usd") or {}
    if template_costs:
        parts = [
            f"{template_id} ${float(template_costs[template_id]):.4f}"
            for template_id in ("geo", "scenario", "explainer")
            if template_id in template_costs
        ]
        scorer = multi.get("scorer_estimated_cost_usd")
        if scorer is not None:
            parts.append(f"scorer ${float(scorer):.4f}")
        if parts:
            lines.append(f"  – write costs: {', '.join(parts)}")

    return lines


def format_generation_slack_lines(
    validation_report: dict[str, Any],
    *,
    model_display: str | None = None,
    model_line_label: str = "Model",
) -> list[str]:
    """Format model, timing, tokens, and cost for Slack approval posts."""
    generation = validation_report.get("generation")
    if not isinstance(generation, dict):
        return []

    model_id = str(generation.get("model_used") or validation_report.get("model") or "").strip()
    if not model_id:
        return []

    lines = [f"• {model_line_label}: {model_display or model_id}"]

    elapsed = generation.get("elapsed_seconds")
    api_calls = generation.get("validation_attempts") or generation.get("validation_attempt")
    if elapsed is not None:
        call_note = ""
        if api_calls:
            call_note = f" ({api_calls} API call{'s' if api_calls != 1 else ''})"
        lines.append(f"• Generation time: {elapsed}s{call_note}")

    usage = generation.get("usage") or {}
    if usage:
        lines.append(
            "• Tokens: "
            f"{_format_token_count(usage.get('prompt_tokens'))} in / "
            f"{_format_token_count(usage.get('completion_tokens'))} out / "
            f"{_format_token_count(usage.get('total_tokens'))} total"
        )

    from blog_automation.pipeline_costs import format_inference_cost_slack_line

    inference_line = format_inference_cost_slack_line(validation_report)
    if inference_line:
        lines.append(inference_line)
    else:
        token_cost = (generation.get("estimated_cost_usd") or {}).get("tokens") or {}
        total_usd = token_cost.get("total")
        if total_usd is not None:
            input_usd = float(token_cost.get("input") or 0)
            output_usd = float(token_cost.get("output") or 0)
            lines.append(
                f"• Est. inference cost: ${float(total_usd):.4f} USD "
                f"(input ${input_usd:.4f}, output ${output_usd:.4f})"
            )

    if generation.get("validation_passed") is True:
        attempt = generation.get("validation_attempt")
        if attempt:
            lines.append(f"• Validation: passed on attempt {attempt}")
        else:
            lines.append("• Validation: passed")
    elif generation.get("validation_passed") is False:
        lines.append("• Validation: failed (see validation JSON)")

    revision_mode = generation.get("revision_mode")
    if revision_mode:
        lines.append(f"• Revision mode: {revision_mode}")

    source_count = validation_report.get("source_count")
    if source_count is not None:
        lines.append(f"• Sources used: {source_count}")

    multi = validation_report.get("multi_run")
    if isinstance(multi, dict):
        lines.extend(format_multi_run_slack_lines(multi))

    return lines


def generate_validated_draft(
    prompt: str,
    model: str,
    *,
    allow_serverless_fallback: bool = True,
    max_attempts: int = DEFAULT_VALIDATION_MAX_ATTEMPTS,
    author_name: str = DEFAULT_AUTHOR_NAME,
    author_credentials: str = DEFAULT_AUTHOR_CREDENTIALS,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
    selected_sources: list[dict[str, Any]] | None = None,
    fact_audit_model: str | None = None,
    run_fact_audit: bool = True,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Generate a draft and retry with validation feedback until checks pass or attempts run out."""
    log = write_log_prefix()
    max_attempts = max(1, max_attempts)
    draft = ""
    validation_report: dict[str, Any] = {"passed": False, "checks": {}}
    generation_reports: list[dict[str, Any]] = []
    revision_feedback = ""

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            current_prompt = prompt
        else:
            current_prompt = (
                f"{prompt}\n\n---\n\n"
                f"VALIDATION REVISION REQUIRED (attempt {attempt} of {max_attempts}):\n"
                f"{revision_feedback}\n\n"
                f"PREVIOUS DRAFT TO REVISE:\n```\n{draft.strip()}\n```"
            )

        draft, generation_report = generate_draft(
            current_prompt,
            model,
            allow_serverless_fallback=allow_serverless_fallback,
        )
        generation_report["validation_attempt"] = attempt
        generation_reports.append(generation_report)

        validation_report = validate_draft(
            draft,
            writing_prompt_id=writing_prompt_id,
            selected_sources=selected_sources,
        )
        if validation_report["passed"] and run_fact_audit and selected_sources:
            from blog_automation.fact_audit import audit_draft_against_sources

            fact_audit_report = audit_draft_against_sources(
                draft,
                selected_sources,
                model=fact_audit_model,
                allow_fallback=allow_serverless_fallback,
            )
            validation_report["fact_audit"] = {
                "passed": fact_audit_report["passed"],
                "issues": fact_audit_report.get("issues", []),
                "revision_notes": fact_audit_report.get("revision_notes", ""),
                "model_requested": fact_audit_report.get("model_requested"),
                "model_used": fact_audit_report.get("model_used"),
            }
            validation_report["checks"]["llm_fact_audit_passed"] = bool(
                fact_audit_report["passed"]
            )
            validation_report["passed"] = all(validation_report["checks"].values())
            if fact_audit_report.get("generation"):
                generation_report["fact_audit"] = fact_audit_report["generation"]
                print_inference_stage_cost(
                    fact_audit_report["generation"],
                    label="Fact-audit",
                    log_prefix=log,
                )

        if validation_report["passed"]:
            print(f"{log} Validation passed on attempt {attempt}.")
            break

        failed = [name for name, passed in validation_report["checks"].items() if not passed]
        print(f"{log} Validation failed on attempt {attempt}: {', '.join(failed)}")
        if attempt < max_attempts:
            revision_feedback = format_failed_validation_feedback(
                validation_report,
                author_name=author_name,
                author_credentials=author_credentials,
                writing_prompt_id=writing_prompt_id,
            )
        else:
            print(f"{log} Warning: Draft still failing validation after {max_attempts} attempt(s).")

    merged_report = merge_generation_reports(generation_reports)
    merged_report["validation_passed"] = validation_report["passed"]
    merged_report["validation_attempts"] = len(generation_reports)
    return draft, validation_report, merged_report


def save_text(text: str, path: Path, *, log_prefix: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"{log_prefix or write_log_prefix()} Saved {path}")


def save_json(data: dict[str, Any], path: Path, *, log_prefix: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"{log_prefix or write_log_prefix()} Saved {path}")


def clear_drafts_directory(output_dir: Path) -> list[Path]:
    """Remove existing draft files from typed subdirectories and legacy flat files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    removed: list[Path] = []
    md_dir, pdf_dir, json_dir = draft_subdirs(output_dir)
    targets = [md_dir, pdf_dir, json_dir, output_dir]
    for directory in targets:
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            if directory == output_dir and path.name in {
                DRAFTS_MD_DIRNAME,
                DRAFTS_PDF_DIRNAME,
                DRAFTS_JSON_DIRNAME,
            }:
                continue
            path.unlink()
            removed.append(path)
    return removed


def draft_artifact_paths(draft_path: Path) -> list[Path]:
    """Return the Markdown, PDF, and validation JSON paths for one draft stem."""
    stem = draft_stem_from_path(draft_path)
    output_dir = resolve_drafts_root(draft_path)
    md_dir, pdf_dir, json_dir = draft_subdirs(output_dir)
    paths = [
        md_dir / f"{stem}.md",
        pdf_dir / f"{stem}.pdf",
        json_dir / f"{stem}-validation.json",
    ]
    if draft_path.parent == output_dir:
        paths.extend(
            [
                output_dir / f"{stem}.md",
                output_dir / f"{stem}.pdf",
                output_dir / f"{stem}-validation.json",
            ]
        )
    return paths


def remove_draft_artifacts(draft_path: Path) -> list[Path]:
    """Remove one draft's Markdown, PDF, and validation JSON files."""
    removed: list[Path] = []
    seen: set[str] = set()
    for path in draft_artifact_paths(draft_path):
        key = str(path.resolve())
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        path.unlink()
        removed.append(path)
    return removed


def save_draft_outputs(
    *,
    draft: str,
    output_dir: Path,
    selected_sources: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    source_decision: dict[str, Any],
    model_used: str,
    generation_report: dict[str, Any],
    skip_pdf: bool = False,
    writing_prompt_id: str = DEFAULT_WRITING_PROMPT_ID,
    report_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, save Markdown/PDF/JSON outputs, and print summary lines."""
    log = write_log_prefix()
    draft_path, pdf_path, report_path = output_paths(draft, output_dir)
    report = validate_draft(
        draft,
        writing_prompt_id=writing_prompt_id,
        selected_sources=selected_sources,
    )
    report["generated_at"] = datetime.now().isoformat()
    report["source_count"] = len(selected_sources)
    report["available_source_count"] = len(sources)
    report["source_selection"] = source_decision
    report["model"] = model_used
    report["generation"] = generation_report
    report["draft_path"] = str(draft_path)
    report["sources_used"] = sources_used_payload(selected_sources)
    search_ran = consume_tavily_search_ran() is not None
    report["tavily_search_ran"] = search_ran
    pipeline_costs = load_pipeline_costs()
    if pipeline_costs:
        embedded_costs = dict(pipeline_costs)
        if not search_ran:
            embedded_costs.pop("search", None)
        report["pipeline_costs"] = embedded_costs
    if generation_report.get("validation_attempts") is not None:
        report["validation_attempts"] = generation_report["validation_attempts"]
    if generation_report.get("validation_passed") is not None:
        report["validation_passed_on_generation"] = generation_report["validation_passed"]
    if report_extras:
        report.update(report_extras)

    save_text(draft, draft_path, log_prefix=log)
    if skip_pdf:
        report["pdf_path"] = None
    else:
        try:
            save_draft_pdf(draft, pdf_path)
            print(f"{log} Saved {pdf_path}")
            report["pdf_path"] = str(pdf_path)
        except Exception as exc:
            report["pdf_path"] = None
            report["pdf_error"] = str(exc)
            print(f"{log} Warning: PDF export failed: {exc}")

    pipeline_log_file = os.environ.get("PIPELINE_LOG_FILE", "").strip()
    if pipeline_log_file:
        log_path = Path(pipeline_log_file)
        if log_path.is_file():
            try:
                from blog_automation.pipeline.runner import publish_canonical_pipeline_log

                published = publish_canonical_pipeline_log(log_path)
                rel = log_path.relative_to(PROJECT_ROOT)
                report["pipeline_log_path"] = str(rel)
                if published is not None:
                    report["pipeline_log_canonical_path"] = str(
                        published.relative_to(PROJECT_ROOT)
                    )
            except (OSError, ValueError) as exc:
                print(f"{log} Warning: Could not publish pipeline log path: {exc}")
    save_json(report, report_path, log_prefix=log)

    print(f"{log} Validation passed: {report['passed']}")
    print_generation_cost_summary(
        generation_report,
        model_used=model_used,
        log_prefix=log,
    )
    print_pipeline_run_cost_summary(
        pipeline_costs=report.get("pipeline_costs"),
        generation_report=generation_report,
        multi_run=report.get("multi_run"),
        log_prefix=log,
    )
    if not report["passed"]:
        failed = [name for name, passed in report["checks"].items() if not passed]
        print(f"{log} Failed checks: {', '.join(failed)}")

    return report

