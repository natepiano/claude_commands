#!/usr/bin/env python3
"""Find loose folders in ~/rust, ~/rust/scratch, /tmp, /tmp/claude and $TMPDIR once a day, remove the
idle ones whose removal loses no work, and tell their makers.

The scratch guard refuses most loose folders before they exist, and the hourly tmp sweep removes the
temp leftovers that cannot hold work (tempfile folders, folders that hold only folders, verify.sh run folders, invoke.sh logs). This audit is
the backstop for the rest: folders that can hold work. A folder is loose when it is none of these:

  * in ~/rust: a name in config/rust_roots.txt (one per line, `#` starts a comment), or a scratch
    root (~/rust/repro, ~/rust/scratch);
  * in ~/rust/scratch: nothing; every folder there is loose, since scratch.py never makes one there;
  * in /tmp, /tmp/claude and $TMPDIR: a name the system or a tool owns (KEPT_NAMES, KEPT_PREFIXES),
    a session scratchpad (/tmp/claude-<uid>), a run folder root, a scratch root, a folder that holds
    only folders, a verify.sh run folder or a tempfile folder, which the sweep judges.

Only folders this user owns and idle for MIN_AGE_SECONDS are named. The initial scan includes
folder mtimes and each git repo's HEAD, index and HEAD log, so a commit counts. The shared tmp-sweep
change-time scan also checks mtime and ctime throughout the tree, so copying files with preserved
mtimes starts a new idle period. Removal requires WIDE_IDLE_SECONDS (7 days) without a change.

A loose clone uses clone_max_hours from hanas.conf; other folders use REMOVE_AFTER_SECONDS.
On the Mac, free_floor_gib from mac_test.conf can select younger idle clones too, oldest first.
Under that floor, unsaved clones lose only target. These rules share all of the following keeps:

  * no open hold (`scratch.py hold`) names it, a folder in it, or a folder that holds it;
  * no process works in it, holds a file in it open or maps one (one /proc scan; lsof on the Mac);
  * no socket in it is bound by a live process (/proc/net/unix; on the Mac any socket keeps it);
  * no git repo in it holds work on no remote: uncommitted changes, a stash, commits on no remote
    (tmp_sweep.git_work, shared with the temp sweep). A failed git check keeps the folder.

A removed linked worktree is pruned from its source repo. Removal runs only once armed
(`folder_audit.py arm` writes ARMED_NAME in the folder-sweep state folder; `disarm` removes it);
until then each line says "would remove". When config/rust_roots.txt cannot be read, nothing in
~/rust is judged (an old checkout on the Mac has none). Each removal appends one JSON line to
REMOVED_LOG in the state folder.

The maker is the earliest-started session whose transcript (~/.claude*/projects/*/<session>.jsonl)
holds a command that names the folder; a listing in a command's output does not count.
`folder_audit.py [arm|disarm] [--send]` prints one line per loose folder and what happened to it;
with --send, each maker session also gets one message that lists its folders.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import tmp_sweep
from tmp_sweep import SYSTEM_TEMP_NAMES, SYSTEM_TEMP_PREFIXES, TEMPFILE_NAME
from tmp_sweep import ProcessUsesUnavailable as ProcessUsesUnavailable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import scratch  # noqa: E402
from config_values import config_values  # noqa: E402

RUST_ROOTS_FILE = Path(__file__).resolve().parents[2] / "config" / "rust_roots.txt"
RUST_SCRATCH_NAME = "scratch"
MIN_AGE_SECONDS = 24 * 3600
REMOVE_AFTER_SECONDS = tmp_sweep.WIDE_IDLE_SECONDS
KEPT_NAMES = SYSTEM_TEMP_NAMES | {"unit", "delegate", "scratch", "repro", "session", "copies"}
KEPT_PREFIXES = SYSTEM_TEMP_PREFIXES
IDLE_DEPTH = 3
# A folder with more entries than this inside IDLE_DEPTH is not read to the end, and stays.
IDLE_ENTRY_LIMIT = 50_000
SKIPPED_FOLDERS = frozenset({"target", "node_modules", ".git"})
GIT_TIME_FILES = ("HEAD", "index", os.path.join("logs", "HEAD"))
UNIX_SOCKETS = "/proc/net/unix"
STATE_ENV = "FOLDER_SWEEP_STATE_DIR"
DEFAULT_STATE_DIR = os.path.join("~", ".local", "state", "folder-sweep")
ARMED_NAME = "armed"
REMOVED_LOG = "removed.jsonl"
RG_FILES_PER_CALL = 500
# A session counts as a maker only where one of its own commands names the folder. ripgrep returns
# each whole JSON "command" string that names a loose folder (the path, then a boundary or the
# string's end), and every folder in it is read from there. A listing in a tool result names every
# folder it shows, so it does not count.
COMMAND_START = r'"command":"(?:[^"\\]|\\.)*?'
COMMAND_REST = r'(?:(?:[^A-Za-z0-9_.\-"\\]|\\.)(?:[^"\\]|\\.)*)?"'
TRANSCRIPT_TIME = re.compile(rb'"timestamp":"([^"]+)"')
START_LINES = 20
START_BYTES = 1 << 20
SEND = Path(__file__).resolve().parents[1] / "message" / "send.py"

LISTED = "idle less than 7 days"
REMOVED = "removed"
WOULD_REMOVE = "would remove"
HELD = "a hold keeps it"
TOO_LARGE = "too many entries to read"
LIVE_SOCKET = "a live process binds a socket in it"
TARGET_REMOVED = "removed target; kept unsaved source"
WOULD_REMOVE_TARGET = "would remove target; keep unsaved source"


@dataclass(frozen=True)
class ClonePolicy:
    max_idle_seconds: float
    free_floor_bytes: float


def clone_policy() -> ClonePolicy:
    """Read clone age and the Mac floor at use, without a second set of defaults."""
    config = Path(__file__).resolve().parents[2] / "config"

    def number(file: str, override: str, key: str) -> float:
        path = os.environ.get(override) or str(config / file)
        values = config_values(path)
        try:
            value = float(values[key])
        except (KeyError, ValueError) as error:
            raise ValueError(f"{path}: {key} needs a finite non-negative number") from error
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{path}: {key} needs a finite non-negative number")
        return value

    return ClonePolicy(number("hanas.conf", "HANAS_CONFIG", "clone_max_hours") * 3600,
                       number("mac_test.conf", "MAC_TEST_CONFIG", "free_floor_gib") * 1024**3)


def is_clone(path: Path) -> bool:
    """A registered test clone (also when partial), or a loose independent Git clone."""
    record = scratch.task_of(path)
    if isinstance(record, scratch.Registered) and record.fields.get("test_hana_clone") is True:
        return True
    return (path / ".git").is_dir() and not (path / ".git").is_symlink()


def test_clones(roots: list[Path]) -> list[Path]:
    """Only registered test clones under protected scratch roots belong to this audit."""
    found: list[Path] = []
    for root in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            record = scratch.task_of(entry)
            if isinstance(record, scratch.Registered) and record.fields.get("test_hana_clone") is True:
                found.append(entry)
    return found


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def remove_clone_targets(path: Path, removing: bool) -> str:
    """Keep unsaved source. Delete only build output from its independent clones."""
    targets = [repo / "target" for repo in scratch.repos(path)
               if (repo / "target").is_dir() and not (repo / "target").is_symlink()]
    if not targets:
        return tmp_sweep.GitWork.UNSAVED.value
    if not removing:
        return WOULD_REMOVE_TARGET
    try:
        for target in targets:
            tmp_sweep.delete_tree(str(target))
    except OSError as error:
        return f"target removal failed: {error}"
    return TARGET_REMOVED


@dataclass(frozen=True)
class Loose:
    path: Path
    changed: float
    maker: str
    outcome: str = LISTED


def expected_rust_names(path: Path = RUST_ROOTS_FILE) -> frozenset[str] | None:
    """The names expected in ~/rust, or None when the file cannot be read."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return frozenset(name for line in lines if (name := line.split("#", 1)[0].strip()))


