"""/showrunner:move waits for each move to end and proves the resumed process is on the target."""
from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import showrunner_move
from showrunner_move import MovesItself, NotMoved, Proven, Started
from scripts.accounts import accounts
from scripts.accounts.moves import MoveEntry
from scripts.shutdown.record import SessionShutdown

UNIT = "aaaaaaaa-0000-0000-0000-000000000001"
TARGET = Path("/home/someone/.claude1")
SINCE = "2026-10-14T10:00:00+00:00"


def confirmed(session_id: str, at: str = "2026-10-14T10:05:00+00:00") -> MoveEntry:
    return cast(MoveEntry, cast(object, {"kind": "confirmed", "at": at, "tool": "claude",
                                         "subject": f"{session_id}: nightly-review", "move_id": "r1"}))


def stopping(session_id: str, kind: str, reason: str = "") -> SessionShutdown:
    progress = {"kind": kind, "at": SINCE, **({"reason": reason} if reason else {})}
    return cast(SessionShutdown, cast(object, {"session": {"session_id": session_id}, "progress": progress}))


def live(session_id: str = UNIT, pid: int = 4242) -> list[dict[str, object]]:
    return [{"sessionId": session_id, "name": "nightly-review", "pid": pid}]


class SettleTest(unittest.TestCase):
    def settle(self, records: list[list[SessionShutdown]], journal: list[MoveEntry], config_dir: Path | None = TARGET,
               *, deadline: float = 100.0) -> showrunner_move.Outcome:
        reads = iter(records)
        clock = iter(float(tick) for tick in range(0, 1000, 30))
        with patch("showrunner_move.record.all_session_records", side_effect=lambda: next(reads)), \
             patch("showrunner_move.moves.read_moves", return_value=journal), \
             patch("showrunner_move.sessions.live_sessions", return_value=live()), \
             patch("showrunner_move.account.process_config_dir", return_value=config_dir), \
             patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "the-showrunner"}):
            return showrunner_move.settle(Started(UNIT, "nightly-review"), TARGET, SINCE, deadline,
                                          clock=lambda: next(clock), sleep=lambda _: None)

    def test_a_confirmed_move_is_proven_from_the_resumed_process(self) -> None:
        outcome = self.settle([[stopping(UNIT, "waiting")], []], [confirmed(UNIT)])
        self.assertEqual(outcome, Proven(4242, TARGET))

    def test_a_process_on_another_account_is_not_proven(self) -> None:
        outcome = self.settle([[]], [confirmed(UNIT)], Path("/home/someone/.claude3"))
        self.assertIsInstance(outcome, NotMoved)
        self.assertIn(".claude3", cast(NotMoved, outcome).reason)

    def test_a_failed_restart_names_its_reason(self) -> None:
        outcome = self.settle([[stopping(UNIT, "restart failed", "still on claude 3")]], [])
        self.assertEqual(outcome, NotMoved("still on claude 3"))

    def test_a_stop_without_a_confirmed_move_is_not_proven(self) -> None:
        outcome = self.settle([[]], [confirmed(UNIT, at="2026-10-14T09:00:00+00:00")])
        self.assertEqual(outcome, NotMoved("its stop ended without a confirmed move"))

    def test_a_move_still_running_at_the_deadline_says_where_it_is(self) -> None:
        outcome = self.settle([[stopping(UNIT, "waiting")]] * 10, [], deadline=60.0)
        self.assertEqual(outcome, NotMoved("still waiting when this check stopped waiting"))

    def test_the_calling_session_is_not_waited_for(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": UNIT}):
            self.assertEqual(showrunner_move.settle(Started(UNIT, "me"), TARGET, SINCE, 0.0), MovesItself())


class UnitsTest(unittest.TestCase):
    def test_units_are_this_showrunners_live_units_on_the_source(self) -> None:
        def claude(session_id: str, pid: int) -> showrunner_move.unit_lookup.MarkedUnit:
            return showrunner_move.unit_lookup.MarkedUnit(session_id, "label", showrunner_move.unit_lookup.LiveClaude(
                session_id, session_id, "/sock", pid), showrunner_move.unit_lookup.TmuxPlace("pane"))
        marked = {"one": claude("on-three", 1), "two": claude("on-one", 2),
                  "three": showrunner_move.unit_lookup.MarkedUnit("three", "label", showrunner_move.unit_lookup.ClaudeNotRunning(),
                                                                  showrunner_move.unit_lookup.TmuxPlace("pane"))}
        folders = {1: Path("/home/someone/.claude3"), 2: TARGET}
        with patch("showrunner_move.showrunners.own_slug", return_value="build-followups"), \
             patch("showrunner_move.unit_lookup.marked_units", return_value=marked) as units, \
             patch("showrunner_move.accounts.account_dir", return_value=Path("/home/someone/.claude3")), \
             patch("showrunner_move.account.process_config_dir", side_effect=folders.get):
            self.assertEqual(showrunner_move.own_units("claude 3"), ["on-three"])
            self.assertEqual(showrunner_move.own_units(""), ["on-three", "on-one"])
        units.assert_called_with("build-followups")

    def test_a_session_running_no_production_has_no_units(self) -> None:
        with patch("showrunner_move.showrunners.own_slug", return_value=""):
            self.assertRaises(accounts.AccountsError, showrunner_move.own_units, "")


class MainTest(unittest.TestCase):
    def run_main(self, argv: list[str], moved: object, outcome: showrunner_move.Outcome) -> tuple[int, str]:
        output = io.StringIO()
        with patch("showrunner_move.accounts.move", return_value=moved) as move, \
             patch("showrunner_move.accounts.account_dir", return_value=TARGET), \
             patch("showrunner_move.sessions.live_sessions", return_value=live()), \
             patch.object(showrunner_move, "settle", return_value=outcome), redirect_stdout(output):
            code = showrunner_move.main(argv)
        if moved is not None:
            move.assert_called_once_with("nightly-review", "claude 1", why="claude 3 is out")
        return code, output.getvalue()

    def test_a_proven_move_prints_one_line_and_exits_zero(self) -> None:
        code, text = self.run_main(["session", "nightly-review", "claude 1", "--why", "claude 3 is out"],
                                   accounts.MoveStarted(UNIT, "claude 1", "", "", 0), Proven(4242, TARGET))
        self.assertEqual(code, 0)
        self.assertEqual(text, f"nightly-review: on claude 1, proven (pid 4242 runs on {TARGET})\n")

    def test_a_refused_move_exits_one_with_its_reason(self) -> None:
        code, text = self.run_main(["session", "nightly-review", "claude 1", "--why", "claude 3 is out"],
                                   accounts.MoveRefused("nightly-review is already on claude 1"), Proven(4242, TARGET))
        self.assertEqual(code, 1)
        self.assertEqual(text, "nightly-review: not moved: nightly-review is already on claude 1\n")

    def test_an_unproven_move_exits_one(self) -> None:
        code, text = self.run_main(["session", "nightly-review", "claude 1", "--why", "claude 3 is out"],
                                   accounts.MoveStarted(UNIT, "claude 1", "", "", 0), NotMoved("no live process after the move"))
        self.assertEqual(code, 1)
        self.assertEqual(text, "nightly-review: not on claude 1: no live process after the move\n")


if __name__ == "__main__":
    _ = unittest.main()
