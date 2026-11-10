#!/usr/bin/env python3
"""The production staff record, stored once on natedev for both workstations."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping, Callable, Generator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, NewType, Protocol, Self, TypeVar, cast
from uuid import UUID

if __name__ == "__main__":
    # Lazy runtime readers share these exact row and exception classes in the CLI.
    sys.modules["scripts.production.staff"] = sys.modules[__name__]
# The status line imports this module on each refresh. Defer imports for other paths.
if TYPE_CHECKING:
    import argparse

OWNER_MACHINE = "natedev"
StaffId = NewType("StaffId", str)
Kind = Literal["showrunner", "unit", "worker", "agent"]
_KINDS: tuple[Kind, ...] = ("showrunner", "unit", "worker", "agent")
WorkerRole = Literal["impl", "test", "review", "fix", "astra-designer", "astra-reviewer"]
WORKER_ROLES: tuple[WorkerRole, ...] = ("impl", "test", "review", "fix", "astra-designer", "astra-reviewer")


@dataclass(frozen=True)
class KnownWorkerRole:
    value: WorkerRole


@dataclass(frozen=True)
class WorkerRoleUnknown:
    """No dispatch has recorded the worker role."""


@dataclass(frozen=True)
class SessionHome:
    path: Path


@dataclass(frozen=True)
class HomeUnknown:
    """No pane start has recorded the session home."""


@dataclass(frozen=True)
class ReportsTo:
    id: StaffId


@dataclass(frozen=True)
class StaffRoot:
    """A top showrunner has no parent."""


@dataclass(frozen=True)
class ClaudeBackend:
    """The worker runs in Claude."""
    family: ClassVar[str] = "claude"


@dataclass(frozen=True)
class CodexBackend:
    family: ClassVar[str] = "codex"
    codex_home: Path


@dataclass(frozen=True)
class BackendUnavailable:
    """No producer proved the worker backend."""


def _seconds(key: str) -> float:
    from scripts.lib import conf

    source = Path(os.environ["STAFF_CONFIG"]) if os.environ.get("STAFF_CONFIG") else "staff"
    return conf.read(source).number(key)


def _wait_seconds(key: str) -> float:
    from scripts.agents import worker_lifecycle

    remaining = worker_lifecycle.effective_deadline() - time.monotonic()
    if remaining <= 0:
        raise worker_lifecycle.OperationDeadlineExpired("staff operation deadline expired")
    return min(_seconds(key), remaining)


def native_id(value: str) -> StaffId:
    """Validate an identity supplied by Claude or Codex, without assigning one."""
    try:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical runtime id")
    except ValueError as error:
        raise StaffRefused(f"invalid native runtime id: {value!r}") from error
    return StaffId(value)


STAFF_ROOT = StaffRoot()
BACKEND_UNAVAILABLE = BackendUnavailable()


def _valid_id(value: str) -> bool:
    try:
        _ = native_id(value)
        return True
    except StaffRefused:
        # Transitional records are readable only so the one-time migration can run.
        return bool(re.fullmatch(r"s[a-z0-9]{11}", value))


def caller_identity() -> str:
    """Read the caller's exported session identity."""
    return os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def runtime_id(pid: int | None = None) -> str:
    """Read the exact Claude registry pid, or the caller's Codex runtime identity."""
    import subprocess
    from scripts.agents import worker_lifecycle
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
            process_parent = cast(Callable[[int], int], import_module("scripts.agents.pane_record").process_parent)
            current = process_parent(current)
        except worker_lifecycle.OperationDeadlineExpired:
            raise
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


def _unknown_fields(value: dict[str, object], known: set[str]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key not in known}


def _location(machine: str, path: Path) -> None:
    if not machine or machine != machine.strip() or not path.is_absolute():
        raise StaffRefused("a staff location requires a machine name and an absolute path")


@dataclass(frozen=True)
class PresentWorktree:
    machine: str
    path: Path
    unknown_fields: dict[str, object] = field(default_factory=dict, kw_only=True, compare=False)

    def __post_init__(self) -> None:
        _location(self.machine, self.path)


@dataclass(frozen=True)
class RemovedWorktree:
    machine: str
    path: Path
    unknown_fields: dict[str, object] = field(default_factory=dict, kw_only=True, compare=False)

    def __post_init__(self) -> None:
        _location(self.machine, self.path)


@dataclass(frozen=True)
class WorktreeUnfilled:
    """The transitional record has no worktree fact yet."""


def worktree_machine(row: StaffRow) -> str:
    if isinstance(row.worktree, (PresentWorktree, RemovedWorktree)):
        return row.worktree.machine
    return ""


@dataclass(frozen=True)
class LiveDoc:
    machine: str
    path: Path
    unknown_fields: dict[str, object] = field(default_factory=dict, kw_only=True, compare=False)

    def __post_init__(self) -> None:
        _location(self.machine, self.path)


@dataclass(frozen=True)
class WrappedDoc:
    machine: str
    path: Path
    unknown_fields: dict[str, object] = field(default_factory=dict, kw_only=True, compare=False)

    def __post_init__(self) -> None:
        _location(self.machine, self.path)


@dataclass(frozen=True)
class DocUnfilled:
    """The transitional record has no production document fact yet."""


def _keep_location_fields[Location: PresentWorktree | RemovedWorktree | LiveDoc | WrappedDoc](
    new: Location,
    current: PresentWorktree | RemovedWorktree | LiveDoc | WrappedDoc | WorktreeUnfilled | DocUnfilled,
) -> Location:
    if isinstance(current, (WorktreeUnfilled, DocUnfilled)):
        return new
    return replace(new, unknown_fields=current.unknown_fields)


WORKTREE_UNFILLED = WorktreeUnfilled()
DOC_UNFILLED = DocUnfilled()


@dataclass(frozen=True)
class Active:
    """The staff row has no hold record."""


@dataclass(frozen=True)
class HoldRecord:
    since: str
    by: str
    why: str
    unknown_fields: dict[str, object] = field(default_factory=dict, kw_only=True, compare=False)

    def to_json(self) -> dict[str, object]:
        return {**self.unknown_fields, "since": self.since, "by": self.by, "why": self.why}

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict):
            raise StaffUnreadable(f"{cls.__name__.lower()} must contain since, by and why")
        fields = cast(dict[str, object], value)
        required = {"since", "by", "why"}
        if not required <= fields.keys() or not all(isinstance(fields[key], str) for key in required):
            raise StaffUnreadable(f"{cls.__name__.lower()} must contain since, by and why as strings")
        return cls(cast(str, fields["since"]), cast(str, fields["by"]), cast(str, fields["why"]),
                   unknown_fields=_unknown_fields(fields, required))


@dataclass(frozen=True)
class Paused(HoldRecord):
    """The director holds at its prompt."""


@dataclass(frozen=True)
class Exited(HoldRecord):
    """The director and workers have an exit request."""


@dataclass(frozen=True)
class OpenRun:
    path: Path


@dataclass(frozen=True)
class NoRun:
    """The member has no open run."""


