"""Bind checks to server-owned Actions jobs; cache only inside one snapshot."""
import re

from api_target import ApiError, positive_id


def json_id(value):
    if type(value) is not int:
        raise ApiError("GitHub identity must be a positive JSON integer")
    return positive_id(value)


def run_revision(run):
    return tuple(run[field] for field in ("id", "head_sha", "check_suite_id", "run_attempt", "updated_at"))


def inventory(api, run, view):
    repository_id = json_id(run["repository"].get("id"))
    key = run_revision(run) + (view,)
    if key in api.job_inventories:
        return api.job_inventories[key]
    rows = api.collection(f"/repos/{api.repository}/actions/runs/{run['id']}/jobs?filter={view}",
                          "jobs", require_total=True, repository_id=repository_id)
    result, ids = {}, set()
    prefix = re.escape(api.base + "/repos/" + api.repository)
    for row in rows:
        try:
            job_id = json_id(row["id"])
            match = re.fullmatch(prefix + r"/check-runs/([1-9][0-9]*)", row["check_run_url"])
            if (not match or row["run_id"] != run["id"] or type(row["run_id"]) is not int
                    or row["run_url"] != api.base + f"/repos/{api.repository}/actions/runs/{run['id']}"
                    or row["head_sha"] != run["head_sha"]
                    or type(row["run_attempt"]) is not int or not 1 <= row["run_attempt"] <= run["run_attempt"]
                    or not isinstance(row["name"], str) or not row["name"]
                    or not isinstance(row["status"], str) or not row["status"]
                    or (row["conclusion"] is not None and not isinstance(row["conclusion"], str))):
                raise ApiError("Actions job identity or attempt did not verify")
            check_id = int(match[1])
            if job_id in ids or check_id in result:
                raise ApiError("Duplicate Actions job/check identity")
            ids.add(job_id)
            result[check_id] = {name: row[name] for name in (
                "id", "run_id", "run_url", "head_sha", "run_attempt", "name", "status", "conclusion", "check_run_url")}
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("Actions job metadata is incomplete or malformed") from error
    api.job_inventories[key] = result
    return result


def bind_check(api, check, run, expected_sha, linked_job):
    try:
        if (json_id(check["check_suite"]["id"]) != json_id(run["check_suite_id"])
                or check["head_sha"] != run["head_sha"] or check["head_sha"] != expected_sha):
            raise ApiError("Check suite/head does not match its exact producer")
        check_id = json_id(check["id"])
        latest = inventory(api, run, "latest")
        current = check_id in latest
        jobs = latest
        if not current:
            jobs = inventory(api, run, "all")
            if any(jobs.get(identity) != job for identity, job in latest.items()):
                raise ApiError("Latest/all job inventories are not coherent")
        job = jobs.get(check_id)
        if (job is None or job["name"] != check["name"]
                or (linked_job is not None and job["id"] != linked_job)
                or (job["status"], job["conclusion"]) != (check["status"], check["conclusion"])):
            raise ApiError("Check is not bound to the matching server-owned Actions job")
        return job, current
    except (KeyError, TypeError, ValueError) as error:
        raise ApiError("Check/job provenance metadata is incomplete or malformed") from error
