"""Judgment tips, branch ancestry and synchronization."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan

import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_refill_says_merge_the_style_branch_first_when_each_cell_left_was_judged_at_the_unit_tip(self) -> None:
        read, unit_tip = self.judged_at_unit_tip_then_style_moves()
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")
        self.seed_sync_wait()

        self.assertEqual(self.candidates(), ())
        self.assertEqual(self.run_next(),
                         (6, f"merge the style branch first: 1 cell judged at {unit_tip[:7]} wait for style/main\n", ""))
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual(self.sync_wait_rows(), [])
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")

        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)
        merged = self.git("rev-parse", "HEAD", cwd=self.unit)
        # The unit base holds the unit's own lib.rs commit, which the synced tip lacks, so the ticket waits for the sync.
        waiting = (f"waiting for sync: read crates/alpha: {self.sources[0]} changed in {unit_tip[:7]}, "
                   + f"not in the synced tip {self.tip[:7]}\n")
        self.assertEqual(self.run_next(), (3, waiting + waiting + "waiting for sync\n", ""))
        _ = self.git("merge", "-q", "--ff-only", "style/main")
        self.tip = merged
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        # The synced change to other.rs makes its crate-scope cell stale too.
        self.assertEqual((ticket["files"], ticket["base"]), (list(self.sources[:2]), merged))
        self.record(read, self.sources[0], merged, ticket="names-unit-1")
        self.assertEqual(self.cell_state(read, self.sources[0]), "current")

    def test_refill_issues_a_stale_cell_whose_inputs_at_the_unit_tip_changed_since_its_judgment(self) -> None:
        read, _ = self.judged_at_unit_tip_then_style_moves()
        _ = (self.unit / "crates/alpha/README.md").write_text("Unit notes.\n")
        _ = self.git("add", "crates/alpha/README.md", cwd=self.unit)
        _ = self.git("commit", "-qm", "Unit change in the same crate", cwd=self.unit)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_waits_for_sync_until_the_style_tip_changes_a_cell_judged_at_the_unit_tip(self) -> None:
        read = self.judged_on_all_sources_then_main_changes_crate_alpha()
        unit_tip = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.assertEqual(self.candidates(), ())

        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.unit), unit_tip)
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)

        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        self.assertEqual(self.run_next(),
                         (6, f"merge the style branch first: 2 cells judged at {unit_tip[:7]} wait for style/main\n", ""))
        self.assertEqual(self.sync_wait_rows(), [])
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.unit)
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["rules"], ticket["base"]), (list(self.sources[:2]), [read], self.tip))

    def test_refill_names_a_style_branch_missing_on_origin(self) -> None:
        _ = self.judged_at_unit_tip_then_style_moves()
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")

        code, output, errors = self.run_next()
        self.assertEqual((code, output), (2, ""))
        self.assertIn("cannot be resolved; fetch origin first", errors)
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_without_a_stored_style_branch_waits_for_sync(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip)
        for path in self.sources:
            self.record(read, path, self.tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip)
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")
        self.assertEqual(self.candidates(), ())

        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))

    def test_refill_waits_for_sync_when_the_unit_tip_is_not_in_the_style_tip(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        for path in self.sources:
            self.record(read, path, self.tip)
        _ = (self.unit / self.sources[2]).write_text("pub fn unit_only() {}\n")
        _ = self.git("commit", "-qam", "Unit change in crate beta only", cwd=self.unit)
        _ = (self.style / self.sources[1]).write_text("pub fn changed() {}\n")
        _ = self.git("commit", "-qam", "Change crate alpha", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.sync()
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")
        self.assertEqual(self.candidates(), ())

        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))

    def test_refill_issues_a_cell_whose_newest_judgment_is_not_in_the_unit_tip(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = (self.style / self.sources[2]).write_text("pub fn beta_only() {}\n")
        _ = self.git("commit", "-qam", "Change crate beta only", cwd=self.style)
        self.record(read, self.sources[0], self.git("rev-parse", "HEAD", cwd=self.style))
        _ = (self.style / self.sources[1]).write_text("pub fn changed() {}\n")
        _ = self.git("commit", "-qam", "Change crate alpha", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.sync()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_issues_a_cell_whose_newest_judgment_is_not_in_the_style_branch(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = (self.unit / self.sources[2]).write_text("pub fn beta_only() {}\n")
        _ = self.git("commit", "-qam", "Unit change in crate beta only", cwd=self.unit)
        self.record(read, self.sources[0], self.git("rev-parse", "HEAD", cwd=self.unit))
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.cell_state(read, self.sources[0]), "stale")

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_names_unit_waits_for_a_stale_structure_cell_judged_in_its_tip(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(structure, path, self.tip)
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        self.assertEqual(self.cell_state(structure, self.sources[0]), "stale")
        self.assertEqual(self.cell_state(read, self.sources[0]), "open")

        self.assertEqual(self.candidates(), ())
        code, output, errors = self.run_next()
        self.assertEqual((code, errors), (3, ""))
        self.assertTrue(output.endswith("waiting for structure: crates/alpha\n"), output)
        self.assertIn(f"structure stays with modules: {structure} {self.sources[0]}: "
                      + "judged at unit base; merge the style branch first\n", output)

    def test_modules_unit_merges_the_style_branch_first_for_a_structure_cell_judged_at_its_tip(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        plan = self.modules_unit()
        unit_tip = self.tip
        for path in self.sources:
            self.record(structure, path, self.tip)
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        _ = self.git("merge", "-q", "--ff-only", self.tip, cwd=self.style)
        self.assertEqual(self.cell_state(structure, self.sources[0]), "stale")
        self.assertEqual(self.candidates("modules"), ())

        self.assertEqual(style_plan.next_phase("modules-unit", plan, self.repo),
                         style_plan.MergeStyleBranchFirst(2, unit_tip, "refs/heads/style/main"))
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.root / "modules")
        self.assertEqual(style_plan.next_phase("modules-unit", plan, self.repo), style_plan.PhaseWritten("modules-unit-1"))
        ticket = self.ledger().ticket_open("modules-unit")[0]
        self.assertEqual((ticket["kind"], ticket["files"], ticket["base"]), ("structure", list(self.sources[:2]), self.tip))

if __name__ == "__main__":
    _ = unittest.main()
