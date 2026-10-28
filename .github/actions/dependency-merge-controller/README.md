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

A required producer discovered by a check's strict single-run fallback also
participates in workflow readiness, even if it appeared after the workflow-list
read. A known newer pending/failed run blocks despite green named checks. The
initial complete workflow inventory and its pending/failed observations remain
intact; a later cache observation cannot turn that snapshot's negative evidence
green. This uses already fetched metadata and adds no API requests.

Each complete snapshot lists repository workflow runs once per explicit
head/test-merge SHA and shares that snapshot's validated producer metadata.
Missing producer IDs use the single-run API. Check timestamps or attempt
evidence newer than cached run metadata require one coherent fresh single-run
read or block; a refreshed pending retry also updates workflow readiness.
Terminal checks use their completion timestamp, which must be present; active
checks use their start timestamp. A skipped check can have a synthetic start
later than its completion and does not become evidence of a newer retry.
The next snapshot discards the entire cache. PR metadata and the first history,
review and thread pages share one GraphQL query, with independent continuation
cursors and a ten-page limit for each connection, including its first page.
Both full fresh ready snapshots remain mandatory before enqueue.

Actions check provenance also requires positive server suite IDs and exact
check/run/collection SHA equality. A check's name and details link cannot prove
its producer: its exact ID must appear in the run's server-owned jobs through
`check_run_url`, with matching name, run, head and status. A job suffix, if
present in the details link, must identify that actual job. Required gates must
belong to the newest expected workflow run and its current server `latest`
jobs. The controller's own-job exception requires the same verified current
membership. Foreign-App checks retain their generic additional-check semantics.

Each snapshot caches `latest` jobs once per referenced Actions run. A historical
check absent there uses one cached `all` inventory; old and new checks remain
available, but historical success cannot fill a missing current gate. Failed-job
retries can clone successful jobs with new IDs and old execution timestamps.
Current server membership is authoritative; raw check attempt numbers or
execution after retry start are not required. Complete stable counts, unique
job/check identities and exact advancing pagination are mandatory. The jobs
endpoint has no workflow-search 1,000-result restriction; the ten-page/response
safety bounds still fail closed rather than accepting incomplete inventory.
Each normalized membership proof records its source run revision. If a later
check refreshes that run within the same snapshot, earlier proofs must still
match the final run cache or the snapshot blocks without additional reads.
The proof revision is distinct from a carried job's older originating attempt.

Repository-wide workflow search has a shared 1,000-result ceiling for each SHA,
rather than a separate ceiling for each required workflow. A count at that
ceiling, missing/changing count, incomplete pagination or duplicate run identity
blocks instead of treating a truncated list as complete. This can block a SHA
with many workflow runs earlier than the per-workflow lookup did. The one-page
fixture with two SHAs and ten producer runs uses eight base adapter requests
plus ten current job inventories: 18 per snapshot. When all ten have historical
checks, ten cached `all` reads bring it to 28. Fallback run reads and extra pages
add their real requests. A retained 24-check native failed-job retry binds all
records through two cached job reads, preserving its 11 cloned successes.
This is an API-demand measurement, not a hosted elapsed-time improvement.
Unreferenced sane `dynamic/` paths with event `dynamic` remain part of the
bounded inventory but cannot enter the trusted workflow cache. Unknown or
malformed workflow identities block. If a check references a dynamic producer,
its single-run lookup retains strict workflow-path validation and blocks.

The producer uses `pull_request_target` opened/synchronize/reopened. The separate
evaluator uses `pull_request_review` submitted/dismissed and `workflow_run:
completed` with the complete gate-name allowlist **plus the explicit producer
workflow name**, and `renovate/**` / `dependabot/**` branch filters. The producer
completion is accepted only for its exact trusted path, bot actor, same source
repository and successful target event. Keep main, merge-group and ordinary
feature completions out of scope. A completion before origin completion blocks;
the later successful producer completion supplies the progress event.

An opt-in evaluator may additionally subscribe to `pull_request_target` types
`unlabeled`, `ready_for_review`, `edited` and `auto_merge_enabled`. Mirror the
shared reusable's metadata job filter in the caller before allocation: require
a same-repository Renovate/Dependabot dependency branch, removal of exactly
`deps-no-automerge`/`deps-major`, ready draft state, an actual changed
`changes.base.ref.from`, or human User auto-merge activation respectively.
Ordinary labels/title/body edits and other PRs must not allocate a controller.
These events execute only `evaluate` in its separate caller and share its full
head concurrency group. They never prepare intent or post an approval. A PR
that had no eligible bot intent before a label/draft change still needs valid
intent or the existing independently verified human recovery history.

Resolving the last thread or completing an external/unlisted check does not
have a guaranteed supported evaluator wakeup. For bounded manual recovery,
re-run all jobs on the latest actual evaluator run for that same current head.
It re-reads the full policy and preserves intent/provenance/removal checks; an
old-head run cannot be repointed. Observer receipt artifact names include run
ID and attempt. A rerun does not create a trusted bot origin if none exists.
Do not describe this as complete automatic liveness or add an unsupported
Actions review-thread trigger. Native auto-merge can enqueue before the
evaluator runs and enforces the server's configured rules, which may be weaker
than the controller inventory and approval minimum. Human activation is not a
substitute for full controller readiness. `GITHUB_TOKEN` metadata activity also
does not guarantee a follow-on workflow run.

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
