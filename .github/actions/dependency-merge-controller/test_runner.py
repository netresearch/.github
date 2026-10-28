"""Controller/API seam fixtures. Every external operation is a local stub."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from policy import initial_intent
from runner import ApiError, GitHub, evaluate_and_enqueue, main, verify_intent_record
from test_policy import POLICY, REPO, SHA, TIME, fixtures


class FakeAPI:
    def __init__(self, snapshots):
        self.repository = REPO
        self.snapshots = list(snapshots)
        self.mutations = []

    def snapshot(self, number):
        return copy.deepcopy(self.snapshots.pop(0))

    def enqueue(self, pr_id, head):
        self.mutations.append((pr_id, head))


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.context, self.snapshot = fixtures()
        self.intent = initial_intent(self.context, self.snapshot, POLICY)

    def test_insert_once_with_expected_head_and_confirm_once(self):
        queued = copy.deepcopy(self.snapshot)
        queued["pr"]["queued"] = True
        api = FakeAPI([self.snapshot, self.snapshot, queued])
        self.assertEqual(evaluate_and_enqueue(api, self.context, POLICY, self.intent)[0], "queued")
        self.assertEqual(api.mutations, [("PR_42", SHA)])
        self.assertEqual(api.snapshots, [])

    def test_later_completion_for_existing_entry_never_mutates(self):
        self.snapshot["pr"]["queued"] = True
        api = FakeAPI([self.snapshot])
        self.assertEqual(evaluate_and_enqueue(api, self.context, POLICY, self.intent)[0], "queued")
        self.assertEqual(api.mutations, [])

    def test_changed_head_or_removal_on_final_read_blocks(self):
        for field in ("head", "history"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.snapshot)
                if field == "head":
                    changed["pr"]["head"] = "c" * 40
                else:
                    changed["history"] = [{"type": "RemovedFromMergeQueueEvent",
                        "created_at": "2026-10-28T10:01:00Z", "actor_type": "User"}]
                api = FakeAPI([self.snapshot, changed])
                self.assertEqual(evaluate_and_enqueue(api, self.context, POLICY, self.intent)[0], "blocked")
                self.assertEqual(api.mutations, [])

    def test_pending_result_does_not_poll(self):
        self.snapshot["checks"][0]["status"] = "in_progress"
        api = FakeAPI([self.snapshot])
        self.assertEqual(evaluate_and_enqueue(api, self.context, POLICY, self.intent)[0], "waiting")
        self.assertEqual(api.mutations, [])
        self.assertEqual(api.snapshots, [])

    def test_initial_pending_run_persists_intent_without_enqueuing(self):
        self.snapshot["checks"][0]["status"] = "queued"
        api = FakeAPI([self.snapshot])
        api.intent = lambda context: None
        api.request = mock.Mock()
        emitted = []
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text("{}")
            environ = {"CONTROLLER_POLICY": json.dumps(POLICY), "GITHUB_REPOSITORY": REPO,
                       "READ_TOKEN": "stub", "GITHUB_EVENT_PATH": str(event), "RUNNER_TEMP": directory}
            with mock.patch.dict(os.environ, environ), mock.patch("runner.GitHub", return_value=api), \
                    mock.patch("runner.context_from_event", return_value=self.context), \
                    mock.patch("runner.output", side_effect=lambda **values: emitted.append(values)):
                main("prepare")
            self.assertEqual(emitted[0]["intent_name"], f"merge-intent-pr-42-{SHA}")
            saved = json.loads(Path(emitted[0]["intent_path"]).read_text())
            self.assertEqual(saved["head"], SHA)
            self.assertEqual(saved["run_id"], 9)
            self.assertEqual(api.mutations, [])

    def test_failed_intent_storage_cannot_progress_to_enqueue(self):
        api = FakeAPI([self.snapshot])
        self.assertEqual(evaluate_and_enqueue(api, self.context, POLICY, None)[0], "blocked")
        self.assertEqual(api.mutations, [])

    def provenance(self):
        artifact = {"name": f"merge-intent-pr-42-{SHA}", "expired": False,
                    "workflow_run": {"id": 9}, "created_at": TIME,
                    "expires_at": "2026-11-04T10:00:00Z"}
        run = {"id": 9, "event": "pull_request_target", "actor": {"login": "renovate[bot]"},
               "path": ".github/workflows/auto-merge-deps.yml", "status": "completed",
               "conclusion": "success", "repository": {"full_name": REPO},
               "head_repository": {"full_name": REPO}, "created_at": TIME}
        return artifact, run

    def test_exact_server_metadata_verifies_intent(self):
        artifact, run = self.provenance()
        self.assertTrue(verify_intent_record(self.intent, artifact, run, self.context)["verified"])

    def test_spoofed_origin_or_expired_artifact_blocks(self):
        variants = [("artifact", "expired", True), ("artifact", "workflow_run", {"id": 10}),
                    ("artifact", "name", "fake"), ("run", "path", ".github/workflows/fake.yml"),
                    ("run", "event", "pull_request"), ("run", "actor", {"login": "attacker"}),
                    ("run", "repository", {"full_name": "attacker/fork"}),
                    ("run", "head_repository", {"full_name": "attacker/fork"})]
        for where, key, value in variants:
            artifact, run = self.provenance()
            with self.subTest(where=where, key=key), self.assertRaises(ApiError):
                (artifact if where == "artifact" else run)[key] = value
                verify_intent_record(self.intent, artifact, run, self.context)

    def test_expired_intent_is_absent_and_requires_explicit_human_recovery(self):
        client = GitHub.__new__(GitHub)
        client.repository = REPO
        artifact, _ = self.provenance()
        artifact["expired"] = True
        client.collection = lambda *args: [artifact]
        self.assertIsNone(client.intent(self.context))
        self.assertEqual(evaluate_and_enqueue(FakeAPI([self.snapshot]), self.context, POLICY, None)[0], "blocked")

    def test_rest_pagination_fails_closed_at_cap(self):
        client = GitHub.__new__(GitHub)
        client.rest = lambda path: ([1] * 100, {"Link": '<next>; rel="next"'})
        with self.assertRaises(ApiError):
            client.collection("/records", maximum_pages=2)

    def test_rest_pagination_collects_every_page(self):
        client = GitHub.__new__(GitHub)
        seen = []

        def page(path):
            seen.append(path)
            return ([1], {"Link": '<next>; rel="next"'}) if len(seen) == 1 else ([2], {})

        client.rest = page
        self.assertEqual(client.collection("/records"), [1, 2])
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
