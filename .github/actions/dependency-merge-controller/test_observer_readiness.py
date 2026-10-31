"""Trusted completion hints and two full queued-observer readiness predicates."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest import mock

import runner
from policy import Blocked, decide, declared_checks, digest, initial_intent, validate_policy
from test_api_economy import Response, run_record
from test_applicability import PathServer
from test_phases import StubAPI
from test_policy import POLICY, REPO, SHA, fixtures


WAKES = "completion_wakeups"
LEGACY = "auto-merge-deps.yml"
TARGET_EVENT = "pull_request_target"
INTENT = ".github/workflows/dependency-merge-observe-intent.yml"
EVALUATOR = ".github/workflows/dependency-merge-observe-evaluate.yml"
WAKE = {"workflow": LEGACY, "events": [TARGET_EVENT]}
READY_EXISTING = "observed-ready-existing"


class CompletionServer(PathServer):
    """Extend the real adapter transport only with a current-head PR lookup."""

    def __init__(self):
        """Describe the existing legacy bot completion as an explicit v2 hint."""
        super().__init__()
        self.policy[WAKES] = [copy.deepcopy(WAKE)]
        self.runs[400] = run_record(400, LEGACY, event=TARGET_EVENT)
        self.candidates = [{"number": 42, "head": {"sha": SHA, "repo": {"full_name": REPO}}}]

    def open(self, request, **kwargs):
        """Retain server-owned run reads and resolve exactly one live PR."""
        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.path == f"/repos/{REPO}/pulls":
            self.calls.append((parsed.path, urllib.parse.parse_qs(parsed.query)))
            return Response(self.candidates)
        return super().open(request, **kwargs)


def completion_context(server, **event_changes):
    """Resolve the genuine workflow identity through the actual HTTP adapter."""
    api = server.client()
    environment = {"GITHUB_EVENT_NAME": "workflow_run", "GITHUB_ACTOR": "renovate[bot]",
                   "GITHUB_RUN_ID": "500", "CALLER_WORKFLOW": EVALUATOR, "INTENT_WORKFLOW": INTENT}
    event = {"action": "completed", "workflow_run": {"id": 400}, **event_changes}
    with mock.patch.dict(os.environ, environment), server.transport():
        context = runner.context_from_event(api, event, server.policy)
    return context, api


def cohort():
    """Collect complete immutable v2 evidence before creating separate intent."""
    server = PathServer()
    server.policy[WAKES] = [copy.deepcopy(WAKE)]
    api = server.client()
    context, _ = fixtures()
    context.update(caller_path=INTENT, intent_path=INTENT, observe_only=True)
    with server.transport():
        snapshot = api.snapshot(42)
    intent = initial_intent(context, snapshot, server.policy)
    context.update(event="workflow_run", run_id=500, caller_path=EVALUATOR)
    return context, snapshot, server.policy, intent


def negative_snapshots(snapshot):
    """Build independent complete-policy counterexamples for queued observation."""
    negatives = []
    for field, value in (("reviews", []), ("threads", [{"resolved": False}]),
                         ("history", [{"type": "RemovedFromMergeQueueEvent", "created_at": "2026-10-29T00:00:00Z"}])):
        changed = copy.deepcopy(snapshot)
        changed[field] = value
        negatives.append((field, changed, "blocked" if field == "history" else "waiting"))
    for conclusion, status, expected in (("failure", "completed", "blocked"),
                                         ("skipped", "completed", "blocked"), (None, "in_progress", "waiting")):
        changed = copy.deepcopy(snapshot)
        changed["checks"][0].update(conclusion=conclusion, status=status)
        negatives.append((str(conclusion), changed, expected))
    for conclusion, status, expected in ((None, "in_progress", "waiting"), ("failure", "completed", "blocked")):
        changed = copy.deepcopy(snapshot)
        changed["checks"].append({**changed["checks"][0], "id": 999, "name": "Legacy merge",
                                  "workflow": LEGACY, "status": status, "conclusion": conclusion})
        negatives.append(("legacy " + status, changed, expected))
    changed = copy.deepcopy(snapshot)
    changed["statuses"] = [{"id": 1, "context": "external", "state": "failure"}]
    negatives.append(("status", changed, "blocked"))
    changed = copy.deepcopy(snapshot)
    changed["workflows"] = []
    negatives.append(("missing workflows", changed, "waiting"))
    changed = copy.deepcopy(snapshot)
    changed["checks"][0]["job_current"] = False
    negatives.append(("wrong job revision", changed, "blocked"))
    changed = copy.deepcopy(snapshot)
    changed["complete"]["checks"] = False
    negatives.append(("incomplete inventory", changed, "blocked"))
    changed = copy.deepcopy(snapshot)
    changed["rules"][1]["parameters"]["required_status_checks"].append({"context": "unknown", "integration_id": 15368})
    negatives.append(("unknown native obligation", changed, "blocked"))
    return negatives


class CompletionWakeupTest(unittest.TestCase):
    """A completion hint never supplies a gate or an intent capability."""

    def test_explicit_legacy_success_completion_uses_existing_two_context_reads(self):
        """The new hint admits current server metadata without a new API search."""
        server = CompletionServer()
        context, api = completion_context(server)
        self.assertEqual((context["number"], context["event_head"]), (42, SHA))
        self.assertEqual(api.api_calls, 2)
        self.assertEqual(api.mutation_attempts, 0)

    def test_wake_is_never_promoted_to_a_declared_check(self):
        """Scheduling hints remain separate from all producer obligations."""
        server = PathServer()
        expected = copy.deepcopy(declared_checks(server.policy))
        server.policy[WAKES] = [copy.deepcopy(WAKE)]
        self.assertEqual(declared_checks(server.policy), expected)

    def test_omitted_empty_or_different_wake_cannot_authorize_legacy(self):
        """An exact declaration is necessary even when the display name matches."""
        for rows in (None, [], [{"workflow": "other.yml", "events": [TARGET_EVENT]}]):
            server = CompletionServer()
            if rows is None:
                del server.policy[WAKES]
            else:
                server.policy[WAKES] = rows
            with self.subTest(rows=rows), self.assertRaisesRegex(Blocked, "scope"):
                completion_context(server)

    def test_payload_names_do_not_override_the_server_workflow_identity(self):
        """Only the fetched run path may match the trusted completion hint."""
        server = CompletionServer()
        server.runs[400]["path"] = ".github/workflows/other.yml"
        with self.assertRaisesRegex(Blocked, "scope"):
            completion_context(server, workflow_run={"id": 400, "name": LEGACY,
                                                     "path": ".github/workflows/" + LEGACY})

    def test_wake_requires_exact_trusted_successful_bot_completion(self):
        """Foreign, stale, nonbot, active or negative completions cannot advance."""
        changes = [{"event": "pull_request"}, {"event": "workflow_dispatch"},
                   {"actor": {"login": "human"}}, {"actor": {"login": "renovate"}},
                   {"head_repository": {"full_name": "attacker/fork"}},
                   {"head_branch": "feature/example"}, {"head_sha": "c" * 40},
                   {"status": "in_progress", "conclusion": None},
                   *({"conclusion": conclusion} for conclusion in ("failure", "cancelled", "timed_out", None))]
        for changed in changes:
            server = CompletionServer()
            server.runs[400].update(changed)
            reason = "exactly one" if "head_sha" in changed else "scope"
            with self.subTest(changed=changed), self.assertRaisesRegex(Blocked, reason):
                completion_context(server)
        server = CompletionServer()
        server.runs[400]["repository"] = {"full_name": "attacker/fork"}
        with self.assertRaisesRegex(runner.ApiError, "identity or attempt"):
            completion_context(server)
        server = CompletionServer()
        with self.assertRaisesRegex(Blocked, "scope"):
            completion_context(server, action="requested")

    def test_missing_or_ambiguous_current_pr_cannot_consume_wake(self):
        """Branch identity must resolve to exactly one live same-head PR."""
        for candidates in ([], [{"number": 42, "head": {"sha": "c" * 40, "repo": {"full_name": REPO}}}],
                           CompletionServer().candidates * 2):
            server = CompletionServer()
            server.candidates = candidates
            with self.subTest(candidates=candidates), self.assertRaisesRegex(Blocked, "exactly one"):
                completion_context(server)

    def test_wake_schema_is_v2_only_exact_and_separate_from_check_obligations(self):
        """Malformed, redundant and pseudo-required declarations fail closed."""
        server = CompletionServer()
        for version in (1, 2):
            invalid = [None, {}, "legacy", [None], [{}], [WAKE, WAKE],
                       [{**WAKE, "name": "fake gate"}], [{**WAKE, "events": []}],
                       [{**WAKE, "events": ["pull_request"]}], [{**WAKE, "events": [TARGET_EVENT] * 2}],
                       [{**WAKE, "workflow": "../auto-merge-deps.yml"}],
                       [{**WAKE, "workflow": LEGACY + "@main"}],
                       [{"workflow": "ci.yml", "events": ["pull_request"]}]]
            if version == 1:
                invalid += [[], [WAKE]]
            for rows in invalid:
                policy = copy.deepcopy(server.policy if version == 2 else POLICY)
                policy[WAKES] = rows
                with self.subTest(version=version, rows=rows), self.assertRaises(Blocked):
                    validate_policy(policy)

    def test_invalid_schema_is_rejected_before_run_fetch_or_origin_matching(self):
        """A matching origin cannot hide an invalid extra wake declaration."""
        server = CompletionServer()
        server.policy[WAKES] = [{**WAKE, "unexpected": True}]
        server.runs[400]["path"] = INTENT
        api = server.client()
        with mock.patch.object(server, "client", return_value=api), \
                mock.patch.object(api, "run") as fetch, self.assertRaises(Blocked):
            completion_context(server)
        fetch.assert_not_called()

    def test_wake_cannot_duplicate_an_existing_exact_producer_event(self):
        """An existing completion capability needs no second weaker declaration."""
        server = CompletionServer()
        server.policy["required_checks"][0]["events"] = [TARGET_EVENT]
        server.policy[WAKES] = [{"workflow": "ci.yml", "events": [TARGET_EVENT]}]
        with self.assertRaisesRegex(Blocked, "redundant"):
            validate_policy(server.policy)

    def test_self_and_intent_wakes_are_rejected_even_during_prepare(self):
        """No wake may create an evaluator recursion or weaker origin path."""
        for path in (EVALUATOR, INTENT):
            server = CompletionServer()
            server.policy[WAKES] = [{**WAKE, "workflow": path.split("/")[-1]}]
            for event in ("workflow_run", TARGET_EVENT):
                environment = {"GITHUB_EVENT_NAME": event, "GITHUB_ACTOR": "renovate[bot]",
                               "GITHUB_RUN_ID": "500", "CALLER_WORKFLOW": EVALUATOR,
                               "INTENT_WORKFLOW": INTENT}
                payload = {"action": "completed" if event == "workflow_run" else "opened",
                           "workflow_run": {"id": 400}, "pull_request": {"number": 42, "head": {"sha": SHA}}}
                api = server.client()
                with self.subTest(path=path, event=event), mock.patch.dict(os.environ, environment), \
                        server.transport(), self.assertRaises(Blocked):
                    runner.context_from_event(api, payload, server.policy)

    def test_wake_does_not_change_complete_discovery_and_changes_intent_digest(self):
        """No extra producer, compare, job or required-check request is introduced."""
        server = PathServer()
        context, _ = fixtures()
        with server.transport():
            before_api = server.client()
            before = before_api.snapshot(42)
        before_calls = list(server.calls)
        intent = initial_intent(context, before, server.policy)
        old_digest = digest(server.policy)
        server.policy[WAKES] = [copy.deepcopy(WAKE)]
        with server.transport():
            after_api = server.client()
            after = after_api.snapshot(42)
        self.assertEqual(before_calls, server.calls[len(before_calls):])
        self.assertEqual(before["applicability"]["policy_digest"], old_digest)
        self.assertEqual(after["applicability"]["policy_digest"], digest(server.policy))
        before["applicability"]["policy_digest"] = digest(server.policy)
        self.assertEqual(before, after)
        self.assertEqual(before_api.api_calls, after_api.api_calls)
        self.assertNotEqual(old_digest, digest(server.policy))
        self.assertEqual(decide(context, after, server.policy, intent)[0], "blocked")


class QueuedObserverReadinessTest(unittest.TestCase):
    """Queue membership cannot replace either strict full readiness predicate."""

    def test_real_adapter_queued_observer_reads_twice_and_never_inserts(self):
        """Native typed provider data and both ahead/diverged proofs are recomputed."""
        for diverged, reads in ((False, 40), (True, 42)):
            server = PathServer(diverged=diverged)
            api = server.client()
            context, _ = fixtures()
            context.update(caller_path=INTENT, intent_path=INTENT, observe_only=True)
            with server.transport():
                intent = initial_intent(context, api.snapshot(42), server.policy)
                context.update(event="workflow_run", run_id=500, caller_path=EVALUATOR)
                server.raw["isInMergeQueue"] = True
                before = api.api_calls
                state, _ = runner.evaluate_and_enqueue(api, context, server.policy, intent)
            with self.subTest(diverged=diverged):
                self.assertEqual(state, READY_EXISTING)
                self.assertEqual(api.api_calls - before, reads)
                self.assertEqual(api.mutation_attempts, 0)

    def test_final_queue_fact_is_separate_from_two_ready_predicates(self):
        """A queue appearing between snapshots still cannot be claimed as insertion."""
        context, snapshot, policy, intent = cohort()
        for initial, final in ((False, False), (False, True), (True, True), (True, False)):
            first, second = copy.deepcopy(snapshot), copy.deepcopy(snapshot)
            first["pr"]["queued"], second["pr"]["queued"] = initial, final
            api = StubAPI([first, second], observe_only=True)
            state, _ = runner.evaluate_and_enqueue(api, context, policy, intent)
            with self.subTest(initial=initial, final=final):
                self.assertEqual(state, READY_EXISTING if final else "observed-ready")
                self.assertEqual(api.snapshots, [])
                self.assertEqual(api.enqueues, [])

    def test_queued_pr_cannot_hide_negative_policy_evidence_in_either_snapshot(self):
        """Every existing readiness prerequisite remains mandatory on both reads."""
        context, snapshot, policy, intent = cohort()
        for label, changed, expected in negative_snapshots(snapshot):
            for index in (0, 1):
                snapshots = [copy.deepcopy(snapshot), copy.deepcopy(snapshot)]
                snapshots[index] = copy.deepcopy(changed)
                for item in snapshots:
                    item["pr"]["queued"] = True
                api = StubAPI(snapshots, observe_only=True)
                with self.subTest(label=label, index=index):
                    self.assertEqual(runner.evaluate_and_enqueue(api, context, policy, intent)[0], expected)
                    self.assertEqual(len(api.snapshots), 1 if index == 0 else 0)
                    self.assertEqual(api.enqueues, [])

    def test_queued_observer_rejects_missing_or_rebound_intent(self):
        """Neither existing queue state nor a wake supplies origin authority."""
        context, snapshot, policy, intent = cohort()
        snapshot["pr"]["queued"] = True
        invalid = [None, {**intent, "expired": True}, {**intent, "observe_only": False},
                   {**intent, "policy_digest": "wrong"}, {**intent, "head": "c" * 40}, {**intent, "verified": False}]
        for document in invalid:
            api = StubAPI([snapshot, snapshot], observe_only=True)
            with self.subTest(document=document):
                self.assertEqual(runner.evaluate_and_enqueue(api, context, policy, document)[0], "blocked")
                self.assertEqual(len(api.snapshots), 1)
                self.assertEqual(api.enqueues, [])

    def test_second_snapshot_anchor_or_proof_changes_cannot_report_ready(self):
        """Queue state cannot override the existing cross-snapshot stability guard."""
        context, snapshot, policy, intent = cohort()
        snapshot["pr"]["queued"] = True
        for field in ("head", "base", "applicability"):
            changed = copy.deepcopy(snapshot)
            if field == "applicability":
                changed[field]["paths"] = ["different-file"]
            else:
                changed["pr"][field] = "c" * 40
            api = StubAPI([snapshot, changed], observe_only=True)
            with self.subTest(field=field):
                self.assertEqual(runner.evaluate_and_enqueue(api, context, policy, intent)[0], "blocked")
                self.assertEqual(api.enqueues, [])

    def test_v1_observation_and_every_enforcing_queued_noop_keep_one_read(self):
        """The new strict capability never enters a production queued decision."""
        context, snapshot, v2, _ = cohort()
        snapshot["pr"]["queued"] = True
        snapshot["checks"][0]["conclusion"] = "failure"
        for policy, observe, expected in ((POLICY, True, "observed-existing"),
                                           (POLICY, False, "queued"), (v2, False, "queued")):
            api = StubAPI([snapshot], observe_only=observe)
            current = context | {"observe_only": observe}
            with self.subTest(version=policy["version"], observe=observe):
                self.assertEqual(runner.evaluate_and_enqueue(api, current, policy, None)[0], expected)
                self.assertEqual(api.snapshots, [])
                self.assertEqual(api.enqueues, [])

    def test_internal_strict_option_cannot_be_used_outside_v2_observation(self):
        """An internal keyword never grants production a second queued decision."""
        context, snapshot, policy, intent = cohort()
        snapshot["pr"]["queued"] = True
        for current, selected, option in ((context, POLICY, True),
                                           (context | {"observe_only": False}, policy, True),
                                           (context, policy, "true")):
            with self.subTest(version=selected["version"], option=option):
                self.assertEqual(decide(current, snapshot, selected, intent, inspect_queued=option)[0], "blocked")
        self.assertEqual(decide(context, snapshot, policy, None)[0], "queued")
        api = StubAPI([snapshot], observe_only=False)
        self.assertEqual(runner.evaluate_and_enqueue(api, context, policy, None)[0], "queued")
        self.assertEqual(api.enqueues, [])

    def test_queued_observation_still_waits_for_an_absent_applicable_producer(self):
        """A queued native path match cannot waive missing conditional work."""
        server = PathServer(["Documentation/a.rst"])
        server.raw["isInMergeQueue"] = True
        api = server.client()
        context, _ = fixtures()
        context.update(caller_path=INTENT, intent_path=INTENT, observe_only=True)
        with server.transport():
            intent = initial_intent(context, api.snapshot(42), server.policy)
            context.update(event="workflow_run", run_id=500, caller_path=EVALUATOR)
            state, reason = runner.evaluate_and_enqueue(api, context, server.policy, intent)
        self.assertEqual(state, "waiting")
        self.assertIn("Missing expected workflow", reason)
        self.assertEqual(api.mutation_attempts, 0)

    def test_receipt_exposes_final_queue_fact_with_strict_state_and_zero_writes(self):
        """The new state records readiness separately from existing queue presence."""
        context, snapshot, policy, intent = cohort()
        snapshot["pr"]["queued"] = True
        api = StubAPI([snapshot, snapshot], observe_only=True)
        state, reason = runner.evaluate_and_enqueue(api, context, policy, intent)
        with tempfile.TemporaryDirectory() as directory:
            environment = {"RUNNER_TEMP": directory, "GITHUB_OUTPUT": str(Path(directory) / "output"),
                           "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary")}
            with mock.patch.dict(os.environ, environment):
                runner.write_receipt(api, context, policy, "evaluate", intent, state, reason)
            receipt = json.loads((Path(directory) / "dependency-controller-receipt.json").read_text())
        self.assertEqual(receipt["decision"], READY_EXISTING)
        self.assertIs(receipt["queue_present"], True)
        self.assertEqual(receipt["mutation_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
