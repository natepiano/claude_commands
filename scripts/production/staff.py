#!/usr/bin/env python3
"""The production staff record, stored once on natedev for both workstations."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping, Callable, Generator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import Literal, NewType, Protocol, TypeVar, cast
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if __name__ == "__main__":
    # Lazy runtime readers share these exact row and exception classes in the CLI.
    sys.modules["scripts.production.staff"] = sys.modules[__name__]
from scripts.lib import atomic_file  # noqa: E402
from scripts.shutdown import remote  # noqa: E402

OWNER_MACHINE = "natedev"
OPERATION_LOCK_TIMEOUT_SECONDS = 30.0
StaffId = NewType("StaffId", str)
Kind = Literal["showrunner", "unit", "worker"]
_KINDS: tuple[Kind, ...] = ("showrunner", "unit", "worker")
def native_id(value: str) -> StaffId:
    """Validate an identity supplied by Claude or Codex, without assigning one."""
    try:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical runtime id")
    except ValueError as error:
        raise StaffRefused(f"invalid native runtime id: {value!r}") from error
    return StaffId(value)


def _valid_id(value: str) -> bool:
    try:
        _ = native_id(value)
        return True
    except StaffRefused:
        # Transitional records are readable only so the one-time migration can run.
        return bool(re.fullmatch(r"s[a-z0-9]{11}", value))


def runtime_id(pid: int | None = None) -> str:
    """Read the exact Claude registry pid, or the caller's Codex runtime identity."""
    from scripts.message import sessions
    if pid is None and (thread := os.environ.get("CODEX_THREAD_ID", "")):
        try:
            return str(native_id(thread))
        except StaffRefused:
            return ""
    current = os.getpid() if pid is None else pid
    for depth in range(32):
        del depth
        started = sessions.process_start(current)
        if not started:
            return ""
        path = sessions.sessions_dir() / f"{current}.json"
        if path.exists():
            record = sessions.read_session(path)
            if (not isinstance(record, sessions.UnreadableSessionRecord)
                    and record["pid"] == current and sessions.running_session(record)
                    and sessions.process_start(current) == started):
                return str(native_id(record["sessionId"]))
        if pid is not None or current <= 1:
            return ""
        try:
            if sys.platform == "darwin":
                result = subprocess.run(["ps", "-p", str(current), "-o", "ppid="], capture_output=True,
                                        text=True, check=True, timeout=2)
                current = int(result.stdout.strip())
            else:
                stat = (Path(os.environ.get("HERDR_PROC", "/proc")) / str(current) / "stat").read_text()
                current = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return ""
    return ""

_Result = TypeVar("_Result")


class StaffUnreadable(OSError):
    """The staff record could not be read or safely written."""


class StaffAbsent(StaffUnreadable):
    """No staff record exists yet; a first seed may create it."""


class StaffRefused(Exception):
    """A requested change would break the staff rules."""


class StaffNotFound(LookupError):
    """A requested member does not exist."""

    def __init__(self, staff_id: StaffId) -> None:
        self.staff_id: StaffId = staff_id
        super().__init__(f"staff member not found: {staff_id}")


@dataclass(frozen=True)
class WorkerName:
    """A launcher-safe name; assignment and holder are read from worker records."""

    value: str

    @classmethod
    def parse(cls, name: str) -> WorkerName:
        if re.fullmatch(r"[A-Za-z0-9._-]+", name) and name not in (".", ".."):
            return cls(name)
        raise StaffRefused(f"invalid worker name: {name!r}; expected launcher name characters")


def _location(machine: str, path: Path) -> None:
    if not machine or machine != machine.strip() or not path.is_absolute():
        raise StaffRefused("a staff location requires a machine name and an absolute path")


@dataclass(frozen=True)
class PresentWorktree:
    machine: str
    path: Path

    def __post_init__(self) -> None:
        _location(self.machine, self.path)


@dataclass(frozen=True)
class RemovedWorktree:
    machine: str
    path: Path

    def __post_init__(self) -> None:
        _location(self.machine, self.path)


@dataclass(frozen=True)
class WorktreeUnfilled:
    """The transitional record has no worktree fact yet."""


@dataclass(frozen=True)
class LiveDoc:
    machine: str
    path: Path

    def __post_init__(self) -> None:
        _location(self.machine, self.path)


@dataclass(frozen=True)
class WrappedDoc:
    machine: str
    path: Path

    def __post_init__(self) -> None:
        _location(self.machine, self.path)


@dataclass(frozen=True)
class DocUnfilled:
    """The transitional record has no production document fact yet."""


WORKTREE_UNFILLED = WorktreeUnfilled()
DOC_UNFILLED = DocUnfilled()


@dataclass(frozen=True)
class StaffRow:
    id: StaffId
    kind: Kind
    name: str
    parent: StaffId | None
    current_session: StaffId = field(kw_only=True)

    worktree: PresentWorktree | RemovedWorktree | WorktreeUnfilled = field(default_factory=WorktreeUnfilled)
    doc: LiveDoc | WrappedDoc | DocUnfilled = field(default_factory=DocUnfilled)

    def to_json(self) -> dict[str, object]:
        value: dict[str, object] = {"id": self.id, "kind": self.kind, "name": self.name, "parent": self.parent, "current_session": self.current_session}
        if not isinstance(self.worktree, WorktreeUnfilled):
            value["worktree"] = {"machine": self.worktree.machine, "path": str(self.worktree.path),
                                 "state": "present" if isinstance(self.worktree, PresentWorktree) else "removed"}
        if not isinstance(self.doc, DocUnfilled):
            value["doc"] = {"machine": self.doc.machine, "path": str(self.doc.path),
                            "state": "live" if isinstance(self.doc, LiveDoc) else "wrapped"}
        return value

    @classmethod
    def from_json(cls, value: object) -> StaffRow:
        if not isinstance(value, dict):
            raise StaffUnreadable("staff row must be an object")
        fields = cast(dict[str, object], value)
        if not {"id", "kind", "name", "parent"} <= set(fields) or set(fields) - {"id", "kind", "name", "parent", "current_session", "worktree", "doc"}:
            raise StaffUnreadable("staff row must contain id, kind, name and parent")
        staff_id, kind, name, parent = (fields[key] for key in ("id", "kind", "name", "parent"))
        if not isinstance(staff_id, str) or not _valid_id(staff_id):
            raise StaffUnreadable("invalid staff id")
        if not isinstance(kind, str) or kind not in _KINDS:
            raise StaffUnreadable("invalid staff kind")
        if not isinstance(name, str):
            raise StaffUnreadable("staff name must be a string")
        if parent is not None and (not isinstance(parent, str) or not _valid_id(parent)):
            raise StaffUnreadable("invalid staff parent id")
        current = fields.get("current_session", staff_id)
        if not isinstance(current, str) or not _valid_id(current):
            raise StaffUnreadable("invalid current session")
        worktree, doc = _row_locations(kind, fields)
        return cls(StaffId(staff_id), kind, name,
                   StaffId(parent) if isinstance(parent, str) else None, worktree, doc, current_session=StaffId(current))


