#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Memory admission with only temporary history, cgroups, and ledgers."""

from __future__ import annotations

from scripts.build_queue import admission_records as records, snapshot_codec as codec, step_estimator

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
from functools import cached_property
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal, cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo


from scripts.lint import memory_admit as gate, tmp_sweep
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.lib.build_steps import read_step_names
from scripts.lib import conf
from scripts.build_queue.scope_activity import IdleService, RunningServiceJob, LiveBuildRun, NoLiveBuildRun
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service as service, queue_view, queue_evidence
from scripts.build_queue.admission_test_support import queue_decision, AdmissionFixture, InjectedCrash, only_admitted
from scripts.build_queue.queue_fixture import write_memory_config as write_memory_config, fixture_queue_inputs, fixture_reservation, write_process, ci_step
from scripts.lint import nextest_budget as budget
from scripts.lint.build_shape import BuildShape, CompiledCrates, build_shape
from scripts.lint.memory_test_support import fake_memory_readings, gate_isolation_environment, write_config_fixture, write_config_fault, commit_config_fixture
from scripts.buildlog import store
from scripts.buildlog import record as buildlog_record
from scripts.buildlog import index as file_marks
from scripts.lib import test_hang_guard
from scripts.message.roster import UnitLabels

READY_FOR_ADMISSION = gate.ReadyForAdmission()


class SnapshotCompatibilityTests(unittest.TestCase):
    def test_older_record_uses_field_default(self) -> None:
        self.assertEqual(codec.parse_snapshot_value(
            {"kind": "MeasuredMemory", "bytes": 12}, gate.MeasuredMemory),
            gate.MeasuredMemory(12))

    def test_memory_room_without_ci_use_decodes_with_zero_ci_use(self) -> None:
        room = gate.memory_room(gate.HostMemory(20 * GIB, 64 * GIB), (), admission_policy_fixture())
        assert isinstance(room, gate.MemoryRoom)
        record = cast(dict[str, object], codec.snapshot_value(room))
        del record["ci_use"]
        decoded = codec.parse_snapshot_value(record, gate.MemoryRoom)
        assert isinstance(decoded, gate.MemoryRoom)
        self.assertEqual(decoded.ci_use, 0)
        self.assertEqual(decoded, room)

    def test_newer_record_ignores_unknown_field(self) -> None:
        self.assertEqual(codec.parse_snapshot_value(
            {"kind": "MeasuredMemory", "bytes": 12, "future": {"kind": "UnknownRecord"}},
            gate.MeasuredMemory), gate.MeasuredMemory(12))

    def test_older_record_uses_default_factory(self) -> None:
        self.assertEqual(codec.parse_snapshot_value(
            {"kind": "PeakHold", "default_seconds": 30}, gate.PeakHold), gate.PeakHold(30))

    def test_missing_required_field_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid snapshot record fields"):
            _ = codec.parse_snapshot_value({"kind": "MeasuredMemory"}, gate.MeasuredMemory)

    def test_wrong_or_missing_tag_is_refused(self) -> None:
        for record in ({"bytes": 12}, {"kind": "GoneMemory", "bytes": 12}):
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, "invalid snapshot record tag"):
                _ = codec.parse_snapshot_value(record, gate.MeasuredMemory)

    def test_nested_records_use_defaults_and_ignore_unknown_fields(self) -> None:
        record: dict[str, object] = {
            "kind": "RunningStep", "session_id": "session", "session_name": "worker",
            "step": "nextest", "folder": "/tmp/tree", "promise": 12,
            "pid": {"kind": "PidNotRecorded"},
            "measured": {"kind": "MeasuredMemory", "bytes": 5, "future": False},
            "future": {"kind": "UnknownRecord"},
        }
        self.assertEqual(codec.parse_snapshot_value(record, gate.RunningStep),
                         gate.RunningStep("session", "worker", "nextest", "/tmp/tree", 12,
                                          gate.MeasuredMemory(5), pid=gate.PidNotRecorded()))

    def test_former_schema_shims_use_dataclass_defaults(self) -> None:
        records: tuple[object, ...] = (
            gate.StepProfile("repo", "nextest", "tree", gate.ExpectedPeak(12, "measured", 1)),
            gate.MemoryPressure(8),
            gate.RunningStep("session", "worker", "nextest", "/tmp/tree", 12, gate.MeasuredMemory(5), pid=gate.PidNotRecorded()),
            gate.UnownedSliceMemory(5, pid=gate.PidNotRecorded()),
        )
        omitted = ("build_history_generation", "condition", "run", "started_at", "activity")
        for original in records:
            with self.subTest(schema=type(original).__name__):
                record = {name: value for name, value in
                          cast(dict[str, object], codec.snapshot_value(original)).items() if name not in omitted}
                record["future"] = "ignored"
                self.assertEqual(codec.parse_snapshot_value(record, type(original)), original)

    def test_nested_required_field_and_tag_are_checked(self) -> None:
        for memory in ({"kind": "MeasuredMemory"}, {"bytes": 5}, {"kind": "UnknownRecord", "bytes": 5}):
            with self.subTest(memory=memory), self.assertRaises(ValueError):
                _ = codec.parse_snapshot_value({
                    "kind": "RunningStep", "session_id": "session", "session_name": "worker",
                    "step": "nextest", "folder": "/tmp/tree", "promise": 12, "measured": memory,
                    "pid": {"kind": "PidNotRecorded"},
                }, gate.RunningStep)

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


def assert_history_decoded_once[Result](test: unittest.TestCase, path: Path,
                                        read: Callable[[], Result]) -> Result:
    with patch.object(step_estimator, "anonymous_history_rows", wraps=step_estimator.anonymous_history_rows) as decode:
        result = read()
    test.assertEqual(decode.call_count, 1)
    test.assertEqual(decode.call_args_list[0].args, (path,))
    return result


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
        _ = write_config_fixture(self.config, "build_budget_gib=21")
        commit_config_fixture(self.config)


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


class CloudFinalizationTests(unittest.TestCase):
    def test_release_removes_cloud_progress_and_module_counter_files(self) -> None:
        from scripts.build_queue import cloud_progress

        with AdmissionFixture() as fixture:
            sidecar = fixture.root / "cloud.cgroup"
            _ = sidecar.write_text("")
            request = replace(fixture.request(step="merge-gate"),
                place=wire.CloudPlace("aws"), sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            cloud_progress.publish(sidecar, cloud_progress.CloudProgress("running", 16, 0, fixture.clock.now()))
            progress.publish_counter(sidecar, progress.ProgressCounter("modules", 4, 10))
            cloud_file = Path(str(sidecar) + ".cloud.json")
            self.assertTrue(cloud_file.exists())
            fixture.release(grant)
            for path in (sidecar, cloud_file, progress.progress_path(sidecar, "json")):
                with self.subTest(path=path.name):
                    self.assertFalse(path.exists())
            self.assertFalse(fixture.server.reservations)


class MemoryRoomRulesTests(unittest.TestCase):
    def test_room_and_keep_aside_never_become_negative(self) -> None:
        policy = admission_policy_fixture()
        for used, kept, room in ((0, 12, 0), (2, 10, 0), (12, 0, 10), (14, 0, 10)):
            with self.subTest(used=used):
                reading = gate.memory_room(gate.HostMemory(13 * GIB, 60 * GIB), [gate.RunMemory("run", 12 * GIB, gate.AnonymousRunUse(used * GIB))], policy)
                assert isinstance(reading, gate.MemoryRoom)
                self.assertEqual(reading.kept, kept * GIB)
                self.assertEqual(reading.room, room * GIB)
                self.assertEqual(reading.runs[0].bytes, kept * GIB)

    def test_room_uses_free_memory_minus_safety_and_each_runs_remaining_need(self) -> None:
        reading = gate.memory_room(gate.HostMemory(20 * GIB, 64 * GIB), [
            gate.RunMemory("first", 8 * GIB, gate.AnonymousRunUse(3 * GIB)),
            gate.RunMemory("second", 6 * GIB, gate.AnonymousRunUse(2 * GIB)),
        ], admission_policy_fixture())
        assert isinstance(reading, gate.MemoryRoom)
        self.assertEqual(reading.used, 5 * GIB)
        self.assertEqual(reading.kept, 9 * GIB)
        self.assertEqual(reading.room, 8 * GIB)
        self.assertEqual(reading.cap, 22 * GIB)

    def test_ci_use_increases_the_cap_without_changing_room_or_admission(self) -> None:
        policy = admission_policy_fixture()
        run = gate.RunningStep("run", "run", "check", "/run", 8 * GIB,
            gate.MeasuredMemory(3 * GIB, anonymous=gate.AnonymousRunUse(3 * GIB)), pid=100)
        ci = ci_step(4 * GIB, RunningServiceJob(NOW_EPOCH - 60))
        reading = ci.measured
        assert isinstance(reading, gate.UnownedSliceMemory)
        baseline_ci = replace(ci, measured=replace(reading, bytes=0))
        for need, state in ((5, "admit"), (6, "hold")):
            with self.subTest(need=need):
                waiter: gate.Waiter = {"pid": 200, "start_time": "200", "need": need * GIB,
                                       "since": NOW_EPOCH}
                inputs = gate.QueueInputs([(Path("200.waiting"), waiter)], (run, ci),
                    (fixture_reservation(100, 8 * GIB),), gate.HostMemory(20 * GIB, 64 * GIB),
                    gate.NoPressureHold(), gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), NOW_EPOCH)
                baseline = replace(inputs, running=(run, baseline_ci))
                before = gate.admission_pass(baseline, NOW_EPOCH, policy).rows[0].decision
                after = gate.admission_pass(inputs, NOW_EPOCH, policy).rows[0].decision
                self.assertEqual(after.state, state)
                self.assertEqual(after, before)

    def test_ci_keep_aside_stays_separate_from_run_use_and_run_holds(self) -> None:
        reading = gate.memory_room(gate.HostMemory(20 * GIB, 64 * GIB), [gate.RunMemory("run", 8 * GIB, gate.AnonymousRunUse(3 * GIB))], admission_policy_fixture(), ci_keep_aside=gate.CIKeepAside(7 * GIB))
        assert isinstance(reading, gate.MemoryRoom)
        self.assertEqual(reading.used, 3 * GIB)
        self.assertEqual(reading.kept, 12 * GIB)
        self.assertEqual(reading.room, 5 * GIB)
        self.assertEqual(reading.cap, 20 * GIB)
        self.assertEqual(len(reading.runs), 1)
        self.assertEqual(reading.runs[0].bytes, 5 * GIB)
        self.assertEqual(reading.ci_keep_aside.bytes, 7 * GIB)

    def test_negative_room_shows_zero_and_keeps_the_exact_shortfall(self) -> None:
        reading = gate.memory_room(gate.HostMemory(5 * GIB, 64 * GIB), [gate.RunMemory("run", 18 * GIB, gate.AnonymousRunUse(10 * GIB))], admission_policy_fixture())
        assert isinstance(reading, gate.MemoryRoom)
        self.assertEqual(reading.room, 0)
        self.assertEqual(reading.shortfall, 6 * GIB)
        self.assertEqual(reading.cap, 12 * GIB)
        self.assertEqual(reading.kept, 8 * GIB)

    def test_initial_total_charge_is_need_not_need_plus_use(self) -> None:
        reading = gate.memory_room(gate.HostMemory(20 * GIB, 64 * GIB), [gate.RunMemory("run", 12 * GIB, gate.AnonymousRunUse(5 * GIB))], admission_policy_fixture())
        assert isinstance(reading, gate.MemoryRoom)
        self.assertEqual(reading.used + reading.kept, 12 * GIB)
        self.assertEqual(reading.kept, 7 * GIB)

    def test_missing_anonymous_use_keeps_the_full_need_and_marks_the_cap_unavailable(self) -> None:
        missing = gate.RunUseUnavailable("scope memory.stat is missing")
        reading = gate.memory_room(gate.HostMemory(20 * GIB, 64 * GIB), [gate.RunMemory("run", 12 * GIB, missing)], admission_policy_fixture())
        assert isinstance(reading, gate.MemoryRoom)
        self.assertEqual(reading.kept, 12 * GIB)
        self.assertEqual(reading.room, 5 * GIB)
        self.assertEqual(reading.runs[0].memory.use, missing)
        self.assertIsInstance(reading.cap, gate.CapUnavailable)

    def test_a_gap_keeps_its_bytes_without_subtracting_another_steps_use(self) -> None:
        reading = gate.memory_room(gate.HostMemory(20 * GIB, 64 * GIB), [gate.RunMemory("run", 8 * GIB, gate.AnonymousRunUse(3 * GIB), held=6 * GIB)], admission_policy_fixture())
        assert isinstance(reading, gate.MemoryRoom)
        self.assertEqual(reading.kept, 11 * GIB)
        self.assertEqual(reading.room, 6 * GIB)
        self.assertEqual(reading.used, 3 * GIB)

    def test_unavailable_host_memory_cannot_publish_room(self) -> None:
        missing = gate.ReadingUnavailable("meminfo is missing")
        reading = gate.memory_room(missing, [], admission_policy_fixture())
        self.assertEqual(reading, missing)

    def test_overlapping_grants_count_each_anonymous_scope_once(self) -> None:
        from scripts.build_queue.scope_activity import LiveBuildRun
        run = LiveBuildRun(900, "900", NOW_EPOCH - 60)
        for scopes in (("/run", "/run"), ("/run", "/run/child"), ("/run/child", "/run")):
            with self.subTest(scopes=scopes):
                rows: list[gate.Reservation] = []
                steps: list[gate.RunningStep] = []
                for index, scope in enumerate(scopes, 1):
                    amount = 4 * GIB if scope == "/run" else GIB
                    rows.append({"pid": index, "start_time": str(index), "need": 6 * GIB,
                        "repo": "test", "step": "nextest", "worktree": "/tree", "admitted_at": "",
                        "sidecar": "", "test_threads": gate.NoTestWidth(), "call_id": gate.NoCallRecord(),
                        "build_run": run})
                    steps.append(gate.RunningStep("run", "run", "nextest", "/tree", 6 * GIB,
                        gate.MeasuredMemory(amount, anonymous=gate.AnonymousRunUse(amount), anonymous_source=gate.ScopeSource(Path(scope), frozenset())), run=run, pid=index))
                inputs = gate.QueueInputs([], tuple(steps), tuple(rows), gate.HostMemory(20 * GIB, 64 * GIB),
                    gate.NoPressureHold(), gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), NOW_EPOCH)
                reading = gate.queue_read_from(inputs, admission_policy_fixture())
                room = reading.explanation.room
                assert isinstance(room, gate.MemoryRoom)
                self.assertEqual(room.used, 4 * GIB)
                self.assertEqual(room.kept, 8 * GIB)
                self.assertEqual(room.room, 9 * GIB)


