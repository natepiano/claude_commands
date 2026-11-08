"""Read detached worker runs and measure their CPU and log growth for one board build."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from scripts.agents.codex_mesh import DETACHED_RUNS_DIR
from scripts.agents.mesh_runs import RunRecord
from scripts.agents import worker_lifecycle
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff


@dataclass(frozen=True)
class ProcessCpu:
    ppid: int
    cpu_seconds: float


type ProcessTable = dict[int, ProcessCpu]


@dataclass(frozen=True)
class ProcessCpuWithReapedChildren:
    cpu_seconds: float


@dataclass(frozen=True)
class ProcessStatUnavailable:
    reason: str


type ProcessStatReader = Callable[[int], ProcessCpuWithReapedChildren | ProcessStatUnavailable]


def parse_process_stat(text: str, clock_ticks: int) -> ProcessCpuWithReapedChildren | ProcessStatUnavailable:
    """Read CPU ticks after the command name's final closing parenthesis."""
    closing = text.rfind(")")
    if closing < 0 or clock_ticks <= 0:
        return ProcessStatUnavailable("the process stat header or clock tick rate is invalid")
    fields = text[closing + 1:].split()
    try:
        # The suffix starts at field 3. Fields 14 through 17 hold own and reaped child CPU ticks.
        ticks = sum(int(fields[index]) for index in (11, 12, 13, 14))
    except (IndexError, ValueError):
        return ProcessStatUnavailable("the process stat CPU fields are missing or invalid")
    return ProcessCpuWithReapedChildren(ticks / clock_ticks)


