#!/usr/bin/env python3
"""One holder file per session; decide when builds are quiet and may resume."""

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
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, time as ClockTime, timezone
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
from sessions import addressed, live_sessions  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from process_failure import failure_detail
from user_zone import user_zone
import unit_runs
sys.path.insert(0, str(Path(__file__).resolve().parent))
import override_guard
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_targets"))
from target_config import ConfigError, read_targets


BUILD_COMMANDS = frozenset({"cargo", "rustc", "cargo-nextest"})
PROC_ROOT = Path("/proc")
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")
SAFE = re.compile(r"[^A-Za-z0-9._-]")
# Retry a failed session lookup before reporting an undelivered release notice.
LOOKUP_TRIES = 3
LOOKUP_RETRY_S = 1.0
RELEASE_NOTICE = ("/build:hold: the build hold has been released. Start your builds and BRP launches now; "
                  "their memory gate orders them. No answer is needed.")

DeliveryOutcome = Literal["NotAttempted", "Delivered", "QueuedUnconfirmed", "RecipientGone", "DeliveryFailed"]


@dataclass(frozen=True)
class OwningSession:
    session_id: str


@dataclass(frozen=True)
class NoOwningSession:
    pass


class ReleaseEntry(TypedDict):
    session_id: str
    name: str
    delivery: DeliveryOutcome


class HoldCycle(TypedDict):
    id: str
    opened_at: str
    holders: dict[str, dict[str, str]]
    recipients: dict[str, str]
    entries: list[ReleaseEntry]
    release_started_at: str


class ReleaseRecordReadError(ValueError):
    """The stored hold cycle cannot be read safely."""


@dataclass(frozen=True)
class NoCycle:
    pass


class CycleReleaseNotices(TypedDict):
    kind: Literal["cycle"]
    cycle_id: str
    recipients: dict[str, str]


class BroadcastReleaseNotice(TypedDict):
    kind: Literal["broadcast"]


class ReleaseIntent(TypedDict):
    holder_key: str
    holder_name: str
    holder_since: str
    holder_directory: str
    released_at: str
    notices: CycleReleaseNotices | BroadcastReleaseNotice


@dataclass(frozen=True)
class NoReleaseIntent:
    pass


@dataclass(frozen=True)
class RecordedRelease:
    path: Path
    intent: ReleaseIntent
    state: Literal["pending", "completed"]


@dataclass(frozen=True)
class DamagedRecordSetAside:
    path: Path


@dataclass(frozen=True)
class ReleaseRecordsForChange:
    records: list[RecordedRelease]
    set_aside: list[DamagedRecordSetAside]


@dataclass(frozen=True)
class CycleNoticesToSend:
    cycle: HoldCycle
    entries: list[ReleaseEntry]


class ReleaseNoticeReadError(ReleaseRecordReadError):
    path: Path

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path


def release_record_error_line(error: ReleaseRecordReadError) -> str:
    return f"release record could not be read: {error}; /build:hold release sets it aside and ends the hold"


@dataclass(frozen=True)
class RecipientGone:
    pass


@dataclass(frozen=True)
class RecipientLive:
    socket: str


@dataclass(frozen=True)
class RecipientLookupFailed:
    reason: str


@dataclass(frozen=True)
class ReleaseDelivered:
    pass


@dataclass(frozen=True)
class ReleaseQueued:
    pass


@dataclass(frozen=True)
class ReleaseSendFailed:
    reason: str


@dataclass(frozen=True)
class KnownReleaseEta:
    at: datetime


@dataclass(frozen=True)
class UnknownReleaseEta:
    pass


ReleaseEta = KnownReleaseEta | UnknownReleaseEta


@dataclass(frozen=True)
class NoReleaseEta:
    pass


@dataclass(frozen=True)
class ClockReleaseEta:
    clock: ClockTime
    zone: ZoneInfo


ReleaseRequest = NoReleaseEta | ClockReleaseEta


@dataclass(frozen=True)
class HolderIdentity:
    key: str
    name: str


@dataclass(frozen=True)
class NatedevHoldTarget:
    pass


@dataclass(frozen=True)
class MacHoldTarget:
    pass


@dataclass(frozen=True)
class UnknownHoldTarget:
    name: str
    configured: tuple[str, ...]


@dataclass(frozen=True)
class UncheckedHoldTarget:
    name: str
    reason: str


HoldTarget = NatedevHoldTarget | MacHoldTarget | UnknownHoldTarget | UncheckedHoldTarget


def parse_hold_target(name: str) -> HoldTarget:
    if name == "natedev":
        return NatedevHoldTarget()
    if name == "mac":
        return MacHoldTarget()
    try:
        configured = read_targets()
    except (ConfigError, UnicodeError) as error:
        return UncheckedHoldTarget(name, str(error))
    return UnknownHoldTarget(name, tuple(target.name for target in configured))


@dataclass(frozen=True)
class MacHoldResult:
    status: int
    stdout: str
    stderr: str


def run_mac_hold(arguments: Sequence[str]) -> MacHoldResult:
    script = Path(__file__).resolve().parent.parent / "mac_test" / "mac_test.py"
    result = subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True)
    return MacHoldResult(result.returncode, result.stdout, result.stderr)


def release_mac_hold(identity: HolderIdentity, given: str) -> MacHoldResult:
    result = run_mac_hold(["unblock", "--holder", identity.name])
    prefix = "blocked by "
    line = result.stdout.rstrip("\n")
    if result.status != 1 or not line.startswith(prefix):
        return result
    holder = line[len(prefix):]
    no_former_names: list[str] = []
    former = next((record["formerNames"] for record in live_sessions()
                   if record["sessionId"] == identity.key), no_former_names)
    if holder != identity.name and holder in (identity.name, given, *former):
        return run_mac_hold(["unblock", "--holder", holder])
    return result


def print_mac_hold_result(result: MacHoldResult) -> int:
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.status


@dataclass(frozen=True)
class Holder:
    # The holder's name now when it is a live Claude session, else the name the hold was taken under.
    name: str
    since: datetime
    purpose: str
    release: ReleaseEta
    # The hold file's name: the holder's Claude session id, or its name when it is no Claude session.
    key: str
    keep: tuple[ProcessIdentity, ...] = ()


@dataclass(frozen=True)
class NatedevHolds:
    in_force: tuple[Holder, ...]


@dataclass(frozen=True)
class NoHolders:
    pass


@dataclass(frozen=True)
class ActiveHolders:
    holders: tuple[Holder, ...]

    def __post_init__(self) -> None:
        if not self.holders:
            raise ValueError("active holders cannot be empty")
        if tuple(sorted(self.holders, key=lambda holder: holder.since)) != self.holders:
            raise ValueError("active holders must be ordered by since")


HoldState = NoHolders | ActiveHolders


@dataclass(frozen=True)
class Process:
    pid: int
    user: str
    command: str


@dataclass(frozen=True)
class ProcessCpuTime:
    ticks: int
    started_at_ticks: int


@dataclass(frozen=True)
class CurrentProcessCpu:
    process: Process
    cpu_percent: float


@dataclass(frozen=True)
class Quiet:
    processes: tuple[Process, ...]


@dataclass(frozen=True)
class Busy:
    reasons: tuple[str, ...]


def owning_session(environment: Mapping[str, str] = os.environ) -> OwningSession | NoOwningSession:
    """Find the session that owns this run before using the shell's session id."""
    run = (environment.get("UNIT_RUN_DIR") or environment.get("PLAN_DELEGATE_BOARD_DIR")
           or environment.get("UNIT_SESSION_DIR") or environment.get("PLAN_DELEGATE_SESSION_DIR"))
    if run:
        markers: list[Path] = []
        for active in unit_runs.active_dirs(
            environment.get("UNIT_ACTIVE_DIR") or environment.get("PLAN_DELEGATE_ACTIVE_DIR") or None
        ):
            try:
                markers.extend(active.iterdir())
            except OSError:
                continue
        matches: list[tuple[int, str]] = []
        for marker in markers:
            try:
                if marker.is_file() and marker.read_text().strip() == run:
                    matches.append((marker.stat().st_mtime_ns, marker.name))
            except (OSError, UnicodeError):
                continue
        if matches:
            return OwningSession(max(matches)[1])
    session_id = environment.get("CLAUDE_CODE_SESSION_ID", "")
    return OwningSession(session_id) if session_id else NoOwningSession()


