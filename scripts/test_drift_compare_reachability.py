"""The online check behind the pin tolerance, against real git and then each branch."""

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

import pin_reachability
from pin_reachability import PinChecker, Reachability
import test_drift_compare_pins as pins
from test_drift_compare_pins import ACTION, CHECKS, NEW, OLD, PIN_NOTICE, REUSABLE, WORKFLOW
from test_drift_compare_pins import fixture, run_compare, workflow

# Heads in forks of the action, measured on 2026-11-01. Each is in no branch or tag
# of step-security/harden-runner, and GET commits/<sha> there answers 200 for each.
FORK = "0592be34e86d8be46f8dd1e62095ebbeb56fd213"  # rohan-stepsecurity/harden-runner
FORK_PULL_REQUEST = "9832ceae2813a581ac51ba4848e6ff540858f53c"  # the head of its pull request 699
VERIFIED_NOTICE = "verified as reachable"
UNVERIFIED = "was not verified"
CLEAN = "No drift detected."
SUMMARY = "hold a pin that was not verified"
OTHER = ".github/workflows/other.yml"
PATCHED = f"{ACTION}@{NEW}"


def run_git(directory, *arguments):
    """Git for building a fixture, with no configuration from this machine."""
    environment = os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True,
                          check=True, env=environment).stdout.strip()


def caller(action, pin):
    """A workflow with one pinned step."""
    return f"on: push\njobs:\n  build:\n    runs-on: x\n    steps:\n      - uses: {action}@{pin}\n"


