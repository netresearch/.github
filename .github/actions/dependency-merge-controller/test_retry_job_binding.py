"""Actual failed-job retry evidence through the real offline HTTP adapter."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest import mock
import urllib.parse

from runner import ApiError, GitHub
from test_api_economy import Response


FIXTURE = Path(__file__).with_name("fixtures") / "producer-retry.json"


class RetryJobServer:
    def __init__(self, fixture):
        self.fixture = copy.deepcopy(fixture)
        self.repository = fixture["provenance"]["repository"]
        self.run = fixture["run_after"]
        self.calls = []

    def open(self, request, **_kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        path = f"/repos/{self.repository}/actions/runs/{self.run['id']}/jobs"
        view = query.get("filter", [None])[0]
        if (request.get_method() != "GET" or parsed.scheme != "https"
                or parsed.netloc != "api.github.com" or parsed.path != path
                or query != {"filter": [view], "per_page": ["100"], "page": ["1"]}
                or view not in {"latest", "all"}):
            raise AssertionError("Unexpected offline fixture request: " + request.full_url)
        self.calls.append(view)
        return Response(self.fixture[view + "_jobs"])

    def transport(self):
        return mock.patch("runner.urllib.request.build_opener", return_value=self)

    def client(self):
        with mock.patch.dict(os.environ, {"GITHUB_API_URL": "https://api.github.com"}):
            api = GitHub(self.repository, "synthetic-token", observe_only=True)
        run = api.run_metadata(copy.deepcopy(self.run), expected_id=self.run["id"],
                               expected_sha=self.run["head_sha"])
        api.runs[run["id"]] = run
        return api, {"head": run["head_sha"], "test_merge": None}


class RetryJobBindingTest(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.current_ids = {job["id"] for job in self.fixture["latest_jobs"]["jobs"]}

    def current_check(self, fixture=None):
        fixture = fixture or self.fixture
        return next(check for check in fixture["check_runs"]["check_runs"]
                    if check["id"] in self.current_ids)

    def assert_rejected(self, fixture, check, reads):
        server = RetryJobServer(fixture)
        api, pr = server.client()
        with server.transport(), self.assertRaises(ApiError):
            api.normalize_check(check, pr, expected_sha=pr["head"])
        self.assertEqual(api.api_calls, reads)
        self.assertEqual(api.mutation_attempts, 0)
        return server

    def test_all_actual_checks_bind_with_two_cached_inventory_reads(self):
        server = RetryJobServer(self.fixture)
        api, pr = server.client()
        checks = self.fixture["check_runs"]["check_runs"]
        self.assertEqual(len(checks), 24)
        self.assertTrue(all("run_attempt" not in check for check in checks))
        with server.transport():
            normalized = [api.normalize_check(check, pr, expected_sha=pr["head"])
                          for check in checks]
            repeated = [api.normalize_check(check, pr, expected_sha=pr["head"])
                        for check in checks]
        self.assertEqual(normalized, repeated)
        self.assertTrue(all(check["job_verified"] for check in normalized))
        current = [check for check in normalized if check["job_current"]]
        historical = [check for check in normalized if not check["job_current"]]
        self.assertEqual((len(current), len(historical)), (12, 12))
        self.assertEqual({check["id"] for check in current}, self.current_ids)
        self.assertEqual({check["attempt"] for check in current}, {2})
        self.assertEqual({check["attempt"] for check in historical}, {1})
        self.assertEqual(server.calls, ["latest", "all"])
        self.assertEqual(api.api_calls, 2)
        self.assertEqual(api.mutation_attempts, 0)

    def test_eleven_cloned_successes_keep_pre_retry_execution_times(self):
        server = RetryJobServer(self.fixture)
        api, pr = server.client()
        checks = {check["id"]: check for check in self.fixture["check_runs"]["check_runs"]}
        jobs = {job["id"]: job for job in self.fixture["all_jobs"]["jobs"]}
        pairs = self.fixture["preserved_success_pairs"]
        self.assertEqual(len(pairs), 11)
        with server.transport():
            for pair in pairs:
                with self.subTest(name=pair["name"]):
                    before, after = jobs[pair["before_job_id"]], jobs[pair["after_job_id"]]
                    self.assertNotEqual(before["id"], after["id"])
                    self.assertEqual((before["started_at"], before["completed_at"]),
                                     (after["started_at"], after["completed_at"]))
                    self.assertLess(after["completed_at"], server.run["run_started_at"])
                    self.assertGreater(after["created_at"], after["completed_at"])
                    result = api.normalize_check(checks[after["id"]], pr, expected_sha=pr["head"])
                    self.assertTrue(result["job_verified"] and result["job_current"])
                    self.assertEqual(result["attempt"], 2)
        self.assertEqual(server.calls, ["latest"])
        self.assertEqual(api.api_calls, 1)

    def test_invalid_suite_or_exact_head_is_rejected_before_job_reads(self):
        for suite in (None, True, "110282175671", 0, -1, 110282175672):
            fixture = copy.deepcopy(self.fixture)
            check = self.current_check(fixture)
            check["check_suite"]["id"] = suite
            with self.subTest(suite=suite):
                self.assert_rejected(fixture, check, 0)
        fixture = copy.deepcopy(self.fixture)
        check = self.current_check(fixture)
        check["head_sha"] = "a" * 40
        self.assert_rejected(fixture, check, 0)

    def test_copied_name_suite_head_and_link_cannot_supply_missing_check_membership(self):
        fixture = copy.deepcopy(self.fixture)
        check = self.current_check(fixture)
        check["id"] = 999999999999
        server = self.assert_rejected(fixture, check, 2)
        self.assertEqual(server.calls, ["latest", "all"])

    def test_canonical_server_check_url_and_job_source_identity_are_required(self):
        changes = {
            "foreign check repository": {"check_run_url": "https://api.github.com/repos/other/repo/check-runs/1"},
            "noncanonical check URL": {"check_run_url": self.fixture["latest_jobs"]["jobs"][0]["check_run_url"] + "?x=1"},
            "wrong run": {"run_id": self.fixture["run_after"]["id"] + 1},
            "foreign run URL": {"run_url": "https://api.github.com/repos/other/repo/actions/runs/1"},
            "wrong job head": {"head_sha": "a" * 40},
            "future job attempt": {"run_attempt": 3},
            "wrong native name": {"name": "copied check name has no native job"},
        }
        for name, fields in changes.items():
            fixture = copy.deepcopy(self.fixture)
            job = fixture["latest_jobs"]["jobs"][0]
            check = next(check for check in fixture["check_runs"]["check_runs"]
                         if check["id"] == job["id"])
            job.update(fields)
            with self.subTest(name=name):
                self.assert_rejected(fixture, check, 1)

    def test_linked_job_suffix_must_match_the_actual_native_job(self):
        fixture = copy.deepcopy(self.fixture)
        check = self.current_check(fixture)
        other = next(job for job in fixture["latest_jobs"]["jobs"] if job["id"] != check["id"])
        check["details_url"] = other["html_url"]
        self.assert_rejected(fixture, check, 1)

    def test_check_url_join_does_not_assume_job_and_check_numeric_ids_match(self):
        fixture = copy.deepcopy(self.fixture)
        check = self.current_check(fixture)
        job = next(job for job in fixture["latest_jobs"]["jobs"] if job["id"] == check["id"])
        check["details_url"] = check["details_url"].rsplit("/", 1)[0] + "/999999999999"
        for view in ("latest_jobs", "all_jobs"):
            native = next(row for row in fixture[view]["jobs"]
                          if row["check_run_url"] == job["check_run_url"])
            native.update(id=999999999999, html_url=check["details_url"])
        server = RetryJobServer(fixture)
        api, pr = server.client()
        with server.transport():
            result = api.normalize_check(check, pr, expected_sha=pr["head"])
        self.assertTrue(result["job_verified"] and result["job_current"])
        self.assertEqual(result["id"], check["id"])
        self.assertEqual(api.api_calls, 1)

    def test_current_pending_or_failure_cannot_fall_back_to_an_old_success(self):
        for status, conclusion in (("in_progress", None), ("completed", "failure")):
            fixture = copy.deepcopy(self.fixture)
            check = self.current_check(fixture)
            job = next(job for job in fixture["latest_jobs"]["jobs"] if job["id"] == check["id"])
            old = [row for row in fixture["all_jobs"]["jobs"]
                   if row["name"] == job["name"] and row["run_attempt"] == 1]
            self.assertEqual([row["conclusion"] for row in old], ["success"])
            job.update(status=status, conclusion=conclusion)
            with self.subTest(status=status, conclusion=conclusion):
                server = self.assert_rejected(fixture, check, 1)
                self.assertEqual(server.calls, ["latest"])

    def test_historical_join_rejects_disagreement_with_latest_inventory(self):
        fixture = copy.deepcopy(self.fixture)
        check = next(check for check in fixture["check_runs"]["check_runs"]
                     if check["id"] not in self.current_ids)
        current_id = fixture["latest_jobs"]["jobs"][0]["id"]
        next(job for job in fixture["all_jobs"]["jobs"]
             if job["id"] == current_id)["name"] = "different all-view native job"
        server = self.assert_rejected(fixture, check, 2)
        self.assertEqual(server.calls, ["latest", "all"])

    def test_duplicate_job_or_check_mapping_blocks_the_inventory(self):
        for field in ("id", "check_run_url"):
            fixture = copy.deepcopy(self.fixture)
            first, second = fixture["latest_jobs"]["jobs"][:2]
            second[field] = first[field]
            with self.subTest(field=field):
                self.assert_rejected(fixture, self.current_check(fixture), 1)


if __name__ == "__main__":
    unittest.main()
