"""Move one local conversation and preserve its session identity."""

from __future__ import annotations

from pathlib import Path


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


from scripts.whoami.note_file import note_name

from scripts.shutdown import inventory
from scripts.message import sessions
from scripts.production import showrunners
from scripts.production.staff_test_support import recorded_showrunner
from scripts.whoami import account
from scripts.whoami import agent_accounts
from scripts.accounts import accounts, refusals
from scripts.accounts.home_test_support import scratch_accounts_config
from scripts.accounts.move_test_support import FakeSessionMoves, MoveCall
from scripts.shutdown import stop_resume
from scripts.shutdown.test_restart import showrunner, top_level, unit
from scripts.shutdown.test_support import ShutdownTestCase

# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300


class AccountMoveTests(ShutdownTestCase):
    root: Path = Path()
    moves: FakeSessionMoves = FakeSessionMoves()
    owner: inventory.Session = top_level("move-id", name="kept name")

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(scratch_accounts_config())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notes = self.root / "notes"
        notes.mkdir()
        context = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(notes),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "registry"),
        })))
        del context
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
        _ = self.enterContext(patch.object(accounts, "live_group_members", side_effect=self.group_members))
        at = datetime.now(timezone.utc)
        _ = self.enterContext(patch.object(refusals, "stored_reading", return_value=refusals.FreshReading(
            refusals.ReadingWindow(80, refusals.NoResetKnown(), at), refusals.NoWindow(), "")))


    def group_members(self) -> dict[accounts.ClaudeGroup, list[accounts.LiveGroupMember]]:
        groups: dict[accounts.ClaudeGroup, list[accounts.LiveGroupMember]] = {"units": [], "showrunners": []}
        for session in inventory.listing():
            group = accounts.session_group(session)
            if group != "ungrouped":
                groups[group].append(accounts.LiveGroupMember(session.session_id, session.name, group, session.config_dir))
        return groups

    def test_group_and_single_move_ignore_stale_shutdown_holds(self) -> None:
        for subject in ("units", "move-id"):
            with self.subTest(subject=subject):
                self.moves.calls.clear()
                member = replace(self.current_listing()[0], kind="unit")
                with (patch.object(accounts, "live_group_members", return_value={"units": [member], "showrunners": []})):
                    result, output, error = self.cli(subject)
                self.assertEqual(result, 0, error)
                self.assertEqual(error, "")
                self.assertIn("move-id: Claude is up on claude3", output)
                self.assertEqual([call.session_id for call in self.moves.calls], ["move-id"])

    def test_target_preview_reads_usage_without_querying_holds(self) -> None:
        before = self.disk_state()
        result = accounts.check_move_target("claude3", datetime.now(timezone.utc), repair=False)
        self.assertIsInstance(result, accounts.MoveTargetReady)
        self.assertEqual(self.disk_state(), before)


    def listed(self, report: inventory.Inventory) -> list[inventory.ListedSession]:
        return [inventory.ListedSession(session_id=item["session_id"], pid=item["pid"],
            proc_start=item["proc_start"], name=item["name"], kind=item["kind"],
            config_dir=Path(item.get("config_dir", str(accounts.account_dir("claude1")))), login=report["login"],
            production=item["production"] if item["kind"] == "showrunner" else
                staff_sources.unit_parent(item["host"]["unit"]).name if item["host"]["kind"] == "unit" else "",
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
            patch.object(stop_resume, "move_sessions") as move,
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
        move.assert_not_called()


    def test_move_absent_from_live_registry_reaches_the_saved_tab_boundary(self) -> None:
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(stop_resume, "move_sessions", return_value=[stop_resume.SessionNotFound("remote-only")]) as moved):
            status, output, error = self.cli(session="remote-only")
        self.assertEqual((status, output), (0, ""))
        self.assertEqual(error, "accounts: anomaly: remote-only: no saved session was found\n")
        moved.assert_called_once_with(["remote-only"], "claude3", why="finish account move", cause="by hand",
                                      source_accounts={})

    def test_stopped_move_keeps_pending_outcome_then_rerun_reaches_resume(self) -> None:
        with (patch.object(inventory, "listing", return_value=[]),
              patch.object(stop_resume, "move_sessions", side_effect=[
                  [stop_resume.ResumePending("move-id", "claude3", "registry unreadable")],
                  [stop_resume.ClaudeUp("move-id", "w1:p1", "claude3")]]) as moved):
            status, output, error = self.cli()
            self.assertEqual((status, output), (1, ""))
            self.assertEqual(error, "move-id: stopped, resume pending on claude3: registry unreadable\n")
            self.assertNotIn("not moved", error)
            status, output, error = self.cli()
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(output, "move-id: Claude is up on claude3 in w1:p1 (0.0 s)\n")
        self.assertEqual(moved.call_count, 2)


    def test_group_preview_starts_in_a_fresh_process_without_prior_shutdown_imports(self) -> None:
        environment = {
            "HOME": str(self.root),
            "STAFF_FILE": os.environ["STAFF_FILE"],
            "STAFF_CONFIG": os.environ["STAFF_CONFIG"],
            "PATH": os.defpath,
            "CONF_DIR": os.environ["CONF_DIR"],
            "AGENT_NOTES_DIR": str(self.root / "notes"),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "NOTIFIER_STATE_DIR": os.environ["NOTIFIER_STATE_DIR"],
            "NOTIFIER_SESSIONS_DIR": str(self.root / "registry"),
            "USER_ZONE": "UTC",
        }
        before = self.disk_state()
        result = subprocess.run(
            [str(accounts.SCRIPTS / "lib" / "py"), str(accounts.SCRIPTS / "accounts" / "accounts.py"),
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


    def test_move_to_its_current_login_is_an_idempotent_success(self) -> None:
        status, output, error = self.cli(target="claude1")
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("kept name: already on claude1", output)


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
                   replace(self.current_listing()[0], session_id="second", kind="showrunner", login="default@example.invalid", config_dir=self.root / ".claude2"),
                   replace(self.current_listing()[0], session_id="already", kind="showrunner", login="target@example.invalid", config_dir=self.root / ".claude3")]
        with (patch.object(inventory, "listing", return_value=members),
              patch.object(accounts, "live_group_members", return_value={"units": [], "showrunners": members}),
              patch.object(inventory, "unregistered_showrunners", return_value=[])):
            result = accounts.move_group("showrunners", "claude3", why="manual group move")
        self.assertIsInstance(result, accounts.GroupMoveStarted)
        self.assertEqual(self.moves.calls, [MoveCall(identity, "claude3", "manual group move") for identity in ("first", "second")])
        self.assertEqual(self.moves.batches, [["first", "second"]])
        self.assertEqual(accounts.group_accounts().get("showrunners"), "claude3")
        assert isinstance(result, accounts.GroupMoveStarted)
        self.assertEqual(sum("Claude is up on" in line for line in result.stdout.splitlines()), 2)

    def test_move_sessions_continues_after_one_member_fails(self) -> None:
        base = self.current_listing()[0]
        members = [replace(base, session_id="failed"), replace(base, session_id="ok"), replace(base, session_id="worker", kind="worker")]
        self.moves.failures["failed"] = "stop refused"
        error = io.StringIO()
        with (redirect_stderr(error), patch.object(inventory, "listing", return_value=members),
              patch.object(stop_resume, "move_sessions", return_value=[
                  stop_resume.StopFailed("failed", "stop refused"), stop_resume.ClaudeUp("ok", "w1:p1", "claude3"),
                  stop_resume.ClaudeUp("worker", "w1:p1", "claude3"),
                  stop_resume.SessionNotFound("missing")]) as batch):
            outcomes = accounts.move_sessions(("failed", "ok", "worker", "missing"), "claude3", why="retry")
        batch.assert_called_once_with(["failed", "ok", "worker", "missing"], "claude3", why="retry", cause="by hand",
                                      source_accounts={"failed": "claude1", "ok": "claude1", "worker": "claude1"})
        self.assertEqual([accounts.move_result_line(item) for item in outcomes], [
            "failed: not moved: stop refused", "ok: Claude is up on claude3 in w1:p1 (0.0 s)",
            "worker: Claude is up on claude3 in w1:p1 (0.0 s)"])
        self.assertEqual(error.getvalue(), "accounts: anomaly: missing: no saved session was found\n")

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
                    def reading(_tool: str, login: str, _now: datetime, *_age: timedelta) -> refusals.FreshReading:
                        if not has_room or login == "target@example.invalid":
                            return empty
                        days = 2 if login == "default@example.invalid" else 3
                        return refusals.FreshReading(refusals.ReadingWindow(50,
                            refusals.KnownReset(at + timedelta(days=days)), at), refusals.NoWindow(), "")

                    before = self.disk_state()
                    with (patch.object(refusals, "stored_reading", side_effect=reading),
                          patch.object(refusals, "stored_reading_with_age", side_effect=reading),
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
        doc = self.root / "missing-production.md"
        owner = recorded_showrunner("missing", doc)
        runner = Path(os.environ["NOTIFIER_STATE_DIR"]) / f"showrunner-{owner.id}"
        runner.mkdir(parents=True)
        _ = doc.write_text("- **User zone:** America/New_York\n", encoding="utf-8")
        _ = (runner / "conf").write_text(
            f"TARGET=session:absent-session\nCHECK=zsh /opt/production_check.sh {owner.id}\n", encoding="utf-8")
        with (patch.object(inventory, "listing", return_value=[]),
              patch.object(inventory, "_conf", wraps=getattr(inventory, "_conf")) as checked,
              patch.object(showrunners, "socket_for", side_effect=AssertionError("preview resolved a socket")),
              patch.object(showrunners, "registered_showrunners", side_effect=AssertionError("preview resolved showrunners")),
              patch.object(inventory, "inventory", side_effect=AssertionError("registration check inventories"))):
            result, output, error = self.batch_cli(dry_run=True)
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(checked.call_count, 1, "preview must reuse registration discovery")
        self.assertIn("missing: no session runs under the id its update timer names", output)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py", output)
        self.assertIn("in any Claude session on", output)
        self.assertIn("--production missing", output)


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