class RealGitTest(unittest.TestCase):
    """Real clones of an upstream that hands out a stray object, as GitHub does for a fork."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.server = root / "server"
        upstream = cls.server / "step-security/harden-runner.git"
        upstream.mkdir(parents=True)
        run_git(upstream, "init", "--quiet", "--bare", "--initial-branch=main")
        run_git(upstream, "config", "uploadpack.allowFilter", "true")
        work = root / "work"
        work.mkdir()
        run_git(work, "init", "--quiet", "--initial-branch=main")
        run_git(work, "remote", "add", "origin", str(upstream))

        def commit(message):
            run_git(work, "commit", "--quiet", "--allow-empty", "-m", message)
            return run_git(work, "rev-parse", "HEAD")

        cls.old_on_main = commit("old")
        cls.tip_of_main = commit("tip")
        run_git(work, "push", "--quiet", "origin", "main")
        run_git(work, "checkout", "--quiet", "-b", "release/v1", cls.old_on_main)
        cls.on_a_branch = commit("only on a branch")
        run_git(work, "push", "--quiet", "origin", "release/v1")
        run_git(work, "checkout", "--quiet", "--detach", cls.old_on_main)
        cls.under_a_tag = commit("only under a lightweight tag")
        run_git(work, "tag", "v1.0.0")
        cls.annotated = commit("only under an annotated tag")
        run_git(work, "tag", "-a", "v2.0.0", "-m", "annotated")
        cls.tag_object = run_git(work, "rev-parse", "v2.0.0")
        run_git(work, "push", "--quiet", "origin", "v1.0.0", "v2.0.0")
        run_git(work, "checkout", "--quiet", "--detach", cls.old_on_main)
        cls.pull_request_head = commit("only in a pull request")
        run_git(work, "push", "--quiet", "origin", "HEAD:refs/pull/1/head")
        run_git(work, "checkout", "--quiet", "--detach", cls.old_on_main)
        cls.fork_only = commit("only in the fork network")
        run_git(work, "push", "--quiet", "origin", "HEAD:refs/heads/fork")
        run_git(upstream, "update-ref", "-d", "refs/heads/fork")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def environment(self):
        return {"GITHUB_SERVER_URL": f"file://{self.server}", "GITHUB_TOKEN": "t", "GH_TOKEN": "",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def compare(self, pin):
        return run_compare(workflow(), workflow(step=f"{ACTION}@{pin}"),
                           environment=self.environment())

    def test_a_commit_in_the_history_of_a_branch_or_tag_is_verified(self):
        for name in ("old_on_main", "tip_of_main", "on_a_branch", "under_a_tag", "annotated"):
            with self.subTest(name):
                result = self.compare(getattr(self, name))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(VERIFIED_NOTICE, result.stdout)

    def test_any_other_object_is_refused(self):
        cases = {"fork network": self.fork_only, "pull request": self.pull_request_head,
                 "no such object": "0123456789abcdef0123456789abcdef01234567",
                 "annotated tag object": self.tag_object}
        for name, pin in cases.items():
            with self.subTest(name):
                result = self.compare(pin)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"pin {ACTION}@{pin} is not a commit of {ACTION}", result.stderr)
                self.assertIn("none of the 5 commits on its branches and tags", result.stderr)

    def test_the_fixture_hands_out_the_fork_commit_to_a_lookup(self):
        """Control: refusing it above means something only while a lookup would succeed."""
        clone = Path(self.directory.name) / "control"
        run_git(self.directory.name, "clone", "--quiet", "--bare", "--filter=tree:0",
                f"file://{self.server}/step-security/harden-runner.git", str(clone))
        self.assertNotIn(self.fork_only, run_git(clone, "rev-list", "--branches", "--tags"))
        run_git(clone, "cat-file", "-e", self.fork_only)
        self.assertNotIn(self.fork_only, run_git(clone, "rev-list", "--branches", "--tags"))

    def test_a_repository_that_does_not_exist_warns(self):
        result = run_compare(caller("acme/gone", OLD), caller("acme/gone", NEW),
                             environment=self.environment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"pin acme/gone@{NEW} {UNVERIFIED}", result.stdout)
        self.assertNotIn(VERIFIED_NOTICE, result.stdout)


class FakeGitCase(unittest.TestCase):
    """Run the CLI with a recording `git`."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / "git.log"

    def environment(self, commits=NEW, **variables):
        return pins.reachable_everything() | {"FAKE_GIT_LOG": str(self.log),
                                              "FAKE_GIT_COMMITS": commits} | variables

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def clones(self):
        return [call for call in self.calls() if call["arguments"][0] == "clone"]

    def failing_for(self, repository):
        """PATH for a `git` that fails for one repository and works for the others."""
        script = Path(self.directory.name) / "git"
        script.write_text("#!/usr/bin/env bash\n"
                          f'case "$*" in *{repository}.git*) exit 128;; esac\n'
                          f'exec {pins.FAKE_GIT / "git"} "$@"\n')
        script.chmod(0o755)
        return f"{self.directory.name}{os.pathsep}{os.environ['PATH']}"

    def compare(self, consumer=None, *, wrapper=False, actions=False, **variables):
        """A tolerated pin in a workflow, unless a consumer says otherwise."""
        return run_compare(workflow(), consumer or workflow(step=PATCHED), wrapper=wrapper,
                           actions=actions, environment=self.environment(**variables))

    def assert_verified(self, result, path=WORKFLOW):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(VERIFIED_NOTICE, result.stdout)
        self.assertIn(PIN_NOTICE, result.stdout)
        self.assertIn(f"≈ {path}", result.stdout)
        self.assertIn(CLEAN, result.stdout)
        self.assertNotIn(UNVERIFIED, result.stdout)

    def assert_unverified(self, result, uses=PATCHED, path=WORKFLOW):
        """Not a failure, and in no way a success for that pin."""
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"{path}: pin {uses} {UNVERIFIED}", result.stdout)
        self.assertIn(f"? {path}", result.stdout)
        self.assertIn(SUMMARY, result.stdout)
        self.assertNotIn(VERIFIED_NOTICE, result.stdout)
        self.assertNotIn(CLEAN, result.stdout)
        self.assertNotIn(f"≈ {path}", result.stdout)

    def assert_unreachable(self, result, uses=PATCHED, path=WORKFLOW):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        repository = "/".join(uses.split("@")[0].split("/")[:2])
        self.assertIn(f"{path}: pin {uses} is not a commit of {repository}", result.stderr)
        self.assertIn(f"  - {path}", result.stderr)
        self.assertIn(f"=== diff: {path} ===", result.stderr)
        self.assertNotIn(VERIFIED_NOTICE, result.stdout)
        self.assertNotIn(CLEAN, result.stdout)