def read_process_stat(pid: int) -> ProcessCpuWithReapedChildren | ProcessStatUnavailable:
    """Read a tree member's own CPU and the CPU of children it reaped."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return parse_process_stat(text, os.sysconf("SC_CLK_TCK"))
    except (OSError, UnicodeError, ValueError) as error:
        return ProcessStatUnavailable(str(error))


@dataclass(frozen=True)
class ProcessSnapshotFailed:
    reason: str


def parse_process_table(ps_output: str) -> ProcessTable:
    """Parse Linux and macOS CPU times from the one process snapshot."""
    result: ProcessTable = {}
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            days, mark, clock = parts[2].partition("-")
            clocks = (clock if mark else days).split(":")
            if len(clocks) not in (2, 3):
                continue
            seconds = float(clocks[-1]) + int(clocks[-2]) * 60
            if len(clocks) == 3:
                seconds += int(clocks[0]) * 3600
            if mark:
                seconds += int(days) * 86400
            if pid > 0 and ppid >= 0 and math.isfinite(seconds) and seconds >= 0:
                result[pid] = ProcessCpu(ppid, seconds)
        except ValueError:
            continue
    return result


def read_process_table() -> ProcessTable | ProcessSnapshotFailed:
    """Take one process snapshot for all shown runs."""
    try:
        result = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,time="], capture_output=True,
                                text=True, check=False)
    except OSError as error:
        return ProcessSnapshotFailed(str(error))
    if result.returncode != 0:
        return ProcessSnapshotFailed(result.stderr.strip() or f"ps exited {result.returncode}")
    return parse_process_table(result.stdout)


def tree_cpu_seconds(table: ProcessTable, root_pid: int, stat_reader: ProcessStatReader) -> float:
    """Sum tree CPU and Linux reaped child CPU, with a per-pid ps fallback."""
    children: dict[int, list[int]] = defaultdict(list)
    for pid, process in table.items():
        children[process.ppid].append(pid)
    pending = [root_pid]
    seen: set[int] = set()
    total = 0.0
    while pending:
        pid = pending.pop()
        if pid in seen or pid not in table:
            continue
        seen.add(pid)
        seconds = table[pid].cpu_seconds
        accounting = stat_reader(pid)
        if isinstance(accounting, ProcessCpuWithReapedChildren):
            seconds = accounting.cpu_seconds
        total += seconds
        pending.extend(children[pid])
    return total


class MeasuredGrowth(TypedDict):
    kind: Literal["measured"]
    cpu_percent: float
    log_bytes_per_minute: float


class FirstReading(TypedDict):
    kind: Literal["first_reading"]


class FinishedGrowth(TypedDict):
    kind: Literal["finished"]
    exit_code: int


class WorkerRun(TypedDict):
    label: str
    started_at: float
    growth: MeasuredGrowth | FirstReading | FinishedGrowth
    last_line: str


class WorkerRunState(TypedDict):
    state: str
    run: NotRequired[WorkerRun]


class RunReading(TypedDict):
    run_id: str
    at: float
    cpu_seconds: float
    log_bytes: int


@dataclass(frozen=True)
class ProcessSnapshotUnread:
    pass


READING_RETENTION_SECONDS = 10 * 60


def _read_readings(path: Path) -> dict[str, RunReading]:
    readings: dict[str, RunReading] = {}
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(value, dict):
            return readings
        for run_id, reading in cast(dict[str, object], value).items():
            if not isinstance(reading, dict):
                continue
            row = cast(dict[str, object], reading)
            if (row.get("run_id") == run_id and isinstance(row.get("log_bytes"), int)
                    and all(isinstance(row.get(key), int | float) for key in ("at", "cpu_seconds"))):
                readings[run_id] = RunReading(
                    run_id=run_id, at=float(cast(int | float, row["at"])),
                    cpu_seconds=float(cast(int | float, row["cpu_seconds"])),
                    log_bytes=cast(int, row["log_bytes"]))
    except (OSError, UnicodeError, ValueError):
        pass
    return readings


@dataclass
class BoardRunReadings:
    """Share the process snapshot and readings across every board in one invocation."""

    board_root: Path
    now: float
    process_reader: Callable[[], ProcessTable | ProcessSnapshotFailed] = read_process_table
    stat_reader: ProcessStatReader = read_process_stat
    previous: dict[str, RunReading] = field(init=False, default_factory=dict)
    current: dict[str, RunReading] = field(init=False, default_factory=dict)
    sources: set[Path] = field(init=False, default_factory=set)
    finished_runs: set[str] = field(init=False, default_factory=set)
    snapshot: ProcessTable | ProcessSnapshotFailed | ProcessSnapshotUnread = field(init=False, default_factory=ProcessSnapshotUnread)

    def __post_init__(self) -> None:
        self.previous = _read_readings(self.board_root / "run_readings.json")

    def processes(self) -> ProcessTable | ProcessSnapshotFailed:
        if isinstance(self.snapshot, ProcessSnapshotUnread):
            self.snapshot = self.process_reader()
        return self.snapshot

    def growth(self, run: RunRecord) -> MeasuredGrowth | FirstReading:
        processes = self.processes()
        if isinstance(processes, ProcessSnapshotFailed):
            return {"kind": "first_reading"}
        log = Path(run["log"])
        try:
            size = log.stat().st_size
        except OSError:
            size = 0
        reading = RunReading(run_id=run["run_id"], at=self.now,
                             cpu_seconds=tree_cpu_seconds(processes, run["pid"], stat_reader=self.stat_reader), log_bytes=size)
        self.current[run["run_id"]] = reading
        previous = self.previous.get(run["run_id"])
        if previous is None or self.now <= previous["at"]:
            return {"kind": "first_reading"}
        elapsed = self.now - previous["at"]
        return {"kind": "measured",
                "cpu_percent": max(0.0, reading["cpu_seconds"] - previous["cpu_seconds"]) / elapsed * 100,
                "log_bytes_per_minute": max(0, size - previous["log_bytes"]) / elapsed * 60}

    def save(self) -> None:
        """Merge fresh readings under a shared lock without removing another board's live readings."""
        if not self.current and not self.previous and not self.finished_runs:
            return
        self.board_root.mkdir(parents=True, exist_ok=True)
        path = self.board_root / "run_readings.json"
        # The lock inode stays stable when atomic publication replaces the readings file.
        with (self.board_root / "run_readings.lock").open("a") as lock:
            worker_lifecycle.acquire_flock(lock)
            latest = _read_readings(path)
            merged = {run_id: reading for run_id, reading in latest.items()
                      if run_id not in self.finished_runs and reading["at"] >= self.now - READING_RETENTION_SECONDS}
            for run_id, reading in self.current.items():
                if run_id not in merged or merged[run_id]["at"] <= reading["at"]:
                    merged[run_id] = reading
            if merged != latest:
                write_text_atomic(path, json.dumps(merged) + "\n")


