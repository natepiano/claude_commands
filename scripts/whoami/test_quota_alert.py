"""Quota reminders, routing and registry cancellation."""

from __future__ import annotations

import io
import fcntl
import json
import os
import subprocess
import sys
import select
import shlex
import signal
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.whoami import quota_alert_test_support as support
import quota_alert
import review
from push_texts import render
from scripts.whoami import agent_accounts
from agent_accounts import ClaudeAccountsInUse, Quota, Report
import agent_notes
import session_table
import note_file
import account
import usage_store
from agent_notes import local_reset
from note_file import read_note
from scripts.accounts import accounts, moves, next_moves, refusals
from user_action import ActionRequired

from scripts.whoami.quota_alert_test_support import Notes, REAL_RECIPIENTS, REAL_TELL_USER, REFUSAL_ACTION, REGISTRY as REGISTRY, running

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

setUpModule = support.setUpModule


class QuotaAlertTests(support.QuotaReminderFixture):
    def test_shutdown_hold_has_its_own_blocked_head_line(self) -> None:
        detail = "accounts: claude4 is held by a shutdown; no session was stopped"
        reason = quota_alert.TargetHeldByShutdown(detail)
        self.assertEqual(quota_alert.blocked_next_move_line("units", "claude4", "head", reason),
                         "units 1 → claude4 due, " + detail)
        self.assertNotEqual(quota_alert.blocked_next_move_reason_key(reason),
                            quota_alert.blocked_next_move_reason_key(quota_alert.TargetPreparationRefused(detail)))

    def test_failed_login_renewal_has_its_own_blocked_head_line(self) -> None:
        reason = quota_alert.TargetLoginRenewalFailed("timed out after 120 s")
        self.assertEqual(quota_alert.blocked_next_move_line("units", "claude4", "head", reason),
            "units 1 → claude4 due, claude4's login expired, and a probe session on it failed "
            + "(timed out after 120 s). Log in on claude4 by hand.")
        self.assertNotEqual(quota_alert.blocked_next_move_reason_key(reason),
            quota_alert.blocked_next_move_reason_key(quota_alert.TargetReadingUnavailable(quota_alert.NoTargetReading())))

    def test_a_claim_during_a_codex_save_keeps_both_results(self) -> None:
        home = self.root / ".codex"
        login = "reader@example.invalid"
        worker = agent_accounts.KnownCodexAccount(home, "worker@example.invalid", "codex1")
        report = Report("Codex", email=worker.login, quotas=[Quota("Weekly", 40, None)])
        readings = quota_alert.CodexAlertPassReadings(
            accounts.CodexWorkersAccount(worker.label, home), worker,
            (quota_alert.CodexHomeReading(home, report, self.now),))
        merging, claiming = Event(), Event()
        merge = usage_store.merge

        def merge_while_claim_starts(stored: usage_store.UsageReadings,
                                    reports: list[Report], at: datetime) -> bool:
            merging.set()
            with usage_store.store_file().with_name("usage.lock").open("a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(claiming.wait(timeout=TEST_HANG_GUARD_SECS))
            return merge(stored, reports, at)

        def claim() -> agent_accounts.UsageReadClaim | agent_accounts.UsageReadHeld:
            self.assertTrue(merging.wait(timeout=TEST_HANG_GUARD_SECS))
            claiming.set()
            return usage_store.claim_read(login, self.now)

        with (mock.patch.object(usage_store, "merge", side_effect=merge_while_claim_starts),
              ThreadPoolExecutor(max_workers=2) as pool):
            saved = pool.submit(quota_alert._save_codex_readings, readings, self.now)  # pyright: ignore[reportPrivateUsage]
            claimed = pool.submit(claim)
            saved.result(timeout=TEST_HANG_GUARD_SECS)
            result = claimed.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertIsInstance(result, agent_accounts.UsageReadClaim)
        stored = usage_store.load()
        self.assertEqual(stored[login].get("attempted_at"), self.now.isoformat())
        self.assertEqual(stored[usage_store.key("Codex", worker.login)].get("weekly", {}).get("left"), 60)
        self.assertIsInstance(usage_store.claim_read(login, self.now + timedelta(seconds=1)),
                              agent_accounts.UsageReadHeld)

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
        self.assertEqual([note.label for note in quota_alert.low(notes, 5)], ["claude1", "claude2"])

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

    def test_quota_config_uses_shared_showrunner_settings(self) -> None:
        self.assertEqual(quota_alert.Config.__module__, "scripts.production.showrunners")
        self.assertEqual(quota_alert.load_settings_from.__module__, "scripts.production.showrunners")

    def test_setup_blocks_real_phone_sends(self) -> None:
        """Guard the fixture patch that prevents real phone sends."""
        self.assertIsNot(quota_alert.tell_user, REAL_TELL_USER)

    def test_load_config_reads_the_named_object_layout_registry(self) -> None:
        named_config = self.root / "named-showrunners.json"
        _ = named_config.write_text(json.dumps({
            "threshold_percent": 7,
            "repeat_minutes": 45,
            "stall_minutes": 9,
            "faults_to": "fault-handler",
            "always": ["always-there"],
            "showrunners": [{
                "session": "named-director",
                "zone": "America/New_York",
                "units": [{"session": "named-unit", "status": "run-finished"}],
            }],
        }), encoding="utf-8")

        with mock.patch.object(quota_alert, "CONFIG", named_config):
            config = quota_alert.load_config()

        self.assertEqual(config["threshold_percent"], 7)
        self.assertEqual(config["always"], ["always-there"])
        # A list of showrunners left in the file is not read: they are found by their update timers.
        self.assertNotIn("showrunners", config)

    def test_codex_two_percent_reminder_pushes_once_per_episode_without_session_repeat(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        for minute, left in ((0, "3"), (2, "2"), (10, "1"), (32, "1")):
            _ = self.alert([self.note("codex1.md", "active", left)], self.now + timedelta(minutes=minute))
            self.assertEqual(len(self.noted), 0 if minute == 0 else 1)
        push = self.noted[0]
        self.assertEqual(push.id, "quota.codex_next_move")
        self.assertEqual(push.title, "codex1 Codex workers will use paid credits")
        self.assertEqual(push.body,
            "codex1 has 2% of its weekly usage left, and no move is set up for its workers. When it runs out, they spend its paid credits, then move to Claude. Nothing is needed.")
        self.assertEqual(push.action, ActionRequired(
            "Optional: to move them at 1% instead, type in the natedev session: /account_moves when workers reach 1% switch to Claude"))
        self.assertEqual(self.sent, [])
        _ = self.alert([self.note("codex1.md", "active", "80")], self.now + timedelta(minutes=34))
        self.sent.clear()  # Restoration notices remain in Phase54.
        _ = self.alert([self.note("codex1.md", "active", "2")], self.now + timedelta(minutes=36))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_workers_self_target_keeps_the_low_reminder_due_without_pushing_a_fallback(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        entry = next_moves.add_next_move("workers", 1, "codex1")
        note = self.note("codex1.md", "active", "2")
        for minute in (0, 2, 32):
            _ = self.alert([note], self.now + timedelta(minutes=minute))
        self.assertEqual(quota_alert.codex_next_move_push(note, now=self.now),
                         quota_alert.QuotaStatusNote("codex1", "the queued next entry targets the current account"))
        self.assertEqual(self.noted, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"]["codex1"].get("push"), "due")

    def test_runnable_workers_entry_keeps_the_reminder_due_until_removed(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        _ = next_moves.add_next_move("workers", 1, "codex2")
        note = self.note("codex1.md", "active", "2")
        _ = self.alert([note], self.now)
        self.assertEqual((self.noted, self.sent), ([], []))
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"]["codex1"].get("push"), "due")
        next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
        _ = self.alert([note], self.now + timedelta(minutes=2))
        self.assertEqual([push.id for push in self.noted], ["quota.codex_next_move"])
        self.assertEqual(self.sent, [])

    def test_unreadable_claude_history_makes_log_coverage_empty_for_the_reminder(self) -> None:
        entry = next_moves.add_next_move("units", 1, "claude3")
        history = accounts.roles_history_file()
        history.parent.mkdir(parents=True, exist_ok=True)
        _ = history.write_bytes(b"not JSON\n")
        _ = self.alert([self.note("claude2.md", "active", "1")], self.now)
        self.assertEqual([push.id for push in self.noted], ["quota.claude_next_move"])
        self.assertEqual(self.sent, [])
        self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_episode_without_push_marker_never_sends_old_news(self) -> None:
        stamp = self.now.isoformat(timespec="seconds")
        _ = quota_alert.STATE.write_text(json.dumps({
            "episodes": {"claude2": {"since": stamp, "last": {}}},
        }))
        _ = self.alert([self.note("claude2.md", "active", "0")], self.now)
        self.assertEqual((self.pushed, self.noted, self.sent), ([], [], []))
        with quota_alert.state_file() as state:
            self.assertNotIn("push", state["episodes"]["claude2"])

    def test_codex_weekly_zero_status_pushes_once_without_a_next_move_reminder(self) -> None:
        note = self.note("codex1.md", "active", "0", credit_balance="60498")
        _ = self.alert([note], self.now)
        self.assertEqual(self.noted, [])
        self.assertEqual(len(self.pushed), 1)
        title, body, ask = self.pushed[0]
        self.assertEqual(title, "quota_alert: codex1 now uses paid credits")
        self.assertIn("60,498", body)
        self.assertEqual(ask.need, "decision")
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
            self.assertEqual(state.get("codex_zero", {})["codex1"]["phone"], self.now.isoformat(timespec="seconds"))
        _ = self.alert([note], self.now + timedelta(minutes=30))
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.noted, [])

    def test_outgoing_account_refusal_is_logged_without_a_phone_push(self) -> None:
        """F005: a refusal from an account no longer used needs no user action."""
        outgoing = self.note("codex1.md", "inactive", "0", login="old@example.invalid")
        current = self.note("codex2.md", "active", "90", login="someone@example.com")
        accounts.set_role("workers", "codex2")
        readings = self.alert_readings([outgoing, current], self.now)
        refusing = agent_accounts.KnownCodexAccount(self.root / "outgoing", "old@example.invalid", "codex1")
        lines = quota_alert.blocked([outgoing, current], refusing, self.now, codex_readings=readings)
        self.assertEqual((self.pushed, self.sent), ([], []))
        self.assertIn("quota status codex1: Codex refused work on codex1, the outgoing account. Nothing switched to Claude.", lines)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
            notice = state.get("refusal_notices", {}).get(str(refusing.home))
            assert notice is not None and notice["kind"] == "known"
            self.assertEqual(notice.get("push"), self.now.isoformat(timespec="seconds"))

    def test_internal_switch_failure_details_do_not_leak_into_the_phone_push(self) -> None:
        note = self.note("codex1.md", "active", "0")
        push = quota_alert.refusal_push(note, quota_alert.NoCodexQuotaFallback(),
                                        "Could not read /private/account/registry " + "unavailable " * 100, False)
        self.assertIsInstance(push, quota_alert.UserPush)
        self.assertNotIn("/private", push.text)
        self.assertNotIn("registry", push.text)
        self.assertIn("the work is paused", push.text)

    def test_actionable_quota_pushes_fit_phone_body_and_name_where_to_act(self) -> None:
        notes: Notes = [self.note("claude2.md", "active", "0", limit_reset_count="0"),
                        self.note("codex1.md", "active", "0", credit_balance="60498")]
        _ = self.alert(notes, self.now)
        _ = self.block(notes, self.now)
        for summary, text, ask in self.pushed:
            self.assertLess(len(summary), 45)
            self.assertLess(len("Action: " + ask.action + "\n" + text), 500)
            self.assertTrue("natedev" in ask.action or "chatgpt.com/codex/settings/usage" in ask.action)
            self.assertLessEqual(text.count("recommended"), 1)

    def test_every_user_push_states_its_action_as_send_py_requires(self) -> None:
        _ = quota_alert.REGISTRY.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        claude = self.note("claude2.md", "active", "0", limit_reset_count="3")
        codex = self.note("codex1.md", "active", "0")
        spare = self.note("claude1.md", "inactive", "80", login="spare@example.invalid")
        sends = self.user_sends(lambda: self.alert([claude, codex, spare], self.now))
        sends |= self.user_sends(lambda: self.block([codex], self.now))
        notes = [title for title, (need, _action) in sends.items() if need == "note"]
        self.assertEqual(len(notes), 1)
        for title in notes:
            _ = sends.pop(title)
        self.assertEqual(sends, {
            "quota_alert: codex1 now uses paid credits": ("decision", ActionRequired(
                "To stop it, set use_credits: off in ~/rust/hanadocs/agents/codex1.md on natedev")),
            "Codex work stopped for lack of usage": ("decision", ActionRequired(REFUSAL_ACTION)),
        })

    def test_user_delivery_names_success_process_failure_and_launch_failure(self) -> None:
        push = render("quota.claude_next_move", account="claude2", remaining="0%")
        for code, line, expected in ((0, "SENT: to the user", quota_alert.PushSent()),
                                     (3, "FAILED: channel unavailable", quota_alert.PushFailed("FAILED: channel unavailable"))):
            with self.subTest(code=code), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, line, "")):
                self.assertEqual(REAL_TELL_USER(push), expected)
        with mock.patch.object(subprocess, "run", side_effect=OSError("sender unavailable")):
            self.assertEqual(REAL_TELL_USER(push), quota_alert.PushFailed("sender unavailable"))

    def test_user_send_uses_configured_review_limit_without_shortening_agent_relays(self) -> None:
        config = self.root / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        done = subprocess.CompletedProcess(["send.py"], 0, "SENT: to the user", "")
        with (mock.patch.object(review, "NOTIFY_CONFIG", config),
              mock.patch.object(subprocess, "run", return_value=done) as run):
            self.assertIsInstance(REAL_TELL_USER(render("quota.claude_next_move", account="claude2", remaining="0%")), quota_alert.PushSent)
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(float(command[command.index("--timeout") + 1]), review.user_send_seconds())
            self.assertEqual(run.call_args.kwargs["timeout"], review.user_send_seconds() + 15)
            self.assertNotIn("input", run.call_args.kwargs)
        self.assertEqual(quota_alert.RELAY_TIMEOUT, 40)

    def test_failed_codex_reminder_retries_phone_delivery_without_session_alerts(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        note = self.note("codex1.md", "active", "2")
        self.push_errors.append(quota_alert.PushFailed("fixture offline"))
        log = self.alert([note], self.now)
        self.assertIn("quota push codex1 -> user: not delivered: fixture offline", log)
        _ = self.alert([note], self.now + timedelta(minutes=2))
        _ = self.alert([note], self.now + timedelta(minutes=32))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_switch_to_a_low_account_is_not_a_restore(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.recipients()
        _ = self.alert([self.note("codex1.md", "inactive", "0", login="old@example.invalid"), self.note("codex2.md", "active", "1")],
                              self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.noted[-1].values["account"], "codex2")

    def test_retained_notice_recipients_include_configured_sessions_and_showrunners_once(self) -> None:
        with (mock.patch.object(quota_alert, "recipients", REAL_RECIPIENTS),
              mock.patch.object(quota_alert, "registered_showrunners",
                  return_value=running("natedev", "boss of bosses", "new director"))):
            self.assertEqual(quota_alert.recipients(quota_alert.load_config()),
                             ["natedev", "boss of bosses", "new director"])
            self.assertEqual(quota_alert.recipients(quota_alert.load_config(), "natedev"),
                             ["boss of bosses", "new director"])

    def test_low_recovery_closes_the_episode_without_telling_sessions(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "1")], self.now)
        _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state["episodes"])

    def test_refresh_after_recovery_tells_no_session(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "1")], self.now)
        notes: Notes = [self.note("codex1.md", "active", "100")]
        _ = self.alert(notes, self.now + timedelta(minutes=2))
        lines = self.refresh(notes, "natedev", self.now + timedelta(minutes=3))
        self.assertIn("notes refreshed; nobody was told", lines)
        self.assertEqual(self.sent, [])

    def test_refresh_low_allowance_tells_no_session(self) -> None:
        lines = self.refresh([self.note("codex1.md", "active", "1")], "natedev", self.now)
        self.assertIn("notes refreshed; nobody was told", lines)
        self.assertEqual(self.sent, [])

    def test_legacy_blocked_cli_finds_the_canonical_episode_and_saves_no_spaced_label(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "1")]
        _ = self.alert(notes, self.now)
        _ = self.block(notes, self.now)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(state["episodes"]["codex1"].get("refusal_push"), self.now.isoformat(timespec="seconds"))
            # Only the episode's delivery marker may suppress this retry.
            _ = state.get("refusal_notices", {})[str(self.root / ".codex")].pop("push", None)
        self.pushed.clear()
        self.sent.clear()
        readings = self.alert_readings(notes, self.now)
        output = io.StringIO()

        with (mock.patch.object(sys, "argv", ["agent_notes.py", "blocked", str(self.root / ".codex"),
                  "someone@example.com", "codex 1"]),
              mock.patch.object(note_file, "read_notes", return_value=notes),
              mock.patch.object(quota_alert, "resolve_codex_readings", return_value=readings),
              redirect_stdout(output)):
            agent_notes.main()

        self.assertEqual(self.pushed, [], "the canonical episode already records its refusal push")
        self.assertEqual(self.sent, [], "the canonical episode must suppress another refusal delivery")
        self.assertNotIn("codex 1", output.getvalue())
        saved = cast(dict[str, object], json.loads(quota_alert.STATE.read_text()))
        notices = cast(dict[str, dict[str, object]], saved["refusal_notices"])
        self.assertEqual(notices[str(self.root / ".codex")]["label"], "codex1")

    def test_current_session_is_found_up_the_process_tree(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        parent = os.getppid()
        grandparent = parent + 1000
        with mock.patch.object(quota_alert, "SESSIONS", sessions), mock.patch.object(
            subprocess, "run", side_effect=[
                subprocess.CompletedProcess(["ps"], 0, str(grandparent), ""),
                subprocess.CompletedProcess(["ps"], 0, "1", ""),
                subprocess.CompletedProcess(["ps"], 0, str(grandparent), ""),
            ]
        ):
            self.assertIsNone(quota_alert.current_session())
            # Two levels up, so the walk past a parent with no session file is exercised.
            _ = (sessions / f"{grandparent}.json").write_text(json.dumps({"name": "boss of bosses"}))
            self.assertEqual(quota_alert.current_session(), "boss of bosses")

    def test_inactive_above_threshold_unknown_or_reset_notes_are_quiet(self) -> None:
        past = local_reset(self.now - timedelta(hours=1))
        notes: Notes = [self.note("codex2.md", "inactive", "0"), self.note("claude1.md", "active", "2"),
                        self.note("claude2.md", "active", "null"), self.note("codex1.md", "active", "0", past)]
        self.assertEqual(self.alert(notes, self.now), [])
        self.assertEqual(self.sent, [])

    def test_explicit_refusal_time_is_recorded_without_replacing_it_with_pass_time(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        readings = self.alert_readings(notes, self.now)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        earlier = self.now - timedelta(minutes=1)
        _ = quota_alert.blocked(notes, identity, self.now, codex_readings=readings,
            refused_at=quota_alert.RefusedAt(earlier), window=refusals.KnownWindow("weekly"))
        self.assertEqual(refusals.load()[refusals.key("codex", identity.login)].at, earlier)

    def test_unknown_refusal_time_uses_pass_time_and_ignores_inherited_handoff_environment(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        readings = self.alert_readings(notes, self.now)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        with mock.patch.dict(os.environ, {"ACCOUNT_REFUSAL_AT": (self.now - timedelta(days=1)).isoformat()}):
            _ = quota_alert.blocked(notes, identity, self.now, codex_readings=readings,
                refused_at=quota_alert.RefusalTimeUnknown(), window=refusals.KnownWindow("weekly"))
        self.assertEqual(refusals.load()[refusals.key("codex", identity.login)].at, self.now)

    def test_codex_reaching_the_threshold_switches_nothing_and_only_reminds_the_phone(self) -> None:
        log = self.alert([self.note("codex1.md", "active", "1")], self.now)
        self.assertEqual([line for line in log if "switch" in line], [])
        self.assertEqual(self.assignments(),
                         {"direct": "codex", "cli": "claude", "module_review": "codex", "ask_a_friend": "caller"})
        self.assertEqual(self.sent, [])
        self.assertEqual([push.id for push in self.noted], ["quota.codex_next_move"])
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_a_quota_refusal_moves_every_codex_function_to_claude_once(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.assertEqual(self.refuse(), "switched to claude for codex1: direct, module_review")
        self.assertEqual(self.assignments(),
                         {"direct": "claude", "cli": "claude", "module_review": "claude", "ask_a_friend": "caller"})
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            switch = state["fallback"]
            self.assertIsInstance(switch, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(switch, quota_alert.AccountCodexQuotaFallback)
            self.assertIsInstance(switch.hold, quota_alert.NoFallbackHold)
        saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
        assert "switch" in saved
        self.assertNotIn("credits_off_until", saved["switch"])
        self.assertEqual(self.routing_entries()[-1]["kind"], "routed to claude")
        # The user puts module_review back on codex mid-episode; the next run does not move it again.
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "module_review", "codex"))
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["module_review"], "codex")

    def test_legacy_low_quota_episode_does_not_send_a_second_opening_alert(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "1")]
        _ = self.alert(notes, self.now)
        self.assertEqual(self.sent, [])
        self.assertEqual([push.id for push in self.noted], ["quota.codex_next_move"])
        original = quota_alert.STATE.read_text()
        self.assertIn('"codex1":', original)
        _ = quota_alert.STATE.write_text(original.replace('"codex1":', '"codex 1":'))
        self.sent.clear()
        self.pushed.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        self.assertEqual(len(self.noted), 1)
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
            self.assertNotIn("codex 1", state["episodes"])

    def test_legacy_fallback_owner_and_refusal_notice_load_canonically(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        _ = self.refuse()
        original = quota_alert.STATE.read_text()
        self.assertIn('"label": "codex1"', original)
        _ = quota_alert.STATE.write_text(original.replace('"label": "codex1"', '"label": "codex 1"'))
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.owner.label, "codex1")
            notices = state.get("refusal_notices", {})
            self.assertTrue(notices)
            for notice in notices.values():
                assert notice["kind"] == "known"
                self.assertEqual(notice["label"], "codex1")

    def test_successful_claude_fallback_is_logged_without_a_session_or_phone_push(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.sent.clear()
        self.pushed.clear()
        later = self.now + timedelta(minutes=5)
        log = self.block(notes, later)
        self.assertIn("switched to claude for codex1: direct, module_review", log)
        self.assertTrue(any(line.startswith("quota status codex1:") for line in log))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        self.sent.clear()
        _ = self.block(notes, later + timedelta(minutes=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])

    def test_failed_refusal_push_retries_on_the_next_refusal_then_stops(self) -> None:
        _ = quota_alert.REGISTRY.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        self.push_errors.append(quota_alert.PushFailed("phone offline"))
        first = self.block(notes, self.now + timedelta(minutes=1))
        self.assertIn("quota refusal codex1 -> user: not delivered: phone offline", first)
        second = self.block(notes, self.now + timedelta(minutes=2))
        self.assertIn("quota refusal codex1 -> user: sent", second)
        third = self.block(notes, self.now + timedelta(minutes=3))
        self.assertFalse(any(line.endswith("-> user: sent") for line in third))
        self.assertEqual(len(self.pushed), 2)

    def test_refusal_push_says_when_the_switch_had_already_happened(self) -> None:
        note = self.note("codex1.md", "active", "0")
        _ = self.alert([note], self.now)
        self.pushed.clear()
        stamp = self.now.isoformat(timespec="seconds")
        owner = self.alert_readings([note], self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with quota_alert.state_file() as state:
            self.assertEqual(quota_alert.switch_to_claude(state, note, stamp, owner),
                             quota_alert.CodexWorkMovedToClaude("codex1", ("direct", "module_review")))

        log = self.block([note], self.now + timedelta(minutes=1))
        self.assertTrue(any("New work had already moved to Claude" in line for line in log))
        self.assertEqual(self.pushed, [])

    def test_refusal_push_says_when_nothing_was_on_codex(self) -> None:
        registry = quota_alert.REGISTRY
        _ = registry.write_text(REGISTRY.replace("direct=codex", "direct=claude").replace(
            "module_review=codex", "module_review=claude"))
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now + timedelta(minutes=1))
        self.assertTrue(any("No delegated work needed to move" in line for line in log))
        self.assertEqual(self.pushed, [])

    def test_a_refusal_above_weekly_threshold_still_uses_fallback_when_no_codex_has_room(self) -> None:
        log = self.block([self.note("codex1.md", "active", "24")], self.now)
        self.assertTrue(any("switched to claude" in line for line in log), log)
        self.assertEqual(self.assignments()["direct"], "claude")
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.pushed), 0)
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"], {})

    def test_a_refusal_before_the_first_alert_or_after_an_earlier_episode_still_switches(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        self.assertIn("switched to claude for codex1: direct, module_review", self.block(notes, self.now))
        self.assertEqual(self.sent, [])
        _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(minutes=2))
        self.assertEqual(self.assignments()["direct"], "codex")
        _ = self.alert(notes, self.now + timedelta(minutes=4))
        self.sent.clear()
        log = self.block(notes, self.now + timedelta(minutes=6))
        self.assertIn("switched to claude for codex1: direct, module_review", log)
        self.assertEqual(self.sent, [])

    def test_claude_running_out_switches_nothing(self) -> None:
        _ = self.alert([self.note("claude2.md", "active", "0")], self.now)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.noted), 1)
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_recovery_moves_back_only_what_is_still_on_claude(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "module_review", "codex"))
        self.sent.clear()
        log = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: direct; left alone, "
                      + "re-assigned since the switch: module_review", log)
        self.assertEqual(self.assignments(),
                         {"direct": "codex", "cli": "claude", "module_review": "codex", "ask_a_friend": "caller"})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.routing_entries()[-1]["kind"], "routed back")

    def test_fallback_recovery_after_state_reload_keeps_its_pairing_time_and_journals_once(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        _ = self.block(notes, self.now)
        _ = self.block(notes, self.now)
        opened = self.routing_entries()
        self.assertEqual(len(opened), 1)
        self.assertEqual((opened[0]["kind"], opened[0]["owner"], opened[0]["cause"]),
                         ("routed to claude", "codex1", "refusal"))
        self.assertEqual(set(cast(list[str], opened[0]["moved"])), {"direct", "module_review"})
        # Reload through the persisted updater state, then recover in another pass.
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.at, opened[0]["fallback_at"])
        _ = quota_alert.edit_registry("agents_set_assignment", "module_review", "codex")
        later = self.now + timedelta(hours=1)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routed back"])
        self.assertEqual(entries[0]["fallback_at"], entries[1]["fallback_at"])
        self.assertEqual(entries[1]["restored"], ["direct"])
        self.assertEqual(entries[1]["kept"], ["module_review"])
        self.assertNotIn("move_id", entries[1])

    def test_partial_routing_restoration_records_failed_keys_and_never_claims_full_recovery(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        edit = quota_alert.edit_registry
        def fail_one(function: str, *arguments: str) -> str | None:
            if function == "agents_set_assignment" and arguments[0] == "module_review":
                return "registry locked"
            return edit(function, *arguments)
        with mock.patch.object(quota_alert, "edit_registry", side_effect=fail_one):
            _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routing not restored"])
        self.assertEqual(entries[1]["outcome"], "partial")
        self.assertEqual(entries[1]["restored"], ["direct"])
        self.assertEqual(entries[1]["failed"], ["module_review (registry locked)"])
        self.assertEqual(entries[1]["fallback_at"], entries[0]["fallback_at"])
        self.assertEqual(self.assignments()["module_review"], "claude")

    def test_unreadable_registry_records_failed_recovery_for_every_owed_key(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        with mock.patch.object(quota_alert, "registry_assignments", side_effect=OSError("registry unreadable")):
            _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routing not restored"])
        self.assertEqual(entries[1]["outcome"], "failed")
        self.assertEqual(entries[1]["restored"], [])
        self.assertEqual(set(cast(list[str], entries[1]["failed"])),
                         {"direct (registry unreadable)", "module_review (registry unreadable)"})

    def test_unknown_fallback_owner_stays_unknown_after_reload_and_recovery_on_a_noted_home(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        with quota_alert.state_file() as state:
            prior = state["fallback"]
            assert isinstance(prior, quota_alert.AccountCodexQuotaFallback)
            state["fallback"] = quota_alert.UnknownOwnerCodexQuotaFallback(prior.at, prior.moved)
        later = self.now + timedelta(hours=1)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        recovered = self.routing_entries()[-1]
        self.assertEqual(recovered["kind"], "routed back")
        self.assertEqual(recovered["owner"], "unknown")
        self.assertEqual(recovered["fallback_at"], prior.at)

    def test_mixed_routing_journal_closes_legacy_worker_wait_and_no_room_only_once(self) -> None:
        for kind in ("waited", "no room"):
            common = moves.MoveCommon(at=self.now.isoformat(), tool="codex", subject="workers", move_id=kind)
            entry: moves.MoveEntry = (moves.WaitedMove(**common, kind="waited", window="five_hour", reset=self.now.isoformat())
                if kind == "waited" else moves.NoRoomMove(**common, kind="no room", earliest_reset=self.now.isoformat()))
            path = moves.store_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as output:
                _ = output.write(json.dumps({**entry, "subject": "seats"}) + "\n")
        moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=self.now.isoformat(), tool="codex",
            fallback_at=self.now.isoformat(), owner="codex1", cause="refusal", why="Codex refused work for quota", moved=["direct"]))
        _ = self.note("codex1.md", "active", "100")
        for _ in range(2):
            quota_alert._finish_workers_transitions(self.now)  # pyright: ignore[reportPrivateUsage]
            quota_alert._room_again(self.now)  # pyright: ignore[reportPrivateUsage]
        closing = [entry for entry in moves.read_moves() if entry["kind"] in ("wait ended", "room again")]
        self.assertCountEqual([(entry["kind"], entry["move_id"]) for entry in closing],
                              [("wait ended", "waited"), ("room again", "no room")])

    def test_a_refused_switch_still_alerts_and_says_why(self) -> None:
        registry = quota_alert.REGISTRY
        _ = registry.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now)
        self.assertTrue(any(line.startswith("switch to claude for codex1 failed: ERROR: [direct.impl] effort 'ultra'") for line in log))
        self.assertIn("quota refusal codex1 -> user: sent", log)
        self.assertEqual(self.pushed[0][0::2], ("Codex work stopped for lack of usage", quota_alert.Ask("decision", REFUSAL_ACTION)))
        self.assertIn("Moving those jobs to Claude", self.pushed[0][1])
        second = self.block(notes, self.now)
        self.assertFalse(any(line.startswith("quota push") for line in second))
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            failed = state.get("refusal_notices", {})[str(self.root / ".codex")].get("switch_failed")
        self.assertIsInstance(failed, str)
        # A registry fixed mid-episode is not switched by the next run.
        _ = registry.write_text(REGISTRY)
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["direct"], "codex")

    def test_a_stopped_switch_leaves_the_registry_alone(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        # A live descendant holds this pipe until the editor's whole group dies.
        # Without killpg it could still reach the real registry editor later.
        gate = self.root / "release editor"
        os.mkfifo(gate)
        reader, writer = os.pipe()
        editor = self.root / "held editor.sh"
        _ = editor.write_text(
            "agents_set_all_assignments() {\n"
            + f"  (printf 'ready\\n' >&{writer}; read -r release < {shlex.quote(str(gate))}; "
            + f"source {shlex.quote(str(quota_alert.AGENTS))}; agents_set_all_assignments \"$@\") &\n"
            + "  wait \"$!\"\n}\n")
        real_popen = subprocess.Popen
        with os.fdopen(reader, "rb", buffering=0) as ready, os.fdopen(writer, "wb", buffering=0) as held:
            def start_editor(command: list[str], *, stdout: int, stderr: int, text: bool,
                             env: dict[str, str], start_new_session: bool) -> subprocess.Popen[str]:
                process = real_popen(command, stdout=stdout, stderr=stderr, text=text,
                    env=env, start_new_session=start_new_session, pass_fds=(held.fileno(),))
                held.close()
                communicate = process.communicate

                def kill_editor_group() -> None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

                self.addCleanup(kill_editor_group)

                def expire_when_ready(input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
                    if timeout is not None:
                        self.assertEqual(timeout, TEST_HANG_GUARD_SECS)
                        try:
                            self.assertTrue(select.select([ready], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(ready.readline(), b"ready\n")
                        except BaseException:
                            kill_editor_group()
                            raise
                        raise subprocess.TimeoutExpired(command, timeout)
                    return communicate(input, timeout=TEST_HANG_GUARD_SECS)

                _ = self.enterContext(mock.patch.object(process, "communicate", side_effect=expire_when_ready))
                return process

            with (mock.patch.object(quota_alert, "AGENTS", editor),
                  mock.patch.object(quota_alert, "EDIT_TIMEOUT", TEST_HANG_GUARD_SECS),
                  mock.patch.object(subprocess, "Popen", side_effect=start_editor)):
                line = self.refuse()
            self.assertEqual(line, "switch to claude for codex1 failed: agents_set_all_assignments took longer "
                             + f"than {TEST_HANG_GUARD_SECS} s and was stopped")
            self.assertTrue(select.select([ready], [], [], TEST_HANG_GUARD_SECS)[0])
            self.assertEqual(ready.read(), b"", "an editor descendant can still change the registry")
        self.assertEqual(self.assignments()["direct"], "codex")
        self.assertEqual([path.name for path in self.root.glob("agents.conf.*")], [])

    def test_selected_allowance_restores_assignments_despite_low_weekly_episode(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.sent.clear()
        log = self.alert([self.note("codex1.md", "inactive", "0"), self.note("codex2.md", "active", "1")],
                                self.now + timedelta(minutes=2))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: direct, module_review", log)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.assertFalse(any(text.startswith("Quota alert:") for _, text in self.sent))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.routing_entries()[-1]["kind"], "routed back")
        _ = self.alert([self.note("codex1.md", "inactive", "0"), self.note("codex2.md", "active", "90")],
                              self.now + timedelta(minutes=4))
        self.assertEqual(self.assignments()["direct"], "codex")

    def test_refresh_restores_assignments_and_records_recovery_without_session_delivery(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.sent.clear()
        lines = self.refresh([self.note("codex1.md", "active", "100")], "natedev",
                             self.now + timedelta(minutes=3))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: direct, module_review", lines)
        self.assertEqual(self.assignments()["module_review"], "codex")
        self.assertEqual(self.sent, [])
        self.assertEqual([row["kind"] for row in self.routing_entries()], ["routed to claude", "routed back"])

    def test_notice_shows_each_switch_once_and_only_in_the_owner_session(self) -> None:
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.assertIsNone(quota_alert.notice(lambda: "boss of bosses"))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith(f"Codex refused work for quota (codex1 at 0%, resets {self.resets}): direct, "
                                        + "module_review moved to Claude at "))
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith("Codex recovered at "))
        self.assertTrue(text.endswith(": moved back from Claude to Codex: direct, module_review."))


if __name__ == "__main__":
    _ = unittest.main()
