"""Separate prepare/evaluate and no-write observer contracts, using local stubs."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import runner
from policy import initial_intent
from test_policy import POLICY, REPO, SHA, fixtures


PRODUCER = ".github/workflows/dependency-merge-intent.yml"
EVALUATOR = ".github/workflows/dependency-merge-controller.yml"


class StubAPI:
    def __init__(self, snapshots, observe_only=False, intent=None):
        self.repository = REPO
        self.observe_only = observe_only
        self.mutation_attempts = 0
        self.snapshots = list(snapshots)
        self.intent_document = intent
        self.requests = []
        self.enqueues = []
        self.approved_commits = []

    def snapshot(self, number):
        return copy.deepcopy(self.snapshots.pop(0))

    def intent(self, context):
        return self.intent_document

    def request(self, path, method="GET", data=None, **options):
        self.requests.append((path, method, copy.deepcopy(data), options))
        if method == "POST" and path.endswith("/reviews"):
            # Simulate a new commit arriving after the preparation snapshot.
            live_head_when_posted = "c" * 40
            self.approved_commits.append(data.get("commit_id", live_head_when_posted))
        return {}, {}

    def enqueue(self, pr_id, head):
        self.enqueues.append((pr_id, head))


class ExecutionInputTest(unittest.TestCase):
    def test_only_explicit_phases_and_exact_boolean_strings_are_accepted(self):
        for phase in ("prepare", "evaluate"):
            for raw, expected in (("true", True), ("false", False)):
                with self.subTest(phase=phase, raw=raw):
                    self.assertIs(runner.parse_execution(phase, raw), expected)

    def test_unknown_phase_cannot_fall_through_to_evaluation(self):
        for phase in (None, "", "Prepare", "evalute", "evaluate ", "all", True, 0):
            with self.subTest(phase=phase), self.assertRaises(runner.ApiError):
                runner.parse_execution(phase, "false")

    def test_observer_value_never_falls_back_to_enforcement(self):
        for raw in (None, "", "TRUE", "False", " true", "false\n", "0", "1", True, False, 0):
            with self.subTest(raw=raw), self.assertRaises(runner.ApiError):
                runner.parse_execution("evaluate", raw)

    def test_invalid_public_inputs_fail_before_api_construction(self):
        for phase, raw in (("evalute", "false"), ("", "true"), ("evaluate", "yes")):
            with self.subTest(phase=phase, raw=raw), \
                    mock.patch.dict(os.environ, {"OBSERVE_ONLY": raw}, clear=True), \
                    mock.patch("runner.GitHub") as constructor, \
                    self.assertRaises(runner.ApiError):
                runner.main(phase)
            constructor.assert_not_called()


class ObserverAdapterTest(unittest.TestCase):
    def client(self, phase="evaluate"):
        return runner.GitHub(REPO, "read-stub", "write-stub", observe_only=True, phase=phase)

    def test_observer_rejects_every_rest_write_before_request_construction(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for write in (False, True):
                api = self.client()
                with self.subTest(method=method, write=write), \
                        mock.patch("runner.urllib.request.Request") as sink, \
                        self.assertRaises(runner.ApiError):
                    api.request(f"/repos/{REPO}/pulls/42/reviews", method,
                                {"event": "APPROVE", "commit_id": SHA}, write=write)
                sink.assert_not_called()
                self.assertEqual(api.mutation_attempts, 1)

    def test_observer_rejects_graphql_mutations_even_when_write_flag_is_false(self):
        mutations = (
            "mutation($id:ID!){enqueuePullRequest(input:{pullRequestId:$id}){mergeQueueEntry{id}}}",
            "\n  mutation Enable{enablePullRequestAutoMerge(input:{pullRequestId:\"PR_42\"}){clientMutationId}}",
            "# a read-token flag is not authorization\nmutation M{deletePullRequestReview(input:{pullRequestReviewId:\"R_1\"}){clientMutationId}}",
        )
        for query in mutations:
            for write in (False, True):
                api = self.client()
                with self.subTest(query=query, write=write), \
                        mock.patch("runner.urllib.request.Request") as sink, \
                        self.assertRaises(runner.ApiError):
                    api.request("/graphql", "POST", {"query": query, "variables": {}}, write=write)
                sink.assert_not_called()
                self.assertEqual(api.mutation_attempts, 1)

    def test_observer_keeps_rest_get_and_generated_graphql_queries_available(self):
        api = self.client()
        response = mock.MagicMock()
        response.__enter__.return_value.headers = {}
        response.__enter__.return_value.read.side_effect = [
            b'{"id":9}',
            b'{"data":{"repository":{"pullRequest":{"id":"PR_42"}}}}',
        ]
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch("runner.urllib.request.build_opener", return_value=opener):
            self.assertEqual(api.rest(f"/repos/{REPO}/actions/runs/9")[0], {"id": 9})
            self.assertEqual(api.pr_query(42, "id"), {"id": "PR_42"})
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(api.mutation_attempts, 0)

    def test_prepare_adapter_cannot_enqueue_even_in_enforcement_mode(self):
        api = runner.GitHub(REPO, "read-stub", "write-stub", observe_only=False, phase="prepare")
        with mock.patch("runner.urllib.request.Request") as sink, self.assertRaises(runner.ApiError):
            api.enqueue("PR_42", SHA)
        sink.assert_not_called()


class ObserverDecisionTest(unittest.TestCase):
    def setUp(self):
        self.context, self.snapshot = fixtures()
        self.context.update(intent_path=PRODUCER, caller_path=PRODUCER, observe_only=True)
        self.intent = initial_intent(self.context, self.snapshot, POLICY)
        self.context.update(event="workflow_run", caller_path=EVALUATOR, run_id=10)

    def test_ready_observation_requires_two_snapshots_and_never_enqueues(self):
        api = StubAPI([self.snapshot, self.snapshot], observe_only=True)
        state, _ = runner.evaluate_and_enqueue(api, self.context, POLICY, self.intent)
        self.assertEqual(state, "observed-ready")
        self.assertEqual(api.snapshots, [])
        self.assertEqual(api.enqueues, [])

    def test_existing_entry_is_reported_as_observed_without_claiming_queue_insertion(self):
        self.snapshot["pr"]["queued"] = True
        api = StubAPI([self.snapshot], observe_only=True)
        state, _ = runner.evaluate_and_enqueue(api, self.context, POLICY, self.intent)
        self.assertEqual(state, "observed-existing")
        self.assertEqual(api.enqueues, [])

    def test_existing_entry_appearing_on_final_read_is_observed_existing(self):
        queued = copy.deepcopy(self.snapshot)
        queued["pr"]["queued"] = True
        api = StubAPI([self.snapshot, queued], observe_only=True)
        state, _ = runner.evaluate_and_enqueue(api, self.context, POLICY, self.intent)
        self.assertEqual(state, "observed-existing")
        self.assertEqual(api.enqueues, [])

    def test_observation_does_not_relax_freshness_checks_or_missing_intent(self):
        changed = copy.deepcopy(self.snapshot)
        changed["pr"]["head"] = "c" * 40
        for snapshots, intent in (([self.snapshot, changed], self.intent), ([self.snapshot], None)):
            api = StubAPI(snapshots, observe_only=True)
            with self.subTest(intent_present=intent is not None):
                state, _ = runner.evaluate_and_enqueue(api, self.context, POLICY, intent)
                self.assertEqual(state, "blocked")
                self.assertEqual(api.enqueues, [])

    def test_pending_observation_keeps_waiting_without_an_extra_snapshot(self):
        self.snapshot["checks"][0]["status"] = "in_progress"
        api = StubAPI([self.snapshot], observe_only=True)
        state, _ = runner.evaluate_and_enqueue(api, self.context, POLICY, self.intent)
        self.assertEqual(state, "waiting")
        self.assertEqual(api.snapshots, [])
        self.assertEqual(api.enqueues, [])


class PhaseEntrypointTest(unittest.TestCase):
    def run_main(self, phase, api):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"action": "opened", "pull_request": {
                "number": 42, "head": {"sha": SHA}}}))
            summary = Path(directory) / "summary.md"
            environment = {
                "CONTROLLER_POLICY": json.dumps(POLICY), "GITHUB_REPOSITORY": REPO,
                "READ_TOKEN": "read-stub", "WRITE_TOKEN": "write-stub",
                "GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_ACTOR": "renovate[bot]", "GITHUB_RUN_ID": "9",
                "RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(summary),
                "CALLER_WORKFLOW": PRODUCER, "INTENT_WORKFLOW": PRODUCER,
                "OBSERVE_ONLY": "true" if api.observe_only else "false",
            }
            emitted = []
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("runner.GitHub", return_value=api), \
                    mock.patch("runner.output", side_effect=lambda **values: emitted.append(values)), \
                    mock.patch("runner.evaluate_and_enqueue") as evaluate:
                runner.main(phase)
            documents = [json.loads(Path(values["intent_path"]).read_text())
                         for values in emitted if "intent_path" in values]
            return emitted, documents, evaluate

    def test_prepare_finishes_without_running_the_evaluator_even_when_ready(self):
        _, snapshot = fixtures()
        api = StubAPI([snapshot])
        _, documents, evaluate = self.run_main("prepare", api)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["head"], SHA)
        self.assertEqual(api.enqueues, [])
        evaluate.assert_not_called()

    def test_prepare_approval_is_bound_to_snapshot_head_despite_a_new_live_head(self):
        _, snapshot = fixtures()
        api = StubAPI([snapshot])
        self.run_main("prepare", api)
        self.assertEqual(api.approved_commits, [SHA])
        self.assertEqual(len(api.requests), 1)
        path, method, body, _ = api.requests[0]
        self.assertEqual((path, method), (f"/repos/{REPO}/pulls/42/reviews", "POST"))
        self.assertEqual(body, {"event": "APPROVE", "commit_id": SHA})

    def test_observer_prepare_saves_intent_without_approval_or_enqueue(self):
        _, snapshot = fixtures()
        api = StubAPI([snapshot], observe_only=True)
        _, documents, evaluate = self.run_main("prepare", api)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["head"], SHA)
        self.assertEqual(api.requests, [])
        self.assertEqual(api.enqueues, [])
        evaluate.assert_not_called()

    def test_blocked_preparation_has_no_intent_approval_or_evaluation(self):
        _, snapshot = fixtures()
        snapshot["pr"]["author"] = "human"
        api = StubAPI([snapshot], observe_only=True)
        _, documents, evaluate = self.run_main("prepare", api)
        self.assertEqual(documents, [])
        self.assertEqual(api.requests, [])
        self.assertEqual(api.enqueues, [])
        evaluate.assert_not_called()

    def test_context_keeps_executing_caller_and_trusted_intent_workflow_distinct(self):
        api = StubAPI([], observe_only=True)
        event = {"action": "submitted", "pull_request": {"number": 42, "head": {"sha": SHA}}}
        environment = {"GITHUB_EVENT_NAME": "pull_request_review", "GITHUB_ACTOR": "reviewer",
                       "GITHUB_RUN_ID": "10", "CALLER_WORKFLOW": EVALUATOR,
                       "INTENT_WORKFLOW": PRODUCER, "OBSERVE_ONLY": "true"}
        with mock.patch.dict(os.environ, environment):
            context = runner.context_from_event(api, event, POLICY)
        self.assertEqual(context["caller_path"], EVALUATOR)
        self.assertEqual(context["intent_path"], PRODUCER)
        self.assertIs(context["observe_only"], True)


if __name__ == "__main__":
    unittest.main()
