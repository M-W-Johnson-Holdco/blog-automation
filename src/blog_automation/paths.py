"""Repository paths shared across the blog automation package."""

from __future__ import annotations

import shutil
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SRC_DIR = PACKAGE_DIR.parent
PROJECT_ROOT = SRC_DIR.parent

# Single bytecode cache tree under src/ (see blog_automation._pycache_prefix).
BYTECODE_CACHE_DIR = SRC_DIR / "__pycache__"


def configure_bytecode_cache() -> Path:
    """Ensure ``PYTHONPYCACHEPREFIX`` points at ``src/__pycache__/``."""
    from blog_automation._pycache_prefix import apply

    return apply()


# Empty dirs left after moving stages into blog_automation/pipeline/.
LEGACY_PACKAGE_DIR_NAMES = ("approve", "evaluate", "search", "write")


def remove_legacy_package_dirs() -> list[Path]:
    """Delete obsolete approve/search/write/evaluate folders under blog_automation."""
    removed: list[Path] = []
    for name in LEGACY_PACKAGE_DIR_NAMES:
        path = PACKAGE_DIR / name
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
    return removed


PROMPTS_DIR = PROJECT_ROOT / "prompts"
FEEDBACK_DIR = PROJECT_ROOT / "feedback"

# Runtime artifacts are per-company so both tenants can run/commit without
# colliding (output/<slug>/..., generated/<slug>/...).
from blog_automation.company import get_company_slug as _get_company_slug

COMPANY_SLUG = _get_company_slug()
OUTPUT_DIR = PROJECT_ROOT / "output" / COMPANY_SLUG
SOURCES_DIR = OUTPUT_DIR / "sources"
DRAFTS_DIR = OUTPUT_DIR / "drafts"
PIPELINE_RUN_LOG_PATH = DRAFTS_DIR / "pipeline_run.log"
APPROVED_DIR = OUTPUT_DIR / "approved"
LEGACY_APPROVALS_DIR = OUTPUT_DIR / "approvals"
LOGS_DIR = OUTPUT_DIR / "logs"
GENERATED_DIR = PROJECT_ROOT / "generated" / COMPANY_SLUG
GENERATED_RUNS_DIR = GENERATED_DIR / "runs"
GENERATED_APPROVED_DIR = GENERATED_DIR / "approved"
GENERATED_SLACK_INDEX_PATH = GENERATED_DIR / "slack_index.json"
GENERATED_WEEKLY_PIPELINE_PATH = GENERATED_DIR / "weekly_pipeline.json"
