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
