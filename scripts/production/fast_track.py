#!/usr/bin/env python3
"""Check a small worktree patch, push main, and retain it for blind review."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Literal, TypedDict, cast
from uuid import uuid4

from scripts.lib import process_group

from scripts.copies import copies
from scripts.production import checkpoint_gate
from scripts.lib import build_folders, conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail


class Refused(Exception):
    """The requested patch must not be pushed."""


class PublishedFailure(Exception):
    """Main contains the fix, but installation or local cleanup needs a retry."""


@dataclass(frozen=True)
class FastTrackLimits:
    max_changed_lines: int
    max_files: int
    check_time_limit_seconds: float
    max_push_attempts: int
    refused_paths: tuple[str, ...]


class PushRecord(TypedDict):
    patch_sha256: str
    unit: str
    commit: str
    why: str
    time: str
    review: str
    repo: str
    paths: list[str]
    review_pass: str


class PushIntent(TypedDict):
    record: PushRecord
    worktree_head: str
    worktree_tree: str
    index_tree: str


class PushAttempt(TypedDict):
    restore: PushIntent
    base: str
    production: str
    publication: Literal["prepared", "published", "not_published"]
    restored: bool
    restore_closed: Literal["", "ancestry", "explicit"]
    promoted: bool


@dataclass(frozen=True)
class SelectedPatch:
    """The patch and recovery states selected before any checks."""
    patch: bytes
    worktree_head: str
    worktree_tree: str
    index_tree: str
    recovery_ref_prefix: str


@dataclass(frozen=True)
class FileState:
    mode: str = ""
    content: bytes = b""


@dataclass
class FastTrackArguments(argparse.Namespace):
    """Typed CLI boundary; an empty production name is refused before a push."""
    unit: str = ""
    production: str = ""
    why: str = ""
    pending: bool = False
    mark_reviewed: bool = False
    close_restore: str = ""
    review_pass: str = ""
    commit: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)


def limits(settings_read: conf.ConfigRead) -> FastTrackLimits:
    return FastTrackLimits(
        max_changed_lines=settings_read.whole("max_changed_lines"),
        max_files=settings_read.whole("max_files"),
        check_time_limit_seconds=settings_read.number("check_time_limit_seconds"),
        max_push_attempts=settings_read.whole("max_push_attempts"),
        refused_paths=settings_read.words("refused_paths"),
    )


def run_git(repo: Path, *arguments: str,
            environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    literal_paths = {**(os.environ if environment is None else environment), "GIT_LITERAL_PATHSPECS": "1"}
    return subprocess.run(["git", "-C", str(repo), *arguments], env=literal_paths,
                          capture_output=True, text=True, check=False)


def git(repo: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    result = run_git(repo, *arguments, environment=environment)
    if result.returncode:
        raise Refused(failure_detail(result))
    return result.stdout


def fetch_main(repo: Path, settings_read: conf.ConfigRead) -> None:
    attempts = settings_read.whole("fetch_lock_attempts")
    wait_seconds = settings_read.number("fetch_lock_wait_seconds")
    for attempt in range(attempts):
        result = run_git(repo, "fetch", "origin", "main")
        if result.returncode == 0:
            return
        if "cannot lock ref" not in result.stderr or attempt + 1 == attempts:
            raise Refused(failure_detail(result))
        time.sleep(wait_seconds)


def selected_paths(repo: Path, names: list[str], config: FastTrackLimits) -> list[str]:
    paths: list[str] = []
    for name in names:
        path = (Path.cwd() / name).absolute()
        try:
            relative = path.relative_to(repo).as_posix()
            _ = path.resolve().relative_to(repo)
        except ValueError as error:
            raise Refused(f"path outside the repo: {name}") from error
        # Lexical traversal and Git pathspec magic must not broaden the request.
        if ".." in Path(relative).parts or relative.startswith(":"):
            raise Refused(f"name a file within the repo: {name}")
        if refused_path(relative, config):
            raise Refused(f"refused path: {relative}")
        if relative not in paths:
            paths.append(relative)
    return paths


def patch_for(repo: Path, paths: list[str]) -> bytes:
    """Use a private index to include new files without changing the director's index."""
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(prefix=build_folders.FOLDER_PREFIXES["fast-track-index"])) as temporary:
        environment = {**os.environ, "GIT_INDEX_FILE": str(temporary / "index"),
                       "GIT_LITERAL_PATHSPECS": "1"}
        _ = git(repo, "read-tree", "HEAD", environment=environment)
        # A missing untracked path after a successful restore is an empty patch.
        tracked = set(git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
        existing = [p for p in paths if any(t == p or t.startswith(p + "/") for t in tracked) or (repo / p).exists() or (repo / p).is_symlink()]
        if not existing:
            return b""
        _ = git(repo, "add", "--force", "--all", "--", *existing, environment=environment)
        result = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--binary", "--full-index",
                                 "--no-renames", "HEAD", "--", *existing], env=environment,
                                capture_output=True, check=False)
        if result.returncode:
            raise Refused(result.stderr.decode(errors="replace"))
        return result.stdout


