#!/usr/bin/env python3
"""Memory admission with only temporary history, cgroups, and ledgers."""

from __future__ import annotations

from scripts.lint.memory_test_support import admission_configuration_fixture, admission_policy_fixture

import fcntl
import io
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
import contextlib
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.lint import memory_admit as gate, tmp_sweep
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
import conf
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service as service, queue_view
from scripts.build_queue.admission_test_support import AdmissionFixture, InjectedCrash, only_admitted
from scripts.build_queue.queue_fixture import write_memory_config as write_memory_config, write_recovery_fixture, fixture_queue_inputs, write_process
import nextest_budget as budget
from build_shape import BuildShape, build_shape
from memory_test_support import fake_memory_readings, gate_isolation_environment, write_config_fixture, write_config_fault, commit_config_fixture
if TYPE_CHECKING:
    from ..buildlog import store
    from ..buildlog import record as buildlog_record
    from ..buildlog import index as file_marks
    from ..lib import test_hang_guard
    from ..message.roster import UnitLabels
else:
    import store
    import record as buildlog_record
    import index as file_marks
    from scripts.lib import test_hang_guard
    from scripts.message.roster import UnitLabels

READY_FOR_ADMISSION = gate.ReadyForAdmission()


class SnapshotCompatibilityTests(unittest.TestCase):
    def test_older_record_uses_field_default(self) -> None:
        self.assertEqual(gate.parse_snapshot_value(
            {"kind": "MeasuredMemory", "bytes": 12}, gate.MeasuredMemory),
            gate.MeasuredMemory(12))

    def test_newer_record_ignores_unknown_field(self) -> None:
        self.assertEqual(gate.parse_snapshot_value(
            {"kind": "MeasuredMemory", "bytes": 12, "future": {"kind": "UnknownRecord"}},
            gate.MeasuredMemory), gate.MeasuredMemory(12))

    def test_older_record_uses_default_factory(self) -> None:
        self.assertEqual(gate.parse_snapshot_value(
            {"kind": "PeakHold", "default_seconds": 30}, gate.PeakHold), gate.PeakHold(30))

    def test_missing_required_field_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid snapshot record fields"):
            _ = gate.parse_snapshot_value({"kind": "MeasuredMemory"}, gate.MeasuredMemory)

    def test_wrong_or_missing_tag_is_refused(self) -> None:
        for record in ({"bytes": 12}, {"kind": "GoneMemory", "bytes": 12}):
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, "invalid snapshot record tag"):
                _ = gate.parse_snapshot_value(record, gate.MeasuredMemory)

    def test_nested_records_use_defaults_and_ignore_unknown_fields(self) -> None:
        record: dict[str, object] = {
            "kind": "RunningStep", "session_id": "session", "session_name": "worker",
            "step": "nextest", "folder": "/tmp/tree", "promise": 12,
            "measured": {"kind": "MeasuredMemory", "bytes": 5, "future": False},
            "future": {"kind": "UnknownRecord"},
        }
        self.assertEqual(gate.parse_snapshot_value(record, gate.RunningStep),
                         gate.RunningStep("session", "worker", "nextest", "/tmp/tree", 12,
                                          gate.MeasuredMemory(5)))

    def test_former_schema_shims_use_dataclass_defaults(self) -> None:
        records: tuple[object, ...] = (
            gate.StepProfile("repo", "nextest", "tree", gate.ExpectedPeak(12, "measured", 1)),
            gate.MemoryPressure(8),
            gate.RunningStep("session", "worker", "nextest", "/tmp/tree", 12, gate.MeasuredMemory(5)),
            gate.UnownedSliceMemory(5),
        )
        omitted = ("build_history_generation", "condition", "run", "started_at", "activity")
        for original in records:
            with self.subTest(schema=type(original).__name__):
                record = {name: value for name, value in
                          cast(dict[str, object], gate.snapshot_value(original)).items() if name not in omitted}
                record["future"] = "ignored"
                self.assertEqual(gate.parse_snapshot_value(record, type(original)), original)

    def test_nested_required_field_and_tag_are_checked(self) -> None:
        for memory in ({"kind": "MeasuredMemory"}, {"bytes": 5}, {"kind": "UnknownRecord", "bytes": 5}):
            with self.subTest(memory=memory), self.assertRaises(ValueError):
                _ = gate.parse_snapshot_value({
                    "kind": "RunningStep", "session_id": "session", "session_name": "worker",
                    "step": "nextest", "folder": "/tmp/tree", "promise": 12, "measured": memory,
                }, gate.RunningStep)


buildlog_record = gate.record

GIB = gate.GIB
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
NOW_EPOCH = NOW.timestamp()
HISTORY_AT = NOW - timedelta(seconds=1)
FRESH_HISTORY_AT = NOW - timedelta(minutes=5)
LAST_TEST_BUILD_AT = NOW - timedelta(minutes=1)
INVOKE = Path(__file__).with_name("invoke.sh")
MEMORY_GATE = Path(__file__).with_name("memory_gate.sh")
HANA = gate.RepositoryIdentity("https://example.org/nate/hana", "hana", "hana")
CARGO_LINER = gate.RepositoryIdentity("cargo-liner", "cargo-liner", "cargo-liner")
RELEASE_BUILD = ["cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "hana"]
RELEASE_SHAPE = build_shape(RELEASE_BUILD)
HistoryAppendStop = Literal["before newline", "inside UTF-8"]
HISTORY_APPEND_STOPS: tuple[HistoryAppendStop, ...] = ("before newline", "inside UTF-8")


def sample_anon_once(sidecar: Path, maximum: Path) -> subprocess.CompletedProcess[str]:
    program = INVOKE.read_text().split("buildlog_sample_anon() {", 1)[1].split("\n}\n", 1)[0]
    return subprocess.run(["bash", "-c", "buildlog_sample_anon() {" + program +
        '\n}\nsleep() { exit 0; }\nbuildlog_sample_anon "$1" "$2" "$$"',
        "sampler-test", str(sidecar), str(maximum)], capture_output=True, text=True,
        timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)


def buildlog_rows(root: Path) -> list[dict[str, object]]:
    return [cast(dict[str, object], json.loads(line)) for path in root.glob("*/*.jsonl")
            for line in path.read_text().splitlines()]


def write_history_rows(root: Path, rows: Iterable[dict[str, object]], *,
                       indexed: bool = False, crates: int | None = 1) -> None:
    """Write anonymous samples and optional matching Cargo step evidence."""
    path = gate.admission_dir(root) / "anon_peaks.jsonl"
    with path.open("a") as output:
        for original in rows:
            row = dict(original)
            if indexed:
                with closing(sqlite3.connect(file_marks.index_path_in(root))) as db, db:
                    _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, "
                                   + "started_at TEXT, peak_mem_bytes INTEGER, argv TEXT, call_id TEXT, "
                                   + "crates_compiled INTEGER DEFAULT 1, repo_identity TEXT)")
                    columns = {str(value[1]) for value in cast(list[tuple[object, ...]],
                               db.execute("PRAGMA table_info(steps)").fetchall())}
                    for name, kind in (("argv", "TEXT"), ("call_id", "TEXT"),
                                       ("crates_compiled", "INTEGER")):
                        if name not in columns:
                            _ = db.execute(f"ALTER TABLE steps ADD COLUMN {name} {kind}")
                    call_id = row.get("call_id")
                    if not call_id:
                        call_id = f"sample-{output.tell()}"
                        row["call_id"] = call_id
                    _ = db.execute("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes,argv,"
                                   + "call_id,crates_compiled) VALUES (?,?,?,?,?,?,?,?)",
                                   (row.get("host"), row.get("repo"), row.get("step"),
                                    store.utc_iso((datetime.fromisoformat(str(row["ended_at"])) - timedelta(seconds=1)).timestamp()),
                                    0, json.dumps(["cargo", row.get("step")]), call_id, crates))
            _ = output.write(json.dumps(row) + "\n")




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
    _ = unittest.enterModuleContext(fake_cargo_output_roots())
    _ = write_config_fixture(root / "build_admission.conf", "build_budget_gib=21")
    _ = (root / "meminfo").write_text("MemTotal: 67108864 kB\nMemAvailable: 67108864 kB\n")
    _ = (root / "pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        + "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    _ = cast(object, unittest.enterModuleContext(patch.dict(os.environ, {
        "BUILDLOG_DIR": str(root / "buildlog"),
        "BUILDLOG_MEMINFO": str(root / "meminfo"),
        "BUILDLOG_MEMORY_PRESSURE": str(root / "pressure"),
        "BUILDLOG_LIVE_MEMORY_TRIPWIRE": os.environ.get(
            "BUILDLOG_LIVE_MEMORY_TRIPWIRE", str(root / "live-memory-reads.jsonl")
        ),
        "BUILDLOG_BUILD_BUDGET_GIB": "20",
        "LINT_CONFIG_FILE": str(root / "lint.conf"), "CONF_DIR": str(Path(root / "build_admission.conf").parent),
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


class BuildBudgetTests(unittest.TestCase):
    config: Path = Path()

    @override
    def setUp(self) -> None:
        self.config = Path(self.enterContext(tempfile.TemporaryDirectory())) / "build_admission.conf"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"LINT_CONFIG_FILE": str(self.config.with_name("lint.conf")), "CONF_DIR": str(Path(self.config).parent)})))
        _ = os.environ.pop("BUILDLOG_BUILD_BUDGET_GIB", None)
        _ = write_config_fixture(self.config, "build_budget_gib=21")
        commit_config_fixture(self.config)

    def test_positive_environment_budget_wins_without_reading_config(self) -> None:
        _ = write_config_fault(self.config, "[memory]\nbuild_budget_gib=7.25\n")
        with patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "31.5"}), \
             patch.object(conf.ConfigRead, "number", side_effect=AssertionError("environment wins")):
            self.assertEqual(gate.build_budget_bytes(), int(31.5 * GIB))

    def test_config_budget_is_read_again_after_each_change(self) -> None:
        for budget in ("7.25", "8"):
            _ = write_config_fault(self.config, f"[memory]\nbuild_budget_gib={budget} # trial\n")
            self.assertEqual(gate.build_budget_bytes(), int(float(budget) * GIB))
            self.assertEqual(gate.decide(60 * GIB, 60 * GIB, GIB, []).budget,
                             int(float(budget) * GIB))
            self.assertEqual(gate.AdmissionDecision("admit", 0, 0, 0, 0).budget,
                             int(float(budget) * GIB))

    def test_invalid_environment_budget_uses_config(self) -> None:
        _ = write_config_fault(self.config, "[memory]\nbuild_budget_gib=9\n")
        for budget in ("", "bad", "0", "-1", "nan", "inf", "1e999"):
            with self.subTest(budget=budget), patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": budget}):
                self.assertEqual(gate.build_budget_bytes(), 9 * GIB)

    def test_missing_or_invalid_config_budget_uses_the_fallback(self) -> None:
        self.assertEqual(gate.build_budget_bytes(), 21 * GIB)
        for setting in ("[memory]\n", *(f"[memory]\nbuild_budget_gib={value}\n"
                         for value in ("", "bad", "0", "-1", "nan", "inf", "1e999"))):
            with self.subTest(setting=setting):
                _ = write_config_fault(self.config, setting)
                self.assertEqual(gate.build_budget_bytes(), 21 * GIB)


    def test_running_pass_interval_uses_fallback_and_reads_config_at_each_use(self) -> None:
        self.assertEqual(gate.read_memory_config().running_pass_seconds, 5)
        for raw in ("", "abc", "-5", "nan"):
            with self.subTest(raw=raw):
                _ = write_config_fault(self.config, f"[memory]\nrunning_pass_seconds={raw}\n")
                self.assertEqual(gate.read_memory_config().running_pass_seconds, 5)
        for seconds in (3, 7.5):
            _ = write_config_fault(self.config, f"[memory]\nrunning_pass_seconds={seconds}\n")
            self.assertEqual(gate.read_memory_config().running_pass_seconds, seconds)

    def test_service_intervals_use_positive_finite_config_and_read_each_use(self) -> None:
        readers = (
            ("waiting_pass_seconds", lambda: gate.read_memory_config().waiting_pass_seconds, 1),
            ("wait_reply_seconds", lambda: gate.read_memory_config().wait_reply_seconds, 60),
            ("query_timeout_seconds", lambda: gate.read_memory_config().query_timeout_seconds, 0.5),
            ("replay_retention_seconds", lambda: gate.read_memory_config().replay_retention_seconds, 86400),
            ("reconnect_seconds", lambda: gate.read_memory_config().reconnect_seconds, 2),
            ("silence_seconds", lambda: gate.read_memory_config().silence_seconds, 120),
            ("outage_notice_seconds", lambda: gate.read_memory_config().outage_notice_seconds, 60),
            ("source_check_seconds", lambda: gate.read_memory_config().source_check_seconds, 1),
            ("source_settle_seconds", lambda: gate.read_memory_config().source_settle_seconds, 2),
            ("override_reconnect_seconds", lambda: gate.read_memory_config().override_reconnect_seconds, 10),
        )
        for key, reader, fallback in readers:
            with self.subTest(key=key):
                _ = write_config_fault(self.config, "[memory]\n")
                self.assertEqual(reader(), fallback)
                for raw in ("abc", "-5", "nan", "inf", "0"):
                    _ = write_config_fault(self.config, f"[memory]\n{key}={raw}\n")
                    self.assertEqual(reader(), fallback)
                for seconds in (3, 7.5):
                    _ = write_config_fault(self.config, f"[memory]\n{key}={seconds}\n")
                    self.assertEqual(reader(), seconds)

    def test_request_size_limit_uses_positive_integer_config_at_each_use(self) -> None:
        self.assertEqual(gate.read_memory_config().request_max_bytes, 1048576)
        for raw in ("", "abc", "-5", "nan", "inf", "0", "1.5"):
            with self.subTest(raw=raw):
                _ = write_config_fault(self.config, f"[memory]\nrequest_max_bytes={raw}\n")
                self.assertEqual(gate.read_memory_config().request_max_bytes, 1048576)
        for size in (70000, 2097152):
            _ = write_config_fault(self.config, f"[memory]\nrequest_max_bytes={size}\n")
            self.assertEqual(gate.read_memory_config().request_max_bytes, size)


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

    def test_positive_budget_override_changes_admission_and_invalid_values_use_fallback(self) -> None:
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
        decision = gate.decide(40 * GIB, 60 * GIB, GIB, [], pressure=gate.MemoryPressure(11, condition="full"))
        self.assertEqual(decision.state, "hold")

    def test_empty_machine_cannot_skip_the_budget_reserved_for_a_head(self) -> None:
        head: gate.Waiter = {"pid": 1, "start_time": "1", "need": int(19.3 * GIB), "since": 0,
                             "priority_since": NOW.timestamp()}
        younger: gate.Waiter = {"pid": 2, "start_time": "2", "need": int(10.8 * GIB), "since": 1}
        decision = gate.admission_walk(40 * GIB, 60 * GIB, [head, younger], [],
                                       gate.HeadPriority(head, gate.StrictUntil(NOW.timestamp() + 600)),
                                       gate.NoPressureHold(), recovery=gate.NoRecoverySpacing(), now=NOW.timestamp(), policy=admission_policy_fixture())[1].decision
        self.assertEqual(decision.state, "hold")



class HistoryTests(unittest.TestCase):
    root: Path = Path()
    admission: Path = Path()
    host: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root)))
        self.admission = self.root / "admission"
        self.admission.mkdir()
        self.host = "test-host"

    def add_measured(self, count: int, *, host: str = "test-host", repo: str = "hana",
                     at: datetime = HISTORY_AT, test_threads: int | None = None,
                     start_gib: int = 1) -> None:
        rows: list[dict[str, object]] = []
        for index in range(count):
            row: dict[str, object] = {
                "host": host, "repo": repo, "step": "nextest",
                "ended_at": store.utc_iso(at.timestamp()),
                "anon_peak_bytes": (start_gib + index) * GIB,
            }
            if test_threads is not None:
                row["test_threads"] = test_threads
            rows.append(row)
        write_history_rows(self.root, rows, indexed=True)

    def add_index(self, *, at: datetime = HISTORY_AT) -> None:
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, peak_mem_bytes INTEGER, crates_compiled INTEGER DEFAULT 1)")
            for index in range(5):
                _ = db.execute("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes) VALUES (?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", store.utc_iso(at.timestamp()), (index + 1) * GIB))

    def peak(self) -> gate.ExpectedPeak:
        return gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)

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
        self.assertEqual(self.peak(), gate.ExpectedPeak(12 * GIB, "fallback", 0))

    def test_a_repo_with_few_runs_keeps_its_largest_peak_below_the_pool(self) -> None:
        self.add_measured(3, repo="other")
        self.add_measured(4, start_gib=10)
        self.assertEqual(self.peak(), gate.ExpectedPeak(13 * GIB, "measured", 4))

    def test_own_buildlog_history_comes_before_the_pooled_p90(self) -> None:
        self.add_index()
        self.add_measured(5, repo="other")
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_missing_index_or_history_is_fallback(self) -> None:
        self.assertEqual(self.peak().source, "fallback")
        self.add_index()
        self.assertEqual(self.peak().source, "buildlog")

    def test_legacy_index_without_compile_counts_keeps_its_clippy_peaks(self) -> None:
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT)")
            _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", [
                (self.host, "hana", "clippy", HISTORY_AT.isoformat(), 4 * GIB,
                 json.dumps(["cargo", "clippy"]))
            ] * 5)
        config = admission_configuration_fixture()
        peak = gate.expected_peak(HANA, "clippy", self.host, NOW, self.root,
                                  first_peak_bytes=config.program_first_peaks["cargo"],
                                  budget=config.build_budget_bytes)
        self.assertEqual(peak, gate.ExpectedPeak(int(4 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_measured_history_pools_every_width_unless_one_is_named(self) -> None:
        self.add_measured(5, test_threads=16)
        self.add_measured(5, test_threads=4, start_gib=6)
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, test_threads=16, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
            gate.ExpectedPeak(5 * GIB, "measured", 5),
        )
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
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
        self.assertEqual(self.peak(), gate.ExpectedPeak(12 * GIB, "fallback", 0))

    def test_buildlog_tier_keeps_filtered_runs_separate(self) -> None:
        self.add_index()
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("ALTER TABLE steps ADD COLUMN argv TEXT")
            _ = db.execute("UPDATE steps SET argv = ?", (json.dumps(["cargo", "nextest", "run"]),))
            for _ in range(5):
                _ = db.execute("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes,argv) VALUES (?, ?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), 2 * GIB,
                                json.dumps(["cargo", "nextest", "run", "-E", "test(one)"])))
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope="filtered", first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
                         gate.ExpectedPeak(int(2 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_scratch_checkout_cannot_take_legacy_buildlog_samples_by_repository_name(self) -> None:
        self.add_index()
        scratch = gate.RepositoryIdentity(HANA.key, HANA.name)
        self.assertEqual(gate.expected_peak(scratch, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes).source,
                         "fallback")


class FilteredPromiseTests(unittest.TestCase):
    root: Path = Path()
    host: str = "test-host"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root)))
        (self.root / "admission").mkdir()

    def test_package_check_keeps_legacy_measured_peaks_without_package_fields(self) -> None:
        arguments = ["cargo", "check", "-p", "hana"]
        rows: list[dict[str, object]] = [{
            "host": self.host, "repo": HANA.legacy_name, "repo_identity": HANA.key,
            "step": "check", "anon_peak_bytes": 2 * GIB,
            "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
        }] * 5
        write_history_rows(self.root, rows, indexed=True)
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("UPDATE steps SET argv=?", (json.dumps(arguments),))
        config = admission_configuration_fixture()
        estimate = gate.expected_peak(
            HANA, "check", self.host, NOW, self.root,
            package=gate.test_package(arguments), scope=gate.test_scope(arguments),
            first_peak_bytes=config.program_first_peaks["cargo"], budget=config.build_budget_bytes)
        self.assertEqual(estimate, gate.ExpectedPeak(2 * GIB, "measured", 5))

    def samples(self, peaks: list[int], *, package: str = "hana", filters: tuple[str, ...] = (),
                scope: gate.TestScope = "filtered", step: str = "nextest",
                repository: gate.RepositoryIdentity = HANA, host: str = "test-host",
                at: datetime = HISTORY_AT, crates: int | None = 1,
                keyed: bool = True, indexed: bool = True) -> None:
        write_history_rows(self.root, ({
            "host": host, "repo": repository.name, "repo_identity": repository.key if keyed else "",
            "step": step, "test_scope": scope, "package": package,
            "filters": list(filters), "anon_peak_bytes": peak,
            "ended_at": store.utc_iso(at.timestamp()),
        } for peak in peaks), indexed=indexed, crates=crates)

    def legacy_selection_sample(self, call_id: str, anonymous_peak: int,
                                indexed_peak: int, expression: str) -> None:
        """Keep the selection only in argv, as the older recorder did."""
        write_history_rows(self.root, [{
            "host": self.host, "repo": CARGO_LINER.legacy_name, "step": "nextest",
            "call_id": call_id, "anon_peak_bytes": anonymous_peak,
            "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
        }], indexed=True)
        arguments = ["cargo", "nextest", "run", "-E", expression]
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("UPDATE steps SET argv=?, peak_mem_bytes=? WHERE call_id=?",
                           (json.dumps(arguments), indexed_peak, call_id))

    def test_legacy_exact_selection_uses_its_anonymous_peak_before_the_indexed_peak(self) -> None:
        # The cargo-tile row supplied the indexed_exact tier on 2026-10-22.
        # Its exact and package histories contain the same anonymous row.
        names = ("an_excluded_command_after_a_valued_global_option_is_excluded",
                 "cargo_offline_port_passes_straight_through_the_shim",
                 "the_installed_shim_skips_every_valued_global_option")
        expression = "package(cargo-tile) & (" + " | ".join(f"test({name})" for name in names) + ")"
        self.legacy_selection_sample("v20261018T004601-3696591-31646",
                                     1_114_603_520, 2_897_424_384, expression)
        estimate = self.promise(names, repository=CARGO_LINER, package="cargo-tile")
        self.assertEqual(estimate, gate.ExpectedPeak(1_114_603_520, "measured", 1))

    def test_legacy_package_selection_recovers_anonymous_peaks_for_an_unseen_filter(self) -> None:
        # These cargo-handler rows supplied the indexed tier on 2026-10-21.
        # No exact row matches the new filter, so the package p90 sets the promise.
        samples = (
            ("v20261021T155249-4110155-296", 833_626_112, 3_038_146_560,
             "light_running_and_waiting_stems"),
            ("v20261021T152015-1436029-2533", 828_477_440, 2_620_203_008,
             "shell_running_uses_agent_running_ink"),
            ("v20261021T141843-3388864-5330", 1_114_439_680, 3_182_821_376,
             "default_tree_sort_preserves"),
            ("v20261021T140931-2292796-12929", 1_125_695_488, 3_289_464_832,
             "shell_rows_draw_running"),
            ("v20261021T130628-2237327-5786", 530_890_752, 2_714_832_896,
             "unassigned_and_unlabeled_role_cells"),
        )
        for call_id, anonymous_peak, indexed_peak, name in samples:
            self.legacy_selection_sample(call_id, anonymous_peak, indexed_peak,
                                         f"package(cargo-handler) & test({name})")
        estimate = self.promise(("new_filter",), repository=CARGO_LINER, package="cargo-handler")
        self.assertEqual(estimate, gate.ExpectedPeak(1_125_695_488, "measured", 5))

    def test_full_package_selection_excludes_other_packages_from_legacy_anonymous_history(self) -> None:
        # The full cargo-handler tier mixed these packages on 2026-10-21.
        samples = (
            ("cargo-handler", "v20261021T153138-2292084-16164", 671_563_776, 2_940_760_064),
            ("cargo-handler", "v20261021T150957-577237-15636", 709_586_944, 3_062_943_744),
            ("cargo-handler", "v20261021T144821-2738321-14641", 670_871_552, 3_004_026_880),
            ("cargo-handler", "v20261021T143045-736918-24002", 1_101_647_872, 2_983_149_568),
            ("cargo-handler", "v20261021T134700-3850863-2816", 1_168_596_992, 2_912_776_192),
            ("cargo-berth", "v20261021T115802-2170238-2089", 1_721_237_504, 4_158_533_632),
            ("cargo-berth", "v20261021T115036-1012290-20085", 1_943_429_120, 4_275_789_824),
            ("cargo-berth", "v20261021T114640-618929-30845", 952_893_440, 3_459_043_328),
            ("cargo-berth", "v20261021T114115-4061014-30821", 1_681_874_944, 4_025_790_464),
            ("cargo-berth", "v20261021T113635-3464735-28770", 1_779_761_152, 4_688_084_992),
        )
        for package, call_id, anonymous_peak, indexed_peak in samples:
            self.legacy_selection_sample(call_id, anonymous_peak, indexed_peak,
                                         f"package({package})")
        estimate = self.promise((), repository=CARGO_LINER, package="cargo-handler", scope="full")
        self.assertEqual(estimate, gate.ExpectedPeak(1_168_596_992, "measured", 5))

    def test_a_larger_package_peak_protects_a_lower_exact_filter_peak(self) -> None:
        # These rows appear in promise_sources.json as anon:30269 and anon:30112.
        self.legacy_selection_sample(
            "v20261021T155249-4110155-296", 833_626_112, 3_038_146_560,
            "package(cargo-handler) & test(light_running_and_waiting_stems)")
        self.legacy_selection_sample(
            "v20261021T140931-2292796-12929", 1_125_695_488, 3_289_464_832,
            "package(cargo-handler) & (test(shell_rows_draw_running) | test(shell_timer_status_keeps)"
            + " | test(status_cells_keep_glyph) | test(showrunner_session_sort_ignores)"
            + " | test(unassigned_session_sort_ignores) | test(summary_name_sort_ignores))")
        estimate = self.promise(("light_running_and_waiting_stems",),
                                repository=CARGO_LINER, package="cargo-handler")
        self.assertEqual(estimate, gate.ExpectedPeak(1_125_695_488, "measured", 2))

    def test_five_keyed_runs_exclude_older_unkeyed_peaks(self) -> None:
        self.samples([19 * GIB] * 10, scope="full", keyed=False)
        self.samples([2 * GIB] * 4, scope="full")
        before = gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                                    first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"],
                                    budget=admission_configuration_fixture().build_budget_bytes)
        self.assertEqual((before.bytes, before.count), (19 * GIB, 14))
        self.samples([2 * GIB], scope="full")
        after = gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                                   first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"],
                                   budget=admission_configuration_fixture().build_budget_bytes)
        self.assertEqual((after.bytes, after.count), (2 * GIB, 5))

    def test_sparse_repository_history_uses_own_largest_or_a_lower_pool(self) -> None:
        parent = self.root
        other = gate.RepositoryIdentity("https://example.org/nate/other", "other", "other")
        for count in range(1, 5):
            for pool, expected in ((18, 6), (2, 2)):
                with self.subTest(own_runs=count, pool=pool):
                    self.root = parent / f"{count}-{pool}"
                    (self.root / "admission").mkdir(parents=True)
                    self.samples([6 * GIB] * count, scope="full")
                    self.samples([pool * GIB] * 50, scope="full", repository=other)
                    peak = gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                                              first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"],
                                              budget=admission_configuration_fixture().build_budget_bytes)
                    self.assertEqual(peak.bytes, expected * GIB)
        self.root = parent

    def test_compile_history_excludes_known_zero_counts_and_keeps_unknown_counts(self) -> None:
        self.samples([7 * GIB], filters=("native_rate",), crates=3)
        self.samples([20 * GIB] * 10, filters=("native_rate",), crates=0)
        self.assertEqual(self.promise(), gate.ExpectedPeak(7 * GIB, "measured", 1))
        self.samples([21 * GIB] * 10, filters=("native_rate",), crates=None)
        self.assertEqual(self.promise(), gate.ExpectedPeak(21 * GIB, "measured", 11))

    def test_unknown_attempt_between_zero_attempts_keeps_its_compile_peak(self) -> None:
        self.samples([7 * GIB], filters=("native_rate",), crates=0)
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            for count in (None, 0):
                _ = db.execute("INSERT INTO steps(host,repo,step,started_at,call_id,crates_compiled) "
                               + "SELECT host,repo,step,started_at,call_id,? FROM steps LIMIT 1", (count,))
        self.assertEqual(self.promise(), gate.ExpectedPeak(7 * GIB, "measured", 1))

    def test_legacy_index_without_compile_counts_keeps_its_filtered_nextest_peaks(self) -> None:
        arguments = ["cargo", "nextest", "run", "-E", "package(hana) & test(native_rate)"]
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT)")
            _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", [
                (self.host, "hana", "nextest", HISTORY_AT.isoformat(), 8 * GIB, json.dumps(arguments))
            ] * 5)
        peak = gate.filtered_peak(HANA, "nextest", self.host, NOW, self.root,
                                  None, ("native_rate",), "hana")
        self.assertEqual(peak, gate.ExpectedPeak(int(8 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_missing_call_and_step_matches_keep_peaks_until_classification(self) -> None:
        self.samples([3 * GIB], filters=("native_rate",))
        self.samples([20 * GIB] * 10, filters=("native_rate",), indexed=False)
        write_history_rows(self.root, [{
            "host": self.host, "repo": "hana", "repo_identity": HANA.key,
            "step": "nextest", "test_scope": "filtered", "package": "hana",
            "filters": ["native_rate"], "anon_peak_bytes": 21 * GIB,
            "call_id": "different-step", "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
        }])
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("INSERT INTO steps(host,repo,step,started_at,call_id,crates_compiled) "
                           + "VALUES (?,?,?,?,?,?)",
                           (self.host, "hana", "check", HISTORY_AT.isoformat(), "different-step", 5))
        self.assertEqual(self.promise(), gate.ExpectedPeak(20 * GIB, "measured", 12))

    def test_gate_history_remains_saved_without_entering_cargo_estimates(self) -> None:
        self.samples([3 * GIB] * 5, scope="full")
        self.samples([20 * GIB] * 10, scope="full", step="merge-gate", indexed=False)
        path = self.root / "admission/anon_peaks.jsonl"
        before = path.read_bytes()
        peak = gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                                  first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"],
                                  budget=admission_configuration_fixture().build_budget_bytes)
        self.assertEqual(peak, gate.ExpectedPeak(3 * GIB, "measured", 5))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sum(row["step"] == "merge-gate" for row in gate.anonymous_history_rows(path)), 10)

    def promise(self, filters: tuple[str, ...] = ("native_rate",), *,
                repository: gate.RepositoryIdentity = HANA, package: str = "hana",
                scope: gate.TestScope = "filtered") -> gate.ExpectedPeak:
        config = admission_configuration_fixture()
        return gate.expected_peak(repository, "nextest", self.host, NOW, self.root,
                                  scope=scope, package=package, filters=filters,
                                  first_peak_bytes=config.program_first_peaks["cargo"],
                                  budget=config.build_budget_bytes)

    def test_unfinished_rows_do_not_change_the_filtered_estimate(self) -> None:
        self.samples([3 * GIB], filters=("native_rate",))
        assert_unfinished_history_ignored(
            self, self.root / "admission/anon_peaks.jsonl",
            lambda: gate.filtered_peak(HANA, "nextest", self.host, NOW, self.root,
                                       None, ("native_rate",), "hana"))

    def test_filter_name_order_keeps_the_larger_exact_p90(self) -> None:
        self.samples([number * GIB for number in range(11, 21)], filters=("render", "native_rate"))
        self.samples([14 * GIB] * 10, filters=("other",))
        self.samples([19 * GIB] * 10, scope="full")
        for filters in (("native_rate", "render"), ("render", "native_rate")):
            with self.subTest(filters=filters):
                self.assertEqual(self.promise(filters), gate.ExpectedPeak(19 * GIB, "measured", 10))

    def test_a_lower_exact_sample_keeps_the_larger_package_p90(self) -> None:
        self.samples([2 * GIB], filters=("native_rate",))
        self.samples([9 * GIB] * 10, filters=("render",))
        self.assertEqual(self.promise(), gate.ExpectedPeak(9 * GIB, "measured", 11))

    def test_unseen_filter_set_uses_all_filtered_runs_of_the_same_package(self) -> None:
        self.samples([number * GIB for number in range(1, 6)], filters=("render",))
        self.samples([number * GIB for number in range(6, 11)], filters=("audio",))
        self.samples([17 * GIB] * 10, filters=("native_rate",), package="hana-ui")
        other = gate.RepositoryIdentity("https://example.org/nate/other", "other", "other")
        self.samples([18 * GIB] * 10, filters=("native_rate",), repository=other)
        self.samples([19 * GIB] * 10, scope="full")
        self.assertEqual(self.promise(), gate.ExpectedPeak(9 * GIB, "measured", 10))

    def test_measured_compile_peak_below_the_floor_keeps_its_promise(self) -> None:
        compile_peak = gate.FILTERED_FLOOR // 2
        self.samples([compile_peak] * 5, step="check", scope="full")
        self.samples([19 * GIB] * 5, scope="full")
        self.assertEqual(self.promise().bytes, compile_peak)

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
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT, crates_compiled INTEGER DEFAULT 1)")
            for package, peak in (("hana", 8 * GIB), ("hana-ui", 20 * GIB)):
                _ = db.executemany("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes,argv) VALUES (?, ?, ?, ?, ?, ?)", [
                    (self.host, "hana", "check", store.utc_iso(HISTORY_AT.timestamp()), peak,
                     json.dumps(["cargo", "check", "-p", package]))
                ] * 5)
        self.assertEqual(self.promise().bytes, max(gate.FILTERED_FLOOR, int(8 * GIB * gate.ANON_SHARE)))

    def test_package_anonymous_history_precedes_exact_indexed_history(self) -> None:
        self.samples([19 * GIB] * 10, filters=("other",))
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT, crates_compiled INTEGER DEFAULT 1)")
            for package, names, peaks in (
                ("hana", "test(render) | test(native_rate)", list(range(1, 11))),
                ("hana-ui", "test(render) | test(native_rate)", [20] * 10),
                ("hana", "test(other)", [21] * 10),
            ):
                argv = ["cargo", "nextest", "run", "-E", f"package({package}) & ({names})"]
                _ = db.executemany("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes,argv) VALUES (?, ?, ?, ?, ?, ?)", [
                    (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), peak * GIB,
                     json.dumps(argv)) for peak in peaks
                ])
        self.assertEqual(self.promise(("native_rate", "render")),
                         gate.ExpectedPeak(19 * GIB, "measured", 10))

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
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
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
                               package=gate.test_package(argv), filters=gate.test_filters(argv), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
            gate.ExpectedPeak(15 * GIB, "measured", 10),
        )

    def test_negated_index_expression_cannot_supply_a_positive_exact_promise(self) -> None:
        self.samples([9 * GIB] * 5, filters=("other",))
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT, crates_compiled INTEGER DEFAULT 1)")
            for expression in ("package(hana) & not test(native_rate)", "package(hana) & !test(native_rate)"):
                _ = db.execute("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes,argv) VALUES (?, ?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), GIB,
                                json.dumps(["cargo", "nextest", "run", "-E", expression])))
        self.assertEqual(self.promise(), gate.ExpectedPeak(9 * GIB, "measured", 5))

    def test_unscoped_filtered_run_without_history_retains_the_legacy_fallback(self) -> None:
        argv = ["cargo", "nextest", "run", "--workspace", "-E", "test(native_rate)"]
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope=gate.test_scope(argv),
                               package=gate.test_package(argv), filters=gate.test_filters(argv), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
            gate.ExpectedPeak(12 * GIB, "fallback", 0),
        )

    def test_unscoped_filtered_run_uses_its_single_measured_peak(self) -> None:
        self.samples([2 * GIB], package="", filters=("native_rate",))
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                               scope="filtered", filters=("native_rate",), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
            gate.ExpectedPeak(2 * GIB, "measured", 1),
        )

    def test_unscoped_filtered_run_keeps_its_own_peaks_below_the_pool(self) -> None:
        self.samples([2 * GIB] * 3, package="")
        other = gate.RepositoryIdentity("https://example.org/nate/other", "other", "other")
        self.samples([9 * GIB] * 5, package="", repository=other)
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope="filtered", first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
            gate.ExpectedPeak(2 * GIB, "measured", 3),
        )

    def test_no_filtered_history_uses_same_package_build_measurements(self) -> None:
        self.samples([8 * GIB] * 5, step="build", scope="full")
        self.samples([20 * GIB] * 5, step="build", scope="full", package="hana-ui")
        self.assertEqual(self.promise(), gate.ExpectedPeak(8 * GIB, "measured", 5))

    def test_build_measurements_precede_compile_index_history(self) -> None:
        self.samples([8 * GIB] * 5, step="build", scope="full")
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT, crates_compiled INTEGER DEFAULT 1)")
            _ = db.executemany("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes,argv) VALUES (?, ?, ?, ?, ?, ?)", [
                (self.host, "hana", "check", store.utc_iso(HISTORY_AT.timestamp()), 20 * GIB,
                 json.dumps(["cargo", "check", "-p", "hana"]))
            ] * 5)
        self.assertEqual(self.promise(), gate.ExpectedPeak(8 * GIB, "measured", 5))

    def test_no_filtered_history_uses_same_package_build_index_measurements(self) -> None:
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT, crates_compiled INTEGER DEFAULT 1)")
            for package, peak in (("hana", 8 * GIB), ("hana-ui", 20 * GIB)):
                _ = db.executemany("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes,argv) VALUES (?, ?, ?, ?, ?, ?)", [
                    (self.host, "hana", "build", store.utc_iso(HISTORY_AT.timestamp()), peak,
                     json.dumps(["cargo", "build", "-p", package]))
                ] * 5)
        self.assertEqual(self.promise(), gate.ExpectedPeak(int(8 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_indexed_member_compile_promise_can_exceed_the_full_suite(self) -> None:
        self.samples([12 * GIB] * 5, scope="full")
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                           + "peak_mem_bytes INTEGER, argv TEXT, crates_compiled INTEGER DEFAULT 1)")
            _ = db.executemany("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes,argv) VALUES (?, ?, ?, ?, ?, ?)", [
                (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), 30 * GIB,
                 json.dumps(["cargo", "nextest", "run", "-E",
                             "package(hana_diegetic) & test(native_rate)"]))
            ] * 66)
        for filters in (("native_rate",), ("another_test",)):
            with self.subTest(filters=filters):
                self.assertEqual(
                    gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                                       scope="filtered", package="hana_diegetic", filters=filters, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
                    gate.ExpectedPeak(int(30 * GIB * gate.ANON_SHARE), "buildlog", 66),
                )
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
                         gate.ExpectedPeak(12 * GIB, "measured", 5))

    def test_exact_compile_measurements_can_exceed_the_full_suite_promise(self) -> None:
        self.samples([7 * GIB] * 5, scope="full")
        self.samples([20 * GIB], filters=("native_rate",))
        self.samples([2 * GIB], filters=("small_test",))
        self.assertEqual(self.promise(), gate.ExpectedPeak(20 * GIB, "measured", 1))
        self.assertEqual(self.promise(("small_test",)), gate.ExpectedPeak(20 * GIB, "measured", 2))

    def test_compile_fallback_can_exceed_the_full_suite_promise(self) -> None:
        self.samples([4 * GIB] * 5, scope="full")
        self.samples([8 * GIB] * 5, step="build", scope="full")
        self.assertEqual(self.promise(), gate.ExpectedPeak(8 * GIB, "measured", 5))

    def test_compile_estimate_uses_the_requested_test_width(self) -> None:
        for scope, width, peak, count in (("full", 16, 6 * GIB, 5),
                                           ("full", 4, 30 * GIB, 5),
                                           ("filtered", 16, 20 * GIB, 1),
                                           ("filtered", 4, 30 * GIB, 1)):
            for _ in range(count):
                write_history_rows(self.root, [{
                    "host": self.host, "repo": HANA.name, "repo_identity": HANA.key,
                    "step": "nextest", "test_scope": scope, "test_threads": width,
                    "package": "hana", "filters": ["native_rate"], "anon_peak_bytes": peak,
                    "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
                }], indexed=True)
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, test_threads=16,
                               scope="filtered", package="hana", filters=("native_rate",), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes),
            gate.ExpectedPeak(20 * GIB, "measured", 1),
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
        self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root)))
        self.worktree = self.root / "checkout"
        (self.worktree / "target").mkdir(parents=True)
        self.state = self.root / "sweep-state"
        self.state.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {tmp_sweep.STATE_ENV: str(self.state)})))
        (self.root / "admission").mkdir()
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
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
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            for peak in peaks:
                self.sample_id += 1
                call = f"call-{self.sample_id}"
                timestamp = store.utc_iso(at.timestamp())
                _ = db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (host, "hana", "nextest", timestamp, timestamp, 20 * GIB,
                                json.dumps(["cargo", "nextest", "run", "-E", expression]), call,
                                "/another/history-worktree", "historical-key", status, tests_run, crates, 0))
                if measured:
                    write_history_rows(self.root, [{
                        "host": host, "repo": "hana", "repo_identity": HANA.key, "step": "nextest",
                        "package": package, "test_scope": scope, "filters": list(filters),
                        "ended_at": timestamp, "anon_peak_bytes": peak, "call_id": call,
                    }])

    def previous_build(self, *, key: str = "current-key", worktree: str = "", package: str = "hana",
                       scope: gate.TestScope = "filtered", status: int = 0, tests_run: int = 1,
                       at: datetime = LAST_TEST_BUILD_AT, finished: bool = True,
                       indexed: bool = True, recorded_build_key: bool = True) -> None:
        expression = f"package({package})" + (" & test(other_filter)" if scope == "filtered" else "")
        timestamp = store.utc_iso(at.timestamp())
        arguments = ["cargo", "nextest", "run", "-E", expression]
        if indexed:
            with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
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
            marks = file_marks.marks(handle, offset)
        source = path.relative_to(self.root).as_posix()
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("ALTER TABLE steps ADD COLUMN test_build_key TEXT")
            _ = db.execute("ALTER TABLE steps ADD COLUMN src TEXT")
            _ = db.execute("UPDATE steps SET test_build_key = tree_key, src = ? WHERE worktree = ?",
                           (source, str(self.worktree)))
            _ = db.execute("CREATE TABLE files(path TEXT PRIMARY KEY, offset INTEGER, marks TEXT)")
            _ = db.execute("INSERT INTO files VALUES (?, ?, ?)", (source, offset, marks))
        return path

    def fresh(self, *, key: str = "current-key", scope: gate.TestScope = "filtered",
              target_dir: str = "target", build_dir: str = "") -> bool:
        roots = (CargoOutputUnavailable("the target root is unknown")
                 if target_dir in ("", "unknown", "unset")
                 else CargoOutputRoots(self.worktree.resolve(),
                                       (self.worktree / target_dir).resolve(),
                                       (self.worktree / (build_dir or target_dir)).resolve()))
        return gate.is_fresh_test(self.root, self.host, str(self.worktree), "hana", scope, key, NOW,
                                  roots)

    def promise(self, *, key: str = "current-key", scope: gate.TestScope = "filtered",
                filters: tuple[str, ...] = ("native_rate",)) -> gate.ExpectedPeak:
        return gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope=scope,
                                  package="hana", filters=filters, fresh=self.fresh(key=key, scope=scope), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)

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
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
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
                    patch.object(store, "host_name", return_value=self.host), \
                    patch.object(store, "host_file", return_value=path):
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

    def test_freshness_reads_the_current_shared_sweep_state(self) -> None:
        self.previous_build()
        self.assertTrue(self.fresh())
        other = self.root / "other sweep state"
        other.mkdir()
        _ = (other / "target-deletes.json").write_text(json.dumps({
            str(self.worktree / "target"): (NOW - timedelta(seconds=30)).timestamp(),
        }))
        with patch.dict(os.environ, {tmp_sweep.STATE_ENV: str(other)}):
            self.assertFalse(self.fresh())
        self.assertTrue(self.fresh())
        with patch.object(tmp_sweep, "state_dir", return_value=other) as authority:
            self.assertFalse(self.fresh())
            authority.assert_called_once_with()

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

    def test_intermediate_deletion_ends_warm_promise_with_artifacts_present(self) -> None:
        build = self.root / "intermediates"
        build.mkdir()
        self.previous_build()
        self.assertTrue(self.fresh(build_dir=str(build)))
        _ = (self.state / "target-deletes.json").write_text(json.dumps({
            str(build): (NOW - timedelta(seconds=30)).timestamp(),
        }))
        self.assertFalse(self.fresh(build_dir=str(build)))
        self.assertTrue((self.worktree / "target").is_dir())

    def test_missing_intermediates_end_warm_promise_and_equal_roots_stay_warm(self) -> None:
        self.previous_build()
        self.assertFalse(self.fresh(build_dir=str(self.root / "missing-intermediates")))
        self.assertTrue(self.fresh())

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
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
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

    def test_admission_uses_the_resolved_target_and_build_key_passed_by_verify(self) -> None:
        configured = self.worktree / 'cargo-config-target'
        configured.mkdir()
        self.samples([2 * GIB])
        self.previous_build()
        meminfo = self.root / 'meminfo'
        _ = meminfo.write_text('MemAvailable: 67108864 kB\n')
        arguments = ['cargo', 'nextest', 'run', '-E', 'package(hana) & test(native_rate)']
        with patch.dict(os.environ, {'BUILDLOG_TEST_BUILD_KEY': 'current-key', 'BUILDLOG_TREE': 'source-only-key', 'BUILDLOG_TEST_TARGET_DIR': str(configured), 'BUILDLOG_TEST_BUILD_DIR': str(configured), 'CARGO_TARGET_DIR': str(self.worktree / 'target')}), patch.object(gate, 'git_identity', return_value=(HANA, 'checkout')), patch.object(store, 'host_name', return_value=self.host), patch.object(buildlog_record, 'git_directory', return_value=str(self.worktree)), patch.object(buildlog_record, 'git_facts', return_value={'worktree': str(self.worktree)}), patch.object(gate, 'process_start_time', return_value=None):
            fresh = gate.step_estimate(arguments, str(self.worktree), gate.INFER_FROM_ARGV, NOW, self.root, admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)
            self.assertEqual(fresh.profile.peak.bytes, 2 * GIB)
            _ = (self.state / 'target-deletes.json').write_text(json.dumps({str(configured): (NOW - timedelta(seconds=30)).timestamp()}))
            swept = gate.step_estimate(arguments, str(self.worktree), gate.INFER_FROM_ARGV, NOW, self.root, admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)
            self.assertEqual(swept.profile.peak.bytes, 10 * GIB)


