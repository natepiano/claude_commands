"""Style planner checkpoint refill contracts."""

from __future__ import annotations

from scripts.style.ledger_test_support import DEFAULT_STYLE_ASSIGNMENTS, ticket_owner_id
from scripts.style.style_config import StyleUnitAssignment

import json
import os
import subprocess
import sqlite3
import unittest
from contextlib import closing
from collections.abc import Callable
from typing import cast, final
from unittest import mock


from scripts.style import style_plan
from scripts.direct import progress_history
from scripts.direct.assign_launcher_test_support import AssignLauncherFixture, invoke_assignment
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

    def test_queued_sync_survives_a_ledger_sync_and_blocks_a_free_worker_slot(self) -> None:
        ledger = self.queued_parallel_work(2)
        self.assertEqual(self.run_next()[0], 0)
        self.write(self.sources[3], "pub fn main_change() {}\n")
        self.tip = self.commit()
        self.assertEqual(self.run_next(), (0, "resume 1\nresume 2\n", ""))
        pending = style_ledger.publication_path(self.unit, "pending-sync")
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

    def test_pending_sync_ends_after_any_merge_contains_the_saved_tip(self) -> None:
        ledger = self.queued_parallel_work(2)
        ledger.ticket_assign("1", "demo-style-names")
        self.write(self.sources[3], "pub fn main_change() {}\n")
        main = self.commit()
        self.assertEqual(self.run_next()[0], 3)
        pending = style_ledger.publication_path(self.unit, "pending-sync")
        self.assertEqual(pending.read_text().strip(), main)
        self.sync()
        _ = self.git("merge", "--no-edit", main, cwd=self.unit)
        self.assertEqual(self.run_next()[0], 0)
        self.assertFalse(pending.exists())
        self.assertEqual(len(ledger.ticket_open(ticket_owner_id("names"))), 2)

    def test_single_ticket_does_not_keep_a_pending_sync_after_checkpoint(self) -> None:
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("1", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[0]], [rule])
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.write(self.sources[3], "pub fn main_change() {}\n")
        self.tip = self.commit()
        self.assertEqual(self.run_next(), (0, "resume 1\n", ""))
        pending = style_ledger.publication_path(self.unit, "pending-sync")
        self.assertFalse(pending.exists())
        self.sync()
        ticket = ledger.ticket_get("1")
        head = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.record(ticket["rules"][0], ticket["files"][0], head, "1")
        ledger.ticket_checkpoint("1", head)
        code, output, error = self.run_next()
        self.assertEqual((code, error), (0, ""))
        self.assertIn("phase written: 2", output)
        self.assertFalse(pending.exists())

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
        self.assertIn("two waiting checkpoints", output)
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


@final
class TicketWorkerTests(AssignLauncherFixture):
    def test_worker_slots_record_real_dispatches_and_reuse_an_existing_name_after_failure(self) -> None:
        plan = self.project / "plan.md"
        _ = plan.write_text("# Standing plan\n\n## Phases\n\n"
                            + "### Phase 7 — 7 · status: todo\nEdit the assigned source.\n\n"
                            + "### Phase 8 — 8 · status: todo\nEdit the other source.\n")
        self.record("start-phase", "--phase-id", "8", "--phase-title", "8")
        original = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        submitted: list[list[str]] = []
        def command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["bash", str(style_plan.SCRIPTS / "direct/assign.sh")]:
                submitted.append(args)
                return invoke_assignment(args[2:], os.environ.copy())
            return original(args, **kwargs)
        tickets = ("7", "8")
        with mock.patch.object(subprocess, "run", side_effect=command):
            for ticket in tickets:
                style_plan.restore_ticket_worker(self.session_dir, self.project, plan, ticket, ("src/worker.py",))
            record_file = self.session_dir / "progress_history_state.json"
            passes = cast(dict[str, dict[str, object]], json.loads(record_file.read_text())["pass"])
            self.assertEqual(set(passes), {"style-ticket-7", "style-ticket-8"})
            self.assertEqual({slot: entry["status"] for slot, entry in passes.items()},
                             {"style-ticket-7": "active", "style-ticket-8": "active"})
            for ticket in tickets:
                style_plan.restore_ticket_worker(self.session_dir, self.project, plan, ticket, ("src/worker.py",))
            self.assertEqual(len(submitted), 2)
            progress_history.complete_dispatch(self.session_dir, "style-ticket-7", str(passes["style-ticket-7"]["instance_id"]),
                                               status="error")
            before = self.assignment_records()
            style_plan.restore_ticket_worker(self.session_dir, self.project, plan, tickets[0], ("src/worker.py",))
            self.assertEqual(self.assignment_records(), before)
        self.assertEqual([args[args.index("--new-worker") + 1] for args in submitted],
                         ["style-ticket-7", "style-ticket-8", "style-ticket-7"])
        self.assertEqual(self.turn_starts, 3)
        prompt = self.session_dir / "phases/7/ticket-work-order.md"
        for sentence in (
            "We believe in three gods, Simple, Fast and Beautiful, with correctness as the floor beneath them.",
            "Type Design Contract: ~/.claude/docs/type_design.md applies to the code you change.",
            "Edit only the listed paths.",
            "Run no git add, commit, stash, reset or checkout.",
            "Start no detached run you will not wait on.",
            "Finish your summary before you end your turn.",
        ):
            self.assertIn(sentence, prompt.read_text())


if __name__ == "__main__":
    _ = unittest.main()
