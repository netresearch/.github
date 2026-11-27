"""Fixtures for release-version.yml: the version a release pull request names
must equal the version git-cliff computes from the commits.

The step scripts are read from the workflow file and run as GitHub runs them,
so the tests cannot drift from the workflow. The git-cliff cases run the real
binary, installed the way the workflow installs it (the newest release), on
throwaway repositories with the configuration the workflow writes.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from drift_compare import WorkflowLoader

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release-version.yml"
GIT_CLIFF = os.environ.get("GIT_CLIFF") or shutil.which("git-cliff")
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org",
       "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false"]
DETECT = "Detect a release pull request"
CONFIG = "Write the git-cliff configuration"
COMPARE = "Compare the versions"
QUEUE_SHA = "c" * 40
PR_SHA = "a" * 40

# Stand-in for git-cliff in the comparison tests: prints FAKE_VERSION, or
# fails with FAKE_RC and a message on stderr.
FAKE_CLIFF = """#!/usr/bin/env bash
if [[ "${FAKE_RC:-0}" != 0 ]]; then echo "ERROR fake git-cliff failure" >&2; exit "$FAKE_RC"; fi
printf '%s\\n' "$FAKE_VERSION"
"""

# Stand-in for `gh api repos/<repo>/pulls/<n> --jq <expr>`; unknown calls fail.
FAKE_GH = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
assert args[0] == "api" and args[2] == "--jq", args
expected = f"repos/{os.environ['REPO']}/pulls/{os.environ['FAKE_PR']}"
if args[1] != expected:
    sys.exit(f"unexpected gh call: {args}")
body = {"head": {"ref": os.environ["FAKE_HEAD_REF"]}, "title": os.environ["FAKE_TITLE"]}
sys.stdout.write(subprocess.run(["jq", "-r", args[3]], input=json.dumps(body), text=True,
                                capture_output=True, check=True).stdout)
"""


def steps():
    doc = yaml.load(WORKFLOW.read_text(), Loader=WorkflowLoader)
    return {s["name"]: s for s in doc["jobs"]["release-version"]["steps"]}


