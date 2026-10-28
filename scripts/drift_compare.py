#!/usr/bin/env python3
"""Compare a consumer repo's .github/ tree against a template.

Usage:
    drift_compare.py <consumer-dir> <template-dir>

Exit codes: 0 no drift, 1 drift, 2 the template or metadata could not be read,
or a governed path resolved outside the directory it was compared in.

YAML files are compared as parsed documents rather than as bytes. A file that
differs only in comments, key order or whitespace runs exactly the same, so it
is reported as cosmetic instead of failing: governing prose byte-for-byte turned
a reworded comment in the template into a pull request in every consumer, for no
change in behaviour. Prose that has to be authoritative belongs in the reusable
it documents, where it exists once. Non-YAML files stay byte-governed, and a
file that does not parse is treated as drift rather than waved through.

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

IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))

# Complete reviewed Checks documents, before and after PR-only cancellation.
# Do not extend this set to future templates: retire this transition after
# all applicable TYPO3 consumers have moved to the new canonical document.
CHECKS_CONCURRENCY_VARIANTS = frozenset({
    "65c451549abd2ebd6fcdb367d47b855e25b2d48b8315149276caf33af2a01656",
    "9255cc2adf021436f7f52875e0d8e654d1e7aad8dad4dacfb1638f4ab1e18a22",
})


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


def read_intentional(consumer_root: Path) -> set[str]:
    """Paths the consumer has declared as deliberate exceptions."""
    meta = consumer_root / ".github" / "template.yaml"
    if not within(consumer_root, meta) or not meta.is_file():
        return set()
    try:
        doc = yaml.load(meta.read_text(encoding="utf-8"), Loader=WorkflowLoader) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        annotate("error", f"failed to parse template.yaml: {exc}")
        sys.exit(2)

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


def checks_concurrency_transition(template_root: Path, rel: str,
                                 template: object, consumer: object) -> bool:
    """Permit exactly A↔B at the one reviewed TYPO3 Checks path."""
    if template_root.name != "typo3-extension" or rel != ".github/workflows/checks.yml":
        return False
    variants = {semantic_fingerprint(template), semantic_fingerprint(consumer)}
    return variants == CHECKS_CONCURRENCY_VARIANTS


def document_difference(consumer_path: Path, tpl_file: Path, template_root: Path,
                        rel: str) -> str:
    """Distinguish cosmetic equality, the exact transition and ordinary drift."""
    ok_consumer, doc_consumer = parsed(consumer_path)
    ok_template, doc_template = parsed(tpl_file)
    if not ok_consumer or not ok_template:
        return "differing"
    if doc_consumer == doc_template:
        return "cosmetic"
    if checks_concurrency_transition(template_root, rel, doc_template, doc_consumer):
        return "transition"
    return "differing"


def classify(consumer_root: Path, template_root: Path, intentional: set[str]):
    """Classify governed files, preserving the exact transition as its own result."""
    missing: list[str] = []
    differing: list[str] = []
    cosmetic: list[str] = []
    escaped: list[str] = []
    transition: list[str] = []
    documents = {"cosmetic": cosmetic, "differing": differing, "transition": transition}

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

        category = document_difference(consumer_path, tpl_file, template_root, rel)
        documents[category].append(rel)

    return missing, differing, cosmetic, escaped, transition


def main(argv: list[str]) -> int:
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

    intentional = read_intentional(consumer_root)
    if intentional:
        print(f"Intentional-drift paths ({len(intentional)}):")
        for path in sorted(intentional):
            print(f"  - {path}")
    else:
        print("No intentional-drift entries.")

    missing, differing, cosmetic, escaped, transition = classify(
        consumer_root, template_root, intentional
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
        annotate("notice", "Approved Checks PR-concurrency transition (exact reviewed A/B documents).")
        for rel in transition:
            print(f"  ↔ {rel}")

    if escaped:
        annotate(
            "error",
            f"{len(escaped)} governed path(s) resolve outside the directory being compared.",
        )
        for rel in escaped:
            print(f"  ! {rel}", file=sys.stderr)
        return 2

    if not missing and not differing:
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
