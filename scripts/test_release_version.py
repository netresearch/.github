"""Fixtures for release-version.yml: the version a release pull request names
must equal the version git-cliff computes from the commits.

The step scripts are read from the workflow file and run as GitHub runs them,
so the tests cannot drift from the workflow. The git-cliff cases run the real
binary (the version the workflow installs) on throwaway repositories, with the
configuration the workflow writes.
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


def steps():
    doc = yaml.load(WORKFLOW.read_text(), Loader=WorkflowLoader)
    return {s["name"]: s for s in doc["jobs"]["release-version"]["steps"]}


def run_step(name, env, cwd=None):
    """Run a `run:` step the way `shell: bash` does; return (rc, output, outputs, summary)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "output")
        summary = Path(tmp, "summary")
        out.touch()
        summary.touch()
        full_env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out),
                    "GITHUB_STEP_SUMMARY": str(summary),
                    "RUNNER_TEMP": env.pop("RUNNER_TEMP", tmp), **env}
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", steps()[name]["run"]],
            env=full_env, cwd=cwd, capture_output=True, text=True)
        return (result.returncode, result.stdout + result.stderr,
                out.read_text(), summary.read_text())


def compare(head_ref, title, computed, skip="", skip_version=""):
    return run_step("Compare the versions", {
        "HEAD_REF": head_ref, "PR_TITLE": title, "COMPUTED": computed, "SKIP_REASON": skip,
        "SKIP_VERSION": skip_version})


class DetectTest(unittest.TestCase):
    def test_release_branch_is_checked(self):
        rc, _, outputs, _ = run_step("Detect a release pull request",
                                     {"EVENT_NAME": "pull_request", "HEAD_REF": "release/v0.56.0"})
        self.assertEqual(rc, 0)
        self.assertIn("release=true", outputs)

    def test_other_branch_passes_with_notice(self):
        rc, log, outputs, _ = run_step("Detect a release pull request",
                                       {"EVENT_NAME": "pull_request", "HEAD_REF": "feat/x"})
        self.assertEqual(rc, 0)
        self.assertIn("release=false", outputs)
        self.assertIn("::notice::", log)

    def test_branch_without_v_is_not_a_release_branch(self):
        _, _, outputs, _ = run_step("Detect a release pull request",
                                    {"EVENT_NAME": "pull_request", "HEAD_REF": "release/0.56.0"})
        self.assertIn("release=false", outputs)

    def test_merge_group_passes_with_notice(self):
        # A required check must report on a merge group, or the queue ejects the entry.
        rc, log, outputs, _ = run_step("Detect a release pull request",
                                       {"EVENT_NAME": "merge_group", "HEAD_REF": ""})
        self.assertEqual(rc, 0, log)
        self.assertIn("release=false", outputs)
        self.assertIn("::notice::Merge group", log)

    def test_other_event_fails(self):
        rc, log, _, _ = run_step("Detect a release pull request",
                                 {"EVENT_NAME": "push", "HEAD_REF": ""})
        self.assertEqual(rc, 1)
        self.assertIn("::error::", log)


class CompareTest(unittest.TestCase):
    def test_match_passes(self):
        # The action's content output carries the tag prefix and a newline.
        rc, log, _, summary = compare("release/v0.56.1", "chore(release): 0.56.1", "v0.56.1\n")
        self.assertEqual(rc, 0, log)
        self.assertIn("::notice::", log)
        self.assertIn("| git-cliff --bumped-version | v0.56.1 |", summary)

    def test_computed_differs_fails(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.56.0", "v0.55.2\n")
        self.assertEqual(rc, 1)
        self.assertIn("::error::This pull request releases 0.56.0, but the commits since the last"
                      " release call for 0.55.2.", log)

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
        rc, log, _, _ = compare("release/v0.56", "chore(release): 0.56", "v0.56.0")
        self.assertEqual(rc, 1)
        self.assertIn("::error::The head branch must be release/vX.Y.Z", log)

    def test_no_computed_version_fails(self):
        rc, log, _, _ = compare("release/v0.56.0", "chore(release): 0.56.0", "")
        self.assertEqual(rc, 1)
        self.assertIn("::error::git-cliff returned no version", log)

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

    def bumped(self, messages, tags=("v0.5.0",), split="false", pattern=None, ignore_last=False):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            temp = Path(tmp, "temp")
            temp.mkdir()
            subprocess.run([*GIT, "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "chore: init"],
                           check=True)
            for tag in tags:
                subprocess.run([*GIT, "-C", str(repo), "tag", tag], check=True)
            for message in messages:
                subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message],
                               check=True)
            if ignore_last:
                head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                      capture_output=True, text=True).stdout
                Path(repo, ".cliffignore").write_text(head)
            default_pattern = yaml.load(WORKFLOW.read_text(), Loader=WorkflowLoader)[
                "on"]["workflow_call"]["inputs"]["tag-pattern"]["default"]
            rc, log, _, _ = run_step("Write the git-cliff configuration", {
                "RUNNER_TEMP": str(temp), "TAG_PATTERN": pattern or default_pattern,
                "SPLIT_COMMITS": split}, cwd=repo)
            self.assertEqual(rc, 0, log)
            self.assertFalse(Path(repo, ".cliffignore").exists())
            result = subprocess.run(
                [GIT_CLIFF, f"--config={temp}/release-version-cliff.toml", "--bumped-version"],
                cwd=repo, capture_output=True, text=True,
                env={k: v for k, v in os.environ.items() if not k.startswith("GIT_CLIFF")})
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

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
        self.assertEqual(self.bumped(["fix: a"], tags=("v0.5.0", "v1")), "v0.5.1")

    def test_split_commits_drops_footers(self):
        # Why split-commits defaults to false.
        self.assertEqual(self.bumped(["feat: a\n\nBREAKING CHANGE: removed x"], split="true"),
                         "v0.5.1")

    def test_cliffignore_cannot_hide_a_breaking_commit(self):
        # The pull request controls .cliffignore; the workflow removes it.
        self.assertEqual(self.bumped(["fix: a", "feat!: drop the old API"], ignore_last=True),
                         "v0.6.0")

    def test_end_to_end_mismatch_fails(self):
        computed = self.bumped(["fix: a", "feat!: drop the old API"])
        rc, log, _, _ = compare("release/v0.5.1", "chore(release): 0.5.1", computed)
        self.assertEqual(rc, 1)
        self.assertIn("call for 0.6.0", log)


if __name__ == "__main__":
    unittest.main()
