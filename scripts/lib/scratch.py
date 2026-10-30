#!/usr/bin/env python3
"""Scratch folders that belong to a task, and go when the task ends.

Agents made loose folders in /tmp and ~/rust (repo copies, repros, test trees) and nothing removed
them. Sessions run for weeks, so a session end is no cleanup point (user, 2026-10-26/27). Each
scratch folder belongs to a task, and the thing that ends the task removes it:

    scratch.py new [--task <task> | --until <when>] [--name <word>] [--visible]   make one; prints its path
    scratch.py adopt <path> [--task <task> | --until <when>] [--name <word>] [--visible]   move a loose folder in
    scratch.py hold <path> [--task <task> | --until <when>] --reason <why>   keep a folder where it is
    scratch.py release <path>    drop a hold
    scratch.py renew <path>      mark it changed
    scratch.py hand-over <path> --to <unit> --task <task>  replace owner and task without moving it
    scratch.py end --task <task>  remove every folder and held folder of the task
    scratch.py list              each folder and hold, its task and its last change

A task, and what ends it:

  * `run:<run id>`: end_session.sh, at the run's end;
  * `phase:<unit>/<phase>`: merge_checkpoint.py, when the phase's checkpoint merges;
  * `gate:<id>`: the gate's caller, with `scratch.py end`, when the gate ends;
  * `landed:<unit>/<branch>`: the branch is in its recorded land target and all work is saved;
  * `row:<showrunner>-C<n>`: change_review.py verdict;
  * `until:<YYYY-MM-DDTHHMMZ>`: the hourly tmp sweep, once the time passes. `--until` takes
    `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` in the user's zone. With no task, a folder gets
    `until:` DEFAULT_LIFE_SECONDS (24 h) from now.

A folder lives in ROOT as `<task>-<word>-<6 chars>`; `--visible` (rows only) puts it in VISIBLE_ROOT
as `<showrunner>-C<n>-<word>`, where cargo-port lists it. Each holds RECORD with its task, maker and
times. The hourly tmp sweep (scripts/lint/tmp_sweep.py) keeps a folder in ROOT while its task is
open, and removes it when its until: time passes, or after 7 idle days whatever its task, since an
ender can fail. A row's folder waits for its verdict, and VISIBLE_ROOT is never swept. A git
worktree goes through `git worktree remove`, so its source repo keeps no stale entry. Removal
first writes `.scratch-removing-<folder>.json` beside the folder and deletes RECORD last, so a
stop at any line leaves the task findable and a rerun of `end` finishes the removal.

A hold keeps a folder that must stay where it is (a repo the user checks a change in). Its record
(task, maker, reason) is in the machine's HOLDS file, not in the folder, where a repo would show it
as uncommitted work. `end` removes a held folder with the task's other folders, unless a repo in
it holds work that is on no remote (unsaved_work): that hold stays, marked ended, for the folder
audit to name to its maker. The scratch guard hook
(scripts/hooks/pre-tool-use-scratch-guard.py) refuses a loose folder in /tmp, ~/rust and the
other roots.

Copies live at /tmp/claude/copies/<pair>/<copy>. Their records also name source and copy commits,
target mode, seed, borrowed git directory, owner and land target. Open phases and borrowed parents
stay; an unreadable record, phase state or land target needs repair. Shared .claude-busy locks
keep a copy while work runs or waits. Removal holds an exclusive lock through deletion.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.user_zone import user_zone  # noqa: E402

ROOT_ENV = "SCRATCH_ROOT"
VISIBLE_ROOT_ENV = "SCRATCH_VISIBLE_ROOT"
DEFAULT_ROOT = "/tmp/claude/scratch"
DEFAULT_VISIBLE_ROOT = "~/rust/repro"
RECORD = ".scratch.json"
# A started removal keeps the record outside the folder until the folder is gone.
REMOVAL_RECORD_PREFIX = ".scratch-removing-"
REMOVAL_RECORD_SUFFIX = ".json"
HOLDS_ENV = "SCRATCH_HOLDS"
DEFAULT_HOLDS = "~/.local/state/scratch/holds.json"
TASK = re.compile(r"(?P<kind>run|phase|row|gate|until|landed):(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_./-]+)?)")
UNTIL_FORMAT = "%Y-%m-%dT%H%MZ"
DEFAULT_LIFE_SECONDS = 24 * 3600
# A repo deeper than this inside a removed folder is not looked for; build output is never a repo.
REPO_DEPTH = 3
SKIPPED_FOLDERS = frozenset({"target", "node_modules", ".git"})
ROW_NAME = re.compile(r"[A-Za-z0-9_-]+-C[0-9]+")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
SUFFIX_LENGTH = 6
SUFFIX_CHARACTERS = string.ascii_lowercase + string.digits
SESSION_VARIABLES = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
BUSY = ".claude-busy"
COPIES_ENV = "SCRATCH_COPIES_ROOT"
RECORD_LOCKS: ContextVar[frozenset[tuple[int, int]]] = ContextVar("scratch_record_locks", default=frozenset())


@dataclass(frozen=True)
class Registered:
    task: str
    fields: dict[str, object]


@dataclass(frozen=True)
class NotRegistered:
    pass


@dataclass(frozen=True)
class UnreadableRecord:
    reason: str


@dataclass(frozen=True)
class StaleRemoval:
    """The certificate belongs to a previous copy at this path."""


@dataclass(frozen=True)
class Live:
    why: str


@dataclass(frozen=True)
class Ended:
    evidence: str
    at: float = 0.0


@dataclass(frozen=True)
class Unknown:
    reason: str


def copies_root() -> Path:
    configured = Path(os.environ.get(COPIES_ENV) or str(root().parent / "copies")).expanduser()
    return Path(os.path.realpath(configured))


def copy_folder(path: Path) -> Path | None:
    """The copy that contains path; convert the path boundary to no copy here."""
    base = copies_root()
    resolved = path.resolve()
    if resolved.is_relative_to(base):
        parts = resolved.relative_to(base).parts
        if len(parts) >= 2:
            return base / parts[0] / parts[1]
    return None


def atomic_record(path: Path, fields: object) -> None:
    # A killed writer may leave its stage file. Keep copy stages in Git metadata,
    # where they cannot look like unsaved user work on the next run.
    git = path.parent / ".git"
    staging = git if path.name == RECORD and git.is_dir() and not git.is_symlink() else path.parent
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=staging,
                                     prefix=f".{path.name}.", delete=False) as output:
        temporary = Path(output.name)
        try:
            _ = output.write(json.dumps(fields) + "\n")
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def exclude_copy_metadata(path: Path) -> None:
    if not (path / ".git").is_dir() or (path / ".git").is_symlink():
        return
    exclude = path / ".git/info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = exclude.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    patterns = [f"/{name}" for name in (RECORD, BUSY) if f"/{name}" not in text.splitlines()]
    if patterns:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=exclude.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                _ = output.write(text + ("\n" if text and not text.endswith("\n") else "") + "\n".join(patterns) + "\n")
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, exclude)
            finally:
                temporary.unlink(missing_ok=True)


def register_copy(path: Path, *, task: str, owner_unit: str, source_path: str, source_commit: str,
                  copy_commit: str, target_mode: str, seed_source: str, borrows_from: str,
                  land_target: str = "", run_dir: str = "", phase_id: str = "") -> None:
    if copy_folder(path) != path.resolve() or path.is_symlink() or not path.is_dir():
        raise ScratchError(f"{path}: a copy must be a folder at {copies_root()}/<pair>/<copy>")
    def registered(record: Registered | NotRegistered | UnreadableRecord) -> dict[str, object]:
        fields: dict[str, object] = dict(owner_unit=owner_unit, source_path=source_path,
            source_commit=source_commit, copy_commit=copy_commit, target_mode=target_mode, seed_source=seed_source,
            borrows_from=borrows_from, maker=maker(), created=time.time(), renewed=time.time())
        if isinstance(record, Registered):
            fields["created"] = record.fields.get("created", fields["created"])
        return fields_for_task(fields, task, run_dir=run_dir, phase_id=phase_id, land_target=land_target)

    _ = update_record(path, registered)


@contextmanager
def copy_lock(path: Path) -> Generator[None]:
    folder = copy_folder(path)
    if folder is None:
        yield
        return
    lock = os.open(folder / BUSY, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_SH)
        with copy_record_lock(folder):
            # A remover can unlink the mark while this holder waits on its old inode.
            # Recheck after the record lock too: deletion can finish while it waits.
            held = os.fstat(lock)
            try:
                present = (folder / BUSY).stat(follow_symlinks=False)
            except FileNotFoundError as error:
                raise ScratchError(f"{folder}: copy was removed before its busy lock was acquired") from error
            if (held.st_dev, held.st_ino) != (present.st_dev, present.st_ino):
                raise ScratchError(f"{folder}: copy changed before its busy lock was acquired")
            pending = pending_removal(folder)
            if isinstance(pending, UnreadableRecord):
                raise ScratchError(pending.reason)
            if isinstance(pending, Registered):
                raise ScratchError(f"{folder}: removal has already started; finish cleanup first")
            exclude_copy_metadata(folder)
        yield
    finally:
        os.close(lock)


@contextmanager
def copy_record_lock(path: Path) -> Generator[None]:
    """Serialize record changes and deletion on the existing directory, without creating a file."""
    with directory_record_lock(path, wait=True):
        yield


@contextmanager
def directory_record_lock(path: Path, *, wait: bool) -> Generator[bool]:
    """Lock the directory still at path; readers can decline to wait on another copy."""
    if copy_folder(path) != path.resolve():
        yield True
        return
    for attempt in range(3):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise ScratchError(f"{path}: copy was removed before its record lock was acquired") from error
        try:
            directory = os.fstat(descriptor)
            identity = (directory.st_dev, directory.st_ino)
            reentered = identity in RECORD_LOCKS.get()
            if not reentered:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                except BlockingIOError:
                    if wait:
                        raise
                    yield False
                    return
            # Waiting on a removed inode does not lock a new copy at the same path.
            # Close that descriptor before retrying, including reentrant acquisitions.
            locked = os.fstat(descriptor)
            try:
                present = path.lstat()
            except FileNotFoundError as error:
                raise ScratchError(f"{path}: copy was removed before its record lock was acquired") from error
            if (locked.st_dev, locked.st_ino) != (present.st_dev, present.st_ino):
                if attempt == 2:
                    raise ScratchError(f"{path}: copy kept changing while its record lock was acquired; retry")
                continue
            if reentered:
                yield True
                return
            token = RECORD_LOCKS.set(RECORD_LOCKS.get() | {identity})
            try:
                yield True
            finally:
                RECORD_LOCKS.reset(token)
            return
        finally:
            os.close(descriptor)


@contextmanager
def removal_lock(path: Path) -> Generator[bool]:
    """Claim the busy mark even when absent, and hold it through removal."""
    with busy_locks(path, create=True) as idle:
        yield idle


@contextmanager
def judgment_lock(path: Path) -> Generator[bool]:
    """Check existing busy marks without creating metadata during a scan or dry run."""
    with busy_locks(path, create=False) as idle:
        yield idle


@contextmanager
def busy_locks(path: Path, *, create: bool) -> Generator[bool]:
    held: list[int] = []
    acquired = True
    try:
        folder = copy_folder(path)
        marks = dict.fromkeys((path / BUSY, path.parent / BUSY, *((folder / BUSY,) if folder is not None else ())))
        try:
            for mark in marks:
                creating = create and mark.parent in (path, folder) and mark.parent.is_dir() and not mark.parent.is_symlink()
                if not creating and not os.path.lexists(mark):
                    continue
                try:
                    fd = os.open(mark, os.O_RDONLY | os.O_NOFOLLOW | (os.O_CREAT if creating else 0), 0o600)
                except FileNotFoundError:
                    if creating:
                        acquired = False
                        break
                    continue
                held.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            acquired = False
        yield acquired
    finally:
        for fd in held:
            os.close(fd)


def hand_over(path: Path, owner: str, task: str, *, run_dir: str = "", land_target: str = "") -> None:
    if not WORD.fullmatch(owner):
        raise ScratchError(f"{owner!r}: invalid owner unit")
    _ = checked_task(task, False)

    def transferred(record: Registered | NotRegistered | UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, Registered):
            raise ScratchError(f"{path}: no readable copy record")
        return fields_for_task({**record.fields, "owner_unit": owner}, task, run_dir=run_dir, land_target=land_target)

    with copy_lock(path):
        _ = update_record(path, transferred)


def fields_for_task(fields: dict[str, object], task: str, *, run_dir: str = "", phase_id: str = "",
                    land_target: str = "") -> dict[str, object]:
    """Replace every field whose meaning is scoped to a task, in one place."""
    match = checked_task(task, False)
    result = {key: value for key, value in fields.items()
              if key not in ("task", "run_dir", "phase_id", "land_target", "ended", "ended_task")}
    result["task"] = task
    if match["kind"] == "phase":
        unit, separator, phase = match["name"].partition("/")
        if not separator or not unit or not phase or phase_id and phase_id != phase:
            raise ScratchError(f"{task!r}: phase_id must match the task's phase")
        result.update(run_dir=run_dir, phase_id=phase)
    if match["kind"] == "landed" or land_target:
        # Registration may record a future land target before the task is a landed rule.
        result["land_target"] = land_target or "origin/main"
    return result


def update_record(path: Path, change: Callable[[Registered | NotRegistered | UnreadableRecord], dict[str, object]],
                  *, removal: bool = False) -> Registered:
    """The only record-write path: read, change and publish under the directory lock."""
    with copy_record_lock(path):
        pending = pending_removal(path) if copy_folder(path) == path.resolve() else NotRegistered()
        if isinstance(pending, UnreadableRecord):
            raise ScratchError(pending.reason)
        if isinstance(pending, Registered) and not removal:
            raise ScratchError(f"{path}: removal has already started; finish cleanup first")
        record = task_of(path)
        fields = change(record)
        task = fields.get("task")
        if not isinstance(task, str):
            raise ScratchError(f"{path}: record needs a task")
        _ = checked_task(task, False)
        if copy_folder(path) == path.resolve():
            directory = path.stat()
            identity = record.fields.get("copy_id") if isinstance(record, Registered) else ""
            fields.update(copy_id=identity if isinstance(identity, str) and identity else secrets.token_hex(16),
                          copy_device=directory.st_dev, copy_inode=directory.st_ino)
            if removal:
                fields["copy_path"] = str(path.resolve())
            else:
                exclude_copy_metadata(path)
        destination = removal_mark(path) if removal else path / RECORD
        atomic_record(destination, fields)
        return Registered(task, fields)


class ScratchError(Exception):
    pass


@dataclass(frozen=True)
class Folder:
    path: Path
    task: str
    changed: float


class Hold(TypedDict):
    task: str
    maker: str
    reason: str
    made: float
    ended: float


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()


def visible_root() -> Path:
    return Path(os.environ.get(VISIBLE_ROOT_ENV) or DEFAULT_VISIBLE_ROOT).expanduser()


def until_task(moment: float) -> str:
    return "until:" + datetime.fromtimestamp(moment, timezone.utc).strftime(UNTIL_FORMAT)


def parsed_until(text: str) -> str:
    """The until: task for `YYYY-MM-DD` (its end) or `YYYY-MM-DD HH:MM`, in the user's zone."""
    for form, day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            moment = datetime.strptime(text.strip(), form).replace(tzinfo=user_zone())
        except ValueError:
            continue
        return until_task(moment.timestamp() + (24 * 3600 if day else 0))
    raise ScratchError(f"{text!r}: --until takes YYYY-MM-DD or YYYY-MM-DD HH:MM")


