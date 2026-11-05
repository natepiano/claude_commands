"""Collect repository lines and count Python children with standard library hooks."""
from __future__ import annotations

import atexit
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import threading
from types import CodeType
from typing import Literal, TypedDict, cast
import uuid


REPOSITORY = Path(__file__).resolve().parents[2]
ExecutedLine = tuple[str, int]
LaunchKind = Literal["python", "leaf", "unverified"]


class ChildLaunch(TypedDict):
    launch_id: str
    program: str
    kind: LaunchKind
    started: float


@dataclass
class ActiveTest:
    test_id: str
    lines: set[ExecutedLine] = field(default_factory=set)
    forks: int = 0
    pid: int = field(default_factory=os.getpid)
    fork_parent_pid: int = 0
    launches: list[ChildLaunch] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    stopped: float = 0.0
    launch_id: str = field(default_factory=lambda: os.environ.get("NATEST_COVERAGE_LAUNCH", ""))
    processes: dict[str, subprocess.Popen[bytes] | subprocess.Popen[str]] = field(default_factory=dict)
    exited_launches: set[str] = field(default_factory=set)
    pending_launches: set[str] = field(default_factory=set)

    def finish_processes(self) -> None:
        """Remove failed starts and record each held process's exit state."""
        failed = {launch_id for launch_id, process in self.processes.items() if getattr(process, "pid", None) is None}
        self.launches = [launch for launch in self.launches if launch["launch_id"] not in failed]
        self.exited_launches = {launch_id for launch_id, process in self.processes.items()
                               if launch_id not in failed and process.poll() is not None}
        self.pending_launches = self.processes.keys() - failed - self.exited_launches
        self.processes.clear()


class ChildReport(TypedDict):
    test_id: str
    pid: int
    lines: list[tuple[str, int]]
    forks: int
    fork_parent_pid: int
    launches: list[ChildLaunch]
    exited: float
    launch_id: str
    exited_launches: list[str]


@dataclass(frozen=True)
class NoActiveTest:
    """The runner is between tests."""


@dataclass(frozen=True)
class NoChildProcess:
    """This launch does not expose a Popen object."""


@dataclass(frozen=True)
class IgnoredSource:
    """The source is outside the measured production files."""


@dataclass(frozen=True)
class MonitoringTool:
    tool_id: int


@dataclass(frozen=True)
class NoMonitoringTool:
    """The collector does not own a monitoring tool id."""


@dataclass(frozen=True)
class NoCollector:
    """No collector runs in this process."""


_collector: LineCollector | NoCollector = NoCollector()
_audit_installed = False
_popen_arguments = threading.local()


def after_fork() -> None:
    """Keep the fork child's events separate from the parent's events."""
    if isinstance(_collector, LineCollector) and isinstance(_collector.active, ActiveTest):
        _collector.active = ActiveTest(_collector.active.test_id, fork_parent_pid=os.getppid())
    sys.monitoring.restart_events()


def command_text(value: object) -> str:
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return value if isinstance(value, str) else ""


def launch_kind(executable: str, cwd: str, environment: object) -> LaunchKind:
    """Trust Python startup or a configured binary, with no shell inference."""
    search_path = os.environ.get("PATH", "")
    if isinstance(environment, Mapping):
        variables = cast(Mapping[object, object], environment)
        search_path = command_text(variables.get("PATH", variables.get(b"PATH", search_path)))
    destination = executable
    if os.sep not in destination:
        destination = shutil.which(destination, path=search_path) or destination
    path = Path(destination)
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    try:
        with path.open("rb") as handle:
            header = handle.read(512)
    except OSError:
        return "unverified"
    if header.startswith(b"#!"):
        try:
            interpreter = shlex.split(os.fsdecode(header.splitlines()[0][2:]))
        except ValueError:
            return "unverified"
        if interpreter and Path(interpreter[0]).name.startswith("python"):
            return "python"
        return "unverified"
    if Path(executable).name.startswith("python") or executable == sys.executable:
        return "python"
    leaves = os.environ.get("NATEST_COVERAGE_LEAF_PROGRAMS", "").split()
    return "leaf" if Path(executable).name in leaves else "unverified"


def child_event(event: str, arguments: tuple[object, ...]) -> None:
    """Count Python starts without replacing the process creation functions."""
    if not isinstance(_collector, LineCollector):
        return
    collector = _collector
    if event in {"os.fork", "os.forkpty"}:
        if isinstance(collector.active, ActiveTest):
            collector.active.forks += 1
    if event not in {"subprocess.Popen", "os.posix_spawn", "os.exec"}:
        return
    if event == "subprocess.Popen":
        _popen_arguments.value = arguments[1]
    elif event == "os.posix_spawn" and arguments[1] is getattr(_popen_arguments, "value", None):
        return
    executable = command_text(arguments[0])
    cwd = command_text(arguments[2]) if event == "subprocess.Popen" else ""
    environment = arguments[3] if event == "subprocess.Popen" else arguments[2]
    kind = launch_kind(executable, cwd, environment)
    launch_id = uuid.uuid4().hex
    process: subprocess.Popen[bytes] | subprocess.Popen[str] | NoChildProcess = NoChildProcess()
    if event == "subprocess.Popen":
        frame = inspect.currentframe()
        while frame is not None:
            owner = frame.f_locals.get("self")
            if isinstance(owner, subprocess.Popen):
                process = owner
                break
            frame = frame.f_back
    if isinstance(environment, MutableMapping):
        variables = cast(MutableMapping[object, object], environment)
        if any(isinstance(key, bytes) for key in variables):
            variables[b"NATEST_COVERAGE_LAUNCH"] = launch_id.encode()
        else:
            variables["NATEST_COVERAGE_LAUNCH"] = launch_id
    elif environment is None:
        os.environ["NATEST_COVERAGE_LAUNCH"] = launch_id
    launch: ChildLaunch = {"launch_id": launch_id, "program": Path(executable).name, "kind": kind,
                           "started": time.monotonic()}
    if isinstance(collector.active, ActiveTest):
        collector.active.launches.append(launch)
        if isinstance(process, subprocess.Popen):
            collector.active.processes[launch_id] = process
    else:
        collector.shared_launches.append(launch)
    if event == "os.exec":
        directory = os.environ.get("NATEST_COVERAGE_DIR")
        if directory and isinstance(collector.active, ActiveTest):
            write_child_report(collector, Path(directory), fork_exec=True)


