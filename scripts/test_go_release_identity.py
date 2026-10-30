"""Run release-source shell guards against local synthetic tags; no publisher."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from drift_compare import WorkflowLoader

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    name: yaml.load((ROOT / ".github/workflows" / f"{name}.yml").read_text(), Loader=WorkflowLoader)
    for name in ("golib-create-release", "release-go-app")
}


def step(workflow, job, name):
    """Select the actual reusable step whose guard the fixture will execute."""
    return next(item for item in WORKFLOWS[workflow]["jobs"][job]["steps"] if item.get("name") == name)


class ReleaseIdentityTest(unittest.TestCase):
    """Exercise tag/provenance identity while blocking every real publisher."""

    def setUp(self):
        """Build isolated commits/tags and a CLI stub with no network path."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Offline release fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "first")
        self.sha = self.git("rev-parse", "HEAD")
        for tag in ("v1.2.3", "v1.2.4"):
            self.git("-c", "tag.gpgsign=false", "tag", "-a", tag, "-m", tag)
        self.git("-c", "tag.gpgsign=false", "tag", "v1.2.5")
        self.tag_object = self.git("rev-parse", "refs/tags/v1.2.3")
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "second")
        self.other_sha = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach", self.sha)
        binary = self.directory / "bin"
        binary.mkdir()
        fake_gh = binary / "gh"
        fake_gh.write_text("""#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = api ]; then
  [ "${REMOTE_FAILURE:-false}" != true ] || exit 1
  printf '%s\\n' "$REMOTE_TAG_OBJECT"
elif [ "${1:-} ${2:-}" = 'release edit' ]; then
  printf 'stub publisher only\\n' >> "$PUBLISH_MARKER"
elif [ "${1:-} ${2:-}" = 'release view' ]; then
  printf 'https://example.invalid/offline-fixture\\n'
else
  echo 'Unexpected command blocked by offline fixture' >&2
  exit 99
fi
""")
        fake_gh.chmod(0o755)
        self.environment = os.environ | {
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "INPUT_TAG": "v1.2.3", "REF_NAME": "v1.2.3",
            "EVENT_REF": "refs/tags/v1.2.3", "EVENT_REF_TYPE": "tag", "EVENT_SHA": self.sha,
            "TAG": "v1.2.3", "EXPECTED_SHA": self.sha, "EXPECTED_TAG_OBJECT": self.tag_object,
            "REMOTE_TAG_OBJECT": self.tag_object, "REPO": "offline/fixture",
            "GH_TOKEN": "not-a-real-token", "MAKE_LATEST": "false", "IS_PRE": "false",
            "PUBLISH_MARKER": str(self.directory / "publisher-was-stubbed"),
        }

    def git(self, *arguments):
        """Run Git only in the temporary fixture with hooks disabled."""
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *arguments],
                                       cwd=self.repo, text=True, stderr=subprocess.DEVNULL).strip()

    def execute(self, item, **environment):
        """Execute a source guard and capture its success-only identity outputs."""
        output = self.directory / "output"
        output.write_text("")
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", item["run"]], cwd=self.repo,
                                env=self.environment | {"GITHUB_OUTPUT": str(output)} | environment,
                                capture_output=True, text=True, check=False)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
        return result, values

    def bind_steps(self):
        """Return both library and application event-to-tag binding guards."""
        return (step("golib-create-release", "test", "Bind release tag to event source"),
                step("release-go-app", "preflight", "Resolve tag"))

    def test_tag_event_and_dispatch_at_selected_tag_preserve_sha_and_tag_object(self):
        """Accept the exact tag identity with an explicit or default selection."""
        for item in self.bind_steps():
            for override in ("", "v1.2.3"):
                result, values = self.execute(item, INPUT_TAG=override)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(values["tag"], "v1.2.3")
                self.assertEqual(values["sha"], self.sha)
                self.assertEqual(values["tag-object"], self.tag_object)

    def test_wrong_oidc_branch_other_tag_or_sha_fails_before_outputs(self):
        """Reject wrong event refs/commits and malformed or missing tag selections."""
        variants = ({"EVENT_REF": "refs/heads/main", "EVENT_REF_TYPE": "branch"},
                    {"EVENT_REF": "refs/tags/v1.2.4"}, {"EVENT_SHA": self.other_sha},
                    {"INPUT_TAG": "missing", "EVENT_REF": "refs/tags/missing"},
                    {"INPUT_TAG": "bad\ntag", "EVENT_REF": "refs/tags/bad\ntag"})
        for item in self.bind_steps():
            for variant in variants:
                with self.subTest(step=item["name"], variant=variant):
                    result, values = self.execute(item, **variant)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(values, {})
            self.git("checkout", "-q", "--detach", self.other_sha)
            self.assertNotEqual(self.execute(item)[0].returncode, 0)
            self.git("checkout", "-q", "--detach", self.sha)

    def test_library_still_refuses_lightweight_tag(self):
        """Retain the existing library annotated-tag requirement."""
        result, values = self.execute(self.bind_steps()[0], INPUT_TAG="v1.2.5",
                                      EVENT_REF="refs/tags/v1.2.5", REF_NAME="v1.2.5")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(values, {})

    def test_library_release_checkout_and_local_tag_must_match_tested_identity(self):
        """Reject a publishing checkout or local tag that moved after testing."""
        item = step("golib-create-release", "release", "Verify tested release source")
        self.assertEqual(self.execute(item)[0].returncode, 0)
        self.git("checkout", "-q", "--detach", self.other_sha)
        self.assertNotEqual(self.execute(item)[0].returncode, 0)
        self.git("checkout", "-q", "--detach", self.sha)
        self.git("-c", "tag.gpgsign=false", "tag", "-f", "-a", "v1.2.3", self.other_sha, "-m", "moved")
        self.assertNotEqual(self.execute(item)[0].returncode, 0)

    def test_remote_replacement_deletion_and_read_failure_block_every_publisher_guard(self):
        """Fail every publisher guard on remote identity/read errors before publish."""
        guards = (step("golib-create-release", "release", "Verify tested release source"),
                  step("golib-create-release", "release", "Recheck release tag before publication"),
                  step("release-go-app", "container", "Recheck release tag before image publication"),
                  step("release-go-app", "release", "Recheck release tag before publication"),
                  step("release-go-app", "release", "Publish draft release"))
        for item in guards:
            with self.subTest(step=item["name"]):
                for replacement in (self.other_sha, "", "null"):
                    self.assertNotEqual(self.execute(item, REMOTE_TAG_OBJECT=replacement)[0].returncode, 0)
                self.assertNotEqual(self.execute(item, REMOTE_FAILURE="true")[0].returncode, 0)
                self.assertFalse(Path(self.environment["PUBLISH_MARKER"]).exists())
                self.assertEqual(self.execute(item)[0].returncode, 0)
                marker = Path(self.environment["PUBLISH_MARKER"])
                if marker.exists():
                    self.assertEqual(item["name"], "Publish draft release")
                    marker.unlink()

    def test_native_graph_race_and_platform_contracts_remain(self):
        """Retain native jobs, race tests, platform scope and immutable checkouts."""
        library, app = WORKFLOWS["golib-create-release"], WORKFLOWS["release-go-app"]
        self.assertEqual(set(library["jobs"]), {"test", "release"})
        self.assertEqual(step("golib-create-release", "test", "Run tests")["run"].strip(),
                         "go test -race -v ./...")
        for item in self.bind_steps():
            for key, context in (("EVENT_SHA", "sha"), ("EVENT_REF", "ref"),
                                 ("EVENT_REF_TYPE", "ref_type")):
                self.assertEqual(item["env"][key], "${{ github." + context + " }}")
        checkouts = [(item["with"]["ref"], workflow, job) for workflow, data in WORKFLOWS.items()
                     for job, config in data["jobs"].items() for item in config["steps"]
                     if item.get("uses", "").startswith("actions/checkout@")]
        for ref, workflow, job in checkouts:
            self.assertIn(ref, ("${{ github.sha }}", "${{ needs.test.outputs.sha }}",
                                "${{ needs.preflight.outputs.sha }}"), (workflow, job))
        inputs = app["on"]["workflow_call"]["inputs"]
        self.assertEqual(len(json.loads(inputs["goos-goarch-matrix"]["default"])), 8)
        self.assertEqual(len(inputs["container-platforms"]["default"].split(",")), 5)
        for workflow, job, guard, publisher in (
            ("golib-create-release", "release", "Recheck release tag before publication", "Create GitHub Release"),
            ("release-go-app", "container", "Recheck release tag before image publication", "Build and push"),
            ("release-go-app", "release", "Recheck release tag before publication", "Create draft release + upload all assets")):
            names = [item.get("name") for item in WORKFLOWS[workflow]["jobs"][job]["steps"]]
            self.assertEqual(names.index(guard) + 1, names.index(publisher))


if __name__ == "__main__":
    unittest.main()
