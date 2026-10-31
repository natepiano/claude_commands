"""Head facts use admission records and complete local bump-log lines."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import unittest
from typing import cast
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.build_queue import head_facts
from scripts.build_queue.queue_fixture import (
    AdmissionLogTests, bump_line as bump_line, head_reading, queue_read_fixture as queue_read_fixture,
    queue_time_line as queue_time_line, token_gap_line as token_gap_line,
    write_bumps as write_bumps, write_lines, write_token_gaps as write_token_gaps, with_waiting_process,
)
from scripts.buildlog import store
from scripts.lint import memory_admit as gate

NOW = datetime(2026, 10, 30, 11, 0, tzinfo=UTC).timestamp()
ZONE = ZoneInfo("America/New_York")


def write_bumps_with_block_edge(line: bytes, split_at: int) -> None:
    """Put the first reverse-read block edge at a specified byte within a bump."""
    newer = bump_line(NOW - 1)
    prefix, suffix = b'{"kind":"step","padding":"', b'"}\n'
    padding = head_facts.BLOCK_BYTES - (len(line) - split_at) - len(newer) - len(prefix) - len(suffix)
    if padding < 0:
        raise ValueError("bump fixture is too large for one block")
    write_bumps(NOW, [line, prefix + b"x" * padding + suffix, newer])


class HeadFactsTests(AdmissionLogTests):
    def facts(self, reading: gate.QueueRead) -> head_facts.HeadFacts:
        result = head_facts.read_head_facts(reading)
        self.assertIsInstance(result, head_facts.HeadFacts)
        if not isinstance(result, head_facts.HeadFacts):
            self.fail("the locked reading has a head")
        return result

    def history(self, reading: gate.QueueRead) -> head_facts.BumpHistory:
        result = self.facts(reading).bumps
        self.assertIsInstance(result, head_facts.BumpHistory)
        if not isinstance(result, head_facts.BumpHistory):
            self.fail(f"complete fixture log must be readable: {result}")
        return result

    def test_bumps_include_join_and_now_but_exclude_earlier_and_future_rows(self) -> None:
        since = NOW - 600
        write_bumps(NOW, [bump_line(since - 0.001), bump_line(since),
                          bump_line(NOW), bump_line(NOW + 0.001)])
        history = self.history(head_reading(since=since))
        self.assertEqual(history.total, 2)
        self.assertEqual(len(history.groups), 1)
        self.assertEqual(history.groups[0].last_at, NOW)

    def test_scan_stops_at_the_first_older_bump_before_malformed_history(self) -> None:
        write_bumps(NOW, [b'{"kind":"queue_bump",broken}\n',
                          bump_line(NOW - 601), bump_line(NOW - 5)])
        self.assertEqual(self.history(head_reading()).total, 1)

    def test_older_bump_stops_scan_before_the_byte_limit(self) -> None:
        padding = b'{"kind":"step","padding":"' + b"x" * (33 * 1024 * 1024) + b'"}\n'
        write_bumps(NOW, [padding, bump_line(NOW - 601), bump_line(NOW)])
        self.assertEqual(self.history(head_reading()).total, 1)

    def test_month_boundary_reads_both_months_and_only_the_local_host(self) -> None:
        now = datetime(2026, 11, 1, 0, 0, 30, tzinfo=UTC).timestamp()
        since = now - 60
        write_bumps(since, [bump_line(since)])
        write_bumps(now, [bump_line(now)])
        other = self.root / "another-host" / "2026-11.jsonl"
        other.parent.mkdir()
        write_lines(other, [bump_line(now)])
        self.assertEqual(self.history(head_reading(since=since, now=now)).total, 2)

    def test_missing_month_files_are_known_empty_history(self) -> None:
        since = datetime(2026, 8, 31, 23, 59, tzinfo=UTC).timestamp()
        history = self.history(head_reading(since=since))
        self.assertEqual((history.total, history.groups), (0, ()))
        self.assertEqual(head_facts.bump_text(self.facts(head_reading(since=since)), ZONE, short=True),
                         "no bumps since it joined")

    def test_missing_middle_month_does_not_hide_rows_in_earlier_month(self) -> None:
        since = datetime(2026, 8, 31, 23, 59, tzinfo=UTC).timestamp()
        write_bumps(since, [bump_line(since)])
        write_bumps(NOW, [bump_line(NOW)])
        self.assertEqual(self.history(head_reading(since=since)).total, 2)

    def test_torn_last_line_is_ignored_until_a_newline_publishes_it(self) -> None:
        write_bumps(NOW, [bump_line(NOW - 2)], tail=bump_line(NOW - 1).rstrip(b"\n"))
        path = store.host_file(store.host_name(), NOW)
        self.assertEqual(self.history(head_reading()).total, 1)
        with path.open("ab") as output:
            self.assertEqual(output.write(b"\n"), 1)
        self.assertEqual(self.history(head_reading()).total, 2)

    def test_a_bump_line_crossing_a_one_mib_block_boundary_counts_once(self) -> None:
        line = bump_line(NOW - 2)
        split_at = line.index(b"queue_bump") + 5
        write_bumps_with_block_edge(line, split_at)
        path = store.host_file(store.host_name(), NOW)
        self.assertEqual(path.stat().st_size - head_facts.BLOCK_BYTES, split_at)
        self.assertEqual(self.history(head_reading()).total, 2)

    def test_a_bump_utf8_character_crossing_a_block_boundary_is_decoded_once(self) -> None:
        line = bump_line(NOW - 2, session="café-session")
        split_at = line.index("é".encode()) + 1
        write_bumps_with_block_edge(line, split_at)
        path = store.host_file(store.host_name(), NOW)
        self.assertEqual(path.stat().st_size - head_facts.BLOCK_BYTES, split_at)
        self.assertEqual(line[split_at - 1:split_at + 1], "é".encode())
        history = self.history(head_reading())
        self.assertEqual(history.total, 2)
        self.assertEqual({group.session for group in history.groups}, {"café-session", "bumped-session"})

    def test_non_bump_rows_need_no_json_or_former_field_mapping(self) -> None:
        write_bumps(NOW, [b"invalid non-bump line\n", b'{"kind":"call",broken}\n', bump_line(NOW)])
        path = store.host_file(store.host_name(), NOW)
        before = path.read_bytes()
        with mock.patch("sqlite3.connect", side_effect=AssertionError("no index")):
            self.assertEqual(self.history(head_reading()).total, 1)
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_bump_line_discards_a_newer_partial_count(self) -> None:
        for malformed in (b'{"kind":"queue_bump",broken}\n',
                          b'{"kind":"queue_bump","at":"not-a-time"}\n',
                          b'{"kind":"queue_bump"}\n'):
            with self.subTest(line=malformed):
                write_bumps(NOW, [malformed, bump_line(NOW)])
                facts = self.facts(head_reading())
                self.assertIsInstance(facts.bumps, head_facts.BumpsUnavailable)
                self.assertRegex(head_facts.bump_text(facts, ZONE),
                                 r"^Bumps since it joined: unknown \(.+\)$")
                self.assertEqual(head_facts.bump_text(facts, ZONE, short=True), "bumps: unknown")

    def test_read_error_discards_newer_months_partial_count(self) -> None:
        since = datetime(2026, 9, 30, 23, 59, tzinfo=UTC).timestamp()
        write_bumps(NOW, [bump_line(NOW)])
        store.host_file(store.host_name(), since).mkdir()
        facts = self.facts(head_reading(since=since))
        self.assertIsInstance(facts.bumps, head_facts.BumpsUnavailable)
        self.assertIn("unknown", head_facts.bump_text(facts, ZONE))

    def test_thirty_two_mib_limit_discards_partial_count(self) -> None:
        padding = b'{"kind":"step","padding":"' + b"x" * (33 * 1024 * 1024) + b'"}\n'
        write_bumps(NOW, [padding, bump_line(NOW)])
        facts = self.facts(head_reading())
        self.assertIsInstance(facts.bumps, head_facts.BumpsUnavailable)
        self.assertIn("unknown", head_facts.bump_text(facts, ZONE))
        self.assertEqual(head_facts.MAX_SCAN_BYTES, 32 * 1024 * 1024)
        self.assertEqual(head_facts.BLOCK_BYTES, 1024 * 1024)

    def test_groups_use_all_three_recorded_identity_fields_and_count_then_last_order(self) -> None:
        write_bumps(NOW, [
            bump_line(NOW - 9, session="one", folder="/tmp/a", actor="old-actor"),
            bump_line(NOW - 8, session="two", folder="/tmp/a"),
            bump_line(NOW - 7, session="two", folder="/tmp/a"),
            bump_line(NOW - 6, session="one", step="clippy", folder="/tmp/a"),
            bump_line(NOW - 5, session="one", folder="/tmp/b"),
            bump_line(NOW - 4, session="one", folder="/tmp/a", actor="new-actor"),
        ])
        history = self.history(head_reading())
        self.assertEqual(history.total, 6)
        self.assertEqual([(row.session, row.step, row.folder, row.count, row.last_at)
                          for row in history.groups], [
            ("one", "check", "/tmp/a", 2, NOW - 4),
            ("two", "check", "/tmp/a", 2, NOW - 7),
            ("one", "check", "/tmp/b", 1, NOW - 5),
            ("one", "clippy", "/tmp/a", 1, NOW - 6),
        ])
        self.assertEqual(set(history.groups[0].actors), {"old-actor", "new-actor"})

    def test_reader_consumes_the_locked_decisions_without_another_read_or_order_calculation(self) -> None:
        reading = head_reading(fields={"since": NOW - 9000, "priority_since": NOW - 50})
        write_bumps(NOW, [bump_line(NOW - 800), bump_line(NOW - 5)])
        with mock.patch.object(gate, "locked_queue_read", side_effect=AssertionError("one locked pass")), \
             mock.patch.object(gate, "head_window", side_effect=AssertionError("admission owns windows")), \
             mock.patch.object(gate, "head_priority", side_effect=AssertionError("admission owns priority")), \
             mock.patch.object(gate, "waiter_order", side_effect=AssertionError("admission owns order")), \
             mock.patch.object(gate, "build_budget_bytes", side_effect=AssertionError("snapshot budget")), \
             mock.patch.object(gate, "read_meminfo", side_effect=AssertionError("snapshot free memory")):
            facts = self.facts(reading)
        self.assertEqual(facts.joined_at, NOW - 600)
        self.assertEqual(facts.bumps, head_facts.BumpHistory((head_facts.BumpGroup(
            "bumped-session", "check", "/tmp/bumped", 1, ("actor-session",), NOW - 5),), 1,
            (head_facts.BumpRecord(NOW - 5, "actor-session", "bumped-session", "check", "/tmp/bumped", 3),)))

    def test_total_wait_ignores_every_legacy_head_clock(self) -> None:
        for fields in cast(tuple[dict[str, object], ...], ({}, {"head_state": "head", "head_since": NOW - 125},
                       {"head_state": "not_head"}, {"head_since": float("inf")})):
            with self.subTest(fields=fields):
                reading = head_reading(since=NOW - 158 * 60, fields=fields)
                facts = self.facts(reading)
                self.assertEqual(facts.joined_at, NOW - 158 * 60)
                self.assertNotIn("head_since", repr(facts))
                self.assertNotIn("Head for", head_facts.order_text(facts, ZONE))

    def test_follower_token_gap_never_becomes_the_same_sessions_head_gap(self) -> None:
        reading = head_reading()
        follower = replace(reading.view.waiting[0], pid=456)
        reading = with_waiting_process(reading, follower, "456")
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46, pid=456, start_time="456")])
        facts = self.facts(reading)
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}),
                         ["Cargo token: head-session nextest: waited 154 s (06:56-06:59)"])
        self.assertEqual(head_facts.cargo_token_text(facts), "no cargo token wait")

    def test_token_gap_without_process_identity_matches_no_waiter(self) -> None:
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46, start_time="")])
        facts = self.facts(head_reading())
        self.assertEqual(head_facts.cargo_token_text(facts), "no cargo token wait")

    def test_live_queue_priority_is_shown_with_legacy_bump_history(self) -> None:
        reading = head_reading()
        priority: gate.QueuePriority = {"unit": "resources", "why": "user work", "set_at": NOW,
            "set_by": "showrunner"}
        reading = replace(reading, explanation=replace(reading.explanation, priority=priority))
        write_bumps(NOW, [bump_line(NOW - 5)])
        facts = self.facts(reading)
        self.assertIn("queue priority: resources (the user's bump)", head_facts.order_text(facts, ZONE))
        self.assertEqual(self.history(reading).total, 1)
        self.assertEqual(head_facts.bump_text(facts, ZONE), "Bumps since it joined at 06:50:00: 1")

    def test_three_order_texts_use_recorded_priority_and_deadline(self) -> None:
        reading = head_reading()
        head = reading.explanation.head
        if not isinstance(head, gate.HeadExplanation):
            self.fail("fixture needs a head")
        for order, priority, expected in (
            (gate.StrictUntil(NOW + 125), True,
             "younger steps may use 20.0 GiB of the budget until 07:02 (2 min 5 s left)"),
            (gate.BackfillUntil(NOW + 65), True,
             "younger steps may start until 07:01 (1 min 5 s left)"),
            (gate.StrictUntil(NOW + 125), False,
             "no head priority: younger steps that fit may start"),
            (gate.BackfillUntil(NOW + 65), False,
             "no head priority: younger steps that fit may start"),
        ):
            with self.subTest(order=order, priority=priority):
                explanation = replace(reading.explanation, head=replace(head, order=order, has_priority=priority))
                facts = self.facts(replace(reading, explanation=explanation))
                self.assertEqual(head_facts.order_text(facts, ZONE), expected)

    def test_strict_order_has_no_room_when_head_needs_the_whole_budget(self) -> None:
        reading = head_reading()
        head = reading.explanation.head
        if not isinstance(head, gate.HeadExplanation):
            self.fail("fixture needs a head")
        for need in (21, 22):
            with self.subTest(need=need):
                waiting = replace(head.waiter, need=need * gate.GIB)
                explanation = replace(reading.explanation, head=replace(
                    head, waiter=waiting, order=gate.StrictUntil(NOW + 125), has_priority=True))
                facts = self.facts(replace(reading, explanation=explanation))
                self.assertEqual(head_facts.order_text(facts, ZONE),
                    "younger steps wait until 07:02 (2 min 5 s left)")

    def test_token_wait_is_unknown_without_a_record(self) -> None:
        facts = self.facts(head_reading(fields={"priority_since": NOW - 154}))
        self.assertIsInstance(facts.token_waits, head_facts.TokenWaitsNotRecorded)
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}), ["Cargo token waits: unknown"])

    def test_recorded_token_return_uses_only_the_complete_gap_log_interval(self) -> None:
        reading = head_reading(since=NOW - 1000,
            fields={"token_left_at": NOW - 900, "token_back_at": NOW - 500})
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46)])
        facts = self.facts(reading)
        lines = head_facts.cargo_token_lines(facts, ZONE, {})
        self.assertEqual(len(lines), 1)
        self.assertIn("head-session", lines[0])
        self.assertIn("waited 154 s (06:56-06:59)", lines[0])

    def test_another_process_gap_on_the_same_step_is_not_the_heads(self) -> None:
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46, pid=456, start_time="different")])
        lines = head_facts.cargo_token_lines(self.facts(head_reading()), ZONE, {})
        self.assertEqual(lines, ["Cargo token waits: none"])

    def test_token_gaps_fall_back_to_both_pid_and_start_time_for_legacy_identity(self) -> None:
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46, session="", step="legacy-check")])
        lines = head_facts.cargo_token_lines(self.facts(head_reading(step_name="legacy-check")), ZONE, {})
        self.assertEqual(lines, ["Cargo token: unknown legacy-check: waited 154 s (06:56-06:59)"])

    def test_token_gaps_from_earlier_runs_of_the_same_step_are_excluded(self) -> None:
        since = NOW - 600
        write_token_gaps([
            token_gap_line(since - 300, since - 200),
            token_gap_line(since - 100, since + 10),
            token_gap_line(since, since + 154),
        ])
        facts = self.facts(head_reading(since=since))
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}),
                         ["Cargo token: head-session nextest: waited 154 s (06:50-06:52)"])
        self.assertEqual(head_facts.cargo_token_text(facts), "cargo token waited 154 s")

    def test_legacy_token_gap_requires_the_waiting_step_name(self) -> None:
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46, session="", step="clippy")])
        facts = self.facts(head_reading())
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}), ["Cargo token waits: none"])
        self.assertEqual(head_facts.cargo_token_text(facts), "no cargo token wait")

    def test_nonempty_token_session_never_falls_back_to_matching_process(self) -> None:
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46, session="other-session")])
        facts = self.facts(head_reading())
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}), ["Cargo token waits: none"])
        self.assertEqual(head_facts.cargo_token_text(facts), "no cargo token wait")

    def test_token_scan_stops_before_a_large_prefix_older_than_all_waiters(self) -> None:
        write_token_gaps([b"x" * (4 * head_facts.BLOCK_BYTES) + b"\n",
                          token_gap_line(NOW - 1000, NOW - 700),
                          token_gap_line(NOW - 200, NOW - 46)])
        budget = head_facts.LogScanBudget()
        with mock.patch.object(head_facts, "LogScanBudget", return_value=budget):
            facts = self.facts(head_reading())
        self.assertEqual(budget.read_bytes, head_facts.BLOCK_BYTES)
        self.assertEqual(head_facts.cargo_token_text(facts), "cargo token waited 154 s")

    def test_newer_lifecycle_lines_do_not_hide_a_matching_token_gap(self) -> None:
        for kind in ("admitted", "left", "ended"):
            with self.subTest(kind=kind):
                write_token_gaps([token_gap_line(NOW - 200, NOW - 46),
                                  queue_time_line(NOW - 20, kind=kind, session_id="head-session")])
                facts = self.facts(head_reading())
                self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}),
                                 ["Cargo token: head-session nextest: waited 154 s (06:56-06:59)"])
                self.assertEqual(head_facts.cargo_token_text(facts), "cargo token waited 154 s")

    def test_old_lifecycle_line_stops_token_scan_before_a_large_prefix(self) -> None:
        write_token_gaps([b"x" * (4 * head_facts.BLOCK_BYTES) + b"\n",
                          queue_time_line(NOW - 601, kind="left"),
                          token_gap_line(NOW - 200, NOW - 46)])
        budget = head_facts.LogScanBudget()
        with mock.patch.object(head_facts, "LogScanBudget", return_value=budget):
            facts = self.facts(head_reading())
        self.assertEqual(budget.read_bytes, head_facts.BLOCK_BYTES)
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}),
                         ["Cargo token: head-session nextest: waited 154 s (06:56-06:59)"])
        self.assertEqual(head_facts.cargo_token_text(facts), "cargo token waited 154 s")

    def test_queue_time_lines_without_numeric_at_make_history_unknown(self) -> None:
        for malformed in (b'{"kind":"admitted"}\n',
                          b'{"kind":"left","at":"not-a-time"}\n',
                          b'{"kind":"ended","at":true}\n',
                          b'{"kind":"admitted","at":NaN}\n',
                          b'[]\n', b'\n'):
            with self.subTest(line=malformed):
                write_token_gaps([token_gap_line(NOW - 200, NOW - 46), malformed])
                facts = self.facts(head_reading())
                self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}), [
                    "Cargo token: head-session nextest: waited 154 s (06:56-06:59)",
                    "Cargo token waits: unknown",
                ])
                self.assertEqual(head_facts.cargo_token_text(facts), "cargo token: unknown")

    def test_old_malformed_token_line_marks_unknown_and_stops_the_scan(self) -> None:
        write_token_gaps([b"x" * (4 * head_facts.BLOCK_BYTES) + b"\n",
                          queue_time_line(NOW - 601, kind="token_gap", session_id="head-session"),
                          token_gap_line(NOW - 200, NOW - 46)])
        budget = head_facts.LogScanBudget()
        with mock.patch.object(head_facts, "LogScanBudget", return_value=budget):
            facts = self.facts(head_reading())
        self.assertEqual(budget.read_bytes, head_facts.BLOCK_BYTES)
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}), [
            "Cargo token: head-session nextest: waited 154 s (06:56-06:59)",
            "Cargo token waits: unknown",
        ])
        self.assertEqual(head_facts.cargo_token_text(facts), "cargo token: unknown")

    def test_token_scan_uses_the_oldest_waiter_join_instead_of_the_head_join(self) -> None:
        reading = head_reading(since=NOW - 60)
        follower = replace(reading.view.waiting[0], pid=456, session_id="follower", since=NOW - 400)
        reading = with_waiting_process(reading, follower, "123")
        write_token_gaps([token_gap_line(NOW - 300, NOW - 146, session="follower", pid=456)])
        facts = self.facts(reading)
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}),
                         ["Cargo token: follower nextest: waited 154 s (06:55-06:57)"])
        self.assertEqual(head_facts.cargo_token_text(facts), "no cargo token wait")

    def test_token_scan_byte_limit_keeps_a_partial_sum_unknown(self) -> None:
        write_token_gaps([b"x" * (33 * head_facts.BLOCK_BYTES) + b"\n",
                          token_gap_line(NOW - 200, NOW - 46)])
        facts = self.facts(head_reading())
        self.assertIsInstance(facts.token_waits, head_facts.TokenWaitsNotRecorded)
        self.assertEqual(head_facts.cargo_token_text(facts), "cargo token: unknown")

    def test_token_gap_partial_identity_matches_do_not_include_an_unrelated_step(self) -> None:
        for session, step, pid, start_time in (
            ("head-session", "other-step", 456, "different"),
            ("other-session", "nextest", 456, "different"),
            ("", "legacy-check", 123, "different"),
            ("", "legacy-check", 456, "123"),
            ("unrelated", "check", 456, "different"),
        ):
            with self.subTest(session=session, step=step, pid=pid, start_time=start_time):
                write_token_gaps([token_gap_line(NOW - 200, NOW - 46, session=session,
                                                step=step, pid=pid, start_time=start_time)])
                self.assertEqual(head_facts.cargo_token_lines(self.facts(head_reading()), ZONE, {}),
                                 ["Cargo token waits: none"])

    def test_token_gaps_include_only_waiters_with_their_own_recorded_join_time(self) -> None:
        reading = head_reading(since=NOW - 60)
        younger = replace(reading.view.waiting[0], pid=456, session_id="younger-session", step="clippy",
                          since=NOW - 300)
        running = gate.RunningStep("running-session", "runner", "check", "/tmp/run", gate.GIB,
                                   gate.MeasuredMemory(gate.GIB))
        reading = with_waiting_process(reading, younger, "123")
        reading = replace(reading, view=replace(reading.view, running=(running,)))
        write_token_gaps([
            token_gap_line(NOW - 400, NOW - 300, session="running-session", step="check", pid=789),
            token_gap_line(NOW - 200, NOW - 46, session="younger-session", step="clippy", pid=456),
            token_gap_line(NOW - 40, NOW - 20, session="finished-unrelated", step="check", pid=999),
        ])
        facts = self.facts(reading)
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}), [
            "Cargo token: younger-session clippy: waited 154 s (06:56-06:59)",
        ])
        self.assertEqual(head_facts.session_ids(facts), ["younger-session"])
        self.assertEqual(head_facts.cargo_token_text(facts), "no cargo token wait")

    def test_malformed_token_line_keeps_valid_intervals_and_marks_missing_history_unknown(self) -> None:
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46), b"broken JSON\n"])
        path = store.root() / head_facts.QUEUE_TIMES_PATH
        before = path.read_bytes()
        facts = self.facts(head_reading())
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}), [
            "Cargo token: head-session nextest: waited 154 s (06:56-06:59)",
            "Cargo token waits: unknown",
        ])
        self.assertEqual(path.read_bytes(), before)

    def test_partial_token_line_keeps_valid_intervals_and_marks_missing_history_unknown(self) -> None:
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46)],
                         tail=token_gap_line(NOW - 40, NOW - 20).rstrip(b"\n"))
        self.assertEqual(head_facts.cargo_token_lines(self.facts(head_reading()), ZONE, {}), [
            "Cargo token: head-session nextest: waited 154 s (06:56-06:59)",
            "Cargo token waits: unknown",
        ])

    def test_waiter_token_fields_do_not_supply_an_unrecorded_gap(self) -> None:
        facts = self.facts(head_reading(fields={"token_left_at": NOW - 200, "token_back_at": NOW - 46}))
        self.assertEqual(head_facts.cargo_token_lines(facts, ZONE, {}), ["Cargo token waits: unknown"])

    def test_empty_token_gap_file_still_has_no_recorded_wait(self) -> None:
        write_token_gaps([])
        self.assertEqual(head_facts.cargo_token_lines(self.facts(head_reading()), ZONE, {}),
                         ["Cargo token waits: unknown"])

    def test_torn_token_gap_is_unknown_until_the_complete_line_is_published(self) -> None:
        write_token_gaps([], tail=token_gap_line(NOW - 200, NOW - 46).rstrip(b"\n"))
        path = store.root() / head_facts.QUEUE_TIMES_PATH
        self.assertEqual(head_facts.cargo_token_lines(self.facts(head_reading()), ZONE, {}),
                         ["Cargo token waits: unknown"])
        with path.open("ab") as output:
            self.assertEqual(output.write(b"\n"), 1)
        lines = head_facts.cargo_token_lines(self.facts(head_reading()), ZONE, {})
        self.assertEqual(len(lines), 1)
        self.assertIn("waited 154 s", lines[0])

    def test_malformed_or_incomplete_token_gaps_never_claim_an_active_wait(self) -> None:
        for malformed in (b"broken JSON\n",
                          queue_time_line(NOW - 60, kind="token_gap", session_id="head-session",
                                          step="nextest", pid=123, start_time="123", token_left_at=NOW - 60),
                          token_gap_line(NOW - 10, NOW - 20),
                          token_gap_line(NOW - 20, NOW + 10),
                          token_gap_line(float("nan"), NOW - 10)):
            with self.subTest(line=malformed):
                write_token_gaps([malformed])
                lines = head_facts.cargo_token_lines(self.facts(head_reading()), ZONE, {})
                self.assertTrue(any("unknown" in line for line in lines))
                self.assertFalse(any("waiting for the cargo token since" in line for line in lines))

    def test_scan_budget_is_shared_across_months(self) -> None:
        since = datetime(2026, 9, 30, 23, 59, tzinfo=UTC).timestamp()
        padding = b'{"kind":"step","padding":"' + b"x" * (17 * 1024 * 1024) + b'"}\n'
        write_bumps(since, [padding, bump_line(since)])
        write_bumps(NOW, [padding, bump_line(NOW)])
        facts = self.facts(head_reading(since=since))
        self.assertIsInstance(facts.bumps, head_facts.BumpsUnavailable)
        self.assertEqual(head_facts.bump_text(facts, ZONE, short=True), "bumps: unknown")

    def test_no_waiter_does_not_open_the_bump_log(self) -> None:
        reading = queue_read_fixture(gate.QueueView((), (), 30 * gate.GIB, 0, 3 * gate.GIB, NOW))
        with mock.patch.object(store, "host_file", side_effect=AssertionError("nothing waits")):
            self.assertEqual(head_facts.read_head_facts(reading), gate.NoWaiter())

    def test_bump_scan_keeps_oldest_waiters_moves_separate_from_head_history(self) -> None:
        reading = head_reading(since=NOW - 60)
        older = replace(reading.view.waiting[0], pid=456, session_id="older", since=NOW - 900)
        reading = replace(reading, view=replace(reading.view, waiting=(*reading.view.waiting, older)))
        write_bumps(NOW, [bump_line(NOW - 500, session="older"), bump_line(NOW - 5)])
        facts = self.facts(reading)
        self.assertEqual([record.at for record in facts.bump_records], [NOW - 500, NOW - 5])
        assert isinstance(facts.bumps, head_facts.BumpHistory)
        self.assertEqual(facts.bumps.total, 1)
        self.assertEqual(facts.bumps.groups[0].session, "bumped-session")

    def test_waiting_move_history_matches_identity_join_boundary_and_resolved_priority(self) -> None:
        reading = head_reading(since=NOW - 60)
        step = reading.view.waiting[0]
        row: gate.Waiter = {**reading.ordered[0][1], "priority_rank": 0}
        write_bumps(NOW, [
            bump_line(NOW - 61, session="head-session", step="nextest", folder="/tmp/head"),
            bump_line(NOW - 60, session="head-session", step="nextest", folder="/tmp/head", old_position=9),
            bump_line(NOW - 10, session="head-session", step="clippy", folder="/tmp/head"),
            bump_line(NOW - 9, session="head-session", step="nextest", folder="/tmp/other"),
            bump_line(NOW - 8, session="other", step="nextest", folder="/tmp/head"),
            bump_line(NOW - 1, session="head-session", step="nextest", folder="/tmp/head", old_position=2),
        ])
        facts = self.facts(reading)
        moves = head_facts.waiting_moves(facts, step, row)
        self.assertEqual([(record.at, record.old_position) for record in moves], [(NOW - 60, 9), (NOW - 1, 2)])
        self.assertEqual(head_facts.waiting_moves(facts, step, reading.ordered[0][1]), ())
        legacy = replace(step, session_id="", session_name="head-session")
        self.assertEqual(head_facts.waiting_moves(facts, legacy, row), moves)

    def test_running_moves_stop_at_admission_and_require_every_identity_field(self) -> None:
        reading = head_reading(since=NOW - 600)
        write_bumps(NOW, [
            bump_line(NOW - 60, session="runner", step="nextest", folder="/tmp/run", old_position=29),
            bump_line(NOW - 50, session="runner", step="nextest", folder="/tmp/other"),
            bump_line(NOW - 25, session="runner", step="nextest", folder="/tmp/run", old_position=4),
            bump_line(NOW - 24, session="runner", step="nextest", folder="/tmp/run", old_position=2),
        ])
        step = gate.RunningStep("runner", "runner", "nextest", "/tmp/run", gate.GIB, gate.MeasuredMemory(gate.GIB))
        running = gate.ExplainedRunningStep(step, NOW - 25, gate.GIB, gate.GIB, gate.GIB, gate.UsingNow(gate.GIB))
        facts = self.facts(reading)
        self.assertEqual([record.old_position for record in head_facts.running_moves(facts, running)], [29, 4])
        self.assertEqual(head_facts.running_moves(facts, replace(running, admitted_at=gate.AdmissionTimeNotRecorded())), ())


class PaceTests(AdmissionLogTests):
    def test_window_edges_this_host_future_and_torn_last_line(self) -> None:
        from scripts.build_queue.queue_fixture import QueueFixture
        with QueueFixture() as fixture:
            fixture.write_pace([NOW - 3600.001, NOW - 3600, NOW - 1, NOW, NOW + 1],
                tail=(json.dumps({"host": store.host_name(), "ended_at": store.utc_iso(NOW)}) + "no-newline").encode())
            self.assertEqual(head_facts.read_pace(NOW), head_facts.RecordedPace(3))
            fixture.write_pace([NOW - 1], host="another-host")
            self.assertEqual(head_facts.read_pace(NOW), head_facts.RecordedPace(0))

    def test_old_line_stops_before_malformed_or_large_older_history(self) -> None:
        path = self.root / "admission/anon_peaks.jsonl"
        write_lines(path, [b'{broken old line}\n',
            (json.dumps({"host": store.host_name(), "ended_at": store.utc_iso(NOW - 3601)}) + "\n").encode(),
            (json.dumps({"host": store.host_name(), "ended_at": store.utc_iso(NOW)}) + "\n").encode()])
        self.assertEqual(head_facts.read_pace(NOW), head_facts.RecordedPace(1))

    def test_missing_and_empty_files_have_zero_pace_but_a_read_error_is_unknown(self) -> None:
        path = self.root / "admission/anon_peaks.jsonl"
        self.assertEqual(head_facts.read_pace(NOW), head_facts.RecordedPace(0))
        write_lines(path, [])
        self.assertEqual(head_facts.read_pace(NOW), head_facts.RecordedPace(0))
        path.unlink()
        path.mkdir()
        self.assertIsInstance(head_facts.read_pace(NOW), head_facts.PaceUnavailable)


if __name__ == "__main__":
    _ = unittest.main()
