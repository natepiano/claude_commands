#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""List tests whose executed lines are covered by tests that remain kept.

Candidates can check different results. Review their assertions before removal.
Collector times describe this run. Measure speed with the collector disabled.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Sequence
from collections import defaultdict
from dataclasses import dataclass
import inspect
import math
import json
import os
from pathlib import Path
import platform
import sys
from typing import TYPE_CHECKING, TypedDict, cast, override
import tempfile
import unittest


from scripts.test_times.line_collector import (
    REPOSITORY, SHARED_TEST, SHARED_THREAD, ChildLaunch, ChildReport, ExecutedLine, LineCollector, read_child_reports,
)
from scripts.lib import conf
from scripts.lib.terminal_cells import print_table, seconds
from scripts.buildlog import treekey
from scripts.production.gate_modules import discover_test_modules
from scripts.test_times.module_run import TimedResult, TimedRunner, timed_outcomes

if TYPE_CHECKING:
    from scripts.test_times.gate_record import TestStatus


class ModuleImportsFailed(Exception):
    """Selected test modules failed during discovery."""


@dataclass(frozen=True)
class TestCoverage:
    test_id: str
    status: TestStatus
    seconds: float
    lines: frozenset[ExecutedLine]
    child: str


class CoverageRow(TypedDict):
    id: str
    status: TestStatus
    seconds: float
    line_count: int
    child: str
    candidate: bool
    covered_by: list[str]


class CoverageReport(TypedDict):
    tree_sha: str
    interpreter: str
    tests: list[CoverageRow]


class ChildEnvironment:
    """Restore the test environment after each measured test."""

    def __init__(self, directory: Path) -> None:
        self.directory: Path = directory
        self.names: tuple[str, ...] = ("NATEST_COVERAGE_TEST", "NATEST_COVERAGE_DIR",
                                       "NATEST_COVERAGE_LEAF_PROGRAMS", "NATEST_COVERAGE_LAUNCH", "PYTHONPATH")
        self.saved: dict[str, str] = {name: os.environ[name] for name in self.names if name in os.environ}
        child_path = str(Path(__file__).resolve().parent / "coverage_child")
        previous = self.saved.get("PYTHONPATH", "")
        from scripts.lib.python_startup.sitecustomize import inherited_paths

        paths = inherited_paths(previous)
        self.python_path: str = os.pathsep.join((child_path, *paths))
        self.leaves: str = " ".join(conf.read("test_times").words("COVERAGE_LEAF_PROGRAMS"))

    def start_test(self, test_id: str) -> None:
        os.environ["PYTHONPATH"] = self.python_path
        os.environ["NATEST_COVERAGE_TEST"] = test_id
        os.environ["NATEST_COVERAGE_DIR"] = str(self.directory)
        os.environ["NATEST_COVERAGE_LAUNCH"] = ""
        os.environ["NATEST_COVERAGE_LEAF_PROGRAMS"] = self.leaves

    def between_tests(self) -> None:
        self.start_test(SHARED_TEST)

    def restore(self) -> None:
        for name in self.names:
            if name in self.saved:
                os.environ[name] = self.saved[name]
            else:
                _ = os.environ.pop(name, None)


@dataclass
class ChildEvidence:
    """Keep reports and resolved owners without changing launch records."""

    reports: dict[str, list[ChildReport]]
    launches: dict[str, ChildLaunch]
    owners: dict[str, str]
    all_reports: list[ChildReport]


def child_evidence(collector: LineCollector, reports: list[ChildReport]) -> ChildEvidence:
    launches = {launch["launch_id"]: launch for launch in collector.shared_launches}
    for recorded in collector.completed.values():
        launches.update((launch["launch_id"], launch) for launch in recorded.launches)
    parents: dict[str, str] = {}
    fallback_owners: dict[str, str] = {}
    for report in reports:
        fallback_owners[report["launch_id"]] = report["test_id"]
        for launch in report["launches"]:
            launches[launch["launch_id"]] = launch
            if report["launch_id"]:
                parents[launch["launch_id"]] = report["launch_id"]

    def ownership(launch_id: str, seen: frozenset[str]) -> str:
        launch = launches.get(launch_id)
        if launch is None:
            return fallback_owners.get(launch_id, SHARED_TEST)
        if launch["owner"] == SHARED_THREAD:
            return SHARED_THREAD
        if launch_id in parents and launch_id not in seen:
            return ownership(parents[launch_id], seen | {launch_id})
        return launch["owner"]

    owners = {launch_id: ownership(launch_id, frozenset()) for launch_id in launches}
    grouped: dict[str, list[ChildReport]] = defaultdict(list)
    for report in reports:
        owner = owners.get(report["launch_id"], report["test_id"])
        grouped[owner].append(report)
    return ChildEvidence(grouped, launches, owners, reports)