def task_end(task: str, now: float | None = None) -> Live | Ended | Unknown:
    """End evidence for a deadline, or the reason its external end is still needed."""
    match = TASK.fullmatch(task)
    if match is None:
        return Unknown(f"invalid task: {task}")
    if match["kind"] != "until":
        return Live(f"task {task} has not ended")
    try:
        at = datetime.strptime(match["name"], UNTIL_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return Unknown(f"invalid task deadline: {task}")
    return Ended(f"deadline {task} passed", at) if at <= (time.time() if now is None else now) else Live(f"task {task} has not ended")


def ended(task: str, now: float) -> bool:
    return isinstance(task_end(task, now), Ended)


def checked_task(task: str, visible: bool) -> re.Match[str]:
    match = TASK.fullmatch(task)
    if match is None:
        raise ScratchError(f"{task!r} is no task: use run:<id>, phase:<unit>/<phase>, gate:<id>, "
                           + "landed:<unit>/<branch>, row:<showrunner>-C<n> or --until <when>")
    if match["kind"] == "row" and not ROW_NAME.fullmatch(match["name"]):
        raise ScratchError(f"{task!r}: a row task is row:<showrunner>-C<n>")
    if match["kind"] == "until" and isinstance(task_end(task), Unknown):
        raise ScratchError(f"{task!r}: an until task is until:YYYY-MM-DDTHHMMZ; use --until")
    if visible and match["kind"] != "row":
        raise ScratchError("--visible is for a change review row's repro only")
    return match


def target(task: str, word: str, visible: bool) -> Path:
    """The new folder's path for the task; never one that exists."""
    match = checked_task(task, visible)
    if word and not WORD.fullmatch(word):
        raise ScratchError(f"{word!r}: a name is letters, digits, - and _")
    if visible:
        path = visible_root() / "-".join(part for part in (match["name"], word) if part)
        if os.path.lexists(path):
            raise ScratchError(f"{path} exists")
        return path
    suffix = "".join(secrets.choice(SUFFIX_CHARACTERS) for _ in range(SUFFIX_LENGTH))
    slug = f"{match['kind']}-{match['name'].replace('/', '-')}"
    return root() / "-".join(part for part in (slug, word, suffix) if part)


def maker() -> str:
    return next((os.environ[name] for name in SESSION_VARIABLES if os.environ.get(name)), "")


def task_or_default(task: str, now: float) -> str:
    return task or until_task(now + DEFAULT_LIFE_SECONDS)


def write_record(path: Path, task: str, now: float) -> None:
    _ = update_record(path, lambda record: fields_for_task(
        {**(record.fields if isinstance(record, Registered) else {}),
         "maker": maker(), "created": now, "renewed": now}, task))


def new(task: str = "", word: str = "", visible: bool = False, now: float | None = None) -> Path:
    moment = time.time() if now is None else now
    task = task_or_default(task, moment)
    path = target(task, word, visible)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    write_record(path, task, moment)
    return path


def adopt(source: Path, task: str, word: str = "", visible: bool = False, now: float | None = None) -> Path:
    """Move a loose folder into a scratch root and record its task. A git worktree's link moves
    with it unrepaired: `git worktree repair` from the new place fixes it when it must work."""
    if source.is_symlink() or not source.is_dir():
        raise ScratchError(f"{source} is no folder")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment)
    path = target(task, word, visible)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.move(str(source), str(path))
    write_record(path, task, moment)
    return path


