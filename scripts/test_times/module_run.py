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
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
import resource
import signal
import sys
import tempfile
from types import MethodType, TracebackType
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, TypedDict, cast, override
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
class TimesOutput:
    path: Path


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
                fixtures: list[FixtureMeasure]) -> None:
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

    @override
    def run(self, test: unittest.TestSuite | unittest.TestCase) -> unittest.TextTestResult:
        with fixture_measures(test) as fixtures:
            result = super().run(test)
        wall_s = time.perf_counter() - _STARTED_AT
        if isinstance(self.destination, TimesOutput):
            write_times(self.destination.path, cast(TimedResult, result), wall_s, fixtures)
        return result


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: module_run.py <start_dir> <pattern>", file=sys.stderr)
        sys.exit(2)
    start_dir, pattern = sys.argv[1:]
    output = os.environ.pop("TEST_TIMES_OUT", None)
    TimedRunner.destination = NoTimesOutput() if output is None else TimesOutput(Path(output))
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
