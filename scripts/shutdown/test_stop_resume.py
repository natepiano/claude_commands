from __future__ import annotations

import io
import fcntl
import json
import math
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
from scripts.whoami import account
from scripts.shutdown import stop_resume
from scripts.production import showrunners, staff, staff_sources, stall_watch
from scripts.shutdown.inventory import PausedSeat
import herdr
import pane_record
import sessions
import shutdown
import unit_lookup
from test_pane_restore import FakeClient


class ResumeClock(local):
    elapsed: float = 0.0
    identity: str = ""


@final
class StopResumeTests(unittest.TestCase):
    def test_resume_from_the_target_pane_refuses_without_a_launch(self) -> None:
        with (patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["resume", "alpha"]), 1)
        self.assertEqual(output.getvalue().splitlines(), [
            f"{self.entry['session_id']}: resume failed: the session is this pane; type /exit and run resume <name> from another pane"])
        self.start.assert_not_called()
        self.kill.assert_not_called()
        self.assertEqual(pane_record.read("agents")["w1:p2"], self.entry)

    def test_admitted_pane_run_with_no_start_fails_after_one_attempt(self) -> None:
        with patch.object(self.client, "run", self.start):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "never started"))
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.clock.elapsed, 20.0)
        self.assertEqual({invocation.args[2] for invocation in self.start.call_args_list}, {"w1:p2"})

    def test_batch_without_a_tab_reaches_the_shared_restore(self) -> None:
        up = stop_resume.ClaudeUp("saved", "new-pane", "claude4")
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.SessionNotFound("saved")),
              patch.object(stop_resume, "resume", return_value=up) as resumed):
            results = stop_resume.move_sessions(["saved"], "claude4", why="limit")
        self.assertEqual(results, [up])
        resumed.assert_called_once_with("saved", why="limit", to="claude4")

    def test_stale_launch_metadata_does_not_prevent_resume(self) -> None:
        identity = self.entry["session_id"]
        self.entry.update(session_id="sid-other", flags_read=False, ended=True)
        pane_record.write("agents", self.entry)
        tab = herdr.TabSession("w1:p2", "alpha", "w1", identity)
        with (patch.object(stop_resume, "_tab_of", return_value=tab), redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume(identity)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("launch record anomaly:", output.getvalue())
        self.start.assert_called_once()
        self.assertIn(identity, self.launched)

    def test_failed_stop_bookkeeping_reports_an_anomaly_and_keeps_the_stopped_result(self) -> None:
        result = stop_resume.Stopped(self.entry["session_id"], 123)
        with (patch.object(stop_resume, "_stop", return_value=result),
              patch.object(pane_record, "end", side_effect=OSError("fixture write failed")),
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(stop_resume.stop("alpha"), result)
        self.assertEqual(output.getvalue().splitlines(),
                         [f"{result.session_id}: stop note anomaly: fixture write failed"])

    def test_failed_account_bookkeeping_reports_an_anomaly_and_resumes(self) -> None:
        with (patch.object(pane_record, "write", side_effect=OSError("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha", to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("account record anomaly: fixture", output.getvalue())
        self.start.assert_called_once()

    def test_missing_prompt_confirmation_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        with (patch.object(self.client, "submit_opening_prompt", return_value=herdr.PromptNotSubmitted("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("prompt anomaly: fixture", output.getvalue())
        self.start.assert_called_once()

    def test_missing_stay_generation_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        with (patch.object(sessions, "process_start", return_value=None), redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("observer anomaly: process generation is unavailable", output.getvalue())
        self.start.assert_called_once()
        self.detach.assert_not_called()

    def test_failed_stay_launch_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        self.detach.side_effect = OSError("fixture")
        with redirect_stderr(io.StringIO()) as output:
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("observer anomaly: fixture", output.getvalue())
        self.start.assert_called_once()

    def test_failed_title_change_reports_an_anomaly_and_resumes(self) -> None:
        with (patch.object(self.client, "retitle", side_effect=herdr.HerdrFailure("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("title anomaly: fixture", output.getvalue())
        self.start.assert_called_once()

    def test_resume_names_share_one_snapshot_and_default_reason(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts-unit"), "unit", "accounts", None,
                             current_session=identity)
        for reference in (row.name, row.id, identity):
            self.launched.clear()
            self.client.prompts.clear()
            with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))) as read:
                result = stop_resume.resume(reference)
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            read.assert_called_once()
            self.assertIn("resumed by name", self.client.prompts[0][1].text)
            self.assertNotIn("the herdr server restarted", self.client.prompts[0][1].text)

    def test_unknown_ambiguous_and_remote_names_refuse(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts-unit"), "unit", "accounts", None,
                             current_session=identity)
        other = staff.StaffRow(staff.StaffId("other-unit"), "unit", "accounts", None,
                               current_session=staff.StaffId("other-session"))
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row, other))):
            for reference, text in (("absent", "unknown name"), ("accounts", "ambiguous name")):
                result = stop_resume.resume(reference)
                assert isinstance(result, stop_resume.ResumeFailed)
                self.assertIn(text, result.reason)
                if reference == "accounts":
                    self.assertIn("accounts-unit", result.reason)
                    self.assertIn("other-unit", result.reason)
                else:
                    self.assertEqual(result.reason, "unknown name")
        remote_row = staff.StaffRow(row.id, row.kind, row.name, row.parent,
            current_session=identity, worktree=staff.PresentWorktree("other-machine", self.root))
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((remote_row,))):
            result = stop_resume.resume("accounts")
        self.assertEqual(result, stop_resume.ResumeFailed("accounts", "session is on other-machine"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_unknown_name_lists_only_close_local_units_and_showrunners(self) -> None:
        import remote

        machine = remote.local_machine()
        rows = (
            staff.StaffRow(staff.StaffId("accounts-unit"), "unit", "accounts", None,
                current_session=staff.StaffId("accounts-session"),
                worktree=staff.PresentWorktree(machine, self.root)),
            staff.StaffRow(staff.StaffId("team-accounts"), "showrunner", "team-accounts", None,
                current_session=staff.StaffId("team-session"), doc=staff.LiveDoc(machine, self.root / "plan.md")),
            staff.StaffRow(staff.StaffId("account-worker"), "worker", "accounts-worker", None,
                current_session=staff.StaffId("worker-session"), worktree=staff.PresentWorktree(machine, self.root)),
            staff.StaffRow(staff.StaffId("remote-accounts"), "unit", "accounts-remote", None,
                current_session=staff.StaffId("remote-session"), worktree=staff.PresentWorktree("other-machine", self.root)),
            staff.StaffRow(staff.StaffId("unrelated"), "unit", "herdr", None,
                current_session=staff.StaffId("herdr-session"), worktree=staff.PresentWorktree(machine, self.root)),
        )
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)) as read:
            self.assertEqual(stop_resume.resume("acc"), stop_resume.ResumeFailed(
                "acc", "unknown name; candidates: accounts, team-accounts"))
        read.assert_called_once()
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)):
            self.assertEqual(stop_resume.resume("absent"), stop_resume.ResumeFailed("absent", "unknown name"))
        self.start.assert_not_called()

    def test_resume_of_a_live_target_is_an_idempotent_success(self) -> None:
        identity = self.entry["session_id"]
        self.launched.add(identity)
        self.client.native_ids["w1:p2"] = identity
        self.assertEqual(stop_resume.resume("alpha", to="claude4"), stop_resume.ClaudeUp(
            identity, "w1:p2", "claude4"))
        self.start.assert_not_called()
        self.kill.assert_not_called()
        self.detach.assert_not_called()

    def test_resume_by_name_of_a_running_session_reports_success_without_a_launch(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts-unit"), "unit", "accounts", None,
                             current_session=identity)
        self.client.native_ids["w1:p2"] = identity
        self.live.return_value = sessions.LiveSessionProcess(123)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["resume", "accounts"]), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            f"{identity}: unchanged; Claude runs on claude3 in w1:p2"])
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_resume_arguments_use_the_current_row_name_after_a_rename_and_clear(self) -> None:
        fixed = staff.StaffId(self.entry["session_id"])
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        owner = staff.StaffRow(fixed, "unit", "row-name", None, current_session=current)
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((owner,))):
            arguments = stop_resume.resume_arguments(self.entry)
        self.assertEqual(arguments[arguments.index("--resume") + 1], current)
        self.assertEqual(arguments[arguments.index("-n") + 1], "row-name")
        self.assertEqual(arguments[arguments.index("--remote-control") + 1], "row-name")
        self.assertEqual(self.entry["name"], "alpha")

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.entry = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3"), "SHOWRUNNER_UNIT": "alpha"},
            ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "sonnet"])
        self.client = FakeClient([{"pane_id": "w1:p2", "tab_id": "w1:t9", "label": "alpha", "workspace_id": "w1",
                                   "agent_session": {"kind": "id", "value": "019b3a40-4250-7000-8000-000000000102"}}])
        self.client_result = self.client.result
        self.tab_titles: dict[str, str] = {"w1:t9": "alpha"}
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
        _ = self.enterContext(patch.object(account, "process_config_dir", side_effect=self.process_account))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "STAFF_FILE": str(self.root / "staff.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "", "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
        })))
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="agents"))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        pane_record.write("agents", self.entry)
        _ = self.enterContext(patch.object(stall_watch, "STATE_DIR",
                                         Path(self.enterContext(tempfile.TemporaryDirectory()))))
        _ = self.enterContext(patch.object(sessions, "session_liveness", self.observe))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="generation-1"))
        _ = self.enterContext(patch.object(staff, "runtime_id", side_effect=self.runtime_identity))
        _ = self.enterContext(patch.object(sessions, "process_liveness", self.process))
        _ = self.enterContext(patch.object(os, "kill", self.kill))
        _ = self.enterContext(patch.object(stop_resume, "Event", self.wait))
        cast(Mock, cast(Mock, self.wait.return_value).wait).side_effect = self.advance
        _ = self.enterContext(patch.object(self.client, "run", self.pane_run))
        _ = self.enterContext(patch.object(self.client, "result", self.pane_result))
        _ = self.enterContext(patch.object(stop_resume, "monotonic", lambda: self.clock.elapsed))
        _ = self.enterContext(patch.object(time, "sleep", self.advance))
        _ = self.enterContext(patch("unit_runs.held", return_value=[]))
        _ = self.enterContext(patch.object(pane_record, "seed", self.seed))
        _ = self.enterContext(patch.object(stop_resume, "_detach", self.detach))

    def runtime_identity(self, pid: int | None = None) -> str:
        return self.clock.identity if pid else ""

    def process_account(self, _pid: int) -> account.ConfiguredClaudeFolder:
        return account.ConfiguredClaudeFolder(self.root / (".claude4" if self.launched else ".claude3"))

    def advance(self, seconds: float) -> None:
        self.clock.elapsed += seconds

    def observe(self, session: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
        self.clock.identity = session
        return sessions.LiveSessionProcess(456) if session in self.launched else cast(
            sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown, self.live(session))

    def pane_result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        if arguments[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": row.get("tab_id", str(row["pane_id"]) + ":tab"),
                              "label": self.tab_titles.get(
                                  str(row.get("tab_id", str(row["pane_id"]) + ":tab")), row["label"]),
                              "pane_count": 1} for row in self.client.rows]}
        return self.client_result(*arguments, deadline=deadline)

    def pane_run(self, *arguments: str) -> str:
        if arguments[:2] in (("pane", "rename"), ("tab", "rename")):
            for row in self.client.rows:
                tab = str(row.get("tab_id", str(row["pane_id"]) + ":tab"))
                if arguments[:2] == ("pane", "rename") and arguments[2] == row["pane_id"]:
                    if tab not in self.tab_titles:
                        self.tab_titles[tab] = str(row["label"])
                    row["label"] = arguments[3]
                elif arguments[:2] == ("tab", "rename") and arguments[2] == tab:
                    self.tab_titles[tab] = arguments[3]
            return "{}"
        result = cast(str, self.start(*arguments))
        command = self._typed(arguments)
        identity = command[command.index("--resume") + 1]
        self.launched.add(identity)
        self.client.native_ids[arguments[2]] = identity
        pane = arguments[2]
        if pane not in pane_record.read("agents"):
            original = next((entry for entry in pane_record.read("agents").values()
                             if entry["session_id"] == identity), None)
            assert original is not None
            environment = dict(original["environment"])
            for item in command[:command.index("exec")]:
                if "=" in item:
                    key, value = item.split("=", 1)
                    environment[key] = value
            replacement = pane_record.launch(pane, identity, original["cwd"], environment,
                ["claude", *command[command.index("exec") + 2:]])
            replacement["session_id"] = original["session_id"]
            entries = pane_record.read("agents")
            entries[pane] = replacement
            pane_record.save("agents", entries)
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

    def alive_until_kill(self, _pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return (sessions.LiveSessionProcess(123) if call(123, signal.SIGKILL) not in self.kill.call_args_list
                else sessions.SessionNotLive())

    def test_saved_review_resumes_without_retired_allocation_options(self) -> None:
        run, cwd = str(self.root / "worker run"), str(self.root / "working tree")
        task = [run, cwd, "old prompt.md", "review", "check this change", "reviewing", "2"]
        command = ["bash", str(stop_resume.SCRIPTS / "direct/review.sh"),
                   "--pool", "code", "--lease", "x/y/7", "--phase", "3p", "--lens", "contract",
                   "--slot", "review", *task]
        seat = PausedSeat(run_dir=run, seat="review2", role="review", thread_id="same-thread",
                          turn_live=True, launcher_command=command)
        expected = [*command[:2], "--phase", "3p", "--lens", "contract", "--to", "review2",
                    run, cwd, str(Path(run) / "review2.resume-prompt.md"), *task[3:]]
        rebuilt = stop_resume.resume_worker_command(seat, cwd)
        self.assertEqual(shlex.split(rebuilt), expected)
        self.assertNotIn("--pool", shlex.split(rebuilt))
        self.assertNotIn("--lease", shlex.split(rebuilt))
        seat["launcher_command"] = shlex.split(rebuilt)
        self.assertEqual(stop_resume.resume_worker_command(seat, cwd), rebuilt)

    def test_recovery_opens_a_fresh_tab_for_a_missing_pane_then_uses_the_shared_resume(self) -> None:
        self.client.rows.clear()
        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            self.assertEqual((space, title, cwd), ("alpha", "alpha", str(self.root)))
            self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.root / ".claude3"))
            self.client.rows.append({"pane_id": "w2:p3", "label": title, "workspace_id": "w2",
                "agent_session": {"kind": "none"}})
            return "w2:p3"
        with patch.object(self.client, "open_tab", side_effect=open_tab) as opened:
            result = stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="watchdog recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual((result.session_id, result.pane_id, result.account), ("019b3a40-4250-7000-8000-000000000102", "w2:p3", "claude3"))
        opened.assert_called_once()
        self.assertIn("019b3a40-4250-7000-8000-000000000102", self.launched)
        self.assertEqual(pane_record.read("agents")["w2:p3"]["session_id"], "019b3a40-4250-7000-8000-000000000102")
        self.assertEqual(self._typed(self.start.call_args.args)[2], "w2:p3")

    def test_recovery_uses_one_snapshot_for_the_renamed_row_title_and_arguments(self) -> None:
        from scripts.production import session_staff

        fixed = staff.StaffId(self.entry["session_id"])
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        row = staff.StaffRow(fixed, "showrunner", "row-name", None, current_session=current)
        record = staff.StaffSnapshot((row,))
        retained = self.entry.copy()
        retained["session_id"] = current
        pane_record.save("agents", {retained["pane_id"]: retained})
        self.client.rows.clear()
        opened: list[str] = []

        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            _ = space, cwd, environment
            opened.append(title)
            self.client.rows.append({"pane_id": "w2:p3", "label": title, "workspace_id": "w2"})
            return "w2:p3"

        with (patch.object(staff, "initial_snapshot", return_value=record) as snapshot,
              patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources({}, ())),
              patch.object(self.client, "open_tab", side_effect=open_tab)):
            result = stop_resume.resume(str(fixed), why="recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(opened, ["row-name"])
        snapshot.assert_called_once()
        command = self._typed(self.start.call_args.args)
        self.assertEqual(command[command.index("-n") + 1], "row-name")
        self.assertEqual(command[command.index("--resume") + 1], current)
        self.assertEqual(pane_record.read("agents")["w2:p3"]["name"], "row-name")

    def test_recovery_retitles_an_existing_pane_with_the_current_row_name(self) -> None:
        from scripts.production import session_staff

        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "showrunner", "row-name", None, current_session=identity)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))) as snapshot,
              patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources({}, ()))):
            result = stop_resume.resume(str(identity), why="recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(self.client.rows[0]["label"], "row-name")
        command = self._typed(self.start.call_args.args)
        self.assertEqual(command[command.index("-n") + 1], "row-name")
        snapshot.assert_called_once()

    def test_recovery_reconciles_titles_and_preserves_unnamed_titles(self) -> None:
        from scripts.production import session_staff

        identity = staff.StaffId(self.entry["session_id"])
        cases = (
            ("stale-tab", "row-name", "row-name", "old-tab", 1),
            ("current-titles", "row-name", "row-name", "row-name", 2),
            ("unnamed", "", "manual-pane", "manual-tab", 1),
        )
        for case, name, pane_title, tab_title, attempts in cases:
            with self.subTest(case=case):
                retained = self.entry.copy()
                retained["name"] = name
                pane_record.save("agents", {retained["pane_id"]: retained})
                self.client.rows[0]["label"] = pane_title
                self.tab_titles["w1:t9"] = tab_title
                record = staff.StaffSnapshot((staff.StaffRow(
                    identity, "showrunner", name, None, current_session=identity),)) if name else staff.StaffSnapshot(())
                state = self.root / case
                with (patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}),
                      patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources({}, ())),
                      patch.object(self.client, "retitle", wraps=self.client.retitle) as retitle,
                      patch.object(self.client, "run", wraps=self.client.run) as commands,
                      patch.object(self.client, "order_tabs", return_value={}) as order,
                      patch.object(self.client, "save_layout", wraps=self.client.save_layout) as save):
                    for _ in range(attempts):
                        self.launched.clear()
                        result = stop_resume.resume(str(identity), why="recovery")
                        self.assertIsInstance(result, stop_resume.ClaudeUp)
                    renames = [invocation for invocation in commands.call_args_list
                               if invocation.args[:2] in (("pane", "rename"), ("tab", "rename"))]
                    if case == "stale-tab":
                        self.assertEqual(renames, [call("tab", "rename", "w1:t9", name)])
                        order.assert_called_once()
                        save.assert_called_once()
                    else:
                        self.assertEqual(renames, [])
                        order.assert_not_called()
                        save.assert_not_called()
                        self.assertFalse(herdr.layout_path("agents").exists())
                    self.assertEqual(self.client.rows[0]["label"], name or pane_title)
                    self.assertEqual(self.tab_titles["w1:t9"], name or tab_title)
                    if name:
                        self.assertEqual(retitle.call_count, attempts)
                    else:
                        retitle.assert_not_called()

    def test_recovery_fixture_rejects_an_unrecorded_resume_identity(self) -> None:
        with self.assertRaises(AssertionError):
            _ = self.pane_run("pane", "run", "w2:p3", "exec claude --resume unrecorded-session")
        self.assertNotIn("w2:p3", pane_record.read("agents"))

    def test_fixed_owner_resume_uses_current_pane_and_preserves_staff_and_retained_record(self) -> None:
        from scripts.production.staff_test_support import isolated_staff_file, isolated_source_readers, source_environment

        path = self.root / "staff.json"
        with (isolated_staff_file(path, {}),
              patch.dict(os.environ, source_environment(self.root / "sources")),
              isolated_source_readers()):
            parent = staff.ensure("showrunner", name="demo", runtime_id="019b3a40-4250-7000-8000-000000000101")
            owner = staff.ensure("unit", name="alpha", parent=parent.id, runtime_id=self.entry["session_id"])
            current = staff.native_id("019b3a40-4250-7000-8000-000000000103")
            _ = staff.update_current_session(owner.current_session, current)
            self.client.rows[0]["agent_session"] = {"kind": "id", "value": current}
            pane_record.write("agents", self.entry)
            record = pane_record.record_path("agents")
            before = path.read_bytes(), record.read_bytes(), record.stat().st_mtime_ns
            result = stop_resume.resume(owner.id, why="fixture changed conversation")
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            assert isinstance(result, stop_resume.ClaudeUp)
            self.assertEqual((result.session_id, result.pane_id, result.account), (current, "w1:p2", "claude3"))
            arguments = self._typed(self.start.call_args.args)
            self.assertEqual(arguments[arguments.index("--resume") + 1], current)
            self.assertNotIn(owner.id, arguments)
            self.assertEqual(self.launched, {current})
            self.assertEqual((path.read_bytes(), record.read_bytes(), record.stat().st_mtime_ns), before)

    def test_stop_returns_as_soon_as_term_takes_effect(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def exits_soon(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(123) if self.clock.elapsed < 0.1 else sessions.SessionNotLive()
        self.process.side_effect = exits_soon
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertLessEqual(self.clock.elapsed, 0.1)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def prompt_retirement_fixture(self, *, alerted: bool) -> Path:
        """Publish cancellation requests for the watcher with an isolated send.py recorder."""
        self.entry["environment"].update({"SHOWRUNNER_UNIT": "production", "SHOWRUNNER_UNIT_ID": "alpha"})
        pane_record.write("agents", self.entry)
        state = self.root / "stall-watch"
        state.mkdir(exist_ok=True)
        sender = self.root / "retire.py"
        log = self.root / "retire.json"
        _ = sender.write_text("import json, pathlib, sys\n"
                              + f"pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        _ = self.enterContext(patch.object(stall_watch, "STATE_DIR", state))
        _ = self.enterContext(patch.object(stall_watch, "SEND", sender))
        _ = self.enterContext(patch.object(showrunners, "canonical_slug", return_value="production"))
        prompt = stall_watch.PromptWait(session_id="019b3a40-4250-7000-8000-000000000102", created=0,
            key="stall-watch:prompt:production:alpha", since=0, alerted=alerted, tool="permission", retiring=False, told=60)
        stall_watch.save_prompt(stall_watch.prompt_path("production", "alpha"), prompt)
        return log

    def assert_prompt_cancellation_requested(self, log: Path) -> None:
        self.assertFalse(log.exists(), "stop and move must never send")
        path = stall_watch.prompt_path("production", "alpha")
        self.assertTrue(path.exists())
        requests = list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json"))
        self.assertTrue(requests)
        for request in requests:
            record = cast(dict[str, object], json.loads(request.read_text()))
            self.assertEqual(set(record), {"session_id", "at"})
            self.assertEqual(record["session_id"], "019b3a40-4250-7000-8000-000000000102")
            self.assertIsInstance(record["at"], (float, int))

    def drain_prompt_cancellations(self) -> None:
        with (patch.object(showrunners, "load_settings", return_value={}),
              patch.object(showrunners, "registered_showrunners", return_value=showrunners.ShowrunnerDiscovery()),
              patch.object(stall_watch, "processes", return_value=[]),
              patch.object(stall_watch, "prompt_grace_seconds", return_value=120),
              patch.object(unit_lookup, "read_run_states", return_value=unit_lookup.RunStates({}))):
            _ = stall_watch.tick(100)

    def test_stop_requests_prompt_cancellation_before_returning_without_sending(self) -> None:
        log = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.assert_prompt_cancellation_requested(log)

    def test_move_requests_prompt_cancellation_before_resuming_without_sending(self) -> None:
        log = self.prompt_retirement_fixture(alerted=False)
        def start_after_request(*arguments: str) -> str:
            self.assertIn("exec", self._typed(arguments))
            self.assert_prompt_cancellation_requested(log)
            return ""
        self.start.side_effect = start_after_request
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.assert_prompt_cancellation_requested(log)

    def test_failed_prompt_retirement_stays_requested_until_the_watcher_confirms_it(self) -> None:
        log = self.prompt_retirement_fixture(alerted=True)
        sender = self.root / "retire.py"
        original = sender.read_text()
        _ = sender.write_text(original + "raise SystemExit(3)\n")
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.assert_prompt_cancellation_requested(log)
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.drain_prompt_cancellations()
        self.assertIn("stall-watch: retire:", errors.getvalue())
        path = stall_watch.prompt_path("production", "alpha")
        self.assertTrue(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")))
        _ = sender.write_text(original)
        self.drain_prompt_cancellations()
        self.assertFalse(path.exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])
        self.assertEqual(json.loads(log.read_text()), ["retire", "--to", "user", "--from", "stall-watch",
                                                      "--key", "stall-watch:prompt:production:alpha"])

    def test_cancellation_key_errors_cannot_prevent_stop_bookkeeping_or_move_resume(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        errors = io.StringIO()
        with (patch.object(stall_watch, "retire_session_prompt", side_effect=KeyError("session_id")),
              redirect_stderr(errors)):
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("ended"))
        self.assertIn("019b3a40-4250-7000-8000-000000000102: prompt cancellation:", errors.getvalue())
        pane_record.write("agents", self.entry)
        self.live.return_value = sessions.SessionNotLive()
        self.process.side_effect = None
        self.process.return_value = sessions.LiveSessionProcess(456)
        with (patch.object(stall_watch, "retire_session_prompt", side_effect=KeyError("session_id")),
              redirect_stderr(errors)):
            self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp)
        self.assertEqual(errors.getvalue().count("prompt cancellation:"), 2)

    def test_stop_and_move_never_take_a_held_watcher_lock(self) -> None:
        log = self.prompt_retirement_fixture(alerted=True)
        real_flock = fcntl.flock
        forbidden_attempts: list[tuple[int, int]] = []
        with (stall_watch.STATE_DIR / "lock").open("a+") as lock:
            real_flock(lock, fcntl.LOCK_EX)
            held = os.fstat(lock.fileno())
            watcher_identity = (held.st_dev, held.st_ino)

            def forbid_watcher_lock(file: int | io.IOBase, operation: int) -> None:
                descriptor = file if isinstance(file, int) else file.fileno()
                stat = os.fstat(descriptor)
                identity = (stat.st_dev, stat.st_ino)
                if identity == watcher_identity:
                    forbidden_attempts.append(identity)
                    raise AssertionError("watcher lock must not be acquired")
                real_flock(file, operation)

            with patch.object(fcntl, "flock", side_effect=forbid_watcher_lock):
                self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
                self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp)
        self.assertEqual(forbidden_attempts, [], "stop and move must never attempt the watcher lock")
        self.assert_prompt_cancellation_requested(log)

    def test_a_delayed_cancellation_never_retires_a_resumed_sessions_new_prompt(self) -> None:
        log = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        path = stall_watch.prompt_path("production", "alpha")
        stall_watch.save_prompt(path, stall_watch.PromptWait(session_id="019b3a40-4250-7000-8000-000000000102", created=time.time() + 1,
            key="stall-watch:prompt:production:alpha", since=200, alerted=False, tool="new-request", retiring=False))
        self.drain_prompt_cancellations()
        self.assertFalse(log.exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])
        saved = stall_watch.saved_prompt(path)
        assert saved is not None
        self.assertEqual((saved["since"], saved["retiring"]), (200, False))

    def test_a_malformed_cancellation_does_not_stop_valid_requests(self) -> None:
        log = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        bad = stall_watch.STATE_DIR / "aaa.prompt.cancel.json"
        _ = bad.write_text("{")
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.drain_prompt_cancellations()
        self.assertIn(str(bad), errors.getvalue())
        self.assertTrue(bad.exists())
        self.assertTrue(log.exists())
        self.assertFalse(stall_watch.prompt_path("production", "alpha").exists())

    def test_a_mac_stop_publishes_cancellation_without_any_lookup(self) -> None:
        log = self.prompt_retirement_fixture(alerted=True)
        with (patch("platform.system", return_value="Darwin"),
              patch.object(pane_record, "launch_for_session", side_effect=AssertionError("no launch lookup")) as launch,
              patch.object(staff_sources, "unit_parent", side_effect=AssertionError("no remote lookup")) as parent,
              patch.object(showrunners, "socket_for", side_effect=AssertionError("no socket lookup")) as socket):
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        launch.assert_not_called()
        parent.assert_not_called()
        socket.assert_not_called()
        self.assert_prompt_cancellation_requested(log)

    def test_launch_deletion_after_stop_cannot_lose_the_old_alerts_cancellation(self) -> None:
        log = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        # retire_unit removes launch records after stop and before the next watcher pass.
        pane_record.record_path("agents").unlink()
        self.drain_prompt_cancellations()
        self.assertEqual(json.loads(log.read_text()), ["retire", "--to", "user", "--from", "stall-watch",
                                                      "--key", "stall-watch:prompt:production:alpha"])
        self.assertFalse(stall_watch.prompt_path("production", "alpha").exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])

    def test_missing_pane_recovery_does_not_duplicate_a_live_session(self) -> None:
        self.client.rows.clear()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        with patch.object(self.client, "open_tab") as opened:
            result = stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="recovery")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))
        opened.assert_not_called()
        self.start.assert_not_called()

    def test_seed_reads_only_the_selected_session_pane(self) -> None:
        self.add_beta()
        _ = stop_resume.stop("alpha")
        self.seed.assert_called_once()
        select = cast(Callable[[herdr.PaneIdentity], bool], self.seed.call_args.kwargs["select"])
        self.assertEqual([pane["pane_id"] for pane in self.client.panes() if select(pane)], ["w1:p2"])

    def test_move_resume_failure_is_pending_and_rerun_finishes_without_restopping(self) -> None:
        self.start.side_effect = herdr.HerdrFailure("registry temporarily unreadable")
        with patch.object(stop_resume, "_stop", return_value=stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123)):
            pending = stop_resume.move("alpha", "claude4", why="limit")
        self.assertIsInstance(pending, stop_resume.ResumePending)
        assert isinstance(pending, stop_resume.ResumePending)
        self.assertEqual(accounts.move_result_line(pending),
                         "019b3a40-4250-7000-8000-000000000102: stopped, resume pending: registry temporarily unreadable")
        self.start.side_effect = None
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.start.reset_mock()
        self.seed.reset_mock()
        before = pane_record.record_path("agents").read_bytes()
        with patch.object(stop_resume, "_stop", side_effect=AssertionError("rerun stopped the destination")), \
                patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000102"}):
            self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                             stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.start.assert_not_called()
        self.seed.assert_not_called()
        self.detach.assert_called_once()  # Only the successful resume's stay check, never a rerun handoff.
        self.assertEqual(pane_record.record_path("agents").read_bytes(), before)

    def test_shutdown_resume_finishes_a_pending_move_and_reruns_with_one_line(self) -> None:
        self.start.side_effect = herdr.HerdrFailure("cannot start yet")
        self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ResumePending)
        self.start.side_effect = None
        for iteration in range(2):
            output = io.StringIO()
            with redirect_stdout(output):
                status = shutdown.main(["resume", "alpha", "--why", "finish move"])
            self.assertEqual(status, 0, output.getvalue())
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertIn("Claude is up on claude4" if iteration == 0 else "unchanged; Claude runs on claude4", output.getvalue())

    def test_a_stopped_tab_resumes_by_id_or_label_from_its_launch_entry(self) -> None:
        self.client.rows[0]["agent_session"] = None
        for session in ("019b3a40-4250-7000-8000-000000000102", "alpha"):
            self.launched.clear()
            self.assertEqual(stop_resume.resume(session, why="maintenance"), stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))
        self.assertEqual(stop_resume.resume("sid-other", why="maintenance"), stop_resume.ResumeFailed("sid-other", "unknown name"))

    def test_a_new_session_herdr_has_no_id_for_is_found_through_the_launches_read_first(self) -> None:
        self.client.rows[0]["agent_session"] = None
        pane_record.record_path("agents").unlink()
        def seed(_session: str, *, select: Callable[[herdr.PaneIdentity], bool]) -> int:
            self.assertEqual({pane["pane_id"] for pane in self.client.panes() if select(pane)}, {"w1:p2"})
            pane_record.write("agents", self.entry)
            return 0
        self.seed.side_effect = seed
        self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="limit"),
                         [stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4")])

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
            _ = stop_resume.move_sessions(["019b3a40-4250-7000-8000-000000000102", "sid-beta"], "claude4", why="limit")
        self.assertEqual(order, ["seed", "stop", "stop"])
        self.launched.clear()
        self.seed.side_effect = herdr.HerdrFailure("no reply")
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))

    def test_stop_sends_term_then_kill_to_the_same_claude_and_only_marks_its_launch_ended(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def signal_delivered(_pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                self.live.return_value = sessions.SessionNotLive()
                self.process.return_value = sessions.SessionNotLive()
        self.kill.side_effect = signal_delivered
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
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
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_registry_removal_does_not_resume_until_the_saved_process_exits(self) -> None:
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = self.alive_until_kill
        def start(*_command: str) -> str:
            self.assertGreater(self.process.call_count, 4, "registry deletion is not process exit")
            self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
            return ""
        self.start.side_effect = start
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))

    def test_stop_confirms_exit_after_kill(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = self.alive_until_kill
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
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
        from scripts.production.staff_test_support import isolated_staff_file

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))
        self.add_beta()
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000102"}), patch.object(stop_resume, "_detach", wraps=self.real_detach), patch.object(sys, "platform", platform), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "", "")) as run, patch.object(subprocess, "Popen") as popen:
                self.client.rows[0]["agent_session"] = None
                result = stop_resume.move_sessions(["beta", "alpha"], "claude4", why="limit", warn_seconds=3)
                self.assertEqual(result, [stop_resume.MoveHandedOff("sid-beta"), stop_resume.MoveHandedOff("019b3a40-4250-7000-8000-000000000102")])
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
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000102"}), patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
            result = stop_resume.main(["batch", "--target", "claude4", "--why", "limit", "--log", str(log), "alpha", "missing"])
        self.assertEqual(result, 1)
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertIn('"result": "ClaudeUp"', log.read_text())
        self.assertIn('"result": "ResumePending"', log.read_text())
        self.start.assert_called_once()
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        self.seed.assert_called_once()
        run.assert_not_called()
        popen.assert_not_called()

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
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.live.return_value = sessions.SessionLivenessUnknown("registry unreadable")
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopFailed("019b3a40-4250-7000-8000-000000000102", "registry unreadable"))
        self.assertEqual(stop_resume.stop("elsewhere"), stop_resume.SessionNotFound("elsewhere"))
        self.assertEqual(stop_resume.resume("elsewhere", why="maintenance"), stop_resume.ResumeFailed("elsewhere", "unknown name"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_tab_labels_are_exact_and_unidentified_or_ambiguous_tabs_never_signal(self) -> None:
        self.assertEqual(stop_resume.stop("alph"), stop_resume.SessionNotFound("alph"))
        self.client.rows.append({"pane_id": "w1:p4", "label": "empty", "workspace_id": "w1"})
        self.assertEqual(stop_resume.stop("empty"), stop_resume.SessionNotFound("empty"))
        self.client.rows.append({"pane_id": "w1:p5", "label": "alpha", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-other"}})
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopFailed("alpha", "alpha: several tabs match"))
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_resume_leaves_a_live_session_on_its_account_without_starting(self) -> None:
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        self.live.return_value = sessions.LiveSessionProcess(123)
        for target in (None, "claude4"):
            self.assertEqual(stop_resume.resume("alpha", why="maintenance", to=target),
                             stop_resume.ClaudeUp(self.entry["session_id"], "w1:p2", "claude3"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_resume_does_not_type_into_a_pane_with_other_work(self) -> None:
        with patch.object(self.client, "pane_activity", return_value=herdr.PaneRunning(("other work",))):
            result = stop_resume.resume("alpha")
        self.assertEqual(result, stop_resume.ResumeFailed(
            self.entry["session_id"], f"{self.entry['session_id']}: target pane has live work"))
        self.start.assert_not_called()
        self.assertEqual(self.client.prompts, [])

    def test_resume_proves_the_native_id_before_submitting_the_opening_work(self) -> None:
        def observe(pane: str, kind: str, timeout: float) -> str:
            self.assertEqual((pane, kind), ("w1:p2", "claude"))
            self.assertGreater(timeout, 0)
            self.assertEqual(self.client.prompts, [])
            self.start.assert_called_once()
            arguments = self._typed(cast(tuple[str, ...], self.start.call_args.args))
            self.assertNotIn("Continue where you stopped", " ".join(arguments))
            self.assertNotIn("STAFF_ID=", " ".join(arguments))
            return self.entry["session_id"]

        with patch.object(self.client, "native_session", side_effect=observe):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        assert isinstance(opening.transcript, herdr.SessionTranscript)
        self.assertEqual(opening.transcript.session_id, self.entry["session_id"])
        self.assertIn("maintenance", opening.text)
        self.assertIn("Continue where you stopped", opening.text)

    def test_resume_identity_mismatch_or_missing_proof_dispatches_no_work(self) -> None:
        for proof in ("019b3a40-4250-7000-8000-000000000199", herdr.HerdrFailure("registry unavailable")):
            with self.subTest(proof=proof):
                self.launched.clear()
                self.client.prompts.clear()
                self.start.reset_mock()
                errors = io.StringIO()
                identity = staff.StaffId(self.entry["session_id"])
                row = staff.StaffRow(identity, "unit", "alpha", None, current_session=identity)
                with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(self.client, "native_session", return_value=proof if isinstance(proof, str) else "",
                                   side_effect=None if isinstance(proof, str) else proof),
                      redirect_stderr(errors)):
                    result = stop_resume.resume("alpha", why="maintenance")
                self.assertIsInstance(result, stop_resume.ClaudeUp)
                self.assertEqual(self.client.prompts, [])
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("prompt identity anomaly:", errors.getvalue())
                self.detach.assert_called()

    def test_resume_runs_account_exec_in_its_own_pane_with_one_message(self) -> None:
        self.entry["environment"].update(HERDR_PANE_ID="w9:p9", HERDR_WORKSPACE_ID="w9")
        pane_record.write("agents", self.entry)
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))
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
        self.assertEqual(command[index + 1:], ("claude3", "--resume", "019b3a40-4250-7000-8000-000000000102", "-n", "alpha",
            "--remote-control", "alpha", "--model", "sonnet", "--settings", '{"disableAgentView": true}'))
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        self.assertIn("stopped and resumed", opening.text)
        self.assertIn("maintenance", opening.text)
        self.assertIn("Continue where you stopped", opening.text)
        self.assertIn("Codex", opening.text)
        self.assertEqual(self.client.closed, [])
        self.assertEqual(self.client.started, [])

    def test_resume_returns_before_the_stay_window_and_detaches_its_generation(self) -> None:
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertLess(self.clock.elapsed, stop_resume.STAY_SECONDS)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        arguments = cast(list[str], self.detach.call_args.args[0])
        self.assertEqual(arguments[:3], ["stay", "--session", "019b3a40-4250-7000-8000-000000000102"])
        self.assertEqual(arguments[arguments.index("--pid") + 1], "456")
        self.assertIn("generation-1", arguments)
        self.assertEqual(arguments[arguments.index("--why") + 1], "maintenance")
        self.start.assert_called_once()

    def run_stay_child(self, *, requester: str = "", why: str = "usage limit") -> tuple[int, str]:
        def exits_soon(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(456) if self.clock.elapsed < 0.6 else sessions.SessionNotLive()
        self.process.side_effect = exits_soon
        log = self.root / "stay.log"
        result = stop_resume.main(["stay", "--session", "019b3a40-4250-7000-8000-000000000102", "--pane", "w1:p2",
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
        self.assertEqual(line, "019b3a40-4250-7000-8000-000000000102 stopped 0.6 s after its move or resume to claude3; resume it: "
                         + "cd ~/.claude && python3 scripts/shutdown/shutdown.py resume 019b3a40-4250-7000-8000-000000000102 --to claude3 --why 'usage limit'")
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
        with (patch.object(staff, "runtime_id", return_value="019b3a40-4250-7000-8000-000000000201"),
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
            lookup.assert_called_once_with("019b3a40-4250-7000-8000-000000000201")
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
                self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"),
                                 stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))

    def test_saved_entry_never_selects_a_different_live_conversation(self) -> None:
        self.client.rows[0]["agent_session"] = {"kind": "id", "value": "another-live-session"}
        self.live.return_value = sessions.LiveSessionProcess(789)
        self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"), stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "unknown name"))
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.SessionNotFound("019b3a40-4250-7000-8000-000000000102"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_saved_entries_for_closed_panes_do_not_create_ambiguous_matches(self) -> None:
        missing = self.entry.copy()
        missing["pane_id"] = "w1:p-missing"
        pane_record.write("agents", missing)
        self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))

    def test_move_stops_then_runs_the_target_account_in_the_same_pane(self) -> None:
        identity = "019b3a40-4250-7000-8000-000000000102"
        self.entry["environment"]["STAFF_ID"] = identity
        pane_record.write("agents", self.entry)
        tab = dict(self.client.rows[0])
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        result = stop_resume.move("alpha", "claude4", why="weekly limit")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[command.index("exec") + 1], "claude4")
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        self.assertIn("moved from claude3 to claude4", opening.text)
        self.assertIn("weekly limit", opening.text)
        self.assertNotIn("the herdr server restarted", opening.text)
        self.assertNotIn(f"STAFF_ID={identity}", command)
        self.assertIn(identity, command)
        environment = dict(value.split("=", 1) for value in command[command.index("env") + 1:command.index("exec")] if "=" in value)
        refresh = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", str(self.root),
                                     environment, ["claude", "--resume", "019b3a40-4250-7000-8000-000000000102"])
        pane_record.write("agents", refresh)
        self.assertEqual(pane_record.read("agents")["w1:p2"]["session_id"], identity)
        self.assertNotIn("STAFF_ID", pane_record.read("agents")["w1:p2"]["environment"])
        self.assertEqual(self.client.rows[0], tab)
        self.assertEqual(self.client.closed, [])
        self.assertEqual(self.client.started, [])

    def test_move_of_a_stopped_session_resumes_but_a_failed_stop_does_not(self) -> None:
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.start.reset_mock()
        self.launched.clear()
        self.live.return_value = sessions.SessionLivenessUnknown("identity unreadable")
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.StopFailed("019b3a40-4250-7000-8000-000000000102", "identity unreadable"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_a_failed_pane_run_never_reports_resume_success(self) -> None:
        self.start.side_effect = herdr.HerdrFailure("pane run refused")
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "pane run refused"))
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
            self.assertCountEqual(stopped, ["019b3a40-4250-7000-8000-000000000102", "sid-beta", "sid-gamma"])
            _ = both_resuming.wait()
            return ""
        self.start.side_effect = run
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            results = stop_resume.move_sessions(["019b3a40-4250-7000-8000-000000000102", "sid-beta", "sid-gamma"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"),
                                   stop_resume.ClaudeUp("sid-beta", "w1:p4", "claude4"),
                                   stop_resume.ClaudeUp("sid-gamma", "w1:p6", "claude4")])
        calls = [self._typed(cast(tuple[str, ...], invocation.args)) for invocation in self.start.call_args_list]
        self.assertEqual({args[2]: args[args.index("--resume") + 1] for args in calls},
                         {"w1:p2": "019b3a40-4250-7000-8000-000000000102", "w1:p4": "sid-beta", "w1:p6": "sid-gamma"})

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
            stop_resume.ResumePending("resume-error", "claude4", "cannot run"),
            stop_resume.SessionNotFound("gone-tab"), stop_resume.ClaudeUp("good", "own-pane", "claude4")])

    def test_cli_prints_one_line_and_uses_the_result_exit_status(self) -> None:
        for arguments, expected in ((["stop", "alpha"], 0), (["resume", "alpha", "--why", "maintenance"], 0),
                                    (["stop", "elsewhere"], 2)):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(arguments), expected)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.launched.clear()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["resume", "alpha", "--why", "maintenance"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)


if __name__ == "__main__":
    _ = unittest.main()