def read_record(path: Path) -> Registered | NotRegistered | UnreadableRecord:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NotRegistered()
    except (OSError, UnicodeError, ValueError) as error:
        return UnreadableRecord(f"repair record {path}: {error}")
    fields = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    task = fields.get("task")
    if not isinstance(task, str) or not TASK.fullmatch(task):
        return UnreadableRecord(f"repair record {path}: invalid task")
    return Registered(task, fields)


def removal_mark(path: Path) -> Path:
    """The record a started removal keeps beside the folder, outside the tree it deletes."""
    return path.parent / f"{REMOVAL_RECORD_PREFIX}{path.name}{REMOVAL_RECORD_SUFFIX}"


def removal_target(mark: Path) -> Path | None:
    """The folder a removal mark names; None when mark is no removal mark."""
    name = mark.name
    if not name.startswith(REMOVAL_RECORD_PREFIX) or not name.endswith(REMOVAL_RECORD_SUFFIX):
        return None
    folder = name[len(REMOVAL_RECORD_PREFIX):-len(REMOVAL_RECORD_SUFFIX)]
    return mark.with_name(folder) if folder else None


def marked_folders(entries: Iterable[Path]) -> Iterator[Path]:
    """Each folder whose removal mark is among entries, also when the folder is already gone."""
    for mark in entries:
        target = removal_target(mark)
        if target is not None and mark.is_file():
            yield target


