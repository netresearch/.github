"""Selected native metadata wakes never create intent or relax full readiness."""

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

import runner
from policy import Blocked, initial_intent
from test_policy import POLICY, REPO, SHA, fixtures
from test_runner import FakeAPI
from test_wiring import manifest


PRODUCER = ".github/workflows/dependency-merge-intent.yml"
EVALUATOR = ".github/workflows/dependency-merge-evaluate.yml"


def event(action):
    return {"action": action, "repository": {"full_name": REPO},
            "sender": {"login": "maintainer", "type": "User"},
            "pull_request": {"number": 42, "draft": False, "user": {"login": "renovate[bot]"},
                "head": {"sha": SHA, "ref": "renovate/phpunit", "repo": {"full_name": REPO}},
                "base": {"ref": "main", "repo": {"full_name": REPO}}},
            "label": {"name": "deps-no-automerge"},
            "changes": {"base": {"ref": {"from": "previous-base"}}}}


class Missing(str):
    def __new__(cls):
        return super().__new__(cls, "")

    def __getattr__(self, name):
        return self

    def __getitem__(self, key):
        return self


class Values(dict):
    def __getitem__(self, key):
        value = self.get(key, Missing())
        return Values(value) if isinstance(value, dict) else value

    __getattr__ = __getitem__


def allocated(payload, phase="evaluate", actor="maintainer"):
    """Evaluate the manifest's small boolean subset; hosted scheduling is separate."""
    expression = manifest(".github/workflows/dependency-merge-controller.yml")["jobs"]["controller"]["if"]
    expression = " ".join(expression.split())
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"\btrue\b", "True", expression)
    expression = re.sub(r"\bfalse\b", "False", expression)
    return bool(eval(expression, {"__builtins__": {},
        "startsWith": lambda value, prefix: isinstance(value, str) and value.startswith(prefix)},
        {"inputs": Values(phase=phase), "github": Values(repository=REPO, actor=actor,
            event_name="pull_request_target", event=payload)}))


