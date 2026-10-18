#!/usr/bin/env python3
"""Open one issue per repository whose Renovate pull requests are stuck red.

Usage:
    renovate-stuck.py [--days 3] [--issue-repo owner/repo] [--dry-run]

A Renovate pull request is stuck when it has been open for more than --days
days and its head commit's combined check state is FAILURE or ERROR. For each
repository with at least one stuck pull request, an issue titled
"Renovate stuck: <repo>" is opened in --issue-repo, or updated if one is open.
An open issue whose repository has no stuck pull request any more is closed.
--dry-run prints the actions instead of performing them.

Why this exists: Renovate merges only green pull requests, and a red one stays
open without telling anyone. netresearch/ofelia#867, Renovate's update to Go
1.27.2, failed gosec for ten days without anyone noticing, while every other
pull request in the repository failed govulncheck on the twelve standard
library vulnerabilities that update would have fixed. The issues land where
drift-scan.yml already reports, so stuck updates are triaged in the same place.

Discovery is one GraphQL search per 50 pull requests. Any failed call stops
the run: a failed query must not read as "nothing is stuck", which would close
every open issue.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

# The organisation this repository serves. Deliberately not a flag, as in
# list-consumers.py: a caller-supplied owner would travel into the search
# query built below.
ORG = "netresearch"
TITLE_PREFIX = "Renovate stuck: "
LABEL = "renovate-stuck"
RED = {"FAILURE", "ERROR"}

QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        url
        title
        createdAt
        repository { nameWithOwner isArchived }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
                contexts(first: 100) {
                  nodes {
                    ... on CheckRun { name conclusion }
                    ... on StatusContext { context state }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def run(argv: list[str]) -> str:
    p = subprocess.run(argv, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"renovate-stuck: {' '.join(argv[:3])} failed")
    return p.stdout


def open_renovate_prs() -> list[dict]:
    search = f"org:{ORG} is:pr is:open author:app/renovate archived:false"
    prs: list[dict] = []
    after: str | None = None
    while True:
        argv = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"q={search}"]
        if after:
            argv += ["-f", f"after={after}"]
        data = json.loads(run(argv))
        if data.get("errors"):
            raise SystemExit(f"renovate-stuck: GraphQL errors: {data['errors']}")
        result = data["data"]["search"]
        prs += [n for n in result["nodes"] if n]
        if not result["pageInfo"]["hasNextPage"]:
            return prs
        after = result["pageInfo"]["endCursor"]


def failing_checks(pr: dict) -> list[str]:
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    names = []
    for ctx in ((rollup or {}).get("contexts") or {}).get("nodes") or []:
        if ctx.get("conclusion") in {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"}:
            names.append(ctx["name"])
        elif ctx.get("state") in RED:
            names.append(ctx["context"])
    return sorted(set(names))


def stuck_by_repo(prs: list[dict], days: int, now: datetime) -> dict[str, list[dict]]:
    cutoff = now - timedelta(days=days)
    out: dict[str, list[dict]] = {}
    for pr in prs:
        if pr["repository"]["isArchived"]:
            continue
        commits = pr["commits"]["nodes"]
        rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
        if not rollup or rollup["state"] not in RED:
            continue
        created = datetime.fromisoformat(pr["createdAt"])
        if created > cutoff:
            continue
        pr["ageDays"] = (now - created).days
        pr["failing"] = failing_checks(pr)
        out.setdefault(pr["repository"]["nameWithOwner"], []).append(pr)
    return out


def issue_body(repo: str, prs: list[dict], days: int, now: datetime) -> str:
    lines = [
        "## Renovate pull requests with failing checks",
        "",
        f"Repo: `{repo}`",
        f"Scan date: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        (
            f"These Renovate pull requests have been open for more than {days} days "
            "and their latest commit has failing checks. Renovate merges only green "
            "pull requests, so each one stays open until someone fixes the cause."
        ),
        "",
        "| Pull request | Open for | Failing checks |",
        "| --- | --- | --- |",
    ]
    for pr in sorted(prs, key=lambda p: p["number"]):
        checks = ", ".join(f"`{c}`" for c in pr["failing"]) or "(none reported)"
        title = pr["title"].replace("|", "\\|")
        lines.append(
            f"| [#{pr['number']}]({pr['url']}) {title} | {pr['ageDays']} days | {checks} |"
        )
    lines += [
        "",
        (
            "Auto-opened by `.github/workflows/renovate-stuck.yml`. The scan updates "
            "this issue daily and closes it once no Renovate pull request in the "
            "repository is stuck."
        ),
    ]
    return "\n".join(lines)


def open_issues(issue_repo: str) -> dict[str, int]:
    out = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            issue_repo,
            "--state",
            "open",
            "--search",
            f'"{TITLE_PREFIX.strip()}" in:title',
            "--limit",
            "500",
            "--json",
            "number,title",
        ]
    )
    return {
        i["title"][len(TITLE_PREFIX) :]: i["number"]
        for i in json.loads(out)
        if i["title"].startswith(TITLE_PREFIX)
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--days",
        type=int,
        default=3,
        help="minimum age of a stuck pull request (default 3)",
    )
    ap.add_argument(
        "--issue-repo",
        default=f"{ORG}/.github",
        help="repository the issues are filed in",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the actions instead of performing them",
    )
    args = ap.parse_args()

    now = datetime.now(UTC)
    prs = open_renovate_prs()
    stuck = stuck_by_repo(prs, args.days, now)
    existing = open_issues(args.issue_repo)
    print(
        f"renovate-stuck: {len(prs)} open Renovate pull request(s), "
        f"{sum(len(v) for v in stuck.values())} stuck in {len(stuck)} repo(s), "
        f"{len(existing)} open issue(s)"
    )

    for repo, repo_prs in sorted(stuck.items()):
        body = issue_body(repo, repo_prs, args.days, now)
        title = TITLE_PREFIX + repo
        if repo in existing:
            print(f"update #{existing[repo]}: {title} ({len(repo_prs)} PR)")
            if not args.dry_run:
                run(
                    [
                        "gh",
                        "issue",
                        "edit",
                        str(existing[repo]),
                        "--repo",
                        args.issue_repo,
                        "--body",
                        body,
                    ]
                )
        else:
            print(f"open: {title} ({len(repo_prs)} PR)")
            if not args.dry_run:
                base = [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    args.issue_repo,
                    "--title",
                    title,
                    "--body",
                    body,
                ]
                # The label may not exist yet; an issue without it is still an issue.
                if (
                    subprocess.run(
                        base + ["--label", LABEL], capture_output=True, check=False
                    ).returncode
                    != 0
                ):
                    run(base)

    for repo, number in sorted(existing.items()):
        if repo not in stuck:
            print(f"close #{number}: {TITLE_PREFIX}{repo}")
            if not args.dry_run:
                run(
                    [
                        "gh",
                        "issue",
                        "close",
                        str(number),
                        "--repo",
                        args.issue_repo,
                        "--comment",
                        (
                            f"No Renovate pull request in `{repo}` is stuck any more — closed by "
                            f"renovate-stuck on {now.strftime('%Y-%m-%dT%H:%M:%SZ')}."
                        ),
                    ]
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