def _row_locations(kind: str, fields: dict[str, object]) -> tuple[PresentWorktree | RemovedWorktree | WorktreeUnfilled, LiveDoc | WrappedDoc | DocUnfilled]:
    worktree: PresentWorktree | RemovedWorktree | WorktreeUnfilled = WorktreeUnfilled()
    doc: LiveDoc | WrappedDoc | DocUnfilled = DocUnfilled()
    for key in ("worktree", "doc"):
        if key not in fields:
            continue
        if kind != ("unit" if key == "worktree" else "showrunner"):
            raise StaffUnreadable(f"{kind} cannot hold {key}")
        raw = fields[key]
        if not isinstance(raw, dict):
            raise StaffUnreadable(f"{key} must be an object")
        location = cast(dict[str, object], raw)
        if set(location) != {"machine", "path", "state"}:
            raise StaffUnreadable(f"{key} requires machine, path and state")
        machine, path, state = (location.get(item) for item in ("machine", "path", "state"))
        if not isinstance(machine, str) or not isinstance(path, str):
            raise StaffUnreadable(f"invalid {key} location")
        try:
            if key == "worktree" and state in ("present", "removed"):
                worktree = PresentWorktree(machine, Path(path)) if state == "present" else RemovedWorktree(machine, Path(path))
            elif key == "doc" and state in ("live", "wrapped"):
                doc = LiveDoc(machine, Path(path)) if state == "live" else WrappedDoc(machine, Path(path))
            else:
                raise StaffUnreadable(f"invalid {key} state: {state!r}")
        except StaffRefused as error:
            raise StaffUnreadable(str(error)) from error
    return worktree, doc


@dataclass(frozen=True)
class CheckoutDocument:
    """A document identified by its repository and path within a checkout."""

    common_directory: Path
    relative_path: Path


@dataclass(frozen=True)
class OutsideGitCheckout:
    """No readable checkout metadata identifies this document."""


def _document_checkout(path: Path) -> CheckoutDocument | OutsideGitCheckout:
    """Read document identity from Git metadata without starting a process."""
    try:
        document = path.resolve()
        for folder in document.parents:
            marker = folder / ".git"
            if marker.is_dir():
                git_directory = marker
            elif marker.is_file():
                directive = marker.read_text(encoding="utf-8").strip()
                if not directive.startswith("gitdir:"):
                    return OutsideGitCheckout()
                location = directive.removeprefix("gitdir:").strip()
                if not location:
                    return OutsideGitCheckout()
                git_directory = folder / location
            else:
                continue
            git_directory = git_directory.resolve()
            if not git_directory.is_dir():
                return OutsideGitCheckout()
            common_file = git_directory / "commondir"
            if common_file.exists():
                location = common_file.read_text(encoding="utf-8").strip()
                if not location:
                    return OutsideGitCheckout()
                common_directory = (git_directory / location).resolve()
            else:
                common_directory = git_directory
            if not common_directory.is_dir():
                return OutsideGitCheckout()
            return CheckoutDocument(common_directory, document.relative_to(folder))
    except (OSError, UnicodeError, RuntimeError):
        return OutsideGitCheckout()
    return OutsideGitCheckout()


@dataclass(frozen=True)
class StaffSnapshot:
    rows: tuple[StaffRow, ...]
    pending_retire: tuple[StaffId, ...] = ()
    inputs: tuple[str, ...] = field(default=(), compare=False)
    schema: int = field(default=2, compare=False)
    current_sessions_recorded: bool = field(default=True, compare=False)

    def by_id(self, staff_id: StaffId) -> StaffRow | None:
        return _row_by_id(self.rows, staff_id)

    def by_current_session(self, session_id: str) -> StaffRow | None:
        return next((row for row in self.rows if row.current_session == session_id), None)

    def by_member_reference(self, reference: str) -> StaffRow | None:
        """Resolve a persisted fixed member id or its current conversation."""
        return self.by_id(StaffId(reference)) or self.by_current_session(reference)

    def by_path(self, names: str) -> StaffRow | None:
        return _row_by_path(self.rows, names)

    def by_worktree(self, machine: str, path: Path) -> StaffRow | None:
        matches = [row for row in self.rows if isinstance(row.worktree, PresentWorktree)
                   and row.worktree.machine == machine and path.is_relative_to(row.worktree.path)]
        return max(matches, key=lambda row: len(cast(PresentWorktree, row.worktree).path.parts), default=None)

    def by_doc(self, machine: str, path: Path) -> StaffRow | None:
        document = path.absolute()
        candidates = [row for row in self.rows if isinstance(row.doc, (LiveDoc, WrappedDoc))
                      and row.doc.machine == machine]
        exact = next((row for row in candidates if isinstance(row.doc, (LiveDoc, WrappedDoc))
                      and row.doc.path == document), None)
        if exact is not None:
            return exact
        identity = _document_checkout(document)
        if isinstance(identity, OutsideGitCheckout):
            return None
        matches = [row for row in candidates if isinstance(row.doc, (LiveDoc, WrappedDoc))
                   and _document_checkout(row.doc.path) == identity]
        if len(matches) > 1:
            names = ", ".join(str(row.id) for row in matches)
            raise StaffRefused(f"{document}: ambiguous production document; staff rows: {names}")
        return next(iter(matches), None)

    def find(self, *, kind: Kind | None = None, parent: StaffId | None = None) -> list[StaffRow]:
        """An omitted kind or parent leaves that field unrestricted."""
        return [row for row in self.rows if (kind is None or row.kind == kind)
                and (parent is None or row.parent == parent)]


def active(record: StaffSnapshot, kind: Kind) -> tuple[StaffRow, ...]:
    """Current members; raw identity and history readers keep using the snapshot."""
    return tuple(row for row in record.rows if row.kind == kind
                 and not isinstance(row.worktree, RemovedWorktree)
                 and not isinstance(row.doc, WrappedDoc))


@dataclass
class RecordInputs:
    """Attempted source-record reads for one command, including unreadable files."""

    paths: set[str] = field(default_factory=set)

    def __call__(self, path: Path) -> None:
        self.paths.add(str(path.expanduser().absolute()))


@dataclass(frozen=True)
class NewSeedMember:
    """No member matched; ensure the source name under the write lock."""


@dataclass(frozen=True)
class KeepSeedParent:
    """The source did not establish a parent; preserve the current edge."""


@dataclass(frozen=True)
class RootSeedParent:
    """The source establishes a showrunner with no parent."""


@dataclass(frozen=True)
class SeedParent:
    key: str


@dataclass(frozen=True)
class SeedMember:
    key: str
    kind: Kind
    name: str
    match: StaffId | NewSeedMember
    parent: SeedParent | KeepSeedParent | RootSeedParent
    runtime_id: str = ""


