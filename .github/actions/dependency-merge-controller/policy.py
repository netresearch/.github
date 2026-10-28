"""Pure, fail-closed dependency queue policy; no network or mutations."""

import hashlib
import json
import re

BOTS = {"dependabot[bot]", "renovate[bot]"}
REMOVALS = {"RemovedFromMergeQueueEvent", "AutoMergeDisabledEvent"}
ACTIVATIONS = {"AddedToMergeQueueEvent", "AutoMergeEnabledEvent"}


class Blocked(Exception):
    """A prerequisite could not be established safely."""


def digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def validate_policy(policy):
    if policy.get("version") != 1 or not policy.get("required_checks"):
        raise Blocked("A nonempty version-1 required-check inventory is required")
    names = set()
    for check in policy["required_checks"]:
        if (not isinstance(check.get("name"), str) or check["name"] in names
                or check.get("app_id") != 15368
                or not re.fullmatch(r"[A-Za-z0-9_-]+\.ya?ml", check.get("workflow", ""))
                or not check.get("events")
                or set(check["events"]) - {"pull_request", "pull_request_target", "merge_group"}):
            raise Blocked("Invalid or duplicate required-check identity")
        names.add(check["name"])
    minimum = policy.get("minimum_approvals", 2)
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
        raise Blocked("minimum_approvals must be a nonnegative integer")
    if names.intersection(policy.get("advisory_checks", [])):
        raise Blocked("A required check cannot also be advisory")


def eligible(context, snapshot, policy):
    validate_policy(policy)
    if not snapshot.get("complete") or not all(snapshot["complete"].values()):
        raise Blocked("API inventory is unreadable or truncated")
    pr = snapshot["pr"]
    if (pr["repository"] != context["repository"]
            or pr["source_repository"] != context["repository"]
            or pr["author"] not in BOTS
            or not pr["head_ref"].startswith(("renovate/", "dependabot/"))
            or pr["state"] != "OPEN" or pr["draft"]
            or {"deps-no-automerge", "deps-major"}.intersection(pr["labels"])
            or not re.fullmatch(r"[0-9a-f]{40}", pr["head"])):
        raise Blocked("PR author, source, branch, labels or state is not eligible")
    if context.get("event_head") and context["event_head"] != pr["head"]:
        raise Blocked("The event head is no longer current")
    history = sorted(snapshot["history"], key=lambda item: item["created_at"])
    removed = False
    for item in history:
        if item["type"] in REMOVALS:
            removed = True
        elif item["type"] in ACTIVATIONS and item.get("actor_type") == "User":
            removed = False
    if removed:
        raise Blocked("Queue removal or auto-merge disable requires human reactivation")


def initial_intent(context, snapshot, policy):
    eligible(context, snapshot, policy)
    if (context["event"] != "pull_request_target"
            or context["action"] not in {"opened", "synchronize", "reopened"}
            or context["actor"] not in BOTS
            or context.get("event_head") != snapshot["pr"]["head"]):
        raise Blocked("Only a current trusted bot PR event may create intent")
    safe = True
    if policy.get("safe_updates_only") and snapshot["pr"]["author"] == "dependabot[bot]":
        update = context.get("update_type", "")
        safe = (update in {"version-update:semver-minor", "version-update:semver-patch"}
                or (context.get("ecosystem") == "docker" and bool(update)
                    and update != "version-update:semver-major"))
    if not safe:
        raise Blocked("Dependency metadata does not establish a permitted safe update")
    return {"version": 1, "repository": context["repository"],
            "number": snapshot["pr"]["number"], "head": snapshot["pr"]["head"],
            "origin_event": context["event"], "origin_action": context["action"],
            "actor": context["actor"], "run_id": context["run_id"],
            "caller_path": context["caller_path"], "created_at": context["now"],
            "policy_digest": digest(policy), "safe_update": safe,
            "verified": True, "expired": False}


def human_activation(snapshot):
    """A later explicit human activation can authorize recovery after expiry."""
    last = None
    for item in sorted(snapshot["history"], key=lambda item: item["created_at"]):
        if item["type"] in REMOVALS:
            last = None
        elif item["type"] in ACTIVATIONS and item.get("actor_type") == "User":
            last = item
    return last


