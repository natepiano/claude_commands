"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import io
import os
import json
from pathlib import Path
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import cast, final, override
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate
from scripts.lint import memory_admit
from scripts.production.merge_gate_test_support import gate_suites
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture


@final
class SharedGateAdmissionTests(GateSuitesFixture):
    def test_memory_wait_reports_changes_and_only_one_reminder_per_minute(self) -> None:
        busy = self.occupy_memory_queue()
        checks = memory_admit.check
        # Changes in pressure readings alone must not produce a line per sample.
        polls = ((0.0, 512, 0.0), (1.0, 512, 0.0), (59.0, 512, 0.0),
                 (60.0, 512, 0.0), (61.0, 512, 11.0), (62.0, 512, 12.0),
                 (63.0, 768, 12.0), (64.0, 768, 12.0))
        index = 0
        reported_at: list[float] = []
        output = io.StringIO()
        lines_seen = 0

        def check(meminfo: Path, pid: int, sidecar: str, argv: list[str], *, root: Path,
                  profile: memory_admit.StepProfile | memory_admit.GateProfile) -> memory_admit.CheckResult:
            result = checks(meminfo, pid, sidecar, argv, root=root, profile=profile)
            if result.decision.state == "admit":
                return result
            _, need_mib, full = polls[index]
            return replace(result, peak=memory_admit.ExpectedPeak(need_mib * (1 << 20), "fallback", 0),
                           decision=replace(result.decision, pressure=(memory_admit.MemoryPressure(full)
                                            if full else memory_admit.NO_PRESSURE_HOLD)))

        def next_poll(_seconds: float) -> None:
            nonlocal index, lines_seen
            lines = output.getvalue().splitlines()
            reported_at.extend([polls[index][0]] * (len(lines) - lines_seen))
            lines_seen = len(lines)
            index += 1
            if index == len(polls):
                busy.unlink()

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(memory_admit, "check", side_effect=check), \
                mock.patch.object(time, "sleep", side_effect=next_poll), \
                mock.patch.object(time, "monotonic", side_effect=lambda: polls[min(index, len(polls) - 1)][0]), \
                redirect_stdout(output):
            with merge_gate.gate_reservation(self.checkout, 512, self.state, merge_gate.BuildHoldWait()):
                pass
        self.assertEqual(reported_at, [0.0, 60.0, 61.0, 63.0], output.getvalue())
        self.assertIn("memory pressure", output.getvalue())
        self.assertIn("0.750 GiB needed", output.getvalue())
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_gate_waiting_past_profile_expiry_refreshes_without_losing_identity(self) -> None:
        busy = self.occupy_memory_queue()
        clock = datetime(2026, 10, 30, tzinfo=UTC)
        profiles: list[memory_admit.GateProfile] = []
        sleep = time.sleep

        class AdmissionClock(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return clock.astimezone(tz)

        def short_sleep(seconds: float) -> None:
            sleep(min(seconds, 0.01))

        def observe(result: memory_admit.CheckResult) -> None:
            nonlocal clock
            self.assertIsInstance(result.profile, memory_admit.GateProfile)
            profiles.append(cast(memory_admit.GateProfile, result.profile))
            if len(profiles) == 1:
                self.assertEqual(result.decision.state, "hold")
                clock += timedelta(seconds=601)
            elif len(profiles) == 2:
                self.assertEqual(result.decision.state, "hold")
                busy.unlink()

        with self.observing_admission(observe), \
                mock.patch.object(memory_admit, "datetime", AdmissionClock), \
                mock.patch.object(memory_admit, "git_identity", side_effect=AssertionError("gate inferred cargo")), \
                mock.patch.object(time, "sleep", side_effect=short_sleep):
            result = self.run_gate("--tests-only", "--memory-cap-mib", "512")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(profiles), 3)
        for profile in profiles:
            self.assertIsInstance(profile, memory_admit.GateProfile)
            self.assertEqual((profile.step, profile.worktree, profile.cap_bytes),
                             ("merge-gate", str(self.checkout), 512 * 1024 * 1024))
        self.assertEqual(profiles[-1].estimated_at, clock.timestamp())
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_stop_during_memory_wait_removes_the_live_parent_waiter(self) -> None:
        busy = self.occupy_memory_queue()
        held: list[Path] = []

        def observe(result: memory_admit.CheckResult) -> None:
            if result.decision.state == "hold":
                waiter, = self.admission_directory.glob("*.waiting")
                held.append(waiter)
                raise KeyboardInterrupt("fixture stop during admission")

        with self.observing_admission(observe):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(held), 1)
        self.assertEqual(self.module_events(), [])
        busy.unlink()
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_full_queue_holds_gate_until_the_running_charge_is_released(self) -> None:
        busy = self.occupy_memory_queue()
        decisions: list[str] = []

        def observe(result: memory_admit.CheckResult) -> None:
            decisions.append(result.decision.state)
            if result.decision.state == "hold":
                self.assertEqual(self.module_events(), [])
                row, = self.admission_directory.glob("*.waiting")
                waiter = cast(dict[str, object], json.loads(row.read_text()))
                self.assertEqual(waiter["step"], "merge-gate")
                busy.unlink()

        with self.observing_admission(observe):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(decisions, ["hold", "admit"])
        self.assert_all_modules_started()
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_first_gate_asks_for_its_cap_and_release_supplies_the_next_estimate(self) -> None:
        estimates: list[memory_admit.GateEstimate] = []
        reservations: list[Path] = []

        def sample(roots: set[int]) -> dict[int, float]:
            return {pid: 123.0 for pid in roots}

        def observe(result: memory_admit.CheckResult) -> None:
            self.assertEqual((result.repo, result.step), (str(Path.home() / ".claude"), "merge-gate"))
            self.assertEqual(result.profile.worktree, str(self.checkout))
            self.assertIsInstance(result.profile, memory_admit.GateProfile)
            estimates.append(cast(memory_admit.GateProfile, result.profile).estimate)
            if isinstance(result.reservation, memory_admit.Reserved):
                reservations.append(result.reservation.path)

        with self.observing_admission(observe), \
                mock.patch.object(merge_gate, "process_memory_mib", side_effect=sample):
            first = self.run_gate("--tests-only", "--memory-cap-mib", "512")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(len(reservations), 1)
        self.assertIsInstance(estimates[0], memory_admit.NoGateHistory)
        self.assertEqual(cast(memory_admit.NoGateHistory, estimates[0]).cap_bytes, 512 * 1024 * 1024)
        self.assert_gate_released()
        row, = self.gate_history()
        self.assertEqual((row["step"], row["anon_peak_bytes"]), ("merge-gate", 123 * 1024 * 1024))
        self.clear_gate_events()
        with self.observing_admission(observe):
            second = self.run_gate("--tests-only", "--memory-cap-mib", "512")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIsInstance(estimates[-1], memory_admit.MeasuredGatePeak)
        self.assertEqual(cast(memory_admit.MeasuredGatePeak, estimates[-1]).bytes, 123 * 1024 * 1024)
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_failure_and_module_kill_release_the_gate_reservation(self) -> None:
        check = memory_admit.check
        for marker in ("red-hooks", "signal-hooks"):
            with self.subTest(marker=marker):
                self.clear_gate_events()
                self.write(self.state / marker, "fail")
                with mock.patch.object(memory_admit, "check", wraps=check) as checked:
                    result = self.run_gate("--tests-only")
                self.assertTrue(checked.called)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_gate_released()
                (self.state / marker).unlink(missing_ok=True)

    @gate_suites("hooks")
    def test_admission_error_refuses_before_any_module_starts(self) -> None:
        with mock.patch.object(memory_admit, "check", side_effect=OSError("fixture admission error")):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture admission error", result.stderr.splitlines()[-1])
        self.assertEqual(self.module_events(), [])
        self.assert_gate_released()


