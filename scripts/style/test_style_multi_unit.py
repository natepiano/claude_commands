"""Test configured ownership through real planner and ledger operations."""

from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from scripts.production.staff_test_support import fixture_unit_location, recorded_showrunner
from scripts.style import rules, style_ledger, style_plan
from scripts.style.ledger_test_support import DEFAULT_STYLE_ASSIGNMENTS, ticket_owner_id, write_style_units
from scripts.style.style_config import StyleUnitAssignment
from scripts.style.style_plan_test_support import StylePlanFixture


FAMILIES: tuple[rules.Family, ...] = ("names", "values", "types", "modules")


class StyleMultiUnitTests(StylePlanFixture):
    def finish_ticket(self, ticket: style_ledger.TicketRecord) -> None:
        """Record the selected cells and complete the unchanged checkpoint."""
        ledger = self.ledger()
        cells = ticket["cells"] or [(rule, path) for rule in ticket["rules"] for path in ticket["files"]]
        for rule, path in cells:
            self.record(rule, path, self.tip, ticket["id"])
        output = io.StringIO()
        with redirect_stdout(output):
            result = style_plan.gate(self.repo, ticket["id"])
        self.assertEqual(result, 0, output.getvalue())
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        self.assertEqual(ledger.ticket_merge(ticket["id"]), ())
        self.assertEqual(ledger.ticket_get(ticket["id"])["state"], "merged")

    def refill(self, unit: str, plan: Path) -> style_ledger.TicketRecord:
        """Assign one ticket through the planner CLI."""
        output = io.StringIO()
        with redirect_stdout(output):
            result = style_plan.main([
                "next-phase", "--repo", str(self.repo), "--unit", unit, "--plan", str(plan),
            ])
        self.assertEqual(result, 0, output.getvalue())
        ticket, = self.ledger().ticket_open(ticket_owner_id(unit))
        return ticket

    def test_combined_unit_completes_ranked_refills_and_waits_at_capacity_one(self) -> None:
        self.style_units((StyleUnitAssignment("names", FAMILIES),))
        definitions: dict[rules.Family, str] = {family: self.rule(f"integration-{family}",
                       f"tier: {'D' if family == 'modules' else 'C'}\nfamily: {family}\ninput_scope: file\n")
                       for family in FAMILIES}
        pending: dict[rules.Family, tuple[str, ...]] = {
            "modules": (self.sources[0], self.sources[2], self.sources[3]),
            "types": (self.sources[2],), "values": (self.sources[3],), "names": (self.sources[0],),
        }
        self.sync()
        for family, rule in definitions.items():
            for path in self.sources:
                if path not in pending[family]:
                    self.record(rule, path, self.tip)

        completed: list[str] = []
        expected: tuple[tuple[rules.Family, str], ...] = (
            ("modules", "crates/beta"), ("modules", "crates/alpha"),
            ("types", "crates/beta"), ("values", "crates/beta"), ("names", "crates/alpha"),
        )
        for family, crate in expected:
            with self.subTest(family=family, crate=crate):
                ticket = self.refill("names", self.plan)
                self.assertEqual((ticket["family"], ticket["crate"], ticket["rules"]),
                                 (family, crate, [definitions[family]]))
                before = self.plan.read_bytes()
                self.assertEqual(self.run_next(), (0, f"resume {ticket['id']}\n", ""))
                self.assertEqual(self.plan.read_bytes(), before)
                self.assertEqual(len(self.ledger().ticket_open(ticket_owner_id("names"))), 1)
                self.finish_ticket(ticket)
                completed.append(ticket["id"])
        self.assertEqual(len(set(completed)), 5)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names")), ())

    def test_four_units_complete_only_their_owned_family_without_borrowing(self) -> None:
        plans = {"names": self.plan, "types": self.types_unit(), "modules": self.modules_unit()}
        worktree = self.root / "values"
        _ = self.git("worktree", "add", "-qb", "demo-style-values", str(worktree))
        values_plan = worktree / "docs/style-production/units/values-unit.md"
        values_plan.parent.mkdir(parents=True)
        _ = values_plan.write_text(self.plan_header())
        self.unit_plan_config(worktree, "values-unit.md")
        _ = fixture_unit_location(recorded_showrunner("sample-style", self.doc), "values", worktree)
        plans["values"] = values_plan
        definitions: dict[rules.Family, str] = {family: self.rule(f"owned-{family}",
                       f"tier: {'D' if family == 'modules' else 'C'}\nfamily: {family}\ninput_scope: file\n")
                       for family in FAMILIES}
        self.sync()
        for index, family in enumerate(FAMILIES):
            for path in self.sources:
                if path != self.sources[index]:
                    self.record(definitions[family], path, self.tip)
        for family in ("modules", "types", "values", "names"):
            with self.subTest(family=family):
                ticket = self.refill(family, plans[family])
                self.assertEqual((ticket["family"], ticket["rules"]), (family, [definitions[family]]))
                self.finish_ticket(ticket)
                with redirect_stdout(io.StringIO()):
                    result = style_plan.next_phase(family, plans[family], self.repo)
                self.assertIsInstance(result, style_plan.WaitingForSync)
                self.assertEqual(self.ledger().ticket_open(ticket_owner_id(family)), ())

    def parallel_owned_tickets(self) -> tuple[style_ledger.Ledger, tuple[str, str]]:
        """Assign two owned families to separate files and phase sections."""
        self.style_units((StyleUnitAssignment("names", FAMILIES, 2),))
        names = self.rule("parallel-names")
        types = self.rule("parallel-types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        for rule, selected in ((names, self.sources[0]), (types, self.sources[2])):
            for path in self.sources:
                if path != selected:
                    self.record(rule, path, self.tip)
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "read", "types", "crates/beta", [self.sources[2]], [types])
        ledger.ticket_add("2", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[0]], [names])
        self.assertEqual(self.run_next(), (0, "phase written: 1\nphase written: 2\n", ""))
        return ledger, ("1", "2")

    def test_capacity_two_assigns_two_owned_families_with_separate_phases(self) -> None:
        ledger, tickets = self.parallel_owned_tickets()
        opened = ledger.ticket_open(ticket_owner_id("names"))
        self.assertEqual(tuple(ticket["id"] for ticket in opened), tickets)
        self.assertEqual({ticket["state"] for ticket in opened}, {"assigned"})
        self.assertEqual({ticket["family"] for ticket in opened}, {"names", "types"})
        self.assertTrue(set(opened[0]["files"]).isdisjoint(opened[1]["files"]))
        self.assertEqual(tuple(match.group("id") for match in style_plan.PHASE.finditer(self.plan.read_text())), tickets)
        self.assertEqual(self.run_next(), (0, "resume 1\nresume 2\n", ""))

    def test_capacity_two_overlap_stops_the_real_gate_and_merge(self) -> None:
        ledger, tickets = self.parallel_owned_tickets()
        for ticket_id in tickets:
            ticket = ledger.ticket_get(ticket_id)
            for rule in ticket["rules"]:
                for path in ticket["files"]:
                    self.record(rule, path, self.tip, ticket_id)
        ledger.ticket_checkpoint("1", self.tip)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("INSERT INTO ticket_widened_paths VALUES (?,?,?,?,?)",
                                   ("2", self.sources[2], "Stored overlap", "fixture", "now"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.gate(self.repo, "2"), 1)
        self.assertIn("overlap: " + self.sources[2], output.getvalue())
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[2]):
            _ = ledger.ticket_merge("1")
        self.assertEqual((ledger.ticket_get("1")["state"], ledger.ticket_get("2")["state"]),
                         ("checkpointed", "assigned"))

    def test_one_and_four_units_preserve_the_same_rule_exception(self) -> None:
        rule = self.rule("exception")
        definition = self.rule_dir / "exception.md"
        _ = definition.write_text(definition.read_text() + "Permit the name value in a fixture function.\n")
        self.sync()
        for path in self.sources[1:]:
            self.record(rule, path, self.tip)
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ledger.record(rule, self.sources[0], self.tip, blob, "exception",
                      detail="The rule permits this fixture function.", ticket="outside", worker="fixture")
        partitions = ((StyleUnitAssignment("names", FAMILIES),), DEFAULT_STYLE_ASSIGNMENTS)
        for assignments in partitions:
            with self.subTest(assignments=assignments):
                self.style_units(assignments)
                self.assertEqual(next(cell.state for cell in ledger.cells(rule) if cell.path == self.sources[0]), "current")
                with redirect_stdout(io.StringIO()):
                    self.assertIsInstance(style_plan.next_phase("names", self.plan, self.repo), style_plan.WaitingForSync)
                self.assertEqual(ledger.ticket_open(ticket_owner_id("names")), ())
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.commit_to_every_checkout()
        for assignments in partitions:
            with self.subTest(changed_assignments=assignments):
                self.style_units(assignments)
                self.assertEqual(next(cell.state for cell in ledger.cells(rule) if cell.path == self.sources[0]), "stale")
                ticket = self.refill("names", self.plan)
                self.assertEqual((ticket["family"], ticket["rules"], ticket["files"]), ("names", [rule], [self.sources[0]]))
                ledger.ticket_drop(ticket["id"])

    def test_missing_family_owner_refuses_refill_without_ticket_or_plan_changes(self) -> None:
        _ = self.rule("read")
        self.sync()
        write_style_units(self.doc, tuple(row for row in DEFAULT_STYLE_ASSIGNMENTS if row.unit != "modules"))
        plan = self.plan.read_bytes()
        before = self.count_tickets()
        code, output, errors = self.run_next()
        self.assertEqual(code, 2, output + errors)
        self.assertIn("modules: no style owner", errors)
        self.assertEqual(self.plan.read_bytes(), plan)
        self.assertEqual(self.count_tickets(), before)


if __name__ == "__main__":
    _ = unittest.main()