class RepositoryHistoryTests(unittest.TestCase):
    root: Path = Path()
    admission: Path = Path()
    host: str = "test-host"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root)))
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
        write_history_rows(self.root, ({
            "host": self.host, "repo": "repo", "step": "nextest",
            "repo_identity": repository.key, "test_scope": scope,
            "anon_peak_bytes": gib * GIB,
            "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
        } for _ in range(5)), indexed=True)

    def estimate(self, repository: gate.Repository, scope: gate.TestScope = "full") -> gate.ExpectedPeak:
        return gate.expected_peak(repository, "nextest", self.host, NOW, self.root, scope=scope, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)

    def test_unrelated_repositories_named_repo_use_their_own_peaks(self) -> None:
        first = self.identity(self.checkout("one/repo", "git@example.org:nate/first.git"))
        second = self.identity(self.checkout("two/repo", "https://example.org/nate/second.git"))
        self.samples(first, 19)
        self.samples(second, 2)
        self.assertEqual(self.estimate(first), gate.ExpectedPeak(19 * GIB, "measured", 5))
        self.assertEqual(self.estimate(second), gate.ExpectedPeak(2 * GIB, "measured", 5))

    def test_repository_identity_uses_the_recorders_remote_rule(self) -> None:
        directory = self.checkout("project", "git@example.org:owner/project.git")
        with patch.object(gate.record, "remote_identity", return_value="https://example.org/owner/project") as normalize:
            identity = self.identity(directory)
        self.assertEqual(identity.key, "https://example.org/owner/project")
        normalize.assert_called_once_with("git@example.org:owner/project.git", str(directory))

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
        rows: list[dict[str, object]] = [{"host": self.host, "repo": "hana", "step": "nextest",
                 "anon_peak_bytes": 3 * GIB, "ended_at": store.utc_iso(HISTORY_AT.timestamp())}] * 5
        write_history_rows(self.root, rows, indexed=True)
        self.assertEqual(self.estimate(identity), gate.ExpectedPeak(3 * GIB, "measured", 5))
        peaks.unlink()
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, peak_mem_bytes INTEGER, crates_compiled INTEGER DEFAULT 1)")
            _ = db.executemany("INSERT INTO steps(host,repo,step,started_at,peak_mem_bytes) VALUES (?, ?, ?, ?, ?)",
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
        self.assertEqual(self.estimate(gate.UnknownRepository()), gate.ExpectedPeak(12 * GIB, "fallback", 0))

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


    def write_meminfo(self, available: int) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {available // 1024} kB\n")








    def held_reservation(self, sidecar: str, admitted: datetime) -> None:
        """A live reservation of this process, admitted at admitted, for 12 GiB."""
        row: dict[str, object] = {"pid": self.pid, "start_time": gate.process_start_time(self.pid) or "",
                                 "need": 12 * GIB, "repo": "hana", "step": "clippy", "worktree": "old",
                                 "admitted_at": store.utc_iso(admitted.timestamp()), "sidecar": sidecar,
                                 "test_threads": None, "call_id": None}
        _ = (gate.admission_dir(self.root) / f"{self.pid}-held.reservation").write_text(json.dumps(row))










class MemoryQueueFixture(unittest.TestCase):
    root: Path = Path()
    meminfo: Path = Path()
    processes: dict[int, str] = {}
    readings: dict[int, gate.MeasuredMemory | gate.GoneMemory] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.meminfo = self.root / "meminfo"
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config.parent)})))
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

        from scripts.build_queue.queue_fixture import fixture_queue_read
        _ = self.enterContext(patch.object(gate, "queue_read", side_effect=lambda:
            fixture_queue_read(gate.admission_dir(self.root), gate.process_table(self.root / "proc"), NOW.timestamp())))

    def waiter(self, pid: int, since: float, folder: str, name: str, need: float = 10.9) -> Path:
        row: gate.Waiter = {"pid": pid, "start_time": self.processes[pid], "since": since, "need": int(need * GIB),
                            "session_id": f"session-{pid}", "session_name": name, "step": "nextest", "folder": folder}
        path = gate.admission_dir(self.root) / f"{pid}-{self.processes[pid]}.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def running(self, pid: int, need: float, age: float = 180, *, sidecar: str = "",
                peak: float | None = None, test_phase: dict[str, object] | str | None = None,
                fields: Mapping[str, object] | None = None) -> Path:
        # Each reservation setup starts a live owner, including after an ended-owner case.
        _ = self.processes.setdefault(pid, f"fixture-{pid}")
        row: dict[str, object] = {"pid": pid, "start_time": self.processes[pid], "need": int(need * GIB),
                                 "repo": "test", "step": "nextest", "worktree": "test", "sidecar": sidecar,
                                 "admitted_at": store.utc_iso(NOW.timestamp() - age),
                                 "test_threads": None, "call_id": None}
        path = gate.admission_dir(self.root) / f"{pid}.reservation"
        _ = path.write_text(json.dumps({**row, **(fields or {})}))
        if sidecar:
            _ = Path(sidecar).write_text(str(self.root / f"{pid}.scope"))
        if peak is not None:
            _ = path.with_suffix(".max").write_text(str(int(peak * GIB)))
            _ = path.with_suffix(".wave").write_text(f"{int(peak * GIB)} {int(NOW.timestamp())}")
        if test_phase is not None:
            phase_path = gate.test_phase_path(gate.read_reservation(path))
            if not isinstance(phase_path, Path):
                self.fail("a test-phase fixture needs a cgroup sidecar")
            _ = phase_path.write_text(test_phase if isinstance(test_phase, str) else json.dumps(test_phase))
        return path

    def pass_for(self, pid: int, need: float, free: float, *, seconds: float = 0) -> gate.PassRow:
        swap = "\n".join(line for line in self.meminfo.read_text().splitlines() if line.startswith("Swap")) if self.meminfo.exists() else ""
        _ = self.meminfo.write_text(f"MemAvailable: {int(free * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n{swap}\n")
        now = NOW_EPOCH + seconds
        path = gate.admission_dir(self.root) / f"{pid}-{self.processes[pid]}.waiting"
        if path.exists():
            row = gate.read_waiter(path)
            row["need"] = int(need * GIB)
            _ = path.write_text(json.dumps(row))
        else:
            _ = self.waiter(pid, now, "tree", "asking", need)
        policy = gate.read_memory_config().policy
        with patch.dict(os.environ, {"BUILDLOG_MEMINFO": str(self.meminfo)}):
            inputs = fixture_queue_inputs(gate.admission_dir(self.root),
                gate.process_table(self.root / "proc"), now, policy)
        inputs = replace(inputs, blockers_for=frozenset({f"{pid}-{self.processes[pid]}"}))
        result = gate.admission_pass(inputs, now, gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), policy)
        return next(row for row in result.rows if row.owner == f"{pid}-{self.processes[pid]}")

    def head(self, need: float = 10.9) -> Path:
        row: gate.Waiter = {"pid": 300, "start_time": "head", "need": int(need * GIB),
                            "since": NOW.timestamp() - 2400, "session_name": "clone88ci nextest (verify pid 300)", "label": "clone88ci nextest (verify pid 300)"}
        path = gate.admission_dir(self.root) / "300-head.waiting"
        _ = path.write_text(json.dumps(row))
        return path







