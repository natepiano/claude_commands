"""Quota alerts: claude."""

from __future__ import annotations

import sys
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.whoami import quota_alert_test_support as support
import quota_alert
from scripts.whoami import agent_accounts
from scripts.whoami.agent_accounts import ClaudeAccountsInUse, Quota, Report
import agent_notes
from scripts.whoami import session_table
from scripts.whoami import note_file
from scripts.whoami import account
from scripts.whoami.note_file import read_note
from scripts.accounts import accounts, next_moves
from user_action import ActionRequired
from scripts.whoami.quota_alert_test_support import Notes

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    def test_claude_reminder_pushes_once_at_two_percent_without_session_repeat_or_restore(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        _ = self.alert([self.note("claude2.md", "active", "3")], self.now)
        self.assertEqual(self.noted, [])
        _ = self.alert([self.note("claude2.md", "active", "2")], self.now + timedelta(minutes=2))
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.noted[0].body,
            "claude2 has 2% of its weekly usage left, and no move is set up for the units or showrunners on it. When it runs out, they move to a Claude account with room, or stop if none has any.")
        self.assertEqual(self.noted[0].title, "Claude work on claude2 may stop")
        self.assertEqual(self.noted[0].action,
                         ActionRequired("In the natedev session, type /account_moves and the move you want, for example: when units reach 1% switch to claude2."))
        for minute, left in ((4, "1"), (32, "1"), (120, "null"), (122, "1")):
            _ = self.alert([self.note("claude2.md", "active", left)], self.now + timedelta(minutes=minute))
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        _ = self.alert([self.note("claude2.md", "active", "80")], self.now + timedelta(minutes=124))
        self.assertEqual((self.sent, len(self.noted)), ([], 1))
        _ = self.alert([self.note("claude2.md", "active", "2")], self.now + timedelta(minutes=126))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_self_targeting_entry_does_not_silence_the_two_percent_claude_reminder(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        entry = next_moves.add_next_move("units", 1, "claude2")
        note = self.note("claude2.md", "active", "2")
        for minute in (0, 2, 32):
            _ = self.alert([note], self.now + timedelta(minutes=minute))
        self.assertEqual([push.values["account"] for push in self.noted], ["claude2"])
        self.assertEqual((self.sent, self.pushed), ([], []))
        self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_claude_account_used_only_by_ungrouped_sessions_sends_no_phone_reminder(self) -> None:
        note = self.note("claude2.md", "inactive", "1")
        _ = note.path.write_text(note.path.read_text().replace("sessions: 0", "sessions: 2"))
        note = note_file.read_note(note.path)
        assert isinstance(note, note_file.Note)
        self.assertTrue(note_file.in_use(note))
        self.assertEqual(note.get_list("role"), ())
        self.assertEqual(next_moves.read_next_moves(), ())
        for minute in (0, 2, 32):
            _ = self.alert([note], self.now + timedelta(minutes=minute))
        self.assertEqual((self.noted, self.pushed, self.sent), ([], [], []))

    def test_self_targeting_claude_head_with_a_later_move_still_sends_the_reminder(self) -> None:
        head = next_moves.add_next_move("units", 1, "claude2")
        later = next_moves.add_next_move("units", 1, "claude3")
        note = self.note("claude2.md", "active", "1")
        for minute in (0, 2, 32):
            _ = self.alert([note], self.now + timedelta(minutes=minute))
        self.assertEqual([push.id for push in self.noted], ["quota.claude_next_move"])
        self.assertEqual(self.noted[0].values["account"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), (head, later))
        self.assertEqual((self.sent, self.pushed), ([], []))

    def test_claude_reminder_is_suppressed_only_when_every_group_on_the_account_has_an_entry(self) -> None:
        note = self.note("claude2.md", "active", "1")
        _ = note.path.write_text(note.path.read_text().replace("role: unit", "role: [units, showrunners]"))
        note = note_file.read_note(note.path)
        assert isinstance(note, note_file.Note)
        unit = next_moves.add_next_move("units", 1, "claude3")
        _ = next_moves.add_next_move("showrunners", 1, "claude1")
        _ = self.alert([note], self.now)
        _ = self.alert([note], self.now + timedelta(minutes=30))
        self.assertEqual((self.noted, self.sent, self.pushed), ([], [], []))
        next_moves.remove_next_move("showrunners", next_moves.GroupPosition(1))
        _ = self.alert([note], self.now + timedelta(minutes=32))
        self.assertEqual(len(self.noted), 1)
        _ = self.alert([note], self.now + timedelta(minutes=64))
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(next_moves.read_next_moves(), (unit,))

    def test_legacy_acknowledgement_does_not_silence_a_new_claude_next_move_reminder(self) -> None:
        _ = quota_alert.STATE.write_text(json.dumps({"episodes": {"claude2": {
            "since": self.now.isoformat(), "acknowledged": self.now.isoformat(), "push": "due", "last": {}}}}))
        _ = self.alert([self.note("claude2.md", "active", "1")], self.now)
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.sent, [])
        document = cast(dict[str, object], json.loads(quota_alert.STATE.read_text()))
        episodes = cast(dict[str, dict[str, object]], document["episodes"])
        self.assertNotIn("acknowledged", episodes["claude2"])

    def test_claude_phone_failure_retries_delivery_without_sending_sessions_anything(self) -> None:
        self.push_errors.append(quota_alert.PushFailed("fixture offline"))
        note = self.note("claude2.md", "active", "1")
        first = self.alert([note], self.now)
        self.assertIn("quota push claude2 -> user: not delivered: fixture offline", first)
        _ = self.alert([note], self.now + timedelta(minutes=2))
        _ = self.alert([note], self.now + timedelta(minutes=32))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_low_notes_include_roles_or_sessions_but_exclude_unused_accounts(self) -> None:
        role_only = self.note("claude1.md", "active", "1")
        session_only = self.note("claude2.md", "inactive", "1")
        spare = self.note("claude3.md", "inactive", "1")
        _ = session_only.path.write_text(session_only.path.read_text().replace("sessions: 0", "sessions: 2"))
        session_only = read_note(session_only.path)
        assert isinstance(session_only, note_file.Note)
        notes = [role_only, session_only, spare]
        self.assertEqual([note.label for note in quota_alert.low(notes, 5, now=self.now)], ["claude1", "claude2"])

    def test_alert_opens_episodes_for_role_only_and_peer_sessions_only_accounts(self) -> None:
        """F001: a note's role or peer session count suffices without a local session."""
        role_only = self.note("claude1.md", "active", "1", login="role@example.invalid")
        peer_only = self.note("claude2.md", "inactive", "1", login="peer@example.invalid")
        _ = peer_only.path.write_text(peer_only.path.read_text().replace("sessions: 0", "sessions: 2"))
        peer_only = read_note(peer_only.path)
        assert isinstance(peer_only, note_file.Note)
        selection = ClaudeAccountsInUse(
            agent_accounts.NumberedAccount("claude3", Path.home() / ".claude", "local@example.invalid"),
            True, (), frozenset(),
        )

        _ = self.alert([role_only, peer_only], self.now, selection=selection)

        with quota_alert.state_file() as state:
            self.assertEqual(set(state["episodes"]), {"claude1", "claude2"})
        self.assertEqual(self.sent, [])
        self.assertEqual({push.values["account"] for push in self.noted}, {"claude1"})

    def test_alert_does_not_open_an_episode_for_an_unused_default_account(self) -> None:
        """F001: the default login alone does not make an unused note eligible."""
        note = self.note("claude1.md", "inactive", "1")
        selection = ClaudeAccountsInUse(
            agent_accounts.NumberedAccount("claude1", Path.home() / ".claude", "someone@example.com"),
            False, (), frozenset(),
        )

        _ = self.alert([note], self.now, selection=selection)

        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"], {})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.noted, [])

    def test_claude_role_changes_do_not_repeat_the_phone_reminder_before_quota_recovers(self) -> None:
        for minute, state, left in ((0, "active", "1"), (2, "inactive", "1"),
                                    (4, "active", "1"), (6, "active", "80"), (8, "active", "1")):
            _ = self.alert([self.note("claude1.md", state, left)], self.now + timedelta(minutes=minute))
            self.assertEqual(len(self.noted), 2 if minute == 8 else 1)
        self.assertEqual(self.sent, [])

    def test_sessions_on_multiple_accounts_each_get_their_accounts_alerts(self) -> None:
        from scripts.message import sessions
        self.configure_claude_roles()
        owners = [sessions.SessionRecord(pid=pid, sessionId=str(pid), name=str(pid),
                  messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[])
                  for pid in (10001, 10003)]
        identities = {10001: account.Account("claude", "spare@example.invalid", "claude1"),
                      10003: account.Account("claude", "unit@example.invalid", "claude3")}
        notes: Notes = [self.note("claude1.md", "active", "0", login="spare@example.invalid"),
                        self.note("claude2.md", "active", "0", login="showrunner@example.invalid"),
                        self.note("claude3.md", "active", "0", login="unit@example.invalid")]
        def observed_account(pid: int, *, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome) -> account.Account | None:
            identity = identities.get(pid)
            if (identity is not None and isinstance(home_state, account.LegacyHomeLogin)
                    and identity.login.casefold() == home_state.account.login.casefold()):
                return home_state.account
            return identity
        with (mock.patch.object(sessions, "live_sessions", return_value=owners),
              mock.patch.object(account, "account_of", side_effect=observed_account)):
            _ = self.alert(notes, self.now)
        for label in ("claude1", "claude2", "claude3"):
            self.assertTrue(any(push.values["account"] == label for push in self.noted))
        with quota_alert.state_file() as state:
            self.assertEqual(set(state["episodes"]), {"claude1", "claude2", "claude3"})

    def test_collected_readings_remind_the_unfit_group_account_but_not_ungrouped_external_sessions(self) -> None:
        from scripts.message import sessions
        import five_hour
        self.configure_claude_roles()
        _ = accounts.set_group_account("units", "claude3", why="fixture units", now=self.now)
        unfit = accounts.account_dir("claude3")
        (unfit / "projects").unlink()
        external = self.root / "retained account"
        external.mkdir()
        owners = [sessions.SessionRecord(pid=pid, sessionId=str(pid), name=str(pid),
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]) for pid in (10001, 10003)]
        directories = {10001: external, 10003: unfit}
        logins = {self.root / ".claude": "showrunner@example.invalid", accounts.account_dir("claude2"): "showrunner@example.invalid",
                  unfit: "unit@example.invalid",
                  external: "external@example.invalid", accounts.account_dir("claude1"): "spare@example.invalid"}
        _ = self.note("claude4.md", "inactive", "90", login="external@example.invalid")
        _ = self.note("claude3.md", "inactive", "90", login="unit@example.invalid")
        _ = self.note("claude2.md", "active", "90", login="showrunner@example.invalid")
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
            return Report("Claude", email=logins[directory], quotas=[Quota("Weekly", 100,
                datetime.now(timezone.utc) + timedelta(days=2))], source=agent_accounts.ClaudeDirectorySource(directory))
        with (mock.patch.object(sessions, "live_sessions", return_value=owners),
              mock.patch.object(account, "account_of", side_effect=process_account),
              mock.patch.object(account, "process_config_dir", side_effect=process_directory)):
            selection = accounts.claude_account_selection()
        with (mock.patch.object(agent_accounts, "claude_live", side_effect=live),
              mock.patch.object(agent_accounts, "codex_homes", return_value=[]),
              mock.patch.object(five_hour, "watch", return_value=[]),
              mock.patch.object(session_table, "live_sessions", return_value=session_table.ReadSessions({
                  label: (session_table.SessionRow(label, "freestanding", "natedev", "~/project", label),)
                  for label in ("claude3", "claude4")}, frozenset()))):
            _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
        _ = self.alert(list(note_file.read_notes()), datetime.now(timezone.utc), selection=selection)
        self.assertEqual({push.values["account"] for push in self.noted}, {"claude3"})
        with quota_alert.state_file() as state:
            self.assertIn("claude4", state["episodes"])
        self.assertEqual(self.sent, [])

    def test_alert_uses_the_updaters_role_value_without_resolving_again(self) -> None:
        """Update and alert can share the same run's role value."""
        self.configure_claude_roles()
        unit = self.note("claude3.md", "active", "0", login="unit@example.invalid")
        roles = accounts.claude_account_selection()
        self.set_claude_activity("claude1")
        with mock.patch.object(accounts, "claude_account_selection", side_effect=AssertionError("roles resolved twice")):
            _ = self.alert([unit], self.now, selection=roles)
        self.assertEqual(self.sent, [])
        self.assertEqual([push.values["account"] for push in self.noted], ["claude3"])

    def test_refusal_uses_supplied_selection_without_resolving_again(self) -> None:
        """Refusal notices accept the same run's frozen role value."""
        self.configure_claude_roles()
        note = self.note("codex1.md", "active", "0")
        roles = accounts.claude_account_selection()
        with mock.patch.object(accounts, "claude_account_selection", side_effect=AssertionError("roles resolved twice")):
            lines = self.block([note], self.now, selection=roles)
        self.assertIn("switched to claude for codex1: direct, module_review", lines)
        self.assertEqual((len(self.sent), len(self.pushed)), (0, 0))

    def test_recovery_does_not_announce_a_different_healthy_claude_account(self) -> None:
        self.configure_claude_roles()
        unit = self.note("claude3.md", "active", "0", login="unit@example.invalid")
        main = self.note("claude2.md", "active", "80", login="showrunner@example.invalid")
        _ = self.alert([unit, main], self.now)
        self.sent.clear()
        unit = self.note("claude3.md", "inactive", "0", login="unit@example.invalid")
        _ = self.alert([unit, main], self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    _ = unittest.main()
