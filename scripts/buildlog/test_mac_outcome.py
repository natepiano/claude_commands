"""Shared Mac results and shares, using temporary build logs."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import override
from unittest.mock import patch

import index
import mac_outcome as outcome
from test_index import call, encode, point_root_at


class MacOutcomeTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        point_root_at(self, self.root)

    def test_each_mac_result_reads_as_its_named_type(self) -> None:
        completed: tuple[outcome.MacRunOutcome, ...] = ("passed", "passed_filter", "failed")
        for mac in completed:
            with self.subTest(mac=mac):
                self.assertEqual(outcome.offload_record(mac, "", None, 0.4), outcome.MacRunCompleted(mac, 0.4))
        self.assertEqual(outcome.offload_record("lost", None, None, 1), outcome.MacRunLost(1))
        self.assertEqual(outcome.offload_record("declined", "no_tests", None, 1), outcome.MacRunMatchedNoTest(1))
        self.assertEqual(outcome.offload_record("declined", "local_flag", "needs Linux", None), outcome.LocalRunRequested("needs Linux"))
        self.assertIs(outcome.offload_record("declined", "runner", None, None), outcome.RUNNER_RESULT_UNAVAILABLE)
        for reason in outcome.DECLINE_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(outcome.offload_record("declined", reason, None, 2), outcome.OffloadDeclined(reason, 2))

    def test_ineligible_rows_need_no_mac_seconds(self) -> None:
        reasons: tuple[outcome.OffloadDeclineReason, ...] = ("repo", "linux_only")
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertEqual(outcome.offload_record("declined", reason, reason, None), outcome.OffloadDeclined(reason, 0))

    def test_empty_mac_and_interrupted_attempts_preserve_only_recorded_results(self) -> None:
        for mac in (None, ""):
            self.assertIs(outcome.offload_record(mac, "busy", None, 5, "interrupted"), outcome.NO_OFFLOAD_RECORD)
        self.assertEqual(outcome.offload_record("declined", "busy", None, 5, "interrupted"), outcome.OffloadDeclined("busy", 5))
        self.assertEqual(outcome.offload_record("passed", "", None, 5, "interrupted"), outcome.MacRunCompleted("passed", 5))

    def test_rows_the_runner_does_not_write_are_unrecognized(self) -> None:
        for reason in ("runner", "local_flag"):
            self.assertIs(outcome.offload_record("declined", reason, "reason", 1), outcome.UNRECOGNIZED_OFFLOAD_RECORD)
        for seconds in (None, -1, True, "1", float("nan"), float("inf")):
            self.assertIs(outcome.offload_record("passed", "", None, seconds), outcome.UNRECOGNIZED_OFFLOAD_RECORD)
        self.assertIs(outcome.offload_record("passed", "busy", None, 1), outcome.UNRECOGNIZED_OFFLOAD_RECORD)
        self.assertIs(outcome.offload_record("declined", "unknown", None, 1), outcome.UNRECOGNIZED_OFFLOAD_RECORD)
        for reason in (None, ""):
            record = outcome.offload_record("declined", "local_flag", reason, None)
            self.assertEqual(record, outcome.LocalRunRequested(outcome.LOCAL_REASON_NOT_RECORDED))

    def test_candidate_rule_excludes_cache_local_requests_and_ineligible_results(self) -> None:
        for reused in ("reused", "replayed"):
            record = outcome.offload_record("passed", "", None, 1, reused)
            self.assertIs(record, outcome.CACHE_REUSE)
            self.assertFalse(outcome.is_mac_candidate(record))
        for record in (outcome.NO_OFFLOAD_RECORD, outcome.UNRECOGNIZED_OFFLOAD_RECORD,
                       outcome.LocalRunRequested("Linux"), outcome.OffloadDeclined("repo", 0),
                       outcome.OffloadDeclined("linux_only", 0)):
            self.assertFalse(outcome.is_mac_candidate(record))
        for record in (outcome.MacRunCompleted("passed", 1), outcome.MacRunLost(1),
                       outcome.MacRunMatchedNoTest(1), outcome.RUNNER_RESULT_UNAVAILABLE,
                       outcome.OffloadDeclined("off", 1)):
            self.assertTrue(outcome.is_mac_candidate(record))

    def test_share_rule_counts_every_result_on_its_side(self) -> None:
        for reason in outcome.DECLINE_REASONS:
            expected = 0 if reason in (*outcome.NOT_USABLE_REASONS, "repo", "linux_only") else 1
            self.assertEqual(outcome.share_of_records([outcome.OffloadDeclined(reason, 1)]), outcome.UsableShare(0, expected))
        completed: tuple[outcome.MacRunOutcome, ...] = ("passed", "passed_filter", "failed")
        for mac in completed:
            self.assertEqual(outcome.share_of_records([outcome.MacRunCompleted(mac, 1)]), outcome.UsableShare(1, 1))
        self.assertEqual(outcome.share_of_records([outcome.MacRunMatchedNoTest(1)]), outcome.UsableShare(1, 1))
        for record in (outcome.MacRunLost(1), outcome.RUNNER_RESULT_UNAVAILABLE):
            self.assertEqual(outcome.share_of_records([record]), outcome.UsableShare(0, 1))
        self.assertEqual(outcome.share_of_records([outcome.CACHE_REUSE, outcome.NO_OFFLOAD_RECORD,
                         outcome.UNRECOGNIZED_OFFLOAD_RECORD, outcome.LocalRunRequested("Linux")]), outcome.UsableShare(0, 0))

    def test_each_unfinished_candidate_belongs_to_exactly_one_share_list(self) -> None:
        not_usable = set(outcome.NOT_USABLE_REASONS)
        counted_against = set(outcome.COUNTED_AGAINST_SHARE)
        for reason in outcome.DECLINE_REASONS:
            record = outcome.OffloadDeclined(reason, 1)
            with self.subTest(reason=reason):
                membership = int(reason in not_usable) + int(reason in counted_against)
                self.assertEqual(membership, int(outcome.is_mac_candidate(record)))
        for reason, record in (("lost", outcome.MacRunLost(1)), ("runner", outcome.RUNNER_RESULT_UNAVAILABLE)):
            with self.subTest(reason=reason):
                self.assertTrue(outcome.is_mac_candidate(record))
                self.assertEqual(int(reason in not_usable) + int(reason in counted_against), 1)
        self.assertFalse(not_usable & counted_against)

    def test_share_counts_missed_candidates_only_from_the_shared_list(self) -> None:
        counted_against = tuple(reason for reason in outcome.COUNTED_AGAINST_SHARE if reason not in ("busy", "lost", "runner"))
        records = [outcome.OffloadDeclined("busy", 1), outcome.MacRunLost(1), outcome.RUNNER_RESULT_UNAVAILABLE]
        with patch.object(outcome, "COUNTED_AGAINST_SHARE", counted_against):
            self.assertEqual(outcome.share_of_records(records), outcome.UsableShare(0, 0))

    def test_range_share_uses_the_same_rule_and_calls_started_index(self) -> None:
        log = self.root / "natedev" / "2026-10.jsonl"
        log.parent.mkdir(parents=True)
        with log.open("wb") as handle:
            for record in (
                call("none", mac="declined", mac_reason="no_tests", mac_s=1),
                call("off", mac="declined", mac_reason="off", mac_s=1),
                call("busy", mac="declined", mac_reason="busy", mac_s=1),
                call("cached", mac="passed", mac_s=1, outcome="reused"),
                call("end", started_at="2026-10-03T00:00:00.000Z", mac="passed", mac_s=1),
            ):
                _ = handle.write(encode(record))
        _ = index.update()
        with closing(index.read_only()) as connection:
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            self.assertEqual(outcome.usable_share(connection, "2026-10-02T00:00:00.000Z", "2026-10-03T00:00:00.000Z"), outcome.UsableShare(1, 2))
            self.assertEqual(len(statements), 1)
            self.assertIn("calls_started", str(connection.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()))
            self.assertNotIn("localtime", statements[0])

    def test_python_and_sql_step_rules_identify_the_same_mac_caller(self) -> None:
        for caller in ("verify-mac", "verify", None, "agent"):
            self.assertEqual(outcome.is_mac_step(caller), caller == "verify-mac")
        self.assertEqual(outcome.natedev_step_predicate("s"), "NOT (s.caller IS 'verify-mac')")


if __name__ == "__main__":
    _ = unittest.main()
