"""Account ownership, live swap advice and fallback recovery across workers swaps."""

from __future__ import annotations

from scripts.lib.test_agents_registry import registry_text, write_registry

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import note_file
import quota_alert
import agent_accounts as sibling_agent_accounts
from scripts.whoami import agent_accounts
from agent_accounts import CodexHomeSource, Quota, Report
from scripts.accounts import accounts, moves, next_moves, refusals, role_map
from scripts.lib import agents_registry
from scripts.production import showrunners
import usage_store
from push_texts import RenderedPush, catalog, render_entry
from user_action import ActionRequired
from test_quota_alert import REGISTRY

CURRENT_READING_AGE = timedelta()


def registry_view() -> str:
    """The registry as the alert reads it: the defaults with the overlay its edits write."""
    return agents_registry.registry_text(quota_alert.REGISTRY)


def reset_registry(text: str) -> None:
    """Make `text` the whole registry: the defaults, and no overlay over them."""
    _ = write_registry(quota_alert.REGISTRY, base_text=text)
    agents_registry.overlay_path(quota_alert.REGISTRY).unlink(missing_ok=True)


class CodexAccountAlertTests(unittest.TestCase):
    root: Path = Path()
    now: datetime = datetime.now(timezone.utc)
    sent: list[str] = []
    pushes: list[RenderedPush] = []
    relayed: list[str] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.sent = []
        self.pushes = []
        self.relayed = []
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        _ = self.enterContext(patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unexpected ssh")))
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
        _ = write_registry(registry, base_text=REGISTRY)
        _ = self.enterContext(patch.object(quota_alert, "CONFIG", config))
        _ = self.enterContext(patch.object(quota_alert, "STATE", self.root / "alerts.json"))
        _ = self.enterContext(patch.object(quota_alert, "REGISTRY", registry))
        _ = self.enterContext(patch.object(quota_alert, "recipients", return_value=["director"]))
        _ = self.enterContext(patch.object(quota_alert, "relay", side_effect=self.relay))
        _ = self.enterContext(patch.object(quota_alert, "tell_user", side_effect=self.push))
        _ = self.enterContext(patch.object(accounts, "_retire_workers_servers", return_value=[]))
        _ = self.enterContext(patch.object(accounts, "_codex_live", side_effect=self.live_for_swap))

    def live_for_swap(self, home: Path) -> Report:
        identity = agent_accounts.codex_account(home)
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        return self.report(identity, 80)

    def relay(self, recipient: str, text: str, key: str | None) -> None:
        del recipient, key
        self.sent.append(text)
        self.relayed.append(text)

    def push(self, push: RenderedPush) -> quota_alert.PushDelivery:
        self.pushes.append(push)
        self.sent.append(push.body)
        return quota_alert.PushSent()

    def test_fresh_room_after_a_weekly_refusal_is_picked_without_a_later_reset(self) -> None:
        target, source = self.account(1), self.account(2)
        refused_at = self.now - timedelta(days=1)
        refusals.write_refusal(refusals.AccountRefusal("codex", target.login,
            refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=3)), refused_at))
        for reset in (None, self.now + timedelta(hours=1)):
            with self.subTest(reset=reset):
                report = self.report(target, 100)
                report.quotas = [Quota("Weekly", 0, reset)]
                usage_store.save([report], self.now)
                chosen = accounts.pick("codex", self.now, exclude=frozenset({source.label}))
                assert isinstance(chosen, accounts.PickedAccount)
                self.assertEqual(chosen.label, target.label)
                self.assertFalse(accounts.codex_destination_uses_credits(chosen, self.now))

    def test_next_move_reminder_uses_the_real_default_destination_and_credits_permission(self) -> None:
        source, target, later = self.account(1), self.account(2), self.account(3)
        # The low account's stored allowance must never make it its own fallback.
        self.save_room(source, 100, reset=self.now + timedelta(hours=1))
        for credits in ("on", "off"):
            for fallback in ("codex2", "Claude"):
                for left in (2, 1, 0, 3):
                    with self.subTest(credits=credits, fallback=fallback, left=left):
                        self.save_room(target, 80 if fallback == "codex2" else 0,
                                       reset=self.now + timedelta(hours=12))
                        self.save_room(later, 90 if fallback == "codex2" else 0)
                        note = self.note(1, "active", left)
                        _ = note.path.write_text(note.path.read_text().replace(
                            "credit_balance:", f"use_credits: {credits}\ncredit_balance:"))
                        note = note_file.read_note(note.path)
                        assert isinstance(note, note_file.Note)
                        push = quota_alert.codex_next_move_push(note, now=self.now)
                        if not 0 < left <= 2:
                            self.assertIsInstance(push, quota_alert.QuotaStatusNote)
                            continue
                        self.assertIsInstance(push, quota_alert.UserPush)
                        assert isinstance(push, quota_alert.UserPush)
                        rendered = push.rendered
                        self.assertEqual(rendered.values["fallback"], fallback)
                        self.assertNotEqual(rendered.values["fallback"], source.label)
                        self.assertLessEqual(len(rendered.title), 45)
                        self.assertLess(len(rendered.body), 500)
                        self.assertLess(len(rendered.message), 500)
                        if credits == "on" and fallback == "Claude":
                            title = "codex1 Codex workers will use paid credits"
                            body = (f"codex1 has {left}% of its weekly usage left, and no move is set up for its workers. "
                                    + f"When it runs out, they spend its paid credits, then move to {fallback}. Nothing is needed.")
                        elif fallback == "Claude":
                            title = "codex1 Codex workers will move to Claude"
                            body = (f"codex1 has {left}% of its weekly usage left. When it runs out, its workers move on their own "
                                    + "to Claude, since no other Codex account has room. Nothing is needed.")
                        else:
                            title = "codex1 Codex workers will move to codex2"
                            body = (f"codex1 has {left}% of its weekly usage left. When it runs out, its workers move on their own "
                                    + "to codex2, the Codex account with room whose weekly reset comes first. Nothing is needed.")
                        self.assertEqual(rendered.title, title)
                        self.assertEqual(rendered.body, body)
                        self.assertEqual(rendered.action, ActionRequired(
                            "Optional: to move them at 1% instead, type in the natedev session: "
                            + f"/account_moves when workers reach 1% switch to {fallback}"))

    def test_codex_reminder_catalog_examples_match_the_three_chosen_texts(self) -> None:
        entry = next(entry for entry in catalog() if entry.id == "quota.codex_next_move")
        expected = {
            "credits_on": ("codex2", "2%", "codex1",
                "codex2 Codex workers will use paid credits",
                "codex2 has 2% of its weekly usage left, and no move is set up for its workers. When it runs out, they spend its paid credits, then move to codex1. Nothing is needed."),
            "credits_off_codex": ("codex2", "2%", "codex1",
                "codex2 Codex workers will move to codex1",
                "codex2 has 2% of its weekly usage left. When it runs out, its workers move on their own to codex1, the Codex account with room whose weekly reset comes first. Nothing is needed."),
            "credits_off_claude": ("codex1", "1%", "Claude",
                "codex1 Codex workers will move to Claude",
                "codex1 has 1% of its weekly usage left. When it runs out, its workers move on their own to Claude, since no other Codex account has room. Nothing is needed."),
        }
        self.assertEqual({example.name for example in entry.examples}, set(expected))
        for example in entry.examples:
            with self.subTest(example=example.name):
                account, remaining, fallback, title, body = expected[example.name]
                rendered = render_entry(entry, example.values)
                self.assertEqual((rendered.values["account"], rendered.values["remaining"], rendered.values["fallback"]),
                                 (account, remaining, fallback))
                self.assertEqual((rendered.title, rendered.body), (title, body))
                self.assertEqual(rendered.action, ActionRequired(
                    "Optional: to move them at 1% instead, type in the natedev session: "
                    + f"/account_moves when workers reach 1% switch to {fallback}"))

    def test_scheduled_codex_move_fires_once_consumes_entry_and_keeps_auto_role_reason(self) -> None:
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        entry = next_moves.add_next_move("workers", 1, "codex2")
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertEqual(next_moves.read_next_moves(), ())
        requests = [row for row in moves.read_moves() if row["kind"] == "requested"]
        self.assertEqual([(row.get("cause"), row["why"], row["to"]) for row in requests],
                         [("auto", f"auto: {entry.id}", "codex2")])
        roles = [cast(dict[str, object], json.loads(line))
                 for line in accounts.roles_history_file().read_text().splitlines()]
        self.assertEqual((roles[-1]["cause"], roles[-1]["why"]), ("auto", f"auto: {entry.id}"))
        returned = self.readings(source, self.report(source), self.report(target, 80))
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=returned)
        self.assertEqual(agent_accounts.workers_home(), source.home)
        self.assertEqual(len([row for row in moves.read_moves() if row["kind"] == "requested"]), 1)

    def test_refused_scheduled_swap_keeps_entry_and_records_failure_then_can_retry(self) -> None:
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        entry = next_moves.add_next_move("workers", 1, "codex2")
        with patch.object(accounts, "swap", side_effect=accounts.AccountsError("fixture swap refused")):
            _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertEqual(agent_accounts.workers_home(), source.home)
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        journal = moves.read_moves()
        self.assertEqual([row["kind"] for row in journal], ["requested", "failed"])
        self.assertEqual(journal[0]["move_id"], journal[1]["move_id"])
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertEqual(next_moves.read_next_moves(), ())

    def test_interrupted_workers_log_removal_does_not_replay_after_operator_return(self) -> None:
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        entry = next_moves.add_next_move("workers", 1, "codex2")
        with patch.object(next_moves, "write_next_moves", side_effect=OSError("fixture log replacement failed")):
            try:
                _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
            except OSError:
                pass
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        returned = self.readings(source, self.report(source), self.report(target, 80))
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=returned)
        self.assertEqual(agent_accounts.workers_home(), source.home)
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual(len([row for row in moves.read_moves() if row["kind"] == "requested"]), 1)

    def test_failed_routing_append_preserves_the_scheduled_hold_and_recovery(self) -> None:
        """F001: a journal error must not strand assignments on Claude."""
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        _ = next_moves.add_next_move("workers", 1, "claude")
        reset = self.now + timedelta(days=2)
        with patch.object(moves, "append_routing", side_effect=OSError("fixture routing append failed")) as append:
            _ = quota_alert.run_workers_next_moves(readings, self.now)
        self.assertTrue(append.called)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.hold, quota_alert.ScheduledMoveHold(reset))
            self.assertIn("direct", fallback.moved)
        later = self.now + timedelta(minutes=2)
        fresh = quota_alert.CodexAlertPassReadings(readings.workers, source,
            (quota_alert.CodexHomeReading(source.home, self.report(source, 80), later),))
        _ = quota_alert.alert([self.note(1, "active", 80)], later, codex_readings=fresh)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
        refilled = self.report(source, 80)
        refilled.quotas = [Quota("Weekly", 20, reset + timedelta(days=7))]
        refilled.five_hour = Quota("5-hour", 0, reset + timedelta(hours=5))
        fresh = quota_alert.CodexAlertPassReadings(readings.workers, source,
            (quota_alert.CodexHomeReading(source.home, refilled, reset),))
        _ = quota_alert.alert([self.note(1, "active", 80)], reset, codex_readings=fresh)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")

    def test_failed_failure_append_does_not_consume_a_refused_workers_entry(self) -> None:
        """F002: an unrecorded refusal must remain retryable after restart."""
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        entry = next_moves.add_next_move("workers", 1, "codex2")
        append_move = moves.append_move

        def append_or_fail(row: moves.MoveEntry) -> None:
            if row["kind"] == "failed":
                raise OSError("fixture failed-row append failed")
            append_move(row)

        with (patch.object(accounts, "swap", side_effect=accounts.AccountsError("fixture swap refused")) as swap,
              patch.object(moves, "append_move", side_effect=append_or_fail)):
            _ = quota_alert.run_workers_next_moves(readings, self.now)
        swap.assert_called_once()
        self.assertEqual(agent_accounts.workers_home(), source.home)
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        _ = quota_alert.run_workers_next_moves(readings, self.now)
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertEqual(next_moves.read_next_moves(), ())

    def test_pending_failure_retry_after_a_failed_state_save_records_one_failure(self) -> None:
        """F009: a saved retry must not duplicate a failure already in the journal."""
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        entry = next_moves.add_next_move("workers", 1, "codex2")
        append_move = moves.append_move

        def refuse_failure_append(row: moves.MoveEntry) -> None:
            if row["kind"] == "failed":
                raise OSError("fixture failed-row append failed")
            append_move(row)

        with (patch.object(accounts, "swap", side_effect=accounts.AccountsError("fixture swap refused")),
              patch.object(moves, "append_move", side_effect=refuse_failure_append)):
            _ = quota_alert.run_workers_next_moves(readings, self.now)
        with quota_alert.state_file() as state:
            pending = list(state.get("pending_move_failures", []))
        self.assertEqual(len(pending), 1)
        self.assertEqual([row["kind"] for row in moves.read_moves()], ["requested"])

        replace = os.replace

        def fail_retry_state_save(source_path: str | Path, target_path: str | Path) -> None:
            if Path(target_path) == quota_alert.STATE:
                raise OSError("fixture retry state save failed")
            replace(source_path, target_path)

        with patch.object(os, "replace", side_effect=fail_retry_state_save) as save:
            _ = quota_alert.run_workers_next_moves(readings, self.now)
        self.assertTrue(save.called)
        failures = [row for row in moves.read_moves() if row["kind"] == "failed"]
        self.assertEqual(failures, pending)
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("pending_move_failures"), pending)
        self.assertEqual(agent_accounts.workers_home(), source.home)
        self.assertEqual(next_moves.read_next_moves(), (entry,))

        _ = quota_alert.run_workers_next_moves(readings, self.now)
        failures = [row for row in moves.read_moves() if row["kind"] == "failed"]
        self.assertEqual(failures, pending)
        self.assertEqual(len(failures), 1)
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertEqual(next_moves.read_next_moves(), ())
        with quota_alert.state_file() as state:
            self.assertFalse(state.get("pending_move_failures"))

    def test_zero_then_positive_low_does_not_rearm_a_delivered_reminder(self) -> None:
        """F004: 1% -> 0% -> 1% stays in the same weekly low episode."""
        source = self.account(1)
        readings = self.readings(source, self.report(source, 1, "20"))
        for minute, left in ((0, 1), (2, 0), (4, 1)):
            at = self.now + timedelta(minutes=minute)
            fresh = quota_alert.CodexAlertPassReadings(readings.workers, source,
                (quota_alert.CodexHomeReading(source.home, self.report(source, left, "20"), at),))
            _ = quota_alert.alert([self.note(1, "active", left, "20")], at, codex_readings=fresh)
            self.assertEqual(len([push for push in self.pushes if push.id == "quota.codex_next_move"]), 1)
        self.assertEqual(agent_accounts.workers_home(), source.home)

    def test_scheduled_routing_notice_names_the_source_weekly_reset_deadline(self) -> None:
        """F005: allowance recovery cannot end the scheduled hold early."""
        source = self.account(1)
        readings = self.readings(source, self.report(source))
        entry = next_moves.add_next_move("workers", 1, "claude")
        _ = quota_alert.run_workers_next_moves(readings, self.now)
        with quota_alert.state_file() as state:
            text = "\n".join(state.get("unseen", []))
        self.assertIn(f"auto: {entry.id}", text)
        self.assertIn("weekly reset", text)
        self.assertIn(quota_alert.local_time((self.now + timedelta(days=2)).isoformat()), text)
        self.assertNotIn("move back when Codex recovers", text)

    def test_engine_swap_replaces_zero_source_reading_before_the_default_zero_move(self) -> None:
        source, target, other = self.account(1), self.account(2), self.account(3)
        self.save_room(source, 0)
        self.save_room(target, 80)
        self.save_room(other, 80)
        note = self.note(1, "active", 0)
        _ = note.path.write_text(note.path.read_text().replace("credit_balance:", "use_credits: off\ncredit_balance:"))
        note = note_file.read_note(note.path)
        assert isinstance(note, note_file.Note)
        readings = self.readings(source, self.report(source, 0), self.report(target, 80), self.report(other, 80))
        _ = next_moves.add_next_move("workers", 1, "codex2")
        with patch.object(accounts, "swap", wraps=accounts.swap) as swap:
            _ = quota_alert.alert([note], self.now, codex_readings=readings)
        self.assertEqual(swap.call_count, 1)
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertIn("direct=codex", registry_view())

    def test_unreadable_workers_journal_preserves_queue_and_roles_and_blocks_fallback_reminders(self) -> None:
        source, target = self.account(1), self.account(2)
        entry = next_moves.add_next_move("workers", 1, "codex2")
        journal = moves.store_file()
        journal.parent.mkdir(parents=True, exist_ok=True)
        open_path = Path.open
        for problem, left in ((problem, left) for problem in ("permission", "malformed", "encoding")
                              for left in (0, 1)):
            with self.subTest(problem=problem, left=left):
                quota_alert.STATE.unlink(missing_ok=True)
                self.pushes.clear()
                readings = self.readings(source, self.report(source, left, "20"), self.report(target, 80))
                original = b'{broken\n' if problem == "malformed" else b'\xff\n' if problem == "encoding" else b''
                _ = journal.write_bytes(original)

                def read_or_refuse(path: Path, mode: str = "r", buffering: int = -1,
                                   encoding: str | None = None, errors: str | None = None,
                                   newline: str | None = None) -> object:
                    if path == journal and mode == "rb" and problem == "permission":
                        raise PermissionError("fixture move history unreadable")
                    return open_path(path, mode, buffering, encoding, errors, newline)

                output = io.StringIO()
                with (patch.object(Path, "open", read_or_refuse), redirect_stdout(output),
                      patch.object(quota_alert, "_codex_zero_alert", wraps=quota_alert._codex_zero_alert) as zero):  # pyright: ignore[reportPrivateUsage]
                    _ = quota_alert.alert([self.note(1, "active", left, "20")], self.now, codex_readings=readings)
                self.assertTrue(zero.called)
                self.assertEqual(agent_accounts.workers_home(), source.home)
                self.assertIn("direct=codex", registry_view())
                self.assertEqual(next_moves.read_next_moves(), (entry,))
                self.assertEqual(journal.read_bytes(), original)
                self.assertEqual(len([line for line in output.getvalue().splitlines()
                                      if "cannot read" in line]), 1)
                self.assertEqual([push.id for push in self.pushes],
                                 ["quota.paid_credits_queued"] if left == 0 else [])
                if left == 1:
                    self.assertEqual(quota_alert.codex_next_move_push(self.note(1, "active", left), now=self.now,
                        history=moves.UnreadableMoveJournal()),
                        quota_alert.QuotaStatusNote("codex1", "what happens next is set up"))

    def test_torn_final_workers_journal_line_does_not_block_the_entry(self) -> None:
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        entry = next_moves.add_next_move("workers", 1, "codex2")
        journal = moves.store_file()
        journal.parent.mkdir(parents=True, exist_ok=True)
        _ = journal.write_bytes(b'{"kind":')
        output = io.StringIO()
        with redirect_stdout(output):
            _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertEqual(next_moves.read_next_moves(), ())
        lines = [line for line in output.getvalue().splitlines() if "torn final line" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("moves.jsonl", lines[0])
        self.assertEqual([row["why"] for row in moves.read_moves() if row["kind"] == "requested"],
                         [f"auto: {entry.id}"])

    def test_workers_engine_requires_host_fresh_readings_target_room_and_resolved_refusal(self) -> None:
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        entry = next_moves.add_next_move("workers", 1, "codex2")
        stale = self.now - timedelta(minutes=11)
        for problem in ("remote", "stale-source", "stale-target", "low-target", "refused-target"):
            with self.subTest(problem=problem):
                report = self.report(target, 1 if problem == "low-target" else 80)
                snapshot = quota_alert.CodexAlertPassReadings(readings.workers, source,
                    (quota_alert.CodexHomeReading(source.home, self.report(source), stale if problem == "stale-source" else self.now),
                     quota_alert.CodexHomeReading(target.home, report, stale if problem == "stale-target" else self.now)))
                if problem == "refused-target":
                    refusals.write_refusal(refusals.AccountRefusal("codex", target.login,
                        refusals.KnownWindow("weekly"), refusals.KnownReset(self.now + timedelta(days=2)), self.now))
                with patch.object(role_map, "is_role_map_host", return_value=problem != "remote"):
                    # The engine must not reach the remote transport at all.
                    _ = quota_alert.run_workers_next_moves(snapshot, self.now)
                self.assertEqual(agent_accounts.workers_home(), source.home)
                self.assertEqual(next_moves.read_next_moves(), (entry,))
                self.assertEqual(moves.read_moves(), [])

    def test_scheduled_claude_route_holds_until_source_reset_even_with_positive_allowance(self) -> None:
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        entry = next_moves.add_next_move("workers", 1, "claude")
        reset = self.now + timedelta(days=2)
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertIn("direct=claude", registry_view())
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.hold, quota_alert.ScheduledMoveHold(reset))
        route = [row for row in moves.read_journal() if row["kind"] == "routed to claude"]
        self.assertEqual([(row["cause"], row["why"]) for row in route], [("auto", f"auto: {entry.id}")])
        for at in (self.now + timedelta(minutes=2), reset - timedelta(seconds=1)):
            fresh = quota_alert.CodexAlertPassReadings(readings.workers, source,
                (quota_alert.CodexHomeReading(source.home, self.report(source, 80), at),
                 quota_alert.CodexHomeReading(target.home, self.report(target, 80), at)))
            _ = quota_alert.alert([self.note(1, "active", 80)], at, codex_readings=fresh)
            self.assertIn("direct=claude", registry_view())
            self.assertEqual(agent_accounts.workers_home(), source.home)
        refilled = self.report(source, 80)
        refilled.quotas = [Quota("Weekly", 20, reset + timedelta(days=7))]
        refilled.five_hour = Quota("5-hour", 0, reset + timedelta(hours=5))
        fresh = quota_alert.CodexAlertPassReadings(readings.workers, source,
            (quota_alert.CodexHomeReading(source.home, refilled, reset),))
        _ = quota_alert.alert([self.note(1, "active", 80)], reset, codex_readings=fresh)
        self.assertIn("direct=codex", registry_view())

    def test_scheduled_hold_suppresses_low_phone_push_and_survives_state_reload(self) -> None:
        source = self.account(1)
        readings = self.readings(source, self.report(source))
        _ = next_moves.add_next_move("workers", 1, "claude")
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertEqual(self.pushes, [])
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.hold, quota_alert.ScheduledMoveHold(self.now + timedelta(days=2)))
            self.assertEqual(state["episodes"]["codex1"].get("push"), "due")
        later = self.now + timedelta(minutes=32)
        fresh = quota_alert.CodexAlertPassReadings(readings.workers, source,
            (quota_alert.CodexHomeReading(source.home, self.report(source), later),))
        _ = quota_alert.alert([self.note(1, "active", 1)], later, codex_readings=fresh)
        self.assertEqual(self.pushes, [])
        self.assertIn("direct=claude", registry_view())

    def test_workers_head_targeting_current_account_blocks_successors_and_reports_once(self) -> None:
        source, first, second = self.account(1), self.account(2), self.account(3)
        readings = self.readings(source, self.report(source), self.report(first, 80), self.report(second, 80))
        same = next_moves.add_next_move("workers", 1, "codex1")
        move = next_moves.add_next_move("workers", 1, "codex2")
        later = next_moves.add_next_move("workers", 1, "codex3")
        with (patch.object(accounts, "swap", wraps=accounts.swap) as swap,
              patch.object(accounts, "check_codex_target", wraps=accounts.check_codex_target) as check,
              patch.object(quota_alert, "notify_blocked_next_move", wraps=quota_alert.notify_blocked_next_move) as notice):
            for at in (self.now, self.now + timedelta(minutes=2)):
                _ = quota_alert.alert([self.note(1, "active", 1)], at, codex_readings=readings)
                self.assertEqual(agent_accounts.workers_home(), source.home)
                self.assertEqual(next_moves.read_next_moves(), (same, move, later))
                self.assertEqual(moves.read_journal(), [])
            swap.assert_not_called()
            check.assert_not_called()
            notice.assert_called_once_with("workers", "codex1", same.id,
                                           quota_alert.TargetIsCurrentAccount(), self.now)
        self.assertEqual(len(self.relayed), 1)
        self.assertNotIn(same.id, self.relayed[0])
        for text in ("workers 1", "codex1", "due", "already the current account"):
            self.assertIn(text, self.relayed[0])
        self.assertEqual(self.pushes, [])

    def test_workers_head_target_exceeds_its_own_threshold_despite_a_higher_successor_threshold(self) -> None:
        source, target = self.account(1), self.account(2)
        head = next_moves.add_next_move("workers", 1, "codex2")
        later = next_moves.add_next_move("workers", 90, "claude")
        readings = self.readings(source, self.report(source), self.report(target, 80))
        with patch.object(accounts, "swap", wraps=accounts.swap) as swap:
            returned = quota_alert.run_workers_next_moves(readings, self.now)
        swap.assert_called_once_with("workers", "codex2", why=f"auto: {head.id}", cause="auto")
        self.assertEqual(returned.workers, accounts.CodexWorkersAccount(target.label, target.home))
        self.assertEqual(next_moves.read_next_moves(), (later,))
        self.assertEqual([row["why"] for row in moves.read_moves() if row["kind"] == "requested"], [f"auto: {head.id}"])

    def test_workers_head_target_at_or_below_its_own_threshold_stays_queued_and_is_reported(self) -> None:
        source, target = self.account(1), self.account(2)
        head = next_moves.add_next_move("workers", 1, "codex2")
        later = next_moves.add_next_move("workers", 90, "claude")
        with patch.object(accounts, "swap", wraps=accounts.swap) as swap:
            for left in (1, 0):
                readings = self.readings(source, self.report(source), self.report(target, left))
                _ = quota_alert.run_workers_next_moves(readings, self.now)
                self.assertEqual(next_moves.read_next_moves(), (head, later))
                self.assertEqual(agent_accounts.workers_home(), source.home)
                swap.assert_not_called()
                self.assertEqual(len(self.relayed), 1)
                self.assertIn("at or below 1%", self.relayed[0])
        self.assertEqual(moves.read_journal(), [])

    def test_queued_workers_head_suppresses_fallback_push_and_self_target_has_a_status_note(self) -> None:
        source, target = self.account(1), self.account(2)
        readings = self.readings(source, self.report(source), self.report(target, 80))
        for target_label, expected in (("codex1", "the queued next entry targets the current account"),
                                       ("codex2", "what happens next is set up")):
            with self.subTest(target=target_label):
                head = next_moves.add_next_move("workers", 1, target_label)
                note = self.note(1, "active", 1)
                _ = note.path.write_text(note.path.read_text().replace("credit_balance:", "use_credits: off\ncredit_balance:"))
                note = note_file.read_note(note.path)
                assert isinstance(note, note_file.Note)
                reminder = quota_alert.codex_next_move_push(note, now=self.now)
                self.assertEqual(reminder, quota_alert.QuotaStatusNote("codex1", expected))
                with patch.object(accounts, "swap", wraps=accounts.swap) as swap:
                    _ = quota_alert.alert([note], self.now, codex_readings=readings)
                self.assertEqual(self.pushes, [])
                if target_label == "codex1":
                    swap.assert_not_called()
                    self.assertEqual(next_moves.read_next_moves(), (head,))
                    self.assertIn("already the current account", self.relayed[-1])
                    next_moves.remove_next_move("workers", next_moves.GroupPosition(1))

    def test_due_codex_head_runs_with_an_unknown_source_weekly_reset(self) -> None:
        source, target = self.account(1), self.account(2)
        report = self.report(source)
        report.quotas = [Quota("Weekly", 99, None)]
        readings = self.readings(source, report, self.report(target, 80))
        head = next_moves.add_next_move("workers", 1, "codex2")
        with patch.object(accounts, "swap", wraps=accounts.swap) as swap:
            returned = quota_alert.run_workers_next_moves(readings, self.now)
        swap.assert_called_once_with("workers", "codex2", why=f"auto: {head.id}", cause="auto")
        self.assertEqual(returned.workers, accounts.CodexWorkersAccount(target.label, target.home))
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual([row["why"] for row in moves.read_moves() if row["kind"] == "requested"], [f"auto: {head.id}"])

    def test_queued_workers_head_still_records_the_brief_five_hour_refusal_wait(self) -> None:
        source, target = self.account(1), self.account(2)
        reset = self.now + timedelta(minutes=19)
        report = self.report(source, 80, five_left=0)
        report.five_hour = Quota("5-hour", 100, reset)
        readings = self.readings(source, report, self.report(target, 80))
        head = next_moves.add_next_move("workers", 1, "codex2")
        with (patch.object(accounts, "swap", wraps=accounts.swap) as swap,
              patch.object(accounts, "pick", side_effect=AssertionError("brief wait picked fallback"))):
            for _attempt in range(2):
                _ = quota_alert.blocked([self.note(1, "active", 80)], source, self.now,
                    codex_readings=readings, window=refusals.KnownWindow("five_hour"))
        swap.assert_not_called()
        waits = moves.read_moves()
        self.assertEqual([row["kind"] for row in waits], ["waited"])
        waited = waits[0]
        assert waited["kind"] == "waited"
        self.assertEqual(waited["reset"], reset.isoformat())
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.assertEqual(agent_accounts.workers_home(), source.home)
        self.assertIn("direct=codex", registry_view())

    def test_unknown_weekly_reset_keeps_a_claude_entry_without_routing(self) -> None:
        source = self.account(1)
        report = self.report(source)
        report.quotas = [Quota("Weekly", 99, None)]
        readings = self.readings(source, report)
        entry = next_moves.add_next_move("workers", 1, "claude")
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        self.assertIn("direct=codex", registry_view())
        self.assertEqual(moves.read_journal(), [])

    def assert_queued_workers_wait_for_known_reset(self, window: Literal["five_hour", "weekly"]) -> None:
        source, target = self.account(1), self.account(2)
        reset = self.now + timedelta(hours=3)
        report = self.report(source, 80)
        if window == "five_hour":
            report.five_hour = Quota("5-hour", 100, reset)
        else:
            report.quotas = [Quota("Weekly", 20, reset)]
        readings = self.readings(source, report, self.report(target, 80))
        head = next_moves.add_next_move("workers", 1, "codex2")
        with (patch.object(accounts, "pick", side_effect=AssertionError("queued reset wait picked an account")),
              patch.object(accounts, "swap", side_effect=AssertionError("queued reset wait moved workers"))):
            lines = quota_alert.blocked([self.note(1, "active", 80)], source, self.now,
                codex_readings=readings, window=refusals.KnownWindow(window))
            _ = quota_alert.blocked([self.note(1, "active", 80)], source, self.now + timedelta(minutes=2),
                codex_readings=readings, window=refusals.KnownWindow(window))
            waits = moves.read_moves()
            self.assertEqual([row["kind"] for row in waits], ["waited"])
            waited = waits[0]
            assert waited["kind"] == "waited"
            self.assertEqual((waited["window"], waited["reset"], waited["subject"]),
                             (window, reset.isoformat(), "workers"))
            window_text = "5-hour" if window == "five_hour" else "weekly"
            self.assertTrue(any(f"workers wait for codex1's {window_text} reset at " in line for line in lines), lines)
            renewed = self.readings(source, self.report(source, 80), self.report(target, 80))
            for at in (reset - timedelta(seconds=1), reset, reset + timedelta(minutes=2)):
                _ = quota_alert.alert([self.note(1, "active", 80)], at, codex_readings=renewed)
                ends = [row for row in moves.read_moves() if row["kind"] == "wait ended"]
                self.assertEqual(len(ends), int(at >= reset))
            self.assertEqual(ends[0]["move_id"], waited["move_id"])
        self.assertEqual(agent_accounts.workers_home(), source.home)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.assertIn("direct=codex", registry_view())
        self.assertEqual([row["kind"] for row in moves.read_moves()], ["waited", "wait ended"])
        self.assertEqual(self.pushes, [])

    def test_queued_workers_head_waits_for_a_five_hour_reset_three_hours_away(self) -> None:
        self.assert_queued_workers_wait_for_known_reset("five_hour")

    def test_queued_workers_head_waits_for_a_known_weekly_reset(self) -> None:
        self.assert_queued_workers_wait_for_known_reset("weekly")

    def test_no_workers_entry_keeps_long_reset_refusals_moving_immediately(self) -> None:
        source, target = self.account(1), self.account(2)
        for window in ("five_hour", "weekly"):
            with self.subTest(window=window):
                accounts.set_role("workers", "codex1")
                report = self.report(source, 80)
                reset = self.now + timedelta(hours=3)
                report.five_hour = Quota("5-hour", 100 if window == "five_hour" else 20, reset)
                report.quotas = [Quota("Weekly", 100 if window == "weekly" else 20, reset)]
                readings = self.readings(source, report, self.report(target, 80))
                with patch.object(accounts, "swap", wraps=accounts.swap) as swap:
                    _ = quota_alert.blocked([self.note(1, "active", 80)], source, self.now,
                        codex_readings=readings, window=refusals.KnownWindow(window))
                swap.assert_called_once()
                self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertFalse(any(row["kind"] == "waited" for row in moves.read_moves()))

    def test_no_entry_default_refusal_picks_soonest_reset_not_the_most_allowance(self) -> None:
        source, soon, ample = self.account(1), self.account(2), self.account(3)
        early = self.report(soon, 20)
        early.quotas = [Quota("Weekly", 80, self.now + timedelta(hours=12))]
        late = self.report(ample, 90)
        self.save_room(soon, 20, reset=self.now + timedelta(hours=12))
        self.save_room(ample, 90, reset=self.now + timedelta(days=2))
        readings = self.readings(source, self.report(source, 0), early, late)
        _ = quota_alert.blocked([self.note(1, "active", 0)], source, self.now,
            codex_readings=readings, window=refusals.KnownWindow("weekly"))
        self.assertEqual(agent_accounts.workers_home(), soon.home)
        self.assertIn("direct=codex", registry_view())

    def test_each_exited_refusal_tells_only_its_director_even_with_an_open_fallback(self) -> None:
        source = self.account(1)
        readings = self.readings(source, self.report(source, 0))
        note = self.note(1, "active", 0)
        commands = (("director-a", "impl", "codex_mesh start --prompt 'first task'"),
                    ("director-b", "test", "codex_mesh follow --to test --prompt 'second task'"))
        with patch.object(quota_alert, "relay", return_value=None) as relay:
            for director, worker, command in commands:
                log = quota_alert.blocked([note], source, self.now, codex_readings=readings,
                    stopped=quota_alert.StoppedWorkerCommand(director, worker, command))
                self.assertTrue(any(f"quota rerun {worker} -> {director}: sent" in line for line in log))
                with quota_alert.state_file() as state:
                    self.assertIsInstance(state["fallback"], quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(relay.call_count, 2)
            for call, (director, worker, command) in zip(relay.call_args_list, commands):
                recipient, text, key = cast(tuple[str, str, str | None], call.args)
                self.assertEqual(recipient, f"session:{director}")
                self.assertIn(worker, text)
                self.assertIn(command, text)
                self.assertIn("Rerun this command on Claude", text)
                self.assertIsNone(key)
            _ = quota_alert.blocked([note], source, self.now, codex_readings=readings,
                                    stopped=quota_alert.StoppedCommandUnknown())
            self.assertEqual(relay.call_count, 2)

    def account(self, number: int, *, default: bool = False) -> agent_accounts.KnownCodexAccount:
        home = self.root / (".codex" if default else f".codex{number}")
        home.mkdir(exist_ok=True)
        login = f"account{number}@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {
            "id_token": f"header.{claims}.signature",
        }}))
        _ = self.note(number, "inactive", 80)
        return agent_accounts.KnownCodexAccount(home, login, f"codex{number}")

    def note(self, number: int, state: str, left: int, credits: str = "0") -> quota_alert.AgentNote:
        path = self.root / f"codex{number}.md"
        _ = path.write_text(f"---\nlogin: account{number}@example.invalid\nrole: {'worker' if state == 'active' else '[]'}\nsessions: 0\n"
            + f"weekly_remaining_usage: {left}\ncredit_balance: {credits}\n"
            + f"resets: {(self.now + timedelta(days=2)).isoformat()}\n---\n")
        note = note_file.read_note(path)
        assert isinstance(note, note_file.Note)
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
        self.assertEqual(accounts.main(["set-role", "workers", label]), 0)
        return quota_alert.CodexAlertPassReadings(
            accounts.role_account("workers"), selected,
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

    def test_outgoing_refusal_cannot_move_workers_after_selection_changes_at_transition_lock(self) -> None:
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
                        accounts.set_role("workers", current.label)
                    with lock() as state:
                        yield state
                with (patch.object(quota_alert, "state_file", side_effect=change_before_lock),
                      patch.object(accounts, "swap", wraps=accounts.swap) as swap,
                      patch.object(quota_alert, "switch_to_claude") as fallback):
                    _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
                        codex_readings=readings, window=window)
                swap.assert_not_called()
                fallback.assert_not_called()
                self.assertEqual(agent_accounts.workers_home(), current.home)
                self.assertEqual(moves.read_moves(), [])
                self.assertIn("direct=codex", registry_view())

    def test_usage_store_write_failure_does_not_prevent_low_quota_alert(self) -> None:
        old, target = self.account(1), self.account(2)
        readings = self.readings(old, self.report(old, 1), self.report(target))
        errors = io.StringIO()
        with patch.object(usage_store, "write", side_effect=PermissionError("read-only store")), redirect_stderr(errors):
            _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=readings)
        self.assertEqual([push.title for push in self.pushes], ["codex1 Codex workers will use paid credits"])
        self.assertIn("read-only store", errors.getvalue())

    def test_swap_advice_does_not_write_usage_store(self) -> None:
        old, target = self.account(1), self.account(2)
        readings = self.readings(old, self.report(old), self.report(target))
        self.save_room(old, 1)
        self.save_room(target, 1)
        with patch.object(usage_store, "write") as write:
            self.assertEqual(quota_alert.codex_swap_line(readings, self.now), "workers move to codex2 when Codex refuses work")
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
        self.assertEqual(agent_accounts.workers_home(), target.home)
        request = moves.read_moves()[0]
        self.assertEqual(request["kind"], "requested")
        self.assertNotIn("reset", request)

    def test_post_refusal_window_inference_keeps_subsecond_observation_time(self) -> None:
        old, target = self.account(1), self.account(2)
        observed_at = self.now + timedelta(microseconds=2)
        report = self.report(old, 80, five_left=0)
        report.five_hour = Quota("5-hour", 100, self.now + timedelta(minutes=19))
        initial = self.readings(old, report, self.report(target))
        readings = quota_alert.CodexAlertPassReadings(initial.workers, old, tuple(
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
            log = quota_alert.blocked([self.note(1, "active", 0)], old, self.now, codex_readings=readings)
        self.assertEqual(agent_accounts.workers_home(), old.home)
        self.assertIn("direct=claude", registry_view())
        self.assertTrue(any("workers could not move" in text and "destination unavailable" in text for text in log))
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        self.assertEqual(entries[0]["move_id"], entries[1]["move_id"])

    def test_weekly_refusal_moves_workers_to_picked_home_and_records_request_not_confirmation(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 0), self.report(target, 1))
        with (patch.object(quota_alert, "switch_to_claude") as fallback,
              patch.object(accounts, "swap", wraps=accounts.swap) as swap):
            log = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
                codex_readings=readings, window=refusals.KnownWindow("weekly"))
        fallback.assert_not_called()
        swap.assert_called_once()
        self.assertEqual(swap.call_args.args[:2], ("workers", "codex2"))
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertIn("direct=codex", registry_view())
        self.assertEqual(sum(text.startswith("workers moved from codex1 to codex2:")
                             for text in log), 1)
        held = refusals.load()[refusals.key("codex", old.login)]
        self.assertEqual(held.window, refusals.KnownWindow("weekly"))
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested"])
        self.assertEqual(entries[0]["subject"], "workers")
        self.assertTrue(entries[0]["move_id"])
        requested = entries[0]
        assert requested["kind"] == "requested"
        self.assertEqual((requested["from"], requested["to"], requested["window"]),
                         ("codex1", "codex2", "weekly"))
        assert "reset" in requested
        self.assertEqual(requested["reset"], (self.now + timedelta(days=2)).isoformat())

    def test_stale_one_percent_weekly_with_unknown_refusal_moves_instead_of_waiting(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 1, age=timedelta(minutes=11))
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 1), self.report(target, 1))
        readings = quota_alert.CodexAlertPassReadings(readings.workers, old, tuple(
            quota_alert.CodexHomeReading(item.home, item.report,
                self.now - timedelta(minutes=11) if item.home == old.home else self.now)
            for item in readings.checked_homes))
        _ = quota_alert.blocked([self.note(1, "active", 1)], old, self.now,
            codex_readings=readings, window=refusals.UnknownWindow())
        self.assertEqual(agent_accounts.workers_home(), target.home)
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
        log: list[str] = []
        for _attempt in range(2):
            log += quota_alert.blocked([self.note(1, "active", 80)], old, self.now,
                codex_readings=readings, window=refusals.KnownWindow("five_hour"))
        self.assertEqual(agent_accounts.workers_home(), old.home)
        self.assertIn("direct=codex", registry_view())
        self.assertEqual(sum(text.startswith("workers wait for codex1's 5-hour reset at ")
                             for text in log), 1)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited"])
        renewed = self.report(old, 80)
        later_readings = quota_alert.CodexAlertPassReadings(readings.workers, old,
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
                self.assertEqual(agent_accounts.workers_home(), target.home)
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
        self.assertEqual(agent_accounts.workers_home(), target.home)
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
        self.assertEqual(agent_accounts.workers_home(), old.home)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["waited"])

    def test_unknown_refusal_never_infers_a_window_from_pre_refusal_or_ambiguous_readings(self) -> None:
        old, target = self.account(1), self.account(2)
        for weekly, five, observed_at in ((80, 0, self.now - timedelta(seconds=1)),
                                         (0, 0, self.now + timedelta(seconds=1))):
            with self.subTest(weekly=weekly, observed_at=observed_at):
                self.save_room(target, 1)
                reports = self.readings(old, self.report(old, weekly, five_left=five), self.report(target, 1))
                readings = quota_alert.CodexAlertPassReadings(reports.workers, old, tuple(
                    quota_alert.CodexHomeReading(item.home, item.report,
                        observed_at if item.home == old.home else self.now)
                    for item in reports.checked_homes))
                now = max(self.now, observed_at)
                _ = quota_alert.blocked([self.note(1, "active", weekly)], old, now,
                    codex_readings=readings, window=refusals.UnknownWindow(), refused_at=quota_alert.RefusedAt(self.now))
                self.assertEqual(refusals.load()[refusals.key("codex", old.login)].window,
                                 refusals.UnknownWindow())
                self.assertEqual(agent_accounts.workers_home(), target.home)

    def test_no_room_fallback_waits_for_a_changed_reset_before_using_positive_usage(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 0)
        readings = self.readings(old, self.report(old, 0), self.report(target, 0))
        _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
            codex_readings=readings, window=refusals.KnownWindow("weekly"))
        self.assertIn("direct=claude", registry_view())
        self.assertEqual(self.relayed, [])
        self.assertTrue(any(row["kind"] == "routed to claude" for row in moves.read_journal()))
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["no room"])
        later = self.now + timedelta(minutes=1)
        same_reset = quota_alert.CodexAlertPassReadings(readings.workers, old,
            (quota_alert.CodexHomeReading(old.home, self.report(old, 80), later),))
        _ = quota_alert.alert([self.note(1, "active", 80)], later, codex_readings=same_reset)
        self.assertIn("direct=claude", registry_view())
        renewed = self.report(old, 80)
        renewed.quotas = [Quota("Weekly", 20, self.now + timedelta(days=9))]
        recovered = quota_alert.CodexAlertPassReadings(readings.workers, old,
            (quota_alert.CodexHomeReading(old.home, renewed, later + timedelta(seconds=1)),))
        _ = quota_alert.alert([self.note(1, "active", 80)], later + timedelta(seconds=1), codex_readings=recovered)
        self.assertTrue(refusals.resolved(refusals.load()[refusals.key("codex", old.login)],
            refusals.stored_reading("codex", old.login, later + timedelta(seconds=1)), later + timedelta(seconds=1)))
        self.assertIn("direct=codex", registry_view())
        self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["no room", "room again"])

    def test_zero_reading_moves_workers_to_room_before_spending_credits(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 0), self.report(target, 1))
        with patch.object(accounts, "swap", wraps=accounts.swap) as swap:
            _ = quota_alert.alert([self.note(1, "active", 0)], self.now, codex_readings=readings)
        swap.assert_called_once()
        self.assertEqual(agent_accounts.workers_home(), target.home)
        self.assertIn("direct=codex", registry_view())
        self.assertTrue(any(entry["kind"] == "requested" for entry in moves.read_moves()))
        self.assertEqual(self.pushes, [])

    def test_live_paid_only_destination_keeps_workers_on_source_credits_and_alerts_once(self) -> None:
        source, target = self.account(1), self.account(2)
        for number in (1, 2):
            note = self.note(number, "active" if number == 1 else "inactive", 0, "6370")
            _ = note.path.write_text(note.path.read_text().replace("credit_balance:",
                "use_credits: on\ncredit_balance:"))
        readings = self.readings(source, self.report(source, 0, "6370"), self.report(target, 1, "6370"))
        with (patch.object(accounts, "_codex_live", return_value=self.report(target, 0, "6370")) as live,
              patch.object(accounts, "_retire_workers_servers") as retire):
            for minute in (0, 2):
                _ = quota_alert.alert([], self.now + timedelta(minutes=minute), codex_readings=readings)
            self.assertGreater(live.call_count, 0)
            retire.assert_not_called()
        self.assertEqual(agent_accounts.workers_home(), source.home)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.pushes[0].id, "quota.paid_credits")
        self.assertEqual(self.pushes[0].values["account"], source.label)
        failures = [entry for entry in moves.read_moves() if entry["kind"] == "failed"]
        self.assertTrue(failures)
        self.assertTrue(all("no included usage left" in entry["reason"] for entry in failures))
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_failed_workers_swap_records_failure_with_request_id_and_never_confirms(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 1)
        readings = self.readings(old, self.report(old, 0), self.report(target, 1))
        with patch.object(accounts, "_codex_live", return_value=Report("Codex", problem="target unavailable")):
            _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
                codex_readings=readings, window=refusals.KnownWindow("weekly"))
        self.assertEqual(agent_accounts.workers_home(), old.home)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        self.assertEqual(entries[0]["move_id"], entries[1]["move_id"])
        failed = entries[1]
        assert failed["kind"] == "failed"
        self.assertIn("target unavailable", failed["reason"])
        self.assertIn("direct=claude", registry_view())

    def test_no_room_fallback_restores_on_another_account_when_it_regains_room(self) -> None:
        old, target = self.account(1), self.account(2)
        self.save_room(old, 0)
        self.save_room(target, 0)
        readings = self.readings(old, self.report(old, 0), self.report(target, 0))
        _ = quota_alert.blocked([self.note(1, "active", 0)], old, self.now,
            codex_readings=readings, window=refusals.KnownWindow("weekly"))
        self.assertIn("direct=claude", registry_view())
        later = self.now + timedelta(minutes=1)
        recovered = quota_alert.CodexAlertPassReadings(readings.workers, old,
            (quota_alert.CodexHomeReading(old.home, self.report(old, 0), later),
             quota_alert.CodexHomeReading(target.home, self.report(target, 1), later)))
        _ = quota_alert.alert([self.note(1, "active", 0), self.note(2, "inactive", 1)], later,
                              codex_readings=recovered)
        self.assertIn("direct=codex", registry_view())
        self.assertEqual(agent_accounts.workers_home(), target.home)
        entries = moves.read_moves()
        recovered_entries = [entry for entry in entries if entry["kind"] == "room again"]
        self.assertEqual(len(recovered_entries), 1)
        self.assertEqual(recovered_entries[0]["label"], "codex2")
        self.assertEqual(entries[0]["move_id"], recovered_entries[0]["move_id"])

    def test_low_account_phone_reminder_does_not_repeat_after_swap_away_and_back(self) -> None:
        a, b = self.account(1), self.account(2)
        a_pass = self.readings(a, self.report(a), self.report(b, 70))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 70)],
                              self.now, codex_readings=a_pass)
        self.sent.clear()
        self.now += timedelta(hours=1)
        b_pass = self.readings(b, self.report(a), self.report(b, 70))
        _ = quota_alert.alert([self.note(1, "inactive", 1), self.note(2, "active", 70)],
                              self.now, codex_readings=b_pass)
        self.assertEqual(self.sent, [])
        self.now += timedelta(hours=1)
        a_pass = self.readings(a, self.report(a), self.report(b, 70))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 70)],
                              self.now, codex_readings=a_pass)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.pushes), 1)
        with quota_alert.state_file() as state:
            self.assertNotIn("acknowledged", state["episodes"]["codex1"])

    def test_stale_active_spare_never_opens_an_episode_with_or_without_credits(self) -> None:
        a, b = self.account(1), self.account(2, default=True)
        for credits in ("0", "37"):
            with self.subTest(credits=credits):
                quota_alert.STATE.unlink(missing_ok=True)
                readings = self.readings(a, self.report(a), self.report(b, 0, credits, five_left=0))
                _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "active", 0, credits)],
                                      self.now, codex_readings=readings)
                with quota_alert.state_file() as state:
                    self.assertEqual(set(state["episodes"]), {"codex1"})

    def test_spare_episode_closes_only_when_its_own_quota_recovers(self) -> None:
        a, b = self.account(1), self.account(2)
        selected_a = self.readings(a, self.report(a))
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now, codex_readings=selected_a)
        selected_b = self.readings(b, self.report(a, 90), self.report(b, 90))
        _ = quota_alert.alert([self.note(1, "inactive", 90), self.note(2, "active", 90)],
                              self.now + timedelta(minutes=1), codex_readings=selected_b)
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state["episodes"])

    def test_spare_recommendation_uses_the_home_login_account_number_and_live_credits(self) -> None:
        a, b = self.account(1), self.account(2, default=True)
        readings = self.readings(a, self.report(a), self.report(b, 1, "17", five_left=0))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 1)],
                              self.now, codex_readings=readings)
        text = quota_alert.codex_swap_line(readings, self.now)
        self.assertIn("workers move to", text)
        self.assertNotIn("swap workers", text)
        self.assertIn("codex2", text)
        self.assertEqual(agent_accounts.workers_home(), a.home)

    def test_paid_credits_catalog_entries_use_exact_values_and_queued_advice(self) -> None:
        from push_texts import catalog, render_entry
        ids = {"quota.paid_credits", "quota.paid_credits_queued"}
        entries = [entry for entry in catalog() if entry.id in ids]
        self.assertEqual({entry.id for entry in entries}, ids)
        for entry in entries:
            for example in entry.examples:
                with self.subTest(id=entry.id, example=example.name):
                    rendered = render_entry(entry, example.values)
                    self.assertEqual((rendered.sender, rendered.need), (example.values["sender"], "decision"))
                    self.assertIsInstance(rendered.action, ActionRequired)
                    assert isinstance(rendered.action, ActionRequired)
                    self.assertIn(example.values["account"] + ".md", rendered.action.text)
                    keys = {"sender", "account", "machine", "credits", "refill"}
                    if entry.id == "quota.paid_credits":
                        keys.add("next_reset")
                        self.assertIn("Claude", rendered.body)
                    else:
                        self.assertNotIn("Claude", rendered.body)
                        self.assertIn("queued move", rendered.body)
                    self.assertEqual(set(example.values), keys)

    def test_codex_refusal_and_swap_advice_use_worker_wording_and_the_home_note_number(self) -> None:
        a, b = self.account(1), self.account(2, default=True)
        readings = self.readings(a, self.report(a, 0), self.report(b, 90))
        advice = quota_alert.codex_swap_line(readings, self.now)
        _ = quota_alert.blocked([self.note(1, "active", 0)], a, self.now,
            codex_readings=readings, window=refusals.KnownWindow("weekly"))
        text = advice + "\n" + "\n".join(self.sent)
        self.assertNotIn("default", text.lower())
        self.assertIn("workers", text)
        self.assertIn("codex2", text)

    def test_spare_with_used_up_window_and_no_credits_is_not_recommended(self) -> None:
        a, b = self.account(1), self.account(2)
        readings = self.readings(a, self.report(a), self.report(b, 80, "0", five_left=0))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 80)],
                              self.now, codex_readings=readings)
        self.assertNotIn("workers move to codex2 when Codex refuses work", quota_alert.codex_swap_line(readings, self.now))

    def test_failed_or_expired_spare_read_never_uses_saved_credits_for_advice(self) -> None:
        a, b = self.account(1), self.account(2)
        for report in (self.report(b, 90, None, failed=True), self.report(b, 90, "0", expired=True)):
            with self.subTest(problem=report.problem):
                quota_alert.STATE.unlink(missing_ok=True)
                self.sent.clear()
                readings = self.readings(a, self.report(a), report)
                _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 90, "999")],
                                      self.now, codex_readings=readings)
                self.assertNotIn("workers move to codex2 when Codex refuses work", quota_alert.codex_swap_line(readings, self.now))

    def test_unchecked_target_is_not_recommended_even_with_live_allowance(self) -> None:
        a, b = self.account(1), self.account(2)
        private_cache = b.home / "cache"
        private_cache.symlink_to(self.root, target_is_directory=True)
        readings = self.readings(a, self.report(a), self.report(b, 90))
        _ = quota_alert.alert([self.note(1, "active", 1), self.note(2, "inactive", 90)],
                              self.now, codex_readings=readings)
        self.assertNotIn("workers move to codex2 when Codex refuses work", quota_alert.codex_swap_line(readings, self.now))
        self.assertEqual(accounts.check("codex2"), [
            f"accounts: {private_cache} is a link; sign-in and home state stay in each account home",
        ])

        private_cache.unlink()

        self.assertEqual(accounts.check("codex2"), [])
        self.assertEqual(quota_alert.codex_swap_line(readings, self.now),
                         "workers move to codex2 when Codex refuses work")

    def test_unknown_selected_identity_does_not_alert_old_active_notes(self) -> None:
        a = self.account(1)
        known = self.readings(a, self.report(a))
        unknown = quota_alert.CodexAlertPassReadings(known.workers,
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
                self.assertIn("direct=codex", registry_view())

    def test_selected_refusal_owns_fallback_and_credits_restore_only_after_refusal_reset(self) -> None:
        a = self.account(1)
        readings = self.readings(a, self.report(a, 0))
        _ = quota_alert.blocked([self.note(1, "active", 0)], a, self.now, codex_readings=readings, window=refusals.KnownWindow("weekly"))
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.owner, a)
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "module_review", "codex"))
        self.now += timedelta(days=3)
        funded = self.readings(a, self.report(a, 0, "19", five_left=0))
        _ = quota_alert.alert([self.note(1, "active", 0, "19")],
                              self.now + timedelta(minutes=1), codex_readings=funded)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
        registry = registry_view()
        self.assertIn("direct=codex", registry)
        self.assertIn("module_review=codex", registry)

    def test_missing_and_old_fallback_records_decode_named_states_and_recover(self) -> None:
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
        reset_registry(registry_text({"assignments": {"direct": "claude"}}, base_text=REGISTRY))
        _ = quota_alert.STATE.write_text(json.dumps({"episodes": {}, "switch": {
            "at": self.now.isoformat(), "moved": ["direct"],
        }}))
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.UnknownOwnerCodexQuotaFallback)
        a = self.account(1)
        readings = self.readings(a, self.report(a, 90))
        _ = quota_alert.alert([self.note(1, "active", 90)], self.now, codex_readings=readings)
        self.assertIn("direct=codex", registry_view())

    def test_shared_account_identity_matches_logins_without_regard_to_case(self) -> None:
        a = self.account(1)
        path = self.root / "codex1.md"
        _ = path.write_text(path.read_text().replace(a.login, a.login.upper()))
        identity = agent_accounts.codex_account(a.home)
        self.assertIsInstance(identity, agent_accounts.KnownCodexAccount)
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        self.assertEqual((identity.home, identity.login, identity.label), (a.home, a.login, "codex1"))

    def test_selected_and_spare_readings_share_the_ten_minute_limit(self) -> None:
        a, b = self.account(1), self.account(2)
        self.save_room(a, 1)
        self.save_room(b, 80)
        readings = self.readings(a, self.report(a), self.report(b, 80))
        at_limit = self.now + timedelta(minutes=10)
        self.assertIsNone(quota_alert.selected_codex_report(readings, at_limit).problem)
        self.assertIn("workers move to codex2 when Codex refuses work", quota_alert.codex_swap_line(readings, at_limit))
        after_limit = at_limit + timedelta(microseconds=1)
        self.assertIsNotNone(quota_alert.selected_codex_report(readings, after_limit).problem)
        self.assertIn("no other Codex account has room until", quota_alert.codex_swap_line(readings, after_limit))

    def test_standalone_refusal_uses_readings_collected_after_the_call_starts(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = self.readings(a)
        async def live(home: Path) -> Report:
            return self.report(a if home == a.home else b, 0 if home == a.home else 80)
        with patch.object(quota_alert, "codex_live", side_effect=live), patch.object(quota_alert, "tell_user", wraps=self.push) as push:
            log = quota_alert.blocked([self.note(1, "active", 0)], a)
        self.assertIn("workers moved from codex1 to codex2", "\n".join(log))
        self.assertEqual(agent_accounts.workers_home(), b.home)
        push.assert_not_called()

    def test_standalone_alert_and_refresh_accept_their_new_readings(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = self.readings(a)
        note = self.note(1, "active", 1)
        async def live(home: Path) -> Report:
            return self.report(a if home == a.home else b, 1 if home == a.home else 80)
        with patch.object(quota_alert, "codex_live", side_effect=live):
            _ = quota_alert.alert([note])
            self.assertEqual([push.title for push in self.pushes], ["codex1 Codex workers will move to codex2"])
            self.assertEqual(self.relayed, [])
            quota_alert.STATE.unlink(missing_ok=True)
            self.sent.clear()
            _ = quota_alert.refresh([note], None)
            self.assertEqual(len(self.pushes), 2)
            self.assertFalse(any(text.startswith("Quota alert:") for text in self.relayed))

    def test_refusals_create_no_weekly_episodes_or_false_recovery_notices(self) -> None:
        a, b = self.account(1), self.account(2)
        for selected, left in ((a, 0), (b, 90)):
            with self.subTest(selected=selected.label):
                quota_alert.STATE.unlink(missing_ok=True)
                reset_registry(REGISTRY)
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

    def test_spare_reset_closes_the_episode_before_the_new_low_week(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now,
            codex_readings=self.readings(a, self.report(a), self.report(b, 80)))
        self.now += timedelta(days=3)
        _ = quota_alert.alert([self.note(1, "inactive", 1), self.note(2, "active", 80)], self.now,
            codex_readings=self.readings(b, self.report(a), self.report(b, 80)))
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state["episodes"])
        self.sent.clear()
        _ = quota_alert.alert([self.note(1, "active", 1)], self.now,
            codex_readings=self.readings(a, self.report(a), self.report(b, 80)))
        self.assertEqual(len(self.pushes), 2)

    def test_outgoing_refusal_records_a_note_without_user_or_session_delivery(self) -> None:
        """F008: an outgoing account's refusal is recorded in the quota log only."""
        a, b, c = self.account(1), self.account(2), self.account(3)
        readings = self.readings(b, self.report(a, 0), self.report(b, 0), self.report(c, 80))
        with patch.object(quota_alert, "tell_user", wraps=self.push) as push:
            lines = quota_alert.blocked([self.note(1, "inactive", 0)], a, self.now, codex_readings=readings)
        push.assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertIn("quota status codex1: Codex refused work on codex1, the outgoing account. Nothing switched to Claude.", lines)

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
            self.assertEqual(count, 2)
            messages = len(self.sent)
            with quota_alert.state_file() as state:
                unseen = list(state.get("unseen", []))
            _ = quota_alert.alert([self.note(1, "active", 90)], self.now, codex_readings=funded)
            self.assertEqual(edit.call_count, count * 2)
            self.assertEqual(len(self.sent), messages)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.moved, ("direct", "module_review"))
            self.assertEqual(state.get("unseen"), unseen)

    def test_unknown_refusal_logs_without_telling_any_session(self) -> None:
        a = self.account(1)
        readings = self.readings(a, self.report(a, 90))
        unknown = agent_accounts.UnknownCodexAccount(a.home, "sign-in unreadable")
        log = quota_alert.blocked([], unknown, self.now, codex_readings=readings)
        self.assertTrue(any("refusing account unknown" in line for line in log), log)
        _ = quota_alert.blocked([], unknown, self.now, codex_readings=readings)
        self.assertEqual(self.relayed, [])
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"], {})

    def test_refusal_after_credit_recovery_keeps_the_new_move_without_session_or_phone_delivery(self) -> None:
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
        self.assertIn("direct=codex", registry_view())
        self.sent.clear()
        low = self.readings(a, self.report(a, 0))
        with patch.object(quota_alert, "relay", wraps=self.relay) as relay, patch.object(
                quota_alert, "tell_user", wraps=self.push) as push:
            _ = quota_alert.blocked([note], a, self.now, codex_readings=low)
            relay.assert_not_called()
            push.assert_not_called()
            _ = quota_alert.blocked([note], a, self.now, codex_readings=low)
            relay.assert_not_called()
            push.assert_not_called()
        self.assertIn("direct=claude", registry_view())

    def test_outgoing_refusal_does_not_silence_a_later_selected_move(self) -> None:
        a, b = self.account(1), self.account(2)
        _ = quota_alert.blocked([self.note(1, "inactive", 80)], a, self.now,
            codex_readings=self.readings(b, self.report(a, 80), self.report(b, 80)))
        self.assertIn("direct=codex", registry_view())
        selected = self.readings(a, self.report(a, 0), self.report(b, 80))
        note = self.note(1, "active", 0)
        _ = quota_alert.alert([note], self.now, codex_readings=selected)
        with quota_alert.state_file() as state:
            self.assertIn("push", state.get("refusal_notices", {})[str(a.home)])
        self.sent.clear()
        with patch.object(quota_alert, "relay", wraps=self.relay) as relay, patch.object(
                quota_alert, "tell_user", wraps=self.push) as push:
            _ = quota_alert.blocked([note], a, self.now, codex_readings=selected)
            relay.assert_not_called()
            push.assert_not_called()
        self.assertIn("direct=codex", registry_view())
        self.assertEqual(agent_accounts.workers_home(), b.home)

    def test_switch_failure_phone_pushes_repeat_only_for_a_new_failure(self) -> None:
        a = self.account(1)
        readings = self.readings(a, self.report(a, 0))
        note = self.note(1, "active", 0)
        with patch.object(quota_alert, "edit_registry", side_effect=[
                "editor unavailable", "editor unavailable", "registry is read only",
                "registry is read only", "editor unavailable"]), patch.object(quota_alert, "relay", wraps=self.relay) as relay, patch.object(
                quota_alert, "tell_user", wraps=self.push) as push:
            for count, attempted_failure in ((1, "editor unavailable"), (1, "editor unavailable"),
                                        (2, "registry is read only"), (2, "registry is read only"),
                                        (2, "editor unavailable")):
                log = quota_alert.blocked([note], a, self.now, codex_readings=readings)
                relay.assert_not_called()
                self.assertEqual(push.call_count, count)
                self.assertEqual(cast(RenderedPush, push.call_args.args[0]).id, "quota.codex_refused")
                self.assertIn("the work is paused", cast(RenderedPush, push.call_args.args[0]).body)
                self.assertNotIn(attempted_failure, cast(RenderedPush, push.call_args.args[0]).body)
                self.assertTrue(any(attempted_failure in line for line in log), log)
        self.assertIn("direct=codex", registry_view())

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
