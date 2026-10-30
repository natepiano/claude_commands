"""Moving one local conversation refuses before any mutation and preserves its identity."""

from __future__ import annotations

from scripts.production import staff_sources
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.whoami.note_file import note_name

import inventory
import shutdown_record
import sessions
import settle
import shutdown
import showrunners
import stop as stop_work
import restart
import launch_permission
from importlib import import_module
import account
import agent_accounts
from scripts.accounts import accounts, refusals
from test_restart import RESTARTED, STOPPED, entry, worker_session, showrunner, top_level, unit
from test_support import ShutdownTestCase

# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300


class AccountMoveTests(ShutdownTestCase):
    root: Path = Path()
    owner: inventory.Session = top_level("move-id", name="kept name")

    def test_move_opening_note_dates_the_pause_and_resume_even_when_delivered_late(self) -> None:
        saved = entry(top_level("dated-move"))
        saved.update(target="claude3", source_label="claude1", why="weekly allowance exhausted")
        note = restart.restart_note(saved, RESTARTED)
        paused = restart._user_time(STOPPED)  # pyright: ignore[reportPrivateUsage]
        resumed = restart._user_time(RESTARTED)  # pyright: ignore[reportPrivateUsage]
        self.assertIn(paused, note)
        self.assertIn(resumed, note)
        self.assertLess(note.index(paused), note.index(resumed))
        self.assertIn("claude1", note)
        self.assertIn("claude3", note)
        self.assertIn(saved["where"]["text"] if saved["where"]["kind"] == "said" else "", note)

    def test_move_opening_note_points_to_resume_message_only_when_workers_were_paused(self) -> None:
        director = unit("moved-director")
        saved = entry(director)
        saved["target"] = "claude3"
        saved["source_label"] = "claude1"
        sentence = "Your Codex workers are paused, not ended; a message lists the command that resumes each."
        self.assertNotIn(sentence, restart.restart_note(saved, saved["requested_at"]))
        saved["codex_seats"] = [shutdown_record.PausedSeat(run_dir="/run", seat="impl", role="impl", thread_id="private-thread", turn_live=True, launcher_command=[])]
        note = restart.restart_note(saved, saved["requested_at"])
        self.assertEqual(note.count(sentence), 1)
        self.assertIn("message/send.py pending", note)
        self.assertIn("Continue your /unit:direct run", note)
        self.assertNotIn("private-thread", note)

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notes = self.root / "notes"
        notes.mkdir()
        context = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(notes),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "registry"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
        })))
        del context
        _ = self.enterContext(patch.object(import_module("showrunners"), "NOTIFIER_STATE_DIR", self.root / "notifier"))
        _ = self.enterContext(patch.object(import_module("scripts.production.showrunners"), "NOTIFIER_STATE_DIR", self.root / "notifier"))
        _ = self.enterContext(patch.object(agent_accounts, "user_zone", return_value=timezone.utc))
        _ = self.enterContext(patch.object(accounts, "follow_move", side_effect=AssertionError("unfaked move follow")))
        (self.root / ".claude").mkdir()
        _ = (self.root / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "default@example.invalid"},
        }), encoding="utf-8")
        self.note("claude2", "default@example.invalid")
        self.named_account("claude2", "default@example.invalid")
        self.named_account("claude1", "current@example.invalid")
        self.named_account("claude3", "target@example.invalid")
        self.owner = top_level("move-id", name="kept name", host=inventory.HerdrHost(kind="herdr", herdr_session="kept-host", pane_id="w1:p1", presentation="detached"))
        self.owner["config_dir"] = str(accounts.account_dir("claude1"))
        _ = self.enterContext(patch.object(sessions, "live_sessions", side_effect=self.live_registry))
        _ = self.enterContext(patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(accounts.account_dir("claude1"))))
        _ = self.enterContext(patch.object(inventory, "inventory", side_effect=AssertionError("accounts must use the listing")))
        _ = self.enterContext(patch.object(inventory, "listing", side_effect=self.current_listing))
        at = datetime.now(timezone.utc)
        _ = self.enterContext(patch.object(refusals, "stored_reading", return_value=refusals.FreshReading(
            refusals.ReadingWindow(80, refusals.NoResetKnown(), at), refusals.NoWindow(), "")))
        _ = self.enterContext(patch.object(settle, "prompt_state", return_value=settle.TakesPrompts("room")))

    def test_target_shutdown_hold_refuses_group_and_single_move_before_any_write(self) -> None:
        reason = "claude3 is held by a shutdown (since 10-18), requested by session b1f5ca98"
        for permission in (launch_permission.BlockedByShutdown(reason),
                           launch_permission.ShutdownStateUnreadable("bad shutdown state")):
            for subject in ("units", "move-id"):
                with self.subTest(permission=permission, subject=subject):
                    before = self.disk_state()
                    with (patch.object(launch_permission, "launch_permission", return_value=permission),
                          patch.object(accounts, "move_sessions") as move,
                          patch.object(accounts, "set_group_account") as select):
                        result, output, error = self.cli(subject)
                    self.assertEqual(result, 1)
                    self.assertEqual(output, "")
                    self.assertIn("no session was stopped", error)
                    move.assert_not_called()
                    select.assert_not_called()
                    self.assertEqual(before, self.disk_state())

    def test_released_target_hold_is_named_and_dry_run_does_not_call_the_check(self) -> None:
        for session_id, requester in (
            ("b1f5ca98-rest", "session b1f5ca98"),
            (None, "requested in a terminal or by a recovery snapshot"),
        ):
            with self.subTest(session_id=session_id):
                hold = launch_permission.ReleasedHold(
                    label="claude3", then="stop", requested_at="2026-10-18T12:00:00+00:00",
                    session_id=session_id)
                with (patch.object(launch_permission, "launch_permission",
                                   return_value=launch_permission.AllowedByShutdownState(hold)),
                      patch.object(accounts, "move_sessions", return_value=()),
                      patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": []})):
                    outcome = accounts.move_group("units", "claude3")
                self.assertIsInstance(outcome, accounts.GroupMoveStarted)
                assert isinstance(outcome, accounts.GroupMoveStarted)
                self.assertIn(
                    f"cleared a finished stop hold on claude3 from 2026-10-18 12:00 UTC ({requester})",
                    outcome.stdout)
        with patch.object(launch_permission, "launch_permission") as check:
            result = accounts.check_move_target("claude3", datetime.now(timezone.utc), repair=False)
        check.assert_not_called()
        assert isinstance(result, accounts.MoveTargetReady)
        self.assertIn("would check the shutdown hold", result.notices[0])

    def listed(self, report: inventory.Inventory) -> list[inventory.ListedSession]:
        return [inventory.ListedSession(session_id=item["session_id"], pid=item["pid"],
            proc_start=item["proc_start"], name=item["name"], kind=item["kind"],
            config_dir=Path(item.get("config_dir", str(accounts.account_dir("claude1")))), login=report["login"],
            production=item["production"] if item["kind"] == "showrunner" else
                staff_sources.unit_parent(item["host"]["unit"]).slug if item["host"]["kind"] == "unit" else "",
            doc=item["doc"] if item["kind"] == "showrunner" else
                staff_sources.unit_parent(item["host"]["unit"]).doc if item["host"]["kind"] == "unit" else "")
            for item in report["sessions"]]

    def current_listing(self, login: str | None = None, **options: object) -> list[inventory.ListedSession]:
        del login, options
        report = inventory.Inventory(machine="natedev", login="current@example.invalid", label="claude1",
            sessions=[self.owner], unattributed=[])
        owner = self.listed(report)[0]
        return [inventory.ListedSession(item["sessionId"], item["pid"], owner.proc_start,
            item["name"], owner.kind, owner.config_dir, owner.login, owner.production, owner.doc)
            for item in sessions.live_sessions()]

    def note(self, label: str, login: str) -> None:
        _ = (self.root / "notes" / note_name(label)).write_text(
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

    def disk_state(self) -> dict[str, bytes | str]:
        return {str(path.relative_to(self.root)): str(path.readlink()) if path.is_symlink() else
                path.read_bytes() if path.is_file() else "directory"
                for path in self.root.rglob("*")}

    def cli(self, session: str = "move-id", target: str = "claude3") -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = accounts.main(["move", session, target])
        return result, output.getvalue(), error.getvalue()

    def refuses_without_change(self, cause: str, *, session: str = "move-id", target: str = "claude3") -> None:
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

    def test_group_preview_starts_in_a_fresh_process_without_prior_shutdown_imports(self) -> None:
        environment = {
            "HOME": str(self.root),
            "PATH": os.defpath,
            "AGENT_NOTES_DIR": str(self.root / "notes"),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "registry"),
            "USER_ZONE": "UTC",
        }
        before = self.disk_state()
        result = subprocess.run(
            [sys.executable, str(accounts.SCRIPTS / "accounts" / "accounts.py"),
             "move", "showrunners", "claude3", "--dry-run"],
            env=environment, capture_output=True, text=True, check=False, timeout=HANG_GUARD_SECONDS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("showrunners: would select claude3", result.stdout)
        self.assertIn("showrunners: no live session moves", result.stdout)
        self.assertEqual(self.disk_state(), before)

    def test_move_refuses_an_ambiguous_live_name_and_lists_both_ids_without_change(self) -> None:
        first = self.live_registry()[0]
        second = first.copy()
        second["sessionId"] = "second-id"
        second["pid"] = first["pid"] + 1
        with patch.object(sessions, "live_sessions", return_value=[first, second]):
            self.refuses_without_change("move-id, second-id", session="kept name")

    def test_move_refuses_a_worker_without_change(self) -> None:
        self.owner = worker_session("move-id", owner="director")
        self.refuses_without_change("workers move with their unit director")

    def test_move_refuses_the_targets_first_preparation_problem_without_change(self) -> None:
        with patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused(
            "accounts: first target problem\naccounts: second target problem"
        )):
            self.refuses_without_change("first target problem")

    def test_move_refuses_its_current_login_without_change(self) -> None:
        self.refuses_without_change("kept name is already on claude1", target="claude1")

    def start_one(self, command: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
        saved = entry(self.owner)
        saved["login"] = "current@example.invalid"
        saved["label"] = "claude1"
        saved["source_label"] = "claude1"
        saved["target"] = command[command.index("--to") + 1]
        saved["why"] = command[command.index("--why") + 1]
        saved["progress"] = shutdown_record.SessionWaiting(kind="waiting")
        self.assertTrue(shutdown_record.create_session(saved))
        return subprocess.CompletedProcess(command, 0, "", "")

    def assert_move_to_the_home_number(self, target: str) -> None:
        with patch.object(subprocess, "run", side_effect=self.start_one) as process:
            result, output, error = self.cli(target=target)
            self.assertEqual((result, error), (0, ""))
            self.assertIn("to move them back, in this terminal:", output)
        process.assert_called_once()
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--to") + 1], target)
        self.assertEqual(accounts.claude_account_dir(arguments[arguments.index("--to") + 1]), self.root / f".{target}")

    def test_move_to_claude2_uses_its_numbered_folder(self) -> None:
        self.assert_move_to_the_home_number("claude2")

    def test_move_to_a_named_directory_sharing_the_legacy_login_keeps_the_numbered_folder(self) -> None:
        self.named_account("claude4", "default@example.invalid")
        self.assert_move_to_the_home_number("claude4")

    def test_move_refuses_an_open_shutdown_for_itself_without_change(self) -> None:
        saved = entry(self.owner)
        saved["login"] = "current@example.invalid"
        saved["label"] = "claude1"
        self.assertTrue(shutdown_record.create_session(saved))
        self.refuses_without_change("kept name already has a shutdown in progress")

    def test_move_by_name_starts_only_its_id_on_its_process_account(self) -> None:
        before = self.disk_state()
        with patch.object(subprocess, "run", side_effect=self.start_one) as process:
            result, output, error = self.cli("kept name")
            self.assertEqual((result, error), (0, ""))
            self.assertIn("accounts.py move move-id claude1", output)
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(tuple(arguments[2:12]), ("down", "claude1", "--here", "--only", "move-id",
            "--restart", "--to", "claude3", "--why", "claude1 is out of usage"))
        self.assertEqual(arguments[12], "--move-id")
        saved = shutdown_record.find_session("current@example.invalid", "move-id")
        self.assertIsNotNone(saved)
        self.assertNotEqual(self.disk_state(), before)

    def test_move_forwards_an_explicit_reason_to_the_selected_shutdown(self) -> None:
        with patch.object(subprocess, "run", side_effect=self.start_one) as process:
            self.assertEqual(accounts.main(["move", "move-id", "claude3", "--why", "weekly limit reached"]), 0)
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--why") + 1], "weekly limit reached")

    def test_single_move_forwards_no_alert_only_when_requested(self) -> None:
        for flags in ((), ("--no-alert",)):
            with (
                self.subTest(flags=flags),
                patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / ("quiet" if flags else "alert"))}),
                patch.object(subprocess, "run", side_effect=self.start_one) as process,
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(accounts.main(["move", "move-id", "claude3", *flags]), 0)
                process.assert_called_once()
                command = cast(tuple[str, ...], process.call_args.args[0])
                self.assertEqual(command[2:4], ("down", "claude1"))
                self.assertEqual(command[command.index("--only") + 1], "move-id")
                self.assertEqual(command.count("--no-alert"), len(flags))

    def alert_move_members(self) -> list[inventory.ListedSession]:
        report = self.showrunner_inventory()
        first, second = report["sessions"][0], report["sessions"][2]
        second["config_dir"] = str(self.root / ".claude")
        return [*self.listed(inventory.Inventory(machine=report["machine"], login=report["login"],
                    label=report["label"], sessions=[first], unattributed=[])),
                *self.listed(inventory.Inventory(machine=report["machine"], login="default@example.invalid",
                    label="claude2", sessions=[second], unattributed=[]))]

    def test_group_move_forwards_no_alert_to_every_source_only_when_requested(self) -> None:
        members = self.alert_move_members()

        def start(command: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            selected = command[command.index("--only") + 1].split(",")
            for member in members:
                if member.session_id in selected:
                    saved = entry(showrunner(member.session_id))
                    saved["login"], saved["label"] = member.login, command[3]
                    saved["target"] = command[command.index("--to") + 1]
                    self.assertTrue(shutdown_record.create_session(saved))
            return subprocess.CompletedProcess(command, 0, "", "")

        for flags in ((), ("--no-alert",)):
            with (
                self.subTest(flags=flags),
                patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / ("quiet" if flags else "alert"))}),
                patch.object(inventory, "listing", return_value=members),
                patch.object(subprocess, "run", side_effect=start) as process,
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(accounts.main(["move", "showrunners", "claude3", *flags]), 0)
                commands = [cast(tuple[str, ...], call.args[0]) for call in process.call_args_list]
                self.assertEqual(len(commands), 2)
                self.assertEqual({command[3] for command in commands}, {"claude1", "claude2"})
                self.assertEqual({command[command.index("--only") + 1] for command in commands},
                                 {"sr-one", "sr-two"})
                for command in commands:
                    self.assertEqual(command[2], "down")
                    self.assertEqual(command.count("--no-alert"), len(flags))

    def test_move_dry_run_preserves_no_alert_in_each_planned_command_without_writes(self) -> None:
        members = [self.listed(inventory.Inventory(machine="natedev", login="current@example.invalid",
                    label="claude1", sessions=[self.owner], unattributed=[]))[0], *self.alert_move_members()]
        for subject, expected_sources in (("move-id", {"claude1"}), ("showrunners", {"claude1", "claude2"})):
            for flags in ((), ("--no-alert",)):
                with self.subTest(subject=subject, flags=flags):
                    before = self.disk_state()
                    output, error = io.StringIO(), io.StringIO()
                    with (
                        patch.object(inventory, "listing", return_value=members),
                        patch.object(subprocess, "run") as process,
                        redirect_stdout(output), redirect_stderr(error),
                    ):
                        result = accounts.main(["move", subject, "claude3", "--dry-run", *flags])
                    self.assertEqual((result, error.getvalue()), (0, ""))
                    commands = [shlex.split(line)
                                for line in output.getvalue().splitlines() if "shutdown.py down " in line]
                    if flags:
                        self.assertEqual(len(commands), len(expected_sources), output.getvalue())
                        self.assertEqual({command[3] for command in commands}, expected_sources)
                        selected: set[str] = set()
                        for command in commands:
                            self.assertEqual(command.count("--no-alert"), 1)
                            self.assertIn("--here", command)
                            self.assertIn("--restart", command)
                            self.assertEqual(command[command.index("--to") + 1], "claude3")
                            selected.update(command[command.index("--only") + 1].split(","))
                        self.assertEqual(selected, {"move-id"} if subject == "move-id" else {"sr-one", "sr-two"})
                    else:
                        self.assertNotIn("--no-alert", output.getvalue())
                    self.assertEqual(self.disk_state(), before)
                    process.assert_not_called()

    def test_move_defaults_its_reason_to_the_source_label_being_out_of_usage(self) -> None:
        with patch.object(subprocess, "run", side_effect=self.start_one) as process:
            result, output, error = self.cli()
            self.assertEqual((result, error), (0, ""))
            self.assertIn("to move them back", output)
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--why") + 1], "claude1 is out of usage")

    def test_move_failed_shutdown_start_returns_failure_instead_of_started(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 3,
            "nothing started\n", "shutdown: state unreadable\n")):
            outcome = accounts.move("move-id", "claude3")
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
        saved["label"] = "claude1"
        self.assertTrue(shutdown_record.create_session(saved))
        before = self.disk_state()
        with patch.object(subprocess, "run", side_effect=self.start_one) as process:
            result, output, error = self.cli()
            self.assertEqual((result, error), (0, ""))
            self.assertIn("to move them back", output)
        process.assert_called_once()
        self.assertEqual(shutdown_record.find_session("current@example.invalid", "other-id"), saved)
        self.assertNotEqual(self.disk_state(), before)

    def test_retired_unit_swap_refuses_before_role_history_shutdown_or_process_changes(self) -> None:
        configuration = accounts.accounts_config_file()
        configuration.parent.mkdir(parents=True, exist_ok=True)
        _ = configuration.write_text(json.dumps(accounts.AccountsConfiguration(unit="claude1")), encoding="utf-8")
        saved = entry(self.owner)
        saved["login"] = "current@example.invalid"
        saved["label"] = "claude1"
        self.assertTrue(shutdown_record.create_session(saved))
        before = self.disk_state()
        output, error = io.StringIO(), io.StringIO()
        with (
            patch.object(subprocess, "run") as process,
            patch.object(os, "execvpe") as launch,
            patch.object(settle, "begin") as begin,
            redirect_stdout(output), redirect_stderr(error),
        ):
            self.assertEqual(accounts.main(["swap", "unit", "claude3"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(),
            "unit launches use the configured units account; "
            + "select and move the group with accounts.py move units <claudeN>\n")
        process.assert_not_called()
        launch.assert_not_called()
        begin.assert_not_called()
        self.assertEqual(self.disk_state(), before)

    def showrunner_inventory(self) -> inventory.Inventory:
        first = showrunner("sr-one", host=inventory.HerdrHost(kind="herdr", herdr_session="first-pane", pane_id="w1:p1", presentation="detached"))
        first["config_dir"] = str(accounts.account_dir("claude1"))
        first["name"] = "first showrunner"
        first["cwd"] = "/tmp/first workspace"
        first["doc"] = "/tmp/first production.md"
        first["model"] = inventory.ModelName(kind="model", name="opus")
        second = showrunner("sr-two", host=inventory.HerdrHost(kind="herdr", herdr_session="second-pane", pane_id="w1:p1", presentation="detached"))
        second["config_dir"] = str(accounts.account_dir("claude1"))
        second["name"] = "second showrunner"
        second["cwd"] = "/tmp/second workspace"
        second["doc"] = "/tmp/second production.md"
        second["model"] = inventory.ModelName(kind="model", name="sonnet")
        return inventory.Inventory(machine="natedev", login="current@example.invalid", label="claude1",
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
            saved["progress"] = shutdown_record.SessionWaiting(kind="waiting")
            self.assertTrue(shutdown_record.create_session(saved))
        return subprocess.CompletedProcess(command, 0, "", "")

    def batch_cli(self, *, target: str = "claude3", dry_run: bool = False) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        arguments = ["move", "showrunners", target]
        if dry_run:
            arguments.append("--dry-run")
        report = self.showrunner_inventory()
        with (patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
              patch.object(account, "account_of", return_value=account.Account("claude", report["login"], report["label"])),
              redirect_stdout(output), redirect_stderr(error)):
            result = accounts.main(arguments)
        return result, output.getvalue(), error.getvalue()

    def batch_refuses_without_change(self, cause: str, *, target: str = "claude3") -> None:
        before = self.disk_state()
        with (
            patch.object(inventory, "listing", return_value=self.listed(self.showrunner_inventory())),
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

    def test_group_move_real_shutdown_record_pauses_until_ready_and_tells_unit_showrunner(self) -> None:
        director = unit("director", doc="/tmp/demo-production.md")
        director["config_dir"] = str(accounts.account_dir("claude1"))
        director["checkout"] = inventory.NotACheckout(kind="not a checkout")
        director["status"] = "busy"
        report = inventory.Inventory(machine="natedev", login="current@example.invalid", label="claude1",
                                     sessions=[director], unattributed=[])
        commands: list[list[str]] = []
        events: list[str] = []
        runner_registry = import_module("showrunners")

        def request(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            output = io.StringIO()
            with redirect_stdout(output):
                code = shutdown.main(command[2:])
            return subprocess.CompletedProcess(command, code, output.getvalue(), "")

        def delivered(to: str, first: str, text: str, *, key: str = "") -> settle.MessageDelivery:
            _ = key
            events.append(first)
            if to == "session:director":
                self.assertTrue(first.startswith("Pause for move: claude1 → claude3 (checkpoint move, "))
                self.assertIn("shutdown.py ready --where", text)
            return {"kind": "sent"}

        def stopped(saved: shutdown_record.SessionShutdown, *, idle_reason: stop_work.IdleStopReason = "not an idle stop",
                    forced: bool = False) -> shutdown_record.SessionShutdown:
            self.assertEqual(idle_reason, "not an idle stop")
            self.assertFalse(forced)
            events.append("stop")
            self.assertEqual(saved["progress"]["kind"], "ready")
            checkpoint = saved["where"]
            self.assertEqual(checkpoint["kind"], "said")
            assert checkpoint["kind"] == "said"
            self.assertEqual(checkpoint["text"], "finish the checkpoint")
            return saved

        with (patch.object(accounts, "live_group_members", return_value={"units": self.listed(report), "showrunners": []}),
              patch.object(inventory, "listing", return_value=self.listed(report)),
              patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
              patch.object(account, "account_of", return_value=account.Account("claude", report["login"], report["label"])),
              patch.object(shutdown, "inventory", return_value=report),
              patch.object(shutdown, "listing", return_value=[inventory.ListedSession(
                  session_id=director["session_id"], pid=director["pid"],
                  proc_start=director["proc_start"], name=director["name"], kind="unit",
                  config_dir=Path(director["config_dir"]), login=report["login"],
                  production=staff_sources.unit_parent(director["host"]["unit"]).slug, doc=staff_sources.unit_parent(director["host"]["unit"]).doc,
              )]),
              patch.object(settle, "inventory", return_value=report),
              patch.object(settle, "run_inventory", return_value=report),
              patch.object(settle, "_warm", return_value=True),
              patch.object(settle, "_launch_conductor"),
              patch.object(settle, "_session_timer_snapshot", return_value=[]),
              patch.object(settle, "_showing_form", return_value=False),
              patch.object(settle, "merge_in_progress", return_value=False),
              patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "requester-outside-group"}),
              patch.object(subprocess, "run", side_effect=request),
              patch.object(settle, "_carry_trust"),
              patch.object(settle, "send_message", side_effect=delivered),
              patch.object(runner_registry, "current_name", return_value="parent-runner"),
              patch.object(stop_work, "stop_session", side_effect=stopped) as stop):
            result = accounts.move_group("units", "claude3", why="checkpoint move")
            self.assertIsInstance(result, accounts.GroupMoveStarted)
            self.assertEqual(len(commands), 1)
            self.assertEqual(tuple(commands[0][2:]), ("down", "claude1", "--here", "--only", "director",
                "--restart", "--to", "claude3", "--why", "checkpoint move", "--move-id",
                cast(accounts.GroupTransition, accounts.group_transition("units")).move_id))
            saved = shutdown_record.session_records(report["login"])[0]
            self.assertEqual(saved["force"], "wait for ready")
            observed = stop_work.fresh_session(saved["session"], report)
            self.assertIsInstance(settle.next_step(saved, observed, [saved], datetime.now(timezone.utc)),
                                  settle.SendSettleMessage)
            settle._send_settle_message(saved)  # pyright: ignore[reportPrivateUsage]
            saved = shutdown_record.session_records(report["login"])[0]
            self.assertNotIsInstance(settle.next_step(saved, observed, [saved], datetime.now(timezone.utc)),
                                     settle.StopSession)
            stop.assert_not_called()
            director["status"] = "idle"
            with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "director"}):
                self.assertEqual(shutdown.main(["ready", "--where", "finish the checkpoint"]), 0)
            saved = shutdown_record.session_records(report["login"])[0]
            self.assertIsInstance(settle.next_step(saved, observed, [saved], datetime.now(timezone.utc)),
                                  settle.StopSession)
            _ = settle._stop_after_telling(saved, settle.StopSession())  # pyright: ignore[reportPrivateUsage]
            stop.assert_called_once()
        self.assertTrue(events[1].startswith(f"{director['name']} is stopping:"), events)
        self.assertEqual(events[-1], "stop")

    def test_group_move_selects_classified_showrunners_and_uses_one_local_down(self) -> None:
        report = self.showrunner_inventory()
        with (
            patch.object(inventory, "listing", return_value=self.listed(report)) as read_inventory,
            patch.object(inventory, "inventory", side_effect=AssertionError("move read full inventory")),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", side_effect=self.start_batch) as process,
        ):
            result, output, error = self.batch_cli()
        self.assertEqual((result, error), (0, ""))
        read_inventory.assert_called()
        process.assert_called_once()
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[2:4], ("down", "claude1"))
        self.assertIn("--here", arguments)
        self.assertIn("--restart", arguments)
        self.assertNotIn("now", arguments)
        self.assertEqual(arguments[arguments.index("--to") + 1], "claude3")
        self.assertEqual(set(arguments[arguments.index("--only") + 1].split(",")), {"sr-one", "sr-two"})

        self.assertEqual([line for line in output.splitlines() if line.startswith("to move them back")],
            ["to move them back, in this terminal: ~/.claude/scripts/lib/py "
             + "~/.claude/scripts/accounts/accounts.py move showrunners claude1"])

    def mixed_source_run(self, follow_codes: tuple[int, int] = (0, 0)) -> tuple[int, str, list[tuple[str, str, str]]]:
        report = self.showrunner_inventory()
        owners = [report["sessions"][0], report["sessions"][2]]
        owners[1]["config_dir"] = str(self.root / ".claude")
        members = self.listed(inventory.Inventory(machine=report["machine"], login=report["login"],
            label=report["label"], sessions=[owners[0]], unattributed=[]))
        second_report = inventory.Inventory(machine="natedev", login="default@example.invalid", label="claude2",
            sessions=[owners[1]], unattributed=[])
        members += self.listed(second_report)
        events: list[tuple[str, str, str]] = []
        codes = iter(follow_codes)
        def start(command: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[2], "down")
            source = command[3]
            ids = command[command.index("--only") + 1].split(",")
            events.append(("down", source, ",".join(ids)))
            for owner in owners:
                if owner["session_id"] not in ids:
                    continue
                saved = entry(owner)
                saved["login"] = "current@example.invalid" if source == "claude1" else "default@example.invalid"
                saved["label"] = source
                saved["request_id"] = "request-" + source
                saved["target"] = "claude3"
                self.assertTrue(shutdown_record.create_session(saved))
            return subprocess.CompletedProcess(command, 0, "started " + source + "\n", "")
        def follow(login: str, request_id: str) -> int:
            events.append(("follow", login, request_id))
            code = next(codes)
            print(request_id + (": needs attention" if code else ": all 1 back on claude3"))
            return code
        output = io.StringIO()
        environment = dict(os.environ)
        _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(inventory, "listing", return_value=members),
              patch.object(inventory, "inventory", side_effect=AssertionError("move inventories")),
              patch.object(subprocess, "run", side_effect=start),
              patch.object(accounts, "follow_move", side_effect=follow),
              patch.object(output, "isatty", return_value=True), redirect_stdout(output)):
            result = accounts.main(["move", "showrunners", "claude3"])
        return result, output.getvalue(), events

    def test_mixed_source_group_returns_to_its_previously_selected_account(self) -> None:
        _ = accounts.set_group_account("showrunners", "claude2", why="original selection")
        result, output, events = self.mixed_source_run()
        self.assertEqual(result, 0)
        self.assertEqual(events, [("down", "claude1", "sr-one"), ("down", "claude2", "sr-two"),
            ("follow", "current@example.invalid", "request-claude1"),
            ("follow", "default@example.invalid", "request-claude2")])
        self.assertEqual(output.count("all 1 back on claude3"), 2)
        self.assertEqual([line for line in output.splitlines() if line.startswith("to move them back")],
            ["to move them back, in this terminal: ~/.claude/scripts/lib/py "
             + "~/.claude/scripts/accounts/accounts.py move showrunners claude2"])
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude3")

    def test_mixed_source_group_without_a_prior_account_has_no_conflicting_move_back_commands(self) -> None:
        self.assertFalse(accounts.groups_file().exists())
        result, output, events = self.mixed_source_run()
        self.assertEqual(result, 0)
        self.assertEqual(len(events), 4)
        self.assertNotIn("to move them back", output)

    def test_group_follows_later_source_after_an_earlier_request_needs_attention(self) -> None:
        result, output, events = self.mixed_source_run((1, 0))
        self.assertEqual(result, 1)
        self.assertEqual(events[2:], [("follow", "current@example.invalid", "request-claude1"),
            ("follow", "default@example.invalid", "request-claude2")])
        self.assertIn("request-claude1: needs attention", output)
        self.assertIn("request-claude2: all 1 back on claude3", output)

    def test_group_follow_detach_leaves_later_requests_running_without_following_them(self) -> None:
        result, output, events = self.mixed_source_run((130, 0))
        self.assertEqual(result, 130)
        self.assertEqual(events, [("down", "claude1", "sr-one"), ("down", "claude2", "sr-two"),
            ("follow", "current@example.invalid", "request-claude1")])
        self.assertNotIn("request-claude2: all", output)
        self.assertIsNotNone(shutdown_record.find_session("default@example.invalid", "sr-two"))

    def test_follow_uses_the_source_login_that_started_shutdown_after_listing_changes(self) -> None:
        original = self.current_listing()[0]
        moved = replace(original, login="default@example.invalid", config_dir=self.root / ".claude")
        def start(command: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[2:4], ("down", "claude2"))
            saved = entry(self.owner)
            saved["login"] = moved.login
            saved["label"] = "claude2"
            saved["source_label"] = "claude2"
            saved["request_id"] = "request-from-new-source"
            saved["target"] = "claude3"
            self.assertTrue(shutdown_record.create_session(saved))
            return subprocess.CompletedProcess(command, 0, "started on claude2\n", "")
        output = io.StringIO()
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(inventory, "listing", side_effect=[[original], [moved]]),
              patch.object(subprocess, "run", side_effect=start),
              patch.object(accounts, "follow_move", return_value=0) as follow,
              patch.object(output, "isatty", return_value=True), redirect_stdout(output)):
            result = accounts.main(["move", original.session_id, "claude3"])
        self.assertEqual(result, 0)
        follow.assert_called_once_with(moved.login, "request-from-new-source")
        self.assertIsNotNone(shutdown_record.find_session(moved.login, original.session_id))

    def test_bulk_move_forwards_a_single_explicit_reason_for_all_selected_showrunners(self) -> None:
        report = self.showrunner_inventory()
        with (
            patch.object(inventory, "listing", return_value=self.listed(report)),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", side_effect=self.start_batch) as process,
        ):
            with patch.object(account, "account_of", return_value=account.Account("claude", report["login"], report["label"])):
                self.assertEqual(accounts.main(["move", "showrunners", "claude3",
                    "--why", "weekly limit reached"]), 0)
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--why") + 1], "weekly limit reached")
        process.assert_called_once()

    def test_bulk_move_failed_shutdown_start_returns_failure_instead_of_started(self) -> None:
        report = self.showrunner_inventory()
        with (
            patch.object(inventory, "listing", return_value=self.listed(report)),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 2,
                "", "shutdown: keys are cold\n")),
        ):
            outcome = accounts.move_group("showrunners", "claude3")
        self.assertIsInstance(outcome, accounts.GroupMoveStartFailed)
        if not isinstance(outcome, accounts.GroupMoveStartFailed):
            self.fail("bulk shutdown start failed without a failed-start result")
        self.assertEqual(outcome.returncode, 2)
        self.assertEqual(outcome.stderr.strip(), "shutdown: keys are cold")

    def test_group_move_without_live_members_sets_next_launch_account(self) -> None:
        report = self.showrunner_inventory()
        report["sessions"] = [unit("unit-id"), top_level("ordinary-id")]
        with patch.object(inventory, "listing", return_value=self.listed(report)), patch.object(subprocess, "run") as process:
            result, _, error = self.batch_cli()
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude3")
        process.assert_not_called()

    def test_group_move_refuses_first_preparation_problem_without_change(self) -> None:
        with patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused(
            "accounts: target problem\naccounts: second target problem"
        )):
            self.batch_refuses_without_change("target problem")

    def assert_group_alias_moves_to_default(self, target: str) -> None:
        report = self.showrunner_inventory()
        with (patch.object(inventory, "listing", return_value=self.listed(report)),
              patch.object(subprocess, "run", side_effect=self.start_batch) as process):
            result, _, error = self.batch_cli(target=target)
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(accounts.group_accounts().get("showrunners"), target)
        process.assert_called_once()
        arguments = cast(list[str], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--to") + 1], target)

    def test_group_move_default_login_note_alias_selects_default(self) -> None:
        self.assert_group_alias_moves_to_default("claude2")

    def test_group_move_default_login_named_directory_selects_default(self) -> None:
        self.named_account("claude4", "default@example.invalid")
        self.assert_group_alias_moves_to_default("claude4")

    def test_group_move_to_current_account_sets_group_without_shutdown(self) -> None:
        with (patch.object(inventory, "listing", return_value=self.listed(self.showrunner_inventory())),
              patch.object(subprocess, "run") as process):
            result, _, error = self.batch_cli(target="claude1")
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude1")
        process.assert_not_called()

    def test_group_move_preserves_open_shutdown_and_requests_remaining_member(self) -> None:
        report = self.showrunner_inventory()
        saved = entry(report["sessions"][0])
        saved["login"] = "current@example.invalid"
        saved["label"] = "claude1"
        self.assertTrue(shutdown_record.create_session(saved))
        before = self.disk_state()
        with (
            patch.object(inventory, "listing", return_value=self.listed(report)),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", side_effect=self.start_batch) as process,
        ):
            result, output, error = self.batch_cli()
        self.assertEqual(result, 1)
        self.assertIn("already has a shutdown in progress", output + error)
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude3")
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--only") + 1], "sr-two")
        self.assertEqual(shutdown_record.find_session("current@example.invalid", "sr-one"), saved)
        added = shutdown_record.find_session("current@example.invalid", "sr-two")
        assert added is not None and "target" in added
        self.assertEqual(added["target"], "claude3")
        self.assertNotEqual(self.disk_state(), before)

    def test_group_preview_uses_only_listing_and_explains_wait_or_reap_without_writes(self) -> None:
        report = self.showrunner_inventory()
        before = self.disk_state()
        states = [settle.TakesPrompts("room"),
            settle.StoppedTakingPrompts("account out of usage", "weekly limit, resets Thu 9:00 AM")]
        with (patch.object(inventory, "listing", return_value=self.listed(report)) as listing,
              patch.object(inventory, "inventory", side_effect=AssertionError("preview read full inventory")),
              patch.object(settle, "prompt_state", side_effect=states) as prompt,
              patch.object(subprocess, "run") as process):
            result, output, error = self.batch_cli(dry_run=True)
        self.assertEqual((result, error), (0, ""))
        listing.assert_called()
        self.assertIn("sr-one", output)
        self.assertIn("first showrunner:", output)
        self.assertIn("will move once idle (claude1 has room)", output)
        self.assertIn("sr-two", output)
        self.assertIn("second showrunner:", output)
        self.assertIn("will stop and move now (claude1 out of usage: weekly limit, resets Thu 9:00 AM)", output)
        subjects = [cast(settle.PromptSubject, call.args[0]) for call in prompt.call_args_list]
        self.assertEqual([subject.session_id for subject in subjects], ["sr-one", "sr-two"])
        self.assertTrue(all(subject.login == report["login"] for subject in subjects))
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()

    def test_exhausted_target_refuses_group_and_single_moves_before_any_write(self) -> None:
        at = datetime.now(timezone.utc)
        empty = refusals.FreshReading(refusals.ReadingWindow(0,
            refusals.KnownReset(at + timedelta(days=1)), at), refusals.NoWindow(), "")
        for group in (False, True):
            for has_room in (False, True):
                with self.subTest(group=group, has_room=has_room):
                    def reading(_tool: str, login: str, _now: datetime) -> refusals.FreshReading:
                        if not has_room or login == "target@example.invalid":
                            return empty
                        days = 2 if login == "default@example.invalid" else 3
                        return refusals.FreshReading(refusals.ReadingWindow(50,
                            refusals.KnownReset(at + timedelta(days=days)), at), refusals.NoWindow(), "")

                    before = self.disk_state()
                    with (patch.object(refusals, "stored_reading", side_effect=reading),
                          patch.object(subprocess, "run") as process):
                        result, output, error = self.batch_cli() if group else self.cli()
                    self.assertNotEqual(result, 0)
                    self.assertIn("claude3 is out of usage", output + error)
                    self.assertIn("weekly 0% left", output + error)
                    self.assertIn("resets", output + error)
                    if has_room:
                        self.assertIn("; claude2 has room", output + error)
                    else:
                        self.assertIn("; no claude account has room; the earliest reset is", output + error)
                    self.assertNotIn("pick", output + error)
                    self.assertEqual(self.disk_state(), before)
                    process.assert_not_called()

    def test_target_with_exhausted_window_and_credits_can_still_be_previewed(self) -> None:
        at = datetime.now(timezone.utc)
        reading = refusals.FreshReading(refusals.ReadingWindow(0, refusals.NoResetKnown(), at),
            refusals.ReadingWindow(0, refusals.NoResetKnown(), at), "5.00")
        output, error = io.StringIO(), io.StringIO()
        before = self.disk_state()
        with (patch.object(refusals, "stored_reading", return_value=reading),
              patch.object(subprocess, "run") as process, redirect_stdout(output), redirect_stderr(error)):
            result = accounts.main(["move", "move-id", "claude3", "--dry-run"])
        self.assertEqual((result, error.getvalue()), (0, ""))
        self.assertIn("move-id", output.getvalue())
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()

    def test_single_terminal_move_returns_follow_detach_and_preserves_the_request(self) -> None:
        output, error = io.StringIO(), io.StringIO()
        environment = dict(os.environ)
        _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(subprocess, "run", side_effect=self.start_one) as process,
              patch.object(accounts, "follow_move", return_value=130) as follow,
              patch.object(output, "isatty", return_value=True), redirect_stdout(output), redirect_stderr(error)):
            result = accounts.main(["move", "move-id", "claude3"])
        self.assertEqual((result, error.getvalue()), (130, ""))
        saved = shutdown_record.find_session("current@example.invalid", "move-id")
        assert saved is not None
        follow.assert_called_once_with(saved["login"], saved["request_id"])
        process.assert_called_once()
        self.assertEqual(saved["progress"]["kind"], "waiting")
        self.assertIn("accounts.py move move-id claude1", output.getvalue())

    def test_stale_target_reading_is_named_in_preview_and_run(self) -> None:
        at = datetime.now(timezone.utc) - timedelta(minutes=25)
        stale = refusals.StaleReading(refusals.ReadingWindow(0, refusals.NoResetKnown(), at))
        for dry_run in (True, False):
            with (self.subTest(dry_run=dry_run),
                  patch.object(refusals, "stored_reading", return_value=stale),
                  patch.object(inventory, "listing", return_value=[]),
                  patch.object(subprocess, "run") as process):
                before = self.disk_state()
                result, output, error = self.batch_cli(dry_run=dry_run)
                self.assertEqual((result, error), (0, ""))
                self.assertIn("no fresh usage reading for claude3 (last ", output)
                self.assertIn("ago)", output)
                if dry_run:
                    self.assertEqual(self.disk_state(), before)
                process.assert_not_called()

    def test_recovered_not_ready_record_still_refuses_a_second_move(self) -> None:
        saved = entry(self.owner)
        saved["login"], saved["label"] = "current@example.invalid", "claude1"
        saved["progress"] = {"kind": "stop failed", "at": saved["requested_at"],
            "reason": "session or worker is not idle and ready"}
        saved["stop_issues"] = [shutdown_record.NotReadyToStop(kind="not ready to stop", at=saved["requested_at"],
            status="busy", progress="waiting")]
        directory = self.root / "shutdown" / saved["login"] / "sessions"
        directory.mkdir(parents=True)
        _ = (directory / "move-id.json").write_text(json.dumps(saved))
        before = self.disk_state()
        with patch.object(subprocess, "run") as process:
            result, output, error = self.cli()
        self.assertEqual(result, 1)
        self.assertIn("already has a shutdown in progress", output + error)
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()

    def test_single_preview_uses_listing_and_writes_nothing(self) -> None:
        before = self.disk_state()
        output, error = io.StringIO(), io.StringIO()
        with (patch.object(inventory, "inventory", side_effect=AssertionError("single preview inventories")),
              patch.object(subprocess, "run") as process,
              redirect_stdout(output), redirect_stderr(error)):
            result = accounts.main(["move", "kept name", "claude3", "--dry-run"])
        self.assertEqual((result, error.getvalue()), (0, ""))
        self.assertIn("kept name:", output.getvalue())
        self.assertIn("move-id", output.getvalue())
        self.assertIn("will move once idle", output.getvalue())
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()

    def test_preview_reports_absent_registration_target_from_one_timer_read_without_sockets(self) -> None:
        runner = self.root / "notifier/showrunner-missing-runner"
        runner.mkdir(parents=True)
        doc = self.root / "missing-production.md"
        _ = doc.write_text("- **User zone:** America/New_York\n", encoding="utf-8")
        _ = (runner / "conf").write_text(
            f"TARGET=session:absent-session\nCHECK=zsh /opt/production_check.sh {doc}\n", encoding="utf-8")
        with (patch.object(inventory, "listing", return_value=[]),
              patch.object(showrunners, "checked_doc", wraps=showrunners.checked_doc) as checked,
              patch.object(showrunners, "socket_for", side_effect=AssertionError("preview resolved a socket")),
              patch.object(showrunners, "registered_showrunners", side_effect=AssertionError("preview resolved showrunners")),
              patch.object(inventory, "inventory", side_effect=AssertionError("registration check inventories"))):
            result, output, error = self.batch_cli(dry_run=True)
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(checked.call_count, 1, "preview must reuse registration discovery")
        self.assertIn("missing-runner", output)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py", output)
        self.assertIn("in any Claude session on", output)
        self.assertIn("--production " + str(doc), output)

    def test_group_move_dry_run_prints_members_and_target_without_change(self) -> None:
        report = self.showrunner_inventory()
        before = self.disk_state()
        with patch.object(inventory, "listing", return_value=self.listed(report)), patch.object(subprocess, "run") as process:
            result, output, error = self.batch_cli(dry_run=True)
        self.assertEqual((result, error), (0, ""))
        self.assertIn("showrunners: would select claude3", output)
        self.assertIn("first showrunner:", output)
        self.assertIn("second showrunner:", output)
        self.assertNotIn("unit-id", output)
        self.assertNotIn("ordinary-id", output)
        self.assertEqual(self.disk_state(), before)
        process.assert_not_called()

    def test_bulk_move_to_home_preserves_its_number_in_the_single_down(self) -> None:
        report = self.showrunner_inventory()
        with (
            patch.object(inventory, "listing", return_value=self.listed(report)),
            patch.object(sessions, "live_sessions", return_value=self.batch_registry(report)),
            patch.object(subprocess, "run", side_effect=self.start_batch) as process,
        ):
            result, _, error = self.batch_cli(target="claude2")
        self.assertEqual((result, error), (0, ""))
        arguments = cast(tuple[str, ...], process.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--to") + 1], "claude2")

    def test_group_move_with_all_members_shutting_down_commits_group_without_second_request(self) -> None:
        report = self.showrunner_inventory()
        for owner in (report["sessions"][0], report["sessions"][2]):
            saved = entry(owner)
            saved["login"] = "current@example.invalid"
            saved["label"] = "claude1"
            self.assertTrue(shutdown_record.create_session(saved))
        before = self.disk_state()
        with patch.object(inventory, "listing", return_value=self.listed(report)), patch.object(subprocess, "run") as process:
            result, output, error = self.batch_cli()
        self.assertEqual(result, 1)
        self.assertEqual((output + error).count("already has a shutdown in progress"), 2)
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude3")
        self.assertNotEqual(self.disk_state(), before)
        process.assert_not_called()
