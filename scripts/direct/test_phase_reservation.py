#!/usr/bin/env python3
"""Tests for the phase reservation state verbs: enroll, committed, clear, and restore on reopen."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import phase_reservation  # noqa: E402

ACTIVE: dict[str, object] = {"kind": "active", "active_phase_reservation": {
    "reservation_id": "r1", "coordination_run_id": "c1", "phase": "34h", "phase_start_head": "a" * 40}}


@final
class PhaseStateTests(unittest.TestCase):
    session: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.session = Path(folder.name)

    def phase_dir(self) -> Path:
        return phase_reservation.phase_directory(self.session, "34h")

    def state(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.phase_dir() / phase_reservation.STATE_FILE).read_text()))

    def run_verb(self, *arguments: str) -> tuple[int, str]:
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code = phase_reservation.main([*arguments, "--session-dir", str(self.session), "--phase", "34h"])
        return code, errors.getvalue()

    def test_enroll_writes_the_first_state_from_each_board_answer(self) -> None:
        self.assertEqual(self.run_verb("enroll", "--board", "board_ready"), (0, ""))
        self.assertEqual(self.state(), {"kind": "enrolled_awaiting_first_touch", "phase": "34h"})
        self.assertEqual(self.run_verb("enroll", "--board", "unconfigured"), (0, ""))
        self.assertEqual(self.state(), {"kind": "repository_not_enrolled", "phase": "34h"})

    def test_a_held_reservation_is_never_replaced_or_cleared_by_hand(self) -> None:
        phase_reservation._write_state(self.phase_dir(), ACTIVE)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.run_verb("enroll", "--board", "board_ready")[0], 2)
        self.assertEqual(self.run_verb("clear")[0], 2)
        self.assertEqual(self.state(), ACTIVE)

    def test_committed_then_clear_needs_the_released_checkpoint(self) -> None:
        phase_reservation._write_state(self.phase_dir(), ACTIVE)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.run_verb("committed", "--commit", "b" * 40), (0, ""))
        pending = cast(dict[str, object], self.state()["checkpoint_release_confirmation_pending"])
        self.assertEqual(pending["checkpoint_commit"], "b" * 40)
        self.assertEqual(pending["reservation_id"], "r1")
        self.assertEqual(self.run_verb("clear", "--released", "c" * 40)[0], 2)
        self.assertEqual(self.run_verb("clear", "--released", "b" * 40), (0, ""))
        self.assertFalse((self.phase_dir() / phase_reservation.STATE_FILE).exists())

    def test_a_finished_phase_reopens_with_its_kept_board_answer(self) -> None:
        _ = self.run_verb("enroll", "--board", "board_ready")
        self.assertEqual(self.run_verb("clear"), (0, ""))
        self.assertEqual(phase_reservation.open_phases(self.session), [])
        self.assertEqual(phase_reservation.restore(self.phase_dir(), "34h"), "enrolled_awaiting_first_touch")
        self.assertEqual([phase.phase for phase in phase_reservation.open_phases(self.session)], ["34h"])
        # An open phase is left as it is, and a phase never enrolled has nothing to restore.
        self.assertEqual(phase_reservation.restore(self.phase_dir(), "34h"), "")
        self.assertEqual(phase_reservation.restore(phase_reservation.phase_directory(self.session, "9"), "9"), "")


if __name__ == "__main__":
    _ = unittest.main()
