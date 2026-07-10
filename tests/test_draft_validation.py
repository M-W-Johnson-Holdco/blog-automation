"""Regression tests for draft accuracy validation helpers."""

from __future__ import annotations

import unittest
from pathlib import Path

from blog_automation.write_common import (
    citations_match_kept_source_urls,
    extract_citations,
    percentage_claims_grounded_in_sources,
    validate_draft,
)

GOOD_IJ_URL = "https://www.insurancejournal.com/news/southcentral/2026/06/16/873958.htm"
BROKEN_IJ_URL = "https://www.insurancejournal.com/news/southcentral/2026/06/16/16/873958.htm"
PC360_URL = "https://www.propertycasualty360.com/2026/06/10/michigan-bill-targets-insurance-price-optimization/"

KEPT_SOURCES = [
    {
        "title": "Latent Construction Defects",
        "url": GOOD_IJ_URL,
        "content": "For adjusters handling commercial property claims, latent construction defects introduce subrogation considerations.",
        "source": {
            "title": "Latent Construction Defects",
            "url": GOOD_IJ_URL,
            "content": "For adjusters handling commercial property claims, latent construction defects introduce subrogation considerations.",
        },
    },
    {
        "title": "Michigan bill targets insurance price optimization",
        "url": PC360_URL,
        "content": "Insurers are increasingly using price optimization algorithms across the national property insurance sector.",
        "source": {
            "title": "Michigan bill targets insurance price optimization",
            "url": PC360_URL,
            "content": "Insurers are increasingly using price optimization algorithms across the national property insurance sector.",
        },
    },
]


class DraftAccuracyValidationTests(unittest.TestCase):
    def test_citations_match_rejects_altered_url_path(self) -> None:
        markdown = (
            "Body text with a cite. "
            f"(Source: [insurancejournal.com]({BROKEN_IJ_URL}), June 2026)"
        )
        citations = extract_citations(markdown)
        matched, invalid = citations_match_kept_source_urls(citations, KEPT_SOURCES)
        self.assertFalse(matched)
        self.assertEqual(len(invalid), 1)

    def test_citations_match_accepts_exact_kept_urls(self) -> None:
        markdown = (
            f"(Source: [insurancejournal.com]({GOOD_IJ_URL}), June 2026) "
            f"(Source: [propertycasualty360.com]({PC360_URL}), June 2026)"
        )
        citations = extract_citations(markdown)
        matched, invalid = citations_match_kept_source_urls(citations, KEPT_SOURCES)
        self.assertTrue(matched)
        self.assertEqual(invalid, [])

    def test_percentage_claims_reject_ungrounded_stat(self) -> None:
        markdown = "### Does the 33% rise in construction costs affect my claim?"
        grounded, ungrounded = percentage_claims_grounded_in_sources(markdown, KEPT_SOURCES)
        self.assertFalse(grounded)
        self.assertEqual(ungrounded, ["33%"])

    def test_percentage_claims_accept_when_source_contains_stat(self) -> None:
        sources = [
            {
                "title": "Cost report",
                "url": "https://example.com/costs",
                "content": "Residential roof replacement costs jumped 33 percent in 2025.",
            }
        ]
        markdown = "Costs jumped 33% in 2025 for Metro Atlanta homeowners."
        grounded, ungrounded = percentage_claims_grounded_in_sources(markdown, sources)
        self.assertTrue(grounded)
        self.assertEqual(ungrounded, [])

    def test_validate_draft_flags_132946_accuracy_issues(self) -> None:
        draft_path = (
            Path(__file__).resolve().parents[1]
            / "output/multi_run/2026-06-16-132648/industry_insight/drafts_md/2026-06-16-132946-industry_insight.md"
        )
        if not draft_path.exists():
            self.skipTest("132946 draft fixture not present in workspace")
        markdown = draft_path.read_text(encoding="utf-8")
        report = validate_draft(markdown, selected_sources=KEPT_SOURCES)
        self.assertFalse(report["checks"]["citations_match_kept_source_urls"])
        self.assertFalse(report["checks"]["percentage_claims_grounded_in_sources"])
        self.assertIn("33%", report["ungrounded_percentage_claims"])


if __name__ == "__main__":
    unittest.main()
