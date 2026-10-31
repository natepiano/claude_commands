"""Serial crate lifecycle tests: temporary Git and ledger, fake planner and verification."""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from functools import cached_property
from pathlib import Path
from typing import cast, override
from unittest import mock

import bulk_rename as bulk
import bulk_run as run
import crate_run as crates
import rules
import style_ledger as ledger_api
from approved_renames import BindingRename
from test_bulk_rename import CargoProjectTests


class Commands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.failure: str = ""
        self.move: tuple[Path, str, str] | None = None

    def __call__(self, repo: Path, arguments: Sequence[str]) -> run.CommandResult:
        self.calls.append(tuple(arguments))
        if arguments[0] == "bash" and self.failure and self.failure in str(repo):
            return run.CommandResult(1, "FAIL crate test")
        if arguments[0] == "bash" and arguments[2] == "final-pass" and self.move is not None:
            canonical, destination, commit = self.move
            _ = run.git(canonical, "update-ref", destination, commit)
            self.move = None
        return run.CommandResult(0)


def renamed(checkout: Path, _ledger: ledger_api.Ledger, _base: str,
            selected: run.Selection, availability: run.PlanningFileAvailability,
            _definition: rules.Rule) -> run.PlannedEdits:
    files: list[bulk.FileRenames] = []
    for finding in selected.findings:
        availability.require((finding.path,))
        source = (checkout / finding.path).read_text()
        pair = BindingRename(finding.line, finding.text, finding.before, finding.after)
        files.append(bulk.FileRenames(finding.path, source, source.replace(finding.before, finding.after),
                                     (bulk.KeptRename(bulk.RenameFinding(finding.finding_id, finding.path, pair),
                                                      "let", 0),), ()))
    return run.PlannedEdits(tuple(files), ())


def fixture_execute(checkout: Path, base: str, planned: run.PlannedEdits,
                    intent: run.WorkspaceIntent, runner: run.Runner,
                    boundary: Callable[[Path, str], None], /) -> crates.ValidatedEdits:
    """Write exact fake rename fixtures; this is not the production execution pipeline.

    No formatting, constant extraction, dependency rebuilding or safety proof is simulated.
    Phase 115 must supply the actual bulk_run execution owner before production use.
    """
    fixtures: list[bulk.FileRenames] = []
    for file in planned.files:
        assert isinstance(file, bulk.FileRenames)
        boundary(checkout, file.path)
        fixtures.append(file)
    workspace = run.WorkspaceEdits(checkout, base, tuple(fixtures))
    intent.record_plan(planned)
    for file in fixtures:
        _ = (checkout / file.path).write_text(file.edited)
    kept, refused = run.validate(workspace, runner, {"check_clippy": 0.0}, [], lambda paths: paths)
    refused.update((item.finding_id, item.reason) for item in planned.refusals)
    return crates.ValidatedEdits(tuple(sorted(kept)),
                                 tuple(item.finding.finding_id for file in fixtures
                                       if file.path in kept for item in file.kept), refused)


def available(_repo: Path, _path: str) -> None:
    pass


