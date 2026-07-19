# GitHub Actions SHA-Pinning Policy — ADR

Date: 2026-07-19
Status: Accepted

## Context

GitHub Actions and reusable workflows are referenced by a Git ref. A mutable
ref (`@v4`, `@main`) can be repointed by whoever controls the referenced
repository, so a compromised or malicious update reaches every consumer on the
next run. The mitigation is to pin third-party actions to a full-length commit
SHA, which is immutable.

That mitigation does **not** fit org-owned resources. Netresearch reusable
workflows live in [`netresearch/.github`](https://github.com/netresearch/.github)
and [`netresearch/typo3-ci-workflows`](https://github.com/netresearch/typo3-ci-workflows)
and are referenced as `netresearch/<repo>/.github/workflows/<file>.yml@main`.
They are first-party and trusted, and consumers are meant to pick up fixes the
moment they land on `main` — without a digest-bump PR per consumer. Pinning
them to a SHA defeats that and turns every workflow fix into N follow-up PRs.

The two requirements pull in opposite directions, so the policy has to
distinguish the two classes of dependency and the tools have to agree on the
distinction. In July 2026 they did not: Renovate auto-merged two PRs
([sdk-api-universal-messenger#38](https://github.com/netresearch/sdk-api-universal-messenger/pull/38),
[#40](https://github.com/netresearch/sdk-api-universal-messenger/pull/40)) that
SHA-pinned and then digest-bumped a `netresearch/.github` reusable-workflow
reference, against the intent already encoded for zizmor. This ADR records the
policy and how each tool implements it so the tools stay aligned.

## Decision

- **Third-party (external) actions** — pin to a full-length commit SHA, with the
  human-readable version as a trailing comment:
  `uses: actions/checkout@<sha> # v6.0.2`.
- **Org-owned resources** (`netresearch/*` actions and reusable workflows) —
  reference by branch (`@main`), never SHA-pinned. They are first-party and
  version by branch so fixes propagate to all consumers.

The dividing line is ownership, not trust-by-tool: `netresearch/*` is ref-pinned,
everything else is hash-pinned.

## Implementation per player

| Player | Role | Mechanism |
| --- | --- | --- |
| **GitHub** (platform) | How refs are written | Org-owned reusable workflows are called `…@main`; third-party actions carry `@<sha> # <version>`. |
| **zizmor** | Enforcement (audit) | `.github/zizmor.yml` `unpinned-uses` policy — authoritative. |
| **Renovate** | Dependency updates | Not pinned by default; repos opt into the `:pinning` sub-preset, which pins third-party actions and exempts `netresearch/**`. |
| **SonarCloud** | Secondary audit | Rule `githubactions:S7637` flags external actions without a SHA. |
| **OpenSSF Scorecard** | Score signal | `Pinned-Dependencies` check; org-owned ref-pins lower the sub-score by design. |

### GitHub

Reusable workflows are the primary org-owned resource. Consumers reference them
by branch:

```yaml
uses: netresearch/.github/.github/workflows/php-ci.yml@main
```

There is no digest to bump; a fix on `main` in `netresearch/.github` reaches
every consumer on its next run. Third-party `uses:` in the same workflows carry
a SHA plus a version comment.

### zizmor — authoritative enforcement

[zizmor](https://zizmor.sh) audits workflows in CI (the `zizmor.yml` reusable
workflow, wired into the org `ci.yml`). Its `unpinned-uses` rule encodes the
policy directly, and this file is the source of truth:

```yaml
# .github/zizmor.yml
rules:
  unpinned-uses:
    config:
      policies:
        "netresearch/*": ref-pin   # org-owned: branch ref is allowed
        "*": hash-pin               # everything else: full commit SHA required
```

An external action left on a tag fails the audit; an org-owned workflow on
`@main` passes.

### Renovate — no pinning by default, exempt org-owned when opted in

GitHub repos extend the shared preset
[`github>netresearch/renovate-config`](https://github.com/netresearch/renovate-config),
which sets base policy (`config:recommended`, stability delay, deny-lists) and
does **not** pin actions by default. Pinning is enforcement (zizmor), not a
default source of update PRs.

A repo that wants Renovate to maintain third-party SHA pins extends the
`:pinning` sub-preset:

```json
{
  "extends": ["github>netresearch/renovate-config:pinning"]
}
```

The sub-preset bundles `helpers:pinGitHubActionDigests` with the
`netresearch/**` exemption applied **after** it, so the exemption always wins —
Renovate applies the last matching `packageRule`, and the exemption is the last
one in the preset. Repos must **not** extend `helpers:pinGitHubActionDigests`
directly: that pins **all** actions, org-owned included, and a `netresearch/**`
exemption placed only in the shared `default` preset would be overridden by the
later `helpers:pinGitHubActionDigests` and have no effect. The `:pinning`
sub-preset exists precisely so the exemption cannot be omitted or ordered wrong.
Repos on the org default (no pinning) need nothing.

### SonarCloud

SonarCloud raises `githubactions:S7637` ("using external GitHub actions and
workflows without a commit SHA"). It overlaps zizmor's `hash-pin` for external
actions. Where it reports an org-owned reusable workflow, the finding is
accepted (marked safe) per this policy — `netresearch/*` is ref-pinned by
design, not an oversight.

### OpenSSF Scorecard

Scorecard's `Pinned-Dependencies` check rewards SHA-pinning every dependency.
Ref-pinning org-owned resources lowers that sub-score slightly. This is an
accepted trade-off: propagating first-party fixes without per-consumer digest
bumps outweighs a few points on a check whose threat model (untrusted upstream)
does not apply to our own repositories.

## Consequences

- One policy, five tools, no drift: ownership decides ref-pin vs. hash-pin, and
  zizmor is the enforcement of record.
- A repo that wants Renovate action-pinning extends the `:pinning` sub-preset,
  which carries the `netresearch/**` exemption. Extending
  `helpers:pinGitHubActionDigests` directly re-pins org-owned workflows (the
  July 2026 incident:
  [sdk-api-universal-messenger#38](https://github.com/netresearch/sdk-api-universal-messenger/pull/38)/[#40](https://github.com/netresearch/sdk-api-universal-messenger/pull/40)).
- The Scorecard `Pinned-Dependencies` sub-score is intentionally below 10 on
  repos that consume org-owned reusable workflows.
- SonarCloud `githubactions:S7637` findings on `netresearch/*` are triaged as
  safe, not fixed.

## Checklist for a new repository

1. Third-party `uses:` → `@<sha> # <version>`.
2. Org-owned `uses:` → `@main`.
3. If the repo wants Renovate to maintain third-party SHA pins, extend
   `github>netresearch/renovate-config:pinning` — never
   `helpers:pinGitHubActionDigests` directly.
4. Keep `.github/zizmor.yml` `unpinned-uses` policies aligned with this ADR.