class WaitHolderRulesTests(unittest.TestCase):
    ZERO_ANONYMOUS_USE: records.AnonymousRunUse = records.AnonymousRunUse(0)
    NO_LIVE_RUN: NoLiveBuildRun = NoLiveBuildRun()

    def waiter(self, pid: int, *, session: str = "waiting", unit: str = "", need: int = 20 * GIB) -> records.Waiter:
        return {"pid": pid, "start_time": str(pid), "need": need, "since": NOW_EPOCH,
                "session_id": session, "session_name": session, "unit": unit,
                "step": "check", "folder": "/tree"}

    def inputs(self, *steps: records.RunningStep) -> records.QueueInputs:
        reservations: list[records.Reservation] = []
        for step in steps:
            if not gate.running_has_reservation(step):
                continue
            assert isinstance(step.pid, int)
            row = fixture_reservation(step.pid, step.promise, step=step.step, folder=step.folder)
            row.update(session_id=step.session_id, session_name=step.session_name,
                       unit=step.unit, build_run=step.run)
            reservations.append(row)
        return records.QueueInputs([(Path("waiting"), self.waiter(999))], tuple(steps), tuple(reservations),
            records.HostMemory(4 * GIB, 64 * GIB), records.NoPressureHold(), records.ReadyToStart(),
            records.NoHeadAdmissionOverride(), NOW_EPOCH)

    def running(self, pid: int, need: int, *, session: str = "runner", unit: str = "",
                use: records.AnonymousRunUse | records.RunUseUnavailable = ZERO_ANONYMOUS_USE,
                run: LiveBuildRun | NoLiveBuildRun = NO_LIVE_RUN) -> records.RunningStep:
        return records.RunningStep(session, session, "check", "/tree", need,
            records.MeasuredMemory(0, anonymous=use), run=run, pid=pid, unit=unit)

    def test_room_wait_names_the_largest_grouped_run_instead_of_the_largest_step(self) -> None:
        run = LiveBuildRun(900, "900", NOW_EPOCH - 60)
        inputs = self.inputs(self.running(1, 5 * GIB, session="group", run=run),
                             self.running(2, 5 * GIB, session="group", run=run),
                             self.running(3, 8 * GIB, session="single"))
        result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
        self.assertEqual(result.rows[0].holder, records.SessionHolder("group", "group"))
        self.assertEqual(result.rows[0].decision.state, "hold")

    def test_room_wait_uses_the_progress_charge_instead_of_the_full_reservation(self) -> None:
        inputs = self.inputs(self.running(1, 20 * GIB, session="late"),
                             self.running(2, 6 * GIB, session="larger"))
        first = inputs.running[0]
        reading = records.ProgressAdjustedMemory(0, anonymous=records.AnonymousRunUse(0),
            counter=progress.ProgressCounter("build:1", 90, 100),
            window=progress.WindowPeak(GIB, 20), remaining_need=GIB)
        inputs = replace(inputs, running=(replace(first, measured=reading), inputs.running[1]))
        result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
        self.assertEqual(result.rows[0].holder, records.SessionHolder("larger", "larger"))

    def test_unavailable_run_use_keeps_its_full_charge_when_selecting_the_holder(self) -> None:
        inputs = self.inputs(self.running(1, 12 * GIB, session="unreadable",
            use=records.RunUseUnavailable("anonymous reading missing")),
            self.running(2, 8 * GIB, session="measured", use=records.AnonymousRunUse(7 * GIB)))
        result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
        self.assertEqual(result.rows[0].holder, records.SessionHolder("unreadable", "unreadable"))

    def test_a_running_unit_without_a_session_is_the_holder(self) -> None:
        inputs = self.inputs(self.running(1, 8 * GIB, session="", unit="compiler"))
        result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
        self.assertEqual(result.rows[0].holder, records.UnitHolder("compiler"))

    def test_a_waiter_behind_the_protected_head_names_that_head(self) -> None:
        inputs = self.inputs(self.running(1, 8 * GIB, session="unrelated"))
        inputs = replace(inputs, ordered=[(Path("head"), self.waiter(100, session="head")),
                                         (Path("tail"), self.waiter(200, session="tail"))])
        result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
        self.assertEqual(result.rows[1].holder, records.SessionHolder("head", "head"))
        self.assertIsInstance(result.rows[1].decision.order_hold, records.HeldBehindHead)

    def test_a_protected_head_without_a_session_names_its_unit(self) -> None:
        inputs = self.inputs(self.running(1, 8 * GIB))
        inputs = replace(inputs, ordered=[(Path("head"), self.waiter(100, session="", unit="head-unit")),
                                         (Path("tail"), self.waiter(200))])
        result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
        self.assertEqual(result.rows[1].holder, records.UnitHolder("head-unit"))

    def test_a_continuation_extra_protects_its_waiter_before_the_ordinary_head(self) -> None:
        run = LiveBuildRun(900, "900", NOW_EPOCH - 60)
        head = self.waiter(100, session="ordinary")
        continuation = self.waiter(200, session="continuation", need=10 * GIB)
        continuation["build_run"] = run
        inputs = self.inputs(self.running(1, GIB, session="other"))
        slot = records.BetweenStepsSlot(run, "previous", 2 * GIB, NOW_EPOCH + 10)
        inputs = replace(inputs, ordered=[(Path("head"), head), (Path("continuation"), continuation)],
            slots=(records.ContinuationSlot(slot, records.waiter_owner(continuation)),))
        result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
        ordinary = next(row for row in result.rows if row.owner == records.waiter_owner(head))
        self.assertEqual(ordinary.holder, records.SessionHolder("continuation", "continuation"))

    def test_pressure_unreadable_reading_spacing_and_refresh_name_no_holder(self) -> None:
        inputs = self.inputs(self.running(1, 12 * GIB))
        expired = self.waiter(999)
        expired["estimated_at"] = NOW_EPOCH - 10000
        cases = (
            replace(inputs, pressure=records.MemoryPressure(10, 20, 0)),
            replace(inputs, pressure=records.PressureUnavailable("PSI missing")),
            replace(inputs, host=records.ReadingUnavailable("meminfo missing")),
            replace(inputs, spacing=records.NextStartAt(NOW_EPOCH + 5)),
            replace(inputs, ordered=[(Path("waiting"), expired)]),
        )
        for current in cases:
            with self.subTest(pressure=current.pressure, host=current.host, spacing=current.spacing,
                              waiter=current.ordered[0][1]):
                result = gate.admission_pass(current, current.now, admission_policy_fixture())
                self.assertEqual(result.rows[0].holder, records.NoHolder())
                self.assertEqual(result.rows[0].decision.state, "hold")

    def test_an_unregistered_ci_job_is_never_the_holder(self) -> None:
        ci = ci_step(30 * GIB, RunningServiceJob(NOW_EPOCH - 60))
        inputs = self.inputs(self.running(1, 4 * GIB, session="build"), ci)
        result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
        self.assertEqual(result.rows[0].holder, records.SessionHolder("build", "build"))

    def test_overlapping_processes_select_the_same_holder_in_both_reservation_orders(self) -> None:
        run = LiveBuildRun(900, "900", NOW_EPOCH - 60)
        shared = records.ProcessAnonymousUse(700, records.AnonymousRunUse(10 * GIB))
        steps = [self.running(1, GIB, session="shared", run=run),
                 self.running(2, GIB, session="shared", run=run)]
        steps = [replace(step, measured=records.MeasuredMemory(10 * GIB,
            anonymous=records.AnonymousRunUse(10 * GIB), anonymous_source=records.ProcessSource((shared,))))
            for step in steps]
        for ordered in (steps, list(reversed(steps))):
            with self.subTest(order=[step.pid for step in ordered]):
                inputs = self.inputs(*ordered, self.running(3, 12 * GIB, session="larger"))
                result = gate.admission_pass(inputs, inputs.now, admission_policy_fixture())
                self.assertEqual(result.rows[0].holder, records.SessionHolder("larger", "larger"))
                room = result.policy.room
                assert isinstance(room, records.MemoryRoom)
                self.assertEqual(room.used, 10 * GIB)


