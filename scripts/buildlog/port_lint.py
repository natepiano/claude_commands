#!/usr/bin/env python3
"""port-lint: cargo-port's lint command. Defer, reuse a recorded step, or run.

  port-lint clippy|mend|sweep [ARG...]

cargo-port runs it after a save, with its cwd in the project. In order:

Defer, exit 75, when someone is at work in the worktree: a `cargo-handler
probe` row that is busy, at a shell, or has a Codex seat at work (a thread
child), whose folder is the worktree or inside it; or a process named cargo*
other than cargo-handler and cargo-port, whose cwd is inside it. A lint then
would compete with that work for the build lock and the CPU, and lint files
that are still changing. A probe that is missing, fails or takes over 10 s is
skipped.

Reuse, clippy and mend only, and only without extra args: when a step on this
host already ran the argv `lint` would run, in the same worktree, on the same
files (tree_key) with the same rustc, its result stands in. A pass exits 0; a
failure prints its saved log and exits with its status. Any caller's step
counts: an agent's, a terminal's, cargo-port's own. A step with a signal
status, a usage error (2) or a sandbox failure (3) never does, and neither
does a mend --fix that applied fixes.

Run: otherwise exec `lint <command> [ARG...]`, so cargo-port sees lint's
output and status directly; the build log records its steps with caller
cargo-port. Sweep never reuses: it acts on the target folder, which the tree
key does not cover.

Reused, replayed and deferred calls each leave a call record (tool
port-lint); a call that ran leaves its steps instead. A recorder error never
changes the exit status.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

import index
import record
import store
import treekey

DEFERRED = 75
USAGE = 2
COMMANDS = ("clippy", "mend", "sweep")
LINT = Path.home() / ".claude/scripts/lint/lint"
PROBE_TIMEOUT_S = 10
LSOF_TIMEOUT_S = 10
BUSY = frozenset({"busy", "shell"})
NOT_A_BUILD = frozenset({"cargo-handler", "cargo-port"})
SCOPE = ["--all-targets", "--workspace"]
CLIPPY = ["cargo", "clippy"]
CLIPPY_LINTS = ["-D", "warnings"]
MEND = ["env", "RUSTC_WRAPPER=", "cargo", "mend"]


class ProbeChild(TypedDict):
    kind: str | dict[str, str]


class ProbeRow(TypedDict):
    name: str
    status: str | None
    directory: str
    children: list[ProbeChild]


class Probe(TypedDict):
    rows: list[ProbeRow]


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    cwd: str


@dataclass(frozen=True)
class Reusable:
    id: str
    caller: str | None
    seat: str | None
    started_at: str
    status: int
    log: str | None
    duration_s: float


def inside(path: str, worktree: str) -> bool:
    return path == worktree or path.startswith(worktree.rstrip(os.sep) + os.sep)


def real(directory: str) -> str:
    return os.path.realpath(os.path.expanduser(directory))


def busy_row(rows: list[ProbeRow], worktree: str) -> tuple[str, str] | None:
    """Who is at work in the worktree, and where: a row in a folder that merely holds it does not count."""
    for row in rows:
        directory = real(row["directory"])
        if not inside(directory, worktree):
            continue
        if row["status"] in BUSY:
            return f"agent {row['name']}", directory
        if any(child["kind"] == "thread" for child in row["children"]):
            return f"a Codex seat of {row['name']}", directory
    return None


def probe() -> list[ProbeRow] | None:
    handler = shutil.which("cargo-handler") or str(Path.home() / ".cargo/bin/cargo-handler")
    try:
        result = subprocess.run(
            [handler, "probe"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return cast(Probe, json.loads(result.stdout))["rows"]
    except (ValueError, KeyError, TypeError):
        return None


def proc_cargos() -> list[Process]:
    found: list[Process] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            name = Path(f"/proc/{entry}/comm").read_text().strip()
            if name.startswith("cargo"):
                found.append(Process(int(entry), name, os.readlink(f"/proc/{entry}/cwd")))
        except OSError:
            continue
    return found


def lsof_cargos() -> list[Process]:
    """macOS has no /proc: lsof lists each cargo* process's cwd. +c 0 keeps the whole name."""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        result = subprocess.run(
            [lsof, "+c", "0", "-nP", "-a", "-c", "cargo", "-d", "cwd", "-Fpcn"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=LSOF_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[Process] = []
    pid: int | None = None
    name = ""
    for line in result.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, name = int(value), ""
        elif tag == "c":
            name = value
        elif tag == "n" and pid is not None:
            found.append(Process(pid, name, value))
    return found


def busy_cargo(processes: list[Process], worktree: str) -> tuple[str, str] | None:
    for process in processes:
        if not process.name.startswith("cargo") or process.name in NOT_A_BUILD:
            continue
        directory = real(process.cwd)
        if inside(directory, worktree):
            return f"cargo pid {process.pid} ({process.name})", directory
    return None


def deferral(worktree: str) -> str | None:
    """The line to print when someone is at work in the worktree, or None."""
    rows = probe()
    busy = busy_row(rows, worktree) if rows is not None else None
    if busy is None:
        busy = busy_cargo(proc_cargos() if os.path.isdir("/proc/self") else lsof_cargos(), worktree)
    if busy is None:
        return None
    who, directory = busy
    return f"port-lint: deferred — {who} is busy in {directory}"


def lint_argv(command: str, argv: list[str]) -> bool:
    """Whether a step's argv is the one `lint <command>` runs in a workspace: scope flags in any order."""
    if command == "clippy":
        if argv[: len(CLIPPY)] != CLIPPY or "--" not in argv:
            return False
        split = argv.index("--")
        return sorted(argv[len(CLIPPY) : split]) == SCOPE and argv[split + 1 :] == CLIPPY_LINTS
    if command == "mend":
        if argv[: len(MEND)] != MEND:
            return False
        return sorted(argv[len(MEND) :]) in (SCOPE, sorted([*SCOPE, "--fix"]))
    return False


def stands_in(command: str, argv: list[str], mend_fixes: int | None) -> bool:
    """A mend --fix counts only when it reported no fixes; mend without --fix never applies any."""
    if not lint_argv(command, argv):
        return False
    if command == "mend":
        return mend_fixes == 0 or (mend_fixes is None and "--fix" not in argv)
    return True


def find_reusable(command: str, host: str, worktree: str, key: str, rustc: str) -> Reusable | None:
    """The newest step on host that ran `lint <command>` in worktree on these files with this rustc."""
    _ = index.update()
    with closing(index.read_only()) as connection:
        rows = cast(
            list[tuple[str, str | None, str | None, str, int, str | None, float, str, int | None]],
            connection.execute(
                "SELECT id, caller, seat, started_at, status, log, duration_s, argv, mend_fixes FROM steps"
                + " WHERE host = ? AND worktree = ? AND step = ? AND tree_key = ? AND rustc = ?"
                # A signal, a usage error (2) or a sandbox failure (3) says nothing about the files.
                + " AND status < 128 AND status NOT IN (2, 3) ORDER BY started_at DESC",
                (host, worktree, command, key, rustc),
            ).fetchall(),
        )
    for step_id, caller, seat, started_at, status, log, duration_s, argv, mend_fixes in rows:
        if stands_in(command, cast(list[str], json.loads(argv)), mend_fixes):
            return Reusable(step_id, caller, seat, started_at, status, log, duration_s)
    return None


def reusable(command: str, cwd: str, worktree: str) -> Reusable | None:
    key = treekey.tree_key(cwd)
    rustc = record.rustc_version([], cwd)
    if key is None or rustc is None:
        return None
    return find_reusable(command, store.host_name(), worktree, key, rustc)


def replay(command: str, found: Reusable) -> None:
    who = f"{found.caller or 'unknown'} {found.seat}" if found.seat else found.caller or "unknown"
    at = datetime.fromisoformat(found.started_at.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    word = "passed" if found.status == 0 else "failed"
    print(f"port-lint: {command} reused from {who} at {at}, tree unchanged ({word})", flush=True)
    if found.status == 0:
        return
    if found.log is None:
        print("port-lint: no log was kept for that run", flush=True)
        return
    path = store.root() / found.log
    try:
        data = gzip.decompress(path.read_bytes())
    except (OSError, EOFError, gzip.BadGzipFile):
        print(f"port-lint: its log {path} cannot be read", flush=True)
        return
    _ = sys.stdout.buffer.write(data)
    _ = sys.stdout.buffer.flush()


def note_call(started: float, words: list[str], outcome: str, status: int, found: Reusable | None, reason: str | None) -> None:
    fields: dict[str, object] = {
        "tool": "port-lint",
        "caller": "cargo-port",
        "command": " ".join(["port-lint", *words]),
        "verb": words[0],
        "package": None,
        "outcome": outcome,
        "status": status,
        "cached": False,
        "wait_s": 0,
        "wall_s": round(time.time() - started),
        "build_s": None,
        "saved_s": round(found.duration_s) if found else 0,
        "reuses": found.id if found else None,
        "reason": reason,
    }
    try:
        record.write_call(started, fields)
    except Exception:  # noqa: BLE001 -- the record never changes what cargo-port sees
        store.note_error(f"port-lint call record {outcome}")


def main(argv: list[str]) -> int:
    started = time.time()
    if not argv or argv[0] not in COMMANDS:
        print("usage: port-lint clippy|mend|sweep [ARG...]", file=sys.stderr)
        return USAGE
    command, extra = argv[0], argv[1:]
    cwd = os.getcwd()
    worktree = treekey.worktree(cwd) or cwd
    try:
        reason = deferral(real(worktree))
    except Exception:  # noqa: BLE001 -- a broken check lints rather than defers forever
        store.note_error("port-lint deferral")
        reason = None
    if reason is not None:
        print(reason, flush=True)
        note_call(started, argv, "deferred", DEFERRED, None, reason)
        return DEFERRED
    if command != "sweep" and not extra:
        try:
            found = reusable(command, cwd, worktree)
        except Exception:  # noqa: BLE001 -- a broken lookup runs the lint
            store.note_error(f"port-lint reuse {command}")
            found = None
        if found is not None:
            replay(command, found)
            note_call(started, argv, "reused" if found.status == 0 else "replayed", found.status, found, None)
            return found.status
    os.environ["BUILDLOG_CALLER"] = "cargo-port"
    try:
        os.execv(LINT, [str(LINT), command, *extra])
    except OSError as error:
        print(f"port-lint: cannot run {LINT}: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
