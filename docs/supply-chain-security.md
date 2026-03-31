# Supply Chain Security

## Defense Layers

| Layer | Mechanism | Scope | When |
|-------|-----------|-------|------|
| 1. Stability delay | Renovate `stabilityDays: 3` | All repos with Renovate | Before PR creation |
| 2. Dependency review | `dependency-review-action` | All repos (org-wide default) | On every PR |
| 3. Package audit | `pnpm audit` / `composer audit` | Repos with audit workflow | On every PR |
| 4. Version deny-list | Renovate org preset `packageRules` | All repos with Renovate | Before PR creation |

## Incident Response Playbook

When a supply chain attack is discovered:

### Immediate (within 1 hour)

1. **Deny-list the version** — add to [`netresearch/renovate-config/default.json`](https://github.com/netresearch/renovate-config):

   ```json
   {
     "description": "Deny-list <package>@<version> — <link to advisory>",
     "matchPackageNames": ["<package>"],
     "allowedVersions": "!=<version>"
   }
   ```

2. **Check which repos are affected** — search lockfiles:

   ```bash
   gh api search/code --method GET \
     -f q='"<package>@<version>" org:netresearch filename:*lock*' \
     --jq '.items[].repository.full_name'
   ```

### Short-term (within 24 hours)

3. **Revert affected repos** — downgrade to last safe version, regenerate lockfiles
4. **Rotate secrets** — if the malicious package could have exfiltrated credentials
5. **Audit CI logs** — check if the malicious code ran in any CI pipeline

### Post-incident

6. **Review auto-merge logs** — identify if/how the compromised version was merged
7. **Update this document** with lessons learned

## Workflow Architecture

```text
netresearch/.github (org-wide)
├── dependency-review.yml    ← runs on ALL repos automatically (org-wide default)
├── auto-merge-deps.yml      ← reusable, called by repos
├── codeql.yml               ← reusable
├── gitleaks.yml             ← reusable (secret scanning)
├── node-audit.yml           ← reusable (Node.js dependency audit)
├── scorecard.yml            ← reusable (OpenSSF Scorecard)
├── greetings.yml            ← reusable
├── labeler.yml              ← reusable
├── lock.yml                 ← reusable
├── pr-quality.yml           ← reusable
└── stale.yml                ← reusable

netresearch/typo3-ci-workflows (TYPO3/PHP-specific)
├── ci.yml                   ← PHP lint, PHPStan, PHPUnit
├── security.yml             ← composer audit + delegates gitleaks to .github
├── docs.yml                 ← TYPO3 documentation rendering
├── e2e.yml                  ← Playwright E2E against TYPO3
├── extended-testing.yml     ← Coverage, mutation, fuzz
├── fuzz.yml                 ← PHP fuzz testing
├── license-check.yml        ← Composer license audit
├── publish-to-ter.yml       ← TER publishing
├── release.yml              ← GitHub release with SBOMs
└── (generic wrappers)       ← backward-compat, delegate to .github

netresearch/renovate-config
└── default.json             ← org-wide Renovate preset (stability delay, deny-lists)
```

## Key Repos

| Repo | Role |
|------|------|
| [`netresearch/renovate-config`](https://github.com/netresearch/renovate-config) | Org-wide Renovate preset (deny-lists, stability delay) |
| [`netresearch/.github`](https://github.com/netresearch/.github) | Org-wide default workflows + generic reusable workflows |
| [`netresearch/typo3-ci-workflows`](https://github.com/netresearch/typo3-ci-workflows) | TYPO3/PHP-specific reusable workflows |