class DecisionTests(unittest.TestCase):
    def test_alone_covers_need_and_earlyoom_reserve(self) -> None:
        result = queue_decision(23 * GIB, 60 * GIB, 20 * GIB, [])
        self.assertEqual((result.state, result.threshold, result.reserve), ("admit", 23 * GIB, 3 * GIB))
        self.assertEqual(queue_decision(23 * GIB - 1, 60 * GIB, 20 * GIB, []).state, "hold")

    def test_measured_use_is_not_charged_again_but_remaining_need_is_kept(self) -> None:
        running: list[gate.StepMemory] = [gate.MeasuredMemory(3 * GIB, gate.LifetimePromise(3 * GIB), anonymous=gate.AnonymousRunUse(3 * GIB)), gate.PromisedMemory(2 * GIB)]
        result = queue_decision(10 * GIB, 60 * GIB, 5 * GIB, running)
        self.assertEqual((result.promised, result.measured, result.threshold, result.state),
                         (2 * GIB, 3 * GIB, 10 * GIB, "admit"))
        self.assertEqual(queue_decision(10 * GIB - 1, 60 * GIB, 5 * GIB, running).state, "hold")


    def test_no_total_has_no_reserve(self) -> None:
        result = queue_decision(5 * GIB, gate.UnknownHostTotal(), 5 * GIB, [])
        self.assertEqual((result.reserve, result.threshold, result.state), (3 * GIB, 8 * GIB, "hold"))


    def test_psi_11_holds_a_step_that_fits_memory_and_budget(self) -> None:
        decision = queue_decision(40 * GIB, 60 * GIB, GIB, [], pressure=gate.MemoryPressure(11, condition="full"))
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
        return gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])

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
                                  first_peak_bytes=config.program_first_peaks["cargo"])
        self.assertEqual(peak, gate.ExpectedPeak(int(4 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_measured_history_pools_every_width_unless_one_is_named(self) -> None:
        self.add_measured(5, test_threads=16)
        self.add_measured(5, test_threads=4, start_gib=6)
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, test_threads=16, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
            gate.ExpectedPeak(5 * GIB, "measured", 5),
        )
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
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
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope="filtered", first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
                         gate.ExpectedPeak(int(2 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_scratch_checkout_cannot_take_legacy_buildlog_samples_by_repository_name(self) -> None:
        self.add_index()
        scratch = gate.RepositoryIdentity(HANA.key, HANA.name)
        self.assertEqual(gate.expected_peak(scratch, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]).source,
                         "fallback")


class FilteredPromiseTests(unittest.TestCase):
    root: Path = Path()
    host: str = "test-host"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root)))
        (self.root / "admission").mkdir()

    def test_filtered_estimate_decodes_the_history_once_for_all_compile_steps(self) -> None:
        self.samples([3 * GIB], filters=("native_rate",))
        self.samples([6 * GIB], step="check")
        self.samples([8 * GIB], step="build")
        estimate = assert_history_decoded_once(
            self, self.root / "admission/anon_peaks.jsonl", self.promise)
        self.assertEqual(estimate, gate.ExpectedPeak(3 * GIB, "measured", 1))

    def test_shared_call_keeps_compile_results_and_nextest_selection_separate(self) -> None:
        rows: list[dict[str, object]] = [{
            "host": self.host, "repo": HANA.name, "repo_identity": HANA.key,
            "step": step, "call_id": "shared", "anon_peak_bytes": peak,
            "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
        } for step, peak in (("build", 8 * GIB), ("nextest", 3 * GIB))]
        write_history_rows(self.root, rows, indexed=True)
        arguments = ["cargo", "nextest", "run", "-E", "package(hana) & test(native_rate)"]
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as db, db:
            _ = db.execute("UPDATE steps SET crates_compiled=0 WHERE step='build'")
            _ = db.execute("UPDATE steps SET crates_compiled=7, argv=? WHERE step='nextest'",
                           (json.dumps(arguments),))
        history = gate.read_estimate_history(self.root, self.host, ("build", "nextest"), NOW)
        grouped = gate.compiling_history(history)
        self.assertEqual(grouped["build"], [])
        self.assertEqual(len(grouped["nextest"]), 1)
        item, evidence = grouped["nextest"][0]
        self.assertEqual(evidence.compilation, CompiledCrates(7))
        self.assertEqual(gate.history_selection(item, evidence),
                         gate.HistorySelection("hana", "filtered", ("native_rate",)))
        self.assertEqual(self.promise(), gate.ExpectedPeak(3 * GIB, "measured", 1))

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
            first_peak_bytes=config.program_first_peaks["cargo"])
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
                                    first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
        self.assertEqual((before.bytes, before.count), (19 * GIB, 14))
        self.samples([2 * GIB], scope="full")
        after = gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                                   first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
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
                                              first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
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
        peak = self.promise()
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
                                  first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
        self.assertEqual(peak, gate.ExpectedPeak(3 * GIB, "measured", 5))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sum(row["step"] == "merge-gate" for row in gate.anonymous_history_rows(path)), 10)

    def promise(self, filters: tuple[str, ...] = ("native_rate",), *,
                repository: gate.RepositoryIdentity = HANA, package: str = "hana",
                scope: gate.TestScope = "filtered") -> gate.ExpectedPeak:
        config = admission_configuration_fixture()
        return gate.expected_peak(repository, "nextest", self.host, NOW, self.root,
                                  scope=scope, package=package, filters=filters,
                                  first_peak_bytes=config.program_first_peaks["cargo"])

    def test_unfinished_rows_do_not_change_the_filtered_estimate(self) -> None:
        self.samples([3 * GIB], filters=("native_rate",))
        assert_unfinished_history_ignored(
            self, self.root / "admission/anon_peaks.jsonl",
            self.promise)

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
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
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
                               package=gate.test_package(argv), filters=gate.test_filters(argv), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
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
                               package=gate.test_package(argv), filters=gate.test_filters(argv), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
            gate.ExpectedPeak(12 * GIB, "fallback", 0),
        )

    def test_unscoped_filtered_run_uses_its_single_measured_peak(self) -> None:
        self.samples([2 * GIB], package="", filters=("native_rate",))
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root,
                               scope="filtered", filters=("native_rate",), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
            gate.ExpectedPeak(2 * GIB, "measured", 1),
        )

    def test_unscoped_filtered_run_keeps_its_own_peaks_below_the_pool(self) -> None:
        self.samples([2 * GIB] * 3, package="")
        other = gate.RepositoryIdentity("https://example.org/nate/other", "other", "other")
        self.samples([9 * GIB] * 5, package="", repository=other)
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope="filtered", first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
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
                                       scope="filtered", package="hana_diegetic", filters=filters, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
                    gate.ExpectedPeak(int(30 * GIB * gate.ANON_SHARE), "buildlog", 66),
                )
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
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
                               scope="filtered", package="hana", filters=("native_rate",), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"]),
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
                                  package="hana", filters=filters, fresh=self.fresh(key=key, scope=scope), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])

    def test_fresh_and_compile_estimates_share_the_precise_window_edge(self) -> None:
        now = NOW.replace(microsecond=123456)
        cutoff = now - timedelta(days=gate.HISTORY_DAYS)
        for crates in (0, 1):
            self.samples([3 * GIB], package="edge-package", crates=crates,
                         at=cutoff + timedelta(seconds=1))
            self.samples([19 * GIB], package="edge-package", crates=crates,
                         at=cutoff + timedelta(seconds=1))
        path = self.root / "admission/anon_peaks.jsonl"
        rows = list(gate.anonymous_history_rows(path))
        for row in rows:
            if row.get("package") == "edge-package":
                delta = 44 if row["anon_peak_bytes"] == 3 * GIB else -44
                row["ended_at"] = (cutoff + timedelta(microseconds=delta)).isoformat()
        _ = path.write_text("")
        write_history_rows(self.root, rows)
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                estimate = gate.expected_peak(
                    HANA, "nextest", self.host, now, self.root, scope="filtered",
                    package="edge-package", filters=("native_rate",), fresh=fresh,
                    first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
                self.assertEqual(estimate, gate.ExpectedPeak(3 * GIB, "measured", 1))

    def test_fresh_estimate_decodes_the_history_once(self) -> None:
        self.samples([2 * GIB])
        self.previous_build()
        estimate = assert_history_decoded_once(
            self, self.root / "admission/anon_peaks.jsonl", self.promise)
        self.assertEqual(estimate, gate.ExpectedPeak(2 * GIB, "measured", 1))

    def test_fresh_compile_fallback_reuses_the_decoded_history(self) -> None:
        self.previous_build()
        estimate = assert_history_decoded_once(
            self, self.root / "admission/anon_peaks.jsonl", self.promise)
        self.assertEqual(estimate, gate.ExpectedPeak(10 * GIB, "measured", 10))

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
                                         gate.read_estimate_history(self.root, self.host, ("nextest",), NOW)))

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
                    patch("scripts.buildlog.treekey.tree_key", return_value=ending_source), \
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
        with patch.dict(os.environ, {'BUILDLOG_TEST_BUILD_KEY': 'current-key', 'BUILDLOG_TREE': 'source-only-key', 'BUILDLOG_TEST_TARGET_DIR': str(configured), 'BUILDLOG_TEST_BUILD_DIR': str(configured), 'CARGO_TARGET_DIR': str(self.worktree / 'target')}), patch.object(step_estimator, 'git_identity', return_value=(HANA, 'checkout')), patch.object(store, 'host_name', return_value=self.host), patch.object(buildlog_record, 'git_directory', return_value=str(self.worktree)), patch.object(buildlog_record, 'git_facts', return_value={'worktree': str(self.worktree)}), patch.object(gate, 'process_start_time', return_value=None):
            fresh = gate.step_estimate(arguments, str(self.worktree), gate.INFER_FROM_ARGV, NOW, self.root, admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
            self.assertEqual(fresh.profile.peak.bytes, 2 * GIB)
            _ = (self.state / 'target-deletes.json').write_text(json.dumps({str(configured): (NOW - timedelta(seconds=30)).timestamp()}))
            swept = gate.step_estimate(arguments, str(self.worktree), gate.INFER_FROM_ARGV, NOW, self.root, admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
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
        return gate.expected_peak(repository, "nextest", self.host, NOW, self.root, scope=scope, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])

    def test_unrelated_repositories_named_repo_use_their_own_peaks(self) -> None:
        first = self.identity(self.checkout("one/repo", "git@example.org:nate/first.git"))
        second = self.identity(self.checkout("two/repo", "https://example.org/nate/second.git"))
        self.samples(first, 19)
        self.samples(second, 2)
        self.assertEqual(self.estimate(first), gate.ExpectedPeak(19 * GIB, "measured", 5))
        self.assertEqual(self.estimate(second), gate.ExpectedPeak(2 * GIB, "measured", 5))

    def test_repository_identity_uses_the_recorders_remote_rule(self) -> None:
        directory = self.checkout("project", "git@example.org:owner/project.git")
        with patch.object(gate.repository, "remote_identity", return_value="https://example.org/owner/project") as normalize:
            identity = self.identity(directory)
        self.assertEqual(identity.key, "https://example.org/owner/project")
        normalize.assert_called_once_with("git@example.org:owner/project.git", str(directory))

    def test_real_remote_forms_keep_the_repository_key_and_legacy_name(self) -> None:
        for folder, remote in (("one/project", "git@Example.org:owner/project.git"),
                               ("two/project", "https://example.org/owner/project.git")):
            with self.subTest(remote=remote):
                directory = self.checkout(folder, remote)
                self.assertEqual(self.identity(directory),
                                 gate.RepositoryIdentity("https://example.org/owner/project", "project", "project"))

    def test_real_repository_without_origin_keeps_the_root_commit_key_and_checkout_name(self) -> None:
        directory = self.checkout("project")
        root_commit = subprocess.check_output(["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(self.identity(directory), gate.RepositoryIdentity("git-root:" + root_commit, "project"))

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
        _ = self.enterContext(patch.object(step_estimator, "git_identity", return_value=(HANA, "test-worktree")))
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
        self.readings = {100: gate.MeasuredMemory(GIB, anonymous=gate.AnonymousRunUse(GIB)), 200: gate.MeasuredMemory(2 * GIB, anonymous=gate.AnonymousRunUse(2 * GIB))}
        _ = cast(object, self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root))))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root), "BUILDLOG_PROC": str(self.root / "proc"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        _ = self.enterContext(patch.object(gate, "process_start_time", side_effect=self.processes.get))
        def table(_proc: Path) -> dict[int, gate.ProcessIdentity]:
            return {pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()}
        _ = self.enterContext(patch.object(gate, "process_table", side_effect=table))
        def reading(pid: int, _start: str, _processes: dict[int, gate.ProcessIdentity],
                    _proc: Path) -> gate.MeasuredMemory | gate.GoneMemory:
            # Newly admitted live processes have not allocated memory yet.
            result = self.readings.get(pid, gate.MeasuredMemory(0, anonymous=gate.AnonymousRunUse(0)))
            scope = self.root / f"fixture-anonymous-{pid}"
            if scope.is_dir() and isinstance(result, gate.MeasuredMemory):
                return replace(result, anonymous_source=gate.ScopeSource(scope, frozenset()))
            return result
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
        if not sidecar:
            scope = self.root / f"fixture-anonymous-{pid}"
            scope.mkdir(exist_ok=True)
            sidecar = str(self.root / f"fixture-anonymous-{pid}.cgroup")
            _ = Path(sidecar).write_text(str(scope))
        row: dict[str, object] = {"pid": pid, "start_time": self.processes[pid], "need": int(need * GIB),
                                 "repo": "test", "step": "nextest", "worktree": "test", "sidecar": sidecar,
                                 "admitted_at": store.utc_iso(NOW.timestamp() - age),
                                 "test_threads": None, "call_id": None}
        path = gate.admission_dir(self.root) / f"{pid}.reservation"
        _ = path.write_text(json.dumps({**row, **(fields or {})}))
        if sidecar and not Path(sidecar).name.startswith("fixture-anonymous-"):
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
        result = gate.admission_pass(inputs, now, policy)
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
            **fake_memory_readings(self.root),
            "LINT_CONFIG_FILE": str(self.root / "lint.conf"), "CONF_DIR": str(Path(self.root / "build_admission.conf").parent),
        }))
        _ = write_config_fixture(self.root / "build_admission.conf", "")
        commit_config_fixture(self.root / "build_admission.conf")
        self.phase_need = gate.TestPhaseNeed(2 * GIB, 10, 8, 8, "same_width", margin=1.2)
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
        self.readings[100] = gate.MeasuredMemory(GIB * 2 // 5, anonymous=gate.AnonymousRunUse(GIB * 2 // 5))
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


    def test_test_phase_selects_its_need_and_shows_the_same_run_keep_aside(self) -> None:
        for peak in (3 * GIB, 4 * GIB):
            with self.subTest(peak=peak):
                history = replace(self.phase_need, bytes=3 * GIB)
                path = self.running(100, 12, test_phase=self.phase_fields(peak), fields={
                    "test_phase_need": gate.test_phase_need_record(history),
                    "repo_identity": HANA.key, "test_scope": "filtered", "package": "hana",
                    "filters": ["render", "native_rate"], "test_threads": 8, "call_id": "phase-call"})
                self.readings[100] = gate.MeasuredMemory(3 * GIB, anonymous=gate.AnonymousRunUse(3 * GIB))
                reading = gate.queue_read()
                room = reading.explanation.room
                assert isinstance(room, gate.MemoryRoom)
                hold, = room.runs
                expected = 3.6 if peak == 3 * GIB else 4.8
                self.assertAlmostEqual(hold.memory.need / GIB, expected, places=6)
                self.assertAlmostEqual(hold.bytes / GIB, expected - 3, places=6)
                self.assertEqual(hold.memory.use, gate.AnonymousRunUse(3 * GIB))
                self.assertNotEqual(hold.bytes, 9 * GIB)
                running, = reading.explanation.running
                self.assertEqual(running.promise, hold.memory.need)
                assert isinstance(running.step.measured, gate.MeasuredMemory)
                self.assertIsInstance(running.step.measured.peak_history, gate.TestPhaseStarted)
                text = queue_evidence.running_text(running, reading.explanation, {}, ZoneInfo("UTC"), names=read_step_names())
                self.assertIn(f"tests started: need {expected:.1f} GiB", text)
                self.assertIn(f"test peak {peak / GIB:.1f} GiB, history 3.0 GiB x 1.2", text)
                self.assertIn(queue_evidence.keep_aside_text(hold), text)
                self.assertEqual(gate.read_reservation(path)["need"], 12 * GIB)

    def test_test_peak_grows_past_the_frozen_baseline_with_the_frozen_margin(self) -> None:
        path = self.phase_step(peak=GIB)
        first = gate.queue_read()
        room = first.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        hold, = room.runs
        self.assertEqual(hold.memory.need, math.ceil(2 * GIB * 1.2))
        _ = write_config_fixture(self.root / "build_admission.conf", "test_phase_margin=1.8")
        self.phase_samples([9 * GIB])
        phase_path = gate.test_phase_path(gate.read_reservation(path))
        assert isinstance(phase_path, Path)
        _ = phase_path.write_text(json.dumps(self.phase_fields(5 * GIB)))
        later = gate.queue_read()
        room = later.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        hold, = room.runs
        self.assertEqual(hold.memory.need, math.ceil(5 * GIB * 1.2))
        running, = later.explanation.running
        assert isinstance(running.step.measured, gate.MeasuredMemory)
        phase = running.step.measured.peak_history
        assert isinstance(phase, gate.TestPhaseStarted)
        self.assertEqual(phase.need, self.phase_need)
        self.assertEqual(phase.peak_so_far, 5 * GIB)

    def test_full_step_need_remains_before_tests_or_without_history_or_an_observed_window(self) -> None:
        for missing in ("before tests", "stored need", "window"):
            with self.subTest(missing=missing):
                fields: dict[str, object] = {
                    "repo_identity": HANA.key, "test_scope": "filtered", "package": "hana",
                    "filters": ["render", "native_rate"], "test_threads": 8, "call_id": "phase-call"}
                if missing != "stored need":
                    fields["test_phase_need"] = gate.test_phase_need_record(replace(self.phase_need, bytes=3 * GIB))
                phase = self.phase_fields(3 * GIB) if missing != "before tests" else None
                path = self.running(100, 12, test_phase=phase, fields=fields)
                if missing == "window":
                    phase_path = gate.test_phase_path(gate.read_reservation(path))
                    assert isinstance(phase_path, Path)
                    phase_path.unlink()
                elif missing == "before tests":
                    phase_path = gate.test_phase_path(gate.read_reservation(path))
                    assert isinstance(phase_path, Path)
                    phase_path.unlink(missing_ok=True)
                self.readings[100] = gate.MeasuredMemory(3 * GIB, anonymous=gate.AnonymousRunUse(3 * GIB))
                reading = gate.queue_read()
                room = reading.explanation.room
                assert isinstance(room, gate.MemoryRoom)
                hold, = room.runs
                self.assertEqual(hold.memory.need, 12 * GIB)
                self.assertEqual(hold.bytes, 9 * GIB)
                self.assertEqual(reading.explanation.running[0].promise, 12 * GIB)

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
        self.assertEqual(gate.test_phase_need_from_rows(gate.anonymous_history_rows(self.root / "admission/anon_peaks.jsonl"), gate.read_reservation(path), "test-host", NOW, 1.2),
                         gate.TestPhaseNeed(9 * GIB, 10, 8, 8, "same_width", margin=1.2))

    def test_nearest_wider_history_is_not_scaled_down(self) -> None:
        path = self.phase_step()
        self.phase_samples([3 * GIB, 4 * GIB], width=12)
        self.phase_samples([19 * GIB], width=16)
        self.phase_samples([GIB], width=4)
        self.assertEqual(gate.test_phase_need_from_rows(gate.anonymous_history_rows(self.root / "admission/anon_peaks.jsonl"), gate.read_reservation(path), "test-host", NOW, 1.2),
                         gate.TestPhaseNeed(4 * GIB, 2, 8, 12, "wider_unscaled", margin=1.2))

    def test_widest_narrower_history_scales_up_and_rounds_up(self) -> None:
        path = self.phase_step()
        self.phase_samples([GIB + 1], width=3)
        self.phase_samples([10 * GIB], width=2)
        self.assertEqual(gate.test_phase_need_from_rows(gate.anonymous_history_rows(self.root / "admission/anon_peaks.jsonl"), gate.read_reservation(path), "test-host", NOW, 1.2),
                         gate.TestPhaseNeed(((GIB + 1) * 8 + 2) // 3, 1, 8, 3, "scaled_up", margin=1.2))

    def test_test_phase_need_uses_the_filtered_floor(self) -> None:
        path = self.phase_step()
        self.phase_samples([GIB // 10])
        self.assertEqual(gate.test_phase_need_from_rows(gate.anonymous_history_rows(self.root / "admission/anon_peaks.jsonl"), gate.read_reservation(path), "test-host", NOW, 1.2),
                         gate.TestPhaseNeed(GIB, 1, 8, 8, "same_width", margin=1.2))

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
        self.assertEqual(gate.test_phase_need_from_rows(gate.anonymous_history_rows(self.root / "admission/anon_peaks.jsonl"), row, "test-host", NOW, 1.2), gate.NoTestPhaseNeed("no_history"))
        row["test_threads"] = gate.UnspecifiedTestWidth()
        self.assertEqual(gate.test_phase_need_from_rows(gate.anonymous_history_rows(self.root / "admission/anon_peaks.jsonl"), row, "test-host", NOW, 1.2), gate.NoTestPhaseNeed("no_width"))

    def test_test_phase_reader_uses_the_shared_whole_row_boundary(self) -> None:
        path = self.phase_step()
        self.phase_samples([2 * GIB])
        need = assert_history_decoded_once(
            self, self.root / "admission/anon_peaks.jsonl",
            lambda: gate.test_phase_need_from_rows(
                step_estimator.anonymous_history_rows(self.root / "admission/anon_peaks.jsonl"),
                gate.read_reservation(path), "test-host", NOW, 1.2))
        self.assertEqual(need, gate.TestPhaseNeed(2 * GIB, 1, 8, 8, "same_width", margin=1.2))
        assert_unfinished_history_ignored(self, self.root / "admission/anon_peaks.jsonl",
            lambda: gate.test_phase_need_from_rows(gate.anonymous_history_rows(self.root / "admission/anon_peaks.jsonl"), gate.read_reservation(path), "test-host", NOW, 1.2))


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


    def test_saved_test_margin_requires_a_finite_float_of_at_least_one(self) -> None:
        for margin in (1, "1.2", True, float("nan"), float("inf"), 0.9):
            with self.subTest(margin=margin):
                record = {**gate.test_phase_need_record(self.phase_need), "margin": margin}
                self.assertEqual(gate.read_test_phase_need(record), gate.NoTestPhaseNeed("not_recorded"))

    def test_charge_wire_types_have_one_shared_owner(self) -> None:
        from scripts.lib import test_phase

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
        from scripts.lib import test_phase

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
        from scripts.lib import test_phase

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
            self.assertEqual(sampled.intervals[0].need, 2 * GIB)
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
            fixture.hold_starts()
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

    def test_source_counts_separate_admissions_logical_waits_and_missing_evidence(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(since=fixture.clock.now() - 5)
            assert isinstance(request.profile, gate.StepProfile)
            first = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(10)
            _ = fixture.server.handle_request(wire.TokenWaitRequest("token-return", first.reservation))
            expected = gate.QueueSourceCounts(
                admissions={"measured": 1}, memory_waits=(5,), incomplete_memory_waits=0,
                unsampled_intervals=1, open_intervals=0, completed_steps=0)
            self.assertEqual(gate.count_queue_sources(gate.read_queue_lifetimes(fixture.state_dir)), expected)

            fixture.clock.advance(20)
            fixture.free(0)
            fixture.hold_starts()
            returned = replace(request, episode="after-token", token_back_at=fixture.clock.now(),
                sidecar=str(fixture.scope(request.owner)),
                profile=replace(request.profile, peak=gate.ExpectedPeak(request.profile.peak.bytes, "buildlog", 5)))
            self.assertIsInstance(fixture.server.handle_request(returned)[0], wire.Wait)
            fixture.clock.advance(7)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            second = only_admitted([fixture.server.ledger[returned.episode].reply])
            expected = replace(expected, admissions={"measured": 1, "buildlog": 1},
                memory_waits=(12,), open_intervals=1)
            self.assertEqual(gate.count_queue_sources(gate.read_queue_lifetimes(fixture.state_dir)), expected)

            fixture.scoped_peak(second, GIB)
            fixture.clock.advance(2)
            _ = fixture.server.handle_request(wire.ReleaseRequest("finished", second.reservation, 0))
            expected = replace(expected, open_intervals=0, completed_steps=1)
            self.assertEqual(gate.count_queue_sources(gate.read_queue_lifetimes(fixture.state_dir)), expected)

            path = fixture.state_dir / "queue_times.jsonl"
            events = [event for event in gate.anonymous_history_rows(path) if event.get("kind") != "token_gap"]
            _ = path.write_text("".join(json.dumps(event) + "\n" for event in events))
            expected = replace(expected, memory_waits=(), incomplete_memory_waits=1)
            self.assertEqual(gate.count_queue_sources(gate.read_queue_lifetimes(fixture.state_dir)), expected)

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
            "USER_ZONE": "America/New_York",
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
            admission.hold_starts()
            for pid, age in ((300, 900), (400, 600)):
                request = admission.request(owner=admission.fixture_owner(pid, self.processes[pid]),
                    need=12 * GIB, since=self.clock - age)
                _ = admission.server.handle_request(request)
            before = {key: wire.encode_record(gate.stored_waiter(value.row))
                for key, value in admission.server.waiters.items()}
            for unit, expected in (("first", [300, 400]), ("second", [400, 300])):
                replies = admission.completed_queue_change(wire.PrioritySetRequest(unit, unit, "user's work", "showrunner-id"))
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
            self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                "initial", "chosen", "saved reason", "director")), [wire.Ok()])
            priority = fixture.server.priority
            assert not isinstance(priority, gate.NoQueuePriority)
            before = priority.copy()
            logged = buildlog_rows(fixture.state_dir.parent)
            for unit in ("not-a-roster-unit", UnitLabels("").unit):
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
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
                    _ = fixture.completed_queue_change(request)
            priority = fixture.server.priority
            assert not isinstance(priority, gate.NoQueuePriority)
            self.assertIsInstance(priority["transition"], gate.PendingPriorityTransition)
            transition = priority["transition"]
            assert isinstance(transition, gate.PendingPriorityTransition)
            self.assertEqual(transition.month_row_end, len(prefix))
            fixture.restart()
            self.assertEqual(fixture.completed_queue_change(request), [wire.Ok()])
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
                        fixture.allow_next_start()
                        next_request = fixture.request(need=GIB)
                        _ = only_admitted(fixture.server.handle_request(next_request))
                    self.assertEqual(fixture.server.reservations[request.episode], saved)
                    self.assertIn(saved.path, errors.getvalue())
                    self.assertIn("unreadable peak", errors.getvalue())


class BuildPromiseFixture(MemoryQueueFixture):
    """One private history, index and ledger for the build-promise regressions."""

    release_headroom: gate.ReleaseHeadroom = gate.ReleaseHeadroom(1.0)
    dev_size_headroom: gate.DevSizeHeadroom = gate.DevSizeHeadroom(1.0, 0)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root),
        }))
        _ = self.enterContext(patch.object(step_estimator, "git_identity", return_value=(HANA, "hana-clone")))
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
        return gate.expected_peak(HANA, "build", "test-host", now, self.root, shape=shape, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])

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
        self.assertEqual((self.estimate().bytes, self.estimate().count),
                         (gate.shape_headroom_bytes(20 * GIB, self.release_headroom), 2))

    def test_repeated_step_with_a_zero_count_keeps_its_compile_peak(self) -> None:
        self.sample(16, call_id="retried-build", crates=652)
        self.index_build("retried-build", RELEASE_BUILD, crates=0)
        peak = self.estimate()
        self.assertEqual(peak.count, 1)
        self.assertEqual(peak.bytes, gate.shape_headroom_bytes(16 * GIB, self.release_headroom))

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

    def test_profile_fallback_keeps_the_full_failed_peak_without_a_host_limit(self) -> None:
        for arguments in (RELEASE_BUILD, ["cargo", "build", "--bin", "hana"]):
            shape = build_shape(arguments)
            self.sample(24, shape=build_shape(["cargo", "build", "--profile", shape.profile, "--lib"]),
                        outcome="killed", need=20)
            with self.subTest(profile=shape.profile):
                peak = self.estimate(shape)
                self.assertEqual((peak.bytes, peak.count), (24 * GIB, 0))
                self.assertEqual(peak.basis, gate.ShapeFallback())
                self.assertEqual(gate.need_basis_record(peak.basis), {
                    "kind": "shape_fallback", "cap": {"kind": "none"},
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
                self.assertEqual(peak.bytes, gate.shape_headroom_bytes(10 * GIB, rule))
                basis = self.same_shape_basis(peak)
                assert isinstance(basis, gate.ShapeLargest)
                self.assertEqual(basis.factor, rule)

    def test_build_promises_keep_the_full_history_peak_and_headroom(self) -> None:
        for arguments in (RELEASE_BUILD, ["cargo", "build"]):
            shape = build_shape(arguments)
            self.sample(30, shape=shape, need=30)
            with self.subTest(profile=shape.profile):
                peak = self.estimate(shape)
                rule = self.release_headroom if shape.profile == "release" else self.dev_size_headroom
                self.assertEqual(peak.bytes, gate.shape_headroom_bytes(30 * GIB, rule))
                self.assertEqual(self.same_shape_basis(peak).cap, gate.UnderBudget())

    def test_old_history_matches_a_copy_with_a_different_checkout_name(self) -> None:
        self.index_build("copied-build", RELEASE_BUILD)
        with closing(sqlite3.connect(file_marks.index_path_in(self.root))) as connection, connection:
            _ = connection.execute("UPDATE steps SET repo = 'hana-copy'")
        self.sample(16, record_shape=False, call_id="copied-build")
        peak = self.estimate()
        self.assertEqual((peak.count, peak.bytes), (1, gate.shape_headroom_bytes(16 * GIB, self.release_headroom)))

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
                             gate.shape_headroom_bytes(16 * GIB, self.release_headroom))
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

            def delayed_generation(
                configuration: gate.MemoryConfig | client.ReadConfigurationAtUse = client.READ_CONFIGURATION_AT_USE,
            ) -> gate.BuildHistoryGeneration:
                self.assertIsInstance(configuration, gate.MemoryConfig)
                fixture.clock.advance(10)
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "publish-during-query", grant.reservation, 137)), [wire.Ok()])
                fixture.clock.advance(90)
                fixture.free(18 * GIB)
                fixture.hold_starts()
                return fixture.build_history_generation()

            with patch.object(client, "CLOCK", fixture.clock), \
                    patch.object(client, "build_history_generation", side_effect=delayed_generation) as query:
                request = client.step_request(RELEASE_BUILD, str(fixture.root), owner,
                    NOW_EPOCH, "", "after-delayed-query")
            query.assert_called_once()
            current = self.estimate(now=datetime.fromtimestamp(fixture.clock.now(), UTC))
            self.assertEqual(current.bytes, gate.shape_headroom_bytes(16 * GIB, self.release_headroom))
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
                self.assertIsInstance((held.reason.cause if isinstance(held.reason, gate.HeadAdmissionStatus) else held.reason), gate.EstimateExpired)
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
                self.assertNotIsInstance((reply.reason.cause if isinstance(reply.reason, gate.HeadAdmissionStatus) else reply.reason), gate.EstimateExpired)
                self.assertEqual(reply.snapshot.peak, refreshed)
                self.assertEqual(reply.snapshot.decision.state, "hold")
                self.assertNotIn(waiter.episode, fixture.server.reservations)
                fixture.free(60 * GIB)
                fixture.allow_next_start()
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
                            self.assertNotIsInstance((reply.reason.cause if isinstance(reply.reason, gate.HeadAdmissionStatus) else reply.reason), gate.EstimateExpired)
                            fixture.clock.advance(1)
                            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                                "floor-before-refresh", grant.reservation, 137)), [wire.Ok()])
                        elif len(replies) == 2:
                            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, gate.HeadAdmissionStatus) else reply.reason), gate.EstimateExpired)
                            if restarted:
                                fixture.restart()
                            fixture.clock.advance(1)
                            fixture.free(18 * GIB)
                            reads_at_expiry = history_scans
                        else:
                            self.assertEqual(len(replies), 3)
                            self.assertEqual(history_scans - reads_at_expiry, 1,
                                "one expiry must read history once")
                            self.assertNotIsInstance((reply.reason.cause if isinstance(reply.reason, gate.HeadAdmissionStatus) else reply.reason), gate.EstimateExpired)
                            self.assertGreater(reply.snapshot.peak.bytes, replies[0].snapshot.peak.bytes)
                            self.assertEqual(reply.snapshot.decision.state, "hold")
                            waiting, = fixture.server.waiters.values()
                            self.assertEqual(waiting.request.since, NOW_EPOCH)
                            self.assertEqual(waiting.request.owner, owner)
                            self.assertEqual(waiting.request.profile.peak, reply.snapshot.peak)
                            self.assertNotIn(waiting.request.episode, fixture.server.reservations)
                            raise RefreshChecked()
                with patch.object(client, "CLOCK", fixture.clock), \
                     patch.object(records, "process_start_time", side_effect=self.processes.get), \
                     patch.object(client.WaitOutput, "__call__", side_effect=observe), \
                     patch.object(step_estimator, "anonymous_history_rows", side_effect=count_history_scans), \
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
        self.assertEqual(gate.read_need_basis(encoded), basis)

    def test_legacy_budget_cap_does_not_consult_the_current_budget(self) -> None:
        encoded = {"kind": "shape_fallback", "cap": {"kind": "budget", "uncapped": 22 * GIB}}
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


    def test_legacy_gate_history_keeps_the_first_need_across_hosts_and_ages(self) -> None:
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
        self.assertEqual(gate.program_peak_estimate("merge-gate", "host", NOW, self.root, 4 * GIB), gate.FirstProgramPeak(4 * GIB))

    def test_expired_measured_gate_profile_retains_its_original_fallback_cap(self) -> None:
        profile = gate.ProgramProfile("merge-gate", "installed/.claude", "checkout",
            gate.HistoryPeakSizing(4 * GIB, gate.MeasuredProgramPeak(GIB, 1),
                NOW_EPOCH - admission_policy_fixture().timing.estimate_refresh_seconds))
        with patch.object(step_estimator, "git_identity", side_effect=AssertionError("the program inferred a cargo identity")):
            result = gate.step_estimate(["merge-gate"], "launcher", profile, NOW, self.root, admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
        self.assertEqual(result.profile.peak, gate.ExpectedPeak(4 * GIB, "fallback", 0))
        assert isinstance(result.profile, gate.ProgramProfile)
        self.assertEqual(result.profile.sizing.estimate, gate.FirstProgramPeak(4 * GIB))
        self.assertEqual((result.profile.repo, result.profile.step, result.profile.worktree),
                         ("installed/.claude", "merge-gate", "checkout"))

    def test_expired_gate_profile_refreshes_outside_lock_without_cargo_inference(self) -> None:
        profile = gate.ProgramProfile("merge-gate", "installed/.claude", "checkout",
            gate.HistoryPeakSizing(4 * GIB, gate.FirstProgramPeak(4 * GIB),
                NOW_EPOCH - admission_policy_fixture().timing.estimate_refresh_seconds))
        def estimate(program: str, host: str, now: datetime, root: Path, cap: int) -> gate.MeasuredProgramPeak:
            self.assertEqual((program, cap, now, root), ("merge-gate", 4 * GIB, NOW, self.root))
            self.assertTrue(host)
            return gate.MeasuredProgramPeak(6 * GIB, 2)
        with patch.object(step_estimator, "program_peak_estimate", side_effect=estimate) as refreshed, \
             patch.object(step_estimator, "git_identity", side_effect=AssertionError("the program inferred a cargo identity")), \
             patch.object(fcntl, "flock", side_effect=AssertionError("the estimate acquired an admission lock")):
            result = gate.step_estimate(["merge-gate"], "launcher", profile, NOW, self.root, admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
        refreshed.assert_called_once()
        self.assertEqual((result.profile.repo, result.profile.step, result.profile.worktree, result.profile.peak.bytes),
                         ("installed/.claude", "merge-gate", "checkout", 6 * GIB))
        self.assertEqual(result.profile.estimated_at, NOW_EPOCH)


class HistoryAdmissionTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            **fake_memory_readings(self.root, total_kib=60 * GIB // 1024),
            "LINT_CONFIG_FILE": str(config),
        })))





    def test_history_waiter_expires_when_lock_wait_exceeds_head_seconds(self) -> None:
        policy = gate.read_memory_config().policy
        row: gate.Waiter = {"pid": 400, "start_time": "warmup", "need": 6 * GIB,
            "since": NOW_EPOCH, "estimated_at": NOW_EPOCH, "need_source": "measured"}
        self.assertTrue(gate.estimate_expired(row, NOW_EPOCH + policy.timing.estimate_refresh_seconds, policy=policy))
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
        result = gate.admission_pass(inputs, NOW_EPOCH, policy)
        row = next((row for row in result.rows if row.owner == owner))
        return gate.blocker_identity(row.blocker, list(inputs.reservations), processes, self.root / 'proc')

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, fake_memory_readings(self.root, available_kib=8 * GIB // 1024, total_kib=60 * GIB // 1024))))
        _ = self.enterContext(patch.object(time, 'time', return_value=NOW.timestamp()))
        self.readings[100] = gate.MeasuredMemory(12 * GIB, anonymous=gate.AnonymousRunUse(12 * GIB))
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
        self.readings[100] = gate.MeasuredMemory(GIB, anonymous=gate.AnonymousRunUse(GIB))
        _ = self.meminfo.write_text(f"MemAvailable: {26 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
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
                with patch.dict(os.environ, fake_memory_readings(self.root, available_kib=(26 if reason == 'ready' else 8) * GIB // 1024, total_kib=60 * GIB // 1024, full_avg10=11 if reason == 'pressure' else 0)):
                    self.readings[100] = gate.MeasuredMemory(GIB if reason == 'ready' else 12 * GIB, anonymous=gate.AnonymousRunUse(GIB if reason == 'ready' else 12 * GIB))
                    extra = self.root / 'admission/200.reservation'
                    if reason == 'ambiguous':
                        self.readings[100] = gate.MeasuredMemory(6 * GIB, anonymous=gate.AnonymousRunUse(6 * GIB))
                        self.readings[200] = gate.MeasuredMemory(6 * GIB, anonymous=gate.AnonymousRunUse(6 * GIB))
                        extra = self.running(200, 6)
                    if reason == 'unknown-owner':
                        (self.root / 'proc/100/environ').unlink()
                    if reason == 'no-reservation':
                        self.reservation.unlink()
                    self.assertIsInstance(self.blocker(), gate.NoSingleBlocker)
                    self.assertEqual(gate.blocker_fields(self.blocker()), {})
                    extra.unlink(missing_ok=True)

    def test_removing_one_of_several_reservations_must_be_enough(self) -> None:
        waiter = cast(gate.Waiter, json.loads(self.waiter_path.read_text()))
        waiter["need"] = 30 * GIB
        _ = self.waiter_path.write_text(json.dumps(waiter))
        self.readings[100] = gate.MeasuredMemory(18 * GIB, anonymous=gate.AnonymousRunUse(18 * GIB))
        self.readings[200] = gate.MeasuredMemory(10 * GIB, anonymous=gate.AnonymousRunUse(10 * GIB))
        _ = self.running(200, 10)
        self.assertEqual(gate.blocker_fields(self.blocker()), {})
        self.readings[200] = gate.MeasuredMemory(2 * GIB, anonymous=gate.AnonymousRunUse(2 * GIB))
        _ = self.meminfo.write_text(f"MemAvailable: {24 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
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
        _ = self.blocker()
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

    def test_accepted_needs_stay_kept_after_the_first_two_minutes(self) -> None:
        for pid in (100, 200, 500, 600, 700):
            self.processes[pid] = str(pid)
            self.readings[pid] = gate.MeasuredMemory(int(1.36 * GIB), anonymous=gate.AnonymousRunUse(int(1.36 * GIB)))
            _ = self.running(pid, 8.68)
        result = self.propose(10.8, 18.1)
        self.assertIsInstance(result.admission, gate.NotAdmitted)
        self.assertEqual(result.decision.state, "hold")
        self.assertAlmostEqual(result.decision.promised / GIB, 36.6)
        self.assertAlmostEqual(result.decision.measured / GIB, 6.8)
        self.assertAlmostEqual(result.decision.charged / GIB, 43.4)


    def test_22_gib_step_runs_alone_despite_the_budget(self) -> None:
        result = self.propose(22, 30)
        self.assertIsInstance(result.admission, gate.ProposedAdmission)


    def test_reading_a_finished_tree_does_not_keep_a_promise(self) -> None:
        _ = self.running(100, 23.5)
        self.readings[100] = gate.GoneMemory()
        result = self.propose(10.8, 15)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))


    def test_clone88ci_head_starts_when_measured_free_memory_covers_it(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        _ = self.head()
        result = self.pass_for(pid=300, need=10.9, free=35)
        self.assertEqual(result.decision.state, "admit")
        self.assertIsInstance(result.decision.order_hold, gate.NoOrderHold)
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
        _ = (scope / "memory.stat").write_text(f"anon {2 * GIB}\ninactive_file {GIB}\n")
        _ = (scope / "memory.swap.current").write_text(str(7 * GIB))
        row = gate.read_reservation(path)
        reading = gate.step_memory(row, gate.process_table(self.root / "proc"), self.root / "proc")
        self.assertIsInstance(reading, gate.MeasuredMemory)
        assert isinstance(reading, gate.MeasuredMemory)
        self.assertEqual((reading.bytes, reading.swap_bytes), (2 * GIB, 7 * GIB))
        captured = gate.reservation_memory(path, row, NOW, gate.process_table(self.root / "proc"), self.root / "proc", admission_policy_fixture())
        step = gate.RunningStep("owner", "owner", "nextest", "/tree", GIB, captured, pid=row["pid"])
        inputs = gate.QueueInputs([], (step,), (row,), gate.HostMemory(40 * GIB, 60 * GIB),
            gate.NoPressureHold(), gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), NOW.timestamp())
        result = gate.queue_read_from(inputs, policy=admission_policy_fixture())
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
                        reading = gate.step_memory(row, processes, self.root / "proc")
                    if missing == "memory.swap.current":
                        self.assertIsInstance(reading, gate.MeasuredMemory)
                        assert isinstance(reading, gate.MeasuredMemory)
                        self.assertEqual((reading.bytes, reading.swap_bytes), (3 * GIB, 0))
                        captured = gate.reservation_memory(reservation, row, NOW, processes,
                                                           self.root / "proc", admission_policy_fixture())
                        assert isinstance(captured, gate.MeasuredMemory)
                        assert isinstance(captured.anonymous_source, gate.ScopeSource)
                        self.assertEqual(str(captured.anonymous_source.directory), str(scope))
                        self.assertEqual(reading.promise, gate.LifetimePromise(12 * GIB))
                        self.assertIsInstance(reading.anonymous, gate.RunUseUnavailable)
                        tree.assert_not_called()
                    elif owner == "live":
                        self.assertEqual(reading, gate.MeasuredMemory(2 * GIB, promise=gate.LifetimePromise(12 * GIB)))
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
                        self.assertIn("swap", gate.head_wait_text(result.reason, ZoneInfo("UTC")))


    def test_sccache_anon_is_one_charge_and_missing_or_unreadable_is_zero(self) -> None:
        self.readings_for()
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(8 * GIB, anonymous=gate.AnonymousRunUse(8 * GIB))
        service = self.builds / "sccache.service"
        service.mkdir(parents=True)
        stat = service / "memory.stat"
        _ = stat.write_text(f"anon {6 * GIB}\nfile {90 * GIB}\ninactive_file {90 * GIB}\n")
        _ = (service / "memory.current").write_text(str(96 * GIB))
        result = self.admit_at()
        self.assertEqual((result.decision.state, result.decision.charged), ("admit", 12 * GIB))
        view = gate.queue_read().view
        self.assertEqual(len(view.running), 2)
        self.assertEqual(view.waiting[0].reason, result.reason)
        stat.unlink()
        for unreadable in (False, True):
            if unreadable:
                stat.mkdir()
            result = self.admit_at()
            self.assertEqual((result.decision.state, result.decision.charged), ("admit", 12 * GIB))

    def test_sccache_is_charged_but_does_not_prevent_running_alone(self) -> None:
        service = self.builds / "sccache.service"
        service.mkdir(parents=True)
        _ = (service / "memory.stat").write_text("anon 1048576\ninactive_file 0\n")
        _ = (service / "memory.current").write_text("1048576")
        result = self.pass_for(400, need=22, free=60)
        self.assertEqual((result.decision.state, result.decision.charged), ("admit", 0))
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
        _ = self.pressure.write_text("full avg10=4.0 avg60=1.0 total=1\nsome avg10=0\n")
        _ = self.meminfo.write_text(
            f"MemAvailable: {40 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n"
            + "SwapTotal: 100 kB\nSwapFree: 1 kB\n")
        policy = gate.read_memory_config().policy
        host = gate.read_meminfo(self.meminfo)
        pressure = gate.read_pressure(host, policy=policy)
        result = queue_decision(host.available, host.total, 8 * GIB, [], pressure=pressure)
        self.assertEqual(result.state, "admit")
        self.assertIsInstance(result.pressure, gate.NoPressureHold)

    def test_unreadable_pressure_holds_only_until_the_reading_is_repaired(self) -> None:
        self.pressure.unlink()
        self.pressure.mkdir()
        self.assert_admission(True)
        result = self.pass_for(400, 8, 1)
        self.assertEqual(result.decision.state, "hold")
        self.assertIsInstance(result.decision.pressure, gate.PressureUnavailable)
        self.pressure.rmdir()
        _ = self.pressure.write_text("some avg10=0\nfull avg10=0\n")
        self.assert_admission(False)


    def test_pressure_is_in_checker_summary_even_with_no_running_step(self) -> None:
        _ = self.pressure.write_text('full avg10=6.0 avg60=1.0 total=1\nsome avg10=0\n')
        result = self.pass_for(400, need=8, free=40)
        self.assertEqual(result.decision.state, "hold")
        assert isinstance(result.decision.pressure, gate.MemoryPressure)
        self.assertEqual(result.decision.pressure.full_avg10, 6.0)
        self.assertIn("memory pressure", gate.pressure_text(result.decision.pressure))


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
            **fake_memory_readings(self.root),
        })))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.meminfo.write_text(f"MemAvailable: {60 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def strict_head(self, need: float = 11.5, *, priority: float = NOW_EPOCH) -> Path:
        path = self.waiter(300, NOW.timestamp() - 2400, "/tmp/head", "head", need)
        row = cast(gate.Waiter, json.loads(path.read_text()))
        row.update(priority_since=priority, label="head nextest")
        _ = path.write_text(json.dumps(row))
        return path


    def test_fitting_strict_head_keeps_its_place_when_younger_polls_first(self) -> None:
        for need, free in ((4, 60), (5, 60), (4, 21)):
            with self.subTest(need=need, free=free):
                directory = gate.admission_dir(self.root)
                for pattern in ("*.waiting", "*.reservation"):
                    for path in directory.glob(pattern):
                        path.unlink()
                _ = self.running(100, 10, age=30)
                self.readings[100] = gate.MeasuredMemory(10 * GIB, anonymous=gate.AnonymousRunUse(10 * GIB))
                _ = self.strict_head(8)
                _ = self.waiter(400, NOW.timestamp() - 60, "/tmp/younger", "younger", need)
                result = self.pass_for(pid=400, need=need, free=free)
                self.assertEqual(result.decision.state, "hold")
                if free == 21:
                    self.assertIsInstance(result.reason, gate.BehindHead)
                else:
                    self.assertEqual(result.reason, gate.StartSpacingWait(NOW_EPOCH + 5))
                self.assertEqual(gate.queue_view().waiting[-1].reason, result.reason)
                self.assertEqual(gate.explain_queue().line.steps[-1].waiter.reason, result.reason)
                self.assertIsInstance(gate.blocker_identity(result.blocker,
            [row for _, row in gate.reservation_rows(gate.admission_dir(self.root))],
            gate.process_table(self.root / "proc"), self.root / "proc"), gate.NoSingleBlocker)
                self.assertEqual(self.pass_for(pid=300, need=8, free=free).decision.state, "admit")

    def test_pressure_and_missing_memory_do_not_admit_a_younger_run(self) -> None:
        _ = self.running(100, 20, age=30)
        _ = self.strict_head()
        pressure = self.root / "pressure"
        _ = pressure.write_text("full avg10=11\nsome avg10=0\n")
        result = self.pass_for(pid=400, need=2, free=60)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual(result.decision.pressure, gate.MemoryPressure(11, condition="full"))
        self.meminfo.unlink()
        self.assertTrue(all(isinstance(step.reason.cause if isinstance(step.reason, gate.HeadAdmissionStatus) else step.reason, gate.ReadingUnavailable) for step in gate.queue_view().waiting))

    def test_room_is_taken_in_queue_order_before_a_younger_polls(self) -> None:
        _ = self.running(100, 11.1, age=30)
        _ = self.strict_head()
        _ = self.waiter(400, NOW_EPOCH - 120, "/tmp/older", "older", 5)
        self.processes[500] = "young"
        _ = self.waiter(500, NOW_EPOCH - 60, "/tmp/young", "young", 5)
        result = self.pass_for(pid=500, need=5, free=25)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual(result.reason, gate.BehindHead("head", NOW_EPOCH - 2400, "session-300"))
        self.assertEqual(gate.queue_view().waiting[-1].reason, result.reason)
        self.assertEqual(gate.explain_queue().line.steps[-1].waiter.reason, result.reason)
        self.assertIsInstance(gate.blocker_identity(result.blocker,
            [row for _, row in gate.reservation_rows(gate.admission_dir(self.root))],
            gate.process_table(self.root / "proc"), self.root / "proc"), gate.NoSingleBlocker)
        self.assertEqual(self.pass_for(pid=300, need=11.5, free=30).decision.state, "admit")


class OrderedAdmissionTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        environment = fake_memory_readings(self.root)
        environment.update({"BUILDLOG_MEMINFO": str(self.meminfo)})
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        self.processes[500] = "younger"
        _ = self.meminfo.write_text(f"MemAvailable: {24 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def scenario(self, older_need: float, younger_need: float) -> tuple[Path, Path]:
        _ = self.running(100, 5.8)
        return (self.waiter(400, NOW_EPOCH - 120, "/tmp/berth", "berth", older_need),
                self.waiter(500, NOW_EPOCH - 60, "/tmp/handler", "handler", younger_need))

    def test_younger_poll_keeps_room_for_the_protected_head(self) -> None:
        _ = self.scenario(2.9, 3)
        result = self.pass_for(500, need=3, free=12)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual(result.reason, gate.BehindHead("berth", NOW_EPOCH - 120, "session-400"))
        self.assertEqual(self.pass_for(400, need=2.9, free=12).decision.state, "admit")

    def test_older_smaller_step_gets_room_before_the_younger_step(self) -> None:
        _ = self.scenario(1, 4)
        self.assertEqual(self.pass_for(500, need=4, free=10).decision.state, "hold")
        self.assertEqual(self.pass_for(400, need=1, free=10).decision.state, "admit")


    def test_one_snapshot_agrees_in_check_queue_explain_blocker_and_shell(self) -> None:
        _ = self.scenario(2.9, 3)
        result = self.pass_for(500, need=3, free=12)
        self.assertEqual(result.decision.state, "hold")
        self.assertIsInstance(result.reason, gate.BehindHead)
        read = gate.queue_read()
        self.assertEqual(read.view.waiting[-1].reason, result.reason)
        self.assertEqual(read.explanation.line.steps[-1].waiter.reason, result.reason)
        self.assertIsInstance(gate.blocker_identity(result.blocker,
            [row for _, row in gate.reservation_rows(gate.admission_dir(self.root))],
            gate.process_table(self.root / "proc"), self.root / "proc"), gate.NoSingleBlocker)
        self.assertEqual(queue_view.reason_phrase(result.reason, read.view.waiting), "behind #1")


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
        _ = self.waiter(300, NOW_EPOCH - 60, "/tmp/head", "head", 7)
        _ = self.waiter(400, NOW_EPOCH - 30, "/tmp/follower", "follower", 3)
        result = self.pass_for(300, need=7, free=8)
        reason = result.reason
        assert isinstance(reason, gate.HeadAdmissionStatus)
        self.assertEqual((reason.need, reason.available), (7 * GIB, 8 * GIB))
        self.assertEqual((reason.decision.threshold, reason.decision.promised, reason.decision.reserve),
                         (14 * GIB, 4 * GIB, 3 * GIB))
        self.assertEqual(reason.decision.threshold,
                         reason.need + reason.decision.promised + reason.decision.reserve)
        self.assertEqual(gate.queue_view().waiting[0].reason, reason)
        follower = gate.queue_view().waiting[1]
        self.assertEqual(follower.reason, gate.BehindHead("head", NOW_EPOCH - 60, "session-300"))

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
        self.assertEqual((view.promised, sum(step.promise for step in view.running)), (int(20.5 * GIB), int(23.5 * GIB)))
        self.assertCountEqual([step.measured for step in view.running],
                              [gate.MeasuredMemory(GIB, anonymous=gate.AnonymousRunUse(GIB), promise=gate.LifetimePromise(12 * GIB),
                                  anonymous_source=gate.ScopeSource(self.root / "fixture-anonymous-100", frozenset())),
                               gate.MeasuredMemory(2 * GIB, anonymous=gate.AnonymousRunUse(2 * GIB),
                                  promise=gate.LifetimePromise(int(11.5 * GIB)), anonymous_source=gate.ScopeSource(self.root / "fixture-anonymous-200", frozenset()))])
        self.assertEqual(view.waiting[0].session_name, "clone88ci")
        self.assertIsInstance(view.waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, view.waiting[0].reason).decision.state, "hold")
        self.assertTrue(all(isinstance(row.reason, gate.BehindHead) for row in view.waiting[1:]))


    def test_missing_meminfo_is_a_reading_unavailable_reason(self) -> None:
        _ = self.queue()
        self.meminfo.unlink()
        self.assertTrue(all(isinstance(row.reason.cause if isinstance(row.reason, gate.HeadAdmissionStatus) else row.reason, gate.ReadingUnavailable) for row in gate.queue_view().waiting))

    def test_psi_11_holds_backfill_without_making_the_young_head_strict(self) -> None:
        _ = self.waiter(300, NOW.timestamp() - 540, "/tmp/head", "head")
        pressure = self.root / "pressure"
        _ = pressure.write_text("full avg10=11.0 avg60=1.0 total=1\nsome avg10=0\n")
        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": str(pressure)}):
            result = self.pass_for(400, need=1, free=40)
            self.assertEqual(result.decision.state, "hold")
            self.assertEqual(result.decision.pressure, gate.MemoryPressure(11, condition="full"))
            self.assertIsInstance(gate.queue_view().waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertIsInstance(result.decision.order_hold, gate.NoOrderHold)

    def test_head_names_the_free_memory_shortfall(self) -> None:
        _ = self.running(100, 1)
        _ = self.head(15.1)
        result = self.pass_for(pid=300, need=15.1, free=18)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual(gate.queue_view().waiting[0].reason, result.reason)
        assert isinstance(result.reason, gate.HeadAdmissionStatus)
        self.assertAlmostEqual(result.decision.threshold / GIB, 18.1)
        self.assertFalse(result.decision.free_passes)
        self.assertNotIn("budget", gate.head_wait_text(result.reason, ZoneInfo("America/New_York")))

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
        _ = pressure.write_text("full avg10=11.0 avg60=1.0 total=1\nsome avg10=0\n")
        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": str(pressure)}):
            head = self.pass_for(pid=300, need=19.3, free=21.9)
            self.assertEqual(head.decision.state, "hold")
            view = gate.queue_view()
            follower, = gate.explain_queue().line.steps
            self.assertEqual(view.waiting[0].reason, head.reason)
        self.assertIsInstance(head.reason, gate.HeadAdmissionStatus)
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)
        self.assertEqual(follower.decision.pressure, gate.MemoryPressure(11.0, 0.0, condition="full"))


    def test_head_hold_survives_rising_free_memory_and_falling_measured_use(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB, anonymous=gate.AnonymousRunUse(3 * GIB))
        path = self.head(19.3)
        row = gate.read_waiter(path)
        row["priority_since"] = NOW_EPOCH
        _ = path.write_text(json.dumps(row))
        first = self.pass_for(400, need=1, free=20)
        self.assertEqual(first.decision.state, "hold")
        self.readings[100] = gate.MeasuredMemory(0, anonymous=gate.AnonymousRunUse(0))
        second = self.pass_for(400, need=1, free=21.9)
        self.assertEqual(second.decision.state, "hold")
        view = gate.queue_view()
        reason = view.waiting[0].reason
        assert isinstance(reason, gate.HeadAdmissionStatus)
        self.assertEqual(gate.head_wait_text(reason, ZoneInfo("America/New_York")), "head: needs 19.3 GiB; room 6.9 GiB")
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)


    def test_drained_head_starts_below_memory_floor_and_a_younger_check_cannot_take_its_place(self) -> None:
        running = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB, anonymous=gate.AnonymousRunUse(3 * GIB))
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


