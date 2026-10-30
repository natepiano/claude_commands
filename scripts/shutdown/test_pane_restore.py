from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Mapping
from pathlib import Path
from typing import cast, final, override
from unittest import mock

import pane_restore  # first: it puts the herdr scripts on the path
import herdr  # noqa: E402
import pane_record  # noqa: E402
import sessions  # noqa: E402

END_HOOK = Path(__file__).resolve().parent.parent / "hooks/session-end-pane-record.py"
SETTINGS = '{"disableAgentView": true}'


@final
class FakeClient(herdr.Herdr):
    """The herdr calls a restore makes, on a layout the test sets."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        with mock.patch.object(herdr, "herdr_binary", return_value="fixture-herdr"):
            super().__init__("agents")
        self.rows = rows
        self.started: list[tuple[str, str, list[str]]] = []
        self.closed: list[str] = []
        self.start_error: herdr.HerdrFailure | None = None

    @override
    def result(self, *arguments: str) -> dict[str, object]:
        if arguments[:2] == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "w1", "label": "demo"}]}
        if arguments[:2] == ("pane", "list"):
            return {"panes": self.rows}
        if arguments[:2] == ("pane", "process-info"):
            return {"process_info": {"foreground_processes": [
                {"argv": ["claude", "-n", "alpha", "--model", "opus"], "pid": 4242, "cwd": "/work/alpha"}]}}
        raise AssertionError(arguments)

    @override
    def pane_activity(self, pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
        _ = pane
        return herdr.PaneAtShell()

    @override
    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
        _ = (kind, opening, timeout_ms)
        if self.start_error is not None:
            raise self.start_error
        self.started.append((name, pane, agent_arguments))
        return herdr.PromptSubmissionNotRequired()

    @override
    def close_tab_of(self, pane: str) -> None:
        self.closed.append(pane)


@final
class PaneRestoreTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.config = Path()
        self.opened: list[tuple[str, str, str, str, dict[str, str]]] = []
        self.owned: list[str] = []
        self.live: list[str] = []

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / ".claude3"
        (self.config / "projects/work").mkdir(parents=True)
        _ = (self.config / "projects/work/sid-alpha.jsonl").write_text("{}\n")
        def no_envrc(directory: Path, environment: Mapping[str, str]) -> herdr.nix_env.NoEnvrc:
            _ = directory
            return herdr.nix_env.NoEnvrc(dict(environment))

        for patcher in (mock.patch.object(herdr.nix_env, "ensure", side_effect=no_envrc),
                        mock.patch.object(pane_record, "RECORD_DIR", self.root / "herdr-panes"),
                        mock.patch.object(pane_record, "PROC", self.root / "proc"),
                        mock.patch.dict(os.environ, {"HOME": str(self.root)}),
                        mock.patch.object(pane_restore, "all_session_records", self.records),
                        mock.patch.object(sessions, "live_sessions", self.live_sessions),
                        mock.patch.object(sessions, "running_sessions", list),
                        mock.patch.object(herdr, "open_tab", self.open_tab)):
            _ = patcher.start()
            self.addCleanup(patcher.stop)

    def live_sessions(self) -> list[dict[str, str]]:
        return [{"sessionId": session_id} for session_id in self.live]

    def records(self) -> list[dict[str, dict[str, str]]]:
        return [{"session": {"session_id": session_id}} for session_id in self.owned]

    def open_tab(self, name: str, space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
        self.opened.append((name, space, title, cwd, environment))
        return "w1:p3"

    def entry(self, **changes: object) -> None:
        launch = pane_record.launch("w1:p2", "sid-alpha", "/work/alpha",
                                    {"CLAUDE_CONFIG_DIR": str(self.config), "SHOWRUNNER_UNIT": "demo",
                                     "CLAUDE_CODE_OAUTH_TOKEN": "secret"},
                                    ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "opus"])
        pane_record.write("agents", cast(pane_record.PaneLaunch, cast(object, {**launch, **changes})))

    def run_restore(self, client: FakeClient) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(herdr, "Herdr", return_value=client), redirect_stdout(output), redirect_stderr(output):
            code = pane_restore.restore("agents", dry_run=False)
        return code, output.getvalue()

    @staticmethod
    def restored(session_id: str = "sid-alpha") -> FakeClient:
        return FakeClient([{"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1",
                            "agent_session": {"kind": "id", "value": session_id}}])

    def test_a_restored_pane_resumes_on_its_own_account_and_its_restored_tab_closes(self) -> None:
        self.entry()
        client = self.restored()
        code, output = self.run_restore(client)
        self.assertEqual(code, 0, output)
        (name, space, title, cwd, environment), = self.opened
        self.assertEqual((name, space, title, cwd), ("agents", "demo", "alpha", "/work/alpha"))
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.config))
        self.assertEqual(environment["SHOWRUNNER_UNIT"], "demo")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
        self.assertEqual(client.started, [("alpha", "w1:p3", [
            "--resume", "sid-alpha", "-n", "alpha", "--remote-control", "alpha", "--model", "opus",
            "--settings", SETTINGS])])
        self.assertEqual(client.closed, ["w1:p2"])

    def test_a_pane_is_left_as_it_is_for_each_reason_and_named(self) -> None:
        cases: list[tuple[dict[str, object], str, str]] = [
            ({"ended": True}, "sid-alpha", "closed by hand"),
            ({"flags_read": False}, "sid-alpha", "launch flags are unknown"),
            ({"session_id": "sid-gone"}, "sid-gone", "no transcript of sid-gone"),
            ({}, "sid-other", "herdr has sid-other, the entry sid-alpha"),
        ]
        for changes, herdr_id, reason in cases:
            with self.subTest(reason=reason):
                self.opened.clear()
                self.entry(**changes)
                code, output = self.run_restore(self.restored(herdr_id))
                self.assertEqual((code, self.opened), (0, []))
                self.assertIn(reason, output)
        self.entry()
        self.owned.append("sid-alpha")
        _, output = self.run_restore(self.restored())
        self.assertIn("a shutdown record owns it", output)
        self.owned.clear()
        self.live.append("sid-alpha")
        _, output = self.run_restore(self.restored())
        self.assertIn("it already runs", output)
        self.assertEqual(self.opened, [])

    def test_a_failed_start_closes_the_new_tab_keeps_the_restored_one_and_exits_1(self) -> None:
        self.entry()
        client = self.restored()
        client.start_error = herdr.HerdrFailure("agent not ready")
        code, output = self.run_restore(client)
        self.assertEqual(code, 1)
        self.assertIn("not resumed: agent not ready", output)
        self.assertEqual(client.closed, ["w1:p3"])

    def test_seed_writes_the_entry_of_each_running_claude(self) -> None:
        (self.root / "proc/4242").mkdir(parents=True)
        _ = (self.root / "proc/4242/environ").write_bytes(
            f"CLAUDE_CONFIG_DIR={self.config}\0SHOWRUNNER_UNIT=demo\0CLAUDE_CODE_OAUTH_TOKEN=secret\0".encode())
        (self.config / "sessions").mkdir()
        _ = (self.config / "sessions/4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "sid-alpha"}))
        client = FakeClient([{"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1"}])
        with mock.patch.object(herdr, "Herdr", return_value=client), redirect_stdout(io.StringIO()):
            self.assertEqual(pane_record.seed("agents"), 0)
        entry = pane_record.read("agents")["w1:p2"]
        self.assertEqual((entry["session_id"], entry["config_dir"], entry["name"], entry["model"], entry["cwd"]),
                         ("sid-alpha", str(self.config), "alpha", "opus", "/work/alpha"))
        self.assertEqual(entry["environment"], {"SHOWRUNNER_UNIT": "demo"})

    def test_the_end_hook_marks_only_an_end_by_hand_of_the_same_conversation(self) -> None:
        self.entry()
        environment = {"PATH": "", "HOME": str(self.root), "HERDR_ENV": "1", "HERDR_SESSION": "agents",
                       "HERDR_PANE_ID": "w1:p2", "HERDR_PANE_RECORD_DIR": str(self.root / "herdr-panes")}
        for session_id, reason, ended in (("sid-alpha", "other", False), ("sid-old", "prompt_input_exit", False),
                                          ("sid-alpha", "prompt_input_exit", True)):
            ran = subprocess.run([sys.executable, str(END_HOOK)], input=json.dumps(
                {"session_id": session_id, "reason": reason}), env=environment, capture_output=True, text=True,
                check=False, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertEqual(pane_record.read("agents")["w1:p2"]["ended"], ended, (session_id, reason))


if __name__ == "__main__":
    _ = unittest.main()
