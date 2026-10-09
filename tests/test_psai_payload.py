"""Tests for the PSAI blog payload body (title handling, first-sentence limit)."""

from __future__ import annotations

import unittest

from blog_automation.post import PsaiConfig, PsaiError, build_blog_payload
from blog_automation.write_common import validate_draft

CONFIG = PsaiConfig(
    api_key="test",
    api_url="https://example.invalid",
    author="author@example.com",
    default_status="draft",
    site_brand="Test Roofing",
    notify_subscribers=False,
    auto_publish=False,
    display_author_details=True,
)


def _draft(first_sentence: str) -> str:
    return (
        "# Atlanta Flash Floods: How to Spot Hidden Roof Water Damage\n\n"
        f"{first_sentence} Second sentence of the opening.\n\n"
        "## What should homeowners check?\n\nBody text.\n"
    )


class PsaiPayloadTests(unittest.TestCase):
    def test_content_omits_leading_h1_title(self) -> None:
        payload = build_blog_payload(_draft("FOX 5 Atlanta reported storms on Friday."), None, CONFIG)
        self.assertEqual(payload["title"], "Atlanta Flash Floods: How to Spot Hidden Roof Water Damage")
        self.assertNotIn("<h1", payload["content"])
        self.assertTrue(payload["content"].lstrip().startswith("<p>FOX 5 Atlanta"))

    def test_rejects_first_sentence_over_psai_limit(self) -> None:
        long_sentence = "FOX 5 Atlanta reported " + "very " * 60 + "heavy storms."
        with self.assertRaisesRegex(PsaiError, "first sentence is"):
            build_blog_payload(_draft(long_sentence), None, CONFIG)

    def test_link_markup_does_not_count_toward_first_sentence(self) -> None:
        sentence = (
            "Regulators clarified roof-age rules (Source: [propertycasualty360.com]"
            "(https://www.propertycasualty360.com/" + "x" * 300 + "), October 2026)."
        )
        payload = build_blog_payload(_draft(sentence), None, CONFIG)
        self.assertIn("propertycasualty360.com", payload["content"])

    def test_validation_flags_long_opening_first_sentence(self) -> None:
        long_sentence = "FOX 5 Atlanta reported " + "very " * 55 + "heavy storms."
        report = validate_draft(_draft(long_sentence))
        self.assertFalse(report["checks"]["opening_first_sentence_short"])
        self.assertGreater(report["opening_first_sentence_chars"], 260)

        short = validate_draft(_draft("FOX 5 Atlanta reported storms on Friday."))
        self.assertTrue(short["checks"]["opening_first_sentence_short"])


if __name__ == "__main__":
    unittest.main()
