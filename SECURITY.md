# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in any Netresearch repository, please report it responsibly.

**Do NOT open a public issue.**

Instead, use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):

1. Go to the affected repository's **Security** tab
2. Click **"Report a vulnerability"**
3. Fill in the description, steps to reproduce, and impact

We will acknowledge your report within **2 business days** and aim to provide a fix or mitigation within **10 business days**, depending on severity.

## Supported Versions

We actively maintain the latest release of each repository. Security patches are applied to:

- The current major version
- The previous major version (for 6 months after a new major release)

Older versions receive patches only for critical vulnerabilities at our discretion.

## Disclosure Policy

- We follow coordinated disclosure — please give us reasonable time to fix before publishing
- We credit reporters in release notes (unless you prefer anonymity)
- We use GitHub Security Advisories for tracking and publishing fixes

## Handling of Dependency and Code Analysis Findings

This section applies to every repository that does not carry its own policy. The shared workflows in this repository enforce the blocking rules on pull requests; a repository that does not call them names its own checks in its `SECURITY.md` or `CONTRIBUTING.md`.

### Dependencies (software composition analysis)

| Finding | Rule | Enforced by |
| --- | --- | --- |
| Known vulnerability of severity high or critical in a dependency added or changed by a pull request | The pull request cannot pass | `dependency-review.yml` (`fail-on-severity: high`) |
| Known vulnerability in a PHP dependency | The build fails | Composer Audit in `typo3-ci-workflows` `security.yml` |
| Known vulnerability of severity low or medium | Fixed within 60 days after it is published | Renovate update pull requests; maintainers |
| Dependency under a licence that is not OSI-approved or not compatible with the project licence | Not accepted | Review; `license-check.yml` in `typo3-ci-workflows` blocks SSPL and BSL |
| Malicious package | Removed at once; affected releases are reported as an advisory | Maintainers |

### Static analysis (SAST)

| Finding | Rule | Enforced by |
| --- | --- | --- |
| Opengrep finding of severity WARNING or higher | The pull request cannot pass | `typo3-ci-workflows` `security.yml` (`--error --severity WARNING`) |
| CodeQL alert of severity high or critical | Fixed before the next release, or dismissed with a written reason | Code scanning; maintainers |
| Workflow finding from zizmor | Fixed before the next release, or dismissed with a written reason | `zizmor.yml` reports to code scanning; maintainers |
| Secret detected by Betterleaks | The pull request cannot pass; the secret is rotated | `betterleaks.yml` |

### Exceptions

An exception is allowed only when the finding is not exploitable in the project or no fix exists yet. Every exception is written down where the tool reads it, with a reason: `config.audit.ignore` in `composer.json`, a dismissal reason in code scanning, or the tool's own ignore file. Maintainers review all exceptions before each release and remove those that no longer apply.

## Secret Management

- **Storage:** Secrets for CI and releases are stored only as GitHub Actions secrets of the organisation or the repository. Secrets never appear in the repository, in issues or in logs. Workflows use short-lived tokens (the job's `GITHUB_TOKEN`, GitHub App installation tokens, OIDC) where the target service supports them.
- **Access:** Organisation secrets are managed by organisation owners, repository secrets by repository admins; both are listed in [docs/access-roster.md](docs/access-roster.md). Workflows receive a secret only in the job that needs it.
- **Detection:** Betterleaks scans every pull request and push for committed secrets.
- **Rotation:** A secret is rotated at once when it may have been exposed and when a person with access to it leaves the project, and otherwise at least every 12 months.
