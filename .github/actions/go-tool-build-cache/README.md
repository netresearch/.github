# Optional pinned Go tool compilation cache

Internal action for the default-off `enable-tool-build-cache` input in
`go-check.yml`. Only the commit-pinned gosec and go-licenses installers opt in.
The default path retains the existing installers; every scan/report and every
independent native job remains. Golangci and govulncheck `latest` resolution is
unchanged and receives no new cache.

The owning first-party action is called at `netresearch/.github/...@main`.
Its actual helper/descriptor digest is printed and forms part of the key;
native qualification must also retain the downloaded action's resolved SHA.

Keys bind full tool commit, actual configured Go patch release, OS/architecture,
architecture features/CGO mode, versioned Linux runner image and helper source.
Go's internal cache identities handle other compiler flags. Unknown image,
custom/uncertain toolchain, mutable target or newer module requirement disables
the cache. A different installed compiler forbids saving. The existing pinned
installer always executes, including exact cache hits; a cache install failure
retries it with its original environment and preserves real failures.

Only install-specific `GOCACHE` and module download data are archived. Effective
`GOBIN` (or first `GOPATH/bin`) and both roots must be disjoint after resolving
symlinks, checked before restore and installation. Installed binaries, expanded
module trees, downloaded toolchains, reports and test outcomes are excluded.
Scans use their original environment. No restore prefixes are accepted.

Saving is explicitly after a successful scan/SARIF job, on a protected default
branch push only. Pull requests, merge groups, tags, unprotected refs and failed
scans cannot save. Cache service failures do not replace quality outcomes.

The protected-default rule governs this helper's writes. Restores retain
GitHub's current/base/default branch-scope lookup. Keys and compiler metadata
identify contents without authenticating protected-main provenance. GOCACHE
retains the existing runner/cache trust; a download-only module archive adds
no provenance authentication to GOCACHE.

Local proof on Go 1.27.3 retains byte-identical tool binaries, identical clean
gosec SARIF and real red gosec/license findings. Download-only replay at the same
module path also preserves bytes/scans, while the module archive compresses
from 54.4 MB to 32.0 MB for licenses and 142.9 MB to 86.4 MB for gosec (zstd level 3).
These local results do not include hosted transfer time or prove a hosted gain.

Review/activation: independently review guard and negative fixtures, merge the
default-off source, capture actual reusable/action revisions, then opt in only
go-cron. Measure native cold/warm installs, transfer/save time, cache footprint,
all native check contexts, scan findings and reports. Promote Ofelia only after
that pilot is green and beneficial. No mutation/test/coverage scope changes.
