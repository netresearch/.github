"""Real CLI regressions for a consumer's strict pin-drift metadata opt-in."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from drift_compare import GO_CONCURRENCY_VARIANTS
import test_drift_compare as checks_fixtures
import test_drift_compare_go as go_fixtures
from test_drift_compare_pins import ACTION, REUSABLE, OLD, NEW, PIN_NOTICE, workflow

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/demo.yml"
STRICT_NOTICE = "Strict pin comparison enabled"


def compare(template, consumer, metadata="strict-pins: true\n", *, metadata_kind="file",
            wrapper=False, actions=False, template_name="skill", path=WORKFLOW):
    """Exercise the actual entry point with isolated governed content and metadata."""
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        consumer_root, template_root = base / "consumer", base / "templates" / template_name
        for root, text in ((template_root, template), (consumer_root, consumer)):
            target = root / path
            target.parent.mkdir(parents=True)
            if text is not None:
                target.write_text(text)
        meta = consumer_root / ".github/template.yaml"
        if metadata_kind == "directory":
            meta.mkdir()
        elif metadata_kind in {"escape", "broken", "internal"}:
            target = base / "outside.yaml" if metadata_kind == "escape" else meta.parent / "inside.yaml"
            if metadata_kind != "broken":
                target.write_text(metadata)
            meta.symlink_to(target)
        elif metadata is not None:
            meta.write_text(metadata)
            if metadata_kind == "unreadable":
                meta.chmod(0)
        command = (["bash", str(ROOT / "scripts/check-drift.sh")] if wrapper else
                   [sys.executable, str(ROOT / "scripts/drift_compare.py")])
        environment = os.environ | {"PYTHONDONTWRITEBYTECODE": "1",
                                    "GITHUB_ACTIONS": "true" if actions else ""}
        return subprocess.run(command + [str(consumer_root), str(template_root)],
                              capture_output=True, text=True, check=False, env=environment)


class StrictPinTest(unittest.TestCase):
    """An explicit true flag restores pin drift without changing the default."""

    def test_explicit_true_blocks_arbitrary_full_sha_at_both_locations(self):
        """Even a syntactically valid all-zero SHA must be ordinary drift."""
        for field, action in (("step", ACTION), ("job", REUSABLE)):
            changed = workflow(**{field: f"{action}@{'0' * 40}"})
            for template, consumer in ((workflow(), changed), (changed, workflow())):
                with self.subTest(field=field, reversed=template == changed):
                    result = compare(template, consumer)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(STRICT_NOTICE, result.stdout)
                    self.assertNotIn(PIN_NOTICE, result.stdout)

    def test_absent_false_empty_and_null_documents_keep_pin_notice(self):
        """Ordinary absence and empty documents preserve default tolerance."""
        for metadata in (None, "", "null\n", "~\n", "{}\n", "strict-pins: false\n"):
            with self.subTest(metadata=metadata):
                result = compare(workflow(), workflow(step=f"{ACTION}@{NEW}"), metadata)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(PIN_NOTICE, result.stdout)
                self.assertNotIn(STRICT_NOTICE, result.stdout)

    def test_true_preserves_equality_and_cosmetic_yaml(self):
        """Strict mode compares parsed workflows, preserving YAML 1.2 keys."""
        for text in (workflow(), "# consumer prose\n" + workflow().replace("\non:", '\n"on":')):
            result = compare(workflow(), text)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(STRICT_NOTICE, result.stdout)
            self.assertNotIn(PIN_NOTICE, result.stdout)

    def test_wrapper_and_workflow_annotations_execute_strict_mode(self):
        """The local wrapper and hosted CLI use the same validated setting."""
        for wrapper, actions in ((True, False), (False, True)):
            result = compare(workflow(), workflow(step=f"{ACTION}@{NEW}"),
                             wrapper=wrapper, actions=actions)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn(STRICT_NOTICE, result.stdout)
            self.assertEqual("::notice::" in result.stdout, actions)

    def test_invalid_flag_types_fail_before_clean_result(self):
        """Strings, numbers, nulls and containers cannot silently disable strictness."""
        for value in ('"true"', '"false"', "yes", "no", "on", "off", "0", "1",
                      "null", "~", "[]", "{}"):
            with self.subTest(value=value):
                result = compare(workflow(), workflow(), f"strict-pins: {value}\n")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("strict-pins must be a YAML boolean", result.stderr)
                self.assertNotIn("No drift detected", result.stdout)

    def test_explicit_boolean_tags_keep_type_based_selection(self):
        """Real tagged booleans select their value; invalid tags remain metadata errors."""
        changed = workflow(step=f"{ACTION}@{NEW}")
        for value, expected in (("true", 1), ("false", 0), ("yes", 1), ("no", 0)):
            with self.subTest(value=value):
                result = compare(workflow(), changed, f"strict-pins: !!bool {value}\n")
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual(STRICT_NOTICE in result.stdout, expected == 1)
        for tag, value in (("bool", "invalid"), ("int", "nope")):
            with self.subTest(tag=tag):
                result = compare(workflow(), workflow(), f"strict-pins: !!{tag} {value}\n")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_nonmapping_malformed_and_unreadable_metadata_fail_closed(self):
        """A clean governed tree never conceals invalid or inaccessible metadata."""
        for metadata in ("false\n", "0\n", "[]\n", "text\n", "[broken\n"):
            with self.subTest(metadata=metadata):
                result = compare(workflow(), workflow(), metadata)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("failed to parse template.yaml", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        for kind in ("directory", "unreadable", "escape", "broken"):
            with self.subTest(kind=kind):
                result = compare(workflow(), workflow(), metadata_kind=kind)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("failed to parse template.yaml", result.stderr)

    def test_duplicate_flags_direct_aliased_and_merged_are_rejected(self):
        """Neither declaration order nor YAML indirection chooses a winning flag."""
        for first, second in (("true", "false"), ("false", "true"), ("true", "true")):
            variants = (
                f"strict-pins: {first}\nstrict-pins: {second}\n",
                f"&flag strict-pins: {first}\n*flag: {second}\n",
                f"base: &base {{strict-pins: {first}}}\n<<: *base\nstrict-pins: {second}\n",
                f"base: &base {{strict-pins: {first}}}\nstrict-pins: {second}\n<<: *base\n",
                f"<<: [{{strict-pins: {first}}}, {{strict-pins: {second}}}]\n",
            )
            for metadata in variants:
                with self.subTest(metadata=metadata):
                    result = compare(workflow(), workflow(), metadata)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("duplicate strict-pins", result.stderr)

    def test_single_alias_merge_and_contained_symlink_keep_true(self):
        """Unambiguous YAML aliases and safe local metadata links remain supported."""
        variants = ("flag: &enabled true\nstrict-pins: *enabled\n",
                    "base: &base {strict-pins: true}\n<<: *base\n")
        for metadata in variants:
            for kind in ("file", "internal"):
                with self.subTest(metadata=metadata, kind=kind):
                    result = compare(workflow(), workflow(step=f"{ACTION}@{NEW}"),
                                     metadata, metadata_kind=kind)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(STRICT_NOTICE, result.stdout)

    def test_existing_intentional_paths_stay_explicit_exceptions(self):
        """Both existing list entry forms keep their declared path exemption."""
        for entry in (WORKFLOW, f"{{path: {WORKFLOW}}}"):
            metadata = f"strict-pins: true\nintentional-drift:\n  - {entry}\n"
            result = compare(workflow(), workflow(step="attacker/action@main"), metadata)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"  - {WORKFLOW}", result.stdout)
        result = compare(workflow(), workflow(), "strict-pins: true\nintentional-drift: bad\n")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_nested_metadata_does_not_become_a_root_flag(self):
        """Only the root opt-in controls drift; nested metadata keeps old loading."""
        result = compare(workflow(), workflow(step=f"{ACTION}@{NEW}"),
                         "extra: {strict-pins: true, strict-pins: false}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(PIN_NOTICE, result.stdout)

    def test_other_differences_and_missing_files_remain_drift(self):
        """Strictness never turns other ref forms or additional changes into a pass."""
        changed = (workflow(step="attacker/action@" + NEW), workflow(step=ACTION + "@main"),
                   workflow(step=f"{ACTION}@{NEW}") + "concurrency: arbitrary\n", None)
        for text in changed:
            with self.subTest(text=text):
                result = compare(workflow(), text)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn(PIN_NOTICE, result.stdout)

    def test_shared_workflow_alias_is_not_normalized_in_strict_mode(self):
        """A uses mapping shared with another field cannot escape whole-document drift."""
        template = f"jobs:\n  build:\n    steps:\n      - &step {{uses: {ACTION}@{OLD}}}\nenv: *step\n"
        result = compare(template, template.replace(OLD, NEW))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_frozen_checks_transition_keeps_both_strict_directions(self):
        """The existing complete-document transition remains independently allowed."""
        checks_fixtures.TransitionTest.setUpClass()
        fixtures = checks_fixtures.TransitionTest
        for template, consumer in ((fixtures.a, fixtures.b), (fixtures.b, fixtures.a)):
            result = compare(yaml.safe_dump(template), yaml.safe_dump(consumer),
                             template_name="typo3-extension", path=".github/workflows/checks.yml")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Approved Checks PR-concurrency transition", result.stdout)
            self.assertIn(STRICT_NOTICE, result.stdout)

    def test_frozen_go_transitions_keep_both_strict_directions(self):
        """Every existing Go template/path pair retains its exact transition contract."""
        fixtures = go_fixtures.GoTransitionTest()
        for name in ("go-app", "go-lib"):
            for path in GO_CONCURRENCY_VARIANTS:
                _current, before, after = fixtures.documents(name, path)
                for template, consumer in ((before, after), (after, before)):
                    with self.subTest(template=name, path=path):
                        result = compare(yaml.safe_dump(template), yaml.safe_dump(consumer),
                                         template_name=name, path=path)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("Approved Go PR-concurrency transition", result.stdout)


if __name__ == "__main__":
    unittest.main()
