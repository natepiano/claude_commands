"""Persisted waits, interrupted phases and queued-ticket recovery."""

from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import final
from unittest import mock


from scripts.style.ledger_test_support import ticket_owner_id

from scripts.style import style_plan
from scripts.style import style_proposals

from scripts.lib import berth_board
from scripts.style import rules
from scripts.style import style_ledger
from scripts.style.style_config import StyleUnitAssignment

from scripts.style.style_plan_test_support import StylePlanFixture, unit_staff_id
from scripts.production import staff


@final
class StylePlanTests(StylePlanFixture):
    def test_sync_wait_names_one_needed_main_path(self) -> None:
        path = "crates/alpha/src/a.rs"
        changes = style_plan.MainChanges(self.tip, frozenset((path, self.sources[2])))
        result = style_plan.ticket_sync_wait(self.ledger(), changes, "listed", "crates/alpha", "rule-x", (path,))
        reason = f"listed crates/alpha rule-x: main touched {path}"
        self.assertEqual(result, style_plan.WaitingForSync(0, reason))
        assert isinstance(result, style_plan.WaitingForSync)
        self.assertEqual(style_plan.refill_outcome(result), f"waiting for sync: {reason}")

    def test_sync_wait_names_first_three_of_five_needed_main_paths(self) -> None:
        paths = tuple(f"crates/alpha/src/{name}.rs" for name in "edcba")
        changes = style_plan.MainChanges(self.tip, frozenset((*paths, self.sources[2])))
        result = style_plan.ticket_sync_wait(self.ledger(), changes, "listed", "crates/alpha", "rule-x", paths)
        reason = "listed crates/alpha rule-x: main touched crates/alpha/src/a.rs, crates/alpha/src/b.rs, crates/alpha/src/c.rs +2"
        self.assertEqual(result, style_plan.WaitingForSync(0, reason))
        assert isinstance(result, style_plan.WaitingForSync)
        self.assertEqual(style_plan.refill_outcome(result), f"waiting for sync: {reason}")

    def test_refill_records_sync_wait_reason_in_ledger(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write(self.sources[0], "pub fn changed_on_main() {}\n")
        _ = self.commit()
        outcome = f"waiting for sync: read crates/alpha: main touched {self.sources[0]}"
        self.assertEqual(self.run_next(), (3, outcome + "\n", ""))
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            self.assertEqual(connection.execute(
                "SELECT outcome FROM refill_results WHERE unit='names'",
            ).fetchone(), (outcome,))


    def test_sync_wait_is_recorded_until_a_held_source_becomes_available(self) -> None:
        _ = self.rule("read")
        self.sync()
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources)), \
                mock.patch.object(style_ledger, "_now", return_value="2026-01-02T03:04:05+00:00"):
            self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.sync_wait_rows(), [(str(next(row.id for row in staff.initial_snapshot().find(kind="unit") if row.name == "names")), "2026-01-02T03:04:05+00:00")])
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources)), \
                mock.patch.object(style_ledger, "_now", return_value="2026-01-03T03:04:05+00:00"):
            self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.sync_wait_rows(), [(str(next(row.id for row in staff.initial_snapshot().find(kind="unit") if row.name == "names")), "2026-01-02T03:04:05+00:00")])
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["crate"], "crates/alpha")
        self.assertEqual(self.sync_wait_rows(), [])

    def test_combined_unit_skips_held_structure_and_preserves_unclassified_warning(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("modules", "names")),
                          StyleUnitAssignment("types", ("types",)), StyleUnitAssignment("values", ("values",))))
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        _ = self.rule("legacy", "")
        self.sync()
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources)):
            code, output, errors = self.run_next()
        self.assertEqual((code, errors), (3, "unclassified: 1 rules — pull nate_style\n"))
        self.assertIn("waiting for structure: crates/alpha, crates/beta\n", output)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names")), ())
        # The structure read becomes eligible after the product releases the files.
        self.assertEqual(self.run_next(), (0, "phase written: 1\n",
                                          "unclassified: 1 rules — pull nate_style\n"))
        ticket, = self.ledger().ticket_open(ticket_owner_id("names"))
        self.assertEqual((ticket["kind"], ticket["files"]), ("structure", list(self.sources[:2])))
        self.assertEqual(self.sync_wait_rows(), [])

    def test_names_refill_waits_for_sync_when_only_structure_cells_are_pending_in_scope(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        read = self.rule("read")
        self.sync()
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        for path in self.sources[:2]:
            self.record(read, path, self.tip)

        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], [str(next(row.id for row in staff.initial_snapshot().find(kind="unit") if row.name == "names"))])

    def test_combined_unit_structure_wait_and_read_exclude_crates_outside_its_pilot_scope(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("modules", "names")),
                          StyleUnitAssignment("types", ("types",)), StyleUnitAssignment("values", ("values",))))
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** beta\n\n## Phases"))

        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources)):
            code, output, errors = self.run_next()
        self.assertEqual((code, errors), (3, ""))
        self.assertIn("waiting for structure: crates/beta\n", output)
        self.assertNotIn("crates/alpha", output)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names")), ())
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        ticket, = self.ledger().ticket_open(ticket_owner_id("names"))
        self.assertEqual((ticket["kind"], ticket["crate"], ticket["files"]),
                         ("structure", "crates/beta", list(self.sources[2:])))

    def test_modules_refill_with_completed_structure_waits_for_sync_despite_pending_names_cells(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(structure, path, self.tip)
        plan = self.modules_unit()
        worktree = self.root / "modules"
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_plan.main(["next-phase", "--unit", "modules", "--plan", str(plan), "--repo", str(worktree)])

        self.assertEqual((code, output.getvalue()), (
            3, "no available work: modules no available work\n"
            + "waiting for sync\n"))
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], [str(next(row.id for row in staff.initial_snapshot().find(kind="unit") if row.name == "modules"))])


    def test_unheld_structure_read_starts_before_a_second_board_read(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("modules", "names")),
                          StyleUnitAssignment("types", ("types",)), StyleUnitAssignment("values", ("values",))))
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        with mock.patch.object(berth_board, "read_board", side_effect=[self.board(*self.sources[:2]),
                               AssertionError("unnecessary second board read")]) as board:
            self.assertEqual(self.run_next(), (
                0,
                "phase written: 1\n",
                "",
            ))
        self.assertEqual(board.call_count, 1)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["kind"], "structure")
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["files"], list(self.sources[2:]))
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_rechecks_after_registering_a_wait_and_finds_the_just_merged_crate(self) -> None:
        _ = self.rule("read")
        self.sync()
        reads = 0

        def board_after_merge(repo: Path) -> berth_board.Board:
            nonlocal reads
            self.assertEqual(repo, self.repo)
            reads += 1
            if reads == 2:
                self.assertEqual([row[0] for row in self.sync_wait_rows()], [str(next(row.id for row in staff.initial_snapshot().find(kind="unit") if row.name == "names"))])
            return self.board(*(self.sources if reads == 1 else self.sources[2:]))

        with mock.patch.object(berth_board, "read_board", side_effect=board_after_merge):
            self.assertEqual(style_plan.next_phase("names", self.plan, self.repo),
                             style_plan.PhaseWritten("1"))
        self.assertEqual(reads, 2)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["crate"], "crates/alpha")
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_drops_a_busy_structure_ticket_once_and_reads_unheld_structure(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("modules", "names")),
                          StyleUnitAssignment("types", ("types",)), StyleUnitAssignment("values", ("values",))))
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        _ = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "structure", "modules", "crates/alpha",
                          list(self.sources[:2]), [structure])
        output = io.StringIO()
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources[:2])) as board, redirect_stdout(output):
            result = style_plan.next_phase("names", self.plan, self.repo)
        self.assertEqual(result, style_plan.PhaseWritten("2"))
        self.assertEqual(board.call_count, 1)
        self.assertEqual(output.getvalue().count("dropped 1: files busy\n"), 1)
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names"))[0]["kind"], "structure")
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names"))[0]["files"], list(self.sources[2:]))
        self.assertEqual(self.sync_wait_rows(), [])
        self.assertEqual(ledger.queued_tickets(ticket_owner_id("names")), ())

    def test_edited_files_of_a_read_ticket_reads_no_synced_tip(self) -> None:
        read = self.rule("read")
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "read", "names", "crates/alpha",
                          list(self.sources[:2]), [read])
        edits = style_plan.EditedFiles(self.repo, ledger, style_plan.NoUnitBranch())
        self.assertEqual(edits.of_ticket(ledger.queued_tickets(ticket_owner_id("names"))[0]), self.sources[:2])
        self.assertIsNone(edits.tip)

    def test_board_failure_after_registering_a_wait_preserves_it_for_a_later_wake(self) -> None:
        _ = self.rule("read")
        self.sync()
        unavailable = berth_board.BoardUnavailable("second read failed")
        with mock.patch.object(berth_board, "read_board", side_effect=[self.board(*self.sources), unavailable]):
            result = style_plan.next_phase("names", self.plan, self.repo)
        self.assertEqual(result, unavailable)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], [str(next(row.id for row in staff.initial_snapshot().find(kind="unit") if row.name == "names"))])
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names")), ())

    def test_resuming_an_assigned_ticket_clears_a_persisted_sync_wait(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        self.seed_sync_wait()
        self.assertEqual(self.run_next(), (0, "resume 1\n", ""))
        self.assertEqual(self.sync_wait_rows(), [])

    def test_repair_board_and_merge_waits_leave_the_sync_wait_unchanged(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.seed_sync_wait()
        expected = [(str(next(row.id for row in staff.initial_snapshot().find(kind="unit") if row.name == "names")), "2026-01-01T00:00:00+00:00")]
        original = self.plan.read_text()
        _ = self.plan.write_text(original + "\n### Phase 3 — repair round 1 · status: todo\nRepair.\n")
        self.assertEqual(self.run_next(), (0, "repair first\n", ""))
        self.assertEqual(self.sync_wait_rows(), expected)
        _ = self.plan.write_text(original)
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.BoardUnavailable("unreadable")):
            self.assertEqual(self.run_next()[0], 4)
        self.assertEqual(self.sync_wait_rows(), expected)
        self.assertEqual(self.run_next()[0], 0)
        ticket = self.ledger().ticket_open(ticket_owner_id("names"))[0]
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                self.record(rule, path, self.tip, ticket["id"])
        self.ledger().ticket_checkpoint(ticket["id"], self.tip)
        self.seed_sync_wait()
        # Disjoint work now refills; block it here to retain the merge-wait preservation case.
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources)):
            self.assertEqual(self.run_next()[0], 5)
        self.assertEqual(self.sync_wait_rows(), expected)


    def test_legacy_only_waits_with_action_and_mixed_catalog_selects_only_classified_rules(self) -> None:
        _ = self.rule("legacy", "")
        self.sync()
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", "unclassified: 1 rules — pull nate_style\n"))
        read = self.rule("read")
        self.sync()
        self.assertEqual({rule for ticket in self.candidates() for rule in ticket.rules}, {read})
        self.assertEqual(self.run_next()[0], 0)

    def test_stalest_crate_is_first_and_read_tickets_cover_only_pending_cells(self) -> None:
        first = self.rule("first")
        second = self.rule("second")
        self.sync()
        self.record(first, self.sources[2], self.tip)
        self.record(second, self.sources[0], self.tip)
        self.write(self.sources[2], "pub fn revised() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        tickets = self.candidates()
        self.assertEqual(tickets[0].crate, "crates/beta")
        states = {(cell.rule, cell.path): cell.state for cell in self.ledger().cells()}
        for ticket in tickets:
            for rule in ticket.rules:
                for path in ticket.files:
                    self.assertIn(states[rule, path], ("open", "stale"))
        self.assertNotIn((second, self.sources[0]), {(rule, path) for ticket in tickets for rule in ticket.rules for path in ticket.files})

    def test_finished_phases_are_discarded_before_resuming_or_waiting_for_merge(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ledger = self.ledger()
        ledger.ticket_drop("1")
        self.assertEqual(self.run_next()[0], 0)
        current = self.plan.read_text()
        finished = "\n### Phase 1 — 1  · status: done\nFinished instructions.\n\n"
        for state in ("assigned", "checkpointed"):
            with self.subTest(state=state):
                _ = self.plan.write_text(current.replace("### Phase 2", finished + "### Phase 2"))
                if state == "checkpointed":
                    ticket = ledger.ticket_open(ticket_owner_id("names"))[0]
                    for rule in ticket["rules"]:
                        for path in ticket["files"]:
                            self.record(rule, path, self.tip, ticket["id"])
                    ledger.ticket_checkpoint("2", self.tip)
                with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources),
                                       side_effect=AssertionError("board read on resume") if state == "assigned" else None):
                    code, output, errors = self.run_next()
                self.assertEqual((code, errors), (0 if state == "assigned" else 5, ""))
                self.assertIn("discarded 1: dropped\n", output)
                self.assertIn("resume 2" if state == "assigned" else "waiting for merge: 2", output)
                self.assertNotIn("### Phase 1", self.plan.read_text())
                self.assertIn("### Phase 2", self.plan.read_text())
                self.assertEqual(self.count_tickets(), 2)


    def test_rebuild_after_rule_edit_replaces_stale_ticket_with_current_instructions(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ledger = self.ledger()
        pinned = ledger.pinned_rule_texts("1")[0]
        path = self.plan.parent.parent / "rules" / (pinned.blob + ".md")
        path.unlink()
        _ = self.plan.write_text(self.plan_header())
        _ = (self.rule_dir / "read.md").write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\nChanged instructions.\n")
        self.assertEqual(self.run_next(), (0, "phase written: 2\n", ""))
        self.assertEqual([ticket["id"] for ticket in ledger.ticket_open(ticket_owner_id("names"))], ["2"])
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM tickets WHERE id='1'").fetchone(),
                             ("dropped",))
        current = ledger.pinned_rule_texts("2")[0]
        self.assertNotEqual(current.blob, pinned.blob)
        self.assertIn(b"Changed instructions.", current.text)
        path = path.with_name(current.blob + ".md")
        self.assertEqual(path.read_bytes(), current.text)
        self.assertEqual(self.git("hash-object", str(path)), current.blob)
        self.assertIn("2", self.plan.read_text())
        self.assertNotIn("--ticket 1 ", self.plan.read_text())
        self.assertNotIn("### Phase 1 —", self.plan.read_text())

    def test_busy_queued_ticket_is_dropped_and_replaced_with_fresh_available_work(self) -> None:
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[0]], [rule])
        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            code, output, errors = self.run_next()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("dropped 1: files busy", output)
        self.assertIn("phase written: 2", output)
        self.assertNotIn(self.sources[0], ledger.ticket_open(ticket_owner_id("names"))[0]["files"])
        self.assertEqual(self.count_tickets(), 2)

    def test_checkpoint_waits_then_merged_phase_is_discarded_before_refill(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        ledger = self.ledger()
        ticket = ledger.ticket_open(ticket_owner_id("names"))[0]
        for rule in ticket["rules"]:
            for path in ticket["files"]:
                self.record(rule, path, self.tip, ticket["id"])
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        before = self.plan.read_bytes()
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources)):
            self.assertEqual(self.run_next(), (
                5, f"waiting for merge: 1 checkpointed at {self.tip[:7]}; no disjoint ticket available\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        _ = ledger.ticket_merge(ticket["id"])
        code, output, errors = self.run_next()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("discarded 1: merged", output)
        self.assertIn("phase written: 2", output)
        self.assertNotIn("### Phase 1 — 1", self.plan.read_text())
        self.assertIn("### Phase 2 — 2", self.plan.read_text())


    def test_reconcile_dropped_ticket_phase_is_removed_after_unit_branch_disappears(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        _ = self.git("checkout", "--detach", cwd=self.unit)
        _ = self.git("branch", "-D", "demo-style-names")
        ledger = self.ledger()
        _ = ledger.reconcile("main", "style/main")
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources)):
            code, output, errors = self.run_next()
        self.assertEqual((code, errors), (3, "style: names: the unit worktree has a detached HEAD\n"))
        self.assertIn("discarded 1: dropped", output)
        self.assertNotIn("### Phase 1 — 1", self.plan.read_text())

    def test_refill_builds_no_ticket_on_a_file_the_unit_base_changed_after_the_synced_tip(self) -> None:
        _ = self.only_first_source_open()
        unit_change = self.unit_commit(self.sources[0], "pub fn unit() {}\n")
        waiting = (f"waiting for sync: read crates/alpha: {self.sources[0]} changed in {unit_change[:7]}, "
                   + f"not in the synced tip {self.tip[:7]}\n")
        self.assertEqual(self.run_next(), (3, waiting + waiting + "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_builds_a_ticket_when_the_unit_base_changed_only_other_files(self) -> None:
        _ = self.only_first_source_open()
        _ = self.unit_commit(self.sources[1], "pub fn unit() {}\n")
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["files"], [self.sources[0]])

    def test_refill_builds_a_ticket_when_the_unit_base_is_behind_the_synced_tip(self) -> None:
        _ = self.only_first_source_open()
        self.write(self.sources[0], "pub fn synced() {}\n")
        self.tip = self.commit()
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["files"], [self.sources[0]])

    def test_a_decision_builds_no_ticket_on_a_file_the_unit_base_changed_after_the_synced_tip(self) -> None:
        rule = self.only_first_source_open()
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "approved.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        finding = ledger.hold(rule, self.sources[0], 1, "value", "rename", "old", "new", blob, "outside")
        ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text('---\nstatus: open\nstage: backlog\ntags: [issue, proposal, style]\n'
                             + f'unit: {unit_staff_id("names")}\nplan: "{self.plan} Phase 1"\nstyle_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n')
        self.proposals_field(folder)
        unit_change = self.unit_commit(self.sources[0], "pub fn value() { unit(); }\n")
        with mock.patch.object(style_proposals, "proposal_command") as start:
            code, output, _ = self.run_next()
        self.assertEqual(code, 3)
        self.assertIn(f"waiting {issue}: {self.sources[0]} changed in {unit_change[:7]}, not in the synced tip {self.tip[:7]}\n",
                      output)
        start.assert_not_called()
        self.assertEqual(self.count_tickets(), 0)

if __name__ == "__main__":
    _ = unittest.main()
