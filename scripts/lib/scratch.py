#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
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
  * `until:<YYYY-MM-DDTHHMMZ>/<id>`: the hourly temporary sweep ends the task when its time passes.
    `--until` takes `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` in the user's zone.
    Each call makes a random id with scratch.conf suffix_length characters.
    With no task, a folder gets a separate deadline task for scratch.conf default_life_seconds from now.
    Old deadline tasks without an id remain valid.

A folder lives in ROOT as `<task>-<word>-<suffix>`; scratch.conf suffix_length sets the suffix length.
`--visible` (rows only) puts it in VISIBLE_ROOT
as `<showrunner>-C<n>-<word>`, where cargo-port lists it. Each holds copy_record.RECORD with its task, maker and
times. The hourly tmp sweep (scripts/lint/tmp_sweep.py) keeps a folder in ROOT while its task is
open, and removes it when its until: time passes, or after 7 idle days whatever its task, since an
ender can fail. A row's folder waits for its verdict, and VISIBLE_ROOT is never swept. A git
worktree goes through `git worktree remove`, so its source repo keeps no stale entry. Removal
first writes `.scratch-removing-<folder>.json` beside the folder and deletes copy_record.RECORD last, so a
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
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
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

from scripts.lib import cargo_output, conf, copy_record  # noqa: E402
from scripts.lib.conf import UseConfiguredValue

from scripts.lib.build_folders import BuildFolderSpec, build_folder_path  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402

VISIBLE_ROOT_ENV = "SCRATCH_VISIBLE_ROOT"
# A started removal keeps the record outside the folder until the folder is gone.
REMOVAL_RECORD_PREFIX = ".scratch-removing-"
REMOVAL_RECORD_SUFFIX = ".json"
HOLDS_ENV = "SCRATCH_HOLDS"
UNTIL_FORMAT = "%Y-%m-%dT%H%MZ"
ROW_NAME = re.compile(r"[A-Za-z0-9_-]+-C[0-9]+")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
BUSY = ".claude-busy"
RECORD_LOCKS: ContextVar[frozenset[tuple[int, int]]] = ContextVar("scratch_record_locks", default=frozenset())
USE_STAMP = ".lint-sweep-used"
CACHE_TAG = "CACHEDIR.TAG"
CACHE_TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
CARGO_TAG_LINE = "# This file is a cache directory tag created by cargo."
STYLE_TARGET_NAME = "style-sync-target"
STYLE_LOCK_NAME = "sync.lock"


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


def mark_cargo_cache(target: Path) -> None:
    """Tag a lasting Cargo cache and refresh its one use stamp."""
    from scripts.lib.atomic_file import write_text_atomic

    target.mkdir(parents=True, exist_ok=True)
    tag = target / CACHE_TAG
    if not tag.exists():
        write_text_atomic(tag, CACHE_TAG_SIGNATURE + "\n" + CARGO_TAG_LINE + "\n")
    (target / USE_STAMP).touch()


