"""Account ownership, live swap advice and fallback recovery across seats swaps."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import note_file
import quota_alert
import agent_accounts as sibling_agent_accounts
from scripts.whoami import agent_accounts
from agent_accounts import CodexHomeSource, Quota, Report
from scripts.accounts import accounts
from test_quota_alert import REGISTRY


class CodexAccountAlertTests(unittest.TestCase):
    root: Path = Path()
    now: datetime = datetime.now(timezone.utc)
    sent: list[str] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.sent = []
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root),
            "CODEX_CONFIG_FILE": str(self.root / "absent"),
            "CODEX_MODELS_CACHE_FILE": str(self.root / "absent"),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(self.root / "synced"),
        })))
        (self.root / "synced").touch()
        _ = self.enterContext(patch.object(note_file, "AGENTS_DIR", self.root))
        config = self.root / "showrunners.json"
        _ = config.write_text(json.dumps({"threshold_percent": 1, "repeat_minutes": 30,
            "stall_minutes": 5, "faults_to": "director", "always": ["director"]}))
        registry = self.root / "agents.conf"
        _ = registry.write_text(REGISTRY)
        _ = self.enterContext(patch.object(quota_alert, "CONFIG", config))
        _ = self.enterContext(patch.object(quota_alert, "STATE", self.root / "alerts.json"))
        _ = self.enterContext(patch.object(quota_alert, "REGISTRY", registry))
        _ = self.enterContext(patch.object(quota_alert, "recipients", return_value=["director"]))
        _ = self.enterContext(patch.object(quota_alert, "not_live", return_value=[]))
        _ = self.enterContext(patch.object(quota_alert, "relay", side_effect=self.relay))
        _ = self.enterContext(patch.object(quota_alert, "tell_user", side_effect=self.push))

    def relay(self, recipient: str, text: str, key: str | None) -> None:
        del recipient, key
        self.sent.append(text)

    def push(self, summary: str, text: str, ask: quota_alert.Ask | None) -> None:
        del summary, ask
        self.sent.append(text)

    def account(self, number: int, *, default: bool = False) -> agent_accounts.KnownCodexAccount:
        home = self.root / (".codex" if default else f".codex{number}")
        home.mkdir(exist_ok=True)
        login = f"account{number}@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {
            "id_token": f"header.{claims}.signature",
        }}))
        _ = self.note(number, "inactive", 80)
        return agent_accounts.KnownCodexAccount(home, login, f"codex {number}")

    def note(self, number: int, state: str, left: int, credits: str = "0") -> quota_alert.AgentNote:
        path = self.root / f"codex {number}.md"
        _ = path.write_text(f"---\nlogin: account{number}@example.invalid\nstate: {state}\n"
            + f"weekly_remaining_usage: {left}\ncredit_balance: {credits}\n"
            + f"resets: {(self.now + timedelta(days=2)).isoformat()}\n---\n")
        note = note_file.read_note(path)
        assert note is not None
        return note

    def report(self, account: agent_accounts.KnownCodexAccount, left: int = 1,
               credits: str | None = "0", *, five_left: int = 60,
               expired: bool = False, failed: bool = False) -> Report:
        reset = self.now + timedelta(days=-1 if expired else 2)
        return Report("Codex", email=account.login, source=CodexHomeSource(account.home),
            quotas=[] if failed else [Quota("Weekly", 100 - left, reset)],
            five_hour=None if failed else Quota("5-hour", 100 - five_left, reset),
            credit_balance=credits, problem="read failed" if failed else None)

    def readings(self, selected: agent_accounts.KnownCodexAccount, *reports: Report
                 ) -> quota_alert.CodexAlertPassReadings:
        label = "default" if selected.home.name == ".codex" else selected.label
        self.assertEqual(accounts.main(["set-role", "seats", label]), 0)
        return quota_alert.CodexAlertPassReadings(
            accounts.role_account("seats"), selected,
            tuple(quota_alert.CodexHomeReading(report.source.home, report, self.now)
                  for report in reports if isinstance(report.source, CodexHomeSource)),
        )

    def test_acknowledged_account_stays_quiet_after_swap_away_and_back(self) -> None:
        a, b = self.account(1), self.account(2)
        a_pass = self.readings(a, self.report(a), self.report(b, 70))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 70)],
                              self.now, codex_readings=a_pass)
        _ = quota_alert.acknowledge("codex 1", now=self.now)
        self.sent.clear()
        self.now += timedelta(hours=1)
        b_pass = self.readings(b, self.report(a), self.report(b, 70))
        _ = quota_alert.alert([self.note(1, "inactive", 1), self.note(2, "active", 70)],
                              self.now, codex_readings=b_pass)
        self.now += timedelta(hours=1)
        a_pass = self.readings(a, self.report(a), self.report(b, 70))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 70)],
                              self.now, codex_readings=a_pass)
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            self.assertIn("acknowledged", state["episodes"]["codex 1"])

    def test_stale_active_spare_never_opens_an_episode_with_or_without_credits(self) -> None:
        a, b = self.account(1), self.account(2, default=True)
        for credits in ("0", "37"):
            with self.subTest(credits=credits):
                quota_alert.STATE.unlink(missing_ok=True)
                readings = self.readings(a, self.report(a), self.report(b, 0, credits, five_left=0))
                _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "active", 0, credits)],
                                      self.now, codex_readings=readings)
                with quota_alert.state_file() as state:
                    self.assertEqual(set(state["episodes"]), {"codex 1"})

    def test_spare_episode_closes_only_when_its_own_quota_recovers(self) -> None:
        a, b = self.account(1), self.account(2)
        selected_a = self.readings(a, self.report(a))
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=selected_a)
        _ = quota_alert.acknowledge("codex 1", now=self.now)
        selected_b = self.readings(b, self.report(a, 90), self.report(b, 90))
        _ = quota_alert.alert([self.note(1, "inactive", 90), self.note(2, "active", 90)],
                              self.now + timedelta(minutes=1), codex_readings=selected_b)
        with quota_alert.state_file() as state:
            self.assertNotIn("codex 1", state["episodes"])

    def test_spare_recommendation_uses_default_target_and_live_credits(self) -> None:
        a, b = self.account(1), self.account(2, default=True)
        readings = self.readings(a, self.report(a), self.report(b, 1, "17", five_left=0))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 1)],
                              self.now, codex_readings=readings)
        text = "\n".join(self.sent)
        self.assertIn("swap seats default", text)
        self.assertIn("codex 2", text)
        self.assertIn("1%", text)
        self.assertIn("17", text)

    def test_spare_with_used_up_window_and_no_credits_is_not_recommended(self) -> None:
        a, b = self.account(1), self.account(2)
        readings = self.readings(a, self.report(a), self.report(b, 80, "0", five_left=0))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 80)],
                              self.now, codex_readings=readings)
        self.assertNotIn('swap seats "codex 2"', "\n".join(self.sent))

    def test_failed_or_expired_spare_read_never_uses_saved_credits_for_advice(self) -> None:
        a, b = self.account(1), self.account(2)
        for report in (self.report(b, 90, None, failed=True), self.report(b, 90, "0", expired=True)):
            with self.subTest(problem=report.problem):
                quota_alert.STATE.unlink(missing_ok=True)
                self.sent.clear()
                readings = self.readings(a, self.report(a), report)
                _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 90, "999")],
                                      self.now, codex_readings=readings)
                self.assertNotIn('swap seats "codex 2"', "\n".join(self.sent))

    def test_unchecked_target_is_not_recommended_even_with_live_allowance(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = (b.home / "unexpected").write_text("private entry")
        readings = self.readings(a, self.report(a), self.report(b, 90))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 90)],
                              self.now, codex_readings=readings)
        self.assertNotIn('swap seats "codex 2"', "\n".join(self.sent))

    def test_unknown_selected_identity_does_not_alert_old_active_notes(self) -> None:
        a = self.account(1)
        known = self.readings(a, self.report(a))
        unknown = quota_alert.CodexAlertPassReadings(known.seats,
            agent_accounts.UnknownCodexAccount(a.home, "sign-in unreadable"), known.checked_homes)
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=unknown)
        self.assertEqual(self.sent, [])

    def test_outgoing_or_unknown_refusal_names_account_without_switching_assignments(self) -> None:
        a, b = self.account(1), self.account(2)
        readings = self.readings(b, self.report(a, 0), self.report(b, 90))
        for refusing in (a, agent_accounts.UnknownCodexAccount(a.home, "server identity lost")):
            with self.subTest(refusing=refusing):
                self.sent.clear()
                lines = quota_alert.blocked([self.note(1, "inactive", 0), self.note(2, "active", 90)],
                    refusing, self.now, codex_readings=readings)
                self.assertIn("codex", " ".join(lines).lower())
                with quota_alert.state_file() as state:
                    self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
                self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())

    def test_selected_refusal_owns_fallback_and_credits_restore_owed_assignments(self) -> None:
        a = self.account(1)
        readings = self.readings(a, self.report(a, 0))
        _ = quota_alert.blocked([self.note(1, "active", 0)], a, self.now, codex_readings=readings)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.owner, a)
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "fix", "codex"))
        funded = self.readings(a, self.report(a, 0, "19", five_left=0))
        _ = quota_alert.alert([self.note(1, "active", 0, "19")],
                              self.now + timedelta(minutes=1), codex_readings=funded)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
        registry = quota_alert.REGISTRY.read_text()
        self.assertIn("delegate=codex", registry)
        self.assertIn("fix=codex", registry)

    def test_missing_and_old_fallback_records_decode_named_states_and_recover(self) -> None:
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
        _ = quota_alert.REGISTRY.write_text(REGISTRY.replace("delegate=codex", "delegate=claude"))
        _ = quota_alert.STATE.write_text(json.dumps({"episodes": {}, "switch": {
            "at": self.now.isoformat(), "moved": ["delegate"],
        }}))
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.UnknownOwnerCodexQuotaFallback)
        a = self.account(1)
        readings = self.readings(a, self.report(a, 90))
        _ = quota_alert.alert([self.note(1, "active", 90)], self.now, codex_readings=readings)
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())

    def test_shared_account_identity_accepts_note_names_without_regard_to_case(self) -> None:
        a = self.account(1)
        _ = (self.root / "codex 1.md").rename(self.root / "Codex 1.md")
        identity = agent_accounts.codex_account(a.home)
        self.assertIsInstance(identity, agent_accounts.KnownCodexAccount)
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        self.assertEqual((identity.home, identity.login, identity.label), (a.home, a.login, "Codex 1"))

    def test_selected_and_spare_readings_share_the_ten_minute_limit(self) -> None:
        a, b = self.account(1), self.account(2)
        readings = self.readings(a, self.report(a), self.report(b, 80))
        at_limit = self.now + timedelta(minutes=10)
        self.assertIsNone(quota_alert.selected_codex_report(readings, at_limit).problem)
        self.assertIn('swap seats "codex 2"', quota_alert.codex_swap_line(readings, at_limit))
        after_limit = at_limit + timedelta(microseconds=1)
        self.assertIsNotNone(quota_alert.selected_codex_report(readings, after_limit).problem)
        self.assertEqual(quota_alert.codex_swap_line(readings, after_limit), "")

    def test_standalone_refusal_uses_readings_collected_after_the_call_starts(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = self.readings(a)
        async def live(home: Path) -> Report:
            return self.report(a if home == a.home else b, 0 if home == a.home else 80)
        with patch.object(quota_alert, "codex_live", side_effect=live), patch.object(quota_alert, "tell_user", wraps=self.push) as push:
            _ = quota_alert.blocked([self.note(1, "active", 0)], a)
        self.assertIn('swap seats "codex 2"', "\n".join(self.sent))
        self.assertIn('swap seats "codex 2"', cast(str, push.call_args.args[1]))

    def test_standalone_alert_message_and_refresh_accept_their_new_readings(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = self.readings(a)
        note = self.note(1, "active", 1)
        async def live(home: Path) -> Report:
            return self.report(a if home == a.home else b, 1 if home == a.home else 80)
        with patch.object(quota_alert, "codex_live", side_effect=live):
            _ = quota_alert.alert([note])
            self.assertIn('swap seats "codex 2"', "\n".join(self.sent))
            text = quota_alert.message(note, [note], quota_alert.load_config())
            self.assertIn('swap seats "codex 2"', text)
            quota_alert.STATE.unlink(missing_ok=True)
            self.sent.clear()
            _ = quota_alert.refresh([note], None)
            self.assertIn('swap seats "codex 2"', "\n".join(self.sent))

    def test_refusals_create_no_weekly_episodes_or_false_recovery_notices(self) -> None:
        a, b = self.account(1), self.account(2)
        for selected, left in ((a, 0), (b, 90)):
            with self.subTest(selected=selected.label):
                quota_alert.STATE.unlink(missing_ok=True)
                _ = quota_alert.REGISTRY.write_text(REGISTRY)
                readings = self.readings(selected, self.report(a, left), self.report(b, 90))
                _ = quota_alert.blocked([self.note(1, "inactive", left), self.note(2, "active", 90)],
                    a, self.now, codex_readings=readings)
                with quota_alert.state_file() as state:
                    self.assertEqual(state["episodes"], {})
                self.sent.clear()
                _ = quota_alert.alert([self.note(1, "inactive", 90), self.note(2, "active", 90)],
                    self.now, codex_readings=self.readings(b, self.report(a, 90), self.report(b, 90)))
                if selected == b:
                    self.assertFalse(any(text.startswith("Quota restored:") for text in self.sent))

    def test_spare_reset_closes_acknowledgement_before_the_new_low_week(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now,
            codex_readings=self.readings(a, self.report(a), self.report(b, 80)))
        _ = quota_alert.acknowledge("codex 1", now=self.now)
        self.now += timedelta(days=3)
        _ = quota_alert.alert([self.note(1, "inactive", 1), self.note(2, "active", 80)], self.now,
            codex_readings=self.readings(b, self.report(a), self.report(b, 80)))
        with quota_alert.state_file() as state:
            self.assertNotIn("codex 1", state["episodes"])
        self.sent.clear()
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now,
            codex_readings=self.readings(a, self.report(a), self.report(b, 80)))
        self.assertTrue(any(text.startswith("Quota alert:") for text in self.sent))

    def test_outgoing_refusal_user_push_has_the_session_swap_line(self) -> None:
        a, b, c = self.account(1), self.account(2), self.account(3)
        readings = self.readings(b, self.report(a, 0), self.report(b, 0), self.report(c, 80))
        with patch.object(quota_alert, "tell_user", wraps=self.push) as push:
            _ = quota_alert.blocked([self.note(1, "inactive", 0)], a, self.now, codex_readings=readings)
        advice = 'swap seats "codex 3"'
        self.assertIn(advice, cast(str, push.call_args.args[1]))
        self.assertTrue(any(advice in text for text in self.sent))

    def test_failed_restoration_is_attempted_and_reported_once(self) -> None:
        a = self.account(1)
        _ = quota_alert.blocked([self.note(1, "active", 0)], a, self.now,
            codex_readings=self.readings(a, self.report(a, 0)))
        funded = self.readings(a, self.report(a, 90))
        self.sent.clear()
        with patch.object(quota_alert, "edit_registry", return_value="editor refused") as edit:
            _ = quota_alert.alert([self.note(1, "active", 90)], self.now, codex_readings=funded)
            count = edit.call_count
            messages = len(self.sent)
            with quota_alert.state_file() as state:
                unseen = list(state.get("unseen", []))
            _ = quota_alert.alert([self.note(1, "active", 90)], self.now, codex_readings=funded)
            self.assertEqual(edit.call_count, count)
            self.assertEqual(len(self.sent), messages)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
            self.assertEqual(state.get("unseen"), unseen)

    def test_unknown_refusal_is_sent_once_until_a_known_account_is_reported(self) -> None:
        a = self.account(1)
        readings = self.readings(a, self.report(a, 90))
        unknown = agent_accounts.UnknownCodexAccount(a.home, "sign-in unreadable")
        _ = quota_alert.blocked([], unknown, self.now, codex_readings=readings)
        sent = len(self.sent)
        _ = quota_alert.blocked([], unknown, self.now, codex_readings=readings)
        self.assertEqual(len(self.sent), sent)
        _ = quota_alert.blocked([self.note(1, "active", 90)], a, self.now, codex_readings=readings)
        _ = quota_alert.blocked([], unknown, self.now, codex_readings=readings)
        self.assertGreater(len(self.sent), sent)
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"], {})

    def test_refusal_after_credit_recovery_announces_the_new_move(self) -> None:
        a = self.account(1)
        note = self.note(1, "active", 0)
        low = self.readings(a, self.report(a, 0))
        _ = quota_alert.alert([note], self.now, codex_readings=low)
        _ = quota_alert.blocked([note], a, self.now, codex_readings=low)
        funded = self.readings(a, self.report(a, 0, "19", five_left=0))
        _ = quota_alert.alert([note], self.now, codex_readings=funded)
        with quota_alert.state_file() as state:
            self.assertIn("refusal_push", state["episodes"][a.label])
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        self.sent.clear()
        with patch.object(quota_alert, "relay", wraps=self.relay) as relay, patch.object(
                quota_alert, "tell_user", wraps=self.push) as push:
            _ = quota_alert.blocked([note], a, self.now, codex_readings=low)
            relay.assert_called_once()
            push.assert_called_once()
            self.assertIn("moved", cast(str, relay.call_args.args[1]))
            self.assertIn("moved", cast(str, push.call_args.args[1]))
            _ = quota_alert.blocked([note], a, self.now, codex_readings=low)
            relay.assert_called_once()
            push.assert_called_once()
        self.assertIn("delegate=claude", quota_alert.REGISTRY.read_text())

    def test_outgoing_refusal_does_not_silence_a_later_selected_move(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = quota_alert.blocked([self.note(1, "inactive", 80)], a, self.now,
            codex_readings=self.readings(b, self.report(a, 80), self.report(b, 80)))
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        selected = self.readings(a, self.report(a, 0), self.report(b, 80))
        note = self.note(1, "active", 0)
        _ = quota_alert.alert([note], self.now, codex_readings=selected)
        with quota_alert.state_file() as state:
            self.assertIn("push", state.get("refusal_notices", {})[str(a.home)])
        self.sent.clear()
        with patch.object(quota_alert, "relay", wraps=self.relay) as relay, patch.object(
                quota_alert, "tell_user", wraps=self.push) as push:
            _ = quota_alert.blocked([note], a, self.now, codex_readings=selected)
            relay.assert_called_once()
            push.assert_called_once()
            self.assertIn("moved", cast(str, relay.call_args.args[1]))
            self.assertIn("moved", cast(str, push.call_args.args[1]))
        self.assertIn("delegate=claude", quota_alert.REGISTRY.read_text())

    def test_switch_failure_messages_repeat_only_when_the_failure_changes(self) -> None:
        a = self.account(1)
        readings = self.readings(a, self.report(a, 0))
        note = self.note(1, "active", 0)
        with patch.object(quota_alert, "edit_registry", side_effect=[
                "editor unavailable", "editor unavailable", "registry is read only",
                "registry is read only", "editor unavailable"]), patch.object(quota_alert, "relay", wraps=self.relay) as relay, patch.object(
                quota_alert, "tell_user", wraps=self.push) as push:
            for count, sent_failure in ((1, "editor unavailable"), (1, "editor unavailable"),
                                        (2, "registry is read only"), (2, "registry is read only"),
                                        (2, "registry is read only")):
                _ = quota_alert.blocked([note], a, self.now, codex_readings=readings)
                self.assertEqual(relay.call_count, count)
                self.assertEqual(push.call_count, count)
                self.assertIn(sent_failure, cast(str, push.call_args.args[1]))
                self.assertIn(sent_failure, cast(str, relay.call_args.args[1]))
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())

    def test_account_import_forms_share_module_and_identity_class(self) -> None:
        self.assertIs(sibling_agent_accounts, agent_accounts)
        self.assertIs(sibling_agent_accounts.KnownCodexAccount, agent_accounts.KnownCodexAccount)

    def test_account_identity_reads_leave_the_import_path_unchanged(self) -> None:
        a = self.account(1)
        before = list(sys.path)
        self.assertEqual(agent_accounts.codex_account(a.home), a)
        self.assertEqual(agent_accounts.codex_account(a.home), a)
        self.assertEqual(sys.path, before)


if __name__ == "__main__":
    _ = unittest.main()
