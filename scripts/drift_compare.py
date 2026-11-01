#!/usr/bin/env python3
"""Compare a consumer repo's .github/ tree against a template.

Usage:
    drift_compare.py <consumer-dir> <template-dir>

Exit codes: 0 no drift, 1 drift, 2 the template or metadata could not be read,
or a governed path resolved outside the directory it was compared in, 3 no drift
but a pin was not verified, only when DRIFT_UNVERIFIED_EXIT is set.

YAML files are compared as parsed documents rather than as bytes. A file that
differs only in comments, key order or whitespace runs exactly the same, so it
is reported as cosmetic instead of failing: governing prose byte-for-byte turned
a reworded comment in the template into a pull request in every consumer, for no
change in behaviour. Prose that has to be authoritative belongs in the reusable
it documents, where it exists once. Non-YAML files stay byte-governed, and a
file that does not parse is treated as drift rather than waved through.

A workflow file whose only remaining difference is the commit SHA a `uses:` is
pinned to is reported as a pin notice and does not fail either: Renovate moves
the template and each consumer on separate days, and neither order should turn
an unrelated pull request red. The tolerance covers only a full SHA on both
sides of the same action at the same job or step. Tags, branches, short SHAs and
every other field stay drift, and so does a pin difference next to any of them.
Consumers can set `strict-pins: true` in their .github/template.yaml to make
pin-only differences fail too. The default remains tolerant.

GitHub serves a commit of any fork through every repository of the fork network,
so each tolerated pin is verified online against the action's own branches and
tags (pin_reachability.py). A pin in none of them counts as differing. A pin that
could not be checked, for instance without a token in GITHUB_TOKEN or GH_TOKEN,
is tolerated with a warning that says so, never with the notice of a verified one.
It exits 0 unless DRIFT_UNVERIFIED_EXIT is set: then 3, for a caller that has to
tell a clean result from an unchecked one by the exit code alone.

Intentional-drift is read from <consumer-dir>/.github/template.yaml's
`intentional-drift:` list (each entry either a string path or a mapping with a
`path:` key).

This is the single implementation: both scripts/check-drift.sh and the reusable
.github/workflows/check-template-drift.yml call it, so local and CI verdicts
cannot disagree.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

from pin_reachability import PinChecker, Reachability

IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
STRICT_PINS = "strict-pins"

# Complete reviewed Checks documents, before and after PR-only cancellation.
# Do not extend this set to future templates: retire this transition after
# all applicable TYPO3 consumers have moved to the new canonical document.
CHECKS_CONCURRENCY_VARIANTS = frozenset({
    "65c451549abd2ebd6fcdb367d47b855e25b2d48b8315149276caf33af2a01656",
    "9255cc2adf021436f7f52875e0d8e654d1e7aad8dad4dacfb1638f4ab1e18a22",
})

# Complete reviewed Go caller documents, not a concurrency-field exemption.
# Retire each pair only after read-only discovery proves its consumers migrated.
GO_CONCURRENCY_VARIANTS = {
    ".github/workflows/ci.yml": frozenset({
        "e26ea16b1f76c83d4ce01e1cf6d75d741fd32386bb9e78c202c5f356b01f461b",
        "fb2840a850cf893ad0d40ae0f7ed124220fa03ca897be075d4462ecd38835c45",
    }),
    ".github/workflows/codeql.yml": frozenset({
        "30c54a9bfa68bc900cfd7fd8bbea592bd85ed9deda224728764d94d7f57f0037",
        "9797f59a21fbc962b2e13c2d505898f3daaf31cdaaf5dfa5755e545c54df8e4e",
    }),
    ".github/workflows/betterleaks.yml": frozenset({
        "cd20037e125efd45568f48a30eef21f4cd077e43ed5bb835969ebf915ce4d373",
        "74146965e5e63c00d44d362c26b9597284f52698201870ac4429f14c455205e9",
    }),
    ".github/workflows/zizmor.yml": frozenset({
        "818be610c09a931beb57783e459ed6b5f37dc461d938c4f14ec923b1777df317",
        "f514f2e25efe9684874ba9e9bc2e68e542103399503e037195c5ea87f0024c03",
    }),
    ".github/workflows/mutation.yml": frozenset({
        "280dbde91c0b21db6abb49907ea9f9b5849a546336b7bc90f8da8011d35719ee",
        "0c301b1f2eb746f49d921e1a3f0d9141f6820191a91cf5bf7cd4cb59cc3e5595",
    }),
}

# `owner/repo[/path]@<full commit SHA>`, the only `uses:` form a pin can tolerate.
PINNED_USES = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._/-]+)?)@[0-9a-f]{40}"
)


class WorkflowLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 boolean semantics.

    PyYAML implements YAML 1.1, where bare `on`, `off`, `yes` and `no` resolve
    to booleans. GitHub parses workflows as YAML 1.2, where only `true`/`false`
    are booleans and `on:` stays an ordinary string key. Under 1.1 rules a
    template writing `on:` and a consumer writing `"on":` would compare as
    different documents even though the two run identically.
    """


