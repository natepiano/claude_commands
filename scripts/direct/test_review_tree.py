#!/usr/bin/env python3
"""Tests for the changed paths a review leaves to the other phases open beside it."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import phase_reservation, recorder_state, review_tree  # noqa: E402


@final
class OtherPhasesPathsTests(unittest.TestCase):
    session: Path = Path()

    @override
    def setUp(self) -> None:
        self.session = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def open_phases(self, files: dict[str, str]) -> None:
        """The recorder lists each phase id open, and each phase's directory holds its files list."""
        records = [{"id": phase, "status": "active"} for phase in files]
        _ = (self.session / recorder_state.STATE_FILENAME).write_text(json.dumps({
            "phase": records[-1], "open_phases": records[:-1]}))
        for phase, listed in files.items():
            directory = phase_reservation.phase_directory(self.session, phase)
            directory.mkdir(parents=True)
            _ = (directory / phase_reservation.FILES_FILE).write_text(listed)

    def test_an_ad_hoc_phase_open_beside_another_keeps_its_paths_from_the_review(self) -> None:
        self.open_phases({"ad hoc": "notes.md\n", "3": "reviewed.txt\n"})
        changed = ["notes.md", "reviewed.txt"]
        self.assertEqual(review_tree.other_phases_paths(self.session, "3", changed), {"adhoc": ["notes.md"]})
        self.assertEqual(review_tree.other_phases_paths(self.session, "ad hoc", changed), {"3": ["reviewed.txt"]})


if __name__ == "__main__":
    _ = unittest.main()
