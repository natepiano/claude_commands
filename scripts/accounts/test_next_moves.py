"""Ordered next moves use an isolated log and the role-map host transport."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.accounts.home_test_support import scratch_account_home
from scripts.accounts import accounts, next_moves, refusal_watch, refusals, role_map, test_refusal_watch as watch_tests
from scripts.lib.conf import UseConfiguredValue
from scripts.lib import conf
from scripts.whoami import run_out, usage_store
from scripts.whoami import quota_alert

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


class NextMovesTests(unittest.TestCase):
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(scratch_account_home(self.home, {}, clear=True)))
        _ = self.enterContext(patch.object(quota_alert, "STATE", self.home / "quota_alert.json"))
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        _ = self.enterContext(patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unexpected ssh")))
        _ = self.enterContext(patch.object(run_out, "READINGS_LOG", self.home / "pace.jsonl"))

    @contextmanager
    def watch_fixture(self) -> Generator[watch_tests.RefusalWatchTests]:
        fixture = watch_tests.RefusalWatchTests()
        try:
            fixture.setUp()
            yield fixture
        finally:
            _ = fixture.doCleanups()

    def test_else_is_listed_and_old_entries_keep_their_format(self) -> None:
        entry = next_moves.add_next_move("showrunners", 1, "claude2", fallback_targets=("claude4",))
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        self.assertEqual(next_moves.list_next_moves(), "showrunners 1: at 1% → claude2, else claude4")
        legacy = next_moves.ClaudeGroupMove("old", "units", 1, 0, "claude2", NOW)
        document = next_moves.encode_next_moves((legacy,))
        self.assertNotIn('"else"', document)
        self.assertEqual(next_moves.decode_next_moves(document), (legacy,))
        self.assertEqual(legacy.fallback_targets, ())

    def test_workers_keep_their_existing_target_form(self) -> None:
        with self.assertRaisesRegex(next_moves.NextMoveError, "workers entries cannot use --else"):
            _ = next_moves.add_next_move("workers", 1, "codex2", fallback_targets=("claude4",))
        self.assertEqual(next_moves.read_next_moves(), ())

    def test_cli_add_and_mac_forward_keep_the_ordered_fallback(self) -> None:
        with (patch.object(accounts, "claude_account_dir", return_value=self.home / ".claude2") as checked,
              redirect_stdout(io.StringIO())):
            self.assertEqual(accounts.main(["next", "add", "showrunners", "1", "claude2", "--else", "claude4"]), 0)
        self.assertEqual([call.args[0] for call in checked.call_args_list], ["claude2", "claude4"])
        entry, = next_moves.read_next_moves()
        assert isinstance(entry, next_moves.ClaudeGroupMove)
        self.assertEqual(entry.fallback_targets, ("claude4",))

        def remote(arguments: tuple[str, ...]) -> str:
            self.assertEqual(arguments[0], "next-write")
            forwarded, = next_moves.decode_next_moves(arguments[1])
            assert isinstance(forwarded, next_moves.ClaudeGroupMove)
            self.assertEqual((forwarded.to, forwarded.fallback_targets), ("claude2", ("claude4",)))
            return arguments[1]

        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=remote)):
            forwarded = next_moves.add_next_move("showrunners", 1, "claude2", fallback_targets=("claude4",))
        self.assertEqual(next_moves.decode_next_moves(next_moves.encode_next_moves((forwarded,))), (forwarded,))
        self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_due_entry_uses_the_first_target_when_both_have_room(self) -> None:
        for group in ("units", "showrunners"):
            with self.subTest(group=group), self.watch_fixture() as fixture:
                _ = fixture.groups(showrunners="claude1")
                _ = fixture.owner("member", host="unit" if group == "units" else "herdr")
                fixture.reading(watch_tests.TARGET_LOGIN, 80)
                entry = next_moves.add_next_move(group, 1, "claude2", fallback_targets=("claude3",))
                lines = refusal_watch.run_auto_moves(watch_tests.NOW)
                self.assertTrue(any("to claude2" in line for line in lines))
                self.assertEqual([call.target for call in fixture.moves.calls], ["claude2"])
                self.assertEqual(next_moves.read_next_moves(), ())
                self.assertIn("auto: " + entry.id, accounts.roles_history_file().read_text())
                self.assertEqual(fixture.notifications, [])

    def test_due_entry_uses_else_for_each_blocked_first_target(self) -> None:
        for cause in ("low", "zero", "stale", "missing reading", "refusal", "login", "missing account"):
            with self.subTest(cause=cause), self.watch_fixture() as fixture:
                _ = fixture.groups()
                _ = fixture.owner("unit", host="unit")
                fixture.reading(watch_tests.TARGET_LOGIN, 80)
                if cause in ("low", "zero", "stale"):
                    fixture.reading("main@example.invalid", 1 if cause == "low" else 0 if cause == "zero" else 80,
                                    at=watch_tests.NOW - timedelta(minutes=11) if cause == "stale" else watch_tests.NOW)
                elif cause == "missing reading":
                    saved = usage_store.load()
                    del saved[usage_store.key("Claude", "main@example.invalid")]
                    usage_store.write(saved)
                elif cause == "refusal":
                    refusals.write_refusal(refusals.AccountRefusal("claude", "main@example.invalid",
                        refusals.UnknownWindow(), refusals.NoResetKnown(), watch_tests.NOW))
                elif cause == "login":
                    fixture.expired_login()
                entry = next_moves.add_next_move("units", 1,
                    "claude99" if cause == "missing account" else "claude2",
                                                 fallback_targets=("claude3",))
                preview = refusal_watch.run_auto_moves(watch_tests.NOW, dry_run=True)
                self.assertTrue(any("would move from claude1 to claude3" in line for line in preview))
                self.assertEqual(next_moves.read_next_moves(), (entry,))
                self.assertEqual(fixture.moves.calls, [])
                _ = refusal_watch.run_auto_moves(watch_tests.NOW)
                self.assertEqual([call.target for call in fixture.moves.calls], ["claude3"])
                self.assertEqual(next_moves.read_next_moves(), ())
                self.assertEqual(fixture.notifications, [])

    def test_current_account_target_consumes_the_entry_without_a_move(self) -> None:
        for target, fallbacks in (("claude1", ()), ("claude1", ("claude3",)), ("claude2", ("claude1",))):
            with self.subTest(target=target, fallbacks=fallbacks), self.watch_fixture() as fixture:
                _ = fixture.groups()
                fixture.reading("main@example.invalid", 0)
                entry = next_moves.add_next_move("units", 1, target, fallback_targets=fallbacks)
                self.assertEqual(refusal_watch.run_auto_moves(watch_tests.NOW, dry_run=True), ["units: unchanged"])
                self.assertEqual(next_moves.read_next_moves(), (entry,))
                self.assertEqual(refusal_watch.run_auto_moves(watch_tests.NOW), ["units: unchanged"])
                self.assertEqual(next_moves.read_next_moves(), ())
                self.assertEqual(fixture.moves.calls, [])
                self.assertEqual(fixture.notifications, [])

    def test_due_entry_stays_queued_when_both_targets_are_blocked(self) -> None:
        with self.watch_fixture() as fixture:
            _ = fixture.groups()
            fixture.reading("main@example.invalid", 0)
            fixture.reading(watch_tests.TARGET_LOGIN, 0)
            entry = next_moves.add_next_move("units", 1, "claude2", fallback_targets=("claude3",))
            for _ in range(2):
                lines = refusal_watch.run_auto_moves(watch_tests.NOW)
                self.assertEqual(len(lines), 1)
                self.assertIn("claude2", lines[0])
                self.assertIn("claude3", lines[0])
            self.assertEqual(len(fixture.notifications), 1)
            fixture.reading(watch_tests.TARGET_LOGIN, 80, at=watch_tests.NOW - timedelta(minutes=11))
            _ = refusal_watch.run_auto_moves(watch_tests.NOW)
            self.assertEqual(len(fixture.notifications), 2)
            self.assertEqual(next_moves.read_next_moves(), (entry,))
            self.assertEqual(fixture.moves.calls, [])

    def test_fallback_hold_uses_the_selected_account_and_keeps_its_clock(self) -> None:
        with self.watch_fixture() as fixture:
            _ = fixture.groups()
            fixture.reading("main@example.invalid", 0)
            fixture.reading(watch_tests.TARGET_LOGIN, 80)
            entry = next_moves.add_next_move("units", 1, "claude2", next_moves.HalfPointHold(),
                                             fallback_targets=("claude3",))
            with (patch.object(next_moves, "account_pace", return_value=next_moves.PaceEstimate(2)) as pace,
                  patch.object(next_moves, "render_time", return_value=watch_tests.NOW)):
                self.assertIn("else claude3, hold half a point (≈15 min)", next_moves.list_next_moves())
                pace.assert_called_once_with("claude1", watch_tests.NOW)
                pace.reset_mock()
                _ = refusal_watch.run_auto_moves(watch_tests.NOW)
                pace.assert_called_once_with("claude1", watch_tests.NOW)
                _ = refusal_watch.run_auto_moves(watch_tests.NOW + timedelta(minutes=1))
                self.assertEqual(pace.call_count, 1)
            self.assertEqual(next_moves.read_next_moves(), (entry,))
            later = watch_tests.NOW + timedelta(minutes=15)
            fixture.reading(watch_tests.SOURCE_LOGIN, 0, at=later)
            fixture.reading("main@example.invalid", 0, at=later)
            fixture.reading(watch_tests.TARGET_LOGIN, 80, at=later)
            _ = refusal_watch.run_auto_moves(later)
            self.assertEqual(next_moves.read_next_moves(), ())
            self.assertEqual(accounts.group_label(accounts.group_accounts(), "units"), "claude3")

    def test_replay_repairs_a_fallback_selection_before_consuming_the_entry(self) -> None:
        with self.watch_fixture() as fixture:
            _ = fixture.groups()
            entry = next_moves.add_next_move("units", 1, "claude2", fallback_targets=("claude3",))
            _ = accounts.set_group_account("units", "claude3", why="auto: " + entry.id,
                                           now=watch_tests.NOW, origin=accounts.AutoOrigin())
            accounts.roles_history_file().unlink()
            _ = refusal_watch.run_auto_moves(watch_tests.NOW)
            self.assertEqual(next_moves.read_next_moves(), ())
            self.assertIn("auto: " + entry.id, accounts.roles_history_file().read_text())
            self.assertEqual(fixture.moves.calls, [])

    def test_add_and_remove_keep_entry_order_and_preserve_the_other_groups_plan(self) -> None:
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertFalse(next_moves.next_moves_file().exists())
        first = next_moves.add_next_move("units", 1, "claude4", 5)
        second = next_moves.add_next_move("units", 1, "claude2", 5)
        runner = next_moves.add_next_move("showrunners", 2, "claude2")
        self.assertEqual(next_moves.read_next_moves(), (first, second, runner))
        self.assertNotEqual(first.id, second.id)
        next_moves.remove_next_move("units", next_moves.GroupPosition(2))
        self.assertEqual(next_moves.read_next_moves(), (first, runner))
        before = next_moves.next_moves_file().read_bytes()
        with self.assertRaises(next_moves.NextMoveError):
            next_moves.remove_next_move("units", next_moves.GroupPosition(2))
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        next_moves.remove_next_move("units", next_moves.GroupPosition(1))
        next_moves.remove_next_move("showrunners", next_moves.GroupPosition(1))
        self.assertEqual(next_moves.read_next_moves(), ())

    def test_group_positions_are_one_based_and_out_of_range_edits_preserve_the_log(self) -> None:
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(next_moves.NextMoveError):
                _ = next_moves.GroupPosition(value)
        entry = next_moves.add_next_move("units", 1, "claude2")
        before = next_moves.next_moves_file().read_bytes()
        with self.assertRaises(next_moves.GroupPositionOutOfRange) as error:
            next_moves.move_next_move("units", next_moves.GroupPosition(1), next_moves.GroupPosition(2))
        self.assertEqual((error.exception.group, error.exception.requested, error.exception.size),
                         ("units", next_moves.GroupPosition(2), 1))
        with self.assertRaises(next_moves.GroupPositionOutOfRange) as error:
            _ = next_moves.add_next_move("units", 1, "claude3",
                insertion=next_moves.InsertAt(next_moves.GroupPosition(3)))
        self.assertEqual((error.exception.group, error.exception.requested, error.exception.size),
                         ("units", next_moves.GroupPosition(3), 1))
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)

    def test_positional_edits_preserve_surviving_ids_and_the_other_groups_interleaved_order(self) -> None:
        entries: tuple[next_moves.NextMove, ...] = (
            next_moves.ClaudeGroupMove("unit-one", "units", 1, 5, "claude2", NOW),
            next_moves.WorkersMove("worker-one", 1, next_moves.WorkersToClaude(), NOW),
            next_moves.ClaudeGroupMove("runner-one", "showrunners", 1, 0, "claude3", NOW),
            next_moves.ClaudeGroupMove("unit-two", "units", 2, 5, "claude3", NOW),
            next_moves.ClaudeGroupMove("runner-two", "showrunners", 2, 0, "claude2", NOW),
            next_moves.WorkersMove("worker-two", 2, next_moves.CodexWorkersTarget("codex2"), NOW))
        groups: tuple[next_moves.NextMoveGroup, ...] = ("showrunners", "units", "workers")
        for group in groups:
            with self.subTest(group=group):
                with next_moves.next_moves_lock():
                    next_moves.write_next_moves(entries)
                own = [entry for entry in entries if entry.group == group]
                others = [entry for entry in entries if entry.group != group]
                next_moves.move_next_move(group, next_moves.GroupPosition(2), next_moves.GroupPosition(1))
                changed = next_moves.read_next_moves()
                self.assertEqual([entry for entry in changed if entry.group == group], list(reversed(own)))
                self.assertEqual([entry for entry in changed if entry.group != group], others)
                self.assertEqual({entry.id for entry in changed}, {entry.id for entry in entries})
                inserted = next_moves.add_next_move(group, 3, "claude" if group == "workers" else "claude4",
                    insertion=next_moves.InsertAt(next_moves.GroupPosition(2)))
                changed = next_moves.read_next_moves()
                self.assertEqual([entry for entry in changed if entry.group == group], [own[1], inserted, own[0]])
                self.assertEqual([entry for entry in changed if entry.group != group], others)
                next_moves.remove_next_move(group, next_moves.GroupPosition(3))
                changed = next_moves.read_next_moves()
                self.assertEqual([entry for entry in changed if entry.group == group], [own[1], inserted])
                self.assertEqual([entry for entry in changed if entry.group != group], others)
                self.assertEqual({entry.id for entry in changed},
                                 ({entry.id for entry in entries} - {own[0].id}) | {inserted.id})
                appended = next_moves.add_next_move(group, 4, "claude" if group == "workers" else "claude4",
                    insertion=next_moves.AppendToGroup())
                self.assertEqual([entry for entry in next_moves.read_next_moves() if entry.group == group],
                                 [own[1], inserted, appended])

    def test_remote_position_verbs_forward_group_and_positions_without_reading_the_queue(self) -> None:
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", return_value="") as remote):
            next_moves.move_next_move("showrunners", next_moves.GroupPosition(2), next_moves.GroupPosition(1))
            remote.assert_called_once_with(("next", "move", "showrunners", "2", "1"))
            remote.reset_mock()
            next_moves.remove_next_move("workers", next_moves.GroupPosition(3))
            remote.assert_called_once_with(("next", "remove", "workers", "3"))
        self.assertFalse(next_moves.next_moves_file().exists())

    def test_remote_insert_sends_a_position_resolved_by_the_host_instead_of_a_replacement_log(self) -> None:
        def remote(arguments: tuple[str, ...]) -> str:
            self.assertEqual(arguments[0], "next-write")
            self.assertEqual(arguments[2:], ("--at", "2"))
            entry, = next_moves.decode_next_moves(arguments[1])
            self.assertEqual((entry.group, next_moves.target_label(entry)), ("workers", "claude"))
            return next_moves.encode_next_moves((entry,))

        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=remote) as transport):
            _ = next_moves.add_next_move("workers", 1, "claude",
                insertion=next_moves.InsertAt(next_moves.GroupPosition(2)))
        transport.assert_called_once()
        self.assertFalse(next_moves.next_moves_file().exists())

    def test_remote_read_uses_host_log_without_creating_a_local_copy(self) -> None:
        document = json.dumps([{"id": "remote-entry", "group": "units", "at_percent": 1,
            "hold_minutes": 5, "to": "claude2", "added_at": NOW.isoformat()}])
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", return_value=document) as remote):
            entries = next_moves.read_next_moves()
        self.assertEqual(entries, (next_moves.ClaudeGroupMove("remote-entry", "units", 1, 5, "claude2", NOW),))
        remote.assert_called_once_with(("next-read",))
        self.assertFalse(next_moves.next_moves_file().exists())

    def test_workers_entries_round_trip_alongside_claude_entries_and_remove_independently(self) -> None:
        unit = next_moves.add_next_move("units", 1, "claude4", 5)
        codex = next_moves.add_next_move("workers", 1, "codex2")
        claude = next_moves.add_next_move("workers", 0, "claude")
        self.assertIsInstance(codex, next_moves.WorkersMove)
        self.assertIsInstance(claude, next_moves.WorkersMove)
        assert isinstance(codex, next_moves.WorkersMove)
        assert isinstance(claude, next_moves.WorkersMove)
        self.assertEqual(codex.to, next_moves.CodexWorkersTarget("codex2"))
        self.assertEqual(claude.to, next_moves.WorkersToClaude())
        entries = next_moves.read_next_moves()
        self.assertEqual(entries, (unit, codex, claude))
        self.assertIn("workers 1: at 1% → codex2", next_moves.render_next_move(codex))
        self.assertIn("workers 1: at 0% → claude", next_moves.render_next_move(claude))
        stored = cast(list[dict[str, object]], json.loads(next_moves.encode_next_moves(entries)))
        self.assertEqual([row["to"] for row in stored], ["claude4", "codex2", "claude"])
        self.assertNotIn("hold_minutes", stored[1])
        next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
        self.assertEqual(next_moves.read_next_moves(), (unit, claude))

    def test_numbered_claude_workers_target_survives_storage_and_listing(self) -> None:
        entry = next_moves.add_next_move("workers", 1, "claude4")
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        assert isinstance(entry, next_moves.WorkersMove)
        self.assertEqual(entry.to, next_moves.WorkersToClaude(next_moves.ClaudeWorkersTarget("claude4")))
        self.assertIn("workers 1: at 1% → claude4", next_moves.list_next_moves())

    def test_workers_hold_and_wrong_tool_targets_are_refused_without_changing_the_log(self) -> None:
        entry = next_moves.add_next_move("units", 1, "claude4")
        for target, hold in (("codex2", 5), ("claude", 1), ("claude0", 0), ("codex0", 0)):
            with self.subTest(target=target, hold=hold):
                with self.assertRaises(next_moves.NextMoveError):
                    _ = next_moves.add_next_move("workers", 1, target, hold)
                self.assertEqual(next_moves.read_next_moves(), (entry,))
        document = [{"id": "workers-held", "group": "workers", "at_percent": 1,
                     "hold_minutes": 1, "to": "claude", "added_at": NOW.isoformat()}]
        with self.assertRaises(next_moves.NextMoveError):
            _ = next_moves.decode_next_moves(json.dumps(document))

    def test_remote_workers_add_and_read_preserve_target_variants_without_local_log(self) -> None:
        for target in ("codex2", "claude", "claude4"):
            with self.subTest(target=target):
                def remote(arguments: tuple[str, ...]) -> str:
                    self.assertEqual(arguments[0], "next-write")
                    received = next_moves.decode_next_moves(arguments[1])
                    self.assertIsInstance(received[0], next_moves.WorkersMove)
                    self.assertEqual(next_moves.target_label(received[0]), target)
                    return arguments[1]

                with (patch.object(role_map, "is_role_map_host", return_value=False),
                      patch.object(role_map, "ssh_role_map", side_effect=remote)):
                    entry = next_moves.add_next_move("workers", 1, target)
                self.assertIsInstance(entry, next_moves.WorkersMove)
                self.assertFalse(next_moves.next_moves_file().exists())

    def test_remote_failure_refuses_instead_of_reading_a_different_local_plan(self) -> None:
        local = next_moves.add_next_move("units", 1, "claude2")
        before = next_moves.next_moves_file().read_bytes()
        with (patch.object(role_map, "is_role_map_host", return_value=False),
              patch.object(role_map, "ssh_role_map", side_effect=role_map.RoleMapUnavailable("host unavailable"))):
            with self.assertRaises((next_moves.NextMoveError, role_map.RoleMapUnavailable)):
                _ = next_moves.read_next_moves()
        self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
        self.assertEqual(next_moves.read_next_moves(), (local,))

    def test_invalid_log_is_refused_without_rewriting_it(self) -> None:
        entry = {"id": "entry", "group": "units", "at_percent": 1, "hold_minutes": 10,
                 "to": "claude2", "added_at": NOW.isoformat()}
        cases: tuple[object, ...] = (
            {}, [{**entry, "group": "workers"}], [{**entry, "at_percent": True}],
            [{**entry, "at_percent": 101}], [{**entry, "hold_minutes": -1}],
            [{**entry, "hold_minutes": 10 ** 400}], [{**entry, "added_at": "2026-10-17T00:00:00"}],
            [entry, entry],
        )
        path = next_moves.next_moves_file()
        path.parent.mkdir(parents=True)
        for value in cases:
            with self.subTest(value=value):
                original = json.dumps(value)
                _ = path.write_text(original)
                with self.assertRaises(next_moves.NextMoveError):
                    _ = next_moves.read_next_moves()
                self.assertEqual(path.read_text(), original)

    def test_started_rows_show_stored_minutes_source_and_rounded_time_remaining(self) -> None:
        half = next_moves.ClaudeGroupMove("half", "units", 1.234567, next_moves.HalfPointHold(), "claude4", NOW)
        fixed = next_moves.ClaudeGroupMove("fixed", "units", 1, 30, "claude4", NOW)
        cases = (
            (half, next_moves.ResolvedHold(34, next_moves.PaceEstimate(0.901)),
             "units 1: at 1.234567% → claude4, hold half a point (34 min at 0.9 points/hour, 12 min left)"),
            (half, next_moves.ResolvedHold(40, next_moves.NoUsablePace()),
             "units 1: at 1.234567% → claude4, hold half a point (40 min default: no pace history, 18 min left)"),
            (fixed, next_moves.ResolvedHold(30, next_moves.FixedHold(30)),
             "units 1: at 1% → claude4, hold 30 min (8 min left)"))
        for entry, duration, expected in cases:
            with (self.subTest(source=duration.source),
                  patch.object(next_moves, "render_time", return_value=NOW + timedelta(minutes=22, seconds=1)) as clock,
                  patch.object(accounts, "group_half_point_hold", side_effect=AssertionError("started hold read fresh pace"))):
                self.assertEqual(next_moves.render_next_move(entry,
                    started_hold=next_moves.StartedHold(NOW, duration)), expected)
                clock.assert_called_once_with()

    def test_started_hold_at_or_past_its_deadline_reads_hold_over(self) -> None:
        entry = next_moves.ClaudeGroupMove("half", "units", 1, next_moves.HalfPointHold(), "claude4", NOW)
        held = next_moves.StartedHold(NOW, next_moves.ResolvedHold(30, next_moves.PaceEstimate(1)))
        for elapsed, remaining in ((timedelta(minutes=29, seconds=59), "1 min left"),
                                   (timedelta(minutes=30), "hold over"),
                                   (timedelta(minutes=31), "hold over")):
            with self.subTest(elapsed=elapsed), patch.object(next_moves, "render_time", return_value=NOW + elapsed):
                self.assertEqual(next_moves.render_next_move(entry, started_hold=held),
                    f"units 1: at 1% → claude4, hold half a point (30 min at 1 points/hour, {remaining})")

    def test_unstarted_rows_keep_the_current_estimate_and_fixed_hold_forms(self) -> None:
        half = next_moves.ClaudeGroupMove("half", "units", 1, next_moves.HalfPointHold(), "claude4", NOW)
        for duration, expected in ((next_moves.ResolvedHold(30, next_moves.PaceEstimate(1)), "≈30 min"),
                                   (next_moves.ResolvedHold(40, next_moves.NoUsablePace()),
                                    "40 min default: no pace history")):
            with (self.subTest(source=duration.source), patch.object(next_moves, "render_time", return_value=NOW),
                  patch.object(accounts, "group_half_point_hold", return_value=duration) as estimate):
                self.assertEqual(next_moves.render_next_move(half, started_hold=next_moves.HoldNotStarted()),
                                 f"units 1: at 1% → claude4, hold half a point ({expected})")
                estimate.assert_called_once_with("units", NOW)
        fixed = next_moves.ClaudeGroupMove("fixed", "units", 1, 30, "claude4", NOW)
        self.assertEqual(next_moves.render_next_move(fixed, started_hold=next_moves.HoldNotStarted()),
                         "units 1: at 1% → claude4, hold 30 min")

    def test_listed_percent_round_trips_for_claude_and_workers_entries(self) -> None:
        for percent, displayed in ((1.234567, "1.234567"), (1, "1"), (0, "0"), (100, "100"),
                                   (1.0000000000000002, "1.0000000000000002"),
                                   (99.99999999999999, "99.99999999999999"), (5e-324, "5e-324")):
            entries: tuple[next_moves.NextMove, ...] = (
                next_moves.ClaudeGroupMove("unit", "units", percent, 5, "claude2", NOW),
                next_moves.ClaudeGroupMove("runner", "showrunners", percent, 5, "claude3", NOW),
                next_moves.WorkersMove("worker", percent, next_moves.WorkersToClaude(), NOW))
            with self.subTest(percent=percent):
                for line in next_moves.render_next_moves(entries).splitlines():
                    if line:
                        token = line.split("at ", 1)[1].split("%", 1)[0]
                        self.assertEqual(token, displayed)
                        self.assertEqual(float(token), percent)

    def test_half_point_and_legacy_fixed_entries_round_trip_without_losing_the_mode(self) -> None:
        half = next_moves.ClaudeGroupMove("half", "units", 1, next_moves.HalfPointHold(), "claude2", NOW)
        fixed = next_moves.ClaudeGroupMove("fixed", "showrunners", 1, next_moves.FixedHold(17), "claude3", NOW)
        encoded = next_moves.encode_next_moves((half, fixed))
        self.assertEqual(next_moves.decode_next_moves(encoded), (half, fixed))
        legacy = json.dumps([{"id": "legacy", "group": "units", "at_percent": 1,
                              "hold_minutes": 9, "to": "claude2", "added_at": NOW.isoformat()}])
        entry, = next_moves.decode_next_moves(legacy)
        assert isinstance(entry, next_moves.ClaudeGroupMove)
        self.assertEqual(entry.hold_mode, next_moves.FixedHold(9))
        for group, mode in (("units", "unknown-future-mode"), ("workers", "half-point")):
            document = json.dumps([{"id": "invalid", "group": group, "at_percent": 1,
                "hold_mode": mode, "to": "claude" if group == "workers" else "claude2",
                "added_at": NOW.isoformat()}])
            with self.subTest(group=group, mode=mode), self.assertRaises(next_moves.NextMoveError):
                _ = next_moves.decode_next_moves(document)

    def test_half_point_duration_keeps_the_callers_settings_after_a_live_change(self) -> None:
        account_config = conf.read("accounts")
        path = conf.path("accounts")
        contents = path.read_text()
        if not path.write_text(contents.replace("hold_default_minutes=40", "hold_default_minutes=99")):
            self.fail("The hold configuration fixture is empty")
        with (patch.object(next_moves, "account_pace", return_value=next_moves.NoUsablePace()),
              patch.object(conf, "read", side_effect=AssertionError("hold read the configuration again"))):
            resolved = next_moves.resolve_hold(next_moves.HalfPointHold(), "claude1", NOW,
                                                account_config=account_config)
        self.assertEqual(resolved, next_moves.ResolvedHold(40, next_moves.NoUsablePace()))

    def test_half_point_hold_reads_current_settings_for_the_named_request(self) -> None:
        with (patch.object(conf, "read", wraps=conf.read) as reads,
              patch.object(next_moves, "account_pace", return_value=next_moves.NoUsablePace())):
            resolved = next_moves.resolve_hold(next_moves.HalfPointHold(), "claude1", NOW,
                                                account_config=UseConfiguredValue.CURRENT)
        self.assertEqual(resolved, next_moves.ResolvedHold(40, next_moves.NoUsablePace()))
        reads.assert_called_once_with("accounts")

    def test_half_point_duration_uses_selected_accounts_pace_in_points_per_second(self) -> None:
        path = self.home / "pace.jsonl"
        _ = path.write_text("\n".join(json.dumps({"account": label, "at": NOW.isoformat(), "remaining": left})
                                    for label, left in (("claude1", 1), ("claude2", 80))))
        readings = run_out.read_readings(path)
        with (patch.object(run_out, "READINGS_LOG", path),
              patch.object(run_out, "weighted_rate", return_value=1 / 3600) as pace):
            resolved = next_moves.resolve_hold(next_moves.HalfPointHold(), "claude1", NOW)
        pace.assert_called_once_with(readings["claude1"], NOW.timestamp())
        self.assertEqual(resolved, next_moves.ResolvedHold(30, next_moves.PaceEstimate(1)))
        with (patch.object(run_out, "READINGS_LOG", path),
              patch.object(run_out, "weighted_rate", return_value=0.7 / 3600)):
            self.assertEqual(next_moves.resolve_hold(next_moves.HalfPointHold(), "claude1", NOW).minutes, 43)

    def test_unusable_pace_uses_the_named_default_and_fixed_holds_do_not_read_history(self) -> None:
        for rate in (None, 0, -1):
            with (self.subTest(rate=rate), patch.object(run_out, "READINGS_LOG", self.home / "missing-history"),
                  patch.object(run_out, "weighted_rate", return_value=rate)):
                self.assertEqual(next_moves.resolve_hold(next_moves.HalfPointHold(), "claude1", NOW),
                                 next_moves.ResolvedHold(40, next_moves.NoUsablePace()))
        with patch.object(run_out, "read_readings", side_effect=AssertionError("fixed hold read pace")):
            self.assertEqual(next_moves.resolve_hold(next_moves.FixedHold(7.5), "claude1", NOW),
                             next_moves.ResolvedHold(7.5, next_moves.FixedHold(7.5)))