WorkflowLoader.yaml_implicit_resolvers = {
    first: [
        (tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class MetadataLoader(WorkflowLoader):
    """Reject ambiguous strict-pin declarations without changing workflow loading."""

    def construct_document(self, node):
        """Count the root flag after merges, including aliased key declarations."""
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            flags = sum(key.tag == "tag:yaml.org,2002:str" and key.value == STRICT_PINS
                        for key, _value in node.value)
            if flags > 1:
                raise yaml.constructor.ConstructorError(
                    None, None, "duplicate strict-pins declaration", node.start_mark
                )
        return super().construct_document(node)


def annotate(level: str, message: str) -> None:
    """Emit a workflow annotation in CI, plain text elsewhere."""
    stream = sys.stderr if level == "error" else sys.stdout
    prefix = f"::{level}::" if IN_ACTIONS else f"{level}: "
    print(f"{prefix}{message}", file=stream)


def within(root: Path, candidate: Path) -> bool:
    """True when candidate exists and resolves to a location inside root.

    Both roots come from the command line and every file path is built by
    joining onto them, so each one is checked after resolution: a symlink in
    either tree, or a `..` segment, would otherwise reach a file outside the
    directory the caller asked to compare.
    """
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return False
    return True


def read_metadata(consumer_root: Path) -> dict:
    """Read contained consumer metadata once, rejecting an invalid explicit flag."""
    meta = consumer_root / ".github" / "template.yaml"
    try:
        if not metadata_present(consumer_root, meta):
            return {}
        doc = yaml.load(meta.read_text(encoding="utf-8"), Loader=MetadataLoader)
        if doc is None:
            return {}
        if not isinstance(doc, dict):
            raise ValueError("metadata must be a mapping")
        if STRICT_PINS in doc and type(doc[STRICT_PINS]) is not bool:
            raise ValueError("strict-pins must be a YAML boolean")
        return doc
    except (yaml.YAMLError, OSError, ValueError, KeyError, RecursionError) as exc:
        annotate("error", f"failed to parse template.yaml: {exc}")
        sys.exit(2)


def metadata_present(consumer_root: Path, meta: Path) -> bool:
    """Distinguish ordinary absence from unreadable or escaping metadata paths."""
    try:
        meta.lstat()
    except FileNotFoundError:
        if meta.parent.is_symlink() and not within(consumer_root, meta.parent):
            raise ValueError("metadata directory must resolve inside the consumer")
        return False
    if not within(consumer_root, meta) or not meta.is_file():
        raise ValueError("metadata must be a file inside the consumer")
    return True


def read_intentional(consumer_root: Path, metadata: dict | None = None) -> set[str]:
    """Paths the consumer has declared as deliberate exceptions."""
    doc = read_metadata(consumer_root) if metadata is None else metadata
    entries = doc.get("intentional-drift") or []
    if not isinstance(entries, list):
        # A scalar would otherwise be iterated character by character, turning
        # `intentional-drift: foo` into three one-letter exemptions.
        annotate(
            "error",
            f"intentional-drift must be a list, got {type(entries).__name__}",
        )
        sys.exit(2)

    paths = set()
    for item in entries:
        if isinstance(item, dict) and item.get("path"):
            paths.add(item["path"])
        elif isinstance(item, str):
            paths.add(item)
    return paths


def strict_pin_mode(metadata: dict) -> bool:
    """Report an explicit strict selection while keeping the absent mode silent."""
    strict_pins = metadata.get(STRICT_PINS, False)
    if strict_pins:
        annotate("notice", "Strict pin comparison enabled (strict-pins: true).")
    return strict_pins


def parsed(path: Path) -> tuple[bool, object]:
    """(True, document) for parseable YAML, (False, None) otherwise."""
    if path.suffix not in (".yml", ".yaml"):
        return False, None
    try:
        return True, yaml.load(path.read_text(encoding="utf-8"), Loader=WorkflowLoader)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return False, None


def semantic_fingerprint(document: object) -> str | None:
    """A stable whole-document identity; unsupported YAML values are not allowed."""
    try:
        data = json.dumps(document, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False) + "\n"
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return None


def action_identity(value: object) -> str | None:
    """The part of a full-SHA `uses:` value before the `@`, else None."""
    match = PINNED_USES.fullmatch(value) if isinstance(value, str) else None
    return match[1] if match else None


def adopt_pin(holder: dict, canonical: dict) -> dict:
    """Return holder with the template's `uses:` if only the pinned SHA differs."""
    identity = action_identity(holder.get("uses"))
    if identity is None or identity != action_identity(canonical.get("uses")):
        return holder
    return {**holder, "uses": canonical["uses"]}


def adopt_job_pins(job: dict, canonical: dict) -> dict:
    """Apply adopt_pin to a job and to the steps at the same list positions."""
    aligned = adopt_pin(job, canonical)
    steps, canonical_steps = job.get("steps"), canonical.get("steps")
    if not isinstance(steps, list) or not isinstance(canonical_steps, list):
        return aligned
    if len(steps) != len(canonical_steps):
        return aligned
    return {**aligned, "steps": [
        adopt_pin(step, other) if isinstance(step, dict) and isinstance(other, dict) else step
        for step, other in zip(steps, canonical_steps)
    ]}


def pin_only_difference(consumer: object, template: object) -> bool:
    """True when adopting the template's pins makes the two documents equal.

    The tolerated pins are replaced by the template's own values rather than by
    a placeholder on both sides, so no string in either file can collide with a
    marker. Mappings are copied rather than edited: YAML aliases can share one
    mapping between a step and an `env:`, which must keep its own value.
    """
    if not isinstance(consumer, dict) or not isinstance(template, dict):
        return False
    jobs, canonical_jobs = consumer.get("jobs"), template.get("jobs")
    if not isinstance(jobs, dict) or not isinstance(canonical_jobs, dict):
        return False
    aligned = {
        name: adopt_job_pins(job, canonical_jobs[name])
        if isinstance(job, dict) and isinstance(canonical_jobs.get(name), dict) else job
        for name, job in jobs.items()
    }
    return {**consumer, "jobs": aligned} == template


def changed_pins(consumer: dict, template: dict) -> list[str]:
    """The consumer's `uses:` values differing from a document pin_only_difference accepted."""
    changed = []
    for name, job in consumer["jobs"].items():
        canonical = template["jobs"][name]
        if not isinstance(job, dict):
            continue
        pairs = [(job, canonical)]
        if isinstance(job.get("steps"), list):
            pairs += zip(job["steps"], canonical["steps"])
        changed += [holder["uses"] for holder, other in pairs
                    if isinstance(holder, dict) and holder.get("uses") != other.get("uses")]
    return changed


def checks_concurrency_transition(template_root: Path, rel: str,
                                 template: object, consumer: object) -> bool:
    """Permit exactly A↔B at the one reviewed TYPO3 Checks path."""
    if template_root.name != "typo3-extension" or rel != ".github/workflows/checks.yml":
        return False
    variants = {semantic_fingerprint(template), semantic_fingerprint(consumer)}
    return variants == CHECKS_CONCURRENCY_VARIANTS


def go_concurrency_transition(template_root: Path, rel: str,
                              template: object, consumer: object) -> bool:
    """Permit only the frozen A↔B callers in the two reviewed Go templates."""
    if template_root.name not in {"go-lib", "go-app"}:
        return False
    allowed = GO_CONCURRENCY_VARIANTS.get(rel)
    return allowed is not None and {
        semantic_fingerprint(template), semantic_fingerprint(consumer)
    } == allowed


def document_difference(consumer_path: Path, tpl_file: Path, template_root: Path,
                        rel: str, strict_pins: bool = False) -> str:
    """Distinguish cosmetic equality, the transitions, pins and ordinary drift.

    The transition fingerprints always hash the documents as written, pins
    included, and are consulted first. Their A and B documents differ in
    concurrency, so no pair can be both a transition and a pin-only difference.
    """
    ok_consumer, doc_consumer = parsed(consumer_path)
    ok_template, doc_template = parsed(tpl_file)
    if not ok_consumer or not ok_template:
        return "differing"
    if doc_consumer == doc_template:
        return "cosmetic"
    if checks_concurrency_transition(template_root, rel, doc_template, doc_consumer):
        return "transition"
    if go_concurrency_transition(template_root, rel, doc_template, doc_consumer):
        return "transition"
    if (not strict_pins and rel.startswith(".github/workflows/")
            and pin_only_difference(doc_consumer, doc_template)):
        return "pinned"
    return "differing"


def classify(consumer_root: Path, template_root: Path, intentional: set[str],
             strict_pins: bool = False):
    """Classify governed files, preserving the exact transition as its own result."""
    missing: list[str] = []
    differing: list[str] = []
    cosmetic: list[str] = []
    escaped: list[str] = []
    transition: list[str] = []
    pinned: list[str] = []
    documents = {"cosmetic": cosmetic, "differing": differing, "transition": transition,
                 "pinned": pinned}

    governed = sorted(p for p in (template_root / ".github").rglob("*") if p.is_file())
    for tpl_file in governed:
        rel = str(tpl_file.relative_to(template_root))

        # .github/template.yaml is per-consumer metadata, never in the diff set.
        if rel == ".github/template.yaml" or rel in intentional:
            continue

        if not within(template_root, tpl_file):
            escaped.append(rel)
            continue

        consumer_path = consumer_root / rel
        if not consumer_path.is_file():
            missing.append(rel)
            continue
        if not within(consumer_root, consumer_path):
            escaped.append(rel)
            continue
        if consumer_path.read_bytes() == tpl_file.read_bytes():
            continue

        category = document_difference(consumer_path, tpl_file, template_root, rel, strict_pins)
        documents[category].append(rel)

    return missing, differing, cosmetic, escaped, transition, pinned


def report_transitions(transition: list[str]) -> None:
    """Print approved transition notices and their paths in the existing order."""
    if ".github/workflows/checks.yml" in transition:
        annotate("notice", "Approved Checks PR-concurrency transition (exact reviewed A/B documents).")
    if any(rel in GO_CONCURRENCY_VARIANTS for rel in transition):
        annotate("notice", "Approved Go PR-concurrency transition (exact reviewed A/B documents).")
    for rel in transition:
        print(f"  ↔ {rel}")


def verify_pins(pinned: list[str], consumer_root: Path, template_root: Path):
    """Sort tolerated files into verified, unverified and unreachable, reporting each pin."""
    checker = PinChecker.from_environment()
    verified: list[str] = []
    unverified: list[str] = []
    unreachable: list[str] = []
    for rel in pinned:
        states = set()
        pins = changed_pins(parsed(consumer_root / rel)[1], parsed(template_root / rel)[1])
        for uses in dict.fromkeys(pins):
            repository = "/".join(action_identity(uses).split("/")[:2])
            state, reason = checker.check(repository, uses.rsplit("@", 1)[1])
            states.add(state)
            if state is Reachability.UNVERIFIED:
                annotate("warning", f"{rel}: pin {uses} was not verified against {repository}: "
                                    f"{reason}. It is tolerated without that check.")
            elif state is Reachability.UNREACHABLE:
                annotate("error", f"{rel}: pin {uses} is not a commit of {repository}: {reason}. "
                                  "It may be an impostor commit from the fork network.")
        if Reachability.UNREACHABLE in states:
            unreachable.append(rel)
        elif Reachability.UNVERIFIED in states:
            unverified.append(rel)
        else:
            verified.append(rel)
    return verified, unverified, unreachable


def main(argv: list[str]) -> int:
    """Compare governed files, report their classifications and return the verdict."""
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    # Resolved once, so every path built by joining onto them can be checked
    # against a fixed, absolute boundary.
    consumer_root = Path(argv[1]).resolve()
    template_root = Path(argv[2]).resolve()

    if not template_root.is_dir():
        annotate("error", f"template dir {template_root} does not exist")
        return 2

    # Without this, a mis-pointed template directory governs zero files and the
    # run ends in "No drift detected" — a failure reported as an all-clear.
    governed_root = template_root / ".github"
    if not governed_root.is_dir() or not any(
        p.is_file() for p in governed_root.rglob("*")
    ):
        annotate(
            "error",
            f"{governed_root} holds no files — refusing to report a clean result "
            "for a template that governs nothing.",
        )
        return 2

    metadata = read_metadata(consumer_root)
    intentional = read_intentional(consumer_root, metadata)
    strict_pins = strict_pin_mode(metadata)
    if intentional:
        print(f"Intentional-drift paths ({len(intentional)}):")
        for path in sorted(intentional):
            print(f"  - {path}")
    else:
        print("No intentional-drift entries.")

    missing, differing, cosmetic, escaped, transition, pinned = classify(
        consumer_root, template_root, intentional, strict_pins
    )

    if cosmetic:
        annotate(
            "notice",
            f"{len(cosmetic)} file(s) differ only in comments, key order or "
            "whitespace and parse identically — not drift.",
        )
        for rel in cosmetic:
            print(f"  ~ {rel}")

    if transition:
        report_transitions(transition)

    verified, unverified, unreachable = verify_pins(pinned, consumer_root, template_root)
    differing += unreachable

    if verified:
        annotate(
            "notice",
            f"{len(verified)} workflow file(s) differ only in the commit SHA an "
            "action is pinned to, verified as reachable from the action's own "
            "branches or tags — not drift.",
        )
        for rel in verified:
            print(f"  ≈ {rel}")

    for rel in unverified:
        print(f"  ? {rel}")

    if escaped:
        annotate(
            "error",
            f"{len(escaped)} governed path(s) resolve outside the directory being compared.",
        )
        for rel in escaped:
            print(f"  ! {rel}", file=sys.stderr)
        return 2

    if not missing and not differing:
        if unverified:
            annotate("warning", f"No other drift, but {len(unverified)} workflow file(s) "
                                "hold a pin that was not verified.")
            return 3 if os.environ.get("DRIFT_UNVERIFIED_EXIT") else 0
        annotate("notice", "No drift detected.")
        return 0

    annotate("error", f"Template drift detected vs. {template_root}.")
    if missing:
        print(
            "\nMissing files (template has them, consumer does not):", file=sys.stderr
        )
        for rel in missing:
            print(f"  - {rel}", file=sys.stderr)
    if differing:
        print("\nDiffering files:", file=sys.stderr)
        for rel in differing:
            print(f"  - {rel}", file=sys.stderr)
    print(
        "\nResolve by running scripts/sync-template.sh against this repo,\n"
        "or add explicit exceptions to .github/template.yaml's intentional-drift:.",
        file=sys.stderr,
    )

    # Show the diffs so a reviewer can see what actually changed.
    for rel in differing:
        print(f"\n=== diff: {rel} ===", file=sys.stderr)
        sys.stderr.writelines(
            difflib.unified_diff(
                (consumer_root / rel)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines(keepends=True),
                (template_root / rel)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines(keepends=True),
                fromfile=f"consumer/{rel}",
                tofile=f"template/{rel}",
            )
        )

    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
