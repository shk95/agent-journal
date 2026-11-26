import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_journal import summary


class SummaryPolicyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.levels = summary.LEVELS.copy()
        self.choices = {
            level: {"model": "fixture-model", "effort": effort}
            for level, effort in zip(self.levels, ("low", "medium", "high"))
        }

    def test_settings_keep_descriptions_when_selecting_a_localized_level(self):
        settings = summary.load_settings(self.root)
        self.assertEqual(settings["level"], "Normal")
        settings["levels"]["High"] = "Preserve the important outcome in ambiguous turns."
        summary.save_settings(self.root, settings, "높음")
        self.assertEqual(summary.load_settings(self.root), settings | {"level": "High"})

    def test_reviews_when_due_or_when_descriptions_change(self):
        with (
            patch.object(summary.time, "time", return_value=100) as clock,
            patch.object(summary, "review_levels", return_value=self.choices) as review,
        ):
            first = summary.summary_policy(self.root, self.levels)
            self.assertEqual(first["choices"], self.choices)
            clock.return_value = first["next_review"] - 1
            self.assertEqual(summary.summary_policy(self.root, self.levels), first)
            review.assert_called_once_with(self.levels)
            clock.return_value += 1
            summary.summary_policy(self.root, self.levels)
            self.levels["High"] = "Preserve important results from running commands."
            summary.summary_policy(self.root, self.levels)
            self.assertEqual(review.call_count, 3)

    def test_failed_review_keeps_previous_choices_and_backs_off(self):
        with (
            patch.object(summary.time, "time", return_value=100) as clock,
            patch.object(summary, "review_levels", return_value=self.choices) as review,
        ):
            first = summary.summary_policy(self.root, self.levels)
            clock.return_value = first["next_review"]
            review.side_effect = RuntimeError("Fixture unavailable")
            cached = summary.summary_policy(self.root, self.levels)
            self.assertEqual(cached["choices"], self.choices)
            self.assertEqual(cached["next_review"], clock.return_value + summary.REVIEW_RETRY)
            self.assertEqual(summary.summary_policy(self.root, self.levels), cached)
            self.assertEqual(review.call_count, 2)

    def test_review_rejects_effort_the_model_does_not_support(self):
        catalog = {"models": [{
            "slug": "fixture-model",
            "description": "Synthetic model",
            "visibility": "list",
            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
        }]}
        with (
            patch.object(summary.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(catalog),
            )),
            patch.object(summary, "run_codex", return_value=json.dumps(self.choices)),
            self.assertRaisesRegex(ValueError, "unsupported model or reasoning effort"),
        ):
            summary.review_levels(self.levels)

    def test_quality_levels_include_more_preceding_conversation_and_evidence(self):
        record = {
            "prompt": "Use the second option.",
            "status": "completed",
            "items": [{"type": "agentMessage", "text": f"Observation {i}"} for i in range(55)],
        }
        previous = [{"request": f"Earlier request {i}", "answer": "Reviewed options."} for i in range(6)]
        for level, turns, items in (("Low", 1, 10), ("Normal", 3, 24), ("High", 6, 40)):
            with self.subTest(level=level):
                context = summary.evidence(record, previous, level)
                self.assertEqual(context["previous"], previous[-turns:])
                self.assertEqual(len(context["observed"]), items)
                self.assertIn("Observation 0", context["observed"][0])
                self.assertIn("Observation 54", context["observed"][-1])
        self.assertEqual(summary.evidence(record, previous[-1], "High")["previous"], previous[-1:])
