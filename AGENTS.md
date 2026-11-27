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
  change that only touches comments is reported as cosmetic, not as drift. A
  workflow file that differs only in the full commit SHA of the same action, in
  a job-level or step-level `uses:`, gets its own notice and does not fail
  either. Tags, branches, short SHAs and any further difference still fail.
  That SHA is checked online (`scripts/pin_reachability.py`) with the job's
  token, because GitHub serves a fork's commit through the parent repository: a
  commit in no branch or tag of the action's repository fails the check, and a
  pin that could not be checked gets a `was not verified` warning instead of the
  notice. The check is a treeless clone of the action and a listing of its
  commits; do not replace it with the compare API or a lookup of the SHA, for the
  reasons in that file. `scripts/check-drift.sh` checks only with `GITHUB_TOKEN`
  or `GH_TOKEN` set; without one it fetches nothing and warns the same way.
- **Detect:** `.github/workflows/drift-scan.yml` runs weekly (Mon 06:00 UTC) and
  on `workflow_dispatch` (with an optional `template:` input that limits the
  scan to one template's consumers). It auto-opens
  `Template drift: <repo> vs <template>` issues and auto-closes them once drift
  is gone — so after merging fixes, dispatch the scan to close immediately
  instead of waiting for the next schedule.
- **Fix:** `scripts/sync-template.sh <template> netresearch/<repo>`
  SSH-clones the consumer, copies the template `.github/` tree, commits with
  `-S --signoff`, pushes a `sync/...` branch, and opens a draft PR
  (`--no-draft` for a ready one; `--trailer "Key: Value"`, repeatable, adds
  commit trailers). Only drifting
  files change. `.github/template.yaml` is created on first sync only and never
  overwritten — it carries each repo's `intentional-drift:` state.
  `scripts/sync-all-consumers.sh [--template <name>] [--dry-run]` runs it for
  every consumer.
- **Scope:** templates carry only what every consumer of that template needs.
  Dependency updates come from Renovate; the templates ship no
  `dependabot.yml`. A consumer that needs a file to differ from its template
  lists the path under `intentional-drift`. Before adding a file to a
  template, check whether the tool that reads it can fetch it from a central
  place at run time: the `zizmor.yml` reusable installs this repository's
  `.github/zizmor.yml` in any consumer that has none, so that config left the
  templates. A file the tool can fetch does not belong in every consumer, and
  prose that has to be authoritative belongs in the reusable it documents,
  where it exists once.

## Stuck Renovate pull requests

Renovate merges only green pull requests; a red one stays open and notifies
no one. `.github/workflows/renovate-stuck.yml` runs daily (06:30 UTC) and on
`workflow_dispatch` (with an optional `days:` input, default 3). It runs
`scripts/renovate-stuck.py`, which opens one `Renovate stuck: <owner/repo>`
issue per repository whose Renovate pull requests have been open longer than
`days` with failing checks, lists each pull request with its failing checks,
updates the issue on later runs and closes it once nothing in the repository
is stuck. `scripts/renovate-stuck.py --dry-run` prints the planned actions
without writing. `GITHUB_TOKEN` reads public repositories only, so the one
private repository is not scanned.


## Failing scheduled workflows

A scheduled run has no pull request and no author, so a red one notifies no
one. `.github/workflows/scheduled-failing.yml` runs daily (06:45 UTC) and on
`workflow_dispatch` (with an optional `runs:` input, default 2). It runs
`scripts/scheduled-failing.py`, which opens one `Scheduled runs failing:
<owner/repo>` issue per repository in which the last `runs` scheduled runs of
a workflow, within 30 days, all ended in failure, cancelled, timed_out or
startup_failure. It updates the issue on later runs and closes it once no
workflow in the repository is failing. `scripts/scheduled-failing.py
--dry-run` prints the planned actions without writing. `GITHUB_TOKEN` reads
public repositories only.

## Release version check

`.github/workflows/release-version.yml` checks a release pull request: one whose
head branch starts with `release/v` or whose title starts with `chore(release)`.
The branch must be `release/vX.Y.Z`, the title `chore(release): X.Y.Z`, and that
version must equal what the newest git-cliff computes with `--bumped-version`
from the conventional commits since the last `vX.Y.Z` tag. Below 1.0.0 a
breaking change (`!` in the subject or a `BREAKING CHANGE:` footer) raises the
middle number, and features and fixes raise the last; a repository without a
release tag starts at 0.1.0. Other pull requests pass with a notice, so the
check can be required. On `merge_group` it runs again at the queue's commit,
with the branch and title of the queued pull request read from the API, so a
breaking change queued ahead of a release fails the release there. Callers grant
`contents: read` and `pull-requests: read`.

git-cliff is installed from PyPI at its newest release and called directly;
`ci.yml` installs it the same way for `scripts/test_release_version.py`, which
runs the workflow's step scripts and the real git-cliff on throwaway
repositories, so a git-cliff release that changes the result fails the tests.
The configuration is written into `$RUNNER_TEMP`, and the caller's `cliff.toml`
is not read. git-cliff layers a configuration over its embedded default, which
skips `chore(deps…)` commits, so the workflow sets its own `commit_parsers`. It
also removes `.cliffignore` and `.git-blame-ignore-revs` from the checkout,
because git-cliff drops the commits listed there. `split-commits` defaults to
false, because with it a `BREAKING CHANGE:` footer is dropped.

Below 1.0.0 a release that raises the middle number for features or fixes now
fails: features and fixes go into the last number, and only a breaking change
raises the middle one (decided 2026-11-27). The mismatch with earlier releases
is intended: of the last 17 netresearch/t3x-nr-llm releases (v0.44.0 to
v0.55.1), 12 raised the middle number without a breaking commit and would have
failed (11 of them carried features, v0.52.0 only fixes).

A second gap: when the commits and the changelog disagree, the check fails and
the release author aligns them: add a commit with a breaking marker to the
release branch, or set `skip-reason` together with `skip-version`. Below 1.0.0 a
breaking commit raises only the middle number, so 1.0.0 is released only through
the skip. The skip is logged as a warning and applies only to that version; the
next release pull request fails until both inputs are removed. The workflow
header names the cases from netresearch/t3x-nr-llm 0.35.0, 0.39.0 and 0.41.0.

## Reusable workflows

The reusable workflows under `.github/workflows/*.yml` are consumed across the org. Reference convention:

- **Intra-org callers reference these by `@main` (or a published `@vN` interface tag), never a full-length SHA.** These workflows live in this same org and are kept correct here; SHA-pinning freezes callers on a stale revision and creates per-repo Renovate churn for no security gain. Example: `uses: netresearch/.github/.github/workflows/go-check.yml@main` — note the doubled `.github/.github`, since the org repo is literally named `.github`.
- SonarCloud's "pin GitHub Actions to a full-length commit SHA" hotspot is **advisory and wrong for first-party intra-org reusables** — mark it *Safe* rather than SHA-pinning. (SHA-pinning is still correct for third-party, non-org actions.)
- **Third-party actions inside these workflows are SHA-pinned**, so foreign code is pinned where it actually enters: `step-security/harden-runner`, `actions/checkout`, `actions/setup-go`, `oven-sh/setup-bun`, `codecov/codecov-action`, `golangci/golangci-lint-action`, `github/codeql-action` — every one carries a full SHA plus a version comment. Go tools installed with `go install` (gotestsum, go-licenses, gosec) are pinned to a commit the same way. The `@main` above applies to first-party reusables only.
- **The distinction is enforced, not just documented**: `.github/zizmor.yml` sets `unpinned-uses` policies `netresearch/* -> ref-pin` and `* -> hash-pin`, and the shared `zizmor.yml` workflow installs that config in any consumer repo that carries none. An unpinned third-party `uses:` therefore still fails an audit.
- Scanners that cannot express that distinction produce first-party noise, and are handled centrally rather than per repo:
  - **CodeQL** `actions/unpinned-tag` — excluded via `query-filters` in the shared `codeql.yml`, with zizmor as the compensating control.
  - **OpenSSF Scorecard** `Pinned-Dependencies` — the action has no per-check opt-out, so the alerts are dismissed as *won't fix* with this section as the reason. The score is affected; that is the accepted cost.
- **`auto-merge-deps.yml` is frozen by a test.** `.github/actions/dependency-merge-controller/test_contract.py` pins a byte fingerprint of `.github/workflows/auto-merge-deps.yml` (`test_legacy_reusable_is_unchanged`). The fingerprint ignores only action pins, and `test_contract_comments.py` asserts that pin and numeric version bumps are the only edits that keep it. Any other edit fails the "Dependency controller policy fixtures" check, even a one-word input rename. Change the file only together with the fingerprint, as a deliberate change of the contract. `dependency-merge-controller.yml` carries no fingerprint; tests such as `test_wiring.py` and `test_phases.py` parse its structure instead.
