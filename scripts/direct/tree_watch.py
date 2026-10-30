#!/usr/bin/env python3
"""Shared verify cache key and conservative workspace-input watcher.

--key [--watch-start FILE] [tree_key args] prints the existing cache key.
--watch-key [tree_key args] prints only the watched input key.
START FIRED PARENT [--signal-parent] [tree_key args] watches until changed (0)
or the parent exits (3). Cargo metadata is supplied once on stdin.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import NotRequired, Protocol, TypedDict, cast


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import process_start


class Dependency(TypedDict, total=False):
    path: str | None


class Package(TypedDict):
    id: str
    name: str
    manifest_path: str
    dependencies: list[Dependency]


class Metadata(TypedDict):
    workspace_root: str
    packages: list[Package]
    workspace_members: list[str]
    target_directory: NotRequired[str]


class AssertionGuard(Protocol):
    """The configured guard defines its own base and ruling inputs."""

    GuardError: type[Exception]

    def default_base(self, repo: Path, explicit_base: str = "") -> str: ...
    def git(self, repo: Path, *arguments: str) -> str: ...
    def read_rulings(self, sources: list[str], messages: str) -> dict[tuple[str, str], str]: ...


def run(*args: str | bytes) -> bytes:
    return subprocess.check_output(list(args), stderr=subprocess.DEVNULL)


def content(path: bytes) -> bytes:
    if os.path.islink(path):
        return b"link " + os.readlink(path)
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).digest()
    return b"absent"


def blob(path: bytes) -> bytes:
    """Git's entry for a present file; empty when absent."""
    if os.path.islink(path):
        data, mode = os.readlink(path), b"120000"
    elif os.path.isfile(path):
        with open(path, "rb") as handle:
            data = handle.read()
        mode = b"100755" if os.access(path, os.X_OK) else b"100644"
    else:
        return b""
    return mode + b" blob " + hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest().encode()


