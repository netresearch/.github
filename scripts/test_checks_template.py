"""Fixtures for the TYPO3 Checks template: PR cancellation and the gate verdict.

A run cancelled on the head SHA leaves a failed `All security checks`
check-run there, and the newer run's green gate does not replace it: each run
keeps its own check-run and the commit's rollup stays FAILURE. These tests pin the two halves
of the fix: only `synchronize` (the event that moves the head SHA) cancels, and
the gate still fails on `cancelled`, so the fix is not a gate tolerance.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest

import yaml

from drift_compare import WorkflowLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates/typo3-extension/.github/workflows/checks.yml"
CANCEL = "${{ github.event_name == 'pull_request' && github.event.action == 'synchronize' }}"
GROUP = "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}"


def load_template():
    return yaml.load(TEMPLATE.read_text(), Loader=WorkflowLoader)


class ConcurrencyTest(unittest.TestCase):
    def test_only_synchronize_cancels_a_pull_request_run(self):
        concurrency = load_template()["concurrency"]
        self.assertEqual(concurrency, {"group": GROUP, "cancel-in-progress": CANCEL})

    def test_pull_request_types_that_reuse_the_head_sha_are_still_triggers(self):
        # The narrowed cancellation matters only because these run at all.
        types = load_template()["on"]["pull_request"]["types"]
        self.assertIn("ready_for_review", types)
        self.assertIn("reopened", types)
        self.assertIn("synchronize", types)


class GateVerdictTest(unittest.TestCase):
    """Run the gate step exactly as the template writes it."""

    @classmethod
    def setUpClass(cls):
        gate = load_template()["jobs"]["gate"]
        cls.needs = gate["needs"]
        steps = [step for step in gate["steps"] if "run" in step]
        assert len(steps) == 1, steps
        cls.script = steps[0]["run"]
        cls.condition = gate["if"]

    def test_gate_reports_whatever_its_needs_did(self):
        # Without `always()` a failed or cancelled scan would skip the gate,
        # and a skipped required check counts as passed.
        self.assertEqual(self.condition, "${{ always() }}")

    def verdict(self, results):
        payload = {name: {"result": result, "outputs": {}} for name, result in results.items()}
        # `shell: bash` runs as `bash --noprofile --norc -eo pipefail {0}`.
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", self.script],
                              env={"RESULTS": json.dumps(payload), "PATH": os.environ["PATH"]},
                              capture_output=True, text=True, check=False)

    def assert_verdict(self, results, code):
        result = self.verdict(results)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_all_success_passes(self):
        self.assert_verdict(dict.fromkeys(self.needs, "success"), 0)

    def test_skipped_passes(self):
        results = dict.fromkeys(self.needs, "success")
        results.update({"scorecard": "skipped", "pr-quality": "skipped"})
        self.assert_verdict(results, 0)

    def test_one_failure_fails(self):
        for name in self.needs:
            with self.subTest(job=name):
                results = dict.fromkeys(self.needs, "success")
                results[name] = "failure"
                result = self.assert_verdict(results, 1)
                self.assertIn(f"{name}=failure", result.stdout)

    def test_one_cancelled_fails(self):
        # A timed-out or hand-cancelled scan scanned nothing; it must not pass.
        for name in self.needs:
            with self.subTest(job=name):
                results = dict.fromkeys(self.needs, "success")
                results[name] = "cancelled"
                result = self.assert_verdict(results, 1)
                self.assertIn(f"{name}=cancelled", result.stdout)

    def test_superseded_run_shape_still_fails(self):
        # The needs of t3x-nr-vault run 42936523040 when the concurrency group
        # cancelled it. The gate is right to fail here; the fix keeps such a
        # run from being cancelled on the head SHA, it does not make this pass.
        results = dict.fromkeys(self.needs, "cancelled")
        results.update({"scorecard": "skipped", "pr-quality": "skipped"})
        result = self.assert_verdict(results, 1)
        self.assertIn("security=cancelled", result.stdout)


if __name__ == "__main__":
    unittest.main()