class TestPhaseChargeTests(MemoryQueueFixture):
    """The real reservation reader, estimator and finalizer share one private ledger."""

    phase_need: gate.TestPhaseNeed  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root), "BUILDLOG_BUILD_BUDGET_GIB": "21",
            "LINT_CONFIG_FILE": str(self.root / "lint.conf"), "CONF_DIR": str(Path(self.root / "build_admission.conf").parent),
        }))
        _ = write_config_fixture(self.root / "build_admission.conf", "test_phase_margin=1.2\n")
        commit_config_fixture(self.root / "build_admission.conf")
        self.phase_need = gate.TestPhaseNeed(2 * GIB, 10, 8, 8, "same_width", margin=admission_configuration_fixture().test_phase_margin)
        _ = self.enterContext(patch.object(store, "host_name", return_value="test-host"))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))

    def phase_fields(self, peak: int) -> dict[str, object]:
        return {"test_phase_peak_bytes": peak, "test_phase_started_at": NOW_EPOCH - 30,
                "repo_identity": HANA.key, "test_scope": "filtered", "package": "hana",
                "filters": ["render", "native_rate"], "test_threads": 8, "call_id": "phase-call"}

    def phase_step(self, *, peak: int = GIB * 2 // 5, recorded: bool = True) -> Path:
        fields: dict[str, object] = {
            "repo_identity": HANA.key, "test_scope": "filtered", "package": "hana",
            "filters": ["native_rate", "render"], "test_threads": 8, "worktree": "hana-Δ",
            "reservation_id": os.urandom(16).hex(),
        }
        if recorded:
            fields["test_phase_need"] = gate.test_phase_need_record(self.phase_need)
        self.readings[100] = gate.MeasuredMemory(GIB * 2 // 5)
        return self.running(100, 11, sidecar=str(self.root / "100.cgroup"), peak=11,
                            test_phase=self.phase_fields(peak), fields=fields)

    def memory(self, path: Path, now: datetime = NOW) -> gate.StepMemory:
        return gate.reservation_memory(path, gate.read_reservation(path), now,
                                       gate.process_table(self.root / "proc"), self.root / "proc", policy=admission_policy_fixture())

    def phase_samples(self, peaks: list[int], *, width: int = 8,
                      changes: Mapping[str, object] | None = None) -> None:
        write_history_rows(self.root, ({
            "host": "test-host", "repo": "hana", "repo_identity": HANA.key,
            "step": "nextest", "test_scope": "filtered", "package": "hana",
            "filters": ["render", "native_rate"], "test_threads": width,
            "anon_peak_bytes": 11 * GIB, "test_phase_peak_bytes": peak,
            "ended_at": store.utc_iso(HISTORY_AT.timestamp()), **(changes or {}),
        } for peak in peaks))


    def test_test_phase_charge_follows_peak_so_far(self) -> None:
        memory = self.memory(self.phase_step(peak=5 * GIB))
        self.assertEqual(gate.build_charge(memory), 6 * GIB)
        self.assertEqual(gate.growth_headroom(memory), 0, "test-phase history is no growth promise")

    def test_configured_margin_is_frozen_when_the_test_phase_need_is_written(self) -> None:
        path = self.phase_step(peak=5 * GIB, recorded=False)
        self.phase_samples([2 * GIB])
        config = self.root / "build_admission.conf"
        for margin in (1.5, 1.25):
            with self.subTest(margin=margin):
                _ = write_config_fault(config, f"[memory]\ntest_phase_margin={margin}\n")
                need = gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False))
                self.assertEqual(need, gate.TestPhaseNeed(2 * GIB, 1, 8, 8, "same_width", margin))
                row = cast(dict[str, object], json.loads(path.read_text()))
                row["test_phase_need"] = gate.test_phase_need_record(need)
                _ = path.write_text(json.dumps(row))
                _ = config.write_text("[memory]\ntest_phase_margin=2\n")
                self.assertEqual(gate.build_charge(self.memory(path)), math.ceil(5 * GIB * margin))
        self.readings[100] = gate.MeasuredMemory(GIB * 2 // 5)
        _ = write_config_fault(config, "[memory]\ntest_phase_margin=1.5\n")
        need = gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False))
        row = cast(dict[str, object], json.loads(path.read_text()))
        row["test_phase_need"] = gate.test_phase_need_record(need)
        _ = path.write_text(json.dumps(row))
        phase = gate.test_phase_path(gate.read_reservation(path))
        assert isinstance(phase, Path)
        _ = phase.write_text(json.dumps(self.phase_fields(GIB * 2 // 5)))
        self.assertEqual(gate.build_charge(self.memory(path)), 3 * GIB)

    def test_invalid_or_missing_margin_uses_the_named_fallback(self) -> None:
        path = self.phase_step(recorded=False)
        self.phase_samples([2 * GIB])
        config = self.root / "build_admission.conf"
        settings = (None, "[memory]\n", *(f"[memory]\ntest_phase_margin={value}\n"
                    for value in ("", "bad", "0", "0.99", "-1", "nan", "inf", "1e999")))
        for setting in settings:
            with self.subTest(setting=setting):
                if setting is None:
                    config.unlink(missing_ok=True)
                else:
                    _ = write_config_fault(config, setting)
                need = gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False))
                self.assertEqual(need, replace(self.phase_need, runs=1, margin=1.2))
                row = cast(dict[str, object], json.loads(path.read_text()))
                row["test_phase_need"] = gate.test_phase_need_record(need)
                _ = path.write_text(json.dumps(row))
                self.assertEqual(gate.build_charge(self.memory(path)), math.ceil(2 * GIB * 1.2))

    def test_test_phase_estimate_uses_acquired_settings_when_live_config_is_unusable(self) -> None:
        path = self.phase_step(recorded=False)
        self.phase_samples([2 * GIB])
        acquired = gate.read_memory_config()
        unusable = self.root / "unusable-config"
        unusable.mkdir()
        write_memory_config(unusable / "build_admission.conf", test_phase_margin="invalid")
        with patch.dict(os.environ, {"CONF_DIR": str(unusable)}):
            self.assertIsInstance(gate.read_admission_configuration(), gate.UnusableAdmissionConfiguration)
        with patch.dict(os.environ, {"CONF_DIR": str(unusable)}), patch.object(gate, "read_memory_config", side_effect=AssertionError("estimate read live config")):
            need = gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root,
                                        config=acquired)
        self.assertEqual(need, gate.TestPhaseNeed(2 * GIB, 1, 8, 8, "same_width", acquired.test_phase_margin))
        row = cast(dict[str, object], json.loads(path.read_text()))
        row["test_phase_need"] = gate.test_phase_need_record(need)
        _ = path.write_text(json.dumps(row))
        self.assertEqual(gate.build_charge(self.memory(path)), math.ceil(2 * GIB * acquired.test_phase_margin))

    def test_unreadable_test_phase_history_keeps_the_compile_charge(self) -> None:
        path = self.phase_step(recorded=False)
        row = gate.read_reservation(path)
        with patch.object(gate, "anonymous_history_rows", side_effect=PermissionError("history unreadable")):
            need = gate.test_phase_need(row, "test-host", NOW, self.root, config=gate.read_memory_config(report=False))
        self.assertEqual(need, gate.NoTestPhaseNeed("no_history"))
        stored = cast(dict[str, object], json.loads(path.read_text()))
        stored["test_phase_need"] = gate.test_phase_need_record(need)
        _ = path.write_text(json.dumps(stored))
        memory = self.memory(path)
        self.assertIsInstance(gate.charge_basis(memory), gate.PeakSoFar)
        self.assertEqual(gate.build_charge(memory), 11 * GIB)

    def test_test_phase_charge_does_not_decay(self) -> None:
        path = self.phase_step(peak=5 * GIB)
        later = NOW + timedelta(seconds=admission_policy_fixture().peak_hold.default_seconds + 1)
        self.assertEqual(gate.charge_basis(self.memory(path, later)), gate.charge_basis(self.memory(path)))
        self.assertEqual(gate.build_charge(self.memory(path, later)), 6 * GIB)

    def test_no_history_keeps_compile_peak(self) -> None:
        path = self.phase_step(recorded=False)
        for stored in (False, True):
            with self.subTest(stored_no_history=stored):
                if stored:
                    row = cast(dict[str, object], json.loads(path.read_text()))
                    row["test_phase_need"] = gate.test_phase_need_record(gate.NoTestPhaseNeed("no_history"))
                    _ = path.write_text(json.dumps(row))
                memory = self.memory(path)
                self.assertIsInstance(gate.charge_basis(memory), gate.PeakSoFar)
                self.assertEqual(gate.build_charge(memory), 11 * GIB)
                later = NOW + timedelta(seconds=admission_policy_fixture().peak_hold.default_seconds)
                self.assertIsInstance(gate.charge_basis(self.memory(path, later)), gate.PeakDecayed)
                self.assertEqual(gate.build_charge(self.memory(path, later)), GIB * 2 // 5)

    def test_no_mark_keeps_today(self) -> None:
        path = self.phase_step()
        phase = self.root / "100.test-phase.json"
        for contents in (None, "{}", "broken json"):
            with self.subTest(contents=contents):
                phase.unlink(missing_ok=True)
                if contents is not None:
                    _ = phase.write_text(contents)
                self.assertIsInstance(gate.charge_basis(self.memory(path)), gate.PeakSoFar)
                self.assertEqual(gate.build_charge(self.memory(path)), 11 * GIB)

    def test_unreadable_compile_peak_keeps_the_existing_promise_before_the_mark(self) -> None:
        path = self.phase_step()
        _ = path.with_suffix(".max").write_text("unreadable peak")
        self.assertEqual(gate.charge_basis(self.memory(path)), gate.PeakFileUnreadable(11 * GIB))

    def test_test_phase_charge_keeps_current_usage_and_the_nextest_ramp(self) -> None:
        path = self.phase_step()
        self.readings[100] = gate.MeasuredMemory(7 * GIB)
        self.assertEqual(gate.build_charge(self.memory(path)), 7 * GIB)
        self.readings[100] = gate.MeasuredMemory(GIB // 2)
        self.assertEqual(gate.charge_basis(self.memory(path, NOW - timedelta(seconds=120))),
                         gate.RampPromise(11 * GIB))

    def test_test_phase_path_uses_only_cgroup_sidecars(self) -> None:
        path = self.phase_step()
        row = gate.read_reservation(path)
        self.assertEqual(gate.test_phase_path(gate.read_reservation(path)), self.root / "100.test-phase.json")
        for sidecar in ("", str(self.root / "other.json")):
            row["sidecar"] = sidecar
            self.assertIsInstance(gate.test_phase_path(row), gate.NoTestPhaseFile)

    def test_same_width_p90_precedes_other_widths_and_sorts_filters(self) -> None:
        path = self.phase_step()
        self.phase_samples([number * GIB for number in range(1, 11)])
        self.phase_samples([20 * GIB], width=16)
        self.phase_samples([15 * GIB], width=4)
        self.assertEqual(gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False)),
                         gate.TestPhaseNeed(9 * GIB, 10, 8, 8, "same_width", margin=admission_configuration_fixture().test_phase_margin))

    def test_nearest_wider_history_is_not_scaled_down(self) -> None:
        path = self.phase_step()
        self.phase_samples([3 * GIB, 4 * GIB], width=12)
        self.phase_samples([19 * GIB], width=16)
        self.phase_samples([GIB], width=4)
        self.assertEqual(gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False)),
                         gate.TestPhaseNeed(4 * GIB, 2, 8, 12, "wider_unscaled", margin=admission_configuration_fixture().test_phase_margin))

    def test_widest_narrower_history_scales_up_and_rounds_up(self) -> None:
        path = self.phase_step()
        self.phase_samples([GIB + 1], width=3)
        self.phase_samples([10 * GIB], width=2)
        self.assertEqual(gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False)),
                         gate.TestPhaseNeed(((GIB + 1) * 8 + 2) // 3, 1, 8, 3, "scaled_up", margin=admission_configuration_fixture().test_phase_margin))

    def test_test_phase_need_uses_the_filtered_floor(self) -> None:
        path = self.phase_step()
        self.phase_samples([GIB // 10])
        self.assertEqual(gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False)),
                         gate.TestPhaseNeed(GIB, 1, 8, 8, "same_width", margin=admission_configuration_fixture().test_phase_margin))

    def test_test_phase_history_never_widens_the_family_or_time_window(self) -> None:
        path = self.phase_step()
        for changes in ({"filters": ["other"]}, {"package": "other"}, {"repo_identity": "other"},
                        {"host": "other"}, {"step": "build"}, {"test_scope": "full"},
                        {"ended_at": store.utc_iso((NOW - timedelta(days=gate.HISTORY_DAYS, seconds=1)).timestamp())},
                        {"ended_at": store.utc_iso((NOW + timedelta(seconds=1)).timestamp())},
                        {"test_phase_peak_bytes": 0}, {"test_phase_peak_bytes": True},
                        {"test_phase_peak_bytes": "large"}):
            with self.subTest(changes=changes):
                self.phase_samples([10 * GIB], changes=changes)
        row = gate.read_reservation(path)
        self.assertEqual(gate.test_phase_need(row, "test-host", NOW, self.root, config=gate.read_memory_config(report=False)), gate.NoTestPhaseNeed("no_history"))
        row["test_threads"] = gate.UnspecifiedTestWidth()
        self.assertEqual(gate.test_phase_need(row, "test-host", NOW, self.root, config=gate.read_memory_config(report=False)), gate.NoTestPhaseNeed("no_width"))

    def test_test_phase_reader_uses_the_shared_whole_row_boundary(self) -> None:
        path = self.phase_step()
        self.phase_samples([2 * GIB])
        with patch.object(gate, "anonymous_history_rows", wraps=gate.anonymous_history_rows) as reader:
            self.assertEqual(gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False)),
                             gate.TestPhaseNeed(2 * GIB, 1, 8, 8, "same_width", margin=admission_configuration_fixture().test_phase_margin))
        self.assertEqual(reader.call_args_list[0].args, (self.root / "admission/anon_peaks.jsonl",))
        assert_unfinished_history_ignored(self, self.root / "admission/anon_peaks.jsonl",
            lambda: gate.test_phase_need(gate.read_reservation(path), "test-host", NOW, self.root, config=gate.read_memory_config(report=False)))




    def test_estimate_codecs_keep_their_domains_and_missing_states(self) -> None:
        bases: tuple[gate.NeedBasis | gate.BasisNotRecorded, ...] = (
            "measured", "buildlog", "pooled", "fallback", gate.BasisNotRecorded(),
            gate.ShapeP90(10, gate.FailedPeakFloor(4 * GIB, HISTORY_AT.isoformat()),
                          gate.LegacyDevMultiplierHeadroom(), gate.CappedAtBudget(25 * GIB, gate.CapNotRecorded())),
            gate.ShapeLargest(2, factor=gate.ReleaseHeadroom(gate.read_memory_config(report=False).release_headroom)), gate.ShapeFallback())
        for basis in bases:
            with self.subTest(basis=basis):
                encoded = gate.need_basis_record(basis)
                self.assertEqual(gate.read_need_basis(encoded), basis)
                self.assertEqual(gate.read_test_phase_need(encoded), gate.NoTestPhaseNeed("not_recorded"))
        for need in (self.phase_need, replace(self.phase_need, margin=1.75), gate.NoTestPhaseNeed("no_width"),
                     gate.NoTestPhaseNeed("no_history"), gate.NoTestPhaseNeed("not_recorded")):
            with self.subTest(need=need):
                encoded = gate.test_phase_need_record(need)
                self.assertEqual(gate.read_test_phase_need(encoded), need)
                self.assertEqual(gate.read_need_basis(encoded), gate.BasisNotRecorded())
        legacy = gate.test_phase_need_record(replace(self.phase_need, margin=1.75))
        del legacy["margin"]
        with patch.object(gate, "read_memory_config", side_effect=AssertionError("legacy record read current margin")):
            self.assertEqual(gate.read_test_phase_need(legacy), gate.NoTestPhaseNeed("not_recorded"))
        row = gate.read_reservation(self.phase_step(recorded=False))
        self.assertEqual(gate.read_test_phase_need(row.get("test_phase_need")), gate.NoTestPhaseNeed("not_recorded"))
        self.assertEqual(gate.read_need_basis(row.get("need_basis")), gate.BasisNotRecorded())



    def test_charge_wire_types_have_one_shared_owner(self) -> None:
        import test_phase

        pairs = (
            (gate.TestPhaseChargeUse, test_phase.TestPhaseChargeUse),
            (gate.NoTestPhaseChargeUse, test_phase.NoTestPhaseChargeUse),
            (gate.CallRecord, test_phase.CallRecord),
            (gate.NoCallRecord, test_phase.NoCallRecord),
        )
        for exported, shared in pairs:
            with self.subTest(name=exported.__name__):
                self.assertIs(exported, shared)

    def test_shared_charge_check_rejects_invalid_fields(self) -> None:
        import test_phase

        evidence: dict[str, object] = {
            "bytes": 8192, "selected_at": 123, "reservation_id": "own",
            "need": gate.test_phase_need_record(self.phase_need),
        }
        self.assertEqual(test_phase.parse_test_phase_charge_use(evidence), evidence)
        self.assertIsInstance(test_phase.parse_test_phase_charge_use(evidence)["selected_at"], float)
        changes: tuple[dict[str, object], ...] = (
            {"bytes": "broken"}, {"bytes": True}, {"bytes": 0},
            {"selected_at": True}, {"selected_at": "123"}, {"selected_at": float("nan")},
            {"selected_at": float("inf")}, {"selected_at": -float("inf")}, {"selected_at": 10 ** 400},
            {"reservation_id": None}, {"need": []},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                _ = test_phase.parse_test_phase_charge_use({**evidence, **change})

    def test_saved_charge_keeps_admission_need_validation(self) -> None:
        import test_phase

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservation.json"
            evidence: dict[str, object] = {
                "bytes": 8192, "selected_at": 123, "reservation_id": "own", "need": {},
            }
            _ = test_phase.parse_test_phase_charge_use(evidence)
            _ = path.with_suffix(".test-phase-charge.json").write_text(json.dumps(evidence))
            self.assertIsInstance(gate.read_test_phase_charge_use(path, "own"), gate.NoTestPhaseChargeUse)
            evidence["need"] = gate.test_phase_need_record(self.phase_need)
            _ = path.with_suffix(".test-phase-charge.json").write_text(json.dumps(evidence))
            self.assertEqual(gate.read_test_phase_charge_use(path, "own"), evidence)
            self.assertIsInstance(gate.read_test_phase_charge_use(path, "other"), gate.NoTestPhaseChargeUse)

    def charge_evidence(self, row: Mapping[str, object]) -> dict[str, object]:
        evidence = row.get("test_phase_charge_use")
        if not isinstance(evidence, dict):
            self.fail("a selected test-phase charge must carry its decision evidence")
        return cast(dict[str, object], evidence)

    def test_nextest_charge_falls_to_test_phase_need_at_mark(self) -> None:
        path = self.phase_step()
        phase = self.root / "100.test-phase.json"
        phase.unlink()
        self.assertEqual(gate.decide(16 * GIB, 60 * GIB, 12 * GIB, [self.memory(path)],
            limits=gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())).state, "hold")
        _ = phase.write_text(json.dumps(self.phase_fields(GIB * 2 // 5)))
        memory = self.memory(path)
        self.assertEqual(gate.charge_basis(memory), gate.TestPhaseCharge(
            math.ceil(2 * GIB * 1.2), GIB * 2 // 5, self.phase_need, NOW_EPOCH - 30))
        self.assertEqual(gate.decide(16 * GIB, 60 * GIB, 12 * GIB, [self.memory(path)],
            limits=gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())).state, "admit")
        self.assertTrue(phase.exists(), "admission must leave record.py's source intact")
        self.assertEqual(int(path.with_suffix(".max").read_text()), 11 * GIB)









class QueueLifetimeReaderTests(unittest.TestCase):
    def test_overlapping_gates_keep_sampled_unsampled_and_cancelled_lifetimes(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(step="merge-gate", need=2 * GIB, since=fixture.clock.now() - 10)
            first = replace(first, sidecar=str(fixture.scope(first.owner)))
            first_grant = only_admitted(fixture.server.handle_request(first))
            fixture.scoped_peak(first_grant, GIB)
            fixture.clock.advance(5)
            second = fixture.request(step="merge-gate", need=3 * GIB, since=fixture.clock.now() - 3)
            second_grant = only_admitted(fixture.server.handle_request(second))
            fixture.free(0)
            cancelled = fixture.request(step="merge-gate")
            peer = fixture.connect(cancelled)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            fixture.clock.advance(4)
            peer.close()
            fixture.server.run_once(0)
            fixture.clock.advance(1)
            _ = fixture.server.handle_request(wire.ReleaseRequest("first-end", first_grant.reservation, 0))
            first_end = fixture.clock.now()
            fixture.clock.advance(1)
            _ = fixture.server.handle_request(wire.ReleaseRequest("second-end", second_grant.reservation, 1))
            lifetimes = {step.pid: step for step in gate.read_queue_lifetimes(fixture.state_dir)}
            self.assertEqual(set(lifetimes), {first.owner.pid, second.owner.pid, cancelled.owner.pid})
            sampled, unsampled, left = (lifetimes[request.owner.pid] for request in (first, second, cancelled))
            self.assertEqual((sampled.start_time, sampled.since, sampled.step),
                             (first.owner.start_time, first.since, "merge-gate"))
            self.assertEqual((sampled.memory_wait_s, unsampled.memory_wait_s), (10, 3))
            self.assertEqual(len(sampled.intervals), 1)
            self.assertEqual(len(unsampled.intervals), 1)
            self.assertLess(unsampled.intervals[0].admitted_at, first_end)
            self.assertEqual(sampled.intervals[0].reservation_id, first_grant.reservation)
            self.assertEqual(sampled.intervals[0].budget, 21 * GIB)
            self.assertIsInstance(sampled.end, gate.ReservationEnded)
            assert isinstance(sampled.end, gate.ReservationEnded)
            self.assertEqual(sampled.end.peak, gate.PeakSampled(GIB))
            self.assertIsInstance(sampled.end.end_kind, gate.StepEnd)
            self.assertEqual(sampled.end.ended_at, first_end)
            self.assertIsInstance(unsampled.end, gate.ReservationEnded)
            assert isinstance(unsampled.end, gate.ReservationEnded)
            self.assertIsInstance(unsampled.end.peak, gate.NoPositiveSample)
            self.assertEqual(unsampled.end.outcome, "failed")
            self.assertIsInstance(left.end, gate.LeftQueue)
            assert isinstance(left.end, gate.LeftQueue)
            self.assertEqual(left.end.reason, "cancelled")
            self.assertFalse(left.intervals)
            self.assertEqual(len(fixture.history()), 1)

    def test_token_return_joins_intervals_without_completing_the_step(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(since=fixture.clock.now() - 5)
            first = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(10)
            _ = fixture.server.handle_request(wire.TokenWaitRequest("token-return", first.reservation))
            open_step, = gate.read_queue_lifetimes(fixture.state_dir)
            self.assertIsInstance(open_step.end, gate.StepStillOpen)
            interval_end = open_step.intervals[0].end
            assert isinstance(interval_end, gate.ReservationEnded)
            self.assertIsInstance(interval_end.end_kind, gate.TokenReturn)
            fixture.clock.advance(20)
            fixture.free(0)
            returned = replace(request, episode="after-token", token_back_at=fixture.clock.now())
            self.assertIsInstance(fixture.server.handle_request(returned)[0], wire.Wait)
            fixture.clock.advance(7)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            second = only_admitted([fixture.server.ledger[returned.episode].reply])
            fixture.clock.advance(2)
            _ = fixture.server.handle_request(wire.ReleaseRequest("finished", second.reservation, 0))
            step, = gate.read_queue_lifetimes(fixture.state_dir)
            self.assertEqual((step.pid, step.start_time, step.since),
                             (request.owner.pid, request.owner.start_time, request.since))
            self.assertEqual([interval.reservation_id for interval in step.intervals],
                             [first.reservation, second.reservation])
            self.assertEqual(len(step.token_gaps), 1)
            gap = step.token_gaps[0]
            self.assertEqual(gap.token_back_at - gap.token_left_at, 20)
            self.assertEqual(step.memory_wait_s, 12)
            self.assertIsInstance(step.end, gate.ReservationEnded)
            assert isinstance(step.end, gate.ReservationEnded)
            self.assertIsInstance(step.end.end_kind, gate.StepEnd)
            self.assertEqual(step.end, step.intervals[-1].end)

    def test_cancellation_and_owner_exit_during_token_wait_leave_no_completed_step(self) -> None:
        for reason in ("cancelled", "owner_gone"):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                request = fixture.request()
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.clock.advance(2)
                _ = fixture.server.handle_request(wire.TokenWaitRequest("token-return", grant.reservation))
                fixture.clock.advance(3)
                if reason == "cancelled":
                    reconnect = replace(request, episode="token-wait-reconnect")
                    peer = fixture.connect(reconnect)
                    reply = peer.replies()[0]
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance(reply.reason, gate.TokenWaitingWaiter)
                    peer.close()
                    fixture.server.run_once(0)
                else:
                    fixture.end_owner(request.owner)
                step, = gate.read_queue_lifetimes(fixture.state_dir)
                self.assertIsInstance(step.end, gate.LeftQueue)
                assert isinstance(step.end, gate.LeftQueue)
                self.assertEqual((step.end.reason, step.end.ended_at), (reason, fixture.clock.now()))
                self.assertEqual(len(step.intervals), 1)
                interval_end = step.intervals[0].end
                assert isinstance(interval_end, gate.ReservationEnded)
                self.assertIsInstance(interval_end.end_kind, gate.TokenReturn)
                self.assertFalse(fixture.history())


class QueuePriorityAndTokenWaitTests(MemoryQueueFixture):
    """Private identities and clocks for queue priority and token waits."""

    clock: float = 0.0
    labels: dict[str, UnitLabels] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = NOW.timestamp()
        self.labels = {}
        self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root), "BUILDLOG_MEMINFO": str(self.meminfo),
            "BUILDLOG_BUILD_BUDGET_GIB": "21", "USER_ZONE": "America/New_York",
            "CLAUDE_CODE_SESSION_ID": "showrunner-id", "CODEX_THREAD_ID": "",
        }))
        _ = self.enterContext(patch.object(time, "time", side_effect=lambda: self.clock))
        _ = self.enterContext(patch.object(gate, "priority_labels", return_value=self.labels))
        _ = self.meminfo.write_text(f"MemAvailable: {60 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        self.readings.update({300: gate.MeasuredMemory(0), 400: gate.MeasuredMemory(0)})



    def label(self, pid: int, unit: str) -> None:
        self.labels[f"session-{pid}"] = UnitLabels(f"worker-{pid}", "builds", unit)



    def gaps(self) -> list[dict[str, object]]:
        path = self.root / "admission/queue_times.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_priority_replacement_stays_live_and_clear_restores_order_without_resetting_clocks(self) -> None:
        from scripts.build_queue import admission_protocol as wire
        self.label(300, "first")
        self.label(400, "second")
        with AdmissionFixture(root=self.root) as admission:
            admission.clock.epoch = self.clock
            admission.free(0)
            for pid, age in ((300, 900), (400, 600)):
                request = admission.request(owner=admission.fixture_owner(pid, self.processes[pid]),
                    need=12 * GIB, since=self.clock - age)
                _ = admission.server.handle_request(request)
            before = {key: wire.encode_record(gate.stored_waiter(value.row))
                for key, value in admission.server.waiters.items()}
            for unit, expected in (("first", [300, 400]), ("second", [400, 300])):
                replies = admission.server.handle_request(wire.PrioritySetRequest(unit, unit, "user's work", "showrunner-id"))
                self.assertEqual(replies, [wire.Ok()])
                reading, = admission.server.handle_request(wire.ViewRequest())
                assert isinstance(reading, wire.ViewReply)
                self.assertEqual([step.pid for step in reading.reading.view.waiting], expected)
            admission.clock.advance(30 * 86400)
            _ = admission.server.run_pass()
            reading, = admission.server.handle_request(wire.ViewRequest())
            assert isinstance(reading, wire.ViewReply)
            self.assertEqual([step.pid for step in reading.reading.view.waiting], [400, 300])
            saved = admission.server.priority
            assert not isinstance(saved, gate.NoQueuePriority)
            self.assertNotIn("until", saved)
            self.assertEqual(saved["unit"], "second")
            self.assertEqual(admission.server.handle_request(wire.PriorityClearRequest("clear", "showrunner-id")), [wire.Ok()])
            reading, = admission.server.handle_request(wire.ViewRequest())
            assert isinstance(reading, wire.ViewReply)
            self.assertEqual([step.pid for step in reading.reading.view.waiting], [300, 400])
            self.assertEqual({key: wire.encode_record(gate.stored_waiter(value.row))
                for key, value in admission.server.waiters.items()}, before)
            events = [row for row in buildlog_rows(admission.state_dir.parent) if row.get("kind") == "queue_priority"]
            self.assertCountEqual([(row["action"], row["unit"]) for row in events],
                [("set", "first"), ("set", "second"), ("clear", "second")])
            self.assertTrue(all("until" not in row for row in events))


    def test_token_gap_retry_reads_only_the_recent_queue_time_tail(self) -> None:
        directory = gate.admission_dir(self.root)
        path = directory / "queue_times.jsonl"
        old = json.dumps({"kind": "admitted", "at": self.clock - 100, "padding": "x" * 4096}) + "\n"
        _ = path.write_text(old * 256)
        gap: dict[str, object] = {"kind": "token_gap", "pid": 300, "start_time": "300",
            "token_left_at": self.clock - 1, "token_back_at": self.clock}
        loads = json.loads
        decoded_bytes = 0
        def counted(value: str | bytes | bytearray) -> object:
            nonlocal decoded_bytes
            decoded_bytes += len(value)
            return cast(object, loads(value))
        with patch.object(json, "loads", side_effect=counted):
            gate.append_queue_time(directory, gap, self.clock)
            gate.append_queue_time(directory, gap, self.clock + 1)
            gate.append_queue_time(directory, {"kind": "admitted", "pid": 400}, self.clock + 2)
        self.assertLess(decoded_bytes, 256 * 1024, "each poll must avoid decoding the old queue-time prefix")
        rows = self.gaps()
        self.assertEqual(len(rows), 258)
        self.assertEqual(sum(row.get("kind") == "token_gap" for row in rows), 1)
        self.assertEqual([row["at"] for row in rows[-2:]], [self.clock, self.clock + 2])














    def test_lifecycle_retry_stops_the_duplicate_scan_at_an_older_row(self) -> None:
        directory = gate.admission_dir(self.root)
        path = directory / "queue_times.jsonl"
        old = json.dumps({"kind": "admitted", "at": self.clock - 100, "padding": "x" * 4096}) + "\n"
        _ = path.write_text(old * 256)
        event: dict[str, object] = {"kind": "left", "event_id": "left:cancelled-step",
                                  "ended_at": self.clock - 1, "reason": "cancelled"}
        loads = json.loads
        decoded_bytes = 0

        def counted(value: str | bytes | bytearray) -> object:
            nonlocal decoded_bytes
            decoded_bytes += len(value)
            return cast(object, loads(value))

        with patch.object(json, "loads", side_effect=counted):
            gate.append_queue_time(directory, event, self.clock)
            gate.append_queue_time(directory, event, self.clock + 1)
        self.assertLess(decoded_bytes, 64 * 1024, "a lifecycle retry must not decode the older prefix")
        rows = self.gaps()
        self.assertEqual(len(rows), 257)
        self.assertEqual(sum(row.get("event_id") == event["event_id"] for row in rows), 1)

    def test_recent_queue_events_trim_the_oldest_rows_to_the_byte_limit(self) -> None:
        directory = gate.admission_dir(self.root)
        path = directory / "queue_times.jsonl"
        rows = [{"kind": "admitted", "at": self.clock - 36 + index,
                 "sequence": index, "padding": "x" * (256 * 1024)} for index in range(36)]
        _ = path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertGreater(path.stat().st_size, 8 * 1024 * 1024)
        gate.append_queue_time(directory, {"kind": "left", "sequence": 36}, self.clock)
        kept = self.gaps()
        self.assertLess(path.stat().st_size, 6 * 1024 * 1024)
        self.assertGreater(len(kept), 1)
        self.assertLess(len(kept), len(rows))
        self.assertEqual([row["sequence"] for row in kept], list(range(37 - len(kept), 37)))
        self.assertTrue(all(cast(float, row["at"]) >= self.clock - 14 * 86400 for row in kept))

    def test_queue_time_append_stamps_all_kinds_and_trims_old_rows_in_order(self) -> None:
        directory = gate.admission_dir(self.root)
        path = directory / "queue_times.jsonl"
        old = self.clock - 14 * 86400 - 1
        _ = path.write_text(json.dumps({"kind": "token_gap", "at": old, "padding": "x" * (8 * 1024 * 1024)}) + "\n" +
                        json.dumps({"kind": "admitted", "at": self.clock - 1}) + "\n")
        for offset, kind in enumerate(("token_gap", "admitted", "left_queue")):
            gate.append_queue_time(directory, {"kind": kind, "at": -1, "pid": 300}, self.clock + offset)
        rows = self.gaps()
        self.assertEqual([row["at"] for row in rows], [self.clock - 1, self.clock, self.clock + 1, self.clock + 2])
        self.assertEqual([row["kind"] for row in rows], ["admitted", "token_gap", "admitted", "left_queue"])
        self.assertLess(path.stat().st_size, 8 * 1024 * 1024)



class ServicePriorityRepairTests(unittest.TestCase):
    """Keep Phase 42 rules at the service's durable state boundary."""

    labels: dict[str, UnitLabels] = {}

    @override
    def setUp(self) -> None:
        from scripts.message import roster
        self.labels = {"director-unit": UnitLabels("director", unit="chosen")}
        _ = self.enterContext(patch.object(roster, "roster", return_value=roster.Roster([], [])))
        _ = self.enterContext(patch.object(roster, "unit_labels", return_value=self.labels))
        _ = self.enterContext(patch.object(roster, "unit_labels_for", return_value=self.labels))

    def test_unknown_priority_unit_preserves_the_priority_and_log(self) -> None:
        with AdmissionFixture() as fixture:
            self.assertEqual(fixture.server.handle_request(wire.PrioritySetRequest(
                "initial", "chosen", "saved reason", "director")), [wire.Ok()])
            priority = fixture.server.priority
            assert not isinstance(priority, gate.NoQueuePriority)
            before = priority.copy()
            logged = buildlog_rows(fixture.state_dir.parent)
            for unit in ("not-a-roster-unit", UnitLabels("").unit):
                self.assertEqual(fixture.server.handle_request(wire.PrioritySetRequest(
                    "bad-" + unit, unit, "new reason", "director")), [wire.Refused(
                        f"unknown unit {unit}; the queue priority did not change")])
                self.assertEqual(fixture.server.priority, before)
                self.assertEqual(buildlog_rows(fixture.state_dir.parent), logged)

    def test_unassigned_waiter_never_gets_placeholder_priority(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request()
            self.labels[request.identity.session_id] = UnitLabels("unassigned")
            _ = fixture.server.handle_request(request)
            fixture.server.priority = {"unit": UnitLabels("").unit, "why": "old wire value",
                "set_at": fixture.clock.now(), "set_by": "director", "transition": gate.SettledPriority()}
            view, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(view, wire.ViewReply)
            self.assertEqual(gate.waiter_priority(view.reading.ordered[0][1]), gate.NormalOrder())
            self.assertNotIn("priority_rank", gate.stored_waiter(view.reading.ordered[0][1]))

    def test_priority_cleanup_uses_the_saved_log_end_and_records_a_stopped_set_once(self) -> None:
        with AdmissionFixture() as fixture:
            path = store.host_file(store.host_name(), fixture.clock.now())
            path.parent.mkdir(parents=True, exist_ok=True)
            prefix = b"this old prefix is not JSON\n"
            _ = path.write_bytes(prefix)
            request = wire.PrioritySetRequest("stopped-set", "chosen", "saved reason", "director")
            with patch.object(buildlog_record, "queue_priority", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _ = fixture.server.handle_request(request)
            priority = fixture.server.priority
            assert not isinstance(priority, gate.NoQueuePriority)
            self.assertIsInstance(priority["transition"], gate.PendingPriorityTransition)
            transition = priority["transition"]
            assert isinstance(transition, gate.PendingPriorityTransition)
            self.assertEqual(transition.month_row_end, len(prefix))
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(request), [wire.Ok()])
            fixture.restart()
            tail = path.read_bytes()[len(prefix):]
            self.assertEqual(path.read_bytes()[:len(prefix)], prefix)
            rows = [json.loads(line) for line in tail.splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["kind"], rows[0]["action"], rows[0]["unit"]),
                             ("queue_priority", "set", "chosen"))

    def test_failed_dead_owner_token_cleanup_does_not_stop_later_admissions(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            with patch.object(gate, "finalize_reservation", side_effect=OSError("unreadable peak")):
                with self.assertRaises(OSError):
                    _ = fixture.server.handle_request(wire.TokenWaitRequest("token-cut", grant.reservation))
                saved = fixture.server.reservations[request.episode]
                fixture.proc.kill(request.owner)
                for _ in range(2):
                    with patch("sys.stderr", io.StringIO()) as errors:
                        next_request = fixture.request(need=GIB)
                        _ = only_admitted(fixture.server.handle_request(next_request))
                    self.assertEqual(fixture.server.reservations[request.episode], saved)
                    self.assertIn(saved.path, errors.getvalue())
                    self.assertIn("unreadable peak", errors.getvalue())

    def test_bad_legacy_token_intent_does_not_stop_other_imports_or_admission(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            (fixture.state_dir / "state.json").unlink()
            bad = fixture.state_dir / "bad.token-wait"
            _ = bad.write_text("not JSON")
            with patch("sys.stderr", io.StringIO()) as errors:
                fixture.server = fixture.new_server()
            self.assertIn(str(bad), errors.getvalue())
            self.assertEqual(bad.read_text(), "not JSON")
            _ = only_admitted(fixture.server.handle_request(fixture.request()))
            self.assertTrue(bad.exists())

        with AdmissionFixture() as fixture:
            request, intent, waiter = fixture.legacy_token_wait()
            with patch.object(gate, "read_token_wait", side_effect=OSError("temporary read failure")):
                with patch("sys.stderr", io.StringIO()) as errors:
                    fixture.server = fixture.new_server()
                    _ = only_admitted(fixture.server.handle_request(fixture.request()))
                    fixture.restart()
                self.assertIn(str(intent), errors.getvalue())
                self.assertTrue(intent.exists())
                self.assertEqual(fixture.server.legacy_pending, (str(intent),))
                self.assertIn("retry", fixture.server.reservations)
            _ = fixture.server.run_pass()
            self.assertEqual(fixture.server.legacy_pending, ())
            self.assertFalse(intent.exists())
            self.assertNotIn("retry", fixture.server.reservations)
            place = next(place for place in fixture.server.places.values() if place.owner == request.owner)
            assert "token_left_at" in waiter
            self.assertEqual((place.since, place.token_left_at), (request.since, waiter["token_left_at"]))
            history = [row for row in fixture.history() if row["reservation_id"] == "retry"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["outcome"], "completed")
            fixture.restart()
            self.assertEqual([row for row in fixture.history() if row["reservation_id"] == "retry"], history)

    def test_legacy_token_import_keeps_recovery_data_after_a_failed_state_write(self) -> None:
        with AdmissionFixture() as fixture:
            _, intent, _ = fixture.legacy_token_wait()
            with patch.object(gate, "read_token_wait", side_effect=OSError("temporary read failure")):
                with patch("sys.stderr", io.StringIO()):
                    fixture.server = fixture.new_server()
            from scripts.lib.atomic_file import write_text_atomic

            publish = write_text_atomic
            failed = False

            def fail_first_write(path: Path, text: str) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("temporary state write failure")
                publish(path, text)

            with patch.object(service, "write_text_atomic", side_effect=fail_first_write):
                with patch("sys.stderr", io.StringIO()) as errors:
                    _ = fixture.server.run_pass()
            self.assertTrue(failed)
            self.assertIn(str(intent), errors.getvalue())
            self.assertTrue(intent.exists())
            self.assertEqual(fixture.server.legacy_pending, (str(intent),))
            self.assertNotIn(str(intent), fixture.server.legacy_cleanup)
            self.assertEqual(service.read_state(fixture.state_dir).legacy_pending, (str(intent),))
            self.assertEqual(fixture.history(), [])
            with patch.object(gate, "read_token_wait", side_effect=OSError("temporary read failure")):
                with patch("sys.stderr", io.StringIO()):
                    fixture.restart()
                    _ = only_admitted(fixture.server.handle_request(fixture.request(need=GIB)))
            self.assertTrue(intent.exists())
            self.assertEqual(fixture.server.legacy_pending, (str(intent),))
            self.assertIn("retry", fixture.server.reservations)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertTrue(any(row.get("reservation_id") == "retry" for row in inputs.reservations))
            with patch.object(fixture.server, "_finish_import", side_effect=InjectedCrash("before intent cleanup")):
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.run_pass()
            state = service.read_state(fixture.state_dir)
            self.assertEqual(state.legacy_pending, ())
            self.assertIn(str(intent), state.legacy_cleanup)
            self.assertTrue(intent.exists())
            fixture.restart()
            self.assertFalse(intent.exists())
            history = [row for row in fixture.history() if row["reservation_id"] == "retry"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["outcome"], "completed")
            fixture.restart()
            self.assertEqual([row for row in fixture.history() if row["reservation_id"] == "retry"], history)

    def test_legacy_token_import_preserves_completed_outcome_after_the_owner_and_scope_end(self) -> None:
        with AdmissionFixture() as fixture:
            request, intent, _ = fixture.legacy_token_wait()
            with patch.object(gate, "read_token_wait", side_effect=OSError("temporary read failure")):
                with patch("sys.stderr", io.StringIO()):
                    fixture.server = fixture.new_server()
                    fixture.proc.kill(request.owner)
                    scope = fixture.root / f"scope-{request.owner.pid}"
                    _ = (scope / "cgroup.procs").write_text("")
                    _ = (scope / "cgroup.events").write_text("populated 0\n")
                    fixture.restart()
                    _ = only_admitted(fixture.server.handle_request(fixture.request(need=GIB)))
            self.assertTrue(intent.exists())
            self.assertEqual(fixture.server.legacy_pending, (str(intent),))
            self.assertIn("retry", fixture.server.reservations)
            self.assertEqual(fixture.history(), [])
            _ = fixture.server.run_pass()
            self.assertFalse(intent.exists())
            self.assertNotIn("retry", fixture.server.reservations)
            history = [row for row in fixture.history() if row["reservation_id"] == "retry"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["outcome"], "completed")
            fixture.restart()
            self.assertEqual([row for row in fixture.history() if row["reservation_id"] == "retry"], history)


class BuildPromiseFixture(MemoryQueueFixture):
    """One private history, index and ledger for the build-promise regressions."""

    release_headroom: gate.ReleaseHeadroom = gate.ReleaseHeadroom(1.0)
    dev_size_headroom: gate.DevSizeHeadroom = gate.DevSizeHeadroom(1.0, 0)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root), "BUILDLOG_BUILD_BUDGET_GIB": "21",
        }))
        _ = self.enterContext(patch.object(gate, "git_identity", return_value=(HANA, "hana-clone")))
        _ = self.enterContext(patch.object(store, "host_name", return_value="test-host"))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        config = gate.read_memory_config(report=False)
        self.release_headroom = gate.ReleaseHeadroom(config.release_headroom)
        self.dev_size_headroom = gate.DevSizeHeadroom(config.dev_size_factor, config.dev_size_margin_bytes)

    def sample(self, peak: float, *, shape: BuildShape = RELEASE_SHAPE,
               at: datetime = HISTORY_AT, outcome: str = "completed", need: float = 21,
               host: str = "test-host", repository: gate.RepositoryIdentity = HANA,
               call_id: str = "", record_shape: bool = True, crates: int | None = 1) -> None:
        row: dict[str, object] = {
            "host": host, "repo": repository.name, "repo_identity": repository.key,
            "step": "build", "ended_at": store.utc_iso(at.timestamp()),
            "anon_peak_bytes": int(peak * GIB), "need": int(need * GIB), "call_id": call_id,
        }
        if record_shape:
            row["shape"] = shape.key
        if outcome:
            row["outcome"] = outcome
        write_history_rows(self.root, [row], indexed=record_shape, crates=crates)

    def index_build(self, call_id: str, arguments: list[str], *,
                    at: datetime = HISTORY_AT, crates: int = 1) -> None:
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as connection, connection:
            _ = connection.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, "
                                   + "repo_identity TEXT, step TEXT, started_at TEXT, "
                                   + "call_id TEXT, argv TEXT, peak_mem_bytes INTEGER, crates_compiled INTEGER DEFAULT 1)")
            _ = connection.execute("INSERT INTO steps(host,repo,repo_identity,step,started_at,call_id,argv,peak_mem_bytes,crates_compiled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   ("test-host", "hana", HANA.key, "build", at.isoformat(),
                                    call_id, json.dumps(arguments), 0, crates))

    def estimate(self, shape: BuildShape = RELEASE_SHAPE, *, now: datetime = NOW) -> gate.ExpectedPeak:
        return gate.expected_peak(HANA, "build", "test-host", now, self.root, shape=shape, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)

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



