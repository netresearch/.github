# go-app Atomic-Release Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single new reusable workflow `release-go-app.yml` that owns the entire go-app release pipeline (preflight → binaries → container → atomic release publish) and replaces the four-job pipeline that's broken by GitHub release immutability. Slim the template caller to ~25 lines, add deprecation notices to the old reusables, and validate end-to-end on `ldap-manager` with a prerelease tag.

**Architecture:** Single orchestrator workflow modelled directly on the proven [`typo3-ci-workflows/release-typo3-extension.yml`](https://github.com/netresearch/typo3-ci-workflows/blob/main/.github/workflows/release-typo3-extension.yml) and [`skill-repo-skill/release.yml`](https://github.com/netresearch/skill-repo-skill/blob/main/.github/workflows/release.yml). Builds upload to GitHub Actions artifacts (not the release). A final `release` job downloads everything, signs each asset (cosign sign-blob → `.bundle`), generates+signs+attests `checksums.txt`, composes the verification block, and creates the release in one `softprops/action-gh-release@v3` call.

**Tech Stack:** GitHub Actions (reusable workflows), `softprops/action-gh-release@v3.0.0`, `cosign` (keyless OIDC), `actions/attest-build-provenance@v4.1.0`, `actions/upload-artifact@v7.0.1` / `actions/download-artifact@v8.0.1`, `anchore/sbom-action@v0.24.0` (SPDX), `docker/build-push-action@v7.1.0`, Trivy SARIF, `aquasecurity/trivy-action@v0.36.0`. All actions pinned by SHA per repo convention.

**Spec:** [`docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md`](../specs/2026-04-26-release-go-app-orchestrator-design.md)

**Working branch:** `feat/release-go-app-orchestrator` (already created off origin/main, spec already committed).

---

## File Structure

| File | Action | Purpose |
|---|---|---|
| `.github/workflows/release-go-app.yml` | **Create** | New 4-job orchestrator (preflight, binaries, container, release). |
| `templates/go-app/.github/workflows/release.yml` | **Replace** | Slim down from ~140 lines (4 reusable callers) to ~25 lines (1 reusable caller). |
| `.github/workflows/create-release.yml` | **Modify** | Add deprecation notice header + `::warning::` annotation in the main job. |
| `.github/workflows/finalize-release.yml` | **Modify** | Same. |
| `docs/design/go-workflow-standardization.md` | **Modify** | Add a "release pipeline" section linking the spec + plan. |

**Established repo conventions to follow:**
- All third-party action references pinned by full SHA + `# vX.Y.Z` comment.
- `step-security/harden-runner@8d3c67de8e2fe68ef647c8db1e6a09f647780f40 # v2.19.0` is always the first step of every job.
- Caller `permissions:` block must mirror the union of declared permissions in the called workflow (GitHub rejects at startup otherwise — see comments in existing `build-go-attest.yml`).
- Files end with a single trailing newline (no trailing blank lines — see CLAUDE.md "YAML Trailing Blank Lines").
- `secrets: inherit` is BANNED — pass secrets explicitly per name (see CLAUDE.md "Reusable Workflow Secrets").
- Signed commits required (`git commit -S --signoff`).

---

## Task 1: Create orchestrator skeleton (workflow metadata + inputs/outputs/permissions)

**Files:**
- Create: `.github/workflows/release-go-app.yml`

This task lays down the `name:`, `on:`, top-level `permissions:`, full `inputs:` block, and `outputs:` block. Jobs are added in subsequent tasks. The file is unrunnable but lintable on its own.

- [ ] **Step 1: Create the orchestrator file with workflow metadata**

```yaml
name: Release (Go application)

# Atomic-release orchestrator for go-app consumers. Builds binaries (matrix),
# container image (multi-arch), then creates the GitHub Release in a single
# softprops/action-gh-release@v3 call with all assets — compatible with
# GitHub's release-immutability enforcement.
#
# Replaces the legacy create-release.yml + finalize-release.yml split, which
# scattered writes to the release across multiple jobs and broke after
# immutability was enforced.
#
# Mirrors typo3-ci-workflows/release-typo3-extension.yml and
# skill-repo-skill/release.yml — both already proven immutability-friendly.

on:
  workflow_call:
    inputs:
      app-name:
        description: "Application binary + container image name (e.g. ldap-manager). Caller typically passes github.event.repository.name."
        required: true
        type: string
      tag:
        description: "Tag to release (e.g. v1.2.3). Defaults to github.ref_name. Override for workflow_dispatch backfills."
        required: false
        type: string
        default: ""
      main-package:
        description: "Go main package path. Set to 'auto' to detect '.' vs './cmd/<repo-name>' from the checked-out tree."
        required: false
        type: string
        default: "auto"
      goos-goarch-matrix:
        description: "JSON array of {target, goos, goarch, goarm?} matrix entries. Defaults to the standard 8-platform set."
        required: false
        type: string
        default: >-
          [
            {"target":"linux-386","goos":"linux","goarch":"386"},
            {"target":"linux-amd64","goos":"linux","goarch":"amd64"},
            {"target":"linux-arm64","goos":"linux","goarch":"arm64"},
            {"target":"linux-armv6","goos":"linux","goarch":"arm","goarm":"6"},
            {"target":"linux-armv7","goos":"linux","goarch":"arm","goarm":"7"},
            {"target":"darwin-amd64","goos":"darwin","goarch":"amd64"},
            {"target":"darwin-arm64","goos":"darwin","goarch":"arm64"},
            {"target":"windows-amd64","goos":"windows","goarch":"amd64"}
          ]
      cgo-enabled:
        description: "CGO_ENABLED build flag."
        required: false
        type: string
        default: "0"
      ldflags:
        description: "Linker flags. Orchestrator appends '-X main.version=<tag> -X main.build=<sha>' automatically."
        required: false
        type: string
        default: "-s -w"
      auto-build-timestamp:
        description: "Append '-X main.buildTime=<HEAD-iso8601>' so workflow_dispatch backfills get a populated timestamp."
        required: false
        type: boolean
        default: true
      setup-bun:
        description: "Install Bun before pre-build-command. Set true for repos that embed Bun-built assets."
        required: false
        type: boolean
        default: true
      bun-version:
        description: "Bun version (when setup-bun=true)."
        required: false
        type: string
        default: "latest"
      setup-node:
        description: "Install Node.js before pre-build-command."
        required: false
        type: boolean
        default: false
      node-version:
        description: "Node.js version (when setup-node=true)."
        required: false
        type: string
        default: "lts/*"
      pre-build-command:
        description: "Shell hook run before 'go build'. Useful for asset embedding (templ generate, bun run build:assets)."
        required: false
        type: string
        default: ""
      container:
        description: "Build the container image. Set false for binary-only releases."
        required: false
        type: boolean
        default: true
      container-platforms:
        description: "Container build platforms (comma-separated)."
        required: false
        type: string
        default: "linux/386,linux/amd64,linux/arm/v6,linux/arm/v7,linux/arm64"
      container-pre-build-command:
        description: "Extra shell run AFTER the orchestrator's automatic binary-artifact download. Empty by default."
        required: false
        type: string
        default: ""
      dockerfile:
        description: "Path to Dockerfile."
        required: false
        type: string
        default: "./Dockerfile"
      prerelease:
        description: "Prerelease override: 'auto' (detect -rc/-alpha/-beta/-pre suffix), 'true', or 'false'."
        required: false
        type: string
        default: "auto"
      make-latest:
        description: "make_latest override: 'auto' (compute from semver vs existing releases), 'true', or 'false'."
        required: false
        type: string
        default: "auto"
      previous-tag:
        description: "Override previous tag used for changelog generation."
        required: false
        type: string
        default: ""
      require-annotated-tag:
        description: "Reject lightweight tags."
        required: false
        type: boolean
        default: true
      sign-artifacts:
        description: "Per-asset cosign sign-blob → .bundle file alongside each binary/SBOM."
        required: false
        type: boolean
        default: true
      include-sbom:
        description: "Generate Syft SBOM (spdx-json) per binary. Uploaded as release asset alongside the binary."
        required: false
        type: boolean
        default: true
# NOTE: outputs block is deferred to Task 5. actionlint statically resolves
# the `value: ${{ jobs.<name>.outputs.<x> }}` references and forward-referencing
# jobs that don't exist yet fails the lint. The full outputs block lands in
# Task 5 alongside the release job.

# CALLER REQUIREMENTS
# ===================
# Caller's job-level permissions: block MUST grant at least:
#
#   permissions:
#     contents: write       # release create, checkout, attestation upload
#     packages: write       # GHCR push (when container=true)
#     id-token: write       # cosign keyless OIDC + attestations
#     attestations: write   # actions/attest-build-provenance
#     security-events: write # Trivy SARIF upload (when container=true)
permissions:
  contents: read

jobs:
  # Jobs are added in subsequent tasks: preflight, binaries, container, release.
  # Placeholder NOOP keeps actionlint happy until the real jobs land.
  noop:
    runs-on: ubuntu-latest
    steps:
      - run: 'echo "skeleton — jobs added in later plan tasks"'
```

- [ ] **Step 2: Run actionlint to verify metadata + inputs are valid YAML**

Run: `cd /home/cybot/projects/netresearch-dotgithub && actionlint .github/workflows/release-go-app.yml`
Expected: clean (no output, exit 0).

- [ ] **Step 3: Verify YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('/home/cybot/projects/netresearch-dotgithub/.github/workflows/release-go-app.yml'))"`
Expected: silent success.