@dataclass(frozen=True)
class StaffRow:
    id: StaffId
    kind: Kind
    name: str
    parent: ReportsTo | StaffRoot
    current_session: StaffId = field(kw_only=True)

    worktree: PresentWorktree | RemovedWorktree | WorktreeUnfilled = field(default_factory=WorktreeUnfilled)
    doc: LiveDoc | WrappedDoc | DocUnfilled = field(default_factory=DocUnfilled)

    paused: Paused | Active = field(default_factory=Active)
    exited: Exited | Active = field(default_factory=Active)
    role: KnownWorkerRole | WorkerRoleUnknown = field(default_factory=WorkerRoleUnknown, kw_only=True)
    home: SessionHome | HomeUnknown = field(default_factory=HomeUnknown, kw_only=True)
    run: OpenRun | NoRun = field(default_factory=NoRun, kw_only=True)
    unknown_fields: dict[str, object] = field(default_factory=dict, kw_only=True, compare=False)

    def reports_to(self, identity: StaffId) -> bool:
        return self.parent == ReportsTo(identity)

    def to_json(self) -> dict[str, object]:
        value: dict[str, object] = {**self.unknown_fields, "id": self.id, "kind": self.kind, "name": self.name, "parent": self.parent.id if isinstance(self.parent, ReportsTo) else None, "current_session": self.current_session}
        if isinstance(self.role, KnownWorkerRole):
            value["role"] = self.role.value
        if isinstance(self.run, OpenRun):
            value["run"] = str(self.run.path)
        if isinstance(self.home, SessionHome):
            value["home"] = str(self.home.path)
        if not isinstance(self.worktree, WorktreeUnfilled):
            value["worktree"] = {**self.worktree.unknown_fields, "machine": self.worktree.machine, "path": str(self.worktree.path),
                                 "state": "present" if isinstance(self.worktree, PresentWorktree) else "removed"}
        if not isinstance(self.doc, DocUnfilled):
            value["doc"] = {**self.doc.unknown_fields, "machine": self.doc.machine, "path": str(self.doc.path),
                            "state": "live" if isinstance(self.doc, LiveDoc) else "wrapped"}
        if isinstance(self.paused, Paused):
            value["paused"] = self.paused.to_json()
        if isinstance(self.exited, Exited):
            value["exited"] = self.exited.to_json()
        return value

    @classmethod
    def from_json(cls, value: object) -> StaffRow:
        if not isinstance(value, dict):
            raise StaffUnreadable("staff row must be an object")
        fields = cast(dict[str, object], value)
        if not {"id", "kind", "name", "parent"} <= set(fields):
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
        run = fields.get("run")
        if "run" in fields and (not isinstance(run, str) or not run):
            raise StaffUnreadable("staff run must be a nonempty path string")
        role, home = fields.get("role"), fields.get("home")
        parsed_role = (KnownWorkerRole(role)
                       if isinstance(role, str) and role in WORKER_ROLES else WorkerRoleUnknown())
        parsed_home = SessionHome(Path(home)) if isinstance(home, str) and Path(home).is_absolute() else HomeUnknown()
        known_fields = {"id", "kind", "name", "parent", "current_session", "worktree", "doc", "paused", "exited", "run"}
        invalid: list[str] = []
        for key, fact in (("role", parsed_role), ("home", parsed_home)):
            if isinstance(fact, (KnownWorkerRole, SessionHome)):
                known_fields.add(key)
            elif key in fields:
                invalid.append(key)
        if invalid:
            print(f"anomaly: {staff_id}: unknown staff fields: {', '.join(invalid)}", file=sys.stderr)
        worktree, doc = _row_locations(kind, fields)
        return cls(StaffId(staff_id), kind, name,
                   ReportsTo(StaffId(parent)) if isinstance(parent, str) else StaffRoot(), worktree, doc, current_session=StaffId(current),
                   role=parsed_role, home=parsed_home,
                   run=OpenRun(Path(run)) if isinstance(run, str) else NoRun(),
                   paused=Paused.from_json(fields["paused"]) if "paused" in fields else Active(),
                   exited=Exited.from_json(fields["exited"]) if "exited" in fields else Active(),
                   unknown_fields=_unknown_fields(fields, known_fields))


def _row_locations(kind: str, fields: dict[str, object]) -> tuple[PresentWorktree | RemovedWorktree | WorktreeUnfilled, LiveDoc | WrappedDoc | DocUnfilled]:
    worktree: PresentWorktree | RemovedWorktree | WorktreeUnfilled = WorktreeUnfilled()
    doc: LiveDoc | WrappedDoc | DocUnfilled = DocUnfilled()
    for key in ("worktree", "doc"):
        if key not in fields:
            continue
        if kind not in ("agent", "unit" if key == "worktree" else "showrunner"):
            raise StaffUnreadable(f"{kind} cannot hold {key}")
        raw = fields[key]
        if not isinstance(raw, dict):
            raise StaffUnreadable(f"{key} must be an object")
        location = cast(dict[str, object], raw)
        required = {"machine", "path", "state"}
        if not required <= location.keys():
            raise StaffUnreadable(f"{key} requires machine, path and state")
        machine, path, state = (location.get(item) for item in ("machine", "path", "state"))
        if not isinstance(machine, str) or not isinstance(path, str):
            raise StaffUnreadable(f"invalid {key} location")
        unknown_fields = _unknown_fields(location, required)
        try:
            if key == "worktree" and state in ("present", "removed"):
                worktree = PresentWorktree(machine, Path(path), unknown_fields=unknown_fields) if state == "present" else RemovedWorktree(machine, Path(path), unknown_fields=unknown_fields)
            elif key == "doc" and state in ("live", "wrapped"):
                doc = LiveDoc(machine, Path(path), unknown_fields=unknown_fields) if state == "live" else WrappedDoc(machine, Path(path), unknown_fields=unknown_fields)
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
    """The document has no checkout metadata."""


@dataclass(frozen=True)
class UnreadableGitCheckout:
    """Checkout metadata cannot identify the document safely."""

    path: Path
    reason: str


def _document_checkout(path: Path) -> CheckoutDocument | OutsideGitCheckout | UnreadableGitCheckout:
    """Read document identity from Git metadata without starting a process."""
    metadata = path
    try:
        document = path.resolve()
        for folder in document.parents:
            metadata = folder / ".git"
            try:
                mode = metadata.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode):
                git_directory = metadata
            elif stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                if metadata.is_dir():
                    git_directory = metadata
                else:
                    directive = metadata.read_text(encoding="utf-8").strip()
                    if not directive.startswith("gitdir:"):
                        raise ValueError("expected a gitdir: line")
                    location = directive.removeprefix("gitdir:").strip()
                    if not location:
                        raise ValueError("empty gitdir path")
                    git_directory = folder / location
            else:
                raise ValueError("expected a Git metadata file or directory")
            metadata = git_directory
            git_directory = git_directory.resolve(strict=True)
            if not stat.S_ISDIR(git_directory.stat().st_mode):
                raise ValueError("Git directory is not a directory")
            metadata = git_directory / "commondir"
            try:
                _ = metadata.lstat()
            except FileNotFoundError:
                common_directory = git_directory
            else:
                location = metadata.read_text(encoding="utf-8").strip()
                if not location:
                    raise ValueError("empty commondir path")
                common_directory = (git_directory / location).resolve(strict=True)
            metadata = common_directory
            if not stat.S_ISDIR(common_directory.stat().st_mode):
                raise ValueError("Git common directory is not a directory")
            return CheckoutDocument(common_directory, document.relative_to(folder))
    except (OSError, UnicodeError, RuntimeError, ValueError) as error:
        return UnreadableGitCheckout(metadata, str(error))
    return OutsideGitCheckout()


@dataclass(frozen=True)
class StaffSnapshot:
    rows: tuple[StaffRow, ...]
    pending_retire: tuple[StaffId, ...] = ()
    inputs: tuple[str, ...] = field(default=(), compare=False)
    schema: int = field(default=2, compare=False)
    current_sessions_recorded: bool = field(default=True, compare=False)
    # True only when snapshot() reads the production record.
    production: bool = field(default=False, compare=False)
    # The session ids a caller already read. None reads the session registry at use.
    running_sessions: frozenset[str] | None = field(default=None, compare=False)

    @cached_property
    def backends(self) -> Mapping[StaffId, ClaudeBackend | CodexBackend]:
        return _found_backends(self.rows, self.running_sessions)

    @cached_property
    def rows_by_id(self) -> dict[StaffId, StaffRow]:
        return {row.id: row for row in self.rows}

    def by_id(self, staff_id: StaffId) -> StaffRow | None:
        return self.rows_by_id.get(staff_id)

    def parent_of(self, row: StaffRow) -> StaffRow | None:
        return self.by_id(row.parent.id) if isinstance(row.parent, ReportsTo) else None

    def owner_of(self, reference: str) -> StaffRow | None:
        """Return the member, or the parent unit when the member is a worker."""
        row = self.by_member_reference(reference)
        return self.parent_of(row) if row is not None and row.kind == "worker" else row

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

    def unit_by_worktree(self, machine: str, path: Path) -> StaffRow | None:
        """A path that a live unit holds belongs to that unit; the newest row wins within each state."""
        matches = [row for row in reversed(self.rows) if row.kind == "unit"
                   and isinstance(row.worktree, (PresentWorktree, RemovedWorktree))
                   and row.worktree.machine == machine and row.worktree.path == path]
        return next((row for row in matches if isinstance(row.worktree, PresentWorktree)),
                    next(iter(matches), None))

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
        if isinstance(identity, UnreadableGitCheckout):
            raise StaffRefused(f"{document}: cannot read Git metadata {identity.path}: {identity.reason}")
        matches: list[StaffRow] = []
        for row in candidates:
            if not isinstance(row.doc, (LiveDoc, WrappedDoc)):
                continue
            candidate = _document_checkout(row.doc.path)
            if isinstance(candidate, UnreadableGitCheckout):
                continue
            if candidate == identity:
                matches.append(row)
        if len(matches) > 1:
            names = ", ".join(str(row.id) for row in matches)
            raise StaffRefused(f"{document}: ambiguous production document; staff rows: {names}")
        return next(iter(matches), None)

    def find(self, *, kind: Kind | None = None, parent: StaffId | None = None) -> list[StaffRow]:
        """An omitted kind or parent leaves that field unrestricted."""
        return [row for row in self.rows if (kind is None or row.kind == kind)
                and (parent is None or row.parent == ReportsTo(parent))]


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
            entry = _seed_fields(cast(object, item), keys | (set(raw) & {"runtime_id"}))
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