def patch_counts(repo: Path, patch: bytes) -> list[tuple[str, int, int]]:
    result = subprocess.run(["git", "-C", str(repo), "apply", "--numstat", "-z"], input=patch,
                            capture_output=True, check=False)
    if result.returncode:
        raise Refused("cannot count patch lines")
    counts: list[tuple[str, int, int]] = []
    for row in result.stdout.split(b"\0"):
        if not row:
            continue
        added, removed, path = row.split(b"\t", 2)
        if not added.isdigit() or not removed.isdigit():
            raise Refused("max_changed_lines cannot cover a binary patch; use the normal path")
        counts.append((os.fsdecode(path), int(added), int(removed)))
    return counts


def check_size(repo: Path, paths: list[str], patch: bytes, config: FastTrackLimits) -> None:
    counts = patch_counts(repo, patch)
    if sum(added + removed for _, added, removed in counts) > config.max_changed_lines:
        raise Refused("max_changed_lines exceeded")
    if len(counts) > config.max_files:
        raise Refused("max_files exceeded")
    check_paths(repo, paths, config)


def refused_path(path: str, config: FastTrackLimits) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("/") + "/")
               for pattern in config.refused_paths)


def imported_paths(repo: Path, path: str) -> set[str]:
    source = repo / path
    if source.is_file():
        text = source.read_text()
    else:
        text = git(repo, "show", f"HEAD:{path}")
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as error:
        raise Refused(f"invalid Python module {path}: {error}") from error
    imported: set[str] = set()
    for node in ast.walk(tree):
        modules: list[tuple[Path, str]] = []
        if isinstance(node, ast.Import):
            modules.extend((Path(path).parent, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            directory = Path(path).parent
            if node.level:
                for _level in range(node.level - 1):
                    directory = directory.parent
            module = node.module or ""
            modules.append((directory if node.level else Path(path).parent, module))
            modules.extend((directory if node.level else Path(path).parent,
                            ".".join(part for part in (module, alias.name) if part))
                           for alias in node.names if alias.name != "*")
        for directory, module in modules:
            relative = Path(*module.split(".")) if module else Path()
            roots = (directory,) if isinstance(node, ast.ImportFrom) and node.level else (directory, Path())
            for root in roots:
                candidate = root / relative
                files = (candidate.with_suffix(".py"), candidate / "__init__.py") if module else (candidate / "__init__.py",)
                found = next((p for p in files if (repo / p).is_file()), None)
                if found is not None:
                    imported.add(found.as_posix())
                    # A package initializer can import a refused module as well.
                    for parent in found.parents:
                        initializer = parent / "__init__.py"
                        if (repo / initializer).is_file():
                            imported.add(initializer.as_posix())
                    break
    return imported


def check_paths(repo: Path, paths: list[str], config: FastTrackLimits) -> None:
    for path in paths:
        if refused_path(path, config):
            raise Refused(f"refused path: {path}")
        if not path.endswith(".py"):
            continue
        pending = [path]
        seen: set[str] = set()
        while pending:
            dependency = pending.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            if refused_path(dependency, config):
                raise Refused(f"refused import: {path} imports {dependency}")
            pending.extend(imported_paths(repo, dependency) - seen)


def state_file() -> Path:
    return Path(os.environ.get("FAST_TRACK_STATE_DIR", str(Path.home() / ".local/state/fast-track"))) / "records.jsonl"


@contextmanager
def record_lock() -> Generator[None]:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def push_record(value: object, path: Path) -> PushRecord:
    if not isinstance(value, dict):
        raise Refused(f"invalid fast-track record in {path}")
    fields = cast(dict[str, object], value)
    for key in ("patch_sha256", "unit", "commit", "why", "time", "review"):
        if key not in fields or not isinstance(fields[key], str):
            raise Refused(f"invalid fast-track record key {key} in {path}")
    # Older review rows contain only the public review fields.
    for key in ("repo", "review_pass"):
        if key not in fields:
            fields[key] = ""
    if "paths" not in fields:
        fields["paths"] = []
    if (not isinstance(fields["repo"], str) or not isinstance(fields["review_pass"], str)
            or not isinstance(fields["paths"], list)
            or any(not isinstance(item, str) for item in cast(list[object], fields["paths"]))
            or fields["review"] not in ("pending", "reviewed")):
        raise Refused(f"invalid fast-track record in {path}")
    return cast(PushRecord, cast(object, fields))


def records() -> list[PushRecord]:
    path = state_file()
    if not path.exists():
        return []
    return [push_record(cast(object, json.loads(line)), path) for line in path.read_text().splitlines()]


def read_attempt(path: Path) -> PushAttempt:
    value = cast(object, json.loads(path.read_text()))
    if not isinstance(value, dict):
        raise Refused(f"invalid fast-track attempt in {path}")
    fields = cast(dict[str, object], value)
    snapshot = fields["restore"]
    if not isinstance(snapshot, dict):
        raise Refused(f"invalid restore snapshot in {path}")
    restore = cast(dict[str, object], snapshot)
    row = push_record(restore["record"], path)
    restore["record"] = row
    if "restore_closed" not in fields:
        fields["restore_closed"] = ""
    if (any(not isinstance(restore[key], str) for key in ("worktree_head", "worktree_tree", "index_tree"))
            or any(not isinstance(fields[key], str) for key in ("base", "production"))
            or fields["publication"] not in ("prepared", "published", "not_published")
            or fields["restore_closed"] not in ("", "ancestry", "explicit")
            or any(not isinstance(fields[key], bool) for key in ("restored", "promoted"))):
        raise Refused(f"invalid fast-track attempt in {path}")
    return cast(PushAttempt, cast(object, fields))


def attempts() -> list[tuple[Path, PushAttempt]]:
    folder = state_file().parent / "attempts"
    return [(path, read_attempt(path)) for path in sorted(folder.glob("*.json"))]


def save_attempt(path: Path, attempt: PushAttempt) -> None:
    write_text_atomic(path, json.dumps(attempt) + "\n")


def save_records(rows: list[PushRecord]) -> None:
    write_text_atomic(state_file(), "".join(json.dumps(row) + "\n" for row in rows))


def pending(repo: Path, unit: str, settings_read: conf.ConfigRead) -> None:
    _ = reconcile(repo, unit, settings_read=settings_read)
    with record_lock():
        rows = records()
    for row in rows:
        if row["unit"] == unit and row["review"] == "pending":
            print(f"\n## Fast-track {row['commit']} — {row['why']}\n")
            print(git(repo, "show", "--format=fuller", "--binary", row["commit"]))


def mark_reviewed(unit: str, commits: list[str], review_pass: str) -> None:
    if not commits or not review_pass:
        raise Refused("--mark-reviewed requires --commit and --pass")
    with record_lock():
        rows = records()
        for row in rows:
            if row["unit"] == unit and row["commit"] in commits and row["review"] == "pending":
                row["review"] = "reviewed"
                row["review_pass"] = review_pass
        save_records(rows)
        for path, attempt in attempts():
            row = attempt["restore"]["record"]
            if row["unit"] == unit and row["commit"] in commits and row["review"] == "pending":
                row["review"] = "reviewed"
                row["review_pass"] = review_pass
                save_attempt(path, attempt)


def check_command(command: list[str], repo: Path, environment: dict[str, str], deadline: float,
                  *, types: bool = False, empty_suite: bool = False) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Refused("over the fast-track time limit; use the normal path")
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=repo, env=environment, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            _ = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            process_group.send_process_group(process.pid, signal.SIGKILL)
            _ = process.wait()
            raise Refused("over the fast-track time limit; use the normal path") from error
        _ = output.seek(0)
        text = output.read().decode(errors="replace")
    print(text, end="" if text.endswith("\n") else "\n")
    passed = (bool(re.search(r"(?m)^0 errors, 0 warnings, 0 notes\s*$", text)) if types else
              process.returncode == 0 or empty_suite and process.returncode == 5 and "Ran 0 tests" in text)
    if not passed:
        raise Refused(f"fast check failed: {' '.join(command)}")


def fast_check(repo: Path, paths: list[str], config: FastTrackLimits) -> None:
    from scripts.production import merge_gate

    deadline = time.monotonic() + config.check_time_limit_seconds
    check_paths(repo, paths, config)
    python_paths = [Path(p) for p in paths if p.endswith(".py")]
    python_files = [p for p in python_paths if (repo / p).is_file()]
    modules: set[Path] = set()
    for path in python_paths:
        if path.name.startswith("test_") and (repo / path).is_file():
            modules.add(path)
        for pattern in (f"test_{path.stem}.py", f"test_{path.stem}_*.py"):
            modules.update(p.relative_to(repo) for p in (repo / path.parent).glob(pattern))
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONUNBUFFERED", "REFUSAL_WATCH_INVENTORY", "STAFF_OPERATION_FD")}
    environment["HERDR_BIN"] = "/nonexistent/herdr"
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(prefix=build_folders.FOLDER_PREFIXES["fast-track-check"])) as temporary:
        for number, module in enumerate(sorted(modules)):
            root = temporary / str(number)
            root.mkdir()
            isolated = merge_gate.test_module_environment(root, environment)
            check_command([str(repo / "scripts/lib/py"), "-m", "unittest",
                           ".".join(module.with_suffix("").parts)], repo, isolated, deadline, empty_suite=True)
            for marker in (root / "build-tool-calls.jsonl", root / "live-memory-reads.jsonl"):
                if marker.exists() and marker.stat().st_size:
                    raise Refused(f"fast check tripwire: {marker.read_text()}")
        for path in python_files:
            module = ".".join(path.with_suffix("").parts)
            if not all(part.isidentifier() for part in module.split(".")):
                raise Refused(f"not a Python package module: {path}")
            check_command([sys.executable, "-c", f"import {module}"], repo, environment, deadline)
        if python_files:
            check_command([os.environ.get("MERGE_GATE_BASEDPYRIGHT", "basedpyright"),
                           *map(str, python_files)], repo, environment, deadline, types=True)


def drop_recovery_refs(repo: Path, prefix: str) -> None:
    for name in ("head", "index", "worktree", "commit"):
        _ = git(repo, "update-ref", "-d", f"{prefix}/{name}")


@contextmanager
def select_patch(repo: Path, paths: list[str]) -> Generator[SelectedPatch]:
    """Protect the selection until durable attempts protect the same objects."""
    prefix = f"refs/fast-track/selections/{uuid4().hex}"
    try:
        head = git(repo, "rev-parse", "HEAD").strip()
        _ = git(repo, "update-ref", prefix + "/head", head)
        index = git(repo, "write-tree").strip()
        _ = git(repo, "update-ref", prefix + "/index", index)
        patch = patch_for(repo, paths)
        if git(repo, "rev-parse", "HEAD").strip() != head:
            raise Refused("HEAD changed while selecting the patch; retry the command")
        with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(prefix=build_folders.FOLDER_PREFIXES["fast-track-restore-index"])) as temporary:
            environment = {**os.environ, "GIT_INDEX_FILE": str(temporary / "index")}
            _ = git(repo, "read-tree", head, environment=environment)
            if patch:
                result = subprocess.run(["git", "-C", str(repo), "apply", "--cached"], input=patch,
                                        env=environment, capture_output=True, check=False)
                if result.returncode:
                    raise Refused(result.stderr.decode(errors="replace"))
            tree = git(repo, "write-tree", environment=environment).strip()
        _ = git(repo, "update-ref", prefix + "/worktree", tree)
        yield SelectedPatch(patch, head, tree, index, prefix)
    finally:
        drop_recovery_refs(repo, prefix)


