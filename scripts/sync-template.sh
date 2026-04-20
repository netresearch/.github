#!/usr/bin/env bash
# Sync a consuming repo with the canonical template.
#
# Usage:
#   scripts/sync-template.sh <template> <owner/repo> [--pr|--no-pr] [--branch <name>]
#
# Templates: go-app | go-lib
#
# Behavior:
#   1. Clones <owner/repo> via SSH into a temp worktree.
#   2. Creates a branch (default: sync/template-<template>-<timestamp>).
#   3. Copies templates/<template>/.github/ into the target, overwriting
#      matching files. template.yaml is ONLY created on first sync (never
#      overwritten — it carries per-repo intentional-drift state).
#   4. Commits (signed, DCO sign-off) if there are changes.
#   5. Pushes the branch and opens a PR via gh (default; --no-pr to skip).
#
# Requires: git, gh, bash 4+, ssh access to the target repo.

set -euo pipefail

TEMPLATE="${1:?template required: go-app or go-lib}"
TARGET="${2:?owner/repo required}"
shift 2 || true

MODE="pr"
BRANCH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pr) MODE="pr"; shift ;;
    --no-pr) MODE="no-pr"; shift ;;
    --branch) BRANCH="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "$TEMPLATE" in
  go-app|go-lib) ;;
  *) echo "unknown template: $TEMPLATE (want go-app|go-lib)" >&2; exit 2 ;;
esac

# Resolve script dir → project root (templates/ lives here).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$PROJECT_ROOT/templates/$TEMPLATE/.github"

if [ ! -d "$TEMPLATE_DIR" ]; then
  echo "::error::template dir not found: $TEMPLATE_DIR" >&2
  exit 1
fi

BRANCH_WAS_SUPPLIED=0
if [ -n "$BRANCH" ]; then
  BRANCH_WAS_SUPPLIED=1
else
  BRANCH="sync/template-${TEMPLATE}-$(date +%Y%m%d-%H%M%S)"
fi

WORKTREE=$(mktemp -d -t sync-template-XXXXXX)
trap 'rm -rf "$WORKTREE"' EXIT

echo "[$TARGET] cloning…"
git clone --quiet "git@github.com:${TARGET}.git" "$WORKTREE/consumer"
cd "$WORKTREE/consumer"

DEFAULT_BRANCH=$(gh api "repos/${TARGET}" --jq '.default_branch')
git checkout "$DEFAULT_BRANCH" --quiet
git pull --quiet

# Handle pre-existing remote branch. For auto-generated branch names
# (no --branch flag), we can safely delete because the timestamp suffix
# makes collisions self-inflicted. For user-supplied names, refuse to
# destroy — the user may have in-flight work on that branch.
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  if [ "$BRANCH_WAS_SUPPLIED" = "1" ]; then
    echo "::error::Branch '$BRANCH' already exists on origin. Delete it manually or use a different --branch name; refusing to overwrite user-supplied ref." >&2
    exit 1
  fi
  echo "note: deleting stale auto-generated branch '$BRANCH' from origin."
  git push origin --delete "$BRANCH" >/dev/null 2>&1 || true
fi

git checkout -b "$BRANCH" --quiet

# Copy every file from the template's .github/ tree into the consumer,
# except files the consumer has explicitly flagged as intentional-drift.
# That flag lives in .github/template.yaml's intentional-drift[].path list.
# template.yaml itself is never overwritten — it carries per-repo drift state.
EXISTING_TEMPLATE_YAML=""
if [ -f .github/template.yaml ]; then
  EXISTING_TEMPLATE_YAML=$(cat .github/template.yaml)
fi

# Resolve intentional-drift paths (relative to consumer root) so the copy
# step can skip them. Tolerate missing/malformed template.yaml — the copy
# proceeds as an unconstrained sync in that case.
mapfile -t DRIFT_PATHS < <(python3 -c '
import sys, yaml
try:
    with open(".github/template.yaml") as f:
        doc = yaml.safe_load(f) or {}
except FileNotFoundError:
    sys.exit(0)
except Exception as e:
    sys.stderr.write(f"warning: could not parse .github/template.yaml: {e}\n")
    sys.exit(0)
for item in (doc.get("intentional-drift") or []):
    if isinstance(item, dict) and item.get("path"):
        print(item["path"])
    elif isinstance(item, str):
        print(item)
' 2>&1 | grep -v "^warning:" || true)

mkdir -p .github

if [ ${#DRIFT_PATHS[@]} -eq 0 ]; then
  # Fast path: no drift — plain recursive copy.
  cp -a "$TEMPLATE_DIR/." .github/
else
  echo "preserving ${#DRIFT_PATHS[@]} intentional-drift path(s):"
  printf "  - %s\n" "${DRIFT_PATHS[@]}"
  # Build rsync exclude pattern file. rsync patterns are relative to the
  # transfer root (.github/), but intentional-drift paths are relative to
  # repo root — strip the `.github/` prefix.
  EXCLUDES=$(mktemp)
  for p in "${DRIFT_PATHS[@]}"; do
    case "$p" in
      .github/*) echo "${p#.github/}" >> "$EXCLUDES" ;;
      *)         echo "$p"           >> "$EXCLUDES" ;;
    esac
  done
  # -a archive; -v verbose (echoes in $CLONE log); --exclude-from skips drifted files.
  rsync -a --exclude-from="$EXCLUDES" "$TEMPLATE_DIR/" .github/
  rm -f "$EXCLUDES"
fi

if [ -n "$EXISTING_TEMPLATE_YAML" ]; then
  printf "%s\n" "$EXISTING_TEMPLATE_YAML" > .github/template.yaml
  echo "note: preserved existing .github/template.yaml (intentional-drift state)."
fi

# `git diff` only notices tracked-file changes; new files brought in
# by the template (e.g. a new codecov.yml) stay untracked and would
# otherwise be missed. Use `git status --porcelain` to detect ANY
# change, then `git add -A` picks them all up.
if [ -z "$(git status --porcelain)" ]; then
  echo "[$TARGET] no changes vs template — already in sync."
  exit 0
fi

git add -A
git commit -S --signoff -m "ci: sync with netresearch/.github templates/$TEMPLATE

Auto-generated by scripts/sync-template.sh. Any changes you want to keep
must be declared in .github/template.yaml's intentional-drift: list — the
check-template-drift.yml job will otherwise revert them on next sync."

git push --set-upstream origin "$BRANCH"

if [ "$MODE" = "pr" ]; then
  gh pr create \
    --title "ci: sync with netresearch/.github templates/$TEMPLATE" \
    --body "Auto-opened by sync-template.sh. Brings this repo back into alignment with the canonical \`$TEMPLATE\` template in \`netresearch/.github\`.

To keep any diverging files, add their paths to \`.github/template.yaml\`'s \`intentional-drift:\` list before merging — otherwise the next sync run will revert them."
fi

echo "[$TARGET] sync complete on branch $BRANCH"
