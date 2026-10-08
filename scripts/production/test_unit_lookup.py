"""Tests for finding a production's units by the marks on their tmux sessions."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

import fake_tmux
import unit_lookup
from fake_tmux import FakeSession
from unit_lookup import ClaudeNotRunning, ClaudeUnknown, LiveClaude, UnitState

SCRIPT = Path(__file__).with_name("unit_lookup.py")
FAKE = str(Path(__file__).with_name("fake_tmux.py"))


def marked(label: str, pane: str, unit: str, slug: str = "show", **extra: str) -> FakeSession:
    return FakeSession(label=label, panes=[pane], env={"SHOWRUNNER_UNIT": slug, "SHOWRUNNER_UNIT_ID": unit, **extra})


class UnitLookupTests(unittest.TestCase):
    root: Path = Path()
    state: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "tmux.json"
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.environment = {**os.environ, "FAKE_TMUX_STATE": str(self.state), "UNIT_LOOKUP_TMUX": FAKE,
                            "NOTIFIER_SESSIONS_DIR": str(self.records)}
        self.enterContext(mock.patch.dict(os.environ, self.environment))

    def claude(self, name: str, pane: str, *, updated: int = 1, session_id: str = "") -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        path = self.root / f"{name}-{updated}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        _ = (self.records / f"{name}-{updated}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": session_id or f"id-{name}", "name": name,
            "messagingSocketPath": str(path), "updatedAt": updated, "tmux": f"label-at-start:@1.{pane}"}))

    def run_script(self, *arguments: str, pane: str = "") -> subprocess.CompletedProcess[str]:
        environment = {**self.environment, "TMUX_PANE": pane}
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=environment, capture_output=True,
                              text=True, check=False)

    def test_a_unit_is_found_by_its_mark_whatever_its_session_is_called_now(self) -> None:
        fake_tmux.write(self.state, {"$1": marked("renamed-since-launch", "%4", "trunk-unit"),
                                     "$2": marked("another-show", "%5", "trunk-unit", slug="other"),
                                     "$3": FakeSession(label="unmarked", panes=["%6"], env={})})
        self.claude("renamed-since-launch", "%4")
        found = unit_lookup.marked_units("show")
        self.assertEqual(list(found), ["trunk-unit"])
        unit = found["trunk-unit"]
        self.assertEqual((unit.pane, unit.label, unit.state), ("%4", "renamed-since-launch", UnitState.RUNNING))
        assert isinstance(unit.claude, LiveClaude)
        self.assertEqual((unit.claude.name, unit.claude.session_id), ("renamed-since-launch", "id-renamed-since-launch"))

    def test_a_relaunch_in_the_same_pane_is_found_from_its_newest_record(self) -> None:
        fake_tmux.write(self.state, {"$1": marked("trunk", "%4", "trunk-unit")})
        self.claude("before", "%4", updated=1, session_id="old-id")
        self.claude("after", "%4", updated=2, session_id="new-id")
        claude = unit_lookup.marked_units("show")["trunk-unit"].claude
        assert isinstance(claude, LiveClaude)
        self.assertEqual((claude.name, claude.session_id), ("after", "new-id"))

    def test_a_marked_session_with_no_claude_is_still_the_units_pane(self) -> None:
        fake_tmux.write(self.state, {"$1": marked("trunk", "%4", "trunk-unit", SHOWRUNNER_UNIT_STATE="run-finished")})
        unit = unit_lookup.marked_units("show")["trunk-unit"]
        self.assertEqual((unit.pane, unit.state), ("%4", UnitState.RUN_FINISHED))
        self.assertIsInstance(unit.claude, ClaudeNotRunning)

    def test_an_unreadable_record_leaves_a_unit_without_a_match_unknown_not_stopped(self) -> None:
        fake_tmux.write(self.state, {"$1": marked("trunk", "%4", "trunk-unit"), "$2": marked("fps", "%5", "fps-unit")})
        self.claude("fps", "%5")
        _ = (self.records / "broken.json").write_text("{")
        found = unit_lookup.marked_units("show")
        self.assertIsInstance(found["trunk-unit"].claude, ClaudeUnknown)
        self.assertIsInstance(found["fps-unit"].claude, LiveClaude)

    def test_no_tmux_server_means_no_units_and_a_broken_tmux_is_an_error(self) -> None:
        self.assertEqual(unit_lookup.marked_units("show"), {})
        with mock.patch.dict(os.environ, {"UNIT_LOOKUP_TMUX": str(self.root / "absent")}), \
                self.assertRaises(OSError):
            _ = unit_lookup.marked_units("show")

    def test_two_sessions_carrying_one_units_mark_are_an_error(self) -> None:
        fake_tmux.write(self.state, {"$1": marked("trunk", "%4", "trunk-unit"), "$2": marked("copy", "%5", "trunk-unit")})
        with self.assertRaisesRegex(OSError, "both carry the mark of trunk-unit"):
            _ = unit_lookup.marked_units("show")

    def test_the_command_prints_the_pane_and_tells_absent_from_cannot_tell(self) -> None:
        fake_tmux.write(self.state, {"$1": marked("trunk", "%4", "trunk-unit")})
        self.claude("trunk", "%4")
        found = self.run_script("pane", "show", "trunk-unit")
        self.assertEqual((found.returncode, found.stdout), (0, "%4\n"))
        self.assertEqual(self.run_script("pane", "show", "fps-unit").returncode, 1)
        listed = self.run_script("list", "show")
        self.assertEqual(listed.stdout.split("\t")[:5], ["trunk-unit", "running", "%4", "trunk", "live"])
        self.state.unlink()
        _ = self.state.write_text("not json")
        self.assertEqual(self.run_script("pane", "show", "trunk-unit").returncode, 3)
        self.assertEqual(self.run_script("list").returncode, 2)

    def test_a_session_is_marked_once_and_its_state_follows(self) -> None:
        fake_tmux.write(self.state, {"$1": FakeSession(label="old-name", panes=["%4"], env={})})
        with self.assertRaisesRegex(OSError, "not a marked unit session"):
            unit_lookup.set_state("%4", UnitState.RUN_FINISHED)
        self.assertEqual(self.run_script("mark", "show", "trunk-unit", "old-name").returncode, 0)
        self.assertEqual(self.run_script("mark", "show", "trunk-unit", "old-name").returncode, 0)
        self.assertEqual(self.run_script("mark", "show", "trunk-unit", "absent").returncode, 3)
        unit_lookup.set_state("%4", UnitState.RUN_FINISHED)
        self.assertEqual(unit_lookup.marked_units("show")["trunk-unit"].state, UnitState.RUN_FINISHED)

    def test_the_showrunner_sets_a_units_state_by_unit_id_from_outside_its_pane(self) -> None:
        fake_tmux.write(self.state, {"$1": marked("any-label", "%4", "trunk-unit")})
        self.assertEqual(self.run_script("state", "show", "trunk-unit", "run-finished").returncode, 0)
        self.assertEqual(unit_lookup.marked_units("show")["trunk-unit"].state, UnitState.RUN_FINISHED)
        self.assertEqual(self.run_script("state", "show", "trunk-unit", "running").returncode, 0)
        self.assertEqual(unit_lookup.marked_units("show")["trunk-unit"].state, UnitState.RUNNING)
        self.assertEqual(self.run_script("state", "show", "fps-unit", "run-finished").returncode, 1)
        self.assertEqual(self.run_script("state", "show", "trunk-unit", "asleep").returncode, 2)


if __name__ == "__main__":
    _ = unittest.main()
