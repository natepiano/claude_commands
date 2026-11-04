"""The replay separates hold causes and reports missing OOM evidence."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path

from scripts.build_queue import budget_replay
from scripts.build_queue.queue_fixture import NOW, QueueFixtureTests, write_memory_config
from scripts.lint import memory_admit as gate

GIB = gate.GIB


class BudgetReplayTests(QueueFixtureTests):
    def configuration(self, *, floor: str = "10") -> gate.MemoryConfig:
        _ = os.environ.pop("BUILDLOG_BUILD_BUDGET_GIB", None)
        write_memory_config(self.fixture.root / "build_admission.conf", build_budget_gib=floor,
            build_budget_ceiling_gib="30", build_budget_window_seconds="30",
            build_budget_raise_gib="2", build_budget_raise_interval_seconds="10",
            build_free_check_calm="largest")
        return gate.read_memory_config()

    @staticmethod
    def frame(at: float) -> gate.QueueInputs:
        step = gate.RunningStep("run", "worker", "nextest", "/tree", 8 * GIB,
            gate.MeasuredMemory(2 * GIB, promise=gate.RampPromise(8 * GIB)))
        waiter: gate.Waiter = {"pid": 123, "start_time": "456", "need": 4 * GIB, "since": NOW}
        return gate.QueueInputs([(Path("123.waiting"), waiter)], (step,), (),
            gate.HostMemory(40 * GIB, 60 * GIB), gate.NoPressureHold(),
            gate.NoRecoverySpacing(), gate.NoHeadAdmissionOverride(), at)

    @staticmethod
    def sample(at: float, *, builds: int = 0, ci: int = 0, boot: str = "boot") -> dict[str, object]:
        return {"at": datetime.fromtimestamp(at, UTC).isoformat(), "boot_id": boot,
            "mem_used_bytes": 5 * GIB, "swap_used_bytes": 0,
            "stall_full_us": 0, "stall_some_us": 0,
            "slices": {"builds": {"identity": 1, "events": {"oom_kill": builds}},
                       "ci": {"identity": 2, "events": {"oom_kill": ci}}}}

    @staticmethod
    def lifetime(pid: int, since: float, admitted: float, need: int,
                 end: gate.ReservationEnded | gate.StepStillOpen) -> gate.QueueStepLifetime:
        interval = gate.ReservationInterval(str(pid), admitted, need, "fallback", 24 * GIB,
            True, gate.WindowNotStarted(), end)
        return gate.QueueStepLifetime(pid, str(pid), since, "nextest", "repo", (interval,), (), end, admitted - since)

    def test_unmeasured_grants_keep_their_promise_and_remaining_growth(self) -> None:
        config = self.configuration()
        ends = (gate.StepStillOpen(), gate.ReservationEnded(NOW + 60, "completed", gate.StepEnd(), gate.NoPositiveSample()))
        for end in ends:
            with self.subTest(end=end):
                step = self.lifetime(1, NOW, NOW, 8 * GIB, end)
                frame = budget_replay.replay_frames([step], [self.sample(NOW + 10)], 60 * GIB, config)[0]
                self.assertIsInstance(frame.running[0].measured, gate.PromisedMemory)
                decision = gate.decide(60 * GIB, 60 * GIB, 4 * GIB, [step.measured for step in frame.running], limits=gate.AdmissionLimits(24 * GIB, 3 * GIB))
                self.assertEqual(decision.measured, 0)
                self.assertEqual(decision.threshold, 15 * GIB)

    def test_recorded_ci_memory_blocks_a_waiter_that_exceeds_the_budget(self) -> None:
        config = self.configuration(floor="24")
        samples = [self.sample(NOW), self.sample(NOW + 10)]
        for sample in samples:
            sample["slices"] = {"ci": {"state": "present", "current_bytes": 5 * GIB,
                "stat": {"inactive_file": GIB}, "swap_current_bytes": 2 * GIB}}
        steps = [self.lifetime(1, NOW, NOW, 18 * GIB, gate.StepStillOpen()),
                 self.lifetime(2, NOW, NOW + 20, 4 * GIB, gate.StepStillOpen())]
        frames = budget_replay.replay_frames(steps, samples, 60 * GIB, config)
        result = budget_replay.estimate(frames, config)
        self.assertEqual(result.fixed_wait_seconds, 10)
        self.assertEqual(result.budget_hold_seconds, 10)
        ci = frames[0].running[-1].measured
        self.assertIsInstance(ci, gate.UnownedSliceMemory)
        if not isinstance(ci, gate.UnownedSliceMemory):
            self.fail("the CI reading is missing")
        self.assertEqual(ci.bytes, 4 * GIB)

    def test_cgroup_replacement_never_proves_zero_ooms(self) -> None:
        samples = [self.sample(NOW, builds=5), self.sample(NOW + 10, builds=5)]
        samples[1]["slices"] = {"builds": {"identity": 3, "events": {"oom_kill": 5}},
                                "ci": {"identity": 2, "events": {"oom_kill": 0}}}
        result = budget_replay.oom_evidence(samples, [], [], ci_slice="hana-ci.slice", journals_complete=True)
        self.assertIn("builds cgroup identity changed or unavailable", result.incomplete)

    def test_lifecycle_boundaries_preserve_waits_between_machine_samples(self) -> None:
        config = self.configuration(floor="24")
        end = gate.ReservationEnded(NOW + 50, "completed", gate.StepEnd(), gate.NoPositiveSample())
        step = self.lifetime(1, NOW + 10, NOW + 20, 4 * GIB, end)
        samples = [self.sample(NOW), self.sample(NOW + 60)]
        for sample in samples:
            sample["mem_used_bytes"] = 59 * GIB
        frames = budget_replay.replay_frames([step], samples, 60 * GIB, config)
        self.assertEqual([frame.now - NOW for frame in frames], [0, 10, 20, 50, 60])
        result = budget_replay.estimate(frames, config)
        self.assertEqual(result.fixed_wait_seconds, 10)
        self.assertEqual(result.adaptive_wait_seconds, 10)

        config = self.configuration()
        running_end = gate.ReservationEnded(NOW + 60, "completed", gate.StepEnd(), gate.PeakSampled(2 * GIB))
        waiting_end = gate.ReservationEnded(NOW + 55, "completed", gate.StepEnd(), gate.PeakSampled(GIB))
        steps = [self.lifetime(1, NOW, NOW, 8 * GIB, running_end),
                 self.lifetime(2, NOW + 40, NOW + 50, 4 * GIB, waiting_end)]
        samples = [self.sample(NOW), self.sample(NOW + 1), self.sample(NOW + 60)]
        result = budget_replay.estimate(budget_replay.replay_frames(steps, samples, 60 * GIB, config), config)
        self.assertEqual(result.fixed_wait_seconds, 10)
        self.assertEqual(result.adaptive_wait_seconds, 0)
        self.assertEqual(result.budget_saved_seconds, 10)

    def test_budget_wait_savings_are_deterministic_and_separate_from_free_memory(self) -> None:
        config = self.configuration()
        frames = [self.frame(NOW + seconds) for seconds in (0, 30, 40)]
        result = budget_replay.estimate(frames, config)
        self.assertEqual(result.fixed_wait_seconds, 40)
        self.assertEqual(result.adaptive_wait_seconds, 30)
        self.assertEqual(result.budget_saved_seconds, 10)
        self.assertEqual(result.free_saved_seconds, 0)
        self.assertEqual(result.saved_seconds, 10)
        self.assertEqual(budget_replay.estimate(frames, config), result)

    def test_calm_free_check_savings_are_separate_from_budget_savings(self) -> None:
        config = self.configuration(floor="20")
        second = gate.RunningStep("other", "worker", "nextest", "/tree", 5 * GIB,
            gate.MeasuredMemory(GIB, promise=gate.RampPromise(5 * GIB)))
        frames: list[gate.QueueInputs] = []
        for seconds in (0, 30, 40):
            frame = self.frame(NOW + seconds)
            frames.append(replace(frame, running=(*frame.running, second), host=gate.HostMemory(13 * GIB, 60 * GIB)))
        result = budget_replay.estimate(frames, config)
        self.assertEqual(result.fixed_wait_seconds, 40)
        self.assertEqual(result.adaptive_wait_seconds, 30)
        self.assertEqual(result.budget_saved_seconds, 0)
        self.assertEqual(result.free_saved_seconds, 10)

    def test_pressure_free_memory_and_token_holds_have_separate_totals(self) -> None:
        config = self.configuration()
        pressure = replace(self.frame(NOW), pressure=gate.MemoryPressure(90, condition="full"))
        free = replace(self.frame(NOW + 10), host=gate.HostMemory(GIB, 60 * GIB))
        token = replace(self.frame(NOW + 20), ordered=[(Path("123.waiting"), {
            "pid": 123, "start_time": "456", "need": 4 * GIB, "since": NOW, "token_left_at": NOW + 20})])
        result = budget_replay.estimate([pressure, free, token, self.frame(NOW + 30)], config)
        self.assertEqual(result.fixed_wait_seconds, 20)
        self.assertEqual(result.adaptive_wait_seconds, 20)
        self.assertEqual(result.budget_hold_seconds, 20)
        self.assertEqual(result.free_hold_seconds, 10)
        self.assertEqual(result.pressure_hold_seconds, 10)
        self.assertEqual(result.token_hold_seconds, 10)

    def test_replay_rejects_reversed_time_and_oversized_input_before_loading_it(self) -> None:
        config = self.configuration()
        with self.assertRaisesRegex(ValueError, "times must increase"):
            _ = budget_replay.estimate([self.frame(NOW + 1), self.frame(NOW)], config)
        path = self.fixture.root / "oversized.jsonl"
        with path.open("wb") as stream:
            _ = stream.truncate(129 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "replay inputs exceed"):
            _ = budget_replay.bounded_rows([path])

    def test_lifecycle_token_gap_stays_a_token_wait_in_replay_frames(self) -> None:
        config = self.configuration()
        interval = gate.ReservationInterval("grant", NOW + 40, 4 * GIB, "measured", 10 * GIB,
            True, gate.WindowNotStarted(), gate.StepStillOpen())
        step = gate.QueueStepLifetime(123, "456", NOW, "nextest", "repo", (interval,),
            (gate.QueueTokenGap(NOW + 10, NOW + 30),), gate.StepStillOpen(), 20)
        frames = budget_replay.replay_frames([step], [self.sample(NOW + 20), self.sample(NOW + 30)], 60 * GIB, config)
        self.assertIsInstance(gate.waiter_state(frames[0].ordered[0][1]), gate.TokenWaitingWaiter)
        self.assertIsInstance(gate.waiter_state(frames[1].ordered[0][1]), gate.QueuedWaiter)

    def test_nested_build_and_ci_ooms_count_without_unrelated_process_kills(self) -> None:
        samples = [self.sample(NOW, builds=1), self.sample(NOW + 10, builds=3, ci=1)]
        kernel = ["oom-kill: task_memcg=/builds.slice/run-job.scope task=rustc",
                  "Killed process 123 in /hana-ci.slice/job.scope",
                  "Killed process 124 in /user.slice/browser.scope"]
        earlyoom = ["sending SIGKILL to process 123 in /builds.slice/run-job.scope",
                    "sending SIGKILL to process 124 in /user.slice/browser.scope"]
        evidence = budget_replay.oom_evidence(samples, kernel, earlyoom,
            ci_slice="hana-ci.slice", journals_complete=True)
        self.assertEqual(evidence.kernel_kills, 2)
        self.assertEqual(evidence.earlyoom_kills, 1)
        self.assertEqual(evidence.cgroup_kills, 3)
        self.assertEqual(evidence.incomplete, ())

    def test_missing_journals_endpoints_or_reset_counters_never_prove_zero_ooms(self) -> None:
        cases: tuple[tuple[list[dict[str, object]], bool], ...] = (([], True), ([self.sample(NOW), self.sample(NOW + 10)], False),
                 ([self.sample(NOW, builds=2), self.sample(NOW + 10)], True),
                 ([self.sample(NOW), self.sample(NOW + 10, boot="new")], True))
        for samples, complete in cases:
            with self.subTest(samples=samples, journals_complete=complete):
                evidence = budget_replay.oom_evidence(samples, [], [],
                    ci_slice="hana-ci.slice", journals_complete=complete)
                self.assertTrue(evidence.incomplete)
