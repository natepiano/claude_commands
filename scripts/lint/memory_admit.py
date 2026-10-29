#!/usr/bin/env python3
"""Expected process-memory peaks and atomic build admission."""

from __future__ import annotations

import fcntl
import importlib
import json
import math
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, NoReturn, NotRequired, Protocol, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "buildlog"))


class GitFacts(TypedDict):
    repo_path: str | None
    worktree: str | None
    branch: str | None
    sha: str | None


class StepParser(Protocol):
    def step_name(self, argv: list[str]) -> str: ...


class GitIdentityReader(Protocol):
    def git_directory(self, argv: list[str], cwd: str) -> str: ...
    def git_facts(self, directory: str) -> GitFacts: ...
    def queue_bump(self, actor: str, session: str, step: str, folder: str,
                   old_position: int, at: float) -> None: ...


class BuildLogStore(Protocol):
    INDEX_NAME: str

    def root(self) -> Path: ...
    def host_name(self) -> str: ...
    def utc_iso(self, epoch: float) -> str: ...
    def append_line(self, path: Path, record: object) -> None: ...
    def host_file(self, host: str, epoch: float) -> Path: ...


parse: StepParser = cast(StepParser, cast(object, importlib.import_module("parse")))
record: GitIdentityReader = cast(GitIdentityReader, cast(object, importlib.import_module("record")))
store: BuildLogStore = cast(BuildLogStore, cast(object, importlib.import_module("store")))

GIB = 1 << 30
FALLBACK = 12 * GIB
# On natedev, isolated hana nextest scopes had p90 anon / memory.peak 61%;
# clippy had 65%, so 0.65 covers the measured p90 process-memory share.
ANON_SHARE = 0.65
HISTORY_DAYS = 14
# Build anon at 16–20 GiB averaged 10.4% full PSI; at 20–24 GiB it averaged 16.4%.
BUILD_BUDGET_BYTES = 20 * GIB
RAMP_SECONDS = 120
PROC = Path("/proc")
# A step held this long gets room first when it can start.
HEAD_SECONDS = 900
QUEUE_LOCK_TIMEOUT_SECONDS = 10.0


class Reservation(TypedDict):
    pid: int
    start_time: str
    need: int
    repo: str
    step: str
    worktree: str
    admitted_at: str
    sidecar: str
    test_threads: int | None
    call_id: str | None


@dataclass(frozen=True)
class ExpectedPeak:
    bytes: int
    source: Literal["measured", "buildlog", "pooled", "fallback"]
    count: int


@dataclass(frozen=True)
class PromisedMemory:
    bytes: int


@dataclass(frozen=True)
class MeasuredMemory:
    bytes: int
    # Only a step inside its ramp window reserves growth.
    ramp_promise: int = 0


@dataclass(frozen=True)
class GoneMemory:
    pass


StepMemory = PromisedMemory | MeasuredMemory | GoneMemory


@dataclass(frozen=True)
class NoPressureHold:
    pass


@dataclass(frozen=True)
class MemoryPressure:
    full_avg10: float


Pressure = NoPressureHold | MemoryPressure
NO_PRESSURE_HOLD = NoPressureHold()


class Waiter(TypedDict):
    pid: int
    start_time: str
    need: int
    since: float
    label: NotRequired[str]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
    step: NotRequired[str]
    folder: NotRequired[str]
    bump_order: NotRequired[int]
    holds_younger: NotRequired[bool]


@dataclass(frozen=True)
class HeadPriority:
    waiter: Waiter


@dataclass(frozen=True)
class NoHeadPriority:
    pass


@dataclass(frozen=True)
class AdmissionDecision:
    state: Literal["admit", "hold"]
    threshold: int
    promised: int
    reserve: int
    measured: int
    pressure: Pressure = NO_PRESSURE_HOLD
    charged: int = 0
    budget: int = BUILD_BUDGET_BYTES


@dataclass(frozen=True)
class NeedsBuildBudget:
    need: int
    charged: int
    budget: int


@dataclass(frozen=True)
class NeedsMemory:
    need: int = 0
    free: int = 0


