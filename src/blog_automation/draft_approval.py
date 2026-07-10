"""Slack approval metadata stored inside draft validation JSON files."""

from __future__ import annotations

from blog_automation.paths import GENERATED_APPROVED_DIR, GENERATED_RUNS_DIR, OUTPUT_DIR, PROJECT_ROOT
from blog_automation.write_common import (
    DRAFTS_JSON_DIRNAME,
    DRAFTS_MD_DIRNAME,
    DRAFTS_PDF_DIRNAME,
    DEFAULT_OUTPUT_DIR,
    draft_stem_from_path,
    draft_subdirs,
    draft_validation_json_path,
    ensure_draft_subdirs,
    resolve_drafts_root,
)

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


APPROVED_OUTPUT_DIR = OUTPUT_DIR / "approved"
LEGACY_APPROVALS_DIR = OUTPUT_DIR / "approvals"

APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_NEEDS_FEEDBACK = "needs_feedback"
APPROVAL_STATUS_FEEDBACK_RECEIVED = "feedback_received"
APPROVAL_STATUS_REVISION_GENERATED = "revision_generated"
APPROVAL_STATUS_PIPELINE_RESTART_IN_PROGRESS = "pipeline_restart_in_progress"
APPROVAL_STATUS_PIPELINE_RESTART_FAILED = "pipeline_restart_failed"
APPROVAL_STATUS_RECYCLED = "recycled"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_approval_block(
    *,
    channel: str,
    message_ts: str,
    rewritten_from: str | None = None,
    recycled_from: str | None = None,
) -> dict[str, Any]:
    return {
        "post_format": "pdf",
        "channel": channel,
        "message_ts": message_ts,
        "status": APPROVAL_STATUS_PENDING,
        "posted_at": utc_now(),
        "approved_at": None,
        "approved_by": None,
        "rejected_at": None,
        "rejected_by": None,
        "feedback_requested_at": None,
        "rewritten_from": rewritten_from,
        "recycled_from": recycled_from,
        "revision_draft_path": None,
        "revision_generated_at": None,
        "pipeline_restart_requested_at": None,
        "pipeline_restart_requested_by": None,
        "pipeline_restart_failed_at": None,
        "pipeline_restart_error": None,
        "recycled_at": None,
        "recycled_by": None,
        "revision_mode": "",
        "revision_mode_reason": "",
        "feedback": [],
    }


