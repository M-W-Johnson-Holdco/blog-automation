"""Tests for weekly failure Slack copy and credit-reason detection."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from blog_automation.weekly_pipeline import (
    FAILURE_REASON_ANTHROPIC_CREDITS,
    detect_anthropic_credits_failure_from_logs,
    monday_failure_slack_text,
    no_draft_slack_text,
    resolve_pipeline_failure_reason,
)


class WeeklyFailureSlackTextTests(unittest.TestCase):
    def test_monday_default_mentions_widened_retry(self) -> None:
        text = monday_failure_slack_text(iso_week="2026-W29")
        self.assertIn("no qualifying", text.lower())
        self.assertIn("Wednesday", text)
        self.assertNotIn("Anthropic", text)

    def test_monday_credits_asks_to_refill(self) -> None:
        text = monday_failure_slack_text(
            iso_week="2026-W29",
            reason=FAILURE_REASON_ANTHROPIC_CREDITS,
        )
        self.assertIn("Anthropic API credits are exhausted", text)
        self.assertIn("refill", text.lower())
        self.assertIn("console.anthropic.com/settings/billing", text)
        self.assertNotIn("no qualifying", text.lower())

    def test_wednesday_credits_asks_to_refill(self) -> None:
        text = no_draft_slack_text(
            iso_week="2026-W29",
            reason=FAILURE_REASON_ANTHROPIC_CREDITS,
        )
        self.assertIn("Anthropic API credits are exhausted", text)
        self.assertIn("refill", text.lower())


class ResolveFailureReasonTests(unittest.TestCase):
    def test_detects_credit_message_in_run_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output" / "peachtree"
            run_log = output / "multi_run" / "2026-07-13-120000" / "run.log"
            run_log.parent.mkdir(parents=True)
            run_log.write_text(
                "anthropic.BadRequestError: Your credit balance is too low to access the Anthropic API.\n",
                encoding="utf-8",
            )
            with patch("blog_automation.weekly_pipeline.OUTPUT_DIR", output):
                with patch(
                    "blog_automation.weekly_pipeline.PIPELINE_FAILURE_REASON_PATH",
                    output / "pipeline_failure_reason.json",
                ):
                    self.assertTrue(detect_anthropic_credits_failure_from_logs())
                    self.assertEqual(
                        resolve_pipeline_failure_reason(),
                        FAILURE_REASON_ANTHROPIC_CREDITS,
                    )


if __name__ == "__main__":
    unittest.main()
