# `release-go-app.yml` orchestrator — design

**Status:** approved 2026-04-26
**Author:** sebastian.mendel@netresearch.de
**Supersedes:** the multi-job pipeline currently in `templates/go-app/.github/workflows/release.yml`

## 1. Problem

GitHub now treats published releases as **immutable**: once a release is published, asset uploads return `HTTP 422: Cannot upload assets to an immutable release.`

The current go-app release pipeline scatters writes to the release across four jobs:

```
create-release  →  binaries (matrix×8)  →  container  →  finalize
   (publishes)        (uploads binary)    (uses release)  (uploads checksums)
```

Every downstream job touches the release after it's already published. After GitHub started enforcing immutability, every consumer release ships only the auto-generated source archives — no binaries, no checksums, no SBOMs. Most recently observed on [`netresearch/ldap-manager` v1.4.0](https://github.com/netresearch/ldap-manager/releases/tag/v1.4.0).

The other Netresearch releasers — [`skill-repo-skill/release.yml`](https://github.com/netresearch/skill-repo-skill/blob/main/.github/workflows/release.yml) and [`typo3-ci-workflows/release-typo3-extension.yml`](https://github.com/netresearch/typo3-ci-workflows/blob/main/.github/workflows/release-typo3-extension.yml) — already solved this with the **atomic-release** pattern: build everything first, then create the GitHub release in a single `softprops/action-gh-release@v3` call. The TYPO3 releaser even comments: *"immutability-friendly: nothing is added, removed, or edited after publication."*

This spec converges the go-app pipeline on the same proven pattern.

## 2. Solution

Add a single new reusable workflow `release-go-app.yml` that owns the entire release pipeline as an immutability-friendly DAG. The template `templates/go-app/.github/workflows/release.yml` becomes a thin caller. `create-release.yml` and `finalize-release.yml` are deprecated (kept short-term for the migration window, removed in a follow-up PR after consumers drift-update).

## 3. Architecture

```
┌─────────────────┐
│ preflight       │  Resolve tag (semver check), verify annotated tag,
│                 │  refuse if release already exists for this tag.
│                 │  Compute prerelease + make_latest flags.
│                 │  Generate release notes (changelog).
└────────┬────────┘
         │ outputs: tag, version, sha, is-latest, is-prerelease, notes
         ▼
┌─────────────────┐  Matrix: 8 platforms by default (linux/{386,amd64,arm64,
│ binaries (×8)   │  armv6,armv7}, darwin/{amd64,arm64}, windows/amd64).
│ (parallel)      │  Each: go build → attest-build-provenance → optional SBOM
│                 │  → upload to GitHub Actions artifact `binary-<target>`.
└────────┬────────┘  No release writes.
         │
         ▼
┌─────────────────┐  needs: binaries.
│ container       │  Downloads all linux/* binaries from artifacts into bin/.
│ (1 job)         │  Builds multi-arch image, pushes to GHCR, signs (cosign
│                 │  keyless), attests, runs Trivy. Uploads SARIF to Security
└────────┬────────┘  tab. Independent of release.
         │ output: image-ref, digest, tags
         ▼
┌─────────────────┐  needs: [preflight, binaries, container].
│ release         │  Downloads ALL binary+SBOM artifacts. Per-asset cosign
│ (atomic publish)│  sign-blob → .bundle. Generates checksums.txt. Signs
│                 │  checksums.txt. Attests checksums. Composes release body
│                 │  (notes + verification block + container reference).
│                 │  ONE softprops/action-gh-release@v3 call with all files.
└─────────────────┘  → release.url
```

### Job dependencies

- `preflight → binaries → container → release`
- `release` waits for ALL of the above (transitive via `needs: [preflight, binaries, container]`)
- If any earlier stage fails, `release` is skipped — no partial release.

### Why this DAG

- **Linear, not parallel.** The container needs binaries (Dockerfile expects pre-built `bin/<name>-linux-*`). The release needs binaries + the container's image-ref for the verification block. There's no useful parallelism past the binary matrix itself.
- **Container is independent of release.** Image is published to GHCR regardless of whether the release job runs. If the release publish fails (rate limit, network blip), the image is still in the registry — the maintainer can re-tag and re-run; the new release just references the existing image.
- **Preflight is first, not folded into the release job.** A release-already-exists check at minute 1 is much cheaper than discovering the conflict at minute 15 after the matrix completes.

## 4. New file: `.github/workflows/release-go-app.yml`

### 4.1 Inputs

| Input | Type | Default | Notes |
|---|---|---|---|
| `app-name` | string | _required_ | Binary + image name. Caller passes `${{ github.event.repository.name }}`. |
| `tag` | string | `""` | Override; defaults to `github.ref_name`. For workflow_dispatch backfills. |
| `main-package` | string | `auto` | Forwarded to build step (auto-detect `.` vs `./cmd/<repo-name>`). |
| `goos-goarch-matrix` | string (JSON) | `[{"target":"linux-386","goos":"linux","goarch":"386"},...]` (default 8) | Override platform matrix. |
| `cgo-enabled` | string | `"0"` | Build flag. |
| `ldflags` | string | `-s -w` | Linker flags (orchestrator appends `-X main.version=<tag> -X main.build=<sha>`). |
| `auto-build-timestamp` | boolean | `true` | Append `-X main.buildTime=<HEAD-iso8601>`. |
| `setup-bun` | boolean | `false` | Install Bun for asset embedding. Template caller sets `true` for go-app consumers (preserves byte-identical-template policy with ~10s install overhead per matrix runner). |
| `bun-version` | string | `latest` | |
| `setup-node` | boolean | `false` | |
| `node-version` | string | `lts/*` | |
| `pre-build-command` | string | `""` | Pre-Go-build hook (default empty; template caller passes `if [ -f package.json ]; then bun install ...; fi`). |
| `container` | boolean | `true` | Build container image. Set false for binary-only releases. |
| `container-platforms` | string | `linux/386,linux/amd64,linux/arm/v6,linux/arm/v7,linux/arm64` | Multi-arch container platforms. |
| `container-pre-build-command` | string | `""` | Extra shell run AFTER the orchestrator's automatic binary-artifact download (which always happens). Useful if Dockerfile needs additional pre-staging. |
| `dockerfile` | string | `./Dockerfile` | |
| `prerelease` | string | `auto` | `auto` detects `-rc/-alpha/-beta/-pre` suffix. |
| `make-latest` | string | `auto` | `auto` computes from semver vs existing releases. |
| `previous-tag` | string | `""` | Override base for changelog generation. |
| `require-annotated-tag` | boolean | `true` | Reject lightweight tags. |
| `sign-artifacts` | boolean | `true` | Per-asset `cosign sign-blob --bundle file.bundle`. |
| `include-sbom` | boolean | `true` | Per-binary Syft SBOM (spdx-json). |

### 4.2 Outputs

| Output | Description |
|---|---|
| `tag` | Resolved tag (e.g. `v1.4.1`). |
| `version` | Tag without `v` prefix. |
| `release-url` | URL of the created release. |
| `image-ref` | `ghcr.io/<owner>/<app-name>` (full ref without tag). |
| `image-digest` | `sha256:...` of the published image. |
| `is-latest` | `true`/`false` — whether the release was marked as Latest. |

### 4.3 Permissions (declared)

```yaml
permissions:
  contents: write       # release create, checkout
  packages: write       # GHCR push
  id-token: write       # cosign keyless OIDC + attestations
  attestations: write   # actions/attest-build-provenance
  security-events: write # Trivy SARIF upload
```

Caller's job-level `permissions:` block must grant the same set.

### 4.4 Job 1: `preflight`

**Runs on:** `ubuntu-latest`, timeout 5 min.
**Permissions:** `contents: read` (release view).

Steps (in order):

1. **Harden Runner** (`step-security/harden-runner@v2`).
2. **Checkout** at `${{ inputs.tag || github.ref }}` with `fetch-depth: 0`.
3. **Resolve tag** — strict semver regex (same as current create-release.yml). Output: `tag`, `version`, `sha`.
4. **Verify annotated tag** — if `require-annotated-tag: true`, fail on lightweight. Verify GPG signature if available (warning-level).
5. **Refuse if release already exists** — `gh release view "$TAG"` succeeds → fail with:
   ```
   ::error::Release $TAG already exists. GitHub releases are immutable —
   even after deletion, the tag name cannot be reused for a new release.
   To retry: cut a new patch tag (git tag -s vX.Y.Z+1 ...) and re-run.
   ```
6. **Compute prerelease + make_latest** — same logic as current create-release.yml's `flags` step. Outputs: `is-prerelease`, `make-latest`.
7. **Generate release notes** — same logic as current create-release.yml's `notes` step (git log between previous tag and HEAD). Output: `notes` (heredoc).

**Outputs:** `tag`, `version`, `sha`, `is-prerelease`, `make-latest`, `notes`.

### 4.5 Job 2: `binaries` (matrix × 8 by default)

**Runs on:** `ubuntu-latest`, timeout 15 min per matrix entry.
**Permissions:** `contents: read`, `id-token: write`, `attestations: write`.
**Needs:** `preflight`.
**Strategy:** matrix from `inputs.goos-goarch-matrix` (default 8 platforms), `fail-fast: false`.

Steps:

1. **Harden Runner**.
2. **Checkout** at `${{ needs.preflight.outputs.tag }}` (tag, not ref — important for workflow_dispatch backfills).
3. **Setup Go** via `go-version-file: go.mod`.
4. **Setup Bun** (if `inputs.setup-bun: true`).
5. **Setup Node** (if `inputs.setup-node: true`).
6. **Pre-build command** (`bash -euo pipefail -c "$PRE_BUILD_CMD"`).
7. **Resolve `main-package: auto`** — same logic as current build-go-attest.yml.
8. **Compute auto-build-timestamp** — `git show -s --format=%cI HEAD`.
9. **Build binary** — `go build -trimpath -ldflags="<final-ldflags>" -o <name> <main-package>`.
10. **Attest binary** — `actions/attest-build-provenance@v4` with `subject-path: <binary>`.
11. **Generate SBOM** (if `inputs.include-sbom: true`) — `anchore/sbom-action@v0.24` → `<binary>.spdx.json`.
12. **Attest SBOM** (if SBOM generated).
13. **Upload artifact** — `actions/upload-artifact@v7` with name `binary-<target>`, includes both binary and SBOM. Retention: 1 day.

**No release writes.**

### 4.6 Job 3: `container`

**Runs on:** `ubuntu-latest`, timeout 30 min.
**Permissions:** `contents: read`, `packages: write`, `security-events: write`, `id-token: write`, `attestations: write`.
**Needs:** `[preflight, binaries]`.
**If:** `inputs.container == true`.

Steps:

1. **Harden Runner**.
2. **Checkout** at `${{ needs.preflight.outputs.tag }}`.
3. **Download all linux binary artifacts** (always — orchestrator-controlled, not user-overridable):
   ```yaml
   - uses: actions/download-artifact@v8
     with:
       pattern: 'binary-linux-*'
       path: bin/
       merge-multiple: true
   - run: chmod +x bin/*
   ```
4. **Run `container-pre-build-command`** (if non-empty) — extra shell hook for repos whose Dockerfile needs additional pre-staging beyond the binaries.
5. **Setup QEMU**, **Setup Buildx**.
6. **Gather Docker metadata** — same `docker/metadata-action@v6` config (semver `{{version}}`, `{{major}}.{{minor}}`, `{{major}}` from tag).
7. **Login to GHCR** (`docker/login-action@v4`).
8. **Build and push** — `docker/build-push-action@v7` with `inputs.container-platforms`.
9. **Trivy scan** + SARIF upload (CRITICAL,HIGH).
10. **Cosign sign** by digest — `cosign sign --yes "$IMAGE_REF@$DIGEST"`.
11. **Attest container** — `actions/attest-build-provenance@v4` with `subject-name`, `subject-digest`, `push-to-registry: true`.

**Outputs:** `image-ref`, `image-digest`, `tags`.

### 4.7 Job 4: `release` (atomic publish)

**Runs on:** `ubuntu-latest`, timeout 10 min.
**Permissions:** `contents: write`, `id-token: write`, `attestations: write`.
**Needs:** `[preflight, binaries, container]`.
**If:** `always() && needs.preflight.result == 'success' && needs.binaries.result == 'success' && (needs.container.result == 'success' || needs.container.result == 'skipped')`.

Steps:

1. **Harden Runner**.
2. **Download all binary artifacts** (`actions/download-artifact@v8`, pattern `binary-*`, path `release/`, `merge-multiple: true`).
3. **Generate sha256 checksums** — `cd release && sha256sum * > checksums.txt` (excludes `*.bundle`, `checksums.txt*`).
4. **Install Cosign** (if `sign-artifacts: true`).
5. **Per-asset cosign sign-blob** (if `sign-artifacts: true`):
   ```bash
   cd release
   for file in *; do
     [ -f "$file" ] || continue
     case "$file" in
       *.bundle) continue ;;  # don't sign bundles
       checksums.txt*) continue ;; # signed separately below
     esac
     cosign sign-blob --yes "$file" --bundle "${file}.bundle"
   done
   # Also sign checksums.txt itself
   cosign sign-blob --yes checksums.txt --bundle checksums.txt.bundle
   ```
6. **Attest checksums** — `actions/attest-build-provenance@v4` with `subject-path: release/checksums.txt`.
7. **Compose release body** — Markdown with sections:
   - **Changes** (from `needs.preflight.outputs.notes`)
   - **Container image** (if `inputs.container: true`):
     ```
     ghcr.io/<owner>/<app-name>:<version>
     ghcr.io/<owner>/<app-name>:<major>.<minor>
     ghcr.io/<owner>/<app-name>:<major>
     ```
   - **Verify your download** (verification block — see §5)
8. **Two-phase atomic publish (single job)** — softprops creates as draft + uploads, then `gh release edit` flips draft → published. Atomic from caller's POV (one job owns both phases; partial state is impossible because the publish step only runs if uploads succeeded), two-phase at API level because GitHub treats published releases as immutable and rejects asset uploads. softprops/action-gh-release surfaces this with the explicit hint: *"Cannot upload asset X to an immutable release. ... keep the release as a draft with draft: true, then publish it later from that draft."*

   ```yaml
   - name: Create draft release + upload all assets
     uses: softprops/action-gh-release@v3
     with:
       tag_name: ${{ needs.preflight.outputs.tag }}
       name: ${{ needs.preflight.outputs.tag }}
       body: ${{ steps.body.outputs.body }}
       files: release/*
       fail_on_unmatched_files: true
       draft: true
       generate_release_notes: false

   - name: Publish draft release
     id: publish
     env:
       GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
       TAG: ${{ needs.preflight.outputs.tag }}
       REPO: ${{ github.repository }}
       MAKE_LATEST: ${{ needs.preflight.outputs.make-latest }}
       IS_PRE: ${{ needs.preflight.outputs.is-prerelease }}
     run: |
       set -euo pipefail
       gh release edit "$TAG" --repo "$REPO" \
         --draft=false --latest="$MAKE_LATEST" --prerelease="$IS_PRE"
       URL=$(gh release view "$TAG" --repo "$REPO" --json url --jq .url)
       echo "url=$URL" >> "$GITHUB_OUTPUT"
   ```

   **Why two-phase, not single-call:** The single softprops call works for non-prerelease workflows with modest asset counts (typo3 releaser pattern). It fails for our combination of prereleases + ~30+ assets — softprops uploads files individually, and GitHub flips the release to immutable mid-upload, causing the last asset to fail with HTTP 422. Empirical validation in the v1.4.1-rc1 E2E test on ldap-manager: 33 of 34 assets uploaded before checksums.txt failed; release ended up published with 0 assets (rolled back). Draft mode sidesteps this entirely — drafts aren't immutable, all uploads succeed, then a single edit publishes.

## 5. Verification block (release body)

```markdown
## Verify your download

Per-asset signatures are bundled. Verify any single binary:

```bash
cosign verify-blob \
  --bundle ldap-manager-linux-amd64.bundle \
  --certificate-identity-regexp "https://github.com/netresearch/.*" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  ldap-manager-linux-amd64
```

Verify checksums against the signed manifest:

```bash
cosign verify-blob \
  --bundle checksums.txt.bundle \
  --certificate-identity-regexp "https://github.com/netresearch/.*" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  checksums.txt
sha256sum -c checksums.txt --ignore-missing
```

Verify build provenance:

```bash
gh attestation verify <artifact> --repo netresearch/<repo>
```

Verify the container image (when applicable):

```bash
cosign verify ghcr.io/netresearch/<app>:<version> \
  --certificate-identity-regexp "https://github.com/netresearch/.*" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
gh attestation verify oci://ghcr.io/netresearch/<app>:<version> --repo netresearch/<repo>
```
```

(All placeholders interpolated at runtime from `needs.preflight.outputs.*` and `inputs.app-name`.)

## 6. Updated `templates/go-app/.github/workflows/release.yml`

```yaml
name: Release

on:
  push:
    tags: ['v*']
  workflow_dispatch:
    inputs:
      tag:
        description: "Tag to (re)build (e.g. v1.2.3)."
        required: true
        type: string

permissions: {}

jobs:
  release:
    uses: netresearch/.github/.github/workflows/release-go-app.yml@main
    permissions:
      contents: write
      packages: write
      id-token: write
      attestations: write
      security-events: write
    with:
      app-name: ${{ github.event.repository.name }}
      tag: ${{ inputs.tag || github.ref_name }}
      pre-build-command: |
        if [ -f package.json ]; then
          bun install --frozen-lockfile
          bun run build:assets
        fi
```

That's the entire template caller. ~25 lines total (vs ~140 today).

## 7. Deprecation plan

`create-release.yml` and `finalize-release.yml` are still consumed only by the (current) go-app template — three repos: `ldap-manager`, `raybeam`, `ldap-selfservice-password-changer`. (`golib-create-release.yml` is separate; not affected.)

**Two-stage approach** (avoids any window where a consumer's release breaks):

### Stage 1 — this PR (`feat/release-go-app-orchestrator`)
- Add `release-go-app.yml`.
- Update `templates/go-app/.github/workflows/release.yml` to call the new orchestrator.
- Add deprecation notice to top of `create-release.yml` and `finalize-release.yml`:
  ```yaml
  # DEPRECATED — see docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md
  # New consumers: use release-go-app.yml instead. This file remains only
  # to keep existing consumers green during the migration window. Will be
  # removed after all consumers picked up the template-drift PR.
  ```
- Add `::warning::` annotation in the first step.
- Document migration in `docs/design/go-workflow-standardization.md`.

### Stage 2 — follow-up PR (after consumers drift-update)
- Wait for `check-template-drift` PRs to land in all three consumer repos (typically same-day).
- Verify each consumer's next release ships full binaries + checksums + SBOMs + container.
- Open a follow-up PR that deletes `create-release.yml` and `finalize-release.yml`.

## 8. Migration plan (consumer repos)

After Stage 1 merges:

1. The `check-template-drift.yml` workflow in each consumer repo opens a PR replacing their `release.yml` with the new ~25-line template.
2. Maintainer reviews + merges the drift PR.
3. Next release validates the atomic flow end-to-end.

Order (by lowest blast radius first):
1. `raybeam` (least active)
2. `ldap-selfservice-password-changer`
3. `ldap-manager` (cut v1.4.1 to validate)

## 9. Out of scope

- **`golib-create-release.yml`** — Go libraries don't have binaries to upload, so atomic-release isn't needed. Leave alone.
- **Backfill of already-published releases** — explicitly unsupported; instructed to cut new patch tag (see §4.4 step 5).
- **Removing `build-go-attest.yml` and `build-container.yml`** — they remain for non-release consumers (CI builds, etc.) and are called transparently from the new orchestrator. No external API change.
- **PR/CI build paths** — this spec covers release only. The existing `ci.yml` workflows that call `build-go-attest.yml` for PR validation continue unchanged.

## 10. Testing plan

Each stage gets validated independently:

**Stage 1 PR (this one):**
- `actionlint` clean.
- `yamllint` no new warnings.
- Manual review of the orchestrator file structure.
- Test on a throwaway repo (or fork): cut a real tag, run the workflow, verify binaries + SBOMs + container ship correctly. Use a `v0.0.1-rc1` style prerelease tag so the run doesn't pollute "Latest".

**Stage 2 (consumer migrations):**
- One consumer at a time. Cut a patch tag (e.g. `raybeam` v0.2.5).
- Verify release page has all expected files: 8 binaries, 8 SBOMs, 17 .bundle files (8+8+1 checksums), checksums.txt.
- Verify cosign verify-blob works for one binary.
- Verify `gh attestation verify` works.
- Verify container image is on GHCR with all expected tags.

**Stage 2 follow-up PR (delete old workflows):**
- Search org for any remaining callers (must be zero).
- Merge.

## 11. Open risks

| Risk | Mitigation |
|---|---|
| `softprops/action-gh-release@v3` rate limits / API errors during multi-file upload | softprops retries internally; failure → release isn't created at all (atomic) → maintainer re-runs after cutting new patch tag. |
| Workflow_dispatch backfill confusion ("why can't I re-release v1.4.0?") | Clear error message in preflight (see §4.4 step 5) tells the maintainer exactly what to do. |
| Consumer repo's `release.yml` was customized | `check-template-drift` PR shows the diff; maintainer can decline if they need custom behavior. The new `release-go-app.yml` exposes enough inputs to cover all current consumer customization. |
| Per-asset signing doubles file count in release UI | Cosmetic; release page sorts alphabetically so `.bundle` files cluster next to their counterparts. Trade-off accepted in §clarifying-question-3. |
| Container build slowness on QEMU emulation gates the release | Already a property of the current pipeline; not introduced here. Mitigation: aggressive buildx GHA cache (already configured). |
