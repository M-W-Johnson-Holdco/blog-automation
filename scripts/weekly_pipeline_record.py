"""Record scheduled weekly pipeline success or failure."""

from __future__ import annotations

import argparse
import sys

from blog_automation.weekly_pipeline import SCHEDULED_DAYS, record_pipeline_attempt


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record weekly pipeline attempt state.")
    parser.add_argument("--status", required=True, choices=("success", "failed"))
    parser.add_argument("--day", required=True, help="monday, wednesday, or manual")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)

    if args.day not in SCHEDULED_DAYS:
        print(f"[weekly_pipeline_record] Skipping state for day={args.day!r}")
        return

    draft_run_id = args.run_id if args.status == "success" else None
    record_pipeline_attempt(
        day=args.day,
        run_id=args.run_id,
        status=args.status,
        draft_run_id=draft_run_id,
    )
    print(f"[weekly_pipeline_record] Recorded {args.day} {args.status} run {args.run_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[weekly_pipeline_record] Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
