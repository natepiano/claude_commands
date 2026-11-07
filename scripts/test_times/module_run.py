#!/usr/bin/env python3
"""Run unittest discovery and record each test's time."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import time

_STARTED_AT = time.perf_counter()

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import resource
import sys
import tempfile
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Protocol, cast, override
import unittest

if TYPE_CHECKING:
    from scripts.test_times.gate_record import ModuleTimes, TestStatus, TestTime


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

    @override
    def startTest(self, test: unittest.TestCase) -> None:
        self.open_test = OpenTest(test.id())
        super().startTest(test)

    @override
    def stopTest(self, test: unittest.TestCase) -> None:
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


def test_times(result: TimedResult) -> list[TestTime]:
    """Use short test names for the timing output."""
    return [{"test": test_id if test_id in result.fixtures else test_id.partition(".")[2] or test_id,
             "s": seconds, "status": status}
            for test_id, status, seconds in timed_outcomes(result)]


def write_times(path: Path, result: TimedResult, wall_s: float) -> None:
    """Replace the output file without changing the test result on failure."""
    temporary = ""
    try:
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        tests = test_times(result)
        times: ModuleTimes = {
            "wall_s": wall_s,
            "cpu_s": own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
            "tests_s": sum(test["s"] for test in tests),
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "tests": tests,
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
        result = super().run(test)
        wall_s = time.perf_counter() - _STARTED_AT
        if isinstance(self.destination, TimesOutput):
            write_times(self.destination.path, cast(TimedResult, result), wall_s)
        return result


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: module_run.py <start_dir> <pattern>", file=sys.stderr)
        sys.exit(2)
    # Match the import path of python -m unittest from the current folder.
    sys.path[0] = os.getcwd()
    start_dir, pattern = sys.argv[1:]
    output = os.environ.pop("TEST_TIMES_OUT", None)
    TimedRunner.destination = NoTimesOutput() if output is None else TimesOutput(Path(output))
    try:
        _ = unittest.main(module=None,
                          argv=["python -m unittest", "discover", "-s", start_dir, "-p", pattern],
                          testRunner=TimedRunner)
    except SystemExit:
        if "scripts.message.session_send_test_support" in sys.modules:
            from scripts.message import session_send_test_support

            marker = session_send_test_support.refusal_marker
            if isinstance(marker, session_send_test_support.SenderRefusalMarker) and marker.path.is_file():
                print(f"{pattern}: started the real scripts/message/send.py", file=sys.stderr)
                sys.exit(1)
        raise


if __name__ == "__main__":
    main()
