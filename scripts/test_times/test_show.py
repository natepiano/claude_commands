"""Test the history reports with private gate records and a private repository."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import io
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, override
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

if TYPE_CHECKING:
    from ..buildlog import record, store
    from ..buildlog.index import index_path_in
    from ..lint.memory_test_support import write_config_fixture
    from ..production.gate_modules import discover_test_modules
    from . import gate_record, show
    from .gate_record import GateRun, ModuleRun, TestTime
    from .gate_record_test_support import gate_run
else:
    # Import show first because it adds the build log folder to the import path.
    from scripts.test_times import gate_record, show
    from scripts.buildlog import record, store
    from index import index_path_in
    from scripts.lint.memory_test_support import write_config_fixture
    from scripts.production.gate_modules import discover_test_modules
    from scripts.test_times.gate_record import GateRun, ModuleRun, TestTime
    from scripts.test_times.gate_record_test_support import gate_run

EPOCH = datetime(2026, 11, 3, 14, 30, tzinfo=UTC).timestamp()
HOST = store.host_name()
REPORT_COMMANDS = (("top",), ("suites",), ("suite", "tools"),
                   ("module", "tools/test_alpha.py"), ("test", "target"))


def run(*arguments: str) -> tuple[int, str, str]:
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        status = show.main(list(arguments))
    return status, output.getvalue(), errors.getvalue()


def module_run(module: str, wall_s: float, *, suite: str = "tools", attempt: int = 1,
               restart: str = "", missing: str = "", status: int = 0,
               tests: Sequence[TestTime] = ()) -> ModuleRun:
    return {
        "suite": suite, "module": module, "attempt": attempt,
        "wall_s": wall_s, "cpu_s": wall_s / 2, "peak_mib": 12.0, "status": status,
        "tests_run": 0 if missing else 13, "failures": int(status != 0), "errors": 0, "skipped": 0,
        "tests_s": 0.0 if missing else wall_s / 4, "tests": list(tests), "restart": restart, "times_missing": missing,
    }


def type_job(wall_s: float) -> ModuleRun:
    row = module_run(gate_record.TYPE_CHECK_JOB[1], wall_s, suite=gate_record.TYPE_CHECK_JOB[0])
    row.update(tests_run=0, tests_s=0.0)
    return row


class TestTimesTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    config: Path = Path()
    log: Path = Path()
    next_id: int = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "home" / ".claude"
        self.config = self.root / "config"
        self.log = self.root / "buildlog"
        self.next_id = 0
        self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(self.log), "CONF_DIR": str(self.config),
            "HOME": str(self.root / "home"), "USER_ZONE": "America/New_York",
        }))
        _ = write_config_fixture(self.config / "test_times.conf", "TOP_RUNS=3")
        for name in ("test_alpha.py", "test_beta.py", "test_missing.py"):
            path = self.repo / "scripts" / "tools" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text("# A live fixture module.\n", encoding="utf-8")

    def write_run(self, modules: list[ModuleRun], *, epoch: float = EPOCH,
                  host: str = HOST, aborted: bool = False, status: int = 0) -> None:
        self.next_id += 1
        suites: list[gate_record.SuiteRun] = [
            {"suite": suite, "wall_s": sum(row["wall_s"] for row in modules if row["suite"] == suite),
             "status": status} for suite in dict.fromkeys(row["suite"] for row in modules)
            if suite != gate_record.TYPE_CHECK_JOB[0]
        ]
        run: GateRun = {
            **gate_run(), "started_at": store.utc_iso(epoch), "ended_at": store.utc_iso(epoch + 30),
            "wall_s": 30.0, "hold_wait_s": 2.0, "selection": "full", "reason": "fixture run",
            "status": status, "aborted": aborted, "suites": suites, "modules": modules,
        }
        with mock.patch.object(record, "new_id", return_value=f"history-{self.next_id}"), \
                mock.patch.object(socket, "gethostname", return_value=host):
            gate_record.write_gate_run(run)

    def report(self, *arguments: str) -> str:
        status, output, errors = run(*arguments)
        self.assertEqual(status, 0, output + errors)
        return output

    def line(self, output: str, subject: str) -> str:
        lines = [line for line in output.splitlines() if subject in line]
        self.assertEqual(len(lines), 1, output)
        return lines[0]

    def cell(self, output: str, row: str, header: str) -> str:
        headings = list(re.finditer(r"\S.*?(?= {2,}|$)", output.splitlines()[0]))
        index = [match.group() for match in headings].index(header)
        start = headings[index].start()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(row)
        return row[start:end].strip()

    def test_every_gate_discovered_timing_module_loads_tests(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        for suite, path in discover_test_modules(repo):
            if suite != "test_times":
                continue
            with self.subTest(module=path.name):
                loader = unittest.TestLoader()
                tests = loader.loadTestsFromName(f"scripts.test_times.{path.stem}")
                self.assertEqual(loader.errors, [])
                self.assertGreater(tests.countTestCases(), 0, str(path))

    def test_top_ranks_live_local_modules_by_the_first_completed_attempt_and_recent_median(self) -> None:
        for offset, wall in enumerate((100.0, 8.0, 4.0)):
            self.write_run([module_run("test_alpha.py", wall)], epoch=EPOCH + offset * 60)
        slow: TestTime = {"test": "Alpha.test_slow", "s": 1.25, "status": "pass"}
        types = type_job(3.0)
        self.write_run([
            module_run("test_alpha.py", 1000.0, restart="hold", status=-15),
            module_run("test_alpha.py", 2.0, attempt=2, status=1, tests=[slow]),
            module_run("test_alpha.py", 0.01, attempt=3),
            module_run("test_beta.py", 7.0), module_run("test_removed.py", 999.0), types,
        ], epoch=EPOCH + 180)
        self.write_run([module_run("test_alpha.py", 9999.0)], host="other-host", epoch=EPOCH + 240)

        output = self.report("top", "--repo", str(self.repo))
        beta = self.line(output, "tools/test_beta.py")
        alpha = self.line(output, "tools/test_alpha.py")
        type_line = self.line(output, "types/basedpyright")
        self.assertLess(output.index(beta), output.index(alpha))
        self.assertLess(output.index(alpha), output.index(type_line))
        self.assertNotIn("test_removed.py", output)
        self.assertNotIn("9999", output)
        self.assertIn("Alpha.test_slow", alpha)
        numbers = [float(match.group()) for match in re.finditer(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", alpha)]
        self.assertEqual(numbers, [2, 4, 2, 2, 13, 1.25, 3], alpha)
        self.assertRegex(output.splitlines()[-1], r"3 modules; sum of medians 14(?:\.0+)? s")
        self.assertEqual(self.report(), output)
        limited = self.report("top", "--limit", "1", "--repo", str(self.repo))
        self.assertIn("tools/test_beta.py", limited)
        self.assertNotIn("tools/test_alpha.py", limited)
        _ = write_config_fixture(self.config / "test_times.conf", "TOP_RUNS=1")
        changed = self.report("top", "--repo", str(self.repo))
        self.assertLess(changed.index("types/basedpyright"), changed.index("tools/test_alpha.py"))

    def test_missing_times_and_type_jobs_do_not_show_test_measurements(self) -> None:
        types = type_job(37.0)
        missing = module_run("test_missing.py", 19.0, missing="runner ended early")
        self.write_run([missing, types])
        output = self.report("top", "--repo", str(self.repo))
        missing_line = self.line(output, "tools/test_missing.py")
        self.assertEqual(missing_line.count("times missing"), 2, missing_line)
        type_line = self.line(output, "types/basedpyright")
        self.assertNotIn("times missing", type_line)
        self.assertEqual(self.cell(output, type_line, "tests run"), "")
        self.assertEqual(self.cell(output, type_line, "slowest kept test"), "")
        output = self.report("module", "tools/test_missing.py")
        self.assertIn("times missing: runner ended early", output)
        type_history = self.report("module", "types/basedpyright")
        self.assertNotIn("times missing", type_history)
        row = self.line(type_history, "11-03 09:30")
        for header in ("tests run", "tests s", "import and fixture s", "slowest kept tests"):
            self.assertEqual(self.cell(type_history, row, header), "", type_history)

    def test_histories_show_local_time_latest_attempts_and_aborted_runs(self) -> None:
        tests: list[TestTime] = [{"test": f"Case.test_{name}", "s": duration, "status": "pass"}
                                for name, duration in (("fast", 0.25), ("slow", 3.0), ("middle", 2.0), ("last", 1.0))]
        self.write_run([module_run("test_alpha.py", 8.0, tests=tests)])
        self.write_run([], epoch=EPOCH + 60, aborted=True, status=1)
        self.write_run([module_run("test_alpha.py", 99.0, restart="hold", status=-15),
                        module_run("test_alpha.py", 12.0, attempt=2, tests=tests)],
                       epoch=EPOCH + 120, aborted=True, status=1)
        self.write_run([module_run("test_alpha.py", 777.0)], host="other-host", epoch=EPOCH + 180)
        suites = self.report("suites", "--days", "100000")
        self.assertLess(suites.index("11-03 09:32"), suites.index("11-03 09:31"))
        self.assertLess(suites.index("11-03 09:31"), suites.index("11-03 09:30"))
        self.assertEqual(suites.count("aborted"), 2, suites)
        self.assertIn("PASS", suites)
        self.assertEqual(suites.count("RED"), 2, suites)
        self.assertIn(HOST, suites)
        self.assertNotIn("other-host", suites)
        self.assertIn("tools/test_alpha.py", suites)
        latest_run = self.line(suites, "11-03 09:32")
        self.assertEqual(self.cell(suites, latest_run, "selection"), "full")
        self.assertEqual(self.cell(suites, latest_run, "modules run"), "1")
        self.assertEqual(float(self.cell(suites, latest_run, "wall s")), 30.0)
        suite = self.report("suite", "tools", "--days", "100000")
        self.assertIn("aborted", self.line(suite, "11-03 09:32"))
        self.assertNotIn("11-03 09:31", suite)
        self.assertNotIn("11-03 09:33", suite)
        latest_suite = self.line(suite, "11-03 09:32")
        self.assertEqual(float(self.cell(suite, latest_suite, "wall s")), 111.0)
        module = self.report("module", "tools/test_alpha.py", "--runs", "2")
        self.assertEqual(module.count("aborted"), 2, module)
        self.assertIn("hold", module)
        self.assertNotIn("11-03 09:30", module)
        self.assertNotIn("Case.test_fast", module)
        for name in ("Case.test_slow", "Case.test_middle", "Case.test_last"):
            self.assertIn(name, module)
        latest_module = module.splitlines()[1]
        for header, value in (("attempt", 2), ("wall s", 12), ("CPU s", 6), ("peak MiB", 12),
                              ("tests run", 13), ("tests s", 3), ("import and fixture s", 9)):
            self.assertEqual(float(self.cell(module, latest_module, header)), value, module)
        self.assertLess(latest_module.index("Case.test_slow"), latest_module.index("Case.test_middle"))
        self.assertLess(latest_module.index("Case.test_middle"), latest_module.index("Case.test_last"))
        top = self.report("top", "--repo", str(self.repo))
        self.assertRegex(self.line(top, "tools/test_alpha.py"), r"\b10\.0+\b")
        history = self.report("test", "test_slow", "--runs", "2")
        self.assertIn("aborted", history)
        self.assertNotIn("other-host", history)
        latest_test = history.splitlines()[1]
        self.assertEqual(float(self.cell(history, latest_test, "duration s")), 3.0)
        self.assertEqual(self.cell(history, latest_test, "status"), "pass")
        self.assertEqual(self.cell(history, latest_test, "run"), "aborted")

    def test_test_history_separates_kept_missing_and_absent_rows_without_invented_durations(self) -> None:
        kept: TestTime = {"test": "Example.test_target", "s": 0.75, "status": "pass"}
        self.write_run([module_run("test_alpha.py", 8.0, tests=[kept])])
        _ = write_config_fixture(self.config / "test_times.conf", "SLOW_TEST_S=1.0")
        self.write_run([module_run("test_alpha.py", 7.0)], epoch=EPOCH + 60)
        removed = module_run("test_alpha.py", 6.0)
        removed["tests_run"] = 12
        self.write_run([removed], epoch=EPOCH + 120)
        missing = module_run("test_alpha.py", 5.0, missing="no output")
        self.write_run([missing], epoch=EPOCH + 180)
        self.write_run([module_run("test_alpha.py", 999.0, tests=[kept])],
                       host="other-host", epoch=EPOCH + 240)
        output = self.report("test", "target")
        self.assertIn("0.75", self.line(output, "11-03 09:30"))
        for when in ("11-03 09:31", "11-03 09:32"):
            line = self.line(output, when)
            self.assertIn("not kept", line)
            self.assertNotRegex(line, r"\b\d+\.\d+\b")
        line = self.line(output, "11-03 09:33")
        self.assertIn("times missing", line)
        self.assertNotRegex(line, r"\b\d+\.\d+\b")
        self.assertNotIn("11-03 09:34", output)
        self.assertNotIn("11-03 09:30", self.report("test", "target", "--runs", "2"))

    def test_substring_history_limits_gate_runs_without_cutting_matching_tests(self) -> None:
        tests: list[TestTime] = [
            {"test": "Case.test_target_one", "s": 1.0, "status": "pass"},
            {"test": "Case.test_target_two", "s": 2.0, "status": "fail"},
        ]
        for offset in range(3):
            self.write_run([module_run("test_alpha.py", 8.0, tests=tests)], epoch=EPOCH + offset * 60)
        output = self.report("test", "target", "--runs", "2")
        self.assertNotIn("11-03 09:30", output)
        for when in ("11-03 09:32", "11-03 09:31"):
            matches = [line for line in output.splitlines() if when in line]
            self.assertEqual(len(matches), 2, output)
            for test in tests:
                self.assertEqual(sum(test["test"] in line for line in matches), 1, output)
        self.assertLess(output.index("11-03 09:32"), output.index("11-03 09:31"))

    def test_day_limits_and_suite_filters_exclude_old_and_other_suite_rows(self) -> None:
        now = datetime.now(UTC).timestamp()
        self.write_run([module_run("test_alpha.py", 8.0)], epoch=now)
        self.write_run([module_run("test_beta.py", 9.0, suite="other")], epoch=now - 60)
        self.write_run([module_run("test_alpha.py", 123.0)], epoch=946684800.0)
        suites = self.report("suites", "--days", "1")
        self.assertIn("tools/test_alpha.py", suites)
        self.assertIn("other/test_beta.py", suites)
        self.assertNotIn("01-01", suites)
        suite = self.report("suite", "tools", "--days", "1")
        self.assertIn("8.000", suite)
        self.assertNotIn("other", suite)
        self.assertNotIn("123", suite)

    def test_empty_reports_return_success_with_the_subject(self) -> None:
        for arguments, expected in zip(REPORT_COMMANDS, (
            "no modules recorded yet", "no gate runs recorded yet", "no gate runs recorded for suite tools",
            "no gate runs recorded for module tools/test_alpha.py", "no tests recorded matching target",
        ), strict=True):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.report(*arguments), expected + "\n")

    def test_reports_rebuild_a_dropped_gate_table_from_recorded_runs(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        before = self.report("module", "tools/test_alpha.py")
        with closing(sqlite3.connect(index_path_in(self.log))) as connection:
            _ = connection.execute("DROP TABLE gate_modules")
        self.assertEqual(self.report("module", "tools/test_alpha.py"), before)

    def test_reports_name_a_missing_gate_table_without_a_traceback(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = self.report("suites")
        with closing(sqlite3.connect(index_path_in(self.log))) as connection:
            _ = connection.execute("DROP TABLE gate_modules")
        for arguments in REPORT_COMMANDS:
            with self.subTest(arguments=arguments), mock.patch.object(show.index, "update", return_value=0):
                status, output, errors = run(*arguments)
                self.assertEqual(status, 1)
                self.assertEqual(output, "")
                self.assertEqual(errors.splitlines(), [
                    "test times unavailable: no such table: gate_modules; run `buildlog reindex`",
                ])

    def test_report_query_errors_propagate_after_the_table_probe(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        open_index = show.open_report_index

        def remove_table_after_probe() -> sqlite3.Connection:
            connection = open_index()
            with closing(sqlite3.connect(index_path_in(self.log))) as writer:
                writer.execute("DROP TABLE gate_modules").close()
            return connection

        with mock.patch.object(show, "open_report_index", remove_table_after_probe), \
                self.assertRaisesRegex(sqlite3.OperationalError, "no such table: gate_modules"):
            _ = run("top", "--repo", str(self.repo))

    def test_cli_imports_the_index_from_an_external_working_folder(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("show.py")), "module", "tools/test_alpha.py"],
            cwd=self.root, env=dict(os.environ), capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertIn("11-03 09:30", completed.stdout)


if __name__ == "__main__":
    _ = unittest.main()
