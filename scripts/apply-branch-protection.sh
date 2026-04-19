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

  # Fetch current required status check contexts. Surface unexpected
  # errors instead of silently treating every failure as "no contexts".
  TMP_ERR=$(mktemp)
  if EXISTING=$(gh api "repos/$target/branches/$DEFAULT_BRANCH/protection/required_status_checks/contexts" 2>"$TMP_ERR"); then
    EXISTING=$(echo "$EXISTING" | jq -r '.[]')
  else
    # Branch protection may lack required_status_checks entirely, which
    # returns HTTP 404 on this endpoint. That's an expected empty-set
    # case; any other error is surfaced.
    if grep -q '"status": "404"\|HTTP 404' "$TMP_ERR"; then
      EXISTING=""
    else
      echo "::error::Failed to read required status checks for $target:" >&2
      cat "$TMP_ERR" >&2
      rm -f "$TMP_ERR"
      exit 1
    fi
  fi
  rm -f "$TMP_ERR"

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
