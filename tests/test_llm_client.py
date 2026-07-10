"""Tests for llm_client Anthropic message mapping and usage extraction."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from blog_automation.llm_client import (
    anthropic_omits_sampling_params,
    chat_completion,
    get_llm_provider,
    normalize_anthropic_usage,
    split_messages_for_anthropic,
)


class AnthropicMessageMappingTests(unittest.TestCase):
    def test_split_messages_extracts_system_prompt(self) -> None:
        system, conversation = split_messages_for_anthropic(
            [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": "Score this source."},
            ]
        )
        self.assertEqual(system, "Return JSON only.")
        self.assertEqual(conversation, [{"role": "user", "content": "Score this source."}])

    def test_normalize_anthropic_usage_maps_token_fields(self) -> None:
        usage = SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=50)
        normalized = normalize_anthropic_usage(usage)
        self.assertEqual(normalized["prompt_tokens"], 1200)
        self.assertEqual(normalized["completion_tokens"], 300)
        self.assertEqual(normalized["total_tokens"], 1500)
        self.assertEqual(normalized["cached_tokens"], 50)


class AnthropicSamplingParamTests(unittest.TestCase):
    def test_opus_48_omits_sampling_params(self) -> None:
        self.assertTrue(anthropic_omits_sampling_params("claude-opus-4-8"))

    def test_sonnet_46_allows_sampling_params(self) -> None:
        self.assertFalse(anthropic_omits_sampling_params("claude-sonnet-4-6"))


class AnthropicChatCompletionTests(unittest.TestCase):
    def test_chat_completion_returns_content_and_cost_metadata(self) -> None:
        mock_response = SimpleNamespace(
            model="claude-sonnet-4-6",
            content=[SimpleNamespace(type="text", text='{"passed": true}')],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0),
        )
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch.dict(
            os.environ,
            {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "test-key"},
            clear=False,
        ):
            with patch("blog_automation.llm_client.get_anthropic_client", return_value=mock_client):
                content, metadata = chat_completion(
                    model="claude-sonnet-4-6",
                    messages=[
                        {"role": "system", "content": "Return strict JSON only."},
                        {"role": "user", "content": "Audit this draft."},
                    ],
                    temperature=0.1,
                    max_tokens=800,
                    response_format="json",
                )

        self.assertEqual(content, '{"passed": true}')
        self.assertEqual(metadata["model_used"], "claude-sonnet-4-6")
        self.assertEqual(metadata["provider"], "anthropic")
        self.assertEqual(metadata["usage"]["prompt_tokens"], 100)
        self.assertEqual(metadata["estimated_cost_usd"]["tokens"]["pricing_source"], "anthropic_catalog")

        request_kwargs = mock_client.messages.create.call_args.kwargs
        self.assertEqual(request_kwargs["model"], "claude-sonnet-4-6")
        self.assertIn("Return strict JSON only", request_kwargs["system"])
        self.assertEqual(request_kwargs["temperature"], 0.1)

    def test_opus_write_omits_temperature(self) -> None:
        mock_response = SimpleNamespace(
            model="claude-opus-4-8",
            content=[SimpleNamespace(type="text", text="# Atlanta Roof Guide\n\nBody.")],
            usage=SimpleNamespace(input_tokens=500, output_tokens=200, cache_read_input_tokens=0),
        )
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch.dict(
            os.environ,
            {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "test-key"},
            clear=False,
        ):
            with patch("blog_automation.llm_client.get_anthropic_client", return_value=mock_client):
                chat_completion(
                    model="claude-opus-4-8",
                    messages=[
                        {"role": "system", "content": "Return markdown only."},
                        {"role": "user", "content": "Write a blog draft."},
                    ],
                    temperature=0.35,
                    max_tokens=3400,
                )

        request_kwargs = mock_client.messages.create.call_args.kwargs
        self.assertNotIn("temperature", request_kwargs)


class LlmProviderTests(unittest.TestCase):
    def test_get_llm_provider_accepts_claude_alias(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "claude"}, clear=False):
            self.assertEqual(get_llm_provider(), "anthropic")


if __name__ == "__main__":
    unittest.main()
