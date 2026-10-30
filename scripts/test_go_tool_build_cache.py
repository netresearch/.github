"""Offline cache eligibility, trust, fallback and unchanged-check contracts."""

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from drift_compare import WorkflowLoader

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/go-tool-build-cache"
SPEC = importlib.util.spec_from_file_location("tool_cache", ACTION / "cache.py")
CACHE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CACHE)
WORKFLOW = yaml.load((ROOT / ".github/workflows/go-check.yml").read_text(), Loader=WorkflowLoader)
DESCRIPTOR = yaml.load((ACTION / "action.yml").read_text(), Loader=WorkflowLoader)


def evaluate_save_condition(source):
    """Interpret only literal equality conjunctions from the substituted policy."""
    root = ast.parse(source, mode="eval").body
    if not isinstance(root, ast.BoolOp) or not isinstance(root.op, ast.And):
        raise ValueError("expected the reviewed AND policy")
    values = []
    for term in root.values:
        if isinstance(term, ast.Constant) and isinstance(term.value, bool):
            values.append(term.value)
        elif (isinstance(term, ast.Compare) and len(term.ops) == 1 and
              isinstance(term.left, ast.Constant) and isinstance(term.comparators[0], ast.Constant)):
            equal = term.left.value == term.comparators[0].value
            if not isinstance(term.ops[0], (ast.Eq, ast.NotEq)):
                raise ValueError("unsupported comparison")
            values.append(equal if isinstance(term.ops[0], ast.Eq) else not equal)
        else:
            raise ValueError("unsupported policy syntax")
    return all(values)


