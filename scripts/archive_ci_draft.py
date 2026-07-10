"""Archive the latest CI draft into generated/ for cloud Slack webhooks."""

from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from blog_automation.generated_store import archive_latest_draft_for_ci
from blog_automation.paths import PROJECT_ROOT
from blog_automation.write_common import DEFAULT_OUTPUT_DIR, draft_validation_json_path, latest_markdown_draft
from blog_automation.draft_approval import get_approval_block, load_validation_report


def _latest_slack_target() -> tuple[str, str]:
    draft_path = latest_markdown_draft(DEFAULT_OUTPUT_DIR)
    report = load_validation_report(draft_validation_json_path(draft_path))
    approval = get_approval_block(report)
    channel = str(approval.get("channel") or os.getenv("SLACK_APPROVAL_CHANNEL", "")).strip()
    message_ts = str(approval.get("message_ts") or "").strip()
    if not channel or not message_ts:
        raise EnvironmentError(
            "Could not determine Slack channel/message_ts from the latest validation JSON. "
            "Run approve_post before archive_ci_draft."
        )
    return channel, message_ts


def main(argv: list[str] | None = None) -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Archive latest draft to generated/ for Slack webhooks.")
    parser.add_argument("--run-id", default=os.getenv("GITHUB_RUN_ID", "").strip(), help="GitHub run ID")
    parser.add_argument("--channel", default="", help="Slack channel ID override")
    parser.add_argument("--message-ts", default="", help="Slack message timestamp override")
    args = parser.parse_args(argv)

    run_id = args.run_id or f"local-{latest_markdown_draft(DEFAULT_OUTPUT_DIR).stem[:32]}"
    channel = args.channel.strip()
    message_ts = args.message_ts.strip()
    if not channel or not message_ts:
        channel, message_ts = _latest_slack_target()

    validation_path = archive_latest_draft_for_ci(
        run_id=run_id,
        channel=channel,
        message_ts=message_ts,
    )
    print(json.dumps({"validation_path": str(validation_path.relative_to(PROJECT_ROOT)), "run_id": run_id}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[archive_ci_draft] Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