def kept_in_temp(entry: Path) -> bool:
    name = entry.name
    if name in KEPT_NAMES or name.startswith(KEPT_PREFIXES) or TEMPFILE_NAME.fullmatch(name):
        return True
    return tmp_sweep.holds_no_file(str(entry)) or tmp_sweep.VERIFY_TEMP_NAME.fullmatch(name) is not None


def candidates(rust: Path, temps: list[Path], expected: frozenset[str] | None) -> list[Path]:
    """Every loose folder this user owns, before the age rule."""
    def kept_in_rust(entry: Path) -> bool:
        return expected is None or entry.name in expected or entry.name in KEPT_NAMES

    roots: list[tuple[Path, Callable[[Path], bool]]] = [
        (rust, kept_in_rust), (rust / RUST_SCRATCH_NAME, lambda _entry: False),
        *((temp, kept_in_temp) for temp in temps)]
    found: list[Path] = []
    for root, keep in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir() or entry.stat().st_uid != os.getuid():
                    continue
            except OSError:
                continue
            if not keep(entry):
                found.append(entry)
    return found


def _walk(path: Path, depth: int) -> Iterator[os.DirEntry[str]]:
    """Every entry under path down to depth folders, SKIPPED_FOLDERS passed over; links not followed."""
    frontier = [(str(path), depth)]
    while frontier:
        folder, left = frontier.pop()
        for entry in tmp_sweep.listing(folder):
            yield entry
            try:
                if left and entry.name not in SKIPPED_FOLDERS and entry.is_dir(follow_symlinks=False):
                    frontier.append((entry.path, left - 1))
            except OSError:
                continue


