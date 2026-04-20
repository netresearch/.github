# Go Workflow Standardization — Design

Date: 2026-04-19
Status: Approved, in execution.

## Goal

Every netresearch Go repository runs an identical, committed-to-repo workflow standard. No inline action use in project-specific workflows; the "standard" is a template checked into `netresearch/.github`, drift is blocking CI in every consuming repo.

## Scope

**In-scope repos (6):**

| Repo | Kind | Template |
|---|---|---|
| ofelia | app (fork) | go-app |
| ldap-manager | app | go-app |
| ldap-selfservice-password-changer | app | go-app |
| raybeam | app | go-app |
| simple-ldap-go | lib | go-lib |
| go-cron | lib (fork) | go-lib |

**Out-of-scope:** `terraform-provider-ad` (HashiCorp upstream conventions conflict with our templates).

## Target state

### Templates live in `netresearch/.github`

```
netresearch/.github/
├── .github/workflows/          (reusables — existing + new)
├── templates/
│   ├── go-app/
│   └── go-lib/
├── scripts/
│   ├── sync-template.sh
│   ├── sync-all-consumers.sh
│   └── apply-branch-protection.sh
└── docs/design/
    └── go-workflow-standardization.md   (this doc)
```

### No versioning yet (YAGNI)

All callers reference `@main`. When breaking-change pain justifies it, we introduce a `release/v1` branch — not before.

### Template contents

**go-app (12 workflow files):** `auto-merge-deps.yml`, `ci.yml`, `codeql.yml`, `container-retention.yml`, `dependency-review.yml`, `gitleaks.yml`, `labeler.yml`, `mutation.yml`, `pr-quality.yml`, `release.yml`, `scorecard.yml`, `check-template-drift.yml`. Container builds happen inside `release.yml` on tag push — not a separate per-push workflow. See "Single-build release pipeline" below.

**go-lib (11 workflow files):** `go-app` minus `container-retention.yml`; `release.yml` calls `golib-create-release.yml`.

Plus `.github/dependabot.yml`, `.github/labeler.yml`, `.github/template.yaml` in both templates.

### Reusable workflow changes

**Extended:**
- `go-check.yml` — new inputs `enable-fuzz` (default `true`), `enable-license-check` (default `true` with MIT/Apache-2.0/BSD-*/ISC/MPL-2.0 allowlist + `license-allowlist-extra`), `enable-smoke-fast-feedback` (default `true`), `coverage-threshold` (default `80.0`).
- `pr-quality.yml` — new input `auto-approve-maintainers` (default `true`) approving PRs from dependabot/renovate + OWNER/MEMBER/COLLABORATOR.

**New:**
- `ghcr-retention.yml` — ported from ofelia's `cleanup-containers.yml`. Inputs: `package-name`, `release-tag-patterns`, `edge-tag-patterns`, `edge-max-age-days`, `dry-run`.
- `go-mutation-testing.yml` — ported from ofelia's `mutation.yml`. Inputs: `go-version-file`, `config-path` (default `.gremlins.yaml`), `timeout-minutes`, `diff-only`.
- `check-template-drift.yml` — compares consumer's `.github/` to `templates/<template>/.github/` on `main`, minus `intentional-drift:` paths in the consumer's `.github/template.yaml`. Exits 1 on any diff.

### Drift enforcement

Each consuming repo:
- Includes `check-template-drift.yml` caller (part of the template, so self-verifies).
- Has `.github/template.yaml`:
  ```yaml
  template: go-app            # or go-lib
  intentional-drift:
    - path: .github/workflows/ci.yml
      reason: "upstream-fork patches conflict with smoke/full split"
      expires: 2026-07-01
  ```
- Drift check is a **required status check** — blocking merge.

A scheduled `drift-scan.yml` in `netresearch/.github` runs weekly across the fleet and opens/updates one issue per repo listing active drift.

### Single-build release pipeline

