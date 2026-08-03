#!/usr/bin/env python3
"""List every repository in the organisation that consumes a template.

Usage:
    list-consumers.py [--org netresearch] [--json] [--template skill]

Output is one `owner/repo<TAB>template` line per consumer, sorted. `--json`
emits a GitHub Actions matrix instead: {"include":[{"repo":…,"template":…}]}.

Why this exists: the consumer list used to be written out twice by hand — in
scripts/sync-all-consumers.sh and in the drift-scan.yml matrix — with a comment
telling the reader to keep the two in step. Both said six Go repositories while
the fleet had grown to 57 across five templates, so the weekly drift scan was
watching a ninth of what it claimed to watch and the sync script skipped the
rest. A repository *declares* its own membership by carrying
`.github/template.yaml`; asking the organisation is therefore both simpler and
correct by construction, and adding a consumer stops being a two-file edit.

Discovery is one GraphQL query per 50 repositories, not one REST call per
repository: the fleet costs four round trips, not two hundred.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

import yaml

CHUNK = 50

# GitHub owner and repository names. Two things this rejects matter here: a
# leading hyphen, which `gh` would read as a flag rather than a value, and a
# quote or backslash, which would close the GraphQL string the query is built
# from. Both reach a command line, so neither is checked as a formality.
# A leading dot is allowed — `.github` is a real repository in this very org,
# and a stricter pattern rejected the organisation's own template source.
NAME = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,99}$")


def checked_name(kind: str, value: str) -> str:
    if not NAME.match(value or ""):
        raise SystemExit(
            f"list-consumers: refusing {kind} {value!r} — not a GitHub name "
            "(letters, digits, dot, hyphen, underscore; no leading hyphen)"
        )
    return value


def run(argv: list[str], stdin: str | None = None) -> str:
    p = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"list-consumers: {argv[0]} failed")
    return p.stdout


def repo_names(org: str) -> list[str]:
    """Every non-archived repository. Archived ones never run Actions."""
    out = run(
        [
            "gh",
            "repo",
            "list",
            org,
            "--limit",
            "1000",
            "--no-archived",
            "--json",
            "name",
            "--jq",
            ".[].name",
        ]
    )
    return sorted(n for n in out.splitlines() if n)


def fetch_template_files(org: str, names: list[str]) -> dict[str, str]:
    """alias -> file contents, for the repos that carry a template.yaml."""
    found: dict[str, str] = {}
    for start in range(0, len(names), CHUNK):
        chunk = names[start : start + CHUNK]
        parts = []
        for i, name in enumerate(chunk):
            checked_name("repository name", name)
            parts.append(
                f'r{i}: repository(owner: "{org}", name: "{name}") {{'
                '  object(expression: "HEAD:.github/template.yaml") {'
                "    ... on Blob { text }"
                "  }"
                "}"
            )
        query = "query { " + " ".join(parts) + " }"
        data = json.loads(run(["gh", "api", "graphql", "-f", f"query={query}"]))
        for i, name in enumerate(chunk):
            node = (data.get("data") or {}).get(f"r{i}") or {}
            obj = node.get("object")
            if obj and obj.get("text"):
                found[f"{org}/{name}"] = obj["text"]
    return found


def consumers(org: str) -> list[tuple[str, str]]:
    out = []
    for repo, text in sorted(fetch_template_files(org, repo_names(org)).items()):
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            # A repo that cannot be read is not silently dropped: it would then
            # look like a non-consumer and stop being scanned entirely.
            print(
                f"list-consumers: {repo}: unparseable template.yaml: {exc}",
                file=sys.stderr,
            )
            continue
        template = doc.get("template")
        if template:
            out.append((repo, str(template)))
        else:
            print(
                f"list-consumers: {repo}: template.yaml has no `template:` key",
                file=sys.stderr,
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", default="netresearch")
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit a GitHub Actions matrix instead of TSV",
    )
    ap.add_argument("--template", help="only consumers of this template")
    args = ap.parse_args()

    rows = consumers(checked_name("organisation", args.org))
    if args.template:
        rows = [r for r in rows if r[1] == args.template]
    if not rows:
        print(
            "list-consumers: no consumers found — refusing to report an empty "
            "fleet, which would silently scan nothing",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps({"include": [{"repo": r, "template": t} for r, t in rows]}))
    else:
        for repo, template in rows:
            print(f"{repo}\t{template}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
