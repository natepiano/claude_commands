"""Progress counters and the remaining anonymous memory need."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


from scripts.build_queue import progress_profile as progress

GIB = 1 << 30


class ProgressCounterTests(unittest.TestCase):
    def test_cargo_reads_the_last_carriage_return_frame_with_ansi_codes(self) -> None:
        counter = progress.parse_progress(
            "   Compiling example v1.0\n" +
            "\x1b[2K\r    Building [===>       ] 2/10: example\r" +
            "\x1b[2K\r    Building [======>    ] 6/10: example")
        self.assertIsInstance(counter, progress.ProgressCounter)
        assert isinstance(counter, progress.ProgressCounter)
        self.assertEqual((counter.phase, counter.done, counter.total), ("build:1", 6, 10))

    def test_output_without_a_counter_has_a_named_missing_state(self) -> None:
        self.assertIsInstance(progress.parse_progress("Compiling example v1.0\n"), progress.NoCounter)

    def test_a_second_check_has_a_new_phase_even_with_the_same_total(self) -> None:
        output = "\r    Building [==========] 10/10: first check"
        first = progress.parse_progress(output)
        second = progress.parse_progress(output + "\r    Building [=>        ] 1/10: second check")
        assert isinstance(first, progress.ProgressCounter)
        assert isinstance(second, progress.ProgressCounter)
        self.assertEqual((second.done, second.total), (1, 10))
        self.assertNotEqual(first.phase, second.phase)

    def test_nextest_moves_from_build_to_test_counters(self) -> None:
        output = "\r    Building [======>   ] 7/10: tests"
        build = progress.parse_progress(output)
        tests = progress.parse_progress(output +
            "\nStarting 12 tests across 3 binaries\nPASS [0.1s] (4/12) example::test")
        assert isinstance(build, progress.ProgressCounter)
        assert isinstance(tests, progress.ProgressCounter)
        self.assertEqual((tests.phase, tests.done, tests.total), ("test:1", 4, 12))
        self.assertNotEqual(build.phase, tests.phase)

    def test_finished_phase_does_not_keep_a_stale_counter(self) -> None:
        self.assertIsInstance(progress.parse_progress(
            "\r    Building [==========] 10/10: example\nFinished dev profile in 1.0s\n"),
            progress.NoCounter)


class ProgressCaptureTests(unittest.TestCase):
    def test_a_counter_split_between_log_reads_is_complete_after_the_next_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cargo.log"
            _ = path.write_text("\rBuilding [====>     ] 4/")
            reader = progress.CaptureReader(Path(temporary))
            self.assertIsInstance(reader.log_counter(path), progress.NoCounter)
            with path.open("a") as output:
                _ = output.write("10: example\r")
            self.assertEqual(reader.log_counter(path), progress.ProgressCounter("build:1", 4, 10))

    def test_a_long_log_keeps_the_full_need_until_capture_reaches_its_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cargo.log"
            _ = path.write_text("Building [=>        ] 1/10: old\n" + "compiler output\n" * 5000 +
                "\rBuilding [========> ] 8/10: current\r")
            reader = progress.CaptureReader(Path(temporary))
            self.assertIsInstance(reader.log_counter(path), progress.NoCounter)
            self.assertEqual(reader.log_counter(path), progress.ProgressCounter("build:1", 8, 10))

    def test_a_phase_reset_in_a_later_log_increment_has_a_new_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cargo.log"
            _ = path.write_text("Building [==========] 10/10: first check\r")
            reader = progress.CaptureReader(Path(temporary))
            first = reader.log_counter(path)
            with path.open("a") as output:
                _ = output.write("Building [=>        ] 1/10: second check\r")
            second = reader.log_counter(path)
            assert isinstance(first, progress.ProgressCounter)
            assert isinstance(second, progress.ProgressCounter)
            self.assertNotEqual(first.phase, second.phase)
            self.assertEqual((second.done, second.total), (1, 10))


class ProgressWindowTests(unittest.TestCase):
    @staticmethod
    def profile(phase: str, values: tuple[int, ...]) -> progress.PhaseProfile:
        return progress.PhaseProfile(phase, values)

    def test_the_lookahead_crosses_from_the_build_end_into_the_test_phase(self) -> None:
        profile = progress.ProgressProfile((
            self.profile("build:1", (GIB,) * 20),
            self.profile("test:1", (2 * GIB, 3 * GIB, 5 * GIB) + (GIB,) * 17)))
        result = progress.window_peak(profile, progress.ProgressCounter("build:1", 90, 100), 20)
        assert isinstance(result, progress.WindowPeak)
        self.assertEqual(result.bytes, 5 * GIB)

    def test_the_user_peak_enters_the_window_twenty_percent_before_it_occurs(self) -> None:
        low = GIB // 10
        high = 49 * GIB // 10
        profile = progress.ProgressProfile((self.profile("build:1", (low,) * 16 + (high,) + (low,) * 3),))
        observed: list[int] = []
        for done in (0, 20, 40, 60, 80, 100):
            result = progress.window_peak(profile, progress.ProgressCounter("build:1", done, 100), 20)
            assert isinstance(result, progress.WindowPeak)
            observed.append(result.bytes)
        self.assertEqual(observed, [low, low, low, high, high, low])

    def test_a_phase_restart_without_a_matching_phase_has_no_window(self) -> None:
        profile = progress.ProgressProfile((self.profile("build:1", (GIB,) * 20),))
        self.assertIsInstance(progress.window_peak(profile,
            progress.ProgressCounter("build:2", 10, 100), 20), progress.NoMatchingProfile)

    def test_missing_counter_profile_or_window_samples_cannot_lower_the_need(self) -> None:
        complete = progress.ProgressProfile((self.profile("build:1", (GIB,) * 20),))
        sparse = progress.ProgressProfile((progress.PhaseProfile("build:1",
            (GIB,) + (progress.UnmeasuredBin(),) * 19),))
        cases = (
            (complete, progress.NoCounter()),
            (progress.NoMatchingProfile(), progress.ProgressCounter("build:1", 0, 100)),
            (sparse, progress.ProgressCounter("build:1", 0, 100)),
        )
        for profile, counter in cases:
            with self.subTest(profile=profile, counter=counter):
                self.assertIsInstance(progress.window_peak(profile, counter, 20), progress.NoMatchingProfile)

    def test_each_phase_bin_keeps_its_highest_sample_until_history_expires(self) -> None:
        recorder = progress.ProfileRecorder()
        _ = recorder.observe(progress.ProgressCounter("build:1", 6, 100), 2 * GIB)
        _ = recorder.observe(progress.ProgressCounter("build:1", 9, 100), GIB)
        _ = recorder.observe(progress.ProgressCounter("test:1", 6, 100), 3 * GIB)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            progress.save_profile(path, recorder.profile(), at=100)
            restored = progress.read_profile(path, cutoff=99)
            for phase, expected in (("build:1", 2 * GIB), ("test:1", 3 * GIB)):
                with self.subTest(phase=phase):
                    result = progress.window_peak(restored, progress.ProgressCounter(phase, 6, 100), 1)
                    assert isinstance(result, progress.WindowPeak)
                    self.assertEqual(result.bytes, expected)
            self.assertIsInstance(progress.read_profile(path, cutoff=101), progress.NoMatchingProfile)

    def test_legacy_gate_records_are_not_anonymous_progress_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gate.json"
            _ = path.write_text('{"step":"merge-gate","anon_peak_bytes":99999999999,"at":100}')
            self.assertIsInstance(progress.read_profile(path, cutoff=99), progress.NoMatchingProfile)
            self.assertIsInstance(progress.read_profile(path.with_name("missing.json"), cutoff=99), progress.NoMatchingProfile)


class ProfileHistoryTests(unittest.TestCase):
    def test_gate_tail_window_stops_before_sparse_rerun_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            history = progress.ProfileHistory(Path(temporary))
            for episode, rerun in (("first-pass", False), ("old-rerun", True)):
                for index in range(20):
                    history.observe(episode, "gate", progress.ProgressCounter("suites:1", index * 5, 100),
                        (20 - index) * GIB // 100, 100)
                if rerun:
                    history.observe(episode, "gate", progress.ProgressCounter("suites:2", 0, 100), 9 * GIB, 100)
                history.finish(episode, 101, 90)
            profile = progress.ProfileHistory(Path(temporary)).matching("gate", 90)
            for done, expected in ((86, 3 * GIB // 100), (99, GIB // 100)):
                with self.subTest(done=done):
                    window = progress.window_peak(profile, progress.ProgressCounter("suites:1", done, 100), 20)
                    self.assertEqual(window, progress.WindowPeak(expected, 20))
            self.assertIsInstance(progress.window_peak(profile,
                progress.ProgressCounter("suites:2", 0, 100), 20), progress.NoMatchingProfile)

    def test_merged_partial_runs_keep_build_before_test_and_preserve_the_cross_phase_peak(self) -> None:
        build = progress.PhaseProfile("build:1", (GIB,) * 20)
        tests = progress.PhaseProfile("test:1", (9 * GIB,) * 20)
        merged = progress.merge_profiles((progress.ProgressProfile((tests,)), progress.ProgressProfile((build, tests))))
        peak = progress.window_peak(merged, progress.ProgressCounter("build:1", 90, 100), 20)
        assert isinstance(peak, progress.WindowPeak)
        self.assertEqual(peak.bytes, 9 * GIB)

    def test_merged_runs_keep_numbered_phases_in_run_order(self) -> None:
        first = progress.PhaseProfile("build:1", (GIB,) * 20)
        second = progress.PhaseProfile("build:2", (9 * GIB,) * 20)
        merged = progress.merge_profiles((progress.ProgressProfile((second,)), progress.ProgressProfile((first,))))
        peak = progress.window_peak(merged, progress.ProgressCounter("build:1", 90, 100), 20)
        assert isinstance(peak, progress.WindowPeak)
        self.assertEqual(peak.bytes, 9 * GIB)

    def test_width_changes_publish_each_sample_under_its_own_history_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            history = progress.ProfileHistory(Path(temporary))
            counter = progress.ProgressCounter("build:1", 5, 100)
            history.observe("episode", "eight", counter, GIB, 100)
            history.observe("episode", "four", counter, 2 * GIB, 101)
            history.finish("episode", 102, 90)
            for key, expected in (("eight", GIB), ("four", 2 * GIB)):
                peak = progress.window_peak(history.matching(key, 90), counter, 1)
                assert isinstance(peak, progress.WindowPeak)
                self.assertEqual(peak.bytes, expected)

    def test_restart_publishes_saved_samples_when_the_owner_ends_before_another_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            counter = progress.ProgressCounter("build:1", 5, 100)
            progress.ProfileHistory(root).observe("episode", "build", counter, 2 * GIB, 100)
            restarted = progress.ProfileHistory(root)
            restarted.finish("episode", 101, 90)
            peak = progress.window_peak(restarted.matching("build", 90), counter, 1)
            assert isinstance(peak, progress.WindowPeak)
            self.assertEqual(peak.bytes, 2 * GIB)
            self.assertEqual(list(root.rglob("*.active")), [])

    def test_orphan_active_samples_expire_with_the_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            progress.ProfileHistory(root).observe("gone", "build", progress.ProgressCounter("build:1", 5, 100), GIB, 100)
            restarted = progress.ProfileHistory(root)
            restarted.prune(101)
            self.assertIsInstance(restarted.matching("build", 101), progress.NoMatchingProfile)
            self.assertEqual(list(root.rglob("*.active")), [])

    def test_failed_publication_can_retry_the_same_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            history = progress.ProfileHistory(Path(temporary))
            counter = progress.ProgressCounter("build:1", 5, 100)
            history.observe("episode", "build", counter, 2 * GIB, 100)
            with patch.object(progress, "atomic_sample", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    history.finish("episode", 101, 90)
            history.finish("episode", 102, 90)
            peak = progress.window_peak(history.matching("build", 90), counter, 1)
            assert isinstance(peak, progress.WindowPeak)
            self.assertEqual(peak.bytes, 2 * GIB)

    def test_an_identical_observation_retries_a_failed_sample_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = progress.ProfileHistory(root)
            counter = progress.ProgressCounter("build:1", 5, 100)
            with patch.object(progress, "atomic_sample", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    history.observe("episode", "build", counter, GIB, 100)
            history.observe("episode", "build", counter, GIB, 101)
            restarted = progress.ProfileHistory(root)
            restarted.finish("episode", 102, 90)
            peak = progress.window_peak(restarted.matching("build", 90), counter, 1)
            assert isinstance(peak, progress.WindowPeak)
            self.assertEqual(peak.bytes, GIB)

    def test_profile_records_preserve_tagged_unmeasured_bins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            recorder = progress.ProfileRecorder()
            _ = recorder.observe(progress.ProgressCounter("build:1", 5, 100), GIB)
            progress.save_profile(path, recorder.profile(), 100)
            record = progress.read_profile_record(path)
            assert isinstance(record, progress.RecordedProfile)
            self.assertEqual(record.profile, recorder.profile())
            self.assertIn('"kind":"UnmeasuredBin"', path.read_text())

    def test_completed_history_is_rebuilt_when_its_oldest_sample_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            history = progress.ProfileHistory(Path(temporary))
            counter = progress.ProgressCounter("build:1", 5, 100)
            history.observe("old", "build", counter, 9 * GIB, 100)
            history.finish("old", 100, 0)
            history.observe("new", "build", counter, GIB, 200)
            history.finish("new", 200, 0)
            peak = progress.window_peak(history.matching("build", 101), counter, 1)
            assert isinstance(peak, progress.WindowPeak)
            self.assertEqual(peak.bytes, GIB)

    def test_an_unchanged_capture_retains_its_last_counter_and_unused_cursors_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "cargo.log"
            _ = path.write_text("Building [=>] 1/4: example")
            reader = progress.CaptureReader(root)
            first = reader.log_counter(path)
            reader.end_pass()
            self.assertEqual(reader.log_counter(path), first)
            reader.end_pass()
            reader.end_pass()
            _ = path.write_text("Building [=>] 2/4: replacement")
            self.assertEqual(reader.log_counter(path), progress.ProgressCounter("build:1", 2, 4))

    def test_cargo_counter_wins_when_one_frame_also_contains_test_status(self) -> None:
        counter = progress.parse_progress("Building [=>] 1/4: PASS [0.1s] (2/9) example")
        self.assertEqual(counter, progress.ProgressCounter("build:1", 1, 4))


if __name__ == "__main__":
    _ = unittest.main()
