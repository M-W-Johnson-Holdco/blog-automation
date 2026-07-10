"""Handle Slack Events API payloads (Render/FastAPI or local testing)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

from blog_automation.slack_actions.processor import process_slack_event, should_process_slack_event
from blog_automation.slack_webhook.github_sync import commit_paths, github_sync_enabled, pull_latest


def signing_secret() -> str:
    secret = os.getenv("SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        raise EnvironmentError("SLACK_SIGNING_SECRET is not set.")
    return secret


def verify_slack_signature(*, timestamp: str, body: bytes, signature: str) -> None:
    if abs(time.time() - int(timestamp)) > 60 * 5:
        raise ValueError("Slack request timestamp is too old.")

    base = f"v0:{timestamp}:{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(signing_secret().encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    if not hmac.compare_digest(expected, signature):
        raise ValueError("Invalid Slack signature.")


def process_slack_event_with_git_sync(event: dict[str, Any]) -> None:
    if github_sync_enabled():
        pull_latest()

    if not should_process_slack_event(event):
        return

    changed = process_slack_event(event, auto_rewrite=False)
    if changed and github_sync_enabled():
        from blog_automation.paths import GENERATED_DIR, SOURCES_DIR

        commit_paths(
            "Sync Slack approval state from webhook",
            [GENERATED_DIR, SOURCES_DIR / "used_sources.json"],
        )


def parse_events_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Legacy helper for tests; production uses FastAPI background tasks in app.py."""
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    if payload.get("type") != "event_callback":
        return None

    event = payload.get("event") or {}
    process_slack_event_with_git_sync(event)
    return {"ok": True}
