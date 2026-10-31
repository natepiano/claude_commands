from __future__ import annotations

import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Barrier, Lock, local
from typing import cast, final, override
from unittest.mock import Mock, call, patch

from scripts.accounts import accounts
from scripts.shutdown import stop_resume
import herdr
import pane_record
import sessions
from inventory import Inventory
import shutdown
import restart
import restart_support
import shutdown_record
from scripts.whoami.account import Account
from test_pane_restore import FakeClient


class ResumeClock(local):
    elapsed: float = 0.0


@final
class StopResumeTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.entry = pane_record.launch("w1:p2", "sid-alpha", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3"), "SHOWRUNNER_UNIT": "alpha"},
            ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "sonnet"])
        self.client = FakeClient([{"pane_id": "w1:p2", "tab_id": "w1:t9", "label": "alpha", "workspace_id": "w1",
                                   "agent_session": {"kind": "id", "value": "sid-alpha"}}])
        for label in ("claude3", "claude4"):
            config = self.root / f".{label}"
            config.mkdir()
            _ = (config / ".claude.json").write_text(json.dumps({
                "projects": {str(self.root): {"hasTrustDialogAccepted": True}},
            }))
        self.live = Mock(return_value=sessions.SessionNotLive())
        self.process = Mock(return_value=sessions.LiveSessionProcess(123))
        self.kill = Mock()
        self.wait = Mock()
        self.start = Mock(return_value="")
        self.seed = Mock(return_value=0)
        self.detach = Mock()
        self.real_detach = stop_resume._detach  # pyright: ignore[reportPrivateUsage]
        self.clock = ResumeClock()
        self.launched: set[str] = set()

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(accounts, "accounts_home", return_value=self.root))
        _ = self.enterContext(patch.object(herdr, "repository_root", return_value=self.root))
        _ = self.enterContext(patch.object(herdr, "repository_trust_key", return_value=self.root))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "SHUTDOWN_STATE_DIR": str(self.root / "shutdown")})))
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="agents"))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        pane_record.write("agents", self.entry)
        _ = self.enterContext(patch.object(sessions, "session_liveness", self.observe))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="generation-1"))
        _ = self.enterContext(patch.object(sessions, "process_liveness", self.process))
        _ = self.enterContext(patch.object(os, "kill", self.kill))
        _ = self.enterContext(patch.object(stop_resume, "Event", self.wait))
        cast(Mock, cast(Mock, self.wait.return_value).wait).side_effect = self.advance
        _ = self.enterContext(patch.object(self.client, "run", self.pane_run))
        _ = self.enterContext(patch.object(stop_resume, "monotonic", lambda: self.clock.elapsed))
        _ = self.enterContext(patch.object(time, "sleep", self.advance))
        _ = self.enterContext(patch("unit_runs.held", return_value=[]))
        _ = self.enterContext(patch.object(pane_record, "seed", self.seed))
        _ = self.enterContext(patch.object(stop_resume, "_detach", self.detach))

    def advance(self, seconds: float) -> None:
        self.clock.elapsed += seconds

    def observe(self, session: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
        return sessions.LiveSessionProcess(456) if session in self.launched else cast(
            sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown, self.live(session))

    def pane_run(self, *arguments: str) -> str:
        result = cast(str, self.start(*arguments))
        command = self._typed(arguments)
        self.launched.add(command[command.index("--resume") + 1])
        return result

    @staticmethod
    def _typed(arguments: tuple[str, ...]) -> tuple[str, ...]:
        """What the pane's shell parses from the one line herdr types."""
        return (*arguments[:3], *shlex.split(arguments[3]))

    def add_beta(self) -> None:
        second = pane_record.launch("w1:p4", "sid-beta", str(self.root),
            {"CLAUDE_CONFIG_DIR": self.entry["config_dir"]}, ["claude", "-n", "beta", "--model", "opus"])
        self.client.rows.append({"pane_id": "w1:p4", "tab_id": "w1:t7", "label": "beta", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-beta"}})
        pane_record.write("agents", second)

    def add_three_accounts(self) -> None:
        self.add_beta()
        beta = pane_record.read("agents")["w1:p4"]
        beta["config_dir"] = str(self.root / ".claude4")
        pane_record.write("agents", beta)
        gamma = self.entry.copy()
        gamma.update(pane_id="w1:p6", session_id="sid-gamma", name="gamma")
        pane_record.write("agents", gamma)
        self.client.rows.append({"pane_id": "w1:p6", "label": "gamma", "workspace_id": "w1",
                                "agent_session": {"kind": "id", "value": "sid-gamma"}})

    def mark_down_launches(self) -> None:
        for entry in pane_record.read("agents").values():
            entry["ended"] = True
            entry["restart_after_down"] = True
            pane_record.write("agents", entry)

    def restart_from_down(self, arguments: list[str]) -> tuple[int, str]:
        def account(label: str) -> Account:
            if label.endswith("@fixture.invalid"):
                name = label.removesuffix("@fixture.invalid")
                return Account("claude", label, name if name.startswith("claude") else "claude")
            if label not in {"claude3", "claude4"}:
                raise ValueError("unknown account " + label)
            return Account("claude", label + "@fixture.invalid", label)

        output = io.StringIO()
        with (patch.object(restart, "all_session_records", return_value=[]),
              patch.object(restart, "session_records", return_value=[]),
              patch.object(restart, "all_snapshots", return_value=[]),
              patch.object(restart, "lookup_account_hold", return_value=shutdown_record.AccountOpen()),
              patch.object(restart, "migrate", return_value=[]),
              patch.object(restart, "named_claude_account", side_effect=account),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_command_security_session", return_value=herdr.NotMacSession()),
              patch.object(herdr, "restore_server"),
              patch.object(restart_support, "unrecorded_local_sessions", return_value={}),
              patch.object(shutdown, "all_session_records", return_value=[]),
              patch.object(shutdown, "all_account_holds", return_value=[]),
              patch.object(shutdown, "named_claude_account", side_effect=account),
              patch.object(shutdown_record, "all_snapshots", return_value=[]),
              redirect_stdout(output)):
            result = shutdown.main(["restart", *arguments, "--no-alert"])
        return result, output.getvalue()

    def test_down_stops_three_in_parallel_marks_launches_and_rerun_stops_nobody(self) -> None:
        self.add_three_accounts()
        self.live.return_value = sessions.LiveSessionProcess(123)
        barrier = Barrier(3, timeout=2)

        def shared_stop(identity: str) -> stop_resume.Stopped:
            _ = barrier.wait()
            return stop_resume.Stopped(identity, 123)

        with patch.object(sessions, "running_sessions", return_value=[]), patch.object(
                stop_resume, "_stop", side_effect=shared_stop) as stopped, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["down"]), 0)
        self.assertEqual({item.args[0] for item in stopped.call_args_list}, {"sid-alpha", "sid-beta", "sid-gamma"})
        self.assertEqual(output.getvalue().splitlines(), [f"sid-{name}: stopped" for name in ("alpha", "beta", "gamma")])
        for entry in pane_record.read("agents").values():
            self.assertIs(entry.get("ended"), True)
            self.assertTrue(entry.get("restart_after_down", False))
        self.live.return_value = sessions.SessionNotLive()
        with patch.object(sessions, "running_sessions", return_value=[]), patch.object(
                stop_resume, "_stop") as again, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["down"]), 0)
        again.assert_not_called()
        self.assertEqual(output.getvalue().splitlines(), [f"sid-{name}: already down" for name in ("alpha", "beta", "gamma")])
        self.kill.assert_not_called()

    def test_down_only_resolves_names_and_ids_and_reports_each_failure(self) -> None:
        self.add_three_accounts()
        self.live.return_value = sessions.LiveSessionProcess(123)
        def shared_stop(identity: str) -> stop_resume.Stopped | stop_resume.StopFailed:
            return (stop_resume.Stopped(identity, 123) if identity == "sid-alpha"
                    else stop_resume.StopFailed(identity, "permission denied"))

        with patch.object(sessions, "running_sessions", return_value=[]), patch.object(
                stop_resume, "_stop", side_effect=shared_stop) as stopped, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["down", "--only", "alpha,sid-beta"]), 1)
        self.assertEqual({item.args[0] for item in stopped.call_args_list}, {"sid-alpha", "sid-beta"})
        self.assertIn("sid-beta: failed: permission denied", output.getvalue())
        entries = pane_record.read("agents")
        self.assertIs(entries["w1:p2"].get("ended"), True)
        self.assertIs(entries["w1:p4"].get("ended"), False)
        self.assertIs(entries["w1:p6"].get("ended"), False)

    def test_restart_restores_down_launches_in_their_own_tabs_accounts_and_rerun_is_idle(self) -> None:
        self.add_three_accounts()
        self.live.return_value = sessions.LiveSessionProcess(123)

        def shared_stop(identity: str) -> stop_resume.Stopped:
            return stop_resume.Stopped(identity, 123)

        with patch.object(sessions, "running_sessions", return_value=[]), patch.object(
                stop_resume, "_stop", side_effect=shared_stop):
            self.assertTrue(all(isinstance(result, stop_resume.Stopped) for result in stop_resume.down()))
        self.live.return_value = sessions.SessionNotLive()
        result, output = self.restart_from_down(["--here"])
        self.assertEqual(result, 0, output)
        self.assertEqual(self.launched, {"sid-alpha", "sid-beta", "sid-gamma"})
        self.assertEqual(output.count("Claude is up"), 3)
        commands = [self._typed(item.args) for item in self.start.call_args_list]
        self.assertEqual({(command[2], command[command.index("exec") + 1]) for command in commands},
                         {("w1:p2", "claude3"), ("w1:p4", "claude4"), ("w1:p6", "claude3")})
        for entry in pane_record.read("agents").values():
            self.assertIs(entry.get("ended"), False)
            self.assertNotIn("restart_after_down", entry)
        self.start.reset_mock()
        result, output = self.restart_from_down(["--all"])
        self.assertEqual(result, 0, output)
        self.start.assert_not_called()

    def test_restart_after_down_opens_a_fresh_tab_when_the_pane_is_gone(self) -> None:
        self.mark_down_launches()
        self.client.rows.clear()

        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            self.assertEqual((space, title, cwd), ("alpha", "alpha", str(self.root)))
            self.assertEqual(environment["CLAUDE_CONFIG_DIR"], self.entry["config_dir"])
            self.client.rows.append({"pane_id": "w2:p3", "label": title, "workspace_id": "w2",
                                    "agent_session": {"kind": "none"}})
            return "w2:p3"

        with patch.object(self.client, "open_tab", side_effect=open_tab) as opened:
            result, output = self.restart_from_down(["--all"])
        self.assertEqual(result, 0, output)
        opened.assert_called_once()
        self.assertEqual(self._typed(self.start.call_args.args)[2], "w2:p3")
        newest = pane_record.launch_for_session("sid-alpha")
        assert newest is not None
        self.assertIs(newest.get("ended"), False)
        self.assertNotIn("restart_after_down", newest)

    def test_restart_keeps_a_hand_closed_launch_closed(self) -> None:
        pane_record.end("agents", "w1:p2", "sid-alpha")
        result, output = self.restart_from_down(["--all"])
        self.assertEqual(result, 0, output)
        self.start.assert_not_called()
        self.assertIs(pane_record.read("agents")["w1:p2"].get("ended"), True)

    def test_down_saves_intent_before_stop_and_survives_a_missing_pane(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)

        def interrupted(_identity: str) -> stop_resume.Stopped:
            saved = pane_record.read("agents")["w1:p2"]
            self.assertTrue(saved.get("restart_after_down", False))
            self.assertIs(saved.get("ended"), False)
            self.live.return_value = sessions.SessionNotLive()
            self.client.rows.clear()
            raise OSError("coordinator interrupted after exit")

        with patch.object(sessions, "running_sessions", return_value=[]), patch.object(
                stop_resume, "_stop", side_effect=interrupted):
            result = stop_resume.down()
        self.assertEqual(result, [stop_resume.StopFailed("sid-alpha", "coordinator interrupted after exit")])
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("restart_after_down", False))
        with patch.object(sessions, "running_sessions", return_value=[]), patch.object(stop_resume, "_stop") as stopped:
            self.assertEqual(stop_resume.down(), [stop_resume.NotRunning("sid-alpha")])
        stopped.assert_not_called()
        with patch.object(self.client, "open_tab", return_value="w1:p2"):
            self.client.rows.append({"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1",
                                    "agent_session": {"kind": "none"}})
            result, output = self.restart_from_down(["--all"])
        self.assertEqual(result, 0, output)
        self.assertEqual(self.launched, {"sid-alpha"})

    def test_down_rerun_finishes_a_marked_live_launch_and_preserves_intent_at_exit(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)

        def interrupted(_identity: str) -> stop_resume.Stopped:
            self.assertTrue(pane_record.read("agents")["w1:p2"].get("restart_after_down", False))
            raise OSError("interrupted before stop")

        with patch.object(sessions, "running_sessions", return_value=[]), patch.object(
                stop_resume, "_stop", side_effect=interrupted):
            self.assertIsInstance(stop_resume.down()[0], stop_resume.StopFailed)
        pane_record.end("agents", "w1:p2", "sid-alpha")  # A SessionEnd hook cannot erase down's intent.
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("restart_after_down", False))
        with patch.object(sessions, "running_sessions", return_value=[]), patch.object(
                stop_resume, "_stop", return_value=stop_resume.Stopped("sid-alpha", 123)) as stopped:
            self.assertEqual(stop_resume.down(), [stop_resume.Stopped("sid-alpha", 123)])
        stopped.assert_called_once_with("sid-alpha")

    def test_down_hands_off_self_selection_and_child_finishes_without_rehandoff(self) -> None:
        self.add_three_accounts()
        self.live.return_value = sessions.LiveSessionProcess(123)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-alpha"}), patch.object(
                sessions, "running_sessions", return_value=[]), patch.object(stop_resume, "_stop") as stopped:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(["down"]), 0)
        stopped.assert_not_called()
        self.detach.assert_called_once_with(["down", "--", "sid-alpha", "sid-beta", "sid-gamma"], "down")
        self.assertEqual(output.getvalue().splitlines(), [f"sid-{name}: stopping in detached batch" for name in ("alpha", "beta", "gamma")])
        for entry in pane_record.read("agents").values():
            self.assertTrue(entry.get("restart_after_down", False))
        self.detach.reset_mock()
        log = self.root / "down.log"
        def shared_stop(identity: str) -> stop_resume.Stopped:
            return stop_resume.Stopped(identity, 123)

        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-alpha"}), patch.object(
                sessions, "running_sessions", return_value=[]), patch.object(
                stop_resume, "_stop", side_effect=shared_stop) as child_stop:
            self.assertEqual(stop_resume.main(["down", "--log", str(log), "--", "sid-alpha", "sid-beta", "sid-gamma"]), 0)
        self.assertEqual(child_stop.call_count, 3)
        self.detach.assert_not_called()
        self.assertEqual(log.read_text().splitlines(), [f"sid-{name}: stopped" for name in ("alpha", "beta", "gamma")])

    def test_restart_default_folder_and_bad_launch_do_not_block_other_accounts(self) -> None:
        self.add_three_accounts()
        self.mark_down_launches()
        entries = pane_record.read("agents")
        entries["w1:p2"]["config_dir"] = str(self.root / ".claude")
        entries["w1:p4"]["config_dir"] = str(self.root / ".bad-account")
        pane_record.write("agents", entries["w1:p2"])
        pane_record.write("agents", entries["w1:p4"])

        error = io.StringIO()
        with patch("scripts.whoami.account.claude_account", return_value=Account("claude", "home@fixture.invalid", "claude")), patch.object(
                stop_resume, "_heal_launch_trust", return_value=herdr.Trusted(self.root, self.root / "trust-fixture", self.root)), redirect_stderr(error):
            result, output = self.restart_from_down(["--here"])
        self.assertEqual(result, 1, output + error.getvalue())
        self.assertEqual(self.launched, {"sid-alpha", "sid-gamma"})
        commands = [self._typed(item.args) for item in self.start.call_args_list]
        self.assertEqual({command[command.index("exec") + 1] for command in commands}, {"claude", "claude3"})
        self.assertIn("beta: restart failed: unknown account bad-account", error.getvalue())
        self.assertIn("Claude is up", output)

    def test_restart_reconciles_live_down_marks_without_starting_again(self) -> None:
        self.mark_down_launches()
        self.launched.add("sid-alpha")
        result, output = self.restart_from_down(["--all"])
        self.assertEqual(result, 0, output)
        self.assertIn("alpha: already running", output)
        self.start.assert_not_called()
        entry = pane_record.read("agents")["w1:p2"]
        self.assertIs(entry.get("ended"), False)
        self.assertNotIn("restart_after_down", entry)
        before = pane_record.record_path("agents").read_bytes()
        result, output = self.restart_from_down(["--all"])
        self.assertEqual(result, 0, output)
        self.assertEqual(output.splitlines(), ["alpha: already running; skipped"])
        self.assertEqual(pane_record.record_path("agents").read_bytes(), before)

    def test_restart_idle_rerun_reports_each_live_session_without_changes(self) -> None:
        self.add_three_accounts()
        self.launched.update(("sid-alpha", "sid-beta", "sid-gamma"))
        before = pane_record.record_path("agents").read_bytes()
        for arguments in (["--here"], ["--all"]):
            with self.subTest(arguments=arguments):
                result, output = self.restart_from_down(arguments)
                self.assertEqual(result, 0, output)
                self.assertCountEqual(output.splitlines(), [f"{name}: already running; skipped" for name in ("alpha", "beta", "gamma")])
                self.assertEqual(pane_record.record_path("agents").read_bytes(), before)
        self.start.assert_not_called()

    def test_restart_only_reconciles_live_down_marks_without_starting_again(self) -> None:
        self.add_beta()
        self.launched.add("sid-alpha")
        live = sessions.SessionRecord(pid=456, sessionId="sid-alpha", name="alpha",
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        report = Inventory(machine=restart_support.local_machine(), login="claude3@fixture.invalid",
            label="claude3", sessions=[], unattributed=[])
        with (patch.object(sessions, "live_sessions", return_value=[live]),
              patch.object(shutdown, "listing", return_value=[]),
              patch.object(shutdown, "inventory", return_value=report),
              patch.object(stop_resume, "resume", side_effect=AssertionError("live session relaunched"))):
            for account in ([], ["claude3"]):
                for item in ("alpha", "sid-alpha"):
                    with self.subTest(account=account, item=item):
                        self.mark_down_launches()
                        arguments = [*account, "--only", item, "--here"]
                        before = pane_record.record_path("agents").read_bytes()
                        result, output = self.restart_from_down([*arguments, "--dry-run"])
                        self.assertEqual((result, output.splitlines()), (0, ["alpha: already running; skipped"]))
                        self.assertEqual(pane_record.record_path("agents").read_bytes(), before)
                        result, output = self.restart_from_down(arguments)
                        self.assertEqual((result, output.splitlines()), (0, ["alpha: already running; skipped"]))
                        entries = pane_record.read("agents")
                        self.assertIs(entries["w1:p2"].get("ended"), False)
                        self.assertNotIn("restart_after_down", entries["w1:p2"])
                        self.assertIs(entries["w1:p4"].get("ended"), True)
                        self.assertIs(entries["w1:p4"].get("restart_after_down"), True)
                        before = pane_record.record_path("agents").read_bytes()
                        result, output = self.restart_from_down(arguments)
                        self.assertEqual((result, output.splitlines()), (0, ["alpha: already running; skipped"]))
                        self.assertEqual(pane_record.record_path("agents").read_bytes(), before)
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def alive_until_kill(self, _pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return (sessions.LiveSessionProcess(123) if call(123, signal.SIGKILL) not in self.kill.call_args_list
                else sessions.SessionNotLive())

    def test_recovery_opens_a_fresh_tab_for_a_missing_pane_then_uses_the_shared_resume(self) -> None:
        self.client.rows.clear()
        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            self.assertEqual((space, title, cwd), ("alpha", "alpha", str(self.root)))
            self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.root / ".claude3"))
            self.client.rows.append({"pane_id": "w2:p3", "label": title, "workspace_id": "w2",
                "agent_session": {"kind": "none"}})
            return "w2:p3"
        with patch.object(self.client, "open_tab", side_effect=open_tab) as opened:
            result = stop_resume.resume("sid-alpha", why="watchdog recovery", restore_missing=True)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual((result.session_id, result.pane_id, result.account), ("sid-alpha", "w2:p3", "claude3"))
        opened.assert_called_once()
        self.assertIn("sid-alpha", self.launched)
        self.assertEqual(pane_record.read("agents")["w2:p3"]["session_id"], "sid-alpha")
        self.assertEqual(self._typed(self.start.call_args.args)[2], "w2:p3")

    def test_stop_returns_as_soon_as_term_takes_effect(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def exits_soon(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(123) if self.clock.elapsed < 0.1 else sessions.SessionNotLive()
        self.process.side_effect = exits_soon
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("sid-alpha", 123))
        self.assertLessEqual(self.clock.elapsed, 0.1)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_seed_reads_only_the_selected_session_pane(self) -> None:
        self.add_beta()
        _ = stop_resume.stop("alpha")
        self.seed.assert_called_once()
        select = cast(Callable[[herdr.PaneIdentity], bool], self.seed.call_args.kwargs["select"])
        self.assertEqual([pane["pane_id"] for pane in self.client.panes() if select(pane)], ["w1:p2"])

    def test_move_copies_source_folder_trust_to_the_target_before_start(self) -> None:
        target = self.root / ".claude4/.claude.json"
        _ = target.write_text(json.dumps({"projects": {}, "unrelated": "keep"}))
        def start_after_trust(*arguments: str) -> str:
            self.assertIsInstance(herdr.claude_trust(self.root, target), herdr.Trusted)
            command = self._typed(arguments)
            self.assertEqual(command[command.index("exec") + 1], "claude4")
            return ""
        self.start.side_effect = start_after_trust
        results = stop_resume.move_sessions(["alpha"], "claude4", why="usage limit")
        self.start.assert_called_once()
        result = results[0]
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.trust_copied_from, "claude3")
        line = accounts.move_result_line(result)
        self.assertEqual(line.count("copied folder trust from claude3"), 1)
        self.assertEqual(json.loads(target.read_text())["unrelated"], "keep")

    def test_group_move_leaves_untrusted_member_running_and_moves_the_other_two(self) -> None:
        self.add_beta()
        beta = pane_record.read("agents")["w1:p4"]
        beta["cwd"] = str(self.root / "beta")
        pane_record.write("agents", beta)
        gamma = self.entry.copy()
        gamma.update(pane_id="w1:p6", session_id="sid-gamma", name="gamma")
        pane_record.write("agents", gamma)
        self.client.rows.append({"pane_id": "w1:p6", "label": "gamma", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-gamma"}})
        target = self.root / ".claude4"
        command = accounts.trust_command(Path(beta["cwd"]), accounts.NamedAccount("claude4", target))
        def liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(123) if identity == "sid-beta" else sessions.SessionNotLive()
        self.live.side_effect = liveness
        def stopped(identity: str) -> stop_resume.Stopped:
            return stop_resume.Stopped(identity, 123)
        for unreadable in (False, True):
            for caller in ("", "sid-beta"):
                with self.subTest(unreadable=unreadable, caller=caller):
                    self.launched.clear()
                    self.start.reset_mock()
                    self.detach.reset_mock()
                    def trust(folder: Path, account_file: Path) -> herdr.Trusted | herdr.Untrusted | herdr.UnreadableTrustFile:
                        if folder == self.root:
                            return herdr.Trusted(folder, account_file, folder)
                        if unreadable and account_file.parent == target:
                            return herdr.UnreadableTrustFile(folder, account_file, "PermissionError")
                        return herdr.Untrusted(folder, account_file, (folder,))
                    with patch.object(herdr, "claude_trust", side_effect=trust), patch.object(
                            stop_resume, "_stop", side_effect=stopped) as stop, patch.dict(
                            os.environ, {"CLAUDE_CODE_SESSION_ID": caller}):
                        results = stop_resume.move_sessions(["alpha", "beta", "gamma"], "claude4", why="limit")
                    self.assertCountEqual(stop.call_args_list, [call("alpha"), call("gamma")])
                    self.kill.assert_not_called()
                    self.assertEqual(self.start.call_count, 2)
                    self.assertEqual(self.launched, {"sid-alpha", "sid-gamma"})
                    self.assertEqual(self.observe("sid-beta"), sessions.LiveSessionProcess(123))
                    self.assertEqual(len(results), 3)
                    self.assertEqual(results[0], stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude4"))
                    self.assertEqual(results[2], stop_resume.ClaudeUp("sid-gamma", "w1:p6", "claude4"))
                    result = results[1]
                    self.assertIsInstance(result, stop_resume.ResumeFailed)
                    assert isinstance(result, stop_resume.ResumeFailed)
                    self.assertEqual(result.session_id, "sid-beta")
                    self.assertIn("cannot read" if unreadable else "Claude has not trusted this folder", result.reason)
                    self.assertIn(f"Run: {command}", result.reason)
                    line = accounts.move_result_line(accounts.SessionMoveRefused(result.session_id, result.reason))
                    self.assertIn("sid-beta: not moved:", line)
                    self.assertIn(command, line)

    def test_move_preflights_copyable_trust_then_stops_copies_and_resumes(self) -> None:
        target = self.root / ".claude4"
        events: list[str] = []
        copied = False
        def trust(folder: Path, account_file: Path) -> herdr.Trusted | herdr.Untrusted:
            if account_file.parent == target and not copied:
                return herdr.Untrusted(folder, account_file, (folder,))
            return herdr.Trusted(folder, account_file, folder)
        def copy(folder: Path, source: Path, destination: Path) -> herdr.Trusted:
            nonlocal copied
            self.assertEqual(source.parent, self.root / ".claude3")
            self.assertEqual(destination.parent, target)
            events.append("copy")
            copied = True
            return herdr.Trusted(folder, destination, folder)
        heal = herdr.heal_claude_trust
        def checked(folder: Path, destination: Path, sources: list[tuple[str, Path]], *, dry_run: bool
                    ) -> herdr.Trusted | herdr.TrustCopied | herdr.Untrusted | herdr.UnreadableTrustFile:
            events.append("preflight" if dry_run else "heal")
            return heal(folder, destination, sources, dry_run=dry_run)
        def stop(_session: str) -> stop_resume.Stopped:
            self.assertEqual(events, ["preflight"])
            self.assertFalse(copied)
            events.append("stop")
            return stop_resume.Stopped("sid-alpha", 123)
        def start(*_arguments: str) -> str:
            self.assertTrue(copied)
            events.append("resume")
            return ""
        self.start.side_effect = start
        with patch.object(herdr, "claude_trust", side_effect=trust), patch.object(
                herdr, "copy_claude_trust", side_effect=copy), patch.object(
                herdr, "heal_claude_trust", side_effect=checked), patch.object(stop_resume, "_stop", side_effect=stop):
            results = stop_resume.move_sessions(["alpha"], "claude4", why="limit")
        self.assertEqual(events, ["preflight", "stop", "heal", "copy", "resume"])
        self.assertEqual(results, [stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude4")])
        assert isinstance(results[0], stop_resume.ClaudeUp)
        self.assertEqual(results[0].trust_copied_from, "claude3")

    def test_move_without_trust_refuses_with_target_trust_command_before_start(self) -> None:
        for label in ("claude3", "claude4"):
            _ = (self.root / f".{label}/.claude.json").write_text('{"projects": {}}')
        self.live.return_value = sessions.LiveSessionProcess(123)
        with patch.object(stop_resume, "_stop", wraps=stop_resume._stop) as stop:  # pyright: ignore[reportPrivateUsage]
            results = stop_resume.move_sessions(["alpha"], "claude4", why="usage limit")
        stop.assert_not_called()
        self.kill.assert_not_called()
        self.assertEqual(self.observe("sid-alpha"), sessions.LiveSessionProcess(123))
        result = results[0]
        self.assertIsInstance(result, stop_resume.ResumeFailed)
        assert isinstance(result, stop_resume.ResumeFailed)
        command = accounts.trust_command(self.root, accounts.NamedAccount("claude4", self.root / ".claude4"))
        self.assertIn("Claude has not trusted this folder", result.reason)
        self.assertIn(f"Run: {command}", result.reason)
        self.start.assert_not_called()
        self.detach.assert_not_called()

    def test_resume_with_unreadable_target_trust_refuses_before_start(self) -> None:
        _ = (self.root / ".claude4/.claude.json").write_text('{"private": "never disclose"')
        result = stop_resume.resume("alpha", to="claude4", why="usage limit")
        self.assertIsInstance(result, stop_resume.ResumeFailed)
        assert isinstance(result, stop_resume.ResumeFailed)
        self.assertIn("cannot read", result.reason)
        self.assertIn("Run: " + accounts.trust_command(self.root, accounts.NamedAccount("claude4", self.root / ".claude4")), result.reason)
        self.assertNotIn("never disclose", result.reason)
        self.start.assert_not_called()

    def test_a_stopped_tab_resumes_by_id_or_label_from_its_launch_entry(self) -> None:
        self.client.rows[0]["agent_session"] = None
        for session in ("sid-alpha", "alpha"):
            self.launched.clear()
            self.assertEqual(stop_resume.resume(session, why="maintenance"), stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude3"))
        self.assertEqual(stop_resume.resume("sid-other", why="maintenance"), stop_resume.SessionNotFound("sid-other"))

    def test_a_new_session_herdr_has_no_id_for_is_found_through_the_launches_read_first(self) -> None:
        self.client.rows[0]["agent_session"] = None
        pane_record.record_path("agents").unlink()
        def seed(_session: str, *, select: Callable[[herdr.PaneIdentity], bool]) -> int:
            self.assertEqual({pane["pane_id"] for pane in self.client.panes() if select(pane)}, {"w1:p2"})
            pane_record.write("agents", self.entry)
            return 0
        self.seed.side_effect = seed
        self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="limit"),
                         [stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude4")])

    def test_launches_are_read_while_claude_runs_before_any_stop(self) -> None:
        order: list[str] = []
        def seed(_session: str, *, select: Callable[[herdr.PaneIdentity], bool]) -> int:
            self.assertEqual({pane["pane_id"] for pane in self.client.panes() if select(pane)}, {"w1:p2"})
            order.append("seed")
            return 0
        self.seed.side_effect = seed
        def stop_one(session: str) -> stop_resume.NotRunning:
            order.append("stop")
            return stop_resume.NotRunning(session)
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            _ = stop_resume.move_sessions(["sid-alpha", "sid-beta"], "claude4", why="limit")
        self.assertEqual(order, ["seed", "stop", "stop"])
        self.launched.clear()
        self.seed.side_effect = herdr.HerdrFailure("no reply")
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.NotRunning("sid-alpha"))

    def test_stop_sends_term_then_kill_to_the_same_claude_and_only_marks_its_launch_ended(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def signal_delivered(_pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                self.live.return_value = sessions.SessionNotLive()
                self.process.return_value = sessions.SessionNotLive()
        self.kill.side_effect = signal_delivered
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("sid-alpha", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        self.assertTrue(all(0 < invocation.args[0] <= 0.1
                            for invocation in cast(Mock, cast(Mock, self.wait.return_value).wait).call_args_list))
        launch_path = pane_record.record_path("agents")
        expected = cast(dict[str, dict[str, object]], json.loads(before[launch_path]))
        expected["w1:p2"]["ended"] = True
        self.assertEqual(json.loads(launch_path.read_bytes()), expected)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(set(after), set(before))
        self.assertEqual({path: content for path, content in after.items() if path != launch_path},
                         {path: content for path, content in before.items() if path != launch_path})
        self.start.assert_not_called()

    def test_stop_does_not_kill_an_exited_or_replaced_process(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.Stopped("sid-alpha", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_registry_removal_does_not_resume_until_the_saved_process_exits(self) -> None:
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = self.alive_until_kill
        def start(*_command: str) -> str:
            self.assertGreater(self.process.call_count, 4, "registry deletion is not process exit")
            self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
            return ""
        self.start.side_effect = start
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude4"))

    def test_stop_confirms_exit_after_kill(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = self.alive_until_kill
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("sid-alpha", 123))
        self.assertGreater(self.process.call_count, 4)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])

    def test_a_process_still_alive_after_kill_fails_and_never_resumes(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        result = stop_resume.move("alpha", "claude4", why="limit")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("123", result.reason)
        self.assertIn("SIGKILL", result.reason)
        self.start.assert_not_called()

    def test_self_move_hands_off_the_whole_batch_without_signalling(self) -> None:
        self.add_beta()
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-alpha"}), patch.object(stop_resume, "_detach", wraps=self.real_detach), patch.object(sys, "platform", platform), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "", "")) as run, patch.object(subprocess, "Popen") as popen:
                self.client.rows[0]["agent_session"] = None
                result = stop_resume.move_sessions(["beta", "alpha"], "claude4", why="limit", warn_seconds=3)
                self.assertEqual(result, [stop_resume.MoveHandedOff("sid-beta"), stop_resume.MoveHandedOff("sid-alpha")])
                invocation = run.call_args if platform == "linux" else popen.call_args
                self.assertIsNotNone(invocation)
                assert invocation is not None
                argv = cast(list[str], invocation.args[0])
                if platform == "linux":
                    self.assertEqual(argv[:5], ["systemd-run", "--user", "--collect", "--quiet", "--no-block"])
                    self.assertIn("CLAUDE_CODE_SESSION_ID", argv)
                else:
                    self.assertTrue(cast(bool, invocation.kwargs["start_new_session"]))
                self.assertNotIn("CLAUDE_CODE_SESSION_ID", cast(dict[str, str], invocation.kwargs["env"]))
                self.assertEqual(cast(object, invocation.kwargs["stdin"]), subprocess.DEVNULL)
                self.assertIn("batch", argv)
                self.assertEqual(argv[argv.index("--target") + 1], "claude4")
                self.assertEqual(argv[-2:], ["beta", "alpha"])
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_detached_batch_uses_the_same_moves_and_logs_each_member(self) -> None:
        log = self.root / "batch.log"
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-alpha"}), patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
            result = stop_resume.main(["batch", "--target", "claude4", "--why", "limit", "--log", str(log), "alpha", "missing"])
        self.assertEqual(result, 1)
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertIn('"result": "ClaudeUp"', log.read_text())
        self.assertIn('"result": "SessionNotFound"', log.read_text())
        self.start.assert_called_once()
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        self.seed.assert_called_once()
        run.assert_not_called()
        popen.assert_not_called()

    def test_failed_handoff_reports_failure_without_signalling(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-alpha"}), patch.object(stop_resume, "_detach", wraps=self.real_detach), patch.object(sys, "platform", "linux"), patch.object(subprocess, "run", side_effect=OSError("service manager unavailable")):
            result = stop_resume.move_sessions(["alpha"], "claude4", why="limit")
        self.assertEqual(result, [stop_resume.StopFailed("alpha", "cannot hand off move: service manager unavailable")])
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_reused_pid_generation_is_checked_before_each_signal(self) -> None:
        for changed_before_term in (True, False):
            with self.subTest(before_term=changed_before_term):
                self.kill.reset_mock()
                self.live.return_value = sessions.LiveSessionProcess(123)
                self.process.side_effect = ([sessions.SessionNotLive()] if changed_before_term else
                                           [sessions.LiveSessionProcess(123), sessions.SessionNotLive()])
                result = stop_resume.stop("alpha")
                self.assertIsInstance(result, stop_resume.NotRunning if changed_before_term else stop_resume.Stopped)
                self.assertEqual(self.kill.call_args_list, [] if changed_before_term else [call(123, signal.SIGTERM)])

    def test_absent_and_unknown_sessions_are_distinct_and_unsignalled(self) -> None:
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("sid-alpha"))
        self.live.return_value = sessions.SessionLivenessUnknown("registry unreadable")
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopFailed("sid-alpha", "registry unreadable"))
        self.assertEqual(stop_resume.stop("elsewhere"), stop_resume.SessionNotFound("elsewhere"))
        self.assertEqual(stop_resume.resume("elsewhere", why="maintenance"), stop_resume.SessionNotFound("elsewhere"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_tab_labels_are_exact_and_unidentified_or_ambiguous_tabs_never_signal(self) -> None:
        self.assertEqual(stop_resume.stop("alph"), stop_resume.SessionNotFound("alph"))
        self.client.rows.append({"pane_id": "w1:p4", "label": "empty", "workspace_id": "w1"})
        self.assertEqual(stop_resume.stop("empty"), stop_resume.SessionNotFound("empty"))
        self.client.rows.append({"pane_id": "w1:p5", "label": "alpha", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-other"}})
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopFailed("alpha", "alpha: several tabs match"))
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.NotRunning("sid-alpha"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_resume_of_a_live_session_succeeds_without_starting_or_writing(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        before = pane_record.record_path("agents").read_bytes()
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude3"))
        self.assertEqual(pane_record.record_path("agents").read_bytes(), before)
        self.start.assert_not_called()
        self.detach.assert_not_called()

    def test_resume_runs_account_exec_in_its_own_pane_with_one_message(self) -> None:
        self.entry["environment"].update(HERDR_PANE_ID="w9:p9", HERDR_WORKSPACE_ID="w9")
        pane_record.write("agents", self.entry)
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude3"))
        self.start.assert_called_once()
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[:4], ("pane", "run", "w1:p2", "env"))
        self.assertIn("SHOWRUNNER_UNIT=alpha", command)
        self.assertIn("HERDR_PANE_ID=w1:p2", command)
        self.assertEqual(command[4:6], ("-u", "HERDR_TAB_ID"))
        self.assertIn("HERDR_WORKSPACE_ID=w1", command)
        self.assertLess(self.clock.elapsed, 1.0)
        self.detach.assert_called_once()
        index = command.index("exec")
        self.assertTrue(command[index - 1].endswith("accounts/accounts.py"))
        self.assertEqual(command[index + 1:-1], ("claude3", "--resume", "sid-alpha", "-n", "alpha",
            "--remote-control", "alpha", "--model", "sonnet", "--settings", '{"disableAgentView": true}'))
        self.assertIn("stopped and resumed", command[-1])
        self.assertIn("maintenance", command[-1])
        self.assertIn("Continue where you stopped", command[-1])
        self.assertIn("Codex", command[-1])
        self.assertEqual(self.client.closed, [])
        self.assertEqual(self.client.started, [])

    def test_admitted_pane_run_with_no_start_retries_three_times_then_fails(self) -> None:
        with patch.object(self.client, "run", self.start):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ResumeFailed("sid-alpha", "never started"))
        self.assertEqual(self.start.call_count, 3)
        self.assertEqual(self.clock.elapsed, 60.0)
        self.assertEqual({invocation.args[2] for invocation in self.start.call_args_list}, {"w1:p2"})

    def test_resume_returns_before_the_stay_window_and_detaches_its_generation(self) -> None:
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertLess(self.clock.elapsed, stop_resume.STAY_SECONDS)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        arguments = cast(list[str], self.detach.call_args.args[0])
        self.assertEqual(arguments[:3], ["stay", "--session", "sid-alpha"])
        self.assertEqual(arguments[arguments.index("--pid") + 1], "456")
        self.assertIn("generation-1", arguments)
        self.assertEqual(arguments[arguments.index("--why") + 1], "maintenance")
        self.start.assert_called_once()

    def run_stay_child(self, *, requester: str = "", why: str = "usage limit") -> tuple[int, str]:
        def exits_soon(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(456) if self.clock.elapsed < 0.6 else sessions.SessionNotLive()
        self.process.side_effect = exits_soon
        log = self.root / "stay.log"
        result = stop_resume.main(["stay", "--session", "sid-alpha", "--pane", "w1:p2",
            "--account", "claude3", "--pid", "456", "--generation", "generation-1",
            "--requester", requester, "--why", why, "--log", str(log)])
        return result, log.read_text(encoding="utf-8")

    def test_detached_stay_check_alerts_on_early_exit_without_restarting(self) -> None:
        import quota_alert
        with patch.object(quota_alert, "tell_user") as push:
            result, log = self.run_stay_child()
        self.assertEqual(result, 1)
        self.assertIn("stopped 0.6 s", log)
        self.assertLess(self.clock.elapsed, stop_resume.STAY_SECONDS)
        push.assert_called_once()
        self.start.assert_not_called()

    def test_early_exit_tells_showrunner_pushes_except_probe_and_enters_watchdog(self) -> None:
        from scripts.production import showrunners, staff_sources
        from scripts.accounts import unit_watchdog
        from scripts.notify.push_texts import RenderedPush
        import quota_alert
        self.entry["environment"]["SHOWRUNNER_UNIT_ID"] = "alpha-unit"
        for probe in (False, True):
            for outcome in ("watchdog: alpha-unit restarted on claude3",
                            "watchdog: alpha-unit: folder no longer exists; restore dropped"):
                with self.subTest(probe=probe, outcome=outcome):
                    self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1" if probe else "0"
                    pane_record.write("agents", self.entry)
                    with (patch.object(staff_sources, "unit_parent", return_value=staff_sources.UnitProduction("demo", str(self.root / "production.md"))),
                          patch.object(showrunners, "tell", return_value="told demo") as tell,
                          patch.object(quota_alert, "tell_user") as push,
                          patch.object(unit_watchdog, "run", return_value=[outcome]) as recovery):
                        result, log = self.run_stay_child(requester="other")
                        self.assertEqual(result, 1)
                        tell.assert_called_once()
                        self.assertEqual(tell.call_args.args[0], "demo")
                        line = cast(str, tell.call_args.args[2])
                        self.assertEqual(line, f"alpha stopped 0.6 s after its move or resume to claude3; {outcome}")
                        self.assertIn(line, log)
                        self.assertEqual(push.call_count, 0 if probe else 1)
                        if not probe:
                            self.assertEqual(cast(RenderedPush, push.call_args.args[0]).values["cause"], line)
                        recovery.assert_called_once()
                    self.clock.elapsed = 0.0

    def test_named_probe_reports_rounded_time_account_retry_and_shutdown_sender(self) -> None:
        from scripts.production import showrunners
        import quota_alert
        self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1"
        pane_record.write("agents", self.entry)
        with (patch.object(showrunners, "current_address", return_value="session:requester"),
              patch.object(showrunners, "send_to_address", return_value=showrunners.NoticeDelivery(("told requester",))) as send,
              patch.object(quota_alert, "tell_user") as push):
            result, log = self.run_stay_child(requester="requester")
        self.assertEqual(result, 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "session:requester")
        self.assertEqual(send.call_args.kwargs, {"recipient": "requester", "sender": "shutdown"})
        line = cast(str, send.call_args.args[2])
        self.assertEqual(line, "alpha stopped 0.6 s after its move or resume to claude3; resume it: "
                         + "cd ~/.claude && python3 scripts/shutdown/shutdown.py resume alpha --to claude3 --why 'usage limit'")
        self.assertIn(line, log)
        push.assert_not_called()

    def test_unnamed_session_uses_its_id_in_alert_and_retry(self) -> None:
        from scripts.production import showrunners
        from scripts.notify.push_texts import RenderedPush
        import quota_alert
        self.entry["name"] = ""
        self.client.rows[0]["label"] = None
        pane_record.write("agents", self.entry)
        with (patch.object(showrunners, "tell", return_value="told requester") as tell,
              patch.object(quota_alert, "tell_user") as push):
            result, log = self.run_stay_child(requester="requester")
        self.assertEqual(result, 1)
        line = cast(str, tell.call_args.args[2])
        self.assertEqual(line, "sid-alpha stopped 0.6 s after its move or resume to claude3; resume it: "
                         + "cd ~/.claude && python3 scripts/shutdown/shutdown.py resume sid-alpha --to claude3 --why 'usage limit'")
        self.assertIn(line, log)
        self.assertEqual(cast(RenderedPush, push.call_args.args[0]).values["cause"], line)

    def test_no_requester_or_unit_never_tells_showrunners_and_only_pushes_non_probes(self) -> None:
        from scripts.production import showrunners
        import quota_alert
        for probe in (False, True):
            with self.subTest(probe=probe):
                self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1" if probe else "0"
                pane_record.write("agents", self.entry)
                with (patch.object(showrunners, "tell") as tell,
                      patch.object(showrunners, "tell_others") as broadcast,
                      patch.object(quota_alert, "tell_user") as push):
                    result, log = self.run_stay_child()
                self.assertEqual(result, 1)
                self.assertIn("alpha stopped 0.6 s", log)
                tell.assert_not_called()
                broadcast.assert_not_called()
                self.assertEqual(push.call_count, 0 if probe else 1)
                self.clock.elapsed = 0.0

    def test_non_unit_early_exit_tells_only_the_requesting_showrunner(self) -> None:
        from scripts.production import showrunners
        import quota_alert
        # The requester's registry is independent of the resumed session's mark.
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-requester"}),
              patch.object(showrunners, "production_of", return_value=self.root / "requester-production.md") as lookup,
              patch.object(stop_resume, "_detach", wraps=self.real_detach) as detach,
              patch.object(subprocess, "run"), patch.object(subprocess, "Popen"),
              patch.object(showrunners, "tell") as tell,
              patch.object(showrunners, "tell_others") as broadcast,
              patch.object(quota_alert, "tell_user") as push):
            _ = stop_resume.resume("alpha", why="maintenance")
            invocation = cast(list[str], (detach.call_args.args[0]))
            # Read the actual detached command, after _detach appends the requester.
            platform_call = cast(Mock, subprocess.Popen if sys.platform == "darwin" else subprocess.run)
            command = cast(list[str], platform_call.call_args.args[0])
            arguments = command[command.index("stay"):]
            self.assertEqual(invocation[0], "stay")
            self.assertIn("--requester", arguments)
            self.assertEqual(arguments[arguments.index("--requester") + 1], "requester")
            lookup.assert_called_once_with("sid-requester")
            log = self.root / "stay.log"
            self.process.return_value = sessions.SessionNotLive()
            arguments[arguments.index("--log") + 1] = str(log)
            result = stop_resume.main(arguments)
            self.assertEqual(result, 1)
            tell.assert_called_once()
            self.assertEqual(tell.call_args.args[0], "requester")
            broadcast.assert_not_called()
            push.assert_called_once()

    def test_detached_self_move_keeps_the_requester_for_its_stay_child(self) -> None:
        log = self.root / "requested-batch.log"
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-requester"}):
            result = stop_resume.main(["batch", "--requester", "requester", "--target", "claude4",
                                       "--why", "limit", "--log", str(log), "alpha"])
            self.assertEqual(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "")
        self.assertEqual(result, 0)
        arguments = cast(list[str], self.detach.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--requester") + 1], "requester")

    def test_saved_entry_finds_an_unlabelled_pane_with_empty_or_stale_herdr_id(self) -> None:
        self.client.rows[0]["label"] = None
        for agent in (None, {"kind": "id", "value": "stale-session"}):
            with self.subTest(agent=agent):
                self.launched.clear()
                self.client.rows[0]["agent_session"] = agent
                self.assertEqual(stop_resume.resume("sid-alpha", why="maintenance"),
                                 stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude3"))

    def test_saved_entry_never_selects_a_different_live_conversation(self) -> None:
        self.client.rows[0]["agent_session"] = {"kind": "id", "value": "another-live-session"}
        self.live.return_value = sessions.LiveSessionProcess(789)
        self.assertEqual(stop_resume.resume("sid-alpha", why="maintenance"), stop_resume.SessionNotFound("sid-alpha"))
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.SessionNotFound("sid-alpha"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_saved_entries_for_closed_panes_do_not_create_ambiguous_matches(self) -> None:
        missing = self.entry.copy()
        missing["pane_id"] = "w1:p-missing"
        pane_record.write("agents", missing)
        self.assertEqual(stop_resume.resume("sid-alpha", why="maintenance"),
                         stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude3"))

    def test_resume_refuses_a_mismatched_entry_but_does_not_wait_for_idle(self) -> None:
        self.entry["session_id"] = "sid-other"
        pane_record.write("agents", self.entry)
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.SessionNotFound("alpha"))
        self.start.assert_not_called()
        self.entry["session_id"] = "sid-alpha"
        self.live.return_value = sessions.SessionNotLive()
        pane_record.write("agents", self.entry)
        with patch.object(self.client, "pane_activity", side_effect=AssertionError("no idle gate")):
            self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude3"))

    def test_move_stops_then_runs_the_target_account_in_the_same_pane(self) -> None:
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        result = stop_resume.move("alpha", "claude4", why="weekly limit")
        self.assertEqual(result, stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude4"))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[command.index("exec") + 1], "claude4")
        self.assertIn("moved from claude3 to claude4", command[-1])

    def test_move_of_a_stopped_session_resumes_but_a_failed_stop_does_not(self) -> None:
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude4"))
        self.start.reset_mock()
        self.launched.clear()
        self.live.return_value = sessions.SessionLivenessUnknown("identity unreadable")
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.StopFailed("sid-alpha", "identity unreadable"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_a_failed_pane_run_never_reports_resume_success(self) -> None:
        self.start.side_effect = herdr.HerdrFailure("pane run refused")
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ResumeFailed("sid-alpha", "pane run refused"))
        self.assertEqual(self.client.closed, [])

    def test_batch_stops_all_before_any_resume_and_uses_each_own_pane(self) -> None:
        self.add_beta()
        third = pane_record.launch("w1:p6", "sid-gamma", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3")}, ["claude", "-n", "gamma"])
        pane_record.write("agents", third)
        self.client.rows.append({"pane_id": "w1:p6", "label": "gamma", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-gamma"}})
        both_stopping = Barrier(3, timeout=3)
        both_resuming = Barrier(3, timeout=3)
        stopped: list[str] = []
        lock = Lock()
        def stop_one(session: str) -> stop_resume.Stopped:
            _ = both_stopping.wait()
            with lock:
                stopped.append(session)
            return stop_resume.Stopped(session, 123)
        def run(*_command: str) -> str:
            self.assertCountEqual(stopped, ["sid-alpha", "sid-beta", "sid-gamma"])
            _ = both_resuming.wait()
            return ""
        self.start.side_effect = run
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            results = stop_resume.move_sessions(["sid-alpha", "sid-beta", "sid-gamma"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.ClaudeUp("sid-alpha", "w1:p2", "claude4"),
                                   stop_resume.ClaudeUp("sid-beta", "w1:p4", "claude4"),
                                   stop_resume.ClaudeUp("sid-gamma", "w1:p6", "claude4")])
        calls = [self._typed(cast(tuple[str, ...], invocation.args)) for invocation in self.start.call_args_list]
        self.assertEqual({args[2]: args[args.index("--resume") + 1] for args in calls},
                         {"w1:p2": "sid-alpha", "w1:p4": "sid-beta", "w1:p6": "sid-gamma"})

    def test_batch_failures_do_not_stop_other_members(self) -> None:
        def stop_one(session: str) -> stop_resume.NotRunning:
            if session == "stop-error":
                raise OSError("cannot signal")
            return stop_resume.NotRunning(session)
        def resume_one(session: str, *, why: str, to: str) -> stop_resume.ClaudeUp | stop_resume.SessionNotFound:
            self.assertEqual(why, "limit")
            if session == "resume-error":
                raise OSError("cannot run")
            if session == "gone-tab":
                return stop_resume.SessionNotFound(session)
            return stop_resume.ClaudeUp(session, "own-pane", to)
        with patch.object(stop_resume, "_stop", side_effect=stop_one), patch.object(stop_resume, "resume", side_effect=resume_one):
            results = stop_resume.move_sessions(["stop-error", "resume-error", "gone-tab", "good"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.StopFailed("stop-error", "cannot signal"),
            stop_resume.ResumeFailed("resume-error", "cannot run"),
            stop_resume.ResumeFailed("gone-tab", "session has no tab after stop"), stop_resume.ClaudeUp("good", "own-pane", "claude4")])

    def test_warning_precedes_all_stops_and_is_limited_to_fifteen_seconds(self) -> None:
        events: list[str] = []
        def warn(session: str, _target: str, _why: str, _seconds: int) -> None:
            events.append("warn " + session)
        def stop_one(session: str) -> stop_resume.StopFailed:
            self.assertCountEqual(events, ["warn first", "warn second"])
            return stop_resume.StopFailed(session, "fixture")
        with patch.object(stop_resume, "_warn_session", side_effect=warn), patch.object(stop_resume, "_stop", side_effect=stop_one), patch.object(stop_resume, "monotonic", side_effect=[100.0, 100.0, 102.0]):
            results = stop_resume.move_sessions(["first", "second"], "claude4", why="limit", warn_seconds=15)
            self.assertEqual(len(results), 2)
            with self.assertRaises(ValueError):
                _ = stop_resume.move_sessions(["first"], "claude4", why="limit", warn_seconds=16)
        cast(Mock, cast(Mock, self.wait.return_value).wait).assert_called_once_with(13.0)

    def test_cli_prints_one_line_and_uses_the_result_exit_status(self) -> None:
        for arguments, expected in ((["stop", "alpha"], 0), (["resume", "alpha", "--why", "maintenance"], 0),
                                    (["stop", "elsewhere"], 2)):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(arguments), expected)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.launched.clear()
        self.live.return_value = sessions.LiveSessionProcess(123)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["resume", "alpha", "--why", "maintenance"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(self.launched, set())
        with patch.object(stop_resume, "resume", return_value=stop_resume.ResumeFailed(
                "sid-alpha", "fixture refusal")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["resume", "alpha", "--why", "maintenance"]), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("fixture refusal", output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