def process_start_time(pid: int, proc_root: Path = PROC_ROOT) -> str:
    if pid <= 0:
        return ""
    try:
        fields = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else ""
    except (OSError, IndexError):
        return ""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: str


@dataclass(frozen=True)
class AuditProcess:
    identity: ProcessIdentity
    parent_pid: int
    command: str
    argv: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]
    in_build_scope: bool
    uid: int = -1
    tty_nr: int = 0
    executable: str = ""
    desktop_app: bool = False


@dataclass(frozen=True)
class FullBuildAudit:
    pass


@dataclass(frozen=True)
class HoldBuildCandidates:
    uid: int


FULL_BUILD_AUDIT = FullBuildAudit()
SYSTEM_CGROUP_ROOT = Path("/sys/fs/cgroup")


BuildClass = Literal["holder", "admitted", "other_session", "no_session"]
AUDIT_COMMANDS = frozenset({"cargo-tile-real", "cargo", "cargo-nextest", "rustc", "rustdoc"})
CARGO_COMMANDS = AUDIT_COMMANDS - {"rustc", "rustdoc"}


@dataclass(frozen=True)
class AuditedBuild:
    process: AuditProcess
    owner: OwningSession | NoOwningSession
    classification: BuildClass
    children: tuple[ProcessIdentity, ...]


def read_audit_processes(
    proc_root: Path, selection: FullBuildAudit | HoldBuildCandidates = FULL_BUILD_AUDIT,
    cgroup_root: Path = SYSTEM_CGROUP_ROOT,
) -> dict[int, AuditProcess]:
    """Read the table once; an enforcer reads details only for candidates and ancestors."""
    processes: dict[int, AuditProcess] = {}
    desktop_pids: set[int] = set()
    if isinstance(selection, HoldBuildCandidates):
        cgroup_root = Path(os.environ.get("BUILD_HOLD_CGROUP_ROOT", str(cgroup_root)))
        apps = cgroup_root / f"user.slice/user-{selection.uid}.slice/user@{selection.uid}.service/app.slice"
        for unit in apps.glob("app-*"):
            if not (unit.name.endswith(".scope") or "@" in unit.name and unit.name.endswith(".service")):
                continue
            for file in unit.rglob("cgroup.procs"):
                try:
                    desktop_pids.update(int(pid) for pid in file.read_text().split() if pid.isdecimal())
                except OSError:
                    continue
    for directory in proc_root.iterdir():
        if not directory.name.isdecimal():
            continue
        pid = int(directory.name)
        try:
            stat = (directory / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()
            start = stat[19]
            if stat[0] == "Z" or not start.isdecimal():
                continue
            parent_pid = int(stat[1])
            command = (directory / "comm").read_text(errors="replace").strip()
            uid, tty_nr = directory.stat().st_uid, int(stat[4])
        except (OSError, ValueError, IndexError):
            continue
        try:
            executable = (directory / "exe").readlink().name.removesuffix(" (deleted)")
        except OSError:
            executable = command
        processes[pid] = AuditProcess(ProcessIdentity(pid, start), parent_pid, command, (), "", {}, False,
                                      uid, tty_nr, executable, pid in desktop_pids)
    details: set[int] = set()
    for pid, process in processes.items():
        if (isinstance(selection, FullBuildAudit) or process.uid == selection.uid and
                any(name in AUDIT_COMMANDS | {"pytest", "py.test", "sccache", "bash", "sh", "dash", "zsh",
                                             "verify.sh", "merge_gate.py", "clippy-driver", "rust-analyzer",
                                             "basedpyright", "node", "nodejs"}
                    or name.startswith("python") for name in (process.command, process.executable))):
            details.add(pid)
            details.update(parent.identity.pid for parent in audit_ancestors(process, processes))
    for pid in details:
        process = processes[pid]
        directory = proc_root / str(pid)
        try:
            argv = tuple(part.decode("utf-8", "replace") for part in
                         (directory / "cmdline").read_bytes().split(b"\0") if part)
        except OSError:
            argv = ()
        try:
            environment = dict(part.decode("utf-8", "replace").split("=", 1) for part in
                               (directory / "environ").read_bytes().split(b"\0") if b"=" in part)
        except OSError:
            environment = {}
        try:
            cwd = str((directory / "cwd").readlink())
        except OSError:
            cwd = ""
        in_build_scope = False
        if isinstance(selection, FullBuildAudit):
            try:
                in_build_scope = any("/builds.slice/" in line
                                     for line in (directory / "cgroup").read_text(errors="replace").splitlines())
            except OSError:
                pass
        if process_start_time(pid, proc_root) != process.identity.start_time:
            del processes[pid]
            continue
        processes[pid] = AuditProcess(process.identity, process.parent_pid, process.command, argv, cwd,
                                      environment, in_build_scope, process.uid, process.tty_nr,
                                      process.executable, process.desktop_app)
    return processes


def audit_ancestors(process: AuditProcess, processes: Mapping[int, AuditProcess]) -> tuple[AuditProcess, ...]:
    ancestors: list[AuditProcess] = []
    seen = {process.identity.pid}
    while process.parent_pid in processes and process.parent_pid not in seen:
        parent = processes[process.parent_pid]
        # A new process at an old parent PID cannot own the child.
        if int(parent.identity.start_time) > int(process.identity.start_time):
            break
        ancestors.append(parent)
        seen.add(parent.identity.pid)
        process = parent
    return tuple(ancestors)


def audit_command(process: AuditProcess) -> str:
    executable = Path(process.argv[0]).name if process.argv else process.command
    return executable if executable in AUDIT_COMMANDS | {"claude", "codex"} else process.command


def audit_owner(process: AuditProcess, ancestors: Sequence[AuditProcess]) -> OwningSession | NoOwningSession:
    for item in (process, *ancestors):
        session_id = item.environment.get("CLAUDE_CODE_SESSION_ID", "")
        if session_id:
            return OwningSession(session_id)
        owner = owning_session(item.environment)
        if isinstance(owner, OwningSession):
            return owner
        if item.environment.get("CODEX_THREAD_ID"):
            return OwningSession(item.environment["CODEX_THREAD_ID"])
    for parent in ancestors:
        if audit_command(parent) in {"claude", "codex"}:
            owner = owning_session(parent.environment)
            if isinstance(owner, OwningSession):
                return owner
            return OwningSession(parent.environment.get("CODEX_THREAD_ID")
                                 or f"{audit_command(parent)}:{parent.identity.pid}:{parent.identity.start_time}")
    return NoOwningSession()


def admitted_processes(buildlog_root: Path) -> frozenset[ProcessIdentity]:
    """Read memory gate reservations without pruning or changing the ledger."""
    admitted: set[ProcessIdentity] = set()
    for path in (buildlog_root / "admission").glob("*.reservation"):
        try:
            value = cast(object, json.loads(path.read_text()))
            if not isinstance(value, dict):
                continue
            row = cast(dict[str, object], value)
            pid, start = row.get("pid"), row.get("start_time")
            if isinstance(pid, int) and isinstance(start, str) and start.isdecimal():
                admitted.add(ProcessIdentity(pid, start))
        except (OSError, ValueError):
            continue
    return frozenset(admitted)


def owner_is_sole_holder(owner: OwningSession | NoOwningSession, holders: Sequence[Holder]) -> bool:
    return (isinstance(owner, OwningSession) and bool(holders)
            and all(holder.key == holder_path(Path(), owner.session_id).name for holder in holders))


class HoldEnforcer(Protocol):
    def start(self) -> None: ...
    def record_build(self, build: AuditedBuild, holder: Holder, proc_root: Path,
                     action: Literal["audit", "stopped"]) -> None: ...
    def report(self, proc_root: Path, directory: Path) -> list[str]: ...


def hold_enforcer() -> HoldEnforcer:
    # The enforcer uses these audit types; load it after this module is ready.
    import importlib
    return cast(HoldEnforcer, cast(object, importlib.import_module("hold_enforcer")))


def audit_builds(proc_root: Path, holders: Sequence[Holder], buildlog_root: Path) -> tuple[AuditedBuild, ...]:
    processes = read_audit_processes(proc_root)
    admitted = admitted_processes(buildlog_root)
    builds: list[AuditedBuild] = []
    children: dict[int, list[ProcessIdentity]] = {}
    roots: list[AuditProcess] = []
    for process in processes.values():
        if audit_command(process) not in AUDIT_COMMANDS:
            continue
        ancestors = audit_ancestors(process, processes)
        cargo_parents = [parent for parent in ancestors if audit_command(parent) in CARGO_COMMANDS]
        if cargo_parents:
            children.setdefault(cargo_parents[-1].identity.pid, []).append(process.identity)
        else:
            roots.append(process)
    for process in sorted(roots, key=lambda item: item.identity.pid):
        ancestors = audit_ancestors(process, processes)
        if any(process_start_time(item.identity.pid, proc_root) != item.identity.start_time
               for item in (process, *ancestors)):
            continue
        owner = audit_owner(process, ancestors)
        classification: BuildClass
        if owner_is_sole_holder(owner, holders):
            classification = "holder"
        elif process.in_build_scope or any(item.identity in admitted for item in (process, *ancestors)):
            classification = "admitted"
        elif isinstance(owner, OwningSession):
            classification = "other_session"
        else:
            classification = "no_session"
        live_children = tuple(sorted((child for child in children.get(process.identity.pid, [])
                                      if process_start_time(child.pid, proc_root) == child.start_time),
                                     key=lambda item: item.pid))
        builds.append(AuditedBuild(process, owner, classification, live_children))
    return tuple(builds)


def audit_lines(builds: Sequence[AuditedBuild], *, beginning: bool = False) -> list[str]:
    lines = ["Build audit: tests and builds outside the hold are stopped; use --keep <pid> to keep a run."
             if beginning else "Build audit:"]
    for build in builds:
        if build.classification == "no_session":
            continue
        owner = build.owner.session_id if isinstance(build.owner, OwningSession) else ""
        command = shlex.join(build.process.argv) or build.process.command
        lines.append(f"  {build.classification}: {owner}, pid {build.process.identity.pid}: {command}"
                     + (f" ({len(build.children)} build children)" if build.children else ""))
    if len(lines) == 1:
        lines.append("  No session-owned builds are running.")
    return lines


def audit_hold(directory: Path, proc_root: Path) -> tuple[AuditedBuild, ...]:
    try:
        holders = natedev_holds(directory).in_force
    except ReleaseRecordReadError:
        state = read_holders(directory)
        holders = state.holders if isinstance(state, ActiveHolders) else ()
    buildlog_root = Path(os.environ.get("BUILDLOG_DIR") or Path.home() / ".local/state/buildlog")
    builds = audit_builds(proc_root, holders, buildlog_root)
    for build in builds:
        if build.classification in {"holder", "admitted"}:
            continue
        for holder in holders:
            hold_enforcer().record_build(build, holder, proc_root, "audit")
    return builds


def print_hold_audit(directory: Path, proc_root: Path, *, beginning: bool = False) -> None:
    try:
        builds = audit_hold(directory, proc_root)
    except (OSError, ValueError) as error:
        print(f"Build audit unavailable: {error}")
        return
    print("\n".join(audit_lines(builds, beginning=beginning)))
    try:
        for line in hold_enforcer().report(proc_root, directory):
            print(line)
    except (OSError, ValueError) as error:
        print(f"Hold enforcer report unavailable: {error}")


def holder_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))


