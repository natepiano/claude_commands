"""Stable account-session tables preserve the surrounding user-written body."""

from __future__ import annotations

import sys
import os
import difflib
import json
import re
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Self, cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.message import roster

from scripts.whoami import session_table
import agent_notes
from scripts.whoami import agent_accounts
import five_hour
from scripts.whoami import note_file
from scripts.accounts import accounts


LEGACY_ACCOUNT = roster.AccountLabel("claude 1")
OBSERVED_AT = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
# TIMESTAMPS from /etc/nixos/modules/linux/agent-sessions-snapshot.py.
SNAPSHOT_TIMESTAMPS = re.compile(
    r"^Snapshot taken |^date_modified:|^weekly_usage_checked_at:|^weekly_remaining_usage:"
    + r"|^credit_balance:|^resets:|^limit_reset:"
)


def snapshot_changes(before: str, after: str) -> list[str]:
    return [line for line in difflib.ndiff(before.splitlines(), after.splitlines())
            if line.startswith(("+ ", "- ")) and not SNAPSHOT_TIMESTAMPS.match(line[2:])]


class SessionTableTests(unittest.TestCase):
    def test_table_round_trips_escaped_cells_and_sorts_machine_role_name_id(self) -> None:
        rows = (
            session_table.SessionRow("zeta", "unit", "natedev", "/home/nate/worktrees/zeta", "unit-z"),
            session_table.SessionRow("review | api", "worker", "macbook", "/Users/nate/rust/api | ui", "thread-1"),
            session_table.SessionRow("runner", "showrunner", "natedev", "/srv/project", "runner-1"),
            session_table.SessionRow("alpha", "unit", "natedev", "/home/nate/worktrees/alpha", "unit-a"),
            session_table.SessionRow("alpha", "unit", "natedev", "/home/nate/worktrees/alpha", "unit-b"),
            session_table.SessionRow("scratch", "freestanding", "natedev", "/tmp/probe", "—"),
            session_table.SessionRow("kept", "unit", "macbook", "~/project", "stale-id",
                                     "2026-10-14T12:00:00+00:00"),
        )
        rendered = session_table.render_section(rows)
        self.assertIn("review \\| api", rendered)
        self.assertIn("~/rust/api \\| ui", rendered)
        self.assertIn("~/worktrees/zeta", rendered)
        parsed = session_table.section_rows(rendered)
        self.assertEqual([row.session_id for row in parsed],
                         ["stale-id", "thread-1", "runner-1", "unit-a", "unit-b", "unit-z", "—"])
        self.assertEqual(parsed[1].name, "review | api")
        self.assertEqual(parsed[1].folder, "~/rust/api | ui")
        self.assertEqual(session_table.render_section(parsed), rendered)

    def test_section_replacement_keeps_other_headings_and_body_in_place(self) -> None:
        section = session_table.render_section((
            session_table.SessionRow("director", "unit", "natedev", "/tmp/project", "sid-1"),
        ))
        for suffix in ("", "## My notes\n\nKeep the conclusion.\n", "# Appendix\nKeep the appendix.\n"):
            with self.subTest(suffix=suffix):
                body = "Introduction.\n\n## Live sessions\n\nOld table.\n\n" + suffix
                rewritten = session_table.replace_section(body, section)
                self.assertTrue(rewritten.startswith("Introduction.\n\n"))
                self.assertEqual(rewritten.count("## Live sessions"), 1)
                self.assertIn("sid-1", rewritten)
                self.assertNotIn("Old table", rewritten)
                if suffix:
                    self.assertTrue(rewritten.endswith(suffix))
                self.assertEqual(session_table.replace_section(rewritten, section), rewritten)

    def test_absent_section_is_appended_and_empty_rows_are_explicit(self) -> None:
        section = session_table.render_section(())
        self.assertIn("No live sessions.", section)
        self.assertEqual(session_table.section_rows(section), ())
        rewritten = session_table.replace_section("Keep this body.\n", section)
        self.assertTrue(rewritten.startswith("Keep this body.\n\n## Live sessions"))
        self.assertEqual(session_table.replace_section(rewritten, section), rewritten)

    def test_unparsable_rows_do_not_hide_valid_neighbors(self) -> None:
        section = ("## Live sessions\n\n| Session | Role | Machine | Folder | Session id |\n"
                   + "| --- | --- | --- | --- | --- |\n"
                   + "| good | unit | natedev | ~/project | sid |\n"
                   + "| bad | unit | natedev | missing cell |\n"
                   + "| bad role | nonsense | natedev | ~/project | sid2 |\n")
        self.assertEqual(session_table.section_rows(section), (
            session_table.SessionRow("good", "unit", "natedev", "~/project", "sid"),
        ))

    def refresh_note(self, root: Path, local: list[roster.RosterEntry],
                     peer: roster.Roster | roster.PeerNotReached, *,
                     at: datetime = OBSERVED_AT,
                     reports: tuple[agent_accounts.Report, ...] = ()) -> note_file.Note:
        class ObservationTime(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> Self:
                return cls.fromtimestamp(at.timestamp(), tz)

        selection = agent_accounts.ClaudeAccountsInUse(agent_accounts.UnreadableIdentity(Path.home() / ".claude", "fixture identity unavailable"), False, (), frozenset())
        with patch.dict(os.environ, {"HOME": str(root), "AGENT_NOTES_DIR": str(root)}), \
             patch.object(roster, "roster", return_value=roster.Roster(local, [])), \
             patch.object(roster, "peer_snapshot", return_value=peer), \
             patch.object(roster, "machine_name", return_value="natedev"), \
             patch.object(agent_notes, "live_reports", return_value=list(reports)), \
             patch.object(accounts, "group_note_labels", return_value={}), \
             patch.object(agent_notes, "datetime", ObservationTime), \
             patch.object(five_hour, "watch", return_value=[]):
            _ = agent_notes.update_alert_pass(selection, root / ".codex")
        note = note_file.read_note(root / "claude1.md")
        assert isinstance(note, note_file.Note)
        return note

    def entry(self, *, machine: str = "mac", label: roster.AccountAttribution = LEGACY_ACCOUNT) -> roster.RosterEntry:
        return roster.RosterEntry(machine, "claude", "unit director", "director", "working",
            roster.Messageable("session:peer-id"), "/home/nate/project", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(), session=roster.ClaudeSession("peer-id"), account=label)

    def test_peer_legacy_label_lands_in_the_numbered_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ = (root / "claude1.md").write_text("---\nlogin: user@example.invalid\n---\nKeep this body.\n")
            note = self.refresh_note(root, [], roster.Roster([self.entry()], []))
            self.assertEqual(note.get("sessions"), "1")
            self.assertIn("peer-id", note.text)
            self.assertIn("Keep this body.", note.text)
            self.assertFalse((root / "claude 1.md").exists())

    def test_unchanged_local_and_peer_sessions_change_only_snapshot_ignored_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ = (root / "claude1.md").write_text("---\nlogin: user@example.invalid\n---\nKeep this body.\n")
            local = replace(self.entry(machine="natedev"), session=roster.ClaudeSession("local-id"))
            peer = roster.Roster([self.entry()], [])
            reports = (agent_accounts.Report("Claude", email="user@example.invalid",
                       quotas=[agent_accounts.Quota("Weekly", 40, OBSERVED_AT + timedelta(days=2))]),)
            first = self.refresh_note(root, [local], peer, reports=reports)
            second = self.refresh_note(root, [local], peer, at=OBSERVED_AT + timedelta(minutes=2), reports=reports)
            self.assertEqual(first.get("weekly_usage_checked_at"), second.get("weekly_usage_checked_at"))
            self.assertEqual(snapshot_changes(first.text, second.text), [])
            self.assertEqual(second.get("sessions"), "2")
            self.assertNotIn("observed-at", second.text)
            self.assertNotIn("last seen", second.text)
            seen = cast(dict[str, str], json.loads((root / ".local/state/agent-notes/sessions_seen.json").read_text()))
            self.assertEqual({machine: datetime.fromisoformat(at) for machine, at in seen.items()},
                             {"natedev": OBSERVED_AT + timedelta(minutes=2), "mac": OBSERVED_AT + timedelta(minutes=2)})

    def test_session_start_stop_move_and_unattributed_changes_remain_visible_to_snapshot(self) -> None:
        local = self.entry(machine="natedev")
        changed = replace(local, session=roster.ClaudeSession("new-id"))
        cases: tuple[tuple[list[roster.RosterEntry], list[roster.RosterEntry]], ...] = (
            ([], [local]), ([local], []),
            ([local], [replace(local, account=roster.AccountLabel("claude2"))]),
            ([local], [local, changed]),
            ([], [replace(local, account=roster.AccountUnread("unread"))]),
        )
        for before_rows, after_rows in cases:
            with self.subTest(before=before_rows, after=after_rows), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _ = (root / "claude1.md").write_text("---\nlogin: user@example.invalid\n---\n")
                _ = (root / "claude2.md").write_text("---\nlogin: other@example.invalid\n---\n")
                before = self.refresh_note(root, before_rows, roster.Roster([], []))
                after = self.refresh_note(root, after_rows, roster.Roster([], []), at=OBSERVED_AT + timedelta(minutes=2))
                self.assertTrue(snapshot_changes(before.text, after.text))

    def test_first_unreachable_refresh_reads_legacy_observation_without_rewriting_comment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ = (root / "claude1.md").write_text(
                "---\nlogin: user@example.invalid\n---\n## Live sessions\n\n"
                + "| Session | Role | Machine | Folder | Session id |\n| --- | --- | --- | --- | --- |\n"
                + "| director | unit | mac | ~/project | peer-id <!-- observed-at: 2026-10-14T12:00:00+00:00 --> |\n")
            note = self.refresh_note(root, [], roster.PeerNotReached("mac", "asleep"), at=OBSERVED_AT + timedelta(hours=1))
            row = session_table.section_rows(note.text)[0]
            self.assertEqual(datetime.fromisoformat(row.last_seen), OBSERVED_AT)
            self.assertNotIn("observed-at", note.text)
            self.assertEqual(note.get("sessions"), "0")

    def test_kept_row_time_prefers_marker_then_machine_state_then_legacy_comment(self) -> None:
        row = session_table.SessionRow("peer", "unit", "mac", "~/project", "sid", observed_at="legacy")
        self.assertEqual(session_table.last_seen(row, {}).last_seen, "legacy")
        self.assertEqual(session_table.last_seen(row, {"mac": "recorded"}).last_seen, "recorded")
        marked = row._replace(last_seen="frozen")
        kept = session_table.last_seen(marked, {"mac": "newer"})
        self.assertEqual(kept.last_seen, "frozen")
        self.assertNotIn("observed-at", session_table.render_section((kept,)))
        self.assertEqual(session_table.last_seen(row._replace(observed_at=""), {}).last_seen, "unknown")

    def test_unread_local_identity_is_reported_and_never_says_no_live_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ = (root / "claude1.md").write_text("---\nlogin: user@example.invalid\n---\n")
            unread = self.entry(machine="natedev", label=roster.AccountUnread("identity unreadable"))
            note = self.refresh_note(root, [unread], roster.Roster([], []))
            self.assertIn("1 live session(s) not attributed to an account", note.text)
            self.assertNotIn("No live sessions", note.text)
            self.assertEqual(note.get("sessions"), "0")

    def test_kept_peer_rows_are_last_seen_uncounted_and_return_to_live_when_reached(self) -> None:
        for reason in (roster.PeerNotReached("mac", "asleep"),
                       roster.Roster([self.entry(label=roster.AccountAttributionUnavailable("old peer"))], [])):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _ = (root / "claude1.md").write_text("---\nlogin: user@example.invalid\n---\n")
                observed = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
                live = self.refresh_note(root, [], roster.Roster([self.entry()], []), at=observed)
                self.assertEqual(live.get("sessions"), "1")
                self.assertNotIn("last seen", live.text)
                stale = self.refresh_note(root, [], reason, at=observed + timedelta(hours=6))
                self.assertIn("peer-id", stale.text)
                rows = session_table.section_rows(stale.text)
                self.assertEqual(len(rows), 1)
                self.assertEqual(datetime.fromisoformat(rows[0].last_seen), observed)
                self.assertEqual(stale.get("sessions"), "0")
                later = self.refresh_note(root, [], reason, at=observed + timedelta(hours=7))
                self.assertEqual(session_table.section_rows(later.text), rows)
                self.assertEqual(snapshot_changes(stale.text, later.text), [])
                recovered_at = observed + timedelta(hours=8)
                again = self.refresh_note(root, [], roster.Roster([self.entry()], []), at=recovered_at)
                self.assertEqual(again.get("sessions"), "1")
                self.assertNotIn("last seen", again.text)
                self.assertEqual(again.text.count("peer-id"), 1)
                lost_again = self.refresh_note(root, [], reason, at=observed + timedelta(hours=9))
                self.assertEqual(datetime.fromisoformat(session_table.section_rows(lost_again.text)[0].last_seen), recovered_at)

    def test_collection_ignores_activity_status_and_preserves_unattributable_peer_machines(self) -> None:
        local = roster.RosterEntry("natedev", "claude", "unit director", "director", "working",
            roster.Messageable("session:sid"), "/home/nate/project", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(), session=roster.ClaudeSession("sid"),
            account=roster.AccountLabel("claude1"))
        remote = replace(local, machine="mac", name="remote", account=roster.AccountAttributionUnavailable("peer sent no account"))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HOME": directory, "AGENT_NOTES_DIR": directory}), \
             patch.object(roster, "machine_name", return_value="natedev"):
            with patch.object(roster, "roster", return_value=roster.Roster([local], [])), \
                 patch.object(roster, "peer_snapshot", return_value=roster.Roster([remote], [])):
                first = session_table.live_sessions(os.environ)
            assert isinstance(first, session_table.ReadSessions)
            self.assertEqual(first.rows_kept, frozenset({"mac"}))
            self.assertEqual(first.machines_read, frozenset({"natedev"}))
            self.assertEqual(first.rows_by_label, {"claude1": (
                session_table.SessionRow("director", "unit", "natedev", "~/project", "sid"),)})
            with patch.object(roster, "roster", return_value=roster.Roster([replace(local, status="idle")], [])), \
                 patch.object(roster, "peer_snapshot", return_value=roster.PeerNotReached("mac", "asleep")):
                second = session_table.live_sessions(os.environ)
            self.assertEqual(second, first)
            with patch.object(roster, "roster", side_effect=OSError("registry unreadable")), \
                 patch.object(roster, "peer_snapshot", return_value=roster.Roster([], [])):
                unreadable = session_table.live_sessions(os.environ)
            self.assertIsInstance(unreadable, session_table.SessionsUnread)

    def test_empty_rosters_still_record_reached_machines(self) -> None:
        with patch.object(roster, "machine_name", return_value="local"), \
             patch.object(roster.remote, "other_machine", return_value="peer"), \
             patch.object(roster, "roster", return_value=roster.Roster([], [])), \
             patch.object(roster, "peer_snapshot", return_value=roster.Roster([], [])):
            result = session_table.live_sessions({})
        assert isinstance(result, session_table.ReadSessions)
        self.assertEqual(result.machines_read, frozenset({"local", "peer"}))

    def test_unreached_ssh_alias_keeps_the_hostname_in_saved_peer_rows(self) -> None:
        remote = session_table.SessionRow("remote", "unit", "nates-macbook", "~/remote", "mac-id")
        local = roster.RosterEntry("natedev", "claude", "unit director", "local", "working",
            roster.Messageable("session:local-id"), "/home/nate/project", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(), session=roster.ClaudeSession("local-id"),
            account=roster.AccountLabel("claude1"))
        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            _ = (notes / "claude1.md").write_text("---\nlogin: unit@example.invalid\n---\n" + session_table.render_section((remote,)))
            with patch.dict(os.environ, {"HOME": directory, "AGENT_NOTES_DIR": directory}), \
                 patch.object(roster, "machine_name", return_value="natedev"), \
                 patch.object(roster, "roster", return_value=roster.Roster([local], [])), \
                 patch.object(roster, "peer_snapshot", return_value=roster.PeerNotReached("mac", "asleep")):
                result = session_table.live_sessions(os.environ)
            assert isinstance(result, session_table.ReadSessions)
            self.assertIn("nates-macbook", result.rows_kept)
            self.assertNotIn("natedev", result.rows_kept)
            self.assertEqual([row.session_id for row in result.rows_by_label["claude1"]], ["local-id"])

    def test_collection_maps_codex_threads_and_claude_ids_and_skips_unread_accounts(self) -> None:
        base = roster.RosterEntry("natedev", "codex", "worker", "impl", "working",
            roster.CannotReceiveMessages("between turns"), "/srv/project", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(), thread=roster.CodexThread("thread-1"),
            account=roster.AccountLabel("codex2"))
        unread = replace(base, name="unread", account=roster.AccountUnread("unknown identity"))
        no_id = replace(base, name="no id", thread=roster.NoCodexThread())
        with patch.object(roster, "roster", return_value=roster.Roster([base, unread, no_id], [])), \
             patch.object(roster, "peer_snapshot", return_value=roster.Roster([], [])):
            result = session_table.live_sessions({})
        assert isinstance(result, session_table.ReadSessions)
        self.assertEqual([row.session_id for row in result.rows_by_label["codex2"]], ["thread-1", "—"])


if __name__ == "__main__":
    _ = unittest.main()
