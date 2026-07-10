"""Slack approval workflow for generated blog drafts (standalone module).

Default (post latest draft + listen):
    python -m blog_automation.pipeline.approve_listen

Subcommands:
    python -m blog_automation.pipeline.approve_listen post --latest
    python -m blog_automation.pipeline.approve_listen post --latest --then-listen
    python -m blog_automation.pipeline.approve_listen listen
    python -m blog_automation.pipeline.approve_listen clear-channel --dry-run
    python -m blog_automation.pipeline.approve_listen clear-channel --yes

While listening, type ``e`` + Enter to stop and return to the ``pipeline.py`` menu.
The bot posts a thread reply when the listener stops that way.

Posting uploads the existing PDF from `output/drafts/drafts_pdf/`. Run write first.

Slack reactions on the approval message:
- :white_check_mark: — approve (records sources; moves draft to approved)
- Reply `publish` (live) or `draft` (PSAI drafts) in thread when ✅ is still on the intro message
- :x: — request revisions (reply in thread with feedback)
- :repeat: — discard and rerun search → evaluate → write → post new draft

Required: SLACK_APPROVAL_BOT_TOKEN, SLACK_APPROVAL_CHANNEL, SLACK_APPROVAL_TOKEN (listen).
Optional PSAI: PSAI_API_KEY in `.env` / GitHub Secrets; `api_url` and `author` in `config/psai.<company>.json`.
"""

from __future__ import annotations

import blog_automation._pycache_prefix  # noqa: F401

from blog_automation.paths import PROJECT_ROOT

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from blog_automation.generated_store import archive_draft_for_ci
from blog_automation.pipeline.runner import (
    DEFAULT_REWRITE_MODULE_KEY,
    build_module_command,
    pipeline_subprocess_env,
    resolve_pipeline_log_path,
)
from blog_automation.pipeline.write_serverless import model_label, resolve_writing_model
from blog_automation.draft_approval import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_FEEDBACK_RECEIVED,
    APPROVAL_STATUS_NEEDS_FEEDBACK,
    APPROVAL_STATUS_PIPELINE_RESTART_IN_PROGRESS,
    APPROVAL_STATUS_REVISION_GENERATED,
    APPROVED_OUTPUT_DIR,
    append_feedback as append_validation_feedback,
    approval_status,
    clear_approval_status as clear_validation_approval_status,
    clear_rejection_status as clear_validation_rejection_status,
    draft_in_approved_storage,
    find_validation_for_slack_message,
    get_approval_block,
    iter_validation_json_paths,
    load_validation_report,
    mark_approval_status,
    new_approval_block,
    relocate_draft_artifacts,
    resolve_draft_path_from_report,
    save_validation_report,
    unapprove_destination_root,
)
from blog_automation.post import (
    PSAI_PUBLISH_REACTIONS,
    psai_already_published,
    psai_configured,
    parse_psai_post_command,
    psai_publish_command_prompt_text,
    psai_publish_success_text,
    publish_from_validation_path,
    undo_psai_publish_from_validation,
)
from blog_automation.used_sources import (
    record_used_sources_from_validation_report,
    remove_used_sources_from_validation_report,
)
from blog_automation.pipeline_costs import format_approval_summary_slack_lines
from blog_automation.write_common import (
    DEFAULT_OUTPUT_DIR,
    draft_pdf_path,
    draft_run_id_from_path,
    draft_validation_json_path,
    latest_markdown_draft,
    resolve_drafts_root,
    update_record_revision_mode,
)

APPROVE_RUNNER = "blog_automation.pipeline.approve_listen"

