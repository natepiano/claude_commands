"""Test the history reports with private gate records and a private repository."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir, closing, contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, tzinfo
import io
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import subprocess
import tempfile
from threading import Event
import time
from typing import TYPE_CHECKING, BinaryIO, Self, cast, override
import unittest
from unittest import mock

from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

if TYPE_CHECKING:
    from scripts.buildlog import parse, record, repository, store
    from scripts.buildlog.index import index_path_in
    from scripts.buildlog.test_index import call, configure_cleanup, encode, step
    from scripts.lib.rerun_test_support import initialize_repository
    from scripts.lint.memory_test_support import write_config_fixture
    from scripts.production.gate_modules import discover_test_modules
    from scripts.test_times import gate_record, show
    from scripts.test_times.gate_record import GateRun, ModuleRun, TestTime
    from scripts.test_times.gate_record_test_support import fixture_executions, gate_run, remove_run_measurements
else:
    from scripts.test_times import gate_record, show
    from scripts.buildlog import parse, record, repository, store
    from scripts.buildlog.index import index_path_in
    from scripts.buildlog.test_index import call, configure_cleanup, encode, step
    from scripts.lib.rerun_test_support import initialize_repository
    from scripts.lint.memory_test_support import write_config_fixture
    from scripts.production.gate_modules import discover_test_modules
    from scripts.test_times.gate_record import GateRun, ModuleRun, TestTime
    from scripts.test_times.gate_record_test_support import fixture_executions, gate_run, remove_run_measurements

EPOCH = datetime(2026, 11, 3, 14, 30, tzinfo=UTC).timestamp()
HOST = store.host_name()
REPORT_PRINT_WAIT_SECS = 60
REPORT_COMMANDS = (("top",), ("suites",), ("suite", "tools"),
                   ("module", "tools/test_alpha.py"), ("test", "target"))

REPORT_PROCESS = """\
import os
from pathlib import Path
import runpy
import socket
import sys
report = runpy.run_path(str(Path(sys.argv[1]) / 'scripts/test_times/show.py'))
main = report['main']
main.__globals__['TOOL_REPOSITORY'] = Path(sys.argv[2])
index = report['index']
if sys.argv[3]:
    table = sys.argv[3]
    columns, key, description = index.TABLES[table]
    index.TABLES[table] = (columns + [('fixture_column', 'TEXT', 'fixture column')], key, description)
event = socket.socket(fileno=int(sys.argv[4]))
parent = os.getpid()
update = index.update
if os.environ.get('TEST_LOCKED_INDEX'):
    from contextlib import contextmanager
    update_lock = index.update_lock_in
    @contextmanager
    def report_update_lock(root):
        if os.getpid() == parent:
            event.sendall(b'U')
        with update_lock(root):
            yield
    index.update_lock_in = report_update_lock
def refresh():
    if os.getpid() != parent:
        event.sendall(b'S')
        event.recv(1)
    return update()