class CrateRunTests(CargoProjectTests):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = (self.repo / "Cargo.toml").write_text((self.repo / "Cargo.toml").read_text()
                                                 + '\n[workspace]\nmembers=[".", "beta"]\n')
        (self.repo / "beta/src").mkdir(parents=True)
        _ = (self.repo / "beta/Cargo.toml").write_text(
            '[package]\nname="beta"\nversion="0.1.0"\nedition="2021"\n')
        _ = (self.repo / "beta/src/lib.rs").write_bytes((self.repo / "src/lib.rs").read_bytes())
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "two crates")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)

    @cached_property
    def base(self) -> str:
        return self.git("rev-parse", "HEAD")

    @cached_property
    def tree(self) -> str:
        return self.git("rev-parse", "HEAD^{tree}")

    @property
    def destination(self) -> str:
        return "refs/heads/integration-destination"

    @cached_property
    def authority(self) -> ledger_api.Ledger:
        return self.ledger()

    @cached_property
    def definition(self) -> rules.Rule:
        return next(rule for rule in rules.load_rules(self.repo)
                    if rule.name == "prefer-type-named-fields-and-bindings")

    @cached_property
    def commands(self) -> Commands:
        return Commands()

    @cached_property
    def batch(self) -> crates.CrateRun:
        return crates.CrateRun(self.authority, "identified")

    def freeze(self, *, shared_proposal: bool = False, paths: tuple[str, ...] = ("src/lib.rs", "beta/src/lib.rs")) -> None:
        self.authority.ticket_add("read-1", "read", "read", "names", ".", list(paths), [self.definition.rule_id])
        self.authority.ticket_assign("read-1", "main")
        _ = self.authority.ticket_base("read-1")
        for index, path in enumerate(paths):
            proposal = self.root / ("decision.md" if shared_proposal else f"decision-{index}.md")
            _ = proposal.write_text("---\nunit: approved-unit\nplan: approved-plan\n---\nApproved rename\n")
            source = (self.repo / path).read_text()
            finding = self.authority.hold(self.definition.rule_id, path, 2, source.splitlines()[1],
                                          "rename", "old", "number", self.definition.blob, "read-1")
            self.authority.file_held(finding.finding_id, proposal)
        self.authority.ticket_drop("read-1")
        self.batch.create(self.definition, self.tree, self.destination)

    def advance(self, package: str) -> crates.Manifest:
        return self.batch.advance(package, renamed, self.commands, available, fixture_execute)

    def test_one_crate_has_canonical_branch_before_assignment_and_preserves_checkpoint_ancestry(self) -> None:
        self.freeze()
        assign = self.authority.ticket_assign
        def assigned(ticket: str, branch: str) -> None:
            self.assertEqual(self.git("rev-parse", branch), self.base)
            assign(ticket, branch)
        with mock.patch.object(self.authority, "ticket_assign", side_effect=assigned):
            result = self.advance("bulk_sample")
        record = result["crates"]["bulk_sample"]
        assert "tickets" in record and "owner" in record
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        commit = self.batch.result(record)["commit"]
        self.assertEqual(self.git("merge-base", commit, self.destination), commit)
        self.assertEqual(self.git("rev-parse", record["branch"]), commit)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual([args[2] for args in self.commands.calls if args[0] == "bash"],
                         ["final", "lint", "final-pass"] * 2)
        self.assertTrue(all(args[2:4] == ("--workspace", "--all-targets")
                            for args in self.commands.calls if args[0] == "cargo" and args[1] in {"check", "clippy"}))
        for ticket in set(record["tickets"].values()):
            self.assertEqual(self.authority.ticket_base(ticket), self.base)

    def test_two_crates_integrate_serially_without_reselecting(self) -> None:
        self.freeze()
        first = self.advance("bulk_sample")
        first_record = first["crates"]["bulk_sample"]
        self.assertTrue(Path(first_record["result_path"]).exists())
        checkpoint = self.batch.result(first_record)["commit"]
        with mock.patch.object(run, "select", side_effect=AssertionError("must not reselect")):
            result = self.advance("beta")
        second_record = result["crates"]["beta"]
        self.assertTrue(Path(second_record["result_path"]).exists())
        second = self.batch.result(second_record)["commit"]
        self.assertEqual(result["crates"]["beta"]["stage"], crates.CrateStage.INTEGRATED)
        for commit in (checkpoint, second):
            self.assertEqual(self.git("merge-base", commit, self.destination), commit)
        for path in ("src/lib.rs", "beta/src/lib.rs"):
            self.assertIn("let number", self.git("show", self.destination + ":" + path))

    def test_proposal_spanning_two_crates_is_refused_whole_without_claims(self) -> None:
        self.freeze(shared_proposal=True)
        self.assertEqual(len(self.batch.read()["refusals"]), 2)
        self.assertTrue(all(not record["selected"] for record in self.batch.read()["crates"].values()))
        result = self.advance("beta")
        self.assertEqual(result["crates"]["beta"]["stage"], crates.CrateStage.ABANDONED)
        self.assertEqual(self.commands.calls, [])

    def test_one_crate_failure_releases_only_its_claims_and_the_other_proceeds(self) -> None:
        self.freeze()
        failures = 0
        def failing(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            nonlocal failures
            if arguments[0] == "bash" and failures == 0:
                failures += 1
                return run.CommandResult(1, "FAIL crate")
            return self.commands(repo, arguments)
        with mock.patch.object(self.batch, "import_result", side_effect=SystemExit("beta tested")):
            with self.assertRaises(SystemExit):
                _ = self.advance("beta")
        beta = self.batch.read()["crates"]["beta"]
        assert "owner" in beta and "tickets" in beta
        with mock.patch.object(self.authority, "ticket_drop_batch", wraps=self.authority.ticket_drop_batch) as dropped:
            result = self.batch.advance("bulk_sample", renamed, failing, available, fixture_execute)
            failed = result["crates"]["bulk_sample"]
            self.assertEqual(failed["stage"], crates.CrateStage.ABANDONED)
            assert "owner" in failed and "tickets" in failed
            self.assertEqual(dropped.call_args_list,
                             [mock.call(ticket, ledger_api.BatchOwner(failed["owner"]))
                              for ticket in sorted(set(failed["tickets"].values()))])
        for finding in self.authority.named_held(failed["tickets"]):
            self.assertIsInstance(finding.build_ticket, ledger_api.NoBuildTicket)
        self.assertTrue(all(self.authority.ticket_batch_owner(ticket) == ledger_api.BatchOwner(beta["owner"])
                            for ticket in set(beta["tickets"].values())))
        result = self.advance("beta")
        self.assertEqual(result["crates"]["beta"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(self.git("show", self.destination + ":src/lib.rs"), (self.repo / "src/lib.rs").read_text().strip())

    def test_interrupted_checkpoint_publication_resumes_by_id_without_planning_or_tests(self) -> None:
        self.freeze()
        share = self.authority.bulk_share_checkpoint
        with mock.patch.object(self.authority, "bulk_share_checkpoint", side_effect=RuntimeError("publication interrupted")):
            with self.assertRaisesRegex(RuntimeError, "publication interrupted"):
                _ = self.advance("bulk_sample")
        pending = self.batch.read()["crates"]["bulk_sample"]
        assert "owner" in pending and "tickets" in pending
        self.assertEqual(pending["stage"], crates.CrateStage.IMPORTED)
        self.assertTrue(all(self.authority.ticket_batch_owner(ticket) == ledger_api.BatchOwner(pending["owner"])
                            for ticket in set(pending["tickets"].values())))
        before = len(self.commands.calls)
        with mock.patch.object(run, "select", side_effect=AssertionError("reselection")), mock.patch.object(
                self.authority, "bulk_share_checkpoint", side_effect=share):
            resumed = crates.CrateRun(self.authority, "identified")
            result = resumed.advance("bulk_sample", cast(crates.Planner, mock.Mock(side_effect=AssertionError("planning"))),
                                     self.commands, available, fixture_execute)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual([args[2] for args in self.commands.calls[before:] if args[0] == "bash"],
                         ["final", "lint", "final-pass"])

    def test_destination_moving_during_combined_validation_refuses_cas_and_keeps_pending_claims(self) -> None:
        self.freeze()
        moved = self.git("commit-tree", self.tree, "-p", self.base, "-m", "destination moved")
        proof = run.test_tree
        calls = 0
        def testing(repo: Path, commit: str, tree: str, runner: run.Runner,
                    times: dict[str, float]) -> run.CommandResult:
            nonlocal calls
            calls += 1
            result = proof(repo, commit, tree, runner, times)
            if calls == 2:
                _ = self.git("update-ref", self.destination, moved, self.base)
            return result
        with mock.patch.object(run, "test_tree", side_effect=testing):
            with self.assertRaisesRegex(bulk.BulkRenameError, "cannot lock ref"):
                _ = self.advance("bulk_sample")
        self.assertEqual(self.git("rev-parse", self.destination), moved)
        record = self.batch.read()["crates"]["bulk_sample"]
        assert "tickets" in record and "owner" in record
        self.assertEqual(record["stage"], crates.CrateStage.CHECKPOINT_PUBLISHED)
        self.assertTrue(self.batch.result(record)["commit"])
        self.assertTrue(all(self.authority.ticket_batch_owner(ticket) == ledger_api.BatchOwner(record["owner"])
                            for ticket in set(record["tickets"].values())))

    def test_late_result_from_a_replaced_owner_is_rejected_before_import(self) -> None:
        self.freeze()
        with mock.patch.object(self.batch, "import_result", side_effect=RuntimeError("stop after test")):
            with self.assertRaisesRegex(RuntimeError, "stop after test"):
                _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        assert "tickets" in record and "owner" in record
        with mock.patch.object(self.authority, "ticket_batch_owner", return_value=ledger_api.BatchOwner("replacement")):
            with self.assertRaisesRegex(bulk.BulkRenameError, "late result"):
                _ = self.advance("bulk_sample")
        self.assertEqual(self.git("rev-parse", record["branch"]), self.base)
        self.assertEqual(self.git("rev-parse", self.destination), self.base)

    def test_shared_include_str_and_path_sources_refuse_complete_group_before_overlay(self) -> None:
        for loader in ('const SOURCE: &str = include_str!("../../src/lib.rs");\n',
                       '#[path="../../src/lib.rs"] mod shared;\n',
                       'include!("../../src/lib.rs");\n'):
            with self.subTest(loader=loader):
                _ = (self.repo / "beta/src/lib.rs").write_text(loader)
                _ = self.git("add", ".")
                _ = self.git("commit", "-qm", "shared loader")
                members = rules.workspace_members({"Cargo.toml": (self.repo / "Cargo.toml").read_text(),
                                                   "beta/Cargo.toml": (self.repo / "beta/Cargo.toml").read_text()})
                owners = crates.SourceOwnership(self.repo, self.git("rev-parse", "HEAD"), members)
                with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                    owners.require(".", ("src/lib.rs",))
                with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                    owners.require("beta", ("beta/src/lib.rs",))

    def test_same_crate_source_loader_remains_owned(self) -> None:
        _ = (self.repo / "src/local.rs").write_text("pub fn local() {}\n")
        _ = (self.repo / "src/lib.rs").write_text('include_str!("local.rs");\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "same crate loader")
        members = rules.workspace_members({"Cargo.toml": (self.repo / "Cargo.toml").read_text(),
                                           "beta/Cargo.toml": (self.repo / "beta/Cargo.toml").read_text()})
        owners = crates.SourceOwnership(self.repo, self.git("rev-parse", "HEAD"), members)
        owners.require(".", ("src/lib.rs", "src/local.rs"))
        self.assertEqual(owners.owners["src/local.rs"], {"."})

    def test_final_diff_checks_creations_deletions_modes_and_new_shared_loaders(self) -> None:
        self.freeze()
        _ = self.git("update-index", "--chmod=+x", "src/lib.rs")
        _ = (self.repo / "src/new.rs").write_text("pub fn new() {}\n")
        _ = self.git("add", "src/new.rs")
        _ = self.git("rm", "beta/src/lib.rs")
        tree = self.git("write-tree")
        self.assertEqual(set(crates.changed(self.repo, self.base, tree)),
                         {"src/lib.rs", "src/new.rs", "beta/src/lib.rs"})
        owners = crates.SourceOwnership(self.repo, self.base, rules.workspace_members({
            "Cargo.toml": (self.repo / "Cargo.toml").read_text(),
            "beta/Cargo.toml": (self.repo / "beta/Cargo.toml").read_text()}))
        with self.assertRaises(bulk.BulkRenameError):
            owners.require(".", crates.changed(self.repo, self.base, tree))

    def test_failed_combination_retains_tested_commit_and_resumes_publication(self) -> None:
        self.freeze()
        validate = run.validate
        calls = 0
        def validation(workspace: run.WorkspaceEdits, runner: run.Runner, times: dict[str, float],
                       loops: list[dict[str, object]],
                       reprove_remaining: Callable[[set[str]], set[str]]) -> tuple[set[str], dict[str, str]]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise bulk.BulkRenameError("integration failed")
            return validate(workspace, runner, times, loops, reprove_remaining)
        with mock.patch.object(run, "validate", side_effect=validation):
            with self.assertRaisesRegex(bulk.BulkRenameError, "integration failed"):
                _ = self.advance("bulk_sample")
        pending = self.batch.read()["crates"]["bulk_sample"]
        assert "owner" in pending and "tickets" in pending
        self.assertEqual(pending["stage"], crates.CrateStage.CHECKPOINT_PUBLISHED)
        self.assertEqual(self.git("rev-parse", self.destination), self.base)
        result = self.advance("bulk_sample")
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def test_manifest_pins_exact_selection_rule_and_result_paths(self) -> None:
        self.freeze()
        serialized = cast(object, json.loads(self.batch.path.read_text()))
        self.assertIsInstance(serialized, dict)
        manifest = self.batch.read()
        self.assertEqual(manifest["base"], self.base)
        self.assertEqual(manifest["rule_blob"], self.definition.blob)
        self.assertEqual(manifest["rule_text"], self.definition.path.read_text())
        self.assertTrue(all(record["selected"] and record["result_path"] for record in manifest["crates"].values()))

    def test_staged_interruption_restores_original_journal_then_replans(self) -> None:
        self.freeze()
        git = run.git
        def interrupted(repo: Path, *arguments: str) -> str:
            if arguments[0] == "commit-tree" and repo != self.repo:
                raise SystemExit("interrupted after staging")
            return git(repo, *arguments)
        with mock.patch.object(run, "git", side_effect=interrupted):
            with self.assertRaisesRegex(SystemExit, "interrupted after staging"):
                _ = self.advance("bulk_sample")
        pending = self.batch.read()["crates"]["bulk_sample"]
        self.assertEqual(pending["stage"], crates.CrateStage.EDITED)
        journal = bulk.object_fields(cast(object, json.loads(Path(pending["intent"]).read_text())))
        edit = bulk.object_fields(bulk.object_fields(journal["files"])["src/lib.rs"])
        self.assertEqual(bytes.fromhex(str(edit["before"])), (self.repo / "src/lib.rs").read_bytes())
        self.assertNotEqual(edit["before"], edit["after"])
        result = self.advance("bulk_sample")
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def test_commit_prepared_interruption_resumes_without_replanning(self) -> None:
        self.freeze()
        save = self.batch.save
        def interrupted(manifest: crates.Manifest) -> None:
            save(manifest)
            if manifest["crates"]["bulk_sample"]["stage"] == crates.CrateStage.COMMITTED:
                raise SystemExit("commit durable")
        with mock.patch.object(self.batch, "save", side_effect=interrupted):
            with self.assertRaisesRegex(SystemExit, "commit durable"):
                _ = self.advance("bulk_sample")
        with mock.patch.object(run, "select", side_effect=AssertionError("no selection")):
            result = self.batch.advance("bulk_sample", cast(crates.Planner, mock.Mock(
                side_effect=AssertionError("no replanning"))), self.commands, available, fixture_execute)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def test_worker_edit_after_commit_preparation_is_preserved_and_refused(self) -> None:
        self.freeze()
        save = self.batch.save
        def interrupted(manifest: crates.Manifest) -> None:
            save(manifest)
            if manifest["crates"]["bulk_sample"]["stage"] == crates.CrateStage.COMMITTED:
                raise SystemExit("commit durable")
        with mock.patch.object(self.batch, "save", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        source = Path(record["checkout"]) / "src/lib.rs"
        _ = source.write_text("// unrecorded work\n")
        with self.assertRaisesRegex(bulk.BulkRenameError, "worker index or files changed"):
            _ = self.advance("bulk_sample")
        self.assertEqual(source.read_text(), "// unrecorded work\n")

    def test_interruption_after_destination_cas_reuses_durable_combined_proof(self) -> None:
        self.freeze()
        save = self.batch.save
        def interrupted(manifest: crates.Manifest) -> None:
            if manifest["crates"]["bulk_sample"]["stage"] == crates.CrateStage.INTEGRATED:
                raise SystemExit("published before final manifest")
            save(manifest)
        with mock.patch.object(self.batch, "save", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                _ = self.advance("bulk_sample")
        before = len(self.commands.calls)
        result = self.advance("bulk_sample")
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(len(self.commands.calls), before)

    def test_claim_interrupted_before_write_ahead_intent_can_be_abandoned(self) -> None:
        self.freeze()
        with mock.patch.object(run, "claim", side_effect=SystemExit("before journal")):
            with self.assertRaises(SystemExit):
                _ = self.advance("bulk_sample")
        self.assertEqual(self.batch.read()["crates"]["bulk_sample"]["stage"], crates.CrateStage.CLAIMING)
        result = self.advance("bulk_sample")
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.ABANDONED)
        self.assertEqual(self.advance("beta")["crates"]["beta"]["stage"], crates.CrateStage.INTEGRATED)

    def test_shared_mounted_modules_children_are_also_refused(self) -> None:
        _ = (self.repo / "src/child.rs").write_text("pub fn child() {}\n")
        _ = (self.repo / "src/lib.rs").write_text("mod child;\n")
        _ = (self.repo / "beta/src/lib.rs").write_text('#[path="../../src/lib.rs"] mod shared;\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "shared module and child")
        owners = crates.SourceOwnership(self.repo, self.git("rev-parse", "HEAD"),
                                        rules.workspace_members({
            "Cargo.toml": (self.repo / "Cargo.toml").read_text(),
            "beta/Cargo.toml": (self.repo / "beta/Cargo.toml").read_text()}))
        with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
            owners.require(".", ("src/child.rs",))

    def test_shared_source_boundary_runs_before_planner_overlay(self) -> None:
        _ = (self.repo / "beta/src/lib.rs").write_text('include_str!("../../src/lib.rs");\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "shared source")
        self.base = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(paths=("src/lib.rs",))
        result = self.advance("bulk_sample")
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.ABANDONED)
        self.assertIn("shared or cross-crate", record.get("error", ""))
        self.assertEqual(self.commands.calls, [])
        self.assertEqual((Path(record["checkout"]) / "src/lib.rs").read_bytes(),
                         (self.repo / "src/lib.rs").read_bytes())

    def test_partial_refusal_publication_resumes_without_releasing_checkpointed_ticket(self) -> None:
        _ = (self.repo / "src/second.rs").write_bytes((self.repo / "src/lib.rs").read_bytes())
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "two finding files")
        self.base = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(shared_proposal=True, paths=("src/lib.rs", "src/second.rs"))
        def partial(checkout: Path, ledger: ledger_api.Ledger, base: str,
                    selected: run.Selection, availability: run.PlanningFileAvailability,
                    definition: rules.Rule) -> run.PlannedEdits:
            accepted = tuple(item for item in selected.findings if item.path == "src/lib.rs")
            refused = tuple(bulk.Refusal(item.finding_id, "unit refusal") for item in selected.findings
                            if item.path != "src/lib.rs")
            planned = renamed(checkout, ledger, base, run.Selection(accepted, ()), availability, definition)
            return run.PlannedEdits(planned.files, refused)
        with mock.patch.object(self.authority, "bulk_share_checkpoint", side_effect=SystemExit("checkpoint stopped")):
            with self.assertRaises(SystemExit):
                _ = self.batch.advance("bulk_sample", partial, self.commands, available, fixture_execute)
        pending = self.batch.read()["crates"]["bulk_sample"]
        assert "tickets" in pending
        self.assertEqual(len(self.batch.result(pending)["refusals"]), 1)
        ticket = next(iter(pending["tickets"].values()))
        self.assertEqual(len(self.authority.bulk_refusals(ticket)), 1)
        with mock.patch.object(self.authority, "bulk_release", side_effect=AssertionError("already released")):
            result = self.advance("bulk_sample")
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def ownership(self) -> crates.SourceOwnership:
        return crates.SourceOwnership(self.repo, self.git("rev-parse", "HEAD"), rules.workspace_members({
            "Cargo.toml": (self.repo / "Cargo.toml").read_text(),
            "beta/Cargo.toml": (self.repo / "beta/Cargo.toml").read_text()}))

    def test_include_words_used_as_data_are_not_loaders(self) -> None:
        _ = (self.repo / "beta/src/lib.rs").write_text(
            '\n'.join(('use std::include_str as source;',
                       'fn inspect(include: &str) {',
                       '    let _ = include == "include!(dynamic) #[path = dynamic]";',
                       '    let _ = object.include; object.include_str();',
                       '    // include!(dynamic) #[path = dynamic]',
                       '}', 'const RAW: &str = r#"include_bytes!(unknown)"#;')))
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "loader names as data")
        owners = self.ownership()
        self.assertEqual(dict(owners.uncertain), {})
        owners.require(".", ("src/lib.rs",))
        owners.require("beta", ("beta/src/lib.rs",))

    def test_unreachable_uncertainty_does_not_refuse_selected_crate(self) -> None:
        _ = (self.repo / "beta/src/lib.rs").write_text('include!(concat!(env!("OUT_DIR"), "/code.rs"));\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "unreachable loader")
        owners = self.ownership()
        owners.require(".", ("src/lib.rs",))
        self.assertNotIn("beta/src/lib.rs", owners.reachable(("src/lib.rs",)))
        with self.assertRaisesRegex(bulk.BulkRenameError, "beta/src/lib.rs: nonliteral include"):
            owners.require("beta", ("beta/src/lib.rs",))

    def test_literal_edge_reaches_uncertainty_but_unrelated_file_stays_owned(self) -> None:
        _ = (self.repo / "src/uncertain.rs").write_text('include_bytes!(dynamic);\n')
        _ = (self.repo / "src/loader.rs").write_text('include_str!("uncertain.rs");\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "reachable uncertainty")
        owners = self.ownership()
        owners.require(".", ("src/lib.rs",))
        self.assertIn("src/uncertain.rs", owners.reachable(("src/loader.rs",)))
        with self.assertRaisesRegex(bulk.BulkRenameError, "src/uncertain.rs: nonliteral include_bytes"):
            owners.require(".", ("src/loader.rs",))

    def test_reachable_uncertain_file_is_refused_alone_and_published_to_ledger(self) -> None:
        _ = (self.repo / "src/uncertain.rs").write_text(
            (self.repo / "src/lib.rs").read_text() + 'include!(dynamic);\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "uncertain finding file")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(shared_proposal=True, paths=("src/lib.rs", "src/uncertain.rs"))
        result = self.advance("bulk_sample")
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)
        proof = self.batch.result(record)
        self.assertEqual(proof["paths"], ["src/lib.rs"])
        self.assertEqual(len(proof["findings"]), 1)
        self.assertEqual(len(proof["refusals"]), 1)
        reason = next(iter(proof["refusals"].values()))
        self.assertIn("src/uncertain.rs: nonliteral include", reason)
        assert "tickets" in record
        ticket = next(iter(record["tickets"].values()))
        self.assertEqual([row.reason for row in self.authority.bulk_refusals(ticket)], [reason])
        self.assertIn("let number", self.git("show", self.destination + ":src/lib.rs"))
        self.assertIn("let old", self.git("show", self.destination + ":src/uncertain.rs"))

    def test_final_uncertain_loader_refuses_only_its_edit_group(self) -> None:
        _ = (self.repo / "src/second.rs").write_bytes((self.repo / "src/lib.rs").read_bytes())
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "two finding files")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(shared_proposal=True, paths=("src/lib.rs", "src/second.rs"))
        def loader(checkout: Path, base: str, planned: run.PlannedEdits,
                   intent: run.WorkspaceIntent, runner: run.Runner,
                   boundary: Callable[[Path, str], None], /) -> crates.ValidatedEdits:
            result = fixture_execute(checkout, base, planned, intent, runner, boundary)
            if "src/second.rs" in result.paths:
                path = checkout / "src/second.rs"
                _ = path.write_text(path.read_text() + 'include_str!(dynamic);\n')
            return result
        result = self.batch.advance("bulk_sample", renamed, self.commands, available, loader)
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)
        proof = self.batch.result(record)
        self.assertEqual(proof["paths"], ["src/lib.rs"])
        self.assertEqual(len(proof["refusals"]), 1)
        self.assertIn("src/second.rs: nonliteral include_str", next(iter(proof["refusals"].values())))
        self.assertEqual(self.git("show", self.destination + ":src/second.rs"),
                         self.git("show", self.base + ":src/second.rs"))

    def test_package_named_integration_has_distinct_retained_refs_after_resume(self) -> None:
        manifest_path = self.repo / "beta/Cargo.toml"
        _ = manifest_path.write_text(manifest_path.read_text().replace('name="beta"', 'name="integration"'))
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "legal package called integration")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(paths=("beta/src/lib.rs",))
        with mock.patch.object(self.batch, "integrate", side_effect=SystemExit("before integration")):
            with self.assertRaises(SystemExit):
                _ = self.advance("integration")
        result = self.advance("integration")
        record = result["crates"]["integration"]
        commit = self.batch.result(record)["commit"]
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(record["branch"], "refs/heads/style-crate/identified/workers/integration")
        self.assertNotEqual(record["branch"], result["integration_ref"])
        self.assertEqual(self.git("rev-parse", record["branch"]), commit)
        self.assertEqual(self.git("rev-parse", result["integration_ref"]), self.git("rev-parse", self.destination))
        self.assertEqual(self.git("rev-parse", self.destination + "^{tree}"), self.batch.result(record)["tree"])
        self.assertEqual(self.git("merge-base", commit, self.destination), commit)

    def test_ownership_uses_one_ledger_inventory_and_one_batched_blob_read(self) -> None:
        members = rules.workspace_members({"Cargo.toml": (self.repo / "Cargo.toml").read_text(),
                                           "beta/Cargo.toml": (self.repo / "beta/Cargo.toml").read_text()})
        with mock.patch.object(ledger_api, "tree_entries", wraps=ledger_api.tree_entries) as inventory, mock.patch.object(
                ledger_api, "blob_contents", wraps=ledger_api.blob_contents) as contents, mock.patch.object(
                run, "git", side_effect=AssertionError("no per-file git show")):
            owners = crates.SourceOwnership(self.repo, self.tree, members)
        self.assertEqual(inventory.call_count, 1)
        self.assertEqual(contents.call_count, 1)
        self.assertEqual(owners.owners["src/lib.rs"], {"."})

    def test_default_execution_uses_bulk_run_owner_and_retains_ancestry(self) -> None:
        self.freeze()
        with mock.patch.object(run, "execute_plan", wraps=run.execute_plan) as execute:
            result = self.batch.advance("bulk_sample", renamed, self.commands, available)
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.git("merge-base", self.batch.result(record)["commit"], self.destination),
                         self.batch.result(record)["commit"])
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("let number", self.git("show", self.destination + ":src/lib.rs"))

    def test_owned_execution_rebuilds_formatter_survivors_and_refreshes_journal(self) -> None:
        extra = self.repo / "src/extra.rs"
        _ = extra.write_bytes((self.repo / "src/lib.rs").read_bytes())
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "second file in crate")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(paths=("src/lib.rs", "src/extra.rs"))
        planned: list[tuple[str, ...]] = []
        def planner(checkout: Path, ledger: ledger_api.Ledger, base: str, selected: run.Selection,
                    availability: run.PlanningFileAvailability, definition: rules.Rule) -> run.PlannedEdits:
            planned.append(tuple(finding.path for finding in selected.findings))
            return renamed(checkout, ledger, base, selected, availability, definition)
        def formatter(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            if "fmt" in arguments and len(planned) == 1:
                _ = (repo / "src/extra.rs").write_text("pub fn changed() {}\n")
            return self.commands(repo, arguments)
        with mock.patch.object(run.WorkspaceIntent, "record_plan", autospec=True,
                               side_effect=run.WorkspaceIntent.record_plan) as journal:
            result = self.batch.advance("bulk_sample", planner, formatter, available)
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(planned, [("src/extra.rs", "src/lib.rs"), ("src/lib.rs",)])
        self.assertEqual(journal.call_count, 2)
        evidence = self.batch.result(record)
        self.assertEqual(evidence["paths"], ["src/lib.rs"])
        self.assertEqual(len(evidence["refusals"]), 1)
        self.assertEqual(self.git("show", self.destination + ":src/extra.rs"), extra.read_text().strip())
        self.assertIn("let number", self.git("show", self.destination + ":src/lib.rs"))

    def test_executor_receives_complete_plan_before_any_write_and_journals_once(self) -> None:
        self.freeze()
        calls: list[str] = []
        def executing(checkout: Path, base: str, planned: run.PlannedEdits,
                      intent: run.WorkspaceIntent, runner: run.Runner,
                      boundary: Callable[[Path, str], None], /) -> crates.ValidatedEdits:
            self.assertEqual(run.git(checkout, "status", "--porcelain"), "")
            for file in planned.files:
                self.assertEqual((checkout / file.path).read_text(), file.original)
            calls.extend(file.path for file in planned.files)
            with mock.patch.object(intent, "record_plan", wraps=intent.record_plan) as journal:
                result = fixture_execute(checkout, base, planned, intent, runner, boundary)
                self.assertEqual(journal.call_count, 1)
                return result
        result = self.batch.advance("bulk_sample", renamed, self.commands, available, executing)
        self.assertEqual(calls, ["src/lib.rs"])
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def test_result_publication_interruption_before_atomic_write_replans(self) -> None:
        self.freeze()
        atomic = bulk.atomic_json
        def stopped(path: Path, value: object) -> None:
            if path.name.endswith(".result.json"):
                raise SystemExit("before result write")
            atomic(path, value)
        with mock.patch.object(bulk, "atomic_json", side_effect=stopped):
            with self.assertRaisesRegex(SystemExit, "before result write"):
                _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.EDITED)
        self.assertNotIn("evidence", record)
        self.assertFalse(Path(record["result_path"]).exists())
        with mock.patch.object(self.batch, "execute", wraps=self.batch.execute) as execute:
            result = self.advance("bulk_sample")
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def test_result_publication_interruption_after_atomic_write_resumes_without_planning(self) -> None:
        self.freeze()
        atomic = bulk.atomic_json
        def stopped(path: Path, value: object) -> None:
            atomic(path, value)
            if path.name.endswith(".result.json"):
                raise SystemExit("after result write")
        with mock.patch.object(bulk, "atomic_json", side_effect=stopped):
            with self.assertRaisesRegex(SystemExit, "after result write"):
                _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.EDITED)
        authoritative = Path(record["result_path"]).read_bytes()
        self.assertNotIn("evidence", record)
        with mock.patch.object(self.batch, "execute", side_effect=AssertionError("must use result file")):
            result = self.advance("bulk_sample")
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(Path(record["result_path"]).read_bytes(), authoritative)

    def test_missing_or_changed_result_is_refused_without_manifest_fallback(self) -> None:
        self.freeze()
        with mock.patch.object(self.batch, "import_result", side_effect=SystemExit("tested")):
            with self.assertRaises(SystemExit):
                _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        path = Path(record["result_path"])
        authoritative = path.read_bytes()
        for replacement in (None, b"{}", authoritative.replace(b'"tree": "', b'"tree": "bad')):
            with self.subTest(replacement=replacement):
                if replacement is None:
                    path.unlink()
                else:
                    _ = path.write_bytes(replacement)
                with self.assertRaisesRegex(bulk.BulkRenameError, "invalid authoritative crate result"):
                    _ = self.advance("bulk_sample")
                self.assertEqual(self.batch.read()["crates"]["bulk_sample"]["stage"], crates.CrateStage.TESTED)
                self.assertEqual(self.git("rev-parse", record["branch"]), self.base)
                self.assertEqual(self.git("rev-parse", self.destination), self.base)
                _ = path.write_bytes(authoritative)
        self.assertEqual(self.advance("bulk_sample")["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def assert_pending(self, record: crates.CrateRecord) -> None:
        self.assertEqual(record["stage"], crates.CrateStage.CHECKPOINT_PUBLISHED)
        assert "owner" in record and "tickets" in record
        self.assertTrue(all(self.authority.ticket_batch_owner(ticket) == ledger_api.BatchOwner(record["owner"])
                            for ticket in set(record["tickets"].values())))
        self.assertEqual(self.git("rev-parse", self.destination), self.base)

    def test_moved_canonical_worker_ref_refuses_import_and_preserves_tested_claims(self) -> None:
        self.freeze()
        with mock.patch.object(self.batch, "import_result", side_effect=SystemExit("tested")):
            with self.assertRaises(SystemExit):
                _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        moved = self.git("commit-tree", self.tree, "-p", self.base, "-m", "moved worker")
        _ = self.git("update-ref", record["branch"], moved, self.base)
        with self.assertRaisesRegex(bulk.BulkRenameError, "cannot lock ref"):
            _ = self.advance("bulk_sample")
        self.assertEqual(self.batch.read()["crates"]["bulk_sample"]["stage"], crates.CrateStage.TESTED)
        self.assertEqual(self.git("rev-parse", record["branch"]), moved)
        self.assertEqual(self.git("rev-parse", self.destination), self.base)
        assert "tickets" in record and "owner" in record
        self.assertTrue(all(self.authority.ticket_batch_owner(ticket) == ledger_api.BatchOwner(record["owner"])
                            for ticket in set(record["tickets"].values())))

    def test_checked_out_destination_refuses_publication_and_retains_pending_claims(self) -> None:
        self.freeze()
        _ = self.git("worktree", "add", str(self.root / "publication"), self.destination.removeprefix("refs/heads/"))
        with self.assertRaisesRegex(bulk.BulkRenameError, "destination is checked out"):
            _ = self.advance("bulk_sample")
        self.assert_pending(self.batch.read()["crates"]["bulk_sample"])

    def test_combined_test_failure_retains_pending_claims_for_resume(self) -> None:
        self.freeze()
        proof = run.test_tree
        def failed(repo: Path, commit: str, tree: str, runner: run.Runner,
                   times: dict[str, float]) -> run.CommandResult:
            if repo.name.startswith("integration-"):
                return run.CommandResult(1, "combined proof failure")
            return proof(repo, commit, tree, runner, times)
        with mock.patch.object(run, "test_tree", side_effect=failed):
            with self.assertRaisesRegex(bulk.BulkRenameError, "combined-tree proof failed"):
                _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        self.assert_pending(record)
        assert "integration" in record
        self.assertEqual(record["integration"]["stage"], crates.IntegrationStage.PREPARED)
        self.assertEqual(self.advance("bulk_sample")["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def test_exact_union_mismatch_retains_pending_claims_and_refs(self) -> None:
        self.freeze()
        git = run.git
        def wrong_tree(repo: Path, *arguments: str) -> str:
            if arguments[0] == "merge-tree":
                return self.tree
            return git(repo, *arguments)
        with mock.patch.object(run, "git", side_effect=wrong_tree):
            with self.assertRaisesRegex(bulk.BulkRenameError, "differs from the exact edit union"):
                _ = self.advance("bulk_sample")
        self.assert_pending(self.batch.read()["crates"]["bulk_sample"])

    def test_final_tree_new_loader_is_refused_through_execute(self) -> None:
        self.freeze()
        def new_loader(checkout: Path, base: str, planned: run.PlannedEdits,
                       intent: run.WorkspaceIntent, runner: run.Runner,
                       boundary: Callable[[Path, str], None], /) -> crates.ValidatedEdits:
            result = fixture_execute(checkout, base, planned, intent, runner, boundary)
            # Simulate a bad execution owner whose final tree introduces a shared loader.
            path = checkout / "src/lib.rs"
            _ = path.write_text(path.read_text() + 'include_str!("../beta/src/lib.rs");\n')
            return result
        with mock.patch.object(run, "git", wraps=run.git) as git:
            result = self.batch.advance("bulk_sample", renamed, self.commands, available, new_loader)
        self.assertFalse(any(call.args[1] == "commit-tree" for call in git.call_args_list))
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.ABANDONED)
        self.assertIn("shared or cross-crate", record.get("error", ""))
        self.assertEqual(self.git("rev-parse", self.destination), self.base)

    def test_mode_only_shared_source_change_is_refused(self) -> None:
        _ = (self.repo / "beta/src/lib.rs").write_text('include_str!("../../src/lib.rs");\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "shared loader")
        base = self.git("rev-parse", "HEAD")
        _ = self.git("update-index", "--chmod=+x", "src/lib.rs")
        tree = self.git("write-tree")
        self.assertEqual(crates.changed(self.repo, base, tree), ("src/lib.rs",))
        members = rules.workspace_members({"Cargo.toml": (self.repo / "Cargo.toml").read_text(),
                                           "beta/Cargo.toml": (self.repo / "beta/Cargo.toml").read_text()})
        with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
            crates.SourceOwnership(self.repo, tree, members).require(".", crates.changed(self.repo, base, tree))

    def test_overlap_refuses_integration_without_moving_retained_refs(self) -> None:
        self.freeze()
        with mock.patch.object(self.batch, "integrate", side_effect=SystemExit("checkpoint")):
            with self.assertRaises(SystemExit):
                _ = self.advance("bulk_sample")
        manifest = self.batch.read()
        record = manifest["crates"]["bulk_sample"]
        worker = self.batch.result(record)["commit"]
        # A resumed integration already contains a different edit of this same path.
        _ = (self.repo / "src/lib.rs").write_text("pub fn conflicting() {}\n")
        _ = self.git("add", "src/lib.rs")
        prior_tree = self.git("write-tree")
        prior = self.git("commit-tree", prior_tree, "-p", self.base, "-m", "prior integration")
        _ = self.git("reset", "--hard", self.base)
        _ = self.git("update-ref", self.destination, prior, self.base)
        manifest["expected_tip"] = prior
        self.batch.save(manifest)
        with self.assertRaisesRegex(bulk.BulkRenameError, "overlaps already published paths"):
            _ = self.advance("bulk_sample")
        current = self.batch.read()["crates"]["bulk_sample"]
        self.assertEqual(current["stage"], crates.CrateStage.CHECKPOINT_PUBLISHED)
        self.assertEqual(self.git("rev-parse", record["branch"]), worker)
        self.assertEqual(self.git("rev-parse", self.destination), prior)
        assert "owner" in record and "tickets" in record
        self.assertTrue(all(self.authority.ticket_batch_owner(ticket) == ledger_api.BatchOwner(record["owner"])
                            for ticket in set(record["tickets"].values())))
