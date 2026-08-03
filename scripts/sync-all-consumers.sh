#!/usr/bin/env bash
# Run sync-template.sh across every consumer repo.
#
# The consumer list is discovered, not written down: scripts/list-consumers.py
# asks the organisation which repositories carry .github/template.yaml. It used
# to be a hardcoded map here that had to be kept in step with drift-scan.yml's
# matrix by hand, and both had drifted to six Go repositories while the fleet
# had grown to 57 across five templates.
#
# Usage:
#   sync-all-consumers.sh [--template <name>] [--dry-run] [-- <sync-template.sh args>]
#
#   --template <name>   only consumers of that template (go-app, skill, …)
#   --dry-run           list what would be synced, sync nothing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILTER=""
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --template) TEMPLATE_FILTER="${2:?--template needs a name}"; shift 2 ;;
    --dry-run)  DRY=1; shift ;;
    --)         shift; break ;;
    *)          break ;;
  esac
done

# The exit status has to be captured, not inferred: a failure inside a process
# substitution does not reach `set -e`, so `mapfile < <(...)` would leave ROWS
# empty and the run would report "Consumers: 0" and finish successfully having
# synced nothing.
if ! DISCOVERED=$(python3 "$SCRIPT_DIR/list-consumers.py" \
      ${TEMPLATE_FILTER:+--template "$TEMPLATE_FILTER"}); then
  echo "sync-all-consumers: consumer discovery failed — refusing to sync" >&2
  exit 1
fi
mapfile -t ROWS <<<"$DISCOVERED"
if [ "${#ROWS[@]}" -eq 0 ] || [ -z "${ROWS[0]}" ]; then
  echo "sync-all-consumers: no consumers discovered — refusing to sync" >&2
  exit 1
fi

echo "Consumers: ${#ROWS[@]}${TEMPLATE_FILTER:+ (template: $TEMPLATE_FILTER)}"
echo

for row in "${ROWS[@]}"; do
  target="${row%%$'\t'*}"
  template="${row##*$'\t'}"
  echo "=== $target → $template ==="
  if [ "$DRY" = "1" ]; then
    echo "(dry run)"
  else
    bash "$SCRIPT_DIR/sync-template.sh" "$template" "$target" "$@"
  fi
  echo
done
