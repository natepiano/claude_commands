"""Progress counters and the remaining anonymous memory need."""
from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from typing import cast
from unittest.mock import patch


from scripts.build_queue import progress_profile as progress
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.queue_fixture import write_memory_config
from scripts.lib import conf

GIB = 1 << 30

# These records preserve the two gate outliers from 2026-11-07.
BAD_GATE_RECORDS: tuple[tuple[str, str], ...] = (
    ('1113174-59791205-1794084730975817216.json',
        '{"measurement":"attributed_anonymous","record":{"kind":"RecordedProfile","profile":{"kind":"ProgressProfile","phases":[{"kind":"PhaseProfile","phase":"suites:1","bins":[1497841664,1413365760,1918402560,18596134912,1029701632,761946112,916357120,965931008,969994240,994250752,651124736,180883456,180883456,1229758464,137351168,123850752,82882560,94289920,75440128,60465152]}]},"at":1794086888.881132}}'),
    ('1844752-60085669-1794087676089650432.json',
        '{"measurement":"attributed_anonymous","record":{"kind":"RecordedProfile","profile":{"kind":"ProgressProfile","phases":[{"kind":"PhaseProfile","phase":"suites:1","bins":[1657450496,1565593600,1434177536,1321656320,2641358848,17153081344,962326528,1150939136,1150939136,955875328,955875328,708452352,686346240,{"kind":"UnmeasuredBin"},686346240,430080000,427921408,{"kind":"UnmeasuredBin"},427921408,{"kind":"UnmeasuredBin"}]}]},"at":1794088680.5554764}}'),
)


def setUpModule() -> None:
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    write_memory_config(root / "build_admission.conf")
    _ = cast(object, unittest.enterModuleContext(patch.dict(os.environ, {"CONF_DIR": str(root)})))


