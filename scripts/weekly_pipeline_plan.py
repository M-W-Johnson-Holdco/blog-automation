"""Emit GitHub Actions outputs for the weekly blog pipeline plan."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from blog_automation.weekly_pipeline import plan_manual_dispatch, plan_scheduled_run


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    event = os.getenv("GITHUB_EVENT_NAME", "").strip()
    scheduled_day = os.getenv("SCHEDULED_DAY_INPUT", "").strip().lower()
    output_path = os.getenv("GITHUB_OUTPUT")

    if event == "workflow_dispatch" and scheduled_day in {"monday", "wednesday"}:
        plan = plan_scheduled_run(utc_weekday=0 if scheduled_day == "monday" else 2)
    elif event == "workflow_dispatch":
        plan = plan_manual_dispatch(widen_search=_truthy(os.getenv("WIDEN_SEARCH_INPUT")))
    elif event == "schedule":
        utc_weekday = datetime.now(timezone.utc).weekday()
        plan = plan_scheduled_run(utc_weekday=utc_weekday)
    else:
        plan = plan_manual_dispatch(widen_search=False)

    lines = plan.github_output_lines()
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(f"{line}\n")
    else:
        for line in lines:
            print(line)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[weekly_pipeline_plan] Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
