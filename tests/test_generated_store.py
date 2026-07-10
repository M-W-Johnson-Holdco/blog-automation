"""Tests for generated/ archive helpers used by cloud Slack approval."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from blog_automation.generated_store import archive_draft_for_ci, load_slack_index
from blog_automation.paths import GENERATED_RUNS_DIR, GENERATED_SLACK_INDEX_PATH, PROJECT_ROOT
from blog_automation.write_common import DEFAULT_OUTPUT_DIR, ensure_draft_subdirs


class GeneratedStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = "test-revision-archive"
        self.channel = "C_TEST"
        self.message_ts = "12345.6789"
        self.stem = "2099-01-01-000000-test-revision"
        self.draft_root = DEFAULT_OUTPUT_DIR
        md_dir, pdf_dir, json_dir = ensure_draft_subdirs(self.draft_root)
        self.md_path = md_dir / f"{self.stem}.md"
        self.pdf_path = pdf_dir / f"{self.stem}.pdf"
        self.json_path = json_dir / f"{self.stem}-validation.json"
        self.generated_root = GENERATED_RUNS_DIR / self.run_id
        self.prior_index_text = GENERATED_SLACK_INDEX_PATH.read_text(encoding="utf-8") if GENERATED_SLACK_INDEX_PATH.exists() else None

        self.md_path.write_text("# Test revision\n\nBody.", encoding="utf-8")
        self.pdf_path.write_bytes(b"%PDF test")
        self.json_path.write_text(
            json.dumps(
                {
                    "draft_path": str(self.md_path.relative_to(PROJECT_ROOT)),
                    "pdf_path": str(self.pdf_path.relative_to(PROJECT_ROOT)),
                    "approval": {
                        "channel": self.channel,
                        "message_ts": self.message_ts,
                        "status": "pending",
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        for path in (self.md_path, self.pdf_path, self.json_path):
            path.unlink(missing_ok=True)
        shutil.rmtree(self.generated_root, ignore_errors=True)
        if self.prior_index_text is None:
            GENERATED_SLACK_INDEX_PATH.unlink(missing_ok=True)
        else:
            GENERATED_SLACK_INDEX_PATH.write_text(self.prior_index_text, encoding="utf-8")

    def test_archive_specific_revision_indexes_message_ts(self) -> None:
        validation_path = archive_draft_for_ci(
            self.md_path,
            run_id=self.run_id,
            channel=self.channel,
            message_ts=self.message_ts,
        )

        self.assertTrue(validation_path.is_file())
        report = json.loads(validation_path.read_text(encoding="utf-8"))
        self.assertEqual(report["ci_run_id"], self.run_id)
        from blog_automation.company import get_company_slug

        self.assertTrue(
            report["draft_path"].startswith(f"generated/{get_company_slug()}/runs/{self.run_id}/")
        )

        index = load_slack_index()
        entry = index["messages"][f"{self.channel}:{self.message_ts}"]
        self.assertEqual(entry["run_id"], self.run_id)
        self.assertEqual(entry["validation_path"], str(validation_path.relative_to(PROJECT_ROOT)))


if __name__ == "__main__":
    unittest.main()
