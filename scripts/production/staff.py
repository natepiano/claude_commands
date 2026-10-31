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
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import Literal, NewType, Protocol, TypeVar, cast
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import atomic_file  # noqa: E402
from scripts.production import unit_names  # noqa: E402
from scripts.shutdown import remote  # noqa: E402

OWNER_MACHINE = "natedev"
OPERATION_LOCK_TIMEOUT_SECONDS = 30.0
StaffId = NewType("StaffId", str)
Kind = Literal["showrunner", "unit", "worker"]
_KINDS: tuple[Kind, ...] = ("showrunner", "unit", "worker")
_ID = re.compile(r"s[a-z0-9]{11}")
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


class WorkerRole(StrEnum):
    impl = "impl"
    test = "test"
    fix = "fix"
    review = "review"
    designer = "designer"
    reviewer = "reviewer"


@dataclass(frozen=True)
class WorkerName:
    role: WorkerRole
    number: int

    @classmethod
    def parse(cls, name: str) -> WorkerName:
        match = re.fullmatch(r"([a-z]+)([1-9][0-9]*)", name)
        if match is not None:
            try:
                return cls(WorkerRole(match[1]), int(match[2]))
            except ValueError:
                pass
        raise StaffRefused(f"invalid worker name: {name!r}; expected a worker role and positive number")


@dataclass(frozen=True)
class StaffRow:
    id: StaffId
    kind: Kind
    name: str
    parent: StaffId | None

    def to_json(self) -> dict[str, str | None]:
        return {"id": self.id, "kind": self.kind, "name": self.name, "parent": self.parent}

    @classmethod
    def from_json(cls, value: object) -> StaffRow:
        if not isinstance(value, dict):
            raise StaffUnreadable("staff row must be an object")
        fields = cast(dict[str, object], value)
        if set(fields) != {"id", "kind", "name", "parent"}:
            raise StaffUnreadable("staff row must contain id, kind, name and parent")
        staff_id, kind, name, parent = (fields[key] for key in ("id", "kind", "name", "parent"))
        if not isinstance(staff_id, str) or _ID.fullmatch(staff_id) is None:
            raise StaffUnreadable("invalid staff id")
        if not isinstance(kind, str) or kind not in _KINDS:
            raise StaffUnreadable("invalid staff kind")
        if not isinstance(name, str):
            raise StaffUnreadable("staff name must be a string")
        if parent is not None and (not isinstance(parent, str) or _ID.fullmatch(parent) is None):
            raise StaffUnreadable("invalid staff parent id")
        return cls(StaffId(staff_id), kind, name,
                   StaffId(parent) if isinstance(parent, str) else None)


@dataclass(frozen=True)
class StaffSnapshot:
    rows: tuple[StaffRow, ...]
    pending_retire: tuple[StaffId, ...] = ()

    def by_id(self, staff_id: StaffId) -> StaffRow | None:
        return _row_by_id(self.rows, staff_id)

    def by_path(self, names: str) -> StaffRow | None:
        return _row_by_path(self.rows, names)

    def find(self, *, kind: Kind | None = None, parent: StaffId | None = None) -> list[StaffRow]:
        """An omitted kind or parent leaves that field unrestricted."""
        return [row for row in self.rows if (kind is None or row.kind == kind)
                and (parent is None or row.parent == parent)]


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
    kind: Literal["showrunner", "unit"]
    name: str
    match: StaffId | NewSeedMember
    parent: SeedParent | KeepSeedParent | RootSeedParent


def _seed_fields(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StaffRefused("seed input must contain objects")
    fields = cast(dict[str, object], value)
    if set(fields) != keys:
        raise StaffRefused(f"seed object requires {', '.join(sorted(keys))}")
    return fields


def _seed_id(value: object) -> StaffId:
    if not isinstance(value, str) or not _ID.fullmatch(value):
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
                            "parent": parent})
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
            entry = _seed_fields(item, {"key", "kind", "name", "match", "parent"})
            key, kind, name, match = (entry[field] for field in ("key", "kind", "name", "match"))
            if not isinstance(key, str) or not key or kind not in ("showrunner", "unit") or not isinstance(name, str):
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
                                      NewSeedMember() if match is None else _seed_id(match), parent))
        retire: list[SeedRetirement] = []
        for item in cast(list[object], raw_retire):
            entry = _seed_fields(item, {"id", "parent"})
            retire.append(SeedRetirement(_seed_id(entry["id"]), _seed_id(entry["parent"])))
        return cls(tuple(members), tuple(retire))


