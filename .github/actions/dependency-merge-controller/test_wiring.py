"""Parsed workflow wiring checks; native scheduling remains a separate pilot."""

import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]


def manifest(path):
    return json.loads(subprocess.check_output(["yq", "-o=json", ".", str(ROOT / path)], text=True))


class WiringTest(unittest.TestCase):
    def setUp(self):
        self.workflow = manifest(".github/workflows/dependency-merge-controller.yml")
        self.action = manifest(".github/actions/dependency-merge-controller/action.yml")

    def test_one_phase_job_and_no_workflow_level_concurrency(self):
        self.assertEqual(len(self.workflow["jobs"]), 1)
        self.assertNotIn("concurrency", self.workflow)
        inputs = self.workflow["on"]["workflow_call"]["inputs"]
        self.assertTrue(inputs["phase"]["required"])
        self.assertTrue(inputs["intent-workflow"]["required"])
        runs = [step["run"] for step in self.action["runs"]["steps"]
                if "runner.py" in step.get("run", "")]
        self.assertEqual(runs, ['python3 "$ACTION_ROOT/runner.py" "$PHASE"'])

    def test_actual_groups_keep_writers_and_each_full_head_separate(self):
        group = self.workflow["jobs"]["controller"]["concurrency"]["group"]
        formats = re.findall(r"format\('([^']+)', ([^)]+)\)", group)
        self.assertEqual(len(formats), 2)
        writer_format, writer_args = formats[0]
        evaluator_format, evaluator_args = formats[1]
        self.assertIn("github.event.pull_request.number", writer_args)
        self.assertIn("github.event.pull_request.head.sha", writer_args)
        self.assertIn("github.event.pull_request.head.ref || github.event.workflow_run.head_branch", evaluator_args)
        self.assertIn("github.event.pull_request.head.sha || github.event.workflow_run.head_sha", evaluator_args)
        head, old_head = "a" * 40, "b" * 40
        writer = writer_format.format("org/repo", 42, head, False)
        completion = evaluator_format.format("org/repo", "renovate/phpunit", head, False)
        review = evaluator_format.format("org/repo", "renovate/phpunit", head, False)
        late_old = evaluator_format.format("org/repo", "renovate/phpunit", old_head, False)
        self.assertNotEqual(writer, completion)
        self.assertEqual(completion, review)
        self.assertNotEqual(late_old, completion)
        self.assertFalse(self.workflow["jobs"]["controller"]["concurrency"]["cancel-in-progress"])

    def test_prepare_and_observer_cannot_mint_a_queue_app_token(self):
        mint = next(step for step in self.workflow["jobs"]["controller"]["steps"] if step.get("id") == "app-token")
        self.assertIn("inputs.phase == 'evaluate'", mint["if"])
        self.assertIn("!inputs.observe-only", mint["if"])
        uploads = [step for step in self.action["runs"]["steps"]
                   if step.get("name") == "Persist current-head intent before any queue decision"]
        self.assertEqual(len(uploads), 1)
        self.assertIn("inputs.phase == 'prepare'", uploads[0]["if"])


if __name__ == "__main__":
    unittest.main()
