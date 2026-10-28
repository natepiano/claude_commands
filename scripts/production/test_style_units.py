"""The style units table reads ticket work, dated builds and approved work left."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch
from zoneinfo import ZoneInfo

import live_units
import proposal_issue
import style_units
import style_ledger
import unit_lookup
import unit_table_cells
from herdr import HerdrPlace
from scripts.direct import plan_history


NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
ZONE = ZoneInfo("America/New_York")
RULE = "global:rust/comments-name-code-constructs.md"


def live_unit(name: str, session: str = "") -> live_units.LiveUnit:
    if not session:
        return live_units.LiveUnit(name, None)
    claude = unit_lookup.LiveClaude(session, session + "-id", "/tmp/claude/test.sock", 1)
    marked = unit_lookup.MarkedUnit(name, claude, HerdrPlace("test", "w1:p1"))
    return live_units.LiveUnit(name, marked)


class StyleUnitsTests(unittest.TestCase):
    root: Path = Path()
    database: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp/claude")))
        self.database = self.root / "ledger.sqlite"
        with closing(sqlite3.connect(self.database)) as connection:
            _ = connection.executescript("""
                CREATE TABLE tickets (
                    id TEXT PRIMARY KEY, unit TEXT, kind TEXT, family TEXT, rule TEXT, crate TEXT,
                    files TEXT, sites TEXT, rules TEXT, finding_ids TEXT, state TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE held_findings (
                    finding_id INTEGER PRIMARY KEY, proposal TEXT, state TEXT, build_ticket TEXT,
                    built_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE refill_results (
                    unit TEXT PRIMARY KEY, at TEXT NOT NULL, outcome TEXT NOT NULL,
                    split_at TEXT NOT NULL DEFAULT '', ready INTEGER NOT NULL DEFAULT 0,
                    changed INTEGER NOT NULL DEFAULT 0, structure INTEGER NOT NULL DEFAULT 0,
                    other INTEGER NOT NULL DEFAULT 0
                );
            """)

    def ticket(self, *, unit: str = "names-unit", number: str = "42", kind: str = "proposal",
               rule: str = RULE, rules: tuple[str, ...] = (), count: int = 2, state: str = "assigned",
               created_at: str = "2026-10-27T10:00:00+00:00", family: str = "names") -> str:
        ticket_id = unit + "-" + number
        items = list(range(count))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            _ = connection.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                ticket_id, unit, kind, family, rule, "cargo-liner", json.dumps(items), json.dumps(items),
                json.dumps(rules), json.dumps(items), state, created_at, "2026-10-28T11:59:00+00:00",
            ))
        return ticket_id

    def decision(self, name: str, unit: str, answer: str = "approved") -> Path:
        path = self.root / (name + ".md")
        fields = {"approved": f"unit: {unit}\nplan: approved\n", "unanswered": "",
                  "declined": f"unit: {unit}\nplan: approved\nstyle_answer: declined\n"}
        _ = path.write_text("---\nstatus: open\n" + fields[answer] + "---\n", encoding="utf-8")
        return path

    def findings(self, proposal: Path, state: str, count: int = 1, *, ticket: str = "",
                 built_at: str = "", updated_at: str = "2026-10-28T11:59:00+00:00") -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            _ = connection.executemany(
                "INSERT INTO held_findings(proposal,state,build_ticket,built_at,updated_at) VALUES (?,?,?,?,?)",
                [(str(proposal), state, ticket, built_at, updated_at)] * count,
            )

    def refill(self, unit: str = "names-unit", *, split_at: str = "2026-10-28T11:30:00+00:00") -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            _ = connection.execute("INSERT INTO refill_results VALUES (?,?,?,?,?,?,?,?)", (
                unit, "2026-10-28T13:45:00+00:00", "waiting for structure: cargo-liner", split_at, 1234, 20, 30, 16,
            ))

    def render(self, units: list[live_units.LiveUnit] | None = None,
               records: dict[str, object] | None = None) -> str:
        selected = [live_unit("names-unit", "names")] if units is None else units
        return style_units.table(selected, {} if records is None else records, now=NOW, zone=ZONE,
                                 ledger_paths={unit.unit: self.database for unit in selected})

    def test_every_ticket_kind_uses_its_own_count_and_singular(self) -> None:
        for kind, noun in (("proposal", "finding"), ("listed", "site"), ("read", "file"), ("structure", "file")):
            for count in (1, 2):
                with self.subTest(kind=kind, count=count):
                    with closing(sqlite3.connect(self.database)) as connection, connection:
                        _ = connection.execute("DELETE FROM tickets")
                    _ = self.ticket(kind=kind, count=count)
                    expected = f"#42 · comments-name-code-constructs · cargo-liner · {count} {noun}{'' if count == 1 else 's'}"
                    self.assertIn(f"| names | {expected} |", self.render())
        self.assertTrue(self.render().startswith("| Unit | Ticket | Step | Built | Left |\n"))

    def test_ticket_count_comes_from_the_payload_for_its_kind(self) -> None:
        for kind, noun, expected in (("proposal", "findings", 3), ("listed", "sites", 2), ("read", "file", 1),
                                     ("structure", "file", 1)):
            with self.subTest(kind=kind):
                ticket = style_units.Ticket("names-unit-104", "names-unit", kind, RULE, "cargo-liner",
                                            '["file.rs"]', '[{}, {}]', "[]", '["a", "b", "c"]', "assigned")
                self.assertEqual(style_units.ticket_cell(ticket),
                                 f"#104 · comments-name-code-constructs · cargo-liner · {expected} {noun}")

    def test_empty_rule_uses_the_first_rule_stem_and_the_extra_rule_count(self) -> None:
        for rules, expected in (((RULE,), "comments-name-code-constructs"),
                                ((RULE, "local:rust/one.md", "global:rust/two.md"), "comments-name-code-constructs+2")):
            with self.subTest(rules=rules):
                ticket = style_units.Ticket("names-unit-8", "names-unit", "read", "", "cargo-liner", '["a"]', "[]",
                                            json.dumps(rules), "[]", "assigned")
                self.assertEqual(style_units.ticket_cell(ticket), f"#8 · {expected} · cargo-liner · 1 file")
        ticket = style_units.Ticket("names-unit-9", "names-unit", "read", RULE, "cargo-liner", '["a"]', "[]",
                                    '["global:rust/ignored.md"]', "[]", "assigned")
        self.assertIn("· comments-name-code-constructs ·", style_units.ticket_cell(ticket))

    def test_oldest_open_ticket_wins_and_closed_or_other_unit_tickets_do_not(self) -> None:
        _ = self.ticket(number="1", state="merged", created_at="2020-01-01")
        _ = self.ticket(number="2", state="dropped", created_at="2020-01-02")
        _ = self.ticket(number="3", state="queued", created_at="2020-01-03")
        _ = self.ticket(unit="types-unit", number="4", created_at="2020-01-04")
        _ = self.ticket(number="6", created_at="2026-10-28")
        _ = self.ticket(number="5", state="checkpointed", created_at="2026-10-27")
        self.assertIn("| names | #5 ·", self.render())
        self.assertIn("| checkpoint sent |", self.render())

    def test_idle_with_and_without_a_refill_record_uses_the_production_zone(self) -> None:
        self.assertIn("| names | idle | — | 0 (0 in 24 h) | 0 |", self.render())
        self.refill()
        self.assertIn("| names | idle — waiting for structure: cargo-liner (09:45) |", self.render())

    def test_step_is_the_label_of_the_newest_open_step_with_eta_in_the_production_zone(self) -> None:
        old = self.root / "old.jsonl"
        newest = self.root / "new.jsonl"
        _ = old.write_text(json.dumps({"event_type": "activity_started", "activity_label": "Old"}) + "\n")
        events: list[dict[str, str]] = [
            {"event_type": "phase_started"},
            {"event_type": "pass_started", "pass_instance_id": "p", "pass_kind": "impl", "pass_activity": "long text"},
            {"event_type": "activity_started", "activity_instance_id": "a", "activity_label": "Verification",
             "activity_text": "a long command that does not belong in the step"},
        ]
        _ = newest.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        record: dict[str, object] = {"plan": str(self.root / "plan.md"),
                                     "current": {"eta": {"time": "2026-10-28T16:15:00+02:00"}}}
        with patch.object(plan_history, "plan_runs", return_value=[old, newest]):
            output = self.render(records={"names-unit": record})
            self.assertIn("| Verification · ETA 10:15 |", output)
            self.assertNotIn("long command", output)
            events.append({"event_type": "activity_finished", "activity_instance_id": "a"})
            _ = newest.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            self.assertIn("| impl · ETA 10:15 |", self.render(records={"names-unit": record}))

    def test_closed_steps_show_checkpoint_sent_or_a_dash(self) -> None:
        run = self.root / "closed.jsonl"
        _ = run.write_text('\n'.join((json.dumps({"event_type": "phase_started"}),
                                      json.dumps({"event_type": "pass_started", "pass_instance_id": "p", "pass_kind": "impl"}),
                                      json.dumps({"event_type": "phase_finished"}))) + '\n')
        record: dict[str, object] = {"plan": str(self.root / "plan.md"), "current": None}
        with patch.object(plan_history, "plan_runs", return_value=[run]):
            self.assertEqual(style_units.step_cell(record, True, ZONE), "checkpoint sent")
            self.assertEqual(style_units.step_cell(record, False, ZONE), "—")
        with patch.object(plan_history, "plan_runs", return_value=[]):
            self.assertEqual(style_units.step_cell(record, True, ZONE), "checkpoint sent")
            self.assertEqual(style_units.step_cell(record, False, ZONE), "—")

    def test_no_open_step_or_checkpoint_shows_the_current_eta_alone(self) -> None:
        record: dict[str, object] = {"plan": str(self.root / "plan.md"),
                                     "current": {"eta": {"time": "2026-10-28T16:15:00+02:00"}}}
        with patch.object(plan_history, "plan_runs", return_value=[]):
            self.assertEqual(style_units.step_cell(record, False, ZONE), "ETA 10:15")
            self.assertIn("| idle | ETA 10:15 |", self.render(records={"names-unit": record}))

    def test_built_counts_use_the_decision_family_and_built_at_instead_of_updated_at(self) -> None:
        decision = self.decision("names", "project-style-names-unit")
        self.findings(decision, "built", built_at="2026-10-28T11:00:00+00:00", updated_at="2020-01-01")
        self.findings(decision, "built", built_at="2026-10-27T12:00:00+00:00")
        self.findings(decision, "built", built_at="2026-10-27T11:59:59+00:00")
        self.findings(decision, "built")
        self.findings(self.decision("types", "types-unit"), "built", built_at="2026-10-28T11:00:00+00:00")
        self.assertIn("| 4 (2 in 24 h) |", self.render())
        self.assertIn("All families: 5 built", self.render())

    def test_left_shows_the_approved_total_and_recorded_counts_with_their_age(self) -> None:
        approved = self.decision("approved", "names-unit")
        self.findings(approved, "filed", 1300)
        for state in ("unfiled", "waived", "withdrawn"):
            self.findings(approved, state, 3)
        self.findings(self.decision("declined", "names-unit", "declined"), "filed", 7)
        self.findings(self.decision("unanswered", "names-unit", "unanswered"), "filed", 9)
        self.findings(self.decision("types", "types-unit"), "filed", 11)
        self.findings(self.root / "missing.md", "filed", 13)
        self.refill()
        output = self.render()
        self.assertIn("| 1,300: ready 1,234 · changed 20 · structure 30 · other 16 (30 min) |", output)
        self.assertIn("All families: 0 built · 1,311 left", output)

    def test_left_with_default_reserved_columns_uses_only_the_approved_total(self) -> None:
        self.findings(self.decision("approved", "names-unit"), "filed", 1234)
        self.refill(split_at="")
        self.assertIn("| 1,234 |", self.render())

    def test_split_age_uses_whole_minutes_hours_and_days_from_its_offset(self) -> None:
        for measured_at, age in (("2026-10-28T13:59:01+02:00", "0 min"),
                                 ("2026-10-28T11:01:00+00:00", "59 min"),
                                 ("2026-10-28T11:00:00+00:00", "1 h"),
                                 ("2026-10-27T13:00:00+00:00", "23 h"),
                                 ("2026-10-27T12:00:00+00:00", "1 d"),
                                 ("2026-10-25T11:59:00+00:00", "3 d")):
            with self.subTest(measured_at=measured_at):
                split = style_ledger.RecordedSplit(1000, 2000, 3000, 4000, measured_at)
                self.assertEqual(style_units.left_cell(10000, split, NOW),
                                 f"10,000: ready 1,000 · changed 2,000 · structure 3,000 · other 4,000 ({age})")

    def test_a_first_zero_split_is_recorded_and_still_shows_idle_without_a_refill(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            _ = connection.execute("INSERT INTO refill_results(unit,at,outcome,split_at) VALUES (?,'','',?)",
                                   ("names-unit", "2026-10-28T10:00:00+00:00"))
        self.assertIn("| names | idle | — | 0 (0 in 24 h) | "
                      + "0: ready 0 · changed 0 · structure 0 · other 0 (2 h) |", self.render())

    def test_a_built_finding_without_a_decision_uses_its_build_ticket_family(self) -> None:
        ticket = self.ticket(unit="types-unit", family="names", state="merged")
        self.findings(self.root / "missing.md", "built", 5, ticket=ticket, built_at="2026-10-28T11:00:00+00:00")
        self.findings(self.root / "missing.md", "filed", 7, ticket=ticket)
        self.findings(self.root / "missing.md", "built", 3, ticket="missing-ticket")
        self.assertIn("| 5 (5 in 24 h) | 0 |", self.render())

    def test_total_with_a_rate_counts_all_families_once_and_uses_thousands_commas(self) -> None:
        names = self.decision("names", "names-unit")
        types = self.decision("types", "types-unit")
        self.findings(names, "built", 1200, built_at="2026-10-28T11:00:00+00:00")
        self.findings(names, "built", 34, built_at="2026-10-20T11:00:00+00:00")
        self.findings(types, "built", 100, built_at="2026-10-28T11:00:00+00:00")
        self.findings(names, "filed", 2600)
        self.findings(types, "filed", 1300)
        units = [live_unit("names-unit", "names"), live_unit("types-unit", "types")]
        output = self.render(units)
        self.assertEqual(output.splitlines()[-1], "All families: 1,334 built · 3,900 left · 1,300 built in the last 24 h · "
                         + "ETA at that rate 2026-10-31")
        self.assertIn("| 1,234 (1,200 in 24 h) | 2,600 |", output)

    def test_total_without_a_recent_build_has_no_eta(self) -> None:
        decision = self.decision("names", "names-unit")
        self.findings(decision, "built", 1234, built_at="2026-10-01T12:00:00+00:00")
        self.findings(decision, "filed", 2345)
        self.assertEqual(self.render().splitlines()[-1],
                         "All families: 1,234 built · 2,345 left · 0 built in the last 24 h · no ETA: nothing built in 24 h")

    def test_each_decision_is_read_once_across_groups_and_shared_database_units(self) -> None:
        decision = self.decision("names", "names-unit")
        self.findings(decision, "filed")
        self.findings(decision, "built", built_at="2026-10-28T10:00:00+00:00")
        self.findings(decision, "built", built_at="2026-10-28T11:00:00+00:00")
        with patch.object(style_units, "decision_answer", wraps=proposal_issue.decision_answer) as answer:
            _ = self.render([live_unit("names-unit"), live_unit("types-unit")])
        self.assertEqual(answer.call_count, 1)

    def test_missing_database_or_tables_give_empty_data_without_creating_them(self) -> None:
        for missing in ("tickets", "held_findings", "refill_results"):
            with self.subTest(table=missing):
                database = self.root / (missing + ".sqlite")
                with (closing(sqlite3.connect(self.database)) as source,
                      closing(sqlite3.connect(database)) as connection):
                    source.backup(connection)
                    with connection:
                        _ = connection.execute(f"DROP TABLE {missing}")
                before = database.read_bytes()
                output = style_units.table([live_unit("names-unit")], {}, now=NOW, zone=ZONE,
                                           ledger_paths={"names-unit": database})
                self.assertIn("| gone | idle | — | 0 (0 in 24 h) | 0 |", output)
                self.assertEqual(database.read_bytes(), before)
        database = self.root / "absent" / "ledger.sqlite"
        output = style_units.table([live_unit("names-unit")], {}, now=NOW, zone=ZONE,
                                   ledger_paths={"names-unit": database})
        self.assertIn("All families: 0 built · 0 left", output)
        self.assertFalse(database.parent.exists())

    def test_the_ledger_is_opened_once_read_only_and_a_write_through_that_connection_fails(self) -> None:
        connect = sqlite3.connect
        opened: list[str] = []

        def read_only_connection(database: str, *, uri: bool) -> sqlite3.Connection:
            self.assertTrue(uri)
            self.assertEqual(database, self.database.resolve().as_uri() + "?mode=ro")
            connection = connect(database, uri=uri)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                _ = connection.execute("CREATE TABLE forbidden(value TEXT)")
            opened.append(database)
            return connection

        before = self.database.read_bytes()
        with (patch.object(sqlite3, "connect", side_effect=read_only_connection),
              patch.object(style_ledger, "Ledger", side_effect=AssertionError("no migrations")),
              patch.object(style_ledger, "open_ledger", side_effect=AssertionError("no writer"))):
            _ = self.render([live_unit("names-unit"), live_unit("types-unit")])
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.database.read_bytes(), before)

    def test_a_caller_must_supply_ledger_paths_even_when_a_phase_record_names_a_plan(self) -> None:
        records: dict[str, object] = {"names-unit": {"plan": str(self.root / "names.md")}}
        with (patch.object(style_ledger, "ledger_location", side_effect=AssertionError("no phase-based lookup")),
              self.assertRaisesRegex(TypeError, "ledger_paths")):
            _ = style_units.table([live_unit("names-unit")], records)  # pyright: ignore[reportCallIssue, reportUnknownVariableType]

    def test_an_explicit_ledger_path_keeps_ticket_and_counts_when_the_phase_record_is_unavailable(self) -> None:
        _ = self.ticket(count=3)
        self.findings(self.decision("approved", "names-unit"), "filed", 7)
        records: dict[str, object] = {"names-unit": {"unavailable": "plan cannot be read"}}
        with patch.object(style_ledger, "ledger_location", side_effect=AssertionError("the caller resolved the path")):
            output = self.render(records=records)
        self.assertIn("| names | #42 · comments-name-code-constructs · cargo-liner · 3 findings | — | 0 (0 in 24 h) | 7 |", output)

    def test_session_cells_and_pipes_keep_the_phase_table_meaning(self) -> None:
        units = [live_unit("names-unit", "names|worker"), live_unit("types-unit")]
        output = self.render(units)
        self.assertIn("| names\\|worker | idle |", output)
        self.assertIn("| gone | idle |", output)
        self.assertNotIn("| names-unit |", output)
        self.assertEqual(unit_table_cells.session_cell(units[0]), "names|worker")


if __name__ == "__main__":
    _ = unittest.main()
