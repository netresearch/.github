# Dependency merge controller

This opt-in reusable evaluates a dependency PR when an allowed event arrives,
then exits with `waiting`, `blocked` or a confirmed `queued` result. It never
waits for tests, directly merges a PR, removes a queue entry or executes PR code.
Existing `auto-merge-deps.yml` callers and their permissions are unchanged.

The new caller invokes
`netresearch/.github/.github/workflows/dependency-merge-controller.yml@main` and
grants `contents: write`, `pull-requests: write`, `actions: read`, `checks: read`
and `statuses: read`. Optional merge-App credentials affect only the final
enqueue mutation. Reads and the initial eligibility approval use the caller's
GitHub token. A configured App-token mint failure stops the controller.

The trusted caller supplies a version-1 JSON policy:

```json
{
  "version": 1,
  "minimum_approvals": 2,
  "safe_updates_only": false,
  "required_checks": [
    {"name": "ci / All CI checks", "app_id": 15368,
     "workflow": "ci.yml", "events": ["pull_request"]}
  ]
}
```

Supply the complete repository inventory, including enabled security, DCO,
E2E, drift and harness aggregates. The one-entry example is not nr-vault's
inventory. Each entry binds its exact check name to the GitHub Actions App,
caller workflow filename and permitted producer events. Unknown active branch
rule contexts block the controller. Additional failed or pending checks and
commit statuses also block; only explicitly listed `advisory_checks` may be
ignored. The native queue remains the final enforcement of branch rules,
Code Owners, signatures and review requirements. Reconcile classic protection
separately with administrative read access before first adoption.

Only a current same-repository Renovate/Dependabot PR on a `renovate/` or
`dependabot/` branch can proceed. Existing major/no-automerge labels and safe
update rules are retained. The trusted initial `pull_request_target` event
records the exact repository, PR, head, policy digest and originating run in
`merge-intent-pr-<number>-<headSHA>`. The artifact is retained for seven days.
Reopened events on an unchanged head reuse the verified existing intent.
Intent storage or origin-verification failure blocks automatic continuation.

Completion events re-read the current PR and independently verify artifact
metadata, originating workflow path/event/actor, expiry and head identity.
The archive is bounded, read as JSON and never executed or extracted to disk.
Authentication is not forwarded to the artifact's signed storage redirect.
Review events can also trigger a fresh evaluation. Fully paginated queue and
auto-merge history makes any removal/deactivation block until a later explicit
human activation. A new bot commit does not override that decision. Missing,
expired, ambiguous or truncated evidence stays blocked; manual GitHub
activation remains available as the controlled recovery route.

Required workflow runs and individual checks are both evaluated: an older
green check cannot satisfy a newer queued workflow whose jobs have not appeared
yet. Two fresh reads check head/base, complete gate results, current-head
approvals and resolved threads before one `enqueuePullRequest` mutation with
`expectedHeadOid`. One subsequent read confirms the entry. Controller runs for
the same dependency branch are serialized without cancelling an active run.

Use `pull_request_target` opened/synchronize/reopened, `pull_request_review`
submitted/dismissed and `workflow_run: completed` with a narrow workflow-name
allowlist and dependency branch filters. Keep both main and merge-group
completions out of the caller's event scope. A new reusable must be available
on `@main` before migrating consumers. Count short controller allocations
separately when measuring organization-wide runner savings.

Run offline fixtures with:

```sh
python3 -m unittest discover -s .github/actions/dependency-merge-controller -p 'test_*.py' -v
```

The tests use API/mutation stubs, including fresh-pending-to-ready progress,
spoofed/expired intent, newest pending runs, missing or wrong producers,
unreadable/truncated histories, manual removal, races and duplicate events.
Native event scheduling and the artifact handoff still require a bounded pilot
before consumer activation. A local fixture pass does not prove hosted event
semantics or a measured elapsed-time improvement.

API contracts: [queue mutation](https://docs.github.com/en/graphql/reference/mutations#enqueuepullrequest),
[queue input and expected head](https://docs.github.com/en/graphql/reference/input-objects#enqueuepullrequestinput),
[artifact metadata/download](https://docs.github.com/en/rest/actions/artifacts),
[branch rules](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).
