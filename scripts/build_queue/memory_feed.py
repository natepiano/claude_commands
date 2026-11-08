#!/usr/bin/env python3
"""Publish the build memory page and its current queue reading."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo


class CodeRootArguments(argparse.Namespace):
    code_root: Path = Path(__file__).resolve().parents[2]


installed = CodeRootArguments()
if __name__ == "__main__":
    bootstrap = argparse.ArgumentParser(add_help=False)
    _ = bootstrap.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[2])
    installed, _remaining = bootstrap.parse_known_args(namespace=installed)
    sys.path.insert(0, str(installed.code_root))

from scripts.build_queue import admission_code


def installed_code_version(code_root: Path) -> str:
    """Use the admission service fingerprint for the installed sources."""
    return admission_code.admission_code_version(code_root / "scripts")


LOADED_ADMISSION_CODE_VERSION = installed_code_version(installed.code_root)

from scripts.buildlog import charts
from scripts.buildlog.store import host_name
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue.queue_evidence import ci_amount as ci_bytes_text, room_text, keep_aside_text, service_activity_text, step_labels
from scripts.build_queue import admission_client, admission_protocol as wire, head_facts, queue_view
from scripts.build_queue.queue_lines import age, plain, step_text
from scripts.build_queue.queue_view import reason_phrase
from scripts.lib.build_steps import RECORDED_STEP_NAMES
from scripts.build_queue.scope_activity import LiveBuildRun, NotServiceSlice, ScopeProcesses
from scripts.lint import memory_admit as gate
from scripts.message import unit_labels
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone

GIB = 1024 ** 3
CI_CGROUP = Path("/sys/fs/cgroup/hana.slice/hana-ci.slice")
PROC = Path("/proc")


@dataclass(frozen=True)
class FeedConfiguration:
    output_folder: Path
    interval_seconds: float
    history_days: int
    swap_rows: int
    report_refresh_seconds: int


def read_configuration(settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> FeedConfiguration:
    """Read the page settings from one configuration."""
    if isinstance(settings, conf.UseConfiguredValue):
        settings = conf.read("memory_feed")
    return FeedConfiguration(Path(os.path.expandvars(settings.text("output_folder"))).expanduser(),
                             settings.number("interval_seconds"), settings.whole("history_days"),
                             settings.whole("swap_rows"), settings.whole("report_refresh_seconds"))


class PageArguments(CodeRootArguments):
    once: bool = False


def page_arguments(argv: Sequence[str]) -> PageArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--once", action="store_true", help="Write one reading and exit.")
    _ = parser.add_argument("--code-root", type=Path, default=PageArguments.code_root,
                            help="Read the installed admission code from this repository.")
    return parser.parse_args(argv, namespace=PageArguments())


def publish_page(configuration: FeedConfiguration) -> None:
    """Publish the repository page with the configured refresh interval."""
    source = Path(__file__).with_name("memory_live.html").read_text(encoding="utf-8")
    page = source.replace("__MEMORY_FEED_INTERVAL_SECONDS__", json.dumps(configuration.interval_seconds))
    write_text_atomic(configuration.output_folder / "memory_live.html", page, new_file_mode=0o644)


def gib(value: object) -> float:
    return float(value) / GIB if isinstance(value, (int, float)) else 0.0


class GroupPart(TypedDict):
    who: str
    pid: int | gate.PidNotRecorded
    gib: float
    age: str


class LeftoverProcess(TypedDict):
    pid: int | gate.PidNotRecorded
    program: str


class LeftoverScope(TypedDict):
    owner: str
    age: str
    used: float
    members: list[LeftoverProcess]


class CIRunnerMemory(TypedDict):
    unit: str
    name: str
    used: float
    pid: int | gate.PidNotRecorded


class BusyCIRunner(CIRunnerMemory):
    state: Literal["busy"]
    started_at: float
    job: str


class IdleCIRunner(CIRunnerMemory):
    state: Literal["idle"]


class UnreadableCIRunner(TypedDict):
    unit: str
    name: str
    state: Literal["unreadable"]
    diagnostic: str


CIRunner = BusyCIRunner | IdleCIRunner | UnreadableCIRunner


@dataclass(frozen=True)
class RunnerProcess:
    pid: int
    parent: int
    started_at: float
    program: str
    command: tuple[str, ...]


class NoProgress(TypedDict):
    kind: Literal["NoProgress"]
    reason: str


class RowProgress(TypedDict):
    kind: Literal["RowProgress"]
    phase: Literal["building", "testing"]
    done: int
    total: int
    percent: float
    unit: Literal["modules", "units", "tests"]


class RunRow(TypedDict):
    machine: NotRequired[str]
    age: NotRequired[str]
    name: str
    showrunner: str
    unit: str
    progress: RowProgress | NoProgress
    state: str
    pid: int | gate.PidNotRecorded
    detail: str
    used: float | gate.RunUseUnavailable
    counted: float | gate.RunUseUnavailable
    reserved: float
    started_at: float
    queued: bool
    keep_text: str
    group: str
    ci: bool
    parts: list[GroupPart]
    ci_runners: NotRequired[list[CIRunner]]


class SwapRow(TypedDict):
    name: str
    detail: str
    swap: float


class WaitRow(TypedDict):
    name: str
    showrunner: str
    unit: str
    pid: int
    detail: str
    need: float
    since: float
    reason: str
    fits: bool


@dataclass(frozen=True)
class PressureNotRecorded:
    reason: str


class PressureValues(TypedDict):
    some: float | PressureNotRecorded
    full: float | PressureNotRecorded


class PressureLimits(TypedDict):
    some: float
    full: float
    swap: float


class ReportCost(TypedDict):
    machine: str
    cause: str
    recent: dict[str, object]
    window: dict[str, object]
    action: str
    owner: str


@dataclass(frozen=True)
class ReportSections:
    charts: dict[str, object]
    top: tuple[ReportCost, ...]
    start: str
    end: str
    recent_days: int
    zone: str


class ReportNotRead(Enum):
    VALUE = "not read"


class MemoryPage(TypedDict):
    ok: bool
    read_at: float
    total: float
    available: float
    cap: float | gate.CapUnavailable
    floor: float
    cap_text: str
    shortfall: float
    used: float
    reserved: float
    taken: float
    room: float
    room_source: Literal["free"]
    limit: str
    other: float
    shown_reserved: float
    ci_name: str
    ci_ram: float
    ci_kept: float
    shown_ci_kept: float
    shown_safety: float
    waiting: int
    smallest_need: float
    pressure: str
    swap_used: float
    swap_total: float
    walk: list[RunRow]
    waiters: list[WaitRow]
    swap_by: list[SwapRow]
    cloud: list[str]
    cloud_heading: str
    cloud_slots_used: int
    cloud_slots: int
    psi: PressureValues
    limits: PressureLimits
    report: ReportSections | charts.IndexUnavailable
    swap_rows: int
    leftovers: list[LeftoverScope]


class RetainedReading(MemoryPage):
    read_error: str
    error_at: float


@dataclass(frozen=True)
class NoGoodReading:
    pass


class PageUnavailable(TypedDict):
    ok: bool
    error: str
    read_at: float


def cargo_job(command: tuple[str, ...]) -> str:
    """Return the job word from a Cargo command."""
    for index, argument in enumerate(command):
        if Path(argument).name not in ("cargo", "cargo-tile-real", "cargo-nextest"):
            continue
        arguments = iter(command[index + 1:])
        for argument in arguments:
            if argument in ("--color", "--config", "--manifest-path", "--lockfile-path", "-C", "-Z"):
                _ = next(arguments, "")
            elif not argument.startswith(("-", "+")):
                if argument in ("test", "nextest"):
                    return "tests"
                if argument == "build" and any(flag in command for flag in ("--release", "-r")):
                    return "release build"
                return argument
    return ""


def read_ci_runner(unit: Path, proc: Path) -> CIRunner:
    name = unit.name.removeprefix("github-runner-").removesuffix(".service")
    try:
        used = gib(gate.slice_working_set(unit).bytes)
        boot = float(next(line.split()[1] for line in (proc / "stat").read_text().splitlines()
                          if line.startswith("btime ")))
        ticks = int(os.sysconf("SC_CLK_TCK"))
        members: set[int] = set()
        pending = [unit]
        while pending:
            scope = pending.pop()
            members.update(int(value) for value in (scope / "cgroup.procs").read_text().split())
            pending.extend(child for child in scope.iterdir() if child.is_dir())
        processes: dict[int, RunnerProcess] = {}
        for pid in sorted(members):
            try:
                stat = (proc / str(pid) / "stat").read_text()
                fields = stat.rsplit(") ", 1)[1].split()
                if fields[0] in ("Z", "X"):
                    continue
                command = tuple(part for part in (proc / str(pid) / "cmdline").read_bytes()
                                .decode(errors="replace").split("\0") if part)
                processes[pid] = RunnerProcess(pid, int(fields[1]), boot + int(fields[19]) / ticks,
                                               stat.split("(", 1)[1].rsplit(") ", 1)[0], command)
            except FileNotFoundError:
                continue
        workers = sorted((p for p in processes.values() if p.program == "Runner.Worker"),
                         key=lambda p: (p.started_at, p.pid))
        if workers:
            worker = workers[0]
            jobs: list[tuple[float, str]] = []
            for process in processes.values():
                job = cargo_job(process.command)
                if not job:
                    continue
                ancestor = process.pid
                seen: set[int] = set()
                while ancestor in processes and ancestor not in seen:
                    if ancestor == worker.pid:
                        jobs.append((process.started_at, job))
                        break
                    seen.add(ancestor)
                    ancestor = processes[ancestor].parent
            return {"unit": unit.name, "name": name, "state": "busy", "used": used,
                    "pid": worker.pid, "started_at": worker.started_at,
                    "job": min(jobs)[1] if jobs else "CI job"}
        listeners = sorted(p.pid for p in processes.values() if p.program == "Runner.Listener")
        return {"unit": unit.name, "name": name, "state": "idle", "used": used,
                "pid": listeners[0] if listeners else gate.PidNotRecorded()}
    except (OSError, ValueError, IndexError, StopIteration) as error:
        return {"unit": unit.name, "name": name, "state": "unreadable", "diagnostic": str(error)}


def job_age(started_at: float, now: float) -> str:
    minutes = max(0, int((now - started_at) / 60 + 0.5))
    if minutes < 1:
        return "under 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h {minutes % 60} min" if hours < 48 else f"{hours // 24} days"


def ci_amount(value: float) -> str:
    """Show the shared CI text for an amount in GiB."""
    return ci_bytes_text(round(value * gate.GIB))


def build_ci_row(used: float, ci_kept: float, now: float,
                 cgroup: Path = CI_CGROUP, proc: Path = PROC) -> RunRow:
    runners: list[CIRunner]
    try:
        units = sorted(child for child in cgroup.iterdir()
                       if child.name.startswith("github-runner-") and child.name.endswith(".service"))
        runners = [read_ci_runner(unit, proc) for unit in units]
    except OSError as error:
        runners = [{"unit": cgroup.name, "name": "hana CI",
                                    "state": "unreadable", "diagnostic": str(error)}]
    busy = [runner for runner in runners if runner["state"] == "busy"]
    busy.sort(key=lambda runner: runner["started_at"])
    detail = (f"{len(busy)} of {len(runners)} runners busy · {job_age(busy[0]['started_at'], now)}"
              if busy else "idle")
    if any(runner["state"] == "unreadable" for runner in runners):
        detail = detail + " · runner not readable" if busy else "runner not readable"
    lines: list[str] = []
    for runner in runners:
        if runner["state"] == "unreadable":
            lines.append(f"{runner['name']} · not readable")
        elif runner["state"] == "busy":
            lines.append(f"{runner['name']} · {runner['job']} · {job_age(runner['started_at'], now)} · {ci_amount(runner['used'])}")
        else:
            lines.append(f"{runner['name']} · idle · {ci_amount(runner['used'])}")
    lines.append(f"hana CI uses {ci_amount(used)} now. This use is part of the work inside the cap.")
    if ci_kept > 0:
        lines.append(f"CI keep-aside {ci_kept:g} GiB also reduces the room.")
    lines.append("CI builds do not wait in the queue yet.")
    idle = [runner for runner in runners if runner["state"] == "idle" and isinstance(runner["pid"], int)]
    pid = busy[0]["pid"] if busy else idle[0]["pid"] if idle else gate.PidNotRecorded()
    return {"name": "hana CI", "showrunner": "", "unit": cgroup.name, "state": detail if not busy else "",
            "pid": pid, "detail": detail, "used": used,
            "counted": 0.0, "reserved": 0.0, "started_at": busy[0]["started_at"] if busy else 0,
            "queued": False, "keep_text": "\n".join(lines), "group": "", "ci": True, "parts": [],
            "ci_runners": runners, "progress": {"kind": "NoProgress", "reason": "no admission counter"}}


def row_progress(steps: Sequence[gate.RunningStep]) -> RowProgress | NoProgress:
    """Use the newest step counter from the admission reading."""
    reason = "no admission counter"
    phase: Literal["building", "testing"]
    unit: Literal["modules", "units", "tests"]
    for step in sorted(steps, key=lambda step: step.started_at, reverse=True):
        memory = step.measured
        if not isinstance(memory, gate.ProgressAdjustedMemory):
            continue
        counter = memory.counter
        if isinstance(counter, progress.NoCounter):
            reason = counter.reason
            continue
        if counter.phase.startswith("suites"):
            phase, unit = "testing", "modules"
        elif counter.phase.startswith("test:"):
            phase, unit = "testing", "tests"
        elif counter.phase.startswith("build:"):
            phase, unit = "building", "units"
        else:
            reason = f"unknown counter phase: {counter.phase}"
            continue
        return {"kind": "RowProgress", "phase": phase, "done": counter.done,
                "total": counter.total, "percent": counter.percent, "unit": unit}
    return {"kind": "NoProgress", "reason": reason}


def run_keep_text(hold: gate.RunKeepAside, steps: Sequence[gate.RunningStep]) -> str:
    lines = [keep_aside_text(hold)]
    for step in steps:
        detail = gate.progress_text(step.measured)
        if detail:
            lines.append(f"{step.step}: {detail}")
    return "\n".join(lines)


def read_stalls() -> PressureValues:
    """Read the current ten-second memory stall averages."""
    path = PROC / "pressure/memory"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        missing = PressureNotRecorded(f"Pressure is unreadable: {path}")
        return {"some": missing, "full": missing}
    values: PressureValues = {"some": PressureNotRecorded(f"Pressure is unreadable: {path}"),
                              "full": PressureNotRecorded(f"Pressure is unreadable: {path}")}
    for line in lines:
        fields = line.split()
        if not fields or fields[0] not in ("some", "full"):
            continue
        name = fields[0]
        for field in fields[1:]:
            if field.startswith("avg10="):
                try:
                    values[name] = float(field.removeprefix("avg10="))
                except ValueError:
                    values[name] = PressureNotRecorded(f"Pressure is unreadable: {path}")
    return values


def report_reading(days: int) -> ReportSections | charts.IndexUnavailable:
    """Read both report sections from one provider result."""
    try:
        recent_days = conf.read("buildlog").whole("TOP_COST_RECENT_DAYS")
        if days < recent_days:
            raise conf.Unusable(f"history_days={days} must be at least TOP_COST_RECENT_DAYS={recent_days}")
        report = charts.page_data(days, n=3)
        if isinstance(report, charts.IndexUnavailable):
            return report
        top = report.top
        rows: list[ReportCost] = []
        if not isinstance(top.rows, charts.NoDataYet):
            for row in top.rows:
                window: dict[str, object] = ({"state": "matches recent"} if isinstance(row.window, charts.WindowMatchesRecent)
                          else charts.cost_json(row.window.value, 3600))
                rows.append({"machine": row.machine, "cause": row.cause,
                             "recent": charts.cost_json(row.recent, 3600 * top.recent_days),
                             "window": window, "action": row.action.text, "owner": row.action.owner})
        zone = user_zone()
        return ReportSections(charts.chart_data(report.charts), tuple(rows), top.start.astimezone(zone).isoformat(),
                              top.end.astimezone(zone).isoformat(), top.recent_days, str(zone))
    except (OSError, ValueError, RuntimeError) as error:
        return charts.IndexUnavailable(str(error))


def reading(configuration: FeedConfiguration | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
            report: ReportSections | charts.IndexUnavailable | ReportNotRead = ReportNotRead.VALUE) -> MemoryPage:
    if isinstance(configuration, conf.UseConfiguredValue):
        configuration = read_configuration()
    parsed = admission_client.view()
    explanation = parsed.explanation
    captured = explanation.room
    host = explanation.decided_from.host
    if not isinstance(captured, gate.MemoryRoom) or not isinstance(host, gate.HostMemory):
        raise RuntimeError("recorded room unavailable")
    labels = unit_labels.unit_labels_for(
        [step.session_id for step in (*parsed.view.running, *parsed.view.waiting)], os.environ)
    leftovers: list[LeftoverScope] = []
    for step, memory, activity in queue_view.leftover_processes(parsed.view.running):
        identity = labels.get(activity.session_id)
        owner = identity.session if identity is not None else activity.session_name or "unlabelled session"
        scope_members: list[LeftoverProcess] = [{"pid": member.pid, "program": member.program} for member in activity.members]
        if not scope_members:
            scope_members = [{"pid": step.pid, "program": activity.program}]
        leftovers.append({"owner": owner, "age": queue_view.leftover_age(age(activity.started_at, explanation.now))
                          if activity.started_at else "not recorded", "used": gib(memory.bytes), "members": scope_members})
    cap = gib(captured.cap) if isinstance(captured.cap, int) else captured.cap
    safety = gib(captured.safety)
    used = gib(captured.used)
    total = gib(host.total) if isinstance(host.total, int) else used + gib(captured.available)
    reserved = gib(sum(run.bytes for run in captured.runs))
    ci_kept = gib(captured.ci_keep_aside.bytes)
    ci_ram, ci_name = gib(captured.ci_use), "hana CI"
    by_owner: dict[str, gate.RunningStep] = {}
    steps_by_owner: dict[str, list[gate.RunningStep]] = {}
    reserved_steps = (entry for entry in explanation.running if gate.running_has_reservation(entry.step))
    for reservation, entry in zip(explanation.decided_from.reservations, reserved_steps, strict=True):
        owner = gate.reservation_owner(reservation, entry.step)
        by_owner[owner] = entry.step
        steps_by_owner.setdefault(owner, []).append(entry.step)
    run_steps = {gate.run_owner(entry.step.run.pid, entry.step.run.start_time): entry.step
                 for entry in explanation.running if isinstance(entry.step.run, LiveBuildRun)}
    walk: list[RunRow] = []
    swaps: list[SwapRow] = []
    for hold in captured.runs:
        step = by_owner.get(hold.memory.owner)
        metadata = step if step is not None else run_steps.get(hold.memory.owner)
        name = step_text(step, RECORDED_STEP_NAMES) if step is not None else "between steps"
        identity = step_labels(metadata, labels) if metadata is not None else unit_labels.UnitLabels("")
        unit = identity.unit
        detail = unit if metadata is not None else hold.memory.owner
        use = gib(hold.memory.use.bytes) if isinstance(hold.memory.use, gate.AnonymousRunUse) else hold.memory.use
        run = hold.memory.run
        pid = (metadata.run.pid if isinstance(metadata.run, LiveBuildRun) else metadata.pid) if metadata is not None else (
            run.pid if isinstance(run, LiveBuildRun) else gate.PidNotRecorded())
        walk.append({"name": name, "showrunner": identity.showrunner, "unit": unit,
                     "state": "", "pid": pid, "detail": detail, "used": use,
                     "counted": use + gib(hold.bytes) if isinstance(use, float) else use, "reserved": gib(hold.bytes),
                     "started_at": metadata.started_at if metadata is not None else 0,
                     "progress": row_progress(steps_by_owner.get(hold.memory.owner, ())),
                     "queued": True, "keep_text": run_keep_text(hold, steps_by_owner.get(hold.memory.owner, ())), "group": "", "ci": False, "parts": []})
    groups: dict[str, list[RunRow]] = {}
    for entry in explanation.running:
        step = entry.step
        memory = step.measured
        identity = step_labels(step, labels)
        name, detail = step_text(step, RECORDED_STEP_NAMES), identity.unit
        unit, state = detail, ""
        if isinstance(memory, (gate.UnownedSliceMemory, gate.CompilerServiceMemory)):
            service = (isinstance(memory, gate.CompilerServiceMemory)
                       or not isinstance(memory.job_state, NotServiceSlice) or bool(memory.diagnostic))
            if service:
                name = memory.owner if isinstance(memory, gate.UnownedSliceMemory) else "compiler cache"
                activity = service_activity_text(memory, explanation.now)
                state = activity
                unit = memory.owner if isinstance(memory, gate.UnownedSliceMemory) else "sccache.service"
                detail = activity if isinstance(memory, gate.UnownedSliceMemory) else "sccache.service · " + activity
                if isinstance(memory, gate.UnownedSliceMemory):
                    ci_name = "hana CI"
                    description = memory.diagnostic
                else:
                    description = "Memory the compiler cache holds outside the build queue."
            else:
                description = "Memory these processes hold outside the build queue."
            row: RunRow = {"name": name, "showrunner": "", "unit": unit, "state": state, "pid": (step.run.pid if isinstance(step.run, LiveBuildRun) else step.pid), "detail": detail, "used": gib(memory.bytes),
                           "counted": 0.0, "reserved": 0.0, "started_at": step.started_at,
                           "age": queue_view.leftover_age(age(step.started_at, explanation.now)) if step.started_at else "not recorded",
                           "progress": row_progress((step,)),
                           "queued": False, "keep_text": description, "group": "", "ci": False, "parts": []}
            if service and isinstance(memory, gate.UnownedSliceMemory):
                row = build_ci_row(gib(memory.bytes), ci_kept, explanation.now,
                                   Path(memory.cgroup) if memory.cgroup else CI_CGROUP)
                if memory.diagnostic:
                    row["keep_text"] += "\n" + memory.diagnostic
                name, detail = row["name"], row["detail"]
            if not service and isinstance(memory, gate.UnownedSliceMemory) and isinstance(memory.activity, ScopeProcesses) and memory.bytes < GIB // 2:
                program = memory.activity.program
                group = "Codex servers" if program in ("codex", "codex-real", "mesh_runs.py") else "worker background runs"
                row["detail"] = " · ".join(part for part in (detail, program) if part)
                groups.setdefault(group, []).append(row)
            else:
                walk.append(row)
        if isinstance(memory, gate.MeasuredMemory) and memory.swap_bytes:
            swaps.append({"name": name, "detail": detail, "swap": gib(memory.swap_bytes)})
    walk.sort(key=lambda row: row["started_at"])
    for name, members in groups.items():
        amount = sum(row["used"] for row in members if isinstance(row["used"], float))
        walk.append({"name": f"{name} ({len(members)})", "showrunner": "", "unit": "", "state": "", "pid": gate.PidNotRecorded(), "detail": "each under 0.5 GiB",
                     "used": amount, "counted": 0.0, "reserved": 0.0, "started_at": 0,
                     "queued": False, "keep_text": "Memory these processes hold outside the build queue.",
                     "progress": {"kind": "NoProgress", "reason": "group has no admission counter"},
                     "group": name, "ci": False, "parts": [{"who": row["detail"], "pid": row["pid"],
                         "gib": row["used"], "age": row.get("age", "")}
                         for row in sorted(members, key=lambda row: gib(row["used"]), reverse=True)
                         if isinstance(row["used"], float)]})
    machine = host_name()
    for row in walk:
        row["machine"] = machine
    waiters: list[WaitRow] = []
    checks = {row.waiter.pid: row.room for row in explanation.line.steps}
    if isinstance(explanation.head, gate.HeadExplanation):
        checks[explanation.head.waiter.pid] = explanation.head.room
    for waiter in parsed.view.waiting:
        check = checks.get(waiter.pid)
        need = check.need if check is not None else waiter.need
        identity = step_labels(waiter, labels)
        reason = reason_phrase(waiter.reason, parsed.view.waiting)
        if isinstance(waiter.reason, gate.BehindHead):
            reason = "head protection · " + reason
        waiters.append({"name": step_text(waiter, RECORDED_STEP_NAMES), "showrunner": identity.showrunner, "unit": identity.unit, "pid": (waiter.run.pid if isinstance(waiter.run, LiveBuildRun) else waiter.pid), "detail": identity.unit,
                        "need": gib(waiter.need), "since": waiter.since,
                        "reason": reason,
                        "fits": need <= captured.signed_room})
    cloud: list[str] = []
    cloud_heading = ""
    cloud_slots_used = 0
    cloud_slots = 0
    if isinstance(parsed, wire.CloudQueueRead):
        cloud_slots = parsed.cloud_slots
        model = queue_view.QueueModel(
            parsed, labels, gate.NoWaiter(), head_facts.PaceUnavailable(), ZoneInfo("UTC"),
            queue_view.HostLoadUnavailable(), gate.NoWaiter(), names=RECORDED_STEP_NAMES)
        rows = queue_view.cloud_rows(model)
        cloud_slots_used = sum(isinstance(row.wait, gate.ReadyForAdmission) for row in rows)
        lines = queue_view.cloud_lines(model, 1_000_000)
        if lines:
            cloud_heading = plain(lines[:1])
            cloud = [plain([line]).strip() for line in lines[1:]]
    pressure = explanation.decided_from.pressure
    policy = explanation.policy.pressure
    pressure_values = read_stalls()
    pressure_label = "none"
    if isinstance(pressure, gate.MemoryPressure):
        pressure_label = gate.pressure_text(pressure)
    elif isinstance(pressure, gate.PressureUnavailable):
        pressure_label = "unavailable: " + pressure.reason
    return {
        "ok": True, "read_at": explanation.now, "total": total, "available": gib(captured.available),
        "cap": cap, "floor": safety, "cap_text": room_text(captured),
        "shortfall": gib(captured.shortfall), "used": used, "reserved": reserved,
        "taken": gib(captured.cap_use) + reserved + ci_kept, "room": gib(captured.room), "room_source": "free", "limit": "room and head protection",
        "other": max(0.0, total - gib(captured.available) - gib(captured.cap_use)),
        "ci_name": ci_name, "ci_ram": ci_ram, "ci_kept": ci_kept,
        "shown_ci_kept": max(0.0, min(ci_kept, gib(captured.available) - safety - reserved)),
        "shown_safety": min(safety, used + gib(captured.available)),
        "shown_reserved": max(0.0, min(reserved, gib(captured.available) - safety)),
        "waiting": len(waiters), "smallest_need": min((step.need / GIB for step in parsed.view.waiting), default=0.0),
        "pressure": pressure_label,
        "swap_used": gib(host.swap_total - host.swap_free), "swap_total": gib(host.swap_total),
        "walk": walk, "waiters": waiters, "swap_by": swaps, "psi": pressure_values,
        "cloud": cloud, "cloud_heading": cloud_heading,
        "cloud_slots_used": cloud_slots_used, "cloud_slots": cloud_slots,
        "limits": {"some": policy.some_avg10_limit, "full": policy.full_avg10_limit, "swap": policy.swap_used_limit},
        "report": report_reading(configuration.history_days) if isinstance(report, ReportNotRead) else report,
        "swap_rows": configuration.swap_rows, "leftovers": leftovers,
    }


def page_json(payload: MemoryPage | RetainedReading | PageUnavailable) -> str:
    """Preserve named unavailable readings at the JSON boundary."""
    return json.dumps(codec.snapshot_value(payload))


def main(argv: Sequence[str]) -> int:
    args = page_arguments(argv)
    configuration = read_configuration()
    publish_page(configuration)
    target = configuration.output_folder / "memory.json"
    last_good: MemoryPage | NoGoodReading = NoGoodReading()
    report: ReportSections | charts.IndexUnavailable | ReportNotRead = ReportNotRead.VALUE
    next_report_at = 0.0
    while True:
        if installed_code_version(args.code_root) != LOADED_ADMISSION_CODE_VERSION:
            print("The installed admission code changed. Restart the feed.", file=sys.stderr)
            return 1
        now = time.monotonic()
        if isinstance(report, ReportNotRead) or now >= next_report_at:
            report = report_reading(configuration.history_days)
            next_report_at = now + configuration.report_refresh_seconds
        try:
            payload: MemoryPage | RetainedReading | PageUnavailable = reading(configuration, report)
            last_good = payload
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            if isinstance(last_good, NoGoodReading):
                payload = {"ok": False, "error": str(error), "read_at": time.time()}
            else:
                payload = {**last_good, "read_error": str(error), "error_at": time.time()}
        write_text_atomic(target, page_json(payload), new_file_mode=0o644)
        if args.once:
            return 0
        time.sleep(configuration.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