def _default_file() -> Path:
    return Path.home() / ".local/state/staff/staff.json"


def _file_override() -> str:
    return os.environ.get("STAFF_FILE", "")
def record_path() -> Path:
    return _file()


def _file() -> Path:
    return Path(_file_override() or _default_file()).expanduser()


def folder_move_path(unit: StaffId) -> Path:
    """Keep move progress beside the selected staff record."""
    _ = native_id(unit)
    return _file().parent / "folder_moves" / f"{unit}.json"


def is_owner() -> bool:
    """An explicit STAFF_FILE is local state owned by this process."""
    from scripts.agents import remote

    return bool(_file_override()) or remote.other_machine() != OWNER_MACHINE


def child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove row lock proofs, staff hints, and operation state from agent environments."""
    from scripts.lib.models_registry import agent_environment

    return {key: value for key, value in agent_environment(environment).items()
            if not key.startswith("STAFF_ROW_FD_") and key != "STAFF_ID"}


def operation_fds() -> tuple[int, ...]:
    """Descriptors a nested tool must preserve with subprocess pass_fds."""
    result: list[int] = []
    if _joined_operation(_file().parent / "operation.lock"):
        result.append(int(os.environ["STAFF_OPERATION_FD"]))
    for variable in os.environ:
        if variable.startswith("STAFF_ROW_FD_"):
            path, variable = _row_lock(variable.removeprefix("STAFF_ROW_FD_"))
            if _joined_operation(path, variable):
                result.append(int(os.environ[variable]))
    return tuple(result)


def _row_lock(key: str) -> tuple[Path, str]:
    return _file().parent / "rows" / f"{key}.lock", "STAFF_ROW_FD_" + key


@contextmanager
def row_operation(reference: str) -> Generator[None]:
    """Lock one staff row across lifecycle commands and inherited children."""
    if _joined_operation(*_row_lock(reference)):
        yield
        return
    record = initial_snapshot()
    row = record.by_member_reference(reference)
    if row is None:
        matches = [item for item in record.rows if item.name == reference]
        row = matches[0] if len(matches) == 1 else None
    if row is None:
        yield
        return
    with slot_operation(str(row.id), row.name):
        yield


@contextmanager
def slot_operation(key: str, name: str) -> Generator[None]:
    """Lock a row or a named worker slot across inherited children."""
    path, variable = _row_lock(key)
    if _joined_operation(path, variable):
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StaffRefused(f"{name}: another operation holds this row") from error
        previous = os.environ.get(variable)
        os.environ[variable] = str(lock.fileno())
        try:
            yield
        finally:
            if previous is None:
                _ = os.environ.pop(variable, None)
            else:
                os.environ[variable] = previous


def _joined_operation(path: Path, variable: str = "STAFF_OPERATION_FD") -> bool:
    try:
        descriptor = int(os.environ[variable])
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
    from scripts.agents import worker_lifecycle

    limit = _wait_seconds("OPERATION_LOCK_SECONDS")
    started = time.monotonic()
    try:
        worker_lifecycle.acquire_flock(descriptor, deadline=started + limit)
    except worker_lifecycle.OperationDeadlineExpired as error:
        raise worker_lifecycle.OperationDeadlineExpired(
            f"staff operation lock {path}: refused after {time.monotonic() - started:.2f}s; "
            + f"{_operation_lock_holder(path)}; limit {limit:.2f}s") from error


def operation() -> AbstractContextManager[None]:
    """Order source changes, joining only a proven inherited operation lock."""
    from scripts.agents import worker_lifecycle

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
            except worker_lifecycle.OperationDeadlineExpired:
                raise
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
    return () if kind == "agent" else ("unit", "showrunner") if kind == "worker" else ("showrunner",)


def validate_name(kind: Kind, name: str) -> None:
    """Check a staff name before a caller writes its operation journal."""
    if not name or _valid_id(name) or name != name.strip() or "/" in name or any(
        ord(character) < 32 or ord(character) == 127 for character in name
    ):
        raise StaffRefused(f"invalid short staff name: {name!r}")
    if kind == "worker":
        _ = WorkerName.parse(name)


def _validate(rows: list[StaffRow], unknown_rows: Sequence[dict[str, object]] = ()) -> None:
    """Check the complete proposed graph before any bytes are replaced."""
    indexed: dict[StaffId, StaffRow] = {}
    unknown = {cast(str, row["id"]) for row in unknown_rows}
    sessions = {cast(str, row.get("current_session", row["id"])) for row in unknown_rows}
    names: set[tuple[Kind, ReportsTo | StaffRoot | None, str]] = set()
    worktrees: set[tuple[str, Path]] = set()
    for row in rows:
        if row.id in indexed or row.id in unknown:
            raise StaffRefused(f"duplicate staff id: {row.id}")
        indexed[row.id] = row
        if not _valid_id(row.current_session):
            raise StaffRefused(f"{row.id}: invalid current session")
        if row.current_session in sessions:
            raise StaffRefused(f"duplicate current session: {row.current_session}")
        sessions.add(row.current_session)
        if row.kind not in ("unit", "agent") and not isinstance(row.worktree, WorktreeUnfilled):
            raise StaffRefused(f"{row.kind} cannot hold worktree")
        if row.kind not in ("showrunner", "agent") and not isinstance(row.doc, DocUnfilled):
            raise StaffRefused(f"{row.kind} cannot hold doc")
        if isinstance(row.worktree, PresentWorktree):
            location = (row.worktree.machine, row.worktree.path)
            if location in worktrees:
                raise StaffRefused(f"duplicate present worktree: {location[0]}:{location[1]}")
            worktrees.add(location)
        if row.kind not in _KINDS:
            raise StaffRefused(f"invalid staff kind: {row.kind}")
        validate_name(row.kind, row.name)
        key = (row.kind, None if row.kind == "showrunner" else row.parent, row.name)
        if key in names:
            raise StaffRefused(f"{row.kind} name already exists: {row.name}")
        names.add(key)
    for row in rows:
        if isinstance(row.parent, StaffRoot):
            if row.kind not in ("showrunner", "agent"):
                raise StaffRefused(f"{row.kind} {row.name} requires a parent")
        elif row.parent.id not in unknown:
            parent = indexed.get(row.parent.id)
            allowed = _parent_kinds(row.kind)
            if parent is None or parent.kind not in allowed:
                raise StaffRefused(f"{row.kind} {row.name} requires a {' or '.join(allowed)} parent: {row.parent.id}")
    # Once a chain is checked, every later chain reaching it is already known to end.
    checked: set[StaffId] = set()
    for row in rows:
        chain: set[StaffId] = set()
        current = row
        while current.id not in checked:
            if current.id in chain:
                raise StaffRefused(f"staff parent loop at {current.name}")
            chain.add(current.id)
            if isinstance(current.parent, StaffRoot) or current.parent.id in unknown:
                break
            current = indexed[current.parent.id]
        checked.update(chain)


def _decode(text: str) -> object:
    try:
        return cast(object, json.loads(text))
    except (ValueError, RecursionError) as error:
        raise StaffUnreadable(f"invalid staff JSON: {error}") from error


def _rows(value: object) -> list[StaffRow]:
    if not isinstance(value, list):
        raise StaffUnreadable("staff rows must be a list")
    rows: list[StaffRow] = []
    unknown: list[dict[str, object]] = []
    for item in cast(list[object], value):
        fields = cast(dict[str, object], item) if isinstance(item, dict) else dict[str, object]()
        if isinstance(fields.get("kind"), str) and fields["kind"] not in _KINDS:
            unknown.append(fields)
            print(f"staff anomaly: row {fields.get('id')}: unknown kind {fields['kind']}; skipped", file=sys.stderr)
            continue
        rows.append(StaffRow.from_json(cast(object, item)))
    try:
        _validate(rows, unknown)
    except StaffRefused as error:
        raise StaffUnreadable(str(error)) from error
    return sorted(rows, key=lambda row: row.id)


def _local_fields() -> dict[str, object]:
    """Read the local record without constructing every staff row."""
    try:
        value = _decode(_file().read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise StaffAbsent(f"cannot read staff record: {error}") from error
    except (OSError, UnicodeError) as error:
        raise StaffUnreadable(f"cannot read staff record {_file()}: {error}") from error
    if not isinstance(value, dict):
        raise StaffUnreadable("staff record must be an object")
    fields = cast(dict[str, object], value)
    schema = fields.get("schema")
    if type(schema) is not int or schema not in (1, 2):
        raise StaffUnreadable(f"unsupported staff schema: {schema!r}")
    return fields


def _load(*, initial: bool = False) -> StaffSnapshot:
    try:
        fields = _local_fields()
    except StaffAbsent:
        if initial:
            return StaffSnapshot(())
        raise
    return replace(_snapshot_value(fields), inputs=(str(_file().absolute()),))


def local_by_current_session(session_id: str) -> StaffRow | None:
    """Read one local row for a hook without constructing the other rows."""
    rows = _local_fields().get("rows")
    if not isinstance(rows, list):
        raise StaffUnreadable("staff rows must be a list")
    for value in cast(list[object], rows):
        if isinstance(value, dict):
            fields = cast(dict[str, object], value)
            if fields.get("current_session", fields.get("id")) == session_id:
                return StaffRow.from_json(fields)
    return None


def backend(row: StaffRow) -> ClaudeBackend | CodexBackend | BackendUnavailable:
    """Find the backend for this row's current session."""
    return _found_backends((row,)).get(row.id, BACKEND_UNAVAILABLE)


