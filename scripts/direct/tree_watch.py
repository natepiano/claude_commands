#!/usr/bin/env python3
"""Shared verify cache key and conservative workspace-input watcher.

--key [--with-tree-content] [--watch-start FILE] [tree_key args] prints the cache key.
--with-tree-content also prints the key without script files.
--watch-key [tree_key args] prints only the watched input key.
START FIRED PARENT [--signal-parent] [tree_key args] watches until changed (0)
or the parent exits (3). Cargo metadata is supplied once on stdin.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import NotRequired, TypedDict, cast

from scripts.lib.cargo_output import CargoOutputRoots, read_cargo_output_roots
from scripts.lib.git_blob import git_blob_id


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


def run(*args: str | bytes) -> bytes:
    return subprocess.check_output(list(args), stderr=subprocess.DEVNULL)


def lint_tool_versions() -> bytes:
    """Identify each lint tool through the command that runs it."""
    saved = os.environ.get("VERIFY_LINT_VERSIONS_FILE", "")
    if saved and Path(saved).exists():
        versions = Path(saved).read_bytes()
        if not versions:
            raise ValueError("lint tool versions are unavailable")
        return versions
    versions = bytearray()
    try:
        for command in (("taplo", "--version"), ("cargo", "mend", "--version"),
                        ("cargo", "+nightly", "fmt", "--version"), ("cargo", "clippy", "--version")):
            formatter = os.environ.get("VERIFY_LINT_RUSTFMT_VERSION_FILE", "") if command[1:2] == ("+nightly",) else ""
            if formatter and Path(formatter).exists():
                version = Path(formatter).read_bytes().strip()
            else:
                try:
                    version = subprocess.check_output(command, stderr=subprocess.DEVNULL, timeout=10).strip()
                except (OSError, subprocess.SubprocessError):
                    if formatter:
                        _ = Path(formatter).write_bytes(b"")
                    raise
                if formatter:
                    _ = Path(formatter).write_bytes(version)
            if not version:
                raise ValueError("lint tool returned no version: " + " ".join(command))
            versions.extend("\0".join(command).encode() + b"\0" + version + b"\0")
    except (OSError, ValueError, subprocess.SubprocessError):
        if saved:
            _ = Path(saved).write_bytes(b"")
        raise
    if saved:
        _ = Path(saved).write_bytes(versions)
    return bytes(versions)


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
    return mode + b" blob " + git_blob_id(data).encode()


class TreeInputs:
    """One call's fixed scope, shared by cache records and the live watch."""

    def __init__(self, args: list[str], metadata: str) -> None:
        split = args.index("--")
        self.tree_content_key: str = ""
        self.files: list[str] = args[:split]
        self.words: list[str] = args[split + 1:]
        self.forwarded_result_environment: dict[str, str] = {}
        descriptor = os.environ.get("VERIFY_RESULT_ENVIRONMENT_FD", "")
        if self.words[:1] == ["test"] and descriptor:
            with os.fdopen(int(descriptor), "rb", closefd=False) as source:
                for entry in source.read().split(b"\0"):
                    if entry:
                        name, _, value = entry.partition(b"=")
                        self.forwarded_result_environment[os.fsdecode(name)] = os.fsdecode(value)
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
        self.watch_entries: dict[str, str] = {}
        try:
            meta: Metadata = cast(Metadata, json.loads(metadata)) if metadata.strip() else {"workspace_root": "", "packages": [], "workspace_members": []}
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

    def keys(self, *, cache: bool = True, watch: bool = True, lint_versions: bool = True) -> tuple[str, str]:
        self.tree_content_key = ""
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
            self.watch_entries = {}
            for path in sorted(filter(self.watched, entries)):
                self.watch_entries[os.fsdecode(path)] = entries[path].decode()
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
        key.update(run("rustc", "-vV"))
        if lint_versions and self.words[:1] == ["lint"]:
            try:
                versions = lint_tool_versions()
            except (OSError, ValueError, subprocess.SubprocessError):
                # Unknown tool versions prevent cache reuse and publication.
                return "", watch_key
            key.update(versions)
        tree_content = key.copy()
        for name in self.files:
            key.update(content(os.fsencode(name)))
        build_key = key.copy()
        words = iter(self.words)
        for word in words:
            if word == "--filter" or (word == "--env" and self.words[:1] == ["test"]):
                _ = next(words, None)
            else:
                build_key.update(b"\0" + word.encode())
        words = iter(self.words)
        for word in words:
            if word == "--env" and self.words[:1] == ["test"]:
                _ = next(words, None)
                continue
            key.update(b"\0" + word.encode())
            tree_content.update(b"\0" + word.encode())
        for name, value in sorted(os.environ.items()):
            if name.startswith(("RUST", "CARGO_", "NEXTEST_")) and name != "CARGO_MAKEFLAGS":
                key.update(("\0" + name + "=" + value).encode())
                tree_content.update(("\0" + name + "=" + value).encode())
                if not name.startswith("NEXTEST_") or name == "NEXTEST_CARGO_PROFILE":
                    build_key.update(("\0" + name + "=" + value).encode())
        if self.words[:1] == ["test"]:
            from scripts.direct.result_environment import read_policy

            policy = read_policy()
            environment = {name: value for name, value in os.environ.items() if policy.selects(name)}
            words = iter(self.words)
            for word in words:
                if word == "--env":
                    name = next(words, "")
                    if name in self.forwarded_result_environment:
                        environment[name] = self.forwarded_result_environment[name]
                    elif name in os.environ:
                        environment[name] = os.environ[name]
            environment.update(policy.fixed)
            for name, value in sorted(environment.items()):
                encoded = ("\0" + name + "=" + value).encode()
                key.update(encoded)
                tree_content.update(encoded)
        if self.package and os.environ.get("VERIFY_COMPUTE_TEST_BUILD") == "1":
            # Cargo resolves config, environment and workspace target paths.
            # An unknown destination proves no fresh build.
            output = os.environ.get("VERIFY_TEST_BUILD_INPUTS_FILE", "")
            if output and self.workspace_root:
                roots = read_cargo_output_roots(Path(os.fsdecode(self.workspace_root)))
                if isinstance(roots, CargoOutputRoots):
                    build_key.update(b"\0target=" + os.fsencode(roots.target_directory))
                    build_key.update(b"\0build=" + os.fsencode(roots.build_directory))
                    _ = Path(output).write_text("\n".join((build_key.hexdigest(),
                        str(roots.target_directory), str(roots.build_directory), "")))
        self.tree_content_key = tree_content.hexdigest()
        return key.hexdigest(), watch_key


