"""Handle top-level @bot pipeline mentions in the approval channel."""

from __future__ import annotations

import re
from typing import Any

from blog_automation.slack_actions.github_trigger import trigger_github_workflow

PIPELINE_MENTION_COMMAND = "pipeline"
_MENTION_PATTERN = re.compile(r"<@[^>]+>")


def strip_slack_mentions(text: str) -> str:
    """Remove Slack user/bot mention tokens from message text."""
    return _MENTION_PATTERN.sub("", text).strip()


def is_pipeline_mention_command(text: str) -> bool:
    """True when mention text is exactly ``pipeline`` (case-insensitive)."""
    return strip_slack_mentions(text).lower() == PIPELINE_MENTION_COMMAND


def pipeline_mention_help_text() -> str:
    return (
        "To run a full search → write → Slack post, mention me in this channel with: "
        "`@PT Blog Bot pipeline` (top-level message, not inside a draft thread)."
    )


def queue_weekly_pipeline_via_github(
    client: Any,
    *,
    channel: str,
    thread_ts: str,
    user: str,
) -> None:
    """Dispatch weekly.yml and confirm in the Slack thread."""
    trigger_github_workflow("weekly.yml", {"send_to_slack": "true"})
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=(
            f"<@{user}> queued the Weekly Blog Pipeline in GitHub Actions. "
            "A new draft will post here when the run finishes."
        ),
    )


def handle_pipeline_mention_event(
    event: dict[str, Any],
    client: Any,
    *,
    approval_channel: str,
) -> bool:
    """
    Process ``app_mention`` events that request a manual pipeline run.

    Returns True when the event was handled (including usage hints); False when ignored.
    """
    if event.get("type") != "app_mention":
        return False
    if event.get("thread_ts"):
        return False

    channel = str(event.get("channel") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    user = str(event.get("user") or "unknown").strip()
    text = str(event.get("text") or "")

    if not channel or not message_ts:
        return False
    if channel != approval_channel:
        return False

    if not is_pipeline_mention_command(text):
        client.chat_postMessage(
            channel=channel,
            thread_ts=message_ts,
            text=pipeline_mention_help_text(),
        )
        return True

    try:
        queue_weekly_pipeline_via_github(
            client,
            channel=channel,
            thread_ts=message_ts,
            user=user,
        )
    except Exception as exc:
        client.chat_postMessage(
            channel=channel,
            thread_ts=message_ts,
            text=f"Could not queue the Weekly Blog Pipeline: {exc}",
        )
    return True
