# Exact Go caller cancellation transition

Merge the comparator compatibility change before changing the Go source
templates or consumer callers. It recognizes only complete frozen before/after
documents at five exact paths in `go-lib` and `go-app`: CI, CodeQL, Betterleaks,
Zizmor and Mutation Testing. The two templates currently have identical callers
at those paths. No field is stripped, no path is ignored, and a future canonical
document cannot inherit this compatibility rule. Accepted transitions print a
distinct notice. Other workflow changes still fail drift checks.

The new documents cancel superseded pull-request runs of the same workflow and
PR number. Every other event uses its unique run ID, preserving merge-group,
main/tag push, schedule and manual runs. Mutation already cancelled PR runs;
the transition also stops its previous main-ref group from cancelling a
scheduled or manually requested full run. Triggers, jobs and checks are intact.

Keep the frozen variants until read-only consumer discovery proves migration is
complete; remove them in a separate reviewed change. Do not dispatch a fleet
sync to retire them. Roll back templates safely while both variants remain.

Run all existing and Go-specific negative controls locally:

```sh
python3 -m unittest discover -s scripts -p 'test_drift_compare*.py' -v
```

The existing self-only fixture job runs the added tests. Consumer workflows gain
no runner from these fixtures, no new intentional-drift exemption, and no check
or approval requirement changes.
