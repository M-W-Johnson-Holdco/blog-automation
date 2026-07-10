"""Tests for Markdown → HTML table border inlining."""

from __future__ import annotations

import unittest

from blog_automation.draft_pdf import (
    CELL_INLINE_STYLE,
    TABLE_INLINE_STYLE,
    TH_INLINE_STYLE,
    apply_inline_table_borders,
    markdown_body_to_html,
)


class InlineTableBorderTests(unittest.TestCase):
    def test_apply_inline_table_borders_adds_styles(self) -> None:
        html = "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
        styled = apply_inline_table_borders(html)
        self.assertIn(f'style="{TABLE_INLINE_STYLE}"', styled)
        self.assertIn(f'style="{TH_INLINE_STYLE}"', styled)
        self.assertIn(f'style="{CELL_INLINE_STYLE}"', styled)
        self.assertIn("border: 1px solid #999", styled)

    def test_apply_inline_table_borders_merges_existing_style(self) -> None:
        html = '<td style="color: red">x</td>'
        styled = apply_inline_table_borders(html)
        self.assertIn('style="color: red; ' + CELL_INLINE_STYLE + '"', styled)

    def test_markdown_body_to_html_inlines_table_borders(self) -> None:
        markdown = "| Area | Action |\n| --- | --- |\n| Dallas | Inspect |"
        html = markdown_body_to_html(markdown)
        self.assertIn("<table", html)
        self.assertIn("border: 1px solid #999", html)
        self.assertIn("border-collapse: collapse", html)


if __name__ == "__main__":
    unittest.main()
