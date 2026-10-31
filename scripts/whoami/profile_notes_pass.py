#!/usr/bin/env python3
"""Profile the notes timer without changing notes, accounts or session state.

Only the director runs this against a live HOME. Run from the timer's environment:
  python3 scripts/whoami/profile_notes_pass.py --stats /tmp/claude/notes.prof \
      --scratch /tmp/claude/notes-profile

Action counters include zeroes. Read-only children run normally; only their first
two command words appear in the report. Pass messages and child output stay private.
"""
from __future__ import annotations

import argparse
import builtins
import cProfile
import io
import json
import os
import pstats
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Literal, cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class DiscardedTextWrite(io.StringIO):
    @override
    def fileno(self) -> int:
        # Lock calls are also suppressed. No real descriptor belongs to this write.
        return -1


class DiscardedBinaryWrite(io.BytesIO):
    @override
    def fileno(self) -> int:
        return -1


@dataclass(frozen=True)
class PassCallPatch:
    """A pass boundary that suppresses an action or times a read-only child."""
    owner: object
    attribute: str
    effect: str
    result: object = None
    mode: Literal["noop", "open", "os_open", "codex", "child_launch", "child_wait", "child_communicate"] = "noop"

    @property
    def name(self) -> str:
        owner = self.owner
        prefix = owner.__name__ if isinstance(owner, (ModuleType, type)) else type(owner).__name__
        return f"{prefix}.{self.attribute}"


@dataclass(frozen=True)
class StateCopy:
    """A state reader redirected to a private copy before the pass starts."""
    owner: object
    attribute: str
    source: Path
    path_function: bool = False
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChildRunning:
    """The child has not completed yet."""


@dataclass(frozen=True)
class ChildCompleted:
    finished: float


@dataclass
class ChildTiming:
    command: tuple[str, ...]
    started: float
    completion: ChildRunning | ChildCompleted = field(default_factory=ChildRunning)

    def wall_time(self, observed: float) -> float:
        end = self.completion.finished if isinstance(self.completion, ChildCompleted) else observed
        return end - self.started


@dataclass
class Measurements:
    calls: Counter[str] = field(default_factory=Counter)
    children: list[ChildTiming] = field(default_factory=list)
    child_by_process: dict[int, ChildTiming] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    incomplete: tuple[str, ...] = ()
    peer_python: dict[str, str] = field(default_factory=dict)

    def count(self, name: str) -> None:
        with self.lock:
            self.calls[name] += 1


@dataclass(frozen=True)
class NotesPass:
    notes: ModuleType
    accounts: ModuleType
    watch: ModuleType
    watchdog: ModuleType
    quota: ModuleType
    pacer: ModuleType
    usage: ModuleType
    five: ModuleType
    roles: ModuleType
    moves: ModuleType
    next_moves: ModuleType
    refusals: ModuleType
    showrunners: ModuleType
    run_out: ModuleType
    outage: ModuleType
    record: ModuleType
    permission: ModuleType


def load_pass() -> NotesPass:
    # Keep the timer modules on their existing sibling import names.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    notes = import_module("agent_notes")
    watch = import_module("scripts.accounts.refusal_watch")
    watchdog = import_module("scripts.accounts.unit_watchdog")
    return NotesPass(notes, import_module("scripts.accounts.accounts"), watch, watchdog,
        import_module("quota_alert"), import_module("codex_pacer"), import_module("scripts.whoami.usage_store"),
        import_module("five_hour"), import_module("scripts.accounts.role_map"),
        import_module("scripts.accounts.moves"), import_module("scripts.accounts.next_moves"),
        import_module("scripts.accounts.refusals"), import_module("scripts.production.showrunners"),
        import_module("scripts.whoami.run_out"), import_module("scripts.accounts.outage_alerts"),
        import_module("shutdown_record"), import_module("launch_permission"))


def result(owner: ModuleType, name: str, *args: object) -> object:
    """Call a result constructor at the reflection boundary."""
    return cast(Callable[..., object], getattr(owner, name))(*args)


def state_path(owner: ModuleType, name: str, function: bool = False) -> Path:
    value = cast(object, getattr(owner, name))
    return cast(Callable[[], Path], value)() if function else cast(Path, value)


