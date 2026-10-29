"""Account attribution and stale-data behavior of the vault writer."""

from scripts.whoami.note_file import note_name

import asyncio
import base64
import io
import json
import os
import subprocess
import time
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Self, cast, override
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_notes
import note_file
import session_table
import account
import agent_accounts
import codex_pacer
import five_hour
import quota_alert
import usage_store
import whoami
from agent_accounts import (ClaudeDirectorySource, CodexHomeSource, CodexRateLimits, Quota, Report,
                            codex_credits, reset_credits)
from agent_notes import local_reset
from note_file import Note, read_note
from scripts.accounts import accounts, refusal_watch, refusals, role_map
from scripts.production import showrunners


def apply_quota_and_write(notes: list[Note], report: Report, checked_at: datetime) -> list[str]:
    """Exercise quota calculation and the writer without a live roster or account-role selection."""
    result = agent_notes.apply(notes, report, checked_at)
    stamp = {"date_modified": "fixture"}
    for note in notes:
        if note.label in result.updates:
            _ = agent_notes.write_note(note, dict(result.updates[note.label]), stamp=stamp)
    messages = list(result.messages)
    if result.readings:
        try:
            agent_notes.append_readings(list(result.readings), checked_at)
        except (OSError, UnicodeError) as error:
            messages.append(f"readings log: {error}")
    return messages


def setUpModule() -> None:
    _ = unittest.enterModuleContext(patch.object(role_map, "is_role_map_host", return_value=True))
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", root))


class AgentNotesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.now: datetime = datetime.now(timezone.utc).replace(microsecond=0)
        self.future: datetime = self.now + timedelta(days=2)
        self.rows: MagicMock = MagicMock()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        home_context = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root),
        })))
        del home_context
        _ = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        _ = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        _ = os.environ.pop("CODEX_HOME", None)
        _ = self.enterContext(patch.object(note_file, "AGENTS_DIR", self.root))
        _ = self.enterContext(patch.object(agent_notes, "READINGS_LOG", self.root / "readings.jsonl", create=True))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))
        self.rows = cast(MagicMock, self.enterContext(patch.object(session_table, "live_sessions", return_value=session_table.ReadSessions({}, frozenset()))))

    def snapshot(self, reports: list[Report]) -> agent_notes.UpdatedAgentNotes:
        selection = agent_accounts.ClaudeAccountsInUse(agent_accounts.UnreadableIdentity(Path.home() / ".claude", "fixture identity unavailable"), False, (), frozenset())
        with patch.object(agent_notes, "live_reports", return_value=reports), patch.object(five_hour, "watch", return_value=[]):
            return agent_notes.update_alert_pass(selection, agent_accounts.observe_workers_home(), force_usage=True)

    def test_timer_reuses_usage_but_updates_session_sections_each_pass(self) -> None:
        home = self.root / ".codex"
        self.codex_login(home, "timer@example.invalid")
        note = self.note("codex1.md", "timer@example.invalid")
        report = Report("Codex", email="timer@example.invalid", source=CodexHomeSource(home),
                        quotas=[Quota("Weekly", 20, self.future)], credit_balance="0")
        selection = agent_accounts.ClaudeAccountsInUse(
            agent_accounts.UnreadableIdentity(self.root / ".claude", "fixture"), False, (), frozenset())
        with (patch.object(agent_notes, "live_reports", return_value=[report]) as collect,
              patch.object(five_hour, "watch", return_value=[])):
            first = agent_notes.update_alert_pass(selection, home)
            before = note.path.read_bytes()
            second = agent_notes.update_alert_pass(selection, home)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(self.rows.call_count, 2)
        self.assertEqual(note.path.read_bytes(), before)
        self.assertEqual(second.codex_readings.checked_homes[0].observed_at,
                         first.codex_readings.checked_homes[0].observed_at)
        self.assertEqual(second.codex_readings.checked_homes[0].report.weekly_remaining, 80)

    def test_explicit_usage_refresh_bypasses_recent_timer_reading(self) -> None:
        with (patch.object(agent_notes, "live_reports", return_value=[]) as collect,
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update()
            with patch.object(sys, "argv", ["agent_notes.py", "refresh"]), \
                 patch.object(agent_notes, "refresh", return_value=[]), \
                 patch.object(agent_notes, "current_session", return_value=None), \
                 patch.object(codex_pacer, "tick"), patch.object(agent_notes, "mark_tier"), \
                 redirect_stdout(io.StringIO()):
                agent_notes.main()
        self.assertEqual(collect.call_count, 2)

    def test_refusal_reads_once_per_pass_and_does_not_repeat_session_collection(self) -> None:
        from collections.abc import Callable

        def refuse(*, refresh_usage: Callable[[], None]) -> list[str]:
            refresh_usage()
            return []

        with (patch.object(agent_notes, "live_reports", return_value=[]) as collect,
              patch.object(five_hour, "watch", return_value=[]),
              patch.object(sys, "argv", ["agent_notes.py"]),
              patch.object(agent_notes, "alert", return_value=[]),
              patch.object(codex_pacer, "tick"), patch.object(agent_notes, "mark_tier"),
              patch.object(refusal_watch, "run", side_effect=refuse), redirect_stdout(io.StringIO())):
            agent_notes.main()
            self.assertEqual(collect.call_count, 1)
            agent_notes.main()
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(self.rows.call_count, 2)

    def test_timer_signature_is_stable_when_session_order_changes(self) -> None:
        from dataclasses import replace
        selection = agent_accounts.ClaudeAccountsInUse(
            agent_accounts.UnreadableIdentity(self.root / ".claude", "fixture"), False, (),
            frozenset(("first@example.invalid", "second@example.invalid")),
            (self.root / ".claude2", self.root / ".claude3"))
        reordered = replace(selection, live_directories=tuple(reversed(selection.live_directories)))
        self.assertEqual(agent_notes.timer_usage_signature(selection, self.root / ".codex"),
                         agent_notes.timer_usage_signature(reordered, self.root / ".codex"))

    def test_refusal_refresh_saves_the_same_login_labels_as_the_main_pass(self) -> None:
        from collections.abc import Callable

        _ = self.note("claude1.md", "timer@example.invalid")

        def refuse(*, refresh_usage: Callable[[], None]) -> list[str]:
            refresh_usage()
            return []

        with (patch.object(agent_notes, "live_reports", return_value=[]),
              patch.object(five_hour, "watch", return_value=[]),
              patch.object(sys, "argv", ["agent_notes.py"]),
              patch.object(agent_notes, "alert", return_value=[]),
              patch.object(codex_pacer, "tick"), patch.object(agent_notes, "mark_tier"),
              patch.object(refusal_watch, "run", side_effect=refuse),
              patch.object(usage_store, "save") as save, redirect_stdout(io.StringIO())):
            agent_notes.main()
            agent_notes.main()
        self.assertEqual(save.call_count, 2)
        for saved in save.call_args_list:
            self.assertEqual(saved.args[2:], ({"claude1": "timer@example.invalid"},))

    def test_low_in_use_quota_keeps_two_minute_reads_until_exhaustion(self) -> None:
        for tool in ("Claude", "Codex"):
            for window in ("weekly", "five_hour"):
                for left in (1, 10):
                    with self.subTest(tool=tool, window=window, left=left):
                        home = self.root / ".codex"
                        report = Report(tool, email="timer@example.invalid",
                            source=CodexHomeSource(home) if tool == "Codex" else ClaudeDirectorySource(self.root / ".claude1"),
                            quotas=[Quota("Weekly", 100 - left if window == "weekly" else 20, self.future)],
                            five_hour=Quota("5-hour", 100 - left if window == "five_hour" else 20, self.future))
                        exhausted = Report(tool, email=report.email, source=report.source,
                            quotas=[Quota("Weekly", 100, self.future)], five_hour=Quota("5-hour", 100, self.future))
                        selection = agent_accounts.ClaudeAccountsInUse(
                            agent_accounts.UnreadableIdentity(self.root / ".claude", "fixture"), False, (),
                            frozenset(("timer@example.invalid",)) if tool == "Claude" else frozenset())
                        with (patch.object(agent_notes, "live_reports", side_effect=[[report], [exhausted]]) as collect,
                              patch.object(five_hour, "watch", return_value=[])):
                            _ = agent_notes.update_alert_pass(selection, home, force_usage=True)
                            result = agent_notes.update_alert_pass(selection, home)
                        self.assertEqual(collect.call_count, 2)
                        self.assertFalse(result.usage_reused)

    def test_timer_reads_again_when_a_codex_login_changes(self) -> None:
        home = self.root / ".codex"
        self.codex_login(home, "first@example.invalid")
        selection = agent_accounts.ClaudeAccountsInUse(
            agent_accounts.UnreadableIdentity(self.root / ".claude", "fixture"), False, (), frozenset())
        with (patch.object(agent_notes, "live_reports", return_value=[]) as collect,
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update_alert_pass(selection, home)
            self.codex_login(home, "second@example.invalid")
            _ = agent_notes.update_alert_pass(selection, home)
        self.assertEqual(collect.call_count, 2)

    def test_timer_refreshes_at_ten_minutes(self) -> None:
        selection = agent_accounts.ClaudeAccountsInUse(
            agent_accounts.UnreadableIdentity(self.root / ".claude", "fixture"), False, (), frozenset())
        observed = self.now

        class ObservationTime(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> Self:
                return cls.fromtimestamp(observed.timestamp(), tz)

        with (patch.object(agent_notes, "live_reports", return_value=[]) as collect,
              patch.object(five_hour, "watch", return_value=[]),
              patch.object(agent_notes, "datetime", ObservationTime)):
            _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
            observed = self.now + timedelta(minutes=9, seconds=59)
            _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
            self.assertEqual(collect.call_count, 1)
            observed = self.now + timedelta(minutes=10)
            _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
        self.assertEqual(collect.call_count, 2)

    def test_timer_reads_again_for_exhaustion_or_pending_move(self) -> None:
        from scripts.accounts import next_moves
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                _ = self.note("claude1.md", "timer@example.invalid",
                              "weekly_remaining_usage: 0\n" if exhausted else "")
                selection = agent_accounts.ClaudeAccountsInUse(
                    agent_accounts.UnreadableIdentity(self.root / ".claude", "fixture"), False, (),
                    frozenset(("timer@example.invalid",)))
                with (patch.object(agent_notes, "live_reports", return_value=[]) as collect,
                      patch.object(five_hour, "watch", return_value=[]),
                      patch.object(next_moves, "read_next_moves", return_value=() if exhausted else (object(),))):
                    _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
                    _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
                self.assertEqual(collect.call_count, 2)

    def test_exhausted_inactive_spare_does_not_disable_timer_reuse(self) -> None:
        _ = self.note("claude1.md", "spare@example.invalid", "weekly_remaining_usage: 0\n")
        report = Report("Claude", email="spare@example.invalid", quotas=[Quota("Weekly", 100, self.future)])
        selection = agent_accounts.ClaudeAccountsInUse(
            agent_accounts.UnreadableIdentity(self.root / ".claude", "fixture"), False, (), frozenset())
        with (patch.object(agent_notes, "live_reports", return_value=[report]) as collect,
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
            _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
        self.assertEqual(collect.call_count, 1)

    def test_exhausted_selected_five_hour_window_requires_fresh_usage(self) -> None:
        home = self.root / ".codex"
        report = Report("Codex", source=CodexHomeSource(home), quotas=[Quota("Weekly", 20, self.future)],
                        five_hour=Quota("5-hour", 100, self.future))
        selection = agent_accounts.ClaudeAccountsInUse(
            agent_accounts.UnreadableIdentity(self.root / ".claude", "fixture"), False, (), frozenset())
        with (patch.object(agent_notes, "live_reports", return_value=[report]) as collect,
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update_alert_pass(selection, home)
            _ = agent_notes.update_alert_pass(selection, home)
        self.assertEqual(collect.call_count, 2)

    def test_held_usage_adds_no_note_message_or_quota_change(self) -> None:
        note = self.note("claude2.md", "main@example.invalid",
            "weekly_remaining_usage: 75\nweekly_usage_checked_at: 2026-10-19T08:09:00+00:00\n")
        before = note.path.read_bytes()
        report = Report("Claude", email="main@example.invalid",
            usage_read_outcome=agent_accounts.UsageReadOutcome.HELD)
        messages = apply_quota_and_write([note], report, self.now)
        self.assertEqual(messages, [])
        self.assertEqual(note.path.read_bytes(), before)
        self.assertFalse((self.root / "readings.jsonl").exists())

    def test_unmapped_workers_keep_observations_without_selecting_the_signed_in_home(self) -> None:
        self.codex_login(self.root / ".codex", "spare@example.invalid")
        note = self.note("codex2.md", "spare@example.invalid")
        report = Report("Codex", email="spare@example.invalid",
                        source=CodexHomeSource(self.root / ".codex"),
                        quotas=[Quota("Weekly", 40, self.future)])
        with (patch.object(agent_notes, "live_reports", return_value=[report]),
              patch.object(five_hour, "watch", return_value=[]) as watch):
            messages = agent_notes.update()
        updated = read_note(note.path)
        assert isinstance(updated, Note)
        self.assertEqual(updated.get("weekly_remaining_usage"), "60")
        self.assertIn("accounts: workers has no mapped account; run accounts.py set-role workers <codexN>", messages)
        self.assertEqual(watch.call_args.args[0], [])
        readings = cast(object, watch.call_args.kwargs["codex_readings"])
        assert isinstance(readings, quota_alert.CodexAlertPassReadings)
        self.assertIsInstance(readings.workers, agent_accounts.NoWorkersHome)
        self.assertIsInstance(readings.selected_account, agent_accounts.NoWorkersHome)
        self.assertEqual([reading.home for reading in readings.checked_homes], [self.root / ".codex"])

    def test_each_updater_run_sweeps_stale_claude_temps_once_with_multiple_account_notes(self) -> None:
        _ = self.note("claude1.md", "unit@example.invalid")
        _ = self.note("claude2.md", "spare@example.invalid")
        stale = self.root / ".claude.json.tmp.1234.abandoned"
        old = time.time() - timedelta(days=2).total_seconds()
        for run in range(2):
            with self.subTest(run=run):
                _ = stale.write_text("stale atomic write", encoding="utf-8")
                os.utime(stale, (old, old))
                with patch.object(accounts, "sweep_claude_temp_files", wraps=accounts.sweep_claude_temp_files) as sweep:
                    _ = self.snapshot([])
                sweep.assert_called_once()
                self.assertFalse(stale.exists())

    def test_first_snapshot_renames_legacy_notes_without_changing_body_or_mode(self) -> None:
        old = self.root / "codex 2.md"
        text = "---\nlogin: legacy@example.invalid\nuse_credits: off\n---\nMy untouched account notes.\n"
        _ = old.write_text(text)
        old.chmod(0o644)
        inode = old.stat().st_ino
        self.rows.return_value = session_table.SessionsUnread("no sessions fixture")
        previous_umask = os.umask(0o077)
        try:
            messages = self.snapshot([]).messages
        finally:
            _ = os.umask(previous_umask)
        numbered = self.root / "codex2.md"
        self.assertFalse(old.exists())
        self.assertEqual(numbered.read_text(), text)
        self.assertEqual(numbered.stat().st_mode & 0o777, 0o644)
        self.assertEqual(numbered.stat().st_ino, inode)
        self.assertIn("agents: renamed codex 2.md to codex2.md", messages)
        self.rows.return_value = session_table.SessionsUnread("no sessions fixture")
        _ = self.snapshot([])
        self.assertIn("My untouched account notes.", numbered.read_text())

    def test_snapshot_names_legacy_collision_without_replacing_either_file(self) -> None:
        old, new = self.root / "codex 2.md", self.root / "codex2.md"
        _ = old.write_text("---\nlogin: legacy@example.invalid\n---\nOld body.\n")
        _ = new.write_text("---\nlogin: current@example.invalid\n---\nNew body.\n")
        before = (old.read_bytes(), new.read_bytes())
        messages = agent_notes.rename_legacy_notes(self.root)
        self.assertEqual((old.read_bytes(), new.read_bytes()), before)
        self.assertEqual(messages, ["agents: codex 2.md and codex2.md both exist; reading codex2.md"])

    def test_note_discovery_and_migration_are_idempotent_for_each_filename_combination(self) -> None:
        self.rows.return_value = session_table.SessionsUnread("fixture")
        for names in (("codex2.md",), ("codex 2.md",), ("codex 2.md", "codex2.md")):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in names:
                    _ = (root / name).write_text("---\nlogin: current@example.invalid\n---\nKeep this body.\n")
                with patch.dict(os.environ, {"AGENT_NOTES_DIR": directory}):
                    _ = self.snapshot([])
                    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.glob("*.md")}
                    _ = self.snapshot([])
                    self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.glob("*.md")}, before)
                    self.assertEqual([(note.label, note.path.name) for note in note_file.read_notes()], [("codex2", "codex2.md")])
                    self.assertIn("Keep this body.", (root / "codex2.md").read_text())

    def test_snapshot_writes_configured_roles_sessions_and_section_without_state(self) -> None:
        note = self.note("claude1.md", "unit@example.invalid", "state: active\n")
        _ = self.note("claude2.md", "spare@example.invalid")
        self.codex_login(self.root / ".codex", "worker@example.invalid")
        _ = self.note("codex2.md", "worker@example.invalid")
        self.select_workers("codex2")
        _ = accounts.set_group_account("units", "claude1", why="fixture", now=self.now)
        _ = accounts.set_group_account("showrunners", "claude1", why="fixture", now=self.now)
        row = session_table.SessionRow("director", "unit", "natedev", "/home/nate/project", "sid-1")
        self.rows.return_value = session_table.ReadSessions({"claude1": (row,)}, frozenset())
        _ = self.snapshot([])
        notes = {current.label: current for current in note_file.read_notes()}
        self.assertEqual(notes["claude1"].get_list("role"), ("showrunner", "unit"))
        self.assertIn("role:\n  - showrunner\n  - unit\n", note.path.read_text())
        self.assertEqual(notes["codex2"].get_list("role"), ("worker",))
        self.assertIn("role: []", notes["claude2"].text)
        self.assertTrue(all(current.get("state") is None for current in notes.values()))
        self.assertEqual(notes["claude1"].get("sessions"), "1")
        self.assertEqual(session_table.section_rows(notes["claude1"].text)[0].session_id, "sid-1")
        self.assertIn("Body stays.", notes["claude1"].text)

    def test_unreadable_group_configuration_and_codex_identity_keep_existing_roles(self) -> None:
        _ = self.note("claude1.md", "unit@example.invalid", "role:\n  - showrunner\n  - unit\n")
        _ = self.note("codex1.md", "worker@example.invalid", "role:\n  - worker\n")
        with patch.object(accounts, "group_accounts", side_effect=accounts.AccountsError("group file unreadable")):
            _ = self.snapshot([])
        notes = {note.label: note for note in note_file.read_notes()}
        self.assertEqual(notes["claude1"].get_list("role"), ("showrunner", "unit"))
        self.assertEqual(notes["codex1"].get_list("role"), ("worker",))

    def test_unchanged_sessions_with_no_fresh_observation_do_not_replace_the_note(self) -> None:
        note = self.note("claude1.md", "unit@example.invalid")
        row = session_table.SessionRow("director", "unit", "natedev", "~/project", "sid-1")
        self.rows.return_value = session_table.ReadSessions({"claude1": (row,)}, frozenset())
        observed = self.now

        class ObservationTime(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> Self:
                return cls.fromtimestamp(observed.timestamp(), tz)

        _ = self.enterContext(patch.object(agent_notes, "datetime", ObservationTime))
        _ = self.snapshot([])
        before, modified = note.path.read_bytes(), note.path.stat().st_mtime_ns
        with patch.object(os, "replace", wraps=os.replace) as replace:
            _ = self.snapshot([])
        self.assertEqual([event for event in replace.call_args_list if Path(cast(str | Path, cast(object, event.args[1]))) == note.path], [])
        self.assertEqual(note.path.read_bytes(), before)
        self.assertEqual(note.path.stat().st_mtime_ns, modified)
        self.assertFalse((self.root / "readings.jsonl").exists())

    def test_fresh_quota_and_table_changes_share_one_note_replace_and_one_reading(self) -> None:
        note = self.note("codex2.md", "worker@example.invalid")
        self.codex_login(self.root / ".codex", "worker@example.invalid")
        self.select_workers("codex2")
        row = session_table.SessionRow("impl", "worker", "natedev", "~/project", "thread-1")
        self.rows.return_value = session_table.ReadSessions({"codex2": (row,)}, frozenset())
        report = Report("Codex", email="worker@example.invalid", source=CodexHomeSource(self.root / ".codex"),
                        quotas=[Quota("Weekly", 40, self.future)], credit_balance="2114")
        for minute, rows in enumerate(((row,), (row,), (row, session_table.SessionRow("test", "worker", "natedev", "~/project", "thread-2")))):
            self.rows.return_value = session_table.ReadSessions({"codex2": rows}, frozenset())
            observed = self.now + timedelta(minutes=minute * 2)
            class ObservationTime(datetime):
                @classmethod
                @override
                def now(cls, tz: tzinfo | None = None) -> Self:
                    return cls.fromtimestamp(observed.timestamp(), tz)
            with patch.object(agent_notes, "datetime", ObservationTime), patch.object(os, "replace", wraps=os.replace) as replace:
                _ = self.snapshot([report])
            note_replaces = [event for event in replace.call_args_list if Path(cast(str | Path, cast(object, event.args[1]))) == note.path]
            self.assertEqual(len(note_replaces), 1)
        readings = [cast(agent_notes.StoredReading, json.loads(line))
                    for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertEqual([reading["account"] for reading in readings], ["codex2", "codex2", "codex2"])
        updated = read_note(note.path)
        assert isinstance(updated, Note)
        self.assertEqual(updated.get("sessions"), "2")
        self.assertEqual(updated.get("credit_balance"), "2114")

    def test_role_or_session_changes_alone_do_not_create_usage_observations(self) -> None:
        note = self.note("claude1.md", "unit@example.invalid")
        _ = self.snapshot([])
        _ = accounts.set_group_account("units", "claude1", why="fixture", now=self.now)
        self.rows.return_value = session_table.ReadSessions({"claude1": (
            session_table.SessionRow("director", "unit", "natedev", "~/project", "sid-1"),)}, frozenset())
        _ = self.snapshot([])
        updated = read_note(note.path)
        assert isinstance(updated, Note)
        self.assertEqual(updated.get_list("role"), ("unit",))
        self.assertEqual(updated.get("sessions"), "1")
        self.assertFalse((self.root / "readings.jsonl").exists())

    def test_unreached_or_pre_upgrade_peer_keeps_its_rows_while_local_rows_are_replaced(self) -> None:
        note = self.note("claude1.md", "unit@example.invalid")
        old = session_table.SessionRow("old local", "unit", "natedev", "~/old", "old-id")
        remote = session_table.SessionRow("remote", "unit", "mac", "~/remote", "mac-id")
        self.rows.return_value = session_table.ReadSessions({"claude1": (old, remote)}, frozenset())
        _ = self.snapshot([])
        local = session_table.SessionRow("new local", "unit", "natedev", "~/new", "new-id")
        self.rows.return_value = session_table.ReadSessions({"claude1": (local,)}, frozenset({"mac"}))
        _ = self.snapshot([])
        updated = read_note(note.path)
        assert isinstance(updated, Note)
        self.assertEqual(updated.get("sessions"), "1")
        self.assertIn("last seen", updated.text)
        self.assertEqual({row.session_id for row in session_table.section_rows(updated.text)}, {"new-id", "mac-id"})
        self.rows.return_value = session_table.SessionsUnread("local registry unreadable")
        before = updated.text
        result = self.snapshot([])
        self.assertEqual(note.path.read_text(), before)
        self.assertTrue(any("sessions: local registry unreadable" in line for line in result.messages))

    def test_machine_observations_replace_once_for_all_notes_and_keep_unreached_times(self) -> None:
        _ = self.note("claude1.md", "unit@example.invalid")
        _ = self.note("claude2.md", "spare@example.invalid")
        path = self.root / "seen.json"
        previous = (self.now - timedelta(hours=2)).isoformat()
        _ = path.write_text(json.dumps({"mac": previous}))
        self.rows.return_value = session_table.ReadSessions({}, frozenset({"mac"}),
                                                            machines_read=frozenset({"natedev"}))
        with patch.object(agent_notes, "SESSIONS_SEEN", path), patch.object(os, "replace", wraps=os.replace) as replace:
            started = datetime.now(timezone.utc).replace(microsecond=0)
            _ = self.snapshot([])
            state_replaces = [event for event in replace.call_args_list if event.args[1] == path]
            self.assertEqual(len(state_replaces), 1)
            source = Path(cast(str | Path, cast(object, state_replaces[0].args[0])))
            self.assertEqual(source.parent, path.parent)
            self.assertFalse(source.exists())
            seen = agent_notes.read_sessions_seen()
            self.assertEqual(seen["mac"], previous)
            self.assertGreaterEqual(datetime.fromisoformat(seen["natedev"]), started)
            self.assertLessEqual(datetime.fromisoformat(seen["natedev"]), datetime.now(timezone.utc))
            self.rows.return_value = session_table.SessionsUnread("registry unavailable")
            before = path.read_bytes()
            _ = self.snapshot([])
            self.assertEqual(path.read_bytes(), before)

    def test_missing_or_damaged_machine_observations_do_not_block_refresh(self) -> None:
        note = self.note("claude1.md", "unit@example.invalid")
        path = self.root / "seen.json"
        self.rows.return_value = session_table.ReadSessions({}, frozenset(), machines_read=frozenset({"natedev"}))
        with patch.object(agent_notes, "SESSIONS_SEEN", path):
            for data in (None, b"not json", b"\xff", b"[]", b'{"mac":"not a time"}',
                         b'{"mac":"2026-10-14T12:00:00"}', b'{"mac":42}'):
                with self.subTest(data=data):
                    path.unlink(missing_ok=True)
                    if data is not None:
                        _ = path.write_bytes(data)
                    messages = self.snapshot([]).messages
                    self.assertFalse(any(message.startswith("sessions seen:") for message in messages))
                    self.assertEqual(set(agent_notes.read_sessions_seen()), {"natedev"})
                    self.assertIn("No live sessions.", note.path.read_text())
            path.unlink()
            path.mkdir()
            messages = self.snapshot([]).messages
            self.assertTrue(any(message.startswith("sessions seen:") for message in messages))
            self.assertIn("No live sessions.", note.path.read_text())
            self.assertEqual(list(self.root.glob(".sessions-seen-*")), [])

    def test_writer_replaces_role_blocks_and_owned_sections_without_disturbing_user_text(self) -> None:
        note = self.note("claude1.md", "unit@example.invalid", "return:\n  - \"[[agents.base]]\"\nrole:\n  - old\n  - orphan\nstate: active\n")
        original = note.text + "\n## Live sessions\n\nOld rows.\n\n## User notes\nKeep below.\n"
        _ = note.path.write_text(original)
        note = read_note(note.path)
        assert isinstance(note, Note)
        section = session_table.SessionsSection(session_table.render_section(()))
        _ = agent_notes.write_note(note, {"role": ("showrunner", "unit"), "sessions": "0"},
                                    remove=frozenset({"state"}), section=section, stamp={"date_modified": "fixture"})
        written = note.path.read_text()
        self.assertIn('return:\n  - "[[agents.base]]"\n', written)
        self.assertNotIn("old\n", written)
        self.assertNotIn("orphan", written)
        self.assertNotIn("state:", written)
        self.assertIn("Body stays.", written)
        self.assertTrue(written.endswith("## User notes\nKeep below.\n"))
        self.assertEqual(note.get_list("role"), ("showrunner", "unit"))
        self.assertEqual(note.path.stat().st_mode & 0o777, 0o644)

    def test_quota_calculation_returns_updates_and_observations_without_writing(self) -> None:
        note = self.note("codex2.md", "worker@example.invalid")
        before = note.path.read_bytes()
        report = Report("Codex", email="worker@example.invalid", quotas=[Quota("Weekly", 40, self.future)], credit_balance="2114")
        with patch.object(os, "replace", wraps=os.replace) as replace:
            result = agent_notes.apply([note], report, self.now)
        replace.assert_not_called()
        self.assertEqual(note.path.read_bytes(), before)
        self.assertEqual(result.updates["codex2"]["weekly_remaining_usage"], "60")
        self.assertEqual([(reading["account"], reading["remaining"]) for reading in result.readings], [("codex2", 60)])
        self.assertFalse((self.root / "readings.jsonl").exists())

    def test_mark_tier_clears_the_former_worker_after_the_role_moves(self) -> None:
        """F002: a worker account swap must not leave the former account marked fast."""
        former = self.note("codex1.md", "former@example.invalid", "role: []\ntier: fast\n")
        selected = self.note("codex2.md", "worker@example.invalid", "role:\n  - worker\ntier: null\n")
        with patch.object(codex_pacer, "registry_tier", return_value="default"), \
             patch.object(os, "replace", wraps=os.replace) as replace:
            agent_notes.mark_tier(cast(codex_pacer.Plan, cast(object, {"tier": "default"})))
        former_after, selected_after = read_note(former.path), read_note(selected.path)
        assert isinstance(former_after, Note) and isinstance(selected_after, Note)
        self.assertEqual(former_after.get("tier"), "null")
        self.assertEqual(selected_after.get("tier"), "regular")
        self.assertEqual([event.args[1] for event in replace.call_args_list], [former.path, selected.path])

    def test_argument_free_notes_update_runs_watcher_after_notes_alert_and_pacer(self) -> None:
        events: list[str] = []
        update = agent_notes.update_alert_pass
        def updated(selection: agent_accounts.ClaudeAccountsInUse, home: Path, *, force_usage: bool = False) -> agent_notes.UpdatedAgentNotes:
            events.append("notes")
            return update(selection, home, force_usage=force_usage)
        def alerted(*_args: object, **_kwargs: object) -> list[str]:
            events.append("alert")
            return []
        def paced(*_args: object, **_kwargs: object) -> None:
            events.append("pacer")
        def watched(*_args: object, **_kwargs: object) -> list[str]:
            events.append("watcher")
            return ["fixture watcher decision"]
        with (patch.object(sys, "argv", ["agent_notes.py"]),
              patch.object(agent_notes, "live_reports", return_value=[]),
              patch.object(five_hour, "watch", return_value=[]),
              patch.object(agent_notes, "update_alert_pass", side_effect=updated),
              patch.object(agent_notes, "alert", side_effect=alerted),
              patch.object(codex_pacer, "tick", side_effect=paced),
              patch.object(agent_notes, "mark_tier"),
              patch.object(refusal_watch, "run", side_effect=watched), redirect_stdout(io.StringIO()) as output):
            agent_notes.main()
        self.assertEqual(events, ["notes", "alert", "pacer", "watcher"])
        self.assertIn("fixture watcher decision", output.getvalue())

    def test_watcher_failure_prints_one_stderr_line_without_failing_notes_update(self) -> None:
        with (patch.object(sys, "argv", ["agent_notes.py"]),
              patch.object(agent_notes, "live_reports", return_value=[]),
              patch.object(five_hour, "watch", return_value=[]),
              patch.object(agent_notes, "alert", return_value=[]),
              patch.object(codex_pacer, "tick"), patch.object(agent_notes, "mark_tier"),
              patch.object(refusal_watch, "run", side_effect=OSError("watch fixture unavailable")),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error):
            agent_notes.main()
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        self.assertIn("watch fixture unavailable", error.getvalue())

    def test_explicit_refresh_does_not_run_automatic_refusal_watcher(self) -> None:
        with (patch.object(sys, "argv", ["agent_notes.py", "refresh"]),
              patch.object(agent_notes, "live_reports", return_value=[]),
              patch.object(five_hour, "watch", return_value=[]),
              patch.object(agent_notes, "refresh", return_value=[]),
              patch.object(agent_notes, "current_session", return_value=None),
              patch.object(codex_pacer, "tick"), patch.object(agent_notes, "mark_tier"),
              patch.object(refusal_watch, "run") as watcher, redirect_stdout(io.StringIO())):
            agent_notes.main()
        watcher.assert_not_called()

    def note(self, name: str, email: str, extra: str = "") -> Note:
        path = self.root / name
        _ = path.write_text(f'---\nlogin: {email}\n{extra}---\nBody stays.\n')
        path.chmod(0o644)
        note = read_note(path)
        if not isinstance(note, Note):
            self.fail(f"{name} did not parse as a note")
        return note

    def account_dir(self, label: str, login: str, token: str, *, subscription_type: str = "") -> Path:
        if label == "default":
            directory = self.root / ".claude"
            directory.mkdir(exist_ok=True)
            config = self.root / ".claude.json"
        else:
            self.assertEqual(accounts.setup(label), [])
            directory = accounts.account_dir(label)
            config = directory / ".claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
        _ = (directory / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": token, "subscriptionType": subscription_type},
        }))
        return directory

    def select_unit(self, label: str) -> None:
        """Represent a live local session on this account for the activity snapshot."""
        from scripts.message import sessions
        identity = accounts.account_identity(accounts.account_dir(label))
        if isinstance(identity, agent_accounts.UnnotedLogin):
            _ = self.note(note_file.note_name(label), identity.login)
            identity = accounts.account_identity(accounts.account_dir(label))
        assert isinstance(identity, agent_accounts.NumberedAccount)
        _ = self.enterContext(patch.object(sessions, "live_sessions", return_value=[sessions.SessionRecord(
            pid=43210, sessionId="activity-fixture", name="live unit", messagingSocketPath="/fixture.sock",
            updatedAt=1, tmux="", formerNames=[])]))
        _ = self.enterContext(patch.object(account, "account_of",
            return_value=account.Account("claude", identity.login, identity.label)))

    def select_workers(self, label: str) -> None:
        self.assertEqual(accounts.main(["set-role", "workers", label]), 0)

    def codex_login(self, home: Path, login: str) -> None:
        home.mkdir(exist_ok=True)
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({
            "tokens": {"id_token": f"header.{claims}.signature"},
        }))

    def test_two_minute_snapshots_preserve_the_user_credit_setting_line_in_place(self) -> None:
        for setting in ("off", "on"):
            with self.subTest(setting=setting):
                path = self.root / "codex2.md"
                original = ("---\ncredit_balance: 2115\ndate_modified: old\nlimit_reset: null\n"
                    + "limit_reset_count: 0\nlogin: snapshot@example.invalid\nresets: old\nrole: []\n"
                    + f"# My credit policy\nuse_credits: {setting}\n# Keep the policy here\n"
                    + "weekly_remaining_usage: 98\nweekly_usage_checked_at: old\n---\nBody stays.\n").encode()
                _ = path.write_bytes(original)
                policy = f"use_credits: {setting}\n".encode()
                policy_index = original.splitlines(keepends=True).index(policy)
                for minute, used, credits in ((0, 3, "2114"), (2, 4, "2113")):
                    note = read_note(path)
                    assert isinstance(note, Note)
                    observed = self.now + timedelta(minutes=minute)
                    report = Report("Codex", email="snapshot@example.invalid",
                        source=CodexHomeSource(self.root / ".codex"),
                        quotas=[Quota("Weekly", used, self.future)], credit_balance=credits,
                        limit_reset_count=1)
                    messages = apply_quota_and_write([note], report, observed)
                    self.assertTrue(messages, "the usage observation must actually rewrite the note")
                    rewritten = path.read_bytes()
                    lines = rewritten.splitlines(keepends=True)
                    self.assertEqual(lines.count(policy), 1)
                    self.assertEqual(lines[policy_index], policy)
                    self.assertEqual(lines[policy_index - 1:policy_index + 2],
                                     original.splitlines(keepends=True)[policy_index - 1:policy_index + 2])
                    self.assertIn(b"Body stays.", rewritten)
                    updated = read_note(path)
                    assert isinstance(updated, Note)
                    self.assertEqual(updated.get("credit_balance"), credits)
                    self.assertEqual(updated.get("weekly_remaining_usage"), str(100 - used))
                    self.assertEqual(updated.get("weekly_usage_checked_at"), observed.isoformat(timespec="seconds"))
                    self.assertEqual(updated.get("use_credits"), setting)

    def test_update_passes_one_frozen_codex_reading_to_watch_alert_and_refresh(self) -> None:
        default, spare = self.root / ".codex", self.root / ".codex2"
        self.codex_login(default, "workers@example.com")
        self.codex_login(spare, "spare@example.com")
        _ = self.note("codex1.md", "workers@example.com")
        _ = self.note("codex2.md", "spare@example.com")
        reports = [Report("Codex", email="workers@example.com", source=CodexHomeSource(default)),
                   Report("Codex", email="spare@example.com", source=CodexHomeSource(spare))]

        async def collect(selection: agent_accounts.ClaudeAccountsInUse, workers_home: Path) -> list[Report]:
            del selection
            self.assertEqual(workers_home, default)
            self.select_workers("codex2")
            return reports

        passes: list[quota_alert.CodexAlertPassReadings] = []

        def receive(*args: object, **kwargs: object) -> list[str]:
            del args
            value = kwargs.get("codex_readings")
            self.assertIsInstance(value, quota_alert.CodexAlertPassReadings)
            assert isinstance(value, quota_alert.CodexAlertPassReadings)
            passes.append(value)
            return []

        for verb in ([], ["refresh"]):
            self.select_workers("codex1")
            passes.clear()
            with (self.subTest(verb=verb),
                  patch.object(sys, "argv", ["agent_notes.py", *verb]),
                  patch.object(agent_notes, "live_reports", side_effect=collect),
                  patch.object(five_hour, "watch", side_effect=receive),
                  patch.object(agent_notes, "alert", side_effect=receive),
                  patch.object(agent_notes, "refresh", side_effect=receive),
                  redirect_stdout(io.StringIO())):
                agent_notes.main()
            self.assertEqual(len(passes), 2)
            self.assertIs(passes[0], passes[1])
            assert isinstance(passes[0].workers, accounts.CodexWorkersAccount)
            self.assertEqual(passes[0].workers.home, default)
            self.assertEqual([reading.home for reading in passes[0].checked_homes], [default, spare])
            self.assertTrue(all(reading.observed_at.tzinfo is not None for reading in passes[0].checked_homes))

    def test_blocked_cli_passes_explicit_window_and_aware_refusal_time(self) -> None:
        refused_at = self.now - timedelta(seconds=3)
        with (patch.object(sys, "argv", ["agent_notes.py", "blocked", str(self.root / ".codex"),
                  "workers@example.invalid", "codex 1", "--window", "five_hour", "--refused-at", refused_at.isoformat()]),
              patch.object(agent_notes, "blocked", return_value=[]) as blocked,
              patch.object(note_file, "read_notes", return_value=[])):
            agent_notes.main()
        blocked.assert_called_once()
        self.assertEqual(blocked.call_args.args[1],
                         agent_accounts.KnownCodexAccount(self.root / ".codex", "workers@example.invalid", "codex1"))
        self.assertEqual(blocked.call_args.kwargs["window"], refusals.KnownWindow("five_hour"))
        self.assertEqual(blocked.call_args.kwargs["refused_at"], quota_alert.RefusedAt(refused_at))

    def test_blocked_cli_missing_time_has_explicit_unknown_refusal_time(self) -> None:
        with (patch.object(sys, "argv", ["agent_notes.py", "blocked", str(self.root / ".codex"),
                  "workers@example.invalid", "codex1"]),
              patch.object(agent_notes, "blocked", return_value=[]) as blocked,
              patch.object(note_file, "read_notes", return_value=[])):
            agent_notes.main()
        self.assertEqual(blocked.call_args.kwargs["refused_at"], quota_alert.RefusalTimeUnknown())
        self.assertEqual(blocked.call_args.kwargs["stopped"], quota_alert.StoppedCommandUnknown())

    def test_blocked_cli_converts_stopped_worker_command_at_the_argument_boundary(self) -> None:
        command = "codex_mesh follow --to impl --prompt 'finish the patch'"
        with (patch.object(sys, "argv", ["agent_notes.py", "blocked", str(self.root / ".codex"),
                  "workers@example.invalid", "codex1", "--stopped", "own-director", "impl", command]),
              patch.object(agent_notes, "blocked", return_value=[]) as blocked,
              patch.object(note_file, "read_notes", return_value=[])):
            agent_notes.main()
        blocked.assert_called_once()
        self.assertEqual(blocked.call_args.kwargs["stopped"],
                         quota_alert.StoppedWorkerCommand("own-director", "impl", command))

    def test_blocked_cli_rejects_incomplete_stopped_context_before_reporting(self) -> None:
        with (patch.object(sys, "argv", ["agent_notes.py", "blocked", str(self.root / ".codex"),
                  "workers@example.invalid", "codex1", "--stopped", "own-director", "impl"]),
              patch.object(agent_notes, "blocked") as blocked,
              redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exited):
            agent_notes.main()
        self.assertEqual(exited.exception.code, 2)
        blocked.assert_not_called()

    def test_blocked_cli_rejects_naive_refusal_time_before_reporting(self) -> None:
        with (patch.object(sys, "argv", ["agent_notes.py", "blocked", str(self.root / ".codex"),
                  "workers@example.invalid", "codex1", "--refused-at", "2026-10-13T12:00:00"]),
              patch.object(agent_notes, "blocked") as blocked,
              redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exited):
            agent_notes.main()
        self.assertEqual(exited.exception.code, 2)
        blocked.assert_not_called()

    def test_update_writes_each_codex_home_and_marks_only_the_selected_worker_note(self) -> None:
        default = self.root / ".codex"
        spare = self.root / ".codex2"
        self.codex_login(default, "workers@example.com")
        self.codex_login(spare, "spare@example.com")
        _ = self.note("codex1.md", "WORKERS@example.com")
        _ = self.note("codex2.md", "spare@example.com")
        _ = self.note("codex3.md", "missing@example.com")
        self.select_workers("codex1")
        workers_report = Report("Codex", email="workers@example.com", source=CodexHomeSource(default),
            quotas=[Quota("Weekly", 20, self.future)], credit_balance="7", limit_reset_count=1,
            limit_reset_expirations=[self.future])
        spare_report = Report("Codex", email="spare@example.com", source=CodexHomeSource(spare),
            quotas=[Quota("Weekly", 95, self.future)], credit_balance="23", limit_reset_count=2,
            limit_reset_expirations=[self.future])

        for reports in ([workers_report, spare_report], [spare_report, workers_report]):
            with (
                self.subTest(order=[report.email for report in reports]),
                patch.object(usage_store, "recent_timer_usage", return_value=usage_store.TimerUsageDue()),
                patch.object(agent_notes, "live_reports", return_value=reports),
                patch.object(five_hour, "watch", return_value=[]) as watch,
            ):
                _ = agent_notes.update()
            notes = {note.label: note for note in note_file.read_notes()}
            self.assertEqual({label: note.get("weekly_remaining_usage") for label, note in notes.items()}, {
                "codex1": "80", "codex2": "5", "codex3": "null",
            })
            self.assertEqual(notes["codex1"].get("credit_balance"), "7")
            self.assertEqual(notes["codex2"].get("credit_balance"), "23")
            self.assertEqual(notes["codex1"].get("limit_reset_count"), "1")
            self.assertEqual(notes["codex2"].get("limit_reset_count"), "2")
            self.assertNotIn(notes["codex1"].get("weekly_usage_checked_at"), (None, "null"))
            self.assertNotIn(notes["codex2"].get("weekly_usage_checked_at"), (None, "null"))
            self.assertEqual(watch.call_count, 1)
            self.assertEqual(watch.call_args.args[0], [workers_report])
        stored = usage_store.load()
        workers_usage = stored[usage_store.key("Codex", "workers@example.com")]
        spare_usage = stored[usage_store.key("Codex", "spare@example.com")]
        if "weekly" not in workers_usage or "weekly" not in spare_usage:
            self.fail("Both signed-in homes must save their weekly readings")
        self.assertEqual(workers_usage["weekly"]["left"], 80)
        self.assertEqual(spare_usage["weekly"]["left"], 5)
        readings = [cast(agent_notes.StoredReading, json.loads(line))
                    for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertEqual({reading["account"] for reading in readings}, {"codex1", "codex2"})

    def test_update_follows_the_codex_home_in_use_for_worker_role(self) -> None:
        default = self.root / ".codex"
        selected = self.root / ".codex2"
        self.codex_login(default, "default@example.com")
        self.codex_login(selected, "selected@example.com")
        _ = self.note("codex1.md", "default@example.com")
        _ = self.note("codex2.md", "selected@example.com")
        default_report = Report("Codex", email="default@example.com", source=CodexHomeSource(default),
            quotas=[Quota("Weekly", 10, self.future)])
        self.select_workers("codex2")
        selected_report = Report("Codex", email="selected@example.com", source=CodexHomeSource(selected),
            quotas=[Quota("Weekly", 30, self.future)])
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(selected)}),
            patch.object(agent_notes, "live_reports", return_value=[selected_report, default_report]),
            patch.object(five_hour, "watch", return_value=[]) as watch,
        ):
            _ = agent_notes.update()
        notes = {note.label: note for note in note_file.read_notes()}
        self.assertEqual(notes["codex1"].get("weekly_remaining_usage"), "90")
        self.assertEqual(notes["codex2"].get("weekly_remaining_usage"), "70")
        self.assertEqual(watch.call_count, 1)
        self.assertEqual(watch.call_args.args[0], [selected_report])

    def test_five_hour_watch_selects_codex_by_home_even_with_shared_or_absent_login(self) -> None:
        default = self.root / ".codex"
        spare = self.root / ".codex2"
        self.codex_login(default, "shared@example.com")
        _ = self.note("codex1.md", "shared@example.com")
        self.select_workers("codex1")
        # A later duplicate sign-in must not change this pass's selected home.
        self.codex_login(spare, "shared@example.com")
        workers_report = Report("Codex", email="shared@example.com", source=CodexHomeSource(default))
        spare_report = Report("Codex", email="shared@example.com", source=CodexHomeSource(spare))
        for current_login in ("shared@example.com", None):
            workers_report.email = current_login
            with (
                self.subTest(current_login=current_login),
                patch.object(usage_store, "recent_timer_usage", return_value=usage_store.TimerUsageDue()),
                patch.object(agent_notes, "live_reports", return_value=[spare_report, workers_report]),
                patch.object(five_hour, "watch", return_value=[]) as watch,
            ):
                _ = agent_notes.update()
            self.assertEqual(watch.call_count, 1)
            self.assertEqual(watch.call_args.args[0], [workers_report])

    def test_failed_selected_codex_read_preserves_activity_and_alert_episode(self) -> None:
        default = self.root / ".codex"
        selected = self.root / ".codex1"
        self.codex_login(default, "spare@example.com")
        self.codex_login(selected, "WORKERS@example.com")
        workers = self.note("codex1.md", "workers@example.com")
        _ = self.note("codex2.md", "spare@example.com")
        _ = apply_quota_and_write([workers], Report("Codex", email="workers@example.com",
            quotas=[Quota("Weekly", 100, self.future)]), self.now)
        self.select_workers("codex1")
        _ = agent_notes.write_note(workers, {"role": ("worker",)}, stamp={"date_modified": "fixture"})
        stamp = self.now.isoformat(timespec="seconds")
        episode: quota_alert.Episode = {"since": stamp, "last": {}}
        state: quota_alert.State = {"episodes": {"codex1": episode.copy()},
                                    "fallback": quota_alert.NoCodexQuotaFallback()}
        workers_report = Report("Codex", problem="Account unavailable", source=CodexHomeSource(selected))
        spare_report = Report("Codex", email="spare@example.com", source=CodexHomeSource(default),
            quotas=[Quota("Weekly", 95, self.future)], credit_balance="23")
        selection = agent_accounts.ClaudeAccountsInUse(
            agent_accounts.UnreadableIdentity(Path.home() / ".claude", "fixture identity unavailable"), False, (), frozenset(),
        )

        with (
            patch.dict(os.environ, {"CODEX_HOME": str(selected)}),
            patch.object(agent_notes, "live_reports", return_value=[spare_report, workers_report]),
            patch.object(five_hour, "watch", return_value=[]) as watch,
        ):
            _ = agent_notes.update_for_selection(selection, agent_accounts.observe_workers_home())

        notes = {note.label: note for note in note_file.read_notes()}
        self.assertEqual(notes["codex1"].get_list("role"), ("worker",))
        self.assertEqual(notes["codex1"].get("weekly_remaining_usage"), "0")
        self.assertEqual(notes["codex2"].get("weekly_remaining_usage"), "5")
        self.assertEqual(notes["codex2"].get("credit_balance"), "23")
        self.assertEqual(quota_alert.close_recovered(state, list(notes.values()), 5, selection, self.now,
            quota_alert.codex_alert_pass(selected, [spare_report, workers_report], self.now)), [])
        self.assertEqual(state["episodes"], {"codex1": episode})
        self.assertEqual(watch.call_count, 1)
        self.assertEqual(watch.call_args.args[0], [workers_report])

    def test_unreadable_selected_codex_login_preserves_every_note_role(self) -> None:
        default = self.root / ".codex"
        selected = self.root / ".codex1"
        self.codex_login(default, "spare@example.com")
        self.codex_login(selected, "workers@example.com")
        workers = self.note("codex1.md", "workers@example.com")
        spare = self.note("codex2.md", "spare@example.com")
        _ = self.note("codex3.md", "absent@example.com")
        _ = apply_quota_and_write([workers], Report("Codex", email="workers@example.com",
            quotas=[Quota("Weekly", 100, self.future)]), self.now)
        _ = apply_quota_and_write([spare], Report("Codex", email="spare@example.com",
            quotas=[Quota("Weekly", 90, self.future)]), self.now)
        self.select_workers("codex1")
        _ = (selected / "auth.json").write_text("not json")
        workers_report = Report("Codex", problem="Account unavailable", source=CodexHomeSource(selected))
        spare_report = Report("Codex", email="spare@example.com", source=CodexHomeSource(default),
            quotas=[Quota("Weekly", 95, self.future)])

        for reports in ([spare_report, workers_report], [workers_report, spare_report]):
            with (
                self.subTest(order=[report.source for report in reports]),
                patch.dict(os.environ, {"CODEX_HOME": str(selected)}),
                patch.object(agent_notes, "live_reports", return_value=reports),
                patch.object(five_hour, "watch", return_value=[]) as watch,
            ):
                _ = agent_notes.update()
            notes = {note.label: note for note in note_file.read_notes()}
            self.assertEqual(notes["codex1"].get("weekly_remaining_usage"), "0")
            self.assertEqual(notes["codex2"].get("weekly_remaining_usage"), "5")
            self.assertEqual(watch.call_count, 1)
            self.assertEqual(watch.call_args.args[0], [workers_report])

    def test_update_names_the_logged_out_codex_spare_home(self) -> None:
        default = self.root / ".codex"
        spare = self.root / ".codex1"
        self.codex_login(default, "workers@example.com")
        spare.mkdir()
        _ = self.note("codex2.md", "workers@example.com")
        reports = [
            Report("Codex", email="workers@example.com", source=CodexHomeSource(default)),
            Report("Codex", problem="Not logged in", source=CodexHomeSource(spare)),
        ]
        with (
            patch.object(agent_notes, "live_reports", return_value=reports),
            patch.object(five_hour, "watch", return_value=[]),
        ):
            messages = agent_notes.update()
        self.assertIn("Codex (~/.codex1): Not logged in", messages)
        self.assertNotIn("Codex: Not logged in", messages)

    def test_update_with_one_logged_out_codex_home_keeps_the_original_message(self) -> None:
        _ = self.note("codex1.md", "workers@example.com")
        report = Report("Codex", problem="Not logged in", source=CodexHomeSource(self.root / ".codex"))
        with (
            patch.object(agent_notes, "live_reports", return_value=[report]),
            patch.object(five_hour, "watch", return_value=[]),
        ):
            messages = agent_notes.update()
        self.assertEqual(messages[-1], "Codex: Not logged in")

    def test_update_with_one_codex_home_keeps_observations_and_messages(self) -> None:
        home = self.root / ".codex"
        self.codex_login(home, "workers@example.com")
        current = self.note("codex1.md", "workers@example.com")
        self.select_workers("codex1")
        spare = self.note("codex2.md", "spare@example.com")
        report = Report("Codex", email="workers@example.com", source=CodexHomeSource(home),
            quotas=[Quota("Weekly", 20, self.future)], credit_balance="7")
        checked_at = self.now

        class FixedNoteCheckTime(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> Self:
                return cls.fromtimestamp(checked_at.timestamp(), tz)

        with (
            patch.object(agent_notes, "live_reports", return_value=[report]),
            patch.object(agent_notes, "datetime", FixedNoteCheckTime),
            patch.object(five_hour, "watch", return_value=[]) as watch,
        ):
            messages = agent_notes.update()
        self.assertTrue(any("credit_balance=7" in line for line in messages))
        self.assertFalse(any("state=" in line for line in messages))
        updated = {note.label: note for note in note_file.read_notes()}
        self.assertEqual(updated[current.label].get("weekly_remaining_usage"), "80")
        self.assertTrue(all("Body stays." in note.path.read_text() for note in (current, spare)))
        self.assertEqual(watch.call_count, 1)
        self.assertEqual(watch.call_args.args[0], [report])

    def test_notes_and_watch_keep_one_workers_role_through_a_role_change_during_read(self) -> None:
        default = self.root / ".codex"
        selected = self.root / ".codex1"
        next_home = self.root / ".codex3"
        for home, login, label in ((default, "default@example.com", "codex2"),
            (selected, "selected@example.com", "codex1"), (next_home, "next@example.com", "codex3")):
            self.codex_login(home, login)
            _ = self.note(note_name(label), login)
        self.select_workers("codex1")
        reports = [Report("Codex", email="selected@example.com", source=CodexHomeSource(selected),
            quotas=[Quota("Weekly", 100, self.future)]),
            Report("Codex", email="next@example.com", source=CodexHomeSource(next_home),
                quotas=[Quota("Weekly", 10, self.future)])]
        async def read(_selection: agent_accounts.ClaudeAccountsInUse, workers_home: Path) -> list[Report]:
            self.assertEqual(workers_home, selected)
            self.select_workers("codex3")
            return reports
        with patch.dict(os.environ, {"CODEX_HOME": str(default)}), patch.object(agent_notes, "live_reports", side_effect=read), patch.object(five_hour, "watch", return_value=[]) as watch:
            _ = agent_notes.update()
        notes = {note.label: note for note in note_file.read_notes()}
        self.assertEqual(notes["codex3"].get("weekly_remaining_usage"), "90")
        self.assertEqual(watch.call_count, 1)
        self.assertEqual(watch.call_args.args[0], [reports[0]])

    def test_update_reads_two_active_accounts_and_an_inactive_spare(self) -> None:
        default = self.account_dir("default", "main@example.com", "main-fixture-token", subscription_type="max")
        unit = self.account_dir("claude3", "unit@example.com", "unit-fixture-token", subscription_type="max")
        spare = self.account_dir("claude1", "spare@example.com", "spare-fixture-token", subscription_type="max")
        (self.root / ".claude9").mkdir()
        self.select_unit("claude3")
        _ = self.note("claude2.md", "main@example.com")
        _ = self.note("claude3.md", "unit@example.com")
        _ = self.note("claude1.md", "spare@example.com")
        identities = {default: "main@example.com", unit: "unit@example.com", spare: "spare@example.com"}
        tokens = {"Bearer main-fixture-token": 20, "Bearer unit-fixture-token": 96, "Bearer spare-fixture-token": 10}
        observed: list[Path] = []

        def status(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command, ("fixture-claude", "auth", "status"))
            environment = cast(dict[str, str], kwargs["env"])
            directory = Path(environment.get("CLAUDE_CONFIG_DIR", str(default)))
            observed.append(directory)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "loggedIn": True, "email": identities[directory], "authMethod": "claude.ai",
            }), "")

        def usage(request: urllib.request.Request, timeout: int) -> io.BytesIO:
            self.assertEqual(timeout, 15)
            return io.BytesIO(json.dumps({"seven_day": {
                "utilization": tokens[request.get_header("Authorization") or ""],
                "resets_at": self.future.isoformat(),
            }}).encode())

        with (
            patch.object(account, "process_config_dir", return_value=account.NoAccountSetting()),
            patch.object(agent_accounts, "executable", return_value="fixture-claude"),
            patch.object(subprocess, "run", side_effect=status),
            patch.object(urllib.request, "urlopen", side_effect=usage),
            patch.object(agent_accounts, "codex_live", return_value=Report("Codex", problem="fixture")),
            patch.object(five_hour, "watch", return_value=[]) as watch,
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(spare), "CLAUDE_CODE_OAUTH_TOKEN": "inherited-token"}),
        ):
            _ = agent_notes.update()

        self.assertEqual(observed, [])
        notes = {note.label: note for note in note_file.read_notes()}
        self.assertEqual({label: note.get("weekly_remaining_usage") for label, note in notes.items()}, {
            "claude1": "90", "claude2": "80", "claude3": "4",
        })
        readings = [cast(agent_notes.StoredReading, json.loads(line))
                    for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertCountEqual([(reading["account"], reading["remaining"]) for reading in readings],
                              [("claude2", 80), ("claude3", 4), ("claude1", 90)])
        watched = cast(list[Report], watch.call_args.args[0])
        self.assertEqual({report.plan for report in watched}, {"max"})
        self.assertEqual([(report.tool, report.email) for report in watched],
                         [("Claude", "unit@example.com"), ("Claude", "main@example.com")])
        self.assertEqual(watched[1].source, ClaudeDirectorySource(default))

    def test_two_account_fixture_writes_two_readings(self) -> None:
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude3", "unit@example.com", "unit-token")
        self.select_unit("claude3")
        _ = self.note("claude2.md", "main@example.com")
        _ = self.note("claude3.md", "unit@example.com")
        reports = [Report("Claude", email=email, quotas=[Quota("Weekly", used, self.future)])
                   for email, used in (("main@example.com", 20), ("unit@example.com", 96))]
        with (patch.object(agent_notes, "live_reports", return_value=reports),
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update()
        self.assertEqual(len((self.root / "readings.jsonl").read_text().splitlines()), 2)
        self.assertEqual(
            {login: usage.get("weekly", {}).get("left") for login, usage in usage_store.load().items()},
            {"main@example.com": 80.0, "unit@example.com": 4.0},
        )
        by_label = (self.root / ".local/state/agent-notes/by-label.json").read_text(encoding="utf-8")
        self.assertEqual(
            {label: usage["weekly"]["left"]
             for label, usage in cast(dict[str, dict[str, dict[str, float]]], json.loads(by_label)).items()},
            {"claude2": 80.0, "claude3": 4.0},
        )
        self.assertNotIn("@", by_label)

    def test_update_keeps_its_live_login_snapshot_when_sessions_change_mid_read(self) -> None:
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude1", "old-unit@example.com", "old-token")
        _ = self.account_dir("claude3", "new-unit@example.com", "new-token")
        self.select_unit("claude1")
        for label, login in (("claude1", "old-unit@example.com"), ("claude2", "main@example.com"),
                             ("claude3", "new-unit@example.com")):
            _ = self.note(note_name(label), login)
        resolve = accounts.claude_account_selection

        def changed_after_snapshot() -> agent_accounts.ClaudeAccountsInUse:
            selection = resolve()
            self.select_unit("claude3")
            return selection

        reports = [Report("Claude", email=login, quotas=[Quota("Weekly", 20, self.future)])
                   for login in ("old-unit@example.com", "main@example.com", "new-unit@example.com")]
        with (patch.object(accounts, "claude_account_selection", side_effect=changed_after_snapshot) as snapshot,
              patch.object(agent_notes, "live_reports", return_value=reports),
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update()
        snapshot.assert_called_once_with()

    def test_sessions_on_multiple_logins_all_stay_visible_in_notes_and_whoami(self) -> None:
        from scripts.message import sessions
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude1", "first@example.com", "first-token")
        _ = self.account_dir("claude3", "third@example.com", "third-token")
        for label, login in (("claude1", "first@example.com"), ("claude2", "main@example.com"),
                             ("claude3", "third@example.com")):
            _ = self.note(note_name(label), login)
        owners = [sessions.SessionRecord(pid=pid, sessionId=str(pid), name=str(pid),
                  messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[])
                  for pid in (10001, 10003, 10009)]
        identities = {10001: account.Account("claude", "FIRST@example.com", "claude1"),
                      10003: account.Account("claude", "third@example.com", "claude3")}
        reports = [Report("Claude", email=login, quotas=[Quota("Weekly", 20, self.future)])
                   for login in ("main@example.com", "first@example.com", "third@example.com")]
        def observed_account(pid: int, *, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome) -> account.Account | None:
            identity = identities.get(pid)
            if (identity is not None and isinstance(home_state, account.LegacyHomeLogin)
                    and identity.login.casefold() == home_state.account.login.casefold()):
                return home_state.account
            return identity
        with (patch.object(sessions, "live_sessions", return_value=owners),
              patch.object(account, "account_of", side_effect=observed_account),
              patch.object(agent_notes, "live_reports", return_value=reports),
              patch.object(five_hour, "watch", return_value=[])):
            selection = accounts.claude_account_selection()
            _ = agent_notes.update_for_selection(selection, agent_accounts.observe_workers_home())
            output = io.StringIO()
            with patch.object(whoami, "live_reports", return_value=reports), redirect_stdout(output):
                asyncio.run(whoami.main())
        for label in ("claude1", "claude2", "claude3"):
            self.assertIn(label, output.getvalue())

    def test_live_unfit_and_external_accounts_are_collected_once_and_remain_visible(self) -> None:
        from scripts.message import sessions
        default = self.account_dir("default", "main@example.com", "main-token")
        unfit = self.account_dir("claude3", "unfit@example.com", "unfit-token")
        (unfit / "projects").unlink()
        _ = (unfit / "projects").write_text("local file against the shared projects folder")
        external = self.root / "external account"
        external.mkdir()
        _ = (external / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "external@example.com"}}))
        for label, login in (("claude2", "main@example.com"), ("claude3", "unfit@example.com"),
                             ("claude4", "external@example.com")):
            _ = self.note(note_name(label), login)
        directories = {10001: unfit, 10002: external, 10003: external, 10004: default}
        owners = [sessions.SessionRecord(pid=pid, sessionId=str(pid), name=str(pid),
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]) for pid in directories]
        logins = {default: "main@example.com", unfit: "unfit@example.com", external: "external@example.com"}
        def process_directory(pid: int) -> account.ConfiguredClaudeFolder | account.ProcessEnvironmentUnread:
            directory = directories.get(pid)
            return account.ConfiguredClaudeFolder(directory) if directory is not None else account.ProcessEnvironmentUnread("fixture")
        def process_account(pid: int, *, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome) -> account.Account | None:
            directory = directories.get(pid)
            if directory == Path.home() / ".claude" and isinstance(home_state, account.LegacyHomeLogin):
                return home_state.account
            return account.Account("claude", logins[directory], "fixture") if directory is not None else None
        def live(directory: Path, *, use_disk_identity: bool = False) -> Report:
            self.assertTrue(use_disk_identity)
            return Report("Claude", email=logins[directory], quotas=[Quota("Weekly", 96, self.future)],
                          source=agent_accounts.ClaudeDirectorySource(directory))
        with (patch.object(sessions, "live_sessions", return_value=owners) as observed_sessions,
              patch.object(account, "process_config_dir", side_effect=process_directory) as observed_directories,
              patch.object(account, "account_of", side_effect=process_account)):
            selection = accounts.claude_account_selection()
        observed_sessions.assert_called_once_with()
        self.assertEqual(observed_directories.call_args_list, [call(pid) for pid in directories])
        self.assertCountEqual(selection.live_directories, logins)
        self.assertEqual(selection.named_accounts, ())
        self.assertTrue(selection.is_in_use("external@example.com"))
        with (patch.object(agent_accounts, "claude_live", side_effect=live) as reads,
              patch.object(agent_accounts, "codex_homes", return_value=[]),
              patch.object(accounts, "claude_account_selection", return_value=selection),
              patch.object(five_hour, "watch", return_value=[]),
              patch.object(agent_notes, "alert", return_value=[])):
            _ = agent_notes.update_for_selection(selection, self.root / ".codex")
            self.assertCountEqual(reads.call_args_list, [call(directory, use_disk_identity=True) for directory in logins])
            reads.reset_mock()
            output = io.StringIO()
            with redirect_stdout(output):
                asyncio.run(whoami.main())
            self.assertCountEqual(reads.call_args_list, [call(directory, use_disk_identity=True) for directory in logins])
        for login in logins.values():
            self.assertIn(login, output.getvalue())

    def test_five_hour_watch_uses_live_logins_without_privileging_the_config_home(self) -> None:
        """A numbered account in use is eligible independently of the config home."""
        default = self.account_dir("default", "shared@example.com", "main-token")
        named = self.account_dir("claude3", "shared@example.com", "named-token")
        self.select_unit("claude3")
        _ = self.note("claude2.md", "shared@example.com")
        _ = self.note("claude3.md", "shared@example.com")
        main = Report("Claude", email="shared@example.com", source=ClaudeDirectorySource(default))
        unit = Report("Claude", email="shared@example.com", source=ClaudeDirectorySource(named))
        codex = Report("Codex")
        for main_email in ("shared@example.com", None):
            main.email = main_email
            with (
                self.subTest(main_email=main_email),
                patch.object(usage_store, "recent_timer_usage", return_value=usage_store.TimerUsageDue()),
                patch.object(agent_notes, "live_reports", return_value=[unit, codex, main]),
                patch.object(five_hour, "watch", return_value=[]) as watch,
            ):
                _ = agent_notes.update()
            watched = cast(list[Report], watch.call_args.args[0])
            self.assertEqual(watched, [unit, main] if main_email else [unit])

    def test_updater_and_alert_share_the_same_role_snapshot(self) -> None:
        """The timer entry point resolves roles once for writes and notices."""
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude3", "unit@example.com", "unit-token")
        self.select_unit("claude3")
        _ = self.note("claude2.md", "main@example.com")
        _ = self.note("claude3.md", "unit@example.com")
        selection = accounts.claude_account_selection()
        reports = [Report("Claude", email=login, quotas=[Quota("Weekly", 20, self.future)])
                   for login in ("main@example.com", "unit@example.com")]
        with (
            patch.object(accounts, "claude_account_selection", return_value=selection) as resolve,
            patch.object(agent_notes, "live_reports", return_value=reports) as collect,
            patch.object(five_hour, "watch", return_value=[]),
            patch.object(agent_notes, "alert", return_value=[]) as alert,
            patch.object(codex_pacer, "tick", side_effect=ValueError("fixture")),
            patch.object(sys, "argv", ["agent_notes.py"]),
            redirect_stdout(io.StringIO()),
        ):
            agent_notes.main()
        resolve.assert_called_once_with()
        collect.assert_called_once_with(selection, agent_accounts.observe_workers_home())
        notice_roles = cast(object, alert.call_args.kwargs["selection"])
        self.assertIs(notice_roles, selection)

    def test_default_account_collection_reads_its_directory_and_matching_quota(self) -> None:
        _ = self.account_dir("default", "main@example.com", "main-token")
        active = self.note("claude2.md", "main@example.com")
        spare = self.note("claude3.md", "other@example.com")
        report = Report("Claude", email="main@example.com", quotas=[Quota("Weekly", 20, self.future)])
        with (patch.object(agent_accounts, "claude_live", return_value=report) as claude,
              patch.object(agent_accounts, "codex_live", return_value=Report("Codex")),
              patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update()
        claude.assert_called_once_with(self.root / ".claude", use_disk_identity=True)
        updated = {note.label: note for note in note_file.read_notes()}
        self.assertEqual(updated[active.label].get("weekly_remaining_usage"), "80")
        self.assertEqual(updated[spare.label].get("weekly_remaining_usage"), "null")

    def test_implicit_live_read_keeps_inherited_oauth_token_and_subprocess_environment(self) -> None:
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "fixture-inherited"}),
            patch.object(agent_accounts, "executable", return_value="fixture-claude"),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, '{"loggedIn": true, "email": "main@example.com", "authMethod": "claude.ai"}', "")) as status,
            patch.object(agent_accounts, "claude_credentials", side_effect=RuntimeError("unused")) as credentials,
            patch.object(urllib.request, "urlopen", return_value=io.BytesIO(
                json.dumps({"seven_day": {"utilization": 20, "resets_at": self.future.isoformat()}}).encode())) as usage,
        ):
            report = agent_accounts.claude_live()
        self.assertEqual(report.weekly_remaining, 80)
        credentials.assert_not_called()
        self.assertNotIn("env", status.call_args.kwargs)
        request = cast(urllib.request.Request, usage.call_args.args[0])
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-inherited")
        self.assertEqual(report.source, ClaudeDirectorySource(self.root / ".claude"))

    def test_an_expired_stored_token_is_not_sent(self) -> None:
        named = self.root / ".claude3"
        named.mkdir()
        _ = (named / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "fixture-stale", "expiresAt": int((time.time() - 60) * 1000)}}))
        with (
            patch.object(agent_accounts, "executable", return_value="fixture-claude"),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, '{"loggedIn": true, "email": "unit@example.com", "authMethod": "claude.ai"}', "")),
            patch.object(urllib.request, "urlopen", side_effect=AssertionError("a stale token was sent")),
        ):
            report = agent_accounts.claude_live(named)
        self.assertEqual(report.quota_problem,
                         "Weekly quota: not read (stale token; a Claude session on this account refreshes it)")
        self.assertIsInstance(report.weekly_read_skip, agent_accounts.StaleTokenSkip)

    def test_stale_login_after_reset_is_persisted_without_inventing_usage_or_refreshing_the_token(self) -> None:
        named = self.root / ".claude3"
        named.mkdir()
        login = "idle@example.invalid"
        read = self.now - timedelta(days=2)
        reset = self.now - timedelta(days=1)
        note = self.note("claude3.md", login,
            f"resets: {local_reset(reset)}\nweekly_remaining_usage: 10\nweekly_usage_checked_at: {read.isoformat()}\n")
        usage_store.save([Report("Claude", email=login, quotas=[Quota("Weekly", 90, reset)])], read)
        stored = usage_store.load()[login]
        assert "weekly" in stored
        before = stored["weekly"].copy()
        credentials = named / ".credentials.json"
        _ = credentials.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "fixture-stale", "expiresAt": int((time.time() - 60) * 1000)}}))
        credential_bytes = credentials.read_bytes()
        with (patch.object(agent_accounts, "executable", return_value="fixture-claude"),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps({
                  "loggedIn": True, "email": login, "authMethod": "claude.ai"}), "")) as auth,
              patch.object(urllib.request, "urlopen", side_effect=AssertionError("stale token used for a read")),
              patch.object(subprocess, "Popen", side_effect=AssertionError("token refresh launched")),
              patch.object(account, "process_config_dir", return_value=account.NoAccountSetting())):
            report = agent_accounts.claude_live(named)
            _ = self.snapshot([report])
        auth.assert_called_once()
        self.assertEqual(auth.call_args.args[0], ("fixture-claude", "auth", "status"))
        self.assertEqual(credentials.read_bytes(), credential_bytes)
        held = usage_store.load()[login]
        self.assertIsInstance(usage_store.weekly_read_skip(held), agent_accounts.StaleTokenSkip)
        assert "weekly" in held
        self.assertEqual(held["weekly"], before)
        updated = read_note(note.path)
        assert isinstance(updated, Note)
        self.assertEqual(updated.get("weekly_remaining_usage"), "null")
        self.assertEqual(updated.get("weekly_usage_checked_at"), read.isoformat())
        self.assertEqual(updated.get("resets"), local_reset(reset))

    def test_named_credentials_never_fall_back_to_the_default_keychain(self) -> None:
        named = self.root / ".claude3"
        named.mkdir()
        with (patch.object(sys, "platform", "darwin"),
              patch.object(agent_accounts, "command_json", side_effect=RuntimeError("unavailable")) as command):
            with self.assertRaisesRegex(RuntimeError, "usage credentials unavailable"):
                _ = agent_accounts.claude_credentials(named)
        command.assert_called_once_with("security", "find-generic-password", "-s",
                                        agent_accounts.claude_keychain_service(named), "-w")

    def test_explicit_default_credentials_use_the_keychain_even_from_a_named_shell(self) -> None:
        with (patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3")}),
              patch.object(sys, "platform", "darwin"),
              patch.object(agent_accounts, "command_json", return_value={"claudeAiOauth": {"accessToken": "fixture"}}) as command):
            self.assertEqual(agent_accounts.claude_credentials(self.root / ".claude")
                             .get("claudeAiOauth", {}).get("accessToken"), "fixture")
        command.assert_called_once_with("security", "find-generic-password", "-s", "Claude Code-credentials", "-w")

    def test_whoami_prints_both_active_roles_and_omits_the_spare(self) -> None:
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude3", "unit@example.com", "unit-token")
        _ = self.account_dir("claude1", "spare@example.com", "spare-token")
        self.select_unit("claude3")
        _ = self.note("claude2.md", "main@example.com")
        _ = self.note("claude3.md", "unit@example.com")
        _ = self.note("claude1.md", "spare@example.com")
        output = io.StringIO()
        with (patch.object(whoami, "live_reports", return_value=[
            Report("Claude", email="main@example.com"), Report("Claude", email="unit@example.com"),
            Report("Claude", email="spare@example.com"), Report("Codex", problem="fixture"),
        ]), redirect_stdout(output)):
            asyncio.run(whoami.main())
        self.assertIn("Account: claude2 (in use)", output.getvalue())
        self.assertIn("Account: claude3 (in use)", output.getvalue())
        self.assertNotIn("spare@example.com", output.getvalue())

    def test_whoami_filters_inactive_accounts_even_when_only_default_is_configured(self) -> None:
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.note("claude2.md", "main@example.com")
        _ = self.note("claude3.md", "inactive@example.com")
        reports = [Report("Claude", email="main@example.com"), Report("Claude", email="inactive@example.com")]
        output = io.StringIO()
        with patch.object(whoami, "live_reports", return_value=reports), redirect_stdout(output):
            asyncio.run(whoami.main())
        self.assertIn("main@example.com", output.getvalue())
        self.assertNotIn("inactive@example.com", output.getvalue())

    def test_whoami_filters_and_renders_from_one_live_account_snapshot(self) -> None:
        _ = self.account_dir("default", "main@example.com", "main-token")
        _ = self.account_dir("claude1", "old-unit@example.com", "old-token")
        _ = self.account_dir("claude3", "new-unit@example.com", "new-token")
        self.select_unit("claude1")
        for label, login in (("claude1", "old-unit@example.com"), ("claude2", "main@example.com"), ("claude3", "new-unit@example.com")):
            _ = self.note(note_name(label), login)
        resolve = accounts.claude_account_selection
        def changed_after_snapshot() -> agent_accounts.ClaudeAccountsInUse:
            selected = resolve()
            self.select_unit("claude3")
            return selected
        reports = [Report("Claude", email=login) for login in ("old-unit@example.com", "main@example.com", "new-unit@example.com")]
        output = io.StringIO()
        with (patch.object(accounts, "claude_account_selection", side_effect=changed_after_snapshot) as snapshot,
              patch.object(whoami, "live_reports", return_value=reports), redirect_stdout(output)):
            asyncio.run(whoami.main())
        snapshot.assert_called_once_with()
        self.assertIn("Account: claude1 (in use)", output.getvalue())
        self.assertIn("Account: claude2 (in use)", output.getvalue())
        self.assertNotIn("new-unit@example.com", output.getvalue())

    def test_legacy_whoami_account_line_is_unchanged(self) -> None:
        _ = self.note("claude2.md", "main@example.com")
        self.assertEqual(whoami.render(Report("Claude", email="main@example.com"),
                                      accounts.claude_account_selection(), agent_accounts.observe_workers_home()), [
            "Claude", "  Account: claude2 — main@example.com", "  Plan: unavailable",
        ])

    def test_live_percentage_is_written_only_to_matching_tool_and_email(self):
        active = self.note("claude1.md", "a@example.com")
        other = self.note("claude2.md", "b@example.com")
        codex = self.note("codex1.md", "a@example.com")
        original_codex = codex.path.read_text()
        report = Report("Claude", email="A@example.com", quotas=[Quota("Weekly", 99, self.future)])
        _ = apply_quota_and_write([active, other, codex], report, self.now)
        self.assertEqual(active.get("weekly_remaining_usage"), "1")
        self.assertEqual(active.get("resets"), local_reset(self.future))
        self.assertEqual(other.get("weekly_remaining_usage"), "null")
        self.assertEqual(codex.path.read_text(), original_codex)
        self.assertTrue(active.path.read_text().endswith("---\nBody stays.\n"))
        self.assertEqual(active.path.stat().st_mode & 0o777, 0o644)

    def test_switch_preserves_inactive_observation_until_window_expires(self):
        old = self.note("codex1.md", "old@example.com")
        new = self.note("codex2.md", "new@example.com")
        _ = apply_quota_and_write([old, new], Report("Codex", email="old@example.com", quotas=[Quota("Weekly", 20, self.future)]), self.now)
        checked = old.get("weekly_usage_checked_at")
        _ = apply_quota_and_write([old, new], Report("Codex", email="new@example.com", quotas=[Quota("Weekly", 5, self.future)]), self.now)
        self.assertEqual(old.get("weekly_remaining_usage"), "80")
        self.assertEqual(old.get("weekly_usage_checked_at"), checked)
        self.assertEqual(new.get("weekly_remaining_usage"), "95")

    def test_expired_inactive_usage_is_unknown(self):
        past = local_reset(self.now - timedelta(days=1))
        note = self.note("claude1.md", "old@example.com", f"resets: {past}\nweekly_remaining_usage: 30\n")
        _ = apply_quota_and_write([note], Report("Claude", email="new@example.com"), self.now)
        self.assertEqual(note.get("weekly_remaining_usage"), "null")
        self.assertEqual(note.get("resets"), past)

    def test_failed_usage_is_not_zero_and_does_not_get_a_fresh_timestamp(self):
        note = self.note("claude1.md", "a@example.com")
        _ = apply_quota_and_write([note], Report("Claude", email="a@example.com", quotas=[Quota("Weekly", 100, self.future)]), self.now)
        self.assertEqual(note.get("weekly_remaining_usage"), "0")
        checked = note.get("weekly_usage_checked_at")
        _ = apply_quota_and_write([note], Report("Claude", email="a@example.com", quota_problem="Unavailable"), self.now + timedelta(minutes=2))
        self.assertEqual(note.get("weekly_remaining_usage"), "null")
        self.assertEqual(note.get("weekly_usage_checked_at"), checked)

    def test_unknown_identity_does_not_mark_account_inactive(self):
        note = self.note("codex1.md", "a@example.com")
        _ = apply_quota_and_write([note], Report("Codex", email="a@example.com", quotas=[Quota("Weekly", 5, self.future)]), self.now)
        original = note.path.read_text()
        _ = apply_quota_and_write([note], Report("Codex", problem="Unavailable"), self.now)
        self.assertEqual(note.path.read_text(), original)

    def test_reset_expiration_uses_eastern_date_and_summary_count(self):
        note = self.note("codex1.md", "a@example.com")
        report = Report("Codex", email="a@example.com")
        reset_credits(report, {"availableCount": 2, "credits": [
            {"status": "available", "expiresAt": "2026-10-23T01:00:00Z"},
            {"status": "redeemed", "expiresAt": "2026-10-01T01:00:00Z"},
        ]})
        _ = apply_quota_and_write([note], report, self.now)
        self.assertEqual(note.get("limit_reset_count"), "2")
        self.assertEqual(note.get("limit_reset"), "2026-10-22T21:00:00-04:00")
        # An unavailable API answer must not erase a known reset.
        _ = apply_quota_and_write([note], Report("Codex", email="a@example.com"), self.now)
        self.assertEqual(note.get("limit_reset_count"), "2")
        reset_credits(report, {"availableCount": 0, "credits": []})
        _ = apply_quota_and_write([note], report, self.now)
        self.assertEqual(note.get("limit_reset_count"), "0")
        self.assertEqual(note.get("limit_reset"), "null")

    def test_credit_balance_is_read_from_the_main_bucket(self):
        def usage(credits: object) -> CodexRateLimits:
            return {"rateLimits": {"credits": credits}}  # pyright: ignore[reportReturnType]

        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": False,
                                              "balance": "60498.0534770000"})), "60498")
        self.assertEqual(codex_credits(usage({"hasCredits": False, "unlimited": False, "balance": None})), "0")
        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": True, "balance": None})), "unlimited")
        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": False, "balance": None})), "null")
        self.assertEqual(codex_credits(usage(None)), "null")
        self.assertEqual(codex_credits({}), "null")

    def test_credit_balance_is_written_and_an_unread_one_is_kept(self):
        note = self.note("codex1.md", "a@example.com")
        _ = apply_quota_and_write([note], Report("Codex", email="a@example.com", credit_balance="60498"), self.now)
        self.assertEqual(note.get("credit_balance"), "60498")
        # A failed read must not erase the last balance seen.
        _ = apply_quota_and_write([note], Report("Codex", email="a@example.com"), self.now)
        self.assertEqual(note.get("credit_balance"), "60498")
        _ = apply_quota_and_write([note], Report("Codex", email="a@example.com", credit_balance="null"), self.now)
        self.assertEqual(note.get("credit_balance"), "null")

    def test_codex_reading_includes_numeric_credit_balance_from_the_note(self):
        note = self.note("codex1.md", "a@example.com", "credit_balance: 60498\n")
        weekly = Quota("Weekly", 40, self.future)
        _ = apply_quota_and_write([note], Report("Codex", email="a@example.com", quotas=[weekly]), self.now)
        records = [json.loads(line) for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertEqual(records, [{
            "account": "codex1",
            "at": self.now.isoformat(timespec="seconds"),
            "remaining": 60,
            "credits": 60498.0,
        }])

    def test_codex_reading_omits_absent_or_non_numeric_credit_balance(self):
        absent = self.note("codex1.md", "a@example.com")
        unlimited = self.note("codex2.md", "a@example.com", "credit_balance: unlimited\n")
        weekly = Quota("Weekly", 40, self.future)
        _ = apply_quota_and_write([absent, unlimited], Report("Codex", email="a@example.com", quotas=[weekly]), self.now)
        records = [json.loads(line) for line in (self.root / "readings.jsonl").read_text().splitlines()]
        self.assertCountEqual(records, [
            {"account": "codex1", "at": self.now.isoformat(timespec="seconds"), "remaining": 60},
            {"account": "codex2", "at": self.now.isoformat(timespec="seconds"), "remaining": 60},
        ])

    def test_a_refresh_clears_a_stale_claude_reset_count_on_every_claude_note(self):
        signed_in = self.note("claude1.md", "a@example.com", "limit_reset: 2026-10-22T12:34:00\nlimit_reset_count: 1\n")
        other = self.note("claude2.md", "b@example.com", "limit_reset: null\nlimit_reset_count: 0\n")
        _ = apply_quota_and_write([signed_in, other], Report("Claude", email="a@example.com"), self.now)
        for note in (signed_in, other):
            self.assertEqual((note.get("limit_reset_count"), note.get("limit_reset")), ("null", "null"))

    def test_a_claude_note_with_no_reset_count_gains_none(self):
        note = self.note("claude1.md", "a@example.com")
        _ = apply_quota_and_write([note], Report("Claude", email="a@example.com"), self.now)
        self.assertIsNone(note.get("limit_reset_count"))
        self.assertIsNone(note.get("limit_reset"))

    def test_reading_for_each_active_value_and_none_for_null_or_inactive(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        first = self.note("claude1.md", "a@example.com")
        second = self.note("claude2.md", "a@example.com")
        inactive = self.note("claude3.md", "b@example.com")
        weekly = Quota("Weekly", 40, checked + timedelta(days=2))
        _ = apply_quota_and_write([second, inactive, first], Report("Claude", email="a@example.com", quotas=[weekly]), checked)
        path = self.root / "readings.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertCountEqual(records, [
            {"account": "claude1", "at": "2026-10-05T19:45:00+00:00", "remaining": 60},
            {"account": "claude2", "at": "2026-10-05T19:45:00+00:00", "remaining": 60},
        ])
        _ = apply_quota_and_write([first, second, inactive], Report("Claude", email="a@example.com"), checked + timedelta(minutes=2))
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_reading_log_keeps_only_the_last_eight_days(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        note = self.note("claude1.md", "a@example.com")
        path = self.root / "readings.jsonl"
        old = {"account": "claude1", "at": "2026-09-27T19:44:59+00:00", "remaining": 70}
        recent = {"account": "claude1", "at": "2026-09-27T19:45:00+00:00", "remaining": 65}
        _ = path.write_text(json.dumps(old) + "\n" + json.dumps(recent) + "\n")
        _ = apply_quota_and_write([note], Report("Claude", email="a@example.com", quotas=[
            Quota("Weekly", 40, checked + timedelta(days=2))
        ]), checked)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records, [recent, {
            "account": "claude1", "at": "2026-10-05T19:45:00+00:00", "remaining": 60,
        }])

    def test_unwritable_log_reports_once_and_keeps_note_write(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        note = self.note("claude1.md", "a@example.com")
        (self.root / "readings.jsonl").mkdir()
        messages = apply_quota_and_write([note], Report("Claude", email="a@example.com", quotas=[
            Quota("Weekly", 40, checked + timedelta(days=2))
        ]), checked)
        self.assertEqual(note.get("weekly_remaining_usage"), "60")
        self.assertEqual(note.get("weekly_usage_checked_at"), "2026-10-05T19:45:00+00:00")
        self.assertEqual(len(messages), 2)
        self.assertEqual(sum("log" in message.lower() or "readings" in message.lower() for message in messages), 1)


class ResetTimeTests(unittest.TestCase):
    """Notes travel between machines in the vault, so a reset time carries its offset."""

    def test_a_reset_is_written_with_its_offset(self) -> None:
        at = datetime(2026, 10, 13, 7, 0, 20, tzinfo=timezone.utc)
        written = datetime.fromisoformat(local_reset(at))
        self.assertIsNotNone(written.tzinfo)
        self.assertEqual(written, at.replace(second=0))

    def test_a_reset_written_in_another_zone_is_read_by_its_offset(self) -> None:
        self.assertEqual(note_file.reset_time("2026-10-13T03:00:00-07:00"),
                         note_file.KnownReset(datetime(2026, 10, 13, 10, tzinfo=timezone.utc)))
        self.assertTrue(agent_notes.still_ahead("2099-01-01T00:00:00-07:00"))
        self.assertFalse(agent_notes.still_ahead("2000-01-01T00:00:00-07:00"))
        self.assertTrue(quota_alert.ahead("2099-01-01T00:00:00+09:00"))
        self.assertFalse(quota_alert.ahead("2000-01-01T00:00:00+09:00"))
        self.assertEqual(codex_pacer.parse_local("2026-10-13T03:00:00-07:00"),
                         note_file.KnownReset(datetime(2026, 10, 13, 10, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)))

    def test_a_reset_written_before_the_offset_is_read_in_the_machines_zone(self) -> None:
        self.assertEqual(note_file.reset_time("2026-10-13T03:00:00"), note_file.KnownReset(datetime(2026, 10, 13, 3).astimezone()))
        self.assertTrue(agent_notes.still_ahead("2099-01-01T00:00:00"))
        self.assertFalse(quota_alert.ahead("2000-01-01T00:00:00"))
        self.assertEqual(codex_pacer.parse_local("2026-10-13T03:00:00"), note_file.KnownReset(datetime(2026, 10, 13, 3)))

    def test_an_absent_or_unreadable_reset_names_the_reason(self) -> None:
        for value in (None, "", "null", "soon"):
            with self.subTest(value=value):
                self.assertIsInstance(note_file.reset_time(value),
                                      note_file.InvalidReset if value == "soon" else note_file.NoResetKnown)
                self.assertFalse(agent_notes.still_ahead(value))
                self.assertFalse(quota_alert.ahead(value))


if __name__ == "__main__":
    _ = unittest.main()
