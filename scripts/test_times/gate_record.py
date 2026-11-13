"""Read module times and append gate runs to the build log."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Final, Literal, NotRequired, TypedDict, cast, get_args

from scripts.lib import conf


TestStatus = Literal["pass", "fail", "error", "skip", "expected_failure", "unexpected_success"]
_TEST_STATUSES = frozenset(cast(tuple[str, ...], get_args(TestStatus)))


class TestTime(TypedDict):
    test: str
    s: float
    status: TestStatus


class ModuleTimes(TypedDict):
    config_reads: NotRequired[list[str]]
    folder_scans: NotRequired[list[str]]
    content_reads: NotRequired[list[str]]
    wall_s: float
    cpu_s: float
    tests_s: float
    tests_run: int
    failures: int
    errors: int
    skipped: int
    tests: list[TestTime]


# The (suite, module) of the merge gate's type-check job, which runs no tests.
TYPE_CHECK_JOB: Final = ("types", "basedpyright")


class ModuleRun(TypedDict):
    # Older records can omit the module times.
    started_at: NotRequired[str]
    ended_at: NotRequired[str]
    config_reads: NotRequired[list[str]]
    folder_scans: NotRequired[list[str]]
    content_reads: NotRequired[list[str]]
    suite: str
    module: str
    attempt: int
    wall_s: float
    cpu_s: float
    peak_mib: float
    status: int
    tests_run: int
    failures: int
    errors: int
    skipped: int
    tests_s: float
    tests: list[TestTime]
    restart: str
    times_missing: str
    log: str


class SuiteRun(TypedDict):
    suite: str
    wall_s: float
    status: int


class GateRun(TypedDict):
    step: str
    dirty_paths: list[str]
    dirty_path_count: int
    started_at: str
    ended_at: str
    wall_s: float
    hold_wait_s: float
    selection: str
    reason: str
    status: int
    max_jobs: int
    peak_mib: float
    cap_mib: float
    checkout: str
    branch: str
    sha: str
    tree: str
    source: str
    aborted: bool
    dirty: bool
    suites: list[SuiteRun]
    modules: list[ModuleRun]
    slow_test_s: float


class GateRunRecord(GateRun):
    kind: str
    v: int
    id: str
    host: str
    tests_run: int


@dataclass(frozen=True)
class NoModuleTimes:
    reason: str


@dataclass(frozen=True)
class WallClockSpan:
    """The wall-clock epoch seconds for one module attempt."""
    started_at: float
    ended_at: float


@dataclass(frozen=True)
class WallClockNotRecorded:
    """The attempt has no recorded wall-clock span."""
    reason: str


@dataclass(frozen=True)
class RecordedConfigReads:
    """The module recorded its reads, including a known empty set."""

    stems: frozenset[str]


@dataclass(frozen=True)
class NoConfigReadRecord:
    """The module has no usable record of its config reads."""


def config_read_record(value: object) -> RecordedConfigReads | NoConfigReadRecord:
    if value is None:
        return NoConfigReadRecord()
    if not isinstance(value, list) or not all(isinstance(stem, str) for stem in cast(list[object], value)):
        raise ValueError("expected a list of config stems")
    return RecordedConfigReads(frozenset(cast(list[str], value)))


@dataclass(frozen=True)
class ListedDirectory:
    """The module listed the direct entries of one repository folder."""

    path: str


@dataclass(frozen=True)
class RecursiveDirectory:
    """The module walked every path under one repository folder."""

    prefix: str


@dataclass(frozen=True)
class WholeRepository:
    """Every repository path is within this scan."""


@dataclass(frozen=True)
class RecordedFolderScans:
    """The module recorded its repository listings, including an empty set."""

    folders: frozenset[ListedDirectory | RecursiveDirectory | WholeRepository]


@dataclass(frozen=True)
class NoFolderScanRecord:
    """The module has no usable record of its repository listings."""


@dataclass(frozen=True)
class RecordedContentReads:
    """The module recorded repository file reads, including an empty set."""

    paths: frozenset[str]


@dataclass(frozen=True)
class NoContentReadRecord:
    """The module has no usable record of repository file reads."""


@dataclass(frozen=True)
class ModuleReadRecord:
    config_reads: RecordedConfigReads | NoConfigReadRecord
    folder_scans: RecordedFolderScans | NoFolderScanRecord
    content_reads: RecordedContentReads | NoContentReadRecord


def module_read_record(configs: object, folders: object, contents: object) -> ModuleReadRecord:
    """Keep absent fields separate from recorded empty fields."""
    config_record = config_read_record(configs)
    folder_record: RecordedFolderScans | NoFolderScanRecord = NoFolderScanRecord()
    if folders is not None:
        if not isinstance(folders, list) or not all(isinstance(path, str) for path in cast(list[object], folders)):
            raise ValueError("expected a list of scanned folders")
        decoded: set[ListedDirectory | RecursiveDirectory | WholeRepository] = set()
        for path in cast(list[str], folders):
            decoded.add(WholeRepository() if path == "/" else RecursiveDirectory(path[:-1])
                        if path.endswith("/") else ListedDirectory(path))
        folder_record = RecordedFolderScans(frozenset(decoded))
    content_record: RecordedContentReads | NoContentReadRecord = NoContentReadRecord()
    if contents is not None:
        if not isinstance(contents, list) or not all(isinstance(path, str) for path in cast(list[object], contents)):
            raise ValueError("expected a list of repository file reads")
        content_record = RecordedContentReads(frozenset(cast(list[str], contents)))
    return ModuleReadRecord(config_record, folder_record, content_record)


def folder_scan_strings(scans: RecordedFolderScans) -> list[str]:
    """Encode the named folder states in the existing JSON format."""
    return sorted(folder.path if isinstance(folder, ListedDirectory)
                  else folder.prefix + "/" if isinstance(folder, RecursiveDirectory) else "/"
                  for folder in scans.folders)


@dataclass(frozen=True)
class TimingNotApplicable:
    """The type-check job runs no tests and writes no timing file."""


@dataclass(frozen=True)
class Execution:
    """One finished gate job, including each hold restart and retry."""
    suite: str
    module: str
    wall_s: float
    cpu_s: float
    peak_mib: float
    status: int
    restart: Literal["", "hold"]
    times: ModuleTimes | NoModuleTimes | TimingNotApplicable
    output: str
    wall_clock: WallClockSpan | WallClockNotRecorded = field(default=WallClockNotRecorded("not recorded"), kw_only=True)


def _fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected seconds as a number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("expected finite, nonnegative seconds")
    return seconds


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a nonnegative integer")
    return value


def _test_time(value: object) -> TestTime:
    fields = _fields(value)
    test = fields["test"]
    status = fields["status"]
    if not isinstance(test, str):
        raise ValueError("expected a test name")
    if not isinstance(status, str) or status not in _TEST_STATUSES:
        raise ValueError("invalid test status")
    return {"test": test, "s": _seconds(fields["s"]), "status": cast(TestStatus, status)}


def read_module_times(path: Path) -> ModuleTimes | NoModuleTimes:
    """Validate the complete file before returning module times."""
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
        fields = _fields(decoded)
        tests = fields["tests"]
        if not isinstance(tests, list):
            raise ValueError("expected a list of test times")
        times: ModuleTimes = {
            "wall_s": _seconds(fields["wall_s"]),
            "cpu_s": _seconds(fields["cpu_s"]),
            "tests_s": _seconds(fields["tests_s"]),
            "tests_run": _count(fields["tests_run"]),
            "failures": _count(fields["failures"]),
            "errors": _count(fields["errors"]),
            "skipped": _count(fields["skipped"]),
            "tests": [_test_time(test) for test in cast(list[object], tests)],
        }
        reads = module_read_record(fields.get("config_reads"), fields.get("folder_scans"), fields.get("content_reads"))
        if isinstance(reads.config_reads, RecordedConfigReads):
            times["config_reads"] = sorted(reads.config_reads.stems)
        if isinstance(reads.folder_scans, RecordedFolderScans):
            times["folder_scans"] = folder_scan_strings(reads.folder_scans)
        if isinstance(reads.content_reads, RecordedContentReads):
            times["content_reads"] = sorted(reads.content_reads.paths)
        return times
    except (OSError, UnicodeError, ValueError, KeyError, OverflowError, RecursionError) as error:
        return NoModuleTimes(f"{path}: {str(error).replace(chr(10), ' ')}")


def kept_tests(times: ModuleTimes, slow_test_s: float) -> list[TestTime]:
    """Keep failures and tests that meet the time threshold."""
    return sorted(
        (test for test in times["tests"]
         if test["status"] in {"fail", "error", "unexpected_success"} or test["s"] >= slow_test_s),
        key=lambda test: test["s"], reverse=True,
    )


def slow_test_s() -> float:
    return conf.read("test_times").number("SLOW_TEST_S")


def module_runs(executions: Sequence[Execution], slow_test_s: float) -> list[ModuleRun]:
    """Keep each attempt and select its test times with one threshold."""
    from scripts.buildlog import store

    attempts: dict[tuple[str, str], int] = {}
    modules: list[ModuleRun] = []
    for job in executions:
        key = (job.suite, job.module)
        attempts[key] = attempts.get(key, 0) + 1
        module: ModuleRun = {
            "suite": job.suite, "module": job.module, "attempt": attempts[key],
            "wall_s": job.wall_s, "cpu_s": job.cpu_s, "peak_mib": job.peak_mib,
            "status": job.status, "tests_run": 0, "failures": 0, "errors": 0,
            "skipped": 0, "tests_s": 0.0, "tests": [], "restart": job.restart,
            "times_missing": job.times.reason if isinstance(job.times, NoModuleTimes) else "",
            "log": "",
        }
        if isinstance(job.wall_clock, WallClockSpan):
            module["started_at"] = store.utc_iso(job.wall_clock.started_at)
            module["ended_at"] = store.utc_iso(job.wall_clock.ended_at)
        if not isinstance(job.times, (NoModuleTimes, TimingNotApplicable)):
            times = job.times
            reads = module_read_record(times.get("config_reads"), times.get("folder_scans"), times.get("content_reads"))
            if isinstance(reads.config_reads, RecordedConfigReads):
                module["config_reads"] = sorted(reads.config_reads.stems)
            if isinstance(reads.folder_scans, RecordedFolderScans):
                module["folder_scans"] = folder_scan_strings(reads.folder_scans)
            if isinstance(reads.content_reads, RecordedContentReads):
                module["content_reads"] = sorted(reads.content_reads.paths)
            module.update(tests_run=times["tests_run"], failures=times["failures"],
                          errors=times["errors"], skipped=times["skipped"], tests_s=times["tests_s"],
                          tests=kept_tests(times, slow_test_s))
        modules.append(module)
    return modules


def report_not_written(error: BaseException) -> None:
    """Report a failed record on one line."""
    detail = " ".join(str(error).splitlines())
    print(f"gate run not written: {detail}", file=sys.stderr)


def keep_failure_logs(record_id: str, host: str, epoch: float, modules: Sequence[ModuleRun],
                      executions: Sequence[Execution]) -> None:
    """Keep each failed attempt under the host that recorded the gate."""
    from scripts.buildlog import record

    for module in modules:
        module["log"] = ""
    failures = [(module, job) for module, job in zip(modules, executions, strict=True)
                if job.status != 0 and not job.restart]
    if failures:
        limit = conf.read("test_times").whole("GATE_FAILURE_LOG_KIB") * 1024
        for module, job in failures:
            data = job.output.encode("utf-8")
            cut = max(0, len(data) - limit)
            kept = f"Cut {cut} bytes from the start of this output.\n".encode("utf-8") + data[-limit:]
            key = json.dumps((module["suite"], module["module"], module["attempt"])).encode("utf-8")
            name = f"{record_id}-{hashlib.sha256(key).hexdigest()}"
            try:
                module["log"] = record.keep_log(kept, host, epoch, name)
            except Exception as error:
                report_not_written(error)


def write_gate_run(run: GateRun, executions: Sequence[Execution]) -> None:
    """Append one gate record without failing the caller."""
    try:
        # This import keeps gate_record free of an import cycle with index.py.
        from scripts.buildlog import record, store

        epoch = time.time()
        host = store.host_name()
        entry: GateRunRecord = {"kind": "gate_run", "v": record.RECORD_VERSION,
                                "id": record.new_id(host, epoch), "host": host, **run,
                                "tests_run": sum(module["tests_run"] for module in run["modules"])}
        keep_failure_logs(entry["id"], host, epoch, run["modules"], executions)
        store.append_line(store.host_file(host, epoch), entry)
    except Exception as error:
        report_not_written(error)
