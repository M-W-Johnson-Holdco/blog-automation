"""Tests for provider-aware Claude model resolution."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from blog_automation.claude_models import (
    DEFAULT_EVALUATION_MODEL,
    DEFAULT_FACT_AUDIT_MODEL,
    DEFAULT_WRITING_MODEL,
    resolve_evaluation_model,
    resolve_fact_audit_model,
    resolve_writing_model,
)
from blog_automation.llm_models import (
    default_writing_model,
    resolve_writing_model as resolve_provider_writing_model,
)


class ClaudeModelResolutionTests(unittest.TestCase):
    def test_default_writing_model_is_opus(self) -> None:
        self.assertEqual(DEFAULT_WRITING_MODEL, "claude-opus-4-8")

    def test_default_evaluation_and_fact_audit_use_sonnet(self) -> None:
        self.assertEqual(DEFAULT_EVALUATION_MODEL, "claude-sonnet-4-6")
        self.assertEqual(DEFAULT_FACT_AUDIT_MODEL, "claude-sonnet-4-6")

    def test_env_override_for_writing_model(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_WRITING_MODEL": "claude-sonnet-4-6"}, clear=False):
            self.assertEqual(resolve_writing_model(), "claude-sonnet-4-6")

    def test_explicit_model_arg_wins_over_env(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_WRITING_MODEL": "claude-sonnet-4-6"}, clear=False):
            self.assertEqual(resolve_writing_model("claude-opus-4-8"), "claude-opus-4-8")

    def test_provider_defaults_to_anthropic_models(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}, clear=False):
            self.assertEqual(default_writing_model(), "claude-opus-4-8")
            self.assertEqual(resolve_provider_writing_model(), "claude-opus-4-8")
            self.assertEqual(resolve_fact_audit_model(), DEFAULT_FACT_AUDIT_MODEL)


if __name__ == "__main__":
    unittest.main()
