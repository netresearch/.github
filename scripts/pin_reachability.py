"""Decide whether a pinned commit belongs to the repository named in `uses:`.

GitHub serves every commit of a fork network through every repository of that
network: `GET /repos/<parent>/commits/<sha>` answers 200 for a commit that exists
only in a fork, and a workflow can run it. A commit belongs to a repository only
when one of its own branches or tags has the commit in its history, which is what
zizmor's `impostor-commit` audit tests (v1.30.0).

zizmor asks the compare API per ref. Here the verdict comes from a list instead:
a treeless clone of the action's branches and tags, and the commits `git rev-list`
walks from them. Three properties follow, and each one is why this is not an API
call or a lookup of the SHA:

- The cost depends on the action's repository, which the template names, and
  never on the pinned commit. Compare has to walk from the candidate, so a fork
  with hundreds of thousands of commits could make the API answer 502, and "the
  API failed" is a warning, so whoever pins the commit could choose the warning.
- The candidate is never looked up. `git cat-file -e`, `--contains` and every other
  lookup in a partial clone fetch a missing object on demand, and GitHub hands out
  the fork commit, so they report it as present. `rev-list` walks commits it has.
- `refs/pull/*` is not fetched. Everyone can create such a ref by opening a pull
  request, so a commit in one proves nothing.

A clone takes 0.7 s and 648 KB for step-security/harden-runner, and 1.5 s and 7 MB
for github/codeql-action.
"""

from __future__ import annotations

import base64
from enum import Enum
import os
from pathlib import Path
import subprocess
import tempfile
import time

GIT_TIMEOUT = 60
# Fits `timeout-minutes: 5` in check-template-drift.yml, with the checkouts.
RUN_DEADLINE = 180


class Reachability(Enum):
    """The three answers a caller has to be able to tell apart."""

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNVERIFIED = "unverified"


class Unanswered(Exception):
    """No usable answer could be obtained; the text says why."""


class PinChecker:
    """Answers per `owner/repo@sha`, from one clone per repository in a run."""

    def __init__(self, token: str, server_url: str):
        self.token = token
        self.server_url = server_url
        self.deadline: float | None = None
        self.listed: dict[str, set[str] | Unanswered] = {}

    @classmethod
    def from_environment(cls) -> PinChecker:
        """Use the server Actions defines, and the token the workflow step passes."""
        return cls(
            os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "",
            os.environ.get("GITHUB_SERVER_URL") or "https://github.com",
        )

    def check(self, repository: str, sha: str) -> tuple[Reachability, str]:
        """(state, explanation) for a commit in `owner/repo`."""
        if repository not in self.listed:
            try:
                self.listed[repository] = self.list_commits(repository)
            except Unanswered as reason:
                self.listed[repository] = reason
        commits = self.listed[repository]
        if isinstance(commits, Unanswered):
            return Reachability.UNVERIFIED, str(commits)
        if sha in commits:
            return Reachability.REACHABLE, "on a branch or tag"
        return Reachability.UNREACHABLE, f"none of the {len(commits)} commits on its branches and tags"

    def list_commits(self, repository: str) -> set[str]:
        """Every commit reachable from a branch or a tag of the repository."""
        if not self.token:
            raise Unanswered("no GITHUB_TOKEN or GH_TOKEN is set, so nothing was fetched")
        with tempfile.TemporaryDirectory(prefix="drift-pin-") as directory:
            clone = str(Path(directory) / "repository.git")
            self.git("clone", "--quiet", "--bare", "--filter=tree:0",
                     f"{self.server_url}/{repository}.git", clone)
            commits = set(self.git("-C", clone, "rev-list", "--branches", "--tags").split())
        if not commits:
            raise Unanswered(f"{repository} has no branch or tag")
        return commits

    def git(self, *arguments: str) -> str:
        """The output of a git command that cannot prompt or fetch on demand."""
        now = time.monotonic()
        if self.deadline is None:
            self.deadline = now + RUN_DEADLINE
        elif now > self.deadline:
            raise Unanswered(f"the budget of {RUN_DEADLINE} s for this run is spent")
        try:
            result = subprocess.run(
                ["git", *arguments], capture_output=True, text=True, check=False,
                env=self.environment(), timeout=min(GIT_TIMEOUT, self.deadline - now),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Unanswered(f"git {arguments[0]} did not finish: {error}") from error
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines() or ["no message"]
            raise Unanswered(f"git exited with {result.returncode}: {lines[-1]}")
        return result.stdout

    def environment(self) -> dict[str, str]:
        """The token travels in the environment, never on a command line."""
        credentials = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return os.environ | {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{self.server_url}/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
        }
