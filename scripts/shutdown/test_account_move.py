"""Moving one local conversation refuses before any mutation and preserves its identity."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import inventory
import record
import restart
import sessions
import settle
import account
from scripts.accounts import accounts
from test_restart import entry, seat, showrunner, top_level, tmux_host, unit
from test_support import ShutdownTestCase


class AccountMoveTests(ShutdownTestCase):
    root: Path = Path()
    owner: inventory.Session = top_level("move-id", name="kept name")

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notes = self.root / "notes"
        notes.mkdir()
        context = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(notes),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "registry"),
        })))
        del context
        (self.root / ".claude").mkdir()
        _ = (self.root / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "default@example.invalid"},
        }), encoding="utf-8")
        self.note("claude 2", "default@example.invalid")
        self.named_account("claude 1", "current@example.invalid")
        self.named_account("claude 3", "target@example.invalid")
        self.owner = top_level("move-id", name="kept name", host=tmux_host("kept tmux"))
        self.owner["config_dir"] = str(accounts.account_dir("claude 1"))
        _ = self.enterContext(patch.object(sessions, "live_sessions", side_effect=self.live_registry))
        _ = self.enterContext(patch.object(account, "process_config_dir", return_value=accounts.account_dir("claude 1")))
        _ = self.enterContext(patch.object(inventory, "inventory", side_effect=self.current_inventory))

    def note(self, label: str, login: str) -> None:
        _ = (self.root / "notes" / f"{label}.md").write_text(
            f"---\nlogin: {login}\n---\n", encoding="utf-8")

    def named_account(self, label: str, login: str) -> None:
        self.note(label, login)
        self.assertEqual(accounts.setup(label), [])
        _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": login},
        }), encoding="utf-8")

    def live_registry(self) -> list[sessions.SessionRecord]:
        return [sessions.SessionRecord(pid=self.owner["pid"], sessionId=self.owner["session_id"],
            name=self.owner["name"], messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])]

    def current_inventory(self, login: str, selected: frozenset[str]) -> inventory.Inventory:
        self.assertEqual((login, selected), ("current@example.invalid", frozenset(("move-id",))))
        return inventory.Inventory(machine="natedev", login=login, label="claude 1",
            sessions=[self.owner], unattributed=[])

    def disk_state(self) -> dict[str, bytes | str]:
        return {str(path.relative_to(self.root)): str(path.readlink()) if path.is_symlink() else
                path.read_bytes() if path.is_file() else "directory"
                for path in self.root.rglob("*")}

    def cli(self, session: str = "move-id", target: str = "claude 3") -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = accounts.main(["move", session, target])
        return result, output.getvalue(), error.getvalue()

    def refuses_without_change(self, cause: str, *, session: str = "move-id", target: str = "claude 3") -> None:
        before = self.disk_state()
        environment = dict(os.environ)
        with (
            patch.object(subprocess, "run") as process,
            patch.object(os, "execvpe") as launch,
            patch.object(settle, "begin") as begin,
        ):
            result, output, error = self.cli(session, target)
        self.assertNotEqual(result, 0)
        self.assertEqual(output, "")
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn(cause, error)
        self.assertEqual(self.disk_state(), before)
        self.assertEqual(dict(os.environ), environment)
        process.assert_not_called()
        launch.assert_not_called()
        begin.assert_not_called()

    def test_move_refuses_a_session_absent_from_this_machines_live_registry_without_change(self) -> None:
        with patch.object(sessions, "live_sessions", return_value=[]):
            self.refuses_without_change("no live session", session="remote-only")

    def test_move_refuses_an_ambiguous_live_name_and_lists_both_ids_without_change(self) -> None:
        first = self.live_registry()[0]
        second = first.copy()
        second["sessionId"] = "second-id"
        second["pid"] = first["pid"] + 1
        with patch.object(sessions, "live_sessions", return_value=[first, second]):
            self.refuses_without_change("move-id, second-id", session="kept name")

    def test_move_refuses_a_seat_without_change(self) -> None:
        self.owner = seat("move-id", owner="director")
        self.refuses_without_change("seats move with their unit director")

    def test_move_refuses_the_targets_first_check_problem_without_change(self) -> None:
        with patch.object(accounts, "check", return_value=["accounts: first target problem", "second problem"]):
            self.refuses_without_change("first target problem")

    def test_move_refuses_its_current_login_without_change(self) -> None:
        self.refuses_without_change("kept name is already on claude 1", target="claude 1")

    def test_move_refuses_the_default_logins_note_label_without_change(self) -> None:
        self.refuses_without_change("claude 2 is the default account; use default", target="claude 2")

    def test_move_refuses_a_named_dir_signed_in_to_default_without_change(self) -> None:
        self.named_account("claude 4", "default@example.invalid")
        self.refuses_without_change("claude 4 is the default account; use default", target="claude 4")

    def test_move_refuses_an_open_shutdown_for_itself_without_change(self) -> None:
        saved = entry(self.owner)
        saved["login"] = "current@example.invalid"
        saved["label"] = "claude 1"
        self.assertTrue(record.create_session(saved))
        self.refuses_without_change("kept name already has a shutdown in progress")

    def test_move_by_name_starts_only_its_id_on_its_process_account(self) -> None:
        before = self.disk_state()
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as process:
            self.assertEqual(self.cli("kept name"), (0, "", ""))
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(tuple(arguments[2:]), ("down", "claude 1", "--here", "--only", "move-id",
            "--restart", "--to", "claude 3", "--why", "claude 1 is out of usage"))
        self.assertEqual(self.disk_state(), before)

    def test_move_forwards_an_explicit_reason_to_the_selected_shutdown(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as process:
            self.assertEqual(accounts.main(["move", "move-id", "claude 3", "--why", "weekly limit reached"]), 0)
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--why") + 1], "weekly limit reached")

    def test_move_defaults_its_reason_to_the_source_label_being_out_of_usage(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as process:
            self.assertEqual(self.cli(), (0, "", ""))
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--why") + 1], "claude 1 is out of usage")

    def test_move_failed_shutdown_start_returns_failure_instead_of_started(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 3,
            "nothing started\n", "shutdown: state unreadable\n")):
            outcome = accounts.move("move-id", "claude 3")
        self.assertIsInstance(outcome, accounts.MoveStartFailed)
        if not isinstance(outcome, accounts.MoveStartFailed):
            self.fail("shutdown start failed without a failed-start result")
        self.assertEqual(outcome.returncode, 3)
        self.assertEqual(outcome.stdout, "nothing started\n")
        self.assertEqual(outcome.stderr, "shutdown: state unreadable\n")

    def test_move_cli_preserves_shutdown_failed_start_exit_and_diagnostics(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 3,
            "nothing started\n", "shutdown: state unreadable\n")):
            self.assertEqual(self.cli(), (3, "nothing started\n", "shutdown: state unreadable\n"))

    def test_move_allows_another_sessions_open_shutdown_on_the_same_account(self) -> None:
        saved = entry(top_level("other-id"))
        saved["login"] = "current@example.invalid"
        saved["label"] = "claude 1"
        self.assertTrue(record.create_session(saved))
        before = self.disk_state()
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as process:
            self.assertEqual(self.cli(), (0, "", ""))
        process.assert_called_once()
        self.assertEqual(self.disk_state(), before)

    def test_move_unknown_host_prints_one_resume_line_before_stopping_and_restart_repeats_it(self) -> None:
        self.owner = top_level("move-id", name="kept name", host=inventory.UnknownHost(kind="unknown"))
        self.owner["config_dir"] = str(accounts.account_dir("claude 1"))
        output, error = io.StringIO(), io.StringIO()
        before_stop: list[str] = []

        def stop(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            before_stop.append(output.getvalue())
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.object(subprocess, "run", side_effect=stop), redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(accounts.main(["move", "move-id", "claude 3"]), 0)
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(before_stop, [output.getvalue()])
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        prefix = "run this once kept name has exited: "
        self.assertTrue(lines[0].startswith(prefix))
        command = lines[0].removeprefix(prefix)
        arguments = shlex.split(command)
        self.assertEqual(arguments[arguments.index("exec") + 1], "claude 3")
        self.assertEqual(arguments[arguments.index("--resume") + 1], "move-id")
        saved = entry(self.owner)
        saved["target"] = "claude 3"
        saved["source_label"] = "claude 1"
        saved["why"] = "claude 1 is out of usage"
        restarted = io.StringIO()
        with redirect_stdout(restarted), patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()):
            _ = restart.relaunch_session(saved, dry_run=True, desktop_state=restart.DesktopLaunchState())
        self.assertEqual(restarted.getvalue(), f"manual restart kept name: {command}\n")

    def test_unknown_host_preview_keeps_the_custom_move_reason_in_the_restart_command(self) -> None:
        self.owner = top_level("move-id", name="kept name", host=inventory.UnknownHost(kind="unknown"))
        self.owner["config_dir"] = str(accounts.account_dir("claude 1"))
        output = io.StringIO()
        with (
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")),
            redirect_stdout(output),
        ):
            self.assertEqual(accounts.main(["move", "move-id", "claude 3", "--why", "weekly limit reached"]), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        command = lines[0].removeprefix("run this once kept name has exited: ")
        self.assertEqual(command, restart.resume_command(self.owner, "claude 3", "claude 1", "weekly limit reached"))
        arguments = shlex.split(command.removesuffix("; exec zsh"))
        self.assertIn("moved from claude 1 to claude 3 because weekly limit reached", arguments[-1])
        self.assertIn("account move, not a stop", arguments[-1])

    def test_retired_unit_swap_refuses_before_role_history_shutdown_or_process_changes(self) -> None:
        configuration = accounts.accounts_config_file()
        configuration.parent.mkdir(parents=True, exist_ok=True)
        _ = configuration.write_text(json.dumps(accounts.AccountsConfiguration(unit="claude 1")), encoding="utf-8")
        saved = entry(self.owner)
        saved["login"] = "current@example.invalid"
        saved["label"] = "claude 1"
        self.assertTrue(record.create_session(saved))
        before = self.disk_state()
        output, error = io.StringIO(), io.StringIO()
        with (
            patch.object(subprocess, "run") as process,
            patch.object(os, "execvpe") as launch,
            patch.object(settle, "begin") as begin,
            redirect_stdout(output), redirect_stderr(error),
        ):
            self.assertEqual(accounts.main(["swap", "unit", "claude 3"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(),
            "the unit role is gone: new sessions pick the account with room "
            + "(accounts.py pick claude); move one session with accounts.py move\n")
        process.assert_not_called()
        launch.assert_not_called()
        begin.assert_not_called()
        self.assertEqual(self.disk_state(), before)

    def showrunner_inventory(self) -> inventory.Inventory:
        first = showrunner("sr-one", host=tmux_host("first-pane"))
        first["config_dir"] = str(accounts.account_dir("claude 1"))
        first["name"] = "first showrunner"
        first["cwd"] = "/tmp/first workspace"
        first["doc"] = "/tmp/first production.md"
        first["model"] = inventory.ModelName(kind="model", name="opus")
        second = showrunner("sr-two", host=tmux_host("second-pane"))
        second["config_dir"] = str(accounts.account_dir("claude 1"))
        second["name"] = "second showrunner"
        second["cwd"] = "/tmp/second workspace"
        second["doc"] = "/tmp/second production.md"
        second["model"] = inventory.ModelName(kind="model", name="sonnet")
        return inventory.Inventory(machine="natedev", login="current@example.invalid", label="claude 1",
            sessions=[first, unit("unit-id"), second, top_level("ordinary-id")], unattributed=[])

    def batch_registry(self, report: inventory.Inventory) -> list[sessions.SessionRecord]:
        return [sessions.SessionRecord(pid=owner["pid"], sessionId=owner["session_id"],
            name=owner["name"], messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
            for owner in report["sessions"]]

    def start_batch(self, command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        selected = command[command.index("--only") + 1].split(",")
        report = self.showrunner_inventory()
        for owner in report["sessions"]:
            if owner["session_id"] not in selected:
                continue
            saved = entry(owner)
            saved["login"] = report["login"]
            saved["label"] = report["label"]
            saved["source_label"] = report["label"]
            saved["target"] = command[command.index("--to") + 1]
            saved["why"] = command[command.index("--why") + 1]
            saved["progress"] = record.SessionWaiting(kind="waiting")
            self.assertTrue(record.create_session(saved))
        return subprocess.CompletedProcess(command, 0, "", "")

    def batch_cli(self, *, target: str = "claude 3", dry_run: bool = False) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        arguments = ["move", "showrunners", "--from", "claude 1", "--to", target]
        if dry_run:
            arguments.append("--dry-run")
        with redirect_stdout(output), redirect_stderr(error):
            result = accounts.main(arguments)
        return result, output.getvalue(), error.getvalue()

    def batch_refuses_without_change(self, cause: str, *, target: str = "claude 3") -> None:
        before = self.disk_state()
        with (
            patch.object(inventory, "inventory", return_value=self.showrunner_inventory()),
            patch.object(subprocess, "run") as process,
            patch.object(os, "execvpe") as launch,
        ):
            result, output, error = self.batch_cli(target=target)
        self.assertNotEqual(result, 0)
        self.assertEqual(output, "")
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn(cause, error)
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()
        launch.assert_not_called()

    def test_bulk_move_selects_only_source_accounts_showrunners_and_uses_one_local_down(self) -> None:
        report = self.showrunner_inventory()
        with (
            patch.object(inventory, "inventory", return_value=report) as read_inventory,
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", side_effect=self.start_batch) as process,
        ):
            result, _, error = self.batch_cli()
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(read_inventory.call_args.args[0], "current@example.invalid")
        process.assert_called_once()
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[2:4], ("down", "claude 1"))
        self.assertIn("--here", arguments)
        self.assertIn("--restart", arguments)
        self.assertEqual(arguments[arguments.index("--to") + 1], "claude 3")
        self.assertEqual(set(arguments[arguments.index("--only") + 1].split(",")), {"sr-one", "sr-two"})

    def test_bulk_move_forwards_a_single_explicit_reason_for_all_selected_showrunners(self) -> None:
        report = self.showrunner_inventory()
        with (
            patch.object(inventory, "inventory", return_value=report),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", side_effect=self.start_batch) as process,
        ):
            self.assertEqual(accounts.main(["move", "showrunners", "--from", "claude 1", "--to", "claude 3",
                "--why", "weekly limit reached"]), 0)
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--why") + 1], "weekly limit reached")
        process.assert_called_once()

    def test_bulk_move_failed_shutdown_start_returns_failure_instead_of_started(self) -> None:
        report = self.showrunner_inventory()
        with (
            patch.object(inventory, "inventory", return_value=report),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 2,
                "", "shutdown: keys are cold\n")),
        ):
            outcome = accounts.move_showrunners("claude 1", "claude 3")
        self.assertIsInstance(outcome, accounts.ShowrunnersMoveStartFailed)
        if not isinstance(outcome, accounts.ShowrunnersMoveStartFailed):
            self.fail("bulk shutdown start failed without a failed-start result")
        self.assertEqual(outcome.returncode, 2)
        self.assertEqual(outcome.stderr, "shutdown: keys are cold\n")

    def test_bulk_move_refuses_no_local_showrunners_on_source_without_change(self) -> None:
        report = self.showrunner_inventory()
        report["sessions"] = [unit("unit-id"), top_level("ordinary-id")]
        before = self.disk_state()
        with patch.object(inventory, "inventory", return_value=report), patch.object(subprocess, "run") as process:
            result, output, error = self.batch_cli()
        self.assertNotEqual(result, 0)
        self.assertEqual(output, "")
        self.assertEqual(error, "no showrunner on claude 1 on this machine\n")
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()

    def test_bulk_move_refuses_first_target_check_problem_for_every_showrunner_without_change(self) -> None:
        with patch.object(accounts, "check", return_value=["accounts: target problem", "later problem"]):
            self.batch_refuses_without_change("target problem")

    def test_bulk_move_refuses_default_login_note_label_for_every_showrunner_without_change(self) -> None:
        self.batch_refuses_without_change("claude 2 is the default account; use default", target="claude 2")

    def test_bulk_move_refuses_default_login_named_dir_for_every_showrunner_without_change(self) -> None:
        self.named_account("claude 4", "default@example.invalid")
        self.batch_refuses_without_change("claude 4 is the default account; use default", target="claude 4")

    def test_bulk_move_refuses_its_source_login_as_target_without_change(self) -> None:
        self.batch_refuses_without_change("already", target="claude 1")

    def test_bulk_move_skips_open_shutdown_showrunner_and_moves_the_rest(self) -> None:
        report = self.showrunner_inventory()
        saved = entry(report["sessions"][0])
        saved["login"] = "current@example.invalid"
        saved["label"] = "claude 1"
        self.assertTrue(record.create_session(saved))
        before = self.disk_state()
        with (
            patch.object(inventory, "inventory", return_value=report),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", side_effect=self.start_batch) as process,
        ):
            result, output, error = self.batch_cli()
        self.assertEqual(result, 0)
        self.assertIn("first showrunner already has a shutdown in progress", output + error)
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--only") + 1], "sr-two")
        self.assertEqual(record.find_session("current@example.invalid", "sr-one"), saved)
        added = record.find_session("current@example.invalid", "sr-two")
        assert added is not None and "target" in added
        self.assertEqual(added["target"], "claude 3")
        self.assertNotEqual(self.disk_state(), before)

    def test_bulk_move_dry_run_prints_selection_down_and_each_exact_resume_without_change(self) -> None:
        report = self.showrunner_inventory()
        before = self.disk_state()
        with patch.object(inventory, "inventory", return_value=report), patch.object(subprocess, "run") as process:
            result, output, error = self.batch_cli(dry_run=True)
        self.assertEqual((result, error), (0, ""))
        self.assertIn("first showrunner", output)
        self.assertIn("second showrunner", output)
        lines = output.splitlines()
        down = next(shlex.split(line) for line in lines if "shutdown.py" in line and "--only" in line)
        self.assertEqual(down[down.index("--to") + 1], "claude 3")
        self.assertEqual(set(down[down.index("--only") + 1].split(",")), {"sr-one", "sr-two"})
        self.assertIn("--here", down)
        resumes = [line for line in lines if "--resume" in line]
        self.assertEqual(len(resumes), 2)
        for owner in (report["sessions"][0], report["sessions"][2]):
            self.assertIn(restart.resume_command(owner, "claude 3"), output)
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()

    def test_bulk_move_to_default_preserves_default_target_in_the_single_down(self) -> None:
        report = self.showrunner_inventory()
        with (
            patch.object(inventory, "inventory", return_value=report),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", side_effect=self.start_batch) as process,
        ):
            result, _, error = self.batch_cli(target="default")
        self.assertEqual((result, error), (0, ""))
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--to") + 1], "default")

    def test_bulk_move_with_every_showrunner_already_shutting_down_starts_no_new_shutdown(self) -> None:
        report = self.showrunner_inventory()
        for owner in (report["sessions"][0], report["sessions"][2]):
            saved = entry(owner)
            saved["login"] = "current@example.invalid"
            saved["label"] = "claude 1"
            self.assertTrue(record.create_session(saved))
        before = self.disk_state()
        with patch.object(inventory, "inventory", return_value=report), patch.object(subprocess, "run") as process:
            _, output, error = self.batch_cli()
        self.assertIn("first showrunner already has a shutdown in progress", output + error)
        self.assertIn("second showrunner already has a shutdown in progress", output + error)
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()