class MetadataTest(unittest.TestCase):
    def environment(self):
        return {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_ACTOR": "maintainer",
                "GITHUB_RUN_ID": "10", "CALLER_WORKFLOW": EVALUATOR, "INTENT_WORKFLOW": PRODUCER}

    def context(self, payload):
        api = FakeAPI([])
        with mock.patch.dict(os.environ, self.environment()):
            return runner.context_from_event(api, payload, POLICY)

    def test_four_selected_shapes_allocate_and_accept_evaluation(self):
        for action in ("unlabeled", "ready_for_review", "edited", "auto_merge_enabled"):
            with self.subTest(action=action):
                payload = event(action)
                self.assertTrue(allocated(payload))
                context = self.context(payload)
                runner.phase_context(context, "evaluate")
                self.assertEqual(context["event_head"], SHA)
                with self.assertRaises(Blocked):
                    runner.phase_context(context | {"caller_path": PRODUCER}, "prepare")

    def test_bot_only_initial_producer_and_phase_partition_are_preserved(self):
        for action in ("opened", "synchronize", "reopened"):
            with self.subTest(action=action):
                self.assertTrue(allocated(event(action), phase="prepare", actor="renovate[bot]"))
                self.assertFalse(allocated(event(action), phase="prepare", actor="maintainer"))
                self.assertFalse(allocated(event(action), phase="evaluate", actor="renovate[bot]"))
        for action in ("unlabeled", "ready_for_review", "edited", "auto_merge_enabled"):
            self.assertFalse(allocated(event(action), phase="prepare", actor="renovate[bot]"))

    def test_malformed_metadata_is_rejected_before_any_adapter_read(self):
        for payload in ({}, {"action": "unlabeled", "pull_request": None},
                        event("edited") | {"changes": {"base": None}},
                        event("auto_merge_enabled") | {"sender": {"type": "User", "login": []}}):
            with self.subTest(payload=payload), self.assertRaises(Blocked):
                self.context(payload)

    def test_irrelevant_and_malformed_shapes_do_not_allocate_or_reach_reads(self):
        cases = []
        for action in ("opened", "synchronize", "closed", "labeled", "unknown"):
            cases.append(event(action))
        ordinary_label = event("unlabeled")
        ordinary_label["label"]["name"] = "documentation"
        cases.append(ordinary_label)
        for changes in ({}, {"title": {"from": "old"}}, {"base": {"ref": {"from": "main"}}},
                        {"base": {"ref": {"from": ""}}}):
            payload = event("edited")
            payload["changes"] = changes
            cases.append(payload)
        draft = event("ready_for_review")
        draft["pull_request"]["draft"] = True
        cases.append(draft)
        bot_activation = event("auto_merge_enabled")
        bot_activation["sender"]["type"] = "Bot"
        cases.append(bot_activation)
        for change in ("author", "branch", "source", "base"):
            payload = event("unlabeled")
            if change == "author":
                payload["pull_request"]["user"]["login"] = "human"
            elif change == "branch":
                payload["pull_request"]["head"]["ref"] = "feature/test"
            else:
                payload["pull_request"]["head" if change == "source" else "base"]["repo"]["full_name"] = "foreign/fork"
            cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertFalse(allocated(payload))
                # Initial target events have context, but cannot evaluate.
                try:
                    context = self.context(payload)
                except Blocked:
                    continue
                with self.assertRaises(Blocked):
                    runner.phase_context(context, "evaluate")

    def run_main(self, payload, snapshots, intent, phase="evaluate"):
        api = FakeAPI(snapshots)
        api.observe_only = True
        api.intent = mock.Mock(return_value=intent)
        api.request = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "event.json"
            path.write_text(json.dumps(payload))
            environment = self.environment() | {
                "GITHUB_REPOSITORY": REPO, "CONTROLLER_POLICY": json.dumps(POLICY),
                "GITHUB_EVENT_PATH": str(path), "READ_TOKEN": "synthetic", "OBSERVE_ONLY": "true",
                "RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(root / "summary")}
            with mock.patch.dict(os.environ, environment), mock.patch("runner.GitHub", return_value=api), \
                    mock.patch("runner.output"), mock.patch("runner.prepare_intent") as prepare:
                runner.main(phase)
            receipt = json.loads((root / "dependency-controller-receipt.json").read_text())
            prepare.assert_not_called()
            api.request.assert_not_called()
            self.assertEqual(api.mutations, [])
            return receipt, api

    def observer_intent(self):
        context, snapshot = fixtures()
        context["observe_only"] = True
        return snapshot, initial_intent(context, snapshot, POLICY)

    def test_metadata_observation_uses_two_complete_reads_and_never_prepares(self):
        snapshot, intent = self.observer_intent()
        for action in ("unlabeled", "ready_for_review", "edited", "auto_merge_enabled"):
            with self.subTest(action=action):
                receipt, api = self.run_main(event(action), [snapshot, snapshot], intent)
                self.assertEqual(receipt["decision"], "observed-ready")
                self.assertEqual(api.snapshots, [])

    def test_absent_intent_and_old_head_do_not_become_authorization(self):
        snapshot, intent = self.observer_intent()
        stale = event("unlabeled")
        stale["pull_request"]["head"]["sha"] = "c" * 40
        for payload, artifact in ((event("ready_for_review"), None), (stale, intent)):
            with self.subTest(payload=payload):
                receipt, api = self.run_main(payload, [snapshot], artifact)
                self.assertEqual(receipt["decision"], "blocked")

    def test_final_base_change_or_pending_gate_still_blocks_mutation(self):
        snapshot, intent = self.observer_intent()
        for change in ("base", "check"):
            fresh = copy.deepcopy(snapshot)
            if change == "base":
                fresh["pr"]["base"] = "d" * 40
            else:
                fresh["checks"][0]["status"] = "queued"
            with self.subTest(change=change):
                receipt, api = self.run_main(event("edited"), [snapshot, fresh], intent)
                self.assertEqual(receipt["decision"], "blocked" if change == "base" else "waiting")

    def test_only_later_human_activation_can_recover_after_removal_without_intent(self):
        snapshot, intent = self.observer_intent()
        snapshot["history"] = [{"type": "RemovedFromMergeQueueEvent", "created_at": "2026-10-28T09:00:00Z",
                                "actor_type": "User", "actor": "maintainer"}]
        receipt, _ = self.run_main(event("auto_merge_enabled"), [snapshot], None)
        self.assertEqual(receipt["decision"], "blocked")
        snapshot["history"].append({"type": "AutoMergeEnabledEvent", "created_at": "2026-10-28T09:01:00Z",
                                    "actor_type": "User", "actor": "maintainer"})
        receipt, _ = self.run_main(event("auto_merge_enabled"), [snapshot, snapshot], None)
        self.assertEqual(receipt["decision"], "observed-ready")

    def test_actual_pre_mint_shell_accepts_only_selected_target_evaluation_actions(self):
        workflow = manifest(".github/workflows/dependency-merge-controller.yml")
        script = next(step["run"] for step in workflow["jobs"]["controller"]["steps"]
                      if step.get("name") == "Validate phase and separate caller identities before minting")
        for action, allowed in (("unlabeled", True), ("ready_for_review", True), ("edited", True),
                                ("auto_merge_enabled", True), ("opened", False), ("unknown", False)):
            environment = os.environ | {"PHASE": "evaluate", "EVENT": "pull_request_target",
                "ACTION": action, "CALLER": EVALUATOR, "INTENT": PRODUCER}
            result = subprocess.run(["bash", "-e", "-c", script], env=environment, capture_output=True)
            with self.subTest(action=action):
                self.assertEqual(result.returncode == 0, allowed)

    def test_current_head_rerun_uses_existing_event_without_new_intent(self):
        snapshot, intent = self.observer_intent()
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
            receipt, api = self.run_main(event("unlabeled"), [snapshot, snapshot], intent)
        self.assertEqual(receipt["run_id"], 10)
        self.assertEqual(receipt["event"], "pull_request_target")
        self.assertEqual(receipt["decision"], "observed-ready")
        self.assertEqual(api.intent.call_count, 1)
        action = manifest(".github/actions/dependency-merge-controller/action.yml")
        upload = next(step for step in action["runs"]["steps"]
                      if step.get("name") == "Retain the observer receipt without modifying a PR")
        self.assertIn("github.run_id", upload["with"]["name"])
        self.assertIn("github.run_attempt", upload["with"]["name"])


if __name__ == "__main__":
    unittest.main()
