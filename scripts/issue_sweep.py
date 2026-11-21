"""Shared issue handling for the fleet scans in this directory.

renovate-stuck.py and scheduled-failing.py each keep one issue per
repository in netresearch/.github: opened while the repository has a
finding, updated on later runs, closed once the finding is gone. This module
holds that part, so both scans file and close issues the same way.

Any failed gh call stops the run: a failed query must not read as "nothing
found", which would close every open issue.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable

ISSUE_REPO = "netresearch/.github"


def run(argv: list[str], name: str) -> str:
    p = subprocess.run(argv, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"{name}: {' '.join(argv[:3])} failed")
    return p.stdout


def gh_issue(name: str, *args: str) -> str:
    return run(["gh", "issue", *args, "--repo", ISSUE_REPO], name)


def open_issues(name: str, title_prefix: str) -> dict[str, int]:
    """Open issues whose title starts with title_prefix, by repository."""
    out = gh_issue(
        name,
        "list",
        "--state",
        "open",
        "--search",
        f'"{title_prefix.strip()}" in:title',
        "--limit",
        "500",
        "--json",
        "number,title",
    )
    return {
        i["title"][len(title_prefix) :]: i["number"]
        for i in json.loads(out)
        if i["title"].startswith(title_prefix)
    }


def sync(
    name: str,
    title_prefix: str,
    label: str,
    found: dict[str, tuple[str, str]],
    existing: dict[str, int],
    close_comment: Callable[[str], str],
    dry_run: bool,
) -> None:
    """Open or update an issue per repository in found, close the rest.

    found maps a repository to (issue body, summary for the log line).
    """
    for repo, (body, summary) in sorted(found.items()):
        title = title_prefix + repo
        if repo in existing:
            print(f"update #{existing[repo]}: {title} ({summary})")
            if not dry_run:
                gh_issue(name, "edit", str(existing[repo]), "--body", body)
        else:
            print(f"open: {title} ({summary})")
            if not dry_run:
                gh_issue(
                    name, "create", "--title", title, "--body", body, "--label", label
                )
    for repo, number in sorted(existing.items()):
        if repo in found:
            continue
        print(f"close #{number}: {title_prefix}{repo}")
        if not dry_run:
            gh_issue(name, "close", str(number), "--comment", close_comment(repo))
