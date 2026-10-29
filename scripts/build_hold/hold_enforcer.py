#!/usr/bin/env python3
"""Stop tests and builds outside a natedev hold, independent of their launch path."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import build_hold
from hold_applies import BUILDING_CARGO_VERBS, cargo_verb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "buildlog"))


class BuildLogWriter(Protocol):
    def host_name(self) -> str: ...
    def root(self) -> Path: ...
    def utc_iso(self, epoch: float) -> str: ...
    def append_line(self, path: Path, record: object) -> None: ...


store = cast(BuildLogWriter, cast(object, importlib.import_module("store")))


@dataclass(frozen=True)
class AllowedBuild:
    process: build_hold.AuditProcess
    owner: build_hold.OwningSession | build_hold.NoOwningSession
    reason: str


@dataclass(frozen=True)
class KeptBuild:
    process: build_hold.AuditProcess
    owner: build_hold.OwningSession | build_hold.NoOwningSession
    holder: build_hold.Holder

    @property
    def reason(self) -> str:
        return f"kept by {self.holder.name}, with PID and start time checked"


@dataclass(frozen=True)
class WouldStopBuild:
    process: build_hold.AuditProcess
    owner: build_hold.OwningSession | build_hold.NoOwningSession
    reason: str
    holder: build_hold.Holder


BuildDecision = AllowedBuild | KeptBuild | WouldStopBuild


def state_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_ENFORCER_DIR", str(Path.home() / ".local/state/build-hold-enforcer")))


def python_target(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Read the Python entry point, not arguments passed to another script."""
    skip_value = False
    for index, argument in enumerate(arguments):
        if skip_value:
            skip_value = False
        elif argument in {"-W", "-X", "--check-hash-based-pycs"}:
            skip_value = True
        elif argument == "--":
            return arguments[index + 1:]
        elif argument in {"-m", "-c"} or not argument.startswith("-"):
            return arguments[index:]
        elif argument.startswith("-m"):
            return ("-m", argument[2:], *arguments[index + 1:])
    return ()


def is_waiting_gate(process: build_hold.AuditProcess) -> bool:
    # These scripts wait themselves. A quoted mention in a shell command is not a script path.
    if not process.argv:
        return False
    executable = Path(process.argv[0]).name
    if executable == "verify.sh":
        return True
    if executable not in {"bash", "sh", "dash", "zsh"}:
        return False
    for argument in process.argv[1:]:
        if argument in {"-c", "-lc", "-ic"}:
            return False
        if not argument.startswith("-"):
            return Path(argument).name == "verify.sh"
    return False


def is_build(process: build_hold.AuditProcess) -> bool:
    names = {process.executable, Path(process.argv[0]).name if process.argv else process.command}
    if "cargo" in names:
        return cargo_verb(process.argv[1:]) in BUILDING_CARGO_VERBS
    if names & ((build_hold.AUDIT_COMMANDS - {"cargo"}) | {"clippy-driver", "pytest", "py.test", "basedpyright", "merge_gate.py"}):
        return True
    if "rust-analyzer" in names:
        # Ownership needs ancestors, so it is checked by decide_builds.
        return True
    if any(name.startswith("python") for name in names):
        if any(Path(name).match("test_*.py") or Path(name).match("*_test.py") for name in names):
            return True
        target = python_target(process.argv[1:])
        return (target[:2] in {("-m", "unittest"), ("-m", "pytest")}
                or target[:2] == ("-m", "scripts.production.merge_gate")
                or bool(target) and (Path(target[0]).name in {"pytest", "py.test", "merge_gate.py", "basedpyright"}
                                     or Path(target[0]).match("test_*.py") or Path(target[0]).match("*_test.py")))
    if names & {"node", "nodejs"}:
        return any(Path(argument).name in {"basedpyright", "basedpyright.js"} for argument in process.argv[1:])
    if names & {"bash", "sh", "dash", "zsh"}:
        if any(Path(name).match("test_*.sh") for name in names):
            return True
        for argument in process.argv[1:]:
            if argument in {"-c", "-lc", "-ic"}:
                return False
            if not argument.startswith("-"):
                return Path(argument).match("test_*.sh")
    return False


def is_build_like(process: build_hold.AuditProcess) -> bool:
    return is_build(process) or is_waiting_gate(process)


def is_sccache_server(process: build_hold.AuditProcess) -> bool:
    names = {process.executable, Path(process.argv[0]).name if process.argv else process.command}
    return "sccache" in names and (
        any(argument in {"--start-server", "--server", "server"} for argument in process.argv[1:])
        or process.environment.get("SCCACHE_START_SERVER", "") in {"1", "true"}
        or process.environment.get("SCCACHE_SERVER", "") in {"1", "true"})