class ProgressKeepAsideTests(unittest.TestCase):
    @staticmethod
    def reservation(step: str = "build", *, identity: str = "step", need: int = 10 * GIB) -> gate.Reservation:
        return {**fixture_reservation(100, need), "step": step, "reservation_id": identity,
                "build_run": LiveBuildRun(50, "50", 100)}

    @staticmethod
    def profile(phase: str, peak: int) -> progress.ProgressProfile:
        return progress.ProgressProfile((progress.PhaseProfile(phase, (peak,) * 20),))

    @staticmethod
    def inputs(rows: tuple[gate.Reservation, ...], readings: tuple[gate.MeasuredMemory, ...]) -> gate.QueueInputs:
        return gate.QueueInputs([], tuple(gate.RunningStep("session", "fixture", row["step"],
            row["worktree"], row["need"], reading, pid=row["pid"], run=LiveBuildRun(50, "50", 100))
            for row, reading in zip(rows, readings, strict=True)), rows,
            gate.HostMemory(30 * GIB, 60 * GIB), gate.NO_PRESSURE_HOLD, gate.ReadyToStart(),
            gate.NoHeadAdmissionOverride(), 100)

    def test_the_user_readings_keep_nothing_extra_until_the_peak_enters_the_window(self) -> None:
        row = self.reservation()
        low, high = GIB // 10, 49 * GIB // 10
        profile = progress.ProgressProfile((progress.PhaseProfile("build:1",
            (low,) * 16 + (high,) + (low,) * 3),))
        kept: list[int] = []
        policy = admission_policy_fixture()
        for done, use in ((0, low), (20, low), (40, low), (60, low), (80, high), (100, low)):
            reading = gate.MeasuredMemory(20 * GIB, swap_bytes=7 * GIB,
                anonymous=gate.AnonymousRunUse(use))
            adjusted = gate.progress_memory(row, reading, gate.AttributedStepUse(reading.anonymous, True),
                progress.ProgressCounter("build:1", done, 100), profile, policy)
            room = gate.inputs_room(self.inputs((row,), (adjusted,)), policy)
            assert isinstance(room, gate.MemoryRoom)
            kept.append(room.kept)
            self.assertEqual(room.used, use)
        self.assertEqual(kept, [0, 0, 0, high - low, 0, 0])

    def test_progress_window_keeps_the_recorded_build_headroom(self) -> None:
        row = self.reservation()
        row["queue_waiter"] = {"pid": 100, "start_time": "100", "need": row["need"], "since": 0,
            "need_basis": gate.need_basis_record(gate.ShapeLargest(1, factor=gate.DevSizeHeadroom(1.25, GIB // 2)))}
        reading = gate.MeasuredMemory(4 * GIB, anonymous=gate.AnonymousRunUse(GIB))
        adjusted = gate.progress_memory(row, reading, gate.AttributedStepUse(reading.anonymous, True),
            progress.ProgressCounter("build:1", 80, 100), self.profile("build:1", 4 * GIB), admission_policy_fixture())
        self.assertEqual(gate.reservation_need(row, adjusted), 11 * GIB // 2)

    def test_filter_order_reuses_the_same_progress_history(self) -> None:
        first = self.reservation("nextest")
        first["filters"] = ["test(z)", "test(a)"]
        second: gate.Reservation = {**first, "filters": list(reversed(first["filters"]))}
        self.assertEqual(gate.reservation_profile_key(first, "host"), gate.reservation_profile_key(second, "host"))

    def test_a_late_merge_gate_profile_lowers_only_the_remaining_keep_aside(self) -> None:
        row = self.reservation("merge-gate", need=4 * GIB)
        reading = gate.MeasuredMemory(9 * GIB, swap_bytes=5 * GIB,
            anonymous=gate.AnonymousRunUse(GIB))
        adjusted = gate.progress_memory(row, reading, gate.AttributedStepUse(reading.anonymous, True),
            progress.ProgressCounter("suites:1", 8, 10), self.profile("suites:1", 3 * GIB // 2),
            admission_policy_fixture())
        room = gate.inputs_room(self.inputs((row,), (adjusted,)), admission_policy_fixture())
        assert isinstance(room, gate.MemoryRoom)
        self.assertEqual(room.used, GIB)
        self.assertEqual(room.kept, GIB // 2)
        self.assertEqual(gate.reservation_need(row, reading), 4 * GIB)
        self.assertEqual(row["need"], 4 * GIB)
        text = gate.progress_text(adjusted)
        self.assertIn("suites:1, 80% done", text)
        self.assertIn("need 1.5 GiB", text)
        self.assertIn("highest anonymous use in the next 20%", text)

    def test_gate_profiles_separate_types_and_share_test_only_copies(self) -> None:
        first = self.reservation("merge-gate")
        first["shape"] = gate.merge_gate_shape(["merge-gate"])
        second: gate.Reservation = {**first, "worktree": "/another/gate"}
        nightly: gate.Reservation = {**second, "shape": gate.merge_gate_shape(["merge-gate", "--type-dirs", "scripts"])}

        self.assertEqual(gate.reservation_profile_key(first, "host"),
                         gate.reservation_profile_key(second, "host"))
        self.assertNotEqual(gate.reservation_profile_key(second, "host"),
                            gate.reservation_profile_key(nightly, "host"))

    def test_missing_progress_or_anonymous_use_keeps_the_full_need(self) -> None:
        row = self.reservation()
        counter = progress.ProgressCounter("build:1", 80, 100)
        profile = self.profile("build:1", GIB)
        cases = (
            (progress.NoCounter(), profile, gate.AnonymousRunUse(2 * GIB), 8 * GIB),
            (counter, progress.NoMatchingProfile(), gate.AnonymousRunUse(2 * GIB), 8 * GIB),
            (progress.ProgressCounter("build:2", 0, 100), profile, gate.AnonymousRunUse(2 * GIB), 8 * GIB),
            (counter, profile, gate.RunUseUnavailable("anonymous counter missing"), 10 * GIB),
        )
        for current, history, use, kept in cases:
            with self.subTest(counter=current, history=history, use=use):
                reading = gate.MeasuredMemory(20 * GIB, anonymous=use)
                adjusted = gate.progress_memory(row, reading, gate.AttributedStepUse(use, True),
                    current, history, admission_policy_fixture())
                self.assertEqual(gate.reservation_need(row, adjusted), 10 * GIB)
                room = gate.inputs_room(self.inputs((row,), (adjusted,)), admission_policy_fixture())
                assert isinstance(room, gate.MemoryRoom)
                self.assertEqual(room.kept, kept)

    def test_matched_test_progress_replaces_baseline_but_the_growing_peak_keeps_its_frozen_margin(self) -> None:
        row = self.reservation("nextest", need=20 * GIB)
        baseline = gate.TestPhaseNeed(8 * GIB, 5, 8, 8, "same_width", margin=1.25)
        profile = self.profile("test:1", 3 * GIB)
        for peak, expected in ((2 * GIB, 15 * GIB // 4), (10 * GIB, 25 * GIB // 2)):
            with self.subTest(peak=peak):
                reading = gate.MeasuredMemory(20 * GIB, anonymous=gate.AnonymousRunUse(GIB),
                    peak_history=gate.TestPhaseStarted(90, peak, baseline))
                adjusted = gate.progress_memory(row, reading, gate.AttributedStepUse(reading.anonymous, True),
                    progress.ProgressCounter("test:1", 80, 100), profile, admission_policy_fixture())
                self.assertEqual(gate.reservation_need(row, adjusted), expected)
                self.assertEqual(adjusted.peak_history, gate.TestPhaseStarted(90, peak, baseline))

    def test_test_phase_reset_and_missing_counter_restore_the_frozen_baseline_and_margin(self) -> None:
        row = self.reservation("nextest", need=20 * GIB)
        baseline = gate.TestPhaseNeed(8 * GIB, 5, 8, 8, "same_width", margin=1.25)
        reading = gate.MeasuredMemory(20 * GIB, anonymous=gate.AnonymousRunUse(GIB),
            peak_history=gate.TestPhaseStarted(90, 2 * GIB, baseline))
        for counter in (progress.NoCounter(), progress.ProgressCounter("test:2", 0, 100)):
            with self.subTest(counter=counter):
                profile = self.profile("test:2", GIB)
                adjusted = gate.progress_memory(row, reading, gate.AttributedStepUse(reading.anonymous, True),
                    counter, profile, admission_policy_fixture())
                self.assertEqual(gate.reservation_need(row, adjusted), 10 * GIB)
                self.assertEqual(gate.reservation_need(row, reading), 10 * GIB)

    def test_shared_process_use_is_counted_once_before_distinct_step_profiles_are_grouped(self) -> None:
        rows = (self.reservation("build", identity="z-parent"), self.reservation("clippy", identity="a-child"))
        source = gate.ProcessSource((gate.ProcessAnonymousUse(100, gate.AnonymousRunUse(3 * GIB)),))
        shared = gate.MeasuredMemory(20 * GIB, swap_bytes=9 * GIB,
            anonymous=gate.AnonymousRunUse(3 * GIB), anonymous_source=source)
        readings = (shared, shared)
        inputs = self.inputs(rows, readings)
        attributed = tuple(item[2] for item in gate.attributed_step_uses(inputs))
        self.assertEqual(attributed, (gate.AttributedStepUse(gate.AnonymousRunUse(0), False),
                                     gate.AttributedStepUse(gate.AnonymousRunUse(3 * GIB), True)))
        adjusted = tuple(gate.progress_memory(row, reading, use,
            progress.ProgressCounter("build:1", 80, 100), self.profile("build:1", peak), admission_policy_fixture())
            for row, reading, use, peak in zip(rows, readings, attributed, (2 * GIB, 4 * GIB), strict=True))
        room = gate.inputs_room(self.inputs(rows, adjusted), admission_policy_fixture())
        assert isinstance(room, gate.MemoryRoom)
        hold, = room.runs
        self.assertEqual(hold.memory.need, 6 * GIB)
        self.assertEqual(hold.memory.use, gate.AnonymousRunUse(3 * GIB))
        self.assertEqual(hold.bytes, 3 * GIB)


class ProcessRunUseTests(unittest.TestCase):
    @cached_property
    def fixture(self) -> AdmissionFixture:
        return self.enterContext(AdmissionFixture())

    @override
    def setUp(self) -> None:
        self.process(100, parent=1, resident=4 * GIB, anonymous=2 * GIB, swap=GIB)
        self.process(101, parent=100, resident=2 * GIB, anonymous=GIB, swap=GIB // 2)
        self.process(200, parent=1, resident=20 * GIB, anonymous=15 * GIB, swap=8 * GIB)

    def process(self, pid: int, *, parent: int, resident: int, anonymous: int, swap: int) -> None:
        write_process(self.fixture.proc.path, pid, resident, parent=parent,
                      anonymous=anonymous, swap=swap)

    def reservation(self, name: str, pid: int, *, scope: str = "", need: int = 6 * GIB) -> Path:
        sidecar = ""
        if scope:
            path = self.fixture.root / f"{name}.cgroup"
            _ = path.write_text(scope)
            sidecar = str(path)
        row: dict[str, object] = {
            "pid": pid, "start_time": str(pid), "need": need, "repo": "test", "step": "merge-gate",
            "worktree": "/tmp/fixture", "sidecar": sidecar,
            "admitted_at": store.utc_iso(self.fixture.clock.now() - 180),
            "test_threads": None, "call_id": None, "reservation_id": name,
        }
        path = self.fixture.state_dir / f"{name}.reservation"
        _ = path.write_text(json.dumps(row))
        return path

    def captured(self) -> gate.QueueInputs:
        return fixture_queue_inputs(self.fixture.state_dir, gate.process_table(self.fixture.proc.path),
                                    self.fixture.clock.now(), admission_policy_fixture())

    def room(self) -> gate.MemoryRoom:
        inputs = self.captured()
        room = gate.inputs_room(inputs, admission_policy_fixture())
        assert isinstance(room, gate.MemoryRoom)
        return room

    def scope(self) -> Path:
        sidecar = self.fixture.scope(wire.Owner(100, "100"), folder="builds.slice/holder.scope",
            members=(100, 101), current=10 * GIB, anonymous=5 * GIB, swap=3 * GIB)
        return Path(sidecar.read_text())

    def test_unscoped_run_uses_only_its_tree_anonymous_memory_for_the_cap_and_keep_aside(self) -> None:
        _ = self.reservation("holder", 100)
        inputs = self.captured()
        reading = inputs.running[0].measured
        assert isinstance(reading, gate.MeasuredMemory)
        self.assertEqual((reading.bytes, reading.swap_bytes), (6 * GIB, 3 * GIB // 2))
        self.assertEqual(reading.anonymous, gate.AnonymousRunUse(3 * GIB))
        self.assertEqual(reading.anonymous_source, gate.ProcessSource((
            gate.ProcessAnonymousUse(100, gate.AnonymousRunUse(2 * GIB)),
            gate.ProcessAnonymousUse(101, gate.AnonymousRunUse(GIB)))))
        room = self.room()
        self.assertEqual(room.used, 3 * GIB)
        self.assertEqual(room.kept, 3 * GIB)
        self.assertEqual(room.cap, room.available + 3 * GIB - room.safety)
        self.assertEqual(room.room, room.available - room.safety - 3 * GIB)

    def test_scoped_run_uses_memory_stat_instead_of_process_anonymous_memory(self) -> None:
        scope = self.scope()
        _ = (self.fixture.proc.path / "100/status").write_text("VmRSS: 4194304 kB\nVmSwap: 1048576 kB\n")
        _ = self.reservation("scoped", 100, scope=str(scope))
        reading = self.captured().running[0].measured
        assert isinstance(reading, gate.MeasuredMemory)
        self.assertEqual(reading.anonymous_source, gate.ScopeSource(scope, frozenset({100, 101})))
        room = self.room()
        self.assertEqual(room.used, 5 * GIB)
        self.assertEqual(room.kept, GIB)
        self.assertEqual(room.cap, room.available + 5 * GIB - room.safety)

    def test_missing_or_invalid_process_anonymous_memory_keeps_the_run_unavailable(self) -> None:
        _ = self.reservation("holder", 100)
        status = self.fixture.proc.path / "101/status"
        for content in ("VmRSS: 2097152 kB\nVmSwap: 524288 kB\n",
                        "VmRSS: 2097152 kB\nRssAnon: invalid kB\n", "VmRSS: 2097152 kB\nRssAnon: -1 kB\n"):
            with self.subTest(status=content):
                status.unlink(missing_ok=True)
                _ = status.write_text(content)
                room = self.room()
                self.assertIsInstance(room.cap, gate.CapUnavailable)
                hold, = room.runs
                self.assertIsInstance(hold.memory.use, gate.RunUseUnavailable)
                assert isinstance(hold.memory.use, gate.RunUseUnavailable)
                self.assertIn(str(status), hold.memory.use.reason)
                self.assertEqual(hold.bytes, 6 * GIB)

    def test_a_missing_or_zombie_child_adds_no_use_and_keeps_the_cap_available(self) -> None:
        _ = self.reservation("holder", 100)
        status = self.fixture.proc.path / "101/status"
        for content in (None, "Name: fixture\nState: Z (zombie)\n"):
            with self.subTest(status=content):
                status.unlink(missing_ok=True)
                if content is not None:
                    _ = status.write_text(content)
                room = self.room()
                self.assertEqual(room.used, 2 * GIB)
                self.assertEqual(room.kept, 4 * GIB)
                self.assertEqual(room.cap, room.available + 2 * GIB - room.safety)

    def test_a_denied_status_keeps_the_run_unavailable_and_names_its_file(self) -> None:
        _ = self.reservation("holder", 100)
        denied = self.fixture.proc.path / "101/status"
        read_text = Path.read_text

        def read_private(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == denied:
                raise PermissionError(13, "Permission denied", str(path))
            return read_text(path, encoding=encoding, errors=errors)

        with patch.object(Path, "read_text", read_private):
            room = self.room()
        self.assertIsInstance(room.cap, gate.CapUnavailable)
        hold, = room.runs
        assert isinstance(hold.memory.use, gate.RunUseUnavailable)
        self.assertIn(str(denied), hold.memory.use.reason)
        self.assertIn("holder", queue_evidence.room_text(room))

    def test_a_scope_child_removed_during_the_walk_does_not_hide_live_use(self) -> None:
        scope = self.scope()
        child = scope / "ended.scope"
        child.mkdir()
        _ = (child / "cgroup.procs").write_text("200\n")
        read_text = Path.read_text

        def remove_child(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == child / "cgroup.procs":
                path.unlink()
                child.rmdir()
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return read_text(path, encoding=encoding, errors=errors)

        with patch.object(Path, "read_text", remove_child):
            reading = gate.process_memory(gate.PopulatedStepScope(scope), self.fixture.proc.path)
        self.assertEqual(reading.anonymous, gate.AnonymousRunUse(5 * GIB))
        self.assertEqual(reading.anonymous_source, gate.ScopeSource(scope, frozenset({100, 101})))

    def test_two_reservations_over_one_tree_count_anonymous_use_once(self) -> None:
        _ = self.reservation("first", 100)
        for pid in (100, 101):
            with self.subTest(second_root=pid):
                _ = self.reservation("second", pid)
                room = self.room()
                self.assertEqual(room.used, 3 * GIB)
                self.assertEqual(sum(run.memory.need for run in room.runs), 12 * GIB)
                self.assertEqual(room.kept, 9 * GIB)
                self.assertEqual(room.cap, room.available + 3 * GIB - room.safety)

    def test_tree_inside_a_read_scope_counts_anonymous_use_once_in_either_order(self) -> None:
        scope = self.scope()
        for scoped_first in (True, False):
            with self.subTest(scoped_first=scoped_first):
                for path in self.fixture.state_dir.glob("*.reservation"):
                    path.unlink()
                _ = self.reservation("a" if scoped_first else "b", 100, scope=str(scope))
                _ = self.reservation("b" if scoped_first else "a", 101)
                room = self.room()
                self.assertEqual(room.used, 5 * GIB)
                self.assertEqual(room.kept, 7 * GIB)
                self.assertEqual(room.cap, room.available + 5 * GIB - room.safety)

    def test_nested_and_equal_readings_assign_shared_use_to_the_child_in_both_orders(self) -> None:
        self.process(100, parent=1, resident=4 * GIB, anonymous=0, swap=0)
        self.process(101, parent=100, resident=4 * GIB, anonymous=4 * GIB, swap=0)
        for kind in ("process tree", "scope tree", "same scope", "same process set"):
            with self.subTest(kind=kind):
                for path in self.fixture.state_dir.glob("*.reservation"):
                    path.unlink()
                parent_scope = child_scope = ""
                child_pid = 101
                if kind in ("scope tree", "same scope"):
                    parent_sidecar = self.fixture.scope(wire.Owner(100, "100"), folder="shared.scope",
                        members=(100, 101), current=4 * GIB, anonymous=4 * GIB)
                    parent_scope = parent_sidecar.read_text()
                    if kind == "scope tree":
                        child_sidecar = self.fixture.scope(wire.Owner(101, "101"), folder="shared.scope/child.scope",
                            members=(101,), current=4 * GIB, anonymous=4 * GIB)
                        child_scope = child_sidecar.read_text()
                    else:
                        child_scope = parent_scope
                elif kind == "same process set":
                    child_pid = 100
                _ = self.reservation("z-parent", 100, scope=parent_scope, need=2 * GIB)
                _ = self.reservation("a-child", child_pid, scope=child_scope, need=6 * GIB)
                inputs = self.captured()
                head: gate.Waiter = {"pid": 300, "start_time": "300", "since": inputs.now,
                    "need": GIB, "step": "check", "folder": "/tmp/waiter"}
                decisions: list[gate.AdmissionDecision] = []
                for reverse in (False, True):
                    ordered_inputs = replace(inputs, host=gate.HostMemory(8 * GIB, 60 * GIB),
                        reservations=tuple(reversed(inputs.reservations)) if reverse else inputs.reservations,
                        running=tuple(reversed(inputs.running)) if reverse else inputs.running,
                        ordered=[(Path("waiter.waiting"), gate.rank_waiter(head, gate.NormalOrder()))])
                    result = gate.admission_pass(ordered_inputs, inputs.now, admission_policy_fixture())
                    room = result.policy.room
                    assert isinstance(room, gate.MemoryRoom)
                    holds = {hold.memory.owner: hold for hold in room.runs}
                    self.assertEqual(holds["a-child"].memory.use, gate.AnonymousRunUse(4 * GIB))
                    self.assertEqual(holds["z-parent"].memory.use, gate.AnonymousRunUse(0))
                    self.assertEqual({owner: hold.bytes for owner, hold in holds.items()},
                                     {"a-child": 2 * GIB, "z-parent": 2 * GIB})
                    self.assertEqual(room.used, 4 * GIB)
                    self.assertEqual(room.kept, 4 * GIB)
                    self.assertEqual(result.rows[0].decision.state, "admit")
                    decisions.append(result.rows[0].decision)
                self.assertEqual(decisions[0], decisions[1])

    def test_unowned_scope_subtracts_tree_resident_bytes_and_swap_separately(self) -> None:
        _ = self.scope()
        path = self.reservation("holder", 100)
        readings = gate.unowned_slice_memory([gate.read_reservation(path)],
            gate.process_table(self.fixture.proc.path), self.fixture.proc.path)
        reading, = readings
        self.assertEqual(reading.bytes, 4 * GIB)
        self.assertEqual(reading.swap_bytes, 3 * GIB // 2)

    def test_queue_place_sampler_keeps_resident_bytes_separate_from_anonymous_use_and_swap(self) -> None:
        import threading
        from scripts.build_queue import queue_place as queue

        sidecar = self.fixture.root / "sample.cgroup"
        _ = sidecar.write_text("")
        path = self.fixture.root / "sample"

        class SingleSampleQueuePlace(queue.QueuePlace):
            def sample_once(self) -> None:
                stopped = threading.Event()
                stopped.set()
                self._sample(queue.ProgramTree(100), stopped)

        place = SingleSampleQueuePlace(path, sidecar, configuration=admission_configuration_fixture())
        with patch.object(queue, "program_members", return_value={100: "100", 101: "101"}):
            place.sample_once()
        self.assertEqual(place.peak_bytes, 6 * GIB)
        self.assertEqual(int(path.with_suffix(".max").read_text()), 6 * GIB)

    def test_merge_gate_context_holder_without_a_scope_gets_a_cap_value(self) -> None:
        from scripts.build_queue.queue_place import queue_place

        self.fixture.configure(**{"program_first_peak_gib.merge-gate": 6})
        pid = os.getpid()
        owner = self.fixture.fixture_owner(pid, str(pid))
        self.process(pid, parent=1, resident=4 * GIB, anonymous=2 * GIB, swap=GIB)
        with patch.object(gate, "process_start_time", return_value=owner.start_time), self.fixture.serving():
            with queue_place("merge-gate", self.fixture.root):
                with self.fixture.loop_turn():
                    _ = self.fixture.run_pass()
                    records = list(self.fixture.server.reservations.values())
                    self.assertEqual(len(records), 1)
                    record = records[0]
                    path, row = Path(record.path), service.reservation_row(record)
                    self.assertEqual(row["pid"], pid)
                    self.assertEqual(row["step"], "merge-gate")
                    self.assertEqual(Path(row["sidecar"]).read_text(), "")
                    reading = gate.reservation_memory(path, row,
                        datetime.fromtimestamp(self.fixture.clock.now(), UTC),
                        gate.process_table(self.fixture.proc.path), self.fixture.proc.path, admission_policy_fixture())
                    assert isinstance(reading, gate.MeasuredMemory)
                    self.assertEqual(reading.anonymous, gate.AnonymousRunUse(2 * GIB))
                    reply, = self.fixture.server.handle_request(wire.ExplainRequest())
                    assert isinstance(reply, wire.ExplainReply)
                    room = reply.explanation.room
                    assert isinstance(room, gate.MemoryRoom)
                    self.assertEqual(room.cap, room.available + 2 * GIB - room.safety)
                    self.assertEqual(room.kept, max(0, row["need"] - 2 * GIB))


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
                _ = (path / "status").write_text(
                    f"VmRSS: {resident} kB\nRssAnon: {resident // 2} kB\nVmSwap: {swap} kB\n")
            processes = gate.process_table(proc)
            self.assertEqual(gate.tree_memory(100, "start", processes, proc),
                             gate.MeasuredMemory(60 * 1024, swap_bytes=9 * 1024,
                                 anonymous=gate.AnonymousRunUse(30 * 1024), anonymous_source=gate.ProcessSource((
                                     gate.ProcessAnonymousUse(100, gate.AnonymousRunUse(5 * 1024)),
                                     gate.ProcessAnonymousUse(101, gate.AnonymousRunUse(10 * 1024)),
                                     gate.ProcessAnonymousUse(102, gate.AnonymousRunUse(15 * 1024))))))
            self.assertEqual(gate.tree_memory(100, "reused", processes, proc), gate.GoneMemory())
            self.assertEqual(gate.tree_memory(999, "gone", processes, proc), gate.GoneMemory())

    def test_swap_release_does_not_make_a_reservation_the_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / 'proc'
            for pid, start in ((100, 'holder'), (200, 'other'), (400, 'waiter')):
                process = proc / str(pid)
                process.mkdir(parents=True)
                fields = ['S', '1', *['0'] * 17, start]
                _ = (process / 'stat').write_text(f'{pid} (test process) ' + ' '.join(fields))
            _ = (proc / '100/status').write_text(f'VmRSS: {GIB // 1024} kB\nVmSwap: {15 * GIB // 1024} kB\n')
            _ = (proc / '100/environ').write_bytes(b'CLAUDE_CODE_SESSION_ID=owner-id\x00CLAUDE_SESSION_NAME=owner-name\x00')
            directory = gate.admission_dir(root)
            reservation: dict[str, object] = {'pid': 100, 'start_time': 'holder', 'need': 16 * GIB, 'repo': 'test', 'step': 'check', 'worktree': 'test', 'sidecar': '', 'admitted_at': store.utc_iso(NOW.timestamp() - 120), 'test_threads': None, 'call_id': None}
            _ = (directory / '100.reservation').write_text(json.dumps(reservation))
            _ = (proc / '200/status').write_text(f'VmRSS: {GIB // 1024} kB\nVmSwap: 0 kB\n')
            other = dict(reservation, pid=200, start_time='other', need=GIB)
            _ = (directory / '200.reservation').write_text(json.dumps(other))
            waiter = directory / '400-waiter.waiting'
            _ = waiter.write_text(json.dumps({'pid': 400, 'start_time': 'waiter', 'need': 12 * GIB, 'since': NOW.timestamp()}))
            environment = fake_memory_readings(root, available_kib=2 * GIB // 1024, total_kib=60 * GIB // 1024)
            config = root / 'build_admission.conf'
            write_memory_config(config)
            environment.update({'BUILDLOG_DIR': str(root), 'BUILDLOG_PROC': str(proc), 'LINT_CONFIG_FILE': str(config)})
            with patch.dict(os.environ, environment), patch.object(time, 'time', return_value=NOW.timestamp()):
                inputs = fixture_queue_inputs(directory, gate.process_table(proc), NOW_EPOCH, admission_policy_fixture())
                inputs = replace(inputs, blockers_for=frozenset({"400-waiter"}))
                result = gate.admission_pass(inputs, NOW_EPOCH, admission_policy_fixture())
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
            )
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
        running = (gate.RunningStep("one", "first", "nextest", "/tmp/one", 0, gate.MeasuredMemory(GIB), pid=123),
                   gate.RunningStep("two", "second", "clippy", "/tmp/two", 0, gate.MeasuredMemory(2 * GIB), pid=123))
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
        decision = gate.AdmissionDecision("hold", 19_434_727_014, 0, 3 * GIB, 0,
            room=gate.RoomCheck(16_213_501_542, 15 * GIB, False, basis=gate.DoesNotFit()))
        pressure_reason = pressure
        head_reason = gate.HeadAdmissionStatus(decision, 16_213_501_542,
            18 * GIB, cause=gate.NeedsMemory(16_213_501_542, 18 * GIB))
        for periodic in (False, True):
            for reason, expected in ((pressure_reason, "memory pressure: full avg10 11.0"),
                                     (head_reason, "needs 15.1 GiB; room 15.0 GiB")):
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
        self.admission.hold_starts()
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
        snapshot = gate.AdmissionWaitSnapshot(queue_decision(0, 60 * GIB, peak.bytes, [], policy=admission_policy_fixture()), peak, (), 0, "repo", "nextest",
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
        self.admission.hold_starts()
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
        self.admission.hold_starts()
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
        _ = py.write_text((Path(__file__).resolve().parents[1] / "lib/py").read_text())
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
        self.environment["TEST_PY"] = str(Path(__file__).resolve().parents[1] / "lib/py")
        self.environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])


    def run_step(self, hook: str | None, sleep: str, step: str = 'run_once touch "$2"') -> subprocess.CompletedProcess[str]:
        self.admission.free(0)
        self.admission.hold_starts()
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
                if gate.read_meminfo(self.meminfo).available:
                    _ = (self.root / "pressure").write_text("full avg10=0\nsome avg10=0\n")
            return reply
        if sleep.startswith('printf "MemAvailable:'):
            self.admission.free(60 * GIB)
            _ = (self.root / "pressure").write_text("full avg10=0\nsome avg10=0\n")
        self.admission.server.reply_filter = advance_memory
        command = ('source "$1"; buildlog_memory_python() { '
                   + 'if [[ "$1" == "$BUILDLOG_MEM_ADMIT_SCRIPT" ]]; then shift; '
                   + '"$TEST_PY" -c \'import sys; from scripts.lint.test_memory_admit import queue_leave_client_main; '
                   + 'sys.exit(queue_leave_client_main(sys.argv[1:]))\' "$@"; else "$TEST_PY" "$@"; fi; }; '
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
                + f'printf "%s\\n" "$*" > "{ran_args}"; "$TEST_PY" "$TEST_CAPTURE_WIDTH" > "{reservation}"; }}; '
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
            "BUILDLOG_MEMINFO": str(self.meminfo),
            "BUILDLOG_MEMORY_PRESSURE": str(self.root / "pressure"),
        }))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.meminfo.write_text(f"MemAvailable: {16 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = (self.root / "pressure").write_text('full avg10=0.00 avg60=0.00 avg300=0.00 total=0\nsome avg10=0\n')

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


    def test_host_pressure_prevents_every_follower_from_counting_as_fitting(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {50 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = self.waiting(300, 20 * GIB, 20, estimated_at=NOW_EPOCH - 600)
        _ = self.waiting(400, GIB, 1)
        for full in (0, 11):
            with self.subTest(full=full):
                _ = (self.root / "pressure").write_text(
                    (f"full avg10={full}.00 avg60=0.00 avg300=0.00 total=0\n" + "some avg10=0\n"))
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
        self.assertIsInstance(explanation.head.cause, gate.ReadingUnavailable)
        path.unlink()
        empty = gate.explain_queue()
        self.assertIsInstance(empty.head, gate.NoWaiter)
        self.assertEqual((empty.line.count, empty.line.steps), (0, ()))


    def test_no_reading_yet_names_the_promised_charge(self) -> None:
        self.readings[100] = gate.MeasuredMemory(0, anonymous=gate.AnonymousRunUse(0))
        _ = self.running(100, 8, age=30)
        running = gate.explain_queue().running[0]
        self.assertEqual(running.promise, 8 * GIB)
        self.assertEqual((running.promise, running.measured, running.charged), (8 * GIB, 0, 8 * GIB))


    def test_real_pressure_still_holds_after_every_running_step_ends(self) -> None:
        _ = self.running(100, 6, age=30)
        _ = self.waiting(300, 4 * GIB, 20)
        _ = (self.root / "pressure").write_text('full avg10=11.00 avg60=0.00 avg300=0.00 total=0\nsome avg10=0\n')
        explanation = gate.explain_queue()
        for result in (gate.head_after_steps_end(explanation, (0,)), gate.smallest_head_release(explanation)):
            with self.subTest(result=result):
                if not isinstance(result, gate.HeadStillHeld):
                    self.fail(f"expected a pressure hold, got {result}")
                self.assertEqual(result.causes, ("pressure",))


    def snapshot_inputs(self) -> gate.QueueInputs:
        return fixture_queue_inputs(gate.admission_dir(self.root), gate.process_table(self.root / "proc"),
                                    NOW_EPOCH, admission_policy_fixture())

    def test_queue_read_from_uses_no_files_processes_or_reader_limits(self) -> None:
        _ = self.running(100, 6)
        _ = self.waiting(300, 12 * GIB, 20)
        _ = self.waiting(400, GIB, 5)
        inputs = self.snapshot_inputs()
        expected = gate.queue_read_from(inputs, policy=admission_policy_fixture())
        with patch.dict(os.environ, {"BUILDLOG_PROC": "/absent"}), \
             patch.object(Path, "open", side_effect=AssertionError("pure read opened file")), \
             patch.object(Path, "read_text", side_effect=AssertionError("pure read read file")), \
             patch.object(gate, "process_table", side_effect=AssertionError("pure read scanned processes")), \
             patch.object(gate, "read_pressure", side_effect=AssertionError("pure read read recovery")):
            self.assertEqual(gate.queue_read_from(inputs, policy=admission_policy_fixture()), expected)


    def test_pass_and_view_share_each_rows_decision_and_reason_in_every_scenario(self) -> None:
        _ = self.running(100, 8)
        self.readings[100] = gate.MeasuredMemory(8 * GIB, anonymous=gate.AnonymousRunUse(8 * GIB))
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
            "recovery": replace(inputs, spacing=gate.NextStartAt(NOW_EPOCH + 30)),
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
                result = gate.admission_pass(evidence, evidence.now, policy=admission_policy_fixture())
                reading = gate.queue_read_from(evidence, policy=admission_policy_fixture())
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
                    self.assertEqual(result.rows[1].reason, gate.StartSpacingWait(NOW_EPOCH + 5))

    def test_pass_is_pure_and_ignores_reader_environment_and_clock(self) -> None:
        _ = self.waiting(300, 4 * GIB, 1)
        _ = self.waiting(400, 5 * GIB, 0)
        inputs = self.snapshot_inputs()
        before = codec.snapshot_value(inputs)
        expected = gate.admission_pass(inputs, NOW_EPOCH, policy=admission_policy_fixture())
        with patch.dict(os.environ, {
                "BUILDLOG_PROC": str(self.root / "empty-proc"),
                "BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(self.root / "tripwire.jsonl")}), \
             patch.object(Path, "read_text", side_effect=AssertionError("pass read a file")), \
             patch.object(Path, "write_text", side_effect=AssertionError("pass wrote a file")), \
             patch.object(gate, "process_table", side_effect=AssertionError("pass scanned processes")), \
             patch.object(gate, "read_meminfo", side_effect=AssertionError("pass read host memory")), \
             patch.object(gate, "read_pressure", side_effect=AssertionError("pass read recovery")), \
             patch.object(time, "time", side_effect=AssertionError("pass read the clock")):
            self.assertEqual(gate.admission_pass(inputs, NOW_EPOCH, policy=admission_policy_fixture()), expected)
        self.assertEqual(codec.snapshot_value(inputs), before)

    def test_one_walk_decides_all_rows_and_each_requested_ending_is_one_more_scenario(self) -> None:
        _ = self.running(100, 18, age=30)
        _ = self.running(200, 1, age=30)
        _ = self.waiting(300, 10 * GIB, 20)
        _ = self.waiting(400, 4 * GIB, 5)
        inputs = replace(self.snapshot_inputs(), host=gate.HostMemory(16 * GIB, 60 * GIB))
        with patch.object(gate, "admission_walk", wraps=gate.admission_walk) as walk:
            _ = gate.queue_read_from(inputs, policy=admission_policy_fixture())
        self.assertEqual(walk.call_count, 1)
        with patch.object(gate, "admission_walk", wraps=gate.admission_walk) as walk:
            result = gate.admission_pass(replace(inputs, blockers_for=frozenset(("300-head",))), NOW_EPOCH, policy=admission_policy_fixture())
        self.assertEqual(walk.call_count, 1 + len(inputs.running))
        self.assertEqual(result.rows[0].blocker, gate.BlockingIndex(0))
        self.assertIsInstance(result.rows[1].blocker, gate.BlockerNotRequested)
        requested = replace(inputs, blockers_for=frozenset(("300-head", "400-warmup")))
        with patch.object(gate, "admission_walk", wraps=gate.admission_walk) as walk:
            result = gate.admission_pass(requested, NOW_EPOCH, policy=admission_policy_fixture())
        self.assertEqual(walk.call_count, 1 + len(inputs.running))
        self.assertEqual(result.rows[0].blocker, gate.BlockingIndex(0))
        self.assertIsInstance(result.rows[1].blocker, gate.NoSingleBlocker)
        pressure_held = gate.admission_pass(replace(requested, pressure=gate.MemoryPressure(8, condition="full")), NOW_EPOCH, policy=admission_policy_fixture())
        self.assertTrue(all(isinstance(row.blocker, gate.NoSingleBlocker) for row in pressure_held.rows))

    def test_two_proposals_charge_the_first_need_to_the_second(self) -> None:
        _ = self.waiting(300, 4 * GIB, 1)
        _ = self.waiting(400, 5 * GIB, 0)
        inputs = replace(self.snapshot_inputs(), host=gate.HostMemory(60 * GIB, 60 * GIB))
        result = gate.admission_pass(inputs, NOW_EPOCH, policy=admission_policy_fixture())
        self.assertEqual([row.decision.state for row in result.rows], ["admit", "hold"])
        self.assertEqual([row.decision.charged for row in result.rows], [0, 0])
        self.assertEqual(result.rows[1].reason, gate.StartSpacingWait(NOW_EPOCH + 5))
        self.assertEqual([row.admission.rank for row in result.rows
                          if isinstance(row.admission, gate.ProposedAdmission)], [1])


    def test_expired_estimate_keeps_its_room_and_blocks_a_younger_fit(self) -> None:
        _ = self.waiting(300, 18 * GIB, 1, estimated_at=NOW_EPOCH - admission_policy_fixture().timing.estimate_refresh_seconds)
        _ = self.waiting(400, 5 * GIB, 0)
        inputs = replace(self.snapshot_inputs(), host=gate.HostMemory(22 * GIB, 60 * GIB))
        result = gate.admission_pass(inputs, NOW_EPOCH, policy=admission_policy_fixture())
        self.assertEqual((result.rows[0].reason.cause if isinstance(result.rows[0].reason, gate.HeadAdmissionStatus) else result.rows[0].reason), gate.EstimateExpired(NOW_EPOCH - admission_policy_fixture().timing.estimate_refresh_seconds))
        self.assertEqual([row.decision.state for row in result.rows], ["hold", "hold"])
        self.assertIsInstance(result.rows[1].reason, gate.BehindHead)
        self.assertEqual(result.rows[1].decision.charged, 0)
        self.assertTrue(all(isinstance(row.admission, gate.NotAdmitted) for row in result.rows))

    def test_estimate_expiry_boundary_agrees_between_the_walk_and_the_pass_reason(self) -> None:
        _ = self.waiting(300, GIB, 1)
        inputs = self.snapshot_inputs()
        path, waiter = inputs.ordered[0]
        for age in (None, -1, admission_policy_fixture().timing.estimate_refresh_seconds - 0.001, admission_policy_fixture().timing.estimate_refresh_seconds, admission_policy_fixture().timing.estimate_refresh_seconds + 1):
            with self.subTest(age=age):
                row = waiter.copy()
                if age is not None:
                    row["estimated_at"] = NOW_EPOCH - age
                expired = age is not None and age >= admission_policy_fixture().timing.estimate_refresh_seconds
                self.assertEqual(gate.estimate_expired(row, NOW_EPOCH, policy=admission_policy_fixture()), expired)
                result = gate.admission_pass(replace(inputs, ordered=[(path, row)]), NOW_EPOCH, policy=admission_policy_fixture()).rows[0]
                self.assertEqual(result.decision.state, "hold" if expired else "admit")
                assert isinstance(result.reason, gate.HeadAdmissionStatus)
                self.assertEqual(isinstance(result.reason.cause, gate.EstimateExpired), expired)
                self.assertEqual(isinstance(result.admission, gate.NotAdmitted), expired)

    def test_release_amount_and_counterfactual_free_memory_use_only_resident_bytes(self) -> None:
        _ = self.running(100, 6)
        _ = self.running(200, 5)
        self.readings[100] = gate.MeasuredMemory(6 * GIB, swap_bytes=2 * GIB, anonymous=gate.AnonymousRunUse(6 * GIB))
        self.readings[200] = gate.MeasuredMemory(GIB, swap_bytes=2 * GIB, anonymous=gate.AnonymousRunUse(GIB))
        _ = self.waiting(300, GIB, 1)
        inputs = self.snapshot_inputs()
        compiler = gate.RunningStep("", "sccache.service", "compile", "builds.slice", 0,
                                    gate.CompilerServiceMemory(3 * GIB, swap_bytes=GIB, pid=gate.PidNotRecorded()), pid=gate.PidNotRecorded())
        inputs = replace(inputs, running=(*inputs.running, compiler))
        explanation = gate.queue_read_from(inputs, policy=admission_policy_fixture()).explanation
        for ended, expected in (((), 0), ((0,), 6 * GIB), ((1,), GIB), ((2,), 3 * GIB), ((0, 1, 2), 10 * GIB)):
            with self.subTest(ended=ended):
                self.assertEqual(gate.released_resident_bytes(inputs.running, frozenset(ended)), expected)
                changed = gate.inputs_after_steps_end(inputs, frozenset(ended), admission_policy_fixture())
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
        reading = gate.queue_read_from(self.snapshot_inputs(), admission_policy_fixture())
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        follower = reading.explanation.line.steps[0]
        self.assertEqual(head.decision.state, "admit")
        self.assertEqual(follower.decision.state, "hold")
        self.assertFalse(follower.fits_now)
        self.assertTrue(follower.room.passes)
        self.assertEqual(follower.decision.charged, 0)
        self.assertEqual(follower.waiter.reason, gate.StartSpacingWait(NOW_EPOCH + 5))
        self.assertEqual(follower.room.need, 10 * GIB)
        self.assertEqual((follower.room, follower.free),
                         gate.decision_checks(follower.decision, 10 * GIB, 60 * GIB))
        self.assertEqual((head.room, head.free), gate.decision_checks(head.decision, 15 * GIB, 60 * GIB))

    def test_waiter_and_running_identity_matching_and_place_boundaries(self) -> None:
        _ = self.waiting(300, 12 * GIB, 20)
        _ = self.waiting(400, GIB, 5)
        view = gate.queue_read_from(self.snapshot_inputs(), admission_policy_fixture()).view
        for selector, places in (("1", [1]), ("2", [2]), ("0", []), ("3", []),
                                 ("session-400", [2]), ("worker-400", [2]),
                                 ("/tmp/tree-400", [2]), ("tree-400", [2]), ("missing", [])):
            with self.subTest(selector=selector):
                self.assertEqual(gate.match_waiters(selector, view), places)
        waiter = view.waiting[1]
        running = gate.RunningStep(waiter.session_id, waiter.session_name, waiter.step, waiter.folder,
                                   waiter.need, gate.MeasuredMemory(GIB), pid=waiter.pid)
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
                    self.assertEqual(codec.parse_snapshot_value(codec.snapshot_value(inputs), gate.QueueInputs), inputs)

    def test_active_compiler_memory_keeps_its_variant_in_a_lock_free_snapshot(self) -> None:
        _ = self.waiting(300, 18 * GIB, 1)
        inputs = replace(self.snapshot_inputs(), running=(gate.RunningStep("", "sccache.service", "compile",
            "builds.slice", 0, gate.CompilerServiceMemory(2 * GIB, pid=gate.PidNotRecorded()), pid=gate.PidNotRecorded()),))
        restored = codec.parse_snapshot_value(codec.snapshot_value(inputs), gate.QueueInputs)
        assert isinstance(restored, gate.QueueInputs)
        compiler, = restored.running
        self.assertIsInstance(compiler.measured, gate.CompilerServiceMemory)
        assert isinstance(compiler.measured, gate.CompilerServiceMemory)
        self.assertEqual((compiler.session_name, compiler.measured.bytes), ("sccache.service", 2 * GIB))
        reading = gate.queue_read_from(restored, admission_policy_fixture())
        assert isinstance(reading.explanation.head, gate.HeadExplanation)
        self.assertEqual(reading.explanation.head.decision.measured, 0)

    def test_gather_and_publication_never_resolve_or_store_limits(self) -> None:
        inputs = self.snapshot_inputs()
        encoded = codec.snapshot_value(inputs)
        self.assertIsInstance(encoded, dict)
        assert isinstance(encoded, dict)
        for key in ("limits", "budget", "reserve"):
            self.assertNotIn(key, cast(dict[str, object], encoded))


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

    def test_missing_or_invalid_test_margin_uses_the_fallback_and_valid_margin_freezes(self) -> None:
        for value in (None, "nan", "inf", "0.9", "1.4"):
            with self.subTest(value=value):
                self.write_config(test_phase_margin=value)
                with contextlib.redirect_stderr(io.StringIO()):
                    config = gate.read_memory_config()
                expected = 1.4 if value == "1.4" else 1.2
                self.assertEqual(config.test_phase_margin, expected)
                row: dict[str, object] = {"repo_identity": HANA.key, "test_threads": 8}
                sample: dict[str, object] = {**row, "host": "test-host", "step": "nextest",
                    "test_phase_peak_bytes": 3 * GIB, "ended_at": store.utc_iso(NOW_EPOCH - 1)}
                need = gate.test_phase_need_from_rows([sample], row, "test-host", NOW, config.test_phase_margin)
                assert isinstance(need, gate.TestPhaseNeed)
                self.assertEqual(need.margin, expected)
                phase = gate.TestPhaseStarted(NOW_EPOCH, 3 * GIB, need)
                self.assertAlmostEqual(gate.test_phase_charge(phase) / GIB, 3 * expected, places=6)

    def test_unavailable_key_refuses_without_recovery_and_accepts_the_repaired_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "build_admission.conf"
            for key in ("estimate_refresh_seconds", "slot_gap_seconds", "program_first_peak_gib.cargo",
                        "ci_memory_margin_gib"):
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
                        if key == "estimate_refresh_seconds":
                            self.assertEqual(repaired.config.policy.timing.estimate_refresh_seconds, 7)
                        elif key == "slot_gap_seconds":
                            self.assertEqual(repaired.config.policy.timing.slot_gap_seconds, 7)
                        elif key == "ci_memory_margin_gib":
                            self.assertEqual(repaired.config.policy.ci_memory_margin_bytes, 7 * GIB)
                        else:
                            self.assertEqual(repaired.config.program_first_peaks["cargo"], 7 * GIB)

    def test_ci_margin_reads_bytes_and_recovers_invalid_values_like_other_policy_keys(self) -> None:
        for margin in (0, 2.5, 7):
            with self.subTest(margin=margin):
                self.write_config(ci_memory_margin_gib=str(margin))
                config = gate.read_memory_config()
                self.assertEqual(config.policy.ci_memory_margin_bytes, int(margin * GIB))
        for raw in (None, "abc", "-1", "nan", "inf"):
            with self.subTest(raw=raw):
                self.write_config(ci_memory_margin_gib=raw)
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    config = gate.read_memory_config()
                self.assertEqual(config.policy.ci_memory_margin_bytes, 7 * GIB)
                self.assertIn("ci_memory_margin_gib", errors.getvalue())
                self.assertIn("warning:", errors.getvalue())

    def test_cargo_estimate_reads_the_configured_first_peak_at_each_use(self) -> None:
        for peak in (7, 13):
            with self.subTest(peak=peak):
                self.write_config(**{"program_first_peak_gib.cargo": str(peak)})
                result = gate.expected_peak(HANA, "clippy", "test-host", NOW, self.root, first_peak_bytes=gate.read_memory_config().program_first_peaks["cargo"])
                self.assertEqual(result.bytes, peak * GIB)
                self.assertEqual(result.source, "fallback")


    def test_invalid_policy_values_name_the_default_and_the_bad_input(self) -> None:
        baseline = gate.read_memory_config()
        keys = ("slot_gap_seconds", "estimate_refresh_seconds", "recovery_start_seconds",
                "full_pressure_limit", "swap_used_limit", "some_pressure_limit")
        for key in keys:
            for raw in (None, "abc", "-5", "nan", "inf", "0"):
                with self.subTest(key=key, raw=raw):
                    self.write_config(**{key: raw})
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        config = gate.read_memory_config()
                    self.assertEqual(config, baseline)
                    self.assertIn(key, errors.getvalue())
                    self.assertIn("warning:", errors.getvalue())
        for key in keys[:3]:
            self.write_config(**{key: "1.5"})
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(gate.read_memory_config(), baseline)
            self.assertIn(key, errors.getvalue())

    def test_one_config_read_supplies_slot_timing_pressure_and_service_intervals(self) -> None:
        self.write_config(estimate_refresh_seconds="321", slot_gap_seconds="12",
                          recovery_start_seconds="7", full_pressure_limit="9", running_pass_seconds="3")
        with patch.object(conf, "read", wraps=conf.read) as reader:
            config = gate.read_memory_config()
        reader.assert_called_once_with("build_admission")
        self.assertEqual(config.policy.timing.estimate_refresh_seconds, 321)
        self.assertEqual(config.policy.timing.slot_gap_seconds, 12)
        self.assertEqual(config.policy.timing.recovery_start_seconds, 7)
        self.assertEqual(config.policy.pressure.full_avg10_limit, 9)
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

    def test_missing_or_malformed_psi_cannot_supply_a_calm_pressure_reading(self) -> None:
        path = self.root / "pressure"
        policy = gate.read_memory_config().policy
        for source in (None, "", "full avg10=broken\nsome avg10=0\n", "full avg10=0\n"):
            with self.subTest(source=source):
                if source is None:
                    path.unlink(missing_ok=True)
                else:
                    _ = path.write_text(source)
                self.assertIsInstance(gate.read_pressure(policy=policy), gate.PressureUnavailable)

        _ = path.write_text("full avg10=0\nsome avg10=0\n")
        self.assertIsInstance(gate.read_pressure(policy=policy), gate.NoPressureHold)


    def test_counterfactuals_keep_the_policy_that_decided_the_explanation(self) -> None:
        _ = self.running(100, 18, age=30)
        self.readings[100] = gate.MeasuredMemory(8 * GIB, anonymous=gate.AnonymousRunUse(8 * GIB))
        path = self.waiter(300, NOW_EPOCH - 450, "/tmp/head", "head", need=8)
        row = cast(dict[str, object], json.loads(path.read_text()))
        row["estimated_at"] = NOW_EPOCH - 450
        _ = path.write_text(json.dumps(row))
        policy = gate.read_memory_config().policy
        inputs = fixture_queue_inputs(gate.admission_dir(self.root),
            gate.process_table(self.root / "proc"), NOW_EPOCH, policy)
        explanation = gate.queue_read_from(inputs, policy).explanation
        before = (gate.smallest_head_release(explanation), gate.head_after_steps_end(explanation, (0,)))
        self.assertIsInstance(before[0], gate.HeadAdmitsAfterEnd)
        self.write_config(estimate_refresh_seconds="300")
        changed = gate.read_memory_config().policy
        fresh = gate.queue_read_from(inputs, changed).explanation
        self.assertIsInstance(gate.smallest_head_release(fresh), gate.EstimateExpired)
        with patch.object(gate, "read_memory_config", side_effect=AssertionError("frozen policy")), \
             patch.object(gate, "memory_config", side_effect=AssertionError("frozen policy")):
            self.assertEqual((gate.smallest_head_release(explanation),
                              gate.head_after_steps_end(explanation, (0,))), before)
            self.assertEqual(gate.queue_read_from(inputs, policy).explanation, explanation)
        self.assertEqual(explanation.policy.timing.estimate_refresh_seconds, 600)
        self.assertEqual(codec.parse_snapshot_value(codec.snapshot_value(explanation),
            gate.QueueExplanation), explanation)


    def test_changed_estimate_age_applies_to_the_next_check_without_restart(self) -> None:
        path = self.waiter(300, NOW_EPOCH - 450, "/tmp/head", "head", need=8)
        row = gate.read_waiter(path)
        row["estimated_at"] = NOW_EPOCH - 450
        _ = path.write_text(json.dumps(row))
        before = self.pass_for(300, need=8, free=30)
        self.assertEqual(before.decision.state, "admit")
        self.write_config(estimate_refresh_seconds="300")
        after = self.pass_for(300, need=8, free=30)
        self.assertEqual(after.decision.state, "hold")
        self.assertEqual((after.reason.cause if isinstance(after.reason, gate.HeadAdmissionStatus) else after.reason), gate.EstimateExpired(NOW_EPOCH - 450))
        self.assertEqual(gate.read_memory_config().policy.timing.estimate_refresh_seconds, 300)

    def test_warning_boundary_reports_each_fallback_once_and_checker_stays_silent(self) -> None:
        self.write_config(estimate_refresh_seconds="abc", slot_gap_seconds=None)
        with patch.object(records, "_warned_configuration_diagnostics", set[gate.ConfigurationDiagnostic]()), contextlib.redirect_stderr(io.StringIO()) as errors:
            first = gate.read_memory_config(report=False)
            second = gate.read_memory_config(report=False)
            gate.warn_configuration_diagnostics(first.diagnostics)
            gate.warn_configuration_diagnostics(second.diagnostics)
        self.assertEqual(errors.getvalue().count("estimate_refresh_seconds='abc'"), 1)
        self.assertEqual(errors.getvalue().count("slot_gap_seconds='<missing>'"), 1)
        _ = self.running(100, 8)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            checked = self.pass_for(400, need=8, free=0)
        self.assertIn("estimate_refresh_seconds", errors.getvalue())
        self.assertEqual(checked.decision.state, "hold")
        self.assertEqual(gate.read_memory_config().policy.timing.estimate_refresh_seconds, 600)


class ServiceAdmissionRuleTests(unittest.TestCase):
    def test_reservation_stem_keeps_owner_identity_and_admission_time(self) -> None:
        directory = Path("/tmp/private-admission")
        first = gate.reservation_stem(directory, 123, "456", 789)
        second = gate.reservation_stem(directory, 123, "456", 790)
        self.assertEqual(first, directory / "123-456-789")
        self.assertNotEqual(first, second)
        self.assertEqual(first.with_suffix(".max").name, "123-456-789.max")
        self.assertEqual(first.with_suffix(".wave").name, "123-456-789.wave")


    def test_history_program_estimate_refreshes_only_after_its_policy_age(self) -> None:
        policy = admission_policy_fixture()
        profile = gate.ProgramProfile("rust-analyzer", "repo", "tree",
            gate.HistoryPeakSizing(18 * GIB, gate.FirstProgramPeak(18 * GIB), NOW_EPOCH))
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(step_estimator, "program_peak_estimate", return_value=gate.MeasuredProgramPeak(7 * GIB, 9)) as estimate:
            young = gate.step_estimate([], "tree", profile,
                NOW + timedelta(seconds=policy.timing.estimate_refresh_seconds - 1), Path(folder), policy, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
            estimate.assert_not_called()
            aged = gate.step_estimate([], "tree", profile,
                NOW + timedelta(seconds=policy.timing.estimate_refresh_seconds), Path(folder), policy, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
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

    def test_fresh_nextest_estimate_carries_its_cache_policy_to_expiry(self) -> None:
        for fresh in (False, True):
            with self.subTest(fresh=fresh), tempfile.TemporaryDirectory() as folder, \
                 patch.object(step_estimator, "git_identity", return_value=(HANA, "tree")), \
                 patch.object(step_estimator, "is_fresh_test", return_value=fresh), \
                 patch.object(step_estimator, "expected_peak", return_value=gate.ExpectedPeak(2 * GIB, "measured", 5)):
                result = gate.step_estimate(["cargo", "nextest", "run"], "tree",
                    gate.InferFromArgv(), NOW, Path(folder), admission_policy_fixture(), first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
                self.assertIsInstance(result.cache, gate.FreshTest if fresh else gate.ReusableEstimate)
                row: gate.Waiter = {"pid": 123, "start_time": "owner", "need": result.profile.peak.bytes,
                    "since": NOW_EPOCH, "estimated_at": NOW_EPOCH, "fresh_test": fresh}
                interval = admission_policy_fixture().timing.fresh_test_seconds
                self.assertFalse(gate.estimate_expired(row, NOW_EPOCH + interval - 1, admission_policy_fixture()))
                self.assertEqual(gate.estimate_expired(row, NOW_EPOCH + interval, admission_policy_fixture()), fresh)


class CiSliceMemoryTests(unittest.TestCase):
    root: Path = Path()
    ci: Path = Path()
    proc: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = fake_memory_readings(self.root)
        write_memory_config(self.root / "build_admission.conf")
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
        reading, = gate.unowned_slice_memory([], gate.process_table(self.proc), self.proc, ci_memory_slice=config.ci_memory_slice)
        return reading

    def start_ci_job(self) -> None:
        write_process(self.proc, 234)
        _ = (self.proc / "234/comm").write_text("Runner.Worker\n")
        _ = (self.ci / "cgroup.procs").write_text("123\n234\n")

    def test_registered_and_unregistered_jobs_each_enter_the_cap_once(self) -> None:
        for pid, parent, used, program in (
                (234, 1, 0, "Runner.Worker"), (235, 234, 2 * GIB, "cargo"),
                (345, 1, 0, "Runner.Worker"), (346, 345, 4 * GIB, "cargo")):
            write_process(self.proc, pid, used, parent=parent)
            _ = (self.proc / str(pid) / "comm").write_text(program + "\n")
        _ = (self.ci / "cgroup.procs").write_text("123\n234\n235\n345\n346\n")
        _ = (self.ci / "memory.current").write_text(str(6 * GIB))
        _ = (self.ci / "memory.stat").write_text(f"anon {6 * GIB}\ninactive_file 0\n")
        processes = gate.process_table(self.proc)
        policy = records.read_memory_config().policy
        for registered, margin, registered_use, unregistered_use in (
                ((), 7, 0, 6), ((235,), 7, 2, 4), ((235, 346), 0, 6, 0)):
            with self.subTest(registered=registered):
                reservations = [fixture_reservation(pid, 8 * GIB) for pid in registered]
                runs = [gate.RunningStep(str(pid), "CI", "build", str(self.root), 8 * GIB,
                            gate.process_memory(gate.process_tree({pid: str(pid)}, processes), self.proc), pid=pid)
                        for pid in registered]
                readings = gate.unowned_slice_memory(reservations, processes, self.proc,
                    ci_memory_slice=records.read_memory_config().ci_memory_slice)
                runs.extend(gate.RunningStep("CI slice", "CI", "service", str(self.ci), 0, reading,
                    pid=reading.pid) for reading in readings)
                inputs = gate.QueueInputs([], tuple(runs), tuple(reservations), gate.HostMemory(20 * GIB, 64 * GIB),
                    gate.NoPressureHold(), gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), NOW_EPOCH)
                room = gate.inputs_room(inputs, policy)
                self.assertIsInstance(room, records.MemoryRoom)
                if isinstance(room, records.MemoryRoom):
                    self.assertEqual(room.ci_keep_aside.bytes, margin * GIB)
                    self.assertEqual(room.used, registered_use * GIB)
                    self.assertEqual(room.ci_use, unregistered_use * GIB)
                    self.assertEqual(room.cap, 23 * GIB)
                    self.assertEqual(room.kept, (8 * len(registered) - registered_use + margin) * GIB)

    def queue_with_ci(self, reading: gate.StepMemory, available: int = 20 * GIB) -> gate.QueueRead:
        config = gate.read_memory_config()
        step = gate.RunningStep("ci", "ci", "service", "/ci", 0, reading,
            pid=reading.pid if isinstance(reading, (gate.UnownedSliceMemory, gate.CompilerServiceMemory)) else gate.PidNotRecorded())
        inputs = gate.QueueInputs([], (step,), (), gate.HostMemory(available, 64 * GIB),
            gate.NoPressureHold(), gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), NOW_EPOCH)
        return gate.queue_read_from(inputs, config.policy)

    def test_ci_use_counts_only_readable_service_slice_evidence(self) -> None:
        readings = (
            (gate.UnownedSliceMemory(4 * GIB, pid=gate.PidNotRecorded(), job_state=IdleService()), 4 * GIB),
            (gate.UnownedSliceMemory(4 * GIB, pid=gate.PidNotRecorded()), 0),
            (gate.CompilerServiceMemory(4 * GIB, pid=gate.PidNotRecorded()), 0),
        )
        for reading, expected in readings:
            with self.subTest(reading=reading):
                room = self.queue_with_ci(reading).explanation.room
                assert isinstance(room, gate.MemoryRoom)
                self.assertEqual(room.ci_use, expected)
                self.assertEqual(room.cap, 17 * GIB + expected)

    def test_running_ci_job_keeps_one_margin_and_reduces_room_by_seven_gib(self) -> None:
        idle = self.queue_with_ci(self.reading()).explanation.room
        self.start_ci_job()
        reading = self.reading()
        self.assertIsInstance(reading.job_state, RunningServiceJob)
        active = self.queue_with_ci(reading).explanation.room
        assert isinstance(idle, gate.MemoryRoom)
        assert isinstance(active, gate.MemoryRoom)
        self.assertEqual(active.ci_keep_aside.bytes, 7 * GIB)
        self.assertEqual(active.kept, 7 * GIB)
        self.assertEqual(active.room, 10 * GIB)
        self.assertEqual(idle.room - active.room, 7 * GIB)
        self.assertEqual(active.runs, ())
        self.assertEqual(active.cap, idle.cap)
        config = gate.read_memory_config()
        policy = config.policy
        self.assertEqual(queue_decision(20 * GIB, 64 * GIB, 11 * GIB, [reading], policy=policy).state, "hold")
        self.assertEqual(queue_decision(20 * GIB, 64 * GIB, 10 * GIB, [reading, reading], policy=policy).state, "admit")

    def test_ci_ram_changes_free_memory_without_entering_use_or_keep_asides(self) -> None:
        self.start_ci_job()
        for resident, available in ((2, 22), (6, 18)):
            with self.subTest(resident=resident):
                _ = (self.ci / "memory.current").write_text(str(resident * GIB))
                reading = self.reading()
                room = self.queue_with_ci(reading, available * GIB).explanation.room
                assert isinstance(room, gate.MemoryRoom)
                self.assertEqual(reading.bytes, resident * GIB)
                self.assertEqual(room.available, available * GIB)
                self.assertEqual(room.used, 0)
                self.assertEqual(room.kept, 7 * GIB)
                self.assertEqual(room.runs, ())
                self.assertEqual(room.cap, (resident + available - 3) * GIB)
                self.assertEqual(room.room, (available - 10) * GIB)

    def test_idle_ci_charges_resident_memory_and_records_swap_separately(self) -> None:
        resident = int(0.09 * GIB)
        swapped = int(1.98 * GIB)
        _ = (self.ci / "memory.current").write_text(str(resident))
        _ = (self.ci / "memory.swap.current").write_text(str(swapped))
        reading = self.reading()
        self.assertEqual(reading.bytes, resident)
        self.assertEqual(reading.swap_bytes, swapped)
        self.assertIsInstance(reading.job_state, IdleService)
        room = self.queue_with_ci(reading).explanation.room
        assert isinstance(room, gate.MemoryRoom)
        self.assertEqual(room.ci_keep_aside.bytes, 0)
        self.assertEqual((room.used, room.kept, room.room), (0, 0, 17 * GIB))

    def test_empty_ci_slice_allows_the_sole_oversized_step(self) -> None:
        _ = (self.ci / "cgroup.procs").write_text("")
        _ = (self.ci / "memory.current").write_text("0")
        reading = self.reading()
        decision = queue_decision(60 * GIB, 64 * GIB, 22 * GIB, [reading], policy=replace(admission_policy_fixture(), safety_bytes=0))
        self.assertEqual(decision.state, "admit")


    def test_idle_ci_excludes_inactive_file_cache_and_records_swap_separately(self) -> None:
        _ = (self.ci / "memory.current").write_text(str(10 * GIB))
        _ = (self.ci / "memory.stat").write_text(f"anon 1024\nfile {9 * GIB}\ninactive_file {8 * GIB}\n")
        _ = (self.ci / "memory.swap.current").write_text(str(GIB))
        reading = self.reading()
        self.assertEqual(reading.bytes, 2 * GIB)
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
        self.assertEqual(reading.swap_bytes, GIB)
        (scope / "memory.stat").unlink()
        self.assertEqual(gate.unowned_slice_memory([], {}, self.proc), ())


    def test_unreadable_or_invalid_ci_stat_records_a_diagnostic(self) -> None:
        self.start_ci_job()
        stat = self.ci / "memory.stat"
        for content in (None, "anon 1024\n", "inactive_file invalid\n"):
            with self.subTest(content=content):
                stat.unlink(missing_ok=True)
                if content is not None:
                    _ = stat.write_text(content)
                reading = self.reading()
                self.assertIn("memory.stat", reading.diagnostic)
                self.assertEqual(reading.bytes, 0)
                captured = self.queue_with_ci(reading)
                self.assertEqual(captured.view.running[0].measured, reading)
                room = captured.explanation.room
                assert isinstance(room, gate.MemoryRoom)
                self.assertEqual(room.ci_keep_aside.bytes, 0)
                self.assertEqual((room.used, room.kept, room.room), (0, 0, 17 * GIB))


    def test_ci_memory_max_does_not_change_resident_evidence(self) -> None:
        for content in (None, "max", "1"):
            with self.subTest(content=content):
                limit = self.ci / "memory.max"
                limit.unlink(missing_ok=True)
                if content is not None:
                    _ = limit.write_text(content)
                self.assertEqual(self.reading().bytes, 3 * GIB)

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
