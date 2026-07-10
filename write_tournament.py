#!/usr/bin/env python3
"""Source-mode writing run: search (optional) → write_multi → tournament summary + run log.

Reads kept sources (or runs search first with ``--with-search``), generates the
appropriate template(s), saves the winner under ``output/drafts/``, and writes
artifacts under ``output/multi_run/<timestamp>/`` including:

- ``run.log`` — plain-text CLI transcript (search through tournament summary)
- ``tournament_summary.md`` — human-readable results summary
- ``score_result.json`` — machine-readable scores

A copy of the log is also saved to ``output/drafts/pipeline_run.log`` for Slack
approval uploads on CI.

Examples:
    python write_tournament.py
    python write_tournament.py --with-search
    python write_tournament.py --with-search --all-queries --clear-drafts
    python write_tournament.py --no-pdf

Requires ``output/sources/kept_sources.json`` unless ``--with-search`` is used.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
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

from blog_automation.paths import OUTPUT_DIR, PROJECT_ROOT
from blog_automation.pipeline.runner import (
    DEFAULT_SEARCH_MODULE_ARGS,
    init_pipeline_log_at,
    publish_canonical_pipeline_log,
    run_module,
)
from blog_automation.llm_models import default_writing_model
from blog_automation.pipeline.write_serverless import (
    activate_pipeline_default_write_model,
    model_label,
)

MULTI_RUN_SUBDIR = "multi_run"


def _build_write_multi_argv(
    multi_run_dir: Path,
    passthrough: list[str],
) -> list[str]:
    args = ["--multi-run-dir", str(multi_run_dir), *passthrough]
    if not any(
        token == "--model" or (token.startswith("--model="))
        for token in passthrough
    ):
        args = ["--model", default_writing_model(), *args]
    return args


def _build_search_argv(args: argparse.Namespace) -> tuple[str, ...]:
    search_args = [
        arg
        for arg in DEFAULT_SEARCH_MODULE_ARGS
        if arg != "--confirm-national-fallback"
    ]
    if args.all_queries:
        search_args.append("--all-queries")
    if args.include_used_sources:
        search_args.append("--include-used-sources")
    return tuple(search_args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run write_multi with a per-run CLI log under output/multi_run/<timestamp>/.",
    )
    parser.add_argument(
        "--with-search",
        action="store_true",
        help="Run search + incremental evaluate before writing (uses strong search defaults).",
    )
    parser.add_argument(
        "--all-queries",
        action="store_true",
        help="With --with-search: run every search query (not just the credit-capped rotation).",
    )
    parser.add_argument(
        "--include-used-sources",
        action="store_true",
        help="With --with-search: do not skip URLs already listed in used_sources.json.",
    )
    args, passthrough = parser.parse_known_args()

    activate_pipeline_default_write_model()

    run_stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    multi_run_dir = (OUTPUT_DIR / MULTI_RUN_SUBDIR / run_stamp).resolve()
    multi_run_dir.mkdir(parents=True, exist_ok=True)
    log_path = init_pipeline_log_at(
        multi_run_dir / "run.log",
        title="Peachtree write_tournament run log",
    )

    print(f"[write_tournament] Generation model: {model_label(default_writing_model())}")
    print(f"[write_tournament] Multi-run dir: {multi_run_dir.relative_to(PROJECT_ROOT)}")

    if args.with_search:
        if run_module(
            "search",
            *_build_search_argv(args),
            label="Search + evaluate (incremental)",
        ) != 0:
            raise SystemExit(1)

    write_args = _build_write_multi_argv(multi_run_dir, passthrough)
    if run_module("write_multi", *write_args, label="Write tournament") != 0:
        raise SystemExit(1)

    published = publish_canonical_pipeline_log(log_path)
    if published:
        print(
            f"[write_tournament] Canonical log: {published.relative_to(PROJECT_ROOT)}",
            flush=True,
        )

    print(
        f"[write_tournament] Done — log: {log_path.relative_to(PROJECT_ROOT)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
