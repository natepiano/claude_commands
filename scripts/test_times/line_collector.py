"""Collect repository lines and count Python children with standard library hooks."""
from __future__ import annotations

import atexit
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
import inspect
import importlib.util
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
SHARED_TEST = "__shared__"
SHARED_THREAD = "__shared_thread__"
POPEN_CLASS = subprocess.Popen
POPEN_INIT = subprocess.Popen.__init__.__code__


class ChildLaunch(TypedDict):
    launch_id: str
    program: str
    kind: LaunchKind
    started: float
    owner: str


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
    report_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    causes: set[str] = field(default_factory=set)
    initial_threads: set[threading.Thread] = field(default_factory=set)


@dataclass(frozen=True)
class PendingPopen:
    """Keep audit data until the Popen constructor returns."""

    launch: ChildLaunch
    process: subprocess.Popen[bytes] | subprocess.Popen[str]


class ChildReport(TypedDict):
    test_id: str
    pid: int
    lines: list[tuple[str, int]]
    forks: int
    fork_parent_pid: int
    launches: list[ChildLaunch]
    exited: float
    launch_id: str
    ends: dict[str, float]
    collector_id: str
    completed: bool
    exec_launch_id: str
    causes: list[str]


@dataclass(frozen=True)
class NoActiveTest:
    """The runner is between tests."""


@dataclass(frozen=True)
class NoChildProcess:
    """This launch does not expose a Popen object."""


@dataclass(frozen=True)
class NoChildReports:
    """The runner writes no child reports."""


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


def source_classifier() -> Callable[[Path], bool]:
    """Load the shared classifier without changing a child's module search path."""
    path = REPOSITORY / "scripts" / "lib" / "code_kind.py"
    spec = importlib.util.spec_from_file_location("_natest_code_kind", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load the source classifier: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Callable[[Path], bool], module.is_test_code)


def after_fork() -> None:
    """Keep the audit owner when a thread forks."""
    if isinstance(_collector, LineCollector):
        collector = _collector
        previous = collector.active
        owner = cast(str, getattr(_popen_arguments, "fork_owner", SHARED_TEST))
        collector.reset_process_state()
        if isinstance(previous, ActiveTest):
            collector.active = ActiveTest(owner, fork_parent_pid=os.getppid(), launch_id=previous.launch_id)
        if isinstance(collector.report_directory, NoChildReports) and os.environ.get("NATEST_COVERAGE_DIR"):
            start_child()
    sys.monitoring.restart_events()


def stack_popen() -> subprocess.Popen[bytes] | subprocess.Popen[str] | NoChildProcess:
    """Find the Popen object whose constructor is on the stack."""
    frame = inspect.currentframe()
    while frame is not None:
        if frame.f_code is POPEN_INIT:
            process = frame.f_locals.get("self")
            if isinstance(process, POPEN_CLASS):
                return process
        frame = frame.f_back
    return NoChildProcess()


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
        owner = collector.launch_owner()
        _popen_arguments.fork_owner = owner
        if isinstance(collector.active, ActiveTest) and owner == collector.active.test_id:
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
    if isinstance(environment, MutableMapping):
        variables = cast(MutableMapping[object, object], environment)
        if any(isinstance(key, bytes) for key in variables):
            variables[b"NATEST_COVERAGE_LAUNCH"] = launch_id.encode()
        else:
            variables["NATEST_COVERAGE_LAUNCH"] = launch_id
    elif environment is None:
        os.environ["NATEST_COVERAGE_LAUNCH"] = launch_id
    launch: ChildLaunch = {"launch_id": launch_id, "program": Path(executable).name, "kind": kind,
                           "started": time.monotonic(), "owner": collector.launch_owner()}
    if event == "subprocess.Popen":
        process = stack_popen()
        if isinstance(process, POPEN_CLASS):
            _popen_arguments.pending = PendingPopen(launch, process)
        return
    collector.record_launch(launch)
    if event == "os.exec":
        sys.monitoring.restart_events()
        collector.write_snapshot(exec_launch_id=launch_id)
        collector.pending_exec = launch_id
        if isinstance(collector.tool, MonitoringTool):
            sys.monitoring.set_events(collector.tool.tool_id, sys.monitoring.events.LINE | sys.monitoring.events.RAISE)


