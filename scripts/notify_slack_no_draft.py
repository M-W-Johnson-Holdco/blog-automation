"""Post Slack messages when scheduled weekly runs cannot produce a draft."""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from blog_automation.paths import PROJECT_ROOT
from blog_automation.pipeline.approve_listen import get_approval_channel, get_slack_client
from blog_automation.weekly_pipeline import (
    FAILURE_REASON_ANTHROPIC_CREDITS,
    credits_failure_slack_text,
    current_iso_week,
    manual_failure_slack_text,
    mark_credits_failure_notified,
    mark_monday_failure_notified,
    mark_no_draft_notified,
    monday_failure_slack_text,
    no_draft_slack_text,
    resolve_pipeline_failure_reason,
    should_notify_credits_failure,
    should_notify_monday_failure,
    should_notify_no_draft,
)


def main(argv: list[str] | None = None) -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Notify Slack when no draft this week.")
    parser.add_argument("--day", required=True, help="Scheduled day from weekly plan")
    args = parser.parse_args(argv)

    failure_reason = resolve_pipeline_failure_reason()
    iso_week = current_iso_week()
    day = str(args.day or "").strip().lower() or "manual"

    # Credit failures notify for any day (including manual), separately from quiet-news alerts.
    if failure_reason == FAILURE_REASON_ANTHROPIC_CREDITS:
        if not should_notify_credits_failure():
            print("[notify_slack_no_draft] Skipping — credits failure already notified this week.")
            return
        text = credits_failure_slack_text(iso_week=iso_week)
        if day == "monday":
            text += " Wednesday auto-retry will also fail until credits are restored."
        elif day == "wednesday":
            text += " Next scheduled run: Monday."
        mark = mark_credits_failure_notified
    elif day == "monday":
        if not should_notify_monday_failure():
            print("[notify_slack_no_draft] Skipping — draft exists or already notified.")
            return
        text = monday_failure_slack_text(iso_week=iso_week, reason=failure_reason)
        mark = mark_monday_failure_notified
    elif day == "wednesday":
        if not should_notify_no_draft():
            print("[notify_slack_no_draft] Skipping — draft exists or already notified.")
            return
        text = no_draft_slack_text(iso_week=iso_week, reason=failure_reason)
        mark = mark_no_draft_notified
    else:
        # Manual / other UI runs: always post so operators get the same Slack feedback.
        text = manual_failure_slack_text(iso_week=iso_week, reason=failure_reason)
        mark = None

    if failure_reason:
        print(f"[notify_slack_no_draft] Failure reason: {failure_reason}")

    client = get_slack_client()
    channel = get_approval_channel()
    response = client.chat_postMessage(channel=channel, text=text)
    if mark is not None:
        mark()
    print(
        "[notify_slack_no_draft] Posted no-draft message "
        f"(day={day}, ts={response.get('ts', '')})"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[notify_slack_no_draft] Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
