# Agent Guide — netresearch/.github

Organization-level community health files, reusable GitHub Actions workflows,
and the canonical Go repository templates (`templates/go-app`, `templates/go-lib`)
that consumer repos are kept in sync with.

## Template-drift sync

The `go-app` and `go-lib` templates under `templates/` are the source of truth
for each consumer repo's `.github/` tree. Drift between a consumer and its
template is detected and reconciled by dedicated tooling:

- **Detect:** `.github/workflows/drift-scan.yml` runs weekly (Mon 06:00 UTC) and
  on `workflow_dispatch` (with an optional space-separated `repos:` input). It
  auto-opens "Template drift: \<repo\> vs \<go-app|go-lib\>" issues and
  auto-closes them once drift is gone — so after merging fixes, dispatch the
  scan to close immediately instead of waiting for the next schedule.
- **Fix:** `scripts/sync-template.sh <go-app|go-lib> netresearch/<repo>`
  SSH-clones the consumer, copies the template `.github/` tree, commits with
  `-S --signoff`, pushes a `sync/...` branch, and opens a PR. Only drifting
  files change. `template.yaml` is created on first sync only and never
  overwritten — it carries each repo's `intentional-drift:` state.
- **Scope:** keep templates trimmed to a universal core (gomod + github-actions);
  per-repo extras (docker, npm, devcontainers) are opt-in via a self-managed
  `dependabot.yml` plus `intentional-drift`, because not every consumer has
  those manifests and an undeclared ecosystem fails Dependabot with
  `dependency_file_not_found`.