def child_causes(collector: LineCollector, evidence: ChildEvidence) -> dict[str, set[str]]:
    """Check completed reports separately from each child's lifetime."""
    causes = {test_id: set(recorded.causes) for test_id, recorded in collector.completed.items()}
    reports_by_launch: dict[str, list[ChildReport]] = defaultdict(list)
    for report in evidence.all_reports:
        reports_by_launch[report["launch_id"]].append(report)
    report_ends: dict[int, float] = {}

    def report_end(report: ChildReport, seen: frozenset[str]) -> float:
        key = id(report)
        if key in report_ends:
            return report_ends[key]
        if report["completed"]:
            end = report["exited"]
        else:
            transition = report["exec_launch_id"]
            successors = reports_by_launch.get(transition, []) if transition and transition not in seen else []
            end = max((report_end(successor, seen | {transition}) for successor in successors), default=math.inf)
        report_ends[key] = end
        return end

    ends = {launch_id: max(report_end(report, frozenset()) for report in reports)
            for launch_id, reports in reports_by_launch.items()}
    reported = {report["launch_id"] for report in evidence.all_reports if math.isfinite(report_ends[id(report)])}
    for report in evidence.all_reports:
        ends.update(report["ends"])
    ends.update(collector.ends)
    launches_by_owner: dict[str, list[ChildLaunch]] = defaultdict(list)
    for launch_id, launch in evidence.launches.items():
        launches_by_owner[evidence.owners[launch_id]].append(launch)

    for test_id, recorded in collector.completed.items():
        reports = evidence.reports.get(test_id, [])
        for report in reports:
            causes[test_id].update(report["causes"])
        launches = launches_by_owner.get(test_id, [])
        for launch in launches:
            if launch["kind"] == "unverified":
                causes[test_id].add(f'unverified launch {launch["program"]}')
        starts = [launch for launch in launches if launch["kind"] == "python"]
        arrived = sum(launch["launch_id"] in reported for launch in starts)
        if arrived < len(starts):
            causes[test_id].add(f"{len(starts)} python children, {arrived} reported")
        fork_counts = [(recorded.pid, recorded.forks)] + [(report["pid"], report["forks"]) for report in reports]
        for pid, forks in fork_counts:
            if forks == 0:
                continue
            transitions = {report["collector_id"] for report in reports
                           if report["fork_parent_pid"] == pid and report["exec_launch_id"]
                           and report["exec_launch_id"] in reported}
            if forks > len(transitions):
                causes[test_id].add("fork")

    for launch_id, launch in evidence.launches.items():
        owner = evidence.owners[launch_id]
        if owner == SHARED_TEST and launch["kind"] != "python":
            continue
        started = launch["started"]
        end = ends.get(launch_id, math.inf)
        for test_id, recorded in collector.completed.items():
            at_start = launch_id in recorded.live_children_at_start or started < recorded.started < end
            at_stop = launch_id in recorded.live_children_at_stop or started < recorded.stopped < end
            overlap = at_start or at_stop or (started < recorded.stopped and recorded.started < end)
            if owner == SHARED_THREAD:
                if overlap:
                    causes[test_id].add("shared thread")
            elif owner == test_id:
                if at_stop:
                    causes[test_id].add("child outlived its test")
            elif at_start:
                causes[test_id].add("shared child" if owner == SHARED_TEST else "child outlived its test")
    return causes


def collect(suite: Callable[[], unittest.TestSuite], repository: Path = REPOSITORY
            ) -> tuple[TimedResult, list[TestCoverage]]:
    """Run a suite through the shared timed runner and join its child reports."""
    with tempfile.TemporaryDirectory(prefix="natest-coverage-") as directory:
        environment = ChildEnvironment(Path(directory))

        class CoverageResult(TimedResult):
            @override
            def startTest(self, test: unittest.TestCase) -> None:
                super().startTest(test)
                environment.start_test(test.id())
                collector.start_test(test.id())

            @override
            def stopTest(self, test: unittest.TestCase) -> None:
                collector.stop_test()
                environment.between_tests()
                super().stopTest(test)

        ignored_files = [Path(inspect.getfile(cls)) for cls in CoverageResult.__mro__ if cls.__module__ != "builtins"]
        collector = LineCollector(repository, ignored_files)
        runner = TimedRunner(verbosity=1, resultclass=CoverageResult)
        try:
            environment.between_tests()
            collector.start()
            result = cast(TimedResult, runner.run(suite()))
        finally:
            collector.stop()
            environment.restore()
        evidence = child_evidence(collector, read_child_reports(Path(directory)))
    causes = child_causes(collector, evidence)
    tests: list[TestCoverage] = []
    for test_id, status, duration in timed_outcomes(result):
        recorded = collector.completed.get(test_id)
        lines: set[ExecutedLine] = set() if recorded is None else recorded.lines
        for report in evidence.reports.get(test_id, []):
            lines.update((path, line) for path, line in report["lines"])
        reasons = causes.get(test_id, set())
        child = "unknown: " + "; ".join(sorted(reasons)) if reasons else "none"
        tests.append(TestCoverage(test_id, status, duration,
                                  frozenset(lines), child))
    return result, tests


