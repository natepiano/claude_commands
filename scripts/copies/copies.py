#!/usr/bin/env python3
"""Run gates in one warm checkout per owner, with a second checkout when busy."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum, auto
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from threading import Event, Thread
from typing import NoReturn, cast, override

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib import conf, copy_record, file_lock, scratch
from scripts.lib.conf import UseConfiguredValue
from scripts.message import process_table, sessions
from scripts.lib.atomic_file import remove_dead_temporary_files, write_text_atomic

GATE_RECORD = ".gate.json"
LEGACY_WORKER_HOLD_REASON = "legacy crate worker"
OWNER = re.compile(r"[a-z0-9][a-z0-9-]*")
CLONE_STAGE = ".gate-clone"
GATE_SUFFIXES = ("", "-2")
COMMAND_LOCKS: ContextVar[tuple[int, ...]] = ContextVar("gate_command_locks", default=())
ACTIVE_GATES: ContextVar[tuple[Path, ...]] = ContextVar("active_gate_checkouts", default=())


class CopyError(Exception):
    """An invalid request or an unavailable checkout."""

    exit_code: int = 2


@dataclass(frozen=True)
class AllRepositories:
    """Remove copies from every repository."""


@dataclass(frozen=True)
class SelectedRepository:
    """Remove copies from one canonical repository name."""

    name: str


RepositoryScope = AllRepositories | SelectedRepository
ALL_REPOSITORIES = AllRepositories()


def repository_scope(repo: object) -> RepositoryScope:
    """Validate a repository scope at the public API boundary."""
    if isinstance(repo, SelectedRepository):
        if Path(repo.name).name != repo.name or repo.name in ("", ".", ".."):
            raise CopyError(f"{repo.name!r}: invalid repository name")
        return repo
    if isinstance(repo, AllRepositories):
        return repo
    raise CopyError("expected a repository scope: AllRepositories or SelectedRepository")


class BusyCopy(CopyError):
    """A checkout still has an active busy lock."""

    def __init__(self, tree: Path, known_holder: KnownHolder | None = None) -> None:
        self.tree: Path = tree
        self.holder: KnownHolder | UnreadableHolder = known_holder if known_holder is not None else holder(tree)
        super().__init__(self.description({item["pid"]: item["name"]
                                           for item in sessions.running_sessions() if item["name"]}))

    def description(self, names: Mapping[int, str]) -> str:
        if isinstance(self.holder, KnownHolder):
            name = names.get(self.holder.pid, "")
            if not name:
                try:
                    table = process_table.snapshot()
                    process = table.get(self.holder.pid)
                    if process is not None:
                        name = process_table.nearest_named(process, table, names) or ""
                except (OSError, subprocess.SubprocessError):
                    pass
            if name:
                return f"{self.tree}: busy; held by session {name}"
        return f"{self.tree}: busy; held by pid {self.holder}"


class PreparationError(CopyError):
    """Uncommitted changes or a merge could not be applied to the gate commit."""

    exit_code: int = 3


class PostUseError(CopyError):
    """The caller finished, but refreshing its standing checkout metadata failed."""

    def __init__(self, message: str, *, caller_error: BaseException | None = None) -> None:
        super().__init__(message)
        self.caller_error: BaseException | None = caller_error


class ClaimState(Enum):
    ACQUIRED = auto()
    BUSY = auto()
    REPLACED = auto()


@dataclass(frozen=True)
class GateLease:
    tree: Path
    descriptors: tuple[int, ...]


@dataclass(frozen=True)
class KnownHolder:
    pid: int

    @override
    def __str__(self) -> str:
        return str(self.pid)


@dataclass(frozen=True)
class UnreadableHolder:
    reason: str

    @override
    def __str__(self) -> str:
        return f"unreadable holder ({self.reason})"


@dataclass(frozen=True)
class GateRecord:
    owner: str
    repo: str
    source: str
    commit: str
    last_use: float
    holder_pid: int


@dataclass(frozen=True)
class CopiesConfiguration:
    root: Path
    wait_seconds: float
    git_timeout_seconds: float
    guard_timeout_seconds: float
    allow_list: dict[str, str]


def config_settings() -> tuple[Path, dict[str, str]]:
    path = Path(os.environ.get("COPIES_CONFIG") or Path(__file__).resolve().parents[2] / "config/copies.conf")
    path = path.expanduser()
    return path, conf.read(path).values()


def load_config() -> CopiesConfiguration:
    path, values = config_settings()
    try:
        root = Path(values["root"]).expanduser()
        delay = float(values["wait_seconds"])
        git_timeout = float(values["git_timeout_seconds"])
        guard_timeout = float(values["guard_timeout_seconds"])
        if not root.is_absolute():
            raise ValueError("root must be absolute")
        if any(not math.isfinite(value) or value <= 0 for value in (delay, git_timeout, guard_timeout)):
            raise ValueError("wait_seconds, git_timeout_seconds and guard_timeout_seconds must be finite and positive")
    except (KeyError, ValueError) as error:
        raise CopyError(f"{path}: invalid copies configuration: {error}") from error
    allow_list = {key.removeprefix("allow."): reason for key, reason in values.items()
                  if key.startswith("allow.") and reason}
    return CopiesConfiguration(root.resolve(), delay, git_timeout, guard_timeout, allow_list)


def fallback_owner(role: str) -> str:
    """Use a configured role owner when the caller has no production context."""
    path, values = config_settings()
    try:
        return checked_owner(values[f"owner.{role}"])
    except KeyError as error:
        raise CopyError(f"{path}: no configured owner for {role}") from error


def gates_root() -> Path:
    return load_config().root


def git(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
    timeout = load_config().git_timeout_seconds
    try:
        # Git children inherit the busy lock. Keep automatic maintenance synchronous.
        result = subprocess.run(["git", "-c", "maintenance.autoDetach=false", "-c", "gc.autoDetach=false",
                                 "-C", str(repo), *arguments], input=data,
                                capture_output=True, timeout=timeout, pass_fds=COMMAND_LOCKS.get())
    except subprocess.TimeoutExpired as error:
        raise CopyError(f"git {' '.join(arguments)} in {repo}: timed out after {timeout:g} seconds") from error
    if result.returncode:
        detail = "\n".join(part.decode(errors="replace").strip() for part in (result.stdout, result.stderr)
                           if part.strip()) or f"exit {result.returncode}"
        raise CopyError(f"git {' '.join(arguments)} in {repo}: {detail}")
    return result.stdout


def commit_at(repo: Path, revision: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()


def repository_name(source: Path) -> str:
    record = scratch.task_of(source)
    if isinstance(record, copy_record.Registered):
        original = record.fields.get("source_path")
        if isinstance(original, str) and Path(original) != source:
            source = Path(original)
    common = Path(git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    # Linked unit worktrees share the main repository's name, including .claude.
    return common.parent.name if common.name == ".git" else source.name


def checked_owner(owner: str) -> str:
    if not OWNER.fullmatch(owner):
        raise CopyError(f"{owner!r}: owner must match [a-z0-9][a-z0-9-]*")
    return owner


def tree_paths(owner: str, repo: str, pool: CopiesConfiguration) -> tuple[Path, ...]:
    parent = pool.root / repo
    return tuple(parent / f"{owner}-gate{suffix}" for suffix in GATE_SUFFIXES)


def read_record(tree: Path) -> GateRecord:
    try:
        raw = cast(object, json.loads((tree / GATE_RECORD).read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError("expected an object")
        fields = cast(dict[str, object], raw)
        labels = [fields.get(key) for key in ("owner", "repo", "source", "commit")]
        moment, pid = fields.get("last_use"), fields.get("holder_pid")
        if (not all(isinstance(value, str) for value in labels)
                or not isinstance(moment, (int, float)) or not isinstance(pid, int)):
            raise ValueError("invalid owner, repo, source, commit, last_use or holder_pid")
        owner, repo, source, commit = cast(list[str], labels)
        return GateRecord(owner, repo, source, commit, float(moment), pid)
    except (OSError, ValueError) as error:
        raise CopyError(f"{tree / GATE_RECORD}: unreadable gate record: {error}") from error


def write_record(tree: Path, owner: str, repo: str, source: Path, commit: str) -> None:
    fields = {"owner": owner, "repo": repo, "source": str(source), "commit": commit,
              "last_use": time.time(), "holder_pid": os.getpid()}
    write_text_atomic(tree / GATE_RECORD, json.dumps(fields) + "\n")


def holder(tree: Path) -> KnownHolder | UnreadableHolder:
    try:
        return KnownHolder(read_record(tree).holder_pid)
    except CopyError as error:
        for mark in (tree / scratch.BUSY, tree.parent / scratch.BUSY):
            pid = file_lock.lock_holder(str(mark))
            if pid is not None and pid > 0:
                return KnownHolder(pid)
        return UnreadableHolder(str(error))


@contextmanager
def claim_tree(tree: Path, *, create: bool, inherited_fds: list[int] | None = None) -> Generator[ClaimState]:
    """Use scratch's busy mark; reject a directory replaced during acquisition."""
    try:
        if create:
            tree.mkdir(parents=True, exist_ok=True)
        if tree.is_symlink() or not tree.is_dir():
            if not create and not tree.exists():
                yield ClaimState.REPLACED
                return
            raise CopyError(f"{tree}: expected a standing checkout directory")
        before = tree.stat()
    except FileNotFoundError:
        yield ClaimState.REPLACED
        return
    except OSError as error:
        raise CopyError(f"{tree}: cannot claim checkout: {error}") from error
    with scratch.busy_locks(tree, create=True, inherited_fds=inherited_fds) as idle:
        try:
            after = tree.stat()
            same = (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        except FileNotFoundError:
            same = False
        yield ClaimState.ACQUIRED if idle and same else ClaimState.BUSY if same else ClaimState.REPLACED


@contextmanager
def available_tree(owner: str, repo: str, pool: CopiesConfiguration) -> Generator[GateLease]:
    candidates = tree_paths(owner, repo, pool)
    announced = False
    wake = Event()

    def first_tree_released() -> None:
        try:
            # Wait on the existing busy mark, using scratch's only flock path.
            with scratch.busy_locks(candidates[0], create=False, wait=True):
                pass
        finally:
            wake.set()

    waiter = Thread(target=first_tree_released, daemon=True)
    while True:
        for tree in candidates:
            if removing_path(tree).exists():
                continue
            descriptors: list[int] = []
            with claim_tree(tree, create=not tree.exists(), inherited_fds=descriptors) as state:
                if state is ClaimState.ACQUIRED:
                    yield GateLease(tree, tuple(descriptors))
                    return
        if not announced:
            held = "; ".join(f"{tree} held by pid {holder(tree)}" for tree in candidates)
            print(f"copies: waiting for {held}", file=sys.stderr, flush=True)
            announced = True
        if (candidates[0] / scratch.BUSY).exists() and not removing_path(candidates[0]).exists():
            if not waiter.is_alive():
                wake.clear()
                waiter = Thread(target=first_tree_released, daemon=True)
                waiter.start()
        _ = wake.wait(pool.wait_seconds)


def initialize(tree: Path, source: Path, owner: str, repo: str, base: str) -> None:
    remove_dead_temporary_files(tree / GATE_RECORD)
    replace_git = False
    if (tree / GATE_RECORD).exists():
        previous = read_record(tree)
        if previous.owner != owner or previous.repo != repo:
            raise CopyError(f"{tree}: gate record belongs to another owner or repository")
        replace_git = previous.source != str(source)
    elif any(child.name != scratch.BUSY for child in tree.iterdir()):
        raise CopyError(f"{tree}: refusing an unregistered existing directory")
    if (tree / ".git").is_dir() and not replace_git:
        try:
            _ = git(tree, "rev-list", "--objects", "--all", "--quiet")
        except CopyError:
            replace_git = True
    write_record(tree, owner, repo, source, base)
    if replace_git and (tree / ".git").is_dir() and not (tree / ".git").is_symlink():
        shutil.rmtree(tree / ".git")
    initialize_git(tree, source)


def source_origin(source: Path) -> tuple[str, str]:
    """Return the upstream URL and the source refs that represent it."""
    if "origin" in git(source, "remote").decode().splitlines():
        return git(source, "remote", "get-url", "origin").decode().strip(), "refs/remotes/origin"
    return str(source), "refs/heads"


def resolved_remote_url(repo: Path, url: str) -> str:
    """Resolve a local remote path from its repository."""
    if re.match(r"^[^/]+:", url):
        return url
    return str((repo / Path(url).expanduser()).resolve())


def refresh_remotes(tree: Path, source: Path) -> None:
    """Keep upstream refs and saved local branches in separate remotes."""
    url, refs = source_origin(source)
    for remote, remote_url, source_refs in (("origin", url, refs), ("source", str(source), "refs/heads")):
        mapping = f"+{source_refs}/*:refs/remotes/{remote}/*"
        _ = git(tree, "config", "--replace-all", f"remote.{remote}.url", remote_url)
        _ = git(tree, "config", "--replace-all", f"remote.{remote}.fetch",
                f"+refs/heads/*:refs/remotes/{remote}/*")
        # Pruning keeps symbolic HEAD refs. Remove them before copying the source's refs.
        _ = git(tree, "update-ref", "--no-deref", "-d", f"refs/remotes/{remote}/HEAD")
        _ = git(tree, "fetch", "-q", "--prune", "--", str(source), mapping)


def initialize_git(tree: Path, source: Path) -> None:
    """Create private Git metadata while the caller holds the busy mark."""
    if not (tree / ".git").exists():
        # clone needs an empty destination; the tree already holds scratch's busy mark.
        # Stage only Git metadata, so first creation never has an unlocked checkout.
        stage = tree / CLONE_STAGE
        if stage.exists():
            shutil.rmtree(stage)
        _ = git(source, "clone", "--shared", "--no-checkout", "--quiet", "--", str(source), str(stage))
        _ = (stage / ".git").rename(tree / ".git")
        stage.rmdir()
    if (tree / ".git").is_symlink() or not (tree / ".git").is_dir():
        raise CopyError(f"{tree}: standing checkout must own its .git directory")
    actual = git(tree, "rev-parse", "--absolute-git-dir").decode().strip()
    if Path(actual).resolve() != (tree / ".git").resolve():
        raise CopyError(f"{tree}: Git directory is {actual}, expected {tree / '.git'}")
    refresh_remotes(tree, source)
    exclude = tree / ".git/info/exclude"
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    entries = [entry for entry in (f"/{GATE_RECORD}", f"/{scratch.BUSY}", "/target/")
               if entry not in text.splitlines()]
    if entries:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(exclude, text + ("\n" if text and not text.endswith("\n") else "")
                          + "\n".join(entries) + "\n")


def clone(source: Path, task: str = "", word: str = "hana", now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    """Create a task-owned clone. Keep partial files owned if Git fails."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    path = scratch.new(task, word, now=now, scratch_settings=scratch_settings)
    _ = scratch.update_record(path, lambda record: {
        **(record.fields if isinstance(record, copy_record.Registered) else {}),
        "test_hana_clone": True, "source_path": str(source.resolve()),
    }, scratch_settings=scratch_settings)
    checkout = path / "checkout"
    try:
        # The task folder keeps one lock path while its checkout gains Git metadata.
        with claim_tree(path, create=False) as state:
            if state is not ClaimState.ACQUIRED:
                raise CopyError(f"{path}: task folder is busy")
            checkout.mkdir()
            initialize_git(checkout, source.resolve())
            _ = git(checkout, "reset", "--hard", "--quiet")
            from scripts.accounts import accounts

            if accounts.any_claude_home_trusts(source):
                folder = str(checkout.resolve())
                _ = scratch.update_record(path, lambda record: {
                    **(record.fields if isinstance(record, copy_record.Registered) else {}),
                    "claude_trust_folder": folder,
                }, scratch_settings=scratch_settings)
                accounts.set_claude_folder_trust(checkout, trusted=True)
    except (OSError, CopyError) as error:
        raise CopyError(f"clone failed; its task still owns the partial folder {path}: {error}") from error
    return checkout


def apply_changes(tree: Path, worktree: Path, base: str) -> None:
    changed: list[str] = []
    try:
        changed = [os.fsdecode(name) for name in git(worktree, "diff", "--name-only", "-z", "HEAD").split(b"\0")
                   if name]
        diff = git(worktree, "diff", "--binary", "HEAD", "--")
        if diff:
            _ = git(tree, "apply", "--index", "--3way", "--binary", "-", data=diff)
        for name in git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
            if not name:
                continue
            relative = Path(os.fsdecode(name))
            changed.append(str(relative))
            if relative.parts[0] in (GATE_RECORD, scratch.BUSY, ".git", CLONE_STAGE):
                raise CopyError(f"{relative}: path conflicts with gate metadata")
            original, destination = worktree / relative, tree / relative
            mode = original.lstat().st_mode
            if not destination.parent.resolve().is_relative_to(tree.resolve()):
                raise CopyError(f"{relative}: path escapes the gate checkout")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                destination.unlink()
            if stat.S_ISLNK(mode):
                destination.symlink_to(os.readlink(original))
            elif stat.S_ISREG(mode):
                _ = shutil.copyfile(original, destination)
                shutil.copymode(original, destination)
            else:
                raise CopyError(f"{relative}: not a regular file or symlink")
            _ = git(tree, "--literal-pathspecs", "add", "--force", "--", str(relative))
        short = git(tree, "rev-parse", "--short", base).decode().strip()
        _ = git(tree, "-c", "user.name=copies.py", "-c", "user.email=copies@localhost",
                "-c", "commit.gpgsign=false", "commit", "--quiet", "--no-verify", "--allow-empty", "-m",
                f"gate: uncommitted {worktree} at {short}")
    except (CopyError, OSError) as error:
        files = ", ".join(changed) or "no changed files could be read"
        raise PreparationError(f"cannot apply uncommitted {worktree}; files: {files}; {error}") from error


def merge_changes(tree: Path, source: Path, revision: str) -> None:
    try:
        if revision.startswith("origin/"):
            branch = revision.removeprefix("origin/")
            origin, _ = source_origin(source)
            origin = resolved_remote_url(source, origin)
            _ = git(source, "check-ref-format", f"refs/heads/{branch}")
            _ = git(tree, "fetch", "-q", "--", origin,
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            commit = commit_at(tree, revision)
        else:
            commit = commit_at(source, revision)
            _ = git(tree, "fetch", "-q", "--", str(source), commit)
        _ = git(tree, "-c", "user.name=copies.py", "-c", "user.email=copies@localhost",
                "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-edit", "--no-verify", commit)
    except CopyError as error:
        conflicts = git(tree, "diff", "--name-only", "--diff-filter=U").decode(errors="replace").strip()
        raise PreparationError(f"cannot merge {revision}; files: {conflicts or 'see git error'}; {error}") from error


def mark_target(tree: Path) -> None:
    """Refresh only the gate's own cache, including when a caller replaced target."""
    target = tree / "target"
    if not target.is_symlink():
        scratch.mark_cargo_cache(target)


def prepare(tree: Path, source: Path, owner: str, name: str, base: str,
            apply_uncommitted: str | Path | None, merge: str | None) -> None:
    """Convert only our own preparation errors, before entering caller code."""
    try:
        initialize(tree, source, owner, name, base)
        _ = git(tree, "fetch", "-q", "--", str(source), base)
        _ = git(tree, "checkout", "-q", "--force", "--detach", base)
        _ = git(tree, "clean", "-ffdq")
        target = tree / "target"
        if target.is_symlink():
            target.unlink()
        mark_target(tree)
        if apply_uncommitted is not None:
            apply_changes(tree, Path(apply_uncommitted).expanduser().resolve(), base)
        if merge is not None:
            merge_changes(tree, source, merge)
        write_record(tree, owner, name, source, commit_at(tree, "HEAD"))
    except (OSError, scratch.ScratchError) as error:
        raise CopyError(str(error)) from error


def workspace_path(owner: str, repo: str | Path, name: str) -> Path:
    """Locate a named editable copy in the existing scratch registry."""
    owner = checked_owner(owner)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise CopyError(f"{name!r}: invalid workspace name")
    source = Path(repo).expanduser().resolve()
    return copy_record.copies_root() / f"{repository_name(source)}-{owner}" / name


@contextmanager
def workspace(owner: str, repo: str | Path, commit: str, *, name: str, branch: str,
              task: str = "", path: Path | None = None, run_manifest: Path | None = None) -> Generator[Path]:
    """Hold a registered editable copy without resetting edits during recovery."""
    source = Path(repo).expanduser().resolve()
    base = commit_at(source, commit)
    owner = checked_owner(owner)
    tree = path.resolve() if path is not None else workspace_path(owner, source, name)
    legacy = copy_record.copy_folder(tree) != tree
    if legacy and not tree.is_dir():
        raise CopyError(f"{tree}: a legacy workspace must already exist")
    branch = branch.removeprefix("refs/heads/")
    _ = git(source, "check-ref-format", f"refs/heads/{branch}")
    common = git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
    descriptors: list[int] = []
    with claim_tree(tree, create=True, inherited_fds=descriptors) as state:
        if state is not ClaimState.ACQUIRED:
            raise CopyError(f"{tree}: workspace is busy")
        token = COMMAND_LOCKS.set(tuple(descriptors))
        try:
            record = scratch.task_of(tree)
            task = scratch.task_or_default(task or (record.task if isinstance(record, copy_record.Registered) else ""), time.time())

            linked_run = record.fields.get("crate_run", "") if isinstance(record, copy_record.Registered) else ""
            manifest_path = str(run_manifest.resolve()) if run_manifest is not None else linked_run
            if not isinstance(manifest_path, str) or linked_run and manifest_path != linked_run:
                raise CopyError(f"{tree}: crate run link changed")

            def register(copy_commit: str) -> None:
                if legacy:
                    _ = scratch.hold(tree, task, LEGACY_WORKER_HOLD_REASON)
                    fields: dict[str, object] = dict(owner_unit=owner, source_path=str(source),
                        source_commit=base, copy_commit=copy_commit, target_mode="none",
                        seed_source="", borrows_from=common)
                    if manifest_path:
                        fields["crate_run"] = manifest_path
                    _ = scratch.update_record(tree, lambda _: scratch.fields_for_task(fields, task))
                    scratch.exclude_copy_metadata(tree)
                else:
                    scratch.register_copy(tree, task=task, owner_unit=owner, source_path=str(source),
                                          source_commit=base, copy_commit=copy_commit, target_mode="none",
                                          seed_source="", borrows_from=common, crate_run=manifest_path)

            if isinstance(record, copy_record.Registered):
                expected = {"owner_unit": owner, "source_path": str(source), "source_commit": base}
                if any(record.fields.get(key) != value for key, value in expected.items()):
                    raise CopyError(f"{tree}: workspace belongs to another request")
            elif legacy:
                if not isinstance(record, copy_record.NotRegistered):
                    raise CopyError(f"{tree}: unreadable legacy workspace")
                origin = git(tree, "remote", "get-url", "origin").decode().strip()
                upstream, _ = source_origin(source)
                resolved_origin = resolved_remote_url(tree, origin)
                if resolved_origin not in (resolved_remote_url(source, upstream), str(source)):
                    raise CopyError(f"{tree}: legacy workspace source differs")
                _ = git(tree, "merge-base", "--is-ancestor", base, "HEAD")
                if git(tree, "symbolic-ref", "HEAD").decode().strip() != f"refs/heads/{branch}":
                    raise CopyError(f"{tree}: workspace branch changed")
                if (tree / ".git").is_symlink() or not (tree / ".git").is_dir():
                    raise CopyError(f"{tree}: workspace must own its Git directory")
                register(commit_at(tree, "HEAD"))
                record = scratch.task_of(tree)
            else:
                if not isinstance(record, copy_record.NotRegistered) or any(
                        child.name != scratch.BUSY for child in tree.iterdir()):
                    raise CopyError(f"{tree}: refusing an unregistered workspace")
                register("")
                record = scratch.task_of(tree)
            assert isinstance(record, copy_record.Registered)
            if record.fields.get("copy_commit"):
                if (tree / ".git").is_symlink() or not (tree / ".git").is_dir():
                    raise CopyError(f"{tree}: workspace must own its Git directory")
                if git(tree, "symbolic-ref", "HEAD").decode().strip() != f"refs/heads/{branch}":
                    raise CopyError(f"{tree}: workspace branch changed")
                if record.task != task or manifest_path != linked_run:
                    register(str(record.fields["copy_commit"]))
                else:
                    scratch.renew(tree)
                refresh_remotes(tree, source)
            else:
                initialize_git(tree, source)
                _ = git(tree, "checkout", "-q", "-B", branch, base)
                for key in ("user.name", "user.email"):
                    _ = git(tree, "config", key, git(source, "config", key).decode().strip())
                register(base)
            try:
                yield tree
            finally:
                caller_error = sys.exception()
                try:
                    refresh_remotes(tree, source)
                    mark_target(tree)
                    register(commit_at(tree, "HEAD"))
                except (OSError, scratch.ScratchError, CopyError) as error:
                    raise PostUseError(f"{tree}: workspace refresh failed: {error}",
                                       caller_error=caller_error) from error
        except (OSError, scratch.ScratchError) as error:
            raise CopyError(str(error)) from error
        finally:
            COMMAND_LOCKS.reset(token)


def end_workspaces(task: str, paths: list[Path]) -> None:
    """End native copies for the sweep and remove adopted workers through guarded cleanup."""
    holds = scratch.holds()
    changed = False
    for hold in holds.values():
        if hold["task"] == task:
            hold["ended"] = time.time()
            changed = True
    if changed:
        scratch.write_holds(holds)
    for tree in paths:
        if copy_record.copy_folder(tree) != tree.resolve():
            if (not tree.exists() and isinstance(scratch.pending_removal(tree), copy_record.NotRegistered)
                    or scratch.end_copy(tree, task, time.time())):
                hold = scratch.holds().get(str(tree.resolve()))
                if hold is not None and hold["task"] == task:
                    _ = scratch.release(tree)
            continue
        if not tree.exists():
            continue
        with claim_tree(tree, create=False) as state:
            if state is not ClaimState.ACQUIRED:
                raise CopyError(f"{tree}: workspace is busy during task completion")
            def ended(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
                if not isinstance(record, copy_record.Registered) or record.task != task:
                    raise CopyError(f"{tree}: workspace task differs")
                return {**record.fields, "ended": time.time(), "ended_task": task}
            _ = scratch.update_record(tree, ended)


@contextmanager
def standing(owner: str, repo: str | Path, commit: str, *, apply_uncommitted: str | Path | None = None,
             merge: str | None = None) -> Generator[Path]:
    """Hold a warm gate checkout through preparation and the caller's entire use."""
    owner = checked_owner(owner)
    try:
        source = Path(repo).expanduser().resolve()
        name = repository_name(source)
        base = commit_at(source, commit)
    except OSError as error:
        raise CopyError(str(error)) from error
    if (source in ACTIVE_GATES.get() and read_record(source).owner == owner
            and commit_at(source, "HEAD") == base and apply_uncommitted is None and merge is None):
        yield source
        return
    with available_tree(owner, name, load_config()) as lease:
        tree = lease.tree
        token = COMMAND_LOCKS.set(lease.descriptors)
        active = ACTIVE_GATES.set((*ACTIVE_GATES.get(), tree))
        try:
            prepare(tree, source, owner, name, base, apply_uncommitted, merge)
            try:
                yield tree
            finally:
                caller_error = sys.exception()
                try:
                    refresh_remotes(tree, source)
                    mark_target(tree)
                    write_record(tree, owner, name, source, commit_at(tree, "HEAD"))
                except (OSError, scratch.ScratchError, CopyError) as error:
                    raise PostUseError(f"{tree}: post-use refresh failed: {error}",
                                       caller_error=caller_error) from error
        finally:
            ACTIVE_GATES.reset(active)
            COMMAND_LOCKS.reset(token)


def list_trees() -> None:
    root = gates_root()
    if not root.exists():
        return
    for directory in sorted(root.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            continue
        for tree in sorted(directory.iterdir()):
            if tree.is_symlink() or not tree.is_dir():
                continue
            try:
                record = read_record(tree)
            except CopyError as error:
                print(f"{tree}\t{error}")
                continue
            with scratch.busy_locks(tree, create=False) as idle:
                state = "idle" if idle else "busy"
            target = tree / "target"
            disk = "0"
            if target.exists():
                size = subprocess.run(["du", "-sh", str(target)], capture_output=True, text=True)
                words = size.stdout.split()
                disk = words[0] if size.returncode == 0 and words else "unreadable"
            print(f"{tree}\t{record.owner}\t{record.repo}\t{record.commit}\t{state}\t{record.holder_pid}\t{disk}")


def removing_path(tree: Path) -> Path:
    return tree.with_name(tree.name + ".removing")


def workspace_repository(record: copy_record.Registered) -> str:
    """Read repository provenance without requiring the source checkout to exist."""
    seen: set[str] = set()
    while True:
        common = record.fields.get("borrows_from")
        if not isinstance(common, str) or common in seen:
            source = record.fields.get("source_path")
            return Path(source).name if isinstance(source, str) else ""
        seen.add(common)
        git_dir = Path(common)
        if git_dir.name != ".git":
            source = record.fields.get("source_path")
            return Path(source).name if isinstance(source, str) else ""
        parent = scratch.task_of(git_dir.parent)
        if not isinstance(parent, copy_record.Registered):
            return git_dir.parent.name
        record = parent


def owned_workspaces(owner: str, scope: RepositoryScope = ALL_REPOSITORIES) -> list[tuple[Path, copy_record.Registered]]:
    """Select registered copies and adopted copies without depending on a live source."""
    root = copy_record.copies_root()
    paths = set(scratch.registered_copy_paths())
    paths.update(scratch.pending_copy_paths(root))
    held = scratch.holds()
    paths.update(Path(path) for path in held)
    found: list[tuple[Path, copy_record.Registered]] = []
    for tree in sorted(paths):
        record = scratch.task_of(tree)
        if isinstance(record, copy_record.UnreadableRecord):
            raise CopyError(record.reason)
        if isinstance(record, copy_record.NotRegistered):
            hold = held.get(str(tree))
            if (copy_record.copy_folder(tree) != tree.resolve()
                    and (hold is None or hold["reason"] != LEGACY_WORKER_HOLD_REASON)):
                continue
            raise CopyError(f"repair record {tree / copy_record.RECORD}: copy is not registered")
        if record.fields.get("owner_unit") != owner:
            continue
        if isinstance(scope, SelectedRepository) and workspace_repository(record) != scope.name:
            continue
        found.append((tree, record))
    selected = dict(found)
    ordered: list[tuple[Path, copy_record.Registered]] = []
    visited: set[Path] = set()
    visiting: set[Path] = set()

    def visit(tree: Path) -> None:
        if tree in visited:
            return
        if tree in visiting:
            raise CopyError(f"{tree}: borrowed objects form a cycle; repair the copy records")
        visiting.add(tree)
        for child, record in found:
            borrowed = record.fields.get("borrows_from")
            if isinstance(borrowed, str) and borrowed and Path(borrowed).resolve() == (tree / ".git").resolve():
                visit(child)
        visiting.remove(tree)
        visited.add(tree)
        ordered.append((tree, selected[tree]))

    for tree, _ in found:
        visit(tree)
    return ordered


@contextmanager
def workspace_removal_guard(tree: Path, record: copy_record.Registered) -> Generator[Callable[[], None]]:
    """Lock the linked run before the copy, then read its state under both locks."""
    if "crate_run" not in record.fields:
        yield lambda: None
        return
    link = record.fields["crate_run"]
    if not isinstance(link, str) or not Path(link).is_absolute():
        raise CopyError(f"{tree}: repair crate run link in {tree / copy_record.RECORD}")
    path = Path(link)
    stage_summary = ""

    def validate() -> None:
        nonlocal stage_summary
        try:
            value = cast(object, json.loads(path.read_text()))
            if not isinstance(value, dict):
                raise ValueError("expected a manifest object")
            manifest = cast(dict[str, object], value)
            crates = manifest.get("crates")
            if (manifest.get("version") != 2 or manifest.get("canonical") != record.fields.get("source_path")
                    or manifest.get("base") != record.fields.get("source_commit")
                    or not isinstance(crates, dict) or not crates):
                raise ValueError("manifest authority differs or crate records are missing")
            stages: list[str] = []
            matched = False
            terminal = True
            for package, item in cast(dict[str, object], crates).items():
                if not isinstance(item, dict):
                    raise ValueError("invalid crate record")
                fields = cast(dict[str, object], item)
                checkout, stage = fields.get("checkout"), fields.get("stage")
                if not isinstance(checkout, str) or not isinstance(stage, str):
                    raise ValueError("crate checkout or stage is missing")
                matched |= Path(checkout).resolve() == tree.resolve()
                stages.append(f"{package}={stage}")
                terminal &= stage in ("integrated", "abandoned")
            if not matched:
                raise ValueError("manifest does not own this copy")
            stage_summary = ", ".join(stages)
            if not terminal:
                raise CopyError(f"{tree}: crate run {path} is not terminal; stages: {stage_summary}")
        except (OSError, UnicodeError, ValueError) as error:
            raise CopyError(f"{tree}: repair crate run {path}: {error}") from error

    try:
        with file_lock.exclusive(path.parent / "run.lock", wait=False) as state:
            if state is file_lock.LockResult.BUSY:
                pid = file_lock.lock_holder(str(path.parent / "run.lock"))
                busy = BusyCopy(tree, KnownHolder(pid) if pid is not None else None)
                try:
                    validate()
                except CopyError as error:
                    raise CopyError(f"{busy}; {error}") from error
                raise CopyError(f"{busy}; crate run {path}; stages: {stage_summary}")
            yield validate
    except OSError as error:
        raise CopyError(f"{tree}: repair crate run {path}: {error}") from error


def remove_trees(owner: str, repo: RepositoryScope = ALL_REPOSITORIES) -> None:
    """Remove an owner's idle trees within the selected repository scope."""
    pool = load_config()
    scope = repository_scope(repo)
    directories = ([pool.root / scope.name] if isinstance(scope, SelectedRepository) else
                   sorted(path for path in pool.root.iterdir() if path.is_dir() and not path.is_symlink())
                   if pool.root.exists() else [])
    if not OWNER.fullmatch(owner):
        directories = []
    workspaces = owned_workspaces(owner, scope)
    with ExitStack() as locks:
        present: list[Path] = []
        for directory in directories:
            for candidate in tree_paths(owner, directory.name, pool):
                for tree in (removing_path(candidate), candidate):
                    if not tree.exists():
                        continue
                    state = locks.enter_context(claim_tree(tree, create=False))
                    if state is ClaimState.REPLACED:
                        continue
                    if state is not ClaimState.ACQUIRED:
                        raise BusyCopy(tree)
                    if (tree / GATE_RECORD).exists():
                        record = read_record(tree)
                        if record.owner != owner or record.repo != directory.name:
                            raise CopyError(f"{tree}: gate record belongs to another owner or repository")
                    present.append(tree)
        for tree, record in workspaces:
            try:
                with workspace_removal_guard(tree, record) as validate:
                    if not scratch.end_selected_copy(tree, record, time.time(), validate=validate):
                        with scratch.judgment_lock(tree) as idle:
                            if not idle:
                                raise BusyCopy(tree)
                        reason = scratch.unsaved_work(tree) or str(scratch.lifetime(tree))
                        raise CopyError(f"{tree}: kept: {reason}")
            except scratch.ScratchError as error:
                raise CopyError(str(error)) from error
            _ = scratch.release(tree)
            print(f"removed {tree}")
        for tree in present:
            tombstone = tree if tree.name.endswith(".removing") else removing_path(tree)
            if tombstone != tree:
                _ = tree.rename(tombstone)
            shutil.rmtree(tombstone)
            print(f"removed {tree}")


class CopyParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise CopyError(message)


class CopyArguments(argparse.Namespace):
    action: str = ""
    owner: str = ""
    repo: str | None = None
    commit: str = ""
    apply_uncommitted: str | None = None
    merge: str | None = None
    command: list[str] = []
    source: str = ""
    task: str = ""
    until: str = ""
    name: str = "hana"


def main(argv: list[str] | None = None) -> int:
    parser = CopyParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    clone_command = commands.add_parser("clone", help="create a task-owned test clone")
    _ = clone_command.add_argument("source")
    ends = clone_command.add_mutually_exclusive_group()
    _ = ends.add_argument("--task", default="")
    _ = ends.add_argument("--until", default="")
    _ = clone_command.add_argument("--name", default="hana")
    run = commands.add_parser("run", help="prepare a standing tree and run a command in it")
    remove = commands.add_parser("remove", help="remove an owner's idle standing trees")
    for command in (run, remove):
        _ = command.add_argument("--owner", required=True)
    _ = run.add_argument("--repo", required=True)
    _ = remove.add_argument("--repo")
    _ = run.add_argument("--commit", required=True)
    _ = run.add_argument("--apply-uncommitted")
    _ = run.add_argument("--merge")
    _ = run.add_argument("command", nargs=argparse.REMAINDER)
    _ = commands.add_parser("list", help="list standing trees and their busy state")
    try:
        args = parser.parse_args(argv, namespace=CopyArguments())
        if args.action == "clone":
            task = scratch.parsed_until(args.until) if args.until else args.task
            print(clone(Path(args.source), task, args.name))
            return 0
        if args.action == "run":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise CopyError("run requires a command after --")
            with standing(args.owner, args.repo or "", args.commit,
                          apply_uncommitted=args.apply_uncommitted, merge=args.merge) as tree:
                result = subprocess.run([part.replace("{tree}", str(tree)) for part in command],
                                        cwd=tree, pass_fds=COMMAND_LOCKS.get())
                return result.returncode if result.returncode >= 0 else 128 - result.returncode
        if args.action == "list":
            list_trees()
        else:
            scope = (ALL_REPOSITORIES if args.repo is None else
                     SelectedRepository(repository_name(Path(args.repo).expanduser().resolve())))
            remove_trees(args.owner, scope)
    except (CopyError, OSError, scratch.ScratchError) as error:
        print(f"copies: {error}", file=sys.stderr)
        return error.exit_code if isinstance(error, CopyError) else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