class LineCollector:
    """Own one monitoring tool id and one line set for each test."""

    def __init__(self, repository: Path = REPOSITORY, ignored_files: Sequence[Path] = ()) -> None:
        self.repository: Path = repository.resolve()
        self.ignored_files: set[Path] = {Path(__file__).resolve(), *(path.resolve() for path in ignored_files)}
        self.sources: dict[str, str | IgnoredSource] = {}
        self.is_test_code: Callable[[Path], bool] = source_classifier()
        self.tool: MonitoringTool | NoMonitoringTool = NoMonitoringTool()
        self.report_directory: Path | NoChildReports = NoChildReports()
        self.reset_process_state()

    def reset_process_state(self) -> None:
        """Reset all records that belong to one process."""
        self.collector_id: str = uuid.uuid4().hex
        self.runner_thread: threading.Thread = threading.current_thread()
        self.active: ActiveTest | NoActiveTest = NoActiveTest()
        self.completed: dict[str, ActiveTest] = {}
        self.shared_launches: list[ChildLaunch] = []
        self.held: dict[str, subprocess.Popen[bytes] | subprocess.Popen[str]] = {}
        self.ends: dict[str, float] = {}
        self.pending_exec: str = ""
        _popen_arguments.pending = NoChildProcess()

    def launch_owner(self) -> str:
        """Keep launches from other threads separate from tests."""
        if threading.current_thread() is not self.runner_thread:
            return SHARED_THREAD
        return self.active.test_id if isinstance(self.active, ActiveTest) else SHARED_TEST

    def record_launch(self, launch: ChildLaunch) -> None:
        if isinstance(self.active, ActiveTest) and launch["owner"] == self.active.test_id:
            self.active.launches.append(launch)
        else:
            self.shared_launches.append(launch)

    def source(self, filename: str) -> str | IgnoredSource:
        if filename not in self.sources:
            path = Path(filename).resolve()
            if (path.is_relative_to(self.repository / "scripts")
                    and path not in self.ignored_files and not self.is_test_code(path) and path.is_file()):
                self.sources[filename] = path.relative_to(self.repository).as_posix()
            else:
                self.sources[filename] = IgnoredSource()
        return self.sources[filename]

    def discard_launch(self, launch_id: str) -> None:
        """Remove an exec launch that failed."""
        self.shared_launches = [launch for launch in self.shared_launches if launch["launch_id"] != launch_id]
        records = list(self.completed.values())
        if isinstance(self.active, ActiveTest):
            records.append(self.active)
        for recorded in records:
            recorded.launches = [launch for launch in recorded.launches if launch["launch_id"] != launch_id]

    def constructor_return(self, *event: object) -> None:
        if event[0] is not POPEN_INIT:
            return
        pending = cast(PendingPopen | NoChildProcess, getattr(_popen_arguments, "pending", NoChildProcess()))
        if isinstance(pending, PendingPopen) and pending.process is stack_popen():
            self.record_launch(pending.launch)
            self.held[pending.launch["launch_id"]] = pending.process
            _popen_arguments.pending = NoChildProcess()

    def poll_children(self) -> None:
        """Record the first boundary that observes each child's exit."""
        for launch_id, process in list(self.held.items()):
            if process.poll() is not None:
                self.ends[launch_id] = time.monotonic()
                del self.held[launch_id]

    def exec_failed(self, *event: object) -> None:
        """Cancel a pending exec when Python raises its failure."""
        if isinstance(event[2], BaseException):
            if isinstance(self.tool, MonitoringTool):
                sys.monitoring.set_events(self.tool.tool_id, sys.monitoring.events.LINE)
            self.discard_launch(self.pending_exec)
            self.pending_exec = ""
            self.write_snapshot()

    def write_snapshot(self, exec_launch_id: str = "") -> None:
        """Write child evidence without stopping collection."""
        if isinstance(self.active, ActiveTest) and isinstance(self.report_directory, Path):
            self.poll_children()
            self.save_report(self.active, completed=False, exec_launch_id=exec_launch_id)

    def line(self, code: CodeType, line: int) -> object:
        source = self.sources.get(code.co_filename)
        if source is None:
            source = self.source(code.co_filename)
        if not isinstance(source, str):
            return sys.monitoring.DISABLE
        active = self.active
        if isinstance(active, ActiveTest):
            if active.initial_threads:
                if threading.current_thread() in active.initial_threads:
                    active.causes.add("shared thread")
                else:
                    active.lines.add((source, line))
                return None
            active.lines.add((source, line))
            return sys.monitoring.DISABLE
        return None

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
            _ = sys.monitoring.register_callback(self.tool.tool_id, sys.monitoring.events.PY_RETURN, self.constructor_return)
            _ = sys.monitoring.register_callback(self.tool.tool_id, sys.monitoring.events.RAISE, self.exec_failed)
            sys.monitoring.set_local_events(self.tool.tool_id, POPEN_INIT,
                                            sys.monitoring.events.PY_RETURN)
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
            sys.monitoring.set_local_events(self.tool.tool_id, POPEN_INIT, 0)
            _ = sys.monitoring.register_callback(self.tool.tool_id, sys.monitoring.events.PY_RETURN, None)
            _ = sys.monitoring.register_callback(self.tool.tool_id, sys.monitoring.events.RAISE, None)
            _ = sys.monitoring.register_callback(self.tool.tool_id, sys.monitoring.events.LINE, None)
            sys.monitoring.free_tool_id(self.tool.tool_id)
            self.tool = NoMonitoringTool()

    def other_threads(self) -> set[threading.Thread]:
        return set(threading.enumerate()) - {self.runner_thread}

    def start_test(self, test_id: str) -> None:
        self.poll_children()
        sys.monitoring.restart_events()
        self.active = ActiveTest(test_id, initial_threads=self.other_threads())

    def stop_test(self) -> None:
        if isinstance(self.active, ActiveTest):
            self.poll_children()
            self.active.stopped = time.monotonic()
            if self.other_threads() - self.active.initial_threads:
                self.active.causes.add("thread outlived its test")
            self.completed[self.active.test_id] = self.active
        self.active = NoActiveTest()

    def save_report(self, active: ActiveTest, completed: bool, exec_launch_id: str = "") -> None:
        if not isinstance(self.report_directory, Path):
            return
        report: ChildReport = {"test_id": active.test_id, "pid": os.getpid(),
                               "lines": list(active.lines), "forks": active.forks,
                               "fork_parent_pid": active.fork_parent_pid,
                               "launches": active.launches + self.shared_launches,
                               "exited": time.monotonic(), "launch_id": active.launch_id,
                               "ends": dict(self.ends), "collector_id": self.collector_id,
                               "completed": completed, "exec_launch_id": exec_launch_id,
                               "causes": list(active.causes)}
        destination = self.report_directory / f"{self.collector_id}-{active.report_id}.json"
        write_child_report(report, destination)


def finish_child(collector: LineCollector) -> None:
    """Stop collection before writing the child's final report."""
    active = collector.active
    collector.stop_test()
    collector.stop()
    if isinstance(active, ActiveTest):
        collector.save_report(active, completed=True)


def write_child_report(report: ChildReport, destination: Path) -> None:
    """Replace only the snapshot of this collector instance and test."""
    temporary = destination.with_suffix(".tmp")
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
    """Enable reports in a new Python process or a fork child."""
    if isinstance(_collector, LineCollector):
        collector = _collector
    else:
        collector = LineCollector(ignored_files=(Path(__file__).parent / "coverage_child" / "sitecustomize.py",))
        collector.start()
        collector.start_test(os.environ["NATEST_COVERAGE_TEST"])
    collector.report_directory = Path(os.environ["NATEST_COVERAGE_DIR"])
    _ = atexit.register(finish_child, collector)