def _found_backends(rows: Sequence[StaffRow],
                    running: frozenset[str] | None = None) -> dict[StaffId, ClaudeBackend | CodexBackend]:
    """Find worker sessions with one scan of each home."""
    from scripts.message import sessions

    accounts = import_module("scripts.whoami.agent_accounts")
    observe_home = cast(Callable[[], object], accounts.observe_workers_home)
    find_homes = cast(Callable[[object], list[Path]], accounts.codex_homes)
    homes = find_homes(observe_home())
    if os.environ.get("CODEX_HOME"):
        homes.insert(0, Path(os.environ["CODEX_HOME"]))
    wanted = {row.current_session: row.id for row in rows if row.kind == "worker"}
    found: dict[StaffId, ClaudeBackend | CodexBackend] = {}
    for home in dict.fromkeys(homes):
        try:
            for folder in ("sessions", "archived_sessions"):
                for path in (home / folder).rglob("*.jsonl"):
                    identity = wanted.get(StaffId(path.stem[-36:]))
                    if identity is not None:
                        _ = found.setdefault(identity, CodexBackend(home))
        except OSError as error:
            print(f"anomaly: {home}: {error}", file=sys.stderr)
    if running is None:
        running = (frozenset(record["sessionId"] for record in sessions.running_sessions())
                   if any(identity not in found for identity in wanted.values()) else frozenset())
    for current, identity in wanted.items():
        if identity not in found and (current in running or bool(sessions.transcript_locations(current))):
            found[identity] = ClaudeBackend()
    return found


def _snapshot_value(fields: dict[str, object]) -> StaffSnapshot:
    pending = fields.get("pending_retire", [])
    if not isinstance(pending, list) or any(not isinstance(item, str) or not _valid_id(item)
                                             for item in cast(list[object], pending)):
        raise StaffUnreadable("invalid pending retirement ids")
    inputs = fields.get("inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(path, str) or not Path(path).is_absolute()
                                         for path in cast(list[object], inputs)):
        raise StaffUnreadable("invalid staff input paths")
    rows = _rows(fields.get("rows"))
    return StaffSnapshot(tuple(rows),
                         tuple(sorted({StaffId(item) for item in cast(list[str], pending)})),
                         tuple(cast(list[str], inputs)), cast(int, fields.get("schema", 2)),
                         all("current_session" in item for item in cast(list[dict[str, object]], fields.get("rows", []))))


def _remote(arguments: list[str], *, input_text: str | None = None) -> object:
    # Direct worker writers run inside the caller's membership lock. Match the
    # local API's row transaction without entering the outer lifecycle operation.
    import shlex
    import subprocess
    from scripts.agents import worker_lifecycle

    if arguments[:2] == ["ensure", "worker"] or arguments[0] in ("update-current-session", "ensure-worker"):
        arguments = [*arguments, "--row-write"]
    elif arguments[0] == "retire":
        arguments = [*arguments, "--worker-row"]
    timeout = _wait_seconds("REMOTE_SECONDS")
    command = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout))}", OWNER_MACHINE,
               "~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py "
               + shlex.join([*arguments, "--json"])]
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL if input_text is None else None,
                              input=input_text, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        if time.monotonic() >= worker_lifecycle.effective_deadline():
            raise worker_lifecycle.OperationDeadlineExpired("staff remote deadline expired") from error
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
    return list(_load().rows) if is_owner() else _rows(_remote(["show"]))


def snapshot() -> StaffSnapshot:
    if is_owner():
        return replace(_load(), production=not _file_override())
    value = _remote(["show", "--snapshot"])
    if not isinstance(value, dict):
        raise StaffUnreadable("staff snapshot must be an object")
    return replace(_snapshot_value(cast(dict[str, object], value)), production=True)


def local_snapshot() -> StaffSnapshot:
    """Read only this machine's retained record; never contact its remote owner."""
    return _load()


def wait_for_writes() -> None:
    """Read the local record after pending staff transactions finish."""
    from scripts.agents import worker_lifecycle

    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a", encoding="utf-8") as lock:
        worker_lifecycle.acquire_flock(lock, mode=fcntl.LOCK_SH)


def initial_snapshot() -> StaffSnapshot:
    """Read a writer's starting record, including an installation not yet seeded."""
    try:
        return replace(snapshot(), production=False)
    except StaffAbsent:
        return StaffSnapshot(())


def _row_by_id(rows: Sequence[StaffRow], staff_id: StaffId) -> StaffRow | None:
    return next((row for row in rows if row.id == staff_id), None)


def _row_by_path(rows: Sequence[StaffRow], names: str) -> StaffRow | None:
    parts = names.split("/")
    if not all(parts) or len(parts) > len(_KINDS):
        return None
    # A root kind starts its own path.
    roots = [row for row in rows if row.kind in ("showrunner", "agent") and row.name == parts[0]]
    row = roots[0] if len(roots) == 1 else None
    for name in parts[1:]:
        if row is None:
            return None
        parent = row.id
        matches = [member for member in rows
                   if member.kind != "showrunner" and member.parent == ReportsTo(parent) and member.name == name]
        row = matches[0] if len(matches) == 1 else None
    return row


def _lookup(rows: list[StaffRow], target: str) -> StaffRow | None:
    from scripts.agents import remote

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


def _under_test_root(path: Path) -> bool:
    """Identify temporary locations before a write to the real staff record."""
    import tempfile

    resolved = path.expanduser().resolve()
    return any(resolved.is_relative_to(root.resolve())
               for root in (Path(tempfile.gettempdir()), Path("/tmp"), Path("/dev/shm")))


def _location_paths(row: StaffRow) -> tuple[Path, ...]:
    return tuple(location.path for location in (row.doc, row.worktree)
                 if isinstance(location, (LiveDoc, WrappedDoc, PresentWorktree, RemovedWorktree)))