def prepare_attempt(repo: Path, row: PushRecord, selection: SelectedPatch, base: str, production: str, candidate_source: Path) -> Path:
    intent = PushIntent(record=row, worktree_head=selection.worktree_head, worktree_tree=selection.worktree_tree,
                        index_tree=selection.index_tree)
    identity = json.dumps([row["unit"], str(repo.resolve()), row["commit"]]).encode()
    path = state_file().parent / "attempts" / (hashlib.sha256(identity).hexdigest() + ".json")
    with record_lock():
        # Retrying this candidate must retain its original restore snapshot.
        if not path.exists():
            prefix = f"refs/fast-track/attempts/{path.stem}"
            # Retain the candidate before the attempt becomes durable.
            _ = git(repo, "fetch", "--no-tags", str(candidate_source),
                    f"{row['commit']}:{prefix}/commit")
            for name, target in (("head", selection.worktree_head), ("index", selection.index_tree),
                                 ("worktree", selection.worktree_tree)):
                _ = git(repo, "update-ref", f"{prefix}/{name}", target)
            attempt = PushAttempt(restore=intent, base=base, production=production,
                                  publication="prepared", restored=False, restore_closed="", promoted=False)
            save_attempt(path, attempt)
        drop_recovery_refs(repo, selection.recovery_ref_prefix)
    return path