index.update = refresh
status = main(sys.argv[5:])
os.close(1)
os.close(2)
os.waitpid(-1, 0)
os._exit(status)
"""


class ReportTime(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(EPOCH + 300, tz)


def run(*arguments: str) -> tuple[int, str, str]:
    # These report assertions use a complete snapshot. Process tests cover refreshes.
    _ = show.index.update()
    return report_without_update(*arguments)


def report_without_update(*arguments: str) -> tuple[int, str, str]:
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors), mock.patch.object(show, "refresh_after_report"):
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
        "log": "",
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
    tool_identity: str = ""
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
        _ = configure_cleanup(self, self.log)
        _ = write_config_fixture(self.config / "test_times.conf", "TOP_RUNS=3\nTOP_CHECKOUT=~/.claude\nTOP_MAX_AGE_DAYS=7")
        _ = write_config_fixture(self.config / "buildlog.conf")
        _ = self.enterContext(mock.patch.object(show, "datetime", ReportTime))
        for name in ("test_alpha.py", "test_beta.py", "test_missing.py"):
            path = self.repo / "scripts" / "tools" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text("# A live fixture module.\n", encoding="utf-8")
        initialize_repository(self.repo, self.root / "claude-origin.git", os.environ)
        _ = self.enterContext(mock.patch.object(show, "TOOL_REPOSITORY", self.repo))
        self.tool_identity = repository.repository_key(str(self.repo))
        _ = self.enterContext(chdir(self.repo))

    def repository(self, name: str) -> tuple[Path, str]:
        path = self.root / name
        path.mkdir()
        initialize_repository(path, self.root / f"{name}-origin.git", os.environ)
        return path, repository.repository_key(str(path))

    def write_nextest(self, repository: tuple[Path, str], text: str, *, epoch: float = EPOCH,
                      host: str = HOST, call_id: str = "", caller: str = "test") -> None:
        self.next_id += 1
        path, identity = repository
        argv = ["cargo", "nextest", "run", "--workspace"]
        entry = step(
            f"nextest-{self.next_id}", step="nextest", argv=argv, host=host, call_id=call_id,
            started_at=store.utc_iso(epoch), ended_at=store.utc_iso(epoch + 30),
            cwd=str(path), worktree=str(path), repo_path=str(path), caller=caller, repo_identity=identity,
            **parse.log_facts("nextest", text),
        )
        if identity == store.UNKNOWN_IDENTITY:
            del entry["repo_path"]
        store.append_line(store.host_file(host, epoch), entry)

    def write_run(self, modules: list[ModuleRun], *, epoch: float = EPOCH,
                  host: str = HOST, aborted: bool = False, status: int = 0,
                  checkout: str = "") -> None:
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
            "checkout": checkout or str(self.repo),
            "slow_test_s": gate_record.slow_test_s(),
        }
        with mock.patch.object(record, "new_id", return_value=f"history-{self.next_id}"), \
                mock.patch.object(socket, "gethostname", return_value=host):
            gate_record.write_gate_run(run, fixture_executions(run))

    def report(self, *arguments: str) -> str:
        status, output, errors = run(*arguments)
        self.assertEqual(status, 0, output + errors)
        return output

    @contextmanager
    def report_process(self, changed_table: str = "", *arguments: str,
                       wait_for_refresh: bool = True) -> Generator[tuple[subprocess.Popen[bytes], socket.socket]]:
        event, child_event = socket.socketpair()
        event.settimeout(TEST_HANG_GUARD_SECS)
        process = subprocess.Popen(
            [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", REPORT_PROCESS, str(Path(__file__).resolve().parents[2]),
             str(self.repo), changed_table, str(child_event.fileno()),
             *(arguments or ("top", "--repo", str(self.repo)))],
            cwd=self.root, env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(child_event.fileno(),),
        )
        child_event.close()
        try:
            if wait_for_refresh:
                self.assertEqual(event.recv(1), b"S")
            yield process, event
        finally:
            try:
                event.sendall(b"F")
            except BrokenPipeError:
                pass
            event.close()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0)

    def report_pipe(self, pipe: BinaryIO) -> str:
        chunks: list[bytes] = []
        while True:
            ready, _, _ = select.select([pipe], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(ready, "The report pipe stayed open during the refresh")
            chunk = os.read(pipe.fileno(), 65536)
            if not chunk:
                return b"".join(chunks).decode()
            chunks.append(chunk)

    def test_first_report_closes_its_pipe_before_refresh_and_later_report_has_new_rows(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = show.index.update()
        with closing(sqlite3.connect(index_path_in(self.log))) as writer, writer:
            writer.execute("UPDATE meta SET value=? WHERE key='updated_at'", (str(EPOCH),)).close()
        self.write_run([module_run("test_beta.py", 9.0)], epoch=EPOCH + 60)
        with self.report_process() as (process, event):
            first = self.report_pipe(cast(BinaryIO, process.stdout))
            errors = self.report_pipe(cast(BinaryIO, process.stderr))
            self.assertIn("tools/test_alpha.py", first)
            self.assertNotIn("tools/test_beta.py", first.split("unmeasured:")[0])
            self.assertEqual(errors, "rows as of 11-03 09:30 EST\n")
            self.assertIsNone(process.poll(), "The supervisor must still wait for its refresh child")
            event.sendall(b"F")
        with self.report_process() as (process, event):
            later = self.report_pipe(cast(BinaryIO, process.stdout))
            self.assertIn("tools/test_beta.py", later.split("unmeasured:")[0])
            event.sendall(b"F")

    def test_report_reads_complete_rows_during_a_refresh_larger_than_five_mib(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = show.index.update()
        program = """
import socket
import sys
from scripts.buildlog import index
with socket.socket(fileno=int(sys.argv[2])) as event, index.update_lock():
    connection = index.open_for_update(index.index_path())
    try:
        connection.execute("BEGIN IMMEDIATE").close()
        connection.execute("UPDATE gate_modules SET times_missing=?", ("x" * (6 * 1024 * 1024),)).close()
        event.sendall(b"W")
        event.recv(1)
        connection.execute("ROLLBACK").close()
    finally:
        connection.close()
