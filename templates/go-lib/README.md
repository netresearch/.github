# go-lib template

Canonical workflow set for a netresearch Go library repository
(one imported by other code, not shipping a binary or container).

## Differences vs go-app

- No `container.yml` (no image build)
- No `container-retention.yml` (no image retention)
- `release.yml` calls `golib-create-release.yml` (Go-proxy-friendly release without binaries)

Everything else mirrors `go-app`. See the go-app README for per-workflow descriptions.

## Consuming

```
bash scripts/sync-template.sh go-lib <owner>/<repo>
```
