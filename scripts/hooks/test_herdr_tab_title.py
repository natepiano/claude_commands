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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdr"))
from scripts.lib import names
from scripts.lib.test_names import fixture_names
import herdr  # noqa: E402
import herdr_tab_title  # noqa: E402
from herdr_tab_title import StopPayload  # noqa: E402

REAL_RETITLE = herdr.Herdr.retitle


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
            "NOTIFIER_SESSIONS_DIR": str(self.records), "NOTIFIER_STATE_DIR": str(root / "notifier"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
            "SHOWRUNNER_UNIT": "", "SHOWRUNNER_UNIT_ID": ""}))
        _ = self.enterContext(patch.object(names, "runtime_names", return_value=fixture_names()))
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

    def test_turn_end_preserves_session_name_and_rename_updates_tab_and_pane(self) -> None:
        environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents",
                       "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "roles-unit"}
        pane: dict[str, object] = {"pane_id": "w3:p7", "tab_id": "w3:t2", "workspace_id": "w3",
                                   "label": "showrunner-fixer"}
        tab: dict[str, object] = {"tab_id": "w3:t2", "label": "showrunner-fixer"}

        def result(_client: herdr.Herdr, *arguments: str) -> dict[str, object]:
            return {"panes": [pane]} if arguments == ("pane", "list") else {"tabs": [tab]}

        def run(_client: herdr.Herdr, *arguments: str) -> str:
            target = pane if arguments[0] == "pane" else tab
            target["label"] = arguments[-1]
            return ""

        with (patch.object(herdr.Herdr, "retitle", new=REAL_RETITLE),
              patch.object(herdr.Herdr, "result", autospec=True, side_effect=result) as reads,
              patch.object(herdr.Herdr, "run", autospec=True, side_effect=run) as writes,
              patch.object(herdr.Herdr, "order_tabs", return_value={}) as ordered,
              patch.object(herdr.Herdr, "save_layout") as saved,
              patch.object(herdr, "saved_layout", return_value=herdr.SavedLayout(at=0.0, workspaces=[]))):
            self.assertFalse(herdr_tab_title.sync({"session_id": "session-a"}, environment))
            self.assertEqual(reads.call_count, 2)
            writes.assert_not_called()
            ordered.assert_not_called()
            saved.assert_not_called()
            self.record(41, "session-a", "renamed-director")
            self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"}, environment))
            self.assertEqual(pane["label"], "renamed-director")
            self.assertEqual(tab["label"], "renamed-director")
            self.assertEqual(writes.call_count, 2)
            ordered.assert_called_once()
            saved.assert_called_once()

    def test_showrunner_title_uses_session_name_after_session_rename(self) -> None:
        state = self.records.parent / "notifier"
        instance = state / "showrunner-sdlc"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            "TARGET=session:session-a\nCHECK=/scripts/production_check.sh /docs/sdlc-production.md\n")
        environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents", "NOTIFIER_STATE_DIR": str(state)}
        self.record(41, "session-a", "renamed-showrunner")
        self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"}, environment))
        self.assertEqual(self.retitled, [("w3:p7", "renamed-showrunner")])

    def test_showrunner_unit_director_and_plain_tabs_use_only_the_session_name(self) -> None:
        state = self.records.parent / "notifier/showrunner-sdlc"
        state.mkdir(parents=True)
        _ = (state / "conf").write_text(
            "TARGET=session:showrunner-id\nCHECK=/scripts/production_check.sh /docs/sdlc-production.md\n")
        cases: tuple[tuple[str, str, dict[str, str]], ...] = (
            ("showrunner-id", "control", {}),
            ("director-id", "roles", {"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "roles-unit"}),
            ("plain-id", "scratch", {}))
        for pid, (session_id, name, marks) in enumerate(cases, 50):
            with self.subTest(session=session_id):
                self.record(pid, session_id, name)
                environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents", **marks}
                self.assertTrue(herdr_tab_title.sync({"session_id": session_id}, environment))
                title = self.retitled[-1][1]
                self.assertEqual(title, name)
                for added in (" · ", "sdlc", "showrunner", "unit director"):
                    self.assertNotIn(added, title)


if __name__ == "__main__":
    _ = unittest.main()
