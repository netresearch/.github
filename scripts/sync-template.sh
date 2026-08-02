#!/usr/bin/env bash
# Sync a consuming repo with the canonical template.
#
# Usage:
#   scripts/sync-template.sh <template> <owner/repo> [--pr|--no-pr] [--branch <name>]
#
# Templates: go-app | go-lib | typo3-extension | skill | php-module
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
  go-app|go-lib|typo3-extension|skill|php-module) ;;
  *) echo "unknown template: $TEMPLATE (want go-app|go-lib|typo3-extension|skill|php-module)" >&2; exit 2 ;;
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
# step can skip them.
#
# A MISSING template.yaml means no drift is declared (e.g. the first sync) and
# an unconstrained copy is correct. But a template.yaml that EXISTS and cannot
# be read — PyYAML not installed, or the file is malformed — must be a HARD
# FAILURE: proceeding would silently clobber the very files the repo declared
# as intentional drift (its per-extension ci.yml matrix, release.yml values,
# …). Stdout carries only the paths; diagnostics go to stderr and never leak
# into DRIFT_PATHS (the old code merged 2>&1 and mapfile'd Python tracebacks
# as if they were paths).
DRIFT_PATHS=()
if [ -f .github/template.yaml ]; then
  DRIFT_ERR_FILE=$(mktemp)
  if ! DRIFT_OUT=$(python3 - .github/template.yaml 2>"$DRIFT_ERR_FILE" <<'PY'
import sys
try:
    import yaml
except ModuleNotFoundError:
    sys.stderr.write("PyYAML is not installed; cannot read intentional-drift.\n")
    sys.exit(3)
try:
    with open(sys.argv[1]) as f:
        doc = yaml.safe_load(f) or {}
except Exception as e:  # noqa: BLE001 - any parse error is fatal here
    sys.stderr.write(f"malformed template.yaml: {e}\n")
    sys.exit(3)
entries = doc.get("intentional-drift") or []
if not isinstance(entries, list):
    sys.stderr.write(
        f"intentional-drift must be a list, got {type(entries).__name__}\n"
    )
    sys.exit(3)
for item in entries:
    if isinstance(item, dict) and item.get("path"):
        print(item["path"])
    elif isinstance(item, str):
        print(item)
PY
  ); then
    DRIFT_ERR=$(cat "$DRIFT_ERR_FILE")
    rm -f "$DRIFT_ERR_FILE"
    echo "::error::Refusing to sync $TARGET: .github/template.yaml exists but its intentional-drift list could not be read (${DRIFT_ERR:-no diagnostic}). Proceeding would overwrite files this repo declared as intentional drift. Install PyYAML (pip install pyyaml) or fix the file, then re-run." >&2
    exit 3
  fi
  rm -f "$DRIFT_ERR_FILE"
  # Keep only non-empty lines; DRIFT_OUT is paths-only (stderr was separate).
  while IFS= read -r line; do
    [ -n "$line" ] && DRIFT_PATHS+=("$line")
  done <<< "$DRIFT_OUT"
fi

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
