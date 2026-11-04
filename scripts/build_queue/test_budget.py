"""The service budget changes only at the stated pressure and time boundaries."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from unittest import mock

from scripts.build_queue import budget
from scripts.build_queue.queue_fixture import NOW, QueueFixtureTests, write_memory_config
from scripts.lint import memory_admit as gate

GIB = gate.GIB


class BudgetTests(QueueFixtureTests):
    def configuration(self, **changes: str) -> gate.MemoryConfig:
        _ = os.environ.pop("BUILDLOG_BUILD_BUDGET_GIB", None)
        write_memory_config(self.fixture.root / "build_admission.conf", **{
            "build_budget_gib": "10", "build_budget_ceiling_gib": "20",
            "build_budget_window_seconds": "30", "build_budget_raise_gib": "2",
            "build_budget_raise_interval_seconds": "10", "build_free_check_calm": "largest",
            **changes})
        return gate.read_memory_config()

    @staticmethod
    def inputs() -> gate.QueueInputs:
        memory = gate.MeasuredMemory(2 * GIB, promise=gate.RampPromise(8 * GIB))
        running = gate.RunningStep("owner", "worker", "nextest", "/tree", 8 * GIB, memory)
        waiter: gate.Waiter = {"pid": 123, "start_time": "456", "need": 4 * GIB, "since": NOW}
        return gate.QueueInputs([(Path("123.waiting"), waiter)], (running,), (),
            gate.HostMemory(40 * GIB, 60 * GIB), gate.NoPressureHold(),
            gate.NoRecoverySpacing(), gate.NoHeadAdmissionOverride(), NOW)

    def test_full_calm_window_precedes_one_raise_and_missed_intervals_do_not_accumulate(self) -> None:
        config, inputs = self.configuration(), self.inputs()
        state = budget.start_budget(config, NOW)
        state, limits, _ = budget.advance_budget(state, inputs, config, NOW + 29.999)
        self.assertEqual(limits.budget, 10 * GIB)
        state, limits, _ = budget.advance_budget(state, inputs, config, NOW + 30)
        self.assertEqual(limits.budget, 12 * GIB)
        state, limits, _ = budget.advance_budget(state, inputs, config, NOW + 30)
        self.assertEqual(limits.budget, 12 * GIB)
        demanding = replace(inputs, ordered=[(Path("123.waiting"), {
            "pid": 123, "start_time": "456", "need": 12 * GIB, "since": NOW})])
        state, limits, _ = budget.advance_budget(state, demanding, config, NOW + 39.999)
        self.assertEqual(limits.budget, 12 * GIB)
        state, limits, _ = budget.advance_budget(state, demanding, config, NOW + 300)
        self.assertEqual(limits.budget, 14 * GIB)

    def test_idle_or_free_memory_blocked_queue_does_not_raise_the_budget(self) -> None:
        config, inputs = self.configuration(), self.inputs()
        cases = (replace(inputs, ordered=[]),
                 replace(inputs, host=gate.HostMemory(GIB, 60 * GIB)))
        for current in cases:
            with self.subTest(waiters=len(current.ordered), host=current.host):
                state = budget.start_budget(config, NOW)
                _, limits, _ = budget.advance_budget(state, current, config, NOW + 300)
                self.assertEqual(limits.budget, 10 * GIB)

    def test_a_hold_preserves_the_last_raise_time_and_reason(self) -> None:
        config, inputs = self.configuration(), self.inputs()
        state = budget.start_budget(config, NOW)
        state, _, changed = budget.advance_budget(state, inputs, config, NOW + 30)
        _, _, held = budget.advance_budget(state, inputs, config, NOW + 31)
        self.assertEqual(held.budget, changed.budget)
        self.assertEqual(held.changed_at, changed.changed_at)
        self.assertEqual(held.change_reason, "BudgetRaised")
        self.assertEqual(held.hold_reason, "RaiseInterval")

    def test_pressure_or_unavailable_psi_resets_budget_and_restarts_the_calm_window(self) -> None:
        config, inputs = self.configuration(), self.inputs()
        for pressure in (gate.MemoryPressure(90, condition="full"), gate.PressureUnavailable("PSI missing")):
            with self.subTest(pressure=pressure):
                state = budget.start_budget(config, NOW)
                state, raised, _ = budget.advance_budget(state, inputs, config, NOW + 30)
                self.assertEqual(raised.budget, 12 * GIB)
                breached = (replace(inputs, pressure=pressure, pressure_reading=pressure)
                            if isinstance(pressure, gate.MemoryPressure)
                            else replace(inputs, pressure_reading=pressure))
                state, lowered, _ = budget.advance_budget(state, breached, config, NOW + 31)
                self.assertEqual(lowered.budget, 10 * GIB)
                self.assertEqual(lowered.free_check, "sum")
                state, held, _ = budget.advance_budget(state, inputs, config, NOW + 60.999)
                self.assertEqual(held.budget, 10 * GIB)
                _, raised, _ = budget.advance_budget(state, inputs, config, NOW + 61)
                self.assertEqual(raised.budget, 12 * GIB)

    def test_raise_stops_at_the_configured_or_physical_ceiling(self) -> None:
        for ceiling, total, expected in (("11", 60 * GIB, 11 * GIB),
                                         ("20", 12 * GIB, 12 * GIB - 12 * GIB * 5 // 100)):
            with self.subTest(ceiling=ceiling, total=total):
                config = self.configuration(build_budget_ceiling_gib=ceiling)
                inputs = replace(self.inputs(), host=gate.HostMemory(40 * GIB, total))
                state = budget.start_budget(config, NOW)
                state, limits, _ = budget.advance_budget(state, inputs, config, NOW + 30)
                self.assertEqual(limits.budget, expected)
                _, limits, _ = budget.advance_budget(state, inputs, config, NOW + 300)
                self.assertEqual(limits.budget, expected)

    def test_unknown_ram_keeps_the_floor_without_authorizing_a_raise(self) -> None:
        config = self.configuration()
        inputs = replace(self.inputs(), host=gate.HostMemory(40 * GIB, gate.UnknownHostTotal()))
        state = budget.start_budget(config, NOW)
        _, limits, _ = budget.advance_budget(state, inputs, config, NOW + 300)
        self.assertEqual(limits.budget, 10 * GIB)

    def test_operator_override_keeps_a_fixed_budget_and_publishes_its_reason(self) -> None:
        config, inputs = self.configuration(), self.inputs()
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "13"}):
            state = budget.start_budget(config, NOW)
            for pressure in (gate.NoPressureHold(), gate.MemoryPressure(90, condition="full")):
                state, limits, decision = budget.advance_budget(state, replace(inputs, pressure=pressure), config, NOW + 300)
                self.assertEqual(limits.budget, 13 * GIB)
                self.assertEqual(decision.change_reason, "OperatorFixedBudget")

    def test_calm_free_check_counts_sum_half_or_largest_and_pressure_restores_sum(self) -> None:
        first = gate.MeasuredMemory(2 * GIB, promise=gate.RampPromise(8 * GIB))
        second = gate.MeasuredMemory(GIB, promise=gate.RampPromise(5 * GIB))
        for mode, growth in (("sum", 10 * GIB), ("half", 5 * GIB), ("largest", 6 * GIB)):
            with self.subTest(mode=mode):
                config = self.configuration(build_free_check_calm=mode)
                inputs = replace(self.inputs(), ordered=[])
                state = budget.start_budget(config, NOW)
                state, limits, _ = budget.advance_budget(state, inputs, config, NOW + 30)
                decision = gate.decide(40 * GIB, 60 * GIB, 4 * GIB, [first, second], limits=limits)
                self.assertEqual(decision.threshold, 4 * GIB + growth + 3 * GIB)
                state, limits, _ = budget.advance_budget(state,
                    replace(inputs, pressure=gate.MemoryPressure(90, condition="full")), config, NOW + 31)
                decision = gate.decide(40 * GIB, 60 * GIB, 4 * GIB, [first, second], limits=limits)
                self.assertEqual(decision.threshold, 17 * GIB)
