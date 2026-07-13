"""Track scheduled weekly draft runs (Mon primary, Wed conditional retry)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blog_automation.paths import GENERATED_DIR, GENERATED_WEEKLY_PIPELINE_PATH, OUTPUT_DIR, PROJECT_ROOT
from blog_automation.company import get_profile

SCHEDULED_DAYS = frozenset({"monday", "wednesday"})
PIPELINE_FAILURE_REASON_PATH = OUTPUT_DIR / "pipeline_failure_reason.json"
FAILURE_REASON_ANTHROPIC_CREDITS = "anthropic_credits"
_ANTHROPIC_CREDIT_LOG_MARKERS = (
    "credit balance is too low",
    "llmcreditsexhausted",
    "anthropic api credit balance is too low",
    "purchase credits",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_iso_week(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def load_weekly_state() -> dict[str, Any]:
    if not GENERATED_WEEKLY_PIPELINE_PATH.is_file():
        return {}
    with GENERATED_WEEKLY_PIPELINE_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {GENERATED_WEEKLY_PIPELINE_PATH}")
    return data


def save_weekly_state(state: dict[str, Any]) -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    with GENERATED_WEEKLY_PIPELINE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    print(
        f"[weekly] Saved {GENERATED_WEEKLY_PIPELINE_PATH.relative_to(PROJECT_ROOT)}"
    )


def reset_week_state_if_new_week(state: dict[str, Any]) -> dict[str, Any]:
    iso_week = current_iso_week()
    if state.get("iso_week") == iso_week:
        return state
    return {"iso_week": iso_week}


def draft_posted_this_week(state: dict[str, Any] | None = None) -> bool:
    state = reset_week_state_if_new_week(state or load_weekly_state())
    return bool(str(state.get("draft_run_id") or "").strip())


def clear_pipeline_failure_reason() -> None:
    if PIPELINE_FAILURE_REASON_PATH.is_file():
        PIPELINE_FAILURE_REASON_PATH.unlink()


def record_pipeline_failure_reason(code: str, message: str = "") -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "code": code,
        "message": message,
        "recorded_at": utc_now(),
    }
    with PIPELINE_FAILURE_REASON_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    print(
        f"[weekly] Recorded failure reason {code!r} → "
        f"{PIPELINE_FAILURE_REASON_PATH.relative_to(PROJECT_ROOT)}"
    )


def load_pipeline_failure_reason() -> dict[str, Any] | None:
    if not PIPELINE_FAILURE_REASON_PATH.is_file():
        return None
    with PIPELINE_FAILURE_REASON_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else None


def _log_mentions_anthropic_credits(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _ANTHROPIC_CREDIT_LOG_MARKERS)


def detect_anthropic_credits_failure_from_logs() -> bool:
    """Fallback detector when a marker file was not written (e.g. mid-run billing error)."""
    candidates: list[Path] = []
    pipeline_log = OUTPUT_DIR / "drafts" / "pipeline_run.log"
    if pipeline_log.is_file():
        candidates.append(pipeline_log)
    multi_run = OUTPUT_DIR / "multi_run"
    if multi_run.is_dir():
        candidates.extend(sorted(multi_run.glob("*/run.log"), reverse=True)[:3])
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _log_mentions_anthropic_credits(text):
            return True
    return False


def resolve_pipeline_failure_reason() -> str | None:
    recorded = load_pipeline_failure_reason()
    if recorded and str(recorded.get("code") or "").strip():
        return str(recorded["code"]).strip()
    if detect_anthropic_credits_failure_from_logs():
        return FAILURE_REASON_ANTHROPIC_CREDITS
    return None


def should_notify_no_draft(state: dict[str, Any] | None = None) -> bool:
    state = reset_week_state_if_new_week(state or load_weekly_state())
    if draft_posted_this_week(state):
        return False
    if state.get("no_draft_notified_at"):
        return False
    return True


def should_notify_monday_failure(state: dict[str, Any] | None = None) -> bool:
    state = reset_week_state_if_new_week(state or load_weekly_state())
    if draft_posted_this_week(state):
        return False
    if state.get("monday_failure_notified_at"):
        return False
    return True


def should_notify_credits_failure(state: dict[str, Any] | None = None) -> bool:
    """Credit alerts are separate from quiet-news alerts (and work for manual runs)."""
    state = reset_week_state_if_new_week(state or load_weekly_state())
    if state.get("credits_failure_notified_at"):
        return False
    return True


def credits_failure_slack_text(*, iso_week: str) -> str:
    return (
        f"No blog draft for week {iso_week} ({get_profile().COMPANY_SHORT}) — "
        "Anthropic API credits are exhausted. "
        "Please refill credits at https://console.anthropic.com/settings/billing, "
        "then re-run the Weekly Blog Pipeline."
    )


def manual_failure_slack_text(*, iso_week: str, reason: str | None = None) -> str:
    if reason == FAILURE_REASON_ANTHROPIC_CREDITS:
        return credits_failure_slack_text(iso_week=iso_week)
    return (
        f"No blog draft for week {iso_week} ({get_profile().COMPANY_SHORT}) — "
        f"search found no qualifying {get_profile().METRO_AREA} roofing stories "
        "(manual run). Re-run the Weekly Blog Pipeline after conditions improve, "
        "or wait for the next scheduled Monday/Wednesday attempt."
    )


def monday_failure_slack_text(*, iso_week: str, reason: str | None = None) -> str:
    if reason == FAILURE_REASON_ANTHROPIC_CREDITS:
        return credits_failure_slack_text(iso_week=iso_week) + (
            " Wednesday auto-retry will also fail until credits are restored."
        )
    return (
        f"No blog draft for week {iso_week} — search found no qualifying "
        f"{get_profile().METRO_AREA} roofing stories. Will retry Wednesday at 8:00 AM ET "
        "with widened search."
    )


def no_draft_slack_text(*, iso_week: str, reason: str | None = None) -> str:
    if reason == FAILURE_REASON_ANTHROPIC_CREDITS:
        return credits_failure_slack_text(iso_week=iso_week) + " Next scheduled run: Monday."
    return (
        f"No blog draft for week {iso_week} — search found no qualifying "
        f"{get_profile().METRO_AREA} roofing stories after the Wednesday retry. "
        "Next scheduled run: Monday."
    )


@dataclass(frozen=True)
class WeeklyRunPlan:
    run_pipeline: bool
    widen_search: bool
    scheduled_day: str
    notify_on_final_failure: bool
    skip_reason: str = ""

    def github_output_lines(self) -> list[str]:
        return [
            f"run_pipeline={'true' if self.run_pipeline else 'false'}",
            f"widen_search={'true' if self.widen_search else 'false'}",
            f"scheduled_day={self.scheduled_day}",
            (
                "notify_on_final_failure="
                f"{'true' if self.notify_on_final_failure else 'false'}"
            ),
            f"skip_reason={self.skip_reason.replace(chr(10), ' ').strip()}",
        ]


def plan_manual_dispatch(*, widen_search: bool) -> WeeklyRunPlan:
    return WeeklyRunPlan(
        run_pipeline=True,
        widen_search=widen_search,
        scheduled_day="manual",
        notify_on_final_failure=False,
    )


def plan_scheduled_run(*, utc_weekday: int) -> WeeklyRunPlan:
    """Plan a cron run. ``utc_weekday``: Monday=0, Wednesday=2 (``datetime.weekday()``)."""
    if utc_weekday == 0:
        state = reset_week_state_if_new_week(load_weekly_state())
        if state.get("monday"):
            return WeeklyRunPlan(
                run_pipeline=False,
                widen_search=False,
                scheduled_day="monday",
                notify_on_final_failure=False,
                skip_reason="Monday run already attempted this ISO week (noon retry no-op)",
            )
        return WeeklyRunPlan(
            run_pipeline=True,
            widen_search=False,
            scheduled_day="monday",
            notify_on_final_failure=False,
        )
    if utc_weekday == 2:
        state = reset_week_state_if_new_week(load_weekly_state())
        if draft_posted_this_week(state):
            return WeeklyRunPlan(
                run_pipeline=False,
                widen_search=False,
                scheduled_day="wednesday",
                notify_on_final_failure=False,
                skip_reason="Draft already archived this ISO week",
            )
        return WeeklyRunPlan(
            run_pipeline=True,
            widen_search=True,
            scheduled_day="wednesday",
            notify_on_final_failure=True,
        )
    return WeeklyRunPlan(
        run_pipeline=False,
        widen_search=False,
        scheduled_day="unknown",
        notify_on_final_failure=False,
        skip_reason=f"Unexpected UTC weekday {utc_weekday}",
    )


def record_pipeline_attempt(
    *,
    day: str,
    run_id: str,
    status: str,
    draft_run_id: str | None = None,
) -> dict[str, Any]:
    if day not in SCHEDULED_DAYS:
        raise ValueError(f"Refusing to record weekly state for non-scheduled day: {day}")

    state = reset_week_state_if_new_week(load_weekly_state())
    finished_at = utc_now()
    state[day] = {
        "run_id": run_id,
        "status": status,
        "finished_at": finished_at,
    }
    if status == "success" and draft_run_id:
        state["draft_run_id"] = draft_run_id
        state["draft_archived_at"] = finished_at
    save_weekly_state(state)
    return state


def mark_no_draft_notified() -> dict[str, Any]:
    state = reset_week_state_if_new_week(load_weekly_state())
    state["no_draft_notified_at"] = utc_now()
    save_weekly_state(state)
    return state


def mark_monday_failure_notified() -> dict[str, Any]:
    state = reset_week_state_if_new_week(load_weekly_state())
    state["monday_failure_notified_at"] = utc_now()
    save_weekly_state(state)
    return state


def mark_credits_failure_notified() -> dict[str, Any]:
    state = reset_week_state_if_new_week(load_weekly_state())
    state["credits_failure_notified_at"] = utc_now()
    save_weekly_state(state)
    return state
