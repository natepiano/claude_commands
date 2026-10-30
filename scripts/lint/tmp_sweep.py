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
For hand cleanup use `tmp_sweep.py --plan <new-plan.json>` first; it lists every
candidate with keep/remove and a reason, writes the plan, and removes nothing.
Then use `tmp_sweep.py --apply <plan.json>` to recheck and remove only planned
paths. Hand cleanup also requires end evidence: all recorded workers ended, a
scratch task ended, or a recorded verify.sh run stopped. Age alone keeps a path.

An entry is one top-level file, folder or link under a root: /tmp/claude,
/tmp/claude/delegate, /tmp/claude/unit, or a /tmp/claude-<uid>/-<project>/
folder of each /tmp/claude-<uid> this user owns, or the scratch root of scripts/lib/scratch.py
(/tmp/claude/scratch), where each task's folder is an entry: it stays while its task is open, goes
when its until: time passes, and goes after TASK_IDLE_SECONDS whatever its task. Each running
session's temp root (SESSION_TEMP_ROOT/<session id>, its TMPDIR) and each run folder's tmp/ (the
run's TMPDIR) stays, and every entry in it is one too, judged by the 4 h rule: sessions run for
weeks, so nothing waits on a session end. In a temp folder a folder's ctime is no change, since
systemd-tmpfiles-clean moves it each day when it sets the folder's times back. In a temp folder (/tmp, $TMPDIR
and the Mac's per-user temp folder) only a tempfile folder is an entry, the
name a tempfile maker gives (TEMPFILE_NAME: Rust's tempfile crate, mktemp and
Python's tempfile), which test runs leave behind, and a lint/invoke.sh log
file (INVOKE_TEMP_NAME). A direct/verify.sh run folder (VERIFY_TEMP_NAME) is an entry
too: it stays while the run whose pid it names runs, and goes at once after. In /tmp alone (the
wide root) every other empty folder this user owns, also one that holds only folders, is an entry
too, whatever its name, and goes after WIDE_IDLE_SECONDS instead. A folder a system tool owns (SYSTEM_TEMP_NAMES,
SYSTEM_TEMP_PREFIXES; an empty EMPTY_PREFIXES one aside), another root and a
folder that holds one stay. A folder in /tmp that holds something is
folder_audit.py's, which checks it for unsaved git work first. Roots are real paths on both machines. An entry is deleted when
both are true:

  * nothing in it changed for IDLE_SECONDS. A change is the newer of mtime and
    ctime (a `cp -a` copy keeps its mtime but sets ctime), over every file,
    folder and link inside it; the entry's own times do not count, except for a
    file, a link, or a folder with nothing in it;
  * no process has its working folder, an open file or a mapped file inside it
    (one /proc scan per run; where no /proc exists, one lsof scan).

These stay too: runs named by active markers, runs with any worker not ended (including
paused workers), and runs using a live or paused session worktree. Unreadable ownership records
keep runs too, including their temp entries. These runs also keep folders named by cwd in their
mesh_runs/*.json records. Empty mesh_runs files are not records. Pending files are not read.
A worker thread not ended keeps its session temp parent, but its old temp entries can go.
An unreadable worker record keeps scratch entries and top-level /tmp/claude entries, and names
the record in the log. Before an idle entry can go, a git check searches
three folder levels without following links. Uncommitted work, a stash, commits absent from remote
and clone-source refs, or a failed git check keep it. Git commands have a timeout and never fetch.
Timed and hand cleanup use the same checks. The roots and their active/ run-marker folders stay; a unit run
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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from ..lib import process_start
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start

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
# An empty folder this user owns in /tmp goes after this long, whatever its name: test leftovers
# have names of their own (1,103 month-old ones went by hand on 2026-10-26, and the user had to
# approve the rm). A folder there that holds something is folder_audit.py's, which checks git first.
WIDE_IDLE_SECONDS = 7 * 24 * HOUR_SECONDS
# A scratch folder whose task is open goes after this long anyway: an ender can fail.
TASK_IDLE_SECONDS = 7 * 24 * HOUR_SECONDS
# Inside a running session's folders only an entry this large can go (showrunner, 2026-10-25).
SESSION_ENTRY_MIN_BYTES = GIB
RUN_INTERVAL_SECONDS = HOUR_SECONDS
LOG_KEEP_SECONDS = 7 * 24 * HOUR_SECONDS
TMP_ROOT = "/tmp/claude"
RUN_ROOTS = (os.path.join(TMP_ROOT, "delegate"), os.path.join(TMP_ROOT, "unit"))
# Each Claude session's TMPDIR is SESSION_TEMP_ROOT/<session id> (hooks/session-start-temp-root.py),
# and each run's is <run folder>/RUN_TEMP_NAME; the entries in both are judged with the 4 h rule.
SESSION_TEMP_ROOT = os.path.join(TMP_ROOT, "session")
RUN_TEMP_NAME = "tmp"
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
# The folder names tempfile makers give by default: Rust's tempfile crate ".tmp" and 6 letters or
# digits, mktemp "tmp." and 8 to 10, Python's tempfile "tmp" and 8 lower-case letters, digits or "_".
# Test runs leave them behind in the temp folders (3,058 Rust ones in natedev /tmp since 2026-09-06;
# 3,917 mktemp and Python ones on 2026-10-26).
# Folders in /tmp that system tools own; the 7-day rule never judges them, except an empty one with
# an EMPTY_PREFIXES name: nix-shell and nix develop leave theirs (1,321 on natedev, 2026-10-27).
SYSTEM_TEMP_NAMES = frozenset({"claude", ".X11-unix", ".ICE-unix", ".XIM-unix", ".font-unix", ".Test-unix"})
SYSTEM_TEMP_PREFIXES = ("systemd-private-", "tmux-", "nix-", "ssh-", "claude-", "com.apple.", "powerlog")
EMPTY_PREFIXES = ("nix-",)
# lint/invoke.sh's own logs in $TMPDIR or /tmp; a killed run leaves them (1,308 lint_invoke and
# 1,438 buildlog files in natedev /tmp on 2026-10-27).
INVOKE_TEMP_NAME = re.compile(r"lint_invoke\.\d+\.\d+\.log|buildlog\.\d+\.\d+\.(?:log|peak|peak\.cgroup)")
TEMPFILE_NAME = re.compile(r"\.tmp[A-Za-z0-9]{6}|tmp\.[A-Za-z0-9]{8,10}|tmp[a-z0-9_]{8}")
# direct/verify.sh's run folder (its copy folders sit inside it), named with the run's pid. Its EXIT
# trap removes it, but a killed run leaves it (47 hold and 6 copy folders in natedev /tmp, 2026-10-27).
# A named pid that is no verify.sh run started before the folder ends the folder at once; an old name
# with no pid waits out the 4 h rule.
VERIFY_TEMP_NAME = re.compile(r"verify-(?:hold|copy)\.(?:(\d+)\.)?[A-Za-z0-9]{6}")
VERIFY_SCRIPT = "verify.sh"
# ps prints elapsed time to the second, so a run's start can read up to this late.
START_SLACK_SECONDS = 2.0
# A folder in /tmp that holds only folders, however deep, is empty; a tree of more folders than this
# counts as holding something (155 nix-shell folders of empty folders on natedev, 2026-10-27).
EMPTY_TREE_FOLDERS = 1000
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
VERIFY_RUNS = "its verify.sh run is running"
RECENT = "changed in the last 4 h"
RECENT_WIDE = "changed in the last 7 days"
TASK_OPEN = "its task is open and it changed in the last 7 days"
SMALL_IN_LIVE_SESSION = "under 1 GiB in a running session's folder"
UNLISTED_REASONS = (RECENT, RECENT_WIDE, SMALL_IN_LIVE_SESSION, TASK_OPEN)
UNREADABLE = "it cannot be read"
WORKER_USES = "a live worker uses it"
UNSAVED_WORK = "it holds unsaved git work"
GIT_DEPTH = 3
GIT_TIMEOUT_SECONDS = 10
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
class Limit:
    """An entry goes when nothing in it changed at or after `since`; `recent` is why it stays when
    something did. Without `folder_ctime`, a folder's ctime is no change."""
    since: float
    recent: str
    folder_ctime: bool = True


@dataclass(frozen=True)
class Roots:
    """The folders whose top-level entries the sweep judges, by kind. In a temp folder only a
    tempfile folder (TEMPFILE_NAME) and an invoke.sh log (INVOKE_TEMP_NAME) are judged, and in a
    wide one an empty folder too."""
    plain: tuple[str, ...]
    runs: tuple[str, ...]
    projects: tuple[str, ...]
    temp: tuple[str, ...] = ()
    scratch: tuple[str, ...] = ()
    wide: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()

    def all(self) -> tuple[str, ...]:
        return self.plain + self.runs + self.projects + self.temp + self.scratch + self.sessions

    def entries(self, root: str) -> Iterator[os.DirEntry[str]]:
        """The entries of root the sweep judges, without retaining the directory."""
        for entry in listing(root):
            if root not in self.temp or self.judged_temp(root, entry):
                yield entry

    def judged_temp(self, root: str, entry: os.DirEntry[str]) -> bool:
        try:
            if entry.is_symlink():
                return False
            if entry.is_file():
                return INVOKE_TEMP_NAME.fullmatch(entry.name) is not None
            return entry.is_dir() and (TEMPFILE_NAME.fullmatch(entry.name) is not None
                                       or VERIFY_TEMP_NAME.fullmatch(entry.name) is not None
                                       or self.judged_empty(root, entry))
        except OSError:
            return False

    def is_wide(self, root: str, entry: os.DirEntry[str]) -> bool:
        """Whether entry goes only after WIDE_IDLE_SECONDS: an empty folder in a wide root that is
        no tempfile folder."""
        name = entry.name
        return root in self.wide and not any(pattern.fullmatch(name) for pattern in (TEMPFILE_NAME, INVOKE_TEMP_NAME, VERIFY_TEMP_NAME))

    def limit(self, root: str, entry: os.DirEntry[str], now: float, proc: str = PROC_DIR) -> Limit:
        """How long entry must be idle to go, and how a change in it counts."""
        verify = VERIFY_TEMP_NAME.fullmatch(entry.name) if root in self.temp else None
        if verify is not None and verify.group(1):
            try:
                made = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                made = now
            running = verify_running(int(verify.group(1)), made, now, proc)
            return Limit(float("-inf"), VERIFY_RUNS) if running else Limit(float("inf"), RECENT)
        if root in self.scratch:
            task = scratch.task_of(Path(entry.path))
            if task is not None and scratch.ended(task, now):
                return Limit(float("inf"), RECENT)
            if task is not None:
                return Limit(now - TASK_IDLE_SECONDS, TASK_OPEN)
        # systemd-tmpfiles-clean sets each folder's times back after it reads it, which moves the
        # folder's ctime, so in a temp folder only a folder's mtime counts.
        if self.is_wide(root, entry):
            return Limit(now - WIDE_IDLE_SECONDS, RECENT_WIDE, folder_ctime=False)
        return Limit(now - IDLE_SECONDS, RECENT, folder_ctime=root not in self.temp)

    def judged_empty(self, root: str, entry: os.DirEntry[str]) -> bool:
        """An empty folder of a wide root that this user owns, that no system tool owns (an
        EMPTY_PREFIXES name aside), and that is no other root."""
        name = entry.name
        if root not in self.wide or name in SYSTEM_TEMP_NAMES or (
                name.startswith(SYSTEM_TEMP_PREFIXES) and not name.startswith(EMPTY_PREFIXES)):
            return False
        if not holds_no_file(entry.path):
            return False
        inside = os.path.join(entry.path, "")
        if any(other == entry.path or other.startswith(inside) for other in self.all()):
            return False
        try:
            return entry.stat(follow_symlinks=False).st_uid == os.getuid()
        except OSError:
            return False


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
                  scratch_roots: Sequence[str] | None = None, wide_roots: Sequence[str] | None = None,
                  session_temp_roots: Sequence[str] | None = None) -> Roots:
    """The same roots on natedev and the Mac, each by its real path: the Mac's /tmp is a link to
    /private/tmp, and lsof prints real paths. The wide root is /tmp alone: $TMPDIR and the Mac's
    temp folder hold app state, and macOS clears its own."""
    sessions_at = session_scratch_roots() if session_roots is None else session_roots
    projects = tuple(entry.path for root in sessions_at for entry in listing(os.path.realpath(root))
                     if entry.name.startswith("-") and entry.is_dir(follow_symlinks=False))
    temp = temp_folders() if temp_roots is None else temp_roots
    task_roots = (str(scratch.root()),) if scratch_roots is None else scratch_roots
    temp_real = tuple(os.path.realpath(root) for root in temp)
    wide = ("/tmp",) if wide_roots is None else wide_roots
    session_temp = (SESSION_TEMP_ROOT,) if session_temp_roots is None else session_temp_roots
    return Roots((os.path.realpath(tmp_root),), tuple(os.path.realpath(root) for root in run_roots), projects,
                 temp_real, tuple(os.path.realpath(root) for root in task_roots if os.path.isdir(root)),
                 tuple(real for root in wide if (real := os.path.realpath(root)) in temp_real),
                 tuple(os.path.realpath(root) for root in session_temp if os.path.isdir(root)))


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


def holds_no_file(folder: str) -> bool:
    """Whether folder holds only folders, however deep (or nothing). A link, a socket or a tree
    of more than EMPTY_TREE_FOLDERS folders holds something; links are never followed."""
    frontier = [folder]
    walked = 0
    while frontier:
        walked += 1
        if walked > EMPTY_TREE_FOLDERS:
            return False
        try:
            with os.scandir(frontier.pop()) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        return False
                    frontier.append(entry.path)
        except OSError:
            return False
    return True


def verify_running(pid: int, made: float, now: float, proc: str = PROC_DIR) -> bool:
    """Whether pid is a verify.sh run that started before made, the folder's mtime: a pid reused by
    another process, or by a later run, is not the run that made the folder."""
    _ = now
    root = Path(proc)
    try:
        start = process_start.process_start(pid, root)
        if isinstance(start, process_start.ProcessGone):
            return False
        instant = process_start.started_at(start, root).timestamp()
        if root.is_dir():
            arguments = (root / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
        else:
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                     capture_output=True, text=True, check=False, timeout=10)
            if command.returncode or command.stderr.strip():
                return True  # Unknown ownership keeps the scratch folder.
            arguments = command.stdout.split()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return True  # A read error cannot prove that the run ended.
    return any(os.path.basename(argument) == VERIFY_SCRIPT for argument in arguments) and instant <= made + START_SLACK_SECONDS


def listing(directory: str) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            yield from entries
    except OSError:
        return


def running_session_ids() -> frozenset[str] | None:
    """The id of every session whose process runs; None when a session record cannot be read, since
    that session can be live."""
    registry = sessions.read_registry()
    if registry.unreadable:
        return None
    return frozenset(entry.record["sessionId"] for entry in registry.entries
                     if sessions.running_session(entry.record))


@dataclass(frozen=True)
class ProcessUsesKnown:
    """A completed scan of process paths, including a scan with no matches."""
    paths: list[tuple[str, int, str]]


@dataclass(frozen=True)
class ProcessUsesUnavailable:
    """Process use cannot be checked; candidates must stay."""


def process_paths(proc: str, prefix: str | tuple[str, ...]) -> ProcessUsesKnown | ProcessUsesUnavailable:
    """Each path under prefix that a process works in, holds open or maps, with its pid and how;
    unavailable when proc does not exist. A process that ends mid-scan, or one this user cannot read, is
    passed over: another user's process does not work in an agent's scratch."""
    if not os.path.isdir(proc):
        return ProcessUsesUnavailable()
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
            with open(os.path.join(entry.path, "maps"), encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    parts = line.rstrip("\n").split(maxsplit=5)
                    if len(parts) == 6 and parts[5].startswith(prefix):
                        found.append((parts[5].removesuffix(DELETED_SUFFIX), pid, "maps a file in it"))
        except OSError:
            continue
    return ProcessUsesKnown(found)


def lsof_paths(prefix: str | tuple[str, ...]) -> ProcessUsesKnown | ProcessUsesUnavailable:
    """Process paths from one lsof scan; unavailable when lsof cannot run or prints nothing."""
    try:
        completed = subprocess.run(LSOF, capture_output=True, text=True, errors="replace",
                                   timeout=LSOF_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ProcessUsesUnavailable()
    # lsof exits 1 when it cannot read some process; the lines it printed still hold.
    return ProcessUsesKnown(parse_lsof(completed.stdout, prefix)) if completed.stdout else ProcessUsesUnavailable()


def parse_lsof(text: str, prefix: str | tuple[str, ...]) -> list[tuple[str, int, str]]:
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


def last_change(path: str, limit: Limit) -> tuple[float, int] | str:
    """The newest change inside path and its disk blocks, or why it stays: limit.recent when
    something changed at or after limit.since, UNREADABLE when a part of it could not be read. A
    folder's own times count only when it holds nothing. Symbolic links are never followed."""
    since, recent = limit.since, limit.recent
    try:
        top = os.lstat(path)
    except OSError:
        return UNREADABLE
    folder = stat.S_ISDIR(top.st_mode)
    own = max(top.st_mtime, top.st_ctime if limit.folder_ctime or not folder else 0.0)
    if not folder:
        return recent if own >= since else (own, top.st_blocks * 512)
    newest = 0.0
    total = 0
    seen: set[tuple[int, int]] = set()
    frontier = [path]
    while frontier:
        try:
            with os.scandir(frontier.pop()) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    folder = stat.S_ISDIR(info.st_mode)
                    newest = max(newest, info.st_mtime, info.st_ctime if limit.folder_ctime or not folder else 0.0)
                    if newest >= since:
                        return recent
                    if folder:
                        frontier.append(entry.path)
                    elif info.st_nlink <= 1:
                        total += info.st_blocks * 512
                    elif (info.st_dev, info.st_ino) not in seen:
                        # Only hard links need an identity retained across the scan.
                        seen.add((info.st_dev, info.st_ino))
                        total += info.st_blocks * 512
        except OSError:
            return UNREADABLE
    if not newest:
        return recent if own >= since else (own, 0)
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


class RunKeepReason(Enum):
    ACTIVE_MARKER = "an active-run marker names it"
    UNENDED_WORKER = "a worker has not ended"
    SESSION_WORKTREE = "a live or paused session uses its worktree"
    UNREADABLE_RECORD = "a run ownership record cannot be read"
    DIRECTOR = DIRECTOR_RUNS


class MissingRecord(Enum):
    ABSENT = "record absent"


def record_object(path: Path) -> dict[str, object] | MissingRecord:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError(f"{path}: broken record link") from None
        return MissingRecord.ABSENT
    raw = cast(object, json.loads(text))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected an object")
    return cast(dict[str, object], raw)


def record_entries(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except FileNotFoundError:
        if directory.is_symlink():
            raise ValueError(f"{directory}: broken record link") from None
        return []


def marked_runs(roots: Roots) -> dict[str, str]:
    """Markers protect their run even when the director is paused. Read errors reach the guard."""
    marked: dict[str, str] = {}
    for root in roots.runs:
        for marker in record_entries(Path(root) / RUN_MARKERS):
            with marker.open(encoding="utf-8") as handle:
                run = handle.readline().strip()
            if not run or not os.path.isabs(run):
                raise ValueError(f"{marker}: expected a run path")
            marked[os.path.realpath(run)] = marker.name
    return marked


@dataclass(frozen=True)
class SessionRunUse:
    worktrees: frozenset[str]
    runs: frozenset[str]


def session_run_use(live: frozenset[str]) -> SessionRunUse:
    """Read registry cwd and the mover's shutdown session/legacy pause records without writing."""
    registry = sessions.read_registry()
    if registry.unreadable:
        raise ValueError("session registry unreadable")
    worktrees: set[str] = set()
    for item in registry.entries:
        cwd = cast(object, item.details.get("cwd", ""))
        if not isinstance(cwd, str) or (cwd and not os.path.isabs(cwd)):
            raise ValueError("session cwd unreadable")
        if item.record["sessionId"] in live and cwd:
            worktrees.add(os.path.realpath(cwd))
    runs: set[str] = set()

    def add_session(value: object) -> None:
        if not isinstance(value, dict):
            raise ValueError("shutdown session unreadable")
        session = cast(dict[str, object], value)
        cwd, run_dirs = session.get("cwd"), session.get("run_dirs")
        if not isinstance(cwd, str) or not isinstance(run_dirs, list):
            raise ValueError("shutdown session has no cwd or run_dirs")
        if cwd:
            if not os.path.isabs(cwd):
                raise ValueError("shutdown session cwd unreadable")
            worktrees.add(os.path.realpath(cwd))
        for run in cast(list[object], run_dirs):
            if not isinstance(run, str) or not os.path.isabs(run):
                raise ValueError("shutdown session has an invalid run path")
            runs.add(os.path.realpath(run))

    def add_pause(record: dict[str, object]) -> None:
        progress = record.get("progress")
        if not isinstance(progress, dict) or not isinstance(cast(dict[str, object], progress).get("kind"), str):
            raise ValueError("shutdown progress unreadable")
        if cast(dict[str, object], progress)["kind"] not in ("restarted", "timers pending"):
            add_session(record.get("session"))

    state = Path(os.environ.get("SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")))
    for account in record_entries(state):
        if not account.is_dir():
            continue
        for path in record_entries(account / "sessions"):
            if path.suffix == ".json":
                record = record_object(path)
                if isinstance(record, MissingRecord):
                    raise ValueError(f"{path}: record disappeared")
                add_pause(record)
        # The mover still reads pre-session account records and partial shutdowns.
        legacy = [account / "record.json"] + [path for path in record_entries(account / "partial")
                                             if path.suffix == ".json"]
        for path in legacy:
            record = record_object(path)
            if isinstance(record, MissingRecord):
                continue
            if record.get("state") in ("up", "cancelled"):
                continue
            entries = record.get("entries")
            if not isinstance(entries, list):
                raise ValueError(f"{path}: shutdown entries unreadable")
            for entry in cast(list[object], entries):
                if not isinstance(entry, dict):
                    raise ValueError(f"{path}: shutdown entry unreadable")
                add_pause(cast(dict[str, object], entry))
    return SessionRunUse(frozenset(worktrees), frozenset(runs))


def run_folder_guards(roots: Roots, live: frozenset[str] | None,
                      marked: dict[str, str]) -> dict[str, RunKeepReason]:
    folders = [entry.path for root in roots.runs for entry in listing(root)
               if entry.name != RUN_MARKERS and entry.is_dir(follow_symlinks=False)]
    try:
        if live is None:
            raise ValueError("session registry unreadable")
        use = session_run_use(live)
    except (OSError, UnicodeError, ValueError):
        return dict.fromkeys(folders, RunKeepReason.UNREADABLE_RECORD)
    kept: dict[str, RunKeepReason] = {}
    for folder in folders:
        try:
            roster = record_object(Path(folder) / "mesh_roster.json")
            if not isinstance(roster, MissingRecord):
                for worker in roster.values():
                    if not isinstance(worker, dict) or not isinstance(cast(dict[str, object], worker).get("status"), str):
                        raise ValueError("worker roster entry unreadable")
                    if cast(dict[str, object], worker)["status"] != "ended":
                        kept[folder] = RunKeepReason.UNENDED_WORKER
            # Read even when another reason keeps the folder, so errors are visible.
            state = record_object(Path(folder) / unit_runs.STATE_FILE)
            if not isinstance(state, MissingRecord):
                working = state.get("working_dir", "")
                if not isinstance(working, str):
                    raise ValueError("run worktree unreadable")
                if working and os.path.realpath(working) in use.worktrees:
                    kept[folder] = RunKeepReason.SESSION_WORKTREE
            if os.path.realpath(folder) in use.runs:
                kept[folder] = RunKeepReason.SESSION_WORKTREE
            if os.path.realpath(folder) in marked:
                kept[folder] = RunKeepReason.ACTIVE_MARKER
            # director() handles the history fallback; check its inputs before that permissive reader.
            director = Path(folder) / unit_runs.DIRECTOR_FILE
            if os.path.lexists(director) and not director.read_text(encoding="utf-8").strip():
                raise ValueError("run director unreadable")
            if folder not in kept and unit_runs.director(Path(folder)) in live:
                kept[folder] = RunKeepReason.DIRECTOR
            if not isinstance(state, MissingRecord) and state.get("history_file"):
                history = state["history_file"]
                if not isinstance(history, str):
                    raise ValueError("run history path unreadable")
                with Path(history).open(encoding="utf-8") as handle:
                    event = cast(object, json.loads(handle.readline()))
                if not isinstance(event, dict):
                    raise ValueError("run history unreadable")
        except (OSError, UnicodeError, ValueError):
            kept[folder] = RunKeepReason.UNREADABLE_RECORD
    return kept


@dataclass(frozen=True)
class WorkerUse:
    folders: frozenset[str]
    threads: frozenset[str]
    unreadable_records: tuple[str, ...]


def worker_use(guarded: dict[str, RunKeepReason]) -> WorkerUse:
    """Read working folders and live thread ids only from runs that must stay."""
    folders: set[str] = set()
    threads: set[str] = set()
    unreadable: list[str] = []

    for folder in guarded:
        roster_path = Path(folder) / "mesh_roster.json"
        try:
            roster = record_object(roster_path)
            if not isinstance(roster, MissingRecord):
                for value in roster.values():
                    if not isinstance(value, dict):
                        raise ValueError("worker roster unreadable")
                    worker = cast(dict[str, object], value)
                    if worker.get("status") != "ended":
                        thread = worker.get("thread_id", "")
                        if not isinstance(thread, str):
                            raise ValueError("worker thread id unreadable")
                        if thread:
                            threads.add(thread)
        except (OSError, UnicodeError, ValueError):
            unreadable.append(str(roster_path))
        runs_path = Path(folder) / "mesh_runs"
        try:
            for path in record_entries(runs_path):
                if path.suffix != ".json":
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                    if not text:
                        continue
                    record = cast(object, json.loads(text))
                    if not isinstance(record, dict):
                        continue
                    cwd = cast(dict[str, object], record).get("cwd", "")
                    if not isinstance(cwd, str) or (cwd and not os.path.isabs(cwd)):
                        raise ValueError("worker working folder unreadable")
                    if cwd:
                        folders.add(os.path.realpath(cwd))
                except (OSError, UnicodeError, ValueError):
                    unreadable.append(str(path))
        except (OSError, UnicodeError, ValueError):
            unreadable.append(str(runs_path))
    return WorkerUse(frozenset(folders), frozenset(threads), tuple(unreadable))


class GitWork(Enum):
    SAVED = "all git work is saved"
    UNSAVED = UNSAVED_WORK
    UNREADABLE = UNREADABLE


def checked_git(repo: Path, *arguments: str) -> str:
    """Read git state without optional locks or a network request."""
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True,
                            check=False, timeout=GIT_TIMEOUT_SECONDS,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise ValueError(f"{repo}: git {arguments[0]} failed")
    return result.stdout


def repo_git_work(repo: Path) -> GitWork:
    if checked_git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        return GitWork.UNSAVED
    refs = checked_git(repo, "for-each-ref", "--format=%(refname)")
    linked = (repo / ".git").is_file()
    if not linked and "refs/stash" in refs.splitlines():
        return GitWork.UNSAVED
    # An empty repository has no commit to lose. A broken HEAD fails closed below.
    head = checked_git(repo, "rev-parse", "--git-path", "HEAD").strip()
    head_path = Path(head) if os.path.isabs(head) else repo / head
    head_text = head_path.read_text(encoding="utf-8").strip()
    if not refs.strip() and head_text.startswith("ref: "):
        return GitWork.SAVED
    sources: list[str] = []
    for remote in checked_git(repo, "remote").splitlines():
        url = checked_git(repo, "remote", "get-url", remote).strip()
        if url.startswith("file://"):
            parsed = urlparse(url)
            if parsed.netloc not in ("", "localhost"):
                continue
            url = unquote(parsed.path)
        elif ":" in url and not os.path.isabs(url):
            continue
        source = Path(url) if os.path.isabs(url) else repo / url
        sources.extend(checked_git(source, "for-each-ref", "--format=%(objectname)", "refs/heads/").splitlines())
    tips = ["HEAD"] if linked else ["HEAD", "--branches", "--tags"]
    remaining = checked_git(repo, "-c", "core.alternateRefsPrefixes=refs/heads/", "rev-list", "-n1",
                            *tips, "--not", "--remotes", "--alternate-refs", *sources)
    return GitWork.UNSAVED if remaining.strip() else GitWork.SAVED


def git_work(path: Path, depth: int = GIT_DEPTH) -> GitWork:
    """Check all worktrees down to a fixed depth. Never follow links."""
    try:
        if path.is_symlink() or not path.is_dir():
            return GitWork.SAVED
        if (path / ".git").is_symlink():
            return GitWork.UNREADABLE
        if os.path.lexists(path / ".git"):
            result = repo_git_work(path)
            if result is not GitWork.SAVED:
                return result
        if depth:
            for entry in path.iterdir():
                if entry.name == ".git":
                    continue
                result = git_work(entry, depth - 1)
                if result is not GitWork.SAVED:
                    return result
        return GitWork.SAVED
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        return GitWork.UNREADABLE


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
    if root in roots.projects or root in roots.sessions:
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


def temp_parents(roots: Roots, live: frozenset[str] | None) -> tuple[str, ...]:
    """Each running session's temp root and each run folder's temp folder: the parent stays, and
    every entry in it is judged with the 4 h rule. Sessions run for weeks, so these entries are what
    goes; the parent goes only once its session or director stops."""
    sessions = () if live is None else tuple(
        entry.path for root in roots.sessions for entry in listing(root)
        if entry.name in live and entry.is_dir(follow_symlinks=False))
    runs = tuple(temp for root in roots.runs for entry in listing(root)
                 if entry.is_dir(follow_symlinks=False) and os.path.isdir(temp := os.path.join(entry.path, RUN_TEMP_NAME))
                 and not os.path.islink(temp))
    return sessions + runs


@dataclass(frozen=True)
class KeptEntry:
    path: str
    reason: str
    use: str = ""


@dataclass(frozen=True)
class RemovableEntry:
    path: str
    root: str
    newest: float
    size: int


def judge_entries(roots: Roots, now: float, proc: str) -> Iterator[KeptEntry | RemovableEntry]:
    """Apply the ownership, use and idle checks shared by timed and hand cleanup."""
    if not roots.all():
        return
    # The scan keeps only paths under the roots' common folder (/tmp), which entry_uses then sorts.
    prefix = os.path.join(os.path.commonpath(roots.all()), "")
    paths = process_paths(proc, prefix)
    if isinstance(paths, ProcessUsesUnavailable):
        paths = lsof_paths(prefix)
    if isinstance(paths, ProcessUsesUnavailable):
        for root in roots.all():
            for entry in roots.entries(root):
                yield KeptEntry(entry.path, NO_PROC)
        return
    live = running_session_ids()
    inner = session_folders(roots, live)
    try:
        marked = marked_runs(roots)
        guarded = run_folder_guards(roots, live, marked)
    except (OSError, UnicodeError, ValueError):
        marked = {}
        guarded = {entry.path: RunKeepReason.UNREADABLE_RECORD for root in roots.runs
                   for entry in listing(root) if entry.name != RUN_MARKERS}
    workers = worker_use(guarded)
    for path in workers.unreadable_records:
        print(f"tmp sweep: {path}: {UNREADABLE}; keeping scratch and top-level /tmp/claude entries", file=sys.stderr)
    temp_live = None if live is None else live | workers.threads
    judged = roots.all() + inner + temp_parents(roots, temp_live)
    uses = entry_uses(paths.paths, judged)
    for root in judged:
        for entry in roots.entries(root):
            reason = owner_reason(entry, root, roots, temp_live, marked)
            # Apply to every deletion path, including entries inside a protected run's tmp/.
            if reason != ROOT:
                # Resolve only the parents: a link inside a protected run is judged where it lies.
                path = os.path.join(os.path.realpath(os.path.dirname(entry.path)), entry.name)
                for folder, guard in guarded.items():
                    if path == folder or path.startswith(folder + os.sep) or folder.startswith(path + os.sep):
                        reason = NO_SESSIONS if live is None else guard.value
                        break
                if any(path == folder or folder.startswith(path + os.sep) for folder in workers.folders):
                    reason = WORKER_USES
                elif workers.unreadable_records and root in roots.scratch + roots.plain and reason is None:
                    reason = UNREADABLE
            if reason is None and entry.path in uses:
                yield KeptEntry(entry.path, IN_USE, uses[entry.path])
                continue
            change = last_change(entry.path, roots.limit(root, entry, now, proc)) if reason is None else reason
            if isinstance(change, str):
                yield KeptEntry(entry.path, change)
                continue
            newest, size = change
            if root in inner and size < SESSION_ENTRY_MIN_BYTES:
                yield KeptEntry(entry.path, SMALL_IN_LIVE_SESSION)
                continue
            git = git_work(Path(entry.path))
            if git is not GitWork.SAVED:
                yield KeptEntry(entry.path, git.value)
                continue
            yield RemovableEntry(entry.path, root, newest, size)


def remove_entry(entry: RemovableEntry, result: Sweep, dry_run: bool) -> bool:
    if not dry_run:
        try:
            delete_tree(entry.path)
        except OSError as error:
            if os.path.lexists(entry.path):
                result.failed.append(FailedEntry(path=entry.path, error=str(error)))
            return False
    result.freed += entry.size
    result.deleted.append(DeletedEntry(path=entry.path, gib=round(entry.size / GIB, 3),
                                       last_change=when(entry.newest)))
    return True


def sweep(dry_run: bool, roots: Roots, now: float, proc: str = PROC_DIR) -> Sweep:
    """Delete every idle entry no process uses, and record why each other entry stays."""
    result = Sweep()
    emptied: set[str] = set()
    removals: list[RemovableEntry] = []
    for entry in judge_entries(roots, now, proc):
        if isinstance(entry, KeptEntry):
            path = f"{entry.path}: {entry.use}" if entry.use else entry.path
            result.keep(path, entry.reason)
        else:
            removals.append(entry)
    # A linked worktree can use another candidate's git metadata. Finish all
    # checks before removing any candidate, retaining only removal decisions.
    for entry in removals:
        if remove_entry(entry, result, dry_run) and not dry_run:
            emptied.add(entry.root)
    for root in emptied.intersection(roots.projects):
        try:
            os.rmdir(root)
        except OSError:
            pass
    return result


class CleanupEndReason(Enum):
    WORKERS_ENDED = "all recorded workers ended; idle and unused"
    TASK_ENDED = "its scratch task ended; idle and unused"
    VERIFY_STOPPED = "its recorded verify.sh run stopped; idle and unused"


class CleanupKeepReason(Enum):
    NO_END = "no recorded end permits hand cleanup"
    UNREADABLE = "its end record cannot be read"


def cleanup_end_reason(entry: RemovableEntry, roots: Roots, now: float, proc: str) -> CleanupEndReason | CleanupKeepReason:
    """Require end evidence as well as the timed sweep's checks."""
    try:
        for root in roots.runs:
            relative = Path(entry.path).relative_to(root) if Path(entry.path).is_relative_to(root) else Path()
            if relative.parts:
                roster = record_object(Path(root) / relative.parts[0] / "mesh_roster.json")
                if isinstance(roster, dict) and roster and all(
                        isinstance(worker, dict) and cast(dict[str, object], worker).get("status") == "ended"
                        for worker in roster.values()):
                    return CleanupEndReason.WORKERS_ENDED
        if entry.root in roots.scratch:
            task = scratch.task_of(Path(entry.path))
            if task is not None and scratch.ended(task, now):
                return CleanupEndReason.TASK_ENDED
        verify = VERIFY_TEMP_NAME.fullmatch(Path(entry.path).name) if entry.root in roots.temp else None
        if verify is not None and verify.group(1):
            made = os.stat(entry.path, follow_symlinks=False).st_mtime
            if not verify_running(int(verify.group(1)), made, now, proc):
                return CleanupEndReason.VERIFY_STOPPED
    except (OSError, UnicodeError, ValueError):
        return CleanupKeepReason.UNREADABLE
    return CleanupKeepReason.NO_END


class PlanEntry(TypedDict):
    path: str
    action: str
    reason: str


@dataclass(frozen=True)
class CleanupRemoval:
    entry: RemovableEntry
    reason: CleanupEndReason


def cleanup_entries(roots: Roots, now: float, proc: str) -> list[KeptEntry | CleanupRemoval]:
    result: list[KeptEntry | CleanupRemoval] = []
    for entry in judge_entries(roots, now, proc):
        if isinstance(entry, KeptEntry):
            result.append(entry)
            continue
        reason = cleanup_end_reason(entry, roots, now, proc)
        if isinstance(reason, CleanupKeepReason):
            result.append(KeptEntry(entry.path, reason.value))
        else:
            result.append(CleanupRemoval(entry, reason))
    return result


def write_cleanup_plan(plan: Path, roots: Roots, now: float, proc: str) -> int:
    rows: list[PlanEntry] = []
    for entry in cleanup_entries(roots, now, proc):
        action = "keep" if isinstance(entry, KeptEntry) else "remove"
        reason = entry.reason if isinstance(entry, KeptEntry) else entry.reason.value
        path = entry.path if isinstance(entry, KeptEntry) else entry.entry.path
        rows.append(PlanEntry(path=path, action=action, reason=reason))
    # An existing plan is never replaced; apply uses exactly the reviewed remove list.
    with plan.open("x", encoding="utf-8") as handle:
        _ = handle.write(json.dumps({"version": 1, "entries": rows}, indent=2) + "\n")
    for row in rows:
        print(f"{row['action']} {row['path']}: {row['reason']}")
    print(f"tmp sweep: plan {plan}")
    return 0


def planned_removals(plan: Path) -> list[str]:
    record = record_object(plan)
    if not isinstance(record, dict) or record.get("version") != 1 or not isinstance(record.get("entries"), list):
        raise ValueError("invalid cleanup plan")
    paths: list[str] = []
    for value in cast(list[object], record["entries"]):
        if not isinstance(value, dict):
            raise ValueError("invalid cleanup plan entry")
        row = cast(dict[str, object], value)
        path, action = row.get("path"), row.get("action")
        if not isinstance(path, str) or not os.path.isabs(path) or action not in ("keep", "remove"):
            raise ValueError("invalid cleanup plan path or action")
        if action == "remove" and path not in paths:
            paths.append(path)
    return paths


def apply_cleanup_plan(plan: Path, roots: Roots, now: float, proc: str) -> int:
    result = Sweep()
    removals = planned_removals(plan)
    # Judge every candidate again once, after the plan; the timed sweep has the same window.
    current = {entry.path if isinstance(entry, KeptEntry) else entry.entry.path: entry
               for entry in cleanup_entries(roots, now, proc)}
    for path in removals:
        entry = current.get(path)
        if not isinstance(entry, CleanupRemoval):
            reason = entry.reason if isinstance(entry, KeptEntry) else "no current sweep candidate"
            print(f"keep {path}: {reason}")
            result.keep(path, reason)
            continue
        if remove_entry(entry.entry, result, False):
            removed = result.deleted[-1]
            print(f"tmp sweep: deleted {path} ({removed['gib']:.1f} GiB), last changed {removed['last_change']}")
    for failure in result.failed:
        print(f"tmp sweep: could not delete {failure['path']}: {failure['error']}", file=sys.stderr)
    append_log(state_dir() / LOG_NAME, record(result, False, now), now)
    return 1 if result.failed else 0


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
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        with staged.open("w", encoding="utf-8") as output:
            try:
                with path.open(encoding="utf-8") as source:
                    for line in source:
                        if logged_at(line) >= now - LOG_KEEP_SECONDS:
                            _ = output.write(line.rstrip("\n") + "\n")
            except FileNotFoundError:
                pass
            # iterencode avoids a second full copy of a large run record.
            for chunk in json.JSONEncoder(sort_keys=True).iterencode(entry):
                _ = output.write(chunk)
            _ = output.write("\n")
        _ = staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


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
    if len(argv) == 2 and argv[0] in ("--plan", "--apply"):
        operation = write_cleanup_plan if argv[0] == "--plan" else apply_cleanup_plan
        try:
            return operation(Path(argv[1]), default_roots(), time.time(), PROC_DIR)
        except (OSError, UnicodeError, ValueError) as error:
            print(f"tmp sweep: {error}", file=sys.stderr)
            return 1
    if any(arg != DRY_RUN_FLAG for arg in argv):
        print(f"usage: tmp_sweep.py [{DRY_RUN_FLAG}] | --plan <new-plan.json> | --apply <plan.json>", file=sys.stderr)
        return 2
    return run(DRY_RUN_FLAG in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
