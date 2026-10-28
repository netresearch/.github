"""Offline policy fixtures. No token, network or sender is used."""

import copy
import unittest

from policy import Blocked, decide, initial_intent


SHA = "a" * 40
REPO = "netresearch/t3x-nr-vault"
TIME = "2026-10-28T10:00:00Z"
POLICY = {
    "version": 1,
    "required_checks": [
        {"name": "ci / All CI checks", "app_id": 15368,
         "workflow": "ci.yml", "events": ["pull_request"]},
        {"name": "All E2E checks", "app_id": 15368,
         "workflow": "e2e.yml", "events": ["pull_request"]},
    ],
    "minimum_approvals": 2,
    "safe_updates_only": False,
}


def fixtures():
    context = {"repository": REPO, "event": "pull_request_target",
               "action": "opened", "actor": "renovate[bot]", "run_id": 9,
               "caller_path": ".github/workflows/dependency-merge-intent.yml",
               "intent_path": ".github/workflows/dependency-merge-intent.yml", "observe_only": False,
               "event_head": SHA, "number": 42, "now": TIME}
    snapshot = {
        "pr": {"number": 42, "id": "PR_42", "repository": REPO,
               "source_repository": REPO, "author": "renovate[bot]",
               "head": SHA, "base": "b" * 40, "base_ref": "main",
               "head_ref": "renovate/phpunit", "state": "OPEN", "draft": False,
               "mergeable": "MERGEABLE", "review_decision": "APPROVED",
               "queued": False, "labels": []},
        "reviews": [{"author": "one", "state": "APPROVED", "commit": SHA},
                    {"author": "two", "state": "APPROVED", "commit": SHA}],
        "threads": [], "history": [],
        "rules": [{"type": "merge_queue"}, {"type": "required_status_checks",
                  "parameters": {"required_status_checks": [
                      {"context": c["name"], "integration_id": 15368}
                      for c in POLICY["required_checks"]]}}],
        "complete": {"history": True, "reviews": True, "threads": True,
                     "rules": True, "checks": True, "statuses": True},
        "checks": [{"name": c["name"], "app_id": 15368, "app_slug": "github-actions",
                    "sha": SHA, "workflow": c["workflow"], "event": "pull_request",
                    "repository": REPO, "run_id": 20, "attempt": 1,
                    "job_verified": True, "job_current": True,
                    "id": i, "status": "completed", "conclusion": "success"}
                   for i, c in enumerate(POLICY["required_checks"], start=1)],
        "statuses": [],
        "workflows": [{"workflow": check["workflow"], "event": "pull_request",
                       "repository": REPO, "sha": SHA, "run_id": 20,
                       "status": "completed", "conclusion": "success"}
                      for check in POLICY["required_checks"]],
    }
    return context, snapshot


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.context, self.snapshot = fixtures()
        self.intent = initial_intent(self.context, self.snapshot, POLICY)

    def result(self, **changes):
        context = self.context | {"event": "workflow_run", "actor": "human", "run_id": 10,
                                  "caller_path": ".github/workflows/auto-merge-deps.yml"} | changes
        return decide(context, self.snapshot, POLICY, self.intent)[0]

    def test_fresh_pending_then_completion_enqueues(self):
        self.snapshot["checks"][0]["status"] = "in_progress"
        intent = initial_intent(self.context, self.snapshot, POLICY)
        self.assertEqual(decide(self.context, self.snapshot, POLICY, intent)[0], "waiting")
        self.snapshot["checks"][0]["status"] = "completed"
        self.assertEqual(self.result(), "ready")

    def test_already_queued_is_noop(self):
        self.snapshot["pr"]["queued"] = True
        self.assertEqual(self.result(), "queued")

    def test_missing_check_waits(self):
        self.snapshot["checks"].pop()
        self.assertEqual(self.result(), "waiting")

    def test_older_success_does_not_hide_newer_pending(self):
        newer = self.snapshot["checks"][0] | {"run_id": 21, "status": "queued"}
        self.snapshot["checks"].append(newer)
        self.snapshot["workflows"].append(self.snapshot["workflows"][0] | {"run_id": 21, "status": "queued", "conclusion": None})
        self.assertEqual(self.result(), "waiting")

    def test_new_workflow_pending_before_any_new_check_exists(self):
        self.snapshot["workflows"].append(self.snapshot["workflows"][0] | {
            "run_id": 21, "status": "queued", "conclusion": None})
        self.assertEqual(self.result(), "waiting")

    def test_controller_name_from_other_run_does_not_hide_failure(self):
        self.snapshot["checks"].append(self.snapshot["checks"][0] | {
            "name": "auto-merge / Dependency merge controller", "conclusion": "failure"})
        self.assertEqual(self.result(), "blocked")

    def test_newer_failed_attempt_blocks(self):
        self.snapshot["checks"].append(self.snapshot["checks"][0] | {
            "attempt": 2, "id": 50, "conclusion": "failure"})
        self.assertEqual(self.result(), "blocked")

    def test_wrong_app_or_workflow_cannot_satisfy_name(self):
        for changes in ({"app_id": 12}, {"app_slug": "forged"},
                        {"workflow": "attacker.yml"}, {"event": "workflow_dispatch"},
                        {"repository": "attacker/fork"}, {"sha": "c" * 40}):
            with self.subTest(changes=changes):
                self.snapshot["checks"][0] = fixtures()[1]["checks"][0] | changes
                self.assertEqual(self.result(), "blocked")

    def test_new_wrong_app_does_not_hide_behind_old_trusted_success(self):
        self.snapshot["checks"].append(self.snapshot["checks"][0] | {
            "id": 500, "run_id": 0, "app_id": 12, "app_slug": "fake"})
        self.assertEqual(self.result(), "blocked")

    def test_additional_failure_is_not_hidden_by_inventory(self):
        self.snapshot["checks"].append(self.snapshot["checks"][0] | {
            "name": "another quality gate", "conclusion": "failure"})
        self.assertEqual(self.result(), "blocked")

    def test_required_skipped_is_not_green(self):
        self.snapshot["checks"][0]["conclusion"] = "skipped"
        self.assertEqual(self.result(), "blocked")

    def test_unreadable_or_truncated_collections_block(self):
        for field in self.snapshot["complete"]:
            with self.subTest(field=field):
                self.snapshot["complete"][field] = False
                self.assertEqual(self.result(), "blocked")
                self.snapshot["complete"][field] = True

    def test_unknown_branch_rule_check_blocks(self):
        self.snapshot["rules"][1]["parameters"]["required_status_checks"].append(
            {"context": "new required gate", "integration_id": 15368})
        self.assertEqual(self.result(), "blocked")

    def test_no_queue_rule_never_direct_merges(self):
        self.snapshot["rules"].pop(0)
        self.assertEqual(self.result(), "blocked")

    def test_manual_dequeue_survives_new_bot_commit(self):
        self.snapshot["history"] = [{"type": "RemovedFromMergeQueueEvent",
                                     "created_at": "2026-10-28T10:01:00Z",
                                     "actor_type": "User", "actor": "maintainer"}]
        self.assertEqual(self.result(), "blocked")
        with self.assertRaises(Blocked):
            initial_intent(self.context, self.snapshot, POLICY)

    def test_human_reactivation_restores_intent(self):
        self.snapshot["history"] = [
            {"type": "RemovedFromMergeQueueEvent", "created_at": "2026-10-28T09:00:00Z",
             "actor_type": "Bot", "actor": "github-merge-queue[bot]"},
            {"type": "AutoMergeEnabledEvent", "created_at": "2026-10-28T09:01:00Z",
             "actor_type": "User", "actor": "maintainer"}]
        self.assertEqual(self.result(), "ready")

    def test_bot_reactivation_cannot_override_removal(self):
        self.snapshot["history"] = [
            {"type": "AutoMergeDisabledEvent", "created_at": "2026-10-28T10:01:00Z",
             "actor_type": "User", "actor": "maintainer"},
            {"type": "AutoMergeEnabledEvent", "created_at": "2026-10-28T10:02:00Z",
             "actor_type": "Bot", "actor": "renovate[bot]"}]
        self.assertEqual(self.result(), "blocked")

    def test_missing_or_forged_or_stale_intent_blocks(self):
        for changes in (None, {"head": "c" * 40}, {"repository": "attacker/fork"},
                        {"run_id": 11}, {"origin_event": "pull_request"},
                        {"verified": False}, {"expired": True}):
            with self.subTest(changes=changes):
                self.intent = None if changes is None else initial_intent(
                    self.context, self.snapshot, POLICY) | changes
                if changes and "run_id" in changes:
                    self.intent["verified"] = False
                self.assertEqual(self.result(), "blocked")

    def test_head_race_blocks(self):
        self.snapshot["pr"]["head"] = "d" * 40
        self.assertEqual(self.result(), "blocked")

    def test_reviews_and_threads_are_current(self):
        self.snapshot["reviews"][0]["commit"] = "c" * 40
        self.assertEqual(self.result(), "waiting")
        self.snapshot["reviews"] = fixtures()[1]["reviews"]
        self.snapshot["threads"] = [{"resolved": False}]
        self.assertEqual(self.result(), "waiting")

    def test_labels_author_and_forks_are_refreshed(self):
        for changes in ({"labels": ["deps-no-automerge"]}, {"labels": ["deps-major"]},
                        {"author": "someone"}, {"source_repository": "attacker/fork"},
                        {"draft": True}, {"head_ref": "feature/normal"}):
            with self.subTest(changes=changes):
                self.snapshot["pr"] = fixtures()[1]["pr"] | changes
                self.assertEqual(self.result(), "blocked")

    def test_initial_only_trusted_actor_and_matching_event_head(self):
        for changes in ({"actor": "human"}, {"event": "workflow_run"},
                        {"action": "labeled"}, {"event_head": "c" * 40}):
            with self.subTest(changes=changes), self.assertRaises(Blocked):
                initial_intent(self.context | changes, self.snapshot, POLICY)

    def test_changed_policy_invalidates_old_intent(self):
        changed = copy.deepcopy(POLICY)
        changed["minimum_approvals"] = 3
        self.assertEqual(decide(self.context, self.snapshot, changed, self.intent)[0], "blocked")

    def test_safe_dependabot_requires_recorded_metadata(self):
        safe = POLICY | {"safe_updates_only": True}
        self.snapshot["pr"]["author"] = "dependabot[bot]"
        context = self.context | {"actor": "dependabot[bot]"}
        with self.assertRaises(Blocked):
            initial_intent(context, self.snapshot, safe)
        context["update_type"] = "version-update:semver-patch"
        self.assertTrue(initial_intent(context, self.snapshot, safe)["safe_update"])


if __name__ == "__main__":
    unittest.main()