go-app `release.yml` cross-compiles Go binaries **once** and reuses them for two consumers:
1. Published to the GitHub Release page as user-facing artifacts (all 8 targets: `linux-{386,amd64,arm64,armv6,armv7}`, `darwin-{amd64,arm64}`, `windows-amd64`).
2. Downloaded back into `bin/` by the `container` job, where the Dockerfile's `binary-selector` stage (`COPY bin/<name>-linux-*`) picks the right binary per `TARGETARCH`/`TARGETVARIANT`.

No `go build` runs inside the Dockerfile — that would be a second compile of the same code for the same outputs. The Dockerfile stays a thin staging layer.

Container images build 5 platforms: `linux/386, linux/amd64, linux/arm/v6, linux/arm/v7, linux/arm64`.

**Frontend-embedding repos** (e.g. ones with `//go:embed *.css`): ship a `bun run build:assets` script in `package.json`. `release.yml` runs it via `build-go-attest.yml`'s `pre-build-command` before `go build`, so embedded assets exist when the matrix binary compiles. Non-frontend repos share the identical workflow — the step is a no-op when `package.json` is absent.

**Dockerfile convention** (all go-app repos):
```dockerfile
FROM alpine:<pinned> AS binary-selector
ARG TARGETARCH
ARG TARGETVARIANT
COPY bin/<name>-linux-* /tmp/
RUN case "${TARGETARCH}" in \
      arm)            BINARY="<name>-linux-arm${TARGETVARIANT}" ;; \
      386|amd64|arm64) BINARY="<name>-linux-${TARGETARCH}" ;; \
      *) echo "unsupported: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    cp "/tmp/${BINARY}" /usr/bin/<name>; chmod +x /usr/bin/<name>

FROM alpine:<pinned>
# (LABELs, USER, ENTRYPOINT, etc.)
COPY --from=binary-selector /usr/bin/<name> /usr/bin/<name>
```

No per-push `container.yml` workflow — containers are only built on tag push from pre-built matrix binaries. The previous template had a separate `container.yml` that ran plain `docker buildx build` on every main push; it was removed because (a) it can't co-exist with the single-build rule without duplicating the Go compile, and (b) container publishing on every main commit was never a fleet requirement.

### Coverage floor

80% fleet-wide. Repos below 80% raise coverage in their migration PR (Mitigation A — no two-speed fleet).

## Migration waves

Executed without grace periods or soft-warn phases.

| Wave | Scope | Deliverable |
|---|---|---|
| 0 | Foundation: extend + add reusables; add templates; add scripts; add drift-scan | Single PR against `netresearch/.github` |
| 1 | ofelia: sync to go-app template + raise coverage if needed | One PR against ofelia |
| 2 | raybeam, ldap-manager, ldap-selfservice-password-changer: sync to go-app | Three parallel PRs |
| 3 | simple-ldap-go, go-cron: sync to go-lib | Two parallel PRs |
| 4 | Mark drift-check as required status check in all 6 repos; enable scheduled drift-scan | Branch protection updates + one PR |

## Trigger matrix

| Workflow | Triggers |
|---|---|
| auto-merge-deps | `pull_request_target` |
| ci / go-check | `push: main, tags`, `pull_request`, `merge_group`, `workflow_dispatch`, `schedule` (weekly) |
| codeql | `push: main`, `pull_request`, `schedule` |
| container-retention | `schedule: '0 2 * * 0'`, `workflow_dispatch` |
| dependency-review | `pull_request` |
| gitleaks | `push: main`, `pull_request` |
| labeler | `pull_request_target` |
| mutation | `schedule: '0 2 * * 0'`, `pull_request: paths: ['**.go', '.gremlins.yaml', '.github/workflows/mutation.yml']`, `workflow_dispatch` |
| pr-quality | `pull_request: [opened, synchronize, reopened, ready_for_review]` |
| release | `push: tags: v*`, `workflow_dispatch` |
| scorecard | `push: main`, `schedule: '0 0 * * 0'`, `workflow_dispatch` |
| check-template-drift | `pull_request`, `push: main` |

## Rollback

Each wave is a single PR or small set. Rollback by revert commit on the offending branch. Templates are additive to `netresearch/.github` in Wave 0, so reverting the Wave 0 PR leaves all existing consumers unaffected.
