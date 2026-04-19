#!/usr/bin/env bash
# Compare a consumer repo's .github/ tree against a template.
#
# Usage:
#   check-drift.sh <consumer-dir> <template-dir>
#
# Exits 1 on drift, 0 otherwise. Prints missing/differing file lists and
# unified diffs to stderr.
#
# Intentional-drift is read from <consumer-dir>/.github/template.yaml's
# `intentional-drift:` list (each entry either a string path or a mapping
# with a `path:` key).

set -euo pipefail

CONSUMER_ROOT="${1:?consumer dir required}"
TEMPLATE_ROOT="${2:?template dir required}"

if [ ! -d "$TEMPLATE_ROOT" ]; then
  echo "::error::template dir $TEMPLATE_ROOT does not exist" >&2
  exit 2
fi

DRIFT_FILE="$CONSUMER_ROOT/.github/template.yaml"
INTENTIONAL=()
if [ -f "$DRIFT_FILE" ]; then
  mapfile -t INTENTIONAL < <(python3 -c '
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        doc = yaml.safe_load(f) or {}
except Exception as e:
    sys.stderr.write(f"failed to parse template.yaml: {e}\n"); sys.exit(2)
for item in (doc.get("intentional-drift") or []):
    if isinstance(item, dict) and item.get("path"):
        print(item["path"])
    elif isinstance(item, str):
        print(item)
' "$DRIFT_FILE") || true
fi

MISSING=()
DIFFERING=()

while IFS= read -r -d '' tpl_file; do
  rel="${tpl_file#"$TEMPLATE_ROOT"/}"

  # .github/template.yaml is per-consumer metadata, never in the diff set.
  if [ "$rel" = ".github/template.yaml" ]; then
    continue
  fi

  skip=0
  for entry in "${INTENTIONAL[@]:-}"; do
    if [ "$entry" = "$rel" ]; then skip=1; break; fi
  done
  if [ "$skip" = "1" ]; then continue; fi

  consumer_path="$CONSUMER_ROOT/$rel"
  if [ ! -f "$consumer_path" ]; then
    MISSING+=("$rel"); continue
  fi
  if ! diff -q "$tpl_file" "$consumer_path" >/dev/null 2>&1; then
    DIFFERING+=("$rel")
  fi
done < <(find "$TEMPLATE_ROOT" -type f -print0)

if [ ${#MISSING[@]} -eq 0 ] && [ ${#DIFFERING[@]} -eq 0 ]; then
  echo "No drift."
  exit 0
fi

{
  if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Missing files (template has them, consumer does not):"
    printf '  - %s\n' "${MISSING[@]}"
    echo ""
  fi
  if [ ${#DIFFERING[@]} -gt 0 ]; then
    echo "Differing files:"
    printf '  - %s\n' "${DIFFERING[@]}"
    echo ""
    for f in "${DIFFERING[@]}"; do
      echo "=== diff: $f ==="
      diff -u "$CONSUMER_ROOT/$f" "$TEMPLATE_ROOT/$f" || true
    done
  fi
} >&2

exit 1
