"""Test merged time spans and their combined duration."""

import unittest

from scripts.lib.intervals import merged_spans, union_seconds


class IntervalTests(unittest.TestCase):
    def test_spans_merge_and_measure_duration(self) -> None:
        cases: list[tuple[str, list[tuple[float, float]], list[tuple[float, float]], float]] = [
            ("empty", [], [], 0.0),
            ("out of order", [(8.0, 10.0), (1.0, 3.0), (5.0, 6.0)],
             [(1.0, 3.0), (5.0, 6.0), (8.0, 10.0)], 5.0),
            ("overlapping", [(1.0, 4.0), (3.0, 7.0), (6.0, 9.0)], [(1.0, 9.0)], 8.0),
            ("contained", [(1.0, 9.0), (2.0, 4.0)], [(1.0, 9.0)], 8.0),
            ("touching", [(1.0, 3.0), (3.0, 5.0)], [(1.0, 5.0)], 4.0),
            ("gap", [(1.0, 3.0), (7.0, 10.0)], [(1.0, 3.0), (7.0, 10.0)], 5.0),
        ]
        for name, spans, merged, seconds in cases:
            with self.subTest(name=name):
                self.assertEqual(merged_spans(spans), merged)
                self.assertEqual(union_seconds(spans), seconds)

    def test_generators_supply_spans_once(self) -> None:
        spans = [(1.0, 4.0), (3.0, 7.0), (6.0, 9.0)]
        self.assertEqual(merged_spans(span for span in spans), [(1.0, 9.0)])
        self.assertEqual(union_seconds(span for span in spans), 8.0)


if __name__ == "__main__":
    _ = unittest.main()