def _store(rows: list[StaffRow], pending: set[StaffId]) -> None:
    from scripts.lib import atomic_file

    retained = cast(dict[str, object], _decode(_file().read_text(encoding="utf-8"))) if _file().exists() else {}
    unknown = [row for row in cast(list[dict[str, object]], retained.get("rows", [])) if row["kind"] not in _KINDS]
    _validate(rows, unknown)
    document: dict[str, object] = {"schema": 2, "rows": [row.to_json() for row in rows] + unknown}
    if pending:
        document["pending_retire"] = sorted(pending)
    atomic_file.write_text_atomic(_file(), json.dumps(document, indent=2, sort_keys=True) + "\n")
    _copy_to_mac()


def _copy_to_mac(*, detached: bool = True) -> None:
    """Send the latest default record under one lock, outside the writing process."""
    import subprocess
    from scripts.agents import remote

    try:
        if "STAFF_FILE" in os.environ or remote.local_machine() != OWNER_MACHINE:
            return
        if detached:
            code = "from scripts.production import staff; staff._copy_to_mac(detached=False)"
            # A retained stderr pipe would make captured CLI callers wait for SSH.
            with _default_file().with_name("mac-copy.log").open("a", encoding="utf-8") as errors:
                _ = subprocess.Popen(
                    [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", code], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True,
                )
            return
        import signal

        path = _default_file()
        # Keep this lock through SSH so an older send cannot follow a newer send.
        with path.with_name("mac-copy.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            text = path.read_text(encoding="utf-8")
            command = '''set -e
directory="$HOME/.local/state/staff"
mkdir -p "$directory"
temporary=$(mktemp "$directory/staff.json.mac-copy.XXXXXXXX")
trap 'rm -f "$temporary"' EXIT
cat > "$temporary"
mv -f "$temporary" "$directory/staff.json"
'''
            def stop(_number: int, _frame: object) -> None:
                raise InterruptedError("copier terminated")

            # Defer SIGTERM until the SSH child has a cleanup handler.
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
            previous_handler = signal.signal(signal.SIGTERM, stop)
            try:
                with subprocess.Popen(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                     remote.other_machine(), command],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ) as process:
                    try:
                        _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
                        _, errors = process.communicate(input=text, timeout=remote.STANDARD_TIME_LIMIT["seconds"])
                    except BaseException:
                        _ = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
                        process.terminate()
                        _ = process.wait()
                        raise
                    if process.returncode:
                        reason = " ".join(errors.split()) or f"ssh exited {process.returncode}"
                        raise OSError(reason)
            finally:
                _ = signal.signal(signal.SIGTERM, previous_handler)
                _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"staff: Mac copy failed: {' '.join(str(error).split())}", file=sys.stderr)


def _record_transaction(edit: Callable[[list[StaffRow], set[StaffId]], _Result]) -> _Result:
    from scripts.lib import atomic_file
    from scripts.agents import worker_lifecycle

    path = _file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a", encoding="utf-8") as lock:
            worker_lifecycle.acquire_flock(lock)
            for temporary in path.parent.glob(f".{path.name}.*"):
                temporary.unlink()
            original = _load(initial=True)
            proposed = list(original.rows)
            pending = set(original.pending_retire)
            result = edit(proposed, pending)
            if path.resolve() == _default_file().resolve():
                previous = {row.id: row for row in original.rows}
                for row in proposed:
                    if previous.get(row.id) != row:
                        for location in _location_paths(row):
                            if _under_test_root(location):
                                raise StaffRefused(f"test-root path cannot enter the real staff record: {location}")
                        # The fixture backend uses UUID version 5; native backends use versions 4 and 7.
                        if any(identity[14:15] == "5" for identity in (row.id, row.current_session)):
                            raise StaffRefused("fixture runtime id cannot enter the real staff record")
            proposed.sort(key=lambda row: row.id)
            if original.schema == 2 and original.current_sessions_recorded and proposed == list(original.rows) and pending == set(original.pending_retire):
                # A prior replace may have succeeded before its folder sync failed.
                atomic_file.sync_folder(path.parent)
                print("unchanged", file=sys.stderr)
            else:
                _store(proposed, pending)
            return result
    except worker_lifecycle.OperationDeadlineExpired:
        raise
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


def _ensure_row(rows: list[StaffRow], kind: Kind, name: str, parent: ReportsTo | StaffRoot, *,
                worktree: PresentWorktree | RemovedWorktree | WorktreeUnfilled = WORKTREE_UNFILLED,
                doc: LiveDoc | WrappedDoc | DocUnfilled = DOC_UNFILLED,
                runtime_id: str = "") -> StaffRow:
    if kind not in ("unit", "agent") and not isinstance(worktree, WorktreeUnfilled):
        raise StaffRefused(f"{kind} cannot hold worktree")
    if kind not in ("showrunner", "agent") and not isinstance(doc, DocUnfilled):
        raise StaffRefused(f"{kind} cannot hold doc")
    existing = next((row for row in rows
                     if (row.kind, row.name, row.parent) == (kind, name, parent)), None)
    if existing is not None:
        if not isinstance(worktree, WorktreeUnfilled) and not isinstance(existing.worktree, WorktreeUnfilled) and existing.worktree != worktree:
            raise StaffRefused(f"{existing.id}: existing worktree conflicts with admission")
        if not isinstance(doc, DocUnfilled) and not isinstance(existing.doc, DocUnfilled) and existing.doc != doc:
            raise StaffRefused(f"{existing.id}: existing doc conflicts with admission")
        admitted = replace(existing, current_session=native_id(runtime_id) if runtime_id else existing.current_session,
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


def ensure(kind: Kind, *, name: str, parent: ReportsTo | StaffRoot = STAFF_ROOT,
           worktree: PresentWorktree | RemovedWorktree | WorktreeUnfilled = WORKTREE_UNFILLED,
           doc: LiveDoc | WrappedDoc | DocUnfilled = DOC_UNFILLED,
                runtime_id: str = "") -> StaffRow:
    """Admit the identity and its location in the same owner transaction."""
    if not is_owner():
        arguments = ["ensure", kind, f"--name={name}", f"--runtime-id={runtime_id}"]
        if isinstance(parent, ReportsTo):
            arguments.extend(("--parent", parent.id))
        if not isinstance(worktree, WorktreeUnfilled):
            arguments.extend(("--worktree", worktree.machine, str(worktree.path),
                              "present" if isinstance(worktree, PresentWorktree) else "removed"))
        if not isinstance(doc, DocUnfilled):
            arguments.extend(("--doc", doc.machine, str(doc.path), "live" if isinstance(doc, LiveDoc) else "wrapped"))
        return _remote_row(arguments)

    return _transaction(lambda rows: _ensure_row(rows, kind, name, parent, worktree=worktree, doc=doc, runtime_id=runtime_id))


def ensure_worker(identity: StaffId, name: str, parent: StaffId) -> StaffRow:
    """Publish worker membership before dispatch."""
    identity = native_id(identity)
    if not is_owner():
        return _remote_row(["ensure-worker", identity, "--name", name, "--parent", parent])
    return _transaction(lambda rows: _ensure_row(rows, "worker", name, ReportsTo(parent), runtime_id=identity))


def set_worker_role(identity: StaffId, role: KnownWorkerRole) -> StaffRow:
    """Record the role of the worker's last launch or dispatch."""
    if not is_owner():
        return _remote_row(["set-worker-role", identity, role.value])
    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, identity)
        changed = replace(row, role=role)
        rows[rows.index(row)] = changed
        return changed
    return _transaction(edit)


def set_home(identity: StaffId, home: SessionHome) -> StaffRow:
    """Record the home proved by a pane start."""
    if not is_owner():
        return _remote_row(["set-home", identity, str(home.path)])
    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, identity)
        changed = replace(row, home=home)
        rows[rows.index(row)] = changed
        return changed
    return _transaction(edit)


def set_run(identity: StaffId, run: OpenRun) -> StaffRow:
    """Record the member's open run through the row transaction."""
    if not is_owner():
        row = by_id(identity)
        if row is None:
            raise StaffNotFound(identity)
        return _remote_row(["set-run", row.current_session, str(run.path)])
    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, identity)
        changed = replace(row, run=run)
        rows[rows.index(row)] = changed
        return changed
    return _transaction(edit)


