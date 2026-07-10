"""Score validated blog drafts across writing templates and pick a winner."""

from __future__ import annotations

import json
import re
from typing import Any

from blog_automation.llm_client import chat_completion
from blog_automation.company import get_profile

_PROFILE = get_profile()
from blog_automation.llm_models import resolve_scorer_model
from blog_automation.together_models import (
    DEFAULT_SCORER_MODEL,
    SCORER_MODEL_FALLBACK_CHAIN,
)
from blog_automation.write_common import (
    METRO_LOCATIONS,
    estimate_token_cost_usd,
    extract_opening_paragraph,
)

SCORER_MODEL = DEFAULT_SCORER_MODEL
SCORER_PASSES = 3
TIEBREAKER_ORDER = ["scenario", "geo", "explainer", "local_anchor", "industry_insight"]
SCORE_DIMENSIONS = [
    "title_quality",
    "opening_quality",
    "neighborhood_prose",
    "table_specificity",
    "faq_depth",
    "geo_quotability",
]
MAX_SCORE = len(SCORE_DIMENSIONS) * 10
MAX_TOTAL_WITH_BONUS = MAX_SCORE + 2

TEMPLATE_IDS = ("geo", "scenario", "explainer")


def _extract_title(markdown: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _extract_h2_headings(markdown: str) -> list[str]:
    return re.findall(r"^##\s+(.+)$", markdown, flags=re.MULTILINE)


def _extract_neighborhood_section_text(markdown: str) -> str:
    h2_matches = list(re.finditer(r"^##\s+(.+)$", markdown, flags=re.MULTILINE))
    for index, match in enumerate(h2_matches):
        heading = match.group(1).lower()
        if "neighborhood" in heading or "counties" in heading or "county" in heading:
            start = match.end()
            end = h2_matches[index + 1].start() if index + 1 < len(h2_matches) else len(markdown)
            return markdown[start:end].strip()[:400]
    return ""


def _extract_table_preview(markdown: str, *, max_rows: int = 6) -> list[str]:
    rows: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if re.match(r"^\|\s*:?-+", stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")][:3]
        rows.append(" | ".join(cells))
        if len(rows) >= max_rows:
            break
    return rows


def _extract_faq_answer_first_sentences(markdown: str) -> list[str]:
    faq_match = re.search(r"(^##+ FAQ\b.*)", markdown, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
    if not faq_match:
        return []

    faq_section = faq_match.group(1)
    headings = list(re.finditer(r"^#{3,6}\s+(.+)$", faq_section, flags=re.MULTILINE))
    sentences: list[str] = []
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(faq_section)
        answer_text = faq_section[start:end].strip().split("\n\n", maxsplit=1)[0].strip()
        if not answer_text:
            sentences.append("")
            continue
        first_sentence = re.split(r"(?<=[.!?])\s+", answer_text, maxsplit=1)[0].strip()
        sentences.append(first_sentence[:200])
    return sentences


def _locations_found(markdown: str) -> list[str]:
    return sorted(
        location
        for location in METRO_LOCATIONS
        if re.search(rf"\b{re.escape(location)}\b", markdown)
    )


def build_draft_summary(
    draft: str,
    validation_report: dict[str, Any],
    template_id: str,
) -> dict[str, Any]:
    title = _extract_title(draft)
    opening = extract_opening_paragraph(draft, writing_prompt_id=template_id)
    checks = validation_report.get("checks") or {}
    failed_checks = [name for name, passed in checks.items() if not passed]

    return {
        "template_id": template_id,
        "title": title,
        "title_char_count": len(title),
        "opening_preview": opening[:200],
        "opening_word_count": len(opening.split()),
        "h2_headings": _extract_h2_headings(draft),
        "locations_found": _locations_found(draft),
        "citation_count": validation_report.get("citation_count", 0),
        "cta_count": validation_report.get("cta_count", 0),
        "faq_count": validation_report.get("faq_count", 0),
        "has_comparison_table": bool(validation_report.get("checks", {}).get("has_comparison_table")),
        "validation_passed": bool(validation_report.get("passed")),
        "failed_checks": failed_checks,
        "neighborhood_section_preview": _extract_neighborhood_section_text(draft),
        "table_rows_preview": _extract_table_preview(draft),
        "faq_answer_first_sentences": _extract_faq_answer_first_sentences(draft),
    }


def build_scorer_prompt(summaries: list[dict[str, Any]]) -> str:
    summaries_block = json.dumps(summaries, indent=2, ensure_ascii=False)

    # Build the expected JSON schema example dynamically so new template IDs
    # (local_anchor, industry_insight, etc.) are shown explicitly rather than
    # relying on the trailing "Only include..." instruction.
    dim_row = (
        '"title_quality": <0-10>, "opening_quality": <0-10>, '
        '"neighborhood_prose": <0-10>, "table_specificity": <0-10>, '
        '"faq_depth": <0-10>, "geo_quotability": <0-10>, '
        '"validation_bonus": <-2, 0, or 2>, "total": <sum of above>'
    )
    present_ids = [s["template_id"] for s in summaries if "template_id" in s]
    scores_example_lines = ["    {{"]
    for i, tid in enumerate(present_ids):
        comma = "," if i < len(present_ids) - 1 else ""
        scores_example_lines.append(f'      "{tid}": {{ {dim_row} }}{comma}')
    scores_example_lines.append("    }}")
    scores_example = "\n".join(scores_example_lines)

    return f"""You are scoring blog drafts for {_PROFILE.COMPANY_NAME}.
Score each draft on exactly 6 dimensions, 0-10 each.
Return ONLY valid JSON — no preamble, no commentary.

SCORING DIMENSIONS (0-10 each):
1. title_quality: Is the title <=70 characters? Does it include a {_PROFILE.METRO_AREA} location and specific topic? Is it search-query shaped (not generic)?
2. opening_quality: Does the opening paragraph name a news outlet and date in sentence one? Does it answer the reader's question directly? Is it 50-120 words?
3. neighborhood_prose: Is the neighborhood section written in prose paragraphs (not bullet points)? Does each location mention carry a specific concrete reason (terrain, housing era, drainage issue)?
4. table_specificity: Do table rows name specific {_PROFILE.METRO_AREA} locations + housing types + concrete mechanisms? Are rows distinct and individually quotable? Penalize generic rows (e.g., "Risk Factor | Impact | Prevention").
5. faq_depth: Do FAQ questions cover varied topics (not the same question 8 ways)? Does the first sentence of each answer fully answer the question? Are at least 3 questions location-specific?
6. geo_quotability: Can you quote a single sentence from the opening or any H2 section and have it stand alone as a useful answer? Are H2 first sentences direct answers, not setup sentences?

VALIDATION BONUS: Add 2 points to the total if validation_passed is true. Subtract 2 if title_char_count > 70.

DRAFT SUMMARIES:
{summaries_block}

Return this exact JSON structure (one entry per template_id in DRAFT SUMMARIES above):
{{
  "scores": {scores_example},
  "winner": "<template_id with highest total>",
  "tiebreaker_applied": <true or false>,
  "reason": "<1-2 sentence explanation of why winner scored highest>"
}}

Only include score objects for template_ids present in DRAFT SUMMARIES.
"""


def _parse_scorer_response(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Scorer response was not a JSON object.")
    return parsed


def score_drafts(
    summaries: list[dict[str, Any]],
    *,
    model: str | None = None,
    allow_serverless_fallback: bool = True,
) -> dict[str, Any]:
    prompt = build_scorer_prompt(summaries)
    active_model = resolve_scorer_model(model)
    content, metadata = chat_completion(
        model=active_model,
        messages=[{"role": "user", "content": prompt}],
        allow_fallback=allow_serverless_fallback,
        fallback_chain=SCORER_MODEL_FALLBACK_CHAIN,
        log_prefix="[draft_scorer]",
        temperature=0.1,
        max_tokens=800,
        response_format="json",
        role="scorer",
    )
    parsed = _parse_scorer_response(content)
    usage = metadata.get("usage") or {}
    model_used = str(metadata.get("model_used") or active_model)
    return {"result": parsed, "usage": usage, "model": model_used, "generation": metadata}


def apply_tiebreaker(score_result: dict[str, Any]) -> dict[str, Any]:
    parsed = score_result.get("result")
    if not isinstance(parsed, dict):
        return score_result

    scores = parsed.get("scores")
    if not isinstance(scores, dict) or not scores:
        return score_result

    totals: dict[str, float] = {}
    for template_id, entry in scores.items():
        if isinstance(entry, dict):
            totals[str(template_id)] = float(entry.get("total") or 0)

    if not totals:
        return score_result

    max_total = max(totals.values())
    leaders = [template_id for template_id, total in totals.items() if max_total - total <= 2]
    winner = parsed.get("winner")
    tiebreaker_applied = False

    if len(leaders) > 1:
        for preferred in TIEBREAKER_ORDER:
            if preferred in leaders:
                winner = preferred
                tiebreaker_applied = True
                break
    elif not winner or winner not in totals:
        winner = max(totals, key=totals.get)

    parsed["winner"] = winner
    parsed["tiebreaker_applied"] = tiebreaker_applied
    score_result["result"] = parsed
    return score_result


def validation_check_score(validation_report: dict[str, Any]) -> int:
    checks = validation_report.get("checks") or {}
    return sum(1 for passed in checks.values() if passed)


def _sum_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for usage in usages:
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                result[key] = result.get(key, 0) + value
    return result


def _aggregate_pass_scores(all_parsed: list[dict[str, Any]]) -> dict[str, Any]:
    all_scores = [p.get("scores") or {} for p in all_parsed]
    template_ids: set[str] = {tid for scores in all_scores for tid in scores}

    aggregated: dict[str, Any] = {}
    for template_id in template_ids:
        sums: dict[str, float] = {}
        count = 0
        for scores in all_scores:
            entry = scores.get(template_id)
            if not isinstance(entry, dict):
                continue
            count += 1
            for key, value in entry.items():
                if key == "total":
                    continue
                if isinstance(value, (int, float)):
                    sums[key] = sums.get(key, 0.0) + value
        if count == 0:
            continue
        averaged = {k: round(v / count, 1) for k, v in sums.items()}
        averaged["total"] = round(sum(averaged.values()), 1)
        aggregated[template_id] = averaged

    best_total = -1.0
    reason = ""
    for p in all_parsed:
        winner = p.get("winner") or ""
        winner_total = float(((p.get("scores") or {}).get(winner) or {}).get("total") or 0)
        if winner_total > best_total:
            best_total = winner_total
            reason = str(p.get("reason") or "").strip()

    return {"scores": aggregated, "reason": reason}


def run_scoring(
    drafts: dict[str, tuple[str, dict[str, Any]]],
    *,
    model: str = SCORER_MODEL,
) -> dict[str, Any]:
    summaries = [
        build_draft_summary(draft_text, validation_report, template_id)
        for template_id, (draft_text, validation_report) in drafts.items()
    ]

    payloads = [score_drafts(summaries, model=model) for _ in range(SCORER_PASSES)]
    aggregated = _aggregate_pass_scores([p.get("result") or {} for p in payloads])

    merged: dict[str, Any] = {"scores": aggregated["scores"], "winner": None, "tiebreaker_applied": False, "reason": aggregated["reason"]}
    merged_payload = apply_tiebreaker({"result": merged, "usage": {}})
    merged = merged_payload.get("result") or {}

    combined_usage = _sum_usage([p.get("usage") or {} for p in payloads])
    token_cost = estimate_token_cost_usd(model, combined_usage) if combined_usage else {}

    return {
        "winner": merged.get("winner"),
        "scores": merged.get("scores") or {},
        "tiebreaker_applied": bool(merged.get("tiebreaker_applied")),
        "reason": str(merged.get("reason") or "").strip(),
        "scorer_usage": combined_usage,
        "scorer_model": model,
        "scorer_generation": payloads[-1].get("generation") or {},
        "scorer_estimated_cost_usd": token_cost.get("total"),
        "scorer_passes": SCORER_PASSES,
    }