def last_change(path: Path) -> float | str:
    """The newest mtime in path (see the module text), or TOO_LARGE."""
    newest = _mtime(path)
    for count, entry in enumerate(_walk(path, IDLE_DEPTH)):
        if count >= IDLE_ENTRY_LIMIT:
            return TOO_LARGE
        try:
            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
        except OSError:
            continue
    for repo in scratch.repos(path):
        git = scratch.git_directory(repo)
        if isinstance(git, scratch.GitDirectory):
            newest = max([newest, *(_mtime(git.path / name) for name in GIT_TIME_FILES)])
    return newest


def bound_sockets(table: str = UNIX_SOCKETS) -> frozenset[str] | None:
    """The path of every unix socket a live process binds, or None where the table cannot be read."""
    try:
        lines = Path(table).read_text(encoding="utf-8", errors="replace").splitlines()[1:]
    except OSError:
        return None
    return frozenset(parts[7] for line in lines if len(parts := line.split()) >= 8 and parts[7].startswith("/"))


def live_socket(path: Path, bound: frozenset[str] | None) -> bool:
    """Whether a socket in path is live; every socket counts as live when `bound` is None."""
    for entry in _walk(path, IDLE_DEPTH):
        try:
            if stat.S_ISSOCK(entry.stat(follow_symlinks=False).st_mode):
                if bound is None or entry.path in bound or os.path.realpath(entry.path) in bound:
                    return True
        except OSError:
            continue
    return False


def held(path: Path, holds: dict[str, scratch.Hold]) -> bool:
    inside = str(path) + os.sep
    return any(not hold["ended"] and (held_path == str(path) or held_path.startswith(inside)
                                      or str(path).startswith(held_path + os.sep))
               for held_path, hold in holds.items())


def process_uses(paths: list[Path], proc: str = tmp_sweep.PROC_DIR) -> tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable:
    """Process paths from /proc, or lsof where /proc is missing; unavailable when neither can run."""
    if not paths:
        return tmp_sweep.ProcessUsesKnown([])
    prefixes = tuple(dict.fromkeys(str(path.parent) + os.sep for path in paths))
    found = tmp_sweep.process_paths(proc, prefixes)
    if isinstance(found, tmp_sweep.ProcessUsesUnavailable):
        return tmp_sweep.lsof_paths(prefixes)
    return found


def worktree_sources(path: Path) -> list[Path]:
    """The source repo of each linked worktree in path, to prune once path is gone."""
    sources: list[Path] = []
    for repo in scratch.repos(path):
        if (repo / ".git").is_file():
            result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
                                     "--git-common-dir"], capture_output=True, text=True, check=False)
            if not result.returncode and result.stdout.strip():
                sources.append(Path(result.stdout.strip()))
    return sources


def state_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get(STATE_ENV, DEFAULT_STATE_DIR)))


def armed() -> bool:
    return (state_dir() / ARMED_NAME).is_file()


