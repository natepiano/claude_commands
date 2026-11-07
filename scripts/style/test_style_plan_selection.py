"""Held judgments, proposal state and synchronized branch ancestry."""

from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname

from scripts.style.ledger_test_support import ticket_owner_id

import errno
import io
import re
import sqlite3
import subprocess
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import final
from unittest import mock


from scripts.style import style_plan
from scripts.style import style_proposals

from scripts.lib import berth_board
from scripts.style import rules
from scripts.style import style_ledger
from scripts.style import unit_files
from scripts.shutdown import remote
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_unit_location, recorded_showrunner

from scripts.style.style_plan_test_support import StylePlanFixture, unit_staff_id
from scripts.style.unit_plans import AllCrates


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())


@final
class StylePlanTests(StylePlanFixture):
    def test_refill_uses_its_production_when_another_unit_has_the_same_name(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.finish_judging()
        record = staff.initial_snapshot()
        parent = record.by_doc(remote.local_machine(), self.doc)
        assert parent is not None
        own = next(row for row in record.find(kind="unit", parent=parent.id) if row.name == "names")
        other = fixture_member("showrunner", name="other-style")
        duplicate = fixture_member("unit", name="names", parent=other.id)
        code, output, errors = self.run_next()
        self.assertEqual(code, 0, output + errors)
        tickets = self.ledger().ticket_open(str(own.id))
        self.assertTrue(tickets)
        self.assertEqual({ticket["unit"] for ticket in tickets}, {str(own.id)})
        self.assertEqual(self.ledger().ticket_open(str(duplicate.id)), ())

    def values_unit(self) -> Path:
        worktree = self.root / "values"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-values", str(worktree))
        plan = worktree / "docs/style-production/units/values-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header().replace("names", "values"))
        self.unit_plan_config(worktree, "values-unit.md")
        _ = fixture_unit_location(recorded_showrunner("sample-style", self.doc), "values", worktree)
        return plan

    def select_for_family(self, family: rules.Family, plan: Path) -> style_ledger.TicketRecord:
        self.finish_judging()
        output = io.StringIO()
        with mock.patch.object(style_proposals, "proposal_command"), redirect_stdout(output):
            code = style_plan.main(["next-phase", "--unit", family, "--plan", str(plan),
                                    "--repo", str(self.repo)])
        self.assertEqual(code, 0, output.getvalue())
        ticket, = self.ledger().ticket_open(ticket_owner_id(family))
        return ticket

    def approved_findings_blocking_structure(self) -> tuple[Path, Path, str]:
        """Approved modules work at the only pending structure file, blocking a names read there."""
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("read")
        self.sync()
        issue = self.borrowing_decision(structure, "modules", self.sources[0], count=2)
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)
        return plan, issue, structure

    def test_refill_builds_approved_findings_before_structure_on_the_same_file(self) -> None:
        plan, issue, _ = self.approved_findings_blocking_structure()
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]), ("proposal", [self.sources[0]]))
        self.assertEqual(set(ticket["finding_ids"]),
                         {finding.finding_id for finding in self.ledger().proposal_findings(issue)})

    def test_refill_builds_followed_approved_findings_before_structure_at_their_new_file(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("read")
        old, current = self.sources[0], "crates/alpha/src/current.rs"
        self.commit_files({old: "pub struct Parent {\n    old: u32,\n}\n"})
        plan = self.modules_unit()
        issue, (finding,) = self.judged_findings(structure, ((old, 2, "old: u32,"),))
        _ = issue.write_text(issue.read_text().replace(f"unit: {unit_staff_id('names')}", f"unit: {unit_staff_id('modules')}"))
        self.write_generated_body(issue)
        self.finish_judging()
        _ = self.git("mv", old, current)
        self.commit_to_every_checkout()
        _ = self.git("merge", "--ff-only", "main", cwd=plan.parents[3])
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)

        # Following refreshes the generated decision, including its rule link. Use the same
        # rule-origin fixture as the declaration-follow tests so refresh is not skipped.
        with mock.patch.object(style_proposals, "github_rule_root",
                               return_value=style_proposals.GitHubRuleRoot("example/nate_style", "main")):
            ticket = self.select_for_family("modules", plan)

        followed, = self.ledger().proposal_findings(issue)
        self.assertEqual((followed.finding_id, followed.path), (finding.finding_id, current))
        self.assertEqual((ticket["kind"], ticket["files"], ticket["finding_ids"]),
                         ("proposal", [current], [finding.finding_id]))

    def test_refill_selects_structure_when_its_file_no_longer_has_approved_findings(self) -> None:
        plan, issue, _ = self.approved_findings_blocking_structure()
        first = self.select_for_family("modules", plan)
        self.assertEqual(first["kind"], "proposal")
        self.ledger().ticket_drop(first["id"])
        _ = issue.write_text(re.sub(r"unit: [^\n]*\n", "", issue.read_text()))
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]), ("structure", [self.sources[0]]))
        self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                            for finding in self.ledger().proposal_findings(issue)))

    def test_refill_selects_structure_after_the_approved_findings_ticket_merges(self) -> None:
        plan, _, _ = self.approved_findings_blocking_structure()
        first = self.select_for_family("modules", plan)
        self.assertEqual(first["kind"], "proposal")
        ledger = self.ledger()
        ledger.ticket_checkpoint(first["id"], self.tip)
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources[1:])):
            with mock.patch.object(style_proposals, "proposal_command"):
                waiting = style_plan.next_phase("modules", plan, self.repo)
        self.assertIsInstance(waiting, style_plan.WaitingForMerge)
        _ = ledger.ticket_merge(first["id"])
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]), ("structure", [self.sources[0]]))

    def test_refill_resumes_the_same_approved_ticket_without_creating_structure_work(self) -> None:
        plan, _, _ = self.approved_findings_blocking_structure()
        first = self.select_for_family("modules", plan)
        self.assertEqual(first["kind"], "proposal")
        before = plan.read_bytes(), self.count_tickets(), first
        second = self.select_for_family("modules", plan)
        self.assertEqual((plan.read_bytes(), self.count_tickets(), second), before)

    def test_queued_structure_yields_once_to_approved_findings_on_its_file(self) -> None:
        plan, _, structure = self.approved_findings_blocking_structure()
        ledger = self.ledger()
        ledger.ticket_add("3001", ticket_owner_id("modules"), "structure", "modules", "crates/alpha",
                          [self.sources[0]], [structure])
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]), ("proposal", [self.sources[0]]))
        self.assertEqual(self.count_tickets(), 3)  # The judging ticket, one dropped read, and one build.
        self.assertEqual(ledger.queued_tickets(ticket_owner_id("modules")), ())
        before = self.count_tickets()
        self.assertEqual(self.select_for_family("modules", plan), ticket)
        self.assertEqual(self.count_tickets(), before)

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

    def test_modules_reads_blocking_structure_despite_unsynced_source_changes(self) -> None:
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

        with (mock.patch.object(style_plan, "unsynced_changes", return_value={
                self.sources[0]: style_plan.UnsyncedChange(self.sources[0], self.tip)}),
              mock.patch.object(style_plan, "main_changes", return_value=style_plan.MainChanges(
                  self.tip, frozenset({self.sources[0]})))):
            ticket = self.select_for_family("modules", plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                         ("structure", "modules", [self.sources[0]]))
        self.assertEqual(ticket["finding_ids"], [])
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_modules_skips_held_structure_files_and_manifest(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        _ = self.values_unit()
        _ = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("12001", ticket_owner_id("values"), "read", "modules", "crates/alpha",
                          [self.sources[0]], [structure])
        ledger.ticket_assign("12001", "style/main")
        branch = self.git("rev-parse", "demo-style-modules")
        # Board reservations and the other ticket's held paths both exclude source reads.
        board = self.board("crates/alpha/Cargo.toml", *self.sources[:2])
        busy = style_plan.busy_files(self.repo, self.doc, ledger, ticket_owner_id("modules"), board)
        self.assertIn(self.sources[0], busy.paths)
        self.assertTrue(busy.contains("crates/alpha/Cargo.toml"))
        with mock.patch.object(berth_board, "read_board", return_value=board):
            ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]),
                         ("structure", ["crates/beta/Cargo.toml", *self.sources[2:]]))
        self.assertEqual(self.git("rev-parse", "demo-style-modules"), branch)
        self.assertEqual(ledger.ticket_open(ticket_owner_id("values"))[0]["state"], "assigned")

    def test_modules_reads_blocking_structure_before_scanning_decision_footprints(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.values_unit()
        _ = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        _ = self.borrowing_decision(structure, "modules", self.sources[2])
        with mock.patch.object(style_plan.EditedFiles, "of", side_effect=AssertionError("decision footprint read")):
            ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]), ("structure", list(self.sources[:2])))

    def test_borrowed_structure_skips_product_reservations_and_refill_is_idempotent(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("read")
        self.sync()
        held, available = self.sources[:2]
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        # Own modules and borrowed modules use the same busy set before slicing.
        candidates = self.candidates("modules", blocked=(held,))
        self.assertEqual({path for ticket in candidates for path in ticket.files}, set(self.sources[1:]))
        ledger = self.ledger()
        # An old queued structure ticket must pass the same availability check.
        ledger.ticket_add("1", ticket_owner_id("names"), "structure", "modules", "crates/alpha", [held], [structure])
        with mock.patch.object(berth_board, "read_board", return_value=self.board(held)):
            code, output, errors = self.run_next()
            self.assertEqual((code, errors), (0, ""), output)
            ticket, = ledger.ticket_open(ticket_owner_id("names"))
            self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                             ("structure", "modules", [available]))
            self.assertIn("dropped 1: files busy", output)
            before = self.plan.read_bytes(), ledger.ticket_open(ticket_owner_id("names")), self.count_tickets()
            code, output, errors = self.run_next()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertEqual((self.plan.read_bytes(), ledger.ticket_open(ticket_owner_id("names")), self.count_tickets()), before)

    def test_structure_selection_omits_a_deleted_file_from_an_older_cell_snapshot(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        ledger = self.ledger()
        cells = tuple(ledger.cells(structure))
        deleted = self.sources[0]
        _ = self.git("rm", deleted)
        tip = self.commit()
        ledger.set_tip(tip, "style/main")
        # The old snapshot still names the deleted file; eligibility and size use the current tip.
        with mock.patch.object(ledger, "cells", return_value=cells):
            tickets = style_plan.candidate_tickets(self.repo, ledger, style_plan.BusyFiles(self.board(), frozenset()),
                                                   "modules", base=style_plan.NoUnitBranch())
        self.assertEqual({path for ticket in tickets for path in ticket.files}, set(self.sources[1:]))

    def test_refill_checks_current_file_presence_even_after_a_declaration_read(self) -> None:
        plan = self.modules_unit()
        read = self.rule("read", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(read, "modules", self.sources[0])
        self.finish_judging()
        finding, = self.ledger().proposal_findings(issue)
        _ = self.git("rm", finding.path)
        self.commit_to_every_checkout()
        # A prior declaration reading cannot make a file missing at today's tip buildable.
        follow = style_proposals.FollowRun(style_ledger.DeclarationReading(
            self.tip, (style_ledger.DeclarationInPlace(finding),)), (), ())
        with mock.patch.object(style_proposals, "run_follow", return_value=follow):
            ticket = self.select_for_family("modules", plan)
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].state, style_ledger.Waived)
        self.assertNotIn(finding.path, ticket["files"])

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
        product = self.root / "product"
        _ = self.git("worktree", "add", "-q", "-b", "product-change", str(product))
        _ = self.git("mv", self.sources[1], "crates/alpha/src/renamed.rs", cwd=product)
        _ = self.git("add", "--all", cwd=product)
        _ = self.git("commit", "-qm", "Product", cwd=product)
        _ = (product / self.sources[2]).write_text("dirty\n")
        _ = fixture_unit_location(recorded_showrunner("sample", self.repo / "docs/product-production.md"), "product", product)
        _ = self.modules_unit()
        ledger = self.ledger()
        ledger.ticket_add("3001", ticket_owner_id("modules"), "read", "names", "crates/beta", [self.sources[3]], [rule])
        ledger.ticket_assign("3001", "demo-style-modules")
        self.assertEqual(self.candidates(blocked=(self.sources[0],)), ())
        ledger.ticket_drop("3001")
        self.assertEqual(tuple(path for ticket in self.candidates(blocked=(self.sources[0],)) for path in ticket.files), (self.sources[3],))

    def test_held_files_follow_staff_when_the_worktree_cell_names_another_checkout(self) -> None:
        stale = self.root / "stale-names"
        _ = self.git("worktree", "add", "-q", "-b", "stale-names", str(stale))
        staff_file, stale_file = self.sources[:2]
        _ = (self.unit / staff_file).write_text("staff location change\n")
        _ = (stale / stale_file).write_text("stale cell change\n")
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.unit), str(stale)))

        holders = unit_files.unit_file_holders(self.repo, self.doc, self.ledger())

        self.assertEqual(holders["names"], frozenset({staff_file}))

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
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["files"], [self.sources[0]])

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
        self.commit_files({self.sources[0]: (self.repo / self.sources[0]).read_text() + "\npub struct JudgedAgain;\n"})
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
