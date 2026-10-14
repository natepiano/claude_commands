"""Stable account-session tables preserve the surrounding user-written body."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from scripts.message import roster

import session_table


class SessionTableTests(unittest.TestCase):
    def test_table_round_trips_escaped_cells_and_sorts_machine_role_name_id(self) -> None:
        rows = (
            session_table.SessionRow("zeta", "unit", "natedev", "/home/nate/worktrees/zeta", "unit-z"),
            session_table.SessionRow("review | api", "worker", "macbook", "/Users/nate/rust/api | ui", "thread-1"),
            session_table.SessionRow("runner", "showrunner", "natedev", "/srv/project", "runner-1"),
            session_table.SessionRow("alpha", "unit", "natedev", "/home/nate/worktrees/alpha", "unit-a"),
            session_table.SessionRow("alpha", "unit", "natedev", "/home/nate/worktrees/alpha", "unit-b"),
            session_table.SessionRow("scratch", "freestanding", "natedev", "/tmp/probe", "—"),
        )
        rendered = session_table.render_section(rows)
        self.assertIn("review \\| api", rendered)
        self.assertIn("~/rust/api \\| ui", rendered)
        self.assertIn("~/worktrees/zeta", rendered)
        parsed = session_table.section_rows(rendered)
        self.assertEqual([row.session_id for row in parsed],
                         ["thread-1", "runner-1", "unit-a", "unit-b", "unit-z", "—"])
        self.assertEqual(parsed[0].name, "review | api")
        self.assertEqual(parsed[0].folder, "~/rust/api | ui")
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

    def test_collection_ignores_activity_status_and_preserves_unattributable_peer_machines(self) -> None:
        local = roster.RosterEntry("natedev", "claude", "unit director", "director", "working",
            roster.Messageable("session:sid"), "/home/nate/project", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(), session=roster.ClaudeSession("sid"),
            account=roster.AccountLabel("claude 1"))
        remote = replace(local, machine="mac", name="remote", account=roster.AccountAttributionUnavailable("peer sent no account"))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HOME": directory, "AGENT_NOTES_DIR": directory}):
            with patch.object(roster, "roster", return_value=roster.Roster([local], [])), \
                 patch.object(roster, "peer_snapshot", return_value=roster.Roster([remote], [])):
                first = session_table.live_sessions(os.environ)
            assert isinstance(first, session_table.ReadSessions)
            self.assertEqual(first.rows_kept, frozenset({"mac"}))
            self.assertEqual(first.rows_by_label, {"claude 1": (
                session_table.SessionRow("director", "unit", "natedev", "~/project", "sid"),)})
            with patch.object(roster, "roster", return_value=roster.Roster([replace(local, status="idle")], [])), \
                 patch.object(roster, "peer_snapshot", return_value=roster.PeerNotReached("mac", "asleep")):
                second = session_table.live_sessions(os.environ)
            self.assertEqual(second, first)
            with patch.object(roster, "roster", side_effect=OSError("registry unreadable")), \
                 patch.object(roster, "peer_snapshot", return_value=roster.Roster([], [])):
                unreadable = session_table.live_sessions(os.environ)
            self.assertIsInstance(unreadable, session_table.SessionsUnread)

    def test_unreached_ssh_alias_keeps_the_hostname_in_saved_peer_rows(self) -> None:
        remote = session_table.SessionRow("remote", "unit", "nates-macbook", "~/remote", "mac-id")
        local = roster.RosterEntry("natedev", "claude", "unit director", "local", "working",
            roster.Messageable("session:local-id"), "/home/nate/project", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(), session=roster.ClaudeSession("local-id"),
            account=roster.AccountLabel("claude 1"))
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
            self.assertEqual([row.session_id for row in result.rows_by_label["claude 1"]], ["local-id"])

    def test_collection_maps_codex_threads_and_claude_ids_and_skips_unread_accounts(self) -> None:
        base = roster.RosterEntry("natedev", "codex", "worker", "impl", "working",
            roster.CannotReceiveMessages("between turns"), "/srv/project", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(), thread=roster.CodexThread("thread-1"),
            account=roster.AccountLabel("codex 2"))
        unread = replace(base, name="unread", account=roster.AccountUnread("unknown identity"))
        no_id = replace(base, name="no id", thread=roster.NoCodexThread())
        with patch.object(roster, "roster", return_value=roster.Roster([base, unread, no_id], [])), \
             patch.object(roster, "peer_snapshot", return_value=roster.Roster([], [])):
            result = session_table.live_sessions({})
        assert isinstance(result, session_table.ReadSessions)
        self.assertEqual([row.session_id for row in result.rows_by_label["codex 2"]], ["thread-1", "—"])


if __name__ == "__main__":
    _ = unittest.main()