- [ ] **Step 4: Commit**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git add .github/workflows/release-go-app.yml
git commit -S --signoff -m "feat(release-go-app): scaffold orchestrator workflow with inputs/outputs

Add the empty workflow shell with the full inputs and outputs blocks.
Jobs (preflight, binaries, container, release) are added in subsequent
commits to keep each diff reviewable.

Spec: docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md"
```

---

## Task 2: Add `preflight` job

**Files:**
- Modify: `.github/workflows/release-go-app.yml`

Resolves the tag, refuses if release exists, computes prerelease + make-latest flags, generates release notes. Logic is mostly copy-and-trim from the existing `.github/workflows/create-release.yml` — change is the new "refuse if exists" semantics (replaces the create-or-update path).

- [ ] **Step 1: Replace the `noop` placeholder with the `preflight` job**

In `.github/workflows/release-go-app.yml`, replace the entire `jobs:` block with:

```yaml
jobs:
  preflight:
    name: Preflight
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: read
    outputs:
      tag: ${{ steps.tag.outputs.tag }}
      version: ${{ steps.tag.outputs.version }}
      sha: ${{ steps.tag.outputs.sha }}
      is-prerelease: ${{ steps.flags.outputs.is_prerelease }}
      make-latest: ${{ steps.flags.outputs.make_latest }}
      notes: ${{ steps.notes.outputs.notes }}
    steps:
      - name: Harden Runner
        uses: step-security/harden-runner@8d3c67de8e2fe68ef647c8db1e6a09f647780f40 # v2.19.0
        with:
          egress-policy: audit

      - name: Checkout
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
        with:
          fetch-depth: 0
          persist-credentials: false
          ref: ${{ inputs.tag || github.ref }}

      - name: Resolve tag
        id: tag
        env:
          INPUT_TAG: ${{ inputs.tag }}
          REF_NAME: ${{ github.ref_name }}
        run: |
          set -euo pipefail
          TAG="${INPUT_TAG:-$REF_NAME}"
          SEMVER_RE='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'
          if ! [[ "$TAG" =~ $SEMVER_RE ]]; then
            echo "::error::Tag '$TAG' is not a valid vMAJOR.MINOR.PATCH semver (e.g. v1.2.3, v1.2.3-rc1)"
            exit 1
          fi
          SHA=$(git rev-parse "${TAG}^{commit}")
          {
            echo "tag=$TAG"
            echo "version=${TAG#v}"
            echo "sha=$SHA"
          } >> "$GITHUB_OUTPUT"

      - name: Verify annotated tag
        if: inputs.require-annotated-tag
        env:
          TAG: ${{ steps.tag.outputs.tag }}
        run: |
          set -euo pipefail
          TAG_TYPE=$(git cat-file -t "$TAG")
          if [ "$TAG_TYPE" != "tag" ]; then
            echo "::error::Tag $TAG is lightweight (type: $TAG_TYPE). Only annotated/signed tags are allowed."
            exit 1
          fi
          echo "Tag $TAG is annotated (type: $TAG_TYPE)"
          if git tag -v "$TAG" 2>/dev/null; then
            echo "Tag signature verified"
          else
            echo "::warning::Tag $TAG signature not verifiable (signing key not available in CI — informational only)"
          fi

      - name: Refuse if release already exists
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          TAG: ${{ steps.tag.outputs.tag }}
          REPO: ${{ github.repository }}
        run: |
          set -euo pipefail
          if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
            cat <<EOF
          ::error::Release $TAG already exists.

          GitHub releases are immutable — even after deletion, the tag name
          cannot be reused for a new release. To retry: cut a new patch tag
          and re-run, e.g.

              git tag -s v$(echo "${TAG#v}" | awk -F. '{printf "%s.%s.%d", $1, $2, $3+1}') -m "release"
              git push origin v$(echo "${TAG#v}" | awk -F. '{printf "%s.%s.%d", $1, $2, $3+1}')
          EOF
            exit 1
          fi
          echo "No existing release for $TAG — proceeding"

      - name: Compute prerelease and make_latest flags
        id: flags
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          TAG: ${{ steps.tag.outputs.tag }}
          REPO: ${{ github.repository }}
          PRERELEASE_INPUT: ${{ inputs.prerelease }}
          MAKE_LATEST_INPUT: ${{ inputs.make-latest }}
        run: |
          set -euo pipefail

          case "$PRERELEASE_INPUT" in
            true|false)
              IS_PRE="$PRERELEASE_INPUT"
              ;;
            auto)
              case "$TAG" in
                *-rc*|*-alpha*|*-beta*|*-pre*) IS_PRE=true ;;
                *) IS_PRE=false ;;
              esac
              ;;
            *)
              echo "::error::invalid prerelease input '$PRERELEASE_INPUT' (expected: auto|true|false)"; exit 1 ;;
          esac
          echo "is_prerelease=$IS_PRE" >> "$GITHUB_OUTPUT"

          case "$MAKE_LATEST_INPUT" in
            true|false)
              MAKE_LATEST="$MAKE_LATEST_INPUT"
              ;;
            auto)
              if [ "$IS_PRE" = "true" ]; then
                MAKE_LATEST=false
              else
                ALL_TAGS=$(gh api "repos/$REPO/releases" --paginate --jq '.[] | select(.draft==false and .prerelease==false) | .tag_name')
                SEMVER_RE_GREP='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'
                EXISTING=$(printf '%s\n' "$ALL_TAGS" | grep -E "$SEMVER_RE_GREP" || true)
                if [ -z "$EXISTING" ]; then
                  MAKE_LATEST=true
                else
                  HIGHEST=$(printf '%s\n%s\n' "$TAG" "$EXISTING" | grep -v '^$' | sort -V -r | head -n1)
                  if [ "$HIGHEST" = "$TAG" ]; then
                    MAKE_LATEST=true
                  else
                    MAKE_LATEST=false
                    echo "::notice::Tag $TAG is not the highest semver (highest: $HIGHEST) — not marking as latest."
                  fi
                fi
              fi
              ;;
            *)
              echo "::error::invalid make-latest input '$MAKE_LATEST_INPUT' (expected: auto|true|false)"; exit 1 ;;
          esac
          echo "make_latest=$MAKE_LATEST" >> "$GITHUB_OUTPUT"
          echo "Resolved: prerelease=$IS_PRE, make_latest=$MAKE_LATEST"

      - name: Generate release notes
        id: notes
        env:
          TAG: ${{ steps.tag.outputs.tag }}
          PREVIOUS_OVERRIDE: ${{ inputs.previous-tag }}
          REPO: ${{ github.repository }}
        run: |
          set -euo pipefail
          DELIMITER="ghadelim_$(openssl rand -hex 8)"

          if [ -n "$PREVIOUS_OVERRIDE" ]; then
            PREVIOUS_TAG="$PREVIOUS_OVERRIDE"
          else
            PREVIOUS_TAG=$(git tag --list 'v[0-9]*' --sort=-v:refname | grep -v "^${TAG}$" | head -n1 || true)
          fi

          {
            echo "notes<<$DELIMITER"
            if [ -z "$PREVIOUS_TAG" ]; then
              echo "Initial release."
            else
              git log --pretty=format:"- %s" "$PREVIOUS_TAG..$TAG"
              echo ""
              echo ""
              echo "**Full changelog**: https://github.com/$REPO/compare/$PREVIOUS_TAG...$TAG"
            fi
            echo "$DELIMITER"
          } >> "$GITHUB_OUTPUT"
