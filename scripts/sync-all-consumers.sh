#!/usr/bin/env bash
# Run sync-template.sh across every known consumer repo.
#
# Consumer list is hardcoded here and must match drift-scan.yml's matrix.
# Edit both when a repo is added/removed from the fleet.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

declare -A CONSUMERS=(
  [netresearch/ofelia]=go-app
  [netresearch/ldap-manager]=go-app
  [netresearch/ldap-selfservice-password-changer]=go-app
  [netresearch/raybeam]=go-app
  [netresearch/simple-ldap-go]=go-lib
  [netresearch/go-cron]=go-lib
)

for target in "${!CONSUMERS[@]}"; do
  template="${CONSUMERS[$target]}"
  echo "=== $target → $template ==="
  bash "$SCRIPT_DIR/sync-template.sh" "$template" "$target" "$@"
  echo
done