def file_at(repo: Path, tree: str, path: str) -> FileState:
    entry = git(repo, "ls-tree", "-z", tree, "--", path).split("\0", 1)[0]
    if not entry:
        return FileState()
    mode, kind, blob = entry.split("\t", 1)[0].split()
    if kind == "tree":
        return FileState(mode)
    result = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob],
                            capture_output=True, check=False)
    if result.returncode:
        raise Refused(result.stderr.decode(errors="replace"))
    return FileState(mode, result.stdout)


def worktree_file(path: Path) -> FileState:
    try:
        if path.is_symlink():
            return FileState("120000", os.fsencode(os.readlink(path)))
        if path.is_dir():
            return FileState("040000")
        if path.is_file():
            return FileState("100755" if path.stat().st_mode & 0o111 else "100644", path.read_bytes())
    except (FileNotFoundError, NotADirectoryError):
        pass
    return FileState()


def restore_prepared(repo: Path, intent: PushIntent) -> None:
    row = intent["record"]
    head = intent["worktree_head"]
    if git(repo, "rev-parse", "HEAD").strip() != head:
        raise PublishedFailure("pushed patch, but HEAD changed; kept worktree edits")
    index = git(repo, "write-tree").strip()
    restore: list[str] = []
    remove: list[str] = []
    # Validate the whole restore before changing a path. A prior stop may have
    # restored any subset; a later edit must stay in both the index and worktree.
    for path in row["paths"]:
        baseline = file_at(repo, head, path)
        published = file_at(repo, intent["worktree_tree"], path)
        current = worktree_file(repo / path)
        staged = file_at(repo, index, path)
        original_index = file_at(repo, intent["index_tree"], path)
        allowed_worktree = (baseline, published, FileState()) if baseline.mode == "040000" else (baseline, published)
        if current not in allowed_worktree or staged not in (baseline, published, original_index):
            raise PublishedFailure(f"pushed patch, but {path} changed; kept worktree edits")
        if baseline.mode in ("", "040000"):
            if staged.mode not in ("", "040000") or current == published and current.mode not in ("", "040000"):
                remove.append(path)
        elif current != baseline or staged != baseline:
            restore.append(path)
    # Remove added leaves before restoring tracked children of a replaced folder.
    for path in remove:
        _ = git(repo, "update-index", "--force-remove", "--", path)
        if not (repo / path).is_dir():
            (repo / path).unlink(missing_ok=True)
    for path in restore:
        _ = git(repo, "restore", "--source=" + head, "--staged", "--worktree", "--", path)