def holder_path(directory: Path, name: str) -> Path:
    safe = SAFE.sub("-", name)
    if not safe or safe in {".", ".."}:
        raise ValueError("holder name must contain a file-name character")
    return directory / safe


def holder_of(given: str | None) -> HolderIdentity:
    """The key a holder's hold is kept under, and the holder's name now.

    The key is the Claude session id of the session `given` means, or of the caller when no name is
    given, so a rename between hold and release loses nothing. A holder that is no live Claude
    session keeps `given` as its key.
    """
    records = live_sessions()
    if given is None:
        own = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        session = next((record for record in records if own and record["sessionId"] == own), None)
        if session is None:
            raise ValueError("give --holder: this is not a live Claude session")
        return HolderIdentity(session["sessionId"], session["name"])
    session = addressed(given, records)
    return HolderIdentity(given, given) if session is None else HolderIdentity(session["sessionId"], session["name"])


def held_key(directory: Path, given: str | None) -> str:
    """The key of the hold `given`, or the caller, has. A hold taken under a name before holds were
    kept by session id is found by the name given, the session's name now, and each name it once had."""
    identity = holder_of(given)
    key, name = identity.key, identity.name
    none: list[str] = []
    former = next((record["formerNames"] for record in live_sessions() if record["sessionId"] == key), none)
    candidates: list[str] = [key, given or name, name, *former]
    found = next((one for one in candidates if holder_path(directory, one).is_file()), None)
    if found is None:
        raise ValueError(f"{given or name} held nothing")
    return found


def aware_instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("instant needs an offset")
    return instant


def read_holder(path: Path) -> Holder:
    """Even an unreadable or malformed regular file remains an active hold."""
    fallback = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    try:
        content = path.read_text().strip()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError):
        return Holder(path.name, fallback, "unreadable holder file", UnknownReleaseEta(), path.name)
    try:
        raw = cast(object, json.loads(content))
        if isinstance(raw, dict):
            fields = cast(dict[str, object], raw)
            name = fields.get("holder")
            since = fields.get("since")
            purpose = fields.get("for")
            release = fields.get("release_eta")
            if isinstance(name, str) and isinstance(since, str) and isinstance(purpose, str) and isinstance(release, str):
                eta: ReleaseEta = UnknownReleaseEta() if release == "unknown" else KnownReleaseEta(aware_instant(release))
                now = next((record["name"] for record in live_sessions() if record["sessionId"] == path.name), name)
                keep: list[ProcessIdentity] = []
                values = fields.get("keep", [])
                if isinstance(values, list):
                    for value in cast(list[object], values):
                        if isinstance(value, dict):
                            entry = cast(dict[str, object], value)
                            pid, start = entry.get("pid"), entry.get("start_time")
                            if isinstance(pid, int) and pid > 0 and isinstance(start, str) and start.isdecimal():
                                keep.append(ProcessIdentity(pid, start))
                return Holder(now, aware_instant(since), purpose, eta, path.name, tuple(keep))
    except (ValueError, TypeError):
        pass
    match = INSTANT.search(content)
    if match is None:
        return Holder(path.name, fallback, content, UnknownReleaseEta(), path.name)
    try:
        since = aware_instant(match.group())
    except ValueError:
        since = fallback
    return Holder(path.name, since, content[match.end():].lstrip(" ,;:-").strip(), UnknownReleaseEta(), path.name)


def read_holders(directory: Path) -> HoldState:
    try:
        paths = [path for path in directory.iterdir() if path.is_file()]
    except FileNotFoundError:
        return NoHolders()
    found: list[Holder] = []
    for path in paths:
        try:
            found.append(read_holder(path))
        except FileNotFoundError:
            continue
    holders = tuple(sorted(found, key=lambda holder: (holder.since, holder.name)))
    return ActiveHolders(holders) if holders else NoHolders()


def has_holder_file(directory: Path) -> bool:
    try:
        return any(path.is_file() for path in directory.iterdir())
    except FileNotFoundError:
        return False


def live_holds(cycle: HoldCycle, directory: Path) -> tuple[Holder, ...]:
    holders = read_holders(directory)
    if isinstance(holders, NoHolders):
        return ()
    return holders_in_force(cycle, directory, holders.holders)


