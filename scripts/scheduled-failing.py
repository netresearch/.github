#!/usr/bin/env python3
"""Open one issue per repository whose scheduled workflows keep failing.

Usage:
    scheduled-failing.py [--runs 2] [--days 30] [--dry-run]

A scheduled workflow is failing when its last --runs scheduled runs, all
completed within the last --days days, ended in failure, cancelled,
timed_out or startup_failure. For each repository with at least one failing
workflow, an issue titled "Scheduled runs failing: <repo>" is opened in
netresearch/.github, or updated if one is open. An open issue whose
repository has no failing scheduled workflow any more is closed. --dry-run
prints the actions instead of performing them.

Why this exists: a scheduled run has no pull request and no author, so a red
one tells nobody. ofelia's weekly mutation run hit the 2-hour job limit on
four schedules in a row (2026-10-25 to 2026-11-15) and reported no score,
and nobody noticed. The issues land next to the drift and Renovate ones.

Discovery is one REST call per public repository. Any failed call stops the
run: a failed query must not read as "nothing is failing", which would close
every open issue.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

# Deliberately not flags, as in renovate-stuck.py: a caller-supplied value
# would travel into the gh argument list (S8705).
ORG = "netresearch"
ISSUE_REPO = f"{ORG}/.github"
TITLE_PREFIX = "Scheduled runs failing: "
LABEL = "scheduled-failing"
RED = {"failure", "cancelled", "timed_out", "startup_failure"}


def run(argv: list[str]) -> str:
    p = subprocess.run(argv, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"scheduled-failing: {' '.join(argv[:3])} failed")
    return p.stdout


def public_repos() -> list[str]:
    out = run(
        [
            "gh",
            "api",
            "--paginate",
            f"orgs/{ORG}/repos?type=public&per_page=100",
            "--jq",
            ".[] | select(.archived | not) | .full_name",
        ]
    )
    return sorted(line for line in out.splitlines() if line)


def scheduled_runs(repo: str) -> list[dict]:
    out = run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs?event=schedule&status=completed&per_page=100",
        ]
    )
    return json.loads(out).get("workflow_runs", [])


def failing_workflows(
    runs: list[dict], n_runs: int, days: int, now: datetime
) -> list[dict]:
    """Workflows whose last n_runs completed scheduled runs are all red."""
    cutoff = now - timedelta(days=days)
    by_workflow: dict[int, list[dict]] = {}
    for r in runs:
        created = datetime.fromisoformat(r["created_at"])
        if created < cutoff:
            continue
        by_workflow.setdefault(r["workflow_id"], []).append(r)
    out = []
    for wf_runs in by_workflow.values():
        wf_runs.sort(key=lambda r: r["created_at"], reverse=True)
        last = wf_runs[:n_runs]
        if len(last) < n_runs or any(r["conclusion"] not in RED for r in last):
            continue
        out.append(
            {
                "name": last[0]["name"],
                "path": last[0]["path"],
                "runs": [
                    {
                        "url": r["html_url"],
                        "date": r["created_at"][:10],
                        "conclusion": r["conclusion"],
                    }
                    for r in last
                ],
            }
        )
    return sorted(out, key=lambda w: w["path"])


def issue_body(repo: str, workflows: list[dict], n_runs: int, now: datetime) -> str:
    lines = [
        "## Scheduled workflows with failing runs",
        "",
        f"Repo: `{repo}`",
        f"Scan date: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        (
            f"The last {n_runs} scheduled runs of these workflows did not succeed. "
            "A scheduled run has no pull request and no author, so nobody else is told."
        ),
        "",
        "| Workflow | Last runs |",
        "| --- | --- |",
    ]
    for wf in workflows:
        runs = ", ".join(
            f"[{r['date']} {r['conclusion']}]({r['url']})" for r in wf["runs"]
        )
        name = wf["name"].replace("|", "\\|")
        lines.append(f"| {name} (`{wf['path']}`) | {runs} |")
    lines += [
        "",
        (
            "Auto-opened by `.github/workflows/scheduled-failing.yml`. The scan "
            "updates this issue daily and closes it once a scheduled run of each "
            "workflow succeeds again."
        ),
    ]
    return "\n".join(lines)


def gh_issue(*args: str) -> str:
    return run(["gh", "issue", *args, "--repo", ISSUE_REPO])


def open_issues() -> dict[str, int]:
    out = gh_issue(
        "list",
        "--state",
        "open",
        "--search",
        f'"{TITLE_PREFIX.strip()}" in:title',
        "--limit",
        "500",
        "--json",
        "number,title",
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
        "--runs", type=int, default=2, help="consecutive red runs (default 2)"
    )
    ap.add_argument(
        "--days", type=int, default=30, help="look-back window in days (default 30)"
    )
    ap.add_argument("--dry-run", action="store_true", help="print the actions only")
    args = ap.parse_args()
    if args.runs < 1:
        raise SystemExit("scheduled-failing: --runs must be at least 1")

    now = datetime.now(UTC)
    repos = public_repos()
    failing: dict[str, list[dict]] = {}
    for repo in repos:
        wfs = failing_workflows(scheduled_runs(repo), args.runs, args.days, now)
        if wfs:
            failing[repo] = wfs
    existing = open_issues()
    print(
        f"scheduled-failing: {len(repos)} repo(s) scanned, "
        f"{sum(len(v) for v in failing.values())} failing workflow(s) in "
        f"{len(failing)} repo(s), {len(existing)} open issue(s)"
    )
    for repo, wfs in sorted(failing.items()):
        body = issue_body(repo, wfs, args.runs, now)
        title = TITLE_PREFIX + repo
        if repo in existing:
            print(f"update #{existing[repo]}: {title} ({len(wfs)} workflow(s))")
            if not args.dry_run:
                gh_issue("edit", str(existing[repo]), "--body", body)
        else:
            print(f"open: {title} ({len(wfs)} workflow(s))")
            if not args.dry_run:
                gh_issue("create", "--title", title, "--body", body, "--label", LABEL)
    for repo, number in sorted(existing.items()):
        if repo in failing:
            continue
        print(f"close #{number}: {TITLE_PREFIX}{repo}")
        if not args.dry_run:
            gh_issue(
                "close",
                str(number),
                "--comment",
                f"No scheduled workflow in `{repo}` is failing any more — closed by "
                f"scheduled-failing on {now.strftime('%Y-%m-%dT%H:%M:%SZ')}.",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
