"""Merge gate suites contracts."""

from __future__ import annotations

from scripts.build_queue import admission_records

from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Callable
import unittest
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from dataclasses import replace
from typing import cast, final, override
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock
from scripts.production import merge_gate
from scripts.lib import build_folders
from scripts.production.gate_modules import GateRefused
from scripts.build_queue import queue_place as queue, admission_client as client, admission_protocol as wire, admission_service as service
from scripts.build_queue import progress_profile as progress, step_estimator
from scripts.build_queue.queue_fixture import fixture_reservation
from scripts.build_queue.admission_test_support import only_admitted, FakeClock, wait_reply, snapshot_with_head_cause
from scripts.lint import memory_admit
from scripts.lint.memory_test_support import admission_policy_fixture, fake_memory_readings, write_config_fixture
from scripts.production.merge_gate_test_support import gate_suites, wait_for_file
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture


class GateProgressHistoryTests(unittest.TestCase):
    def test_gate_tail_uses_first_pass_memory_when_history_contains_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = progress.ProfileHistory(root)
            for episode, rerun in (("first-pass", False), ("old-rerun", True)):
                for index in range(20):
                    history.observe(episode, "gate", progress.ProgressCounter("suites:1", index * 5, 100),
                        (20 - index) * admission_records.GIB // 10, 100)
                if rerun:
                    history.observe(episode, "gate", progress.ProgressCounter("suites:2", 0, 100),
                        9 * admission_records.GIB, 100)
                history.finish(episode, 101, 90)
            profile = progress.ProfileHistory(root).matching("gate", 90)
            row = fixture_reservation(123, 4 * admission_records.GIB, step="merge-gate")
            reading = admission_records.MeasuredMemory(admission_records.GIB // 20,
                anonymous=admission_records.AnonymousRunUse(admission_records.GIB // 20))
            for done, expected in ((86, 3 * admission_records.GIB // 10), (99, admission_records.GIB // 10)):
                with self.subTest(done=done):
                    adjusted = memory_admit.progress_memory(row, reading,
                        memory_admit.AttributedStepUse(reading.anonymous, True),
                        progress.ProgressCounter("suites:1", done, 100), profile, admission_policy_fixture())
                    self.assertEqual(adjusted.remaining_need, expected)
                    self.assertNotIn("full need", memory_admit.progress_text(adjusted))
            rerun = memory_admit.progress_memory(row, reading,
                memory_admit.AttributedStepUse(reading.anonymous, True),
                progress.ProgressCounter("suites:2", 0, 100), profile, admission_policy_fixture())
            self.assertEqual(rerun.remaining_need, row["need"])
            self.assertIn("full need", memory_admit.progress_text(rerun))


class ServiceGateFixture(GateSuitesFixture):
    def reservation(self) -> tuple[Path, admission_records.Reservation]:
        record, = self.admission.server.reservations.values()
        return Path(record.path), service.reservation_row(record)



@final
class SharedGateAdmissionTests(ServiceGateFixture):
    @gate_suites("lib", "hooks")
    def test_suite_progress_counts_type_jobs_and_clears_the_counter_for_reruns(self) -> None:
        self.write(self.state / "fail-first-lib", "fail")
        counters: list[progress.ProgressCounter] = []
        sidecars: list[Path] = []
        starts: set[Path] = set()
        reruns: list[Path] = []
        publish = progress.publish_counter
        start = merge_gate.start_gate_job

        def record(sidecar: Path, counter: progress.ProgressCounter) -> None:
            publish(sidecar, counter)
            self.assertEqual(progress.read_counter(sidecar), counter)
            counters.append(counter)
            sidecars.append(sidecar)

        def start_job(suite: str, path: Path, argv: list[str], repo: Path,
                      environment: dict[str, str], log: Path, *,
                      kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
            if path in starts:
                reruns.append(path)
                self.assertFalse(progress.progress_path(sidecars[-1], "json").exists())
            starts.add(path)
            return start(suite, path, argv, repo, environment, log, kind=kind)

        with mock.patch.object(progress, "publish_counter", side_effect=record), \
             mock.patch.object(merge_gate, "start_gate_job", side_effect=start_job):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        initial_total = self.module_count + 1
        self.assertEqual([counter for counter in counters if counter.phase == "suites:1"],
            [progress.ProgressCounter("suites:1", done, initial_total) for done in range(initial_total + 1)])
        self.assertEqual([counter for counter in counters if counter.phase != "suites:1"], [])
        self.assertEqual(len(reruns), 2)
        recorder = progress.ProfileRecorder()
        recorded_use = admission_records.GIB // 20
        for counter in counters:
            _ = recorder.observe(counter, recorded_use)
        near_end = progress.ProgressCounter("suites:1", 99, 100)
        window = progress.window_peak(recorder.profile(), near_end, 20)
        self.assertIsInstance(window, progress.WindowPeak)
        assert isinstance(window, progress.WindowPeak)
        self.assertEqual(window.bytes, recorded_use)
        row = fixture_reservation(123, 4 * admission_records.GIB, step="merge-gate")
        reading = admission_records.MeasuredMemory(recorded_use,
            anonymous=admission_records.AnonymousRunUse(recorded_use))
        adjusted = memory_admit.progress_memory(row, reading,
            memory_admit.AttributedStepUse(reading.anonymous, True), near_end, recorder.profile(),
            admission_policy_fixture())
        self.assertEqual(adjusted.remaining_need, recorded_use)
        self.assert_gate_released()

    def test_suites_refuse_unwritable_memory_parent_before_admission(self) -> None:
        access = os.access

        def writable(path: str | os.PathLike[str], mode: int) -> bool:
            return False if Path(path) == build_folders.tmpfs_temporary_parent() else access(path, mode)

        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(os, "access", side_effect=writable), \
             mock.patch.object(merge_gate, "gate_reservation") as admission:
            with self.assertRaisesRegex(GateRefused,
                                        f"requires writable tmpfs at {build_folders.tmpfs_temporary_parent()}"):
                _ = merge_gate.run_suites(self.checkout)
            admission.assert_not_called()
        self.assert_gate_released()

    def test_gate_reservation_refuses_unusable_config_before_default_sizing_admission(self) -> None:
        config = self.root / "invalid-config" / "build_admission.conf"
        _ = write_config_fixture(config, "safety_gib=invalid")
        diagnostics = io.StringIO()
        with mock.patch.dict(os.environ, {**self.environment, "CONF_DIR": str(config.parent)}), \
             mock.patch.object(step_estimator, "memory_config", wraps=step_estimator.memory_config) as read_config, \
             mock.patch.object(client, "admit", wraps=client.admit) as checked, \
             redirect_stderr(diagnostics):
            with self.assertRaises(GateRefused) as refused:
                with merge_gate.gate_reservation(self.checkout, self.state, merge_gate.BuildHoldWait()):
                    self.fail("gate admitted an unusable configuration")
            read_config.assert_called_once_with()
            checked.assert_not_called()
        self.assertIn(str(config), str(refused.exception))
        self.assertIn("safety_gib", str(refused.exception))
        self.assertIn("safety_gib='invalid'", str(refused.exception))
        self.assertIn("no usable committed value", str(refused.exception))
        self.assertEqual(diagnostics.getvalue(), "")
        self.assert_gate_released()

    def test_invalid_first_peak_refuses_before_admission(self) -> None:
        self.configure_gate("program_first_peak_gib.merge-gate=0")
        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(client, "admit", wraps=client.admit) as acquire:
            with self.assertRaisesRegex(GateRefused, "program_first_peak_gib.merge-gate"):
                with merge_gate.gate_reservation(self.checkout, self.state, merge_gate.BuildHoldWait()):
                    self.fail("gate admitted an invalid reservation")
            acquire.assert_not_called()
        self.assert_gate_released()

    def test_gate_reservation_uses_the_shared_place_with_first_peak_and_hold(self) -> None:
        self.configure_gate("program_first_peak_gib.merge-gate=0.5")
        hold = merge_gate.BuildHoldWait()
        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(merge_gate, "queue_place", wraps=queue.queue_place) as acquire, \
             mock.patch.object(hold, "wait_until_free", wraps=hold.wait_until_free) as before_check:
            with merge_gate.gate_reservation(self.checkout, self.state, hold) as place:
                path, row = self.reservation()
                self.assertEqual(place.path, path)
                self.assertEqual((row["step"], row["need"]), ("merge-gate", 512 * 1024 * 1024))
                place.observe(123 * 1024 * 1024, 123 * 1024 * 1024)
                place.status = 0
            acquire.assert_called_once_with("merge-gate", self.checkout,
                before_check=client.AdmissionHoldCheck(hold.wait_until_free, hold.admits))
            before_check.assert_called_once_with()
        self.assert_gate_released()
        row, = self.gate_history()
        self.assertEqual(row["anon_peak_bytes"], 123 * 1024 * 1024)

    def test_missing_and_legacy_profiles_keep_the_full_four_gib_first_need(self) -> None:
        history = self.admission_directory / "anon_peaks.jsonl"
        for legacy_peak in (None, 128 * admission_records.GIB, admission_records.GIB // 10):
            with self.subTest(legacy_peak=legacy_peak):
                history.unlink(missing_ok=True)
                if legacy_peak is not None:
                    _ = history.write_text(json.dumps({
                        "host": "test-host", "repo": str(Path.home() / ".claude"),
                        "step": "merge-gate", "anon_peak_bytes": legacy_peak,
                        "ended_at": datetime.fromtimestamp(self.admission.clock.now() - 1, UTC).isoformat(),
                        "status": 0, "outcome": "completed",
                    }) + "\n")
                with mock.patch.dict(os.environ, self.environment):
                    with merge_gate.gate_reservation(self.checkout, self.state, merge_gate.BuildHoldWait()):
                        with self.admission.loop_turn():
                            _ = self.admission.server.run_pass()
                            _, row = self.reservation()
                            inputs = self.admission.server.last_inputs
                            assert isinstance(inputs, admission_records.QueueInputs)
                            room = memory_admit.inputs_room(inputs, admission_policy_fixture())
                            assert isinstance(room, admission_records.MemoryRoom)
                            hold, = room.runs
                            self.assertEqual(row["need"], 4 * admission_records.GIB)
                            self.assertEqual(hold.memory.need, 4 * admission_records.GIB)
                            self.assertEqual(hold.bytes, 4 * admission_records.GIB)
                self.assert_gate_released()
                with self.admission.loop_turn():
                    self.admission.allow_next_start()

    def test_gate_waits_for_the_protected_head_and_prints_its_need(self) -> None:
        self.configure_gate("program_first_peak_gib.merge-gate=0.5")
        fixture = self.admission
        with fixture.loop_turn():
            busy = only_admitted(fixture.server.handle_request(fixture.request(owner=fixture.proc.spawn(used=0))))
            fixture.allow_next_start()
        with fixture.loop_turn():
            older = fixture.request(need=400 * (1 << 20), since=fixture.clock.now() - 60)
            older = replace(older, identity=replace(older.identity, name="older-step"))
            fixture.free(0)
            reply, = fixture.server.handle_request(older)
        self.assertIsInstance(reply, wire.Wait)
        handle_request = fixture.server.handle_request
        first_gate_request = threading.Event()

        def fit_older(request: wire.Request,
                      connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            with fixture.loop_turn():
                if isinstance(request, wire.AdmitRequest) and request.owner != older.owner and not first_gate_request.is_set():
                    first_gate_request.set()
                    self.assertIn(older.episode, fixture.server.waiters)
                    fixture.free(23 * admission_records.GIB // 2)
                return handle_request(request, connection)

        admit = client.admit
        output = io.StringIO()

        def acquire(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None], refresh: Callable[[], wire.AdmitRequest], *,
                    _attempt: Callable[[], None]) -> wire.Admitted | wire.Refused | client.LeftQueue:
            def held(reply: wire.Wait) -> None:
                on_wait(reply)
                fixture.proc.kill(older.owner)
                fixture.free(60 * admission_records.GIB)
            return admit(request, held, refresh, _attempt=_attempt)

        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(fixture.server, "handle_request", side_effect=fit_older), \
             mock.patch.object(client, "admit", side_effect=acquire), redirect_stderr(output):
            with merge_gate.gate_reservation(self.checkout, self.state, merge_gate.BuildHoldWait()):
                pass
        self.assertIn("head older-step needs 0.4 GiB", output.getvalue())
        self.assertIsInstance(client.release(busy.reservation, 0, busy.cleanup_timing), wire.Ok)
        self.assert_gate_released()

    def test_memory_wait_reports_changes_and_only_one_reminder_per_minute(self) -> None:
        clock = FakeClock()
        formatter = client.WaitOutput()
        reported_at: list[float] = []
        output = io.StringIO()
        reply = wait_reply(admission_records.NeedsMemory(1, 0))
        with mock.patch.object(client, "CLOCK", clock), redirect_stderr(output):
            for at, need_mib, full in ((0,512,0), (1,512,0), (59,512,0), (60,512,0),
                                      (61,512,11), (62,512,12), (63,768,12)):
                clock.advance(at - clock.monotonic())
                snapshot = replace(reply.snapshot,
                    peak=admission_records.ExpectedPeak(need_mib * (1 << 20), "fallback", 0),
                    decision=replace(reply.snapshot.decision, pressure=(admission_records.MemoryPressure(full)
                        if full else admission_records.NO_PRESSURE_HOLD)))
                snapshot = snapshot_with_head_cause(snapshot, admission_records.MemoryPressure(full)
                    if full else admission_records.NeedsMemory(snapshot.peak.bytes, snapshot.available))
                before = len(output.getvalue().splitlines())
                formatter(replace(reply, snapshot=snapshot))
                reported_at.extend([float(at)] * (len(output.getvalue().splitlines()) - before))
        self.assertEqual(reported_at, [0.0, 60.0, 61.0, 63.0], output.getvalue())
        self.assertIn("memory pressure", output.getvalue())
        self.assertIn("0.750 GiB needed", output.getvalue())
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_gate_waiting_past_refresh_keeps_its_first_need_and_identity(self) -> None:
        self.configure_gate("program_first_peak_gib.merge-gate=0.5")
        fixture = self.admission
        with fixture.loop_turn():
            busy = only_admitted(fixture.server.handle_request(fixture.request()))
            fixture.allow_next_start()
        fixture.free(0)
        profiles: list[admission_records.ProgramProfile] = []
        identities: list[tuple[str, float, wire.Identity]] = []
        admit = client.admit
        def acquire(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None], refresh: Callable[[], wire.AdmitRequest], *,
                    _attempt: Callable[[], None]) -> wire.Admitted | wire.Refused | client.LeftQueue:
            def held(reply: wire.Wait) -> None:
                on_wait(reply)
                self.assertIsInstance(request.profile, admission_records.ProgramProfile)
                self.assertEqual(reply.snapshot.decision.state, "hold")
                profiles.append(cast(admission_records.ProgramProfile, request.profile))
                identities.append((request.episode, request.since, request.identity))
                if len(profiles) == 1:
                    fixture.clock.advance(admission_policy_fixture().timing.estimate_refresh_seconds + 1)
                else:
                    fixture.free(60 * admission_records.GIB)
            result = admit(request, held, refresh, _attempt=_attempt)
            profiles.append(cast(admission_records.ProgramProfile, request.profile))
            return result
        with mock.patch.object(client, "admit", side_effect=acquire), \
             mock.patch.object(client, "CLOCK", fixture.clock), \
             mock.patch.object(step_estimator, "git_identity", side_effect=AssertionError("gate inferred cargo")):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(profiles), 3)
        self.assertEqual(len(set(identities)), 1)
        for profile in profiles:
            self.assertIsInstance(profile, admission_records.ProgramProfile)
            self.assertEqual((profile.step, profile.worktree), ("merge-gate", str(self.checkout)))
            self.assertIsInstance(profile.sizing, admission_records.HistoryPeakSizing)
            assert isinstance(profile.sizing, admission_records.HistoryPeakSizing)
            self.assertEqual(profile.sizing.first_peak_bytes, 512 * 1024 * 1024)
        self.assertIsInstance(client.release(busy.reservation, 0, busy.cleanup_timing), wire.Ok)
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_stop_during_memory_wait_removes_the_live_parent_waiter(self) -> None:
        with self.admission.loop_turn():
            busy = only_admitted(self.admission.server.handle_request(self.admission.request()))
            self.admission.allow_next_start()
        self.admission.free(0)
        held: list[str] = []
        disconnected = threading.Event()
        disconnect = cast(Callable[[service.Connection], None], getattr(self.admission.server, "_disconnect"))
        def finish_disconnect(connection: service.Connection) -> None:
            disconnect(connection)
            if not self.admission.server.waiters:
                disconnected.set()
        admit = client.admit
        def acquire(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None], refresh: Callable[[], wire.AdmitRequest], *,
                    _attempt: Callable[[], None]) -> wire.Admitted | wire.Refused | client.LeftQueue:
            def stop(reply: wire.Wait) -> None:
                on_wait(reply)
                episode, = self.admission.server.waiters
                held.append(episode)
                raise KeyboardInterrupt("fixture stop during admission")
            return admit(request, stop, refresh, _attempt=_attempt)
        with mock.patch.object(client, "admit", side_effect=acquire), \
             mock.patch.object(self.admission.server, "_disconnect", side_effect=finish_disconnect):
            result = self.run_gate("--tests-only")
            self.assertTrue(disconnected.wait(timeout=5), "the private service did not remove the closed waiter within 5 seconds")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(held), 1)
        self.assertEqual(self.module_events(), [])
        self.assertIsInstance(client.release(busy.reservation, 0, busy.cleanup_timing), wire.Ok)
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_full_queue_holds_gate_until_the_running_charge_is_released(self) -> None:
        with self.admission.loop_turn():
            busy = only_admitted(self.admission.server.handle_request(self.admission.request(need=21 * admission_records.GIB)))
            self.admission.allow_next_start()
            self.admission.free(24 * admission_records.GIB)
        decisions: list[str] = []
        admit = client.admit
        def acquire(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None], refresh: Callable[[], wire.AdmitRequest], *,
                    _attempt: Callable[[], None]) -> wire.Admitted | wire.Refused | client.LeftQueue:
            def held(reply: wire.Wait) -> None:
                decisions.append("hold")
                self.assertEqual(self.module_events(), [])
                waiting, = self.admission.server.waiters.values()
                self.assertEqual(waiting.row.get("step"), "merge-gate")
                on_wait(reply)
                self.assertIsInstance(client.release(busy.reservation, 0, busy.cleanup_timing), wire.Ok)
            result = admit(request, held, refresh, _attempt=_attempt)
            if isinstance(result, wire.Admitted):
                decisions.append("admit")
            return result
        with mock.patch.object(client, "admit", side_effect=acquire):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(decisions, ["hold", "admit"], result.stderr)
        self.assert_all_modules_started()
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_measured_gate_peak_sets_the_next_claim(self) -> None:
        self.configure_gate("program_first_peak_gib.merge-gate=0.5")
        profiles: list[admission_records.ProgramProfile] = []
        admit = client.admit
        def acquire(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None], refresh: Callable[[], wire.AdmitRequest], *,
                    _attempt: Callable[[], None]) -> wire.Admitted | wire.Refused | client.LeftQueue:
            self.assertIsInstance(request.profile, admission_records.ProgramProfile)
            profiles.append(cast(admission_records.ProgramProfile, request.profile))
            self.assertEqual((request.profile.repo, request.profile.step, request.profile.worktree),
                             (str(Path.home() / ".claude"), "merge-gate", str(self.checkout)))
            sizing = cast(admission_records.ProgramProfile, request.profile).sizing
            self.assertIsInstance(sizing, admission_records.HistoryPeakSizing)
            assert isinstance(sizing, admission_records.HistoryPeakSizing)
            self.assertEqual(sizing.first_peak_bytes, 512 * 1024 * 1024)
            result = admit(request, on_wait, refresh, _attempt=_attempt)
            self.assertEqual(len(self.admission.server.reservations), 1)
            return result
        def sample(roots: set[int]) -> dict[int, float]:
            return {pid: 123.0 for pid in roots}
        with mock.patch.object(client, "admit", side_effect=acquire), \
             mock.patch.object(merge_gate, "process_memory_mib", side_effect=sample):
            first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual((profiles[0].peak.bytes, profiles[0].peak.source), (512 * 1024 * 1024, "fallback"))
        self.assert_gate_released()
        row, = self.gate_history()
        self.assertEqual((row["step"], row["anon_peak_bytes"]), ("merge-gate", 123 * 1024 * 1024))
        self.clear_gate_events()
        with mock.patch.object(client, "admit", side_effect=acquire):
            second = self.run_gate("--tests-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual((profiles[-1].peak.bytes, profiles[-1].peak.source), (123 * 1024 * 1024, "measured"))
        self.assert_gate_released()

    @gate_suites("lib", "hooks")
    def test_gate_keeps_one_first_need_from_first_to_last_module(self) -> None:
        reservation_mib = 768
        self.configure_gate(f"program_first_peak_gib.merge-gate={reservation_mib / 1024}")
        paths: list[Path] = []
        kinds: list[merge_gate.GateJobKind] = []
        started = merge_gate.start_gate_job
        release = client.release
        def assert_cap() -> None:
            path, row = self.reservation()
            self.assertEqual(row["need"], reservation_mib * 1024 * 1024)
            self.assertEqual(row.get("queue_waiter", {}).get("need_source"), "fallback")
            paths.append(path)
        def start(suite: str, path: Path, argv: list[str], repo: Path,
                  environment: dict[str, str], log: Path, *,
                  kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
            assert_cap()
            kinds.append(kind)
            return started(suite, path, argv, repo, environment, log, kind=kind)
        def finish(reservation: str, status: int, configuration: admission_records.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
            assert_cap()
            return release(reservation, status, configuration)
        with mock.patch.object(merge_gate, "start_gate_job", side_effect=start), \
             mock.patch.object(client, "release", side_effect=finish):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        self.assertEqual(kinds.count(merge_gate.GateJobKind.TEST_MODULE), len(self.module_events()) // 2)
        self.assertEqual(kinds.count(merge_gate.GateJobKind.TYPE_CHECK), 1)
        self.assertEqual(len(paths), len(kinds) + 1)
        self.assertEqual(len(set(paths)), 1)
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_failure_and_module_kill_release_the_gate_reservation(self) -> None:
        for marker in ("red-hooks", "signal-hooks"):
            with self.subTest(marker=marker):
                self.clear_gate_events()
                self.write(self.state / marker, "fail")
                with mock.patch.object(client, "admit", wraps=client.admit) as checked:
                    result = self.run_gate("--tests-only")
                self.assertTrue(checked.called)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_gate_released()
                (self.state / marker).unlink(missing_ok=True)

    @gate_suites("hooks")
    def test_admission_error_refuses_before_any_module_starts(self) -> None:
        with mock.patch.object(client, "admit", return_value=wire.Refused("fixture admission error")):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture admission error", result.stderr.splitlines()[-1])
        self.assertEqual(self.module_events(), [])
        self.assert_gate_released()


@final
class MergeGateAdmissionTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, fake_memory_readings(root)))

    def test_swap_wait_reason_uses_the_policy_saved_in_the_check_result(self) -> None:
        peak = admission_records.ExpectedPeak(admission_records.GIB, "fallback", 0)
        default = admission_policy_fixture()
        root = Path(os.environ["CONF_DIR"])
        _ = fake_memory_readings(root, full_avg10=8, some_avg10=40,
                                 swap_total_kib=100, swap_free_kib=20)
        lower = replace(default, pressure=replace(default.pressure, swap_used_limit=70))
        higher = replace(default, pressure=replace(default.pressure, swap_used_limit=90))
        host = memory_admit.read_meminfo(Path(os.environ["BUILDLOG_MEMINFO"]))
        pressure = memory_admit.read_pressure(host, policy=lower)
        assert isinstance(pressure, admission_records.MemoryPressure)
        higher_pressure = memory_admit.read_pressure(host, policy=higher)
        assert isinstance(higher_pressure, admission_records.MemoryPressure)
        result = admission_records.AdmissionWaitSnapshot(
            admission_records.AdmissionDecision("hold", 0, 0, 0, 0, pressure=pressure), peak, (),
            0, "test", "merge-gate", default, ())
        with mock.patch.object(admission_records, "read_memory_config", side_effect=AssertionError("frozen check")), \
             mock.patch.object(step_estimator, "memory_config", side_effect=AssertionError("frozen check")):
            self.assertEqual(queue.wait_reasons(snapshot_with_head_cause(replace(result, policy=lower), pressure)),
                             ("swap memory pressure",))
            self.assertEqual(queue.wait_reasons(snapshot_with_head_cause(replace(result, policy=higher),
                                 higher_pressure)),
                             ("memory pressure",))

    def test_scope_probe_timeout_or_missing_command_runs_without_prefix(self) -> None:
        for error in (subprocess.TimeoutExpired("systemd-run", 10), FileNotFoundError("systemd-run")):
            with self.subTest(error=type(error).__name__), mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                    mock.patch.object(queue, "in_builds_slice", return_value=False), \
                    mock.patch("scripts.production.merge_gate.subprocess.run", side_effect=error) as probe, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(queue.suite_scope_prefix(), [])
                self.assertEqual(probe.call_args.kwargs["timeout"], 10)
                self.assertIn("Module scope skipped: probe failed", output.getvalue())

    def test_builds_slice_detection_reads_the_gates_cgroup(self) -> None:
        for cgroup, expected in (("0::/user.slice/builds.slice/run-123.scope\n", True),
                                 ("0::/user.slice/app.slice/session.scope\n", False)):
            with self.subTest(cgroup=cgroup), mock.patch.object(Path, "read_text", autospec=True, return_value=cgroup) as read:
                self.assertEqual(queue.in_builds_slice(), expected)
                read.assert_called_once_with(Path("/proc/self/cgroup"))

    def test_a_sample_counts_swapped_memory(self) -> None:
        with mock.patch.object(merge_gate, "swapped_kib", return_value=1024 * 1024):
            sample = merge_gate.process_memory_mib({os.getpid()})
        self.assertGreater(sample[os.getpid()], 1024.0)

    def test_sampled_admission_counts_each_reservation_between_scans(self) -> None:
        admission = merge_gate.SampledRSSAdmission(512)
        admission.reserve(1, 100.0)
        admission.reserve(2, 100.0)
        admission.observe(100.0, 300.0, {1: 100.0, 2: 30.0})
        self.assertTrue(admission.admits(100.0))
        admission.reserve(3, 100.0)
        self.assertFalse(admission.admits(100.0))
        admission.release(2)
        admission.release(3)
        admission.observe(101.0, 450.0, {1: 80.0})
        self.assertFalse(admission.admits(100.0))
        self.assertEqual(admission.reserved_mib, 20.0)
        self.assertEqual(admission.peak_mib, 450.0)
        admission.release(1)
        self.assertTrue(admission.admits(1000.0))

    def test_starting_job_keeps_its_reservation_after_a_small_rss_sample(self) -> None:
        admission = merge_gate.SampledRSSAdmission(256)
        admission.observe(100.0, 24.0, {})
        admission.reserve(1, 96.0)
        admission.reserve(2, 96.0)
        self.assertFalse(admission.admits(96.0))
        admission.observe(100.25, 40.0, {1: 8.0, 2: 8.0})
        self.assertEqual(admission.reserved_mib, 176.0)
        self.assertFalse(admission.admits(96.0))
        # At the reservation size, real RSS replaces padding without lowering
        # the charged total; a completed job releases its padding.
        admission.observe(100.5, 128.0, {1: 96.0, 2: 8.0})
        self.assertEqual(admission.reserved_mib, 88.0)
        self.assertFalse(admission.admits(96.0))
        admission.release(2)
        self.assertEqual(admission.reserved_mib, 0.0)
        self.assertTrue(admission.admits(96.0))

    def test_a_larger_sample_does_not_change_other_jobs_reservations(self) -> None:
        admission = merge_gate.SampledRSSAdmission(512)
        admission.reserve(1, 96.0)
        admission.reserve(2, 96.0)
        admission.observe(100.0, 280.0, {1: 240.0, 2: 16.0})
        self.assertEqual(admission.reserved_mib, 80.0)
        self.assertTrue(admission.admits(96.0))

    def test_a_large_job_keeps_its_own_reservation_between_samples(self) -> None:
        admission = merge_gate.SampledRSSAdmission(4096)
        admission.observe(100.0, 0.0, {})
        admission.reserve(1, 3072.0)
        self.assertEqual(admission.reserved_mib, 3072.0)
        self.assertTrue(admission.admits(640.0))
        admission.observe(100.25, 2700.0, {1: 2700.0})
        self.assertEqual(admission.reserved_mib, 372.0)
        self.assertTrue(admission.admits(640.0))
        admission.release(1)
        self.assertEqual(admission.reservations_mib, {})

    def test_a_module_three_times_slower_than_its_record_and_a_minute_over_is_slow(self) -> None:
        self.assertTrue(merge_gate.slower_than_recorded(1264.0, 49.0))
        self.assertTrue(merge_gate.slower_than_recorded(90.0, 30.0))
        self.assertFalse(merge_gate.slower_than_recorded(89.0, 30.0))
        self.assertFalse(merge_gate.slower_than_recorded(12.0, 1.0))

    def test_memory_scan_schedule_uses_controlled_quarter_second_clock(self) -> None:
        admission = merge_gate.SampledRSSAdmission(3072)
        self.assertTrue(admission.sample_due(10.0))
        admission.observe(10.0, 30.0, {})
        for tick in (10.05, 10.10, 10.15, 10.20):
            self.assertFalse(admission.sample_due(tick))
        self.assertTrue(admission.sample_due(10.25))


@dataclass(frozen=True)
class AdmissionRun:
    gate: merge_gate.GateRun
    report: str
    reservations_mib: dict[str, float]
    sampled_before_reservation: set[str]
    concurrent_starts: list[set[str]]


@final
class MergeGateRecordedPeakTests(ServiceGateFixture):
    def observe_gate(self, measured_peaks: dict[str, float], *,
                     governor_mib: int = 4096,
                     type_dirs: tuple[str, ...] = ()) -> AdmissionRun:
        """Hold real jobs until a controlled sample, then release them through fixture events."""
        self.configure_gate(f"merge_gate_governor_gib={governor_mib / 1024}")
        self.write(self.state / "concurrency-barrier", "held")
        if type_dirs:
            self.write(self.state / "types-hold", "held")
        started: dict[int, merge_gate.RunningGateJob] = {}
        sampled: set[int] = set()
        reservations: dict[str, float] = {}
        sampled_before_reservation: set[str] = set()
        concurrent_starts: list[set[str]] = []
        start = merge_gate.start_gate_job
        reserve = merge_gate.SampledRSSAdmission.reserve

        def key(job: merge_gate.RunningGateJob) -> str:
            return f"{job.suite}/{job.module}"

        def start_held(suite: str, path: Path, argv: list[str], repo: Path,
                       environment: dict[str, str], log: Path, *,
                       kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            started[job.process.pid] = job
            wait_for_file(self.state / ("types-ran" if job.kind is merge_gate.GateJobKind.TYPE_CHECK
                                        else f"{job.process.pid}.started"))
            concurrent_starts.append({key(active) for active in started.values() if active.process.poll() is None})
            return job

        def record_reservation(admission: merge_gate.SampledRSSAdmission, pid: int, expected_mib: float) -> None:
            name = key(started[pid])
            reservations[name] = expected_mib
            if pid in sampled:
                sampled_before_reservation.add(name)
            reserve(admission, pid, expected_mib)

        def sample_and_release(roots: set[int]) -> dict[int, float]:
            measured: dict[int, float] = {}
            for pid in roots & started.keys():
                job = started[pid]
                sampled.add(pid)
                measured[pid] = measured_peaks.get(key(job), 12.0)
                if job.kind is merge_gate.GateJobKind.TYPE_CHECK:
                    (self.state / "types-sampled").touch()
                else:
                    (self.state / f"{pid}.released").touch()
            return {os.getpid(): 24.0 + sum(measured.values()), **measured}

        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "start_gate_job", side_effect=start_held), \
                mock.patch.object(merge_gate.SampledRSSAdmission, "reserve", autospec=True, side_effect=record_reservation), \
                mock.patch.object(merge_gate, "process_memory_mib", side_effect=sample_and_release), \
                redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout, type_dirs=type_dirs,
                                        max_running_jobs=2, full=True)
        return AdmissionRun(run, output.getvalue(), reservations, sampled_before_reservation, concurrent_starts)

    def test_module_above_six_gib_passes_without_a_memory_kill(self) -> None:
        observed = self.observe_gate({"hooks/test_first.py": 8192.0})
        self.assertTrue(observed.gate.passed, observed.report)
        job, = observed.gate.jobs
        self.assertEqual((job.returncode, job.killed_for, job.peak_mib), (0, "", 8192.0))
        self.assertEqual(observed.gate.peak_memory_mib, 8216.0)
        self.assertEqual(merge_gate.read_measures(self.state / "runner/module_peak_mib.json"),
                         {"hooks/test_first.py": 8192.0})
        self.assertRegex(observed.report, r"(?m)^Gate tests: .*peak memory: 8\.023 GiB \(sampled memory\); full suite: --full$")
        self.assert_gate_released()
        row, = self.gate_history()
        self.assertEqual(row["anon_peak_bytes"], 8216 * 1024 * 1024)
        self.assertEqual((row["status"], row["outcome"]), (0, "completed"))

    @gate_suites("lib")
    def test_recorded_module_reservations_apply_before_sampling_and_prevent_overlap(self) -> None:
        self.record_module_peaks({"lib/test_first.py": 400.0, "lib/test_second.py": 100.0})
        observed = self.observe_gate({}, governor_mib=490)
        self.assertTrue(observed.gate.passed, observed.report)
        self.assertEqual(observed.reservations_mib, {"lib/test_first.py": 480.0, "lib/test_second.py": 120.0})
        self.assertEqual(observed.sampled_before_reservation, set())
        self.assertEqual(max(map(len, observed.concurrent_starts)), 1)
        self.assertEqual(len(observed.gate.jobs), self.module_count)
        self.assertEqual(merge_gate.read_measures(self.state / "runner" / "module_peak_mib.json"),
                         {"lib/test_first.py": 12.0, "lib/test_second.py": 12.0})

    @gate_suites("lib")
    def test_unrecorded_module_reserves_the_largest_recorded_module_peak(self) -> None:
        self.record_module_peaks({"lib/test_first.py": 100.0, "other/tests/test_large.py": 500.0})
        observed = self.observe_gate({}, governor_mib=730)
        self.assertTrue(observed.gate.passed, observed.report)
        self.assertEqual(observed.reservations_mib, {"lib/test_first.py": 120.0, "lib/test_second.py": 600.0})
        self.assertEqual(observed.sampled_before_reservation, set())
        self.assertEqual(max(map(len, observed.concurrent_starts)), 1)
        self.assertEqual(len(observed.gate.jobs), self.module_count)

    @gate_suites("lib")
    def test_modules_without_any_record_reserve_640_mib(self) -> None:
        observed = self.observe_gate({}, governor_mib=1200)
        self.assertTrue(observed.gate.passed, observed.report)
        self.assertEqual(observed.reservations_mib, {"lib/test_first.py": 640.0, "lib/test_second.py": 640.0})
        self.assertEqual(observed.sampled_before_reservation, set())
        self.assertEqual(max(map(len, observed.concurrent_starts)), 1)
        self.assertEqual(len(observed.gate.jobs), self.module_count)

    @gate_suites("types")
    def test_a_types_suite_module_keeps_its_own_reservation_and_peak_record(self) -> None:
        self.record_module_peaks({"types/test_first.py": 100.0})
        merge_gate.save_measures(self.state / "runner/types_peak_mib.json", {"scripts": 4065.0})
        observed = self.observe_gate({"types/basedpyright": 4065.0}, type_dirs=("scripts",))
        self.assertTrue(observed.gate.passed, observed.report)
        self.assertEqual(observed.reservations_mib, {"types/test_first.py": 120.0, "types/basedpyright": 4878.0})
        self.assertEqual(merge_gate.read_measures(self.state / "runner/module_peak_mib.json"),
                         {"types/test_first.py": 12.0})
        self.assertEqual(merge_gate.read_measures(self.state / "runner/types_peak_mib.json"), {"scripts": 4065.0})
        self.assertIn("module: types/test_first.py", observed.report)


if __name__ == "__main__":
    _ = unittest.main()