class BuildShapePromiseTests(BuildPromiseFixture):
    def test_dev_history_uses_size_headroom_below_the_cap(self) -> None:
        for base, binary in ((0.5, "hana-small"), (6.3, "hana")):
            shape = build_shape(["cargo", "build", "--bin", binary])
            previous_count = 0
            for count in (1, 9):
                for _ in range(count - previous_count):
                    self.sample(base, shape=shape)
                previous_count = count
                with self.subTest(base=base, runs=count):
                    peak = self.estimate(shape)
                    basis = self.same_shape_basis(peak)
                    self.assertEqual(peak.count, count)
                    self.assertIsInstance(basis, gate.ShapeLargest)
                    self.assertIsInstance(basis.cap, gate.UnderBudget)
                    self.assertIsInstance(basis.factor, gate.DevSizeHeadroom)
                    self.assertEqual(basis.factor, self.dev_size_headroom)
                    self.assertEqual(peak.bytes, gate.shape_headroom_bytes(int(base * GIB), self.dev_size_headroom))
                    if base == 6.3:
                        self.assertGreaterEqual(peak.bytes, int(7.8 * GIB))
                        self.assertLess(peak.bytes, 21 * GIB)
                    self.assertEqual(gate.read_need_basis(gate.need_basis_record(basis)), basis)

    def test_dev_size_headroom_fits_the_s2_build(self) -> None:
        self.assertEqual(gate.shape_headroom_bytes(int(6.3 * GIB), self.dev_size_headroom), 8_622_146_848)

    def test_changed_dev_factor_changes_the_next_estimate(self) -> None:
        shape = build_shape(["cargo", "build", "--bin", "hana"])
        self.sample(6.3, shape=shape)
        before = self.estimate(shape)
        saved_basis = gate.need_basis_record(before.basis)
        write_memory_config(self.root / "build_admission.conf", dev_size_factor="1.2")
        config = gate.read_memory_config(report=False)
        rule = gate.DevSizeHeadroom(config.dev_size_factor, config.dev_size_margin_bytes)
        after = self.estimate(shape)
        self.assertGreater(after.bytes, before.bytes)
        self.assertEqual(after.count, before.count)
        self.assertEqual(after.bytes, gate.shape_headroom_bytes(int(6.3 * GIB), rule))
        self.assertEqual(self.same_shape_basis(after).factor, rule)
        self.assertIsInstance(self.same_shape_basis(after).cap, gate.UnderBudget)
        self.assertEqual(gate.read_need_basis(saved_basis), before.basis)

    def test_changed_dev_margin_changes_only_the_next_estimate(self) -> None:
        from scripts.build_queue.queue_evidence import need_text
        from scripts.lib.user_zone import user_zone

        shape = build_shape(["cargo", "build", "--bin", "hana"])
        self.sample(6.3, shape=shape)
        before = self.estimate(shape)
        saved = gate.need_basis_record(before.basis)
        write_memory_config(self.root / "build_admission.conf", dev_size_margin_gib="2.0")
        config = gate.read_memory_config(report=False)
        rule = gate.DevSizeHeadroom(config.dev_size_factor, config.dev_size_margin_bytes)
        after = self.estimate(shape)
        self.assertGreater(after.bytes, before.bytes)
        self.assertEqual(after.count, before.count)
        self.assertEqual(after.bytes, gate.shape_headroom_bytes(int(6.3 * GIB), rule))
        self.assertEqual(self.same_shape_basis(after).factor, rule)
        self.assertEqual(gate.read_need_basis(saved), before.basis)
        text = need_text(gate.RecordedNeed(after.source, after.count, 14, "full", "", (),
                                         gate.WidthNotRecorded(), after.basis), zone=user_zone())
        self.assertIn(f"{config.dev_size_margin_bytes / GIB:.1f} GiB", text)
        self.assertIn(f"{config.dev_size_factor:g}", text)

    def test_changed_release_headroom_changes_only_the_next_estimate(self) -> None:
        from scripts.build_queue.queue_evidence import need_text
        from scripts.lib.user_zone import user_zone

        self.sample(6)
        before = self.estimate()
        saved = gate.need_basis_record(before.basis)
        write_memory_config(self.root / "build_admission.conf", release_headroom="1.5")
        config = gate.read_memory_config(report=False)
        rule = gate.ReleaseHeadroom(config.release_headroom)
        after = self.estimate()
        self.assertGreater(after.bytes, before.bytes)
        self.assertEqual(after.count, before.count)
        self.assertEqual(after.bytes, gate.shape_headroom_bytes(6 * GIB, rule))
        self.assertEqual(self.same_shape_basis(after).factor, rule)
        self.assertEqual(gate.read_need_basis(saved), before.basis)
        text = need_text(gate.RecordedNeed(after.source, after.count, 14, "full", "", (),
                                         gate.WidthNotRecorded(), after.basis), zone=user_zone())
        self.assertIn(f"release headroom × {config.release_headroom:g}", text)

    def test_sparse_build_history_keeps_its_shape_rule_above_a_lower_pool(self) -> None:
        other = gate.RepositoryIdentity("https://example.org/nate/other", "other", "other")
        shape = build_shape(["cargo", "build", "--bin", "hana"])
        for count in range(1, 5):
            self.sample(6.3, shape=shape)
            self.sample(0.5, shape=shape, repository=other)
            with self.subTest(own_runs=count):
                peak = self.estimate(shape)
                self.assertEqual(peak.count, count)
                self.assertIsInstance(peak.basis, gate.ShapeLargest)
                self.assertEqual(peak.bytes, gate.shape_headroom_bytes(int(6.3 * GIB), self.dev_size_headroom))

    def test_stored_build_shape_does_not_bypass_compile_classification(self) -> None:
        self.sample(3)
        self.sample(20, crates=0)
        self.assertEqual((self.estimate().bytes, self.estimate().count),
                         (gate.shape_headroom_bytes(3 * GIB, self.release_headroom), 1))
        self.sample(20, crates=None)
        self.assertEqual((self.estimate().bytes, self.estimate().count), (21 * GIB, 2))

    def test_repeated_step_with_a_zero_count_keeps_its_compile_peak(self) -> None:
        self.sample(16, call_id="retried-build", crates=652)
        self.index_build("retried-build", RELEASE_BUILD, crates=0)
        peak = self.estimate()
        self.assertEqual(peak.count, 1)
        self.assertEqual(peak.bytes, min(21 * GIB, gate.shape_headroom_bytes(16 * GIB, self.release_headroom)))

    def test_legacy_index_without_compile_counts_keeps_its_build_peak(self) -> None:
        self.index_build("legacy-build", RELEASE_BUILD)
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as connection, connection:
            _ = connection.execute("ALTER TABLE steps DROP COLUMN crates_compiled")
        self.sample(5, call_id="legacy-build", record_shape=False)
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count),
                         (gate.shape_headroom_bytes(5 * GIB, self.release_headroom), 1))

    def test_small_dev_history_still_covers_the_limiting_build(self) -> None:
        shape = build_shape(["cargo", "build", "--bin", "cargo-berth"])
        self.sample(0.34, shape=shape)
        peak = self.estimate(shape)
        self.assertGreaterEqual(peak.bytes, math.ceil(1.41 * GIB))
        self.assertIsInstance(self.same_shape_basis(peak).cap, gate.UnderBudget)

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
                self.assertEqual(peak.basis, gate.ShapeFallback(cap=gate.CappedAtBudget(24 * GIB, 21 * GIB)))
                self.assertEqual(gate.need_basis_record(peak.basis), {
                    "kind": "shape_fallback", "cap": {"kind": "budget", "uncapped": 24 * GIB, "cap": 21 * GIB},
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
        self.assertEqual((peak.bytes, peak.source, peak.count), (gate.shape_headroom_bytes(3 * GIB, self.release_headroom), "shape", 2))
        self.assertEqual(peak.basis, gate.ShapeLargest(2, gate.NoFloor(), self.release_headroom, gate.UnderBudget()))

    def test_fewer_than_ten_same_shape_runs_use_the_largest(self) -> None:
        for value in range(1, 10):
            self.sample(value)
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count), (gate.shape_headroom_bytes(9 * GIB, self.release_headroom), 9))
        self.assertIsInstance(peak.basis, gate.ShapeLargest)

    def test_ten_same_shape_runs_use_p90(self) -> None:
        for _ in range(9):
            self.sample(3)
        self.sample(10)
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count), (gate.shape_headroom_bytes(3 * GIB, self.release_headroom), 10))
        self.assertEqual(peak.basis, gate.ShapeP90(10, gate.NoFloor(), self.release_headroom, gate.UnderBudget()))

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

    def test_each_build_profile_uses_its_headroom_rule(self) -> None:
        for arguments, rule in (
            (RELEASE_BUILD, self.release_headroom),
            (["cargo", "build", "--profile", "release", "--bin", "hana"], self.release_headroom),
            (["cargo", "build"], self.dev_size_headroom),
            (["cargo", "build", "--profile", "small"], gate.NoHeadroom()),
        ):
            shape = build_shape(arguments)
            self.sample(10, shape=shape)
            with self.subTest(arguments=arguments):
                peak = self.estimate(shape)
                self.assertEqual(peak.bytes, min(21 * GIB, gate.shape_headroom_bytes(10 * GIB, rule)))
                basis = self.same_shape_basis(peak)
                assert isinstance(basis, gate.ShapeLargest)
                self.assertEqual(basis.factor, rule)

    def test_build_promises_never_exceed_21_gib(self) -> None:
        for arguments in (RELEASE_BUILD, ["cargo", "build"]):
            shape = build_shape(arguments)
            self.sample(30, shape=shape, need=30)
            with self.subTest(profile=shape.profile):
                peak = self.estimate(shape)
                self.assertEqual(peak.bytes, 21 * GIB)
                rule = self.release_headroom if shape.profile == "release" else self.dev_size_headroom
                self.assertEqual(self.same_shape_basis(peak).cap,
                                 gate.CappedAtBudget(gate.shape_headroom_bytes(30 * GIB, rule), 21 * GIB))

    def test_old_history_matches_a_copy_with_a_different_checkout_name(self) -> None:
        self.index_build("copied-build", RELEASE_BUILD)
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as connection, connection:
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
        before = file_marks.index_path_in(self.root).read_bytes()
        peak = self.estimate()
        self.assertEqual((peak.bytes, peak.count), (gate.shape_headroom_bytes(5 * GIB, self.release_headroom), 1))
        self.assertEqual(file_marks.index_path_in(self.root).read_bytes(), before)

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
        self.assertEqual((self.estimate().bytes, self.estimate().count), (gate.shape_headroom_bytes(3 * GIB, self.release_headroom), 1))

    def test_shape_history_includes_a_peak_that_ends_at_the_estimate_time(self) -> None:
        self.sample(10, at=NOW)
        peak = self.estimate(now=NOW)
        self.assertEqual((peak.bytes, peak.count), (gate.shape_headroom_bytes(10 * GIB, self.release_headroom), 1))
        self.assertIsInstance(peak.basis, gate.ShapeLargest)

    def test_killed_peak_sets_a_floor_even_when_p90_is_lower(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, outcome="killed", need=16)
        peak = self.estimate()
        self.assertEqual(peak.bytes, gate.shape_headroom_bytes(int(15.76 * GIB), self.release_headroom))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.FailedPeakFloor(int(15.76 * GIB), store.utc_iso(HISTORY_AT.timestamp())))
        self.assertAlmostEqual(peak.bytes / GIB, 20.70864, places=6)

    def test_failed_peak_floor_uses_completion_time_for_the_history_window(self) -> None:
        for _ in range(9):
            self.sample(10)
        ended = NOW - timedelta(days=13, hours=23)
        self.index_build("long-build", RELEASE_BUILD, at=NOW - timedelta(days=14, hours=1))
        self.sample(15.76, outcome="killed", at=ended, call_id="long-build", record_shape=False)
        peak = self.estimate()
        self.assertEqual(peak.count, 10)
        self.assertEqual(peak.bytes, gate.shape_headroom_bytes(int(15.76 * GIB), self.release_headroom))
        self.assertEqual(self.same_shape_basis(peak).floor,
                         gate.FailedPeakFloor(int(15.76 * GIB), store.utc_iso(ended.timestamp())))

    def test_later_success_below_the_floor_does_not_end_it(self) -> None:
        for _ in range(9):
            self.sample(10)
        ended = NOW - timedelta(hours=1)
        self.sample(15.76, outcome="owner_gone", at=ended)
        self.sample(1, at=HISTORY_AT)
        peak = self.estimate()
        self.assertEqual(peak.bytes, gate.shape_headroom_bytes(int(15.76 * GIB), self.release_headroom))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.FailedPeakFloor(int(15.76 * GIB), store.utc_iso(ended.timestamp())))

    def test_completed_run_above_its_need_also_sets_the_floor(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, need=12, outcome="completed")
        peak = self.estimate()
        self.assertEqual(peak.bytes, gate.shape_headroom_bytes(int(15.76 * GIB), self.release_headroom))
        self.assertIsInstance(self.same_shape_basis(peak).floor, gate.FailedPeakFloor)

    def test_newest_failed_row_replaces_the_floor_in_time_order(self) -> None:
        for _ in range(20):
            self.sample(10)
        self.sample(11, outcome="killed", at=HISTORY_AT)
        # Append the older and larger failure last: file order is not time order.
        self.sample(15.76, outcome="owner_gone", at=NOW - timedelta(hours=1))
        peak = self.estimate()
        self.assertEqual(peak.bytes, gate.shape_headroom_bytes(11 * GIB, self.release_headroom))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.FailedPeakFloor(11 * GIB, store.utc_iso(HISTORY_AT.timestamp())))

    def test_failed_floor_expires_after_fourteen_days(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, outcome="killed", at=NOW - timedelta(days=14, seconds=1))
        peak = self.estimate()
        self.assertEqual(peak.bytes, gate.shape_headroom_bytes(10 * GIB, self.release_headroom))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.NoFloor())

    def test_old_row_with_no_outcome_never_sets_a_failed_floor(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, need=1, outcome="")
        peak = self.estimate()
        self.assertEqual(peak.bytes, gate.shape_headroom_bytes(10 * GIB, self.release_headroom))
        self.assertEqual(self.same_shape_basis(peak).floor, gate.NoFloor())


class BuildLifetimePromiseTests(BuildPromiseFixture):
    def test_release_build_after_ramp_keeps_need_as_its_budget_charge(self) -> None:
        path = self.build_reservation(need=16, age=admission_policy_fixture().timing.ramp_seconds + 3600)
        row = cast(gate.Reservation, json.loads(path.read_text()))
        memory = gate.reservation_memory(path, row, NOW, gate.process_table(self.root / "proc"), self.root / "proc", policy=admission_policy_fixture())
        self.assertIsInstance(memory, gate.MeasuredMemory)
        if not isinstance(memory, gate.MeasuredMemory):
            self.fail("fixture build must have a current memory reading")
        self.assertEqual(memory.promise, gate.LifetimePromise(16 * GIB))
        self.assertEqual(gate.charge_basis(memory), gate.LifetimePromise(16 * GIB))
        self.assertEqual((gate.build_charge(memory), gate.growth_headroom(memory)), (16 * GIB, 15 * GIB))


    def test_release_build_can_pass_budget_and_still_hold_for_free_growth(self) -> None:
        path = self.build_reservation(need=16, age=admission_policy_fixture().timing.ramp_seconds + 3600)
        row = gate.read_reservation(path)
        memory = gate.reservation_memory(path, row, NOW, gate.process_table(self.root / "proc"),
                                         self.root / "proc", policy=admission_policy_fixture())
        decision = gate.decide(20 * GIB, 60 * GIB, 4 * GIB, [memory],
                               limits=gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()))
        self.assertEqual((decision.budget_passes, decision.free_passes, decision.state),
                         (True, False, "hold"))
        self.assertEqual((decision.charged, decision.promised, decision.threshold),
                         (16 * GIB, 15 * GIB, 22 * GIB))

    def test_release_charge_covers_current_memory_and_observed_peak_above_need(self) -> None:
        path = self.build_reservation(need=10)
        row = cast(gate.Reservation, json.loads(path.read_text()))
        for current, peak in ((18, 17), (2, 19)):
            with self.subTest(current=current, peak=peak):
                self.readings[100] = gate.MeasuredMemory(current * GIB)
                _ = path.with_suffix(".max").write_text(str(peak * GIB))
                _ = path.with_suffix(".wave").write_text(f"{peak * GIB} {int(NOW.timestamp())}")
                memory = gate.reservation_memory(path, row, NOW, gate.process_table(self.root / "proc"), self.root / "proc", policy=admission_policy_fixture())
                self.assertEqual(gate.build_charge(memory), max(10, current, peak) * GIB)

    def test_dev_build_and_nextest_after_ramp_keep_current_charge(self) -> None:
        for step in ("build", "nextest"):
            path = self.build_reservation(shape=build_shape(["cargo", "build"]), need=16)
            row = cast(dict[str, object], json.loads(path.read_text()))
            row["step"] = step
            _ = path.write_text(json.dumps(row))
            memory = gate.reservation_memory(path, cast(gate.Reservation, cast(object, row)), NOW,
                                             gate.process_table(self.root / "proc"), self.root / "proc", policy=admission_policy_fixture())
            with self.subTest(step=step):
                self.assertEqual(gate.charge_basis(memory), gate.UsingNow(GIB))
                self.assertEqual((gate.build_charge(memory), gate.growth_headroom(memory)), (GIB, 0))
                if not isinstance(memory, gate.MeasuredMemory):
                    self.fail("fixture step must have a current reading")
                self.assertEqual(memory.promise, gate.NoPromise())



















class BuildFinalizerTests(BuildPromiseFixture):
    def test_failed_peak_refresh_precedes_indexing_and_zero_compile_evidence_removes_it(self) -> None:
        with AdmissionFixture() as fixture, patch.object(self, "root", fixture.state_dir.parent):
            fixture.clock.epoch = NOW_EPOCH
            before = self.estimate()
            request = fixture.request(step="build", owner=fixture.fixture_owner(100, "publisher"))
            assert isinstance(request.profile, gate.StepProfile)
            request = replace(request, argv=tuple(RELEASE_BUILD), profile=gate.StepProfile(
                "hana", "build", "hana-clone", before, HANA, "full", NOW_EPOCH,
                request.profile.build_history_generation), sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 16 * GIB)
            self.assertEqual(fixture.server.handle_request(
                wire.ReleaseRequest("publish-before-index", grant.reservation, 137)), [wire.Ok()])
            refreshed = self.estimate(now=NOW + timedelta(seconds=1))
            self.assertGreater(refreshed.bytes, before.bytes)
            self.assertEqual(refreshed.count, 1)
            self.assertEqual(refreshed.bytes,
                             min(21 * GIB, gate.shape_headroom_bytes(16 * GIB, self.release_headroom)))
            self.assertIsInstance(self.same_shape_basis(refreshed).floor, gate.FailedPeakFloor)
            assert isinstance(request.call, gate.CallRecord)
            self.index_build(request.call.call_id, RELEASE_BUILD, crates=0)
            self.assertEqual(self.estimate(now=NOW + timedelta(seconds=1)), before)

    def test_publication_during_a_delayed_generation_query_cannot_admit_the_old_need(self) -> None:
        with AdmissionFixture() as fixture, patch.object(self, "root", fixture.state_dir.parent):
            fixture.clock.epoch = NOW_EPOCH
            for _ in range(9):
                self.sample(10)
            stale = self.estimate()
            self.assertAlmostEqual(stale.bytes / GIB, 13.14, places=6)
            build = fixture.request(step="build", owner=fixture.fixture_owner(100, "first"))
            assert isinstance(build.profile, gate.StepProfile)
            build = replace(build, argv=tuple(RELEASE_BUILD), profile=gate.StepProfile(
                "hana", "build", "hana-clone", stale, HANA, "full", NOW_EPOCH,
                build.profile.build_history_generation), sidecar=str(fixture.scope(build.owner)))
            grant = only_admitted(fixture.server.handle_request(build))
            fixture.scoped_peak(grant, 16 * GIB)
            owner = fixture.fixture_owner(400, "next")

            def delayed_generation(configuration: gate.MemoryConfig | None = None) -> gate.BuildHistoryGeneration:
                self.assertIsInstance(configuration, gate.MemoryConfig)
                fixture.clock.advance(10)
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "publish-during-query", grant.reservation, 137)), [wire.Ok()])
                fixture.clock.advance(90)
                fixture.free(18 * GIB)
                return fixture.build_history_generation()

            with patch.object(client, "CLOCK", fixture.clock), \
                    patch.object(client, "build_history_generation", side_effect=delayed_generation) as query:
                request = client.step_request(RELEASE_BUILD, str(fixture.root), owner,
                    NOW_EPOCH, "", "after-delayed-query")
            query.assert_called_once()
            current = self.estimate(now=datetime.fromtimestamp(fixture.clock.now(), UTC))
            self.assertEqual(current.bytes, 21 * GIB)
            self.assertLess(stale.bytes + 3 * GIB, 18 * GIB)
            assert isinstance(request.profile, gate.StepProfile)
            reply, = fixture.server.handle_request(request)
            self.assertIsInstance(reply, wire.Wait, "the stale need must not receive a grant")
            assert isinstance(reply, wire.Wait)
            if not isinstance(reply.reason, gate.EstimateExpired):
                self.assertEqual(request.profile.peak, current)
                self.assertEqual(reply.snapshot.decision.state, "hold")
            self.assertNotIn(request.episode, fixture.server.reservations)
            history, = [row for row in fixture.history() if row.get("reservation_id") == grant.reservation]
            self.assertEqual(history["anon_peak_bytes"], 16 * GIB)

    def test_shared_reservation_reader_rejects_missing_required_fields(self) -> None:
        for missing in ("all", "start_time"):
            with self.subTest(missing=missing):
                path = self.build_reservation()
                row = cast(dict[str, object], json.loads(path.read_text()))
                if missing == "all":
                    row.clear()
                else:
                    del row["start_time"]
                _ = path.write_text(json.dumps(row))
                before = path.read_bytes()
                self.assertEqual(gate.reservation_rows(path.parent), [])
                self.assertEqual(path.read_bytes(), before)

    def test_malformed_finalization_intents_are_ignored_by_cleanup_and_release(self) -> None:
        valid: dict[str, object] = {"reservation_id": "ended-build", "peak": int(15.76 * GIB),
            "scoped": True, "outcome": "killed", "ended_at": store.utc_iso(NOW_EPOCH), "status": 137}
        malformed: list[object] = [None, {}]
        for field in ("reservation_id", "peak", "scoped", "outcome", "ended_at"):
            malformed.append({key: value for key, value in valid.items() if key != field})
        for field, value in (("reservation_id", 1), ("peak", "large"), ("peak", True),
                             ("scoped", 1), ("outcome", "unknown"), ("ended_at", None),
                             ("status", "137"), ("status", True)):
            malformed.append({**valid, field: value})
        for intent in malformed:
            with self.subTest(intent=intent):
                path = self.build_reservation(sidecar=True)
                row = cast(dict[str, object], json.loads(path.read_text()))
                row["finalization"] = intent
                _ = path.write_text(json.dumps(row))
                maximum = path.with_suffix(".max")
                _ = maximum.write_text(str(int(15.76 * GIB)))
                files = (path, maximum, self.root / "100.cgroup")
                before = {file: file.read_bytes() for file in files}
                self.assertEqual(gate.reservation_rows(path.parent), [])
                self.assertEqual({file: file.read_bytes() for file in files}, before)
                self.assertFalse((path.parent / "anon_peaks.jsonl").exists())


    def test_saved_peak_floor_expires_cached_estimates_after_legacy_join_and_append_retry(self) -> None:
        for legacy, crash in ((True, False), (False, True)):
            with self.subTest(legacy=legacy, crash=crash), AdmissionFixture() as fixture, \
                    patch.object(self, "root", fixture.state_dir.parent):
                fixture.clock.epoch = NOW_EPOCH
                for _ in range(9):
                    self.sample(10)
                generation = fixture.build_history_generation()
                peak = self.estimate()
                profile = gate.StepProfile("hana", "build", "hana-clone", peak, HANA,
                    "full", NOW_EPOCH, generation)
                owner = fixture.fixture_owner(100, "first")
                request = fixture.request(step="build", owner=owner)
                request = replace(request, argv=tuple(RELEASE_BUILD), profile=profile,
                    sidecar=str(fixture.scope(owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                if legacy:
                    self.index_build("old-reservation", RELEASE_BUILD)
                    fixture.server.reservations[request.episode] = replace(
                        fixture.server.reservations[request.episode],
                        shape="", call=gate.CallRecord("old-reservation"))
                fixture.scoped_peak(grant, int(15.76 * GIB))
                fixture.free(0)
                waiter = replace(fixture.request(step="build", owner=fixture.fixture_owner(400, "warmup")),
                    argv=tuple(RELEASE_BUILD), profile=profile)
                _ = fixture.server.handle_request(waiter)
                before = dict(fixture.server.waiters[waiter.episode].row)
                release = wire.ReleaseRequest("new-floor", grant.reservation, 137)
                if crash:
                    fixture.fault.stage = "after_history_append"
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.server.handle_request(release)
                    fixture.restart()
                    _ = fixture.server.handle_request(waiter)
                else:
                    _ = fixture.server.handle_request(release)
                fixture.free(18 * GIB)
                _ = fixture.server.run_pass()
                self.assertNotIn(waiter.episode, fixture.server.reservations,
                    "the saved floor must invalidate the cached estimate")
                waiting = fixture.server.waiters[waiter.episode]
                held = waiting.last_reply
                assert isinstance(held, wire.Wait)
                self.assertIsInstance(held.reason, gate.EstimateExpired)
                self.assertEqual(waiting.row["since"], before["since"])
                fixture.clock.advance(1)
                generation = fixture.build_history_generation()
                refreshed = self.estimate(now=NOW + timedelta(seconds=1))
                self.assertGreater(refreshed.bytes, peak.bytes)
                self.assertGreater(refreshed.bytes + 3 * GIB, 18 * GIB)
                next_attempt = replace(waiter, profile=replace(profile, peak=refreshed,
                    estimated_at=fixture.clock.now(), build_history_generation=generation))
                reply, = fixture.server.handle_request(next_attempt)
                assert isinstance(reply, wire.Wait)
                self.assertNotIsInstance(reply.reason, gate.EstimateExpired)
                self.assertEqual(reply.snapshot.peak, refreshed)
                self.assertEqual(reply.snapshot.decision.state, "hold")
                self.assertNotIn(waiter.episode, fixture.server.reservations)
                fixture.free(60 * GIB)
                admitted = only_admitted(fixture.server.handle_request(next_attempt))
                self.assertEqual(admitted.need, refreshed.bytes)
                self.assertEqual(fixture.server.reservations[waiter.episode].need, refreshed.bytes)


    def test_cli_refresh_reads_history_once_after_expiry_across_reconnect_and_restart(self) -> None:
        class RefreshChecked(BaseException):
            pass
        for restarted in (False, True):
            with self.subTest(restarted=restarted), AdmissionFixture() as fixture, \
                    patch.object(self, "root", fixture.state_dir.parent):
                fixture.clock.epoch = NOW_EPOCH
                for _ in range(9):
                    self.sample(10)
                generation = fixture.build_history_generation()
                profile = gate.StepProfile("hana", "build", "hana-clone", self.estimate(),
                    HANA, "full", NOW_EPOCH, generation)
                build = replace(fixture.request(step="build", owner=fixture.fixture_owner(100, "first")), argv=tuple(RELEASE_BUILD), profile=profile)
                build = replace(build, sidecar=str(fixture.scope(build.owner)))
                grant = only_admitted(fixture.server.handle_request(build))
                fixture.scoped_peak(grant, int(15.76 * GIB))
                owner = fixture.fixture_owner(400, "warmup")
                fixture.free(0)
                replies: list[wire.Wait] = []
                reads_at_expiry = 0
                history_reader = gate.anonymous_history_rows
                history_scans = 0
                def count_history_scans(path: Path) -> Iterator[dict[str, object]]:
                    nonlocal history_scans
                    history_scans += 1
                    return history_reader(path)
                def observe(reply: wire.Wait) -> None:
                    nonlocal reads_at_expiry
                    replies.append(reply)
                    with fixture.loop_turn():
                        if len(replies) == 1:
                            self.assertNotIsInstance(reply.reason, gate.EstimateExpired)
                            fixture.clock.advance(1)
                            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                                "floor-before-refresh", grant.reservation, 137)), [wire.Ok()])
                        elif len(replies) == 2:
                            self.assertIsInstance(reply.reason, gate.EstimateExpired)
                            if restarted:
                                fixture.restart()
                            fixture.clock.advance(1)
                            fixture.free(18 * GIB)
                            reads_at_expiry = history_scans
                        else:
                            self.assertEqual(len(replies), 3)
                            self.assertEqual(history_scans - reads_at_expiry, 1,
                                "one expiry must read history once")
                            self.assertNotIsInstance(reply.reason, gate.EstimateExpired)
                            self.assertGreater(reply.snapshot.peak.bytes, replies[0].snapshot.peak.bytes)
                            self.assertEqual(reply.snapshot.decision.state, "hold")
                            waiting, = fixture.server.waiters.values()
                            self.assertEqual(waiting.request.since, NOW_EPOCH)
                            self.assertEqual(waiting.request.owner, owner)
                            self.assertEqual(waiting.request.profile.peak, reply.snapshot.peak)
                            self.assertNotIn(waiting.request.episode, fixture.server.reservations)
                            raise RefreshChecked()
                with patch.object(client, "CLOCK", fixture.clock), \
                     patch.object(client.WaitOutput, "__call__", side_effect=observe), \
                     patch.object(gate, "anonymous_history_rows", side_effect=count_history_scans), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                     test_hang_guard.LauncherHangGuard(self.id()).protect(), fixture.serving():
                    with self.assertRaises(RefreshChecked):
                        _ = client.main(["admit", "--pid", str(owner.pid), "--since", str(NOW_EPOCH),
                                         "--", *RELEASE_BUILD])
                self.assertEqual(len(replies), 3)


