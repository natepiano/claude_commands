"""Test timing records with isolated configuration and build log folders."""

from __future__ import annotations

from contextlib import redirect_stderr
from datetime import UTC, datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import cast, override
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.test_times import gate_record
from scripts.buildlog import record
from scripts.test_times.gate_record import GateRun, ModuleTimes, TestTime


def module_times() -> ModuleTimes:
    return {
        "wall_s": 3.0, "cpu_s": 2.0, "tests_s": 1.5, "tests_run": 2,
        "failures": 1, "errors": 0, "skipped": 0,
        "tests": [{"test": "Example.test_pass", "s": 1.0, "status": "pass"},
                  {"test": "Example.test_fail", "s": 0.5, "status": "fail"}],
    }


def gate_run() -> GateRun:
    times = module_times()
    return {
        "started_at": "2026-11-03T12:00:00.000Z", "ended_at": "2026-11-03T12:00:05.000Z",
        "wall_s": 5.0, "hold_wait_s": 1.0, "selection": "affected", "reason": "fixture change",
        "status": 1, "max_jobs": 2, "peak_mib": 30.0, "cap_mib": 100.0,
        "checkout": "/fixture/checkout", "branch": "fixture", "sha": "a" * 40,
        "tree": "b" * 40, "source": "tests-only", "aborted": False, "dirty": True,
        "suites": [{"suite": "scripts", "wall_s": 4.0, "status": 1}],
        "modules": [{"suite": "scripts", "module": "test_example.py", "attempt": 1,
                     "wall_s": times["wall_s"], "cpu_s": times["cpu_s"], "peak_mib": 30.0,
                     "status": 1, "tests_run": times["tests_run"], "failures": times["failures"],
                     "errors": times["errors"], "skipped": times["skipped"],
                     "tests_s": times["tests_s"], "tests": times["tests"],
                     "restart": "hold", "times_missing": ""}],
    }


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

    def write_json(self, value: object) -> None:
        _ = self.timing_path.write_text(json.dumps(value), encoding="utf-8")

    def assert_unavailable(self) -> None:
        result = gate_record.read_module_times(self.timing_path)
        self.assertIsInstance(result, gate_record.NoModuleTimes)
        if not isinstance(result, gate_record.NoModuleTimes):
            self.fail("Invalid timing output must return NoModuleTimes")
        self.assertTrue(result.reason.strip(), "A missing result must explain the failure")

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

    def test_gate_write_appends_complete_records_in_the_host_month(self) -> None:
        run = gate_run()
        before = datetime.now(UTC).timestamp()
        gate_record.write_gate_run(run)
        gate_record.write_gate_run(run)
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
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])

    def test_gate_write_failure_reports_one_line_and_returns(self) -> None:
        _ = self.buildlog.write_text("This file blocks the output directory.\n", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            gate_record.write_gate_run(gate_run())
        self.assertEqual(len(stderr.getvalue().splitlines()), 1, stderr.getvalue())
        self.assertTrue(stderr.getvalue().strip())
