# go-app template

Canonical workflow set for a netresearch Go application repository
(one that ships a binary and/or container image).

## Files included

| File | Purpose |
|---|---|
| `.github/workflows/auto-merge-deps.yml` | Auto-approve + auto-merge Dependabot/Renovate PRs via org reusable. |
| `.github/workflows/check-template-drift.yml` | Blocks PRs that drift from this template; exceptions in `.github/template.yaml`. |
| `.github/workflows/ci.yml` | Build, vet, test (+ smoke, coverage ≥80%), golangci-lint, govulncheck, gosec, fuzz corpus replay, license check. |
| `.github/workflows/codeql.yml` | CodeQL analysis for Go. |
| `.github/workflows/container.yml` | Multi-arch container build + push + sign + SBOM + trivy + hadolint. |
| `.github/workflows/container-retention.yml` | Weekly GHCR cleanup — releases forever, edge >30d deleted, orphan untagged pruned. |
| `.github/workflows/dependency-review.yml` | PR dependency review. |
| `.github/workflows/gitleaks.yml` | Secret scan on push + PR. |
| `.github/workflows/labeler.yml` | PR labeling via `.github/labeler.yml` config. |
| `.github/workflows/mutation.yml` | Weekly + on-PR gremlins mutation testing (diff-only on PR). |
| `.github/workflows/pr-quality.yml` | PR size check + auto-approve for maintainers. |
| `.github/workflows/release.yml` | GitHub release + SLSA-attested multi-arch Go binaries on tag push. |
| `.github/workflows/scorecard.yml` | OSSF Scorecard weekly + on-push. |
| `.github/dependabot.yml` | Canonical Dependabot config (weekly grouped updates for go / actions / docker). |
| `.github/labeler.yml` | Canonical PR label rules. |
| `.github/template.yaml` | Template identity + per-repo intentional-drift exceptions. |

## Consuming

```
bash scripts/sync-template.sh go-app <owner>/<repo>
```

Opens a PR on `<owner>/<repo>` that copies this directory's content into
the target repo, overwriting any existing matching files.

## Tuning inputs

The `ci.yml` caller exposes all `go-check.yml` inputs. For overrides
that should persist, edit your copy of `ci.yml` and add that file path
to `.github/template.yaml`'s `intentional-drift:` so drift-check doesn't
revert your change on the next sync.
