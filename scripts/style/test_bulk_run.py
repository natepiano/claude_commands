"""Workspace bulk runs use temporary Git repos and a fake command runner; never build Rust."""
from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname, isolated_staff_file

import json
import fcntl
import os
import shutil
import signal
import tempfile
import sqlite3
import subprocess
import unittest
from io import StringIO
from unittest import mock
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, TypedDict, cast, final, override
from types import SimpleNamespace


from scripts.style import bulk_run as run
from scripts.style import style_sync
from scripts.style import bulk_rename as bulk
from scripts.style import fake_fmt
from scripts.style.style_child_test_support import StyleQueueFake, install_refusal_tools, unqueued_style_children
from scripts.build_queue import queue_place
from scripts.style.style_plan_test_support import unit_staff_id
from scripts.lib import berth_board
from scripts.copies import copies
from scripts.style import rules
from scripts.style.ledger_test_support import machine_rename_fixture, MACHINE_RENAME_AFTER, ticket_owner_id
from scripts.style import style_ledger as ledger_api
from scripts.style.approved_renames import AppliedInCode, BindingRename, NoBindingScope, rename_declaration
from scripts.style.test_bulk_rename import CargoProjectTests, ScriptedServer, word_edits, stop_after_edit, stop_process


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.error_file: str = ""
        self.no_progress: bool = False
        self.red_tests: bool = False
        self.unrelated: bool = False
        self.break_proof: bool = False
        self.test_checkout: Path = Path()
        self.test_tree: str = ""
        self.fail_stage: str = ""
        self.mutate_stage: str = ""
        self.other_tree_record: bool = False

    def __call__(self, repo: Path, arguments: Sequence[str]) -> run.CommandResult:
        args = tuple(arguments)
        self.calls.append((repo, args))
        if "fmt" in args:
            if self.unrelated:
                _ = (repo / "Cargo.toml").write_text("unrelated change")
            if self.break_proof:
                _ = (repo / "src/lib.rs").write_text("pub fn run() {}\n")
        if len(args) > 1 and args[1] in ("check", "clippy"):
            if self.no_progress:
                return run.CommandResult(1, "linker failed without a source span")
            if self.error_file and "let number" in (repo / self.error_file).read_text():
                return run.CommandResult(1, json.dumps({"reason": "compiler-message", "message": {
                    "level": "error", "spans": [{"file_name": self.error_file}], "children": []}}))
        if len(args) >= 3 and args[0] == "bash":
            self.test_checkout = repo
            self.test_tree = run.git(repo, "rev-parse", "HEAD^{tree}")
            stage = "final --saved" if args[2:4] == ("final", "--saved") else args[2]
            if stage == self.mutate_stage:
                _ = (repo / "unexpected.txt").write_text("untracked test output")
            if stage == "final" and self.red_tests:
                return run.CommandResult(1, "FAIL [0.01s] bulk_sample::tests::broken\n")
            if stage == self.fail_stage:
                return run.CommandResult(1, stderr=f"{stage} refused")
            if stage == "final --saved" and (len(args) != 6 or args[4] != "--tree" or self.other_tree_record or args[5] != self.test_tree):
                return run.CommandResult(1, stderr="no matching tested-tree record")
        return run.CommandResult(0)


class BulkRunFixture(CargoProjectTests):
    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.prepare_project_seed()
        cls.fixture_seed = cls.project_seed.parent / "fixture"
        fixture = cls(methodName="runTest")
        environment = os.environ.copy()
        try:
            CargoProjectTests.setUp(fixture)
            cls.fixture_root = fixture.root
            cls.fixture_environment = {key: value for key, value in os.environ.items()
                                       if environment.get(key) != value}
            _ = shutil.copytree(fixture.root, cls.fixture_seed, symlinks=True)
        finally:
            fixture.doCleanups()

    @classmethod
    def prepare_project_seed(cls) -> None:
        pass

    @override
    def setUp(self) -> None:
        if self.fixture_root.exists():
            super().setUp()
            return
        self.root: Path = self.fixture_root
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _ = shutil.copytree(self.fixture_seed, self.root, symlinks=True)
        self.repo: Path = self.root / "project"
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.fixture_environment)))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.queue: StyleQueueFake = self.enterContext(unqueued_style_children())