@dataclass(frozen=True)
class PendingWorktreePrune:
    removed_path: Path
    sources: list[Path]


def read_pending_prune(record: Path) -> PendingWorktreePrune:
    value = cast(object, json.loads(record.read_text()))
    if isinstance(value, dict):
        row = cast(dict[str, object], value)
        path, sources = row.get("path"), row.get("sources")
        if isinstance(path, str) and Path(path).is_absolute() and isinstance(sources, list):
            entries = cast(list[object], sources)
            if all(isinstance(source, str) and Path(source).is_absolute() for source in entries):
                return PendingWorktreePrune(Path(path), [Path(str(source)) for source in entries])
    raise ValueError(f"invalid pending prune record: {record}")


def finish_pending_prune(record: Path, pending: PendingWorktreePrune) -> None:
    for source in pending.sources:
        try:
            _ = source.stat()
        except FileNotFoundError:
            continue
        _ = subprocess.run(["git", "--git-dir", str(source), "worktree", "prune"],
                           capture_output=True, check=True)
    record.unlink()


def resume_pending_prunes() -> None:
    directory = state_dir()
    if not directory.exists():
        return
    with (directory / "prune.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for record in directory.glob("*.prune"):
            try:
                pending = read_pending_prune(record)
                # A partial delete must pass the normal removal checks again.
                if not os.path.lexists(pending.removed_path):
                    finish_pending_prune(record, pending)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"folder audit: pending prune {record}: {error}", file=sys.stderr)


