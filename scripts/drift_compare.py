#!/usr/bin/env python3
"""Compare a consumer repo's .github/ tree against a template.

Usage:
    drift_compare.py <consumer-dir> <template-dir>

Exit codes: 0 no drift, 1 drift, 2 the template or metadata could not be read.

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
import os
import sys
from pathlib import Path

import yaml

IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))


def annotate(level: str, message: str) -> None:
    """Emit a workflow annotation in CI, plain text elsewhere."""
    stream = sys.stderr if level == "error" else sys.stdout
    prefix = f"::{level}::" if IN_ACTIONS else f"{level}: "
    print(f"{prefix}{message}", file=stream)


def read_intentional(consumer_root: Path) -> set[str]:
    """Paths the consumer has declared as deliberate exceptions."""
    meta = consumer_root / ".github" / "template.yaml"
    if not meta.is_file():
        return set()
    try:
        doc = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        annotate("error", f"failed to parse template.yaml: {exc}")
        sys.exit(2)

    paths = set()
    for item in doc.get("intentional-drift") or []:
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
        return True, yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return False, None


def classify(consumer_root: Path, template_root: Path, intentional: set[str]):
    """Split the governed files into missing, differing and cosmetic."""
    missing: list[str] = []
    differing: list[str] = []
    cosmetic: list[str] = []

    governed = sorted(p for p in (template_root / ".github").rglob("*") if p.is_file())
    for tpl_file in governed:
        rel = str(tpl_file.relative_to(template_root))

        # .github/template.yaml is per-consumer metadata, never in the diff set.
        if rel == ".github/template.yaml" or rel in intentional:
            continue

        consumer_path = consumer_root / rel
        if not consumer_path.is_file():
            missing.append(rel)
            continue
        if consumer_path.read_bytes() == tpl_file.read_bytes():
            continue

        ok_consumer, doc_consumer = parsed(consumer_path)
        ok_template, doc_template = parsed(tpl_file)
        if ok_consumer and ok_template and doc_consumer == doc_template:
            cosmetic.append(rel)
        else:
            differing.append(rel)

    return missing, differing, cosmetic


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    consumer_root = Path(argv[1])
    template_root = Path(argv[2])

    if not template_root.is_dir():
        annotate("error", f"template dir {template_root} does not exist")
        return 2

    intentional = read_intentional(consumer_root)
    if intentional:
        print(f"Intentional-drift paths ({len(intentional)}):")
        for path in sorted(intentional):
            print(f"  - {path}")
    else:
        print("No intentional-drift entries.")

    missing, differing, cosmetic = classify(consumer_root, template_root, intentional)

    if cosmetic:
        annotate(
            "notice",
            f"{len(cosmetic)} file(s) differ only in comments, key order or "
            "whitespace and parse identically — not drift.",
        )
        for rel in cosmetic:
            print(f"  ~ {rel}")

    if not missing and not differing:
        annotate("notice", "No drift detected.")
        return 0

    annotate("error", f"Template drift detected vs. {template_root}.")
    if missing:
        print("\nMissing files (template has them, consumer does not):", file=sys.stderr)
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
        print(f"\n=== diff: {rel} ===")
        sys.stdout.writelines(
            difflib.unified_diff(
                (consumer_root / rel).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
                (template_root / rel).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
                fromfile=f"consumer/{rel}",
                tofile=f"template/{rel}",
            )
        )

    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
