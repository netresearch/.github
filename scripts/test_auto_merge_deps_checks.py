"""Run the check gate of auto-merge-deps.yml against recorded check lists.

The step's own `run:` script and its CHECKS_SELECT program are read from the
workflow with yq and executed with `gh`, `date` and `sleep` replaced by stubs,
so the tests exercise the shipped text rather than a copy of it.

The fixtures under fixtures/auto-merge-deps/ are CHECKS_QUERY responses
recorded on 2026-10-31 from open pull requests:

- t3x-nr-vault-478: the pull request from #561, after its cancelled duplicate
  runs had been re-run by hand. CI ran twice (41155455079 and 41155455739).
- ldap-manager-717: one run whose "Companion browser checks" job was
  cancelled, with no run replacing it.
- t3x-contexts-221: two failing CI checks.
- t3x-nr-llm-1197: all checks green, commit status renovate/stability-days
  pending.

The state #561 describes no longer exists on GitHub, because the re-run
replaced it. The tests rebuild it from the t3x-nr-vault-478 recording.
"""

import copy
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/auto-merge-deps.yml"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures/auto-merge-deps"
STEP = '.jobs.auto-merge.steps[] | select(.name == "Enable auto-merge")'

OLD_CI_RUN = 41155455079
NEW_CI_RUN = 41155455739
# Check names #561 lists for the cancelled run: jobs cancelled before their
# matrix was expanded. No later run produces a check with these names.
ORPHANS = [
    "ci / Tests (PHP ${{ matrix.php }}, TYPO3 ${{ matrix.typo3 }})",
    "codeql / Analyze (${{ matrix.language }})",
    "ci / inputs.lowest-deps && 'Lowest dependencies' || "
    "(inputs.pin-packages != '' && 'Pinned dependencies' || 'Lowest or pinned dependencies')",
]

GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
d="$STUB_DIR"
case "$*" in
  "api graphql "*)
    # The flags the shipped call cannot do without: without --paginate only
    # the first 100 contexts are read, without --slurp the pages are not one
    # JSON array, and without the number nothing names the pull request.
    case " $* " in *" --paginate "*) ;; *) echo "graphql call without --paginate" >&2; exit 2 ;; esac
    case " $* " in *" --slurp "*) ;; *) echo "graphql call without --slurp" >&2; exit 2 ;; esac
    case " $* " in *" -F number=1 "*) ;; *) echo "graphql call without the PR number" >&2; exit 2 ;; esac
    n=$(cat "$d/graphql.count" 2>/dev/null || echo 0)
    n=$((n + 1)); echo "$n" > "$d/graphql.count"
    total=$(ls "$d"/poll-*.json | wc -l)
    [ "$n" -le "$total" ] || n=$total
    cat "$d/poll-$n.json" ;;
  "api repos/"*"/rules/branches/"*) echo '[]' ;;
  "api repos/"*) echo '{"allow_squash_merge":false,"allow_merge_commit":true,"allow_auto_merge":false}' ;;
  "pr view "*) echo false ;;
  "pr merge "*) echo "$* after poll $(cat "$d/graphql.count")" >> "$d/merges" ;;
  *) echo "unexpected gh call: $*" >&2; exit 2 ;;
esac
"""

# Each call advances the clock by one second; a one-minute deadline therefore
# allows about thirty polls, which no scenario below needs.
DATE_STUB = r"""#!/usr/bin/env bash
n=$(cat "$STUB_DIR/clock" 2>/dev/null || echo 0)
echo $((n + 1)) > "$STUB_DIR/clock"
echo "$n"
"""


def yq(expression):
    return subprocess.run(["yq", expression, str(WORKFLOW)], check=True,
                          capture_output=True, text=True).stdout


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def nodes(pages):
    for page in pages:
        yield from page["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"]["contexts"]["nodes"]


def run_of(node):
    suite = node.get("checkSuite") or {}
    return ((suite.get("workflowRun") or {}).get("databaseId"))


def rewrite(pages, keep, change):
    """Copy recorded pages, dropping nodes `keep` rejects and editing the rest."""
    pages = copy.deepcopy(pages)
    for page in pages:
        contexts = page["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"]["contexts"]
        contexts["nodes"] = [change(node) or node for node in contexts["nodes"] if keep(node)]
    return pages


def cancel_old_run(node):
    if run_of(node) == OLD_CI_RUN:
        node["status"], node["conclusion"] = "COMPLETED", "CANCELLED"


def with_orphans(pages):
    template = next(node for node in nodes(pages) if run_of(node) == OLD_CI_RUN)
    for offset, name in enumerate(ORPHANS, start=1):
        orphan = copy.deepcopy(template)
        orphan.update(databaseId=template["databaseId"] - offset, name=name,
                      status="COMPLETED", conclusion="CANCELLED")
        pages[0]["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"]["contexts"]["nodes"].append(orphan)
    return pages


def issue_561_states():
    """The three states of t3x-nr-vault#478 the gate polled through."""
    recorded = load("t3x-nr-vault-478")
    first = with_orphans(rewrite(recorded, lambda n: run_of(n) != NEW_CI_RUN, cancel_old_run))

    def queue_new(node):
        cancel_old_run(node)
        if run_of(node) == NEW_CI_RUN:
            node["status"], node["conclusion"] = "IN_PROGRESS", None

    running = with_orphans(rewrite(recorded, lambda n: True, queue_new))
    settled = with_orphans(rewrite(recorded, lambda n: True, cancel_old_run))
    return first, running, settled