DEFAULT_DRAFT_DIR = DEFAULT_OUTPUT_DIR
REWRITE_MODULE_KEY = DEFAULT_REWRITE_MODULE_KEY
APPROVE_REACTIONS = {"white_check_mark", "heavy_check_mark"}
REJECT_REACTIONS = {"x", "negative_squared_cross_mark"}
RESTART_REACTIONS = {"repeat"}
SLACK_PIPELINE_LOG_FILENAME = "run.txt"
PIPELINE_RESTART_BLOCK_STATUSES = {
    APPROVAL_STATUS_PIPELINE_RESTART_IN_PROGRESS,
    APPROVAL_STATUS_APPROVED,
}
IGNORE_MESSAGE_SUBTYPES = {
    "bot_message",
    "message_changed",
    "message_deleted",
    "channel_join",
    "channel_leave",
    "file_share",
    "file_comment",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_slack_client():
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: slack_sdk. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    token = os.getenv("SLACK_APPROVAL_BOT_TOKEN")
    if not token:
        raise EnvironmentError("SLACK_APPROVAL_BOT_TOKEN is not set.")
    return WebClient(token=token)


def get_approval_channel() -> str:
    channel = os.getenv("SLACK_APPROVAL_CHANNEL")
    if not channel:
        raise EnvironmentError("SLACK_APPROVAL_CHANNEL is not set.")
    return channel


def latest_draft_path(draft_dir: Path = DEFAULT_DRAFT_DIR) -> Path:
    return latest_markdown_draft(draft_dir)


def prepare_new_approval_post() -> None:
    """No-op: approval state lives in each draft's validation JSON under output/drafts/drafts_json/."""


def title_from_markdown(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Blog draft"


def record_approved_draft_sources(report: dict[str, Any]) -> int:
    """Persist sources from an approved draft into used_sources.json."""
    draft_path = resolve_draft_path_from_report(report)
    if not draft_path or not draft_path.is_file():
        print(f"[approve] Warning: draft not found for used-source recording: {draft_path}")
        return 0

    recorded = record_used_sources_from_validation_report(
        report,
        draft_path=draft_path.relative_to(PROJECT_ROOT),
        runner=APPROVE_RUNNER,
    )
    if recorded:
        print(
            f"[approve] Recorded {len(recorded)} used source URL(s) in "
            "output/sources/used_sources.json"
        )
    return len(recorded)


def _send_psai_publish_prompt(
    client: Any,
    *,
    channel: str,
    thread_ts: str,
) -> None:
    if not psai_configured():
        return
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=psai_publish_command_prompt_text(),
    )


def user_has_reaction_on_message(
    client: Any,
    *,
    channel: str,
    message_ts: str,
    user_id: str,
    reaction_names: set[str] | frozenset[str],
) -> bool:
    try:
        response = client.reactions_get(channel=channel, timestamp=message_ts)
    except Exception as exc:
        print(f"[approve] Warning: reactions_get failed: {exc}")
        return False
    if not response.get("ok"):
        print(f"[approve] Warning: reactions_get not ok: {response}")
        return False
    message = response.get("message") or {}
    for reaction in message.get("reactions") or []:
        if reaction.get("name") not in reaction_names:
            continue
        if user_id in (reaction.get("users") or []):
            return True
    return False


def _handle_psai_post_command(
    client: Any,
    *,
    channel: str,
    thread_ts: str,
    user: str,
    validation_path: Path,
    report: dict[str, Any],
    psai_status: str,
) -> bool:
    status = approval_status(report)
    approval = get_approval_block(report)

    if status != APPROVAL_STATUS_APPROVED:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                "This draft is not approved yet. React with :white_check_mark: "
                "on the intro message above first."
            ),
        )
        return False

    approver = str(approval.get("approved_by") or "").strip()
    if approver and user != approver:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Only <@{approver}> can publish this draft.",
        )
        return False

    if not user_has_reaction_on_message(
        client,
        channel=channel,
        message_ts=thread_ts,
        user_id=user,
        reaction_names=APPROVE_REACTIONS,
    ):
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                "Your :white_check_mark: must still be on the intro message above. "
                "React :white_check_mark: to approve again, then reply `publish` or `draft`."
            ),
        )
        return False

    if psai_already_published(report):
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="This draft was already sent to PSAI.",
        )
        return False

    if not psai_configured():
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="Website publishing is not configured (set PSAI_API_KEY; see config/psai.<company>.json).",
        )
        return False

    try:
        response = publish_from_validation_path(
            validation_path,
            published_by=user,
            status=psai_status,
        )
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=psai_publish_success_text(response, status=psai_status),
        )
        print(f"[approve] Sent draft to PSAI as {psai_status} for {validation_path}")
        return True
    except Exception as exc:
        print(f"[approve] PSAI publish failed: {exc}")
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Website publish failed: {exc}",
        )
        return False


def _handle_website_publish_reaction(
    client: Any,
    *,
    channel: str,
    ts: str,
    validation_path: Path,
    report: dict[str, Any],
    user: str,
) -> None:
    if approval_status(report) != APPROVAL_STATUS_APPROVED:
        print("[approve] Ignoring website publish reaction; draft is not approved yet")
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=(
                "This draft is not approved in GitHub yet. React with :white_check_mark: first "
                "(on the intro message above), wait for the approval reply, then click "
                ":globe_with_meridians: again."
            ),
        )
        return

    if psai_already_published(report):
        print("[approve] Ignoring website publish reaction; post already recorded")
        return

    if not psai_configured():
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text="Website publishing is not configured (set PSAI_API_KEY; see config/psai.<company>.json).",
        )
        return

    try:
        response = publish_from_validation_path(
            validation_path,
            published_by=user,
            status="published",
        )
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=psai_publish_success_text(response, status="published"),
        )
        print(f"[approve] Published draft live via PSAI for {validation_path}")
    except Exception as exc:
        print(f"[approve] PSAI publish failed: {exc}")
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=f"Website publish failed: {exc}",
        )


def build_approval_intro(
    title: str,
    draft_path: Path,
    rewritten_from: str | None,
    *,
    recycled_from: str | None = None,
    validation_report: dict[str, Any] | None = None,
    model_display: str | None = None,
) -> str:
    run_id = draft_run_id_from_path(draft_path)
    intro = [
        f"*Blog draft ready for approval:* {title}",
        f"Draft: `{run_id}`",
        "",
    ]
    if recycled_from:
        prior = draft_run_id_from_path(recycled_from)
        intro.insert(1, f"Full pipeline restart from draft `{prior}`.")
    elif rewritten_from:
        prior = draft_run_id_from_path(rewritten_from)
        intro.insert(1, f"Revision of draft `{prior}`.")

    summary_lines = format_approval_summary_slack_lines(
        validation_report or {},
        model_display=model_display,
        rewritten_from=rewritten_from,
        recycled_from=recycled_from,
    )
    if summary_lines:
        intro.extend(summary_lines)
        intro.append("")

    intro.append(f"Attachments: PDF draft + `{SLACK_PIPELINE_LOG_FILENAME}` (search through write/tournament).")
    intro.append(
        "React to this message with :white_check_mark: to approve (remove yours to undo), "
        ":x: to request revisions, :repeat: to discard and rerun."
    )
    return "\n".join(intro)


