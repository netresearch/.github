"""Bounded safe diagnostics from existing API responses, without new requests."""

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest import mock

import runner
from test_applicability import PathServer
from test_policy import POLICY, REPO, SHA, fixtures


READ_TOKEN = "read-role-secret"
WRITE_TOKEN = "write-role-secret"
READ_PATH = f"/repos/{REPO}/actions/runs/9"
FIELD = "api_rate_limits"
RESOURCE_HEADER = "x-ratelimit-resource"
LIMIT_HEADER = "x-ratelimit-limit"
REMAINING_HEADER = "x-ratelimit-remaining"
USED_HEADER = "x-ratelimit-used"
RESET_HEADER = "x-ratelimit-reset"
NUMBERS = ("limit", "remaining", "used", "reset")


class ByteResponse(io.BytesIO):
    """Keep original response bodies and headers available at the HTTP boundary."""

    def __init__(self, headers, body=b'{"ok":true}'):
        """Return arbitrary response bytes without interpreting diagnostic data."""
        super().__init__(body)
        self.headers = headers


def client(version=2, phase="evaluate"):
    """Use valid existing policies and explicit credential roles in real adapter."""
    policy = PathServer().policy if version == 2 else POLICY
    return runner.GitHub(REPO, READ_TOKEN, WRITE_TOKEN, policy, phase=phase)


def transport(response=None, error=None):
    """Stub only the authenticated network opener and retain request evidence."""
    opener = mock.Mock()
    if error is not None:
        opener.open.side_effect = error
    else:
        opener.open.return_value = response
    return opener, mock.patch("runner.urllib.request.build_opener", return_value=opener)


def receipt(api):
    """Read actual serialized phase diagnostics without a live origin or write."""
    context, _ = fixtures()
    with tempfile.TemporaryDirectory() as directory:
        environment = {"RUNNER_TEMP": directory, "GITHUB_OUTPUT": str(Path(directory) / "out"),
                       "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary")}
        with mock.patch.dict(os.environ, environment), mock.patch("builtins.print"):
            runner.write_receipt(api, context, api.policy, "evaluate", None, "blocked", "fixture")
        return json.loads((Path(directory) / "dependency-controller-receipt.json").read_text())


def sample(role, resource, ordinal, **numbers):
    """Describe the fixed safe projection, preserving unknown separately from zero."""
    return {"credential_kind": role, "resource": resource, "api_call": ordinal,
            **{field: numbers.get(field) for field in NUMBERS}}


