#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Run unittest discovery and record each test's time."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import time

_STARTED_AT = time.perf_counter()
startup_cpu_s = 0.0
__unittest = True

import json
import os
import fcntl
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from inspect import currentframe
from pathlib import Path
import resource
import signal
import stat
import sys
import tempfile
from types import FrameType, MethodType, TracebackType
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, TypedDict, cast, final, override
import unittest
import unittest.case as unittest_case

if TYPE_CHECKING:
    from scripts.test_times.gate_record import TestStatus, TestTime


ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]

_OUTCOME_PRIORITY: dict[TestStatus, int] = {
    "pass": 0, "skip": 1, "expected_failure": 2,
    "unexpected_success": 3, "fail": 4, "error": 5,
}


@dataclass(frozen=True)
class OpenTest:
    test_id: str


@dataclass(frozen=True)
class NoOpenTest:
    """No parent test runs during fixture outcomes."""


@dataclass(frozen=True)
class NoConfigReadLog:
    """The isolated project has no config reader to record its reads."""


@final
class RepositoryReadAudit:
    """Record repository listings and file reads in this process."""

    def __init__(self, root: Path, folders: set[str], contents: set[str]) -> None:
        self.roots = tuple(dict.fromkeys((os.path.realpath(root), os.path.abspath(root))))
        self.folders = folders
        self.contents = contents
        self.process_id = os.getpid()
        self.current_process = os.getpid
        self.decode = os.fsdecode
        self.current_directory = os.getcwd
        self.normalize = os.path.normpath
        self.read_link = os.readlink
        self.get_descriptor_path = fcntl.fcntl
        self.descriptor_path_command = cast(int, getattr(fcntl, "F_GETPATH", 50))
        self.platform = sys.platform
        self.current_frame = currentframe
        self.file_status = os.stat
        self.is_regular = stat.S_ISREG
        self.write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL

    def absolute_path(self, path: str, cwd: str) -> str:
        return self.normalize(path if path.startswith("/") else cwd + "/" + path)

    def listed_folder(self, value: object, cwd: str) -> set[str]:
        if value is None:
            value = cwd
        if isinstance(value, int):
            try:
                if self.platform == "darwin":
                    value = self.get_descriptor_path(value, self.descriptor_path_command, bytes(1024)).split(b"\0", 1)[0]
                else:
                    value = self.read_link(f"/proc/self/fd/{value}")
            except (OSError, ValueError):
                return {"/"}
        if not isinstance(value, (str, bytes, os.PathLike)):
            return set()
        absolute = self.absolute_path(self.decode(value), cwd)
        for root in self.roots:
            if absolute == root:
                return {"."}
            if absolute.startswith(root + "/"):
                return {absolute[len(root) + 1:]}
        return set()

    def git_folders(self, arguments: tuple[object, ...]) -> set[str]:
        command = arguments[1]
        if not isinstance(command, (list, tuple)):
            return set()
        values = cast(Sequence[object], command)
        if not values or not all(isinstance(value, (str, bytes, os.PathLike)) for value in values):
            return set()
        argv = [self.decode(cast(str | bytes | os.PathLike[str] | os.PathLike[bytes], value)) for value in values]
        if argv[0].rsplit("/", 1)[-1] != "git":
            return set()
        current_directory = self.current_directory()
        cwd = current_directory if arguments[2] is None else self.decode(
            cast(str | bytes | os.PathLike[str] | os.PathLike[bytes], arguments[2]))
        cwd = self.absolute_path(cwd, current_directory)
        index = 1
        while index < len(argv):
            option = argv[index]
            if option == "-C" and index + 1 < len(argv):
                cwd = self.absolute_path(argv[index + 1], cwd)
                index += 2
            elif option.startswith("-C"):
                cwd = self.absolute_path(option[2:], cwd)
                index += 1
            elif option in {"-c", "--git-dir", "--work-tree", "--namespace"}:
                index += 2
            elif option.startswith("-"):
                index += 1
            else:
                break
        if index == len(argv) or argv[index] != "ls-files" or not self.listed_folder(cwd, current_directory):
            return set()
        pathspecs: list[str] = []
        index += 1
        while index < len(argv):
            option = argv[index]
            if option == "--":
                pathspecs.extend(argv[index + 1:])
                break
            if option in {"-x", "--exclude", "-X", "--exclude-from", "--exclude-per-directory", "--format"}:
                index += 2
                continue
            if not option.startswith("-"):
                pathspecs.append(option)
            index += 1
        if not pathspecs:
            return {"/"}
        folders: set[str] = set()
        for pathspec in pathspecs:
            # Complex Git pathspecs conservatively cover the repository.
            if pathspec.startswith(":") or any(mark in pathspec for mark in ("*", "?", "[")):
                folders.add("/")
                continue
            for folder in self.listed_folder(pathspec, cwd):
                folders.add("/" if folder == "." else folder.rstrip("/") + "/")
        return folders

    def __call__(self, event: str, arguments: tuple[object, ...]) -> None:
        if event not in {"os.scandir", "os.listdir", "subprocess.Popen", "open"}:
            return
        if self.current_process() != self.process_id:
            return
        try:
            target = self.folders
            if event == "open":
                mode, flags = arguments[1:3]
                if (not isinstance(flags, int) or flags & self.write_flags
                        or isinstance(mode, str) and (not mode.startswith("r") or "+" in mode)):
                    return
                paths = self.listed_folder(arguments[0], self.current_directory())
                paths = {path for path in paths if path not in {".", "/"}
                         and not path.startswith("config/") and "__pycache__" not in path.split("/")}
                if not paths:
                    return
                # Use the original spelling for the existing-file check.
                if not self.is_regular(self.file_status(cast(str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
                                                            arguments[0])).st_mode):
                    return
                target = self.contents
            else:
                paths = (self.git_folders(arguments) if event == "subprocess.Popen"
                         else self.listed_folder(arguments[0], self.current_directory()))
            if not paths:
                return
            frame: FrameType | None = self.current_frame()
            try:
                while frame is not None:
                    if frame.f_code.co_filename == "<frozen importlib._bootstrap_external>":
                        return
                    frame = frame.f_back
            finally:
                del frame
            target.update(paths)
        except (OSError, ValueError):
            # Observation cannot change a test's result.
            return