def folder_record(path: Path) -> Path:
    """The file that holds path's task: its own record, else its removal mark."""
    record = path / RECORD
    return record if record.is_file() else removal_mark(path)


def pending_removal(path: Path) -> Registered | NotRegistered | UnreadableRecord:
    """Removal evidence lives outside the tree; for a copy it names the exact copy it certifies."""
    record = checked_removal(path)
    if not isinstance(record, StaleRemoval):
        return record
    # A lifetime scan may already own a different copy's record lock. Never wait
    # on a sibling merely to discard an obsolete certificate.
    with directory_record_lock(path, wait=False) as acquired:
        if acquired:
            current = checked_removal(path)
            if not isinstance(current, StaleRemoval):
                return current
            removal_mark(path).unlink(missing_ok=True)
    return NotRegistered()


def checked_removal(path: Path) -> Registered | NotRegistered | UnreadableRecord | StaleRemoval:
    record = read_record(removal_mark(path))
    if not isinstance(record, Registered) or copy_folder(path) != path.resolve():
        return record
    if record.fields.get("copy_path") != str(path.resolve()):
        return UnreadableRecord(f"repair removal record {removal_mark(path)}: copy path differs")
    try:
        directory = path.stat()
    except FileNotFoundError:
        return record
    except OSError as error:
        return UnreadableRecord(f"repair copy identity {path}: {error}")
    device, inode = record.fields.get("copy_device"), record.fields.get("copy_inode")
    if not isinstance(device, int) or not isinstance(inode, int):
        return UnreadableRecord(f"repair removal record {removal_mark(path)}: copy identity is missing")
    present = read_record(path / RECORD)
    different_record = (isinstance(present, Registered) and bool(present.fields.get("copy_id"))
                        and present.fields.get("copy_id") != record.fields.get("copy_id"))
    if (device, inode) != (directory.st_dev, directory.st_ino) or different_record:
        return StaleRemoval()
    return record


