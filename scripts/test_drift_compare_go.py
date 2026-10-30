"""Adversarial CLI fixtures for the exact Go caller concurrency migration."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from drift_compare import GO_CONCURRENCY_VARIANTS, WorkflowLoader

ROOT = Path(__file__).resolve().parents[1]
GROUP = "${{ github.workflow }}-${{ github.event_name }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}"
CANCEL = "${{ github.event_name == 'pull_request' }}"
NOTICE = "Approved Go PR-concurrency transition"


def fingerprint(document):
    return hashlib.sha256((json.dumps(document, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False) + "\n").encode()).hexdigest()


class GoTransitionTest(unittest.TestCase):
    def documents(self, template, path):
        current = yaml.load((ROOT / "templates" / template / path).read_text(), Loader=WorkflowLoader)
        before = copy.deepcopy(current)
        before.pop("concurrency", None)
        if path.endswith("/mutation.yml"):
            before["concurrency"] = {
                "group": "mutation-${{ github.head_ref || github.ref }}", "cancel-in-progress": True}
        after = copy.deepcopy(before)
        after["concurrency"] = {"group": GROUP, "cancel-in-progress": CANCEL}
        return current, before, after

    def compare(self, template, path, canonical, consumer, *, text=None, escape=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            template_root, consumer_root = base / template, base / "consumer"
            for root, document in ((template_root, canonical), (consumer_root, consumer)):
                target = root / path
                target.parent.mkdir(parents=True)
                if document is not None:
                    target.write_text(yaml.safe_dump(document, sort_keys=False))
            target = consumer_root / path
            if text is not None:
                target.write_text(text)
            if escape:
                outside = base / "outside.yml"
                target.rename(outside)
                target.symlink_to(outside)
            return subprocess.run([sys.executable, str(ROOT / "scripts/drift_compare.py"),
                                   str(consumer_root), str(template_root)],
                                  capture_output=True, text=True, check=False)

    def test_frozen_documents_and_both_rollout_directions(self):
        for template in ("go-lib", "go-app"):
            for path, allowed in GO_CONCURRENCY_VARIANTS.items():
                current, before, after = self.documents(template, path)
                with self.subTest(template=template, path=path):
                    self.assertIn(fingerprint(current), allowed)
                    self.assertEqual({fingerprint(before), fingerprint(after)}, allowed)
                    for canonical, consumer in ((before, after), (after, before)):
                        result = self.compare(template, path, canonical, consumer)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn(NOTICE, result.stdout)
                    for document in (before, after):
                        result = self.compare(template, path, document, document)
                        self.assertEqual(result.returncode, 0)
                        self.assertNotIn(NOTICE, result.stdout)

    def test_changed_jobs_inputs_permissions_triggers_and_concurrency_still_fail(self):
        for path in GO_CONCURRENCY_VARIANTS:
            _, before, after = self.documents("go-lib", path)
            changes = []
            for key in before["jobs"]:
                changed = copy.deepcopy(after)
                del changed["jobs"][key]
                changes.append(changed)
                for field, value in (("uses", "attacker/action@main"), ("if", "false"),
                                     ("with", {"enable-gosec": False}),
                                     ("permissions", {"contents": "write"})):
                    changed = copy.deepcopy(after)
                    changed["jobs"][key][field] = value
                    changes.append(changed)
            for field, value in (("permissions", {"contents": "write"}), ("on", {}),
                                 ("env", {"SKIP_TESTS": "true"}),
                                 ("concurrency", {"group": GROUP, "cancel-in-progress": True}),
                                 ("concurrency", {"group": "${{ github.ref }}", "cancel-in-progress": CANCEL})):
                changed = copy.deepcopy(after)
                changed[field] = value
                changes.append(changed)
            for changed in changes:
                with self.subTest(path=path, fingerprint=fingerprint(changed)):
                    result = self.compare("go-lib", path, before, changed)
                    self.assertEqual(result.returncode, 1)
                    self.assertNotIn(NOTICE, result.stdout)
                    # A future canonical document cannot inherit obsolete variants.
                    self.assertEqual(self.compare("go-lib", path, changed, before).returncode, 1)
                    self.assertEqual(self.compare("go-lib", path, changed, after).returncode, 1)

    def test_exact_template_path_and_readable_documents_required(self):
        path = ".github/workflows/ci.yml"
        _, before, after = self.documents("go-lib", path)
        for template, other_path in (("typo3-extension", path), ("skill", path),
                                     ("go-lib", ".github/workflows/ci.yaml"),
                                     ("go-app", ".github/workflows/other.yml")):
            self.assertEqual(self.compare(template, other_path, before, after).returncode, 1)
        self.assertEqual(self.compare("go-lib", path, before, None).returncode, 1)
        self.assertEqual(self.compare("go-lib", path, before, after, text="jobs: [broken\n").returncode, 1)
        self.assertEqual(self.compare("go-lib", path, before, after, escape=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