def holders_in_force(cycle: HoldCycle, directory: Path, holders: tuple[Holder, ...]) -> tuple[Holder, ...]:
    # Cycle fields record history. Only removal of a holder file ends a hold.
    _ = cycle
    return tuple(holder for holder in holders if holder_path(directory, holder.key).is_file())


def natedev_holds(directory: Path) -> NatedevHolds:
    holders = read_holders(directory)
    if isinstance(holders, NoHolders):
        return NatedevHolds(())
    return NatedevHolds(holders.holders)


def release_eta_text(release: ReleaseEta) -> str:
    if isinstance(release, UnknownReleaseEta):
        return "unknown"
    return release.at.astimezone(user_zone()).strftime("%H:%M %Z")


def holder_text(holder: Holder) -> str:
    return f"{holder.name} (for {holder.purpose}, release eta {release_eta_text(holder.release)})"


def resume_refusal(holders: tuple[Holder, ...]) -> str:
    return ("a hold is in force (" + "; ".join(holder_text(holder) for holder in holders)
            + "); --resume only continues a release already begun. Release that hold without --resume.")


def release_request(release_time: str | None, zone_name: str | None) -> ReleaseRequest:
    if release_time is None:
        if zone_name is not None:
            raise ValueError("--zone goes with --release-eta")
        return NoReleaseEta()
    if zone_name is None:
        raise ValueError("--zone goes with --release-eta")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", release_time):
        raise ValueError("release ETA must be HH:MM")
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown IANA zone: {zone_name}") from None
    hour, minute = map(int, release_time.split(":"))
    return ClockReleaseEta(ClockTime(hour, minute), zone)


class KeptRunRecord(TypedDict):
    pid: int
    start_time: str


HoldFileRecord = TypedDict("HoldFileRecord", {
    "holder": str, "since": str, "for": str, "release_eta": str, "keep": list[KeptRunRecord],
})


def write_hold(directory: Path, identity: HolderIdentity, purpose: str, request: ReleaseRequest, now: datetime,
               keep: Sequence[ProcessIdentity] = ()) -> str:
    """Keep a hold under its resolved key and display name."""
    if not purpose.strip() or "\n" in purpose:
        raise ValueError("hold purpose must be one line of text")
    if isinstance(request, NoReleaseEta):
        release_eta = "unknown"
    else:
        release = datetime.combine(now.astimezone(request.zone).date(), request.clock, request.zone)
        if release <= now:
            raise ValueError("release ETA has already passed today")
        release_eta = release.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    path = holder_path(directory, identity.key)
    previous = read_holder(path).keep if path.is_file() else ()
    record: HoldFileRecord = {
        "holder": identity.name, "since": now.isoformat(), "for": purpose.strip(), "release_eta": release_eta,
        "keep": [{"pid": item.pid, "start_time": item.start_time} for item in dict.fromkeys((*previous, *keep))],
    }
    temporary = directory.parent / f"{directory.name}.{path.name}.tmp"
    _ = temporary.write_text(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(temporary, path)
    return (f"natedev is held for {purpose.strip()}. verify.sh waits by itself; an app launch is refused until the release, "
            "so launch again after it. Tests that can run on the Mac keep running there. "
            "Every other test and build is stopped with the reason, including runs already started. "
            "Use --keep <pid> to keep a run and its children. Stopped sessions get a release notice; run again then. "
            "No release after 2 h: ask me.")


@dataclass(frozen=True)
class NatedevHoldEvent:
    kind: Literal["began", "ended"]
    holder: Holder
    at: datetime


pending_hold_events: ContextVar[tuple[NatedevHoldEvent, ...]] = ContextVar("pending_hold_events", default=())
coordination_depth: ContextVar[int] = ContextVar("coordination_depth", default=0)


@contextmanager
def record_after_unlock() -> Generator[None, None, None]:
    depth = coordination_depth.get()
    token = coordination_depth.set(depth + 1)
    try:
        yield
    finally:
        coordination_depth.reset(token)
        if depth == 0:
            events = pending_hold_events.get()
            _ = pending_hold_events.set(())
            for event in events:
                start_hold_recorder(event)


def start_hold_recorder(event: NatedevHoldEvent) -> None:
    until = event.holder.release.at.isoformat() if isinstance(event.holder.release, KnownReleaseEta) else ""
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / "buildlog" / "record.py"),
               "hold-event", event.kind, "natedev", event.holder.key, event.holder.name, event.holder.purpose,
               event.at.astimezone(timezone.utc).isoformat(), until]
    start_recorder(command)


def start_recorder(command: list[str]) -> None:
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return
    threading.Thread(target=process.wait, name="hold-event-recorder", daemon=True).start()


def record_hold_event(event: Literal["began", "ended"], holder: Holder, at: datetime) -> None:
    record = NatedevHoldEvent(event, holder, at)
    if coordination_depth.get():
        _ = pending_hold_events.set((*pending_hold_events.get(), record))
    else:
        start_hold_recorder(record)


def release_hold(directory: Path, holder: Holder) -> str:
    path = directory / holder.key
    now = datetime.now(timezone.utc)
    try:
        path.unlink()
    except FileNotFoundError:
        raise ValueError(f"{holder.name} held nothing") from None
    record_hold_event("ended", holder, now)
    state = read_holders(directory)
    if isinstance(state, NoHolders):
        return "released, builds may resume."
    return "released; still held by " + "; ".join(holder_text(holder) for holder in state.holders)


def release_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_RELEASE_DIR", str(Path.home() / ".local/state/build-hold-release")))