def select(pages):
    program = yq(STEP + " | .env.CHECKS_SELECT")
    return json.loads(subprocess.run(["jq", "-c", program], input=json.dumps(pages),
                                     check=True, capture_output=True, text=True).stdout)


def gate(polls):
    """Run the step script; return (exit code, output, merge calls, polls made)."""
    script = yq(STEP + " | .run")
    env_block = json.loads(yq(STEP + " | .env | to_json"))
    with tempfile.TemporaryDirectory() as stub_dir:
        stubs = pathlib.Path(stub_dir)
        for name, body in (("gh", GH_STUB), ("date", DATE_STUB), ("sleep", "#!/bin/sh\n")):
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        for index, pages in enumerate(polls, start=1):
            (stubs / f"poll-{index}.json").write_text(json.dumps(pages))
        env = {
            "PATH": f"{stub_dir}:{os.environ['PATH']}",
            "STUB_DIR": stub_dir,
            "RUNNER_TEMP": stub_dir,
            "PR_URL": "https://github.com/netresearch/example/pull/1",
            "GH_TOKEN": "stub",
            "REPO": "netresearch/example",
            "BASE_REF": "main",
            "STRATEGY_OVERRIDE": "",
            "CHECKS_TIMEOUT_MINUTES": "1",
            "PR_NUMBER": "1",
            "CHECKS_QUERY": env_block["CHECKS_QUERY"],
            "CHECKS_SELECT": env_block["CHECKS_SELECT"],
        }
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                                timeout=60)
        merges = (stubs / "merges").read_text().splitlines() if (stubs / "merges").exists() else []
        count = int((stubs / "graphql.count").read_text()) if (stubs / "graphql.count").exists() else 0
        return result.returncode, result.stdout + result.stderr, merges, count


