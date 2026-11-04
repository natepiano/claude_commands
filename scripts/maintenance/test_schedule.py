"""Persisted schedules isolate overlapping passes and preserve due work."""
from __future__ import annotations

import tempfile
import os
import unittest
from pathlib import Path

from scripts.maintenance.schedule import IntervalHistory, ScheduleBusy, locked_history


class ScheduleTests(unittest.TestCase):
    def test_due_state_survives_reopening_and_a_busy_pass_does_not_overwrite_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with locked_history(directory) as history:
                self.assertIsInstance(history, IntervalHistory)
                assert isinstance(history, IntervalHistory)
                self.assertTrue(history.is_due("usage", 60, 120))
                history.record("usage", 120)
                before = history.path.read_bytes(), history.path.stat().st_mtime_ns
                with locked_history(directory) as busy:
                    self.assertIsInstance(busy, ScheduleBusy)
                    assert isinstance(busy, ScheduleBusy)
                    self.assertEqual(busy.owner_id, str(os.getpid()))
                self.assertEqual((history.path.read_bytes(), history.path.stat().st_mtime_ns), before)
            with locked_history(directory) as reopened:
                assert isinstance(reopened, IntervalHistory)
                self.assertFalse(reopened.is_due("usage", 60, 179))
                self.assertTrue(reopened.is_due("usage", 60, 180))

    def test_invalid_history_refuses_without_replacing_the_saved_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "completed.json"
            _ = path.write_text("[]\n")
            with self.assertRaisesRegex(ValueError, "expected completion times"), locked_history(directory):
                self.fail("invalid history was accepted")
            self.assertEqual(path.read_text(), "[]\n")


if __name__ == "__main__":
    _ = unittest.main()
