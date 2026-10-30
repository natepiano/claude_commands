"""Claude session warnings, Codex log markers and legacy notice cleanup."""
from __future__ import annotations

import sys
import tempfile
import subprocess
import os
import base64
import json
import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami import agent_accounts
from scripts.whoami import note_file
from scripts.whoami.agent_accounts import KnownCodexAccount, UnknownCodexAccount
import five_hour
from scripts.whoami.agent_accounts import CodexHomeSource, CodexRateLimits, Quota, Report
from scripts.accounts import accounts, role_map

import escalate  # five_hour puts scripts/message on the path
import quota_alert
import send
import sessions
from quota_alert import Config, Showrunner
from user_action import NoActionRequired

RESETS = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=21, minute=20, second=0, microsecond=0)
CONFIG = cast(Config, cast(object, {
    "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "hana", "always": ["natedev"],
}))
SHOWRUNNERS = [Showrunner(session="hana", socket="/hana.sock", slug="show", zone="America/Los_Angeles", doc="")]
CLAUDE_LOGIN = "claude-fixture@example.invalid"


def claude_notice_key(login: str = CLAUDE_LOGIN) -> str:
    return "five-hour-claude-" + hashlib.sha256(login.casefold().encode()).hexdigest()[:24]


def report(tool: str, used: float | None) -> Report:
    return Report(tool, email=CLAUDE_LOGIN if tool == "Claude" else None,
                  five_hour=None if used is None else Quota("5-hour", used, RESETS))