def patch_list(pass_: NotesPass) -> tuple[PassCallPatch, ...]:
    """The complete action inventory. Keep reads and calculations below these boundaries."""
    n, a, w, d, q, p, u = (pass_.notes, pass_.accounts, pass_.watch, pass_.watchdog,
                            pass_.quota, pass_.pacer, pass_.usage)
    return (
        PassCallPatch(a, "sweep_claude_temp_files", "deletes old Claude temporary files"),
        PassCallPatch(n, "rename_legacy_notes", "links and unlinks vault notes", []),
        PassCallPatch(n, "write_note", "replaces vault note text", False),
        PassCallPatch(n, "append_readings", "replaces the usage readings log"),
        PassCallPatch(n, "write_sessions_seen", "replaces session observation state"),
        PassCallPatch(u, "save", "writes usage and account-label readings"),
        PassCallPatch(u, "save_timer_usage", "writes timer reports"),
        PassCallPatch(u, "write", "writes usage read claims and outcomes"),
        PassCallPatch(u, "finish_read", "records a completed live usage attempt"),
        PassCallPatch(u, "read_lock", "creates and locks the usage lock", mode="noop", result=nullcontext()),
        PassCallPatch(p, "write_atomic", "writes pacer measurements, plan and launch tier"),
        PassCallPatch(q, "edit_registry", "edits live agent assignments", "profile: registry edit suppressed"),
        PassCallPatch(q, "relay", "sends or queues a session message", "profile: message suppressed"),
        PassCallPatch(pass_.five, "relay", "sends or queues a five-hour message", "profile: message suppressed"),
        PassCallPatch(q, "tell_user", "sends a phone push", result(q, "PushFailed", "profile: push suppressed")),
        PassCallPatch(pass_.five, "cancel_outgoing_notices", "retires queued warnings and removes markers"),
        PassCallPatch(import_module("escalate"), "close", "removes an escalation and retires its push", False),
        PassCallPatch(import_module("escalate"), "reconcile_codex_holds", "cancels outgoing account escalation holds"),
        PassCallPatch(pass_.roles, "_atomic_write", "updates the remote role-map read cache"),
        PassCallPatch(pass_.roles, "write_role_map", "changes account roles locally or remotely"),
        PassCallPatch(pass_.moves, "append_move", "appends the move journal"),
        PassCallPatch(pass_.moves, "append_routing", "appends the work-routing journal"),
        PassCallPatch(pass_.next_moves, "write_next_moves", "consumes or replaces next moves"),
        PassCallPatch(pass_.next_moves, "next_moves_lock", "creates and locks the next-move lock", nullcontext()),
        PassCallPatch(pass_.refusals, "write_refusal", "writes account refusal evidence"),
        PassCallPatch(w, "_watch_lock", "creates and locks the watcher lock", nullcontext()),
        PassCallPatch(w, "_atomic_json", "writes watcher, renewal or pending notice state"),
        PassCallPatch(w, "clear_assumed", "deletes an assumed refusal"),
        PassCallPatch(w, "_continue_session", "sends a continuation to a live session",
                       result(w, "ContinuationNotDelivered", "profile: continuation suppressed")),
        PassCallPatch(a, "renew_login", "launches a Claude probe and refreshes credentials",
                       result(a, "LoginRenewalFailed", "profile: renewal suppressed")),
        PassCallPatch(a, "prepared_account", "repairs account links before a move",
                       result(a, "AccountRefused", "profile: account preparation suppressed")),
        PassCallPatch(a, "check_move_target_hold", "can clear a finished shutdown hold",
                       result(a, "MoveRefused", "profile: hold change suppressed")),
        PassCallPatch(a, "set_group_account", "changes a group selection",
                       result(a, "GroupTransition", "profile", result(a, "HandOrigin"))),
        PassCallPatch(a, "move_sessions", "starts session shutdowns and restarts", ()),
        PassCallPatch(a, "swap", "prepares a Codex home and changes workers selection", []),
        PassCallPatch(d, "_write_state", "writes watchdog outage ownership"),
        PassCallPatch(d, "_start_recovery", "starts a detached unit restart"),
        PassCallPatch(d, "_alert_failure", "claims an outage and sends its failure push"),
        PassCallPatch(d, "_alert_without_record", "claims a dropped-record outage and pushes it"),
        PassCallPatch(pass_.showrunners, "tell", "sends a watchdog outcome to a showrunner", "profile: message suppressed"),
        PassCallPatch(pass_.outage, "clear", "clears the outage alert ledger", False),
        PassCallPatch(pass_.record, "clear_restore_alert", "clears a restart alert marker"),
        PassCallPatch(pass_.record, "mark_snapshot_agent_exited", "updates saved crash evidence", False),
        PassCallPatch(pass_.record, "restore_snapshot_agent_exited", "restores saved crash evidence", False),
        PassCallPatch(pass_.permission, "launch_permission", "runs launch-blocked which can release shutdown holds",
                       result(pass_.permission, "BlockedByShutdown", "profile: launch action suppressed")),
        PassCallPatch(import_module("scripts.whoami.agent_accounts"), "codex_live",
                       "starts a Codex server that can write its session index; uses copied timer reports", mode="codex"),
        PassCallPatch(q, "codex_live",
                       "imported Codex usage action; uses copied timer reports", mode="codex"),
        # These cover inline marker/state writes, and fail closed for new write paths.
        PassCallPatch(Path, "write_text", "inline state and temporary-file writes", 0),
        PassCallPatch(Path, "write_bytes", "inline binary state writes", 0),
        PassCallPatch(Path, "unlink", "inline marker and watchdog-result removal"),
        PassCallPatch(Path, "mkdir", "inline state-directory creation"),
        PassCallPatch(Path, "replace", "inline state replacement"),
        PassCallPatch(os, "replace", "inline state replacement"),
        PassCallPatch(io, "open", "write opens only; reads still run", mode="open"),
        PassCallPatch(builtins, "open", "write opens only; reads still run", mode="open"),
        PassCallPatch(os, "open", "new low-level write attempts are refused", mode="os_open"),
        PassCallPatch(import_module("fcntl"), "flock", "state locks; copies need no live synchronization"),
        PassCallPatch(import_module("scripts.accounts.worker_names"), "roles_lock", "creates a role history lock", nullcontext()),
        PassCallPatch(subprocess.Popen, "__init__", "read-only children are measured; acting children are refused", mode="child_launch"),
        PassCallPatch(subprocess.Popen, "wait", "measures child completion; read cost remains in the profile", mode="child_wait"),
        PassCallPatch(subprocess.Popen, "communicate", "measures child completion; read cost remains in the profile", mode="child_communicate"),
    )