class FirstCaseTest(FakeGitCase):
    """The real pair behind the tolerance: the old pin, and a commit of a real fork."""

    def run_checks(self, consumer, commits):
        return run_compare(fixture("template-checks.yml"), consumer, path=CHECKS,
                           template_name="typo3-extension", environment=self.environment(commits))

    def test_the_real_tolerated_pin_is_verified(self):
        consumer = fixture("consumer-checks.yml")
        self.assertIn(f"{ACTION}@{OLD}", consumer)
        self.assert_verified(self.run_checks(consumer, f"{NEW} {OLD}"), CHECKS)

    def test_the_real_fork_commit_is_differing(self):
        consumer = fixture("consumer-checks.yml").replace(OLD, FORK)
        result = self.run_checks(consumer, f"{NEW} {OLD}")
        self.assert_unreachable(result, f"{ACTION}@{FORK}", CHECKS)
        self.assertIn("none of the 2 commits on its branches and tags", result.stderr)


class FailureTest(FakeGitCase):
    """Whatever stops the check warns; it neither fails the run nor reads as a pass."""

    def test_every_failure_warns(self):
        for name, variables in {
            "clone refused": {"FAKE_GIT_CLONE_EXIT": "128"},
            "clone fails otherwise": {"FAKE_GIT_CLONE_EXIT": "1"},
            "listing fails": {"FAKE_GIT_REV_LIST_EXIT": "128"},
            "nothing listed": {"FAKE_GIT_COMMITS": ""},
            "no git": {"PATH": "/nonexistent"},
        }.items():
            with self.subTest(name):
                self.assert_unverified(self.compare(**variables))

    def test_the_reason_names_the_failure(self):
        for variables, reason in (({"FAKE_GIT_CLONE_EXIT": "128"},
                                   "git exited with 128: fatal: clone went wrong"),
                                  ({"FAKE_GIT_COMMITS": ""}, f"{ACTION} has no branch or tag"),
                                  ({"PATH": "/nonexistent"}, "git clone did not finish")):
            with self.subTest(reason):
                self.assertIn(reason, self.compare(**variables).stdout)

    def test_a_silent_failure_still_gives_a_reason(self):
        with mock.patch.object(pin_reachability.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stderr="", stdout="")):
            state, reason = PinChecker("t", "unused").check("a/b", NEW)
        self.assertIs(state, Reachability.UNVERIFIED)
        self.assertIn("git exited with 1: no message", reason)

    def test_workflow_annotations_tell_the_three_results_apart(self):
        reachable = self.compare(actions=True)
        self.assertIn("::notice::1 workflow file(s) differ only", reachable.stdout)
        self.assertNotIn("::warning::", reachable.stdout)

        failed = self.compare(actions=True, FAKE_GIT_CLONE_EXIT="128")
        self.assertIn(f"::warning::{WORKFLOW}: pin {PATCHED} {UNVERIFIED}", failed.stdout)
        self.assertNotIn("::notice::", failed.stdout)
        self.assertNotIn("::error::", failed.stdout + failed.stderr)

        refused = self.compare(actions=True, FAKE_GIT_COMMITS=OLD)
        self.assertIn(f"::error::{WORKFLOW}: pin {PATCHED} is not a commit", refused.stderr)
        self.assertNotIn("::warning::", refused.stdout)

    def test_an_opt_in_gives_unverified_its_own_exit_code(self):
        failing = {"FAKE_GIT_CLONE_EXIT": "128"}
        self.assertEqual(self.compare(DRIFT_UNVERIFIED_EXIT="1", **failing).returncode, 3)
        self.assertEqual(self.compare(DRIFT_UNVERIFIED_EXIT="", **failing).returncode, 0)
        self.assertEqual(self.compare(DRIFT_UNVERIFIED_EXIT="1").returncode, 0)
        self.assertEqual(self.compare(DRIFT_UNVERIFIED_EXIT="1", FAKE_GIT_COMMITS=OLD).returncode, 1)
        self.assertEqual(self.compare(DRIFT_UNVERIFIED_EXIT="1", wrapper=True, **failing).returncode, 3)