class WorkspaceRunTests(BulkRunFixture):
    base: str = ""
    tree: str = ""
    runner: FakeRunner = FakeRunner()
    server: ScriptedServer = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))

    @classmethod
    @override
    def prepare_project_seed(cls) -> None:
        fixture = cls(methodName="runTest")
        fixture.repo = cls.project_seed
        _ = (fixture.repo / "Cargo.toml").write_text(
            (fixture.repo / "Cargo.toml").read_text() + '\n[workspace]\nmembers=[".", "beta"]\n')
        (fixture.repo / "beta/src").mkdir(parents=True)
        _ = (fixture.repo / "beta/Cargo.toml").write_text(
            '[package]\nname="beta"\nversion="0.1.0"\nedition="2021"\n')
        _ = (fixture.repo / "beta/src/lib.rs").write_bytes((fixture.repo / "src/lib.rs").read_bytes())
        _ = fixture.git("add", ".")
        _ = fixture.git("commit", "-qm", "workspace fixture")
        cls.base = fixture.git("rev-parse", "HEAD")
        cls.tree = fixture.git("rev-parse", "HEAD^{tree}")

    def assert_refused_writer_releases_its_finding(self, *, shared_proposal: bool) -> None:
        ledger, definition, findings = self.setup_findings(shared_proposal=shared_proposal)
        self.addCleanup(ledger.close)
        holder = berth_board.Reservation("writer", "style/main", self.root / "product",
                                         "refs/heads/product", ("src/lib.rs",), "active", "blocking")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((holder,))):
            report_path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                                       self.server, self.runner, bulk.check_available)
        report = bulk.object_fields(cast(object, json.loads(report_path.read_text())))
        self.assertEqual((report["applied"], report["refused"], report["reverted"]), (1, 1, 0))
        self.assertNotIn("pending", report)
        reason = bulk.object_fields(report["refusals"])[findings[0].finding_id]
        for detail in ("src/lib.rs", str(holder.holder_worktree_root), holder.holder_branch, holder.reservation_id):
            self.assertIn(detail, str(reason))
        self.assertEqual(report["status"], "tested; checkpointed; built at normal ticket merge")
        self.assertIn("let old", (self.repo / "src/lib.rs").read_text())
        self.assertIn("let number", (self.repo / "beta/src/lib.rs").read_text())
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "1")
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == "bash"],
                         ["final", "lint", "final --saved"])
        released = ledger.named_held((findings[0].finding_id,))[0]
        self.assertIsInstance(released.build_ticket, ledger_api.NoBuildTicket)
        self.assertIsInstance(released.state, ledger_api.Filed)
        ready = ledger.named_held((findings[1].finding_id,))[0]
        self.assertIsInstance(ready.build_ticket, ledger_api.BuildTicket)
        assert isinstance(ready.build_ticket, ledger_api.BuildTicket)
        ready_ticket = next(item for item in ledger.proposal_tickets() if item["id"] == ready.build_ticket.ticket)
        self.assertEqual(ready_ticket["state"], "checkpointed")
        tickets = [item for item in ledger.proposal_tickets() if item["id"] in cast(list[str], report["tickets"])]
        self.assertTrue(all(item["state"] in ("checkpointed", "dropped") for item in tickets))
        self.assertEqual(sum(len(ledger.bulk_refusals(item["id"])) for item in tickets), 1)

    def test_workspace_applies_ready_findings_and_releases_a_blocked_writers_claim(self) -> None:
        self.assert_refused_writer_releases_its_finding(shared_proposal=False)

    def test_workspace_checkpoints_a_shared_ticket_after_releasing_its_blocked_member(self) -> None:
        self.assert_refused_writer_releases_its_finding(shared_proposal=True)

    def test_workspace_releases_an_entire_blocked_batch_without_tests(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        holder = berth_board.Reservation("writer", "style/main", self.root / "product",
                                         "refs/heads/product", ("tree:.",), "active", "blocking")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((holder,))):
            path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                                self.server, self.runner, bulk.check_available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report["applied"], report["refused"], report["reverted"]), (0, 2, 0))
        self.assertNotIn("pending", report)
        self.assertEqual(report["status"], "no edits")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertTrue(all(isinstance(item.build_ticket, ledger_api.NoBuildTicket)
                            for item in ledger.named_held(item.finding_id for item in findings)))
        self.assertTrue(all(item["state"] == "dropped" for item in ledger.proposal_tickets()
                            if item["id"] in cast(list[str], report["tickets"])))

    def test_workspace_writer_arriving_during_execution_rolls_back_and_releases_the_batch(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        holder = berth_board.Reservation("writer", "style/main", self.root / "product",
                                         "refs/heads/product", ("tree:.",), "active", "blocking")
        checked: Counter[str] = Counter()
        def boundary(_repo: Path, path: str) -> None:
            checked[path] += 1
            if checked[path] > 1:
                raise bulk.ReservationBlocked(path, (holder,))
        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                            self.server, self.runner, boundary)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report["applied"], report["refused"], report["reverted"]), (0, 0, 2))
        self.assertNotIn("pending", report)
        self.assertTrue(str(report["status"]).startswith("batch refused:"))
        self.assertTrue(all(item["state"] == "dropped" for item in ledger.proposal_tickets()))
        self.assertTrue(all(isinstance(item.build_ticket, ledger_api.NoBuildTicket)
                            for item in ledger.named_held(item.finding_id for item in findings)))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.runner.calls, [])

    def shared_file_reservation_probe(self) -> tuple[ledger_api.Ledger, rules.Rule, ledger_api.HeldFinding]:
        path = self.repo / "beta/src/lib.rs"
        _ = path.write_text(path.read_text() + "fn extra() {\n    let other = 2;\n    let _ = other + 1;\n}\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "shared file checkpoint fixture")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        ledger, definition, findings = self.setup_findings(shared_proposal=True)
        self.addCleanup(ledger.close)
        extra = self.approve_extra_rename(ledger, definition, 6, "other", "size", "beta/src/lib.rs")
        self.server = ScriptedServer(lambda text, offset, new:
                                     word_edits(text, "old" if new == "number" else "other", new))
        holder = berth_board.Reservation("writer", "style/main", self.root / "product",
                                         "refs/heads/product", ("src/lib.rs",), "active", "blocking")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((holder,))):
            report_path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                                       self.server, self.runner, bulk.check_available)
        report = bulk.object_fields(cast(object, json.loads(report_path.read_text())))
        self.assertEqual((report["applied"], report["refused"], report["reverted"]), (2, 1, 0))
        self.assertNotIn("pending", report)
        reason = str(bulk.object_fields(report["refusals"])[findings[0].finding_id])
        self.assertIn(str(holder.holder_worktree_root), reason)
        self.assertIn(holder.holder_branch, reason)
        self.assertIn(holder.reservation_id, reason)
        built = ledger.named_held((extra.finding_id,))[0]
        self.assertIsInstance(ledger.applied_check(built, self.git("rev-parse", "HEAD")), AppliedInCode)
        tickets = cast(list[str], report["tickets"])
        self.assertEqual(len(tickets), 2)
        self.assertTrue(all(item["state"] == "checkpointed" and item["checkpoint"] == report["commit"]
                            for item in ledger.proposal_tickets() if item["id"] in tickets))
        released = ledger.named_held((findings[0].finding_id,))[0]
        self.assertIsInstance(released.build_ticket, ledger_api.NoBuildTicket)
        self.assertIsInstance(released.state, ledger_api.Filed)
        ledger.set_tip(self.git("rev-parse", "HEAD"), "main")
        for ticket in tickets:
            self.assertEqual(ledger.ticket_merge(ticket), ())
        return ledger, definition, released

    def test_workspace_shared_file_proof_includes_every_contributing_tickets_checkpoint(self) -> None:
        _ = self.shared_file_reservation_probe()

    def test_later_workspace_run_selects_the_released_finding_after_the_writer_leaves(self) -> None:
        ledger, definition, released = self.shared_file_reservation_probe()
        tree = self.git("rev-parse", "HEAD^{tree}")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(())):
            path = run.run_rule(self.repo, definition.rule_id, tree, ledger,
                                self.server, self.runner, bulk.check_available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report["applied"], report["refused"], report["reverted"]), (1, 0, 0))
        self.assertEqual(report["applied_ids"], [released.finding_id])
        self.assertEqual(report["status"], "tested; checkpointed; built at normal ticket merge")
        self.assertIn("let number", (self.repo / "src/lib.rs").read_text())
        ledger.set_tip(self.git("rev-parse", "HEAD"), "main")
        for ticket in cast(list[str], report["tickets"]):
            self.assertEqual(ledger.ticket_merge(ticket), ())
        self.assertIsInstance(ledger.named_held((released.finding_id,))[0].state, ledger_api.Built)

    def test_unnormalizable_shorthand_refusal_keeps_independent_plan_before_any_write(self) -> None:
        source = 'pub fn run() {\n    let old = 1;\n    consume(Outcome::Honored { old });\n}\n'
        _ = (self.repo / 'src/lib.rs').write_text(source)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'candidate shorthand fixture')
        self.base, self.tree = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD^{tree}')
        ledger, _definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        original_beta = (self.repo / 'beta/src/lib.rs').read_text()
        def edits(text: str, _offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
            return tuple(replace(edit, text=new + ' /* analyzer fragment */')
                         if text[edit.end:].startswith(' });') else edit
                         for edit in word_edits(text, 'old', new))
        server = ScriptedServer(edits)
        plan = run.plan_renames(self.repo, ledger, self.base, run.Selection(findings, ()), server,
                               run.PlanningFileAvailability(self.repo, self.available))
        self.assertEqual([item.finding_id for item in plan.refusals], [findings[0].finding_id])
        self.assertIn('record shorthand', plan.refusals[0].reason)
        self.assertEqual([file.path for file in plan.files], ['beta/src/lib.rs'])
        self.assertIn('let number', plan.files[0].edited)
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), source)
        self.assertEqual((self.repo / 'beta/src/lib.rs').read_text(), original_beta)

    def test_recovery_preserves_another_users_ticket_after_plan_record(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        begin = bulk.BatchIntent.begin

        def stopped(intent: bulk.BatchIntent, files: tuple[bulk.FileRenames, ...],
                    tickets: Sequence[str] = (), identities: Sequence[str] = ()) -> None:
            begin(intent, files, tickets, identities)
            os._exit(73)

        # Lose the process after the durable plan, before any ticket claim.
        with mock.patch.object(bulk.BatchIntent, "begin", stopped):
            stop_process(lambda: self.execute(ledger, definition))
        intent_path = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "style-batch-intent.json"))
        record = bulk.object_fields(cast(object, json.loads(intent_path.read_text())))
        planned = cast(list[str], record["tickets"])
        ticket = planned[0]
        finding = findings[0]
        self.assertIsInstance(finding.state, ledger_api.Filed)
        assert isinstance(finding.state, ledger_api.Filed)
        # An independent ledger connection takes the still-unreserved ID.
        other = ledger_api.Ledger(self.repo, ledger.database)
        self.addCleanup(other.close)
        other.ticket_add(ticket, ticket_owner_id("other"), "proposal", "names", ".",
                         [finding.path], [finding.rule], proposal=str(finding.state.proposal),
                         finding_ids=(finding.finding_id,))
        before = next(item for item in other.proposal_tickets() if item["id"] == ticket)
        output = StringIO()
        with redirect_stdout(output):
            _ = self.execute(ledger, definition)
        after = next(item for item in other.proposal_tickets() if item["id"] == ticket)
        self.assertEqual(after, before)
        self.assertEqual(other.named_held((finding.finding_id,))[0].build_ticket, ledger_api.BuildTicket(ticket))
        self.assertIn(ticket, output.getvalue())

    def test_recovery_names_another_users_read_ticket_after_plan_record(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        begin = bulk.BatchIntent.begin

        def stopped(intent: bulk.BatchIntent, files: tuple[bulk.FileRenames, ...],
                    tickets: Sequence[str] = (), identities: Sequence[str] = ()) -> None:
            begin(intent, files, tickets, identities)
            os._exit(73)

        with mock.patch.object(bulk.BatchIntent, "begin", stopped):
            stop_process(lambda: self.execute(ledger, definition))
        intent_path = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "style-batch-intent.json"))
        record = bulk.object_fields(cast(object, json.loads(intent_path.read_text())))
        ticket = cast(list[str], record["tickets"])[0]
        unit = "other"
        with ledger_api.Ledger(self.repo, ledger.database) as other:
            other.ticket_add(ticket, ticket_owner_id(unit), "read", "names", ".", [findings[0].path], [definition.rule_id])
            before = other.queued_tickets(ticket_owner_id(unit))[0]
            output = StringIO()
            with redirect_stdout(output):
                _ = self.execute(ledger, definition)
            self.assertEqual(other.queued_tickets(ticket_owner_id(unit))[0], before)
            self.assertIn(ticket, output.getvalue())

    def test_workspace_recovers_claim_committed_before_analysis(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        add = ledger_api.Ledger.ticket_add

        def stopped(instance: ledger_api.Ledger, ticket: str, unit: str, kind: ledger_api.TicketKind,
                    family: str, crate: str, files: list[str], rule_ids: list[str], *,
                    proposal: str, finding_ids: tuple[str, ...],
                    batch_owner: ledger_api.BatchOwner | ledger_api.UnbatchedTicket) -> None:
            # The fault is after ticket_add's commit, before claim() can record its return.
            add(instance, ticket, unit, kind, family, crate, files, rule_ids,
                proposal=proposal, finding_ids=finding_ids, batch_owner=batch_owner)
            os._exit(73)

        with mock.patch.object(ledger_api.Ledger, "ticket_add", stopped):
            stop_process(lambda: self.execute(ledger, definition))
        interrupted = {ticket["id"] for ticket in ledger.proposal_tickets()
                       if ticket["state"] not in ledger_api.FINAL_TICKET_STATES}
        self.assertTrue(interrupted)
        report = self.execute(ledger, definition)
        self.assertEqual(report["applied"], len(findings))
        self.assertTrue(all(ticket["state"] in ledger_api.FINAL_TICKET_STATES
                            for ticket in ledger.proposal_tickets() if ticket["id"] in interrupted))
        self.assertTrue(all(ticket["checkpoint"] for ticket in ledger.proposal_tickets()
                            if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))

    def test_workspace_reruns_after_process_loss_on_first_edit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        stop_after_edit(self.repo / "src/lib.rs", lambda: self.execute(ledger, definition))
        self.assertTrue(ledger.proposal_tickets())
        report = self.execute(ledger, definition)
        self.assertEqual(report["applied"], len(findings))
        self.assertEqual(report["refused"], 0)
        self.assertEqual(report["reverted"], 0)
        self.assertTrue(all(ticket["checkpoint"] for ticket in ledger.proposal_tickets()
                            if ticket["id"] in cast(list[str], report["tickets"])))

    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root / "gate-home"),
            "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
        }))
        _ = self.enterContext(mock.patch.object(style_sync, "gate_owner", return_value="sample-style"))

        _ = self.git("update-index", "--refresh")
        self.runner = FakeRunner()
        self.server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))

    def crate_cli(self, ledger: ledger_api.Ledger, arguments: list[str]) -> int:
        owned_run = run.run_crates
        def crates(repo: Path, packages: Sequence[str], run_id: str, rule: str,
                   base_tree: str, destination: str, authority: ledger_api.Ledger) -> int:
            return owned_run(repo, packages, run_id, rule, base_tree, destination, authority,
                             self.server, self.runner, self.available)
        with mock.patch.object(run, "socket", SimpleNamespace(gethostname=lambda: "natedev")), mock.patch.object(
                ledger_api, "open_ledger", return_value=nullcontext(ledger)), mock.patch.object(
                run, "run_crates", side_effect=crates), redirect_stdout(StringIO()):
            return run.main(["--repo", str(self.repo), *arguments])

    def test_crate_cli_runs_lifecycle_and_resume_advances_second_crate_without_selection(self) -> None:
        from scripts.style import crate_run

        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        destination = "refs/heads/cli-destination"
        _ = self.git("update-ref", destination, self.base)
        batch = crate_run.CrateRun(ledger, "cli")
        end_workspaces = copies.end_workspaces

        def remove_under_run_lock(task: str, paths: list[Path], *,
                                  run_lock: copies.CrateRunRemovalLock = copies.ACQUIRE_CRATE_RUN_LOCK) -> None:
            with (batch.folder / "run.lock").open("a") as second:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                end_workspaces(task, paths, run_lock=run_lock)

        with mock.patch.object(run, "execute_plan", wraps=run.execute_plan) as execute:
            with mock.patch.object(copies, "end_workspaces", side_effect=remove_under_run_lock):
                self.assertEqual(self.crate_cli(ledger, ["--crate", "bulk_sample", "--run", "cli",
                                 "--rule", definition.rule_id, "--base-tree", self.tree,
                                 "--destination", destination]), 0)
            batch = crate_run.CrateRun(ledger, "cli")
            first = batch.read()["crates"]["bulk_sample"]
            self.assertEqual(first["stage"], crate_run.CrateStage.INTEGRATED)
            self.assertFalse(Path(first["checkout"]).exists())
            self.assertEqual(batch.read()["crates"]["beta"]["stage"], crate_run.CrateStage.SELECTED)
            with self.subTest("terminal resume removes a remaining copy"):
                with copies.workspace(batch.owner, self.repo, self.base, name=Path(first["checkout"]).name,
                                      branch=first["branch"], task=batch.task, run_manifest=batch.path) as remaining:
                    self.assertEqual(str(remaining), first["checkout"])
                self.assertTrue(remaining.exists())
                with mock.patch.object(batch, "frozen_checkout", side_effect=AssertionError("terminal crate reopened")):
                    self.assertEqual(batch.advance("bulk_sample"), batch.read())
                self.assertFalse(remaining.exists())
            with mock.patch.object(run, "select", side_effect=AssertionError("no reselection")):
                self.assertEqual(self.crate_cli(ledger, ["--crate", "beta", "--run", "cli"]), 0)
            self.assertEqual(execute.call_count, 2)
        result = batch.read()
        for record in result["crates"].values():
            self.assertEqual(record["stage"], crate_run.CrateStage.INTEGRATED)
            self.assertFalse(Path(record["checkout"]).exists())
            checkpoint = batch.result(record)["commit"]
            self.assertEqual(self.git("merge-base", checkpoint, destination), checkpoint)
            self.assertEqual(self.git("rev-parse", record["branch"]), checkpoint)
        for path in ("src/lib.rs", "beta/src/lib.rs"):
            self.assertIn("let number", self.git("show", destination + ":" + path))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_repeated_crate_cli_arguments_advance_serially_in_one_frozen_run(self) -> None:
        from scripts.style import crate_run

        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        destination = "refs/heads/cli-destination"
        _ = self.git("update-ref", destination, self.base)
        with mock.patch.object(run, "select", wraps=run.select) as select:
            self.assertEqual(self.crate_cli(ledger, ["--crate", "bulk_sample", "--crate", "beta",
                             "--run", "sequence", "--rule", definition.rule_id,
                             "--base-tree", self.tree, "--destination", destination]), 0)
            self.assertEqual(select.call_count, 1)
        result = crate_run.CrateRun(ledger, "sequence").read()
        self.assertTrue(all(record["stage"] == crate_run.CrateStage.INTEGRATED
                            for record in result["crates"].values()))
        self.assertTrue(all(not Path(record["checkout"]).exists() for record in result["crates"].values()))
        self.doCleanups()
        with self.subTest(failed_crate_copy="adopted"):
            case = WorkspaceRunTests(methodName="runTest")
            case.setUp()
            try:
                failed_ledger, failed_definition, _ = case.setup_findings()
                case.addCleanup(failed_ledger.close)
                _ = case.git("update-ref", destination, case.base)
                batch = crate_run.CrateRun(failed_ledger, "failed")
                batch.create(failed_definition, case.tree, destination)
                manifest = batch.read()
                record = manifest["crates"]["bulk_sample"]
                adopted = case.root / "adopted" / "worker"
                adopted.mkdir(parents=True)
                copies.initialize_git(adopted, case.repo)
                _ = run.git(adopted, "checkout", "-q", "-B", record["branch"].removeprefix("refs/heads/"), case.base)
                record["checkout"] = str(adopted)
                record["intent"] = str(adopted / ".git/style-batch-intent.json")
                batch.save(manifest)
                case.runner.red_tests = True
                self.assertEqual(case.crate_cli(failed_ledger, ["--crate", "bulk_sample", "--run", "failed",
                                 "--rule", failed_definition.rule_id, "--base-tree", case.tree,
                                 "--destination", destination]), 1)
                batch = crate_run.CrateRun(failed_ledger, "failed")
                failed = batch.read()["crates"]["bulk_sample"]
                self.assertEqual(failed["stage"], crate_run.CrateStage.ABANDONED)
                self.assertFalse(Path(failed["checkout"]).exists())
                self.assertEqual(batch.read()["crates"]["beta"]["stage"], crate_run.CrateStage.SELECTED)
                with mock.patch.object(batch, "frozen_checkout", side_effect=AssertionError("terminal crate reopened")):
                    self.assertEqual(batch.advance("bulk_sample"), batch.read())
                self.assertFalse(Path(failed["checkout"]).exists())
                case.runner.red_tests = False
                self.assertEqual(case.crate_cli(failed_ledger, ["--crate", "beta", "--run", "failed"]), 0)
                self.assertEqual(batch.read()["crates"]["beta"]["stage"], crate_run.CrateStage.INTEGRATED)
                self.assertTrue(all(not Path(record["checkout"]).exists() for record in batch.read()["crates"].values()))
            finally:
                case.doCleanups()

    def test_cli_without_crate_uses_unchanged_workspace_run(self) -> None:
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        owned_run = run.run_rule
        def workspace(repo: Path, rule: str, base_tree: str, authority: ledger_api.Ledger) -> Path:
            return owned_run(repo, rule, base_tree, authority, self.server, self.runner, self.available)
        with mock.patch.object(run, "socket", SimpleNamespace(gethostname=lambda: "natedev")), mock.patch.object(
                ledger_api, "open_ledger", return_value=nullcontext(ledger)), mock.patch.object(
                run, "run_rule", side_effect=workspace) as invoked, mock.patch.object(
                run, "run_crates", side_effect=AssertionError("workspace does not enter crate lifecycle")), redirect_stdout(StringIO()):
            self.assertEqual(run.main(["--repo", str(self.repo), "--rule", definition.rule_id,
                                       "--base-tree", self.tree]), 0)
            invoked.assert_called_once_with(self.repo, definition.rule_id, self.tree, ledger)
        self.assertEqual(self.git("rev-list", "--count", self.base + "..HEAD"), "1")
        self.assertFalse((ledger.database.parent / "crate_runs").exists())
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == "bash"],
                         ["final", "lint", "final --saved"])

    def test_doc_parameter_and_derive_field_share_one_tested_rename_commit(self) -> None:
        source = '/// Add `old`.\nfn run(old: usize) -> usize { old + 1 }\n'
        derived = '#[derive(serde::Serialize)]\nstruct R { old: usize }\n'
        _ = (self.repo / 'src/lib.rs').write_text(source)
        _ = (self.repo / 'beta/src/lib.rs').write_text(derived)
        _ = (self.repo / 'README.md').write_text('run takes `old`.\n')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'documented and derived fixture')
        self.base, self.tree = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        self.server = self.derive_server(lambda text, offset, new: word_edits(text, 'old', new))
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 0, 0))
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), source.replace('old', 'number'))
        self.assertIn('#[serde(rename = "old")] number:', (self.repo / 'beta/src/lib.rs').read_text())
        self.assertEqual((self.repo / 'README.md').read_text(), 'run takes `old`.\n')
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final --saved'])

    def test_metadata_only_rule_edit_selects_approved_findings_but_body_edit_is_stale(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        _ = subprocess.run(["git", "init", "-q", str(definition.path.parent.parent)], check=True)
        _ = subprocess.run(["git", "-C", str(definition.path.parent.parent), "hash-object", "-w", str(definition.path)],
                           check=True, capture_output=True)
        text = definition.path.read_text()
        _ = definition.path.write_text(text.replace("---\n", "---\napproval: always\n", 1))
        current = next(rule for rule in rules.load_rules(self.repo) if rule.rule_id == definition.rule_id)
        self.assertEqual({f.finding_id for f in run.select(ledger, current).findings}, {f.finding_id for f in findings})
        _ = definition.path.write_text(definition.path.read_text() + "Changed requirement\n")
        changed = next(rule for rule in rules.load_rules(self.repo) if rule.rule_id == definition.rule_id)
        selected = run.select(ledger, changed)
        self.assertFalse(selected.findings)
        self.assertEqual({item.reason for item in selected.skipped}, {"rule changed since judged"})

    def setup_findings(self, *, shared_proposal: bool = False) -> tuple[ledger_api.Ledger, rules.Rule, tuple[ledger_api.HeldFinding, ...]]:
        ledger = self.ledger()
        ledger.set_tip(self.base)
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.name == "prefer-type-named-fields-and-bindings")
        paths = ["src/lib.rs", "beta/src/lib.rs"]
        ledger.ticket_add("1001", ticket_owner_id("read"), "read", "names", ".", paths, [definition.rule_id])
        ledger.ticket_assign("1001", "main")
        _ = ledger.ticket_base("1001")
        findings: list[ledger_api.HeldFinding] = []
        for index, path in enumerate(paths):
            issue = self.root / ("decision.md" if shared_proposal else f"decision-{index}.md")
            _ = issue.write_text(f'---\nunit: {unit_staff_id("names")}\nplan: /tmp/standing-names.md\nstatus: open\n---\nApproved rename\n')
            finding = ledger.hold(definition.rule_id, path, 2, (self.repo / path).read_text().splitlines()[1],
                                  "rename", "old", "number", definition.blob, "1001")
            ledger.file_held(finding.finding_id, issue)
            findings.extend(ledger.named_held((finding.finding_id,)))
        ledger.ticket_drop("1001")
        return ledger, definition, tuple(findings)

    def test_queue_refusal_keeps_workspace_tickets_without_finding_dispositions(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        config = self.root / "lint.conf"
        key = "program_first_peak_gib.cargo"
        self.queue.refusals["cargo"] = f"{config}: {key} is missing or invalid"
        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
            _ = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                             self.server, run.run_command, self.available)
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn(str(config), errors.getvalue())
        self.assertIn(key, errors.getvalue())
        current = ledger.named_held(item.finding_id for item in findings)
        self.assertEqual(len(current), len(findings))
        tickets: set[str] = set()
        for finding in current:
            self.assertIsInstance(finding.build_ticket, ledger_api.BuildTicket)
            assert isinstance(finding.build_ticket, ledger_api.BuildTicket)
            tickets.add(finding.build_ticket.ticket)
            self.assertIsInstance(finding.state, ledger_api.Filed)
        self.assertTrue(tickets)
        for ticket in tickets:
            self.assertEqual(ledger.bulk_refusals(ticket), ())
        self.assertTrue(all(ticket["state"] == "assigned" for ticket in ledger.proposal_tickets()
                            if ticket["id"] in tickets))
        self.assertFalse((ledger.database.parent / "bulk_review").exists())
        self.assertEqual(self.queue.places, [])

    def available(self, _repo: Path, _path: str) -> None:
        pass

    def check_repeated_queue_refusal(self, values: bool) -> None:
        ledger, definition, findings = self.setup_values() if values else self.setup_findings()
        self.addCleanup(ledger.close)
        install_refusal_tools(self.root)
        config = self.root / "lint.conf"
        _ = config.write_text("[memory]\n")
        key = "program_first_peak_gib.cargo"
        sources = {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*.rs")}
        formatted: list[str] = []

        def refuse_check(repo: Path, arguments: Sequence[str]) -> None:
            if "check" in arguments and key not in config.read_text():
                changed = [path for path in repo.rglob("*.rs") if path.read_bytes().startswith(b"\n")]
                self.assertTrue(changed)
                formatted.extend(str(path.relative_to(repo)) for path in changed)
                raise queue_place.QueuePlaceRefused(f"{config}: {key} is missing or invalid")

        self.queue.before_start = refuse_check
        intent = run.WorkspaceIntent(self.repo,
            f"bulk_run --rule {definition.rule_id} --base-tree {self.tree} --repo {self.repo}")
        ticket_records: tuple[ledger_api.TicketRecord, ...] = ()
        claims: tuple[ledger_api.HeldFinding, ...] = ()
        ownership: dict[str, object] = {}
        for attempt in range(2):
            with ledger_api.Ledger(self.repo, ledger.database) as fresh:
                with redirect_stderr(StringIO()), self.assertRaises(bulk.StyleAdmissionStopped) as stopped:
                    _ = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                     self.server, run.run_command, self.available)
                self.assertEqual(stopped.exception.code, 2)
                current_tickets = fresh.proposal_tickets()
                current_claims = fresh.named_held(item.finding_id for item in findings)
                journal = bulk.object_fields(cast(object, json.loads(intent.path.read_text())))
                current_ownership = {name: journal[name] for name in
                                     ("batch", "operation", "base", "branch", "tickets", "findings", "completed_claims")}
                self.assertIs(journal["admission_stopped"], True)
                if attempt == 0:
                    ticket_records, claims, ownership = current_tickets, current_claims, current_ownership
                else:
                    self.assertEqual(current_tickets, ticket_records)
                    self.assertEqual(current_claims, claims)
                    self.assertEqual(current_ownership, ownership)
                for ticket in current_tickets:
                    self.assertEqual(fresh.bulk_refusals(ticket["id"]), ())
            self.assertEqual({str(path.relative_to(self.repo)): path.read_bytes()
                              for path in self.repo.rglob("*.rs")}, sources)
            self.assertTrue(all(place.closed for place in self.queue.places))
        if values:
            self.assertIn("src/constants.rs", formatted)
            self.assertFalse((self.repo / "src/constants.rs").exists())
        _ = config.write_text(f"[memory]\n{key}=12\n")
        with ledger_api.Ledger(self.repo, ledger.database) as fresh:
            with (mock.patch.object(run, "select", side_effect=AssertionError("retry must reuse selection")),
                  mock.patch.object(run, "claim", side_effect=AssertionError("retry must reuse claims"))):
                path = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                    self.server, run.run_command, self.available)
            report = bulk.object_fields(cast(object, json.loads(path.read_text())))
            self.assertEqual(report["applied"], 2)
            self.assertEqual(set(cast(list[str], report["tickets"])), set(cast(list[str], ownership["tickets"])))
        self.assertFalse(intent.path.exists())

    def test_workspace_repeated_refusal_reuses_completed_claims(self) -> None:
        self.check_repeated_queue_refusal(False)

    def test_workspace_repeated_refusal_restores_formatted_creations(self) -> None:
        self.check_repeated_queue_refusal(True)

    def test_interrupted_admission_restarts_with_original_claims(self) -> None:
        for case in ("recovery_interrupt", "cleanup_interrupt", "analyzer_refusal"):
            with self.subTest(case=case):
                fixture = WorkspaceRunTests(methodName="runTest")
                try:
                    fixture.setUp()
                    fixture.check_interrupted_admission_restart(case)
                finally:
                    _ = fixture.doCleanups()

    def check_interrupted_admission_restart(self, case: str) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        sources = {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*.rs")}
        intent = run.WorkspaceIntent(self.repo,
            f"bulk_run --rule {definition.rule_id} --base-tree {self.tree} --repo {self.repo}")
        ticket_records: tuple[ledger_api.TicketRecord, ...] = ()
        claims: tuple[ledger_api.HeldFinding, ...] = ()
        ownership: dict[str, object] = {}
        refused = False
        interrupted = False
        authority = ledger

        def journal_ownership() -> dict[str, object]:
            journal = bulk.object_fields(cast(object, json.loads(intent.path.read_text())))
            return {name: journal[name] for name in
                    ("batch", "operation", "base", "branch", "tickets", "findings", "completed_claims", "finding_inputs")}

        def refuse_start(_repo: Path, arguments: Sequence[str]) -> None:
            nonlocal refused, ticket_records, claims, ownership
            program = "rust-analyzer" if case == "analyzer_refusal" else "cargo"
            self.assertEqual(arguments[0], program)
            if not refused:
                ticket_records = authority.proposal_tickets()
                claims = authority.named_held(finding.finding_id for finding in findings)
                ownership = journal_ownership()
                self.assertTrue(ticket_records)
            else:
                self.assertEqual(authority.proposal_tickets(), ticket_records)
                self.assertEqual(authority.named_held(finding.finding_id for finding in findings), claims)
                self.assertEqual(journal_ownership(), ownership)
            refused = True
            raise queue_place.QueuePlaceRefused("place lost before child start")

        recover = run.WorkspaceIntent.recover

        def interrupt_recovery(current: run.WorkspaceIntent) -> tuple[str, ...]:
            nonlocal interrupted
            if case == "recovery_interrupt" and refused and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return recover(current)

        def interrupt_cleanup() -> None:
            nonlocal interrupted
            if case == "cleanup_interrupt" and not interrupted:
                interrupted = True
                raise SystemExit(130)

        self.queue.before_start = refuse_start
        self.queue.after_close = interrupt_cleanup
        server = run.RenameService.ON_DEMAND if case == "analyzer_refusal" else self.server
        expected = (KeyboardInterrupt if case == "recovery_interrupt" else
                    SystemExit if case == "cleanup_interrupt" else bulk.StyleAdmissionStopped)
        with mock.patch.object(run.WorkspaceIntent, "recover", new=interrupt_recovery), redirect_stderr(StringIO()):
            with ledger_api.Ledger(self.repo, ledger.database) as fresh:
                authority = fresh
                with self.assertRaises(expected) as stopped:
                    _ = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                     server, run.run_command, self.available)
                if isinstance(stopped.exception, SystemExit):
                    self.assertEqual(stopped.exception.code, 130 if case == "cleanup_interrupt" else 2)
                self.assertTrue(refused)
                self.assertEqual(fresh.proposal_tickets(), ticket_records)
                self.assertEqual(fresh.named_held(finding.finding_id for finding in findings), claims)
                self.assertEqual(journal_ownership(), ownership)
            for succeeds in (False, True):
                if succeeds:
                    self.queue.before_start = lambda _repo, _arguments: None
                with (ledger_api.Ledger(self.repo, ledger.database) as fresh,
                      mock.patch.object(run, "select", side_effect=AssertionError("restart selected findings")),
                      mock.patch.object(run, "claim", side_effect=AssertionError("restart claimed tickets")),
                      mock.patch.object(fresh, "ticket_drop_batch", side_effect=AssertionError("restart dropped a batch")),
                      mock.patch.object(fresh, "ticket_drop", side_effect=AssertionError("restart dropped a ticket"))):
                    authority = fresh
                    if not succeeds:
                        with self.assertRaises(bulk.StyleAdmissionStopped):
                            _ = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                             server, run.run_command, self.available)
                        self.assertEqual(fresh.proposal_tickets(), ticket_records)
                        self.assertEqual(fresh.named_held(finding.finding_id for finding in findings), claims)
                        self.assertEqual(journal_ownership(), ownership)
                        self.assertEqual({str(path.relative_to(self.repo)): path.read_bytes()
                                          for path in self.repo.rglob("*.rs")}, sources)
                    else:
                        report_path = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                                   self.server, self.runner, self.available)
                        report = bulk.object_fields(cast(object, json.loads(report_path.read_text())))
                        self.assertEqual(report["applied"], 2)
                        self.assertEqual(set(cast(list[str], report["tickets"])),
                                         {ticket["id"] for ticket in ticket_records})
                        owner = ledger_api.BatchOwner(str(ownership["batch"]))
                        self.assertTrue(all(fresh.ticket_batch_owner(ticket["id"]) == owner for ticket in ticket_records))
        self.assertTrue(all(place.closed for place in self.queue.places))
        self.assertFalse(intent.path.exists())

    def test_admission_window_preserves_only_admission_interrupts(self) -> None:
        seed = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="admission-window-seed-"))) / "case"
        # Keep recorded paths valid when each case restores the private staff and repository.
        _ = shutil.copytree(self.root, seed, symlinks=True)
        for case in ("cleanup_interrupt", "recovery_interrupt", "startup_interrupt", "tested_interrupt"):
            with self.subTest(case=case):
                shutil.rmtree(self.root)
                _ = shutil.copytree(seed, self.root, symlinks=True)
                fixture = WorkspaceRunTests(methodName="runTest")
                fixture.root = self.root
                fixture.repo = self.repo
                fixture.runner = FakeRunner()
                fixture.server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
                try:
                    fixture.queue = fixture.enterContext(unqueued_style_children())
                    _ = fixture.git("update-index", "--refresh")
                    fixture.check_admission_window(case)
                finally:
                    _ = fixture.doCleanups()

    def check_admission_window(self, case: str) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        install_refusal_tools(self.root)
        self.queue.real_startup = True
        self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "0"}))
        sources = {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*.rs")}
        intent = run.WorkspaceIntent(self.repo,
            f"bulk_run --rule {definition.rule_id} --base-tree {self.tree} --repo {self.repo}")
        tickets: tuple[ledger_api.TicketRecord, ...] = ()
        claims: tuple[ledger_api.HeldFinding, ...] = ()
        ownership: dict[str, object] = {}
        authority = ledger
        refused = False
        interrupted = False
        allow = case == "startup_interrupt"

        def journal_owner() -> dict[str, object]:
            journal = bulk.object_fields(cast(object, json.loads(intent.path.read_text())))
            return {key: journal[key] for key in
                    ("batch", "operation", "base", "branch", "tickets", "findings", "completed_claims", "finding_inputs")}

        def before_start(_repo: Path, arguments: Sequence[str]) -> None:
            nonlocal tickets, claims, ownership, refused
            if not tickets:
                tickets = authority.proposal_tickets()
                claims = authority.named_held(finding.finding_id for finding in findings)
                ownership = journal_owner()
                self.assertTrue(tickets)
            if not allow and "check" in arguments:
                refused = True
                raise queue_place.QueuePlaceRefused("controlled refusal after formatting")

        def before_started() -> None:
            nonlocal interrupted
            if case == "startup_interrupt" and not interrupted:
                interrupted = True
                signal.raise_signal(signal.SIGINT)

        def after_close() -> None:
            nonlocal interrupted
            if case == "cleanup_interrupt" and refused and not interrupted:
                interrupted = True
                raise SystemExit(130)

        recover = run.WorkspaceIntent.recover

        def recover_attempt(current: run.WorkspaceIntent) -> tuple[str, ...]:
            nonlocal interrupted
            if case == "recovery_interrupt" and refused and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return recover(current)

        plan_rule = run.plan_rule

        def plan_without_marker(repo: Path, authority: ledger_api.Ledger, base: str,
                                selected: run.Selection, rule: rules.Rule,
                                server: bulk.RenameServer | run.RenameService,
                                available: Callable[[Path, str], None]) -> run.PlannedEdits:
            self.assertFalse(intent.retry_eligible())
            return plan_rule(repo, authority, base, selected, rule, server, available)

        _ = self.enterContext(mock.patch.object(run, "plan_rule", new=plan_without_marker))
        self.queue.before_start = before_start
        self.queue.before_started = before_started
        self.queue.after_close = after_close
        expected = (bulk.StyleAdmissionStopped if case == "tested_interrupt" else
                    SystemExit if case == "cleanup_interrupt" else KeyboardInterrupt)
        with mock.patch.object(run.WorkspaceIntent, "recover", new=recover_attempt), redirect_stderr(StringIO()):
            with ledger_api.Ledger(self.repo, ledger.database) as fresh:
                authority = fresh
                with self.assertRaises((KeyboardInterrupt, SystemExit)) as stopped:
                    _ = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                     self.server, run.run_command, self.available)
                self.assertIsInstance(stopped.exception, expected)
                if isinstance(stopped.exception, SystemExit):
                    self.assertEqual(stopped.exception.code, 2 if case == "tested_interrupt" else 130)
                self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
                if case != "startup_interrupt":
                    self.assertEqual(fresh.proposal_tickets(), tickets)
                    self.assertEqual(fresh.named_held(finding.finding_id for finding in findings), claims)
                    self.assertEqual(journal_owner(), ownership)
                    self.assertTrue(intent.retry_eligible())
                    if case != "recovery_interrupt":
                        self.assertEqual({str(path.relative_to(self.repo)): path.read_bytes()
                                          for path in self.repo.rglob("*.rs")}, sources)
            if case != "startup_interrupt":
                allow = True
                with (ledger_api.Ledger(self.repo, ledger.database) as fresh,
                      mock.patch.object(run, "select", side_effect=AssertionError("restart selected findings")),
                      mock.patch.object(run, "claim", side_effect=AssertionError("restart claimed tickets"))):
                    authority = fresh
                    def test_tree(repo: Path, _commit: str, _tree: str, runner: run.Runner,
                                  times: dict[str, float], *, owner: str = "") -> run.CommandResult:
                        self.assertNotEqual(run.git(repo, "rev-parse", "HEAD"), self.base)
                        self.assertFalse(intent.retry_eligible())
                        self.assertEqual(journal_owner(), ownership)
                        if case == "tested_interrupt":
                            raise KeyboardInterrupt()
                        return original_test_tree(repo, _commit, _tree, runner, times, owner=owner)
                    original_test_tree = run.test_tree
                    with mock.patch.object(run, "test_tree", new=test_tree):
                        if case == "tested_interrupt":
                            with self.assertRaises(KeyboardInterrupt):
                                _ = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                                 self.server, run.run_command, self.available)
                        else:
                            with (mock.patch.object(fresh, "ticket_drop_batch", side_effect=AssertionError("dropped batch")),
                                  mock.patch.object(fresh, "ticket_drop", side_effect=AssertionError("dropped ticket"))):
                                path = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                                    self.server, run.run_command, self.available)
                            report = bulk.object_fields(cast(object, json.loads(path.read_text())))
                            self.assertEqual(report["applied"], 2)
                            self.assertEqual(set(cast(list[str], report["tickets"])), {ticket["id"] for ticket in tickets})
                            owner = ledger_api.BatchOwner(str(ownership["batch"]))
                            self.assertTrue(all(fresh.ticket_batch_owner(ticket["id"]) == owner for ticket in tickets))
                            self.assertNotEqual(self.git("rev-parse", "HEAD"), self.base)
            if case in {"startup_interrupt", "tested_interrupt"}:
                with ledger_api.Ledger(self.repo, ledger.database) as fresh:
                    authority = fresh
                    self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
                    self.assertEqual({str(path.relative_to(self.repo)): path.read_bytes()
                                      for path in self.repo.rglob("*.rs")}, sources)
                    records = {ticket["id"]: ticket for ticket in fresh.proposal_tickets()}
                    self.assertTrue(all(records[ticket["id"]]["state"] in ledger_api.FINAL_TICKET_STATES for ticket in tickets))
                    self.assertTrue(all(isinstance(finding.build_ticket, ledger_api.NoBuildTicket)
                                        for finding in fresh.named_held(finding.finding_id for finding in findings)))
                    self.assertEqual(journal_owner(), ownership)
                    self.assertFalse(intent.retry_eligible())
                    with mock.patch.object(run, "select", return_value=run.Selection((), ())):
                        path = run.run_rule(self.repo, definition.rule_id, self.tree, fresh,
                                            self.server, run.run_command, self.available)
                    self.assertEqual(bulk.object_fields(cast(object, json.loads(path.read_text())))["applied"], 0)
        self.assertTrue(all(place.closed for place in self.queue.places))
        self.assertFalse(intent.path.exists())

    def test_refusal_from_popen_recovers_after_queue_cleanup(self) -> None:
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        original = (self.repo / "src/lib.rs").read_bytes()

        def refuse_start(_repo: Path, _arguments: Sequence[str]) -> None:
            self.assertIn(b"let number", (self.repo / "src/lib.rs").read_bytes())
            raise queue_place.QueuePlaceRefused("place lost before child start")

        self.queue.before_start = refuse_start
        with redirect_stderr(StringIO()), self.assertRaises(bulk.StyleAdmissionStopped):
            _ = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                             self.server, run.run_command, self.available)
        self.assertEqual(len(self.queue.places), 1)
        self.assertTrue(self.queue.places[0].closed)
        self.assertEqual(self.queue.places[0].processes, [])
        self.assertEqual((self.repo / "src/lib.rs").read_bytes(), original)

    def test_unknown_change_blocks_admission_recovery_without_releasing_claims(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        install_refusal_tools(self.root)
        unknown = b"pub fn outside_change() {}\n"

        def refuse_changed(repo: Path, arguments: Sequence[str]) -> None:
            if "check" in arguments:
                _ = (repo / "src/lib.rs").write_bytes(unknown)
                raise queue_place.QueuePlaceRefused("place lost after unknown change")

        self.queue.before_start = refuse_changed
        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(bulk.StyleAdmissionStopped):
            _ = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                             self.server, run.run_command, self.available)
        self.assertIn("bytes do not match the recorded edit", errors.getvalue())
        tickets = ledger.proposal_tickets()
        claims = ledger.named_held(item.finding_id for item in findings)
        intent = run.WorkspaceIntent(self.repo,
            f"bulk_run --rule {definition.rule_id} --base-tree {self.tree} --repo {self.repo}")
        self.assertTrue(intent.path.exists())
        with self.assertRaisesRegex(bulk.BulkRenameError, "bytes do not match the recorded edit"):
            _ = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                             self.server, run.run_command, self.available)
        self.assertEqual(ledger.proposal_tickets(), tickets)
        self.assertEqual(ledger.named_held(item.finding_id for item in findings), claims)
        self.assertEqual((self.repo / "src/lib.rs").read_bytes(), unknown)
        self.assertTrue(intent.path.exists())

    def execute(self, ledger: ledger_api.Ledger, definition: rules.Rule) -> dict[str, object]:
        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, self.runner,
                            available=self.available)
        return bulk.object_fields(cast(object, json.loads(path.read_text())))

    def approve_extra_rename(self, ledger: ledger_api.Ledger, definition: rules.Rule,
                             line: int, old: str, new: str, path: str = 'src/lib.rs') -> ledger_api.HeldFinding:
        ledger.ticket_add('14001', ticket_owner_id('read-extra'), 'read', 'names', '.', [path], [definition.rule_id])
        ledger.ticket_assign('14001', 'main')
        _ = ledger.ticket_base('14001')
        finding = ledger.hold(definition.rule_id, path, line,
                              (self.repo / path).read_text().splitlines()[line - 1],
                              'rename', old, new, definition.blob, '14001')
        proposal = self.root / 'decision-extra.md'
        _ = proposal.write_text(f'---\nunit: {unit_staff_id("names")}\nplan: /tmp/standing-names.md\nstatus: open\n---\nApproved extra rename\n')
        ledger.file_held(finding.finding_id, proposal)
        ledger.ticket_drop('14001')
        return finding

    def setup_field_reference(self, tail: str = '') -> tuple[ledger_api.Ledger, rules.Rule, tuple[ledger_api.HeldFinding, ...]]:
        source = 'struct Record {\n    old: usize,\n}\nmod child;\n' + tail
        child = self.repo / 'src/child.rs'
        child_source = 'fn read(r: super::Record) -> usize { r.old }\n'
        _ = (self.repo / 'src/lib.rs').write_text(source)
        _ = child.write_text(child_source)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'field references in another module')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, findings = self.setup_findings()
        rename = self.server.rename
        definitions = self.server.definitions

        def references(path: Path, text: str, offset: int, new: str) -> bulk.WorkspaceRename:
            result = rename(path, text, offset, new)
            if path == self.repo / 'src/lib.rs' and new == 'number':
                return bulk.WorkspaceRename((*result.documents, bulk.DocumentEdits(
                    child, child_source, word_edits(child_source, 'old', new))))
            return result

        def resolve(path: Path, text: str, offsets: tuple[int, ...]) -> tuple[bulk.DefinitionResolution, ...]:
            if path == child:
                target = bulk.DefinitionTarget((self.repo / 'src/lib.rs').as_uri(), (1, 4), (1, 10))
                return tuple(target for _ in offsets)
            return definitions(path, text, offsets)

        _ = self.enterContext(mock.patch.object(self.server, 'rename', side_effect=references))
        _ = self.enterContext(mock.patch.object(self.server, 'definitions', side_effect=resolve))
        return ledger, definition, findings

    def test_names_held_reference_refuses_only_its_edit_group(self) -> None:
        ledger, definition, findings = self.setup_field_reference()
        source = (self.repo / 'src/lib.rs').read_text()
        child = self.repo / 'src/child.rs'
        child_source = child.read_text()
        checked: Counter[str] = Counter()
        reason = 'src/child.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            checked[path] += 1
            if path == 'src/child.rs':
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self, 'available', side_effect=available):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id: reason})
        self.assertEqual(checked, {'src/lib.rs': 1, 'src/child.rs': 1, 'beta/src/lib.rs': 2})
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), source)
        self.assertEqual(child.read_text(), child_source)
        self.assertIn('let number', (self.repo / 'beta/src/lib.rs').read_text())
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final --saved'])
        self.assertIsInstance(ledger.named_held((findings[0].finding_id,))[0].state, ledger_api.Filed)

    def test_held_field_reference_preserves_an_independent_local_rename_in_the_same_file(self) -> None:
        ledger, definition, findings = self.setup_field_reference('fn extra() { let other = 2; consume(other); }\n')
        extra = self.approve_extra_rename(ledger, definition, 5, 'other', 'size')
        self.server.edit = lambda text, offset, new: word_edits(text, 'other' if new == 'size' else 'old', new)
        child = self.repo / 'src/child.rs'
        original = (self.repo / 'src/lib.rs').read_text()
        child_source = child.read_text()
        checked: Counter[str] = Counter()
        reason = 'src/child.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            checked[path] += 1
            if path == 'src/child.rs':
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self, 'available', side_effect=available):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id: reason})
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), original.replace('other', 'size'))
        self.assertEqual(child.read_text(), child_source)
        self.assertEqual(checked, {'src/lib.rs': 2, 'src/child.rs': 1, 'beta/src/lib.rs': 2})
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final --saved'])
        self.assertIsInstance(ledger.named_held((findings[0].finding_id,))[0].state, ledger_api.Filed)
        self.assertNotIn(extra.finding_id, cast(dict[str, str], report['refusals']))

    def test_later_rename_is_proved_without_a_held_traits_overlay(self) -> None:
        self.check_trait_overlay_rollback('planning')

    def test_formatter_rollback_reproves_and_refuses_a_new_capture(self) -> None:
        self.check_trait_overlay_rollback('formatting')

    def check_trait_overlay_rollback(self, stage: str) -> None:
        source = ('struct Record;\n'
                  'trait Named { fn old(&self); }\n'
                  'impl Named for Record { fn old(&self) {} }\n'
                  'mod methods;\n'
                  'mod child;\n'
                  'fn call(record: Record) { record.old(); }\n')
        root = self.repo / 'src/lib.rs'
        child = self.repo / 'src/child.rs'
        methods = self.repo / 'src/methods.rs'
        child_source = 'fn call<T: super::Named>(record: &T) { record.old(); }\n'
        methods_source = 'impl super::Record { fn other(&self) {} }\n'
        _ = root.write_text(source)
        _ = child.write_text(child_source)
        _ = methods.write_text(methods_source)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'trait and inherent method fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, findings = self.setup_findings()
        inherent = self.approve_extra_rename(ledger, definition, 1, 'other', 'old', 'src/methods.rs')
        named_held = ledger.named_held

        def trait_first(names: Iterable[str]) -> tuple[ledger_api.HeldFinding, ...]:
            # Finding hashes normally choose cross-file order. Exercise the dangerous
            # order explicitly: the held trait edit precedes the independent method edit.
            return tuple(sorted(named_held(names), key=lambda finding: finding.path != 'src/lib.rs'))

        original_rename = self.server.rename
        original_definitions = self.server.definitions
        checked_trees: list[str] = []

        def edits(text: str, _offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
            # The probe leaves the concrete call untouched, exercising destination-name proof.
            old = 'other' if new == 'old' else 'old'
            return tuple(edit for edit in word_edits(text, old, new)
                         if text.count('\n', 0, edit.start) != 5)

        self.server.edit = edits

        def references(path: Path, text: str, offset: int, new: str) -> bulk.WorkspaceRename:
            result = original_rename(path, text, offset, new)
            if path == root:
                return bulk.WorkspaceRename((*result.documents, bulk.DocumentEdits(
                    child, child_source, word_edits(child_source, 'old', new))))
            return result

        def resolve(path: Path, text: str, offsets: tuple[int, ...]) -> tuple[bulk.DefinitionResolution, ...]:
            if path not in {root, child, methods}:
                return original_definitions(path, text, offsets)
            current = text if path == root else self.server.documents[root]
            method = text if path == methods else self.server.documents.get(methods, methods_source)
            trait_name = 'number' if 'fn number' in current else 'old'
            method_name = 'old' if 'fn old' in method else 'other'
            trait_start = current.splitlines()[1].index('fn ' + trait_name) + 3
            trait = bulk.DefinitionTarget(root.as_uri(), (1, trait_start), (1, trait_start + len(trait_name)))
            method_start = method.index('fn ' + method_name) + 3
            own = bulk.DefinitionTarget(methods.as_uri(), (0, method_start), (0, method_start + len(method_name)))
            answers: list[bulk.DefinitionResolution] = []
            for offset in offsets:
                if path == root and text.count('\n', 0, offset) == 5:
                    checked_trees.append(current)
                    answers.append(own if method_name == 'old' or trait_name == 'number' else trait)
                else:
                    answers.append(own if path == methods else trait)
            return tuple(answers)

        reason = ('src/child.rs: another worktree holds the file' if stage == 'planning'
                  else 'post-format rename edit changed: src/lib.rs')
        self.runner.break_proof = stage == 'formatting'

        def available(_repo: Path, path: str) -> None:
            if stage == 'planning' and path == 'src/child.rs':
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self.server, 'rename', side_effect=references), mock.patch.object(
                self.server, 'definitions', side_effect=resolve), mock.patch.object(self, 'available', side_effect=available), mock.patch.object(
                ledger, 'named_held', side_effect=trait_first):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 2, 0))
        refusals = cast(dict[str, str], report['refusals'])
        self.assertEqual(refusals[findings[0].finding_id], reason)
        self.assertIn("capture in an unedited file for 'old'", refusals[inherent.finding_id])
        self.assertTrue(checked_trees)
        if stage == 'planning':
            self.assertTrue(all('trait Named { fn old' in text for text in checked_trees))
        else:
            self.assertTrue(any('trait Named { fn old' in text for text in checked_trees))
        self.assertEqual(root.read_text(), source)
        self.assertEqual(self.server.documents[root], source)
        self.assertEqual(methods.read_text(), methods_source)
        self.assertEqual(self.server.documents[methods], methods_source)
        self.assertEqual(child.read_text(), child_source)
        self.assertTrue(all(isinstance(row.state, ledger_api.Filed)
                            for row in ledger.named_held((findings[0].finding_id, inherent.finding_id))))

    def test_values_held_reference_refuses_the_whole_shared_target_group(self) -> None:
        ledger, definition, findings = self.setup_values(dual_target=True)
        third = next(finding for finding in findings if finding.path == 'src/worker.rs' and finding.line == 4)
        proposal = cast(ledger_api.Filed, third.state).proposal
        _ = proposal.write_text(proposal.read_text().replace('declined', 'approved'))
        before = {path: (self.repo / path).read_bytes() for path in ('src/lib.rs', 'src/main.rs', 'src/worker.rs')}
        checked: Counter[str] = Counter()
        reason = 'src/main.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            checked[path] += 1
            if path == 'src/main.rs':
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self, 'available', side_effect=available), mock.patch.object(
                bulk, 'RustAnalyzer', side_effect=AssertionError('values started rust-analyzer')):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 3, 0))
        held = {finding.finding_id for finding in findings if finding.path == 'src/worker.rs' and finding.line in (2, 4)}
        refusals = cast(dict[str, str], report['refusals'])
        self.assertEqual({identity: refusals[identity] for identity in held}, dict.fromkeys(held, reason))
        self.assertTrue(all(isinstance(row.state, ledger_api.Filed) for row in ledger.named_held(held)))
        self.assertEqual({path: (self.repo / path).read_bytes() for path in before}, before)
        self.assertFalse((self.repo / 'src/constants.rs').exists())
        self.assertIn('const FIRST: usize = 42', (self.repo / 'beta/src/constants.rs').read_text())
        self.assertTrue(all(count == (2 if path.startswith('beta/') else 1) for path, count in checked.items()))
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final --saved'])

    def test_values_hold_appearing_after_planning_still_aborts(self) -> None:
        ledger, definition, _ = self.setup_values()
        checked: Counter[str] = Counter()
        reason = 'src/constants.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            checked[path] += 1
            if path == 'src/constants.rs' and checked[path] == 2:
                raise bulk.BulkRenameError(reason)

        with mock.patch.object(self, 'available', side_effect=available):
            report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['reverted']), (0, 2))
        self.assertEqual(report['status'], 'batch refused: ' + reason)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('diff', self.base), '')
        self.assertFalse(self.runner.calls)
        self.assertFalse((self.repo / 'src/constants.rs').exists())

    def test_stale_lineage_names_the_ledger_reason(self) -> None:
        ledger, _, findings = self.setup_findings()
        finding = findings[0]
        for reason in ('item changed', 'recorded anchor commit is missing'):
            with self.subTest(reason=reason), mock.patch.object(
                    ledger, 'finding_site', return_value=ledger_api.NeedsJudgment(reason)):
                result = run.current_rename(ledger, finding, self.base, run.RenameSources(), self.repo)
                self.assertEqual(result, bulk.Refusal(finding.finding_id,
                    'changed since judged: ' + reason))

    def test_stale_unreadable_judged_file_names_its_cause(self) -> None:
        ledger, _, findings = self.setup_findings()
        finding = findings[0]
        with mock.patch.object(run.RenameSources, 'judged_file', return_value=run.CommandResult(1)):
            result = run.current_rename(ledger, finding, self.base, run.RenameSources(), self.repo)
        self.assertEqual(result, bulk.Refusal(finding.finding_id,
            'changed since judged: the file cannot be read at the judged commit'))

    def test_stale_physical_line_bytes_names_its_cause(self) -> None:
        ledger, _, findings = self.setup_findings()
        finding = findings[0]
        # Token-equivalent CRLF is still different judged physical line evidence.
        source = (self.repo / finding.path).read_bytes().decode().replace('\n', '\r\n')
        with mock.patch.object(run.RenameSources, 'judged_file', return_value=run.CommandResult(0, source)):
            result = run.current_rename(ledger, finding, self.base, run.RenameSources(), self.repo)
        self.assertEqual(result, bulk.Refusal(finding.finding_id,
            'changed since judged: the judged line bytes differ from the current line'))

    def test_stale_declaration_off_current_line_names_its_cause(self) -> None:
        ledger, _, findings = self.setup_findings()
        finding = findings[0]
        source = (self.repo / finding.path).read_text()
        declaration = rename_declaration(source, BindingRename(finding.line, finding.text, 'old', 'number'))
        self.assertNotIsInstance(declaration, NoBindingScope)
        assert not isinstance(declaration, NoBindingScope)
        with mock.patch.object(run, 'rename_declaration', return_value=replace(declaration, offset=0)):
            result = run.current_rename(ledger, finding, self.base, run.RenameSources(), self.repo)
        self.assertEqual(result, bulk.Refusal(finding.finding_id,
            'changed since judged: the declaration is not on the current line'))

    def setup_values(self, *, dual_target: bool = False) -> tuple[ledger_api.Ledger, rules.Rule, tuple[ledger_api.HeldFinding, ...]]:
        source = 'pub fn run() {\n    let first: usize = 42;\n    let second: usize = 43;\n    let third: usize = 44;\n}\n'
        for prefix in ('', 'beta/'):
            _ = (self.repo / (prefix + 'src/lib.rs')).write_text('mod worker;\n')
            _ = (self.repo / (prefix + 'src/worker.rs')).write_text(source)
        if dual_target:
            _ = (self.repo / 'src/main.rs').write_text('mod worker;\nfn main() {}\n')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'values fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        ledger = self.ledger()
        _ = (self.root / 'style/rust/no-magic-values.md').write_text(
            '---\ntier: B\nfamily: values\ninput_scope: file\npre_filter: magic-values\napproval: always\n---\n# Values\n')
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.name == 'no-magic-values')
        ledger.set_tip(self.base)
        paths = ['src/worker.rs', 'beta/src/worker.rs']
        ledger.ticket_add('11001', ticket_owner_id('values-read'), 'read', 'values', '.', paths, [definition.rule_id])
        ledger.ticket_assign('11001', 'main')
        _ = ledger.ticket_base('11001')
        identities: list[str] = []
        for index, (path, line, after, answer) in enumerate((
            (paths[0], 2, 'Add `const FIRST: usize = 42;` in constants.rs.', ''),
            (paths[1], 2, 'Add `const FIRST: usize = 42;` in constants.rs.', 'approved'),
            (paths[0], 3, 'Compute a better value instead.', ''),
            (paths[0], 4, 'Add `const THIRD: usize = 44;` in constants.rs.', 'declined'),
        )):
            issue = self.root / f'{44000 + index}.md'
            _ = issue.write_text('---\nstatus: open\n' + (f'style_answer: {answer}\n' if answer else '') + '---\nValues decision\n')
            finding = ledger.hold(definition.rule_id, path, line, source.splitlines()[line - 1],
                                  'restructure', str(40 + line), after, definition.blob, '11001')
            ledger.file_held(finding.finding_id, issue)
            identities.append(finding.finding_id)
        ledger.ticket_drop('11001')
        return ledger, definition, ledger.named_held(identities)

    def test_values_apply_in_one_tested_commit_and_keep_refusals_filed(self) -> None:
        ledger, definition, findings = self.setup_values()
        with (
            mock.patch.object(bulk, 'RustAnalyzer',
                              side_effect=AssertionError('values started rust-analyzer')),
            mock.patch.object(run, 'plan_rule', wraps=run.plan_rule) as plan,
        ):
            path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, runner=self.runner,
                                available=lambda repo, path: None)
        self.assertEqual(plan.call_count, 1)
        selected = cast(run.Selection, plan.call_args.args[3])
        # named_held returns finding-id order, not the fixture's declaration order.
        self.assertEqual({finding.finding_id for finding in selected.findings},
                         {finding.finding_id for finding in findings if finding.line != 4})
        self.assertEqual(selected.skipped, tuple(
            bulk.Refusal(finding.finding_id, 'decision not approved')
            for finding in findings if finding.line == 4))
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertIn('no exact constant', str(report['refusals']))
        self.assertEqual(report['skip_counts'], {'decision not approved': 1})
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final --saved'])
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertIn('checkpointed', str(report['status']))
        for prefix in ('', 'beta/'):
            self.assertIn('pub(super) const FIRST: usize = 42;', (self.repo / (prefix + 'src/constants.rs')).read_text())
            self.assertIn('let first: usize = FIRST;', (self.repo / (prefix + 'src/worker.rs')).read_text())
        self.assertTrue(all(row['family'] == 'values' for row in cast(list[dict[str, object]], report['counts'])))
        rows = ledger.named_held(finding.finding_id for finding in findings)
        refused = set(cast(dict[str, str], report['refusals']))
        for row in rows:
            if row.finding_id in refused:
                self.assertIsInstance(row.state, ledger_api.Filed)
                self.assertIsInstance(row.build_ticket, ledger_api.NoBuildTicket)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_values_diagnostics_revert_created_module_and_its_whole_extraction(self) -> None:
        ledger, definition, _ = self.setup_values()
        def fail_root(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if len(arguments) > 1 and arguments[1] in ('check', 'clippy') and (repo / 'src/constants.rs').exists():
                return run.CommandResult(1, json.dumps({'level': 'error', 'spans': [{'file_name': 'src/constants.rs'}]}))
            return result
        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, runner=fail_root,
                            available=lambda repo, path: None)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 2, 0))
        self.assertFalse((self.repo / 'src/constants.rs').exists())
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), 'mod worker;\n')
        self.assertIn('let first: usize = 42;', (self.repo / 'src/worker.rs').read_text())
        self.assertTrue((self.repo / 'beta/src/constants.rs').is_file())

    def test_values_formatter_drift_and_red_tests_restore_new_and_existing_files(self) -> None:
        ledger, definition, _ = self.setup_values()
        self.runner.break_proof = True
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['reverted']), (0, 2))
        self.assertIn('post-format constant proof', str(report['status']))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.runner = FakeRunner()
        self.runner.red_tests = True
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['reverted']), (0, 2))
        self.assertIn('tests failed', str(report['status']))
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse((self.repo / 'src/constants.rs').exists())

    def test_values_resume_after_process_loss_removes_recorded_creations(self) -> None:
        ledger, definition, _ = self.setup_values()
        stop_after_edit(self.repo / 'src/constants.rs', lambda: self.execute(ledger, definition))
        self.assertTrue((self.repo / 'src/constants.rs').is_file())
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertIn('checkpointed', str(report['status']))

    def test_values_shared_module_in_two_targets_is_proved_for_both_and_counted_once(self) -> None:
        ledger, definition, _ = self.setup_values(dual_target=True)
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertIn('mod constants;', (self.repo / 'src/lib.rs').read_text())
        self.assertIn('mod constants;', (self.repo / 'src/main.rs').read_text())
        self.assertIn('checkpointed', str(report['status']))

    def test_values_unproved_target_route_refuses_shared_worker(self) -> None:
        ledger, definition, findings = self.setup_values(dual_target=True)
        shared = next(finding for finding in findings if finding.path == 'src/worker.rs' and finding.line == 2)
        binary = ('#[path="worker.rs"] mod worker;\n'
                  + 'mod constants { pub(super) const FIRST: usize = 99; }\n'
                  + 'fn main() { worker::run(); }\n')
        _ = (self.repo / 'src/main.rs').write_text(binary)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'attributed binary instance with a different constant')
        self.base = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        ledger.set_tip(self.base)
        plan = run.plan_constants(self.repo, ledger, self.base, run.select(ledger, definition))
        self.assertNotIn('src/worker.rs', {edit.path for edit in plan.files})
        self.assertTrue(any(item.finding_id == shared.finding_id
                            and 'target src/main.rs' in item.reason and 'attributed module route' in item.reason
                            for item in plan.refusals), repr(plan.refusals))
        report = self.execute(ledger, definition)
        self.assertEqual(report['applied'], 1, 'the independently proved beta target still applies')
        self.assertIn(shared.finding_id, cast(dict[str, str], report['refusals']))
        self.assertIn('let first: usize = 42;', (self.repo / 'src/worker.rs').read_text())
        self.assertEqual((self.repo / 'src/main.rs').read_text(), binary)
        self.assertFalse((self.repo / 'src/constants.rs').exists())
        self.assertIsInstance(ledger.named_held((shared.finding_id,))[0].state, ledger_api.Filed)

    def test_values_dangling_creation_symlink_refuses_before_any_write(self) -> None:
        ledger, definition, _ = self.setup_values()
        outside = self.root / 'outside-constants.rs'
        link = self.repo / 'src/constants.rs'
        link.symlink_to(outside)
        report = self.execute(ledger, definition)
        self.assertEqual(report['applied'], 0)
        self.assertFalse(outside.exists(), 'a refused creation must never write outside the workspace')
        self.assertIn('symlink', str(report['status']))
        self.assertTrue(link.is_symlink(), 'rollback must preserve a symlink it did not create')
        self.assertEqual(link.readlink(), outside)
        self.assertFalse((self.repo / 'beta/src/constants.rs').exists())
        self.assertEqual(self.git('diff', self.base), '')
        self.assertFalse(self.runner.calls, 'preflight must precede formatting and validation')

    def test_values_rollback_removes_ignored_owned_creations(self) -> None:
        ledger, definition, findings = self.setup_values()
        exclude = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))
        _ = exclude.write_text(exclude.read_text() + '\nconstants.rs\n')
        self.runner.break_proof = True
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['reverted']), (0, 2))
        self.assertIn('post-format constant proof', str(report['status']))
        self.assertEqual(self.git('diff', self.base), '')
        for prefix in ('', 'beta/'):
            self.assertFalse((self.repo / (prefix + 'src/constants.rs')).exists())
            self.assertEqual((self.repo / (prefix + 'src/lib.rs')).read_text(), 'mod worker;\n')
        intent = run.WorkspaceIntent(self.repo, 'unused')
        self.assertFalse(intent.path.exists(), 'all owned files must be removed before clearing the journal')
        rows = ledger.named_held(item.finding_id for item in findings)
        self.assertTrue(all(isinstance(row.state, ledger_api.Filed) for row in rows))

    def test_values_preserve_preexisting_untracked_constant_file(self) -> None:
        ledger, definition, _ = self.setup_values()
        untracked = self.repo / 'src/constants.rs'
        _ = untracked.write_text('// user work\n')
        report = self.execute(ledger, definition)
        self.assertEqual(report['applied'], 0)
        self.assertIn('overwrite an untracked file', str(report['status']))
        self.assertEqual(untracked.read_text(), '// user work\n')
        self.assertEqual(self.git('diff', self.base), '')

    def test_values_recovery_refuses_modified_creation_without_restoring_any_other_file(self) -> None:
        ledger, definition, _ = self.setup_values()
        stop_after_edit(self.repo / 'src/constants.rs', lambda: self.execute(ledger, definition))
        _ = (self.repo / 'src/constants.rs').write_text('// independently changed\n')
        before = self.git('diff', self.base)
        with self.assertRaisesRegex(bulk.BulkRenameError, 'bytes do not match the recorded creation'):
            _ = self.execute(ledger, definition)
        self.assertEqual(self.git('diff', self.base), before)
        self.assertEqual((self.repo / 'src/constants.rs').read_text(), '// independently changed\n')

    def test_values_skip_a_file_held_by_another_open_ticket(self) -> None:
        ledger, definition, findings = self.setup_values()
        other = next(finding for finding in findings if finding.path.startswith('beta/'))
        assert isinstance(other.state, ledger_api.Filed)
        ledger.ticket_add('34001', ticket_owner_id('other-values'), 'proposal', 'values', 'beta', [other.path],
                          [definition.rule_id], proposal=str(other.state.proposal), finding_ids=(other.finding_id,))
        report = self.execute(ledger, definition)
        self.assertEqual((report['applied'], report['refused']), (1, 1))
        self.assertIn('held by another open ticket', str(report['skip_counts']))
        self.assertFalse((self.repo / 'beta/src/constants.rs').exists())

    def test_values_planner_reads_base_sources_and_configured_cargo_target(self) -> None:
        ledger, definition, _ = self.setup_values()
        manifest = self.repo / 'Cargo.toml'
        _ = manifest.write_text(manifest.read_text() + '\n[lib]\npath="src/entry.rs"\n')
        _ = self.git('mv', 'src/lib.rs', 'src/entry.rs')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'custom library target')
        base = self.git('rev-parse', 'HEAD')
        _ = (self.repo / 'src/worker.rs').write_text('dirty checkout bytes must not enter the snapshot')
        # Site lineage is held fixed to inspect the planner's source boundary directly.
        selected = run.select(ledger, definition)
        def unchanged(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding, head: str,
                      sources: run.RenameSources, checkout: Path) -> ledger_api.HeldFinding:
            del ledger, head, sources
            self.assertEqual(checkout, self.repo)
            return finding
        with mock.patch.object(run, 'current_finding', side_effect=unchanged):
            plan = run.plan_constants(self.repo, ledger, base, selected)
        self.assertEqual(len({item.finding.finding_id for file in plan.files for item in file.kept}), 2)
        self.assertTrue(any(file.path == 'src/entry.rs' for file in plan.files))
        self.assertTrue(all('dirty checkout' not in file.original for file in plan.files))

    def test_third_rule_family_refuses_and_names_both_supported_families(self) -> None:
        ledger, definition, _ = self.setup_findings()
        with mock.patch.object(rules, 'load_rules', return_value=(replace(definition, family='types'),)):
            with self.assertRaisesRegex(bulk.BulkRenameError, 'names or values'):
                _ = self.execute(ledger, definition)
        self.assertFalse(self.runner.calls)

    def test_identical_declaration_line_is_eligible_before_editing(self) -> None:
        ledger, _, findings = self.setup_findings()
        for finding in findings:
            evidence = ledger.finding_site(finding, self.base)
            judged = ledger.judged_commit(finding)
            self.assertIsInstance(evidence, ledger_api.UnchangedSite, repr((evidence, judged)))
            self.assertIsInstance(
                run.current_rename(ledger, finding, self.base, run.RenameSources(), self.repo),
                bulk.RenameFinding, repr((evidence, judged, finding)))

    def test_field_refactor_runs_through_lineage_and_post_format_guard(self) -> None:
        source = "struct Record {\n old: usize,\n}\nfn f(r: Record) -> usize { r.old }\n"
        for path in ("src/lib.rs", "beta/src/lib.rs"):
            _ = (self.repo / path).write_text(source)
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "private field fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        ledger, definition, findings = self.setup_findings()
        for finding in findings:
            self.assertIsInstance(
                run.current_rename(ledger, finding, self.base, run.RenameSources(), self.repo),
                bulk.RenameFinding)
        report = self.execute(ledger, definition)
        self.assertEqual(report["applied"], 2)
        self.assertEqual(report["refused"], 0)
        self.assertIn("r.number", (self.repo / "src/lib.rs").read_text())

    def test_workspace_selection_crosses_crates_and_skips_built_withdrawn_and_held_with_reasons(self) -> None:
        ledger, definition, findings = self.setup_findings()
        selected = run.select(ledger, definition)
        self.assertEqual({f.path for f in selected.findings}, {"src/lib.rs", "beta/src/lib.rs"})
        first, second = findings
        ledger.ticket_add("5001", ticket_owner_id("other"), "proposal", "names", "beta", [second.path], [second.rule],
                          proposal=str(cast(ledger_api.Filed, second.state).proposal), finding_ids=(second.finding_id,))
        # Queued tickets own findings too, before any branch is assigned.
        selected = run.select(ledger, definition)
        self.assertEqual([f.finding_id for f in selected.findings], [first.finding_id])
        self.assertEqual([(f.finding_id, f.reason) for f in selected.skipped], [(second.finding_id, "held by another open ticket")])
        ledger.ticket_drop("5001")
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", (first.finding_id,))
            _ = connection.execute("UPDATE held_findings SET state='withdrawn' WHERE finding_id=?", (second.finding_id,))
            connection.commit()
        self.assertEqual({f.reason for f in run.select(ledger, definition).skipped}, {"built", "withdrawn"})

    def test_selection_includes_always_approved_but_never_overrides_a_decline(self) -> None:
        ledger, definition, findings = self.setup_findings()
        for finding in findings:
            _ = cast(ledger_api.Filed, finding.state).proposal.write_text('---\nstatus: open\n---\nAwaiting answer\n')
        self.assertFalse(run.select(ledger, definition).findings)
        automatic = replace(definition, approval=rules.AlwaysApproved("names"))
        self.assertEqual(len(run.select(ledger, automatic).findings), 2)
        _ = cast(ledger_api.Filed, findings[1].state).proposal.write_text('---\nstyle_answer: declined\n---\nNo\n')
        self.assertEqual(len(run.select(ledger, automatic).findings), 1)

    def test_rejudge_cut_refuses_changed_declaration_bytes_but_applies_identical_line(self) -> None:
        ledger, definition, findings = self.setup_findings()
        path = self.repo / "src/lib.rs"
        _ = path.write_text(path.read_text().replace("let old = 1", "let old = 2"))
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "change one declaration")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        ledger.set_tip(self.git("rev-parse", "HEAD"))
        report = self.execute(ledger, definition)
        self.assertEqual(report["refusals"], {findings[0].finding_id:
            "fresh judgment: rule applicability is not machine-checkable for this declaration"})
        self.assertEqual(report["applied_ids"], [findings[1].finding_id])
        self.assertIn("let old = 2", path.read_text())
        self.assertIn("let number", (self.repo / "beta/src/lib.rs").read_text())

    def test_base_tree_mismatch_and_dirty_index_refuse_before_selection_or_commands(self) -> None:
        ledger, definition, _ = self.setup_findings()
        with self.assertRaisesRegex(bulk.BulkRenameError, "base-tree mismatch"):
            _ = run.run_rule(self.repo, definition.rule_id, "0" * 40, ledger, self.server, self.runner)
        _ = (self.repo / "src/lib.rs").write_text("changed")
        _ = self.git("add", "src/lib.rs")
        with self.assertRaisesRegex(bulk.BulkRenameError, "clean tracked"):
            _ = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, self.runner)
        self.assertFalse(self.runner.calls)
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))

    def test_workspace_validation_runs_once_and_one_clean_test_commit_merges_all_decisions(self) -> None:
        ledger, definition, findings = self.setup_findings()
        report = self.execute(ledger, definition)
        commands = [args for _, args in self.runner.calls]
        self.assertEqual([args[1] for args in commands], ["+nightly", "check", "clippy", str(Path.home() / ".claude/scripts/direct/verify.sh"),
                                                                          str(Path.home() / ".claude/scripts/direct/verify.sh"),
                                                                          str(Path.home() / ".claude/scripts/direct/verify.sh")])
        self.assertEqual(commands[0], ("cargo", "+nightly", "fmt", "--package", "beta", "--package", "bulk_sample"))
        self.assertIn("--all-targets", commands[1])
        self.assertIn("--message-format=json", commands[2])
        verify = str(Path.home() / ".claude/scripts/direct/verify.sh")
        self.assertEqual(commands[3:], [("bash", verify, "final"), ("bash", verify, "lint"),
                                       ("bash", verify, "final", "--saved", "--tree", str(report["tested_tree"]))])
        self.assertTrue(all(folder == self.runner.test_checkout for folder, args in self.runner.calls if args[0] == "bash"))
        self.assertNotEqual(self.runner.test_checkout, self.repo)
        self.assertFalse(self.runner.test_checkout.exists())
        self.assertEqual(self.runner.test_tree, report["tested_tree"])
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "1")
        self.assertIn(f"Tested-Tree: {report['tested_tree']}", self.git("log", "-1", "--format=%B"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        ledger.set_tip(self.git("rev-parse", "HEAD"), "main")
        for ticket in cast(list[str], report["tickets"]):
            self.assertEqual(ledger.ticket_merge(ticket), ())
        self.assertTrue(all(isinstance(finding.state, ledger_api.Built) for finding in ledger.named_held(f.finding_id for f in findings)))

    def test_distinct_decisions_on_one_file_all_build_through_normal_ticket_merge(self) -> None:
        path = self.repo / "src/lib.rs"
        _ = path.write_text(path.read_text() + "fn extra() {\n    let other = 2;\n    let _ = other + 1;\n}\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "second binding fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        ledger, definition, findings = self.setup_findings()
        extra = self.approve_extra_rename(ledger, definition, 6, 'other', 'size')
        self.server = ScriptedServer(lambda text, offset, new: word_edits(text, "old" if new == "number" else "other", new))
        available = mock.Mock()
        command = subprocess.run
        with mock.patch.object(subprocess, "run", wraps=command) as calls:
            path_report = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, self.runner,
                                       available=available)
        report = bulk.object_fields(cast(object, json.loads(path_report.read_text())))
        self.assertEqual(available.call_count, 4)  # Selection, then every WorkspaceEdit document before writing.
        self.assertEqual({call.args[1] for call in available.call_args_list}, {"src/lib.rs", "beta/src/lib.rs"})
        judged_reads = [call.args[0] for call in calls.call_args_list
                        if call.args[0][:2] == ["git", "show"]]
        self.assertEqual(len(judged_reads), 2, repr(judged_reads))
        self.assertEqual(report["applied"], 3)
        ledger.set_tip(self.git("rev-parse", "HEAD"), "main")
        for ticket in cast(list[str], report["tickets"]):
            self.assertEqual(ledger.ticket_merge(ticket), ())
        self.assertTrue(all(isinstance(finding.state, ledger_api.Built)
                            for finding in ledger.named_held([*(f.finding_id for f in findings), extra.finding_id])))

    def test_checkpoint_publication_failure_preserves_tested_commit_and_recovers(self) -> None:
        ledger, definition, findings = self.setup_findings()
        checkpoint = ledger.ticket_checkpoint
        count = 0

        def interrupted(id: str, commit: str) -> None:
            nonlocal count
            count += 1
            if count == 2:
                raise ledger_api.LedgerError("checkpoint write interrupted")
            checkpoint(id, commit)

        with mock.patch.object(ledger, "ticket_checkpoint", side_effect=interrupted):
            report = self.execute(ledger, definition)
        self.assertIn("publication pending:", str(report["status"]))
        self.assertEqual(report["applied"], 2)
        self.assertEqual(self.git("rev-parse", "HEAD"), report["commit"])
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), report["tested_tree"])
        tickets = cast(list[str], report["tickets"])
        for id in tickets:
            ledger.ticket_checkpoint(id, str(report["commit"]))
        ledger.bulk_share_checkpoint(tickets)
        ledger.set_tip(str(report["commit"]), "main")
        for id in tickets:
            self.assertEqual(ledger.ticket_merge(id), ())
        self.assertTrue(all(isinstance(f.state, ledger_api.Built)
                            for f in ledger.named_held(f.finding_id for f in findings)))

    def test_json_error_reverts_only_its_file_then_checks_workspace_again(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.error_file = "beta/src/lib.rs"
        report = self.execute(ledger, definition)
        self.assertEqual((report["applied"], report["refused"], report["reverted"]), (1, 1, 0))
        self.assertEqual(report["refusals"], {findings[1].finding_id: "workspace diagnostics"})
        self.assertIn("let old", (self.repo / "beta/src/lib.rs").read_text())
        self.assertIn("let number", (self.repo / "src/lib.rs").read_text())
        self.assertEqual(sum(args[1] == "check" for _, args in self.runner.calls), 2)
        self.assertEqual(sum(args[1] == "clippy" for _, args in self.runner.calls), 2)
        self.assertEqual(sum(args[0] == "bash" and args[2] == "final" and args[2:4] != ("final", "--saved") for _, args in self.runner.calls), 1)
        self.assertEqual(len(cast(list[object], report["validation_loops"])), 2)

    def test_diagnostic_reader_reverts_the_edits_it_reads_transitively(self) -> None:
        files = tuple(bulk.rename_file(self.server, self.repo / path, path, (self.repo / path).read_text(),
                                      (bulk.RenameFinding(path, path, self.finding((self.repo / path).read_text()).rename, self.finding((self.repo / path).read_text(), path=path).authority, path),))
                      for path in ("src/lib.rs", "beta/src/lib.rs"))
        workspace = run.WorkspaceEdits(self.repo, self.base, files)
        for file in files:
            _ = (self.repo / file.path).write_text(file.edited)
        workspace.dependencies["src/lib.rs"] = frozenset(("src/lib.rs", "beta/src/lib.rs"))
        self.runner.error_file = "src/lib.rs"
        kept, reverted = run.validate(workspace, self.runner, {"check_clippy": 0.0}, [], lambda paths: paths)
        self.assertFalse(kept)
        self.assertEqual(set(reverted), {"src/lib.rs", "beta/src/lib.rs"})
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(sum(args[1] == "check" for _, args in self.runner.calls), 2)

    def test_no_progress_aborts_whole_batch_and_releases_every_finding(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.no_progress = True
        report = self.execute(ledger, definition)
        self.assertIn("no new edited file", str(report["status"]))
        self.assertEqual((report["applied"], report["reverted"]), (0, 2))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))
        self.assertTrue(all(isinstance(f.build_ticket, ledger_api.NoBuildTicket) for f in ledger.named_held(f.finding_id for f in findings)))
        self.assertFalse(any(args[0] == "bash" for _, args in self.runner.calls))

    def test_formatter_refusal_survives_later_validation_abort(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        self.runner.break_proof = True
        self.runner.no_progress = True
        report = self.execute(ledger, definition)
        formatter_reason = 'post-format rename edit changed: src/lib.rs'
        abort_reason = ('batch refused: validation errors remain but no new edited file '
                        'can be reverted')
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (0, 1, 1))
        self.assertEqual(report['refusals'], {findings[0].finding_id: formatter_reason})
        self.assertEqual(report['reversions'], {findings[1].finding_id: abort_reason})
        self.assertEqual(report['status'], abort_reason)
        self.assertEqual(report['counts'], [
            {'disposition': 'refused', 'reason': formatter_reason,
             'family': 'names', 'crate': '.', 'count': 1},
            {'disposition': 'reverted', 'reason': abort_reason,
             'family': 'names', 'crate': 'beta', 'count': 1},
        ])
        self.assertEqual({row.finding_id: row.reason
                          for ticket in cast(list[str], report['tickets'])
                          for row in ledger.bulk_refusals(ticket)},
                         {findings[0].finding_id: formatter_reason,
                          findings[1].finding_id: abort_reason})
        self.assertTrue(all(isinstance(finding.build_ticket, ledger_api.NoBuildTicket)
                            for finding in ledger.named_held(f.finding_id for f in findings)))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse(any(args[0] == 'bash' for _, args in self.runner.calls))

    def test_red_workspace_tests_drop_commit_and_release_all_kept_findings_with_failure(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.red_tests = True
        report = self.execute(ledger, definition)
        self.assertEqual((report["applied"], report["reverted"], report["commit"]), (0, 2, ""))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))
        for ticket in cast(list[str], report["tickets"]):
            self.assertTrue(all(row.reason == "tests failed: FAIL [0.01s] bulk_sample::tests::broken" for row in ledger.bulk_refusals(ticket)))
        self.assertTrue(all(isinstance(f.build_ticket, ledger_api.NoBuildTicket) for f in ledger.named_held(f.finding_id for f in findings)))
        self.assertEqual(sum(args[0] == "bash" and args[2] == "final" and args[2:4] != ("final", "--saved") for _, args in self.runner.calls), 1)
        self.assertFalse(list((ledger.database.parent / "bulk_review").glob("*.tested.json")))

    def test_report_has_reason_family_crate_counts_stages_and_tested_tree_evidence(self) -> None:
        ledger, definition, _ = self.setup_findings()
        report = self.execute(ledger, definition)
        self.assertEqual(report["applied"], 2)
        self.assertEqual({row["crate"] for row in cast(list[dict[str, object]], report["counts"])}, {".", "beta"})
        self.assertEqual({row["family"] for row in cast(list[dict[str, object]], report["counts"])}, {"names"})
        times = cast(dict[str, float], report["seconds"])
        self.assertEqual(set(times), {"select", "rename", "fmt", "check_clippy", "commit", "tests", "verify_final", "verify_lint", "final_pass"})
        self.assertTrue(all(value >= 0 for value in times.values()))
        self.assertEqual(report["tested_tree"], self.git("rev-parse", "HEAD^{tree}"))
        self.assertFalse(list((ledger.database.parent / "bulk_review").glob("*.tested.json")))
        self.assertEqual(self.runner.test_tree, report["tested_tree"])
        self.assertEqual(len(cast(list[object], report["sample"])), 2)
        self.assertEqual(report["summary"], run.summary(report))

    def test_test_gate_refuses_untracked_files_and_a_different_tree_before_running(self) -> None:
        _ = (self.repo / "untracked.txt").write_text("untracked test input")
        with self.assertRaisesRegex(bulk.BulkRenameError, "clean checkout"):
            _ = run.tested_tree_proof(self.repo, self.tree, self.runner, {})
        (self.repo / "untracked.txt").unlink()
        with self.assertRaisesRegex(bulk.BulkRenameError, "tested tree"):
            _ = run.tested_tree_proof(self.repo, "0" * 40, self.runner, {})
        self.assertFalse(self.runner.calls)

    def test_unexpected_runner_failure_is_reported_as_a_tool_defect(self) -> None:
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)

        def defective_runner(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            _ = repo, arguments
            raise RuntimeError('formatter adapter broke')

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server,
                            defective_runner, available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual(report['status'], 'tool defect: formatter adapter broke')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_unrelated_formatter_changes_abort_and_restore(self) -> None:
        ledger, definition, _ = self.setup_findings()
        self.runner.unrelated = True
        report = self.execute(ledger, definition)
        self.assertIn("unrelated file", str(report["status"]))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_formatter_token_change_refuses_only_its_file_and_keeps_one_tested_commit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        source = (self.repo / 'src/lib.rs').read_text()
        self.runner.break_proof = True
        report = self.execute(ledger, definition)
        reason = 'post-format rename edit changed: src/lib.rs'
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id: reason})
        self.assertEqual((self.repo / 'src/lib.rs').read_text(), source)
        self.assertIn('let number', (self.repo / 'beta/src/lib.rs').read_text())
        self.assertIsInstance(ledger.named_held((findings[0].finding_id,))[0].state, ledger_api.Filed)
        self.assertTrue(any(item.reason == reason for ticket in cast(list[str], report['tickets'])
                            for item in ledger.bulk_refusals(ticket)))
        self.assertEqual(self.git('rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == 'bash'], ['final', 'lint', 'final --saved'])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_formatter_failure_in_a_reference_restores_its_whole_rename_group(self) -> None:
        ledger, definition, findings = self.setup_field_reference()
        self.addCleanup(ledger.close)
        originals = {path: (self.repo / path).read_text() for path in ('src/lib.rs', 'src/child.rs')}

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                _ = (repo / 'src/child.rs').write_text('fn read() {}\n')
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, formatter,
                            available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id:
                                             'post-format rename edit changed: src/child.rs'})
        for name, original in originals.items():
            self.assertEqual((self.repo / name).read_text(), original)
        self.assertIn('let number', (self.repo / 'beta/src/lib.rs').read_text())

    def test_formatter_rollback_reproves_and_keeps_an_independent_reader(self) -> None:
        extra_path = self.repo / 'src/extra.rs'
        extra_source = 'fn extra() { let other = 2; consume(other); }\n'
        _ = extra_path.write_text(extra_source)
        ledger, definition, findings = self.setup_field_reference('mod extra;\n')
        self.addCleanup(ledger.close)
        extra = self.approve_extra_rename(ledger, definition, 1, 'other', 'number', 'src/extra.rs')
        self.server.edit = lambda text, offset, new: word_edits(text, 'other' if 'let other' in text else 'old', new)
        named_held = ledger.named_held

        def declaration_first(names: Iterable[str]) -> tuple[ledger_api.HeldFinding, ...]:
            return tuple(sorted(named_held(names), key=lambda finding: finding.path != 'src/lib.rs'))

        self.runner.break_proof = True
        with mock.patch.object(ledger, 'named_held', side_effect=declaration_first), mock.patch.object(
                run, 'plan_renames', wraps=run.plan_renames) as planner:
            report = self.execute(ledger, definition)
        self.assertEqual(planner.call_count, 2)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 1, 0))
        self.assertEqual(report['refusals'], {findings[0].finding_id: 'post-format rename edit changed: src/lib.rs'})
        self.assertNotIn(extra.finding_id, cast(dict[str, str], report['refusals']))
        self.assertEqual(extra_path.read_text(), extra_source.replace('other', 'number'))
        self.assertIn('let number', (self.repo / 'beta/src/lib.rs').read_text())

    def check_formatter_retry_after_reproof_excludes_another_file(self, interrupted: bool) -> None:
        extra = self.repo / 'src/extra.rs'
        _ = extra.write_text('fn extra() { let old = 2; consume(old); }\n')
        library = self.repo / 'src/lib.rs'
        _ = library.write_text(library.read_text() + '\nmod extra;\n')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'retry survivor fixture')
        self.base, self.tree = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        _ = self.approve_extra_rename(ledger, definition, 1, 'old', 'number', 'src/extra.rs')
        original = (self.repo / 'beta/src/lib.rs').read_text()
        planner = run.plan_renames
        passes = 0

        def replan(repo: Path, store: ledger_api.Ledger, base: str, selected: run.Selection,
                   server: bulk.RenameServer, availability: run.PlanningFileAvailability) -> run.PlannedEdits:
            plan = planner(repo, store, base, selected, server, availability)
            if passes:
                # The remaining reader fails fresh resolution after the first edit is restored.
                return run.PlannedEdits(tuple(file for file in plan.files if file.path != 'beta/src/lib.rs'),
                                        (*plan.refusals, bulk.Refusal(findings[1].finding_id, 'reader no longer proved')))
            return plan

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            nonlocal passes
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                passes += 1
                if passes == 1:
                    _ = (repo / 'src/lib.rs').write_text('fn changed() {}\n')
                else:
                    _ = (repo / 'beta/src/lib.rs').write_text('\n' + original)
                    if interrupted:
                        os._exit(73)
            return result

        with mock.patch.object(run, 'plan_renames', side_effect=replan):
            if interrupted:
                stop_process(lambda: run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                                                  self.server, formatter, available=self.available))
            else:
                path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger,
                                    self.server, formatter, available=self.available)
                report = bulk.object_fields(cast(object, json.loads(path.read_text())))
                self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 2, 0))
                self.assertEqual(passes, 2)
                self.assertIn('let number', extra.read_text())
                self.assertEqual((self.repo / 'beta/src/lib.rs').read_text(), original)
                self.assertEqual(self.git('status', '--porcelain'), '')
                return
        tickets = {ticket['id'] for ticket in ledger.proposal_tickets()
                   if ticket['state'] not in ledger_api.FINAL_TICKET_STATES}
        self.assertTrue(tickets)
        self.assertEqual((self.repo / 'beta/src/lib.rs').read_text(), '\n' + original)
        # Stop the recovered run at selection, so only recovery can restore the dirty files.
        with mock.patch.object(run, 'select', return_value=run.Selection((), ())):
            try:
                report = self.execute(ledger, definition)
            except bulk.BulkRenameError as error:
                self.fail(f'recovery must restore the retry and release its tickets: {error}')
        self.assertEqual(report['applied'], 0)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertTrue(all(ticket['state'] in ledger_api.FINAL_TICKET_STATES
                            for ticket in ledger.proposal_tickets() if ticket['id'] in tickets))
        self.assertTrue(all(isinstance(row.build_ticket, ledger_api.NoBuildTicket)
                            for row in ledger.named_held(tuple(finding.finding_id for finding in findings))))

    def test_formatter_retry_restores_paths_excluded_by_survivor_reproof(self) -> None:
        self.check_formatter_retry_after_reproof_excludes_another_file(False)

    def test_recovery_restores_a_formatter_retry_on_an_excluded_path_and_releases_tickets(self) -> None:
        self.check_formatter_retry_after_reproof_excludes_another_file(True)

    def test_recovery_refuses_content_changes_in_an_excluded_formatter_path(self) -> None:
        files = tuple(bulk.FileRenames(path, (self.repo / path).read_text(),
                                      (self.repo / path).read_text().replace('old', 'number'), (), ())
                      for path in ('src/lib.rs', 'beta/src/lib.rs'))
        intent = run.WorkspaceIntent(self.repo, 'retry recovery')
        intent.begin(())
        intent.record_plan(run.PlannedEdits(files, ()))
        intent.record_plan(run.PlannedEdits(files[1:], ()))
        _ = (self.repo / files[0].path).write_text('fn different_item() {}\n')
        _ = (self.repo / files[1].path).write_text(files[1].edited)
        with self.assertRaisesRegex(bulk.BulkRenameError, 'bytes do not match the recorded edit'):
            _ = intent.recover()
        self.assertEqual((self.repo / files[1].path).read_text(), files[1].edited)
        self.assertEqual((self.repo / files[0].path).read_text(), 'fn different_item() {}\n')

    def test_rebuilt_plan_rechecks_a_path_that_became_unavailable(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        held = False
        reason = 'beta/src/lib.rs: another worktree holds the file'

        def available(_repo: Path, path: str) -> None:
            if held and path == 'beta/src/lib.rs':
                raise bulk.BulkRenameError(reason)

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            nonlocal held
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                held = True
                _ = (repo / 'src/lib.rs').write_text('fn changed() {}\n')
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server,
                            formatter, available=available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (0, 2, 0))
        self.assertEqual(bulk.object_fields(report['refusals'])[findings[1].finding_id], reason)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_lengthening_rename_reflow_keeps_identifier_token_positions(self) -> None:
        source = ('fn run() {\n'
                  '    let old = input.first_method().second_method().third_method().fourth_method().fifth_method();\n'
                  '    consume(old);\n}\n')
        _ = (self.repo / 'src/lib.rs').write_text(source)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'long method chain')
        self.base, self.tree = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD^{tree}')
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        planned = source.replace('old', 'number')

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                # Model rustfmt's method-chain reflow; the scratch replay runs the real formatter.
                formatted = planned.replace('input.', 'input\n        .').replace('().', '()\n        .')
                _ = (repo / 'src/lib.rs').write_text(formatted)
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, formatter,
                            available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        formatted = (self.repo / 'src/lib.rs').read_text()
        self.assertNotEqual(formatted, planned)
        self.assertIn('\n        .first_method()', formatted)
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (2, 0, 0))
        self.assertTrue(bulk.file_edits_preserved(bulk.FileRenames('src/lib.rs', source, planned, (), ()), formatted))

    def test_all_formatter_failures_leave_findings_filed_without_tests_or_a_commit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)

        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if 'fmt' in arguments:
                for name in ('src/lib.rs', 'beta/src/lib.rs'):
                    _ = (repo / name).write_text('fn changed() {}\n')
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server, formatter,
                            available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (0, 2, 0))
        self.assertEqual(report['status'], 'no edits')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse(any(args[0] == 'bash' for _, args in self.runner.calls))
        self.assertTrue(all(isinstance(row.state, ledger_api.Filed)
                            for row in ledger.named_held(tuple(finding.finding_id for finding in findings))))

    def test_formatter_wraps_plain_comment_text_at_the_same_token(self) -> None:
        for marker in ('//', '///', '//!'):
            with self.subTest(marker=marker):
                before = f'{marker} Recorded `answers` keep the same text and attachment.\nfn run() {{}}\n'
                after = f'{marker} Recorded `answers` keep the same text\n{marker} and attachment.\nfn run() {{}}\n'
                file = bulk.FileRenames('src/lib.rs', before, before, (), ())
                self.assertTrue(bulk.file_edits_preserved(file, after))
                self.assertFalse(bulk.file_edits_preserved(file, after.replace('same', 'other')))
                self.assertFalse(bulk.file_edits_preserved(file, 'fn run() {}\n' + after.split('fn run()')[0]))
        before = '/// First paragraph.\n///\n/// Second paragraph.\nfn run() {}'
        after = '/// First paragraph. Second paragraph.\nfn run() {}'
        self.assertFalse(bulk.file_edits_preserved(bulk.FileRenames('src/lib.rs', before, before, (), ()), after))

    def test_formatter_wraps_prose_starting_with_inline_code_without_changing_its_attachment(self) -> None:
        before = ('// `tree.insert_into_hierarchy` inserts `root_item` under an existing parent workspace or as a\n'
                  '// top-level peer; `regroup_members` reapplies `inline_dirs`.\nfn run() {}\n')
        after = ('// `tree.insert_into_hierarchy` inserts `root_item` under an existing parent workspace or as\n'
                 '// a top-level peer; `regroup_members` reapplies `inline_dirs`.\nfn run() {}\n')
        file = bulk.FileRenames('src/lib.rs', before, before, (), ())
        self.assertTrue(bulk.file_edits_preserved(file, after))
        self.assertFalse(bulk.file_edits_preserved(file, after.replace('root_item', 'other')))
        self.assertFalse(bulk.file_edits_preserved(file, 'fn run() {}\n' + after.split('fn run()')[0]))

    def test_formatter_preserves_structured_comments_and_item_attachment(self) -> None:
        for body in ('/// ```\n/// first\n/// second\n/// ```\n',
                     '/// first  \n/// second\n', '/// - first\n/// - second\n',
                     '/* first\nsecond */\n'):
            with self.subTest(body=body):
                before = body + 'fn first() {}\nfn second() {}\n'
                file = bulk.FileRenames('src/lib.rs', before, before, (), ())
                self.assertFalse(bulk.file_edits_preserved(file, 'fn first() {}\n' + body + 'fn second() {}\n'))
                self.assertFalse(bulk.file_edits_preserved(file, before.replace('first\n', 'first second\n').replace('/// second\n', '')))

    def test_formatter_preserves_line_comment_like_contents_inside_a_block_comment(self) -> None:
        before = '/**\n// first\n// second\n*/\nfn run() {}\n'
        file = bulk.FileRenames('src/lib.rs', before, before, (), ())
        self.assertFalse(bulk.file_edits_preserved(file, '/**\n// first second\n*/\nfn run() {}\n'))
        self.assertFalse(bulk.file_edits_preserved(file, '// first second\nfn run() {}\n'))

    def test_formatter_preserves_backslash_hard_breaks_in_comment_paragraphs(self) -> None:
        before = '/// first\\\n/// second\nfn run() {}\n'
        file = bulk.FileRenames('src/lib.rs', before, before, (), ())
        self.assertFalse(bulk.file_edits_preserved(file, before.replace('first\\\n/// second', 'first second')))
        self.assertFalse(bulk.file_edits_preserved(file, before.replace('first\\\n/// second', 'first\\ second')))
        plain = '// first\n// second\nfn run() {}\n'
        self.assertTrue(bulk.file_edits_preserved(bulk.FileRenames('src/lib.rs', plain, plain, (), ()),
                                                 '// first second\nfn run() {}\n'))

    def test_compiler_failure_is_refused_with_its_cause_before_the_survivor_commit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.addCleanup(ledger.close)
        def diagnostic_runner(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            result = self.runner(repo, arguments)
            if (len(arguments) > 1 and arguments[1] in ('check', 'clippy')
                    and 'let number' in (repo / 'src/lib.rs').read_text()):
                return run.CommandResult(101, json.dumps({'reason': 'compiler-message', 'message': {
                    'level': 'error', 'message': 'struct `Record` has no field named `number`',
                    'code': {'code': 'E0560'}, 'spans': [{'file_name': 'src/lib.rs'}]}}))
            return result
        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server,
                            diagnostic_runner, available=self.available)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report['applied'], report['refused'], report['reverted']), (1, 1, 0))
        refusals = bulk.object_fields(report['refusals'])
        self.assertIn('E0560: struct `Record` has no field named `number`', str(refusals[findings[0].finding_id]))
        self.assertIn('let old', self.git('show', 'HEAD:src/lib.rs'))
        self.assertIn('let number', self.git('show', 'HEAD:beta/src/lib.rs'))
        self.assertIsInstance(ledger.named_held((findings[0].finding_id,))[0].state, ledger_api.Filed)
        self.assertEqual(sum(args[0] == 'bash' and args[2] == 'final' and args[2:4] != ('final', '--saved') for _, args in self.runner.calls), 1)

    def test_read_only_proof_dependencies_do_not_join_the_edit_group(self) -> None:
        files = (bulk.FileRenames('src/lib.rs', '', '', (), (), proof_dependencies=frozenset({'src/other.rs'})),
                 bulk.FileRenames('src/other.rs', '', '', (), ()))
        workspace = run.WorkspaceEdits(self.repo, self.base, files)
        self.assertEqual(run.dependent_files(workspace.dependencies, {'src/other.rs'}), {'src/other.rs'})

    def test_formatter_comma_and_block_brace_changes_keep_identifiers_and_comments(self) -> None:
        for before, after in (
                ('fn run() { make(number) }', 'fn run() { { make(number,) } }'),
                ('fn run() { Record { number } }', 'fn run() { Record { number, } }'),
                ('fn run() { { number } }', 'fn run() { number }'),
                ('fn run() { Record { number /* retained */, } }',
                 'fn run() { Record { number, /* retained */ } }')):
            with self.subTest(before=before):
                file = bulk.FileRenames('src/lib.rs', before, before, (), ())
                self.assertTrue(bulk.file_edits_preserved(file, after))
                self.assertFalse(bulk.file_edits_preserved(file, after.replace('number', 'other')))
                if 'retained' in after:
                    self.assertFalse(bulk.file_edits_preserved(file, after.replace('retained', 'changed')))

    def test_formatter_import_reordering_still_refuses(self) -> None:
        before = 'pub(crate) use z::Z;\npub(crate) use a::A;\n'
        after = 'pub(crate) use a::A;\npub(crate) use z::Z;\n'
        self.assertFalse(bulk.file_edits_preserved(bulk.FileRenames('src/lib.rs', before, before, (), ()), after))

    def test_formatter_proof_rejects_added_removed_changed_or_reordered_tokens(self) -> None:
        # These proof tests belong to test_bulk_rename, kept here within this phase's file scope.
        before = 'fn run() { let number = input.first().second(); consume(number); }\n'
        file = bulk.FileRenames('src/lib.rs', before, before, (), ())
        for changed in (before.replace('.second()', ''),
                        before.replace('consume(number)', 'consume(other)'),
                        before.replace('input.first().second()', 'input.second().first()')):
            with self.subTest(changed=changed):
                self.assertFalse(bulk.file_edits_preserved(file, changed))
        self.assertFalse(bulk.file_edits_preserved(file, before + '// unexpected comment\n'))


    def test_verify_final_and_lint_both_must_pass_and_have_separate_times(self) -> None:
        for failed in ("", "lint"):
            with self.subTest(failed=failed):
                self.runner = FakeRunner()
                self.runner.fail_stage = failed
                times: dict[str, float] = {}
                result = run.tested_tree_proof(self.repo, self.tree, self.runner, times)
                stages = [("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls]
                self.assertEqual(stages, ["final", "lint", "final --saved"] if not failed else ["final", "lint"])
                self.assertEqual(result.code, 0 if not failed else 1)
                self.assertTrue(all(folder == self.repo and args[:2] == ("bash", str(Path.home() / ".claude/scripts/direct/verify.sh"))
                                    for folder, args in self.runner.calls))
                self.assertIn("verify_final", times)
                self.assertIn("verify_lint", times)
                self.assertTrue(all(value >= 0 for value in times.values()))

    def assert_pass_record_refuses_batch(self, other_tree: bool) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.fail_stage = "" if other_tree else "final --saved"
        self.runner.other_tree_record = other_tree
        report = self.execute(ledger, definition)
        self.assertEqual((report["applied"], report["reverted"], report["commit"]), (0, 2, ""))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        query = self.runner.calls[-1]
        self.assertEqual(query[1][-4:], ("final", "--saved", "--tree", str(report["tested_tree"])))
        self.assertNotEqual(query[0], self.repo)
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))
        self.assertTrue(all(isinstance(f.build_ticket, ledger_api.NoBuildTicket)
                            for f in ledger.named_held(f.finding_id for f in findings)))

    def test_missing_pass_record_reverts_and_releases_the_batch(self) -> None:
        self.assert_pass_record_refuses_batch(False)

    def test_other_tree_pass_record_reverts_and_releases_the_batch(self) -> None:
        self.assert_pass_record_refuses_batch(True)

    def test_tested_tree_proof_rejects_drift_after_each_command(self) -> None:
        for stage in ("final", "lint", "final --saved"):
            with self.subTest(stage=stage):
                self.runner = FakeRunner()
                self.runner.mutate_stage = stage
                with self.assertRaisesRegex(bulk.BulkRenameError, "clean checkout"):
                    _ = run.test_tree(self.repo, self.base, self.tree, self.runner, {})
                self.assertFalse(self.runner.test_checkout.exists())
                self.assertEqual("final --saved" if self.runner.calls[-1][1][2:4] == ("final", "--saved") else self.runner.calls[-1][1][2], stage)

    def test_hidden_untracked_test_output_never_publishes_or_keeps_the_batch_commit(self) -> None:
        ledger, definition, findings = self.setup_findings()
        self.runner.mutate_stage = "final"

        def hide_untracked_output(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            if tuple(arguments)[-1:] == ("final",):
                _ = run.git(repo, "config", "status.showUntrackedFiles", "no")
            result = self.runner(repo, arguments)
            if tuple(arguments)[-1:] == ("final",):
                self.assertTrue((repo / "unexpected.txt").exists())
                self.assertEqual(run.git(repo, "status", "--porcelain"), "")
            return result

        path = run.run_rule(self.repo, definition.rule_id, self.tree, ledger, self.server,
                            hide_untracked_output, available=lambda repo, path: None)
        report = bulk.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual((report["applied"], report["reverted"], report["commit"]), (0, 2, ""))
        self.assertIn("clean checkout", str(report["status"]))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for _, args in self.runner.calls if args[0] == "bash"], ["final"])
        self.assertFalse(self.runner.test_checkout.exists())
        self.assertFalse(tuple(ticket for ticket in ledger.proposal_tickets()
                               if ticket["state"] not in ledger_api.FINAL_TICKET_STATES))
        self.assertTrue(all(isinstance(f.build_ticket, ledger_api.NoBuildTicket)
                            for f in ledger.named_held(f.finding_id for f in findings)))


    def test_private_planners_read_physical_bytes_from_execution_checkout(self) -> None:
        ledger, definition, _ = self.setup_findings()
        self.addCleanup(ledger.close)
        checkout = self.root / "private"
        _ = self.git("clone", "--shared", str(self.repo), str(checkout))
        selected = run.select(ledger, definition)
        for finding in selected.findings:
            _ = (self.repo / finding.path).write_text("pub fn dirty() {}\n")
        planned = run.plan_rule(checkout, ledger, self.base, selected, definition,
                                self.server, self.available)
        self.assertEqual(planned.refusals, ())
        self.assertEqual({item.finding.finding_id for file in planned.files for item in file.kept},
                         {finding.finding_id for finding in selected.findings})
        self.assertTrue(all("let old" in file.original for file in planned.files))
        self.assertTrue(all("let number" in file.edited for file in planned.files))


