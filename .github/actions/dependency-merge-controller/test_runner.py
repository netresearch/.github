"""Controller/API seam fixtures. Every external operation is a local stub."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest import mock

from policy import initial_intent
from runner import ApiError, GitHub, evaluate_and_enqueue, main, verify_intent_record, write_receipt
from test_policy import POLICY, REPO, SHA, TIME, fixtures


class FakeAPI:
    def __init__(self, snapshots):
        self.repository = REPO
        self.snapshots = list(snapshots)
        self.mutations = []
        self.observe_only = False

    def snapshot(self, number):
        return copy.deepcopy(self.snapshots.pop(0))

    def enqueue(self, pr_id, head):
        self.mutations.append((pr_id, head))


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.context, self.snapshot = fixtures()
        self.intent = initial_intent(self.context, self.snapshot, POLICY)

    def test_receipt_never_uses_context_or_phase_as_a_path(self):
        class ReadOnlyAPI:
            observe_only = True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(root / "summary")}
            with mock.patch.dict(os.environ, environment), mock.patch("runner.output") as emitted:
                write_receipt(ReadOnlyAPI(), {"run_id": "../../escaped"}, POLICY,
                              "../../outside", None, "blocked", "fixture")
            receipt = Path(emitted.call_args.kwargs["receipt_path"])
            self.assertEqual(receipt.parent, root)
            self.assertEqual(receipt.name, "dependency-controller-receipt.json")
            self.assertEqual(json.loads(receipt.read_text())["phase"], "../../outside")

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
                       "READ_TOKEN": "stub", "GITHUB_EVENT_PATH": str(event), "RUNNER_TEMP": directory,
                       "OBSERVE_ONLY": "false", "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary.md")}
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
        artifact = {"id": 12, "name": f"merge-intent-pr-42-{SHA}", "expired": False,
                    "workflow_run": {"id": 9}, "created_at": TIME,
                    "expires_at": "2026-11-04T10:00:00Z"}
        run = {"id": 9, "event": "pull_request_target", "actor": {"login": "renovate[bot]"},
               "head_sha": SHA, "head_branch": "renovate/phpunit",
               "path": self.context["intent_path"], "status": "completed",
               "conclusion": "success", "repository": {"full_name": REPO},
               "head_repository": {"full_name": REPO}, "created_at": TIME}
        return artifact, run

    def test_exact_server_metadata_verifies_intent(self):
        artifact, run = self.provenance()
        context = self.context | {"run_id": 10, "caller_path": ".github/workflows/auto-merge-deps.yml"}
        self.assertTrue(verify_intent_record(self.intent, artifact, run, context)["verified"])

    def test_same_run_cannot_supply_its_own_intent_even_if_marked_completed(self):
        artifact, run = self.provenance()
        with self.assertRaises(ApiError):
            verify_intent_record(self.intent, artifact, run, self.context)

    def test_spoofed_origin_or_expired_artifact_blocks(self):
        variants = [("artifact", "expired", True), ("artifact", "workflow_run", {"id": 10}),
                    ("artifact", "name", "fake"), ("run", "path", ".github/workflows/fake.yml"),
                    ("run", "event", "pull_request"), ("run", "actor", {"login": "attacker"}),
                    ("run", "head_sha", "c" * 40), ("run", "head_branch", "main"),
                    ("run", "repository", {"full_name": "attacker/fork"}),
                    ("run", "head_repository", {"full_name": "attacker/fork"})]
        for where, key, value in variants:
            artifact, run = self.provenance()
            with self.subTest(where=where, key=key), self.assertRaises(ApiError):
                (artifact if where == "artifact" else run)[key] = value
                verify_intent_record(self.intent, artifact, run, self.context | {"run_id": 10})

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

    def test_workflow_paths_normalize_only_the_documented_ref_suffix(self):
        client = GitHub(REPO, "stub")
        for path in (".github/workflows/auto-merge-deps.yml",
                     ".github/workflows/auto-merge-deps.yml@main",
                     ".github/workflows/auto-merge-deps.yml@refs/heads/renovate/test",
                     ".github/workflows/auto-merge-deps.yml@refs/heads/renovate/pkg@1",
                     ".github/workflows/auto-merge-deps.yml@refs/heads/feature#part"):
            client.runs = {}
            client.rest = lambda endpoint: ({"path": path}, {})
            self.assertEqual(client.run(9)["path"], ".github/workflows/auto-merge-deps.yml")
        for path in (None, "../auto-merge-deps.yml", ".github/workflows/fake.yml@",
                     ".github/workflows/fake.yml@main\ninvalid", ".github/workflows/fake.yml@main\n@other"):
            client.runs = {}
            client.rest = lambda endpoint: ({"path": path}, {})
            with self.subTest(path=path), self.assertRaises(ApiError):
                client.run(9)

    def test_malformed_run_ids_never_reach_authenticated_request(self):
        api = GitHub(REPO, "stub")
        for run_id in ("١", "1٢", "１", "../artifacts", "%2e%2e/artifacts", "123?per_page=1",
                       "123#ignored", True, 0, -1, "12/34", "1\n"):
            with self.subTest(run_id=run_id), mock.patch.object(api, "rest") as request:
                with self.assertRaises(ApiError):
                    api.run(run_id)
                request.assert_not_called()

    def test_central_authenticated_url_is_repo_and_endpoint_bound(self):
        api = GitHub(REPO, "stub")
        root = f"/repos/{REPO}"
        unsafe = ("https://attacker.invalid/", "//attacker.invalid/", "/repos/attacker/fork/actions/runs/1",
                  root + "/actions/runs/../artifacts", root + "/actions/runs/%2e%2e/artifacts",
                  root + "/actions/runs/%252e%252e/artifacts", root + "/actions/runs/1?bad=1",
                  root + "/actions/runs/1#ignored", root + "/actions/runs/1\r\nX: bad",
                  root + "/actions/runs/1#", root + "/pulls?unpaired",
                  root + "/actions/artifacts?name=ok&name=other", root + "/pulls?state=open&head=attacker%3Abr",
                  root + "/rules/branches/%2e%2e", root + "/rules/branches/main%2F..%2Fother")
        for path in unsafe:
            with self.subTest(path=path), self.assertRaises(ApiError):
                api.api_url(path)

    def test_legitimate_endpoints_and_encoded_git_refs_remain_usable(self):
        api = GitHub(REPO, "stub")
        root = f"/repos/{REPO}"
        paths = [root + "/actions/runs/123", root + f"/commits/{SHA}/check-runs?filter=all&per_page=100&page=2",
                 root + "/actions/workflows/ci.yml/runs?head_sha=" + SHA,
                 root + f"/actions/artifacts?name=merge-intent-pr-42-{SHA}",
                 root + "/pulls?state=open&head=netresearch%3Arenovate%2Fa%26b%3Dc"]
        for branch in ("release%2F2026.10", "release%252F2026", "feature%23part", "feature%252e%252e%2Fname"):
            paths.append(root + "/rules/branches/" + branch)
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(api.api_url(path).startswith("https://api.github.com/repos/" + REPO + "/"))
        self.assertEqual(api.api_url("/graphql", "POST"), "https://api.github.com/graphql")

    def test_authenticated_request_rejects_endpoint_escape_before_sink(self):
        api = GitHub(REPO, "stub")
        with mock.patch("runner.urllib.request.Request") as sink:
            with self.assertRaises(ApiError):
                api.request(f"/repos/{REPO}/actions/runs/../artifacts")
            sink.assert_not_called()

    def test_artifact_redirect_does_not_forward_api_credentials(self):
        api = GitHub(REPO, "stub")
        redirect = urllib.error.HTTPError("url", 302, "redirect", {"Location": "https://blob.test/artifact"}, None)
        opener = mock.Mock()
        opener.open.side_effect = redirect
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"zip"
        response.__enter__.return_value.headers = {}
        with mock.patch("runner.urllib.request.build_opener", return_value=opener), \
                mock.patch("runner.urllib.request.urlopen", return_value=response) as download:
            self.assertEqual(api.request(f"/repos/{REPO}/actions/artifacts/12/zip", binary=True)[0], b"zip")
            request = download.call_args.args[0]
            self.assertNotIn("Authorization", request.headers)
            self.assertEqual(request.full_url, "https://blob.test/artifact")
        redirect.close()


if __name__ == "__main__":
    unittest.main()
