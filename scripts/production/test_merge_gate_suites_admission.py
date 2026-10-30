"""Merge gate suites contracts."""

from __future__ import annotations

from dataclasses import dataclass
import unittest
import io
import os
from pathlib import Path
import subprocess
from typing import final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import gate_suites, wait_for_file
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture


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
class MergeGateRecordedPeakTests(GateSuitesFixture):
    def observe_gate(self, measured_peaks: dict[str, float], *,
                     memory_cap_mib: int = merge_gate.DEFAULT_MEMORY_CAP_MIB,
                     type_dirs: tuple[str, ...] = ()) -> AdmissionRun:
        """Hold real jobs until a controlled sample, then release them through fixture events."""
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
            run = merge_gate.run_suites(self.checkout, memory_cap_mib=memory_cap_mib, type_dirs=type_dirs,
                                        max_running_jobs=2, full=True)
        return AdmissionRun(run, output.getvalue(), reservations, sampled_before_reservation, concurrent_starts)

    @gate_suites("lib")
    def test_recorded_module_reservations_apply_before_sampling_and_prevent_overlap(self) -> None:
        self.record_module_peaks({"lib/test_first.py": 400.0, "lib/test_second.py": 100.0})
        observed = self.observe_gate({}, memory_cap_mib=490)
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
        observed = self.observe_gate({}, memory_cap_mib=730)
        self.assertTrue(observed.gate.passed, observed.report)
        self.assertEqual(observed.reservations_mib, {"lib/test_first.py": 120.0, "lib/test_second.py": 600.0})
        self.assertEqual(observed.sampled_before_reservation, set())
        self.assertEqual(max(map(len, observed.concurrent_starts)), 1)
        self.assertEqual(len(observed.gate.jobs), self.module_count)

    @gate_suites("lib")
    def test_modules_without_any_record_reserve_640_mib(self) -> None:
        observed = self.observe_gate({}, memory_cap_mib=1200)
        self.assertTrue(observed.gate.passed, observed.report)
        self.assertEqual(observed.reservations_mib, {"lib/test_first.py": 640.0, "lib/test_second.py": 640.0})
        self.assertEqual(observed.sampled_before_reservation, set())
        self.assertEqual(max(map(len, observed.concurrent_starts)), 1)
        self.assertEqual(len(observed.gate.jobs), self.module_count)

    def test_a_limit_kill_does_not_record_a_new_peak_or_replace_an_old_peak(self) -> None:
        previous_records: tuple[dict[str, float], ...] = ({}, {"hooks/test_first.py": 80.0})
        for previous in previous_records:
            with self.subTest(previous=previous):
                self.record_module_peaks(previous)
                observed = self.observe_gate({"hooks/test_first.py": 300.0}, memory_cap_mib=256)
                self.assertFalse(observed.gate.passed, observed.report)
                self.assertIn("killed: sampled memory", observed.gate.jobs[0].killed_for)
                self.assertEqual(merge_gate.read_measures(self.state / "runner" / "module_peak_mib.json"), previous)

    def test_default_cap_holds_the_full_tree_type_reservation_beside_a_module(self) -> None:
        merge_gate.save_measures(self.state / "runner" / "types_peak_mib.json", {"scripts": 4065.0})
        observed = self.observe_gate({"types/basedpyright": 4065.0}, type_dirs=("scripts",))
        self.assertTrue(observed.gate.passed, observed.report)
        self.assertEqual(observed.reservations_mib["types/basedpyright"], 4878.0)
        self.assertIn({"types/basedpyright", "hooks/test_first.py"}, observed.concurrent_starts)
        self.assertEqual(observed.sampled_before_reservation, set())
        self.assertEqual(len(observed.gate.jobs), self.module_count + 1)

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
