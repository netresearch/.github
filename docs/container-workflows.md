# Reusable Container Workflows

Org-wide reusable workflows for container repos (Dockerfile-based deliverables
published to ghcr.io). All workflows are versioned by their location in
`netresearch/.github` and pinned by caller refs (`@main` or a SHA).

Consumer repos: [snipe-it-docker-compose-stack](https://github.com/netresearch/snipe-it-docker-compose-stack),
[phpbu-docker](https://github.com/netresearch/phpbu-docker), more to follow.

## Workflows

| Workflow | Purpose | Status |
| -------- | ------- | ------ |
| [`build-container.yml`](../.github/workflows/build-container.yml) | Multi-arch buildx + ghcr push + Trivy + cosign + SLSA provenance | Pre-existing |
| [`lint-container.yml`](../.github/workflows/lint-container.yml) | Dockerfile lint (hadolint) + optional shellcheck on shipped scripts | New |
| [`security-container.yml`](../.github/workflows/security-container.yml) | Post-build Trivy scan against a published image tag, SARIF upload | New |
| [`smoke-test-container.yml`](../.github/workflows/smoke-test-container.yml) | Build locally + run container-structure-test | New |
| [`scorecard.yml`](../.github/workflows/scorecard.yml) | OpenSSF Scorecard | Pre-existing |
| [`ghcr-retention.yml`](../.github/workflows/ghcr-retention.yml) | GHCR tag retention / cleanup | Pre-existing |
| [`gitleaks.yml`](../.github/workflows/gitleaks.yml) | Secret scanning | Pre-existing |
| [`lint-workflows.yml`](../.github/workflows/lint-workflows.yml) | actionlint | Pre-existing |
| [`lint-yaml.yml`](../.github/workflows/lint-yaml.yml) | yamllint | Pre-existing |
| [`auto-merge-deps.yml`](../.github/workflows/auto-merge-deps.yml) | Auto-merge Renovate / Dependabot | Pre-existing |

## Caller patterns

Each reusable workflow's header block documents its inputs and a copy-pasteable
caller snippet. The short version:

### Minimal container repo

```yaml
# .github/workflows/lint.yml
name: lint
on:
  push: { branches: [main] }
  pull_request: { branches: [main] }
permissions: { contents: read }
jobs:
  container:
    uses: netresearch/.github/.github/workflows/lint-container.yml@main
    with:
      shell-scandirs: ./bin ./rootfs/usr/local/bin
  workflows:
    uses: netresearch/.github/.github/workflows/lint-workflows.yml@main
  yaml:
    uses: netresearch/.github/.github/workflows/lint-yaml.yml@main
```

```yaml
# .github/workflows/smoke-test.yml
name: smoke-test
on:
  pull_request: { branches: [main] }
  push: { branches: [main] }
permissions: { contents: read }
jobs:
  smoke:
    uses: netresearch/.github/.github/workflows/smoke-test-container.yml@main
    with:
      image-tag: my-app:test
      target: runtime
      cst-config-path: tests/container-structure-test.yaml
```

### Post-build security scan with matrix fan-out

`security-container.yml` declares its own job-level `permissions:`
(`security-events: write` for SARIF upload, `packages: read` for GHCR
pulls, `contents: read` for checkout). The caller's top-level
`permissions:` block applies only to OTHER jobs in the same workflow —
keep it `contents: read` and let the reusable handle its needs.

```yaml
# .github/workflows/security.yml
name: security
on:
  workflow_run:
    workflows: [build]
    types: [completed]
    branches: [main]
  schedule:
    - cron: '0 6 * * *'
permissions: { contents: read }
jobs:
  trivy:
    if: ${{ github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success' }}
    strategy:
      fail-fast: false
      matrix:
        tag: [latest, rolling]
    uses: netresearch/.github/.github/workflows/security-container.yml@main
    with:
      image-ref: ghcr.io/${{ github.repository_owner }}/my-app:${{ matrix.tag }}
      sarif-category: trivy-${{ matrix.tag }}
```

## What stays in the caller

The reusable workflows deliberately do NOT cover these pieces — they need
caller-specific bootstrap and don't generalise cleanly:

- `docker compose config` validation with placeholder `.env` substitution
  (needs app-specific `.env.example` shape).
- `docker compose up -d --wait` + HTTP probe (needs known route + healthcheck
  semantics).
- Initialisation-script idempotency tests (needs caller's `init.sh` contract).
- `osv-scanner` against language lockfiles inside the image (needs lockfile
  path + language; varies per stack).
- Multi-track / multi-composer-mode build matrices (caller defines the matrix
  axes and tag schemes; `build-container.yml` handles a single `ref` per call).

## Conventions

- SPDX `MIT` header + `Copyright (c) 2026 Netresearch DTT GmbH` on every
  workflow file.
- All third-party actions SHA-pinned with a trailing `# vX.Y.Z` comment;
  Renovate updates them.
- `harden-runner` (egress audit) as the first step in every job.
- `permissions:` enumerated per job — never `read-all` (SonarCloud S8234).
- `${{ ... }}` interpolation passes through `env:` to `run:` blocks
  (SonarCloud S7630, shell-injection hardening).
- `persist-credentials: false` on every `actions/checkout`.
- Caller passes secrets by name (`secrets: { GHCR_TOKEN: ${{ secrets.GITHUB_TOKEN }} }`),
  never `secrets: inherit`.
