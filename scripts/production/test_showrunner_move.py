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
from scripts.whoami import account
from scripts.accounts.moves import MoveEntry
from scripts.shutdown.shutdown_record import SessionShutdown

UNIT = "aaaaaaaa-0000-0000-0000-000000000001"
TARGET = Path("/home/someone/.claude1")
SINCE = "2026-10-14T10:00:00+00:00"
PROVEN = Proven(4242, TARGET)


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
        with patch("showrunner_move.shutdown_record.all_session_records", side_effect=lambda: next(reads)), \
             patch("showrunner_move.moves.read_moves", return_value=journal), \
             patch("showrunner_move.sessions.live_sessions", return_value=live()), \
             patch("showrunner_move.account.process_config_dir", return_value=account.ConfiguredClaudeFolder(config_dir) if config_dir is not None else account.ProcessEnvironmentUnread("unreadable")), \
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
        outcome = self.settle([[stopping(UNIT, "restart failed", "still on claude3")]], [])
        self.assertEqual(outcome, NotMoved("still on claude3"))

    def test_a_stop_without_a_confirmed_move_is_not_proven(self) -> None:
        outcome = self.settle([[]], [confirmed(UNIT, at="2026-10-14T09:00:00+00:00")])
        self.assertEqual(outcome, NotMoved("its stop ended without a confirmed move"))

    def test_a_move_still_running_at_the_deadline_says_where_it_is(self) -> None:
        outcome = self.settle([[stopping(UNIT, "waiting")]] * 10, [], deadline=60.0)
        self.assertEqual(outcome, NotMoved("still waiting when this check stopped waiting"))

    def test_the_calling_session_is_not_waited_for(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": UNIT}):
            self.assertEqual(showrunner_move.settle(Started(UNIT, "me"), TARGET, SINCE, 0.0), MovesItself())


class GroupTest(unittest.TestCase):
    def run_group(self, argv: list[str], moved: object, outcome: showrunner_move.Outcome = PROVEN) -> tuple[int, str]:
        output = io.StringIO()
        with patch("showrunner_move.accounts.move_group", return_value=moved) as move_group, \
             patch("showrunner_move.accounts.claude_account_dir", return_value=TARGET), \
             patch("showrunner_move.sessions.live_sessions", return_value=live()), \
             patch.object(showrunner_move, "settle", return_value=outcome) as settle, redirect_stdout(output):
            code = showrunner_move.main(argv)
        move_group.assert_called_once_with(argv[0], "claude1", dry_run="--dry-run" in argv, why="")
        if not isinstance(moved, accounts.GroupMoveStarted | accounts.GroupMoveStartFailed):
            settle.assert_not_called()
        return code, output.getvalue()

    def test_each_moved_member_is_proven(self) -> None:
        code, text = self.run_group(["units", "claude1"], accounts.GroupMoveStarted((UNIT,), "claude1", "", "", 0))
        self.assertEqual((code, text), (0, f"nightly-review: on claude1, proven (pid 4242 runs on {TARGET})\n"))

    def test_group_success_keeps_usage_notice_and_move_back_guidance(self) -> None:
        notice = "no fresh usage reading for claude1 (last 25 minutes ago)"
        back = ("to move them back, in this terminal: ~/.claude/scripts/lib/py "
                + "~/.claude/scripts/accounts/accounts.py move units claude3")
        code, text = self.run_group(["units", "claude1"], accounts.GroupMoveStarted(
            (UNIT,), "claude1", "shutdown started\n" + notice + "\n" + back + "\n", "", 0))
        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines(), [notice, back,
            f"nightly-review: on claude1, proven (pid 4242 runs on {TARGET})"])

    def test_a_group_already_on_its_account_is_a_note_and_exits_zero(self) -> None:
        code, text = self.run_group(["units", "claude1"],
                                    accounts.GroupMoveStarted((), "claude1", "units: no live session moves\n", "", 0))
        self.assertEqual((code, text), (0, "units: no live session moves\n"))

    def test_an_unregistered_showrunner_left_where_it_is_exits_one(self) -> None:
        skipped = ("other: no session runs under the id its update timer names; a showrunner running under another id "
                   + "is not moved until it runs `update_registration.py register --production /p.md`")
        code, text = self.run_group(["showrunners", "claude1"],
                                    accounts.GroupMoveStarted((UNIT,), "claude1", skipped + "\n", "", 0))
        self.assertEqual(code, 1)
        self.assertEqual(text.splitlines(), [skipped, f"nightly-review: on claude1, proven (pid 4242 runs on {TARGET})"])

    def test_a_refused_member_is_named_with_the_shutdown_reason(self) -> None:
        code, text = self.run_group(["units", "claude1"],
                                    accounts.GroupMoveStartFailed((UNIT,), "claude1", "", "a1b2: already has a shutdown in progress\n", 1))
        self.assertEqual(code, 1)
        self.assertEqual(text.splitlines()[0], "units: a1b2: already has a shutdown in progress")

    def test_a_dry_run_prints_the_plan_and_moves_nothing(self) -> None:
        code, text = self.run_group(["showrunners", "claude1", "--dry-run"],
                                    accounts.GroupMoveDryRun((UNIT,), ("showrunners: would select claude1", f"nightly-review: {UNIT}")))
        self.assertEqual((code, text), (0, f"showrunners: would select claude1\nnightly-review: {UNIT}\n"))

    def test_a_refused_group_exits_one_with_its_reason(self) -> None:
        code, text = self.run_group(["units", "claude1"], accounts.MoveRefused("claude1 has no room"))
        self.assertEqual((code, text), (1, "units: not moved: claude1 has no room\n"))