class BuildNeedBasisTests(BuildPromiseFixture):
    def test_recorded_budget_cap_survives_a_larger_current_budget(self) -> None:
        basis = gate.ShapeFallback(gate.CappedAtBudget(22 * GIB, 21 * GIB))
        encoded = gate.need_basis_record(basis)
        self.assertEqual(encoded, {
            "kind": "shape_fallback",
            "cap": {"kind": "budget", "uncapped": 22 * GIB, "cap": 21 * GIB},
        })
        with patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "24"}):
            self.assertEqual(gate.read_need_basis(encoded), basis)

    def test_legacy_budget_cap_does_not_consult_the_current_budget(self) -> None:
        encoded = {"kind": "shape_fallback", "cap": {"kind": "budget", "uncapped": 22 * GIB}}
        for budget in ("21", "24"):
            with self.subTest(budget=budget), patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": budget}):
                basis = gate.read_need_basis(encoded)
                self.assertEqual(basis, gate.ShapeFallback(gate.CappedAtBudget(22 * GIB, gate.CapNotRecorded())))
                self.assertEqual(gate.read_need_basis(gate.need_basis_record(basis)), basis)

    def test_old_dev_factor_remains_readable_after_the_rule_changes(self) -> None:
        encoded: dict[str, object] = {
            "kind": "shape_largest", "runs": 2,
            "floor": {"kind": "none"},
            "factor": {"kind": "dev", "factor": gate.DEV_HEADROOM},
            "cap": {"kind": "none"},
        }
        basis = gate.read_need_basis(encoded)
        self.assertEqual(basis, gate.ShapeLargest(2, factor=gate.LegacyDevMultiplierHeadroom()))
        self.assertEqual(gate.need_basis_record(basis), encoded)

    def test_recorded_budget_cap_requires_a_positive_cap_below_the_uncapped_need(self) -> None:
        for cap in (0, -GIB, 22 * GIB, 23 * GIB, True, "21"):
            with self.subTest(cap=cap):
                self.assertEqual(gate.read_need_basis({
                    "kind": "shape_fallback",
                    "cap": {"kind": "budget", "uncapped": 22 * GIB, "cap": cap},
                }), gate.BasisNotRecorded())

    def test_dev_factor_overflow_is_not_a_recorded_basis(self) -> None:
        self.assertEqual(gate.read_need_basis({
            "kind": "shape_largest", "runs": 2,
            "floor": {"kind": "none"},
            "factor": {"kind": "dev", "factor": 10 ** 400},
            "cap": {"kind": "none"},
        }), gate.BasisNotRecorded())

    def test_release_factor_keeps_its_saved_value(self) -> None:
        encoded: dict[str, object] = {
            "kind": "shape_largest", "runs": 2,
            "floor": {"kind": "none"},
            "factor": {"kind": "release", "factor": 1.5},
            "cap": {"kind": "none"},
        }
        basis = gate.read_need_basis(encoded)
        self.assertEqual(basis, gate.ShapeLargest(2, factor=gate.ReleaseHeadroom(1.5)))
        self.assertEqual(gate.need_basis_record(basis), encoded)

    def test_waiter_records_tagged_basis_and_queue_reads_the_same_basis(self) -> None:
        for _ in range(9):
            self.sample(10)
        self.sample(15.76, outcome="killed")
        peak = self.estimate()
        with AdmissionFixture(root=self.root, log_dir=self.root) as fixture:
            fixture.clock.epoch = NOW_EPOCH
            fixture.free(0)
            request = fixture.request(step="build", owner=fixture.fixture_owner(400, "warmup"))
            request = replace(request, argv=tuple(RELEASE_BUILD), profile=gate.StepProfile(
                "hana", "build", "hana-clone", peak, HANA, "full", NOW_EPOCH))
            _ = fixture.server.handle_request(request)
            row = fixture.server.waiters[request.episode].row
            self.assertEqual(row.get("need_source"), "shape")
            basis = row.get("need_basis")
            self.assertIsInstance(basis, dict)
            serialized = wire.encode_record(basis)
            for value in (str(self.release_headroom.factor), str(int(15.76 * GIB)), HISTORY_AT.strftime("%Y-%m-%d")):
                self.assertIn(value, serialized)
            before = fixture.state_text()
            explanation = fixture.server.handle_request(wire.ExplainRequest())[0]
            assert isinstance(explanation, wire.ExplainReply)
            head = explanation.explanation.head
            assert isinstance(head, gate.HeadExplanation) and isinstance(head.need_source, gate.RecordedNeed)
            self.assertEqual(head.need_source.basis, peak.basis)
            self.assertEqual(fixture.state_text(), before)

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
        for basis in (gate.ShapeP90(10, floor, self.release_headroom, gate.CappedAtBudget(24 * GIB, 21 * GIB)),
                      gate.ShapeLargest(2, floor, self.release_headroom, gate.UnderBudget()),
                      gate.ShapeLargest(2, factor=gate.DevSizeHeadroom(1.25, 2 * GIB)),
                      gate.ShapeFallback(gate.CappedAtBudget(24 * GIB, 21 * GIB)),
                      gate.ShapeFallback(gate.CappedAtBudget(24 * GIB, gate.CapNotRecorded()))):
            with self.subTest(basis=basis):
                peak = gate.ExpectedPeak(GIB, "shape", 1, basis)
                self.assertEqual(gate.read_need_basis(gate.need_basis_record(peak.basis)), basis)

    def test_shape_estimate_refuses_a_missing_shape_basis(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            _ = gate.ExpectedPeak(GIB, "shape", 1)

    def test_old_queue_row_has_basis_not_recorded(self) -> None:
        path = self.waiter(400, NOW_EPOCH, "/tmp/build", "builder", need=12)
        row = gate.read_waiter(path)
        row["need_source"] = "fallback"
        _ = path.write_text(json.dumps(row))
        head = gate.explain_queue().head
        if not isinstance(head, gate.HeadExplanation) or not isinstance(head.need_source, gate.RecordedNeed):
            self.fail("the fixture must have a head with a recorded need")
        self.assertEqual(head.need_source.basis, gate.BasisNotRecorded())






class ProgramProfileTests(MemoryQueueFixture):
    def test_absent_history_uses_the_first_program_peak(self) -> None:
        history = gate.admission_dir(self.root) / "anon_peaks.jsonl"
        self.assertFalse(history.exists())
        self.assertEqual(list(gate.anonymous_history_rows(history)), [])
        self.assertEqual(gate.program_peak_estimate("merge-gate", "host", NOW, self.root, 4 * GIB),
                         gate.FirstProgramPeak(4 * GIB))

    def test_unfinished_rows_do_not_change_the_program_estimate(self) -> None:
        history = gate.admission_dir(self.root) / "anon_peaks.jsonl"
        _ = history.write_text(json.dumps({
            "step": "merge-gate", "host": "host", "anon_peak_bytes": 3 * GIB,
            "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
        }) + "\n")
        assert_unfinished_history_ignored(
            self, history, lambda: gate.program_peak_estimate("merge-gate", "host", NOW, self.root, 4 * GIB))

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root))))


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
        self.assertEqual(gate.program_peak_estimate("merge-gate", "host", NOW, self.root, 4 * GIB), gate.MeasuredProgramPeak(9 * GIB, 10))

    def test_expired_measured_gate_profile_retains_its_original_fallback_cap(self) -> None:
        profile = gate.ProgramProfile("merge-gate", "installed/.claude", "checkout",
            gate.HistoryPeakSizing(4 * GIB, gate.MeasuredProgramPeak(GIB, 1),
                NOW_EPOCH - admission_policy_fixture().timing.head_seconds))
        with patch.object(gate, "git_identity", side_effect=AssertionError("the program inferred a cargo identity")):
            result = gate.step_estimate(["merge-gate"], "launcher", profile, NOW, self.root, admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)
        self.assertEqual(result.profile.peak, gate.ExpectedPeak(4 * GIB, "fallback", 0))
        assert isinstance(result.profile, gate.ProgramProfile)
        self.assertEqual(cast(gate.HistoryPeakSizing, result.profile.sizing).estimate, gate.FirstProgramPeak(4 * GIB))
        self.assertEqual((result.profile.repo, result.profile.step, result.profile.worktree),
                         ("installed/.claude", "merge-gate", "checkout"))

    def test_expired_gate_profile_refreshes_outside_lock_without_cargo_inference(self) -> None:
        profile = gate.ProgramProfile("merge-gate", "installed/.claude", "checkout",
            gate.HistoryPeakSizing(4 * GIB, gate.FirstProgramPeak(4 * GIB),
                NOW_EPOCH - admission_policy_fixture().timing.head_seconds))
        def estimate(program: str, host: str, now: datetime, root: Path, cap: int) -> gate.MeasuredProgramPeak:
            self.assertEqual((program, cap, now, root), ("merge-gate", 4 * GIB, NOW, self.root))
            self.assertTrue(host)
            return gate.MeasuredProgramPeak(6 * GIB, 2)
        with patch.object(gate, "program_peak_estimate", side_effect=estimate) as refreshed, \
             patch.object(gate, "git_identity", side_effect=AssertionError("the program inferred a cargo identity")), \
             patch.object(fcntl, "flock", side_effect=AssertionError("the estimate acquired an admission lock")):
            result = gate.step_estimate(["merge-gate"], "launcher", profile, NOW, self.root, admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)
        refreshed.assert_called_once()
        self.assertEqual((result.profile.repo, result.profile.step, result.profile.worktree, result.profile.peak.bytes),
                         ("installed/.claude", "merge-gate", "checkout", 6 * GIB))
        self.assertEqual(result.profile.estimated_at, NOW_EPOCH)





class FixedCapAdmissionTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root, total_kib=60 * GIB // 1024),
            "BUILDLOG_BUILD_BUDGET_GIB": "21", "LINT_CONFIG_FILE": str(config),
        })))







    def test_program_cap_basis_round_trips_for_each_program(self) -> None:
        for program in ("merge-gate", "rust-analyzer"):
            with self.subTest(program=program):
                basis = gate.ProgramMemoryCap(program)
                self.assertEqual(gate.read_need_basis(gate.need_basis_record(basis)), basis)


    def test_fixed_cap_promise_uses_the_recorded_source_for_other_programs(self) -> None:
        path = self.running(100, 6, age=admission_policy_fixture().timing.ramp_seconds + 1,
                            fields={"step": "rust-analyzer", "queue_waiter": {"need_source": "cap"}})
        row = gate.read_reservation(path)
        memory = gate.step_promise(row, NOW, gate.MeasuredMemory(GIB), admission_policy_fixture())
        self.assertEqual(gate.charge_basis(memory), gate.LifetimePromise(6 * GIB))
        self.assertEqual(gate.growth_headroom(memory), 5 * GIB)
        for used, observed in ((7, 0), (1, 8)):
            with self.subTest(used=used, observed=observed):
                memory = gate.step_promise(row, NOW, gate.MeasuredMemory(used * GIB, observed_peak=observed * GIB), admission_policy_fixture())
                self.assertEqual(gate.build_charge(memory), max(6, used, observed) * GIB)

    def test_cap_waiter_admits_when_lock_wait_exceeds_head_seconds(self) -> None:
        profile = gate.ProgramProfile("merge-gate", "repo", "tree", gate.FixedCapSizing(6 * GIB))
        policy = gate.read_memory_config().policy
        row: gate.Waiter = {"pid": 400, "start_time": "warmup", "need": profile.peak.bytes,
            "since": NOW_EPOCH, "estimated_at": NOW_EPOCH, "need_source": profile.peak.source,
            "need_basis": gate.need_basis_record(profile.peak.basis)}
        self.assertFalse(gate.estimate_expired(row, NOW_EPOCH + policy.timing.head_seconds + 1, policy=policy))
        self.assertEqual(gate.read_need_basis(row.get("need_basis")), gate.ProgramMemoryCap("merge-gate"))

    def test_history_waiter_expires_when_lock_wait_exceeds_head_seconds(self) -> None:
        policy = gate.read_memory_config().policy
        row: gate.Waiter = {"pid": 400, "start_time": "warmup", "need": 6 * GIB,
            "since": NOW_EPOCH, "estimated_at": NOW_EPOCH, "need_source": "measured"}
        self.assertTrue(gate.estimate_expired(row, NOW_EPOCH + policy.timing.head_seconds, policy=policy))
        self.assertEqual(row["estimated_at"], NOW_EPOCH)









class CurrentBlockerTests(MemoryQueueFixture):
    reservation: Path = Path()
    waiter_path: Path = Path()

    def blocker(self) -> gate.Blocker:
        policy = gate.read_memory_config().policy
        processes = gate.process_table(self.root / 'proc')
        inputs = fixture_queue_inputs(gate.admission_dir(self.root), processes, NOW_EPOCH, policy)
        owner = self.waiter_path.stem
        inputs = replace(inputs, blockers_for=frozenset({owner}))
        result = gate.admission_pass(inputs, NOW_EPOCH, gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), policy)
        row = next((row for row in result.rows if row.owner == owner))
        return gate.blocker_identity(row.blocker, list(inputs.reservations), processes, self.root / 'proc')

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root, available_kib=26 * GIB // 1024, total_kib=60 * GIB // 1024))))
        _ = self.enterContext(patch.object(time, 'time', return_value=NOW.timestamp()))
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        self.reservation = self.running(100, 12)
        process = self.root / 'proc/100'
        process.mkdir(parents=True)
        _ = (process / 'environ').write_bytes(b'CLAUDE_CODE_SESSION_ID=owner-id\x00CLAUDE_SESSION_NAME=owner-name\x00')
        self.waiter_path = self.root / 'admission/400-warmup.waiting'
        _ = self.waiter_path.write_text(json.dumps({'pid': 400, 'start_time': 'warmup', 'need': 12 * GIB, 'since': NOW.timestamp()}))

    def test_blocker_names_the_session_that_would_release_admission(self) -> None:
        result = self.blocker()
        self.assertIsInstance(result, gate.ReservationBlocker)
        self.assertEqual(gate.blocker_fields(self.blocker()), {'session_id': 'owner-id', 'session_name': 'owner-name', 'pool': 'memory'})

    def test_check_reports_holder_changes_on_existing_poll_with_opt_in_field(self) -> None:
        for name in ("owner-name", "new-owner"):
            _ = (self.root / "proc/100/environ").write_text(
                f"CLAUDE_CODE_SESSION_ID=owner-id\0CLAUDE_SESSION_NAME={name}\0")
            result = self.blocker()
            self.assertEqual(result, gate.ReservationBlocker("owner-id", name))
            self.assertEqual(gate.blocker_fields(result), {
                "session_id": "owner-id", "session_name": name, "pool": "memory"})
        self.readings[100] = gate.MeasuredMemory(GIB)
        self.assertIsInstance(self.blocker(), gate.NoSingleBlocker)

    def test_blocker_names_only_the_scope_when_session_is_unknown(self) -> None:
        (self.root / 'proc/100/environ').unlink()
        sidecar = self.root / 'holder.cgroup'
        _ = sidecar.write_text('/fake/cgroup/builds.slice/build-holder.scope\n')
        row = cast(dict[str, object], json.loads(self.reservation.read_text()))
        row['sidecar'] = str(sidecar)
        _ = self.reservation.write_text(json.dumps(row))
        self.assertIsInstance(self.blocker(), gate.UnitBlocker)
        self.assertEqual(gate.blocker_fields(self.blocker()), {'unit': 'build-holder.scope'})

    def test_no_single_blocker_for_pressure_ready_ambiguous_or_unknown_owner(self) -> None:
        for reason in ('pressure', 'ready', 'ambiguous', 'unknown-owner', 'no-reservation'):
            with self.subTest(reason=reason):
                with patch.dict(os.environ, fake_memory_readings(self.root, available_kib=26 * GIB // 1024, total_kib=60 * GIB // 1024, full_avg10=11 if reason == 'pressure' else 0)):
                    self.readings[100] = gate.MeasuredMemory(GIB if reason == 'ready' else 12 * GIB)
                    extra = self.root / 'admission/200.reservation'
                    if reason == 'ambiguous':
                        self.readings[100] = gate.MeasuredMemory(6 * GIB)
                        self.readings[200] = gate.MeasuredMemory(6 * GIB)
                        extra = self.running(200, 6)
                    if reason == 'unknown-owner':
                        (self.root / 'proc/100/environ').unlink()
                    if reason == 'no-reservation':
                        self.reservation.unlink()
                    self.assertIsInstance(self.blocker(), gate.NoSingleBlocker)
                    self.assertEqual(gate.blocker_fields(self.blocker()), {})
                    extra.unlink(missing_ok=True)

    def test_removing_one_of_several_reservations_must_be_enough(self) -> None:
        self.readings[100] = gate.MeasuredMemory(18 * GIB)
        self.readings[200] = gate.MeasuredMemory(10 * GIB)
        _ = self.running(200, 10)
        self.assertEqual(gate.blocker_fields(self.blocker()), {})
        self.readings[200] = gate.MeasuredMemory(2 * GIB)
        self.assertEqual(gate.blocker_fields(self.blocker()), {'session_id': 'owner-id', 'session_name': 'owner-name', 'pool': 'memory'})

    def test_queue_priority_is_not_blame_for_a_running_reservation(self) -> None:
        _ = self.head(need=15)
        waiter = cast(gate.Waiter, json.loads(self.waiter_path.read_text()))
        waiter['need'] = 8 * GIB
        _ = self.waiter_path.write_text(json.dumps(waiter))
        self.assertEqual(gate.blocker_fields(self.blocker()), {})

    def test_measured_memory_release_can_remove_the_free_memory_hold(self) -> None:
        _ = self.meminfo.write_text(f'MemAvailable: {8 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n')
        self.assertEqual(gate.blocker_fields(self.blocker()), {'session_id': 'owner-id', 'session_name': 'owner-name', 'pool': 'memory'})

    def test_missing_process_reading_is_not_reported_as_a_gone_waiter(self) -> None:
        with patch.object(gate, 'process_table', side_effect=FileNotFoundError('missing process snapshot')):
            with self.assertRaisesRegex(FileNotFoundError, 'missing process snapshot'):
                _ = gate.blocker_fields(self.blocker())

    def test_blocker_does_not_prune_or_change_state_bytes_or_mtimes(self) -> None:
        directory = self.root / 'admission'
        _ = (directory / 'stale.waiting').write_text('{"pid":999,"start_time":"gone"}')
        _ = (directory / 'stale.reservation').write_text('{"pid":999,"start_time":"gone"}')
        _ = (directory / 'stale.max').write_text('123')
        _ = (directory / 'broken.reservation').write_text('partial')

        def snapshot() -> dict[str, tuple[bytes, int]]:
            return {str(path.relative_to(self.root)): (path.read_bytes() if path.is_file() else b'', path.stat().st_mtime_ns) for path in [self.root, *self.root.rglob('*')]}
        before = snapshot()
        self.assertEqual(gate.blocker_fields(self.blocker()).get('session_id'), 'owner-id')
        self.assertEqual(snapshot(), before)


class MeasuredAdmissionTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root))))

    def propose(self, need: float, free: float) -> gate.PassRow:
        return self.pass_for(400, need, free)

    def test_five_mature_steps_with_43_point_4_promised_admit_10_point_8_on_6_point_8_used(self) -> None:
        for pid in (100, 200, 500, 600, 700):
            self.processes[pid] = str(pid)
            self.readings[pid] = gate.MeasuredMemory(int(1.36 * GIB))
            _ = self.running(pid, 8.68)
        result = self.propose(10.8, 18.1)
        self.assertIsInstance(result.admission, gate.ProposedAdmission)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))
        self.assertAlmostEqual(result.decision.charged / GIB, 6.8)

    def test_mature_steps_using_12_gib_hold_10_point_8_against_the_budget(self) -> None:
        _ = self.running(100, 43.4)
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        result = self.propose(10.8, 18.1)
        self.assertEqual((result.decision.state, result.decision.charged), ("hold", 12 * GIB))
        self.assertGreater(18.1 * GIB, result.decision.threshold)
        self.assertIsInstance(result.reason, gate.HeadAdmissionStatus)
        view = gate.queue_read().view
        self.assertEqual(view.waiting[0].reason, result.reason)

    def test_22_gib_step_runs_alone_despite_the_budget(self) -> None:
        result = self.propose(22, 30)
        self.assertIsInstance(result.admission, gate.ProposedAdmission)

    def test_memory_floor_still_holds_with_room_in_the_budget(self) -> None:
        _ = self.running(100, 12)
        result = self.propose(10.8, 9)
        self.assertEqual(result.decision.state, "hold")
        self.assertLess(result.decision.charged + int(10.8 * GIB), result.decision.budget)

    def test_ramp_charge_covers_measured_growth_above_the_promise(self) -> None:
        _ = self.running(100, 2, age=admission_policy_fixture().timing.ramp_seconds - 1)
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        result = self.propose(10, 40)
        self.assertEqual((result.decision.state, result.decision.charged, result.decision.promised),
                         ("hold", 12 * GIB, 0))

    def test_fresh_step_keeps_its_promise_then_uses_its_first_reading(self) -> None:
        _ = self.running(100, 23.5, age=0)
        self.assertEqual(self.propose(10.8, 16).decision.promised, int(22.5 * GIB))
        _ = self.running(100, 23.5, age=admission_policy_fixture().timing.ramp_seconds)
        result = self.propose(10.8, 16)
        self.assertEqual((result.decision.promised, result.decision.measured), (0, GIB))

    def test_reading_a_finished_tree_does_not_keep_a_promise(self) -> None:
        _ = self.running(100, 23.5)
        self.readings[100] = gate.GoneMemory()
        result = self.propose(10.8, 15)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))

    def test_check_charges_only_measured_use_after_ramp(self) -> None:
        _ = self.running(100, 12)
        for used, state in ((1, "admit"), (11, "admit"), (13, "hold")):
            with self.subTest(measured_gib=used):
                self.readings[100] = gate.MeasuredMemory(used * GIB)
                result = self.propose(8, 15)
                self.assertEqual((result.decision.state, result.decision.promised), (state, 0))

    def test_two_10_point_8_steps_cannot_start_together_inside_ramp(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {40 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.waiter(100, NOW_EPOCH - 1, "first", "first", 10.8)
        _ = self.waiter(200, NOW_EPOCH, "second", "second", 10.8)
        policy = gate.read_memory_config().policy
        inputs = fixture_queue_inputs(gate.admission_dir(self.root), gate.process_table(self.root / "proc"), NOW_EPOCH, policy)
        first, second = gate.admission_pass(inputs, NOW_EPOCH, gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), policy).rows
        self.assertIsInstance(first.admission, gate.ProposedAdmission)
        self.assertEqual((second.decision.state, second.decision.charged), ("hold", int(10.8 * GIB)))

    def test_pending_start_promise_cannot_make_a_head_fit_after_drain(self) -> None:
        _ = self.running(100, 12, age=0)
        _ = self.head(20)
        result = self.propose(2, 18)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 11 * GIB))
        self.assertIsInstance(result.decision.head_room, gate.NoHeadRoom)

    def test_head_beyond_memory_released_by_running_steps_does_not_hold_smaller_steps(self) -> None:
        _ = self.running(100, 1)
        _ = self.head(20)
        result = self.propose(2, 8)
        self.assertEqual(result.decision.state, "admit")
        self.assertIsInstance(result.decision.head_room, gate.NoHeadRoom)

    def test_head_that_fits_after_measured_steps_end_holds_smaller_steps(self) -> None:
        _ = self.running(100, 30)
        self.readings[100] = gate.MeasuredMemory(int(21.5 * GIB))
        _ = self.head(19.3)
        result = self.propose(2, 15)
        self.assertEqual(result.decision.state, "hold")
        self.assertIsInstance(result.admission, gate.NotAdmitted)
        self.assertIsInstance(result.decision.order_hold, gate.NoOrderHold)

    def test_head_budget_keeps_room_before_warmup(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        _ = self.head()
        result = self.pass_for(400, need=10.8, free=40)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual((result.decision.promised, result.decision.measured), (0, 3 * GIB))
        self.assertEqual(result.reason, gate.BehindHead("clone88ci nextest (verify pid 300)", NOW.timestamp() - 2400, ""))

    def test_clone88ci_head_starts_when_measured_free_memory_covers_it(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        _ = self.head()
        result = self.pass_for(pid=300, need=10.9, free=35)
        self.assertEqual(result.decision.state, "admit")
        self.assertIsInstance(result.decision.head_room, gate.NoHeadRoom)
        self.assertIsInstance(result.admission, gate.ProposedAdmission)


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




    def test_scoped_step_and_queue_totals_exclude_swap_and_inactive_file_cache(self) -> None:
        sidecar = self.root / "step.cgroup"
        path = self.running(100, 1, sidecar=str(sidecar))
        scope = Path(sidecar.read_text())
        scope.mkdir(parents=True)
        _ = (scope / "cgroup.procs").write_text("100\n")
        _ = (scope / "memory.current").write_text(str(3 * GIB))
        _ = (scope / "memory.stat").write_text(f"inactive_file {GIB}\n")
        _ = (scope / "memory.swap.current").write_text(str(7 * GIB))
        row = gate.read_reservation(path)
        reading = gate.step_memory(row, NOW, gate.process_table(self.root / "proc"),
                                   self.root / "proc", policy=admission_policy_fixture())
        self.assertIsInstance(reading, gate.MeasuredMemory)
        assert isinstance(reading, gate.MeasuredMemory)
        self.assertEqual((reading.bytes, reading.swap_bytes), (2 * GIB, 7 * GIB))
        step = gate.RunningStep("owner", "owner", "nextest", "/tree", GIB, reading)
        inputs = gate.QueueInputs([], (step,), (row,), gate.HostMemory(40 * GIB, 60 * GIB),
            gate.NoPressureHold(), gate.NoRecoverySpacing(), gate.NoHeadAdmissionOverride(), NOW.timestamp())
        result = gate.queue_read_from(inputs, gate.AdmissionLimits(21 * GIB, 3 * GIB),
                                      policy=admission_policy_fixture())
        self.assertEqual(result.explanation.totals.measured, 2 * GIB)
        self.assertEqual(result.explanation.totals.charged, 2 * GIB)

    def test_scope_counter_disappearing_uses_owner_lifetime(self) -> None:
        reservation = self.running(100, 12)
        scope = self.builds / "ending.scope"
        scope.mkdir(parents=True)
        _ = (scope / "cgroup.procs").write_text("101\n")
        _ = (scope / "memory.stat").write_text("inactive_file 0\n")
        sidecar = self.root / "scope-path"
        _ = sidecar.write_text(str(scope))
        row = cast(gate.Reservation, json.loads(reservation.read_text()))
        row["sidecar"] = str(sidecar)
        for missing in ("memory.current", "memory.swap.current"):
            for owner in ("live", "gone", "reused"):
                with self.subTest(missing=missing, owner=owner):
                    for counter in ("memory.current", "memory.swap.current"):
                        _ = (scope / counter).write_text("0")
                    _ = (scope / "memory.current").write_text(str(3 * GIB))
                    (scope / missing).unlink()
                    processes = {} if owner == "gone" else {100: gate.ProcessIdentity(1, "first" if owner == "live" else "other")}
                    with patch.object(gate, "tree_memory", return_value=gate.MeasuredMemory(2 * GIB)) as tree:
                        reading = gate.step_memory(row, NOW, processes, self.root / "proc", policy=admission_policy_fixture())
                    if missing == "memory.swap.current":
                        self.assertEqual(reading, gate.MeasuredMemory(3 * GIB, swap_bytes=0))
                        tree.assert_not_called()
                    elif owner == "live":
                        self.assertEqual(reading, gate.MeasuredMemory(2 * GIB))
                        tree.assert_called_once_with(100, "first", processes, self.root / "proc")
                    else:
                        self.assertIsInstance(reading, gate.GoneMemory)
                        tree.assert_not_called()











    def test_sampler_replaces_peak_without_changing_open_snapshot(self) -> None:
        scope = self.root / "scope"
        scope.mkdir()
        _ = (scope / "memory.stat").write_text(f"anon {13 * GIB}\n")
        sidecar = self.root / "scope-path"
        _ = sidecar.write_text(str(scope) + "\n")
        maximum = self.root / "sample.max"
        _ = maximum.write_text(str(12 * GIB) + "\n")
        with maximum.open() as before:
            result = sample_anon_once(sidecar, maximum)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(int(maximum.read_text()), 13 * GIB)
            self.assertEqual(int(before.read()), 12 * GIB)
















    def test_caught_live_cgroup_refusal_still_records_the_read(self) -> None:
        record = self.root / "caught-reads.jsonl"
        live = Path("/sys/fs/cgroup/test-builds/sccache.service/memory.current")
        with patch.dict(os.environ, {"BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(record),
                                    "BUILDLOG_BUILDS_CGROUP": str(live.parent.parent)}):
            with self.assertRaises(gate.LiveMemoryReading), patch.object(Path, "read_text", side_effect=AssertionError("live read")):
                _ = gate.sccache_memory()
        records = [cast(dict[str, object], json.loads(line)) for line in record.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reading"], str(live))
        self.assertEqual(records[0]["pid"], os.getpid())
        self.assertEqual(records[0]["argv"], sys.argv)

    def admit_at(self, pid: int = 400, seconds: float = 0, need: float = 8) -> gate.PassRow:
        return self.pass_for(pid, need, 40, seconds=seconds)

    def test_peak_charge_expires_at_1500_seconds_since_near_peak_use(self) -> None:
        for age, expected_charge, expected_state in ((admission_policy_fixture().peak_hold.default_seconds - 1, 13, "hold"),
                                                  (admission_policy_fixture().peak_hold.default_seconds, 2, "admit")):
            with self.subTest(age=age):
                self.readings_for()
                reservation = self.running(100, 12)
                _ = reservation.with_suffix(".max").write_text(str(13 * GIB))
                _ = reservation.with_suffix(".wave").write_text(f"{13 * GIB} {int(NOW.timestamp()) - age}")
                self.readings[100] = gate.MeasuredMemory(2 * GIB)
                result = self.admit_at()
                self.assertEqual((result.decision.state, result.decision.charged),
                                 (expected_state, expected_charge * GIB))

    def test_missing_near_peak_record_uses_peak_publication_time(self) -> None:
        for age, expected_charge, expected_state in ((0, 13, "hold"),
                (admission_policy_fixture().peak_hold.default_seconds - 1, 13, "hold"),
                (admission_policy_fixture().peak_hold.default_seconds, 2, "admit")):
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
                    self.assertEqual(result.decision.state, "hold")
                    self.assertEqual(result.decision.charged, max(13, current) * GIB)

    def test_swap_and_some_need_both_strict_thresholds(self) -> None:
        for index, (free, some, expected) in enumerate(((14, 26, "hold"), (15, 26, "admit"),
                                     (14, 25, "admit"), (90, 37, "admit"), (1, 0, "admit"))):
            with self.subTest(swap_free=free, some=some):
                self.readings_for(swap_free=free, some=some)
                result = self.admit_at(seconds=120 * index)
                self.assertEqual(result.decision.state, expected)
                if expected == "hold":
                    self.assertIsInstance(result.reason, gate.HeadAdmissionStatus)
                    if isinstance(result.reason, gate.HeadAdmissionStatus):
                        self.assertIn("swap", gate.head_wait_text(result.reason, ZoneInfo("UTC"), policy=admission_policy_fixture()))

    def test_freeze_study_recorded_peaks_hold_even_at_lowest_pressure(self) -> None:
        """The recorded peaks hold the queue without a pressure hold."""
        recorded = ((100, '18:35:00.535', '19:05:45.268', 8433098752), (200, '18:35:12.084', '18:55:32.895', 2768523264), (300, '18:42:53.156', '19:20:47.547', 11967815680))
        for pid, start, end, peak in recorded:
            self.assertLess(start, '18:47:12')
            self.assertGreater(end, '18:47:54')
            reservation = self.running(pid, 1)
            _ = reservation.with_suffix('.max').write_text(str(peak))
            os.utime(reservation.with_suffix('.max'), (NOW.timestamp(), NOW.timestamp()))
            self.readings[pid] = gate.MeasuredMemory(0)
        service = self.builds / 'sccache.service'
        service.mkdir(parents=True)
        _ = (service / 'memory.stat').write_text(f'anon {int(5.95 * GIB)}\ninactive_file 0\n')
        _ = (service / 'memory.current').write_text(str(int(5.95 * GIB)))
        self.readings_for(swap_free=20, some=29, full=0)
        charged_peaks = sum((peak for _, _, _, peak in recorded))
        with patch.dict(os.environ, {'BUILDLOG_BUILD_BUDGET_GIB': '21'}):
            for second, need in ((0, 3.14), (14, 3.14), (28, 3.02), (42, 1.0)):
                for pid, started, peak in ((500, 7.023, 3011469312), (600, 38.947, 2664980480)):
                    if second >= started and pid not in self.processes:
                        self.processes[pid] = str(pid)
                        self.readings[pid] = gate.MeasuredMemory(0)
                        reservation = self.running(pid, 3.14, age=-started)
                        _ = reservation.with_suffix('.max').write_text(str(peak))
                        published_at = NOW.timestamp() + second
                        os.utime(reservation.with_suffix('.max'), (published_at, published_at))
                        charged_peaks += max(peak, int(3.14 * GIB))
                result = self.admit_at(seconds=second, need=need)
                self.assertEqual(result.decision.state, 'hold', f'recorded peaks at +{second}s')
                self.assertIsInstance(result.decision.pressure, gate.NoPressureHold)
                self.assertEqual(result.decision.charged, charged_peaks + int(5.95 * GIB))
                self.assertGreater(result.decision.charged, result.decision.budget)

    def test_sccache_anon_is_one_charge_and_missing_or_unreadable_is_zero(self) -> None:
        self.readings_for()
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(8 * GIB)
        service = self.builds / "sccache.service"
        service.mkdir(parents=True)
        stat = service / "memory.stat"
        _ = stat.write_text(f"anon {6 * GIB}\nfile {90 * GIB}\ninactive_file {90 * GIB}\n")
        _ = (service / "memory.current").write_text(str(96 * GIB))
        result = self.admit_at()
        self.assertEqual((result.decision.state, result.decision.charged), ("hold", 14 * GIB))
        view = gate.queue_read().view
        self.assertEqual(len(view.running), 2)
        self.assertEqual(view.waiting[0].reason, result.reason)
        stat.unlink()
        for unreadable in (False, True):
            if unreadable:
                stat.mkdir()
            result = self.admit_at()
            self.assertEqual((result.decision.state, result.decision.charged), ("admit", 8 * GIB))

    def test_sccache_is_charged_but_does_not_prevent_running_alone(self) -> None:
        service = self.builds / "sccache.service"
        service.mkdir(parents=True)
        _ = (service / "memory.stat").write_text("anon 1048576\ninactive_file 0\n")
        _ = (service / "memory.current").write_text("1048576")
        result = self.pass_for(400, need=22, free=60)
        self.assertEqual((result.decision.state, result.decision.charged), ("admit", 1048576))
        head = self.head(22)
        row = gate.read_waiter(head)
        row["priority_since"] = NOW_EPOCH
        _ = head.write_text(json.dumps(row))
        result = self.pass_for(300, need=22, free=20)
        self.assertEqual(result.decision.state, "admit")






class PressureAdmissionTests(MemoryQueueFixture):
    pressure: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.pressure = self.root / "pressure"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_MEMORY_PRESSURE": str(self.pressure),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "builds.slice"),
            "BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_PROC": str(self.root / "proc"),
        })))

    def assert_admission(self, held: bool) -> None:
        result = self.pass_for(400, 8, 40)
        self.assertEqual(result.decision.state, "hold" if held else "admit")
        self.assertEqual(isinstance(result.admission, gate.ProposedAdmission), not held)
        if held:
            view = gate.queue_read().view
            self.assertEqual(view.waiting[0].reason, result.reason)

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
        policy = gate.read_memory_config().policy
        host = gate.read_meminfo(self.meminfo)
        pressure = gate.read_pressure(host, policy=policy)
        result = gate.decide(host.available, host.total, 8 * GIB, [], pressure=pressure)
        self.assertEqual(result.state, "admit")
        self.assertIsInstance(result.pressure, gate.NoPressureHold)

    def test_unreadable_pressure_does_not_hold_but_memory_still_does(self) -> None:
        self.pressure.mkdir()
        self.assert_admission(False)
        result = self.pass_for(400, 8, 1)
        self.assertEqual(result.decision.state, "hold")
        self.assertIsInstance(result.decision.pressure, gate.NoPressureHold)


    def test_pressure_is_in_checker_summary_even_with_no_running_step(self) -> None:
        _ = self.pressure.write_text("full avg10=6.0 avg60=1.0 total=1\n")
        result = self.pass_for(400, need=8, free=40)
        self.assertEqual(result.decision.state, "hold")
        assert isinstance(result.decision.pressure, gate.MemoryPressure)
        self.assertEqual(result.decision.pressure.full_avg10, 6.0)
        self.assertIn("memory pressure", gate.pressure_text(result.decision.pressure, gate.read_memory_config().policy))








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
                _ = gate.read_pressure(policy=admission_policy_fixture())
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
            _ = gate.read_pressure(policy=admission_policy_fixture())
        rows = [cast(dict[str, object], json.loads(line)) for line in self.record.read_text().splitlines()]
        self.assertEqual([row["reading"] for row in rows], ["/proc/meminfo", "/proc/pressure/memory"])




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
                    self.assertIsInstance(gate.read_pressure(policy=admission_policy_fixture()), gate.Pressure)

    def test_shell_refuses_live_meminfo_or_pressure_and_records_one_reading(self) -> None:
        for reading in ("/proc/meminfo", "/proc/pressure/memory"):
            with self.subTest(reading=reading), AdmissionFixture() as fixture:
                os.environ["BUILDLOG_LIVE_MEMORY_TRIPWIRE"] = str(self.record)
                self.record.unlink(missing_ok=True)
                with self.assertRaises(gate.LiveMemoryReading) as raised:
                    if reading == "/proc/meminfo":
                        _ = gate.read_meminfo(Path(reading))
                    else:
                        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": reading}):
                            _ = gate.read_pressure(policy=admission_policy_fixture())
                reason = str(raised.exception)
                fixture.server.reply_filter = lambda reply: wire.Refused(reason)
                with fixture.serving():
                    result = subprocess.run(
                        ["bash", "-c", 'source "$1"; buildlog_wait_for_memory', "test", str(MEMORY_GATE)],
                        env=dict(os.environ), capture_output=True, text=True,
                        check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                    )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip(), f"memory admission check failed ({self.refusal(reading)}); step refused")
                self.assertNotIn("waiting for memory", result.stderr)
                self.assert_record(reading)


    def test_fake_readings_admit_check_and_queue_without_a_record(self) -> None:
        with patch.dict(os.environ, fake_memory_readings(self.root)):
            result = self.pass_for(400, need=8, free=40)
            self.assertEqual(result.decision.state, "admit")
            self.assertIsInstance(result.admission, gate.ProposedAdmission)
            view = gate.queue_read().view
            self.assertEqual(len(view.waiting), 1)
            reason = view.waiting[0].reason
            self.assertIsInstance(reason, gate.HeadAdmissionStatus)
            if isinstance(reason, gate.HeadAdmissionStatus):
                self.assertEqual(reason.decision.state, "admit")
        self.assertFalse(self.record.exists() and self.record.read_text())

    def test_queue_propagates_live_memory_refusals(self) -> None:
        _ = self.meminfo.write_text(f"MemTotal: {60 * GIB // 1024} kB\nMemAvailable: {60 * GIB // 1024} kB\n")
        for variable, reading in (("BUILDLOG_MEMINFO", "/proc/meminfo"),
                                  ("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory")):
            with self.subTest(variable=variable), patch.dict(os.environ):
                os.environ[variable] = reading
                self.record.unlink(missing_ok=True)
                with self.assertRaises(gate.LiveMemoryReading) as raised:
                    _ = gate.queue_read()
                self.assertEqual(str(raised.exception), self.refusal(reading))
                self.assert_record(reading)

    def test_unwritable_record_keeps_the_named_refusal_in_direct_and_queue_calls(self) -> None:
        _ = self.meminfo.write_text(f"MemTotal: {60 * GIB // 1024} kB\nMemAvailable: {60 * GIB // 1024} kB\n")
        record = self.root / "missing-directory" / "tripwire.jsonl"
        for variable, reading in (("BUILDLOG_MEMINFO", "/proc/meminfo"),
                                  ("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory")):
            with patch.dict(os.environ, {"BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(record), variable: reading}):
                with self.subTest(reading=reading, caller="direct"):
                    with self.assertRaises(gate.LiveMemoryReading) as raised:
                        gate.check_memory_reading(Path(reading))
                    self.assertEqual(str(raised.exception), self.refusal(reading))
                with self.subTest(reading=reading, caller="projection"):
                    with self.assertRaises(gate.LiveMemoryReading) as raised:
                        _ = gate.queue_read()
                    self.assertEqual(str(raised.exception), self.refusal(reading))


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
        row.update(priority_since=priority, label="head nextest")
        _ = path.write_text(json.dumps(row))
        return path


    def test_strict_room_matches_decision_room_and_window_clause(self) -> None:
        for need, expected_room, text in (
                (7, 14, "younger steps may use 14.0 GiB of the budget until 18:10"),
                (21, 0, "younger steps wait until 18:10"),
                (22, -1, "younger steps wait until 18:10")):
            with self.subTest(head_need=need):
                head: gate.Waiter = {"pid": 300, "start_time": "300", "need": need * GIB,
                    "since": 0, "priority_since": NOW.timestamp()}
                younger: gate.Waiter = {"pid": 400, "start_time": "400", "need": GIB, "since": 1}
                window = gate.StrictUntil(NOW.timestamp() + 600)
                decision = gate.admission_walk(60 * GIB, 60 * GIB, [head, younger], [],
                    gate.HeadPriority(head, window), gate.NoPressureHold(),
                    recovery=gate.NoRecoverySpacing(), now=NOW.timestamp(), policy=admission_policy_fixture())[1].decision
                self.assertEqual(gate.strict_room(21 * GIB, need * GIB), expected_room * GIB)
                self.assertIsInstance(decision.head_room, gate.HeadRoom)
                room = cast(gate.HeadRoom, decision.head_room)
                self.assertEqual((room.head_need, room.used, room.room, room.passes),
                                 (need * GIB, 0, expected_room * GIB, need == 7))
                self.assertEqual(decision.state, "admit" if need == 7 else "hold")
                self.assertEqual(gate.window_clause(window, 21 * GIB, need * GIB,
                                                   ZoneInfo("America/New_York")), text)




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
                text = gate.head_wait_text(gate.HeadAdmissionStatus(decision, int(need * GIB), 60 * GIB, window), zone, policy=admission_policy_fixture())
                self.assertTrue(text.endswith(expected), text)

    def test_room_sum_uses_milliseconds_live_needs_and_charges(self) -> None:
        priority = NOW.timestamp() + 0.0009
        path = self.strict_head(priority=priority)
        head = cast(gate.Waiter, json.loads(path.read_text()))
        _ = self.running(100, 4, age=1)
        _ = self.running(200, 2, age=0)
        _ = self.running(400, 3, age=-600)  # A backfill admission still counts in the next strict window.
        self.readings.update({100: gate.MeasuredMemory(4 * GIB), 200: gate.MeasuredMemory(5 * GIB),
                              400: gate.MeasuredMemory(GIB)})
        rows = [(path, row) for path, row in gate.reservation_rows(gate.admission_dir(self.root))
                if self.processes.get(row["pid"]) == row["start_time"]]
        reservations = [row for _, row in rows]
        memories = [gate.reservation_memory(path, row, NOW + timedelta(seconds=720),
                                            gate.process_table(self.root / "proc"), self.root / "proc", policy=admission_policy_fixture())
                    for path, row in rows]
        priority_head = gate.HeadPriority(head, gate.StrictUntil(NOW.timestamp() + 1320))
        self.assertEqual(gate.head_room_used(reservations, memories, priority_head), 8 * GIB)
        del self.processes[200]
        rows = [(path, row) for path, row in gate.reservation_rows(gate.admission_dir(self.root))
                if self.processes.get(row["pid"]) == row["start_time"]]
        reservations = [row for _, row in rows]
        memories = [gate.reservation_memory(path, row, NOW + timedelta(seconds=720),
                                            gate.process_table(self.root / "proc"), self.root / "proc", policy=admission_policy_fixture())
                    for path, row in rows]
        self.assertEqual(gate.head_room_used(reservations, memories, priority_head), 3 * GIB)
        self.assertEqual(gate.head_room_used(reservations, memories, gate.NoHeadPriority()), 0)

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
                _ = self.waiter(400, NOW.timestamp() - 60, "/tmp/younger", "younger", need)
                result = self.pass_for(pid=400, need=need, free=free)
                self.assertEqual(result.decision.state, "hold")
                self.assertIsInstance(result.reason, gate.BehindOlderFit)
                self.assertEqual(gate.queue_view().waiting[-1].reason, result.reason)
                self.assertEqual(gate.explain_queue().line.steps[-1].waiter.reason, result.reason)
                self.assertIsInstance(gate.blocker_identity(result.blocker,
            [row for _, row in gate.reservation_rows(gate.admission_dir(self.root))],
            gate.process_table(self.root / "proc"), self.root / "proc"), gate.NoSingleBlocker)
                self.assertEqual(self.pass_for(pid=300, need=8, free=free).decision.state, "admit")

    def test_resource_holds_do_not_claim_the_heads_room_is_the_cause(self) -> None:
        _ = self.running(100, 20, age=30)
        self.readings[100] = gate.MeasuredMemory(20 * GIB)
        _ = self.strict_head()
        for free, reason in ((60, gate.NeedsBuildBudget), (3, gate.NeedsBuildBudget)):
            result = self.pass_for(pid=400, need=2, free=free)
            self.assertIsInstance(result.reason, reason)
            self.assertIsInstance(result.decision.order_hold, gate.NoOrderHold)
        _ = self.running(100, 1, age=30)
        self.readings[100] = gate.MeasuredMemory(GIB)
        result = self.pass_for(pid=400, need=2, free=3)
        self.assertIsInstance(result.reason, gate.NeedsMemory)
        self.assertIsInstance(result.decision.order_hold, gate.NoOrderHold)

    def test_room_is_taken_in_queue_order_before_a_younger_polls(self) -> None:
        _ = self.running(100, 11.1, age=30)
        self.readings[100] = gate.MeasuredMemory(int(9.9 * GIB))
        _ = self.strict_head()
        _ = self.waiter(400, NOW.timestamp() - 120, "/tmp/older", "older", 5)
        self.processes[500] = "young"
        _ = self.waiter(500, NOW.timestamp() - 60, "/tmp/young", "young", 5)
        result = self.pass_for(pid=500, need=5, free=60)
        self.assertEqual(result.reason, gate.BehindOlderFit("older", NOW.timestamp() - 120, "session-400"))
        self.assertEqual(result.decision.head_room,
            gate.HeadRoom(int(11.5 * GIB), 5 * GIB, int(9.5 * GIB), False))
        self.assertEqual(gate.queue_view().waiting[-1].reason, result.reason)
        self.assertEqual(gate.explain_queue().line.steps[-1].waiter.reason, result.reason)
        self.assertIsInstance(gate.blocker_identity(result.blocker,
            [row for _, row in gate.reservation_rows(gate.admission_dir(self.root))],
            gate.process_table(self.root / "proc"), self.root / "proc"), gate.NoSingleBlocker)
        self.assertEqual(queue_view.reason_phrase(result.reason, ()), "behind an older step that fits")
        self.assertEqual(self.pass_for(pid=400, need=5, free=60).decision.state, "admit")










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
            row.update(label="head nextest",
                       priority_since=NOW.timestamp() - (600 if window == "backfill" else 0))
            _ = path.write_text(json.dumps(row))
        return (self.waiter(400, NOW.timestamp() - 120, "/tmp/berth", "berth", older_need),
                self.waiter(500, NOW.timestamp() - 60, "/tmp/handler", "handler", younger_need))

    def test_younger_poll_keeps_room_for_older_fitting_step_in_each_window(self) -> None:
        for window in ("none", "backfill"):
            with self.subTest(window=window):
                _ = self.scenario(window, 2.9, 3.0)
                result = self.pass_for(500, need=3, free=24)
                self.assertEqual(result.decision.state, "hold")
                self.assertEqual(result.reason, gate.BehindOlderFit("berth", NOW.timestamp() - 120, "session-400"))
                self.assertEqual(self.pass_for(400, need=2.9, free=24).decision.state, "admit")

    def test_older_smaller_step_gets_room_before_younger_in_each_window(self) -> None:
        for window in ("none", "backfill"):
            with self.subTest(window=window):
                _ = self.scenario(window, 1, 4)
                self.assertEqual(self.pass_for(500, need=4, free=24).decision.state, "hold")
                self.assertEqual(self.pass_for(400, need=1, free=24).decision.state, "admit")

    def test_younger_starts_when_older_does_not_fit_in_each_window(self) -> None:
        for window in ("none", "backfill"):
            with self.subTest(window=window):
                _ = self.scenario(window, 5, 3)
                self.assertEqual(self.pass_for(500, need=3, free=24).decision.state, "admit")

    def test_strict_window_keeps_behind_head_reason(self) -> None:
        _ = self.scenario("strict", 2.9, 3)
        view = gate.queue_view()
        self.assertIsInstance(view.waiting[-1].reason, gate.BehindHead)
        self.assertEqual(self.pass_for(500, need=3, free=24).decision.state, "hold")


    def test_one_snapshot_agrees_in_check_queue_explain_blocker_and_shell(self) -> None:
        _ = self.scenario("none", 2.9, 3)
        result = self.pass_for(500, need=3, free=24)
        self.assertEqual(result.decision.state, "hold")
        self.assertIsInstance(result.reason, gate.BehindOlderFit)
        read = gate.queue_read()
        self.assertEqual(read.view.waiting[-1].reason, result.reason)
        self.assertEqual(read.explanation.line.steps[-1].waiter.reason, result.reason)
        self.assertIsInstance(gate.blocker_identity(result.blocker,
            [row for _, row in gate.reservation_rows(gate.admission_dir(self.root))],
            gate.process_table(self.root / "proc"), self.root / "proc"), gate.NoSingleBlocker)
        self.assertEqual(queue_view.reason_phrase(result.reason, read.view.waiting), "behind #1, an older step that fits")

    def test_open_recovery_slot_reserves_first_fitting_waiter_in_each_window(self) -> None:
        for window in ("none", "backfill", "strict"):
            with self.subTest(window=window):
                need = 1 if window == "strict" else 3
                _ = self.scenario(window, need, need)
                directory = gate.admission_dir(self.root)
                if window == "none":
                    (directory / "100.reservation").unlink()
                elif window == "backfill":
                    self.readings[100] = gate.MeasuredMemory(GIB)
                else:
                    head_path = directory / "300-head.waiting"
                    head = gate.read_waiter(head_path)
                    head["need"] = 6 * GIB
                    _ = head_path.write_text(json.dumps(head))
                inputs = fixture_queue_inputs(directory, gate.process_table(self.root / "proc"),
                    NOW_EPOCH, admission_policy_fixture())
                inputs = replace(inputs, recovery=gate.RecoveryStarts(NOW_EPOCH + 120, NOW_EPOCH))
                result = gate.admission_pass(inputs, NOW_EPOCH, gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), admission_policy_fixture())
                older = next(row for row in result.rows if row.owner == "400-warmup")
                younger = next(row for row in result.rows if row.owner == "500-younger")
                self.assertEqual(older.decision.state, "admit")
                self.assertEqual(younger.decision.state, "hold")
                assert isinstance(older.admission, gate.ProposedAdmission)
                self.assertEqual(older.admission.recovery,
                    gate.RecoveryStarts(NOW_EPOCH + 120, NOW_EPOCH + admission_policy_fixture().timing.recovery_start_seconds))












class QueueApiTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_PROC": str(self.root / "proc"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        _ = os.environ.pop(gate.BUILDLOG_UNIT, None)
        self.processes[500] = "third"
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.meminfo.write_text(f"MemAvailable: {int(23.1 * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def queue(self) -> tuple[Path, Path, Path]:
        return (self.waiter(300, NOW.timestamp() - 2400, "/tmp/clone88ci", "clone88ci"),
                self.waiter(400, NOW.timestamp() - 300, "/tmp/warmup-tree", "warmup"),
                self.waiter(500, NOW.timestamp() - 60, "/tmp/third", "third"))



    def test_waiter_identity_reads_the_unit_override_without_changing_the_session(self) -> None:
        with patch.dict(os.environ, {gate.BUILDLOG_UNIT: "queue-unit", "CODEX_THREAD_ID": "showrunner"}):
            identity = gate.waiter_identity("merge-gate", "tree")
            self.assertEqual(identity.get("unit"), "queue-unit")
            self.assertEqual(identity["session_id"], "showrunner")
            self.assertEqual(gate.waiter_unit_name(identity, 123), "queue-unit")

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
                             "priority_since": NOW.timestamp()}
        younger: gate.Waiter = {"pid": 400, "start_time": "warmup", "need": 3 * GIB, "since": 1}
        decision = gate.admission_walk(8 * GIB, 60 * GIB, [head, younger],
            [gate.PromisedMemory(4 * GIB)], gate.HeadPriority(head, gate.StrictUntil(NOW.timestamp() + 600)),
            gate.NoPressureHold(), recovery=gate.NoRecoverySpacing(), now=NOW.timestamp(), policy=admission_policy_fixture())[1].decision
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
                    head_name=expected_name, joined_at=NOW.timestamp() - 2400, head_session_id="session-300"))

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
            self.assertEqual(gate.head_wait_text(reason, ZoneInfo("America/New_York"), policy=admission_policy_fixture()),
                             "head: needs 10.9 GiB; builds use 23.5 of the 20.0 GiB budget "
                             + "(starts when build use falls to 9.1 GiB); needs 34.4 GiB free; 23.1 free; younger steps may use 9.1 GiB of the budget until 18:10")
        self.assertCountEqual([row.measured for row in view.running],
                              [gate.MeasuredMemory(GIB, gate.RampPromise(12 * GIB)), gate.MeasuredMemory(2 * GIB, gate.RampPromise(int(11.5 * GIB)))])
















    def test_missing_meminfo_is_a_reading_unavailable_reason(self) -> None:
        _ = self.queue()
        self.meminfo.unlink()
        self.assertTrue(all(isinstance(row.reason, gate.ReadingUnavailable) for row in gate.queue_view().waiting))

    def test_psi_11_holds_backfill_without_making_the_young_head_strict(self) -> None:
        _ = self.waiter(300, NOW.timestamp() - 540, "/tmp/head", "head")
        pressure = self.root / "pressure"
        _ = pressure.write_text("full avg10=11.0 avg60=1.0 total=1\n")
        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": str(pressure)}):
            result = self.pass_for(400, need=1, free=40)
            self.assertEqual(result.decision.state, "hold")
            self.assertEqual(result.decision.pressure, gate.MemoryPressure(11, condition="full"))
            self.assertIsInstance(gate.queue_view().waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertIsInstance(result.decision.order_hold, gate.NoOrderHold)

    def test_head_names_only_free_memory_when_budget_fits(self) -> None:
        _ = self.running(100, 1)
        _ = self.head(15.1)
        result = self.pass_for(pid=300, need=15.1, free=18)
        self.assertEqual(result.decision.state, "hold")
        self.assertLess(result.decision.charged + int(15.1 * GIB), result.decision.budget)
        self.assertEqual(gate.queue_view().waiting[0].reason, result.reason)
        self.assertIsInstance(result.reason, gate.HeadAdmissionStatus)
        if isinstance(result.reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(result.reason, ZoneInfo("America/New_York"), policy=admission_policy_fixture()), "head: needs 18.1 GiB free; 18.0 free; younger steps may use 4.9 GiB of the budget until 18:10")

    def test_removed_head_row_releases_younger_steps_to_normal_limits(self) -> None:
        path = self.head(19.3)
        self.assertEqual(self.pass_for(400, need=1, free=40).decision.state, "hold")
        path.unlink()
        younger = self.pass_for(400, need=1, free=21.9)
        self.assertEqual(younger.decision.state, "admit")
        self.assertIsInstance(younger.decision.order_hold, gate.NoOrderHold)

    def test_psi_11_holds_a_drained_head_without_releasing_younger_steps(self) -> None:
        _ = self.head(19.3)
        self.assertEqual(self.pass_for(400, need=1, free=40).decision.state, "hold")
        pressure = self.root / "pressure"
        _ = pressure.write_text("full avg10=11.0 avg60=1.0 total=1\n")
        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": str(pressure)}):
            head = self.pass_for(pid=300, need=19.3, free=21.9)
            self.assertEqual(head.decision.state, "hold")
            view = gate.queue_view()
        self.assertEqual(view.waiting[0].reason, head.reason)
        self.assertIsInstance(head.reason, gate.HeadAdmissionStatus)
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
                result = self.pass_for(400, need=2, free=15)
                view = gate.queue_view()
                reason = next(step.reason for step in view.waiting if step.pid == 400)
                if held:
                    self.assertEqual(result.decision.state, "hold")
                    self.assertEqual(reason, gate.BehindHead("clone88ci", NOW.timestamp() - 2400, ""))
                else:
                    self.assertEqual(result.decision.state, "admit")
                    self.assertIsInstance(reason, gate.ReadyForAdmission)


    def test_head_hold_survives_rising_free_memory_and_falling_measured_use(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        row = gate.read_waiter(path)
        row["priority_since"] = NOW_EPOCH
        _ = path.write_text(json.dumps(row))
        first = self.pass_for(400, need=1, free=20)
        self.assertEqual(first.decision.state, "hold")
        self.readings[100] = gate.MeasuredMemory(0)
        second = self.pass_for(400, need=1, free=21.9)
        self.assertEqual(second.decision.state, "hold")
        view = gate.queue_view()
        reason = view.waiting[0].reason
        assert isinstance(reason, gate.HeadAdmissionStatus)
        self.assertEqual(gate.head_wait_text(reason, ZoneInfo("America/New_York"),
            policy=admission_policy_fixture()), "head: needs 22.3 GiB free; 21.9 free; younger steps may use 0.7 GiB of the budget until 18:10")
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)

    def test_mature_builds_hold_head_on_13_point_2_used_not_72_point_3_promised(self) -> None:
        for pid in range(500, 510):
            self.processes[pid] = str(pid)
            self.readings[pid] = gate.MeasuredMemory(int(1.32 * GIB))
            _ = self.running(pid, 7.23)
        _ = self.head(15.1)
        result = self.pass_for(300, need=15.1, free=19.5)
        self.assertEqual(result.decision.state, "hold")
        inputs = fixture_queue_inputs(gate.admission_dir(self.root), gate.process_table(self.root / "proc"),
            NOW_EPOCH, admission_policy_fixture())
        self.assertAlmostEqual(sum(row["need"] for row in inputs.reservations) / GIB, 72.3)
        self.assertAlmostEqual(result.decision.charged / GIB, 13.2)
        assert isinstance(inputs.host, gate.HostMemory)
        self.assertGreater(inputs.host.available, result.decision.threshold)
        self.assertEqual(gate.queue_view().waiting[0].reason, result.reason)
        assert isinstance(result.reason, gate.HeadAdmissionStatus)
        text = gate.head_wait_text(result.reason, ZoneInfo("America/New_York"), policy=admission_policy_fixture())
        self.assertEqual(text, "head: needs 15.1 GiB; builds use 13.2 of the 20.0 GiB budget "
            + "(starts when build use falls to 4.9 GiB); younger steps may use 4.9 GiB of the budget until 18:10")

    def test_backfill_at_nine_minutes_becomes_sticky_at_ten_minutes(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        path = self.head(19.3)
        row = gate.read_waiter(path)
        row["priority_since"] = NOW_EPOCH - 600
        _ = path.write_text(json.dumps(row))
        first = self.pass_for(400, need=1, free=60)
        self.assertEqual(first.decision.state, "admit")
        head = gate.queue_view().waiting[0].reason
        assert isinstance(head, gate.HeadAdmissionStatus)
        self.assertIsInstance(head.backfill, gate.BackfillUntil)
        second = self.pass_for(400, need=1, free=60, seconds=120)
        self.assertEqual(second.decision.state, "hold")
        self.assertIsInstance(second.reason, gate.BehindHead)

    def test_strict_backfill_strict_windows_keep_queue_and_admission_in_agreement(self) -> None:
        from scripts.build_queue.queue_fixture import fixture_queue_read
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        self.readings[400] = gate.MeasuredMemory(0)
        _ = self.meminfo.write_text(f'MemAvailable: {20 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n')
        head = gate.read_waiter(path)
        head["priority_since"] = NOW_EPOCH
        _ = path.write_text(json.dumps(head))
        for elapsed, state, deadline, window in ((0, "hold", "18:10", "may use 0.7 GiB of the budget"),
                                                (600, "admit", "18:12", "may start"),
                                                (720, "hold", "18:22", "may use 0.7 GiB of the budget")):
            result = self.pass_for(400, need=1, free=20, seconds=elapsed)
            self.assertEqual(result.decision.state, state)
            with patch.object(gate, "queue_read", side_effect=lambda:
                    fixture_queue_read(gate.admission_dir(self.root),
                        gate.process_table(self.root / "proc"), NOW_EPOCH + elapsed)):
                view = gate.queue_view()
            reason = view.waiting[0].reason
            assert isinstance(reason, gate.HeadAdmissionStatus)
            text = gate.head_wait_text(reason, ZoneInfo("America/New_York"), policy=admission_policy_fixture())
            self.assertTrue(text.endswith(f"younger steps {window} until {deadline}"), text)
            if state == "hold":
                self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)
            else:
                self.assertIsInstance(result.admission, gate.ProposedAdmission)
            self.assertEqual(gate.read_waiter(path).get("priority_since"), NOW_EPOCH)

    def test_drained_head_starts_below_memory_floor_and_a_younger_check_cannot_take_its_place(self) -> None:
        running = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        row = gate.read_waiter(path)
        row["priority_since"] = NOW_EPOCH
        _ = path.write_text(json.dumps(row))
        self.assertEqual(self.pass_for(400, need=10.8, free=20).decision.state, 'hold')
        running.unlink()
        younger = self.pass_for(400, need=10.8, free=21.9)
        self.assertEqual(younger.decision.state, 'hold')
        view = gate.queue_view()
        self.assertIsInstance(view.waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, view.waiting[0].reason).decision.state, 'admit')
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)
        head = self.pass_for(pid=300, need=19.3, free=21.9)
        self.assertLess(int(21.9 * GIB), head.decision.threshold)
        self.assertIsInstance(head.admission, gate.ProposedAdmission)