```

- [ ] **Step 2: Run actionlint**

Run: `cd /home/cybot/projects/netresearch-dotgithub && actionlint .github/workflows/release-go-app.yml`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git add .github/workflows/release-go-app.yml
git commit -S --signoff -m "feat(release-go-app): add preflight job

Resolves the tag (semver check), refuses if a release already exists
(immutability — see error message for guidance), computes prerelease +
make_latest flags, and generates release notes. Logic mirrors the
existing create-release.yml's resolution and flag computation."
```

---

## Task 3: Add `binaries` matrix job

**Files:**
- Modify: `.github/workflows/release-go-app.yml`

Per-platform build job. Mirrors `build-go-attest.yml`, MINUS the release-upload steps (we upload to GitHub Actions artifacts instead). Each artifact contains the binary AND its SBOM.

- [ ] **Step 1: Append the `binaries` job to `release-go-app.yml`**

Append immediately after the `preflight` job (before `# Jobs:` end marker if present), maintaining indentation (2 spaces under `jobs:`):

```yaml
  binaries:
    name: Build ${{ matrix.target }}
    needs: preflight
    runs-on: ubuntu-latest
    timeout-minutes: 15
    strategy:
      fail-fast: false
      matrix:
        include: ${{ fromJSON(inputs.goos-goarch-matrix) }}
    permissions:
      contents: read
      id-token: write
      attestations: write
    steps:
      - name: Harden Runner
        uses: step-security/harden-runner@8d3c67de8e2fe68ef647c8db1e6a09f647780f40 # v2.19.0
        with:
          egress-policy: audit

      - name: Checkout at tag
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
        with:
          ref: ${{ needs.preflight.outputs.tag }}
          persist-credentials: false

      - name: Set up Go
        uses: actions/setup-go@4a3601121dd01d1626a1e23e37211e3254c1c06c # v6.4.0
        with:
          go-version-file: go.mod

      - name: Set up Node.js
        if: inputs.setup-node
        uses: actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e # v6.4.0
        with:
          node-version: ${{ inputs.node-version }}

      - name: Set up Bun
        if: inputs.setup-bun
        uses: oven-sh/setup-bun@0c5077e51419868618aeaa5fe8019c62421857d6 # v2.2.0
        with:
          bun-version: ${{ inputs.bun-version }}

      - name: Run pre-build command
        if: inputs.pre-build-command != ''
        env:
          PRE_BUILD_CMD: ${{ inputs.pre-build-command }}
        run: |
          set -euo pipefail
          echo "::group::pre-build-command"
          bash -euo pipefail -c "$PRE_BUILD_CMD"
          echo "::endgroup::"

      - name: Build binary
        env:
          CGO_ENABLED: ${{ inputs.cgo-enabled }}
          GOOS: ${{ matrix.goos }}
          GOARCH: ${{ matrix.goarch }}
          INPUT_GOARM: ${{ matrix.goarm || '' }}
          BINARY_NAME: ${{ inputs.app-name }}-${{ matrix.target }}
          USER_LDFLAGS: ${{ inputs.ldflags }}
          MAIN_PACKAGE: ${{ inputs.main-package }}
          AUTO_BUILD_TIMESTAMP: ${{ inputs.auto-build-timestamp }}
          TAG: ${{ needs.preflight.outputs.tag }}
          SHA: ${{ needs.preflight.outputs.sha }}
        run: |
          set -euo pipefail

          # Compose final ldflags: caller-provided + auto version/build/buildTime injection.
          LDFLAGS="${USER_LDFLAGS} -X main.version=${TAG} -X main.build=${SHA}"

          if [[ "${AUTO_BUILD_TIMESTAMP}" == "true" ]]; then
            if ! BUILD_TS=$(git show -s --format=%cI HEAD); then
              echo "::error::auto-build-timestamp=true but 'git show -s --format=%cI HEAD' failed."
              exit 1
            fi
            if [[ -z "${BUILD_TS}" ]]; then
              echo "::error::auto-build-timestamp=true but 'git show -s --format=%cI HEAD' returned empty."
              exit 1
            fi
            LDFLAGS="${LDFLAGS} -X main.buildTime=${BUILD_TS}"
            echo "auto-build-timestamp: appended -X main.buildTime=${BUILD_TS}"
          fi

          # main-package=auto resolution.
          if [[ "${MAIN_PACKAGE}" == "auto" ]]; then
            REPO_NAME="${GITHUB_REPOSITORY##*/}"
            has_main() {
              local dir="$1" match
              match=$(find "$dir" -maxdepth 1 -type f -name '*.go' ! -name '*_test.go' \
                -exec grep -qE '^package main([[:space:]]|$)' {} \; -print -quit 2>/dev/null)
              [[ -n "$match" ]]
            }
            if has_main .; then
              MAIN_PACKAGE="."
            elif [[ -d "./cmd/${REPO_NAME}" ]] && has_main "./cmd/${REPO_NAME}"; then
              MAIN_PACKAGE="./cmd/${REPO_NAME}"
            else
              echo "::error::main-package=auto: no non-test '.go' file declaring 'package main' at . or ./cmd/${REPO_NAME}/. Set main-package explicitly."
              exit 1
            fi
            echo "main-package auto-detected as ${MAIN_PACKAGE}"
          fi

          # GOARM (only meaningful when GOARCH=arm).
          if [[ "${GOARCH}" == "arm" ]]; then
            if [[ -z "${INPUT_GOARM}" ]]; then
              echo "::error::GOARM is required when GOARCH=arm"
              exit 1
            fi
            export GOARM="${INPUT_GOARM}"
          fi

          # .exe suffix for Windows.
          OUTPUT="${BINARY_NAME}"
          if [[ "${GOOS}" == "windows" ]]; then
            OUTPUT="${BINARY_NAME}.exe"
          fi

          go build -trimpath -ldflags="${LDFLAGS}" -o "${OUTPUT}" "${MAIN_PACKAGE}"

          echo "BINARY=${OUTPUT}" >> "$GITHUB_ENV"

      - name: Generate provenance attestation for binary
        uses: actions/attest-build-provenance@a2bbfa25375fe432b6a289bc6b6cd05ecd0c4c32 # v4.1.0
        with:
          subject-path: ${{ env.BINARY }}

      - name: Generate SBOM (SPDX)
        if: inputs.include-sbom
        uses: anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610 # v0.24.0
        with:
          file: ${{ env.BINARY }}
          format: spdx-json
          output-file: ${{ env.BINARY }}.spdx.json
          upload-artifact: false
          upload-release-assets: false

      - name: Attest SBOM
        if: inputs.include-sbom
        uses: actions/attest-build-provenance@a2bbfa25375fe432b6a289bc6b6cd05ecd0c4c32 # v4.1.0
        with:
          subject-path: ${{ env.BINARY }}.spdx.json

      - name: Upload binary + SBOM as workflow artifact
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: binary-${{ matrix.target }}
          path: |
            ${{ env.BINARY }}
            ${{ env.BINARY }}.spdx.json
          retention-days: 1
          if-no-files-found: error
```