def clear_run(identity: StaffId, run: Path) -> StaffRow:
    """Clear the pointer only while the member still names this run."""
    if not is_owner():
        row = by_id(identity)
        if row is None:
            raise StaffNotFound(identity)
        return _remote_row(["clear-run", row.current_session, str(run)])
    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, identity)
        if not isinstance(row.run, OpenRun) or row.run.path.resolve() != run.resolve():
            return row
        changed = replace(row, run=NoRun())
        rows[rows.index(row)] = changed
        return changed
    return _transaction(edit)


def update_current_session(old_session: StaffId, new_session: StaffId) -> StaffRow:
    """Publish a proved runtime handoff without changing the member or its links."""
    _ = native_id(new_session)
    if not is_owner():
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
    with row_operation(old_session):
        return _transaction(edit)


@dataclass(frozen=True)
class ShowrunnerRole:
    name: str
    parent: StaffId
    doc: LiveDoc


@dataclass(frozen=True)
class UnitRole:
    name: str
    parent: StaffId
    worktree: PresentWorktree


def change_director(identity: StaffId, destination: ShowrunnerRole | UnitRole) -> StaffRow:
    """Change a director's role and preserve its identity and workers."""
    promoting = isinstance(destination, ShowrunnerRole)
    location = destination.doc if isinstance(destination, ShowrunnerRole) else destination.worktree
    if not is_owner():
        return _remote_row(["promote-unit" if promoting else "demote-showrunner", identity,
                            "--name", destination.name, "--parent", destination.parent,
                            "--machine", location.machine, "--path", str(location.path)])
    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, identity)
        kind: Kind = "showrunner" if promoting else "unit"
        source = "unit" if promoting else "showrunner"
        changed = replace(row, kind=kind, name=destination.name, parent=ReportsTo(destination.parent),
                          worktree=WORKTREE_UNFILLED if promoting else cast(UnitRole, destination).worktree,
                          doc=cast(ShowrunnerRole, destination).doc if promoting else DOC_UNFILLED)
        if row.kind == kind:
            if row != changed:
                raise StaffRefused(f"{identity}: director destination conflicts")
            return row
        if row.kind != source:
            raise StaffRefused(f"{identity}: role change requires a {source}")
        rows[rows.index(row)] = changed
        if not promoting:
            # A unit cannot own other directors. Its workers retain their parent.
            rows[:] = [replace(child, parent=changed.parent)
                       if child.parent == ReportsTo(identity) and child.kind in {"unit", "showrunner"}
                       else child for child in rows]
        return changed
    return _transaction(edit)


def promote_unit(identity: StaffId, *, name: str, parent: StaffId, doc: LiveDoc) -> StaffRow:
    """Promote the same session through the shared director writer."""
    return change_director(identity, ShowrunnerRole(name, parent, doc))


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
                           parent=ReportsTo(identities.get(row.parent.id, row.parent.id)) if isinstance(row.parent, ReportsTo) else StaffRoot())
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
        return StaffSnapshot(tuple(sorted(rows, key=lambda row: row.id)),
                             tuple(sorted(pending)))
    return _record_transaction(edit)


def restore_unit(staff_id: StaffId, worktree: PresentWorktree) -> StaffRow:
    """Reactivate the retained unit in place, preserving its identity and parent."""
    if not is_owner():
        return _remote_row(["restore-unit", staff_id, "--machine", worktree.machine, "--path", str(worktree.path)])

    def edit(rows: list[StaffRow], pending: set[StaffId]) -> StaffRow:
        existing = _required(rows, staff_id)
        if existing.kind != "unit":
            raise StaffRefused("restore-unit requires a unit")
        if isinstance(existing.worktree, WorktreeUnfilled):
            raise StaffRefused(f"{existing.name}: staff worktree is unfilled")
        if (existing.worktree.machine, existing.worktree.path) != (worktree.machine, worktree.path):
            raise StaffRefused(f"{existing.name}: restore worktree conflicts with retained location")
        pending.discard(staff_id)
        restored = replace(existing, worktree=_keep_location_fields(worktree, existing.worktree))
        rows[rows.index(existing)] = restored
        return restored

    return _record_transaction(edit)


def rename(staff_id: StaffId, new_name: str) -> StaffRow:
    if not is_owner():
        renamed = _remote_row(["rename", staff_id, f"--name={new_name}"])
    else:
        def edit(rows: list[StaffRow]) -> StaffRow:
            row = _required(rows, staff_id)
            renamed = replace(row, name=new_name)
            rows[rows.index(row)] = renamed
            return renamed

        renamed = _transaction(edit)
    if renamed.kind == "worker":
        herdr = import_module("scripts.herdr.herdr")
        client = cast(Callable[[str], object], herdr.Herdr)
        shared_session = cast(Callable[[], str], herdr.shared_session)
        display_failure = cast(type[Exception], herdr.HerdrFailure)

        try:
            retitle = cast(Callable[[str], bool], getattr(client(shared_session()), "retitle_row"))
            _ = retitle(renamed.id)
        except (OSError, ValueError, display_failure) as error:
            print(f"anomaly: worker display: {error}", file=sys.stderr)
    return renamed


def move(staff_id: StaffId, new_parent: ReportsTo | StaffRoot) -> StaffRow:
    if not is_owner():
        return _remote_row(["move", staff_id, f"--parent={new_parent.id if isinstance(new_parent, ReportsTo) else 'none'}"])

    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, staff_id)
        moved = replace(row, parent=new_parent)
        rows[rows.index(row)] = moved
        return moved

    return _transaction(edit)


def set_hold(reference: str, hold: Paused | Exited | Active) -> StaffRow:
    """Write a lifecycle state through the staff owner."""
    if not is_owner():
        state = "paused" if isinstance(hold, Paused) else "exited" if isinstance(hold, Exited) else "active"
        return _remote_row(["set-hold", reference, "--state", state,
                            "--record", json.dumps(hold.to_json() if isinstance(hold, HoldRecord) else {})])

    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _lookup(rows, reference)
        if row is None:
            matches = [member for member in rows if member.kind == "unit" and member.name == reference]
            row = matches[0] if len(matches) == 1 else None
        if row is None:
            raise StaffNotFound(StaffId(reference))
        if isinstance(hold, Paused):
            changed = replace(row, paused=row.paused if isinstance(row.paused, Paused) else hold, exited=Active())
        elif isinstance(hold, Exited):
            changed = replace(row, exited=row.exited if isinstance(row.exited, Exited) else hold, paused=Active())
        else:
            changed = replace(row, paused=Active(), exited=Active())
        rows[rows.index(row)] = changed
        return changed

    return _transaction(edit)


def _hold(reference: str, why: str, state: type[Paused] | type[Exited]) -> StaffRow:
    from scripts.lib.user_zone import user_zone

    caller = by_current_session(runtime_id())
    return set_hold(reference, state(datetime.now(user_zone()).isoformat(), caller.name if caller else "", why))


def pause(reference: str, why: str) -> StaffRow:
    """Record the director's pause request."""
    return _hold(reference, why, Paused)


def exit(reference: str, why: str) -> StaffRow:
    """Record the director's exit request."""
    return _hold(reference, why, Exited)


def resume(reference: str) -> StaffRow:
    """Remove the pause or exit record."""
    return set_hold(reference, Active())


def set_worktree(unit: StaffId, worktree: PresentWorktree | RemovedWorktree, *,
                 expected: PresentWorktree | RemovedWorktree | WorktreeUnfilled) -> StaffRow:
    """Set one unit's worktree through the existing owner transaction."""
    if not is_owner():
        previous = (["--expected-state", "unfilled"] if isinstance(expected, WorktreeUnfilled) else
                    ["--expected-state", "present" if isinstance(expected, PresentWorktree) else "removed",
                     "--expected-machine", expected.machine, "--expected-path", str(expected.path)])
        return _remote_row(["set-worktree", unit, "--machine", worktree.machine, "--path", str(worktree.path),
                            "--state", "present" if isinstance(worktree, PresentWorktree) else "removed", *previous])

    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, unit)
        if row.kind not in ("unit", "agent"):
            raise StaffRefused("set-worktree requires a unit or agent")
        if row.worktree == worktree:
            return row
        if row.worktree != expected:
            raise StaffRefused(f"{unit}: worktree differs from the expected value {expected!r}; found {row.worktree!r}")
        changed = replace(row, worktree=_keep_location_fields(worktree, row.worktree))
        rows[rows.index(row)] = changed
        return changed

    return _transaction(edit)


