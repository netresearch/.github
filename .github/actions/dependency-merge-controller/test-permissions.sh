#!/usr/bin/env bash
# Check actual parsed reusable permission contracts against consumer grants.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
legacy=$(yq -o=json '.jobs.auto-merge.permissions' "$root/.github/workflows/auto-merge-deps.yml")
required=$(yq -o=json '.jobs.controller.permissions' "$root/.github/workflows/dependency-merge-controller.yml")
provided='{"contents":"write","pull-requests":"write","actions":"read","checks":"read","statuses":"read"}'

allows() {
  local needed="$1" provided="$2"
  jq -en --argjson need "$needed" --argjson have "$provided" '
    all($need | to_entries[];
      . as $scope | ($have[$scope.key] == $scope.value) or
      ($scope.value == "read" and $have[$scope.key] == "write"))' >/dev/null
}

allows "$legacy" '{"contents":"write","pull-requests":"write"}'
allows "$required" "$provided"
missing=$(jq 'del(.actions)' <<<"$provided")
if allows "$required" "$missing"; then
  echo 'A caller missing actions:read unexpectedly satisfies the new contract' >&2
  exit 1
fi
if allows "$required" "$legacy"; then
  echo 'The old permission set unexpectedly satisfies the opt-in contract' >&2
  exit 1
fi
echo 'Legacy, opted-in and missing-actions permission fixtures passed'
