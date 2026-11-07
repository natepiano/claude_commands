"""Tests for each unit's staff name, phase and task."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import Mock, patch


from scripts.production import live_units
from scripts.production import showrunners
from scripts.production import unit_lookup
from scripts.herdr.herdr import HerdrPlace
from scripts.production import unit_phases
from scripts.production import style_units
from scripts.style import style_ledger
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member, fixture_runtime_id

from scripts.direct import phase_table
from scripts.direct import plan_history


def unit(name: str, claude: unit_lookup.Claude | None) -> live_units.LiveUnit:
    if claude is None:
        return live_units.LiveUnit(name, None)
    return live_units.LiveUnit(name, unit_lookup.LabeledSession(name, claude, HerdrPlace(f"label-of-{name}", "w1:p1")))


def live(name: str) -> unit_lookup.LiveClaude:
    return unit_lookup.LiveClaude(name, f"{name}-id", f"/run/{name}.sock", 1)


def phase_rows(*statuses: str) -> list[object]:
    return [{"phase": str(index + 1), "status": status} for index, status in enumerate(statuses)]


def open_record(plan: Path) -> dict[str, object]:
    return {"plan": str(plan), "current": {"phase": "3", "of": 7, "title": "Every session records itself"},
            "plan_finish": None, "phases": phase_rows("done", "done", "running", "todo")}


def event(kind: str, **fields: str) -> str:
    return json.dumps({"event_type": kind, **fields})


class UnitPhasesTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        from scripts.message import process_table
        sessions = self.root / "sessions"
        sessions.mkdir()
        self.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(sessions)}))
        _ = self.enterContext(patch.object(process_table, "snapshot", return_value={}))

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
                rendered = unit_phases.table([unit("namecheck", session)], {})
                self.assertEqual(rendered.splitlines()[2], "| namecheck | active | — | — |")

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
                self.assertEqual(unit_phases.phase_cell(record), expected)

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
        self.assertEqual(len(task), unit_phases.TASK_WIDTH)
        self.assertTrue(task.startswith("Verification: verify.sh test hana --filter"))
        self.assertTrue(task.endswith("…"))

    def test_a_unit_with_no_open_phase_shows_no_task(self) -> None:
        with patch.object(plan_history, "plan_runs") as runs:
            self.assertEqual(unit_phases.task_cell({"plan": "/p.md", "current": None, "phases": []}), "—")
        runs.assert_not_called()

    def test_table_has_one_row_per_unit_and_escapes_pipes(self) -> None:
        owner = fixture_member("showrunner", name="table-owner")
        for name in ("one", "two"):
            _ = fixture_member("unit", name=name, parent=owner.id)
        records: dict[str, object] = {"one": {"current": {"phase": "1", "of": 2, "title": "a|b"}}}
        self.assertEqual(unit_phases.table([unit("one", live("a|b")), unit("two", None)], records), "\n".join([
            "| Unit | Status | Phase | Task |",
            "| --- | --- | --- | --- |",
            "| one | active | 1 of 2 · a\\|b | — |",
            "| two | active | — | — |",
        ]))

        records = {"one": {"current": None, "phases": phase_rows("todo")}}
        self.assertIn("| one | active | not started | — |",
                      unit_phases.table([unit("one", live("a|b"))], records))

    def test_plain_table_reads_active_and_paused_status_from_staff_even_when_gone(self) -> None:
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
            with patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())):
                return unit_phases.table(live_units.units_in(owner.name, every), {})

        initial = render()
        self.assertIn("| Unit | Status | Phase | Task |", initial)
        self.assertIn("| first | active | — | — |", initial)
        _ = staff.pause(second.id, "review the change")
        paused = render()
        self.assertIn("| first | active | — | — |", paused)
        self.assertIn("| second | paused | — | — |", paused)
        _ = staff.resume(second.id)
        resumed = render()
        self.assertIn("| second | active | — | — |", resumed)
        self.assertEqual(staff.by_id(first.id), first)

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
              patch.object(phase_table, "show_production", return_value="{}"),
              patch.object(unit_phases, "subprocess", SimpleNamespace(run=sender)),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            result = unit_phases.main(["--production", owner.name])
        self.assertEqual(result, 0, errors.getvalue())
        lines = output.getvalue().splitlines()
        table_end = next(index for index, line in enumerate(lines) if line.startswith("| member |"))
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
              patch.object(phase_table, "show_production", return_value="{}"),
              patch.object(unit_phases, "subprocess", SimpleNamespace(run=sender)), contextlib.redirect_stdout(output)):
            result = unit_phases.main(["--production", owner.name])
        self.assertEqual(result, 0)
        self.assertIn("| member | active |", output.getvalue())
        self.assertNotIn("anomaly:", output.getvalue())
        sender.assert_not_called()
        self.assertEqual(staff.by_id(member.id), member)

    def production_document(self, doc: Path) -> None:
        _ = doc.write_text(doc.read_text() + f"\n- **Merge branch:** main\n"
                           + f"- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        _ = fixture_member("showrunner", name="show", doc=staff.LiveDoc("natedev", doc))

    def test_main_keeps_plan_history_functions_intact_while_it_reads_and_renders(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text("# Production\n", encoding="utf-8")
        self.production_document(doc)
        headers = plan_history.run_started_event
        paths = plan_history.resolve_plan_path
        scans = plan_history.plan_runs
        calls: list[str] = []

        def records(_doc: Path) -> str:
            self.assertIs(plan_history.run_started_event, headers)
            self.assertIs(plan_history.resolve_plan_path, paths)
            self.assertIs(plan_history.plan_runs, scans)
            calls.append("records")
            return "{}"

        def render(_units: list[live_units.LiveUnit], _records: dict[str, object]) -> str:
            self.assertIs(plan_history.run_started_event, headers)
            self.assertIs(plan_history.resolve_plan_path, paths)
            self.assertIs(plan_history.plan_runs, scans)
            calls.append("table")
            return "phase table"

        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing([unit("one", live("one"))], ())),
              patch.object(phase_table, "show_production", side_effect=records),
              patch.object(unit_phases, "table", side_effect=render),
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        self.assertEqual(calls, ["records", "table"])
        self.assertEqual(output.getvalue(), "phase table\n")
        self.assertIs(plan_history.run_started_event, headers)
        self.assertIs(plan_history.resolve_plan_path, paths)
        self.assertIs(plan_history.plan_runs, scans)

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
              patch.object(phase_table, "show_production", return_value="{}"),
              contextlib.redirect_stdout(out)):
            self.assertEqual(unit_phases.main([]), 0)
        found.assert_called_once_with()
        self.assertIn("| a | active | — | — |", out.getvalue())

    def test_a_session_that_is_no_showrunner_is_told_to_name_the_production(self) -> None:
        err = io.StringIO()
        with (patch.object(showrunners, "own_name", return_value=""),
              contextlib.redirect_stderr(err)):
            self.assertEqual(unit_phases.main([]), 1)
        self.assertIn("pass --production", err.getvalue())

    def test_style_kind_routes_to_the_ticket_table(self) -> None:
        doc = self.root / "style-production.md"
        _ = doc.write_text(f"# Production\n- **Kind:** style\n- **Showrunner checkout:** `{self.root}`\n")
        self.production_document(doc)
        _ = subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True,
                           timeout=10)
        owner = fixture_member("showrunner", name="show", doc=staff.LiveDoc("natedev", doc))
        _ = fixture_member("unit", name="names", parent=owner.id,
                           worktree=staff.PresentWorktree("natedev", self.root))
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
                      patch.object(phase_table, "show_production", return_value="{}"),
                      patch.object(style_units, "table") as tickets, contextlib.redirect_stdout(output)):
                    self.assertEqual(unit_phases.main(["--production", "show"]), 0)
                tickets.assert_not_called()
                self.assertEqual(output.getvalue(), "| Unit | Status | Phase | Task |\n| --- | --- | --- | --- |\n| a | active | — | — |\n")

    def test_a_missing_plan_folder_keeps_ledger_data_and_an_unresolved_worktree_keeps_its_row(self) -> None:
        _ = subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        _ = self.enterContext(patch.dict(os.environ, {"STYLE_LEDGER_STATE_DIR": str(self.root / "state")}))  # pyright: ignore[reportAny]
        doc = self.root / "style-production.md"
        _ = doc.write_text(f"# Production\n- **Kind:** style\n- **Showrunner checkout:** `{self.root}`\n")
        self.production_document(doc)
        owner = fixture_member("showrunner", name="show", doc=staff.LiveDoc("natedev", doc))
        for name, folder in (("names", self.root), ("types", self.root / "gone")):
            _ = fixture_member("unit", name=name, parent=owner.id,
                               worktree=staff.PresentWorktree("natedev", folder))
        decision = self.root / "decision.md"
        names = next(row for row in staff.initial_snapshot().find(kind="unit", parent=owner.id) if row.name == "names")
        _ = decision.write_text(f"---\nstatus: open\nunit: {names.id}\nplan: approved\n---\n")
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
        self.assertIn("| names | active | #12 · read · alpha · 1 finding | starting 1 finding | — | 0 | 0 | 1 |", output.getvalue())
        self.assertIn("| types | active | idle | idle | — | 0 | 0 | 0 |", output.getvalue())

    def test_production_of_reads_the_staff_row_addressed_to_the_session(self) -> None:
        doc = self.root / "show-production.md"
        for name, target in (("showrunner-other", "session:someone-else"), ("showrunner-show", "session:caller")):
            directory = self.root / name
            directory.mkdir()
            _ = (directory / "conf").write_text(
                f"TARGET={target}\nCHECK=zsh /opt/production_check.sh show\n", encoding="utf-8")
        caller = fixture_runtime_id("unit-phases-caller")
        _ = fixture_member("showrunner", name="show", runtime_id=caller, doc=staff.LiveDoc("natedev", doc))
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
