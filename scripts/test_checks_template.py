"""Fixtures for the TYPO3 Checks template: PR cancellation and the gate verdict.

A run cancelled on the head SHA leaves a failed `All security checks`
check-run there, and the newer run's green gate does not replace it: each run
keeps its own check-run and the commit's rollup stays FAILURE. These tests pin
the two halves of the fix: only the `supersede` job cancels, and only runs for
other head SHAs while its own run is for the live head (#592), and the gate
still fails on `cancelled`, so the fix is not a gate tolerance.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from drift_compare import WorkflowLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates/typo3-extension/.github/workflows/checks.yml"
SUPERSEDE_IF = ("github.event_name == 'pull_request'"
                " && github.event.pull_request.head.repo.full_name == github.repository")
HEAD = "a" * 40
OLD = "b" * 40
REPO = "netresearch/example"

# A stand-in for `gh api` that serves a fixture, applies `--jq` with the real
# jq and logs every cancel request. Unknown calls fail loudly.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
fx = json.loads(os.environ["FIXTURE"])
OWN_CREATED = fx.get("own_created", "2026-11-11T10:00:00Z")
args = sys.argv[1:]
assert args[0] == "api", args
method, jq, fields, path, i = "GET", None, {}, None, 1
while i < len(args):
    a = args[i]
    if a == "--method": method = args[i + 1]; i += 2
    elif a == "--jq": jq = args[i + 1]; i += 2
    elif a == "-f": k, v = args[i + 1].split("=", 1); fields[k] = v; i += 2
    elif a == "--paginate": i += 1
    else: path = a; i += 1
def out(body):
    if jq is None: print(json.dumps(body)); return
    sys.stdout.write(subprocess.run(["jq", "-r", jq], input=json.dumps(body), text=True,
                                    capture_output=True, check=True).stdout)
m = re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/cancel", path)
if method == "POST" and m:
    with open(os.environ["CANCEL_LOG"], "a") as log: log.write(m.group(1) + "\n")
    sys.exit(1 if int(m.group(1)) in fx.get("cancel_fail", []) else 0)
if re.fullmatch(r"repos/[^/]+/[^/]+/pulls/\d+", path):
    if fx["live"] is None: sys.exit(1)
    out({"head": {"sha": fx["live"]}}); sys.exit(0)
if re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/\d+", path):
    out({"workflow_id": 7, "created_at": OWN_CREATED}); sys.exit(0)
if path.endswith("/actions/workflows/7/runs"):
    assert fields["event"] == "pull_request", fields
    if fx.get("list_fails"): sys.exit(1)
    runs = [r for r in fx["runs"] if r["status"] == fields["status"] and r["head_branch"] == fields["branch"]]
    out({"workflow_runs": runs}); sys.exit(0)
sys.exit(f"unexpected gh call: {args}")
'''


def run(run_id, sha, status="in_progress", branch="feature", repo=REPO,
        created="2026-11-11T09:59:00Z"):
    # The fake reports this run's own `created_at` as 2026-11-11T10:00:00Z.
    return {"id": run_id, "head_sha": sha, "status": status, "head_branch": branch,
            "head_repository": {"full_name": repo}, "created_at": created}


def load_template():
    return yaml.load(TEMPLATE.read_text(), Loader=WorkflowLoader)


class ConcurrencyTest(unittest.TestCase):
    def test_no_concurrency_group(self):
        # Every group-based rule let a run for the head SHA cancel or replace
        # another one (#592); superseded runs are cancelled by `supersede`.
        self.assertNotIn("concurrency", load_template())
        for name, job in load_template()["jobs"].items():
            with self.subTest(job=name):
                self.assertNotIn("concurrency", job)

    def test_gate_needs_every_job_except_supersede(self):
        jobs = load_template()["jobs"]
        expected = set(jobs) - {"gate", "supersede"}
        self.assertEqual(set(jobs["gate"]["needs"]), expected)

    def test_supersede_runs_on_same_repository_pull_requests_only(self):
        job = load_template()["jobs"]["supersede"]
        self.assertEqual(job["if"], SUPERSEDE_IF)
        self.assertEqual(job["permissions"], {"actions": "write", "pull-requests": "read"})

    def test_pull_request_types_that_reuse_the_head_sha_are_still_triggers(self):
        # The narrowed cancellation matters only because these run at all.
        types = load_template()["on"]["pull_request"]["types"]
        self.assertIn("ready_for_review", types)
        self.assertIn("reopened", types)
        self.assertIn("synchronize", types)


class SupersedeStepTest(unittest.TestCase):
    """Run the supersede step exactly as the template writes it, against a fake `gh`."""

    @classmethod
    def setUpClass(cls):
        steps = [step for step in load_template()["jobs"]["supersede"]["steps"] if "run" in step]
        assert len(steps) == 1, steps
        cls.script = steps[0]["run"]

    def execute(self, live, runs, **extra):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            gh = base / "gh"
            gh.write_text(FAKE_GH)
            gh.chmod(0o755)
            log = base / "cancelled"
            log.touch()
            env = {"PATH": f"{base}:{os.environ['PATH']}", "CANCEL_LOG": str(log),
                   "FIXTURE": json.dumps({"live": live, "runs": runs, **extra}),
                   "REPO": REPO, "PR_NUMBER": "12", "HEAD_SHA": HEAD, "HEAD_REF": "feature",
                   "RUN_ID": "100"}
            result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", self.script],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return sorted(log.read_text().split()), result.stdout

    def test_cancels_only_runs_for_earlier_heads_of_this_branch(self):
        runs = [run(100, HEAD), run(101, HEAD), run(102, HEAD, "queued"),  # same SHA: never
                run(201, OLD), run(202, OLD, "queued"), run(203, "c" * 40, "waiting"),
                run(301, OLD, branch="other"), run(302, OLD, repo="fork/example")]
        cancelled, _ = self.execute(HEAD, runs)
        self.assertEqual(cancelled, ["201", "202", "203"])

    def test_runs_created_after_this_one_are_never_cancelled(self):
        # A push between the head read and the listing starts a run for a
        # newer head; cancelling it would leave a failed gate on that head.
        runs = [run(201, OLD), run(401, "d" * 40, created="2026-11-11T10:00:05Z"),
                run(402, "e" * 40, "queued", created="2026-11-11T10:00:00Z")]
        cancelled, _ = self.execute(HEAD, runs)
        self.assertEqual(cancelled, ["201"])

    def test_run_whose_head_moved_on_cancels_nothing(self):
        # A re-run or a late event for an earlier commit: the newer head's run
        # is listed with a different SHA and must survive.
        cancelled, output = self.execute(OLD, [run(201, OLD), run(100, HEAD)])
        self.assertEqual(cancelled, [])
        self.assertIn("nothing cancelled", output)

    def test_errors_cancel_nothing_and_do_not_fail(self):
        for extra, live in (({}, None), ({"list_fails": True}, HEAD)):
            with self.subTest(extra=extra, live=live):
                cancelled, output = self.execute(live, [run(201, OLD)], **extra)
                self.assertEqual(cancelled, [])
                self.assertIn("::warning::", output)

    def test_failed_cancel_is_a_warning(self):
        cancelled, output = self.execute(HEAD, [run(201, OLD), run(202, OLD)], cancel_fail=[201])
        self.assertEqual(cancelled, ["201", "202"])
        self.assertIn("::warning::Could not cancel run 201", output)
        self.assertIn("Cancelled run 202", output)


class GateVerdictTest(unittest.TestCase):
    """Run the gate step exactly as the template writes it."""

    @classmethod
    def setUpClass(cls):
        gate = load_template()["jobs"]["gate"]
        cls.needs = gate["needs"]
        steps = [step for step in gate["steps"] if "run" in step]
        assert len(steps) == 1, steps
        cls.script = steps[0]["run"]
        cls.condition = gate["if"]

    def test_gate_reports_whatever_its_needs_did(self):
        # Without `always()` a failed or cancelled scan would skip the gate,
        # and a skipped required check counts as passed.
        self.assertEqual(self.condition, "${{ always() }}")

    def verdict(self, results):
        payload = {name: {"result": result, "outputs": {}} for name, result in results.items()}
        # `shell: bash` runs as `bash --noprofile --norc -eo pipefail {0}`.
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", self.script],
                              env={"RESULTS": json.dumps(payload), "PATH": os.environ["PATH"]},
                              capture_output=True, text=True, check=False)

    def assert_verdict(self, results, code):
        result = self.verdict(results)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_all_success_passes(self):
        self.assert_verdict(dict.fromkeys(self.needs, "success"), 0)

    def test_skipped_passes(self):
        results = dict.fromkeys(self.needs, "success")
        results.update({"scorecard": "skipped", "pr-quality": "skipped"})
        self.assert_verdict(results, 0)

    def test_one_failure_fails(self):
        for name in self.needs:
            with self.subTest(job=name):
                results = dict.fromkeys(self.needs, "success")
                results[name] = "failure"
                result = self.assert_verdict(results, 1)
                self.assertIn(f"{name}=failure", result.stdout)

    def test_one_cancelled_fails(self):
        # A timed-out or hand-cancelled scan scanned nothing; it must not pass.
        for name in self.needs:
            with self.subTest(job=name):
                results = dict.fromkeys(self.needs, "success")
                results[name] = "cancelled"
                result = self.assert_verdict(results, 1)
                self.assertIn(f"{name}=cancelled", result.stdout)

    def test_superseded_run_shape_still_fails(self):
        # The needs of t3x-nr-vault run 42936523040 when the concurrency group
        # cancelled it. The gate is right to fail here; the fix keeps such a
        # run from being cancelled on the head SHA, it does not make this pass.
        results = dict.fromkeys(self.needs, "cancelled")
        results.update({"scorecard": "skipped", "pr-quality": "skipped"})
        result = self.assert_verdict(results, 1)
        self.assertIn("security=cancelled", result.stdout)


if __name__ == "__main__":
    unittest.main()
