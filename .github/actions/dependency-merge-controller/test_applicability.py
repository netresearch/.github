"""Exercise v2 applicability through the real authenticated HTTP boundary."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest import mock

from api_target import ApiError
from policy import Blocked, decide, digest, initial_intent, validate_policy
from runner import allowed_completion, evaluate_and_enqueue, write_receipt
from test_api_economy import Response, Server, run_record
from test_policy import REPO, SHA, fixtures

BASE, ASSOCIATED, MERGE_BASE, BLOB = (character * 40 for character in "bdef")
DOC_CHECK = "docs / Render Documentation"
DOC_WORKFLOW = "docs.yml"
DOC_PATH = "Documentation/a.rst"
OLD_DOC_PATH = "Documentation/old.rst"


class PathServer(Server):
    """Supply bounded immutable path evidence at the real adapter boundary."""

    def __init__(self, paths=None, *, diverged=False):
        """Build a same-repository v2 cohort with distinct live/associated bases."""
        super().__init__()
        self.policy.update(version=2, base_ref="main", conditional_checks=[{
            "name": DOC_CHECK, "app_id": 15368,
            "workflow": DOC_WORKFLOW, "events": ["pull_request"],
            "workflow_blob_oid": BLOB,
            "paths": {"exact": [".github/workflows/docs.yml"], "prefix": ["Documentation/"]}}])
        self.raw.update(baseRefOid=ASSOCIATED, changedFiles=len(paths or ["composer.json"]),
            baseRef={"name": "main", "prefix": "refs/heads/", "repository": {"nameWithOwner": REPO},
                     "target": {"__typename": "Commit", "oid": BASE}})
        self.compare = {"base_commit": {"sha": BASE},
            "merge_base_commit": {"sha": MERGE_BASE if diverged else BASE},
            "status": "diverged" if diverged else "ahead", "behind_by": 6 if diverged else 0,
            "ahead_by": 1, "total_commits": 1, "commits": [{"sha": SHA}],
            "files": [{"filename": path, "status": "modified"} for path in paths or ["composer.json"]]}
        self.ancestor = copy.deepcopy(self.compare)
        self.ancestor.update(base_commit={"sha": MERGE_BASE}, merge_base_commit={"sha": MERGE_BASE},
                             status="ahead", behind_by=0)
        self.blobs = {"b0": {"__typename": "Blob", "oid": BLOB}, "h0": {"__typename": "Blob", "oid": BLOB}}
        self.closing = None
        self.blob_requests = []
        self.closing_errors = []

    def open(self, request, **kwargs):
        """Answer only the new compare/blob reads alongside the existing fake."""
        parsed = urllib.parse.urlsplit(request.full_url)
        resource = parsed.path.removeprefix(f"/repos/{REPO}/")
        if resource.startswith("compare/"):
            self.calls.append((resource, {}))
            return Response(self.ancestor if resource == f"compare/{MERGE_BASE}...{SHA}" else self.compare)
        if parsed.path == "/graphql" and "object(" in json.loads(request.data)["query"]:
            self.blob_requests.append(json.loads(request.data))
            self.calls.append(("/graphql", {}))
            return Response({"data": {"repository": {"nameWithOwner": REPO,
                "pullRequest": copy.deepcopy(self.closing or self.raw), **self.blobs}}, "errors": self.closing_errors})
        return super().open(request, **kwargs)

    def producer(self, **changes):
        """Add a real matching server run and its bound current Actions job."""
        run = run_record(200, DOC_WORKFLOW, **changes)
        self.runs[200] = run
        self.listed.append(run)
        self.checks.append(self.check(200, DOC_CHECK,
            status=run["status"], conclusion=run["conclusion"]))


def decision(server):
    """Collect through HTTP, create trusted intent and run the real policy."""
    context, _ = fixtures()
    api = server.client()
    with server.transport():
        snapshot = api.snapshot(42)
    intent = initial_intent(context, snapshot, server.policy)
    return decide(context, snapshot, server.policy, intent), api, snapshot


class ApplicabilityTest(unittest.TestCase):
    """Falsify missing evidence, waived jobs, provenance drift and API overruns."""

    def test_nonmatching_complete_diff_needs_no_absent_producer(self):
        """An unrelated complete diff has no conditional workflow obligation."""
        result, api, snapshot = decision(PathServer())
        self.assertEqual(result[0], "ready")
        self.assertEqual(api.api_calls, 20)
        self.assertEqual(snapshot["pr"]["base"], BASE)
        self.assertEqual(snapshot["pr"]["pr_base_oid"], ASSOCIATED)

    def test_matching_missing_producer_waits_then_real_job_succeeds(self):
        """Matching paths must wait until the real current producer succeeds."""
        server = PathServer(["Documentation/Api/example.rst"])
        self.assertEqual(decision(server)[0][0], "waiting")
        server.producer()
        result, api, _ = decision(server)
        self.assertEqual(result[0], "ready")
        self.assertEqual(api.api_calls, 21)  # one actual extra job inventory
        self.assertTrue(allowed_completion(server.runs[200], {"intent_path": "intent.yml"}, server.policy))

    def test_diverged_bot_diff_uses_second_immutable_comparison(self):
        """A normal behind-base bot branch uses one additional immutable read."""
        result, api, snapshot = decision(PathServer(diverged=True))
        self.assertEqual(result[0], "ready")
        self.assertEqual(api.api_calls, 21)
        self.assertEqual(snapshot["applicability"]["merge_base"], MERGE_BASE)

    def test_native_rule_cannot_be_only_conditional(self):
        """Native obligations remain unconditional even on nonmatching paths."""
        server = PathServer()
        server.rules[1]["parameters"]["required_status_checks"].append(
            {"context": DOC_CHECK, "integration_id": 15368})
        self.assertEqual(decision(server)[0][0], "blocked")

    def test_compare_endpoint_allows_only_exact_repository_immutable_get(self):
        """Ref aliases, queries, foreign repositories and writes cannot reach I/O."""
        api = PathServer().client()
        route = f"/repos/{REPO}/compare/{BASE}...{SHA}"
        self.assertTrue(api.api_url(route).endswith(route))
        for invalid in (route + "?page=1", route + "?per_page=100", route.replace(BASE, "main"),
                        route.replace(BASE, BASE[:7]), route.replace("...", ".."),
                        route.replace(REPO, "attacker/fork"), route.replace(BASE, BASE.upper())):
            with self.subTest(invalid=invalid), self.assertRaises(ApiError):
                api.api_url(invalid)
        with self.assertRaises(ApiError):
            api.api_url(route, "POST")

    def test_invalid_complete_file_and_commit_evidence_blocks(self):
        """Partial, renamed, malformed or incorrectly bound evidence cannot pass."""
        mutations = [
            lambda s: s.compare.pop("files"),
            lambda s: s.compare.update(files=[]),
            lambda s: s.compare.update(files=s.compare["files"] * 2),
            lambda s: s.compare.update(total_commits=True),
            lambda s: s.compare.update(commits=[]),
            lambda s: s.compare.update(commits=[{"sha": BASE}]),
            lambda s: s.compare.update(status="behind"),
            lambda s: s.compare.update(behind_by=True),
            lambda s: s.compare.update(too_large=True),
            lambda s: s.compare["base_commit"].update(sha=ASSOCIATED),
            lambda s: s.compare["merge_base_commit"].update(sha=MERGE_BASE),
            lambda s: s.compare["files"][0].update(status="renamed", previous_filename=OLD_DOC_PATH),
            lambda s: s.compare["files"][0].update(previous_filename=OLD_DOC_PATH),
            lambda s: s.compare["files"][0].update(filename="../Documentation/a"),
            lambda s: s.compare["files"][0].update(filename="Documentation/%61.rst"),
            lambda s: s.raw.update(changedFiles=2),
        ]
        for index, mutate in enumerate(mutations):
            server = PathServer()
            mutate(server)
            api = server.client()
            with self.subTest(index=index), server.transport(), self.assertRaises((ApiError, ValueError)):
                api.snapshot(42)

    def test_drift_is_rejected_even_for_nonmatching_paths(self):
        """Unrelated changed paths cannot hide drift in a declared workflow."""
        for alias in ("b0", "h0"):
            for value in (None, {"__typename": "Tree", "oid": BLOB}, {"__typename": "Blob", "oid": BASE}):
                server = PathServer()
                server.blobs[alias] = value
                api = server.client()
                with self.subTest(alias=alias, value=value), server.transport(), self.assertRaises((ApiError, ValueError)):
                    api.snapshot(42)

    def test_closing_current_base_and_associated_anchor_races_block(self):
        """Movement during collection invalidates the entire path snapshot."""
        for mutate in (lambda p: p["baseRef"]["target"].update(oid=MERGE_BASE),
                       lambda p: p.update(baseRefOid=MERGE_BASE), lambda p: p.update(changedFiles=2),
                       lambda p: p.update(headRefOid=BASE), lambda p: p.update(baseRefName="other"),
                       lambda p: p.update(potentialMergeCommit=None)):
            server = PathServer()
            server.closing = copy.deepcopy(server.raw)
            mutate(server.closing)
            api = server.client()
            with server.transport(), self.assertRaises((ApiError, ValueError)):
                api.snapshot(42)

    def test_live_base_ref_never_falls_back_to_associated_oid(self):
        """Missing live refs cannot borrow the PR-associated base identity."""
        for value in (None, {}, {"name": "main", "target": {"oid": BASE}}):
            server = PathServer()
            server.raw["baseRef"] = value
            api = server.client()
            with self.subTest(value=value), server.transport(), self.assertRaises((ApiError, ValueError)):
                api.snapshot(42)

    def test_applicable_newer_pending_or_unsuccessful_run_is_not_hidden(self):
        """Older green jobs cannot satisfy a newer unfinished or failed run."""
        for status, conclusion, expected in (("queued", None, "waiting"),
                ("completed", "failure", "blocked"), ("completed", "skipped", "blocked"),
                ("completed", "cancelled", "blocked")):
            server = PathServer(["Documentation/example.rst"])
            server.producer()
            server.runs[201] = run_record(201, DOC_WORKFLOW, status=status, conclusion=conclusion)
            server.listed.append(server.runs[201])
            self.assertEqual(decision(server)[0][0], expected)

    def test_invalid_versioned_schema_never_drops_obligations(self):
        """Bad declarations fail instead of disabling conditional requirements."""
        for mutate in (lambda p: p.update(version=1), lambda p: p.update(base_ref=""),
                       lambda p: p.update(conditional_checks=[]),
                       lambda p: p["conditional_checks"][0].update(events=["merge_group"]),
                       lambda p: p["conditional_checks"][0].update(workflow_blob_oid="short"),
                       lambda p: p["conditional_checks"][0]["paths"].update(prefix=["Documentation/**"]),
                       lambda p: p["conditional_checks"][0].update(name=p["required_checks"][0]["name"]),
                       lambda p: p.update(advisory_checks=[p["conditional_checks"][0]["name"]])):
            policy = PathServer().policy
            mutate(policy)
            with self.subTest(policy=policy), self.assertRaises((Blocked, ValueError)):
                validate_policy(policy)

    def test_two_ready_observer_snapshots_recompute_and_never_write(self):
        """Observation pays for both complete snapshots and attempts no mutation."""
        server = PathServer(diverged=True)
        api = server.client()
        context, _ = fixtures()
        with server.transport():
            intent = initial_intent(context, api.snapshot(42), server.policy)
            before = api.api_calls
            result = evaluate_and_enqueue(api, context, server.policy, intent)
        self.assertEqual(result[0], "observed-ready")
        self.assertEqual(api.api_calls - before, 42)
        self.assertEqual(api.mutation_attempts, 0)

    def test_full_small_boundary_inventories_and_literal_nonmatches(self):
        """Supported maxima pass while caps and filename look-alikes do not."""
        server = PathServer([f"src/file-{index}.php" for index in range(299)])
        commits = [{"sha": f"{index:040x}"} for index in range(248)] + [{"sha": SHA}]
        server.compare.update(commits=commits, total_commits=249, ahead_by=249)
        self.assertEqual(decision(server)[0][0], "ready")
        for path in ("documentation/a.rst", "Documentation-old/a.rst", "ROADMAP.md.bak"):
            self.assertEqual(decision(PathServer([path]))[0][0], "ready")
        for field, value in (("total_commits", 250), ("ahead_by", 250)):
            broken = copy.deepcopy(server)
            broken.compare[field] = value
            api = broken.client()
            with broken.transport(), self.assertRaises(ApiError):
                api.snapshot(42)
        oversized = PathServer([f"src/file-{index}.php" for index in range(300)])
        api = oversized.client()
        with oversized.transport(), self.assertRaises(ApiError):
            api.snapshot(42)

    def test_diverged_second_comparison_cannot_disagree_or_be_pending_data(self):
        """The merge-base comparison must independently confirm the same diff."""
        for mutate in (lambda s: s.ancestor["files"][0].update(filename=DOC_PATH),
                       lambda s: s.ancestor.update(commits=[]), lambda s: s.ancestor.update(status="diverged"),
                       lambda s: s.ancestor["base_commit"].update(sha=BASE)):
            server = PathServer(diverged=True)
            mutate(server)
            api = server.client()
            with server.transport(), self.assertRaises(ApiError):
                api.snapshot(42)

    def test_removed_files_and_null_previous_name_still_activate_producer(self):
        """Ordinary added, changed and removed paths all carry obligations."""
        for status in ("added", "modified", "removed"):
            server = PathServer([OLD_DOC_PATH])
            server.compare["files"][0].update(status=status, previous_filename=None)
            self.assertEqual(decision(server)[0][0], "waiting")

    def test_multiple_real_checks_from_one_workflow_are_each_mandatory(self):
        """Workflow success alone cannot replace another missing required job."""
        server = PathServer([DOC_PATH])
        server.policy["conditional_checks"].append(server.policy["conditional_checks"][0] | {"name": "docs / Second job"})
        server.producer()
        self.assertEqual(decision(server)[0][0], "waiting")
        server.checks.append(server.check(200, "docs / Second job", id=201,
            details_url=f"https://github.com/{REPO}/actions/runs/200/job/201"))
        self.assertEqual(decision(server)[0][0], "ready")

    def test_conditional_late_fallback_and_inapplicable_visible_negatives_survive(self):
        """Late required runs and visible additional failures retain their veto."""
        server = PathServer([DOC_PATH])
        server.producer(status="in_progress", conclusion=None)
        server.listed = [run for run in server.listed if run["id"] != 200]
        self.assertEqual(decision(server)[0][0], "waiting")
        for status, conclusion, expected in (("completed", "failure", "blocked"), ("in_progress", None, "waiting")):
            server = PathServer()
            server.checks[-1].update(status=status, conclusion=conclusion)
            self.assertEqual(decision(server)[0][0], expected)

    def test_missing_or_rebound_normalized_proof_never_means_inapplicable(self):
        """Incomplete normalized evidence cannot waive a producer in pure policy."""
        server = PathServer()
        _, _, snapshot = decision(server)
        context, _ = fixtures()
        intent = initial_intent(context, snapshot, server.policy)
        for mutate in (lambda s: s["applicability"]["anchor"].pop("head"),
                       lambda s: s["applicability"]["anchor"].update(base=MERGE_BASE),
                       lambda s: s["applicability"].update(policy_digest="stale"),
                       lambda s: s["applicability"].update(paths=[]),
                       lambda s: s["complete"].update(changed_files=False),
                       lambda s: s["complete"].pop("conditional_workflows")):
            broken = copy.deepcopy(snapshot)
            mutate(broken)
            self.assertEqual(decide(context, broken, server.policy, intent)[0], "blocked")

    def test_ref_identity_retarget_between_ready_snapshots_prevents_enqueue(self):
        """A same-tip change to a distinct anchor still prevents mutation."""
        server = PathServer()
        api = server.client()
        context, _ = fixtures()
        with server.transport():
            intent = initial_intent(context, api.snapshot(42), server.policy)
        original, reads = api.snapshot, []

        def snapshot(number):
            """Expose a coherent changed associated anchor only on the later read."""
            value = original(number)
            if reads:
                value["applicability"]["anchor"]["pr_base_oid"] = MERGE_BASE
                value["pr"]["pr_base_oid"] = MERGE_BASE
            reads.append(number)
            return value

        api.snapshot = snapshot
        with server.transport():
            result = evaluate_and_enqueue(api, context, server.policy, intent)
        self.assertEqual(result[0], "blocked")
        self.assertEqual(api.mutation_attempts, 0)

    def test_four_workflows_six_checks_share_exactly_one_eight_blob_read(self):
        """All current declaration shapes share one correctly bound blob query."""
        server = PathServer()
        for workflow, name, exact, prefix in (
            ("decision-sidecar.yml", "decision-sidecar / Python 3.14 (ubuntu-latest)", [], ["Build/decision/"]),
            ("decision-sidecar.yml", "decision-image / Build Container", [], ["Build/decision/"]),
            ("reranker-sidecar.yml", "reranker-sidecar / Python 3.14 (ubuntu-latest)", [], ["Build/reranker/"]),
            ("reranker-sidecar.yml", "reranker-image / Build Container", [], ["Build/reranker/"]),
            ("roadmap-check.yml", "roadmap-check / check", ["ROADMAP.md"], [])):
            server.policy["conditional_checks"].append({"name": name, "app_id": 15368,
                "workflow": workflow, "events": ["pull_request"], "workflow_blob_oid": BLOB,
                "paths": {"exact": exact, "prefix": prefix}})
        server.blobs = {f"{prefix}{index}": {"__typename": "Blob", "oid": BLOB}
                        for prefix in ("b", "h") for index in range(4)}
        result, api, snapshot = decision(server)
        self.assertEqual(result[0], "ready")
        self.assertEqual(api.api_calls, 20)
        self.assertEqual(len(server.blob_requests), 1)
        expected = {f"{prefix}{index}": revision + ":.github/workflows/" + workflow
                    for index, workflow in enumerate(sorted({entry["workflow"] for entry in server.policy["conditional_checks"]}))
                    for prefix, revision in (("b", BASE), ("h", SHA))}
        self.assertEqual({key: value for key, value in server.blob_requests[0]["variables"].items()
                          if key in expected}, expected)
        self.assertEqual(len(snapshot["applicability"]["inapplicable_checks"]), 6)

    def test_wrong_conditional_provenance_and_closing_graphql_errors_block(self):
        """Wrong producers and partial provider errors cannot qualify readiness."""
        for mutate in (lambda s: s.checks[-1]["app"].update(id=12),
                       lambda s: s.runs[200].update(event="workflow_dispatch"),
                       lambda s: s.runs[200].update(path=".github/workflows/other.yml")):
            server = PathServer([DOC_PATH])
            server.producer()
            mutate(server)
            self.assertIn(decision(server)[0][0], {"waiting", "blocked"})
        server = PathServer()
        server.closing_errors = [{"message": "partial blob result"}]
        api = server.client()
        with server.transport(), self.assertRaises(ApiError):
            api.snapshot(42)

    def test_v2_receipt_binds_actual_paths_and_api_cost_without_mutations(self):
        """Receipts expose the actual proof digest, anchors and request counts."""
        server = PathServer(diverged=True)
        _, api, snapshot = decision(server)
        api.last_snapshot = snapshot
        context, _ = fixtures()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
                "RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary")}), \
                mock.patch("runner.output"):
            write_receipt(api, context, server.policy, "evaluate", None, "observed-ready", "fixture")
            receipt = json.loads((Path(directory) / "dependency-controller-receipt.json").read_text())
        self.assertEqual(receipt["applicability_digest"], digest(snapshot["applicability"]))
        self.assertEqual(receipt["api_calls"], 21)
        self.assertEqual(receipt["mutation_attempts"], 0)
        self.assertEqual(receipt["applicability"]["anchor"]["pr_base_oid"], ASSOCIATED)

    def test_client_setup_error_cannot_masquerade_as_snapshot_rejection(self):
        """A broken fixture constructor must error, not satisfy a snapshot oracle."""
        case = ApplicabilityTest("test_invalid_complete_file_and_commit_evidence_blocks")
        result = unittest.TestResult()
        with mock.patch.object(PathServer, "client", side_effect=ApiError("client setup failed")):
            case.run(result)
        self.assertTrue(result.errors, "Client setup failure was swallowed by an exception expectation")


if __name__ == "__main__":
    unittest.main()
