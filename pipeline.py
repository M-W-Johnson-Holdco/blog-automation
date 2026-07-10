#!/usr/bin/env python3
"""Blog pipeline entry point (runs stages from src/blog_automation).

Multi-tenant: pass ``--company peachtree`` or ``--company tc`` (or set the
``COMPANY`` env var) to pick which brand's profile, prompts, and PSAI tenant
to use. Defaults to peachtree.

Stages match GitHub Actions ``weekly.yml``. Slack approval is **not** local listen —
use Cloudflare Worker → ``slack_approve.yml`` (see ``docs/cloudflare-workers-setup.md``).

**CI flow (weekly.yml):**
1. ``write_tournament.py --with-search --clear-drafts``
2. ``pipeline.py --stage approve_post``
3. ``scripts/archive_ci_draft.py`` (inside approve_post stage locally; separate GH step on CI)

**Manual GitHub Actions:**
    gh workflow run weekly.yml --field send_to_slack=true
    gh workflow run weekly.yml --field widen_search=true --field send_to_slack=true

Examples:
    python pipeline.py                              # interactive menu
    python pipeline.py --default                    # 397B write model
    python pipeline.py --all --default --send-to-slack   # full CI flow locally
    python pipeline.py --stage search
    python pipeline.py --stage write_tournament --with-search --clear-drafts
    python pipeline.py --stage approve_post         # post + archive (no listen)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --company must be applied before any blog_automation import binds a profile.
if "--company" in sys.argv:
    _idx = sys.argv.index("--company")
    if _idx + 1 >= len(sys.argv):
        sys.exit("--company requires a value: peachtree or tc")
    os.environ["COMPANY"] = sys.argv[_idx + 1]
    del sys.argv[_idx : _idx + 2]

_SRC = Path(__file__).resolve().parent / "src"
_cache = _SRC / "__pycache__"
_cache.mkdir(parents=True, exist_ok=True)
os.environ["PYTHONPYCACHEPREFIX"] = str(_cache.resolve())
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from blog_automation.paths import remove_legacy_package_dirs

for _legacy in remove_legacy_package_dirs():
    pass  # cleanup empty approve/search/write/evaluate dirs from old layout

from blog_automation.pipeline.cli import main

if __name__ == "__main__":
    main()
