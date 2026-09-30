# Governance

This document describes how decisions are made in the public repositories of the `netresearch` GitHub organisation, who holds which role, and who has access to sensitive resources. It applies to every repository that does not carry its own `GOVERNANCE.md`. A repository-specific file takes precedence over this one.

## Ownership

Netresearch DTT GmbH owns the organisation and the repositories in it. The company employs the maintainers and is the copyright holder named in the licence files, unless a repository states otherwise.

## Roles

| Role | Who | Responsibilities |
| --- | --- | --- |
| Organisation owner | GitHub organisation owners, listed in [docs/access-roster.md](docs/access-roster.md#organisation-owners) | Grant and revoke access, manage organisation settings, rulesets, organisation secrets and installed GitHub Apps, decide escalated disputes |
| Repository admin | Accounts with the `admin` role on a repository | Repository settings and rulesets, repository secrets, releases and tags, triage of private vulnerability reports |
| Maintainer | Accounts with the `maintain` or `write` role on a repository | Review and merge pull requests, triage issues, prepare releases |
| Contributor | Anyone | Open issues and pull requests; see [CONTRIBUTING.md](CONTRIBUTING.md) |

The list of accounts per active public repository and role is in [docs/access-roster.md](docs/access-roster.md). `scripts/generate-access-roster.sh` regenerates it from the GitHub API.

## Decision making

1. A change is proposed as a pull request. Ideas that need discussion first go into an issue.
2. A maintainer of the repository decides whether the change is merged. The merge requires the checks and reviews that the repository ruleset enforces.
3. If maintainers disagree, they discuss the question in the pull request or issue and look for consensus.
4. If no consensus is reached, an organisation owner decides. The decision and its reason are recorded in the issue or pull request.

Changes to licensing, to this document or to the security policy are decided by an organisation owner.

## Access to sensitive resources

Sensitive resources are: write access to a repository, repository and organisation settings, secrets, release signing, and package registry credentials.

- Organisation membership is granted by an organisation owner. The organisation base permission is `write`, so every member can push to every repository that the rulesets do not protect. Admin and maintain roles are granted per repository.
- Access is revoked when a person leaves Netresearch or no longer works on the project.
- After every change of access, an organisation owner regenerates [docs/access-roster.md](docs/access-roster.md).
- Accounts with access must use two-factor authentication. The organisation enforces this setting.

## Continuity

At least two organisation owners exist at all times, so that access and releases can continue if one person is not available.