def protected_reason(
    process: build_hold.AuditProcess, ancestors: Sequence[build_hold.AuditProcess],
    owner: build_hold.OwningSession | build_hold.NoOwningSession,
    holders: Sequence[build_hold.Holder], enforcer_ancestors: frozenset[build_hold.ProcessIdentity],
) -> str:
    if process.identity in enforcer_ancestors:
        return "enforcer or its ancestor"
    if is_waiting_gate(process):
        return "verify.sh sends tests to the Mac and waits at its own gate"
    if any(is_sccache_server(item) for item in (process, *ancestors)):
        return "sccache server compiles for the holder too"
    if build_hold.owner_is_sole_holder(owner, holders):
        return "owned by the sole in-force holder"
    lineage = (process, *ancestors)
    markers = {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR",
               "PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_SESSION_DIR"}
    if (not any(build_hold.audit_command(item) in {"claude", "codex"} for item in lineage)
            and not any(item.environment.get(marker) for item in lineage for marker in markers)
            and any(item.tty_nr != 0 or item.desktop_app for item in lineage)):
        return "user terminal or desktop app, with no agent ancestor or session marker"
    return ""


def decide_builds(
    processes: Mapping[int, build_hold.AuditProcess], holders: Sequence[build_hold.Holder],
    uid: int, enforcer_pid: int,
) -> tuple[BuildDecision, ...]:
    protected: frozenset[build_hold.ProcessIdentity] = frozenset()
    if enforcer_pid in processes:
        enforcer = processes[enforcer_pid]
        protected = frozenset(item.identity for item in (enforcer, *build_hold.audit_ancestors(enforcer, processes)))
    decisions: list[BuildDecision] = []
    for process in sorted(processes.values(), key=lambda item: (len(build_hold.audit_ancestors(item, processes)), item.identity.pid)):
        if process.uid != uid or not is_build_like(process):
            continue
        ancestors = build_hold.audit_ancestors(process, processes)
        owner = build_hold.audit_owner(process, ancestors)
        names = {process.executable, Path(process.argv[0]).name if process.argv else process.command}
        if "rust-analyzer" in names and isinstance(owner, build_hold.NoOwningSession):
            continue
        reason = protected_reason(process, ancestors, owner, holders, protected)
        if reason or not holders:
            decisions.append(AllowedBuild(process, owner, reason or "no hold is in force"))
            continue
        kept = next((holder for holder in holders if any(
            item.identity in holder.keep for item in (process, *ancestors))), None)
        if kept is not None:
            decisions.append(KeptBuild(process, owner, kept))
            continue
        owner_key = build_hold.holder_path(Path(), owner.session_id).name if isinstance(owner, build_hold.OwningSession) else ""
        blocking = [holder for holder in holders if holder.key != owner_key] or list(holders)
        holder = min(blocking, key=lambda item: item.since)
        decisions.append(WouldStopBuild(process, owner, f"test or build outside {holder.name}'s hold for {holder.purpose}", holder))
    return tuple(decisions)


def record_build(
    build: build_hold.AuditedBuild, holder: build_hold.Holder, proc_root: Path,
    action: Literal["audit", "stopped"],
) -> None:
    """Audit and enforcement use the same permanent log and append writer."""
    try:
        boot = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = ""
    identity = build.process.identity
    hold_id = hashlib.sha256(json.dumps([holder.key, holder.since.isoformat()]).encode()).hexdigest()
    build_id = hashlib.sha256(json.dumps([boot, str(identity.pid), identity.start_time]).encode()).hexdigest()
    host = store.host_name()
    path = store.root() / host / f"hold-builds-{hold_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep duplicate reads and the shared append in one lock, without nesting the append writer's file lock.
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            value = cast(object, json.loads(line))
            if isinstance(value, dict):
                row = cast(dict[str, object], value)
                if row.get("id") == build_id and row.get("action", "audit") == action:
                    return
        store.append_line(path, {
            "kind": "hold_build", "v": 1, "id": build_id, "at": store.utc_iso(time.time()), "host": host,
            "hold_key": holder.key, "hold_since": holder.since.isoformat(), "boot_id": boot,
            "pid": identity.pid, "proc_start": identity.start_time, "class": build.classification,
            "session": build.owner.session_id if isinstance(build.owner, build_hold.OwningSession) else "",
            "command": shlex.join(build.process.argv) or build.process.command, "cwd": build.process.cwd,
            "action": action,
        })