def task_of(path: Path) -> Registered | NotRegistered | UnreadableRecord:
    record = read_record(path / RECORD)
    return pending_removal(path) if isinstance(record, NotRegistered) else record


def pending_copy_paths(base: Path) -> Iterator[Path]:
    if not base.exists():
        return
    for pair in base.iterdir():
        if pair.is_dir() and not pair.is_symlink():
            yield from marked_folders(pair.iterdir())


def copy_paths() -> Iterator[Path]:
    """Each pair/copy without following links. An unreadable registry fails closed."""
    base = copies_root()
    if not base.exists():
        return
    for pair in base.iterdir():
        if pair.is_dir() and not pair.is_symlink():
            for path in pair.iterdir():
                if path.is_dir() and not path.is_symlink():
                    yield path


def record_lifetime(path: Path, now: float, ending_task: str = "") -> Live | Ended | Unknown:
    """Read end evidence while the caller holds any required removal lock."""
    removal = pending_removal(path)
    if isinstance(removal, Registered):
        return Ended(f"removal started for task {removal.task}")
    if isinstance(removal, UnreadableRecord):
        return Unknown(removal.reason)
    record = task_of(path)
    if isinstance(record, UnreadableRecord):
        return Unknown(record.reason)
    if isinstance(record, NotRegistered):
        return Unknown(f"repair record {path / RECORD}: copy is not registered")
    protection: Live | Ended | Unknown = Ended("no copy borrows from it")
    try:
        for child in copy_paths():
            if child.resolve() == path.resolve():
                continue
            if isinstance(pending_removal(child), Registered):
                continue
            other = task_of(child)
            if isinstance(other, NotRegistered):
                protection = Unknown(f"repair record {child / RECORD}: cannot rule out borrowed objects")
                break
            if isinstance(other, UnreadableRecord):
                protection = Unknown(other.reason + "; cannot rule out borrowed objects")
                break
            borrowed = other.fields.get("borrows_from", "")
            if not isinstance(borrowed, str):
                protection = Unknown(f"repair record {child / RECORD}: invalid borrows_from")
                break
            if borrowed and Path(borrowed).resolve() == (path / ".git").resolve():
                protection = Live(f"borrowed: {child} still uses its git objects")
                break
    except OSError as error:
        protection = Unknown(f"repair copies registry {copies_root()}: {error}")
    if record.task.startswith("phase:"):
        run, phase = record.fields.get("run_dir"), record.fields.get("phase_id")
        if not isinstance(run, str) or not run or not isinstance(phase, str) or not phase:
            return Unknown(f"repair record {path / RECORD}: phase needs run_dir and phase_id")
        progress = Path(run) / "progress_history_state.json"
        try:
            raw = cast(object, json.loads(progress.read_text(encoding="utf-8")))
            if not isinstance(raw, dict):
                raise ValueError("expected progress object")
            state = cast(dict[str, object], raw)
            current = state.get("phase")
            parked = state.get("open_phases", [])
            if not isinstance(current, dict) or not isinstance(parked, list):
                raise ValueError("invalid phase state")
            phases: list[object] = [cast(object, current), *cast(list[object], parked)]
            for item in phases:
                if not isinstance(item, dict):
                    raise ValueError("invalid phase record")
                fields = cast(dict[str, object], item)
                if str(fields.get("id")) == phase:
                    status = fields.get("status")
                    if status == "active" or item in parked:
                        return Live(f"open phase: {phase} in {run}")
                    # finish-phase's --status choices in progress_history.py; no shared constant exists.
                    if status not in ("completed", "stopped", "error", "finished", "complete", "ended"):
                        raise ValueError("unreadable phase status")
                    return Ended(f"phase {phase} ended in {progress}") if isinstance(protection, Ended) else protection
            if record.task == ending_task or task_has_end_evidence(record):
                return Ended(f"task {record.task} ended; phase is no longer open in {progress}") if isinstance(protection, Ended) else protection
            raise ValueError(f"phase {phase} has no end record")
        except (OSError, ValueError, UnicodeError) as error:
            return Unknown(f"repair progress file {progress}: {error}")
    if record.task.startswith("landed:"):
        branch = record.task.split("/", 1)[-1]
        target = record.fields.get("land_target", "origin/main") or "origin/main"
        if not isinstance(target, str):
            return Unknown(f"repair land target in {path / RECORD}")
        source = record.fields.get("source_path")
        if not isinstance(source, str) or not source:
            return Unknown(f"repair source checkout in {path / RECORD}: source_path is missing")
        source_path = Path(source)
        try:
            if not source_path.is_dir():
                return Unknown(f"repair source checkout {source_path}: folder is missing or unreadable")
            for ref in ("origin/" + branch, target):
                result = _git(source_path, "rev-parse", "--verify", ref + "^{commit}")
                if result.returncode:
                    return Unknown(f"repair land target {ref} in source checkout {source_path}: {result.stderr.strip()}")
            ancestor = _git(source_path, "merge-base", "--is-ancestor", "origin/" + branch, target)
        except OSError as error:
            return Unknown(f"repair source checkout {source_path}: {error}")
        if ancestor.returncode == 1:
            return Live(f"not landed: origin/{branch} is not in {target}")
        if ancestor.returncode:
            return Unknown(f"repair land target {target} in source checkout {source_path}: {ancestor.stderr.strip()}")
        problem = unsaved_work(path)
        if problem is not None:
            return Unknown(problem) if "git cannot" in problem else Live("not landed: " + problem)
        return Ended(f"origin/{branch} landed in {target}") if isinstance(protection, Ended) else protection
    if record.task == ending_task or task_has_end_evidence(record):
        return Ended(f"task {record.task} ended") if isinstance(protection, Ended) else protection
    deadline = task_end(record.task, now)
    return protection if isinstance(deadline, Ended) and not isinstance(protection, Ended) else deadline


