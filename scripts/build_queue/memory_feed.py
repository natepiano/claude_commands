#!/usr/bin/env python3
"""Publish the build memory page and its current queue reading."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import hashlib
import importlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
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

FEED_IMPORT_STARTED_AT_NS = time.time_ns()

from scripts.build_queue import admission_code


# Preserve the schema check across the imports below.
ADMISSION_VERSION_AT_IMPORT = admission_code.admission_code_version(installed.code_root / "scripts")

from scripts.buildlog import charts, mac_idle_alert
from scripts.buildlog.store import host_name
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue.queue_evidence import ci_amount as ci_bytes_text, room_text, keep_aside_text, service_activity_text, step_labels
from scripts.build_queue import admission_client, admission_protocol as wire, head_facts, queue_view
from scripts.build_queue.queue_lines import age, plain, step_text
from scripts.build_queue.queue_view import reason_phrase
from scripts.lib.build_steps import StepNames, read_step_names
from scripts.build_queue.scope_activity import LiveBuildRun, NotServiceSlice, ScopeProcesses
from scripts.build_queue import admission_records
from scripts.lint import memory_admit as gate
from scripts.message import unit_labels
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.review_links import review_link

# These function-level imports also belong to the fixed source list.
FUNCTION_IMPORT_MODULES = (
    "scripts.buildlog.record", "scripts.buildlog.holds", "scripts.build_targets.target_config",
    "scripts.production.production_doc", "scripts.build_hold.override_guard",
    "scripts.production.change_routing", "scripts.mac_test.offload",
)


def feed_code_paths() -> tuple[str, ...]:
    """Freeze the feed modules and startup files after the imports finish."""
    for name in FUNCTION_IMPORT_MODULES:
        _ = importlib.import_module(name)
    source_root = Path(__file__).resolve().parents[2]
    paths = {"scripts/" + relative for relative in admission_code.ADMISSION_CODE_PATHS}
    paths.update(("scripts/lib/package_root.py", "scripts/build_queue/memory_live.html", "config/memory_feed.conf"))
    for module in tuple(sys.modules.values()):
        source = getattr(module, "__file__", None)
        if isinstance(source, str) and source.endswith(".py"):
            path = Path(source).resolve()
            if path.is_relative_to(source_root):
                paths.add(path.relative_to(source_root).as_posix())
    return tuple(sorted(paths))


FEED_CODE_PATHS = feed_code_paths()


def installed_feed_code_version(code_root: Path) -> str:
    """Hash the fixed feed sources and startup files in the installed copy."""
    digest = hashlib.sha256()
    for relative in FEED_CODE_PATHS:
        try:
            content = (code_root / relative).read_bytes()
        except OSError:
            return ""
        digest.update(relative.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def loaded_feed_code_version(code_root: Path) -> str:
    """Reject an installation that changes while the feed imports its modules."""
    if admission_code.admission_code_version(code_root / "scripts") != ADMISSION_VERSION_AT_IMPORT:
        return ""
    version = installed_feed_code_version(code_root)
    try:
        if any((code_root / relative).stat().st_mtime_ns > FEED_IMPORT_STARTED_AT_NS for relative in FEED_CODE_PATHS):
            return ""
    except OSError:
        return ""
    return version


LOADED_FEED_CODE_VERSION = loaded_feed_code_version(installed.code_root)


GIB = 1024 ** 3
CI_CGROUP = Path("/sys/fs/cgroup/hana.slice/hana-ci.slice")
PROC = Path("/proc")
CGROUP = CI_CGROUP.parents[1]
SWAP_NAMES = {
    "agents": "agent sessions ({count})", "apps": "other apps: {app}",
    "desktop": "desktop session", "system": "system services", "ci": "hana CI",
    "ungrouped": "not in a group", "unattributed": "not attributed", "total": "Total",
    "smaller": "{count} smaller groups", "between": "between steps",
    "compiler": "compiler cache",
    "unavailable": "Swap is unreadable: {path}",
    "difference": "Host swap minus the named cgroup readings.",
    "host": "SwapTotal minus SwapFree from {path}",
}
SWAP_OWNER_SLICES = {"app-agents.slice": "agents", "session.slice": "desktop",
                     "system.slice": "system", "hana.slice": "ci", "hana-ci.slice": "ci"}


@dataclass(frozen=True)
class FeedConfiguration:
    output_folder: Path
    interval_seconds: float
    history_days: int
    swap_rows: int
    report_refresh_seconds: int


def configured_output_folder(settings: conf.ConfigRead) -> Path:
    """Expand the configured page folder."""
    return Path(os.path.expandvars(settings.text("output_folder"))).expanduser()


def read_configuration(settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> FeedConfiguration:
    """Read the page settings from one configuration."""
    if isinstance(settings, conf.UseConfiguredValue):
        settings = conf.read("memory_feed")
    return FeedConfiguration(configured_output_folder(settings),
                             settings.number("interval_seconds"), settings.whole("history_days"),
                             settings.whole("swap_rows"), settings.whole("report_refresh_seconds"))


class PageArguments(CodeRootArguments):
    once: bool = False
    command: Literal["feed", "link"] = "feed"


def page_arguments(argv: Sequence[str]) -> PageArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--once", action="store_true", help="Write one reading and exit.")
    _ = parser.add_argument("--code-root", type=Path, default=PageArguments.code_root,
                            help="Read the installed admission code from this repository.")
    commands = parser.add_subparsers(dest="command")
    _ = commands.add_parser("link", help="Print the build memory page link.")
    parser.set_defaults(command="feed")
    return parser.parse_args(argv, namespace=PageArguments())


def page_link() -> int:
    """Print the configured page URL without publishing a reading."""
    try:
        folder = configured_output_folder(conf.read("memory_feed")).resolve()
        url = review_link.snapshot_url(review_link.review_root().resolve(), folder / "memory_live.html")
    except review_link.SNAPSHOT_ERRORS as error:
        print(f"Build page link: {error}")
        return 1
    print(url)
    return 0


def publish_page(configuration: FeedConfiguration) -> None:
    """Publish the repository page with the configured refresh interval."""
    source = Path(__file__).with_name("memory_live.html").read_text(encoding="utf-8")
    page = source.replace("__MEMORY_FEED_INTERVAL_SECONDS__", json.dumps(configuration.interval_seconds))
    write_text_atomic(configuration.output_folder / "memory_live.html", page, new_file_mode=0o644)


def gib(value: object) -> float:
    return float(value) / GIB if isinstance(value, (int, float)) else 0.0


class GroupPart(TypedDict):
    who: str
    pid: int | admission_records.PidNotRecorded
    gib: float
    age: str


class LeftoverProcess(TypedDict):
    pid: int | admission_records.PidNotRecorded
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
    pid: int | admission_records.PidNotRecorded


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


class RowQueueNotes(TypedDict):
    head_text: NotRequired[str]
    priority_text: NotRequired[str]


class RunRow(RowQueueNotes):
    machine: NotRequired[str]
    vcpus: NotRequired[int | queue_view.UnknownCpuCount]
    age: NotRequired[str]
    between_steps: NotRequired[bool]
    name: str
    showrunner: str
    unit: str
    progress: RowProgress | NoProgress
    state: str
    pid: int | admission_records.PidNotRecorded
    detail: str
    used: float | admission_records.RunUseUnavailable
    counted: float | admission_records.RunUseUnavailable
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
    bytes: int
    kind: Literal["owner", "ungrouped", "unattributed", "smaller", "total"]


@dataclass(frozen=True)
class SwapUnavailable:
    reason: str


@dataclass(frozen=True)
class SwapReading:
    used: int
    total: int
    rows: list[SwapRow]
    files_read: int


class WaitRow(RowQueueNotes):
    name: str
    showrunner: str
    unit: str
    pid: int
    detail: str
    need: float
    since: float
    reason: str
    fits: bool


class CloudWorkRow(RowQueueNotes):
    kind: Literal["cloud"]
    work_kind: NotRequired[Literal["started on the Mac"]]
    name: str
    showrunner: str
    unit: str
    pid: int | admission_records.PidNotRecorded
    detail: str
    machine: Literal["aws", "mac", ""]
    vcpus: int | queue_view.UnknownCpuCount
    state: str
    status: str
    progress: RowProgress | NoProgress
    stale: bool
    late_age: str


class CloudRunRow(CloudWorkRow):
    started_at: float


class CloudWaitRow(CloudWorkRow):
    since: float
    reason: str


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
    cap: float | admission_records.CapUnavailable
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
    shown_safety: float
    waiting: int
    smallest_need: float
    pressure: str
    swap_used: float
    swap_total: float
    walk: list[RunRow | CloudRunRow]
    waiters: list[WaitRow | CloudWaitRow]
    swap_by: list[SwapRow]
    head_text: str
    priority_text: str
    unmatched_priority_text: str
    mac_idle_alert: str
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
                "pid": listeners[0] if listeners else admission_records.PidNotRecorded()}
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
    return ci_bytes_text(round(value * admission_records.GIB))


def build_ci_row(used: float, now: float,
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
    idle = [runner for runner in runners if runner["state"] == "idle" and isinstance(runner["pid"], int)]
    pid = busy[0]["pid"] if busy else idle[0]["pid"] if idle else admission_records.PidNotRecorded()
    return {"name": "hana CI", "showrunner": "", "unit": cgroup.name, "state": detail if not busy else "",
            "pid": pid, "detail": detail, "used": used,
            "counted": 0.0, "reserved": 0.0, "started_at": busy[0]["started_at"] if busy else 0,
            "queued": False, "keep_text": "\n".join(lines), "group": "", "ci": True, "parts": [],
            "ci_runners": runners, "progress": {"kind": "NoProgress", "reason": "no admission counter"}}


def row_progress(steps: Sequence[admission_records.RunningStep]) -> RowProgress | NoProgress:
    """Use the newest step counter from the admission reading."""
    reason = "no admission counter"
    phase: Literal["building", "testing"]
    unit: Literal["modules", "units", "tests"]
    for step in sorted(steps, key=lambda step: step.started_at, reverse=True):
        counter = queue_view.step_counter(step)
        if isinstance(counter, progress.NoCounter):
            reason = counter.reason
            continue
        if counter.phase == "modules" or counter.phase.startswith("suites"):
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


def mac_work_row(name: str, unit: str, pid: int | admission_records.PidNotRecorded, started_at: float,
                 work_kind: Literal["started on the Mac"],
                 counter: RowProgress | NoProgress, *, showrunner: str = unit_labels.OWNER_NOT_RECORDED,
                 status: str = "", detail: str = "") -> CloudRunRow:
    """Show Mac work through the same row as AWS without local memory fields."""
    progress_text = (f"{counter['done']} of {counter['total']} {counter['unit']}"
                     if counter["kind"] == "RowProgress" else "Progress unavailable: " + counter["reason"])
    return {"kind": "cloud", "work_kind": work_kind, "name": name, "showrunner": showrunner,
        "unit": unit or unit_labels.OWNER_NOT_RECORDED, "pid": pid, "detail": detail or unit,
        "machine": "mac", "vcpus": queue_view.UnknownCpuCount(),
        "state": "running", "status": status or f"{work_kind} · {progress_text}", "progress": counter,
        "stale": False, "late_age": "", "started_at": started_at}


def cloud_row_progress(counter: progress.ProgressCounter | progress.NoCounter) -> RowProgress | NoProgress:
    if isinstance(counter, progress.NoCounter):
        return {"kind": "NoProgress", "reason": counter.reason}
    return {"kind": "RowProgress", "phase": "testing", "done": counter.done,
            "total": counter.total, "percent": counter.percent, "unit": "modules"}


def run_keep_text(hold: admission_records.RunKeepAside, steps: Sequence[admission_records.RunningStep]) -> str:
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


def swap_row(name: str, detail: str, amount: int,
             kind: Literal["owner", "ungrouped", "unattributed", "smaller", "total"] = "owner") -> SwapRow:
    return {"name": name, "detail": detail, "swap": gib(amount), "bytes": amount, "kind": kind}


class SwapReadingPass:
    """Read each selected cgroup counter once for one host reading."""

    def __init__(self, owners: Mapping[Path, tuple[str, str]]) -> None:
        self.owners: Mapping[Path, tuple[str, str]] = owners
        self.owner_ancestors: set[Path] = {parent for owner in owners for parent in owner.parents}
        self.counters: dict[Path, int | SwapUnavailable] = {}
        self.rows: dict[str, SwapRow] = {}
        self.agent_count: int = 0

    def counter(self, path: Path) -> int | SwapUnavailable:
        if path not in self.counters:
            try:
                self.counters[path] = int((path / "memory.swap.current").read_text())
            except (OSError, ValueError):
                self.counters[path] = SwapUnavailable(SWAP_NAMES["unavailable"].format(path=path / "memory.swap.current"))
        return self.counters[path]

    def children(self, path: Path) -> list[Path]:
        try:
            with os.scandir(path) as entries:
                return sorted(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))
        except OSError:
            return []

    def add(self, key: str, name: str, path: str, amount: int,
            kind: Literal["owner", "ungrouped"] = "owner") -> None:
        if not amount:
            return
        if key in self.rows:
            row = self.rows[key]
            row["bytes"] += amount
            row["swap"] = gib(row["bytes"])
            row["detail"] += "; " + path
        else:
            self.rows[key] = swap_row(name, path, amount, kind)

    def visit(self, path: Path) -> int:
        counter = self.counter(path)
        nested_owner = path in self.owner_ancestors
        category = SWAP_OWNER_SLICES.get(path.name, "")
        if path.name.startswith("session-") and path.name.endswith(".scope"):
            category = "desktop"
        leaf = path in self.owners or bool(category) or path.parent.name in ("app.slice", "background.slice")
        children = self.children(path) if not leaf or nested_owner or category == "agents" else []
        charged = 0
        if category == "agents":
            self.agent_count += sum(child.name.endswith(".scope") for child in children)
        if path in self.owners and not nested_owner:
            name, detail = self.owners[path]
            if isinstance(counter, int):
                self.add(str(path), name, detail + " · " + str(path.relative_to(CGROUP)), counter)
        elif category and not nested_owner:
            if isinstance(counter, int):
                self.add(category, SWAP_NAMES[category], str(path.relative_to(CGROUP)), counter)
        elif path.parent.name in ("app.slice", "background.slice") and not nested_owner:
            app = path.name.removeprefix("app-").removesuffix(".scope").removesuffix(".service")
            if isinstance(counter, int):
                self.add(str(path), SWAP_NAMES["apps"].format(app=app), str(path.relative_to(CGROUP)), counter)
        elif children:
            charged = sum(self.visit(child) for child in children)
            if isinstance(counter, int):
                if path in self.owners:
                    name, detail = self.owners[path]
                    self.add(str(path), name, detail, counter - charged)
                elif category:
                    self.add(category, SWAP_NAMES[category], str(path.relative_to(CGROUP)), counter - charged)
                else:
                    self.add("ungrouped", SWAP_NAMES["ungrouped"], str(path.relative_to(CGROUP)),
                             counter - charged, "ungrouped")
        elif isinstance(counter, int):
            self.add(str(path), path.name.removesuffix(".scope").removesuffix(".service").removesuffix(".slice"),
                     str(path.relative_to(CGROUP)), counter)
        return counter if isinstance(counter, int) else charged


def read_swap(steps: Sequence[admission_records.RunningStep], labels: Mapping[str, unit_labels.UnitLabels],
              row_limit: int, names: StepNames) -> SwapReading | SwapUnavailable:
    """Account for host swap with disjoint cgroup owners and an explicit difference."""
    owners: dict[Path, tuple[str, str]] = {}
    for step in steps:
        memory = step.measured
        if not isinstance(memory, admission_records.MeasuredMemory):
            continue
        source = memory.anonymous_source
        path: Path
        if isinstance(source, admission_records.ScopeSource):
            path = source.directory
        elif isinstance(memory, admission_records.UnownedSliceMemory) and memory.cgroup:
            path = Path(memory.cgroup)
        else:
            continue
        if path.is_relative_to(CGROUP) and path.name not in SWAP_OWNER_SLICES:
            identity = step_labels(step, labels)
            name, detail = step_text(step, names, labels=labels), identity.session
            if step.step == SWAP_NAMES["between"]:
                name, detail = identity.session or str(path.name), SWAP_NAMES["between"]
            elif isinstance(memory, admission_records.UnownedSliceMemory):
                name = identity.session or memory.owner
            elif isinstance(memory, admission_records.CompilerServiceMemory):
                name = SWAP_NAMES["compiler"]
            _ = owners.setdefault(path, (name, detail))
    reading = SwapReadingPass(owners)
    for child in reading.children(CGROUP):
        _ = reading.visit(child)
    path = PROC / "meminfo"
    try:
        values = {fields[0].removesuffix(":"): int(fields[1]) * 1024
                  for line in path.read_text().splitlines() if (fields := line.split()) and fields[0] in ("SwapTotal:", "SwapFree:")}
        total = values["SwapTotal"]
        used = total - values["SwapFree"]
    except (OSError, ValueError, KeyError, IndexError):
        return SwapUnavailable(SWAP_NAMES["unavailable"].format(path=path))
    if "agents" in reading.rows:
        reading.rows["agents"]["name"] = SWAP_NAMES["agents"].format(count=reading.agent_count)
    rows = sorted((row for row in reading.rows.values() if row["kind"] == "owner"),
                  key=lambda row: row["bytes"], reverse=True)
    main_owners = [reading.rows[key] for key in ("agents", "desktop", "system", "ci") if key in reading.rows]
    other_owners = [row for row in rows if row not in main_owners]
    count = max(0, row_limit - len(main_owners))
    visible = sorted(main_owners + other_owners[:count], key=lambda row: row["bytes"], reverse=True)
    smaller = other_owners[count:]
    if smaller:
        visible.append(swap_row(SWAP_NAMES["smaller"].format(count=len(smaller)),
            "; ".join(f"{row['name']}: {row['bytes']} B" for row in smaller),
            sum(row["bytes"] for row in smaller), "smaller"))
    visible.extend(row for row in reading.rows.values() if row["kind"] == "ungrouped")
    attributed = sum(row["bytes"] for row in visible)
    visible.append(swap_row(SWAP_NAMES["unattributed"], SWAP_NAMES["difference"], used - attributed, "unattributed"))
    visible.append(swap_row(SWAP_NAMES["total"], SWAP_NAMES["host"].format(path=path), used, "total"))
    return SwapReading(used, total, visible, len(reading.counters) + 1)


def swap_text(rows: Sequence[SwapRow]) -> str:
    """Show the same swap rows and Total as the page."""
    return "\n".join(f"{row['name']}: {row['bytes']} B ({row['swap']:.6f} GiB) · {row['detail']}" for row in rows)


def reading(configuration: FeedConfiguration | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
            report: ReportSections | charts.IndexUnavailable | ReportNotRead = ReportNotRead.VALUE) -> MemoryPage:
    if isinstance(configuration, conf.UseConfiguredValue):
        configuration = read_configuration()
    names = read_step_names()
    parsed = admission_client.view()
    explanation = parsed.explanation
    captured = explanation.room
    host = explanation.decided_from.host
    if not isinstance(captured, admission_records.MemoryRoom) or not isinstance(host, admission_records.HostMemory):
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
    ci_ram, ci_name = gib(captured.ci_use), "hana CI"
    by_owner: dict[str, admission_records.RunningStep] = {}
    steps_by_owner: dict[str, list[admission_records.RunningStep]] = {}
    reserved_steps = (entry for entry in explanation.running if gate.running_has_reservation(entry.step))
    for reservation, entry in zip(explanation.decided_from.reservations, reserved_steps, strict=True):
        owner = gate.reservation_owner(reservation, entry.step)
        by_owner[owner] = entry.step
        steps_by_owner.setdefault(owner, []).append(entry.step)
    run_steps = {admission_records.run_owner(entry.step.run.pid, entry.step.run.start_time): entry.step
                 for entry in explanation.running if isinstance(entry.step.run, LiveBuildRun)}
    walk: list[RunRow] = []
    for hold in captured.runs:
        step = by_owner.get(hold.memory.owner)
        metadata = step if step is not None else run_steps.get(hold.memory.owner)
        identity = step_labels(metadata, labels) if metadata is not None else unit_labels.UnitLabels("")
        unit = identity.unit
        name = step_text(step, names, labels=labels) if step is not None else identity.session or hold.memory.owner
        detail = unit if step is not None else SWAP_NAMES["between"]
        use = gib(hold.memory.use.bytes) if isinstance(hold.memory.use, admission_records.AnonymousRunUse) else hold.memory.use
        run = hold.memory.run
        pid = (metadata.run.pid if isinstance(metadata.run, LiveBuildRun) else metadata.pid) if metadata is not None else (
            run.pid if isinstance(run, LiveBuildRun) else admission_records.PidNotRecorded())
        walk.append({"name": name, "showrunner": identity.owner_text, "unit": unit, "between_steps": step is None,
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
        name, detail = step_text(step, names, labels=labels), identity.unit
        unit, state = detail, ""
        if isinstance(memory, (admission_records.UnownedSliceMemory, admission_records.CompilerServiceMemory)):
            service = (isinstance(memory, admission_records.CompilerServiceMemory)
                       or not isinstance(memory.job_state, NotServiceSlice) or bool(memory.diagnostic))
            if service:
                name = memory.owner if isinstance(memory, admission_records.UnownedSliceMemory) else "compiler cache"
                activity = service_activity_text(memory, explanation.now)
                state = activity
                unit = memory.owner if isinstance(memory, admission_records.UnownedSliceMemory) else "sccache.service"
                detail = activity if isinstance(memory, admission_records.UnownedSliceMemory) else "sccache.service · " + activity
                if isinstance(memory, admission_records.UnownedSliceMemory):
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
            if service and isinstance(memory, admission_records.UnownedSliceMemory):
                row = build_ci_row(gib(memory.bytes), explanation.now,
                                   Path(memory.cgroup) if memory.cgroup else CI_CGROUP)
                if memory.diagnostic:
                    row["keep_text"] += "\n" + memory.diagnostic
                name, detail = row["name"], row["detail"]
            if not service and isinstance(memory, admission_records.UnownedSliceMemory) and isinstance(memory.activity, ScopeProcesses) and memory.bytes < GIB // 2:
                program = memory.activity.program
                group = "Codex servers" if program in ("codex", "codex-real") else "unowned processes"
                row["detail"] = " · ".join(part for part in (detail, program) if part)
                groups.setdefault(group, []).append(row)
            else:
                walk.append(row)
    walk.sort(key=lambda row: row["started_at"])
    for name, members in groups.items():
        amount = sum(row["used"] for row in members if isinstance(row["used"], float))
        walk.append({"name": f"{name} ({len(members)})", "showrunner": "", "unit": "", "state": "", "pid": admission_records.PidNotRecorded(), "detail": "each under 0.5 GiB",
                     "used": amount, "counted": 0.0, "reserved": 0.0, "started_at": 0,
                     "queued": False, "keep_text": "Memory these processes hold outside the build queue.",
                     "progress": {"kind": "NoProgress", "reason": "group has no admission counter"},
                     "group": name, "ci": False, "parts": [{"who": row["detail"], "pid": row["pid"],
                         "gib": row["used"], "age": row.get("age", "")}
                         for row in sorted(members, key=lambda row: gib(row["used"]), reverse=True)
                         if isinstance(row["used"], float)]})
    machine = host_name()
    vcpus = os.cpu_count()
    for row in walk:
        row["machine"] = machine
        row["vcpus"] = vcpus if vcpus is not None else queue_view.UnknownCpuCount()
    waiters: list[WaitRow] = []
    checks = {row.waiter.pid: row.room for row in explanation.line.steps}
    if isinstance(explanation.head, admission_records.HeadExplanation):
        checks[explanation.head.waiter.pid] = explanation.head.room
    for waiter in parsed.view.waiting:
        check = checks.get(waiter.pid)
        need = check.need if check is not None else waiter.need
        identity = step_labels(waiter, labels)
        reason = reason_phrase(waiter.reason, parsed.view.waiting)
        if isinstance(waiter.reason, admission_records.BehindHead):
            reason = "head protection · " + reason
        waiters.append({"name": step_text(waiter, names, labels=labels), "showrunner": identity.owner_text, "unit": identity.unit, "pid": (waiter.run.pid if isinstance(waiter.run, LiveBuildRun) else waiter.pid), "detail": identity.unit,
                        "need": gib(waiter.need), "since": waiter.since,
                        "reason": reason,
                        "fits": need <= captured.signed_room})
    work_rows: list[RunRow | CloudRunRow] = list(walk)
    mac_reading = queue_view.read_mac_builds()
    mac_status = queue_view.mac_reading_status(mac_reading, explanation.now)
    if mac_status:
        work_rows.append(mac_work_row("Mac builds", "", admission_records.PidNotRecorded(), 0,
            "started on the Mac", {"kind": "NoProgress", "reason": mac_status}, status=mac_status))
    else:
        work_rows.extend(mac_work_row(Path(build.folder).name, unit_labels.OWNER_NOT_RECORDED, build.pid, build.started_at,
            "started on the Mac", {"kind": "NoProgress", "reason": "Mac process has no readable counter"},
            status=queue_view.mac_build_status(mac_reading, explanation.now), detail=build.folder)
            for build in mac_reading.builds)
    work_rows.sort(key=lambda row: row["started_at"])
    waiting_rows: list[WaitRow | CloudWaitRow] = list(waiters)
    model = queue_view.QueueModel(
        parsed, labels, admission_records.NoWaiter(), head_facts.PaceUnavailable(), ZoneInfo("UTC"),
        queue_view.HostLoadUnavailable(), admission_records.NoWaiter(), names=names)
    cloud_slots_used = 0
    cloud_slots = 0
    if isinstance(parsed, wire.CloudQueueRead):
        cloud_slots = parsed.cloud_slots
        rows = queue_view.cloud_rows(model)
        cloud_slots_used = sum(isinstance(row.wait, admission_records.ReadyForAdmission) for row in rows)
        interval = queue_view.cloud_push_interval()
        cloud_waiters: dict[wire.Owner, CloudWaitRow] = {}
        for cloud in rows:
            information = queue_view.cloud_work(cloud, explanation.now, parsed.cloud_gate_stale_pushes, interval)
            identity = queue_view.cloud_step_labels(cloud, model)
            values: CloudWorkRow = {"kind": "cloud", "name": model.names.shown(cloud.identity.step),
                "showrunner": identity.owner_text, "unit": identity.unit, "pid": cloud.owner.pid,
                "detail": identity.unit, "machine": "" if isinstance(cloud.wait, wire.CloudSlotWait) else "aws",
                "vcpus": information.vcpus,
                "state": information.state, "status": plain([queue_view.cloud_page_status(cloud, information)]),
                "progress": cloud_row_progress(cloud.counter),
                "stale": isinstance(information.update, queue_view.CloudUpdateLate)
                         and not isinstance(cloud.wait, wire.CloudSlotWait),
                "late_age": queue_view.cloud_update_age(information.update.seconds)
                    if isinstance(information.update, queue_view.CloudUpdateLate)
                    and not isinstance(cloud.wait, wire.CloudSlotWait) else ""}
            if isinstance(cloud.wait, wire.CloudSlotWait):
                cloud_waiters[cloud.owner] = {**values, "since": cloud.since, "reason": cloud.wait.text}
            else:
                work_rows.append({**values, "started_at": cloud.started_at or cloud.since})
        work_rows.sort(key=lambda row: row["started_at"])
        waiting_rows = [cloud_waiters[entry.owner] if isinstance(entry, wire.CloudRow) else waiters[entry.position - 1]
                        for entry in queue_view.waiting_builds(model)]
    unmatched_priority_text = ""
    if isinstance(explanation.priority, admission_records.QueuePriority):
        priorities = {entry["unit"]: f"queue priority: {entry['unit']} ({entry['why']})"
                      for entry in explanation.priority.entries}
        matched_units: set[str] = set()
        for work_row in (*work_rows, *waiting_rows):
            if work_row["unit"] in priorities:
                work_row["priority_text"] = priorities[work_row["unit"]]
                matched_units.add(work_row["unit"])
        unmatched_priority_text = "\n".join(text for unit, text in priorities.items() if unit not in matched_units)
    if waiting_rows:
        head_pid = waiting_rows[0]["pid"]
        if isinstance(explanation.head, admission_records.HeadExplanation):
            head = explanation.head.waiter
            head_pid = head.run.pid if isinstance(head.run, LiveBuildRun) else head.pid
        for waiting_row in waiting_rows:
            if waiting_row["pid"] == head_pid:
                waiting_row["head_text"] = queue_view.head_text(model)
                break
    pressure = explanation.decided_from.pressure
    policy = explanation.policy.pressure
    swap = read_swap(parsed.view.running, labels, configuration.swap_rows, names)
    if isinstance(swap, SwapUnavailable):
        raise RuntimeError(swap.reason)
    pressure_values = read_stalls()
    pressure_label = "none"
    if isinstance(pressure, admission_records.MemoryPressure):
        pressure_label = admission_records.pressure_text(pressure)
    elif isinstance(pressure, admission_records.PressureUnavailable):
        pressure_label = "unavailable: " + pressure.reason
    return {
        "ok": True, "read_at": explanation.now, "total": total, "available": gib(captured.available),
        "cap": cap, "floor": safety, "cap_text": room_text(captured),
        "shortfall": gib(captured.shortfall), "used": used, "reserved": reserved,
        "taken": gib(captured.cap_use) + reserved, "room": gib(captured.room), "room_source": "free", "limit": "room and head protection",
        "other": max(0.0, total - gib(captured.available) - gib(captured.cap_use)),
        "ci_name": ci_name, "ci_ram": ci_ram,
        "shown_safety": min(safety, used + gib(captured.available)),
        "shown_reserved": max(0.0, min(reserved, gib(captured.available) - safety)),
        "waiting": len(waiting_rows), "smallest_need": min((step.need / GIB for step in parsed.view.waiting), default=0.0),
        "pressure": pressure_label,
        "swap_used": gib(swap.used), "swap_total": gib(swap.total),
        "walk": work_rows, "waiters": waiting_rows, "swap_by": swap.rows, "psi": pressure_values,
        "head_text": queue_view.head_text(model),
        "priority_text": head_facts.priority_text(explanation.priority),
        "unmatched_priority_text": unmatched_priority_text,
        "mac_idle_alert": mac_idle_alert.alert_line(mac_idle_alert.state_dir() / "alert.json", time.time(), user_zone()) or "",
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
    if args.command == "link":
        return page_link()
    configuration = read_configuration()
    publish_page(configuration)
    target = configuration.output_folder / "memory.json"
    last_good: MemoryPage | NoGoodReading = NoGoodReading()
    report: ReportSections | charts.IndexUnavailable | ReportNotRead = ReportNotRead.VALUE
    next_report_at = 0.0
    next_mac_at = 0.0
    while True:
        if installed_feed_code_version(args.code_root) != LOADED_FEED_CODE_VERSION:
            print("The installed feed code changed. Restart the feed.", file=sys.stderr)
            return 1
        now = time.monotonic()
        if isinstance(report, ReportNotRead) or now >= next_report_at:
            report = report_reading(configuration.history_days)
            next_report_at = now + configuration.report_refresh_seconds
        try:
            if now >= next_mac_at:
                queue_view.refresh_mac_builds()
                next_mac_at = now + queue_view.mac_activity_refresh_seconds()
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
