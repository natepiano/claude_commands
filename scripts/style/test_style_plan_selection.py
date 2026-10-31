"""Held judgments, proposal state and synchronized branch ancestry."""

from __future__ import annotations

import errno
import io
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import style_proposals

import berth_board
import rules
import style_ledger

from style_plan_test_support import StylePlanFixture
from unit_plans import AllCrates


@final
class StylePlanTests(StylePlanFixture):
    def values_unit(self) -> Path:
        worktree = self.root / "values"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-values", str(worktree))
        plan = worktree / "docs/style-production/units/values-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header().replace("names-unit", "values-unit"))
        _ = self.doc.write_text(self.doc.read_text() +
                                f"| values-unit | docs/style-production/units/values-unit.md | {worktree} | demo-style-values |\n")
        return plan

    def select_for_family(self, family: rules.Family, plan: Path) -> style_ledger.TicketRecord:
        self.finish_judging()
        output = io.StringIO()
        with mock.patch.object(style_proposals, "proposal_command"), redirect_stdout(output):
            code = style_plan.main(["next-phase", "--unit", family + "-unit", "--plan", str(plan),
                                    "--repo", str(self.repo)])
        self.assertEqual(code, 0, output.getvalue())
        ticket, = self.ledger().ticket_open(family + "-unit")
        return ticket

    def test_modules_reads_blocking_structure_before_approved_decisions(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.values_unit()
        _ = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(structure, "modules", self.sources[2])

        ticket = self.select_for_family("modules", plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                         ("structure", "modules", list(self.sources[:2])))
        self.assertEqual(ticket["finding_ids"], [])
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_modules_takes_approved_decision_when_unsynced_file_holds_blocking_structure_read(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.values_unit()
        values = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(structure, "modules", self.sources[2])
        for path in (self.sources[1], self.sources[3]):
            self.record(structure, path, self.tip)
        for path in self.sources[1:]:
            self.record(values, path, self.tip)

        with mock.patch.object(style_plan, "unsynced_changes", return_value={
                self.sources[0]: style_plan.UnsyncedChange(self.sources[0], self.tip)}):
            ticket = self.select_for_family("modules", plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                         ("proposal", "modules", [self.sources[2]]))
        self.assertEqual(ticket["finding_ids"], [self.ledger().proposal_findings(issue)[0].finding_id])

    def test_modules_keeps_approved_decisions_first_without_blocked_cells(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(structure, "modules", self.sources[2])

        ticket = self.select_for_family("modules", plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                         ("proposal", "modules", [self.sources[2]]))
        self.assertEqual(ticket["finding_ids"], [self.ledger().proposal_findings(issue)[0].finding_id])

    def test_values_takes_its_blocking_structure_before_borrowing_a_proposal(self) -> None:
        plan = self.values_unit()
        values = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        names = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(names, path, self.tip)
        issue = self.borrowing_decision(names, "names", self.sources[2])

        ticket = self.select_for_family("values", plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["rules"], ticket["files"]),
                         ("structure", "modules", [structure], list(self.sources[:2])))
        self.assertNotIn(values, ticket["rules"])
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)
        self.assertIn("Borrowed from modules: values has no buildable work (waiting for structure).", plan.read_text())

    def test_refill_blob_failures_do_not_reissue_held_cells(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        with self.ledger() as ledger:
            cell = style_ledger.Cell(read, self.sources[0], "crates/alpha", "C", "names", "stale")
            definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
            # Exercise the fallback before any later ticket-selection Git reads can fail.
            for code in (errno.EMFILE, errno.ENOENT):
                with self.subTest(errno=code):
                    failure = OSError(code, "PRIVATE launch failure")
                    with (mock.patch.object(style_plan, "_refill_cells", wraps=style_plan._refill_cells) as refill,  # pyright: ignore[reportPrivateUsage]
                          mock.patch.object(subprocess, "run", side_effect=failure)):
                        with self.assertRaises(style_ledger.GitReadFailed) as refused:
                            _ = style_plan._refill_cells(ledger, (cell,), style_plan.NoUnitBranch(),  # pyright: ignore[reportPrivateUsage]
                                                         "names", AllCrates(), {}, definitions)
                    refill.assert_called_once()
                    self.assertIs(refused.exception.__cause__, failure)
            # An actually removed file cannot prove that this held cell is unchanged.
            _ = self.git("rm", self.sources[0])
            removed_tip = self.commit()
            ledger.set_tip(removed_tip, "style/main")
            result = style_plan._refill_cells(ledger, (cell,), style_plan.NoUnitBranch(),  # pyright: ignore[reportPrivateUsage]
                                              "names", AllCrates(), {}, definitions)
            self.assertEqual(result.cells, (cell,))
            self.assertEqual(result.judged_at_base, frozenset())

    def test_reserved_unmerged_dirty_and_other_open_ticket_files_are_excluded(self) -> None:
        rule = self.rule("read")
        self.sync()
        product = self.root / "product-unit"
        _ = self.git("worktree", "add", "-q", "-b", "product-change", str(product))
        _ = self.git("mv", self.sources[1], "crates/alpha/src/renamed.rs", cwd=product)
        _ = self.git("add", "--all", cwd=product)
        _ = self.git("commit", "-qm", "Product", cwd=product)
        _ = (product / self.sources[2]).write_text("dirty\n")
        parent = self.repo / "docs/product-production.md"
        _ = parent.write_text(parent.read_text() + f"| product-unit | docs/product.md | {product} | product-change |\n")
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-1", "modules-unit", "read", "names", "crates/beta", [self.sources[3]], [rule])
        ledger.ticket_assign("modules-unit-1", "demo-style-modules")
        self.assertEqual(self.candidates(blocked=(self.sources[0],)), ())
        ledger.ticket_drop("modules-unit-1")
        self.assertEqual(tuple(path for ticket in self.candidates(blocked=(self.sources[0],)) for path in ticket.files), (self.sources[3],))

    def test_slices_preserve_directories_and_split_oversized_directories_with_bounds(self) -> None:
        files: list[str] = []
        for directory, count in (("one", 12), ("two", 12), ("large", 30)):
            for index in range(count):
                path = f"crates/alpha/src/{directory}/file{index:02}.rs"
                self.write(path, "// source\n" * 200)
                files.append(path)
        tip = self.commit()
        slices = style_plan.read_slices(self.repo, tip, tuple(reversed(files)))
        self.assertEqual(slices, style_plan.read_slices(self.repo, tip, tuple(files)))
        self.assertEqual({path for group in slices for path in group}, set(files))
        for group in slices:
            self.assertGreaterEqual(len(group), 10)
            self.assertLessEqual(len(group), 25)
            self.assertLessEqual(len(group) * 200, 5000)
        for directory in ("one", "two"):
            self.assertEqual(sum(any(f"/{directory}/" in path for path in group) for group in slices), 1)

    def test_refill_skips_unchanged_held_read_cells_under_open_proposals_after_other_crate_edits(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(next(cell.state for cell in self.ledger().cells(read) if cell.path == self.sources[0]), "stale")
        self.assertEqual(self.candidates(), ())
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_open_proposal_structure_cells_neither_reissue_nor_block_other_families_when_the_file_is_unchanged(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        read = self.rule("read")
        self.sync()
        _ = self.held_proposal(structure, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)
            self.record(read, path, self.tip)
        self.assertEqual(next(cell.state for cell in self.ledger().cells(structure) if cell.path == self.sources[0]), "stale")
        self.assertEqual(self.candidates("modules"), ())
        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))
        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["files"], [self.sources[0]])

    def test_refill_reissues_a_held_cell_under_an_open_proposal_when_its_own_file_changes(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_reissues_an_unchanged_held_cell_as_soon_as_its_proposal_is_not_open(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        issue = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.candidates(), ())

        for status in ("closed", "approved", "declined"):
            with self.subTest(status=status):
                _ = issue.write_text(f"---\nstatus: {status}\n---\n")
                self.assertEqual(self.candidates(), (
                    style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
                ))

    def test_refill_still_issues_stale_cells_that_were_never_held(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        self.record(read, self.sources[0], self.tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_requires_every_finding_on_a_held_cell_to_have_an_open_proposal(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.candidates(), ())

        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == read)
        finding = ledger.hold(read, self.sources[0], 1, "another defect", "rename", "before", "after", blob, "outside")
        expected = (style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),)
        self.assertEqual(self.candidates(), expected)
        second = self.root / "second-proposal.md"
        _ = second.write_text("---\nstatus: open\n---\n")
        ledger.file_held(finding.finding_id, second)
        self.assertEqual(self.candidates(), ())
        _ = second.write_text("---\nstatus: closed\n---\n")
        self.assertEqual(self.candidates(), expected)

    def test_refill_uses_the_latest_verdict_instead_of_an_older_held_judgment(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.record(read, self.sources[0], self.tip, ticket="later-clean")
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_held_cell_skip_ignores_waived_findings_beside_filed_ones(self) -> None:
        read, expected = self.held_cell_with_final_findings()
        self.hold_more(read, "waived")
        self.assertEqual(self.candidates(), ())
        self.hold_more(read, "unfiled")
        self.assertEqual(self.candidates(), expected)

    def test_held_cell_with_only_final_findings_is_not_skipped(self) -> None:
        read, expected = self.held_cell_with_final_findings()
        self.hold_more(read, "waived", "built", "withdrawn")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE state='filed'")
            connection.commit()
        self.assertEqual({type(finding.state) for finding in self.ledger().held_findings(style_ledger.AllHeldFindings())},
                         {style_ledger.Waived, style_ledger.Built, style_ledger.Withdrawn})
        self.assertEqual(self.candidates(), expected)

if __name__ == "__main__":
    _ = unittest.main()
