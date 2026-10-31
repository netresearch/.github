"""Freeze the legacy entrypoint while introducing the separate opt-in contract."""

import hashlib
from pathlib import Path
import re
import unittest

# A pinned action reference, `uses: owner/repo@<40-hex sha> # vX.Y.Z`. Renovate
# rewrites these lines on every action release, which is not a change to the
# legacy gate this test freezes.
ACTION_PIN = re.compile(rb"^(\s*(?:- )?uses: [\w.-]+/[\w./-]+@)[0-9a-f]{40}(?: #[^\n]*)?$", re.MULTILINE)


def fingerprint(text):
    return hashlib.sha256(ACTION_PIN.sub(rb"\1<pin>", text)).hexdigest()


class ContractTest(unittest.TestCase):
    # Pins auto-merge-deps.yml, so a change to it is a deliberate one. The
    # fingerprint was last updated for the superseded-run filter of #561;
    # action pin bumps are left out of it. The caller permission contract is
    # checked by test-permissions.sh.
    def test_legacy_reusable_is_unchanged(self):
        root = Path(__file__).resolve().parents[3]
        legacy = root / ".github/workflows/auto-merge-deps.yml"
        self.assertEqual(fingerprint(legacy.read_bytes()),
                         "f91477280c81632e3f98d7dde461a4768a822d9e2b6d694bc03b6e96ce572110")

    def test_fingerprint_ignores_only_action_pins(self):
        line = b"        uses: step-security/harden-runner@" + b"a" * 40 + b" # v2.22.2\n"
        bumped = b"        uses: step-security/harden-runner@" + b"b" * 40 + b" # v2.22.3\n"
        self.assertEqual(fingerprint(line), fingerprint(bumped))
        self.assertNotEqual(fingerprint(line), fingerprint(line.replace(b"harden-runner", b"other-action")))
        self.assertNotEqual(fingerprint(b"run: echo 1\n"), fingerprint(b"run: echo 2\n"))
        step = b"      - uses: actions/checkout@" + b"c" * 40 + b" # v7.0.1\n"
        self.assertEqual(fingerprint(step), fingerprint(step.replace(b"c" * 40, b"d" * 40)))


if __name__ == "__main__":
    unittest.main()
