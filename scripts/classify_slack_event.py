"""Classify Slack events for lightweight approval workflow setup."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Any


POST_COMMANDS = {"publish", "draft"}
IGNORE_SUBTYPES = {"bot_message", "message_changed", "message_deleted"}


def decode_event_b64(event_b64: str) -> dict[str, Any]:
    payload = json.loads(base64.b64decode(event_b64.encode("ascii")).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Slack event payload must be a JSON object.")
    return payload


def is_thread_reply(event: dict[str, Any]) -> bool:
    thread_ts = str(event.get("thread_ts") or "").strip()
    ts = str(event.get("ts") or "").strip()
    return bool(thread_ts and ts and thread_ts != ts)


def is_simple_post_command(text: str) -> bool:
    return text.strip().lower() in POST_COMMANDS


def should_process_approval_event(event: dict[str, Any]) -> bool:
    """Mirror the approval workflow's event scope before installing dependencies."""
    if event.get("bot_id"):
        return False
    if event.get("subtype") in IGNORE_SUBTYPES:
        return False
    event_type = event.get("type")
    if event_type in {"reaction_added", "reaction_removed"}:
        return True
    if event_type == "message":
        return is_thread_reply(event)
    return False


def should_skip_event(event: dict[str, Any]) -> bool:
    return not should_process_approval_event(event)


def needs_pdf_deps(event: dict[str, Any]) -> bool:
    """Only free-form thread feedback can auto-rewrite and generate a PDF."""
    if should_skip_event(event):
        return False
    if event.get("type") != "message":
        return False
    if not is_thread_reply(event):
        return False
    return not is_simple_post_command(str(event.get("text") or ""))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Classify Slack event dependency needs.")
    parser.add_argument("--event-b64", default="", help="Base64-encoded Slack event JSON")
    parser.add_argument("--event-json", default="", help="Raw Slack event JSON")
    parser.add_argument(
        "--github-output",
        default="",
        help="Optional GitHub Actions output file path.",
    )
    args = parser.parse_args(argv)

    if args.event_b64:
        event = decode_event_b64(args.event_b64)
    elif args.event_json:
        event = json.loads(args.event_json)
    elif not sys.stdin.isatty():
        event = json.load(sys.stdin)
    else:
        raise SystemExit("Provide --event-b64, --event-json, or stdin JSON.")

    skip_value = "true" if should_skip_event(event) else "false"
    pdf_value = "true" if needs_pdf_deps(event) else "false"
    print(f"skip_event={skip_value}")
    print(f"needs_pdf_deps={pdf_value}")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"skip_event={skip_value}\n")
            handle.write(f"needs_pdf_deps={pdf_value}\n")


if __name__ == "__main__":
    main()
