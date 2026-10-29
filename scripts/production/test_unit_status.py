"""Unit-status integration tests, grouped by subject."""
from __future__ import annotations

from scripts.production.unit_status_test_support import UnitStatusFixture, setUpModule as setUpModule


class UnitStatusTests(UnitStatusFixture):
    def test_blocks_are_headed_by_the_unit_ids_of_the_registered_doc(self) -> None:
        output, _ = self.run_status(None, "ok", 0)
        self.assertIn("== hook", output)

    def test_showrunner_form_omits_retired_unit(self) -> None:
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": "— holding: active\n", "stalls": "— holding: old\n"},),
            units=("hook", "stalls"), retired_units=("stalls",),
        )
        self.assertIn("== hook", outputs[0])
        self.assertNotIn("== stalls", outputs[0])
        self.assertNotIn("SESSION GONE", outputs[0])

    def test_idle_run_reports_failed_tick_health(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "failing: no instance", 1)
        self.assertIn("TICKS FAILING (no instance)", output)
        self.assertEqual(calls, "health unit-test-run\n")

    def test_healthy_run_has_no_tick_warning(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0)
        self.assertNotIn("TICKS FAILING", output)
        self.assertEqual(calls, "health unit-test-run\n")

    def test_missing_active_marker_skips_health(self) -> None:
        output, calls = self.run_status(None, "failing: no instance", 1)
        self.assertNotIn("TICKS FAILING", output)
        self.assertIsNone(calls)

    def test_empty_active_marker_skips_health(self) -> None:
        output, calls = self.run_status("", "failing: no instance", 1)
        self.assertNotIn("TICKS FAILING", output)
        self.assertIsNone(calls)

    def test_claude_session_id_comes_from_lookup_even_with_a_different_name(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0)
        self.assertNotIn("CLAUDE NOT RUNNING", output)
        self.assertEqual(calls, "health unit-test-run\n")

    def test_no_live_claude_record_reports_not_running(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0, stopped_units=("hook",))
        self.assertIn("CLAUDE NOT RUNNING", output)
        self.assertNotIn("CLAUDE STATE UNKNOWN", output)
        self.assertIsNone(calls)

    def test_unreadable_claude_records_report_unknown_instead_of_not_running(self) -> None:
        outputs, calls, _ = self.run_statuses(
            "/tmp/test-run\n", "ok", 0, panes=({"hook": "— holding: waiting on x\n"},),
            unreadable_units=("hook",),
        )
        self.assertIn("CLAUDE STATE UNKNOWN", outputs[0].splitlines())
        self.assertNotIn("CLAUDE NOT RUNNING", outputs[0])
        self.assertIsNone(calls)