def _file() -> Path:
    return Path(os.environ.get("STAFF_FILE") or Path.home() / ".local/state/staff/staff.json").expanduser()


def _is_owner() -> bool:
    return remote.other_machine() != OWNER_MACHINE


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


def _validate(rows: list[StaffRow]) -> None:
    """Check the complete proposed graph before any bytes are replaced."""
    indexed: dict[StaffId, StaffRow] = {}
    names: set[tuple[Kind, StaffId | None, str]] = set()
    for row in rows:
        if row.id in indexed:
            raise StaffRefused(f"duplicate staff id: {row.id}")
        indexed[row.id] = row
        if row.kind not in _KINDS:
            raise StaffRefused(f"invalid staff kind: {row.kind}")
        if not row.name or _ID.fullmatch(row.name) or row.name != row.name.strip() or "/" in row.name or any(
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
            expected = "unit" if row.kind == "worker" else "showrunner"
            if parent is None or parent.kind != expected:
                raise StaffRefused(f"{row.kind} {row.name} requires a {expected} parent: {row.parent}")
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
    if type(schema) is not int or schema != 1:
        raise StaffUnreadable(f"unsupported staff schema: {schema!r}")
    return _snapshot_value(fields)


def _snapshot_value(fields: dict[str, object]) -> StaffSnapshot:
    pending = fields.get("pending_retire", [])
    if not isinstance(pending, list) or any(not isinstance(item, str) or not _ID.fullmatch(item)
                                             for item in cast(list[object], pending)):
        raise StaffUnreadable("invalid pending retirement ids")
    return StaffSnapshot(tuple(_rows(fields.get("rows"))),
                         tuple(sorted({StaffId(item) for item in cast(list[str], pending)})))


def _remote(arguments: list[str], *, input_text: str | None = None) -> object:
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
    for kind, name in zip(_KINDS[1:], parts[1:]):
        if row is None:
            return None
        parent = row.id
        row = next((row for row in rows if row.kind == kind and row.parent == parent and row.name == name), None)
    return row


def _lookup(rows: list[StaffRow], target: str) -> StaffRow | None:
    return _row_by_id(rows, StaffId(target)) or _row_by_path(rows, target)


def by_id(staff_id: StaffId) -> StaffRow | None:
    return snapshot().by_id(staff_id)


def by_path(names: str) -> StaffRow | None:
    return snapshot().by_path(names)


def find(*, kind: Kind | None = None, parent: StaffId | None = None) -> list[StaffRow]:
    """Find members; an omitted kind or parent leaves that field unrestricted."""
    return snapshot().find(kind=kind, parent=parent)


def unit_name_collisions(record: StaffSnapshot) -> dict[str, tuple[StaffRow, ...]]:
    """Discover legacy duplicate unit names without making the record unreadable."""
    grouped: dict[str, list[StaffRow]] = {}
    for row in record.find(kind="unit"):
        grouped.setdefault(unit_names.name_key(row.name), []).append(row)
    return {name: tuple(sorted(members, key=lambda row: row.id))
            for name, members in sorted(grouped.items()) if len(members) > 1}


def _store(rows: list[StaffRow], pending: set[StaffId]) -> None:
    document: dict[str, object] = {"schema": 1, "rows": [row.to_json() for row in rows]}
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
            original_units = {row.id: unit_names.name_key(row.name) for row in original.rows if row.kind == "unit"}
            for name, members in unit_name_collisions(StaffSnapshot(tuple(proposed))).items():
                if any(original_units.get(row.id) != name for row in members):
                    raise StaffRefused(f"unit name already exists: {name}")
            proposed.sort(key=lambda row: row.id)
            if proposed == list(original.rows) and pending == set(original.pending_retire):
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


def _ensure_row(rows: list[StaffRow], kind: Kind, name: str, parent: StaffId | None) -> StaffRow:
    existing = next((row for row in rows
                     if (row.kind, row.name, row.parent) == (kind, name, parent)), None)
    if existing is not None:
        return existing
    used = {row.id for row in rows}
    staff_id = StaffId("s" + uuid4().hex[:11])
    while staff_id in used:
        staff_id = StaffId("s" + uuid4().hex[:11])
    row = StaffRow(staff_id, kind, name, parent)
    rows.append(row)
    return row


def ensure(kind: Kind, *, name: str, parent: StaffId | None = None) -> StaffRow:
    if not _is_owner():
        arguments = ["ensure", kind, f"--name={name}"]
        if parent is not None:
            arguments.extend(("--parent", parent))
        return _remote_row(arguments)

    return _transaction(lambda rows: _ensure_row(rows, kind, name, parent))


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


def retire(staff_id: StaffId) -> StaffRow | None:
    if not _is_owner():
        value = _remote(["retire", staff_id])
        return None if value is None else StaffRow.from_json(value)

    return _transaction(lambda rows: _retire_row(rows, staff_id))


def _retire_row(rows: list[StaffRow], staff_id: StaffId) -> StaffRow | None:
    row = _row_by_id(rows, staff_id)
    if row is None:
        return None
    if row.kind == "showrunner" and any(child.parent == row.id and child.kind == "unit" for child in rows):
        raise StaffRefused(f"showrunner {row.name} still has units")
    rows[:] = [replace(member, parent=None) if member.parent == staff_id else member
               for member in rows if member.id != staff_id
               and not (row.kind == "unit" and member.parent == staff_id)]
    return row


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
        if not isinstance(value, list) or any(not isinstance(item, str) or not _ID.fullmatch(item)
                                               for item in cast(list[object], value)):
            raise StaffUnreadable("invalid pending retirement ids")
        return tuple(StaffId(item) for item in cast(list[str], value))

    def edit(rows: list[StaffRow], pending: set[StaffId]) -> tuple[StaffId, ...]:
        ordered = sorted(pending, key=lambda key: (0 if (row := _row_by_id(rows, key)) is None
                                                     or row.kind == "unit" else 1, key))
        for staff_id in ordered:
            row = _row_by_id(rows, staff_id)
            if row is not None:
                held_kind = "worker" if row.kind == "unit" else "unit"
                if any(child.parent == staff_id and child.kind == held_kind for child in rows):
                    continue
                _ = _retire_row(rows, staff_id)
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
            if entry.kind not in ("showrunner", "unit"):
                raise StaffRefused("seed cannot create workers")
            if not isinstance(entry.match, NewSeedMember):
                row = _required(rows, entry.match)
                if row.kind != entry.kind:
                    raise StaffRefused(f"seed member has the wrong kind: {row.id}")
            elif entry.kind == "showrunner":
                row = next((row for row in rows if row.kind == "showrunner" and row.name == entry.name), None)
                if row is None:
                    row = _ensure_row(rows, "showrunner", entry.name, None)
            else:
                if not isinstance(entry.parent, SeedParent):
                    raise StaffRefused("a new seed unit requires its showrunner")
                parent = resolve(entry.parent.key)
                if parent.kind != "showrunner":
                    raise StaffRefused("a seed unit requires a showrunner parent")
                row = _ensure_row(rows, "unit", entry.name, parent.id)
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
            else:
                rows.remove(row)
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
    verb: str = ""
    kind: Kind | None = None
    under: str | None = None
    target: str = ""
    destination: str | None = None
    name: str = ""
    parent: str | None = None
    json: bool = False
    snapshot: bool = False
    ids: list[str] = []


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    show = verbs.add_parser("show", help="show the production staff")
    _ = show.add_argument("--kind", choices=_KINDS)
    _ = show.add_argument("--under", help="parent staff id or path")
    _ = show.add_argument("--snapshot", action="store_true", help=argparse.SUPPRESS)
    collisions = verbs.add_parser("unit-name-collisions", help="discover duplicate unit short names without writing")
    _ = collisions.add_argument("--dry-run", action="store_true", required=True)
    get = verbs.add_parser("get", help="find a member by id or path")
    _ = get.add_argument("target")
    add = verbs.add_parser("ensure", help=argparse.SUPPRESS)
    _ = add.add_argument("kind", choices=_KINDS)
    _ = add.add_argument("--name", required=True)
    _ = add.add_argument("--parent")
    rename_parser = verbs.add_parser("rename", help=argparse.SUPPRESS)
    _ = rename_parser.add_argument("target")
    _ = rename_parser.add_argument("--name", required=True)
    move_parser = verbs.add_parser("move", help="move a unit and its live tab to another showrunner")
    _ = move_parser.add_argument("target", help="<showrunner slug>/<unit>")
    _ = move_parser.add_argument("destination", nargs="?", help="new showrunner slug")
    _ = move_parser.add_argument("--parent", help=argparse.SUPPRESS)
    retire_parser = verbs.add_parser("retire", help=argparse.SUPPRESS)
    _ = retire_parser.add_argument("target")
    apply_parser = verbs.add_parser("apply-seed", help=argparse.SUPPRESS)
    defer_parser = verbs.add_parser("defer-retire", help=argparse.SUPPRESS)
    _ = defer_parser.add_argument("ids", nargs="*")
    pending_parser = verbs.add_parser("retire-pending", help=argparse.SUPPRESS)
    for verb in (show, collisions, get, add, rename_parser, move_parser, retire_parser, apply_parser, defer_parser, pending_parser):
        _ = verb.add_argument("--json", action="store_true")
    return parser


def _parent(value: str | None) -> StaffId | None:
    return None if value is None or value == "none" else StaffId(value)


def _print_row(row: StaffRow, path: str) -> None:
    print(f"{row.id:<12}  {row.kind:<10}  {path}")


def _mutate(arguments: _Arguments) -> StaffRow | StaffSnapshot | tuple[StaffId, ...] | None:
    if arguments.verb == "defer-retire":
        return defer_retire(tuple(StaffId(value) for value in arguments.ids))
    if arguments.verb == "retire-pending":
        return retire_pending()
    if arguments.verb == "ensure":
        assert arguments.kind is not None
        return ensure(arguments.kind, name=arguments.name, parent=_parent(arguments.parent))
    if arguments.verb == "rename":
        return rename(StaffId(arguments.target), arguments.name)
    if arguments.verb == "move":
        return move(StaffId(arguments.target), _parent(arguments.parent))
    if arguments.verb == "apply-seed":
        return apply_seed(SeedInput.from_json(_decode(sys.stdin.read())))
    return retire(StaffId(arguments.target))


def _main(arguments: _Arguments) -> int:
    if arguments.verb == "unit-name-collisions":
        record = snapshot()
        collisions = unit_name_collisions(record)
        if arguments.json:
            print(json.dumps({name: [row.to_json() for row in members]
                              for name, members in collisions.items()}, sort_keys=True))
        else:
            paths = _paths(list(record.rows))
            for name, members in collisions.items():
                print(f"collision {name}: " + ", ".join(f"{paths[row.id]} ({row.id})" for row in members))
            if not collisions:
                print("no unit name collisions")
        return 0
    if arguments.verb == "move":
        if arguments.destination is not None:
            if arguments.parent is not None:
                raise StaffRefused("move takes a showrunner slug or --parent, not both")
            move_command = cast(_MoveCommand, import_module("scripts.production.staff_move").cli_move)
            return move_command(arguments.target, arguments.destination, json_output=arguments.json)
        if arguments.parent is None:
            raise StaffRefused("move needs <showrunner slug>/<unit> <new showrunner slug>")
    if arguments.verb == "show":
        if arguments.snapshot:
            record = snapshot()
            print(json.dumps({"rows": [row.to_json() for row in record.rows],
                              "pending_retire": list(record.pending_retire)}, sort_keys=True))
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
    with operation() if _is_owner() else nullcontext():
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
