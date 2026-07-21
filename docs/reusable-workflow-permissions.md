# Reusable-workflow permission contract

Authoritative reference for the `GITHUB_TOKEN` permissions a **caller** must grant
when invoking a `netresearch/.github`, `netresearch/typo3-ci-workflows`, or
`netresearch/typo3-docs-ci-workflows` reusable workflow.

> **Why this exists.** Under-granting a reusable workflow's permissions does not
> degrade gracefully — it **rejects the whole workflow at startup**
> (`startup_failure`, zero jobs, no logs) before anything runs. Historically this
> bit us repeatedly and "in a different repo every time" because the breakage is
> latent: a permissive repo default masks the missing grant until the repo is
> hardened. This document + the per-reusable `# CALLER REQUIREMENTS` headers +
> the canonical templates + drift enforcement exist to make it impossible to get
> wrong silently.

## 1. The permission model (the rules that bite)

1. **The calling job's `permissions:` is a hard ceiling.** A reusable workflow's
   jobs may declare equal or fewer scopes; they may **never** declare more. Any
   over-declaration anywhere in the call chain fails the run at **startup**, e.g.
   `The nested job 'gitleaks' is requesting 'security-events: write', but is only
   allowed 'security-events: none'`.
2. **Setting any scope forces every unlisted scope to `none`.** A job that writes
   `permissions: { contents: read }` has implicitly set `security-events: none`,
   `id-token: none`, etc. You must enumerate **every** scope the reusable needs.
3. **A calling job that omits `permissions:` inherits the repository default**
   (`default_workflow_permissions`), *not* the caller workflow-level block. So a
   repo whose default is `read` (restricted) hands the reusable `contents: read`
   and `none` for everything else — and any reusable needing a write scope
   startup-fails.
4. **Precedence (outer → inner cap):** enterprise/org/repo `default_workflow_permissions`
   → caller workflow-level `permissions:` → caller **job-level** `permissions:`
   → fork-PR write→read downgrade. A restricted default at any level caps
   everything below it; no `permissions:` block can exceed org/enterprise policy.
