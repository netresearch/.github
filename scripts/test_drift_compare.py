"""Offline CLI fixtures for the exact TYPO3 Checks concurrency transition."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from drift_compare import WorkflowLoader

ROOT = Path(__file__).resolve().parents[1]
CHECKS = ".github/workflows/checks.yml"
GROUP = "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}"
CANCEL = "${{ github.event_name == 'pull_request' }}"
NOTICE = "Approved Checks PR-concurrency transition"
HASH_A = "65c451549abd2ebd6fcdb367d47b855e25b2d48b8315149276caf33af2a01656"
HASH_B = "9255cc2adf021436f7f52875e0d8e654d1e7aad8dad4dacfb1638f4ab1e18a22"
# The harden-runner pin of both reviewed documents. Renovate may move the template
# past it, so the documents are rebuilt with this pin rather than the current one.
REVIEWED_PIN = "step-security/harden-runner@dc2769abcc0fcfbadfc1df54081c1608579ef6b2"


def fingerprint(document):
    data = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return hashlib.sha256(data.encode()).hexdigest()


def with_reviewed_pin(document):
    reviewed = copy.deepcopy(document)
    for job in reviewed["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("step-security/harden-runner@"):
                step["uses"] = REVIEWED_PIN
    return reviewed


class TransitionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        current = yaml.load((ROOT / "templates/typo3-extension" / CHECKS).read_text(), Loader=WorkflowLoader)
        cls.current = current
        cls.a = with_reviewed_pin(current)
        cls.a.pop("concurrency", None)
        cls.b = copy.deepcopy(cls.a)
        cls.b["concurrency"] = {"group": GROUP, "cancel-in-progress": CANCEL}

    def compare(self, template, consumer, *, template_name="typo3-extension", path=CHECKS,
                wrapper=False, actions=False, consumer_text=None, escape=None):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            template_root = base / "templates" / template_name
            consumer_root = base / "consumer"
            template_file = template_root / path
            consumer_file = consumer_root / path
            template_file.parent.mkdir(parents=True)
            consumer_file.parent.mkdir(parents=True)
            template_file.write_text(yaml.safe_dump(template, sort_keys=False, allow_unicode=True))
            if consumer is not None:
                consumer_file.write_text(consumer_text if consumer_text is not None else
                                         yaml.safe_dump(consumer, sort_keys=False, allow_unicode=True))
            if escape:
                outside = base / "outside.yml"
                outside.write_text(yaml.safe_dump(self.a))
                target = consumer_file if escape == "consumer" else template_file
                target.unlink()
                target.symlink_to(outside)
            command = (["bash", str(ROOT / "scripts/check-drift.sh")] if wrapper else
                       [sys.executable, str(ROOT / "scripts/drift_compare.py")])
            environment = os.environ | {"PYTHONDONTWRITEBYTECODE": "1", "GITHUB_ACTIONS": "true" if actions else ""}
            return subprocess.run(command + [str(consumer_root), str(template_root)],
                                  capture_output=True, text=True, check=False, env=environment)

    def assert_drift(self, template, consumer, **kwargs):
        result = self.compare(template, consumer, **kwargs)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(NOTICE, result.stdout)

    def test_frozen_whole_document_fingerprints(self):
        # Any canonical change except a harden-runner pin bump still fails here.
        self.assertIn(fingerprint(with_reviewed_pin(self.current)), {HASH_A, HASH_B})
        self.assertEqual(fingerprint(self.a), HASH_A)
        self.assertEqual(fingerprint(self.b), HASH_B)

    def test_identical_a_and_b_keep_normal_behavior(self):
        for document in (self.a, self.b):
            with self.subTest(fingerprint=fingerprint(document)):
                result = self.compare(document, document)
                self.assertEqual(result.returncode, 0)
                self.assertNotIn(NOTICE, result.stdout)

    def test_both_rollout_directions_have_distinct_notice(self):
        for template, consumer in ((self.a, self.b), (self.b, self.a)):
            with self.subTest(template=fingerprint(template)):
                result = self.compare(template, consumer)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(NOTICE, result.stdout)
                self.assertNotIn("differ only in comments", result.stdout)

    def test_comments_key_order_and_yaml_on_spelling_are_semantic(self):
        reordered = dict(reversed(list(self.b.items())))
        text = "# consumer prose\n" + yaml.safe_dump(reordered, sort_keys=False, allow_unicode=True)
        result = self.compare(self.a, self.b, consumer_text=text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        normal = self.compare(self.b, self.b, consumer_text=text)
        self.assertEqual(normal.returncode, 0)
        self.assertIn("differ only in comments", normal.stdout)

    def test_every_removed_job_is_still_drift(self):
        for job in self.a["jobs"]:
            changed = copy.deepcopy(self.a)
            del changed["jobs"][job]
            with self.subTest(job=job):
                self.assert_drift(self.b, changed)

    def test_gate_dependency_removal_and_reordering_are_drift(self):
        changed = copy.deepcopy(self.a)
        changed["jobs"]["gate"]["needs"].remove("security")
        self.assert_drift(self.b, changed)
        changed = copy.deepcopy(self.a)
        changed["jobs"]["gate"]["needs"].reverse()
        self.assert_drift(self.b, changed)

    def test_permissions_uses_conditions_commands_and_triggers_are_drift(self):
        mutations = [
            ("permissions", lambda doc: doc["jobs"]["security"]["permissions"].update(contents="write")),
            ("uses", lambda doc: doc["jobs"]["security"].update(uses="attacker/action@main")),
            ("condition", lambda doc: doc["jobs"]["security"].update({"if": "false"})),
            ("command", lambda doc: doc["jobs"]["gate"]["steps"][-1].update(run="exit 0")),
            ("trigger", lambda doc: doc["on"].pop("merge_group")),
            ("root-permission", lambda doc: doc["permissions"].update(contents="write")),
            ("extra-input", lambda doc: doc.update(env={"SKIP_SECURITY": "true"})),
        ]
        for name, mutate in mutations:
            for template, consumer in ((self.a, self.b), (self.b, self.a)):
                changed = copy.deepcopy(consumer)
                mutate(changed)
                with self.subTest(name=name, template=fingerprint(template)):
                    self.assert_drift(template, changed)

    def test_extra_job_is_not_grandfathered(self):
        changed = copy.deepcopy(self.a)
        changed["jobs"]["unexpected"] = {"runs-on": "ubuntu-latest", "steps": [{"run": "echo unexpected"}]}
        self.assert_drift(self.b, changed)

    def test_only_exact_reviewed_concurrency_is_allowed(self):
        variants = [None, {}, {"group": GROUP, "cancel-in-progress": True},
                    {"group": "${{ github.ref }}", "cancel-in-progress": CANCEL},
                    {"group": GROUP, "cancel-in-progress": CANCEL, "unexpected": "value"},
                    {"group": GROUP, "cancel-in-progress": False},
                    {"group": GROUP, "cancel-in-progress": "false"}]
        for variant in variants:
            changed = copy.deepcopy(self.a)
            changed["concurrency"] = variant
            with self.subTest(concurrency=variant):
                self.assert_drift(self.a, changed)
                self.assert_drift(self.b, changed)

    def test_exact_template_name_and_path_are_required(self):
        self.assert_drift(self.a, self.b, template_name="go-lib")
        self.assert_drift(self.a, self.b, path=".github/workflows/other.yml")
        self.assert_drift(self.a, self.b, path=".github/workflows/checks.yaml")

    def test_future_canonical_document_never_inherits_transition(self):
        future = copy.deepcopy(self.b)
        future["jobs"]["security"]["uses"] = "netresearch/typo3-ci-workflows/.github/workflows/security.yml@v2"
        self.assert_drift(future, self.a)
        self.assert_drift(future, self.b)
        self.assertEqual(self.compare(future, future).returncode, 0)

    def test_missing_malformed_and_escaping_files_remain_errors(self):
        self.assert_drift(self.b, None)
        self.assert_drift(self.b, self.a, consumer_text="jobs: [unterminated\n")
        for side in ("consumer", "template"):
            with self.subTest(side=side):
                result = self.compare(self.b, self.a, escape=side)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_non_json_yaml_values_do_not_get_a_transition_or_traceback(self):
        text = yaml.safe_dump(self.a, sort_keys=False, allow_unicode=True)
        for suffix in ("unexpected: 2026-10-28\n", "unexpected: &loop {self: *loop}\n"):
            with self.subTest(suffix=suffix):
                result = self.compare(self.b, self.a, consumer_text=text + suffix)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn(NOTICE, result.stdout)

    def test_local_wrapper_and_workflow_context_have_same_verdict(self):
        changed = copy.deepcopy(self.a)
        changed["permissions"]["contents"] = "write"
        for template, consumer, expected in ((self.a, self.b, 0), (self.b, self.a, 0), (self.b, changed, 1)):
            for wrapper, actions in ((True, False), (False, True)):
                with self.subTest(wrapper=wrapper, actions=actions, expected=expected):
                    result = self.compare(template, consumer, wrapper=wrapper, actions=actions)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    if expected == 0:
                        self.assertIn(NOTICE, result.stdout)


if __name__ == "__main__":
    unittest.main()
