#!/usr/bin/env python3
"""Memory admission with only temporary history, cgroups, and ledgers."""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import contextlib
from contextlib import closing, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO, cast, override
from unittest import mock
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_admit as gate
import nextest_budget as budget
from memory_test_support import fake_memory_readings
store = gate.store


class BuildStepRecorder(Protocol):
    store: gate.BuildLogStore

    def step(self, arguments: list[str]) -> None: ...


buildlog_record = cast(BuildStepRecorder, cast(object, gate.record))

GIB = gate.GIB
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
HISTORY_AT = NOW - timedelta(seconds=1)
FRESH_HISTORY_AT = NOW - timedelta(minutes=5)
LAST_TEST_BUILD_AT = NOW - timedelta(minutes=1)
INVOKE = Path(__file__).with_name("invoke.sh")
MEMORY_GATE = Path(__file__).with_name("memory_gate.sh")
HANA = gate.RepositoryIdentity("https://example.org/nate/hana", "hana", "hana")


def setUpModule() -> None:
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = (root / "meminfo").write_text("MemTotal: 67108864 kB\nMemAvailable: 67108864 kB\n")
    _ = (root / "pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        + "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    _ = cast(object, unittest.enterModuleContext(patch.dict(os.environ, {
        "BUILDLOG_MEMINFO": str(root / "meminfo"),
        "BUILDLOG_MEMORY_PRESSURE": str(root / "pressure"),
        "BUILDLOG_LIVE_MEMORY_TRIPWIRE": os.environ.get(
            "BUILDLOG_LIVE_MEMORY_TRIPWIRE", str(root / "live-memory-reads.jsonl")
        ),
        "BUILDLOG_BUILD_BUDGET_GIB": "20",
        "BUILDLOG_BUILDS_CGROUP": str(root / "builds.slice"),
        "USER_ZONE": "America/New_York",
    })))


class MemoryTestSupportTests(unittest.TestCase):
    def test_fake_readings_preserve_an_empty_or_set_tripwire_and_default_only_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared_record = str(root / "shared-tripwire.jsonl")
            for setting, expected in ((None, str(root / "live-memory-reads.jsonl")),
                                      ("", ""), (shared_record, shared_record)):
                with self.subTest(tripwire=setting), patch.dict(os.environ):
                    if setting is None:
                        _ = os.environ.pop("BUILDLOG_LIVE_MEMORY_TRIPWIRE", None)
                    else:
                        os.environ["BUILDLOG_LIVE_MEMORY_TRIPWIRE"] = setting
                    environment = fake_memory_readings(root)
                    self.assertEqual(environment["BUILDLOG_LIVE_MEMORY_TRIPWIRE"], expected)


class DecisionTests(unittest.TestCase):
    def test_alone_covers_need_and_earlyoom_reserve(self) -> None:
        result = gate.decide(23 * GIB, 60 * GIB, 20 * GIB, [])
        self.assertEqual((result.state, result.threshold, result.reserve), ("admit", 23 * GIB, 3 * GIB))
        self.assertEqual(gate.decide(23 * GIB - 1, 60 * GIB, 20 * GIB, []).state, "hold")

    def test_measured_use_is_not_charged_again_but_growth_is_reserved(self) -> None:
        running: list[gate.StepMemory] = [gate.MeasuredMemory(3 * GIB, 3 * GIB), gate.PromisedMemory(2 * GIB)]
        result = gate.decide(10 * GIB, 60 * GIB, 5 * GIB, running)
        self.assertEqual((result.promised, result.measured, result.threshold, result.state),
                         (2 * GIB, 3 * GIB, 10 * GIB, "admit"))
        self.assertEqual(gate.decide(10 * GIB - 1, 60 * GIB, 5 * GIB, running).state, "hold")

    def test_ramping_step_counts_promise_or_higher_measured_use(self) -> None:
        for used, headroom, state in ((1, 11, "hold"), (11, 1, "admit"), (13, 0, "hold")):
            with self.subTest(measured_gib=used):
                result = gate.decide(15 * GIB, 60 * GIB, 8 * GIB,
                                     [gate.MeasuredMemory(used * GIB, 12 * GIB)])
                self.assertEqual((result.state, result.promised, result.measured),
                                 (state, headroom * GIB, used * GIB))

    def test_no_total_has_no_reserve(self) -> None:
        result = gate.decide(5 * GIB, None, 5 * GIB, [])
        self.assertEqual((result.reserve, result.threshold, result.state), (0, 5 * GIB, "admit"))

    def test_positive_budget_override_changes_admission_and_invalid_values_use_21(self) -> None:
        for setting, state in (("24", "admit"), ("20", "hold"), ("0", "admit"), ("-1", "admit"),
                               ("bad", "admit"), ("nan", "admit"), ("inf", "admit"), ("", "admit")):
            with self.subTest(setting=setting), patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": setting}):
                decision = gate.decide(40 * GIB, 60 * GIB, 10 * GIB, [gate.MeasuredMemory(11 * GIB)])
                self.assertEqual(decision.state, state)
        with patch.dict(os.environ):
            del os.environ["BUILDLOG_BUILD_BUDGET_GIB"]
            self.assertEqual(gate.decide(40 * GIB, 60 * GIB, 10 * GIB, [gate.MeasuredMemory(11 * GIB)]).state, "admit")
            self.assertEqual(gate.decide(40 * GIB, 60 * GIB, 10 * GIB + 1, [gate.MeasuredMemory(11 * GIB)]).state, "hold")

    def test_psi_11_holds_a_step_that_fits_memory_and_budget(self) -> None:
        decision = gate.decide(40 * GIB, 60 * GIB, GIB, [], pressure=gate.MemoryPressure(11))
        self.assertEqual(decision.state, "hold")

    def test_empty_machine_cannot_skip_the_budget_reserved_for_a_head(self) -> None:
        decision = gate.decide(40 * GIB, 60 * GIB, int(10.8 * GIB), [], head_need=int(19.3 * GIB))
        self.assertEqual(decision.state, "hold")



class HistoryTests(unittest.TestCase):
    root: Path = Path()
    admission: Path = Path()
    host: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.admission = self.root / "admission"
        self.admission.mkdir()
        self.host = "test-host"

    def add_measured(self, count: int, *, host: str = "test-host", repo: str = "hana",
                     at: datetime = HISTORY_AT, test_threads: int | None = None,
                     start_gib: int = 1) -> None:
        with (self.admission / "anon_peaks.jsonl").open("a") as out:
            for index in range(count):
                record: dict[str, object] = {
                    "host": host,
                    "repo": repo,
                    "step": "nextest",
                    "ended_at": store.utc_iso(at.timestamp()),
                    "anon_peak_bytes": (start_gib + index) * GIB,
                }
                if test_threads is not None:
                    record["test_threads"] = test_threads
                _ = out.write(json.dumps(record) + "\n")

    def add_index(self, *, at: datetime = HISTORY_AT) -> None:
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, peak_mem_bytes INTEGER)")
            for index in range(5):
                _ = db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", store.utc_iso(at.timestamp()), (index + 1) * GIB))

    def peak(self) -> gate.ExpectedPeak:
        return gate.expected_peak(HANA, "nextest", self.host, NOW, self.root)

    def test_measured_overrides_index_at_five(self) -> None:
        self.add_index()
        self.add_measured(4)
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))
        self.add_measured(1)
        self.assertEqual(self.peak(), gate.ExpectedPeak(4 * GIB, "measured", 5))

    def test_stale_and_other_host_are_excluded(self) -> None:
        self.add_measured(5, at=NOW - timedelta(days=15))
        self.add_measured(5, host="other-host")
        self.assertEqual(self.peak(), gate.ExpectedPeak(gate.FALLBACK, "fallback", 0))

    def test_a_repo_with_few_runs_uses_the_step_p90_over_every_repo(self) -> None:
        self.add_measured(3, repo="other")
        self.add_measured(4, start_gib=10)
        self.assertEqual(self.peak(), gate.ExpectedPeak(13 * GIB, "pooled", 7))

    def test_own_buildlog_history_comes_before_the_pooled_p90(self) -> None:
        self.add_index()
        self.add_measured(5, repo="other")
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_missing_index_or_history_is_fallback(self) -> None:
        self.assertEqual(self.peak().source, "fallback")
        self.add_index()
        self.assertEqual(self.peak().source, "buildlog")

    def test_measured_history_pools_every_width_unless_one_is_named(self) -> None:
        self.add_measured(5, test_threads=16)
        self.add_measured(5, test_threads=4, start_gib=6)
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, test_threads=16),
            gate.ExpectedPeak(5 * GIB, "measured", 5),
        )
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root),
            gate.ExpectedPeak(9 * GIB, "measured", 10),
        )

    def test_future_measured_records_do_not_override_prior_index_history(self) -> None:
        """F002: a historical report excludes measured records beyond its end."""
        self.add_index()
        self.add_measured(5, at=NOW + timedelta(seconds=1), start_gib=10)
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_future_index_rows_do_not_supply_buildlog_history(self) -> None:
        """F002: a historical report excludes index rows beyond its end."""
        self.add_index(at=NOW + timedelta(seconds=1))
        self.assertEqual(self.peak(), gate.ExpectedPeak(gate.FALLBACK, "fallback", 0))

    def test_buildlog_tier_keeps_filtered_runs_separate(self) -> None:
        self.add_index()
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("ALTER TABLE steps ADD COLUMN argv TEXT")
            _ = db.execute("UPDATE steps SET argv = ?", (json.dumps(["cargo", "nextest", "run"]),))
            for _ in range(5):
                _ = db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), 2 * GIB,
                                json.dumps(["cargo", "nextest", "run", "-E", "test(one)"])))
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope="filtered"),
                         gate.ExpectedPeak(int(2 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_scratch_checkout_cannot_take_legacy_buildlog_samples_by_repository_name(self) -> None:
        self.add_index()
        scratch = gate.RepositoryIdentity(HANA.key, HANA.name)
        self.assertEqual(gate.expected_peak(scratch, "nextest", self.host, NOW, self.root).source,
                         "fallback")


class FilteredPromiseTests(unittest.TestCase):
    root: Path = Path()
    host: str = "test-host"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "admission").mkdir()

    def samples(self, peaks: list[int], *, package: str = "hana", filters: tuple[str, ...] = (),
                scope: gate.TestScope = "filtered", step: str = "nextest",
                repository: gate.RepositoryIdentity = HANA, host: str = "test-host",
                at: datetime = HISTORY_AT) -> None:
        with (self.root / "admission/anon_peaks.jsonl").open("a") as output:
            for peak in peaks:
                _ = output.write(json.dumps({
                    "host": host, "repo": repository.name, "repo_identity": repository.key,
                    "step": step, "test_scope": scope, "package": package,
                    "filters": list(filters), "anon_peak_bytes": peak,
                    "ended_at": store.utc_iso(at.timestamp()),
                }) + "\n")

    def promise(self, filters: tuple[str, ...] = ("native_rate",)) -> gate.ExpectedPeak:
        return gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                                  scope="filtered", package="hana", filters=filters)

    def test_exact_filter_set_uses_its_p90_regardless_of_name_order(self) -> None:
        self.samples([number * GIB for number in range(1, 11)], filters=("render", "native_rate"))
        self.samples([14 * GIB] * 10, filters=("other",))
        self.samples([19 * GIB] * 10, scope="full")
        for filters in (("native_rate", "render"), ("render", "native_rate")):
            with self.subTest(filters=filters):
                self.assertEqual(self.promise(filters), gate.ExpectedPeak(9 * GIB, "measured", 10))

    def test_one_exact_native_rate_sample_precedes_other_filtered_history(self) -> None:
        self.samples([2 * GIB], filters=("native_rate",))
        self.samples([9 * GIB] * 10, filters=("render",))
        self.assertEqual(self.promise(), gate.ExpectedPeak(2 * GIB, "measured", 1))

    def test_unseen_filter_set_uses_all_filtered_runs_of_the_same_package(self) -> None:
        self.samples([number * GIB for number in range(1, 6)], filters=("render",))
        self.samples([number * GIB for number in range(6, 11)], filters=("audio",))
        self.samples([17 * GIB] * 10, filters=("native_rate",), package="hana-ui")
        other = gate.RepositoryIdentity("https://example.org/nate/other", "other", "other")
        self.samples([18 * GIB] * 10, filters=("native_rate",), repository=other)
        self.samples([19 * GIB] * 10, scope="full")
        self.assertEqual(self.promise(), gate.ExpectedPeak(9 * GIB, "measured", 10))

    def test_no_filtered_history_uses_floor_when_compile_peak_is_lower(self) -> None:
        self.samples([gate.FILTERED_FLOOR // 2] * 5, step="check", scope="full")
        self.samples([19 * GIB] * 5, scope="full")
        self.assertEqual(self.promise().bytes, gate.FILTERED_FLOOR)

    def test_no_filtered_history_keeps_compile_peak_above_the_floor(self) -> None:
        compile_peak = 2 * gate.FILTERED_FLOOR
        self.samples([compile_peak] * 5, step="check", scope="full")
        self.samples([4 * compile_peak] * 5, step="check", scope="full", package="hana-ui")
        self.samples([5 * compile_peak] * 5, scope="full")
        self.assertEqual(self.promise().bytes, compile_peak)

    def test_no_compile_or_filtered_history_uses_floor_instead_of_full_suite(self) -> None:
        self.samples([19 * GIB] * 5, scope="full")
        self.samples([18 * GIB] * 5, package="hana-ui", filters=("native_rate",))
        self.assertEqual(self.promise().bytes, gate.FILTERED_FLOOR)

    def test_compile_fallback_can_use_same_package_buildlog_history(self) -> None:
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT)")
            for package, peak in (("hana", 8 * GIB), ("hana-ui", 20 * GIB)):
                _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", [
                    (self.host, "hana", "check", store.utc_iso(HISTORY_AT.timestamp()), peak,
                     json.dumps(["cargo", "check", "-p", package]))
                ] * 5)
        self.assertEqual(self.promise().bytes, max(gate.FILTERED_FLOOR, int(8 * GIB * gate.ANON_SHARE)))

    def test_exact_buildlog_filters_precede_broader_filtered_anon_history(self) -> None:
        self.samples([19 * GIB] * 10, filters=("other",))
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT)")
            for package, names, peaks in (
                ("hana", "test(render) | test(native_rate)", list(range(1, 11))),
                ("hana-ui", "test(render) | test(native_rate)", [20] * 10),
                ("hana", "test(other)", [21] * 10),
            ):
                argv = ["cargo", "nextest", "run", "-E", f"package({package}) & ({names})"]
                _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", [
                    (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), peak * GIB,
                     json.dumps(argv)) for peak in peaks
                ])
        self.assertEqual(self.promise(("native_rate", "render")),
                         gate.ExpectedPeak(int(9 * GIB * gate.ANON_SHARE), "buildlog", 10))

    def test_verify_nextest_expression_supplies_package_and_canonical_filter_set(self) -> None:
        cases = (
            ("package(hana) & test(native_rate)", ("native_rate",)),
            ("package(hana) & (test(render) | test(native_rate))", ("native_rate", "render")),
            ("package(hana) & (test(render) | test(native_rate) | test(render))", ("native_rate", "render")),
            ("package(hana)", ()),
        )
        for expression, filters in cases:
            for arguments in (["-E", expression], ["--filterset=" + expression]):
                with self.subTest(arguments=arguments):
                    argv = ["cargo", "nextest", "run", "--workspace", "--test-threads", "16", *arguments]
                    self.assertEqual(gate.test_package(argv), "hana")
                    self.assertEqual(gate.test_filters(argv), filters)

    def test_filtered_history_respects_host_and_time_window(self) -> None:
        self.samples([19 * GIB] * 5, filters=("native_rate",), host="other-host")
        self.samples([19 * GIB] * 5, filters=("native_rate",), at=NOW - timedelta(days=15))
        self.samples([19 * GIB] * 5, filters=("native_rate",), at=NOW + timedelta(seconds=1))
        self.assertEqual(self.promise().bytes, gate.FILTERED_FLOOR)

    def test_unfiltered_p90_is_unchanged_by_filtered_and_compile_history(self) -> None:
        self.samples([number * GIB for number in range(1, 11)], scope="full")
        self.samples([19 * GIB] * 10, filters=("native_rate",))
        self.samples([20 * GIB] * 10, step="check", scope="full")
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root),
                         gate.ExpectedPeak(9 * GIB, "measured", 10))

    def test_non_verify_expressions_have_no_exact_package_or_filter_key(self) -> None:
        expressions = (
            "package(hana) & not test(native_rate)",
            "package(hana) & !test(native_rate)",
            "package(hana) & (test(native_rate) & test(render))",
            "package(hana) | test(native_rate)",
            "package(hana) & (test(native_rate) | not test(render))",
            "package(hana) & test(native_rate) & kind(lib)",
            "not package(hana) & test(native_rate)",
            "package(hana) & test(/native.*/)",
            "test(native_rate)",
        )
        for expression in expressions:
            for option in ("-E", "--filterset="):
                arguments = [option, expression] if option == "-E" else [option + expression]
                for package_arguments in ([], ["-p", "hana"]):
                    with self.subTest(expression=expression, option=option, package_arguments=package_arguments):
                        argv = ["cargo", "nextest", "run", *package_arguments, *arguments]
                        self.assertEqual(gate.test_filters(argv), ())
                        self.assertEqual(gate.test_package(argv), "")

    def test_multiple_filter_expressions_cannot_claim_a_verify_exact_key(self) -> None:
        for second in ("test(render)", "not test(native_rate)", "package(hana) & test(render)"):
            with self.subTest(second=second):
                argv = ["cargo", "nextest", "run", "-E", "package(hana) & test(native_rate)", "-E", second]
                self.assertEqual(gate.test_filters(argv), ())
                self.assertEqual(gate.test_package(argv), "")

    def test_non_verify_expression_keeps_the_broad_filtered_memory_estimate(self) -> None:
        self.samples([15 * GIB] * 5, package="")
        self.samples([2 * GIB] * 5, filters=("native_rate",))
        argv = ["cargo", "nextest", "run", "-E", "package(hana) & not test(native_rate)"]
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope=gate.test_scope(argv),
                               package=gate.test_package(argv), filters=gate.test_filters(argv)),
            gate.ExpectedPeak(15 * GIB, "measured", 10),
        )

    def test_negated_index_expression_cannot_supply_a_positive_exact_promise(self) -> None:
        self.samples([9 * GIB] * 5, filters=("other",))
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT)")
            for expression in ("package(hana) & not test(native_rate)", "package(hana) & !test(native_rate)"):
                _ = db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), GIB,
                                json.dumps(["cargo", "nextest", "run", "-E", expression])))
        self.assertEqual(self.promise(), gate.ExpectedPeak(9 * GIB, "measured", 5))

    def test_unscoped_filtered_run_without_history_retains_the_legacy_fallback(self) -> None:
        argv = ["cargo", "nextest", "run", "--workspace", "-E", "test(native_rate)"]
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope=gate.test_scope(argv),
                               package=gate.test_package(argv), filters=gate.test_filters(argv)),
            gate.ExpectedPeak(gate.FALLBACK, "fallback", 0),
        )

    def test_unscoped_filtered_run_with_sparse_history_retains_the_legacy_sample_minimum(self) -> None:
        self.samples([2 * GIB], package="", filters=("native_rate",))
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                               scope="filtered", filters=("native_rate",)),
            gate.ExpectedPeak(gate.FALLBACK, "fallback", 0),
        )

    def test_unscoped_filtered_run_retains_the_cross_repository_pool(self) -> None:
        self.samples([2 * GIB] * 3, package="")
        other = gate.RepositoryIdentity("https://example.org/nate/other", "other", "other")
        self.samples([9 * GIB] * 5, package="", repository=other)
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope="filtered"),
            gate.ExpectedPeak(9 * GIB, "pooled", 8),
        )

    def test_no_filtered_history_uses_same_package_build_measurements(self) -> None:
        self.samples([8 * GIB] * 5, step="build", scope="full")
        self.samples([20 * GIB] * 5, step="build", scope="full", package="hana-ui")
        self.assertEqual(self.promise(), gate.ExpectedPeak(8 * GIB, "measured", 5))

    def test_build_measurements_precede_compile_index_history(self) -> None:
        self.samples([8 * GIB] * 5, step="build", scope="full")
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT)")
            _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", [
                (self.host, "hana", "check", store.utc_iso(HISTORY_AT.timestamp()), 20 * GIB,
                 json.dumps(["cargo", "check", "-p", "hana"]))
            ] * 5)
        self.assertEqual(self.promise(), gate.ExpectedPeak(8 * GIB, "measured", 5))

    def test_no_filtered_history_uses_same_package_build_index_measurements(self) -> None:
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT)")
            for package, peak in (("hana", 8 * GIB), ("hana-ui", 20 * GIB)):
                _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", [
                    (self.host, "hana", "build", store.utc_iso(HISTORY_AT.timestamp()), peak,
                     json.dumps(["cargo", "build", "-p", package]))
                ] * 5)
        self.assertEqual(self.promise(), gate.ExpectedPeak(int(8 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_indexed_member_filter_promises_never_exceed_the_full_suite(self) -> None:
        self.samples([12 * GIB] * 5, scope="full")
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT)")
            _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", [
                (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), 30 * GIB,
                 json.dumps(["cargo", "nextest", "run", "-E",
                             "package(hana_diegetic) & test(native_rate)"]))
            ] * 66)
        for filters in (("native_rate",), ("another_test",)):
            with self.subTest(filters=filters):
                self.assertEqual(
                    gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                                       scope="filtered", package="hana_diegetic", filters=filters),
                    gate.ExpectedPeak(12 * GIB, "buildlog", 66),
                )
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root),
                         gate.ExpectedPeak(12 * GIB, "measured", 5))

    def test_exact_filtered_measurements_are_capped_without_raising_smaller_promises(self) -> None:
        self.samples([7 * GIB] * 5, scope="full")
        self.samples([20 * GIB], filters=("native_rate",))
        self.samples([2 * GIB], filters=("small_test",))
        self.assertEqual(self.promise(), gate.ExpectedPeak(7 * GIB, "measured", 1))
        self.assertEqual(self.promise(("small_test",)), gate.ExpectedPeak(2 * GIB, "measured", 1))

    def test_compile_fallback_is_capped_by_the_full_suite_promise(self) -> None:
        self.samples([4 * GIB] * 5, scope="full")
        self.samples([8 * GIB] * 5, step="build", scope="full")
        self.assertEqual(self.promise(), gate.ExpectedPeak(4 * GIB, "measured", 5))

    def test_filtered_cap_uses_the_requested_test_width(self) -> None:
        with (self.root / "admission/anon_peaks.jsonl").open("a") as output:
            for scope, width, peak, count in (("full", 16, 6 * GIB, 5),
                                               ("full", 4, 30 * GIB, 5),
                                               ("filtered", 16, 20 * GIB, 1)):
                for _ in range(count):
                    _ = output.write(json.dumps({
                        "host": self.host, "repo": HANA.name, "repo_identity": HANA.key,
                        "step": "nextest", "test_scope": scope, "test_threads": width,
                        "package": "hana", "filters": ["native_rate"], "anon_peak_bytes": peak,
                        "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
                    }) + "\n")
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, test_threads=16,
                               scope="filtered", package="hana", filters=("native_rate",)),
            gate.ExpectedPeak(6 * GIB, "measured", 1),
        )


