"""Tests for Slack @bot pipeline mention handling."""

from __future__ import annotations

import unittest

from blog_automation.slack_actions.pipeline_mention import (
    is_pipeline_mention_command,
    strip_slack_mentions,
)


class TestPipelineMention(unittest.TestCase):
    def test_strip_slack_mentions(self) -> None:
        self.assertEqual(strip_slack_mentions("<@U123ABC> pipeline"), "pipeline")
        self.assertEqual(strip_slack_mentions("  <@U1>   <@U2>  pipeline  "), "pipeline")

    def test_is_pipeline_mention_command(self) -> None:
        self.assertTrue(is_pipeline_mention_command("<@U123> pipeline"))
        self.assertTrue(is_pipeline_mention_command("<@U123>  PIPELINE"))
        self.assertFalse(is_pipeline_mention_command("<@U123> publish"))
        self.assertTrue(is_pipeline_mention_command("pipeline"))
        self.assertFalse(is_pipeline_mention_command("<@U123> pipeline please"))


if __name__ == "__main__":
    unittest.main()