def remove(path: Path, changed: float, now: float) -> str:
    """Remove path and prune its worktrees; REMOVED, or why it stays."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / (hashlib.sha256(os.fsencode(path)).hexdigest() + ".prune")
    with (directory / "prune.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            sources = worktree_sources(path)
            if record.exists():
                previous = read_pending_prune(record)
                if previous.removed_path != path:
                    raise ValueError(f"pending prune path differs: {record}")
                sources = list(dict.fromkeys(previous.sources + sources))
            if sources:
                temporary = record.with_suffix(".tmp")
                try:
                    _ = temporary.write_text(json.dumps({"path": str(path), "sources": [str(source) for source in sources]}))
                    os.replace(temporary, record)
                finally:
                    temporary.unlink(missing_ok=True)
            scratch.remove_marked(path, lambda folder: tmp_sweep.delete_tree(str(folder)))
            if sources:
                finish_pending_prune(record, PendingWorktreePrune(path, sources))
        except (OSError, ValueError, scratch.ScratchError, subprocess.SubprocessError) as error:
            return f"removal failed: {error}"

    log = state_dir() / REMOVED_LOG
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            _ = handle.write(json.dumps({"at": now, "path": str(path), "changed": changed}) + "\n")
    except OSError:
        pass
    return REMOVED


def judge(found: list[tuple[Path, float]], now: float, removing: bool, proc: str = tmp_sweep.PROC_DIR,
          sockets: str = UNIX_SOCKETS, *, mac: bool | None = None,
          free_space: Callable[[Path], int] | None = None) -> dict[Path, str]:
    """Apply the same keep checks to aged folders and clones selected by age or Mac floor."""
    policy = clone_policy()
    on_mac = sys.platform == "darwin" if mac is None else mac
    space = free_bytes if free_space is None else free_space
    clones = {path for path, _ in found if is_clone(path)}
    pending = {path for path, _ in found if isinstance(scratch.pending_removal(path), scratch.Registered)}
    due = [path for path, changed in found
           if now - changed >= (policy.max_idle_seconds if path in clones else REMOVE_AFTER_SECONDS)
           or on_mac and path in clones or path in pending]
    outcomes = {path: LISTED for path, _ in found}
    if not due:
        return outcomes
    holds = scratch.holds()
    uses = process_uses(due, proc)
    used_entries = (tmp_sweep.entry_uses(uses.paths, sorted({str(path.parent) for path in due}))
                    if isinstance(uses, tmp_sweep.ProcessUsesKnown) else {})
    bound = bound_sockets(sockets)
    changed = dict(found)
    for path in sorted(due, key=lambda candidate: changed[candidate]):
        try:
            below_floor = on_mac and path in clones and space(path) < policy.free_floor_bytes
        except OSError as error:
            outcomes[path] = f"free space cannot be read: {error}"
            continue
        if path in clones and now - changed[path] < policy.max_idle_seconds and not below_floor and path not in pending:
            continue
        if held(path, holds):
            outcomes[path] = HELD
            continue
        if isinstance(uses, tmp_sweep.ProcessUsesUnavailable):
            outcomes[path] = tmp_sweep.NO_PROC
            continue
        if str(path) in used_entries:
            outcomes[path] = used_entries[str(path)]
            continue
        if live_socket(path, bound):
            outcomes[path] = LIVE_SOCKET
            continue
        with scratch.removal_lock(path) if removing else scratch.judgment_lock(path) as idle:
            if not idle:
                outcomes[path] = "busy: .claude-busy is locked"
                continue
            # This certificate was published only after the original Git check.
            # A partial deletion can no longer pass that check, but all live-use guards still apply.
            resuming = isinstance(scratch.pending_removal(path), scratch.Registered)
            git = tmp_sweep.GitWork.SAVED if resuming else tmp_sweep.git_work(path)
            if git is not tmp_sweep.GitWork.SAVED:
                outcomes[path] = (remove_clone_targets(path, removing)
                                  if below_floor and git is tmp_sweep.GitWork.UNSAVED else git.value)
            else:
                outcomes[path] = remove(path, changed[path], now) if removing else WOULD_REMOVE

    return outcomes


def transcripts() -> list[Path]:
    """Every session transcript once; the account homes link the same projects folder."""
    folders = dict.fromkeys(folder.resolve() for folder in sorted(Path.home().glob(".claude*/projects")))
    return [path for folder in folders for path in folder.glob("*/*.jsonl")]


def makers(paths: list[Path], files: list[Path]) -> dict[Path, str]:
    """The earliest-started session whose own commands name each path, since a folder's maker named it
    before any later session that only talked about it; a path no command names is left out.
    One ripgrep pass reads every transcript: a Python scan of 22 GB took 4 h 24 min (2026-10-26). A
    name must end at a path boundary, so ~/rust/hana is not named by ~/rust/hana_catalyst."""
    if not paths or not files:
        return {}
    names = {str(path) for path in paths}
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    pattern = (COMMAND_START + "(?:" + alternatives + ")" + COMMAND_REST + "\n").encode()
    in_command = re.compile(f"(?:{alternatives})(?![A-Za-z0-9_.-])")
    earliest: dict[str, tuple[float, str]] = {}
    started: dict[str, float] = {}
    for start in range(0, len(files), RG_FILES_PER_CALL):
        chunk = [str(file) for file in files[start:start + RG_FILES_PER_CALL]]
        result = subprocess.run(["rg", "--only-matching", "--with-filename", "--null", "--no-line-number",
                                 "--no-messages", "--no-config", "--file", "-", "--", *chunk],
                                input=pattern, capture_output=True, check=False)
        for line in result.stdout.decode(errors="replace").splitlines():
            file, _, command = line.partition("\0")
            for name in {found.group(0) for found in in_command.finditer(command)}:
                if file not in started:
                    started[file] = _started(Path(file))
                if started[file] < earliest.get(name, (float("inf"), ""))[0]:
                    earliest[name] = (started[file], Path(file).stem)
    return {path: earliest[str(path)][1] for path in paths if str(path) in earliest}


def _started(path: Path) -> float:
    """The first timestamp in a transcript's opening lines, else its file time."""
    try:
        with path.open("rb") as handle:
            for _ in range(START_LINES):
                found = TRANSCRIPT_TIME.search(handle.readline(START_BYTES))
                if found:
                    return datetime.fromisoformat(found.group(1).decode().replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError):
        pass
    return _mtime(path)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def audit(rust: Path, temps: list[Path], expected: frozenset[str] | None, now: float,
          files: list[Path] | None = None, removing: bool = False, proc: str = tmp_sweep.PROC_DIR,
          sockets: str = UNIX_SOCKETS, *, clone_roots: list[Path] | None = None,
          mac: bool | None = None, free_space: Callable[[Path], int] | None = None) -> list[Loose]:
    if removing:
        resume_pending_prunes()
    changes: dict[Path, float | str] = {}
    discovered = candidates(rust, temps, expected) + test_clones([scratch.root()] if clone_roots is None else clone_roots)
    # macOS reports /private/tmp in lsof even when discovery started through /tmp.
    # Resolve once at discovery, before age, hold, process, socket and lock checks.
    for entry in dict.fromkeys(entry.resolve() for entry in discovered):
        when = last_change(entry)
        if isinstance(when, float):
            minimum_age = 0 if is_clone(entry) else MIN_AGE_SECONDS
            change = tmp_sweep.last_change(str(entry), tmp_sweep.Limit(now - minimum_age, LISTED))
            when = change if isinstance(change, str) else max(when, change[0])
        changes[entry] = when
    found = [(entry, when) for entry, when in changes.items()
             if isinstance(when, float) and when and (is_clone(entry) or now - when >= MIN_AGE_SECONDS)]
    outcomes = judge(found, now, removing, proc, sockets, mac=mac, free_space=free_space)
    too_large = [entry for entry, when in changes.items() if when == TOO_LARGE]
    named_paths = [entry for entry, _ in found] + too_large
    sessions = transcripts() if files is None and named_paths else (files or [])
    named = makers(named_paths, sessions)
    return ([Loose(entry, when, named.get(entry, ""), outcomes[entry]) for entry, when in found]
            + [Loose(entry, 0.0, named.get(entry, ""), TOO_LARGE) for entry in too_large])