def _seed_fields(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StaffRefused("seed input must contain objects")
    fields = cast(dict[str, object], value)
    if set(fields) != keys:
        raise StaffRefused(f"seed object requires {', '.join(sorted(keys))}")
    return fields


def _seed_id(value: object) -> StaffId:
    if not isinstance(value, str) or not _valid_id(value):
        raise StaffRefused("invalid staff id in seed input")
    return StaffId(value)


@dataclass(frozen=True)
class SeedRetirement:
    staff_id: StaffId
    parent: StaffId


@dataclass(frozen=True)
class SeedInput:
    members: tuple[SeedMember, ...]
    retire: tuple[SeedRetirement, ...] = ()

    def to_json(self) -> dict[str, object]:
        members: list[dict[str, object]] = []
        for member in self.members:
            parent = ({"key": member.parent.key} if isinstance(member.parent, SeedParent)
                      else {"mode": "root" if isinstance(member.parent, RootSeedParent) else "keep"})
            members.append({"key": member.key, "kind": member.kind, "name": member.name,
                            "match": None if isinstance(member.match, NewSeedMember) else member.match,
                            "parent": parent, "runtime_id": member.runtime_id})
        return {"members": members, "retire": [{"id": entry.staff_id, "parent": entry.parent}
                                               for entry in self.retire]}

    @classmethod
    def from_json(cls, value: object) -> SeedInput:
        fields = _seed_fields(value, {"members", "retire"})
        raw_members, raw_retire = fields["members"], fields["retire"]
        if not isinstance(raw_members, list) or not isinstance(raw_retire, list):
            raise StaffRefused("seed members and retire must be lists")
        members: list[SeedMember] = []
        for item in cast(list[object], raw_members):
            raw = cast(dict[str, object], item) if isinstance(item, dict) else {}
            keys = {"key", "kind", "name", "match", "parent"}
            entry = _seed_fields(cast(object, item), keys | ({"runtime_id"} if "runtime_id" in raw else set()))
            runtime = entry.get("runtime_id", "")
            if not isinstance(runtime, str):
                raise StaffRefused("invalid seed native runtime identity")
            key, kind, name, match = (entry[field] for field in ("key", "kind", "name", "match"))
            if not isinstance(key, str) or not key or kind not in _KINDS or not isinstance(name, str):
                raise StaffRefused("invalid seed member")
            parent_value = entry["parent"]
            if not isinstance(parent_value, dict):
                raise StaffRefused("seed parent must be an object")
            parent_fields = cast(dict[str, object], parent_value)
            if "key" in parent_fields:
                parent_key = _seed_fields(parent_fields, {"key"})["key"]
                if not isinstance(parent_key, str) or not parent_key:
                    raise StaffRefused("invalid seed parent key")
                parent: SeedParent | KeepSeedParent | RootSeedParent = SeedParent(parent_key)
            else:
                mode = _seed_fields(parent_fields, {"mode"})["mode"]
                if mode not in ("keep", "root"):
                    raise StaffRefused("invalid seed parent mode")
                parent = RootSeedParent() if mode == "root" else KeepSeedParent()
            members.append(SeedMember(key, kind, name,
                                      NewSeedMember() if match is None else _seed_id(match), parent, runtime))
        retire: list[SeedRetirement] = []
        for item in cast(list[object], raw_retire):
            entry = _seed_fields(item, {"id", "parent"})
            retire.append(SeedRetirement(_seed_id(entry["id"]), _seed_id(entry["parent"])))
        return cls(tuple(members), tuple(retire))


def _file() -> Path:
    return Path(os.environ.get("STAFF_FILE") or Path.home() / ".local/state/staff/staff.json").expanduser()


def folder_move_path(unit: StaffId) -> Path:
    """Keep move progress beside the selected staff record."""
    _ = native_id(unit)
    return _file().parent / "folder_moves" / f"{unit}.json"


def _is_owner() -> bool:
    """An explicit STAFF_FILE is local state owned by this process."""
    return bool(os.environ.get("STAFF_FILE")) or remote.other_machine() != OWNER_MACHINE


def operation_fds() -> tuple[int, ...]:
    """Descriptors a nested tool must preserve with subprocess pass_fds."""
    try:
        return (int(os.environ["STAFF_OPERATION_FD"]),) if _joined_operation(_file().parent / "operation.lock") else ()
    except (KeyError, ValueError):
        return ()


def _joined_operation(path: Path) -> bool:
    try:
        descriptor = int(os.environ["STAFF_OPERATION_FD"])
        opened, target = os.fstat(descriptor), path.stat()
        if (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino):
            return False
        # A distinct open must be blocked, and the inherited open description
        # must already hold the lock. An unlocked fd is not proof.
        with path.open("a", encoding="utf-8") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
        return False
    except (KeyError, ValueError, OSError):
        return False


def _operation_lock_holder(path: Path) -> str:
    """Read the kernel's current flock owner; inherited locks can outlive that pid."""
    try:
        target = path.stat()
        for line in Path("/proc/locks").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) < 6 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
                continue
            major, minor, inode = fields[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == (
                os.major(target.st_dev), os.minor(target.st_dev), target.st_ino
            ) and int(fields[4]) > 0:
                return f"pid {fields[4]}"
    except (OSError, ValueError):
        pass
    return "holder pid unavailable"


def _wait_for_operation_lock(descriptor: int, path: Path) -> None:
    started = time.monotonic()
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            elapsed = time.monotonic() - started
            remaining = OPERATION_LOCK_TIMEOUT_SECONDS - elapsed
            if remaining <= 0:
                raise StaffRefused(
                    f"staff operation lock {path}: {_operation_lock_holder(path)}; " +
                    f"refused after {elapsed:.2f}s (limit {OPERATION_LOCK_TIMEOUT_SECONDS:.2f}s)"
                ) from None
            time.sleep(min(0.05, remaining))


def operation() -> AbstractContextManager[None]:
    """Order source changes, joining only a proven inherited operation lock."""
    @contextmanager
    def locked() -> Generator[None]:
        path = _file().parent / "operation.lock"
        if _joined_operation(path):
            yield
            return
        with ExitStack() as resources:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                lock = resources.enter_context(path.open("a", encoding="utf-8"))
                _wait_for_operation_lock(lock.fileno(), path)
            except OSError as error:
                raise StaffUnreadable(f"staff operation: {error}") from error
            previous = os.environ.get("STAFF_OPERATION_FD")
            os.environ["STAFF_OPERATION_FD"] = str(lock.fileno())
            try:
                yield
            finally:
                if previous is None:
                    _ = os.environ.pop("STAFF_OPERATION_FD", None)
                else:
                    os.environ["STAFF_OPERATION_FD"] = previous

    return locked()


def _parent_kinds(kind: Kind) -> tuple[Kind, ...]:
    return ("unit", "showrunner") if kind == "worker" else ("showrunner",)


def _validate(rows: list[StaffRow]) -> None:
    """Check the complete proposed graph before any bytes are replaced."""
    indexed: dict[StaffId, StaffRow] = {}
    sessions: set[StaffId] = set()
    names: set[tuple[Kind, StaffId | None, str]] = set()
    worktrees: set[tuple[str, Path]] = set()
    for row in rows:
        if row.id in indexed:
            raise StaffRefused(f"duplicate staff id: {row.id}")
        indexed[row.id] = row
        if not _valid_id(row.current_session):
            raise StaffRefused(f"{row.id}: invalid current session")
        if row.current_session in sessions:
            raise StaffRefused(f"duplicate current session: {row.current_session}")
        sessions.add(row.current_session)
        if row.kind != "unit" and not isinstance(row.worktree, WorktreeUnfilled):
            raise StaffRefused(f"{row.kind} cannot hold worktree")
        if row.kind != "showrunner" and not isinstance(row.doc, DocUnfilled):
            raise StaffRefused(f"{row.kind} cannot hold doc")
        if isinstance(row.worktree, PresentWorktree):
            location = (row.worktree.machine, row.worktree.path)
            if location in worktrees:
                raise StaffRefused(f"duplicate present worktree: {location[0]}:{location[1]}")
            worktrees.add(location)
        if row.kind not in _KINDS:
            raise StaffRefused(f"invalid staff kind: {row.kind}")
        if not row.name or _valid_id(row.name) or row.name != row.name.strip() or "/" in row.name or any(
            ord(character) < 32 or ord(character) == 127 for character in row.name
        ):
            raise StaffRefused(f"invalid short staff name: {row.name!r}")
        if row.kind == "worker":
            _ = WorkerName.parse(row.name)
        key = (row.kind, None if row.kind == "showrunner" else row.parent, row.name)
        if key in names:
            raise StaffRefused(f"{row.kind} name already exists: {row.name}")
        names.add(key)
    for row in rows:
        if row.parent is None:
            if row.kind != "showrunner":
                raise StaffRefused(f"{row.kind} {row.name} requires a parent")
        else:
            parent = indexed.get(row.parent)
            allowed = _parent_kinds(row.kind)
            if parent is None or parent.kind not in allowed:
                raise StaffRefused(f"{row.kind} {row.name} requires a {' or '.join(allowed)} parent: {row.parent}")
    # Once a chain is checked, every later chain reaching it is already known to end.
    checked: set[StaffId] = set()
    for row in rows:
        chain: set[StaffId] = set()
        current = row
        while current.id not in checked:
            if current.id in chain:
                raise StaffRefused(f"staff parent loop at {current.name}")
            chain.add(current.id)
            if current.parent is None:
                break
            current = indexed[current.parent]
        checked.update(chain)


def _decode(text: str) -> object:
    try:
        return cast(object, json.loads(text))
    except (ValueError, RecursionError) as error:
        raise StaffUnreadable(f"invalid staff JSON: {error}") from error


def _rows(value: object) -> list[StaffRow]:
    if not isinstance(value, list):
        raise StaffUnreadable("staff rows must be a list")
    rows = [StaffRow.from_json(item) for item in cast(list[object], value)]
    try:
        _validate(rows)
    except StaffRefused as error:
        raise StaffUnreadable(str(error)) from error
    return sorted(rows, key=lambda row: row.id)


def _load(*, initial: bool = False) -> StaffSnapshot:
    try:
        text = _file().read_text(encoding="utf-8")
    except FileNotFoundError as error:
        if initial:
            return StaffSnapshot(())
        raise StaffAbsent(f"cannot read staff record: {error}") from error
    except (OSError, UnicodeError) as error:
        raise StaffUnreadable(f"cannot read staff record: {error}") from error
    value = _decode(text)
    if not isinstance(value, dict):
        raise StaffUnreadable("staff record must be an object")
    fields = cast(dict[str, object], value)
    schema = fields.get("schema")
    if type(schema) is not int or schema not in (1, 2):
        raise StaffUnreadable(f"unsupported staff schema: {schema!r}")
    return replace(_snapshot_value(fields), inputs=(str(_file().absolute()),))


def _snapshot_value(fields: dict[str, object]) -> StaffSnapshot:
    pending = fields.get("pending_retire", [])
    if not isinstance(pending, list) or any(not isinstance(item, str) or not _valid_id(item)
                                             for item in cast(list[object], pending)):
        raise StaffUnreadable("invalid pending retirement ids")
    inputs = fields.get("inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(path, str) or not Path(path).is_absolute()
                                         for path in cast(list[object], inputs)):
        raise StaffUnreadable("invalid staff input paths")
    return StaffSnapshot(tuple(_rows(fields.get("rows"))),
                         tuple(sorted({StaffId(item) for item in cast(list[str], pending)})),
                         tuple(cast(list[str], inputs)), cast(int, fields.get("schema", 2)),
                         all("current_session" in item for item in cast(list[dict[str, object]], fields.get("rows", []))))


def _remote(arguments: list[str], *, input_text: str | None = None) -> object:
    # Direct worker writers run inside the caller's membership lock. Match the
    # local API's row transaction without entering the outer lifecycle operation.
    if arguments[:2] == ["ensure", "worker"] or arguments[0] == "update-current-session":
        arguments = [*arguments, "--row-write"]
    elif arguments[0] == "retire":
        arguments = [*arguments, "--worker-row"]
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", OWNER_MACHINE,
               "~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py "
               + shlex.join([*arguments, "--json"])]
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL if input_text is None else None,
                              input=input_text, capture_output=True,
                              text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StaffUnreadable(f"{OWNER_MACHINE} not reached") from error
    reason = done.stderr.strip()
    if done.returncode != 0 and (done.returncode == 255 or not reason):
        raise StaffUnreadable(f"{OWNER_MACHINE} not reached")
    if done.returncode == 1 and reason.startswith("staff member not found: "):
        raise StaffNotFound(StaffId(reason.removeprefix("staff member not found: ")))
    if done.returncode == 2:
        if reason.startswith("StaffRefused: "):
            raise StaffRefused(reason.removeprefix("StaffRefused: "))
        if reason.startswith("StaffAbsent: "):
            raise StaffAbsent(reason.removeprefix("StaffAbsent: "))
        if reason.startswith("StaffUnreadable: "):
            raise StaffUnreadable(reason.removeprefix("StaffUnreadable: "))
    if done.returncode != 0:
        raise StaffUnreadable(reason)
    if done.stderr:
        print(done.stderr, end="", file=sys.stderr)
    return _decode(done.stdout)