def check_intent(context, snapshot, policy, intent):
    if intent is None and human_activation(snapshot):
        return
    pr = snapshot["pr"]
    if (not intent or intent.get("version") != 1 or not intent.get("verified")
            or intent.get("expired") or intent.get("repository") != context["repository"]
            or intent.get("number") != pr["number"] or intent.get("head") != pr["head"]
            or intent.get("origin_event") != "pull_request_target"
            or intent.get("actor") not in BOTS or not intent.get("safe_update")
            or intent.get("policy_digest") != digest(policy)):
        raise Blocked("No valid current-head intent from a trusted initial run")


def evaluate_checks(context, snapshot, policy):
    pr = snapshot["pr"]
    expected = {check["name"]: check for check in policy["required_checks"]}
    latest = {}
    for check in snapshot["checks"]:
        key = (check.get("run_id", 0), check.get("attempt", 0), check["id"])
        if check["name"] not in latest or key > latest[check["name"]][0]:
            latest[check["name"]] = (key, check)
    for name, requirement in expected.items():
        if name not in latest:
            return "waiting", f"Missing expected check: {name}"
        check = latest[name][1]
        if (check.get("app_id") != requirement["app_id"]
                or check.get("app_slug") != "github-actions"
                or check.get("workflow") != requirement["workflow"]
                or check.get("event") not in requirement["events"]
                or check.get("repository") != context["repository"]
                or check.get("sha") not in {pr["head"], pr.get("test_merge")}):
            raise Blocked(f"Unexpected producer or commit for required check: {name}")
        if check["status"] != "completed":
            return "waiting", f"Required check is pending: {name}"
        if check.get("conclusion") != "success":
            raise Blocked(f"Required check did not succeed: {name}")
    advisory = set(policy.get("advisory_checks", []))
    for name, (_, check) in latest.items():
        if name in advisory or name.endswith(" / Dependency merge controller"):
            continue
        if check.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}:
            raise Blocked(f"An additional check failed: {name}")
        if check["status"] != "completed":
            return "waiting", f"An additional check is pending: {name}"
    latest_statuses = {}
    for status in snapshot["statuses"]:
        if (status["context"] not in latest_statuses
                or status["id"] > latest_statuses[status["context"]]["id"]):
            latest_statuses[status["context"]] = status
    for name, status in latest_statuses.items():
        if name in advisory:
            continue
        if status["state"] in {"failure", "error"}:
            raise Blocked(f"A commit status failed: {name}")
        if status["state"] != "success":
            return "waiting", f"A commit status is pending: {name}"
    return "ready", "All expected current checks are successful"


def decide(context, snapshot, policy, intent):
    try:
        eligible(context, snapshot, policy)
        pr = snapshot["pr"]
        if pr["queued"]:
            return "queued", "Existing queue entry remains untouched"
        check_intent(context, snapshot, policy, intent)
        if not any(rule["type"] == "merge_queue" for rule in snapshot["rules"]):
            raise Blocked("No server-enforced merge queue is configured")
        expected = {item["name"]: item for item in policy["required_checks"]}
        minimum = policy.get("minimum_approvals", 2)
        for rule in snapshot["rules"]:
            if rule["type"] == "required_status_checks":
                for check in rule["parameters"]["required_status_checks"]:
                    requirement = expected.get(check["context"])
                    if (not requirement or check.get("integration_id") not in {None, requirement["app_id"]}):
                        raise Blocked("Active branch rules are not covered by the canonical inventory")
            if rule["type"] == "pull_request":
                minimum = max(minimum, rule["parameters"].get("required_approving_review_count", 0))
        if pr["mergeable"] != "MERGEABLE":
            return "waiting", "Mergeability is conflicting or not yet known"
        approvals = {review["author"] for review in snapshot["reviews"]
                     if review["state"] == "APPROVED" and review["commit"] == pr["head"]}
        if pr["review_decision"] != "APPROVED" or len(approvals) < minimum:
            return "waiting", "Current-head reviews are not yet sufficient"
        if any(not thread["resolved"] for thread in snapshot["threads"]):
            return "waiting", "Review threads remain unresolved"
        return evaluate_checks(context, snapshot, policy)
    except (Blocked, KeyError, TypeError, ValueError) as error:
        return "blocked", str(error)
