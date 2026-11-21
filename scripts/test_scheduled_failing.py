"""Tests for scheduled-failing.py: which workflows count as failing, and the issue body."""

import importlib.util
import pathlib
import unittest
from datetime import UTC, datetime

_spec = importlib.util.spec_from_file_location(
    "scheduled_failing", pathlib.Path(__file__).with_name("scheduled-failing.py")
)
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def run(
    workflow_id,
    day,
    conclusion,
    name="Mutation Testing",
    path=".github/workflows/mutation.yml",
):
    return {
        "workflow_id": workflow_id,
        "name": name,
        "path": path,
        "created_at": f"2026-11-{day:02d}T02:07:00Z",
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/{workflow_id}{day}",
    }


class FailingWorkflows(unittest.TestCase):
    def test_two_red_runs_in_a_row_are_reported(self):
        # The case this exists for: ofelia's weekly mutation run, cancelled at
        # the 2-hour limit on 2026-11-08 and 2026-11-15.
        runs = [run(1, 15, "cancelled"), run(1, 8, "cancelled"), run(1, 1, "success")]
        got = sf.failing_workflows(runs, 2, 30, NOW)
        self.assertEqual([w["path"] for w in got], [".github/workflows/mutation.yml"])
        self.assertEqual(
            [r["date"] for r in got[0]["runs"]], ["2026-11-15", "2026-11-08"]
        )

    def test_a_green_latest_run_clears_the_workflow(self):
        runs = [run(1, 15, "success"), run(1, 8, "failure"), run(1, 1, "failure")]
        self.assertEqual(sf.failing_workflows(runs, 2, 30, NOW), [])

    def test_one_red_run_is_not_enough(self):
        runs = [run(1, 15, "failure"), run(1, 8, "success")]
        self.assertEqual(sf.failing_workflows(runs, 2, 30, NOW), [])

    def test_fewer_runs_than_required_are_not_reported(self):
        self.assertEqual(sf.failing_workflows([run(1, 15, "failure")], 2, 30, NOW), [])

    def test_runs_outside_the_window_are_ignored(self):
        runs = [run(1, 15, "failure"), run(1, 1, "failure")]
        self.assertEqual(sf.failing_workflows(runs, 2, 10, NOW), [])

    def test_workflows_are_judged_separately(self):
        runs = [
            run(1, 15, "failure"),
            run(1, 8, "failure"),
            run(2, 15, "success", name="CI", path=".github/workflows/ci.yml"),
            run(2, 8, "failure", name="CI", path=".github/workflows/ci.yml"),
        ]
        got = sf.failing_workflows(runs, 2, 30, NOW)
        self.assertEqual([w["name"] for w in got], ["Mutation Testing"])

    def test_every_red_conclusion_counts(self):
        for conclusion in sorted(sf.RED):
            runs = [run(1, 15, conclusion), run(1, 8, conclusion)]
            self.assertEqual(len(sf.failing_workflows(runs, 2, 30, NOW)), 1, conclusion)
        runs = [run(1, 15, "skipped"), run(1, 8, "failure")]
        self.assertEqual(sf.failing_workflows(runs, 2, 30, NOW), [])


class IssueBody(unittest.TestCase):
    def test_lists_each_run_with_its_link(self):
        wfs = sf.failing_workflows(
            [run(1, 15, "cancelled"), run(1, 8, "cancelled")], 2, 30, NOW
        )
        body = sf.issue_body("netresearch/ofelia", wfs, 2, NOW)
        self.assertIn("Repo: `netresearch/ofelia`", body)
        self.assertIn(
            "[2026-11-15 cancelled](https://github.com/o/r/actions/runs/115)", body
        )
        self.assertIn("(`.github/workflows/mutation.yml`)", body)

    def test_escapes_a_pipe_in_the_workflow_name(self):
        wfs = sf.failing_workflows(
            [run(1, 15, "failure", name="a|b"), run(1, 8, "failure", name="a|b")],
            2,
            30,
            NOW,
        )
        self.assertIn("| a\\|b (", sf.issue_body("o/r", wfs, 2, NOW))


class IssueSync(unittest.TestCase):
    """issue_sweep.sync, shared with renovate-stuck.py, run in dry-run mode."""

    def _sync(self, found, existing):
        import contextlib
        import io

        import issue_sweep

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            issue_sweep.sync(
                "t", "Prefix: ", "label", found, existing, lambda r: f"closed {r}", True
            )
        return out.getvalue().splitlines()

    def test_opens_updates_and_closes(self):
        lines = self._sync(
            {"o/new": ("body", "1 x"), "o/kept": ("body", "2 x")},
            {"o/kept": 7, "o/gone": 9},
        )
        self.assertEqual(
            lines,
            [
                "update #7: Prefix: o/kept (2 x)",
                "open: Prefix: o/new (1 x)",
                "close #9: Prefix: o/gone",
            ],
        )

    def test_nothing_found_closes_every_open_issue(self):
        self.assertEqual(self._sync({}, {"o/a": 1}), ["close #1: Prefix: o/a"])


if __name__ == "__main__":
    unittest.main()