def task_has_end_evidence(record: Registered) -> bool:
    at = record.fields.get("ended", 0)
    return record.fields.get("ended_task") == record.task and isinstance(at, (int, float)) and at > 0


def lifetime(path: Path, now: float | None = None) -> Live | Ended | Unknown:
    with judgment_lock(path) as idle:
        if not idle:
            return Live("busy: .claude-busy is locked")
    return record_lifetime(path, time.time() if now is None else now)


def folders() -> list[Folder]:
    """Every recorded scratch folder, including interrupted removals, in both roots."""
    candidates: dict[Path, None] = {}
    for base in (root(), visible_root()):
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        listed = {path for path in entries if path.is_dir()} | set(marked_folders(entries))
        candidates.update(dict.fromkeys(sorted(listed)))
    candidates.update(dict.fromkeys((*copy_paths(), *pending_copy_paths(copies_root()))))
    found: list[Folder] = []
    for path in candidates:
        record = task_of(path) if not path.is_symlink() else NotRegistered()
        if isinstance(record, Registered):
            found.append(Folder(path, record.task, folder_record(path).stat().st_mtime))
    return found


def renew(path: Path, now: float | None = None) -> None:
    moment = time.time() if now is None else now

    def renewed(record: Registered | NotRegistered | UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, Registered):
            raise ScratchError(f"{path} is no scratch folder")
        return {**record.fields, "renewed": moment}

    with copy_record_lock(path):
        _ = update_record(path, renewed)
        os.utime(path / RECORD, (moment, moment))


def remove(path: Path) -> None:
    """Delete one folder, its record last; a linked git worktree through its source repo first.
    Callers go through remove_marked, so a stop at any line leaves the task findable."""
    if (path / ".git").is_file():
        _ = subprocess.run(["git", "-C", str(path), "worktree", "remove", "--force", str(path)],
                           capture_output=True, check=False)
    if os.path.lexists(path):
        for child in sorted(path.iterdir(), key=lambda child: (child.name == RECORD, child.name)):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        path.rmdir()


