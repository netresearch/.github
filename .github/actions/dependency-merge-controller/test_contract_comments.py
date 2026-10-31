"""Protect real legacy workflow bytes outside the explicit pin/version exception."""

import hashlib
from pathlib import Path
import struct
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

    def test_real_workflow_rejects_literal_markers_and_frame_looking_raw_bytes(self):
        """Unmatched input cannot impersonate a normalized pin at any real action."""
        document = LEGACY.read_bytes()
        lines = [line for line in document.splitlines(keepends=True)
                 if contract.ACTION_PIN.fullmatch(line.rstrip(b"\n"))]
        self.assertEqual(len(lines), 3)
        for line in lines:
            prefix = line.split(b"@", 1)[0] + b"@"
            for reference in (b"<pin>", b"<pin> # v2.22.3"):
                changed = document.replace(line, prefix + reference + b"\n", 1)
                with self.subTest(action=prefix, reference=reference):
                    self.assertNotEqual(contract.fingerprint(document), contract.fingerprint(changed))
                    self.assert_frozen_rejects(changed)
        for raw in (b"<pin>", b"U" + b"\0" * 8, b"P" + struct.pack(">Q", 5) + b"<pin>",
                    b"U" + struct.pack(">Q", 1) + b"P\0U\xff"):
            with self.subTest(raw=raw):
                self.assert_frozen_rejects(document + raw)

    def test_canonical_frames_preserve_tags_lengths_empty_segments_and_tail(self):
        """Independent binary records distinguish raw bytes from matched prefixes."""
        for payload in (b"", b"P", b"\0U\xff<pin>", b"a" * 255, b"b" * 256):
            expected = hashlib.sha256(struct.pack(">cQ", b"U", len(payload)) + payload).hexdigest()
            with self.subTest(length=len(payload)):
                self.assertEqual(contract.fingerprint(payload), expected)
        prefix = b"        uses: step-security/harden-runner@"
        line = prefix + b"a" * 40 + b" # v2.22.3\n"
        expected = b"U" + b"\0" * 8
        expected += struct.pack(">cQ", b"P", len(prefix)) + prefix
        expected += b"U" + struct.pack(">Q", 1) + b"\n"
        self.assertEqual(contract.fingerprint(line), hashlib.sha256(expected).hexdigest())
        empty_tail = struct.pack(">cQ", b"U", 0)
        empty_tail += struct.pack(">cQ", b"P", len(prefix)) + prefix
        empty_tail += struct.pack(">cQ", b"U", 0)
        self.assertEqual(contract.fingerprint(line.rstrip(b"\n")), hashlib.sha256(empty_tail).hexdigest())


if __name__ == "__main__":
    unittest.main()