class FreshMachineJudgmentTests(BulkRunFixture):
    def fresh_case(self, current: str = MACHINE_RENAME_AFTER
                   ) -> tuple[ledger_api.Ledger, ledger_api.HeldFinding, str]:
        ledger = self.ledger()
        definition = next(rule for rule in rules.load_rules(self.repo)
                          if rule.name == "prefer-type-named-fields-and-bindings")
        finding, tip = machine_rename_fixture(ledger, definition, self.repo, self.root / "fresh.md", current)
        return ledger, finding, tip

    def prove(self, ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding, tip: str
              ) -> run.FreshMachineJudgment | bulk.Refusal:
        server = ScriptedServer(lambda text, _offset, new: word_edits(text, "item", new))
        return run.fresh_machine_judgment(ledger, finding, tip, self.repo, server,
                                         run.PlanningFileAvailability(self.repo, lambda _repo, _path: None))

    def test_fresh_proof_blocked_by_a_writer_is_pending_instead_of_a_refusal(self) -> None:
        ledger, finding, tip = self.fresh_case()
        writer = berth_board.Reservation("writer", "main", self.root / "writer", "refs/heads/writer",
                                         ("src/lib.rs",), "active", "blocking")
        server = ScriptedServer(lambda text, _offset, new: word_edits(text, "item", new))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((writer,))):
            plan = run.plan_rule(self.repo, ledger, tip, run.Selection((finding,), ()),
                                 next(rule for rule in rules.load_rules(self.repo) if rule.rule_id == finding.rule),
                                 server, bulk.check_available)
        self.assertEqual(plan.refusals, ())
        self.assertEqual(plan.files, ())
        self.assertEqual(plan.pending, (bulk.PendingFinding(finding.finding_id, finding.path, (writer,)),))
        self.assertEqual(server.changes, [])

    def test_planning_availability_cache_keeps_the_reservation_variant(self) -> None:
        writer = berth_board.Reservation("writer", "main", self.root / "writer", "refs/heads/writer",
                                         ("src/lib.rs",), "active", "blocking")
        cache = run.PlanningFileAvailability(self.repo, bulk.check_available)
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((writer,))) as board:
            for _ in range(2):
                with self.assertRaises(bulk.ReservationBlocked) as caught:
                    cache.require(("src/lib.rs",))
                self.assertEqual(caught.exception.holders, (writer,))
        self.assertEqual(board.call_count, 1)

    def test_late_plan_boundary_retains_pending_instead_of_terminal_file_refusals(self) -> None:
        ledger, finding, tip = self.fresh_case()
        server = ScriptedServer(lambda text, _offset, new: word_edits(text, "item", new))
        plan = run.plan_renames(self.repo, ledger, tip, run.Selection((finding,), ()), server,
                                run.PlanningFileAvailability(self.repo, lambda _repo, _path: None))
        self.assertTrue(plan.files)
        writer = berth_board.Reservation("writer", "main", self.root / "writer", "refs/heads/writer",
                                         ("src/lib.rs",), "active", "blocking")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((writer,))):
            checked = run.available_constant_plan(plan, run.PlanningFileAvailability(self.repo, bulk.check_available))
        self.assertEqual(checked.files, ())
        self.assertEqual(checked.refusals, ())
        self.assertEqual(checked.pending, (bulk.PendingFinding(finding.finding_id, finding.path, (writer,)),))

    def test_return_type_and_owner_change_gets_a_new_judgment_without_changing_the_old_one(self) -> None:
        ledger, finding, tip = self.fresh_case()
        self.assertIsInstance(ledger.finding_site(finding, tip), ledger_api.NeedsJudgment)
        old = ledger.finding_anchor(finding)
        server = ScriptedServer(lambda text, _offset, new: word_edits(text, "item", new))
        plan = run.plan_renames(self.repo, ledger, tip, run.Selection((finding,), ()), server,
                                run.PlanningFileAvailability(self.repo, lambda _repo, _path: None))
        self.assertEqual(plan.refusals, ())
        self.assertEqual([item.finding.finding_id for file in plan.files for item in file.kept], [finding.finding_id])
        judgment, = plan.judgments
        self.assertIsInstance(judgment, run.FreshMachineJudgment)
        self.assertEqual(judgment.anchor, ledger_api.FindingAnchor(tip, "src/lib.rs", 4))
        sources = run.RenameSources()
        sources.judgments[finding.finding_id] = judgment
        self.assertIsInstance(run.current_rename(ledger, finding, tip, sources, self.repo), bulk.RenameFinding)
        self.assertEqual(ledger.finding_anchor(finding), old)
        self.assertEqual((self.repo / "src/lib.rs").read_text(), MACHINE_RENAME_AFTER)

    def test_fresh_judgment_refuses_a_shadowing_destination_binding(self) -> None:
        source = MACHINE_RENAME_AFTER.replace("inspect(item)", "let impl_item = item; inspect(item)")
        ledger, finding, tip = self.fresh_case(source)
        result = self.prove(ledger, finding, tip)
        self.assertIsInstance(result, bulk.Refusal)
        assert isinstance(result, bulk.Refusal)
        self.assertIn("capture", result.reason)

    def test_fresh_judgment_refuses_a_changed_pinned_rule(self) -> None:
        ledger, finding, tip = self.fresh_case()
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.rule_id == finding.rule)
        _ = definition.path.write_text(definition.path.read_text() + "Changed rule requirement\n")
        result = self.prove(ledger, finding, tip)
        self.assertIsInstance(result, bulk.Refusal)
        assert isinstance(result, bulk.Refusal)
        self.assertIn("rule changed", result.reason)

    def test_planner_reports_the_changed_rule_instead_of_the_earlier_site_refusal(self) -> None:
        ledger, finding, tip = self.fresh_case()
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.rule_id == finding.rule)
        _ = definition.path.write_text(definition.path.read_text() + "Changed rule requirement\n")
        server = ScriptedServer(lambda text, _offset, new: word_edits(text, "item", new))
        plan = run.plan_renames(self.repo, ledger, tip, run.Selection((finding,), ()), server,
                                run.PlanningFileAvailability(self.repo, lambda _repo, _path: None))
        self.assertEqual(plan.refusals, (bulk.Refusal(finding.finding_id,
                              "changed since judged: rule changed since judged"),))
        self.assertEqual(plan.judgments, ())

    def test_local_rule_conditions_cannot_be_replaced_by_the_filename_contract(self) -> None:
        ledger = self.ledger()
        folder = self.repo / "docs/style"
        folder.mkdir(parents=True)
        text = ("---\ntier: C\nfamily: names\ninput_scope: file\n---\n"
                "# Type-named parameters\n\n"
                "Only in functions returning bool, name a parameter after its type.\n")
        _ = (folder / "prefer-type-named-fields-and-bindings.md").write_text(text)
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.origin == "local")
        finding, tip = machine_rename_fixture(ledger, definition, self.repo, self.root / "fresh.md", rule_text=text)
        server = ScriptedServer(lambda source, _offset, new: word_edits(source, "item", new))
        plan = run.plan_renames(self.repo, ledger, tip, run.Selection((finding,), ()), server,
                                run.PlanningFileAvailability(self.repo, lambda _repo, _path: None))
        self.assertEqual(plan.refusals, (bulk.Refusal(finding.finding_id,
                              "fresh judgment: rule has no recognized machine-checkable contract"),))
        self.assertEqual(plan.judgments, ())
        self.assertEqual(plan.files, ())

    def test_fresh_judgment_never_overrides_a_declined_decision(self) -> None:
        ledger, finding, tip = self.fresh_case()
        assert isinstance(finding.state, ledger_api.Filed)
        _ = finding.state.proposal.write_text("---\nstyle_answer: declined\n---\nNo\n")
        result = self.prove(ledger, finding, tip)
        self.assertIsInstance(result, bulk.Refusal)
        assert isinstance(result, bulk.Refusal)
        self.assertIn("decision not approved", result.reason)

    def test_fresh_judgment_refuses_unresolved_module_instances(self) -> None:
        ledger, finding, tip = self.fresh_case(MACHINE_RENAME_AFTER + "mod missing;\n")
        result = self.prove(ledger, finding, tip)
        self.assertIsInstance(result, bulk.Refusal)
        assert isinstance(result, bulk.Refusal)
        self.assertIn("coverage unavailable", result.reason)

    def test_changed_declaration_with_a_still_applicable_type_name_gets_a_new_judgment(self) -> None:
        ledger, finding, tip = self.fresh_case(MACHINE_RENAME_AFTER.replace("item: &ImplItem", "item: &mut ImplItem"))
        self.assertIsInstance(ledger.finding_site(finding, tip), ledger_api.NeedsJudgment)
        result = self.prove(ledger, finding, tip)
        self.assertIsInstance(result, run.FreshMachineJudgment)
        assert isinstance(result, run.FreshMachineJudgment)
        sources = run.RenameSources()
        sources.judgments[finding.finding_id] = result
        self.assertIsInstance(run.current_rename(ledger, finding, tip, sources, self.repo), bulk.RenameFinding)

    def test_changed_declaration_that_no_longer_declares_the_old_name_is_refused(self) -> None:
        ledger, finding, tip = self.fresh_case(MACHINE_RENAME_AFTER.replace("item: &ImplItem", "other: &ImplItem"))
        result = self.prove(ledger, finding, tip)
        self.assertIsInstance(result, bulk.Refusal)
        assert isinstance(result, bulk.Refusal)
        self.assertIn("declaration", result.reason)

    def test_current_parameter_type_must_still_justify_the_requested_name(self) -> None:
        ledger, finding, tip = self.fresh_case(MACHINE_RENAME_AFTER.replace("item: &ImplItem", "item: &OtherType"))
        result = self.prove(ledger, finding, tip)
        self.assertIsInstance(result, bulk.Refusal)
        assert isinstance(result, bulk.Refusal)
        self.assertIn("current parameter", result.reason)

    def test_frozen_judgment_does_not_accept_another_tip_or_new_physical_bytes(self) -> None:
        ledger, finding, tip = self.fresh_case()
        result = self.prove(ledger, finding, tip)
        self.assertIsInstance(result, run.FreshMachineJudgment)
        assert isinstance(result, run.FreshMachineJudgment)
        self.assertIn("tip changed", run.validate_machine_judgment(ledger, finding, "another-tip", self.repo, result))
        _ = (self.repo / finding.path).write_text(MACHINE_RENAME_AFTER.replace("inspect(item)", "changed(item)"))
        self.assertIn("source inputs changed", run.validate_machine_judgment(ledger, finding, tip, self.repo, result))

    def test_fresh_judgment_never_overrides_a_withdrawn_finding(self) -> None:
        ledger, finding, tip = self.fresh_case()
        self.assertIsInstance(self.prove(ledger, finding, tip), run.FreshMachineJudgment)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='withdrawn' WHERE finding_id=?", (finding.finding_id,))
        withdrawn, = ledger.named_held((finding.finding_id,))
        result = self.prove(ledger, withdrawn, tip)
        self.assertIsInstance(result, bulk.Refusal)
        assert isinstance(result, bulk.Refusal)
        self.assertIn("not an open Rust finding", result.reason)


