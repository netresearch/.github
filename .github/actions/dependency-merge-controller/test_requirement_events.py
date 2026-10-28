"""Each required context keeps its own event-filtered workflow selection."""
import copy
import unittest

from policy import decide, initial_intent
from test_policy import POLICY, REPO, SHA, fixtures


MERGE = "c" * 40
PR_GATE = "Shared CI / Pull request gate"
MERGE_GATE = "Shared CI / Merge group gate"


def scenario(reverse=False):
    context, snapshot = fixtures()
    policy = copy.deepcopy(POLICY)
    requirements = [
        {"name": PR_GATE, "app_id": 15368,
         "workflow": "shared.yml", "events": ["pull_request"]},
        {"name": MERGE_GATE, "app_id": 15368,
         "workflow": "shared.yml", "events": ["merge_group"]},
    ]
    policy["required_checks"] = list(reversed(requirements)) if reverse else requirements
    snapshot["pr"]["test_merge"] = MERGE
    snapshot["workflows"] = [
        {"workflow": "shared.yml", "event": "pull_request", "repository": REPO,
         "sha": SHA, "run_id": 20, "status": "completed", "conclusion": "success"},
        {"workflow": "shared.yml", "event": "merge_group", "repository": REPO,
         "sha": MERGE, "run_id": 21, "status": "completed", "conclusion": "success"},
    ]
    base = snapshot["checks"][0]
    snapshot["checks"] = [
        base | {"id": 100, "name": PR_GATE, "workflow": "shared.yml",
                "event": "pull_request", "run_id": 20, "sha": SHA},
        base | {"id": 101, "name": MERGE_GATE, "workflow": "shared.yml",
                "event": "merge_group", "run_id": 21, "sha": MERGE},
    ]
    snapshot["rules"][1]["parameters"]["required_status_checks"] = [
        {"context": requirement["name"], "integration_id": 15368}
        for requirement in requirements
    ]
    intent = initial_intent(context, snapshot, policy)
    context = context | {"event": "workflow_run", "actor": "human", "run_id": 10,
                         "caller_path": ".github/workflows/dependency-merge-controller.yml"}
    return context, snapshot, policy, intent


class RequirementEventsTest(unittest.TestCase):
    def test_same_workflow_disjoint_event_requirements_are_ready_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                context, snapshot, policy, intent = scenario(reverse)
                state, reason = decide(context, snapshot, policy, intent)
                self.assertEqual(state, "ready", reason)

    def test_check_cannot_borrow_other_event_producer_run(self):
        for reverse in (False, True):
            for index in (0, 1):
                with self.subTest(reverse=reverse, check=index):
                    context, snapshot, policy, intent = scenario(reverse)
                    snapshot["checks"][index]["run_id"] = snapshot["checks"][1 - index]["run_id"]
                    state, reason = decide(context, snapshot, policy, intent)
                    self.assertEqual(state, "blocked", reason)

    def test_check_event_must_match_its_own_requirement(self):
        for reverse in (False, True):
            for index in (0, 1):
                with self.subTest(reverse=reverse, check=index):
                    context, snapshot, policy, intent = scenario(reverse)
                    snapshot["checks"][index]["event"] = snapshot["checks"][1 - index]["event"]
                    state, reason = decide(context, snapshot, policy, intent)
                    self.assertEqual(state, "blocked", reason)

    def test_check_head_must_match_its_selected_event_producer(self):
        for reverse in (False, True):
            for index in (0, 1):
                with self.subTest(reverse=reverse, check=index):
                    context, snapshot, policy, intent = scenario(reverse)
                    snapshot["checks"][index]["sha"] = snapshot["checks"][1 - index]["sha"]
                    state, reason = decide(context, snapshot, policy, intent)
                    self.assertEqual(state, "blocked", reason)


if __name__ == "__main__":
    unittest.main()
