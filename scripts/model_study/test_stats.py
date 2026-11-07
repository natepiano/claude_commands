"""Focused tests for comparison statistics and API-equivalent prices."""

from __future__ import annotations

import statistics
import unittest
from dataclasses import replace

from stats import PRICES, bootstrap_diff, bootstrap_net, label_difference, percentile, request_cost
from turns import Turn


EMPTY_TURN = Turn(
    session="synthetic-session",
    name="synthetic-director",
    request_id="synthetic-request",
    started="2026-10-06T00:00:00+00:00",
    ended="2026-10-06T00:00:01+00:00",
    seconds=1,
    model="claude-opus-5-5",
    effort="xhigh",
    speed="standard",
    stop="tool_use",
    kind="continuation",
    input=0,
    output=0,
    thinking=0,
    cache_read=0,
    write_5m=0,
    write_1h=0,
    context=0,
    switch_turn=False,
    after_compact=0,
)


class StatisticTests(unittest.TestCase):
    def test_percentile_interpolates_between_ranks(self) -> None:
        values = [4, 1, 3, 2]
        self.assertEqual(percentile(values, 0), 1)
        self.assertEqual(percentile(values, 25), 1.75)
        self.assertEqual(percentile(values, 50), 2.5)
        self.assertEqual(percentile(values, 75), 3.25)
        self.assertEqual(percentile(values, 90), 3.7)
        self.assertEqual(percentile(values, 100), 4)
        self.assertEqual(percentile([7], 50), 7)
        with self.assertRaises(ValueError):
            _ = percentile([], 50)

    def test_bootstrap_difference_is_seeded_and_covers_known_shift(self) -> None:
        before = [1, 2, 3, 4] * 20
        after = [value + 10 for value in before]
        first = bootstrap_diff(before, after, statistics.median, resamples=400, seed=19)
        second = bootstrap_diff(before, after, statistics.median, resamples=400, seed=19)
        self.assertEqual(first, second)
        difference, lower, upper = first
        self.assertEqual(difference, 10)
        self.assertLessEqual(lower, 10)
        self.assertGreaterEqual(upper, 10)
        self.assertGreater(lower, 0)

    def test_bootstrap_net_subtracts_the_control_shift(self) -> None:
        baseline = [40] * 40
        sonnet = [30] * 40
        same_shift = bootstrap_net(baseline, sonnet, [25] * 40, [15] * 40,
                                   statistics.median, resamples=100, seed=19)
        self.assertEqual(same_shift, (0, 0, 0))
        self.assertLessEqual(same_shift[1], 0)
        self.assertGreaterEqual(same_shift[2], 0)
        flat = bootstrap_net(baseline, sonnet, [25] * 40, [25] * 40,
                             statistics.median, resamples=100, seed=19)
        self.assertEqual(flat, (-10, -10, -10))
        self.assertEqual(flat, bootstrap_net(baseline, sonnet, [25] * 40, [25] * 40,
                                             statistics.median, resamples=100, seed=19))

    def test_bootstrap_net_rejects_empty_samples_and_zero_resamples(self) -> None:
        samples = ([1], [2], [3], [4])
        for index in range(4):
            arguments = list(samples)
            arguments[index] = []
            with self.subTest(index=index), self.assertRaises(ValueError):
                _ = bootstrap_net(arguments[0], arguments[1], arguments[2],
                                  arguments[3], statistics.median)
        with self.assertRaises(ValueError):
            _ = bootstrap_net(samples[0], samples[1], samples[2], samples[3],
                              statistics.median, resamples=0)

    def test_each_price_column_and_thinking_subset(self) -> None:
        expected = {
            "claude-opus-5-5": (4, 20, 0.20, 5, 8),
            "claude-sonnet-5-5": (2, 10, 0.20, 2.50, 4),
        }
        for model, (input_price, output_price, read_price, write_5m_price, write_1h_price) in expected.items():
            with self.subTest(model=model):
                self.assertIn(model, PRICES)
                baseline = replace(EMPTY_TURN, model=model)
                self.assertEqual(request_cost(replace(baseline, input=1_000_000)), input_price)
                self.assertEqual(request_cost(replace(baseline, output=1_000_000)), output_price)
                self.assertEqual(request_cost(replace(baseline, cache_read=1_000_000)), read_price)
                self.assertEqual(request_cost(replace(baseline, write_5m=1_000_000)), write_5m_price)
                self.assertEqual(request_cost(replace(baseline, write_1h=1_000_000)), write_1h_price)
                self.assertEqual(request_cost(replace(baseline, output=1_000_000, thinking=900_000)), output_price)
        self.assertEqual(request_cost(replace(EMPTY_TURN, model="unpriced", input=1_000_000)), 0)

    def test_difference_labels_follow_interval_and_sample_rule(self) -> None:
        self.assertEqual(label_difference(-3, -1, 30, 30, "seconds"), "faster")
        self.assertEqual(label_difference(1, 3, 30, 30, "seconds"), "slower")
        self.assertEqual(label_difference(-3, -1, 30, 30, "tokens"), "lower")
        self.assertEqual(label_difference(1, 3, 30, 30, "cost"), "higher")
        self.assertEqual(label_difference(-1, 0, 30, 30, "seconds"), "no measurable difference")
        self.assertEqual(label_difference(0, 1, 30, 30, "cost"), "no measurable difference")
        self.assertEqual(label_difference(-3, -1, 29, 30, "seconds"), "too few")
        self.assertEqual(label_difference(-3, -1, 30, 29, "seconds"), "too few")
