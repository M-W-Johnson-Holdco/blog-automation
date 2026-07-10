"""Pipeline orchestrator and interactive stage picker."""

from __future__ import annotations

import blog_automation._pycache_prefix  # noqa: F401

import argparse

from blog_automation.pipeline.runner import (
    DEFAULT_SEARCH_MODULE_ARGS,
    SEARCH_EVAL_CI_ARGS,
    build_write_tournament_args,
    run_module,
    run_repo_script,
    run_write_tournament,
)
from blog_automation.pipeline.write_serverless import (
    DEFAULT_SERVERLESS_MODEL,
    activate_pipeline_default_write_model,
)
from blog_automation.paths import PROJECT_ROOT
from blog_automation.company import get_profile
from dotenv import load_dotenv

# Menu mirrors GitHub Actions weekly.yml. Slack reactions → Cloudflare Worker → slack_approve.yml.
MENU_OPTIONS: list[tuple[str, str]] = [
    ("search_eval", "Search + evaluate (incremental — same as inside write_tournament)"),
    ("write_tournament", "Write tournament (from kept_sources.json)"),
    (
        "approve_post",
        "Post draft to Slack + archive for Cloudflare Worker (no local listen)",
    ),
    (
        "full_write",
        "Full write (CI step 1: write_tournament --with-search --clear-drafts)",
    ),
    (
        "full",
        "Full pipeline (CI: write → Slack post → archive; approve via Cloudflare + GH Actions)",
    ),
    ("exit", "Exit"),
]

CLOUDFLARE_APPROVAL_NOTE = """
[pipeline] Slack ✅ / 🌐 / thread replies are handled in the cloud:
  Cloudflare Worker → GitHub Actions (slack_approve.yml) → process_slack_event.py
  Do not run `approve_listen listen` locally while the Worker is active.
  Setup: docs/cloudflare-workers-setup.md

[pipeline] Manual GitHub Actions:
  gh workflow run weekly.yml --field send_to_slack=true
  gh workflow run weekly.yml --field widen_search=true --field send_to_slack=true
""".strip()


def print_stage_menu() -> None:
    print("\nSelect part of the pipeline you want to initiate:\n")
    for index, (_, label) in enumerate(MENU_OPTIONS, start=1):
        print(f"  {index}. {label}")
    print()


def read_menu_choice() -> int:
    while True:
        raw = input("Enter number: ").strip()
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(MENU_OPTIONS):
                return choice
        print(f"Please enter a number from 1 to {len(MENU_OPTIONS)}.")


def print_cloudflare_approval_note(*, remind_push: bool = False) -> None:
    print(CLOUDFLARE_APPROVAL_NOTE, flush=True)
    if remind_push:
        print(
            "[pipeline] Local archive wrote to generated/ — commit and push so the Worker "
            "can git pull this draft on the next reaction.",
            flush=True,
        )


def run_search_eval(*, ci_aligned: bool = False) -> int:
    search_args = SEARCH_EVAL_CI_ARGS if ci_aligned else DEFAULT_SEARCH_MODULE_ARGS
    return run_module(
        "search",
        *search_args,
        label="Search + evaluate (incremental)",
    )


def run_write_tournament_stage(*, with_search: bool = False, clear_drafts: bool = False) -> int:
    args = build_write_tournament_args(
        with_search=with_search,
        clear_drafts=clear_drafts,
        all_queries=True,
    )
    label = "Write tournament (CI)" if with_search else "Write tournament"
    return run_write_tournament(*args, label=label)


def run_full_write_ci(*, all_queries: bool = True, include_used_sources: bool = False) -> int:
    args = build_write_tournament_args(
        with_search=True,
        clear_drafts=True,
        all_queries=all_queries,
        include_used_sources=include_used_sources,
    )
    return run_write_tournament(*args, label="Write tournament (CI — search + write)")


def run_approve_post_stage(*, archive: bool = True) -> int:
    """Post latest draft (+ run.txt Slack log attachment) and archive to generated/ for cloud approval."""
    load_dotenv(PROJECT_ROOT / ".env")
    code = run_module("approve_listen", "post", "--latest", label="Post draft to Slack")
    if code != 0:
        return code
    if archive:
        code = run_repo_script(
            "scripts/archive_ci_draft.py",
            label="Archive draft for Cloudflare Worker",
        )
        if code != 0:
            return code
        print_cloudflare_approval_note(remind_push=True)
        return 0
    print_cloudflare_approval_note(remind_push=False)
    return 0


def run_full_pipeline_and_approve(
    *,
    all_queries: bool = True,
    include_used_sources: bool = False,
) -> None:
    """CI write step, then Slack post + archive (cloud approval — no local listen)."""
    load_dotenv(PROJECT_ROOT / ".env")
    if run_full_write_ci(all_queries=all_queries, include_used_sources=include_used_sources) != 0:
        print("[pipeline] Stopped before approve — fix the failed stage and try again.")
        return
    if run_approve_post_stage(archive=True) != 0:
        print("[pipeline] Stopped after failed Slack post/archive step.")
        return


