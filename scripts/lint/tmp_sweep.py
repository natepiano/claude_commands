#!/usr/bin/env python3
"""Delete idle agent scratch under /tmp once an hour.

Agents leave shots, traces, gate copies and scratch builds in /tmp/claude and in
Claude Code's own /tmp/claude-<uid>/<project>/ folders, and nothing removed
them: on 2026-10-25 the two held 122 GiB and 86 GiB. sweep.py --floor-only, on
disk-floor.nix's 2-minute timer, calls run_if_due, which runs this sweep when
its last run started RUN_INTERVAL_SECONDS or more ago, and before the floor
takes any build cache. On the Mac, disk-floor does not run, and the tmp-sweep job
in nixos modules/darwin/tmp-sweep.nix runs `tmp_sweep.py` every hour.
`tmp_sweep.py [--dry-run]` runs it at once.

An entry is one top-level file, folder or link under a root: /tmp/claude,
/tmp/claude/delegate, /tmp/claude/unit, or a /tmp/claude-<uid>/-<project>/
folder of each /tmp/claude-<uid> this user owns, or the scratch root of scripts/lib/scratch.py
(/tmp/claude/scratch), where each task's folder is an entry. In a temp folder (/tmp, $TMPDIR
and the Mac's per-user temp folder) only a tempfile folder is an entry: ".tmp"
and 6 letters or digits, the name Rust's tempfile crate gives, which test runs
leave behind. Roots are real paths on both machines. An entry is deleted when
both are true:

  * nothing in it changed for IDLE_SECONDS. A change is the newer of mtime and
    ctime (a `cp -a` copy keeps its mtime but sets ctime), over every file,
    folder and link inside it; the entry's own times do not count, except for a
    file, a link, or a folder with nothing in it;
  * no process has its working folder, an open file or a mapped file inside it
    (one /proc scan per run; where no /proc exists, one lsof scan).

These stay too: the roots and their active/ run-marker folders; a unit run
folder whose own record names a running session: that session's marker in an
active/ folder, else the folder's `director` file, else the `run_started` event
that opens its history (unit_runs.director), so an idle run that no worker has
dispatched in yet stays too; a session folder named
for a running session, though an entry of SESSION_ENTRY_MIN_BYTES or more in
its folders (scratchpad/, tasks/) is judged like any other: a live session
keeps small files there that it only reads, and a read does not move mtime;
each of those when a session record cannot be read; a scratch folder of a change review
row, which its verdict removes (the user can take days to check a row);
and an entry that cannot be read. Links are never followed, a read-only folder
is made writable and deleted, and a project folder left empty goes. With
neither /proc nor lsof nothing goes.

Each run appends one JSON line to LOG_NAME in the lint-sweep state folder: the
entries deleted, the GiB freed, the count kept for each reason, and the paths
kept for each reason except UNLISTED_REASONS, which hold most entries. Lines older than LOG_KEEP_SECONDS
drop off. This replaced the 24-hour session scratch step (2026-10-21), whose
folders it also covers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import scratch  # noqa: E402
from user_zone import user_zone  # noqa: E402
if TYPE_CHECKING:
    from ..lib import unit_runs
    from ..message import sessions
else:
    import unit_runs
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
    import sessions

GIB = 1 << 30
HOUR_SECONDS = 3600
IDLE_SECONDS = 4 * HOUR_SECONDS
# Inside a running session's folders only an entry this large can go (showrunner, 2026-10-25).
SESSION_ENTRY_MIN_BYTES = GIB
RUN_INTERVAL_SECONDS = HOUR_SECONDS
LOG_KEEP_SECONDS = 7 * 24 * HOUR_SECONDS
TMP_ROOT = "/tmp/claude"
RUN_ROOTS = (os.path.join(TMP_ROOT, "delegate"), os.path.join(TMP_ROOT, "unit"))
# Claude Code keeps each session's scratch in /tmp/claude-<uid>/<project>/<session id>/.
SESSION_SCRATCH_PREFIX = "claude-"
RUN_MARKERS = "active"
PROC_DIR = "/proc"
# Where no /proc exists (the Mac), one lsof scan lists what each process uses.
LSOF = ("lsof", "-n", "-P", "-w", "-F", "pfn")
LSOF_TIMEOUT_SECONDS = 120
# lsof's fd names for a working folder and for mapped files (program text, memory maps).
LSOF_CWD = "cwd"
LSOF_MAPPED = ("txt", "mem")
# The folder name Rust's tempfile crate gives by default: ".tmp" and 6 random letters or digits.
# Test runs leave them behind in the temp folders (3,058 in natedev /tmp since 2026-09-06).
TEMPFILE_NAME = re.compile(r"\.tmp[A-Za-z0-9]{6}")
DARWIN_TEMP_NAME = "CS_DARWIN_USER_TEMP_DIR"
DELETED_SUFFIX = " (deleted)"
STATE_ENV = "LINT_SWEEP_STATE_DIR"
DEFAULT_STATE_DIR = os.path.join("~", ".local", "state", "lint-sweep")
LOG_NAME = "tmp-sweep.jsonl"
STAMP_NAME = "tmp-sweep.stamp"
DRY_RUN_FLAG = "--dry-run"

ROOT = "a root or a run-marker folder"
SESSION_RUNS = "its session runs"
DIRECTOR_RUNS = "its unit director runs"
ROW_WAITS = "its change review row waits for a verdict"
NO_SESSIONS = "a session record cannot be read"
IN_USE = "a process uses it"
RECENT = "changed in the last 4 h"
SMALL_IN_LIVE_SESSION = "under 1 GiB in a running session's folder"
UNLISTED_REASONS = (RECENT, SMALL_IN_LIVE_SESSION)
UNREADABLE = "it cannot be read"
NO_PROC = "no /proc or lsof to show which processes use it"


class DeletedEntry(TypedDict):
    path: str
    gib: float
    last_change: str


class FailedEntry(TypedDict):
    path: str
    error: str


class RunRecord(TypedDict):
    at: str
    dry_run: bool
    freed_gib: float
    deleted: list[DeletedEntry]
    failed: list[FailedEntry]
    kept: dict[str, int]
    kept_paths: dict[str, list[str]]


@dataclass(frozen=True)
class Roots:
    """The folders whose top-level entries the sweep judges, by kind. In a temp folder only a
    tempfile folder (TEMPFILE_NAME) is judged."""
    plain: tuple[str, ...]
    runs: tuple[str, ...]
    projects: tuple[str, ...]
    temp: tuple[str, ...] = ()
    scratch: tuple[str, ...] = ()

    def all(self) -> tuple[str, ...]:
        return self.plain + self.runs + self.projects + self.temp + self.scratch

    def entries(self, root: str) -> list[os.DirEntry[str]]:
        """The entries of root the sweep judges."""
        found = listing(root)
        if root not in self.temp:
            return found
        return [entry for entry in found
                if TEMPFILE_NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)]


@dataclass
class Sweep:
    deleted: list[DeletedEntry] = field(default_factory=list)
    failed: list[FailedEntry] = field(default_factory=list)
    kept: dict[str, list[str]] = field(default_factory=dict)
    freed: int = 0

    def keep(self, path: str, reason: str) -> None:
        self.kept.setdefault(reason, []).append(path)


def default_roots(tmp_root: str = TMP_ROOT, run_roots: Sequence[str] = RUN_ROOTS,
                  session_roots: Sequence[str] | None = None, temp_roots: Sequence[str] | None = None,
                  scratch_roots: Sequence[str] | None = None) -> Roots:
    """The same roots on natedev and the Mac, each by its real path: the Mac's /tmp is a link to
    /private/tmp, and lsof prints real paths."""
    sessions_at = session_scratch_roots() if session_roots is None else session_roots
    projects = tuple(entry.path for root in sessions_at for entry in listing(os.path.realpath(root))
                     if entry.name.startswith("-") and entry.is_dir(follow_symlinks=False))
    temp = temp_folders() if temp_roots is None else temp_roots
    task_roots = (str(scratch.root()),) if scratch_roots is None else scratch_roots
    return Roots((os.path.realpath(tmp_root),), tuple(os.path.realpath(root) for root in run_roots), projects,
                 tuple(os.path.realpath(root) for root in temp),
                 tuple(os.path.realpath(root) for root in task_roots if os.path.isdir(root)))


def session_scratch_roots() -> tuple[str, ...]:
    """Each /tmp/claude-<uid> folder this user owns."""
    owned: list[str] = []
    for entry in listing("/tmp"):
        if not entry.name.startswith(SESSION_SCRATCH_PREFIX) or not entry.name[len(SESSION_SCRATCH_PREFIX):].isdigit():
            continue
        try:
            if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_uid == os.getuid():
                owned.append(entry.path)
        except OSError:
            continue
    return tuple(owned)


def temp_folders() -> tuple[str, ...]:
    """/tmp, $TMPDIR and the Mac's per-user temp folder, each by its real path, once."""
    try:
        darwin = os.confstr(DARWIN_TEMP_NAME) or ""
    except (ValueError, OSError):
        darwin = ""
    candidates = ("/tmp", os.environ.get("TMPDIR", ""), darwin)
    return tuple(dict.fromkeys(os.path.realpath(path) for path in candidates if path and os.path.isdir(path)))