"""
        event, child_event = socket.socketpair()
        with event, child_event:
            event.settimeout(TEST_HANG_GUARD_SECS)
            writer = subprocess.Popen(
                [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", program, str(Path(__file__).resolve().parents[2]),
                 str(child_event.fileno())], env=dict(os.environ), pass_fds=(child_event.fileno(),),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(event.recv(1), b"W")
                with self.report_process(wait_for_refresh=False) as (process, refresh):
                    try:
                        ready, _, _ = select.select([cast(BinaryIO, process.stdout)], [], [], REPORT_PRINT_WAIT_SECS)
                        self.assertTrue(ready, "The list must print while the refresh holds its changed rows")
                        output = self.report_pipe(cast(BinaryIO, process.stdout))
                        self.assertIn("tools/test_alpha.py", output.split("unmeasured:")[0])
                        self.assertEqual(refresh.recv(1), b"S")
                    finally:
                        event.sendall(b"F")
                        output, errors = writer.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(writer.returncode, 0, output + errors)
            finally:
                if writer.poll() is None:
                    writer.terminate()
                    _ = writer.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_report_reads_an_older_index_while_a_non_gate_schema_builds(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = show.index.update()
        self.write_run([module_run("test_beta.py", 9.0)], epoch=EPOCH + 60)
        with self.report_process("steps") as (process, event):
            output = self.report_pipe(cast(BinaryIO, process.stdout))
            self.assertIn("tools/test_alpha.py", output)
            self.assertNotIn("tools/test_beta.py", output.split("unmeasured:")[0])
            event.sendall(b"F")

    def test_report_keeps_index_only_rows_after_an_exclusive_lock_exceeds_the_read_wait(self) -> None:
        _ = write_config_fixture(self.config / "buildlog.conf", "INDEX_READ_WAIT_SECONDS=0.05")
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = show.index.update()
        with closing(sqlite3.connect(index_path_in(self.log))) as connection, connection:
            connection.execute("UPDATE gate_modules SET module='test_beta.py'").close()
        program = """
import socket
import sys
from scripts.buildlog import index
with socket.socket(fileno=int(sys.argv[2])) as event, index.update_lock():
    connection = index.open_for_update(index.index_path())
    try:
        connection.execute("PRAGMA locking_mode = EXCLUSIVE").close()
        connection.execute("BEGIN EXCLUSIVE").close()
        event.sendall(b'W')
        event.recv(1)
        connection.execute("COMMIT").close()
    finally:
        connection.close()
