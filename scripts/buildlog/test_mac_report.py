"""Mac report counts, local day ranges and speed, using temporary build logs."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack, cast, override
from unittest.mock import patch

import index
import launches
import mac_report
import mac_outcome
import store

if TYPE_CHECKING:
    from ..direct.test_verify_untested_examples import FIXED_RUSTC, rust_tool_path
else:
    from scripts.direct.test_verify_untested_examples import FIXED_RUSTC, rust_tool_path
from test_holds import hold_event, timed_hold_events, write_mac_block
from test_index import Record, call, encode, point_root_at, step
from test_report import MeasuredCliRun, measure_cli_work
from user_zone import user_zone
if TYPE_CHECKING:
    from ..lib import test_hang_guard
else:
    from scripts.lib import test_hang_guard

CLI = Path(__file__).with_name("cli.py")
NOW = datetime(2026, 10, 12, 15, 9, tzinfo=UTC)
START = "2026-10-11T15:09:00.000Z"
END = "2026-10-12T15:09:00.000Z"


class MacCallFields(TypedDict, total=False):
    command: str
    outcome: str
    tool: str
    verb: str
    local_reason: str
    sha: str
    target: str
    hold_wait_s: float
    mac_holder: str


class MacReportTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    log: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        point_root_at(self, self.root)
        tools = rust_tool_path(self.root.parent / "tools")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"PATH": tools})))
        self.log = self.root / "natedev" / "2026-10.jsonl"
        self.log.parent.mkdir(parents=True)
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        self.enterContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))

        def restore_timezone() -> None:
            if previous is None:
                _ = os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

        self.addCleanup(restore_timezone)

    def write(self, *records: Record) -> None:
        with self.log.open("ab") as handle:
            for record in records:
                _ = handle.write(encode(record))

    def mac_call(self, record_id: str, mac: str = "passed", reason: str = "", **fields: Unpack[MacCallFields]) -> Record:
        record = call(
            record_id, started_at="2026-10-12T14:00:00.000Z", mac=mac, mac_reason=reason, **fields,
        )
        if mac == "declined" and reason in ("local_flag", "runner", "repo", "linux_only"):
            return record
        record["mac_s"] = 10.0
        return record

    def report(self, hours: float = 24) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return mac_report.mac_report(connection, NOW, hours)

    def summary(self) -> mac_report.WindowSummary:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return mac_report.summarize_window(mac_report.read_test_calls(connection, START, END))

    def test_candidates_exclude_cache_local_flag_and_ineligible_calls(self) -> None:
        self.write(
            self.mac_call("candidate"),
            self.mac_call("cached-pass", outcome="reused"),
            self.mac_call("cached-failure", outcome="replayed"),
            self.mac_call("local", "declined", "local_flag", local_reason="needs Linux"),
            self.mac_call("repo", "declined", "repo"),
            self.mac_call("linux", "declined", "linux_only"),
            self.mac_call("other-tool", tool="port-lint"),
            self.mac_call("other-command", verb="lint"),
        )
        summary = self.summary()
        self.assertEqual(summary.candidates, 1)
        self.assertEqual(summary.excluded.cache_reuses, 2)
        self.assertEqual(summary.excluded.local_flag, 1)
        self.assertEqual(summary.excluded.repo, 1)
        self.assertEqual(summary.excluded.linux_only, 1)
        self.assertIn("ineligible: 2 (repo: 1; linux_only: 1)", self.report())

    def test_finished_lost_and_each_decline_reason_are_counted(self) -> None:
        self.write(self.mac_call("passed"), self.mac_call("filter", "passed_filter"), self.mac_call("failed", "failed"), self.mac_call("lost", "lost"))
        reasons: tuple[mac_outcome.OffloadDeclineReason | Literal["runner"], ...] = (
            *[reason for reason in mac_outcome.DECLINE_REASONS if reason not in ("repo", "linux_only")], "runner",
        )
        for reason in reasons:
            self.write(self.mac_call(reason, "declined", reason))
        summary = self.summary()
        self.assertEqual((summary.passed, summary.failed, summary.lost), (2, 1, 1))
        self.assertEqual(summary.candidates, 4 + len(mac_outcome.DECLINE_REASONS) - 1)
        for reason in reasons:
            self.assertEqual(summary.declined[reason], 1)
            self.assertIn(f"{reason}: 1", self.report())

    def test_usable_share_sets_aside_each_unusable_reason_and_counts_other_answers(self) -> None:
        self.write(self.mac_call("pass"), self.mac_call("failure", "failed"), self.mac_call("lost", "lost"))
        for reason in ("blocked", "backoff", "unreachable", "battery", "disk", "off", "busy", "mac_busy", "runner", "state", "copy", "no_tests"):
            self.write(self.mac_call(reason, "declined", reason))
        _ = index.update()
        with closing(index.read_only()) as connection:
            self.assertEqual(mac_outcome.usable_share(connection, START, END), mac_outcome.UsableShare(3, 9))
        output = self.report()
        self.assertIn("Usable share: 3 of 9 finished on the Mac (33.3%)", output)
        self.assertIn("Set aside as not usable: blocked, backoff, unreachable, battery, disk, off", output)
        self.assertIn("Counted against the share: busy, mac_busy, lost, runner, state, copy.", output)

    def test_usable_share_includes_start_and_excludes_end_using_calls_started_index(self) -> None:
        self.write(
            call("before", started_at="2026-10-11T15:08:59.999Z", mac="passed", mac_s=1),
            call("start", started_at=START, mac="passed", mac_s=1),
            call("end", started_at=END, mac="failed", mac_s=1),
        )
        _ = index.update()
        with closing(index.read_only()) as connection:
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            self.assertEqual(mac_outcome.usable_share(connection, START, END), mac_outcome.UsableShare(1, 1))
            self.assertEqual(len(statements), 1)
            self.assertNotIn("localtime", statements[0])
            plan = connection.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()
            self.assertIn("calls_started", str(plan))

    def test_usable_share_applies_each_reason_and_finished_outcome_individually(self) -> None:
        cases = [
            ("declined", reason, 1 if reason == "no_tests" else 0, 0 if reason in mac_outcome.NOT_USABLE_REASONS else 1)
            for reason in ("blocked", "backoff", "unreachable", "battery", "disk", "off", "busy", "mac_busy", "runner", "state", "copy", "no_tests")
        ] + [("lost", "", 0, 1), ("passed", "", 1, 1), ("passed_filter", "", 1, 1), ("failed", "", 1, 1)]
        for number, (mac, reason, _, _) in enumerate(cases):
            at = store.utc_iso((NOW - timedelta(hours=1) + timedelta(minutes=number)).timestamp())
            self.write(call(str(number), started_at=at, mac=mac, mac_reason=reason, mac_s=None if reason == "runner" else 1))
        _ = index.update()
        with closing(index.read_only()) as connection:
            for number, (mac, reason, finished, candidates) in enumerate(cases):
                with self.subTest(mac=mac, reason=reason):
                    start = NOW - timedelta(hours=1) + timedelta(minutes=number)
                    self.assertEqual(
                        mac_outcome.usable_share(connection, store.utc_iso(start.timestamp()), store.utc_iso((start + timedelta(minutes=1)).timestamp())),
                        mac_outcome.UsableShare(finished, candidates),
                    )

    def test_largest_causes_are_printed_largest_first_with_busy_as_one_cause(self) -> None:
        for number, reason in enumerate(("runner", "busy", "busy", "busy", "mac_busy", "mac_busy", "blocked")):
            self.write(self.mac_call(str(number), "declined", reason))
        output = self.report()
        self.assertIn("Largest causes of a usable Mac going unused: busy: 3; mac_busy: 2; runner: 1.", output)

    def test_largest_causes_read_both_shared_share_lists(self) -> None:
        self.write(self.mac_call("busy", "declined", "busy"), self.mac_call("lost", "lost"), self.mac_call("runner", "declined", "runner"))
        counted_against = tuple(reason for reason in mac_outcome.COUNTED_AGAINST_SHARE if reason not in ("busy", "lost", "runner"))
        with patch.object(mac_report, "COUNTED_AGAINST_SHARE", counted_against):
            self.assertFalse(self.summary().causes)
        with patch.object(mac_report, "NOT_USABLE_REASONS", (*mac_outcome.NOT_USABLE_REASONS, "busy")):
            causes = self.summary().causes
            self.assertNotIn("busy", causes)
            self.assertEqual(causes["lost"], 1)
            self.assertEqual(causes["runner"], 1)

    def test_failed_mac_tree_with_a_passing_natedev_step_is_recorded(self) -> None:
        self.write(
            self.mac_call("mac-failed", "failed"),
            step("mac-step", started_at="2026-10-12T14:00:00.000Z", call_id="mac-failed", caller="verify-mac", host="Mac", step="nextest", status=1, tree_key="tree-a"),
            self.mac_call("local-pass", "declined", "local_flag", local_reason="retry Mac failure"),
            step("local-step", started_at="2026-10-12T14:01:00.000Z", call_id="local-pass", step="nextest", status=0, tree_key="tree-a"),
        )
        self.assertIn("Mac failures with a natedev pass of the same tree: 1 recorded", self.report())

    def record_sent_mac_step(self, call_id: str, sent_key: str) -> None:
        mirror = self.root.parent / "mac-mirror"
        mirror.mkdir()
        self.assertFalse((mirror / ".git").exists())
        environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith("BUILDLOG_") and name != "MANIFEST_PATH"
        }
        environment.update({
            "BUILDLOG_DIR": str(self.root), "BUILDLOG_CALLER": "verify-mac",
            "BUILDLOG_CALL_ID": call_id, "BUILDLOG_SENT_TREE_KEY": sent_key,
        })
        started = datetime(2026, 10, 12, 14, tzinfo=UTC).timestamp()
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("record.py")), "step", "1",
             str(started), str(started + 1), "0", "", "", "cargo", "nextest", "run"],
            cwd=mirror, env=environment, capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [
            cast(Record, json.loads(line))
            for path in self.root.glob("*/*.jsonl")
            for line in path.read_text().splitlines()
        ]
        mac_steps = [row for row in rows if row.get("kind") == "step" and row.get("caller") == "verify-mac"]
        self.assertEqual(len(mac_steps), 1)
        self.assertEqual(mac_steps[0]["rustc"], FIXED_RUSTC)
        self.assertEqual((mac_steps[0]["call_id"], mac_steps[0]["tree_key"]), (call_id, sent_key))

    def test_sent_key_on_recorded_mac_step_counts_a_natedev_pass_of_the_same_selection(self) -> None:
        sent_key = "a" * 64
        self.write(
            self.mac_call("sent-mac-failure", "failed", command="test hana --filter one"),
            self.mac_call("same-selection-pass", "declined", "local_flag", local_reason="retry",
                          command="test hana --filter one"),
            step("same-selection-step", started_at=START, call_id="same-selection-pass",
                 step="nextest", status=0, tree_key=sent_key),
        )
        self.record_sent_mac_step("sent-mac-failure", sent_key)
        output = self.report()
        self.assertIn("Mac failures with a natedev pass of the same tree: 1 recorded.", output)
        self.assertNotIn("without a tree key", output)

    def test_sent_key_on_recorded_mac_step_does_not_count_a_different_natedev_command(self) -> None:
        sent_key = "a" * 64
        self.write(
            self.mac_call("sent-mac-failure", "failed", command="test hana --filter one"),
            self.mac_call("different-selection-pass", "declined", "local_flag", local_reason="retry",
                          command="test hana --filter two"),
            step("different-selection-step", started_at=START, call_id="different-selection-pass",
                 step="nextest", status=0, tree_key=sent_key),
        )
        self.record_sent_mac_step("sent-mac-failure", sent_key)
        output = self.report()
        self.assertIn("Mac failures with a natedev pass of the same tree: 0 recorded.", output)
        self.assertNotIn("without a tree key", output)

    def test_mac_failures_require_the_same_test_selection_as_the_natedev_pass(self) -> None:
        self.write(
            self.mac_call("whole-package", "failed", command="test hana"),
            step("whole-mac", started_at=START, call_id="whole-package", caller="verify-mac", step="nextest", status=1, tree_key="tree-a"),
            self.mac_call("filtered-pass", "declined", "local_flag", local_reason="retry", command="test hana --filter one"),
            step("filtered-local", started_at=START, call_id="filtered-pass", step="nextest", status=0, tree_key="tree-a"),
        )
        self.assertIn("Mac failures with a natedev pass of the same tree: 0 recorded.", self.report())
        self.write(
            self.mac_call("whole-pass", "declined", "local_flag", local_reason="retry", command="test hana"),
            step("whole-local", started_at=START, call_id="whole-pass", step="nextest", status=0, tree_key="tree-a"),
        )
        self.assertIn("Mac failures with a natedev pass of the same tree: 1 recorded.", self.report())

    def test_mac_failures_require_a_passing_natedev_step_of_the_same_package_and_tree(self) -> None:
        self.write(
            self.mac_call("local-failed", "failed"),
            step("failed-mac", started_at=START, call_id="local-failed", caller="verify-mac", step="nextest", status=1, tree_key="failed-tree"),
            self.mac_call("local-failed-retry", "declined", "local_flag", local_reason="retry"),
            step("failed-local", started_at=START, call_id="local-failed-retry", step="nextest", status=1, tree_key="failed-tree"),
            self.mac_call("different-tree", "failed"),
            step("different-mac", started_at=START, call_id="different-tree", caller="verify-mac", step="nextest", status=1, tree_key="tree-a"),
            self.mac_call("other-pass", "declined", "local_flag", local_reason="retry"),
            step("other-local", started_at=START, call_id="other-pass", step="nextest", status=0, tree_key="tree-b"),
            call("other-package", started_at=START, package="other"),
            step("package-local", started_at=START, call_id="other-package", step="nextest", status=0, tree_key="tree-a"),
            self.mac_call("passed", "passed"),
            step("passed-mac", started_at=START, call_id="passed", caller="verify-mac", step="nextest", status=0, tree_key="tree-b"),
        )
        self.assertIn("Mac failures with a natedev pass of the same tree: 0 recorded", self.report())

    def test_failed_mac_calls_without_keyed_mac_steps_are_reported(self) -> None:
        self.write(
            self.mac_call("unkeyed", "failed"),
            step("unkeyed-mac", started_at=START, call_id="unkeyed", caller="verify-mac", step="nextest", status=1),
            self.mac_call("unsynced", "failed"),
            self.mac_call("lost", "lost"), self.mac_call("runner", "declined", "runner"),
        )
        output = self.report()
        self.assertIn("Mac failures with a natedev pass of the same tree: 0 recorded, 2 without a tree key", output)
        self.assertIn("Environment failures: lost runs: 1; runner: 1.", output)

    def test_same_tree_passes_stay_within_the_step_read_bounds(self) -> None:
        self.write(
            self.mac_call("earlier-pass", "failed"),
            step("earlier-mac", started_at=START, call_id="earlier-pass", caller="verify-mac", step="nextest", status=1, tree_key="earlier"),
            call("recent-pass", started_at="2026-10-09T08:00:00.000Z"),
            step("recent-local", started_at="2026-10-09T08:00:00.000Z", call_id="recent-pass", step="nextest", status=0, tree_key="earlier"),
            self.mac_call("older-pass", "failed"),
            step("older-mac", started_at=START, call_id="older-pass", caller="verify-mac", step="nextest", status=1, tree_key="older"),
            call("old-pass", started_at="2026-10-01T00:00:00.000Z"),
            step("old-local", started_at="2026-10-01T00:00:00.000Z", call_id="old-pass", step="nextest", status=0, tree_key="older"),
            self.mac_call("future-pass", "failed"),
            step("future-mac", started_at=START, call_id="future-pass", caller="verify-mac", step="nextest", status=1, tree_key="future"),
            call("future-local", started_at=END),
            step("future-local-step", started_at=END, call_id="future-local", step="nextest", status=0, tree_key="future"),
        )
        self.assertIn("Mac failures with a natedev pass of the same tree: 1 recorded.", self.report())

    def test_report_reads_steps_once_for_all_windows(self) -> None:
        for number in range(2):
            self.write(
                self.mac_call(f"failed-{number}", "failed"),
                step(f"mac-{number}", started_at=START, call_id=f"failed-{number}", caller="verify-mac", step="nextest", status=1, tree_key=f"tree-{number}"),
            )
        _ = index.update()
        with closing(index.read_only()) as connection:
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            _ = mac_report.mac_report(connection, NOW)
        self.assertLessEqual(len(statements), 4)
        self.assertEqual(sum("JOIN steps" in sql or "FROM steps" in sql for sql in statements), 1)

    def test_step_read_uses_both_time_bounds_and_the_steps_started_index(self) -> None:
        _ = index.update()
        with closing(index.read_only()) as connection:
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            _ = mac_report.mac_report(connection, NOW)
            query = next(sql for sql in statements if "FROM steps" in sql)
            self.assertIn("s.started_at >=", query)
            self.assertIn("2026-10-09T07:00:00.000Z", query)
            self.assertIn("s.started_at <", query)
            self.assertIn(END, query)
            self.assertIn("steps_started", str(connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()))

    def test_matched_no_tests_is_finished_and_never_a_decline_or_cause(self) -> None:
        self.write(self.mac_call("none", "declined", "no_tests"))
        summary = self.summary()
        self.assertEqual(summary.matched_no_tests, 1)
        self.assertEqual(summary.share, mac_outcome.UsableShare(1, 1))
        output = self.report()
        self.assertIn("Finished on the Mac: 1 (passed: 0; failed: 0; matched no tests: 1)", output)
        self.assertIn("Declined: none.", output)
        self.assertIn("Largest causes of a usable Mac going unused: none.", output)

    def test_off_remains_a_decline_but_is_not_usable_or_a_cause(self) -> None:
        self.write(self.mac_call("off", "declined", "off"))
        summary = self.summary()
        self.assertEqual(summary.candidates, 1)
        self.assertEqual(summary.declined["off"], 1)
        self.assertEqual(summary.share, mac_outcome.UsableShare(0, 0))
        self.assertIn("Largest causes of a usable Mac going unused: none.", self.report())

    def test_local_flags_keep_free_text_count_repeated_reasons_and_unavailable_patterns(self) -> None:
        reasons = ("Mac busy", "Mac busy", "needs Linux paths", 'needs "Linux" paths')
        for number, reason in enumerate(reasons):
            self.write(self.mac_call(str(number), "declined", "local_flag", local_reason=reason))
        output = self.report()
        self.assertIn("Local-flag runs: 4; text says the Mac was unavailable: 2.", output)
        self.assertIn('- 1: needs "Linux" paths', output)
        self.assertLess(output.index("- 2: Mac busy"), output.index("- 1: needs Linux paths"))
        for reason in ("Mac unavailable", "MAC BLOCKED", "unreachable Mac", "Mac offline", "Mac not available", "Mac not reachable", "backoff", "battery low", "SSH failed", "connection refused", "SSH timed out", "cannot reach the Mac", "could not connect to Mac"):
            with self.subTest(reason=reason):
                self.assertTrue(mac_report.local_reason_says_mac_unavailable(reason))
        self.assertFalse(mac_report.local_reason_says_mac_unavailable("needs Linux filesystem semantics"))

    def test_local_reasons_print_only_the_ten_most_repeated_with_the_rest_counted(self) -> None:
        for number in range(12):
            for repeat in range(12 - number):
                self.write(self.mac_call(f"reason-{number}-{repeat}", "declined", "local_flag", local_reason=f"reason {number:02d}"))
        output = self.report()
        reasons = [line for line in output.splitlines() if line.startswith("- ")]
        self.assertEqual(len(reasons), 10)
        self.assertEqual(reasons[0], "- 12: reason 00")
        self.assertEqual(reasons[-1], "- 3: reason 09")
        self.assertIn("Other local reasons: 2 reasons, 3 runs.", output)
        self.assertNotIn("- 2: reason 10", output)

    def test_ten_local_reasons_have_no_remaining_reasons_line(self) -> None:
        for number in range(10):
            self.write(self.mac_call(str(number), "declined", "local_flag", local_reason=str(number)))
        self.assertNotIn("Other local reasons:", self.report())

    def test_remaining_local_reasons_use_singular_words_for_each_count_of_one(self) -> None:
        for number in range(10):
            for repeat in range(3):
                self.write(self.mac_call(f"main-{number}-{repeat}", "declined", "local_flag", local_reason=f"reason {number}"))
        self.write(self.mac_call("last", "declined", "local_flag", local_reason="last reason"))
        self.assertIn("Other local reasons: 1 reason, 1 run.", self.report())
        self.write(self.mac_call("last-repeat", "declined", "local_flag", local_reason="last reason"))
        self.assertIn("Other local reasons: 1 reason, 2 runs.", self.report())

    def test_decline_clusters_split_at_another_candidate_and_skip_non_candidates(self) -> None:
        records: list[Record] = []
        answers = (("declined", "blocked"), ("declined", "local_flag"), ("declined", "blocked"), ("passed", ""), ("declined", "blocked"), ("declined", "blocked"), ("declined", "unreachable"), ("declined", "backoff"), ("declined", "unreachable"))
        for number, (mac, reason) in enumerate(answers):
            at = store.utc_iso((NOW - timedelta(hours=3) + timedelta(minutes=10 * number)).timestamp())
            record = call(str(number), started_at=at, mac=mac, mac_reason=reason)
            if reason == "local_flag":
                record["local_reason"] = "needs Linux"
            else:
                record["mac_s"] = 1
            records.append(record)
        self.write(*records)
        _ = index.update()
        with closing(index.read_only()) as connection:
            calls = mac_report.read_test_calls(connection, START, END)
            local_call = next(item for item in calls if item.call_id == "1")
            self.assertEqual(local_call.decision, mac_outcome.LocalRunRequested("needs Linux"))
            clusters = mac_report.decline_clusters(calls)
            stretches = mac_report.decline_stretches(clusters, START, END)
        self.assertEqual(stretches, mac_report.DeclineStretches(1800, 1200))
        output = self.report()
        self.assertIn("at least 0:30 (lower bound, from declines)", output)
        self.assertIn("at least 0:20 (lower bound, from declines)", output)

    def test_default_window_and_three_local_days_use_timezone_midnights(self) -> None:
        windows = mac_report.report_windows(NOW, 24)
        self.assertEqual((windows[0].start, windows[0].end), (START, END))
        self.assertEqual([window.label for window in windows], ["Last 24 hours", "2026-10-12", "2026-10-11", "2026-10-10"])
        self.assertEqual(windows[1].start, "2026-10-12T07:00:00.000Z")
        self.write(call("day-start", started_at=windows[1].start, mac="passed", mac_s=1))
        output = self.report()
        self.assertIn("through 2026-10-12 08:09 PDT", output)
        self.assertIn("| 2026-10-10 |", output)
        self.assertEqual(sum(line.startswith("| 2026-") for line in output.splitlines()), 3)

    def test_decline_cluster_crossing_local_midnight_contributes_to_both_days(self) -> None:
        self.write(
            call("first", started_at="2026-10-12T06:50:00.000Z", mac="declined", mac_reason="blocked", mac_s=1),
            call("local", started_at="2026-10-12T07:00:00.000Z", mac="declined", mac_reason="local_flag"),
            call("last", started_at="2026-10-12T07:10:00.000Z", mac="declined", mac_reason="blocked", mac_s=1),
        )
        rows = [line for line in self.report().splitlines() if line.startswith(("| 2026-10-11 |", "| 2026-10-12 |"))]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("at least 0:10 (lower bound, from declines)", row)

    def test_day_ranges_follow_timezone_changes_at_daylight_saving_boundary(self) -> None:
        windows = mac_report.report_windows(datetime(2026, 11, 2, 12, tzinfo=UTC), 24)
        sunday = windows[2]
        self.assertEqual(sunday.start, "2026-11-01T07:00:00.000Z")
        self.assertEqual(sunday.end, "2026-11-02T08:00:00.000Z")

    def test_custom_hours_limits_only_the_first_window(self) -> None:
        self.write(call("old", started_at=START, mac="passed", mac_s=1), self.mac_call("recent"))
        output = self.report(2)
        first = output.split("## Last three local days")[0]
        self.assertIn("## Last 2 hours", first)
        self.assertIn("Candidates: 1.", first)
        self.assertIn("| 2026-10-11 |", output)

    def test_old_null_columns_and_new_empty_target_have_named_python_values(self) -> None:
        self.write(self.mac_call("old"), self.mac_call("new", target="", hold_wait_s=0))
        _ = index.update()
        with closing(index.read_only()) as connection:
            calls = mac_report.read_test_calls(connection, START, END)
        by_id = {call.call_id: call for call in calls}
        self.assertIs(by_id["old"].target, mac_report.NotRecorded.VALUE)
        self.assertIs(by_id["old"].hold_wait_s, mac_report.NotRecorded.VALUE)
        self.assertEqual(by_id["new"].target, mac_report.RecordedTarget(""))
        self.assertEqual(by_id["new"].hold_wait_s, 0.0)
        self.assertIn("target not recorded: 1; held-target wait not recorded: 1", self.report())

    def test_interrupted_target_does_not_supply_a_mac_outcome(self) -> None:
        self.write(
            self.mac_call("first", "", outcome="interrupted", target="mac"),
            self.mac_call("retry", "declined", "busy", outcome="interrupted", target="mac"),
            self.mac_call("confirmation", "passed", outcome="interrupted", target="natedev"),
        )
        summary = self.summary()
        self.assertEqual((summary.candidates, summary.passed, summary.failed), (2, 1, 0))
        self.assertEqual(summary.excluded.no_mac_entry, 1)
        self.assertEqual(summary.declined["busy"], 1)

    def test_runner_and_ineligible_declines_need_no_mac_seconds(self) -> None:
        self.write(
            call("runner", started_at=START, mac="declined", mac_reason="runner"),
            call("repo", started_at=START, mac="declined", mac_reason="repo"),
            call("linux", started_at=START, mac="declined", mac_reason="linux_only"),
        )
        summary = self.summary()
        self.assertEqual(summary.candidates, 1)
        self.assertEqual(summary.declined["runner"], 1)
        self.assertEqual(summary.excluded.repo, 1)
        self.assertEqual(summary.excluded.linux_only, 1)

    def test_empty_log_shows_zero_counts_no_usable_candidates_and_closing_line(self) -> None:
        output = self.report()
        self.assertIn("Candidates: 0.", output)
        self.assertIn("Usable share: 0 of 0 finished on the Mac (no usable candidates)", output)
        self.assertIn("at least 0:00 (lower bound, from declines)", output)
        self.assertTrue(output.endswith("natedev holds: no event recorded yet"))
        self.assertIn("mac holds: no event recorded yet", output)
        self.assertNotIn("Not recorded yet:", output)
        self.assertIn("## Hold intervals\n\nnone\n", output)
        self.assertIn("| Blocked | natedev held |", output)

    def test_mac_report_cli_accepts_custom_hours_without_collecting_launches(self) -> None:
        import cli

        with patch.object(launches, "collect", side_effect=AssertionError("launch collection must not run")):
            with patch("builtins.print") as printed:
                self.assertEqual(cli.main(["mac-report", "--hours", "2"]), 0)
        self.assertIn("## Last 2 hours", str(printed.call_args))

    def test_mac_report_cli_rejects_invalid_hours_before_updating_index(self) -> None:
        import cli

        for args in (["--hours", "0"], ["--hours", "nan"], ["--hours", "-1"], ["--hours"], ["--day", "today"]):
            with self.subTest(args=args):
                with patch.object(index, "update", side_effect=AssertionError("invalid range must not read index")):
                    with patch("builtins.print") as printed:
                        self.assertEqual(cli.main(["mac-report", *args]), 2)
                self.assertIn("N must be a positive number", str(printed.call_args))

    def test_mac_report_cli_prints_a_readable_temporary_log_and_schema(self) -> None:
        now = datetime.now(UTC)
        self.write(
            call("pass", started_at=store.utc_iso((now - timedelta(minutes=3)).timestamp()), mac="passed", mac_s=10, target="mac", hold_wait_s=0.4),
            call("blocked-first", started_at=store.utc_iso((now - timedelta(minutes=2)).timestamp()), mac="declined", mac_reason="blocked", mac_s=1),
            call("blocked-last", started_at=store.utc_iso((now - timedelta(minutes=1)).timestamp()), mac="declined", mac_reason="blocked", mac_s=1),
        )
        for args in (["mac-report", "--hours", "2"], ["schema"]):
            result = subprocess.run(
                [sys.executable, str(CLI), *args], env={**os.environ, "BUILDLOG_DIR": str(self.root)},
                capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            if args[0] == "mac-report":
                self.assertIn("Usable share: 1 of 1", result.stdout)
                print(result.stdout)
            else:
                for line in result.stdout.splitlines():
                    if line.lstrip().startswith(("target ", "hold_wait_s ")):
                        print(line)
                self.assertIn("where the call was when it ended: mac, natedev, or empty", result.stdout)

    def test_busy_causes_split_by_the_recorded_holder_and_null_holder_is_named(self) -> None:
        self.write(self.mac_call("known", "declined", "busy", mac_holder="hana nextest"),
                   self.mac_call("lost-run", "declined", "busy", mac_holder="leftovers"),
                   self.mac_call("unreadable", "declined", "busy", mac_holder="unreadable"),
                   self.mac_call("old", "declined", "busy"))
        output = self.report()
        self.assertIn("Largest causes of a usable Mac going unused: busy: 4.", output)
        for name in ("hana nextest", "lost-run leftovers", "holder unreadable", "holder not recorded"):
            self.assertIn(f"- {name}: 1", output)
        self.assertIn("Usable share: 0 of 4", output)

    def test_a_known_job_named_holder_not_recorded_is_counted_separately_from_a_missing_holder(self) -> None:
        self.write(self.mac_call("known", "declined", "busy", mac_holder="holder not recorded"),
                   self.mac_call("old", "declined", "busy"))
        summary = self.summary()
        self.assertEqual(len(summary.busy_holders), 2)
        self.assertEqual(sorted(summary.busy_holders.values()), [1, 1])

    def test_mac_block_intervals_replace_decline_bounds_and_natedev_holds_use_the_union(self) -> None:
        self.write(hold_event("began", "mac-block", "2026-10-12T07:00:00Z", "mac"),
                   hold_event("ended", "mac-block", "2026-10-12T09:00:00Z", "mac"),
                   hold_event("began", "a", "2026-10-12T07:00:00Z"),
                   hold_event("ended", "a", "2026-10-12T09:00:00Z"),
                   hold_event("began", "b", "2026-10-12T08:00:00Z"),
                   hold_event("ended", "b", "2026-10-12T10:00:00Z"))
        output = self.report()
        day_line = next(line for line in output.splitlines() if line.startswith("| 2026-10-12 |"))
        self.assertIn("| 2:00 | 3:00 | at least 0:00", day_line)
        self.assertIn("mac holds recorded since 2026-10-12 00:00 PDT: 1 intervals", output)
        self.assertIn("natedev holds recorded since 2026-10-12 00:00 PDT: 2 intervals", output)
        previous = next(line for line in output.splitlines() if line.startswith("| 2026-10-11 |"))
        self.assertIn("| not recorded |", previous)

    def test_beginnings_before_the_first_window_are_read_and_missing_events_mark_totals(self) -> None:
        since = datetime(2026, 10, 8, 7, tzinfo=UTC)
        write_mac_block(since)
        self.write(hold_event("began", since.isoformat(), since.isoformat(), "mac", store.host_name()),
                   hold_event("began", "missing-end", "2026-10-11T08:00:00Z", "mac"),
                   hold_event("ended", "missing-begin", "2026-10-12T08:00:00Z"))
        output = self.report()
        day_line = next(line for line in output.splitlines() if line.startswith("| 2026-10-12 |"))
        self.assertIn("| at least 8:09 | at least 0:00 |", day_line)
        self.assertIn("to open", output)
        self.assertIn("to end not recorded", output)
        self.assertIn("began not recorded to", output)
        self.assertIn("Now: Mac blocked by hana for render since 2026-10-08 00:00 PDT", output)
        self.assertNotIn("Not recorded yet:", output)

    def test_mac_report_cli_work_grows_near_linearly_at_the_log_size(self) -> None:
        now = datetime.now(UTC)
        began = (now - timedelta(hours=20)).replace(minute=0, second=0, microsecond=0)
        with self.log.open("wb") as handle:
            for number in range(29_138):
                at = store.utc_iso((began + timedelta(seconds=number * 2)).timestamp())
                reason = "blocked" if number < 100 else "busy" if number % 3 else ""
                mac = "failed" if 100 <= number < 200 else "declined" if reason else "passed"
                if mac == "failed":
                    reason = ""
                _ = handle.write(encode(call(
                    f"call-{number}", started_at=at, mac=mac,
                    mac_reason=reason, mac_s=1, target="natedev", hold_wait_s=0,
                )))
            for number in range(36_462):
                at = store.utc_iso((began + timedelta(seconds=number)).timestamp())
                _ = handle.write(encode(step(
                    f"step-{number}", started_at=at, call_id=f"call-{number % 29_138}",
                    step="nextest", duration_s=1,
                    caller="verify-mac" if 100 <= number < 200 else "verify",
                    tree_key=f"tree-{number % 100}", status=1 if 100 <= number < 200 else 0,
                )))
            hold_rows = timed_hold_events(began, now)
            for hold_row in hold_rows:
                _ = handle.write(encode(hold_row))
        self.assertEqual(index.update(), 65_600 + len(hold_rows))
        full_rows = self.log.read_bytes().splitlines(keepends=True)
        quarter_rows = full_rows[:29_138:4] + full_rows[29_138:65_600:4] + full_rows[65_600:]
        environment = {**os.environ, "BUILDLOG_DIR": str(self.root), "TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles"}
        runs: list[list[MeasuredCliRun]] = []
        for rows in (quarter_rows, full_rows):
            _ = self.log.write_bytes(b"".join(rows))
            _ = index.reindex()
            runs.append([measure_cli_work("mac-report", environment, len(rows)) for _ in range(3)])
        for measured in [*runs[0], *runs[1]]:
            self.assertEqual(measured.result.returncode, 0, measured.result.stderr)
            self.assertEqual(measured.work["index_updates"], 1)
            self.assertEqual(measured.work["launch_collections"], 0)
            self.assertLessEqual(measured.work["statements"], 4)
            self.assertEqual(measured.work["step_reads"], 1)
        baseline = min(run.work["cpu_seconds"] for run in runs[0])
        full = min(run.work["cpu_seconds"] for run in runs[1])
        self.assertLessEqual(full, baseline * 8, f"CLI CPU grew from {baseline} to {full} for four times the rows")
        baseline_instructions = min(run.work["instructions"] for run in runs[0])
        self.assertLessEqual(min(run.work["instructions"] for run in runs[1]), baseline_instructions * 8)
        result = runs[1][-1].result
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usable share:", result.stdout)
        self.assertIn("Set aside as not usable: blocked, backoff, unreachable, battery, disk, off", result.stdout)
        self.assertIn("Counted against the share: busy, mac_busy, lost, runner, state, copy.", result.stdout)
        self.assertIn("mac holds recorded since", result.stdout)
        self.assertIn("Mac failures with a natedev pass of the same tree: 100 recorded.", result.stdout)
        self.assertIn("natedev holds recorded since", result.stdout)
        self.assertNotIn("Not recorded yet:", result.stdout)
        self.assertIn("to open", result.stdout)
        self.assertIn("to end not recorded", result.stdout)
        day = began.astimezone(user_zone()).date().isoformat()
        day_line = next(line for line in result.stdout.splitlines() if line.startswith(f"| {day} |"))
        cells = [cell.strip() for cell in day_line.split("|")[1:-1]]
        for cell in cells[9:11]:
            duration = cell.removeprefix("at least ")
            hours, minutes = duration.split(":")
            self.assertGreater(int(hours) * 60 + int(minutes), 0, day_line)



if __name__ == "__main__":
    _ = unittest.main()
