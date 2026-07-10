"""Tests for Slack Approval Handler dependency classification."""

from __future__ import annotations

import unittest

from scripts.classify_slack_event import needs_pdf_deps, should_skip_event


class SlackEventClassifierTests(unittest.TestCase):
    def test_reaction_added_approval_does_not_need_pdf_deps(self) -> None:
        event = {
            "type": "reaction_added",
            "reaction": "white_check_mark",
            "item": {"type": "message", "channel": "C123", "ts": "1000.000"},
        }
        self.assertFalse(needs_pdf_deps(event))
        self.assertFalse(should_skip_event(event))

    def test_thread_reply_publish_does_not_need_pdf_deps(self) -> None:
        event = {
            "type": "message",
            "channel": "C123",
            "ts": "1001.000",
            "thread_ts": "1000.000",
            "text": "publish",
        }
        self.assertFalse(needs_pdf_deps(event))
        self.assertFalse(should_skip_event(event))

    def test_thread_reply_draft_does_not_need_pdf_deps(self) -> None:
        event = {
            "type": "message",
            "channel": "C123",
            "ts": "1001.000",
            "thread_ts": "1000.000",
            "text": " draft ",
        }
        self.assertFalse(needs_pdf_deps(event))
        self.assertFalse(should_skip_event(event))

    def test_free_form_thread_feedback_needs_pdf_deps(self) -> None:
        event = {
            "type": "message",
            "channel": "C123",
            "ts": "1001.000",
            "thread_ts": "1000.000",
            "text": "please rewrite intro",
        }
        self.assertTrue(needs_pdf_deps(event))
        self.assertFalse(should_skip_event(event))

    def test_top_level_channel_message_is_skipped(self) -> None:
        event = {
            "type": "message",
            "channel": "C123",
            "ts": "1001.000",
            "text": "oops wrong channel",
        }
        self.assertTrue(should_skip_event(event))
        self.assertFalse(needs_pdf_deps(event))

    def test_top_level_app_mention_is_skipped_by_approval_handler(self) -> None:
        event = {
            "type": "app_mention",
            "channel": "C123",
            "ts": "1001.000",
            "text": "<@U123> pipeline",
        }
        self.assertTrue(should_skip_event(event))
        self.assertFalse(needs_pdf_deps(event))


if __name__ == "__main__":
    unittest.main()
