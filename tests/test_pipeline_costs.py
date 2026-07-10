"""Tests for per-stage pipeline API cost summaries."""

from __future__ import annotations

import unittest

from blog_automation.pipeline_costs import summarize_pipeline_run_costs


class PipelineCostSummaryTests(unittest.TestCase):
    def test_summarize_full_run_breakdown(self) -> None:
        summary = summarize_pipeline_run_costs(
            pipeline_costs={
                "search": {
                    "queries_run": 50,
                    "credits_used": 100,
                    "estimated_cost_usd": 0.8,
                },
                "evaluate": {
                    "model_used": "claude-sonnet-4-6",
                    "api_calls": 5,
                    "estimated_cost_usd": {
                        "tokens": {"total": 0.032, "input": 0.02, "output": 0.012},
                    },
                },
            },
            generation_report={
                "model_used": "claude-opus-4-8",
                "validation_attempts": 2,
                "estimated_cost_usd": {
                    "tokens": {"total": 0.2, "input": 0.08, "output": 0.12},
                },
                "fact_audit": {
                    "model_used": "claude-sonnet-4-6",
                    "estimated_cost_usd": {
                        "tokens": {"total": 0.03, "input": 0.02, "output": 0.01},
                    },
                },
            },
        )

        labels = [line["label"] for line in summary["lines"]]
        self.assertEqual(
            labels,
            ["Search (Tavily)", "Evaluate", "Write", "Fact-audit"],
        )
        self.assertAlmostEqual(summary["total_usd"], 0.8 + 0.032 + 0.2 + 0.03, places=6)

    def test_summarize_write_only_from_multi_run(self) -> None:
        summary = summarize_pipeline_run_costs(
            multi_run={
                "multi_run_write_inference_cost_usd": 0.11,
                "scorer_estimated_cost_usd": 0.004,
            },
            generation_report={"model_used": "claude-opus-4-8", "validation_attempts": 1},
        )
        labels = [line["label"] for line in summary["lines"]]
        self.assertEqual(labels, ["Write", "Scorer"])
        self.assertAlmostEqual(summary["total_usd"], 0.114, places=6)


if __name__ == "__main__":
    unittest.main()
