"""Small GitHub API adapter. No checkout, polling, direct merge or PR execution."""

import datetime
import io
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from policy import (BOTS, Blocked, check_intent, completion_wakeups, decide,
                    declared_checks, digest, initial_intent, observer_api_attempt_limit, validate_policy)
from api_target import ApiError, positive_id, target_url
from job_binding import bind_check, json_id, run_revision
from changed_paths import (ANCHOR_FIELDS, anchor, closing_query, comparison, conditional_selection,
                           verify_closing, workflow_pins)

MAX_BYTES = 4 * 1024 * 1024
WORKFLOW_DIRECTORY = ".github/workflows/"
RENOVATE_BOT = "renovate[bot]"
DEPENDABOT_BOT = "dependabot[bot]"
HISTORY_TYPES = "ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT,AUTO_MERGE_ENABLED_EVENT,AUTO_MERGE_DISABLED_EVENT"
INITIAL_TARGET_ACTIONS = {"opened", "synchronize", "reopened"}
METADATA_ACTIONS = {"unlabeled", "ready_for_review", "edited", "auto_merge_enabled"}
RATE_LIMIT_FIELDS = ("limit", "remaining", "used", "reset")


def workflow_path(value):
    """Canonical file identity; the REST API may append an @ref suffix."""
    if not isinstance(value, str):
        raise ApiError("Missing workflow path")
    path, separator, ref = value.partition("@")
    if (not re.fullmatch(r"[.]github/workflows/[A-Za-z0-9_-]+[.]ya?ml", path)
            or (separator and (not ref or re.search(r"[\x00-\x20]", ref)))):
        raise ApiError("Invalid workflow path")
    return path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def rate_limit_number(value):
    """Keep fitting ASCII decimal headers numeric and every unknown value null."""
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value, flags=re.ASCII):
        return None
    digits = value.lstrip("0") or "0"
    if len(digits) > 19 or (len(digits) == 19 and digits > "9223372036854775807"):
        return None
    return int(digits)


def rate_limit_sample(headers, credential_kind, api_call):
    """Project only five safe headers, never arbitrary resources or credentials."""
    allowed = {"x-ratelimit-resource", *("x-ratelimit-" + field for field in RATE_LIMIT_FIELDS)}
    try:
        selected = {name.lower(): value for name, value in headers.items()
                    if isinstance(name, str) and name.lower() in allowed}
    except (AttributeError, TypeError, ValueError):
        selected = {}
    resource = selected.get("x-ratelimit-resource")
    if resource not in ("core", "graphql"):
        resource = None
    return {"credential_kind": credential_kind, "resource": resource, "api_call": api_call,
            **{field: rate_limit_number(selected.get("x-ratelimit-" + field)) for field in RATE_LIMIT_FIELDS}}


def rate_limit_rows(samples):
    """Emit at most six last samples with stable ordering including null resource."""
    return sorted(samples.values(), key=lambda row: (row["credential_kind"], row["resource"] or ""))


def configured_observer_limit(policy, observe_only):
    """Reject a configured ceiling outside observation before any controller API."""
    limit = observer_api_attempt_limit(policy)
    if limit is not None and observe_only is not True:
        raise ApiError("Observer API attempt limit requires observation mode")
    return limit


def api_attempt_budget_receipt(api):
    """Expose truthful configured usage and denial without changing default receipts."""
    limit = getattr(api, "observer_api_attempt_limit", None)
    if limit is None:
        return {}
    return {"api_attempt_budget": {"limit": limit, "used": api.api_calls,
                                   "exhausted": api.api_attempt_budget_exhausted}}


def graphql_bot_author(author):
    """Normalize only GitHub's typed, exact aliases for the two approved bots."""
    aliases = {"renovate": RENOVATE_BOT, RENOVATE_BOT: RENOVATE_BOT,
               "dependabot": DEPENDABOT_BOT, DEPENDABOT_BOT: DEPENDABOT_BOT}
    if not isinstance(author, dict) or author.get("__typename") != "Bot":
        raise ApiError("PR author must be a typed recognized Bot")
    login = author.get("login")
    if not isinstance(login, str) or login not in aliases:
        raise ApiError("PR author is not a recognized Bot identity")
    return aliases[login]


def parse_execution(phase, raw_observe):
    if phase not in {"prepare", "evaluate"}:
        raise ApiError("phase must be exactly prepare or evaluate")
    if raw_observe not in {"true", "false"}:
        raise ApiError("observe-only must be exactly true or false")
    return raw_observe == "true"


