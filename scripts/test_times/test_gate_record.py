"""Test timing records with isolated configuration and build log folders."""

from __future__ import annotations

from contextlib import redirect_stderr
from datetime import UTC, datetime
import gzip
import io
import json
import os
from pathlib import Path
import tempfile
from typing import cast, override
import unittest
from unittest import mock


from scripts.test_times import gate_record
from scripts.buildlog import record, store
from scripts.lint.memory_test_support import write_config_fixture
from scripts.test_times.gate_record import ModuleTimes, TestTime
from scripts.test_times.gate_record_test_support import fixture_executions, gate_run, module_times


class GateRecordTests(unittest.TestCase):
    root: Path = Path()
    timing_path: Path = Path()
    config: Path = Path()
    buildlog: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.timing_path = self.root / "module.json"
        self.config = self.root / "config"
        self.config.mkdir()
        self.buildlog = self.root / "buildlog"
        self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(self.config), "BUILDLOG_DIR": str(self.buildlog),
            "HOME": str(self.root / "home"),
        }))
        _ = write_config_fixture(self.config / "test_times.conf", "SLOW_TEST_S=0.5")

    def write_json(self, value: object) -> None:
        _ = self.timing_path.write_text(json.dumps(value), encoding="utf-8")

    def assert_unavailable(self) -> None:
        result = gate_record.read_module_times(self.timing_path)
        self.assertIsInstance(result, gate_record.NoModuleTimes)
        if not isinstance(result, gate_record.NoModuleTimes):
            self.fail("Invalid timing output must return NoModuleTimes")
        self.assertTrue(result.reason.strip(), "A missing result must explain the failure")

    def test_config_reads_preserve_empty_records_and_missing_records(self) -> None:
        for field in ("config_reads", "folder_scans", "content_reads"):
            values = [["z", "a", "z"], [], None]
            if field == "folder_scans":
                values.append([".", "/", "scripts/", "docs", "scripts/"])
            for reads in values:
                with self.subTest(field=field, reads=reads):
                    value: dict[str, object] = dict(module_times())
                    if reads is not None:
                        value[field] = reads
                    self.write_json(value)
                    times = gate_record.read_module_times(self.timing_path)
                    self.assertNotIsInstance(times, gate_record.NoModuleTimes)
                    assert not isinstance(times, gate_record.NoModuleTimes)
                    execution = gate_record.Execution("suite", "test_fixture.py", 1, 1, 1, 0, "", times, "")
                    row = gate_record.module_runs([execution], 0)[0]
                    if reads is None:
                        self.assertNotIn(field, row)
                    else:
                        self.assertEqual(times.get(field), sorted(set(reads)))
                        self.assertEqual(row.get(field), sorted(set(reads)))

    def test_valid_module_and_empty_module_preserve_the_runner_data(self) -> None:
        populated = module_times()
        empty: ModuleTimes = {"wall_s": 0.1, "cpu_s": 0.0, "tests_s": 0.0,
                              "tests_run": 0, "failures": 0, "errors": 0, "skipped": 0, "tests": []}
        for expected in (populated, empty):
            with self.subTest(tests_run=expected["tests_run"]):
                self.write_json(expected)
                self.assertEqual(gate_record.read_module_times(self.timing_path), expected)

    def test_missing_unreadable_and_malformed_files_return_a_reason(self) -> None:
        self.assert_unavailable()
        self.timing_path.mkdir()
        self.assert_unavailable()
        self.timing_path.rmdir()
        for content in (b'{"wall_s":', b'\xff', b'null', b'[]', b'"text"'):
            with self.subTest(content=content):
                _ = self.timing_path.write_bytes(content)
                self.assert_unavailable()
        missing = gate_record.read_module_times(self.root / "absent.json")
        if not isinstance(missing, gate_record.NoModuleTimes):
            self.fail("An absent file must return NoModuleTimes")

    def test_each_required_module_field_is_checked(self) -> None:
        invalid_values: dict[str, object] = {
            "wall_s": "3", "cpu_s": None, "tests_s": [], "tests_run": 2.5,
            "failures": "1", "errors": False, "skipped": None, "tests": {},
        }
        original: dict[str, object] = dict(module_times())
        for key, invalid in invalid_values.items():
            for remove in (False, True):
                with self.subTest(field=key, missing=remove):
                    value = dict(original)
                    if remove:
                        del value[key]
                    else:
                        value[key] = invalid
                    self.write_json(value)
                    self.assert_unavailable()

    def test_each_test_entry_is_checked(self) -> None:
        invalid_rows: list[object] = [
            None, [], "test", {},
            {"test": 42, "s": 0.1, "status": "pass"},
            {"test": "Example.test", "s": "0.1", "status": "pass"},
            {"test": "Example.test", "s": 0.1, "status": False},
            {"test": "Example.test", "s": 0.1, "status": "unknown"},
            {"s": 0.1, "status": "pass"},
            {"test": "Example.test", "status": "pass"},
            {"test": "Example.test", "s": 0.1},
        ]
        for row in invalid_rows:
            with self.subTest(row=row):
                value: dict[str, object] = dict(module_times())
                value["tests"] = [module_times()["tests"][0], row]
                self.write_json(value)
                self.assert_unavailable()

    def test_kept_tests_include_failures_and_the_exact_threshold_in_time_order(self) -> None:
        tests: list[TestTime] = [
            {"test": "Case.fast", "s": 0.49, "status": "pass"},
            {"test": "Case.fail", "s": 0.0, "status": "fail"},
            {"test": "Case.boundary", "s": 0.5, "status": "pass"},
            {"test": "Case.slow", "s": 2.0, "status": "pass"},
            {"test": "Case.error", "s": 0.1, "status": "error"},
            {"test": "Case.unexpected", "s": 0.2, "status": "unexpected_success"},
            {"test": "Case.expected_fast", "s": 0.3, "status": "expected_failure"},
            {"test": "Case.expected_slow", "s": 0.8, "status": "expected_failure"},
            {"test": "Case.skip", "s": 0.0, "status": "skip"},
        ]
        times = module_times()
        times["tests"] = tests
        original = [dict(row) for row in tests]
        kept = gate_record.kept_tests(times, 0.5)
        self.assertEqual([row["test"] for row in kept], [
            "Case.slow", "Case.expected_slow", "Case.boundary", "Case.unexpected", "Case.error", "Case.fail",
        ])
        self.assertEqual(kept, [tests[index] for index in (3, 7, 2, 5, 4, 1)])
        self.assertEqual(times["tests"], original, "Selection must preserve the input order")
        self.assertEqual(gate_record.kept_tests(times, 0.0), sorted(tests, key=lambda row: row["s"], reverse=True))

    def test_slow_threshold_reads_the_config_at_each_call(self) -> None:
        path = self.config / "test_times.conf"
        _ = path.write_text("# range: 0..\nSLOW_TEST_S=0.75\n", encoding="utf-8")
        self.assertEqual(gate_record.slow_test_s(), 0.75)
        _ = path.write_text("# range: 0..\nSLOW_TEST_S=1.25\n", encoding="utf-8")
        self.assertEqual(gate_record.slow_test_s(), 1.25)

    def test_module_runs_number_each_module_and_preserve_execution_data(self) -> None:
        times = module_times()
        times.update(tests_run=7, errors=2, skipped=3)
        executions = [
            gate_record.Execution("hooks", "test_a.py", 4.0, 2.0, 12.0, -15, "hold",
                                  gate_record.NoModuleTimes("runner stopped"), "", wall_clock=gate_record.WallClockSpan(1.0, 5.0)),
            gate_record.Execution("notify", "test_a.py", 5.0, 3.0, 13.0, 1, "", times, "",
                                  wall_clock=gate_record.WallClockSpan(2.0, 7.0)),
            gate_record.Execution(*gate_record.TYPE_CHECK_JOB, 6.0, 4.0, 14.0, 0, "",
                                  gate_record.TimingNotApplicable(), "", wall_clock=gate_record.WallClockSpan(3.0, 9.0)),
            gate_record.Execution("hooks", "test_a.py", 7.0, 5.0, 15.0, 0, "", times, "",
                                  wall_clock=gate_record.WallClockSpan(4.0, 11.0)),
            gate_record.Execution("notify", "test_b.py", 8.0, 6.0, 16.0, 0, "", times, "",
                                  wall_clock=gate_record.WallClockSpan(5.0, 13.0)),
            gate_record.Execution("notify", "test_a.py", 9.0, 7.0, 17.0, 0, "", times, "",
                                  wall_clock=gate_record.WallClockSpan(6.0, 15.0)),
            gate_record.Execution("notify", "test_legacy.py", 1.0, 0.5, 10.0, 0, "", times, "",
                                  wall_clock=gate_record.WallClockNotRecorded("not recorded")),
        ]
        modules = gate_record.module_runs(executions, 1.25)
        self.assertEqual(gate_record.module_runs([], 1.25), [])
        self.assertEqual([row["attempt"] for row in modules], [1, 1, 1, 2, 1, 2, 1])
        self.assertEqual(len(modules), len(executions))
        for execution, row in zip(executions, modules, strict=True):
            self.assertEqual((row["suite"], row["module"], row["wall_s"], row["cpu_s"],
                              row["peak_mib"], row["status"], row["restart"]),
                             (execution.suite, execution.module, execution.wall_s, execution.cpu_s,
                              execution.peak_mib, execution.status, execution.restart))
        self.assertEqual(
            [(row.get("started_at"), row.get("ended_at")) for row in modules],
            [(store.utc_iso(job.wall_clock.started_at), store.utc_iso(job.wall_clock.ended_at))
             if isinstance(job.wall_clock, gate_record.WallClockSpan) else (None, None) for job in executions],
        )
        self.assertEqual(modules[0].get("started_at"), "1970-01-01T00:00:01.000Z")
        self.assertEqual(modules[0].get("ended_at"), "1970-01-01T00:00:05.000Z")
        self.assertNotIn("started_at", modules[6])
        self.assertNotIn("ended_at", modules[6])
        for index in (1, 3, 4, 5):
            row = modules[index]
            self.assertEqual((row["tests_run"], row["failures"], row["errors"], row["skipped"], row["tests_s"]),
                             (7, 1, 2, 3, 1.5))
            self.assertEqual(row["tests"], [{"test": "Example.test_fail", "s": 0.5, "status": "fail"}])
            self.assertEqual(row["times_missing"], "")
        for index, reason in ((0, "runner stopped"), (2, "")):
            row = modules[index]
            self.assertEqual((row["tests_run"], row["failures"], row["errors"], row["skipped"], row["tests_s"]),
                             (0, 0, 0, 0, 0.0))
            self.assertEqual(row["tests"], [])
            self.assertEqual(row["times_missing"], reason)

    def test_record_warning_replaces_newlines_with_spaces(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            gate_record.report_not_written(OSError("first line\nsecond line\r\nthird line"))
        self.assertEqual(stderr.getvalue(), "gate run not written: first line second line third line\n")

    def test_gate_write_appends_complete_records_in_the_host_month(self) -> None:
        run = gate_run()
        before = datetime.now(UTC).timestamp()
        gate_record.write_gate_run(run, fixture_executions(run))
        gate_record.write_gate_run(run, fixture_executions(run))
        after = datetime.now(UTC).timestamp()
        paths = list(self.buildlog.glob("*/*.jsonl"))
        self.assertTrue(paths, "The writer must append to the fixture build log")
        rows: list[dict[str, object]] = []
        for path in paths:
            self.assertIn(path.stem, {datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m") for epoch in (before, after)})
            contents = path.read_text(encoding="utf-8")
            self.assertTrue(contents.endswith("\n"))
            for line in contents.splitlines():
                value = cast(object, json.loads(line))
                if not isinstance(value, dict):
                    self.fail("Each build log line must contain one object")
                row = cast(dict[str, object], value)
                self.assertEqual(row["host"], path.parent.name)
                rows.append(row)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["kind"], "gate_run")
            self.assertEqual(row["v"], record.RECORD_VERSION)
            self.assertIsInstance(row["id"], str)
            self.assertTrue(str(row["id"]).startswith(f'{row["host"]}-'))
            self.assertEqual({key: row[key] for key in run}, run)
            self.assertEqual(row["slow_test_s"], 0.5)
            self.assertEqual(row["tests_run"], 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])

    def test_gate_write_keeps_the_used_threshold_and_counts_each_module_attempt(self) -> None:
        config = self.config / "test_times.conf"
        _ = write_config_fixture(config, "SLOW_TEST_S=0.75")
        threshold = gate_record.slow_test_s()
        first = module_times()
        first["tests_run"] = 3
        retry = module_times()
        retry["tests_run"] = 5
        other = module_times()
        other["tests_run"] = 7
        run = gate_run()
        run["slow_test_s"] = threshold
        executions = [
            gate_record.Execution("tools", "test_a.py", 3.0, 2.0, 12.0, 143, "hold", first, "held output"),
            gate_record.Execution("tools", "test_a.py", 4.0, 3.0, 13.0, 0, "", retry, ""),
            gate_record.Execution("tools", "test_b.py", 5.0, 4.0, 14.0, 0, "", other, ""),
            gate_record.Execution("tools", "test_missing.py", 1.0, 0.5, 10.0, 1, "",
                                  gate_record.NoModuleTimes("runner stopped"), "runner stopped without timing output"),
            gate_record.Execution(*gate_record.TYPE_CHECK_JOB, 2.0, 1.0, 11.0, 0, "",
                                  gate_record.TimingNotApplicable(), ""),
        ]
        run["modules"] = gate_record.module_runs(executions, threshold)
        _ = write_config_fixture(config, "SLOW_TEST_S=1.25")
        gate_record.write_gate_run(run, executions)
        entries = [entry for path in self.buildlog.glob("*/*.jsonl") for entry in store.locked_rows(path)]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["slow_test_s"], 0.75)
        self.assertEqual(entry["tests_run"], 15)
        self.assertEqual(entry["modules"], run["modules"])
        self.assertEqual([test["test"] for test in run["modules"][0]["tests"]],
                         ["Example.test_pass", "Example.test_fail"])
        for module in run["modules"]:
            if module["module"] == "test_missing.py":
                kept = gzip.decompress((self.buildlog / module["log"]).read_bytes())
                self.assertEqual(kept, b"Cut 0 bytes from the start of this output.\nrunner stopped without timing output")
            else:
                self.assertEqual(module["log"], "")

    def test_failure_log_limit_reads_config_at_each_write_and_keeps_the_tail(self) -> None:
        output = ("earlier output π\n" * 400) + "last traceback line\n"
        encoded = output.encode("utf-8")
        sizes: list[int] = []
        for kib in (1, 2):
            with self.subTest(kib=kib):
                _ = write_config_fixture(self.config / "test_times.conf", f"GATE_FAILURE_LOG_KIB={kib}")
                executions = [gate_record.Execution("tools", "nested/test_failure.py", 1.0, 0.5, 10.0, 1, "",
                                                     gate_record.NoModuleTimes("runner stopped"), output)]
                run = gate_run()
                run["modules"] = gate_record.module_runs(executions, 0.5)
                gate_record.write_gate_run(run, executions)
                kept = gzip.decompress((self.buildlog / run["modules"][0]["log"]).read_bytes())
                header, tail = kept.split(b"\n", 1)
                self.assertEqual(header, f"Cut {len(encoded) - len(tail)} bytes from the start of this output.".encode())
                self.assertEqual(len(tail), kib * 1024)
                self.assertTrue(encoded.endswith(tail))
                self.assertTrue(tail.endswith(b"last traceback line\n"))
                self.assertLessEqual(len(kept), kib * 1024 + len(header) + 1)
                sizes.append(len(tail))
        self.assertGreater(sizes[1], sizes[0])

    def test_log_write_failure_keeps_the_gate_record_and_verdict(self) -> None:
        blocked = self.buildlog / "fixture-host" / store.LOGS_DIR
        blocked.parent.mkdir(parents=True)
        _ = blocked.write_text("This file blocks the log directory.\n")
        executions = [gate_record.Execution("tools", "test_failure.py", 1.0, 0.5, 10.0, 1, "",
                                             gate_record.NoModuleTimes("runner stopped"), "failed module output")]
        run = gate_run()
        run["modules"] = gate_record.module_runs(executions, 0.5)
        stderr = io.StringIO()
        with mock.patch.object(store, "host_name", return_value="fixture-host"), redirect_stderr(stderr):
            gate_record.write_gate_run(run, executions)
        entries = [entry for path in self.buildlog.glob("*/*.jsonl") for entry in store.locked_rows(path)]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], 1)
        self.assertEqual(entries[0]["modules"], run["modules"])
        self.assertEqual(run["modules"][0]["log"], "")
        self.assertEqual(len(stderr.getvalue().splitlines()), 1, stderr.getvalue())
        self.assertIn("gate run not written:", stderr.getvalue())

    def test_gate_write_failure_reports_one_line_and_returns(self) -> None:
        _ = self.buildlog.write_text("This file blocks the output directory.\n", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            run = gate_run()
            gate_record.write_gate_run(run, fixture_executions(run))
        self.assertEqual(len(stderr.getvalue().splitlines()), 1, stderr.getvalue())
        self.assertTrue(stderr.getvalue().strip())
