"""Repository-bound endpoint grammar for authenticated GitHub requests."""

import re
import urllib.parse


class ApiError(Exception):
    """An API response or target is unavailable, incomplete or untrusted."""


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value), flags=re.ASCII):
        raise ApiError("Expected a positive numeric GitHub identifier")
    return int(value)


def git_ref(value):
    # A single decode is deliberate: literal '%' and '#' are valid ref data.
    if (not value or re.search(r"[\x00-\x20\x7f\\~^:?*\[]", value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ApiError("Invalid Git ref in API target")
    return value


ROUTES = (
    ("GET", r"actions/runs/[1-9][0-9]*", "single"),
    ("GET", r"actions/runs", "runs"),
    ("GET", r"actions/artifacts", "artifacts"),
    ("GET", r"actions/artifacts/[1-9][0-9]*/zip", "single"),
    ("GET", r"commits/[0-9a-f]{40}/statuses", "list"),
    ("GET", r"commits/[0-9a-f]{40}/check-runs", "checks"),
    ("GET", r"rules/branches/[^/]+", "branch"),
    ("GET", r"pulls", "pulls"),
    ("POST", r"pulls/[1-9][0-9]*/reviews", "single"),
)
QUERY_FIELDS = {
    "single": {}, "list": {}, "branch": {},
    "runs": {"head_sha": r"[0-9a-f]{40}"},
    "checks": {"filter": "all"},
    "artifacts": {"name": r"merge-intent-pr-[1-9][0-9]*-[0-9a-f]{40}"},
    "pulls": {"state": "open", "head": r"[^\x00-\x20\x7f]+"},
}


def query_pairs(kind, query, owner):
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ApiError("Malformed API query parameters") from error
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ApiError("Duplicate API query parameters are forbidden")
    permitted = QUERY_FIELDS[kind] | ({"per_page": r"[1-9][0-9]?|100", "page": r"[1-9][0-9]*"}
                                     if kind != "single" else {})
    for key, value in pairs:
        if key not in permitted or not re.fullmatch(permitted[key], value):
            raise ApiError("Unexpected API query field or value")
    if QUERY_FIELDS[kind].keys() - fields.keys():
        raise ApiError("Required API query fields are missing")
    if kind == "pulls":
        head_owner, separator, branch = fields["head"].partition(":")
        if head_owner != owner or not separator:
            raise ApiError("PR lookup must remain in the current repository owner")
        git_ref(branch)
    return pairs


def target_url(base, repository, path, method):
    if not isinstance(path, str) or re.search(r"[\x00-\x20\x7f\\#]", path):
        raise ApiError("Invalid characters in API target")
    parts = urllib.parse.urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment:
        raise ApiError("API target must be a relative path without a fragment")
    if parts.path == "/graphql" and method == "POST" and not parts.query:
        return base + "/graphql"
    prefix = "/repos/" + repository + "/"
    if not parts.path.startswith(prefix):
        raise ApiError("API target leaves the current repository")
    resource = parts.path[len(prefix):]
    kind = next((kind for verb, pattern, kind in ROUTES
                 if verb == method and re.fullmatch(pattern, resource)), None)
    if kind is None:
        raise ApiError("API endpoint is outside the controller's explicit contract")
    if kind == "branch":
        raw_ref = resource.removeprefix("rules/branches/")
        ref = git_ref(urllib.parse.unquote(raw_ref, errors="strict"))
        if urllib.parse.quote(ref, safe="") != raw_ref:
            raise ApiError("Branch path is not canonically encoded")
    pairs = query_pairs(kind, parts.query, repository.split("/")[0])
    query = "?" + urllib.parse.urlencode(pairs) if pairs else ""
    return base + prefix + resource + query