def verify_intent_record(document, artifact, run, context):
    expected = f"merge-intent-pr-{document['number']}-{document['head']}"
    if (artifact["name"] != expected or artifact["expired"]
            or artifact["workflow_run"]["id"] != document["run_id"]
            or run["id"] != document["run_id"] or run["event"] != "pull_request_target"
            or run["head_sha"] != document["head"] or run["head_branch"] != document["head_ref"]
            or run["id"] == context["run_id"]
            or run["path"] != context["intent_path"]
            or document["caller_path"] != context["intent_path"]
            or document.get("observe_only") != context["observe_only"]
            or run["repository"]["full_name"] != context["repository"]
            or run["head_repository"]["full_name"] != context["repository"]
            or run["actor"]["login"] not in BOTS
            or run["actor"]["login"] != document["actor"]
            or run["status"] != "completed" or run["conclusion"] != "success"
            or timestamp(artifact["expires_at"]) <= timestamp(context["now"])
            or timestamp(artifact["created_at"]) < timestamp(run["created_at"])):
        raise ApiError("Intent artifact provenance or lifetime did not verify")
    # Serialized 'verified' fields are data, never evidence of verification.
    return document | {"verified": True, "expired": False, "artifact_id": artifact["id"],
                       "artifact_name": artifact["name"], "artifact_expires_at": artifact["expires_at"]}


def evaluate_and_enqueue(api, context, policy, intent):
    """Require two fresh ready snapshots before one expected-head enqueue."""
    snapshot = api.snapshot(context["number"])
    api.last_snapshot = snapshot
    inspect_queued = (policy.get("version") == 2 and getattr(api, "observe_only", False) is True
                      and context.get("observe_only") is True)
    result = decide(context, snapshot, policy, intent, inspect_queued=inspect_queued)
    if result[0] != "ready":
        return observer_result(api, result)
    fresh = api.snapshot(context["number"])
    api.last_snapshot = fresh
    if (fresh["pr"]["head"], fresh["pr"]["base"]) != (snapshot["pr"]["head"], snapshot["pr"]["base"]):
        return "blocked", "Head or base changed before enqueue; a new event must re-evaluate"
    if policy.get("version") == 2 and fresh.get("applicability") != snapshot.get("applicability"):
        return "blocked", "Applicability anchors or evidence changed before enqueue"
    result = decide(context, fresh, policy, intent, inspect_queued=inspect_queued)
    if result[0] != "ready":
        return observer_result(api, result)
    if getattr(api, "observe_only", False):
        if inspect_queued and fresh["pr"]["queued"]:
            return "observed-ready-existing", "Two fresh policy snapshots are ready; existing queue observed, no mutation attempted"
        return "observed-ready", "Two fresh policy snapshots are ready; no mutation attempted"
    api.enqueue(fresh["pr"]["id"], fresh["pr"]["head"])
    confirmed = api.snapshot(context["number"])
    if confirmed["pr"]["head"] == fresh["pr"]["head"] and confirmed["pr"]["queued"]:
        return "queued", "Queue entry confirmed for the expected head"
    return "blocked", "Queue insertion was not confirmed; no automatic retry"


def observer_result(api, result):
    if result[0] == "queued" and getattr(api, "observe_only", False):
        return "observed-existing", "Existing queue entry observed; no mutation attempted"
    return result


