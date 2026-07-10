"""Evaluate Tavily search results with the active LLM provider.

This stage reads `output/sources/search_results.json`, scores each source for
the active company's GEO blog strategy, and writes:

- `output/sources/evaluated_sources.json` for all scored sources
- `output/sources/kept_sources.json` for sources worth sending to write_serverless

Run:
    python -m blog_automation.pipeline.evaluate
"""

from __future__ import annotations

import blog_automation._pycache_prefix  # noqa: F401

from blog_automation.paths import PROJECT_ROOT

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from blog_automation.llm_client import chat_completion
from blog_automation.llm_models import resolve_evaluation_model
from blog_automation.used_sources import normalize_source_url, source_url, used_source_urls
from blog_automation.pipeline_costs import record_evaluate_cost, print_inference_stage_cost
from blog_automation.together_models import (
    DEFAULT_EVALUATION_MODEL,
    EVALUATION_MODEL_FALLBACK_CHAIN,
)
from blog_automation.write_common import (
    build_generation_report,
    extract_usage,
    get_together_client,
    print_generation_cost_summary,
)
from blog_automation.company import get_profile, render_template

# Company profile: brand/geo-specific evaluate data (fallback angles, cluster
# keys) lives in blog_automation/companies/<slug>.py. Bound at import time.
_PROFILE = get_profile()


DEFAULT_INPUT_PATH = PROJECT_ROOT / "output" / "sources" / "search_results.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "output" / "sources" / "evaluated_sources.json"
DEFAULT_KEPT_PATH = PROJECT_ROOT / "output" / "sources" / "kept_sources.json"
PROMPT_PATH = PROJECT_ROOT / "prompts" / "evaluate.txt"
NATIONAL_TRADE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "evaluate_national_trade.txt"

DEFAULT_MODEL = DEFAULT_EVALUATION_MODEL
KEEP_THRESHOLD = 6.0
MIN_KEPT_SOURCES = 2
MIN_EVALUATED_KEPT_TO_PROCEED = 1
TARGET_EVALUATED_KEPT = 2
MIN_FALLBACK_WEIGHTED_SCORE = 7.0
MIN_FALLBACK_ROOFING_RELEVANCE = 4
MIN_ROOFING_RELEVANCE = 4
CONTENT_SNIPPET_LIMIT = 2800

SCORE_KEYS = [
    "local_relevance",
    "roofing_relevance",
    "recency",
    "source_authority",
    "actionability",
    "territory_alignment",
    "semantic_relevance",
]

def load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list in {path}")

    return data


def save_json(data: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"[evaluate] Saved {len(data)} records to {path}")


def load_prompt() -> str:
    return render_template(PROMPT_PATH.read_text(encoding="utf-8"))


def load_national_trade_prompt() -> str:
    return render_template(NATIONAL_TRADE_PROMPT_PATH.read_text(encoding="utf-8"))


def source_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def metadata_adjustments(source: dict[str, Any]) -> dict[str, Any]:
    territory_score = source_int(source.get("territory_alignment_score"))
    semantic_score = source_int(source.get("semantic_relevance_score"))
    multi_territory_bonus = source_int(source.get("multi_territory_bonus"))
    off_topic_penalty = source_int(source.get("off_topic_penalty"))
    duplicate_topic_penalty = source_int(source.get("duplicate_topic_penalty"))

    adjustment = round(
        min(multi_territory_bonus, 4) * 0.15
        - min(off_topic_penalty, 10) * 0.35
        - min(duplicate_topic_penalty, 4) * 0.25,
        2,
    )

    hard_reject_reasons = []
    used_urls = used_source_urls()
    if used_urls and normalize_source_url(source_url(source)) in used_urls:
        hard_reject_reasons.append("source URL was already used in an approved blog")
    if off_topic_penalty >= 7:
        hard_reject_reasons.append("off-topic penalty is too high")
    if semantic_score > 0 and semantic_score < 4:
        hard_reject_reasons.append("semantic relevance check did not pass")
    if duplicate_topic_penalty >= 4:
        hard_reject_reasons.append("topic overlaps a draft from the past 30 days")

    return {
        "territory_alignment_score": territory_score,
        "semantic_relevance_score": semantic_score,
        "multi_territory_bonus": multi_territory_bonus,
        "off_topic_penalty": off_topic_penalty,
        "duplicate_topic_penalty": duplicate_topic_penalty,
        "weighted_score_adjustment": adjustment,
        "hard_reject_reasons": hard_reject_reasons,
    }