def atomic_record(path: Path, fields: object) -> None:
    # A killed writer may leave its stage file. Keep copy stages in Git metadata,
    # where they cannot look like unsaved user work on the next run.
    git = path.parent / ".git"
    staging = git if path.name == copy_record.RECORD and git.is_dir() and not git.is_symlink() else path.parent
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
    patterns = [f"/{name}" for name in (copy_record.RECORD, BUSY) if f"/{name}" not in text.splitlines()]
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
                  land_target: str = "", run_dir: str = "", phase_id: str = "", crate_run: str = "",
                  repository_identity: str = "") -> None:
    scratch_settings = conf.read("scratch")
    if copy_record.copy_folder(path) != path.resolve() or path.is_symlink() or not path.is_dir():
        raise ScratchError(f"{path}: a copy must be a folder at {copy_record.copies_root()}/<pair>/<copy>")
    def registered(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        fields: dict[str, object] = dict(owner_unit=owner_unit, source_path=source_path,
            source_commit=source_commit, copy_commit=copy_commit, target_mode=target_mode, seed_source=seed_source,
            borrows_from=borrows_from, repository_identity=repository_identity,
            maker=maker(), created=time.time(), renewed=time.time())
        if isinstance(record, copy_record.Registered):
            fields["created"] = record.fields.get("created", fields["created"])
        linked_run = record.fields.get("crate_run", "") if isinstance(record, copy_record.Registered) else ""
        if crate_run and linked_run and linked_run != crate_run:
            raise ScratchError(f"{path}: crate run link changed")
        if crate_run or linked_run:
            fields["crate_run"] = crate_run or linked_run
        return fields_for_task(fields, task, run_dir=run_dir, phase_id=phase_id, land_target=land_target)

    _ = update_record(path, registered, scratch_settings=scratch_settings)


@dataclass(frozen=True)
class GitDirectory:
    path: Path


@dataclass(frozen=True)
class NoGitDirectory:
    pass


def git_directory(repo: Path) -> GitDirectory | NoGitDirectory:
    """Resolve the checkout's Git metadata, including a linked worktree's pointer."""
    marker = repo / ".git"
    if marker.is_dir() and not marker.is_symlink():
        return GitDirectory(marker)
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return NoGitDirectory()
    if not line.startswith("gitdir:"):
        return NoGitDirectory()
    pointer = line.removeprefix("gitdir:").strip()
    if not pointer or "\n" in pointer:
        return NoGitDirectory()
    found = repo / pointer
    return GitDirectory(found) if found.is_dir() else NoGitDirectory()


def busy_path(path: Path) -> Path:
    """One busy-lock location; Git metadata keeps the lock out of saved source."""
    git = git_directory(path)
    if isinstance(git, GitDirectory):
        return git.path / BUSY
    if os.path.lexists(path / ".git"):
        raise OSError(f"{path}: cannot locate Git metadata for its busy lock")
    return path / BUSY


@contextmanager
def copy_lock(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[None]:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    folder = copy_record.copy_folder(path)
    if folder is None:
        record = task_of(path)
        if not isinstance(record, copy_record.Registered) or "owner_unit" not in record.fields:
            yield
            return
        folder = path
    mark = busy_path(folder)
    lock = os.open(mark, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_SH)
        with copy_record_lock(folder, scratch_settings=scratch_settings):
            # A remover can unlink the mark while this holder waits on its old inode.
            # Recheck after the record lock too: deletion can finish while it waits.
            held = os.fstat(lock)
            try:
                present = mark.stat(follow_symlinks=False)
            except FileNotFoundError as error:
                raise ScratchError(f"{folder}: copy was removed before its busy lock was acquired") from error
            if (held.st_dev, held.st_ino) != (present.st_dev, present.st_ino):
                raise ScratchError(f"{folder}: copy changed before its busy lock was acquired")
            pending = pending_removal(folder, scratch_settings=scratch_settings)
            if isinstance(pending, copy_record.UnreadableRecord):
                raise ScratchError(pending.reason)
            if isinstance(pending, copy_record.Registered):
                raise ScratchError(f"{folder}: removal has already started; finish cleanup first")
            exclude_copy_metadata(folder)
        yield
    finally:
        os.close(lock)


@contextmanager
def copy_record_lock(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[None]:
    """Serialize record changes and deletion on the existing directory, without creating a file."""
    with directory_record_lock(path, wait=True, scratch_settings=scratch_settings):
        yield


@contextmanager
def directory_record_lock(path: Path, *, wait: bool,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[bool]:
    """Lock the directory still at path; readers can decline to wait on another copy."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if copy_record.copy_folder(path) != path.resolve():
        records = (copy_record.read_record(path / copy_record.RECORD), copy_record.read_record(removal_mark(path)))
        if not any(isinstance(record, copy_record.Registered) and "owner_unit" in record.fields for record in records):
            yield True
            return
    attempts = scratch_settings.whole("record_lock_attempts")
    for attempt in range(attempts):
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
                if attempt == attempts - 1:
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
def busy_locks(path: Path, *, create: bool, inherited_fds: list[int] | None = None,
               wait: bool = False) -> Generator[bool]:
    """Hold busy marks; optionally export live descriptors or wait for their release.

    Exported descriptors belong to this context. A child can retain the same
    flock with subprocess pass_fds; callers must not close them themselves.
    """
    held: list[int] = []
    acquired = True
    try:
        try:
            folder = copy_record.copy_folder(path)
            owners = dict.fromkeys((path, path.parent, *((folder,) if folder is not None else ())))
            marks: dict[Path, bool] = {}
            for owner in owners:
                mark = busy_path(owner)
                marks[mark] = create and owner in (path, folder) and owner.is_dir() and not owner.is_symlink()
                # Locks left by older callers still protect their work. Never create them in a checkout.
                _ = marks.setdefault(owner / BUSY, False)
            for mark, creating in marks.items():
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
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except OSError:
            acquired = False
        if acquired and inherited_fds is not None:
            inherited_fds.extend(held)
        yield acquired
    finally:
        for fd in held:
            os.close(fd)


def hand_over(path: Path, owner: str, task: str, *, run_dir: str = "", land_target: str = "",
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if not WORD.fullmatch(owner):
        raise ScratchError(f"{owner!r}: invalid owner unit")
    validate_phase_task(checked_task(task, False))

    def transferred(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, copy_record.Registered):
            raise ScratchError(f"{path}: no readable copy record")
        return fields_for_task({**record.fields, "owner_unit": owner}, task, run_dir=run_dir, land_target=land_target)

    with copy_lock(path, scratch_settings=scratch_settings):
        _ = update_record(path, transferred, scratch_settings=scratch_settings)


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


def update_record(path: Path, change: Callable[[copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord], dict[str, object]],
                  *, removal: bool = False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> copy_record.Registered:
    """The only record-write path: read, change and publish under the directory lock."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    with copy_record_lock(path, scratch_settings=scratch_settings):
        pending = pending_removal(path, scratch_settings=scratch_settings) if copy_record.copy_folder(path) == path.resolve() else copy_record.NotRegistered()
        if isinstance(pending, copy_record.UnreadableRecord):
            raise ScratchError(pending.reason)
        if isinstance(pending, copy_record.Registered) and not removal:
            raise ScratchError(f"{path}: removal has already started; finish cleanup first")
        record = task_of(path, scratch_settings=scratch_settings)
        fields = change(record)
        task = fields.get("task")
        if not isinstance(task, str):
            raise ScratchError(f"{path}: record needs a task")
        _ = checked_task(task, False)
        if copy_record.copy_folder(path) == path.resolve() or "owner_unit" in fields:
            directory = path.stat()
            identity = record.fields.get("copy_id") if isinstance(record, copy_record.Registered) else ""
            fields.update(copy_id=identity if isinstance(identity, str) and identity else secrets.token_hex(16),
                          copy_device=directory.st_dev, copy_inode=directory.st_ino)
            if removal:
                fields["copy_path"] = str(path.resolve())
            else:
                exclude_copy_metadata(path)
        destination = removal_mark(path) if removal else path / copy_record.RECORD
        atomic_record(destination, fields)
        return copy_record.Registered(task, fields)


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


def visible_root(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return Path(os.environ.get(VISIBLE_ROOT_ENV) or scratch_settings.text("visible_root")).expanduser()


def random_suffix(scratch_settings: conf.ConfigRead) -> str:
    length = scratch_settings.whole("suffix_length")
    return secrets.token_hex(length)[:length]


def until_task(moment: float, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    stamp = datetime.fromtimestamp(moment, timezone.utc).strftime(UNTIL_FORMAT)
    return f"until:{stamp}/{random_suffix(scratch_settings)}"


def until_stamp(name: str) -> str:
    return name.partition("/")[0]


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
    match = copy_record.TASK.fullmatch(task)
    if match is None:
        return Unknown(f"invalid task: {task}")
    if match["kind"] != "until":
        return Live(f"task {task} has not ended")
    try:
        at = datetime.strptime(until_stamp(match["name"]), UNTIL_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return Unknown(f"invalid task deadline: {task}")
    return Ended(f"deadline {task} passed", at) if at <= (time.time() if now is None else now) else Live(f"task {task} has not ended")


def ended(task: str, now: float) -> bool:
    return isinstance(task_end(task, now), Ended)


def checked_task(task: str, visible: bool) -> re.Match[str]:
    match = copy_record.TASK.fullmatch(task)
    if match is None:
        raise ScratchError(f"{task!r} is no task: use run:<id>, phase:<unit>/<phase>, gate:<id>, "
                           + "landed:<unit>/<branch>, row:<showrunner>-C<n> or --until <when>")
    if match["kind"] == "row" and not ROW_NAME.fullmatch(match["name"]):
        raise ScratchError(f"{task!r}: a row task is row:<showrunner>-C<n>")
    if match["kind"] == "until" and isinstance(task_end(task), Unknown):
        raise ScratchError(f"{task!r}: an until task is until:YYYY-MM-DDTHHMMZ/<id>; use --until")
    if visible and match["kind"] != "row":
        raise ScratchError("--visible is for a change review row's repro only")
    return match


def registered_plan(unit: str) -> Path:
    """Resolve the plan from the unit's staff parent and production record."""
    from scripts.direct import phase_table
    from scripts.production import proposals_table, staff, unit_lookup
    from scripts.production.production_doc import Refusal

    try:
        parent = unit_lookup.unit_parent(unit, record=staff.snapshot())
        value = cast(object, json.loads(phase_table.show_production(Path(parent.doc))))
    except (OSError, UnicodeError, ValueError, Refusal) as error:
        raise ScratchError(f"{unit}: cannot resolve registered plan: {error}") from error
    if not isinstance(value, dict):
        raise ScratchError(f"{unit}: production plan records are not an object")
    records = cast(dict[str, object], value)
    plans = proposals_table.unit_plan_paths(records)
    if unit not in plans:
        record = records.get(unit)
        cause = cast(dict[str, object], record).get("unavailable") if isinstance(record, dict) else None
        raise ScratchError(f"{unit}: no registered plan in production {parent.doc}: "
                           + (str(cause) if cause else "no staff row or plan"))
    return plans[unit]


def validate_phase_task(match: re.Match[str]) -> None:
    """Validate a new phase assignment, without restricting legacy task cleanup."""
    if match["kind"] != "phase":
        return
    from scripts.direct import plan_history

    unit, separator, phase = match["name"].partition("/")
    hint = "use a phase id from the unit's plan, gate:<id> or --until <when>"
    if not separator or not phase:
        raise ScratchError(f"{match[0]!r}: a phase task needs phase:<unit>/<phase>; {hint}")
    try:
        plan = registered_plan(unit)
    except ScratchError as error:
        raise ScratchError(f"{match[0]!r}: {error}; {hint}") from error
    try:
        phases = plan_history.plan_phases(plan)
    except (OSError, UnicodeError) as error:
        raise ScratchError(f"{unit}: cannot read registered plan {plan}: {error}; {hint}") from error
    if not any(row["id"] == phase for row in phases):
        raise ScratchError(f"{match[0]!r}: phase {phase!r} is not in registered plan {plan}; {hint}")


def target(task: str, word: str, visible: bool, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    """The new folder's path for the task; never one that exists."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    match = checked_task(task, visible)
    validate_phase_task(match)
    if word and not WORD.fullmatch(word):
        raise ScratchError(f"{word!r}: a name is letters, digits, - and _")
    if visible:
        path = visible_root(scratch_settings=scratch_settings) / "-".join(part for part in (match["name"], word) if part)
        if os.path.lexists(path):
            raise ScratchError(f"{path} exists")
        return path
    suffix = random_suffix(scratch_settings)
    name = until_stamp(match["name"]) if match["kind"] == "until" else match["name"]
    slug = f"{match['kind']}-{name.replace('/', '-')}"
    return build_folder_path(BuildFolderSpec(kind="scratch", parent=copy_record.root(), suffix_length=len(suffix),
                                            purpose="-".join(part for part in (slug, word) if part), suffix=suffix))


def maker() -> str:
    from scripts.production import staff
    current = staff.runtime_id()
    try:
        row = staff.by_current_session(current) if current else None
    except staff.StaffAbsent:
        return ""
    return row.id if row is not None else ""


def task_or_default(task: str, now: float, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return task or until_task(now + scratch_settings.whole("default_life_seconds"), scratch_settings=scratch_settings)


def write_record(path: Path, task: str, now: float, *,
    throwaway: bool = False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    _ = update_record(path, lambda record: fields_for_task(
        {**(record.fields if isinstance(record, copy_record.Registered) else {}),
         "maker": maker(), "created": now, "renewed": now, "throwaway": throwaway}, task), scratch_settings=scratch_settings)


def new(task: str = "", word: str = "", visible: bool = False, now: float | None = None, *,
    throwaway: bool = False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    path = target(task, word, visible, scratch_settings=scratch_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    write_record(path, task, moment, throwaway=throwaway, scratch_settings=scratch_settings)
    return path


def adopt(source: Path, task: str, word: str = "", visible: bool = False, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    """Move a loose folder into a scratch root and record its task. A git worktree's link moves
    with it unrepaired: `git worktree repair` from the new place fixes it when it must work."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if source.is_symlink() or not source.is_dir():
        raise ScratchError(f"{source} is no folder")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    path = target(task, word, visible, scratch_settings=scratch_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.move(str(source), str(path))
    write_record(path, task, moment, scratch_settings=scratch_settings)
    return path


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
    record = path / copy_record.RECORD
    return record if record.is_file() else removal_mark(path)


def pending_removal(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord:
    """Removal evidence lives outside the tree; for a copy it names the exact copy it certifies."""
    record = checked_removal(path)
    if not isinstance(record, StaleRemoval):
        return record
    # A lifetime scan may already own a different copy's record lock. Never wait
    # on a sibling merely to discard an obsolete certificate.
    with directory_record_lock(path, wait=False, scratch_settings=scratch_settings) as acquired:
        if acquired:
            current = checked_removal(path)
            if not isinstance(current, StaleRemoval):
                return current
            removal_mark(path).unlink(missing_ok=True)
    return copy_record.NotRegistered()


def checked_removal(path: Path) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord | StaleRemoval:
    record = copy_record.read_record(removal_mark(path))
    if not isinstance(record, copy_record.Registered) or copy_record.copy_folder(path) != path.resolve():
        return record
    if record.fields.get("copy_path") != str(path.resolve()):
        return copy_record.UnreadableRecord(f"repair removal record {removal_mark(path)}: copy path differs")
    try:
        directory = path.stat()
    except FileNotFoundError:
        return record
    except OSError as error:
        return copy_record.UnreadableRecord(f"repair copy identity {path}: {error}")
    device, inode = record.fields.get("copy_device"), record.fields.get("copy_inode")
    if not isinstance(device, int) or not isinstance(inode, int):
        return copy_record.UnreadableRecord(f"repair removal record {removal_mark(path)}: copy identity is missing")
    present = copy_record.read_record(path / copy_record.RECORD)
    different_record = (isinstance(present, copy_record.Registered) and bool(present.fields.get("copy_id"))
                        and present.fields.get("copy_id") != record.fields.get("copy_id"))
    if (device, inode) != (directory.st_dev, directory.st_ino) or different_record:
        return StaleRemoval()
    return record


def task_of(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord:
    """Read folder task evidence; plain files have no task record."""
    if os.path.isfile(path):
        return copy_record.NotRegistered()
    record = copy_record.read_record(path / copy_record.RECORD)
    return pending_removal(path, scratch_settings=scratch_settings) if isinstance(record, copy_record.NotRegistered) else record


def is_throwaway(path: Path) -> bool:
    """Identify a folder that code creates without edits from a person."""
    record = task_of(path)
    return isinstance(record, copy_record.Registered) and record.fields.get("throwaway") is True


def pending_copy_paths(base: Path) -> Iterator[Path]:
    if not base.exists():
        return
    for pair in base.iterdir():
        if pair.is_dir() and not pair.is_symlink():
            yield from marked_folders(pair.iterdir())


def copy_paths() -> Iterator[Path]:
    """Each pair/copy without following links. An unreadable registry fails closed."""
    base = copy_record.copies_root()
    if not base.exists():
        return
    for pair in base.iterdir():
        if pair.is_dir() and not pair.is_symlink():
            for path in pair.iterdir():
                if path.is_dir() and not path.is_symlink():
                    yield path


def registered_copy_paths() -> Iterator[Path]:
    """List native copies and adopted copies from the existing holds."""
    paths = set(copy_paths())
    for held in holds():
        path = Path(held)
        record = task_of(path)
        if (isinstance(record, copy_record.UnreadableRecord)
                or isinstance(record, copy_record.Registered) and "borrows_from" in record.fields):
            paths.add(path)
    yield from sorted(paths)


def record_lifetime(path: Path, now: float, ending_task: str = "", *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Live | Ended | Unknown:
    """Read end evidence while the caller holds any required removal lock."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    removal = pending_removal(path, scratch_settings=scratch_settings)
    if isinstance(removal, copy_record.UnreadableRecord):
        return Unknown(removal.reason)
    record = task_of(path, scratch_settings=scratch_settings)
    if isinstance(record, copy_record.UnreadableRecord):
        return Unknown(record.reason)
    if isinstance(record, copy_record.NotRegistered):
        return Unknown(f"repair record {path / copy_record.RECORD}: copy is not registered")
    protection: Live | Ended | Unknown = Ended("no copy borrows from it")
    try:
        for child in registered_copy_paths():
            if child.resolve() == path.resolve():
                continue
            if not child.exists():
                continue
            other = task_of(child, scratch_settings=scratch_settings)
            if isinstance(other, copy_record.NotRegistered):
                protection = Unknown(f"repair record {child / copy_record.RECORD}: cannot rule out borrowed objects")
                break
            if isinstance(other, copy_record.UnreadableRecord):
                protection = Unknown(other.reason + "; cannot rule out borrowed objects")
                break
            borrowed = other.fields.get("borrows_from", "")
            if not isinstance(borrowed, str):
                protection = Unknown(f"repair record {child / copy_record.RECORD}: invalid borrows_from")
                break
            if borrowed and Path(borrowed).resolve() == (path / ".git").resolve():
                protection = Live(f"borrowed: {child} still uses its git objects")
                break
    except OSError as error:
        protection = Unknown(f"repair copies registry {copy_record.copies_root()}: {error}")
    if isinstance(removal, copy_record.Registered):
        return Ended(f"removal started for task {removal.task}") if isinstance(protection, Ended) else protection
    if record.task.startswith("phase:"):
        run, phase = record.fields.get("run_dir"), record.fields.get("phase_id")
        if not isinstance(run, str) or not run or not isinstance(phase, str) or not phase:
            return Unknown(f"repair record {path / copy_record.RECORD}: phase needs run_dir and phase_id")
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
            return Unknown(f"repair land target in {path / copy_record.RECORD}")
        source = record.fields.get("source_path")
        if not isinstance(source, str) or not source:
            return Unknown(f"repair source checkout in {path / copy_record.RECORD}: source_path is missing")
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
        problem = unsaved_work(path, scratch_settings=scratch_settings)
        if problem is not None:
            return Unknown(problem) if "git cannot" in problem else Live("not landed: " + problem)
        return Ended(f"origin/{branch} landed in {target}") if isinstance(protection, Ended) else protection
    if record.task == ending_task or task_has_end_evidence(record):
        return Ended(f"task {record.task} ended") if isinstance(protection, Ended) else protection
    deadline = task_end(record.task, now)
    return protection if isinstance(deadline, Ended) and not isinstance(protection, Ended) else deadline


def task_has_end_evidence(record: copy_record.Registered) -> bool:
    at = record.fields.get("ended", 0)
    return record.fields.get("ended_task") == record.task and isinstance(at, (int, float)) and at > 0


def lifetime(path: Path, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Live | Ended | Unknown:
    with judgment_lock(path) as idle:
        if not idle:
            return Live("busy: .claude-busy is locked")
    return record_lifetime(path, time.time() if now is None else now, scratch_settings=scratch_settings)


def folders(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[Folder]:
    """Every recorded scratch folder, including interrupted removals, in both roots."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    candidates: dict[Path, None] = {}
    for base in (copy_record.root(), visible_root(scratch_settings=scratch_settings)):
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        listed = {path for path in entries if path.is_dir()} | set(marked_folders(entries))
        candidates.update(dict.fromkeys(sorted(listed)))
    candidates.update(dict.fromkeys((*copy_paths(), *pending_copy_paths(copy_record.copies_root()))))
    found: list[Folder] = []
    for path in candidates:
        record = task_of(path, scratch_settings=scratch_settings) if not path.is_symlink() else copy_record.NotRegistered()
        if isinstance(record, copy_record.Registered):
            try:
                modified = folder_record(path).stat().st_mtime
            except FileNotFoundError:
                continue
            found.append(Folder(path, record.task, modified))
    return found


def renew(path: Path, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    moment = time.time() if now is None else now

    def renewed(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, copy_record.Registered):
            raise ScratchError(f"{path} is no scratch folder")
        return {**record.fields, "renewed": moment}

    with copy_record_lock(path, scratch_settings=scratch_settings):
        _ = update_record(path, renewed, scratch_settings=scratch_settings)
        os.utime(path / copy_record.RECORD, (moment, moment))


@dataclass(frozen=True)
class SavedCargoOutput:
    """Output ownership saved before a checkout can disappear."""
    roots: cargo_output.CargoOutputRoots
    other_checkouts: tuple[Path, ...]
    shared_output_roots: tuple[Path, ...] = ()
    saved_at: float = 0.0

    def borrows_build_directory(self) -> bool:
        return any(self.roots.build_directory.is_relative_to(root) for root in self.shared_output_roots)


def cargo_removal_mark(path: Path) -> Path:
    return path.parent / f".scratch-cargo-{path.name}.json"


def cargo_checkout_peers(workspace: Path) -> tuple[Path, ...]:
    """Read related worktrees and registered scratch checkouts before source removal."""
    peers: set[Path] = set()
    result = _git(workspace, "worktree", "list", "--porcelain")
    if result.returncode == 0:
        peers.update(Path(line.removeprefix("worktree ")).resolve()
                     for line in result.stdout.splitlines() if line.startswith("worktree "))
    for folder in folders():
        peers.update(repo.resolve() for repo in repos(folder.path))
    peers.update(repo.resolve() for held in holds() for repo in repos(Path(held)))
    return tuple(sorted(peers - {workspace}))


def save_cargo_outputs(path: Path) -> tuple[SavedCargoOutput, ...]:
    """Save the mapping outside the deletion tree. Keep the first mapping on retries."""
    path = path.resolve()
    mark = cargo_removal_mark(path)
    if mark.exists():
        try:
            raw = cast(object, json.loads(mark.read_text()))
        except (ValueError, UnicodeError) as error:
            raise OSError(f"invalid Cargo removal record: {mark}: {error}") from error
        if not isinstance(raw, list):
            raise OSError(f"invalid Cargo removal record: {mark}")
        saved: list[SavedCargoOutput] = []
        for item in cast(list[object], raw):
            if not isinstance(item, dict):
                raise OSError(f"invalid Cargo removal record: {mark}")
            fields = cast(dict[str, object], item)
            values = [fields.get(key) for key in ("workspace_root", "target_directory", "build_directory")]
            peers = fields.get("other_checkouts")
            if (not all(isinstance(value, str) and Path(value).is_absolute() for value in values)
                    or not isinstance(peers, list) or not all(isinstance(peer, str) and Path(peer).is_absolute()
                                                                           for peer in cast(list[object], peers))):
                raise OSError(f"invalid Cargo removal record: {mark}")
            roots = cargo_output.CargoOutputRoots(*(Path(cast(str, value)).resolve() for value in values))
            if not roots.workspace_root.is_relative_to(path):
                raise OSError(f"Cargo workspace is outside removal tree: {mark}")
            saved_at = fields.get("saved_at", 0.0)
            if not isinstance(saved_at, (int, float)):
                raise OSError(f"invalid Cargo removal record: {mark}")
            target_link = roots.workspace_root / "target"
            legacy_shared = target_link.resolve() if target_link.is_symlink() else roots.target_directory
            shared = fields.get("shared_output_roots", [str(legacy_shared)]
                                if fields.get("shared_target") is True else [])
            if (not isinstance(shared, list)
                    or not all(isinstance(root, str) and Path(root).is_absolute()
                               for root in cast(list[object], shared))):
                raise OSError(f"invalid Cargo removal record: {mark}")
            saved.append(SavedCargoOutput(roots, tuple(Path(peer) for peer in cast(list[str], peers)),
                                          tuple(Path(root).resolve() for root in cast(list[str], shared)), float(saved_at)))
        return tuple(saved)
    found: list[SavedCargoOutput] = []
    checkouts = set(repos(path))
    if (path / "Cargo.toml").is_file():
        checkouts.add(path)
    for checkout in sorted(checkouts):
        if not (checkout / "Cargo.toml").is_file():
            continue
        roots = cargo_output.read_cargo_output_roots(checkout)
        if isinstance(roots, cargo_output.CargoOutputUnavailable):
            print(f"scratch: cannot read Cargo output for {checkout}: {roots.reason}", file=sys.stderr)
            continue
        if roots.workspace_root.is_relative_to(path) and roots not in [item.roots for item in found]:
            found.append(SavedCargoOutput(roots, cargo_checkout_peers(roots.workspace_root),
                                          ((checkout / "target").resolve(),) if (checkout / "target").is_symlink() else (),
                                          time.time()))
    if found:
        atomic_record(mark, [{"workspace_root": str(item.roots.workspace_root),
                             "target_directory": str(item.roots.target_directory),
                             "build_directory": str(item.roots.build_directory),
                             "other_checkouts": [str(peer) for peer in item.other_checkouts],
                             "shared_output_roots": [str(root) for root in item.shared_output_roots],
                             "saved_at": item.saved_at} for item in found])
    return tuple(found)


def clean_cargo_outputs(path: Path, saved: tuple[SavedCargoOutput, ...]) -> None:
    """Run the shared guard without importing the sweep back into scratch."""
    if not any(not item.roots.build_directory.is_relative_to(path.resolve())
               and item.roots.build_directory.exists() for item in saved):
        return
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "lint" / "folder_audit.py"),
                             "--clean-cargo-output", str(path.resolve())],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise OSError(result.stderr.strip() or "Cargo output cleanup failed")
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)


def remove_with_cargo_output(path: Path, delete: Callable[[Path], None]) -> None:
    """Keep the saved mapping until output and source deletion both finish."""
    path = path.absolute()
    if path.is_symlink():
        delete(path)
        return
    saved = save_cargo_outputs(path)
    clean_cargo_outputs(path, saved)
    delete(path)
    cargo_removal_mark(path.resolve()).unlink(missing_ok=True)


def remove(path: Path) -> None:
    """Delete one folder, its record last; a linked git worktree through its source repo first.
    Callers go through remove_marked, so a stop at any line leaves the task findable."""
    if (path / ".git").is_file():
        _ = subprocess.run(["git", "-C", str(path), "worktree", "remove", "--force", str(path)],
                           capture_output=True, check=False)
    if os.path.lexists(path):
        for child in sorted(path.iterdir(), key=lambda child: (child.name == copy_record.RECORD, child.name)):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        path.rmdir()


def remove_marked(path: Path, delete: Callable[[Path], None], *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    """Write the removal mark outside the tree before its first unlink, delete, then drop the mark.
    A rerun finds the task in the mark after the folder's record or the folder is gone. A copy
    needs a readable record; callers of a copy hold its removal lock. An unrecorded folder
    (a held one) gets no mark."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    def removing(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, copy_record.Registered):
            raise ScratchError(f"{path}: no readable record for removal")
        return dict(record.fields)

    copy = copy_record.copy_folder(path) == path.resolve()
    with copy_record_lock(path, scratch_settings=scratch_settings):
        pending = pending_removal(path, scratch_settings=scratch_settings)
        if isinstance(pending, copy_record.UnreadableRecord) and copy:
            raise ScratchError(pending.reason)
        if isinstance(pending, copy_record.NotRegistered) and (copy or isinstance(copy_record.read_record(path / copy_record.RECORD), copy_record.Registered)):
            pending = update_record(path, removing, removal=True, scratch_settings=scratch_settings)
        if isinstance(pending, copy_record.Registered):
            trust_folder = pending.fields.get("claude_trust_folder")
            if isinstance(trust_folder, str):
                from scripts.accounts import accounts

                accounts.set_claude_folder_trust(Path(trust_folder), trusted=False)
        remove_with_cargo_output(path, delete)
        removal_mark(path).unlink(missing_ok=True)


def clear_finished_copy_removals(base: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    """A stop after the tree went away may leave only its outside removal record."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    for path in pending_copy_paths(base):
        if not os.path.lexists(path) and isinstance(pending_removal(path, scratch_settings=scratch_settings), copy_record.Registered):
            with removal_lock(path) as idle:
                if idle and not os.path.lexists(path):
                    remove_with_cargo_output(path, lambda folder: None)
                    removal_mark(path).unlink(missing_ok=True)


def holds_file(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return Path(os.environ.get(HOLDS_ENV) or scratch_settings.text("holds_file")).expanduser()


def holds(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> dict[str, Hold]:
    """Every hold on this machine, by the held folder's real path."""
    path = holds_file(scratch_settings=scratch_settings)
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
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


def write_holds(found: dict[str, Hold], *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    path = holds_file(scratch_settings=scratch_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    _ = staged.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _ = staged.replace(path)


def hold(path: Path, task: str, reason: str, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    """Hold an existing folder for `task`; return the task."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if path.is_symlink() or not path.is_dir():
        raise ScratchError(f"{path} is no folder")
    if not reason.strip():
        raise ScratchError("a hold needs --reason")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    _ = checked_task(task, False)
    found = holds(scratch_settings=scratch_settings)
    found[os.path.realpath(path)] = Hold(task=task, maker=maker(), reason=reason.strip(), made=moment, ended=0.0)
    write_holds(found, scratch_settings=scratch_settings)
    return task


def release(path: Path, *, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> bool:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    found = holds(scratch_settings=scratch_settings)
    if found.pop(os.path.realpath(path), None) is None:
        return False
    write_holds(found, scratch_settings=scratch_settings)
    return True


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)


def repos(path: Path, *, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[Path]:
    """Find repositories to the configured depth. Exclude configured folder names and symbolic links."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return _repos(path, scratch_settings.whole("repo_depth"), scratch_settings.words("repo_skip"))


def _repos(path: Path, depth: int, skipped: tuple[str, ...]) -> list[Path]:
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
        if entry.name not in skipped and entry.is_dir() and not entry.is_symlink():
            found.extend(_repos(entry, depth - 1, skipped))
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


def unsaved_work(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str | None:
    """`<repo>: <why>` for the first repo in `path` whose work removal would lose, or None."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    for repo in repos(path, scratch_settings=scratch_settings):
        problem = repo_problem(repo)
        if problem is not None:
            return f"{repo}: {problem}"
    return None


def end(task: str, now: float | None = None, *, keep: Callable[[Path], bool] = lambda _: False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[Path]:
    """Remove every folder and held folder of the task. A held folder whose repo holds unsaved work
    stays, and its hold is marked ended. Folders selected by keep stay unchanged."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    _ = checked_task(task, False)
    removed: list[Path] = []
    moment = time.time() if now is None else now
    for folder in folders(scratch_settings=scratch_settings):
        if folder.task == task and not keep(folder.path):
            if copy_record.copy_folder(folder.path) is not None:
                if end_copy(folder.path, task, moment, scratch_settings=scratch_settings):
                    removed.append(folder.path)
            else:
                remove_marked(folder.path, remove, scratch_settings=scratch_settings)
                removed.append(folder.path)
    found = holds(scratch_settings=scratch_settings)
    changed = False
    for path, record in list(found.items()):
        if record["task"] != task or keep(Path(path)):
            continue
        changed = True
        held = Path(path)
        copies = Path(os.path.realpath(copy_record.copies_root()))
        if copies.is_relative_to(held.resolve()) or held.parent == copies:
            for copy in list(copy_paths()):
                if copy.is_relative_to(held) and not keep(copy) and end_copy(copy, task, moment, require_task=False, scratch_settings=scratch_settings):
                    removed.append(copy)
            clear_finished_copy_removals(copies, scratch_settings=scratch_settings)
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
        if os.path.lexists(held) and copy_record.copy_folder(held) is not None:
            if end_copy(held, task, moment, scratch_settings=scratch_settings):
                removed.append(held)
                del found[path]
            else:
                record["ended"] = moment
            continue
        if os.path.lexists(held) and unsaved_work(held, scratch_settings=scratch_settings) is not None:
            record["ended"] = moment
            continue
        if os.path.lexists(held):
            remove_marked(held, remove, scratch_settings=scratch_settings)
            removed.append(held)
        del found[path]
    if changed:
        write_holds(found, scratch_settings=scratch_settings)
    return removed


def end_selected_copy(path: Path, expected: copy_record.Registered, now: float,
                      *, validate: Callable[[], None]) -> bool:
    """Validate selected ownership while holding the removal and record locks."""
    with removal_lock(path) as idle:
        if not idle:
            return False
        if not os.path.lexists(path):
            pending = pending_removal(path)
            if not isinstance(pending, copy_record.Registered) or pending.fields != expected.fields:
                raise ScratchError(f"{path}: removal record changed; retry")
            validate()
            removal_mark(path).unlink(missing_ok=True)
            return True
        with copy_record_lock(path):
            current = task_of(path)
            if not isinstance(current, copy_record.Registered) or current.fields != expected.fields:
                raise ScratchError(f"{path}: owner, task or copy identity changed; retry")
            if "copy_device" in current.fields or "copy_inode" in current.fields:
                directory = path.stat()
                if (current.fields.get("copy_device"), current.fields.get("copy_inode")) != (directory.st_dev, directory.st_ino):
                    raise ScratchError(f"{path}: copy identity changed; retry")
            validate()
            return end_copy_locked(path, current.task, now, require_task=True, idle=True)


def end_copy(path: Path, task: str, now: float, *, require_task: bool = True,
             scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> bool:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    with removal_lock(path) as idle:
        if not os.path.lexists(path):
            pending = pending_removal(path)
            if (not idle or not isinstance(pending, copy_record.Registered)
                    or require_task and pending.task != task):
                return False
            removal_mark(path).unlink(missing_ok=True)
            return True
        with copy_record_lock(path, scratch_settings=scratch_settings):
            return end_copy_locked(path, task, now, require_task=require_task, idle=idle, scratch_settings=scratch_settings)


def end_copy_locked(path: Path, task: str, now: float, *, require_task: bool, idle: bool,
                    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> bool:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    """End and remove a copy while the caller holds its removal and record locks."""
    record = task_of(path, scratch_settings=scratch_settings)
    if not isinstance(record, copy_record.Registered) or require_task and record.task != task:
        return False
    pending = pending_removal(path, scratch_settings=scratch_settings)
    if record.task == task and isinstance(pending, copy_record.NotRegistered) and not task_has_end_evidence(record):
        def task_ended(current: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
            if not isinstance(current, copy_record.Registered) or current.task != task:
                raise ScratchError(f"{path}: task changed while recording its end")
            return {**current.fields, "ended": now, "ended_task": task}

        _ = update_record(path, task_ended, scratch_settings=scratch_settings)
    if not idle:
        return False
    state = record_lifetime(path, now, scratch_settings=scratch_settings)
    if not isinstance(state, Ended):
        return False
    if not isinstance(pending, copy_record.Registered) and unsaved_work(path, scratch_settings=scratch_settings) is not None:
        return False
    remove_marked(path, remove, scratch_settings=scratch_settings)
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
    scratch_settings = conf.read("scratch")
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
    for action in ("cargo-save", "cargo-clean"):
        _ = commands.add_parser(action).add_argument("path")
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
        if command in ("cargo-save", "cargo-clean"):
            path = Path(cast(str, arguments.path)).resolve()
            saved = save_cargo_outputs(path)
            if command == "cargo-clean":
                clean_cargo_outputs(path, saved)
                cargo_removal_mark(path).unlink(missing_ok=True)
        elif command == "new":
            print(new(chosen_task(arguments), cast(str, arguments.name), cast(bool, arguments.visible), scratch_settings=scratch_settings))
        elif command == "adopt":
            print(adopt(Path(cast(str, arguments.path)), chosen_task(arguments), cast(str, arguments.name),
                        cast(bool, arguments.visible), scratch_settings=scratch_settings))
        elif command == "hold":
            path = Path(cast(str, arguments.path))
            print(f"held {os.path.realpath(path)} for {hold(path, chosen_task(arguments), cast(str, arguments.reason), scratch_settings=scratch_settings)}")
        elif command == "release":
            path = Path(cast(str, arguments.path))
            print(f"released {path}" if release(path, scratch_settings=scratch_settings) else f"{path} has no hold")
        elif command == "renew":
            renew(Path(cast(str, arguments.path)), scratch_settings=scratch_settings)
        elif command == "hand-over":
            path = Path(cast(str, arguments.path))
            hand_over(path, cast(str, arguments.to), cast(str, arguments.task),
                      run_dir=cast(str, arguments.run_dir), land_target=cast(str, arguments.land_target), scratch_settings=scratch_settings)
            print(path)
        elif command == "end":
            for path in end(cast(str, arguments.task), scratch_settings=scratch_settings):
                print(f"removed {path}")
        else:
            now = time.time()
            for folder in folders(scratch_settings=scratch_settings):
                print(f"{folder.path}\t{folder.task}\t{(now - folder.changed) / 3600:.1f} h since a change")
            for path, record in sorted(holds(scratch_settings=scratch_settings).items()):
                state = "ended, unsaved work" if record["ended"] else "held"
                print(f"{path}\t{record['task']}\t{state}: {record['reason']}")
    except (ScratchError, OSError) as error:
        print(f"scratch: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