def _remote_row(arguments: list[str]) -> StaffRow:
    return StaffRow.from_json(_remote(arguments))


def _all_rows() -> list[StaffRow]:
    return list(_load().rows) if _is_owner() else _rows(_remote(["show"]))


def snapshot() -> StaffSnapshot:
    if _is_owner():
        return _load()
    value = _remote(["show", "--snapshot"])
    if not isinstance(value, dict):
        raise StaffUnreadable("staff snapshot must be an object")
    return _snapshot_value(cast(dict[str, object], value))


def local_snapshot() -> StaffSnapshot:
    """Read only this machine's retained record; never contact its remote owner."""
    return _load()


def initial_snapshot() -> StaffSnapshot:
    """Read a writer's starting record, including an installation not yet seeded."""
    try:
        return snapshot()
    except StaffAbsent:
        return StaffSnapshot(())


def _row_by_id(rows: Sequence[StaffRow], staff_id: StaffId) -> StaffRow | None:
    return next((row for row in rows if row.id == staff_id), None)


def _row_by_path(rows: Sequence[StaffRow], names: str) -> StaffRow | None:
    parts = names.split("/")
    if not all(parts) or len(parts) > len(_KINDS):
        return None
    # A showrunner starts its own path even when attached to another showrunner.
    row = next((row for row in rows if row.kind == "showrunner" and row.name == parts[0]), None)
    for name in parts[1:]:
        if row is None:
            return None
        parent = row.id
        matches = [member for member in rows
                   if member.kind != "showrunner" and member.parent == parent and member.name == name]
        row = matches[0] if len(matches) == 1 else None
    return row


def _lookup(rows: list[StaffRow], target: str) -> StaffRow | None:
    record = StaffSnapshot(tuple(rows))
    row = record.by_member_reference(target) or record.by_path(target)
    if row is None and Path(target).is_absolute():
        return record.by_worktree(remote.local_machine(), Path(target))
    return row


def by_id(staff_id: StaffId) -> StaffRow | None:
    return snapshot().by_id(staff_id)


def by_current_session(session_id: str) -> StaffRow | None:
    return snapshot().by_current_session(session_id)


def current_session_target(target: str) -> str:
    """Resolve a retained member address when delivering to its live conversation."""
    if not target.startswith("session:"):
        return target
    row = initial_snapshot().by_id(StaffId(target[8:]))
    return "session:" + row.current_session if row is not None else target


def by_path(names: str) -> StaffRow | None:
    return snapshot().by_path(names)


def find(*, kind: Kind | None = None, parent: StaffId | None = None) -> list[StaffRow]:
    """Find members; an omitted kind or parent leaves that field unrestricted."""
    return snapshot().find(kind=kind, parent=parent)


def _store(rows: list[StaffRow], pending: set[StaffId]) -> None:
    document: dict[str, object] = {"schema": 2, "rows": [row.to_json() for row in rows]}
    if pending:
        document["pending_retire"] = sorted(pending)
    content = json.dumps(document,
                         indent=2, sort_keys=True) + "\n"
    atomic_file.write_text_atomic(_file(), content)