def refusal_line(build: WouldStopBuild) -> str:
    return (f"natedev builds and tests are held by {build.holder.name} for {build.holder.purpose}; "
            "this run was stopped. You get a notice at the release; run it again then.\n")


def write_refusal(proc_root: Path, build: WouldStopBuild) -> None:
    """Best effort only; never wait on a full stderr pipe."""
    try:
        descriptor = os.open(proc_root / str(build.process.identity.pid) / "fd/2", os.O_WRONLY | os.O_NONBLOCK)
        try:
            _ = os.write(descriptor, refusal_line(build).encode())
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        pass


class StopBuildsAndTests:
    def __init__(
        self, proc_root: Path, *, signal_process: Callable[[int, int], None] = os.kill,
        clock: Callable[[], float] = time.monotonic,
        record: Callable[[build_hold.AuditedBuild, build_hold.Holder, Path, Literal["audit", "stopped"]], None] = record_build,
        register: Callable[[str], str] = build_hold.register_wait,
    ) -> None:
        self.proc_root: Path = proc_root
        self.signal_process: Callable[[int, int], None] = signal_process
        self.clock: Callable[[], float] = clock
        self.record: Callable[[build_hold.AuditedBuild, build_hold.Holder, Path, Literal["audit", "stopped"]], None] = record
        self.terminating: dict[build_hold.ProcessIdentity, float] = {}
        self.register: Callable[[str], str] = register

    def signal_if_current(self, identity: build_hold.ProcessIdentity, signum: int) -> bool:
        if build_hold.process_start_time(identity.pid, self.proc_root) != identity.start_time:
            return False
        try:
            self.signal_process(identity.pid, signum)
            return True
        except ProcessLookupError:
            return False

    def kill_survivors(self, processes: Mapping[int, build_hold.AuditProcess],
                       holders: Sequence[build_hold.Holder], enforcer_pid: int) -> None:
        if not self.terminating:
            return
        now = self.clock()
        allowed = {item.process.identity for item in decide_builds(processes, holders,
                   os.getuid(), enforcer_pid) if not isinstance(item, WouldStopBuild)}
        for identity, deadline in tuple(self.terminating.items()):
            process = processes.get(identity.pid)
            lineage = (process, *build_hold.audit_ancestors(process, processes)) if process is not None else ()
            if identity in allowed or any(item.identity in holder.keep for holder in holders for item in lineage):
                del self.terminating[identity]
                continue
            if now >= deadline:
                _ = self.signal_if_current(identity, signal.SIGKILL)
                del self.terminating[identity]

    def stop(self, build: WouldStopBuild, processes: Mapping[int, build_hold.AuditProcess],
             holders: Sequence[build_hold.Holder], enforcer_pid: int) -> None:
        root = build.process
        if root.identity in self.terminating:
            return
        ancestors = build_hold.audit_ancestors(root, processes)
        # An ancestor can exit or a PID can be reused after the table read.
        if any(build_hold.process_start_time(item.identity.pid, self.proc_root) != item.identity.start_time
               for item in (root, *ancestors)):
            return
        write_refusal(self.proc_root, build)
        classification: build_hold.BuildClass = "other_session" if isinstance(build.owner, build_hold.OwningSession) else "no_session"
        self.record(build_hold.AuditedBuild(root, build.owner, classification, ()), build.holder, self.proc_root, "stopped")
        if isinstance(build.owner, build_hold.OwningSession):
            _ = self.register(build.owner.session_id)
        # Include all descendants, not only compilers. Parents precede their children.
        children: dict[int, list[build_hold.AuditProcess]] = {}
        for process in processes.values():
            children.setdefault(process.parent_pid, []).append(process)
        protected: frozenset[build_hold.ProcessIdentity] = frozenset()
        if enforcer_pid in processes:
            enforcer = processes[enforcer_pid]
            protected = frozenset(item.identity for item in (enforcer, *build_hold.audit_ancestors(enforcer, processes)))
        pending = [root]
        seen: set[build_hold.ProcessIdentity] = set()
        while pending:
            process = pending.pop(0)
            if process.identity in seen or process.uid != root.uid:
                continue
            seen.add(process.identity)
            parents = build_hold.audit_ancestors(process, processes)
            owner = build_hold.audit_owner(process, parents)
            if protected_reason(process, parents, owner, holders, protected):
                continue
            if any(item.identity in holder.keep for holder in holders for item in (process, *parents)):
                continue
            if process.identity not in self.terminating and self.signal_if_current(process.identity, signal.SIGTERM):
                self.terminating[process.identity] = self.clock() + 2
            pending.extend(child for child in children.get(process.identity.pid, [])
                           if int(child.identity.start_time) >= int(process.identity.start_time))