def remove_marked(path: Path, delete: Callable[[Path], None]) -> None:
    """Write the removal mark outside the tree before its first unlink, delete, then drop the mark.
    A rerun finds the task in the mark after the folder's record or the folder is gone. A copy
    needs a readable record; callers of a copy hold its removal lock. An unrecorded folder
    (a held one) gets no mark."""
    def removing(record: Registered | NotRegistered | UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, Registered):
            raise ScratchError(f"{path}: no readable record for removal")
        return dict(record.fields)

    copy = copy_folder(path) == path.resolve()
    with copy_record_lock(path):
        pending = pending_removal(path)
        if isinstance(pending, UnreadableRecord) and copy:
            raise ScratchError(pending.reason)
        if isinstance(pending, NotRegistered) and (copy or isinstance(read_record(path / RECORD), Registered)):
            _ = update_record(path, removing, removal=True)
        delete(path)
        removal_mark(path).unlink(missing_ok=True)


def clear_finished_copy_removals(base: Path) -> None:
    """A stop after the tree went away may leave only its outside removal record."""
    for path in pending_copy_paths(base):
        if not os.path.lexists(path) and isinstance(pending_removal(path), Registered):
            with removal_lock(path) as idle:
                if idle and not os.path.lexists(path):
                    removal_mark(path).unlink(missing_ok=True)


def holds_file() -> Path:
    return Path(os.environ.get(HOLDS_ENV) or DEFAULT_HOLDS).expanduser()


def holds() -> dict[str, Hold]:
    """Every hold on this machine, by the held folder's real path."""
    try:
        raw = cast(object, json.loads(holds_file().read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    found: dict[str, Hold] = {}
    for path, value in cast(dict[str, object], raw).items():
        if isinstance(value, dict):
            fields = cast(dict[str, object], value)
            task, made, end_at = fields.get("task"), fields.get("made"), fields.get("ended", 0.0)
            if isinstance(task, str) and isinstance(made, (int, float)) and isinstance(end_at, (int, float)):
                found[path] = Hold(task=task, maker=str(fields.get("maker", "")), reason=str(fields.get("reason", "")),
                                   made=float(made), ended=float(end_at))
    return found


def write_holds(found: dict[str, Hold]) -> None:
    path = holds_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    _ = staged.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _ = staged.replace(path)


def hold(path: Path, task: str, reason: str, now: float | None = None) -> str:
    """Hold an existing folder for `task`; return the task."""
    if path.is_symlink() or not path.is_dir():
        raise ScratchError(f"{path} is no folder")
    if not reason.strip():
        raise ScratchError("a hold needs --reason")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment)
    _ = checked_task(task, False)
    found = holds()
    found[os.path.realpath(path)] = Hold(task=task, maker=maker(), reason=reason.strip(), made=moment, ended=0.0)
    write_holds(found)
    return task


def release(path: Path) -> bool:
    found = holds()
    if found.pop(os.path.realpath(path), None) is None:
        return False
    write_holds(found)
    return True


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)


def repos(path: Path, depth: int = REPO_DEPTH) -> list[Path]:
    """Each git repo or worktree at or under `path`, down to `depth` folders; links not followed."""
    if os.path.lexists(path / ".git"):
        return [path]
    if depth == 0:
        return []
    found: list[Path] = []
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name not in SKIPPED_FOLDERS and entry.is_dir() and not entry.is_symlink():
            found.extend(repos(entry, depth - 1))
    return found


def repo_problem(repo: Path) -> str | None:
    """Why removing this repo loses work, or None. A linked worktree's branches and stash live in its
    source repo, so only its own changes and its HEAD count."""
    linked = (repo / ".git").is_file()
    status = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode:
        return f"git cannot read it ({(status.stderr.strip().splitlines() or ['no message'])[0]})"
    if status.stdout.strip():
        return "it has uncommitted changes"
    if _git(repo, "rev-parse", "--verify", "--quiet", "HEAD").returncode:
        return None
    if not linked and not _git(repo, "rev-parse", "--verify", "--quiet", "refs/stash").returncode:
        return "it has a stash"
    if not linked and not _git(repo, "remote").stdout.strip():
        return "it has commits and no remote"
    unpushed = _git(repo, "rev-list", "-n1", "HEAD" if linked else "--branches", "--not", "--remotes")
    if unpushed.returncode:
        return "git cannot list its commits"
    return "it has commits that are on no remote" if unpushed.stdout.strip() else None


def unsaved_work(path: Path) -> str | None:
    """`<repo>: <why>` for the first repo in `path` whose work removal would lose, or None."""
    for repo in repos(path):
        problem = repo_problem(repo)
        if problem is not None:
            return f"{repo}: {problem}"
    return None


