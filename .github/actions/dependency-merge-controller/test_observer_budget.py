"""A configured observer ceiling bounds real adapter attempts without partial readiness."""

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.parse
import zipfile
from unittest import mock

import runner
from policy import Blocked, decide, initial_intent, validate_policy
from test_api_economy import Response, run_record
from test_applicability import PathServer
from test_observer_readiness import CompletionServer
from test_policy import POLICY, REPO, SHA, TIME, fixtures
from test_rate_limits import ByteResponse, receipt


LIMIT = "observer_api_attempt_limit"
BUDGET = "api_attempt_budget"
READ_PATH = f"/repos/{REPO}/actions/runs/9"
INTENT = ".github/workflows/dependency-budget-intent.yml"
EVALUATOR = ".github/workflows/dependency-budget-evaluate.yml"


def configured_policy(limit):
    """Keep the complete existing v2 duties and add only the trusted ceiling."""
    policy = PathServer().policy
    policy[LIMIT] = limit
    return policy


def origin(policy):
    """Create canonical synthetic intent data without charging another phase."""
    context, snapshot = fixtures()
    context.update(caller_path=INTENT, intent_path=INTENT, observe_only=True)
    return initial_intent(context, snapshot, policy)


class BudgetServer(CompletionServer):
    """Use actual context/artifact/snapshot paths with network-free responses."""

    def __init__(self, limit, present_intent=False):
        """Supply a real verified archive route or an explicitly absent intent."""
        super().__init__()
        self.policy[LIMIT] = limit
        self.artifacts = []
        self.archive = b""
        if present_intent:
            document = origin(self.policy)
            name = f"merge-intent-pr-42-{SHA}"
            self.artifacts = [{"id": 12, "name": name, "expired": False, "workflow_run": {"id": 9},
                               "created_at": TIME, "expires_at": "2099-01-01T00:00:00Z"}]
            self.runs[9] = run_record(9, INTENT.split("/")[-1], event="pull_request_target")
            with io.BytesIO() as stream:
                with zipfile.ZipFile(stream, "w") as archive:
                    archive.writestr("intent.json", json.dumps(document))
                self.archive = stream.getvalue()

    def open(self, request, **options):
        """Retain complete real adapter behavior around the bounded artifact reads."""
        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.path == f"/repos/{REPO}/actions/artifacts":
            self.calls.append((parsed.path, urllib.parse.parse_qs(parsed.query)))
            return Response({"total_count": len(self.artifacts), "artifacts": self.artifacts})
        if parsed.path == f"/repos/{REPO}/actions/artifacts/12/zip":
            self.calls.append((parsed.path, {}))
            return ByteResponse({}, self.archive)
        return super().open(request, **options)


def run_phase(api, phase, event_name, event, replacement_intent=None):
    """Drive the actual phase wrapper and inspect receipt/output eligibility."""
    with tempfile.TemporaryDirectory() as directory:
        event_path = Path(directory) / "event.json"
        event_path.write_text(json.dumps(event))
        environment = {"CONTROLLER_POLICY": json.dumps(api.policy), "GITHUB_REPOSITORY": REPO,
                       "READ_TOKEN": "read-stub", "OBSERVE_ONLY": "true", "GITHUB_EVENT_PATH": str(event_path),
                       "GITHUB_EVENT_NAME": event_name, "GITHUB_ACTOR": "renovate[bot]", "GITHUB_RUN_ID": "10",
                       "CALLER_WORKFLOW": INTENT if phase == "prepare" else EVALUATOR, "INTENT_WORKFLOW": INTENT,
                       "RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary")}
        emitted = []
        with mock.patch.dict(os.environ, environment), mock.patch("runner.GitHub", return_value=api), \
                mock.patch("runner.output", side_effect=lambda **values: emitted.append(values)), mock.patch("builtins.print"):
            if replacement_intent is None:
                runner.main(phase)
            else:
                with mock.patch.object(api, "intent", return_value=replacement_intent):
                    runner.main(phase)
        document = json.loads((Path(directory) / "dependency-controller-receipt.json").read_text())
    return document, emitted