def get_bot_user_id(client: Any) -> str | None:
    return get_bot_identity(client).get("user_id")


def get_bot_identity(client: Any) -> dict[str, str | None]:
    response = client.auth_test()
    if not response.get("ok"):
        print(f"[approve] Warning: auth_test failed: {response}")
        return {"user_id": None, "bot_id": None}
    return {
        "user_id": response.get("user_id"),
        "bot_id": response.get("bot_id") or response.get("user_id"),
    }


def message_is_from_bot(
    message: dict[str, Any],
    *,
    bot_user_id: str | None,
    bot_id: str | None,
) -> bool:
    if bot_user_id and message.get("user") == bot_user_id:
        return True
    if bot_id and message.get("bot_id") == bot_id:
        return True
    if message.get("subtype") == "bot_message" and bot_user_id and message.get("user") == bot_user_id:
        return True
    return False


def _message_preview(message: dict[str, Any]) -> str:
    text = str(message.get("text", "")).strip().replace("\n", " ")
    if len(text) > 80:
        return text[:77] + "..."
    return text or f"<{message.get('subtype', 'message')}>"


def collect_bot_messages_in_channel(
    client: Any,
    channel: str,
    *,
    bot_user_id: str | None,
    bot_id: str | None,
) -> list[dict[str, Any]]:
    """Return bot-authored messages in channel roots and threads (deduped by ts)."""
    seen_ts: set[str] = set()
    collected: list[dict[str, Any]] = []

    def add_message(message: dict[str, Any]) -> None:
        ts = str(message.get("ts", "")).strip()
        if not ts or ts in seen_ts:
            return
        if not message_is_from_bot(message, bot_user_id=bot_user_id, bot_id=bot_id):
            return
        seen_ts.add(ts)
        collected.append(message)

    cursor: str | None = None
    while True:
        response = client.conversations_history(channel=channel, cursor=cursor, limit=200)
        if not response.get("ok"):
            raise RuntimeError(f"conversations.history failed: {response}")

        for parent in response.get("messages", []):
            add_message(parent)
            reply_count = int(parent.get("reply_count") or 0)
            if reply_count <= 0:
                continue

            thread_ts = str(parent.get("ts", "")).strip()
            reply_cursor: str | None = None
            while True:
                replies_response = client.conversations_replies(
                    channel=channel,
                    ts=thread_ts,
                    cursor=reply_cursor,
                    limit=200,
                )
                if not replies_response.get("ok"):
                    raise RuntimeError(f"conversations.replies failed: {replies_response}")

                for reply in replies_response.get("messages", []):
                    add_message(reply)

                reply_cursor = replies_response.get("response_metadata", {}).get("next_cursor")
                if not reply_cursor:
                    break

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    collected.sort(key=lambda item: float(item.get("ts", "0")))
    return collected