@contextmanager
def release_lock() -> Generator[None]:
    directory = release_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with record_after_unlock(), (directory / "release.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cycle_path() -> Path | None:
    current = release_directory() / "current"
    try:
        cycle_id = current.read_text().strip()
    except FileNotFoundError:
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", cycle_id):
        raise ValueError("invalid current hold cycle")
    return release_directory() / cycle_id / "cycle.json"


def read_cycle() -> HoldCycle | None:
    try:
        path = cycle_path()
    except (OSError, ValueError) as error:
        raise ReleaseRecordReadError(str(error)) from error
    return None if path is None else read_hold_cycle(path)


def read_hold_cycle(path: Path) -> HoldCycle:
    """Read current and retained cycles through the same validation boundary."""
    try:
        record = cast(object, json.loads(path.read_text()))
        if not isinstance(record, dict):
            raise ValueError("invalid hold cycle: expected object")
        cycle = cast(dict[str, object], record)
        for field in ("id", "opened_at", "release_started_at"):
            if not isinstance(cycle.get(field), str):
                raise ValueError(f"invalid {field}: expected string")
        holders = cycle.get("holders")
        if not isinstance(holders, dict):
            raise ValueError("invalid holders: expected object")
        for name, instants in cast(dict[object, object], holders).items():
            if not isinstance(name, str) or not isinstance(instants, dict):
                raise ValueError("invalid holder: expected name and object")
            fields = cast(dict[str, object], instants)
            if not isinstance(fields.get("since"), str) or not isinstance(fields.get("released_at"), str):
                raise ValueError(f"invalid holder {name}: expected since and released_at strings")
            _ = aware_instant(cast(str, fields["since"]))
        recipients = cycle.get("recipients")
        if not isinstance(recipients, dict):
            raise ValueError("invalid recipients: expected object")
        if any(not isinstance(name, str) or not isinstance(session_id, str)
               for session_id, name in cast(dict[object, object], recipients).items()):
            raise ValueError("invalid recipients: expected string names and session IDs")
        entries = cycle.get("entries")
        if not isinstance(entries, list):
            raise ValueError("invalid entries: expected list")
        normalized: list[ReleaseEntry] = []
        for index, value in enumerate(cast(list[object], entries)):
            if not isinstance(value, dict):
                raise ValueError(f"invalid entry {index}: expected object")
            entry = cast(dict[str, object], value)
            session_id, name = entry.get("session_id"), entry.get("name")
            if not isinstance(session_id, str) or not isinstance(name, str):
                raise ValueError(f"invalid entry {index}: expected session ID and name")
            old_state = entry.get("state")
            old_outcomes = {"DeliveryFailed": "DeliveryFailed", "RecipientGone": "RecipientGone",
                            "DeliveryQueued": "QueuedUnconfirmed", "ReleasedAwaitingAdmission": "Delivered",
                            "NoAdmissionAck": "Delivered", "WaitingForMemory": "Delivered",
                            "MemoryGateReturned": "Delivered"}
            delivery = entry.get("delivery", old_outcomes.get(str(old_state), "NotAttempted"))
            # Old cycles keep delivery by holder generation and include call and gate fields.
            if isinstance(delivery, dict):
                old_delivery = cast(dict[str, object], delivery)
                delivery = (old_delivery.get("outcome", "NotAttempted")
                            if old_delivery.get("generation") == cycle.get("holder_generation", 1)
                            else "NotAttempted")
            if delivery not in {"NotAttempted", "Delivered", "QueuedUnconfirmed", "RecipientGone", "DeliveryFailed"}:
                raise ValueError(f"invalid delivery for entry {index}")
            normalized.append({"session_id": session_id, "name": name, "delivery": cast(DeliveryOutcome, delivery)})
        cycle["entries"] = normalized
        return cast(HoldCycle, cast(object, cycle))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ReleaseRecordReadError(str(error)) from error


def set_aside_damaged_record(path: Path) -> DamagedRecordSetAside:
    """Preserve a damaged record under release.lock without replacing an earlier copy."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = path.with_name(f"{path.name}.damaged-{timestamp}")
    suffix = 2
    while destination.exists() or destination.is_symlink():
        destination = path.with_name(f"{path.name}.damaged-{timestamp}-{suffix}")
        suffix += 1
    _ = path.rename(destination)
    return DamagedRecordSetAside(destination)


def read_cycle_for_change() -> HoldCycle | NoCycle | DamagedRecordSetAside:
    """Read a cycle while release.lock is held, setting damage aside if found."""
    try:
        cycle = read_cycle()
        return NoCycle() if cycle is None else cycle
    except ReleaseRecordReadError as error:
        current = release_directory() / "current"
        try:
            path = cycle_path()
        except (OSError, ValueError):
            path = None
        damaged = path if path is not None and (path.is_symlink() or path.exists()) else current
        saved = set_aside_damaged_record(damaged)
        current.unlink(missing_ok=True)
        print(f"release record {damaged} could not be read ({error}); set aside as {saved.path}; this hold now releases every session at once", flush=True)
        return saved


def save_cycle(cycle: HoldCycle) -> None:
    path = release_directory() / cycle["id"] / "cycle.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps(cycle, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def read_release_intent(path: Path) -> ReleaseIntent | NoReleaseIntent:
    try:
        raw = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return NoReleaseIntent()
    if not isinstance(raw, dict):
        raise ValueError("invalid release intent: expected object")
    fields = cast(dict[str, object], raw)
    for field in ("holder_key", "holder_name", "holder_since", "holder_directory", "released_at"):
        if not isinstance(fields.get(field), str) or not fields[field]:
            raise ValueError(f"invalid release intent: {field}")
    _ = aware_instant(cast(str, fields["holder_since"]))
    _ = aware_instant(cast(str, fields["released_at"]))
    notices = fields.get("notices")
    if not isinstance(notices, dict):
        raise ValueError("invalid release intent: notices")
    notice_fields = cast(dict[str, object], notices)
    if notice_fields.get("kind") == "cycle":
        cycle_id, recipients = notice_fields.get("cycle_id"), notice_fields.get("recipients")
        if not isinstance(cycle_id, str) or not re.fullmatch(r"[0-9a-f]{32}", cycle_id):
            raise ValueError("invalid release intent: cycle ID")
        if not isinstance(recipients, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in cast(dict[object, object], recipients).items()
        ):
            raise ValueError("invalid release intent: recipients")
    elif notice_fields.get("kind") != "broadcast":
        raise ValueError("invalid release intent: notice kind")
    return cast(ReleaseIntent, cast(object, fields))


def save_release_intent(intent: ReleaseIntent) -> None:
    path = release_directory() / "pending-release.json"
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps(intent, ensure_ascii=False) + "\n")
    retain_release_record(path)
    os.replace(temporary, path)


def retain_release_record(path: Path) -> None:
    """Move a prior pending release out of the current slot without dropping its notices."""
    if path.is_file():
        destination = release_directory() / "releases" / uuid.uuid4().hex / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, destination)


def complete_release_record(record: RecordedRelease) -> None:
    """Keep the holder's latest release receipt after its notices are done."""
    receipt = holder_path(release_directory() / "receipts", record.intent["holder_key"]) / "completed-release.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    previous = read_release_intent(receipt)
    if (isinstance(previous, NoReleaseIntent)
            or aware_instant(previous["released_at"]) <= aware_instant(record.intent["released_at"])):
        os.replace(record.path, receipt)
    else:
        # An older pending release can finish after a newer release for this holder.
        record.path.unlink()
    if record.path.parent.parent == release_directory() / "releases":
        try:
            record.path.parent.rmdir()
        except OSError:
            # A directory that still contains another record must stay.
            pass


def pending_release_paths() -> list[Path]:
    return [release_directory() / "pending-release.json",
            *sorted((release_directory() / "releases").glob("*/pending-release.json"))]


def release_entries(cycle: HoldCycle, recipients: Mapping[str, str]) -> list[ReleaseEntry]:
    """Use the first recorded entry for each session, as the sender has always done."""
    first: dict[str, ReleaseEntry] = {}
    for entry in cycle["entries"]:
        _ = first.setdefault(entry["session_id"], entry)
    return [first[session_id] if session_id in first else new_entry(session_id, name)
            for session_id, name in recipients.items()]


def release_notices(intent: ReleaseIntent) -> CycleNoticesToSend | BroadcastReleaseNotice:
    """Read and select a release's unsent entries without changing its files or cycle."""
    notices = intent["notices"]
    if notices["kind"] == "broadcast":
        return notices
    path = release_directory() / notices["cycle_id"] / "cycle.json"
    try:
        cycle = read_hold_cycle(path)
        if cycle["id"] != notices["cycle_id"]:
            raise ValueError("release intent does not match its hold cycle")
    except (OSError, ValueError) as error:
        raise ReleaseNoticeReadError(path, str(error)) from error
    return CycleNoticesToSend(cycle, [entry for entry in release_entries(cycle, notices["recipients"])
                                      if entry["delivery"] in ("NotAttempted", "DeliveryFailed")])


def pending_release_status_lines() -> list[str]:
    """Name unfinished notices without locking, migrating or changing release state."""
    lines: list[str] = []
    for path in pending_release_paths():
        try:
            intent = read_release_intent(path)
            if isinstance(intent, NoReleaseIntent):
                continue
            pending = release_notices(intent)
            notices = intent["notices"]
            unsent = (", ".join(notices["recipients"][entry["session_id"]] for entry in pending.entries) or "none"
                      if isinstance(pending, CycleNoticesToSend) and notices["kind"] == "cycle"
                      else "every session (broadcast)")
        except (OSError, ValueError) as error:
            if isinstance(error, ReleaseNoticeReadError):
                path = error.path
            lines.append(f"release pending: {path} could not be read: {error}; /build:hold release sets it aside")
            continue
        released = aware_instant(intent["released_at"]).astimezone(user_zone())
        lines.append(f"release pending: {intent['holder_name']}, released {released:%H:%M %Z} - unsent: {unsent}; run /build:hold release to finish it")
    return lines


def recorded_releases() -> list[RecordedRelease]:
    return release_records_for_change().records


def release_records_for_change() -> ReleaseRecordsForChange:
    """Read releases under the lock, preserving damaged pending records and continuing."""
    # Migrate old receipts once; later releases read only one receipt per holder.
    for path in [release_directory() / "completed-release.json",
                 *sorted((release_directory() / "releases").glob("*/completed-release.json"))]:
        intent = read_release_intent(path)
        if not isinstance(intent, NoReleaseIntent):
            complete_release_record(RecordedRelease(path, intent, "completed"))
    records: list[RecordedRelease] = []
    set_aside: list[DamagedRecordSetAside] = []
    for state in ("pending", "completed"):
        if state == "pending":
            paths = pending_release_paths()
        else:
            paths = sorted((release_directory() / "receipts").glob("*/completed-release.json"))
        for path in paths:
            try:
                intent = read_release_intent(path)
            except (OSError, ValueError) as error:
                if state != "pending":
                    raise
                saved = set_aside_damaged_record(path)
                set_aside.append(saved)
                print(f"release pending: {path} could not be read: {error}; set aside as {saved.path}", flush=True)
                continue
            if not isinstance(intent, NoReleaseIntent):
                records.append(RecordedRelease(path, intent, state))
    return ReleaseRecordsForChange(records, set_aside)


def recorded_release_holder(directory: Path, given: str | None, *, resume: bool) -> HolderIdentity | NoReleaseIntent | DamagedRecordSetAside:
    """Resolve a stopped or completed release without accepting an unrelated holder."""
    with release_lock():
        # A receipt for a former name must never shadow a live session-id hold.
        if given is None and resume:
            if has_holder_file(directory):
                return NoReleaseIntent()
        else:
            try:
                _ = held_key(directory, given)
            except ValueError:
                pass
            else:
                return NoReleaseIntent()
        recovered = release_records_for_change()
        for record in recovered.records:
            intent = record.intent
            if intent["holder_directory"] != str(directory.resolve()):
                continue
            if given is None and resume:
                return HolderIdentity(intent["holder_key"], intent["holder_name"])
            identity = holder_of(given)
            no_former_names: list[str] = []
            former = next((record["formerNames"] for record in live_sessions()
                           if record["sessionId"] == identity.key), no_former_names)
            candidates = (identity.key, identity.name, given or identity.name, *former)
            if any(holder_path(directory, candidate).name == intent["holder_key"] for candidate in candidates):
                return HolderIdentity(intent["holder_key"], intent["holder_name"])
        if recovered.set_aside and not has_holder_file(directory):
            for record in recovered.records:
                if record.state == "pending" and record.intent["holder_directory"] == str(directory.resolve()):
                    return HolderIdentity(record.intent["holder_key"], record.intent["holder_name"])
            return recovered.set_aside[0]
    return NoReleaseIntent()


def finish_release_intent(intent: ReleaseIntent) -> str:
    """Drain all unfinished releases for this holder directory under release.lock."""
    directory = Path(intent["holder_directory"])
    state = read_holders(directory)
    if isinstance(state, ActiveHolders):
        raise ValueError(resume_refusal(state.holders))
    result = "released, builds may resume."
    failures: list[str] = []
    for record in recorded_releases():
        if record.state == "pending" and record.intent["holder_directory"] == intent["holder_directory"]:
            try:
                result += finish_recorded_notices(record)
            except Exception as error:
                # A failed record must not stop notices for another release.
                failures.append(f"{record.intent['holder_name']}: {error}")
    if failures:
        raise OSError(result + " Release notices remain pending: " + "; ".join(failures))
    return result


def finish_recorded_notices(record: RecordedRelease) -> str:
    intent = record.intent
    directory = Path(intent["holder_directory"])
    try:
        pending = release_notices(intent)
    except ReleaseNoticeReadError as error:
        # The intent still names its recipients. Preserve the damaged cycle and
        # rebuild only that release's delivery state; an uncertain send can repeat.
        # A stop after the rename leaves the intent, so a missing cycle also recovers.
        if error.path.exists() or error.path.is_symlink():
            saved = set_aside_damaged_record(error.path)
            print(f"release pending: {error.path} could not be read: {error}; set aside as {saved.path}", flush=True)
        notices = intent["notices"]
        assert notices["kind"] == "cycle"
        save_cycle({"id": notices["cycle_id"], "opened_at": intent["holder_since"],
                    "holders": {intent["holder_key"]: {"since": intent["holder_since"], "released_at": intent["released_at"]}},
                    "recipients": dict(notices["recipients"]), "entries": [], "release_started_at": intent["released_at"]})
        pending = release_notices(intent)
    if not isinstance(pending, CycleNoticesToSend):
        # The pending record is the broadcast entry. Keep it until the send returns.
        send_release_broadcast()
        result = " No hold cycle: broadcast this release to every session."
    else:
        cycle = pending.cycle
        notices = intent["notices"]
        assert notices["kind"] == "cycle"
        changed = False
        for session_id, name in notices["recipients"].items():
            if session_id not in cycle["recipients"]:
                cycle["recipients"][session_id] = name
                changed = True
        for key, instants in cycle["holders"].items():
            if (holder_path(directory, key).name == intent["holder_key"] and not instants["released_at"]
                    and aware_instant(instants["since"]) == aware_instant(intent["holder_since"])):
                instants["released_at"] = intent["released_at"]
                changed = True
        if not cycle["release_started_at"]:
            cycle["release_started_at"] = intent["released_at"]
            changed = True
        if changed:
            save_cycle(cycle)
        result = send_cycle_notices(cycle, pending.entries)
    complete_release_record(record)
    return result


def open_cycle(now: datetime) -> HoldCycle:
    cycle: HoldCycle = {"id": uuid.uuid4().hex, "opened_at": now.isoformat(), "holders": {}, "recipients": {}, "entries": [], "release_started_at": ""}
    save_cycle(cycle)
    temporary = release_directory() / "current.tmp"
    _ = temporary.write_text(cycle["id"] + "\n")
    os.replace(temporary, release_directory() / "current")
    return cycle


def start_hold(directory: Path, identity: HolderIdentity, purpose: str, request: ReleaseRequest, now: datetime,
               keep: Sequence[ProcessIdentity] = ()) -> str:
    with release_lock():
        at = datetime.now(timezone.utc)
        new_hold = not holder_path(directory, identity.key).is_file()
        if isinstance(read_holders(directory), NoHolders):
            cycle = open_cycle(now)
        else:
            cycle = read_cycle_for_change()
        notice = write_hold(directory, identity, purpose, request, now, keep)
        if new_hold:
            record_hold_event("began", read_holder(holder_path(directory, identity.key)), at)
        if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            if cycle["release_started_at"]:
                cycle["release_started_at"] = ""
                for entry in cycle["entries"]:
                    entry["delivery"] = "NotAttempted"
            cycle["holders"][identity.key] = {"since": now.isoformat(), "released_at": ""}
            save_cycle(cycle)
        return notice


def record_recipients(recipients: Sequence[tuple[str, str]]) -> None:
    if not has_holder_file(holder_directory()):
        return
    with release_lock():
        if isinstance(read_holders(holder_directory()), NoHolders):
            return
        cycle = read_cycle_for_change()
        if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            return
        for name, session_id in recipients:
            if session_id:
                cycle["recipients"][session_id] = name
                for entry in cycle["entries"]:
                    if entry["session_id"] == session_id:
                        entry["name"] = name
        save_cycle(cycle)


def register_wait(session_id: str, name: str = "") -> str:
    if not session_id:
        return "NoSessionId: hold remains active; this session cannot register"
    if not has_holder_file(holder_directory()):
        return "no build hold"
    with release_lock():
        if isinstance(read_holders(holder_directory()), NoHolders):
            return "no build hold"
        cycle = read_cycle()
        if cycle is None:
            return "no hold cycle: wait for the release broadcast"
        _ = cycle["recipients"].setdefault(session_id, name or session_id)
        if not any(entry["session_id"] == session_id for entry in cycle["entries"]):
            cycle["entries"].append(new_entry(session_id, cycle["recipients"][session_id]))
        save_cycle(cycle)
        return f"registered {session_id}; wait for release before building"


def new_entry(session_id: str, name: str) -> ReleaseEntry:
    return {"session_id": session_id, "name": name, "delivery": "NotAttempted"}


def socket_for(session_id: str) -> RecipientLive | RecipientGone | RecipientLookupFailed:
    script = Path(__file__).resolve().parent.parent / "message" / "sessions.py"
    reason = ""
    for attempt in range(LOOKUP_TRIES):
        if attempt:
            time.sleep(LOOKUP_RETRY_S)
        try:
            result = subprocess.run([sys.executable, str(script), "socket", f"session:{session_id}"], capture_output=True, text=True)
        except OSError as error:
            reason = str(error)
            continue
        # Exit 1 is no such live session. Any other failure is the lookup itself failing.
        if result.returncode in (0, 1):
            socket = result.stdout.strip()
            return RecipientLive(socket) if result.returncode == 0 and socket else RecipientGone()
        reason = result.stderr.strip() or f"sessions.py exited {result.returncode}"
    return RecipientLookupFailed(reason)


def send_release(entry: ReleaseEntry, socket: str) -> ReleaseDelivered | ReleaseQueued | ReleaseSendFailed:
    """Tell a recipient to start its builds; the memory gate orders them."""
    script = Path(__file__).resolve().parent.parent / "message" / "send.py"
    notice = f"{RELEASE_NOTICE} Session {entry['session_id']}."
    try:
        result = subprocess.run([sys.executable, str(script), "--to", f"uds:{socket}", "--text", notice], capture_output=True, text=True)
    except OSError as error:
        return ReleaseSendFailed(str(error))
    if result.returncode == 0:
        return ReleaseDelivered()
    if result.returncode == 1:
        return ReleaseQueued()
    return ReleaseSendFailed(f"send.py: {failure_detail(result)}")


def send_release_broadcast() -> None:
    script = Path(__file__).resolve().parent.parent / "production" / "broadcast.py"
    result = subprocess.run([sys.executable, str(script), "--from", "build-hold", "--all", RELEASE_NOTICE],
                            capture_output=True, text=True)
    if result.returncode:
        raise OSError(f"release broadcast: {failure_detail(result)}")


def entry_text(entry: ReleaseEntry) -> str:
    return f"{entry['name']} [{entry['session_id']}]: {entry['delivery']}"


def cycle_status_lines(cycle: HoldCycle) -> list[str]:
    return [entry_text(entry) for entry in cycle["entries"]]


def not_reached_detail(cycle: HoldCycle) -> str:
    outcomes = {"RecipientGone": "session gone", "DeliveryFailed": "send failed",
                "QueuedUnconfirmed": "queued, delivery unconfirmed"}
    missing = [f"{entry['name']} ({outcomes[entry['delivery']]})" for entry in cycle["entries"]
               if entry["session_id"] in cycle["recipients"] and entry["delivery"] in outcomes]
    return " Not reached, tell them yourself: " + ", ".join(missing) if missing else ""


def notify_release(cycle: HoldCycle) -> str:
    with release_lock():
        current = read_cycle()
        if (current is None or current["id"] != cycle["id"]
                or not current["release_started_at"]
                or current["release_started_at"] != cycle["release_started_at"]):
            return ""
        return notify_released_cycle(current)


def notify_released_cycle(cycle: HoldCycle) -> str:
    return send_cycle_notices(cycle, [entry for entry in release_entries(cycle, cycle["recipients"])
                                     if entry["delivery"] == "NotAttempted"])


def send_cycle_notices(cycle: HoldCycle, entries: Sequence[ReleaseEntry]) -> str:
    # The release lock prevents a same-session re-hold between this check and a send.
    if has_holder_file(holder_directory()):
        return ""
    for entry in entries:
        if has_holder_file(holder_directory()):
            break
        if entry not in cycle["entries"]:
            cycle["entries"].append(entry)
        entry["delivery"] = "NotAttempted"
        # Save the pending entry before sending, and the outcome only after return.
        # A stop at any point between these saves may cause a harmless second notice.
        save_cycle(cycle)
        recipient = socket_for(entry["session_id"])
        if isinstance(recipient, RecipientGone):
            entry["delivery"] = "RecipientGone"
        elif isinstance(recipient, RecipientLive):
            result = send_release(entry, recipient.socket)
            if isinstance(result, ReleaseDelivered):
                entry["delivery"] = "Delivered"
            elif isinstance(result, ReleaseQueued):
                entry["delivery"] = "QueuedUnconfirmed"
            else:
                entry["delivery"] = "DeliveryFailed"
        else:
            entry["delivery"] = "DeliveryFailed"
        save_cycle(cycle)
    return not_reached_detail(cycle)


def release_cycle(directory: Path, name: str, *, resume: bool = False,
                  clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    with release_lock():
        path = holder_path(directory, name)
        if not path.is_file():
            for record in recorded_releases():
                intent = record.intent
                if (intent["holder_directory"] != str(directory.resolve())
                        or intent["holder_key"] != path.name):
                    continue
                if record.state == "completed":
                    if not has_holder_file(directory):
                        detail = finish_release_intent(intent).removeprefix("released, builds may resume.")
                        return "hold is already released." + detail
                    return "hold is already released."
                return finish_release_intent(intent)
            raise ValueError(f"{name} held nothing")
        cycle = read_cycle_for_change()
        if resume and not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            holds = live_holds(cycle, directory)
            if holds:
                raise ValueError(resume_refusal(holds))
        holder = read_holder(path)
        state = read_holders(directory)
        intent: ReleaseIntent | NoReleaseIntent = NoReleaseIntent()
        if isinstance(state, ActiveHolders) and len(state.holders) == 1:
            notices: CycleReleaseNotices | BroadcastReleaseNotice = {"kind": "broadcast"}
            if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
                notices = {"kind": "cycle", "cycle_id": cycle["id"], "recipients": dict(cycle["recipients"])}
            intent = {"holder_key": holder.key, "holder_name": holder.name,
                      "holder_since": holder.since.isoformat(), "holder_directory": str(directory.resolve()),
                      "released_at": clock().isoformat(), "notices": notices}
            save_release_intent(intent)
        result = release_hold(directory, holder)
        if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            for key, instants in cycle["holders"].items():
                if holder_path(directory, key).name == holder.key and aware_instant(instants["since"]) == holder.since:
                    instants["released_at"] = clock().isoformat()
            save_cycle(cycle)
        if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            if result != "released, builds may resume.":
                return result
            if isinstance(intent, NoReleaseIntent):
                raise ValueError("last holder has no release intent")
            return finish_release_intent(intent)
        remaining = live_holds(cycle, directory)
        if remaining:
            return "released; still held by " + "; ".join(holder_text(other) for other in remaining)
        cycle["release_started_at"] = clock().isoformat()
        save_cycle(cycle)
        if isinstance(intent, NoReleaseIntent):
            raise ValueError("last holder has no release intent")
        return finish_release_intent(intent)


def quiet_verdict(processes: Sequence[Process], user: str) -> Quiet | Busy:
    builds = [process for process in processes if process.command in BUILD_COMMANDS]
    reasons: list[str] = []
    for owner in sorted({process.user for process in builds}, key=lambda owner: (owner != user, owner)):
        counts = Counter(process.command for process in builds if process.user == owner)
        label = "your builds still running" if owner == user else owner
        reasons.append(label + ": " + ", ".join(
            f"{count} {command}" for command, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))))
    if reasons:
        return Busy(tuple(reasons))
    return Quiet(tuple(processes))