5. **Least privilege lives at the call site.** Granting a broad union at the
   caller does *not* force that breadth onto reusable jobs that self-declare less
   (each job's token = `min(ceiling, its own declaration)`) — but it *does* widen
   the blast radius of any compromised step. Grant the **exact union** the
   reusable chain declares, never `write-all` "to be safe".

## 2. Caller contract per reusable

Grant **at least** these scopes at the calling job. (Each reusable also carries
this in its own `# CALLER REQUIREMENTS` header — that header is the source of
truth; this table is the index.)

### `netresearch/.github`

| reusable | required caller permissions |
| --- | --- |
| `codeql.yml` | `contents: read`, `security-events: write`, `actions: read` |
| `gitleaks.yml` | `contents: read`, `security-events: write` |
| `scorecard.yml` | `contents: read`, `security-events: write`, `id-token: write`, `actions: read` |
| `zizmor.yml` | `contents: read`, `security-events: write` |
| `dependency-review.yml` | `contents: read`, `pull-requests: write` |
| `labeler.yml` | `contents: read`, `pull-requests: write` |
| `pr-quality.yml` | `contents: read`, `pull-requests: write` |
| `greetings.yml` / `lock.yml` / `stale.yml` | `issues: write`, `pull-requests: write` |
| `auto-merge-deps.yml` | `contents: write`, `pull-requests: write` |
| `go-check.yml` | `contents: read`, `security-events: write` |
| `go-mutation-testing.yml` | `contents: read`, `pull-requests: write` |
| `build-container.yml` | `contents: read`, `packages: write`, `security-events: write`, `id-token: write`, `attestations: write` |
| `build-go-attest.yml` | `contents: write`, `id-token: write`, `attestations: write` |
| `release-go-app.yml` | `contents: write`, `packages: write`, `id-token: write`, `attestations: write`, `security-events: write` |
| `golib-create-release.yml` | `contents: write`, `id-token: write`, `attestations: write` |
| `release-composer-package.yml` | `contents: write` |
| `ghcr-retention.yml` | `packages: write` |
| `security-container.yml` | `contents: read`, `security-events: write`, `packages: read` |
| `node-ci.yml` | `actions: read`, `contents: read`, `security-events: write`, `pull-requests: write` |
| `python-app-ci.yml` | `actions: read`, `contents: read`, `security-events: write`, `pull-requests: write`, `id-token: write` |
| `python-release.yml` | `contents: write`, `id-token: write` |
| `lint-*.yml` / `php-ci.yml` / `python-ci.yml` / `python-build.yml` / `python-audit.yml` / `ansible-lint.yml` / `ansible-molecule.yml` / `ts-check.yml` / `node-audit.yml` / `node-test.yml` / `node-build.yml` / `sonarqube.yml` / `smoke-test-container.yml` / `lint-container.yml` / `check-template-drift.yml` | `contents: read` |

### `netresearch/typo3-ci-workflows`

| reusable | required caller permissions |
| --- | --- |
| `ci.yml` | `contents: read` |
| `security.yml` | `contents: read`, `security-events: write` |
| `fuzz.yml` / `license-check.yml` / `e2e.yml` / `extended-testing.yml` / `docs.yml` / `republish.yml` / `publish-*.yml` | `contents: read` |
| `release.yml` / `release-typo3-extension.yml` | `contents: write`, `id-token: write`, `attestations: write` |

### `netresearch/typo3-docs-ci-workflows`

| reusable | required caller permissions |
| --- | --- |
| `backport.yml` | `contents: write`, `pull-requests: write` |
| `docs-render.yml` / `php-quality.yml` / `php-tests.yml` | `contents: read` |

## 3. Canonical consumer pattern

Top-level `permissions: {}` (default-deny); grant per calling job exactly the
contract; never rely on `default_workflow_permissions`:

```yaml
permissions: {}
jobs:
  security:
    uses: netresearch/typo3-ci-workflows/.github/workflows/security.yml@main
    permissions:
      contents: read
      security-events: write   # required — SARIF upload; do NOT rely on the repo default
  codeql:
    uses: netresearch/.github/.github/workflows/codeql.yml@main
    permissions:
      contents: read
      security-events: write
      actions: read
```

Do **not** write a workflow that calls a security/SARIF reusable with only
`contents: read` (or with no `permissions:` block) and rely on the repo default
being `write`. That is the latent failure this contract eliminates.

## 4. Secure baseline: `default_workflow_permissions: read`

The target end-state is `default_workflow_permissions = read` (restricted) on
every repo, with every reusable call site granting its contract explicitly.

**Ordering is load-bearing.** Adopt the canonical template (explicit grants) on a
repo **before** flipping its default to `read`. Flipping first re-creates the
startup-failure for every call site that was relying on the permissive default.

## 5. Kept correct by templates + drift

Per-class canonical templates under `templates/<class>/.github/` carry contract-
correct call sites. `check-template-drift.yml` (shipped into every consumer)
fails CI when a governed file diverges, unless the path is listed under
`intentional-drift:` in the consumer's `.github/template.yaml`. Roll a fix or a
new reusable permission out by editing the template, then `scripts/sync-template.sh`
/ `scripts/sync-all-consumers.sh`; drift-check flags every consumer that hasn't
caught up.

## 6. Change-management runbook (adding/changing a reusable permission)

1. Update the reusable workflow's job `permissions:` **and** its
   `# CALLER REQUIREMENTS` header in the same commit.
2. Update every `templates/<class>/.github/workflows/*` call site that invokes it.
3. `scripts/sync-all-consumers.sh` (opens PRs) — drift-check makes the gap visible
   fleet-wide; do not hand-edit consumers one at a time.
4. Never widen the reusable to avoid a caller change, and never disable a security
   gate to make a run green — fix the caller grant.