def set_doc(showrunner: StaffId, doc: LiveDoc | WrappedDoc) -> StaffRow:
    """Set one showrunner's document through the existing owner transaction."""
    if not is_owner():
        return _remote_row(["set-doc", showrunner, "--machine", doc.machine, "--path", str(doc.path),
                            "--state", "live" if isinstance(doc, LiveDoc) else "wrapped"])

    def edit(rows: list[StaffRow]) -> StaffRow:
        row = _required(rows, showrunner)
        if row.kind not in ("showrunner", "agent"):
            raise StaffRefused("set-doc requires a showrunner or agent")
        changed = replace(row, doc=_keep_location_fields(doc, row.doc))
        rows[rows.index(row)] = changed
        return changed

    return _transaction(edit)


def require_stopped(identity: StaffId) -> None:
    """Removal cannot erase a live native session or uncertain process evidence."""
    record = snapshot()
    target = record.by_id(identity)
    if target is None:
        return
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
    if not is_owner():
        value = _remote(["retire", staff_id])
        return None if value is None else StaffRow.from_json(value)

    require_stopped(staff_id)
    return _transaction(lambda rows: _retire_row(rows, staff_id))


@dataclass(frozen=True)
class FixtureRemoved:
    """The stopped fixture tree no longer exists in the staff record."""

    row: StaffRow


@dataclass(frozen=True)
class FixtureAlreadyAbsent:
    """The staff record already has no selected fixture."""


def _remove_test_fixture(staff_id: StaffId) -> FixtureRemoved | FixtureAlreadyAbsent:  # pyright: ignore[reportUnusedFunction]
    """Remove a stopped fixture tree whose locations name test folders."""
    if not is_owner():
        raise StaffRefused("remove the fixture on the staff owner machine")
    record = snapshot()
    row = record.by_id(staff_id)
    if row is None:
        return FixtureAlreadyAbsent()
    selected = {row.id: row}
    while True:
        children = {candidate.id: candidate for candidate in record.rows
                    if isinstance(candidate.parent, ReportsTo) and candidate.parent.id in selected and candidate.id not in selected}
        if not children:
            break
        selected.update(children)
    from scripts.message import sessions

    for candidate in selected.values():
        locations = _location_paths(candidate)
        if not locations or not all(_under_test_root(path) for path in locations):
            raise StaffRefused(f"{candidate.id}: the location is not under a test root")
        observed = sessions.session_liveness(candidate.current_session)
        if not isinstance(observed, sessions.SessionNotLive):
            raise StaffRefused(f"{candidate.id}: the fixture conversation is live or unreadable")

    def edit(rows: list[StaffRow]) -> FixtureRemoved:
        if any(_row_by_id(rows, identity) != expected for identity, expected in selected.items()):
            raise StaffRefused(f"{staff_id}: the fixture changed before removal")
        if any(isinstance(candidate.parent, ReportsTo) and candidate.parent.id in selected and candidate.id not in selected for candidate in rows):
            raise StaffRefused(f"{staff_id}: the fixture gained a member before removal")
        rows[:] = [candidate for candidate in rows if candidate.id not in selected]
        return FixtureRemoved(row)

    return _transaction(edit)


def _retire_row(rows: list[StaffRow], staff_id: StaffId) -> StaffRow | None:
    row = _row_by_id(rows, staff_id)
    if row is None:
        return None
    if row.kind not in ("worker", "agent"):
        raise StaffRefused("retire requires a worker or agent; retain units and showrunners with their location state")
    rows.remove(row)
    return row


def _retain_retired(rows: list[StaffRow], row: StaffRow) -> bool:
    """Finish one unheld retirement without deleting a row or changing an edge."""
    if any(child.kind == "worker" and child.parent == ReportsTo(row.id) for child in rows):
        return False
    if row.kind == "unit":
        if isinstance(row.worktree, WorktreeUnfilled):
            print(f"staff: keeping {row.id}: worktree is unfilled", file=sys.stderr)
            return False
        changed = replace(row, worktree=_keep_location_fields(
            RemovedWorktree(row.worktree.machine, row.worktree.path), row.worktree))
    elif row.kind == "showrunner":
        if any(child.kind == "unit" and child.parent == ReportsTo(row.id)
               and not isinstance(child.worktree, RemovedWorktree) for child in rows):
            return False
        if isinstance(row.doc, DocUnfilled):
            print(f"staff: keeping {row.id}: doc is unfilled", file=sys.stderr)
            return False
        changed = replace(row, doc=_keep_location_fields(WrappedDoc(row.doc.machine, row.doc.path), row.doc))
    else:
        raise StaffRefused("pending retirement is for units and showrunners")
    rows[rows.index(row)] = changed
    return True


def defer_retire(staff_ids: Sequence[StaffId]) -> None:
    """Remember members kept alive after their source records disappear."""
    if not is_owner():
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
    if not is_owner():
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
    if not is_owner():
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
            elif entry.kind in ("showrunner", "agent"):
                row = next((row for row in rows if row.kind == entry.kind and row.name == entry.name), None)
                if row is None:
                    row = _ensure_row(rows, entry.kind, entry.name, StaffRoot(), runtime_id=entry.runtime_id)
            else:
                if not isinstance(entry.parent, SeedParent):
                    raise StaffRefused(f"a new seed {entry.kind} requires its parent")
                parent = resolve(entry.parent.key)
                allowed = _parent_kinds(entry.kind)
                if parent.kind not in allowed:
                    raise StaffRefused(f"a seed {entry.kind} requires a {' or '.join(allowed)} parent")
                row = _ensure_row(rows, entry.kind, entry.name, ReportsTo(parent.id), runtime_id=entry.runtime_id)
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
            parent = ReportsTo(resolve(entry.parent.key).id) if isinstance(entry.parent, SeedParent) else StaffRoot()
            rows[rows.index(row)] = replace(row, parent=parent)
        retained = {row.id for row in resolved.values()}
        for retirement in seed_input.retire:
            staff_id = retirement.staff_id
            row = _row_by_id(rows, staff_id)
            if row is None:
                continue
            if row.kind != "unit" or staff_id in retained:
                raise StaffRefused(f"seed can retire only an absent unit: {staff_id}")
            if row.parent != ReportsTo(retirement.parent):
                print(f"seed: keeping unit {row.name}: parent changed", file=sys.stderr)
            elif any(child.kind == "worker" and child.parent == ReportsTo(staff_id) for child in rows):
                print(f"seed: keeping unit {row.name}: worker rows remain", file=sys.stderr)
                pending.add(staff_id)
            elif not _retain_retired(rows, row):
                pending.add(staff_id)
        return StaffSnapshot(tuple(sorted(rows, key=lambda row: row.id)),
                             tuple(sorted(pending)))

    return _record_transaction(edit)


def _paths(rows: list[StaffRow]) -> dict[StaffId, str]:
    indexed = {row.id: row for row in rows}
    paths: dict[StaffId, str] = {}
    for row in rows:
        chain: list[str] = [row.name]
        member = row
        while (member.kind not in ("showrunner", "agent") and isinstance(member.parent, ReportsTo)
               and member.parent.id in indexed):
            member = indexed[member.parent.id]
            chain.append(member.name)
        paths[row.id] = "/".join(reversed(chain))
    return paths


class _MoveCommand(Protocol):
    def __call__(self, target: str, new_showrunner: str, *, json_output: bool = False) -> int: ...


class _Arguments:
    hold_record: str = ""
    runtime_id: str = ""
    new_id: str = ""
    value: str = ""
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
    expected_state: str = ""
    expected_machine: str = ""
    expected_path: str = ""
    worktree: list[str] | None = None
    doc: list[str] | None = None