def build_prompt(prompt_template: str, source: dict[str, Any]) -> str:
    return prompt_template.format(
        title=source.get("title", ""),
        url=source.get("url", ""),
        published_date=source.get("published_date", ""),
        content=str(source.get("content", ""))[:CONTENT_SNIPPET_LIMIT],
        domain=source.get("domain", ""),
        query=source.get("query", ""),
        strategy_cluster=source.get("strategy_cluster", ""),
        pillar_topic=source.get("pillar_topic", ""),
        trigger_window_hours=source.get("trigger_window_hours", ""),
        territory_alignment_score=source.get("territory_alignment_score", ""),
        matched_territories=json.dumps(source.get("matched_territories", {}), ensure_ascii=True),
        multi_territory_bonus=source.get("multi_territory_bonus", ""),
        semantic_relevance_score=source.get("semantic_relevance_score", ""),
        semantic_relevance_rules=", ".join(source.get("semantic_relevance_rules", [])),
        off_topic_penalty=source.get("off_topic_penalty", ""),
        off_topic_matches=json.dumps(source.get("off_topic_matches", {}), ensure_ascii=True),
        duplicate_topic_penalty=source.get("duplicate_topic_penalty", ""),
        duplicate_topic_match=json.dumps(source.get("duplicate_topic_match"), ensure_ascii=True),
    )


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("Model response was valid JSON but not an object")

    return parsed


def get_together_client():
    from blog_automation.write_common import get_together_client as _get_client

    return _get_client()


def evaluate_source_with_llm(
    source: dict[str, Any],
    prompt_template: str,
    model: str,
    *,
    allow_fallback: bool = True,
) -> tuple[dict[str, Any], dict[str, int], str | None]:
    content, metadata = chat_completion(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Return strict JSON only. Do not include markdown or commentary.",
            },
            {"role": "user", "content": build_prompt(prompt_template, source)},
        ],
        allow_fallback=allow_fallback,
        fallback_chain=EVALUATION_MODEL_FALLBACK_CHAIN,
        log_prefix="[evaluate]",
        temperature=0.1,
        max_tokens=800,
        response_format="json",
        role="evaluation",
    )
    evaluation = extract_json_object(content)
    model_used = str(metadata.get("model_used") or model)
    usage = metadata.get("usage") or {}
    model_returned = metadata.get("model_returned_by_api")
    if model_returned is not None:
        model_returned = str(model_returned)
    return normalize_evaluation(evaluation, source), usage, model_returned or model_used


def evaluate_source_with_together(
    source: dict[str, Any],
    prompt_template: str,
    client: Any,
    model: str,
    *,
    allow_fallback: bool = True,
) -> tuple[dict[str, Any], dict[str, int], str | None]:
    del client
    return evaluate_source_with_llm(
        source,
        prompt_template,
        model,
        allow_fallback=allow_fallback,
    )


def normalize_score(value: Any) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        score = 1
    return max(1, min(10, score))


