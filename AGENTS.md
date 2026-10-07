# Agent Guide — netresearch/.github

Organization-level community health files, reusable GitHub Actions workflows,
and the repository templates under `templates/` that consumer repos are kept in
sync with: `go-app`, `go-lib`, `php-module`, `skill` and `typo3-extension`.

## Template-drift sync

Each template's `templates/<template>/.github/` tree is the source of truth for
the `.github/` tree of the repositories that consume it. A repository declares
itself a consumer by carrying `.github/template.yaml` (`template: <name>`);
`scripts/list-consumers.py` discovers the consumers from the organisation, so
there is no hand-written consumer list. Drift between a consumer and its
template is detected and reconciled by dedicated tooling:

- **Enforce:** each consumer runs `check-template-drift.yml` on its pull
  requests and pushes; it fails when a governed file differs from the template.
  YAML files are compared as parsed documents (`scripts/drift_compare.py`), so a
  change that only touches comments is reported as cosmetic, not as drift.
- **Detect:** `.github/workflows/drift-scan.yml` runs weekly (Mon 06:00 UTC) and
  on `workflow_dispatch` (with an optional `template:` input that limits the
  scan to one template's consumers). It auto-opens
  `Template drift: <repo> vs <template>` issues and auto-closes them once drift
  is gone — so after merging fixes, dispatch the scan to close immediately
  instead of waiting for the next schedule.
- **Fix:** `scripts/sync-template.sh <template> netresearch/<repo>`
  SSH-clones the consumer, copies the template `.github/` tree, commits with
  `-S --signoff`, pushes a `sync/...` branch, and opens a PR. Only drifting
  files change. `.github/template.yaml` is created on first sync only and never
  overwritten — it carries each repo's `intentional-drift:` state.
  `scripts/sync-all-consumers.sh [--template <name>] [--dry-run]` runs it for
  every consumer.
- **Scope:** templates carry only what every consumer of that template needs.
  Dependency updates come from Renovate; the templates ship no
  `dependabot.yml`. A consumer that needs a file to differ from its template
  lists the path under `intentional-drift`.

## Reusable workflows

The reusable workflows under `.github/workflows/*.yml` are consumed across the org. Reference convention:

- **Intra-org callers reference these by `@main` (or a published `@vN` interface tag), never a full-length SHA.** These workflows live in this same org and are kept correct here; SHA-pinning freezes callers on a stale revision and creates per-repo Renovate churn for no security gain. Example: `uses: netresearch/.github/.github/workflows/go-check.yml@main` — note the doubled `.github/.github`, since the org repo is literally named `.github`.
- SonarCloud's "pin GitHub Actions to a full-length commit SHA" hotspot is **advisory and wrong for first-party intra-org reusables** — mark it *Safe* rather than SHA-pinning. (SHA-pinning is still correct for third-party, non-org actions.)
- **Third-party actions inside these workflows are SHA-pinned**, so foreign code is pinned where it actually enters: `step-security/harden-runner`, `actions/checkout`, `actions/setup-go`, `oven-sh/setup-bun`, `codecov/codecov-action`, `golangci/golangci-lint-action`, `securego/gosec`, `github/codeql-action` — every one carries a full SHA plus a version comment. The `@main` above applies to first-party reusables only.
- **The distinction is enforced, not just documented**: `.github/zizmor.yml` sets `unpinned-uses` policies `netresearch/* -> ref-pin` and `* -> hash-pin`, and the shared `zizmor.yml` workflow installs that config in any consumer repo that carries none. An unpinned third-party `uses:` therefore still fails an audit.
- Scanners that cannot express that distinction produce first-party noise, and are handled centrally rather than per repo:
  - **CodeQL** `actions/unpinned-tag` — excluded via `query-filters` in the shared `codeql.yml`, with zizmor as the compensating control.
  - **OpenSSF Scorecard** `Pinned-Dependencies` — the action has no per-check opt-out, so the alerts are dismissed as *won't fix* with this section as the reason. The score is affected; that is the accepted cost.