class WatchTests(unittest.TestCase):
    sent: list[tuple[str, str, str | None]] = []
    state: Path = Path()

    @override
    def setUp(self) -> None:
        self.sent = []
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.state), "AGENT_NOTES_DIR": str(self.state)})))
        _ = self.enterContext(patch.object(note_file, "AGENTS_DIR", self.state))

        def relay(recipient: str, text: str, key: str | None) -> str | None:
            self.sent.append((recipient, text, key))
            return "QUEUED: no live session" if recipient == "hana" else None

        def config() -> Config:
            return CONFIG

        def schedule() -> None:
            return None

        def showrunners() -> list[Showrunner]:
            return SHOWRUNNERS

        for module, name, stand_in in ((five_hour, "relay", relay), (five_hour, "load_config", config),
                                       (quota_alert, "registered_showrunners", showrunners),
                                       (escalate, "schedule", schedule), (escalate, "STATE", self.state / "holds"),
                                       (five_hour, "STATE", self.state / "notices")):
            _ = self.enterContext(patch.object(module, name, stand_in))

    def successful_relay(self, recipient: str, text: str, key: str | None) -> None:
        self.sent.append((recipient, text, key))

    def watch(self, reports: list[Report]) -> list[str]:
        home = self.state / ".codex"
        identity = KnownCodexAccount(home, "fixture@example.invalid", "codex1")
        now = datetime.now(timezone.utc)
        for live in reports:
            if live.tool == "Codex":
                live.email = identity.login
                live.source = CodexHomeSource(home)
        readings = quota_alert.CodexAlertPassReadings(
            accounts.CodexWorkersAccount(label="codex1", home=home), identity,
            tuple(quota_alert.CodexHomeReading(home, live, now) for live in reports if live.tool == "Codex"))
        return five_hour.watch(reports, codex_readings=readings)

    def test_claude_accounts_keep_independent_low_spells_across_passes_and_recovery(self) -> None:
        low = Report("Claude", email="low@example.invalid", five_hour=Quota("5-hour", 82, RESETS))
        high = Report("Claude", email="high@example.invalid", five_hour=Quota("5-hour", 40, RESETS))
        for label, login in (("claude3", low.email), ("claude4", high.email)):
            _ = (self.state / f"{label}.md").write_text(f"---\nlogin: {login}\n---\n", encoding="utf-8")
        with patch.object(five_hour, "relay", side_effect=self.successful_relay):
            for login in ("low@example.invalid", "LOW@example.invalid", "low@example.invalid"):
                low.email = login
                lines = self.watch([low, high])
                self.assertFalse(any("back above" in line for line in lines))
                self.assertEqual(five_hour.notice_path(claude_notice_key(login)).read_text(), "claude3")
            self.assertCountEqual([(recipient, key) for recipient, _, key in self.sent],
                                  [("natedev", "5-hour claude3"), ("hana", "5-hour claude3")])
            self.assertTrue(all(text.startswith("5-hour limit: claude3 has 18% of its 5-hour limit left;")
                                for _, text, _ in self.sent))
            self.assertFalse(five_hour.notice_path(claude_notice_key("high@example.invalid")).exists())
            low.five_hour = Quota("5-hour", 40, RESETS)
            self.assertEqual(self.watch([low, high]), ["5-hour limit: claude3 is back above 20%"])
            self.assertEqual(self.watch([low, high]), [])
            self.assertFalse(five_hour.notice_path(claude_notice_key("low@example.invalid")).exists())
            low.five_hour = Quota("5-hour", 82, RESETS)
            self.assertEqual(self.watch([low, high]),
                             ["5-hour limit: claude3 18% left -> natedev", "5-hour limit: claude3 18% left -> hana"])
            self.assertEqual(self.watch([low, high]), [])
        self.assertCountEqual([(recipient, key) for recipient, _, key in self.sent],
                              [("natedev", "5-hour claude3"), ("hana", "5-hour claude3")] * 2)
        self.assertEqual(five_hour.notice_path(claude_notice_key("low@example.invalid")).read_text(), "claude3")

    def test_claude_notice_without_a_matching_note_never_discloses_the_login(self) -> None:
        unknown = report("Claude", 82)
        with patch.object(five_hour, "relay", side_effect=self.successful_relay):
            lines = self.watch([unknown])
        self.assertEqual(lines, ["5-hour limit: Claude 18% left -> natedev", "5-hour limit: Claude 18% left -> hana"])
        self.assertTrue(all(text.startswith("5-hour limit: Claude has 18%") for _, text, _ in self.sent))
        self.assertEqual([key for _, _, key in self.sent], ["5-hour Claude", "5-hour Claude"])
        unknown.five_hour = Quota("5-hour", 40, RESETS)
        lines += self.watch([unknown])
        self.assertEqual(lines[-1], "5-hour limit: Claude is back above 20%")
        for text in [*lines, *(text for _, text, _ in self.sent), *(key or "" for _, _, key in self.sent)]:
            self.assertNotIn(CLAUDE_LOGIN, text)

    def test_claude_report_without_a_login_leaves_notice_state_untouched(self) -> None:
        with patch.object(five_hour, "relay", side_effect=self.successful_relay):
            _ = self.watch([report("Claude", 82)])
        before = {path.name: path.read_bytes() for path in five_hour.STATE.iterdir()}
        self.sent.clear()
        for used in (82, 40):
            self.assertEqual(self.watch([Report("Claude", five_hour=Quota("5-hour", used, RESETS))]), [])
        self.assertEqual(self.sent, [])
        self.assertEqual({path.name: path.read_bytes() for path in five_hour.STATE.iterdir()}, before)

    def test_a_low_window_retries_failed_relays_without_a_phone_hold(self) -> None:
        with patch.object(escalate, "hold", side_effect=AssertionError("phone hold")):
            lines = self.watch([report("Claude", 82.4), report("Codex", 10)])
        self.assertEqual(lines, ["5-hour limit: Claude 18% left -> natedev",
                                 "5-hour limit: Claude 18% left -> hana: NOT sent: QUEUED: no live session"])
        self.assertEqual([(name, key) for name, _, key in self.sent],
                         [("natedev", "5-hour Claude"), ("hana", "5-hour Claude")])
        self.assertEqual(self.sent[0][1],
                         "5-hour limit: Claude has 18% of its 5-hour limit left; it resets at 14:20 PDT.\n"
                         + "Tell the user in one line and change nothing else.\n"
                         + f"Protocol: {quota_alert.PROTOCOL}")
        self.assertIsInstance(escalate.read(escalate.held_path(claude_notice_key())), escalate.NoHeld)
        self.assertFalse(five_hour.notice_path(claude_notice_key()).exists())
        with patch.object(five_hour, "relay", side_effect=self.successful_relay):
            retried = self.watch([report("Claude", 90)])
        self.assertEqual(retried, ["5-hour limit: Claude 10% left -> hana"])
        self.assertEqual([name for name, _, _ in self.sent], ["natedev", "hana", "hana"])
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(five_hour.notice_path(claude_notice_key()).read_text(), "Claude")
        self.assertEqual(self.watch([report("Claude", 90)]), [])
        self.assertEqual(len(self.sent), 3)

    def test_recovery_during_relay_rearms_the_next_low_spell(self) -> None:
        recoveries: list[list[str]] = []

        def relay(recipient: str, text: str, key: str | None) -> None:
            self.successful_relay(recipient, text, key)
            recoveries.append(self.watch([report("Claude", 3)]))

        with (patch.object(five_hour, "recipients", return_value=["natedev"]),
              patch.object(five_hour, "relay", side_effect=relay)):
            self.assertEqual(self.watch([report("Claude", 90)]),
                             ["5-hour limit: Claude 10% left -> natedev"])
        self.assertEqual(len(recoveries), 1)
        self.assertFalse(five_hour.notice_path(claude_notice_key()).exists())
        with (patch.object(five_hour, "recipients", return_value=["natedev"]),
              patch.object(five_hour, "relay", side_effect=self.successful_relay)):
            self.assertEqual(self.watch([report("Claude", 90)]),
                             ["5-hour limit: Claude 10% left -> natedev"])
            self.assertEqual(self.watch([report("Claude", 90)]), [])
        self.assertEqual([name for name, _, _ in self.sent], ["natedev", "natedev"])
        self.assertEqual(five_hour.notice_path(claude_notice_key()).read_text(), "Claude")

    def test_repeated_failed_relay_retries_only_the_recipient_still_owed(self) -> None:
        for _ in range(3):
            _ = self.watch([report("Claude", 90)])
        self.assertEqual([name for name, _, _ in self.sent], ["natedev", "hana", "hana", "hana"])
        self.assertFalse(five_hour.notice_path(claude_notice_key()).exists())
        with patch.object(five_hour, "relay", side_effect=self.successful_relay):
            self.assertEqual(self.watch([report("Claude", 90)]), ["5-hour limit: Claude 10% left -> hana"])
        self.assertEqual([name for name, _, _ in self.sent], ["natedev", "hana", "hana", "hana", "hana"])
        self.assertEqual(self.watch([report("Claude", 90)]), [])
        self.assertEqual(five_hour.notice_path(claude_notice_key()).read_text(), "Claude")

    def test_partial_delivery_survives_a_stopped_pass_without_a_second_copy(self) -> None:
        class Killed(BaseException):
            pass

        def relay(recipient: str, text: str, key: str | None) -> None:
            if recipient == "hana":
                raise Killed()
            self.successful_relay(recipient, text, key)

        with patch.object(five_hour, "relay", side_effect=relay):
            with self.assertRaises(Killed):
                _ = self.watch([report("Claude", 90)])
        self.assertEqual([name for name, _, _ in self.sent], ["natedev"])
        self.assertFalse(five_hour.notice_path(claude_notice_key()).exists())
        with patch.object(five_hour, "relay", side_effect=self.successful_relay):
            self.assertEqual(self.watch([report("Claude", 90)]), ["5-hour limit: Claude 10% left -> hana"])
        self.assertEqual([name for name, _, _ in self.sent], ["natedev", "hana"])
        self.assertEqual(five_hour.notice_path(claude_notice_key()).read_text(), "Claude")

    def test_recovery_clears_partial_delivery_for_the_next_low_spell(self) -> None:
        _ = self.watch([report("Claude", 90)])
        self.assertEqual(self.watch([report("Claude", 3)]), ["5-hour limit: Claude is back above 20%"])
        with patch.object(five_hour, "relay", side_effect=self.successful_relay):
            self.assertEqual(self.watch([report("Claude", 90)]),
                             ["5-hour limit: Claude 10% left -> natedev", "5-hour limit: Claude 10% left -> hana"])
        self.assertEqual([name for name, _, _ in self.sent], ["natedev", "hana", "natedev", "hana"])
        self.assertEqual(five_hour.notice_path(claude_notice_key()).read_text(), "Claude")

    def test_interrupted_recovery_ends_the_previous_spell_after_a_stopped_finish(self) -> None:
        class Killed(BaseException):
            pass

        unlink = Path.unlink
        for stop_before in ("notice", "delivery"):
            with (self.subTest(stop_before=stop_before),
                  patch.object(five_hour, "STATE", self.state / stop_before),
                  patch.object(five_hour, "relay", side_effect=self.successful_relay)):
                self.sent.clear()
                marker = five_hour.notice_path(claude_notice_key())
                pending = five_hour.delivery_path(claude_notice_key())

                def stop_after_sent_marker(path: Path, missing_ok: bool = False) -> None:
                    if path == pending:
                        raise Killed()
                    unlink(path, missing_ok=missing_ok)

                with patch.object(Path, "unlink", new=stop_after_sent_marker):
                    with self.assertRaises(Killed):
                        _ = self.watch([report("Claude", 90)])
                self.assertEqual(marker.read_text(), "Claude")
                self.assertTrue(pending.exists())
                self.assertCountEqual([name for name, _, _ in self.sent], ["natedev", "hana"])
                self.assertEqual(self.watch([report("Claude", 90)]), [])

                def stop_recovery_cleanup(path: Path, missing_ok: bool = False) -> None:
                    if path == (marker if stop_before == "notice" else pending):
                        raise Killed()
                    unlink(path, missing_ok=missing_ok)

                with patch.object(Path, "unlink", new=stop_recovery_cleanup):
                    with self.assertRaises(Killed):
                        _ = self.watch([report("Claude", 3)])
                self.sent.clear()
                self.assertEqual(self.watch([report("Claude", 90)]),
                                 ["5-hour limit: Claude 10% left -> natedev", "5-hour limit: Claude 10% left -> hana"])
                self.assertEqual(self.watch([report("Claude", 90)]), [])
                self.assertCountEqual([name for name, _, _ in self.sent], ["natedev", "hana"])
                self.assertEqual(marker.read_text(), "Claude")
                self.assertFalse(pending.exists())

    def test_stopped_claim_is_retried_and_then_marked_sent(self) -> None:
        class Killed(BaseException):
            pass

        with patch.object(five_hour, "ThreadPoolExecutor", side_effect=Killed):
            with self.assertRaises(Killed):
                _ = self.watch([report("Claude", 90)])
        self.assertEqual(self.sent, [])
        self.assertFalse(five_hour.notice_path(claude_notice_key()).exists())
        with patch.object(five_hour, "relay", side_effect=self.successful_relay):
            self.assertEqual(len(self.watch([report("Claude", 90)])), 2)
            self.assertEqual(self.watch([report("Claude", 90)]), [])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(five_hour.notice_path(claude_notice_key()).read_text(), "Claude")

    def test_an_active_claim_excludes_a_concurrent_pass(self) -> None:
        concurrent: list[list[str]] = []

        def relay(recipient: str, text: str, key: str | None) -> None:
            self.sent.append((recipient, text, key))
            concurrent.append(self.watch([report("Claude", 90)]))

        with (patch.object(five_hour, "recipients", return_value=["natedev"]),
              patch.object(five_hour, "relay", side_effect=relay)):
            self.assertEqual(self.watch([report("Claude", 90)]),
                             ["5-hour limit: Claude 10% left -> natedev"])
        self.assertEqual(concurrent, [[]])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(five_hour.notice_path(claude_notice_key()).read_text(), "Claude")

    def test_codex_low_spell_logs_once_without_relay_then_rearms_after_recovery(self) -> None:
        first = self.watch([report("Codex", 95)])
        self.assertEqual(len(first), 1)
        self.assertIn("5-hour limit:", first[0])
        self.assertIn("codex1", first[0])
        self.assertIn("5%", first[0])
        markers = list(five_hour.STATE.glob("five-hour-codex-*.notice"))
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].read_text(), "codex1")
        self.assertEqual(self.watch([report("Codex", 99)]), [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.watch([report("Codex", 3)]), ["5-hour limit: codex1 is back above 20%"])
        self.assertFalse(markers[0].exists())
        self.assertEqual(self.watch([report("Codex", 3)]), [])
        self.assertEqual(len(self.watch([report("Codex", 80)])), 1)
        self.assertEqual(self.sent, [])

    def test_old_phone_hold_closes_once_on_recovery(self) -> None:
        _ = escalate.hold(claude_notice_key(), escalate.SessionPush("Claude", "Usage low", NoActionRequired(), "note"))
        self.assertEqual(self.watch([report("Claude", 3)]), ["5-hour limit: Claude is back above 20%"])
        self.assertEqual(self.watch([report("Claude", 3)]), [])
        self.assertIsInstance(escalate.read(escalate.held_path(claude_notice_key())), escalate.NoHeld)
        self.assertEqual(self.sent, [])

    def test_recovery_clears_the_warning_record_and_the_next_low_spell_is_new(self) -> None:
        _ = self.watch([report("Codex", 95)])
        self.assertEqual(self.watch([report("Codex", 3)]), ["5-hour limit: codex1 is back above 20%"])
        self.assertIsInstance(escalate.read(escalate.held_path("five-hour-codex")), escalate.NoHeld)
        self.assertEqual(self.watch([report("Codex", 3)]), [])
        self.assertEqual(len(self.watch([report("Codex", 80)])), 1)
        self.assertEqual(self.sent, [])

    def test_no_reading_changes_nothing(self) -> None:
        with patch.object(five_hour, "relay", return_value=None):
            _ = self.watch([report("Claude", 99)])
        self.assertEqual(self.watch([report("Claude", None), Report("Codex", problem="Not logged in")]), [])
        self.assertTrue(five_hour.notice_path(claude_notice_key()).exists())


class OutgoingNoticeTests(unittest.TestCase):
    marker: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(five_hour, "STATE", root))
        self.marker = root / "five-hour-codex-outgoing.notice"
        _ = self.marker.write_text("codex1", encoding="utf-8")

    def test_failed_retire_keeps_the_notice_until_all_recipients_retire(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "retire failed")
        succeeded = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(subprocess, "run", side_effect=[failed, succeeded]) as retire:
            five_hour.cancel_outgoing_notices("five-hour-codex-current", ["first", "second"])
        self.assertEqual(retire.call_count, 2)
        self.assertEqual(self.marker.read_text(), "codex1")
        with patch.object(subprocess, "run", return_value=succeeded) as retire:
            five_hour.cancel_outgoing_notices("five-hour-codex-current", ["first", "second"])
        self.assertEqual([call.args[0][4] for call in retire.call_args_list], ["first", "second"])
        self.assertFalse(self.marker.exists())