@final
class MergeGateAdmissionTests(unittest.TestCase):
    def test_scope_probe_timeout_or_missing_command_runs_without_prefix(self) -> None:
        for error in (subprocess.TimeoutExpired("systemd-run", 10), FileNotFoundError("systemd-run")):
            with self.subTest(error=type(error).__name__), mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                    mock.patch.object(merge_gate, "in_builds_slice", return_value=False), \
                    mock.patch("scripts.production.merge_gate.subprocess.run", side_effect=error) as probe, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(merge_gate.suite_scope_prefix(), [])
                self.assertEqual(probe.call_args.kwargs["timeout"], 10)
                self.assertIn("Module scope skipped: probe failed", output.getvalue())

    def test_builds_slice_detection_reads_the_gates_cgroup(self) -> None:
        for cgroup, expected in (("0::/user.slice/builds.slice/run-123.scope\n", True),
                                 ("0::/user.slice/app.slice/session.scope\n", False)):
            with self.subTest(cgroup=cgroup), mock.patch.object(Path, "read_text", autospec=True, return_value=cgroup) as read:
                self.assertEqual(merge_gate.in_builds_slice(), expected)
                read.assert_called_once_with(Path("/proc/self/cgroup"))

    def test_a_sample_counts_swapped_memory(self) -> None:
        with mock.patch.object(merge_gate, "swapped_kib", return_value=1024 * 1024):
            sample = merge_gate.process_memory_mib({os.getpid()})
        self.assertGreater(sample[os.getpid()], 1024.0)

    def test_sampled_admission_learns_headroom_and_reserves_between_scans(self) -> None:
        admission = merge_gate.SampledRSSAdmission(512)
        admission.observe(100.0, 300.0, {1: 100.0, 2: 30.0})
        self.assertTrue(admission.admits(True))
        admission.reserve(3)
        self.assertFalse(admission.admits(True))
        self.assertTrue(admission.admits(False))
        admission.observe(101.0, 450.0, {1: 80.0})
        self.assertFalse(admission.admits(True))
        self.assertEqual(admission.headroom_mib, 100.0)
        self.assertEqual(admission.peak_mib, 450.0)

    def test_starting_job_keeps_its_reservation_after_a_small_rss_sample(self) -> None:
        admission = merge_gate.SampledRSSAdmission(256)
        admission.observe(100.0, 24.0, {})
        admission.reserve(1)
        admission.reserve(2)
        self.assertFalse(admission.admits(True))
        admission.observe(100.25, 40.0, {1: 8.0, 2: 8.0})
        self.assertEqual(admission.reserved_mib, 176.0)
        self.assertFalse(admission.admits(True))
        # At the reservation size, real RSS replaces padding without lowering
        # the charged total; a completed job releases its padding.
        admission.observe(100.5, 128.0, {1: 96.0, 2: 8.0})
        self.assertEqual(admission.reserved_mib, 88.0)
        self.assertFalse(admission.admits(True))
        admission.release(2)
        self.assertEqual(admission.reserved_mib, 0.0)
        self.assertTrue(admission.admits(True))

    def test_larger_job_peak_increases_every_running_jobs_reservation(self) -> None:
        admission = merge_gate.SampledRSSAdmission(512)
        admission.observe(100.0, 280.0, {1: 240.0, 2: 16.0})
        self.assertEqual(admission.headroom_mib, 240.0)
        self.assertEqual(admission.reserved_mib, 224.0)
        self.assertFalse(admission.admits(True))

    def test_a_job_reserved_at_a_known_size_holds_it_and_leaves_module_headroom_alone(self) -> None:
        admission = merge_gate.SampledRSSAdmission(4096)
        admission.observe(100.0, 0.0, {})
        admission.reserve(1, 3072.0)
        self.assertEqual(admission.reserved_mib, 3072.0)
        self.assertTrue(admission.admits(True))
        admission.observe(100.25, 2700.0, {1: 2700.0})
        self.assertEqual(admission.headroom_mib, 96.0)
        self.assertEqual(admission.reserved_mib, 372.0)
        self.assertTrue(admission.admits(True))
        admission.release(1)
        self.assertEqual(admission.fixed_mib, {})

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


if __name__ == "__main__":
    _ = unittest.main()