def listing(directory: str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError:
        return []


def running_session_ids() -> frozenset[str] | None:
    """The id of every session whose process runs; None when a session record cannot be read, since
    that session can be live."""
    registry = sessions.read_registry()
    if registry.unreadable:
        return None
    return frozenset(entry.record["sessionId"] for entry in registry.entries
                     if sessions.running_session(entry.record))


def process_paths(proc: str, prefix: str) -> list[tuple[str, int, str]] | None:
    """Each path under prefix that a process works in, holds open or maps, with its pid and how;
    None when proc does not exist. A process that ends mid-scan, or one this user cannot read, is
    passed over: another user's process does not work in an agent's scratch."""
    if not os.path.isdir(proc):
        return None
    found: list[tuple[str, int, str]] = []
    for entry in listing(proc):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cwd = os.readlink(os.path.join(entry.path, "cwd"))
        except OSError:
            continue
        if cwd.startswith(prefix):
            found.append((cwd, pid, "works in it"))
        fds = os.path.join(entry.path, "fd")
        for fd in listing(fds):
            try:
                target = os.readlink(fd.path)
            except OSError:
                continue
            if target.startswith(prefix):
                found.append((target.removesuffix(DELETED_SUFFIX), pid, "holds a file open in it"))
        try:
            with open(os.path.join(entry.path, "maps"), "rb") as handle:
                maps = handle.read()
        except OSError:
            continue
        for line in maps.decode(errors="replace").splitlines():
            parts = line.split(maxsplit=5)
            if len(parts) == 6 and parts[5].startswith(prefix):
                found.append((parts[5].removesuffix(DELETED_SUFFIX), pid, "maps a file in it"))
    return found


def lsof_paths(prefix: str) -> list[tuple[str, int, str]] | None:
    """process_paths from one lsof scan; None when lsof cannot run or prints nothing."""
    try:
        completed = subprocess.run(LSOF, capture_output=True, text=True, errors="replace",
                                   timeout=LSOF_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # lsof exits 1 when it cannot read some process; the lines it printed still hold.
    return parse_lsof(completed.stdout, prefix) if completed.stdout else None


def parse_lsof(text: str, prefix: str) -> list[tuple[str, int, str]]:
    """Each path under prefix in `lsof -F pfn` output, with its pid and how the process uses it."""
    found: list[tuple[str, int, str]] = []
    pid = 0
    how = ""
    for line in text.splitlines():
        field, value = line[:1], line[1:]
        if field == "p" and value.isdigit():
            pid = int(value)
        elif field == "f":
            how = ("works in it" if value == LSOF_CWD
                   else "maps a file in it" if value in LSOF_MAPPED else "holds a file open in it")
        elif field == "n" and pid and how and value.startswith(prefix):
            found.append((value, pid, how))
    return found


def entry_uses(paths: Sequence[tuple[str, int, str]], roots: Sequence[str]) -> dict[str, str]:
    """For each entry a process uses, under any root, how the first such process uses it."""
    uses: dict[str, str] = {}
    for path, pid, how in paths:
        for root in roots:
            if path.startswith(root + os.sep):
                name = path[len(root) + 1:].split(os.sep, 1)[0]
                _ = uses.setdefault(os.path.join(root, name), f"pid {pid} {how}")
    return uses


def last_change(path: str, since: float) -> tuple[float, int] | str:
    """The newest change inside path and its disk blocks, or why it stays: RECENT when something
    changed at or after since, UNREADABLE when a part of it could not be read. A folder's own times
    count only when it holds nothing. Symbolic links are never followed."""
    try:
        top = os.lstat(path)
    except OSError:
        return UNREADABLE
    own = max(top.st_mtime, top.st_ctime)
    if not stat.S_ISDIR(top.st_mode):
        return RECENT if own >= since else (own, top.st_blocks * 512)
    newest = 0.0
    total = 0
    seen: set[tuple[int, int]] = set()
    frontier = [path]
    while frontier:
        try:
            with os.scandir(frontier.pop()) as entries:
                listed = list(entries)
            for entry in listed:
                info = entry.stat(follow_symlinks=False)
                newest = max(newest, info.st_mtime, info.st_ctime)
                if newest >= since:
                    return RECENT
                if entry.is_dir(follow_symlinks=False):
                    frontier.append(entry.path)
                elif (info.st_dev, info.st_ino) not in seen:
                    seen.add((info.st_dev, info.st_ino))
                    total += info.st_blocks * 512
        except OSError:
            return UNREADABLE
    if not newest:
        return RECENT if own >= since else (own, 0)
    return newest, total


def delete_tree(path: str) -> None:
    """Delete a file, a link or a folder. A folder goes through shutil.rmtree, then once more after
    making each folder writable: a copy of a read-only tree (a nix store path, a Go module cache)
    keeps its modes, and rmtree cannot empty such a folder."""
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        for folder, _, _ in os.walk(path):
            os.chmod(folder, 0o700)
        shutil.rmtree(path)


def marked_runs(roots: Roots) -> dict[str, str]:
    """Each run folder a run marker names, by its real path, to the marker's session. A marker
    holds the run folder's path on its first line."""
    marked: dict[str, str] = {}
    for root in roots.runs:
        for marker in listing(os.path.join(root, RUN_MARKERS)):
            try:
                with open(marker.path, encoding="utf-8") as handle:
                    run = handle.readline().strip()
            except (OSError, UnicodeError):
                continue
            if run:
                marked[os.path.realpath(run)] = marker.name
    return marked


def when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, user_zone()).strftime("%Y-%m-%d %H:%M")


def owner_reason(entry: os.DirEntry[str], root: str, roots: Roots, live: frozenset[str] | None,
                 marked: dict[str, str]) -> str | None:
    """Why an entry stays whatever its age: it is a root or run-marker folder, or a running session
    owns it by the entry's own record."""
    if entry.path in roots.all() or (root in roots.runs and entry.name == RUN_MARKERS):
        return ROOT
    if root in roots.scratch:
        task = scratch.task_of(Path(entry.path))
        return ROW_WAITS if task is not None and task.startswith("row:") else None
    if root in roots.projects:
        if live is None:
            return NO_SESSIONS
        return SESSION_RUNS if entry.name in live else None
    if root in roots.runs and entry.is_dir(follow_symlinks=False):
        names = (marked.get(os.path.realpath(entry.path)), unit_runs.director(Path(entry.path)))
        owners = {name for name in names if name}
        if owners and live is None:
            return NO_SESSIONS
        return DIRECTOR_RUNS if live is not None and owners & live else None
    return None


def session_folders(roots: Roots, live: frozenset[str] | None) -> tuple[str, ...]:
    """Each folder inside a running session's folder (scratchpad/, tasks/): the session folder stays,
    and each entry of SESSION_ENTRY_MIN_BYTES or more in these is judged like any other."""
    if live is None:
        return ()
    return tuple(inner.path for project in roots.projects for session in listing(project)
                 if session.name in live and session.is_dir(follow_symlinks=False)
                 for inner in listing(session.path) if inner.is_dir(follow_symlinks=False))


def sweep(dry_run: bool, roots: Roots, now: float, proc: str = PROC_DIR) -> Sweep:
    """Delete every idle entry no process uses, and record why each other entry stays."""
    result = Sweep()
    if not roots.all():
        return result
    # The scan keeps only paths under the roots' common folder (/tmp), which entry_uses then sorts.
    prefix = os.path.join(os.path.commonpath(roots.all()), "")
    paths = process_paths(proc, prefix)
    if paths is None:
        paths = lsof_paths(prefix)
    if paths is None:
        for root in roots.all():
            for entry in roots.entries(root):
                result.keep(entry.path, NO_PROC)
        return result
    live = running_session_ids()
    inner = session_folders(roots, live)
    marked = marked_runs(roots)
    judged = roots.all() + inner
    uses = entry_uses(paths, judged)
    since = now - IDLE_SECONDS
    for root in judged:
        emptied = False
        for entry in roots.entries(root):
            reason = owner_reason(entry, root, roots, live, marked)
            if reason is None and entry.path in uses:
                result.keep(f"{entry.path}: {uses[entry.path]}", IN_USE)
                continue
            change = last_change(entry.path, since) if reason is None else reason
            if isinstance(change, str):
                result.keep(entry.path, change)
                continue
            newest, size = change
            if root in inner and size < SESSION_ENTRY_MIN_BYTES:
                result.keep(entry.path, SMALL_IN_LIVE_SESSION)
                continue
            if not dry_run:
                try:
                    delete_tree(entry.path)
                except OSError as error:
                    # Another sweep can delete the same entry first.
                    if os.path.lexists(entry.path):
                        result.failed.append(FailedEntry(path=entry.path, error=str(error)))
                    continue
                emptied = True
            result.freed += size
            result.deleted.append(DeletedEntry(path=entry.path, gib=round(size / GIB, 3),
                                               last_change=when(newest)))
        if emptied and root in roots.projects:
            try:
                os.rmdir(root)
            except OSError:
                pass
    return result


def state_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get(STATE_ENV) or DEFAULT_STATE_DIR))


