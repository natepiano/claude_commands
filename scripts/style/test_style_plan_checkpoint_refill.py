"""Style planner checkpoint refill contracts."""

from __future__ import annotations

from scripts.style.ledger_test_support import DEFAULT_STYLE_ASSIGNMENTS, ticket_owner_id
from scripts.style.style_config import StyleUnitAssignment

import json
import subprocess
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from typing import cast, final
from unittest import mock


from scripts.style import style_plan
from scripts.style import style_ledger

from scripts.style.style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def queued_parallel_work(self, capacity: int) -> style_ledger.Ledger:
        self.style_units(tuple(StyleUnitAssignment(item.unit, item.families, capacity if item.unit == "names" else 1)
                               for item in DEFAULT_STYLE_ASSIGNMENTS))
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        for number, path in enumerate(self.sources, 1):
            ledger.ticket_add(str(number), ticket_owner_id("names"), "read", "names",
                              "crates/alpha" if number <= 2 else "crates/beta", [path], [rule])
        return ledger

    def test_refill_fills_capacity_and_keeps_each_phase(self) -> None:
        ledger = self.queued_parallel_work(3)
        code, output, error = self.run_next()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output, "phase written: 1\nphase written: 2\nphase written: 3\n")
        self.assertEqual([ticket["id"] for ticket in ledger.ticket_open(ticket_owner_id("names"))], ["1", "2", "3"])
        self.assertEqual([match.group("id") for match in style_plan.PHASE.finditer(self.plan.read_text())], ["1", "2", "3"])
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets(ticket_owner_id("names"))], ["4"])

    def test_restart_restores_every_assigned_phase_before_refill(self) -> None:
        ledger = self.queued_parallel_work(3)
        for ticket in ("1", "2"):
            ledger.ticket_assign(ticket, "demo-style-names")
        self.assertEqual(self.run_next(), (0, "resume 1\nresume 2\nphase written: 3\n", ""))
        self.assertEqual([match.group("id") for match in style_plan.PHASE.finditer(self.plan.read_text())], ["1", "2", "3"])
        active = self.plan.read_text().replace("### Phase 1 — 1  · status: todo", "### Phase 1 — 1  · status: active")
        _ = self.plan.write_text(active)
        self.assertEqual(self.run_next(), (0, "resume 1\nresume 2\nresume 3\n", ""))
        self.assertEqual(self.plan.read_text(), active)

    def test_lower_capacity_restores_all_assigned_tickets(self) -> None:
        ledger = self.queued_parallel_work(3)
        self.assertEqual(self.run_next()[0], 0)
        self.style_units()
        self.assertEqual(self.run_next(), (0, "resume 1\nresume 2\nresume 3\n", ""))
        self.assertEqual(len(ledger.ticket_open(ticket_owner_id("names"))), 3)
        self.assertEqual(len(ledger.queued_tickets(ticket_owner_id("names"))), 1)

    def test_main_movement_queues_sync_without_another_assignment(self) -> None:
        ledger = self.queued_parallel_work(3)
        ledger.ticket_assign("1", "demo-style-names")
        self.write(self.sources[3], "pub fn main_change() {}\n")
        _ = self.commit()
        code, output, error = self.run_next()
        self.assertEqual((code, error), (3, ""))
        self.assertIn("main moved while tickets are assigned", output)
        self.assertEqual([ticket["id"] for ticket in ledger.ticket_open(ticket_owner_id("names"))], ["1"])
        self.assertEqual(len(self.sync_wait_rows()), 1)
        self.assertIn("### Phase 1 — 1", self.plan.read_text())
        self.assertEqual(style_plan.sync_unit(self.unit, "names"), 4)

    def test_queued_sync_survives_a_ledger_sync_and_blocks_a_free_worker_slot(self) -> None:
        ledger = self.queued_parallel_work(2)
        self.assertEqual(self.run_next()[0], 0)
        self.write(self.sources[3], "pub fn main_change() {}\n")
        self.tip = self.commit()
        self.assertEqual(self.run_next(), (0, "resume 1\nresume 2\n", ""))
        pending = style_plan.publication_path(self.unit, "pending-sync")
        self.assertEqual(pending.read_text().strip(), self.tip)
        self.sync()
        ticket = ledger.ticket_get("1")
        head = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.record(ticket["rules"][0], ticket["files"][0], head, "1")
        ledger.ticket_checkpoint("1", head)
        code, output, error = self.run_next()
        self.assertEqual((code, error), (3, ""))
        self.assertIn("queued main sync", output)
        self.assertEqual([ticket["id"] for ticket in ledger.ticket_open(ticket_owner_id("names"))], ["1", "2"])
        self.assertTrue(pending.exists())

    def test_pending_sync_merges_main_only_after_the_workers_finish_and_the_tree_is_clean(self) -> None:
        ledger = self.queued_parallel_work(2)
        ledger.ticket_assign("1", "demo-style-names")
        ticket = ledger.ticket_get("1")
        self.record(ticket["rules"][0], ticket["files"][0], self.tip, "1")
        self.write(self.sources[3], "pub fn main_change() {}\n")
        main = self.commit()
        self.assertEqual(style_plan.sync_unit(self.unit, "names"), 4)
        ledger.ticket_checkpoint("1", self.tip)
        partial = self.unit / self.sources[1]
        old = partial.read_text()
        _ = partial.write_text(old + "// partial\n")
        self.assertEqual(style_plan.sync_unit(self.unit, "names"), 4)
        _ = partial.write_text(old)
        with mock.patch.object(style_ledger.Ledger, "check_ticket_pipeline", wraps=ledger.check_ticket_pipeline) as proof:
            self.assertEqual(style_plan.sync_unit(self.unit, "names"), 0)
        proof.assert_called_with("1")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.unit), main)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_worker_slots_are_distinct_and_running_workers_are_not_submitted_again(self) -> None:
        ledger = self.queued_parallel_work(3)
        self.assertEqual(self.run_next()[0], 0)
        run = self.root / "run"
        run.mkdir()
        record: dict[str, object] = {"status": "active", "pass": {}}
        record_file = run / "progress_history_state.json"
        _ = record_file.write_text(json.dumps(record))
        submitted: list[list[str]] = []
        def command(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "bash":
                submitted.append(args)
                slot = args[args.index("--new-worker") + 1]
                state = cast(dict[str, dict[str, object]], json.loads(record_file.read_text()))
                state["pass"][slot] = {"status": "active"}
                _ = record_file.write_text(json.dumps(state))
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(subprocess, "run", side_effect=command):
            for _ in range(2):
                for ticket in ledger.ticket_open(ticket_owner_id("names")):
                    style_plan.restore_ticket_worker(run, self.unit, self.plan, ticket, tuple(ticket["files"]))
            failed = cast(dict[str, dict[str, object]], json.loads(record_file.read_text()))
            failed["pass"]["style-ticket-2"] = {"status": "error"}
            _ = record_file.write_text(json.dumps(failed))
            ticket = ledger.ticket_get("2")
            style_plan.restore_ticket_worker(run, self.unit, self.plan, ticket, tuple(ticket["files"]))
        self.assertEqual([args[args.index("--new-worker") + 1] for args in submitted],
                         ["style-ticket-1", "style-ticket-2", "style-ticket-3", "style-ticket-2"])
        self.assertEqual([args[args.index("--phase") + 1] for args in submitted], ["1", "2", "3", "2"])
        self.assertTrue(all(Path(args[args.index("--paths") + 1]).is_file() for args in submitted))

    def test_checkpoint_refill_disjoint_work_starts_at_the_waiting_checkpoint(self) -> None:
        ledger, _, checkpoint = self.waiting_ticket()
        self.assertEqual(self.run_next(), (0, "phase written: 2\n", ""))
        opened = ledger.ticket_open(ticket_owner_id("names"))
        self.assertEqual(len(opened), 2)
        successor = next(ticket for ticket in opened if ticket["state"] == "assigned")
        self.assertEqual(successor["base"], checkpoint)
        self.assertNotIn(self.sources[0], successor["files"])
        self.assertIn("2", self.plan.read_text())
        self.assertEqual(self.run_next(), (0, "resume 2\n", ""))
        self.assertEqual(len(ledger.ticket_open(ticket_owner_id("names"))), 2)

    def test_checkpoint_refill_overlap_waits_names_files_and_keeps_the_queue(self) -> None:
        ledger, rule, checkpoint = self.waiting_ticket()
        ledger.ticket_add("2", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[0]], [rule])
        before = self.plan.read_bytes()
        self.assertEqual(self.run_next(), (
            5, f"waiting for merge: 1 checkpointed at {checkpoint[:7]}; overlap: {self.sources[0]}\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets(ticket_owner_id("names"))], ["2"])

    def test_checkpoint_refill_unknown_footprint_keeps_the_wait(self) -> None:
        ledger, _, _ = self.waiting_ticket()
        before = self.plan.read_bytes()
        with mock.patch.object(style_plan.EditedFiles, "of_ticket", side_effect=style_ledger.LedgerError("unreadable")):
            code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""))
        self.assertIn("footprint unknown: unreadable", output)
        self.assertEqual(len(ledger.ticket_open(ticket_owner_id("names"))), 1)
        self.assertEqual(self.plan.read_bytes(), before)

    def test_checkpoint_refill_unreadable_checkpoint_or_moved_branch_waits(self) -> None:
        ledger, _, checkpoint = self.waiting_ticket()
        for bad in ("0" * 40, self.tip):
            with self.subTest(checkpoint=bad), closing(sqlite3.connect(ledger.database)) as connection:
                _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='1'", (bad,))
                connection.commit()
                code, output, _ = self.run_next()
                self.assertEqual(code, 5)
                self.assertIn("footprint unknown:", output)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id='1'", (checkpoint,))
        self.assertEqual(len(ledger.ticket_open(ticket_owner_id("names"))), 1)

    def test_checkpoint_refill_waiting_rename_protects_reference_and_new_paths(self) -> None:
        rule = self.rule("read")
        old, new = "crates/alpha/src/probe.rs", "crates/alpha/src/frame_log.rs"
        reference = "crates/alpha/src/render.rs"
        issue = self.file_rename_decision(rule, ((old, "Rename probe.rs to frame_log.rs"),))
        ledger = self.ledger()
        finding = ledger.proposal_findings(issue)[0]
        ledger.ticket_add("1", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [old], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("1", "demo-style-names")
        _ = ledger.ticket_base("1")
        _ = self.git("mv", old, new, cwd=self.unit)
        for path in ("crates/alpha/src/lib.rs", reference):
            source = self.unit / path
            _ = source.write_text(source.read_text().replace("probe", "frame_log"))
        _ = self.git("add", "--all", cwd=self.unit)
        _ = self.git("commit", "-qm", "Rename waiting for merge", cwd=self.unit)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        ledger.ticket_checkpoint("1", checkpoint)
        for number, path in enumerate((reference, new), 2):
            with self.subTest(path=path):
                ticket = f"{number}"
                ledger.ticket_add(ticket, ticket_owner_id("names"), "read", "names", "crates/alpha", [path], [rule])
                code, output, error = self.run_next()
                self.assertEqual((code, error), (5, ""))
                self.assertIn("overlap: " + path, output)
                self.assertEqual(len(ledger.ticket_open(ticket_owner_id("names"))), 1)
                ledger.ticket_drop(ticket)

    def test_checkpoint_refill_stops_after_the_successor_checkpoints(self) -> None:
        ledger, rule, _ = self.waiting_ticket()
        self.assertEqual(self.run_next()[0], 0)
        successor = next(ticket for ticket in ledger.ticket_open(ticket_owner_id("names")) if ticket["state"] == "assigned")
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
        self.assertEqual(len(ledger.ticket_open(ticket_owner_id("names"))), 1)

    def test_checkpoint_refill_build_inputs_and_rename_paths_block_symmetrically(self) -> None:
        work = style_plan.WaitingCheckpoint("1", self.tip, frozenset({self.sources[0]}),
                                           ("crates/alpha", "crates/beta"))
        for path in (self.sources[0], "crates/alpha/Cargo.toml", "crates/alpha/build.rs", "Cargo.toml", "Cargo.lock"):
            with self.subTest(path=path):
                self.assertIsInstance(work.check((path,)), style_plan.WaitingForMerge)
                reverse = style_plan.WaitingCheckpoint("1", self.tip, frozenset({path}), work.crates)
                self.assertIsInstance(reverse.check((self.sources[0],)), style_plan.WaitingForMerge)
        self.assertIsInstance(work.check((self.sources[1],)), style_plan.NoCheckpointBlock)
        self.assertIsInstance(work.check((self.sources[2],)), style_plan.NoCheckpointBlock)
        with self.assertRaisesRegex(style_ledger.LedgerError, "unknown crate"):
            _ = work.check(("unknown/file.rs",))



if __name__ == "__main__":
    _ = unittest.main()