class CargoPackageFixture(TypedDict):
    id: str
    name: str
    manifest_path: str
    dependencies: list[dict[str, object]]


class CargoWorkspaceFixture(TypedDict):
    workspace_members: list[str]
    packages: list[CargoPackageFixture]


@final
class StyleChildAdmissionTests(unittest.TestCase):
    repo: Path = Path()
    state: Path = Path()
    queue: StyleQueueFake = StyleQueueFake()

    @override
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="style-admission-test-")))
        self.state = self.repo / "fmt"
        fake_fmt.install(self.repo / "bin", self.state)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "PATH": str(self.repo / "bin") + os.pathsep + os.environ.get("PATH", ""),
            fake_fmt.STATE: str(self.state),
        })))
        self.queue = self.enterContext(unqueued_style_children())

    def test_bulk_cargo_starts_use_the_shared_place_and_verify_does_not(self) -> None:
        for name, text in (("stdout", "out"), ("stderr", "err"), ("exit", "7")):
            _ = (self.state / "cargo" / name).write_text(text)
        commands = (("cargo", "check"), ("cargo", "clippy"), ("cargo", "+nightly", "fmt"))
        for arguments in commands:
            with self.subTest(arguments=arguments):
                self.assertEqual(run.run_command(self.repo, arguments), run.CommandResult(7, "out", "err"))
        self.assertEqual(self.queue.calls, [("cargo", self.repo)] * len(commands))
        self.assertEqual([place.arguments for place in self.queue.places], [[arguments] for arguments in commands])
        self.assertTrue(all(place.closed for place in self.queue.places))
        verify = ("bash", "/private/verify.sh", "check")
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(verify, 0, "ok", "")) as plain:
            self.assertEqual(run.run_command(self.repo, verify), run.CommandResult(0, "ok", ""))
            plain.assert_called_once()
        self.assertEqual(len(self.queue.calls), len(commands))

    def test_bulk_format_and_metadata_start_at_the_shared_place(self) -> None:
        bulk.command(self.repo, ["cargo", "+nightly", "fmt"])
        metadata: CargoWorkspaceFixture = {"workspace_members": ["fixture"], "packages": [
            {"id": "fixture", "name": "fixture", "manifest_path": str(self.repo / "Cargo.toml"), "dependencies": []}]}
        _ = (self.state / "cargo/stdout").write_text(json.dumps(metadata))
        self.assertEqual(bulk.verification_packages(self.repo, "."), ("fixture",))
        self.assertEqual(self.queue.calls, [("cargo", self.repo), ("cargo", self.repo)])
        self.assertEqual([place.arguments for place in self.queue.places], [
            [("cargo", "+nightly", "fmt")], [("cargo", "metadata", "--no-deps", "--format-version", "1")]])
        self.assertTrue(all(place.closed for place in self.queue.places))

    def test_queue_refusal_stops_each_style_start_with_exit_two(self) -> None:
        from scripts.style import style_plan
        config = self.repo / "lint.conf"
        starts: tuple[tuple[str, Callable[[], object]], ...] = (
            ("rust-analyzer", lambda: bulk.RustAnalyzer(self.repo)),
            ("cargo", lambda: bulk.command(self.repo, ["cargo", "+nightly", "fmt"])),
            ("cargo", lambda: bulk.verification_packages(self.repo, ".")),
            ("cargo", lambda: run.run_command(self.repo, ("cargo", "check"))),
            ("cargo", lambda: style_plan.cargo_format(self.repo)),
        )
        for program, start in starts:
            with self.subTest(program=program, start=start):
                key = f"program_first_peak_gib.{program}"
                self.queue.refusals[program] = f"{config}: {key} is missing or invalid"
                errors = StringIO()
                with redirect_stderr(errors), self.assertRaises(BaseException) as stopped:
                    _ = start()
                self.assertIsInstance(stopped.exception, SystemExit)
                assert isinstance(stopped.exception, SystemExit)
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn(str(config), errors.getvalue())
                self.assertIn(key, errors.getvalue())
        self.assertEqual([program for program, _ in self.queue.calls], [program for program, _ in starts])
        self.assertEqual(self.queue.places, [])

    def test_plan_cargo_format_starts_at_the_shared_place(self) -> None:
        from scripts.style import style_plan
        _ = (self.state / "cargo/stdout").write_text("Formatting src/lib.rs\n")
        result = style_plan.cargo_format(self.repo)
        self.assertEqual(result.visited, frozenset({self.repo / "src/lib.rs"}))
        self.assertEqual(self.queue.calls, [("cargo", self.repo)])
        place, = self.queue.places
        self.assertEqual(place.arguments, [style_plan.CARGO_FMT_CHECK])
        self.assertTrue(place.closed)


if __name__ == "__main__":
    _ = unittest.main()