def read_processes() -> list[Process]:
    output = subprocess.check_output(["ps", "-eo", "pid=,user:32=,comm="], text=True)
    processes: list[Process] = []
    for line in output.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3:
            pid, user, command = parts
            try:
                processes.append(Process(int(pid), user, Path(command).name))
            except ValueError:
                continue
    return processes


def read_process_cpu_times(processes: Sequence[Process], proc_root: Path) -> dict[int, ProcessCpuTime]:
    readings: dict[int, ProcessCpuTime] = {}
    for process in processes:
        try:
            # comm can contain spaces and closing parentheses; the remaining fields start at 3.
            fields = (proc_root / str(process.pid) / "stat").read_text().rsplit(")", 1)[1].split()
            readings[process.pid] = ProcessCpuTime(int(fields[11]) + int(fields[12]), int(fields[19]))
        except (OSError, ValueError, IndexError):
            continue
    return readings


def heaviest_processes(
    processes: Sequence[Process],
    *,
    proc_root: Path = PROC_ROOT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[CurrentProcessCpu, ...]:
    if not processes:
        return ()
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    started = clock()
    before = read_process_cpu_times(processes, proc_root)
    sleep(1)
    after = read_process_cpu_times(processes, proc_root)
    elapsed = clock() - started
    if elapsed <= 0:
        return ()
    usage: list[CurrentProcessCpu] = []
    for process in processes:
        if process.pid not in before or process.pid not in after:
            continue
        first, last = before[process.pid], after[process.pid]
        if first.started_at_ticks != last.started_at_ticks or last.ticks <= first.ticks:
            continue
        percent = 100 * (last.ticks - first.ticks) / (elapsed * ticks_per_second)
        usage.append(CurrentProcessCpu(process, percent))
    return tuple(sorted(usage, key=lambda reading: reading.cpu_percent, reverse=True)[:5])


def wait_for_quiet(
    max_wait: float = 600,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    processes: Callable[[], Sequence[Process]] = read_processes,
    user: str | None = None,
) -> Quiet | Busy:
    if max_wait < 0:
        raise ValueError("max wait must be non-negative")
    current_user = user or os.environ.get("USER", "")
    deadline = clock() + max_wait
    while True:
        result = quiet_verdict(processes(), current_user)
        if isinstance(result, Quiet) or clock() >= deadline:
            return result
        sleep(min(10, max(0, deadline - clock())))


def main(arguments: list[str]) -> int:
    override_guard.refuse_overrides(Path(__file__), "build_hold/build_hold.py")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    hold = commands.add_parser("hold")
    _ = hold.add_argument("--holder")
    _ = hold.add_argument("--for", dest="purpose", required=True)
    _ = hold.add_argument("--release-eta")
    _ = hold.add_argument("--zone")
    _ = hold.add_argument("--target", type=parse_hold_target, default="natedev")
    _ = hold.add_argument("--proc-root", type=Path, default=PROC_ROOT)
    _ = hold.add_argument("--keep", type=int, action="append", default=[])
    quiet = commands.add_parser("quiet")
    _ = quiet.add_argument("--max-wait", type=float, default=600)
    release = commands.add_parser("release")
    _ = release.add_argument("--holder")
    _ = release.add_argument("--resume", action="store_true")
    _ = release.add_argument("--target", type=parse_hold_target, default="natedev")
    _ = commands.add_parser("wait")
    recipient = commands.add_parser("record-recipient")
    _ = recipient.add_argument("--session-id", required=True)
    _ = recipient.add_argument("--name", required=True)
    for action in ("status", "audit"):
        audit = commands.add_parser(action)
        _ = audit.add_argument("--proc-root", type=Path, default=PROC_ROOT)
    options = parser.parse_args(arguments)
    try:
        action = cast(str, options.action)
        if action in {"hold", "release"}:
            target = cast(HoldTarget, options.target)
            if isinstance(target, UnknownHoldTarget):
                print(f"unknown target {target.name}; configured targets: {', '.join(target.configured)}")
                return 2
            if isinstance(target, UncheckedHoldTarget):
                print(f"unknown target {target.name}; the target config cannot be read: {target.reason}")
                return 2
            if isinstance(target, MacHoldTarget):
                refused: list[str] = []
                if action == "hold":
                    if cast(list[int], options.keep):
                        refused.append("--keep")
                    if cast(object, options.release_eta) is not None:
                        refused.append("--release-eta")
                    if cast(object, options.zone) is not None:
                        refused.append("--zone")
                elif cast(bool, options.resume):
                    refused.append("--resume")
                if refused:
                    print(f"{refused[0]} is not supported with --target mac")
                    return 2
                identity = holder_of(cast(str | None, options.holder))
                if action == "hold":
                    return print_mac_hold_result(run_mac_hold(
                        ["block", "--holder", identity.name, "--for", cast(str, options.purpose)]))
                return print_mac_hold_result(release_mac_hold(identity, cast(str | None, options.holder) or identity.name))
        if action == "hold":
            request = release_request(cast(str | None, options.release_eta), cast(str | None, options.zone))
            identity = holder_of(cast(str | None, options.holder))
            keep: list[ProcessIdentity] = []
            proc_root = cast(Path, options.proc_root)
            for pid in cast(list[int], options.keep):
                start = process_start_time(pid, proc_root)
                if not start:
                    raise ValueError(f"--keep {pid}: no live process with a start time")
                keep.append(ProcessIdentity(pid, start))
            print(start_hold(holder_directory(), identity, cast(str, options.purpose), request,
                             datetime.now(timezone.utc), keep))
            hold_enforcer().start()
            print_hold_audit(holder_directory(), cast(Path, options.proc_root), beginning=True)
        elif action == "quiet":
            verdict = wait_for_quiet(cast(float, options.max_wait))
            if isinstance(verdict, Busy):
                print("; ".join(verdict.reasons))
                return 1
            heaviest = heaviest_processes(verdict.processes)
            running = ", ".join(f"{reading.process.user} {reading.process.command} {reading.cpu_percent:g}%" for reading in heaviest)
            print(f"builds are quiet: no build runs. 1-minute load {os.getloadavg()[0]:g} is other work: {running or 'none visible'}")
        elif action == "release":
            given = cast(str | None, options.holder)
            resume_requested = cast(bool, options.resume)
            name: str | None = None
            recorded = recorded_release_holder(holder_directory(), given, resume=resume_requested)
            if isinstance(recorded, DamagedRecordSetAside):
                print("released, builds may resume.")
                return 0
            if isinstance(recorded, HolderIdentity) and not holder_path(holder_directory(), recorded.key).is_file():
                name = recorded.key
            if name is None and given is None and resume_requested:
                with release_lock():
                    cycle = read_cycle_for_change()
                    if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
                        name = next((key for key, instants in cycle["holders"].items()
                                     if instants["released_at"] and holder_path(holder_directory(), key).is_file()
                                     and aware_instant(instants["since"]) == read_holder(holder_path(holder_directory(), key)).since), None)
                        if name is None:
                            holds = live_holds(cycle, holder_directory())
                            if holds:
                                raise ValueError(resume_refusal(holds))
                    elif isinstance(cycle, DamagedRecordSetAside):
                        state = read_holders(holder_directory())
                        if isinstance(state, ActiveHolders) and len(state.holders) == 1:
                            name = state.holders[0].key
            if name is None and given is None and resume_requested:
                raise ValueError("release needs --holder, or --resume for an active release")
            if name is None:
                name = held_key(holder_directory(), given)
            print(release_cycle(holder_directory(), name, resume=resume_requested))
        elif action == "wait":
            # Use the local gate's hold and caller check; recording a notice recipient permits no build.
            import importlib
            check_local_hold = cast(Callable[[], int], getattr(
                importlib.import_module("build_targets"), "command_check_local"))
            return check_local_hold()
        elif action == "record-recipient":
            record_recipients([(cast(str, options.name), cast(str, options.session_id))])
        elif action == "audit":
            print("\n".join(audit_lines(audit_hold(holder_directory(), cast(Path, options.proc_root)))))
        else:
            state = read_holders(holder_directory())
            if isinstance(state, NoHolders):
                print("no build hold")
            else:
                for holder in state.holders:
                    print(f"{holder.name} since {holder.since.astimezone(user_zone()):%H:%M %Z}, for {holder.purpose} - release eta: {release_eta_text(holder.release)}")
                    for run in holder.keep:
                        print(f"  kept by {holder.name}: pid {run.pid}, start {run.start_time}")
                try:
                    cycle = read_cycle()
                except ReleaseRecordReadError as error:
                    print(release_record_error_line(error))
                else:
                    if cycle is not None:
                        for line in cycle_status_lines(cycle):
                            print(line)
                print_hold_audit(holder_directory(), cast(Path, options.proc_root))
            for line in pending_release_status_lines():
                print(line)
    except (ValueError, OSError) as error:
        print(f"build_hold: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # The enforcer must use this CLI's domain types, not a second module instance.
    _ = sys.modules.setdefault("build_hold", sys.modules[__name__])
    sys.exit(main(sys.argv[1:]))
