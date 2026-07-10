"""Weekly-rotating blog writing prompt variants and source-mode routing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blog_automation.paths import PROJECT_ROOT, PROMPTS_DIR
from blog_automation.company import render_template

DEFAULT_WRITING_PROMPT_ID = "geo"
SUMMARY_HEADING_WHO = "Who this affects"
SUMMARY_HEADING_WHAT = "What to do"
SUMMARY_HEADING_WHEN = "Timeline"


@dataclass(frozen=True)
class WritingPromptVariant:
    id: str
    label: str
    path: Path
    summary_heading: str
    opening_style: str

    @property
    def summary_heading_markdown(self) -> str:
        return f"**{self.summary_heading}:**"


WRITING_PROMPT_VARIANTS: tuple[WritingPromptVariant, ...] = (
    WritingPromptVariant(
        id="geo",
        label="GEO news + Quick Answer",
        path=PROMPTS_DIR / "blog_geo.txt",
        summary_heading="The short answer",
        opening_style="news-anchored",
    ),
    WritingPromptVariant(
        id="scenario",
        label="Scenario-led vignette",
        path=PROMPTS_DIR / "blog_scenario.txt",
        summary_heading="The short answer",
        opening_style="scenario-led",
    ),
    WritingPromptVariant(
        id="explainer",
        label="Definition-first explainer",
        path=PROMPTS_DIR / "blog_explainer.txt",
        summary_heading="The short answer",
        opening_style="definition-first",
    ),
    WritingPromptVariant(
        id="local_anchor",
        label="Local news + national authority",
        path=PROMPTS_DIR / "blog_local_anchor.txt",
        summary_heading="The short answer",
        opening_style="local-anchor",
    ),
    WritingPromptVariant(
        id="industry_insight",
        label="Industry insight for local homeowners",
        path=PROMPTS_DIR / "blog_industry_insight.txt",
        summary_heading="The short answer",
        opening_style="industry-insight",
    ),
)

# Only these three rotate in single-write (write_serverless) mode.
_ROTATION_VARIANT_IDS = ("geo", "scenario", "explainer")

# Template sets selected based on source geography composition.
# local   = 2+ metro-area priority/secondary sources → single ISO-week-rotated template
# mixed   = exactly 1 local + 1+ national sources   → single local_anchor draft
# national = 0 local sources, national/trade only       → single industry_insight draft
SOURCE_MODE_TEMPLATE_IDS: dict[str, tuple[str, ...]] = {
    "local": ("geo", "scenario", "explainer"),
    "mixed": ("local_anchor",),
    "national": ("industry_insight",),
}

_VARIANT_BY_ID = {variant.id: variant for variant in WRITING_PROMPT_VARIANTS}


def writing_prompt_variant_ids() -> tuple[str, ...]:
    return tuple(variant.id for variant in WRITING_PROMPT_VARIANTS)


def get_writing_prompt_variant(variant_id: str) -> WritingPromptVariant:
    key = str(variant_id or "").strip().lower()
    try:
        return _VARIANT_BY_ID[key]
    except KeyError as exc:
        known = ", ".join(writing_prompt_variant_ids())
        raise ValueError(f"Unknown writing prompt {variant_id!r}. Choose one of: {known}") from exc


def select_writing_prompt_variant(
    *,
    rotation_week: int | None = None,
    variant_id: str | None = None,
) -> WritingPromptVariant:
    if variant_id and str(variant_id).strip().lower() not in {"", "auto"}:
        return get_writing_prompt_variant(variant_id)

    rotation_variants = [v for v in WRITING_PROMPT_VARIANTS if v.id in _ROTATION_VARIANT_IDS]
    week = rotation_week or datetime.now(timezone.utc).isocalendar().week
    index = (week - 1) % len(rotation_variants)
    return rotation_variants[index]


def classify_source_mode(sources: list[dict[str, Any]]) -> str:
    """Return 'local', 'mixed', or 'national' based on how many kept sources are
    from metro-area priority/secondary domains.

    Sources carry ``priority_source`` and ``secondary_source`` boolean flags set
    during the Tavily ingest step.  The evaluated wrapper may nest the raw source
    under a ``"source"`` key, so we check both levels.
    """
    def _is_local(item: dict[str, Any]) -> bool:
        inner = item.get("source", item)
        return bool(
            inner.get("priority_source") or inner.get("secondary_source")
            or item.get("priority_source") or item.get("secondary_source")
        )

    local_count = sum(1 for s in sources if _is_local(s))
    if local_count >= 2:
        return "local"
    if local_count == 1:
        return "mixed"
    return "national"


def resolve_template_ids(
    sources: list[dict[str, Any]],
    *,
    rotation_week: int | None = None,
) -> tuple[str, ...]:
    """Return the appropriate template ID tuple for the given source composition."""
    mode = classify_source_mode(sources)
    template_ids = SOURCE_MODE_TEMPLATE_IDS[mode]
    if mode == "local" and len(template_ids) > 1:
        variant = select_writing_prompt_variant(rotation_week=rotation_week)
        return (variant.id,)
    return template_ids


def describe_writing_prompt_rotation(*, rotation_week: int | None = None) -> str:
    """Human-readable note for logs: which template this ISO week maps to."""
    week = rotation_week or datetime.now(timezone.utc).isocalendar().week
    variant = select_writing_prompt_variant(rotation_week=week)
    return f"ISO week {week} → {variant.id} ({variant.label})"


def load_writing_prompt_text(variant: WritingPromptVariant) -> str:
    if not variant.path.is_file():
        raise FileNotFoundError(f"Writing prompt not found: {variant.path.relative_to(PROJECT_ROOT)}")
    return render_template(variant.path.read_text(encoding="utf-8"))


def writing_prompt_metadata(variant: WritingPromptVariant) -> dict[str, str]:
    return {
        "id": variant.id,
        "label": variant.label,
        "path": str(variant.path.relative_to(PROJECT_ROOT)),
        "summary_heading": variant.summary_heading,
        "opening_style": variant.opening_style,
    }