def record(result: Sweep, dry_run: bool, now: float) -> RunRecord:
    return RunRecord(
        at=datetime.fromtimestamp(now, user_zone()).isoformat(timespec="seconds"),
        dry_run=dry_run,
        freed_gib=round(result.freed / GIB, 3),
        deleted=result.deleted,
        failed=result.failed,
        kept={reason: len(paths) for reason, paths in sorted(result.kept.items())},
        kept_paths={reason: sorted(paths) for reason, paths in sorted(result.kept.items()) if reason not in UNLISTED_REASONS},
    )


def append_log(path: Path, entry: RunRecord, now: float) -> None:
    """Add entry to the log and drop each line older than LOG_KEEP_SECONDS."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept = [line for line in lines if logged_at(line) >= now - LOG_KEEP_SECONDS]
    kept.append(json.dumps(entry, sort_keys=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    _ = staged.write_text("\n".join(kept) + "\n", encoding="utf-8")
    _ = staged.replace(path)


def logged_at(line: str) -> float:
    """A log line's time, or 0 for a line that is not a run record."""
    try:
        parsed = cast(object, json.loads(line))
    except json.JSONDecodeError:
        return 0.0
    if not isinstance(parsed, dict):
        return 0.0
    at = cast(dict[str, object], parsed).get("at")
    try:
        return datetime.fromisoformat(at).timestamp() if isinstance(at, str) else 0.0
    except ValueError:
        return 0.0


