"""Exercise the usage collector against invented source files."""

from __future__ import annotations

import unittest
from typing import final

from scripts.usage.collect_test_support import MacCollectorFixture, SshBehavior


@final
class MacCollectTests(MacCollectorFixture):
    def test_ssh_first_nonempty_stderr_line_is_the_reason_even_when_the_pipe_breaks(self) -> None:
        for behavior in ("unreachable", "early_disconnect"):
            with self.subTest(behavior=behavior):
                run = self.mac_collect(behavior)
                for line in run.output.splitlines():
                    if line.startswith("mac "):
                        self.assertIn("fixture SSH is unreachable", line)
                        self.assertNotIn("Broken pipe", line)
                        self.assertNotIn("second fixture diagnostic", line)
                self.assert_source_states(run.output, "mac", "unreachable")

    def test_cut_after_first_cursor_keeps_file_and_resume_adds_only_remaining_turns(self) -> None:
        cut = self.mac_collect("cut_after_first_cursor")
        self.assertEqual(cut.stream["cursor_records"], 1)
        self.assert_first_file_is_committed()
        self.assert_source_additions(cut.output, "claude", 2, "mac")
        self.assert_source_additions(cut.output, "codex", 0, "mac")
        self.assert_source_states(cut.output, "mac", "partial")
        self.assert_source_states(cut.output, "natedev", "ready")
        self.assertEqual(self.complete_times("mac"), [("claude", None), ("codex", None)])
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "ready")])
        resumed = self.mac_collect()
        self.assert_source_additions(resumed.output, "claude", 2, "mac")
        self.assert_source_additions(resumed.output, "codex", 2, "mac")
        self.assertEqual(resumed.stream["cursor_records"], 3)
        self.assert_mac_fixture_totals()
        self.assert_source_states(resumed.output, "mac", "ready")
        snapshot = self.all_values(self.mac_rows("turns"))
        repeated = self.mac_collect()
        self.assertEqual(repeated.stream["measurement_records"], 0)
        self.assertEqual(repeated.stream["cursor_records"], 0)
        self.assertEqual(self.all_values(self.mac_rows("turns")), snapshot)

    def test_cut_inside_second_file_rolls_back_new_rows_merges_compactions_and_cursor(self) -> None:
        cut = self.mac_collect("cut_inside_second_file")
        self.assertEqual(cut.stream["cursor_records"], 1)
        self.assertEqual(cut.stream["turn_records"], 4)
        self.assert_first_file_is_committed()
        self.assert_source_additions(cut.output, "claude", 2, "mac")
        self.assert_source_states(cut.output, "mac", "partial")
        resumed = self.mac_collect()
        self.assert_source_additions(resumed.output, "claude", 2, "mac")
        self.assert_mac_fixture_totals()
        self.assertEqual(len(self.mac_rows("compactions")), 4)
        self.assertEqual(len(self.mac_rows("cursors")), 4)

    def test_mac_failures_before_any_file_record_unreachable_and_leave_no_turns(self) -> None:
        behaviors: tuple[SshBehavior, ...] = ("unreachable", "python_error")
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                self.db_path = self.state / f"{behavior}.db"
                run = self.mac_collect(behavior)
                self.assertEqual(self.mac_rows("turns"), [])
                self.assertEqual(self.mac_rows("compactions"), [])
                self.assertEqual(self.mac_rows("cursors"), [])
                self.assert_source_states(run.output, "mac", "unreachable")
                self.assert_source_states(run.output, "natedev", "ready")
                self.assert_source_additions(run.output, "claude", 0, "mac")
                self.assert_source_additions(run.output, "codex", 0, "mac")
                self.assertEqual(self.complete_times("mac"), [("claude", None), ("codex", None)])
                self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(0, "unreachable")])
                self.assertTrue(any(line.startswith("mac agents:") and "unreachable" in line
                                    for line in run.output.splitlines()), run.output)

    def test_unreachable_mac_keeps_prior_rows_diagnostics_and_complete_times(self) -> None:
        _ = self.mac_collect()
        snapshot = {table: self.all_values(self.mac_rows(table)) for table in ("turns", "compactions", "cursors")}
        complete_times = self.complete_times("mac")
        run = self.mac_collect("unreachable")
        for table, before in snapshot.items():
            self.assertEqual(self.all_values(self.mac_rows(table)), before, table)
        self.assert_source_states(run.output, "mac", "unreachable")
        self.assertEqual(self.complete_times("mac"), complete_times)
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "unreachable")])
        self.assert_source_additions(run.output, "claude", 0, "mac")
        self.assert_source_additions(run.output, "codex", 0, "mac")

    def test_invalid_remote_measurements_end_pass_without_defaults_or_second_file_commit(self) -> None:
        behaviors: tuple[SshBehavior, ...] = ("malformed_record", "unknown_measurement", "missing_output",
                                              "invalid_activity", "wait_without_action", "missing_activity")
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                self.db_path = self.state / f"{behavior}.db"
                run = self.mac_collect(behavior)
                self.assert_first_file_is_committed()
                self.assert_source_states(run.output, "mac", "partial")
                self.assert_source_additions(run.output, "claude", 2, "mac")
                self.assert_source_additions(run.output, "codex", 0, "mac")
                self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "ready")])
                if behavior == "malformed_record":
                    self.assertIn("partial (invalid remote stream)", run.output)
                if behavior in ("invalid_activity", "wait_without_action", "missing_activity"):
                    self.assertIn("activity", run.output)

    def test_oversized_wire_count_ends_pass_and_records_sources_without_second_file_commit(self) -> None:
        run = self.mac_collect("oversized_output")
        self.assert_first_file_is_committed()
        self.assert_source_states(run.output, "mac", "partial")
        self.assert_source_states(run.output, "natedev", "ready")
        self.assert_source_additions(run.output, "claude", 2, "mac")
        self.assert_source_additions(run.output, "codex", 0, "mac")
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "ready")])

    def test_cut_inside_record_reports_connection_cut_and_keeps_only_finished_file(self) -> None:
        run = self.mac_collect("cut_inside_record")
        self.assert_first_file_is_committed()
        self.assert_source_states(run.output, "mac", "partial")
        for family in ("claude", "codex"):
            lines = [line for line in run.output.splitlines() if line.startswith(f"mac {family}:")]
            self.assertRegex(lines[0], r"\bcut\b")
            self.assertNotIn("invalid remote stream", lines[0])



if __name__ == "__main__":
    _ = unittest.main()