def parent_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return not stat.exists() or stat.read_text().rsplit(") ", 1)[1].split()[0] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False
    except PermissionError:
        return True


def main(args: list[str]) -> int:
    metadata = sys.stdin.read()
    if args[0] in ("--key", "--watch-key"):
        mode, args = args[0], args[1:]
        paired = mode == "--key" and args[:1] == ["--with-tree-content"]
        if paired:
            args = args[1:]
        start = ""
        if args[:1] == ["--watch-start"]:
            start, args = args[1], args[2:]
        lint_versions = args[:1] != ["--without-lint-versions"]
        if not lint_versions:
            args = args[1:]
        inputs = TreeInputs(args, metadata)
        cache, watched = inputs.keys(
            cache=mode == "--key", watch=bool(start) or mode == "--watch-key", lint_versions=lint_versions)
        if start:
            _ = Path(start + ".paths").write_text(json.dumps(inputs.watch_entries))
            _ = Path(start).write_text(watched + "\n")
        print(cache if mode == "--key" else watched)
        if paired:
            print(inputs.tree_content_key)
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
    start_entries = cast(dict[str, str], json.loads(Path(start_file + ".paths").read_text()))
    try:
        interval = float(os.environ.get("VERIFY_TREE_WATCH_S", "5"))
        if not math.isfinite(interval) or interval <= 0:
            interval = 5
    except ValueError:
        interval = 5
    while parent_alive(int(parent)):
        try:
            _, key = inputs.keys(cache=False)
            if key and key != start_key:
                changed = next(path for path in sorted(start_entries.keys() | inputs.watch_entries.keys())
                               if start_entries.get(path) != inputs.watch_entries.get(path))
                _ = Path(fired_file + ".path").write_text(changed + "\n")
                _ = Path(fired_file).write_text(key + "\n")
                if notify:
                    os.kill(int(parent), signal.SIGUSR2)
                return 0
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # An unreadable tree cannot establish that the result is stale.
        time.sleep(interval)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