def contains_commit(repo: Path, commit: str, target: str) -> bool:
    # A prepared attempt that never pushed may exist only in its discarded clone.
    exists = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", commit + "^{commit}"],
                            capture_output=True, check=False)
    if exists.returncode:
        return False
    result = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, target],
                            capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise Refused(failure_detail(result))
    return result.returncode == 0


def is_on_main(repo: Path, commit: str) -> bool:
    return contains_commit(repo, commit, "origin/main")


def same_selection(paths: list[str], recorded: list[str]) -> bool:
    return (all(any(p == name or p.startswith(name + "/") for p in recorded) for name in paths)
            and all(any(p == name or p.startswith(name + "/") for name in paths) for p in recorded))


def selections_overlap(paths: tuple[str, ...], recorded: list[str]) -> bool:
    return any(p == name or p.startswith(name + "/") or name.startswith(p + "/")
               for p in paths for name in recorded)


def close_restore(repo: Path, unit: str, identity: str) -> None:
    with record_lock():
        selected = [(path, attempt) for path, attempt in attempts()
                    if path.stem == identity and attempt["restore"]["record"]["unit"] == unit
                    and attempt["restore"]["record"]["repo"] == str(repo)]
        if not selected:
            raise Refused(f"no fast-track attempt {identity} for {unit} in {repo}")
        path, attempt = selected[0]
        if not attempt["restore_closed"]:
            attempt["restore_closed"] = "explicit"
            save_attempt(path, attempt)
        drop_recovery_refs(repo, f"refs/fast-track/attempts/{path.stem}")
    print(f"automatic restore closed for {identity}")