def run_menu_choice(choice: int) -> None:
    """Run one menu stage, then return so the interactive menu can show again."""
    stage_key = MENU_OPTIONS[choice - 1][0]

    if stage_key == "search_eval":
        run_search_eval(ci_aligned=False)
    elif stage_key == "write_tournament":
        run_write_tournament_stage(clear_drafts=False)
    elif stage_key == "approve_post":
        run_approve_post_stage(archive=True)
    elif stage_key == "full_write":
        run_full_write_ci()
    elif stage_key == "full":
        run_full_pipeline_and_approve()


def run_interactive_menu() -> None:
    while True:
        print_stage_menu()
        choice = read_menu_choice()
        if MENU_OPTIONS[choice - 1][0] == "exit":
            print("[pipeline] Exiting.")
            return
        run_menu_choice(choice)


def run_full_pipeline(
    *,
    send_to_slack: bool,
    all_queries: bool = False,
    include_used_sources: bool = False,
) -> None:
    """Non-interactive CI-aligned write; optional Slack post + archive (no local listen)."""
    load_dotenv(PROJECT_ROOT / ".env")
    code = run_full_write_ci(
        all_queries=all_queries,
        include_used_sources=include_used_sources,
    )
    if code != 0:
        raise SystemExit(code)
    if send_to_slack:
        if run_approve_post_stage(archive=True) != 0:
            raise SystemExit(1)
    print("[pipeline] Pipeline completed successfully")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            f"{get_profile().COMPANY_SHORT} blog pipeline — interactive menu by default. "
            "Write stages match weekly.yml; Slack approval is handled by "
            "Cloudflare Worker + slack_approve.yml (not local listen)."
        )
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Run CI write step: write_tournament --with-search --clear-drafts "
            "(same as weekly.yml)."
        ),
    )
    parser.add_argument(
        "--send-to-slack",
        action="store_true",
        help=(
            "With --all: post draft to Slack and archive to generated/ "
            "(CI approve step; reactions handled in cloud)."
        ),
    )
    parser.add_argument(
        "--stage",
        choices=(
            "search",
            "search_eval",
            "evaluate",
            "write",
            "write_tournament",
            "write_serverless",
            "approve_post",
            "archive_draft",
            "approve_listen",
            "clean",
        ),
        help="Run one stage non-interactively (skips the menu).",
    )
    parser.add_argument(
        "--default",
        action="store_true",
        help=(
            "Use Qwen3.5 397B for write and Slack rewrite/revision without the model menu "
            f"({DEFAULT_SERVERLESS_MODEL})."
        ),
    )
    parser.add_argument(
        "--all-queries",
        action="store_true",
        help="With --all or write_tournament: run every search query (weekly widen_search).",
    )
    parser.add_argument(
        "--include-used-sources",
        action="store_true",
        help="With --all: do not skip URLs already listed in used_sources.json.",
    )
    parser.add_argument(
        "--with-search",
        action="store_true",
        help="With --stage write / write_tournament: run search+evaluate before writing.",
    )
    parser.add_argument(
        "--clear-drafts",
        action="store_true",
        help="With --stage write / write_tournament: clear output/drafts before writing.",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="With --stage approve_post: skip archive_ci_draft (not recommended for cloud approval).",
    )
    args = parser.parse_args(argv)

    if args.default:
        activate_pipeline_default_write_model()
        print(f"[pipeline] Default write model: {DEFAULT_SERVERLESS_MODEL}")

    if args.all:
        run_full_pipeline(
            send_to_slack=args.send_to_slack,
            all_queries=args.all_queries,
            include_used_sources=args.include_used_sources,
        )
        return

    if args.stage:
        if args.stage in {"search", "search_eval"}:
            run_search_eval(ci_aligned=True)
        elif args.stage == "evaluate":
            run_module("evaluate", label="Evaluate search_results.json (no Tavily)")
        elif args.stage in {"write", "write_tournament"}:
            tournament_args = build_write_tournament_args(
                with_search=args.with_search,
                clear_drafts=args.clear_drafts,
                all_queries=args.all_queries or args.with_search,
                include_used_sources=args.include_used_sources,
            )
            run_write_tournament(*tournament_args, label="Write tournament")
        elif args.stage == "write_serverless":
            run_module("write_serverless", label="Write draft (legacy single template)")
        elif args.stage == "approve_post":
            load_dotenv(PROJECT_ROOT / ".env")
            raise SystemExit(run_approve_post_stage(archive=not args.no_archive))
        elif args.stage == "archive_draft":
            load_dotenv(PROJECT_ROOT / ".env")
            code = run_repo_script(
                "scripts/archive_ci_draft.py",
                label="Archive draft for Cloudflare Worker",
            )
            if code == 0:
                print_cloudflare_approval_note(remind_push=True)
            raise SystemExit(code)
        elif args.stage == "approve_listen":
            print(
                "[pipeline] Warning: local Socket Mode listen conflicts with the Cloudflare "
                "Worker path. Prefer Cloudflare + slack_approve.yml.",
                flush=True,
            )
            run_module("approve_listen", label="Approve listen (Socket Mode — local dev only)")
        elif args.stage == "clean":
            run_module("clean_output", label="Clean output")
        return

    run_interactive_menu()


if __name__ == "__main__":
    main()