def summary(result: Sweep, dry_run: bool) -> str:
    verb = "would delete" if dry_run else "deleted"
    kept = "; ".join(f"{reason} {len(paths)}" for reason, paths in sorted(result.kept.items()))
    return (f"tmp sweep: {verb} {len(result.deleted)} entries ({result.freed / GIB:.1f} GiB),"
            + f" kept {sum(len(paths) for paths in result.kept.values())}: {kept or 'none'}")


def run(dry_run: bool, now: float | None = None, roots: Roots | None = None, proc: str = PROC_DIR) -> int:
    """Sweep once, print each deletion and a summary, and log the run unless it is a dry run."""
    moment = time.time() if now is None else now
    result = sweep(dry_run, roots or default_roots(), moment, proc)
    verb = "would delete" if dry_run else "deleted"
    for entry in result.deleted:
        print(f"tmp sweep: {verb} {entry['path']} ({entry['gib']:.1f} GiB), last changed {entry['last_change']}")
    for failure in result.failed:
        print(f"tmp sweep: could not delete {failure['path']}: {failure['error']}", file=sys.stderr)
    print(summary(result, dry_run))
    if not dry_run:
        append_log(state_dir() / LOG_NAME, record(result, dry_run, moment), moment)
    return 1 if result.failed else 0


def run_if_due(dry_run: bool, now: float | None = None) -> int:
    """Run the sweep when the last run started RUN_INTERVAL_SECONDS or more ago. The stamp moves
    before the run, so a run that fails waits an hour like any other."""
    moment = time.time() if now is None else now
    stamp = state_dir() / STAMP_NAME
    try:
        if stamp.stat().st_mtime > moment - RUN_INTERVAL_SECONDS:
            return 0
    except FileNotFoundError:
        pass
    if not dry_run:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (moment, moment))
    return run(dry_run, moment)


def main(argv: list[str]) -> int:
    if any(arg != DRY_RUN_FLAG for arg in argv):
        print(f"usage: tmp_sweep.py [{DRY_RUN_FLAG}]", file=sys.stderr)
        return 2
    return run(DRY_RUN_FLAG in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
