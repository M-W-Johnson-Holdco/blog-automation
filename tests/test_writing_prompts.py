"""Tests for source-mode template routing after Claude migration."""

from __future__ import annotations

import unittest

from blog_automation.writing_prompts import resolve_template_ids


class SourceModeTemplateTests(unittest.TestCase):
    def test_local_mode_returns_single_rotated_template(self) -> None:
        local_sources = [
            {"priority_source": True, "title": "Atlanta storm"},
            {"secondary_source": True, "title": "Cobb roofing"},
        ]
        template_ids = resolve_template_ids(local_sources, rotation_week=10)
        self.assertEqual(len(template_ids), 1)
        self.assertIn(template_ids[0], ("geo", "scenario", "explainer"))

    def test_mixed_mode_uses_local_anchor(self) -> None:
        sources = [
            {"priority_source": True, "title": "Atlanta"},
            {"title": "National trade story"},
        ]
        self.assertEqual(resolve_template_ids(sources), ("local_anchor",))

    def test_national_mode_uses_industry_insight(self) -> None:
        sources = [{"title": "National trade story"}]
        self.assertEqual(resolve_template_ids(sources), ("industry_insight",))


if __name__ == "__main__":
    unittest.main()
