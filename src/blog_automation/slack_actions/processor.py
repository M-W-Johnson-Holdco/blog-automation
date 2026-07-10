"""Process Slack Events API payloads using existing approve_listen handlers."""

from __future__ import annotations

import base64
import json
from typing import Any

from blog_automation.pipeline.approve_listen import (
    RESTART_REACTIONS,
    get_approval_channel,
    get_bot_user_id,
    get_slack_client,
    handle_message,
    handle_reaction,
    handle_reaction_removed,
)
from blog_automation.slack_actions.pipeline_mention import (
    handle_pipeline_mention_event,
    queue_weekly_pipeline_via_github,
)

_IGNORE_MESSAGE_SUBTYPES = frozenset({"bot_message", "message_changed", "message_deleted"})


def _is_thread_reply(event: dict[str, Any]) -> bool:
    thread_ts = str(event.get("thread_ts") or "").strip()
    ts = str(event.get("ts") or "").strip()
    return bool(thread_ts and ts and thread_ts != ts)


def should_process_slack_event(event: dict[str, Any]) -> bool:
    if event.get("bot_id"):
        return False
    if event.get("subtype") in _IGNORE_MESSAGE_SUBTYPES:
        return False
    event_type = event.get("type")
    if event_type in {"reaction_added", "reaction_removed"}:
        return True
    if event_type == "message":
        return _is_thread_reply(event)
    return False


def encode_event_for_github_input(event: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(event, separators=(",", ":")).encode("utf-8")).decode("ascii")


def decode_event_from_github_input(event_b64: str) -> dict[str, Any]:
    payload = json.loads(base64.b64decode(event_b64.encode("ascii")).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Slack event payload must be a JSON object.")
    return payload


def _handle_repeat_via_github_actions(client: Any, *, channel: str, thread_ts: str, user: str) -> None:
    queue_weekly_pipeline_via_github(client, channel=channel, thread_ts=thread_ts, user=user)


def process_slack_event(event: dict[str, Any], *, auto_rewrite: bool = True) -> bool:
    """
    Run approve / publish / feedback handlers for one Slack event.

    Returns True when local draft or source registry files may have changed and should be committed.
    """
    if not should_process_slack_event(event):
        return False

    client = get_slack_client()
    bot_user_id = get_bot_user_id(client)
    event_type = event.get("type")
    changed = False

    if event_type == "reaction_added":
        reaction = event.get("reaction")
        item = event.get("item", {})
        if reaction in RESTART_REACTIONS and item.get("type") == "message":
            user = event.get("user", "unknown")
            # Ignore bot-added prompt reactions (add_approval_prompt_reactions adds :repeat:).
            if bot_user_id and user == bot_user_id:
                return False
            channel = item.get("channel")
            ts = item.get("ts")
            if channel and ts:
                _handle_repeat_via_github_actions(client, channel=channel, thread_ts=ts, user=user)
            return False

        handle_reaction(
            event,
            client,
            bot_user_id=bot_user_id,
            auto_rewrite=auto_rewrite,
        )
        changed = True
    elif event_type == "reaction_removed":
        changed = handle_reaction_removed(event, client, bot_user_id=bot_user_id)
    elif event_type == "message":
        changed = handle_message(
            event,
            client,
            auto_rewrite=auto_rewrite,
            bot_user_id=bot_user_id,
        )
    elif event_type == "app_mention":
        handle_pipeline_mention_event(
            event,
            client,
            approval_channel=get_approval_channel(),
        )

    return changed
