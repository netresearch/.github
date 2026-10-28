"""Separate native origin and evaluator contracts; no remote mutations."""

import os
import unittest
from unittest import mock

from policy import initial_intent
from runner import ApiError, Blocked, context_from_event, phase_context, verify_intent_record
from test_policy import POLICY, REPO, SHA, fixtures

PRODUCER = ".github/workflows/dependency-merge-intent.yml"
EVALUATOR = ".github/workflows/auto-merge-deps.yml"
OBSERVER = ".github/workflows/dependency-controller-observe-intent.yml"


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.origin, self.snapshot = fixtures()
        self.intent = initial_intent(self.origin, self.snapshot, POLICY)
        self.context = self.origin | {"run_id": 10, "caller_path": EVALUATOR, "event": "workflow_run"}
        self.run = {"id": 9, "event": "pull_request_target", "path": PRODUCER,
                    "status": "completed", "conclusion": "success", "head_sha": SHA,
                    "head_branch": "renovate/phpunit", "actor": {"login": "renovate[bot]"},
                    "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
                    "created_at": self.origin["now"]}
        self.artifact = {"id": 12, "name": f"merge-intent-pr-42-{SHA}", "expired": False,
                         "workflow_run": {"id": 9}, "created_at": self.origin["now"],
                         "expires_at": "2026-11-04T10:00:00Z"}

    def test_origin_must_finish_successfully_before_later_run_can_trust_it(self):
        for changes in ({"status": "in_progress", "conclusion": None},
                        {"status": "completed", "conclusion": "failure"},
                        {"status": "completed", "conclusion": "cancelled"}):
            with self.subTest(changes=changes), self.assertRaises(ApiError):
                verify_intent_record(self.intent, self.artifact, self.run | changes, self.context)
        verified = verify_intent_record(self.intent, self.artifact, self.run, self.context)
        self.assertEqual(verified["artifact_id"], 12)
        self.assertEqual(verified["run_id"], 9)

    def test_observer_artifact_cannot_authorize_production_by_path_or_mode(self):
        context = self.origin | {"caller_path": OBSERVER, "intent_path": OBSERVER, "observe_only": True}
        document = initial_intent(context, self.snapshot, POLICY)
        run = self.run | {"path": OBSERVER}
        for evaluator in (self.context, self.context | {"intent_path": OBSERVER}):
            with self.subTest(intent_path=evaluator["intent_path"]), self.assertRaises(ApiError):
                verify_intent_record(document, self.artifact, run, evaluator)

    def test_producer_completion_is_an_explicit_extra_trusted_event(self):
        api = mock.Mock(repository=REPO, owner="netresearch", observe_only=False)
        api.run.return_value = self.run
        api.collection.return_value = [{"number": 42, "head": {"sha": SHA, "repo": {"full_name": REPO}}}]
        environment = {"GITHUB_EVENT_NAME": "workflow_run", "GITHUB_ACTOR": "human",
                       "GITHUB_RUN_ID": "10", "CALLER_WORKFLOW": EVALUATOR,
                       "INTENT_WORKFLOW": PRODUCER}
        event = {"action": "completed", "workflow_run": {"id": 9}}
        with mock.patch.dict(os.environ, environment):
            context = context_from_event(api, event, POLICY)
            self.assertEqual((context["number"], context["event_head"]), (42, SHA))
            for changes in ({"actor": {"login": "human"}}, {"path": ".github/workflows/other.yml"},
                            {"event": "workflow_dispatch"}, {"conclusion": "failure"}):
                api.run.return_value = self.run | changes
                with self.subTest(changes=changes), self.assertRaises(Blocked):
                    context_from_event(api, event, POLICY)

    def test_same_run_or_target_evaluation_cannot_replace_cross_run_handoff(self):
        with self.assertRaises(ApiError):
            verify_intent_record(self.intent, self.artifact, self.run, self.origin)
        with self.assertRaises(Blocked):
            phase_context(self.origin, "evaluate")
        with self.assertRaises(Blocked):
            phase_context(self.context, "prepare")


if __name__ == "__main__":
    unittest.main()