class FreshTestPromiseTests(unittest.TestCase):
    root: Path = Path()
    worktree: Path = Path()
    state: Path = Path()
    sample_id: int = 0
    host: str = "test-host"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.worktree = self.root / "checkout"
        (self.worktree / "target").mkdir(parents=True)
        self.state = self.root / "sweep-state"
        self.state.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"LINT_SWEEP_STATE_DIR": str(self.state)})))
        (self.root / "admission").mkdir()
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "ended_at TEXT, peak_mem_bytes INTEGER, argv TEXT, call_id TEXT, "
                           + "worktree TEXT, tree_key TEXT, status INTEGER, tests_run INTEGER, "
                           + "crates_compiled INTEGER, sweep_freed_bytes INTEGER)")
        self.sample_id = 0
        self.samples([12 * GIB] * 10, scope="full", crates=1)
        self.samples([10 * GIB] * 10, crates=1)

    def samples(self, peaks: list[int], *, package: str = "hana", scope: gate.TestScope = "filtered",
                filters: tuple[str, ...] = ("native_rate",), crates: int | None = 0,
                host: str = "test-host", at: datetime = FRESH_HISTORY_AT,
                status: int = 0, tests_run: int = 1, measured: bool = True) -> None:
        expression = f"package({package})"
        if scope == "filtered":
            selection = " | ".join(f"test({name})" for name in filters)
            expression += " & " + (selection if len(filters) == 1 else f"({selection})")
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db, \
                (self.root / "admission/anon_peaks.jsonl").open("a") as output:
            for peak in peaks:
                self.sample_id += 1
                call = f"call-{self.sample_id}"
                timestamp = store.utc_iso(at.timestamp())
                _ = db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (host, "hana", "nextest", timestamp, timestamp, 20 * GIB,
                                json.dumps(["cargo", "nextest", "run", "-E", expression]), call,
                                "/another/history-worktree", "historical-key", status, tests_run, crates, 0))
                if measured:
                    _ = output.write(json.dumps({
                        "host": host, "repo": "hana", "repo_identity": HANA.key, "step": "nextest",
                        "package": package, "test_scope": scope, "filters": list(filters),
                        "ended_at": timestamp, "anon_peak_bytes": peak, "call_id": call,
                    }) + "\n")

    def previous_build(self, *, key: str = "current-key", worktree: str = "", package: str = "hana",
                       scope: gate.TestScope = "filtered", status: int = 0, tests_run: int = 1,
                       at: datetime = LAST_TEST_BUILD_AT, finished: bool = True,
                       indexed: bool = True, recorded_build_key: bool = True) -> None:
        expression = f"package({package})" + (" & test(other_filter)" if scope == "filtered" else "")
        timestamp = store.utc_iso(at.timestamp())
        arguments = ["cargo", "nextest", "run", "-E", expression]
        if indexed:
            with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
                _ = db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", timestamp, timestamp if finished else None, None,
                                json.dumps(arguments), "last-build", worktree or str(self.worktree),
                                key, status, tests_run, 3, 0))
        record: dict[str, object] = {
            "kind": "step", "host": self.host, "repo": "hana", "step": "nextest",
            "started_at": timestamp, "ended_at": timestamp if finished else None,
            "argv": arguments, "worktree": worktree or str(self.worktree), "tree_key": key,
            "status": status, "tests_run": tests_run, "crates_compiled": 3,
        }
        if recorded_build_key:
            record["test_build_key"] = key
        self.append_step(record, at)

    def append_step(self, record: dict[str, object], at: datetime) -> None:
        store.append_line(self.root / self.host / f"{at:%Y-%m}.jsonl", record)

    def index_current_prefix(self) -> Path:
        path = self.root / self.host / f"{NOW:%Y-%m}.jsonl"
        offset = path.stat().st_size
        with path.open("rb") as handle:
            marks = gate.file_marks.marks(handle, offset)
        source = path.relative_to(self.root).as_posix()
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("ALTER TABLE steps ADD COLUMN test_build_key TEXT")
            _ = db.execute("ALTER TABLE steps ADD COLUMN src TEXT")
            _ = db.execute("UPDATE steps SET test_build_key = tree_key, src = ? WHERE worktree = ?",
                           (source, str(self.worktree)))
            _ = db.execute("CREATE TABLE files(path TEXT PRIMARY KEY, offset INTEGER, marks TEXT)")
            _ = db.execute("INSERT INTO files VALUES (?, ?, ?)", (source, offset, marks))
        return path

    def fresh(self, *, key: str = "current-key", scope: gate.TestScope = "filtered",
              target_dir: str = "target") -> bool:
        return gate.is_fresh_test(self.root, self.host, str(self.worktree), "hana", scope, key, NOW,
                                  target_dir=target_dir)

    def promise(self, *, key: str = "current-key", scope: gate.TestScope = "filtered",
                filters: tuple[str, ...] = ("native_rate",)) -> gate.ExpectedPeak:
        return gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope=scope,
                                  package="hana", filters=filters, fresh=self.fresh(key=key, scope=scope))

    def test_same_tree_native_rate_run_uses_zero_compile_anon_p90_joined_by_call(self) -> None:
        self.samples([number * GIB // 4 for number in range(1, 11)])
        self.samples([11 * GIB] * 10, crates=2)
        self.previous_build()
        self.assertTrue(self.fresh())
        self.assertEqual(self.promise(), gate.ExpectedPeak(9 * GIB // 4, "measured", 10))

    def test_fresh_exact_filter_set_is_sorted_and_precedes_broader_package_history(self) -> None:
        self.samples([2 * GIB], filters=("render", "native_rate"))
        self.samples([8 * GIB] * 10, filters=("other",))
        self.previous_build()
        self.assertEqual(self.promise(filters=("native_rate", "render")),
                         gate.ExpectedPeak(2 * GIB, "measured", 1))

    def test_fresh_unseen_filters_use_only_fresh_same_package_and_scope(self) -> None:
        self.samples([2 * GIB], filters=("render",))
        self.samples([8 * GIB] * 10, package="hana-ui")
        self.samples([9 * GIB] * 10, scope="full")
        self.previous_build()
        self.assertEqual(self.promise(), gate.ExpectedPeak(2 * GIB, "measured", 1))

    def test_fresh_promise_has_the_fixed_floor(self) -> None:
        self.samples([GIB // 4])
        self.previous_build()
        self.assertEqual(self.promise().bytes, gate.FILTERED_FLOOR)

    def test_fresh_full_suite_uses_its_own_zero_compile_history(self) -> None:
        self.samples([3 * GIB], scope="full")
        self.samples([GIB], scope="filtered")
        self.previous_build(scope="full")
        self.assertEqual(self.promise(scope="full"), gate.ExpectedPeak(3 * GIB, "measured", 1))

    def test_fresh_without_zero_compile_history_keeps_compile_promise(self) -> None:
        self.previous_build()
        self.assertTrue(self.fresh())
        self.assertEqual(self.promise(), gate.ExpectedPeak(10 * GIB, "measured", 10))

    def test_fresh_index_peak_is_scaled_when_no_joined_anon_measurement_exists(self) -> None:
        self.samples([GIB], measured=False)
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("UPDATE steps SET peak_mem_bytes = ? WHERE crates_compiled = 0", (4 * GIB,))
        self.previous_build()
        self.assertEqual(self.promise(), gate.ExpectedPeak(int(4 * GIB * gate.ANON_SHARE), "buildlog", 1))

    def test_missing_prior_step_cannot_use_fresh_history(self) -> None:
        self.samples([2 * GIB])
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_other_worktree_cannot_prove_this_target_is_built(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(worktree=str(self.root / "other-checkout"))
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_other_package_or_scope_cannot_prove_this_test_build_is_fresh(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(package="hana-ui")
        self.previous_build(scope="full")
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_changed_or_missing_current_tree_key_keeps_compile_promise(self) -> None:
        self.samples([2 * GIB])
        self.previous_build()
        for key in ("different-key", ""):
            with self.subTest(key=key):
                self.assertFalse(self.fresh(key=key))
                self.assertEqual(self.promise(key=key).bytes, 10 * GIB)

    def test_latest_finished_build_overrides_older_matching_tree_key(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(at=NOW - timedelta(minutes=2))
        self.previous_build(key="newer-different-key")
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_compile_failure_without_tests_cannot_prove_freshness(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(status=101, tests_run=0)
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_failed_tests_still_prove_the_test_targets_were_built(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(status=100, tests_run=1)
        self.assertTrue(self.fresh())
        self.assertEqual(self.promise().bytes, 2 * GIB)

    def test_successful_empty_selection_still_proves_the_test_targets_were_built(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(status=0, tests_run=0)
        self.assertTrue(self.fresh())
        self.assertEqual(self.promise().bytes, 2 * GIB)

    def test_unfinished_step_does_not_replace_the_last_finished_build(self) -> None:
        self.previous_build()
        self.previous_build(key="unfinished-other-key", at=NOW - timedelta(seconds=30), finished=False)
        self.assertTrue(self.fresh())

    def test_source_only_legacy_step_cannot_prove_the_build_inputs_are_fresh(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(recorded_build_key=False)
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_recorder_preserves_build_input_key_only_when_source_stays_unchanged(self) -> None:
        path = self.root / self.host / f"{NOW:%Y-%m}.jsonl"
        for seconds, ending_source, expected_fresh in ((30, "same-source", True), (20, "edited-source", False)):
            with self.subTest(source=ending_source), patch.dict(os.environ, {
                    "BUILDLOG_TREE_START": "same-source", "BUILDLOG_TEST_BUILD_KEY": "current-key"}), \
                    patch.object(buildlog_record, "git_directory", return_value=str(self.worktree)), \
                    patch.object(buildlog_record, "git_facts", return_value={"worktree": str(self.worktree)}), \
                    patch("treekey.tree_key", return_value=ending_source), \
                    patch.object(buildlog_record, "rustc_version", return_value="rustc fixture"), \
                    patch.object(buildlog_record.store, "host_name", return_value=self.host), \
                    patch.object(buildlog_record.store, "host_file", return_value=path):
                ended = NOW.timestamp() - seconds
                buildlog_record.step(["0", str(ended - 5), str(ended), "1", "", "",
                                      "cargo", "nextest", "run", "-E", "package(hana) & test(native_rate)"])
                row = cast(dict[str, object], json.loads(path.read_text().splitlines()[-1]))
                self.assertEqual(row.get("test_build_key"), "current-key" if expected_fresh else None)
                self.assertEqual(self.fresh(), expected_fresh)

    def test_newer_unindexed_step_overrides_the_indexed_matching_build(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(at=NOW - timedelta(minutes=2))
        self.assertTrue(self.fresh())
        self.previous_build(key="different-build-inputs", indexed=False)
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_validated_index_prefix_still_reads_newer_finished_build_in_jsonl_tail(self) -> None:
        self.samples([2 * GIB])
        self.previous_build(at=NOW - timedelta(minutes=2))
        _ = self.index_current_prefix()
        self.assertTrue(self.fresh())
        self.previous_build(key="different-build-inputs", indexed=False)
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_replaced_log_invalidates_previously_matching_index_prefix(self) -> None:
        self.previous_build()
        path = self.index_current_prefix()
        self.assertTrue(self.fresh())
        original = path.read_text()
        replaced = original.replace('"test_build_key":"current-key"',
                                    '"test_build_key":"altered-key"')
        self.assertNotEqual(replaced, original)
        self.assertEqual(len(replaced), len(original))
        _ = path.write_text(replaced)
        self.assertFalse(self.fresh())

    def test_incomplete_tail_after_validated_index_prefix_refuses_freshness(self) -> None:
        self.previous_build()
        path = self.index_current_prefix()
        self.assertTrue(self.fresh())
        with path.open("a") as log:
            _ = log.write('{"kind": "step"')
        self.assertFalse(self.fresh())

    def test_unreadable_current_history_cannot_trust_the_matching_index(self) -> None:
        self.previous_build()
        self.assertTrue(self.fresh())
        with (self.root / self.host / f"{NOW:%Y-%m}.jsonl").open("a") as log:
            _ = log.write("{incomplete record\n")
        self.assertFalse(self.fresh())

    def test_unknown_resolved_target_directory_cannot_prove_freshness(self) -> None:
        self.previous_build()
        self.assertTrue(self.fresh())
        self.assertFalse(self.fresh(target_dir=""))

    def test_sweep_of_cargo_configured_target_invalidates_the_build(self) -> None:
        configured = self.worktree / "configured-target"
        configured.mkdir()
        cargo = self.worktree / ".cargo"
        cargo.mkdir()
        _ = (cargo / "config.toml").write_text('[build]\ntarget-dir = "configured-target"\n')
        self.previous_build()
        self.assertTrue(self.fresh(target_dir=str(configured)))
        _ = (self.state / "target-deletes.json").write_text(json.dumps({
            str(configured): (NOW - timedelta(seconds=30)).timestamp(),
        }))
        self.assertFalse(self.fresh(target_dir=str(configured)))

    def test_admission_uses_the_resolved_target_and_build_key_passed_by_verify(self) -> None:
        configured = self.worktree / "cargo-config-target"
        configured.mkdir()
        self.samples([2 * GIB])
        self.previous_build()
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        arguments = ["cargo", "nextest", "run", "-E", "package(hana) & test(native_rate)"]
        with patch.dict(os.environ, {
                "BUILDLOG_TEST_BUILD_KEY": "current-key", "BUILDLOG_TREE": "source-only-key",
                "BUILDLOG_TEST_TARGET_DIR": str(configured),
                "CARGO_TARGET_DIR": str(self.worktree / "target")}), \
                patch.object(gate, "git_identity", return_value=(HANA, "checkout")), \
                patch.object(gate.store, "host_name", return_value=self.host), \
                patch.object(gate.record, "git_directory", return_value=str(self.worktree)), \
                patch.object(gate.record, "git_facts", return_value={"worktree": str(self.worktree)}), \
                patch.object(gate, "process_start_time", return_value=None):
            fresh = gate.check(meminfo, 400, "", arguments, root=self.root, now=NOW)
            self.assertEqual(fresh.peak.bytes, 2 * GIB)
            _ = (self.state / "target-deletes.json").write_text(json.dumps({
                str(configured): (NOW - timedelta(seconds=30)).timestamp(),
            }))
            swept = gate.check(meminfo, 400, "", arguments, root=self.root, now=NOW)
            self.assertEqual(swept.peak.bytes, 10 * GIB)

    def test_custom_target_deletion_invalidates_its_build(self) -> None:
        custom = self.worktree / "custom-target"
        custom.mkdir()
        self.previous_build()
        self.assertTrue(self.fresh(target_dir="custom-target"))
        _ = (self.state / "target-deletes.json").write_text(json.dumps({
            str(custom): (NOW - timedelta(seconds=30)).timestamp(),
        }))
        self.assertFalse(self.fresh(target_dir="custom-target"))

    def test_corrupt_sweep_state_keeps_compile_promise(self) -> None:
        self.samples([2 * GIB])
        self.previous_build()
        _ = (self.state / "target-deletes.json").write_text("not json")
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_unknown_compile_count_and_compile_failures_are_not_fresh_history(self) -> None:
        self.samples([GIB], crates=None)
        self.samples([GIB], status=101, tests_run=0)
        self.previous_build()
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_target_deletion_since_build_forces_compile_promise(self) -> None:
        self.samples([2 * GIB])
        self.previous_build()
        _ = (self.state / "target-deletes.json").write_text(json.dumps({
            str(self.worktree / "target"): (NOW - timedelta(seconds=30)).timestamp(),
        }))
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_deletion_before_build_or_in_other_target_does_not_invalidate_it(self) -> None:
        self.samples([2 * GIB])
        self.previous_build()
        _ = (self.state / "target-deletes.json").write_text(json.dumps({
            str(self.worktree / "target"): (NOW - timedelta(minutes=2)).timestamp(),
            str(self.root / "other-target"): NOW.timestamp(),
        }))
        self.assertTrue(self.fresh())
        self.assertEqual(self.promise().bytes, 2 * GIB)

    def test_logged_disk_sweep_since_build_forces_compile_promise(self) -> None:
        self.samples([2 * GIB])
        self.previous_build()
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("INSERT INTO steps(host, worktree, step, started_at, sweep_freed_bytes) "
                           + "VALUES (?, ?, 'sweep', ?, ?)",
                           (self.host, str(self.worktree), store.utc_iso((NOW - timedelta(seconds=30)).timestamp()), GIB))
        self.append_step({"kind": "step", "host": self.host, "worktree": str(self.worktree),
                          "step": "sweep", "started_at": store.utc_iso((NOW - timedelta(seconds=30)).timestamp()),
                          "sweep_freed_bytes": GIB}, NOW)
        self.assertFalse(self.fresh())
        self.assertEqual(self.promise().bytes, 10 * GIB)

    def test_missing_target_and_expired_sweep_evidence_force_compile_promise(self) -> None:
        self.previous_build(at=NOW - timedelta(days=2))
        self.assertFalse(self.fresh())
        self.previous_build()
        (self.worktree / "target").rmdir()
        self.assertFalse(self.fresh())


class RepositoryHistoryTests(unittest.TestCase):
    root: Path = Path()
    admission: Path = Path()
    host: str = "test-host"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.admission = self.root / "admission"
        self.admission.mkdir()

    def checkout(self, folder: str, remote: str = "", content: str = "first") -> Path:
        directory = self.root / folder
        directory.mkdir(parents=True)
        for arguments in (("init", "-q"), ("config", "user.email", "test@example.org"),
                          ("config", "user.name", "Test"), ("config", "commit.gpgSign", "false"),
                          ("config", "core.hooksPath", "/dev/null")):
            _ = subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True)
        _ = (directory / "source").write_text(content)
        _ = subprocess.run(["git", "-C", str(directory), "add", "source"], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(directory), "commit", "-qm", "first"], check=True, capture_output=True)
        if remote:
            _ = subprocess.run(["git", "-C", str(directory), "remote", "add", "origin", remote], check=True, capture_output=True)
        return directory

    def identity(self, directory: Path) -> gate.RepositoryIdentity:
        repository, worktree = gate.git_identity(["cargo", "nextest", "run"], str(directory))
        self.assertEqual(worktree, directory.name)
        self.assertIsInstance(repository, gate.RepositoryIdentity)
        assert isinstance(repository, gate.RepositoryIdentity)
        return repository

    def samples(self, repository: gate.RepositoryIdentity, gib: int, *, scope: gate.TestScope = "full") -> None:
        with (self.admission / "anon_peaks.jsonl").open("a") as out:
            for _ in range(5):
                _ = out.write(json.dumps({"host": self.host, "repo": "repo", "step": "nextest",
                                          "repo_identity": repository.key, "test_scope": scope,
                                          "anon_peak_bytes": gib * GIB,
                                          "ended_at": store.utc_iso(HISTORY_AT.timestamp())}) + "\n")

    def estimate(self, repository: gate.Repository, scope: gate.TestScope = "full") -> gate.ExpectedPeak:
        return gate.expected_peak(repository, "nextest", self.host, NOW, self.root, scope=scope)

    def test_unrelated_repositories_named_repo_use_their_own_peaks(self) -> None:
        first = self.identity(self.checkout("one/repo", "git@example.org:nate/first.git"))
        second = self.identity(self.checkout("two/repo", "https://example.org/nate/second.git"))
        self.samples(first, 19)
        self.samples(second, 2)
        self.assertEqual(self.estimate(first), gate.ExpectedPeak(19 * GIB, "measured", 5))
        self.assertEqual(self.estimate(second), gate.ExpectedPeak(2 * GIB, "measured", 5))

    def test_same_repository_shares_peaks_across_folders_and_remote_forms(self) -> None:
        first = self.identity(self.checkout("one/repo", "git@example.org:nate/first.git"))
        second = self.identity(self.checkout("two/different", "https://user@example.org/nate/first/"))
        self.samples(first, 2)
        self.assertEqual(self.estimate(second), gate.ExpectedPeak(2 * GIB, "measured", 5))

    def test_worktree_uses_main_checkout_name_for_old_anon_and_buildlog_rows(self) -> None:
        checkout = self.checkout("hana", "https://example.org/nate/hana.git")
        worktree = self.root / "hana-fix"
        _ = subprocess.run(["git", "-C", str(checkout), "worktree", "add", "-q", "--detach", str(worktree)],
                           check=True, capture_output=True)
        identity = self.identity(worktree)
        peaks = self.admission / "anon_peaks.jsonl"
        rows = [{"host": self.host, "repo": "hana", "step": "nextest",
                 "anon_peak_bytes": 3 * GIB, "ended_at": store.utc_iso(HISTORY_AT.timestamp())}] * 5
        _ = peaks.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertEqual(self.estimate(identity), gate.ExpectedPeak(3 * GIB, "measured", 5))
        peaks.unlink()
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, peak_mem_bytes INTEGER)")
            _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
                               [(self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), 4 * GIB)] * 5)
        self.assertEqual(self.estimate(identity), gate.ExpectedPeak(int(4 * GIB * gate.ANON_SHARE), "buildlog", 5))
        scratch = self.identity(self.checkout("repo", "https://example.org/nate/hana.git"))
        self.assertEqual(self.estimate(scratch).source, "fallback")

    def test_filtered_and_full_runs_never_use_each_others_samples(self) -> None:
        identity = self.identity(self.checkout("repo", "https://example.org/nate/first"))
        self.samples(identity, 19)
        self.assertEqual(self.estimate(identity, "filtered").source, "fallback")
        self.samples(identity, 2, scope="filtered")
        self.assertEqual(self.estimate(identity, "filtered"), gate.ExpectedPeak(2 * GIB, "measured", 5))
        self.assertEqual(self.estimate(identity), gate.ExpectedPeak(19 * GIB, "measured", 5))

    def test_generic_legacy_rows_cannot_supply_an_estimate_to_a_scratch_clone(self) -> None:
        identity = self.identity(self.checkout("repo", "https://example.org/nate/first"))
        with (self.admission / "anon_peaks.jsonl").open("w") as out:
            for _ in range(7):
                _ = out.write(json.dumps({"host": self.host, "repo": "repo", "step": "nextest",
                                          "anon_peak_bytes": 19 * GIB,
                                          "ended_at": store.utc_iso(HISTORY_AT.timestamp())}) + "\n")
        self.assertEqual(self.estimate(identity).source, "fallback")

    def test_clone_uses_other_repositories_pooled_samples_without_a_basename_key(self) -> None:
        first = self.identity(self.checkout("one/repo", "https://example.org/nate/first"))
        second = self.identity(self.checkout("two/checkout", "https://example.org/nate/second"))
        self.samples(first, 3)
        self.assertEqual(self.estimate(second), gate.ExpectedPeak(3 * GIB, "pooled", 5))

    def test_no_origin_uses_root_commit_across_clones(self) -> None:
        original = self.checkout("original")
        clone = self.root / "copy"
        _ = subprocess.run(["git", "clone", "-q", str(original), str(clone)], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(clone), "remote", "remove", "origin"], check=True, capture_output=True)
        self.samples(self.identity(original), 4)
        self.assertEqual(self.estimate(self.identity(clone)), gate.ExpectedPeak(4 * GIB, "measured", 5))
        unrelated = self.identity(self.checkout("other", content="different"))
        self.assertEqual(self.estimate(unrelated).source, "pooled")

    def test_missing_identity_uses_only_the_fallback(self) -> None:
        self.samples(HANA, 19)
        self.assertEqual(self.estimate(gate.UnknownRepository()), gate.ExpectedPeak(gate.FALLBACK, "fallback", 0))

    def test_test_selection_is_carried_from_cargo_arguments(self) -> None:
        full = (["cargo", "nextest", "run", "-p", "hana", "--test-threads", "16"],
                ["cargo", "test", "--manifest-path", "/test/Cargo.toml", "--", "--nocapture"])
        filtered = (["cargo", "nextest", "run", "one_test"],
                    ["cargo", "test", "run"],
                    ["cargo", "nextest", "run", "run"],
                    ["cargo", "+nightly", "nextest", "run", "-E", "test(one)"],
                    ["cargo", "nextest", "run", "--filterset=test(one)"],
                    ["cargo", "test", "-p", "hana", "one_test"],
                    ["cargo", "test", "--", "--skip", "one_test"])
        for arguments in full:
            with self.subTest(arguments=arguments):
                self.assertEqual(gate.test_scope(arguments), "full")
        for arguments in filtered:
            with self.subTest(arguments=arguments):
                self.assertEqual(gate.test_scope(arguments), "filtered")

    def test_verify_package_and_target_runs_share_full_history_but_name_filter_does_not(self) -> None:
        identity = self.identity(self.checkout("hana", "https://example.org/nate/hana.git"))
        self.samples(identity, 9)
        self.samples(identity, 2, scope="filtered")
        cases = ((["-E", "package(hana)"], 9),
                 (["--test", "render", "-E", "package(hana)"], 9),
                 (["-E", "package(hana) & test(render)"], 2))
        for arguments, gib in cases:
            with self.subTest(arguments=arguments):
                scope = gate.test_scope(["cargo", "nextest", "run", *arguments])
                self.assertEqual(self.estimate(identity, scope), gate.ExpectedPeak(gib * GIB, "measured", 5))

    def test_filter_expressions_and_option_values_do_not_become_test_names(self) -> None:
        for option in ("-E", "--filter-expr", "--filterset"):
            for predicate, expected in (("package(hana)", "full"), ("kind(lib)", "full"),
                                        ("package(hana) & test(render)", "filtered")):
                for arguments in ([option, predicate], [option + "=" + predicate]):
                    with self.subTest(arguments=arguments):
                        self.assertEqual(gate.test_scope(["cargo", "nextest", "run", *arguments]), expected)
        for option in ("-F", "--exclude", "-P", "--run-ignored", "-Z", "--no-tests",
                       "--max-fail", "--tool-config-file", "--test-group"):
            for arguments in ([option, "value"], [option + "=value"]):
                with self.subTest(arguments=arguments):
                    argv = ["cargo", "nextest", "run", *arguments]
                    self.assertEqual(gate.test_scope(argv), "full")
                    self.assertEqual(gate.test_scope([*argv, "--", "render"]), "filtered")
        for arguments in (["--exact"], ["--skip", "render"], ["--", "render"], ["-Etest(render)"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(gate.test_scope(["cargo", "nextest", "run", *arguments]), "filtered")


class TestThreadParsingTests(unittest.TestCase):
    def test_all_integer_forms_are_recognized(self) -> None:
        cases = (
            (["cargo", "nextest", "run", "--test-threads", "16"], 16),
            (["cargo", "nextest", "run", "--test-threads=32"], 32),
            (["cargo", "nextest", "run", "-j", "12"], 12),
            (["cargo", "nextest", "run", "-j8"], 8),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(gate.test_threads(argv), expected)

    def test_absent_or_non_integer_width_is_unset(self) -> None:
        for argv in (
            ["cargo", "nextest", "run"],
            ["cargo", "nextest", "run", "--test-threads", "num-cpus"],
            ["cargo", "nextest", "run", "--test-threads=num-cpus"],
            ["cargo", "nextest", "run", "-jnum-cpus"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(gate.test_threads(argv))


class LedgerTests(unittest.TestCase):
    root: Path = Path()
    sidecar: Path = Path()
    meminfo: Path = Path()
    pid: int = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sidecar = self.root / "step.cgroup"
        self.meminfo = self.root / "meminfo"
        _ = self.meminfo.write_text("MemAvailable: 12582912 kB\n")
        self.pid = 700
        def caller_start(pid: int) -> str | None:
            return "start" if pid == self.pid else None
        _ = self.enterContext(patch.object(gate, "process_start_time", side_effect=caller_start))
        _ = self.enterContext(patch.object(gate, "process_table", return_value={self.pid: gate.ProcessIdentity(1, "start")}))
        _ = self.enterContext(patch.object(gate, "tree_memory", return_value=gate.MeasuredMemory(8 * GIB)))
        _ = self.enterContext(patch.object(gate, "git_identity", return_value=(HANA, "test-worktree")))
        _ = self.enterContext(patch.object(store, "host_name", return_value="test-host"))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root / "log")})))

    def check(self, available: int, *, force: bool = False, sidecar: str = "",
              argv: list[str] | None = None) -> tuple[gate.AdmissionDecision, Path | None]:
        self.write_meminfo(available)
        return self.admit(force=force, sidecar=sidecar, argv=argv)

    def write_meminfo(self, available: int) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {available // 1024} kB\n")

    def admit(self, *, force: bool = False, sidecar: str = "",
              argv: list[str] | None = None) -> tuple[gate.AdmissionDecision, Path | None]:
        result = gate.check(
            self.meminfo, self.pid, sidecar, argv or ["cargo", "nextest"], force, self.root, NOW
        )
        path = result.reservation.path if isinstance(result.reservation, gate.Reserved) else None
        return result.decision, path

    def test_two_concurrent_checks_admit_one(self) -> None:
        barrier = threading.Barrier(2)
        self.write_meminfo(12 * GIB)

        def arrive() -> tuple[gate.AdmissionDecision, Path | None]:
            _ = barrier.wait()
            return self.admit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(arrive)
            second = pool.submit(arrive)
            results = [first.result(), second.result()]
        self.assertEqual([decision.state for decision, _ in results].count("admit"), 1)
        self.assertEqual(len(list((self.root / "admission").glob("*.reservation"))), 1)

    def test_ramping_tree_keeps_growth_and_release_records_maximum(self) -> None:
        scope = self.root / "scope"
        scope.mkdir()
        _ = (scope / "memory.stat").write_text(f"anon {8 * GIB}\nfile 1\n")
        _ = self.sidecar.write_text(str(scope) + "\n")
        with patch.dict(os.environ, {"BUILDLOG_CALL_ID": "call-width-16"}):
            _, path = self.check(
                12 * GIB,
                sidecar=str(self.sidecar),
                argv=["cargo", "nextest", "run", "--test-threads", "16"],
            )
        assert path is not None
        with patch.object(gate, "step_memory", return_value=gate.MeasuredMemory(8 * GIB, 12 * GIB)):
            result, _ = self.check(12 * GIB)
        self.assertEqual((result.promised, result.measured), (4 * GIB, 8 * GIB))
        _ = path.with_suffix(".max").write_text(str(8 * GIB))
        with patch.object(store, "host_name", return_value="test-host"):
            gate.release(path, 7, self.root)
        self.assertFalse(path.exists())
        self.assertFalse(self.sidecar.exists())
        line = cast(dict[str, object], json.loads((self.root / "admission/anon_peaks.jsonl").read_text()))
        self.assertEqual((line["anon_peak_bytes"], line["status"]), (8 * GIB, 7))
        self.assertEqual((line["test_threads"], line["call_id"]), (16, "call-width-16"))
        self.assertEqual((line["repo_identity"], line["test_scope"]), (HANA.key, "full"))

    def test_a_run_is_admitted_on_the_history_of_every_width(self) -> None:
        # One width's history is thin; a run asking for 32 threads may claim 4 after admission.
        (self.root / "admission").mkdir()
        with (self.root / "admission/anon_peaks.jsonl").open("a") as out:
            for _ in range(5):
                _ = out.write(json.dumps({"host": "test-host", "repo": "hana", "step": "nextest",
                                          "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
                                          "anon_peak_bytes": 2 * GIB, "test_threads": 4}) + "\n")
        decision, path = self.check(4 * GIB, argv=["cargo", "nextest", "run", "--test-threads", "32"])
        self.assertEqual(decision.state, "admit")
        self.assertIsNotNone(path)

    def test_the_width_claimed_after_admission_is_the_one_recorded(self) -> None:
        _ = self.sidecar.write_text("unused\n")
        _, path = self.check(12 * GIB, sidecar=str(self.sidecar),
                             argv=["cargo", "nextest", "run", "--test-threads", "32"])
        assert path is not None
        gate.set_width(path, 8, self.root)
        _ = path.with_suffix(".max").write_text(str(GIB))
        with patch.object(store, "host_name", return_value="test-host"):
            gate.release(path, 0, self.root)
        line = cast(dict[str, object], json.loads((self.root / "admission/anon_peaks.jsonl").read_text()))
        self.assertEqual(line["test_threads"], 8)

    def test_unscoped_step_writes_no_peak(self) -> None:
        _, path = self.check(12 * GIB)
        assert path is not None
        _ = path.with_suffix(".max").write_text(str(GIB))
        gate.release(path, 0, self.root)
        self.assertFalse((self.root / "admission/anon_peaks.jsonl").exists())

    def test_dead_and_reused_pid_are_pruned(self) -> None:
        directory = gate.admission_dir(self.root)
        for pid, start in ((999999999, "0"), (self.pid, "wrong")):
            path = directory / f"{pid}.reservation"
            row: gate.Reservation = {"pid": pid, "start_time": start, "need": 12 * GIB,
                                     "repo": "hana", "step": "nextest", "worktree": "old",
                                     "admitted_at": "", "sidecar": "", "test_threads": None,
                                     "call_id": None}
            _ = path.write_text(json.dumps(row))
        decision, _ = self.check(12 * GIB)
        self.assertEqual(decision.state, "admit")
        self.assertFalse((directory / "999999999.reservation").exists())
        self.assertFalse((directory / f"{self.pid}.reservation").exists())

    def held_reservation(self, sidecar: str, admitted: datetime) -> None:
        """A live reservation of this process, admitted at admitted, for 12 GiB."""
        row: gate.Reservation = {"pid": self.pid, "start_time": gate.process_start_time(self.pid) or "",
                                 "need": 12 * GIB, "repo": "hana", "step": "clippy", "worktree": "old",
                                 "admitted_at": store.utc_iso(admitted.timestamp()), "sidecar": sidecar,
                                 "test_threads": None, "call_id": None}
        _ = (gate.admission_dir(self.root) / f"{self.pid}-held.reservation").write_text(json.dumps(row))

    def test_step_reserves_growth_only_inside_the_ramp_window(self) -> None:
        self.held_reservation(str(self.sidecar), NOW - timedelta(seconds=gate.RAMP_SECONDS - 1))
        decision, _ = self.check(12 * GIB)
        self.assertEqual((decision.promised, decision.measured), (4 * GIB, 8 * GIB))
        self.held_reservation(str(self.sidecar), NOW - timedelta(seconds=gate.RAMP_SECONDS))
        decision, _ = self.check(12 * GIB)
        self.assertEqual((decision.promised, decision.measured), (0, 8 * GIB))

    def test_unscoped_step_counts_only_measured_use_after_the_ramp_window(self) -> None:
        self.held_reservation("", NOW - timedelta(hours=1))
        decision, _ = self.check(12 * GIB)
        self.assertEqual((decision.promised, decision.measured), (0, 8 * GIB))

    def test_held_step_waits_in_line_and_an_old_waiter_goes_first(self) -> None:
        _ = self.meminfo.write_text("MemAvailable: 0 kB\n")
        held = gate.check(self.meminfo, self.pid, "", ["cargo", "check"], NOW.timestamp() - 1000, root=self.root, now=NOW)
        self.assertEqual((held.decision.state, held.reservation), ("hold", gate.NotReserved("held")))
        waiting = list((self.root / "admission").glob("*.waiting"))
        self.assertEqual(len(waiting), 1)
        older = cast(dict[str, object], json.loads(waiting[0].read_text()))
        _ = waiting[0].rename(waiting[0].with_name("other.waiting"))
        _ = self.meminfo.write_text(f"MemAvailable: {20 * GIB // 1024} kB\n")
        with mock.patch.object(gate, "process_start_time", return_value=str(older["start_time"])):
            younger = gate.check(self.meminfo, self.pid + 1, "", ["cargo", "check"], root=self.root, now=NOW)
        self.assertEqual(younger.decision.threshold - younger.peak.bytes - younger.decision.reserve, older["need"])

    def test_no_argv_reads_live_promise_without_reserving(self) -> None:
        _, path = self.check(12 * GIB)
        assert path is not None
        result = gate.check(self.meminfo, self.pid, "", [], root=self.root, now=NOW)
        self.assertEqual((result.decision.state, result.decision.promised, len(result.live)), ("hold", 4 * GIB, 1))
        self.assertEqual(result.reservation, gate.NotReserved("launch"))
        self.assertEqual(len(list((self.root / "admission").glob("*.reservation"))), 1)

    def test_held_step_has_explicit_reservation_state(self) -> None:
        _ = self.meminfo.write_text("MemAvailable: 0 kB\n")
        result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertEqual(result.reservation, gate.NotReserved("held"))

    def test_unreadable_caller_start_is_untracked(self) -> None:
        with patch.object(gate, "process_start_time", return_value=None):
            result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertEqual(result.reservation, gate.NotReserved("untracked"))

    def test_locked_check_uses_meminfo_after_scope_grows(self) -> None:
        _ = self.meminfo.write_text("MemAvailable: 20971520 kB\n")
        first = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertIsInstance(first.reservation, gate.Reserved)
        _ = self.meminfo.write_text("MemAvailable: 20971520 kB\n")
        def grow(_row: gate.Reservation, _now: datetime, _processes: dict[int, gate.ProcessIdentity],
                 _proc: Path) -> gate.StepMemory:
            _ = self.meminfo.write_text("MemAvailable: 8388608 kB\n")
            return gate.MeasuredMemory(8 * GIB)
        with patch.object(gate, "step_memory", side_effect=grow):
            result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertEqual((result.available, result.decision.state), (8 * GIB, "hold"))

    def test_profile_lookups_run_once_across_three_polls(self) -> None:
        with patch.object(gate, "git_identity", return_value=(HANA, "tree")) as identity, \
             patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(12 * GIB, "fallback", 0)) as history:
            profile = None
            for _ in range(3):
                result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW, profile=profile)
                profile = result.profile
        self.assertEqual(identity.call_count, 1)
        self.assertEqual(history.call_count, 1)


class MemoryQueueFixture(unittest.TestCase):
    root: Path = Path()
    meminfo: Path = Path()
    processes: dict[int, str] = {}
    readings: dict[int, gate.MeasuredMemory | gate.GoneMemory] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.meminfo = self.root / "meminfo"
        self.processes = {100: "first", 200: "second", 300: "head", 400: "warmup"}
        self.readings = {100: gate.MeasuredMemory(GIB), 200: gate.MeasuredMemory(2 * GIB)}
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root), "BUILDLOG_PROC": str(self.root / "proc"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        _ = self.enterContext(patch.object(gate, "process_start_time", side_effect=self.processes.get))
        def table(_proc: Path) -> dict[int, gate.ProcessIdentity]:
            return {pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()}
        _ = self.enterContext(patch.object(gate, "process_table", side_effect=table))
        def reading(pid: int, _start: str, _processes: dict[int, gate.ProcessIdentity],
                    _proc: Path) -> gate.MeasuredMemory | gate.GoneMemory:
            return self.readings[pid]
        _ = self.enterContext(patch.object(gate, "tree_memory", side_effect=reading))

    def running(self, pid: int, need: float, age: float = 180) -> Path:
        row: gate.Reservation = {"pid": pid, "start_time": self.processes[pid], "need": int(need * GIB),
                                 "repo": "test", "step": "nextest", "worktree": "test", "sidecar": "",
                                 "admitted_at": store.utc_iso(NOW.timestamp() - age),
                                 "test_threads": None, "call_id": None}
        path = gate.admission_dir(self.root) / f"{pid}.reservation"
        _ = path.write_text(json.dumps(row))
        return path

    def head(self, need: float = 10.9) -> Path:
        row: gate.Waiter = {"pid": 300, "start_time": "head", "need": int(need * GIB),
                            "since": NOW.timestamp() - 2400, "label": "clone88ci nextest (verify pid 300)"}
        path = gate.admission_dir(self.root) / "300-head.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def check(self, pid: int = 400, need: float = 11.6, free: float = 26,
              since: float = 0) -> gate.CheckResult:
        _ = self.meminfo.write_text(f"MemAvailable: {int(free * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        return gate.check(self.meminfo, pid, "", ["cargo", "nextest"], since,
                          root=self.root, now=NOW,
                          profile=gate.StepProfile("test", "nextest", "test", gate.ExpectedPeak(int(need * GIB), "measured", 5)))



class CurrentBlockerTests(MemoryQueueFixture):
    reservation: Path = Path()
    waiter_path: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, fake_memory_readings(
            self.root, available_kib=26 * GIB // 1024, total_kib=60 * GIB // 1024))))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = gate.admission_dir(self.root).joinpath("lock").write_text("")
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        self.reservation = self.running(100, 12)
        process = self.root / "proc/100"
        process.mkdir(parents=True)
        _ = (process / "environ").write_bytes(
            b"CLAUDE_CODE_SESSION_ID=owner-id\0CLAUDE_SESSION_NAME=owner-name\0")
        self.waiter_path = self.root / "admission/400-warmup.waiting"
        _ = self.waiter_path.write_text(json.dumps({
            "pid": 400, "start_time": "warmup", "need": 12 * GIB, "since": NOW.timestamp(),
        }))

    def verb(self) -> tuple[int, dict[str, str]]:
        output = io.StringIO()
        code = 0
        with patch.object(sys, "argv", ["memory_admit.py", "blocker", "--waiter", str(self.waiter_path)]), \
                redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            try:
                gate.main()
            except SystemExit as error:
                code = cast(int, error.code)
        lines = output.getvalue().splitlines()
        if code == 0:
            self.assertEqual(len(lines), 1)
            return code, cast(dict[str, str], json.loads(lines[0]))
        return code, {}

    def test_blocker_names_the_session_that_would_release_admission(self) -> None:
        result = gate.current_blocker(self.waiter_path)
        self.assertIsInstance(result, gate.ReservationBlocker)
        self.assertEqual(self.verb(), (0, {
            "session_id": "owner-id", "session_name": "owner-name", "pool": "memory"}))

    def test_blocker_names_only_the_scope_when_session_is_unknown(self) -> None:
        (self.root / "proc/100/environ").unlink()
        sidecar = self.root / "holder.cgroup"
        _ = sidecar.write_text("/fake/cgroup/builds.slice/build-holder.scope\n")
        row = cast(dict[str, object], json.loads(self.reservation.read_text()))
        row["sidecar"] = str(sidecar)
        _ = self.reservation.write_text(json.dumps(row))
        self.assertIsInstance(gate.current_blocker(self.waiter_path), gate.UnitBlocker)
        self.assertEqual(self.verb(), (0, {"unit": "build-holder.scope"}))

    def test_no_single_blocker_for_pressure_ready_ambiguous_or_unknown_owner(self) -> None:
        for reason in ("pressure", "ready", "ambiguous", "unknown-owner", "no-reservation"):
            with self.subTest(reason=reason):
                with patch.dict(os.environ, fake_memory_readings(
                        self.root, available_kib=26 * GIB // 1024, total_kib=60 * GIB // 1024,
                        full_avg10=11 if reason == "pressure" else 0)):
                    self.readings[100] = gate.MeasuredMemory(GIB if reason == "ready" else 12 * GIB)
                    extra = self.root / "admission/200.reservation"
                    if reason == "ambiguous":
                        self.readings[100] = gate.MeasuredMemory(6 * GIB)
                        self.readings[200] = gate.MeasuredMemory(6 * GIB)
                        extra = self.running(200, 6)
                    if reason == "unknown-owner":
                        (self.root / "proc/100/environ").unlink()
                    if reason == "no-reservation":
                        self.reservation.unlink()
                    self.assertIsInstance(gate.current_blocker(self.waiter_path), gate.NoSingleBlocker)
                    self.assertEqual(self.verb(), (0, {}))
                    extra.unlink(missing_ok=True)

    def test_removing_one_of_several_reservations_must_be_enough(self) -> None:
        self.readings[100] = gate.MeasuredMemory(18 * GIB)
        self.readings[200] = gate.MeasuredMemory(10 * GIB)
        _ = self.running(200, 10)
        self.assertEqual(self.verb(), (0, {}))
        self.readings[200] = gate.MeasuredMemory(2 * GIB)
        self.assertEqual(self.verb(), (0, {
            "session_id": "owner-id", "session_name": "owner-name", "pool": "memory"}))

    def test_queue_priority_is_not_blame_for_a_running_reservation(self) -> None:
        _ = self.head(need=1)
        self.assertEqual(self.verb(), (0, {}))

    def test_measured_memory_release_can_remove_the_free_memory_hold(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {8 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        self.assertEqual(self.verb(), (0, {
            "session_id": "owner-id", "session_name": "owner-name", "pool": "memory"}))

    def test_gone_waiter_exits_two_but_stale_waiter_has_no_blocker(self) -> None:
        del self.processes[400]
        self.assertEqual(self.verb(), (0, {}))
        self.waiter_path.unlink()
        self.assertEqual(self.verb()[0], 2)

    def test_missing_process_reading_is_not_reported_as_a_gone_waiter(self) -> None:
        with patch.object(gate, "process_table", side_effect=FileNotFoundError("missing process snapshot")):
            with self.assertRaisesRegex(FileNotFoundError, "missing process snapshot"):
                _ = self.verb()

    def test_blocker_does_not_prune_or_change_state_bytes_or_mtimes(self) -> None:
        directory = self.root / "admission"
        _ = (directory / "stale.waiting").write_text('{"pid":999,"start_time":"gone"}')
        _ = (directory / "stale.reservation").write_text('{"pid":999,"start_time":"gone"}')
        _ = (directory / "stale.max").write_text("123")
        _ = (directory / "broken.reservation").write_text("partial")
        def snapshot() -> dict[str, tuple[bytes, int]]:
            return {str(path.relative_to(self.root)): (path.read_bytes() if path.is_file() else b"", path.stat().st_mtime_ns)
                    for path in [self.root, *self.root.rglob("*")]}
        for has_lock in (True, False):
            with self.subTest(lock=has_lock):
                if not has_lock:
                    (directory / "lock").unlink()
                before = snapshot()
                self.assertEqual(self.verb()[1].get("session_id"), "owner-id")
                self.assertEqual(snapshot(), before)

    def test_check_reports_holder_changes_on_existing_poll_with_opt_in_field(self) -> None:
        arguments = ["memory_admit.py", "check", str(self.meminfo), "400", "", "0",
                     "", "", "", "-", "", "0", "cargo", "nextest"]
        for name in ("owner-name", "new-owner"):
            _ = (self.root / "proc/100/environ").write_text(
                f"CLAUDE_CODE_SESSION_ID=owner-id\0CLAUDE_SESSION_NAME={name}\0")
            result = self.check(need=12)
            self.assertEqual(result.decision.state, "hold")
            self.assertEqual(result.blocker, gate.ReservationBlocker("owner-id", name))
            for detail, old_count in (("", 15), ("1", 18)):
                output = io.StringIO()
                with patch.object(sys, "argv", arguments), \
                        patch.dict(os.environ, {"BUILDLOG_MEM_DETAIL": detail, "BUILDLOG_MEM_BLOCKER": "1"}), \
                        patch.object(gate, "git_identity", return_value=(gate.UnknownRepository(), "")), \
                        redirect_stdout(output):
                    gate.main()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(len(fields), old_count + 1)
                self.assertEqual(fields[0], "hold")
                self.assertEqual(json.loads(fields[-1]), {
                    "session_id": "owner-id", "session_name": name, "pool": "memory"})
        self.readings[100] = gate.MeasuredMemory(GIB)
        result = self.check(need=12)
        self.assertEqual(result.decision.state, "admit")
        self.assertIsInstance(result.blocker, gate.NoSingleBlocker)


class MeasuredAdmissionTests(MemoryQueueFixture):
    def test_five_mature_steps_with_43_point_4_promised_admit_10_point_8_on_6_point_8_used(self) -> None:
        for pid in (100, 200, 500, 600, 700):
            self.processes[pid] = str(pid)
            self.readings[pid] = gate.MeasuredMemory(int(1.36 * GIB))
            _ = self.running(pid, 8.68)
        _ = self.meminfo.write_text(f"MemAvailable: {int(18.1 * GIB) // 1024} kB\nMemTotal: {int(60.5 * GIB) // 1024} kB\n")
        result = gate.check(self.meminfo, 400, "", ["cargo", "check"], root=self.root, now=NOW,
                            profile=gate.StepProfile("test", "check", "test", gate.ExpectedPeak(int(10.8 * GIB), "measured", 5)))
        self.assertIsInstance(result.reservation, gate.Reserved)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))
        self.assertAlmostEqual(result.decision.charged / GIB, 6.8)

    def test_mature_steps_using_12_gib_hold_10_point_8_against_the_budget(self) -> None:
        _ = self.running(100, 43.4)
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        result = self.check(need=10.8, free=18.1)
        self.assertEqual((result.decision.state, result.decision.charged), ("hold", 12 * GIB))
        self.assertGreater(result.available, result.decision.threshold)
        with patch.dict(os.environ, {"BUILDLOG_MEMINFO": str(self.meminfo), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")}):
            view = gate.read_queue(gate.admission_dir(self.root), gate.process_table(gate.PROC), NOW.timestamp())[0]
        self.assertEqual(view.waiting[0].reason, result.head_reason)
        self.assertIsInstance(result.head_reason, gate.HeadAdmissionStatus)

    def test_two_10_point_8_steps_cannot_start_together_inside_ramp(self) -> None:
        self.readings[100] = gate.MeasuredMemory(0)
        first = self.check(pid=100, need=10.8, free=40)
        self.assertIsInstance(first.reservation, gate.Reserved)
        second = self.check(pid=200, need=10.8, free=40)
        self.assertEqual((second.decision.state, second.decision.charged), ("hold", int(10.8 * GIB)))

    def test_22_gib_step_runs_alone_despite_the_budget(self) -> None:
        result = self.check(need=22, free=30)
        self.assertIsInstance(result.reservation, gate.Reserved)

    def test_memory_floor_still_holds_with_room_in_the_budget(self) -> None:
        _ = self.running(100, 12)
        result = self.check(need=10.8, free=9)
        self.assertEqual(result.decision.state, "hold")
        self.assertLess(result.decision.charged + result.peak.bytes, result.decision.budget)

    def test_ramp_charge_covers_measured_growth_above_the_promise(self) -> None:
        _ = self.running(100, 2, age=gate.RAMP_SECONDS - 1)
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        result = self.check(need=10, free=40)
        self.assertEqual((result.decision.state, result.decision.charged, result.decision.promised), ("hold", 12 * GIB, 0))

    def test_head_budget_keeps_room_before_warmup(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        _ = self.head()
        result = self.check()
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual((result.decision.promised, result.decision.measured), (0, 3 * GIB))
        self.assertEqual((result.head_need, result.head_label), (int(10.9 * GIB), "clone88ci nextest (verify pid 300)"))

    def test_dead_steps_promise_leaves_the_sum_at_once(self) -> None:
        path = self.running(100, 23.5, age=0)
        del self.processes[100]
        result = self.check(free=15)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))
        self.assertFalse(path.exists())

    def test_fresh_step_keeps_its_promise_then_uses_its_first_reading(self) -> None:
        _ = self.running(100, 23.5, age=0)
        self.assertEqual(self.check().decision.promised, int(22.5 * GIB))
        _ = self.running(100, 23.5, age=gate.RAMP_SECONDS)
        result = self.check()
        self.assertEqual((result.decision.promised, result.decision.measured), (0, GIB))

    def test_clone88ci_head_starts_when_measured_free_memory_covers_it(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        head = self.head()
        result = self.check(pid=300, need=10.9, free=35, since=NOW.timestamp() - 2400)
        self.assertEqual((result.decision.state, result.head_need), ("admit", 0))
        self.assertFalse(head.exists())

    def test_head_beyond_memory_released_by_running_steps_does_not_hold_smaller_steps(self) -> None:
        _ = self.running(100, 1)
        _ = self.head(20)
        result = self.check(need=2, free=8)
        self.assertEqual((result.decision.state, result.head_need), ("admit", 0))

    def test_head_that_fits_after_measured_steps_end_holds_smaller_steps(self) -> None:
        _ = self.running(100, 30)
        self.readings[100] = gate.MeasuredMemory(int(21.5 * GIB))
        _ = self.head(19.3)
        result = self.check(need=2, free=15)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual((result.head_need, result.head_label),
                         (int(19.3 * GIB), "clone88ci nextest (verify pid 300)"))
        self.assertIsInstance(result.reservation, gate.NotReserved)

    def test_pending_start_promise_cannot_make_a_head_fit_after_drain(self) -> None:
        _ = self.running(100, 12, age=0)
        _ = self.head(20)
        result = self.check(need=2, free=18)
        self.assertEqual((result.decision.state, result.head_need, result.decision.promised),
                         ("admit", 0, 11 * GIB))

    def test_dead_head_is_removed_without_charging_its_need(self) -> None:
        head = self.head()
        del self.processes[300]
        result = self.check(free=15)
        self.assertEqual((result.decision.state, result.head_need), ("admit", 0))
        self.assertFalse(head.exists())

    def test_reading_a_finished_tree_does_not_keep_a_promise(self) -> None:
        _ = self.running(100, 23.5)
        self.readings[100] = gate.GoneMemory()
        result = self.check(free=15)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))

    def test_check_charges_only_measured_use_after_ramp(self) -> None:
        path = self.running(100, 12)
        for used, state in ((1, "admit"), (11, "admit"), (13, "hold")):
            with self.subTest(measured_gib=used):
                self.readings[100] = gate.MeasuredMemory(used * GIB)
                result = self.check(need=8, free=15)
                self.assertEqual((result.decision.state, result.decision.promised), (state, 0))
                for reservation in (self.root / "admission").glob("*.reservation"):
                    if reservation != path:
                        reservation.unlink()


class HostMemoryAdmissionTests(MemoryQueueFixture):
    pressure: Path = Path()
    builds: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        environment = fake_memory_readings(self.root)
        self.pressure = Path(environment["BUILDLOG_MEMORY_PRESSURE"])
        self.builds = self.root / "builds.slice"
        environment["BUILDLOG_BUILDS_CGROUP"] = str(self.builds)
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))

    def readings_for(self, *, swap_free: int = 100, some: float = 0, full: float = 0) -> None:
        _ = self.meminfo.write_text(
            f"MemAvailable: {40 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n"
            + f"SwapTotal: 100 kB\nSwapFree: {swap_free} kB\n")
        _ = self.pressure.write_text(f"some avg10={some} total=0\nfull avg10={full} total=0\n")

    def admit_at(self, pid: int = 400, seconds: float = 0, need: float = 8) -> gate.CheckResult:
        return gate.check(self.meminfo, pid, "", ["cargo", "check"], root=self.root,
                          now=NOW + timedelta(seconds=seconds),
                          profile=gate.StepProfile("test", "check", "test", gate.ExpectedPeak(int(need * GIB), "measured", 5)))

    def test_mature_step_keeps_observed_peak_in_budget_and_blocker(self) -> None:
        self.readings_for()
        reservation = self.running(100, 12)
        _ = reservation.with_suffix(".max").write_text(str(13 * GIB))
        os.utime(reservation.with_suffix(".max"), (NOW.timestamp(), NOW.timestamp()))
        self.readings[100] = gate.MeasuredMemory(2 * GIB)
        result = self.admit_at()
        self.assertEqual((result.decision.state, result.decision.charged, result.decision.measured),
                         ("hold", 13 * GIB, 2 * GIB))
        directory = self.root / "admission"
        view, _ = gate.read_queue(directory, gate.process_table(self.root / "proc"), NOW.timestamp())
        self.assertEqual(view.waiting[0].reason, result.head_reason)
        _ = (self.root / "proc/100").mkdir(parents=True)
        _ = (self.root / "proc/100/environ").write_bytes(b"CLAUDE_CODE_SESSION_ID=holder\0")
        with patch("memory_admit.time.time", return_value=NOW.timestamp()):
            self.assertEqual(gate.blocker_fields(gate.current_blocker(directory / "400-warmup.waiting")),
                             {"session_id": "holder", "session_name": "", "pool": "memory"})

    def test_peak_charge_expires_at_1500_seconds_since_near_peak_use(self) -> None:
        for age, expected_charge, expected_state in ((1499, 13, "hold"), (1500, 2, "admit")):
            with self.subTest(age=age):
                self.readings_for()
                reservation = self.running(100, 12)
                _ = reservation.with_suffix(".max").write_text(str(13 * GIB))
                _ = reservation.with_suffix(".wave").write_text(f"{13 * GIB} {int(NOW.timestamp()) - age}")
                self.readings[100] = gate.MeasuredMemory(2 * GIB)
                result = self.admit_at()
                self.assertEqual((result.decision.state, result.decision.charged),
                                 (expected_state, expected_charge * GIB))
                if isinstance(result.reservation, gate.Reserved):
                    gate.release(result.reservation.path, 0, self.root)

    def test_missing_near_peak_record_uses_peak_publication_time(self) -> None:
        for age, expected_charge, expected_state in ((0, 13, "hold"), (1499, 13, "hold"), (1500, 2, "admit")):
            with self.subTest(age=age):
                self.readings_for()
                reservation = self.running(100, 12)
                maximum = reservation.with_suffix(".max")
                _ = maximum.write_text(str(13 * GIB))
                published_at = NOW.timestamp() - age
                os.utime(maximum, (published_at, published_at))
                self.readings[100] = gate.MeasuredMemory(2 * GIB)
                result = self.admit_at()
                self.assertEqual((result.decision.state, result.decision.charged),
                                 (expected_state, expected_charge * GIB))
                if isinstance(result.reservation, gate.Reserved):
                    gate.release(result.reservation.path, 0, self.root)

    def test_recent_near_peak_use_renews_an_old_peak_charge(self) -> None:
        self.readings_for()
        reservation = self.running(100, 12, age=4000)
        maximum = reservation.with_suffix(".max")
        _ = maximum.write_text(str(13 * GIB))
        os.utime(maximum, (NOW.timestamp() - 3000, NOW.timestamp() - 3000))
        _ = reservation.with_suffix(".wave").write_text(f"{13 * GIB} {int(NOW.timestamp()) - 10}")
        self.readings[100] = gate.MeasuredMemory(2 * GIB)
        result = self.admit_at()
        self.assertEqual((result.decision.state, result.decision.charged), ("hold", 13 * GIB))

    def test_unknown_near_peak_time_keeps_peak_charge(self) -> None:
        self.readings_for()
        reservation = self.running(100, 12)
        _ = reservation.with_suffix(".max").write_text(str(13 * GIB))
        os.utime(reservation.with_suffix(".max"), (NOW.timestamp(), NOW.timestamp()))
        near_peak = reservation.with_suffix(".wave")
        self.readings[100] = gate.MeasuredMemory(2 * GIB)
        for contents in (None, "", "bad", "-1", str(int(NOW.timestamp()) - 2000),
                         f"{13 * GIB} -1", "directory"):
            with self.subTest(contents=contents):
                if contents == "directory":
                    near_peak.unlink()
                    near_peak.mkdir()
                elif contents is not None:
                    _ = near_peak.write_text(contents)
                result = self.admit_at()
                self.assertEqual((result.decision.state, result.decision.charged), ("hold", 13 * GIB))

    def test_invalid_near_peak_record_does_not_use_expired_peak_publication_time(self) -> None:
        self.readings_for()
        reservation = self.running(100, 12)
        maximum = reservation.with_suffix(".max")
        _ = maximum.write_text(str(13 * GIB))
        published_at = NOW.timestamp() - 2000
        os.utime(maximum, (published_at, published_at))
        near_peak = reservation.with_suffix(".wave")
        self.readings[100] = gate.MeasuredMemory(2 * GIB)
        for contents in ("", "bad", f"{13 * GIB} -1", str(int(published_at)),
                         f"{12 * GIB} {int(published_at)}", "directory"):
            with self.subTest(contents=contents):
                if contents == "directory":
                    near_peak.unlink()
                    near_peak.mkdir()
                else:
                    _ = near_peak.write_text(contents)
                result = self.admit_at()
                self.assertEqual((result.decision.state, result.decision.charged), ("hold", 13 * GIB))

    def test_new_peak_keeps_charge_until_its_own_near_peak_record_is_published(self) -> None:
        self.readings_for()
        reservation = self.running(100, 12)
        _ = reservation.with_suffix(".max").write_text("200")
        self.readings[100] = gate.MeasuredMemory(1)
        for record in (f"100 {int(NOW.timestamp()) - 2000}",
                       f"200 {int(NOW.timestamp())}"):
            with self.subTest(record=record):
                _ = reservation.with_suffix(".wave").write_text(record)
                result = self.admit_at()
                self.assertEqual(result.decision.charged, 200)
                if isinstance(result.reservation, gate.Reserved):
                    gate.release(result.reservation.path, 0, self.root)

    def test_release_and_dead_reservation_cleanup_remove_near_peak_time(self) -> None:
        for cleanup in ("release", "dead_pid", "gone_tree"):
            with self.subTest(cleanup=cleanup):
                self.readings_for()
                self.processes[100] = "100"
                self.readings[100] = gate.MeasuredMemory(2 * GIB)
                reservation = self.running(100, 12)
                maximum = reservation.with_suffix(".max")
                near_peak = reservation.with_suffix(".wave")
                _ = maximum.write_text(str(13 * GIB))
                _ = near_peak.write_text(str(int(NOW.timestamp())))
                if cleanup == "release":
                    gate.release(reservation, 0, self.root)
                else:
                    if cleanup == "dead_pid":
                        del self.processes[100]
                    else:
                        self.readings[100] = gate.GoneMemory()
                    result = self.admit_at()
                    if isinstance(result.reservation, gate.Reserved):
                        gate.release(result.reservation.path, 0, self.root)
                self.assertFalse(reservation.exists())
                self.assertFalse(maximum.exists())
                self.assertFalse(near_peak.exists())

    def test_bad_peak_keeps_promise_and_current_memory(self) -> None:
        self.readings_for()
        reservation = self.running(100, 13)
        maximum = reservation.with_suffix(".max")
        for contents in ("", "broken", "-1"):
            for current in (2, 14):
                with self.subTest(contents=contents, current=current):
                    _ = maximum.write_text(contents)
                    self.readings[100] = gate.MeasuredMemory(current * GIB)
                    result = self.admit_at(need=8)
                    if isinstance(result.reservation, gate.Reserved):
                        result.reservation.path.unlink()
                    self.assertEqual(result.decision.state, "hold")
                    self.assertEqual(result.decision.charged, max(13, current) * GIB)

    def test_sampler_replaces_peak_without_changing_open_snapshot(self) -> None:
        scope = self.root / "scope"
        scope.mkdir()
        _ = (scope / "memory.stat").write_text(f"anon {13 * GIB}\n")
        sidecar = self.root / "scope-path"
        _ = sidecar.write_text(str(scope) + "\n")
        maximum = self.root / "sample.max"
        _ = maximum.write_text(str(12 * GIB) + "\n")
        # One sampling iteration; retain an open reader of the old inode.
        program = INVOKE.read_text().split("buildlog_sample_anon() {", 1)[1].split("\n}\n", 1)[0]
        with maximum.open() as before:
            result = subprocess.run(["bash", "-c", "buildlog_sample_anon() {" + program +
                '\n}\nsleep() { exit 0; }\nbuildlog_sample_anon "$1" "$2" "$$"',
                "sampler-test", str(sidecar), str(maximum)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(int(maximum.read_text()), 13 * GIB)
            self.assertEqual(int(before.read()), 12 * GIB)

    def test_launches_share_recovery_start_spacing(self) -> None:
        self.readings_for(full=6)
        self.assertEqual(self.admit_at(need=1).decision.state, "hold")
        self.readings_for()
        for second, expected in ((1, "admit"), (2, "hold"), (31, "admit")):
            result = gate.check(self.meminfo, 400, "", [], root=self.root,
                                now=NOW + timedelta(seconds=second),
                                profile=gate.StepProfile("test", "launch", "test", gate.ExpectedPeak(GIB, "fallback", 0)))
            self.assertEqual(result.decision.state, expected, f"launch at +{second}s")
            self.assertIsInstance(result.reservation, gate.NotReserved)

    def test_recovery_spacing_uses_clock_after_lock_acquisition(self) -> None:
        self.readings_for()
        directory = gate.admission_dir(self.root)
        acquire_lock = fcntl.flock
        for held in (False, True):
            with self.subTest(hold_clears=held):
                gate.write_recovery(directory, gate.PressureHoldRecorded() if held else
                                    gate.RecoveryStarts(NOW.timestamp() + 120, NOW.timestamp()))
                clock = NOW

                def delayed_lock(file: TextIO, operation: int) -> None:
                    nonlocal clock
                    acquire_lock(file, operation)
                    clock = max(clock, NOW + timedelta(seconds=40))

                def read_clock(_zone: object) -> datetime:
                    return clock

                with patch("memory_admit.fcntl.flock", side_effect=delayed_lock), \
                        patch.object(gate, "datetime", wraps=datetime) as wall_clock:
                    wall_clock.configure_mock(**{"now.side_effect": read_clock})
                    for second, expected in ((0, "admit"), (40, "hold"), (69, "hold"), (70, "admit")):
                        clock = NOW + timedelta(seconds=second)
                        result = gate.check(self.meminfo, 400, "", [], root=self.root,
                                            profile=gate.StepProfile("test", "launch", "test",
                                                                     gate.ExpectedPeak(GIB, "fallback", 0)))
                        self.assertEqual(result.decision.state, expected, f"launch at +{second}s")
                    self.assertEqual(gate.read_recovery(directory), gate.RecoveryStarts(
                        NOW.timestamp() + (160 if held else 120), NOW.timestamp() + 100))

    def test_estimate_expiry_after_lock_keeps_queue_and_recovery_for_next_poll(self) -> None:
        self.readings_for()
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        directory = self.root / "admission"
        recovery = gate.RecoveryStarts(NOW.timestamp() + 120, NOW.timestamp())
        gate.write_recovery(directory, recovery)
        path = directory / "400-warmup.waiting"
        previous: gate.Waiter = {
            "pid": 400, "start_time": "warmup", "need": 8 * GIB,
            "since": NOW.timestamp() - 900, "label": "waiting check",
            "repo_identity": "", "test_scope": "full", "estimated_at": NOW.timestamp() - 590,
            "bump_order": -1, "holds_younger": True, "priority_since": NOW.timestamp() - 60,
        }
        _ = path.write_text(json.dumps(previous))
        clock, locked = NOW, False
        acquire_lock = fcntl.flock

        def delayed_lock(file: TextIO, operation: int) -> None:
            nonlocal clock, locked
            acquire_lock(file, operation)
            locked = True
            clock = max(clock, NOW + timedelta(seconds=40))

        def read_clock(_zone: object) -> datetime:
            return clock

        def refresh(*_args: object, **_kwargs: object) -> gate.ExpectedPeak:
            self.assertFalse(locked, "history must refresh before acquiring the admission lock")
            return gate.ExpectedPeak(12 * GIB, "measured", 5)

        arguments = ["memory_admit.py", "check", str(self.meminfo), "400", "", "0",
                     "test", "check", "test", str(8 * GIB), "measured", "5", "cargo", "check"]
        with patch("memory_admit.fcntl.flock", side_effect=delayed_lock), \
                patch.object(gate, "datetime", wraps=datetime) as wall_clock, \
                patch("memory_admit.time.time", side_effect=lambda: clock.timestamp()), \
                patch.object(gate, "git_identity", return_value=(gate.UnknownRepository(), "test")), \
                patch.object(gate, "expected_peak", side_effect=refresh) as history:
            wall_clock.configure_mock(**{"now.side_effect": read_clock})
            for poll in range(2):
                output = io.StringIO()
                with patch.object(sys, "argv", arguments), redirect_stdout(output):
                    gate.run_action()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(fields[0], "hold")
                self.assertEqual(fields[9], "")
                self.assertEqual(list(directory.glob("*.reservation")), [directory / "100.reservation"])
                row = cast(gate.Waiter, json.loads(path.read_text()))
                for key in ("since", "bump_order", "holds_younger", "priority_since"):
                    self.assertEqual(row.get(key), previous.get(key))
                self.assertEqual(gate.read_recovery(directory), recovery)
                self.assertEqual(history.call_count, poll)
                if poll == 0:
                    self.assertIn("estimate expired", fields[8])
                    self.assertEqual(row.get("estimated_at"), previous["estimated_at"])
                    self.assertEqual(row["need"], 8 * GIB)
                else:
                    self.assertEqual(row.get("estimated_at"), clock.timestamp())
                    self.assertEqual(row["need"], 12 * GIB)
                    self.assertEqual(int(fields[2]), 12 * GIB)
                clock, locked = NOW + timedelta(seconds=45), False
                arguments = [*arguments[:6], fields[10], fields[11], fields[14],
                             fields[2], fields[3], fields[4], "cargo", "check"]

    def test_expiry_boundary_holds_priority_head_even_without_running_builds(self) -> None:
        self.readings_for()
        directory = gate.admission_dir(self.root)
        acquire_lock = fcntl.flock
        for computed, delay, expected in ((False, 9, "admit"), (False, 10, "hold"),
                                          (False, 40, "hold"), (True, 600, "hold")):
            with self.subTest(new_estimate=computed, lock_delay=delay):
                for path in directory.glob("*.reservation"):
                    path.unlink()
                waiter = directory / "400-warmup.waiting"
                _ = waiter.write_text(json.dumps({
                    "pid": 400, "start_time": "warmup", "need": 8 * GIB,
                    "since": NOW.timestamp() - 900, "label": "priority head",
                    "bump_order": -1, "holds_younger": True, "priority_since": NOW.timestamp(),
                }))
                clock = NOW

                def delayed_lock(file: TextIO, operation: int) -> None:
                    nonlocal clock
                    acquire_lock(file, operation)
                    clock += timedelta(seconds=delay)

                def read_clock(_zone: object) -> datetime:
                    return clock

                profile = None if computed else gate.StepProfile(
                    "test", "check", "test", gate.ExpectedPeak(8 * GIB, "measured", 5),
                    estimated_at=NOW.timestamp() - 590)
                with patch("memory_admit.fcntl.flock", side_effect=delayed_lock), \
                        patch.object(gate, "datetime", wraps=datetime) as wall_clock, \
                        patch.object(gate, "git_identity", return_value=(gate.UnknownRepository(), "test")), \
                        patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(8 * GIB, "measured", 5)):
                    wall_clock.configure_mock(**{"now.side_effect": read_clock})
                    result = gate.check(self.meminfo, 400, "", ["cargo", "check"],
                                        root=self.root, profile=profile)
                self.assertEqual(result.decision.state, expected)
                self.assertEqual(result.profile.estimated_at, NOW.timestamp() - (0 if computed else 590))
                if expected == "hold":
                    self.assertIsInstance(result.reservation, gate.NotReserved)
                    self.assertEqual(list(directory.glob("*.reservation")), [])
                    self.assertTrue(waiter.exists())

    def test_launch_pressure_hold_creates_recovery_on_an_empty_ledger(self) -> None:
        for second, full, expected in ((0, 6, "hold"), (1, 0, "admit"), (2, 0, "hold")):
            self.readings_for(full=full)
            result = gate.check(self.meminfo, 400, "", [], root=self.root,
                                now=NOW + timedelta(seconds=second),
                                profile=gate.StepProfile("test", "launch", "test", gate.ExpectedPeak(GIB, "fallback", 0)))
            self.assertEqual(result.decision.state, expected, f"launch at +{second}s")
            self.assertIsInstance(result.reservation, gate.NotReserved)

    def test_bad_recovery_allows_admission_with_one_reason_line(self) -> None:
        self.readings_for()
        recovery = gate.admission_dir(self.root) / "pressure-recovery"
        for contents in ('{"state":', '[]', '{"state":"recovering","ends_at":false}',
                         '{"state":"recovering","ends_at":NaN,"next_start_at":1}'):
            with self.subTest(contents=contents):
                _ = recovery.write_text(contents)
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    result = self.admit_at(need=1)
                self.assertEqual(result.decision.state, "admit")
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertIn("pressure recovery record", lines[0])
                self.assertIn("ignored", lines[0])
                if isinstance(result.reservation, gate.Reserved):
                    result.reservation.path.unlink()

    def test_bad_recovery_reason_keeps_checker_output_one_line(self) -> None:
        self.readings_for()
        _ = (gate.admission_dir(self.root) / "pressure-recovery").write_text('{"state":')
        output, errors = io.StringIO(), io.StringIO()
        arguments = ["memory_admit.py", "check", str(self.meminfo), "400", "", "0", "", "", "", "-", "", "0"]
        with patch.object(sys, "argv", arguments), redirect_stdout(output), contextlib.redirect_stderr(errors), \
                patch.object(gate, "git_identity", return_value=(gate.UnknownRepository(), "")), \
                patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(GIB, "fallback", 0)):
            gate.main()
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        fields = output.getvalue().strip().split("|")
        self.assertEqual(fields[0], "admit")
        self.assertIn("pressure recovery record", fields[8])
        self.assertIn("ignored", fields[8])

    def test_recovery_replace_preserves_previous_snapshot(self) -> None:
        directory = gate.admission_dir(self.root)
        path = directory / "pressure-recovery"
        gate.write_recovery(directory, gate.PressureHoldRecorded())
        with path.open() as before:
            gate.write_recovery(directory, gate.RecoveryStarts(120, 30))
            self.assertEqual(json.loads(before.read()), {"state": "held"})
        self.assertEqual(gate.read_recovery(directory), gate.RecoveryStarts(120, 30))

    def test_sccache_anon_is_one_charge_and_missing_or_unreadable_is_zero(self) -> None:
        self.readings_for()
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(8 * GIB)
        service = self.builds / "sccache.service"
        service.mkdir(parents=True)
        stat = service / "memory.stat"
        _ = stat.write_text(f"anon {6 * GIB}\nfile {90 * GIB}\n")
        result = self.admit_at()
        self.assertEqual((result.decision.state, result.decision.charged), ("hold", 14 * GIB))
        view, _ = gate.read_queue(self.root / "admission", gate.process_table(self.root / "proc"), NOW.timestamp())
        self.assertEqual(len(view.running), 2)
        self.assertEqual(view.waiting[0].reason, result.head_reason)
        with patch("memory_admit.time.time", return_value=NOW.timestamp()):
            self.assertIsInstance(gate.current_blocker(self.root / "admission/400-warmup.waiting"), gate.NoSingleBlocker)
        stat.unlink()
        for unreadable in (False, True):
            if unreadable:
                stat.mkdir()
            result = self.admit_at()
            self.assertEqual((result.decision.state, result.decision.charged), ("admit", 8 * GIB))
            if isinstance(result.reservation, gate.Reserved):
                result.reservation.path.unlink()

    def test_swap_and_some_need_both_strict_thresholds(self) -> None:
        for index, (free, some, expected) in enumerate(((14, 26, "hold"), (15, 26, "admit"),
                                     (14, 25, "admit"), (90, 37, "admit"), (1, 0, "admit"))):
            with self.subTest(swap_free=free, some=some):
                self.readings_for(swap_free=free, some=some)
                result = self.admit_at(seconds=120 * index)
                self.assertEqual(result.decision.state, expected)
                if expected == "hold":
                    self.assertIsInstance(result.head_reason, gate.HeadAdmissionStatus)
                    if isinstance(result.head_reason, gate.HeadAdmissionStatus):
                        self.assertIn("swap", gate.head_wait_text(result.head_reason, ZoneInfo("UTC")))
                if isinstance(result.reservation, gate.Reserved):
                    result.reservation.path.unlink()

    def test_pressure_recovery_spaces_waiters_and_read_only_views_agree(self) -> None:
        self.readings_for(full=6)
        self.assertEqual(self.admit_at(pid=100, need=1).decision.state, "hold")
        self.assertEqual(self.admit_at(pid=200, need=1).decision.state, "hold")
        self.readings_for()
        self.assertEqual(self.admit_at(pid=100, seconds=1, need=1).decision.state, "admit")
        second = self.admit_at(pid=200, seconds=30, need=1)
        self.assertEqual(second.decision.state, "hold")
        self.assertIsInstance(second.blocker, gate.NoSingleBlocker)
        directory = self.root / "admission"
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
        view, _ = gate.read_queue(directory, gate.process_table(self.root / "proc"), NOW.timestamp() + 30)
        self.assertEqual(view.waiting[0].reason, second.head_reason)
        with patch("memory_admit.time.time", return_value=NOW.timestamp() + 30):
            self.assertIsInstance(gate.current_blocker(directory / "200-second.waiting"), gate.NoSingleBlocker)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()})
        self.assertEqual(self.admit_at(pid=200, seconds=31, need=1).decision.state, "admit")

    def test_recovery_ends_120_seconds_after_clear_and_new_hold_restarts_it(self) -> None:
        self.readings_for(full=6)
        self.assertEqual(self.admit_at(need=1).decision.state, "hold")
        self.readings_for()
        for second, expected in ((1, "admit"), (2, "hold"), (111, "admit"),
                                 (120, "hold"), (121, "admit"), (122, "admit")):
            result = self.admit_at(seconds=second, need=1)
            self.assertEqual(result.decision.state, expected, f"second {second}")
            if isinstance(result.reservation, gate.Reserved):
                result.reservation.path.unlink()
        self.readings_for(full=6)
        self.assertEqual(self.admit_at(seconds=123, need=1).decision.state, "hold")
        self.readings_for()
        first = self.admit_at(seconds=124, need=1)
        self.assertEqual(first.decision.state, "admit")
        if isinstance(first.reservation, gate.Reserved):
            first.reservation.path.unlink()
        self.assertEqual(self.admit_at(seconds=125, need=1).decision.state, "hold")

    def test_sccache_is_charged_but_does_not_prevent_running_alone(self) -> None:
        service = self.builds / "sccache.service"
        service.mkdir(parents=True)
        _ = (service / "memory.stat").write_text("anon 1048576\n")
        result = self.check(need=22, free=60)
        self.assertEqual((result.decision.state, result.decision.charged), ("admit", 1048576))
        if isinstance(result.reservation, gate.Reserved):
            result.reservation.path.unlink()
        head = self.head(22)
        row = cast(gate.Waiter, json.loads(head.read_text()))
        row["holds_younger"] = True  # Priority remains after the builds ahead of it drain.
        _ = head.write_text(json.dumps(row))
        result = self.check(pid=300, need=22, free=20)
        self.assertEqual(result.decision.state, "admit")

    def test_caught_live_cgroup_refusal_still_records_the_read(self) -> None:
        record = self.root / "caught-reads.jsonl"
        live = Path("/sys/fs/cgroup/test-builds/sccache.service/memory.stat")
        with patch.dict(os.environ, {"BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(record),
                                    "BUILDLOG_BUILDS_CGROUP": str(live.parent.parent)}):
            with self.assertRaises(gate.LiveMemoryReading), patch.object(Path, "read_text", side_effect=AssertionError("live read")):
                _ = gate.sccache_memory()
        records = [cast(dict[str, object], json.loads(line)) for line in record.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reading"], str(live))
        self.assertEqual(records[0]["pid"], os.getpid())
        self.assertEqual(records[0]["argv"], sys.argv)

    def test_freeze_study_recorded_peaks_hold_even_at_lowest_pressure(self) -> None:
        """Per-admission snapshots were not saved; these are nearby records.

        The freeze tail excludes unfinished steps. The read-only step log gives
        these three active intervals at 18:47:12Z; anon_peaks.jsonl gives their
        final peaks, recorded after the freeze (not their .max at 18:47).
        Full has only an upper bound (<10), so use zero. Swap=80% and some=29
        are the recorded low bounds. No pressure rule can cause these holds.
        """
        # start, end, final anon peak: each is a distinct reservation.
        recorded = (
            (100, "18:35:00.535", "19:05:45.268", 8433098752),  # hana_p34d_alone nextest
            (200, "18:35:12.084", "18:55:32.895", 2768523264),  # hana frame-cost check
            (300, "18:42:53.156", "19:20:47.547", 11967815680), # p34 merge nextest
        )
        for pid, start, end, peak in recorded:
            self.assertLess(start, "18:47:12")
            self.assertGreater(end, "18:47:54")
            reservation = self.running(pid, 1)
            _ = reservation.with_suffix(".max").write_text(str(peak))
            os.utime(reservation.with_suffix(".max"), (NOW.timestamp(), NOW.timestamp()))
            # No per-step current snapshot exists; use zero to test peak alone.
            self.readings[pid] = gate.MeasuredMemory(0)
        service = self.builds / "sccache.service"
        service.mkdir(parents=True)
        _ = (service / "memory.stat").write_text(f"anon {int(5.95 * GIB)}\n")
        self.readings_for(swap_free=20, some=29, full=0)
        charged_peaks = sum(peak for _, _, _, peak in recorded)
        # Four requests total 10.3 GiB across 42 seconds. Exact admission
        # timestamps were not saved; the charge already exceeds the budget.
        with patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "21"}):
            for second, need in ((0, 3.14), (14, 3.14), (28, 3.02), (42, 1.0)):
                for pid, started, peak in ((500, 7.023, 3011469312), (600, 38.947, 2664980480)):
                    if second >= started and pid not in self.processes:
                        self.processes[pid] = str(pid)
                        self.readings[pid] = gate.MeasuredMemory(0)
                        reservation = self.running(pid, 3.14, age=-started)
                        _ = reservation.with_suffix(".max").write_text(str(peak))
                        published_at = NOW.timestamp() + second
                        os.utime(reservation.with_suffix(".max"), (published_at, published_at))
                        charged_peaks += max(peak, int(3.14 * GIB))
                result = self.admit_at(seconds=second, need=need)
                self.assertEqual(result.decision.state, "hold", f"recorded peaks at +{second}s")
                self.assertIsInstance(result.decision.pressure, gate.NoPressureHold)
                self.assertEqual(result.decision.charged,
                                 charged_peaks + int(5.95 * GIB))
                self.assertGreater(result.decision.charged, result.decision.budget)



class PressureAdmissionTests(MemoryQueueFixture):
    pressure: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.pressure = self.root / "pressure"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_MEMORY_PRESSURE": str(self.pressure),
            "BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_PROC": str(self.root / "proc"),
        })))

    def assert_admission(self, held: bool) -> None:
        result = self.check(need=8, free=40)
        self.assertEqual(result.decision.state, "hold" if held else "admit")
        self.assertEqual(isinstance(result.reservation, gate.Reserved), not held)
        if isinstance(result.reservation, gate.Reserved):
            result.reservation.path.unlink()
        else:
            view, _ = gate.read_queue(self.root / "admission", gate.process_table(self.root / "proc"), NOW.timestamp())
            self.assertEqual(view.waiting[0].reason, result.head_reason)

    def test_full_memory_stall_holds_only_above_five(self) -> None:
        for full in (4, 5, 6):
            with self.subTest(full_avg10=full):
                _ = self.pressure.write_text(f"some avg10=71.0 avg60=1.0 total=1\nfull avg10={full} avg60=1.0 total=1\n")
                self.assert_admission(full > 5)

    def test_idle_swap_at_ninety_nine_percent_does_not_hold_low_pressure_admission(self) -> None:
        _ = self.pressure.write_text("full avg10=4.0 avg60=1.0 total=1\n")
        _ = self.meminfo.write_text(
            f"MemAvailable: {40 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n"
            + "SwapTotal: 100 kB\nSwapFree: 1 kB\n")
        profile = gate.StepProfile("test", "nextest", "test", gate.ExpectedPeak(8 * GIB, "measured", 5))
        result = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root, now=NOW, profile=profile)
        self.assertEqual(result.decision.state, "admit")
        self.assertIsInstance(result.reservation, gate.Reserved)
        self.assertIsInstance(result.decision.pressure, gate.NoPressureHold)

    def test_unreadable_pressure_does_not_hold_but_memory_still_does(self) -> None:
        self.pressure.mkdir()
        self.assert_admission(False)
        result = self.check(need=8, free=1)
        self.assertEqual(result.decision.state, "hold")
        self.assertIsInstance(result.decision.pressure, gate.NoPressureHold)

    def test_pressure_is_in_checker_summary_even_with_no_running_step(self) -> None:
        _ = self.pressure.write_text("full avg10=23.4 avg60=1.0 total=1\n")
        _ = self.meminfo.write_text(f"MemAvailable: {40 * GIB // 1024} kB\n")
        arguments = ["memory_admit.py", "check", str(self.meminfo), "400", "", "0", "repo", "check",
                     "tree", str(8 * GIB), "fallback", "0", "cargo", "check"]
        result = self.check(need=8, free=40)
        output = io.StringIO()
        with patch.object(gate, "check", return_value=result), patch.object(sys, "argv", arguments), \
             patch.dict(os.environ, {"BUILDLOG_MEM_DETAIL": "1"}), redirect_stdout(output):
            gate.main()
        fields = output.getvalue().strip().split("|")
        self.assertEqual(fields[0], "hold")
        self.assertEqual(fields[8], "head: memory pressure: full avg10 23.4; younger steps may start until 18:10")
        self.assertNotIn("swap", fields[8])
        self.assertEqual(fields[6], "0")