def clear_bot_messages_from_channel(
    *,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """Delete all messages posted by this bot in SLACK_APPROVAL_CHANNEL."""
    client = get_slack_client()
    channel = get_approval_channel()
    identity = get_bot_identity(client)
    bot_user_id = identity.get("user_id")
    bot_id = identity.get("bot_id")

    messages = collect_bot_messages_in_channel(
        client,
        channel,
        bot_user_id=bot_user_id,
        bot_id=bot_id,
    )
    print(f"[approve] Found {len(messages)} bot message(s) in channel {channel}")

    if not messages:
        return 0

    for message in messages:
        print(f"  - ts={message.get('ts')} {_message_preview(message)}")

    if dry_run:
        print("[approve] Dry run only — no messages deleted.")
        return 0

    if not yes:
        answer = input(f"Delete {len(messages)} bot message(s) from {channel}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("[approve] Aborted.")
            return 0

    deleted = 0
    failed = 0
    for message in messages:
        ts = str(message.get("ts", "")).strip()
        response = client.chat_delete(channel=channel, ts=ts)
        if response.get("ok"):
            deleted += 1
        else:
            failed += 1
            print(f"[approve] Warning: could not delete {ts}: {response}")

    print(f"[approve] Deleted {deleted} message(s)" + (f"; {failed} failed" if failed else ""))
    return deleted


def approval_prompt_reaction_names() -> tuple[str, ...]:
    return ("white_check_mark", "x", "repeat")


def add_approval_prompt_reactions(client: Any, channel: str, message_ts: str) -> None:
    for name in approval_prompt_reaction_names():
        response = client.reactions_add(channel=channel, timestamp=message_ts, name=name)
        if response.get("ok"):
            print(f"[approve] Added :{name}: reaction prompt on message {message_ts}")
        else:
            print(f"[approve] Warning: Could not add :{name}: reaction: {response}")


def upload_draft_pdf(client: Any, channel: str, thread_ts: str, pdf_path: Path, title: str) -> bool:
    response = client.files_upload_v2(
        channel=channel,
        file=str(pdf_path),
        thread_ts=thread_ts,
        title=title,
        filename=pdf_path.name,
    )
    if response.get("ok"):
        print(f"[approve] Uploaded PDF {pdf_path.name} to thread {thread_ts}")
        return True

    print(f"[approve] Warning: PDF upload failed: {response}")
    return False


def upload_pipeline_log(client: Any, channel: str, thread_ts: str, log_path: Path) -> bool:
    response = client.files_upload_v2(
        channel=channel,
        file=str(log_path),
        thread_ts=thread_ts,
        title=SLACK_PIPELINE_LOG_FILENAME,
        filename=SLACK_PIPELINE_LOG_FILENAME,
    )
    if response.get("ok"):
        print(f"[approve] Uploaded pipeline log {log_path.name} to thread {thread_ts}")
        return True

    print(f"[approve] Warning: Pipeline log upload failed: {response}")
    return False


def infer_write_model_from_report(
    report: dict[str, Any],
    *,
    fallback: str | None = None,
) -> str | None:
    generation = report.get("generation")
    if isinstance(generation, dict):
        model_used = str(generation.get("model_used") or "").strip()
        if model_used:
            return model_used
    model = str(report.get("model") or "").strip()
    return model or fallback


_pipeline_restart_lock = threading.Lock()
_pipeline_restart_in_progress: set[str] = set()
_pipeline_restart_event = threading.Event()
_pending_pipeline_restart: dict[str, Any] | None = None


def infer_preferred_cluster_from_report(report: dict[str, Any]) -> str | None:
    """Prefer the strategy cluster from the draft being recycled."""
    from collections import Counter

    sources = report.get("sources_used") or []
    clusters = [
        str(item.get("strategy_cluster", "")).strip()
        for item in sources
        if isinstance(item, dict) and item.get("strategy_cluster")
    ]
    if not clusters:
        return None
    return Counter(clusters).most_common(1)[0][0]


def schedule_pipeline_restart(**kwargs: Any) -> None:
    """Queue a Slack :repeat: restart for the listen loop (runs on the main CLI thread)."""
    global _pending_pipeline_restart
    with _pipeline_restart_lock:
        _pending_pipeline_restart = kwargs
    _pipeline_restart_event.set()


def restart_pipeline_from_slack(
    *,
    validation_path: Path,
    report: dict[str, Any],
    channel: str,
    thread_ts: str,
    user: str,
    client: Any,
    rewrite_model: str | None,
    auto_rewrite: bool,
) -> None:
    """write_tournament (--with-search) → post new draft to Slack (live output in this terminal)."""
    restart_key = f"{channel}:{thread_ts}"
    try:
        write_model = infer_write_model_from_report(report, fallback=rewrite_model)
        if write_model:
            write_model = resolve_writing_model(write_model, interactive=False)

        approval = report.setdefault("approval", {})
        approval["status"] = APPROVAL_STATUS_PIPELINE_RESTART_IN_PROGRESS
        approval["pipeline_restart_requested_at"] = utc_now()
        approval["pipeline_restart_requested_by"] = user
        save_validation_report(validation_path, report)

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"<@{user}> requested a full pipeline restart "
                "(search → write tournament). Running now — this may take a few minutes."
            ),
        )

        restart_command = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "write_tournament.py"),
            "--with-search",
            "--clear-drafts",
        ]
        if write_model:
            restart_command.extend(["--model", write_model])
        print(f"[approve] Running: {' '.join(restart_command)}", flush=True)
        completed = subprocess.run(
            restart_command,
            cwd=PROJECT_ROOT,
            env=pipeline_subprocess_env(),
        )
        code = completed.returncode
        if code != 0:
            approval["status"] = "pipeline_restart_failed"
            approval["pipeline_restart_failed_at"] = utc_now()
            save_validation_report(validation_path, report)
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Pipeline restart failed. Check the terminal logs and try again.",
            )
            return

        approval["status"] = "recycled"
        approval["recycled_at"] = utc_now()
        approval["recycled_by"] = user
        save_validation_report(validation_path, report)

        new_draft_path = latest_draft_path()
        new_validation_path = post_draft(
            new_draft_path,
            recycled_from=str(validation_path.relative_to(PROJECT_ROOT)),
            rewrite_model=rewrite_model,
            auto_rewrite=auto_rewrite,
        )
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                "New draft posted from full pipeline restart: "
                f"`{new_validation_path.relative_to(PROJECT_ROOT)}`"
            ),
        )
    except Exception as exc:
        approval = report.setdefault("approval", {})
        approval["status"] = "pipeline_restart_failed"
        approval["pipeline_restart_failed_at"] = utc_now()
        approval["pipeline_restart_error"] = str(exc)
        save_validation_report(validation_path, report)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Pipeline restart failed: {exc}",
        )
        print(f"[approve] Pipeline restart failed: {exc}")
    finally:
        with _pipeline_restart_lock:
            _pipeline_restart_in_progress.discard(restart_key)