def state_copies(pass_: NotesPass) -> tuple[StateCopy, ...]:
    """Only side-effect state is copied. Notes, transcripts, registry and rosters stay live."""
    account_patterns = ("refusal_watch.json", "pending_cleared_holds.json", "login_renewals.json",
                        "assumed_refusals/*.json", "unit-watchdog-results/*.json")
    return (
        StateCopy(pass_.usage, "store_file", state_path(pass_.usage, "store_file", True), True),
        StateCopy(pass_.notes, "SESSIONS_SEEN", state_path(pass_.notes, "SESSIONS_SEEN").expanduser()),
        StateCopy(pass_.notes, "READINGS_LOG", state_path(pass_.notes, "READINGS_LOG")),
        StateCopy(pass_.run_out, "READINGS_LOG", state_path(pass_.run_out, "READINGS_LOG")),
        StateCopy(pass_.accounts, "readings_file", state_path(pass_.accounts, "readings_file", True), True),
        StateCopy(pass_.quota, "STATE", state_path(pass_.quota, "STATE")),
        StateCopy(pass_.five, "STATE", state_path(pass_.five, "STATE"), patterns=("*.notice",)),
        StateCopy(pass_.quota, "REGISTRY", state_path(pass_.quota, "REGISTRY")),
        StateCopy(pass_.pacer, "AGENTS_CONF", state_path(pass_.pacer, "AGENTS_CONF")),
        StateCopy(import_module("scripts.accounts.worker_names"), "accounts_config_file",
                  state_path(import_module("scripts.accounts.worker_names"), "accounts_config_file", True), True),
        StateCopy(pass_.pacer, "STATE_FILE", state_path(pass_.pacer, "STATE_FILE")),
        StateCopy(pass_.pacer, "PLAN_FILE", state_path(pass_.pacer, "PLAN_FILE")),
        StateCopy(pass_.pacer, "TIER_FILE", state_path(pass_.pacer, "TIER_FILE")),
        StateCopy(pass_.watch, "state_root", state_path(pass_.watch, "state_root", True), True, account_patterns),
        StateCopy(pass_.roles, "groups_file", state_path(pass_.roles, "groups_file", True), True),
        StateCopy(pass_.accounts, "roles_history_file", state_path(pass_.accounts, "roles_history_file", True), True),
        StateCopy(pass_.moves, "store_file", state_path(pass_.moves, "store_file", True), True),
        StateCopy(pass_.next_moves, "next_moves_file", state_path(pass_.next_moves, "next_moves_file", True), True),
        StateCopy(pass_.refusals, "store_file", state_path(pass_.refusals, "store_file", True), True),
        StateCopy(pass_.watchdog, "_state_file", state_path(pass_.watchdog, "_state_file", True), True),
        StateCopy(pass_.record, "_state_root", state_path(pass_.record, "_state_root", True), True,
                  ("snapshots/*.json", "*/*.json")),
        StateCopy(pass_.outage, "_path", state_path(pass_.outage, "_path", True), True),
        StateCopy(pass_.accounts, "login_renewals_file", state_path(pass_.accounts, "login_renewals_file", True), True),
    )


