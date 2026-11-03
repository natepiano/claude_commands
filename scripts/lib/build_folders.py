#!/usr/bin/env python3
"""Name temporary build folders and preserve their caller's lifetime."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile
from typing import cast

# Python's mkdtemp allocator uses this suffix.
MKDTEMP_SUFFIX_PATTERN = r"[a-z0-9_]{8}"
# Read old shell mktemp folders until 2026-12-03.
LEGACY_MKTEMP_SUFFIX_PATTERN = r"[A-Za-z0-9]{6}"
SYSTEM_TEMPORARY_PARENT = Path("/tmp")
FOLDER_PREFIXES = {
    "merge-gate": "merge-gate-",
    "merge-gate-module": "{owner}-",
    "merge-gate-nightly": "merge-gate-nightly-",
    "promotion-index": "promotion-index-",
    "fast-track-index": "fast-track-index-",
    "fast-track-check": "fast-track-check-",
    "fast-track-restore-index": "fast-track-restore-index-",
    "fast-track-promote": "fast-track-promote-",
    "fast-track": "fast-track-",
    "production-scratch": "production-scratch-",
    "bump-gate": "bump-gate-",
    "ci-point": "ci-point-",
    "bulk-rule-test": "bulk-rule-test-",
    "style-sync-lint": "style-sync-lint-",
    "rust-release-trial": "rust-release-trial-{owner}",
    "verify-tree": "verify-tree.{owner}.",
    "verify-doc": "verify-doc-",
    "verify-copy": "verify-copy.{owner}.",
    "verify-hold": "verify-hold.{owner}.",
    "queue-place": "queue-place-",
    "admit-test": "admit-test-",
    "admit-sock": "admit-sock-",
    "copy-watch": "copy-watch.",
    "linux-cache-stage": ".{owner}.",
    "linux-step": ".linux-step-",
    "style-review": "{owner}-",
    "checkpoint-doc": "checkpoint-doc-",
    "checkpoint-paths": "checkpoint-paths-",
}


@dataclass(frozen=True)
class InheritedTemporaryParent:
    """Use the caller's temporary parent."""


@dataclass(frozen=True)
class ExplicitBuildParent:
    path: Path


@dataclass(frozen=True)
class SystemTemporaryParent:
    """Use the short system temporary parent."""


@dataclass(frozen=True)
class UnitRunParent:
    """Use the current unit root authority."""


BuildFolderParent = InheritedTemporaryParent | ExplicitBuildParent | SystemTemporaryParent | UnitRunParent


@dataclass(frozen=True)
class UniqueBuildFolder:
    prefix: str
    parent: BuildFolderParent = InheritedTemporaryParent()


@dataclass(frozen=True)
class StableOwnerFolder:
    name: str
    parent: BuildFolderParent
    mode: int = 0o777


BuildFolderRequest = UniqueBuildFolder | StableOwnerFolder


def inherited_temporary_parent() -> Path:
    """Read the caller's current temporary parent."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _parent(parent: BuildFolderParent) -> Path:
    if isinstance(parent, InheritedTemporaryParent):
        return inherited_temporary_parent()
    if isinstance(parent, SystemTemporaryParent):
        return SYSTEM_TEMPORARY_PARENT
    if isinstance(parent, UnitRunParent):
        from scripts.lib import unit_runs

        return unit_runs.run_roots()[0]
    return parent.path


def _check_name(name: str) -> None:
    if name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("a build folder name must be one path component")


def create_build_folder(request: BuildFolderRequest) -> Path:
    """Allocate a unique folder or retain an owner's folder."""
    name = request.prefix if isinstance(request, UniqueBuildFolder) else request.name
    _check_name(name)
    parent = _parent(request.parent)
    if isinstance(request, UniqueBuildFolder):
        return Path(tempfile.mkdtemp(prefix=name, dir=parent))
    if not name:
        raise ValueError("a stable build folder needs an owner name")
    path = parent / name
    path.mkdir(mode=request.mode, parents=True, exist_ok=True)
    return path


@contextmanager
def temporary_build_folder(request: UniqueBuildFolder) -> Generator[Path]:
    """Remove the folder with the standard library's permission repair."""
    _check_name(request.prefix)
    with tempfile.TemporaryDirectory(prefix=request.prefix, dir=_parent(request.parent)) as path:
        yield Path(path)


def main(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    unique = commands.add_parser("unique")
    prefix = unique.add_mutually_exclusive_group(required=True)
    _ = prefix.add_argument("--prefix")
    _ = prefix.add_argument("--kind", choices=tuple(FOLDER_PREFIXES))
    _ = unique.add_argument("--owner", default="")
    unique_parent = unique.add_mutually_exclusive_group()
    _ = unique_parent.add_argument("--parent", type=Path)
    _ = unique_parent.add_argument("--system-temp", action="store_true")
    stable = commands.add_parser("stable")
    stable_destination = stable.add_mutually_exclusive_group()
    _ = stable_destination.add_argument("--path", type=Path)
    _ = stable_destination.add_argument("--run-temp", type=Path)
    _ = stable.add_argument("--name", default="")
    stable_parent = stable.add_mutually_exclusive_group()
    _ = stable_parent.add_argument("--parent", type=Path)
    _ = stable_parent.add_argument("--unit-root", action="store_true")
    run_temp = commands.add_parser("run-temp-path")
    _ = run_temp.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    request: BuildFolderRequest
    command = cast(str, args.command)
    try:
        if command == "run-temp-path":
            from scripts.lib import unit_runs

            print(unit_runs.run_temp_path(cast(Path, args.run)))
            return 0
        if command == "stable":
            path = cast(Path | None, args.path)
            run = cast(Path | None, args.run_temp)
            if run is not None:
                from scripts.lib import unit_runs

                path = unit_runs.run_temp_path(run)
            if path is not None:
                request = StableOwnerFolder(path.name, ExplicitBuildParent(path.parent))
            else:
                parent: BuildFolderParent = UnitRunParent() if cast(bool, args.unit_root) else ExplicitBuildParent(cast(Path, args.parent))
                if not cast(bool, args.unit_root) and cast(Path | None, args.parent) is None:
                    raise ValueError("a stable folder needs --path, --run-temp, or a parent and name")
                request = StableOwnerFolder(cast(str, args.name), parent)
        else:
            unique_choice: BuildFolderParent = InheritedTemporaryParent()
            explicit_parent = cast(Path | None, args.parent)
            if cast(bool, args.system_temp):
                unique_choice = SystemTemporaryParent()
            elif explicit_parent is not None:
                unique_choice = ExplicitBuildParent(explicit_parent)
            kind = cast(str | None, args.kind)
            name = FOLDER_PREFIXES[kind].format(owner=cast(str, args.owner)) if kind else cast(str, args.prefix)
            request = UniqueBuildFolder(name, unique_choice)
        print(create_build_folder(request))
    except (OSError, ValueError) as error:
        print(f"build folders: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main(sys.argv[1:]))
