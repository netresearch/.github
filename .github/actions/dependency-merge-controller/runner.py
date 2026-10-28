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

from policy import BOTS, Blocked, check_intent, decide, initial_intent

MAX_BYTES = 4 * 1024 * 1024
HISTORY_TYPES = "ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT,AUTO_MERGE_ENABLED_EVENT,AUTO_MERGE_DISABLED_EVENT"


class ApiError(Exception):
    """An API response is unavailable, incomplete or untrusted."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def verify_intent_record(document, artifact, run, context):
    expected = f"merge-intent-pr-{document['number']}-{document['head']}"
    same_run = run["id"] == context["run_id"]
    if (artifact["name"] != expected or artifact["expired"]
            or artifact["workflow_run"]["id"] != document["run_id"]
            or run["id"] != document["run_id"] or run["event"] != "pull_request_target"
            or run["path"] != context["caller_path"]
            or document["caller_path"] != context["caller_path"]
            or run["repository"]["full_name"] != context["repository"]
            or run["head_repository"]["full_name"] != context["repository"]
            or run["actor"]["login"] not in BOTS
            or run["actor"]["login"] != document["actor"]
            or (not same_run and (run["status"] != "completed" or run["conclusion"] != "success"))
            or timestamp(artifact["expires_at"]) <= timestamp(context["now"])
            or timestamp(artifact["created_at"]) < timestamp(run["created_at"])):
        raise ApiError("Intent artifact provenance or lifetime did not verify")
    # Serialized 'verified' fields are data, never evidence of verification.
    return document | {"verified": True, "expired": False}


def evaluate_and_enqueue(api, context, policy, intent):
    snapshot = api.snapshot(context["number"])
    result = decide(context, snapshot, policy, intent)
    if result[0] != "ready":
        return result
    fresh = api.snapshot(context["number"])
    if (fresh["pr"]["head"], fresh["pr"]["base"]) != (snapshot["pr"]["head"], snapshot["pr"]["base"]):
        return "blocked", "Head or base changed before enqueue; a new event must re-evaluate"
    result = decide(context, fresh, policy, intent)
    if result[0] != "ready":
        return result
    api.enqueue(fresh["pr"]["id"], fresh["pr"]["head"])
    confirmed = api.snapshot(context["number"])
    if confirmed["pr"]["head"] == fresh["pr"]["head"] and confirmed["pr"]["queued"]:
        return "queued", "Queue entry confirmed for the expected head"
    return "blocked", "Queue insertion was not confirmed; no automatic retry"


class GitHub:
    def __init__(self, repository, token, write_token=None, policy=None):
        self.repository = repository
        self.owner, self.repo = repository.split("/")
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self.token = token
        self.write_token = write_token or token
        self.runs = {}
        self.policy = policy or {}

    def request(self, path, method="GET", data=None, write=False, binary=False):
        url = self.base + path
        encoded = json.dumps(data).encode() if data is not None else None
        headers = {"Authorization": "Bearer " + (self.write_token if write else self.token),
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "netresearch-dependency-merge-controller"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                body = response.read(MAX_BYTES + 1)
                response_headers = dict(response.headers)
        except urllib.error.HTTPError as error:
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

    def rest(self, path):
        return self.request(path)

    def collection(self, path, key=None, maximum_pages=10):
        result = []
        for page in range(1, maximum_pages + 1):
            separator = "&" if "?" in path else "?"
            data, headers = self.rest(f"{path}{separator}per_page=100&page={page}")
            values = data[key] if key else data
            if not isinstance(values, list):
                raise ApiError("Expected a paginated API list")
            result.extend(values)
            link = next((value for name, value in headers.items() if name.lower() == "link"), "")
            if 'rel="next"' not in link:
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
        return data["repository"]["pullRequest"]

    def connection(self, number, name, arguments, fields):
        result, after = [], None
        for _ in range(10):
            suffix = "," + arguments if arguments else ""
            data = self.pr_query(number,
                f"{name}(first:100,after:$after{suffix}){{nodes{{{fields}}}pageInfo{{hasNextPage endCursor}}}}",
                {"after": after})[name]
            result.extend(data["nodes"])
            if not data["pageInfo"]["hasNextPage"]:
                return result
            after = data["pageInfo"]["endCursor"]
            if not after:
                raise ApiError("GraphQL pagination has no next cursor")
        raise ApiError("GraphQL pagination was truncated at its explicit safety cap")

    def run(self, run_id):
        if run_id not in self.runs:
            self.runs[run_id] = self.rest(f"/repos/{self.repository}/actions/runs/{run_id}")[0]
        return self.runs[run_id]

    def snapshot(self, number):
        self.runs = {}  # A final reread must see a newly retried workflow attempt.
        raw = self.pr_query(number, """id number state isDraft headRefOid baseRefOid baseRefName headRefName
            author{login} headRepository{nameWithOwner} baseRepository{nameWithOwner}
            mergeable reviewDecision isInMergeQueue potentialMergeCommit{oid}
            labels(first:100){nodes{name}pageInfo{hasNextPage}}""")
        pr = {"id": raw["id"], "number": raw["number"], "state": raw["state"], "draft": raw["isDraft"],
              "head": raw["headRefOid"], "base": raw["baseRefOid"], "base_ref": raw["baseRefName"],
              "head_ref": raw["headRefName"], "author": raw["author"]["login"],
              "repository": raw["baseRepository"]["nameWithOwner"],
              "source_repository": (raw["headRepository"] or {}).get("nameWithOwner"),
              "mergeable": raw["mergeable"], "review_decision": raw["reviewDecision"],
              "queued": raw["isInMergeQueue"], "test_merge": (raw["potentialMergeCommit"] or {}).get("oid"),
              "labels": [label["name"] for label in raw["labels"]["nodes"]]}
        history_fields = " ".join(f"... on {kind}{{createdAt actor{{login __typename}}}}" for kind in (
            "AddedToMergeQueueEvent", "RemovedFromMergeQueueEvent", "AutoMergeEnabledEvent", "AutoMergeDisabledEvent"))
        history = self.connection(number, "timelineItems", f"itemTypes:[{HISTORY_TYPES}]", "__typename " + history_fields)
        reviews = self.connection(number, "latestReviews", "", "author{login}state commit{oid}")
        threads = self.connection(number, "reviewThreads", "", "isResolved")
        rules = self.collection(f"/repos/{self.repository}/rules/branches/{urllib.parse.quote(pr['base_ref'], safe='')}")
        checks, statuses, workflows = [], [], []
        # Both SHA locations are explicit and bound to this freshly read PR.
        for sha in sorted({pr["head"], pr["test_merge"]} - {None}):
            for filename in sorted({item["workflow"] for item in self.policy["required_checks"]}):
                for run in self.collection(f"/repos/{self.repository}/actions/workflows/{filename}/runs?head_sha={sha}", "workflow_runs"):
                    workflows.append({"workflow": filename, "sha": run["head_sha"],
                        "repository": run["repository"]["full_name"], "event": run["event"],
                        "run_id": run["id"], "status": run["status"], "conclusion": run["conclusion"]})
            statuses.extend(self.collection(f"/repos/{self.repository}/commits/{sha}/statuses"))
            for check in self.collection(f"/repos/{self.repository}/commits/{sha}/check-runs?filter=all", "check_runs"):
                normalized = {"id": check["id"], "name": check["name"], "sha": check["head_sha"],
                    "status": check["status"], "conclusion": check["conclusion"],
                    "app_id": check["app"]["id"], "app_slug": check["app"]["slug"],
                    "run_id": 0, "attempt": 0, "repository": None, "workflow": None, "event": None}
                match = re.fullmatch(r"https://github\.com/" + re.escape(self.repository)
                                     + r"/actions/runs/(\d+)(?:/job/\d+)?", check.get("details_url") or "")
                if match:
                    run = self.run(int(match[1]))
                    if run["head_sha"] not in {pr["head"], pr["test_merge"]}:
                        raise ApiError("Check producer run is bound to a different commit")
                    normalized.update(run_id=run["id"], attempt=run["run_attempt"],
                        repository=run["repository"]["full_name"], workflow=run["path"].split("/")[-1],
                        event=run["event"])
                checks.append(normalized)
        return {"pr": pr, "checks": checks, "statuses": statuses, "rules": rules, "workflows": workflows,
            "history": [{"type": item["__typename"], "created_at": item["createdAt"],
                         "actor": (item["actor"] or {}).get("login"),
                         "actor_type": (item["actor"] or {}).get("__typename")} for item in history],
            "reviews": [{"author": (item["author"] or {}).get("login"), "state": item["state"],
                         "commit": (item["commit"] or {}).get("oid")} for item in reviews],
            "threads": [{"resolved": item["isResolved"]} for item in threads],
            "complete": {"history": True, "reviews": True, "threads": True, "rules": True,
                         "checks": True, "statuses": True, "labels": not raw["labels"]["pageInfo"]["hasNextPage"]}}

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
            if run["event"] == "pull_request_target" and run["path"] == context["caller_path"]:
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


def context_from_event(api, event, policy):
    event_name = os.environ["GITHUB_EVENT_NAME"]
    context = {"repository": api.repository, "event": event_name,
               "action": event.get("action"), "actor": os.environ["GITHUB_ACTOR"],
               "run_id": int(os.environ["GITHUB_RUN_ID"]),
               "caller_path": os.environ["CALLER_WORKFLOW"],
               "now": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "update_type": os.environ.get("UPDATE_TYPE", ""),
               "ecosystem": os.environ.get("ECOSYSTEM", "")}
    if event_name in {"pull_request_target", "pull_request_review"}:
        if event_name == "pull_request_review" and event.get("action") not in {"submitted", "dismissed"}:
            raise Blocked("Unsupported review event")
        context.update(number=event["pull_request"]["number"], event_head=event["pull_request"]["head"]["sha"])
    elif event_name == "workflow_run":
        run = api.run(event["workflow_run"]["id"])
        workflow = run["path"].split("/")[-1]
        if (event.get("action") != "completed" or run["status"] != "completed"
                or run["event"] not in {"pull_request", "pull_request_target"}
                or run["repository"]["full_name"] != api.repository
                or run["head_repository"]["full_name"] != api.repository
                or not run["head_branch"].startswith(("renovate/", "dependabot/"))
                or workflow not in {check["workflow"] for check in policy["required_checks"]}):
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


def main(mode):
    policy = json.loads(os.environ["CONTROLLER_POLICY"])
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["READ_TOKEN"], os.environ.get("WRITE_TOKEN"), policy)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    try:
        context = context_from_event(api, event, policy)
        if mode == "prepare":
            if context["event"] != "pull_request_target":
                return
            snapshot = api.snapshot(context["number"])
            intent = initial_intent(context, snapshot, policy)
            existing = api.intent(context)
            if existing is not None:
                check_intent(context, snapshot, policy, existing)
                return  # Reuse the exact trusted head-bound intent; do not create ambiguity.
            directory = Path(os.environ["RUNNER_TEMP"]) / f"dependency-intent-{context['run_id']}"
            directory.mkdir(exist_ok=True)
            path = directory / "intent.json"
            path.write_text(json.dumps(intent, sort_keys=True) + "\n")
            # The existing eligibility approval is retained only on this entry.
            api.request(f"/repos/{api.repository}/pulls/{context['number']}/reviews", "POST", {"event": "APPROVE"})
            output(intent_name=f"merge-intent-pr-{context['number']}-{intent['head']}", intent_path=path)
            return
        intent = api.intent(context)
        state, reason = evaluate_and_enqueue(api, context, policy, intent)
    except (Blocked, ApiError, KeyError, ValueError, TypeError, OSError, zipfile.BadZipFile) as error:
        state, reason = "blocked", str(error)
    output(state=state)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(f"Dependency queue controller: **{state}**. {reason}\n")
    print(f"Dependency queue controller: {state}: {reason}")


if __name__ == "__main__":
    main(sys.argv[1])