SCRATCH_ROOT = Path("/tmp/claude")


def private_scratch(directory: Path) -> Path:
    resolved = directory.resolve()
    root = SCRATCH_ROOT.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("--scratch must be a private directory below /tmp/claude/")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Each invocation gets fresh copies, even when two profiles share --scratch.
    return Path(tempfile.mkdtemp(prefix="notes-pass-", dir=resolved))


def copy_state(source: Path, destination: Path) -> None:
    try:
        data = source.read_bytes()
    except FileNotFoundError:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    _ = destination.write_bytes(data)
    destination.chmod(0o600)


def redirect_state(pass_: NotesPass, scratch: Path, stack: ExitStack) -> None:
    for entry in state_copies(pass_):
        source = entry.source.expanduser().absolute()
        destination = scratch / "state" / source.relative_to(source.anchor)
        if entry.patterns:
            destination.mkdir(parents=True, exist_ok=True)
            for pattern in entry.patterns:
                for path in source.glob(pattern):
                    copy_state(path, destination / path.relative_to(source))
        else:
            copy_state(source, destination)
        if entry.path_function:
            _ = stack.enter_context(patch.object(entry.owner, entry.attribute, return_value=destination))
        else:
            _ = stack.enter_context(patch.object(entry.owner, entry.attribute, destination))
        if entry.owner is pass_.usage:
            for name in ("timer-usage.json", "by-label.json"):
                copy_state(source.with_name(name), destination.with_name(name))
        if entry.owner is pass_.roles:
            copy_state(source.with_name("role_map_cache.json"), destination.with_name("role_map_cache.json"))

    # Recovery jobs hold absolute result paths. Keep their reads on the copied state.
    watchdog = state_path(pass_.watchdog, "_state_file", True)
    if watchdog.exists():
        try:
            decoded = cast(object, json.loads(watchdog.read_text()))
        except ValueError:
            return  # Leave malformed state for the pass's normal reader to report.
        if not isinstance(decoded, dict):
            return
        state = cast(dict[str, object], decoded)
        changed = False
        for outage in state.values():
            if not isinstance(outage, dict):
                continue
            job = cast(dict[str, object], outage).get("job")
            if not isinstance(job, dict):
                continue
            fields = cast(dict[str, object], job)
            name = fields.get("result")
            if isinstance(name, str):
                source = Path(name).absolute()
                layout = (scratch / "state").resolve()
                destination = layout / source.relative_to(source.anchor)
                if not destination.resolve().is_relative_to(layout):
                    raise ValueError("recovery result path escapes the copied state layout")
                copy_state(source, destination)
                fields["result"] = str(destination)
                changed = True
        if changed:
            _ = watchdog.write_text(json.dumps(state), encoding="utf-8")