class GitHub:
    def __init__(self, repository, token, write_token=None, policy=None, *, observe_only=False, phase="evaluate"):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or any(part in {".", ".."} for part in repository.split("/"))):
            raise ApiError("Invalid GitHub repository identity")
        self.repository = repository
        self.owner, self.repo = repository.split("/")
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self.token = token
        self.write_token = write_token or token
        self.runs = {}
        self.refreshed_runs = set()
        self.listed_run_ids = set()
        self.job_inventories = {}
        self.policy = policy or {}
        if not isinstance(observe_only, bool):
            raise ApiError("observe_only must be a boolean")
        parse_execution(phase, "true" if observe_only else "false")
        self.observe_only = observe_only
        self.phase = phase
        self.api_calls = 0
        self.mutation_attempts = 0
        self.api_rate_limits = {}
        self.observer_api_attempt_limit = configured_observer_limit(self.policy, observe_only)
        self.api_attempt_budget_exhausted = False

    def api_url(self, path, method="GET"):
        return target_url(self.base, self.repository, path, method)

    def request(self, path, method="GET", data=None, write=False, binary=False):
        self.guard_operation(path, method, data, write)
        url = self.api_url(path, method)
        self.check_api_attempt_budget()
        self.api_calls += 1
        encoded = json.dumps(data).encode() if data is not None else None
        headers = {"Authorization": "Bearer " + (self.write_token if write else self.token),
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "netresearch-dependency-merge-controller"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                self.record_rate_limits(response.headers, write)
                body = response.read(MAX_BYTES + 1)
                response_headers = dict(response.headers)
        except urllib.error.HTTPError as error:
            self.record_rate_limits(error.headers, write)
            if binary and error.code == 302:
                location = error.headers.get("Location", "")
                if urllib.parse.urlparse(location).scheme != "https":
                    raise ApiError("Artifact redirect is not HTTPS") from error
                # GitHub redirects to signed blob storage. Never forward its token.
                with urllib.request.urlopen(urllib.request.Request(location), timeout=20) as response:
                    body = response.read(MAX_BYTES + 1)
                    response_headers = dict(response.headers)
            else:
                raise ApiError(f"GitHub API {method} {path.split('?')[0]} failed ({error.code})") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ApiError("GitHub API is unavailable") from error
        if len(body) > MAX_BYTES:
            raise ApiError("GitHub response exceeds the bounded response size")
        return (body if binary else json.loads(body)), response_headers

    def check_api_attempt_budget(self):
        """Deny the next permitted attempt before increment, request or opener creation."""
        if self.observer_api_attempt_limit is not None and self.api_calls >= self.observer_api_attempt_limit:
            self.api_attempt_budget_exhausted = True
            raise ApiError("Observer API attempt budget exhausted")

    def record_rate_limits(self, headers, write):
        """Retain one v2 response per existing credential-role/resource pair."""
        if self.policy.get("version") == 2:
            sample = rate_limit_sample(headers, "write" if write else "read", self.api_calls)
            self.api_rate_limits[(sample["credential_kind"], sample["resource"])] = sample

    def guard_operation(self, path, method, data, write):
        read_query = (path == "/graphql" and method == "POST" and isinstance(data, dict)
                      and set(data) <= {"query", "variables"}
                      and isinstance(data.get("query"), str)
                      and re.match(r"\s*query\s*[(\{]", data["query"])
                      and not re.search(r"\bmutation\b", data["query"]))
        mutating = write or (method != "GET" and not read_query)
        if not mutating:
            return
        self.mutation_attempts += 1
        if self.observe_only:
            raise ApiError("Observation forbids every PR/queue mutation")
        approval = (method == "POST" and not write
                    and re.fullmatch(r"/repos/" + re.escape(self.repository) + r"/pulls/[1-9][0-9]*/reviews", path)
                    and isinstance(data, dict) and set(data) == {"event", "commit_id"}
                    and data["event"] == "APPROVE" and re.fullmatch(r"[0-9a-f]{40}", data["commit_id"]))
        if self.phase == "prepare" and not approval:
            raise ApiError("Prepare may only create its head-bound eligibility approval")

    def rest(self, path):
        return self.request(path)

    def collection(self, path, key=None, maximum_pages=10, search_limit=None, require_total=False, repository_id=None):
        if require_total:
            repository_id = json_id(repository_id)
        result, total = [], None
        for page in range(1, maximum_pages + 1):
            separator = "&" if "?" in path else "?"
            data, headers = self.rest(f"{path}{separator}per_page=100&page={page}")
            if key and (not isinstance(data, dict) or key not in data):
                raise ApiError("Missing paginated API collection")
            values = data[key] if key else data
            if not isinstance(values, list):
                raise ApiError("Expected a paginated API list")
            if require_total and len(values) > 100:
                raise ApiError("Job inventory page exceeds its requested size")
            if search_limit is not None or require_total:
                count = data.get("total_count")
                if (type(count) is not int or count < 0 or (search_limit is not None and count >= search_limit)
                        or (total is not None and count != total)):
                    raise ApiError("Workflow search count is unavailable, changing or capped")
                total = count
            result.extend(values)
            link = next((value for name, value in headers.items() if name.lower() == "link"), "")
            if require_total and 'rel="next"' in link:
                targets = re.findall(r'<([^>]+)>;\s*rel="next"', link)
                expected = urllib.parse.urlsplit(self.api_url(f"{path}{separator}per_page=100&page={page + 1}"))
                actual = urllib.parse.urlsplit(targets[0]) if len(targets) == 1 else None
                alias_path = expected.path.replace(f"/repos/{self.repository}/", f"/repositories/{repository_id}/", 1)
                if (actual is None or actual.fragment or actual.scheme != expected.scheme or actual.scheme != "https"
                        or actual.netloc != expected.netloc or actual.path not in {expected.path, alias_path}
                        or urllib.parse.parse_qs(actual.query, keep_blank_values=True, strict_parsing=True)
                        != urllib.parse.parse_qs(expected.query, keep_blank_values=True, strict_parsing=True)
                        or len(urllib.parse.parse_qsl(actual.query, keep_blank_values=True, strict_parsing=True))
                        != len(urllib.parse.parse_qs(actual.query, keep_blank_values=True, strict_parsing=True))):
                    raise ApiError("Job pagination link leaves its exact advancing inventory")
            if (search_limit is not None or require_total) and not values and 'rel="next"' in link:
                raise ApiError("Workflow search returned an empty nonfinal page")
            if 'rel="next"' not in link:
                if total is not None and len(result) != total:
                    raise ApiError("Workflow search did not return its complete inventory")
                return result
        raise ApiError("API pagination was truncated at its explicit safety cap")

    def graphql(self, query, variables, write=False):
        result, _ = self.request("/graphql", "POST", {"query": query, "variables": variables}, write=write)
        if result.get("errors") or not result.get("data"):
            raise ApiError("GraphQL did not return a complete successful result")
        return result["data"]

    def pr_query(self, number, fields, variables=None):
        extra = ",$after:String" if variables is not None else ""
        query = (f"query($owner:String!,$repo:String!,$number:Int!{extra}){{"
                 f"repository(owner:$owner,name:$repo){{pullRequest(number:$number){{{fields}}}}}}}")
        data = self.graphql(query, {"owner": self.owner, "repo": self.repo, "number": number} | (variables or {}))
        repository = data.get("repository")
        if not isinstance(repository, dict) or not isinstance(repository.get("pullRequest"), dict):
            raise ApiError("GraphQL did not return the requested pull request")
        return repository["pullRequest"]

    @staticmethod
    def connection_field(name, arguments, fields, after=False):
        suffix = "," + arguments if arguments else ""
        cursor = ",after:$after" if after else ""
        return f"{name}(first:100{cursor}{suffix}){{nodes{{{fields}}}pageInfo{{hasNextPage endCursor}}}}"

    def connection(self, number, name, arguments, fields, first_page=None):
        result, after, seen = [], None, set()
        data = first_page
        for _ in range(10):
            if data is None:
                data = self.pr_query(number, self.connection_field(name, arguments, fields, after=True),
                                     {"after": after}).get(name)
            if (not isinstance(data, dict) or not isinstance(data.get("nodes"), list)
                    or not isinstance(data.get("pageInfo"), dict)
                    or type(data["pageInfo"].get("hasNextPage")) is not bool):
                raise ApiError("GraphQL connection is missing or malformed")
            result.extend(data["nodes"])
            if not data["pageInfo"]["hasNextPage"]:
                return result
            after = data["pageInfo"].get("endCursor")
            if not isinstance(after, str) or not after or after in seen:
                raise ApiError("GraphQL pagination has no advancing cursor")
            seen.add(after)
            data = None
        raise ApiError("GraphQL pagination was truncated at its explicit safety cap")

    def run_metadata(self, raw, expected_id=None, expected_sha=None, canonical=True):
        if not isinstance(raw, dict):
            raise ApiError("Missing workflow run metadata")
        run = raw.copy()
        try:
            run_id = positive_id(run["id"])
            if (run["id"] != run_id or type(run["run_attempt"]) is not int or run["run_attempt"] < 1
                    or run["repository"]["full_name"] != self.repository
                    or not re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
                    or (expected_id is not None and run_id != expected_id)
                    or (expected_sha is not None and run["head_sha"] != expected_sha)
                    or not isinstance(run["event"], str) or not run["event"]
                    or not isinstance(run["status"], str) or not run["status"]
                    or (run["conclusion"] is not None and not isinstance(run["conclusion"], str))):
                raise ApiError("Workflow run identity or attempt did not verify")
            if not isinstance(run.get("path"), str) or not run["path"]:
                raise ApiError("Workflow run path is missing")
            if canonical:
                run["path"] = workflow_path(run["path"])
                json_id(run["check_suite_id"])
            if timestamp(run["updated_at"]).tzinfo is None:
                raise ApiError("Workflow run timestamp has no timezone")
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ApiError("Workflow run metadata is incomplete or malformed") from error
        return run

    def run(self, run_id, refresh=False):
        run_id = positive_id(run_id)
        if refresh or run_id not in self.runs:
            run = self.rest(f"/repos/{self.repository}/actions/runs/{run_id}")[0]
            self.runs[run_id] = self.run_metadata(run, expected_id=run_id)
        return self.runs[run_id]

    def snapshot(self, number):
        """Collect one fresh complete PR inventory with versioned path evidence."""
        conditional = self.policy.get("version") == 2
        if conditional:
            validate_policy(self.policy)
        self.runs = {}  # A final reread must see a newly retried workflow attempt.
        self.refreshed_runs = set()
        self.listed_run_ids = set()
        self.job_inventories = {}
        history_fields = " ".join(f"... on {kind}{{createdAt actor{{login __typename}}}}" for kind in (
            "AddedToMergeQueueEvent", "RemovedFromMergeQueueEvent", "AutoMergeEnabledEvent", "AutoMergeDisabledEvent"))
        connections = {"timelineItems": (f"itemTypes:[{HISTORY_TYPES}]", "__typename " + history_fields),
                       "latestReviews": ("", "author{login}state commit{oid}"),
                       "reviewThreads": ("", "isResolved")}
        initial = " ".join(self.connection_field(name, *fields) for name, fields in connections.items())
        raw = self.pr_query(number, """id number state isDraft headRefOid baseRefOid baseRefName headRefName
            author{login __typename} headRepository{nameWithOwner} baseRepository{nameWithOwner}
            mergeable reviewDecision isInMergeQueue potentialMergeCommit{oid}
            labels(first:100){nodes{name}pageInfo{hasNextPage}} """ + initial
            + (" " + ANCHOR_FIELDS if conditional else ""))
        if not isinstance(raw, dict) or any(not isinstance(raw.get(name), dict) for name in connections):
            raise ApiError("Initial GraphQL inventory is incomplete")
        pr = {"id": raw["id"], "number": raw["number"], "state": raw["state"], "draft": raw["isDraft"],
              "head": raw["headRefOid"], "base": raw["baseRefOid"], "base_ref": raw["baseRefName"],
              "head_ref": raw["headRefName"], "author": graphql_bot_author(raw.get("author")),
              "repository": raw["baseRepository"]["nameWithOwner"],
              "source_repository": (raw["headRepository"] or {}).get("nameWithOwner"),
              "mergeable": raw["mergeable"], "review_decision": raw["reviewDecision"],
              "queued": raw["isInMergeQueue"], "test_merge": (raw["potentialMergeCommit"] or {}).get("oid"),
              "labels": [label["name"] for label in raw["labels"]["nodes"]]}
        proof = self.start_applicability(raw, pr)
        history, reviews, threads = (self.connection(number, name, *fields, first_page=raw[name])
                                    for name, fields in connections.items())
        rules = self.collection(f"/repos/{self.repository}/rules/branches/{urllib.parse.quote(pr['base_ref'], safe='')}")
        checks, statuses, workflows = self.verification_records(pr)
        self.finish_applicability(number, proof)
        snapshot = {"pr": pr, "checks": checks, "statuses": statuses, "rules": rules, "workflows": workflows,
            "history": [{"type": item["__typename"], "created_at": item["createdAt"],
                         "actor": (item["actor"] or {}).get("login"),
                         "actor_type": (item["actor"] or {}).get("__typename")} for item in history],
            "reviews": [{"author": (item["author"] or {}).get("login"), "state": item["state"],
                         "commit": (item["commit"] or {}).get("oid")} for item in reviews],
            "threads": [{"resolved": item["isResolved"]} for item in threads],
            "complete": {"history": True, "reviews": True, "threads": True, "rules": True,
                         "checks": True, "statuses": True, "labels": not raw["labels"]["pageInfo"]["hasNextPage"]}}
        if conditional:
            snapshot["applicability"] = proof
            snapshot["complete"].update(changed_files=True, conditional_workflows=True)
        return snapshot

    def start_applicability(self, raw, pr):
        """Read immutable v2 comparisons while retaining v1's exact API budget."""
        if self.policy.get("version") != 2:
            return None
        try:
            return self.changed_paths(raw, pr)
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("Changed-path evidence is incomplete or invalid: " + str(error)) from error

    def finish_applicability(self, number, proof):
        """Close v2 evidence with one live-anchor and immutable-blob read."""
        if proof is None:
            return
        try:
            data = self.graphql(*closing_query(self.repository, number, proof["anchor"], self.policy))
            verify_closing(data, proof["anchor"], self.policy)
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("Closing applicability evidence did not verify: " + str(error)) from error

    def changed_paths(self, raw, pr):
        """Bind complete changed paths to the live base, head and reviewed policy."""
        captured = anchor(raw, self.repository, self.policy["base_ref"])
        pr.update(captured)
        base, head = captured["base"], captured["head"]
        data, _ = self.rest(f"/repos/{self.repository}/compare/{base}...{head}")
        evidence = comparison(data, base, head, captured["changed_files"])
        if evidence["status"] == "diverged":
            merge_base = evidence["merge_base"]
            data, _ = self.rest(f"/repos/{self.repository}/compare/{merge_base}...{head}")
            ancestor = comparison(data, merge_base, head, captured["changed_files"])
            if (ancestor["status"] != "ahead" or ancestor["commits"] != evidence["commits"]
                    or ancestor["files"] != evidence["files"]):
                raise ApiError("Diverged and merge-base comparisons are inconsistent")
        paths = [name for name, _ in evidence["files"]]
        applicable = [entry["name"] for entry in conditional_selection(paths, self.policy)]
        return {"anchor": captured, "merge_base": evidence["merge_base"],
                "comparison_status": evidence["status"], "commits": evidence["commits"],
                "paths": paths, "applicable_checks": applicable,
                "inapplicable_checks": [entry["name"] for entry in self.policy["conditional_checks"]
                                        if entry["name"] not in applicable],
                "workflow_blobs": workflow_pins(self.policy), "policy_digest": digest(self.policy)}

    def workflow_records(self, sha):
        """Retain all declared producers from one bounded SHA-specific run list."""
        workflows = []
        expected = {WORKFLOW_DIRECTORY + item["workflow"] for item in declared_checks(self.policy)}
        for raw in self.collection(f"/repos/{self.repository}/actions/runs?head_sha={sha}",
                                   "workflow_runs", search_limit=1000):
            run = self.run_metadata(raw, expected_sha=sha, canonical=False)
            if run["id"] in self.listed_run_ids:
                raise ApiError("Duplicate workflow run identity makes the search inventory ambiguous")
            self.listed_run_ids.add(run["id"])
            if (run["event"] == "dynamic"
                    and re.fullmatch(r"dynamic/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", run["path"])):
                # Only observed dynamic identities are inventory-only. A check
                # reference still takes strict single-run path validation.
                continue
            run["path"] = workflow_path(run["path"])
            json_id(run.get("check_suite_id"))
            self.runs[run["id"]] = run
            if run["path"] in expected:
                workflows.append(self.workflow_record(run))
        return workflows

    @staticmethod
    def workflow_record(run):
        return {"workflow": run["path"].split("/")[-1], "sha": run["head_sha"],
                "repository": run["repository"]["full_name"], "event": run["event"],
                "run_id": run["id"], "status": run["status"], "conclusion": run["conclusion"]}

    def normalize_check(self, check, pr, expected_sha=None):
        normalized = {"id": check["id"], "name": check["name"], "sha": check["head_sha"],
            "status": check["status"], "conclusion": check["conclusion"],
            "app_id": check["app"]["id"], "app_slug": check["app"]["slug"],
            "run_id": 0, "attempt": 0, "repository": None, "workflow": None, "event": None,
            "job_verified": False, "job_current": False, "job_run_revision": None}
        match = re.fullmatch(r"https://github\.com/" + re.escape(self.repository)
                             + r"/actions/runs/([1-9][0-9]*)(?:/job/([1-9][0-9]*))?", check.get("details_url") or "")
        if match and (normalized["app_id"], normalized["app_slug"]) == (15368, "github-actions"):
            run_id = int(match[1])
            cached = run_id in self.runs
            run = self.run(run_id)
            if not cached:
                self.refreshed_runs.add(run_id)
            if self.check_newer_than_run(check, run):
                if run_id in self.refreshed_runs:
                    raise ApiError("Check evidence is newer than its workflow run metadata")
                self.refreshed_runs.add(run_id)
                previous, run = run, self.run(run_id, refresh=True)
                if (any(run[field] != previous[field] for field in ("path", "head_sha", "event", "repository", "check_suite_id"))
                        or run["run_attempt"] < previous["run_attempt"]
                        or timestamp(run["updated_at"]) < timestamp(previous["updated_at"])
                        or self.check_newer_than_run(check, run)):
                    raise ApiError("Workflow run refresh did not establish coherent check metadata")
            if run["head_sha"] not in {pr["head"], pr["test_merge"]}:
                raise ApiError("Check producer run is bound to a different commit")
            job, current = bind_check(self, check, run, expected_sha or check["head_sha"],
                                      int(match[2]) if match[2] else None)
            normalized.update(run_id=run["id"], attempt=job["run_attempt"], job_verified=True, job_current=current,
                job_run_revision=run_revision(run),
                repository=run["repository"]["full_name"], workflow=run["path"].split("/")[-1],
                event=run["event"])
        return normalized

    @staticmethod
    def check_newer_than_run(check, run):
        attempt = check.get("run_attempt")
        if attempt is not None and (type(attempt) is not int or attempt < 1):
            raise ApiError("Check attempt evidence is malformed")
        try:
            # GitHub can synthesize a skipped check's start after completion.
            # Terminal completion is its freshness witness; active work uses start.
            terminal = check["status"] == "completed"
            value = check.get("completed_at" if terminal else "started_at")
            if terminal and (not isinstance(value, str) or not value):
                raise ApiError("Terminal check has no completion timestamp")
            times = [timestamp(value)] if value is not None else []
            if any(value.tzinfo is None for value in times):
                raise ApiError("Check timestamp has no timezone")
            return ((attempt is not None and attempt > run["run_attempt"])
                    or any(value > timestamp(run["updated_at"]) for value in times))
        except (ValueError, TypeError, AttributeError) as error:
            raise ApiError("Check timestamp evidence is malformed") from error

    def verification_records(self, pr):
        """Bind current checks and late producers to coherent server run/jobs."""
        checks, statuses, workflows = [], [], []
        # Both SHA locations are explicit and bound to this freshly read PR.
        for sha in sorted({pr["head"], pr["test_merge"]} - {None}):
            workflows.extend(self.workflow_records(sha))
            statuses.extend(self.collection(f"/repos/{self.repository}/commits/{sha}/statuses"))
            records = self.collection(f"/repos/{self.repository}/commits/{sha}/check-runs?filter=all", "check_runs")
            checks.extend(self.normalize_check(check, pr, sha) for check in records)
        for check in checks:
            if check["job_verified"] and check["job_run_revision"] != run_revision(self.runs[check["run_id"]]):
                raise ApiError("Actions job membership proof changed within the snapshot")
        # Keep every initially observed negative. A coherent newer negative
        # refresh of a green record must also affect workflow readiness.
        workflows = [item if (item["status"], item["conclusion"]) != ("completed", "success")
                     else self.workflow_record(self.runs[item["run_id"]]) for item in workflows]
        initial_ids = {item["run_id"] for item in workflows}
        required = {(WORKFLOW_DIRECTORY + item["workflow"], event)
                    for item in declared_checks(self.policy) for event in item["events"]}
        # A run can appear after the complete list read but before its checks.
        # Strict single-run fallback metadata is already known in this snapshot;
        # do not lose a newly discovered pending/failed required producer.
        workflows.extend(self.workflow_record(run) for run_id, run in self.runs.items()
                         if run_id not in initial_ids and (run["path"], run["event"]) in required
                         and run["repository"]["full_name"] == self.repository
                         and run["head_sha"] in {pr["head"], pr["test_merge"]})
        return checks, statuses, workflows

    def intent(self, context):
        name = f"merge-intent-pr-{context['number']}-{context['event_head']}"
        artifacts = self.collection(f"/repos/{self.repository}/actions/artifacts?name={name}", "artifacts")
        if not artifacts:
            return None
        # Filter server-verified metadata before downloading, then reject ambiguity.
        candidates = []
        for artifact in artifacts:
            if artifact["expired"] or timestamp(artifact["expires_at"]) <= timestamp(context["now"]):
                continue  # No automatic intent remains; explicit human recovery is separate.
            run = self.run(artifact["workflow_run"]["id"])
            if run["event"] == "pull_request_target" and run["path"] == context["intent_path"]:
                candidates.append((artifact, run))
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ApiError("Intent origin is missing or ambiguous")
        artifact, run = candidates[0]
        archive, _ = self.request(f"/repos/{self.repository}/actions/artifacts/{artifact['id']}/zip", binary=True)
        with zipfile.ZipFile(io.BytesIO(archive)) as content:
            members = content.infolist()
            if len(members) != 1 or members[0].filename != "intent.json" or members[0].file_size > 65536:
                raise ApiError("Intent archive has an unexpected layout or size")
            document = json.loads(content.read(members[0]))
        return verify_intent_record(document, artifact, run, context)

    def enqueue(self, pr_id, head):
        self.graphql("""mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(
            input:{pullRequestId:$id,expectedHeadOid:$head}){mergeQueueEntry{id}}}""",
            {"id": pr_id, "head": head}, write=True)


def wakeup_declarations(context, policy):
    """Reject recursive and weaker-origin hints before using any completion."""
    rows = completion_wakeups(policy)
    own = {context.get("caller_path"), context["intent_path"]}
    if any(WORKFLOW_DIRECTORY + row["workflow"] in own for row in rows):
        raise Blocked("Completion wakeups cannot identify the evaluator or intent workflow")
    return rows


def allowed_completion(run, context, policy):
    """Allow trusted origin, declared producers or exact successful bot hints."""
    wakeups = wakeup_declarations(context, policy)
    if run["path"] == context["intent_path"]:
        return (run["event"] == "pull_request_target" and run["actor"]["login"] in BOTS
                and run["conclusion"] == "success")
    if any(run["path"] == WORKFLOW_DIRECTORY + check["workflow"]
           and run["event"] in check["events"] for check in declared_checks(policy)):
        return True
    return (run["conclusion"] == "success" and run["actor"]["login"] in BOTS
            and any(run["path"] == WORKFLOW_DIRECTORY + row["workflow"]
                    and run["event"] in row["events"] for row in wakeups))


def configured_workflow_paths():
    paths = {"caller_path": os.environ["CALLER_WORKFLOW"], "intent_path": os.environ["INTENT_WORKFLOW"]}
    for value in paths.values():
        if workflow_path(value) != value:
            raise Blocked("Configured workflow identities must be bare repository-relative paths")
    return paths


def metadata_wakeup(event, repository):
    """A selected metadata hint can request evaluation, never create intent."""
    try:
        pr, action = event["pull_request"], event["action"]
        if (event["repository"]["full_name"] != repository
                or pr["head"]["repo"]["full_name"] != repository
                or pr["base"]["repo"]["full_name"] != repository
                or pr["user"]["login"] not in BOTS
                or not pr["head"]["ref"].startswith(("renovate/", "dependabot/"))
                or not re.fullmatch(r"[0-9a-f]{40}", pr["head"]["sha"])
                or type(pr["number"]) is not int or pr["number"] < 1):
            return False
        if action == "unlabeled":
            return event["label"]["name"] in {"deps-no-automerge", "deps-major"}
        if action == "ready_for_review":
            return pr["draft"] is False
        if action == "edited":
            previous = event["changes"]["base"]["ref"]["from"]
            return isinstance(previous, str) and bool(previous) and previous != pr["base"]["ref"]
        if action == "auto_merge_enabled":
            sender = event["sender"]
            return sender["type"] == "User" and isinstance(sender["login"], str) and bool(sender["login"])
    except (KeyError, TypeError, AttributeError):
        return False
    return False


def context_from_event(api, event, policy):
    event_name = os.environ["GITHUB_EVENT_NAME"]
    context = {"repository": api.repository, "event": event_name,
               "action": event.get("action"), "actor": os.environ["GITHUB_ACTOR"],
               "run_id": int(os.environ["GITHUB_RUN_ID"]),
               **configured_workflow_paths(),
               "observe_only": api.observe_only,
               "now": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "update_type": os.environ.get("UPDATE_TYPE", ""),
               "ecosystem": os.environ.get("ECOSYSTEM", "")}
    wakeup_declarations(context, policy)
    if event_name in {"pull_request_target", "pull_request_review"}:
        if event_name == "pull_request_review" and event.get("action") not in {"submitted", "dismissed"}:
            raise Blocked("Unsupported review event")
        if event_name == "pull_request_target" and event.get("action") not in INITIAL_TARGET_ACTIONS:
            if not metadata_wakeup(event, api.repository):
                raise Blocked("Target activity is outside the selected dependency metadata scope")
            context["metadata_wakeup"] = True
        context.update(number=event["pull_request"]["number"], event_head=event["pull_request"]["head"]["sha"])
    elif event_name == "workflow_run":
        run = api.run(event["workflow_run"]["id"])
        if (event.get("action") != "completed" or run["status"] != "completed"
                or run["repository"]["full_name"] != api.repository
                or run["head_repository"]["full_name"] != api.repository
                or not run["head_branch"].startswith(("renovate/", "dependabot/"))
                or not allowed_completion(run, context, policy)):
            raise Blocked("Completion is outside the trusted dependency workflow scope")
        candidates = api.collection(f"/repos/{api.repository}/pulls?state=open&head="
                                    + urllib.parse.quote(api.owner + ":" + run["head_branch"], safe=""))
        candidates = [pr for pr in candidates if pr["head"]["sha"] == run["head_sha"]
                      and pr["head"]["repo"]["full_name"] == api.repository]
        if len(candidates) != 1:
            raise Blocked("Completion does not identify exactly one current-head PR")
        context.update(number=candidates[0]["number"], event_head=run["head_sha"])
    else:
        raise Blocked("Unsupported controller event")
    return context


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def phase_context(context, phase):
    producer = context["caller_path"] == context["intent_path"]
    if phase == "prepare" and (context["event"] != "pull_request_target" or not producer
                               or context.get("action") not in INITIAL_TARGET_ACTIONS):
        raise Blocked("Prepare requires the separate trusted PR-target intent caller")
    selected_target = (context["event"] == "pull_request_target"
                       and context.get("metadata_wakeup") is True
                       and context.get("action") in METADATA_ACTIONS)
    if phase == "evaluate" and (producer or (context["event"] not in {"workflow_run", "pull_request_review"}
                                            and not selected_target)):
        raise Blocked("Evaluate requires a separate completion, review or selected metadata caller")


def prepare_intent(api, context, policy):
    snapshot = api.snapshot(context["number"])
    api.last_snapshot = snapshot
    intent = initial_intent(context, snapshot, policy)
    existing = api.intent(context)
    if existing is not None:
        check_intent(context, snapshot, policy, existing)
        return existing
    directory = Path(os.environ["RUNNER_TEMP"]) / f"dependency-intent-{context['run_id']}"
    directory.mkdir(exist_ok=True)
    path = directory / "intent.json"
    path.write_text(json.dumps(intent, sort_keys=True) + "\n")
    if not api.observe_only:
        api.request(f"/repos/{api.repository}/pulls/{context['number']}/reviews", "POST",
                    {"event": "APPROVE", "commit_id": intent["head"]})
    output(intent_name=f"merge-intent-pr-{context['number']}-{intent['head']}", intent_path=path)
    return intent


def write_receipt(api, context, policy, phase, intent, state, reason):
    """Record the observed decision, immutable evidence and actual API demand."""
    snapshot = getattr(api, "last_snapshot", {})
    pr = snapshot.get("pr", {})
    receipt = {"phase": phase, "observe_only": api.observe_only, "event": context.get("event"),
               "run_id": context.get("run_id"), "pr": context.get("number"),
               "head": pr.get("head", context.get("event_head")), "base": pr.get("base"),
               "caller_workflow": context.get("caller_path"), "intent_workflow": context.get("intent_path"),
               "origin_run_id": (intent or {}).get("run_id"), "artifact_id": (intent or {}).get("artifact_id"),
               "artifact_name": (intent or {}).get("artifact_name"), "policy_digest": digest(policy),
               "decision": state, "reason": reason, "required_checks": policy.get("required_checks"),
               "mutation_attempts": getattr(api, "mutation_attempts", 0),
               "api_calls": getattr(api, "api_calls", 0)}
    if policy.get("version") == 2:
        receipt.update(api_attempt_budget_receipt(api))
        receipt["api_rate_limits"] = rate_limit_rows(getattr(api, "api_rate_limits", {}))
        receipt["conditional_checks"] = policy.get("conditional_checks")
        receipt["applicability"] = snapshot.get("applicability")
        receipt["applicability_digest"] = digest(snapshot["applicability"]) if "applicability" in snapshot else None
        if api.observe_only:
            receipt["queue_present"] = pr.get("queued")
    text = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    # One explicit phase executes per isolated job. Keep the filename constant:
    # CLI/context data belongs inside JSON, never in the filesystem path.
    path = Path(os.environ["RUNNER_TEMP"]) / "dependency-controller-receipt.json"
    path.write_text(text)
    output(state=state, receipt_path=path)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(f"Dependency queue controller: **{state}**. {reason}\n\n```json\n{text}```\n")
    print(f"Dependency queue controller: {state}: {reason}")


def main(phase):
    observe_only = parse_execution(phase, os.environ.get("OBSERVE_ONLY", ""))
    policy = json.loads(os.environ["CONTROLLER_POLICY"])
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["READ_TOKEN"], os.environ.get("WRITE_TOKEN"),
                 policy, observe_only=observe_only, phase=phase)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    context, intent = {}, None
    try:
        context = context_from_event(api, event, policy)
        phase_context(context, phase)
        if phase == "prepare":
            intent = prepare_intent(api, context, policy)
            state, reason = "prepared", "Initial intent is ready for upload; this producer never enqueues"
        else:
            intent = api.intent(context)
            state, reason = evaluate_and_enqueue(api, context, policy, intent)
    except (Blocked, ApiError, KeyError, ValueError, TypeError, OSError, zipfile.BadZipFile) as error:
        state, reason = "blocked", str(error)
    write_receipt(api, context, policy, phase, intent, state, reason)


if __name__ == "__main__":
    main(sys.argv[1])