class RateLimitReceiptTest(unittest.TestCase):
    """Instrumentation must never change the request, result or policy decision."""

    def test_case_insensitive_headers_keep_only_exact_safe_numeric_fields(self):
        """Header case is flexible while unknown/secret values never escape."""
        api = client()
        headers = {"X-RateLimit-Resource": "core", "X-RateLimit-Limit": "1000",
                   "x-RATELIMIT-remaining": "0", USED_HEADER: "28", RESET_HEADER: "1793458800",
                   "Authorization": READ_TOKEN, "Set-Cookie": WRITE_TOKEN, "X-Secret": "unrelated-secret"}
        opener, patcher = transport(ByteResponse(headers))
        with patcher:
            result = api.request(READ_PATH)
        self.assertEqual(result, ({"ok": True}, headers))
        self.assertEqual(opener.open.call_count, 1)
        rows = receipt(api).get(FIELD)
        self.assertEqual(rows, [sample("read", "core", 1, limit=1000, remaining=0, used=28, reset=1793458800)])
        self.assertNotIn("secret", json.dumps(rows))
        self.assertEqual(api.api_calls, 1)

    def test_numeric_zero_bounds_and_malformed_fields_are_not_conflated(self):
        """Only fitting ASCII decimal values survive, including leading zeros."""
        cases = [("0", 0), ("0000", 0), ("0" * 5000 + "7", 7), (str(2**63 - 1), 2**63 - 1),
                 (str(2**63), None), ("", None), ("-1", None), ("+1", None), (" 1", None),
                 ("1 ", None), ("1.0", None), ("１２", None), ("١", None), (None, None), (1, None), (True, None)]
        for value, expected in cases:
            api = client()
            headers = {RESOURCE_HEADER: "core", **{"x-ratelimit-" + field: value for field in NUMBERS}}
            _, patcher = transport(ByteResponse(headers))
            with patcher:
                api.request(READ_PATH)
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(receipt(api).get(FIELD), [sample("read", "core", 1, **{field: expected for field in NUMBERS})])

    def test_unknown_and_no_header_responses_are_visible_without_raw_values(self):
        """Received but unmeasured responses produce null, never invented zeros."""
        for headers in ({}, {RESOURCE_HEADER: "private-secret-resource"}, {RESOURCE_HEADER: "CORE"},
                        {None: "ignored-secret", RESOURCE_HEADER: None}, {RESOURCE_HEADER: []},
                        {RESOURCE_HEADER: {}}, {RESOURCE_HEADER: True}, {RESOURCE_HEADER: 42}):
            api = client()
            _, patcher = transport(ByteResponse(headers))
            with patcher:
                api.request(READ_PATH)
            with self.subTest(headers=headers):
                self.assertEqual(receipt(api).get(FIELD), [sample("read", None, 1)])

    def test_last_samples_replace_numbers_and_windows_with_a_bound_of_six(self):
        """Roles/resources stay distinct and newer nulls replace stale numbers."""
        api = client()
        for write in (False, True):
            for resource in (None, "core", "graphql"):
                headers = {RESOURCE_HEADER: resource, REMAINING_HEADER: "99", RESET_HEADER: "100"}
                _, patcher = transport(ByteResponse(headers))
                with patcher:
                    api.request(READ_PATH, write=write)
        _, patcher = transport(ByteResponse({RESOURCE_HEADER: "core", RESET_HEADER: "200"}))
        with patcher:
            api.request(READ_PATH)
        self.assertEqual(receipt(api).get(FIELD), [
            sample("read", None, 1, remaining=99, reset=100), sample("read", "core", 7, reset=200),
            sample("read", "graphql", 3, remaining=99, reset=100), sample("write", None, 4, remaining=99, reset=100),
            sample("write", "core", 5, remaining=99, reset=100), sample("write", "graphql", 6, remaining=99, reset=100)])
        self.assertEqual(api.api_calls, 7)

    def test_graphql_and_queue_write_use_existing_roles_without_token_disclosure(self):
        """The role reports the current write flag, not an independent quota bucket."""
        api = client()
        headers = {RESOURCE_HEADER: "graphql", USED_HEADER: "8"}
        opener, patcher = transport(ByteResponse(headers, b'{"data":{"ok":true}}'))
        opener.open.side_effect = [ByteResponse(headers, b'{"data":{"ok":true}}'),
                                   ByteResponse(headers, b'{"data":{"ok":true}}')]
        with patcher:
            api.request("/graphql", "POST", {"query": "query{viewer{login}}", "variables": {}})
            api.enqueue("PR_42", SHA)
        self.assertEqual(receipt(api).get(FIELD), [sample("read", "graphql", 1, used=8), sample("write", "graphql", 2, used=8)])
        self.assertEqual([call.args[0].get_header("Authorization") for call in opener.open.call_args_list],
                         ["Bearer " + READ_TOKEN, "Bearer " + WRITE_TOKEN])
        self.assertEqual(api.api_calls, 2)

    def test_prepare_approval_still_uses_read_role(self):
        """A permitted approval POST must not be mislabeled as write-token traffic."""
        api = client(phase="prepare")
        _, patcher = transport(ByteResponse({RESOURCE_HEADER: "core", REMAINING_HEADER: "0"}))
        with patcher:
            api.request(f"/repos/{REPO}/pulls/42/reviews", "POST", {"event": "APPROVE", "commit_id": SHA})
        self.assertEqual(receipt(api).get(FIELD), [sample("read", "core", 1, remaining=0)])

    def test_http_error_preserves_failure_and_captures_original_response(self):
        """An error response is sampled before the existing exception is raised."""
        api = client()
        headers = {RESOURCE_HEADER: "core", REMAINING_HEADER: "0", RESET_HEADER: "50"}
        failure = urllib.error.HTTPError("https://api.github.com" + READ_PATH, 403, "denied", headers, None)
        opener, patcher = transport(error=failure)
        with patcher, self.assertRaisesRegex(runner.ApiError, "failed \\(403\\)"):
            api.request(READ_PATH)
        self.assertEqual(receipt(api).get(FIELD), [sample("read", "core", 1, remaining=0, reset=50)])
        self.assertEqual(opener.open.call_count, 1)

    def test_connection_failure_has_no_sample_and_body_failure_keeps_received_headers(self):
        """Only an actually received API response may supply a diagnostic row."""
        api = client()
        _, patcher = transport(error=urllib.error.URLError("offline"))
        with patcher, self.assertRaisesRegex(runner.ApiError, "unavailable"):
            api.request(READ_PATH)
        self.assertEqual(receipt(api).get(FIELD), [])
        response = ByteResponse({RESOURCE_HEADER: "core", LIMIT_HEADER: "42"})
        response.read = mock.Mock(side_effect=urllib.error.URLError("body lost"))
        _, patcher = transport(response)
        with patcher, self.assertRaisesRegex(runner.ApiError, "unavailable"):
            api.request(READ_PATH)
        self.assertEqual(receipt(api).get(FIELD), [sample("read", "core", 2, limit=42)])

    def test_artifact_redirect_keeps_api_headers_and_never_forwards_token(self):
        """Signed-storage headers cannot replace the authenticated GitHub302 sample."""
        api = client()
        path = f"/repos/{REPO}/actions/artifacts/12/zip"
        location = "https://storage.example/blob?signature=private-secret"
        headers = {"Location": location, RESOURCE_HEADER: "core", REMAINING_HEADER: "7"}
        failure = urllib.error.HTTPError("https://api.github.com" + path, 302, "redirect", headers, None)
        opener, patcher = transport(error=failure)
        storage_headers = {RESOURCE_HEADER: "graphql", REMAINING_HEADER: "100", "Set-Cookie": WRITE_TOKEN}
        with patcher, mock.patch("runner.urllib.request.urlopen", return_value=ByteResponse(storage_headers, b"binary")) as storage:
            result = api.request(path, binary=True)
        self.assertEqual(result, (b"binary", storage_headers))
        self.assertEqual(receipt(api).get(FIELD), [sample("read", "core", 1, remaining=7)])
        self.assertIsNone(storage.call_args.args[0].get_header("Authorization"))
        self.assertEqual((opener.open.call_count, storage.call_count, api.api_calls), (1, 1, 1))

    def test_v1_receipt_and_parser_behavior_remain_untouched(self):
        """The new diagnostic field must be absent from all original v1 receipts."""
        api = client(version=1)
        headers = {RESOURCE_HEADER: "core", REMAINING_HEADER: "123"}
        _, patcher = transport(ByteResponse(headers))
        with patcher, mock.patch("runner.rate_limit_sample", wraps=runner.rate_limit_sample) as parser:
            self.assertEqual(api.request(READ_PATH), ({"ok": True}, headers))
        parser.assert_not_called()
        self.assertNotIn(FIELD, receipt(api))
        self.assertEqual(api.api_calls, 1)

    def test_header_values_cannot_change_policy_evidence_or_readiness(self):
        """Zero remaining quota is diagnostic data, never a policy input."""
        server = PathServer()
        api = server.client()
        expected_policy = copy.deepcopy(api.policy)
        context, _ = fixtures()
        context["observe_only"] = True
        original = server.open

        def with_headers(request, **options):
            """Add headers to the unchanged actual transport response bodies."""
            response = original(request, **options)
            response.headers = {RESOURCE_HEADER: "graphql", REMAINING_HEADER: "0", USED_HEADER: "999"}
            return response

        with server.transport(), mock.patch.object(server, "open", side_effect=with_headers):
            before = api.snapshot(42)
            intent = runner.initial_intent(context, before, server.policy)
            state, _ = runner.evaluate_and_enqueue(api, context, server.policy, intent)
        self.assertEqual(api.policy, expected_policy)
        self.assertEqual(state, "observed-ready")
        self.assertEqual(before["applicability"]["policy_digest"], runner.digest(expected_policy))
        self.assertEqual(api.last_snapshot, before)
        self.assertEqual(api.api_calls, 60)
        self.assertEqual(api.mutation_attempts, 0)

    def test_samples_persist_across_full_fresh_snapshots_without_affecting_evidence(self):
        """Snapshot cache resets cannot erase earlier resource samples or add reads."""
        server = PathServer()
        api = server.client()
        _, patcher = transport(ByteResponse({RESOURCE_HEADER: "core", REMAINING_HEADER: "88"}))
        with patcher:
            api.request(READ_PATH)
        with server.transport():
            first = api.snapshot(42)
            second = api.snapshot(42)
        self.assertEqual(first, second)
        self.assertEqual(api.api_calls, 41)
        self.assertEqual(receipt(api).get(FIELD), [sample("read", None, 41), sample("read", "core", 1, remaining=88)])


if __name__ == "__main__":
    unittest.main()