def replacement(entry: PassCallPatch, measurements: Measurements) -> Callable[..., object]:
    policy = child_policy(measurements.peer_python) if entry.mode == "child_launch" else ()
    original = cast(Callable[..., object], getattr(entry.owner, entry.attribute))

    def call(*args: object, **kwargs: object) -> object:
        if entry.mode == "open":
            mode = str(args[1] if len(args) > 1 else kwargs.get("mode", "r"))
            if not any(flag in mode for flag in "wax+"):
                return original(*args, **kwargs)
            measurements.count(entry.name)
            if "x" in mode and Path(cast(str, args[0] if args else kwargs["file"])).exists():
                raise FileExistsError("profile: existing marker")
            content: str | bytes = b"" if "b" in mode else ""
            if "+" in mode or mode.startswith("r"):
                read_args, read_kwargs = list(args), dict(kwargs)
                read_mode = "rb" if "b" in mode else "r"
                if len(read_args) > 1:
                    read_args[1] = read_mode
                else:
                    read_kwargs["mode"] = read_mode
                try:
                    handle = cast(io.TextIOBase | io.BufferedIOBase, original(*read_args, **read_kwargs))
                    with handle:
                        content = handle.read()
                except FileNotFoundError:
                    if mode.startswith("r"):
                        raise
            stream = (DiscardedBinaryWrite(cast(bytes, content)) if "b" in mode
                      else DiscardedTextWrite(cast(str, content)))
            if mode.startswith("a"):
                _ = stream.seek(0, io.SEEK_END)
            return stream
        if entry.mode == "os_open":
            flags = cast(int, args[1] if len(args) > 1 else kwargs["flags"])
            if not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                return original(*args, **kwargs)
            measurements.count(entry.name)
            raise OSError("profile: low-level write suppressed")
        measurements.count(entry.name)
        if entry.mode.startswith("child_"):
            process = cast(subprocess.Popen[bytes], args[0])
            if entry.mode == "child_launch":
                words = command_words(args[1] if len(args) > 1 else kwargs["args"])
                if not readonly_launch_options(args, kwargs):
                    measurements.count("subprocess.Popen (acting or unaudited command)")
                    raise OSError("profile: acting or unaudited child suppressed")
                try:
                    launch = checked_child(policy, words, kwargs.get("env"))
                except OSError:
                    measurements.count("subprocess.Popen (acting or unaudited command)")
                    raise
                words = launch.argv
                kwargs["env"] = launch.environment
                if len(args) > 1:
                    args = (args[0], words)
                else:
                    kwargs["args"] = words
                timing = ChildTiming(words[:2], time.perf_counter())
                with measurements.lock:
                    measurements.children.append(timing)
                    measurements.child_by_process[id(process)] = timing
                try:
                    return original(*args, **kwargs)
                except BaseException:
                    timing.completion = ChildCompleted(time.perf_counter())
                    raise
            try:
                return original(*args, **kwargs)
            finally:
                timing = measurements.child_by_process.get(id(process))
                if timing is not None and isinstance(timing.completion, ChildRunning) and process.returncode is not None:
                    timing.completion = ChildCompleted(time.perf_counter())
        if entry.mode == "codex":
            async def cached() -> object:
                home = args[0]
                # This is an action boundary: do not start an app-server on a live home.
                from scripts.whoami import usage_store
                from scripts.whoami.agent_accounts import CodexHomeSource, Report

                try:
                    saved = cast(dict[str, object], json.loads(usage_store.timer_file().read_text()))
                except (OSError, ValueError):
                    saved = {}
                recent = usage_store.recent_timer_usage(str(saved.get("signature", "")), datetime.now(timezone.utc))
                if isinstance(recent, usage_store.RecentTimerUsage):
                    for report in recent.reports:
                        if isinstance(report.source, CodexHomeSource) and report.source.home == home:
                            return report
                return Report("Codex", problem="profile: live Codex server suppressed")
            return cached()
        return entry.result
    return call


def readonly_launch_options(args: tuple[object, ...], kwargs: dict[str, object]) -> bool:
    """Allow only audited argv and normal process I/O; never a launch hook."""
    # Positional launch options can hide executable, shell or preexec_fn.
    if len(args) > 2:
        return False
    defaults: dict[str, object] = {
        "executable": None, "shell": False, "preexec_fn": None,
        "startupinfo": None, "creationflags": 0,
    }
    transport = {"args", "bufsize", "stdin", "stdout", "stderr", "close_fds",
                 "cwd", "env", "universal_newlines", "encoding", "errors", "text",
                 "restore_signals", "start_new_session", "pass_fds", "pipesize"}
    return all(value == defaults[key] if key in defaults else key in transport
               for key, value in kwargs.items())


