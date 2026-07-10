"""Generate a GEO blog draft with Together AI (standalone writer module).

Default: serverless models with an interactive menu (ignores `TOGETHER_DEDICATED_ENDPOINT_ID`).

Optional dedicated endpoint (replaces former write.py):
    python -m blog_automation.pipeline.write_serverless --dedicated-endpoint

Run:
    python -m blog_automation.pipeline.write_serverless
    python -m blog_automation.pipeline.write_serverless --model 1
"""

from __future__ import annotations

import blog_automation._pycache_prefix  # noqa: F401

from blog_automation.paths import PROJECT_ROOT

import argparse
import json
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from blog_automation.llm_client import get_llm_provider
from blog_automation.llm_models import (
    default_writing_model,
    normalize_writing_model_for_provider,
    resolve_writing_model as resolve_provider_writing_model,
)
from blog_automation.write_common import (
    DEFAULT_AUTHOR_CREDENTIALS,
    DEFAULT_AUTHOR_NAME,
    DEFAULT_INPUT_PATH,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_VALIDATION_MAX_ATTEMPTS,
    REVISION_MODES,
    STYLE_NOTES_PATH,
    build_generation_report,
    build_draft_prompt,
    clear_drafts_directory,
    draft_validation_json_path,
    generate_validated_draft,
    generate_draft,
    load_json,
    load_approval_rewrite_context,
    persist_revision_mode_to_approval_json,
    read_text,
    remove_draft_artifacts,
    resolve_replace_draft_path,
    save_draft_outputs,
    select_sources_for_draft,
    tag_generation_report,
    write_log_prefix,
)
from blog_automation.writing_prompts import (
    describe_writing_prompt_rotation,
    get_writing_prompt_variant,
    load_writing_prompt_text,
    select_writing_prompt_variant,
    writing_prompt_variant_ids,
)

WRITE_RUNNER = "blog_automation.pipeline.write_serverless"

load_dotenv(PROJECT_ROOT / ".env")