class LiveMemoryTripwireTests(MemoryQueueFixture):
    record: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.record = self.root / "expected-live-memory-reads.jsonl"
        (self.root / "proc").mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(self.record),
            "BUILDLOG_MEMINFO": str(self.meminfo),
        })))

    def refusal(self, reading: str) -> str:
        return (
            f"live memory reading {reading} in a test; "
            + "fake BUILDLOG_MEMINFO and BUILDLOG_MEMORY_PRESSURE (scripts/lint/memory_test_support.py)"
        )

    def assert_record(self, reading: str, *, in_process: bool = True) -> None:
        lines = self.record.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        row = cast(dict[str, object], json.loads(lines[0]))
        self.assertEqual(row["reading"], reading)
        self.assertIsInstance(row["argv"], list)
        arguments = cast(list[object], row["argv"])
        self.assertTrue(arguments)
        self.assertTrue(all(isinstance(argument, str) for argument in arguments))
        self.assertIsInstance(row["pid"], int)
        self.assertGreater(cast(int, row["pid"]), 0)
        if in_process:
            self.assertEqual(row["argv"], sys.argv)
            self.assertEqual(row["pid"], os.getpid())

    def test_default_pressure_refuses_before_reading_and_records_the_process(self) -> None:
        del os.environ["BUILDLOG_MEMORY_PRESSURE"]
        with patch.object(Path, "read_text", side_effect=AssertionError("must refuse before reading memory")):
            with self.assertRaises(gate.LiveMemoryReading) as raised:
                _ = gate.read_pressure()
        self.assertIsInstance(raised.exception, RuntimeError)
        self.assertEqual(str(raised.exception), self.refusal("/proc/pressure/memory"))
        self.assert_record("/proc/pressure/memory")

    def test_meminfo_and_its_symlink_refuse_before_reading_and_record_the_live_path(self) -> None:
        link = self.root / "live-meminfo"
        link.symlink_to("/proc/meminfo")
        for path in (Path("/proc/meminfo"), link):
            with self.subTest(path=path):
                self.record.unlink(missing_ok=True)
                with patch.object(Path, "read_text", side_effect=AssertionError("must refuse before reading memory")):
                    with self.assertRaises(gate.LiveMemoryReading) as raised:
                        _ = gate.read_meminfo(path)
                self.assertEqual(str(raised.exception), self.refusal("/proc/meminfo"))
                self.assert_record("/proc/meminfo")

    def test_live_readings_append_records_without_replacing_earlier_entries(self) -> None:
        del os.environ["BUILDLOG_MEMORY_PRESSURE"]
        with self.assertRaises(gate.LiveMemoryReading):
            _ = gate.read_meminfo(Path("/proc/meminfo"))
        with self.assertRaises(gate.LiveMemoryReading):
            _ = gate.read_pressure()
        rows = [cast(dict[str, object], json.loads(line)) for line in self.record.read_text().splitlines()]
        self.assertEqual([row["reading"] for row in rows], ["/proc/meminfo", "/proc/pressure/memory"])

    def test_fake_readings_admit_check_and_queue_without_a_record(self) -> None:
        result = self.check(need=8, free=40)
        self.assertEqual(result.decision.state, "admit")
        self.assertIsInstance(result.reservation, gate.Reserved)
        if isinstance(result.reservation, gate.Reserved):
            result.reservation.path.unlink()
        _ = self.head(need=8)
        view, _ = gate.read_queue(gate.admission_dir(self.root), gate.process_table(gate.PROC), NOW.timestamp())
        self.assertEqual(len(view.waiting), 1)
        reason = view.waiting[0].reason
        self.assertIsInstance(reason, gate.HeadAdmissionStatus)
        if isinstance(reason, gate.HeadAdmissionStatus):
            self.assertEqual(reason.decision.state, "admit")
        self.assertFalse(self.record.exists() and self.record.read_text())

    def test_queue_propagates_live_memory_refusals(self) -> None:
        _ = self.meminfo.write_text("MemTotal: 67108864 kB\nMemAvailable: 67108864 kB\n")
        for variable, reading in (("BUILDLOG_MEMINFO", "/proc/meminfo"),
                                  ("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory")):
            with self.subTest(variable=variable), patch.dict(os.environ):
                del os.environ[variable]
                self.record.unlink(missing_ok=True)
                with self.assertRaises(gate.LiveMemoryReading) as raised:
                    _ = gate.read_queue(gate.admission_dir(self.root), {}, NOW.timestamp())
                self.assertEqual(str(raised.exception), self.refusal(reading))
                self.assert_record(reading)

    def test_unwritable_record_keeps_the_named_refusal_in_direct_and_queue_calls(self) -> None:
        _ = self.meminfo.write_text("MemTotal: 67108864 kB\nMemAvailable: 67108864 kB\n")
        record = self.root / "missing-directory" / "tripwire.jsonl"
        for variable, reading in (("BUILDLOG_MEMINFO", "/proc/meminfo"),
                                  ("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory")):
            with patch.dict(os.environ, {"BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(record), variable: reading}):
                with self.subTest(reading=reading, caller="direct"):
                    with self.assertRaises(gate.LiveMemoryReading) as raised:
                        gate.check_memory_reading(Path(reading))
                    self.assertEqual(str(raised.exception), self.refusal(reading))
                with self.subTest(reading=reading, caller="queue"):
                    with self.assertRaises(gate.LiveMemoryReading) as raised:
                        _ = gate.read_queue(gate.admission_dir(self.root), {}, NOW.timestamp())
                    self.assertEqual(str(raised.exception), self.refusal(reading))

    def test_default_pressure_still_returns_pressure_when_tripwire_is_unset_or_empty(self) -> None:
        del os.environ["BUILDLOG_MEMORY_PRESSURE"]
        for setting in (None, ""):
            with self.subTest(tripwire=setting), patch.dict(os.environ):
                if setting is None:
                    del os.environ["BUILDLOG_LIVE_MEMORY_TRIPWIRE"]
                else:
                    os.environ["BUILDLOG_LIVE_MEMORY_TRIPWIRE"] = setting
                # Keep the default-path contract without reading host memory.
                with patch.object(Path, "read_text", return_value="full avg10=0 total=0\n"):
                    self.assertIsInstance(gate.read_pressure(), gate.Pressure)

    def test_checker_cli_exits_two_with_refusal_as_last_stderr_line(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MEMORY_GATE.with_name("memory_admit.py")), "check", "/proc/meminfo",
             str(os.getpid()), "", "0", "test", "check", "test", str(GIB), "fallback", "0", "/bin/true"],
            env=dict(os.environ), capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.splitlines()[-1], self.refusal("/proc/meminfo"))
        self.assert_record("/proc/meminfo", in_process=False)

    def test_shell_refuses_live_meminfo_or_pressure_and_records_one_reading(self) -> None:
        _ = self.meminfo.write_text("MemTotal: 67108864 kB\nMemAvailable: 67108864 kB\n")
        for variable, reading in (("BUILDLOG_MEMINFO", "/proc/meminfo"),
                                  ("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory")):
            with self.subTest(variable=variable):
                self.record.unlink(missing_ok=True)
                environment = {
                    **os.environ, "HOME": str(self.root),
                    "BUILD_HOLD_DIR": str(self.root / "holders"),
                    "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
                    "UNIT_ACTIVE_DIR": str(self.root / "active"), "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
                    "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                    "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                    "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
                }
                del environment[variable]
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; buildlog_wait_for_memory', "test", str(MEMORY_GATE)],
                    env=environment, capture_output=True, text=True, check=False, timeout=10,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip(), f"memory admission check failed ({self.refusal(reading)}); step refused")
                self.assertNotIn("waiting for memory", result.stderr)
                self.assert_record(reading, in_process=False)


class QueueApiTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_PROC": str(self.root / "proc"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        self.processes[500] = "third"
        processes = {pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()}
        _ = self.enterContext(patch.object(gate, "process_table", return_value=processes))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        def reading(pid: int, _start: str, _table: dict[int, gate.ProcessIdentity], _proc: Path) -> gate.MeasuredMemory | gate.GoneMemory:
            return self.readings[pid]
        _ = self.enterContext(patch.object(gate, "tree_memory", side_effect=reading))
        _ = self.meminfo.write_text(f"MemAvailable: {int(23.1 * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def waiter(self, pid: int, since: float, folder: str, name: str) -> Path:
        row: gate.Waiter = {"pid": pid, "start_time": self.processes[pid], "since": since, "need": int(10.9 * GIB),
                            "session_id": f"session-{pid}", "session_name": name, "step": "nextest", "folder": folder}
        path = gate.admission_dir(self.root) / f"{pid}-{self.processes[pid]}.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def queue(self) -> tuple[Path, Path, Path]:
        return (self.waiter(300, NOW.timestamp() - 2400, "/tmp/clone88ci", "clone88ci"),
                self.waiter(400, NOW.timestamp() - 300, "/tmp/warmup-tree", "warmup"),
                self.waiter(500, NOW.timestamp() - 60, "/tmp/third", "third"))

    def test_waiter_identity_keeps_the_bare_step_and_caller_identity(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "worker-id", "CLAUDE_CODE_SESSION_ID": "parent-id",
                                     "CLAUDE_SESSION_NAME": "recorded-name"}), \
             patch("os.getcwd", return_value="/tmp/hana-tree"):
            for step in ("nextest", "clippy", "mend", "doc"):
                with self.subTest(step=step):
                    self.assertEqual(gate.waiter_identity(step, "hana-tree"), {
                        "session_id": "worker-id", "session_name": "recorded-name",
                        "step": step, "folder": "/tmp/hana-tree",
                    })

    def test_held_step_is_stored_and_read_without_the_repository_prefix(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: 0 kB\nMemTotal: {60 * GIB // 1024} kB\n")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "worker-id", "CLAUDE_SESSION_NAME": "recorded-name"}):
            result = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root, now=NOW,
                                profile=gate.StepProfile("hana", "nextest", "hana-tree",
                                                         gate.ExpectedPeak(12 * GIB, "measured", 5), HANA))
        self.assertEqual(result.decision.state, "hold")
        row = cast(gate.Waiter, json.loads((self.root / "admission/400-warmup.waiting").read_text()))
        self.assertEqual(row.get("step"), "nextest")
        self.assertEqual(row.get("repository_name"), "hana")
        view = gate.queue_view()
        self.assertEqual([(step.session_id, step.step) for step in view.waiting], [("worker-id", "nextest")])

    def test_queue_memory_hold_carries_threshold_and_its_parts(self) -> None:
        _ = self.running(100, 5, age=0)
        _ = self.waiter(300, NOW.timestamp() - 60, "/tmp/head", "head")
        path = self.waiter(400, NOW.timestamp() - 30, "/tmp/follower", "follower")
        row = cast(gate.Waiter, json.loads(path.read_text()))
        row["need"] = 3 * GIB
        _ = path.write_text(json.dumps(row))
        _ = self.meminfo.write_text(f"MemAvailable: {8 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        view = gate.queue_view()
        reason = view.waiting[1].reason
        self.assertIsInstance(reason, gate.NeedsMemory)
        if not isinstance(reason, gate.NeedsMemory):
            self.fail(f"expected memory hold, got {reason}")
        self.assertEqual((reason.need, reason.free), (3 * GIB, 8 * GIB))
        self.assertEqual((reason.threshold, reason.growth, reason.reserve, reason.head_need),
                         (10 * GIB, 4 * GIB, 3 * GIB, 0))
        self.assertEqual(reason.threshold, reason.need + reason.growth + reason.reserve + reason.head_need)

        # Exercise a nonzero head charge at the decision boundary; strict heads normally use BehindHead.
        decision = gate.decide(8 * GIB, 60 * GIB, 3 * GIB, [gate.PromisedMemory(4 * GIB)], head_need=7 * GIB)
        with patch.object(gate, "queue_decision", return_value=decision):
            charged = gate.queue_view().waiting[1].reason
        self.assertIsInstance(charged, gate.NeedsMemory)
        if not isinstance(charged, gate.NeedsMemory):
            self.fail(f"expected memory hold, got {charged}")
        self.assertEqual((charged.threshold, charged.growth, charged.reserve, charged.head_need),
                         (17 * GIB, 4 * GIB, 3 * GIB, 7 * GIB))
        self.assertEqual(charged.threshold, charged.need + charged.growth + charged.reserve + charged.head_need)

    def test_behind_head_carries_head_identity_and_unmatched_name(self) -> None:
        for name, expected_name in (("clone88ci", "clone88ci"), ("", "clone88ci")):
            with self.subTest(recorded_name=name):
                _ = self.waiter(300, NOW.timestamp() - 2400, "/tmp/clone88ci", name)
                _ = self.waiter(400, NOW.timestamp() - 300, "/tmp/warmup-tree", "warmup")
                view = gate.queue_view()
                self.assertEqual(view.waiting[1].reason, gate.BehindHead(
                    head_name=expected_name, head_since=NOW.timestamp() - 2400, head_session_id="session-300"))

    def test_view_counts_measured_use_after_ramp_and_keeps_the_head(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        _ = self.queue()
        with patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
             patch("time.sleep", side_effect=AssertionError("no wait")), \
             patch.object(gate, "process_table", wraps=gate.process_table) as read:
            view = gate.queue_view()
        self.assertEqual(read.call_count, 1)
        self.assertEqual((view.promised, sum(step.promise for step in view.running)), (0, int(23.5 * GIB)))
        self.assertCountEqual([step.measured for step in view.running],
                              [gate.MeasuredMemory(GIB), gate.MeasuredMemory(2 * GIB)])
        self.assertEqual(view.waiting[0].session_name, "clone88ci")
        self.assertIsInstance(view.waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, view.waiting[0].reason).decision.state, "admit")
        self.assertTrue(all(isinstance(row.reason, gate.BehindHead) for row in view.waiting[1:]))

    def test_queue_view_names_budget_before_free_memory_when_both_hold_the_head(self) -> None:
        _ = self.running(100, 12, age=0)
        _ = self.running(200, 11.5, age=0)
        _ = self.queue()
        view = gate.queue_view()
        self.assertEqual(view.promised, int(20.5 * GIB))
        reason = view.waiting[0].reason
        self.assertIsInstance(reason, gate.HeadAdmissionStatus)
        if isinstance(reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(reason, ZoneInfo("America/New_York")),
                             "head: needs 10.9 GiB; builds use 23.5 of the 20.0 GiB budget "
                             + "(starts when build use falls to 9.1 GiB); needs 34.4 GiB free; 23.1 free; younger steps wait until 18:10")
        self.assertCountEqual([row.measured for row in view.running],
                              [gate.MeasuredMemory(GIB, 12 * GIB), gate.MeasuredMemory(2 * GIB, int(11.5 * GIB))])

    def test_backfill_at_nine_minutes_becomes_sticky_at_ten_minutes(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.waiter(300, NOW.timestamp() - 540, "/tmp/head", "head")
        head = self.check(pid=300, need=19.3, free=20)
        self.assertEqual(head.decision.state, "hold")
        self.assertEqual(gate.queue_view().waiting[0].reason, head.head_reason)
        self.assertIsInstance(head.head_reason, gate.HeadAdmissionStatus)
        if isinstance(head.head_reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(head.head_reason, ZoneInfo("America/New_York")),
                             "head: needs 19.3 GiB; builds use 3.0 of the 20.0 GiB budget "
                             + "(starts when build use falls to 0.7 GiB); needs 22.3 GiB free; 20.0 free; "
                             + "younger steps may start until 18:01")
        first = self.check(need=1, free=20)
        self.assertIsInstance(first.reservation, gate.Reserved)
        if isinstance(first.reservation, gate.Reserved):
            first.reservation.path.unlink()
        second = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root,
                            now=NOW + timedelta(seconds=60), profile=first.profile)
        self.assertEqual(second.decision.state, "hold")
        self.assertTrue(cast(gate.Waiter, json.loads(path.read_text())).get("holds_younger", False))
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")

    def test_strict_backfill_strict_windows_keep_queue_and_admission_in_agreement(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        self.readings[400] = gate.MeasuredMemory(0)
        _ = self.meminfo.write_text(f"MemAvailable: {20 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        for elapsed, state, deadline, window in ((0, "hold", "18:10", "wait"),
                                                  (600, "admit", "18:12", "may start"),
                                                  (720, "hold", "18:22", "wait")):
            clock = NOW + timedelta(seconds=elapsed)
            if elapsed == 600:
                with patch.object(time, "time", return_value=clock.timestamp()):
                    before = gate.queue_view()
                self.assertIsInstance(before.waiting[1].reason, gate.ReadyForAdmission)
            profile = gate.StepProfile("test", "nextest", "test", gate.ExpectedPeak(GIB, "measured", 5))
            result = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root,
                                now=clock, profile=profile)
            self.assertEqual(result.decision.state, state)
            with patch.object(time, "time", return_value=clock.timestamp()):
                view = gate.queue_view()
            text = gate.head_wait_text(cast(gate.HeadAdmissionStatus, view.waiting[0].reason),
                                       ZoneInfo("America/New_York"))
            self.assertTrue(text.endswith(f"younger steps {window} until {deadline}"), text)
            if state == "hold":
                self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)
            else:
                # While this child runs, the head still names its backfill window.
                self.assertIsInstance(result.reservation, gate.Reserved)
                if isinstance(result.reservation, gate.Reserved):
                    result.reservation.path.unlink()
            row = cast(gate.Waiter, json.loads(path.read_text()))
            self.assertEqual(row.get("priority_since"), NOW.timestamp())

    def test_bump_restarts_strict_window_during_backfill(self) -> None:
        path = self.waiter(300, NOW.timestamp() - 2400, "/tmp/head", "head")
        row = cast(gate.Waiter, json.loads(path.read_text()))
        row["holds_younger"], row["priority_since"] = True, NOW.timestamp() - 600
        _ = path.write_text(json.dumps(row))
        self.assertIsInstance(gate.head_window(row, NOW.timestamp()), gate.BackfillUntil)
        _ = gate.bump_waiter("head", "actor")
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")
        row = cast(gate.Waiter, json.loads(path.read_text()))
        self.assertEqual(row.get("priority_since"), NOW.timestamp())
        self.assertIsInstance(gate.head_window(row, NOW.timestamp()), gate.StrictUntil)

    def test_psi_11_holds_backfill_without_making_the_young_head_strict(self) -> None:
        _ = self.waiter(300, NOW.timestamp() - 540, "/tmp/head", "head")
        pressure = self.root / "pressure"
        _ = pressure.write_text("full avg10=11.0 avg60=1.0 total=1\n")
        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": str(pressure)}):
            result = self.check(need=1, free=40)
            self.assertEqual(result.decision.state, "hold")
            self.assertEqual(result.decision.pressure, gate.MemoryPressure(11))
            self.assertIsInstance(gate.queue_view().waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(result.head_need, 0)

    def test_head_hold_survives_rising_free_memory_and_falling_measured_use(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        first = self.check(need=1, free=20)
        self.assertEqual(first.decision.state, "hold")
        self.assertTrue(cast(gate.Waiter, json.loads(path.read_text())).get("holds_younger", False))
        self.readings[100] = gate.MeasuredMemory(0)
        second = self.check(need=1, free=21.9)
        self.assertEqual(second.decision.state, "hold")
        view = gate.queue_view()
        reason = view.waiting[0].reason
        self.assertIsInstance(reason, gate.HeadAdmissionStatus)
        if isinstance(reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(reason, ZoneInfo("America/New_York")), "head: needs 22.3 GiB free; 21.9 free; younger steps wait until 18:10")
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)

    def test_mature_builds_hold_head_on_13_point_2_used_not_72_point_3_promised(self) -> None:
        for pid in range(500, 510):
            self.processes[pid] = str(pid)
            self.readings[pid] = gate.MeasuredMemory(int(1.32 * GIB))
            _ = self.running(pid, 7.23)
        _ = self.enterContext(patch.object(gate, "process_table", return_value={
            pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()
        }))
        _ = self.head(15.1)
        result = self.check(pid=300, need=15.1, free=19.5)
        self.assertEqual(result.decision.state, "hold")
        self.assertAlmostEqual(sum(row["need"] for row in result.live) / GIB, 72.3)
        self.assertAlmostEqual(result.decision.charged / GIB, 13.2)
        self.assertGreater(result.available, result.decision.threshold)
        view = gate.queue_view()
        self.assertEqual(view.waiting[0].reason, result.head_reason)
        self.assertIsInstance(result.head_reason, gate.HeadAdmissionStatus)
        if isinstance(result.head_reason, gate.HeadAdmissionStatus):
            text = gate.head_wait_text(result.head_reason, ZoneInfo("America/New_York"))
            self.assertEqual(text, "head: needs 15.1 GiB; builds use 13.2 of the 20.0 GiB budget "
                             + "(starts when build use falls to 4.9 GiB); younger steps wait until 18:10")
            arguments = ["memory_admit.py", "check", str(self.meminfo), "300", "", "0",
                         "test", "nextest", "test", str(int(15.1 * GIB)), "measured", "5", "cargo", "nextest"]
            for detail in ("", "1"):
                output = io.StringIO()
                with patch.object(gate, "check", return_value=result), patch.object(sys, "argv", arguments), \
                     patch.dict(os.environ, {"BUILDLOG_MEM_DETAIL": detail}), redirect_stdout(output):
                    gate.main()
                self.assertIn(text, output.getvalue().split("|")[8])

    def test_head_names_only_free_memory_when_budget_fits(self) -> None:
        _ = self.running(100, 1)
        _ = self.head(15.1)
        result = self.check(pid=300, need=15.1, free=18)
        self.assertEqual(result.decision.state, "hold")
        self.assertLess(result.decision.charged + result.peak.bytes, result.decision.budget)
        self.assertEqual(gate.queue_view().waiting[0].reason, result.head_reason)
        self.assertIsInstance(result.head_reason, gate.HeadAdmissionStatus)
        if isinstance(result.head_reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(result.head_reason, ZoneInfo("America/New_York")), "head: needs 18.1 GiB free; 18.0 free; younger steps wait until 18:10")

    def test_drained_head_starts_below_memory_floor_and_a_younger_check_cannot_take_its_place(self) -> None:
        running = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        self.assertEqual(self.check(need=10.8, free=20).decision.state, "hold")
        running.unlink()
        younger = self.check(need=10.8, free=21.9)
        self.assertEqual(younger.decision.state, "hold")
        view = gate.queue_view()
        self.assertIsInstance(view.waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, view.waiting[0].reason).decision.state, "admit")
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)
        head = self.check(pid=300, need=19.3, free=21.9)
        self.assertLess(head.available, head.decision.threshold)
        self.assertIsInstance(head.reservation, gate.Reserved)
        self.assertFalse(path.exists())

    def test_head_start_removes_priority_so_younger_steps_use_normal_limits(self) -> None:
        path = self.head(2)
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")
        self.readings[300] = gate.MeasuredMemory(0)
        head = self.check(pid=300, need=2, free=40)
        self.assertIsInstance(head.reservation, gate.Reserved)
        self.assertFalse(path.exists())
        younger = self.check(need=1, free=40)
        self.assertEqual((younger.decision.state, younger.head_need), ("admit", 0))

    def test_removed_head_row_releases_younger_steps_to_normal_limits(self) -> None:
        path = self.head(19.3)
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")
        path.unlink()
        younger = self.check(need=1, free=21.9)
        self.assertEqual((younger.decision.state, younger.head_need), ("admit", 0))

    def test_psi_11_holds_a_drained_head_without_releasing_younger_steps(self) -> None:
        _ = self.head(19.3)
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")
        pressure = self.root / "pressure"
        _ = pressure.write_text("full avg10=11.0 avg60=1.0 total=1\n")
        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": str(pressure)}):
            head = self.check(pid=300, need=19.3, free=21.9)
            self.assertEqual(head.decision.state, "hold")
            view = gate.queue_view()
        self.assertEqual(view.waiting[0].reason, head.head_reason)
        self.assertIsInstance(head.head_reason, gate.HeadAdmissionStatus)
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)

    def test_bump_by_position_session_id_session_name_and_folder_starts_the_selected_waiter_first(self) -> None:
        for selector in ("2", "session-400", "warmup", "/tmp/warmup-tree", "warmup-tree"):
            with self.subTest(selector=selector):
                for path in gate.admission_dir(self.root).glob("*.reservation"):
                    path.unlink()
                _ = self.queue()
                moved = gate.bump_waiter(selector, "actor")
                self.assertEqual(moved.pid, 400)
                self.assertEqual(gate.queue_view().waiting[0].pid, 400)
                older = self.check(pid=300, free=40, since=NOW.timestamp() - 2400)
                self.assertEqual(older.decision.state, "hold")
                selected = self.check(pid=400, free=40, since=NOW.timestamp() - 300)
                self.assertEqual(selected.decision.state, "admit")
                self.assertFalse((self.root / "admission/400-warmup.waiting").exists())

    def test_moved_waiter_keeps_its_wait_time_and_head_position_after_another_check(self) -> None:
        paths = self.queue()
        original_waiters = gate.queue_view().waiting
        original = original_waiters[-1]
        _ = gate.bump_waiter("warmup", "actor")
        moved = gate.bump_waiter("third", "actor")
        self.assertEqual((moved.pid, moved.since), (original.pid, original.since))
        waiting = gate.queue_view().waiting
        self.assertEqual([row.pid for row in waiting], [500, 400, 300])
        self.assertEqual({row.pid: row.since for row in waiting}, {row.pid: row.since for row in original_waiters})
        head = waiting[0]
        self.assertEqual((head.pid, head.since), (original.pid, original.since))
        self.assertIsInstance(head.reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, head.reason).decision.state, "admit")
        with patch.object(gate, "read_pressure", return_value=gate.MemoryPressure(11)):
            held = self.check(pid=original.pid, free=0, since=original.since)
        self.assertEqual(held.decision.state, "hold")
        row = cast(dict[str, object], json.loads(paths[-1].read_text()))
        self.assertEqual(row["since"], original.since)
        head = gate.queue_view().waiting[0]
        self.assertEqual((head.pid, head.since), (original.pid, original.since))
        previous_head = self.check(pid=400, free=40, since=original_waiters[1].since)
        self.assertEqual(previous_head.decision.state, "hold")
        older = self.check(pid=300, free=40, since=NOW.timestamp() - 2400)
        self.assertEqual(older.decision.state, "hold")
        selected = self.check(pid=original.pid, free=40, since=original.since)
        self.assertEqual(selected.decision.state, "admit")

    def test_unknown_and_ambiguous_selectors_name_matches_and_move_nothing(self) -> None:
        _ = self.waiter(300, NOW.timestamp() - 2400, "/one/shared", "first-session")
        _ = self.waiter(400, NOW.timestamp() - 300, "/two/shared", "second-session")
        before = {path: path.read_text() for path in (self.root / "admission").glob("*.waiting")}
        for selector, word in (("missing", "unknown"), ("99", "unknown"), ("shared", "ambiguous")):
            with self.assertRaisesRegex(ValueError, word) as result:
                _ = gate.bump_waiter(selector, "actor")
            if selector == "shared":
                self.assertIn("first-session", str(result.exception))
                self.assertIn("second-session", str(result.exception))
            self.assertEqual({path: path.read_text() for path in before}, before)
        self.assertEqual(list(self.root.glob("*/*.jsonl")), [])

    def test_bump_writes_one_row_in_the_private_hold_event_log(self) -> None:
        _ = self.queue()
        _ = gate.bump_waiter("2", "actor-id")
        rows = [cast(dict[str, object], json.loads(line)) for path in self.root.glob("*/*.jsonl")
                for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({key: row[key] for key in ("kind", "actor", "session", "step", "folder", "old_position", "at")},
                         {"kind": "queue_bump", "actor": "actor-id", "session": "session-400", "step": "nextest",
                          "folder": "/tmp/warmup-tree", "old_position": 2, "at": store.utc_iso(NOW.timestamp())})

    def test_queue_view_and_check_agree_on_heads_that_can_and_cannot_fit_after_drain(self) -> None:
        _ = self.running(100, 30)
        self.readings[100] = gate.MeasuredMemory(int(21.5 * GIB))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "30"})))
        for need, held in ((19.3, True), (38, False)):
            with self.subTest(head_need=need):
                path = self.head(need)
                row = cast(gate.Waiter, json.loads(path.read_text()))
                row["session_name"] = "clone88ci"
                _ = path.write_text(json.dumps(row))
                result = self.check(need=2, free=15)
                if not held:
                    assert isinstance(result.reservation, gate.Reserved)
                    result.reservation.path.unlink()
                    _ = self.waiter(400, NOW.timestamp(), "/tmp/warmup-tree", "warmup")
                    waiter_path = self.root / "admission/400-warmup.waiting"
                    follower = cast(gate.Waiter, json.loads(waiter_path.read_text()))
                    follower["need"] = 2 * GIB
                    _ = waiter_path.write_text(json.dumps(follower))
                view = gate.queue_view()
                reason = next(step.reason for step in view.waiting if step.pid == 400)
                if held:
                    self.assertEqual(result.decision.state, "hold")
                    self.assertEqual(reason, gate.BehindHead("clone88ci", NOW.timestamp() - 2400, ""))
                else:
                    self.assertEqual(result.decision.state, "admit")
                    self.assertIsInstance(reason, gate.ReadyForAdmission)

    def test_queue_view_waits_for_lock_and_keeps_real_reasons_without_pruning(self) -> None:
        paths = self.queue()
        del self.processes[300]
        _ = self.enterContext(patch.object(gate, "process_table", return_value={
            pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()}))
        lock_path = self.root / "admission/lock"
        released = threading.Event()
        previous = signal.getsignal(signal.SIGALRM)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            def release() -> None:
                released.set()
                fcntl.flock(lock, fcntl.LOCK_UN)
            timer = threading.Timer(0.05, release)
            timer.start()
            try:
                view = gate.queue_view()
                self.assertTrue(released.is_set())
            finally:
                timer.cancel()
                timer.join()
        self.assertEqual([row.pid for row in view.waiting], [400, 500])
        self.assertTrue(paths[0].exists())
        self.assertIsInstance(view.waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, view.waiting[0].reason).decision.state, "admit")
        self.assertIsInstance(view.waiting[1].reason, gate.ReadyForAdmission)
        self.assertEqual(view.available, int(23.1 * GIB) // 1024 * 1024)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_queue_view_times_out_on_held_lock_and_restores_alarm(self) -> None:
        _ = self.queue()
        previous = signal.getsignal(signal.SIGALRM)
        with (self.root / "admission/lock").open("a+") as lock, \
             patch.object(gate, "QUEUE_LOCK_TIMEOUT_SECONDS", 0.03):
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(TimeoutError, "admission lock busy for 0.03 s; try again"):
                _ = gate.queue_view()
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertEqual(len(gate.queue_view().waiting), 3)

    def test_missing_meminfo_is_a_reading_unavailable_reason(self) -> None:
        _ = self.queue()
        self.meminfo.unlink()
        self.assertTrue(all(isinstance(row.reason, gate.ReadingUnavailable) for row in gate.queue_view().waiting))



class ProcessMemoryTests(unittest.TestCase):
    def test_swap_release_does_not_make_a_reservation_the_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / "proc"
            for pid, start in ((100, "holder"), (400, "waiter")):
                process = proc / str(pid)
                process.mkdir(parents=True)
                fields = ["S", "1", *(["0"] * 17), start]
                _ = (process / "stat").write_text(f"{pid} (test process) " + " ".join(fields))
            _ = (proc / "100/status").write_text(
                f"VmRSS: {GIB // 1024} kB\nVmSwap: {15 * GIB // 1024} kB\n")
            _ = (proc / "100/environ").write_bytes(
                b"CLAUDE_CODE_SESSION_ID=owner-id\0CLAUDE_SESSION_NAME=owner-name\0")
            directory = gate.admission_dir(root)
            reservation: gate.Reservation = {
                "pid": 100, "start_time": "holder", "need": 16 * GIB,
                "repo": "test", "step": "check", "worktree": "test", "sidecar": "",
                "admitted_at": store.utc_iso(NOW.timestamp() - gate.RAMP_SECONDS),
                "test_threads": None, "call_id": None,
            }
            _ = (directory / "100.reservation").write_text(json.dumps(reservation))
            waiter = directory / "400-waiter.waiting"
            _ = waiter.write_text(json.dumps({
                "pid": 400, "start_time": "waiter", "need": 12 * GIB,
                "since": NOW.timestamp(),
            }))
            environment = fake_memory_readings(
                root, available_kib=2 * GIB // 1024, total_kib=60 * GIB // 1024)
            environment.update({"BUILDLOG_DIR": str(root), "BUILDLOG_PROC": str(proc)})
            with patch.dict(os.environ, environment), \
                    patch.object(time, "time", return_value=NOW.timestamp()):
                self.assertIsInstance(gate.current_blocker(waiter), gate.NoSingleBlocker)

    def test_tree_counts_resident_and_swap_and_excludes_other_processes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            for pid, parent, state, start, resident, swap in (
                (100, 1, "S", "start", 10, 2), (101, 100, "S", "child", 20, 3),
                (102, 101, "S", "leaf", 30, 4), (103, 1, "S", "other", 500, 400),
                (104, 100, "Z", "dead", 200, 100),
            ):
                path = proc / str(pid)
                path.mkdir()
                fields = [state, str(parent), *(["0"] * 17), start]
                _ = (path / "stat").write_text(f"{pid} (test process) " + " ".join(fields))
                _ = (path / "status").write_text(f"VmRSS: {resident} kB\nVmSwap: {swap} kB\n")
            processes = gate.process_table(proc)
            self.assertEqual(gate.tree_memory(100, "start", processes, proc),
                             gate.MeasuredMemory(69 * 1024, swap_bytes=9 * 1024))
            self.assertEqual(gate.tree_memory(100, "reused", processes, proc), gate.GoneMemory())
            self.assertEqual(gate.tree_memory(999, "gone", processes, proc), gate.GoneMemory())

    def test_check_reads_one_table_for_promised_measured_and_reused_reservations(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / "proc"
            for pid, parent, start, resident, swap in (
                (100, 1, "fresh", 900, 0), (200, 1, "running", 20, 3),
                (201, 200, "child", 30, 4), (300, 1, "reused", 800, 0),
            ):
                path = proc / str(pid)
                path.mkdir(parents=True)
                fields = ["S", str(parent), *(["0"] * 17), start]
                _ = (path / "stat").write_text(f"{pid} (test process) " + " ".join(fields))
                _ = (path / "status").write_text(f"VmRSS: {resident} kB\nVmSwap: {swap} kB\n")
            directory = gate.admission_dir(root)
            for pid, start, age in ((100, "fresh", 0), (200, "running", gate.RAMP_SECONDS), (300, "old", 0)):
                row: gate.Reservation = {
                    "pid": pid, "start_time": start, "need": 4 * GIB, "repo": "test",
                    "step": "check", "worktree": "test", "sidecar": "",
                    "admitted_at": store.utc_iso(NOW.timestamp() - age),
                    "test_threads": None, "call_id": None,
                }
                _ = (directory / f"{pid}.reservation").write_text(json.dumps(row))
            _ = (directory / "300.max").write_text("100")
            meminfo = root / "meminfo"
            _ = meminfo.write_text(f"MemAvailable: {8 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
            starts = {100: "fresh", 200: "running", 300: "old", 400: "caller"}
            with patch.dict(os.environ, {"BUILDLOG_DIR": str(root), "BUILDLOG_PROC": str(proc)}), \
                 patch.object(gate, "process_start_time", side_effect=starts.get), \
                 patch.object(gate, "process_table", wraps=gate.process_table) as tables:
                result = gate.check(meminfo, 400, "", ["cargo", "check"], root=root, now=NOW,
                                    profile=gate.StepProfile("test", "check", "test", gate.ExpectedPeak(2 * GIB, "fallback", 0)))
            tables.assert_called_once_with(proc)
            self.assertEqual((result.decision.state, result.decision.promised, result.decision.measured),
                             ("hold", 4 * GIB - 900 * 1024, 957 * 1024))
            self.assertCountEqual([row["pid"] for row in result.live], [100, 200])
            self.assertFalse((directory / "300.reservation").exists())
            self.assertFalse((directory / "300.max").exists())



class ShellTests(unittest.TestCase):
    def test_wait_line_carries_head_need_step_and_measured_memory(self) -> None:
        command = ('source "$1"; BUILDLOG_MEM_WAIT_S=120; buildlog_memory_python() { echo UTC; }; '
                   + 'buildlog_memory_line "$2" 0 49000000000 11600000000 measured 5 0 2 3000000000 '
                   + '"test steps" warmup nextest 3221225472 11703785881 "clone88ci nextest (verify pid 300)"')
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("first", "periodic"):
                result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), kind],
                                        env={**os.environ, "BUILDLOG_DIR": folder}, capture_output=True,
                                        text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn("head clone88ci nextest (verify pid 300) needs 10.9 GiB", result.stderr)
                self.assertTrue(result.stderr.endswith(
                    "0.0 GiB promised, 3.0 GiB measured across 2 running steps: test steps; "
                    + "2.8 GiB kept for earlyoom)\n"))

    def test_wait_line_shows_reason_in_place_of_zero_running_steps(self) -> None:
        command = ('source "$1"; BUILDLOG_MEM_WAIT_S=120; buildlog_memory_python() { echo UTC; }; '
                   + 'buildlog_memory_line "$2" 19327352832 19434727014 16213501542 measured 5 0 0 '
                   + '3221225472 "$3" test nextest 0')
        reasons = ("memory pressure: full avg10 11.0",
                   "head: needs 18.1 GiB free; 18.0 free; younger steps may start until 18:10")
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("first", "periodic"):
                for reason in reasons:
                    with self.subTest(kind=kind, reason=reason):
                        result = subprocess.run(
                            ["bash", "-c", command, "test", str(MEMORY_GATE), kind, reason],
                            env={**os.environ, "BUILDLOG_DIR": folder}, capture_output=True,
                            text=True, check=False, timeout=300)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(len(result.stderr.splitlines()), 1)
                        self.assertTrue(result.stderr.endswith(f"; {reason}; 3.0 GiB kept for earlyoom)\n"))
                        self.assertNotIn("0 running steps", result.stderr)
                        self.assertNotIn("GiB promised", result.stderr)
                        self.assertNotIn("GiB measured", result.stderr)

    def test_wait_line_keeps_zero_running_text_when_reason_is_empty(self) -> None:
        command = ('source "$1"; BUILDLOG_MEM_WAIT_S=120; buildlog_memory_python() { echo UTC; }; '
                   + 'buildlog_memory_line "$2" 0 0 0 fallback 0 0 0 0 "" test check 0')
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("first", "periodic"):
                with self.subTest(kind=kind):
                    result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), kind],
                                            env={**os.environ, "BUILDLOG_DIR": folder}, capture_output=True,
                                            text=True, check=False, timeout=300)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(result.stderr.splitlines()), 1)
                    self.assertTrue(result.stderr.endswith(
                        "; 0.0 GiB promised, 0.0 GiB measured across 0 running steps)\n"))

    def test_old_shell_keeps_its_field_count_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            directory = gate.admission_dir(root)
            _ = (directory / "400-start.waiting").write_text(json.dumps({
                "pid": 400, "start_time": "start", "need": 12 * GIB, "since": time.time(),
                "repo_identity": "", "test_scope": "full", "estimated_at": time.time(),
            }))
            arguments = ["memory_admit.py", "check", str(meminfo), "400", "", "0", "repo", "nextest",
                         "clone88ci", str(12 * GIB), "fallback", "0", "cargo", "nextest"]
            for detail, count in (("", 15), ("1", 18)):
                output = io.StringIO()
                with patch.object(sys, "argv", arguments), \
                     patch.dict(os.environ, {"BUILDLOG_DIR": folder, "BUILDLOG_MEM_DETAIL": detail}), \
                     patch.object(gate, "process_start_time", return_value="start"), \
                     redirect_stdout(output):
                    gate.main()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(len(fields), count)
                self.assertEqual(fields[14], "clone88ci")

    def test_shell_replaces_old_need_and_source_with_refreshed_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "proc").mkdir()
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            calls = root / "calls.jsonl"
            wrapper = root / "checker.py"
            _ = wrapper.write_text(
                "import json, sys\nfrom pathlib import Path\n"
                + f"sys.path.insert(0, {str(MEMORY_GATE.parent)!r})\nimport memory_admit as gate\n"
                + f"calls = Path({str(calls)!r})\n"
                + "previous = calls.read_text().splitlines() if calls.exists() else []\n"
                + "with calls.open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                + "if not previous:\n"
                + "    pid = int(sys.argv[3]); start = gate.process_start_time(pid)\n"
                + "    row = {'pid': pid, 'start_time': start, 'need': int(19.3 * gate.GIB), 'since': float(sys.argv[5])}\n"
                + "    (gate.admission_dir(gate.store.root()) / f'{pid}-{start}.waiting').write_text(json.dumps(row))\n"
                + "    print('|'.join(map(str, ['hold', 23 * gate.GIB, row['need'], 'measured', 7, 0, 0, 3 * gate.GIB, '', '', 'repo', 'nextest', 0, 'held', 'repo', 0, 0, ''])))\n"
                + "else:\n"
                + "    gate.git_identity = lambda argv, cwd: (gate.RepositoryIdentity('https://example.org/hana', 'hana'), 'repo')\n"
                + "    gate.expected_peak = lambda *args, **kwargs: gate.ExpectedPeak(2 * gate.GIB, 'pooled', 5)\n"
                + "    gate.main()\n")
            command = ('source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; sleeps=0; '
                       'sleep() { sleeps=$((sleeps + 1)); if (( sleeps == 2 )); then '
                       'printf "MemAvailable: 41943040 kB\\n" > "$BUILDLOG_MEMINFO"; fi; }; '
                       'buildlog_wait_for_memory cargo nextest')
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(wrapper)],
                                    capture_output=True, text=True, check=False, timeout=300,
                                    env={**os.environ, "HOME": folder, "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_PROC": str(root / "proc"),
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"), "UNIT_RUN_DIR": "",
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_HOLD_WAIT_FILE": "",
                                         "BUILDLOG_HOLD_WAITER_FILE": "", "LINT_QUEUE_LEAVE_HOOK": "",
                                         "BUILDLOG_MAC_IDLE_HOOK": ""})
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = [cast(list[str], json.loads(line)) for line in calls.read_text().splitlines()]
            self.assertEqual(len(arguments), 3)
            self.assertEqual((arguments[1][8], arguments[1][9]), (str(int(19.3 * GIB)), "measured"))
            self.assertEqual((arguments[2][8], arguments[2][9]), (str(2 * GIB), "pooled"))
            row = cast(gate.Reservation, json.loads(next((root / "log/admission").glob("*.reservation")).read_text()))
            self.assertEqual((row["need"], row.get("repo_identity")), (2 * GIB, "https://example.org/hana"))

    def test_old_waiter_reestimates_and_current_waiter_refreshes_after_ten_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            path = gate.admission_dir(root) / "400-start.waiting"
            _ = path.write_text(json.dumps({"pid": 400, "start_time": "start",
                                            "need": int(19.3 * GIB), "since": NOW.timestamp()}))
            arguments = ["memory_admit.py", "check", str(meminfo), "400", "", str(NOW.timestamp()),
                         "repo", "nextest", "repo", str(int(19.3 * GIB)), "measured", "7", "cargo", "nextest"]
            with patch.dict(os.environ, {"BUILDLOG_DIR": folder}), \
                 patch.object(gate, "process_start_time", return_value="start"), \
                 patch.object(gate, "process_table", return_value={}), \
                 patch.object(gate, "git_identity", return_value=(HANA, "repo")) as identity, \
                 patch.object(gate, "expected_peak", side_effect=[gate.ExpectedPeak(2 * GIB, "pooled", 5),
                                                                gate.ExpectedPeak(3 * GIB, "measured", 6)]) as history:
                for elapsed, expected_need, expected_source, lookups in (
                        (0, 2 * GIB, "pooled", 1), (599, 2 * GIB, "pooled", 1),
                        (600, 3 * GIB, "measured", 2)):
                    clock = NOW + timedelta(seconds=elapsed)
                    output = io.StringIO()
                    with patch.object(sys, "argv", arguments), redirect_stdout(output), \
                         patch.object(time, "time", return_value=clock.timestamp()), \
                         patch.object(gate, "datetime", wraps=datetime) as dates:
                        dates.configure_mock(**{"now.return_value": clock})
                        gate.main()
                    fields = output.getvalue().strip().split("|")
                    self.assertEqual((int(fields[2]), fields[3]), (expected_need, expected_source))
                    row = cast(gate.Waiter, json.loads(path.read_text()))
                    self.assertEqual((row["need"], row.get("repo_identity"), row.get("test_scope")),
                                     (expected_need, HANA.key, "full"))
                    self.assertEqual(history.call_count, lookups)
                    self.assertEqual(identity.call_count, lookups)
                    arguments = [*arguments[:6], fields[10], fields[11], fields[14],
                                 fields[2], fields[3], fields[4], "cargo", "nextest"]

    def test_cached_poll_keeps_repository_and_filter_scope_in_the_recorded_peak(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            sidecar = root / "step.cgroup"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            _ = sidecar.write_text("unused")
            # verify.sh test hana --filter native_rate builds this nextest expression.
            argv = ["cargo", "nextest", "run", "--test-threads", "16",
                    "-E", "package(hana) & test(native_rate)"]
            first = ["memory_admit.py", "check", str(meminfo), "400", str(sidecar), "0",
                     "", "", "", "-", "", "0", *argv]
            output = io.StringIO()
            with patch.dict(os.environ, {"BUILDLOG_DIR": folder}), \
                 patch.object(gate, "process_start_time", return_value="start"), \
                 patch.object(gate, "process_table", return_value={}), \
                 patch.object(gate, "git_identity", return_value=(HANA, "clone")) as identity, \
                 patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(2 * GIB, "measured", 5)) as history:
                with patch.object(sys, "argv", first), redirect_stdout(output):
                    gate.main()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(fields[0], "hold")
                cached = [*first[:6], fields[10], fields[11], fields[14], fields[2], fields[3], fields[4], *argv]
                _ = meminfo.write_text(f"MemAvailable: {40 * GIB // 1024} kB\n")
                output = io.StringIO()
                with patch.object(sys, "argv", cached), redirect_stdout(output):
                    gate.main()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(fields[0], "admit")
                reservation = Path(fields[9])
                _ = reservation.with_suffix(".max").write_text(str(GIB))
                gate.release(reservation, 0, root)
            self.assertEqual((identity.call_count, history.call_count), (1, 1))
            assert history.call_args is not None
            self.assertEqual(history.call_args.kwargs["package"], "hana")
            self.assertEqual(history.call_args.kwargs["filters"], ("native_rate",))
            sample = cast(dict[str, object], json.loads((root / "admission/anon_peaks.jsonl").read_text()))
            self.assertEqual((sample["repo_identity"], sample["test_scope"]), (HANA.key, "filtered"))
            self.assertEqual((sample["package"], sample["filters"]), ("hana", ["native_rate"]))

    def test_invoke_twice_with_temporary_home(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            config = root / "config.sh"
            _ = config.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
            environment = {**os.environ,
                           "BUILD_HOLD_DIR": str(root / "holders"),
                           "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                           "UNIT_ACTIVE_DIR": str(root / "active"),
                           "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                           "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                           "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_MEMINFO": str(meminfo),
                           "BUILDLOG_OFF": "1", "BUILDLOG_SCOPE": "0",
                           "LINT_CONFIG_READER": str(config)}
            command = 'source "$1"; sweep_after_step() { :; }; run_once true'
            for _ in range(2):
                result = subprocess.run(["bash", "-c", command, "test", str(INVOKE)],
                                        capture_output=True, text=True, timeout=300, check=False,
                                        env=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_gate_with_argv_and_temporary_home_reserves(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            command = 'source "$1"; buildlog_wait_for_memory /bin/true; printf "%s|%s|" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"; if [[ -n "$BUILDLOG_MEM_RESERVATION" && -f "$BUILDLOG_MEM_RESERVATION" ]]; then echo exists; else echo missing; fi; path=$BUILDLOG_MEM_RESERVATION; buildlog_release_memory 0; if [[ -n "$path" && ! -e "$path" ]]; then echo gone; else echo remains; fi'
            result = subprocess.run(
                ["bash", "-c", command, "test", str(MEMORY_GATE)],
                capture_output=True, text=True, timeout=300, check=False,
                env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                     "BUILDLOG_MEMINFO": str(meminfo)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^Granted\|[^|\n]+\|exists\ngone\n$")
            self.assertEqual(len(list((root / "log/admission").glob("*.reservation"))), 0)

    def test_sampler_maximum_reaches_history(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scope = root / "scope"
            scope.mkdir()
            stat = scope / "memory.stat"
            _ = stat.write_text(f"anon {GIB}\n")
            sidecar = root / "step.cgroup"
            _ = sidecar.write_text(str(scope) + "\n")
            with patch.object(gate, "git_identity", return_value=(HANA, "test-worktree")), \
                 patch.object(store, "host_name", return_value="test-host"):
                meminfo = root / "meminfo"
                _ = meminfo.write_text("MemAvailable: 12582912 kB\n")
                checked = gate.check(meminfo, os.getpid(), str(sidecar), ["cargo", "nextest"], root=root, now=NOW)
            assert isinstance(checked.reservation, gate.Reserved)
            reservation = checked.reservation.path
            maximum = reservation.with_suffix(".max")
            command = 'source "$1"; buildlog_sample_anon "$2" "$3" & p=$!; sleep 1.2; kill "$p"; wait "$p" 2>/dev/null || true'
            result = subprocess.run(["bash", "-c", command, "test", str(INVOKE), str(sidecar), str(maximum)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "BUILDLOG_OFF": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(maximum.read_text().strip(), str(GIB))
            with patch.object(store, "host_name", return_value="test-host"):
                gate.release(reservation, 0, root)
            row = cast(dict[str, object], json.loads((root / "admission/anon_peaks.jsonl").read_text()))
            self.assertEqual(row["anon_peak_bytes"], GIB)

    def test_launch_wait_limit_refuses_without_a_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 11010048 kB\n")
            command = ('set -e; source "$1"; sleep() { SECONDS=$((SECONDS + 1)); }; '
                       'buildlog_wait_for_memory; printf "step started\\n"')
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "BUILDLOG_DIR": str(root),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_POLL_S": "1",
                                         "BUILDLOG_MEM_WAIT_LIMIT_S": "1"})
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr.count("waiting for memory since "), 1)
            self.assertIn("(build needs ", result.stderr)
            self.assertIn("memory admission check failed (launch wait limit reached without admission); step refused", result.stderr)
            self.assertFalse((root / "admission").exists())

    def test_gate_wait_lines_in_order_without_repeated_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 8388608 kB\n")
            # Advance a test clock so checker runtime cannot change notice order.
            command = (
                'source "$1"; unset SECONDS; SECONDS=0; calls=0; sleep() { calls=$((calls + 1)); '
                'if (( calls == 1 )); then SECONDS=61; else printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"; fi; }; '
                'buildlog_wait_for_memory'
            )
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo)})
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stderr.splitlines()
            self.assertEqual(len(lines), 3, lines)
            self.assertTrue(lines[0].startswith("waiting for memory since "))
            self.assertTrue(lines[1].startswith("still waiting for memory (1 min): "))
            self.assertRegex(lines[2], r"^memory free after 1 min \d+ s: starting$")
            self.assertEqual(result.stderr.count("waiting for memory since "), 1)

    def test_build_never_starts_at_the_limit_and_waits_first_in_line(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            command = (
                'source "$1"; n=0; sleep() { n=$((n + 1)); echo slept >&2; ls "$BUILDLOG_DIR/admission" | grep -c "waiting$" >&2; '
                'if (( n >= 2 )); then echo "MemAvailable: 67108864 kB" > "$BUILDLOG_MEMINFO"; fi; }; buildlog_wait_for_memory true; '
                'printf "%s|%s|" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"; '
                'if [[ -n "$BUILDLOG_MEM_RESERVATION" && -f "$BUILDLOG_MEM_RESERVATION" ]]; then echo exists; else echo missing; fi; '
                'path=$BUILDLOG_MEM_RESERVATION; buildlog_release_memory 0; if [[ -n "$path" && ! -e "$path" ]]; then echo gone; else echo remains; fi'
            )
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_WAIT_LIMIT_S": "0"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^Granted\|[^|\n]+\|exists\ngone\n$")
            self.assertEqual(result.stderr.count("slept\n1\n"), 2, result.stderr)
            self.assertEqual(result.stderr.count("this step is now first in line"), 1, result.stderr)
            self.assertNotIn("starting anyway", result.stderr)
            self.assertEqual(list((root / "log/admission").glob("*.waiting")), [])
            self.assertEqual(len(list((root / "log/admission").glob("*.reservation"))), 0)

    def test_fitting_at_limit_is_granted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            command = ('source "$1"; sleep() { printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"; SECONDS=1; }; '
                       'buildlog_wait_for_memory /bin/true; printf "%s\n" "$BUILDLOG_MEM_OUTCOME"; buildlog_release_memory 0')
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_WAIT_LIMIT_S": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "Granted\n")
            self.assertIn("memory free after", result.stderr)
            self.assertNotIn("memory wait limit reached", result.stderr)

    def test_python_failure_refuses_the_step_and_names_the_checker_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            broken = root / "broken.py"
            _ = broken.write_text('raise RuntimeError("broken lookup")\n')
            command = 'set -e; source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; buildlog_wait_for_memory /bin/true; printf "step started\\n"'
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(broken)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "BUILDLOG_MEMINFO": str(meminfo),
                                         "HOME": str(root), "BUILDLOG_DIR": str(root / "log")})
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("memory admission check failed (RuntimeError: broken lookup); step refused", result.stderr)

    def test_missing_memory_reading_refuses_the_step_and_names_meminfo(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            command = ('set -e; source "$1"; buildlog_wait_for_memory /bin/true; '
                       'printf "step started\\n"')
            for contents in ("", "MemTotal: 67108864 kB\n"):
                with self.subTest(contents=contents):
                    _ = meminfo.write_text(contents)
                    result = subprocess.run(
                        ["bash", "-c", command, "test", str(MEMORY_GATE)],
                        capture_output=True, text=True, timeout=300, check=False,
                        env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                             "BUILDLOG_MEMINFO": str(meminfo)},
                    )
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(f"memory admission check failed (cannot read MemAvailable from {meminfo}); step refused", result.stderr)

    def test_untracked_admission_refuses_the_step_and_names_the_answer(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            wrapper = root / "untracked.py"
            _ = wrapper.write_text(f'import sys\nsys.path.insert(0, {str(MEMORY_GATE.parent)!r})\nimport memory_admit as gate\ngate.process_start_time = lambda pid: None\ngate.main()\n')
            command = 'set -e; source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; buildlog_wait_for_memory /bin/true; printf "step started\\n"'
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(wrapper)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo)})
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("memory admission check failed (invalid admission answer admit:untracked); step refused", result.stderr)


