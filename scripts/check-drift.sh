#!/usr/bin/env bash
# Compare a consumer repo's .github/ tree against a template.
#
# Usage:
#   check-drift.sh <consumer-dir> <template-dir>
#
# Exits 1 on drift, 0 otherwise. Prints missing/differing file lists and
# unified diffs to stderr.
#
# Thin wrapper around scripts/drift_compare.py, which the reusable
# .github/workflows/check-template-drift.yml also calls — one implementation,
# so a local verdict and the CI verdict cannot disagree. See that file for the
# comparison rules (YAML compared as parsed documents, non-YAML byte-for-byte)
# and for how intentional-drift is read.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/drift_compare.py" \
  "${1:?consumer dir required}" \
  "${2:?template dir required}"