@dataclass(frozen=True)
class BehindHead:
    head_name: str
    head_since: float


@dataclass(frozen=True)
class HeadWaitingForDrain:
    running_count: int


@dataclass(frozen=True)
class ReadyForAdmission:
    pass


@dataclass(frozen=True)
class ReadingUnavailable:
    pass


WaitReason = NeedsBuildBudget | NeedsMemory | BehindHead | HeadWaitingForDrain | ReadyForAdmission | ReadingUnavailable | MemoryPressure


@dataclass(frozen=True)
class WaitingStep:
    pid: int
    session_id: str
    session_name: str
    step: str
    folder: str
    since: float
    need: int
    reason: WaitReason


@dataclass(frozen=True)
class RunningStep:
    session_id: str
    session_name: str
    step: str
    folder: str
    promise: int
    measured: StepMemory


@dataclass(frozen=True)
class QueueView:
    waiting: tuple[WaitingStep, ...]
    running: tuple[RunningStep, ...]
    available: int
    promised: int
    reserve: int
    now: float


@dataclass(frozen=True)
class ProcessIdentity:
    parent: int
    start: str


@dataclass(frozen=True)
class Reserved:
    path: Path


@dataclass(frozen=True)
class NotReserved:
    reason: Literal["held", "launch", "untracked"]


@dataclass(frozen=True)
class StepProfile:
    repo: str
    step: str
    worktree: str
    peak: ExpectedPeak


@dataclass(frozen=True)
class CheckResult:
    decision: AdmissionDecision
    peak: ExpectedPeak
    live: list[Reservation]
    reservation: Reserved | NotReserved
    repo: str
    step: str
    available: int
    profile: StepProfile
    head_need: int = 0
    head_label: str = ""


def build_budget_bytes() -> int:
    try:
        value = float(os.environ["BUILDLOG_BUILD_BUDGET_GIB"]) * GIB
        if math.isfinite(value) and value > 0:
            return max(1, int(value))
    except (KeyError, ValueError):
        pass
    return BUILD_BUDGET_BYTES


def decide(available: int, total: int | None, need: int, running: list[StepMemory],
           head_need: int = 0, pressure: Pressure = NO_PRESSURE_HOLD) -> AdmissionDecision:
    """Apply the build budget, free-memory floor and pressure limit together."""
    promised = sum(growth_headroom(row) for row in running)
    measured = sum(row.bytes for row in running if isinstance(row, MeasuredMemory))
    charged = sum(build_charge(row) for row in running)
    budget = build_budget_bytes()
    reserve = total * 5 // 100 if total is not None else 0
    threshold = need + promised + reserve + head_need
    fits_budget = charged + need + head_need <= budget or (not running and not head_need)
    admit = fits_budget and available >= threshold and isinstance(pressure, NoPressureHold)
    return AdmissionDecision("admit" if admit else "hold", threshold, promised, reserve, measured,
                             pressure, charged, budget)


def build_charge(memory: StepMemory) -> int:
    if isinstance(memory, PromisedMemory):
        return memory.bytes
    if isinstance(memory, MeasuredMemory):
        return max(memory.ramp_promise, memory.bytes)
    return 0


def growth_headroom(memory: StepMemory) -> int:
    if isinstance(memory, PromisedMemory):
        return memory.bytes
    if isinstance(memory, MeasuredMemory):
        return max(0, memory.ramp_promise - memory.bytes)
    return 0