def _session_runs(session: Path, readings: BoardRunReadings) -> dict[str, RunRecord]:
    newest: dict[str, RunRecord] = {}
    readings.sources.update((session, session / DETACHED_RUNS_DIR))
    for path in sorted((session / DETACHED_RUNS_DIR).glob("*.json")):
        readings.sources.add(path)
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
            if not isinstance(value, dict):
                continue
            row = cast(dict[str, object], value)
            if (row.get("status") not in ("running", "finished")
                    or not all(isinstance(row.get(key), str) for key in ("run_id", "staff_id", "label", "log"))
                    or not isinstance(row.get("started_at"), int | float)
                    or not isinstance(row.get("pid"), int)):
                continue
            if (any(key in row and not isinstance(row[key], str) for key in ("delivery", "finished_at"))
                    or ("exit_code" in row and not isinstance(row["exit_code"], int))):
                continue
            run = cast(RunRecord, cast(object, row))
            if run["status"] == "finished":
                readings.finished_runs.add(run["run_id"])
            current = newest.get(run["staff_id"])
            if current is None or run["started_at"] > current["started_at"]:
                newest[run["staff_id"]] = run
        except (OSError, UnicodeError, ValueError):
            continue
    return newest


ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")


def _last_line(log: Path) -> str:
    try:
        with log.open("rb") as stream:
            _ = stream.seek(0, 2)
            _ = stream.seek(max(0, stream.tell() - 4096))
            tail = stream.read(4096).decode("utf-8", errors="replace")
        return next((clean for line in reversed(tail.splitlines())
                     if (clean := ANSI_ESCAPE.sub("", line).strip())), "")
    except OSError:
        return ""


def _finished_epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def observe_session(session: Path, pane_states: Mapping[str, str], pass_starts: Mapping[str, float],
                    readings: BoardRunReadings) -> dict[str, WorkerRunState]:
    """Match runs by staff id and apply worker state priority."""
    runs = _session_runs(session, readings)
    result: dict[str, WorkerRunState] = {}
    for staff_id, pane_state in pane_states.items():
        run = runs.get(staff_id)
        row = WorkerRunState(state=pane_state)
        if run is not None and run["status"] == "running":
            growth = readings.growth(run)
            if pane_state not in ("working", "running", "starting"):
                processes = readings.processes()
                row["state"] = ("waiting on run" if isinstance(processes, ProcessSnapshotFailed)
                                or run["pid"] in processes else "run gone")
                row["run"] = WorkerRun(label=run["label"], started_at=run["started_at"], growth=growth,
                                       last_line=_last_line(Path(run["log"])))
        elif (run is not None and run.get("delivery", "").startswith("queued")
              and pass_starts.get(staff_id, 0) <= _finished_epoch(run.get("finished_at", ""))):
            row["state"] = "result queued"
            row["run"] = WorkerRun(label=run["label"], started_at=run["started_at"],
                                   growth={"kind": "finished", "exit_code": run.get("exit_code", 1)},
                                   last_line=_last_line(Path(run["log"])))
        if pane_state in ("working", "running", "starting"):
            row = WorkerRunState(state="working")
        result[staff_id] = row
    return result


def observe_pane_rows(session: Path, pane_rows: Sequence[Mapping[str, object]], record: staff.StaffSnapshot,
                      owner_id: staff.StaffId, events: Sequence[Mapping[str, object]],
                      readings: BoardRunReadings) -> list[dict[str, object]]:
    """Match pane rows and pass starts to staff before applying detached run states."""
    rows = [dict(row) for row in pane_rows]
    members = {row.name: str(row.id) for row in record.find(kind="worker", parent=owner_id)}
    states: dict[str, str] = {}
    for row in rows:
        slot, state = row.get("slot"), row.get("state")
        if isinstance(slot, str) and slot in members:
            states[members[slot]] = state if isinstance(state, str) else ""
    pass_starts: dict[str, float] = {}
    for event in events:
        if event.get("event_type") != "pass_started":
            continue
        slot = event.get("team_slot") or event.get("team_role")
        identity = event.get("worker_id") or (members.get(slot, "") if isinstance(slot, str) else "")
        started = event.get("pass_started_at") or event.get("timestamp_epoch")
        if (isinstance(identity, str) and identity and isinstance(started, int | float)
                and not isinstance(started, bool)):
            pass_starts[identity] = max(pass_starts.get(identity, 0), float(started))
    observed = observe_session(session, states, pass_starts, readings)
    for row in rows:
        slot = row.get("slot")
        identity = members.get(slot, "") if isinstance(slot, str) else ""
        if identity in observed:
            _ = row.pop("run", None)
            row.update(observed[identity])
    return rows
