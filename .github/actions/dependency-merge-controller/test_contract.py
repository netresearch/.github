"""Freeze the legacy entrypoint while introducing the separate opt-in contract."""

import hashlib
from pathlib import Path
import unittest


class ContractTest(unittest.TestCase):
    def test_legacy_reusable_is_byte_identical(self):
        root = Path(__file__).resolve().parents[3]
        legacy = root / ".github/workflows/auto-merge-deps.yml"
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(),
                         "94e76bb4e90a8479c178339ba85d40b742e86be8e97bd42ba2a10a4372636dce")


if __name__ == "__main__":
    unittest.main()
