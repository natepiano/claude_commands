"""Exercise login sittings with scratch state and injected transports."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.accounts import accounts, login_sittings as sittings
from scripts.lib import conf
from scripts.maintenance.schedule import ScheduleBusy, locked_history
from scripts.notify import push_texts, review
from scripts.message import send_budget
from scripts.whoami import agent_accounts, usage_store
from scripts.whoami.quota_alert import PushFailed, PushSent


class LoginSittingsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.config: Path = Path()
        self.now: datetime = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)
        self.local: tuple[sittings.LoginEnd, ...] = ()
        self.peer: tuple[sittings.LoginEnd, ...] | sittings.PeerReadFailed = ()
        self.sent: list[tuple[push_texts.RenderedPush, sittings.PushKind]] = []
        self.peer_calls: list[tuple[str, int]] = []
        self.outcome: PushSent | PushFailed = PushSent()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "USER_ZONE": "America/New_York"})))
        self.config = self.root / "accounts.conf"
        _ = self.config.write_text(
            "login_sitting_machine=natedev\nlogin_sitting_first=2026-11-17T10:00:00-05:00\n"
            + "# whole: 1..\nlogin_sitting_interval_days=7\n"
            + "# whole: 1..\nlogin_sitting_window_days=10\n"
            + "# whole: 1..\nlogin_safety_margin_days=3\n"
            + "# whole: 1..\nlogin_peer_read_minutes=60\n"
            + "# whole: 1..\nlogin_peer_read_timeout_seconds=30\n", encoding="utf-8")

    def end(self, label: str, days: int, machine: str = "natedev") -> sittings.LoginEnd:
        return sittings.LoginEnd(machine, label, self.now + timedelta(days=days))

    def read_peer(self, machine: str, timeout: int) -> tuple[sittings.LoginEnd, ...] | sittings.PeerReadFailed:
        self.peer_calls.append((machine, timeout))
        return self.peer

    def send(self, push: push_texts.RenderedPush, kind: sittings.PushKind) -> PushSent | PushFailed:
        self.sent.append((push, kind))
        return self.outcome

    def read_local(self, _machine: str) -> tuple[sittings.LoginEnd, ...]:
        return self.local

    def run_pass(self, at: datetime, machine: str = "natedev") -> list[str]:
        return sittings.run(at, configuration=conf.read(self.config), machine=lambda: machine,
            peer_machine=lambda: "mac", read_local=self.read_local,
            read_peer=self.read_peer, send=self.send)

    def state(self) -> sittings.ReminderState:
        return sittings.load_state(self.root / ".local/state/accounts/login_sittings.json")

    def status_reading(self, *ends: sittings.LoginEnd) -> accounts.StatusReading:
        rows = tuple(accounts.StatusRow(label=end.account, tool="claude", folder=f"~/.{end.account}",
            login="fixture", roles=(), weekly_left_percent=accounts.StatusUnavailable(),
            resets_at=accounts.StatusUnavailable(), credits=accounts.StatusUnavailable(), on_credits="no",
            use_credits=False, read_at=accounts.StatusUnavailable(),
            read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=accounts.StatusUnavailable(),
            login_ends_at=agent_accounts.LoginEndKnown(end.at)) for end in ends)
        return accounts.StatusReading(rows, (), (), timezone.utc, self.now)

    def test_overlapping_pass_skips_state_peer_read_and_delivery_while_the_lock_is_busy(self) -> None:
        self.local = (self.end("claude1", 5),)
        nested: list[list[str]] = []
        read_peer = self.read_peer
        send = self.send

        def overlap(machine: str, timeout: int) -> tuple[sittings.LoginEnd, ...] | sittings.PeerReadFailed:
            if not nested:
                nested.append([])
                nested[0] = self.run_pass(self.now)
            return read_peer(machine, timeout)

        def send_claimed(push: push_texts.RenderedPush, kind: sittings.PushKind) -> PushSent | PushFailed:
            with locked_history(self.root / ".local/state/accounts/login_sittings") as held:
                self.assertIsInstance(held, ScheduleBusy)
            return send(push, kind)

        with (mock.patch.object(self, "read_peer", side_effect=overlap),
              mock.patch.object(self, "send", side_effect=send_claimed),
              mock.patch.object(sittings, "load_state", wraps=sittings.load_state) as load):
            _ = self.run_pass(self.now)
        self.assertEqual(nested, [[]])
        load.assert_called_once()
        self.assertEqual(self.peer_calls, [("mac", 30)])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.state().sitting, self.now)

    def test_saves_drop_named_dates_only_when_the_same_machine_and_account_have_a_different_date(self) -> None:
        self.local = (self.end("claude1", 5), self.end("claude2", 6), self.end("claude3", 7))
        old_peer = self.end("claude1", 4, "mac")
        self.peer = (old_peer,)
        _ = self.run_pass(self.now)
        self.assertEqual(self.state().named, {*self.local, old_peer})
        missing = self.local[1]
        retained = self.local[2]
        self.local = (self.end("claude1", 29), retained)
        self.peer = (self.end("claude1", 30, "mac"),)
        _ = self.run_pass(self.now + timedelta(minutes=30))
        self.assertEqual(self.state().named, {missing, retained, old_peer})
        _ = self.run_pass(self.now + timedelta(hours=2))
        self.assertEqual(self.state().named, {missing, retained})
        self.assertEqual(len(self.sent), 1)

    def test_sitting_selects_the_earliest_machine_date_and_names_both_machines_once(self) -> None:
        self.local = (self.end("claude1", 20), self.end("claude2", 11), self.end("claude3", 10))
        self.peer = (self.end("claude1", 4, "mac"), self.end("claude2", 12, "mac"))
        _ = self.run_pass(self.now)
        self.assertEqual(len(self.sent), 1)
        push, kind = self.sent[0]
        self.assertEqual(kind, "sitting")
        self.assertIn("claude1: Mac Sat 11-21, natedev Mon 12-07", push.body)
        self.assertIn("claude3: natedev Fri 11-27", push.body)
        self.assertNotIn("claude2", push.body)
        self.assertEqual(self.state().sitting, self.now)
        self.assertEqual(self.state().named, {self.local[0], self.local[2], self.peer[0]})
        _ = self.run_pass(self.now + timedelta(minutes=1))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.peer_calls, [("mac", 30)])

    def test_sitting_outside_the_window_selects_only_the_account_with_the_earliest_end(self) -> None:
        self.local = (self.end("claude1", 22), self.end("claude2", 25))
        self.peer = (self.end("claude2", 12, "mac"),)
        _ = self.run_pass(self.now)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("claude1", self.sent[0][0].body)
        self.assertIn("claude2: Mac Sun 11-29, natedev Sat 12-12", self.sent[0][0].body)

    def test_sitting_without_any_stored_date_records_the_sitting_without_a_push(self) -> None:
        _ = self.run_pass(self.now)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state().sitting, self.now)
        self.local = (self.end("claude1", 5),)
        _ = self.run_pass(self.now + timedelta(minutes=1))
        self.assertEqual(self.sent, [])

    def test_missed_sittings_send_only_for_the_latest_sitting_window(self) -> None:
        self.local = (self.end("claude1", 30),)
        later = self.now + timedelta(days=23)
        _ = self.run_pass(later)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.state().sitting, self.now + timedelta(days=21))
        _ = self.run_pass(later)
        self.assertEqual(len(self.sent), 1)

    def test_safety_names_each_unnamed_login_at_the_margin_only_once(self) -> None:
        at = self.now - timedelta(days=1)
        self.local = (self.end("claude1", 2), self.end("claude2", 8))
        self.peer = (self.end("claude1", 10, "mac"),)
        _ = self.run_pass(at)
        self.assertEqual([kind for _, kind in self.sent], ["safety"])
        self.assertIn("claude1: natedev Thu 11-19", self.sent[0][0].body)
        self.assertNotIn("Mac", self.sent[0][0].body)
        _ = self.run_pass(at + timedelta(minutes=1))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.state().named, {self.local[0]})

    def test_missing_dates_keep_named_records_and_do_not_repeat_the_safety_push_when_dates_return(self) -> None:
        at = self.now - timedelta(days=1)
        local = self.end("claude1", 1)
        peer = self.end("claude2", 1, "mac")
        self.local, self.peer = (local,), (peer,)
        _ = self.run_pass(at)
        self.assertEqual([kind for _, kind in self.sent], ["safety"])
        self.local, self.peer = (), ()
        _ = self.run_pass(at + timedelta(hours=1))
        self.assertEqual(self.state().named, {local, peer})
        self.local, self.peer = (local,), (peer,)
        _ = self.run_pass(at + timedelta(hours=2))
        self.assertEqual([kind for _, kind in self.sent], ["safety"])
        self.assertEqual(self.state().named, {local, peer})

    def test_a_sitting_push_prevents_a_safety_push_for_the_same_end_date(self) -> None:
        self.local = (self.end("claude1", 2),)
        _ = self.run_pass(self.now)
        _ = self.run_pass(self.now + timedelta(days=1))
        self.assertEqual([kind for _, kind in self.sent], ["sitting"])

    def test_renewed_login_leaves_the_window_and_a_new_end_can_get_a_safety_push(self) -> None:
        self.local = (self.end("claude1", 1), self.end("claude2", 15))
        _ = self.run_pass(self.now)
        self.local = (self.end("claude1", 29), self.end("claude2", 15))
        _ = self.run_pass(self.now + timedelta(days=7))
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("claude1", self.sent[1][0].body)
        self.assertIn("claude2", self.sent[1][0].body)
        _ = self.run_pass(self.now + timedelta(days=26))
        self.assertIn(self.local[0], self.state().named)

    def test_failed_peer_read_keeps_the_last_good_dates_and_throttles_attempts(self) -> None:
        self.peer = (self.end("claude4", 5, "mac"),)
        _ = self.run_pass(self.now - timedelta(days=2))
        self.peer = sittings.PeerReadFailed("fixture offline")
        lines = self.run_pass(self.now)
        self.assertIn("fixture offline", "\n".join(lines))
        self.assertIn("claude4: Mac Sun 11-22", self.sent[0][0].body)
        self.assertEqual(self.state().peer, (self.end("claude4", 5, "mac"),))
        self.assertEqual(self.state().peer_read, self.now - timedelta(days=2))
        _ = self.run_pass(self.now + timedelta(minutes=59))
        self.assertEqual(len(self.peer_calls), 2)
        _ = self.run_pass(self.now + timedelta(minutes=60))
        self.assertEqual(len(self.peer_calls), 3)

    def test_local_read_failure_keeps_the_peer_attempt_and_result_and_throttles_the_next_pass(self) -> None:
        old_peer = self.end("claude4", 5, "mac")
        new_peer = self.end("claude4", 29, "mac")
        results: tuple[tuple[sittings.LoginEnd, ...] | sittings.PeerReadFailed, ...] = (
            (new_peer,), sittings.PeerReadFailed("fixture offline"))
        for index, result in enumerate(results):
            with (self.subTest(result=result),
                  mock.patch.object(accounts, "accounts_home", return_value=self.root / str(index))):
                self.peer_calls.clear()
                self.sent.clear()
                self.peer = (old_peer,)
                _ = self.run_pass(self.now - timedelta(hours=2))
                self.peer = result
                with (mock.patch.object(self, "read_local", side_effect=RuntimeError("fixture local read failed")),
                      self.assertRaisesRegex(RuntimeError, "fixture local read failed")):
                    _ = self.run_pass(self.now)
                state = sittings.load_state(accounts.accounts_home() / ".local/state/accounts/login_sittings.json")
                self.assertEqual(state.peer_attempt, self.now)
                if isinstance(result, sittings.PeerReadFailed):
                    self.assertEqual(state.peer, (old_peer,))
                    self.assertEqual(state.peer_read, self.now - timedelta(hours=2))
                else:
                    self.assertEqual(state.peer, (new_peer,))
                    self.assertEqual(state.peer_read, self.now)
                _ = self.run_pass(self.now + timedelta(minutes=1))
                self.assertEqual(self.peer_calls, [("mac", 30), ("mac", 30)])

    def test_failed_send_records_no_sitting_or_named_login_and_retries(self) -> None:
        self.local = (self.end("claude1", 1),)
        self.outcome = PushFailed("fixture send failed")
        _ = self.run_pass(self.now)
        self.assertIsInstance(self.state().sitting, sittings.NoRecordedTime)
        self.assertEqual(self.state().named, set())
        self.outcome = PushSent()
        _ = self.run_pass(self.now + timedelta(minutes=1))
        self.assertEqual([kind for _, kind in self.sent], ["sitting", "sitting"])
        self.assertEqual(self.state().sitting, self.now)

    def test_failed_safety_send_records_no_login_and_retries(self) -> None:
        self.local = (self.end("claude1", 1),)
        self.outcome = PushFailed("fixture send failed")
        _ = self.run_pass(self.now - timedelta(days=1))
        self.assertEqual(self.state().named, set())
        self.outcome = PushSent()
        _ = self.run_pass(self.now - timedelta(hours=12))
        self.assertEqual([kind for _, kind in self.sent], ["safety", "safety"])

    def test_other_machine_reads_no_dates_and_creates_no_state(self) -> None:
        before = sorted(self.root.rglob("*"))
        self.assertEqual(self.run_pass(self.now, "mac"), [])
        self.assertEqual(self.peer_calls, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_local_dates_use_the_same_stored_rows_as_account_status(self) -> None:
        reports = [agent_accounts.Report("Claude", email="fixture-one",
            source=agent_accounts.ClaudeDirectorySource(self.root / ".claude1"),
            login_ends_at=agent_accounts.LoginEndKnown(self.end("claude1", 5).at)),
            agent_accounts.Report("Claude", email="fixture-shared",
            source=agent_accounts.ClaudeDirectorySource(self.root / ".claude"),
            login_ends_at=agent_accounts.LoginEndKnown(self.end("claude2", 6).at)),
            agent_accounts.Report("Claude", email="fixture-unknown",
            source=agent_accounts.ClaudeDirectorySource(self.root / ".claude3")),
            agent_accounts.Report("Codex", email="fixture-codex",
            source=agent_accounts.CodexHomeSource(self.root / ".codex1"))]
        usage_store.save(reports, self.now)
        with mock.patch.object(accounts, "read_status", return_value=self.status_reading(self.end("claude1", 5))) as read:
            self.assertEqual(sittings.local_dates("natedev"), (self.end("claude1", 5),))
        read.assert_called_once_with(source=accounts.StoredUsageOnly())

    def test_deleted_account_has_no_date_even_when_its_stored_directory_link_remains(self) -> None:
        current, removed = self.root / ".claude1", self.root / ".claude2"
        current.mkdir()
        removed.mkdir()
        usage_store.save([agent_accounts.Report("Claude", email=f"fixture-{index}",
            source=agent_accounts.ClaudeDirectorySource(directory),
            login_ends_at=agent_accounts.LoginEndKnown(self.end(f"claude{index}", index).at))
            for index, directory in enumerate((current, removed), start=1)], self.now)
        removed.rmdir()
        self.assertIn(usage_store.directory_key("Claude", str(removed)), usage_store.load().directory_logins)
        with mock.patch.object(accounts, "read_status", return_value=self.status_reading(self.end("claude1", 1))) as read:
            self.assertEqual(sittings.local_dates("natedev"), (self.end("claude1", 1),))
        read.assert_called_once_with(source=accounts.StoredUsageOnly())

    def test_peer_reader_uses_stored_json_and_excludes_unknown_or_non_claude_rows(self) -> None:
        rows = [{"label": "claude1", "login_ends_at": self.end("claude1", 4).at.isoformat()},
                {"label": "claude2", "login_ends_at": None},
                {"label": "codex1", "login_ends_at": self.now.isoformat()}]
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"accounts": rows}), "")) as read:
            self.assertEqual(sittings.peer_dates("mac", 37), (self.end("claude1", 4, "mac"),))
        self.assertEqual(read.call_args.args[0], ["ssh", "-o", "BatchMode=yes", "mac",
            '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" status --stored --json'])
        self.assertEqual(read.call_args.kwargs["timeout"], 37)
        for stdout, code in (("bad json", 0), ('{"accounts": null}', 0), ("", 255)):
            with self.subTest(stdout=stdout, code=code), mock.patch.object(subprocess, "run",
                    return_value=subprocess.CompletedProcess([], code, stdout, "")):
                self.assertIsInstance(sittings.peer_dates("mac", 37), sittings.PeerReadFailed)

    def test_sender_uses_a_key_per_kind_and_keeps_silence_enabled(self) -> None:
        push = sittings.reminder_push((self.end("claude1", 3),))
        kinds: tuple[sittings.PushKind, ...] = ("sitting", "safety")
        for kind in kinds:
            for status in ("SENT", "KEPT", "REPLACED"):
                with (self.subTest(kind=kind, status=status),
                      mock.patch.object(review, "user_send_seconds", return_value=15),
                      mock.patch.object(send_budget, "user_seconds", return_value=25),
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                          [], 0, f"{status}: fixture\n", "")) as send):
                    self.assertIsInstance(sittings.send_push(push, kind), PushSent)
                argv = cast(list[str], send.call_args.args[0])
                self.assertEqual(argv[argv.index("--key") + 1], f"login-{kind}")
                self.assertEqual(argv[argv.index("--text-id") + 1], sittings.TEXT_ID)
                self.assertEqual(argv[argv.index("--from") + 1], "accounts")
                self.assertEqual(argv[argv.index("--need") + 1], "note")
                self.assertNotIn("--through-silence", argv)
        with (mock.patch.object(review, "user_send_seconds", return_value=15),
              mock.patch.object(send_budget, "user_seconds", return_value=25),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", ""))):
            self.assertIsInstance(sittings.send_push(push, "safety"), PushFailed)


if __name__ == "__main__":
    _ = unittest.main()