@dataclass(frozen=True)
class TimesOutput:
    path: Path
    config_log: Path | NoConfigReadLog


@dataclass(frozen=True)
class NoTimesOutput:
    """The caller did not request a timing file."""


class TestOutput(Protocol):
    """Supply the output methods used by the unittest result."""

    def write(self, text: str, /) -> object: ...

    def flush(self) -> object: ...

    def writeln(self, text: str | None = None, /) -> None: ...


if TYPE_CHECKING:
    # The runtime class does not support the type stub's stream parameter.
    TimedResultBase = unittest.TextTestResult[TestOutput]
else:
    TimedResultBase = unittest.TextTestResult


class TimedResult(TimedResultBase):
    def __init__(self, stream: TestOutput, descriptions: bool, verbosity: int,
                 *, durations: int | None = None) -> None:
        super().__init__(stream, descriptions, verbosity, durations=durations)
        self.open_test: OpenTest | NoOpenTest = NoOpenTest()
        self.outcomes: dict[str, TestStatus] = {}
        self.fixtures: set[str] = set()
        self.test_labels: dict[str, str] = {}
        self.cpu_times: dict[str, float] = {}
        self.test_cpu_started: float = 0.0

    @override
    def startTest(self, test: unittest.TestCase) -> None:
        self.test_cpu_started = cpu_seconds()
        self.open_test = OpenTest(test.id())
        self.test_labels[test.id()] = test.id().removeprefix(type(test).__module__ + ".")
        super().startTest(test)

    @override
    def stopTest(self, test: unittest.TestCase) -> None:
        self.cpu_times[test.id()] = cpu_seconds() - self.test_cpu_started
        super().stopTest(test)
        self.open_test = NoOpenTest()

    def record_outcome(self, test: unittest.TestCase, status: TestStatus) -> None:
        if isinstance(self.open_test, OpenTest):
            test_id = self.open_test.test_id
        else:
            test_id = test.id()
            self.fixtures.add(test_id)
        previous = self.outcomes.get(test_id, "pass")
        if _OUTCOME_PRIORITY[status] >= _OUTCOME_PRIORITY[previous]:
            self.outcomes[test_id] = status

    @override
    def addSuccess(self, test: unittest.TestCase) -> None:
        self.record_outcome(test, "pass")
        super().addSuccess(test)

    @override
    def addFailure(self, test: unittest.TestCase, err: ExceptionInfo) -> None:
        self.record_outcome(test, "fail")
        super().addFailure(test, err)

    @override
    def addError(self, test: unittest.TestCase, err: ExceptionInfo) -> None:
        self.record_outcome(test, "error")
        super().addError(test, err)

    @override
    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self.record_outcome(test, "skip")
        super().addSkip(test, reason)

    @override
    def addExpectedFailure(self, test: unittest.TestCase, err: ExceptionInfo) -> None:
        self.record_outcome(test, "expected_failure")
        super().addExpectedFailure(test, err)

    @override
    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        self.record_outcome(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    @override
    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase,
                   err: ExceptionInfo | None) -> None:
        if err is not None:
            exception_type = err[0]
            status = ("fail" if exception_type is not None
                      and issubclass(exception_type, test.failureException) else "error")
            self.record_outcome(test, status)
        super().addSubTest(test, subtest, err)

    @override
    def addDuration(self, test: unittest.TestCase, elapsed: float) -> None:
        super().addDuration(test, elapsed)
        self.collectedDurations[-1] = (test.id(), elapsed)