def reconcile(repo: Path, unit: str, *, finish: bool = False, paths: tuple[str, ...] = (),
              published_commit: str = "", settings_read: conf.ConfigRead) -> str:
    """Publish review rows first; only command recovery restores and promotes."""
    with record_lock():
        selected = [(path, attempt) for path, attempt in attempts()
                    if attempt["restore"]["record"]["unit"] == unit
                    and attempt["restore"]["record"]["repo"] == str(repo)
                    and (not published_commit or attempt["restore"]["record"]["commit"] == published_commit)]
    if not selected:
        return ""
    fetch_main(repo, settings_read)
    recovered = ""
    # Old attempts finish before new attempts; the snapshots remain immutable.
    selected.sort(key=lambda entry: entry[1]["restore"]["record"]["time"])
    for selected_attempt in selected:
        path = selected_attempt[0]
        with record_lock():
            attempt = read_attempt(path)
            row = attempt["restore"]["record"]
            if not is_on_main(repo, row["commit"]):
                if git(repo, "rev-parse", "origin/main").strip() != attempt["base"]:
                    attempt["publication"] = "not_published"
                    save_attempt(path, attempt)
                continue
            attempt["publication"] = "published"
            rows = records()
            published = next((entry for entry in rows if entry["commit"] == row["commit"]), None)
            if published is None:
                rows.append(row)
                save_records(rows)
            else:
                row["review"] = published["review"]
                row["review_pass"] = published["review_pass"]
            save_attempt(path, attempt)
            if not attempt["restore_closed"] and contains_commit(repo, row["commit"], "HEAD"):
                attempt["restore_closed"] = "ancestry"
                attempt["restored"] = True
                save_attempt(path, attempt)
            if attempt["restore_closed"]:
                drop_recovery_refs(repo, f"refs/fast-track/attempts/{path.stem}")
            if not finish:
                continue
            matches = bool(paths) and same_selection(list(paths), row["paths"])
            overlap = selections_overlap(paths, row["paths"])
            unfinished = (not attempt["restored"] and not attempt["restore_closed"]) or not attempt["promoted"]
            patch = patch_for(repo, list(paths)) if matches else b""
            repeat = matches and (not patch or hashlib.sha256(patch).hexdigest() == row["patch_sha256"])
            if not unfinished and not repeat:
                continue
            try:
                if overlap and not attempt["restore_closed"] and (not attempt["restored"] or (repeat and patch)):
                    try:
                        restore_prepared(repo, attempt["restore"])
                    except (PublishedFailure, Refused, OSError, ValueError) as error:
                        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "--unit", unit,
                                              "--close-restore", path.stem])
                        raise PublishedFailure(f"{error}; retire automatic restore with: {command}") from error
                    attempt["restored"] = True
                    save_attempt(path, attempt)
                if not attempt["promoted"] or repeat:
                    promote(attempt["production"], repo, row["commit"], unit)
                    attempt["promoted"] = True
                    save_attempt(path, attempt)
            except (PublishedFailure, Refused, OSError, ValueError) as error:
                if overlap:
                    raise PublishedFailure(str(error)) from error
                detail = " ".join(str(error).splitlines())
                print(f"fast-track attempt {path.stem} remains pending: {detail}", file=sys.stderr)
                continue
            if not overlap and not attempt["restored"] and not attempt["restore_closed"]:
                print(f"fast-track attempt {path.stem} remains pending: automatic restore needs an overlapping request",
                      file=sys.stderr)
            if matches:
                recovered = row["commit"]
    return recovered


