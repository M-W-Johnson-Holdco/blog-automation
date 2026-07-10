"""Per-company profile selection.

The active company is chosen by the ``COMPANY`` environment variable
(``peachtree`` or ``tc``). Every brand-, geo-, and market-specific value
lives in ``blog_automation/companies/<slug>.py``; shared pipeline code asks
this module for the active profile instead of hardcoding brand constants.

Set ``COMPANY`` before importing pipeline modules (the CLI's ``--company``
flag does this for you). Defaults to ``peachtree`` for backwards
compatibility with existing tests and local usage.
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType

VALID_COMPANIES = ("peachtree", "tc")
DEFAULT_COMPANY = "peachtree"

_profile_cache: dict[str, ModuleType] = {}


def get_company_slug() -> str:
    slug = os.getenv("COMPANY", "").strip().lower() or DEFAULT_COMPANY
    if slug not in VALID_COMPANIES:
        raise RuntimeError(
            f"Unknown COMPANY={slug!r}; expected one of {', '.join(VALID_COMPANIES)}"
        )
    return slug


def get_profile() -> ModuleType:
    """Return the active company profile module (cached per slug)."""
    slug = get_company_slug()
    if slug not in _profile_cache:
        _profile_cache[slug] = importlib.import_module(f"blog_automation.companies.{slug}")
    return _profile_cache[slug]


def render_template(text: str) -> str:
    """Substitute ``{{key}}`` placeholders from the profile's PROMPT_VARS."""
    profile = get_profile()
    for key, value in profile.PROMPT_VARS.items():
        text = text.replace("{{" + key + "}}", value)
    return text
