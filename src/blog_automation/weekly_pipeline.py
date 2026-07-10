"""Track scheduled weekly draft runs (Mon primary, Wed conditional retry)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blog_automation.paths import GENERATED_DIR, GENERATED_WEEKLY_PIPELINE_PATH, PROJECT_ROOT
from blog_automation.company import get_profile

SCHEDULED_DAYS = frozenset({"monday", "wednesday"})


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


def monday_failure_slack_text(*, iso_week: str) -> str:
    return (
        f"No blog draft for week {iso_week} — search found no qualifying "
        f"{get_profile().METRO_AREA} roofing stories. Will retry Wednesday at 8:00 AM ET "
        "with widened search."
    )


def no_draft_slack_text(*, iso_week: str) -> str:
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
