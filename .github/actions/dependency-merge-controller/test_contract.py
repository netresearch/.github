"""Freeze the legacy entrypoint while introducing the separate opt-in contract."""

import hashlib
from pathlib import Path
import unittest


class ContractTest(unittest.TestCase):
    # Pins auto-merge-deps.yml, so a change to it is a deliberate one. The
    # hash was last updated for the superseded-run filter of #561; its
    # caller permission contract is checked by test-permissions.sh.
    def test_legacy_reusable_is_byte_identical(self):
        root = Path(__file__).resolve().parents[3]
        legacy = root / ".github/workflows/auto-merge-deps.yml"
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(),
                         "1c33d4dfd21d6fe5ef7db3c2f2a7cb8bf0f477442e70ecc3b766010273b74419")


if __name__ == "__main__":
    unittest.main()