class LineCollector:
    """Own one monitoring tool id and one line set for each test."""

    def __init__(self, repository: Path = REPOSITORY, ignored_files: Sequence[Path] = ()) -> None:
        self.repository: Path = repository.resolve()
        self.ignored_files: set[Path] = {Path(__file__).resolve(), *(path.resolve() for path in ignored_files)}
        self.sources: dict[str, str | IgnoredSource] = {}
        self.active: ActiveTest | NoActiveTest = NoActiveTest()
        self.completed: dict[str, ActiveTest] = {}
        self.shared_launches: list[ChildLaunch] = []
        self.tool: MonitoringTool | NoMonitoringTool = NoMonitoringTool()

    def source(self, filename: str) -> str | IgnoredSource:
        if filename not in self.sources:
            path = Path(filename).resolve()
            if (path.is_relative_to(self.repository / "scripts")
                    and path not in self.ignored_files and path.is_file()):
                self.sources[filename] = path.relative_to(self.repository).as_posix()
            else:
                self.sources[filename] = IgnoredSource()
        return self.sources[filename]

    def line(self, code: CodeType, line: int) -> object:
        if isinstance(self.active, ActiveTest):
            source = self.source(code.co_filename)
            if isinstance(source, str):
                self.active.lines.add((source, line))
        return sys.monitoring.DISABLE

    def start(self) -> None:
        global _audit_installed, _collector
        for tool_id in range(5, -1, -1):
            if sys.monitoring.get_tool(tool_id) is None:
                sys.monitoring.use_tool_id(tool_id, "coverage_overlap")
                self.tool = MonitoringTool(tool_id)
                break
        if isinstance(self.tool, NoMonitoringTool):
            raise RuntimeError("No monitoring tool id is available")
        try:
            if not _audit_installed:
                sys.addaudithook(child_event)
                if hasattr(os, "register_at_fork"):
                    os.register_at_fork(after_in_child=after_fork)
                _audit_installed = True
            _collector = self
            _ = sys.monitoring.register_callback(self.tool.tool_id, sys.monitoring.events.LINE, self.line)
            sys.monitoring.set_events(self.tool.tool_id, sys.monitoring.events.LINE)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        global _collector
        self.active = NoActiveTest()
        if _collector is self:
            _collector = NoCollector()
        if isinstance(self.tool, MonitoringTool):
            sys.monitoring.set_events(self.tool.tool_id, 0)
            _ = sys.monitoring.register_callback(self.tool.tool_id, sys.monitoring.events.LINE, None)
            sys.monitoring.free_tool_id(self.tool.tool_id)
            self.tool = NoMonitoringTool()

    def start_test(self, test_id: str) -> None:
        self.active = ActiveTest(test_id)
        sys.monitoring.restart_events()

    def stop_test(self) -> None:
        if isinstance(self.active, ActiveTest):
            self.active.stopped = time.monotonic()
            self.active.finish_processes()
            self.completed[self.active.test_id] = self.active
        self.active = NoActiveTest()


def write_child_report(collector: LineCollector, directory: Path, fork_exec: bool = False) -> None:
    """Write a complete report before exposing its final file name."""
    active = collector.active
    collector.stop()
    if not isinstance(active, ActiveTest):
        return
    active.finish_processes()
    report: ChildReport = {"test_id": active.test_id, "pid": os.getpid(),
                           "lines": list(active.lines),
                           "forks": active.forks,
                           "fork_parent_pid": active.fork_parent_pid if fork_exec else 0,
                           "launches": active.launches, "exited": time.monotonic(),
                           "launch_id": active.launch_id, "exited_launches": list(active.exited_launches)}
    name = f"{os.getpid()}-{uuid.uuid4().hex}"
    temporary = directory / f"{name}.tmp"
    destination = directory / f"{name}.json"
    try:
        _ = temporary.write_text(json.dumps(report), encoding="utf-8")
        _ = temporary.replace(destination)
    except OSError:
        # A missing report makes the parent mark this test as unknown.
        temporary.unlink(missing_ok=True)


def read_child_reports(directory: Path) -> list[ChildReport]:
    """Ignore incomplete reports, which leave a shortage in the child count."""
    reports: list[ChildReport] = []
    for path in directory.glob("*.json"):
        try:
            reports.append(cast(ChildReport, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return reports


def start_child() -> None:
    """Start the child collector and write its report at exit."""
    collector = LineCollector(ignored_files=(Path(__file__).parent / "coverage_child" / "sitecustomize.py",))
    collector.start()
    collector.start_test(os.environ["NATEST_COVERAGE_TEST"])
    _ = atexit.register(write_child_report, collector, Path(os.environ["NATEST_COVERAGE_DIR"]))
