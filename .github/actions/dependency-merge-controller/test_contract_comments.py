"""Protect real legacy workflow bytes outside the explicit pin/version exception."""

from pathlib import Path
import unittest
from unittest import mock

import test_contract as contract


ROOT = Path(__file__).resolve().parents[3]
LEGACY = ROOT / ".github/workflows/auto-merge-deps.yml"


def action_line(document):
    """Select an actual pinned action without reproducing the normalizer grammar."""
    return next(line for line in document.splitlines(keepends=True)
                if b"uses: step-security/harden-runner@" in line)


def frozen_result(document):
    """Exercise the original complete-workflow golden assertion on supplied bytes."""
    result = unittest.TestResult()
    with mock.patch.object(contract.Path, "read_bytes", return_value=document):
        contract.ContractTest("test_legacy_reusable_is_unchanged").run(result)
    return result


class ContractCommentTest(unittest.TestCase):
    """Only exact pin/numeric-version edits may preserve the legacy fingerprint."""

    def assert_frozen_rejects(self, document):
        """Require the actual golden test to fail by assertion, never setup error."""
        result = frozen_result(document)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("AssertionError", result.failures[0][1])

    def test_real_workflow_accepts_only_pin_and_numeric_version_bumps(self):
        """The complete current golden survives legitimate finite version forms."""
        document = LEGACY.read_bytes()
        self.assertTrue(frozen_result(document).wasSuccessful())
        line = action_line(document)
        prefix = line.split(b"@", 1)[0] + b"@"
        for version in (b"", b" # v3", b" # v3.14", b" # v3.14.159"):
            changed = document.replace(line, prefix + b"a" * 40 + version + b"\n", 1)
            with self.subTest(version=version):
                self.assertEqual(contract.fingerprint(document), contract.fingerprint(changed))
                self.assertTrue(frozen_result(changed).wasSuccessful())

    def test_real_workflow_protects_inserted_replaced_and_appended_comments(self):
        """Scanner directives and all non-version text must trip the original freeze."""
        document = LEGACY.read_bytes()
        line = action_line(document)
        pinned = line.split(b" # ", 1)[0]
        comments = (b"zizmor: ignore[unpinned-uses]", b"actionlint: ignore=SC2086",
                    b"explanatory text", b"v2.22.3 # zizmor: ignore[unpinned-uses]",
                    b"v2.22.3 # actionlint: ignore=SC2086", b"v2.22.3 trailing text",
                    b"v2.22.3.4", b"v2.22.3-rc.1", b"v2.22.x", b" v2.22.3", b"v\xd9\xa0")
        for comment in comments:
            changed = document.replace(line, pinned + b" # " + comment + b"\n", 1)
            with self.subTest(comment=comment):
                self.assert_frozen_rejects(changed)
        inserted = document.replace(line, b"      # zizmor: ignore[unpinned-uses]\n" + line, 1)
        self.assert_frozen_rejects(inserted)

    def test_real_workflow_protects_refs_identity_logic_and_permissions(self):
        """The pin exception cannot absorb malformed refs or other workflow edits."""
        document = LEGACY.read_bytes()
        line = action_line(document)
        prefix = line.split(b"@", 1)[0] + b"@"
        for reference in (b"v2.22.3", b"a" * 39, b"a" * 41, b"g" * 40, b"A" * 40):
            changed = document.replace(line, prefix + reference + b" # v2.22.3\n", 1)
            with self.subTest(reference=reference):
                self.assert_frozen_rejects(changed)
        for before, after in ((b"step-security/harden-runner@", b"other-owner/harden-runner@"),
                              (b"step-security/harden-runner@", b"step-security/other-action@"),
                              (b"step-security/harden-runner@", b"step-security/harden-runner/sub@"),
                              (b"checks-timeout-minutes || 15", b"checks-timeout-minutes || 14"),
                              (b"\n      pull-requests: write\n", b"\n      pull-requests: read\n")):
            self.assertEqual(document.count(before), 1)
            with self.subTest(change=after):
                self.assert_frozen_rejects(document.replace(before, after, 1))

    def test_normalized_action_match_never_consumes_newline_or_control_indentation(self):
        """The explicit line exception is confined to horizontal whitespace."""
        document = LEGACY.read_bytes()
        line = action_line(document)
        separated = document.replace(line, b"\n" + line, 1)
        self.assert_frozen_rejects(separated)
        matches = list(contract.ACTION_PIN.finditer(separated))
        self.assertTrue(matches)
        self.assertTrue(all(b"\n" not in match.group() for match in matches))
        for prefix in (b"\v", b"\f", b"\r"):
            malformed = prefix + line.lstrip(b" \t")
            with self.subTest(prefix=prefix):
                self.assertIsNone(contract.ACTION_PIN.search(malformed))


if __name__ == "__main__":
    unittest.main()