class QueueLeaveTests(unittest.TestCase):
    """LINT_QUEUE_LEAVE_HOOK on run_once's memory wait, with a stand-in build log recorder."""

    root: Path = Path()
    meminfo: Path = Path()
    calls: Path = Path()
    ran: Path = Path()
    recorded: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.meminfo = self.root / "meminfo"
        self.calls = self.root / "hook-calls"
        self.ran = self.root / "ran"
        self.recorded = self.root / "recorded"
        scripts = self.root / ".claude/scripts"
        (scripts / "lib").mkdir(parents=True)
        (scripts / "buildlog").mkdir()
        py = scripts / "lib/py"
        _ = py.write_text('#!/bin/sh\nexec python3 "$@"\n')
        py.chmod(0o755)
        _ = (scripts / "buildlog/record.py").write_text(
            f"import sys\nwith open({str(self.recorded)!r}, 'a') as log:\n    log.write(sys.argv[1] + '\\n')\n")
        config = self.root / "config.sh"
        _ = config.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
        self.environment = {**os.environ,
                            "BUILD_HOLD_DIR": str(self.root / "holders"),
                            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
                            "UNIT_ACTIVE_DIR": str(self.root / "active"),
                            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"), "HOME": str(self.root),
                            "BUILDLOG_DIR": str(self.root / "log"), "BUILDLOG_MEMINFO": str(self.meminfo),
                            "BUILDLOG_SCOPE": "0", "BUILDLOG_SYNC": "1", "LINT_SKIP_SWEEP": "1",
                            "LINT_CONFIG_READER": str(config)}
        _ = self.environment.pop("LINT_QUEUE_LEAVE_HOOK", None)

    def run_step(self, hook: str | None, sleep: str, step: str = 'run_once touch "$2"') -> subprocess.CompletedProcess[str]:
        _ = self.meminfo.write_text("MemAvailable: 0 kB\n")
        environment = dict(self.environment)
        if hook is not None:
            environment["LINT_QUEUE_LEAVE_HOOK"] = f'echo poll >> "{self.calls}"; {hook}'
        command = (f'source "$1"; sleeps=0; sleep() {{ sleeps=$((sleeps + 1)); {sleep} }}; '
                   + f'status=0; {step} || status=$?; '
                   + 'printf "%s|%s|%s\\n" "$status" "$BUILDLOG_MEM_OUTCOME" "${BUILDLOG_START:-}"')
        return subprocess.run(["bash", "-c", command, "test", str(INVOKE), str(self.ran)],
                              capture_output=True, text=True, timeout=300, check=False, env=environment)

    def hook_calls(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def recorder_actions(self) -> list[str]:
        return self.recorded.read_text().splitlines() if self.recorded.exists() else []

    def reservations(self) -> list[Path]:
        return list((self.root / "log/admission").glob("*.reservation"))

    def test_hook_leaves_a_held_step_before_it_starts(self) -> None:
        result = self.run_step(":", ":;")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "75|LeftQueue|")
        self.assertFalse(self.ran.exists())
        self.assertEqual(self.hook_calls(), 1)
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.recorder_actions(), ["key"])
        # The wait is measured in whole seconds of SECONDS, so a loaded machine
        # can cross a second between the first check and the leave.
        self.assertRegex(result.stderr, r"another machine is free after \d+ min \d+ s: leaving the memory wait\n")

    def test_step_that_can_start_never_asks_the_hook(self) -> None:
        free = 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO";'
        result = self.run_step("exit 0", free)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1].split("|")[:2], ["0", "Granted"])
        self.assertTrue(self.ran.exists())
        self.assertEqual(self.hook_calls(), 0)
        self.assertEqual(self.recorder_actions(), ["key", "step"])

    def test_declining_hook_and_no_hook_wait_alike(self) -> None:
        free_on_second = 'if (( sleeps == 2 )); then printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"; fi;'
        outputs: list[tuple[str, list[str]]] = []
        for hook in ("exit 1", None):
            with self.subTest(hook=hook):
                self.ran.unlink(missing_ok=True)
                result = self.run_step(hook, free_on_second)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.ran.exists())
                self.assertEqual(self.reservations(), [])
                lines = result.stderr.splitlines()
                outputs.append((result.stdout.splitlines()[-1].split("|")[1], [re.sub(r"\d+", "N", line) for line in lines]))
        self.assertEqual(self.hook_calls(), 1)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0][0], "Granted")

    def test_nextest_helper_returns_the_status_and_drops_its_width_claim(self) -> None:
        step = ('LINT_TEST_WIDTH=logical; cargo() { [[ "$*" == "nextest --version" ]] && return 0; touch "$2"; }; '
                + 'run_nextest --workspace')
        result = self.run_step("exit 0", ":;", step)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "75|LeftQueue|")
        self.assertFalse(self.ran.exists())
        # A run still waiting for memory claims no share, so it leaves no marker behind.
        self.assertEqual(list((self.root / "log/admission/nextest").iterdir()), [])
        self.assertEqual(self.reservations(), [])

    def nextest_step(self, ran_args: Path, reservation: Path) -> str:
        return ('LINT_TEST_WIDTH=logical; cargo() { [[ "$*" == "nextest --version" ]] && return 0; '
                + f'printf "%s\\n" "$*" > "{ran_args}"; cat "$BUILDLOG_MEM_RESERVATION" > "{reservation}"; }}; '
                + 'run_nextest --workspace')

    def test_a_run_waiting_for_memory_claims_its_share_only_once_admitted(self) -> None:
        listing, ran_args, reservation = self.root / "listing", self.root / "args", self.root / "reservation"
        free = (f'ls -A "$BUILDLOG_DIR/admission/nextest" >> "{listing}"; echo waited >> "{listing}"; '
                + 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO";')
        result = self.run_step(None, free, self.nextest_step(ran_args, reservation))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1].split("|")[:2], ["0", "Granted"])
        self.assertEqual(listing.read_text().splitlines(), ["waited"])
        width = os.sysconf("SC_NPROCESSORS_ONLN")
        self.assertEqual(ran_args.read_text().split()[:4], ["nextest", "run", "--test-threads", str(width)])
        self.assertEqual(json.loads(reservation.read_text())["test_threads"], width)
        self.assertEqual([path.name for path in (self.root / "log/admission/nextest").iterdir()], ["lock"])

    def test_an_admitted_run_shares_the_threads_with_a_run_in_flight(self) -> None:
        width = os.sysconf("SC_NPROCESSORS_ONLN")
        shared = budget.shared_width(width, 2)
        if shared == width:
            self.skipTest("this machine has too few cores to share")
        ran_args, reservation = self.root / "args", self.root / "reservation"
        markers = self.root / "log/admission/nextest"
        markers.mkdir(parents=True)
        free = 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO";'
        with (markers / "other.run").open("a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            result = self.run_step(None, free, self.nextest_step(ran_args, reservation))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"nextest: {shared} of {width} test threads, shared with the runs in flight", result.stdout)
        self.assertEqual(ran_args.read_text().split()[:4], ["nextest", "run", "--test-threads", str(shared)])
        self.assertEqual(json.loads(reservation.read_text())["test_threads"], shared)
        self.assertEqual(sorted(path.name for path in markers.iterdir()), ["lock", "other.run"])


class WaiterNeedSourceTests(MemoryQueueFixture):
    def test_held_waiter_persists_the_estimate_source_count_and_requested_width(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: 0 kB\nMemTotal: {60 * GIB // 1024} kB\n")
        argv = ["cargo", "nextest", "run", "-E", "package(hana) & (test(one) | test(two))",
                "--test-threads", "8"]
        for peak in (gate.ExpectedPeak(12 * GIB, "measured", 7), gate.ExpectedPeak(12 * GIB, "buildlog", 12),
                     gate.ExpectedPeak(12 * GIB, "pooled", 31), gate.ExpectedPeak(12 * GIB, "fallback", 0)):
            with self.subTest(source=peak.source):
                result = gate.check(self.meminfo, 400, "", argv, root=self.root, now=NOW,
                                    profile=gate.StepProfile("hana", "nextest", "tree", peak, HANA, "filtered"))
                self.assertEqual(result.decision.state, "hold")
                row = cast(dict[str, object], json.loads(
                    (self.root / "admission/400-warmup.waiting").read_text()))
                self.assertEqual(row["need_source"], peak.source)
                self.assertEqual(row["need_runs"], peak.count)
                self.assertEqual(row["test_threads"], 8)
                self.assertEqual(row["need"], peak.bytes)
                self.assertEqual(row["test_scope"], "filtered")
                self.assertEqual(row["package"], "hana")
                self.assertEqual(row["filters"], ["one", "two"])
                self.assertEqual(row["estimated_at"], NOW.timestamp())

    def test_waiter_without_an_explicit_width_records_a_null_width(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: 0 kB\nMemTotal: {60 * GIB // 1024} kB\n")
        result = gate.check(self.meminfo, 400, "", ["cargo", "clippy"], root=self.root, now=NOW,
                            profile=gate.StepProfile("hana", "clippy", "tree",
                                                     gate.ExpectedPeak(GIB, "measured", 9), HANA))
        self.assertEqual(result.decision.state, "hold")
        row = cast(dict[str, object], json.loads((self.root / "admission/400-warmup.waiting").read_text()))
        self.assertIn("test_threads", row)
        self.assertIsNone(row["test_threads"])
        self.assertEqual((row["need_source"], row["need_runs"]), ("measured", 9))


class QueueExplanationTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_BUILD_BUDGET_GIB": "21",
            "BUILDLOG_MEMORY_PRESSURE": str(self.root / "pressure"),
        }))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.meminfo.write_text(f"MemAvailable: {16 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = (self.root / "pressure").write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")

    def waiting(self, pid: int, need: int, minutes: int, **fields: object) -> Path:
        row: dict[str, object] = {
            "pid": pid, "start_time": self.processes[pid], "need": need,
            "since": NOW.timestamp() - minutes * 60, "session_id": f"session-{pid}",
            "session_name": f"worker-{pid}", "step": "nextest", "folder": f"/tmp/tree-{pid}",
            **fields,
        }
        path = gate.admission_dir(self.root) / f"{pid}.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def test_explain_matches_read_queue_and_admission_on_the_same_files(self) -> None:
        _ = self.running(100, 6, age=30)
        _ = self.running(200, 4, age=30)
        _ = self.waiting(300, 12 * GIB, 20, need_source="measured", need_runs=7, test_threads=8,
                         test_scope="filtered", package="hana", filters=["one", "two"],
                         bump_order=-1, priority_since=NOW.timestamp() - 120)
        _ = self.waiting(400, GIB, 5)
        self.processes[500] = "fifth"
        _ = self.waiting(500, 9 * GIB, 3)
        directory = self.root / "admission"
        _ = (directory / "lock").touch()
        processes = gate.process_table(self.root / "proc")
        view, ordered = gate.read_queue(directory, processes, NOW.timestamp())
        reason = view.waiting[0].reason
        if not isinstance(reason, gate.HeadAdmissionStatus):
            self.fail(f"expected admission status, got {reason}")

        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        with patch("sqlite3.connect", side_effect=AssertionError("SQLite is outside the explain read path")), \
             patch.object(gate, "process_table", wraps=gate.process_table) as table, \
             patch.object(gate, "read_meminfo", wraps=gate.read_meminfo) as host_read, \
             patch.object(gate, "reservation_memory", wraps=gate.reservation_memory) as memory_read, \
             patch.object(fcntl, "flock", wraps=fcntl.flock) as lock:
            explanation = gate.explain_queue()
        self.assertEqual(table.call_count, 1)
        self.assertEqual(host_read.call_count, 1)
        self.assertEqual(memory_read.call_count, 2)
        self.assertEqual([call.args[1] for call in lock.call_args_list], [fcntl.LOCK_SH])
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)
        head, host = explanation.head, explanation.host
        if not isinstance(head, gate.HeadExplanation) or not isinstance(host, gate.HostAvailable):
            self.fail(f"expected readable host and head, got {explanation}")
        if not isinstance(host.total, int):
            self.fail("fixture must have a total memory reading")
        self.assertEqual(head.waiter, view.waiting[0])
        self.assertEqual(head.decision, reason.decision)
        memories = [step.measured for step in view.running]
        priority = gate.head_priority([row for _, row in ordered], view.available, host.total, memories, NOW.timestamp())
        self.assertEqual(head.decision, gate.queue_decision(
            view.available, host.total, ordered[0][1], memories, priority, host.pressure))
        self.assertEqual(head.decision.state, "hold")
        self.assertEqual((host.available, host.total, host.reserve, host.budget),
                         (16 * GIB, 60 * GIB, 3 * GIB, 21 * GIB))
        self.assertEqual((explanation.totals.charged, explanation.totals.measured, explanation.totals.growth),
                         (10 * GIB, 3 * GIB, 7 * GIB))
        self.assertEqual((head.budget.charged, head.budget.need, head.budget.head_need, head.budget.budget,
                          head.budget.starts_at, head.budget.passes),
                         (10 * GIB, 12 * GIB, 0, 21 * GIB, 9 * GIB, False))
        self.assertEqual((head.free.available, head.free.need, head.free.reserve, head.free.growth,
                          head.free.head_need, head.free.threshold, head.free.passes),
                         (16 * GIB, 12 * GIB, 3 * GIB, 7 * GIB, 0, 22 * GIB, False))
        self.assertEqual(head.order, gate.head_window(ordered[0][1], NOW.timestamp()))
        self.assertTrue(head.has_priority)
        self.assertEqual(head.bump, gate.QueueBump(NOW.timestamp() - 120, True, False))
        self.assertEqual(head.need_source, gate.RecordedNeed(
            "measured", 7, gate.HISTORY_DAYS, "filtered", "hana", ("one", "two"), 8))
        self.assertEqual((explanation.line.count, explanation.line.fitting, explanation.line.smallest_need,
                          explanation.line.largest_need, explanation.line.oldest_since),
                         (2, 1, GIB, 9 * GIB, NOW.timestamp() - 300))
        self.assertEqual([step.waiter for step in explanation.line.steps], list(view.waiting[1:]))
        for step in explanation.line.steps:
            self.assertEqual(step.fits_now, gate.decide(host.available, 60 * GIB, step.waiter.need,
                                                       memories, pressure=host.pressure).state == "admit")
        self.assertEqual([(step.promise, step.measured, step.charged, step.admitted_at)
                          for step in explanation.running],
                         [(6 * GIB, GIB, 6 * GIB, NOW.timestamp() - 30),
                          (4 * GIB, 2 * GIB, 4 * GIB, NOW.timestamp() - 30)])
        self.assertTrue(all(isinstance(step.basis, gate.RampPromise) for step in explanation.running))

    def test_old_waiter_without_source_has_an_explicit_unrecorded_variant(self) -> None:
        path = self.waiting(300, 12 * GIB, 20)
        before = path.read_bytes()
        head = gate.explain_queue().head
        if not isinstance(head, gate.HeadExplanation):
            self.fail(f"expected head, got {head}")
        self.assertIsInstance(head.need_source, gate.NeedSourceNotRecorded)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_host_reading_and_empty_queue_use_their_domain_variants(self) -> None:
        path = self.waiting(300, 12 * GIB, 20)
        self.meminfo.unlink()
        explanation = gate.explain_queue()
        self.assertIsInstance(explanation.host, gate.ReadingUnavailable)
        if not isinstance(explanation.head, gate.HeadExplanation):
            self.fail(f"expected head, got {explanation.head}")
        self.assertIsInstance(explanation.head.waiter.reason, gate.ReadingUnavailable)
        path.unlink()
        empty = gate.explain_queue()
        self.assertIsInstance(empty.head, gate.NoWaiter)
        self.assertEqual((empty.line.count, empty.line.steps), (0, ()))

    def test_peak_charge_basis_uses_the_same_charge_as_admission_for_each_rule(self) -> None:
        self.readings[100] = gate.MeasuredMemory(GIB)
        path = self.running(100, 8, age=300)
        cases = (
            ("using", "", None, gate.UsingNow, GIB),
            ("peak", str(5 * GIB), int(NOW.timestamp() - 30), gate.PeakSoFar, 5 * GIB),
            ("decayed", str(5 * GIB), int(NOW.timestamp() - 1600), gate.PeakDecayed, GIB),
            ("unreadable", "not a byte count", None, gate.PeakFileUnreadable, 8 * GIB),
        )
        for label, peak, recorded_at, expected_basis, expected_charge in cases:
            with self.subTest(rule=label):
                path.with_suffix(".max").unlink(missing_ok=True)
                path.with_suffix(".wave").unlink(missing_ok=True)
                if peak:
                    _ = path.with_suffix(".max").write_text(peak)
                if recorded_at is not None:
                    _ = path.with_suffix(".wave").write_text(f"{peak} {recorded_at}")
                explanation = gate.explain_queue()
                self.assertEqual(len(explanation.running), 1)
                running = explanation.running[0]
                self.assertEqual(running.charged, expected_charge)
                self.assertEqual(running.charged, gate.build_charge(running.step.measured))
                self.assertIsInstance(running.basis, expected_basis)

    def test_old_sampler_decay_uses_peak_file_publication_time(self) -> None:
        path = self.running(100, 8, age=300)
        _ = path.with_suffix(".max").write_text(str(5 * GIB))
        old_peak_at = NOW.timestamp() - 1600
        os.utime(path.with_suffix(".max"), (old_peak_at, old_peak_at))
        running = gate.explain_queue().running[0]
        if not isinstance(running.basis, gate.PeakDecayed):
            self.fail(f"expected old-sampler decay, got {running.basis}")
        self.assertTrue(running.basis.old_sampler)
        self.assertEqual(running.basis.decayed_at, old_peak_at + gate.PEAK_DECAY_SECONDS)
        self.assertEqual((running.promise, running.charged), (8 * GIB, GIB))

    def test_no_reading_yet_names_the_promised_charge(self) -> None:
        self.readings[100] = gate.MeasuredMemory(0)
        _ = self.running(100, 8, age=30)
        running = gate.explain_queue().running[0]
        self.assertIsInstance(running.basis, gate.PromiseNoReading)
        self.assertEqual((running.promise, running.measured, running.charged), (8 * GIB, 0, 8 * GIB))

    def test_pressure_and_recovery_spacing_match_the_head_decision_without_writing(self) -> None:
        _ = self.waiting(300, GIB, 20)
        directory = self.root / "admission"
        _ = (self.root / "pressure").write_text("full avg10=11.00 avg60=0.00 avg300=0.00 total=0\n")
        explanation = gate.explain_queue()
        if not isinstance(explanation.host, gate.HostAvailable) or not isinstance(explanation.head, gate.HeadExplanation):
            self.fail(f"expected readable explanation, got {explanation}")
        self.assertEqual(explanation.host.pressure, gate.MemoryPressure(11))
        self.assertEqual(explanation.head.decision.state, "hold")
        self.assertEqual(explanation.head.decision.pressure, explanation.host.pressure)
        self.assertFalse((directory / "pressure-recovery").exists())

        _ = (self.root / "pressure").write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        record = directory / "pressure-recovery"
        _ = record.write_text(json.dumps({"state": "recovering", "ends_at": NOW.timestamp() + 100,
                                         "next_start_at": NOW.timestamp() + 30}))
        before = record.read_bytes()
        recovery = gate.explain_queue()
        if not isinstance(recovery.host, gate.HostAvailable) or not isinstance(recovery.head, gate.HeadExplanation):
            self.fail(f"expected recovery explanation, got {recovery}")
        self.assertEqual(recovery.host.pressure, gate.MemoryPressure(0, next_start_at=NOW.timestamp() + 30))
        self.assertEqual(recovery.head.decision.state, "hold")
        self.assertEqual(record.read_bytes(), before)


if __name__ == "__main__":
    _ = unittest.main()