def load_validation_report(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def save_validation_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(f"[approve] Saved {path}")


def get_approval_block(report: dict[str, Any]) -> dict[str, Any]:
    approval = report.get("approval")
    return approval if isinstance(approval, dict) else {}


def approval_status(report: dict[str, Any]) -> str:
    return str(get_approval_block(report).get("status") or APPROVAL_STATUS_PENDING)


def feedback_items(report: dict[str, Any]) -> list[Any]:
    feedback = get_approval_block(report).get("feedback", [])
    if isinstance(feedback, list):
        return feedback
    return []


def resolve_draft_path_from_report(report: dict[str, Any]) -> Path | None:
    draft_ref = str(report.get("draft_path") or "").strip()
    if not draft_ref:
        return None
    draft_path = Path(draft_ref)
    if not draft_path.is_absolute():
        draft_path = PROJECT_ROOT / draft_path
    return draft_path


def resolve_replace_draft_path_from_report(report: dict[str, Any]) -> Path | None:
    approval = get_approval_block(report)
    for key in ("revision_draft_path",):
        draft_rel = str(approval.get(key) or "").strip()
        if not draft_rel:
            continue
        draft_path = Path(draft_rel)
        if not draft_path.is_absolute():
            draft_path = PROJECT_ROOT / draft_path
        if draft_path.is_file():
            return draft_path
    return resolve_draft_path_from_report(report)


def iter_validation_json_paths(*roots: Path) -> Iterator[Path]:
    for root in roots:
        _, _, json_dir = draft_subdirs(root)
        if not json_dir.exists():
            continue
        yield from sorted(json_dir.glob("*-validation.json"))


def find_validation_for_slack_message(channel: str, ts: str) -> tuple[Path, dict[str, Any]] | None:
    from blog_automation.generated_store import lookup_validation_path

    indexed = lookup_validation_path(channel, ts)
    if indexed is not None:
        return indexed, load_validation_report(indexed)

    for validation_path in iter_validation_json_paths(
        DEFAULT_OUTPUT_DIR,
        APPROVED_OUTPUT_DIR,
    ):
        report = load_validation_report(validation_path)
        approval = get_approval_block(report)
        if approval.get("channel") == channel and approval.get("message_ts") == ts:
            return validation_path, report

    from blog_automation.generated_store import iter_generated_validation_paths

    for validation_path in iter_generated_validation_paths():
        report = load_validation_report(validation_path)
        approval = get_approval_block(report)
        if approval.get("channel") == channel and approval.get("message_ts") == ts:
            return validation_path, report

    return _find_legacy_approval_for_message(channel, ts)


def _find_legacy_approval_for_message(channel: str, ts: str) -> tuple[Path, dict[str, Any]] | None:
    if not LEGACY_APPROVALS_DIR.is_dir():
        return None
    for legacy_path in LEGACY_APPROVALS_DIR.glob("*.json"):
        legacy = load_validation_report(legacy_path)
        if legacy.get("channel") != channel or legacy.get("message_ts") != ts:
            continue
        migrated = migrate_legacy_approval_json(legacy_path, legacy)
        if migrated:
            return migrated
    return None


def migrate_legacy_approval_json(
    legacy_path: Path,
    legacy_record: dict[str, Any],
) -> tuple[Path, dict[str, Any]] | None:
    """Copy legacy output/approvals/*.json fields into the draft validation JSON."""
    draft_ref = legacy_record.get("draft_path")
    if not draft_ref:
        return None
    draft_path = Path(str(draft_ref))
    if not draft_path.is_absolute():
        draft_path = PROJECT_ROOT / draft_path

    validation_path = draft_validation_json_path(draft_path)
    if not validation_path.is_file():
        return None

    report = load_validation_report(validation_path)
    approval = new_approval_block(
        channel=str(legacy_record.get("channel") or ""),
        message_ts=str(legacy_record.get("message_ts") or ""),
        rewritten_from=legacy_record.get("rewritten_from"),
        recycled_from=legacy_record.get("recycled_from"),
    )
    for key, value in legacy_record.items():
        if key in {"draft_path", "pdf_path"}:
            continue
        approval[key] = value
    report["approval"] = approval
    if legacy_record.get("pdf_path"):
        report["pdf_path"] = legacy_record["pdf_path"]
    save_validation_report(validation_path, report)

    if approval_status(report) == APPROVAL_STATUS_APPROVED and resolve_drafts_root(draft_path) == DEFAULT_OUTPUT_DIR:
        relocated = relocate_draft_artifacts(draft_path, destination_root=APPROVED_OUTPUT_DIR)
        validation_path = draft_validation_json_path(relocated)
        report = load_validation_report(validation_path)

    legacy_path.unlink(missing_ok=True)
    print(f"[approve] Migrated legacy approval record into {validation_path}")
    return validation_path, report


def _relative_project_path(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


def approval_destination_root(draft_path: Path) -> Path:
    """Use generated/approved for CI drafts archived under generated/runs/."""
    try:
        draft_path.resolve().relative_to(GENERATED_RUNS_DIR.resolve())
        return GENERATED_APPROVED_DIR
    except ValueError:
        return APPROVED_OUTPUT_DIR


def draft_in_approved_storage(draft_path: Path) -> bool:
    return resolve_drafts_root(draft_path) in {APPROVED_OUTPUT_DIR, GENERATED_APPROVED_DIR}


def unapprove_destination_root(report: dict[str, Any]) -> Path:
    """Move un-approved CI drafts back under generated/runs/<run_id>/."""
    run_id = str(report.get("ci_run_id") or "").strip()
    if run_id:
        destination = GENERATED_RUNS_DIR / run_id
        ensure_draft_subdirs(destination)
        return destination
    return DEFAULT_OUTPUT_DIR


def relocate_draft_artifacts(draft_path: Path, *, destination_root: Path | None = None) -> Path:
    """Move one draft's md/pdf/validation JSON to another output root; return new md path."""
    if destination_root is None:
        destination_root = approval_destination_root(draft_path)
    stem = draft_stem_from_path(draft_path)
    source_root = resolve_drafts_root(draft_path)
    src_md_dir, src_pdf_dir, src_json_dir = draft_subdirs(source_root)
    dst_md_dir, dst_pdf_dir, dst_json_dir = ensure_draft_subdirs(destination_root)

    src_md = src_md_dir / f"{stem}.md"
    src_pdf = src_pdf_dir / f"{stem}.pdf"
    src_json = src_json_dir / f"{stem}-validation.json"
    dst_md = dst_md_dir / f"{stem}.md"
    dst_pdf = dst_pdf_dir / f"{stem}.pdf"
    dst_json = dst_json_dir / f"{stem}-validation.json"

    if not src_md.is_file():
        raise FileNotFoundError(f"Draft Markdown not found: {src_md}")
    if not src_json.is_file():
        raise FileNotFoundError(f"Draft validation JSON not found: {src_json}")

    report = load_validation_report(src_json)
    shutil.move(src_md, dst_md)
    if src_pdf.is_file():
        shutil.move(src_pdf, dst_pdf)
    report["draft_path"] = _relative_project_path(dst_md)
    report["pdf_path"] = _relative_project_path(dst_pdf) if dst_pdf.is_file() else None
    if src_json.resolve() != dst_json.resolve():
        shutil.move(src_json, dst_json)
    save_validation_report(dst_json, report)
    return dst_md


def mark_approval_status(
    validation_path: Path,
    report: dict[str, Any],
    status: str,
    user: str,
) -> None:
    approval = report.setdefault("approval", {})
    approval["status"] = status
    if status == APPROVAL_STATUS_APPROVED:
        approval["approved_at"] = utc_now()
        approval["approved_by"] = user
        approval["rejected_at"] = None
        approval["rejected_by"] = None
        approval["feedback_requested_at"] = None
    elif status == APPROVAL_STATUS_NEEDS_FEEDBACK:
        approval["rejected_at"] = utc_now()
        approval["rejected_by"] = user
        approval["feedback_requested_at"] = utc_now()
        approval["approved_at"] = None
        approval["approved_by"] = None
    save_validation_report(validation_path, report)


def clear_approval_status(validation_path: Path, report: dict[str, Any]) -> None:
    approval = report.setdefault("approval", {})
    approval["status"] = APPROVAL_STATUS_PENDING
    approval["approved_at"] = None
    approval["approved_by"] = None
    save_validation_report(validation_path, report)


def clear_rejection_status(validation_path: Path, report: dict[str, Any]) -> None:
    approval = report.setdefault("approval", {})
    approval["status"] = APPROVAL_STATUS_PENDING
    approval["rejected_at"] = None
    approval["rejected_by"] = None
    approval["feedback_requested_at"] = None
    save_validation_report(validation_path, report)


def append_feedback(
    validation_path: Path,
    report: dict[str, Any],
    *,
    user: str,
    text: str,
    event_ts: str | None,
) -> None:
    approval = report.setdefault("approval", {})
    feedback = approval.setdefault("feedback", [])
    if not isinstance(feedback, list):
        feedback = []
        approval["feedback"] = feedback
    feedback.append(
        {
            "user": user,
            "text": text.strip(),
            "event_ts": event_ts,
            "created_at": utc_now(),
        }
    )
    approval["status"] = APPROVAL_STATUS_FEEDBACK_RECEIVED
    save_validation_report(validation_path, report)