class AccountHoldTests(unittest.TestCase):
    root: Path = Path()
    sent: list[str] = []
    now: datetime = datetime.now(timezone.utc)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sent = []
        self.now = datetime.now(timezone.utc)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root)})))
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        _ = self.enterContext(patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unexpected ssh")))
        _ = self.enterContext(patch.object(escalate, "STATE", self.root / "holds"))
        _ = self.enterContext(patch.object(five_hour, "STATE", self.root / "notices"))
        _ = self.enterContext(patch.object(escalate, "schedule"))
        _ = self.enterContext(patch.object(escalate, "load_settings", return_value=CONFIG))
        _ = self.enterContext(patch.object(escalate, "registered_showrunners", return_value=SHOWRUNNERS))
        _ = self.enterContext(patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")))
        _ = self.enterContext(patch.object(five_hour, "load_config", return_value=CONFIG))
        _ = self.enterContext(patch.object(quota_alert, "registered_showrunners", return_value=SHOWRUNNERS))
        _ = self.enterContext(patch.object(five_hour, "relay", side_effect=self.relay))

    def relay(self, recipient: str, text: str, key: str | None) -> None:
        del recipient, key
        self.sent.append(text)

    def readings(self, number: int, used: float | None = 95) -> quota_alert.CodexAlertPassReadings:
        home = self.root / f".codex{number}"
        home.mkdir(exist_ok=True)
        login = f"a{number}@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"x.{claims}.y"}}))
        _ = (self.root / f"codex{number}.md").write_text(f"---\nlogin: {login}\n---\n")
        self.assertEqual(accounts.main(["set-role", "workers", f"codex{number}"]), 0)
        identity = KnownCodexAccount(home, f"a{number}@example.invalid", f"codex{number}")
        live = Report("Codex", email=identity.login, source=CodexHomeSource(home),
            five_hour=None if used is None else Quota("5-hour", used, self.now + timedelta(hours=2)))
        return quota_alert.CodexAlertPassReadings(accounts.role_account("workers"), identity,
            (quota_alert.CodexHomeReading(home, live, self.now),))

    def test_swap_cancels_outgoing_and_legacy_holds_without_recovery_and_keeps_other_tools(self) -> None:
        a = self.readings(1)
        _ = five_hour.watch([a.checked_homes[0].report], codex_readings=a)
        _ = escalate.hold("five-hour-codex", escalate.SessionPush("Old Codex", "old", NoActionRequired(), "note"))
        _ = escalate.hold("five-hour-claude", escalate.SessionPush("Claude", "other", NoActionRequired(), "note"))
        self.sent.clear()
        b = self.readings(2, None)
        _ = five_hour.watch([b.checked_homes[0].report], codex_readings=b)
        self.assertEqual(self.sent, [])
        held = [escalate.read(path) for path in (escalate.STATE / "held").glob("*.json")]
        current = [entry for entry in held if isinstance(entry, escalate.HeldMessage)]
        self.assertEqual([entry.summary for entry in current], ["Claude"])
        low_b = self.readings(2)
        lines = five_hour.watch([low_b.checked_homes[0].report], codex_readings=low_b)
        self.assertEqual(len(lines), 1)
        self.assertIn("codex2", lines[0])
        self.assertEqual(self.sent, [])

    def test_known_swap_retires_an_offline_relay_even_without_a_usage_reading(self) -> None:
        with (patch.object(send, "STATE", self.root / "messages"),
              patch.object(sessions, "live_sessions", return_value=[])):
            a = self.readings(1)
            _ = five_hour.watch([a.checked_homes[0].report], codex_readings=a)
            # This queued relay predates log-only Codex notices; new watches never enqueue it.
            send.enqueue(send.Message("hana", "quota_alert", "", "old Codex warning", "5-hour codex1"),
                         "offline", self.now)
            self.assertTrue(send.read_queue(send.queue_path("hana")))
            chosen = self.readings(2, None)
            b = quota_alert.CodexAlertPassReadings(chosen.workers, chosen.selected_account, ())
            def retire(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                _ = send.retire(command[command.index("--to") + 1], "quota_alert", command[command.index("--key") + 1])
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch.object(subprocess, "run", side_effect=retire):
                self.assertEqual(five_hour.watch([], codex_readings=b), [])
            self.assertEqual(send.read_queue(send.queue_path("hana")), [])

    def test_unknown_selected_identity_preserves_warning_records_and_sends_nothing(self) -> None:
        a = self.readings(1)
        _ = five_hour.watch([a.checked_homes[0].report], codex_readings=a)
        before = {path.name: path.read_text() for path in five_hour.STATE.glob("*.notice")}
        self.sent.clear()
        b = self.readings(2, None)
        assert isinstance(b.workers, accounts.CodexWorkersAccount)
        unknown = quota_alert.CodexAlertPassReadings(b.workers,
            UnknownCodexAccount(b.workers.home, "unreadable login"), b.checked_homes)
        _ = five_hour.watch([], codex_readings=unknown)
        self.assertEqual(self.sent, [])
        self.assertTrue(before)
        self.assertEqual({path.name: path.read_text() for path in five_hour.STATE.glob("*.notice")}, before)

    def test_stop_after_failed_retirement_retries_outgoing_notice_on_rerun(self) -> None:
        class Killed(BaseException):
            pass

        a = self.readings(1)
        _ = five_hour.watch([], codex_readings=a)
        markers = list(five_hour.STATE.glob("five-hour-codex-*.notice"))
        self.assertEqual(len(markers), 1)
        b = self.readings(2, None)
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "failed")),
              patch.object(escalate, "close", side_effect=Killed)):
            with self.assertRaises(Killed):
                _ = five_hour.watch([], codex_readings=b)
        self.assertEqual(markers[0].read_text(), "codex1")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as retire:
            self.assertEqual(five_hour.watch([], codex_readings=b), [])
        self.assertEqual(retire.call_count, 2)
        self.assertTrue(all(call.args[0][-1] == "5-hour codex1" for call in retire.call_args_list))
        self.assertFalse(markers[0].exists())

    def test_spare_used_up_window_is_not_relayed_or_held(self) -> None:
        a = self.readings(1, 10)
        spare = Report("Codex", email="spare@example.invalid", source=CodexHomeSource(self.root / ".codex2"),
            five_hour=Quota("5-hour", 100, self.now + timedelta(hours=1)), credit_balance="0")
        readings = quota_alert.CodexAlertPassReadings(a.workers, a.selected_account,
            (*a.checked_homes, quota_alert.CodexHomeReading(spare.source.home, spare, self.now))
            if isinstance(spare.source, CodexHomeSource) else a.checked_homes)
        _ = five_hour.watch([a.checked_homes[0].report, spare], codex_readings=readings)
        self.assertEqual(self.sent, [])
        self.assertFalse(list((escalate.STATE / "held").glob("*.json")))


