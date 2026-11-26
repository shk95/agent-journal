import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_journal import summary
from agent_journal.explanation_usage import explanation_usage, record_usage
from agent_journal.settings import EXPLANATION_LEVELS, load_settings, update_settings


def usage_event(input_tokens=120, cached_input_tokens=30, output_tokens=25):
    return json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
        },
    })


class SummaryPolicyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.levels = EXPLANATION_LEVELS.copy()
        self.choices = {
            level: {"model": "fixture-model", "effort": effort}
            for level, effort in zip(self.levels, ("low", "medium", "high"))
        }

    def test_only_settings_json_controls_explanation_quality_and_usage(self):
        (self.root / "summary.json").write_text(json.dumps({
            "level": "High", "levels": {"High": "Explain the fixture's evidence."},
        }), encoding="utf-8")
        self.assertEqual(load_settings(self.root)["explanation_quality"], "Contextual")
        (self.root / "settings.json").write_text(json.dumps({
            "explanation_levels": {"In-depth": "Describe the fixture evidence."},
        }), encoding="utf-8")
        settings = update_settings(self.root, quality="심층", measure_usage=False)
        self.assertEqual(settings["explanation_levels"]["In-depth"], "Describe the fixture evidence.")
        self.assertEqual(settings["explanation_quality"], "In-depth")
        self.assertFalse(settings["measure_explanation_usage"])
        self.assertEqual(load_settings(self.root), settings)

    def test_reviews_when_due_or_when_descriptions_change(self):
        with (
            patch.object(summary.time, "time", return_value=100) as clock,
            patch.object(summary, "review_levels", return_value=self.choices) as review,
        ):
            first = summary.summary_policy(self.root, self.levels)
            self.assertEqual(first["choices"], self.choices)
            clock.return_value = first["next_review"] - 1
            self.assertEqual(summary.summary_policy(self.root, self.levels), first)
            review.assert_called_once_with(self.levels, usage_root=None)
            clock.return_value += 1
            summary.summary_policy(self.root, self.levels)
            self.levels["In-depth"] = "Preserve important results from running commands."
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
        for level, turns, items in (("Basic", 1, 10), ("Contextual", 3, 24), ("In-depth", 6, 40)):
            with self.subTest(level=level):
                context = summary.evidence(record, previous, level)
                self.assertEqual(context["previous"], previous[-turns:])
                self.assertEqual(len(context["observed"]), items)
                self.assertIn("Observation 0", context["observed"][0])
                self.assertIn("Observation 54", context["observed"][-1])
        self.assertEqual(summary.evidence(record, previous[-1], "In-depth")["previous"], previous[-1:])

    def test_explanation_generation_records_only_when_enabled(self):
        record = {"prompt": "Explain the example", "items": [], "status": "completed"}
        calls = []

        def codex(command, **options):
            calls.append(command)
            Path(command[command.index("-o") + 1]).write_text(json.dumps({
                "summary": "Checked the example", "summary_detail": "The fixture confirmed the example.",
            }))
            return subprocess.CompletedProcess(command, 0, stdout=usage_event())

        with (
            patch.object(summary.subprocess, "run", side_effect=codex),
            patch.dict(summary.os.environ, {"AGENT_JOURNAL_SUMMARY_MODEL": "legacy-fixture-model"}),
        ):
            summary.generate(record, [], level="In-depth", usage_root=self.root)
            summary.generate(record, [], level="Basic")
        self.assertIn("--json", calls[0])
        self.assertNotIn("--json", calls[1])
        self.assertNotIn("--model", calls[0])
        self.assertEqual(explanation_usage(self.root), [{
            "purpose": "generation", "quality": "In-depth", "model": "Codex default",
            "calls": 1, "measured_calls": 1, "input_tokens": 120,
            "cached_input_tokens": 30, "output_tokens": 25,
        }])

    def test_usage_report_separates_review_and_calls_without_token_data(self):
        record_usage(self.root, "calibration", None, None, usage_event())
        record_usage(self.root, "generation", "Basic", "fixture-model", b"partial output")
        calibration, generation = explanation_usage(self.root)
        self.assertEqual((calibration["purpose"], calibration["input_tokens"]), ("calibration", 120))
        self.assertEqual((generation["calls"], generation["measured_calls"]), (1, 0))