def start() -> None:
    """Keep an active unit; otherwise start the service or one detached fallback."""
    script = Path(__file__).resolve()
    python = str(Path(sys.executable).resolve())
    if shutil.which("systemctl"):
        try:
            active = subprocess.run(["systemctl", "--user", "is-active", "build-hold-enforcer"],
                                    capture_output=True, text=True, timeout=5)
            if active.returncode == 0 or active.stdout.strip() == "activating":
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    bus = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")) or (Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")) / "bus").exists()
    if bus and shutil.which("systemd-run"):
        try:
            launched = subprocess.run([
                "systemd-run", "--user", "--unit=build-hold-enforcer", "--collect", "--", python, str(script), "run",
            ], capture_output=True, text=True, timeout=5)
            if launched.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    _ = subprocess.Popen([python, str(script), "run"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def run(
    proc_root: Path, holder_directory: Path, directory: Path, *,
    uid: int, enforcer_pid: int,
    stopper: StopBuildsAndTests, sleep: Callable[[float], None] = time.sleep,
    cgroup_root: Path = build_hold.SYSTEM_CGROUP_ROOT,
) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        while True:
            holders = build_hold.natedev_holds(holder_directory).in_force
            if not holders:
                return 0
            processes = build_hold.read_audit_processes(proc_root, build_hold.HoldBuildCandidates(uid), cgroup_root)
            stopper.kill_survivors(processes, holders, enforcer_pid)
            for decision in decide_builds(processes, holders, uid, enforcer_pid):
                if isinstance(decision, WouldStopBuild):
                    # Recheck the holder set in case a release or another hold followed the table read.
                    current_holders = build_hold.natedev_holds(holder_directory).in_force
                    if not current_holders:
                        return 0
                    current = decision if current_holders == holders else next(
                        item for item in decide_builds(processes, current_holders, uid, enforcer_pid)
                        if item.process.identity == decision.process.identity)
                    if isinstance(current, WouldStopBuild):
                        stopper.stop(current, processes, current_holders, enforcer_pid)
            sleep(1)


def decision_lines(decisions: Sequence[BuildDecision]) -> list[str]:
    lines: list[str] = []
    for decision in decisions:
        label = "would stop" if isinstance(decision, WouldStopBuild) else "kept" if isinstance(decision, KeptBuild) else "allowed"
        owner = decision.owner.session_id if isinstance(decision.owner, build_hold.OwningSession) else "no session"
        command = shlex.join(decision.process.argv) or decision.process.command
        keep = f"; --keep {decision.process.identity.pid}" if isinstance(decision, WouldStopBuild) else ""
        lines.append(f"{label}: {owner}, pid {decision.process.identity.pid}: {command}; {decision.reason}{keep}")
    return lines


def report(proc_root: Path, directory: Path) -> list[str]:
    holders = build_hold.natedev_holds(directory).in_force
    lines = [f"kept by {holder.name}: pid {run.pid}, start {run.start_time}"
             for holder in holders for run in holder.keep]
    processes = build_hold.read_audit_processes(proc_root, build_hold.HoldBuildCandidates(os.getuid()))
    return lines + decision_lines(decide_builds(processes, holders, os.getuid(), os.getpid()))


def scan(proc_root: Path, holder_directory: Path, uid: int, enforcer_pid: int,
         cgroup_root: Path = build_hold.SYSTEM_CGROUP_ROOT) -> None:
    began = time.monotonic()
    holders = build_hold.natedev_holds(holder_directory).in_force
    processes = build_hold.read_audit_processes(proc_root, build_hold.HoldBuildCandidates(uid), cgroup_root)
    for line in decision_lines(decide_builds(processes, holders, uid, enforcer_pid)):
        print(line)
    print(f"pass: {time.monotonic() - began:.6f} s; {len(processes)} processes")


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("run", "scan"))
    _ = parser.add_argument("--proc-root", type=Path, default=build_hold.PROC_ROOT)
    options = parser.parse_args(arguments)
    proc_root = cast(Path, options.proc_root)
    try:
        if cast(str, options.command) == "scan":
            scan(proc_root, build_hold.holder_directory(), os.getuid(), os.getpid())
            return 0
        return run(proc_root, build_hold.holder_directory(), state_directory(),
                   uid=os.getuid(), enforcer_pid=os.getpid(), stopper=StopBuildsAndTests(proc_root))
    except (OSError, ValueError) as error:
        print(f"hold enforcer: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
