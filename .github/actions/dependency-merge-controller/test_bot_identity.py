"""Preserve typed native GraphQL bot identity without widening trusted actors."""

import copy
import json
import re
import unittest
from unittest import mock

from api_target import ApiError
from policy import BOTS, Blocked, initial_intent
from test_api_economy import Server
from test_applicability import PathServer
from test_policy import fixtures


# Actual paired GraphQL/REST PR1197, head cf62b6a86194dab32e2372a1284cb2b7587650c2:
# GraphQL Bot/renovate, REST Bot/renovate[bot]/29139614. Only author data is replayed.
NATIVE_AUTHOR = {"__typename": "Bot", "login": "renovate", "url": "https://github.com/apps/renovate"}
CANONICAL_RENOVATE = "renovate[bot]"
CANONICAL_DEPENDABOT = "dependabot[bot]"


def collect(author, version=1):
    """Pass provider author data through the real HTTP snapshot boundary."""
    server = Server() if version == 1 else PathServer()
    server.raw["author"] = copy.deepcopy(author)
    api = server.client()
    with server.transport(), mock.patch.object(server, "open", wraps=server.open) as opened:
        snapshot = api.snapshot(42)
    queries = [json.loads(call.args[0].data)["query"] for call in opened.call_args_list
               if call.args[0].full_url.endswith("/graphql")]
    return snapshot, api, server, queries


class ProviderBotIdentityTest(unittest.TestCase):
    """Use the actual native representation and reject same-name User identities."""

    def test_native_graphql_author_reaches_canonical_policy_for_both_versions(self):
        """The real provider spelling must establish the intended bot eligibility."""
        for version, expected_reads in ((1, 18), (2, 20)):
            with self.subTest(version=version):
                snapshot, api, server, _ = collect(NATIVE_AUTHOR, version)
                context, _ = fixtures()
                self.assertEqual(snapshot["pr"]["author"], CANONICAL_RENOVATE)
                intent = initial_intent(context, snapshot, server.policy)
                self.assertEqual(intent["actor"], CANONICAL_RENOVATE)
                self.assertEqual(api.api_calls, expected_reads)
                self.assertEqual(api.mutation_attempts, 0)

    def test_all_four_exact_typed_bot_spellings_keep_the_same_allowlist(self):
        """Known typed aliases preserve canonical names without suffix inference."""
        expected = {"renovate": CANONICAL_RENOVATE, CANONICAL_RENOVATE: CANONICAL_RENOVATE,
                    "dependabot": CANONICAL_DEPENDABOT, CANONICAL_DEPENDABOT: CANONICAL_DEPENDABOT}
        self.assertEqual(BOTS, {CANONICAL_RENOVATE, CANONICAL_DEPENDABOT})
        for login, canonical in expected.items():
            with self.subTest(login=login):
                snapshot, _, server, _ = collect({"__typename": "Bot", "login": login})
                context, _ = fixtures()
                context["actor"] = canonical
                self.assertEqual(snapshot["pr"]["author"], canonical)
                self.assertEqual(initial_intent(context, snapshot, server.policy)["actor"], canonical)

    def test_snapshot_requests_pr_author_type_in_the_actual_query(self):
        """The fake transport's extra fields cannot mask a missing projection."""
        for version in (1, 2):
            with self.subTest(version=version):
                _, _, _, queries = collect(NATIVE_AUTHOR, version)
                primary = next(query for query in queries if "reviewDecision" in query)
                projection = re.search(r"headRefName\s+author\s*\{([^}]+)\}", primary)
                self.assertIsNotNone(projection)
                self.assertEqual(set(projection[1].split()), {"login", "__typename"})

    def test_users_including_canonical_names_cannot_impersonate_typed_bots(self):
        """User names never acquire bot authority through an alias or suffix."""
        for version in (1, 2):
            for login in ("renovate", "dependabot", CANONICAL_RENOVATE, CANONICAL_DEPENDABOT):
                with self.subTest(version=version, login=login), self.assertRaisesRegex(ApiError, "author"):
                    collect({"__typename": "User", "login": login}, version)

    def test_missing_malformed_and_unknown_provider_identities_fail_closed(self):
        """Neither a canonical login alone nor an unknown Bot may enter policy."""
        invalid = [None, [], "renovate", {}, {"login": CANONICAL_RENOVATE},
                   {"__typename": None, "login": CANONICAL_RENOVATE},
                   {"__typename": True, "login": CANONICAL_RENOVATE},
                   {"__typename": "Organization", "login": CANONICAL_RENOVATE},
                   {"__typename": "bot", "login": CANONICAL_RENOVATE}]
        invalid.extend({"__typename": "Bot", "login": login} for login in
                       (None, [], 1, "", "someone[bot]", "renovate-bot", "Renovate", "renovate ", " renovate"))
        for author in invalid:
            with self.subTest(author=author), self.assertRaisesRegex(ApiError, "author"):
                collect(author)

    def test_normalizing_pr_author_does_not_normalize_the_original_event_actor(self):
        """Only the original canonical trusted actor can create initial intent."""
        snapshot, _, server, _ = collect(NATIVE_AUTHOR)
        context, _ = fixtures()
        context["actor"] = "renovate"
        with self.assertRaisesRegex(Blocked, "trusted bot"):
            initial_intent(context, snapshot, server.policy)

    def test_dependabot_safe_update_rules_still_reject_a_major_update(self):
        """Canonical provider mapping cannot bypass the existing update policy."""
        snapshot, _, server, _ = collect({"__typename": "Bot", "login": "dependabot"})
        server.policy["safe_updates_only"] = True
        context, _ = fixtures()
        context.update(actor=CANONICAL_DEPENDABOT, update_type="version-update:semver-major")
        with self.assertRaisesRegex(Blocked, "permitted safe update"):
            initial_intent(context, snapshot, server.policy)


if __name__ == "__main__":
    unittest.main()