- [ ] **Step 2: actionlint**

Run: `cd /home/cybot/projects/netresearch-dotgithub && actionlint .github/workflows/release-go-app.yml`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git add .github/workflows/release-go-app.yml
git commit -S --signoff -m "feat(release-go-app): add binaries matrix job

Per-platform Go build with build-provenance attestation (binary + SBOM)
and SPDX SBOM generation. Outputs to GitHub Actions artifacts instead
of writing to the release — the release job downloads them later for
the atomic publish.

Matrix is JSON-driven via inputs.goos-goarch-matrix, defaulting to the
standard 8-platform set (3× linux, 2× linux/arm, 2× darwin, 1× windows)."
```

---

## Task 4: Add `container` job

**Files:**
- Modify: `.github/workflows/release-go-app.yml`

Multi-arch container build. Mirrors `build-container.yml`. Adds an artifact-download step at the start to populate `bin/` from the binaries matrix output.

- [ ] **Step 1: Append the `container` job after `binaries`**

Append at indentation level matching the other jobs (2 spaces):

```yaml
  container:
    name: Container image
    needs: [preflight, binaries]
    if: inputs.container
    runs-on: ubuntu-latest
    timeout-minutes: 30
    permissions:
      contents: read
      packages: write
      security-events: write
      id-token: write
      attestations: write
    outputs:
      image-ref: ${{ env.IMAGE_REF }}
      image-digest: ${{ steps.build.outputs.digest }}
      tags: ${{ steps.meta.outputs.tags }}
    env:
      IMAGE_REF: ghcr.io/${{ github.repository_owner }}/${{ inputs.app-name }}
    steps:
      - name: Harden Runner
        uses: step-security/harden-runner@8d3c67de8e2fe68ef647c8db1e6a09f647780f40 # v2.19.0
        with:
          egress-policy: audit

      - name: Checkout at tag
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
        with:
          ref: ${{ needs.preflight.outputs.tag }}
          persist-credentials: false

      - name: Download linux binaries from artifacts
        uses: actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53 # v8.0.1
        with:
          pattern: binary-linux-*
          path: bin/
          merge-multiple: true

      - name: Prepare bin/ (drop SBOMs, mark binaries executable)
        env:
          APP: ${{ inputs.app-name }}
        run: |
          set -euo pipefail
          # download-artifact also drops the SBOMs from the binary artifacts
          # into bin/. They'd shadow the Dockerfile's expected `COPY bin/<app>-linux-*`
          # pattern (e.g. ldap-manager-linux-amd64.spdx.json starts with the
          # same prefix as the binary). Drop them.
          find bin -maxdepth 1 -type f -name '*.spdx.json' -delete
          # And mark all remaining binaries executable.
          find bin -maxdepth 1 -type f -name "${APP}-linux-*" -exec chmod +x {} +
          echo "bin/ contents after prep:"
          ls -lah bin/

      - name: Run container-pre-build-command
        if: inputs.container-pre-build-command != ''
        env:
          PRE: ${{ inputs.container-pre-build-command }}
        run: |
          set -euo pipefail
          bash -euo pipefail -c "$PRE"

      - name: Set up QEMU
        uses: docker/setup-qemu-action@ce360397dd3f832beb865e1373c09c0e9f86d70a # v4.0.0

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@4d04d5d9486b7bd6fa91e7baf45bbb4f8b9deedd # v4.0.0

      - name: Gather Docker metadata
        id: meta
        uses: docker/metadata-action@030e881283bb7a6894de51c315a6bfe6a94e05cf # v6.0.0
        env:
          SEMVER_REF: ${{ needs.preflight.outputs.tag }}
        with:
          images: ${{ env.IMAGE_REF }}
          tags: |
            type=semver,pattern={{version}},value=${{ env.SEMVER_REF }}
            type=semver,pattern={{major}}.{{minor}},value=${{ env.SEMVER_REF }}
            type=semver,pattern={{major}},value=${{ env.SEMVER_REF }}

      - name: Log in to GHCR
        uses: docker/login-action@4907a6ddec9925e35a0a9e82d7399ccc52663121 # v4.1.0
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        id: build
        uses: docker/build-push-action@bcafcacb16a39f128d818304e6c9c0c18556b85f # v7.1.0
        env:
          DOCKERFILE: ${{ inputs.dockerfile }}
        with:
          context: .
          file: ${{ env.DOCKERFILE }}
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          platforms: ${{ inputs.container-platforms }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Trivy vulnerability scanner
        if: ${{ steps.build.outputs.digest != '' }}
        uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0
        with:
          image-ref: ${{ env.IMAGE_REF }}@${{ steps.build.outputs.digest }}
          format: sarif
          output: trivy-results.sarif
          severity: CRITICAL,HIGH
          scanners: vuln,config,secret

      - name: Upload Trivy SARIF
        if: hashFiles('trivy-results.sarif') != ''
        uses: github/codeql-action/upload-sarif@95e58e9a2cdfd71adc6e0353d5c52f41a045d225 # v4.35.2
        with:
          sarif_file: trivy-results.sarif
          category: container-scan

      - name: Install Cosign
        uses: sigstore/cosign-installer@cad07c2e89fa2edd6e2d7bab4c1aa38e53f76003 # v4.1.1

      - name: Sign image (cosign keyless)
        env:
          DIGEST: ${{ steps.build.outputs.digest }}
        run: |
          set -euo pipefail
          cosign sign --yes "${IMAGE_REF}@${DIGEST}"

      - name: Attest container build provenance
        uses: actions/attest-build-provenance@a2bbfa25375fe432b6a289bc6b6cd05ecd0c4c32 # v4.1.0
        with:
          subject-name: ${{ env.IMAGE_REF }}
          subject-digest: ${{ steps.build.outputs.digest }}
          push-to-registry: true
```

- [ ] **Step 2: actionlint**

Run: `cd /home/cybot/projects/netresearch-dotgithub && actionlint .github/workflows/release-go-app.yml`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git add .github/workflows/release-go-app.yml
git commit -S --signoff -m "feat(release-go-app): add container job

Multi-arch container build that downloads linux binaries from the
binaries-matrix workflow artifacts (instead of fetching from the
release, which doesn't exist yet under atomic-release semantics).
Pushes to GHCR, signs (cosign keyless), attests, runs Trivy SARIF
upload."
```

---

## Task 5: Add `release` job (atomic publish) + workflow outputs

**Files:**
- Modify: `.github/workflows/release-go-app.yml`

The atomic publish. Downloads ALL binary+SBOM artifacts, per-asset cosign signing, checksums generation + signing + attestation, body composition, single `softprops/action-gh-release@v3` call.

This task ALSO inserts the workflow-level `outputs:` block (deferred from Task 1) into the `workflow_call:` section between `inputs:` and `# CALLER REQUIREMENTS`. The block is:

```yaml
    outputs:
      tag:
        description: "Resolved release tag (e.g. v1.4.1)."
        value: ${{ jobs.preflight.outputs.tag }}
      version:
        description: "Tag without v prefix (e.g. 1.4.1)."
        value: ${{ jobs.preflight.outputs.version }}
      release-url:
        description: "URL of the published GitHub release."
        value: ${{ jobs.release.outputs.release-url }}
      image-ref:
        description: "Container image reference without tag (e.g. ghcr.io/netresearch/ldap-manager). Empty if container=false."
        value: ${{ jobs.container.outputs.image-ref }}
      image-digest:
        description: "Pushed container image digest (sha256:...). Empty if container=false."
        value: ${{ jobs.container.outputs.image-digest }}
      is-latest:
        description: "Whether the release was marked as 'Latest' (true/false)."
        value: ${{ jobs.preflight.outputs.make-latest }}
```

- [ ] **Step 1: Append the `release` job AND insert the deferred outputs block**

```yaml
  release:
    name: Atomic publish
    needs: [preflight, binaries, container]
    if: >-
      ${{
        always()
        && needs.preflight.result == 'success'
        && needs.binaries.result == 'success'
        && (needs.container.result == 'success' || needs.container.result == 'skipped')
      }}
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
      id-token: write
      attestations: write
    outputs:
      release-url: ${{ steps.publish.outputs.url }}
    steps:
      - name: Harden Runner
        uses: step-security/harden-runner@8d3c67de8e2fe68ef647c8db1e6a09f647780f40 # v2.19.0
        with:
          egress-policy: audit

      - name: Download all binary artifacts
        uses: actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53 # v8.0.1
        with:
          pattern: binary-*
          path: release/
          merge-multiple: true

      - name: List downloaded files
        run: |
          set -euo pipefail
          ls -lah release/

      - name: Generate sha256 checksums
        run: |
          set -euo pipefail
          cd release
          shopt -s nullglob
          files=()
          for f in *; do
            case "$f" in
              checksums.txt|*.bundle) continue ;;
              *) [[ -f "$f" ]] && files+=("$f") ;;
            esac
          done
          if [ ${#files[@]} -eq 0 ]; then
            echo "::error::No assets to checksum — release would ship empty"
            exit 1
          fi
          sha256sum "${files[@]}" > checksums.txt
          cat checksums.txt

      - name: Install Cosign
        if: inputs.sign-artifacts
        uses: sigstore/cosign-installer@cad07c2e89fa2edd6e2d7bab4c1aa38e53f76003 # v4.1.1

      - name: Per-asset cosign sign-blob
        if: inputs.sign-artifacts
        run: |
          set -euo pipefail
          cd release
          shopt -s nullglob
          for f in *; do
            [[ -f "$f" ]] || continue
            case "$f" in
              *.bundle) continue ;;
              *) cosign sign-blob --yes "$f" --bundle "${f}.bundle" ;;
            esac
          done
          ls -lah

      - name: Attest checksums.txt
        uses: actions/attest-build-provenance@a2bbfa25375fe432b6a289bc6b6cd05ecd0c4c32 # v4.1.0
        with:
          subject-path: release/checksums.txt

      - name: Compose release body
        id: body
        env:
          NOTES: ${{ needs.preflight.outputs.notes }}
          APP: ${{ inputs.app-name }}
          OWNER: ${{ github.repository_owner }}
          REPO: ${{ github.repository }}
          VERSION: ${{ needs.preflight.outputs.version }}
          MAJOR_MINOR: ""
          MAJOR: ""
          IMAGE_REF: ${{ needs.container.outputs.image-ref }}
          CONTAINER: ${{ inputs.container }}
          SIGN: ${{ inputs.sign-artifacts }}
        run: |
          set -euo pipefail
          DELIMITER="ghadelim_$(openssl rand -hex 8)"

          MM=$(echo "$VERSION" | awk -F. '{printf "%s.%s", $1, $2}')
          MJ=$(echo "$VERSION" | awk -F. '{print $1}')

          {
            echo "body<<$DELIMITER"
            echo "## Changes"
            echo ""
            echo "$NOTES"
            echo ""
            if [ "$CONTAINER" = "true" ] && [ -n "$IMAGE_REF" ]; then
              echo "## Container image"
              echo ""
              echo '```'
              echo "${IMAGE_REF}:${VERSION}"
              echo "${IMAGE_REF}:${MM}"
              echo "${IMAGE_REF}:${MJ}"
              echo '```'
              echo ""
            fi
            echo "## Verify your download"
            echo ""
            if [ "$SIGN" = "true" ]; then
              echo "Per-asset signatures are bundled. Verify any single file:"
              echo ""
              echo '```bash'
              echo "cosign verify-blob \\"
              echo "  --bundle ${APP}-linux-amd64.bundle \\"
              echo "  --certificate-identity-regexp \"https://github.com/${OWNER}/.*\" \\"
              echo "  --certificate-oidc-issuer \"https://token.actions.githubusercontent.com\" \\"
              echo "  ${APP}-linux-amd64"
              echo '```'
              echo ""
              echo "Verify checksums against the signed manifest:"
              echo ""
              echo '```bash'
              echo "cosign verify-blob \\"
              echo "  --bundle checksums.txt.bundle \\"
              echo "  --certificate-identity-regexp \"https://github.com/${OWNER}/.*\" \\"
              echo "  --certificate-oidc-issuer \"https://token.actions.githubusercontent.com\" \\"
              echo "  checksums.txt"
              echo "sha256sum -c checksums.txt --ignore-missing"
              echo '```'
              echo ""
            fi
            echo "Verify build provenance:"
            echo ""
            echo '```bash'
            echo "gh attestation verify <artifact> --repo ${REPO}"
            echo '```'
            if [ "$CONTAINER" = "true" ] && [ -n "$IMAGE_REF" ]; then
              echo ""
              echo "Verify container image:"
              echo ""
              echo '```bash'
              echo "cosign verify ${IMAGE_REF}:${VERSION} \\"
              echo "  --certificate-identity-regexp \"https://github.com/${OWNER}/.*\" \\"
              echo "  --certificate-oidc-issuer \"https://token.actions.githubusercontent.com\""
              echo "gh attestation verify oci://${IMAGE_REF}:${VERSION} --repo ${REPO}"
              echo '```'
            fi
            echo "$DELIMITER"
          } >> "$GITHUB_OUTPUT"

      - name: Atomic release publish
        id: publish
        uses: softprops/action-gh-release@b4309332981a82ec1c5618f44dd2e27cc8bfbfda # v3.0.0
        with:
          tag_name: ${{ needs.preflight.outputs.tag }}
          name: ${{ needs.preflight.outputs.tag }}
          body: ${{ steps.body.outputs.body }}
          files: release/*
          fail_on_unmatched_files: true
          make_latest: ${{ needs.preflight.outputs.make-latest }}
          prerelease: ${{ needs.preflight.outputs.is-prerelease == 'true' }}
          generate_release_notes: false
```

- [ ] **Step 2: actionlint**

Run: `cd /home/cybot/projects/netresearch-dotgithub && actionlint .github/workflows/release-go-app.yml`
Expected: clean.

- [ ] **Step 3: yamllint (catch any formatting regressions)**

Run: `cd /home/cybot/projects/netresearch-dotgithub && yamllint .github/workflows/release-go-app.yml 2>&1 | grep -vE 'document-start|truthy|line-length|too few spaces before comment' || true`
Expected: empty output (only pre-existing repo-wide warnings remain).

- [ ] **Step 4: Verify trailing newline (no trailing blanks)**

Run: `tail -c 4 /home/cybot/projects/netresearch-dotgithub/.github/workflows/release-go-app.yml | xxd -p`
Expected: ends with `0a` (single newline), NOT `0a0a` (double = trailing blank).

- [ ] **Step 5: Commit**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git add .github/workflows/release-go-app.yml
git commit -S --signoff -m "feat(release-go-app): add atomic publish job

Final job in the orchestrator. Downloads all binary+SBOM artifacts,
performs per-asset cosign sign-blob (bundle pattern from typo3
releaser), generates sha256 checksums + signs + attests, composes the
release body with verification block, and creates the GitHub release
in a single softprops/action-gh-release@v3 call. Immutability-friendly:
once the release is created, no further writes are made.

Closes the orchestrator: preflight → binaries → container → release."
```

---

## Task 6: Slim down the template caller

**Files:**
- Replace: `templates/go-app/.github/workflows/release.yml`

Replace the entire ~140-line multi-job template with a ~30-line single-job caller. Removes references to `create-release.yml`, `build-go-attest.yml`, `build-container.yml`, `finalize-release.yml`.

- [ ] **Step 1: Replace `templates/go-app/.github/workflows/release.yml` entirely**

Overwrite the file with:

```yaml
name: Release

# Atomic-release pipeline for go-app consumers. Delegates the entire
# release lifecycle (preflight → binaries → container → atomic publish)
# to the release-go-app.yml reusable orchestrator.
#
# Per-repo customization: usually nothing. Override inputs (e.g.
# container=false, custom platforms) via the `with:` block below.
#
# This file is template-managed — naming derives from
# github.event.repository.name so the workflow is byte-identical
# across consumers.

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
      # Asset embedding hook for repos that ship Bun-built frontend assets.
      # Gated on package.json so non-frontend repos pay zero overhead beyond
      # the Bun setup itself (~10s per matrix entry).
      pre-build-command: |
        if [ -f package.json ]; then
          bun install --frozen-lockfile
          bun run build:assets
        fi
```

- [ ] **Step 2: actionlint**

Run: `cd /home/cybot/projects/netresearch-dotgithub && actionlint templates/go-app/.github/workflows/release.yml`
Expected: clean.

- [ ] **Step 3: Verify trailing newline**

Run: `tail -c 4 /home/cybot/projects/netresearch-dotgithub/templates/go-app/.github/workflows/release.yml | xxd -p`
Expected: ends with `0a` not `0a0a`.

- [ ] **Step 4: Commit**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git add templates/go-app/.github/workflows/release.yml
git commit -S --signoff -m "feat(go-app-template): adopt atomic-release orchestrator

Replaces the four-job pipeline (create-release → binaries → container
→ finalize) with a single call to the new release-go-app.yml
orchestrator. ~140 lines → ~30 lines.

Consumers pick this up via check-template-drift PRs after this PR
merges to main."
```

---

## Task 7: Add deprecation notices to `create-release.yml` and `finalize-release.yml`

**Files:**
- Modify: `.github/workflows/create-release.yml`
- Modify: `.github/workflows/finalize-release.yml`

Mark both as deprecated. They stay functional during the migration window so any consumer pinned to `@main` keeps working until template-drift PRs land.

- [ ] **Step 1: Add deprecation header to `create-release.yml`**

In `.github/workflows/create-release.yml`, replace the line:
```yaml
name: Create GitHub Release
```
with:
```yaml
name: Create GitHub Release (DEPRECATED — use release-go-app.yml)

# DEPRECATED — 2026-04-26
# ========================
# This workflow is incompatible with GitHub's release-immutability
# enforcement when used in pipelines that upload assets after the
# release is created (the entire go-app pattern).
#
# Use `release-go-app.yml` instead — see:
# docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md
#
# This workflow remains during the migration window so consumers pinned
# to @main keep working until their template-drift PR lands. It will be
# removed in a follow-up PR after no consumers reference it.
```

- [ ] **Step 2: Add `::warning::` annotation as the first step inside the `release` job in `create-release.yml`**

Find the existing `steps:` block under `jobs.release.steps:` and insert this as the FIRST step (before `- name: Harden Runner`):

```yaml
      - name: Deprecation notice
        run: |
          echo "::warning title=Deprecated workflow::create-release.yml is deprecated. Migrate to release-go-app.yml — see docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md"
```

- [ ] **Step 3: Same for `finalize-release.yml`** — replace `name:` and add deprecation step at top of `finalize` job

In `.github/workflows/finalize-release.yml`, replace:
```yaml
name: Finalize Release (checksums + cosign + verification notes)
```
with:
```yaml
name: Finalize Release (DEPRECATED — use release-go-app.yml)

# DEPRECATED — 2026-04-26
# ========================
# Counterpart to the deprecated create-release.yml. The new
# release-go-app.yml orchestrator handles checksums, per-asset signing,
# and verification notes inline in its atomic publish step.
#
# See:
# docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md
```

Then under `jobs.finalize.steps:` insert as the FIRST step:

```yaml
      - name: Deprecation notice
        run: |
          echo "::warning title=Deprecated workflow::finalize-release.yml is deprecated. Migrate to release-go-app.yml — see docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md"
```

- [ ] **Step 4: actionlint both files**

Run: `cd /home/cybot/projects/netresearch-dotgithub && actionlint .github/workflows/create-release.yml .github/workflows/finalize-release.yml`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git add .github/workflows/create-release.yml .github/workflows/finalize-release.yml
git commit -S --signoff -m "chore(release-workflows): mark create-release/finalize-release deprecated

Both workflows are incompatible with GitHub's release-immutability
enforcement when used in multi-job pipelines that upload assets after
the release is created (the entire go-app pattern). Replaced by the
new release-go-app.yml orchestrator.

Kept functional during the migration window; will be removed in a
follow-up PR after consumers' template-drift PRs land."
```

---

## Task 8: Update `docs/design/go-workflow-standardization.md`

**Files:**
- Modify: `docs/design/go-workflow-standardization.md`

Add a "Release pipeline" section pointing to the new spec.

- [ ] **Step 1: Read the existing doc to find the right insertion point**

Run: `head -60 /home/cybot/projects/netresearch-dotgithub/docs/design/go-workflow-standardization.md`

Identify a logical insertion point — likely after the existing top-level intro / architecture section. If the doc has section headings like `## Components` or `## Workflows`, append a new `## Release pipeline (atomic, immutability-friendly)` section after them.

- [ ] **Step 2: Append the new section**

Append at end of file (or insert after the workflows section if one exists):

```markdown
## Release pipeline (atomic, immutability-friendly)

The go-app release pipeline is the single reusable orchestrator
[`release-go-app.yml`](../../.github/workflows/release-go-app.yml).
It runs four jobs sequentially:

1. **preflight** — resolves and validates the tag, refuses if a
   release already exists (immutability), computes prerelease /
   make_latest flags, generates release notes from git log.
2. **binaries** — matrix build (8 platforms by default) with
   build-provenance attestation and SPDX SBOM per binary. Outputs
   to GitHub Actions artifacts (no release writes).
3. **container** — multi-arch GHCR image. Downloads binaries from
   the matrix's artifacts (Dockerfile expects pre-built `bin/<app>-linux-*`).
   Cosign keyless sign + attestation. Trivy SARIF upload.
4. **release** — downloads all binary+SBOM artifacts, per-asset
   cosign sign-blob (`.bundle` files), generates+signs+attests
   `checksums.txt`, composes verification block, creates the
   release in one `softprops/action-gh-release@v3` call.

Design + rationale: [`docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md`](../superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md)
Implementation plan: [`docs/superpowers/plans/2026-04-26-go-app-atomic-release.md`](../superpowers/plans/2026-04-26-go-app-atomic-release.md)

The legacy `create-release.yml` + `finalize-release.yml` reusables
were deprecated 2026-04-26 and will be removed once consumer
template-drift PRs have landed.
```

- [ ] **Step 3: Commit**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git add docs/design/go-workflow-standardization.md
git commit -S --signoff -m "docs(design): add release pipeline section

Document the new release-go-app.yml orchestrator and link to the
spec + plan. Notes the deprecation of create-release.yml +
finalize-release.yml."
```

---

## Task 9: Push branch + open PR

**Files:** none

- [ ] **Step 1: Push the branch**

```bash
cd /home/cybot/projects/netresearch-dotgithub
git push -u origin feat/release-go-app-orchestrator
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "feat(release): atomic-release orchestrator for go-app (release-go-app.yml)" --body "$(cat <<'EOF'
## Summary

Refactors the go-app release pipeline to match the immutability-friendly atomic-release pattern already used by the [skill releaser](https://github.com/netresearch/skill-repo-skill/blob/main/.github/workflows/release.yml) and the [TYPO3 extension releaser](https://github.com/netresearch/typo3-ci-workflows/blob/main/.github/workflows/release-typo3-extension.yml).

Replaces the legacy four-job pipeline (create-release → binaries → container → finalize) — which scatters writes to the release after publication and broke after GitHub started enforcing release immutability — with a single new reusable workflow [`release-go-app.yml`](.github/workflows/release-go-app.yml) that owns the whole lifecycle.

Supersedes #94 (closed band-aid).

## Changes

- **New: `.github/workflows/release-go-app.yml`** — 4-job orchestrator:
  - `preflight` — tag resolution, refuse-if-exists, flag computation, notes generation
  - `binaries` — matrix×8, build + per-binary attestation + SBOM, upload to workflow artifacts
  - `container` — multi-arch GHCR image, downloads binaries from artifacts, signs, attests, Trivy
  - `release` — downloads all artifacts, per-asset cosign sign-blob (`.bundle`), checksums + sign + attest, atomic `softprops/action-gh-release@v3`
- **Slimmed: `templates/go-app/.github/workflows/release.yml`** — ~140 lines → ~30 lines
- **Deprecated: `create-release.yml` + `finalize-release.yml`** — kept functional during migration window; will be removed in a follow-up PR
- **Docs: `docs/design/go-workflow-standardization.md`** — links to spec + plan

## Spec & Plan

- Spec: [`docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md`](docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md)
- Plan: [`docs/superpowers/plans/2026-04-26-go-app-atomic-release.md`](docs/superpowers/plans/2026-04-26-go-app-atomic-release.md)

## Migration

Consumer repos (`ldap-manager`, `raybeam`, `ldap-selfservice-password-changer`) will receive `check-template-drift` PRs replacing their `release.yml` with the new ~30-line template.

## Backward compatibility

- Legacy reusables remain functional (with deprecation warnings) so consumers pinned to `@main` keep working until their drift PR lands.
- Follow-up PR will delete the legacy reusables once no consumers reference them.

## Test plan

- [x] actionlint clean on all changed workflows
- [x] yamllint introduces no new warnings
- [ ] End-to-end test: cut `vX.Y.Z-rc1` prerelease tag on `ldap-manager` against this branch, verify the release page contains all expected files (8 binaries, 8 SBOMs, 17 `.bundle` files, `checksums.txt`, `checksums.txt.bundle`, container reference)
- [ ] `cosign verify-blob --bundle <file>.bundle ...` succeeds on a sample binary
- [ ] `gh attestation verify <file> --repo netresearch/ldap-manager` succeeds
- [ ] Container image visible at `ghcr.io/netresearch/ldap-manager:X.Y.Z-rc1` with all expected tags

EOF
)"
```

- [ ] **Step 3: Capture the PR URL for follow-up**

```bash
gh pr view --json url --jq .url
# Note the URL — used in next task for the E2E test PR description.
```

---

## Task 10: End-to-end test on `ldap-manager` (prerelease tag)

**Files (in a SEPARATE worktree):**
- Modify: `/home/cybot/projects/ldap-manager/main/.github/workflows/release.yml` — point at the PR's branch temporarily

This task validates the orchestrator in real conditions before merging the PR. Uses a prerelease tag (`v1.4.1-rc1`) so it doesn't pollute the "Latest" badge.

- [ ] **Step 1: Set up an isolated worktree for the E2E test**

```bash
cd /home/cybot/projects/ldap-manager
git -C .bare worktree add ../e2e-test-atomic-release main
cd /home/cybot/projects/ldap-manager/e2e-test-atomic-release
git checkout -b test/atomic-release-orchestrator
```

- [ ] **Step 2: Replace `release.yml` with the new template form, pinning to the PR branch**

Overwrite `.github/workflows/release.yml` with:

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
    uses: netresearch/.github/.github/workflows/release-go-app.yml@feat/release-go-app-orchestrator
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

- [ ] **Step 3: Commit and push the test branch**

```bash
cd /home/cybot/projects/ldap-manager/e2e-test-atomic-release
git add .github/workflows/release.yml
git commit -S --signoff -m "test: validate atomic-release orchestrator with rc tag

Temporarily pins .github reusable workflow to the
feat/release-go-app-orchestrator branch for E2E validation.
Will be reverted to @main after the orchestrator PR merges.

Refs: netresearch/.github#<PR-NUMBER>"
git push -u origin test/atomic-release-orchestrator
```

- [ ] **Step 4: Cut a signed prerelease tag pointing at the test branch tip**

```bash
cd /home/cybot/projects/ldap-manager/e2e-test-atomic-release
TEST_TAG="v1.4.1-rc1"
git tag -s "$TEST_TAG" -m "$TEST_TAG (atomic-release validation)"
git push origin "$TEST_TAG"
```

- [ ] **Step 5: Watch the workflow run**

```bash
sleep 10  # let GitHub register the tag push
gh run watch --repo netresearch/ldap-manager --exit-status
# Or: gh run list --repo netresearch/ldap-manager --workflow=release.yml --limit 1
```

Expected: all four jobs succeed (preflight, binaries×8, container, release). Total time ≈ 12-15 min (container build dominates).

- [ ] **Step 6: Verify release page contents**

```bash
gh release view v1.4.1-rc1 --repo netresearch/ldap-manager --json assets --jq '.assets | map(.name) | sort'
```

Expected output (sorted): array containing all of:
- `checksums.txt`, `checksums.txt.bundle`
- `ldap-manager-darwin-amd64`, `ldap-manager-darwin-amd64.bundle`, `ldap-manager-darwin-amd64.spdx.json`, `ldap-manager-darwin-amd64.spdx.json.bundle`
- `ldap-manager-darwin-arm64`, `…` (same pattern)
- `ldap-manager-linux-386`, `…`
- `ldap-manager-linux-amd64`, `…`
- `ldap-manager-linux-arm64`, `…`
- `ldap-manager-linux-armv6`, `…`
- `ldap-manager-linux-armv7`, `…`
- `ldap-manager-windows-amd64.exe`, `ldap-manager-windows-amd64.exe.bundle`, `ldap-manager-windows-amd64.exe.spdx.json`, `ldap-manager-windows-amd64.exe.spdx.json.bundle`

Total: 8 binaries + 8 SBOMs + 16 bundles + checksums.txt + checksums.txt.bundle = **34 files** (or 33 if checksums isn't bundled; check the spec — yes, it IS bundled).

- [ ] **Step 7: Verify the release is marked as prerelease, NOT latest**

```bash
gh release view v1.4.1-rc1 --repo netresearch/ldap-manager --json isPrerelease,isLatest
```

Expected: `{"isPrerelease": true, "isLatest": false}`.

- [ ] **Step 8: Verify cosign signature on a sample binary**

```bash
cd /tmp
mkdir -p cosign-verify && cd cosign-verify
gh release download v1.4.1-rc1 --repo netresearch/ldap-manager \
  --pattern 'ldap-manager-linux-amd64' \
  --pattern 'ldap-manager-linux-amd64.bundle'
cosign verify-blob \
  --bundle ldap-manager-linux-amd64.bundle \
  --certificate-identity-regexp "https://github.com/netresearch/.*" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  ldap-manager-linux-amd64
```

Expected: `Verified OK`.

- [ ] **Step 9: Verify build-provenance attestation**

```bash
gh attestation verify ldap-manager-linux-amd64 --repo netresearch/ldap-manager
```

Expected: `Loaded digest sha256:... ✓ Verification succeeded!`.

- [ ] **Step 10: Verify the container image landed on GHCR with all expected tags**

```bash
gh api 'users/netresearch/packages/container/ldap-manager/versions' --jq '.[0:5] | .[] | .metadata.container.tags'
```

Expected: recent versions include `1.4.1-rc1`, `1.4`, `1` tags pointing at the same digest.

```bash
cosign verify ghcr.io/netresearch/ldap-manager:1.4.1-rc1 \
  --certificate-identity-regexp "https://github.com/netresearch/.*" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
```

Expected: signature verified.

- [ ] **Step 11: Clean up the test (delete test tag + release + branch)**

```bash
# Delete the release first (note: tag becomes immutable as per spec — that's OK, we used a -rc1 suffix)
gh release delete v1.4.1-rc1 --repo netresearch/ldap-manager --yes --cleanup-tag
# Local tag cleanup
cd /home/cybot/projects/ldap-manager/e2e-test-atomic-release
git tag -d v1.4.1-rc1
# Delete test branch
git push origin --delete test/atomic-release-orchestrator
# Remove worktree
cd /home/cybot/projects/ldap-manager
git -C .bare worktree remove e2e-test-atomic-release
```

- [ ] **Step 12: Add E2E results to the orchestrator PR**

Update PR body's test plan checkboxes to checked, post a comment summarizing:

```bash
gh pr comment <PR-URL> --body "$(cat <<'EOF'
## E2E validation: ✅

Cut `v1.4.1-rc1` prerelease tag on `ldap-manager`, pinning to this branch.

- All 4 jobs succeeded (preflight, binaries×8, container, release)
- Release page: 34 expected files (8 binaries + 8 SBOMs + 17 .bundles + checksums.txt)
- Marked prerelease, NOT latest (correct for `-rc1` suffix)
- `cosign verify-blob` succeeded on `ldap-manager-linux-amd64.bundle`
- `gh attestation verify` succeeded
- Container image on GHCR with `1.4.1-rc1`, `1.4`, `1` tags
- `cosign verify` succeeded on container image

Test artifacts cleaned up (release deleted, test branch deleted, worktree removed).

Ready to merge.
EOF
)"
```

---

## Task 11: Self-review and prepare for merge

**Files:** none

- [ ] **Step 1: Re-read the spec, check every requirement is addressed**

Open `docs/superpowers/specs/2026-04-26-release-go-app-orchestrator-design.md`. Walk through each numbered section and confirm:
- §3 architecture matches the implemented DAG
- §4 inputs/outputs all wired
- §5 verification block content matches what `body` step emits
- §6 template caller matches Task 6 output
- §7 deprecation done in Task 7

- [ ] **Step 2: Verify CI is green on the PR**

```bash
gh pr checks <PR-URL>
```
Expected: all checks pass. Address any failures.

- [ ] **Step 3: Address any reviewer comments** (when applicable)

If GitHub Copilot is assigned as reviewer, wait for its review before merging (per CLAUDE.md "PR Merge Requirements"). Resolve all review threads before merging.

- [ ] **Step 4: Merge with a merge commit (preserves signed commits)**

```bash
gh pr merge <PR-NUMBER> --merge --repo netresearch/.github
```

(Per CLAUDE.md "PR Merge Requirements": use `--merge` not `--squash` so the per-task signed commits are preserved on main.)

- [ ] **Step 5: Confirm template-drift PRs auto-open in the three consumer repos**

Within ~24h, verify drift PRs appear in:
```bash
for repo in netresearch/ldap-manager netresearch/raybeam netresearch/ldap-selfservice-password-changer; do
  echo "--- $repo ---"
  gh pr list --repo "$repo" --search 'in:title check-template-drift OR template drift'
done
```

Each drift PR is reviewed and merged by the maintainer (out of scope for this plan).

---

## Self-review checklist (controller)

After all tasks complete, the controller (you) verifies:

- [ ] Spec coverage: every numbered section of the spec has a corresponding task. ✓ (mapped in Task 11 step 1)
- [ ] Placeholder scan: search for `TBD`, `TODO`, `FIXME`, `fill in` in the orchestrator and template — none should appear in the SHIPPED workflow files. (`TODO` may legitimately appear in MIGRATION docs — that's fine.)
- [ ] Type/name consistency: `app-name`, `tag`, `version`, `is-latest`, `is-prerelease`, `release-url`, `image-ref`, `image-digest`, `make-latest`, `notes` — all spelled identically across input → output → consumer.
- [ ] Atomicity: only ONE step in the entire orchestrator writes to the release (`softprops/action-gh-release@v3` in the `release` job).
- [ ] Permissions: caller declarations match the orchestrator's declared permissions.

---

## Out of scope (deferred to follow-up PRs)

- **Stage 2 cleanup PR**: Delete `create-release.yml` and `finalize-release.yml` after consumer drift PRs land. Tracked separately.
- **Consumer drift PR review**: Each consumer's maintainer reviews + merges their own drift PR. Not this plan's job.
- **`golib-create-release.yml`**: Out of scope — Go libraries don't need atomic-release (no binaries to upload).
- **Cutting `ldap-manager` v1.4.1**: Optional follow-up to validate end-to-end on a non-prerelease tag once the orchestrator is on `@main`.
