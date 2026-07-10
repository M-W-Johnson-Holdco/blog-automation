"""Set PYTHONPYCACHEPREFIX before other blog_automation modules load (import first)."""

from __future__ import annotations

import os
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
_CACHE_DIR = _SRC_DIR / "__pycache__"


def apply() -> Path:
    cache_dir = _CACHE_DIR.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PYTHONPYCACHEPREFIX"] = str(cache_dir)
    return cache_dir


apply()
