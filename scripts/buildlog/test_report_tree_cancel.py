"""Cancelled verification appears as an outcome, never as failure recovery work."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from typing import override

import index
import report
from test_index import call, encode, local_day, point_root_at, STAMP


class CancelledReportTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        point_root_at(self, self.root)

    def test_cancelled_call_is_known_and_does_not_enter_failure_recovery(self) -> None:
        path = self.root / "natedev/2026-10.jsonl"
        path.parent.mkdir(parents=True)
        records = (
            call("green", unit_session="worker", started_at="2026-10-02T12:00:00Z",
                 ended_at="2026-10-02T12:00:00Z"),
            call("cancelled", unit_session="worker", outcome="cancelled", status=77,
                 started_at="2026-10-02T12:05:00Z", ended_at="2026-10-02T12:05:00Z"),
            call("red", unit_session="worker", outcome="failed", status=1,
                 started_at="2026-10-02T12:10:00Z", ended_at="2026-10-02T12:10:00Z"),
            call("green-again", unit_session="worker", started_at="2026-10-02T12:20:00Z",
                 ended_at="2026-10-02T12:20:00Z"),
        )
        _ = path.write_bytes(b"".join(encode(record) for record in records))
        _ = index.update()
        with closing(index.read_only()) as connection:
            window = report.tests_per_edit_data(connection, local_day(STAMP))
            outcomes = report.outcomes(connection, local_day(STAMP), "verify.sh")
        self.assertEqual(sum(bucket.failures for bucket in window.recovery_bins), 1,
                         "a cancelled run was counted as a failed test")
        self.assertEqual(sum(bucket.recovered for bucket in window.recovery_bins), 1)
        self.assertEqual(sum(bucket.minutes_to_green for bucket in window.recovery_bins), 10)
        self.assertEqual([(row[0], row[1]) for row in outcomes if row[0] == "cancelled"],
                         [("cancelled", 1)], "cancelled disappeared from the outcome report")
        self.assertIn("cancelled", report.OUTCOME_ORDER,
                      "cancelled is still treated as an unknown outcome")


if __name__ == "__main__":
    _ = unittest.main()
