"""Persist CI-generated drafts under generated/ for cloud Slack webhooks."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterator

from blog_automation.draft_approval import load_validation_report, save_validation_report
from blog_automation.paths import (
    GENERATED_APPROVED_DIR,
    GENERATED_DIR,
    GENERATED_RUNS_DIR,
    GENERATED_SLACK_INDEX_PATH,
    PROJECT_ROOT,
)
from blog_automation.write_common import (
    DEFAULT_OUTPUT_DIR,
    draft_stem_from_path,
    draft_subdirs,
    draft_validation_json_path,
    ensure_draft_subdirs,
    latest_markdown_draft,
)


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


def load_slack_index() -> dict[str, Any]:
    if not GENERATED_SLACK_INDEX_PATH.is_file():
        return {"messages": {}}
    with GENERATED_SLACK_INDEX_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {GENERATED_SLACK_INDEX_PATH}")
    data.setdefault("messages", {})
    return data


def save_slack_index(index: dict[str, Any]) -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    with GENERATED_SLACK_INDEX_PATH.open("w", encoding="utf-8") as handle:
        json.dump(index, handle, indent=2)
    print(f"[generated] Saved {GENERATED_SLACK_INDEX_PATH.relative_to(PROJECT_ROOT)}")


def slack_message_key(channel: str, message_ts: str) -> str:
    return f"{channel}:{message_ts}"


def register_slack_message(
    *,
    channel: str,
    message_ts: str,
    validation_path: Path,
    run_id: str,
) -> None:
    index = load_slack_index()
    messages = index.setdefault("messages", {})
    messages[slack_message_key(channel, message_ts)] = {
        "run_id": run_id,
        "validation_path": _relative(validation_path),
    }
    save_slack_index(index)


def lookup_validation_path(channel: str, message_ts: str) -> Path | None:
    index = load_slack_index()
    entry = index.get("messages", {}).get(slack_message_key(channel, message_ts))
    if not isinstance(entry, dict):
        return None
    rel = str(entry.get("validation_path") or "").strip()
    if not rel:
        return None
    path = PROJECT_ROOT / rel
    return path if path.is_file() else None


def iter_generated_validation_paths() -> Iterator[Path]:
    for root in (GENERATED_RUNS_DIR, GENERATED_APPROVED_DIR):
        if not root.is_dir():
            continue
        for json_dir in root.glob(f"**/drafts_json"):
            if json_dir.is_dir():
                yield from sorted(json_dir.glob("*-validation.json"))


def archive_draft_for_ci(
    draft_path: Path,
    *,
    run_id: str,
    channel: str,
    message_ts: str,
) -> Path:
    """Copy an output/drafts bundle into generated/runs/<run_id>/ and index it."""
    if not draft_path.is_absolute():
        draft_path = PROJECT_ROOT / draft_path
    stem = draft_stem_from_path(draft_path)
    src_root = DEFAULT_OUTPUT_DIR
    src_md_dir, src_pdf_dir, src_json_dir = draft_subdirs(src_root)
    dst_root = GENERATED_RUNS_DIR / run_id
    dst_md_dir, dst_pdf_dir, dst_json_dir = ensure_draft_subdirs(dst_root)

    src_md = src_md_dir / f"{stem}.md"
    src_pdf = src_pdf_dir / f"{stem}.pdf"
    src_json = src_json_dir / f"{stem}-validation.json"
    if not src_md.is_file() or not src_json.is_file():
        raise FileNotFoundError(f"Expected draft artifacts for {stem} under {src_root}")

    dst_md = dst_md_dir / f"{stem}.md"
    dst_pdf = dst_pdf_dir / f"{stem}.pdf"
    dst_json = dst_json_dir / f"{stem}-validation.json"

    shutil.copy2(src_md, dst_md)
    if src_pdf.is_file():
        shutil.copy2(src_pdf, dst_pdf)
    shutil.copy2(src_json, dst_json)

    report = load_validation_report(dst_json)
    report["draft_path"] = _relative(dst_md)
    report["pdf_path"] = _relative(dst_pdf) if dst_pdf.is_file() else None
    report["ci_run_id"] = run_id
    save_validation_report(dst_json, report)

    register_slack_message(
        channel=channel,
        message_ts=message_ts,
        validation_path=dst_json,
        run_id=run_id,
    )
    print(f"[generated] Archived draft run {run_id} at {dst_root.relative_to(PROJECT_ROOT)}")
    return dst_json


def archive_latest_draft_for_ci(*, run_id: str, channel: str, message_ts: str) -> Path:
    """Copy the latest output/drafts bundle into generated/runs/<run_id>/ and index it."""
    return archive_draft_for_ci(
        latest_markdown_draft(DEFAULT_OUTPUT_DIR),
        run_id=run_id,
        channel=channel,
        message_ts=message_ts,
    )
