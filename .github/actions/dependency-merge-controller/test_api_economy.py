"""Exercise complete snapshots through the real HTTP adapter without network."""

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import unittest
from unittest import mock

from policy import decide, initial_intent
from runner import ApiError, GitHub, evaluate_and_enqueue, write_receipt
from test_policy import POLICY, REPO, SHA, TIME, fixtures


MERGE = "c" * 40
LATER = "2026-10-28T10:01:00Z"


def page(nodes, after=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": after is not None, "endCursor": after}}


def run_record(number, workflow, **changes):
    return {"id": number, "path": ".github/workflows/" + workflow,
            "head_sha": SHA, "head_branch": "renovate/phpunit", "event": "pull_request",
            "run_attempt": 1, "status": "completed", "conclusion": "success",
            "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
            "actor": {"login": "renovate[bot]"}, "created_at": TIME,
            "updated_at": TIME, **changes}


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}


class Server:
    def __init__(self):
        context, snapshot = fixtures()
        self.policy = copy.deepcopy(POLICY)
        names = ["ci.yml", "e2e.yml"] + [f"gate-{i}.yml" for i in range(6)]
        self.policy["required_checks"] += [
            {"name": f"Gate {i}", "app_id": 15368, "workflow": name, "events": ["pull_request"]}
            for i, name in enumerate(names[2:])]
        self.runs = {i: run_record(i, name) for i, name in enumerate(names, 20)}
        self.runs |= {90: run_record(90, "community.yml"), 91: run_record(91, "labeler.yml")}
        self.listed = list(self.runs.values())
        self.checks = [self.check(i, entry["name"]) for i, entry in
                       enumerate(self.policy["required_checks"], 20)]
        self.checks += [self.check(90, "Community"), self.check(91, "Labels")]
        self.raw = {"id": "PR_42", "number": 42, "state": "OPEN", "isDraft": False,
                    "headRefOid": SHA, "baseRefOid": "b" * 40, "baseRefName": "main",
                    "headRefName": "renovate/phpunit", "author": {"login": "renovate[bot]"},
                    "headRepository": {"nameWithOwner": REPO},
                    "baseRepository": {"nameWithOwner": REPO}, "mergeable": "MERGEABLE",
                    "reviewDecision": "APPROVED", "isInMergeQueue": False,
                    "potentialMergeCommit": {"oid": MERGE}, "labels": page([]),
                    "timelineItems": page([]), "reviewThreads": page([]),
                    "latestReviews": page([{"author": {"login": r["author"]}, "state": r["state"],
                                             "commit": {"oid": r["commit"]}}
                                            for r in snapshot["reviews"]])}
        self.rules = snapshot["rules"]
        self.connections = {}
        self.calls = []
        self.run_responses = {}
        self.list_responses = {}
        self.graphql_errors = []

    @staticmethod
    def check(number, name, **changes):
        return {"id": number, "name": name, "head_sha": SHA, "status": "completed",
                "conclusion": "success", "app": {"id": 15368, "slug": "github-actions"},
                "details_url": f"https://github.com/{REPO}/actions/runs/{number}/job/{number}",
                "started_at": TIME, "completed_at": TIME, **changes}

    def open(self, request, **_kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        resource = parsed.path.removeprefix(f"/repos/{REPO}/")
        self.calls.append((resource, query))
        if parsed.path == "/graphql":
            payload = json.loads(request.data)
            fields, variables = payload["query"], payload["variables"]
            if "headRefOid" in fields:
                value = copy.deepcopy(self.raw)
            else:
                name = next(name for name in ("timelineItems", "latestReviews", "reviewThreads")
                            if name + "(" in fields)
                after = variables.get("after")
                value = {name: copy.deepcopy(self.connections.get((name, after), self.raw.get(name)))}
            return Response({"data": {"repository": {"pullRequest": value}}, "errors": self.graphql_errors})
        if resource == "rules/branches/main":
            return Response(self.rules)
        if resource == "actions/runs" or resource.startswith("actions/workflows/"):
            sha, current = query["head_sha"][0], int(query["page"][0])
            if (sha, current) in self.list_responses:
                body, headers = self.list_responses[(sha, current)]
                return Response(body, headers)
            values = self.listed if sha == SHA else []
            if resource.startswith("actions/workflows/"):
                values = [r for r in values if r["path"].split("/")[-1] == resource.split("/")[2]]
            return Response({"total_count": len(values), "workflow_runs": values})
        if resource.startswith("actions/runs/"):
            number = int(resource.split("/")[-1])
            return Response(self.run_responses.get(number, self.runs[number]))
        if resource.endswith("/statuses"):
            return Response([])
        if resource.endswith("/check-runs"):
            return Response({"check_runs": self.checks if SHA in resource else []})
        raise AssertionError("Unexpected fixture request: " + resource)

    def client(self):
        return GitHub(REPO, "synthetic-token", policy=self.policy, observe_only=True)

    def transport(self):
        return mock.patch("runner.urllib.request.build_opener", return_value=self)


class EconomyTest(unittest.TestCase):
    def test_complete_ready_snapshot_uses_eight_real_adapter_requests(self):
        server = Server()
        api = server.client()
        with server.transport():
            snapshot = api.snapshot(42)
        context, initial = fixtures()
        intent = initial_intent(context, initial, server.policy)
        self.assertEqual(decide(context, snapshot, server.policy, intent)[0], "ready")
        self.assertEqual(len(snapshot["checks"]), 10)
        self.assertEqual(len(snapshot["workflows"]), 8)
        self.assertEqual(api.api_calls, 8)
        self.assertEqual(api.mutation_attempts, 0)

    def test_receipt_accounts_for_actual_snapshot_requests(self):
        server = Server()
        api = server.client()
        with server.transport():
            api.last_snapshot = api.snapshot(42)
        context, initial = fixtures()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
                "RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary")}), \
                mock.patch("runner.output"):
            write_receipt(api, context, server.policy, "evaluate", None, "blocked", "no native intent")
            receipt = json.loads((Path(directory) / "dependency-controller-receipt.json").read_text())
        self.assertEqual(receipt["api_calls"], 8)
        self.assertEqual(receipt["mutation_attempts"], 0)

    def test_missing_unlisted_producer_uses_one_single_run_fallback(self):
        server = Server()
        server.listed = [r for r in server.listed if r["id"] != 91]
        api = server.client()
        with server.transport():
            snapshot = api.snapshot(42)
        self.assertEqual(next(c for c in snapshot["checks"] if c["id"] == 91)["workflow"], "labeler.yml")
        self.assertEqual(api.api_calls, 9)

    def test_conflicting_duplicate_run_metadata_blocks(self):
        server = Server()
        server.listed.append(server.listed[0] | {"run_attempt": 2})
        with server.transport(), self.assertRaises(ApiError):
            server.client().snapshot(42)

    def test_unreferenced_dynamic_copilot_run_is_not_a_trusted_producer(self):
        server = Server()
        # Actual nr-vault inventory includes this legitimate dynamic path/event.
        server.listed.append(run_record(40634492828, "unused.yml",
            path="dynamic/agents/copilot-pull-request-reviewer", event="dynamic", conclusion="failure"))
        api = server.client()
        with server.transport():
            snapshot = api.snapshot(42)
        context, initial = fixtures()
        intent = initial_intent(context, initial, server.policy)
        self.assertEqual(decide(context, snapshot, server.policy, intent)[0], "ready")
        self.assertNotIn(40634492828, api.runs)
        self.assertEqual(api.api_calls, 8)

    def test_referenced_dynamic_producer_keeps_strict_single_run_validation(self):
        server = Server()
        server.runs[90].update(path="dynamic/agents/copilot-pull-request-reviewer", event="dynamic")
        with server.transport(), self.assertRaises(ApiError):
            server.client().snapshot(42)

    def test_dynamic_paths_do_not_exempt_generic_inventory_validation(self):
        for changes in ({"id": 20}, {"head_sha": MERGE}, {"run_attempt": None},
                        {"repository": {"full_name": "attacker/fork"}}):
            server = Server()
            server.listed.append(run_record(40634492828, "unused.yml",
                path="dynamic/agents/copilot-pull-request-reviewer", event="dynamic", **changes))
            with self.subTest(changes=changes), server.transport(), self.assertRaises(ApiError):
                server.client().snapshot(42)

    def test_newer_queued_malformed_required_path_cannot_disappear(self):
        server = Server()
        server.listed.append(run_record(200, "ci.yml", path=".github/workflows/ci.yml@",
                                         status="queued", conclusion=None, updated_at=LATER))
        with server.transport(), self.assertRaises(ApiError):
            server.client().snapshot(42)

    def test_only_sane_dynamic_namespace_with_dynamic_event_is_inventory_only(self):
        for path, event in (("dynamic/agents/copilot-pull-request-reviewer", "pull_request"),
                            ("dynamic/../ci.yml", "dynamic"), ("dynamic/agents/../../ci", "dynamic"),
                            ("dynamic/agents/reviewer\n", "dynamic"),
                            ("dynamic/agents/reviewer%2F..", "dynamic"),
                            ("foreign/.github/workflows/ci.yml", "dynamic")):
            server = Server()
            server.listed.append(run_record(40634492828, "unused.yml", path=path, event=event))
            with self.subTest(path=path, event=event), server.transport(), self.assertRaises(ApiError):
                server.client().snapshot(42)

    def test_missing_or_capped_run_collection_cannot_prove_completeness(self):
        for body in ({"workflow_runs": []}, {"total_count": 1000, "workflow_runs": []},
                     {"total_count": 1001, "workflow_runs": []},
                     {"total_count": True, "workflow_runs": []},
                     {"total_count": 4, "workflow_runs": []}):
            server = Server()
            server.list_responses[(SHA, 1)] = body, {}
            with self.subTest(body=body), server.transport(), self.assertRaises(ApiError):
                server.client().snapshot(42)

    def test_run_pages_are_complete_and_conflicts_across_pages_block(self):
        for second in ("complete", "conflict", "changing-count", "next-at-cap"):
            server = Server()
            rows = server.listed
            server.list_responses[(SHA, 1)] = (
                {"total_count": len(rows), "workflow_runs": rows[:5]}, {"Link": '<next>; rel="next"'})
            tail = rows[5:]
            count = len(rows)
            headers = {}
            if second == "conflict":
                tail = tail[:-1] + [rows[0] | {"run_attempt": 2}]
            elif second == "changing-count":
                count += 1
            elif second == "next-at-cap":
                headers = {"Link": '<next>; rel="next"'}
                for number in range(3, 11):
                    server.list_responses[(SHA, number)] = (
                        {"total_count": len(rows), "workflow_runs": []}, headers)
            server.list_responses[(SHA, 2)] = {"total_count": count, "workflow_runs": tail}, headers
            with self.subTest(second=second), server.transport():
                if second == "complete":
                    api = server.client()
                    self.assertEqual(len(api.snapshot(42)["workflows"]), 8)
                    self.assertEqual(api.api_calls, 9)
                else:
                    with self.assertRaises(ApiError):
                        server.client().snapshot(42)

    def test_malformed_list_metadata_cannot_become_a_cache_hit(self):
        for changes in ({"id": True}, {"head_sha": MERGE}, {"run_attempt": None},
                        {"run_attempt": True}, {"updated_at": "yesterday"},
                        {"repository": {"full_name": "attacker/fork"}},
                        {"path": None}, {"event": []}):
            server = Server()
            server.listed[0] = server.listed[0] | changes
            with self.subTest(changes=changes), server.transport(), self.assertRaises(ApiError):
                server.client().snapshot(42)

    def test_wrong_fallback_identity_or_failed_lookup_blocks(self):
        for changes in ({"id": 900}, {"head_sha": "d" * 40}, {"run_attempt": 0},
                        {"repository": {"full_name": "attacker/fork"}}):
            server = Server()
            server.listed = [r for r in server.listed if r["id"] != 91]
            server.run_responses[91] = server.runs[91] | changes
            with self.subTest(changes=changes), server.transport(), self.assertRaises(ApiError):
                server.client().snapshot(42)

    def test_each_connection_keeps_its_own_seed_cursor(self):
        server = Server()
        server.raw["timelineItems"] = page([], "history-1")
        server.raw["latestReviews"] = page(server.raw["latestReviews"]["nodes"], "reviews-1")
        server.connections[("timelineItems", "history-1")] = page([])
        server.connections[("latestReviews", "reviews-1")] = page([], "reviews-2")
        server.connections[("latestReviews", "reviews-2")] = page([])
        api = server.client()
        with server.transport():
            result = api.snapshot(42)
        self.assertEqual(len(result["reviews"]), 2)
        self.assertEqual(result["history"], [])
        self.assertEqual(api.api_calls, 11)

    def test_missing_seed_connection_and_repeated_cursor_block(self):
        for broken in ("missing", "repeat"):
            server = Server()
            if broken == "missing":
                del server.raw["reviewThreads"]
            else:
                server.raw["reviewThreads"] = page([], "same")
                server.connections[("reviewThreads", "same")] = page([], "same")
            with self.subTest(broken=broken), server.transport(), self.assertRaises(ApiError):
                server.client().snapshot(42)

    def test_malformed_connections_and_partial_graphql_errors_block(self):
        for broken in (None, {}, {"nodes": [], "pageInfo": {}},
                       {"nodes": None, "pageInfo": {"hasNextPage": False}},
                       {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}):
            server = Server()
            server.raw["reviewThreads"] = broken
            with self.subTest(broken=broken), server.transport(), self.assertRaises(ApiError):
                server.client().snapshot(42)
        server = Server()
        server.graphql_errors = [{"message": "partial result"}]
        with server.transport(), self.assertRaises(ApiError):
            server.client().snapshot(42)

    def test_single_sha_deduplicates_all_inventory_reads_and_labels_still_block(self):
        for potential in (None, {"oid": SHA}):
            server = Server()
            server.raw["potentialMergeCommit"] = potential
            server.raw["labels"]["pageInfo"]["hasNextPage"] = True
            api = server.client()
            with self.subTest(potential=potential), server.transport():
                snapshot = api.snapshot(42)
            context, initial = fixtures()
            intent = initial_intent(context, initial, server.policy)
            self.assertEqual(decide(context, snapshot, server.policy, intent)[0], "blocked")
            self.assertEqual(api.api_calls, 5)

    def test_ten_page_cap_includes_combined_seed(self):
        for last in (10, 11):
            server = Server()
            server.raw["reviewThreads"] = page([], "1")
            for current in range(1, last):
                server.connections[("reviewThreads", str(current))] = page(
                    [], str(current + 1) if current + 1 < last else None)
            api = server.client()
            with self.subTest(last=last), server.transport():
                if last == 10:
                    self.assertEqual(api.snapshot(42)["threads"], [])
                    self.assertEqual(api.api_calls, 17)
                else:
                    with self.assertRaises(ApiError):
                        api.snapshot(42)

    def test_new_check_timestamp_refreshes_cached_run_once(self):
        server = Server()
        server.checks[0]["completed_at"] = LATER
        server.run_responses[20] = server.runs[20] | {"updated_at": LATER, "run_attempt": 2}
        api = server.client()
        with server.transport():
            snapshot = api.snapshot(42)
        self.assertEqual(snapshot["checks"][0]["attempt"], 2)
        self.assertEqual(api.api_calls, 9)

    def test_newer_check_with_incoherent_single_run_blocks(self):
        server = Server()
        server.checks[0]["completed_at"] = LATER
        with server.transport(), self.assertRaises(ApiError):
            server.client().snapshot(42)

    def test_terminal_skipped_check_uses_completion_not_synthetic_start_time(self):
        # Actual GitHub PR481 legacy merger: start04:49:47, complete04:49:41,
        # workflow updated04:49:41. A skipped check's start is not retry evidence.
        server = Server()
        server.checks[-1].update(started_at=LATER, completed_at=TIME, conclusion="skipped")
        api = server.client()
        with server.transport():
            snapshot = api.snapshot(42)
        self.assertEqual(snapshot["checks"][-1]["conclusion"], "skipped")
        self.assertEqual(api.api_calls, 8)

    def test_missing_malformed_terminal_time_or_newer_attempt_blocks(self):
        for changes in ({"completed_at": None}, {"completed_at": ""},
                        {"completed_at": "tomorrow"}, {"completed_at": "2026-10-28T10:00:00"},
                        {"run_attempt": 2}, {"run_attempt": True}):
            server = Server()
            server.checks[0].update(changes)
            with self.subTest(changes=changes), server.transport(), self.assertRaises(ApiError):
                server.client().snapshot(42)

    def test_active_check_start_and_terminal_failure_are_retained(self):
        for changes, outcome in (({"status": "in_progress", "started_at": LATER,
                                   "completed_at": None, "conclusion": None}, "waiting"),
                                 ({"completed_at": LATER, "conclusion": "failure"}, "blocked")):
            server = Server()
            server.checks[0].update(changes)
            server.run_responses[20] = server.runs[20] | {"updated_at": LATER}
            context, initial = fixtures()
            intent = initial_intent(context, initial, server.policy)
            with self.subTest(changes=changes), server.transport():
                snapshot = server.client().snapshot(42)
            self.assertEqual(decide(context, snapshot, server.policy, intent)[0], outcome)

    def test_attempt_refresh_updates_workflow_readiness_too(self):
        server = Server()
        server.checks[0]["run_attempt"] = 2
        server.run_responses[20] = server.runs[20] | {
            "updated_at": LATER, "run_attempt": 2, "status": "in_progress", "conclusion": None}
        context, initial = fixtures()
        intent = initial_intent(context, initial, server.policy)
        with server.transport():
            snapshot = server.client().snapshot(42)
        self.assertEqual(decide(context, snapshot, server.policy, intent)[0], "waiting")
        self.assertEqual(snapshot["workflows"][0]["status"], "in_progress")

    def test_additional_pending_and_failed_checks_still_block(self):
        for changes, outcome in (({"status": "in_progress", "conclusion": None}, "waiting"),
                                 ({"conclusion": "failure"}, "blocked")):
            server = Server()
            server.checks[-1].update(changes)
            context, initial = fixtures()
            intent = initial_intent(context, initial, server.policy)
            with self.subTest(changes=changes), server.transport():
                snapshot = server.client().snapshot(42)
            self.assertEqual(decide(context, snapshot, server.policy, intent)[0], outcome)

    def test_old_green_check_cannot_hide_new_pending_retry(self):
        server = Server()
        server.runs[20].update(run_attempt=2, status="in_progress", conclusion=None, updated_at=LATER)
        context, initial = fixtures()
        intent = initial_intent(context, initial, server.policy)
        with server.transport():
            snapshot = server.client().snapshot(42)
        self.assertEqual(decide(context, snapshot, server.policy, intent)[0], "waiting")

    def test_second_full_snapshot_resets_cache_and_sees_retry(self):
        server = Server()
        api = server.client()
        context, initial = fixtures()
        intent = initial_intent(context, initial, server.policy)
        original = api.snapshot
        reads = []

        def snapshot(number):
            if reads:
                server.runs[20].update(run_attempt=2, status="in_progress", conclusion=None, updated_at=LATER)
            reads.append(number)
            return original(number)

        api.snapshot = snapshot
        with server.transport():
            result = evaluate_and_enqueue(api, context, server.policy, intent)
        self.assertEqual(result[0], "waiting")
        self.assertEqual(reads, [42, 42])
        self.assertEqual(api.api_calls, 16)
        self.assertEqual(api.mutation_attempts, 0)

    def test_only_sha_bound_repository_run_listing_is_allowed(self):
        api = Server().client()
        root = f"/repos/{REPO}"
        self.assertTrue(api.api_url(root + "/actions/runs?head_sha=" + SHA).endswith(SHA))
        for suffix in ("/actions/runs", "/actions/runs?head_sha=" + SHA + "&head_sha=" + SHA,
                       "/actions/runs?head_sha=" + SHA + "&status=completed",
                       "/actions/workflows/ci.yml/runs?head_sha=" + SHA):
            with self.subTest(suffix=suffix), self.assertRaises(ApiError):
                api.api_url(root + suffix)


if __name__ == "__main__":
    unittest.main()