def _record_transaction(edit: Callable[[list[StaffRow], set[StaffId]], _Result]) -> _Result:
    path = _file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for temporary in path.parent.glob(f".{path.name}.*"):
                temporary.unlink()
            original = _load(initial=True)
            proposed = list(original.rows)
            pending = set(original.pending_retire)
            result = edit(proposed, pending)
            _validate(proposed)
            proposed.sort(key=lambda row: row.id)
            if original.schema == 2 and original.current_sessions_recorded and proposed == list(original.rows) and pending == set(original.pending_retire):
                # A prior replace may have succeeded before its folder sync failed.
                atomic_file.sync_folder(path.parent)
                print("unchanged", file=sys.stderr)
            else:
                _store(proposed, pending)
            return result
    except OSError as error:
        if isinstance(error, StaffUnreadable):
            raise
        raise StaffUnreadable(f"cannot write staff record: {error}") from error


def _transaction(edit: Callable[[list[StaffRow]], _Result]) -> _Result:
    return _record_transaction(lambda rows, pending: edit(rows))


def _required(rows: list[StaffRow], staff_id: StaffId) -> StaffRow:
    row = _row_by_id(rows, staff_id)
    if row is None:
        raise StaffNotFound(staff_id)
    return row


def _ensure_row(rows: list[StaffRow], kind: Kind, name: str, parent: StaffId | None, *,
                worktree: PresentWorktree | RemovedWorktree | WorktreeUnfilled = WORKTREE_UNFILLED,
                doc: LiveDoc | WrappedDoc | DocUnfilled = DOC_UNFILLED,
                runtime_id: str = "") -> StaffRow:
    if kind != "unit" and not isinstance(worktree, WorktreeUnfilled):
        raise StaffRefused(f"{kind} cannot hold worktree")
    if kind != "showrunner" and not isinstance(doc, DocUnfilled):
        raise StaffRefused(f"{kind} cannot hold doc")
    existing = next((row for row in rows
                     if (row.kind, row.name, row.parent) == (kind, name, parent)), None)
    if existing is not None:
        if runtime_id and existing.current_session != native_id(runtime_id):
            raise StaffRefused(f"{existing.id}: existing member has a different runtime identity: {runtime_id}")
        if not isinstance(worktree, WorktreeUnfilled) and not isinstance(existing.worktree, WorktreeUnfilled) and existing.worktree != worktree:
            raise StaffRefused(f"{existing.id}: existing worktree conflicts with admission")
        if not isinstance(doc, DocUnfilled) and not isinstance(existing.doc, DocUnfilled) and existing.doc != doc:
            raise StaffRefused(f"{existing.id}: existing doc conflicts with admission")
        admitted = replace(existing,
                           worktree=existing.worktree if isinstance(worktree, WorktreeUnfilled) else worktree,
                           doc=existing.doc if isinstance(doc, DocUnfilled) else doc)
        rows[rows.index(existing)] = admitted
        return admitted
    if not runtime_id:
        raise StaffRefused(f"{kind} {name}: native runtime identity is not proved")
    staff_id = native_id(runtime_id)
    if _row_by_id(rows, staff_id) is not None or any(row.current_session == staff_id for row in rows):
        raise StaffRefused(f"{staff_id}: runtime identity already belongs to another member")
    row = StaffRow(staff_id, kind, name, parent, worktree, doc, current_session=staff_id)
    rows.append(row)
    return row


def ensure(kind: Kind, *, name: str, parent: StaffId | None = None,
           worktree: PresentWorktree | RemovedWorktree | WorktreeUnfilled = WORKTREE_UNFILLED,
           doc: LiveDoc | WrappedDoc | DocUnfilled = DOC_UNFILLED,
                runtime_id: str = "") -> StaffRow:
    """Admit the identity and its location in the same owner transaction."""
    if not _is_owner():
        arguments = ["ensure", kind, f"--name={name}", f"--runtime-id={runtime_id}"]
        if parent is not None:
            arguments.extend(("--parent", parent))
        if not isinstance(worktree, WorktreeUnfilled):
            arguments.extend(("--worktree", worktree.machine, str(worktree.path),
                              "present" if isinstance(worktree, PresentWorktree) else "removed"))
        if not isinstance(doc, DocUnfilled):
            arguments.extend(("--doc", doc.machine, str(doc.path), "live" if isinstance(doc, LiveDoc) else "wrapped"))
        return _remote_row(arguments)

    return _transaction(lambda rows: _ensure_row(rows, kind, name, parent, worktree=worktree, doc=doc, runtime_id=runtime_id))


def ensure_worker(name: str, parent: StaffId, *, runtime_id: str) -> StaffRow:
    """Ensure membership under staff.json.lock, after the caller's membership lock.

    This writer takes no membership, operation or lifecycle lock. The caller may
    hold membership; it must release lifecycle, inbox and roster first.
    """
    return ensure("worker", name=name, parent=parent, runtime_id=runtime_id)


def update_current_session(old_session: StaffId, new_session: StaffId) -> StaffRow:
    """Publish a proved runtime handoff without changing the member or its links."""
    _ = native_id(new_session)
    if not _is_owner():
        return _remote_row(["update-current-session", old_session, new_session])
    def edit(rows: list[StaffRow]) -> StaffRow:
        old = next((row for row in rows if row.current_session == old_session), None)
        destination = next((row for row in rows if row.current_session == new_session), None)
        if old is None:
            if destination is not None:
                return destination
            raise StaffNotFound(old_session)
        if old_session == new_session:
            return old
        fixed = _row_by_id(rows, new_session)
        if destination is not None or (fixed is not None and fixed.id != old.id):
            raise StaffRefused(f"{new_session}: runtime identity already belongs to another member")
        changed = replace(old, current_session=new_session)
        rows[rows.index(old)] = changed
        return changed
    return _transaction(edit)


def promote_unit(identity: StaffId, *, name: str, parent: StaffId, doc: LiveDoc) -> StaffRow:
    """The same native session changes role without acquiring another identity."""
    if not _is_owner():
        return _remote_row(["promote-unit", identity, "--name", name, "--parent", parent,
                            "--machine", doc.machine, "--path", str(doc.path)])
    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, identity)
        if row.kind == "showrunner":
            if (row.name, row.parent, row.doc) != (name, parent, doc):
                raise StaffRefused(f"{identity}: promotion destination conflicts")
            return row
        if row.kind != "unit":
            raise StaffRefused(f"{identity}: promotion requires a unit")
        promoted = replace(row, kind="showrunner", name=name, parent=parent,
                           worktree=WORKTREE_UNFILLED, doc=doc)
        rows[rows.index(row)] = promoted
        return promoted
    return _transaction(edit)


def publish_native_ids(expected: StaffSnapshot, identities: Mapping[StaffId, StaffId],
                       additions: tuple[StaffRow, ...] = ()) -> StaffSnapshot:
    """Finish a migration in the single staff writer after its references are ready."""
    def edit(rows: list[StaffRow], pending: set[StaffId]) -> StaffSnapshot:
        if tuple(rows) != expected.rows:
            raise StaffRefused("staff changed during migration; read a fresh snapshot")
        for old, identity in identities.items():
            _ = native_id(identity)
            if not old.startswith("s") and old != identity:
                raise StaffRefused(f"{old}: a native staff id is fixed")
        changed = [replace(row, id=identities.get(row.id, row.id),
                           current_session=(identities.get(row.id, row.current_session)
                                            if row.id.startswith("s") else row.current_session),
                           parent=identities.get(row.parent, row.parent) if row.parent else None)
                   for row in rows]
        for row in additions:
            _ = native_id(row.id)
            existing = _row_by_id(changed, row.id)
            if existing is not None and existing != row:
                raise StaffRefused(f"{row.id}: migration destination conflicts")
            if existing is None:
                changed.append(row)
        rows[:] = changed
        retired = {identities.get(identity, identity) for identity in pending}
        pending.clear()
        pending.update(retired)
        return StaffSnapshot(tuple(sorted(rows, key=lambda row: row.id)), tuple(sorted(pending)))
    return _record_transaction(edit)