def timed_outcomes(result: TimedResult) -> list[tuple[str, TestStatus, float]]:
    """Combine each full test id with its outcome and duration."""
    durations = dict(result.collectedDurations)
    return [(test_id, status, 0.0 if status == "skip" else durations.get(test_id, 0.0))
            for test_id, status in result.outcomes.items()]


def cpu_seconds() -> float:
    """Read own CPU and the CPU of children already reaped by this process."""
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


class TestMeasure(TypedDict):
    test: str
    s: float
    status: TestStatus
    kind: Literal["test", "fixture"]
    cpu_s: float


class FixtureMeasure(TypedDict):
    kind: Literal["fixture"]
    test: str
    s: float
    cpu_s: float


def test_times(result: TimedResult) -> list[TestTime]:
    """Keep legacy outcome rows and add the CPU and measure kind."""
    rows: list[TestTime] = []
    for test_id, status, elapsed in timed_outcomes(result):
        fixture = test_id in result.fixtures
        row: TestMeasure = {
            "test": test_id if fixture else result.test_labels[test_id],
            "s": elapsed, "status": status,
            "kind": "fixture" if fixture else "test",
            "cpu_s": result.cpu_times.get(test_id, 0.0),
        }
        rows.append(row)
    return rows


@contextmanager
def fixture_measures(suite: unittest.TestSuite | unittest.TestCase
                     ) -> Generator[list[FixtureMeasure], None, None]:
    """Measure fixture calls without changing unittest's fixture dispatch."""
    rows: list[FixtureMeasure] = []
    restorations: list[tuple[object, str, dict[str, object]]] = []
    classes: set[type[unittest.TestCase]] = set()
    modules: set[str] = set()
    pending: list[tuple[object, str, Callable[[], object], str, bool]] = []
    depth = 0

    def measured(function: Callable[[], object], name: str) -> Callable[[], object]:
        @wraps(function)
        def call() -> object:
            nonlocal depth
            if depth:
                return function()
            depth += 1
            cpu = cpu_seconds()
            started = time.perf_counter()
            try:
                return function()
            finally:
                depth -= 1
                rows.append({"kind": "fixture", "test": name,
                             "s": time.perf_counter() - started,
                             "cpu_s": cpu_seconds() - cpu})
        return call

    def install(owner: object, name: str, function: Callable[[], object], label: str,
                *, class_method: bool = False) -> None:
        original = {name: vars(owner)[name]} if name in vars(owner) else {}
        restorations.append((owner, name, original))
        if class_method and isinstance(function, MethodType):
            unbound = cast(Callable[[type[unittest.TestCase]], object], function.__func__)

            def class_call(cls: type[unittest.TestCase]) -> object:
                return measured(lambda: unbound(cls), f"{name} ({cls.__module__}.{cls.__qualname__})")()

            setattr(owner, name, classmethod(class_call))
        elif class_method:
            setattr(owner, name, staticmethod(measured(function, label)))
        else:
            setattr(owner, name, measured(function, label))

    def visit(test: unittest.TestSuite | unittest.TestCase) -> None:
        if isinstance(test, unittest.TestSuite):
            for child in test:
                visit(child)
            return
        cls = type(test)
        if cls not in classes:
            classes.add(cls)
            for name in ("setUpClass", "tearDownClass", "doClassCleanups"):
                value = cast(object, getattr(cls, name))
                if value is None:
                    continue
                pending.append((cls, name, cast(Callable[[], object], value), f"{name} ({cls.__module__}.{cls.__qualname__})", True))
        module_name = cls.__module__
        if module_name not in modules:
            modules.add(module_name)
            module = sys.modules.get(module_name)
            if module is None:
                return
            for name in ("setUpModule", "tearDownModule"):
                if name in vars(module):
                    value = cast(object, getattr(module, name))
                    if value is None:
                        continue
                    pending.append((module, name, cast(Callable[[], object], value), f"{name} ({module_name})", False))

    try:
        visit(suite)
        for owner, name, function, label, class_method in pending:
            install(owner, name, function, label, class_method=class_method)
        install(unittest_case, "doModuleCleanups", unittest_case.doModuleCleanups, "doModuleCleanups")
        yield rows
    finally:
        for owner, name, original in reversed(restorations):
            if name in original:
                setattr(owner, name, original[name])
            else:
                delattr(owner, name)