class SelectTest(unittest.TestCase):
    def test_settled_recording_passes(self):
        summary = select(load("t3x-nr-vault-478"))
        self.assertEqual((summary["failed"], summary["cancelled"], summary["pending"]), ([], [], []))
        self.assertGreater(summary["total"], 0)

    def test_superseded_run_and_its_orphans_are_dropped(self):
        first, running, settled = issue_561_states()
        summary = select(settled)
        self.assertEqual((summary["failed"], summary["cancelled"], summary["pending"]), ([], [], []))
        summary = select(running)
        self.assertEqual(summary["cancelled"], [])
        self.assertIn("ci / All CI checks", summary["pending"])

    def test_cancelled_run_without_replacement_is_kept(self):
        first, _, _ = issue_561_states()
        summary = select(first)
        self.assertEqual(summary["failed"], [])
        for orphan in ORPHANS:
            self.assertIn(orphan, summary["cancelled"])
        self.assertEqual(select(load("ldap-manager-717"))["cancelled"], ["Companion browser checks"])

    def test_failing_checks_are_reported(self):
        self.assertEqual(select(load("t3x-contexts-221"))["failed"], ["ci / All CI checks", "ci / Rector"])

    def test_pending_commit_status_is_kept(self):
        summary = select(load("t3x-nr-llm-1197"))
        self.assertEqual((summary["failed"], summary["cancelled"]), ([], []))
        self.assertEqual(summary["pending"], ["renovate/stability-days"])

    def test_own_check_is_excluded(self):
        names = [node["name"] for node in nodes(load("t3x-nr-vault-478")) if "name" in node]
        self.assertIn("auto-merge / Auto-merge dependency PRs", names)
        summary = select(rewrite(load("t3x-nr-vault-478"), lambda n: True,
                                 lambda n: n.update(status="IN_PROGRESS", conclusion=None)
                                 if n.get("name", "").endswith("Auto-merge dependency PRs") else None))
        self.assertEqual(summary["pending"], [])

    def test_run_for_another_event_does_not_supersede(self):
        recorded = load("t3x-nr-vault-478")
        cancelled = next(node for node in nodes(recorded) if node.get("name") == "ci / All CI checks"
                         and run_of(node) == NEW_CI_RUN)
        cancelled["conclusion"] = "CANCELLED"
        other_event = copy.deepcopy(cancelled)
        other_event.update(databaseId=cancelled["databaseId"] + 1000, name="ci / Other event",
                           conclusion="SUCCESS")
        other_event["checkSuite"]["workflowRun"].update(databaseId=NEW_CI_RUN + 1000,
                                                        event="pull_request_target")
        recorded[0]["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"]["contexts"]["nodes"].append(other_event)
        self.assertEqual(select(recorded)["cancelled"], ["ci / All CI checks"])

    def test_failure_of_superseded_run_counts_without_a_successor(self):
        # The newer run does not run the failing job at all (a job `if:` that
        # depends on the event action, say), so nothing replaces the failure.
        recorded = load("t3x-nr-vault-478")

        def change(node):
            if run_of(node) == OLD_CI_RUN and node.get("name") == "ci / All CI checks":
                node["conclusion"] = "FAILURE"

        def keep(node):
            return not (run_of(node) == NEW_CI_RUN and node.get("name") == "ci / All CI checks")

        self.assertEqual(select(rewrite(recorded, keep, change))["failed"], ["ci / All CI checks"])

    def test_failure_of_superseded_run_is_replaced_by_a_successor(self):
        recorded = rewrite(load("t3x-nr-vault-478"), lambda n: True,
                           lambda n: n.update(conclusion="FAILURE") if run_of(n) == OLD_CI_RUN
                           and n.get("conclusion") == "SUCCESS" else None)
        self.assertEqual(select(recorded)["failed"], [])

    def test_commit_status_and_check_of_same_name_both_count(self):
        recorded = load("t3x-nr-llm-1197")
        contexts = recorded[0]["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"]["contexts"]["nodes"]
        contexts.append({"__typename": "StatusContext", "context": "shared", "state": "FAILURE"})
        contexts.append({"__typename": "CheckRun", "databaseId": 1, "name": "shared", "status": "COMPLETED",
                         "conclusion": "SUCCESS", "checkSuite": {"workflowRun": None}})
        self.assertEqual(select(recorded)["failed"], ["shared"])

    def test_rerun_attempt_replaces_earlier_check_of_same_run(self):
        recorded = load("t3x-nr-vault-478")
        target = next(node for node in nodes(recorded) if node.get("name") == "ci / All CI checks"
                      and run_of(node) == NEW_CI_RUN)
        earlier = copy.deepcopy(target)
        earlier.update(databaseId=target["databaseId"] - 1, conclusion="FAILURE")
        recorded[0]["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"]["contexts"]["nodes"].append(earlier)
        self.assertEqual(select(recorded)["failed"], [])


class GateTest(unittest.TestCase):
    def test_issue_561_merges_once_the_replacing_run_is_green(self):
        first, running, settled = issue_561_states()
        code, output, merges, polls = gate([first, running, settled])
        self.assertEqual(code, 0, output)
        self.assertEqual(merges, ["pr merge --merge https://github.com/netresearch/example/pull/1 after poll 3"],
                         output)
        self.assertIn("Waiting for a run to replace", output)

    def test_unreplaced_cancellation_is_not_merged(self):
        code, output, merges, polls = gate([load("ldap-manager-717")])
        self.assertEqual((code, merges), (0, []), output)
        self.assertIn("did not all pass within 1 minutes", output)
        self.assertGreater(polls, 1)

    def test_failure_stops_on_the_first_poll(self):
        code, output, merges, polls = gate([load("t3x-contexts-221")])
        self.assertEqual((code, merges, polls), (0, [], 1), output)
        self.assertIn("2 check(s) failed", output)

    def test_pending_status_is_not_merged(self):
        code, output, merges, polls = gate([load("t3x-nr-llm-1197")])
        self.assertEqual((code, merges), (0, []), output)
        self.assertIn("Waiting for 1 check(s)", output)

    def test_own_check_alone_is_merged(self):
        own = rewrite(load("ldap-manager-717"), lambda n: n.get("name", "").endswith(
            "Auto-merge dependency PRs"), lambda n: n.update(status="IN_PROGRESS", conclusion=None))
        self.assertEqual(len(list(nodes(own))), 1)
        code, output, merges, polls = gate([own])
        self.assertEqual((code, len(merges), polls), (0, 1, 1), output)

    def test_empty_check_list_is_not_merged(self):
        empty = rewrite(load("ldap-manager-717"), lambda n: False, lambda n: None)
        code, output, merges, polls = gate([empty])
        self.assertEqual((code, merges), (0, []), output)
        self.assertIn("No checks reported yet", output)


if __name__ == "__main__":
    unittest.main()
