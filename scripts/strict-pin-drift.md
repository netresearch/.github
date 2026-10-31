# Strict pin drift for a consumer

The template drift comparator normally reports a full-SHA difference for the
same action as a notice. A consumer requiring the template's exact action pin
can add this exact YAML boolean to its existing `.github/template.yaml`:

```yaml
template: typo3-extension
strict-pins: true
intentional-drift:
  - .github/workflows/ci.yml
```

Keep the consumer's existing template name and intentional paths. Metadata is
per consumer, excluded from governed content, and preserved by
`sync-template.sh`; opting in requires no managed workflow change. No template
or other consumer is opted in automatically.

With `strict-pins: true`, a pin-only difference at a job or step is ordinary
drift and exits 1. The comparator prints `Strict pin comparison enabled` so a
local or hosted log identifies the selected mode. Parsed YAML equality,
explicit intentional paths, exact frozen concurrency transitions and all other
drift rules retain their existing behavior. This mode compares against the
template's pin; it does not independently establish a SHA's provenance.

Absent metadata, an empty file, a YAML-null document, an absent flag or explicit
`strict-pins: false` preserves the default notice behavior. An explicit flag
must be a YAML boolean: strings, numbers, null and collections fail with exit 2.
Explicit boolean tags use their parsed boolean value; bare `yes`/`no` stay strings.
Duplicate root flags also fail, including duplicates introduced by aliases or
merge keys. A malformed, non-mapping, unreadable, broken-symlink or escaping
metadata file fails with exit 2. An unambiguous alias, merge or contained
metadata symlink remains supported. Workflow YAML loading is unchanged.

Both entry points read the same metadata and use the same comparator:

```bash
bash scripts/check-drift.sh /path/to/consumer /path/to/templates/typo3-extension
python3 scripts/drift_compare.py /path/to/consumer /path/to/templates/typo3-extension
```

The complete default and strict CLI fixtures run in the existing required
`Dependency controller policy fixtures` job on pull requests, merge groups and
main. That job retains its controller, permission and legacy check commands,
runner, permissions and three-minute timeout. Drift fixtures use the already
hash-pinned YAML test runtime in a step-local directory. The separate template
transition workflow retains its existing coverage.

Consumer adoption follows qualification of the owning source on actual main,
including a successful required fixture check on the combined merge-group
tree before merge. Then verify the real canonical consumer passes and a
temporary arbitrary-pin mutation fails before publishing the consumer change.
First-party reusable references remain at `@main`.
