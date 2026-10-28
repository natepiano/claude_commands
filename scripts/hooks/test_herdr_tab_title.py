"""The Stop hook titles a shared herdr pane's tab with its session's current name."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdr"))
import herdr  # noqa: E402
import herdr_tab_title  # noqa: E402
from herdr_tab_title import StopPayload  # noqa: E402


class HerdrTabTitleTests(unittest.TestCase):
    records: Path = Path()
    retitled: list[tuple[str, str]] = []
    fault: bool = False

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.records = root / "sessions"
        self.records.mkdir()
        (root / "config/herdr").mkdir(parents=True)
        _ = (root / "config/herdr/shared-session").write_text("agents\n")
        self.enterContext(patch.dict("os.environ", {
            "NOTIFIER_SESSIONS_DIR": str(self.records), "XDG_CONFIG_HOME": str(root / "config")}))
        self.record(41, "session-a", "showrunner-fixer")
        self.record(42, "session-b", "send-relay", entrypoint="sdk-cli")
        self.retitled, self.fault = [], False
        _ = self.enterContext(patch.object(herdr.Herdr, "retitle", autospec=True, side_effect=self.fake_retitle))

    def fake_retitle(self, _client: herdr.Herdr, pane: str, title: str) -> bool:
        self.retitled.append((pane, title))
        if self.fault:
            raise herdr.HerdrFailure("server gone")
        return True

    def record(self, pid: int, session_id: str, name: str, entrypoint: str = "cli") -> None:
        _ = (self.records / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": session_id, "name": name, "messagingSocketPath": f"/tmp/{pid}.sock",
            "updatedAt": 1, "entrypoint": entrypoint}))

    def test_a_shared_pane_takes_its_session_name(self) -> None:
        pane = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}
        self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"}, pane))
        self.assertEqual(self.retitled, [("w3:p7", "showrunner-fixer")])

    def test_a_pane_of_the_default_shared_session_takes_its_session_name_with_no_file(self) -> None:
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.records.parent / "unset")}):
            self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"},
                                                 {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": herdr.SHARED_SESSION}))
        self.assertEqual(self.retitled, [("w3:p7", "showrunner-fixer")])

    def test_other_places_and_unknown_sessions_change_nothing(self) -> None:
        cases: tuple[tuple[StopPayload, dict[str, str]], ...] = (
            ({"session_id": "session-a"}, {}),
            ({"session_id": "session-a"}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "own-session"}),
            ({"session_id": "session-a"}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "agents", "TMUX_PANE": "%1"}),
            ({"session_id": "session-b"}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "agents"}),
            ({"session_id": "session-z"}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "agents"}),
            ({}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "agents"}),
        )
        for payload, environment in cases:
            with self.subTest(payload=payload, environment=environment):
                self.assertFalse(herdr_tab_title.sync(payload, environment))
        self.assertEqual(self.retitled, [])

    def test_a_herdr_fault_never_stops_the_turn(self) -> None:
        self.fault = True
        with (patch.dict("os.environ", {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}),
              patch("sys.stdin", io.StringIO(json.dumps({"session_id": "session-a"})))):
            self.assertEqual(herdr_tab_title.main(), 0)
        self.assertEqual(self.retitled, [("w3:p7", "showrunner-fixer")])


if __name__ == "__main__":
    _ = unittest.main()
