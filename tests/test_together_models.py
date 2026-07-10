"""Regression tests for Together model fallback chains and fact-audit parsing."""

from __future__ import annotations

import unittest

from blog_automation.fact_audit import build_fact_audit_prompt, parse_fact_audit_response
from blog_automation.together_models import (
    EVALUATION_MODEL_FALLBACK_CHAIN,
    FACT_AUDIT_MODEL_FALLBACK_CHAIN,
    SCORER_MODEL_FALLBACK_CHAIN,
    WRITING_MODEL_FALLBACK_CHAIN,
    model_attempt_sequence,
    resolve_evaluation_model,
    resolve_fact_audit_model,
    resolve_scorer_model,
)


class TogetherModelFallbackTests(unittest.TestCase):
    def test_writing_chain_retries_down_on_unavailable_primary(self) -> None:
        primary = WRITING_MODEL_FALLBACK_CHAIN[0]
        sequence = model_attempt_sequence(primary, WRITING_MODEL_FALLBACK_CHAIN)
        self.assertEqual(sequence[0], primary)
        self.assertEqual(sequence[1:], list(WRITING_MODEL_FALLBACK_CHAIN[1:]))

    def test_evaluation_chain_starts_with_7b(self) -> None:
        sequence = model_attempt_sequence(
            EVALUATION_MODEL_FALLBACK_CHAIN[0],
            EVALUATION_MODEL_FALLBACK_CHAIN,
        )
        self.assertEqual(sequence[0], "Qwen/Qwen2.5-7B-Instruct-Turbo")
        self.assertIn("Qwen/Qwen3-235B-A22B-Instruct-2507-tput", sequence[1:])

    def test_scorer_and_fact_audit_share_235b_primary(self) -> None:
        self.assertEqual(SCORER_MODEL_FALLBACK_CHAIN[0], FACT_AUDIT_MODEL_FALLBACK_CHAIN[0])
        self.assertEqual(resolve_scorer_model(), resolve_fact_audit_model())
        self.assertEqual(resolve_evaluation_model(), EVALUATION_MODEL_FALLBACK_CHAIN[0])

    def test_custom_model_outside_chain_still_gets_chain_fallbacks(self) -> None:
        sequence = model_attempt_sequence(
            "custom/user-model",
            SCORER_MODEL_FALLBACK_CHAIN,
        )
        self.assertEqual(sequence[0], "custom/user-model")
        self.assertEqual(sequence[1:], list(SCORER_MODEL_FALLBACK_CHAIN))


class FactAuditParseTests(unittest.TestCase):
    def test_build_fact_audit_prompt_preserves_json_example_braces(self) -> None:
        prompt = build_fact_audit_prompt(
            "# Draft\n\nPeachtree draft body.",
            [
                {
                    "url": "https://example.com/a",
                    "title": "Example",
                    "content": "Source fact.",
                }
            ],
        )
        self.assertIn('{\n  "passed"', prompt)
        self.assertNotIn("{{", prompt)
        self.assertNotIn("}}", prompt)
        self.assertIn("https://example.com/a", prompt)
        self.assertIn("Peachtree draft body.", prompt)

    def test_parse_failed_audit_with_issues(self) -> None:
        parsed = parse_fact_audit_response(
            """
            {
              "passed": false,
              "issues": [
                {
                  "severity": "error",
                  "category": "ungrounded_stat",
                  "detail": "FAQ mentions 33% not in sources"
                }
              ],
              "revision_notes": "Remove the 33% reference from FAQ question 1."
            }
            """
        )
        self.assertFalse(parsed["passed"])
        self.assertEqual(len(parsed["issues"]), 1)
        self.assertIn("33%", parsed["revision_notes"])

    def test_passed_audit_clears_issues_and_notes(self) -> None:
        parsed = parse_fact_audit_response(
            '{"passed": true, "issues": [{"severity": "error"}], "revision_notes": "ignore"}'
        )
        self.assertTrue(parsed["passed"])
        self.assertEqual(parsed["issues"], [])
        self.assertEqual(parsed["revision_notes"], "")


if __name__ == "__main__":
    unittest.main()