class ToolBuildCacheTest(unittest.TestCase):
    """Keep cache speedups unable to replace installs, scans or source identity."""

    def setUp(self):
        """Use fixed compiler/module metadata with no downloads or hosted cache."""
        self.environment = {"TOOL_KIND": "gosec", "TOOL_TARGET": WORKFLOW["jobs"]["gosec"]["env"]["GOSEC_TARGET"],
                            "CONFIGURED_GO": "1.27.3", "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64",
                            "RUNNER_TEMP": "/offline-fixture", "ImageOS": "ubuntu24", "ImageVersion": "20261025.1"}
        self.metadata = {"GOVERSION": "go1.27.3", "GOTOOLCHAIN": "local", "GOOS": "linux", "GOARCH": "amd64",
                         "CGO_ENABLED": "1", "GOAMD64": "v1", "GOARM": "", "GOARM64": "",
                         "GOBIN": "/tools", "GOPATH": "/go"}
        self.module = {"GoVersion": "1.26.0", "Version": "v2.29.1-0.20261005092323-d2b649ec0182"}

    def prepare(self, environment=None, metadata=None, module=None):
        """Exercise the real key policy with controlled Go metadata only."""
        with patch.dict(os.environ, self.environment | (environment or {}), clear=True), \
             patch.object(CACHE, "go_json", side_effect=[self.metadata | (metadata or {}), self.module | (module or {})]):
            return CACHE.prepare()

    def install(self, *, first=0, second=0, compiler="go1.27.3", environment=None, paths=None):
        """Capture actual install calls and cache-failure retry environments."""
        env = self.environment | {"TOOL_CACHE_ENABLED": "true", "TOOL_BUILD_CACHE": "/install-only/build",
                                  "TOOL_MODULE_CACHE": "/install-only/modules", "EXPECTED_GO": "go1.27.3",
                                  "GOCACHE": "/original/build", "GOMODCACHE": "/original/modules"} | (environment or {})
        calls = []

        def run(command, **options):
            """Stub Go execution while retaining arguments and environment evidence."""
            calls.append((command, options))
            if command[1] == "install":
                count = sum(previous[0][1] == "install" for previous in calls)
                return subprocess.CompletedProcess(command, first if count == 1 else second)
            return subprocess.CompletedProcess(command, 0, stdout=f"/tools/gosec: {compiler}\n")

        with patch.dict(os.environ, env, clear=True), patch.object(CACHE.subprocess, "run", side_effect=run), \
             patch.object(CACHE, "go_json", return_value=paths or {"GOBIN": "/tools", "GOPATH": "/go"}):
            result = CACHE.install()
        return result, calls

    def test_exact_key_binds_compiler_image_platform_ref_and_helper(self):
        """Change each relevant identity independently and require a different key."""
        expected = self.prepare()
        self.assertEqual(self.prepare()["key"], expected["key"])
        variants = [self.prepare({"ImageVersion": "20261026.1"}),
                    self.prepare({"RUNNER_ARCH": "ARM64"}, {"GOARCH": "arm64"}),
                    self.prepare({"CONFIGURED_GO": "1.27.4"}, {"GOVERSION": "go1.27.4"}),
                    self.prepare(metadata={"GOAMD64": "v2"}),
                    self.prepare({"TOOL_TARGET": CACHE.TOOLS["gosec"][0] + "@" + "a" * 40})]
        for value in variants:
            self.assertNotEqual(value["key"], expected["key"])
        self.assertEqual(expected["build-cache"], "/offline-fixture/go-tool-build-cache/gosec/build")
        self.assertEqual(len(expected["source-sha256"]), 64)

    def test_unknown_mutable_or_ambiguous_toolchains_are_ineligible(self):
        """Refuse unsafe cache identities rather than guessing compiler selection."""
        for variant in ({"TOOL_TARGET": CACHE.TOOLS["gosec"][0] + "@latest"}, {"RUNNER_OS": "Windows"},
                        {"ImageVersion": ""}, {"CONFIGURED_GO": "1.27.2"}):
            with self.subTest(environment=variant), self.assertRaises(ValueError):
                self.prepare(variant)
        for variant in ({"GOTOOLCHAIN": "auto"}, {"GOTOOLCHAIN": "path"}, {"GOVERSION": "go1.27"},
                        {"GOVERSION": "devel go1.28"}, {"GOARCH": "arm64"}):
            with self.subTest(metadata=variant), self.assertRaises(ValueError):
                self.prepare(metadata=variant)
        for variant in ({"GoVersion": "1.28.0"}, {"GoVersion": ""}, {"Error": "unavailable"}):
            with self.subTest(module=variant), self.assertRaises(ValueError):
                self.prepare(module=variant)

    def test_cold_or_warm_cache_still_installs_and_compiler_switch_cannot_save(self):
        """Always run the same pinned install, including on cache hits."""
        (status, values), calls = self.install()
        self.assertEqual(status, 0)
        self.assertEqual(values["cache-saveable"], "true")
        self.assertEqual(calls[0][0], ["go", "install", self.environment["TOOL_TARGET"]])
        self.assertEqual(calls[0][1]["env"]["GOCACHE"], "/install-only/build")
        self.assertEqual(calls[0][1]["env"]["GOTOOLCHAIN"], "auto")
        self.assertEqual(self.install(compiler="go1.28.0")[0][1]["cache-saveable"], "false")

    def test_restore_and_corrupt_cache_failures_use_original_install_and_keep_real_failure(self):
        """Discard cache faults but preserve the original installer's red outcome."""
        (status, values), calls = self.install(first=17)
        self.assertEqual(status, 0)
        self.assertEqual(values["cache-saveable"], "false")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["env"]["GOCACHE"], "/original/build")
        self.assertEqual(calls[1][0], calls[0][0])
        self.assertEqual(self.install(first=17, second=23)[0][0], 23)
        (status, values), calls = self.install(environment={"TOOL_CACHE_ENABLED": "false"})
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["env"]["GOMODCACHE"], "/original/modules")
        self.assertEqual(values["cache-saveable"], "false")

    def test_executable_destination_cannot_overlap_either_cache_root(self):
        """Use the original installer/no-save for explicit or GOPATH-bin overlap."""
        for root in ("build", "modules"):
            destination = f"/offline-fixture/go-tool-build-cache/gosec/{root}"
            with self.subTest(root=root), self.assertRaises(ValueError):
                self.prepare(metadata={"GOBIN": destination})
            paths = {"GOBIN": f"/install-only/{root}", "GOPATH": "/go"}
            (status, values), calls = self.install(paths=paths)
            self.assertEqual(status, 0)
            self.assertEqual(values["cache-saveable"], "false")
            self.assertEqual(calls[0][1]["env"]["GOCACHE"], "/original/build")
        self.assertFalse(CACHE.install_paths_disjoint({"GOBIN": "", "GOPATH": "/cache:/other"},
                                                     "/cache/bin/build", "/modules"))
        self.assertTrue(CACHE.install_paths_disjoint({"GOBIN": "", "GOPATH": "/first:/cache"},
                                                    "/cache/bin/build", "/modules"))
        for paths in ({"GOBIN": "", "GOPATH": ""}, {"GOBIN": "relative", "GOPATH": "/go"}):
            self.assertFalse(CACHE.install_paths_disjoint(paths, "/build", "/modules"))

    def test_symlink_resolved_overlap_is_ineligible(self):
        """Reject aliases hiding an executable destination inside cached data."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            binary = base / "bin"
            binary.mkdir()
            linked = base / "build-link"
            linked.symlink_to(binary, target_is_directory=True)
            self.assertFalse(CACHE.install_paths_disjoint({"GOBIN": str(binary), "GOPATH": "/go"},
                                                         str(linked), str(base / "modules")))

    def test_default_off_native_checks_and_trusted_postscan_save_policy(self):
        """Keep independent jobs and require successful scans before trusted saves."""
        self.assertIs(WORKFLOW["on"]["workflow_call"]["inputs"]["enable-tool-build-cache"]["default"], False)
        self.assertEqual(len(WORKFLOW["jobs"]), 9)
        self.assertEqual(WORKFLOW["on"]["workflow_call"]["inputs"]["coverage-threshold"]["default"], 80.0)
        for job in ("gosec", "license-check"):
            steps = WORKFLOW["jobs"][job]["steps"]
            save = steps[-1]
            for required in ("success()", "github.event_name == 'push'", "github.event.repository.default_branch",
                             "cache-saveable == 'true'", "cache-hit != 'true'", "github.ref_protected == true"):
                self.assertIn(required, save["if"])
            self.assertNotIn("restore-keys", save["with"])
            positive_paths = [line for line in save["with"]["path"].splitlines() if not line.startswith("!")]
            self.assertEqual(positive_paths, ["${{ steps.tool-cache.outputs.build-cache }}",
                                             "${{ steps.tool-cache.outputs.module-cache }}/cache/download"])
            self.assertIn("!${{ steps.tool-cache.outputs.module-cache }}/cache/download/golang.org/toolchain", save["with"]["path"])
            helper = next(item for item in steps if item.get("id") == "tool-cache")
            self.assertEqual(helper["uses"], "netresearch/.github/.github/actions/go-tool-build-cache@main")
            self.assertEqual(steps[-2]["name"], "Upload gosec SARIF" if job == "gosec" else "Run license check")
        restore = next(item for item in DESCRIPTOR["runs"]["steps"] if item.get("id") == "restore")
        self.assertNotIn("restore-keys", restore["with"])
        installer = DESCRIPTOR["runs"]["steps"][-1]
        self.assertIn("cache-matched-key == steps.plan.outputs.key", installer["env"]["TOOL_CACHE_ENABLED"])

    def test_actual_save_expression_rejects_unprotected_or_unsuccessful_runs(self):
        """Evaluate the source condition's protected-push and scan-failure boundaries."""
        for job in ("gosec", "license-check"):
            expression = WORKFLOW["jobs"][job]["steps"][-1]["if"][3:-3].strip()
            for protected, succeeded, event, ref, expected in (
                (True, True, "push", "refs/heads/main", True),
                (False, True, "push", "refs/heads/main", False),
                (True, False, "push", "refs/heads/main", False),
                (True, True, "pull_request", "refs/heads/main", False),
                (True, True, "merge_group", "refs/heads/main", False),
                (True, True, "push", "refs/heads/other", False)):
                source = expression
                replacements = {"format('refs/heads/{0}', github.event.repository.default_branch)": repr("refs/heads/main"),
                                "success()": repr(succeeded), "inputs.enable-tool-build-cache": "True",
                                "steps.tool-cache.outputs.cache-saveable": repr("true"),
                                "steps.tool-cache.outputs.cache-hit": repr("false"),
                                "github.event_name": repr(event), "github.ref_protected": repr(protected),
                                "github.ref": repr(ref)}
                for key, value in replacements.items():
                    source = source.replace(key, value)
                source = source.replace("&&", "and").replace("== true", "== True")
                self.assertEqual(evaluate_save_condition(source), expected)

    def test_single_immutable_pin_remains_tracked_by_renovate(self):
        """Ensure both install modes/cache identity share the maintained gosec pin."""
        configuration = json.loads((ROOT / "renovate.json").read_text())
        pattern = configuration["customManagers"][0]["matchStrings"][0]
        pattern = pattern.replace("(?<currentDigest>", "(?P<currentDigest>").replace("(?<currentValue>", "(?P<currentValue>")
        match = re.search(pattern, (ROOT / ".github/workflows/go-check.yml").read_text())
        self.assertIsNotNone(match)
        self.assertEqual(match.group("currentDigest"), self.environment["TOOL_TARGET"].rsplit("@", 1)[1])
        self.assertEqual(match.group("currentValue"), "master")


if __name__ == "__main__":
    unittest.main()
