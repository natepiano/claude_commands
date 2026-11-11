"""Gate history queries use the index and preserve recorded attempts."""

from __future__ import annotations

from contextlib import closing, redirect_stdout
import gzip
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import time
from typing import cast, override
import unittest
from unittest import mock

from scripts.buildlog import cli, index
from scripts.buildlog.index import GATE_TABLES
from scripts.buildlog.test_index import Record, configure_cleanup, encode, point_root_at, rows, table_rows, use_test_log

use_test_log()

from scripts.buildlog import record, store
from scripts.test_times import gate_record
from scripts.test_times.gate_record import TYPE_CHECK_JOB, GateRun, ModuleRun
from scripts.test_times.gate_record_test_support import fixture_executions, gate_run, remove_run_measurements
from scripts.lint.memory_test_support import write_config_fixture

EPOCH = 1_793_700_000.0
STARTED = "2026-11-03T12:00:00.000Z"


def fixture_run() -> GateRun:
    final_attempt: ModuleRun = {
        "suite": "scripts", "module": "test_shared.py", "attempt": 2,
        "restart": "", "times_missing": "", "wall_s": 4.0, "cpu_s": 3.0,
        "log": "",
        "peak_mib": 25.0, "status": 1, "tests_run": 7, "failures": 1,
        "errors": 0, "skipped": 1, "tests_s": 2.5,
        "tests": [{"test": "Case.test_shared", "s": 0.75, "status": "pass"},
                  {"test": "Case.test_fail", "s": 0.01, "status": "fail"}],
    }
    held_attempt: ModuleRun = {
        **final_attempt, "attempt": 1, "restart": "hold", "wall_s": 1.0,
        "cpu_s": 0.5, "peak_mib": 15.0, "status": 143, "tests_run": 100,
        "tests_s": 0.3, "skipped": 0,
        "tests": [{"test": "Case.test_shared", "s": 0.3, "status": "fail"}],
    }
    other_suite: ModuleRun = {
        **final_attempt, "suite": "tools", "attempt": 1, "tests_run": 4,
        "errors": 1, "failures": 0, "skipped": 0, "tests_s": 1.5,
        "tests": [{"test": "Case.test_shared", "s": 1.5, "status": "error"}],
    }
    missing: ModuleRun = {
        **final_attempt, "module": "test_missing.py", "attempt": 1,
        "times_missing": "process ended before output", "status": 137,
        "tests_run": 0, "failures": 0, "skipped": 0, "tests_s": 0.0, "tests": [],
    }
    types: ModuleRun = {
        **missing, "suite": TYPE_CHECK_JOB[0], "module": TYPE_CHECK_JOB[1], "status": 0,
        "times_missing": "", "tests_run": 0,
    }
    return {
        **gate_run(), "ended_at": "2026-11-03T12:00:10.000Z",
        "wall_s": 10.0, "hold_wait_s": 2.0, "reason": "fixture changes", "max_jobs": 3,
        "peak_mib": 45.0, "cap_mib": 128.0, "source": "checkpoint", "aborted": True,
        "suites": [{"suite": "scripts", "wall_s": 6.0, "status": 137},
                   {"suite": "tools", "wall_s": 3.0, "status": 1},
                   {"suite": TYPE_CHECK_JOB[0], "wall_s": 1.0, "status": 0}],
        "modules": [final_attempt, held_attempt, other_suite, missing, types],
    }


def run_rows(table: str, run_id: str) -> list[Record]:
    return table_rows(table, f'WHERE {"id" if table == "gate_runs" else "run_id"} = ?', run_id)


class GateIndexTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        point_root_at(self, self.root)
        config = configure_cleanup(self, self.root)
        _ = write_config_fixture(config / "test_times.conf")

    def write_run(self, identifier: str, run: GateRun, host: str = "fixture-host") -> str:
        with mock.patch.object(record, "new_id", return_value=identifier), \
                mock.patch.object(store, "host_name", return_value=host), \
                mock.patch.object(time, "time", return_value=EPOCH):
            gate_record.write_gate_run(run, fixture_executions(run))
        path = store.host_file(host, EPOCH)
        self.assertTrue(path.is_file(), "The fixture writer must produce the source record")
        return path.relative_to(self.root).as_posix()

    def snapshot(self) -> dict[str, list[Record]]:
        return {table: table_rows(table) for table in GATE_TABLES}

    def test_missing_gate_tables_recover_all_recorded_attempts(self) -> None:
        source = self.write_run("recovered", fixture_run())
        self.assertEqual(index.update(), 1)
        before = self.snapshot()
        original = (self.root / source).read_bytes()
        table = GATE_TABLES[0]
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute(f"DROP TABLE {table}")
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(index.update(), 0)
        self.assertEqual((self.root / source).read_bytes(), original)

    def test_gate_rows_preserve_fields_attempts_and_recorded_attempt_totals(self) -> None:
        run = fixture_run()
        source = self.write_run("gate-one", run)
        self.assertEqual(index.update(), 1)
        common: Record = {"src": source, "host": "fixture-host", "started_at": STARTED}
        expected_run: Record = {
            **{key: value for key, value in run.items() if key not in {"suites", "modules", "dirty_paths", "dirty_path_count"}},
            **common, "id": "gate-one", "aborted": 1, "dirty": 1,
            "modules_run": 3, "tests_run": 111,
        }
        self.assertEqual(table_rows("gate_runs"), [expected_run])
        child: Record = {**common, "run_id": "gate-one"}
        expected_suites = [{**suite, **child} for suite in run["suites"]]
        self.assertCountEqual(table_rows("gate_suites"), expected_suites)
        expected_modules = [{**{key: value for key, value in module.items() if key != "tests"}, **child}
                            for module in run["modules"]]
        self.assertCountEqual(table_rows("gate_modules"), expected_modules)
        expected_tests: list[Record] = []
        for module in run["modules"]:
            for test in module["tests"]:
                expected_tests.append({
                    **child, "suite": module["suite"], "module": module["module"],
                    "attempt": module["attempt"], "test": test["test"],
                    "duration_s": test["s"], "status": test["status"],
                })
        self.assertCountEqual(table_rows("gate_tests"), expected_tests)
        before = self.snapshot()
        with closing(index.read_only()) as connection:
            version = cast(tuple[int], connection.execute("PRAGMA data_version").fetchone())
            self.assertEqual(index.update(), 0)
            self.assertEqual(cast(tuple[int], connection.execute("PRAGMA data_version").fetchone()), version,
                             "An unchanged update must not write the database")
        self.assertEqual(self.snapshot(), before)

    def test_gate_index_reads_absent_run_fields_as_null_without_deriving_them(self) -> None:
        source = self.write_run("legacy", fixture_run())
        path = self.root / source
        entry = next(store.locked_rows(path))
        remove_run_measurements(entry)
        del entry["step"]
        for module in cast(list[Record], entry["modules"]):
            del module["log"]
        _ = path.write_bytes(encode(entry))
        original = path.read_bytes()
        self.assertEqual(index.update(), 1)
        self.assertEqual(rows("SELECT slow_test_s, tests_run, modules_run FROM gate_runs"), [(None, None, 3)])
        self.assertEqual(rows("SELECT step FROM gate_runs"), [("",)])
        self.assertEqual(rows("SELECT log FROM gate_modules"), [(None,)] * len(fixture_run()["modules"]))
        self.assertEqual(rows("SELECT tests_run FROM gate_modules WHERE suite = ? AND attempt = ?"
                              + " AND module = ?", "scripts", 2, "test_shared.py"), [(7,)])
        self.assertEqual(path.read_bytes(), original)

    def test_query_recovers_failure_log_paths_after_an_index_rebuild(self) -> None:
        _ = write_config_fixture(Path(os.environ["CONF_DIR"]) / "test_times.conf")
        output = "Traceback (most recent call last):\nAssertionError: retained failure\n"
        executions = [gate_record.Execution("scripts", "test_failure.py", 1.0, 0.5, 10.0, 1, "",
                                             gate_record.NoModuleTimes("runner stopped"), output)]
        run = gate_run()
        run["modules"] = gate_record.module_runs(executions, 0.5)
        gate_record.write_gate_run(run, executions)
        self.assertEqual(index.update(), 1)
        with closing(sqlite3.connect(index.index_path())) as connection, connection:
            connection.execute("DROP TABLE gate_modules").close()
        printed = io.StringIO()
        with redirect_stdout(printed):
            status = cli.main(["query", "--json", "SELECT log FROM gate_modules"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(printed.getvalue()), [{"log": run["modules"][0]["log"]}])
        kept = gzip.decompress((self.root / run["modules"][0]["log"]).read_bytes())
        self.assertTrue(kept.endswith(output.encode("utf-8")))

    def test_query_text_aligns_numbers_and_prints_empty_results(self) -> None:
        _ = self.write_run("table", fixture_run())
        cases = (
            ("SELECT 7 AS runs, 'alpha' AS label UNION ALL SELECT 123, 'b' UNION ALL SELECT NULL, 'gamma'",
             "runs  label\n   7  alpha\n 123  b\n   —  gamma\n"),
            ("SELECT tests_run AS runs, reason AS label FROM gate_runs WHERE id = 'absent'", "no rows\n"),
        )
        for sql, expected in cases:
            with self.subTest(sql=sql):
                printed = io.StringIO()
                with redirect_stdout(printed):
                    status = cli.main(["query", sql])
                self.assertEqual(status, 0)
                self.assertEqual(printed.getvalue(), expected)

    def test_reused_run_has_zero_counts_and_no_child_rows(self) -> None:
        run = fixture_run()
        run.update({"selection": "reused", "aborted": False, "dirty": False, "suites": [], "modules": []})
        _ = self.write_run("reused", run)
        self.assertEqual(index.update(), 1)
        self.assertEqual(rows("SELECT selection, aborted, dirty, modules_run, tests_run FROM gate_runs"),
                         [("reused", 0, 0, 0, 0)])
        for table in GATE_TABLES[1:]:
            self.assertEqual(table_rows(table), [], table)

    def test_forgetting_one_source_removes_its_run_and_all_children(self) -> None:
        removed = self.write_run("remove", fixture_run(), host="first-host")
        kept = self.write_run("keep", fixture_run(), host="second-host")
        self.assertEqual(index.update(), 2)
        with closing(index.open_for_update(index.index_path())) as connection:
            index.forget(connection, removed)
        for table in GATE_TABLES:
            with self.subTest(table=table):
                self.assertEqual({row["src"] for row in table_rows(table)}, {kept})
        self.assertEqual(rows("SELECT path FROM files"), [(kept,)])

    def test_replacing_a_run_removes_stale_children_and_preserves_other_runs(self) -> None:
        _ = self.write_run("replace", fixture_run())
        _ = self.write_run("keep", fixture_run())
        self.assertEqual(index.update(), 2)
        other_rows = {table: run_rows(table, "keep") for table in GATE_TABLES}
        replacement = fixture_run()
        replacement["suites"] = [{"suite": "tools", "wall_s": 9.0, "status": 0}]
        module = replacement["modules"][2]
        module["tests"] = [{"test": "Case.test_replacement", "s": 2.0, "status": "pass"}]
        replacement["modules"] = [module]
        replacement["reason"] = "replacement record"
        _ = self.write_run("replace", replacement)
        self.assertEqual(index.update(), 1)
        self.assertEqual(rows("SELECT reason, modules_run, tests_run FROM gate_runs WHERE id = ?", "replace"),
                         [("replacement record", 1, 4)])
        self.assertEqual(rows("SELECT suite, wall_s FROM gate_suites WHERE run_id = ?", "replace"), [("tools", 9.0)])
        self.assertEqual(rows("SELECT suite, module, attempt FROM gate_modules WHERE run_id = ?", "replace"),
                         [("tools", "test_shared.py", 1)])
        self.assertEqual(rows("SELECT test, duration_s, status FROM gate_tests WHERE run_id = ?", "replace"),
                         [("Case.test_replacement", 2.0, "pass")])
        for table in GATE_TABLES:
            self.assertCountEqual(run_rows(table, "keep"), other_rows[table])

    def test_nonobject_nested_entries_leave_valid_rows_unchanged(self) -> None:
        source = self.write_run("malformed", fixture_run())
        self.assertEqual(index.update(), 1)
        before = self.snapshot()
        path = self.root / source
        raw = cast(Record, json.loads(path.read_text(encoding="utf-8")))
        invalid: list[object] = [None, "invalid", 42, []]
        suites = cast(list[object], raw["suites"])
        suites.extend(invalid)
        modules = cast(list[object], raw["modules"])
        for module in modules:
            fields = cast(Record, module)
            tests = cast(list[object], fields["tests"])
            tests.extend(invalid)
        modules.extend(invalid)
        store.append_line(path, raw)
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.snapshot(), before)

    def test_history_queries_use_their_indexes_over_five_hundred_runs(self) -> None:
        with mock.patch.object(record, "new_id", side_effect=[f"history-{n}" for n in range(500)]), \
                mock.patch.object(store, "host_name", return_value="fixture-host"), \
                mock.patch.object(time, "time", return_value=EPOCH), \
                mock.patch.object(os, "fsync"):
            for sequence in range(500):
                run = fixture_run()
                run["started_at"] = store.utc_iso(EPOCH + sequence)
                run["ended_at"] = store.utc_iso(EPOCH + sequence + 1)
                run["suites"] = run["suites"][:1]
                run["modules"] = run["modules"][:1]
                gate_record.write_gate_run(run, fixture_executions(run))
        self.assertTrue(store.host_file("fixture-host", EPOCH).is_file(),
                        "The fixture writer must produce the source records")
        self.assertEqual(index.update(), 500)
        since = store.utc_iso(EPOCH)
        queries: list[tuple[str, str, tuple[object, ...]]] = [
            ("gate_runs_started", "SELECT id FROM gate_runs WHERE started_at >= ? ORDER BY started_at DESC LIMIT 20",
             (since,)),
            ("gate_suites_suite", "SELECT run_id FROM gate_suites WHERE suite = ? AND started_at >= ?"
             + " ORDER BY started_at DESC LIMIT 20", ("scripts", since)),
            ("gate_modules_key", "SELECT run_id FROM gate_modules WHERE suite = ? AND module = ? AND started_at >= ?"
             + " ORDER BY started_at DESC LIMIT 20", ("scripts", "test_shared.py", since)),
            ("gate_tests_key", "SELECT run_id FROM gate_tests WHERE suite = ? AND module = ? AND test = ?"
             + " AND started_at >= ? ORDER BY started_at DESC LIMIT 20",
             ("scripts", "test_shared.py", "Case.test_shared", since)),
        ]
        expected = [(f"history-{sequence}",) for sequence in range(499, 479, -1)]
        for name, sql, parameters in queries:
            with self.subTest(index=name):
                self.assertEqual(rows(sql, *parameters), expected)
                plan = " ".join(str(row[3]) for row in rows(f"EXPLAIN QUERY PLAN {sql}", *parameters))
                self.assertIn(name, plan)
                self.assertNotIn("USE TEMP B-TREE", plan)