def command_words(arguments: object) -> tuple[str, ...]:
    if isinstance(arguments, (str, bytes, os.PathLike)):
        return (os.fsdecode(arguments),)
    if isinstance(arguments, Sequence):
        return tuple(os.fsdecode(cast(str | bytes | os.PathLike[str], item))
                     for item in arguments)
    raise OSError("profile: child argv is not readable")


@dataclass(frozen=True)
class TrustedChildRead:
    """A pinned executable, exact read grammar and finite environment changes."""
    names: tuple[str, ...]
    executable: Path
    accepts: Callable[[tuple[str, ...]], bool]
    translate: Callable[[tuple[str, ...]], tuple[str, ...]]
    environment: dict[str, str]
    changes: tuple[tuple[str, str], ...] = ()
    unsets: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckedChildRead:
    argv: tuple[str, ...]
    environment: dict[str, str]


def git_read(args: tuple[str, ...]) -> bool:
    if args[:1] == ("-C",):
        if len(args) < 3 or not args[1]:
            return False
        args = args[2:]
    return args in {
        ("status", "--porcelain"), ("branch", "--show-current"),
        ("symbolic-ref", "HEAD"), ("symbolic-ref", "--short", "HEAD"),
        ("rev-parse", "HEAD"), ("rev-parse", "--show-toplevel"),
        ("rev-parse", "--path-format=absolute", "--show-toplevel"),
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        ("rev-list", "--count", "--invert-grep", "--grep=^agent-sessions-snapshot: ", "@{u}..HEAD"),
    }

def herdr_read(args: tuple[str, ...]) -> bool:
    if args[:1] == ("--session",):
        if len(args) < 3 or not args[1] or args[1].startswith("-"):
            return False
        args = args[2:]
    if args in {("pane", "list"), ("session", "list", "--json"),
                ("workspace", "list"), ("tab", "list"), ("agent", "list"), ("status", "server")}:
        return True
    if len(args) == 4 and args[:3] in {("pane", "process-info", "--pane"), ("tab", "list", "--workspace")}:
        return bool(args[3]) and not args[3].startswith("-")
    if len(args) >= 3 and args[:2] == ("pane", "read") and args[2] and not args[2].startswith("-"):
        tail = args[3:]
        if len(tail) == 0 or tail in {("--source", "visible"), ("--source", "visible", "--format", "ansi")}:
            return True
        if len(tail) < 4:
            return False
        return (len(tail) in {4, 6} and tail[:1] == ("--source",)
                and tail[1] in {"recent", "recent-unwrapped"} and tail[2] == "--lines"
                and tail[3].isdecimal() and (len(tail) == 4 or tail[4:] == ("--format", "ansi")))
    return False



def ps_read(args: tuple[str, ...]) -> bool:
    if args == ("-eo", "pid=,ppid=,comm=,args="):
        return True
    if len(args) == 4 and args[:1] == ("-U",) and args[1] == str(os.getuid()):
        return args[2:] in {("-o", "pid=,ppid=,uid=,comm="), ("-o", "pid=,command=")}
    if args[:1] == ("eww",):
        args = args[1:]
    return (len(args) == 4 and args[0] == "-o" and args[1] in
            {"lstart=", "lstart=,tty=", "command=", "uid=,command="}
            and args[2] == "-p" and args[3].isdecimal())


def lsof_read(args: tuple[str, ...]) -> bool:
    return ((len(args) == 4 and args[:2] == ("-a", "-p") and args[2].isdecimal() and args[3] == "-Fn")
        or (len(args) == 6 and args[:2] == ("-a", "-p") and args[2].isdecimal() and args[3:] == ("-d", "cwd", "-Fn"))
        or args == ("-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"))


def python_read(args: tuple[str, ...]) -> bool:
    message = Path(__file__).resolve().parents[1] / "message"
    return ((len(args) == 3 and Path(args[0]) == message / "sessions.py" and args[1] == "socket"
             and bool(args[2]) and not args[2].startswith("-"))
        or (len(args) == 3 and Path(args[0]) == message / "roster.py" and args[1:] == ("--here", "--snapshot")))