def fixture_label(label: str) -> str:
    operation, opening, owner = label.partition(" (")
    if not opening:
        return label
    parts = owner.removesuffix(")").split(".")
    count = 1 if operation.endswith("Module") else 2
    return f"{operation} ({'.'.join(parts[-count:])})"


def write_times(path: Path, result: TimedResult, wall_s: float,
                fixtures: list[FixtureMeasure], config_reads: Sequence[str] | NoConfigReadLog) -> None:
    """Replace the output file without changing the test result on failure."""
    temporary = ""
    try:
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        tests = test_times(result)
        for row in tests:
            if row["test"] in result.fixtures:
                cast(TestMeasure, row)["cpu_s"] = sum(
                    fixture["cpu_s"] for fixture in fixtures if fixture["test"] == row["test"])
        for measure in [*tests, *fixtures]:
            measure["test"] = fixture_label(measure["test"])
        times: dict[str, object] = {
            "wall_s": wall_s,
            "cpu_s": startup_cpu_s + own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
            "tests_s": sum(test["s"] for test in tests),
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "tests": tests,
            "fixtures": fixtures,
        }
        if not isinstance(config_reads, NoConfigReadLog):
            times["config_reads"] = sorted(set(config_reads))
        times["folder_scans"] = sorted(TimedRunner.scanned_folders)
        times["content_reads"] = sorted(TimedRunner.read_contents)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            json.dump(times, handle)
            _ = handle.write("\n")
        os.replace(temporary, path)
    except Exception as error:
        detail = str(error).replace("\n", " ")
        print(f"test times not written: {detail}", file=sys.stderr)
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


class TimedRunner(unittest.TextTestRunner):
    resultclass: Callable[[TestOutput, bool, int], TimedResult] = TimedResult
    destination: ClassVar[TimesOutput | NoTimesOutput] = NoTimesOutput()
    scanned_folders: ClassVar[set[str]] = set()
    read_contents: ClassVar[set[str]] = set()

    @override
    def run(self, test: unittest.TestSuite | unittest.TestCase) -> unittest.TextTestResult:
        with fixture_measures(test) as fixtures:
            result = super().run(test)
        wall_s = time.perf_counter() - _STARTED_AT
        if isinstance(self.destination, TimesOutput):
            log = self.destination.config_log
            reads = log.read_text(encoding="utf-8").splitlines() if isinstance(log, Path) else log
            write_times(self.destination.path, cast(TimedResult, result), wall_s, fixtures, reads)
        return result


