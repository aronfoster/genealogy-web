"""Reject indirect or implicit build inputs; distribution contents remain private."""

from __future__ import annotations

import fnmatch
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

PACKAGE = "src/genealogy"
INVENTORY = "_genealogy_build_files.json"
METADATA = ("pyproject.toml", "hatch_build.py", "uv.lock", "LICENSE")


def relative_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or path.as_posix() != name
        or ".." in path.parts
        or "\\" in name
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError("Invalid packaging inventory path")
    return path


def regular_input(root: Path, name: str) -> str:
    relative_path(name)
    path = root / name
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError(f"Packaging input cannot be a symlink: {name}")
    if not path.exists() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Packaging input must be a regular file: {name}")
    return str(path)


def runtime_path(name: str, patterns: list[str]) -> bool:
    parts = relative_path(name).parts
    if parts[-1] in ("apple-touch-icon.png", "favicon-32.png", "favicon.svg"):
        return False
    if parts[:2] != ("src", "genealogy"):
        return False
    if any(
        p.startswith(".") or p in {"corpus", "migrations", "proofs", "docs", "__pycache__"}
        for p in parts[2:]
    ):
        return False
    return any(fnmatch.fnmatchcase("/".join(parts[2:]), pattern) for pattern in patterns)


def package_inventory(root: Path, patterns: list[str]) -> list[str]:
    # Git supplies membership, not file contents: normal edits to tracked files
    # are buildable. Do not accidentally inherit a parent checkout's inventory.
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        }
    }
    try:
        checkout = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        checkout = None
    if (
        checkout
        and checkout.returncode == 0
        and Path(checkout.stdout.strip()).resolve() == root.resolve()
    ):
        index = subprocess.run(
            ["git", "ls-files", "--stage", "-z", "--", PACKAGE],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        names = []
        for entry in filter(None, index.stdout.split("\0")):
            header, name = entry.split("\t", 1)
            mode, _, stage = header.split()
            if stage != "0" or mode not in {"100644", "100755"}:
                raise ValueError("Packaging requires regular, conflict-free tracked inputs")
            if runtime_path(name, patterns):
                names.append(name)
    else:
        # Generated when the sdist is built; never maintained in the repository.
        # It keeps Git-less rebuilds independent of newly added local files.
        saved = regular_input(root, INVENTORY)

        def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate packaging inventory key")
                result[key] = value
            return result

        data = json.loads(Path(saved).read_text(), object_pairs_hook=unique_keys)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "files"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["files"], list)
        ):
            raise ValueError("Invalid packaging inventory schema")
        names = []
        for name in data["files"]:
            if not isinstance(name, str) or not runtime_path(name, patterns):
                raise ValueError("Invalid packaging inventory member")
            names.append(name)
    if not names or len(set(names)) != len(names) or PACKAGE + "/__init__.py" not in names:
        raise ValueError("Empty, duplicate or incomplete packaging inventory")
    return sorted(names)


def __getattr__(name: str) -> Callable[..., Any]:
    # Hatchling merges a local hatch.toml over pyproject BEFORE it loads hooks.
    # Reject this overlay at the PEP 517/660 boundary so it cannot replace the
    # selection or disable the containment hook. Delegate the standard build,
    # editable-install and optional metadata APIs without changing their behavior.
    if name not in {
        "build_sdist",
        "build_wheel",
        "build_editable",
        "get_requires_for_build_sdist",
        "get_requires_for_build_wheel",
        "get_requires_for_build_editable",
        "prepare_metadata_for_build_wheel",
        "prepare_metadata_for_build_editable",
    }:
        raise AttributeError(name)
    from hatchling import build

    delegate = getattr(build, name)

    def invoke(*args: Any, **kwargs: Any) -> Any:
        overlay = Path.cwd() / "hatch.toml"
        if overlay.exists() or overlay.is_symlink():
            raise ValueError("Local Hatch configuration overlays are not permitted")
        if any(k.startswith("HATCH_BUILD_") and k != "HATCH_BUILD_LOCATION" for k in os.environ):
            raise ValueError("Hatch build environment overrides are not permitted")
        return delegate(*args, **kwargs)

    return invoke


class ContainmentHook(BuildHookInterface[Any]):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        patterns = self.config.get("include")
        if (
            not isinstance(patterns, list)
            or not patterns
            or any(not isinstance(p, str) for p in patterns)
        ):
            raise ValueError("Packaging requires runtime resource rules")
        names = package_inventory(root, patterns)
        selected = {regular_input(root, name): name for name in names}
        # Refine the declared root using the public cached selection mapping.
        # Nothing else is discovered by Hatchling, including untracked overlays.
        self.build_config.only_include.clear()
        self.build_config.only_include.update(selected)
        expected = {str(root / "uv.lock"): "uv.lock"} if self.target_name == "sdist" else {}
        if self.build_config.force_include != expected:
            raise ValueError("Unexpected implicit packaging input")
        build_data["force_include"] = {}
        self._inventory_tmp: tempfile.TemporaryDirectory[str] | None = None
        if self.target_name == "sdist":
            build_data["force_include"] = {regular_input(root, name): name for name in METADATA}
            self._inventory_tmp = tempfile.TemporaryDirectory(prefix="genealogy-sdist-")
            saved = Path(self._inventory_tmp.name) / INVENTORY
            saved.write_text(json.dumps({"version": 1, "files": names}, sort_keys=True) + "\n")
            build_data["force_include"][str(saved)] = INVENTORY

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        if self._inventory_tmp is not None:
            self._inventory_tmp.cleanup()
