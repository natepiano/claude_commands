"""Structure-first selection, pilot scope and blocked-cell priority."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import unit_plans

import berth_board
import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_structure_precedes_family_reads_and_retains_partial_rule_sites(self) -> None:
        read = self.rule("read")
        structure = self.rule("structure", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: Read the crate layout.\n")
        self.sync()
        self.assertEqual(self.candidates(), ())
        tickets = self.candidates("modules")
        self.assertEqual(len(tickets), 2)
        self.assertEqual(tickets[0].kind, "structure")
        self.assertEqual(tickets[0].rules, (structure,))
        self.assertTrue(tickets[0].sites)
        preview = io.StringIO()
        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            with redirect_stdout(preview):
                self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo)]), 0)
        self.assertTrue(preview.getvalue().startswith("modules-unit: structure"))
        self.assertNotIn(self.sources[0], preview.getvalue())
        self.assertIn(self.sources[1], preview.getvalue())
        ledger = self.ledger()
        for path in self.sources:
            self.record(structure, path, self.tip)
        reads = self.candidates()
        self.assertTrue(reads)
        self.assertEqual({rule for ticket in reads for rule in ticket.rules}, {read})
        self.assertFalse(any(cell.rule.endswith("#uncovered") for cell in ledger.cells(rule=structure)))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo)]), 0)
        self.assertIn("names-unit: read", output.getvalue())

    def test_names_read_refill_takes_only_files_without_pending_structure_cells(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read = self.rule("read")
        self.sync()
        self.record(structure, self.sources[1], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["rules"], ticket["files"]),
                         ("read", [read], [self.sources[1]]))
        self.assertIn(f"- `{self.sources[1]}` — lines 1-1\n", self.plan.read_text())
        self.assertNotIn(f"- `{self.sources[0]}`", self.plan.read_text())

    def test_names_listed_refill_packs_only_sites_on_files_without_pending_structure_cells(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        listed = self.rule("listed", "tier: B\nfamily: names\npre_filter: value\n")
        self.sync()
        self.record(structure, self.sources[1], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["rules"], ticket["files"]),
                         ("listed", [listed], [self.sources[1]]))
        self.assertEqual([(site["path"], site["line"]) for site in ticket["sites"]], [(self.sources[1], 1)])

    def test_names_file_whose_structure_cells_are_judged_refills_while_its_crate_and_other_files_wait(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        read = self.rule("read")
        self.sync()
        self.record(structure, self.sources[0], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        pending = {(cell.rule, cell.path) for cell in self.ledger().cells(crate="crates/alpha") if cell.state == "open"}
        self.assertLessEqual({(manifest, "crates/alpha"), (structure, self.sources[1])}, pending)

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["rules"], ticket["files"]), ("read", [read], [self.sources[0]]))

    def test_names_reads_blocking_structure_when_its_only_unblocked_file_is_busy(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        _ = self.rule("read")
        self.sync()
        self.record(structure, self.sources[0], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket, = self.ledger().ticket_open("names-unit")
        self.assertEqual((ticket["kind"], ticket["files"]),
                         ("structure", ["crates/alpha/Cargo.toml", self.sources[1]]))

    def test_a_crate_cell_waits_for_every_structure_cell_in_its_crate_and_a_file_cell_for_its_own(self) -> None:
        def cell(path: str, tier: rules.Tier, crate: str = "crates/alpha") -> style_ledger.Cell:
            return style_ledger.Cell("rule", path, crate, tier, "names" if tier == "C" else "modules", "open")

        crate_cell, file_cell = cell("crates/alpha", "C"), cell(self.sources[0], "C")
        other_file, own_file, crate_structure = cell(self.sources[1], "D"), cell(self.sources[0], "D"), cell("crates/alpha", "D")
        other_crate = cell(self.sources[2], "D", "crates/beta")
        self.assertTrue(style_plan.blocked_by_structure(crate_cell, (other_file,)))
        self.assertFalse(style_plan.blocked_by_structure(crate_cell, (other_crate,)))
        self.assertTrue(style_plan.blocked_by_structure(file_cell, (other_file, own_file)))
        self.assertFalse(style_plan.blocked_by_structure(file_cell, (other_file, crate_structure, other_crate)))

    def test_names_reads_structure_when_every_pending_names_file_is_closed(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read = self.rule("read")
        self.sync()
        self.record(structure, self.sources[1], self.tip)
        self.record(read, self.sources[1], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket, = self.ledger().ticket_open("names-unit")
        self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                         ("structure", "modules", [self.sources[0]]))
        self.assertEqual(self.sync_wait_rows(), [])

    def test_names_reads_structure_when_its_unblocked_names_file_is_busy(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("read")
        self.sync()
        self.record(structure, self.sources[1], self.tip)
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))

        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[1])):
            self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket, = self.ledger().ticket_open("names-unit")
        self.assertEqual((ticket["kind"], ticket["files"]), ("structure", [self.sources[0]]))

    def test_modules_still_selects_only_pending_structure_files_when_names_can_refill(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("read")
        self.sync()
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)
        before = self.candidates("modules")
        self.assertEqual([(ticket.kind, ticket.crate, ticket.files, ticket.cells) for ticket in before], [
            ("structure", "crates/alpha", (self.sources[0],), ((structure, self.sources[0]),)),
        ])

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.candidates("modules"), before)

    def test_structure_first_unblocks_more_other_family_cells_before_staler_crates(self) -> None:
        read = self.rule("read")
        own = self.rule("module-read", "tier: C\nfamily: modules\ninput_scope: file\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        self.record(read, self.sources[0], self.tip)
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.record(read, self.sources[1], self.tip)
        for path in self.sources[2:]:
            self.record(own, path, self.tip)

        tickets = self.candidates("modules")

        self.assertEqual([(ticket.crate, ticket.kind, ticket.rules) for ticket in tickets], [
            ("crates/beta", "structure", (structure,)), ("crates/alpha", "structure", (structure,)),
        ])
        ledger = self.ledger()
        scoped = style_plan.candidate_tickets(self.repo, ledger, style_plan.BusyFiles(self.board(), frozenset()),
                                             "modules", unit_plans.PilotCrates(frozenset({"crates/alpha"})),
                                             base=self.base("modules"))
        self.assertEqual([(ticket.crate, ticket.kind) for ticket in scoped], [("crates/alpha", "structure")])

    def test_structure_priority_counts_names_cells_only_inside_the_names_pilot_scope(self) -> None:
        _ = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        for name in ("extra", "more"):
            self.write(f"crates/beta/src/{name}.rs", "pub fn value() {}\n")
        self.tip = self.commit()
        self.sync()
        ledger = self.ledger()
        busy = style_plan.BusyFiles(self.board(), frozenset())
        self.assertEqual(style_plan.candidate_tickets(self.repo, ledger, busy, "modules", base=self.base("modules"))[0].crate, "crates/beta")

        tickets = style_plan.candidate_tickets(
            self.repo, ledger, busy, "modules",
            family_scopes={"modules": unit_plans.AllCrates(),
                           "names": unit_plans.PilotCrates(frozenset({"crates/alpha"}))},
            base=self.base("modules"))

        self.assertEqual([ticket.crate for ticket in tickets], ["crates/alpha", "crates/beta"])
        self.assertEqual({path for ticket in tickets for path in ticket.files},
                         set(self.sources) | {"crates/beta/src/extra.rs", "crates/beta/src/more.rs"})

    def test_structure_priority_ignores_pending_cells_of_a_family_without_a_unit_row(self) -> None:
        names = self.rule("read")
        types = (self.rule("types-one", "tier: C\nfamily: types\ninput_scope: file\n"),
                 self.rule("types-two", "tier: C\nfamily: types\ninput_scope: file\n"))
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        for rule in types:
            for path in self.sources[:2]:
                self.record(rule, path, self.tip)
        for path in self.sources[2:]:
            self.record(names, path, self.tip)
        ledger = self.ledger()
        busy = style_plan.BusyFiles(self.board(), frozenset())
        self.assertEqual(style_plan.candidate_tickets(self.repo, ledger, busy, "modules", base=self.base("modules"))[0].crate, "crates/beta")

        tickets = style_plan.candidate_tickets(
            self.repo, ledger, busy, "modules", family_scopes=unit_plans.family_pilot_scopes(self.doc, self.repo),
            base=self.base("modules"))

        self.assertEqual([ticket.crate for ticket in tickets], ["crates/alpha", "crates/beta"])

    def test_family_pilot_scopes_reads_unit_copies_and_missing_plans_allow_all_crates(self) -> None:
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        template = self.style / "docs/style-production/units/names-unit.md"
        _ = template.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** beta\n\n## Phases"))
        _ = self.doc.write_text(self.doc.read_text() + f"| product-unit | docs/product.md | {self.root / 'product'} | product |\n")

        self.assertEqual(unit_plans.family_pilot_scopes(self.doc, self.repo), {
            "names": unit_plans.PilotCrates(frozenset({"crates/alpha"})), "modules": unit_plans.AllCrates(),
        })
        self.plan.unlink()
        self.assertEqual(unit_plans.family_pilot_scopes(self.doc, self.repo), {
            "names": unit_plans.AllCrates(), "modules": unit_plans.AllCrates(),
        })

    def test_modules_refill_and_queue_prioritize_the_crate_blocking_a_pilot_unit(self) -> None:
        _ = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.write("crates/beta/src/extra.rs", "pub fn value() {}\n")
        self.tip = self.commit()
        self.sync()
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        worktree = self.root / "modules"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-modules", str(worktree))
        plan = worktree / "docs/style-production/units/modules-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header())
        preview = io.StringIO()
        with redirect_stdout(preview):
            self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo), "--unit", "modules-unit"]), 0)
        self.assertTrue(preview.getvalue().startswith("modules-unit: structure crates/alpha "), preview.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            code = style_plan.main(["next-phase", "--unit", "modules-unit", "--plan", str(plan), "--repo", str(worktree)])

        self.assertEqual((code, output.getvalue()), (0, "phase written: modules-unit-1\n"))
        ticket = self.ledger().ticket_open("modules-unit")[0]
        self.assertEqual((ticket["kind"], ticket["crate"], ticket["files"]),
                         ("structure", "crates/alpha", list(self.sources[:2])))

    def test_equal_blocked_structure_counts_use_staleness_then_crate_name(self) -> None:
        read = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        original_tip = self.tip
        self.record(read, self.sources[2], original_tip)
        self.write(self.sources[2], "pub fn changed_beta() {}\n")
        self.tip = self.commit()
        self.sync()
        self.assertEqual([ticket.crate for ticket in self.candidates("modules")], ["crates/beta", "crates/alpha"])

        self.record(read, self.sources[0], original_tip)
        self.write(self.sources[0], "pub fn changed_alpha() {}\n")
        self.tip = self.commit()
        self.sync()
        self.assertEqual([ticket.crate for ticket in self.candidates("modules")], ["crates/alpha", "crates/beta"])

    def test_current_other_family_cells_do_not_raise_structure_priority(self) -> None:
        read = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        for path in self.sources[:2]:
            self.record(read, path, self.tip)

        self.assertEqual([ticket.crate for ticket in self.candidates("modules")], ["crates/beta", "crates/alpha"])

    def test_other_family_keeps_stale_order_and_skips_pending_structure(self) -> None:
        # The families here share files, so no rank holds one family back for another.
        _ = self.enterContext(mock.patch.object(rules, "FAMILY_RANKS", ()))
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta", "crates/gamma"]\n')
        self.write("crates/gamma/Cargo.toml", '[package]\nname = "gamma"\nversion = "0.1.0"\n')
        gamma = "crates/gamma/src/lib.rs"
        self.write(gamma, "pub fn value() {}\n")
        self.write("docs/alpha/style/structure.md", "---\ntier: D\nfamily: modules\n---\n# Structure\n")
        self.tip = self.commit()
        read = self.rule("read")
        _ = self.rule("types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        self.record(read, gamma, self.tip)
        self.write(gamma, "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()

        tickets = self.candidates("names")

        self.assertEqual([ticket.crate for ticket in tickets], ["crates/gamma", "crates/beta"])
        self.assertEqual({ticket.kind for ticket in tickets}, {"read"})
        self.assertEqual({rule for ticket in tickets for rule in ticket.rules}, {read})

if __name__ == "__main__":
    _ = unittest.main()
