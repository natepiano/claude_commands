"""Tests for each unit's staff name, phase and task."""
from __future__ import annotations

from scripts.agents import remote

import contextlib
import io
import json
import os
import re
import sqlite3
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from dataclasses import replace
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import Mock, patch


from scripts.production import census, claude_table_layout, display_cells, live_units, unit_queue
from scripts.accounts import session_state
from scripts.lib import conf, unit_runs
from scripts.lib import names as display_names
from scripts.lib.terminal_cells import terminal_width
from scripts.message import sessions
from scripts.production import showrunners
from scripts.production import unit_lookup
from scripts.herdr.herdr import HerdrPlace
from scripts.production import unit_phases
from scripts.production.production_doc import read_production
from scripts.production import style_units
from scripts.style import style_ledger
from scripts.style.ledger_test_support import write_style_units
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member, fixture_runtime_id, recorded_unit

from scripts.direct import phase_table
from scripts.direct import plan_history


def unit(name: str, claude: unit_lookup.Claude | None) -> live_units.LiveUnit:
    member = next((row for row in staff.snapshot().rows if row.kind == "unit" and row.name == name),
                  session_state.UnrecordedStaff())
    activity = (session_state.LiveActivity("idle") if isinstance(claude, unit_lookup.LiveClaude)
                else session_state.NoLiveSession())
    observed = unit_lookup.LabeledSession(name, claude, HerdrPlace(f"label-of-{name}", "w1:p1")) if claude is not None else None
    machine = member.worktree.machine if isinstance(member, staff.StaffRow) and isinstance(member.worktree, staff.PresentWorktree) else ""
    return live_units.LiveUnit(name, observed, status=session_state.classify(member, activity), member=member, machine=machine)


def live(name: str) -> unit_lookup.LiveClaude:
    return unit_lookup.LiveClaude(name, f"{name}-id", f"/run/{name}.sock", 1)


def phase_rows(*statuses: str) -> list[object]:
    return [{"phase": str(index + 1), "title": f"Delivery {index + 1}", "status": status} for index, status in enumerate(statuses)]


def typed_record(plan: Path, *statuses: str) -> phase_table.PhaseRecord:
    now = datetime(2026, 10, 16, 18, tzinfo=UTC)
    rows: list[phase_table.DonePhase | phase_table.OpenPhase | phase_table.TodoPhase] = []
    current: phase_table.OpenPhase | phase_table.NoOpenPhase = phase_table.NoOpenPhase()
    for index, status in enumerate(statuses, 1):
        if status == "done":
            rows.append(phase_table.DonePhase(str(index), f"Delivery {index}", phase_table.PhaseTimingNotRecorded()))
        elif status == "running":
            current = phase_table.OpenPhase(str(index), f"Delivery {index}", now,
                phase_table.PhaseProgressNotReported(), phase_table.EtaUnavailable(),
                phase_table.EtaNeverStated(), f"instance-{index}")
            rows.append(current)
        else:
            rows.append(phase_table.TodoPhase(str(index), f"Delivery {index}", phase_table.PhaseTimingNotPredicted()))
    return phase_table.PhaseRecord(plan, now, current, rows, phase_table.UnknownFinish())


def open_record(plan: Path) -> phase_table.PhaseRecord:
    record = typed_record(plan, "done", "done", "running", "todo", "todo", "todo", "todo")
    current = record.current
    assert isinstance(current, phase_table.OpenPhase)
    current = replace(current, title="Every session records itself")
    return record._replace(current=current, phases=[*record.phases[:2], current, *record.phases[3:]])


def event(kind: str, **fields: str) -> str:
    return json.dumps({"event_type": kind, **fields})


LOCAL = census.Census((census.CensusMachine("natedev", True),), (), ())


def session_of(member: staff.StaffRow, status: str | None) -> census.CensusSession:
    return census.CensusSession(member.current_session, status)


def census_worker(name: str, parent: staff.StaffId, *, status: str | None = "busy", phase: str = "",
                  runs: Sequence[census.CensusRun] = ()) -> census.CensusWorker:
    assignment = census.CensusPass("impl", phase) if phase else census.NoPass()
    return census.CensusWorker(staff.StaffId(f"{name}-id"), name, parent, status, assignment, tuple(runs))


def tiles(*, sessions: Sequence[census.CensusSession] = (),
          workers: Sequence[census.CensusWorker] = ()) -> census.Census:
    return LOCAL._replace(sessions=tuple(sessions), workers=tuple(workers))


def census_for(units: Sequence[live_units.LiveUnit]) -> census.Census:
    """List each recorded unit that is not exited, with the status word of its live classification."""
    return tiles(sessions=[session_of(selected.member, selected.status) for selected in units
                           if isinstance(selected.member, staff.StaffRow) and selected.status != "exited"])


def table_of(units: list[live_units.LiveUnit], records: dict[str, object]) -> str:
    return unit_phases.table(units, records, census_read=census_for(units))


class UnitPhasesTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(unit_runs, "RUN_ROOT", self.root / "runs"))
        config = self.root / "table-conf"
        _ = cast(object, shutil.copytree(Path(__file__).resolve().parents[2] / "config", config))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config)})))
        _ = self.enterContext(patch("socket.gethostname", return_value="natedev"))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        from scripts.message import process_table
        sessions = self.root / "sessions"
        sessions.mkdir()
        self.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(sessions),
                                                    "UNIT_HISTORY_DIR": str(self.root / "history"),
                                                    "UNIT_QUEUE_STATE_DIR": str(self.root / "queue")}))
        _ = self.enterContext(patch.object(process_table, "snapshot", return_value={}))
        _ = self.enterContext(patch.object(census, "read", return_value=LOCAL))

    def test_unit_count_cache_filters_sources_and_retries_failed_reads(self) -> None:
        from scripts.statusline import counts_row
        from scripts.production import session_staff

        doc = self.root / "production.md"
        ledger = self.root / "efficiency.md"
        _ = doc.write_text("# Production\n\n> **Status: PRODUCTION — running**\n"
                           + f"- **Efficiency ledger:** {ledger}\n")
        owner = fixture_member("showrunner", name="cache-owner", doc=staff.LiveDoc("natedev", doc))
        member = fixture_member("unit", name="cache-unit", parent=owner.id)
        _ = ledger.write_text(f"- 2026-11-12 config: Own [by: {member.id}] — proposed\n"
                              + f"- 2026-11-12 config: Other [by: {owner.id}] — proposed\n")
        notifier = self.root / "notifier"
        instance = notifier / f"showrunner-{owner.id}"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:{owner.current_session}\nCHECK=zsh /x/production_check.sh {owner.id}\n")
        _ = (self.root / "table-conf/statusline.conf").write_text("# whole: 1..\ncounts.retry_seconds=7\n")
        environment = {"HOME": str(self.root), "NOTIFIER_STATE_DIR": str(notifier),
                       "SHOWRUNNER_STATE_DIR": str(self.root / "state"),
                       "CHANGE_REVIEW_DIR": str(self.root), "REVIEW_LINKS_ROOT": str(self.root / "links")}
        read_text = Path.read_text

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == ledger:
                raise PermissionError(str(path))
            return read_text(path, encoding=encoding, errors=errors)

        with (patch.dict(os.environ, environment),
              patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(()))):
            cache = counts_row.write_unit(member.id)
            self.assertEqual(cache.read_text().splitlines()[:2], ["For you: 1 efficiency", ""])
            with patch.object(Path, "read_text", read), patch.object(time, "time", return_value=1000):
                cache = counts_row.write_unit(member.id)
            line, due, *sources = cache.read_text().splitlines()
            self.assertEqual((line, due), ("", "1007"))
            self.assertIn(str(ledger), sources)
            self.assertIn(str(doc), sources)
            self.assertIn(f"source=unit:{member.id}", sources)
            cache = counts_row.write_unit(member.id)
            self.assertEqual(cache.read_text().splitlines()[:2], ["For you: 1 efficiency", ""])

    def run_file(self, *events: str) -> Path:
        path = self.root / "run.jsonl"
        _ = path.write_text("\n".join(events) + "\n", encoding="utf-8")
        return path

    def task(self, *events: str) -> str:
        record = open_record(self.root / "plan.md")
        with patch.object(plan_history, "plan_runs", return_value=[self.run_file(*events)]):
            return unit_phases.task_cell(record)

    def test_unit_name_comes_from_staff_for_every_session_state(self) -> None:
        owner = fixture_member("showrunner", name="table-owner")
        _ = fixture_member("unit", name="namecheck", parent=owner.id)
        for session in (None, live("another-session"), unit_lookup.ClaudeNotRunning(),
                        unit_lookup.ClaudeUnknown("no records")):
            with self.subTest(session=session):
                rendered = table_of([unit("namecheck", session)], {})
                expected = "idle" if isinstance(session, unit_lookup.LiveClaude) else "exited"
                self.assertEqual(rendered.splitlines()[0], "| Unit | Status |")
                self.assertEqual(rendered.splitlines()[2], f"| namecheck | {expected} |")
                self.assertNotIn("Progress:", rendered)

    def test_fitted_table_preserves_fixed_cells_and_cuts_text_in_order(self) -> None:
        row = ("unit", "busy", "🟩" * 20, "12/20", "N" * 24, "18:10", "19:00",
               "W" * 34, "O" * 32, "Q" * 30)
        for worker_heading, columns, widths in (("Workers", 226, (30, 32, 34, 24)),
                                                ("Workers", 210, (14, 32, 34, 24)),
                                                ("Workers", 190, (6, 20, 34, 24)),
                                                ("Workers", 170, (6, 14, 20, 24)),
                                                ("Workers", 150, (6, 14, 7, 17)),
                                                ("Workers", 40, (6, 14, 7, 5)),
                                                ("Notes", 170, (6, 14, 20, 24))):
            with self.subTest(worker_heading=worker_heading, columns=columns):
                headers = ("Unit", "Status", "Progress", "Phase", "Notes", "ETA Next", "ETA Plan",
                           worker_heading, "Non-Phase Work", "Queued")
                titles, rows = display_cells.fitted(headers, [row], columns=columns,
                    cuttable=(9, 8, 7, 4))
                self.assertEqual(titles, headers)
                self.assertEqual(rows[0][:4], row[:4])
                self.assertEqual(rows[0][5:7], row[5:7])
                for index, width in zip((9, 8, 7, 4), widths, strict=True):
                    original = row[index]
                    self.assertEqual(rows[0][index], original if len(original) == width else original[:width - 1] + "…")
                drawn = 1 + sum(max(3, terminal_width(title), terminal_width(cell)) + 3
                                for title, cell in zip(titles, rows[0], strict=True))
                if columns == 40:
                    self.assertEqual(drawn, 134)
                else:
                    self.assertLessEqual(drawn, columns - 4)

    def test_fitted_table_keeps_wide_characters_and_strips_spaces_before_the_ellipsis(self) -> None:
        for text, columns, expected in (("a界界界", 12, "a界…"), ("界界界", 12, "界…"),
                                        ("a   bcdef", 12, "a…"), ("a|bcdef", 12, "a|b…")):
            with self.subTest(text=text):
                titles, rows = display_cells.fitted(("Note",), [(text,), ("ok",)], cuttable=(0,), columns=columns)
                self.assertEqual(titles, ("Note",))
                self.assertEqual(rows, [(expected,), ("ok",)])

    def test_fitted_table_omits_only_columns_empty_in_every_row(self) -> None:
        headers = ("Unit", "Workers", "Queued")
        rows = [("one", "—", "—"), ("two", "—", "Review")]
        self.assertEqual(display_cells.fitted(headers, rows, cuttable=(2,), columns=170),
                         (("Unit", "Queued"), [("one", "—"), ("two", "Review")]))
        self.assertEqual(display_cells.fitted(headers, [], cuttable=(2,), columns=170),
                         (headers, []))
        self.assertEqual(display_cells.fitted(headers, [*rows, ("three", "—", "Review delivery")],
                                              cuttable=(1, 2), columns=25),
                         (("Unit", "Queued"), [("one", "—"), ("two", "Review"), ("three", "Review d…")]))

    def test_phase_names_the_open_phase_or_where_the_plan_stands(self) -> None:
        plan = self.root / "outside-plan.md"
        _ = plan.write_text("# Plan\n### Phase 1 — Planned work · status: todo\n")
        owner = fixture_member("showrunner", name="outside")
        member = fixture_member("unit", name="outside", parent=owner.id,
                                worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        history = self.root / "history" / "runs"
        history.mkdir(parents=True)
        _ = (history / "outside.jsonl").write_text("\n".join([
            json.dumps({"event_type": "run_started", "working_dir": str(self.root),
                        "run_id": "outside", "owner_staff_id": member.id,
                        "plan_doc": plan.name, "run_started_at": 1}),
            json.dumps({"event_type": "phase_started", "phase_id": "ad-hoc", "phase_title": "Other work",
                        "phase_instance_id": "outside-instance", "phase_started_at": 2, "timestamp_epoch": 2})]) + "\n")
        run_folder = self.root / "runs" / "outside"
        run_folder.mkdir(parents=True)
        _ = staff.set_run(member.id, staff.OpenRun(run_folder))
        _ = (run_folder / plan_history.STATE_FILENAME).write_text(json.dumps({"owner_staff_id": member.id,
            "working_dir": str(self.root), "plan_doc": plan.name}))
        outside = cast(dict[str, object], json.loads(phase_table.show(run_folder, ZoneInfo("UTC"), json_output=True)))
        self.assertIsInstance(outside["current"], dict)
        cases: list[tuple[object, str, str]] = [
            (open_record(self.root / "plan.md"), "2/7", "phase 3 — Every session records itself"),
            ({"current": None, "phases": phase_rows("done", "done", "todo")}, "2/3", "between phases"),
            ({"current": None, "phases": phase_rows("todo", "todo")}, "0/2", "not started"),
            ({"current": None, "phases": phase_rows("done", "done")}, "2/2", "plan done"),
            ({"current": None, "phases": []}, "—", "no phases"),
            ({"unavailable": "Unable to read plan"}, "—", "no plan"),
            (None, "—", "—"),
            (outside, "0/1", "not started"),
        ]
        for record, count, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(unit_phases.phase_cell(record), count)
                self.assertEqual(unit_phases.notes_cell(record), expected)

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
        record = open_record(self.root / "plan.md")
        with patch.object(plan_history, "plan_runs", return_value=[self.run_file(
                event("phase_started"), event("pass_started", pass_instance_id="p1", pass_kind="impl",
                                              pass_activity="writing the table"))]):
            self.assertEqual(unit_phases.notes_cell(record), "phase 3 — impl: writing the table")

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

    def test_long_notes_preserve_the_complete_task_title_and_fix_rounds(self) -> None:
        activity = "verify.sh test hana " + "--filter one_long_test_name " * 10
        task = self.task(
            event("phase_started"),
            event("activity_started", activity_instance_id="a1", activity_label="Verification",
                  activity_text=activity),
        )
        self.assertEqual(task, "Verification: " + activity.strip())
        with patch.object(plan_history, "plan_runs", return_value=[self.root / "run.jsonl"]):
            cell = unit_phases.notes_cell(open_record(self.root / "plan.md"))
        self.assertEqual(cell, "phase 3 — Verification: " + activity.strip())
        record = open_record(self.root / "plan.md")
        current = record.current
        assert isinstance(current, phase_table.OpenPhase)
        for title, fix_done, expected in (
                ("x" * 100, 0, "phase 3 — " + "x" * 100),
                ("Phase 3 — Every session records itself", 0, "phase 3 — Every session records itself"),
                ("x" * 56, 2, "phase 3 — " + "x" * 56 + " · fix rounds 2")):
            with self.subTest(title=title, fix_done=fix_done), patch.object(plan_history, "plan_runs", return_value=[]):
                sample = record._replace(current=replace(current, title=title, fix_done=fix_done))
                self.assertEqual(unit_phases.notes_cell(sample), expected)

    def test_non_phase_work_and_queued_items_share_the_units_row(self) -> None:
        config = self.root / "table-conf/production.conf"
        configured = config.read_text()
        _ = config.write_text(configured.replace("units_table_floor_columns=120", "units_table_floor_columns=1000"))
        owner = fixture_member("showrunner", name="table-owner")
        member = fixture_member("unit", name="messaging", parent=owner.id,
                                worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        other = fixture_member("unit", name="another-unit", parent=owner.id)
        history = self.root / "history" / "runs"
        history.mkdir(parents=True)
        plan = self.root / "plan.md"
        _ = plan.write_text("# Plan\n### Phase 1 — Planned work · status: todo\n")

        def run(name: str, director: staff.StaffRow, phase: str, started: int, *tail: str,
                use_timestamp: bool = False) -> Path:
            header: dict[str, object] = {"event_type": "run_started", "owner_staff_id": director.id,
                "owner_session": director.current_session, "working_dir": str(self.root), "plan_doc": plan.name}
            header["timestamp_epoch" if use_timestamp else "run_started_at"] = started
            path = history / f"{name}.jsonl"
            _ = path.write_text("\n".join([
                json.dumps(header), event("phase_started", phase_id=phase),
                event("pass_started", phase_id=phase, pass_instance_id="p1", pass_kind="impl",
                      pass_activity=f"write {name}"), *tail]) + "\n")
            folder = self.root / "runs" / name
            folder.mkdir(parents=True, exist_ok=True)
            _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"owner_staff_id": director.id}))
            if any(cast(dict[str, object], json.loads(line)).get("event_type") == "run_finished" for line in tail):
                current = staff.snapshot().by_id(director.id)
                assert current is not None
                if isinstance(current.run, staff.OpenRun):
                    _ = staff.clear_run(director.id, current.run.path)
            else:
                _ = staff.set_run(director.id, staff.OpenRun(folder))
            return path

        _ = run("older", member, "outside-a", 1)
        _ = run("newest", member, "outside-b", 2)
        _ = run("unrelated", other, "another-phase", 100)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": member.current_session, "CODEX_THREAD_ID": ""}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(unit_queue.main(["add", "--text", "Review delivery"]), 0)
                self.assertEqual(unit_queue.main(["add", "--text", "Clean old records"]), 0)
        selected = unit("messaging", live("messaging"))
        records: dict[str, object] = {"messaging": {"current": None, "phases": phase_rows("todo")}}
        with self.subTest(run="newest unfinished"):
            rendered = table_of([selected], records)
            self.assertIn("| messaging | idle | ⬜ | 0/1 |", rendered)
            self.assertIn("| messaging | not started | outside-b: impl: write newest | Review delivery +1 more · plan: phase 1: Delivery 1 |",
                          rendered)
        _ = run("finished", member, "finished", 3, event("run_finished", summary="x" * 32_000))
        with self.subTest(run="newest finished"):
            rendered = table_of([selected], records)
            self.assertIn("| messaging | idle | ⬜ | 0/1 |", rendered)
            self.assertIn("| messaging | not started | Review delivery +1 more · plan: phase 1: Delivery 1 |",
                          rendered)
        former = replace(member, current_session=staff.StaffId(fixture_runtime_id("previous-director")))
        _ = run("former", former, "previous-session", 4, use_timestamp=True)
        with self.subTest(run="newest from a previous session"):
            rendered = table_of([selected], records)
            self.assertIn("| messaging | idle | ⬜ | 0/1 |", rendered)
            self.assertIn("| messaging | not started | previous-session: impl: write former | Review delivery +1 more · plan: phase 1: Delivery 1 |",
                          rendered)

        newest = run("multi", member, "outside-a", 5,
            event("phase_started", phase_id="closed"),
            event("phase_finished", phase_id="closed"),
            event("phase_started", phase_id="outside-b"),
            event("pass_started", phase_id="outside-b", pass_instance_id="p2", pass_kind="impl", pass_activity="write newest"),
            event("phase_started", phase_id="1"),
            event("pass_started", phase_id="1", pass_instance_id="p3", pass_kind="impl", pass_activity="planned task"))
        records["messaging"] = {"plan": str(plan), "current": {"phase": "1", "of": 1, "title": "Planned work"},
                                "phases": phase_rows("running")}
        with patch.object(plan_history, "read_run_events", wraps=plan_history.read_run_events) as whole_reads:
            rendered = table_of([selected], records)
        self.assertEqual([call.args[0] for call in whole_reads.call_args_list], [newest])
        self.assertIn("| messaging | idle | 🟨 | 0/1 |", rendered)
        self.assertIn("| messaging | phase 1 — impl: planned task | outside-b: impl: write newest +1 more | Review delivery +1 more |", rendered)
        notes_cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"current": None, "phases": []}, "no phases"),
            ({"unavailable": "no plan"}, "no plan"))
        for sample, notes in notes_cases:
            with self.subTest(notes=notes):
                records["messaging"] = sample
                rendered = table_of([selected], records)
                self.assertIn("| messaging | idle |", rendered)
                self.assertIn(f"| messaging | {notes} | 1: impl: planned task +2 more | Review delivery +1 more |",
                              rendered)
        long_text = "Review delivery " + "x" * 120
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": member.current_session, "CODEX_THREAD_ID": ""}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(unit_queue.main(["clear"]), 0)
                self.assertEqual(unit_queue.main(["add", "--text", long_text]), 0)
                self.assertEqual(unit_queue.main(["add", "--text", "Later"]), 0)
        _ = run("long", member, "outside-a", 6,
            event("phase_started", phase_id="outside-b"),
            event("phase_started", phase_id="1"),
            event("phase_started", phase_id="outside-c"),
            event("pass_started", phase_id="outside-c", pass_instance_id="p2", pass_kind="review", pass_activity="finished review"),
            event("pass_finished", phase_id="outside-c", pass_instance_id="p2"),
            event("activity_started", phase_id="outside-c", activity_instance_id="a0", activity_label="Old", activity_text="closed activity"),
            event("activity_finished", phase_id="outside-c", activity_instance_id="a0"),
            event("activity_started", phase_id="outside-c", activity_instance_id="a1",
                  activity_label="Verification", activity_text="check " + "x" * 150))
        work = table_of([selected], records).split("\n\n")[-1]
        cells = [cell.strip() for cell in work.splitlines()[2].split("|")[1:-1]]
        self.assertEqual(cells[2], "outside-c: Verification: check " + "x" * 150 + " +3 more")
        queue = cells[3].split(" · ", 1)[0]
        self.assertEqual(queue, long_text + " +1 more")
        _ = config.write_text(configured)
        wide_records: dict[str, object] = {"messaging": typed_record(self.root / "plan.md", *(["todo"] * 20))}
        fitted = table_of([selected], wide_records)
        for section in (fitted.split("\n\n")[0], fitted.split("\n\n")[-1]):
            lines = section.splitlines()
            headings = tuple(cell.strip() for cell in lines[0].split("|")[1:-1])
            rows = [tuple(cell.strip() for cell in line.split("|")[1:-1]) for line in lines[2:]]
            values = dict(zip(headings, rows[0], strict=True))
            settings = conf.read_or_repository("production")
            width = settings.whole("units_table_floor_columns") - claude_table_layout.REPLY_INDENT
            layout = claude_table_layout.fit(headings, rows, width)
            self.assertTrue(layout.keeps_table(headings, rows, width, settings.whole("units_table_cell_lines")))
            if "Progress" in headings:
                self.assertEqual(values["Progress"], "⬜" * 20)
                self.assertEqual(layout.line_count(values["Progress"], headings.index("Progress")), 1)
            else:
                self.assertEqual(values["Notes"], "not started")
                for title, expected in (
                        ("Queued", long_text + " +1 more · plan: phase 1: Delivery 1 +19 more"),
                        ("Non-Phase Work", "outside-c: Verification: check " + "x" * 150 + " +2 more")):
                    column = headings.index(title)
                    restored = " ".join(row[column] for row in rows if row[column])
                    self.assertEqual(restored.replace(" ", ""), expected.replace(" ", ""))
        _ = config.write_text(configured.replace("units_table_floor_columns=120", "units_table_floor_columns=1000"))
        path = unit_queue.state_path(member.id)
        _ = path.write_text("not json")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            rendered = table_of([selected, unit("another-unit", None)], records)
        self.assertIn("| queue unreadable |", rendered)
        self.assertIn("| another-unit | exited |", rendered)
        self.assertIn("| another-unit | — | another-phase: impl: write unrelated | — |", rendered)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertTrue(errors.getvalue().startswith(f"unit_phases: {path}: "))

        _ = run("fitting", member, "unused", 7, event("run_finished"))
        records["messaging"] = {"current": {"phase": "1", "of": 1, "title": "N" * 50},
                                "phases": phase_rows("running")}
        for columns, queued in ((89, "Q" * 60), (90, "Q" * 60), (89, "Review next"),
                                (170, "Review 界面 before delivery " + "é" * 180)):
            with self.subTest(columns=columns, queued=queued):
                _ = config.write_text(configured.replace("units_table_floor_columns=120", f"units_table_floor_columns={columns}"))
                _ = path.write_text(json.dumps([queued]))
                work = table_of([selected], records).split("\n\n")[-1]
                lines = work.splitlines()
                headings = tuple(cell.strip() for cell in lines[0].split("|")[1:-1])
                rows = [tuple(cell.strip() for cell in line.split("|")[1:-1]) for line in lines[2:]]
                self.assertEqual(headings, ("Unit", "Notes", "Queued"))
                self.assertTrue(all(row[0] == "messaging" for row in rows))
                for column, original in ((1, "phase 1 — " + "N" * 50), (2, queued)):
                    restored = " ".join(row[column] for row in rows if row[column])
                    self.assertEqual(restored.replace(" ", ""), original.replace(" ", ""))
                fitted = claude_table_layout.fit(headings, rows, columns)
                self.assertTrue(fitted.keeps_table(headings, rows, columns, 4))

    def test_a_unit_with_no_open_phase_shows_no_task(self) -> None:
        with patch.object(plan_history, "plan_runs") as runs:
            self.assertEqual(unit_phases.task_cell({"plan": "/p.md", "current": None, "phases": []}), "—")
        runs.assert_not_called()

    def test_progress_shows_phase_states_scaled_buckets_and_fix_rounds(self) -> None:
        small = typed_record(self.root / "plan.md", "done", "done", "done", "running", "todo", "todo")
        current = small.current
        assert isinstance(current, phase_table.OpenPhase)
        fixed = replace(current, fix_done=2, fix_open_passes=1)
        cases: list[tuple[object, str, str, str]] = [
            (small, "🟩🟩🟩🟨⬜⬜", "3/6", "phase 4 — Delivery 4"),
            (small._replace(current=fixed), "🟩🟩🟩🟨⬜⬜", "3/6",
             "phase 4 — Delivery 4 · fix rounds 3 + 1 running"),
            (small._replace(current=replace(current, fix_done=2)), "🟩🟩🟩🟨⬜⬜", "3/6",
             "phase 4 — Delivery 4 · fix rounds 2"),
            (typed_record(self.root / "plan.md"), "—", "—", "no phases"),
            (typed_record(self.root / "plan.md", "done"), "🟩", "1/1", "plan done"),
            (typed_record(self.root / "plan.md", "todo"), "⬜", "0/1", "not started"),
            ({"phases": [{"phase": "1", "status": "open"}]}, "🟨", "0/1", "not started"),
            (typed_record(self.root / "plan.md", *(["done"] * 20)), "🟩" * 20, "20/20", "plan done"),
            (typed_record(self.root / "plan.md", *(["done"] * 44 + ["todo"])),
             "🟩" * 19 + "⬜", "44/45", "between phases"),
        ]
        large = ["done"] * 132
        for index, status in ((0, "todo"), (5, "running"), (6, "todo"), (12, "todo"), (125, "running")):
            large[index] = status
        cases.append((typed_record(self.root / "plan.md", *large), "🟨⬜" + "🟩" * 17 + "🟨",
                      "127/132", "phase 126 — Delivery 126"))
        for record, expected, count, notes in cases:
            with self.subTest(expected=expected):
                self.assertEqual(unit_phases.progress_cell(record), expected)
                self.assertEqual(unit_phases.phase_cell(record), count)
                self.assertEqual(unit_phases.notes_cell(record), notes)

    def test_eta_uses_the_latest_promise_and_local_calendar_time(self) -> None:
        record = typed_record(self.root / "plan.md", "running")
        current = record.current
        assert isinstance(current, phase_table.OpenPhase)
        now = record.updated
        promised = replace(current, eta=phase_table.StatedEta(now + timedelta(hours=2),
            phase_table.NoEtaRange(), now, "fixture"), schedule_end=now + timedelta(hours=2))
        cases: list[tuple[phase_table.PhaseRecord, datetime, ZoneInfo, str]] = []
        for target, expected in ((now + timedelta(minutes=30), "18:30"),
                                 (now - timedelta(minutes=65), "16:55 · late 1h 5m"),
                                 (now + timedelta(days=1, minutes=41), "Sat 18:41"),
                                 (now + timedelta(days=8), "24 Oct")):
            latest = replace(promised, eta_trail=[(now - timedelta(hours=3), now - timedelta(hours=4)), (target, now)])
            cases.append((record._replace(current=latest), now, ZoneInfo("UTC"), expected))
        latest = replace(promised, eta_trail=[(now + timedelta(minutes=30), now)])
        cases.extend([
            (record, now, ZoneInfo("UTC"), "no ETA stated"),
            (record._replace(current=latest, plan_finish=phase_table.PredictedFinish(now + timedelta(minutes=42))),
             now, ZoneInfo("UTC"), "18:30"),
            (record._replace(current=latest), now, ZoneInfo("America/New_York"), "14:30"),
            (typed_record(self.root / "plan.md"), now, ZoneInfo("UTC"), "—"),
        ])
        waiting = typed_record(self.root / "plan.md", "done", "todo", "todo", "todo")
        first = waiting.phases[2]
        later = waiting.phases[3]
        assert isinstance(first, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        first = replace(first, eta_trail=[(now + timedelta(hours=4), now - timedelta(minutes=2)),
                                        (now + timedelta(minutes=30), now - timedelta(minutes=1))])
        later = replace(later, eta_trail=[(now + timedelta(hours=2), now)])
        cases.extend([
            (waiting._replace(phases=[*waiting.phases[:2], first, later]), now, ZoneInfo("UTC"), "18:30"),
            (waiting, now, ZoneInfo("UTC"), "—"),
            (waiting._replace(phases=[*waiting.phases[:2],
                replace(first, eta_trail=[(now - timedelta(minutes=5), now - timedelta(minutes=10))]), later]),
             now, ZoneInfo("UTC"), "17:55 · late 5m"),
        ])
        for sample, clock, zone, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(unit_phases.eta_cell(sample, now=clock.timestamp(), zone=zone), expected)
        for target, zone, expected in (
                (now + timedelta(minutes=42), ZoneInfo("UTC"), "18:42"),
                (now + timedelta(days=1, minutes=41), ZoneInfo("UTC"), "Sat 18:41"),
                (now + timedelta(days=8), ZoneInfo("UTC"), "24 Oct"),
                (now + timedelta(minutes=42), ZoneInfo("America/New_York"), "14:42")):
            with self.subTest(plan_target=target, zone=zone):
                self.assertEqual(unit_phases.eta_plan_cell(
                    record._replace(plan_finish=phase_table.PredictedFinish(target)),
                    now=now.timestamp(), zone=zone), expected)

    def test_table_selects_eta_next_before_the_plan_finish(self) -> None:
        owner = fixture_member("showrunner", name="promise-owner")
        member = fixture_member("unit", name="promises", parent=owner.id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        running = typed_record(self.root / "plan.md", "running", "todo")
        _ = running.plan.write_text("# Plan\n### Phase 1 — Delivery 1 · status: todo\n"
                                    + "### Phase 2 — Delivery 2 · status: todo\n")
        now = running.updated
        current = running.current
        assert isinstance(current, phase_table.OpenPhase)
        checkpoint = running.phases[1]
        assert isinstance(checkpoint, phase_table.TodoPhase)
        checkpoint = replace(checkpoint, eta_trail=[(now + timedelta(minutes=45), now)])
        promised = replace(current, eta_trail=[(now + timedelta(minutes=10), now)])
        plan_finish = phase_table.PredictedFinish(now + timedelta(hours=1))
        predicted = phase_table.PredictedPhaseTiming(start=now, finish=plan_finish.at)
        checkpoint = replace(checkpoint, times=predicted)
        running = running._replace(phases=[running.phases[0], replace(running.phases[1], times=predicted)])
        waiting = typed_record(self.root / "plan.md", "done", "todo")._replace(
            phases=[typed_record(self.root / "plan.md", "done").phases[0], checkpoint], plan_finish=plan_finish)
        history = self.root / "history" / "runs"
        history.mkdir(parents=True)
        folder = self.root / "runs" / "promises"
        folder.mkdir(parents=True)
        _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"owner_staff_id": member.id}))
        _ = staff.set_run(member.id, staff.OpenRun(folder))
        started: dict[str, object] = {"event_type": "run_started", "owner_staff_id": member.id,
            "working_dir": str(self.root), "plan_doc": "plan.md", "run_id": "promises",
            "run_started_at": now.timestamp() - 600}
        for sample, outside, target, expected, from_events, expected_plan in (
                (running._replace(current=promised, plan_finish=plan_finish), True, 20, "18:10", False, "19:00"),
                (running._replace(plan_finish=plan_finish), True, 20, "no ETA stated", False, "19:00"),
                (running, True, 20, "18:10", True, "19:00"),
                (running, True, 20, "no ETA stated", True, "19:00"),
                (waiting, True, 20, "18:20", False, "19:00 · phase 2: 18:45"),
                (waiting, True, -5, "17:55 · late 5m", False, "19:00 · phase 2: 18:45"),
                (waiting, True, None, "no ETA stated", False, "19:00 · phase 2: 18:45"),
                (waiting, False, None, "18:45", False, "19:00 · phase 2: 18:45"),
                (typed_record(self.root / "plan.md", "done"), False, None, "—", False, "—")):
            with self.subTest(current=sample.current, outside=outside, target=target,
                              expected=expected, from_events=from_events):
                events = [started]
                has_planned = isinstance(sample.current, phase_table.OpenPhase)
                if has_planned or from_events:
                    events.append({"event_type": "phase_started", "phase_id": "1", "phase_title": "Delivery 1",
                                   "phase_instance_id": "planned", "phase_started_at": now.timestamp() - 550})
                    promises = ([(now + timedelta(minutes=10), now - timedelta(seconds=525))]
                                if from_events and expected == "18:10" else
                                sample.current.eta_trail if not from_events
                                and isinstance(sample.current, phase_table.OpenPhase) else [])
                    for promise, stated in promises:
                        events.append({"event_type": "eta_stated", "eta_phase_id": "1", "phase_instance_id": "planned",
                                       "timestamp_epoch": stated.timestamp(), "eta_at": promise.timestamp()})
                if outside:
                    events.extend([
                        {"event_type": "phase_started", "phase_id": "outside-old", "phase_title": "Old work",
                         "phase_instance_id": "old", "phase_started_at": now.timestamp() - 500},
                        {"event_type": "eta_stated", "eta_phase_id": "outside-old", "phase_instance_id": "old",
                         "timestamp_epoch": now.timestamp() - 450, "eta_at": now.timestamp() + 7200},
                        {"event_type": "phase_started", "phase_id": "outside-new", "phase_title": "New work",
                         "phase_instance_id": "new", "phase_started_at": now.timestamp() - 400}])
                    if target is not None:
                        events.extend([
                            {"event_type": "eta_stated", "eta_phase_id": "outside-new", "phase_instance_id": "new",
                             "timestamp_epoch": now.timestamp() - 300, "eta_at": now.timestamp() + 3600},
                            {"event_type": "eta_stated", "eta_phase_id": "outside-new", "phase_instance_id": "new",
                             "timestamp_epoch": now.timestamp() - 200,
                             "eta_at": now.timestamp() + target * 60}])
                for row in sample.phases:
                    if isinstance(row, phase_table.TodoPhase):
                        for promise, stated in row.eta_trail:
                            events.append({"event_type": "eta_stated", "eta_phase_id": row.phase,
                                "timestamp_epoch": stated.timestamp(), "eta_at": promise.timestamp()})
                _ = (history / "promises.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n")
                if from_events:
                    with patch.object(plan_history, "now_epoch", return_value=now.timestamp()):
                        sample = phase_table.build_plan(running.plan)._replace(plan_finish=plan_finish)
                    sample = sample._replace(phases=[sample.phases[0], replace(sample.phases[1], times=predicted)])
                    self.assertIsInstance(sample.phases[0], phase_table.OpenPhase)
                    assert isinstance(sample.current, phase_table.OpenPhase)
                    self.assertEqual(sample.current.phase, "outside-new")
                rendered = unit_phases.table([unit("promises", None)], {"promises": sample}, census_read=LOCAL,
                                            now=now.timestamp(), zone=ZoneInfo("UTC"))
                headers = [cell.strip() for cell in rendered.splitlines()[0].split("|")[1:-1]]
                table_rows = [[cell.strip() for cell in line.split("|")[1:-1]]
                              for line in rendered.split("\n\n")[0].splitlines()[2:]]
                selected_phase = "1" if has_planned or from_events else "outside-new" if outside else ""
                cells = (next(row for row in table_rows if row[headers.index("Phase")] == selected_phase)
                         if selected_phase else table_rows[0])
                if expected == "—":
                    self.assertNotIn("ETA Next", headers)
                else:
                    self.assertEqual(cells[headers.index("ETA Next")], expected)
                if any(not isinstance(row, phase_table.DonePhase) for row in sample.phases):
                    self.assertEqual(headers[4:6], ["ETA Next", "ETA Plan"])
                    self.assertEqual(table_rows[0][headers.index("ETA Plan")], expected_plan)
                else:
                    self.assertNotIn("ETA Plan", headers)

        _ = running.plan.write_text("# Plan\n### Phase 1 — Delivery 1 · status: done\n"
                                   + "### Phase 2 — Delivery 2 · status: todo\n")
        earlier: list[dict[str, object]] = [
            {**started, "run_id": "earlier", "run_started_at": now.timestamp() - 1200},
            {"event_type": "phase_started", "phase_id": "1", "phase_instance_id": "completed",
             "phase_title": "Delivery 1", "timestamp_epoch": now.timestamp() - 1100},
            {"event_type": "phase_finished", "phase_id": "1", "phase_instance_id": "completed",
             "timestamp_epoch": now.timestamp() - 1040, "phase_elapsed_seconds": 60, "status": "completed"},
            {"event_type": "run_finished", "timestamp_epoch": now.timestamp() - 1000},
        ]
        _ = (history / "earlier.jsonl").write_text("\n".join(map(json.dumps, earlier)) + "\n")
        side_events: list[dict[str, object]] = [
            {**started, "run_id": "side", "run_started_at": now.timestamp() - 10},
            {"event_type": "phase_started", "phase_id": "2", "phase_instance_id": "side-phase",
             "phase_title": "Delivery 2", "timestamp_epoch": now.timestamp() - 9},
            {"event_type": "eta_stated", "eta_phase_id": "2", "phase_instance_id": "side-phase",
             "timestamp_epoch": now.timestamp() - 8, "eta_at": now.timestamp() + 18000},
        ]
        _ = (history / "side.jsonl").write_text("\n".join(map(json.dumps, side_events)) + "\n")
        for current_promise, expected_next, expected_plan in (
                (True, "18:10", "18:10 · phase 2: 18:10"),
                (False, "—", "18:01")):
            with self.subTest(current_promise=current_promise, other_run="newer open run"):
                current_events = [started]
                if current_promise:
                    current_events.append({"event_type": "eta_stated", "eta_phase_id": "2",
                        "timestamp_epoch": now.timestamp() - 100, "eta_at": now.timestamp() + 600})
                _ = (history / "promises.jsonl").write_text("\n".join(map(json.dumps, current_events)) + "\n")
                with patch.object(plan_history, "now_epoch", return_value=now.timestamp()):
                    sample = phase_table.build_plan(running.plan)
                rendered = unit_phases.table([unit("promises", None)], {"promises": sample}, census_read=LOCAL,
                                            now=now.timestamp(), zone=ZoneInfo("UTC"))
                lines = rendered.split("\n\n")[0].splitlines()
                headers = [cell.strip() for cell in lines[0].split("|")[1:-1]]
                cells = dict(zip(headers, (cell.strip() for cell in lines[2].split("|")[1:-1]), strict=True))
                self.assertEqual(cells.get("ETA Next", "—"), expected_next)
                self.assertEqual(cells["ETA Plan"], expected_plan)
                self.assertEqual(cells["Progress"], "🟩⬜")
                self.assertEqual(cells["Phase"], "1/2")
                self.assertIn("| promises | between phases", rendered)

    def test_workers_show_run_results_before_working_workers_and_count_idle_workers(self) -> None:
        parent = staff.StaffId("parent")
        workers = [
            census_worker("d", parent), census_worker("idle", parent, status="idle"),
            census_worker("unread", parent, status=None), census_worker("held", parent, status="paused"),
            census_worker("a", parent, status="idle", runs=[census.CensusRun("x", 1000, "running")]),
            census_worker("b", parent, runs=[census.CensusRun("y", 900, "finished_queued")]),
            census_worker("c", parent, runs=[census.CensusRun("z", 800, "missing")]),
        ]
        cases: list[tuple[Sequence[census.CensusWorker] | census.CensusFailed, str]] = [
            (workers, "a waiting on run: x 1m · b result queued: y · c run gone: z · d working +1 idle"),
            ([census_worker("a", parent, runs=[census.CensusRun("w", None, "running")])], "a waiting on run: w"),
            ([census_worker("idle", parent, status="idle")], "—"),
            ([census_worker("held", parent, status="paused")], "—"), ([], "—"),
            (census.CensusFailed("census took longer than 10 s"), "—")]
        for sample, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(unit_phases.workers_cell(sample, now=1060), expected)

    def test_status_comes_from_the_census_session_or_the_scanned_machines(self) -> None:
        owner = fixture_member("showrunner", name="status-owner")
        member = fixture_member("unit", name="listed", parent=owner.id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        listed = unit("listed", live("listed"))
        mac = listed._replace(machine="mac")
        unrecorded = unit("unrecorded", live("unrecorded"))
        failed = census.CensusFailed("census exited 2: bad config")
        cases: list[tuple[census.Census | census.CensusFailed, live_units.LiveUnit, str]] = [
            (tiles(sessions=[session_of(member, "busy")]), listed, "busy"),
            (tiles(sessions=[session_of(member, "paused")]), mac, "paused"),
            (tiles(sessions=[session_of(member, None)]), listed, "—"),
            (tiles(sessions=[census.CensusSession("listed-id", "busy")]), listed, "exited"),
            (tiles(sessions=[census.CensusSession("unrecorded-id", "idle")]), unrecorded, "idle"),
            (LOCAL, unrecorded._replace(session=None), "exited"),
            (LOCAL, listed, "exited"),
            (LOCAL, listed._replace(machine=""), "exited"),
            (LOCAL, mac, "—"),
            (LOCAL._replace(machines=(census.CensusMachine("natedev", False),)), listed, "—"),
            (failed, listed, "—")]
        for read, selected, expected in cases:
            with self.subTest(read=read, machine=selected.machine):
                view = unit_phases.census_view(read)
                self.assertEqual(unit_phases.census_status(view, selected), expected)
        workers = [census_worker("first", member.id, phase="one"), census_worker("second", member.id),
                   census_worker("other", staff.StaffId("other-unit"))]
        view = unit_phases.census_view(tiles(workers=workers))
        self.assertEqual(unit_phases.census_workers(view, member), tuple(workers[:2]))
        self.assertEqual(unit_phases.census_workers(view, member, "one"), (workers[0],))
        self.assertEqual(unit_phases.census_workers(view, member, "two"), ())
        self.assertEqual(unit_phases.census_workers(unit_phases.census_view(failed), member, "one"), failed)

    def test_each_open_phase_has_its_work_eta_wait_and_recorded_workers(self) -> None:
        owner = fixture_member("showrunner", name="parallel-owner")
        member = fixture_member("unit", name="parallel", parent=owner.id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        holder = fixture_member("showrunner", name="builder")
        plan = self.root / "parallel.md"
        _ = plan.write_text("# Plan\n### Phase 7 — Done delivery · status: done\n"
                            + "### Phase 8 — Repair delivery · status: todo\n"
                            + "### Phase 10 — Verify delivery · status: todo\n"
                            + "### Phase 12 — Later delivery · status: todo\n")
        now = datetime(2026, 11, 13, 3, tzinfo=UTC).timestamp()
        history = self.root / "history" / "runs"
        history.mkdir(parents=True)
        folder = self.root / "runs" / "parallel"
        folder.mkdir(parents=True)
        _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"owner_staff_id": member.id}))
        _ = staff.set_run(member.id, staff.OpenRun(folder))
        events: list[dict[str, object]] = [
            {"event_type": "run_started", "run_id": "parallel", "run_started_at": now - 1_000,
             "owner_staff_id": member.id, "working_dir": str(self.root), "plan_doc": plan.name},
            {"event_type": "phase_started", "phase_id": "7", "phase_instance_id": "seven",
             "phase_title": "Done delivery", "timestamp_epoch": now - 950},
            {"event_type": "phase_finished", "phase_id": "7", "phase_instance_id": "seven",
             "timestamp_epoch": now - 900, "phase_elapsed_seconds": 50, "status": "completed"},
            {"event_type": "phase_started", "phase_id": "10", "phase_instance_id": "ten",
             "phase_title": "Verify delivery", "timestamp_epoch": now - 800},
            {"event_type": "phase_started", "phase_id": "8", "phase_instance_id": "eight",
             "phase_title": "Repair delivery", "timestamp_epoch": now - 700},
            {"event_type": "eta_stated", "phase_id": "8", "phase_instance_id": "eight",
             "eta_phase_id": "8", "eta_at": now - 3600, "timestamp_epoch": now - 600},
            {"event_type": "eta_stated", "phase_id": "10", "phase_instance_id": "ten",
             "eta_phase_id": "8", "eta_at": now - 300, "timestamp_epoch": now - 500},
            {"event_type": "pass_started", "phase_id": "8", "phase_instance_id": "eight",
             "pass_instance_id": "repair-pass", "pass_kind": "fix", "pass_activity": "fix dispatch",
             "team_slot": "repair", "worker_id": "historic-repair", "pass_started_at": now - 400,
             "timestamp_epoch": now - 400},
            {"event_type": "pass_started", "phase_id": "10", "phase_instance_id": "ten",
             "pass_instance_id": "verify-pass", "pass_kind": "test", "pass_activity": "verify the completed rows",
             "team_slot": "verifier", "worker_id": "historic-verifier", "pass_started_at": now - 350,
             "timestamp_epoch": now - 350},
            {"event_type": "wait_started", "phase_id": "8", "phase_instance_id": "eight",
             "pass_instance_id": "repair-pass", "wait_id": "worker-wait", "wait_reason": "memory admission",
             "wait_started_epoch": now - 120, "timestamp_epoch": now - 120,
             "holder": {"state": "HolderNotNamed", "reason": "producer named no holder"}},
            {"event_type": "wait_started", "phase_id": "10", "phase_instance_id": "ten",
             "wait_id": "director-wait", "wait_reason": "build queue", "wait_started_epoch": now - 300,
             "timestamp_epoch": now - 300, "holder": {"state": "NamedHolder", "resource": "build",
                 "resource_kind": "slot", "holder": {"state": "SessionHolder", "session_id": holder.current_session}}},
            {"event_type": "eta_stated", "phase_id": "8", "phase_instance_id": "eight",
             "eta_phase_id": "12", "eta_at": now + 3_600, "timestamp_epoch": now - 100},
            {"event_type": "eta_stated", "phase_id": "8", "phase_instance_id": "eight",
             "eta_phase_id": "12", "eta_at": now + 1_800, "timestamp_epoch": now - 90},
            {"event_type": "eta_stated", "phase_id": "8", "phase_instance_id": "eight",
             "eta_phase_id": "7", "eta_at": now + 7_200, "timestamp_epoch": now - 80},
        ]
        path = history / "parallel.jsonl"
        _ = (history / "later-finished.jsonl").write_text("\n".join(map(json.dumps, [
            {"event_type": "run_started", "run_id": "later-finished", "run_started_at": now - 10,
             "owner_staff_id": member.id, "working_dir": str(self.root), "plan_doc": ""},
            {"event_type": "run_finished", "timestamp_epoch": now - 5},
        ])) + "\n")
        cases = (
            (False, False, False, "build queue"),
            (True, False, False, "build queue"),
            (False, True, False, "build queue"),
            (False, True, True, "build queue"),
            (False, False, False, "blocked: waiting on the showrunner"),
            (False, False, False, "blocked: waiting for the showrunner"),
            (False, False, False, "waiting on the showrunner"),
            (False, False, False, "waiting for the showrunner"),
        )
        for reopened, failed_repair, previous_completion, reason in cases:
            with self.subTest(reopened=reopened, failed_repair=failed_repair,
                              previous_completion=previous_completion, reason=reason):
                selected_events = [dict(event) for event in events]
                if previous_completion:
                    selected_events[3:3] = [
                        {"event_type": "phase_started", "phase_id": "8", "phase_instance_id": "previous-eight",
                         "phase_title": "Repair delivery", "timestamp_epoch": now - 880},
                        {"event_type": "phase_finished", "phase_id": "8", "phase_instance_id": "previous-eight",
                         "timestamp_epoch": now - 860, "phase_elapsed_seconds": 20, "status": "completed"},
                    ]
                if reopened:
                    selected_events[3:3] = [
                        {"event_type": "run_finished", "timestamp_epoch": now - 850},
                        {"event_type": "reopened", "timestamp_epoch": now - 820},
                    ]
                for selected in selected_events:
                    if selected.get("wait_id") == "director-wait":
                        selected["wait_reason"] = reason
                if failed_repair:
                    selected_events.extend([
                        {"event_type": "phase_finished", "phase_id": "8", "phase_instance_id": "eight",
                         "timestamp_epoch": now - 70, "phase_elapsed_seconds": 630, "status": "error"},
                        {"event_type": "eta_stated", "phase_id": "10", "phase_instance_id": "ten",
                         "eta_phase_id": "8", "eta_at": now + 2_400, "timestamp_epoch": now - 60},
                    ])
                _ = path.write_text("\n".join(map(json.dumps, selected_events)) + "\n")
                with patch.object(plan_history, "now_epoch", return_value=now):
                    record = phase_table.build_plan(plan)
                rendered = unit_phases.table([unit("parallel", live("parallel"))._replace(machine="mac")], {"parallel": record},
                    now=now, zone=ZoneInfo("UTC"), census_read=tiles(sessions=[session_of(member, "idle")], workers=[
                        census_worker("repair", member.id, phase="eight"),
                        census_worker("verifier", member.id, phase="ten")]))
                sections = rendered.split("\n\n")
                rows: list[list[dict[str, str]]] = []
                for section in (sections[0], sections[-1]):
                    lines = section.splitlines()
                    headers = [cell.strip() for cell in lines[0].split("|")[1:-1]]
                    rows.append([dict(zip(headers, (cell.strip() for cell in line.split("|")[1:-1]), strict=True))
                                 for line in lines[2:]])
                status, work = rows
                if failed_repair:
                    self.assertEqual([row["Phase"] for row in status], ["10"])
                    self.assertIn("phase 8: 03:40", status[0]["ETA Plan"])
                    self.assertNotIn("phase 7:", status[0]["ETA Plan"])
                    self.assertIn("phase 12: 03:30", status[0]["ETA Plan"])
                    self.assertEqual(status[0][unit_phases.worker_heading()], "verifier working")
                    self.assertIn("test: verify the completed rows", work[0]["Notes"])
                    self.assertTrue(work[0]["Notes"].endswith("waiting on build queue (held by builder) 5m"))
                    continue
                self.assertEqual([row["Unit"] for row in status], ["parallel", "parallel"])
                self.assertEqual([row["Phase"] for row in status], ["8", "10"])
                self.assertEqual([row["Status"] for row in status], ["idle", "—"])
                self.assertEqual([row["Machine"] for row in status], ["mac", "—"])
                self.assertEqual([row["Progress"] for row in status], ["🟩🟨🟨⬜", "—"])
                self.assertEqual([row["ETA Next"] for row in status], ["02:55 · late 5m", "no ETA stated"])
                self.assertIn("phase 12: 03:30", status[0]["ETA Plan"])
                self.assertNotIn("phase 7:", status[0]["ETA Plan"])
                self.assertEqual(status[1]["ETA Plan"], "—")
                self.assertEqual([row[unit_phases.worker_heading()] for row in status],
                                 ["repair working", "verifier working"])
                self.assertEqual([row["Unit"] for row in work], ["parallel", "parallel"])
                self.assertTrue(work[0]["Notes"].startswith("phase 8 — Repair delivery"))
                self.assertNotIn("fix dispatch", work[0]["Notes"])
                self.assertTrue(work[0]["Notes"].endswith("waiting on memory admission 2m"))
                self.assertTrue(work[1]["Notes"].startswith("phase 10 — Verify delivery"))
                self.assertIn("test: verify the completed rows", work[1]["Notes"])
                self.assertIn("held by builder", work[1]["Notes"])
                self.assertNotIn(holder.current_session, work[1]["Notes"])
                expected_wait = "waiting on build queue" if reason == "build queue" else reason
                self.assertEqual(work[1]["Notes"],
                    f"phase 10 — Verify delivery · test: verify the completed rows · {expected_wait} (held by builder) 5m")
                self.assertEqual([row["Queued"] for row in work], ["plan: phase 12: Later delivery", "—"])

        side_path = history / "later-finished.jsonl"
        side_events: list[dict[str, object]] = [
            {"event_type": "run_started", "run_id": "later-finished", "run_started_at": now - 10,
             "owner_staff_id": member.id, "working_dir": str(self.root), "plan_doc": "astra-review.md"},
            {"event_type": "phase_started", "phase_id": "review", "phase_instance_id": "side-review",
             "phase_title": "Side review", "timestamp_epoch": now - 9},
            {"event_type": "pass_started", "phase_instance_id": "side-review", "pass_instance_id": "side-pass",
             "team_slot": "side-worker", "worker_id": "side-worker", "timestamp_epoch": now - 8},
        ]
        current_events = [events[0], events[3], events[8],
            {"event_type": "pass_started", "phase_id": "10", "phase_instance_id": "ten",
             "pass_instance_id": "writer-pass", "team_slot": "writer", "worker_id": "historic-writer",
             "timestamp_epoch": now - 300}]
        run_cases = (
            (False, False, "Verify delivery", "phase 10 — Verify delivery"),
            (True, False, "Verify delivery", "between phases"),
            (False, True, "Verify delivery", "phase 10 — Verify delivery"),
            (False, False, "Phase 10 — Verify delivery", "phase 10 — Verify delivery"),
            (False, False, "pHaSe 10 - Verify delivery", "phase 10 — Verify delivery"),
            (False, False, "PHASE 10 – Verify delivery", "phase 10 — Verify delivery"),
        )
        for between, side_open, title, expected_notes in run_cases:
            with self.subTest(between=between, side_open=side_open, title=title):
                selected_events = [dict(event) for event in current_events]
                selected_events[1]["phase_title"] = title
                if between:
                    selected_events.append({"event_type": "phase_finished", "phase_id": "10",
                        "phase_instance_id": "ten", "timestamp_epoch": now - 200,
                        "phase_elapsed_seconds": 600, "status": "completed"})
                _ = path.write_text("\n".join(map(json.dumps, selected_events)) + "\n")
                _ = side_path.write_text("\n".join(map(json.dumps, side_events + ([] if side_open else [
                    {"event_type": "run_finished", "timestamp_epoch": now - 5}]))) + "\n")
                with patch.object(plan_history, "now_epoch", return_value=now):
                    record = phase_table.build_plan(plan)
                rendered = unit_phases.table([unit("parallel", live("parallel"))], {"parallel": record},
                    now=now, zone=ZoneInfo("UTC"),
                    census_read=tiles(workers=[census_worker("pane-reader", member.id)] if between else [
                        census_worker("verifier", member.id, phase="ten"),
                        census_worker("writer", member.id, phase="ten")]))
                sections = rendered.split("\n\n")
                status_lines = sections[0].splitlines()
                headers = [cell.strip() for cell in status_lines[0].split("|")[1:-1]]
                status = [dict(zip(headers, (cell.strip() for cell in line.split("|")[1:-1]), strict=True))
                          for line in status_lines[2:]]
                self.assertEqual([row["Phase"] for row in status], ["1/4" if between else "10"])
                self.assertEqual([row[unit_phases.worker_heading()] for row in status],
                                 ["pane-reader working" if between else "verifier working · writer working"])
                self.assertIn(f"| parallel | {expected_notes}", sections[-1])
                self.assertNotIn("Side review", rendered)
                self.assertNotIn("side-worker", rendered)
                self.assertNotIn("Phase 10 —", rendered)

    def test_table_shows_census_workers_with_their_runs(self) -> None:
        owner = fixture_member("showrunner", name="workers-owner")
        member = fixture_member("unit", name="workers", parent=owner.id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        outside = fixture_member("unit", name="outside", parent=owner.id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root / "outside"))
        history = self.root / "history" / "runs"
        history.mkdir(parents=True)
        _ = (history / "active.jsonl").write_text(json.dumps({"event_type": "run_started",
            "owner_staff_id": str(member.id), "working_dir": str(self.root), "plan_doc": "missing.md",
            "run_started_at": 1000}) + "\n")
        active = self.root / "active"
        active.mkdir()

        def reader(label: str, started: float) -> census.Census:
            return tiles(workers=[census_worker("reader", member.id, status="idle",
                                                runs=[census.CensusRun(label, started, "running")]),
                                  census_worker("outside-reader", outside.id)])

        names_config = self.root / "table-conf/names.toml"
        configured_names = names_config.read_text()
        load_names = display_names.load_names
        self.addCleanup(display_names.runtime_names.cache_clear)
        for heading in ("workers", "progress"):
            _ = names_config.write_text(configured_names.replace('workers = ["workers"]', f'workers = ["{heading}"]'))
            display_names.runtime_names.cache_clear()
            with (self.subTest(heading=heading),
                  patch.object(display_names, "load_names", side_effect=lambda: load_names(names_config)),
                  patch.object(unit_runs, "run_roots", return_value=(self.root,))):
                rendered = unit_phases.table([unit("workers", None)], {}, now=1060, zone=ZoneInfo("UTC"),
                    snapshot=staff.snapshot(), census_read=reader("Tests", 1000))
            self.assertEqual(rendered.splitlines()[2],
                "| workers | exited | reader waiting on run: Tests 1m |")
            self.assertEqual(rendered.splitlines()[0], f"| Unit | Status | {heading.capitalize()} |")
            self.assertEqual(rendered.split("\n\n")[-1].splitlines(), [
                "| Unit |",
                "| --- |",
                "| workers |",
            ])
            self.assertNotIn("Progress:", rendered)
        display_names.runtime_names.cache_clear()

        planned = typed_record(self.root / "plan.md", "running", *(["todo"] * 19))
        planned = planned._replace(plan_finish=phase_table.PredictedFinish(planned.updated + timedelta(hours=1)))
        planned = planned._replace(phases=[*planned.phases[:-1], replace(planned.phases[-1],
            times=phase_table.PredictedPhaseTiming(start=planned.updated, finish=planned.updated + timedelta(hours=1)))])
        now = planned.updated.timestamp()
        for label_size, other in ((60, False), (20, True)):
            with self.subTest(label_size=label_size, non_phase=other):
                if other:
                    run = history / "active.jsonl"
                    _ = run.write_text(run.read_text() + event("phase_started", phase_id="outside",
                        phase_instance_id="outside") + "\n" + event("pass_started", phase_id="outside",
                        pass_instance_id="outside-pass", pass_kind="impl", pass_activity="other work") + "\n")
                    _ = (active / unit_runs.STATE_FILE).write_text(json.dumps({"owner_staff_id": member.id}))
                    _ = staff.set_run(member.id, staff.OpenRun(active))
                with patch.object(unit_runs, "run_roots", return_value=(self.root,)):
                    rendered = unit_phases.table([unit("workers", None)], {"workers": planned}, now=now,
                        zone=ZoneInfo("UTC"), snapshot=staff.snapshot(),
                        census_read=reader("Check " + "x" * label_size, now - 60))
                values: dict[str, str] = {}
                for section in (rendered.split("\n\n")[0], rendered.split("\n\n")[-1]):
                    headers = [cell.strip() for cell in section.splitlines()[0].split("|")[1:-1]]
                    rows = [tuple(cell.strip() for cell in line.split("|")[1:-1])
                            for line in section.splitlines()[2:]]
                    values.update((header, " ".join(row[column] for row in rows if row[column]))
                                  for column, header in enumerate(headers))
                    if "Status" in headers:
                        self.assertEqual(headers[-2:], ["ETA Plan", unit_phases.worker_heading()])
                self.assertEqual(values["Progress"], "🟨" + "⬜" * 19)
                self.assertEqual(values["ETA Next"], "no ETA stated")
                self.assertEqual(values["ETA Plan"], "19:00")
                self.assertEqual(values["Notes"], "phase 1 — Delivery 1")
                self.assertEqual(values["Queued"], "plan: phase 2: Delivery 2 +18 more")
                expected_worker = ("reader waiting on run: Check " + "x" * 50 + "…" if label_size == 60
                                   else "reader waiting on run: Check " + "x" * 20 + " 1m")
                self.assertEqual(values[unit_phases.worker_heading()].replace(" ", ""),
                                 expected_worker.replace(" ", ""))
                self.assertEqual(headers, ["Unit", "Notes",
                    *(("Non-Phase Work",) if other else ()), "Queued"])
                if other:
                    self.assertEqual(values["Non-Phase Work"], "outside: impl: other work")

    def test_queued_combines_outside_work_with_only_waiting_plan_phases(self) -> None:
        owner = fixture_member("showrunner", name="queue-owner")
        member = fixture_member("unit", name="queued", parent=owner.id)
        record = typed_record(self.root / "plan.md", "done", "running", "todo", "todo")
        long_title = record._replace(phases=[*record.phases[:2],
            replace(record.phases[2], title="x" * 120), record.phases[3]])
        path = unit_queue.state_path(member.id)
        path.parent.mkdir(parents=True)
        for queued, sample, expected in (
                ([], record, "plan: phase 3: Delivery 3 +1 more"),
                ([], long_title, "plan: phase 3: " + "x" * 120 + " +1 more"),
                (["Review delivery", "Clean old records"], None, "Review delivery +1 more"),
                (["Review delivery"], record, "Review delivery · plan: phase 3: Delivery 3 +1 more"),
                ([], typed_record(self.root / "plan.md", "done", "running"), "—"),
                ([], None, "—")):
            with self.subTest(expected=expected):
                _ = path.write_text(json.dumps(queued))
                self.assertEqual(unit_phases.queued_cell(member.id, sample), expected)

    def test_table_reads_the_newest_runs_plan_when_the_plan_record_has_no_phases(self) -> None:
        config = self.root / "table-conf/production.conf"
        _ = config.write_text(config.read_text().replace("units_table_columns=170", "units_table_columns=1000"))
        owner = fixture_member("showrunner", name="fallback-owner")
        member = fixture_member("unit", name="fallback", parent=owner.id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        history = self.root / "history" / "runs"
        history.mkdir(parents=True)
        older = self.root / "old.md"
        newest = self.root / "new.md"
        _ = older.write_text("# Plan\n### Phase 1 — Old work · status: todo\n")
        _ = newest.write_text("# Plan\n### Phase 1 — Done work · status: done\n"
            + "### Phase 2 — Open work · status: todo\n"
            + "### Phase 3 — New work · status: todo\n"
            + "### Phase 4 — Later work · status: todo\n")
        now = datetime(2026, 10, 16, 18, 10, tzinfo=UTC).timestamp()
        for index, plan in enumerate((older, newest), 1):
            events: list[dict[str, object]] = [{"event_type": "run_started",
                "owner_staff_id": str(member.id), "run_id": f"run-{index}", "working_dir": str(self.root), "plan_doc": plan.name,
                "run_started_at": index},
                {"event_type": "phase_started", "phase_id": "1", "phase_title": "Done work",
                 "phase_instance_id": f"done-{index}", "phase_started_at": now - 600},
                {"event_type": "phase_finished", "phase_id": "1", "phase_instance_id": f"done-{index}",
                 "timestamp_epoch": now - 500, "phase_elapsed_seconds": 100, "status": "completed"},
                {"event_type": "phase_started", "phase_id": "2", "phase_title": "Open work",
                 "phase_instance_id": f"open-{index}", "phase_started_at": now - 400},
                {"event_type": "eta_stated", "eta_phase_id": "2", "phase_instance_id": f"open-{index}",
                 "timestamp_epoch": now - 300, "eta_at": now + 600},
                {"event_type": "eta_stated", "eta_phase_id": "2", "phase_instance_id": f"open-{index}",
                 "timestamp_epoch": now - 200, "eta_at": now + 1200}]
            _ = (history / f"run-{index}.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n")
        run = history / "run-2.jsonl"
        open_events = run.read_text()
        records: dict[str, object] = {"fallback": typed_record(self.root / "as-built.md")}
        for finished, missing, fixing, other, queue_items, progress, notes, eta, queued in (
                (False, False, False, False, [], "🟩🟨", "phase 2 — Open work",
                 "18:30", "plan: phase 3: New work +1 more"),
                (False, False, False, False, ["phase 2 — Open work", "Review delivery"],
                 "🟩🟨", "phase 2 — Open work", "18:30",
                 "Review delivery · plan: phase 3: New work +1 more"),
                (False, False, False, False, ["phase 2 — Open work", "Review delivery", "Clean records"],
                 "🟩🟨", "phase 2 — Open work", "18:30",
                 "Review delivery +1 more · plan: phase 3: New work +1 more"),
                (False, False, False, False, ["phase 2 — Open work"], "🟩🟨", "phase 2 — Open work",
                 "18:30", "plan: phase 3: New work +1 more"),
                (False, False, False, True, [], "🟩🟨🟨", "phase 2 — Open work",
                 "18:30", "plan: phase 3: New work +1 more"),
                (True, False, False, False, [], "—", "no phases", "—", "plan: phase 2: Open work +2 more"),
                (False, False, True, False, [], "🟩🟨", "phase 2 — Open work · fix: repair Notes · fix rounds 1 + 1 running",
                 "18:30", "plan: phase 3: New work +1 more"),
                (False, True, False, False, [], "🟩🟨", "phase 2 — Open work", "18:30", "—")):
            with self.subTest(finished=finished, missing=missing, fixing=fixing, other=other, queue_items=queue_items):
                queue_path = unit_queue.state_path(member.id)
                queue_path.parent.mkdir(parents=True, exist_ok=True)
                _ = queue_path.write_text(json.dumps(queue_items))
                tail = (event("pass_started", phase_id="2", phase_instance_id="open-2",
                              pass_instance_id="fix-2", pass_kind="fix", pass_activity="repair Notes")
                        + "\n" if fixing else "")
                lines = open_events.splitlines()
                if other:
                    lines.insert(1, json.dumps({"event_type": "phase_started", "phase_id": "outside",
                        "phase_title": "Other work", "phase_instance_id": "other", "phase_started_at": now - 700}))
                _ = run.write_text("\n".join(lines) + "\n" + tail + (event("run_finished") + "\n" if finished else ""))
                records["fallback"] = ({"plan": str(newest), "phases": [], "current": None} if fixing
                                       else typed_record(self.root / "as-built.md"))
                folder = self.root / "runs" / "run-2"
                folder.mkdir(parents=True, exist_ok=True)
                _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"owner_staff_id": member.id}))
                if finished:
                    _ = staff.clear_run(member.id, folder)
                else:
                    _ = staff.set_run(member.id, staff.OpenRun(folder))
                if missing:
                    newest.unlink()
                with (patch.object(plan_history, "read_run_events", wraps=plan_history.read_run_events) as reads,
                      patch.object(plan_history, "now_epoch", return_value=now)):
                    rendered = unit_phases.table([unit("fallback", None)], records, census_read=LOCAL,
                                                now=now, zone=ZoneInfo("UTC"))
                values: dict[str, str] = {}
                for section in (rendered.split("\n\n")[0], rendered.split("\n\n")[-1]):
                    cells = [cell.strip() for cell in section.splitlines()[2].split("|")[1:-1]]
                    headers = [cell.strip() for cell in section.splitlines()[0].split("|")[1:-1]]
                    values.update(zip(headers, cells, strict=True))
                expected = {"Unit": "fallback", "Status": "exited", "Notes": notes}
                if not finished:
                    expected.update({"Progress": progress, "Phase": "2", "ETA Next": eta,
                                     "ETA Plan": "18:30"})
                if other:
                    self.assertIn("| fallback | phase outside — Other work |", rendered)
                if queued != "—":
                    expected["Queued"] = queued
                self.assertEqual(values, expected)
                self.assertEqual([call.args[0] for call in reads.call_args_list], [run])

    def test_table_has_one_row_per_unit_and_escapes_pipes(self) -> None:
        legend = "Progress: 🟩 done · 🟨 open · ⬜ not started"
        owner = fixture_member("showrunner", name="table-owner")
        for name in ("one", "two"):
            _ = fixture_member("unit", name=name, parent=owner.id,
                               worktree=staff.PresentWorktree("natedev", self.root / name))
        record = typed_record(self.root / "plan.md", "running", "todo")
        current = record.current
        assert isinstance(current, phase_table.OpenPhase)
        current = replace(current, title="a|b")
        records: dict[str, object] = {"one": record._replace(current=current, phases=[current, record.phases[1]])}
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine):
                units = [unit("one", live("a|b")), unit("two", None)._replace(machine=machine)]
                column = " Machine |" if machine == "mac" else ""
                separator = " --- |" if machine == "mac" else ""
                first_machine = " natedev |" if machine == "mac" else ""
                second_machine = " mac |" if machine == "mac" else ""
                self.assertEqual(table_of(units, records), "\n".join([
                    f"| Unit | Status |{column} Progress | Phase | ETA Next |",
                    f"| --- | --- |{separator} --- | --- | --- |",
                    f"| one | idle |{first_machine} 🟨⬜ | 0/2 | no ETA stated |",
                    f"| two | {'—' if machine == 'mac' else 'exited'} |{second_machine} — | — | — |",
                    "",
                    legend,
                    "",
                    "| Unit | Notes | Queued |",
                    "| --- | --- | --- |",
                    "| one | phase 1 — a\\|b | plan: phase 2: Delivery 2 |",
                    "| two | — | — |",
                ]))

        config = self.root / "table-conf/build_targets.conf"
        configured = config.read_text()
        _ = config.write_text(configured.replace("kind.natedev=local", "kind.natedev=ssh\nkind.fixture-home=local"))
        selected = unit("one", live("a|b"))._replace(machine="fixture-home")
        self.assertEqual(table_of([selected], records).splitlines()[0],
                         "| Unit | Status | Progress | Phase | ETA Next |")
        _ = config.write_text(configured)

        records = {"one": typed_record(self.root / "plan.md", "todo")}
        self.assertIn("| one | idle | ⬜ | 0/1 |",
                      table_of([unit("one", live("a|b"))], records))
        self.assertIn("| one | not started | plan: phase 1: Delivery 1 |",
                      table_of([unit("one", live("a|b"))], records))
        for sample, expected in ((typed_record(self.root / "plan.md", "done"),
                                 "| Unit | Status | Progress | Phase |"),
                                (typed_record(self.root / "plan.md"), "| Unit | Status |"),
                                (None, "| Unit | Status |")):
            with self.subTest(finished_or_absent=sample):
                rendered = table_of([unit("one", live("a|b"))], {"one": sample})
                self.assertEqual(rendered.splitlines()[0], expected)
                if "| Progress |" in expected:
                    self.assertIn("\n\n" + legend + "\n\n| Unit | Notes |\n", rendered)
                else:
                    self.assertNotIn("Progress:", rendered)
                self.assertEqual(rendered.count("| Unit |"), 2)
        self.assertNotIn("Progress:", table_of([], {}))

    def test_plain_table_reads_live_activity_and_staff_holds_even_when_gone(self) -> None:
        owner = fixture_member("showrunner", name="table-owner")
        first = fixture_member("unit", name="first", parent=owner.id)
        second = fixture_member("unit", name="second", parent=owner.id)
        doc = self.root / "status-production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        owner = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        from scripts.production import session_staff

        first_session = unit("first", live("first")).session
        assert first_session is not None
        every = unit_lookup.LabeledProductions({owner.name: {"first": first_session}},
                                              {owner.name: ("first", "second")})

        def render() -> str:
            with (patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
                  patch.object(sessions, "running_sessions", return_value=[sessions.SessionRecord(
                      sessionId=first.current_session, pid=1, name="first", status="idle",
                      messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])])):
                return table_of(live_units.units_in(owner.name, every), {})

        initial = render()
        self.assertIn("| Unit | Status |", initial)
        self.assertIn("| first | idle |", initial)
        _ = staff.pause(second.id, "review the change")
        paused = render()
        self.assertIn("| first | idle |", paused)
        self.assertIn("| second | paused |", paused)
        _ = staff.resume(second.id)
        resumed = render()
        self.assertIn("| second | exited |", resumed)
        for activity, hold, expected, detached in (("busy", "", "busy", False), ("shell", "", "idle", False),
                ("busy", "pause", "paused", False), ("busy", "exit", "exited", False),
                ("idle", "", "busy", True), ("busy", "", "busy", True),
                ("starting", "", "busy", False), ("starting", "", "busy", True),
                ("starting", "pause", "paused", True), ("starting", "exit", "exited", True)):
            with self.subTest(activity=activity, hold=hold):
                _ = staff.resume(first.id)
                if hold == "pause":
                    _ = staff.pause(first.id, "review")
                elif hold == "exit":
                    _ = staff.exit(first.id, "stop")
                observed = sessions.SessionRecord(sessionId=first.current_session, pid=1, name="first",
                    status=activity, messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
                snapshot = staff.snapshot()
                with (patch.object(sessions, "running_sessions", return_value=[observed]),
                      patch.object(session_state, "running_detached_work", return_value=session_state.RunningDetachedWork(
                          frozenset({first.id}) if detached else frozenset())),
                      patch.object(staff, "snapshot", wraps=staff.snapshot) as staff_read,
                      patch.object(session_state, "classify", wraps=session_state.classify) as states):
                    units = live_units.units_in(owner.name, every)
                    self.assertEqual(units[0].status, expected)
                    staff_read.assert_called_once_with()
                    self.assertEqual(states.call_count, len(units))
        _ = staff.resume(first.id)
        self.assertEqual(staff.by_id(first.id), first)
        codex = unit_lookup.LabeledSession("first", unit_lookup.ClaudeUnknown("no Claude registry record"),
            HerdrPlace("codex", "w1:p1"), process=unit_lookup.ObservedProcess(41, "codex"),
            identity=unit_lookup.LiveSession(first.current_session))
        for observed, machine, hold, expected in (
                (codex, "natedev", "", "idle"),
                (None, "natedev", "", "exited"),
                (codex, "mac", "", "idle"),
                (None, "mac", "", "exited"),
                (None, "mac", "pause", "paused"),
                (None, "mac", "exit", "exited")):
            with self.subTest(process=observed, machine=machine, hold=hold):
                member = replace(first, worktree=staff.PresentWorktree(machine, self.root),
                    paused=staff.Paused("2026-11-08", "fixture", "pause") if hold == "pause" else staff.Active(),
                    exited=staff.Exited("2026-11-08", "fixture", "exit") if hold == "exit" else staff.Active())
                snapshot = staff.StaffSnapshot((owner, member, second))
                marked: dict[str, unit_lookup.LabeledSession] = {"first": observed} if observed is not None else {}
                listing = unit_lookup.LabeledProductions({owner.name: marked}, {owner.name: ("first", "second")})
                with (patch.object(staff, "snapshot", return_value=snapshot) as staff_read,
                      patch.object(sessions, "running_sessions", return_value=[])):
                    units = live_units.units_in(owner.name, listing)
                    self.assertEqual(units[0].status, expected)
                    self.assertEqual(units[0].machine, machine)
                    output = unit_phases.table(units, {}, snapshot=snapshot,
                                               census_read=tiles(sessions=[session_of(member, expected)]))
                    if machine == "mac":
                        self.assertIn("| Unit | Status | Machine |", output)
                        self.assertIn(f"| first | {expected} | mac |", output)
                        self.assertIn("| second | exited | — |", output)
                    else:
                        self.assertNotIn("Machine", output)
                        self.assertIn(f"| first | {expected} |", output)
                staff_read.assert_called_once_with()

    def test_read_failures_print_below_the_table_and_send_one_configured_message(self) -> None:
        self.assert_anomaly_delivery(send_code=0)

    def test_failed_anomaly_send_keeps_the_table_and_exit_code_and_adds_one_line(self) -> None:
        self.assert_anomaly_delivery(send_code=1)

    def assert_anomaly_delivery(self, *, send_code: int) -> None:
        from scripts.production import session_staff

        doc = self.root / "production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        owner = fixture_member("showrunner", name="anomaly-owner", doc=staff.LiveDoc("natedev", doc))
        member = fixture_member("unit", name="member", parent=owner.id)
        config = self.root / "config"
        config.mkdir()
        config_source = Path(__file__).resolve().parents[2] / "config/production.conf"
        _ = (config / "production.conf").write_text(re.sub(r"(?m)^UNIT_ANOMALIES_TO=.*$", "UNIT_ANOMALIES_TO=fixture-director", config_source.read_text()))
        def observed(*, record: staff.StaffSnapshot, running: object = None) -> tuple[session_staff.LabeledSession, ...]:
            self.assertEqual(record.by_id(member.id), member)
            self.assertIsNone(running)
            for pid, identity in enumerate(("unread-codex", "unread-claude"), 920000):
                session_staff.report_anomaly(f"pid {pid}: {identity}: session record is unreadable", pid=pid)
            return ()

        output, errors = io.StringIO(), io.StringIO()
        sender = Mock(return_value=subprocess.CompletedProcess(
            ["send.py"], send_code, "fixture delivery detail" if send_code else "",
            "fixture send failed\nfixture failure detail" if send_code else ""))
        with (patch.dict(os.environ, {"CONF_DIR": str(config)}),
              patch.object(unit_lookup, "observed_session_labels", side_effect=observed),
              patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(unit_phases, "production_records", return_value={}),
              patch.object(unit_phases, "subprocess", SimpleNamespace(run=sender)),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            result = unit_phases.main(["--production", owner.name])
        self.assertEqual(result, 0, errors.getvalue())
        lines = output.getvalue().splitlines()
        table_end = max(index for index, line in enumerate(lines) if line.startswith("| member |"))
        self.assertEqual(lines[:table_end + 1], [
            "| Unit | Status |", "| --- | --- |", "| member | exited |", "",
            "| Unit |", "| --- |", "| member |",
        ])
        anomalies = [line for line in lines if line.startswith("anomaly:")
                     and ("unread-codex" in line or "unread-claude" in line)]
        self.assertEqual(len(anomalies), 2, output.getvalue())
        for identity in ("unread-codex", "unread-claude"):
            matching = [line for line in anomalies if identity in line]
            self.assertEqual(len(matching), 1, output.getvalue())
            self.assertGreater(lines.index(matching[0]), table_end)
        self.assertNotIn("anomaly:", errors.getvalue())
        sender.assert_called_once()
        call = sender.call_args
        assert call is not None
        command = cast(list[str], call.args[0])
        self.assertEqual(command[command.index("--to") + 1], "fixture-director")
        message = command[command.index("--text") + 1]
        for line in anomalies:
            self.assertIn(line, message)
        extra = [line for line in lines[table_end + 1:] if line.strip() and line not in anomalies]
        self.assertEqual(len(extra), 1 if send_code else 0, output.getvalue())
        if send_code:
            self.assertEqual(extra, [
                "anomaly: could not send unit anomalies to fixture-director: "
                + "stderr: fixture send failed fixture failure detail stdout: fixture delivery detail"
            ])
        self.assertEqual(staff.by_id(member.id), member)

    def test_table_without_anomalies_sends_no_message(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        owner = fixture_member("showrunner", name="quiet-owner", doc=staff.LiveDoc("natedev", doc))
        member = fixture_member("unit", name="member", parent=owner.id)
        from scripts.production import session_staff

        output = io.StringIO()
        sender = Mock()
        with (patch.object(unit_lookup, "observed_session_labels", return_value=()),
              patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(unit_phases, "production_records", return_value={}),
              patch.object(unit_phases, "subprocess", SimpleNamespace(run=sender)), contextlib.redirect_stdout(output)):
            result = unit_phases.main(["--production", owner.name])
        self.assertEqual(result, 0)
        self.assertIn("| member | exited |", output.getvalue())
        self.assertNotIn("anomaly:", output.getvalue())
        sender.assert_not_called()
        self.assertEqual(staff.by_id(member.id), member)

    def test_census_failure_shows_no_status_or_workers_and_one_anomaly(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        owner = fixture_member("showrunner", name="failed-owner", doc=staff.LiveDoc("natedev", doc))
        _ = fixture_member("unit", name="member", parent=owner.id)
        from scripts.production import session_staff

        output = io.StringIO()
        sender = Mock(return_value=subprocess.CompletedProcess(["send.py"], 0, "", ""))
        with (patch.object(unit_lookup, "observed_session_labels", return_value=()),
              patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(unit_phases, "production_records", return_value={}),
              patch.object(census, "read", return_value=census.CensusFailed("census took longer than 10 s")),
              patch.object(unit_phases, "subprocess", SimpleNamespace(run=sender)), contextlib.redirect_stdout(output)):
            result = unit_phases.main(["--production", owner.name])
        self.assertEqual(result, 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[:3], ["| Unit |", "| --- |", "| member |"])
        self.assertEqual([line for line in lines if line.startswith("anomaly:")],
                         ["anomaly: census took longer than 10 s"])
        sender.assert_called_once()

    def production_document(self, doc: Path) -> None:
        _ = doc.write_text(doc.read_text() + f"\n- **Merge branch:** main\n"
                           + f"- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        _ = fixture_member("showrunner", name="show", doc=staff.LiveDoc(remote.local_machine(), doc))

    def test_main_keeps_plan_history_functions_intact_while_it_reads_and_renders(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text("# Production\n", encoding="utf-8")
        self.production_document(doc)
        headers = plan_history.run_started_event
        paths = plan_history.resolve_plan_path
        scans = plan_history.plan_runs
        calls: list[str] = []

        def records(_doc: Path) -> dict[str, object]:
            self.assertIs(plan_history.run_started_event, headers)
            self.assertIs(plan_history.resolve_plan_path, paths)
            self.assertIs(plan_history.plan_runs, scans)
            calls.append("records")
            return {}

        def render(_units: list[live_units.LiveUnit], _records: dict[str, object], *, zone: ZoneInfo,
                   census_read: census.Census | census.CensusFailed, columns: int | None) -> str:
            self.assertIs(plan_history.run_started_event, headers)
            self.assertIs(plan_history.resolve_plan_path, paths)
            self.assertIs(plan_history.plan_runs, scans)
            self.assertIsInstance(zone, ZoneInfo)
            self.assertIsNone(columns)
            self.assertIs(census_read, LOCAL)
            calls.append("table")
            return "phase table"

        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing([unit("one", live("one"))], ())),
              patch.object(unit_phases, "production_records", side_effect=records),
              patch.object(unit_phases, "table", side_effect=render),
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        self.assertEqual(calls, ["records", "table"])
        self.assertEqual(output.getvalue(), "phase table\n")
        self.assertIs(plan_history.run_started_event, headers)
        self.assertIs(plan_history.resolve_plan_path, paths)
        self.assertIs(plan_history.plan_runs, scans)

    def test_main_reads_shared_history_once_for_multiple_unit_plans(self) -> None:
        from scripts.production import session_staff

        doc = self.root / "production.md"
        _ = doc.write_text("# Production\n")
        self.production_document(doc)
        history = self.root / "history" / "runs"
        history.mkdir(parents=True)
        run_files: list[Path] = []
        for name in ("one", "two"):
            tree = self.root / name
            member = recorded_unit("show", name, tree, doc, plan="plan.md")
            _ = (tree / "plan.md").write_text(
                "# Plan\n### Phase 1 — First work · status: todo\n### Phase 2 — Later work · status: todo\n")
            folder = self.root / "runs" / name
            folder.mkdir(parents=True)
            _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"owner_staff_id": member.id}))
            _ = staff.set_run(member.id, staff.OpenRun(folder))
            path = history / f"{name}.jsonl"
            events: list[dict[str, object]] = [
                {"event_type": "run_started", "owner_staff_id": member.id, "run_id": name,
                 "working_dir": str(tree), "plan_doc": "plan.md", "run_started_at": 100},
                {"event_type": "phase_started", "phase_id": "1", "phase_title": "First work",
                 "phase_instance_id": name, "phase_started_at": 110},
                {"event_type": "pass_started", "phase_id": "1", "phase_instance_id": name,
                 "pass_instance_id": name, "pass_kind": "impl", "pass_activity": f"write {name}"},
            ]
            _ = path.write_text("".join(json.dumps(event) + "\n" for event in events))
            run_files.append(path)
        output = io.StringIO()
        with (patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(unit_lookup, "observed_session_labels", return_value=()),
              patch.object(plan_history, "run_started_event", wraps=plan_history.run_started_event) as headers,
              patch.object(staff, "snapshot", wraps=staff.snapshot) as staff_reads,
              patch.object(io, "open", wraps=io.open) as opened,
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        self.assertCountEqual([call.args[0] for call in headers.call_args_list], run_files)
        self.assertEqual(staff_reads.call_count, 1)
        for path in run_files:
            with self.subTest(run=path.name):
                reads = sum(call.args[0] == path for call in opened.call_args_list)
                self.assertEqual(reads - 1, 1, "Read the run events once after the header read.")
        self.assertEqual(output.getvalue().splitlines(), [
            "| Unit | Status | Progress | Phase | ETA Next |",
            "| --- | --- | --- | --- | --- |",
            "| one | exited | 🟨⬜ | 1 | no ETA stated |",
            "| two | exited | 🟨⬜ | 1 | no ETA stated |",
            "",
            "Progress: 🟩 done · 🟨 open · ⬜ not started",
            "",
            "| Unit | Notes | Queued |",
            "| --- | --- | --- |",
            "| one | phase 1 — First work · impl: write one | plan: phase 2: Later work |",
            "| two | phase 1 — First work · impl: write two | plan: phase 2: Later work |",
        ])

    def test_without_a_production_the_callers_staff_name_selects_it(self) -> None:
        owner = fixture_member("showrunner", name="table-owner")
        _ = fixture_member("unit", name="a", parent=owner.id)
        doc = self.root / "show-production.md"
        _ = doc.write_text("# Production\n", encoding="utf-8")
        self.production_document(doc)
        out = io.StringIO()
        with (patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "caller"}),
              patch.object(showrunners, "own_name", return_value="show") as found,
              patch.object(live_units, "read_units", return_value=live_units.UnitListing([unit("a", live("a"))], ())),
              patch.object(census, "read", return_value=census_for([unit("a", live("a"))])),
              patch.object(unit_phases, "production_records", return_value={}),
              contextlib.redirect_stdout(out)):
            self.assertEqual(unit_phases.main([]), 0)
        found.assert_called_once_with()
        self.assertIn("| a | idle |", out.getvalue())

    def test_a_session_that_is_no_showrunner_is_told_to_name_the_production(self) -> None:
        err = io.StringIO()
        with (patch.object(showrunners, "own_name", return_value=""),
              contextlib.redirect_stderr(err)):
            self.assertEqual(unit_phases.main([]), 1)
        self.assertIn("pass --production", err.getvalue())

    def test_style_kind_routes_to_the_ticket_table(self) -> None:
        doc = self.root / "style-production.md"
        _ = doc.write_text(f"# Production\n- **Kind:** style\n- **Merge branch:** style/main\n"
                           + f"- **Branch point:** main\n- **Showrunner checkout:** `{self.root}`\n")
        write_style_units(doc)
        self.production_document(doc)
        _ = subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True,
                           timeout=10)
        owner = fixture_member("showrunner", name="show", doc=staff.LiveDoc(remote.local_machine(), doc))
        _ = fixture_member("unit", name="names", parent=owner.id,
                           worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        units = [unit("names", live("names"))]
        records: dict[str, object] = {"names": {"plan": "/tmp/claude/names.md"}}
        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(units, ())),
              patch.object(phase_table, "show_production", return_value=json.dumps(records)),
              patch.object(style_units, "table", return_value="ticket table") as tickets,
              patch.object(style_ledger, "ledger_location", return_value=style_ledger.LedgerLocation(
                  self.root, self.root / "ledger.sqlite")) as locate,
              patch.object(unit_phases, "table") as phases,
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        tickets.assert_called_once_with(units, records, doc=doc, ledger_paths={"names": self.root / "ledger.sqlite"})
        locate.assert_called_once_with(self.root)
        phases.assert_not_called()
        self.assertEqual(output.getvalue(), "ticket table\n")

        unordered = [unit("zulu", live("zulu")), units[0], unit("Alpha", live("Alpha"))]
        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(unordered, ())),
              patch.object(phase_table, "show_production", return_value=json.dumps(records)),
              patch.object(style_units, "table", return_value="sorted ticket table") as tickets,
              patch.object(style_ledger, "ledger_location", return_value=style_ledger.LedgerLocation(
                  self.root, self.root / "ledger.sqlite")),
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        tickets.assert_called_once_with([unordered[2], units[0], unordered[0]], records, doc=doc,
                                        ledger_paths={"names": self.root / "ledger.sqlite"})
        self.assertEqual(output.getvalue(), "sorted ticket table\n")

    def test_product_parent_reports_direct_style_units_and_product_units(self) -> None:
        from scripts.style import style_config

        doc = self.root / "product-production.md"
        _ = doc.write_text(f"# Production\n- **Kind:** product\n- **Merge branch:** main\n"
                           + f"- **Showrunner checkout:** {self.root}\n")
        write_style_units(doc, (style_config.StyleUnitAssignment(
            "cargo-liner-style", ("names", "values", "types", "modules")),))
        self.production_document(doc)
        style_unit = unit("cargo-liner-style", live("cargo-liner-style"))
        product_unit = unit("feature", live("feature"))
        units = [product_unit, style_unit]
        output = io.StringIO()
        before = doc.read_bytes()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(units, ())),
              patch.object(phase_table, "show_production", return_value="{}"),
              patch.object(unit_phases, "style_ledger_paths", return_value={}),
              patch.object(unit_phases, "table", return_value="product table") as phases,
              patch.object(style_units, "table", return_value="ticket table") as tickets,
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        phases.assert_called_once_with([product_unit], {}, zone=read_production("show").zone,
                                                 census_read=LOCAL, columns=None)
        tickets.assert_called_once_with([style_unit], {}, doc=doc, ledger_paths={})
        self.assertEqual(doc.read_bytes(), before)
        self.assertEqual(output.getvalue(), "product table\n\nticket table\n")

    def test_other_or_absent_kind_keeps_the_phase_table(self) -> None:
        owner = fixture_member("showrunner", name="table-owner")
        _ = fixture_member("unit", name="a", parent=owner.id)
        for kind in ("", "- **Kind:** feature\n"):
            with self.subTest(kind=kind):
                doc = self.root / "phase-production.md"
                _ = doc.write_text("# Production\n" + kind, encoding="utf-8")
                self.production_document(doc)
                units = [unit("a", live("a"))]
                output = io.StringIO()
                with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(units, ())),
                      patch.object(census, "read", return_value=census_for(units)),
                      patch.object(unit_phases, "production_records", return_value={}),
                      patch.object(style_units, "table") as tickets, contextlib.redirect_stdout(output)):
                    self.assertEqual(unit_phases.main(["--production", "show"]), 0)
                tickets.assert_not_called()
                self.assertEqual(output.getvalue(),
                    "| Unit | Status |\n| --- | --- |\n| a | idle |\n\n| Unit |\n| --- |\n| a |\n")

                unordered = [unit("zulu", live("zulu"))._replace(machine="natedev"),
                             unit("Beta", live("Beta"))._replace(machine="mac"),
                             unit("alpha", live("alpha"))._replace(machine="natedev")]
                output = io.StringIO()
                with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(unordered, ())),
                      patch.object(unit_phases, "production_records", return_value={}),
                      patch.object(style_units, "table") as tickets, contextlib.redirect_stdout(output)):
                    self.assertEqual(unit_phases.main(["--production", "show"]), 0)
                tickets.assert_not_called()
                self.assertEqual(output.getvalue().splitlines(), [
                    "| Unit | Status | Machine |",
                    "| --- | --- | --- |",
                    "| alpha | exited | natedev |",
                    "| Beta | — | mac |",
                    "| zulu | exited | natedev |",
                    "",
                    "| Unit |",
                    "| --- |",
                    "| alpha |",
                    "| Beta |",
                    "| zulu |",
                ])

    def test_a_missing_plan_folder_keeps_ledger_data_and_an_unresolved_worktree_keeps_its_row(self) -> None:
        _ = subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        _ = self.enterContext(patch.dict(os.environ, {"STYLE_LEDGER_STATE_DIR": str(self.root / "state")}))  # pyright: ignore[reportAny]
        doc = self.root / "style-production.md"
        _ = doc.write_text(f"# Production\n- **Kind:** style\n- **Merge branch:** style/main\n"
                           + f"- **Branch point:** main\n- **Showrunner checkout:** `{self.root}`\n")
        write_style_units(doc)
        self.production_document(doc)
        owner = fixture_member("showrunner", name="show", doc=staff.LiveDoc(remote.local_machine(), doc))
        for name, folder in (("names", self.root), ("types", self.root / "gone")):
            _ = fixture_member("unit", name=name, parent=owner.id,
                               worktree=staff.PresentWorktree(remote.local_machine(), folder))
        decision = self.root / "decision.md"
        names = next(row for row in staff.initial_snapshot().find(kind="unit", parent=owner.id) if row.name == "names")
        _ = decision.write_text(f"---\nstatus: open\nunit: {names.id}\nplan: approved\nstyle_rule_blob: names\n---\n")
        database = style_ledger.ledger_location(self.root).database
        database.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.executescript("""
                CREATE TABLE rule_texts(blob TEXT, text BLOB);
                CREATE TABLE tickets(id TEXT,unit TEXT,kind TEXT,family TEXT,rule TEXT,crate TEXT,
                    files TEXT,sites TEXT,rules TEXT,finding_ids TEXT,state TEXT,created_at TEXT);
                CREATE TABLE held_findings(proposal TEXT,state TEXT,build_ticket TEXT,built_at TEXT);
                CREATE TABLE refill_results(unit TEXT,at TEXT,outcome TEXT,split_at TEXT,
                    ready INTEGER,changed INTEGER,structure INTEGER,other INTEGER);
            """)
            _ = connection.execute("INSERT INTO rule_texts VALUES ('names',?)", (b"---\nfamily: names\n---\n",))
            _ = connection.execute("INSERT INTO tickets VALUES ('12',?,'proposal','names',"
                + "'global:rust/read.md','alpha','[]','[]','[]','[\"one\"]','assigned','2026-10-01')", (str(names.id),))
            _ = connection.execute("INSERT INTO held_findings VALUES (?,'filed','12','')", (str(decision),))
        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing([unit("names", live("names")),
                                                                        unit("types", live("types"))], ())),
              patch.object(phase_table, "show_production", return_value=json.dumps({
                  "names": {"unavailable": "plan cannot be read"},
                  "types": {"unavailable": "worktree cannot be read"}})),
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        self.assertIn("| names | idle | #12 · read · alpha · 1 finding | starting 1 finding | 0 | 0 | 1 |", output.getvalue())
        self.assertIn("| types | idle | idle | idle | 0 | 0 | 0 |", output.getvalue())

    def test_production_of_reads_the_staff_row_addressed_to_the_session(self) -> None:
        doc = self.root / "show-production.md"
        for name, target in (("showrunner-other", "session:someone-else"), ("showrunner-show", "session:caller")):
            directory = self.root / name
            directory.mkdir()
            _ = (directory / "conf").write_text(
                f"TARGET={target}\nCHECK=zsh /opt/production_check.sh show\n", encoding="utf-8")
        caller = fixture_runtime_id("unit-phases-caller")
        _ = fixture_member("showrunner", name="show", runtime_id=caller, doc=staff.LiveDoc(remote.local_machine(), doc))
        self.assertEqual(showrunners.production_of(caller, self.root), doc)
        self.assertIsNone(showrunners.production_of("nobody", self.root))
        self.assertIsNone(showrunners.production_of("", self.root))



from scripts.production.staff_test_support import StaffTestCase


class BranchLedgerLocationTests(StaffTestCase):
    def test_style_ledger_uses_the_staff_worktree(self) -> None:
        fixture = self.separated_unit_folders()
        paths = unit_phases.style_ledger_paths(fixture.doc, [live_units.LiveUnit("member", None)])
        self.assertEqual(paths, {"member": style_ledger.ledger_location(fixture.worktree).database})


if __name__ == "__main__":
    _ = unittest.main()