def post_draft(
    draft_path: Path,
    rewritten_from: str | None = None,
    recycled_from: str | None = None,
    *,
    rewrite_model: str | None = None,
    auto_rewrite: bool = True,
) -> Path:
    client = get_slack_client()
    channel = get_approval_channel()
    pdf_path = draft_pdf_path(draft_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(
            f"No PDF found for {draft_path.relative_to(PROJECT_ROOT)}. "
            f"Expected {pdf_path.relative_to(PROJECT_ROOT)}. "
            "Run write_serverless first."
        )

    markdown = draft_path.read_text(encoding="utf-8")
    title = title_from_markdown(markdown)
    relative_path = draft_path.relative_to(PROJECT_ROOT)
    pdf_relative_path = pdf_path.relative_to(PROJECT_ROOT)
    validation_path = draft_validation_json_path(draft_path)
    report = load_validation_report(validation_path)
    generation = report.get("generation") if isinstance(report.get("generation"), dict) else {}
    model_id = str(generation.get("model_used") or report.get("model") or "").strip()
    model_display = model_label(model_id) if model_id else None
    text = build_approval_intro(
        title,
        draft_path,
        rewritten_from,
        recycled_from=recycled_from,
        validation_report=report,
        model_display=model_display,
    )

    response = client.chat_postMessage(channel=channel, text=text)
    if not response.get("ok"):
        raise RuntimeError(f"Slack post failed: {response}")

    message_ts = response["ts"]
    if not upload_draft_pdf(client, channel, message_ts, pdf_path, title):
        raise RuntimeError(f"Slack PDF upload failed for {pdf_path.name}")

    log_path = resolve_pipeline_log_path(report)
    if log_path:
        if not upload_pipeline_log(client, channel, message_ts, log_path):
            print(f"[approve] Warning: Could not attach pipeline log from {log_path}")
    else:
        print("[approve] Warning: No pipeline run log found to attach")

    report["draft_path"] = str(relative_path)
    report["pdf_path"] = str(pdf_relative_path)
    if log_path:
        try:
            report["pipeline_log_path"] = str(log_path.relative_to(PROJECT_ROOT))
        except ValueError:
            report["pipeline_log_path"] = str(log_path)
    report["approval"] = new_approval_block(
        channel=channel,
        message_ts=message_ts,
        rewritten_from=rewritten_from,
        recycled_from=recycled_from,
    )
    save_validation_report(validation_path, report)
    add_approval_prompt_reactions(client, channel, message_ts)
    print(f"[approve] Posted draft PDF to Slack channel {channel} at {message_ts}")
    return validation_path


def request_feedback(client: Any, report: dict[str, Any]) -> None:
    approval = get_approval_block(report)
    client.chat_postMessage(
        channel=approval["channel"],
        thread_ts=approval["message_ts"],
        text=(
            "Got it. Please reply in this thread with the changes you want.\n"
            "Start with `edit:` for wording/structure fixes, or `sources:` if you need "
            "new stats/citations from the articles."
        ),
    )


def regenerate_from_feedback(
    validation_path: Path,
    report: dict[str, Any],
    *,
    rewrite_module_key: str = REWRITE_MODULE_KEY,
    rewrite_model: str | None = None,
) -> Path:
    from blog_automation.llm_client import get_llm_provider
    from blog_automation.llm_models import normalize_writing_model_for_provider, resolve_writing_model

    active_model = normalize_writing_model_for_provider(rewrite_model) if rewrite_model else None
    if active_model is None:
        inherited = infer_write_model_from_report(report)
        active_model = normalize_writing_model_for_provider(inherited) if inherited else resolve_writing_model()
    print(
        f"[approve] Slack rewrite: provider={get_llm_provider()}, "
        f"model={model_label(active_model)}"
    )

    rewrite_args: list[str] = ["--feedback-json", str(validation_path)]
    rewrite_args.extend(["--model", active_model])
    command = build_module_command(rewrite_module_key, *rewrite_args)

    print(f"[approve] Regenerating draft: {' '.join(command)}")
    completed = subprocess.run(command, cwd=PROJECT_ROOT, env=pipeline_subprocess_env())
    if completed.returncode != 0:
        raise RuntimeError(f"{rewrite_module_key} failed with exit code {completed.returncode}")

    new_draft_path = latest_draft_path()
    approval = report.setdefault("approval", {})
    approval["status"] = APPROVAL_STATUS_REVISION_GENERATED
    approval["revision_draft_path"] = str(new_draft_path.relative_to(PROJECT_ROOT))
    approval["revision_generated_at"] = utc_now()
    save_validation_report(validation_path, report)
    return new_draft_path


def handle_reaction(
    event: dict[str, Any],
    client: Any,
    bot_user_id: str | None = None,
    *,
    rewrite_model: str | None = None,
    auto_rewrite: bool = True,
) -> None:
    item = event.get("item", {})
    if item.get("type") != "message":
        return

    channel = item.get("channel")
    ts = item.get("ts")
    reaction = event.get("reaction")
    user = event.get("user", "unknown")
    if not channel or not ts or not reaction:
        return

    if bot_user_id and user == bot_user_id:
        return

    found = find_validation_for_slack_message(channel, ts)
    if not found:
        print(f"[approve] No draft validation found for Slack message {channel}:{ts}")
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=(
                "I could not match this message to a draft in GitHub. "
                "React on the *intro* post (with the pre-added emoji prompts), not the PDF attachment."
            ),
        )
        return

    validation_path, report = found
    status = approval_status(report)

    if reaction in APPROVE_REACTIONS:
        if status in {APPROVAL_STATUS_FEEDBACK_RECEIVED, APPROVAL_STATUS_REVISION_GENERATED}:
            print(f"[approve] Ignoring approval reaction; status is {status}")
            return
        previously_approved = status == APPROVAL_STATUS_APPROVED
        mark_approval_status(validation_path, report, APPROVAL_STATUS_APPROVED, user)
        print(f"[approve] Draft approved by {user}")
        if previously_approved:
            print(f"[approve] Updated approval record at {validation_path}")
        else:
            draft_path = resolve_draft_path_from_report(report)
            if draft_path and draft_path.is_file():
                relocated = relocate_draft_artifacts(draft_path)
                validation_path = draft_validation_json_path(relocated)
                report = load_validation_report(validation_path)
            record_approved_draft_sources(report)
            client.chat_postMessage(channel=channel, thread_ts=ts, text=f"Approved by <@{user}>.")
            _send_psai_publish_prompt(client, channel=channel, thread_ts=ts)
    elif reaction in PSAI_PUBLISH_REACTIONS:
        _handle_website_publish_reaction(
            client,
            channel=channel,
            ts=ts,
            validation_path=validation_path,
            report=report,
            user=user,
        )
    elif reaction in REJECT_REACTIONS:
        if status in {
            APPROVAL_STATUS_NEEDS_FEEDBACK,
            APPROVAL_STATUS_FEEDBACK_RECEIVED,
            APPROVAL_STATUS_REVISION_GENERATED,
        }:
            print(f"[approve] Ignoring revision reaction; status is {status}")
            return
        mark_approval_status(validation_path, report, APPROVAL_STATUS_NEEDS_FEEDBACK, user)
        print(f"[approve] Revision requested by {user}")
        request_feedback(client, report)
    elif reaction in RESTART_REACTIONS:
        if status in PIPELINE_RESTART_BLOCK_STATUSES:
            print(f"[approve] Ignoring repeat reaction; status is {status}")
            return
        restart_key = f"{channel}:{ts}"
        with _pipeline_restart_lock:
            if restart_key in _pipeline_restart_in_progress:
                print("[approve] Ignoring repeat reaction; pipeline restart already running")
                return
            _pipeline_restart_in_progress.add(restart_key)
        print(f"[approve] Full pipeline restart requested by {user}", flush=True)
        schedule_pipeline_restart(
            validation_path=validation_path,
            report=report,
            channel=channel,
            thread_ts=ts,
            user=user,
            client=client,
            rewrite_model=rewrite_model,
            auto_rewrite=auto_rewrite,
        )


