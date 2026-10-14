"""Account ownership, live swap advice and fallback recovery across seats swaps."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import note_file
import quota_alert
import agent_accounts as sibling_agent_accounts
from scripts.whoami import agent_accounts
from agent_accounts import CodexHomeSource, Quota, Report
from scripts.accounts import accounts, moves, refusals
from scripts.production import showrunners
import usage_store
from push_texts import RenderedPush
from user_action import ActionRequired, NoActionRequired
from test_quota_alert import REGISTRY

CURRENT_READING_AGE = timedelta()


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
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))
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
        _ = self.enterContext(patch.object(accounts, "_retire_seats_servers", return_value=[]))
        _ = self.enterContext(patch.object(accounts, "_codex_live", side_effect=self.live_for_swap))

    def live_for_swap(self, home: Path) -> Report:
        identity = agent_accounts.codex_account(home)
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        return self.report(identity, 80)

    def relay(self, recipient: str, text: str, key: str | None) -> None:
        del recipient, key
        self.sent.append(text)

    def push(self, push: RenderedPush) -> quota_alert.PushDelivery:
        self.sent.append(push.body)
        return quota_alert.PushSent()

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
        path = self.root / f"codex{number}.md"
        _ = path.write_text(f"---\nlogin: account{number}@example.invalid\nrole: {'worker' if state == 'active' else '[]'}\nsessions: 0\n"
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
        label = selected.label
        self.assertEqual(accounts.main(["set-role", "seats", label]), 0)
        return quota_alert.CodexAlertPassReadings(
            accounts.role_account("seats"), selected,
            tuple(quota_alert.CodexHomeReading(report.source.home, report, self.now)
                  for report in reports if isinstance(report.source, CodexHomeSource)),
        )

    def save_room(self, identity: agent_accounts.KnownCodexAccount, left: float, *,
                  age: timedelta = CURRENT_READING_AGE, reset: datetime | None = None) -> None:
        saved = usage_store.load()
        saved[usage_store.key("Codex", identity.login)] = usage_store.StoredUsage(
            weekly=usage_store.StoredWindow(left=left, at=(self.now - age).isoformat(),
                resets=(reset or self.now + timedelta(days=2)).isoformat()),
            five_hour=usage_store.StoredWindow(left=50, at=(self.now - age).isoformat(),
                resets=(self.now + timedelta(hours=3)).isoformat()),
            credits=usage_store.StoredCredits(balance="0", at=self.now.isoformat()))
        path = usage_store.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(saved))

    def test_outgoing_refusal_cannot_move_seats_after_selection_changes_at_transition_lock(self) -> None:
        old, current, target = self.account(1), self.account(2), self.account(3)
        lock = quota_alert.state_file
        for action in ("move", "wait", "fallback"):
            with self.subTest(action=action):
                room = 0 if action == "fallback" else 1
                self.save_room(old, 0)
                self.save_room(current, room)
                self.save_room(target, room)
                report = self.report(old, 0)
                window = refusals.KnownWindow("weekly")
                if action == "wait":
                    report.five_hour = Quota("5-hour", 100, self.now + timedelta(minutes=19))
                    window = refusals.KnownWindow("five_hour")
                readings = self.readings(old, report, self.report(current, room), self.report(target, room))
                changed = False
                @contextmanager
                def change_before_lock() -> Generator[quota_alert.State]:
                    nonlocal changed
                    if not changed:
                        changed = True
                        accounts.set_role("seats", current.label)
                    with lock() as state:
                        yield state
                with (patch.object(quota_alert, "state_file", side_effect=change_before_lock),
                      patch.object(accounts, "swap", wraps=accounts.swap) as swap,
                      patch.object(quota_alert, "switch_to_claude") as fallback):
                    _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
                        codex_readings=readings, window=window)
                swap.assert_not_called()
                fallback.assert_not_called()
                self.assertEqual(agent_accounts.seats_home(), current.home)
                self.assertEqual(moves.read_moves(), [])
                self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())

    def test_usage_store_write_failure_does_not_prevent_low_quota_alert(self) -> None:
        old, target = self.account(1), self.account(2)
        readings = self.readings(old, self.report(old, 1), self.report(target))
        errors = io.StringIO()
        with patch.object(usage_store, "write", side_effect=PermissionError("read-only store")), redirect_stderr(errors):
            _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertTrue(any(text.startswith("Quota alert:") for text in self.sent), self.sent)
        self.assertIn("read-only store", errors.getvalue())

    def test_swap_advice_does_not_write_usage_store(self) -> None:
        old, target = self.account(1), self.account(2)
        readings = self.readings(old, self.report(old), self.report(target))
        self.save_room(old, 1)
        self.save_room(target, 1)
        with patch.object(usage_store, "write") as write:
            self.assertEqual(quota_alert.codex_swap_line(readings, self.now), "workers move to codex 2 when Codex refuses work")
        write.assert_not_called()

    def test_each_alert_refresh_and_blocked_pass_saves_usage_once(self) -> None:
        old, target = self.account(1), self.account(2)
        readings = self.readings(old, self.report(old), self.report(target))
        notes = [self.note(1, "active", 1)]
        with patch.object(usage_store, "write", wraps=usage_store.write) as write:
            _ = quota_alert.alert(notes, self.now, codex_readings=readings)
            self.assertEqual(write.call_count, 1)
        with patch.object(usage_store, "write", wraps=usage_store.write) as write:
            _ = quota_alert.refresh(notes, None, self.now, codex_readings=readings)
            self.assertEqual(write.call_count, 1)
        with patch.object(usage_store, "write", wraps=usage_store.write) as write:
            _ = quota_alert.blocked(notes, old, self.now, codex_readings=readings)
            self.assertEqual(write.call_count, 1)

    def test_unknown_refusal_ignores_environment_window_and_time(self) -> None:
        old, target = self.account(1), self.account(2)
        report = self.report(old, 80, five_left=0)
        report.five_hour = Quota("5-hour", 100, self.now + timedelta(minutes=19))
        readings = self.readings(old, report, self.report(target))
        with patch.dict(os.environ, {"ACCOUNT_REFUSAL_WINDOW": "five_hour",
                "ACCOUNT_REFUSAL_AT": (self.now - timedelta(seconds=1)).isoformat()}):
            _ = quota_alert.blocked([self.note(1, "active", 80)], old, self.now,
                codex_readings=readings, window=refusals.UnknownWindow())
        refusal = refusals.load()[refusals.key("codex", old.login)]
        self.assertEqual(refusal.window, refusals.UnknownWindow())
        self.assertEqual(refusal.at, self.now)
        self.assertEqual(agent_accounts.seats_home(), target.home)
        request = moves.read_moves()[0]
        self.assertEqual(request["kind"], "requested")
        self.assertNotIn("reset", request)

    def test_post_refusal_window_inference_keeps_subsecond_observation_time(self) -> None:
        old, target = self.account(1), self.account(2)
        observed_at = self.now + timedelta(microseconds=2)
        report = self.report(old, 80, five_left=0)
        report.five_hour = Quota("5-hour", 100, self.now + timedelta(minutes=19))
        initial = self.readings(old, report, self.report(target))
        readings = quota_alert.CodexAlertPassReadings(initial.seats, old, tuple(
            quota_alert.CodexHomeReading(item.home, item.report, observed_at) for item in initial.checked_homes))
        _ = quota_alert.blocked([self.note(1, "active", 80)], old, observed_at,
            codex_readings=readings, refused_at=quota_alert.RefusedAt(self.now + timedelta(microseconds=1)))
        self.assertEqual(refusals.load()[refusals.key("codex", old.login)].window, refusals.KnownWindow("five_hour"))
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited"])

    def test_failed_account_swap_opens_claude_fallback_and_keeps_failed_request(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 0), self.report(target))
        with patch.object(accounts, "swap", side_effect=accounts.AccountsError("destination unavailable")):
            _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now, codex_readings=readings)
        self.assertEqual(agent_accounts.seats_home(), old.home)
        self.assertIn("delegate=claude", quota_alert.REGISTRY.read_text())
        self.assertTrue(any("workers could not move" in text and "destination unavailable" in text for text in self.sent))
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        self.assertEqual(entries[0]["move_id"], entries[1]["move_id"])

    def test_weekly_refusal_moves_seats_to_picked_home_and_records_request_not_confirmation(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 0), self.report(target, 1))
        with (patch.object(quota_alert, "switch_to_claude") as fallback,
              patch.object(accounts, "swap", wraps=accounts.swap) as swap):
            _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
                codex_readings=readings, window=refusals.KnownWindow("weekly"))
        fallback.assert_not_called()
        swap.assert_called_once()
        self.assertEqual(swap.call_args.args[:2], ("seats", "codex 2"))
        self.assertEqual(agent_accounts.seats_home(), target.home)
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        self.assertEqual(sum(text.startswith("workers moved from codex 1 to codex 2:")
                             for text in self.sent), 1)
        held = refusals.load()[refusals.key("codex", old.login)]
        self.assertEqual(held.window, refusals.KnownWindow("weekly"))
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested"])
        self.assertEqual(entries[0]["subject"], "seats")
        self.assertTrue(entries[0]["move_id"])
        requested = entries[0]
        assert requested["kind"] == "requested"
        self.assertEqual((requested["from"], requested["to"], requested["window"]),
                         ("codex 1", "codex 2", "weekly"))
        assert "reset" in requested
        self.assertEqual(requested["reset"], (self.now + timedelta(days=2)).isoformat())

    def test_stale_one_percent_weekly_with_unknown_refusal_moves_instead_of_waiting(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 1, age=timedelta(minutes=11))
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 1), self.report(target, 1))
        readings = quota_alert.CodexAlertPassReadings(readings.seats, old, tuple(
            quota_alert.CodexHomeReading(item.home, item.report,
                self.now - timedelta(minutes=11) if item.home == old.home else self.now)
            for item in readings.checked_homes))
        _ = quota_alert.blocked([self.note(1, "active", 1)], old, self.now,
            codex_readings=readings, window=refusals.UnknownWindow())
        self.assertEqual(agent_accounts.seats_home(), target.home)
        self.assertEqual(refusals.load()[refusals.key("codex", old.login)].window,
                         refusals.UnknownWindow())
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["requested"])

    def test_five_hour_reset_nineteen_minutes_away_waits_once_then_logs_wait_ended(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 80)
        self.save_room(target, 1)
        report = self.report(old, 80, five_left=0)
        reset = self.now + timedelta(minutes=19)
        report.five_hour = Quota("5-hour", 100, reset)
        readings = self.readings(old, report, self.report(target, 1))
        for _attempt in range(2):
            _ = quota_alert.blocked([self.note(1, "active", 80)], old, self.now,
                codex_readings=readings, window=refusals.KnownWindow("five_hour"))
        self.assertEqual(agent_accounts.seats_home(), old.home)
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        self.assertEqual(sum(text.startswith("workers wait for codex 1's 5-hour reset at ")
                             for text in self.sent), 1)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited"])
        renewed = self.report(old, 80)
        later_readings = quota_alert.CodexAlertPassReadings(readings.seats, old,
            (quota_alert.CodexHomeReading(old.home, renewed, reset),))
        _ = quota_alert.alert([self.note(1, "active", 80)], reset, codex_readings=later_readings)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited", "wait ended"])
        self.assertEqual(entries[0]["move_id"], entries[1]["move_id"])

    def test_exactly_twenty_minutes_and_past_five_hour_resets_move(self) -> None:
        old, target = self.account(1), self.account(2)
        for offset in (timedelta(minutes=20), timedelta(seconds=-1)):
            with self.subTest(offset=offset):
                self.sent.clear()
                quota_alert.STATE.unlink(missing_ok=True)
                self.save_room(old, 80)
                self.save_room(target, 1)
                report = self.report(old, 80, five_left=0)
                report.five_hour = Quota("5-hour", 100, self.now + offset)
                readings = self.readings(old, report, self.report(target, 1))
                _ = quota_alert.blocked([self.note(1, "active", 80)], old, self.now,
                    codex_readings=readings, window=refusals.KnownWindow("five_hour"))
                self.assertEqual(agent_accounts.seats_home(), target.home)
                self.assertFalse(any(text.startswith("workers wait") for text in self.sent))

    def test_unknown_five_hour_reset_moves_instead_of_waiting(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 80)
        self.save_room(target, 1)
        report = self.report(old, 80, five_left=0)
        report.five_hour = Quota("5-hour", 100, None)
        readings = self.readings(old, report, self.report(target, 1))
        _ = quota_alert.blocked([self.note(1, "active", 80)], old, self.now,
            codex_readings=readings, window=refusals.KnownWindow("five_hour"))
        self.assertEqual(agent_accounts.seats_home(), target.home)
        self.assertIsInstance(refusals.load()[refusals.key("codex", old.login)].reset, refusals.NoResetKnown)

    def test_unknown_refusal_infers_only_one_zero_window_observed_after_refusal(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(target, 1)
        report = self.report(old, 80, five_left=0)
        reset = self.now + timedelta(minutes=19)
        report.five_hour = Quota("5-hour", 100, reset)
        readings = self.readings(old, report, self.report(target, 1))
        _ = quota_alert.blocked([self.note(1, "active", 80)], old, self.now,
            codex_readings=readings, window=refusals.UnknownWindow(), refused_at=quota_alert.RefusedAt(self.now - timedelta(seconds=1)))
        held = refusals.load()[refusals.key("codex", old.login)]
        self.assertEqual(held.window, refusals.KnownWindow("five_hour"))
        self.assertEqual(held.reset, refusals.KnownReset(reset))
        self.assertEqual(agent_accounts.seats_home(), old.home)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited"])

    def test_unknown_refusal_never_infers_a_window_from_pre_refusal_or_ambiguous_readings(self) -> None:
        old, target = self.account(1), self.account(2)
        for weekly, five, observed_at in ((80, 0, self.now - timedelta(seconds=1)),
                                         (0, 0, self.now + timedelta(seconds=1))):
            with self.subTest(weekly=weekly, observed_at=observed_at):
                self.save_room(target, 1)
                reports = self.readings(old, self.report(old, weekly, five_left=five), self.report(target, 1))
                readings = quota_alert.CodexAlertPassReadings(reports.seats, old, tuple(
                    quota_alert.CodexHomeReading(item.home, item.report,
                        observed_at if item.home == old.home else self.now)
                    for item in reports.checked_homes))
                now = max(self.now, observed_at)
                _ = quota_alert.blocked([self.note(1, "active", weekly)], old, now,
                    codex_readings=readings, window=refusals.UnknownWindow(), refused_at=quota_alert.RefusedAt(self.now))
                self.assertEqual(refusals.load()[refusals.key("codex", old.login)].window,
                                 refusals.UnknownWindow())
                self.assertEqual(agent_accounts.seats_home(), target.home)

    def test_no_room_falls_back_and_positive_allowance_cannot_restore_unresolved_refusal(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 0)
        readings = self.readings(old, self.report(old, 0), self.report(target, 0))
        _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
            codex_readings=readings, window=refusals.KnownWindow("weekly"))
        self.assertIn("delegate=claude", quota_alert.REGISTRY.read_text())
        self.assertTrue(any("earliest reset" in text for text in self.sent), self.sent)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["no room"])
        later = self.now + timedelta(minutes=1)
        same_reset = quota_alert.CodexAlertPassReadings(readings.seats, old,
            (quota_alert.CodexHomeReading(old.home, self.report(old, 80), later),))
        _ = quota_alert.alert([self.note(1, "active", 80)], later, codex_readings=same_reset)
        self.assertIn("delegate=claude", quota_alert.REGISTRY.read_text())
        renewed = self.report(old, 80)
        renewed.quotas = [Quota("Weekly", 20, self.now + timedelta(days=9))]
        recovered = quota_alert.CodexAlertPassReadings(readings.seats, old,
            (quota_alert.CodexHomeReading(old.home, renewed, later + timedelta(seconds=1)),))
        _ = quota_alert.alert([self.note(1, "active", 80)], later + timedelta(seconds=1), codex_readings=recovered)
        self.assertTrue(refusals.resolved(refusals.load()[refusals.key("codex", old.login)],
            refusals.stored_reading("codex", old.login, later + timedelta(seconds=1)), later + timedelta(seconds=1)))
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["no room", "room again"])

    def test_low_reading_never_moves_seats_or_writes_a_move_transition(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 0), self.report(target, 1))
        with patch.object(accounts, "swap", wraps=accounts.swap) as swap:
            _ = quota_alert.alert([self.note(1, "active", 0)], self.now, codex_readings=readings)
        swap.assert_not_called()
        self.assertEqual(agent_accounts.seats_home(), old.home)
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        self.assertEqual(moves.read_moves(), [])

    def test_failed_seats_swap_records_failure_with_request_id_and_never_confirms(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 0), self.report(target, 1))
        with patch.object(accounts, "_codex_live", return_value=Report("Codex", problem="target unavailable")):
            _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
                codex_readings=readings, window=refusals.KnownWindow("weekly"))
        self.assertEqual(agent_accounts.seats_home(), old.home)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        self.assertEqual(entries[0]["move_id"], entries[1]["move_id"])
        failed = entries[1]
        assert failed["kind"] == "failed"
        self.assertIn("target unavailable", failed["reason"])
        self.assertIn("delegate=claude", quota_alert.REGISTRY.read_text())

    def test_no_room_fallback_restores_on_another_account_when_it_regains_room(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 0)
        readings = self.readings(old, self.report(old, 0), self.report(target, 0))
        _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
            codex_readings=readings, window=refusals.KnownWindow("weekly"))
        self.assertIn("delegate=claude", quota_alert.REGISTRY.read_text())
        later = self.now + timedelta(minutes=1)
        recovered = quota_alert.CodexAlertPassReadings(readings.seats, old,
            (quota_alert.CodexHomeReading(old.home, self.report(old, 0), later),
             quota_alert.CodexHomeReading(target.home, self.report(target, 1), later)))
        _ = quota_alert.alert([self.note(1, "active", 0), self.note(2, "inactive", 1)], later,
                              codex_readings=recovered)
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        self.assertEqual(agent_accounts.seats_home(), target.home)
        entries = moves.read_moves()
        recovered_entries = [entry for entry in entries if entry["kind"] == "room again"]
        self.assertEqual(len(recovered_entries), 1)
        self.assertEqual(recovered_entries[0]["label"], "codex 2")
        self.assertEqual(entries[0]["move_id"], recovered_entries[0]["move_id"])

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

    def test_spare_recommendation_uses_the_home_login_account_number_and_live_credits(self) -> None:
        a, b = self.account(1), self.account(2, default=True)
        readings = self.readings(a, self.report(a), self.report(b, 1, "17", five_left=0))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 1)],
                              self.now, codex_readings=readings)
        text = "\n".join(self.sent)
        self.assertIn("workers move to", text)
        self.assertNotIn("swap seats", text)
        self.assertIn("codex 2", text)
        self.assertEqual(agent_accounts.seats_home(), a.home)

    def test_each_new_codex_catalog_example_names_workers_and_numbered_accounts(self) -> None:
        from push_texts import catalog, render_entry
        ids = {"quota.codex_on_credits", "quota.codex_on_credits_last", "quota.codex_workers_moved",
               "quota.codex_workers_stay", "quota.codex_credits_forecast"}
        entries = [entry for entry in catalog() if entry.id.startswith("quota.codex_") and
                   (entry.id in ids or "forecast" in entry.id or "credits" in entry.id)]
        self.assertTrue(ids.issubset({entry.id for entry in entries}))
        for entry in entries:
            self.assertEqual((entry.sender, entry.need), ("quota_alert", "note"))
            self.assertGreaterEqual(len(entry.examples), 2)
            for example in entry.examples:
                with self.subTest(id=entry.id, example=example.name):
                    rendered = render_entry(entry, example.values)
                    text = rendered.title + "\n" + rendered.body
                    self.assertNotIn("default", text.lower())
                    self.assertNotIn("seats", text.lower())
                    self.assertLess(len(rendered.title), 45)
                    if entry.id in ("quota.codex_on_credits", "quota.codex_on_credits_last"):
                        self.assertIsInstance(rendered.action, ActionRequired)
                        assert isinstance(rendered.action, ActionRequired)
                        self.assertIn("use_credits: off", rendered.action.text)
                        self.assertIn(example.values["file"], rendered.action.text)
                        self.assertIn(example.values.get("machine", "natedev"), rendered.action.text)
                        if entry.id == "quota.codex_on_credits":
                            self.assertIn("paid credits", text)
                            self.assertRegex(rendered.body, r"(?i)\b(?:within|in) (?:(?:a few|two|2) )?minutes\b")
                            on = next((line for line in rendered.body.splitlines() if line.lower().startswith("leave on")), "")
                            self.assertIn("spend", on.lower())
                            self.assertIn("credits", on.lower())
                        else:
                            off = next((line for line in rendered.body.splitlines() if line.lower().startswith("turn off")), "")
                            on = next((line for line in rendered.body.splitlines() if line.lower().startswith("leave on")), "")
                            self.assertIn("Claude", off)
                            self.assertIn("until", off.lower())
                            self.assertRegex(off, r"(?i)\b(?:within|in) (?:(?:a few|two|2) )?minutes\b")
                            self.assertNotIn("if Codex refuses", off)
                            self.assertIn("credits", on.lower())
                            self.assertIn("Claude", on)
                        prefix = "Action: " + rendered.action.text + "\n"
                    else:
                        self.assertIsInstance(rendered.action, NoActionRequired)
                        prefix = "No action needed.\n"
                    self.assertLess(len(prefix + rendered.body), 500)

    def test_codex_refusal_and_swap_advice_use_worker_wording_and_the_home_note_number(self) -> None:
        a, b = self.account(1), self.account(2, default=True)
        readings = self.readings(a, self.report(a, 0), self.report(b, 90))
        advice = quota_alert.codex_swap_line(readings, self.now)
        _ = quota_alert.blocked([self.note(1, "active", 0)], a, self.now,
            codex_readings=readings, window=refusals.KnownWindow("weekly"))
        text = advice + "\n" + "\n".join(self.sent)
        self.assertNotIn("default", text.lower())
        self.assertNotIn("seats", text.lower())
        self.assertIn("workers", text)
        self.assertIn("codex 2", text)

    def test_spare_with_used_up_window_and_no_credits_is_not_recommended(self) -> None:
        a, b = self.account(1), self.account(2)
        readings = self.readings(a, self.report(a), self.report(b, 80, "0", five_left=0))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 80)],
                              self.now, codex_readings=readings)
        self.assertNotIn("workers move to codex 2 when Codex refuses work", "\n".join(self.sent))

    def test_failed_or_expired_spare_read_never_uses_saved_credits_for_advice(self) -> None:
        a, b = self.account(1), self.account(2)
        for report in (self.report(b, 90, None, failed=True), self.report(b, 90, "0", expired=True)):
            with self.subTest(problem=report.problem):
                quota_alert.STATE.unlink(missing_ok=True)
                self.sent.clear()
                readings = self.readings(a, self.report(a), report)
                _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 90, "999")],
                                      self.now, codex_readings=readings)
                self.assertNotIn("workers move to codex 2 when Codex refuses work", "\n".join(self.sent))

    def test_unchecked_target_is_not_recommended_even_with_live_allowance(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = (b.home / "unexpected").write_text("private entry")
        readings = self.readings(a, self.report(a), self.report(b, 90))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 90)],
                              self.now, codex_readings=readings)
        self.assertNotIn("workers move to codex 2 when Codex refuses work", "\n".join(self.sent))

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

    def test_selected_refusal_owns_fallback_and_credits_restore_only_after_refusal_reset(self) -> None:
        a = self.account(1)
        readings = self.readings(a, self.report(a, 0))
        _ = quota_alert.blocked([self.note(1, "active", 0)], a, self.now, codex_readings=readings, window=refusals.KnownWindow("weekly"))
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.owner, a)
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "fix", "codex"))
        self.now += timedelta(days=3)
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

    def test_shared_account_identity_matches_logins_without_regard_to_case(self) -> None:
        a = self.account(1)
        path = self.root / "codex1.md"
        _ = path.write_text(path.read_text().replace(a.login, a.login.upper()))
        identity = agent_accounts.codex_account(a.home)
        self.assertIsInstance(identity, agent_accounts.KnownCodexAccount)
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        self.assertEqual((identity.home, identity.login, identity.label), (a.home, a.login, "codex 1"))

    def test_selected_and_spare_readings_share_the_ten_minute_limit(self) -> None:
        a, b = self.account(1), self.account(2)
        self.save_room(a, 1)
        self.save_room(b, 80)
        readings = self.readings(a, self.report(a), self.report(b, 80))
        at_limit = self.now + timedelta(minutes=10)
        self.assertIsNone(quota_alert.selected_codex_report(readings, at_limit).problem)
        self.assertIn("workers move to codex 2 when Codex refuses work", quota_alert.codex_swap_line(readings, at_limit))
        after_limit = at_limit + timedelta(microseconds=1)
        self.assertIsNotNone(quota_alert.selected_codex_report(readings, after_limit).problem)
        self.assertIn("no other Codex account has room until", quota_alert.codex_swap_line(readings, after_limit))

    def test_standalone_refusal_uses_readings_collected_after_the_call_starts(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = self.readings(a)
        async def live(home: Path) -> Report:
            return self.report(a if home == a.home else b, 0 if home == a.home else 80)
        with patch.object(quota_alert, "codex_live", side_effect=live), patch.object(quota_alert, "tell_user", wraps=self.push) as push:
            _ = quota_alert.blocked([self.note(1, "active", 0)], a)
        self.assertIn("workers moved from codex 1 to codex 2", "\n".join(self.sent))
        self.assertEqual(agent_accounts.seats_home(), b.home)
        push.assert_not_called()

    def test_standalone_alert_message_and_refresh_accept_their_new_readings(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = self.readings(a)
        note = self.note(1, "active", 1)
        async def live(home: Path) -> Report:
            return self.report(a if home == a.home else b, 1 if home == a.home else 80)
        with patch.object(quota_alert, "codex_live", side_effect=live):
            _ = quota_alert.alert([note])
            self.assertIn("workers move to codex 2 when Codex refuses work", "\n".join(self.sent))
            text = quota_alert.message(note, [note], quota_alert.load_config())
            self.assertIn("workers move to codex 2 when Codex refuses work", text)
            quota_alert.STATE.unlink(missing_ok=True)
            self.sent.clear()
            _ = quota_alert.refresh([note], None)
            self.assertIn("workers move to codex 2 when Codex refuses work", "\n".join(self.sent))

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

    def test_outgoing_refusal_records_a_note_without_user_or_session_delivery(self) -> None:
        """F008: an outgoing account's refusal is recorded in the quota log only."""
        a, b, c = self.account(1), self.account(2), self.account(3)
        readings = self.readings(b, self.report(a, 0), self.report(b, 0), self.report(c, 80))
        with patch.object(quota_alert, "tell_user", wraps=self.push) as push:
            lines = quota_alert.blocked([self.note(1, "inactive", 0)], a, self.now, codex_readings=readings)
        push.assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertIn("quota status codex 1: Codex refused work on codex 1, the outgoing account. Nothing switched to Claude.", lines)

    def test_failed_restoration_is_attempted_and_reported_once(self) -> None:
        a = self.account(1)
        _ = quota_alert.blocked([self.note(1, "active", 0)], a, self.now,
            codex_readings=self.readings(a, self.report(a, 0)))
        self.now += timedelta(seconds=1)
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

    def test_refusal_after_credit_recovery_relays_the_new_move_without_a_phone_push(self) -> None:
        a = self.account(1)
        note = self.note(1, "active", 0)
        low = self.readings(a, self.report(a, 0))
        _ = quota_alert.alert([note], self.now, codex_readings=low)
        _ = quota_alert.blocked([note], a, self.now, codex_readings=low, window=refusals.KnownWindow("weekly"))
        self.now += timedelta(days=3)
        funded = self.readings(a, self.report(a, 0, "19", five_left=0))
        _ = quota_alert.alert([note], self.now, codex_readings=funded)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        self.sent.clear()
        low = self.readings(a, self.report(a, 0))
        with patch.object(quota_alert, "relay", wraps=self.relay) as relay, patch.object(
                quota_alert, "tell_user", wraps=self.push) as push:
            _ = quota_alert.blocked([note], a, self.now, codex_readings=low)
            relay.assert_called_once()
            push.assert_not_called()
            self.assertIn("moved", cast(str, relay.call_args.args[1]))
            _ = quota_alert.blocked([note], a, self.now, codex_readings=low)
            relay.assert_called_once()
            push.assert_not_called()
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
            push.assert_not_called()
            self.assertIn("moved", cast(str, relay.call_args.args[1]))
            self.assertIn("workers moved", cast(str, relay.call_args.args[1]))
        self.assertIn("delegate=codex", quota_alert.REGISTRY.read_text())
        self.assertEqual(agent_accounts.seats_home(), b.home)

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
                self.assertEqual(cast(RenderedPush, push.call_args.args[0]).id, "quota.codex_refused")
                self.assertIn("the work is paused", cast(RenderedPush, push.call_args.args[0]).body)
                self.assertNotIn(sent_failure, cast(RenderedPush, push.call_args.args[0]).body)
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