class ProfileRankTests(unittest.TestCase):
    @staticmethod
    def runs() -> tuple[progress.ProgressProfile, ...]:
        return tuple(progress.ProgressProfile((progress.PhaseProfile("build:1", values * 5),))
            for values in ((9 * GIB, GIB, 5 * GIB, 2 * GIB),
                           (3 * GIB, 8 * GIB, GIB, 4 * GIB),
                           (6 * GIB, 4 * GIB, 7 * GIB, 3 * GIB)))

    def test_rank_floor_and_share_select_each_bin(self) -> None:
        ordinary = progress.ProgressProfile((progress.PhaseProfile("build:1", (GIB,) * 20),))
        runaway = tuple(progress.ProgressProfile((progress.PhaseProfile("build:1", (peak,) * 20),))
                        for peak in (16 * GIB, 26 * GIB))
        for runs, expected in (
            (self.runs(), (6 * GIB, 4 * GIB, 5 * GIB, 3 * GIB) * 5),
            ((ordinary,) * 20 + runaway, (GIB,) * 20),
        ):
            with self.subTest(count=len(runs)):
                merged = progress.merge_profiles(runs, rank=2)
                assert isinstance(merged, progress.ProgressProfile)
                self.assertEqual(merged.phases[0].bins, expected)

    def test_rank_one_keeps_the_highest_run_in_each_bin(self) -> None:
        merged = progress.merge_profiles(self.runs(), rank=1)
        assert isinstance(merged, progress.ProgressProfile)
        self.assertEqual(merged.phases[0].bins, (9 * GIB, 8 * GIB, 7 * GIB, 4 * GIB) * 5)

    def test_rank_two_with_one_run_keeps_that_run(self) -> None:
        run = self.runs()[0]
        self.assertEqual(progress.merge_profiles((run,), rank=2), run)

    def test_sparse_bins_use_the_lowest_available_run_below_the_rank(self) -> None:
        missing = progress.UnmeasuredBin()
        first = progress.ProgressProfile((progress.PhaseProfile("build:1", (GIB, missing) * 10),))
        second = progress.ProgressProfile((progress.PhaseProfile("build:1", (3 * GIB, 2 * GIB) * 10),))
        merged = progress.merge_profiles((first, second), rank=3)
        assert isinstance(merged, progress.ProgressProfile)
        self.assertEqual(merged.phases[0].bins, (GIB, 2 * GIB) * 10)

    def test_old_records_without_source_or_process_evidence_still_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            name, raw = BAD_GATE_RECORDS[0]
            path = Path(temporary) / name
            _ = path.write_text(raw)
            record = progress.read_profile_record(path)
            assert isinstance(record, progress.RecordedProfile)
            self.assertIsInstance(record.reading_source, progress.ReadingSourceNotRecorded)
            self.assertIsInstance(record.peak_processes, progress.PeakProcessesNotRecorded)
            self.assertEqual(record.at, 1794086888.881132)
            self.assertEqual(record.profile.phases[0].bins[3], 18596134912)

    def test_a_configuration_change_rebuilds_the_cached_rank(self) -> None:
        with AdmissionFixture() as fixture:
            root = fixture.root / "private-profiles"
            directory = root / "build"
            directory.mkdir(parents=True)
            for index, profile in enumerate(self.runs()):
                progress.save_profile(directory / f"run-{index}.json", profile, 100)
            history = progress.ProfileHistory(root)
            config = fixture.root / "build_admission.conf"
            for rank, share in ((2, 10), (1, 10), (1, 50), (1, 0), (2, 10), (1, 0)):
                with self.subTest(rank=rank, share=share):
                    write_memory_config(config, progress_profile_rank=str(rank), peak_history_share_percent=str(share))
                    history.prune(90)
                    self.assertEqual(history.matching("build", 90), progress.merge_profiles(self.runs(), rank=rank))
            write_memory_config(config, progress_profile_rank="1", peak_history_share_percent="50")
            self.assertEqual(history.matching("build", 90), progress.merge_profiles(self.runs(), rank=2, share_percent=0))

    def test_saved_records_preserve_the_source_and_the_highest_bin_processes(self) -> None:
        with AdmissionFixture() as fixture:
            root = fixture.root / "private-profiles"
            history = progress.ProfileHistory(root)
            source = progress.ScopeReading(fixture.root / "private.scope")
            first = (progress.ProfileProcess(101, ("rustc", "--private-first"), GIB),)
            highest = (progress.ProfileProcess(202, ("rustc", "--private-highest"), 3 * GIB),)
            for done, need, processes in ((10, GIB, first), (20, 3 * GIB, highest), (80, 2 * GIB, first)):
                history.observe("episode", "build", progress.ProgressCounter("build:1", done, 100), need, 100,
                    reading_source=source, read_processes=lambda processes=processes: processes)
            history.finish("episode", 101, 90)
            path, = root.glob("*/*.json")
            record = progress.read_profile_record(path)
            assert isinstance(record, progress.RecordedProfile)
            self.assertEqual(record.reading_source, source)
            self.assertEqual(record.peak_processes, progress.PeakProcesses("build:1", 4, 3 * GIB, highest))

    def test_restart_preserves_the_saved_highest_bin_evidence(self) -> None:
        with AdmissionFixture() as fixture:
            root = fixture.root / "private-profiles"
            source = progress.ProcessTreeReading((101,))
            processes = (progress.ProfileProcess(101, ("private-build",), 3 * GIB),)
            progress.ProfileHistory(root).observe("episode", "build", progress.ProgressCounter("build:1", 20, 100),
                3 * GIB, 100, reading_source=source, read_processes=lambda: processes)
            restarted = progress.ProfileHistory(root)
            restarted.observe("episode", "build", progress.ProgressCounter("build:1", 80, 100), GIB, 101)
            restarted.finish("episode", 102, 90)
            path, = root.glob("*/*.json")
            record = progress.read_profile_record(path)
            assert isinstance(record, progress.RecordedProfile)
            self.assertEqual(record.reading_source, source)
            self.assertEqual(record.peak_processes, progress.PeakProcesses("build:1", 4, 3 * GIB, processes))

    def test_invalid_progress_profile_ranks_fail_the_configuration_check(self) -> None:
        with AdmissionFixture() as fixture:
            path = fixture.root / "build_admission.conf"
            conf.check(path, "progress_profile_rank", "1")
            conf.check(path, "progress_profile_rank", "2")
            for value in ("0", "-1", "text"):
                with self.subTest(value=value), self.assertRaises(conf.Unusable):
                    conf.check(path, "progress_profile_rank", value)
            for value in ("0", "10", "50"):
                conf.check(path, "peak_history_share_percent", value)
            for value in ("-1", "51", "0.1", "text"):
                with self.subTest(share=value), self.assertRaises(conf.Unusable):
                    conf.check(path, "peak_history_share_percent", value)

    def test_two_saved_gate_outliers_do_not_set_the_rank_two_need(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profiles: list[progress.ProgressProfile] = []
            for name, raw in BAD_GATE_RECORDS:
                path = root / name
                _ = path.write_text(raw)
                record = progress.read_profile_record(path)
                assert isinstance(record, progress.RecordedProfile)
                profiles.append(record.profile)
            for index, need in enumerate((8 * GIB // 10, GIB, 12 * GIB // 10)):
                ordinary = progress.ProgressProfile((progress.PhaseProfile("suites:1", (need,) * 20),))
                path = root / f"ordinary-{index}.json"
                progress.save_profile(path, ordinary, 1794089000)
                record = progress.read_profile_record(path)
                assert isinstance(record, progress.RecordedProfile)
                profiles.append(record.profile)
            highest = progress.merge_profiles(profiles, rank=1)
            ranked = progress.merge_profiles(profiles, rank=2)
            largest_ranked_need = 0
            largest_highest_need = 0
            for done in range(0, 100, 5):
                counter = progress.ProgressCounter("suites:1", done, 100)
                rank_two = progress.window_peak(ranked, counter, 20)
                rank_one = progress.window_peak(highest, counter, 20)
                assert isinstance(rank_two, progress.WindowPeak)
                assert isinstance(rank_one, progress.WindowPeak)
                largest_ranked_need = max(largest_ranked_need, rank_two.bytes)
                largest_highest_need = max(largest_highest_need, rank_one.bytes)
            self.assertLess(largest_ranked_need, 3 * GIB)
            self.assertGreater(largest_highest_need, 15 * GIB)


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