def percentile(values: list[int]) -> int:
    values.sort()
    return values[(90 * len(values) + 99) // 100 - 1]


def test_threads(argv: list[str]) -> int | None:
    """Return nextest's explicitly requested test width, when it is numeric."""
    for index, argument in enumerate(argv):
        value: str | None = None
        if argument in ("--test-threads", "-j"):
            if index + 1 < len(argv):
                value = argv[index + 1]
        elif argument.startswith("--test-threads="):
            value = argument.removeprefix("--test-threads=")
        elif argument.startswith("-j") and len(argument) > 2:
            value = argument[2:]
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def expected_peak(repo: str, step: str, host: str, now: datetime, root: Path,
                  test_threads: int | None = None) -> ExpectedPeak:
    """The p90 process-memory peak of a step over HISTORY_DAYS.

    The gate pools every test width: one width's history is thin and rebuild-heavy, and the
    claim of a width comes only after admission. `test_threads` narrows the measured history to
    one width, for width_trial.py's report.
    """
    cutoff = store.utc_iso((now - timedelta(days=HISTORY_DAYS)).timestamp())
    before = store.utc_iso(now.timestamp())
    measured: list[int] = []
    # Every repo's runs of this step on this host: a checkout with fewer than 5 runs of its own (a gate
    # copy, a clone) uses their p90, not FALLBACK. Doc steps held 2157 of 4702 wait minutes in the week
    # to 2026-10-28 on that fallback; the measured doc p90 over all repos was 4.15 GiB.
    pooled: list[int] = []
    try:
        with (root / "admission/anon_peaks.jsonl").open() as lines:
            for line in lines:
                try:
                    item = cast(dict[str, object], json.loads(line))
                    if ((item.get("host"), item.get("step")) == (host, step)
                            and (test_threads is None or item.get("test_threads") == test_threads)
                            and cutoff <= str(item.get("ended_at", "")) < before):
                        peak = item.get("anon_peak_bytes")
                        if isinstance(peak, int) and peak > 0:
                            pooled.append(peak)
                            if item.get("repo") == repo:
                                measured.append(peak)
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    if len(measured) >= 5:
        return ExpectedPeak(percentile(measured), "measured", len(measured))
    peaks: list[int] = []
    index = root / store.INDEX_NAME
    try:
        connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
        try:
            rows = cast(list[tuple[int]], connection.execute(
                "SELECT peak_mem_bytes FROM steps WHERE host = ? AND repo = ? AND step = ? "
                + "AND started_at >= ? AND started_at < ? AND peak_mem_bytes > 0",
                (host, repo, step, cutoff, before),
            ).fetchall())
            peaks = [int(value) for (value,) in rows]
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        pass
    if len(peaks) >= 5:
        return ExpectedPeak(int(percentile(peaks) * ANON_SHARE), "buildlog", len(peaks))
    if len(pooled) >= 5:
        return ExpectedPeak(percentile(pooled), "pooled", len(pooled))
    return ExpectedPeak(FALLBACK, "fallback", 0)


def process_start_time(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        fields = stat.rsplit(") ", 1)[1].split()
        return fields[19] if fields[0] not in ("Z", "X") else None
    except (OSError, IndexError):
        return None


def process_table(proc: Path = PROC) -> dict[int, ProcessIdentity]:
    processes: dict[int, ProcessIdentity] = {}
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            if fields[0] not in ("Z", "X"):
                processes[int(entry.name)] = ProcessIdentity(int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    return processes


def tree_memory(pid: int, start: str, processes: dict[int, ProcessIdentity],
                proc: Path = PROC) -> MeasuredMemory | GoneMemory:
    """Sum resident and swapped bytes from one process table reading."""
    identity = processes.get(pid)
    if identity is None or identity.start != start:
        return GoneMemory()
    children: dict[int, list[int]] = {}
    for child, identity in processes.items():
        children.setdefault(identity.parent, []).append(child)
    pending, seen, used = [pid], set[int](), 0
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        pending.extend(children.get(child, []))
        try:
            for line in (proc / str(child) / "status").read_text().splitlines():
                parts = line.split()
                if parts and parts[0] in ("VmRSS:", "VmSwap:"):
                    used += int(parts[1]) * 1024
        except (OSError, ValueError, IndexError):
            continue
    return MeasuredMemory(used)


def step_memory(row: Reservation, now: datetime, processes: dict[int, ProcessIdentity],
                proc: Path = PROC) -> StepMemory:
    identity = processes.get(row["pid"])
    if identity is None or identity.start != row["start_time"]:
        return GoneMemory()
    reading = tree_memory(row["pid"], row["start_time"], processes, proc)
    if isinstance(reading, MeasuredMemory) and now.timestamp() - _epoch(row["admitted_at"]) < RAMP_SECONDS:
        if reading.bytes == 0:
            return PromisedMemory(row["need"])
        return replace(reading, ramp_promise=row["need"])
    return reading


def read_meminfo(path: Path) -> tuple[int, int | None]:
    available: int | None = None
    total: int | None = None
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "kB" and parts[1].isdecimal():
            if parts[0] == "MemAvailable:":
                available = int(parts[1]) * 1024
            elif parts[0] == "MemTotal:":
                total = int(parts[1]) * 1024
    if available is None:
        raise ValueError(f"MemAvailable missing from {path}")
    return available, total


def read_pressure() -> Pressure:
    """An unreadable pressure file does not block; the memory rule still applies."""
    full = 0.0
    try:
        path = Path(os.environ.get("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory"))
        for line in path.read_text().splitlines():
            parts = line.split()
            if parts and parts[0] == "full":
                full = next(float(part.removeprefix("avg10=")) for part in parts[1:] if part.startswith("avg10="))
    except (OSError, ValueError, StopIteration):
        pass
    return MemoryPressure(full) if full > 10.0 else NO_PRESSURE_HOLD


def pressure_text(pressure: MemoryPressure) -> str:
    return f"memory pressure: full avg10 {pressure.full_avg10:.1f}"


def admission_dir(root: Path) -> Path:
    directory = root / "admission"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def live_reservations(directory: Path) -> list[tuple[Path, Reservation]]:
    live: list[tuple[Path, Reservation]] = []
    for path in directory.glob("*.reservation"):
        try:
            row = cast(Reservation, json.loads(path.read_text()))
            if process_start_time(row["pid"]) == row["start_time"]:
                live.append((path, row))
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        path.unlink(missing_ok=True)
        path.with_suffix(".max").unlink(missing_ok=True)
    return live


def live_waiters(directory: Path) -> list[tuple[Path, Waiter]]:
    live: list[tuple[Path, Waiter]] = []
    for path in directory.glob("*.waiting"):
        try:
            row = cast(Waiter, json.loads(path.read_text()))
            if process_start_time(row["pid"]) == row["start_time"]:
                live.append((path, row))
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        path.unlink(missing_ok=True)
    return live


def waiter_label(row: Waiter) -> str:
    label = row.get("label", "")
    if label:
        return label
    # Old callers wrote only a pid. Read their label without changing the record.
    process = Path("/proc") / str(row["pid"])
    try:
        folder = (process / "cwd").readlink().name
        argv = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
        caller = "verify" if any(item.endswith("/verify.sh") for item in argv) else Path(argv[0]).name
        return f"{folder} ({caller} pid {row['pid']})"
    except (OSError, IndexError):
        return f"caller pid {row['pid']}"


def waiter_order(row: Waiter) -> tuple[int, float, int]:
    return row.get("bump_order", 0), row["since"], row["pid"]


def head_priority(waiters: list[Waiter], available: int, total: int | None,
                  running: list[StepMemory], now: float) -> HeadPriority | NoHeadPriority:
    candidates = sorted((row for row in waiters if row.get("holds_younger", False)
                         or row.get("bump_order", 0) < 0 or now - row["since"] >= HEAD_SECONDS),
                        key=waiter_order)
    if candidates:
        head = candidates[0]
        decision = decide(available, total, head["need"], running)
        if (head.get("holds_younger", False) or decision.state == "admit"
                or available + decision.measured >= head["need"] + decision.reserve):
            return HeadPriority(head)
    return NoHeadPriority()


def queue_decision(available: int, total: int | None, row: Waiter,
                   running: list[StepMemory], head: HeadPriority | NoHeadPriority,
                   pressure: Pressure) -> AdmissionDecision:
    younger = isinstance(head, HeadPriority) and waiter_order(head.waiter) < waiter_order(row)
    head_need = head.waiter["need"] if younger and isinstance(head, HeadPriority) else 0
    decision = decide(available, total, row["need"], running, head_need, pressure)
    if younger:
        return replace(decision, state="hold")
    if isinstance(head, HeadPriority) and waiter_order(head.waiter) == waiter_order(row) and not running:
        return replace(decision, state="admit" if isinstance(pressure, NoPressureHold) else "hold")
    return decision


def git_identity(argv: list[str], cwd: str) -> tuple[str, str]:
    if not argv:
        return "", ""
    facts = record.git_facts(record.git_directory(argv, cwd))
    repo_path = facts["repo_path"]
    return (Path(repo_path).name if repo_path else "", Path(facts["worktree"] or cwd).name)


def check(meminfo: Path, pid: int, sidecar: str, argv: list[str], since: float = 0.0,
          root: Path | None = None, now: datetime | None = None,
          profile: StepProfile | None = None) -> CheckResult:
    """Admit or hold one step. A held step is a waiter from `since` (epoch seconds; 0 means now), so a
    step that gave its reservation back for the cargo token keeps its place."""
    root = root if root is not None else store.root()
    now = now if now is not None else datetime.now(UTC)
    width = test_threads(argv)
    if profile is None:
        repo, worktree = git_identity(argv, os.getcwd())
        step = parse.step_name(argv) if argv else "build"
        host = store.host_name()
        peak = expected_peak(repo, step, host, now, root) if repo else ExpectedPeak(FALLBACK, "fallback", 0)
        profile = StepProfile(repo, step, worktree, peak)
    repo, step, worktree, peak = profile.repo, profile.step, profile.worktree, profile.peak
    if not argv and not (root / "admission").exists():
        available, total = read_meminfo(meminfo)
        pressure = read_pressure()
        return CheckResult(decide(available, total, peak.bytes, [], pressure=pressure), peak, [], NotReserved("launch"), repo, step, available, profile)
    directory = admission_dir(root)
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
        processes = process_table(proc)
        live: list[tuple[Path, Reservation]] = []
        running: list[StepMemory] = []
        for path, row in live_reservations(directory):
            memory = step_memory(row, now, processes, proc)
            if isinstance(memory, GoneMemory):
                path.unlink(missing_ok=True)
                path.with_suffix(".max").unlink(missing_ok=True)
                continue
            live.append((path, row))
            running.append(memory)
        start = process_start_time(pid)
        mine = directory / f"{pid}-{start}.waiting"
        waiting = live_waiters(directory)
        previous = next((row for path, row in waiting if path == mine), None)
        waited_from = since or now.timestamp()
        if previous is not None:
            waited_from = min(waited_from, previous["since"])
        waiter: Waiter = {"pid": pid, "start_time": start or "", "need": peak.bytes, "since": waited_from,
                          "label": f"{worktree or repo} {step} ({os.environ.get('BUILDLOG_CALLER') or 'caller'} pid {pid})"}
        if previous is not None:
            if "bump_order" in previous:
                waiter["bump_order"] = previous["bump_order"]
            if previous.get("holds_younger", False):
                waiter["holds_younger"] = True
        available, total = read_meminfo(meminfo)
        pressure = read_pressure()
        head = head_priority([row for path, row in waiting if path != mine] + ([waiter] if argv else []),
                             available, total, running, now.timestamp())
        if isinstance(head, HeadPriority) and not head.waiter.get("holds_younger", False):
            head.waiter["holds_younger"] = True
            for head_path, head_row in waiting:
                if head_path != mine and head_row is head.waiter:
                    _ = head_path.write_text(json.dumps(head_row, separators=(",", ":")))
        head_need, head_label = 0, ""
        if isinstance(head, HeadPriority) and waiter_order(head.waiter) < waiter_order(waiter):
            head_need, head_label = head.waiter["need"], waiter_label(head.waiter)
        decision = queue_decision(available, total, waiter, running, head, pressure)
        reservation: Reserved | NotReserved = NotReserved("held" if argv else "launch")
        if argv and decision.state == "hold" and start is not None:
            identity = waiter_identity(repo, step, worktree)
            waiter["session_id"], waiter["session_name"] = identity["session_id"], identity["session_name"]
            waiter["step"], waiter["folder"] = identity["step"], identity["folder"]
            _ = mine.write_text(json.dumps(waiter, separators=(",", ":")))
        if argv and decision.state == "admit":
            mine.unlink(missing_ok=True)
            if start is not None:
                path = directory / f"{pid}-{start}-{time.time_ns()}.reservation"
                row: Reservation = {"pid": pid, "start_time": start, "need": peak.bytes,
                                    "repo": repo, "step": step, "worktree": worktree,
                                    "admitted_at": store.utc_iso(now.timestamp()), "sidecar": sidecar,
                                    "test_threads": width,
                                    "call_id": os.environ.get("BUILDLOG_CALL_ID") or None}
                _ = path.write_text(json.dumps(row, separators=(",", ":")))
                reservation = Reserved(path)
            else:
                reservation = NotReserved("untracked")
        return CheckResult(decision, peak, [row for _, row in live], reservation, repo, step, available, profile, head_need, head_label)


def waiter_identity(repo: str, step: str, worktree: str) -> WaiterIdentity:
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return {"session_id": session, "session_name": os.environ.get("CLAUDE_SESSION_NAME", ""),
            "step": f"{repo} {step}".strip(), "folder": os.getcwd() if worktree else ""}


class WaiterIdentity(TypedDict):
    session_id: str
    session_name: str
    step: str
    folder: str


class QueueIdentityReader:
    def __init__(self, processes: dict[int, ProcessIdentity], proc: Path = PROC) -> None:
        self.processes: dict[int, ProcessIdentity] = processes
        self.proc: Path = proc
        self.environments: dict[int, dict[str, str]] = {}
        self.names: dict[str, str] = {}
        directory = Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions")))
        for path in directory.glob("*.json"):
            try:
                row = cast(dict[str, object], json.loads(path.read_text()))
                session, name = row.get("sessionId"), row.get("name")
                if isinstance(session, str) and isinstance(name, str):
                    self.names[session] = name
            except (OSError, ValueError, TypeError, AttributeError):
                continue

    def identity(self, pid: int, row: dict[str, object]) -> WaiterIdentity:
        session = str(row.get("session_id", ""))
        name = str(row.get("session_name", ""))
        folder = str(row.get("folder") or row.get("worktree") or "")
        step = str(row.get("step", ""))
        try:
            folder = str((self.proc / str(pid) / "cwd").readlink())
        except OSError:
            pass
        current, seen = pid, set[int]()
        while not session and current in self.processes and current not in seen:
            seen.add(current)
            if current not in self.environments:
                try:
                    raw = (self.proc / str(current) / "environ").read_bytes().decode(errors="replace")
                    self.environments[current] = dict(part.split("=", 1) for part in raw.split("\0") if "=" in part)
                except OSError:
                    self.environments[current] = {}
            env = self.environments[current]
            session = env.get("CODEX_THREAD_ID") or env.get("CLAUDE_CODE_SESSION_ID", "")
            name = name or env.get("CLAUDE_SESSION_NAME", "")
            current = self.processes[current].parent
        if not step:
            try:
                argv = (self.proc / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
                verb = next((item for item in argv if item in ("test", "lint", "check", "build", "doc", "clippy")), "build")
                step = verb
            except OSError:
                step = "build"
        return {"session_id": session, "session_name": name or self.names.get(session, ""), "step": step, "folder": folder}


def queue_rows(directory: Path, suffix: str, processes: dict[int, ProcessIdentity]) -> list[tuple[Path, dict[str, object]]]:
    rows: list[tuple[Path, dict[str, object]]] = []
    for path in directory.glob(f"*.{suffix}"):
        try:
            row = cast(dict[str, object], json.loads(path.read_text()))
            pid = row["pid"]
            identity = processes.get(pid) if isinstance(pid, int) else None
            if identity is not None and identity.start == row["start_time"]:
                rows.append((path, row))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return rows


def read_queue(directory: Path, processes: dict[int, ProcessIdentity], now: float) -> tuple[QueueView, list[tuple[Path, Waiter]]]:
    proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
    identities = QueueIdentityReader(processes, proc)
    running: list[RunningStep] = []
    memory: list[StepMemory] = []
    for _, row in queue_rows(directory, "reservation", processes):
        reservation = cast(Reservation, cast(object, row))
        pid = reservation["pid"]
        reading = step_memory(reservation, datetime.fromtimestamp(now, UTC), processes, proc)
        if isinstance(reading, GoneMemory):
            continue
        identity = identities.identity(pid, row)
        running.append(RunningStep(identity["session_id"], identity["session_name"], identity["step"],
                                   identity["folder"], reservation["need"], reading))
        memory.append(reading)
    available, total = 0, 0
    unavailable = False
    try:
        available, total = read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
    except (OSError, ValueError):
        unavailable = True
    pressure = read_pressure()
    ordered = sorted(((path, cast(Waiter, cast(object, row))) for path, row in queue_rows(directory, "waiting", processes)),
                     key=lambda item: waiter_order(item[1]))
    head = head_priority([row for _, row in ordered], available, total, memory, now)
    pending = sum(growth_headroom(item) for item in memory)
    waiting: list[WaitingStep] = []
    for _, row in ordered:
        identity = identities.identity(row["pid"], cast(dict[str, object], cast(object, row)))
        own = queue_decision(available, total, row, memory, head, pressure)
        reason: WaitReason = ReadyForAdmission()
        if unavailable:
            reason = ReadingUnavailable()
        elif isinstance(head, HeadPriority) and waiter_order(head.waiter) < waiter_order(row):
            head_identity = identities.identity(head.waiter["pid"], cast(dict[str, object], cast(object, head.waiter)))
            reason = BehindHead(head_identity["session_name"] or Path(head_identity["folder"]).name or str(head.waiter["pid"]), head.waiter["since"])
        elif isinstance(own.pressure, MemoryPressure):
            reason = own.pressure
        elif own.state == "hold" and isinstance(head, HeadPriority) and row is head.waiter and memory:
            reason = HeadWaitingForDrain(len(memory))
        elif own.state == "hold":
            reason = (NeedsMemory(row["need"], available) if available < own.threshold
                      else NeedsBuildBudget(row["need"], own.charged, own.budget))
        waiting.append(WaitingStep(row["pid"], identity["session_id"], identity["session_name"], identity["step"],
                                   identity["folder"], row["since"], row["need"], reason))
    return QueueView(tuple(waiting), tuple(running), available, pending, total * 5 // 100 if total else 0, now), ordered


def _queue_lock_timeout(_signum: int, _frame: object) -> NoReturn:
    raise TimeoutError(f"admission lock busy for {QUEUE_LOCK_TIMEOUT_SECONDS:g} s; try again")


def queue_view() -> QueueView:
    directory = store.root() / "admission"
    try:
        lock = (directory / "lock").open("r")
    except FileNotFoundError:
        return read_queue(directory, process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc"))), time.time())[0]
    with lock:
        previous = signal.signal(signal.SIGALRM, _queue_lock_timeout)
        try:
            _ = signal.setitimer(signal.ITIMER_REAL, QUEUE_LOCK_TIMEOUT_SECONDS)
            fcntl.flock(lock, fcntl.LOCK_SH)
        finally:
            _ = signal.setitimer(signal.ITIMER_REAL, 0)
            _ = signal.signal(signal.SIGALRM, previous)
        return read_queue(directory, process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc"))), time.time())[0]


def bump_waiter(selector: str, actor: str) -> WaitingStep:
    directory = admission_dir(store.root())
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        view, ordered = read_queue(directory, process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc"))), time.time())
        matches: list[int] = []
        if selector.isdecimal():
            position = int(selector) - 1
            if 0 <= position < len(view.waiting):
                matches = [position]
        else:
            matches = [position for position, item in enumerate(view.waiting)
                       if selector in (item.session_id, item.session_name, item.folder, Path(item.folder).name)]
        if len(matches) != 1:
            labels = ", ".join(f"{position + 1}: {item.session_name or item.session_id or item.pid} {item.folder}"
                               for position, item in enumerate(view.waiting) if not matches or position in matches)
            raise ValueError(f"{selector!r}: {'unknown' if not matches else 'ambiguous'} waiter; matches: {labels or 'none'}")
        position = matches[0]
        moved = view.waiting[position]
        path, row = ordered[position]
        # Each move gets an order before all current waiters. Keep the wait time.
        row["bump_order"] = min(0, *(item.get("bump_order", 0) for _, item in ordered)) - 1
        temporary = path.with_suffix(".bump")
        _ = temporary.write_text(json.dumps(row, separators=(",", ":")))
        os.replace(temporary, path)
        record.queue_bump(actor, moved.session_id or moved.session_name, moved.step, moved.folder, position + 1, view.now)
        reason = moved.reason if isinstance(moved.reason, (NeedsBuildBudget, NeedsMemory, ReadingUnavailable, MemoryPressure)) else ReadyForAdmission()
        return WaitingStep(moved.pid, moved.session_id, moved.session_name, moved.step, moved.folder,
                           row["since"], moved.need, reason)


def release(path: Path, status: int, root: Path | None = None) -> None:
    root = root if root is not None else store.root()
    directory = admission_dir(root)
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            row = cast(Reservation, json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return
        maximum = path.with_suffix(".max")
        try:
            peak = int(maximum.read_text())
            if peak > 0 and row["sidecar"] and Path(row["sidecar"]).is_file():
                store.append_line(directory / "anon_peaks.jsonl", {
                    "host": store.host_name(), "repo": row["repo"], "step": row["step"],
                    "worktree": row["worktree"], "anon_peak_bytes": peak, "status": status,
                    "test_threads": row.get("test_threads"), "call_id": row.get("call_id"),
                    "ended_at": store.utc_iso(time.time()),
                })
        except (OSError, ValueError):
            pass
        path.unlink(missing_ok=True)
        maximum.unlink(missing_ok=True)
        if row["sidecar"]:
            Path(row["sidecar"]).unlink(missing_ok=True)


def set_width(path: Path, width: int, root: Path | None = None) -> None:
    """Record the test width a reserved step runs at, claimed after its admission."""
    root = root if root is not None else store.root()
    with (admission_dir(root) / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            row = cast(Reservation, json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return
        row["test_threads"] = width
        _ = path.write_text(json.dumps(row, separators=(",", ":")))


def _epoch(instant: str) -> float:
    try:
        return datetime.fromisoformat(instant.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def clean_field(value: str) -> str:
    return value.replace("|", " ").replace("\n", " ")


def main() -> None:
    action = sys.argv[1]
    if action == "release":
        release(Path(sys.argv[2]), int(sys.argv[3]))
        return
    if action == "width":
        set_width(Path(sys.argv[2]), int(sys.argv[3]))
        return
    _, _, meminfo, pid, sidecar, since, cached_repo, cached_step, cached_worktree, cached_need, cached_source, cached_count, *argv = sys.argv
    profile = None
    if cached_need != "-":
        profile = StepProfile(cached_repo, cached_step, cached_worktree,
                              ExpectedPeak(int(cached_need), cast(Literal["measured", "buildlog", "fallback"], cached_source), int(cached_count)))
    result = check(Path(meminfo), int(pid), sidecar, argv, float(since or 0), profile=profile)
    decision, peak, live = result.decision, result.peak, result.live
    summary = ", ".join(f"{row['repo']} {row['step']} in {row['worktree']}" for row in live)
    if isinstance(decision.pressure, MemoryPressure):
        summary = f"{pressure_text(decision.pressure)}; {summary}".rstrip("; ")
    reservation = result.reservation
    path = str(reservation.path) if isinstance(reservation, Reserved) else ""
    reason = "reserved" if isinstance(reservation, Reserved) else reservation.reason
    fields: list[str | int] = [decision.state, decision.threshold, peak.bytes, peak.source, peak.count,
                               decision.promised, len(live), decision.reserve, summary, path, result.repo, result.step,
                               result.available, reason, result.profile.worktree]
    # Running shells may still read the old field count.
    if os.environ.get("BUILDLOG_MEM_DETAIL") == "1":
        fields.extend([decision.measured, result.head_need, result.head_label])
    else:
        details = f"{decision.measured / GIB:.1f} GiB measured"
        if result.head_need:
            details += f"; head {result.head_label} needs {result.head_need / GIB:.1f} GiB"
        fields[8] = f"{summary}; {details}" if summary else details
    print("|".join(clean_field(str(field)) for field in fields))


if __name__ == "__main__":
    main()