class ReaderTests(unittest.TestCase):
    def test_claude_five_hour_window(self) -> None:
        usage: dict[str, object] = {"five_hour": {"utilization": 41.0, "resets_at": RESETS.isoformat()},
                                    "seven_day": {"utilization": 78.0, "resets_at": None}}
        self.assertEqual(agent_accounts.claude_five_hour(usage), Quota("5-hour", 41.0, RESETS))
        self.assertIsNone(agent_accounts.claude_five_hour({"seven_day": {"utilization": 78.0}}))

    def test_codex_five_hour_window(self) -> None:
        week = {"usedPercent": 60.0, "windowDurationMins": 10080, "resetsAt": None}
        usage = cast(CodexRateLimits, cast(object, {"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 12.0, "windowDurationMins": 300, "resetsAt": int(RESETS.timestamp())},
                      "secondary": week},
            "other": {"primary": {"usedPercent": 99.0, "windowDurationMins": 300, "resetsAt": None}}}}))
        self.assertEqual(agent_accounts.codex_five_hour(usage), Quota("5-hour", 12.0, RESETS))
        self.assertIsNone(agent_accounts.codex_five_hour(cast(CodexRateLimits, cast(object, {
            "rateLimits": {"primary": week, "secondary": None}}))))


if __name__ == "__main__":
    _ = unittest.main()