def handle_reaction_removed(event: dict[str, Any], client: Any, bot_user_id: str | None = None) -> bool:
    item = event.get("item", {})
    if item.get("type") != "message":
        return False

    channel = item.get("channel")
    ts = item.get("ts")
    reaction = event.get("reaction")
    user = event.get("user", "unknown")
    if not channel or not ts or not reaction:
        return False

    if bot_user_id and user == bot_user_id:
        return False

    found = find_validation_for_slack_message(channel, ts)
    if not found:
        return False

    validation_path, report = found
    approval = get_approval_block(report)
    status = approval_status(report)

    if reaction in APPROVE_REACTIONS:
        if status != APPROVAL_STATUS_APPROVED:
            return False
        if approval.get("approved_by") != user:
            print(
                f"[approve] Ignoring approval removal from {user}; "
                f"approver is {approval.get('approved_by')}"
            )
            return False

        draft_path = resolve_draft_path_from_report(report)
        draft_ref = str(draft_path.relative_to(PROJECT_ROOT)) if draft_path else str(report.get("draft_path") or "")

        psai_note = undo_psai_publish_from_validation(validation_path, report)[1]
        report = load_validation_report(validation_path)
        removed_sources = remove_used_sources_from_validation_report(report, draft_path=draft_ref)
        if removed_sources:
            print(f"[approve] Removed {len(removed_sources)} used source URL(s) from registry")

        clear_validation_approval_status(validation_path, report)
        report = load_validation_report(validation_path)
        if draft_path and draft_path.is_file() and draft_in_approved_storage(draft_path):
            relocated = relocate_draft_artifacts(
                draft_path,
                destination_root=unapprove_destination_root(report),
            )
            validation_path = draft_validation_json_path(relocated)
            report = load_validation_report(validation_path)

        print(f"[approve] Approval removed by {user}; status reset to pending")
        reply_parts = [f"Approval removed by <@{user}>. Status reset to pending."]
        if psai_note:
            reply_parts.append(psai_note)
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=" ".join(reply_parts),
        )
        return True
    elif reaction in REJECT_REACTIONS:
        if status != APPROVAL_STATUS_NEEDS_FEEDBACK:
            return False
        if approval.get("rejected_by") != user:
            print(
                f"[approve] Ignoring revision removal from {user}; "
                f"rejector is {approval.get('rejected_by')}"
            )
            return False
        clear_validation_rejection_status(validation_path, report)
        print(f"[approve] Revision request removed by {user}; status reset to pending")
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=f"Revision request removed by <@{user}>. Status reset to pending.",
        )
        return True

    return False


def is_human_thread_reply(event: dict[str, Any], bot_user_id: str | None) -> bool:
    subtype = event.get("subtype")
    if subtype in IGNORE_MESSAGE_SUBTYPES:
        return False
    if event.get("bot_id") or event.get("app_id"):
        return False

    user = event.get("user")
    if not user:
        return False
    if bot_user_id and user == bot_user_id:
        return False

    return bool(str(event.get("text", "")).strip())


