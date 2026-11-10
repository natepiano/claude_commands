#!/usr/bin/env python3
"""Run gates in fresh copies or reusable copies with build output."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum, auto
import json
import fcntl
import hashlib
import shlex
import math
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from threading import Event, Thread
from typing import TYPE_CHECKING, NoReturn, TextIO, cast, override
from urllib.parse import unquote, urlsplit
from uuid import UUID


from scripts.lib import build_folders, conf, copy_record, file_lock, scratch
from scripts.lib.copy_record import (
    AllRepositories as AllRepositories,
    SelectedRepository as SelectedRepository,
    RepositoryScope as RepositoryScope,
    ALL_REPOSITORIES as ALL_REPOSITORIES,
)
from scripts.lib.conf import UseConfiguredValue
from scripts.lib.process_failure import failure_detail
from scripts.message import process_table, sessions
from scripts.lib.atomic_file import remove_dead_temporary_files, write_text_atomic
from scripts.mac_test import floor_claims
if TYPE_CHECKING:
    from scripts.lint import tmp_sweep
    from scripts.production import staff

# Checksums skip equal files. Changed files get the receiver write time.
RSYNC_RECEIVER_WRITE_TIME_FLAGS = "-rlpc"
GATE_RECORD = ".gate.json"
LEGACY_WORKER_HOLD_REASON = "legacy crate worker"
OWNER = re.compile(r"[a-z0-9][a-z0-9_-]*")
CLONE_STAGE = ".gate-clone"
GATE_SUFFIXES = ("", "-2")
COMMAND_LOCKS: ContextVar[tuple[int, ...]] = ContextVar("gate_command_locks", default=())
ACTIVE_GATES: ContextVar[tuple[Path, ...]] = ContextVar("active_gate_checkouts", default=())


class CopyError(Exception):
    """An invalid request or an unavailable checkout."""

    exit_code: int = 2


@dataclass(frozen=True)
class StaffOwner:
    """A fixed staff row owns the copy."""

    row_id: str


@dataclass(frozen=True)
class RoleOwner:
    """A fixed role owns the copy without a staff row."""

    name: str


CopyOwner = StaffOwner | RoleOwner


def copy_owner(text: str) -> CopyOwner:
    """Read a row identity or a fixed role at the text boundary."""
    try:
        identity = str(UUID(text))
    except ValueError:
        return RoleOwner(text)
    return StaffOwner(identity)


def owner_name(owner: CopyOwner) -> str:
    """Read the current name without changing the copy's folder."""
    if isinstance(owner, RoleOwner):
        return owner.name
    from scripts.production import staff

    try:
        row = staff.local_snapshot().by_id(staff.StaffId(owner.row_id))
    except staff.StaffUnreadable:
        row = None
    return row.name if row is not None else owner.row_id


@dataclass(frozen=True)
class CurrentStaffOwnerName:
    name: str


@dataclass(frozen=True)
class RoleOwnerName:
    name: str


@dataclass(frozen=True)
class OwnerNameUnavailable:
    """The list cannot resolve the stored staff owner to a current name."""


OwnerDisplay = CurrentStaffOwnerName | RoleOwnerName | OwnerNameUnavailable


@dataclass(frozen=True)
class UnreadStaffNames:
    """The list has not needed staff names yet."""


@dataclass(frozen=True)
class AvailableStaffNames:
    snapshot: staff.StaffSnapshot


@dataclass(frozen=True)
class UnavailableStaffNames:
    """The staff read failed for this list call."""


class ListStaffNames:
    """Use one staff snapshot for all names in a list call."""

    def __init__(self) -> None:
        self._state: UnreadStaffNames | AvailableStaffNames | UnavailableStaffNames = UnreadStaffNames()

    def read(self) -> AvailableStaffNames | UnavailableStaffNames:
        if isinstance(self._state, UnreadStaffNames):
            from scripts.production import staff

            try:
                self._state = AvailableStaffNames(staff.local_snapshot())
            except staff.StaffUnreadable:
                self._state = UnavailableStaffNames()
        return self._state


def list_owner_name(owner: CopyOwner, staff_names: ListStaffNames) -> OwnerDisplay:
    """Resolve an owner for display without changing its stored identity or folder name."""
    if isinstance(owner, RoleOwner):
        return RoleOwnerName(owner.name)
    from scripts.production import staff

    names = staff_names.read()
    if isinstance(names, UnavailableStaffNames):
        return OwnerNameUnavailable()
    row = names.snapshot.by_id(staff.StaffId(owner.row_id))
    return CurrentStaffOwnerName(row.name) if row is not None else OwnerNameUnavailable()


def _owner_display_text(owner: OwnerDisplay) -> str:
    return owner.name if isinstance(owner, (CurrentStaffOwnerName, RoleOwnerName)) else "unavailable"


def repository_scope(repo: object) -> RepositoryScope:
    """Validate a repository scope at the public API boundary."""
    if isinstance(repo, SelectedRepository):
        if Path(repo.name).name != repo.name or repo.name in ("", ".", ".."):
            raise CopyError(f"{repo.name!r}: invalid repository name")
        return repo
    if isinstance(repo, AllRepositories):
        return repo
    raise CopyError("expected a repository scope: AllRepositories or SelectedRepository")


@dataclass(frozen=True)
class CommittedSource:
    """Copy one committed revision."""

    revision: str


@dataclass(frozen=True)
class WorktreeSource:
    """Apply all worktree changes to a committed revision."""

    revision: str
    worktree: str | Path


@dataclass(frozen=True)
class StagedSource:
    """Copy only the specified staged tree."""

    tree: str


@dataclass(frozen=True)
class NoMerge:
    """Keep the source without an additional merge."""


@dataclass(frozen=True)
class MergeRevision:
    """Merge the named revision after source preparation."""

    revision: str


SourcePolicy = CommittedSource | WorktreeSource | StagedSource
MergePolicy = NoMerge | MergeRevision
NO_MERGE = NoMerge()