def select_candidates(tests: Sequence[TestCoverage]) -> dict[str, list[str]]:
    """Remove covered tests in descending time order and name the final kept tests."""
    known = {test.test_id: test for test in tests if test.child == "none"}
    kept = set(known)
    owners: dict[ExecutedLine, set[str]] = {}
    for test in known.values():
        for line in test.lines:
            owners.setdefault(line, set()).add(test.test_id)
    removed: list[str] = []
    for test in sorted(known.values(), key=lambda test: (-test.seconds, test.test_id)):
        if len(kept) > 1 and all(len(owners[line]) > 1 for line in test.lines):
            kept.remove(test.test_id)
            removed.append(test.test_id)
            for line in test.lines:
                owners[line].remove(test.test_id)
    first_owners = {line: min(line_owners) for line, line_owners in owners.items() if line_owners}
    candidates: dict[str, list[str]] = {}
    for test_id in removed:
        covering = {first_owners[line] for line in known[test_id].lines}
        candidates[test_id] = sorted(covering or {min(kept)})
    return candidates


def report_rows(tests: Sequence[TestCoverage]) -> list[CoverageRow]:
    candidates = select_candidates(tests)
    return [{"id": test.test_id, "status": test.status, "seconds": test.seconds,
             "line_count": len(test.lines), "child": test.child,
             "candidate": test.test_id in candidates, "covered_by": candidates.get(test.test_id, [])}
            for test in tests]


def print_candidates(rows: Sequence[CoverageRow]) -> None:
    print("Candidates can check different results. Review assertions before removal.")
    print("Measure speed with the collector disabled.")
    candidates = sorted((row for row in rows if row["candidate"]),
                        key=lambda row: (-row["seconds"], row["id"]))
    print_table(("candidate", "covering tests", "seconds"),
                [(row["id"], ", ".join(row["covered_by"]), seconds(row["seconds"])) for row in candidates],
                empty_message="No removal candidates.")
    known = sum(row["child"] == "none" for row in rows)
    print(f"Known tests: {known}/{len(rows)}")
    for row in rows:
        if row["child"] != "none":
            print(f'{row["id"]}: {row["child"]}')


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: coverage_overlap.py scripts/<folder> <file> [<file> ...]", file=sys.stderr)
        return 2
    folder = Path(argv[0]).resolve()
    modules = {path.resolve() for _, path in discover_test_modules(REPOSITORY)}
    paths = list(dict.fromkeys(folder / name for name in argv[1:]))
    if any(path.resolve() not in modules for path in paths):
        print("coverage overlap: select test modules under scripts/", file=sys.stderr)
        return 2
    output = os.environ.pop("COVERAGE_OUT", "")
    _ = os.environ.pop("TEST_TIMES_OUT", None)
    tree = ""
    if output:
        tree = treekey.tree_key(str(REPOSITORY))
        if tree is None:
            print("coverage overlap: cannot read the tree sha", file=sys.stderr)
            return 2

    def load_suite() -> unittest.TestSuite:
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        import_errors: list[str] = []
        for path in paths:
            previous_errors = len(loader.errors)
            module = ".".join(path.relative_to(REPOSITORY).with_suffix("").parts)
            try:
                suite.addTests(loader.loadTestsFromName(module))
            except Exception as error:
                import_errors.append(f"coverage overlap: {path.name} does not import: {type(error).__name__}: {error}")
            if len(loader.errors) > previous_errors:
                reason = str(loader.errors[-1]).splitlines()[-1]
                import_errors.append(f"coverage overlap: {path.name} does not import: {reason}")
        if import_errors:
            raise ModuleImportsFailed("\n".join(import_errors))
        return suite

    try:
        result, tests = collect(load_suite)
    except ModuleImportsFailed as error:
        print(str(error), file=sys.stderr)
        return 2
    except (RuntimeError, conf.Unusable) as error:
        print(f"coverage overlap: {error}", file=sys.stderr)
        return 2
    rows = report_rows(tests)
    if output:
        report: CoverageReport = {"tree_sha": tree,
                                  "interpreter": f"{sys.executable} ({platform.python_version()})",
                                  "tests": rows}
        try:
            _ = Path(output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            print(f"coverage overlap: cannot write output: {error}", file=sys.stderr)
            return 2
    print_candidates(rows)
    return (0 if result.testsRun else 5) if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
