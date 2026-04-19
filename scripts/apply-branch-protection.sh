#!/usr/bin/env bash
# Mark the template drift check as a required status check on each
# consumer repo's default branch.
#
# Idempotent: re-running preserves existing required checks, only adds
# the drift-check context if absent.

set -euo pipefail

DRIFT_CONTEXT="Template Drift / drift"

declare -A CONSUMERS=(
  [netresearch/ofelia]=go-app
  [netresearch/ldap-manager]=go-app
  [netresearch/ldap-selfservice-password-changer]=go-app
  [netresearch/raybeam]=go-app
  [netresearch/simple-ldap-go]=go-lib
  [netresearch/go-cron]=go-lib
)

for target in "${!CONSUMERS[@]}"; do
  echo "=== $target ==="
  DEFAULT_BRANCH=$(gh api "repos/$target" --jq '.default_branch')

  # Fetch current required status check contexts.
  EXISTING=$(gh api "repos/$target/branches/$DEFAULT_BRANCH/protection/required_status_checks/contexts" \
    2>/dev/null | jq -r '.[]' || true)

  if echo "$EXISTING" | grep -Fxq "$DRIFT_CONTEXT"; then
    echo "  '$DRIFT_CONTEXT' already required — skipping."
    continue
  fi

  # Add the context using the POST /contexts endpoint (appends).
  gh api --method POST \
    "repos/$target/branches/$DEFAULT_BRANCH/protection/required_status_checks/contexts" \
    --input - <<JSON
{"contexts":["$DRIFT_CONTEXT"]}
JSON
  echo "  added '$DRIFT_CONTEXT' to required status checks."
done