class ProcessMemoryTests(unittest.TestCase):

    def test_tree_counts_resident_bytes_and_records_swap_separately(self) -> None:
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
                             gate.MeasuredMemory(60 * 1024, swap_bytes=9 * 1024))
            self.assertEqual(gate.tree_memory(100, "reused", processes, proc), gate.GoneMemory())
            self.assertEqual(gate.tree_memory(999, "gone", processes, proc), gate.GoneMemory())

    def test_swap_release_does_not_make_a_reservation_the_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / 'proc'
            for pid, start in ((100, 'holder'), (400, 'waiter')):
                process = proc / str(pid)
                process.mkdir(parents=True)
                fields = ['S', '1', *['0'] * 17, start]
                _ = (process / 'stat').write_text(f'{pid} (test process) ' + ' '.join(fields))
            _ = (proc / '100/status').write_text(f'VmRSS: {GIB // 1024} kB\nVmSwap: {15 * GIB // 1024} kB\n')
            _ = (proc / '100/environ').write_bytes(b'CLAUDE_CODE_SESSION_ID=owner-id\x00CLAUDE_SESSION_NAME=owner-name\x00')
            directory = gate.admission_dir(root)
            reservation: dict[str, object] = {'pid': 100, 'start_time': 'holder', 'need': 16 * GIB, 'repo': 'test', 'step': 'check', 'worktree': 'test', 'sidecar': '', 'admitted_at': store.utc_iso(NOW.timestamp() - admission_policy_fixture().timing.ramp_seconds), 'test_threads': None, 'call_id': None}
            _ = (directory / '100.reservation').write_text(json.dumps(reservation))
            waiter = directory / '400-waiter.waiting'
            _ = waiter.write_text(json.dumps({'pid': 400, 'start_time': 'waiter', 'need': 12 * GIB, 'since': NOW.timestamp()}))
            environment = fake_memory_readings(root, available_kib=2 * GIB // 1024, total_kib=60 * GIB // 1024)
            config = root / 'build_admission.conf'
            write_memory_config(config)
            environment.update({'BUILDLOG_DIR': str(root), 'BUILDLOG_PROC': str(proc), 'LINT_CONFIG_FILE': str(config)})
            with patch.dict(os.environ, environment), patch.object(time, 'time', return_value=NOW.timestamp()):
                inputs = fixture_queue_inputs(directory, gate.process_table(proc), NOW_EPOCH, admission_policy_fixture())
                inputs = replace(inputs, blockers_for=frozenset({"400-waiter"}))
                result = gate.admission_pass(inputs, NOW_EPOCH, gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), admission_policy_fixture())
                self.assertIsInstance(result.rows[0].blocker, gate.NoSingleBlocker)




