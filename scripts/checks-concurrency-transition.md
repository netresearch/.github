# Exact TYPO3 Checks concurrency transition

The drift comparator temporarily recognizes exactly two complete reviewed
versions of `typo3-extension/.github/workflows/checks.yml`: the existing Checks
document and the same document with PR-only cancellation. This lets consumers
move gradually without a period where their normal drift checks all fail.
The canonical template and consumer files are unchanged by this comparator PR.

`CHECKS_CONCURRENCY_VARIANTS` contains SHA-256 fingerprints of the whole parsed
documents. Their source revisions are `0aa7e423` (before) and `193d5bf` (after).
Serialization uses the existing YAML 1.2 loader, sorted compact UTF-8 JSON and
a final newline. Comments, key order and YAML formatting retain their existing
semantic treatment. The tests independently reproduce both frozen fingerprints.

The exception requires both exact fingerprints at the one exact path and
template name. It does not remove a field before comparison, ignore a path,
accept arbitrary concurrency expressions or permit a changed job, permission,
trigger, action reference or gate dependency. A third canonical document also
gets no compatibility with either obsolete version. Matching future documents
still pass the normal comparator; the frozen-canonical fixture makes a new
canonical semantic change a deliberate migration decision while this rule exists.

The fingerprints hash the documents as written, including the harden-runner pin
in the `gate` job, and the frozen set is not extended for a pin bump. Since the
template moved to harden-runner v2.22.3, its document is in neither fingerprint,
so this exception no longer fires for the current template: a consumer without
the concurrency block is drift until it is synced. The comparator's pin
tolerance does not bridge that gap, because a pin difference next to a
concurrency difference stays drift. The fixture rebuilds both documents with the
reviewed pin; a canonical change other than that pin still fails it.

Accepted cross-version comparisons emit an explicit transition notice instead
of describing an execution change as merely cosmetic. Missing, malformed and
escaping files preserve the existing error behavior. Local `check-drift.sh` and
the shared workflow continue calling the same comparator implementation.

Roll out the compatibility comparator first, then the separate canonical
concurrency change. Update consumers gradually through reviewed PRs; do not
start a fleet-wide batch just to remove the mismatch. Once read-only discovery
proves every applicable consumer matches the new canonical document, remove
this exact transition in a separate reviewed change. The pre-existing drift in
`t3x-contexts_wurfl` needs separate reconciliation. Do not remove the transition
early or use a timer that unexpectedly makes remaining old consumers fail.

Restoring the old canonical document while the transition remains is safe for
both allowed versions. Reverting the comparator transition while consumers are
mixed reintroduces drift failures. Neither allowed version changes the test,
security, signature, DCO or review requirements.

Run the offline fixtures with:

```sh
python3 -m unittest discover -s scripts -p 'test_drift_compare.py' -v
```

The independent baseline has no suitable inline fixture job. Therefore this
change adds one small self-only job, filtered to comparator, fixture, runtime,
wrapper and canonical Checks changes. It changes no shared lint interface and
adds no runner to consumer workflows. Its PyYAML test dependency is version-
and hash-pinned; the fleet comparator's existing runtime is unchanged. Count
that one allocation in the organization repository's rollout workload.

## Third document: cancellation only on `synchronize`

Document B cancelled every pull request run in the group. `ready_for_review`
and `reopened` arrive on the same head SHA as the run they cancel, and the
cancelled run's `gate` job still runs under `always()` and fails on its
`cancelled` needs. Each run keeps its own check-run, so the newer run's green
`All security checks` does not replace the red one: the commit's check rollup
stays FAILURE with both listed, and the pull request stayed blocked until the
old run was re-run (t3x-nr-vault run 42936523040, t3x-nr-llm run 42927203718).

Document C cancels only on `synchronize`. Its fingerprint is `HASH_C` in
`test_drift_compare.py`. It
gets no transition: a consumer on A or B is drift until synced, which is
intended, because B is the version with the defect. `test_checks_template.py`
pins the narrowed cancellation and runs the gate step on success, failure,
cancelled and skipped inputs, so the fix cannot drift into a gate that passes
on `cancelled`.

## Fourth document: no concurrency group, a `supersede` job

Document C still let runs for the same head SHA cancel or replace each other
(#592): a late `synchronize` run cancelled a `ready_for_review` run on the same
SHA, a re-run of a `synchronize` run cancelled again because a re-run carries
its original event, a re-run of a draft run replaced the one pending
`ready_for_review` run the group could hold. A concurrency expression sees
only the triggering event, not the pull request's current head, so no group
rule separates these cases; a `reopened` run after a push to the closed pull
request, for example, can carry the old head while the new head arrives as a
separate `synchronize` (seen once in a test repository).

The canonical document drops the group. A `supersede` job reads the pull
request's live head and, only when it equals the run's own head, cancels the
queued and running Checks runs of the same branch for other head SHAs that
were created before its own run. Its
fingerprint is `HASH_D`; C is rebuilt in the tests from D without the job and
with C's concurrency block, and must reproduce `HASH_C`. D gets no transition:
a consumer on A, B or C is drift until synced.

## Fifth document: the head read again before every cancel

Document D read the pull request head once, before listing the runs. A
force-push that made an earlier commit the head again after that read let D
cancel a run for what was then the head, and that run's failed gate check-run
stayed on the head (#594). Document E reads the head again before every cancel
and stops as soon as it is no longer the run's own head, or cannot be read. Its
fingerprint is `HASH_E`; D is rebuilt in the tests from E without that block
and must reproduce `HASH_D`. E gets no transition: a consumer on A, B, C or D
is drift until synced.