def staged_tree(source: Path) -> str:
    """Read the staged tree without changing the source index."""
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
            build_folders.FOLDER_PREFIXES["copies-index"])) as temporary:
        index = Path(git(source, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        private = temporary / "index"
        if index.exists():
            _ = shutil.copyfile(index, private)
        result = subprocess.run(["git", "-C", str(source), "write-tree"],
                                env={**os.environ, "GIT_INDEX_FILE": str(private)},
                                capture_output=True, timeout=load_config().git_timeout_seconds)
        if result.returncode:
            raise PreparationError(result.stderr.decode(errors="replace"))
        return result.stdout.decode().strip()


def source_commit(source: Path, policy: SourcePolicy) -> str:
    """Give a staged tree a private commit without changing any source refs."""
    if isinstance(policy, (CommittedSource, WorktreeSource)):
        return commit_at(source, policy.revision)
    tree = policy.tree
    return git(source, "-c", "user.name=copies.py", "-c", "user.email=copies@localhost",
               "commit-tree", tree, "-p", commit_at(source, "HEAD"),
               data=b"copies: staged source\n").decode().strip()


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
    """Cleanup or a metadata refresh failed after the caller used a checkout."""

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
class NoGateCache:
    """This gate needs no retained Cargo cache."""


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
class TrackedFileTime:
    """The time last used for one tracked blob in a gate cache."""

    blob: str
    mtime_ns: int


NO_TRACKED_TIMES: Mapping[str, TrackedFileTime] = {}


@dataclass(frozen=True)
class GateRecord:
    owner: str
    repo: str
    source: str
    commit: str
    last_use: float
    holder_pid: int
    tracked_times: dict[str, TrackedFileTime] = field(default_factory=dict)


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


def guard_choice_limit() -> int:
    """Read the branch limit, using the repository setting for older configuration files."""
    path, values = config_settings()
    if "guard_choice_limit" not in values:
        path = Path(__file__).resolve().parents[2] / "config/copies.conf"
        values = conf.read(path).values()
    try:
        limit = int(values["guard_choice_limit"])
        if limit <= 0:
            raise ValueError("guard_choice_limit must be positive")
    except (KeyError, ValueError) as error:
        raise CopyError(f"{path}: invalid copies configuration: {error}") from error
    return limit


def unit_owner(name: str) -> str:
    """Keep the recorded unit name and append one unit suffix."""
    return name.removesuffix("-unit") + "-unit"


def source_owner(source: Path) -> str:
    """Use the standing checkout owner, copy owner or staff owner."""
    if (source / GATE_RECORD).is_file():
        return checked_owner(read_record(source).owner)
    record = scratch.task_of(source)
    if isinstance(record, copy_record.Registered):
        owner = record.fields.get("owner_unit")
        if isinstance(owner, str):
            return checked_owner(owner)
    from scripts.production import staff
    from scripts.agents.remote import local_machine
    try:
        member = staff.local_snapshot().by_worktree(local_machine(), source.resolve())
        if member is not None:
            return str(member.id)
    except staff.StaffUnreadable:
        pass
    return fallback_owner("verify")


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


def caller_owner() -> str:
    """Read the calling session's owner from the local staff record."""
    from scripts.production import staff

    try:
        record = staff.local_snapshot()
    except staff.StaffUnreadable as error:
        raise CopyError(f"calling session has no readable staff row: {error}".replace("\n", " ")) from error
    row = record.owner_of(staff.caller_identity())
    if row is None:
        raise CopyError("calling session has no staff row or unit owner")
    return str(row.id)


def origin_url(source: Path) -> str | None:
    """Read the origin URL when the repository has one."""
    if "origin" in git(source, "remote").decode().splitlines():
        return git(source, "remote", "get-url", "origin").decode().strip()
    return None


def main_checkout(source: Path) -> Path:
    """Read the main checkout, including repositories with separate Git metadata."""
    common, current = git(source, "rev-parse", "--path-format=absolute",
                          "--git-common-dir", "--git-dir").decode().splitlines()
    if common == current and git(source, "rev-parse", "--is-bare-repository").strip() == b"false":
        return Path(git(source, "rev-parse", "--show-toplevel").decode().strip())
    first = git(source, "worktree", "list", "--porcelain").decode().splitlines()[0]
    checkout = Path(first.removeprefix("worktree "))
    configured = git(source, "config", "--default", str(checkout), "--get", "core.worktree").decode().strip()
    return (Path(common) / configured).resolve()


def repository_name(source: Path) -> str:
    """Follow local origins to the repository's upstream identity."""
    seen: set[Path] = set()
    while True:
        origin = origin_url(source)
        if (origin is not None and not origin.startswith("file://")
                and not Path(resolved_remote_url(source, origin)).is_absolute()):
            return origin.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
        checkout = main_checkout(source)
        if checkout in seen:
            raise CopyError(f"{source}: local origins form a cycle")
        seen.add(checkout)
        if origin is None:
            return checkout.name
        source = (Path(unquote(urlsplit(origin).path)) if origin.startswith("file://")
                  else Path(resolved_remote_url(checkout, origin)))


def checked_owner(owner: str) -> str:
    if not OWNER.fullmatch(owner):
        raise CopyError(f"{owner!r}: owner must match [a-z0-9][a-z0-9_-]*")
    return owner


def tree_paths(owner: str, repo: str, pool: CopiesConfiguration) -> tuple[Path, ...]:
    """Find owned records before choosing unused readable folder names."""
    parent = pool.root / repo
    selected: list[Path] = []
    identity = copy_owner(owner)
    if parent.exists():
        for tree in sorted(parent.iterdir()):
            if tree.is_symlink() or not tree.is_dir() or tree.name.endswith(".removing"):
                continue
            try:
                record = read_record(tree)
            except CopyError:
                continue
            if copy_owner(record.owner) == identity:
                selected.append(tree)
    name = owner_name(identity)
    suffix = 1
    while len(selected) < len(GATE_SUFFIXES):
        tail = "" if suffix == 1 else f"-{suffix}"
        candidate = parent / f"{name}-gate{tail}"
        if not (candidate / GATE_RECORD).exists() and not removing_path(candidate).exists():
            selected.append(candidate)
        suffix += 1
    return tuple(selected)


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
        times: dict[str, TrackedFileTime] = {}
        stored = fields.get("tracked_times", {})
        if isinstance(stored, dict):
            for name, value in cast(dict[str, object], stored).items():
                if isinstance(value, list) and len(cast(list[object], value)) == 2:
                    blob, mtime = cast(list[object], value)
                    if isinstance(blob, str) and isinstance(mtime, int):
                        times[name] = TrackedFileTime(blob, mtime)
        return GateRecord(owner, repo, source, commit, float(moment), pid, times)
    except (OSError, ValueError) as error:
        raise CopyError(f"{tree / GATE_RECORD}: unreadable gate record: {error}") from error


def write_record(tree: Path, owner: str, repo: str, source: Path, commit: str, *,
                 tracked_times: Mapping[str, TrackedFileTime] = NO_TRACKED_TIMES) -> None:
    fields = {"owner": owner, "repo": repo, "source": str(source), "commit": commit,
              "last_use": time.time(), "holder_pid": os.getpid(),
              "tracked_times": {name: [item.blob, item.mtime_ns] for name, item in tracked_times.items()}}
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
        candidates = tree_paths(owner, repo, pool)
        for tree in candidates:
            if removing_path(tree).exists():
                continue
            descriptors: list[int] = []
            with claim_tree(tree, create=not tree.exists(), inherited_fds=descriptors) as state:
                if state is ClaimState.ACQUIRED:
                    if (tree / GATE_RECORD).exists():
                        if copy_owner(read_record(tree).owner) != copy_owner(owner):
                            continue
                    else:
                        write_record(tree, owner, repo, Path(), "")
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


def _scratch_gate_word(owner: str) -> str:
    """Make a readable gate word with scratch's permitted characters."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", f"{owner_name(copy_owner(owner))}-gate").lstrip("-_")


if TYPE_CHECKING:
    GateDirectoryBase = tempfile.TemporaryDirectory[str]
else:
    GateDirectoryBase = tempfile.TemporaryDirectory


class GateCheckoutDirectory(GateDirectoryBase):
    """Remove a private gate folder and report a removal failure separately."""

    @override
    def cleanup(self) -> None:
        caller_error = sys.exception()
        try:
            super().cleanup()
        except OSError as error:
            raise PostUseError(f"{self.name}: fresh copy removal failed: {error}",
                               caller_error=caller_error) from error


@contextmanager
def fresh_tree(owner: str) -> Generator[GateLease]:
    """Hold one private checkout under TMPDIR for the gate's lifetime."""
    with GateCheckoutDirectory(prefix=_scratch_gate_word(owner) + "-") as directory:
        path = Path(directory)
        descriptors: list[int] = []
        with claim_tree(path, create=False, inherited_fds=descriptors) as state:
            if state is not ClaimState.ACQUIRED:
                raise CopyError(f"{path}: task folder is busy")
            checkout = path / "checkout"
            checkout.mkdir()
            yield GateLease(checkout, tuple(descriptors))


def keeps_build_cache(source: Path, base: str) -> bool:
    """Use a standing tree when the selected commit has a root Cargo manifest."""
    try:
        _ = git(source, "cat-file", "-e", f"{base}:Cargo.toml")
    except CopyError:
        return False
    return True


def initialize(tree: Path, source: Path, owner: str, repo: str, base: str) -> None:
    remove_dead_temporary_files(tree / GATE_RECORD)
    replace_git = False
    if (tree / GATE_RECORD).exists():
        previous = read_record(tree)
        if copy_owner(previous.owner) != copy_owner(owner) or previous.repo != repo:
            raise CopyError(f"{tree}: gate record belongs to another owner or repository")
        replace_git = previous.source != str(source)
    elif any(child.name != scratch.BUSY for child in tree.iterdir()):
        raise CopyError(f"{tree}: refusing an unregistered existing directory")
    if (tree / ".git").is_dir() and not replace_git:
        try:
            _ = git(tree, "show-ref", "--head")
        except CopyError:
            replace_git = True
    write_record(tree, owner, repo, source, base)
    if replace_git and (tree / ".git").is_dir() and not (tree / ".git").is_symlink():
        shutil.rmtree(tree / ".git")
    initialize_git(tree, source, follow_alternates=True)


def source_origin(source: Path) -> tuple[str, str]:
    """Return the upstream URL and the source refs that represent it."""
    origin = origin_url(source)
    if origin is not None:
        return origin, "refs/remotes/origin"
    return str(source), "refs/heads"


def resolved_remote_url(repo: Path, url: str) -> str:
    """Resolve a local remote path from its repository."""
    if re.match(r"^[^/]+:", url):
        return url
    return str((repo / Path(url).expanduser()).resolve())


def refresh_remotes(tree: Path, source: Path) -> None:
    """Keep upstream refs and saved local branches in separate remotes."""
    url, refs = source_origin(source)
    settings: dict[str, list[str]] = {}
    for setting in git(tree, "config", "--local", "--null", "--list").decode().split("\0"):
        if setting:
            key, _, value = setting.partition("\n")
            settings.setdefault(key, []).append(value)

    def references(repo: Path, *prefixes: str) -> dict[str, str]:
        listing = git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(symref)", *prefixes)
        return dict(line.split(" ", 1) for line in listing.decode().splitlines())

    source_references = references(source, refs, "refs/heads")
    current_refs = references(tree, "refs/remotes/origin", "refs/remotes/source")
    mappings: list[str] = []
    for remote, remote_url, source_refs in (("origin", url, refs), ("source", str(source), "refs/heads")):
        mapping = f"+{source_refs}/*:refs/remotes/{remote}/*"
        for key, value in ((f"remote.{remote}.url", remote_url),
                           (f"remote.{remote}.fetch", f"+refs/heads/*:refs/remotes/{remote}/*")):
            if settings.get(key) != [value]:
                _ = git(tree, "config", "--replace-all", key, value)
        prefix = f"refs/remotes/{remote}/"
        expected = {name.removeprefix(source_refs + "/"): value.split()[0]
                    for name, value in source_references.items() if name.startswith(source_refs + "/")}
        current = {name.removeprefix(prefix): value.strip()
                   for name, value in current_refs.items() if name.startswith(prefix)}
        if current != expected:
            # Pruning keeps symbolic HEAD refs. Remove them before copying the source's refs.
            _ = git(tree, "update-ref", "--no-deref", "-d", prefix + "HEAD")
            mappings.append(mapping)
    if mappings:
        _ = git(tree, "fetch", "-q", "--prune", "--", str(source), *mappings)


def final_object_store(store: Path) -> Path:
    """Follow a single alternate path to the last available object store."""
    store = store.resolve()
    seen = {store}
    while (store / "info/alternates").is_file():
        paths = (store / "info/alternates").read_text(encoding="utf-8").splitlines()
        if len(paths) != 1:
            break
        alternate = (store / paths[0]).resolve()
        if alternate in seen or not alternate.is_dir():
            break
        seen.add(alternate)
        store = alternate
    return store


def initialize_git(tree: Path, source: Path, *, follow_alternates: bool = False) -> None:
    """Create private Git metadata while the caller holds the busy mark."""
    if not (tree / ".git").exists():
        # clone needs an empty destination; the tree already holds scratch's busy mark.
        # Stage only Git metadata, so first creation never has an unlocked checkout.
        clone_source = source
        clone_mode = "--shared"
        if source in ACTIVE_GATES.get():
            clone_mode = "--no-local"
        if follow_alternates:
            original = Path(git(source, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()).resolve()
            store = final_object_store(original)
            clone_source = source if store == original else store.parent
            if store.parent.name == ".git":
                checkout = store.parent.parent
                record_path = copy_record.record_path(checkout)
                record = scratch.task_of(record_path.parent if record_path is not None else checkout)
                if isinstance(record, copy_record.Registered):
                    clone_mode = "--no-local"
        with tempfile.TemporaryDirectory(dir=tree, prefix=CLONE_STAGE + "-") as directory:
            stage = Path(directory)
            _ = git(source, "clone", clone_mode, "--no-checkout", "--quiet", "--", str(clone_source), str(stage))
            _ = (stage / ".git").rename(tree / ".git")
    if (tree / ".git").is_symlink() or not (tree / ".git").is_dir():
        raise CopyError(f"{tree}: standing checkout must own its .git directory")
    actual = git(tree, "rev-parse", "--absolute-git-dir").decode().strip()
    if Path(actual).resolve() != (tree / ".git").resolve():
        raise CopyError(f"{tree}: Git directory is {actual}, expected {tree / '.git'}")
    (tree / ".git/index.lock").unlink(missing_ok=True)
    index = tree / ".git/index"
    try:
        _ = git(tree, "ls-files", "--stage")
    except CopyError:
        index.unlink(missing_ok=True)
    refresh_remotes(tree, source)
    exclude = tree / ".git/info/exclude"
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    entries = [entry for entry in (f"/{GATE_RECORD}", f"/{scratch.BUSY}", "/target")
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

            if accounts.source_has_claude_trust(source):
                _ = scratch.update_record(path, lambda record: {
                    **(record.fields if isinstance(record, copy_record.Registered) else {}),
                    "claude_trust_folder": str(checkout.resolve()),
                }, scratch_settings=scratch_settings)
                accounts.set_claude_folder_trust(checkout, trusted=True)
            source_commit = git(checkout, "rev-parse", "HEAD").decode().strip()
            _ = scratch.update_record(path, lambda record: {
                **(record.fields if isinstance(record, copy_record.Registered) else {}),
                "source_commit": source_commit,
            }, scratch_settings=scratch_settings)
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
            policy: SourcePolicy, merge: MergePolicy) -> None:
    """Convert only our own preparation errors, before entering caller code."""
    try:
        initialize(tree, source, owner, name, base)
        _ = git(tree, "fetch", "-q", "--", str(source), base)
        # Restore changed paths before switching trees. Keep unchanged file times.
        _ = git(tree, "read-tree", "--reset", "HEAD")
        changed = git(tree, "diff", "--name-only", "-z")
        if changed:
            _ = git(tree, "checkout", "-q", "--force", "HEAD",
                    "--pathspec-from-file=-", "--pathspec-file-nul", data=changed)
        _ = git(tree, "clean", "-ffdq")
        _ = git(tree, "checkout", "-q", "--detach", base)
        _ = git(tree, "clean", "-ffdq")
        target = tree / "target"
        if target.is_symlink():
            target.unlink()
        mark_target(tree)
        if isinstance(policy, WorktreeSource):
            apply_changes(tree, Path(policy.worktree).expanduser().resolve(), base)
        if isinstance(merge, MergeRevision):
            merge_changes(tree, source, merge.revision)
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
    identity = repository_name(source)
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
                        seed_source="", borrows_from=common, repository_identity=identity)
                    if manifest_path:
                        fields["crate_run"] = manifest_path
                    _ = scratch.update_record(tree, lambda _: scratch.fields_for_task(fields, task))
                    scratch.exclude_copy_metadata(tree)
                else:
                    scratch.register_copy(tree, task=task, owner_unit=owner, source_path=str(source),
                                          source_commit=base, copy_commit=copy_commit, target_mode="none",
                                          seed_source="", borrows_from=common, crate_run=manifest_path,
                                          repository_identity=identity)

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
def standing(owner: str, repo: str | Path, policy: SourcePolicy, *,
             merge: MergePolicy = NO_MERGE) -> Generator[Path]:
    """Hold a gate checkout through preparation and the caller's use."""
    owner = checked_owner(owner)
    try:
        source = Path(repo).expanduser().resolve()
        name = repository_name(source)
        base = source_commit(source, policy)
    except OSError as error:
        raise CopyError(str(error)) from error
    if (source in ACTIVE_GATES.get() and copy_owner(read_record(source).owner) == copy_owner(owner)
            and commit_at(source, "HEAD") == base and isinstance(policy, CommittedSource) and isinstance(merge, NoMerge)):
        yield source
        return
    keep_cache = keeps_build_cache(source, base)
    with ExitStack() as lifetime:
        cache_lease = lifetime.enter_context(available_tree(owner, name, load_config())) if keep_cache else NoGateCache()
        lease = lifetime.enter_context(fresh_tree(owner))
        tree = lease.tree
        previous = read_record(cache_lease.tree).tracked_times if isinstance(cache_lease, GateLease) else {}
        if isinstance(cache_lease, GateLease):
            write_record(cache_lease.tree, owner, name, source, base, tracked_times=previous)
        descriptors = cache_lease.descriptors if isinstance(cache_lease, GateLease) else ()
        token = COMMAND_LOCKS.set((*descriptors, *lease.descriptors))
        active = ACTIVE_GATES.set((*ACTIVE_GATES.get(), tree))
        try:
            prepare(tree, source, owner, name, base, policy, merge)
            write_text_atomic(tree.parent / copy_record.TEMPORARY_GATE_RECORD, json.dumps({
                "task": f"gate:{owner}", "source_path": str(source), "source_commit": base,
            }) + "\n")
            if isinstance(cache_lease, GateLease):
                cache = cache_lease.tree
                mark_target(cache)
                shutil.rmtree(tree / "target")
                (tree / "target").symlink_to(cache / "target", target_is_directory=True)
                path_files = manifest_path_files(tree)
                for path, current in tracked_file_times(tree).items():
                    saved = previous.get(path)
                    if saved is not None and saved.blob == current.blob and path not in path_files:
                        file = tree / path
                        os.utime(file, ns=(file.lstat().st_atime_ns, saved.mtime_ns), follow_symlinks=False)
            try:
                yield tree
            finally:
                if isinstance(cache_lease, GateLease):
                    caller_error = sys.exception()
                    try:
                        refresh_remotes(tree, source)
                        mark_target(cache_lease.tree)
                        commit = commit_at(tree, "HEAD")
                        write_record(tree, owner, name, source, commit)
                        write_record(cache_lease.tree, owner, name, source, commit, tracked_times=tracked_file_times(tree))
                    except (OSError, scratch.ScratchError, CopyError) as error:
                        raise PostUseError(f"{tree}: post-use refresh failed: {error}",
                                           caller_error=caller_error) from error
        finally:
            ACTIVE_GATES.reset(active)
            COMMAND_LOCKS.reset(token)


def manifest_path_files(tree: Path) -> set[str]:
    """Find tracked files that name Cargo's manifest path variable."""
    timeout = load_config().git_timeout_seconds
    try:
        result = subprocess.run(["git", "-C", str(tree), "grep", "--no-color", "-l", "-z", "-F", "--",
                                 "CARGO_MANIFEST_DIR"], capture_output=True, timeout=timeout,
                                pass_fds=COMMAND_LOCKS.get())
    except subprocess.TimeoutExpired as error:
        raise CopyError(f"git grep in {tree}: timed out after {timeout:g} seconds") from error
    if result.returncode not in (0, 1):
        raise CopyError(f"git grep in {tree}: {result.stderr.decode(errors='replace').strip()}")
    return {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}


def tracked_file_times(tree: Path) -> dict[str, TrackedFileTime]:
    """Read tracked blob identities and their current file times."""
    times: dict[str, TrackedFileTime] = {}
    for entry in git(tree, "ls-tree", "-rz", "HEAD").split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        _, kind, blob = metadata.split()
        if kind != b"blob":
            continue
        name = os.fsdecode(path)
        file = tree / name
        if file.exists() or file.is_symlink():
            times[name] = TrackedFileTime(blob.decode("ascii"), file.lstat().st_mtime_ns)
    return times


def list_trees() -> None:
    root = gates_root()
    staff_names = ListStaffNames()
    print("number\towner\trepository\thost\tpath\tsession\tcommit\tstate\tholder_pid\ttarget")
    number = 0
    host = socket.gethostname()
    directories = sorted(root.iterdir()) if root.exists() else []
    for directory in directories:
        if directory.name.startswith(".") or directory.is_symlink() or not directory.is_dir():
            continue
        for tree in sorted(directory.iterdir()):
            if tree.is_symlink() or not tree.is_dir():
                continue
            try:
                record = read_record(tree)
            except CopyError as error:
                number += 1
                print(f"{number}\tunavailable\t{tree.parent.name}\t{host}\t{tree}\t-\t-\tunreadable\t-\t-")
                print(f"{tree}: {error}", file=sys.stderr)
                continue
            with scratch.busy_locks(tree, create=False) as idle:
                state = "idle" if idle else "busy"
            target = tree / "target"
            disk = "0"
            if target.exists():
                size = subprocess.run(["du", "-sh", str(target)], capture_output=True, text=True)
                words = size.stdout.split()
                disk = words[0] if size.returncode == 0 and words else "unreadable"
            number += 1
            owner = _owner_display_text(list_owner_name(copy_owner(record.owner), staff_names))
            print(f"{number}\t{owner}\t{record.repo}\t{host}\t{tree}\t-\t{record.commit}\t{state}\t{record.holder_pid}\t{disk}")
    mirrors = sorted(remote_mirrors(), key=lambda mirror: (mirror.host, mirror.path))
    for mirror, lease in list_remote_leases(mirrors, staff_names):
        if mirror.host == "localhost" and not floor_claims.mirror_local_path(mirror.path).exists():
            unregister_remote(mirror.host, mirror.path)
            continue
        number += 1
        owner = _owner_display_text(list_owner_name(copy_owner(mirror.owner), staff_names))
        if isinstance(lease, HeldMirrorLease):
            state = "held"
            pid = str(lease.pid)
            if isinstance(lease.holder, CurrentHolderName):
                session = lease.holder.name
            else:
                session = "not recorded" if isinstance(lease.holder, HolderNotRecorded) else "unavailable"
        else:
            state = "idle" if isinstance(lease, IdleMirrorLease) else "unavailable"
            pid = "-"
            session = "-"
        local_host = host if mirror.host == "localhost" else mirror.host
        print(f"{number}\t{owner}\t{mirror.repository.name}\t{local_host}\t{mirror.path}\t{session}\t-\t{state}\t{pid}\t-")


def removing_path(tree: Path) -> Path:
    return tree.with_name(tree.name + ".removing")


def workspace_repository(record: copy_record.Registered) -> str:
    """Read the repository identity saved when the workspace was registered."""
    identity = record.fields["repository_identity"]
    if not isinstance(identity, str):
        raise CopyError("workspace repository_identity must be a string")
    return identity


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
        if copy_owner(str(record.fields.get("owner_unit", ""))) != copy_owner(owner):
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


@dataclass(frozen=True)
class OwnedTrees:
    """The standing trees and workspaces selected for one owner."""

    standing: tuple[Path, ...]
    workspaces: tuple[tuple[Path, copy_record.Registered], ...]
    failures: tuple[str, ...]


def owned_trees(owner: str, repo: RepositoryScope = ALL_REPOSITORIES) -> OwnedTrees:
    """Read the owner's trees without acquiring locks or changing files."""
    pool = load_config()
    scope = repository_scope(repo)
    directories = ([pool.root / scope.name] if isinstance(scope, SelectedRepository) else
                   sorted(path for path in pool.root.iterdir() if path.is_dir() and not path.is_symlink())
                   if pool.root.exists() else [])
    standing_list: list[Path] = []
    for directory in directories:
        if not directory.exists():
            continue
        for tree in sorted(directory.iterdir()):
            if tree.is_symlink() or not tree.is_dir():
                continue
            if tree.name.endswith(".removing"):
                standing_list.append(tree)
                continue
            try:
                record = read_record(tree)
            except CopyError:
                continue
            if copy_owner(record.owner) == copy_owner(owner):
                standing_list.append(tree)
    standing = tuple(standing_list)
    try:
        workspaces = tuple(owned_workspaces(owner, scope))
    except CopyError as error:
        return OwnedTrees(standing, (), (str(error),))
    return OwnedTrees(standing, workspaces, ())


def _gate_borrowers(tree: Path) -> Generator[Path]:
    """Yield the first registered copy that borrows the gate's objects."""
    gate_objects = (tree / ".git/objects").resolve()
    for child in scratch.registered_copy_paths():
        if not child.exists():
            continue
        objects = child / ".git/objects"
        try:
            lines = (objects / "info/alternates").read_text().splitlines()
        except FileNotFoundError:
            continue
        if any(line and (objects / line).resolve() == gate_objects for line in lines):
            yield child
            return


class RemoteMirrorKept(CopyError):
    """A remote mirror needs another cleanup attempt before retirement."""


def remove_trees(owner: str, repo: RepositoryScope = ALL_REPOSITORIES) -> None:
    """Remove an owner's idle trees within the selected repository scope."""
    scope = repository_scope(repo)
    selected = owned_trees(owner, scope)
    failures = list(selected.failures)
    failure_type: type[CopyError] = CopyError
    try:
        owned_mirrors = owned_remote_mirrors(owner, scope)
    except CopyError as error:
        failures.append(str(error))
        failure_type = RemoteMirrorKept
        owned_mirrors = []
    with ExitStack() as locks:
        mirrors: list[RemoteMirror] = []
        for mirror in owned_mirrors:
            try:
                locks.enter_context(remote_lease(mirror))
                mirrors.append(mirror)
            except (CopyError, OSError) as error:
                failures.append(str(error))
                failure_type = RemoteMirrorKept
        present: list[Path] = []
        for tree in selected.standing:
            try:
                state = locks.enter_context(claim_tree(tree, create=False))
                if state is ClaimState.REPLACED:
                    continue
                if state is not ClaimState.ACQUIRED:
                    raise BusyCopy(tree)
                if not tree.name.endswith(".removing") and (tree / GATE_RECORD).exists():
                    record = read_record(tree)
                    if copy_owner(record.owner) != copy_owner(owner) or record.repo != tree.parent.name:
                        raise CopyError(f"{tree}: gate record belongs to another owner or repository")
                present.append(tree)
            except (CopyError, OSError, scratch.ScratchError) as error:
                failures.append(str(error))
        unreachable_hosts: set[str] = set()
        for mirror in mirrors:
            try:
                if mirror.host not in unreachable_hosts:
                    try:
                        remove_remote_mirror(mirror)
                        continue
                    except RemoteHostUnreachable:
                        unreachable_hosts.add(mirror.host)
                defer_remote_removal(mirror)
            except (CopyError, OSError, subprocess.SubprocessError) as error:
                failures.append(str(error))
                failure_type = RemoteMirrorKept
        for tree, record in selected.workspaces:
            try:
                with workspace_removal_guard(tree, record) as validate:
                    if not scratch.end_selected_copy(tree, record, time.time(), validate=validate):
                        with scratch.judgment_lock(tree) as idle:
                            if not idle:
                                raise BusyCopy(tree)
                        reason = scratch.unsaved_work(tree) or str(scratch.lifetime(tree))
                        raise CopyError(f"{tree}: kept: {reason}")
                _ = scratch.release(tree)
                print(f"removed {tree}")
            except (CopyError, OSError, scratch.ScratchError) as error:
                failures.append(str(error))
        for tree in present:
            try:
                for child in _gate_borrowers(tree):
                    raise CopyError(f"{tree}: kept: borrowed: {child} still uses its git objects")
                tombstone = tree if tree.name.endswith(".removing") else removing_path(tree)
                if tombstone != tree:
                    _ = tree.rename(tombstone)
                shutil.rmtree(tombstone)
                print(f"removed {tree}")
            except (CopyError, OSError, scratch.ScratchError) as error:
                failures.append(str(error))
    if failures:
        raise failure_type("\n".join(failures))


class RemoteRemoval(Enum):
    KEEP = "keep"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class RemoteMirror:
    """An owner's registered repository copy on a remote host."""

    owner: str
    repository: SelectedRepository
    host: str
    path: str
    session: str
    removal: RemoteRemoval = RemoteRemoval.KEEP


@dataclass(frozen=True)
class CurrentHolderName:
    name: str


@dataclass(frozen=True)
class HolderNameUnavailable:
    """The recorded live holder has no available current staff name."""


@dataclass(frozen=True)
class HolderNotRecorded:
    """The record does not identify the process that holds the current lease."""


MirrorHolderName = CurrentHolderName | HolderNameUnavailable | HolderNotRecorded


@dataclass(frozen=True)
class HeldMirrorLease:
    pid: int
    holder: MirrorHolderName


@dataclass(frozen=True)
class IdleMirrorLease:
    """The readable lock table has no lease for this mirror."""


@dataclass(frozen=True)
class UnavailableMirrorLease:
    """The list cannot read the mirror's lease evidence."""


MirrorLeaseState = HeldMirrorLease | IdleMirrorLease | UnavailableMirrorLease


def remote_registry() -> Path:
    return gates_root() / ".remote-mirrors"


def remote_key(host: str, path: str) -> str:
    return hashlib.sha256(f"{host}\0{path}".encode()).hexdigest()


def remote_record_path(mirror: RemoteMirror) -> Path:
    return remote_registry() / (remote_key(mirror.host, mirror.path) + ".json")


def remote_lease_path(mirror: RemoteMirror) -> Path:
    return remote_registry() / (remote_key(mirror.host, mirror.path) + ".lease")


def _list_holder_name(mirror: RemoteMirror, pid: int, staff_names: ListStaffNames) -> MirrorHolderName:
    """Read the recorded pid and session together before resolving the live holder's name."""
    try:
        raw = cast(object, json.loads(remote_record_path(mirror).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return HolderNotRecorded()
    if not isinstance(raw, dict):
        return HolderNotRecorded()
    fields = cast(dict[str, object], raw)
    session = fields.get("session")
    if fields.get("holder_pid") != pid or not isinstance(session, str):
        return HolderNotRecorded()
    names = staff_names.read()
    if isinstance(names, UnavailableStaffNames):
        return HolderNameUnavailable()
    row = names.snapshot.by_current_session(session)
    return CurrentHolderName(row.name) if row is not None else HolderNameUnavailable()


def list_remote_leases(mirrors: Sequence[RemoteMirror], staff_names: ListStaffNames) -> list[tuple[RemoteMirror, MirrorLeaseState]]:
    """Read the lock table once and preserve each mirror's held, idle, or unavailable state."""
    if not mirrors:
        return []
    try:
        devices = file_lock.held_flock_keys()
    except OSError:
        return [(mirror, UnavailableMirrorLease()) for mirror in mirrors]
    leases: list[tuple[RemoteMirror, MirrorLeaseState]] = []
    for mirror in mirrors:
        try:
            device = file_lock.device_key(remote_lease_path(mirror))
        except FileNotFoundError:
            state: MirrorLeaseState = IdleMirrorLease()
        except OSError:
            state = UnavailableMirrorLease()
        else:
            pid = devices.get(device)
            state = IdleMirrorLease() if pid is None else HeldMirrorLease(pid, _list_holder_name(mirror, pid, staff_names))
        leases.append((mirror, state))
    return leases


def held_remote_mirrors() -> tuple[floor_claims.HeldMirror, ...]:
    """Read the kernel lock table once for all registered mirrors."""
    try:
        devices = file_lock.held_flock_keys(file_lock.PROC_LOCKS)
    except OSError:
        return ()
    held: list[floor_claims.HeldMirror] = []
    for mirror in remote_mirrors():
        try:
            device = file_lock.device_key(remote_lease_path(mirror))
        except OSError:
            continue
        if device in devices:
            held.append(floor_claims.HeldMirror(floor_claims.MirrorIdentity(mirror.host, mirror.path), mirror.session))
    return tuple(held)


def register_remote(mirror: RemoteMirror) -> None:
    """Publish ownership while the caller holds the mirror lease."""
    _ = checked_owner(mirror.owner)
    _ = repository_scope(mirror.repository)
    remote_registry().mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(remote_registry() / "records.lock"):
        write_text_atomic(remote_record_path(mirror), json.dumps({
            "kind": "remote-mirror", "owner": mirror.owner, "repo": mirror.repository.name,
            "host": mirror.host, "path": mirror.path, "session": mirror.session,
            "holder_pid": os.getpid(), "last_use": time.time(),
        }) + "\n")
    remove_deferred_mirrors(mirror.host)


def defer_remote_removal(mirror: RemoteMirror) -> None:
    """Keep the removal request until the next use of the host."""
    with file_lock.exclusive(remote_registry() / "records.lock"):
        path = remote_record_path(mirror)
        fields = cast(dict[str, object], json.loads(path.read_text()))
        fields["removal"] = RemoteRemoval.DEFERRED.value
        write_text_atomic(path, json.dumps(fields) + "\n")
    print(f"{mirror.host}:{mirror.path}: removal deferred; removal runs at the next use of the host")


def remove_deferred_mirrors(host: str) -> None:
    """Retry local removal requests after a successful use of the host."""
    for path in sorted(remote_registry().glob("*.json")):
        try:
            mirror = read_remote_mirror(path)
            if mirror.host != host or mirror.removal is not RemoteRemoval.DEFERRED:
                continue
            with remote_lease(mirror):
                current = read_remote_mirror(path)
                if current.removal is RemoteRemoval.DEFERRED:
                    remove_remote_mirror(current)
        except (CopyError, OSError, subprocess.SubprocessError) as error:
            print(f"anomaly: deferred mirror removal: {' '.join(str(error).split())}", file=sys.stderr)


def unregister_remote(host: str, path: str) -> None:
    """Remove the registry record after confirmed mirror cleanup."""
    with file_lock.exclusive(remote_registry() / "records.lock"):
        key = remote_key(host, path)
        (remote_registry() / (key + ".json")).unlink(missing_ok=True)


def read_remote_mirror(path: Path) -> RemoteMirror:
    """Decode one registry record without scanning other records."""
    try:
        raw = cast(object, json.loads(path.read_text()))
        if not isinstance(raw, dict):
            raise ValueError("expected a remote mirror record")
        fields = cast(dict[str, object], raw)
        values = [fields.get(key) for key in ("owner", "repo", "host", "path", "session")]
        if not all(isinstance(value, str) for value in values):
            raise ValueError("invalid remote mirror record")
        unit, repo, host, remote, session = cast(list[str], values)
        return RemoteMirror(unit, SelectedRepository(repo), host, remote, session,
                            RemoteRemoval(fields.get("removal", "keep")))
    except (OSError, ValueError) as error:
        raise CopyError(f"{path}: repair remote mirror record: {error}") from error


def remote_mirrors(scope: RepositoryScope = ALL_REPOSITORIES) -> list[RemoteMirror]:
    found: list[RemoteMirror] = []
    for path in sorted(remote_registry().glob("*.json")):
        mirror = read_remote_mirror(path)
        if isinstance(scope, AllRepositories) or scope.name == mirror.repository.name:
            found.append(mirror)
    return found


def owned_remote_mirrors(owner: str, scope: RepositoryScope = ALL_REPOSITORIES) -> list[RemoteMirror]:
    return [mirror for mirror in remote_mirrors(scope) if copy_owner(mirror.owner) == copy_owner(owner)] if owner else []


class BusyRemoteMirror(CopyError):
    """A live run holds this remote mirror."""


@contextmanager
def remote_lease(mirror: RemoteMirror) -> Generator[None]:
    """Take the mirror lease before admission, claims and record locks."""
    remote_registry().mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(remote_lease_path(mirror), wait=False) as state:
        if state is file_lock.LockResult.BUSY:
            record = remote_record_path(mirror)
            session = read_remote_mirror(record).session if record.exists() else mirror.session
            raise BusyRemoteMirror(f"{mirror.host}:{mirror.path}: busy; held by session {session}")
        yield


def remote_copies_command(action: str, mirror: RemoteMirror, *tail: str) -> str:
    """Build the remote command with one mirror identity."""
    return "~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py " + shlex.join([
        action, "--path", mirror.path, "--owner", mirror.owner, "--repository", mirror.repository.name,
        "--session", mirror.session, *tail,
    ])


def remote_rsync(source: Path, mirror: RemoteMirror, excludes: Path, options: list[str]) -> list[str]:
    """Refresh a leased mirror through the remote copy owner."""
    receiver = remote_copies_command("mirror-receive", mirror, "--", "rsync")
    # Keep the existing command order while metadata removal keeps the record last.
    exclusions = floor_claims.MIRROR_METADATA[1:] + floor_claims.MIRROR_METADATA[:1]
    return ["rsync", RSYNC_RECEIVER_WRITE_TIME_FLAGS, "--compress", "--delete", f"--rsync-path={receiver}",
            *(f"--exclude=/{name}" for name in exclusions),
            f"--exclude-from={excludes}", "-e", shlex.join(["ssh", *options]),
            f"{source}/", f"{mirror.host}:{shlex.quote(mirror.path)}/"]


def remote_use_command(mirror: RemoteMirror, command: str) -> str:
    return remote_copies_command("mirror-use", mirror, "--", "sh", "-c", command)


def inherited_mirror_descriptors() -> tuple[int, ...]:
    """Carry the mirror lease through a remote Python entry point."""
    return tuple(int(value) for value in os.environ.get("COPIES_MIRROR_LEASE_FDS", "").split(",") if value)


def mirror_probe_setup(path: str) -> str:
    """Create a marked probe copy through the registered remote kind."""
    program = "\n".join((
        "import sys",
        "from scripts.copies import copies",
        "copies.probe_remote_folder(sys.argv[1])",
    ))
    relative = path.removeprefix("~/")
    mark = shlex.quote(str(Path(relative) / floor_claims.MIRROR_MARK))
    shell_mark = mark if Path(relative).is_absolute() else '"$HOME"/' + mark
    return ("test -f " + shell_mark + ' || "$HOME"/.claude/scripts/lib/py -c ' + shlex.quote(program) + " " +
            shlex.quote(path) + " || exit 1")


def mark_mirror(tree: Path, owner: str, repo: str, session: str, **extra: object) -> None:
    """Publish mirror metadata before removal authority can disappear."""
    write_text_atomic(tree / floor_claims.MIRROR_RECORD, json.dumps({
        "kind": "remote-mirror", "owner": owner, "repo": repo, "session": session, **extra,
    }) + "\n")
    write_text_atomic(tree / floor_claims.MIRROR_SESSION, session + "\n")
    write_text_atomic(tree / floor_claims.MIRROR_MARK, "registered remote mirror\n")


def probe_remote_folder(path: str) -> None:
    tree = floor_claims.mirror_local_path(path)
    if tree.is_dir():
        return
    with local_mirror_lease(tree, "probe"):
        mark_mirror(tree, "probe", "probe", "probe")


@dataclass(frozen=True)
class MirrorProcessScan:
    """Share process paths and unreadable lease groups across mirror checks."""

    uses: tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable
    unreadable_groups: frozenset[int]


def scan_mirror_processes(mirrors: Sequence[Path]) -> MirrorProcessScan:
    from scripts.lint import tmp_sweep

    prefixes = tuple(str(tree.resolve()) for tree in mirrors)
    if not prefixes:
        return MirrorProcessScan(tmp_sweep.ProcessUsesKnown([]), frozenset())
    unreadable_groups: set[int] = set()
    if sys.platform == "linux":
        uses = tmp_sweep.process_paths("/proc", prefixes)
        if isinstance(uses, tmp_sweep.ProcessUsesKnown):
            for pid in uses.unreadable_cwd_pids:
                if pid == os.getpid():
                    continue
                try:
                    unreadable_groups.add(os.getpgid(pid))
                except ProcessLookupError:
                    continue
    else:
        uses = tmp_sweep.lsof_paths(prefixes)
    return MirrorProcessScan(uses, frozenset(unreadable_groups))


def mirror_has_process(tree: Path, processes: MirrorProcessScan) -> bool:
    """Keep a mirror with an active path, unreadable lease group, or unavailable scan."""
    from scripts.lint import tmp_sweep

    if not tree.exists():
        return False
    tree = tree.resolve()
    root = str(tree)
    descendant_prefix = os.path.join(root, "")
    if isinstance(processes.uses, tmp_sweep.ProcessUsesUnavailable):
        return True
    if any(pid != os.getpid() and how in (tmp_sweep.WORKS_IN, tmp_sweep.MAPS_FILE, tmp_sweep.WRITES_FILE)
           and (path == root or path.startswith(descendant_prefix))
           for path, pid, how in processes.uses.paths):
        return True
    try:
        group = floor_claims.read_mirror_record(tree / floor_claims.MIRROR_RECORD).get("process_group")
    except (OSError, ValueError):
        return False
    return isinstance(group, int) and group in processes.unreadable_groups


@contextmanager
def local_mirror_lease(tree: Path, session: str, *, create: bool = True) -> Generator[None]:
    """Lock the mirror before any claim or record operation."""
    if not create and not tree.parent.exists():
        yield
        return
    parent_lock = floor_claims.mirror_parent_lock_path(tree)
    parent_lock.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as leases:
        lease_files: list[TextIO] = []
        try:
            outside = leases.enter_context(parent_lock.open("a+"))
            lease_files.append(outside)
            fcntl.flock(outside.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(outside.fileno()).st_ino != parent_lock.stat().st_ino:
                raise BlockingIOError
            if create:
                tree.parent.mkdir(parents=True, exist_ok=True)
            lock = leases.enter_context(floor_claims.mirror_lock_path(tree).open("a+"))
            lease_files.append(lock)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = mirror_has_process(tree, scan_mirror_processes((tree,)))
        except (BlockingIOError, FileNotFoundError):
            busy = True
        if busy:
            try:
                held = (tree / floor_claims.MIRROR_SESSION).read_text().strip()
            except OSError:
                held = "unreadable"
            raise CopyError(f"{tree}: busy; held by session {held}")
        if create:
            tree.mkdir(parents=True, exist_ok=True)
            write_text_atomic(tree / floor_claims.MIRROR_SESSION, session + "\n")
        token = COMMAND_LOCKS.set((*COMMAND_LOCKS.get(), *(lease.fileno() for lease in lease_files)))
        try:
            yield
        finally:
            COMMAND_LOCKS.reset(token)
            if not tree.parent.exists():
                parent_lock.unlink(missing_ok=True)
            # Closing releases only our descriptor. Live children retain the flock.


@contextmanager
def mirror_lease_set(mirrors: set[Path]) -> Generator[frozenset[Path]]:
    """Hold idle mirror leases and identify mirrors held by live runs."""
    protected: set[Path] = set()
    with ExitStack() as leases:
        candidates: dict[Path, TextIO] = {}
        for mirror in sorted(mirrors):
            mirror.parent.mkdir(parents=True, exist_ok=True)
            lock = leases.enter_context(floor_claims.mirror_lock_path(mirror).open("a+"))
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                protected.add(mirror)
            else:
                candidates[mirror] = lock
        processes = scan_mirror_processes(tuple(candidates))
        for mirror, lock in candidates.items():
            if mirror_has_process(mirror, processes):
                lock.close()
                protected.add(mirror)
        yield frozenset(protected)


@dataclass(frozen=True)
class MirrorSweepLeases:
    """Separate acquired mirror leases from mirrors that another process holds."""

    held: frozenset[Path]
    busy: frozenset[Path]


@contextmanager
def mirror_sweep_leases(root: Path) -> Generator[MirrorSweepLeases]:
    """Take idle mirror leases before reading the floor claims."""
    mirrors = {mark.parent for pattern in (f"*/{floor_claims.MIRROR_MARK}", f"*/*/{floor_claims.MIRROR_MARK}")
               for mark in root.glob(pattern)}
    for item in remote_mirrors():
        if item.host == "localhost":
            checkout = floor_claims.mirror_local_path(item.path)
            if checkout.exists():
                mirrors.add(checkout)
            else:
                unregister_remote(item.host, item.path)
    authority_glob = floor_claims.removal_authority_path(Path("*")).name
    mirrors.update(path.parent / path.name[1:-len(floor_claims.MIRROR_REMOVAL_SUFFIX)]
                   for pattern in (authority_glob, "*/" + authority_glob)
                   for path in root.glob(pattern))
    with mirror_lease_set(mirrors) as protected:
        yield MirrorSweepLeases(frozenset(mirrors) - protected, protected)


@contextmanager
def target_mirror_leases(targets: Sequence[str]) -> Generator[frozenset[Path]]:
    """Keep remote copies safe during a Linux floor sweep."""
    mirrors: set[Path] = set()
    for target in targets:
        path = Path(target).resolve()
        for parent in (path, *path.parents):
            if (parent / floor_claims.MIRROR_MARK).is_file():
                mirrors.add(parent)
                break
    with mirror_lease_set(mirrors) as protected:
        yield protected


class RemoteHostUnreachable(CopyError):
    """The removal command could not connect to the mirror host."""


def remove_remote_mirror(mirror: RemoteMirror) -> None:
    command = remote_copies_command("mirror-remove", mirror, "--host", mirror.host)
    command += '; copies_status=$?; printf "\\nrc=%s\\n" "$copies_status"; exit "$copies_status"'
    try:
        answer = subprocess.run(["ssh", "-o", "BatchMode=yes", mirror.host, command],
                                capture_output=True, text=True, timeout=load_config().git_timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise RemoteHostUnreachable(f"{mirror.host}:{mirror.path}: {error}") from error
    if answer.returncode:
        failure = (RemoteHostUnreachable if answer.returncode == 255 and any(
            text in answer.stderr.lower() for text in (
                "ssh: connect to host ", "ssh: connect to address ", "ssh: could not resolve hostname ",
            )) else CopyError)
        raise failure(f"{mirror.host}:{mirror.path}: {failure_detail(answer)}")
    status = next((line for line in reversed(answer.stdout.splitlines()) if line.startswith("rc=")), "")
    if status != "rc=0":
        detail = status or "remote status is missing"
        raise CopyError(f"{mirror.host}:{mirror.path}: {detail}; {failure_detail(answer)}")
    unregister_remote(mirror.host, mirror.path)


def clear_mirror_content(tree: Path) -> None:
    """Remove the payload before the mirror metadata."""
    for child in tree.iterdir():
        if child.name in floor_claims.MIRROR_METADATA:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for name in floor_claims.MIRROR_METADATA:
        (tree / name).unlink(missing_ok=True)


@contextmanager
def mirror_metadata_locks(tree: Path) -> Generator[floor_claims.ClaimSnapshot]:
    """Read claims, then lock the record while the caller holds the mirror lease."""
    claims = floor_claims.read_claims(load_config().git_timeout_seconds)
    with file_lock.exclusive(floor_claims.record_lock_path(tree)):
        yield claims


def mirror_remove(args: CopyArguments) -> int:
    tree = floor_claims.mirror_local_path(args.path)
    authority = floor_claims.removal_authority_path(tree)
    with local_mirror_lease(tree, args.session, create=False):
        with mirror_metadata_locks(tree) as claims:
            if not tree.exists() and not authority.exists():
                return 0
            if tree.exists() and claims.holds(str(tree), tree.parent,
                    exemption=floor_claims.MirrorIdentity(args.host, args.path)):
                raise CopyError(f"{tree}: busy; held by session {args.session}")
            record = tree / floor_claims.MIRROR_RECORD
            raw = floor_claims.read_mirror_record(record if record.exists() else authority)
            if copy_owner(str(raw.get("owner", ""))) != copy_owner(args.owner) or raw.get("repo") != args.repository:
                raise CopyError(f"{tree}: remote mirror belongs to another owner or repository")
            if tree.exists():
                write_text_atomic(authority, json.dumps(raw) + "\n")
                task_record = copy_record.record_path(tree)
                if task_record is not None and task_record.parent != tree:
                    scratch.remove_marked(task_record.parent, scratch.remove)
                else:
                    clear_mirror_content(tree)
                    tree.rmdir()
            authority.unlink(missing_ok=True)
    return 0


def command_tail(command: list[str]) -> list[str]:
    """Remove the CLI separator before command execution."""
    return command[1:] if command[:1] == ["--"] else command


def receive_metadata(tree: Path, args: CopyArguments) -> None:
    """Finish pending removal and publish all metadata before deleting its authority."""
    authority = floor_claims.removal_authority_path(tree)
    record = tree / floor_claims.MIRROR_RECORD
    previous = floor_claims.read_mirror_record(record) if record.exists() else {}
    if authority.exists():
        pending = floor_claims.read_mirror_record(authority)
        # A different record identifies a complete replacement. Keep its content.
        if not record.exists() or previous == pending:
            clear_mirror_content(tree)
        if not previous:
            previous = pending
    mark_mirror(tree, cast(str, previous.get("owner", args.owner)),
                cast(str, previous.get("repo", args.repository)), args.session,
                last_use=time.time(), process_group=os.getpgrp())
    authority.unlink(missing_ok=True)


def mirror_run(args: CopyArguments) -> int:
    tree = floor_claims.mirror_local_path(args.path)
    with local_mirror_lease(tree, args.session):
        with mirror_metadata_locks(tree):
            if args.action == "mirror-receive":
                receive_metadata(tree, args)
            else:
                mark_mirror(tree, args.owner, args.repository, args.session,
                            last_use=time.time(), process_group=os.getpgrp())
        command = command_tail(args.command)
        if not command:
            raise CopyError("remote mirror requires a command")
        environment = {**os.environ, "COPIES_MIRROR_LEASE_FDS": ",".join(map(str, COMMAND_LOCKS.get()))}
        answer = subprocess.run(command, pass_fds=COMMAND_LOCKS.get(), env=environment)
        return answer.returncode


class CopyParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise CopyError(message)


class CopyArguments(argparse.Namespace):
    host: str = ""
    shell_lease: bool = False
    path: str = ""
    repository: str = ""
    session: str = ""
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
    for action in ("mirror-receive", "mirror-use", "mirror-remove"):
        remote = commands.add_parser(action)
        if action == "mirror-remove":
            _ = remote.add_argument("--host", default="")
        _ = remote.add_argument("--path", required=True)
        _ = remote.add_argument("--owner", required=True)
        _ = remote.add_argument("--repository", required=True)
        _ = remote.add_argument("--session", required=True)
        if action != "mirror-remove":
            _ = remote.add_argument("command", nargs=argparse.REMAINDER)
    for action in ("register-remote", "unregister-remote"):
        registry = commands.add_parser(action)
        _ = registry.add_argument("--host", required=True)
        _ = registry.add_argument("--path", required=True)
        if action == "register-remote":
            _ = registry.add_argument("--owner", required=True)
            _ = registry.add_argument("--repository", required=True)
            _ = registry.add_argument("--session", required=True)
            _ = registry.add_argument("--shell-lease", action="store_true")
    clone_command = commands.add_parser("clone", help="create a task-owned test clone")
    _ = clone_command.add_argument("source")
    ends = clone_command.add_mutually_exclusive_group()
    _ = ends.add_argument("--task", default="")
    _ = ends.add_argument("--until", default="")
    _ = clone_command.add_argument("--name", default="hana")
    _ = clone_command.add_argument("--owner", default="")
    _ = clone_command.add_argument("--repository", default="hana")
    _ = clone_command.add_argument("--session", default="")
    run = commands.add_parser("run", help="prepare a standing tree and run a command in it")
    hold = commands.add_parser("hold", help="hold a staged checkout until standard input closes")
    _ = hold.add_argument("--repo", required=True)
    remove = commands.add_parser("remove", help="remove the caller's idle standing trees")
    _ = run.add_argument("--repo", required=True)
    _ = remove.add_argument("--repo")
    _ = run.add_argument("--commit", required=True)
    _ = run.add_argument("--apply-uncommitted")
    _ = run.add_argument("--merge")
    _ = run.add_argument("command", nargs=argparse.REMAINDER)
    _ = commands.add_parser("list", help="list standing trees and their busy state")
    try:
        args = parser.parse_args(argv, namespace=CopyArguments())
        if args.action in ("register-remote", "unregister-remote"):
            remote_registry().mkdir(parents=True, exist_ok=True)
            if args.action == "unregister-remote":
                unregister_remote(args.host, args.path)
            else:
                mirror = RemoteMirror(args.owner, SelectedRepository(args.repository), args.host, args.path, args.session)
                if args.shell_lease:
                    print(remote_lease_path(mirror))
                else:
                    register_remote(mirror)
            return 0
        if args.action == "hold":
            source = Path(args.repo or "").expanduser().resolve()
            staged = StagedSource(staged_tree(source))
            with standing(source_owner(source), source, staged) as checkout:
                print(staged.tree)
                print(checkout, flush=True)
                _ = sys.stdin.read()
            return 0
        if args.action == "mirror-remove":
            return mirror_remove(args)
        if args.action in ("mirror-receive", "mirror-use"):
            return mirror_run(args)
        if args.action == "clone":
            task = scratch.parsed_until(args.until) if args.until else args.task
            checkout = clone(Path(args.source), task, args.name)
            if args.owner:
                with local_mirror_lease(checkout, args.session):
                    mark_mirror(checkout, checked_owner(args.owner), args.repository, args.session)
                    register_remote(RemoteMirror(args.owner, SelectedRepository(args.repository),
                                                "localhost", str(checkout), args.session))
            print(checkout)
            return 0
        if args.action == "run":
            command = command_tail(args.command)
            if not command:
                raise CopyError("run requires a command after --")
            policy: SourcePolicy = (WorktreeSource(args.commit, args.apply_uncommitted) if args.apply_uncommitted else
                                    CommittedSource(args.commit))
            merge: MergePolicy = MergeRevision(args.merge) if args.merge else NoMerge()
            with standing(caller_owner(), args.repo or "", policy, merge=merge) as tree:
                result = subprocess.run([part.replace("{tree}", str(tree)) for part in command],
                                        cwd=tree, pass_fds=COMMAND_LOCKS.get())
                return result.returncode if result.returncode >= 0 else 128 - result.returncode
        if args.action == "list":
            list_trees()
        else:
            scope = (ALL_REPOSITORIES if args.repo is None else
                     SelectedRepository(repository_name(Path(args.repo).expanduser().resolve())))
            remove_trees(caller_owner(), scope)
    except (CopyError, OSError, scratch.ScratchError) as error:
        print(f"copies: {error}", file=sys.stderr)
        return error.exit_code if isinstance(error, CopyError) else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