class MainTest(unittest.TestCase):
    def test_help_teaches_only_numbered_claude_targets(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            _ = showrunner_move.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("<claudeN>", output.getvalue())
        self.assertNotIn("default", output.getvalue())

    def run_main(self, argv: list[str], moved: object, outcome: showrunner_move.Outcome) -> tuple[int, str]:
        output = io.StringIO()
        with patch("showrunner_move.accounts.move", return_value=moved) as move, \
             patch("showrunner_move.accounts.claude_account_dir", return_value=TARGET), \
             patch("showrunner_move.sessions.live_sessions", return_value=live()), \
             patch.object(showrunner_move, "settle", return_value=outcome), redirect_stdout(output):
            code = showrunner_move.main(argv)
        if moved is not None:
            move.assert_called_once_with("nightly-review", "claude1", why="claude3 is out")
        return code, output.getvalue()

    def test_a_proven_move_prints_one_line_and_exits_zero(self) -> None:
        code, text = self.run_main(["session", "nightly-review", "claude1", "--why", "claude3 is out"],
                                   accounts.MoveStarted(UNIT, "claude1", "", "", 0), Proven(4242, TARGET))
        self.assertEqual(code, 0)
        self.assertEqual(text, f"nightly-review: on claude1, proven (pid 4242 runs on {TARGET})\n")

    def test_single_success_keeps_usage_notice_and_move_back_guidance(self) -> None:
        notice = "no fresh usage reading for claude1 (last reading unknown)"
        back = ("to move them back, in this terminal: ~/.claude/scripts/lib/py "
                + "~/.claude/scripts/accounts/accounts.py move " + UNIT + " claude3")
        code, text = self.run_main(["session", "nightly-review", "claude1", "--why", "claude3 is out"],
            accounts.MoveStarted(UNIT, "claude1", notice + "\nshutdown started\n" + back + "\n", "", 0), PROVEN)
        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines(), [notice, back,
            f"nightly-review: on claude1, proven (pid 4242 runs on {TARGET})"])

    def test_a_refused_move_exits_one_with_its_reason(self) -> None:
        code, text = self.run_main(["session", "nightly-review", "claude1", "--why", "claude3 is out"],
                                   accounts.MoveRefused("nightly-review is already on claude1"), Proven(4242, TARGET))
        self.assertEqual(code, 1)
        self.assertEqual(text, "nightly-review: not moved: nightly-review is already on claude1\n")

    def test_legacy_target_prints_the_canonical_label_for_each_move_outcome(self) -> None:
        target = Path.home() / ".claude2"
        for outcome, expected in (
            (Proven(4242, target), f"nightly-review: on claude2, proven (pid 4242 runs on {target})\n"),
            (NotMoved("restart failed"), "nightly-review: not on claude2: restart failed\n"),
            (MovesItself(), "nightly-review: this session moves too; when it is back, the restart proves its account "
             + "and tells the user if it is not on claude2\n"),
        ):
            with (self.subTest(outcome=outcome),
                  patch.object(accounts, "move", return_value=accounts.MoveStarted(UNIT, "claude2", "", "", 0)),
                  patch.object(accounts, "claude_account_dir", return_value=target),
                  patch("showrunner_move.sessions.live_sessions", return_value=live()),
                  patch.object(showrunner_move, "settle", return_value=outcome)):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = showrunner_move.main(["session", "nightly-review", "claude 2"])
                self.assertEqual(code, 1 if isinstance(outcome, NotMoved) else 0)
                self.assertEqual(output.getvalue(), expected)

    def test_an_unproven_move_exits_one(self) -> None:
        code, text = self.run_main(["session", "nightly-review", "claude1", "--why", "claude3 is out"],
                                   accounts.MoveStarted(UNIT, "claude1", "", "", 0), NotMoved("no live process after the move"))
        self.assertEqual(code, 1)
        self.assertEqual(text, "nightly-review: not on claude1: no live process after the move\n")


if __name__ == "__main__":
    _ = unittest.main()