def handle_message(
    event: dict[str, Any],
    client: Any,
    auto_rewrite: bool,
    bot_user_id: str | None = None,
    *,
    rewrite_module_key: str = REWRITE_MODULE_KEY,
    rewrite_model: str | None = None,
) -> bool:
    if not is_human_thread_reply(event, bot_user_id):
        return False

    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    text = str(event.get("text", "")).strip()
    user = event.get("user", "unknown")
    if not channel or not thread_ts:
        return False

    found = find_validation_for_slack_message(channel, thread_ts)
    if not found:
        return False

    validation_path, report = found
    psai_status = parse_psai_post_command(text)
    if psai_status:
        return _handle_psai_post_command(
            client,
            channel=channel,
            thread_ts=thread_ts,
            user=user,
            validation_path=validation_path,
            report=report,
            psai_status=psai_status,
        )

    if approval_status(report) != APPROVAL_STATUS_NEEDS_FEEDBACK:
        return False

    append_validation_feedback(
        validation_path,
        report,
        user=user,
        text=text,
        event_ts=event.get("event_ts"),
    )
    update_record_revision_mode(report)
    approval = get_approval_block(report)
    mode = approval.get("revision_mode", "editorial")
    save_validation_report(validation_path, report)
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f"Feedback saved ({mode} revision).",
    )

    if auto_rewrite:
        new_draft_path = regenerate_from_feedback(
            validation_path,
            report,
            rewrite_module_key=rewrite_module_key,
            rewrite_model=rewrite_model,
        )
        new_validation_path = post_draft(
            new_draft_path,
            rewritten_from=str(validation_path.relative_to(PROJECT_ROOT)),
        )
        new_report = load_validation_report(new_validation_path)
        new_approval = get_approval_block(new_report)
        new_message_ts = str(new_approval.get("message_ts") or "").strip()
        new_channel = str(new_approval.get("channel") or channel).strip()
        if new_channel and new_message_ts:
            archive_draft_for_ci(
                new_draft_path,
                run_id=f"revision-{draft_run_id_from_path(new_draft_path)}",
                channel=new_channel,
                message_ts=new_message_ts,
            )
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                "Revision generated and posted for approval: "
                f"`{new_validation_path.relative_to(PROJECT_ROOT)}`"
            ),
        )

    return True


LISTEN_EXIT_COMMAND = "e"
LISTEN_EXIT_HINT = (
    f"[approve] Type {LISTEN_EXIT_COMMAND!r} + Enter to stop listening and return to the pipeline menu."
)
LISTENER_STOPPED_SLACK_TEXT = (
    "Approval listener stopped in the terminal (`e` + Enter). "
    "Reactions and feedback will not be processed until the listener is running again."
)


def slack_thread_from_report(report: dict[str, Any]) -> tuple[str, str] | None:
    approval = get_approval_block(report)
    channel = str(approval.get("channel") or "").strip()
    thread_ts = str(approval.get("message_ts") or "").strip()
    if channel and thread_ts:
        return channel, thread_ts
    return None


def slack_thread_from_validation_path(validation_path: Path) -> tuple[str, str] | None:
    return slack_thread_from_report(load_validation_report(validation_path))


def find_latest_listen_slack_thread() -> tuple[str, str] | None:
    """Return the most recently posted approval message (by Slack message ts)."""
    latest: tuple[str, str] | None = None
    latest_score = 0.0
    for validation_path in iter_validation_json_paths(DEFAULT_OUTPUT_DIR, APPROVED_OUTPUT_DIR):
        thread = slack_thread_from_validation_path(validation_path)
        if not thread:
            continue
        try:
            score = float(thread[1])
        except ValueError:
            continue
        if score >= latest_score:
            latest_score = score
            latest = thread
    return latest


def notify_listener_stopped(client: Any, channel: str, thread_ts: str) -> None:
    response = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=LISTENER_STOPPED_SLACK_TEXT,
    )
    if not response.get("ok"):
        print(f"[approve] Warning: Could not post listener-stopped message: {response}")


def _watch_stdin_for_exit(
    stop_event: threading.Event,
    user_typed_exit: threading.Event,
) -> None:
    """Background thread: exit listen loop when the user types ``e`` (+ Enter)."""
    try:
        for line in sys.stdin:
            if line.strip().lower() == LISTEN_EXIT_COMMAND:
                user_typed_exit.set()
                stop_event.set()
                return
    except (EOFError, KeyboardInterrupt):
        stop_event.set()