def end(task: str, now: float | None = None, *, keep: Callable[[Path], bool] = lambda _: False) -> list[Path]:
    """Remove every folder and held folder of the task. A held folder whose repo holds unsaved work
    stays, and its hold is marked ended. Folders selected by keep stay unchanged."""
    _ = checked_task(task, False)
    removed: list[Path] = []
    moment = time.time() if now is None else now
    for folder in folders():
        if folder.task == task and not keep(folder.path):
            if copy_folder(folder.path) is not None:
                if end_copy(folder.path, task, moment):
                    removed.append(folder.path)
            else:
                remove_marked(folder.path, remove)
                removed.append(folder.path)
    found = holds()
    changed = False
    for path, record in list(found.items()):
        if record["task"] != task or keep(Path(path)):
            continue
        changed = True
        held = Path(path)
        copies = Path(os.path.realpath(copies_root()))
        if copies.is_relative_to(held.resolve()) or held.parent == copies:
            for copy in list(copy_paths()):
                if copy.is_relative_to(held) and not keep(copy) and end_copy(copy, task, moment, require_task=False):
                    removed.append(copy)
            clear_finished_copy_removals(copies)
            remove_empty_copy_pairs(copies)
            # A container hold never takes recursive deletion through the copies root.
            if held != copies and held.exists():
                try:
                    held.rmdir()
                except OSError:
                    record["ended"] = moment
                    continue
            if held == copies and any(copy_paths()):
                record["ended"] = moment
                continue
            del found[path]
            continue
        if os.path.lexists(held) and copy_folder(held) is not None:
            if end_copy(held, task, moment):
                removed.append(held)
                del found[path]
            else:
                record["ended"] = moment
            continue
        if os.path.lexists(held) and unsaved_work(held) is not None:
            record["ended"] = moment
            continue
        if os.path.lexists(held):
            remove_marked(held, remove)
            removed.append(held)
        del found[path]
    if changed:
        write_holds(found)
    return removed


def end_copy(path: Path, task: str, now: float, *, require_task: bool = True) -> bool:
    with removal_lock(path) as idle, copy_record_lock(path):
        record = task_of(path)
        if not isinstance(record, Registered) or require_task and record.task != task:
            return False
        pending = pending_removal(path)
        if record.task == task and isinstance(pending, NotRegistered) and not task_has_end_evidence(record):
            def task_ended(current: Registered | NotRegistered | UnreadableRecord) -> dict[str, object]:
                if not isinstance(current, Registered) or current.task != task:
                    raise ScratchError(f"{path}: task changed while recording its end")
                return {**current.fields, "ended": now, "ended_task": task}

            _ = update_record(path, task_ended)
        if not idle:
            return False
        state = record_lifetime(path, now)
        if not isinstance(state, Ended):
            return False
        if not isinstance(pending, Registered) and unsaved_work(path) is not None:
            return False
        remove_marked(path, remove)
        return True


def remove_empty_copy_pairs(base: Path) -> None:
    if base.is_dir():
        for pair in base.iterdir():
            if pair.is_dir() and not pair.is_symlink():
                try:
                    pair.rmdir()
                except OSError:
                    pass


def chosen_task(arguments: argparse.Namespace) -> str:
    """The --task given, the until: task of --until, or "" for the default."""
    until = cast(str, arguments.until)
    return parsed_until(until) if until else cast(str, arguments.task)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scratch.py", description="Scratch folders that belong to a task.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "adopt", "hold"):
        command = commands.add_parser(name)
        if name != "new":
            _ = command.add_argument("path")
        ends = command.add_mutually_exclusive_group()
        _ = ends.add_argument("--task", default="")
        _ = ends.add_argument("--until", default="")
        if name == "hold":
            _ = command.add_argument("--reason", required=True)
        else:
            _ = command.add_argument("--name", default="")
            _ = command.add_argument("--visible", action="store_true")
    _ = commands.add_parser("release").add_argument("path")
    _ = commands.add_parser("renew").add_argument("path")
    _ = commands.add_parser("end").add_argument("--task", required=True)
    _ = commands.add_parser("list")
    handover = commands.add_parser("hand-over")
    _ = handover.add_argument("path")
    _ = handover.add_argument("--to", required=True)
    _ = handover.add_argument("--task", required=True)
    _ = handover.add_argument("--run-dir", default="")
    _ = handover.add_argument("--land-target", default="")
    arguments = parser.parse_args(argv)
    command = cast(str, arguments.command)
    try:
        if command == "new":
            print(new(chosen_task(arguments), cast(str, arguments.name), cast(bool, arguments.visible)))
        elif command == "adopt":
            print(adopt(Path(cast(str, arguments.path)), chosen_task(arguments), cast(str, arguments.name),
                        cast(bool, arguments.visible)))
        elif command == "hold":
            path = Path(cast(str, arguments.path))
            print(f"held {os.path.realpath(path)} for {hold(path, chosen_task(arguments), cast(str, arguments.reason))}")
        elif command == "release":
            path = Path(cast(str, arguments.path))
            print(f"released {path}" if release(path) else f"{path} has no hold")
        elif command == "renew":
            renew(Path(cast(str, arguments.path)))
        elif command == "hand-over":
            path = Path(cast(str, arguments.path))
            hand_over(path, cast(str, arguments.to), cast(str, arguments.task),
                      run_dir=cast(str, arguments.run_dir), land_target=cast(str, arguments.land_target))
            print(path)
        elif command == "end":
            for path in end(cast(str, arguments.task)):
                print(f"removed {path}")
        else:
            now = time.time()
            for folder in folders():
                print(f"{folder.path}\t{folder.task}\t{(now - folder.changed) / 3600:.1f} h since a change")
            for path, record in sorted(holds().items()):
                state = "ended, unsaved work" if record["ended"] else "held"
                print(f"{path}\t{record['task']}\t{state}: {record['reason']}")
    except (ScratchError, OSError) as error:
        print(f"scratch: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
