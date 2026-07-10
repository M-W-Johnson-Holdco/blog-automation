"""Tests for Slack PSAI post commands (publish vs draft)."""

from __future__ import annotations

import unittest

from blog_automation.post import (
    PSAI_POST_COMMAND_STATUSES,
    parse_psai_post_command,
    psai_publish_command_prompt_text,
)


class TestPsaiPostCommands(unittest.TestCase):
    def test_command_status_map(self) -> None:
        self.assertEqual(PSAI_POST_COMMAND_STATUSES["publish"], "published")
        self.assertEqual(PSAI_POST_COMMAND_STATUSES["draft"], "draft")

    def test_parse_psai_post_command(self) -> None:
        self.assertEqual(parse_psai_post_command("publish"), "published")
        self.assertEqual(parse_psai_post_command("  DRAFT "), "draft")
        self.assertIsNone(parse_psai_post_command("feedback please"))
        self.assertIsNone(parse_psai_post_command(""))

    def test_prompt_mentions_both_commands(self) -> None:
        prompt = psai_publish_command_prompt_text()
        self.assertIn("publish", prompt)
        self.assertIn("draft", prompt)
        self.assertIn("post live on the site", prompt)


if __name__ == "__main__":
    unittest.main()
