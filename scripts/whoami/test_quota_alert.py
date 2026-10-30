"""Quota alerts: configuration, reminders and storage."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timedelta
from unittest import mock
from scripts.whoami import quota_alert_test_support as support
import quota_alert
from scripts.whoami import agent_accounts
from agent_accounts import Quota, Report
import usage_store
from agent_notes import local_reset
from scripts.accounts import accounts, moves, next_moves, role_map
from user_action import ActionRequired
from scripts.whoami.quota_alert_test_support import Notes, REAL_RECIPIENTS, REAL_TELL_USER, REGISTRY as REGISTRY, running

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
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

    def test_claude_running_out_switches_nothing(self) -> None:
        _ = self.alert([self.note("claude2.md", "active", "0")], self.now)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.noted), 1)
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))


class QuotaAlertRerunTests(support.CodexRerunFixture):
    def partially_restore(self) -> None:
        owner = self.readings(self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with mock.patch.object(accounts, "pick", return_value=accounts.NoAccountWithRoom(accounts.NoResetKnown(), ())):
            _ = quota_alert.blocked([self.note(1)], owner, self.now, codex_readings=self.readings(self.now))

        def fail_one(editor: str, *arguments: str) -> str | None:
            if arguments == ("module_review", "codex"):
                return "fixture registry locked"
            return self.edit_assignments(editor, *arguments)

        with mock.patch.object(quota_alert, "edit_registry", side_effect=fail_one):
            _ = self.alert(self.now + timedelta(minutes=1), left=90)
        self.assertEqual(self.read_assignments()["direct"], "codex")
        self.assertEqual(self.read_assignments()["module_review"], "claude")

    def test_queued_claude_switch_keeps_failed_restore_keys_until_the_next_recovery(self) -> None:
        class Killed(BaseException):
            pass

        self.partially_restore()
        _ = next_moves.add_next_move("workers", 1, "claude")
        later = self.now + timedelta(minutes=2)
        with mock.patch.object(quota_alert, "_record_claude_switch", side_effect=Killed()):
            with self.assertRaises(Killed):
                _ = self.alert(later, left=0)
        _ = self.alert(later + timedelta(seconds=1), left=0)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(set(fallback.moved), {"direct", "module_review"})
        _ = self.alert(self.now + timedelta(days=4), left=90)
        self.assertEqual({self.read_assignments()[key] for key in ("direct", "module_review")}, {"codex"})
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_repeat_refusal_switches_keys_restored_before_a_partial_failure(self) -> None:
        class Killed(BaseException):
            pass

        self.partially_restore()
        later = self.now + timedelta(minutes=2)
        owner = self.readings(later).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with mock.patch.object(accounts, "pick", return_value=accounts.NoAccountWithRoom(accounts.NoResetKnown(), ())):
            with mock.patch.object(quota_alert, "_record_claude_switch", side_effect=Killed()):
                with self.assertRaises(Killed):
                    _ = quota_alert.blocked([self.note(1)], owner, later, codex_readings=self.readings(later))
            _ = quota_alert.blocked([self.note(1)], owner, later, codex_readings=self.readings(later))
        self.assertEqual(self.read_assignments()["direct"], "claude")
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(set(fallback.moved), {"direct", "module_review"})
        _ = self.alert(later + timedelta(minutes=1), left=90)
        self.assertEqual({self.read_assignments()[key] for key in ("direct", "module_review")}, {"codex"})

    def test_restoration_reports_survive_a_stop_or_failed_journal_append(self) -> None:
        class Killed(BaseException):
            pass

        for index, fault in enumerate((Killed(), OSError("fixture journal unavailable"))):
            with self.subTest(fault=type(fault).__name__):
                at = self.now + timedelta(minutes=3 * index)
                owner = self.readings(at).selected_account
                assert isinstance(owner, agent_accounts.KnownCodexAccount)
                with mock.patch.object(accounts, "pick", return_value=accounts.NoAccountWithRoom(accounts.NoResetKnown(), ())):
                    _ = quota_alert.blocked([self.note(1)], owner, at, codex_readings=self.readings(at))
                before = len([row for row in moves.read_journal() if row["kind"] == "routed back"])
                later = at + timedelta(minutes=1)
                with mock.patch.object(moves, "append_routing", side_effect=fault):
                    try:
                        _ = self.alert(later, left=90)
                    except (Killed, OSError):
                        pass
                self.assertEqual(self.read_assignments()["direct"], "codex")
                _ = self.alert(later + timedelta(seconds=1), left=90)
                rows = [row for row in moves.read_journal() if row["kind"] == "routed back"]
                self.assertEqual(len(rows), before + 1)
                shown = quota_alert.notice(lambda: quota_alert.OWNER)
                assert shown is not None
                self.assertIn("Codex recovered", shown)
                self.assertIn("direct, module_review", shown)
                _ = self.alert(later + timedelta(seconds=2), left=90)
                self.assertEqual(len([row for row in moves.read_journal() if row["kind"] == "routed back"]), before + 1)
                self.assertIsNone(quota_alert.notice(lambda: quota_alert.OWNER))

    def test_restoration_notice_is_saved_once_after_an_interrupted_successful_append(self) -> None:
        class Killed(BaseException):
            pass

        owner = self.readings(self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with mock.patch.object(accounts, "pick", return_value=accounts.NoAccountWithRoom(accounts.NoResetKnown(), ())):
            _ = quota_alert.blocked([self.note(1)], owner, self.now, codex_readings=self.readings(self.now))
        _ = quota_alert.notice(lambda: quota_alert.OWNER)
        append = moves.append_routing

        def append_then_stop(row: moves.RoutingEntry) -> None:
            append(row)
            raise Killed()

        later = self.now + timedelta(minutes=1)
        with mock.patch.object(moves, "append_routing", side_effect=append_then_stop):
            with self.assertRaises(Killed):
                _ = self.alert(later, left=90)
        self.assertEqual(len([row for row in moves.read_journal() if row["kind"] == "routed back"]), 1)
        self.assertIsNone(quota_alert.notice(lambda: quota_alert.OWNER))
        _ = self.alert(later + timedelta(seconds=1), left=90)
        shown = quota_alert.notice(lambda: quota_alert.OWNER)
        assert shown is not None
        self.assertEqual(shown.count("Codex recovered"), 1)
        _ = self.alert(later + timedelta(seconds=2), left=90)
        self.assertIsNone(quota_alert.notice(lambda: quota_alert.OWNER))
        self.assertEqual(len([row for row in moves.read_journal() if row["kind"] == "routed back"]), 1)

    def test_pending_switch_is_recovered_only_by_the_switching_pass_and_tells_owner_once(self) -> None:
        class Killed(BaseException):
            pass

        _ = next_moves.add_next_move("workers", 1, "claude")
        edited = False

        def edit_then_stop_on_read(editor: str, *arguments: str) -> str | None:
            nonlocal edited
            result = self.edit_assignments(editor, *arguments)
            edited = True
            return result

        def read_until_edit() -> dict[str, str]:
            if edited:
                raise Killed()
            return self.read_assignments()

        with (mock.patch.object(quota_alert, "edit_registry", side_effect=edit_then_stop_on_read),
              mock.patch.object(quota_alert, "registry_assignments", side_effect=read_until_edit)):
            with self.assertRaises(Killed):
                _ = self.alert(left=0)
        before = quota_alert.STATE.read_bytes()
        with (mock.patch.object(quota_alert, "registry_assignments", side_effect=AssertionError("a state read must not switch")),
              mock.patch.object(moves, "append_routing", side_effect=AssertionError("a state read must not journal"))):
            with quota_alert.state_file() as state:
                self.assertIn("pending_switch", state)
            self.assertIsNone(quota_alert.notice(lambda: quota_alert.OWNER))
        self.assertEqual(quota_alert.STATE.read_bytes(), before)
        _ = self.alert(self.now + timedelta(seconds=1), left=0)
        shown = quota_alert.notice(lambda: quota_alert.OWNER)
        assert shown is not None
        self.assertIn("direct, module_review moved to Claude", shown)
        _ = self.alert(self.now + timedelta(seconds=2), left=0)
        self.assertIsNone(quota_alert.notice(lambda: quota_alert.OWNER))
        self.assertEqual(len([row for row in moves.read_journal() if row["kind"] == "routed to claude"]), 1)

    def test_old_workers_requests_share_one_role_read_per_pass(self) -> None:
        stamp = self.now.isoformat()
        for index in range(3):
            moves.append_move(moves.RequestedMove(kind="requested", at=stamp, tool="codex", subject="workers",
                move_id=f"old-{index}", cause="auto", why=f"auto: entry-{index}",
                **{"from": "codex2", "to": "codex3"}, window="weekly"))
        with mock.patch.object(role_map, "read_role_map", wraps=role_map.read_role_map) as read_role:
            _ = quota_alert.run_workers_next_moves(self.readings(self.now), self.now)
        self.assertEqual(read_role.call_count, 1)

    def test_stopped_claude_switch_recovers_fallback_and_journals_once(self) -> None:
        class Killed(BaseException):
            pass

        note = self.note(1)
        owner = self.readings(self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        stamp = self.now.isoformat(timespec="seconds")
        hold = quota_alert.ScheduledMoveHold(self.now + timedelta(days=2))
        destination = next_moves.ClaudeWorkersTarget("claude4")
        changed = False

        def edit_then_stop_on_read(editor: str, *arguments: str) -> str | None:
            nonlocal changed
            result = self.edit_assignments(editor, *arguments)
            changed = True
            return result

        def read_until_edit() -> dict[str, str]:
            if changed:
                raise Killed()
            return self.read_assignments()

        with (mock.patch.object(quota_alert, "edit_registry", side_effect=edit_then_stop_on_read),
              mock.patch.object(quota_alert, "registry_assignments", side_effect=read_until_edit)):
            with self.assertRaises(Killed), quota_alert.state_file() as state:
                _ = quota_alert.switch_to_claude(state, note, stamp, owner, hold=hold,
                    why="auto: rerun-entry", claude_account=destination)
        self.assertEqual(self.read_assignments()["direct"], "claude")
        with quota_alert.state_file() as state:
            _ = quota_alert.switch_to_claude(state, note, (self.now + timedelta(minutes=1)).isoformat(),
                owner, hold=hold, why="auto: rerun-entry", claude_account=destination)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual((fallback.owner, fallback.at, fallback.moved, fallback.hold),
                             (owner, stamp, ("direct", "module_review"), hold))
            self.assertEqual(fallback.claude_account, destination)
            _ = quota_alert.switch_to_claude(state, note, stamp, owner, hold=hold,
                why="auto: rerun-entry", claude_account=destination)
        routing = [row for row in moves.read_journal() if row["kind"] == "routed to claude"]
        self.assertEqual(len(routing), 1)
        self.assertEqual((routing[0]["fallback_at"], routing[0]["moved"], routing[0]["why"]),
                         (stamp, ["direct", "module_review"], "auto: rerun-entry"))
        self.assertEqual(routing[0].get("claude_account"), "claude4")
        self.assertEqual(self.read_assignments()["ask_a_friend"], "caller")

    def test_failed_restore_keeps_only_failed_keys_for_the_next_pass(self) -> None:
        class Killed(BaseException):
            pass

        owner = self.readings(self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        stamp = self.now.isoformat(timespec="seconds")
        with quota_alert.state_file() as state:
            _ = quota_alert.switch_to_claude(state, self.note(1), stamp, owner)
        later = self.now + timedelta(hours=1)
        readings = self.readings(later, left=100)

        def fail_one(editor: str, *arguments: str) -> str | None:
            if arguments == ("module_review", "codex"):
                return "fixture registry locked"
            return self.edit_assignments(editor, *arguments)

        with mock.patch.object(quota_alert, "edit_registry", side_effect=fail_one):
            with quota_alert.state_file() as state:
                _ = quota_alert.switch_back(state, later, readings)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual((fallback.at, fallback.moved), (stamp, ("module_review",)))
        self.assertEqual(self.read_assignments()["direct"], "codex")
        self.assertEqual(self.read_assignments()["module_review"], "claude")
        with mock.patch.object(quota_alert, "registry_assignments", side_effect=Killed()):
            with self.assertRaises(Killed), quota_alert.state_file() as state:
                _ = quota_alert.switch_back(state, later + timedelta(seconds=1), readings)
        with mock.patch.object(quota_alert, "edit_registry", side_effect=self.edit_assignments) as edit:
            with quota_alert.state_file() as state:
                _ = quota_alert.switch_back(state, later + timedelta(minutes=1), readings)
        edit.assert_called_once_with("agents_set_assignment", "module_review", "codex")
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
        self.assertEqual(self.read_assignments()["module_review"], "codex")
        routing = [row for row in moves.read_journal()
                   if row["kind"] == "routing not restored" or row["kind"] == "routed back"]
        self.assertEqual([row["kind"] for row in routing], ["routing not restored", "routed back"])
        self.assertEqual([row["fallback_at"] for row in routing], [stamp, stamp])

    def test_stopped_workers_request_retries_same_move_before_consuming_queue(self) -> None:
        class Killed(BaseException):
            pass

        head = next_moves.add_next_move("workers", 1, "codex3")
        readings = self.readings(self.now, left=1, spare_accounts=((3, 80, "0"),))
        with mock.patch.object(accounts, "swap", side_effect=Killed()):
            with self.assertRaises(Killed):
                _ = quota_alert.run_workers_next_moves(readings, self.now)
        requested = [row for row in moves.read_moves() if row["kind"] == "requested"]
        self.assertEqual(len(requested), 1)
        original_move_id = requested[0]["move_id"]
        self.assertEqual(role_map.read_role_map().workers, "codex1")
        self.assertEqual(next_moves.read_next_moves(), (head,))

        def swap_after_request(group: str, target: str, *, why: str, cause: str) -> list[str]:
            self.assertEqual(next_moves.read_next_moves(), (head,))
            self.assertEqual((group, target, why, cause), ("workers", "codex3", f"auto: {head.id}", "auto"))
            accounts.set_role("workers", target)
            return []

        with mock.patch.object(accounts, "swap", side_effect=swap_after_request) as swap:
            returned = quota_alert.run_workers_next_moves(readings, self.now + timedelta(seconds=1))
        swap.assert_called_once()
        self.assertEqual(returned.workers, accounts.CodexWorkersAccount("codex3", self.root / ".codex3"))
        self.assertEqual(role_map.read_role_map().workers, "codex3")
        self.assertEqual(next_moves.read_next_moves(), ())
        requested = [row for row in moves.read_moves() if row["kind"] == "requested"]
        self.assertEqual({row["move_id"] for row in requested}, {original_move_id})
        with mock.patch.object(accounts, "swap", side_effect=AssertionError("finished move repeated")):
            _ = quota_alert.run_workers_next_moves(returned, self.now + timedelta(seconds=2))


if __name__ == "__main__":
    _ = unittest.main()