"""
        event, child_event = socket.socketpair()
        with event, child_event:
            event.settimeout(TEST_HANG_GUARD_SECS)
            writer = subprocess.Popen(
                [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", program, str(Path(__file__).resolve().parents[2]),
                 str(child_event.fileno())], env=dict(os.environ), pass_fds=(child_event.fileno(),),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(event.recv(1), b"W")
                with closing(sqlite3.connect(show.index.read_only_uri(self.log), uri=True,
                        timeout=conf.read("buildlog").number("INDEX_READ_WAIT_SECONDS"))) as reader:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                        reader.execute("SELECT COUNT(*) FROM gate_modules").close()
                with mock.patch.dict(os.environ, {"TEST_LOCKED_INDEX": "1"}), \
                        self.report_process(wait_for_refresh=False) as (process, refresh):
                    try:
                        self.assertEqual(refresh.recv(1), b"U")
                    finally:
                        event.sendall(b"F")
                        output, errors = writer.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(writer.returncode, 0, output + errors)
                    output = self.report_pipe(cast(BinaryIO, process.stdout))
                    self.assertIn("tools/test_beta.py", output.split("unmeasured:")[0])
                    self.assertEqual(refresh.recv(1), b"S")
                with closing(show.index.read_only()) as reader:
                    self.assertEqual(reader.execute("SELECT module FROM gate_modules").fetchall(),
                                     [("test_beta.py",)])
            finally:
                if writer.poll() is None:
                    writer.terminate()
                    _ = writer.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_report_waits_for_an_exclusive_lock_taken_after_index_selection(self) -> None:
        _ = write_config_fixture(self.config / "buildlog.conf", "INDEX_READ_WAIT_SECONDS=0.01")
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = show.index.update()
        select_index = show.index.complete_gate_index_in
        selected = Event()
        locked = Event()
        reader_checked = Event()

        def hold_lock_after_selection() -> None:
            self.assertTrue(selected.wait(TEST_HANG_GUARD_SECS))
            with closing(sqlite3.connect(index_path_in(self.log))) as writer:
                writer.execute("PRAGMA locking_mode = EXCLUSIVE").close()
                writer.execute("BEGIN EXCLUSIVE").close()
                locked.set()
                self.assertTrue(reader_checked.wait(TEST_HANG_GUARD_SECS))
                # Hold the lock longer than the selection timeout.
                time.sleep(0.2)
                writer.execute("COMMIT").close()

        def select_before_writer(root: Path) -> Path | show.index.NoCompleteGateIndex:
            path = select_index(root)
            selected.set()
            self.assertTrue(locked.wait(TEST_HANG_GUARD_SECS))
            try:
                with closing(sqlite3.connect(show.index.read_only_uri(self.log), uri=True,
                        timeout=conf.read("buildlog").number("INDEX_READ_WAIT_SECONDS"))) as reader:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                        reader.execute("SELECT COUNT(*) FROM gate_modules").close()
            finally:
                reader_checked.set()
            return path

        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(hold_lock_after_selection)
            try:
                with mock.patch.object(show.index, "complete_gate_index_in", select_before_writer):
                    status, output, errors = report_without_update("top")
            finally:
                selected.set()
                reader_checked.set()
                writer.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(status, 0, errors)
        self.assertIn("tools/test_alpha.py", output.split("unmeasured:")[0])

    def test_report_builds_before_reading_when_gate_sql_changes(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = show.index.update()
        self.write_run([module_run("test_beta.py", 9.0)], epoch=EPOCH + 60)
        with self.report_process("gate_runs") as (process, event):
            output = self.report_pipe(cast(BinaryIO, process.stdout))
            self.assertIn("tools/test_beta.py", output.split("unmeasured:")[0])
            event.sendall(b"F")

    def test_report_keeps_its_rows_and_timestamp_from_the_same_update(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        with mock.patch.object(time, "time", return_value=EPOCH):
            _ = show.index.update()
        self.write_run([module_run("test_alpha.py", 9.0)], epoch=EPOCH + 60)
        report_module = show.module
        committing = Event()
        open_writer = show.index.open_for_update

        def writer_with_commit_signal(path: Path) -> sqlite3.Connection:
            writer = open_writer(path)
            def trace(statement: str) -> None:
                if statement == "COMMIT":
                    committing.set()
            writer.set_trace_callback(trace)
            return writer

        def publish_after_rows(connection: sqlite3.Connection, host: str, subject: str, runs: int) -> None:
            report_module(connection, host, subject, runs)
            _ = pool.submit(show.index.update)
            self.assertTrue(committing.wait(TEST_HANG_GUARD_SECS))

        with ThreadPoolExecutor(max_workers=1) as pool, \
                mock.patch.object(time, "time", return_value=EPOCH + 60), \
                mock.patch.object(show.index, "open_for_update", writer_with_commit_signal), \
                mock.patch.object(show, "module", publish_after_rows):
            status, output, errors = report_without_update("module", "tools/test_alpha.py")
        self.assertEqual(status, 0, errors)
        self.assertIn("11-03 09:30", output)
        self.assertNotIn("11-03 09:31", output)
        self.assertEqual(errors, "rows as of 09:30 EST\n")
        with closing(show.index.read_only()) as reader:
            self.assertEqual(reader.execute("SELECT count(*) FROM gate_runs").fetchone(), (2,))
            self.assertEqual(reader.execute("SELECT value FROM meta WHERE key='updated_at'").fetchone(),
                             (str(EPOCH + 60),))

    def test_report_replaces_an_invalid_current_completion_time(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = show.index.update()
        with closing(sqlite3.connect(index_path_in(self.log))) as writer, writer:
            writer.execute("UPDATE meta SET value='invalid' WHERE key='updated_at'").close()
        with mock.patch.object(time, "time", return_value=EPOCH):
            status, output, errors = report_without_update("module", "tools/test_alpha.py")
        self.assertEqual(status, 0, errors)
        self.assertIn("11-03 09:30", output)
        self.assertEqual(errors, "rows as of 09:30 EST\n")

    def test_freshness_line_dates_updates_from_another_local_day(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = show.index.update()
        for completed, expected in ((EPOCH, "09:30 EST"), (EPOCH - 86400, "11-02 09:30 EST")):
            with self.subTest(completed=completed):
                with closing(sqlite3.connect(index_path_in(self.log))) as writer, writer:
                    writer.execute("UPDATE meta SET value=? WHERE key='updated_at'", (str(completed),)).close()
                status, output, errors = report_without_update("module", "tools/test_alpha.py")
                self.assertEqual(status, 0, errors)
                self.assertIn("11-03 09:30", output)
                self.assertEqual(errors, f"rows as of {expected}\n")

    def line(self, output: str, subject: str) -> str:
        lines = [line for line in output.splitlines() if subject in line]
        self.assertEqual(len(lines), 1, output)
        return lines[0]

    def cell(self, output: str, row: str, header: str) -> str:
        for line in output.splitlines():
            headings = list(re.finditer(r"\S.*?(?= {2,}|$)", line))
            names = [match.group() for match in headings]
            if header in names:
                break
        else:
            self.fail(f"The report has no {header} column: {output}")
        index = names.index(header)
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
        beta_run = module_run("test_beta.py", 1.0)
        beta_run["cpu_s"] = 3.5
        self.write_run([
            module_run("test_alpha.py", 1000.0, restart="hold", status=-15),
            module_run("test_alpha.py", 2.0, attempt=2, tests=[slow]),
            module_run("test_alpha.py", 0.01, attempt=3),
            beta_run, module_run("test_removed.py", 999.0), types,
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
        self.assertEqual(numbers, [2, 2, 4, 2, 13, 1.25, 3], alpha)
        self.assertRegex(self.line(output, "sum of median CPU"), r"3 modules; sum of median CPU 7(?:\.0+)? s")
        self.assertEqual(self.report(), output)
        limited = self.report("top", "--limit", "1", "--repo", str(self.repo))
        self.assertIn("tools/test_beta.py", limited)
        self.assertNotIn("tools/test_alpha.py", limited)
        _ = write_config_fixture(self.config / "test_times.conf", "TOP_RUNS=1")
        changed = self.report("top", "--repo", str(self.repo))
        self.assertLess(changed.index("types/basedpyright"), changed.index("tools/test_alpha.py"))

    def test_top_ranks_only_the_working_repository_on_the_current_host_within_the_age_limit(self) -> None:
        hana = self.repository("hana")
        other = self.repository("other")
        for offset, duration in enumerate((2.0, 8.0, 4.0)):
            self.write_nextest(hana, f"        PASS [ {duration:.3f}s] hana-bin tests::shared\n",
                               epoch=EPOCH + offset * 60)
        self.write_nextest(hana, "        PASS [ 6.000s] other-bin tests::shared\n")
        self.write_nextest(other, "        PASS [ 900.000s] hana-bin tests::shared\n"
                           + "        PASS [ 900.000s] other-bin tests::other_repository\n")
        self.write_nextest(hana, "        PASS [ 800.000s] hana-bin tests::shared\n"
                           + "        PASS [ 800.000s] hana-bin tests::other_host\n", host="other-host")
        self.write_nextest(hana, "        PASS [ 700.000s] hana-bin tests::shared\n"
                           + "        PASS [ 700.000s] hana-bin tests::old\n", epoch=EPOCH - 8 * 86400)
        folder = hana[0] / "crates" / "widget"
        folder.mkdir(parents=True)
        with chdir(folder):
            output = self.report("top")
        with chdir(other[0]):
            self.assertEqual(self.report("top", "--repo", str(hana[0])), output)
        first = self.line(output, "other-bin")
        second = self.line(output, "hana-bin")
        self.assertLess(output.index(first), output.index(second))
        self.assertEqual(self.cell(output, first, "median s"), "6.000")
        self.assertEqual(self.cell(output, second, "median s"), "4.000")
        self.assertEqual(self.cell(output, second, "runs kept"), "3")
        for excluded in ("other_repository", "other_host", "tests::old"):
            self.assertNotIn(excluded, output)
        self.assertIn(hana[1], output)
        self.assertIn(HOST, output)
        self.assertIn("1.000", output)

    def test_top_uses_the_same_relative_origin_from_the_root_and_a_subdirectory(self) -> None:
        hana = self.repository("hana")
        _ = subprocess.run(["git", "-C", str(hana[0]), "remote", "set-url", "origin", "../hana-origin.git"],
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.write_nextest(hana, "        PASS [ 2.000s] hana tests::relative_origin\n")
        nested = hana[0] / "crates" / "widget"
        nested.mkdir(parents=True)
        with chdir(hana[0]):
            expected = self.report("top")
        with chdir(nested):
            output = self.report("top")
        self.assertEqual(output, expected)
        self.assertEqual(self.cell(output, self.line(output, "tests::relative_origin"), "median s"), "2.000")

    def test_top_selects_mac_history_on_natedev_only_with_the_host_argument(self) -> None:
        hana = self.repository("hana")
        mac_host = "Nates-MacBook-Pro-2023"
        self.write_nextest(hana, "        PASS [ 2.000s] hana tests::linux\n", host="natedev")
        self.write_nextest(hana, "        PASS [ 4.000s] hana tests::mac\n", host=mac_host)
        with chdir(hana[0]), mock.patch.object(socket, "gethostname", return_value="natedev"):
            local = self.report("top")
            remote = self.report("top", "--host", mac_host)
        self.assertIn("tests::linux", local)
        self.assertNotIn("tests::mac", local)
        self.assertIn("host: natedev;", local)
        self.assertNotIn("tests::linux", remote)
        self.assertIn(f"host: {mac_host};", remote)
        self.assertEqual(self.cell(remote, self.line(remote, "tests::mac"), "median s"), "4.000")

    def test_nextest_heading_reads_the_recording_threshold_at_each_report(self) -> None:
        hana = self.repository("hana")
        self.write_nextest(hana, "        PASS [ 2.000s] hana tests::recorded\n")
        with chdir(hana[0]):
            before = self.report("top")
            _ = write_config_fixture(self.config / "test_times.conf", "NEXTEST_SLOW_TEST_S=3.0")
            after = self.report("top")
        self.assertIn("recorded threshold: > 1.000 s", before)
        self.assertIn("recorded threshold: > 3.000 s", after)
        self.assertEqual(self.cell(after, self.line(after, "tests::recorded"), "median s"), "2.000")

    def test_top_keeps_the_module_view_from_the_claude_working_directory(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        self.write_nextest((self.repo, self.tool_identity), "        PASS [ 40.000s] claude-bin tests::nextest\n")
        expected = self.report("top", "--repo", str(self.repo))
        with chdir(self.repo / "scripts" / "tools"):
            output = self.report("top")
        self.assertEqual(output, expected)
        self.assertEqual(self.cell(output, self.line(output, "tools/test_alpha.py"), "median s"), "8.000")
        self.assertNotIn("claude-bin", output)

    def test_top_ranks_a_mac_mirror_test_under_the_repository_of_its_natedev_call(self) -> None:
        hana, identity = self.repository("hana")
        mirror = self.root / "mirror"
        mirror.mkdir()
        mac_host = "Nates-MacBook-Pro-2023"
        self.write_nextest((mirror, store.UNKNOWN_IDENTITY), "        PASS [ 2.000s] hana tests::mac_mirror\n",
                           host=mac_host, call_id="served-call", caller="verify-mac")
        for entry in (
            call("served-call", repo_path=str(hana)),
            step("repository-evidence", call_id="served-call", repo_path=str(hana),
                 repo_identity=identity),
        ):
            store.append_line(store.host_file("natedev", EPOCH), entry)
        with chdir(hana), mock.patch.object(socket, "gethostname", return_value=mac_host):
            output = self.report("top")
        row = self.line(output, "tests::mac_mirror")
        self.assertEqual(self.cell(output, row, "median s"), "2.000")
        self.assertEqual(self.cell(output, row, "runs kept"), "1")
        self.assertIn(identity, output)
        self.assertIn(mac_host, output)

    def test_top_uses_passed_durations_and_places_failure_only_tests_last_by_time(self) -> None:
        hana = self.repository("hana")
        self.write_nextest(hana, "        PASS [ 2.000s] hana tests::mixed\n"
                           + "        PASS [ 1.250s] hana tests::fast\n")
        self.write_nextest(hana, "  TRY 2 PASS [ 4.000s] hana tests::mixed\n", epoch=EPOCH + 30)
        self.write_nextest(hana, "        FAIL [ 90.000s] hana tests::mixed\n", epoch=EPOCH + 60)
        self.write_nextest(hana, "     TIMEOUT [ 80.000s] hana tests::mixed\n"
                           + "     TIMEOUT [ 70.000s] hana tests::timeout_only\n", epoch=EPOCH + 120)
        self.write_nextest(hana, "        FAIL [ 60.000s] hana tests::failure_only\n", epoch=EPOCH + 180)
        with chdir(hana[0]):
            output = self.report("top")
        ordered = [self.line(output, name)
                   for name in ("tests::mixed", "tests::fast", "tests::timeout_only", "tests::failure_only")]
        positions = [output.index(line) for line in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(self.cell(output, ordered[0], "median s"), "3.000")
        self.assertEqual(self.cell(output, ordered[0], "runs kept"), "4")
        self.assertEqual(self.cell(output, ordered[0], "failed / timed out"), "2")
        for line in ordered[2:]:
            self.assertEqual(self.cell(output, line, "median s"), "")
            self.assertEqual(self.cell(output, line, "runs kept"), "1")
            self.assertEqual(self.cell(output, line, "failed / timed out"), "1")
        self.assertEqual(self.cell(output, ordered[0], "last s"), "80.000")
        self.assertEqual(self.cell(output, ordered[2], "last s"), "70.000")
        self.assertEqual(self.cell(output, ordered[3], "last s"), "60.000")

    def test_top_excludes_failed_aborted_old_and_other_checkout_rows_before_the_run_limit(self) -> None:
        self.write_run([module_run("test_alpha.py", 4.0), module_run("test_beta.py", 6.0)])
        rejected = (
            (module_run("test_alpha.py", 900.0, status=1), EPOCH + 60, False, str(self.repo)),
            (module_run("test_alpha.py", 800.0), EPOCH + 120, True, str(self.repo)),
            (module_run("test_alpha.py", 700.0), EPOCH + 300 - 7 * 86400 - 1, False, str(self.repo)),
            (module_run("test_alpha.py", 600.0), EPOCH + 180, False, str(self.root / "other")),
        )
        for module, epoch, aborted, checkout in rejected:
            self.write_run([module], epoch=epoch, aborted=aborted, checkout=checkout)
        output = self.report("top", "--repo", str(self.repo))
        alpha = self.line(output, "tools/test_alpha.py")
        beta = self.line(output, "tools/test_beta.py")
        self.assertLess(output.index(beta), output.index(alpha), output)
        for header, expected in (("median s", "4.000"), ("last s", "4.000"),
                                 ("median CPU s", "2.000"), ("runs counted", "1")):
            self.assertEqual(self.cell(output, alpha, header), expected, output)

    def test_top_reads_history_when_the_configured_checkout_is_a_symlink(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)], checkout=str(self.repo.resolve()))
        alias = self.repo.with_name("checkout-alias")
        alias.symlink_to(self.repo, target_is_directory=True)
        _ = write_config_fixture(self.config / "test_times.conf", "TOP_CHECKOUT=~/checkout-alias")
        output = self.report("top", "--repo", str(self.repo))
        alpha = self.line(output, "tools/test_alpha.py")
        self.assertEqual(self.cell(output, alpha, "median s"), "8.000")
        self.assertEqual(self.cell(output, alpha, "runs counted"), "1")

    def test_top_reads_checkout_and_age_settings_at_each_report(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)], epoch=EPOCH - 2 * 86400)
        self.write_run([module_run("test_beta.py", 9.0)], checkout=str(self.root / "other"))
        output = self.report("top", "--repo", str(self.repo))
        self.assertEqual(self.cell(output, self.line(output, "tools/test_alpha.py"), "median s"), "8.000")
        _ = write_config_fixture(self.config / "test_times.conf", "TOP_MAX_AGE_DAYS=1")
        output = self.report("top", "--repo", str(self.repo))
        self.assertIn("no modules recorded yet", output)
        _ = write_config_fixture(self.config / "test_times.conf", f"TOP_CHECKOUT={self.root / 'other'}")
        output = self.report("top", "--repo", str(self.repo))
        self.assertEqual(self.cell(output, self.line(output, "tools/test_beta.py"), "median s"), "9.000")

    def test_top_names_unmeasured_live_modules_without_counting_the_display_limit(self) -> None:
        nested = self.repo / "scripts" / "tools" / "nested" / "test_deep.py"
        nested.parent.mkdir()
        _ = nested.write_text("# A live fixture module.\n", encoding="utf-8")
        self.write_run([module_run("test_alpha.py", 4.0), module_run("test_beta.py", 6.0),
                        module_run("nested/test_deep.py", 10.0, status=1)])
        output = self.report("top", "--limit", "1", "--repo", str(self.repo))
        self.assertEqual(self.line(output, "unmeasured:"),
                         "unmeasured: 2 live modules: tools/nested/test_deep.py, tools/test_missing.py")
        self.write_run([module_run("nested/test_deep.py", 3.0), module_run("test_missing.py", 2.0)])
        self.assertNotIn("unmeasured:", self.report("top", "--limit", "1", "--repo", str(self.repo)))

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
        self.assertEqual(self.cell(suites, latest_run, "tests run"), "26")
        self.assertEqual(self.cell(suites, self.line(suites, "11-03 09:31"), "tests run"), "0")
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
        self.assertEqual(self.cell(top, self.line(top, "tools/test_alpha.py"), "median s"), "8.000")
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
        _ = write_config_fixture(self.config / "test_times.conf", "SLOW_TEST_S=2.0")
        output = self.report("test", "target")
        self.assertIn("0.75", self.line(output, "11-03 09:30"))
        for when in ("11-03 09:31", "11-03 09:32"):
            line = self.line(output, when)
            self.assertIn("not kept", line)
            self.assertEqual(self.cell(output, line, "duration s"), "")
            self.assertIn("not kept (faster than 1.000 s or did not run)", self.cell(output, line, "status"))
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

    def test_legacy_histories_leave_counts_blank_and_name_the_unknown_threshold(self) -> None:
        kept: TestTime = {"test": "Example.test_target", "s": 0.75, "status": "pass"}
        self.write_run([module_run("test_alpha.py", 8.0, tests=[kept])])
        self.write_run([module_run("test_alpha.py", 7.0)], epoch=EPOCH + 60)
        path = next(self.log.glob("*/*.jsonl"))
        entries = list(store.locked_rows(path))
        for entry in entries:
            remove_run_measurements(entry)
        _ = path.write_bytes(b"".join(encode(entry) for entry in entries))
        _ = write_config_fixture(self.config / "test_times.conf", "SLOW_TEST_S=9.0")
        suites = self.report("suites", "--days", "100000")
        for when in ("11-03 09:30", "11-03 09:31"):
            self.assertEqual(self.cell(suites, self.line(suites, when), "tests run"), "")
        output = self.report("test", "target")
        absent = self.line(output, "11-03 09:31")
        self.assertEqual(self.cell(output, absent, "duration s"), "")
        self.assertIn("not kept", self.cell(output, absent, "status"))
        self.assertIn("unknown", self.cell(output, absent, "status"))
        self.assertNotIn("9.000", absent)
        self.assertEqual(self.cell(output, self.line(output, "11-03 09:30"), "duration s"), "0.750")

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
            "no modules recorded yet\nunmeasured: 3 live modules: tools/test_alpha.py, tools/test_beta.py, tools/test_missing.py",
            "no gate runs recorded yet", "no gate runs recorded for suite tools",
            "no gate runs recorded for module tools/test_alpha.py", "no tests recorded matching target",
        ), strict=True):
            with self.subTest(arguments=arguments):
                status, output, errors = report_without_update(*arguments)
                self.assertEqual(status, 0, errors)
                self.assertEqual(output, expected + "\n")

    def test_reports_rebuild_a_dropped_gate_table_from_recorded_runs(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        before = self.report("module", "tools/test_alpha.py")
        with closing(sqlite3.connect(index_path_in(self.log))) as connection:
            _ = connection.execute("DROP TABLE gate_modules")
        status, output, errors = report_without_update("module", "tools/test_alpha.py")
        self.assertEqual(status, 0, errors)
        self.assertEqual(output, before)

    def test_reports_name_a_missing_gate_table_without_a_traceback(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        _ = self.report("suites")
        with closing(sqlite3.connect(index_path_in(self.log))) as connection:
            _ = connection.execute("DROP TABLE gate_modules")
        for arguments in REPORT_COMMANDS:
            with self.subTest(arguments=arguments), mock.patch.object(show.index, "update", return_value=0):
                status, output, errors = report_without_update(*arguments)
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
            _ = report_without_update("top", "--repo", str(self.repo))

    def test_cli_imports_the_index_from_an_external_working_folder(self) -> None:
        self.write_run([module_run("test_alpha.py", 8.0)])
        event, child_event = socket.socketpair()
        event.settimeout(TEST_HANG_GUARD_SECS)
        try:
            completed = subprocess.run(
                [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).with_name("show.py")), "module", "tools/test_alpha.py"],
                cwd=self.root, env=dict(os.environ), capture_output=True, text=True, check=False,
                pass_fds=(child_event.fileno(),), timeout=TEST_HANG_GUARD_SECS,
            )
            child_event.close()
            self.assertEqual(event.recv(1), b"", "The refresh must end before fixture cleanup")
        finally:
            child_event.close()
            event.close()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertRegex(completed.stderr, r"^rows as of \d\d:\d\d (EST|EDT)\n$")
        self.assertIn("11-03 09:30", completed.stdout)


if __name__ == "__main__":
    _ = unittest.main()