def restore_unit(staff_id: StaffId, worktree: PresentWorktree) -> StaffRow:
    """Reactivate the retained unit in place, preserving its identity and parent."""
    if not _is_owner():
        return _remote_row(["restore-unit", staff_id, "--machine", worktree.machine, "--path", str(worktree.path)])

    def edit(rows: list[StaffRow], pending: set[StaffId]) -> StaffRow:
        existing = _required(rows, staff_id)
        if existing.kind != "unit":
            raise StaffRefused("restore-unit requires a unit")
        if isinstance(existing.worktree, WorktreeUnfilled):
            raise StaffRefused(f"{existing.name}: staff worktree is unfilled; run staff_sources.py fill --retired")
        if (existing.worktree.machine, existing.worktree.path) != (worktree.machine, worktree.path):
            raise StaffRefused(f"{existing.name}: restore worktree conflicts with retained location")
        pending.discard(staff_id)
        restored = replace(existing, worktree=worktree)
        rows[rows.index(existing)] = restored
        return restored

    return _record_transaction(edit)


def rename(staff_id: StaffId, new_name: str) -> StaffRow:
    if not _is_owner():
        return _remote_row(["rename", staff_id, f"--name={new_name}"])

    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, staff_id)
        if row.kind == "worker":
            raise StaffRefused("a worker name is set once")
        renamed = replace(row, name=new_name)
        rows[rows.index(row)] = renamed
        return renamed

    return _transaction(edit)


def move(staff_id: StaffId, new_parent: StaffId | None) -> StaffRow:
    if not _is_owner():
        return _remote_row(["move", staff_id, f"--parent={new_parent if new_parent is not None else 'none'}"])

    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, staff_id)
        if row.kind == "worker":
            raise StaffRefused("a worker cannot move between units")
        moved = replace(row, parent=new_parent)
        rows[rows.index(row)] = moved
        return moved

    return _transaction(edit)


def set_worktree(unit: StaffId, worktree: PresentWorktree | RemovedWorktree) -> StaffRow:
    """Set one unit's worktree through the existing owner transaction."""
    if not _is_owner():
        return _remote_row(["set-worktree", unit, "--machine", worktree.machine, "--path", str(worktree.path),
                            "--state", "present" if isinstance(worktree, PresentWorktree) else "removed"])

    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, unit)
        if row.kind != "unit":
            raise StaffRefused("set-worktree requires a unit")
        changed = replace(row, worktree=worktree)
        rows[rows.index(row)] = changed
        return changed

    return _transaction(edit)


def set_doc(showrunner: StaffId, doc: LiveDoc | WrappedDoc) -> StaffRow:
    """Set one showrunner's document through the existing owner transaction."""
    if not _is_owner():
        return _remote_row(["set-doc", showrunner, "--machine", doc.machine, "--path", str(doc.path),
                            "--state", "live" if isinstance(doc, LiveDoc) else "wrapped"])

    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, showrunner)
        if row.kind != "showrunner":
            raise StaffRefused("set-doc requires a showrunner")
        changed = replace(row, doc=doc)
        rows[rows.index(row)] = changed
        return changed

    return _transaction(edit)


def require_stopped(identity: StaffId) -> None:
    """Removal cannot erase a live native session or uncertain process evidence."""
    record = snapshot()
    target = record.by_id(identity)
    if target is None:
        return
    if target.kind != "worker":
        raise StaffRefused("retire requires a worker; retain units and showrunners with their location state")
    from scripts.message import sessions
    observed = sessions.session_liveness(target.current_session)
    if isinstance(observed, sessions.LiveSessionProcess):
        raise StaffRefused(f"{identity}: native session is live (pid {observed.pid}); stop it before removing its row")
    if isinstance(observed, sessions.SessionLivenessUnknown):
        raise StaffRefused(f"{identity}: native session state is uncertain: {observed.reason}")
    # Runtime collection sits above the writer, like the live display command.
    check = cast(Callable[[StaffSnapshot, StaffId], None],
                 import_module("scripts.production.staff_sources").require_worker_stopped)
    check(record, identity)


def retire(staff_id: StaffId) -> StaffRow | None:
    if not _is_owner():
        value = _remote(["retire", staff_id])
        return None if value is None else StaffRow.from_json(value)

    require_stopped(staff_id)
    return _transaction(lambda rows: _retire_row(rows, staff_id))


def _retire_row(rows: list[StaffRow], staff_id: StaffId) -> StaffRow | None:
    row = _row_by_id(rows, staff_id)
    if row is None:
        return None
    if row.kind != "worker":
        raise StaffRefused("retire requires a worker; retain units and showrunners with their location state")
    rows.remove(row)
    return row


def _retain_retired(rows: list[StaffRow], row: StaffRow) -> bool:
    """Finish one unheld retirement without deleting a row or changing an edge."""
    if any(child.kind == "worker" and child.parent == row.id for child in rows):
        return False
    if row.kind == "unit":
        if isinstance(row.worktree, WorktreeUnfilled):
            print(f"staff: keeping {row.id}: worktree is unfilled", file=sys.stderr)
            return False
        changed = replace(row, worktree=RemovedWorktree(row.worktree.machine, row.worktree.path))
    elif row.kind == "showrunner":
        if any(child.kind == "unit" and child.parent == row.id
               and not isinstance(child.worktree, RemovedWorktree) for child in rows):
            return False
        if isinstance(row.doc, DocUnfilled):
            print(f"staff: keeping {row.id}: doc is unfilled", file=sys.stderr)
            return False
        changed = replace(row, doc=WrappedDoc(row.doc.machine, row.doc.path))
    else:
        raise StaffRefused("pending retirement is for units and showrunners")
    rows[rows.index(row)] = changed
    return True


def defer_retire(staff_ids: Sequence[StaffId]) -> None:
    """Remember members kept alive after their source records disappear."""
    if not _is_owner():
        _ = _remote(["defer-retire", *staff_ids])
        return

    def edit(rows: list[StaffRow], pending: set[StaffId]) -> None:
        for staff_id in staff_ids:
            row = _row_by_id(rows, staff_id)
            if row is not None:
                if row.kind == "worker":
                    raise StaffRefused("pending retirement is for units and showrunners")
                pending.add(staff_id)

    _record_transaction(edit)


def retire_pending() -> tuple[StaffId, ...]:
    """Retire unheld units before showrunners, returning the remaining ids."""
    if not _is_owner():
        value = _remote(["retire-pending"])
        if not isinstance(value, list) or any(not isinstance(item, str) or not _valid_id(item)
                                               for item in cast(list[object], value)):
            raise StaffUnreadable("invalid pending retirement ids")
        return tuple(StaffId(item) for item in cast(list[str], value))

    def edit(rows: list[StaffRow], pending: set[StaffId]) -> tuple[StaffId, ...]:
        ordered = sorted(pending, key=lambda key: (0 if (row := _row_by_id(rows, key)) is None
                                                     or row.kind == "unit" else 1, key))
        for staff_id in ordered:
            row = _row_by_id(rows, staff_id)
            if row is not None:
                if not _retain_retired(rows, row):
                    continue
            pending.remove(staff_id)
        return tuple(sorted(pending))

    return _record_transaction(edit)


