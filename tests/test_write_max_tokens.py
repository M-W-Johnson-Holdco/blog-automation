"""Tests for provider-aware write max_tokens resolution."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from blog_automation.llm_models import normalize_writing_model_for_provider
from blog_automation.write_common import resolve_write_max_tokens


class WriteMaxTokensTests(unittest.TestCase):
    def test_anthropic_default_is_6000(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}, clear=False):
            self.assertEqual(resolve_write_max_tokens(), 6000)

    def test_together_default_is_3400(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "together"}, clear=False):
            self.assertEqual(resolve_write_max_tokens(), 3400)

    def test_claude_env_override(self) -> None:
        with patch.dict(
            os.environ,
            {"LLM_PROVIDER": "anthropic", "CLAUDE_WRITING_MAX_TOKENS": "7000"},
            clear=False,
        ):
            self.assertEqual(resolve_write_max_tokens(), 7000)


class NormalizeWritingModelTests(unittest.TestCase):
    def test_maps_stale_together_model_to_claude_default(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}, clear=False):
            self.assertEqual(
                normalize_writing_model_for_provider("Qwen/Qwen3.5-397B-A17B"),
                "claude-opus-4-8",
            )

    def test_keeps_claude_model_on_anthropic(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}, clear=False):
            self.assertEqual(
                normalize_writing_model_for_provider("claude-opus-4-8"),
                "claude-opus-4-8",
            )


if __name__ == "__main__":
    unittest.main()