class ShellTests(unittest.TestCase):
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))

    @override
    def setUp(self) -> None:
        self.admission = self.enterContext(AdmissionFixture())
        self.admission.clock.epoch = time.time()
        _ = self.enterContext(self.admission.serving())

    def test_sampler_maximum_reaches_history(self) -> None:
        with self.admission.loop_turn():
            request = self.admission.request()
            sidecar = self.admission.scope(request.owner)
            scope = Path(sidecar.read_text())
            _ = (scope / "memory.stat").write_text(f"anon {GIB}\ninactive_file 0\n")
            request = replace(request, sidecar=str(sidecar))
            grant = only_admitted(self.admission.server.handle_request(request))
        result = sample_anon_once(sidecar, Path(grant.max_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(grant.max_path).read_text().strip(), str(GIB))
        self.assertEqual(client.release(grant.reservation, 0, grant.cleanup_timing), wire.Ok())
        row, = self.admission.history()
        self.assertEqual(row["anon_peak_bytes"], GIB)

    def shell(self, body: str, *, launch: bool = False, **environment: str) -> subprocess.CompletedProcess[str]:
        command = 'set -e; source "$1"; shift; ' + body
        return subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE),
                               *([] if launch else ["/bin/true"])],
            env={**os.environ, "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                 "BUILD_HOLD_DIR": str(self.admission.root / "holders"),
                 "BUILD_HOLD_RELEASE_DIR": str(self.admission.root / "releases"),
                 "UNIT_ACTIVE_DIR": str(self.admission.root / "active"), "UNIT_RUN_DIR": "",
                 "LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": "", **environment},
            capture_output=True, text=True, check=False, timeout=30)

    def formatted(self, peak: gate.ExpectedPeak, *, available: int = 0, reserve: int = 0,
                  measured: int = 0, running: tuple[gate.RunningStep, ...] = (),
                  reason: gate.WaitReason = READY_FOR_ADMISSION, head_need: int = 0,
                  head_label: str = "", periodic: bool = False) -> str:
        decision = gate.AdmissionDecision("hold", peak.bytes + reserve, 0, reserve, measured,
            pressure=reason.decision.pressure if isinstance(reason, gate.HeadAdmissionStatus) else
                reason if isinstance(reason, gate.MemoryPressure) else gate.NO_PRESSURE_HOLD,
            budget=21 * GIB)
        snapshot = gate.AdmissionWaitSnapshot(decision, peak, running, available, "hana", "build",
            admission_policy_fixture(), (), head_need, head_label, reason)
        output = client.WaitOutput()
        with patch.object(client, "CLOCK", self.admission.clock), contextlib.redirect_stderr(io.StringIO()) as text:
            output(wire.Wait(reason, 1, head_label, "", snapshot))
            if periodic:
                self.admission.clock.advance(gate.read_memory_config().wait_reply_seconds)
                output(wire.Wait(reason, 1, head_label, "", snapshot))
        return text.getvalue().splitlines()[-1] + "\n"

    def test_mac_claim_admission_never_opens_a_socket_or_reads_proc(self) -> None:
        before = self.admission.readings.counts.copy()
        result = self.shell('unset BUILDLOG_MEMINFO; OSTYPE=darwin; '
            + 'buildlog_wait_for_memory "$@"; printf "%s|%s\\n" "$?" "$BUILDLOG_MEM_OUTCOME"',
            BUILDLOG_MEM_ADMIT_SCRIPT="/nonexistent/admission_client.py", BUILDLOG_PROC="/nonexistent/proc")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0|Granted\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.admission.readings.counts, before)
        self.assertEqual(self.admission.server.reservations, {})

    def test_linux_target_admission_never_uses_the_local_service(self) -> None:
        before = self.admission.readings.counts.copy()
        result = self.shell('buildlog_memory_python() { echo unexpected-client >&2; return 93; }; '
            + 'buildlog_wait_for_memory "$@"; printf "%s|%s|%s\\n" '
            + '"$?" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"',
            BUILDLOG_CALLER="verify-linux", BUILDLOG_CALL_ID="remote-step", OSTYPE="linux-gnu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0|Granted|\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.admission.readings.counts, before)
        self.assertEqual(self.admission.server.reservations, {})

    def test_linux_target_ignores_natedev_hold_checks(self) -> None:
        holder = self.admission.root / "holders/holder.json"
        holder.parent.mkdir(parents=True)
        _ = holder.write_text('{"holder":"natedev-owner"}')
        result = self.shell('build_hold_wait_local; build_hold_check_local; '
            + 'if build_hold_applies; then exit 94; fi; printf "target-admitted\\n"',
            BUILDLOG_CALLER="verify-linux", BUILDLOG_CALL_ID="remote-step",
            BUILD_TARGETS_SCRIPT="/nonexistent/build_targets.py", OSTYPE="linux-gnu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "target-admitted\n")
        self.assertEqual(result.stderr, "")

    def test_shape_without_earlier_builds_names_the_fallback(self) -> None:
        text = self.formatted(gate.ExpectedPeak(17_000_000_000, "shape", 0, gate.ShapeFallback(gate.UnderBudget())))
        self.assertIn("fallback", text)
        self.assertNotIn("0 earlier builds", text)

    def test_first_wait_line_names_a_same_shape_promise_without_claiming_p90(self) -> None:
        text = self.formatted(gate.ExpectedPeak(17_000_000_000, "shape", 2, gate.ShapeLargest(2, gate.NoFloor(), gate.NoHeadroom(), gate.UnderBudget())))
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("same-shape promise", text)
        self.assertNotIn("percentile", text)
        self.assertNotIn("p90", text)
        self.assertNotIn("fallback", text)

    def test_shell_accepts_shape_source(self) -> None:
        def source(reply: wire.Reply) -> wire.Reply:
            return replace(reply, source="shape") if isinstance(reply, wire.LaunchAdmitted) else reply
        self.admission.server.reply_filter = source
        result = self.shell('buildlog_wait_for_memory', launch=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wait_line_carries_head_need_step_and_measured_memory(self) -> None:
        running = (gate.RunningStep("one", "first", "nextest", "/tmp/one", 0, gate.MeasuredMemory(GIB)),
                   gate.RunningStep("two", "second", "clippy", "/tmp/two", 0, gate.MeasuredMemory(2 * GIB)))
        for periodic in (False, True):
            text = self.formatted(gate.ExpectedPeak(11_600_000_000, "measured", 5),
                available=49_000_000_000, reserve=3_000_000_000, measured=3 * GIB, running=running,
                head_need=11_703_785_881, head_label="clone88ci nextest (verify pid 300)", periodic=periodic)
            self.assertEqual(len(text.splitlines()), 1)
            self.assertIn("head clone88ci nextest (verify pid 300) needs 10.9 GiB", text)
            self.assertIn("0.0 GiB promised, 3.0 GiB measured across 2 running steps: one nextest in /tmp/one, two clippy in /tmp/two; ", text)
            self.assertTrue(text.endswith("2.8 GiB kept for earlyoom)\n"))

    def test_wait_line_shows_reason_in_place_of_zero_running_steps(self) -> None:
        pressure = gate.MemoryPressure(11, condition="full")
        decision = gate.AdmissionDecision("hold", 19_434_727_014, 0, 3 * GIB, 0, budget=21 * GIB)
        pressure_reason = pressure
        head_reason = gate.HeadAdmissionStatus(decision, 16_213_501_542,
            18 * GIB, gate.BackfillUntil(18 * 3600 + 10 * 60))
        for periodic in (False, True):
            for reason, expected in ((pressure_reason, "memory pressure: full avg10 11.0"),
                                     (head_reason, "needs 18.1 GiB free; 18.0 free; younger steps may start until 18:10")):
                with self.subTest(periodic=periodic, reason=reason):
                    text = self.formatted(gate.ExpectedPeak(16_213_501_542, "measured", 5),
                        reserve=3 * GIB, reason=reason, periodic=periodic)
                    self.assertEqual(len(text.splitlines()), 1)
                    self.assertIn(expected, text)
                    self.assertTrue(text.endswith("; 3.0 GiB kept for earlyoom)\n"))
                    self.assertNotIn("0 running steps", text)
                    self.assertNotIn("GiB promised", text)
                    self.assertNotIn("GiB measured", text)

    def test_wait_line_keeps_zero_running_text_when_reason_is_empty(self) -> None:
        for periodic in (False, True):
            text = self.formatted(gate.ExpectedPeak(0, "fallback", 0), periodic=periodic)
            self.assertEqual(len(text.splitlines()), 1)
            self.assertTrue(text.endswith("; 0.0 GiB promised, 0.0 GiB measured across 0 running steps)\n"))

    def test_invoke_twice_with_temporary_home(self) -> None:
        config = self.admission.root / "config.sh"
        _ = config.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
        for _ in range(2):
            result = self.shell('source "$TEST_INVOKE"; sweep_after_step() { :; }; run_once true',
                TEST_INVOKE=str(INVOKE), BUILDLOG_OFF="1", BUILDLOG_SCOPE="0", LINT_CONFIG_READER=str(config))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_gate_with_argv_and_temporary_home_reserves(self) -> None:
        captured: list[str] = []
        def capture(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Admitted):
                record, = self.admission.server.reservations.values()
                self.assertEqual(record.reservation_id, reply.reservation)
                captured.append(reply.reservation)
            return reply
        self.admission.server.reply_filter = capture
        result = self.shell('buildlog_wait_for_memory "$@"; printf "%s|%s\\n" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"; buildlog_release_memory 0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"Granted|{captured[0]}\n")
        self.assertEqual(self.admission.server.reservations, {})
        self.assertFalse(list(self.admission.state_dir.glob("*.reservation")))

    def test_launch_wait_limit_refuses_without_a_reservation(self) -> None:
        self.admission.free(0)
        result = self.shell('buildlog_wait_for_memory; printf "step started\\n"', launch=True, BUILDLOG_MEM_WAIT_LIMIT_S="1")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.count("waiting for memory since "), 1)
        self.assertIn("GiB needed", result.stderr)
        self.assertIn("memory admission check failed (launch wait limit reached without admission); step refused", result.stderr)
        self.assertEqual(self.admission.server.reservations, {})
        self.assertEqual(self.admission.server.waiters, {})

    def test_gate_wait_lines_in_order_without_repeated_prefix(self) -> None:
        peak = gate.ExpectedPeak(8 * GIB, "fallback", 0)
        snapshot = gate.AdmissionWaitSnapshot(gate.decide(0, 60 * GIB, peak.bytes, [],
            limits=gate.AdmissionLimits(21 * GIB, 3 * GIB)), peak, (), 0, "repo", "nextest",
            admission_policy_fixture(), ())
        reply = wire.Wait(gate.NeedsMemory(), 1, "", "", snapshot)
        with patch.object(client, "CLOCK", self.admission.clock), contextlib.redirect_stderr(io.StringIO()) as output:
            notice = client.WaitOutput()
            notice(reply)
            self.admission.clock.advance(gate.read_memory_config().wait_reply_seconds + 1)
            notice(reply)
            notice.finished()
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3, lines)
        self.assertTrue(lines[0].startswith("waiting for memory since "))
        self.assertTrue(lines[1].startswith("still waiting for memory (1 min): "))
        self.assertRegex(lines[2], r"^memory free after 1 min \d+ s: starting$")
        self.assertEqual(output.getvalue().count("waiting for memory since "), 1)

    def test_build_never_starts_at_the_limit_and_waits_first_in_line(self) -> None:
        self.admission.free(0)
        waits: list[wire.Wait] = []
        def allow_after_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Wait):
                waits.append(reply)
                self.assertEqual(len(self.admission.server.waiters), 1)
                if len(waits) == 2:
                    self.admission.free(60 * GIB)
                else:
                    self.admission.clock.advance(gate.read_memory_config().wait_reply_seconds)
            return reply
        self.admission.server.reply_filter = allow_after_wait
        result = self.shell('buildlog_wait_for_memory "$@"; printf "%s\\n" "$BUILDLOG_MEM_OUTCOME"; buildlog_release_memory 0',
            BUILDLOG_MEM_WAIT_LIMIT_S="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Granted\n")
        self.assertEqual(len(waits), 2)
        self.assertEqual(result.stderr.count("this step is now first in line"), 1, result.stderr)
        self.assertNotIn("starting anyway", result.stderr)
        self.assertEqual(self.admission.server.waiters, {})
        self.assertEqual(self.admission.server.reservations, {})

    def test_fitting_at_limit_is_granted(self) -> None:
        self.admission.free(0)
        def fit_after_hold(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Wait):
                self.admission.free(60 * GIB)
            return reply
        self.admission.server.reply_filter = fit_after_hold
        result = self.shell('buildlog_wait_for_memory "$@"; printf "%s\\n" "$BUILDLOG_MEM_OUTCOME"; buildlog_release_memory 0',
            BUILDLOG_MEM_WAIT_LIMIT_S="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Granted\n")
        self.assertNotIn("memory wait limit reached", result.stderr)
        self.assertIn("memory free after", result.stderr)

    def test_python_failure_refuses_the_step_and_names_the_checker_error(self) -> None:
        broken = self.admission.root / "broken.py"
        _ = broken.write_text('raise RuntimeError("broken lookup")\n')
        result = self.shell('BUILDLOG_MEM_ADMIT_SCRIPT=$TEST_BROKEN; buildlog_wait_for_memory "$@"; printf "step started\\n"',
            TEST_BROKEN=str(broken))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("RuntimeError: broken lookup", result.stderr)
        self.assertIn("memory admission check failed (admission client failed (exit 1)); step refused", result.stderr)
        self.assertEqual(self.admission.server.reservations, {})

    def test_missing_memory_reading_refuses_the_step_and_names_meminfo(self) -> None:
        path = self.admission.root / "meminfo"
        for contents in ("", "MemTotal: 67108864 kB\n"):
            with self.subTest(contents=contents):
                _ = path.write_text(contents)
                result = self.shell('buildlog_wait_for_memory "$@"; printf "step started\\n"')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(f"memory admission check failed (cannot read MemAvailable from {path}); step refused", result.stderr)



def queue_leave_client_main(argv: list[str]) -> int:
    """Advance the client clock at its first held answer."""
    class HeldWaitClock:
        def __init__(self) -> None:
            self.offset: float = 0.0

        def now(self) -> float:
            return time.time() + self.offset

        def monotonic(self) -> float:
            return time.monotonic() + self.offset

        def sleep(self, seconds: float) -> None:
            time.sleep(seconds)

    clock = HeldWaitClock()
    report = client.WaitOutput.__call__
    advanced = False

    def held(output: client.WaitOutput, reply: wire.Wait) -> None:
        nonlocal advanced
        report(output, reply)
        if not advanced:
            clock.offset += gate.read_memory_config().running_pass_seconds
            advanced = True

    with patch.object(client, "CLOCK", clock), patch.object(client.WaitOutput, "__call__", held):
        return client.main(argv)


class QueueLeaveTests(unittest.TestCase):
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
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
        _ = (scripts / "buildlog/step_context.py").write_text((scripts / "buildlog/record.py").read_text())
        config = self.root / "config.sh"
        _ = config.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
        self.environment = {**os.environ, **gate_isolation_environment(self.root),
                            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"), "HOME": str(self.root),
                            "BUILDLOG_DIR": str(self.root / "log"), "BUILDLOG_MEMINFO": str(self.meminfo),
                            "BUILDLOG_SCOPE": "0", "BUILDLOG_SYNC": "1", "LINT_SKIP_SWEEP": "1",
                            "LINT_CONFIG_READER": str(config)}
        _ = self.environment.pop("LINT_QUEUE_LEAVE_HOOK", None)
        from scripts.build_queue.admission_test_support import caller_admission
        self.admission = caller_admission(self, self.root, self.environment)
        self.admission.configure(wait_reply_seconds=0.1, running_pass_seconds=5)
        capture = self.root / "capture-width.py"
        _ = capture.write_text("import json\nfrom scripts.build_queue import admission_client as client\n"
            + "from scripts.lint.memory_admit import RequestedTestWidth\n"
            + "row, = client.view().explanation.reservations\n"
            + "width = row['test_threads']\nassert isinstance(width, RequestedTestWidth)\n"
            + "print(json.dumps({'test_threads': width.threads}))\n")
        self.environment["TEST_CAPTURE_WIDTH"] = str(capture)
        self.environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])


    def run_step(self, hook: str | None, sleep: str, step: str = 'run_once touch "$2"') -> subprocess.CompletedProcess[str]:
        self.admission.free(0)
        environment = dict(self.environment)
        if hook is not None:
            environment["LINT_QUEUE_LEAVE_HOOK"] = f'echo poll >> "{self.calls}"; {hook}'
        waits = 0
        def advance_memory(reply: wire.Reply) -> wire.Reply:
            nonlocal waits
            if isinstance(reply, wire.Wait):
                waits += 1
                result = subprocess.run(["bash", "-c", f'sleeps={waits}; ' + sleep],
                    env=environment, capture_output=True, text=True, check=False, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
            return reply
        if sleep.startswith('printf "MemAvailable:'):
            self.admission.free(60 * GIB)
        self.admission.server.reply_filter = advance_memory
        command = ('source "$1"; buildlog_memory_python() { '
                   + 'if [[ "$1" == "$BUILDLOG_MEM_ADMIT_SCRIPT" ]]; then shift; '
                   + 'python3 -c \'import sys; from scripts.lint.test_memory_admit import queue_leave_client_main; '
                   + 'sys.exit(queue_leave_client_main(sys.argv[1:]))\' "$@"; else python3 "$@"; fi; }; '
                   + f'status=0; {step} || status=$?; '
                   + 'printf "%s|%s|%s\\n" "$status" "$BUILDLOG_MEM_OUTCOME" "${BUILDLOG_START:-}"')
        result = subprocess.run(["bash", "-c", command, "test", str(INVOKE), str(self.ran)],
            capture_output=True, text=True, timeout=30,
            check=False, env=environment)
        self.admission.server.reply_filter = lambda reply: reply
        return result

    def hook_calls(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def recorder_actions(self) -> list[str]:
        return self.recorded.read_text().splitlines() if self.recorded.exists() else []

    def reservations(self) -> list[Path]:
        return [Path(record.path) for record in self.admission.server.reservations.values()]

    def test_hook_leaves_a_held_step_before_it_starts(self) -> None:
        result = self.run_step(":", ":;")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "75|LeftQueue|")
        self.assertFalse(self.ran.exists())
        self.assertEqual(self.hook_calls(), 1)
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.recorder_actions(), ["step-context"])
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
        self.assertEqual(self.recorder_actions(), ["step-context", "step"])

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
                + f'printf "%s\\n" "$*" > "{ran_args}"; python3 "$TEST_CAPTURE_WIDTH" > "{reservation}"; }}; '
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




class QueueExplanationTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        write_memory_config(self.root / "build_admission.conf")
        self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.root),
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
















    def test_next_recovery_transitions_do_not_consume_a_start(self) -> None:
        due = gate.RecoveryStarts(NOW_EPOCH + 120, NOW_EPOCH)
        for previous, pressure, at, expected in (
                (gate.NoRecoverySpacing(), gate.MemoryPressure(8, condition="full"), NOW_EPOCH, gate.PressureHoldRecorded()),
                (gate.PressureHoldRecorded(), gate.NoPressureHold(), NOW_EPOCH, due),
                (due, gate.NoPressureHold(), NOW_EPOCH + 30, due),
                (due, gate.NoPressureHold(), NOW_EPOCH + 120, gate.NoRecoverySpacing()),
                (due, gate.MemoryPressure(8, condition="full"), NOW_EPOCH + 120, gate.PressureHoldRecorded())):
            with self.subTest(previous=previous, at=at, pressure=pressure):
                self.assertEqual(gate.next_recovery(previous, pressure, at, policy=admission_policy_fixture()), expected)























    def test_recorded_priority_retains_its_fixed_strict_end(self) -> None:
        _ = self.waiting(300, 12 * GIB, 20, priority_since=NOW.timestamp() - 60)
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
        self.assertEqual(running.basis.decayed_at, old_peak_at + admission_policy_fixture().peak_hold.default_seconds)
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
        write_recovery_fixture(gate.admission_dir(self.root), gate.RecoveryStarts(NOW.timestamp() + 120, starts_at))
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
        write_recovery_fixture(gate.admission_dir(self.root),
                            gate.RecoveryStarts(NOW.timestamp() + 120, NOW.timestamp() + 37))
        result = gate.head_after_steps_end(gate.explain_queue(), ())
        if not isinstance(result, gate.HeadStillHeld):
            self.fail(f"expected budget and timer holds, got {result}")
        self.assertEqual(result.causes, ("build budget", "pressure"))

    def test_recovery_spacing_release_keeps_the_required_endings_and_drops(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {40 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.waiting(300, 4 * GIB, 20)
        starts_at = NOW.timestamp() + 37
        write_recovery_fixture(gate.admission_dir(self.root), gate.RecoveryStarts(NOW.timestamp() + 120, starts_at))
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
        self.assertEqual(explanation.host.pressure, gate.MemoryPressure(11, condition="full"))
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
        self.assertEqual(recovery.host.pressure, gate.MemoryPressure(0, next_start_at=NOW.timestamp() + 30, condition="full"))
        self.assertEqual(recovery.head.decision.state, "hold")
        self.assertEqual(record.read_bytes(), before)

    def snapshot_inputs(self) -> gate.QueueInputs:
        return fixture_queue_inputs(gate.admission_dir(self.root), gate.process_table(self.root / "proc"),
                                    NOW_EPOCH, admission_policy_fixture())

    def test_queue_read_from_uses_no_files_processes_or_reader_limits(self) -> None:
        _ = self.running(100, 6)
        _ = self.waiting(300, 12 * GIB, 20)
        _ = self.waiting(400, GIB, 5)
        inputs = self.snapshot_inputs()
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        expected = gate.queue_read_from(inputs, limits, policy=admission_policy_fixture())
        with patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "1", "BUILDLOG_PROC": "/absent"}), \
             patch.object(Path, "open", side_effect=AssertionError("pure read opened file")), \
             patch.object(Path, "read_text", side_effect=AssertionError("pure read read file")), \
             patch.object(gate, "process_table", side_effect=AssertionError("pure read scanned processes")), \
             patch.object(gate, "read_pressure", side_effect=AssertionError("pure read measured pressure")), \
             patch.object(gate, "read_recovery", side_effect=AssertionError("pure read read recovery")):
            self.assertEqual(gate.queue_read_from(inputs, limits, policy=admission_policy_fixture()), expected)

    def test_frozen_pass_keeps_one_window_for_admission_view_explanation_and_wait_text(self) -> None:
        policy = admission_policy_fixture()
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.running(100, 10, age=30)
        self.readings[100] = gate.MeasuredMemory(10 * GIB)
        _ = self.waiting(300, 12 * GIB, 20, priority_since=NOW_EPOCH - 60)
        _ = self.waiting(400, 2 * GIB, 5)
        original = replace(self.snapshot_inputs(), host=gate.HostMemory(60 * GIB, 60 * GIB),
                           blockers_for=frozenset(("300-head", "400-warmup")))
        for offset, window_type in ((0, gate.StrictUntil), (policy.timing.strict_seconds, gate.BackfillUntil)):
            with self.subTest(offset=offset):
                inputs = replace(original, now=NOW_EPOCH + offset)
                resolved = gate.head_policy(inputs, inputs.now, limits, policy)
                result = gate.admission_pass(inputs, inputs.now, limits, policy)
                reason = result.rows[0].reason
                assert isinstance(reason, gate.HeadAdmissionStatus)
                self.assertIsInstance(reason.backfill, window_type)
                self.assertEqual(reason.backfill, resolved.window)
                with patch.object(gate, "head_window", side_effect=AssertionError("reader recomputed the window")):
                    reading = gate.queue_read_from_pass(inputs, limits, policy, result)
                    text = gate.head_wait_text(reason, ZoneInfo("UTC"), policy)
                head = reading.explanation.head
                assert isinstance(head, gate.HeadExplanation)
                self.assertEqual(head.order, resolved.window)
                self.assertEqual(reading.view.waiting[0].reason, reason)
                self.assertEqual(head.waiter.reason, reason)
                self.assertEqual(head.decision, result.rows[0].decision)
                self.assertEqual(reading.view.waiting[1].reason, result.rows[1].reason)
                self.assertEqual(reading.explanation.line.steps[0].waiter.reason, result.rows[1].reason)
                self.assertTrue(text.endswith(gate.window_clause(reason.backfill, limits.budget, reason.need, ZoneInfo("UTC"))))

    def test_recovery_release_keeps_required_endings_and_frozen_window(self) -> None:
        policy = admission_policy_fixture()
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.running(100, 18, age=30)
        self.readings[100] = gate.MeasuredMemory(18 * GIB)
        _ = self.waiting(300, 12 * GIB, 20, priority_since=NOW_EPOCH - 60)
        _ = self.waiting(400, GIB, 5)
        recovery = gate.RecoveryStarts(NOW_EPOCH + 120, NOW_EPOCH + 30)
        inputs = replace(self.snapshot_inputs(), host=gate.HostMemory(60 * GIB, 60 * GIB), recovery=recovery)
        reading = gate.queue_read_from(inputs, limits, policy)
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        reason = head.waiter.reason
        assert isinstance(reason, gate.HeadAdmissionStatus)
        self.assertEqual(head.order, reason.backfill)
        self.assertEqual(head.decision.state, "hold")
        with patch.object(Path, "open", side_effect=AssertionError("release opened a file")), \
             patch.object(Path, "read_text", side_effect=AssertionError("release read a file")), \
             patch.object(time, "time", side_effect=AssertionError("release read the clock")):
            release = gate.smallest_head_release(reading.explanation)
            ended = gate.inputs_after_steps_end(inputs, frozenset((0,)))
            after = gate.admission_pass(ended, inputs.now, limits, policy)
        self.assertIsInstance(release, gate.HeadStartsAt)
        assert isinstance(release, gate.HeadStartsAt)
        self.assertEqual((release.at, release.ended), (recovery.next_start_at, (0,)))
        self.assertEqual(after.policy.window, head.order)
        pressure = after.rows[0].decision.pressure
        assert isinstance(pressure, gate.MemoryPressure)
        self.assertEqual(pressure.next_start_at, release.at)
        self.assertEqual(reading.explanation.decided_from, inputs)

    def test_release_helper_keeps_due_recovery_and_uses_the_frozen_pass_context(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.running(100, 8, age=30)
        _ = self.running(200, 5, age=30)
        _ = self.waiting(300, 12 * GIB, 20, priority_since=NOW_EPOCH - 60)
        _ = self.waiting(400, GIB, 5)
        due = gate.RecoveryStarts(NOW_EPOCH + 120, NOW_EPOCH)
        inputs = replace(self.snapshot_inputs(), recovery=due, host=gate.HostMemory(60 * GIB, 60 * GIB))
        explanation = gate.queue_read_from(inputs, limits, policy=admission_policy_fixture()).explanation
        ended = replace(inputs, running=inputs.running[1:], reservations=inputs.reservations[1:],
                        host=gate.HostMemory(61 * GIB, 60 * GIB))
        expected = gate.admission_pass(ended, NOW_EPOCH, limits, policy=admission_policy_fixture()).rows[0].decision
        with patch.object(gate, "admission_walk", wraps=gate.admission_walk) as walk, \
             patch.object(Path, "read_text", side_effect=AssertionError("release reread the ledger")):
            release = gate.head_after_steps_end(explanation, (0,))
        self.assertEqual(walk.call_count, 1)
        self.assertIsInstance(release, gate.HeadAdmitsAfterEnd)
        assert isinstance(release, gate.HeadAdmitsAfterEnd)
        self.assertEqual(release.decision, expected)
        self.assertEqual(explanation.decided_from.recovery, due)

    def test_pass_and_view_share_each_rows_decision_and_reason_in_every_scenario(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.running(100, 8)
        self.readings[100] = gate.MeasuredMemory(8 * GIB)
        _ = self.waiting(300, 12 * GIB, 20,
                         priority_since=NOW_EPOCH - 120)
        _ = self.waiting(400, 2 * GIB, 5)
        inputs = self.snapshot_inputs()
        head_path, head = inputs.ordered[0]
        override: gate.HeadAdmissionOverride = {
            "waiter": head_path.name, "pid": head["pid"], "start_time": head["start_time"],
            "unit": "head", "step": "nextest", "need": head["need"], "free": 40 * GIB,
            "reserve": 3 * GIB, "why": "urgent", "set_by": "showrunner",
        }
        scenarios = {
            "strict": inputs,
            "backfill": replace(inputs, now=NOW_EPOCH + 500),
            "no priority": replace(inputs, ordered=[(path, cast(gate.Waiter, cast(object,
                {key: value for key, value in row.items() if key != "priority_since"})))
                for path, row in inputs.ordered],
                host=gate.HostMemory(0, 60 * GIB)),
            "pressure": replace(inputs, pressure=gate.MemoryPressure(8, condition="full")),
            "recovery": replace(inputs, recovery=gate.RecoveryStarts(NOW_EPOCH + 120, NOW_EPOCH + 30)),
            "older fit": replace(inputs, host=gate.HostMemory(60 * GIB, 60 * GIB),
                ordered=[(head_path, {**head, "need": 10 * GIB, "since": NOW_EPOCH - 60,
                                      "priority_since": NOW_EPOCH - 660}),
                         (inputs.ordered[1][0], {**inputs.ordered[1][1], "need": 10 * GIB,
                                                "since": NOW_EPOCH - 30})]),
            "recorded priority": replace(inputs, ordered=[(head_path, {**head, "priority_rank": 0}),
                                                             inputs.ordered[1]],
                priority={"unit": "head", "why": "the user's bump", "set_at": NOW_EPOCH,
                          "set_by": "showrunner"}),
            "override": replace(inputs, override=override, host=gate.HostMemory(60 * GIB, 60 * GIB)),
            "unavailable": replace(inputs, host=gate.ReadingUnavailable()),
            "expired": replace(inputs, ordered=[(head_path, {**head, "estimated_at": NOW_EPOCH - 600}),
                                                     inputs.ordered[1]]),
        }
        for scenario, evidence in scenarios.items():
            with self.subTest(scenario=scenario):
                result = gate.admission_pass(evidence, evidence.now, limits, policy=admission_policy_fixture())
                reading = gate.queue_read_from(evidence, limits, policy=admission_policy_fixture())
                self.assertEqual(reading.explanation.decided_from, evidence)
                self.assertEqual([step.reason for step in reading.view.waiting],
                                 [row.reason for row in result.rows])
                first = reading.explanation.head
                assert isinstance(first, gate.HeadExplanation)
                self.assertEqual([first.decision, *(row.decision for row in reading.explanation.line.steps)],
                                 [row.decision for row in result.rows])
                self.assertEqual([row.owner for row in result.rows], ["300-head", "400-warmup"])
                self.assertTrue(all(isinstance(row.blocker, gate.BlockerNotRequested) for row in result.rows))
                if scenario == "older fit":
                    self.assertIsInstance(result.rows[1].reason, gate.BehindOlderFit)

    def test_pass_is_pure_and_ignores_reader_environment_and_clock(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.waiting(300, 4 * GIB, 1)
        _ = self.waiting(400, 5 * GIB, 0)
        inputs = self.snapshot_inputs()
        before = gate.snapshot_value(inputs)
        expected = gate.admission_pass(inputs, NOW_EPOCH, limits, policy=admission_policy_fixture())
        with patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "1",
                "BUILDLOG_PROC": str(self.root / "empty-proc"),
                "BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(self.root / "tripwire.jsonl")}), \
             patch.object(Path, "read_text", side_effect=AssertionError("pass read a file")), \
             patch.object(Path, "write_text", side_effect=AssertionError("pass wrote a file")), \
             patch.object(gate, "process_table", side_effect=AssertionError("pass scanned processes")), \
             patch.object(gate, "read_meminfo", side_effect=AssertionError("pass read host memory")), \
             patch.object(gate, "read_pressure", side_effect=AssertionError("pass read pressure")), \
             patch.object(gate, "read_recovery", side_effect=AssertionError("pass read recovery")), \
             patch.object(time, "time", side_effect=AssertionError("pass read the clock")):
            self.assertEqual(gate.admission_pass(inputs, NOW_EPOCH, limits, policy=admission_policy_fixture()), expected)
        self.assertEqual(gate.snapshot_value(inputs), before)

    def test_one_walk_decides_all_rows_and_each_requested_ending_is_one_more_scenario(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.running(100, 18, age=30)
        _ = self.running(200, 1, age=30)
        _ = self.waiting(300, 10 * GIB, 20)
        _ = self.waiting(400, 4 * GIB, 5)
        inputs = replace(self.snapshot_inputs(), host=gate.HostMemory(60 * GIB, 60 * GIB))
        with patch.object(gate, "admission_walk", wraps=gate.admission_walk) as walk:
            _ = gate.queue_read_from(inputs, limits, policy=admission_policy_fixture())
        self.assertEqual(walk.call_count, 1)
        with patch.object(gate, "admission_walk", wraps=gate.admission_walk) as walk:
            result = gate.admission_pass(replace(inputs, blockers_for=frozenset(("300-head",))), NOW_EPOCH, limits, policy=admission_policy_fixture())
        self.assertEqual(walk.call_count, 1 + len(inputs.running))
        self.assertEqual(result.rows[0].blocker, gate.BlockingIndex(0))
        self.assertIsInstance(result.rows[1].blocker, gate.BlockerNotRequested)
        requested = replace(inputs, blockers_for=frozenset(("300-head", "400-warmup")))
        with patch.object(gate, "admission_walk", wraps=gate.admission_walk) as walk:
            result = gate.admission_pass(requested, NOW_EPOCH, limits, policy=admission_policy_fixture())
        self.assertEqual(walk.call_count, 1 + len(inputs.running))
        self.assertTrue(all(isinstance(row.blocker, gate.BlockingIndex) for row in result.rows))
        pressure_held = gate.admission_pass(replace(requested, pressure=gate.MemoryPressure(8, condition="full")), NOW_EPOCH, limits, policy=admission_policy_fixture())
        self.assertTrue(all(isinstance(row.blocker, gate.NoSingleBlocker) for row in pressure_held.rows))

    def test_two_proposals_charge_the_first_need_to_the_second(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.waiting(300, 4 * GIB, 1)
        _ = self.waiting(400, 5 * GIB, 0)
        inputs = replace(self.snapshot_inputs(), host=gate.HostMemory(60 * GIB, 60 * GIB))
        result = gate.admission_pass(inputs, NOW_EPOCH, limits, policy=admission_policy_fixture())
        self.assertEqual([row.decision.state for row in result.rows], ["admit", "admit"])
        self.assertEqual([row.decision.charged for row in result.rows], [0, 4 * GIB])
        self.assertEqual([row.admission.rank for row in result.rows
                          if isinstance(row.admission, gate.ProposedAdmission)], [1, 2])

    def test_due_recovery_proposes_one_start_without_consuming_the_uncommitted_slot(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.waiting(300, GIB, 1)
        _ = self.waiting(400, GIB, 0)
        due = gate.RecoveryStarts(NOW_EPOCH + 120, NOW_EPOCH)
        inputs = replace(self.snapshot_inputs(), recovery=due, host=gate.HostMemory(60 * GIB, 60 * GIB))
        result = gate.admission_pass(inputs, NOW_EPOCH, limits, policy=admission_policy_fixture())
        self.assertEqual(result.policy.recovery, due)
        self.assertEqual(result.rows[0].admission,
            gate.ProposedAdmission(1, gate.RecoveryStarts(NOW_EPOCH + 120, NOW_EPOCH + 30), gate.OverrideKept()))
        self.assertIsInstance(result.rows[1].admission, gate.NotAdmitted)
        self.assertEqual(result.rows[1].decision.state, "hold")

    def test_expired_estimate_keeps_its_room_and_blocks_a_younger_fit(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.waiting(300, 18 * GIB, 1, estimated_at=NOW_EPOCH - admission_policy_fixture().timing.head_seconds)
        _ = self.waiting(400, 5 * GIB, 0)
        inputs = replace(self.snapshot_inputs(), host=gate.HostMemory(60 * GIB, 60 * GIB))
        result = gate.admission_pass(inputs, NOW_EPOCH, limits, policy=admission_policy_fixture())
        self.assertEqual(result.rows[0].reason, gate.EstimateExpired(NOW_EPOCH - admission_policy_fixture().timing.head_seconds))
        self.assertEqual([row.decision.state for row in result.rows], ["hold", "hold"])
        self.assertIsInstance(result.rows[1].reason, gate.BehindOlderFit)
        self.assertEqual(result.rows[1].decision.charged, 18 * GIB)
        self.assertTrue(all(isinstance(row.admission, gate.NotAdmitted) for row in result.rows))

    def test_estimate_expiry_boundary_agrees_between_the_walk_and_the_pass_reason(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.waiting(300, GIB, 1)
        inputs = self.snapshot_inputs()
        path, waiter = inputs.ordered[0]
        for age in (None, -1, admission_policy_fixture().timing.head_seconds - 0.001, admission_policy_fixture().timing.head_seconds, admission_policy_fixture().timing.head_seconds + 1):
            with self.subTest(age=age):
                row = waiter.copy()
                if age is not None:
                    row["estimated_at"] = NOW_EPOCH - age
                expired = age is not None and age >= admission_policy_fixture().timing.head_seconds
                self.assertEqual(gate.estimate_expired(row, NOW_EPOCH, policy=admission_policy_fixture()), expired)
                result = gate.admission_pass(replace(inputs, ordered=[(path, row)]), NOW_EPOCH, limits, policy=admission_policy_fixture()).rows[0]
                self.assertEqual(result.decision.state, "hold" if expired else "admit")
                self.assertEqual(isinstance(result.reason, gate.EstimateExpired), expired)
                self.assertEqual(isinstance(result.admission, gate.NotAdmitted), expired)

    def test_release_amount_and_counterfactual_free_memory_use_only_resident_bytes(self) -> None:
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        _ = self.running(100, 6)
        _ = self.running(200, 5)
        self.readings[100] = gate.MeasuredMemory(6 * GIB, swap_bytes=2 * GIB)
        self.readings[200] = gate.MeasuredMemory(GIB, swap_bytes=2 * GIB)
        _ = self.waiting(300, GIB, 1)
        inputs = self.snapshot_inputs()
        compiler = gate.RunningStep("", "sccache.service", "compile", "builds.slice", 0,
                                    gate.CompilerServiceMemory(3 * GIB, swap_bytes=GIB))
        inputs = replace(inputs, running=(*inputs.running, compiler))
        explanation = gate.queue_read_from(inputs, limits, policy=admission_policy_fixture()).explanation
        for ended, expected in (((), 0), ((0,), 6 * GIB), ((1,), GIB), ((2,), 3 * GIB), ((0, 1, 2), 10 * GIB)):
            with self.subTest(ended=ended):
                self.assertEqual(gate.released_resident_bytes(inputs.running, frozenset(ended)), expected)
                changed = gate.inputs_after_steps_end(inputs, frozenset(ended))
                assert isinstance(inputs.host, gate.HostMemory) and isinstance(changed.host, gate.HostMemory)
                self.assertEqual(changed.host.available - inputs.host.available, expected)
                release = gate.head_after_steps_end(explanation, ended)
                self.assertIsInstance(release, gate.HeadAdmitsAfterEnd)
                assert isinstance(release, gate.HeadAdmitsAfterEnd)
                self.assertEqual(release.freed, expected)
        unmeasured = (replace(compiler, measured=gate.PromisedMemory(8 * GIB)),
                      replace(compiler, measured=gate.GoneMemory()))
        self.assertEqual(gate.released_resident_bytes(unmeasured, frozenset((0, 1))), 0)

    def test_follower_walk_checks_keep_resource_fit_independent_of_earlier_waiter_charge(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {60 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.waiting(300, 15 * GIB, 1)
        _ = self.waiting(400, 10 * GIB, 0)
        reading = gate.queue_read_from(self.snapshot_inputs(), gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), admission_policy_fixture())
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
        view = gate.queue_read_from(self.snapshot_inputs(), gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), admission_policy_fixture()).view
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

    def test_blocker_owner_set_round_trips_and_snapshot_serves_it_under_a_held_lock(self) -> None:
        _ = self.waiting(300, GIB, 1)
        _ = self.waiting(400, GIB, 0)
        priorities: tuple[gate.QueuePriority | gate.NoQueuePriority, ...] = (
            gate.NO_QUEUE_PRIORITY,
            {"unit": "head", "why": "the user's bump", "set_at": NOW_EPOCH, "set_by": "showrunner",
             "transition": gate.SettledPriority()},
            {"unit": "head", "why": "the user's bump", "set_at": NOW_EPOCH, "set_by": "showrunner",
             "transition": gate.PendingPriorityTransition("transition", "set", NOW_EPOCH)},
            {"unit": "head", "why": "the user's bump", "set_at": NOW_EPOCH, "set_by": "showrunner",
             "transition": gate.PendingPriorityTransition("transition", "clear", NOW_EPOCH)})
        for priority in priorities:
            for owners in (frozenset[str](), frozenset(('400-warmup', '300-head'))):
                with self.subTest(owners=owners):
                    inputs = replace(self.snapshot_inputs(), blockers_for=owners, priority=priority)
                    self.assertEqual(gate.parse_snapshot_value(gate.snapshot_value(inputs), gate.QueueInputs), inputs)

    def test_active_compiler_memory_keeps_its_variant_in_a_lock_free_snapshot(self) -> None:
        _ = self.waiting(300, 18 * GIB, 1)
        inputs = replace(self.snapshot_inputs(), running=(gate.RunningStep("", "sccache.service", "compile",
            "builds.slice", 0, gate.CompilerServiceMemory(2 * GIB)),))
        restored = gate.parse_snapshot_value(gate.snapshot_value(inputs), gate.QueueInputs)
        assert isinstance(restored, gate.QueueInputs)
        compiler, = restored.running
        self.assertIsInstance(compiler.measured, gate.CompilerServiceMemory)
        assert isinstance(compiler.measured, gate.CompilerServiceMemory)
        self.assertEqual((compiler.session_name, compiler.measured.bytes), ("sccache.service", 2 * GIB))
        reading = gate.queue_read_from(restored, gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), admission_policy_fixture())
        assert isinstance(reading.explanation.head, gate.HeadExplanation)
        self.assertEqual(reading.explanation.head.decision.measured, 2 * GIB)

    def test_gather_and_publication_never_resolve_or_store_limits(self) -> None:
        with patch.object(gate, "resolve_admission_limits", side_effect=AssertionError("the builder resolved limits")):
            inputs = self.snapshot_inputs()
            encoded = gate.snapshot_value(inputs)
        self.assertIsInstance(encoded, dict)
        assert isinstance(encoded, dict)
        for key in ("limits", "budget", "reserve"):
            self.assertNotIn(key, cast(dict[str, object], encoded))

    def test_old_head_without_priority_never_reports_a_moving_window(self) -> None:
        path = self.waiting(300, 15 * GIB, 20)
        before = path.read_bytes()
        for now in (NOW.timestamp(), NOW.timestamp() + 60):
            with self.subTest(now=now), patch.object(time, 'time', return_value=now):
                reading = gate.queue_read_from(replace(self.snapshot_inputs(), now=now), gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), admission_policy_fixture())
                head = reading.explanation.head
                self.assertIsInstance(head, gate.HeadExplanation)
                assert isinstance(head, gate.HeadExplanation)
                self.assertFalse(head.has_priority)
                self.assertIsInstance(head.order, gate.WindowNotStarted)
                self.assertEqual(head.decision.state, 'hold')
                self.assertEqual(path.read_bytes(), before)




class ConfiguredAdmissionPolicyTests(MemoryQueueFixture):
    config: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "build_admission.conf"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root), "LINT_CONFIG_FILE": str(self.config.with_name("lint.conf")),
        })))
        self.write_config()
        commit_config_fixture(self.config)

    def write_config(self, **changes: str | None) -> None:
        write_memory_config(self.config, **changes)

    def test_unavailable_key_refuses_without_recovery_and_accepts_the_repaired_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "build_admission.conf"
            for key in ("head_seconds", "build_budget_gib", "program_first_peak_gib.cargo"):
                for bad in (None, "invalid"):
                    with self.subTest(key=key, value=bad), patch.dict(os.environ, {"CONF_DIR": directory}):
                        write_memory_config(config, **{key: bad})
                        result = gate.read_admission_configuration()
                        self.assertIsInstance(result, gate.UnusableAdmissionConfiguration)
                        assert isinstance(result, gate.UnusableAdmissionConfiguration)
                        self.assertIn(str(config), result.reason)
                        self.assertIn(key, result.reason)
                        self.assertEqual(len(result.reason.splitlines()), 1)
                        write_memory_config(config, **{key: "7"})
                        repaired = gate.read_admission_configuration()
                        self.assertIsInstance(repaired, gate.UsableAdmissionConfiguration)
                        assert isinstance(repaired, gate.UsableAdmissionConfiguration)
                        if key == "head_seconds":
                            self.assertEqual(repaired.config.policy.timing.head_seconds, 7)
                        elif key == "build_budget_gib":
                            self.assertEqual(repaired.config.build_budget_bytes, 7 * GIB)
                        else:
                            self.assertEqual(repaired.config.program_first_peaks["cargo"], 7 * GIB)

    def test_cargo_estimate_reads_the_configured_first_peak_at_each_use(self) -> None:
        for peak in (7, 13):
            with self.subTest(peak=peak):
                self.write_config(**{"program_first_peak_gib.cargo": str(peak)})
                result = gate.expected_peak(HANA, "clippy", "test-host", NOW, self.root, first_peak_bytes=gate.read_memory_config().program_first_peaks["cargo"], budget=gate.read_memory_config().build_budget_bytes)
                self.assertEqual(result.bytes, peak * GIB)
                self.assertEqual(result.source, "fallback")










    def test_invalid_policy_values_name_the_default_and_the_bad_input(self) -> None:
        baseline = gate.read_memory_config()
        keys = ("ramp_seconds", "head_seconds", "strict_seconds", "backfill_seconds",
                "recovery_start_seconds", "recovery_window_seconds", "full_pressure_limit",
                "swap_used_limit", "some_pressure_limit", "test_phase_margin", "peak_hold_seconds")
        for key in keys:
            for raw in (None, "abc", "-5", "nan", "inf", "0"):
                with self.subTest(key=key, raw=raw):
                    self.write_config(**{key: raw})
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        config = gate.read_memory_config()
                    self.assertEqual(config, baseline)
                    self.assertIn(key, errors.getvalue())
                    self.assertIn("warning:", errors.getvalue())
        for key in keys[:6]:
            self.write_config(**{key: "1.5"})
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(gate.read_memory_config(), baseline)
            self.assertIn(key, errors.getvalue())

    def test_one_config_read_supplies_policy_margin_budget_and_service_intervals(self) -> None:
        self.write_config(head_seconds="321", test_phase_margin="1.7", build_budget_gib="9.5",
                          running_pass_seconds="3")
        with patch.object(conf, "read", wraps=conf.read) as reader:
            config = gate.read_memory_config()
        reader.assert_called_once_with("build_admission")
        self.assertEqual(config.policy.timing.head_seconds, 321)
        self.assertEqual(config.test_phase_margin, 1.7)
        self.assertEqual(config.build_budget_bytes, int(9.5 * GIB))
        self.assertEqual(config.running_pass_seconds, 3)

    def test_program_first_peaks_are_private_bytes_and_reload_at_use(self) -> None:
        self.write_config(**{"program_first_peak_gib.rust-analyzer": "18",
                             "program_first_peak_gib.indexer": "2.5"})
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            config = gate.read_memory_config()
        self.assertEqual(dict(config.program_first_peaks),
                         {"cargo": 12 * GIB, "rust-analyzer": 18 * GIB, "indexer": int(2.5 * GIB)})
        self.assertEqual(errors.getvalue(), "")
        self.write_config(**{"program_first_peak_gib.rust-analyzer": "7"})
        self.assertEqual(dict(gate.read_memory_config().program_first_peaks), {"cargo": 12 * GIB, "rust-analyzer": 7 * GIB})
        self.assertEqual(config.program_first_peaks["rust-analyzer"], 18 * GIB)

    def test_absent_program_first_peak_has_no_invented_default(self) -> None:
        self.write_config()
        config = gate.read_memory_config()
        self.assertEqual(dict(config.program_first_peaks), {"cargo": 12 * GIB})

    def test_invalid_program_first_peak_is_diagnosed_without_authorizing_a_size(self) -> None:
        key = "program_first_peak_gib.rust-analyzer"
        for raw in ("abc", "0", "-1", "nan", "inf", "1e308"):
            with self.subTest(raw=raw):
                self.write_config(**{key: raw, "program_first_peak_gib.indexer": "2"})
                with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(conf.Unusable) as refused:
                    _ = gate.read_memory_config()
                self.assertIn(key, str(refused.exception))
                self.assertIn(raw, str(refused.exception))
                self.assertEqual(errors.getvalue(), "")




    def test_timing_rules_use_supplied_policy_without_reading_configuration(self) -> None:
        self.write_config(ramp_seconds="60", strict_seconds="30", backfill_seconds="20",
                          recovery_window_seconds="45")
        policy = gate.read_memory_config().policy
        path = self.running(100, 8, age=90)
        row = gate.read_reservation(path)
        waiter: gate.Waiter = {"pid": 300, "start_time": "head", "need": GIB,
            "since": NOW_EPOCH - 900, "priority_since": NOW_EPOCH - 45}
        with patch.object(gate, "read_memory_config", side_effect=AssertionError("pure rule")):
            current = gate.step_promise(row, NOW, gate.MeasuredMemory(GIB), policy)
            default = gate.step_promise(row, NOW, gate.MeasuredMemory(GIB), admission_policy_fixture())
            window = gate.head_window(waiter, NOW_EPOCH, policy)
            recovery = gate.next_recovery(gate.PressureHoldRecorded(), gate.NoPressureHold(), NOW_EPOCH, policy)
        assert isinstance(current, gate.MeasuredMemory)
        assert isinstance(default, gate.MeasuredMemory)
        self.assertIsInstance(current.promise, gate.NoPromise)
        self.assertEqual(default.promise, gate.RampPromise(8 * GIB))
        self.assertEqual(window, gate.BackfillUntil(NOW_EPOCH + 5))
        self.assertEqual(recovery, gate.RecoveryStarts(NOW_EPOCH + 45, NOW_EPOCH))

    def test_pressure_reader_uses_the_supplied_thresholds_after_config_edit(self) -> None:
        self.write_config(full_pressure_limit="9", swap_used_limit="70", some_pressure_limit="12")
        policy = gate.read_memory_config().policy
        _ = (self.root / "pressure").write_text("full avg10=8\nsome avg10=15\n")
        host = gate.HostMemory(20 * GIB, 60 * GIB, 10 * GIB, 2 * GIB)
        self.write_config(full_pressure_limit="3", swap_used_limit="95", some_pressure_limit="30")
        with patch.object(gate, "read_memory_config", side_effect=AssertionError("reader reread config")):
            self.assertEqual(gate.read_pressure(host, policy=policy), gate.MemoryPressure(8, 15, 80, condition="swap"))
            self.assertIsInstance(gate.read_pressure(policy=policy), gate.NoPressureHold)
            self.assertEqual(gate.read_pressure(host, policy=admission_policy_fixture()),
                             gate.MemoryPressure(8, 15, 80, condition="full"))

    def test_missing_kind_peak_hold_inherits_the_default_without_a_fallback(self) -> None:
        self.write_config(peak_hold_seconds="900")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            config = gate.memory_config()
        self.assertEqual(config.policy.peak_hold.seconds_for("merge-gate"), 900)
        self.assertEqual(errors.getvalue(), "")

    def test_peak_hold_changes_only_the_configured_step_kind_at_next_read(self) -> None:
        self.write_config(**{"peak_hold_seconds": "1500", "peak_hold_seconds.merge-gate": "180"})
        for pid, kind in ((100, "merge-gate"), (200, "nextest")):
            path = self.running(pid, 8, age=1800, peak=8, fields={"step": kind})
            _ = path.with_suffix(".wave").write_text(f"{8 * GIB} {int(NOW_EPOCH - 200)}")
        processes = gate.process_table(self.root / "proc")
        policy = gate.read_memory_config().policy
        readings: list[gate.StepMemory] = []
        for pid in (100, 200):
            path = gate.admission_dir(self.root) / f"{pid}.reservation"
            row = gate.read_reservation(path)
            readings.append(gate.reservation_memory(path, row, NOW, processes, self.root / "proc", policy))
        self.assertIsInstance(gate.charge_basis(readings[0]), gate.PeakDecayed)
        self.assertEqual(gate.charge_basis(readings[0]).bytes, GIB)
        self.assertIsInstance(gate.charge_basis(readings[1]), gate.PeakSoFar)
        self.assertEqual(gate.charge_basis(readings[1]).bytes, 8 * GIB)
        self.write_config(**{"peak_hold_seconds": "1500", "peak_hold_seconds.merge-gate": "300"})
        row = gate.read_reservation(gate.admission_dir(self.root) / "100.reservation")
        restored = gate.reservation_memory(gate.admission_dir(self.root) / "100.reservation", row,
            NOW, processes, self.root / "proc", gate.read_memory_config().policy)
        self.assertIsInstance(gate.charge_basis(restored), gate.PeakSoFar)
        self.assertEqual(gate.charge_basis(restored).bytes, 8 * GIB)

    def test_invalid_kind_peak_hold_uses_the_configured_default_with_one_fallback(self) -> None:
        key = "peak_hold_seconds.merge-gate"
        for raw in ("abc", "-5", "nan", "1.5"):
            with self.subTest(raw=raw):
                self.write_config(**{"peak_hold_seconds": "900", key: raw})
                with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(conf.Unusable) as refused:
                    _ = gate.read_memory_config()
                self.assertIn(key, str(refused.exception))
                self.assertIn(raw, str(refused.exception))
                self.assertEqual(errors.getvalue(), "")

    def test_counterfactuals_keep_the_policy_that_decided_the_explanation(self) -> None:
        _ = self.running(100, 18, age=30)
        self.readings[100] = gate.MeasuredMemory(8 * GIB)
        path = self.waiter(300, NOW_EPOCH - 450, "/tmp/head", "head", need=8)
        row = cast(dict[str, object], json.loads(path.read_text()))
        row["estimated_at"] = NOW_EPOCH - 450
        _ = path.write_text(json.dumps(row))
        policy = gate.read_memory_config().policy
        inputs = fixture_queue_inputs(gate.admission_dir(self.root),
            gate.process_table(self.root / "proc"), NOW_EPOCH, policy)
        limits = gate.resolve_admission_limits(60 * GIB, gate.read_memory_config())
        explanation = gate.queue_read_from(inputs, limits, policy).explanation
        before = (gate.smallest_head_release(explanation), gate.head_after_steps_end(explanation, (0,)))
        self.assertIsInstance(before[0], gate.HeadAdmitsAfterEnd)
        self.write_config(head_seconds="300")
        changed = gate.read_memory_config().policy
        fresh = gate.queue_read_from(inputs, limits, changed).explanation
        self.assertIsInstance(gate.smallest_head_release(fresh), gate.EstimateExpired)
        with patch.object(gate, "read_memory_config", side_effect=AssertionError("frozen policy")), \
             patch.object(gate, "memory_config", side_effect=AssertionError("frozen policy")):
            self.assertEqual((gate.smallest_head_release(explanation),
                              gate.head_after_steps_end(explanation, (0,))), before)
            self.assertEqual(gate.queue_read_from(inputs, limits, policy).explanation, explanation)
        self.assertEqual(explanation.policy.timing.head_seconds, 600)
        self.assertEqual(gate.parse_snapshot_value(gate.snapshot_value(explanation),
            gate.QueueExplanation), explanation)

    def test_overflowing_config_budget_refuses_even_with_an_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "build_admission.conf"
            write_memory_config(config, build_budget_gib="1e308")
            for override in ("", "24"):
                with self.subTest(override=override), patch.dict(os.environ, {
                        "CONF_DIR": directory, "BUILDLOG_BUILD_BUDGET_GIB": override}), \
                        patch.object(client, "_connect", side_effect=AssertionError("refusal contacted service")), \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    status = client.main(["admit", "--pid", str(100), "--", "cargo", "clippy"])
                self.assertEqual(status, 2)
                self.assertIn(str(config), errors.getvalue())
                self.assertIn("build_budget_gib='1e308'", errors.getvalue())
                self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_changed_head_age_applies_to_the_next_check_without_restart(self) -> None:
        _ = self.waiter(300, NOW_EPOCH - 450, "/tmp/head", "head", need=8)
        _ = self.meminfo.write_text(f"MemAvailable: {4 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        with patch.object(gate, "sccache_memory", return_value=[gate.CompilerServiceMemory(8 * GIB)]):
            before_policy = gate.read_memory_config().policy
            inputs = fixture_queue_inputs(gate.admission_dir(self.root), gate.process_table(self.root / "proc"), NOW_EPOCH, before_policy)
            before = gate.admission_pass(inputs, NOW_EPOCH, gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), before_policy).rows[0]
            self.assertEqual(before.decision.state, "hold")
            self.assertIsInstance(before.decision.head_room, gate.NoHeadRoom)
            self.write_config(head_seconds="300")
            after_policy = gate.read_memory_config().policy
            inputs = fixture_queue_inputs(gate.admission_dir(self.root), gate.process_table(self.root / "proc"), NOW_EPOCH, after_policy)
            after = gate.admission_pass(inputs, NOW_EPOCH, gate.resolve_admission_limits(60 * GIB, gate.read_memory_config()), after_policy).rows[0]
        self.assertEqual(after_policy.timing.head_seconds, 300)
        self.assertEqual(after.decision.state, "admit")
        self.assertFalse(after.decision.free_passes)
        self.assertIsInstance(after.reason, gate.HeadAdmissionStatus)
        assert isinstance(after.reason, gate.HeadAdmissionStatus)
        self.assertIsInstance(after.reason.backfill, gate.StrictUntil)
        self.assertEqual(before_policy.timing.head_seconds, 600)

    def test_warning_boundary_reports_each_fallback_once_and_checker_stays_silent(self) -> None:
        self.write_config(head_seconds="abc", ramp_seconds=None)
        with patch.object(gate, "_warned_configuration_diagnostics", set[gate.ConfigurationDiagnostic]()), contextlib.redirect_stderr(io.StringIO()) as errors:
            first = gate.read_memory_config(report=False)
            second = gate.read_memory_config(report=False)
            gate.warn_configuration_diagnostics(first.diagnostics)
            gate.warn_configuration_diagnostics(second.diagnostics)
        self.assertEqual(errors.getvalue().count("head_seconds='abc'"), 1)
        self.assertEqual(errors.getvalue().count("ramp_seconds='<missing>'"), 1)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            checked = self.pass_for(400, need=8, free=0)
        self.assertIn("head_seconds", errors.getvalue())
        self.assertEqual(checked.decision.state, "hold")
        self.assertEqual(gate.read_memory_config().policy.timing.head_seconds, 600)


class ServiceAdmissionRuleTests(unittest.TestCase):
    def test_reservation_stem_keeps_owner_identity_and_admission_time(self) -> None:
        directory = Path("/tmp/private-admission")
        first = gate.reservation_stem(directory, 123, "456", 789)
        second = gate.reservation_stem(directory, 123, "456", 790)
        self.assertEqual(first, directory / "123-456-789")
        self.assertNotEqual(first, second)
        self.assertEqual(first.with_suffix(".max").name, "123-456-789.max")
        self.assertEqual(first.with_suffix(".wave").name, "123-456-789.wave")

    def test_fixed_cap_estimate_never_reads_history_or_changes_its_basis(self) -> None:
        profile = gate.ProgramProfile("merge-gate", "repo", "tree", gate.FixedCapSizing(6 * GIB))
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(gate, "program_peak_estimate", side_effect=AssertionError("cap read history")):
            result = gate.step_estimate([], "tree", profile, NOW, Path(folder), admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)
        self.assertEqual(result.profile, profile)
        self.assertEqual(result.profile.peak, gate.ExpectedPeak(6 * GIB, "cap", 0, gate.ProgramMemoryCap("merge-gate")))
        self.assertIsInstance(result.cache, gate.ReusableEstimate)

    def test_history_program_estimate_refreshes_only_after_its_policy_age(self) -> None:
        policy = admission_policy_fixture()
        profile = gate.ProgramProfile("rust-analyzer", "repo", "tree",
            gate.HistoryPeakSizing(18 * GIB, gate.FirstProgramPeak(18 * GIB), NOW_EPOCH))
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(gate, "program_peak_estimate", return_value=gate.MeasuredProgramPeak(7 * GIB, 9)) as estimate:
            young = gate.step_estimate([], "tree", profile,
                NOW + timedelta(seconds=policy.timing.head_seconds - 1), Path(folder), policy, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)
            estimate.assert_not_called()
            aged = gate.step_estimate([], "tree", profile,
                NOW + timedelta(seconds=policy.timing.head_seconds), Path(folder), policy, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)
        self.assertEqual(young.profile, profile)
        self.assertEqual(aged.profile.peak.bytes, 7 * GIB)
        self.assertEqual(estimate.call_count, 1)

    def test_fresh_estimate_keeps_a_fractional_running_pass_interval(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(running_pass_seconds=0.5)
            policy = gate.read_memory_config().policy
            row: gate.Waiter = {"pid": 123, "start_time": "owner", "need": GIB,
                "since": NOW_EPOCH, "estimated_at": NOW_EPOCH, "fresh_test": True}
            self.assertFalse(gate.estimate_expired(row, NOW_EPOCH, policy))
            self.assertFalse(gate.estimate_expired(row, NOW_EPOCH + 0.25, policy))
            self.assertTrue(gate.estimate_expired(row, NOW_EPOCH + 0.5, policy))
            row["fresh_test"] = False
            self.assertFalse(gate.estimate_expired(row, NOW_EPOCH + 0.5, policy))
            row.update(need_source="cap", fresh_test=True)
            self.assertFalse(gate.estimate_expired(row, NOW_EPOCH + policy.timing.head_seconds, policy))

    def test_fresh_nextest_estimate_carries_its_cache_policy_to_expiry(self) -> None:
        for fresh in (False, True):
            with self.subTest(fresh=fresh), tempfile.TemporaryDirectory() as folder, \
                 patch.object(gate, "git_identity", return_value=(HANA, "tree")), \
                 patch.object(gate, "is_fresh_test", return_value=fresh), \
                 patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(2 * GIB, "measured", 5)):
                result = gate.step_estimate(["cargo", "nextest", "run"], "tree",
                    gate.InferFromArgv(), NOW, Path(folder), admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"], budget=admission_configuration_fixture().build_budget_bytes)
                self.assertIsInstance(result.cache, gate.FreshTest if fresh else gate.ReusableEstimate)
                row: gate.Waiter = {"pid": 123, "start_time": "owner", "need": result.profile.peak.bytes,
                    "since": NOW_EPOCH, "estimated_at": NOW_EPOCH, "fresh_test": fresh}
                interval = admission_policy_fixture().timing.fresh_test_seconds
                self.assertFalse(gate.estimate_expired(row, NOW_EPOCH + interval - 1, admission_policy_fixture()))
                self.assertEqual(gate.estimate_expired(row, NOW_EPOCH + interval, admission_policy_fixture()), fresh)
                row["need_source"] = "cap"
                self.assertFalse(gate.estimate_expired(row,
                    NOW_EPOCH + admission_policy_fixture().timing.head_seconds + 1,
                    admission_policy_fixture()))


class CiSliceMemoryTests(unittest.TestCase):
    root: Path = Path()
    ci: Path = Path()
    proc: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = fake_memory_readings(self.root)
        write_memory_config(self.root / "build_admission.conf", ci_memory_margin_gib="6")
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))
        self.ci = self.root / "ci.slice"
        self.proc = self.root / "proc"
        (self.proc / "123").mkdir(parents=True)
        write_process(self.proc, 123)
        _ = (self.proc / "123/comm").write_text("Runner.Listener\n")
        _ = (self.ci / "cgroup.procs").write_text("123\n")
        _ = (self.ci / "memory.current").write_text(str(3 * GIB))
        _ = (self.ci / "memory.max").write_text(str(18 * GIB))

    def reading(self) -> gate.UnownedSliceMemory:
        config = gate.read_memory_config()
        reading, = gate.unowned_slice_memory([], gate.process_table(self.proc), self.proc, ci_memory_slice=config.ci_memory_slice,
                                              ci_memory_margin_bytes=config.ci_memory_margin_bytes)
        return reading

    def test_idle_ci_charges_resident_memory_and_records_swap_separately(self) -> None:
        resident = int(0.09 * GIB)
        swapped = int(1.98 * GIB)
        _ = (self.ci / "memory.current").write_text(str(resident))
        _ = (self.ci / "memory.swap.current").write_text(str(swapped))
        reading = self.reading()
        self.assertEqual(reading.bytes, resident)
        self.assertEqual(gate.build_charge(reading), resident)
        self.assertEqual(reading.swap_bytes, swapped)
        self.assertEqual(gate.growth_headroom(reading), 0)

    def test_empty_ci_slice_allows_the_sole_oversized_step(self) -> None:
        _ = (self.ci / "cgroup.procs").write_text("")
        _ = (self.ci / "memory.current").write_text("0")
        reading = self.reading()
        decision = gate.decide(60 * GIB, 64 * GIB, 22 * GIB, [reading],
                               limits=gate.AdmissionLimits(21 * GIB, 0))
        self.assertEqual(decision.state, "admit")

    def test_idle_ci_with_ram_or_live_processes_keeps_the_budget_limit(self) -> None:
        for members, amount in (("", GIB), ("123\n", 0), ("123\n", GIB)):
            with self.subTest(members=members, amount=amount):
                _ = (self.ci / "cgroup.procs").write_text(members)
                _ = (self.ci / "memory.current").write_text(str(amount))
                reading = self.reading()
                decision = gate.decide(60 * GIB, 64 * GIB, 22 * GIB, [reading],
                                       limits=gate.AdmissionLimits(21 * GIB, 0))
                self.assertEqual(decision.state, "hold")
                self.assertEqual(decision.charged, amount)

    def test_idle_ci_excludes_inactive_file_cache_and_records_swap_separately(self) -> None:
        _ = (self.ci / "memory.current").write_text(str(10 * GIB))
        _ = (self.ci / "memory.stat").write_text(f"anon 1024\nfile {9 * GIB}\ninactive_file {8 * GIB}\n")
        _ = (self.ci / "memory.swap.current").write_text(str(GIB))
        reading = self.reading()
        self.assertEqual(reading.bytes, 2 * GIB)
        self.assertEqual(gate.build_charge(reading), 2 * GIB)
        self.assertEqual(reading.swap_bytes, GIB)
        _ = (self.ci / "memory.stat").write_text(f"inactive_file {11 * GIB}\n")
        self.assertEqual(self.reading().bytes, 0)

    def test_unowned_build_scope_uses_the_same_working_set(self) -> None:
        scope = self.root / "builds.slice/unowned.scope"
        scope.mkdir(parents=True)
        _ = (scope / "memory.current").write_text(str(10 * GIB))
        _ = (scope / "memory.stat").write_text(f"inactive_file {8 * GIB}\n")
        _ = (scope / "memory.swap.current").write_text(str(GIB))
        _ = (scope / "cgroup.procs").write_text("")
        reading, = gate.unowned_slice_memory([], {}, self.proc)
        self.assertEqual(reading.bytes, 2 * GIB)
        self.assertEqual(gate.build_charge(reading), 2 * GIB)
        self.assertEqual(reading.swap_bytes, GIB)
        (scope / "memory.stat").unlink()
        self.assertEqual(gate.unowned_slice_memory([], {}, self.proc), ())

    def test_running_job_adds_margin_to_the_working_set(self) -> None:
        _ = (self.proc / "123/comm").write_text("Runner.Worker\n")
        _ = (self.ci / "memory.current").write_text(str(10 * GIB))
        _ = (self.ci / "memory.stat").write_text(f"inactive_file {8 * GIB}\n")
        _ = (self.ci / "memory.swap.current").write_text(str(GIB))
        reading = self.reading()
        self.assertEqual(reading.bytes, 2 * GIB)
        self.assertEqual(reading.promise, gate.LifetimePromise(8 * GIB))
        self.assertEqual(gate.build_charge(reading), 8 * GIB)
        _ = (self.ci / "memory.max").write_text(str(5 * GIB))
        self.assertEqual(gate.build_charge(self.reading()), 5 * GIB)

    def test_unreadable_or_invalid_memory_stat_refuses_the_reading(self) -> None:
        stat = self.ci / "memory.stat"
        stat.unlink()
        with self.assertRaisesRegex(gate.SliceMemoryUnavailable, "hana-ci.slice.*memory.stat"):
            _ = self.reading()
        for content in ("anon 1024\n", "inactive_file invalid\n"):
            with self.subTest(content=content):
                _ = stat.write_text(content)
                with self.assertRaisesRegex(gate.SliceMemoryUnavailable, "hana-ci.slice.*memory.stat"):
                    _ = self.reading()

    def test_running_job_reserves_the_margin_and_free_memory_growth(self) -> None:
        _ = (self.proc / "123/comm").write_text("Runner.Worker\n")
        reading = self.reading()
        self.assertEqual(reading.bytes, 3 * GIB)
        self.assertEqual(reading.promise, gate.LifetimePromise(9 * GIB))
        self.assertEqual(gate.build_charge(reading), 9 * GIB)
        self.assertEqual(gate.growth_headroom(reading), 6 * GIB)
        decision = gate.decide(60 * GIB, 64 * GIB, 7 * GIB, [reading],
                               limits=gate.AdmissionLimits(24 * GIB, 0))
        self.assertEqual(decision.state, "admit")

    def test_slice_limit_is_read_again_for_each_measurement(self) -> None:
        _ = (self.proc / "123/comm").write_text("Runner.Worker\n")
        self.assertEqual(gate.build_charge(self.reading()), 9 * GIB)
        _ = (self.ci / "memory.max").write_text(str(5 * GIB))
        self.assertEqual(gate.build_charge(self.reading()), 5 * GIB)

    def test_margin_is_read_from_config_for_each_measurement(self) -> None:
        _ = (self.proc / "123/comm").write_text("Runner.Worker\n")
        self.assertEqual(gate.build_charge(self.reading()), 9 * GIB)
        write_memory_config(self.root / "build_admission.conf", ci_memory_margin_gib="2")
        self.assertEqual(gate.build_charge(self.reading()), 5 * GIB)
        write_memory_config(self.root / "build_admission.conf", ci_memory_margin_gib="0")
        self.assertEqual(gate.build_charge(self.reading()), 3 * GIB)

    def test_active_ci_margin_excludes_swap_and_preserves_resident_use(self) -> None:
        _ = (self.proc / "123/comm").write_text("Runner.Worker\n")
        _ = (self.ci / "memory.swap.current").write_text(str(GIB))
        self.assertEqual(gate.build_charge(self.reading()), 9 * GIB)
        _ = (self.ci / "memory.max").write_text(str(3 * GIB))
        self.assertEqual(gate.build_charge(self.reading()), 3 * GIB)
        self.assertEqual(gate.growth_headroom(self.reading()), 0)

    def test_missing_or_unbounded_limit_refuses_the_reading(self) -> None:
        for content in (None, "max"):
            with self.subTest(content=content):
                limit = self.ci / "memory.max"
                if content is None:
                    limit.unlink()
                else:
                    _ = limit.write_text(content)
                with self.assertRaisesRegex(gate.SliceMemoryUnavailable, "hana-ci.slice.*memory.max"):
                    _ = self.reading()

    def test_systemd_supplies_the_slice_path(self) -> None:
        with patch.dict(os.environ, {"BUILDLOG_CI_CGROUP": ""}), patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "/hana.slice/hana-ci.slice\n", "")) as run:
            self.assertEqual(gate.ci_slice_cgroup("hana-ci.slice"), Path("/sys/fs/cgroup/hana.slice/hana-ci.slice"))
        self.assertEqual(run.call_args.args[0],
                         ["systemctl", "show", "--property=ControlGroup", "--value", "hana-ci.slice"])

    def test_empty_slice_name_cannot_disable_ci_accounting(self) -> None:
        write_memory_config(self.root / "build_admission.conf", ci_memory_slice="")
        with self.assertRaisesRegex(conf.Unusable, "ci_memory_slice"):
            _ = gate.read_memory_config()


if __name__ == "__main__":
    _ = unittest.main()