# Only these complete remote reads may be translated to a pinned peer interpreter.
REMOTE_PYTHON_READS = {
    "python3 ~/.claude/scripts/accounts/accounts.py role-map-read": "~/.claude/scripts/accounts/accounts.py role-map-read",
    "python3 ~/.claude/scripts/accounts/accounts.py next-read": "~/.claude/scripts/accounts/accounts.py next-read",
    "~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py --here --snapshot": "~/.claude/scripts/message/roster.py --here --snapshot",
    '\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/message/roster.py\" --here --snapshot': "~/.claude/scripts/message/roster.py --here --snapshot",
}


NO_PEER_PYTHON: Mapping[str, str] = MappingProxyType({})


def child_policy(peer_python: Mapping[str, str] = NO_PEER_PYTHON) -> tuple[TrustedChildRead, ...]:
    """Pin trusted programs once, before the pass or any caller's launch options."""
    environment = dict(os.environ)
    def identity(args: tuple[str, ...]) -> tuple[str, ...]:
        return args
    def remote_read(args: tuple[str, ...]) -> bool:
        return (len(args) == 6 and args[:3] == ("-o", "BatchMode=yes", "-o")
            and args[3] in {"ConnectTimeout=5", "ConnectTimeout=10"}
            and args[4] in peer_python and args[5] in REMOTE_PYTHON_READS
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args[4])))
    def remote_argv(args: tuple[str, ...]) -> tuple[str, ...]:
        interpreter = peer_python[args[4]]
        # A store path is immutable and has no shell syntax. The director pins it.
        if not re.fullmatch(r"/nix/store/[A-Za-z0-9.+_-]+/bin/python3(?:\.[0-9]+)?", interpreter):
            raise OSError("profile: supply the peer's canonical Nix Python path")
        command = ("PYTHONDONTWRITEBYTECODE=1 GIT_OPTIONAL_LOCKS=0 " + interpreter
                   + " -B -I -S " + REMOTE_PYTHON_READS[args[5]])
        return ("-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
                "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "PermitLocalCommand=no",
                "-o", "ProxyCommand=none", *args[:5], command)
    unmarked = ("SHOWRUNNER_UNIT", "SHOWRUNNER_UNIT_ID", "NIX_HEAL_PREPARED_SESSION", "NIX_HEAL_PREPARED_DIRECTORY")
    specs: tuple[tuple[str, Callable[[tuple[str, ...]], bool], Callable[[tuple[str, ...]], tuple[str, ...]], tuple[tuple[str, str], ...], tuple[str, ...]], ...] = (
        ("ps", ps_read, identity, (("LC_ALL", "C"), ("TZ", "UTC")), unmarked),
        ("lsof", lsof_read, lambda args: ("-Di", *args), (), unmarked),
        ("git", git_read, lambda args: ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args),
         (("GIT_OPTIONAL_LOCKS", "0"),), ()),
        ("herdr", herdr_read, identity, (), unmarked),
        ("ssh", remote_read, remote_argv, (), ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")),
        ("security", lambda args: len(args) == 4 and args[:2] == ("find-generic-password", "-s")
         and bool(args[2]) and not args[2].startswith("-") and args[3] == "-w", identity, (), ()),
    )
    rows: list[TrustedChildRead] = []
    for name, accepts, translate, changes, unsets in specs:
        requested = environment.get("HERDR_BIN", name) if name == "herdr" else name
        found = shutil.which(requested, path=environment.get("PATH", os.defpath))
        if found is not None:
            rows.append(TrustedChildRead((name,), Path(found).resolve(), accepts, translate,
                                         environment, changes, unsets))
    rows.append(TrustedChildRead(("python3", Path(sys.executable).name), Path(sys.executable).resolve(),
        python_read, lambda args: ("-B", "-I", "-S", *args), environment,
        (("PYTHONDONTWRITEBYTECODE", "1"),)))
    return tuple(rows)


