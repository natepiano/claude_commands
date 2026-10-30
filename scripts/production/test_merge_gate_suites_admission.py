"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import io
import os
from pathlib import Path
import subprocess
from typing import final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate


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
