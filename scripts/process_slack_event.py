"""Process one Slack event in GitHub Actions (Cloudflare Worker dispatches this workflow)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from blog_automation.paths import GENERATED_DIR, PROJECT_ROOT, SOURCES_DIR
from blog_automation.slack_actions.processor import (
    decode_event_from_github_input,
    process_slack_event,
    should_process_slack_event,
)


def main(argv: list[str] | None = None) -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Process a Slack Events API payload.")
    parser.add_argument("--event-b64", default=os.getenv("SLACK_EVENT_B64", "").strip(), help="Base64 Slack event JSON")
    parser.add_argument("--event-json", default="", help="Raw Slack event JSON string")
    parser.add_argument(
        "--no-auto-rewrite",
        action="store_true",
        help="Do not auto-rewrite from thread feedback (not recommended in Actions)",
    )
    parser.add_argument(
        "--result-json",
        type=Path,
        default=None,
        help="Write machine-readable result JSON here (CI uses this instead of parsing stdout).",
    )
    args = parser.parse_args(argv)

    if args.event_b64:
        event = decode_event_from_github_input(args.event_b64)
    elif args.event_json:
        event = json.loads(args.event_json)
    elif not sys.stdin.isatty():
        event = json.load(sys.stdin)
    else:
        raise SystemExit("Provide --event-b64, --event-json, or stdin JSON.")

    if not should_process_slack_event(event):
        payload = {"changed": False, "skipped": True}
        _emit_result(payload, args.result_json)
        return

    changed = process_slack_event(event, auto_rewrite=not args.no_auto_rewrite)
    payload = {
        "changed": changed,
        "skipped": False,
        "commit_paths": [
            str(GENERATED_DIR.relative_to(PROJECT_ROOT)),
            str((SOURCES_DIR / "used_sources.json").relative_to(PROJECT_ROOT)),
        ],
    }
    _emit_result(payload, args.result_json)


def _emit_result(payload: dict, result_json: Path | None) -> None:
    if result_json is not None:
        result_json.parent.mkdir(parents=True, exist_ok=True)
        result_json.write_text(json.dumps(payload), encoding="utf-8")
        print(f"[process_slack_event] Wrote result to {result_json}")
    else:
        print(json.dumps(payload))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[process_slack_event] Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
