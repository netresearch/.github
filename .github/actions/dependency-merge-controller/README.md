# Dependency merge controller

Use two separate caller workflows, each with one explicit phase. The
`prepare` caller validates the trusted bot event, saves intent and finishes its
entire originating run. The `evaluate` caller handles later allowed review or
workflow completions, including that intent producer's successful completion.
It alone can enqueue. Two jobs in one originating run do not provide this
handoff: replacing a pending evaluator could cancel the still-incomplete origin
and make its artifact permanently untrusted. Neither phase waits for tests,
directly merges, removes queue entries or executes PR code. The legacy
`auto-merge-deps.yml` reusable and all its existing callers remain unchanged.

The new caller invokes
`netresearch/.github/.github/workflows/dependency-merge-controller.yml@main` and
grants `contents: write`, `pull-requests: write`, `actions: read`, `checks: read`
and `statuses: read`, including in observation mode. The required `phase` is
exactly `prepare` or `evaluate`. `caller-workflow` identifies the executing
caller; `intent-workflow` identifies the separate producer. They must match in
prepare and differ in evaluate. Unknown phases and malformed observation flags
fail before API work. Optional merge-App credentials are minted only by an
enforcing evaluator. The producer's eligibility approval uses its GitHub token
and is bound with `commit_id` to the checked head. A configured mint failure
stops the evaluator.

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
records the exact repository, PR, head/branch, mode, policy digest and origin in
`merge-intent-pr-<number>-<headSHA>`. The artifact is retained for seven days.
Reopened events on an unchanged head reuse the verified existing intent.
Intent storage or origin-verification failure blocks automatic continuation.

Completion events re-read the current PR and independently verify artifact
metadata, originating workflow path/event/actor, expiry and head/branch identity.
The origin must be a different, fully completed successful run; there is no
same-run shortcut. REST paths may have the documented `@ref` suffix, but a
foreign prefix, basename-only match or malformed extra suffix is rejected.
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
the same repository, dependency branch and complete event head are serialized
without cancelling an active run. Late old-head events use a different group
and cannot replace the current head's pending evaluator. Fresh-head validation
and the server's expected-head check prevent old evaluations mutating a new
head. Producers have separate repository/PR/head groups, so completion events
cannot replace a pending writer.

The producer uses `pull_request_target` opened/synchronize/reopened. The separate
evaluator uses `pull_request_review` submitted/dismissed and `workflow_run:
completed` with the complete gate-name allowlist **plus the explicit producer
workflow name**, and `renovate/**` / `dependabot/**` branch filters. The producer
completion is accepted only for its exact trusted path, bot actor, same source
repository and successful target event. Keep main, merge-group and ordinary
feature completions out of scope. A completion before origin completion blocks;
the later successful producer completion supplies the progress event.

Set `observe-only: true` for a native pilot. It creates owned intent/receipt
artifacts but never approves a PR or enqueues, and never mints an App token.
The adapter separately rejects all REST writes and GraphQL mutations even when
marked `write=False`. After two fresh ready snapshots it reports
`observed-ready`; an already queued PR is `observed-existing`, never a claimed
insertion. Use distinct observer producer/evaluator paths, for example
`dependency-controller-observe-intent.yml` and `dependency-controller-observe.yml`.
Their path and mode bindings prevent observer artifacts authorizing production.
At cutover disable the observer and obtain fresh intent from the production
producer. Do not change labels, gate inventories, review requirements or history
semantics just to obtain a ready pilot result.

Every phase emits a JSON receipt and summary with phase/mode, event, PR/head/base,
origin run/artifact, policy digest, required inventory, decision and API/mutation
attempt counts. Observer receipts are retained as artifacts for seven days.
An observed ready predicate alone does not establish a native artifact handoff;
the receipt must also show the actual trusted completed origin and artifact.
A new reusable must exist on `@main` before migration. Count the initial producer
and later evaluator allocations, completion/review evaluations and temporary
observer work separately in organization-wide measurements.

Run offline fixtures with:

```sh
python3 -m unittest discover -s .github/actions/dependency-merge-controller -p 'test_*.py' -v
```

The tests use API/mutation stubs, including fresh-pending-to-ready progress,
spoofed/expired intent, newest pending runs, missing or wrong producers,
unreadable/truncated histories, manual removal, races, duplicate events, separate
phase capabilities and strict observation guards. Parsed wiring tests use `yq`,
which the existing permission fixture already requires.
Native event scheduling and the artifact handoff still require a bounded pilot
before consumer activation. A local fixture pass does not prove hosted event
semantics or a measured elapsed-time improvement.

API contracts: [queue mutation](https://docs.github.com/en/graphql/reference/mutations#enqueuepullrequest),
[queue input and expected head](https://docs.github.com/en/graphql/reference/input-objects#enqueuepullrequestinput),
[artifact metadata/download](https://docs.github.com/en/rest/actions/artifacts),
[branch rules](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).