def checked_child(policy: tuple[TrustedChildRead, ...], words: tuple[str, ...],
                  environment: object = None) -> CheckedChildRead:
    if not words:
        raise OSError("profile: empty child argv")
    for row in policy:
        program = words[0]
        if "/" in program:
            if not Path(program).is_absolute() or Path(program).resolve() != row.executable:
                continue
        elif program not in row.names:
            continue
        if not row.accepts(words[1:]):
            continue
        supplied = row.environment if environment is None else environment
        if not isinstance(supplied, Mapping):
            break
        values = cast(Mapping[str, str], supplied)
        changes = dict(row.changes)
        if any(values.get(key) != value and (key, values.get(key)) not in row.changes
               for key, value in row.environment.items() if key not in row.unsets):
            break
        if any(key not in row.environment and changes.get(key) != value for key, value in values.items()):
            break
        if any(key in values and values[key] != row.environment[key] for key in row.unsets if key in row.environment):
            break
        fixed = dict(values)
        fixed["PYTHONDONTWRITEBYTECODE"] = "1"
        if "git" in row.names:
            fixed.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        return CheckedChildRead((str(row.executable), *row.translate(words[1:])), fixed)
    raise OSError("profile: acting or unaudited child suppressed (pin peer Python for remote reads)")


def readonly_child(words: tuple[str, ...]) -> bool:
    """Check the same policy that constructs the actual launch."""
    try:
        _ = checked_child(child_policy(), words)
        return True
    except OSError:
        return False


def profile_pass(stats: Path, scratch: Path, peer_python: Mapping[str, str] = NO_PEER_PYTHON) -> Measurements:
    private = private_scratch(scratch)
    measurements = Measurements(peer_python=dict(peer_python))
    profiler = cProfile.Profile(timer=time.process_time)
    with ExitStack() as stack:
        _ = stack.enter_context(patch.object(sys, "dont_write_bytecode", True))
        pass_ = load_pass()
        redirect_state(pass_, private, stack)
        stack.enter_context(patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}))
        entries = patch_list(pass_)
        for entry in entries:
            _ = stack.enter_context(patch.object(entry.owner, entry.attribute, replacement(entry, measurements)))
        _ = stack.enter_context(patch.object(sys, "argv", [str(Path(__file__).with_name("agent_notes.py"))]))
        # The pass can log account and session text. Only profile metadata is printed.
        before, children_before = time.process_time(), os.times()
        messages = io.StringIO()
        with redirect_stdout(messages), redirect_stderr(messages):
            profiler.runcall(cast(Callable[[], None], getattr(pass_.notes, "main")))
        measurements.incomplete = tuple(component for component in
            ("refusal watcher:", "codex pacer did not run:") if component in messages.getvalue())
        cpu = time.process_time() - before
        children_after = os.times()
        child_cpu = children_after.children_user + children_after.children_system - children_before.children_user - children_before.children_system
    profiler.dump_stats(str(stats))
    if measurements.incomplete:
        print("incomplete components: " + ", ".join(measurements.incomplete))
    print(f"CPU: {cpu + child_cpu:.6f}s (Python process {cpu:.6f}s; reaped children {child_cpu:.6f}s)")
    _ = pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(25)
    print("children (argv[0..1], count, total wall):")
    totals: dict[tuple[str, ...], tuple[int, float]] = {}
    for child in measurements.children:
        count, wall = totals.get(child.command, (0, 0.0))
        totals[child.command] = count + 1, wall + child.wall_time(time.perf_counter())
    for command, (count, wall) in sorted(totals.items()):
        print(f"  {command!r}: {count}, {wall:.6f}s")
    print("patched (cost not measured):")
    for entry in entries:
        print(f"  {entry.name}: {measurements.calls[entry.name]} — {entry.effect}")
    print(f"  subprocess.Popen (acting or unaudited command): {measurements.calls['subprocess.Popen (acting or unaudited command)']}")
    return measurements


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--stats", required=True, type=Path)
    _ = parser.add_argument("--scratch", required=True, type=Path)
    _ = parser.add_argument("--peer-python", action="append", default=[], metavar="HOST=/nix/store/.../bin/python3",
                            help="pin each peer interpreter; remote reads are refused without it")
    options = parser.parse_args(arguments)
    try:
        peers = dict(value.split("=", 1) for value in cast(list[str], options.peer_python))
        _ = profile_pass(cast(Path, options.stats), cast(Path, options.scratch), peers)
    except (OSError, ValueError) as error:
        print(f"profile_notes_pass: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
