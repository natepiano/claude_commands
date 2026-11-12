"""Test producer stops and stray removal with private session files."""

from __future__ import annotations

import io
import json
import math
import os
import signal
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import call, patch

from scripts.agents import pane_record, shutdown, test_stop_resume
from scripts.herdr import herdr, pane_launch
from scripts.lib import atomic_file
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

        from scripts.agents.test_shutdown_cli import DIRECTOR, RetireUnitTests
        from scripts.copies import copies
        from scripts.lib import conf

        case = RetireUnitTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        member = replace(unit.staff_member, id=staff.StaffId(fixture_runtime_id("fixed-retiring-holder")))
        self.assertNotEqual(member.id, member.current_session)
        unit = replace(unit, staff_member=member)
        holds = Path(os.environ["BUILD_HOLD_DIR"])
        fixed_hold = (holds / DIRECTOR).rename(holds / member.id)
        with patch.object(shutdown, "retirement_remove", return_value=[]) as remove, \
             patch.object(copies, "remove_trees") as trees, redirect_stdout(io.StringIO()) as output:
            code = shutdown.retirement_clear_worktree(unit, settings=conf.read("production"),
                record=staff.StaffSnapshot((member,)), pane_workers=[], pane=herdr.NoPane("alpha"), require_cleanup=True)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn(["build_hold.py", "release", "--holder", member.id], case.calls)
        self.assertFalse(fixed_hold.exists())
        remove.assert_called_once()
        trees.assert_called_once_with(str(member.id))

        case.doCleanups()
        from scripts.herdr.test_worker_panes import WorkerPaneTests

        case = WorkerPaneTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        _ = case.enterContext(patch.object(pane_record, "RECORD_DIR", pane_launch.record_directory()))
        owner = case.worker("selected-native")
        foreign = replace(owner, id=staff.StaffId("foreign-native"),
                          current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000199"))
        case.world.add(owner, "w1:p7", label="misleading-current")
        case.world.add(foreign, "w1:p8", label=owner.name)
        case.world.locations["w1:p7"]["agent_session"] = {"value": "stale-report"}
        case.world.locations["w1:p8"]["agent_session"] = {"value": owner.current_session}
        case.world.record(owner, "w1:p8")
        entries = pane_record.read("agents")
        _ = entries.pop("w1:p7", None)
        pane_record.save("agents", entries)

        with self.subTest(record="reused pane"):
            self.assertEqual(shutdown.retirement_pane(owner, staff.snapshot()),
                             shutdown.RetiringPane("agents", "w1:p7", "terminal-w1:p7"))


    def test_remove_stray_preserves_the_real_producer_and_completes_a_rerun(self) -> None:
        for target, failure_step in (("stray", ""), ("current", ""), ("split", ""), ("pane-admission", ""),
                ("other-admission", ""), ("no-row", ""), ("unrelated-unproved", ""), ("unretained-admission", ""),
                ("transcript-failure", "transcript"), ("pane-failure", "pane"),
                ("stop-progress-failure", "stop-progress"), ("transcripts-only", "")):
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
                if target == "unretained-admission" or failure_step:
                    pane_record.save("agents", {})
                if failure_step in ("pane", "stop-progress"):
                    _ = case.client.rows[0].pop("agent_session")
                saved = ({"pane": "w1:p2", "terminal": "w1:p2-terminal"}
                         if target == "unretained-admission" or failure_step else
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
                if target == "transcripts-only":
                    (case.root / "sessions/123.json").unlink()
                    case.live.return_value = sessions.SessionNotLive()
                    case.process.return_value = sessions.SessionNotLive()
                    case.client.rows.clear()
                    pane_record.save("agents", {})
                    receipt.unlink()
                output = io.StringIO()
                with patch.object(herdr, "native_process", NATIVE_PROCESS), redirect_stdout(output):
                    if failure_step:
                        unlink = Path.unlink

                        def fail_transcript(path: Path, missing_ok: bool = False) -> None:
                            if path == transcripts[-1]:
                                raise OSError("fixture transcript deletion failure")
                            unlink(path, missing_ok=missing_ok)

                        def fail_stop_progress(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
                            if path.name == f".producer-cleanup-alpha-{identity}" and json.loads(text)["step"] == "admission":
                                raise OSError("fixture stop progress failure")
                            atomic_file.write_text_atomic(path, text, new_file_mode=new_file_mode)

                        with ExitStack() as failure:
                            if failure_step == "transcript":
                                _ = failure.enter_context(patch.object(Path, "unlink", new=fail_transcript))
                            elif failure_step == "stop-progress":
                                _ = failure.enter_context(patch.object(shutdown, "write_text_atomic", new=fail_stop_progress))
                            else:
                                _ = failure.enter_context(patch.object(case.client, "close_retained_pane", return_value=False))
                            failed = shutdown.main(["remove-stray", "alpha", identity])
                        self.assertNotEqual(failed, 0)
                        pending = herdr.json_object(cast(object, json.loads(
                            (receipt.parent / f".producer-cleanup-alpha-{identity}").read_text())), "pending cleanup")
                        self.assertEqual(pending["session"], identity)
                        self.assertEqual(pending["processes"], [{"pid": 123, "started": "generation-1", "kind": "claude"}])
                        self.assertEqual(pending["panes"], [{"pane": "w1:p2", "tab": "w1:t9", "terminal": "w1:p2-terminal"}])
                        self.assertEqual(pending["admission"], saved)
                        self.assertTrue(pending["admission_matches"])
                    status = shutdown.main(["remove-stray", "alpha", identity])
                    if failure_step:
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
                elif target == "transcripts-only":
                    self.assertEqual(status, 0, output.getvalue())
                    self.assertEqual(case.kill.call_args_list, [])
                    self.assertEqual(case.client.closed, [])
                    self.assertEqual(sessions.transcript_locations(identity), [])
                    self.assertIn(f"{identity}: removed stray", output.getvalue())
                    self.assertIn(f"{identity}: unchanged", output.getvalue())
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