class ObserverBudgetTest(unittest.TestCase):
    """Refute off-by-one, resets, hidden requests and incomplete-ready outcomes."""

    def test_policy_and_constructor_reject_invalid_limits_before_requests(self):
        """Only actual JSON integers1..64 can configure a v2 observer."""
        invalid = (None, False, True, 0, -1, 65, 2**80, "64", 1.0, 64.0, [], {})
        for value in invalid:
            policy = configured_policy(value)
            with self.subTest(value=value), mock.patch("runner.urllib.request.build_opener") as opener:
                with self.assertRaisesRegex(Blocked, LIMIT):
                    validate_policy(policy)
                with self.assertRaisesRegex(Blocked, LIMIT):
                    runner.GitHub(REPO, "read", policy=policy, observe_only=True)
                opener.assert_not_called()
        policy = copy.deepcopy(POLICY)
        policy[LIMIT] = 1
        with self.assertRaisesRegex(Blocked, LIMIT):
            validate_policy(policy)
        with self.assertRaisesRegex(Blocked, LIMIT):
            runner.GitHub(REPO, "read", policy=policy, observe_only=True)

    def test_valid_limits_work_in_both_phases_but_enforcement_is_configuration_error(self):
        """The opt-in ceiling cannot enter a production client or request."""
        for value in (1, 64):
            for phase in ("prepare", "evaluate"):
                policy = configured_policy(value)
                validate_policy(policy)
                api = runner.GitHub(REPO, "read", policy=policy, observe_only=True, phase=phase)
                self.assertEqual(receipt(api).get(BUDGET), {"limit": value, "used": 0, "exhausted": False})
                with self.subTest(value=value, phase=phase), mock.patch("runner.urllib.request.build_opener") as opener:
                    with self.assertRaisesRegex(runner.ApiError, "observation"):
                        runner.GitHub(REPO, "read", policy=policy, observe_only=False, phase=phase)
                    opener.assert_not_called()

    def test_exact64_mixed_requests_deny65_before_request_or_opener_construction(self):
        """Use the existing counter for both REST and GraphQL, never query points."""
        api = runner.GitHub(REPO, "read", policy=configured_policy(64), observe_only=True)
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **options: ByteResponse({"X-RateLimit-Resource": "core"})
        with mock.patch("runner.urllib.request.build_opener", return_value=opener) as factory, \
                mock.patch("runner.urllib.request.Request", wraps=urllib.request.Request) as requests:
            for index in range(64):
                if index % 2:
                    api.request("/graphql", "POST", {"query": "query{viewer{login}}", "variables": {}})
                else:
                    api.request(READ_PATH)
            self.assertEqual(receipt(api).get(BUDGET), {"limit": 64, "used": 64, "exhausted": False})
            with self.assertRaisesRegex(runner.ApiError, "budget exhausted"):
                api.request(READ_PATH)
            self.assertEqual((factory.call_count, opener.open.call_count, requests.call_count), (64, 64, 64))
        self.assertEqual(api.api_calls, 64)
        self.assertEqual(receipt(api).get(BUDGET), {"limit": 64, "used": 64, "exhausted": True})
        self.assertEqual(receipt(api)["api_rate_limits"][0]["api_call"], 64)

    def test_limit1_is_independently_enforced_and_invalid_operations_keep_precedence(self):
        """A spent ceiling never authorizes a mutation or foreign API target."""
        api = runner.GitHub(REPO, "read", policy=configured_policy(1), observe_only=True)
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **options: ByteResponse({})
        with mock.patch("runner.urllib.request.build_opener", return_value=opener):
            api.request(READ_PATH)
            with self.assertRaisesRegex(runner.ApiError, "Observation forbids"):
                api.enqueue("PR_42", SHA)
            with self.assertRaises(runner.ApiError):
                api.request("/repos/attacker/fork/actions/runs/9")
            self.assertEqual(receipt(api).get(BUDGET), {"limit": 1, "used": 1, "exhausted": False})
            with self.assertRaisesRegex(runner.ApiError, "budget exhausted"):
                api.request(READ_PATH)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(api.mutation_attempts, 1)

    def test_http_and_network_errors_spend_attempts_without_retry_or_counter_reset(self):
        """Attempt accounting is independent of receiving a successful body."""
        errors = [urllib.error.HTTPError("https://api.github.com" + READ_PATH, 403, "fixture",
                    {"x-ratelimit-resource": "core", "x-ratelimit-remaining": "0"}, None), urllib.error.URLError("offline")]
        for error in errors:
            api = runner.GitHub(REPO, "read", policy=configured_policy(1), observe_only=True)
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.subTest(error=type(error).__name__), mock.patch("runner.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(runner.ApiError):
                    api.request(READ_PATH)
                self.assertEqual(api.api_calls, 1)
                with self.assertRaisesRegex(runner.ApiError, "budget exhausted"):
                    api.request(READ_PATH)
                self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(receipt(api).get(BUDGET), {"limit": 1, "used": 1, "exhausted": True})
        api = runner.GitHub(REPO, "read", policy=configured_policy(1), observe_only=True)
        opener = mock.Mock()
        opener.open.return_value = ByteResponse({}, b"invalid JSON")
        with mock.patch("runner.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(json.JSONDecodeError):
                api.request(READ_PATH)
            with self.assertRaisesRegex(runner.ApiError, "budget exhausted"):
                api.request(READ_PATH)
        self.assertEqual((api.api_calls, opener.open.call_count), (1, 1))

    def test_original302_counts_once_and_storage_keeps_its_existing_exclusion(self):
        """A blob follow-up is disclosed separately and never receives the token."""
        api = runner.GitHub(REPO, "read", policy=configured_policy(1), observe_only=True)
        path = f"/repos/{REPO}/actions/artifacts/12/zip"
        error = urllib.error.HTTPError("https://api.github.com" + path, 302, "redirect",
                    {"Location": "https://storage.example/fixture", "x-ratelimit-resource": "core"}, None)
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch("runner.urllib.request.build_opener", return_value=opener), \
                mock.patch("runner.urllib.request.urlopen", return_value=ByteResponse({}, b"zip")) as storage:
            self.assertEqual(api.request(path, binary=True)[0], b"zip")
            with self.assertRaisesRegex(runner.ApiError, "budget exhausted"):
                api.request(path, binary=True)
        self.assertEqual((opener.open.call_count, storage.call_count, api.api_calls), (1, 1, 1))
        self.assertIsNone(storage.call_args.args[0].get_header("Authorization"))

    def test_late_closing_exhaustion_blocks_despite_an_earlier_ready_snapshot(self):
        """A partial second snapshot cannot inherit the first readiness result."""
        server = BudgetServer(39)
        api = server.client()
        event = {"action": "submitted", "pull_request": {"number": 42, "head": {"sha": SHA}}}
        with server.transport():
            document, emitted = run_phase(api, "evaluate", "pull_request_review", event, origin(server.policy))
        self.assertEqual(document["decision"], "blocked")
        self.assertIn("budget exhausted", document["reason"])
        self.assertEqual(document.get(BUDGET), {"limit": 39, "used": 39, "exhausted": True})
        self.assertEqual(len(server.calls), 39)
        self.assertEqual(len(server.blob_requests), 1)
        self.assertEqual(document["mutation_attempts"], 0)
        self.assertEqual(document["api_rate_limits"][0]["api_call"], 39)
        self.assertFalse(any(item.get("state", "").startswith("observed-ready") for item in emitted))

    def test_exhausted_prepare_emits_no_successful_intent_eligibility(self):
        """A failed preparation cannot enable the existing intent uploader."""
        server = BudgetServer(1)
        api = server.client()
        api.phase = "prepare"
        event = {"action": "opened", "pull_request": {"number": 42, "head": {"sha": SHA}}}
        with server.transport():
            document, emitted = run_phase(api, "prepare", "pull_request_target", event)
        self.assertEqual(document["decision"], "blocked")
        self.assertEqual(document.get(BUDGET), {"limit": 1, "used": 1, "exhausted": True})
        self.assertFalse(any("intent_name" in item or "intent_path" in item for item in emitted))
        self.assertEqual(len(server.calls), 1)

    def test_context_and_intent_exhaustion_keep_truthful_bounded_receipts(self):
        """Both early recovery paths use the same existing phase instance counter."""
        events = [("workflow_run", {"action": "completed", "workflow_run": {"id": 400}}),
                  ("pull_request_review", {"action": "submitted", "pull_request": {"number": 42, "head": {"sha": SHA}}})]
        for event_name, event in events:
            server = BudgetServer(1, present_intent=True)
            api = server.client()
            with self.subTest(event=event_name), server.transport():
                document, _ = run_phase(api, "evaluate", event_name, event)
            self.assertEqual(document["decision"], "blocked")
            self.assertEqual(document.get(BUDGET), {"limit": 1, "used": 1, "exhausted": True})
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(document["api_rate_limits"][0]["api_call"], 1)

    def test_context_verified_archive_and_snapshots_share_one_counter(self):
        """A real adapter handoff spends its five setup reads plus both snapshots."""
        server = BudgetServer(64, present_intent=True)
        api = server.client()
        event = {"action": "completed", "workflow_run": {"id": 400}}
        with server.transport():
            document, _ = run_phase(api, "evaluate", "workflow_run", event)
        self.assertEqual(document["decision"], "observed-ready")
        self.assertEqual(document.get(BUDGET), {"limit": 64, "used": 45, "exhausted": False})
        self.assertEqual(len(server.calls), 45)
        self.assertEqual(document["origin_run_id"], 9)
        self.assertEqual(document["artifact_id"], 12)
        self.assertEqual(document["mutation_attempts"], 0)

    def test_missing_intent_is_blocked_without_falsely_reporting_exhaustion(self):
        """A budget never replaces current intent or makes absence admissible."""
        server = BudgetServer(64)
        api = server.client()
        event = {"action": "submitted", "pull_request": {"number": 42, "head": {"sha": SHA}}}
        with server.transport():
            document, _ = run_phase(api, "evaluate", "pull_request_review", event)
        self.assertEqual(document["decision"], "blocked")
        self.assertIn("No valid current-head intent", document["reason"])
        self.assertEqual(document.get(BUDGET), {"limit": 64, "used": 21, "exhausted": False})

    def test_success_at_exact_snapshot_limit_does_not_mean_a_denied_attempt(self):
        """Both full proofs can finish exactly at the ceiling with exhausted false."""
        for diverged, limit in ((False, 40), (True, 42)):
            server = PathServer(diverged=diverged)
            server.policy[LIMIT] = limit
            server.raw["isInMergeQueue"] = True
            api = server.client()
            context, _ = fixtures()
            context.update(observe_only=True, caller_path=EVALUATOR, intent_path=INTENT)
            with self.subTest(diverged=diverged), server.transport():
                state, _ = runner.evaluate_and_enqueue(api, context, server.policy, origin(server.policy))
            self.assertEqual(state, "observed-ready-existing")
            self.assertEqual(receipt(api).get(BUDGET), {"limit": limit, "used": limit, "exhausted": False})
            self.assertEqual(api.mutation_attempts, 0)

    def test_limit_change_or_removal_invalidates_the_existing_full_policy_digest(self):
        """Configured intent cannot silently authorize a differently bounded caller."""
        server = BudgetServer(64)
        api = server.client()
        with server.transport():
            snapshot = api.snapshot(42)
        document = origin(server.policy)
        context, _ = fixtures()
        context.update(observe_only=True, caller_path=EVALUATOR, intent_path=INTENT)
        for replacement in (None, 63):
            policy = copy.deepcopy(server.policy)
            if replacement is None:
                del policy[LIMIT]
            else:
                policy[LIMIT] = replacement
            with self.subTest(replacement=replacement):
                state, reason = decide(context, snapshot, policy, document)
                self.assertEqual(state, "blocked")
                self.assertIn("No valid current-head intent", reason)

    def test_absent_key_keeps_v1_observer_and_v2_enforcer_receipts_unchanged(self):
        """The optional interface introduces no default receipt field or request."""
        for policy, observe in ((POLICY, True), (PathServer().policy, False)):
            api = runner.GitHub(REPO, "read", policy=policy, observe_only=observe)
            opener = mock.Mock()
            opener.open.side_effect = lambda *args, **options: ByteResponse({})
            with mock.patch("runner.urllib.request.build_opener", return_value=opener):
                for _ in range(65):
                    api.request(READ_PATH)
            self.assertEqual(api.api_calls, 65)
            self.assertNotIn(BUDGET, receipt(api))
            self.assertEqual(opener.open.call_count, 65)


if __name__ == "__main__":
    unittest.main()
