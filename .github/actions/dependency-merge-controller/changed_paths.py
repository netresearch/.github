"""Bounded, immutable path evidence and literal conditional policy; no I/O."""

import re

from api_target import git_ref


ANCHOR_FIELDS = """number headRefOid headRefName baseRefOid baseRefName changedFiles
    headRepository{nameWithOwner} baseRepository{nameWithOwner} potentialMergeCommit{oid}
    baseRef{name prefix repository{nameWithOwner} target{__typename oid}}"""
ANCHOR_KEYS = {"repository", "number", "head", "head_ref", "base", "base_ref",
               "pr_base_oid", "test_merge", "changed_files"}


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Missing or malformed immutable Git identity")
    return value


def count(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Changed-path evidence count is missing, invalid or capped")
    return value


def path(value, prefix=False):
    if not isinstance(value, str) or (prefix and not value.endswith("/")):
        raise ValueError("Expected a literal repository-relative path")
    clean = value[:-1] if prefix else value
    if (not re.fullmatch(r"[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*", clean)
            or any(part in {".", ".."} for part in clean.split("/"))):
        raise ValueError("Unsupported or ambiguous literal repository path")
    return value


def validate_rule(paths):
    if not isinstance(paths, dict) or set(paths) != {"exact", "prefix"}:
        raise ValueError("Conditional paths require exact and prefix lists")
    for kind in ("exact", "prefix"):
        values = paths[kind]
        if not isinstance(values, list):
            raise ValueError("Conditional paths must be literal string lists")
        for value in values:
            path(value, prefix=kind == "prefix")
        if len(values) != len(set(values)):
            raise ValueError("Duplicate conditional path")
    if not paths["exact"] and not paths["prefix"]:
        raise ValueError("Conditional paths cannot be empty")


def conditional_definition(entry):
    if (not isinstance(entry, dict)
            or set(entry) != {"name", "app_id", "workflow", "events", "paths", "workflow_blob_oid"}
            or not entry["name"] or entry["events"] != ["pull_request"]):
        raise ValueError("Invalid version-2 conditional-check declaration")
    sha(entry["workflow_blob_oid"])
    validate_rule(entry["paths"])
    return entry["paths"], entry["workflow_blob_oid"], entry["events"]


def validate_conditions(policy):
    git_ref(policy["base_ref"])
    entries = policy["conditional_checks"]
    if (not isinstance(entries, list) or not entries
            or not isinstance(policy["required_checks"], list)
            or not all(isinstance(entry, dict) for entry in policy["required_checks"])):
        raise ValueError("Version 2 requires explicit unconditional and conditional check lists")
    workflows = {}
    for entry in entries:
        definition = conditional_definition(entry)
        if entry["workflow"] in workflows and workflows[entry["workflow"]] != definition:
            raise ValueError("One workflow has inconsistent applicability declarations")
        workflows[entry["workflow"]] = definition


def workflow_pins(policy):
    return {entry["workflow"]: entry["workflow_blob_oid"] for entry in policy["conditional_checks"]}


def anchor(raw, repository, base_ref):
    ref = raw["baseRef"]
    if (not isinstance(ref, dict) or ref["name"] != raw["baseRefName"]
            or ref["name"] != base_ref or ref["prefix"] != "refs/heads/"
            or ref["repository"]["nameWithOwner"] != repository
            or ref["target"]["__typename"] != "Commit"
            or raw["baseRepository"]["nameWithOwner"] != repository
            or raw["headRepository"]["nameWithOwner"] != repository):
        raise ValueError("Current base ref identity or repository did not verify")
    return {"repository": repository, "number": count(raw["number"], 1, 2**63 - 1),
            "head": sha(raw["headRefOid"]), "head_ref": git_ref(raw["headRefName"]),
            "base": sha(ref["target"]["oid"]), "base_ref": ref["name"],
            "pr_base_oid": sha(raw["baseRefOid"]),
            "test_merge": sha(raw["potentialMergeCommit"]["oid"]) if raw["potentialMergeCommit"] else None,
            "changed_files": count(raw["changedFiles"], 1, 299)}


def comparison_history(raw, base, head):
    if sha(raw["base_commit"]["sha"]) != base:
        raise ValueError("Comparison base does not match the immutable requested base")
    merge_base = sha(raw["merge_base_commit"]["sha"])
    behind = count(raw["behind_by"], 0, 2**63 - 1)
    status = raw["status"]
    if not ((status == "ahead" and behind == 0 and merge_base == base)
            or (status == "diverged" and behind > 0 and merge_base not in {base, head})):
        raise ValueError("Unsupported or inconsistent comparison history")
    return status, merge_base


def comparison_commits(raw, head):
    total = count(raw["total_commits"], 1, 249)
    if count(raw["ahead_by"], 1, 249) != total or not isinstance(raw["commits"], list):
        raise ValueError("Comparison commit inventory is incomplete")
    commits = [sha(commit["sha"]) for commit in raw["commits"]]
    if len(commits) != total or len(set(commits)) != total or commits[-1] != head:
        raise ValueError("Comparison commits are incomplete, duplicated or bound to another head")
    return commits


def comparison_files(rows, changed_files):
    if not isinstance(rows, list) or count(len(rows), 1, 299) != changed_files:
        raise ValueError("Comparison file inventory does not match the complete PR count")
    files = []
    for row in rows:
        name = path(row["filename"])
        if row["status"] not in {"added", "modified", "removed"} or row.get("previous_filename") is not None:
            raise ValueError("Renamed or unsupported changed-file evidence")
        files.append((name, row["status"]))
    if len({name for name, _ in files}) != len(files):
        raise ValueError("Duplicate changed filename")
    return sorted(files)


def comparison(raw, base, head, changed_files):
    if not isinstance(raw, dict) or raw.get("too_large") or raw.get("truncated"):
        raise ValueError("Comparison is missing or explicitly truncated")
    status, merge_base = comparison_history(raw, base, head)
    return {"status": status, "merge_base": merge_base,
            "commits": comparison_commits(raw, head),
            "files": comparison_files(raw["files"], changed_files)}


def closing_query(repository, number, captured, policy):
    owner, repo = repository.split("/")
    variables = {"owner": owner, "repo": repo, "number": number}
    declarations, aliases = [], []
    for index, workflow in enumerate(sorted(workflow_pins(policy))):
        for prefix, revision in (("b", captured["base"]), ("h", captured["head"])):
            alias = f"{prefix}{index}"
            variables[alias] = revision + ":.github/workflows/" + workflow
            declarations.append(f",${alias}:String!")
            aliases.append(f"{alias}:object(expression:${alias}){{__typename oid}}")
    query = ("query($owner:String!,$repo:String!,$number:Int!" + "".join(declarations)
             + "){repository(owner:$owner,name:$repo){nameWithOwner pullRequest(number:$number){"
             + ANCHOR_FIELDS + "} " + " ".join(aliases) + "}}")
    return query, variables


def verify_closing(data, captured, policy):
    repository = data["repository"]
    if (repository["nameWithOwner"] != captured["repository"]
            or anchor(repository["pullRequest"], captured["repository"], policy["base_ref"]) != captured):
        raise ValueError("PR anchors changed during the applicability snapshot")
    for index, (_, oid) in enumerate(sorted(workflow_pins(policy).items())):
        for prefix in ("b", "h"):
            blob = repository[f"{prefix}{index}"]
            if not isinstance(blob, dict) or blob["__typename"] != "Blob" or sha(blob["oid"]) != oid:
                raise ValueError("Conditional workflow blob changed or did not verify")


def conditional_selection(paths, policy):
    return [entry for entry in policy["conditional_checks"]
            if any(value in entry["paths"]["exact"] or value.startswith(tuple(entry["paths"]["prefix"]))
                   for value in paths)]


def effective_checks(snapshot, policy, policy_digest):
    proof = snapshot["applicability"]
    pr = snapshot["pr"]
    if (snapshot["complete"].get("changed_files") is not True
            or snapshot["complete"].get("conditional_workflows") is not True
            or proof["policy_digest"] != policy_digest
            or proof["workflow_blobs"] != workflow_pins(policy)
            or pr["base_ref"] != policy["base_ref"]
            or set(proof["anchor"]) != ANCHOR_KEYS
            or any(proof["anchor"][key] != pr[key] for key in proof["anchor"])):
        raise ValueError("Applicability proof is incomplete or bound to another PR revision/policy")
    files = proof["paths"]
    if not isinstance(files, list) or count(len(files), 1, 299) != pr["changed_files"]:
        raise ValueError("Applicability paths are incomplete")
    if len({path(value) for value in files}) != len(files):
        raise ValueError("Applicability paths are duplicated")
    return policy["required_checks"] + conditional_selection(files, policy)
