#!/usr/bin/env python3
"""Memory admission with only temporary history, cgroups, and ledgers."""

from __future__ import annotations

import fcntl
import io
import json
import math
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
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
import contextlib
from contextlib import closing, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, TextIO, cast, override
from unittest import mock
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_admit as gate
import nextest_budget as budget
from build_shape import BuildShape, build_shape
from memory_test_support import fake_memory_readings, gate_isolation_environment
store = gate.store


class BuildStepRecorder(Protocol):
    store: gate.BuildLogStore

    def step(self, arguments: list[str]) -> None: ...


buildlog_record = cast(BuildStepRecorder, cast(object, gate.record))

GIB = gate.GIB
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
NOW_EPOCH = NOW.timestamp()
HISTORY_AT = NOW - timedelta(seconds=1)
FRESH_HISTORY_AT = NOW - timedelta(minutes=5)
LAST_TEST_BUILD_AT = NOW - timedelta(minutes=1)
INVOKE = Path(__file__).with_name("invoke.sh")
MEMORY_GATE = Path(__file__).with_name("memory_gate.sh")
HANA = gate.RepositoryIdentity("https://example.org/nate/hana", "hana", "hana")
RELEASE_BUILD = ["cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "hana"]
RELEASE_SHAPE = build_shape(RELEASE_BUILD)
HistoryAppendStop = Literal["before newline", "inside UTF-8"]
HISTORY_APPEND_STOPS: tuple[HistoryAppendStop, ...] = ("before newline", "inside UTF-8")


def buildlog_rows(root: Path) -> list[dict[str, object]]:
    return [cast(dict[str, object], json.loads(line)) for path in root.glob("*/*.jsonl")
            for line in path.read_text().splitlines()]


def assert_unfinished_history_ignored(test: unittest.TestCase, path: Path,
                                     read: Callable[[], object]) -> None:
    """Every estimate must retain its whole samples and leave repair to the writer."""
    whole = path.read_bytes()
    sample = cast(dict[str, object], json.loads(whole.splitlines()[-1]))
    sample["note"] = "build stopped at \u0394"
    encoded = json.dumps(sample, ensure_ascii=False).encode()
    expected = read()
    for name, tail in (("before newline", encoded),
                       ("inside UTF-8", encoded[:encoded.index("\u0394".encode()) + 1])):
        with test.subTest(tail=name):
            contents = whole + tail
            _ = path.write_bytes(contents)
            try:
                test.assertEqual(read(), expected, "an unfinished row is not another history sample")
                test.assertEqual(path.read_bytes(), contents, "an estimate must not repair history")
            finally:
                _ = path.write_bytes(whole)


@contextlib.contextmanager
def stop_history_append(history_store: object, tail: HistoryAppendStop
                        ) -> Generator[None]:
    """Stop the real serializer's write; admission and sweep share this fixture."""
    append = cast(Callable[[BinaryIO, object], None], getattr(history_store, "append_locked"))

    def interrupted_append(handle: BinaryIO, row: object) -> None:
        def write_prefix(fd: int, data: bytes) -> None:
            end = len(data) - 1 if tail == "before newline" else data.index("\u0394".encode()) + 1
            _ = os.write(fd, data[:end])
            raise OSError("injected stop inside history append")

        with patch.object(history_store, "_write_all", side_effect=write_prefix):
            append(handle, row)

    with patch.object(history_store, "append_locked", side_effect=interrupted_append):
        yield


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
        running: list[gate.StepMemory] = [gate.MeasuredMemory(3 * GIB, gate.RampPromise(3 * GIB)), gate.PromisedMemory(2 * GIB)]
        result = gate.decide(10 * GIB, 60 * GIB, 5 * GIB, running)
        self.assertEqual((result.promised, result.measured, result.threshold, result.state),
                         (2 * GIB, 3 * GIB, 10 * GIB, "admit"))
        self.assertEqual(gate.decide(10 * GIB - 1, 60 * GIB, 5 * GIB, running).state, "hold")

    def test_ramping_step_counts_promise_or_higher_measured_use(self) -> None:
        for used, headroom, state in ((1, 11, "hold"), (11, 1, "admit"), (13, 0, "hold")):
            with self.subTest(measured_gib=used):
                result = gate.decide(15 * GIB, 60 * GIB, 8 * GIB,
                                     [gate.MeasuredMemory(used * GIB, gate.RampPromise(12 * GIB))])
                self.assertEqual((result.state, result.promised, result.measured),
                                 (state, headroom * GIB, used * GIB))

    def test_no_total_has_no_reserve(self) -> None:
        result = gate.decide(5 * GIB, gate.UnknownHostTotal(), 5 * GIB, [])
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
        head: gate.Waiter = {"pid": 1, "start_time": "1", "need": int(19.3 * GIB), "since": 0,
                             "holds_younger": True, "priority_since": NOW.timestamp()}
        younger: gate.Waiter = {"pid": 2, "start_time": "2", "need": int(10.8 * GIB), "since": 1}
        decision = gate.queue_decision(40 * GIB, 60 * GIB, younger, [head, younger], [],
                                       gate.HeadPriority(head, gate.StrictUntil(NOW.timestamp() + 600)),
                                       gate.NoPressureHold(), recovery=gate.NoRecoverySpacing(), now=NOW.timestamp())
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

    def test_unfinished_rows_do_not_change_the_full_suite_estimate(self) -> None:
        self.add_measured(5)
        assert_unfinished_history_ignored(self, self.admission / "anon_peaks.jsonl", self.peak)

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

    def test_unfinished_rows_do_not_change_the_filtered_estimate(self) -> None:
        self.samples([3 * GIB], filters=("native_rate",))
        assert_unfinished_history_ignored(
            self, self.root / "admission/anon_peaks.jsonl",
            lambda: gate.filtered_peak(HANA, "nextest", self.host, NOW, self.root,
                                       None, ("native_rate",), "hana"))

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

    def test_unfinished_rows_do_not_change_the_fresh_test_estimate(self) -> None:
        self.samples([3 * GIB])
        assert_unfinished_history_ignored(
            self, self.root / "admission/anon_peaks.jsonl",
            lambda: gate.fresh_test_peak(HANA, "nextest", self.host, NOW, self.root, None,
                                         "filtered", ("native_rate",), "hana",
                                         gate.ExpectedPeak(10 * GIB, "measured", 10)))

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
        with patch.object(gate, "step_memory", return_value=gate.MeasuredMemory(8 * GIB, gate.RampPromise(12 * GIB))):
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
        self.assertEqual(younger.decision.threshold - younger.peak.bytes - younger.decision.reserve, 0)
        self.assertIsInstance(younger.decision.head_room, gate.HeadRoom)
        if isinstance(younger.decision.head_room, gate.HeadRoom):
            self.assertEqual(younger.decision.head_room.head_need, older["need"])

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
            profile = gate.InferFromArgv()
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
            # Newly admitted live processes have not allocated memory yet.
            return self.readings.get(pid, gate.MeasuredMemory(0))
        _ = self.enterContext(patch.object(gate, "tree_memory", side_effect=reading))

    def waiter(self, pid: int, since: float, folder: str, name: str, need: float = 10.9) -> Path:
        row: gate.Waiter = {"pid": pid, "start_time": self.processes[pid], "since": since, "need": int(need * GIB),
                            "session_id": f"session-{pid}", "session_name": name, "step": "nextest", "folder": folder}
        path = gate.admission_dir(self.root) / f"{pid}-{self.processes[pid]}.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def running(self, pid: int, need: float, age: float = 180) -> Path:
        # Each reservation setup starts a live owner, including after an ended-owner case.
        _ = self.processes.setdefault(pid, f"fixture-{pid}")
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


    def shell_fields(self, result: gate.CheckResult) -> list[str]:
        arguments = ["memory_admit.py", "check", str(self.meminfo), "500", "", "0",
                     "-", "-", "-", "-", "-", "-", "cargo", "check"]
        with patch.object(sys, "argv", arguments), patch.object(gate, "check", return_value=result), \
             redirect_stdout(io.StringIO()) as output:
            gate.run_action()
        return output.getvalue().strip().split("|")



class BuildPromiseFixture(MemoryQueueFixture):
    """One private history, index and ledger for the build-promise regressions."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root), "BUILDLOG_BUILD_BUDGET_GIB": "21",
        }))
        _ = self.enterContext(patch.object(gate, "git_identity", return_value=(HANA, "hana-clone")))
        _ = self.enterContext(patch.object(store, "host_name", return_value="test-host"))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))

    def sample(self, peak: float, *, shape: BuildShape = RELEASE_SHAPE,
               at: datetime = HISTORY_AT, outcome: str = "completed", need: float = 21,
               host: str = "test-host", repository: gate.RepositoryIdentity = HANA,
               call_id: str = "", record_shape: bool = True) -> None:
        row: dict[str, object] = {
            "host": host, "repo": repository.name, "repo_identity": repository.key,
            "step": "build", "ended_at": store.utc_iso(at.timestamp()),
            "anon_peak_bytes": int(peak * GIB), "need": int(need * GIB), "call_id": call_id,
        }
        if record_shape:
            row["shape"] = shape.key
        if outcome:
            row["outcome"] = outcome
        with (gate.admission_dir(self.root) / "anon_peaks.jsonl").open("a") as stream:
            _ = stream.write(json.dumps(row) + "\n")

    def index_build(self, call_id: str, arguments: list[str]) -> None:
        with closing(sqlite3.connect(self.root / "index.sqlite")) as connection, connection:
            _ = connection.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, "
                                   + "repo_identity TEXT, step TEXT, started_at TEXT, "
                                   + "call_id TEXT, argv TEXT, peak_mem_bytes INTEGER)")
            _ = connection.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   ("test-host", "hana", HANA.key, "build", HISTORY_AT.isoformat(),
                                    call_id, json.dumps(arguments), 0))

    def estimate(self, shape: BuildShape = RELEASE_SHAPE, *, now: datetime = NOW) -> gate.ExpectedPeak:
        return gate.expected_peak(HANA, "build", "test-host", now, self.root, shape=shape)

    def same_shape_basis(self, peak: gate.ExpectedPeak) -> gate.ShapeP90 | gate.ShapeLargest:
        if not isinstance(peak.basis, (gate.ShapeP90, gate.ShapeLargest)):
            self.fail(f"expected a same-shape history basis, got {peak.basis}")
        return peak.basis

    def build_reservation(self, *, pid: int = 100, shape: BuildShape = RELEASE_SHAPE,
                          need: float = 16, age: float = 3600, sidecar: bool = False) -> Path:
        path = self.running(pid, need, age)
        row = cast(dict[str, object], json.loads(path.read_text()))
        row.update(step="build", repo="hana", repo_identity=HANA.key, shape=shape.key)
        if sidecar:
            scope_file = self.root / f"{pid}.cgroup"
            _ = scope_file.write_text(str(self.root / f"{pid}.ended.scope"))
            row["sidecar"] = str(scope_file)
        _ = path.write_text(json.dumps(row))
        return path

    def build_check(self, *, available: float = 40, argv: list[str] = RELEASE_BUILD,
                    sidecar: str = "") -> gate.CheckResult:
        _ = self.meminfo.write_text(f"MemAvailable: {int(available * GIB) // 1024} kB\n"
                                   + f"MemTotal: {60 * GIB // 1024} kB\n")
        return gate.check(self.meminfo, 400, sidecar, argv, root=self.root, now=NOW)


class BuildShapePromiseTests(BuildPromiseFixture):
    def test_unfinished_rows_do_not_enter_build_shape_history(self) -> None:
        self.sample(3)
        assert_unfinished_history_ignored(
            self, self.root / "admission/anon_peaks.jsonl",
            lambda: gate.build_peaks(HANA, "test-host", NOW, self.root))

    def test_builds_without_same_shape_history_keep_the_unfactored_fallback(self) -> None:
        for arguments in (RELEASE_BUILD, ["cargo", "build", "--bin", "hana"]):
            shape = build_shape(arguments)
            with self.subTest(profile=shape.profile):
                peak = self.estimate(shape)
                self.assertEqual((peak.bytes, peak.source, peak.count), (12 * GIB, "shape", 0))
                self.assertEqual(peak.basis, gate.ShapeFallback())
                self.assertEqual(gate.need_basis_record(peak.basis), {
                    "kind": "shape_fallback", "cap": {"kind": "none"},
                })

    def test_profile_fallback_caps_its_unfactored_base_at_the_build_budget(self) -> None:
        for arguments in (RELEASE_BUILD, ["cargo", "build", "--bin", "hana"]):
            shape = build_shape(arguments)
            self.sample(24, shape=build_shape(["cargo", "build", "--profile", shape.profile, "--lib"]),
                        outcome="killed", need=20)
            with self.subTest(profile=shape.profile):
                peak = self.estimate(shape)
                self.assertEqual((peak.bytes, peak.count), (21 * GIB, 0))
                self.assertEqual(peak.basis, gate.ShapeFallback(cap=gate.CappedAtBudget(24 * GIB)))
                self.assertEqual(gate.need_basis_record(peak.basis), {
                    "kind": "shape_fallback", "cap": {"kind": "budget", "uncapped": 24 * GIB},
                })

    def test_release_history_matches_repository_features_and_target(self) -> None:
        self.sample(2)
        self.sample(3)
        for arguments in (
            ["cargo", "build", "--bin", "hana", "--features=bevy/trace_chrome"],
            ["cargo", "build", "--release", "--bin", "hana"],
            ["cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "other"],
            ["cargo", "build", "--release", "--all-features", "--bin", "hana"],
            [*RELEASE_BUILD, "--no-default-features"],
        ):
            for _ in range(10):
                self.sample(20, shape=build_shape(arguments))
        self.sample(20, repository=gate.RepositoryIdentity("https://example.org/other/hana", "hana"))
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.source, peak.count), (math.ceil(3 * GIB * 1.314), "shape", 2))
        self.assertEqual(peak.basis, gate.ShapeLargest(2, gate.NoFloor(), gate.ReleaseHeadroom(), gate.UnderBudget()))

    def test_fewer_than_ten_same_shape_runs_use_the_largest(self) -> None:
        for value in range(1, 10):
            self.sample(value)
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count), (math.ceil(9 * GIB * 1.314), 9))
        self.assertIsInstance(peak.basis, gate.ShapeLargest)

    def test_ten_same_shape_runs_use_p90(self) -> None:
        for _ in range(9):
            self.sample(3)
        self.sample(10)
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count), (math.ceil(3 * GIB * 1.314), 10))
        self.assertEqual(peak.basis, gate.ShapeP90(10, gate.NoFloor(), gate.ReleaseHeadroom(), gate.UnderBudget()))

    def test_profile_fallback_uses_release_builds_and_never_the_dev_pool(self) -> None:
        self.sample(13, shape=build_shape(["cargo", "build", "--release", "--lib"]))
        for _ in range(10):
            self.sample(20, shape=build_shape(["cargo", "build"]))
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count, peak.source), (13 * GIB, 0, "shape"))
        self.assertIsInstance(peak.basis, gate.ShapeFallback)

    def test_without_release_history_the_profile_fallback_is_at_least_fallback(self) -> None:
        self.sample(20, shape=build_shape(["cargo", "build"]))
        self.sample(2, shape=build_shape(["cargo", "build", "--release", "--lib"]))
        self.assertEqual(self.estimate().bytes, 12 * GIB)

    def test_headroom_applies_only_to_the_release_profile(self) -> None:
        for arguments, factor in ((RELEASE_BUILD, 1.314), (["cargo", "build"], gate.DEV_HEADROOM),
                                  (["cargo", "build", "--profile", "small"], 1.0)):
            shape = build_shape(arguments)
            self.sample(10, shape=shape)
            with self.subTest(profile=shape.profile):
                peak = self.estimate(shape)
                self.assertEqual(peak.bytes, min(21 * GIB, math.ceil(10 * GIB * factor)))
                expected = (gate.ReleaseHeadroom() if shape.profile == "release" else
                            gate.DevHeadroom() if shape.profile == "dev" else gate.NoHeadroom())
                basis = self.same_shape_basis(peak)
                assert isinstance(basis, gate.ShapeLargest)
                self.assertEqual(basis.factor, expected)

    def test_dev_history_gets_measured_headroom_below_the_cap(self) -> None:
        shape = build_shape(["cargo", "build", "--bin", "hana"])
        self.sample(0.5, shape=shape)
        peak = self.estimate(shape)
        self.assertEqual(peak.bytes, math.ceil((GIB // 2) * gate.DEV_HEADROOM))
        basis = self.same_shape_basis(peak)
        assert isinstance(basis, gate.ShapeLargest)
        self.assertEqual(basis.factor, gate.DevHeadroom())
        self.assertEqual(gate.read_need_basis(gate.need_basis_record(basis)), basis)

    def test_build_promises_never_exceed_21_gib(self) -> None:
        for arguments in (RELEASE_BUILD, ["cargo", "build"]):
            shape = build_shape(arguments)
            self.sample(30, shape=shape, need=30)
            with self.subTest(profile=shape.profile):
                peak = self.estimate(shape)
                self.assertEqual(peak.bytes, 21 * GIB)
                factor = 1.314 if shape.profile == "release" else gate.DEV_HEADROOM
                self.assertEqual(self.same_shape_basis(peak).cap, gate.CappedAtBudget(math.ceil(30 * GIB * factor)))

    def test_old_history_matches_a_copy_with_a_different_checkout_name(self) -> None:
        self.index_build("copied-build", RELEASE_BUILD)
        with closing(sqlite3.connect(self.root / "index.sqlite")) as connection, connection:
            _ = connection.execute("UPDATE steps SET repo = 'hana-copy'")
        self.sample(16, record_shape=False, call_id="copied-build")
        peak = self.estimate()
        self.assertEqual((peak.count, peak.bytes), (1, 21 * GIB))

    def test_old_rows_get_shape_only_from_the_call_and_step_index_join(self) -> None:
        self.index_build("joined", RELEASE_BUILD)
        self.index_build("dev", ["cargo", "build"])
        self.sample(5, record_shape=False, call_id="joined")
        for call_id in ("dev", "no-index-match", ""):
            self.sample(20, record_shape=False, call_id=call_id)
        before = (self.root / "index.sqlite").read_bytes()
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count), (math.ceil(5 * GIB * 1.314), 1))
        self.assertEqual((self.root / "index.sqlite").read_bytes(), before)

    def test_rows_without_shape_or_join_cannot_supply_even_a_profile_fallback(self) -> None:
        for _ in range(10):
            self.sample(20, record_shape=False)
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count), (12 * GIB, 0))

    def test_shape_history_excludes_other_hosts_future_and_expired_rows(self) -> None:
        self.sample(3)
        for _ in range(10):
            self.sample(20, host="other-host")
            self.sample(20, at=NOW + timedelta(seconds=1))
            self.sample(20, at=NOW - timedelta(days=14, seconds=1))
        self.assertEqual((self.estimate().bytes, self.estimate().count), (math.ceil(3 * GIB * 1.314), 1))

    def test_killed_peak_sets_a_floor_even_when_p90_is_lower(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, outcome="killed", need=16)
        peak = self.estimate()
        self.assertEqual(peak.bytes, math.ceil(int(15.76 * GIB) * 1.314))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.FailedPeakFloor(int(15.76 * GIB), store.utc_iso(HISTORY_AT.timestamp())))
        self.assertAlmostEqual(peak.bytes / GIB, 20.70864, places=6)

    def test_later_success_below_the_floor_does_not_end_it(self) -> None:
        for _ in range(9):
            self.sample(10)
        ended = NOW - timedelta(hours=1)
        self.sample(15.76, outcome="owner_gone", at=ended)
        self.sample(1, at=HISTORY_AT)
        peak = self.estimate()
        self.assertEqual(peak.bytes, math.ceil(int(15.76 * GIB) * 1.314))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.FailedPeakFloor(int(15.76 * GIB), store.utc_iso(ended.timestamp())))

    def test_completed_run_above_its_need_also_sets_the_floor(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, need=12, outcome="completed")
        peak = self.estimate()
        self.assertEqual(peak.bytes, math.ceil(int(15.76 * GIB) * 1.314))
        self.assertIsInstance(self.same_shape_basis(peak).floor, gate.FailedPeakFloor)

    def test_newest_failed_row_replaces_the_floor_in_time_order(self) -> None:
        for _ in range(20):
            self.sample(10)
        self.sample(11, outcome="killed", at=HISTORY_AT)
        # Append the older and larger failure last: file order is not time order.
        self.sample(15.76, outcome="owner_gone", at=NOW - timedelta(hours=1))
        peak = self.estimate()
        self.assertEqual(peak.bytes, math.ceil(11 * GIB * 1.314))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.FailedPeakFloor(11 * GIB, store.utc_iso(HISTORY_AT.timestamp())))

    def test_failed_floor_expires_after_fourteen_days(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, outcome="killed", at=NOW - timedelta(days=14, seconds=1))
        peak = self.estimate()
        self.assertEqual(peak.bytes, math.ceil(10 * GIB * 1.314))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.NoFloor())

    def test_old_row_with_no_outcome_never_sets_a_failed_floor(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, need=1, outcome="")
        peak = self.estimate()
        self.assertEqual(peak.bytes, math.ceil(10 * GIB * 1.314))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.NoFloor())


class BuildLifetimePromiseTests(BuildPromiseFixture):
    def test_release_build_after_ramp_keeps_need_as_its_budget_charge(self) -> None:
        path = self.build_reservation(need=16, age=gate.RAMP_SECONDS + 3600)
        row = cast(gate.Reservation, json.loads(path.read_text()))
        memory = gate.reservation_memory(path, row, NOW, gate.process_table(self.root / "proc"), self.root / "proc")
        self.assertIsInstance(memory, gate.MeasuredMemory)
        if not isinstance(memory, gate.MeasuredMemory):
            self.fail("fixture build must have a current memory reading")
        self.assertEqual(memory.promise, gate.LifetimePromise(16 * GIB))
        self.assertEqual(gate.charge_basis(memory), gate.LifetimePromise(16 * GIB))
        self.assertEqual((gate.build_charge(memory), gate.growth_headroom(memory)), (16 * GIB, 15 * GIB))

    def test_release_build_can_pass_budget_and_still_hold_for_free_growth(self) -> None:
        _ = self.build_reservation(need=16, age=gate.RAMP_SECONDS + 3600)
        result = self.check(need=4, free=20)
        self.assertEqual((result.decision.budget_passes, result.decision.free_passes, result.decision.state),
                         (True, False, "hold"))
        self.assertEqual((result.decision.charged, result.decision.promised, result.decision.threshold),
                         (16 * GIB, 15 * GIB, 22 * GIB))

    def test_release_charge_covers_current_memory_and_observed_peak_above_need(self) -> None:
        path = self.build_reservation(need=10)
        row = cast(gate.Reservation, json.loads(path.read_text()))
        for current, peak in ((18, 17), (2, 19)):
            with self.subTest(current=current, peak=peak):
                self.readings[100] = gate.MeasuredMemory(current * GIB)
                _ = path.with_suffix(".max").write_text(str(peak * GIB))
                _ = path.with_suffix(".wave").write_text(f"{peak * GIB} {int(NOW.timestamp())}")
                memory = gate.reservation_memory(path, row, NOW, gate.process_table(self.root / "proc"), self.root / "proc")
                self.assertEqual(gate.build_charge(memory), max(10, current, peak) * GIB)

    def test_dev_build_and_nextest_after_ramp_keep_current_charge(self) -> None:
        for step in ("build", "nextest"):
            path = self.build_reservation(shape=build_shape(["cargo", "build"]), need=16)
            row = cast(dict[str, object], json.loads(path.read_text()))
            row["step"] = step
            _ = path.write_text(json.dumps(row))
            memory = gate.reservation_memory(path, cast(gate.Reservation, cast(object, row)), NOW,
                                             gate.process_table(self.root / "proc"), self.root / "proc")
            with self.subTest(step=step):
                self.assertEqual(gate.charge_basis(memory), gate.UsingNow(GIB))
                self.assertEqual((gate.build_charge(memory), gate.growth_headroom(memory)), (GIB, 0))
                if not isinstance(memory, gate.MeasuredMemory):
                    self.fail("fixture step must have a current reading")
                self.assertEqual(memory.promise, gate.NoPromise())


class BuildFinalizerTests(BuildPromiseFixture):
    def test_interrupted_history_append_keeps_one_row_and_the_original_outcome(self) -> None:
        history = self.root / "admission/anon_peaks.jsonl"
        for tail in HISTORY_APPEND_STOPS:
            for cleanup in ("release", "owner gone"):
                with self.subTest(tail=tail, cleanup=cleanup):
                    path = self.build_reservation(sidecar=True)
                    row = cast(dict[str, object], json.loads(path.read_text()))
                    row["worktree"] = "hana-\u0394"
                    _ = path.write_text(json.dumps(row))
                    _ = path.with_suffix(".max").write_text(str(10 * GIB))
                    if cleanup == "owner gone":
                        _ = self.processes.pop(100, None)
                    with stop_history_append(store, tail), self.assertRaises(OSError):
                        if cleanup == "release":
                            gate.release(path, 137, self.root)
                        else:
                            _ = self.build_check(argv=["cargo", "check"])
                    self.assertTrue(path.exists(), "a cut-short row must retain its reservation")
                    # Retry through the other entry point; the saved outcome must win.
                    if cleanup == "release":
                        _ = self.processes.pop(100, None)
                        probe = self.build_check(argv=["cargo", "check"])
                        if isinstance(probe.reservation, gate.Reserved):
                            gate.release(probe.reservation.path, 0, self.root)
                    else:
                        gate.release(path, 0, self.root)
                    store.append_line(history, {"unrelated": True})
                    gate.release(path, 0, self.root)
                    contents = history.read_bytes()
                    self.assertTrue(contents.endswith(b"\n"))
                    rows = [cast(dict[str, object], json.loads(line)) for line in contents.splitlines()]
                    recorded = [item for item in rows if "reservation_id" in item]
                    self.assertEqual(len(recorded), 1)
                    self.assertEqual((recorded[0]["outcome"], recorded[0]["anon_peak_bytes"],
                                      recorded[0]["worktree"]),
                                     ("killed" if cleanup == "release" else "owner_gone", 10 * GIB, "hana-\u0394"))
                    self.assertEqual(rows[-1], {"unrelated": True})
                    self.assertFalse(path.exists())
                    history.unlink()
                    self.processes[100] = "first"

    @contextlib.contextmanager
    def stop_before_reservation_removal(self, path: Path) -> Generator[None, None, None]:
        unlink = Path.unlink

        def unlink_except_reservation(file: Path, missing_ok: bool = False) -> None:
            if file == path:
                raise InterruptedError("stop after recording history")
            unlink(file, missing_ok=missing_ok)

        with patch.object(Path, "unlink", unlink_except_reservation), contextlib.suppress(InterruptedError):
            yield

    def test_cleanup_retry_after_history_append_counts_the_build_once(self) -> None:
        path = self.build_reservation(sidecar=True)
        _ = path.with_suffix(".max").write_text(str(10 * GIB))
        with self.stop_before_reservation_removal(path):
            gate.release(path, 137, self.root)
        self.assertTrue(path.exists())
        gate.release(path, 137, self.root)
        rows = (self.root / "admission/anon_peaks.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 1, "a retry must not change the number of history runs")
        self.assertEqual(json.loads(rows[0])["outcome"], "killed")

    def test_different_builds_in_one_call_each_keep_their_history(self) -> None:
        with patch.dict(os.environ, {"BUILDLOG_CALL_ID": "one-verify-call"}):
            for pid in (100, 200):
                path = self.build_reservation(pid=pid, sidecar=True)
                row = cast(dict[str, object], json.loads(path.read_text()))
                row["call_id"] = "one-verify-call"
                _ = path.write_text(json.dumps(row))
                _ = path.with_suffix(".max").write_text(str(pid * GIB))
                gate.release(path, 0, self.root)
        rows = [cast(dict[str, object], json.loads(line)) for line in
                (self.root / "admission/anon_peaks.jsonl").read_text().splitlines()]
        self.assertCountEqual([row["anon_peak_bytes"] for row in rows], [100 * GIB, 200 * GIB])

    def test_append_retry_keeps_scope_evidence_after_the_sidecar_is_removed(self) -> None:
        sidecar = self.root / "new-build.cgroup"
        _ = sidecar.write_text(str(self.root / "new-build.scope"))
        result = self.build_check(sidecar=str(sidecar))
        if not isinstance(result.reservation, gate.Reserved):
            self.fail("fixture build must be admitted")
        path = result.reservation.path
        _ = path.with_suffix(".max").write_text(str(int(15.76 * GIB)))
        with patch.object(store, "append_locked", side_effect=OSError("history is temporarily unavailable")), \
             contextlib.suppress(OSError):
            gate.release(path, 137, self.root)
        self.assertTrue(path.exists())
        sidecar.unlink(missing_ok=True)
        del self.processes[400]
        _ = self.build_check(argv=["cargo", "check"])
        history = self.root / "admission/anon_peaks.jsonl"
        self.assertTrue(history.exists(), "retry must retain the fact that this build ran in a scope")
        row = cast(dict[str, object], json.loads(history.read_text()))
        self.assertEqual((row["outcome"], row["anon_peak_bytes"]), ("killed", int(15.76 * GIB)))
        self.assertFalse(path.exists())

    def test_peak_read_error_preserves_all_files_for_the_next_cleanup(self) -> None:
        path = self.build_reservation(sidecar=True)
        maximum = path.with_suffix(".max")
        _ = maximum.write_text(str(int(15.76 * GIB)))
        _ = path.with_suffix(".wave").write_text("peak wave")
        files = (path, maximum, path.with_suffix(".wave"), self.root / "100.cgroup")
        before = {file: file.read_bytes() for file in files}
        read_text = Path.read_text

        def fail_peak_read(file: Path) -> str:
            if file == maximum:
                raise PermissionError("peak temporarily unreadable")
            return read_text(file)

        with patch.object(Path, "read_text", fail_peak_read), contextlib.suppress(OSError):
            gate.release(path, 137, self.root)
        self.assertEqual({file: file.read_bytes() for file in files if file.exists()}, before)
        gate.release(path, 137, self.root)
        row = cast(dict[str, object], json.loads((self.root / "admission/anon_peaks.jsonl").read_text()))
        self.assertEqual((row["anon_peak_bytes"], row["outcome"]), (int(15.76 * GIB), "killed"))

    def test_cleanup_finishes_after_a_stop_following_each_file_removal(self) -> None:
        unlink = Path.unlink
        for removed_name in ("100.reservation", "100.max", "100.wave", "100.cgroup"):
            with self.subTest(removed=removed_name):
                path = self.build_reservation(sidecar=True)
                _ = path.with_suffix(".max").write_text(str(10 * GIB))
                _ = path.with_suffix(".wave").write_text("peak wave")
                files = (path, path.with_suffix(".max"), path.with_suffix(".wave"), self.root / "100.cgroup")

                def stop_after_removal(file: Path, missing_ok: bool = False) -> None:
                    existed = file.exists()
                    unlink(file, missing_ok=missing_ok)
                    if existed and file.name == removed_name:
                        raise InterruptedError("stop just after removing a file")

                with patch.object(Path, "unlink", stop_after_removal), contextlib.suppress(InterruptedError):
                    gate.release(path, 137, self.root)
                gate.release(path, 137, self.root)
                self.assertEqual([str(file) for file in files if file.exists()], [])
                history = self.root / "admission/anon_peaks.jsonl"
                self.assertEqual(len(history.read_text().splitlines()), 1)
                history.unlink()

    def test_cleanup_added_floor_expires_the_old_estimate_without_losing_queue_place(self) -> None:
        for _ in range(9):
            self.sample(10)
        background = self.running(200, 0.1, 3600)
        self.readings[200] = gate.MeasuredMemory(GIB // 10)
        path = self.build_reservation(need=12, sidecar=True)
        _ = path.with_suffix(".max").write_text(str(int(15.76 * GIB)))
        del self.processes[100]
        directory = self.root / "admission"
        waiter_path = directory / "400-warmup.waiting"
        previous = {"pid": 400, "start_time": "warmup", "need": math.ceil(10 * GIB * 1.314),
                    "since": NOW.timestamp() - 900, "label": "release build", "bump_order": -2,
                    "holds_younger": True, "priority_since": NOW.timestamp() - 60}
        _ = waiter_path.write_text(json.dumps(previous))
        recovery = gate.RecoveryStarts(NOW.timestamp() + 120, NOW.timestamp())
        gate.write_recovery(directory, recovery)
        first = self.build_check(available=18)
        self.assertEqual(first.decision.state, "hold", "cleanup made the pre-lock estimate stale")
        self.assertEqual(first.reservation, gate.NotReserved("held"))
        self.assertEqual(gate.read_recovery(directory), recovery)
        row = cast(dict[str, object], json.loads(waiter_path.read_text()))
        for field in ("since", "bump_order", "holds_younger", "priority_since"):
            self.assertEqual(row[field], previous[field])
        second = gate.check(self.meminfo, 400, "", RELEASE_BUILD, root=self.root,
                            now=NOW + timedelta(seconds=1), profile=first.profile)
        self.assertEqual(second.decision.state, "hold")
        self.assertEqual(second.peak.bytes, math.ceil(int(15.76 * GIB) * 1.314))
        self.assertCountEqual(list(directory.glob("*.reservation")), [background])

    def test_shared_reservation_reader_rejects_missing_required_fields(self) -> None:
        for reader in ("live", "check"):
            for missing in ("all", "start_time"):
                with self.subTest(reader=reader, missing=missing):
                    path = self.build_reservation()
                    row = cast(dict[str, object], json.loads(path.read_text()))
                    if missing == "all":
                        row.clear()
                    else:
                        del row["start_time"]
                    _ = path.write_text(json.dumps(row))
                    if reader == "live":
                        self.assertEqual(gate.live_reservations(path.parent), [])
                    else:
                        result = self.build_check(argv=["cargo", "check"])
                        self.assertEqual(result.decision.state, "admit")
                        if isinstance(result.reservation, gate.Reserved):
                            gate.release(result.reservation.path, 0, self.root)

    def test_malformed_finalization_intents_are_ignored_by_cleanup_and_release(self) -> None:
        valid: dict[str, object] = {
            "reservation_id": "ended-build", "peak": int(15.76 * GIB), "scoped": True,
            "outcome": "killed", "ended_at": store.utc_iso(NOW.timestamp()), "status": 137,
        }
        malformed: list[object] = [None, {}]
        for field in ("reservation_id", "peak", "scoped", "outcome", "ended_at"):
            malformed.append({key: value for key, value in valid.items() if key != field})
        for field, value in (("reservation_id", 1), ("peak", "large"), ("peak", True),
                             ("scoped", 1), ("outcome", "unknown"), ("ended_at", None),
                             ("status", "137"), ("status", True)):
            malformed.append({**valid, field: value})
        for caller in ("live", "check", "release"):
            for intent in malformed:
                with self.subTest(caller=caller, intent=intent):
                    self.processes[100] = "first"
                    path = self.build_reservation(sidecar=True)
                    row = cast(dict[str, object], json.loads(path.read_text()))
                    row["finalization"] = intent
                    _ = path.write_text(json.dumps(row))
                    maximum = path.with_suffix(".max")
                    _ = maximum.write_text(str(int(15.76 * GIB)))
                    files = (path, maximum, self.root / "100.cgroup")
                    before = {file: file.read_bytes() for file in files}
                    if caller == "live":
                        self.assertEqual(gate.live_reservations(path.parent), [])
                    elif caller == "check":
                        del self.processes[100]
                        result = self.build_check(argv=["cargo", "check"])
                        self.assertEqual(result.decision.state, "admit")
                        if isinstance(result.reservation, gate.Reserved):
                            gate.release(result.reservation.path, 0, self.root)
                    else:
                        gate.release(path, 137, self.root)
                    self.assertEqual({file: file.read_bytes() for file in files if file.exists()}, before)
                    self.assertFalse((path.parent / "anon_peaks.jsonl").exists())

    def test_cleanup_of_an_old_joined_shape_also_expires_the_estimate(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.index_build("old-reservation", RELEASE_BUILD)
        path = self.build_reservation(need=12, sidecar=True)
        row = cast(dict[str, object], json.loads(path.read_text()))
        del row["shape"]
        row["call_id"] = "old-reservation"
        _ = path.write_text(json.dumps(row))
        _ = path.with_suffix(".max").write_text(str(int(15.76 * GIB)))
        del self.processes[100]
        result = self.build_check(available=18)
        self.assertEqual(result.decision.state, "hold", "the history join gives this ended build the waiter's shape")
        self.assertEqual(result.reservation, gate.NotReserved("held"))

    def test_cleanup_retry_cannot_admit_a_cached_estimate_from_before_the_append(self) -> None:
        for _ in range(9):
            self.sample(10)
        profile = gate.StepProfile("hana", "build", "hana-clone", self.estimate(), HANA,
                                   estimated_at=NOW.timestamp())
        path = self.build_reservation(need=12, sidecar=True)
        _ = path.with_suffix(".max").write_text(str(int(15.76 * GIB)))
        del self.processes[100]
        _ = self.meminfo.write_text(f"MemAvailable: {18 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        with self.stop_before_reservation_removal(path):
            _ = gate.check(self.meminfo, 400, "", RELEASE_BUILD, root=self.root, now=NOW, profile=profile)
        self.assertTrue(path.exists())
        result = gate.check(self.meminfo, 400, "", RELEASE_BUILD, root=self.root,
                            now=NOW + timedelta(seconds=1), profile=profile)
        self.assertEqual(result.decision.state, "hold", "a journaled floor still invalidates the cached estimate")
        self.assertEqual(result.reservation, gate.NotReserved("held"))

    def test_check_finalizes_ended_owner_and_next_estimate_uses_the_evidence(self) -> None:
        path = self.build_reservation(need=12, sidecar=True)
        _ = path.with_suffix(".max").write_text(str(int(15.76 * GIB)))
        _ = path.with_suffix(".wave").write_text(f"{int(15.76 * GIB)} {int(NOW.timestamp())}")
        del self.processes[100]
        with patch.object(gate, "release", side_effect=AssertionError("check must not take the release lock")), \
             patch.object(fcntl, "flock", wraps=fcntl.flock) as locks:
            _ = self.build_check(argv=["cargo", "check"])
        self.assertEqual([call.args[1] for call in locks.call_args_list
                          if cast(str, getattr(cast(object, call.args[0]), "name", ""))
                          == str(self.root / "admission/lock")], [fcntl.LOCK_EX])
        history = gate.admission_dir(self.root) / "anon_peaks.jsonl"
        self.assertTrue(history.exists(), "check must preserve an ended owner's measured peak")
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["outcome"], rows[0]["need"], rows[0]["shape"], rows[0]["anon_peak_bytes"]),
                         ("owner_gone", 12 * GIB, RELEASE_SHAPE.key, int(15.76 * GIB)))
        for removed in (path, path.with_suffix(".max"), path.with_suffix(".wave"), self.root / "100.cgroup"):
            self.assertFalse(removed.exists(), str(removed))
        peak = self.estimate(now=NOW + timedelta(seconds=1))
        self.assertEqual(peak.bytes, math.ceil(int(15.76 * GIB) * 1.314))
        self.assertIsInstance(self.same_shape_basis(peak).floor, gate.FailedPeakFloor)

    def test_gone_memory_cleanup_uses_the_same_finalizer(self) -> None:
        path = self.build_reservation(need=10, sidecar=True)
        _ = path.with_suffix(".max").write_text(str(14 * GIB))
        self.readings[100] = gate.GoneMemory()
        _ = self.build_check(argv=["cargo", "check"])
        history = gate.admission_dir(self.root) / "anon_peaks.jsonl"
        self.assertTrue(history.exists(), "GoneMemory cleanup must preserve the peak")
        row = cast(dict[str, object], json.loads(history.read_text()))
        self.assertEqual((row["outcome"], row["shape"], row["need"]), ("owner_gone", RELEASE_SHAPE.key, 10 * GIB))
        self.assertFalse(path.exists())

    def test_release_records_shape_need_and_each_closed_outcome_once(self) -> None:
        for status, outcome in ((0, "completed"), (1, "failed"), (127, "failed"),
                                (128, "killed"), (137, "killed")):
            with self.subTest(status=status):
                path = self.build_reservation(need=12, sidecar=True)
                _ = path.with_suffix(".max").write_text(str(10 * GIB))
                _ = path.with_suffix(".wave").write_text(f"{10 * GIB} {int(NOW.timestamp())}")
                with patch.object(fcntl, "flock", wraps=fcntl.flock) as locks:
                    gate.release(path, status, self.root)
                self.assertEqual([call.args[1] for call in locks.call_args_list
                                  if cast(str, getattr(cast(object, call.args[0]), "name", ""))
                                  == str(self.root / "admission/lock")], [fcntl.LOCK_EX])
                gate.release(path, status, self.root)
                rows = [cast(dict[str, object], json.loads(line)) for line in
                        (self.root / "admission/anon_peaks.jsonl").read_text().splitlines()]
                self.assertEqual((rows[-1]["status"], rows[-1]["outcome"], rows[-1]["shape"], rows[-1]["need"]),
                                 (status, outcome, RELEASE_SHAPE.key, 12 * GIB))
                self.assertEqual(len(rows), (0, 1, 127, 128, 137).index(status) + 1)
                for removed in (path, path.with_suffix(".max"), path.with_suffix(".wave"), self.root / "100.cgroup"):
                    self.assertFalse(removed.exists(), str(removed))

    def test_live_reservations_skips_ended_rows_and_deletes_nothing(self) -> None:
        path = self.build_reservation(sidecar=True)
        _ = path.with_suffix(".max").write_text(str(GIB))
        _ = path.with_suffix(".wave").write_text(f"{GIB} 1")
        del self.processes[100]
        directory = gate.admission_dir(self.root)
        _ = (directory / "broken.reservation").write_text("not json")
        paths = [*directory.iterdir(), self.root / "100.cgroup"]
        before = {file: file.read_bytes() for file in paths}
        self.assertEqual(gate.live_reservations(directory), [])
        self.assertEqual({file: file.read_bytes() for file in paths if file.exists()}, before)
        self.assertFalse((directory / "anon_peaks.jsonl").exists())

    def test_check_records_shape_in_the_admitted_reservation(self) -> None:
        result = self.build_check()
        self.assertIsInstance(result.reservation, gate.Reserved)
        if not isinstance(result.reservation, gate.Reserved):
            self.fail("fixture must admit its single release build")
        row = cast(dict[str, object], json.loads(result.reservation.path.read_text()))
        self.assertEqual(row["shape"], RELEASE_SHAPE.key)
        self.assertEqual(row["need"], 12 * GIB)


class BuildNeedBasisTests(BuildPromiseFixture):
    def test_fallback_basis_rejects_records_with_a_floor_or_a_factor(self) -> None:
        for floor, factor in (
            ({"kind": "failed_peak", "peak": 16 * GIB, "ended_at": "2026-10-05T12:00:00Z"},
             {"kind": "none"}),
            ({"kind": "none"}, {"kind": "release", "factor": 1.314}),
            ({"kind": "none"}, {"kind": "dev", "factor": gate.DEV_HEADROOM}),
        ):
            with self.subTest(floor=floor, factor=factor):
                self.assertEqual(gate.read_need_basis({
                    "kind": "shape_fallback", "runs": 0, "cap": {"kind": "none"},
                    "floor": floor, "factor": factor,
                }), gate.BasisNotRecorded())

    def test_every_shape_tier_round_trips_with_floor_factor_and_cap(self) -> None:
        floor = gate.FailedPeakFloor(16 * GIB, "2026-10-05T12:00:00Z")
        for basis in (gate.ShapeP90(10, floor, gate.ReleaseHeadroom(), gate.CappedAtBudget(24 * GIB)),
                      gate.ShapeLargest(2, floor, gate.ReleaseHeadroom(), gate.UnderBudget()),
                      gate.ShapeFallback(gate.CappedAtBudget(24 * GIB))):
            with self.subTest(basis=basis):
                peak = gate.ExpectedPeak(GIB, "shape", 1, basis)
                self.assertEqual(gate.read_need_basis(gate.need_basis_record(peak.basis)), basis)

    def test_shape_estimate_refuses_a_missing_shape_basis(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            _ = gate.ExpectedPeak(GIB, "shape", 1)

    def test_waiter_records_tagged_basis_and_queue_reads_the_same_basis(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, outcome="killed")
        result = self.build_check(available=0)
        self.assertEqual(result.decision.state, "hold")
        path = self.root / "admission/400-warmup.waiting"
        row = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(row["need_source"], "shape")
        self.assertIsInstance(row["need_basis"], dict)
        serialized = json.dumps(row["need_basis"])
        for value in ("1.314", str(int(15.76 * GIB)), HISTORY_AT.strftime("%Y-%m-%d")):
            self.assertIn(value, serialized)
        before = path.read_bytes()
        explanation = gate.explain_queue()
        if not isinstance(explanation.head, gate.HeadExplanation):
            self.fail("fixture must have a queue head")
        source = explanation.head.need_source
        if not isinstance(source, gate.RecordedNeed):
            self.fail("waiter must carry its need basis")
        self.assertEqual(source.basis, result.peak.basis)
        self.assertEqual(path.read_bytes(), before)

    def test_old_queue_row_has_basis_not_recorded(self) -> None:
        _ = self.build_check(available=0)
        path = self.root / "admission/400-warmup.waiting"
        row = cast(dict[str, object], json.loads(path.read_text()))
        _ = row.pop("need_basis", None)
        _ = path.write_text(json.dumps(row))
        head = gate.explain_queue().head
        if not isinstance(head, gate.HeadExplanation) or not isinstance(head.need_source, gate.RecordedNeed):
            self.fail("fixture must have a head with a recorded need")
        self.assertEqual(head.need_source.basis, gate.BasisNotRecorded())

    def test_cached_cli_rebuilds_basis_from_waiter_and_refreshes_when_absent(self) -> None:
        self.sample(10)
        _ = self.build_check(available=0)
        path = self.root / "admission/400-warmup.waiting"
        first = cast(dict[str, object], json.loads(path.read_text()))
        arguments = ["memory_admit.py", "check", str(self.meminfo), "400", "", "0",
                     "hana", "build", "hana-clone", str(first["need"]), "shape", "1", *RELEASE_BUILD]
        for remove_basis in (False, True):
            with self.subTest(remove_basis=remove_basis):
                row = dict(first)
                if remove_basis:
                    _ = row.pop("need_basis", None)
                _ = path.write_text(json.dumps(row))
                output = io.StringIO()
                with patch.object(gate, "expected_peak", wraps=gate.expected_peak) as history, \
                     patch.object(gate, "datetime", wraps=datetime) as clock, \
                     patch.object(sys, "argv", arguments), redirect_stdout(output):
                    clock.configure_mock(**{"now.return_value": NOW})
                    gate.run_action()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(fields[3], "shape")
                self.assertEqual(history.call_count, int(remove_basis))
                refreshed = cast(dict[str, object], json.loads(path.read_text()))
                self.assertEqual(refreshed["need_basis"], first["need_basis"])


class GateProfileTests(MemoryQueueFixture):
    def test_unfinished_rows_do_not_change_the_gate_estimate(self) -> None:
        history = gate.admission_dir(self.root) / "anon_peaks.jsonl"
        _ = history.write_text(json.dumps({
            "step": "merge-gate", "host": "host", "anon_peak_bytes": 3 * GIB,
            "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
        }) + "\n")
        assert_unfinished_history_ignored(
            self, history, lambda: gate.gate_estimate(4 * GIB, "host", NOW, self.root))

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root))))

    def test_gate_without_history_uses_its_cap_and_keeps_its_queue_identity(self) -> None:
        estimate = gate.gate_estimate(4 * GIB, "host", NOW, self.root)
        self.assertEqual(estimate, gate.NoGateHistory(4 * GIB))
        profile = gate.GateProfile("installed/.claude", "checkout", 4 * GIB, estimate)
        _ = self.running(100, 20, age=1)
        with patch.object(gate, "git_identity", side_effect=AssertionError("gate inferred cargo identity")):
            result = gate.check(self.meminfo, 400, "", ["merge-gate"], root=self.root, now=NOW, profile=profile)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual((result.repo, result.step, result.peak.bytes), ("installed/.claude", "merge-gate", 4 * GIB))
        waiter, = gate.admission_dir(self.root).glob("*.waiting")
        row = cast(gate.Waiter, json.loads(waiter.read_text()))
        self.assertEqual((row.get("step"), row.get("folder"), row.get("need_source")),
                         ("merge-gate", "checkout", "fallback"))

    def test_gate_history_uses_only_recent_gate_peaks_on_this_host(self) -> None:
        history = gate.admission_dir(self.root) / "anon_peaks.jsonl"
        rows = [{"step": "merge-gate", "host": "host", "anon_peak_bytes": value * GIB,
                 "ended_at": store.utc_iso((NOW - timedelta(hours=1)).timestamp())}
                for value in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)]
        rows.extend([
            {"step": "nextest", "host": "host", "anon_peak_bytes": 100 * GIB, "ended_at": store.utc_iso(HISTORY_AT.timestamp())},
            {"step": "merge-gate", "host": "other", "anon_peak_bytes": 100 * GIB, "ended_at": store.utc_iso(HISTORY_AT.timestamp())},
            {"step": "merge-gate", "host": "host", "anon_peak_bytes": 100 * GIB,
             "ended_at": store.utc_iso((NOW - timedelta(days=gate.HISTORY_DAYS + 1)).timestamp())},
        ])
        _ = history.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertEqual(gate.gate_estimate(4 * GIB, "host", NOW, self.root), gate.MeasuredGatePeak(9 * GIB, 10))

    def test_gate_queue_view_keeps_checkout_when_launch_directory_differs(self) -> None:
        checkout, launch = self.root / "gate-checkout", self.root / "launch"
        checkout.mkdir()
        launch.mkdir()
        owner = self.root / "proc/400"
        owner.mkdir(parents=True)
        (owner / "cwd").symlink_to(launch)
        self.readings[400] = gate.MeasuredMemory(GIB)
        busy = self.running(100, 20, age=1)
        profile = gate.GateProfile("installed/.claude", str(checkout), 4 * GIB, gate.NoGateHistory(4 * GIB))
        for state in ("hold", "admit"):
            with self.subTest(state=state):
                if state == "admit":
                    busy.unlink()
                result = gate.check(self.meminfo, 400, "", ["merge-gate"], root=self.root, now=NOW, profile=profile)
                self.assertEqual(result.decision.state, state)
                view, _ = gate.read_queue(gate.admission_dir(self.root), gate.process_table(self.root / "proc"), NOW.timestamp())
                rows = view.waiting if state == "hold" else view.running
                visible, = (row for row in rows if row.step == "merge-gate")
                self.assertEqual(visible.folder, str(checkout))

    def test_expired_gate_profile_refreshes_outside_lock_without_cargo_inference(self) -> None:
        profile = gate.GateProfile("installed/.claude", "checkout", 4 * GIB,
                                   gate.NoGateHistory(4 * GIB), NOW.timestamp() - gate.HEAD_SECONDS)
        locked = False
        acquire = fcntl.flock

        def lock(file: TextIO, operation: int) -> None:
            nonlocal locked
            acquire(file, operation)
            locked = True

        def estimate(cap: int, host: str, now: datetime, root: Path) -> gate.GateEstimate:
            self.assertFalse(locked)
            self.assertEqual((cap, now, root), (4 * GIB, NOW, self.root))
            self.assertTrue(host)
            return gate.MeasuredGatePeak(6 * GIB, 2)

        with patch.object(gate, "gate_estimate", side_effect=estimate) as refreshed, \
                patch.object(gate, "git_identity", side_effect=AssertionError("gate inferred cargo identity")), \
                patch.object(fcntl, "flock", side_effect=lock):
            result = gate.check(self.meminfo, 400, "", ["merge-gate"], root=self.root, now=NOW, profile=profile)
        refreshed.assert_called_once()
        self.assertEqual((result.repo, result.step, result.profile.worktree, result.peak.bytes),
                         ("installed/.claude", "merge-gate", "checkout", 6 * GIB))
        self.assertEqual(result.profile.estimated_at, NOW.timestamp())
        self.assertIsInstance(result.reservation, gate.Reserved)
        if isinstance(result.reservation, gate.Reserved):
            row = cast(gate.Reservation, json.loads(result.reservation.path.read_text()))
            self.assertEqual((row["repo"], row["step"], row["need"]), ("installed/.claude", "merge-gate", 6 * GIB))

    def test_expired_measured_gate_profile_retains_its_original_fallback_cap(self) -> None:
        profile = gate.GateProfile("installed/.claude", "checkout", 4 * GIB,
                                   gate.MeasuredGatePeak(GIB, 1), NOW.timestamp() - gate.HEAD_SECONDS)
        with patch.object(gate, "git_identity", side_effect=AssertionError("gate inferred cargo identity")):
            result = gate.check(self.meminfo, 400, "", ["merge-gate"], root=self.root, now=NOW, profile=profile)
        self.assertEqual(result.peak, gate.ExpectedPeak(4 * GIB, "fallback", 0))
        self.assertEqual(cast(gate.GateProfile, result.profile).estimate, gate.NoGateHistory(4 * GIB))
        self.assertEqual((result.repo, result.step), ("installed/.claude", "merge-gate"))


class AdmitHeadTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        environment = fake_memory_readings(self.root)
        environment.update({"BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_BUILD_BUDGET_GIB": "21",
                            "CLAUDE_CODE_SESSION_ID": "showrunner-id", "CODEX_THREAD_ID": ""})
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        # One boundary replaces every real showrunner-state lookup.
        _ = self.enterContext(patch.object(gate, "showrunner_is_registered", return_value=True, create=True))
        _ = self.meminfo.write_text(f"MemAvailable: {23 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    @property
    def override_path(self) -> Path:
        return self.root / "admission/admit-head.json"

    def waiting_head(self) -> Path:
        path = self.head(12)
        row = cast(gate.Waiter, json.loads(path.read_text()))
        row.update(session_id="head-session", session_name="head-unit", step="nextest", folder=str(self.root / "tree"))
        _ = path.write_text(json.dumps(row))
        return path

    def set_head(self, why: str = "") -> str:
        output = io.StringIO()
        arguments = ["memory_admit.py", "admit-head"] + (["--why", why] if why else [])
        with patch.object(sys, "argv", arguments), redirect_stdout(output):
            gate.main()
        return output.getvalue()

    def events(self) -> list[dict[str, object]]:
        return [row for row in buildlog_rows(self.root) if row.get("kind") == "head_admission"]

    def bump_younger_waiter(self) -> Path:
        younger = self.root / "admission/400-warmup.waiting"
        row = cast(gate.Waiter, json.loads(younger.read_text()))
        row["bump_order"] = -1
        _ = younger.write_text(json.dumps(row))
        return younger

    def test_admit_head_publishes_override_and_snapshot_keeps_budget_and_pressure_outcomes(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {60 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.running(100, 15, age=0)
        _ = self.waiting_head()
        self.assertIn("head-unit", self.set_head("head verified"))
        path = self.root / "admission/queue.json"
        self.assertTrue(path.exists())
        baseline = gate.locked_queue_read()
        self.assertEqual(gate.queue_read(), baseline)
        head = baseline.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        self.assertFalse(head.decision.budget_passes)
        self.assertEqual(head.decision.state, "admit")
        self.assertIsInstance(baseline.explanation.override, dict)
        _ = (self.root / "pressure").write_text("full avg10=11.00 avg60=0.00 avg300=0.00 total=0\n")
        directory = gate.admission_dir(self.root)
        gate.write_queue_snapshot(directory, gate.gather_queue_inputs(
            directory, gate.process_table(self.root / "proc"), NOW.timestamp()))
        held = gate.locked_queue_read()
        self.assertEqual(gate.queue_read(), held)
        assert isinstance(held.explanation.head, gate.HeadExplanation)
        self.assertEqual(held.explanation.head.decision.state, "hold")
        self.assertIsInstance(held.explanation.head.decision.pressure, gate.MemoryPressure)

    def test_showrunner_sets_one_record_for_the_current_head(self) -> None:
        path = self.waiting_head()
        self.assertIn("head-unit", self.set_head("free memory checked"))
        row = cast(dict[str, object], json.loads(self.override_path.read_text()))
        self.assertEqual((row["waiter"], row["pid"], row["start_time"]), (path.name, 300, "head"))
        self.assertEqual((row["why"], row["set_by"]), ("free memory checked", "showrunner-id"))

    def test_non_showrunner_is_refused_without_writing_a_record(self) -> None:
        _ = self.waiting_head()
        for session in ("worker-id", ""):
            with self.subTest(session=session), patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session}), \
                 patch.object(gate, "showrunner_is_registered", return_value=False), \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as stopped:
                    _ = self.set_head()
                self.assertNotEqual(stopped.exception.code, 0)
                self.assertIn("showrunner", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertFalse(self.override_path.exists())
                self.assertEqual(self.events(), [])

    def test_codex_caller_cannot_use_an_inherited_showrunner_session(self) -> None:
        _ = self.waiting_head()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "worker-thread"}), \
             patch.object(gate, "showrunner_is_registered", return_value=True) as lookup, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as stopped:
                _ = self.set_head()
        self.assertNotEqual(stopped.exception.code, 0)
        self.assertIn("showrunner", errors.getvalue())
        lookup.assert_not_called()
        self.assertFalse(self.override_path.exists())
        self.assertEqual(self.events(), [])

    def test_nothing_waiting_is_refused_without_a_record_or_log_row(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as stopped:
                _ = self.set_head()
        self.assertNotEqual(stopped.exception.code, 0)
        self.assertIn("nothing waits", errors.getvalue())
        self.assertFalse(self.override_path.exists())
        self.assertEqual(self.events(), [])

    def test_new_call_replaces_the_previous_record(self) -> None:
        _ = self.waiting_head()
        _ = self.set_head("first")
        _ = self.set_head("replacement")
        row = cast(dict[str, object], json.loads(self.override_path.read_text()))
        self.assertEqual(row["why"], "replacement")
        self.assertEqual(len(list(self.override_path.parent.glob("admit-head*"))), 1)
        self.assertEqual([row["action"] for row in self.events()], ["set", "set"])

    def test_record_selects_the_first_waiter_in_queue_order(self) -> None:
        _ = self.waiting_head()
        _ = self.check(need=12, free=0)
        younger = self.bump_younger_waiter()
        _ = self.set_head()
        self.assertEqual(json.loads(self.override_path.read_text())["waiter"], younger.name)

    def test_head_starts_above_budget_when_free_memory_covers_need_and_reserve(self) -> None:
        _ = self.running(100, 20, age=30)
        _ = self.waiting_head()
        self.assertEqual(self.check(pid=300, need=12, free=15).decision.state, "hold")
        _ = self.set_head()
        with patch.object(gate, "read_meminfo", wraps=gate.read_meminfo) as reading:
            result = self.check(pid=300, need=12, free=15)
        self.assertEqual(reading.call_count, 1)
        self.assertEqual(result.decision.state, "admit")
        self.assertGreater(result.decision.charged + result.peak.bytes, result.decision.budget)
        self.assertEqual(result.decision.budget, 21 * GIB)
        self.assertIsInstance(result.reservation, gate.Reserved)

    def test_head_keeps_waiting_and_keeps_record_when_free_memory_does_not_cover_reserve(self) -> None:
        _ = self.running(100, 20, age=30)
        _ = self.waiting_head()
        _ = self.set_head()
        result = self.check(pid=300, need=12, free=14)
        self.assertEqual(result.decision.state, "hold")
        self.assertTrue(self.override_path.exists())
        self.assertEqual([row["action"] for row in self.events()], ["set"])

    def test_admission_deletes_record_and_writes_both_buildlog_rows(self) -> None:
        _ = self.running(100, 20, age=30)
        waiter = self.waiting_head()
        _ = self.set_head("user asked")
        result = self.check(pid=300, need=12, free=23)
        self.assertEqual(result.decision.state, "admit")
        self.assertFalse(waiter.exists())
        self.assertFalse(self.override_path.exists())
        rows = self.events()
        self.assertEqual([row["action"] for row in rows], ["set", "admitted"])
        for row in rows:
            self.assertEqual((row["unit"], row["step"], row["need"], row["free"], row["reserve"], row["why"], row["set_by"]),
                             ("head-unit", "nextest", 12 * GIB, 23 * GIB, 3 * GIB, "user asked", "showrunner-id"))
            self.assertEqual(row["v"], 1)
            self.assertTrue(row["id"])
            self.assertEqual(row["host"], store.host_name())
            self.assertEqual(row["at"], store.utc_iso(NOW.timestamp()))

    def test_record_lapses_on_next_locked_pass_when_waiter_leaves(self) -> None:
        _ = self.running(100, 20, age=30)
        waiter = self.waiting_head()
        _ = self.set_head()
        waiter.unlink()
        result = self.check(need=12, free=23)
        self.assertEqual(result.decision.state, "hold")
        self.assertFalse(self.override_path.exists())
        self.assertEqual([row["action"] for row in self.events()], ["set", "lapsed"])

    def test_record_for_a_dead_or_reused_waiter_never_admits_the_new_head(self) -> None:
        _ = self.running(100, 20, age=30)
        _ = self.waiting_head()
        _ = self.set_head()
        self.processes[300] = "new-process"
        result = self.check(pid=300, need=12, free=23)
        self.assertEqual(result.decision.state, "hold")
        self.assertFalse(self.override_path.exists())
        self.assertEqual([row["action"] for row in self.events()], ["set", "lapsed"])

    def test_override_does_not_follow_a_bump_or_admit_another_waiter(self) -> None:
        _ = self.running(100, 20, age=30)
        _ = self.waiting_head()
        _ = self.set_head()
        _ = self.check(need=12, free=0)
        _ = self.bump_younger_waiter()
        self.assertEqual(self.check(need=12, free=23).decision.state, "hold")
        self.assertEqual(self.check(pid=300, need=12, free=23).decision.state, "hold")
        self.assertTrue(self.override_path.exists())
        self.assertEqual([row["action"] for row in self.events()], ["set"])

    def test_override_keeps_the_pressure_guard(self) -> None:
        _ = self.running(100, 20, age=30)
        _ = self.waiting_head()
        _ = self.set_head()
        _ = Path(os.environ["BUILDLOG_MEMORY_PRESSURE"]).write_text("full avg10=11.0 total=0\n")
        self.assertEqual(self.check(pid=300, need=12, free=40).decision.state, "hold")
        self.assertTrue(self.override_path.exists())

    def test_override_snapshot_agrees_in_every_reader_and_never_transfers(self) -> None:
        _ = self.running(100, 20, age=30)
        waiter = self.waiting_head()
        _ = self.set_head()
        _ = self.waiter(400, NOW.timestamp() - 60, "/tmp/next", "next", 12)
        before = {path: path.read_bytes() for path in gate.admission_dir(self.root).iterdir()}
        view = gate.queue_view()
        reason = view.waiting[0].reason
        self.assertIsInstance(reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, reason).decision.state, "admit")
        explanation = gate.explain_queue()
        self.assertEqual(cast(gate.HeadExplanation, explanation.head).decision,
                         cast(gate.HeadAdmissionStatus, reason).decision)
        self.assertIsInstance(gate.current_blocker(waiter), gate.NoSingleBlocker)
        release = gate.smallest_head_release(explanation)
        self.assertIsInstance(release, gate.HeadAdmitsAfterEnd)
        if isinstance(release, gate.HeadAdmitsAfterEnd):
            self.assertEqual(release.ended, ())
        self.assertEqual({path: path.read_bytes() for path in gate.admission_dir(self.root).iterdir()}, before)
        result = self.check(pid=300, need=12, free=23)
        self.assertEqual(result.decision, cast(gate.HeadAdmissionStatus, reason).decision)
        self.readings[300] = gate.MeasuredMemory(0)
        self.assertEqual(self.check(pid=400, need=12, free=23).decision.state, "hold")
        self.assertFalse(self.override_path.exists())

    def test_override_pressure_hold_agrees_with_frozen_release_search(self) -> None:
        _ = self.running(100, 20, age=30)
        waiter = self.waiting_head()
        _ = self.set_head()
        _ = Path(os.environ["BUILDLOG_MEMORY_PRESSURE"]).write_text("full avg10=6 total=0\n")
        view = gate.queue_view()
        reason = cast(gate.HeadAdmissionStatus, view.waiting[0].reason)
        self.assertEqual(reason.decision.state, "hold")
        explanation = gate.explain_queue()
        self.assertIsInstance(gate.smallest_head_release(explanation), gate.HeadStillHeld)
        result = self.check(pid=300, need=12, free=23)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual(result.head_reason, reason)
        self.assertTrue(waiter.exists())
        self.assertTrue(self.override_path.exists())

    def test_frozen_release_preserves_negative_queue_order_for_an_override(self) -> None:
        _ = self.running(100, 20, age=30)
        head_path = self.waiting_head()
        follower_path = self.waiter(400, NOW.timestamp() - 60, "/tmp/follower", "follower", 20)
        for path, order in ((head_path, -3), (follower_path, -2)):
            row = cast(gate.Waiter, json.loads(path.read_text()))
            row.update(bump_order=order, priority_since=NOW.timestamp(), holds_younger=True)
            _ = path.write_text(json.dumps(row))
        _ = self.set_head()
        explanation = gate.explain_queue()
        self.assertEqual(cast(gate.HeadExplanation, explanation.head).decision.state, "admit")
        captured = tuple(row.copy() for row in explanation.ordered)
        for result in (gate.head_after_steps_end(explanation, ()), gate.smallest_head_release(explanation)):
            with self.subTest(result=result):
                self.assertIsInstance(result, gate.HeadAdmitsAfterEnd)
                if isinstance(result, gate.HeadAdmitsAfterEnd):
                    self.assertEqual(result.ended, ())
                    self.assertEqual(result.decision.state, "admit")
        self.assertEqual(explanation.ordered, captured)

    def test_stopped_record_replacement_keeps_previous_row_and_can_be_rerun(self) -> None:
        _ = self.waiting_head()
        _ = self.set_head("first")
        before = self.override_path.read_bytes()
        with patch.object(os, "replace", side_effect=OSError("stopped before rename")):
            with self.assertRaises(OSError):
                _ = self.set_head("second")
        self.assertEqual(self.override_path.read_bytes(), before)
        self.assertEqual(len(self.events()), 1)
        _ = self.set_head("second")
        self.assertEqual(json.loads(self.override_path.read_text())["why"], "second")


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
        _ = self.head(need=15)
        waiter = cast(gate.Waiter, json.loads(self.waiter_path.read_text()))
        waiter["need"] = 8 * GIB
        _ = self.waiter_path.write_text(json.dumps(waiter))
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
        self.assertEqual((result.head_need, result.head_label), (0, ""))
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

    def test_orphaned_scope_stays_charged_visible_and_blocks_until_empty(self) -> None:
        self.readings_for()
        reservation = self.running(100, 12)
        scope = self.builds / "orphan.scope"
        scope.mkdir(parents=True)
        _ = (scope / "cgroup.procs").write_text("101\n")
        _ = (scope / "memory.current").write_text(str(13 * GIB))
        _ = (scope / "memory.swap.current").write_text("0")
        sidecar = self.root / "scope-path"
        _ = sidecar.write_text(str(scope))
        row = cast(dict[str, object], json.loads(reservation.read_text()))
        row["sidecar"] = str(sidecar)
        row["session_id"] = "orphan-owner"
        _ = reservation.write_text(json.dumps(row))
        _ = reservation.with_suffix(".max").write_text(str(14 * GIB))
        _ = reservation.with_suffix(".wave").write_text(f"{14 * GIB} {int(NOW.timestamp())}")
        del self.processes[100]  # The shell stopped; its scope child still owns memory.
        result = self.admit_at()
        self.assertEqual((result.decision.state, result.decision.charged), ("hold", 14 * GIB))
        view, _ = gate.read_queue(self.root / "admission", gate.process_table(self.root / "proc"), NOW.timestamp())
        self.assertEqual(len(view.running), 1)
        with patch("memory_admit.time.time", return_value=NOW.timestamp()):
            self.assertEqual(gate.blocker_fields(gate.current_blocker(self.root / "admission/400-warmup.waiting")),
                             {"session_id": "orphan-owner", "session_name": "", "pool": "memory"})
        for suffix in (".reservation", ".max", ".wave"):
            self.assertTrue(reservation.with_suffix(suffix).exists())
        _ = (scope / "cgroup.procs").write_text("")
        self.assertEqual(self.admit_at().decision.state, "admit")
        for suffix in (".reservation", ".max", ".wave"):
            self.assertFalse(reservation.with_suffix(suffix).exists())

    def test_orphaned_scope_does_not_take_identity_from_reused_owner_pid(self) -> None:
        self.readings_for()
        reservation = self.running(100, 12)
        scope = self.builds / "orphan.scope"
        scope.mkdir(parents=True)
        _ = (scope / "cgroup.procs").write_text("101\n")
        _ = (scope / "memory.current").write_text(str(14 * GIB))
        _ = (scope / "memory.swap.current").write_text("0")
        sidecar = self.root / "scope-path"
        _ = sidecar.write_text(str(scope))
        row = cast(dict[str, object], json.loads(reservation.read_text()))
        row["sidecar"] = str(sidecar)
        row["step"] = ""
        _ = reservation.write_text(json.dumps(row))
        self.processes[100] = "unrelated-start"
        process = self.root / "proc/100"
        process.mkdir(parents=True)
        _ = (process / "environ").write_bytes(b"CODEX_THREAD_ID=unrelated-session\0CLAUDE_SESSION_NAME=unrelated-name\0")
        _ = (process / "cmdline").write_bytes(b"cargo\0clippy\0")
        (process / "cwd").symlink_to(self.root / "unrelated-folder")
        result = self.admit_at()
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual(gate.blocker_fields(result.blocker), {"unit": "orphan.scope"})
        with patch("memory_admit.time.time", return_value=NOW.timestamp()):
            self.assertEqual(gate.blocker_fields(gate.current_blocker(self.root / "admission/400-warmup.waiting")),
                             {"unit": "orphan.scope"})
        view, _ = gate.read_queue(self.root / "admission", gate.process_table(self.root / "proc"), NOW.timestamp())
        self.assertEqual(len(view.running), 1)
        entry = view.running[0]
        self.assertEqual((entry.session_id, entry.session_name, entry.folder, entry.step), ("", "", "test", ""))

    def test_scope_counter_disappearing_uses_owner_lifetime(self) -> None:
        reservation = self.running(100, 12)
        scope = self.builds / "ending.scope"
        scope.mkdir(parents=True)
        _ = (scope / "cgroup.procs").write_text("101\n")
        sidecar = self.root / "scope-path"
        _ = sidecar.write_text(str(scope))
        row = cast(gate.Reservation, json.loads(reservation.read_text()))
        row["sidecar"] = str(sidecar)
        for missing in ("memory.current", "memory.swap.current"):
            for owner in ("live", "gone", "reused"):
                with self.subTest(missing=missing, owner=owner):
                    for counter in ("memory.current", "memory.swap.current"):
                        _ = (scope / counter).write_text("0")
                    (scope / missing).unlink()
                    processes = {} if owner == "gone" else {100: gate.ProcessIdentity(1, "first" if owner == "live" else "other")}
                    with patch.object(gate, "tree_memory", return_value=gate.MeasuredMemory(2 * GIB)) as tree:
                        reading = gate.step_memory(row, NOW, processes, self.root / "proc")
                    if owner == "live":
                        self.assertEqual(reading, gate.MeasuredMemory(2 * GIB))
                        tree.assert_called_once_with(100, "first", processes, self.root / "proc")
                    else:
                        self.assertIsInstance(reading, gate.GoneMemory)
                        tree.assert_not_called()

    def test_width_write_interruption_keeps_reservation_for_retry(self) -> None:
        reservation = self.running(100, 12)
        before = reservation.read_bytes()
        write_text = Path.write_text
        def stop(path: Path, data: str) -> int:
            _ = write_text(path, data[:10])
            raise RuntimeError("stopped during width write")
        with patch.object(Path, "write_text", side_effect=None, autospec=True) as write:
            write.side_effect = stop
            with self.assertRaisesRegex(RuntimeError, "stopped during width write"):
                gate.set_width(reservation, 8, self.root)
        self.assertEqual(reservation.read_bytes(), before)
        gate.set_width(reservation, 8, self.root)
        self.assertEqual(json.loads(reservation.read_text())["test_threads"], 8)

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
            "need_basis": {"kind": "measured"}, "need_runs": 5,
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

                profile = gate.InferFromArgv() if computed else gate.StepProfile(
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


class StrictHeadRoomTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root), "BUILDLOG_BUILD_BUDGET_GIB": "21",
        })))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.meminfo.write_text(f"MemAvailable: {60 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def strict_head(self, need: float = 11.5, *, priority: float = NOW_EPOCH) -> Path:
        path = self.waiter(300, NOW.timestamp() - 2400, "/tmp/head", "head", need)
        row = cast(gate.Waiter, json.loads(path.read_text()))
        row.update(holds_younger=True, priority_since=priority, label="head nextest")
        _ = path.write_text(json.dumps(row))
        return path

    def start_younger(self, pid: int, need: float) -> gate.CheckResult:
        self.processes[pid] = str(pid)
        self.readings[pid] = gate.MeasuredMemory(0)
        return self.check(pid=pid, need=need, free=60)

    def test_strict_room_matches_decision_room_and_window_clause(self) -> None:
        for need, expected_room, text in (
                (7, 14, "younger steps may use 14.0 GiB of the budget until 18:10"),
                (21, 0, "younger steps wait until 18:10"),
                (22, -1, "younger steps wait until 18:10")):
            with self.subTest(head_need=need):
                head: gate.Waiter = {"pid": 300, "start_time": "300", "need": need * GIB,
                    "since": 0, "priority_since": NOW.timestamp(), "holds_younger": True}
                younger: gate.Waiter = {"pid": 400, "start_time": "400", "need": GIB, "since": 1}
                window = gate.StrictUntil(NOW.timestamp() + 600)
                decision = gate.queue_decision(60 * GIB, 60 * GIB, younger, [head, younger], [],
                    gate.HeadPriority(head, window), gate.NoPressureHold(),
                    recovery=gate.NoRecoverySpacing(), now=NOW.timestamp())
                self.assertEqual(gate.strict_room(21 * GIB, need * GIB), expected_room * GIB)
                self.assertIsInstance(decision.head_room, gate.HeadRoom)
                room = cast(gate.HeadRoom, decision.head_room)
                self.assertEqual((room.head_need, room.used, room.room, room.passes),
                                 (need * GIB, 0, expected_room * GIB, need == 7))
                self.assertEqual(decision.state, "admit" if need == 7 else "hold")
                self.assertEqual(gate.window_clause(window, 21 * GIB, need * GIB,
                                                   ZoneInfo("America/New_York")), text)

    def test_fitting_strict_head_keeps_its_place_when_younger_polls_first(self) -> None:
        for need, free in ((4, 60), (5, 60), (4, 11)):
            with self.subTest(need=need, free=free):
                directory = gate.admission_dir(self.root)
                for pattern in ("*.waiting", "*.reservation"):
                    for path in directory.glob(pattern):
                        path.unlink()
                _ = self.running(100, 10, age=30)
                self.readings[100] = gate.MeasuredMemory(10 * GIB)
                _ = self.strict_head(8)
                younger = self.waiter(400, NOW.timestamp() - 60, "/tmp/younger", "younger", need)
                result = self.check(pid=400, need=need, free=free)
                self.assertEqual(result.decision.state, "hold")
                self.assertIsInstance(result.head_reason, gate.BehindOlderFit)
                self.assertEqual(gate.queue_view().waiting[-1].reason, result.head_reason)
                self.assertEqual(gate.explain_queue().line.steps[-1].waiter.reason, result.head_reason)
                self.assertIsInstance(gate.current_blocker(younger), gate.NoSingleBlocker)
                self.assertEqual(self.check(pid=300, need=8, free=free).decision.state, "admit")

    def test_first_younger_admitted_in_priority_check_counts_on_next_check(self) -> None:
        _ = self.running(100, 11.1, age=30)
        self.readings[100] = gate.MeasuredMemory(int(9.9 * GIB))
        _ = self.head(11.5)  # No priority timestamp until this admission check takes the lock.
        self.assertEqual(self.start_younger(500, 9.5).decision.state, "admit")
        head = cast(gate.Waiter, json.loads((gate.admission_dir(self.root) / "300-head.waiting").read_text()))
        self.assertEqual(head.get("priority_since"), NOW.timestamp())
        held = self.start_younger(600, 0.4)
        self.assertIsInstance(held.head_reason, gate.BehindHead)
        self.assertEqual(cast(gate.HeadRoom, held.decision.head_room).used, int(9.5 * GIB))

    def test_backfill_admission_uses_room_in_the_next_strict_window(self) -> None:
        _ = self.running(100, 11.1, age=1000)
        self.readings[100] = gate.MeasuredMemory(int(11.1 * GIB))
        _ = self.strict_head(priority=NOW.timestamp() - 600)
        self.assertEqual(self.start_younger(500, 9.5).decision.state, "admit")
        self.readings[500] = gate.MeasuredMemory(int(9.5 * GIB))
        clock = NOW + timedelta(seconds=120)
        profile = gate.StepProfile("test", "nextest", "test", gate.ExpectedPeak(int(0.4 * GIB), "measured", 5))
        with patch.object(time, "time", return_value=clock.timestamp()):
            result = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root,
                                now=clock, profile=profile)
        self.assertIsInstance(result.head_reason, gate.BehindHead)
        self.assertEqual(cast(gate.HeadRoom, result.decision.head_room).used, int(9.5 * GIB))

    def test_head_wait_clause_distinguishes_backfill_room_and_no_room(self) -> None:
        zone = ZoneInfo("America/New_York")
        for need, window, expected in (
            (12, gate.BackfillUntil(NOW.timestamp() + 120), "younger steps may start until 18:02"),
            (11.5, gate.StrictUntil(NOW.timestamp() + 600), "younger steps may use 9.5 GiB of the budget until 18:10"),
            (21, gate.StrictUntil(NOW.timestamp() + 600), "younger steps wait until 18:10"),
            (22, gate.StrictUntil(NOW.timestamp() + 600), "younger steps wait until 18:10"),
        ):
            with self.subTest(need=need, window=window):
                decision = gate.decide(60 * GIB, 60 * GIB, int(need * GIB), [])
                text = gate.head_wait_text(gate.HeadAdmissionStatus(decision, int(need * GIB), 60 * GIB, window), zone)
                self.assertTrue(text.endswith(expected), text)

    def test_incident_room_admits_small_steps_and_head_starts_when_widget_ends(self) -> None:
        widget = self.running(100, 11.1, age=30)
        self.readings[100] = gate.MeasuredMemory(int(9.9 * GIB))
        _ = self.strict_head()
        for pid, need in ((500, 1.0), (600, 3.0), (700, 5.0), (800, 0.5)):
            with self.subTest(need=need):
                admitted = self.start_younger(pid, need)
                self.assertEqual(admitted.decision.state, "admit")
                self.assertEqual((admitted.head_need, admitted.head_label), (0, ""))
        blocked = self.start_younger(900, 0.4)
        self.assertEqual(blocked.decision.state, "hold")
        self.assertTrue(blocked.decision.budget_passes)
        self.assertTrue(blocked.decision.free_passes)
        self.assertIsInstance(blocked.head_reason, gate.BehindHead)
        self.assertEqual(blocked.head_need, int(11.5 * GIB))
        self.assertEqual(blocked.decision.head_room, gate.HeadRoom(int(11.5 * GIB), int(9.5 * GIB), int(9.5 * GIB), False))
        widget.unlink()
        self.assertEqual(self.check(pid=300, need=11.5, free=60).decision.state, "admit")

    def test_head_starts_after_the_original_blocker_set_ends(self) -> None:
        blockers = [self.running(pid, 6, age=30) for pid in (100, 200)]
        self.readings.update({100: gate.MeasuredMemory(6 * GIB), 200: gate.MeasuredMemory(6 * GIB)})
        _ = self.strict_head(12)
        self.assertEqual(self.start_younger(500, 4).decision.state, "admit")
        self.assertEqual(self.start_younger(600, 5).decision.state, "admit")
        blockers[0].unlink()
        self.assertEqual(self.check(pid=300, need=12, free=60).decision.state, "hold")
        blockers[1].unlink()
        self.assertEqual(self.check(pid=300, need=12, free=60).decision.state, "admit")

    def test_resource_holds_do_not_claim_the_heads_room_is_the_cause(self) -> None:
        _ = self.running(100, 20, age=30)
        self.readings[100] = gate.MeasuredMemory(20 * GIB)
        _ = self.strict_head()
        for free, reason in ((60, gate.NeedsBuildBudget), (3, gate.NeedsBuildBudget)):
            result = self.check(pid=400, need=2, free=free)
            self.assertIsInstance(result.head_reason, reason)
            self.assertEqual((result.head_need, result.head_label), (0, ""))
        _ = self.running(100, 1, age=30)
        self.readings[100] = gate.MeasuredMemory(GIB)
        result = self.check(pid=400, need=2, free=3)
        self.assertIsInstance(result.head_reason, gate.NeedsMemory)
        self.assertEqual((result.head_need, result.head_label), (0, ""))

    def test_room_sum_uses_milliseconds_live_needs_and_charges(self) -> None:
        priority = NOW.timestamp() + 0.0009
        path = self.strict_head(priority=priority)
        head = cast(gate.Waiter, json.loads(path.read_text()))
        _ = self.running(100, 4, age=1)
        _ = self.running(200, 2, age=0)
        _ = self.running(400, 3, age=-600)  # A backfill admission still counts in the next strict window.
        self.readings.update({100: gate.MeasuredMemory(4 * GIB), 200: gate.MeasuredMemory(5 * GIB),
                              400: gate.MeasuredMemory(GIB)})
        rows = gate.live_reservations(gate.admission_dir(self.root))
        reservations = [row for _, row in rows]
        memories = [gate.reservation_memory(path, row, NOW + timedelta(seconds=720),
                                            gate.process_table(self.root / "proc"), self.root / "proc")
                    for path, row in rows]
        priority_head = gate.HeadPriority(head, gate.StrictUntil(NOW.timestamp() + 1320))
        self.assertEqual(gate.head_room_used(reservations, memories, priority_head), 8 * GIB)
        del self.processes[200]
        rows = gate.live_reservations(gate.admission_dir(self.root))
        reservations = [row for _, row in rows]
        memories = [gate.reservation_memory(path, row, NOW + timedelta(seconds=720),
                                            gate.process_table(self.root / "proc"), self.root / "proc")
                    for path, row in rows]
        self.assertEqual(gate.head_room_used(reservations, memories, priority_head), 3 * GIB)
        self.assertEqual(gate.head_room_used(reservations, memories, gate.NoHeadPriority()), 0)

    def test_room_is_taken_in_queue_order_before_a_younger_polls(self) -> None:
        _ = self.running(100, 11.1, age=30)
        self.readings[100] = gate.MeasuredMemory(int(9.9 * GIB))
        _ = self.strict_head()
        _ = self.waiter(400, NOW.timestamp() - 120, "/tmp/older", "older", 5)
        self.processes[500] = "young"
        younger = self.waiter(500, NOW.timestamp() - 60, "/tmp/young", "young", 5)
        result = self.check(pid=500, need=5, free=60)
        self.assertEqual(result.head_reason, gate.BehindOlderFit("older", NOW.timestamp() - 120, "session-400"))
        self.assertEqual(result.head_need, 0)
        self.assertEqual(gate.queue_view().waiting[-1].reason, result.head_reason)
        self.assertEqual(gate.explain_queue().line.steps[-1].waiter.reason, result.head_reason)
        self.assertIsInstance(gate.current_blocker(younger), gate.NoSingleBlocker)
        self.assertTrue(self.shell_fields(result)[8].startswith("behind older step older"))
        self.assertEqual(self.check(pid=400, need=5, free=60).decision.state, "admit")

    def test_room_hold_agrees_in_check_view_explain_blocker_and_shell(self) -> None:
        _ = self.running(100, 11.1, age=30)
        self.readings[100] = gate.MeasuredMemory(int(9.9 * GIB))
        _ = self.strict_head()
        self.assertEqual(self.start_younger(500, 9.5).decision.state, "admit")
        self.processes[600] = "held"
        waiter = self.waiter(600, NOW.timestamp() - 30, "/tmp/held", "held", 0.4)
        result = self.check(pid=600, need=0.4, free=60)
        self.assertIsInstance(result.head_reason, gate.BehindHead)
        self.assertEqual(gate.queue_view().waiting[-1].reason, result.head_reason)
        self.assertEqual(gate.explain_queue().line.steps[-1].waiter.reason, result.head_reason)
        self.assertEqual(gate.current_blocker(waiter), result.blocker)
        self.assertIsInstance(result.blocker, gate.NoSingleBlocker)
        with patch.dict(os.environ, {"BUILDLOG_MEM_DETAIL": "1"}):
            fields = self.shell_fields(result)
        self.assertEqual(int(fields[16]), int(11.5 * GIB))
        self.assertIn("head", fields[17])



class OrderedAdmissionTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        environment = fake_memory_readings(self.root)
        environment.update({"BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_BUILD_BUDGET_GIB": "10"})
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        self.processes[500] = "younger"
        _ = self.meminfo.write_text(f"MemAvailable: {24 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def scenario(self, window: str, older_need: float, younger_need: float) -> tuple[Path, Path]:
        directory = gate.admission_dir(self.root)
        for suffix in ("waiting", "reservation"):
            for path in directory.glob(f"*.{suffix}"):
                path.unlink()
        _ = self.running(100, 5.8)
        self.readings[100] = gate.MeasuredMemory(int(5.8 * GIB))
        if window != "none":
            path = self.waiter(300, NOW.timestamp() - 2400, "/tmp/head", "head", 19.3)
            row = cast(gate.Waiter, json.loads(path.read_text()))
            row.update(label="head nextest", holds_younger=True,
                       priority_since=NOW.timestamp() - (600 if window == "backfill" else 0))
            _ = path.write_text(json.dumps(row))
        return (self.waiter(400, NOW.timestamp() - 120, "/tmp/berth", "berth", older_need),
                self.waiter(500, NOW.timestamp() - 60, "/tmp/handler", "handler", younger_need))

    def test_younger_poll_keeps_room_for_older_fitting_step_in_each_window(self) -> None:
        for window in ("none", "backfill"):
            with self.subTest(window=window):
                _ = self.scenario(window, 2.9, 3.0)
                result = self.check(pid=500, need=3, free=24)
                self.assertEqual(result.decision.state, "hold")
                self.assertEqual(result.head_reason, gate.BehindOlderFit("berth", NOW.timestamp() - 120, "session-400"))
                self.assertEqual(self.check(pid=400, need=2.9, free=24).decision.state, "admit")

    def test_older_smaller_step_gets_room_before_younger_in_each_window(self) -> None:
        for window in ("none", "backfill"):
            with self.subTest(window=window):
                _ = self.scenario(window, 1, 4)
                self.assertEqual(self.check(pid=500, need=4, free=24).decision.state, "hold")
                self.assertEqual(self.check(pid=400, need=1, free=24).decision.state, "admit")

    def test_younger_starts_when_older_does_not_fit_in_each_window(self) -> None:
        for window in ("none", "backfill"):
            with self.subTest(window=window):
                _ = self.scenario(window, 5, 3)
                self.assertEqual(self.check(pid=500, need=3, free=24).decision.state, "admit")

    def test_strict_window_keeps_behind_head_reason(self) -> None:
        _ = self.scenario("strict", 2.9, 3)
        view = gate.queue_view()
        self.assertIsInstance(view.waiting[-1].reason, gate.BehindHead)
        self.assertEqual(self.check(pid=500, need=3, free=24).decision.state, "hold")

    def test_one_snapshot_agrees_in_check_queue_explain_blocker_and_shell(self) -> None:
        _, younger = self.scenario("none", 2.9, 3)
        self.assert_older_fit_snapshot(younger)

    def test_open_recovery_slot_reserves_first_fitting_waiter_in_each_window(self) -> None:
        for window in ("none", "backfill", "strict"):
            with self.subTest(window=window):
                need = 1 if window == "strict" else 3
                _, younger = self.scenario(window, need, need)
                directory = gate.admission_dir(self.root)
                if window == "strict":
                    head_path = directory / "300-head.waiting"
                    head_row = cast(gate.Waiter, json.loads(head_path.read_text()))
                    head_row["need"] = 6 * GIB
                    _ = head_path.write_text(json.dumps(head_row))
                if window == "none":
                    (directory / "100.reservation").unlink()
                elif window == "backfill":
                    self.readings[100] = gate.MeasuredMemory(GIB)
                if window == "strict":
                    # Both younger rows fit together; only recovery may reserve the second start.
                    (directory / "pressure-recovery").unlink(missing_ok=True)
                    view = gate.queue_view()
                    self.assertEqual(len(view.waiting), 3)
                    self.assertTrue(all(isinstance(row.reason, gate.ReadyForAdmission) for row in view.waiting[1:]))
                gate.write_recovery(directory, gate.RecoveryStarts(NOW.timestamp() + 120, NOW.timestamp()))
                self.assert_older_fit_snapshot(younger, need)
                self.assertEqual(self.check(pid=400, need=need, free=24).decision.state, "admit")
                self.assertEqual(gate.read_recovery(directory),
                                 gate.RecoveryStarts(NOW.timestamp() + 120, NOW.timestamp() + 30))

    def test_strict_wait_line_names_reserved_head_without_duplicate_behind_text(self) -> None:
        _ = self.scenario("strict", 2.9, 3)
        result = self.check(pid=500, need=3, free=24)
        with patch.dict(os.environ, {"BUILDLOG_MEM_DETAIL": "1"}):
            fields = self.shell_fields(result)
        self.assertEqual(len(fields), 18)
        self.assertNotIn("behind head", fields[8])
        self.assertEqual(int(fields[16]), int(19.3 * GIB))
        self.assertIn("head", fields[17])

    def assert_older_fit_snapshot(self, younger: Path, need: float = 3) -> None:
        reason = gate.BehindOlderFit("berth", NOW.timestamp() - 120, "session-400")
        result = self.check(pid=500, need=need, free=24)
        self.assertEqual(result.head_reason, reason)
        self.assertEqual(result.decision.state, "hold")
        view = gate.queue_view()
        self.assertEqual(view.waiting[-1].reason, reason)
        explanation = gate.explain_queue()
        self.assertEqual(explanation.line.steps[-1].waiter.reason, reason)
        self.assertIsInstance(gate.current_blocker(younger), gate.NoSingleBlocker)
        self.assertIsInstance(result.blocker, gate.NoSingleBlocker)
        fields = self.shell_fields(result)
        self.assertEqual(len(fields), 15)
        self.assertTrue(fields[8].startswith("behind older step berth"), fields[8])



class QueueApiTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_PROC": str(self.root / "proc"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        self.processes[500] = "third"
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.meminfo.write_text(f"MemAvailable: {int(23.1 * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

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
        self.assertEqual((reason.threshold, reason.growth, reason.reserve),
                         (10 * GIB, 4 * GIB, 3 * GIB))
        self.assertEqual(reason.threshold, reason.need + reason.growth + reason.reserve)

        # A strict head's room is independent of the free-memory threshold.
        head: gate.Waiter = {"pid": 300, "start_time": "head", "need": 7 * GIB, "since": 0,
                             "holds_younger": True, "priority_since": NOW.timestamp()}
        younger: gate.Waiter = {"pid": 400, "start_time": "warmup", "need": 3 * GIB, "since": 1}
        decision = gate.queue_decision(8 * GIB, 60 * GIB, younger, [head, younger],
            [gate.PromisedMemory(4 * GIB)], gate.HeadPriority(head, gate.StrictUntil(NOW.timestamp() + 600)),
            gate.NoPressureHold(), recovery=gate.NoRecoverySpacing(), now=NOW.timestamp())
        self.assertEqual((decision.state, decision.threshold), ("hold", 10 * GIB))
        self.assertFalse(decision.free_passes)
        self.assertTrue(cast(gate.HeadRoom, decision.head_room).passes)

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
                             + "(starts when build use falls to 9.1 GiB); needs 34.4 GiB free; 23.1 free; younger steps may use 9.1 GiB of the budget until 18:10")
        self.assertCountEqual([row.measured for row in view.running],
                              [gate.MeasuredMemory(GIB, gate.RampPromise(12 * GIB)), gate.MeasuredMemory(2 * GIB, gate.RampPromise(int(11.5 * GIB)))])

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
        for elapsed, state, deadline, window in ((0, "hold", "18:10", "may use 0.7 GiB of the budget"),
                                                  (600, "admit", "18:12", "may start"),
                                                  (720, "hold", "18:22", "may use 0.7 GiB of the budget")):
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

    def test_bump_refusal_keeps_backfill_window_and_all_waiter_bytes(self) -> None:
        paths = self.queue()
        row = cast(gate.Waiter, json.loads(paths[0].read_text()))
        row.update(holds_younger=True, priority_since=NOW.timestamp() - 600)
        _ = paths[0].write_text(json.dumps(row))
        before = {path: path.read_bytes() for path in paths}
        original = gate.queue_view()
        self.assertIsInstance(gate.head_window(row, NOW.timestamp()), gate.BackfillUntil)
        for selector in ("2", "session-400", "warmup", "/tmp/warmup-tree", "warmup-tree", "missing", "99"):
            with self.subTest(selector=selector), patch.object(fcntl, "flock", side_effect=AssertionError("bump took a lock")), \
                 patch.object(gate, "read_queue", side_effect=AssertionError("bump read the queue")):
                with self.assertRaises(gate.BumpOff) as refused:
                    gate.bump_waiter(selector, "actor")
                self.assertEqual(str(refused.exception),
                                 "bump is off; nothing can move a waiting step")
            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertEqual(gate.queue_view(), original)
            self.assertEqual(buildlog_rows(self.root), [])
        self.assertIsInstance(gate.head_window(cast(gate.Waiter, json.loads(paths[0].read_text())), NOW.timestamp()),
                              gate.BackfillUntil)

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
            self.assertEqual(gate.head_wait_text(reason, ZoneInfo("America/New_York")), "head: needs 22.3 GiB free; 21.9 free; younger steps may use 0.7 GiB of the budget until 18:10")
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
                             + "(starts when build use falls to 4.9 GiB); younger steps may use 4.9 GiB of the budget until 18:10")
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
            self.assertEqual(gate.head_wait_text(result.head_reason, ZoneInfo("America/New_York")), "head: needs 18.1 GiB free; 18.0 free; younger steps may use 4.9 GiB of the budget until 18:10")

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
        self.assertEqual(self.check(need=19, free=40).decision.state, "hold")
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
        self.assertIsInstance(view.waiting[1].reason, gate.MemoryPressure)


    def test_queue_view_and_check_agree_on_heads_that_can_and_cannot_fit_after_drain(self) -> None:
        _ = self.running(100, 30)
        self.readings[100] = gate.MeasuredMemory(int(21.5 * GIB))
        self.readings[400] = gate.MeasuredMemory(0)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "30"})))
        for need, held in ((19.3, False), (29, True), (38, False)):
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
        self.assertEqual(view.waiting[1].reason,
                         gate.BehindOlderFit("warmup", NOW.timestamp() - 300, "session-400"))
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
    def test_shape_without_earlier_builds_names_the_fallback(self) -> None:
        command = ('source "$1"; BUILDLOG_MEM_WAIT_S=0; buildlog_memory_python() { echo UTC; }; '
                   + 'buildlog_memory_line first 0 20000000000 17000000000 shape 0 0 0 3000000000 '
                   + '"" hana build 0')
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    env={**os.environ, **fake_memory_readings(Path(folder)),
                                         "BUILDLOG_DIR": folder}, capture_output=True,
                                    text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fallback", result.stderr)
        self.assertNotIn("0 earlier builds", result.stderr)

    def test_first_wait_line_names_a_same_shape_promise_without_claiming_p90(self) -> None:
        command = ('source "$1"; BUILDLOG_MEM_WAIT_S=0; buildlog_memory_python() { echo UTC; }; '
                   + 'buildlog_memory_line first 0 20000000000 17000000000 shape 2 0 0 3000000000 '
                   + '"" hana build 0')
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    env={**os.environ, **fake_memory_readings(Path(folder)),
                                         "BUILDLOG_DIR": folder}, capture_output=True,
                                    text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("same-shape promise", result.stderr)
        self.assertNotIn("percentile", result.stderr)
        self.assertNotIn("p90", result.stderr)
        self.assertNotIn("fallback", result.stderr)

    def test_shell_accepts_shape_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wrapper = root / "checker.py"
            _ = wrapper.write_text("import os\n" +
                                   "print('admit|0|0|' + os.environ['TEST_NEED_SOURCE'] + "
                                   + "'|0|0|0|0|||||0|launch||0|0|')\n")
            command = 'source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; buildlog_wait_for_memory'
            environment = {**os.environ, **fake_memory_readings(root), "BUILDLOG_DIR": folder,
                           "BUILD_HOLD_DIR": str(root / "holds"),
                           "BUILD_HOLD_RELEASE_DIR": str(root / "releases"),
                           "UNIT_ACTIVE_DIR": str(root / "active"), "UNIT_RUN_DIR": ""}
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(wrapper)],
                                    env={**environment, "TEST_NEED_SOURCE": "shape"}, capture_output=True,
                                    text=True, check=False, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

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
                "need_basis": {"kind": "fallback"}, "need_runs": 0,
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
        self.environment = {**os.environ, **gate_isolation_environment(self.root),
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

    def snapshot_inputs(self) -> gate.QueueInputs:
        directory = gate.admission_dir(self.root)
        return gate.gather_queue_inputs(directory, gate.process_table(self.root / "proc"), NOW.timestamp())

    def published_ledger(self, *, compiler: bool = False, legacy_reservation: bool = False
                         ) -> gate.QueueSnapshot:
        """Publish through check's exclusive exit, then read while admission is locked."""
        self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root)))
        if compiler:
            service = self.root / "builds.slice/sccache.service"
            service.mkdir(parents=True)
            _ = (service / "memory.stat").write_text(f"anon {2 * GIB}\nfile {10 * GIB}\n")
        if legacy_reservation:
            path = self.running(100, 6)
            row = cast(dict[str, object], json.loads(path.read_text()))
            del row["test_threads"], row["call_id"]
            _ = path.write_text(json.dumps(row))
        result = self.check(need=2, free=0)
        self.assertEqual(result.decision.state, "hold")
        directory = gate.admission_dir(self.root)
        snapshot = gate.read_queue_snapshot(directory)
        if not isinstance(snapshot, gate.QueueSnapshot):
            self.fail("check's published ledger did not decode as a queue snapshot")
        with (directory / "lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = time.perf_counter()
            reading = gate.queue_read()
            self.assertLess(time.perf_counter() - started, 1.0)
        self.assertIsInstance(reading, gate.QueueRead)
        assert isinstance(reading, gate.QueueRead)
        self.assertEqual(reading.freshness, gate.CurrentReading())
        self.assertEqual(reading, gate.queue_read_from(snapshot.inputs))
        return snapshot

    def test_check_generated_need_source_survives_publication_without_a_lock(self) -> None:
        snapshot = self.published_ledger()
        self.assertEqual(len(snapshot.inputs.ordered), 1)
        waiter = snapshot.inputs.ordered[0][1]
        self.assertEqual((waiter["pid"], waiter.get("need_source"), waiter.get("need_runs")),
                         (400, "measured", 5))

    def test_active_compiler_memory_keeps_its_variant_in_a_lock_free_snapshot(self) -> None:
        snapshot = self.published_ledger(compiler=True)
        self.assertEqual(len(snapshot.inputs.running), 1)
        compiler = snapshot.inputs.running[0]
        self.assertIsInstance(compiler.measured, gate.CompilerServiceMemory)
        assert isinstance(compiler.measured, gate.CompilerServiceMemory)
        self.assertEqual((compiler.session_name, compiler.measured.bytes), ("sccache.service", 2 * GIB))
        reading = gate.queue_read_from(snapshot.inputs)
        assert isinstance(reading.explanation.head, gate.HeadExplanation)
        self.assertEqual(reading.explanation.head.decision.measured, 2 * GIB)

    def test_legacy_reservation_without_optional_fields_reads_from_the_published_snapshot(self) -> None:
        snapshot = self.published_ledger(compiler=True, legacy_reservation=True)
        self.assertEqual(len(snapshot.inputs.reservations), 1)
        reservation = snapshot.inputs.reservations[0]
        self.assertEqual((reservation["pid"], reservation["test_threads"], reservation["call_id"]),
                         (100, None, None))
        assert isinstance(snapshot.inputs.running[0].measured, gate.MeasuredMemory)
        self.assertEqual((snapshot.inputs.running[0].promise, snapshot.inputs.running[0].measured.bytes),
                         (6 * GIB, GIB))

    def test_damaged_waiter_cannot_mask_a_section_error_or_break_repeated_cancel_and_release(self) -> None:
        damaged = self.waiting(300, 12 * GIB, 20)
        row = cast(dict[str, object], json.loads(damaged.read_text()))
        del row["since"]
        _ = damaged.write_text(json.dumps(row))
        failure = RuntimeError("section's original failure")
        with self.assertRaises(RuntimeError) as raised:
            with gate.exclusive_queue(damaged.parent):
                raise failure
        self.assertIs(raised.exception, failure)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                waiter = self.waiter(400, NOW.timestamp(), "/tmp/warmup", "warmup", need=2)
                reservation = self.running(100, 6)
                gate.cancel_waiter(400, root=self.root)
                gate.release(reservation, 0, root=self.root)
                self.assertFalse(waiter.exists())
                self.assertFalse(reservation.exists())
                reading = gate.queue_read()
                assert isinstance(reading, gate.QueueRead)
                self.assertEqual((reading.view.waiting, reading.view.running), ((), ()))

    def test_snapshot_resolves_the_same_budget_and_reserve_as_admission(self) -> None:
        for budget in ("21", "7.25", "invalid", "0", "nan"):
            for total in (60 * GIB, 0):
                with self.subTest(budget=budget, total=total), \
                     patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": budget}):
                    _ = self.meminfo.write_text(
                        f"MemAvailable: {16 * GIB // 1024} kB\nMemTotal: {total // 1024} kB\n")
                    with patch.object(gate, "resolve_admission_limits", wraps=gate.resolve_admission_limits) as resolve:
                        inputs = self.snapshot_inputs()
                        assert isinstance(inputs.host, gate.HostMemory)
                        decision = gate.decide(inputs.host.available, inputs.host.total, GIB, [])
                    self.assertEqual(resolve.call_args_list,
                                     [mock.call(inputs.host.total), mock.call(inputs.host.total)])
                    self.assertEqual(inputs.limits, gate.AdmissionLimits(decision.budget, decision.reserve))

    def test_check_release_and_width_publish_the_changed_ledger(self) -> None:
        self.readings[400] = gate.MeasuredMemory(GIB)
        result = self.check(need=2, free=60)
        self.assertEqual(result.decision.state, "admit")
        assert isinstance(result.reservation, gate.Reserved)
        path = result.reservation.path
        snapshot = self.root / "admission/queue.json"
        checked = snapshot.read_bytes()
        self.assertEqual(gate.queue_read(), gate.locked_queue_read())
        gate.set_width(path, 3, root=self.root)
        changed = snapshot.read_bytes()
        self.assertNotEqual(changed, checked)
        self.assertIn('"test_threads":3', changed.decode())
        self.assertEqual(gate.queue_read(), gate.locked_queue_read())
        gate.release(path, 0, root=self.root)
        self.assertNotEqual(snapshot.read_bytes(), changed)
        self.assertEqual(gate.queue_read(), gate.locked_queue_read())
        reading = gate.queue_read()
        assert isinstance(reading, gate.QueueRead)
        self.assertEqual(reading.view.running, ())

    def test_failed_cache_publication_keeps_old_bytes_and_check_result(self) -> None:
        directory = gate.admission_dir(self.root)
        gate.write_queue_snapshot(directory, self.snapshot_inputs())
        path = directory / "queue.json"
        old = path.read_bytes()
        for error in (OSError("cache denied"), ValueError("invalid cache input")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(gate, "write_queue_snapshot", side_effect=error):
                result = self.check(free=0)
            self.assertEqual(result.decision.state, "hold")
            self.assertTrue((directory / "400-warmup.waiting").exists())
            self.assertEqual(path.read_bytes(), old)
        with patch.object(os, "replace", side_effect=OSError("replace denied")):
            with self.assertRaises(OSError):
                gate.write_queue_snapshot(directory, self.snapshot_inputs())
        self.assertEqual(path.read_bytes(), old)

    def test_exclusive_exception_publishes_state_without_masking_the_error(self) -> None:
        path = self.waiting(300, 12 * GIB, 20)
        failure = RuntimeError("after ledger change")
        with patch.object(gate, "live_waiters", side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                _ = self.check(free=0)
        self.assertIs(raised.exception, failure)
        self.assertTrue(path.exists())
        self.assertTrue((path.parent / "queue.json").exists())
        self.assertEqual(gate.queue_read(), gate.locked_queue_read())

    def test_snapshot_round_trip_matches_the_locked_read_with_running_memory_and_identity(self) -> None:
        _ = self.running(100, 6, age=30)
        _ = self.waiting(300, 12 * GIB, 20, bump_order=-1, priority_since=NOW.timestamp() - 120)
        _ = self.waiting(400, GIB, 5)
        inputs = self.snapshot_inputs()
        directory = gate.admission_dir(self.root)
        gate.write_queue_snapshot(directory, inputs)
        expected = gate.locked_queue_read()
        with patch.object(gate, "locked_queue_read", side_effect=AssertionError("fresh snapshot took lock")):
            actual = gate.queue_read()
        self.assertEqual(actual, expected)
        self.assertEqual(actual, gate.queue_read_from(inputs))
        raw = cast(dict[str, object], json.loads((directory / "queue.json").read_text()))
        self.assertEqual((raw["v"], raw["written_at"]), (1, NOW.timestamp()))

    def test_gather_reuses_supplied_memory_and_reads_only_missing_reservations(self) -> None:
        first = self.running(100, 6)
        second = self.running(200, 4)
        supplied = gate.MeasuredMemory(7 * GIB)
        with patch.object(gate, "reservation_memory", wraps=gate.reservation_memory) as read:
            inputs = gate.gather_queue_inputs(gate.admission_dir(self.root),
                gate.process_table(self.root / "proc"), NOW.timestamp(), {first: supplied})
        self.assertEqual([call.args[0] for call in read.call_args_list], [second])
        self.assertEqual([step.measured for step in inputs.running], [supplied, self.readings[200]])

    def test_queue_read_from_uses_no_files_processes_or_reader_limits(self) -> None:
        _ = self.running(100, 6)
        _ = self.waiting(300, 12 * GIB, 20)
        _ = self.waiting(400, GIB, 5)
        inputs = self.snapshot_inputs()
        expected = gate.queue_read_from(inputs)
        with patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "1", "BUILDLOG_PROC": "/absent"}), \
             patch.object(Path, "open", side_effect=AssertionError("pure read opened file")), \
             patch.object(Path, "read_text", side_effect=AssertionError("pure read read file")), \
             patch.object(gate, "process_table", side_effect=AssertionError("pure read scanned processes")), \
             patch.object(gate, "read_pressure", side_effect=AssertionError("pure read measured pressure")), \
             patch.object(gate, "read_recovery", side_effect=AssertionError("pure read read recovery")):
            self.assertEqual(gate.queue_read_from(inputs), expected)
        gate.write_queue_snapshot(gate.admission_dir(self.root), inputs)
        with patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "1"}), \
             patch.object(gate, "locked_queue_read", side_effect=AssertionError("fresh snapshot took lock")):
            self.assertEqual(gate.queue_read(), expected)

    def test_snapshot_age_boundary_and_invalid_cache_take_the_bounded_read(self) -> None:
        _ = self.waiting(300, 12 * GIB, 20)
        directory = gate.admission_dir(self.root)
        inputs = self.snapshot_inputs()
        expected = gate.queue_read_from(inputs)
        for age in (0.0, 10.0, 10.1, -0.1):
            with self.subTest(age=age):
                gate.write_queue_snapshot(directory, inputs)
                with patch.object(time, "time", return_value=NOW.timestamp() + age), \
                     patch.object(gate, "locked_queue_read", return_value=expected) as locked:
                    self.assertEqual(gate.queue_read(), expected)
                if 0 <= age <= 10:
                    locked.assert_not_called()
                else:
                    locked.assert_called_once_with(timeout=0.5)
        path = directory / "queue.json"
        gate.write_queue_snapshot(directory, inputs)
        raw = cast(dict[str, object], json.loads(path.read_text()))
        raw["v"] = 99
        for content in (json.dumps(raw), "{", "{}", '{"v":1,"written_at":0,"inputs":[]}', None):
            with self.subTest(content=content):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    _ = path.write_text(content)
                with patch.object(gate, "locked_queue_read", return_value=expected) as locked:
                    self.assertEqual(gate.queue_read(), expected)
                locked.assert_called_once_with(timeout=0.5)

    def test_held_lock_returns_stale_or_busy_in_under_one_second(self) -> None:
        _ = self.waiting(300, 12 * GIB, 20)
        inputs = self.snapshot_inputs()
        directory = gate.admission_dir(self.root)
        path = directory / "queue.json"
        for cache in ("missing", "corrupt", "stale", "future"):
            with self.subTest(cache=cache):
                gate.write_queue_snapshot(directory, inputs)
                if cache == "missing":
                    path.unlink()
                elif cache == "corrupt":
                    _ = path.write_text("{")
                at = NOW.timestamp() - 1 if cache == "future" else NOW.timestamp() + 11
                with (directory / "lock").open("a+") as lock, patch.object(time, "time", return_value=at):
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    started = time.perf_counter()
                    actual = gate.queue_read()
                    elapsed = time.perf_counter() - started
                self.assertLess(elapsed, 1.0)
                if cache in ("stale", "future"):
                    assert isinstance(actual, gate.QueueRead)
                    self.assertEqual(actual.freshness, gate.StaleReading(NOW.timestamp()))
                    self.assertEqual(actual.view, gate.queue_read_from(inputs).view)
                else:
                    self.assertIsInstance(actual, gate.QueueBusy)

    def test_atomic_snapshot_replace_leaves_old_readable_until_complete_publication(self) -> None:
        path = self.waiting(300, 12 * GIB, 20)
        inputs = self.snapshot_inputs()
        directory = gate.admission_dir(self.root)
        gate.write_queue_snapshot(directory, inputs)
        old = (directory / "queue.json").read_bytes()
        row = cast(gate.Waiter, json.loads(path.read_text()))
        row["need"] = 13 * GIB
        _ = path.write_text(json.dumps(row))
        newer = self.snapshot_inputs()
        replace_file = os.replace
        publications: list[bytes] = []
        def publish(source: str | Path, destination: str | Path) -> None:
            self.assertEqual(Path(source), directory / "queue.json.tmp")
            self.assertEqual(Path(destination), directory / "queue.json")
            self.assertEqual((directory / "queue.json").read_bytes(), old)
            self.assertEqual(gate.queue_read(), gate.queue_read_from(inputs))
            publications.append(Path(source).read_bytes())
            replace_file(source, destination)
        with patch.object(os, "replace", side_effect=publish):
            gate.write_queue_snapshot(directory, newer)
        self.assertEqual(len(publications), 1)
        self.assertEqual((directory / "queue.json").read_bytes(), publications[0])
        self.assertEqual(gate.queue_read(), gate.queue_read_from(newer))

    def test_snapshot_keeps_raw_recovery_and_admits_only_one_when_start_is_due(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {60 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.waiting(300, GIB, 20)
        _ = self.waiting(400, GIB, 5)
        directory = gate.admission_dir(self.root)
        recovery = gate.RecoveryStarts(NOW.timestamp() + 120, NOW.timestamp())
        gate.write_recovery(directory, recovery)
        inputs = self.snapshot_inputs()
        self.assertEqual(inputs.recovery, recovery)
        gate.write_queue_snapshot(directory, inputs)
        locked = gate.locked_queue_read()
        self.assertEqual(gate.queue_read(), locked)
        assert isinstance(locked.explanation.head, gate.HeadExplanation)
        self.assertEqual(locked.explanation.head.decision.state, "admit")
        self.assertEqual([row.decision.state for row in locked.explanation.line.steps], ["hold"])

    def test_follower_walk_checks_keep_resource_fit_independent_of_earlier_waiter_charge(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {60 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.waiting(300, 15 * GIB, 1)
        _ = self.waiting(400, 10 * GIB, 0)
        reading = gate.queue_read_from(self.snapshot_inputs())
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        follower = reading.explanation.line.steps[0]
        self.assertEqual(head.decision.state, "admit")
        self.assertEqual(follower.decision.state, "hold")
        self.assertTrue(follower.fits_now)
        self.assertFalse(follower.budget.passes)
        self.assertEqual(follower.budget.charged, 15 * GIB)
        self.assertEqual(follower.budget.need, 10 * GIB)
        self.assertEqual((follower.budget, follower.free),
                         gate.decision_checks(follower.decision, 10 * GIB, 60 * GIB))
        self.assertEqual((head.budget, head.free), gate.decision_checks(head.decision, 15 * GIB, 60 * GIB))

    def test_waiter_and_running_identity_matching_and_place_boundaries(self) -> None:
        _ = self.waiting(300, 12 * GIB, 20)
        _ = self.waiting(400, GIB, 5)
        view = gate.queue_read_from(self.snapshot_inputs()).view
        for selector, places in (("1", [1]), ("2", [2]), ("0", []), ("3", []),
                                 ("session-400", [2]), ("worker-400", [2]),
                                 ("/tmp/tree-400", [2]), ("tree-400", [2]), ("missing", [])):
            with self.subTest(selector=selector):
                self.assertEqual(gate.match_waiters(selector, view), places)
        waiter = view.waiting[1]
        running = gate.RunningStep(waiter.session_id, waiter.session_name, waiter.step, waiter.folder,
                                   waiter.need, gate.MeasuredMemory(GIB))
        for step in (waiter, running):
            for selector in ("session-400", "worker-400", "/tmp/tree-400", "tree-400"):
                self.assertTrue(gate.step_matches(selector, step))
            self.assertFalse(gate.step_matches("worker", step))

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
            view.available, host.total, ordered[0][1], [row for _, row in ordered], memories, priority, host.pressure,
            recovery=gate.NoRecoverySpacing(), now=NOW.timestamp()))
        self.assertEqual(head.decision.state, "hold")
        self.assertEqual((host.available, host.total, host.reserve, host.budget),
                         (16 * GIB, 60 * GIB, 3 * GIB, 21 * GIB))
        self.assertEqual((explanation.totals.charged, explanation.totals.measured, explanation.totals.growth),
                         (10 * GIB, 3 * GIB, 7 * GIB))
        self.assertEqual((head.budget.charged, head.budget.need, head.budget.budget,
                          head.budget.starts_at, head.budget.passes),
                         (10 * GIB, 12 * GIB, 21 * GIB, 9 * GIB, False))
        self.assertEqual((head.free.available, head.free.need, head.free.reserve, head.free.growth,
                          head.free.threshold, head.free.passes),
                         (16 * GIB, 12 * GIB, 3 * GIB, 7 * GIB, 22 * GIB, False))
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

    def test_old_head_without_priority_never_reports_a_moving_window(self) -> None:
        path = self.waiting(300, 15 * GIB, 20)
        before = path.read_bytes()
        for now in (NOW.timestamp(), NOW.timestamp() + 60):
            with self.subTest(now=now), patch.object(time, "time", return_value=now):
                reading = gate.locked_queue_read()
                head = reading.explanation.head
                self.assertIsInstance(head, gate.HeadExplanation)
                assert isinstance(head, gate.HeadExplanation)
                self.assertFalse(head.has_priority)
                self.assertIsInstance(head.order, gate.WindowNotStarted)
                self.assertEqual(head.decision.state, "hold")
                self.assertEqual(path.read_bytes(), before)

    def test_recorded_priority_retains_its_fixed_strict_end(self) -> None:
        _ = self.waiting(300, 12 * GIB, 20, bump_order=-1, priority_since=NOW.timestamp() - 60)
        head = gate.explain_queue().head
        assert isinstance(head, gate.HeadExplanation)
        self.assertTrue(head.has_priority)
        self.assertEqual(head.order, gate.StrictUntil(NOW.timestamp() + 540))

    def test_host_pressure_prevents_every_follower_from_counting_as_fitting(self) -> None:
        _ = self.waiting(300, 20 * GIB, 20)
        _ = self.waiting(400, GIB, 1)
        for full in (0, 11):
            with self.subTest(full=full):
                _ = (self.root / "pressure").write_text(
                    f"full avg10={full}.00 avg60=0.00 avg300=0.00 total=0\n")
                explanation = gate.explain_queue()
                self.assertEqual(len(explanation.line.steps), 1)
                self.assertEqual(explanation.line.steps[0].fits_now, full == 0)
                self.assertEqual(explanation.line.fitting, 1 if full == 0 else 0)

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

    def test_recovery_spacing_alone_names_the_start_without_any_endings(self) -> None:
        _ = self.running(100, 6, age=30)
        _ = self.waiting(300, 4 * GIB, 20)
        starts_at = NOW.timestamp() + 37
        gate.write_recovery(gate.admission_dir(self.root), gate.RecoveryStarts(NOW.timestamp() + 120, starts_at))
        explanation = gate.explain_queue()
        for result in (gate.head_after_steps_end(explanation, ()), gate.smallest_head_release(explanation)):
            with self.subTest(result=result):
                if not isinstance(result, gate.HeadStartsAt):
                    self.fail(f"expected a timed start, got {result}")
                self.assertEqual(result.at, starts_at)
                self.assertEqual(result.ended, ())
                self.assertEqual((result.charge_drop, result.freed, result.growth_drop), (0, 0, 0))

    def test_real_pressure_still_holds_after_every_running_step_ends(self) -> None:
        _ = self.running(100, 6, age=30)
        _ = self.waiting(300, 4 * GIB, 20)
        _ = (self.root / "pressure").write_text("full avg10=11.00 avg60=0.00 avg300=0.00 total=0\n")
        explanation = gate.explain_queue()
        for result in (gate.head_after_steps_end(explanation, (0,)), gate.smallest_head_release(explanation)):
            with self.subTest(result=result):
                if not isinstance(result, gate.HeadStillHeld):
                    self.fail(f"expected a pressure hold, got {result}")
                self.assertEqual(result.causes, ("pressure",))

    def test_recovery_spacing_does_not_hide_a_failed_budget_check(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {40 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.running(100, 20, age=30)
        _ = self.waiting(300, 4 * GIB, 20)
        gate.write_recovery(gate.admission_dir(self.root),
                            gate.RecoveryStarts(NOW.timestamp() + 120, NOW.timestamp() + 37))
        result = gate.head_after_steps_end(gate.explain_queue(), ())
        if not isinstance(result, gate.HeadStillHeld):
            self.fail(f"expected budget and timer holds, got {result}")
        self.assertEqual(result.causes, ("build budget", "pressure"))

    def test_recovery_spacing_release_keeps_the_required_endings_and_drops(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {40 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.waiting(300, 4 * GIB, 20)
        starts_at = NOW.timestamp() + 37
        gate.write_recovery(gate.admission_dir(self.root), gate.RecoveryStarts(NOW.timestamp() + 120, starts_at))
        for charges, ended, drop, freed in (((20,), (0,), 20, 1), ((14, 6), (1,), 6, 2)):
            with self.subTest(charges=charges):
                for path in gate.admission_dir(self.root).glob("*.reservation"):
                    path.unlink()
                for pid, charge in zip((100, 200), charges):
                    _ = self.running(pid, charge, age=30)
                result = gate.smallest_head_release(gate.explain_queue())
                if not isinstance(result, gate.HeadStartsAt):
                    self.fail(f"expected endings followed by a timed start, got {result}")
                self.assertEqual(result.at, starts_at)
                self.assertEqual(result.ended, ended)
                self.assertEqual((result.charge_drop, result.freed, result.growth_drop),
                                 (drop * GIB, freed * GIB, (drop - freed) * GIB))

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
