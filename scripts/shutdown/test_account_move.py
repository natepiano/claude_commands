"""Moving one local conversation refuses before any mutation and preserves its identity."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import staff_sources
import io
import json
import os
import subprocess
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr, redirect_stdout
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.whoami.note_file import note_name

import inventory
import shutdown_record
import sessions
import settle
import showrunners
import launch_permission
from importlib import import_module
from scripts.whoami import account
from scripts.whoami import agent_accounts
from scripts.accounts import accounts, refusals
from scripts.accounts.move_test_support import FakeSessionMoves, MoveCall
from scripts.shutdown import stop_resume
from test_restart import entry, worker_session, showrunner, top_level, unit
from test_support import ShutdownTestCase

# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300


class AccountMoveTests(ShutdownTestCase):
    root: Path = Path()
    moves: FakeSessionMoves = FakeSessionMoves()
    owner: inventory.Session = top_level("move-id", name="kept name")

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
        self.moves = FakeSessionMoves()
        _ = self.enterContext(patch.object(stop_resume, "move_sessions", side_effect=self.moves))
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


    def test_move_calls_stop_resume_once_by_id_with_the_reason_and_prints_its_result(self) -> None:
        result = accounts.move("kept name", "claude3", why="weekly limit")
        self.assertIsInstance(result, accounts.MoveStarted)
        self.assertEqual(self.moves.calls, [MoveCall("move-id", "claude3", "weekly limit")])
        assert isinstance(result, accounts.MoveStarted)
        self.assertEqual(result.stdout.splitlines()[-1], "move-id: resumed on claude3 in w1:p1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(shutdown_record.all_session_records(), [])

    def test_handed_off_self_move_is_success_and_prints_moving_now(self) -> None:
        with patch.object(stop_resume, "move_sessions", return_value=[stop_resume.MoveHandedOff("move-id")]):
            result, output, error = self.cli()
        self.assertEqual(result, 0)
        self.assertEqual(error, "")
        self.assertIn("move-id: moving now\n", output)
        self.assertNotIn("not moved", output)
        self.assertEqual(shutdown_record.all_session_records(), [])

    def test_move_ignores_an_old_open_record_and_leaves_it_unchanged(self) -> None:
        saved = entry(self.owner)
        saved["login"], saved["label"] = "current@example.invalid", "claude1"
        self.assertTrue(shutdown_record.create_session(saved))
        result = accounts.move("move-id", "claude3")
        self.assertIsInstance(result, accounts.MoveStarted)
        self.assertEqual(len(self.moves.calls), 1)
        self.assertEqual(shutdown_record.find_session(saved["login"], "move-id"), saved)

    def test_move_failure_prints_one_failure_line_without_a_follow_or_new_record(self) -> None:
        self.moves.failures["move-id"] = "pane refused resume"
        result, output, error = self.cli()
        self.assertEqual(result, 1)
        self.assertEqual(error, "move-id: not moved: pane refused resume\n")
        self.assertNotIn("follow", output + error)
        self.assertEqual(shutdown_record.all_session_records(), [])

    def test_dry_run_changes_nothing_and_starts_no_move(self) -> None:
        before = self.disk_state()
        result = accounts.move("move-id", "claude3", dry_run=True)
        self.assertIsInstance(result, accounts.MoveDryRun)
        assert isinstance(result, accounts.MoveDryRun)
        self.assertIn("kept name: move-id · stop, then resume on claude3 (from claude1)", result.lines)
        self.assertEqual(self.moves.calls, [])
        self.assertEqual(self.disk_state(), before)

    def test_group_calls_move_once_per_off_target_member_across_source_accounts(self) -> None:
        members = [replace(self.current_listing()[0], session_id="first", kind="showrunner"),
                   replace(self.current_listing()[0], session_id="second", kind="showrunner", login="default@example.invalid"),
                   replace(self.current_listing()[0], session_id="already", kind="showrunner", login="target@example.invalid")]
        with (patch.object(inventory, "listing", return_value=members),
              patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": members}),
              patch.object(inventory, "unregistered_showrunners", return_value=[])):
            result = accounts.move_group("showrunners", "claude3", why="manual group move")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        self.assertEqual(self.moves.calls, [MoveCall(identity, "claude3", "manual group move") for identity in ("first", "second")])
        self.assertEqual(self.moves.batches, [["first", "second"]])
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude3")
        assert isinstance(result, accounts.GroupMoveStarted)
        self.assertEqual(sum("resumed on" in line for line in result.stdout.splitlines()), 2)

    def test_move_sessions_continues_after_one_member_fails_and_refuses_workers(self) -> None:
        base = self.current_listing()[0]
        members = [replace(base, session_id="failed"), replace(base, session_id="ok"), replace(base, session_id="worker", kind="worker")]
        self.moves.failures["failed"] = "stop refused"
        with patch.object(inventory, "listing", return_value=members):
            outcomes = accounts.move_sessions(("failed", "ok", "worker", "missing"), "claude3", why="retry")
        self.assertEqual([call.session_id for call in self.moves.calls], ["failed", "ok"])
        self.assertEqual([accounts.move_result_line(item) for item in outcomes], [
            "failed: not moved: stop refused", "ok: resumed on claude3 in w1:p1",
            "worker: not moved: workers move with their unit director", "missing: not moved: session is not live on this machine"])

    def test_group_preview_and_empty_selection_do_not_move(self) -> None:
        with (patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": []}),
              patch.object(inventory, "unregistered_showrunners", return_value=[])):
            preview = accounts.move_group("units", "claude3", dry_run=True)
            self.assertIsInstance(preview, accounts.GroupMoveDryRun)
            self.assertFalse(accounts.groups_file().exists())
            result = accounts.move_group("units", "claude3")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        self.assertEqual(accounts.group_accounts().get("units"), "claude3")
        self.assertEqual(self.moves.calls, [])

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


    def test_group_move_to_current_account_sets_group_without_shutdown(self) -> None:
        with (patch.object(inventory, "listing", return_value=self.listed(self.showrunner_inventory())),
              patch.object(subprocess, "run") as process):
            result, _, error = self.batch_cli(target="claude1")
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude1")
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
