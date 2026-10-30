"""Prepare and install two pinned Go tools using optional install-only caches."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

TOOLS = {
    "gosec": ("github.com/securego/gosec/v2/cmd/gosec", "github.com/securego/gosec/v2"),
    "go-licenses": ("github.com/google/go-licenses", "github.com/google/go-licenses"),
}


def target():
    """Accept only these tools at immutable full commit references."""
    kind, value = os.environ["TOOL_KIND"], os.environ["TOOL_TARGET"]
    package, separator, revision = value.rpartition("@")
    if kind not in TOOLS or package != TOOLS[kind][0] or separator != "@" or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("tool cache supports only the two known tools at full commit refs")
    return kind, revision


def version(value):
    """Parse stable Go releases; unknown/development toolchains are ineligible."""
    match = re.fullmatch(r"(?:go)?(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        raise ValueError("a stable Go release is required for cache identity")
    return tuple(int(part or 0) for part in match.groups())


def go_json(*arguments, environment=None):
    """Read bounded Go metadata; callers fall back on any probe failure."""
    result = subprocess.run(["go", *arguments], env=environment, text=True,
                            capture_output=True, timeout=30, check=True)
    return json.loads(result.stdout)


def install_paths_disjoint(metadata, build_cache, module_cache):
    """Reject archive roots overlapping Go's effective executable destination."""
    explicit, gopath = metadata.get("GOBIN", ""), metadata.get("GOPATH", "")
    if not isinstance(explicit, str) or not isinstance(gopath, str):
        return False
    first = gopath.split(os.pathsep)[0]
    destination = explicit or (str(Path(first) / "bin") if first else "")
    paths = [Path(value) for value in (destination, build_cache, module_cache)]
    if not destination or not all(path.is_absolute() for path in paths):
        return False
    try:
        binary, *roots = [path.resolve() for path in paths]
        return all(not (binary.is_relative_to(root) or root.is_relative_to(binary)) for root in roots)
    except (OSError, RuntimeError):
        return False


def prepare():
    """Bind an exact cache to tool, compiler, platform, image and helper source."""
    kind, revision = target()
    if os.environ.get("RUNNER_OS") != "Linux" or not all(os.environ.get(key) for key in ("ImageOS", "ImageVersion")):
        raise ValueError("a versioned Linux runner image is required")
    metadata = go_json("env", "-json", "GOVERSION", "GOTOOLCHAIN", "GOOS", "GOARCH",
                       "CGO_ENABLED", "GOAMD64", "GOARM", "GOARM64", "GOBIN", "GOPATH")
    directory = Path(os.environ["RUNNER_TEMP"]) / "go-tool-build-cache" / kind
    if not install_paths_disjoint(metadata, str(directory / "build"), str(directory / "modules")):
        raise ValueError("executable destination and install caches must be provably disjoint")
    if not re.fullmatch(r"go\d+\.\d+\.\d+", metadata["GOVERSION"]):
        raise ValueError("the actual compiler must be a stable patch release")
    if metadata["GOTOOLCHAIN"] != "local" or version(metadata["GOVERSION"]) != version(os.environ["CONFIGURED_GO"]):
        raise ValueError("configured and actual local toolchain must match")
    expected_arch = {"X64": "amd64", "ARM64": "arm64"}.get(os.environ.get("RUNNER_ARCH"))
    if metadata["GOOS"] != "linux" or metadata["GOARCH"] != expected_arch:
        raise ValueError("runner and Go target platform must match")
    module = go_json("list", "-m", "-json", TOOLS[kind][1] + "@" + revision,
                     environment=os.environ | {"GOTOOLCHAIN": "local"})
    if not module.get("Version") or module.get("Error") or version(module["GoVersion"]) > version(metadata["GOVERSION"]):
        raise ValueError("tool module would require an unproved toolchain switch")
    source = hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name("action.yml").read_bytes()).hexdigest()
    print(f"Tool build cache helper SHA256: {source}; compiler: {metadata['GOVERSION']}")
    identity = {key: value for key, value in metadata.items() if key not in {"GOBIN", "GOPATH"}} | {
                           "tool": kind, "commit": revision, "image": os.environ["ImageOS"],
                           "image-version": os.environ["ImageVersion"], "source": source}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {"enabled": "true", "key": f"go-tool-build-v1-{kind}-{os.environ['RUNNER_OS']}-{os.environ['RUNNER_ARCH']}-{metadata['GOVERSION']}-{digest}",
            "build-cache": str(directory / "build"), "module-cache": str(directory / "modules"),
            "go-version": metadata["GOVERSION"], "source-sha256": source}


def cache_install_paths(build_cache, module_cache):
    """Use the original installer when executable/cache paths cannot be proved safe."""
    try:
        paths = go_json("env", "-json", "GOBIN", "GOPATH")
        if install_paths_disjoint(paths, build_cache, module_cache):
            return paths
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    print("::notice::Executable/cache layout is uncertain or overlapping; using the original installer.")
    return {}


def install():
    """Always install the pinned tool; cache faults retry the original installer."""
    kind, _ = target()
    original = os.environ.copy()
    if kind == "gosec":
        original["GOTOOLCHAIN"] = "auto"
    cached = original | {"GOCACHE": os.environ.get("TOOL_BUILD_CACHE", ""),
                         "GOMODCACHE": os.environ.get("TOOL_MODULE_CACHE", "")}
    use_cache = os.environ.get("TOOL_CACHE_ENABLED") == "true" and all(cached[key] for key in ("GOCACHE", "GOMODCACHE"))
    paths = {}
    if use_cache:
        paths = cache_install_paths(cached["GOCACHE"], cached["GOMODCACHE"])
        use_cache = bool(paths)
    result = subprocess.run(["go", "install", os.environ["TOOL_TARGET"]], env=cached if use_cache else original, check=False)
    cache_built = use_cache and result.returncode == 0
    if use_cache and result.returncode:
        print("::notice::Tool cache install failed; retrying the original pinned installer.", flush=True)
        result = subprocess.run(["go", "install", os.environ["TOOL_TARGET"]], env=original, check=False)
    if result.returncode:
        return result.returncode, {"cache-saveable": "false"}
    saveable = False
    if cache_built:
        try:
            binary = Path(paths["GOBIN"] or str(Path(paths["GOPATH"].split(os.pathsep)[0]) / "bin")) / kind
            information = subprocess.run(["go", "version", "-m", str(binary)], capture_output=True,
                                         text=True, timeout=30, check=True).stdout.splitlines()[0]
            saveable = information.rsplit(": ", 1)[-1] == os.environ.get("EXPECTED_GO")
        except (KeyError, ValueError, IndexError, OSError, subprocess.SubprocessError):
            pass
        if not saveable:
            print("::notice::Installed compiler identity is uncertain or switched; no tool cache will be saved.")
    return 0, {"cache-saveable": str(saveable).lower()}


def emit(values):
    """Write controlled action outputs without changing the caller environment."""
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main():
    """Keep metadata/cache failures advisory while install failures remain red."""
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "install"}:
        raise ValueError("expected prepare or install")
    if sys.argv[1] == "prepare":
        try:
            values = prepare()
        except (KeyError, ValueError, OSError, subprocess.SubprocessError):
            print("::notice::Tool cache eligibility could not be proved; using the original installer.")
            values = {"enabled": "false"}
        emit(values)
        return 0
    result, values = install()
    emit(values)
    return result


if __name__ == "__main__":
    sys.exit(main())