def apply_seed(seed_input: SeedInput) -> StaffSnapshot:
    """Apply source evidence in one transaction, preserving names matched by id."""
    if not _is_owner():
        value = _remote(["apply-seed"], input_text=json.dumps(seed_input.to_json(), sort_keys=True))
        if not isinstance(value, dict):
            raise StaffUnreadable("staff snapshot must be an object")
        return _snapshot_value(cast(dict[str, object], value))

    def edit(rows: list[StaffRow], pending: set[StaffId]) -> StaffSnapshot:
        entries = {member.key: member for member in seed_input.members}
        if len(entries) != len(seed_input.members) or any(not key for key in entries):
            raise StaffRefused("seed member keys must be nonempty and unique")
        resolved: dict[str, StaffRow] = {}
        resolving: set[str] = set()

        def resolve(key: str) -> StaffRow:
            if key in resolved:
                return resolved[key]
            if key not in entries or key in resolving:
                raise StaffRefused(f"invalid seed parent reference: {key}")
            resolving.add(key)
            entry = entries[key]
            if entry.kind not in _KINDS:
                raise StaffRefused("invalid seed kind")
            if not isinstance(entry.match, NewSeedMember):
                row = _required(rows, entry.match)
                if row.kind != entry.kind:
                    raise StaffRefused(f"seed member has the wrong kind: {row.id}")
            elif entry.kind == "showrunner":
                row = next((row for row in rows if row.kind == "showrunner" and row.name == entry.name), None)
                if row is None:
                    row = _ensure_row(rows, "showrunner", entry.name, None, runtime_id=entry.runtime_id)
            else:
                if not isinstance(entry.parent, SeedParent):
                    raise StaffRefused(f"a new seed {entry.kind} requires its parent")
                parent = resolve(entry.parent.key)
                allowed = _parent_kinds(entry.kind)
                if parent.kind not in allowed:
                    raise StaffRefused(f"a seed {entry.kind} requires a {' or '.join(allowed)} parent")
                row = _ensure_row(rows, entry.kind, entry.name, parent.id, runtime_id=entry.runtime_id)
            if any(member.id == row.id for member in resolved.values()):
                raise StaffRefused(f"seed names a member more than once: {row.id}")
            resolved[key] = row
            resolving.remove(key)
            return row

        for key in entries:
            _ = resolve(key)
        for key, entry in entries.items():
            row = resolved[key]
            if isinstance(entry.parent, KeepSeedParent):
                continue
            parent = resolve(entry.parent.key).id if isinstance(entry.parent, SeedParent) else None
            rows[rows.index(row)] = replace(row, parent=parent)
        retained = {row.id for row in resolved.values()}
        for retirement in seed_input.retire:
            staff_id = retirement.staff_id
            row = _row_by_id(rows, staff_id)
            if row is None:
                continue
            if row.kind != "unit" or staff_id in retained:
                raise StaffRefused(f"seed can retire only an absent unit: {staff_id}")
            if row.parent != retirement.parent:
                print(f"seed: keeping unit {row.name}: parent changed", file=sys.stderr)
            elif any(child.kind == "worker" and child.parent == staff_id for child in rows):
                print(f"seed: keeping unit {row.name}: worker rows remain", file=sys.stderr)
                pending.add(staff_id)
            elif not _retain_retired(rows, row):
                pending.add(staff_id)
        return StaffSnapshot(tuple(sorted(rows, key=lambda row: row.id)), tuple(sorted(pending)))

    return _record_transaction(edit)


def _paths(rows: list[StaffRow]) -> dict[StaffId, str]:
    indexed = {row.id: row for row in rows}
    paths: dict[StaffId, str] = {}
    for row in rows:
        chain: list[str] = [row.name]
        member = row
        while member.kind != "showrunner":
            assert member.parent is not None
            member = indexed[member.parent]
            chain.append(member.name)
        paths[row.id] = "/".join(reversed(chain))
    return paths


class _MoveCommand(Protocol):
    def __call__(self, target: str, new_showrunner: str, *, json_output: bool = False) -> int: ...


class _Arguments(argparse.Namespace):
    runtime_id: str = ""
    new_id: str = ""
    verb: str = ""
    kind: Kind | None = None
    under: str | None = None
    target: str = ""
    destination: str | None = None
    name: str = ""
    parent: str | None = None
    json: bool = False
    snapshot: bool = False
    live: bool = False
    row_write: bool = False
    worker_row: bool = False
    ids: list[str] = []
    machine: str = ""
    path: str = ""
    state: str = ""
    worktree: list[str] | None = None
    doc: list[str] | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    show = verbs.add_parser("show", help="show the production staff")
    _ = show.add_argument("--kind", choices=_KINDS)
    _ = show.add_argument("--live", action="store_true", help="show live member identities")
    _ = show.add_argument("--under", help="parent staff id or path")
    _ = show.add_argument("--snapshot", action="store_true", help=argparse.SUPPRESS)
    get = verbs.add_parser("get", help="find a member by id, names path or worktree path")
    _ = get.add_argument("target")
    add = verbs.add_parser("ensure", help=argparse.SUPPRESS)
    _ = add.add_argument("kind", choices=_KINDS)
    _ = add.add_argument("--name", required=True)
    _ = add.add_argument("--parent")
    _ = add.add_argument("--worktree", nargs=3, metavar=("MACHINE", "PATH", "STATE"))
    _ = add.add_argument("--doc", nargs=3, metavar=("MACHINE", "PATH", "STATE"))
    _ = add.add_argument("--row-write", action="store_true", help=argparse.SUPPRESS)
    session_parser = verbs.add_parser("update-current-session", help=argparse.SUPPRESS)
    _ = session_parser.add_argument("target")
    _ = session_parser.add_argument("new_id")
    _ = session_parser.add_argument("--json", action="store_true")
    _ = session_parser.add_argument("--row-write", action="store_true", help=argparse.SUPPRESS)
    _ = add.add_argument("--runtime-id", default="")
    promotion = verbs.add_parser("promote-unit", help=argparse.SUPPRESS)
    _ = promotion.add_argument("target")
    _ = promotion.add_argument("--name", required=True)
    _ = promotion.add_argument("--parent", required=True)
    _ = promotion.add_argument("--machine", required=True)
    _ = promotion.add_argument("--path", required=True)
    _ = promotion.add_argument("--json", action="store_true")
    restore_parser = verbs.add_parser("restore-unit", help=argparse.SUPPRESS)
    _ = restore_parser.add_argument("target")
    _ = restore_parser.add_argument("--machine", required=True)
    _ = restore_parser.add_argument("--path", required=True)
    rename_parser = verbs.add_parser("rename", help=argparse.SUPPRESS)
    _ = rename_parser.add_argument("target")
    _ = rename_parser.add_argument("--name", required=True)
    move_parser = verbs.add_parser("move", help="move a unit and its live tab to another showrunner")
    _ = move_parser.add_argument("target", help="<showrunner slug>/<unit>")
    _ = move_parser.add_argument("destination", nargs="?", help="new showrunner slug")
    _ = move_parser.add_argument("--parent", help=argparse.SUPPRESS)
    retire_parser = verbs.add_parser("retire", help=argparse.SUPPRESS)
    _ = retire_parser.add_argument("target")
    _ = retire_parser.add_argument("--row-write", action="store_true", help=argparse.SUPPRESS)
    _ = retire_parser.add_argument("--worker-row", action="store_true", help=argparse.SUPPRESS)
    apply_parser = verbs.add_parser("apply-seed", help=argparse.SUPPRESS)
    defer_parser = verbs.add_parser("defer-retire", help=argparse.SUPPRESS)
    _ = defer_parser.add_argument("ids", nargs="*")
    pending_parser = verbs.add_parser("retire-pending", help=argparse.SUPPRESS)
    location_parsers: list[argparse.ArgumentParser] = []
    for command, states in (("set-worktree", ("present", "removed")), ("set-doc", ("live", "wrapped"))):
        location_parser = verbs.add_parser(command)
        _ = location_parser.add_argument("target")
        _ = location_parser.add_argument("--machine", required=True)
        _ = location_parser.add_argument("--path", required=True)
        _ = location_parser.add_argument("--state", required=True, choices=states)
        location_parsers.append(location_parser)
    for verb in (*location_parsers, show, get, add, restore_parser, rename_parser, move_parser, retire_parser, apply_parser, defer_parser, pending_parser):
        _ = verb.add_argument("--json", action="store_true")
    return parser