class TreeInputs:
    """One call's fixed scope, shared by cache records and the live watch."""

    def __init__(self, args: list[str], metadata: str) -> None:
        split = args.index("--")
        self.files: list[str] = args[:split]
        self.words: list[str] = args[split + 1:]
        self.guard_files: list[str] = self.files[1:2] if self.files[:1] == ["--assertion-guard"] else []
        self.rulings_folders: list[str] = []
        if self.guard_files:
            self.files = self.files[2:]
            while self.files[:1] == ["--rulings"]:
                self.rulings_folders.append(self.files[1])
                self.files = self.files[2:]
        self.package: str = self.files[1] if self.files[:1] == ["--package"] else ""
        if self.package:
            self.files = self.files[2:]
        self.top: bytes = run("git", "--no-optional-locks", "rev-parse", "--show-toplevel").strip()
        self.real_top: bytes = os.path.realpath(self.top)
        self.own: tuple[bytes, ...] = tuple(
            folder + b"/" for folder in (
                os.path.relpath(os.path.realpath(os.fsencode(name)), self.real_top)
                for name in {os.environ.get("UNIT_SESSION_DIR", ""), os.environ.get("UNIT_RUN_DIR", "")} if name
            ) if not folder.startswith(b"..") and folder != b"."
        )
        self.workspace_root: bytes = b""
        self.test_target_directory: str = ""
        try:
            meta: Metadata = cast(Metadata, json.loads(metadata)) if metadata.strip() else {"workspace_root": "", "packages": [], "workspace_members": []}
            self.test_target_directory = meta.get("target_directory", "")
            workspace_root = meta.get("workspace_root", "")
            if workspace_root:
                self.workspace_root = os.fsencode(os.path.realpath(workspace_root))
            packages = {
                os.fsencode(os.path.realpath(os.path.dirname(package["manifest_path"]))): package
                for package in meta["packages"] if package["id"] in meta["workspace_members"]
            }
        except (KeyError, TypeError, ValueError):
            if self.package:
                raise
            # Workspace cache keys have never depended on Cargo metadata.
            # Watch only the inputs whose location the metadata establishes.
            packages = {}
        self.members: list[bytes] = sorted(packages, key=len, reverse=True)
        self.built: set[bytes] = set()
        pending = [root for root, package in packages.items() if package["name"] == self.package] if self.package else []
        if self.package and not pending:
            raise ValueError("package missing from metadata")
        while pending:
            root = pending.pop()
            if root in self.built:
                continue
            self.built.add(root)
            for dependency in packages[root]["dependencies"]:
                path = dependency.get("path")
                if path:
                    dependency_root = os.fsencode(os.path.realpath(path))
                    if dependency_root in packages:
                        pending.append(dependency_root)

    def counts(self, path: bytes) -> bool:
        full = os.path.join(self.real_top, path)
        owner = next((root for root in self.members if full.startswith(root + b"/")), b"")
        return not self.package or not owner or owner in self.built or full == os.path.join(owner, b"Cargo.toml")

    def watched(self, path: bytes) -> bool:
        full = os.path.join(self.real_top, path)
        cargo_path = os.path.relpath(full, self.workspace_root) if self.workspace_root else b""
        cargo_root = cargo_path in (b"Cargo.toml", b"Cargo.lock", b"rust-toolchain", b"rust-toolchain.toml") or cargo_path.startswith(b".cargo/")
        return self.counts(path) and (cargo_root or any(full.startswith(root + b"/") for root in self.members))

    def keys(self, *, cache: bool = True, watch: bool = True) -> tuple[str, str]:
        # status never refreshes the index; diff does, even with no-optional-locks.
        status = run("git", "--no-optional-locks", "-C", self.top, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
        changed = sorted({entry[3:] for entry in status.split(b"\0") if len(entry) > 3 and not entry[3:].startswith(self.own)})
        entries: dict[bytes, bytes] = {}
        if self.package or watch:
            try:
                tree = run("git", "--no-optional-locks", "-C", self.top, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
            except subprocess.SubprocessError:
                if self.package:
                    raise
                # An unavailable watch snapshot must not break the old cache.
                watch, tree = False, b""
            for line in tree.split(b"\0"):
                if b"\t" in line:
                    entry, path = line.split(b"\t", 1)
                    entries[path] = entry
            for path in changed:
                entry = blob(os.path.join(self.top, path))
                if entry:
                    entries[path] = entry
                else:
                    _ = entries.pop(path, None)
        watched = hashlib.sha256(self.top + b"\0")
        if watch:
            for path in sorted(filter(self.watched, entries)):
                watched.update(path + b"\0" + entries[path] + b"\0")
        watch_key = watched.hexdigest() if watch else ""
        if not cache:
            return "", watch_key
        if self.package:
            key = hashlib.sha256(self.top + b"\0")
            for path in sorted(filter(self.counts, entries)):
                key.update(path + b"\0" + entries[path] + b"\0")
        else:
            key = hashlib.sha256(self.top + b"\0" + run("git", "--no-optional-locks", "-C", self.top, "rev-parse", "HEAD^{tree}"))
            for path in changed:
                key.update(path + b"\0" + content(os.path.join(self.top, path)))
        if self.guard_files:
            # Use the invoked guard's own readers, including completed merge
            # parents. An unresolved base leaves no reusable cache key.
            assertion_guard = cast(AssertionGuard, cast(object, SimpleNamespace(
                **runpy.run_path(self.guard_files[0]))))
            repo = Path(os.fsdecode(self.top))
            explicit = os.environ.get("VERIFY_ASSERTION_BASE", "")
            try:
                bases = [assertion_guard.default_base(repo, explicit)]
                if not explicit:
                    bases.extend(assertion_guard.git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[2:])
                for base in dict.fromkeys(bases):
                    key.update(b"\0guard-base=" + base.encode())
                    key.update(assertion_guard.git(repo, "log", "--format=%B", base + "..HEAD", "--").encode())
                key.update(assertion_guard.git(repo, "rev-parse", "HEAD").encode())
                rulings = assertion_guard.read_rulings(self.rulings_folders, "")
            except assertion_guard.GuardError:
                raise SystemExit(1)
            for name in self.guard_files:
                key.update(content(os.fsencode(name)))
            key.update(json.dumps(sorted(rulings.items()), separators=(",", ":")).encode())
        key.update(run("rustc", "-vV"))
        for name in self.files:
            key.update(content(os.fsencode(name)))
        build_key = key.copy()
        words = iter(self.words)
        for word in words:
            if word == "--filter":
                _ = next(words, None)
            else:
                build_key.update(b"\0" + word.encode())
        for word in self.words:
            key.update(b"\0" + word.encode())
        for name, value in sorted(os.environ.items()):
            if name.startswith(("RUST", "CARGO_", "NEXTEST_")) and name != "CARGO_MAKEFLAGS":
                key.update(("\0" + name + "=" + value).encode())
                if not name.startswith("NEXTEST_") or name == "NEXTEST_CARGO_PROFILE":
                    build_key.update(("\0" + name + "=" + value).encode())
        if self.package and os.environ.get("VERIFY_COMPUTE_TEST_BUILD") == "1":
            # Cargo resolves config, environment and workspace target paths.
            # An unknown destination proves no fresh build.
            output = os.environ.get("VERIFY_TEST_BUILD_INPUTS_FILE", "")
            if output and os.path.isabs(self.test_target_directory):
                build_key.update(b"\0target=" + os.fsencode(self.test_target_directory))
                _ = Path(output).write_text(build_key.hexdigest() + "\n" + self.test_target_directory + "\n")
        return key.hexdigest(), watch_key


def main(args: list[str]) -> int:
    metadata = sys.stdin.read()
    if args[0] in ("--key", "--watch-key"):
        mode, args = args[0], args[1:]
        start = ""
        if args[:1] == ["--watch-start"]:
            start, args = args[1], args[2:]
        cache, watched = TreeInputs(args, metadata).keys(cache=mode == "--key", watch=bool(start) or mode == "--watch-key")
        if start:
            _ = Path(start).write_text(watched + "\n")
        print(cache if mode == "--key" else watched)
        return 0
    start_file, fired_file, parent = args[:3]
    args = args[3:]
    notify = args[:1] == ["--signal-parent"]
    if notify:
        args = args[1:]
    inputs = TreeInputs(args, metadata)
    start_key = Path(start_file).read_text().strip()
    if not start_key:
        return 3
    try:
        interval = float(os.environ.get("VERIFY_TREE_WATCH_S", "5"))
        if not math.isfinite(interval) or interval <= 0:
            interval = 5
    except ValueError:
        interval = 5
    parent_pid = int(parent)
    while True:
        try:
            recorded = process_start.process_start(parent_pid)
            break
        except process_start.ProcessReadError:
            time.sleep(interval)  # An unreadable parent is not a dead parent.
    if isinstance(recorded, process_start.ProcessGone):
        return 3
    while process_start.check_owner(parent_pid, recorded) not in (
            process_start.OwnerCheck.GONE, process_start.OwnerCheck.REPLACED):
        try:
            _, key = inputs.keys(cache=False)
            if key and key != start_key:
                _ = Path(fired_file).write_text(key + "\n")
                if notify and process_start.check_owner(parent_pid, recorded) is process_start.OwnerCheck.ALIVE:
                    os.kill(parent_pid, signal.SIGUSR2)
                return 0
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # An unreadable tree cannot establish that the result is stale.
        time.sleep(interval)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