class CallTest(FakeGitCase):
    """What is fetched, how, and how often."""

    def test_the_clone_takes_commits_of_branches_and_tags_only(self):
        self.assert_verified(self.compare(GITHUB_SERVER_URL="https://example.test"))
        clone, listing = self.calls()
        self.assertEqual(clone["arguments"][:4], ["clone", "--quiet", "--bare", "--filter=tree:0"])
        self.assertEqual(clone["arguments"][4], f"https://example.test/{ACTION}.git")
        self.assertEqual(listing["arguments"], ["-C", clone["arguments"][5], "rev-list",
                                                "--branches", "--tags"])

    def test_git_can_neither_prompt_nor_fetch_on_demand(self):
        self.compare()
        for call in self.calls():
            self.assertEqual(call["environment"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(call["environment"]["GIT_NO_LAZY_FETCH"], "1")

    def test_the_token_reaches_the_server_and_no_command_line(self):
        self.compare(GITHUB_TOKEN="s3cret", GITHUB_SERVER_URL="https://example.test")
        credentials = base64.b64encode(b"x-access-token:s3cret").decode()
        for call in self.calls():
            self.assertEqual(call["environment"]["GIT_CONFIG_COUNT"], "1")
            self.assertEqual(call["environment"]["GIT_CONFIG_KEY_0"],
                             "http.https://example.test/.extraheader")
            self.assertEqual(call["environment"]["GIT_CONFIG_VALUE_0"],
                             f"Authorization: Basic {credentials}")
            self.assertNotIn("s3cret", " ".join(call["arguments"]))
            self.assertNotIn(credentials, " ".join(call["arguments"]))

    def test_a_pin_equal_to_the_template_is_not_checked(self):
        result = run_compare(workflow(), workflow(job=f"{REUSABLE}@{NEW}"),
                             environment=self.environment())
        self.assert_verified(result)
        self.assertEqual(len(self.clones()), 1)
        self.assertIn("/acme/workflows.git", self.clones()[0]["arguments"][4])

        self.log.unlink()
        identical = run_compare(workflow(), "# prose\n" + workflow(), environment=self.environment())
        self.assertEqual(identical.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_one_clone_per_repository_and_every_pin_judged_against_it(self):
        other = workflow(step=f"{ACTION}@{FORK}")
        result = run_compare(workflow(), workflow(step=PATCHED),
                             extra_template_files={OTHER: (workflow(), other)},
                             environment=self.environment())
        self.assertEqual(len(self.clones()), 1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"≈ {WORKFLOW}", result.stdout)
        self.assertIn(f"{OTHER}: pin {ACTION}@{FORK} is not a commit", result.stderr)

    def test_a_failure_is_not_retried_and_is_named_per_file(self):
        same = workflow(step=PATCHED)
        result = run_compare(workflow(), same, extra_template_files={OTHER: (workflow(), same)},
                             environment=self.environment(FAKE_GIT_CLONE_EXIT="128"))
        self.assertEqual(len(self.clones()), 1)
        self.assert_unverified(result)
        self.assertIn(f"{OTHER}: pin {PATCHED} {UNVERIFIED}", result.stdout)

    def test_a_pin_twice_in_a_file_warns_once(self):
        def document(pin):
            return f"""on: push
jobs:
  one:
    runs-on: ubuntu-latest
    steps:
      - uses: {ACTION}@{pin}
  two:
    runs-on: ubuntu-latest
    steps:
      - uses: {ACTION}@{pin}
"""
        result = run_compare(document(OLD), document(NEW),
                             environment=self.environment(FAKE_GIT_CLONE_EXIT="128"))
        self.assertEqual(result.stdout.count(f"{UNVERIFIED} against"), 1)

    def test_two_paths_of_one_repository_share_a_clone(self):
        def steps(pin):
            return f"""on: push
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: github/codeql-action/init@{pin}
      - uses: github/codeql-action/analyze@{pin}
"""
        self.assert_verified(run_compare(steps(OLD), steps(NEW), environment=self.environment()))
        self.assertEqual(len(self.clones()), 1)
        self.assertIn("/github/codeql-action.git", self.clones()[0]["arguments"][4])

    def test_entries_that_are_no_mappings_are_passed_over(self):
        def document(pin):
            return f"""on: push
jobs:
  odd: nothing
  count:
    steps: 2
  build:
    runs-on: ubuntu-latest
    steps:
      - nothing
      - uses: {ACTION}@{pin}
"""
        self.assert_verified(run_compare(document(OLD), document(NEW),
                                         environment=self.environment()))
        self.assertEqual(len(self.clones()), 1)

    def test_every_differing_pin_of_a_file_is_checked(self):
        for consumer, refused in ((workflow(step=PATCHED, job=f"{REUSABLE}@{FORK}"),
                                   f"{REUSABLE}@{FORK}"),
                                  (workflow(step=f"{ACTION}@{FORK}", job=f"{REUSABLE}@{NEW}"),
                                   f"{ACTION}@{FORK}")):
            with self.subTest(refused):
                self.assert_unreachable(self.compare(consumer), refused)


class BudgetTest(unittest.TestCase):
    """The time a run may spend; the budget belongs to the run, not to a repository."""

    def test_the_run_deadline_stops_further_git_calls(self):
        limit = pin_reachability.RUN_DEADLINE
        clock = iter([0.0, 1.0, limit - 1.0, limit, limit + 1.0])
        timeouts = []

        def fake_run(_arguments, **keywords):
            timeouts.append(keywords["timeout"])
            return SimpleNamespace(returncode=0, stderr="", stdout=NEW)

        checker = PinChecker("t", "unused")
        with mock.patch.object(pin_reachability, "time",
                               SimpleNamespace(monotonic=lambda: next(clock))), \
                mock.patch.object(pin_reachability.subprocess, "run", fake_run):
            self.assertIs(checker.check("a/one", NEW)[0], Reachability.REACHABLE)
            self.assertIs(checker.check("a/two", NEW)[0], Reachability.REACHABLE)
            state, reason = checker.check("a/three", NEW)
        self.assertIs(state, Reachability.UNVERIFIED)
        self.assertIn(f"the budget of {limit} s for this run is spent", reason)
        git_timeout = pin_reachability.GIT_TIMEOUT
        self.assertEqual(timeouts, [git_timeout, git_timeout, 1.0, 0.0])

    def test_a_hanging_git_is_stopped(self):
        environment = pins.reachable_everything() | {"FAKE_GIT_SECONDS": "30"}
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(pin_reachability, "GIT_TIMEOUT", 0.2):
            state, reason = PinChecker("t", "https://example.test").check("a/b", NEW)
        self.assertIs(state, Reachability.UNVERIFIED)
        self.assertIn("git clone did not finish", reason)


class WorkflowWiringTest(unittest.TestCase):
    """Without the token in these steps every pin would stay unverified, silently."""

    @staticmethod
    def steps(workflow_file, job):
        text = (pins.ROOT / ".github/workflows" / workflow_file).read_text()
        document = yaml.safe_load(text)
        return {entry.get("name"): entry for entry in document["jobs"][job]["steps"]}

    def test_both_workflows_pass_the_job_token_to_the_comparator(self):
        step = self.steps("check-template-drift.yml", "drift")["Compare consumer against template"]
        self.assertEqual(step["env"]["GITHUB_TOKEN"], "${{ github.token }}")
        self.assertNotIn("DRIFT_UNVERIFIED_EXIT", step["env"])
        step = self.steps("drift-scan.yml", "scan")["Run drift check"]
        self.assertEqual(step["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")

    def test_the_scan_neither_opens_nor_closes_an_issue_for_an_unchecked_pin(self):
        steps = self.steps("drift-scan.yml", "scan")
        self.assertTrue(steps["Run drift check"]["env"]["DRIFT_UNVERIFIED_EXIT"])
        self.assertEqual(steps["Open or update drift issue"]["if"],
                         "steps.drift.outputs.status != '0' && steps.drift.outputs.status != '3'")
        self.assertEqual(steps["Close stale drift issue"]["if"],
                         "steps.drift.outputs.status == '0'")

    def test_the_scan_prints_the_warning_only_where_no_issue_carries_it(self):
        script = self.steps("drift-scan.yml", "scan")["Run drift check"]["run"]
        script = script.replace('OUTPUT=$(bash "$GITHUB_WORKSPACE/templates-src/scripts/check-drift.sh" '
                                '"$CONSUMER_DIR" "$TEMPLATE_DIR" 2>&1)',
                                'OUTPUT="::warning::the message"; (exit "$FIXED_STATUS")')
        self.assertNotIn("check-drift.sh", script)
        with tempfile.NamedTemporaryFile() as output:
            for status, printed in ((3, True), (0, False), (1, False)):
                with self.subTest(status=status):
                    result = subprocess.run(
                        ["bash", "-c", script], capture_output=True, text=True, check=False,
                        env=os.environ | {"GITHUB_OUTPUT": output.name, "FIXED_STATUS": str(status)})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual("::warning::the message" in result.stdout, printed)


class CombinedResultTest(FakeGitCase):
    """One result must not hide another."""

    def test_verified_unverified_and_refused_files_in_one_run(self):
        files = {f".github/workflows/{name}.yml": (caller(f"a/{name}", OLD), caller(f"a/{name}", pin))
                 for name, pin in (("down", NEW), ("evil", FORK))}
        result = run_compare(caller("a/ok", OLD), caller("a/ok", NEW), extra_template_files=files,
                             environment=self.environment(PATH=self.failing_for("a/down")))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"≈ {WORKFLOW}", result.stdout)
        self.assertIn("? .github/workflows/down.yml", result.stdout)
        self.assertNotIn("≈ .github/workflows/down.yml", result.stdout)
        self.assertIn("  - .github/workflows/evil.yml", result.stderr)
        self.assertNotIn("  - .github/workflows/down.yml", result.stderr)
        self.assertNotIn(SUMMARY, result.stdout)

    def test_a_pin_that_cannot_be_checked_does_not_hide_a_refused_one_in_its_file(self):
        consumer = workflow(step=f"{ACTION}@{FORK}", job=f"{REUSABLE}@{NEW}")
        result = run_compare(workflow(), consumer,
                             environment=self.environment(PATH=self.failing_for("acme/workflows")))
        self.assert_unreachable(result, f"{ACTION}@{FORK}")
        self.assertIn(f"pin {REUSABLE}@{NEW} {UNVERIFIED}", result.stdout)

    def test_a_warning_does_not_hide_a_missing_file(self):
        result = run_compare(workflow(), workflow(step=PATCHED),
                             extra_template_files={OTHER: (workflow(), None)},
                             environment=self.environment(FAKE_GIT_CLONE_EXIT="128"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(UNVERIFIED, result.stdout)
        self.assertIn(f"  - {OTHER}", result.stderr)


class WithoutTokenTest(FakeGitCase):
    """Local runs: the same warning, and nothing fetched."""

    NO_TOKEN = {"GITHUB_TOKEN": "", "GH_TOKEN": ""}

    def test_script_and_wrapper_warn_and_fetch_nothing(self):
        for wrapper in (False, True):
            with self.subTest(wrapper=wrapper):
                result = self.compare(wrapper=wrapper, **self.NO_TOKEN)
                self.assert_unverified(result)
                self.assertIn("no GITHUB_TOKEN or GH_TOKEN is set, so nothing was fetched",
                              result.stdout)
                self.assertEqual(self.calls(), [])

    def test_either_variable_enables_the_check(self):
        for name in self.NO_TOKEN:
            with self.subTest(name):
                self.assert_verified(self.compare(**self.NO_TOKEN | {name: "from-" + name}))

    def test_github_is_the_default_server(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True):
            checker = PinChecker.from_environment()
        self.assertEqual(checker.server_url, "https://github.com")


@unittest.skipUnless(os.environ.get("DRIFT_LIVE_TOKEN"), "needs the token of a CI job")
class LiveTest(unittest.TestCase):
    """GitHub itself: the check must keep telling these commits apart.

    An unverified result fails here on purpose. With the job's token it would mean
    the check has stopped working for every consumer, where it only warns.
    """

    def run_live(self, consumer):
        token = {"GITHUB_TOKEN": os.environ["DRIFT_LIVE_TOKEN"], "GH_TOKEN": "",
                 "GITHUB_SERVER_URL": "https://github.com"}
        return run_compare(fixture("template-checks.yml"), consumer, path=CHECKS,
                           template_name="typo3-extension", environment=token)

    def test_the_real_pin_is_verified(self):
        result = self.run_live(fixture("consumer-checks.yml"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(VERIFIED_NOTICE, result.stdout)

    def test_real_fork_commits_are_refused(self):
        for pin in (FORK, FORK_PULL_REQUEST):
            with self.subTest(pin):
                result = self.run_live(fixture("consumer-checks.yml").replace(OLD, pin))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"pin {ACTION}@{pin} is not a commit", result.stderr)


if __name__ == "__main__":
    unittest.main()