def _parser() -> argparse.ArgumentParser:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    for command in ("set-worker-role", "set-home"):
        fact = verbs.add_parser(command, help=argparse.SUPPRESS)
        _ = fact.add_argument("target")
        _ = fact.add_argument("value", choices=WORKER_ROLES if command == "set-worker-role" else None)
        _ = fact.add_argument("--json", action="store_true")
    for command in ("set-run", "clear-run"):
        run_parser = verbs.add_parser(command)
        _ = run_parser.add_argument("target")
        _ = run_parser.add_argument("path")
        _ = run_parser.add_argument("--json", action="store_true")
    hold = verbs.add_parser("set-hold", help=argparse.SUPPRESS)
    _ = hold.add_argument("target")
    _ = hold.add_argument("--state", choices=("paused", "exited", "active"), required=True)
    _ = hold.add_argument("--record", dest="hold_record", default="{}")
    _ = hold.add_argument("--json", action="store_true")
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
    worker_parser = verbs.add_parser("ensure-worker", help=argparse.SUPPRESS)
    _ = worker_parser.add_argument("target")
    _ = worker_parser.add_argument("--name", required=True)
    _ = worker_parser.add_argument("--parent", required=True)
    _ = worker_parser.add_argument("--json", action="store_true")
    _ = worker_parser.add_argument("--row-write", action="store_true", help=argparse.SUPPRESS)
    session_parser = verbs.add_parser("update-current-session", help=argparse.SUPPRESS)
    _ = session_parser.add_argument("target")
    _ = session_parser.add_argument("new_id")
    _ = session_parser.add_argument("--json", action="store_true")
    _ = session_parser.add_argument("--row-write", action="store_true", help=argparse.SUPPRESS)
    _ = add.add_argument("--runtime-id", default="")
    for command in ("promote-unit", "demote-showrunner"):
        director = verbs.add_parser(command, help=argparse.SUPPRESS)
        _ = director.add_argument("target")
        _ = director.add_argument("--name", required=True)
        _ = director.add_argument("--parent", required=True)
        _ = director.add_argument("--machine", required=True)
        _ = director.add_argument("--path", required=True)
        _ = director.add_argument("--json", action="store_true")
    restore_parser = verbs.add_parser("restore-unit", help=argparse.SUPPRESS)
    _ = restore_parser.add_argument("target")
    _ = restore_parser.add_argument("--machine", required=True)
    _ = restore_parser.add_argument("--path", required=True)
    rename_parser = verbs.add_parser("rename", help=argparse.SUPPRESS)
    _ = rename_parser.add_argument("target")
    _ = rename_parser.add_argument("--name", required=True)
    move_parser = verbs.add_parser("move", help="move a unit and its live tab to another showrunner")
    _ = move_parser.add_argument("target", help="<showrunner production_name>/<unit>")
    _ = move_parser.add_argument("destination", nargs="?", help="new showrunner production_name")
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
        if command == "set-worktree":
            _ = location_parser.add_argument("--expected-state", required=True, choices=("unfilled", "present", "removed"))
            _ = location_parser.add_argument("--expected-machine", default="")
            _ = location_parser.add_argument("--expected-path", default="")
        location_parsers.append(location_parser)
    for verb in (*location_parsers, show, get, add, restore_parser, rename_parser, move_parser, retire_parser, apply_parser, defer_parser, pending_parser):
        _ = verb.add_argument("--json", action="store_true")
    return parser


def _parent(value: str | None) -> ReportsTo | StaffRoot:
    return StaffRoot() if value is None or value == "none" else ReportsTo(StaffId(value))


def _print_row(row: StaffRow, path: str) -> None:
    print(f"{row.id:<12}  {row.kind:<10}  {path}")


def _mutate(arguments: _Arguments) -> StaffRow | StaffSnapshot | tuple[StaffId, ...] | None:
    if arguments.verb == "set-worker-role":
        return set_worker_role(StaffId(arguments.target), KnownWorkerRole(cast(WorkerRole, arguments.value)))
    if arguments.verb in ("set-run", "clear-run"):
        row = by_current_session(arguments.target)
        if row is None:
            raise StaffNotFound(StaffId(arguments.target))
        return (set_run(row.id, OpenRun(Path(arguments.path))) if arguments.verb == "set-run"
                else clear_run(row.id, Path(arguments.path)))
    if arguments.verb == "set-home":
        return set_home(StaffId(arguments.target), SessionHome(Path(arguments.value).absolute()))
    if arguments.verb == "set-hold":
        hold = (Paused.from_json(_decode(arguments.hold_record)) if arguments.state == "paused" else
                Exited.from_json(_decode(arguments.hold_record)) if arguments.state == "exited" else Active())
        return set_hold(arguments.target, hold)
    if arguments.verb == "ensure-worker":
        assert arguments.parent is not None
        return ensure_worker(native_id(arguments.target), arguments.name, StaffId(arguments.parent))
    if arguments.verb == "update-current-session":
        return update_current_session(StaffId(arguments.target), native_id(arguments.new_id))
    if arguments.verb == "set-worktree":
        worktree = (PresentWorktree(arguments.machine, Path(arguments.path)) if arguments.state == "present"
                    else RemovedWorktree(arguments.machine, Path(arguments.path)))
        expected = (WorktreeUnfilled() if arguments.expected_state == "unfilled" else
                    PresentWorktree(arguments.expected_machine, Path(arguments.expected_path))
                    if arguments.expected_state == "present" else
                    RemovedWorktree(arguments.expected_machine, Path(arguments.expected_path)))
        return set_worktree(StaffId(arguments.target), worktree, expected=expected)
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
    if arguments.verb == "demote-showrunner":
        assert arguments.parent is not None
        return change_director(StaffId(arguments.target), UnitRole(arguments.name, StaffId(arguments.parent),
                               PresentWorktree(arguments.machine, Path(arguments.path))))
    if arguments.verb == "restore-unit":
        return restore_unit(StaffId(arguments.target), PresentWorktree(arguments.machine, Path(arguments.path)))
    if arguments.verb == "rename":
        return rename(StaffId(arguments.target), arguments.name)
    if arguments.verb == "move":
        return move(StaffId(arguments.target), _parent(arguments.parent))
    if arguments.verb == "apply-seed":
        return apply_seed(SeedInput.from_json(_decode(sys.stdin.read())))
    if arguments.row_write and is_owner():
        require_stopped(StaffId(arguments.target))
        return _transaction(lambda rows: _retire_row(rows, StaffId(arguments.target)))
    return retire(StaffId(arguments.target))


def _main(arguments: _Arguments) -> int:
    if arguments.verb == "move":
        if arguments.destination is not None:
            if arguments.parent is not None:
                raise StaffRefused("move takes a showrunner production_name or --parent, not both")
            move_command = cast(_MoveCommand, import_module("scripts.production.staff_move").cli_move)
            return move_command(arguments.target, arguments.destination, json_output=arguments.json)
        if arguments.parent is None:
            raise StaffRefused("move needs <showrunner production_name>/<unit> <new showrunner production_name>")
    if arguments.verb == "show":
        if arguments.live:
            if not arguments.json or arguments.snapshot or arguments.kind or arguments.under:
                raise StaffRefused("live staff requires show --json --live without filters")
            live_command = cast(Callable[[], int], import_module("scripts.production.staff_sources").cli_live)
            return live_command()
        if arguments.snapshot:
            record = snapshot()
            print(json.dumps({"rows": [row.to_json() for row in record.rows],
                              "schema": record.schema, "pending_retire": list(record.pending_retire), "inputs": list(record.inputs)}, sort_keys=True))
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
                           and (parent is None or row.parent == ReportsTo(parent))),
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
    if arguments.row_write and not (arguments.verb in ("retire", "ensure-worker", "update-current-session") or
                                    arguments.verb == "ensure" and arguments.kind == "worker"):
        raise StaffRefused("row writes are for worker admission and retirement")
    with operation() if is_owner() and not arguments.row_write and arguments.verb not in ("set-worker-role", "set-home", "set-run", "clear-run") else nullcontext():
        result = _mutate(arguments)
    if isinstance(result, tuple):
        print(json.dumps(result))
        return 0
    if isinstance(result, StaffSnapshot):
        if arguments.json:
            print(json.dumps({"rows": [row.to_json() for row in result.rows],
                              "schema": result.schema, "pending_retire": list(result.pending_retire)}, sort_keys=True))
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
    from scripts.agents import worker_lifecycle

    try:
        return action()
    except StaffNotFound as error:
        print(str(error), file=sys.stderr)
        return 1
    except (StaffRefused, StaffUnreadable, worker_lifecycle.OperationDeadlineExpired) as error:
        # The transport distinguishes the two errors that share exit code 2.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    arguments = _Arguments()
    _ = _parser().parse_args(argv, namespace=arguments)
    return cli_result(lambda: _main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
