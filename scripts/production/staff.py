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
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal, NewType, cast
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import durable  # noqa: E402
from scripts.shutdown import remote  # noqa: E402

OWNER_MACHINE = "natedev"
StaffId = NewType("StaffId", str)
Kind = Literal["showrunner", "unit", "worker"]
_KINDS: tuple[Kind, ...] = ("showrunner", "unit", "worker")
_ID = re.compile(r"s[a-z0-9]{11}")


class StaffUnreadable(OSError):
    """The staff record could not be read or safely written."""


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


def _file() -> Path:
    return Path(os.environ.get("STAFF_FILE") or Path.home() / ".local/state/staff/staff.json").expanduser()


def _is_owner() -> bool:
    return remote.other_machine() != OWNER_MACHINE


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


def _load(*, initial: bool = False) -> list[StaffRow]:
    try:
        text = _file().read_text(encoding="utf-8")
    except FileNotFoundError as error:
        if initial:
            return []
        raise StaffUnreadable(f"cannot read staff record: {error}") from error
    except (OSError, UnicodeError) as error:
        raise StaffUnreadable(f"cannot read staff record: {error}") from error
    value = _decode(text)
    if not isinstance(value, dict):
        raise StaffUnreadable("staff record must be an object")
    fields = cast(dict[str, object], value)
    schema = fields.get("schema")
    if type(schema) is not int or schema != 1:
        raise StaffUnreadable(f"unsupported staff schema: {schema!r}")
    return _rows(fields.get("rows"))


def _remote(arguments: list[str]) -> object:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", OWNER_MACHINE,
               "~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py "
               + shlex.join([*arguments, "--json"])]
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
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
    return _load() if _is_owner() else _rows(_remote(["show"]))


def _row_by_id(rows: list[StaffRow], staff_id: StaffId) -> StaffRow | None:
    return next((row for row in rows if row.id == staff_id), None)


def _row_by_path(rows: list[StaffRow], names: str) -> StaffRow | None:
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
    return _row_by_id(_all_rows(), staff_id)


def by_path(names: str) -> StaffRow | None:
    return _row_by_path(_all_rows(), names)


def find(*, kind: Kind | None = None, parent: StaffId | None = None) -> list[StaffRow]:
    """Find members; an omitted kind or parent leaves that field unrestricted."""
    return [row for row in _all_rows() if (kind is None or row.kind == kind)
            and (parent is None or row.parent == parent)]


def _store(rows: list[StaffRow]) -> None:
    content = json.dumps({"schema": 1, "rows": [row.to_json() for row in rows]},
                         indent=2, sort_keys=True) + "\n"
    durable.write_text(_file(), content)


def _transaction(edit: Callable[[list[StaffRow]], StaffRow | None]) -> StaffRow | None:
    path = _file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for temporary in path.parent.glob(f".{path.name}.*.tmp"):
                temporary.unlink()
            original = _load(initial=True)
            proposed = list(original)
            result = edit(proposed)
            _validate(proposed)
            proposed.sort(key=lambda row: row.id)
            if proposed == original:
                # A prior replace may have succeeded before its folder sync failed.
                durable.sync_folder(path.parent)
                print("unchanged", file=sys.stderr)
            else:
                _store(proposed)
            return result
    except OSError as error:
        if isinstance(error, StaffUnreadable):
            raise
        raise StaffUnreadable(f"cannot write staff record: {error}") from error


def _required(rows: list[StaffRow], staff_id: StaffId) -> StaffRow:
    row = _row_by_id(rows, staff_id)
    if row is None:
        raise StaffNotFound(staff_id)
    return row


def ensure(kind: Kind, *, name: str, parent: StaffId | None = None) -> StaffRow:
    if not _is_owner():
        arguments = ["ensure", kind, f"--name={name}"]
        if parent is not None:
            arguments.extend(("--parent", parent))
        return _remote_row(arguments)

    def edit(rows: list[StaffRow]) -> StaffRow:
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

    result = _transaction(edit)
    assert result is not None
    return result


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

    result = _transaction(edit)
    assert result is not None
    return result


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

    result = _transaction(edit)
    assert result is not None
    return result


def retire(staff_id: StaffId) -> StaffRow | None:
    if not _is_owner():
        value = _remote(["retire", staff_id])
        return None if value is None else StaffRow.from_json(value)

    def edit(rows: list[StaffRow]) -> StaffRow | None:
        row = _row_by_id(rows, staff_id)
        if row is None:
            return None
        if row.kind == "showrunner" and any(child.parent == row.id and child.kind == "unit" for child in rows):
            raise StaffRefused(f"showrunner {row.name} still has units")
        kept: list[StaffRow] = []
        for member in rows:
            if member.id == staff_id or (row.kind == "unit" and member.parent == staff_id):
                continue
            kept.append(replace(member, parent=None) if member.parent == staff_id else member)
        rows[:] = kept
        return row

    return _transaction(edit)


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


class _Arguments(argparse.Namespace):
    verb: str = ""
    kind: Kind | None = None
    under: str | None = None
    target: str = ""
    name: str = ""
    parent: str | None = None
    json: bool = False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    show = verbs.add_parser("show", help="show the production staff")
    _ = show.add_argument("--kind", choices=_KINDS)
    _ = show.add_argument("--under", help="parent staff id or path")
    get = verbs.add_parser("get", help="find a member by id or path")
    _ = get.add_argument("target")
    add = verbs.add_parser("ensure", help=argparse.SUPPRESS)
    _ = add.add_argument("kind", choices=_KINDS)
    _ = add.add_argument("--name", required=True)
    _ = add.add_argument("--parent")
    rename_parser = verbs.add_parser("rename", help=argparse.SUPPRESS)
    _ = rename_parser.add_argument("target")
    _ = rename_parser.add_argument("--name", required=True)
    move_parser = verbs.add_parser("move", help=argparse.SUPPRESS)
    _ = move_parser.add_argument("target")
    _ = move_parser.add_argument("--parent", required=True)
    retire_parser = verbs.add_parser("retire", help=argparse.SUPPRESS)
    _ = retire_parser.add_argument("target")
    for verb in (show, get, add, rename_parser, move_parser, retire_parser):
        _ = verb.add_argument("--json", action="store_true")
    return parser


def _parent(value: str | None) -> StaffId | None:
    return None if value is None or value == "none" else StaffId(value)


def _main(arguments: _Arguments) -> int:
    if arguments.verb == "show":
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
                print(f"{row.id:<12}  {row.kind:<10}  {paths[row.id]}")
        return 0
    if arguments.verb == "get":
        result = _lookup(_all_rows(), arguments.target)
        if result is None:
            raise StaffNotFound(StaffId(arguments.target))
    elif arguments.verb == "ensure":
        assert arguments.kind is not None
        result = ensure(arguments.kind, name=arguments.name, parent=_parent(arguments.parent))
    elif arguments.verb == "rename":
        result = rename(StaffId(arguments.target), arguments.name)
    elif arguments.verb == "move":
        result = move(StaffId(arguments.target), _parent(arguments.parent))
    else:
        result = retire(StaffId(arguments.target))
    if arguments.json:
        print(json.dumps(None if result is None else result.to_json(), sort_keys=True))
    elif result is not None:
        print(f"{result.id}  {result.kind:<10}  {result.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _Arguments()
    _ = _parser().parse_args(argv, namespace=arguments)
    try:
        return _main(arguments)
    except StaffNotFound as error:
        print(str(error), file=sys.stderr)
        return 1
    except (StaffRefused, StaffUnreadable) as error:
        # The transport distinguishes the two errors that share exit code 2.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