def build_fallback_angle(source: dict[str, Any]) -> str:
    cluster = source.get("strategy_cluster") or "local_roofing"
    title = source.get("title") or "this local event"

    angles = _PROFILE.EVALUATE_FALLBACK_ANGLES
    if cluster == "storm_damage":
        return angles["storm"]
    if cluster == _PROFILE.INSURANCE_CLUSTER_KEY:
        return angles["insurance"]
    if cluster == "roof_safety":
        return angles["safety"]
    if cluster == "county_guides":
        return "What should homeowners in this county know before planning roof work?"
    return angles["generic"].format(title=title)


def normalize_evaluation(evaluation: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    scores = evaluation.get("scores") if isinstance(evaluation.get("scores"), dict) else {}
    normalized_scores = {}
    for key in SCORE_KEYS:
        default = 5 if key in {"territory_alignment", "semantic_relevance"} else 1
        normalized_scores[key] = normalize_score(scores.get(key, default))
    adjustments = metadata_adjustments(source)
    if adjustments["territory_alignment_score"] and not scores.get("territory_alignment"):
        normalized_scores["territory_alignment"] = normalize_score(adjustments["territory_alignment_score"])
    if adjustments["semantic_relevance_score"] and not scores.get("semantic_relevance"):
        normalized_scores["semantic_relevance"] = normalize_score(adjustments["semantic_relevance_score"])

    weighted_score = evaluation.get("weighted_score")
    try:
        weighted_score = round(float(weighted_score), 2)
    except (TypeError, ValueError):
        weighted_score = round(
            normalized_scores["local_relevance"] * 0.24
            + normalized_scores["roofing_relevance"] * 0.24
            + normalized_scores["recency"] * 0.14
            + normalized_scores["source_authority"] * 0.08
            + normalized_scores["actionability"] * 0.10
            + normalized_scores["territory_alignment"] * 0.10
            + normalized_scores["semantic_relevance"] * 0.10,
            2,
        )

    adjusted_weighted_score = round(
        max(1.0, min(10.0, weighted_score + adjustments["weighted_score_adjustment"])),
        2,
    )
    hard_reject_reasons = list(adjustments["hard_reject_reasons"])
    if normalized_scores.get("roofing_relevance", 0) < MIN_ROOFING_RELEVANCE:
        hard_reject_reasons.append("roofing relevance is too low")
    adjustments = {**adjustments, "hard_reject_reasons": hard_reject_reasons}

    keep = bool(evaluation.get("keep", adjusted_weighted_score >= KEEP_THRESHOLD))
    if adjusted_weighted_score < KEEP_THRESHOLD or hard_reject_reasons:
        keep = False

    recommended_angle = evaluation.get("recommended_angle") or build_fallback_angle(source)
    if not keep:
        recommended_angle = ""

    reason = evaluation.get("reason") or "No reason provided."
    if hard_reject_reasons:
        reason = f"{reason} Rejected because {', '.join(hard_reject_reasons)}."

    return {
        "title": evaluation.get("title") or source.get("title", ""),
        "url": evaluation.get("url") or source.get("url", ""),
        "strategy_cluster": source.get("strategy_cluster", ""),
        "pillar_topic": source.get("pillar_topic", ""),
        "trigger_window_hours": source.get("trigger_window_hours"),
        "scores": normalized_scores,
        "model_weighted_score": weighted_score,
        "weighted_score": adjusted_weighted_score,
        "scoring_adjustments": adjustments,
        "keep": keep,
        "reason": reason,
        "recommended_angle": recommended_angle,
        "source": source,
    }


class IncrementalEvaluator:
    """Score search candidates one-by-one during Tavily ingest; track kept sources."""

    def __init__(self, *, model: str | None = None) -> None:
        self.model = resolve_evaluation_model(model)
        load_dotenv(PROJECT_ROOT / ".env")
        self.client = get_together_client()
        self.prompt_template = load_prompt()
        self.evaluated: list[dict[str, Any]] = []
        self._evaluated_urls: set[str] = set()
        self.kept: list[dict[str, Any]] = []
        self._started = time.perf_counter()
        self._total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
        }
        self._model_returned_by_api: str | None = None
        self._prompt_label = "local"
        print(f"[evaluate] Incremental mode on — model: {self.model}")

    def use_national_trade_prompt(self) -> None:
        """Switch to the national trade evaluator prompt for subsequent evaluate calls."""
        self.prompt_template = load_national_trade_prompt()
        self._prompt_label = "national_trade"
        print(f"[evaluate] Switched to national trade evaluation prompt.")

    def evaluate_search_result(self, source: dict[str, Any]) -> dict[str, Any]:
        url = str(source.get("url", "")).strip()
        if url in self._evaluated_urls:
            for item in self.evaluated:
                if item.get("url") == url:
                    return item
            raise RuntimeError(f"URL {url!r} marked evaluated but not found")

        title = source.get("title", "Untitled")
        print(f"[evaluate] Scoring search keep ({len(self.evaluated) + 1}): {title}")
        item, usage, model_returned = evaluate_source_with_together(
            source,
            self.prompt_template,
            self.client,
            self.model,
        )
        self.evaluated.append(item)
        self._evaluated_urls.add(url)
        for key in self._total_usage:
            self._total_usage[key] += int(usage.get(key) or 0)
        if model_returned:
            self._model_returned_by_api = model_returned

        score = item.get("weighted_score", 0)
        if item.get("keep"):
            self.kept.append(item)
            print(
                f"[evaluate] Kept ({len(self.kept)}): score {score} >= {KEEP_THRESHOLD} — {title}"
            )
        else:
            print(f"[evaluate] Rejected: score {score} < {KEEP_THRESHOLD} — {title}")

        return item

    def save_outputs(
        self,
        *,
        evaluated_output: Path = DEFAULT_OUTPUT_PATH,
        kept_output: Path = DEFAULT_KEPT_PATH,
    ) -> None:
        evaluated_sorted = sorted(self.evaluated, key=lambda item: item["weighted_score"], reverse=True)
        kept_sorted = sorted(self.kept, key=lambda item: item["weighted_score"], reverse=True)
        save_json(evaluated_sorted, evaluated_output)
        if kept_sorted:
            save_json(kept_sorted, kept_output)
        else:
            print(
                f"[evaluate] No new keeps — preserving existing {kept_output.name} "
                "(run with --include-used-sources or --all-queries to force refresh)"
            )

    def print_summary(self) -> None:
        print(
            f"[evaluate] Incremental summary: kept {len(self.kept)}/{len(self.evaluated)} "
            f"(threshold {KEEP_THRESHOLD})"
        )
        report = self.build_run_report()
        print_generation_cost_summary(report, model_used=self.model, log_prefix="[evaluate]")
        print_inference_stage_cost(report, label="Evaluate", log_prefix="[evaluate]")

    def build_run_report(self) -> dict[str, Any]:
        report = build_generation_report(
            model_requested=self.model,
            model_used=self.model,
            model_returned_by_api=self._model_returned_by_api,
            elapsed_seconds=time.perf_counter() - self._started,
            usage=self._total_usage,
        )
        report["mode"] = "evaluate_incremental"
        report["api_calls"] = len(self.evaluated)
        return report


