"""Tests for the showrunner's units table: session, current phase and task of each live unit."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

import live_units
import showrunners
import unit_lookup
from herdr import HerdrPlace
import units_table

# Importable once units_table has put scripts/direct on the path.
import phase_table
from scripts.direct import plan_history


def unit(name: str, claude: unit_lookup.Claude | None) -> live_units.LiveUnit:
    if claude is None:
        return live_units.LiveUnit(name, None)
    return live_units.LiveUnit(name, unit_lookup.MarkedUnit(name, claude, HerdrPlace(f"label-of-{name}", "w1:p1")))


def live(name: str) -> unit_lookup.LiveClaude:
    return unit_lookup.LiveClaude(name, f"{name}-id", f"/run/{name}.sock", 1)


def phase_rows(*statuses: str) -> list[object]:
    return [{"phase": str(index + 1), "status": status} for index, status in enumerate(statuses)]


def open_record(plan: Path) -> dict[str, object]:
    return {"plan": str(plan), "current": {"phase": "3", "of": 7, "title": "Every session records itself"},
            "plan_finish": None, "phases": phase_rows("done", "done", "running", "todo")}


def event(kind: str, **fields: str) -> str:
    return json.dumps({"event_type": kind, **fields})


class UnitsTableTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def run_file(self, *events: str) -> Path:
        path = self.root / "run.jsonl"
        _ = path.write_text("\n".join(events) + "\n", encoding="utf-8")
        return path

    def task(self, *events: str) -> str:
        record = open_record(self.root / "plan.md")
        with patch.object(plan_history, "plan_runs", return_value=[self.run_file(*events)]):
            return units_table.task_cell(record)

    def test_session_names_the_live_claude_or_says_why_there_is_none(self) -> None:
        self.assertEqual(units_table.session_cell(unit("a", live("alpha"))), "alpha")
        self.assertEqual(units_table.session_cell(unit("a", None)), "gone")
        self.assertEqual(units_table.session_cell(unit("a", unit_lookup.ClaudeNotRunning())), "not running")
        self.assertEqual(units_table.session_cell(unit("a", unit_lookup.ClaudeUnknown("no records"))), "unknown")

    def test_phase_names_the_open_phase_or_where_the_plan_stands(self) -> None:
        cases: list[tuple[object, str]] = [
            (open_record(self.root / "plan.md"), "3 of 7 · Every session records itself"),
            ({"current": None, "phases": phase_rows("done", "done", "todo")}, "between phases, 2 of 3 done"),
            ({"current": None, "phases": phase_rows("todo", "todo")}, "not started"),
            ({"current": None, "phases": phase_rows("done", "done")}, "plan done"),
            ({"current": None, "phases": []}, "no phases"),
            ({"unavailable": "Unable to read plan"}, "no plan"),
            (None, "—"),
        ]
        for record, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(units_table.phase_cell(record), expected)

    def test_task_is_the_newest_open_activity_before_the_open_pass(self) -> None:
        self.assertEqual(self.task(
            event("phase_started"),
            event("pass_started", pass_instance_id="p1", pass_kind="impl", pass_activity="writing the table"),
            event("activity_started", activity_instance_id="a1", activity_label="Verification",
                  activity_text="verify.sh test"),
        ), "Verification: verify.sh test")
        # A finished activity leaves the pass it ran under.
        self.assertEqual(self.task(
            event("phase_started"),
            event("pass_started", pass_instance_id="p1", pass_kind="impl", pass_activity="writing the table"),
            event("activity_started", activity_instance_id="a1", activity_label="Verification", activity_text="v"),
            event("activity_finished", activity_instance_id="a1"),
        ), "impl: writing the table")

    def test_no_task_is_shown_once_its_step_or_phase_has_closed(self) -> None:
        self.assertEqual(self.task(
            event("phase_started"),
            event("pass_started", pass_instance_id="p1", pass_kind="impl", pass_activity="writing"),
            event("pass_finished", pass_instance_id="p1"),
        ), "—")
        self.assertEqual(self.task(
            event("phase_started"),
            event("pass_started", pass_instance_id="p1", pass_kind="impl", pass_activity="writing"),
            event("phase_finished"),
            event("phase_started"),
        ), "—")

    def test_a_long_task_is_cut_so_the_table_stays_narrow(self) -> None:
        task = self.task(
            event("phase_started"),
            event("activity_started", activity_instance_id="a1", activity_label="Verification",
                  activity_text="verify.sh test hana " + "--filter one_long_test_name " * 10),
        )
        self.assertEqual(len(task), units_table.TASK_WIDTH)
        self.assertTrue(task.startswith("Verification: verify.sh test hana --filter"))
        self.assertTrue(task.endswith("…"))

    def test_a_unit_with_no_open_phase_shows_no_task(self) -> None:
        with patch.object(plan_history, "plan_runs") as runs:
            self.assertEqual(units_table.task_cell({"plan": "/p.md", "current": None, "phases": []}), "—")
        runs.assert_not_called()

    def test_table_has_one_row_per_unit_and_escapes_pipes(self) -> None:
        records: dict[str, object] = {"one-unit": {"current": None, "phases": phase_rows("todo")}}
        self.assertEqual(units_table.table([unit("one-unit", live("a|b")), unit("two-unit", None)], records), "\n".join([
            "| Unit | Session | Phase | Task |",
            "| --- | --- | --- | --- |",
            "| one-unit | a\\|b | not started | — |",
            "| two-unit | gone | — | — |",
        ]))

    def test_without_a_production_the_callers_timer_names_it(self) -> None:
        doc = self.root / "show-production.md"
        out = io.StringIO()
        with (patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "caller"}),
              patch.object(showrunners, "production_of", return_value=doc) as found,
              patch.object(live_units, "production_units", return_value=[unit("a-unit", live("a"))]),
              patch.object(phase_table, "show_production", return_value="{}"),
              contextlib.redirect_stdout(out)):
            self.assertEqual(units_table.main([]), 0)
        found.assert_called_once_with("caller")
        self.assertIn("| a-unit | a | — | — |", out.getvalue())

    def test_a_session_that_is_no_showrunner_is_told_to_name_the_production(self) -> None:
        err = io.StringIO()
        with (patch.object(showrunners, "production_of", return_value=None),
              contextlib.redirect_stderr(err)):
            self.assertEqual(units_table.main([]), 1)
        self.assertIn("pass --production", err.getvalue())

    def test_production_of_reads_the_timer_addressed_to_the_session(self) -> None:
        doc = self.root / "show-production.md"
        for name, target in (("showrunner-other", "session:someone-else"), ("showrunner-show", "session:caller")):
            directory = self.root / name
            directory.mkdir()
            _ = (directory / "conf").write_text(
                f"TARGET={target}\nCHECK=zsh /opt/production_check.sh {doc}\n", encoding="utf-8")
        self.assertEqual(showrunners.production_of("caller", self.root), doc)
        self.assertIsNone(showrunners.production_of("nobody", self.root))
        self.assertIsNone(showrunners.production_of("", self.root))


if __name__ == "__main__":
    _ = unittest.main()