def promote(production: str, repo: Path, commit: str, unit: str) -> None:
    from scripts.production import merge_checkpoint, production_doc

    try:
        promotion = production_doc.promotion_from_doc(production_doc.read_production(production).doc.read_text().splitlines())
        if isinstance(promotion, production_doc.PromoteTo) and not promotion.checkouts and isinstance(promotion.mac, production_doc.MacCheckout):
            # The pull helper needs a local main ref at the published commit.
            with copies.standing(copies.source_owner(repo), repo, copies.CommittedSource(commit)) as source:
                _ = git(source, "update-ref", "refs/heads/main", commit)
                merge_checkpoint.promote_targets(promotion, "main", commit, source=source, label=f"fast-track {unit}")
        else:
            merge_checkpoint.promote_targets(promotion, "main", commit, source=repo, label=f"fast-track {unit}")
    except (checkpoint_gate.Stop, copies.CopyError, production_doc.Refusal) as error:
        raise PublishedFailure(str(error)) from error


def push_patch(repo: Path, production: str, unit: str, why: str, paths: list[str],
               selection: SelectedPatch, config: FastTrackLimits, settings_read: conf.ConfigRead) -> str:
    from scripts.production import merge_checkpoint

    patch = selection.patch
    digest = hashlib.sha256(patch).hexdigest()
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(prefix=build_folders.FOLDER_PREFIXES["fast-track"])) as temporary:
        fetch_main(repo, settings_read)
        with copies.standing(copies.source_owner(repo), repo, copies.CommittedSource("origin/main")) as copy:
            _ = git(copy, "remote", "set-url", "origin", git(repo, "remote", "get-url", "origin").strip())
            for key in ("user.name", "user.email"):
                value = git(repo, "config", "--get", key).strip()
                _ = git(copy, "config", key, value)
            patch_file = temporary / "fix.patch"
            _ = patch_file.write_bytes(patch)
            for _attempt in range(config.max_push_attempts):
                _ = reconcile(repo, unit, settings_read=settings_read)
                _ = git(copy, "fetch", "origin", "main")
                base = git(copy, "rev-parse", "origin/main").strip()
                _ = git(copy, "checkout", "--detach", "--force", base)
                _ = git(copy, "clean", "-ffdq")
                _ = git(copy, "apply", "--3way", str(patch_file))
                fast_check(copy, paths, config)
                _ = git(copy, "commit", "-m", f"fast-track({unit}): {why}\n\nFast-Track: {unit}")
                commit = git(copy, "rev-parse", "HEAD").strip()
                row = PushRecord(patch_sha256=digest, unit=unit, commit=commit, why=why,
                                 time=datetime.now(UTC).isoformat(), review="pending", repo=str(repo),
                                 paths=paths, review_pass="")
                _ = prepare_attempt(repo, row, selection, base, production, copy)
                try:
                    with merge_checkpoint.main_push_lock(repo):
                        _ = git(copy, "fetch", "origin", "main")
                        if git(copy, "rev-parse", "origin/main").strip() != base:
                            continue
                        result = subprocess.run(["git", "-C", str(copy), "push", "origin", "HEAD:main"],
                                                capture_output=True, text=True, check=False)
                except checkpoint_gate.Stop as error:
                    raise Refused(str(error)) from error
                if result.returncode:
                    # A writer outside the shared lock can move origin during push.
                    _ = git(copy, "fetch", "origin", "main")
                    if not is_on_main(copy, commit):
                        _ = reconcile(repo, unit, settings_read=settings_read)
                        if git(copy, "rev-parse", "origin/main").strip() != base:
                            continue
                        raise Refused(failure_detail(result))
                try:
                    _ = reconcile(repo, unit, finish=True, paths=tuple(paths), published_commit=commit, settings_read=settings_read)
                except (Refused, OSError, ValueError, KeyError) as error:
                    raise PublishedFailure(str(error)) from error
                return commit
    _ = reconcile(repo, unit, settings_read=settings_read)
    raise Refused("max_push_attempts exceeded; use the normal path")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--unit", required=True)
    _ = parser.add_argument("--production", default="")
    _ = parser.add_argument("--why", default="")
    mode = parser.add_mutually_exclusive_group()
    _ = mode.add_argument("--pending", action="store_true")
    _ = mode.add_argument("--mark-reviewed", action="store_true")
    _ = mode.add_argument("--close-restore", metavar="ATTEMPT", default="",
                          help="retire automatic restore for this attempt; keep edits and pending review")
    _ = parser.add_argument("--pass", dest="review_pass", default="")
    _ = parser.add_argument("--commit", action="append", default=[], type=str)
    _ = parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv, namespace=FastTrackArguments())
    try:
        repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip()).resolve()
        if args.pending:
            pending(repo, args.unit, conf.read("fast_track"))
            return 0
        if args.mark_reviewed:
            mark_reviewed(args.unit, args.commit, args.review_pass)
            return 0
        if args.close_restore:
            close_restore(repo, args.unit, args.close_restore)
            return 0
        if not args.production or not args.paths or not args.why.strip() or any(c in args.why for c in "\r\n"):
            raise Refused("--production, a one-line --why, and file paths are required")
        from scripts.production.production_doc import read_production
        _ = read_production(args.production)
        settings_read = conf.read("fast_track")
        config = limits(settings_read)
        paths = selected_paths(repo, args.paths, config)
        recovered = reconcile(repo, args.unit, finish=True, paths=tuple(paths), settings_read=settings_read)
        with select_patch(repo, paths) as selection:
            patch = selection.patch
            if recovered and not patch:
                print(f"already on main at {recovered[:12]}")
                return 0
            if not patch:
                raise Refused("no change")
            paths = [path for path, _, _ in patch_counts(repo, patch)]
            check_size(repo, paths, patch, config)
            commit = push_patch(repo, args.production, args.unit, args.why.strip(), paths, selection, config, settings_read)
        print(f"fast-track {args.unit} pushed main at {commit[:12]}")
        return 0
    except (Refused, PublishedFailure, copies.CopyError, OSError, ValueError, KeyError) as error:
        print(f"fast-track: {error}", file=sys.stderr)
        return 1 if isinstance(error, PublishedFailure) else 2


if __name__ == "__main__":
    raise SystemExit(main())