SERVERLESS_MODEL_CHOICES: list[dict[str, str]] = [
    {
        "label": "Qwen3.5 397B A17B — strongest instruction following",
        "model_id": "Qwen/Qwen3.5-397B-A17B",
    },
    {
        "label": "Qwen3 235B A22B Instruct 2507 (throughput) — fast, budget MoE",
        "model_id": "Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
    },
    {
        "label": "OpenAI GPT-OSS 120B — strong cost/quality balance",
        "model_id": "openai/gpt-oss-120b",
    },
    {
        "label": "Llama 3.3 70B Instruct Turbo — faster, lower cost",
        "model_id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    },
]

DEFAULT_SERVERLESS_MODEL = default_writing_model()
PIPELINE_DEFAULT_WRITE_MODEL_ENV = "PIPELINE_DEFAULT_WRITE_MODEL"


def pipeline_default_write_model_enabled() -> bool:
    return os.getenv(PIPELINE_DEFAULT_WRITE_MODEL_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def activate_pipeline_default_write_model() -> None:
    os.environ[PIPELINE_DEFAULT_WRITE_MODEL_ENV] = "1"


def is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def print_model_menu() -> None:
    print("\nWhich model would you like to write the blog draft?\n")
    for index, choice in enumerate(SERVERLESS_MODEL_CHOICES, start=1):
        suffix = " (default)" if choice["model_id"] == DEFAULT_SERVERLESS_MODEL else ""
        print(f"  {index}. {choice['label']}{suffix}")
    print(f"\n  Press Enter for default — {model_label(DEFAULT_SERVERLESS_MODEL)}")
    print()


def prompt_model_choice() -> str:
    print_model_menu()
    while True:
        raw = input("Enter number: ").strip()
        if not raw:
            print(f"[write_serverless] Selected default: {model_label(DEFAULT_SERVERLESS_MODEL)}\n")
            return DEFAULT_SERVERLESS_MODEL
        if not raw.isdigit():
            print("Please enter a number from the list.")
            continue
        index = int(raw)
        if 1 <= index <= len(SERVERLESS_MODEL_CHOICES):
            return SERVERLESS_MODEL_CHOICES[index - 1]["model_id"]
        print(f"Please enter a number between 1 and {len(SERVERLESS_MODEL_CHOICES)}.")


def model_id_from_choice_number(value: str) -> str | None:
    if not value.isdigit():
        return None
    index = int(value)
    if 1 <= index <= len(SERVERLESS_MODEL_CHOICES):
        return SERVERLESS_MODEL_CHOICES[index - 1]["model_id"]
    return None


def model_label(model_id: str) -> str:
    if get_llm_provider() == "anthropic":
        return model_id
    for choice in SERVERLESS_MODEL_CHOICES:
        if choice["model_id"] == model_id:
            return choice["label"]
    return model_id


def infer_model_from_feedback_json(feedback_json: Path) -> str | None:
    if not feedback_json.is_file():
        return None

    with feedback_json.open("r", encoding="utf-8") as handle:
        record: dict[str, Any] = json.load(handle)

    if feedback_json.name.endswith("-validation.json"):
        validation = record
    else:
        draft_path = resolve_replace_draft_path(record)
        if not draft_path:
            return None
        validation_path = draft_validation_json_path(draft_path)
        if not validation_path.is_file():
            return None
        with validation_path.open("r", encoding="utf-8") as handle:
            validation = json.load(handle)

    generation = validation.get("generation", {})
    if not isinstance(generation, dict):
        return None

    model_used = str(generation.get("model_used") or "").strip()
    return model_used or validation.get("model") or None


def resolve_writing_model(
    model_arg: str | None,
    *,
    feedback_json: Path | None = None,
    interactive: bool | None = None,
) -> str:
    if model_arg:
        from_number = model_id_from_choice_number(model_arg)
        if from_number:
            return normalize_writing_model_for_provider(from_number)
        return normalize_writing_model_for_provider(model_arg)

    if feedback_json:
        inherited = infer_model_from_feedback_json(feedback_json)
        if inherited:
            normalized = normalize_writing_model_for_provider(inherited)
            print(f"[write_serverless] Using model from previous draft: {model_label(normalized)}")
            return normalized

    if pipeline_default_write_model_enabled():
        chosen = default_writing_model()
        print(f"[write_serverless] Using pipeline default: {model_label(chosen)}")
        return chosen

    if get_llm_provider() == "anthropic":
        return resolve_provider_writing_model(None)

    use_prompt = interactive if interactive is not None else is_interactive_terminal()
    if use_prompt:
        chosen = prompt_model_choice()
        print(f"[write_serverless] Selected: {model_label(chosen)}\n")
        return normalize_writing_model_for_provider(chosen)

    return normalize_writing_model_for_provider(DEFAULT_SERVERLESS_MODEL)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a GEO blog draft with a selectable Together serverless model.",
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--model",
        help=(
            "Together model ID or menu number (1–"
            f"{len(SERVERLESS_MODEL_CHOICES)}). "
            "Omit for an interactive prompt when running in a terminal."
        ),
    )
    parser.add_argument(
        "--source-strategy",
        choices=("auto", "best", "combine"),
        default="auto",
        help="Choose sources for one draft: auto, best, or combine.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable live terminal progress bars (also set WRITE_NO_PROGRESS=1).",
    )
    parser.add_argument(
        "--feedback-json",
        type=Path,
        help="Draft validation JSON (drafts_json/*-validation.json) whose Slack feedback should be applied.",
    )
    parser.add_argument(
        "--revision-mode",
        choices=REVISION_MODES,
        help="Override editorial vs factual rewrite mode (default: auto-detect from feedback).",
    )
    parser.add_argument(
        "--clear-drafts",
        action="store_true",
        help="Delete all existing files in --output-dir before writing.",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Skip PDF export of the generated Markdown draft.",
    )
    parser.add_argument(
        "--max-validation-attempts",
        type=int,
        default=DEFAULT_VALIDATION_MAX_ATTEMPTS,
        help="Retry generation until validation passes, up to this many attempts.",
    )
    parser.add_argument(
        "--no-validation-retry",
        action="store_true",
        help="Generate once without retrying failed validation checks.",
    )
    parser.add_argument(
        "--dedicated-endpoint",
        action="store_true",
        help="Use TOGETHER_DEDICATED_ENDPOINT_ID from .env (managed start/stop). Default is serverless only.",
    )
    parser.add_argument(
        "--keep-drafts",
        action="store_true",
        help="With --dedicated-endpoint: do not clear output/drafts before writing.",
    )
    parser.add_argument(
        "--writing-prompt",
        choices=("auto", *writing_prompt_variant_ids()),
        default="auto",
        help="Writing template: auto rotates geo/scenario/explainer by ISO week (default: auto).",
    )
    parser.add_argument(
        "--rotation-week",
        type=int,
        default=0,
        help="ISO week number for --writing-prompt auto (0 = current UTC week).",
    )
    args = parser.parse_args()

    os.environ["WRITE_RUNNER"] = WRITE_RUNNER
    load_dotenv(PROJECT_ROOT / ".env")
    if args.no_progress:
        os.environ["WRITE_NO_PROGRESS"] = "1"

    log = write_log_prefix()
    use_dedicated_endpoint = args.dedicated_endpoint
    endpoint_id = os.getenv("TOGETHER_DEDICATED_ENDPOINT_ID", "").strip() if use_dedicated_endpoint else ""

    if use_dedicated_endpoint and not endpoint_id:
        raise SystemExit(
            "TOGETHER_DEDICATED_ENDPOINT_ID is not set. Add it to .env or omit --dedicated-endpoint."
        )

    if use_dedicated_endpoint:
        raw_model = args.model or os.getenv("TOGETHER_WRITING_MODEL", DEFAULT_MODEL)
        from_number = model_id_from_choice_number(raw_model) if raw_model else None
        model_used = from_number or raw_model or DEFAULT_MODEL
        print(f"{log} Dedicated endpoint mode: {endpoint_id}")
        print(f"{log} Writing model: {model_used}")
    else:
        model_used = resolve_writing_model(
            args.model,
            feedback_json=args.feedback_json,
            interactive=True,
        )
        print(f"{log} Writing model: {model_label(model_used)}")

    if args.feedback_json:
        print(f"{log} LLM provider: {get_llm_provider()}")
        print(f"{log} Slack revision rewrite")

    rewrite_context = load_approval_rewrite_context(args.feedback_json)
    approval_feedback = rewrite_context["approval_feedback"]
    previous_draft = rewrite_context["previous_draft"]
    replace_draft_path = rewrite_context.get("replace_draft_path")
    revision_mode = args.revision_mode or rewrite_context.get("revision_mode") or None
    revision_mode_reason = rewrite_context.get("revision_mode_reason") or ""

    rotation_week = args.rotation_week or None
    if args.feedback_json and args.writing_prompt == "auto":
        writing_prompt_id = rewrite_context.get("writing_prompt_id") or "geo"
        prompt_variant = get_writing_prompt_variant(writing_prompt_id)
    else:
        prompt_variant = select_writing_prompt_variant(
            rotation_week=rotation_week,
            variant_id=args.writing_prompt,
        )
        writing_prompt_id = prompt_variant.id
    if args.writing_prompt == "auto":
        print(f"{log} Writing prompt rotation: {describe_writing_prompt_rotation(rotation_week=rotation_week)}")
    print(
        f"{log} Writing prompt: {prompt_variant.label} "
        f"({prompt_variant.path.relative_to(PROJECT_ROOT)})"
    )
    if args.feedback_json:
        if approval_feedback:
            print(f"{log} Loaded Slack approval feedback from {args.feedback_json}")
        if previous_draft:
            print(f"{log} Loaded previous draft for rewrite ({len(previous_draft.split())} words)")
        if revision_mode:
            reason_suffix = f" ({revision_mode_reason})" if revision_mode_reason else ""
            print(f"{log} Revision mode: {revision_mode}{reason_suffix}")
            persist_revision_mode_to_approval_json(
                args.feedback_json,
                revision_mode,
                revision_mode_reason or "manual override" if args.revision_mode else revision_mode_reason,
            )
        if not approval_feedback:
            print(f"{log} Warning: No Slack feedback found in {args.feedback_json}")

    if use_dedicated_endpoint and not args.keep_drafts:
        removed = clear_drafts_directory(args.output_dir)
        if removed:
            print(f"{log} Cleared {len(removed)} file(s) from {args.output_dir}")
    elif args.clear_drafts:
        removed = clear_drafts_directory(args.output_dir)
        if removed:
            print(f"{log} Cleared {len(removed)} file(s) from {args.output_dir}")
    elif replace_draft_path:
        removed = remove_draft_artifacts(replace_draft_path)
        if removed:
            print(f"{log} Removed {len(removed)} replaced draft file(s) for {replace_draft_path.name}")

    sources = load_json(args.input)
    if not sources:
        raise ValueError(f"No kept sources found in {args.input}. Run search.py and evaluate.py first.")

    selected_sources, source_decision = select_sources_for_draft(sources, args.source_strategy)
    print(
        f"{log} Source strategy: "
        f"{source_decision['mode']} "
        f"({source_decision['selected_source_count']}/{source_decision['available_source_count']} sources)"
    )
    print(f"{log} Source decision: {source_decision['reason']}")

    author_name = os.getenv("AUTHOR_NAME", DEFAULT_AUTHOR_NAME)
    author_credentials = os.getenv("AUTHOR_CREDENTIALS", DEFAULT_AUTHOR_CREDENTIALS)

    prompt = build_draft_prompt(
        load_writing_prompt_text(prompt_variant),
        selected_sources,
        read_text(STYLE_NOTES_PATH),
        author_name,
        author_credentials,
        approval_feedback,
        previous_draft,
        revision_mode,
        writing_prompt_id=writing_prompt_id,
    )

    if use_dedicated_endpoint:
        from blog_automation.together_endpoint import managed_dedicated_endpoint

        endpoint_session_started_at = time.monotonic()
        with managed_dedicated_endpoint(endpoint_id) as endpoint_model:
            active_model = model_used
            if endpoint_model and active_model == DEFAULT_MODEL:
                active_model = endpoint_model
                model_used = active_model
                print(f"{log} Using dedicated endpoint model: {active_model}")

            if args.no_validation_retry:
                draft, generation_report = generate_draft(
                    prompt,
                    active_model,
                    allow_serverless_fallback=False,
                )
            else:
                draft, _, generation_report = generate_validated_draft(
                    prompt,
                    active_model,
                    allow_serverless_fallback=False,
                    max_attempts=args.max_validation_attempts,
                    author_name=author_name,
                    author_credentials=author_credentials,
                    writing_prompt_id=writing_prompt_id,
                    selected_sources=selected_sources,
                )
        generation_report["endpoint_management_used"] = True
        generation_report["endpoint_session_seconds"] = round(
            time.monotonic() - endpoint_session_started_at,
            2,
        )
        per_minute = os.getenv("TOGETHER_ENDPOINT_COST_PER_MINUTE", "").strip()
        if per_minute:
            endpoint_cost = (generation_report["endpoint_session_seconds"] / 60.0) * float(per_minute)
            generation_report.setdefault("estimated_cost_usd", {})
            generation_report["estimated_cost_usd"]["endpoint"] = {
                "total": round(endpoint_cost, 4),
                "currency": "USD",
                "cost_per_minute": float(per_minute),
                "pricing_source": "env:TOGETHER_ENDPOINT_COST_PER_MINUTE",
                "note": "Endpoint uptime cost is separate from token usage.",
            }
            token_total = generation_report["estimated_cost_usd"].get("tokens", {}).get("total")
            if token_total is not None:
                generation_report["estimated_cost_usd"]["combined_total"] = round(
                    token_total + endpoint_cost,
                    4,
                )
        if revision_mode:
            generation_report["revision_mode"] = revision_mode
        tag_generation_report(generation_report, mode="dedicated")
    else:
        if args.no_validation_retry:
            draft, generation_report = generate_draft(
                prompt,
                model_used,
                allow_serverless_fallback=True,
            )
        else:
            draft, _, generation_report = generate_validated_draft(
                prompt,
                model_used,
                allow_serverless_fallback=True,
                max_attempts=args.max_validation_attempts,
                author_name=author_name,
                author_credentials=author_credentials,
                writing_prompt_id=writing_prompt_id,
                selected_sources=selected_sources,
            )
        generation_report["endpoint_management_used"] = False
        if revision_mode:
            generation_report["revision_mode"] = revision_mode
        tag_generation_report(generation_report, mode="serverless")

    save_draft_outputs(
        draft=draft,
        output_dir=args.output_dir,
        selected_sources=selected_sources,
        sources=sources,
        source_decision=source_decision,
        model_used=model_used,
        generation_report=generation_report,
        skip_pdf=args.no_pdf,
        writing_prompt_id=writing_prompt_id,
    )


if __name__ == "__main__":
    main()
