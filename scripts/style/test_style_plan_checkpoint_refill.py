"""Style planner checkpoint refill contracts."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_checkpoint_refill_disjoint_work_starts_at_the_waiting_checkpoint(self) -> None:
        ledger, _, checkpoint = self.waiting_ticket()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-2\n", ""))
        opened = ledger.ticket_open("names-unit")
        self.assertEqual(len(opened), 2)
        successor = next(ticket for ticket in opened if ticket["state"] == "assigned")
        self.assertEqual(successor["base"], checkpoint)
        self.assertNotIn(self.sources[0], successor["files"])
        self.assertIn("names-unit-2", self.plan.read_text())
        self.assertEqual(self.run_next(), (0, "resume names-unit-2\n", ""))
        self.assertEqual(len(ledger.ticket_open("names-unit")), 2)

    def test_checkpoint_refill_overlap_waits_names_files_and_keeps_the_queue(self) -> None:
        ledger, rule, checkpoint = self.waiting_ticket()
        ledger.ticket_add("names-unit-2", "names-unit", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        before = self.plan.read_bytes()
        self.assertEqual(self.run_next(), (
            5, f"waiting for merge: names-unit-1 checkpointed at {checkpoint[:7]}; overlap: {self.sources[0]}\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("names-unit")], ["names-unit-2"])

    def test_checkpoint_refill_unknown_footprint_keeps_the_wait(self) -> None:
        ledger, _, _ = self.waiting_ticket()
        before = self.plan.read_bytes()
        with mock.patch.object(style_plan.EditedFiles, "of_ticket", side_effect=style_ledger.LedgerError("unreadable")):
            code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""))
        self.assertIn("footprint unknown: unreadable", output)
        self.assertEqual(len(ledger.ticket_open("names-unit")), 1)
        self.assertEqual(self.plan.read_bytes(), before)

    def test_checkpoint_refill_unreadable_checkpoint_or_moved_branch_waits(self) -> None:
        ledger, _, checkpoint = self.waiting_ticket()
        for bad in ("0" * 40, self.tip):
            with self.subTest(checkpoint=bad), closing(sqlite3.connect(ledger.database)) as connection:
                _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='names-unit-1'", (bad,))
                connection.commit()
                code, output, _ = self.run_next()
                self.assertEqual(code, 5)
                self.assertIn("footprint unknown:", output)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='names-unit-1'", (checkpoint,))
        self.assertEqual(len(ledger.ticket_open("names-unit")), 1)

    def test_checkpoint_refill_waiting_rename_protects_reference_and_new_paths(self) -> None:
        rule = self.rule("read")
        old, new = "crates/alpha/src/probe.rs", "crates/alpha/src/frame_log.rs"
        reference = "crates/alpha/src/render.rs"
        issue = self.file_rename_decision(rule, ((old, "Rename probe.rs to frame_log.rs"),))
        ledger = self.ledger()
        finding = ledger.proposal_findings(issue)[0]
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [old], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("names-unit-1", "demo-style-names")
        _ = ledger.ticket_base("names-unit-1")
        _ = self.git("mv", old, new, cwd=self.unit)
        for path in ("crates/alpha/src/lib.rs", reference):
            source = self.unit / path
            _ = source.write_text(source.read_text().replace("probe", "frame_log"))
        _ = self.git("add", "--all", cwd=self.unit)
        _ = self.git("commit", "-qm", "Rename waiting for merge", cwd=self.unit)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        ledger.ticket_checkpoint("names-unit-1", checkpoint)
        for number, path in enumerate((reference, new), 2):
            with self.subTest(path=path):
                ticket = f"names-unit-{number}"
                ledger.ticket_add(ticket, "names-unit", "read", "names", "crates/alpha", [path], [rule])
                code, output, error = self.run_next()
                self.assertEqual((code, error), (5, ""))
                self.assertIn("overlap: " + path, output)
                self.assertEqual(len(ledger.ticket_open("names-unit")), 1)
                ledger.ticket_drop(ticket)

    def test_checkpoint_refill_stops_after_the_successor_checkpoints(self) -> None:
        ledger, rule, _ = self.waiting_ticket()
        self.assertEqual(self.run_next()[0], 0)
        successor = next(ticket for ticket in ledger.ticket_open("names-unit") if ticket["state"] == "assigned")
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        for path in successor["files"]:
            self.record(rule, path, checkpoint, successor["id"])
        ledger.ticket_checkpoint(successor["id"], checkpoint)
        code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""))
        self.assertIn("two open tickets", output)
        self.assertEqual(self.count_tickets(), 2)

    def test_checkpoint_refill_unknown_selected_footprint_waits_without_assignment(self) -> None:
        ledger, _, _ = self.waiting_ticket()
        with mock.patch.object(style_plan.WaitingCheckpoint, "check", side_effect=style_ledger.LedgerError("selected unreadable")):
            code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""))
        self.assertIn("footprint unknown: selected unreadable", output)
        self.assertEqual(len(ledger.ticket_open("names-unit")), 1)

    def test_checkpoint_refill_build_inputs_and_rename_paths_block_symmetrically(self) -> None:
        work = style_plan.WaitingCheckpoint("names-unit-1", self.tip, frozenset({self.sources[0]}),
                                           ("crates/alpha", "crates/beta"))
        for path in (self.sources[0], "crates/alpha/Cargo.toml", "crates/alpha/build.rs", "Cargo.toml", "Cargo.lock"):
            with self.subTest(path=path):
                self.assertIsInstance(work.check((path,)), style_plan.WaitingForMerge)
                reverse = style_plan.WaitingCheckpoint("names-unit-1", self.tip, frozenset({path}), work.crates)
                self.assertIsInstance(reverse.check((self.sources[0],)), style_plan.WaitingForMerge)
        self.assertIsInstance(work.check((self.sources[1],)), style_plan.NoCheckpointBlock)
        self.assertIsInstance(work.check((self.sources[2],)), style_plan.NoCheckpointBlock)
        with self.assertRaisesRegex(style_ledger.LedgerError, "unknown crate"):
            _ = work.check(("unknown/file.rs",))



if __name__ == "__main__":
    _ = unittest.main()