def _disconnect_socket_client(socket_client: Any) -> None:
    for method_name in ("disconnect", "close"):
        method = getattr(socket_client, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
            return


def listen(
    auto_rewrite: bool,
    *,
    rewrite_module_key: str = REWRITE_MODULE_KEY,
    rewrite_model: str | None = None,
    slack_thread: tuple[str, str] | None = None,
) -> bool:
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError as exc:
        raise RuntimeError(
            "Missing Socket Mode dependency from slack_sdk. Install with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    app_token = os.getenv("SLACK_APPROVAL_TOKEN")
    if not app_token:
        raise EnvironmentError("SLACK_APPROVAL_TOKEN is not set.")

    web_client = get_slack_client()
    bot_user_id = get_bot_user_id(web_client)
    socket_client = SocketModeClient(app_token=app_token, web_client=web_client)

    def process(client: Any, request: Any) -> None:
        if request.type != "events_api":
            return

        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        event = request.payload.get("event", {})
        event_type = event.get("type")
        if event_type == "reaction_added":
            handle_reaction(
                event,
                web_client,
                bot_user_id=bot_user_id,
                rewrite_model=rewrite_model,
                auto_rewrite=auto_rewrite,
            )
        elif event_type == "reaction_removed":
            handle_reaction_removed(event, web_client, bot_user_id=bot_user_id)
        elif event_type == "message":
            handle_message(
                event,
                web_client,
                auto_rewrite=auto_rewrite,
                bot_user_id=bot_user_id,
                rewrite_module_key=rewrite_module_key,
                rewrite_model=rewrite_model,
            )

    socket_client.socket_mode_request_listeners.append(process)
    print("[approve] Listening for Slack approval reactions and feedback...")
    if sys.stdin.isatty():
        print(LISTEN_EXIT_HINT)
    socket_client.connect()

    stop_event = threading.Event()
    user_typed_exit = threading.Event()
    watcher: threading.Thread | None = None
    if sys.stdin.isatty():
        watcher = threading.Thread(
            target=_watch_stdin_for_exit,
            args=(stop_event, user_typed_exit),
            daemon=True,
        )
        watcher.start()

    try:
        while not stop_event.is_set():
            if not _pipeline_restart_event.wait(timeout=0.25):
                continue
            _pipeline_restart_event.clear()
            with _pipeline_restart_lock:
                pending = _pending_pipeline_restart
                _pending_pipeline_restart = None
            if pending:
                print(
                    "\n[approve] Slack :repeat: detected — running pipeline in this terminal "
                    "(search → evaluate → write). Output below is live.",
                    flush=True,
                )
                restart_pipeline_from_slack(**pending)
    finally:
        _disconnect_socket_client(socket_client)

    user_exit = stop_event.is_set()
    if user_typed_exit.is_set() and sys.stdin.isatty():
        thread = slack_thread or find_latest_listen_slack_thread()
        if thread:
            try:
                notify_listener_stopped(web_client, thread[0], thread[1])
                print("[approve] Posted listener-stopped notice to Slack thread.")
            except Exception as exc:
                print(f"[approve] Warning: Could not post listener-stopped message: {exc}")
    if user_exit and sys.stdin.isatty():
        print("[approve] Stopped listening (returning to pipeline menu).")
    else:
        print("[approve] Stopped listening.")
    return user_exit


def _resolve_rewrite_model_arg(model: str | None) -> str | None:
    if not model:
        return None
    return resolve_writing_model(model)


def run_approve_post_and_listen(
    *,
    auto_rewrite: bool = True,
    rewrite_model: str | None = None,
    interactive_model_prompt: bool = True,
) -> bool:
    """Post the latest draft, then listen until the user types ``e`` (+ Enter) in the CLI."""
    if interactive_model_prompt:
        rewrite_model = resolve_writing_model(rewrite_model)
    if rewrite_model:
        print(f"[approve_listen] Rewrite model: {model_label(rewrite_model)}")

    draft_path = latest_draft_path()
    print(f"[approve_listen] Using latest draft: {draft_path.relative_to(PROJECT_ROOT)}")
    prepare_new_approval_post()
    validation_path = post_draft(
        draft_path,
        rewrite_model=rewrite_model,
        auto_rewrite=auto_rewrite,
    )
    return listen(
        auto_rewrite=auto_rewrite,
        rewrite_model=rewrite_model,
        slack_thread=slack_thread_from_validation_path(validation_path),
    )


def _add_listen_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        help="LLM model ID or write_serverless menu number for Slack auto-rewrites (provider default if omitted).",
    )
    parser.add_argument(
        "--no-auto-rewrite",
        action="store_true",
        help="Save thread feedback without rerunning write_serverless.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Slack approval: post blog drafts and listen for reactions/feedback.",
    )
    subparsers = parser.add_subparsers(dest="command")

    post_parser = subparsers.add_parser("post", help="Post a draft to Slack for approval.")
    post_parser.add_argument("draft", nargs="?", type=Path, help="Draft Markdown path to post.")
    post_parser.add_argument("--latest", action="store_true", help="Post the latest Markdown draft.")
    post_parser.add_argument(
        "--then-listen",
        action="store_true",
        help="After posting, start the Socket Mode listener.",
    )
    _add_listen_flags(post_parser)

    listen_parser = subparsers.add_parser("listen", help="Listen for Slack reactions and feedback.")
    _add_listen_flags(listen_parser)

    clear_parser = subparsers.add_parser(
        "clear-channel",
        help="Delete all messages posted by this bot in SLACK_APPROVAL_CHANNEL.",
    )
    clear_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List bot messages that would be deleted without deleting them.",
    )
    clear_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )

    _add_listen_flags(parser)

    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env")

    auto_rewrite = not args.no_auto_rewrite
    rewrite_model = _resolve_rewrite_model_arg(args.model)

    if args.command is None:
        run_approve_post_and_listen(
            auto_rewrite=auto_rewrite,
            rewrite_model=rewrite_model,
            interactive_model_prompt=not args.model,
        )
        return

    if args.command == "post":
        prepare_new_approval_post()
        if args.latest:
            draft_path = latest_draft_path()
        elif args.draft:
            draft_path = args.draft
            if not draft_path.is_absolute():
                draft_path = PROJECT_ROOT / draft_path
        else:
            raise SystemExit("Provide a draft path or use --latest.")

        listen_rewrite_model = rewrite_model
        if args.then_listen and not listen_rewrite_model:
            listen_rewrite_model = resolve_writing_model(None)
        validation_path = post_draft(
            draft_path,
            rewrite_model=listen_rewrite_model if args.then_listen else rewrite_model,
            auto_rewrite=auto_rewrite,
        )
        if args.then_listen:
            listen(
                auto_rewrite=auto_rewrite,
                rewrite_model=listen_rewrite_model,
                slack_thread=slack_thread_from_validation_path(validation_path),
            )
    elif args.command == "listen":
        if not args.model:
            rewrite_model = resolve_writing_model(None)
        listen(auto_rewrite=auto_rewrite, rewrite_model=rewrite_model)
    elif args.command == "clear-channel":
        clear_bot_messages_from_channel(dry_run=args.dry_run, yes=args.yes)


if __name__ == "__main__":
    main()