def outputs_of(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def run_step(name, env, cwd=None):
    """Run a `run:` step the way `shell: bash` does; return (rc, output, outputs, summary)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "output")
        summary = Path(tmp, "summary")
        out.touch()
        summary.touch()
        full_env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out),
                    "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": tmp, **env}
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", steps()[name]["run"]],
            env=full_env, cwd=cwd, capture_output=True, text=True)
        return (result.returncode, result.stdout + result.stderr,
                outputs_of(out.read_text()), summary.read_text())


def detect(event, head_ref="", title="", sha=PR_SHA, mg_ref="", pr=None):
    with tempfile.TemporaryDirectory() as tmp:
        gh = Path(tmp, "gh")
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        env = {"PATH": f"{tmp}:{os.environ['PATH']}", "EVENT_NAME": event, "REPO": "netresearch/x",
               "GH_TOKEN": "t", "PR_HEAD_REF": "", "PR_TITLE": "", "PR_HEAD_SHA": "",
               "MG_HEAD_REF": mg_ref, "MG_HEAD_SHA": "", "FAKE_PR": "0",
               "FAKE_HEAD_REF": "", "FAKE_TITLE": ""}
        if event == "merge_group":
            env.update(MG_HEAD_SHA=QUEUE_SHA, FAKE_PR=str(pr), FAKE_HEAD_REF=head_ref,
                       FAKE_TITLE=title)
        else:
            env.update(PR_HEAD_REF=head_ref, PR_TITLE=title, PR_HEAD_SHA=sha)
        return run_step(DETECT, env)


def compare(head_ref, title, computed="", skip="", skip_version="", cliff_rc=0):
    with tempfile.TemporaryDirectory() as tmp:
        cliff = Path(tmp, "git-cliff")
        cliff.write_text(FAKE_CLIFF)
        cliff.chmod(0o755)
        return run_step(COMPARE, {
            "HEAD_REF": head_ref, "PR_TITLE": title, "SKIP_REASON": skip,
            "SKIP_VERSION": skip_version, "GIT_CLIFF": str(cliff),
            "FAKE_VERSION": computed, "FAKE_RC": str(cliff_rc)})


class DetectTest(unittest.TestCase):
    def test_release_branch_is_checked(self):
        rc, _, out, _ = detect("pull_request", "release/v0.56.0", "chore(release): 0.56.0")
        self.assertEqual(rc, 0)
        self.assertEqual(out["release"], "true")
        self.assertEqual(out["head-ref"], "release/v0.56.0")
        self.assertEqual(out["title"], "chore(release): 0.56.0")
        self.assertEqual(out["sha"], PR_SHA)

    def test_other_pull_request_passes_with_notice(self):
        rc, log, out, _ = detect("pull_request", "feat/x", "feat: x")
        self.assertEqual(rc, 0)
        self.assertEqual(out["release"], "false")
        self.assertIn("::notice::Not a release pull request", log)

    def test_release_title_on_another_branch_is_a_release(self):
        # Otherwise a release PR on a misnamed branch would pass unchecked.
        for branch in ("feat/x", "release/0.56.0"):
            with self.subTest(branch=branch):
                _, _, out, _ = detect("pull_request", branch, "chore(release): 0.56.0")
                self.assertEqual(out["release"], "true")

    def test_merge_group_reads_the_queued_pull_request(self):
        rc, log, out, _ = detect(
            "merge_group", "release/v0.56.1", "chore(release): 0.56.1",
            mg_ref=f"refs/heads/gh-readonly-queue/main/pr-1477-{'b' * 40}", pr=1477)
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["release"], "true")
        self.assertEqual(out["head-ref"], "release/v0.56.1")
        self.assertEqual(out["sha"], QUEUE_SHA)

    def test_merge_group_of_another_pull_request_passes(self):
        rc, log, out, _ = detect(
            "merge_group", "feat/x", "feat: x",
            mg_ref=f"refs/heads/gh-readonly-queue/main/pr-12-{'b' * 40}", pr=12)
        self.assertEqual(rc, 0, log)
        self.assertEqual(out["release"], "false")

    def test_unreadable_merge_group_ref_fails(self):
        rc, log, _, _ = detect("merge_group", mg_ref="refs/heads/main", pr=1)
        self.assertEqual(rc, 1)
        self.assertIn("::error::Cannot read the pull request number", log)

    def test_line_break_in_a_title_is_refused(self):
        # Step outputs are written one per line; a break would add an output.
        rc, log, _, _ = detect("merge_group", "release/v0.5.1", "chore(release): 0.5.1\nrelease=x",
                               mg_ref=f"gh-readonly-queue/main/pr-7-{'b' * 40}", pr=7)
        self.assertEqual(rc, 1)
        self.assertIn("::error::The head branch or the title contains a line break.", log)

    def test_other_events_fail(self):
        for event in ("pull_request_target", "push", "workflow_dispatch"):
            with self.subTest(event=event):
                rc, log, _, _ = detect(event, "release/v0.56.0", "chore(release): 0.56.0")
                self.assertEqual(rc, 1)
                self.assertIn("::error::This workflow checks pull_request and merge_group events", log)


class CompareTest(unittest.TestCase):
    def test_match_passes(self):
        rc, log, _, summary = compare("release/v0.56.1", "chore(release): 0.56.1", "v0.56.1")
        self.assertEqual(rc, 0, log)
        self.assertIn("::notice::", log)
        self.assertIn("| git-cliff --bumped-version | v0.56.1 |", summary)

    def test_computed_differs_fails(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.56.0", "v0.55.2")
        self.assertEqual(rc, 1)
        self.assertIn("::error::This pull request releases 0.56.0, but the commits since the last"
                      " release call for 0.55.2.", log)
        # Renaming a branch closes its pull request.
        self.assertIn("open a new pull request from release/v0.55.2", log)
        self.assertNotIn("rename", log)
        self.assertIn("1.0.0 itself", log)

    def test_branch_and_title_differ_fails(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.55.2", "v0.56.0")
        self.assertEqual(rc, 1)
        self.assertIn("::error::The branch names 0.56.0, the title names 0.55.2.", log)

    def test_malformed_title_fails(self):
        for title in ("chore(release): v0.56.0", "chore: release 0.56.0",
                      "chore(release): 0.56.0 (#12)", "chore(release): 0.56"):
            with self.subTest(title=title):
                rc, log, _, _ = compare("release/v0.56.0", title, "v0.56.0")
                self.assertEqual(rc, 1)
                self.assertIn("::error::The title must be 'chore(release): 0.56.0'", log)

    def test_malformed_branch_fails(self):
        for branch in ("release/v0.56", "feat/x", "release/0.56.0"):
            with self.subTest(branch=branch):
                rc, log, _, _ = compare(branch, "chore(release): 0.56.0", "v0.56.0")
                self.assertEqual(rc, 1)
                self.assertIn("::error::The head branch must be release/vX.Y.Z", log)

    def test_no_computed_version_fails(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.56.0", "")
        self.assertEqual(rc, 1)
        self.assertIn("::error::git-cliff returned no version", log)

    def test_git_cliff_failure_fails_with_its_log(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.56.0", cliff_rc=1)
        self.assertEqual(rc, 1)
        self.assertIn("ERROR fake git-cliff failure", log)
        self.assertIn("::error::git-cliff failed with exit code 1", log)

    def test_git_cliff_failure_still_reaches_the_skip(self):
        rc, log, _, _ = compare("release/v1.0.0", "chore(release): 1.0.0", cliff_rc=1,
                                skip="owner approved 1.0.0", skip_version="1.0.0")
        self.assertEqual(rc, 0, log)
        self.assertIn("::warning::Version check skipped for 1.0.0", log)

    def test_skip_with_reason_passes_and_is_logged(self):
        rc, log, _, summary = compare("release/v0.56.0", "chore(release): 0.56.0", "v0.55.2",
                                      skip="owner approved 2026-11-27: breaking API change",
                                      skip_version="0.56.0")
        self.assertEqual(rc, 0, log)
        self.assertIn("::warning::Version check skipped for 0.56.0 (computed: v0.55.2). Reason:"
                      " owner approved 2026-11-27: breaking API change", log)
        self.assertIn("**Skipped.** Reason: owner approved 2026-11-27", summary)

    def test_blank_skip_reason_does_not_skip(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.56.0", "v0.55.2", skip="  \n",
                                skip_version="0.56.0")
        self.assertEqual(rc, 1)
        self.assertNotIn("::warning::", log)
        self.assertIn("::error::skip-reason and skip-version must be set together.", log)

    def test_blank_skip_reason_alone_counts_as_unset(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.56.0", "v0.56.0", skip=" \n")
        self.assertEqual(rc, 0, log)
        self.assertNotIn("::error::", log)

    def test_skip_reason_without_version_fails(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.56.0", "v0.56.0", skip="ok")
        self.assertEqual(rc, 1)
        self.assertIn("::error::skip-reason and skip-version must be set together.", log)

    def test_skip_does_not_carry_over_to_the_next_release(self):
        # The inputs live in the caller's workflow file and merge with the release.
        rc, log, _, _ = compare("release/v0.56.1", "chore(release): 0.56.1", "v0.56.1",
                                skip="owner approved 0.56.0", skip_version="0.56.0")
        self.assertEqual(rc, 1)
        self.assertNotIn("::warning::", log)
        self.assertIn("::error::The skip was approved for 0.56.0, but this pull request"
                      " releases 0.56.1.", log)

    def test_skip_still_compares_branch_and_title(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.55.2", "v0.55.2", skip="ok",
                                skip_version="0.56.0")
        self.assertEqual(rc, 1)
        self.assertIn("::error::The branch names 0.56.0", log)


# CI installs git-cliff, so there a missing binary fails instead of skipping.
@unittest.skipUnless(GIT_CLIFF or os.environ.get("CI"),
                     "git-cliff not found: set GIT_CLIFF or put it on PATH")
class GitCliffTest(unittest.TestCase):
    """The configuration the workflow writes, run by git-cliff on real commits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def repo(self, messages, tags=("v0.5.0",), head_tags=(), split="false", ignore_last=False):
        """Create a repository and run the configuration step in it; return (repo, temp)."""
        base = Path(tempfile.mkdtemp(dir=self.tmp.name))
        repo, temp = base / "repo", base / "temp"
        temp.mkdir()
        subprocess.run([*GIT, "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "chore: init"],
                       check=True)
        for tag in tags:
            subprocess.run([*GIT, "-C", str(repo), "tag", tag], check=True)
        for message in messages:
            subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message],
                           check=True)
        for tag in head_tags:
            subprocess.run([*GIT, "-C", str(repo), "tag", tag], check=True)
        if ignore_last:
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout
            Path(repo, ".cliffignore").write_text(head)
        pattern = yaml.load(WORKFLOW.read_text(), Loader=WorkflowLoader)[
            "on"]["workflow_call"]["inputs"]["tag-pattern"]["default"]
        rc, log, _, _ = run_step(CONFIG, {"RUNNER_TEMP": str(temp), "TAG_PATTERN": pattern,
                                          "SPLIT_COMMITS": split}, cwd=repo)
        self.assertEqual(rc, 0, log)
        self.assertFalse(Path(repo, ".cliffignore").exists())
        return repo, temp

    def bumped(self, messages, **kwargs):
        repo, temp = self.repo(messages, **kwargs)
        result = subprocess.run(
            [GIT_CLIFF, f"--config={temp}/release-version-cliff.toml", "--bumped-version"],
            cwd=repo, capture_output=True, text=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_CLIFF")})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def check(self, repo, temp, head_ref, title, **env):
        """Run the comparison step with the real git-cliff in the repository."""
        return run_step(COMPARE, {"RUNNER_TEMP": str(temp), "GIT_CLIFF": GIT_CLIFF,
                                  "HEAD_REF": head_ref, "PR_TITLE": title, "SKIP_REASON": "",
                                  "SKIP_VERSION": "", **env}, cwd=repo)

    def test_zero_x_rule(self):
        cases = {
            "fix": (["fix: a"], "v0.5.1"),
            "feature": (["feat: a"], "v0.5.1"),
            "breaking subject": (["feat!: a"], "v0.6.0"),
            "breaking scoped fix": (["fix(api)!: a"], "v0.6.0"),
            "breaking footer": (["feat: a\n\nbody\n\nBREAKING CHANGE: removed x"], "v0.6.0"),
            "footer after a PR template body": (
                ["fix: a (#1)\n\n## Summary\n\nText.\n\n- [x] item\n\nBREAKING CHANGE: removed x"],
                "v0.6.0"),
            # The embedded git-cliff default skips chore(deps…); the workflow's
            # parsers must not, least of all for a breaking one.
            "breaking dependency update": (["chore(deps)!: drop php 8.1"], "v0.6.0"),
            "dependency update only": (["chore(deps): update x to v2 (#5)"], "v0.5.1"),
            "release commit only": (["chore(release): 0.5.1"], "v0.5.1"),
        }
        for name, (messages, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.bumped(messages), expected)

    def test_one_x_rule(self):
        self.assertEqual(self.bumped(["fix: a"], tags=("v1.2.3",)), "v1.2.4")
        self.assertEqual(self.bumped(["feat: a"], tags=("v1.2.3",)), "v1.3.0")
        self.assertEqual(self.bumped(["feat!: a"], tags=("v1.2.3",)), "v2.0.0")

    def test_interface_tag_is_ignored(self):
        # A `v1` interface tag on the newest commit is no release.
        self.assertEqual(self.bumped(["fix: a"], head_tags=("v1",)), "v0.5.1")

    def test_split_commits_drops_footers(self):
        # Why split-commits defaults to false.
        self.assertEqual(self.bumped(["feat: a\n\nBREAKING CHANGE: removed x"], split="true"),
                         "v0.5.1")

    def test_cliffignore_cannot_hide_a_breaking_commit(self):
        # The pull request controls .cliffignore; the workflow removes it.
        self.assertEqual(self.bumped(["fix: a", "feat!: drop the old API"], ignore_last=True),
                         "v0.6.0")

    def test_first_release_is_0_1_0(self):
        # Without a tag git-cliff proposes 0.1.0, which the tag pattern rejected.
        repo, temp = self.repo(["feat: first feature"], tags=())
        rc, log, _, _ = self.check(repo, temp, "release/v0.1.0", "chore(release): 0.1.0")
        self.assertEqual(rc, 0, log)
        self.assertIn("::notice::Release version 0.1.0 matches the commits.", log)

    def test_end_to_end_mismatch_fails(self):
        repo, temp = self.repo(["fix: a", "feat!: drop the old API"])
        rc, log, _, _ = self.check(repo, temp, "release/v0.5.1", "chore(release): 0.5.1")
        self.assertEqual(rc, 1)
        self.assertIn("call for 0.6.0", log)

    def test_breaking_change_queued_ahead_fails_the_merge_group(self):
        # The release PR was green for 0.5.1; a feat! queued ahead of it ends
        # up in the merge group's commit, which the merge-group run checks.
        repo, temp = self.repo(["fix: a", "chore(release): 0.5.1", "feat!: queued ahead"])
        rc, log, out, _ = detect(
            "merge_group", "release/v0.5.1", "chore(release): 0.5.1",
            mg_ref=f"refs/heads/gh-readonly-queue/main/pr-7-{'b' * 40}", pr=7)
        self.assertEqual((rc, out["release"]), (0, "true"), log)
        rc, log, _, _ = self.check(repo, temp, out["head-ref"], out["title"])
        self.assertEqual(rc, 1)
        self.assertIn("call for 0.6.0", log)


if __name__ == "__main__":
    unittest.main()
