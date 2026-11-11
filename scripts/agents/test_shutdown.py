"""Test producer stops and stray removal with private session files."""

from __future__ import annotations

import io
import json
import math
import os
import signal
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

from scripts.agents import pane_record, shutdown, test_stop_resume
from scripts.herdr import herdr
from scripts.message import sessions
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id


NATIVE_PROCESS = herdr.native_process


class ProducerCommandsTests(unittest.TestCase):
    def world(self) -> test_stop_resume.StopResumeTests:
        case = test_stop_resume.StopResumeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def register(self, case: test_stop_resume.StopResumeTests) -> str:
        identity = case.entry["session_id"]
        directory = case.root / "sessions"
        directory.mkdir(exist_ok=True)
        _ = (directory / "123.json").write_text(json.dumps({
            "pid": 123, "sessionId": identity, "name": "alpha",
            "messagingSocketPath": "", "updatedAt": 1, "procStart": "generation-1"}))
        case.live.return_value = sessions.LiveSessionProcess(123)

        def delivered(pid: int, number: int) -> None:
            if (pid, number) == (123, signal.SIGTERM):
                (directory / "123.json").unlink()
                case.live.return_value = sessions.SessionNotLive()
                case.process.return_value = sessions.SessionNotLive()

        case.kill.side_effect = delivered

        def probe(pid: int, number: int) -> None:
            if number:
                case.kill(pid, number)

        _ = case.enterContext(patch.object(os, "kill", side_effect=probe))
        original = case.client.result

        def process_info(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments == ("pane", "process-info", "--pane", "w1:p9"):
                return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 999,
                    "foreground_processes": [{"pid": 999, "name": "codex", "argv": ["codex"]}]}}
            if arguments == ("pane", "process-info", "--pane", "w1:p2") and (directory / "123.json").is_file():
                return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 123,
                    "foreground_processes": [{"pid": 123, "name": "claude", "argv": ["claude"]}]}}
            return original(*arguments, deadline=deadline)

        _ = case.enterContext(patch.object(case.client, "result", side_effect=process_info))
        return identity

    def test_stop_selects_the_producer_process_with_duplicate_or_unreported_panes(self) -> None:
        for selection in ("duplicate-label", "registry-only"):
            with self.subTest(selection=selection):
                case = self.world()
                identity = self.register(case)
                if selection == "duplicate-label":
                    _ = fixture_member("producer", name="alpha", runtime_id=identity)
                    case.client.rows.append({"pane_id": "w1:p8", "terminal_id": "other-terminal",
                        "tab_id": "other-tab", "workspace_id": "w1", "label": "alpha",
                        "agent_session": {"kind": "id", "value": fixture_runtime_id("stray")}})
                else:
                    _ = case.client.rows[0].pop("agent_session")
                    pane_record.save("agents", {})
                output = io.StringIO()
                with patch.object(herdr, "native_process", NATIVE_PROCESS), redirect_stdout(output):
                    status = shutdown.main(["stop", "alpha"])
                self.assertEqual(status, 0, output.getvalue())
                self.assertEqual(case.kill.call_args_list, [call(123, signal.SIGTERM)])
                self.assertIn(f"{identity}: stopped pid 123", output.getvalue())
                self.assertEqual(case.client.closed, [])
                case.doCleanups()

    def test_remove_stray_preserves_the_real_producer_and_completes_a_rerun(self) -> None:
        for target in ("stray", "current", "split", "pane-admission", "other-admission", "no-row", "unrelated-unproved",
                       "unretained-admission", "transcript-failure"):
            with self.subTest(target=target):
                case = self.world()
                identity = self.register(case)
                current = identity if target == "current" else fixture_runtime_id("real-alpha")
                expected = (() if target == "no-row" else
                            (fixture_member("producer", name="alpha", runtime_id=current),))
                if target == "split":
                    case.client.rows.append({"pane_id": "w1:p8", "terminal_id": "other-terminal",
                        "tab_id": "w1:t9", "workspace_id": "w1", "label": "other"})
                elif target == "unrelated-unproved":
                    case.client.rows.append({"pane_id": "w1:p9", "terminal_id": "unknown-terminal",
                        "tab_id": "unknown-tab", "workspace_id": "w1", "label": "unknown"})
                receipt = case.root / "panes/.producer-admission-alpha"
                if target in ("unretained-admission", "transcript-failure"):
                    pane_record.save("agents", {})
                saved = ({"pane": "w1:p2", "terminal": "w1:p2-terminal"}
                         if target in ("unretained-admission", "transcript-failure") else
                         {"pane": "w1:p2"} if target == "pane-admission" else
                         {"session_id": fixture_runtime_id("other-admission"), "pane": "w1:p8"}
                         if target == "other-admission" else {"session_id": identity, "pane": "w1:p2"})
                _ = receipt.write_text(json.dumps(saved))
                transcripts: list[Path] = []
                for account in (".claude3", ".claude4"):
                    folder = case.root / account / "projects/private"
                    folder.mkdir(parents=True)
                    path = folder / f"{identity}.jsonl"
                    _ = path.write_text("retained transcript\n")
                    transcripts.append(path)
                output = io.StringIO()
                with patch.object(herdr, "native_process", NATIVE_PROCESS), redirect_stdout(output):
                    if target == "transcript-failure":
                        unlink = Path.unlink

                        def fail_transcript(path: Path, missing_ok: bool = False) -> None:
                            if path == transcripts[-1]:
                                raise OSError("fixture transcript deletion failure")
                            unlink(path, missing_ok=missing_ok)

                        with patch.object(Path, "unlink", new=fail_transcript):
                            failed = shutdown.main(["remove-stray", "alpha", identity])
                        self.assertNotEqual(failed, 0)
                    status = shutdown.main(["remove-stray", "alpha", identity])
                    if target == "transcript-failure":
                        self.assertIn(f"{identity}: removed stray", output.getvalue())
                    if target != "current":
                        repeated = shutdown.main(["remove-stray", "alpha", identity])
                        self.assertEqual(repeated, 0, output.getvalue())
                if target == "current":
                    self.assertNotEqual(status, 0)
                    self.assertEqual(case.kill.call_args_list, [])
                    self.assertEqual(case.client.closed, [])
                    self.assertEqual([path.read_text() for path in transcripts], ["retained transcript\n"] * 2)
                    self.assertEqual(json.loads(receipt.read_text())["session_id"], identity)
                else:
                    self.assertEqual(status, 0, output.getvalue())
                    self.assertEqual(case.kill.call_args_list, [call(123, signal.SIGTERM)])
                    self.assertEqual(case.client.closed, ["w1:p2"])
                    self.assertEqual(sessions.transcript_locations(identity), [])
                    if target == "other-admission":
                        self.assertEqual(json.loads(receipt.read_text()), saved)
                    else:
                        self.assertEqual(list(receipt.parent.glob(receipt.name)), [])
                    self.assertIn(f"{identity}: unchanged", output.getvalue())
                    if target == "split":
                        self.assertEqual([pane["pane_id"] for pane in case.client.panes()], ["w1:p8"])
                    elif target == "unrelated-unproved":
                        self.assertEqual([pane["pane_id"] for pane in case.client.panes()], ["w1:p9"])
                self.assertEqual(staff.initial_snapshot().rows, expected)
                case.doCleanups()