def evaluate_sources(
    sources: list[dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not sources:
        return [], {"model_used": model, "mode": "evaluate"}

    started = time.perf_counter()
    load_dotenv(PROJECT_ROOT / ".env")
    client = get_together_client()
    prompt_template = load_prompt()
    evaluated: list[dict[str, Any]] = []
    total_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }
    model_returned_by_api: str | None = None

    for index, source in enumerate(sources, start=1):
        print(f"[evaluate] Scoring source {index}/{len(sources)}: {source.get('title', 'Untitled')}")
        item, usage, model_returned = evaluate_source_with_together(
            source, prompt_template, client, model
        )
        evaluated.append(item)
        for key in total_usage:
            total_usage[key] += int(usage.get(key) or 0)
        if model_returned:
            model_returned_by_api = model_returned

    report = build_generation_report(
        model_requested=model,
        model_used=model,
        model_returned_by_api=model_returned_by_api,
        elapsed_seconds=time.perf_counter() - started,
        usage=total_usage,
    )
    report["mode"] = "evaluate"
    report["api_calls"] = len(sources)
    report["validation_attempts"] = len(sources)
    return evaluated, report


def _promote_minimum_kept(item: dict[str, Any]) -> None:
    item["keep"] = True
    fallback_note = (
        f" Kept by minimum-kept fallback (top {MIN_KEPT_SOURCES} by weighted_score "
        f"despite threshold {KEEP_THRESHOLD})."
    )
    item["reason"] = f"{item.get('reason', '').strip()}{fallback_note}".strip()
    if not item.get("recommended_angle"):
        item["recommended_angle"] = build_fallback_angle(item.get("source", {}))


def ensure_minimum_kept(
    evaluated: list[dict[str, Any]],
    *,
    min_kept: int = MIN_KEPT_SOURCES,
) -> list[dict[str, Any]]:
    """Guarantee at least min_kept sources by promoting the highest weighted_score hits."""
    kept = [item for item in evaluated if item.get("keep")]
    if len(kept) >= min_kept:
        return sorted(kept, key=lambda item: item["weighted_score"], reverse=True)

    kept_urls = {item["url"] for item in kept}
    ranked = sorted(evaluated, key=lambda item: item["weighted_score"], reverse=True)

    def qualifies_for_fallback(item: dict[str, Any]) -> bool:
        scores = item.get("scores") or {}
        if scores.get("roofing_relevance", 0) < MIN_FALLBACK_ROOFING_RELEVANCE:
            return False
        if item.get("weighted_score", 0) < MIN_FALLBACK_WEIGHTED_SCORE:
            return False
        hard_rejects = item.get("scoring_adjustments", {}).get("hard_reject_reasons") or []
        return not hard_rejects

    for item in ranked:
        if len(kept) >= min_kept:
            break
        if item["url"] in kept_urls:
            continue
        if not qualifies_for_fallback(item):
            continue
        _promote_minimum_kept(item)
        kept.append(item)
        kept_urls.add(item["url"])

    return sorted(kept, key=lambda item: item["weighted_score"], reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Tavily sources for GEO blog relevance.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--kept-output", type=Path, default=DEFAULT_KEPT_PATH)
    parser.add_argument("--model", default=resolve_evaluation_model())
    args = parser.parse_args()

    sources = load_json(args.input)
    print(f"[evaluate] Loaded {len(sources)} source candidates from {args.input}")
    print(f"[evaluate] Evaluation model: {args.model}")

    evaluated, run_report = evaluate_sources(sources, model=args.model)
    evaluated.sort(key=lambda item: item["weighted_score"], reverse=True)
    kept = ensure_minimum_kept(evaluated)

    save_json(evaluated, args.output)
    save_json(kept, args.kept_output)

    promoted = sum(
        1
        for item in kept
        if "minimum-kept fallback" in str(item.get("reason", ""))
    )
    print(
        f"[evaluate] Kept {len(kept)}/{len(evaluated)} sources "
        f"(threshold {KEEP_THRESHOLD}, minimum {MIN_KEPT_SOURCES}"
        f"{f', {promoted} promoted by fallback' if promoted else ''})"
    )

    if not kept:
        print(
            "[evaluate] No sources met quality thresholds. "
            "Try :repeat: again later, widen search (--all-queries), or lower used-source blocklist."
        )
        raise SystemExit(1)

    model_used = str(run_report.get("model_used") or args.model)
    print_generation_cost_summary(
        run_report,
        model_used=model_used,
        log_prefix="[evaluate]",
    )
    record_evaluate_cost(run_report, api_calls=len(sources))


if __name__ == "__main__":
    main()