def main() -> None:
    try:
        from scripts.lib import conf_reads
    except ImportError:
        from importlib.util import find_spec

        if find_spec("scripts.lib.conf_reads") is not None:
            raise
        run_module(NoConfigReadLog())
        return

    with tempfile.TemporaryDirectory(prefix="config-reads-") as temporary:
        log = Path(temporary) / "reads"
        log.touch()
        with conf_reads.record_reads(log):
            run_module(log)


def run_module(config_log: Path | NoConfigReadLog) -> None:
    if len(sys.argv) != 3:
        print("usage: module_run.py <start_dir> <pattern>", file=sys.stderr)
        sys.exit(2)
    start_dir, pattern = sys.argv[1:]
    output = os.environ.pop("TEST_TIMES_OUT", None)
    TimedRunner.destination = NoTimesOutput() if output is None else TimesOutput(Path(output), config_log)
    try:
        names: list[str] = []
        for path in sorted(Path(start_dir).resolve().glob(pattern)):
            candidates: list[tuple[str, ...]] = []
            for entry in sys.path:
                base = Path(entry).resolve()
                if path.is_relative_to(base):
                    parts = path.relative_to(base).with_suffix("").parts
                    if all(part.isidentifier() for part in parts):
                        candidates.append(parts)
            names.append(".".join(max(candidates, key=len, default=(path.stem,))))
        if isinstance(TimedRunner.destination, TimesOutput):
            TimedRunner.scanned_folders = set()
            TimedRunner.read_contents = set()
            sys.addaudithook(RepositoryReadAudit(Path(__file__).absolute().parents[2], TimedRunner.scanned_folders,
                                                TimedRunner.read_contents))
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        result = TimedRunner(warnings=None if cast(list[str], sys.warnoptions) else "default").run(suite)
        sys.exit(1 if not result.wasSuccessful() else 5 if result.testsRun == 0 and not result.skipped else 0)
    except SystemExit:
        if "scripts.message.session_send_test_support" in sys.modules:
            from scripts.message import session_send_test_support

            if session_send_test_support.real_sender_was_started():
                print(f"{pattern}: started the real scripts/message/send.py", file=sys.stderr)
                sys.exit(1)
        raise


class RunnerInterrupted(BaseException):
    """A signal requests the end of the runner and its descendants."""


def run_with_descendant_cleanup() -> None:
    """Run tests in a child while the parent reaps their orphaned processes."""
    global startup_cpu_s
    from scripts.lib.descendants import adopt_descendants

    if not adopt_descendants():
        main()
        return
    watched = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, watched)
    received: list[int] = []
    cleaning = False

    def interrupted(number: int, frame: object) -> None:
        del frame
        received.append(number)
        if not cleaning:
            raise RunnerInterrupted

    previous = {number: signal.signal(number, interrupted) for number in watched}
    previous_sigchld = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    test_pid = os.fork()
    if test_pid == 0:
        startup_cpu_s = usage.ru_utime + usage.ru_stime
        _ = signal.signal(signal.SIGCHLD, previous_sigchld)
        for number, handler in previous.items():
            _ = signal.signal(number, handler)
        _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        main()
        return
    status = 0
    try:
        _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        while True:
            pid, child_status = os.waitpid(-1, 0)
            if pid == test_pid:
                status = child_status
                break
    except RunnerInterrupted:
        pass
    finally:
        cleaning = True
        while True:
            children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
            for child in children:
                try:
                    os.kill(int(child), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                pid, child_status = os.waitpid(-1, 0)
            except ChildProcessError:
                break
            if pid == test_pid:
                status = child_status
    exit_code = os.waitstatus_to_exitcode(status)
    if received or exit_code < 0:
        number = received[0] if received else -exit_code
        _ = signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)
    sys.exit(exit_code)


if __name__ == "__main__":
    run_with_descendant_cleanup()