def _parent(value: str | None) -> StaffId | None:
    return None if value is None or value == "none" else StaffId(value)


def _print_row(row: StaffRow, path: str) -> None:
    print(f"{row.id:<12}  {row.kind:<10}  {path}")


def _mutate(arguments: _Arguments) -> StaffRow | StaffSnapshot | tuple[StaffId, ...] | None:
    if arguments.verb == "update-current-session":
        return update_current_session(StaffId(arguments.target), native_id(arguments.new_id))
    if arguments.verb == "set-worktree":
        worktree = (PresentWorktree(arguments.machine, Path(arguments.path)) if arguments.state == "present"
                    else RemovedWorktree(arguments.machine, Path(arguments.path)))
        return set_worktree(StaffId(arguments.target), worktree)
    if arguments.verb == "set-doc":
        doc = (LiveDoc(arguments.machine, Path(arguments.path)) if arguments.state == "live"
               else WrappedDoc(arguments.machine, Path(arguments.path)))
        return set_doc(StaffId(arguments.target), doc)
    if arguments.verb == "defer-retire":
        return defer_retire(tuple(StaffId(value) for value in arguments.ids))
    if arguments.verb == "retire-pending":
        return retire_pending()
    if arguments.verb == "ensure":
        assert arguments.kind is not None
        # The record decoder owns the location JSON boundary and wrong-kind checks.
        value: dict[str, object] = {}
        for key, location in (("worktree", arguments.worktree), ("doc", arguments.doc)):
            if location is not None:
                machine, path, state = location
                value[key] = {"machine": machine, "path": path, "state": state}
        worktree, doc = _row_locations(arguments.kind, value)
        return ensure(arguments.kind, name=arguments.name, parent=_parent(arguments.parent),
                      worktree=worktree, doc=doc, runtime_id=arguments.runtime_id)
    if arguments.verb == "promote-unit":
        assert arguments.parent is not None
        return promote_unit(StaffId(arguments.target), name=arguments.name, parent=StaffId(arguments.parent),
                            doc=LiveDoc(arguments.machine, Path(arguments.path)))
    if arguments.verb == "restore-unit":
        return restore_unit(StaffId(arguments.target), PresentWorktree(arguments.machine, Path(arguments.path)))
    if arguments.verb == "rename":
        return rename(StaffId(arguments.target), arguments.name)
    if arguments.verb == "move":
        return move(StaffId(arguments.target), _parent(arguments.parent))
    if arguments.verb == "apply-seed":
        return apply_seed(SeedInput.from_json(_decode(sys.stdin.read())))
    if arguments.row_write and _is_owner():
        require_stopped(StaffId(arguments.target))
        return _transaction(lambda rows: _retire_row(rows, StaffId(arguments.target)))
    return retire(StaffId(arguments.target))


def _main(arguments: _Arguments) -> int:
    if arguments.verb == "move":
        if arguments.destination is not None:
            if arguments.parent is not None:
                raise StaffRefused("move takes a showrunner slug or --parent, not both")
            move_command = cast(_MoveCommand, import_module("scripts.production.staff_move").cli_move)
            return move_command(arguments.target, arguments.destination, json_output=arguments.json)
        if arguments.parent is None:
            raise StaffRefused("move needs <showrunner slug>/<unit> <new showrunner slug>")
    if arguments.verb == "show":
        if arguments.live:
            if not arguments.json or arguments.snapshot or arguments.kind or arguments.under:
                raise StaffRefused("live staff requires show --json --live without filters")
            live_command = cast(Callable[[], int], import_module("scripts.production.staff_sources").cli_live)
            return live_command()
        if arguments.snapshot:
            record = snapshot()
            print(json.dumps({"rows": [row.to_json() for row in record.rows],
                              "pending_retire": list(record.pending_retire), "inputs": list(record.inputs)}, sort_keys=True))
            return 0
        rows = _all_rows()
        paths = _paths(rows)
        parent = None
        if arguments.under is not None:
            member = _lookup(rows, arguments.under)
            if member is None:
                raise StaffNotFound(StaffId(arguments.under))
            parent = member.id
        selected = sorted((row for row in rows if (arguments.kind is None or row.kind == arguments.kind)
                           and (parent is None or row.parent == parent)),
                          key=lambda row: (paths[row.id], row.id))
        if arguments.json:
            print(json.dumps([row.to_json() for row in selected], sort_keys=True))
        else:
            for row in selected:
                _print_row(row, paths[row.id])
        return 0
    if arguments.verb == "get":
        rows = _all_rows()
        row = _lookup(rows, arguments.target)
        if row is None:
            raise StaffNotFound(StaffId(arguments.target))
        if arguments.json:
            print(json.dumps(row.to_json(), sort_keys=True))
        else:
            _print_row(row, _paths(rows)[row.id])
        return 0
    if arguments.verb == "retire" and (arguments.row_write or arguments.worker_row):
        row = by_id(StaffId(arguments.target))
        if arguments.row_write and row is not None and row.kind != "worker":
            raise StaffRefused("row writes are for worker admission and retirement")
        if arguments.worker_row:
            arguments.row_write = row is None or row.kind == "worker"
    if arguments.row_write and not (arguments.verb == "retire" or
                                    arguments.verb == "ensure" and arguments.kind == "worker"):
        raise StaffRefused("row writes are for worker admission and retirement")
    with operation() if _is_owner() and not arguments.row_write else nullcontext():
        result = _mutate(arguments)
    if isinstance(result, tuple):
        print(json.dumps(result))
        return 0
    if isinstance(result, StaffSnapshot):
        if arguments.json:
            print(json.dumps({"rows": [row.to_json() for row in result.rows],
                              "pending_retire": list(result.pending_retire)}, sort_keys=True))
        else:
            paths = _paths(list(result.rows))
            for row in sorted(result.rows, key=lambda row: paths[row.id]):
                _print_row(row, paths[row.id])
        return 0
    if arguments.json:
        print(json.dumps(None if result is None else result.to_json(), sort_keys=True))
    elif result is not None:
        print(f"{result.id}  {result.kind:<10}  {result.name}")
    return 0


def cli_result(action: Callable[[], int]) -> int:
    """The one exception-to-exit-code mapping for staff commands."""
    try:
        return action()
    except StaffNotFound as error:
        print(str(error), file=sys.stderr)
        return 1
    except (StaffRefused, StaffUnreadable) as error:
        # The transport distinguishes the two errors that share exit code 2.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    arguments = _Arguments()
    _ = _parser().parse_args(argv, namespace=arguments)
    return cli_result(lambda: _main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