def message(folders: list[Loose]) -> str:
    shown = "; ".join(f"{folder.path} ({folder.outcome})" for folder in folders)
    return (f"folder audit: loose folder(s) you made: {shown}. The audit uses hanas.conf clone_max_hours "
            + "for clones and 7 idle days for other folders. The Mac floor can remove idle clones sooner, "
            + "or just their targets when source is unsaved. Holds and live processes keep them. Move each you need into a "
            + "task's scratch (`~/.claude/scripts/lib/py ~/.claude/scripts/lib/scratch.py adopt <path> --task "
            + "<task>`) or hold it (`scratch.py hold <path> --reason <why>`). Make new scratch only with "
            + "`scratch.py new`.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="folder_audit.py", description="Find loose folders, remove the idle ones "
                                     + "that lose no work, and tell their makers.")
    _ = parser.add_argument("action", nargs="?", choices=("arm", "disarm"))
    _ = parser.add_argument("--send", action="store_true")
    arguments = parser.parse_args(argv)
    action = cast(str | None, arguments.action)
    marker = state_dir() / ARMED_NAME
    if action == "arm":
        marker.parent.mkdir(parents=True, exist_ok=True)
        _ = marker.write_text(f"{time.time()}\n", encoding="utf-8")
        print(f"folder audit: armed ({marker}); the next run removes folders")
        return 0
    if action == "disarm":
        marker.unlink(missing_ok=True)
        print("folder audit: disarmed; runs only list what they would remove")
        return 0
    send = cast(bool, arguments.send)
    expected = expected_rust_names()
    if expected is None:
        print(f"folder audit: {RUST_ROOTS_FILE} cannot be read; ~/rust is not judged")
    temps = list(dict.fromkeys(Path(os.path.realpath(path)) for path in
                               ("/tmp", "/tmp/claude", os.environ.get("TMPDIR", "")) if path))
    now = time.time()
    removing = armed()
    try:
        loose = audit(Path(os.path.realpath(Path.home() / "rust")), temps, expected, now, removing=removing)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"folder audit: {error}", file=sys.stderr)
        return 2
    by_maker: dict[str, list[Loose]] = {}
    for folder in loose:
        age = (now - folder.changed) / 86400 if folder.changed else float("nan")
        print(f"loose: {folder.path} ({age:.1f} d idle) maker {folder.maker or 'unknown'}: {folder.outcome}")
        by_maker.setdefault(folder.maker, []).append(folder)
    gone = sum(folder.outcome in (REMOVED, WOULD_REMOVE) for folder in loose)
    print(f"folder audit: {len(loose)} loose folder(s), {gone} {'removed' if removing else 'would be removed (dry run)'}")
    if send:
        for maker, folders in by_maker.items():
            if maker:
                _ = subprocess.run([sys.executable, str(SEND), "--to", f"session:{maker}", "--from", "folder-audit",
                                    "--text", message(folders)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
