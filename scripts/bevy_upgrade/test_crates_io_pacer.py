"""Exercise shared crates.io request spacing without real-clock waits."""
from __future__ import annotations

import unittest
from typing import TYPE_CHECKING, final

import crates_io_pacer

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        return method


@final
class StandInClock:
    def __init__(self) -> None:
        self.now: float = 0.0
        self.sleeps: list[float] = []

    def read(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@final
class RequestPacerTests(unittest.TestCase):
    clock: StandInClock = StandInClock()

    @override
    def setUp(self) -> None:
        self.clock = StandInClock()

    def test_request_pacer_first_request_does_not_sleep(self) -> None:
        pacer = crates_io_pacer.RequestPacer(1.0, clock=self.clock.read, sleep=self.clock.sleep)
        pacer.wait()
        self.assertEqual(self.clock.sleeps, [])

    def test_request_pacer_sleeps_only_remaining_interval(self) -> None:
        pacer = crates_io_pacer.RequestPacer(1.0, clock=self.clock.read, sleep=self.clock.sleep)
        pacer.wait()
        self.clock.now = 0.25
        pacer.wait()
        self.assertEqual(self.clock.sleeps, [0.75])
        self.assertEqual(self.clock.now, 1.0)

    def test_request_pacer_does_not_sleep_after_interval_and_records_new_start(self) -> None:
        pacer = crates_io_pacer.RequestPacer(1.0, clock=self.clock.read, sleep=self.clock.sleep)
        pacer.wait()
        self.clock.now = 1.25
        pacer.wait()
        self.assertEqual(self.clock.sleeps, [])
        self.clock.now = 1.5
        pacer.wait()
        self.assertEqual(self.clock.sleeps, [0.75])

    def test_request_pacer_back_to_back_requests_sleep_one_second_each(self) -> None:
        pacer = crates_io_pacer.RequestPacer(1.0, clock=self.clock.read, sleep=self.clock.sleep)
        for _ in range(3):
            pacer.wait()
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])

    def test_request_pacer_measures_from_actual_start_after_a_sleep_that_runs_long(self) -> None:
        requested: list[float] = []

        def oversleep(seconds: float) -> None:
            requested.append(seconds)
            self.clock.now += seconds + 0.5

        pacer = crates_io_pacer.RequestPacer(1.0, clock=self.clock.read, sleep=oversleep)
        for _ in range(3):
            pacer.wait()
        self.assertEqual(requested, [1.0, 1.0])
        self.assertEqual(self.clock.now, 3.0)


if __name__ == "__main__":
    _ = unittest.main()
