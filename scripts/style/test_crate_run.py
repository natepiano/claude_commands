"""Serial crate lifecycle tests: temporary Git and ledger, fake planner and verification."""
from __future__ import annotations

from scripts.style.ledger_test_support import ticket_owner_id

import json
import io
import sqlite3
import shutil
import os
import sys
import tempfile
from dataclasses import replace
from contextlib import closing, redirect_stderr
from collections.abc import Callable, Sequence
from functools import cached_property
from pathlib import Path
from typing import ClassVar, cast, override
from unittest import mock


from scripts.style import bulk_rename as bulk
from scripts.lib import berth_board
from scripts.style import bulk_run as run
from scripts.style import crate_run as crates
from scripts.style import coverage_report
from scripts.style import rules
from scripts.style import style_ledger as ledger_api
from scripts.style.approved_renames import BindingRename
from scripts.style.ledger_test_support import machine_rename_fixture
from scripts.style.test_bulk_rename import ScriptedServer, word_edits, CargoProjectTests
from scripts.style.style_child_test_support import StyleQueueFake, install_refusal_tools, unqueued_style_children
from scripts.build_queue import queue_place
from scripts.copies import copies
from scripts.lib import copy_record, scratch
from scripts.production.staff_test_support import fixture_member, isolated_staff_file


class Commands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.failure: str = ""
        self.move: tuple[Path, str, str] | None = None

    def __call__(self, repo: Path, arguments: Sequence[str]) -> run.CommandResult:
        self.calls.append(tuple(arguments))
        if arguments[0] == "bash" and self.failure and self.failure in str(repo):
            return run.CommandResult(1, "FAIL crate test")
        if arguments[0] == "bash" and tuple(arguments[2:4]) == ("final", "--saved") and self.move is not None:
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
                                     (bulk.KeptRename(bulk.RenameFinding(finding.finding_id, finding.path, pair, _ledger.rename_authority(finding), _ledger.finding_input_identity(finding)),
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
    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture_seed = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="crate-run-seed-"))) / "seed"
        fixture = cls(methodName="runTest")
        environment = os.environ.copy()
        try:
            fixture.prepare_fixture()
            cls.fixture_root = fixture.root
            cls.fixture_environment = {key: value for key, value in os.environ.items()
                                       if environment.get(key) != value}
            fixture.authority.close()
            _ = shutil.copytree(fixture.root, cls.fixture_seed, symlinks=True)
        finally:
            fixture.doCleanups()

    @override
    def setUp(self) -> None:
        self.root: Path = self.fixture_root
        _ = shutil.copytree(self.fixture_seed, self.root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.repo: Path = self.root / "project"
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.fixture_environment)))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.queue: StyleQueueFake = self.enterContext(unqueued_style_children())
        # Each test has fixed copies settings and keeps its own settings dictionary.
        _ = self.enterContext(mock.patch.object(copies, "config_settings", return_value=copies.config_settings()))
        _ = self.base
        _ = self.authority

    def prepare_fixture(self) -> None:
        """Build the complete initial state through the real fixture writers."""
        super().setUp()
        # Finish Git maintenance before the fixture removes its repositories.
        _ = self.git("config", "maintenance.autoDetach", "false")
        _ = self.git("config", "gc.autoDetach", "false")
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = fixture_member("showrunner", name="style-fixture")
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root / "gate-home"),
            "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
            "SCRATCH_HOLDS": str(self.root / "holds.json"),
        }))
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

    def file_findings(self, *, shared_proposal: bool = False,
                      paths: tuple[str, ...] = ("src/lib.rs", "beta/src/lib.rs")) -> None:
        self.authority.ticket_add("1001", ticket_owner_id("read"), "read", "names", ".", list(paths), [self.definition.rule_id])
        self.authority.ticket_assign("1001", "main")
        _ = self.authority.ticket_base("1001")
        for index, path in enumerate(paths):
            proposal = self.root / ("decision.md" if shared_proposal else f"decision-{index}.md")
            _ = proposal.write_text("---\nunit: approved\nplan: approved-plan\n---\nApproved rename\n")
            source = (self.repo / path).read_text()
            finding = self.authority.hold(self.definition.rule_id, path, 2, source.splitlines()[1],
                                          "rename", "old", "number", self.definition.blob, "1001")
            self.authority.file_held(finding.finding_id, proposal)
        self.authority.ticket_drop("1001")

    def freeze(self, *, shared_proposal: bool = False, paths: tuple[str, ...] = ("src/lib.rs", "beta/src/lib.rs")) -> None:
        self.file_findings(shared_proposal=shared_proposal, paths=paths)
        self.batch.create(self.definition, self.tree, self.destination)

    def advance(self, package: str) -> crates.Manifest:
        return self.batch.advance(package, renamed, self.commands, available, fixture_execute)

    def test_legacy_manifest_adopts_existing_worker_and_creates_missing_worker(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        for existing in (True, False):
            with self.subTest(existing=existing):
                batch = crates.CrateRun(self.authority, "legacy-" + str(existing))
                batch.create(self.definition, self.tree, self.destination)
                manifest = batch.read()
                record = manifest["crates"]["bulk_sample"]
                legacy = batch.folder / "workers/bulk_sample"
                record["checkout"] = str(legacy)
                record["intent"] = str(legacy / ".git/style-batch-intent.json")
                batch.save(manifest)
                if existing:
                    legacy.mkdir(parents=True)
                    copies.initialize_git(legacy, self.repo)
                    _ = run.git(legacy, "checkout", "-q", "-B", record["branch"].removeprefix("refs/heads/"), self.base)
                    _ = (legacy / "src/lib.rs").write_text("staged legacy edit\n")
                    _ = run.git(legacy, "add", "src/lib.rs")
                    _ = (legacy / "pending.txt").write_text("untracked legacy edit\n")
                    _ = Path(record["intent"]).write_text("legacy recovery journal\n")
                with mock.patch.object(run, "WorkspaceIntent", side_effect=RuntimeError("after adoption")):
                    with self.assertRaisesRegex(RuntimeError, "after adoption"):
                        _ = batch.advance("bulk_sample", renamed, self.commands, available, fixture_execute)
                worker = Path(batch.read()["crates"]["bulk_sample"]["checkout"])
                if existing:
                    self.assertEqual(worker, legacy)
                    self.assertEqual(run.git(worker, "diff", "--cached", "--name-only"), "src/lib.rs")
                    self.assertEqual((worker / "pending.txt").read_text(), "untracked legacy edit\n")
                    self.assertEqual(Path(record["intent"]).read_text(), "legacy recovery journal\n")
                    self.assertEqual(scratch.holds()[str(worker)]["task"], batch.task)
                else:
                    self.assertNotEqual(worker, legacy)
                    self.assertIn(worker, list(scratch.copy_paths()))

    def legacy_worker(self) -> Path:
        self.freeze(paths=("src/lib.rs",))
        manifest = self.batch.read()
        record = manifest["crates"]["bulk_sample"]
        worker = self.batch.folder / "workers/bulk_sample"
        record["checkout"] = str(worker)
        record["intent"] = str(worker / ".git/style-batch-intent.json")
        self.batch.save(manifest)
        self.initialize_legacy_worker(worker)
        return worker

    def initialize_legacy_worker(self, worker: Path) -> None:
        worker.mkdir(parents=True)
        copies.initialize_git(worker, self.repo)
        branch = self.batch.read()["crates"]["bulk_sample"]["branch"].removeprefix("refs/heads/")
        _ = run.git(worker, "checkout", "-q", "-B", branch, self.base)
        for key in ("user.name", "user.email"):
            _ = run.git(worker, "config", key, self.git("config", key))

    def finish_run(self) -> crates.Manifest:
        for package in self.batch.read()["crates"]:
            _ = self.advance(package)
        manifest = self.batch.read()
        record = manifest["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED, record.get("error", ""))
        return manifest

    def test_owner_removal_preserves_each_nonterminal_crate_worker(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        for kind in ("native", "adopted", "changed-task", "interrupted-removal"):
            with self.subTest(kind=kind):
                batch = crates.CrateRun(self.authority, "owner-" + kind)
                batch.create(self.definition, self.tree, self.destination)
                manifest = batch.read()
                record = manifest["crates"]["bulk_sample"]
                worker = Path(record["checkout"])
                if kind == "adopted":
                    worker = batch.folder / "workers/bulk_sample"
                    record["checkout"] = str(worker)
                    record["intent"] = str(worker / ".git/style-batch-intent.json")
                    batch.save(manifest)
                    worker.mkdir(parents=True)
                    copies.initialize_git(worker, self.repo)
                    _ = run.git(worker, "checkout", "-q", "-B", record["branch"].removeprefix("refs/heads/"), self.base)
                with mock.patch.object(run, "WorkspaceIntent", side_effect=RuntimeError("after registration")):
                    with self.assertRaisesRegex(RuntimeError, "after registration"):
                        _ = batch.advance("bulk_sample", renamed, self.commands, available, fixture_execute)
                if kind == "changed-task":
                    scratch.hand_over(worker, batch.owner, "gate:another-task")
                if kind == "interrupted-removal":
                    with scratch.removal_lock(worker) as idle:
                        self.assertTrue(idle)
                        with mock.patch.object(scratch, "remove", side_effect=RuntimeError("before deletion")):
                            with self.assertRaisesRegex(RuntimeError, "before deletion"):
                                scratch.remove_marked(worker, scratch.remove)
                with self.assertRaisesRegex(copies.CopyError, "crate run") as refusal:
                    copies.remove_trees(batch.owner)
                self.assertIn(str(batch.path), str(refusal.exception))
                self.assertTrue(worker.exists())
                self.assertEqual(batch.read(), manifest)
                current = scratch.task_of(worker)
                assert isinstance(current, copy_record.Registered)
                self.assertNotIn("ended_task", current.fields)
                if kind == "adopted":
                    self.assertIn(str(worker), scratch.holds())
                # Keep earlier workers outside the next owner's selection.
                if kind != "interrupted-removal":
                    scratch.hand_over(worker, "kept_" + kind.replace("-", "_"), current.task)

    def test_owner_removal_refuses_a_legacy_worker_without_its_run_link_after_hand_over(self) -> None:
        worker = self.legacy_worker()
        with mock.patch.object(run, "WorkspaceIntent", side_effect=RuntimeError("after registration")):
            with self.assertRaisesRegex(RuntimeError, "after registration"):
                _ = self.advance("bulk_sample")
        scratch.hand_over(worker, self.batch.owner, "gate:changed-task")
        record = scratch.task_of(worker)
        assert isinstance(record, copy_record.Registered)
        _ = scratch.update_record(worker, lambda _: {**record.fields, "crate_run": ""})
        manifest = self.batch.read()
        with self.assertRaisesRegex(copies.CopyError, "repair crate run link"):
            copies.remove_trees(self.batch.owner)
        self.assertTrue(worker.exists())
        self.assertEqual(self.batch.read(), manifest)
        self.assertIn(str(worker), scratch.holds())

    def test_owner_removal_requires_readable_run_evidence_after_git_or_folder_deletion(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        with mock.patch.object(run, "WorkspaceIntent", side_effect=RuntimeError("after registration")):
            with self.assertRaisesRegex(RuntimeError, "after registration"):
                _ = self.advance("bulk_sample")
        worker = Path(self.batch.read()["crates"]["bulk_sample"]["checkout"])
        manifest = self.batch.path.read_text()
        for evidence in ("missing", "damaged"):
            with self.subTest(evidence=evidence):
                if evidence == "missing":
                    self.batch.path.unlink()
                else:
                    _ = self.batch.path.write_text("{invalid")
                with self.assertRaisesRegex(copies.CopyError, "repair crate run"):
                    copies.remove_trees(self.batch.owner)
                self.assertTrue(worker.exists())
                _ = self.batch.path.write_text(manifest)
        with scratch.removal_lock(worker) as idle:
            self.assertTrue(idle)
            with mock.patch.object(scratch, "remove", side_effect=RuntimeError("before deletion")):
                with self.assertRaisesRegex(RuntimeError, "before deletion"):
                    scratch.remove_marked(worker, scratch.remove)
        shutil.rmtree(worker / ".git")
        with self.assertRaisesRegex(copies.CopyError, "not terminal"):
            copies.remove_trees(self.batch.owner)
        self.assertTrue(worker.exists())
        shutil.rmtree(worker)
        with self.assertRaisesRegex(copies.CopyError, "not terminal"):
            copies.remove_trees(self.batch.owner)
        self.assertTrue(scratch.removal_mark(worker).exists())
        self.assertEqual(self.batch.path.read_text(), manifest)

    def test_owner_removal_removes_only_workers_with_terminal_run_evidence(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        with mock.patch.object(copies, "end_workspaces"):
            manifest = self.finish_run()
        worker = Path(manifest["crates"]["bulk_sample"]["checkout"])
        self.assertTrue(worker.exists())
        copies.remove_trees(self.batch.owner)
        self.assertFalse(worker.exists())
        self.assertEqual(self.batch.read(), manifest)
        self.assertTrue(Path(manifest["crates"]["bulk_sample"]["result_path"]).exists())

    def test_terminal_run_removes_adopted_worker_and_keeps_result_evidence(self) -> None:
        worker = self.legacy_worker()
        manifest = self.finish_run()
        self.assertFalse(worker.exists())
        self.assertNotIn(str(worker), scratch.holds())
        self.assertEqual(self.batch.read(), manifest)
        result = Path(manifest["crates"]["bulk_sample"]["result_path"])
        self.assertTrue(result.is_file())
        self.assertFalse(scratch.removal_mark(worker).exists())

    def test_terminal_recovery_finishes_each_adopted_worker_removal_boundary(self) -> None:
        worker = self.legacy_worker()
        original_remove = scratch.remove
        original_release = scratch.release
        original_end = copies.end_workspaces
        for boundary in ("before cleanup", "after git deletion", "after folder deletion", "before hold release"):
            with self.subTest(boundary=boundary):
                # Restore only the legacy worker between terminal recovery attempts.
                if not worker.exists():
                    self.initialize_legacy_worker(worker)
                with copies.workspace(self.batch.owner, self.repo, self.base, name=worker.name,
                                      branch=self.batch.read()["crates"]["bulk_sample"]["branch"],
                                      task=self.batch.task, path=worker):
                    pass
                def remove(path: Path) -> None:
                    if path == worker and boundary == "after git deletion":
                        import shutil
                        shutil.rmtree(path / ".git")
                        raise RuntimeError(boundary)
                    original_remove(path)
                    if path == worker and boundary == "after folder deletion":
                        raise RuntimeError(boundary)
                def release(path: Path) -> bool:
                    if path == worker and boundary == "before hold release":
                        raise RuntimeError(boundary)
                    return original_release(path)
                def end(task: str, paths: list[Path]) -> None:
                    if boundary == "before cleanup":
                        raise RuntimeError(boundary)
                    original_end(task, paths)
                with (mock.patch.object(scratch, "remove", side_effect=remove),
                      mock.patch.object(scratch, "release", side_effect=release),
                      mock.patch.object(copies, "end_workspaces", side_effect=end)):
                    with self.assertRaisesRegex(RuntimeError, boundary):
                        _ = self.finish_run()
                self.assertIn(str(worker), scratch.holds())
                _ = self.finish_run()
                self.assertFalse(worker.exists())
                self.assertNotIn(str(worker), scratch.holds())
                self.assertFalse(scratch.removal_mark(worker).exists())
                self.assertTrue(Path(self.batch.read()["crates"]["bulk_sample"]["result_path"]).is_file())

    def test_terminal_cleanup_keeps_locked_or_unsaved_adopted_workers(self) -> None:
        worker = self.legacy_worker()
        with mock.patch.object(copies, "end_workspaces"):
            manifest = self.finish_run()
        with scratch.busy_locks(worker, create=True) as idle:
            self.assertTrue(idle)
            copies.end_workspaces(self.batch.task, [worker])
            self.assertTrue(worker.exists())
            self.assertIn(str(worker), scratch.holds())
        _ = (worker / "pending.txt").write_text("keep unsaved work\n")
        _ = self.finish_run()
        self.assertTrue(worker.exists())
        self.assertGreater(scratch.holds()[str(worker)]["ended"], 0)
        self.assertEqual((worker / "pending.txt").read_text(), "keep unsaved work\n")
        self.assertEqual(self.batch.read(), manifest)
        (worker / "pending.txt").unlink()
        _ = self.finish_run()
        self.assertFalse(worker.exists())

    def test_pending_run_survives_expiry_and_terminal_resume_ends_its_task(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            manifest = self.reservation_advance()
        worker = Path(manifest["crates"]["bulk_sample"]["checkout"])
        record = scratch.task_of(worker)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, self.batch.task)
        _ = scratch.update_record(worker, lambda current: scratch.fields_for_task(
            current.fields if isinstance(current, copy_record.Registered) else {}, "until:2000-01-01T0000Z"))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            _ = self.reservation_advance()
        recovered = scratch.task_of(worker)
        assert isinstance(recovered, copy_record.Registered)
        self.assertEqual(recovered.task, self.batch.task)
        self.assertIsInstance(scratch.lifetime(worker, now=10**12), scratch.Live)
        with mock.patch.object(copies, "end_workspaces", side_effect=RuntimeError("before task end")):
            with self.assertRaisesRegex(RuntimeError, "before task end"):
                _ = self.advance("bulk_sample")
                _ = self.advance("beta")
        self.assertTrue(worker.exists())
        _ = self.advance("beta")
        self.assertIsInstance(scratch.lifetime(worker), scratch.Ended)
        self.assertIn(worker, scratch.end(self.batch.task))
        self.assertFalse(worker.exists())

    def test_nested_proof_keeps_the_canonical_owner_with_the_real_resolver(self) -> None:
        from scripts.style import style_sync as owner_resolver
        self.freeze(paths=("src/lib.rs",))
        observed: set[Path] = set()
        observed_caches: set[Path] = set()
        resolver = owner_resolver.gate_owner
        with mock.patch.object(owner_resolver, "gate_owner", side_effect=resolver) as calls:
            batch = crates.CrateRun(self.authority, self.batch.run_id)
            def commands(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
                observed.add(repo)
                observed_caches.add((repo / "target").resolve().parent)
                return self.commands(repo, arguments)
            _ = batch.advance("bulk_sample", renamed, commands, available, fixture_execute)
        self.assertEqual([call.args for call in calls.call_args_list], [(self.repo,)])
        gates = list((copies.gates_root() / self.repo.name).iterdir())
        self.assertEqual({path.name for path in gates}, {"sample-style-gate", "sample-style-gate-2"})
        self.assertTrue(any(path in observed_caches for path in gates))
        self.assertTrue(any(not path.exists() for path in observed))

    def test_worker_names_separate_run_and_package_at_the_last_dot(self) -> None:
        self.freeze()
        # Reuse the real package metadata with only its validated package name changed.
        original = coverage_report.workspace(self.authority, self.base)
        members = tuple(replace(member, package_name=name)
                        for member, name in zip(original, ("gamma", "beta-gamma")))
        paths: list[str] = []
        for identity, package in (("first-beta", "gamma"), ("first", "beta-gamma")):
            batch = crates.CrateRun(self.authority, identity)
            with mock.patch.object(coverage_report, "workspace", return_value=members):
                batch.create(self.definition, self.tree, self.destination)
            paths.append(batch.read()["crates"][package]["checkout"])
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual([Path(path).name for path in paths], ["crate-first-beta.gamma", "crate-first.beta-gamma"])

    def test_crate_trees_keep_ownership_and_use_two_standing_caches(self) -> None:
        self.freeze()
        original = self.git("rev-parse", "HEAD")
        manifest = self.batch.read()
        with self.batch.frozen_checkout(manifest) as frozen:
            frozen_owner = copies.read_record(frozen).owner
            self.assertEqual(copies.read_record(frozen).source, str(self.repo))
            with scratch.busy_locks(frozen, create=False) as idle:
                self.assertFalse(idle)
        observed: set[Path] = set()

        def registered(repo: Path, arguments: Sequence[str]) -> run.CommandResult:
            observed.add(repo)
            if (repo / copies.GATE_RECORD).is_file():
                self.assertEqual(copies.read_record(repo).owner, frozen_owner)
            else:
                record = scratch.task_of(repo)
                self.assertIsInstance(record, copy_record.Registered)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual(record.fields["source_path"], str(self.repo))
            with scratch.busy_locks(repo, create=False) as idle:
                self.assertFalse(idle)
            return self.commands(repo, arguments)

        for package in manifest["crates"]:
            result = self.batch.advance(package, renamed, registered, available, fixture_execute)
            worker = Path(result["crates"][package]["checkout"])
            self.assertIn(worker, list(scratch.copy_paths()))
            self.assertIsInstance(scratch.task_of(worker), copy_record.Registered)
            self.assertIsNone(scratch.unsaved_work(worker))
        gates = list((copies.gates_root() / self.repo.name).iterdir())
        self.assertEqual({tree.name for tree in gates}, {"sample-style-gate", "sample-style-gate-2"})
        self.assertTrue(all((tree / "target").is_dir() for tree in gates))
        self.assertFalse(frozen.exists())
        self.assertTrue(any(not path.exists() for path in observed))
        self.assertEqual(self.git("rev-parse", "HEAD"), original)

    @property
    def other_writer(self) -> berth_board.Reservation:
        return berth_board.Reservation("writer-reservation", "style/main", self.root / "product",
                                       "refs/heads/product", ("src/lib.rs",), "active", "blocking")

    def reservation_advance(self) -> crates.Manifest:
        return self.batch.advance("bulk_sample", renamed, self.commands,
                                  bulk.check_available, fixture_execute)

    def dry_run_snapshot(self) -> dict[str, bytes]:
        return {str(path): path.read_bytes() for root in (self.repo, self.authority.database.parent)
                for path in root.rglob("*") if path.is_file()}

    def test_dry_run_uses_execution_admission_for_reserved_stale_and_unsupported_findings_without_writes(self) -> None:
        source = (self.repo / "src/lib.rs").read_text()
        stale = self.repo / "src/stale.rs"
        _ = stale.write_text(source + source.replace("run()", "second()"))
        _ = (self.repo / "src/unsupported.rs").write_text(source)
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "admission fixtures")
        self.authority.set_tip(self.git("rev-parse", "HEAD"))
        self.file_findings(paths=("src/lib.rs", "src/stale.rs", "src/unsupported.rs"))
        with closing(sqlite3.connect(self.authority.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET kind='restructure' WHERE path='src/unsupported.rs'")
        _ = stale.write_text(source.replace("old", "gone") + source.replace("run()", "second()"))
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "remove only the first repeated declaration")
        self.authority.set_tip(self.git("rev-parse", "HEAD"))
        before = self.dry_run_snapshot()
        output = io.StringIO()
        with (mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))),
              mock.patch.object(crates, "admission_plan", wraps=crates.admission_plan) as admission,
              mock.patch("sys.stdout", output)):
            code = run.main(["--repo", str(self.repo), "--crate", "bulk_sample", "--dry-run",
                             "--rule", self.definition.rule_id, "--base-tree", self.git("rev-parse", "HEAD^{tree}")])
        self.assertEqual(code, 0, output.getvalue())
        admission.assert_called_once()
        report = bulk.object_fields(cast(object, json.loads(output.getvalue())))
        outcomes = [bulk.object_fields(item) for item in bulk.object_list(bulk.object_fields(report["crates"])["bulk_sample"])]
        findings = {finding.finding_id: finding for finding in self.authority.held_findings(ledger_api.AllHeldFindings())}
        by_path = {findings[str(item["finding_id"])].path: item for item in outcomes}
        self.assertEqual(by_path["src/lib.rs"]["outcome"], "pending")
        self.assertEqual(by_path["src/lib.rs"]["holders"], [{"worktree": str(self.other_writer.holder_worktree_root),
            "branch": self.other_writer.holder_branch, "reservation_id": self.other_writer.reservation_id}])
        self.assertEqual(by_path["src/stale.rs"]["outcome"], "refused")
        self.assertTrue(str(by_path["src/stale.rs"]["reason"]))
        self.assertEqual(by_path["src/unsupported.rs"], {"finding_id": by_path["src/unsupported.rs"]["finding_id"],
            "outcome": "refused", "reason": "finding does not specify one identifier rename"})
        self.assertEqual(self.dry_run_snapshot(), before)
        self.assertFalse(self.batch.folder.exists())
        self.assertTrue(all(isinstance(item.build_ticket, ledger_api.NoBuildTicket) for item in findings.values()))

    def test_values_dry_run_never_exports_sources_even_when_every_finding_is_pending(self) -> None:
        source = ('pub fn run() {\n    let first: usize = 42;\n' +
                  '    let second: usize = 43;\n}\n')
        _ = (self.repo / "src/lib.rs").write_text(source)
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "values admission fixture")
        base = self.git("rev-parse", "HEAD")
        tree = self.git("rev-parse", "HEAD^{tree}")
        self.authority.set_tip(base)
        _ = (self.root / "style/rust/no-magic-values.md").write_text(
            "---\ntier: B\nfamily: values\ninput_scope: file\npre_filter: magic-values\n" +
            "approval: always\n---\n# Values\n")
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.name == "no-magic-values")
        self.authority.ticket_add("11001", ticket_owner_id("values-read"), "read", "values", ".",
                                  ["src/lib.rs"], [definition.rule_id])
        self.authority.ticket_assign("11001", "main")
        _ = self.authority.ticket_base("11001")
        identities: list[str] = []
        for line, after in ((2, "Add `const FIRST: usize = 42;` in constants.rs."),
                            (3, "Compute a better value instead.")):
            issue = self.root / f"{44000 + line}.md"
            _ = issue.write_text("---\nstatus: open\n---\nValues decision\n")
            finding = self.authority.hold(definition.rule_id, "src/lib.rs", line,
                source.splitlines()[line - 1], "restructure", str(40 + line), after,
                definition.blob, "11001")
            self.authority.file_held(finding.finding_id, issue)
            identities.append(finding.finding_id)
        self.authority.ticket_drop("11001")
        before = self.dry_run_snapshot()
        denying_writes = False
        def deny_writes(event: str, arguments: tuple[object, ...]) -> None:
            if not denying_writes:
                return
            if event == "open":
                if isinstance(arguments[0], int):
                    return
                # TemporaryFile first opens its existing directory through io.open,
                # then its opener requests O_TMPFILE there. Neither names a file.
                # A custom opener that creates a named file emits its own audited
                # open, which is still refused below; directory mutations are too.
                if isinstance(arguments[0], (str, bytes)) and os.path.isdir(arguments[0]):
                    return
                mode, flags = arguments[1:3]
                if (isinstance(mode, str) and any(flag in mode for flag in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                    raise AssertionError(f"dry run attempted a file write: {arguments!r}")
            elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink",
                           "os.truncate", "os.chmod", "os.chown", "os.utime"}:
                raise AssertionError(f"dry run attempted a filesystem mutation: {event} {arguments!r}")
        # Audit hooks cannot be removed; the finally block disables this hook after each report.
        sys.addaudithook(deny_writes)
        try:
            denying_writes = True
            with tempfile.TemporaryFile() as spool:
                _ = spool.write(b"anonymous git read output")
                self.assertEqual(os.fstat(spool.fileno()).st_nlink, 0)
            with self.assertRaisesRegex(AssertionError, "dry run attempted a file write"):
                with tempfile.NamedTemporaryFile():
                    self.fail("a named temporary file must not be created")
            with self.assertRaisesRegex(AssertionError, "dry run attempted a filesystem mutation: os.mkdir"):
                _ = tempfile.mkdtemp()
            with self.assertRaisesRegex(AssertionError, "dry run attempted a file write"):
                _ = (self.repo / "named-write-probe").write_bytes(b"must not be written")
        finally:
            denying_writes = False
        for blocked in (True, False):
            with (self.subTest(blocked=blocked),
                  ledger_api.Ledger(self.repo, self.authority.database, access=ledger_api.LedgerAccess.READ_ONLY) as ledger,
                  mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(
                      (self.other_writer,) if blocked else ()))):
                try:
                    denying_writes = True
                    report = crates.dry_run_crates(self.repo, ["bulk_sample"], definition.rule_id, tree, ledger)
                finally:
                    denying_writes = False
                outcomes = {str(item["finding_id"]): item for item in (
                    bulk.object_fields(value) for value in bulk.object_list(
                        bulk.object_fields(report["crates"])["bulk_sample"]))}
                if blocked:
                    self.assertEqual([outcomes[identity]["outcome"] for identity in identities], ["pending", "pending"])
                    self.assertEqual(outcomes[identities[0]]["holders"], [{
                        "worktree": str(self.other_writer.holder_worktree_root),
                        "branch": self.other_writer.holder_branch, "reservation_id": self.other_writer.reservation_id}])
                else:
                    self.assertEqual(outcomes[identities[0]]["outcome"], "admitted")
                    self.assertEqual(outcomes[identities[1]]["outcome"], "refused")
                    self.assertTrue(outcomes[identities[1]]["reason"])
                self.assertEqual(self.dry_run_snapshot(), before)
                self.assertFalse(self.batch.folder.exists())
                self.assertTrue(all(isinstance(item.build_ticket, ledger_api.NoBuildTicket)
                                    for item in self.authority.named_held(identities)))

    def test_dry_run_labels_semantic_proof_not_checked_instead_of_starting_a_writer(self) -> None:
        self.file_findings(paths=("src/lib.rs",))
        before = self.dry_run_snapshot()
        with (ledger_api.Ledger(self.repo, self.authority.database, access=ledger_api.LedgerAccess.READ_ONLY) as ledger,
              mock.patch.object(bulk, "RustAnalyzer", side_effect=AssertionError("must not start a building service"))):
            report = crates.dry_run_crates(self.repo, ["bulk_sample"], self.definition.rule_id,
                                           self.tree, ledger, available=available)
        outcomes = bulk.object_list(bulk.object_fields(report["crates"])["bulk_sample"])
        self.assertEqual(len(outcomes), 1)
        outcome = bulk.object_fields(outcomes[0])
        self.assertEqual(outcome["outcome"], "not checked")
        self.assertIn("rust-analyzer", str(outcome["reason"]))
        self.assertIn("require writes", str(report["not_checked"]))
        self.assertEqual(self.dry_run_snapshot(), before)

    def test_dry_run_with_a_read_only_analyzer_reports_the_shared_semantic_proof_without_claiming(self) -> None:
        self.file_findings(paths=("src/lib.rs",))
        before = self.dry_run_snapshot()
        server = ScriptedServer(lambda text, _offset, new: word_edits(text, "old", new))
        with ledger_api.Ledger(self.repo, self.authority.database, access=ledger_api.LedgerAccess.READ_ONLY) as ledger:
            report = crates.dry_run_crates(self.repo, ["bulk_sample"], self.definition.rule_id,
                                           self.tree, ledger, server, available)
        outcomes = bulk.object_list(bulk.object_fields(report["crates"])["bulk_sample"])
        self.assertEqual([bulk.object_fields(item)["outcome"] for item in outcomes], ["admitted"])
        self.assertTrue(server.calls)
        self.assertEqual(self.dry_run_snapshot(), before)
        self.assertFalse(self.batch.folder.exists())

    def test_blocking_reservation_is_pending_with_holder_and_keeps_the_same_ticket(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            result = self.reservation_advance()
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], "waiting for reservations")
        self.assertEqual(record.get("pending"), [{"finding_id": record["selected"][0]["id"], "path": "src/lib.rs",
            "holders": [{"worktree": str(self.other_writer.holder_worktree_root),
                         "branch": self.other_writer.holder_branch, "reservation_id": self.other_writer.reservation_id}]}])
        self.assertEqual(result["refusals"], [])
        assert "tickets" in record
        ticket = record["tickets"][record["selected"][0]["id"]]
        self.assertEqual(self.authority.named_held(record["tickets"])[0].build_ticket, ledger_api.BuildTicket(ticket))
        self.assertEqual(self.authority.bulk_refusals(ticket), ())
        self.assertFalse(Path(record["result_path"]).exists())
        self.assertEqual(self.commands.calls, [])
        self.assertIn("let old", (Path(record["checkout"]) / "src/lib.rs").read_text())

    def test_released_reservation_executes_once_without_a_second_ticket(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            pending = self.reservation_advance()["crates"]["bulk_sample"]
        claims = pending.get("tickets", {})
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(())), mock.patch.object(
                self.authority, "ticket_add", side_effect=AssertionError("must keep original claim")), mock.patch(
                __name__ + ".fixture_execute", wraps=fixture_execute) as execute:
            result = self.reservation_advance()
            again = self.reservation_advance()
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(result, again)
        self.assertEqual(result["crates"]["bulk_sample"].get("tickets"), claims)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(result["crates"]["bulk_sample"].get("pending"), [])
        self.assertIn("let number", self.git("show", self.destination + ":src/lib.rs"))

    def test_restart_keeps_manifest_pending_until_the_writer_releases(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            before = self.reservation_advance()["crates"]["bulk_sample"]
        with ledger_api.Ledger(self.repo, self.authority.database) as ledger:
            resumed = crates.CrateRun(ledger, self.batch.run_id)
            with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
                blocked = resumed.advance("bulk_sample", renamed, self.commands, bulk.check_available, fixture_execute)
            self.assertEqual(blocked["crates"]["bulk_sample"].get("pending"), before.get("pending"))
            self.assertEqual(blocked["crates"]["bulk_sample"]["stage"], "waiting for reservations")
            with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(())):
                result = resumed.advance("bulk_sample", renamed, self.commands, bulk.check_available, fixture_execute)
        self.assertEqual(result["crates"]["bulk_sample"].get("tickets"), before.get("tickets"))
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def test_pending_writer_can_become_a_clear_read_claim(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            blocked = self.reservation_advance()
        self.assertEqual(blocked["crates"]["bulk_sample"]["stage"], "waiting for reservations")
        reader = replace(self.other_writer, edit_blocking_status="clear")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((reader,))):
            result = self.reservation_advance()
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def test_pending_finding_released_after_planning_is_admitted_in_the_same_invocation(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        boards = iter((berth_board.Board((self.other_writer,)),))
        def released(_repo: Path) -> berth_board.Board:
            return next(boards, berth_board.Board(()))
        with mock.patch.object(berth_board, "read_board", side_effect=released):
            result = self.reservation_advance()
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(result["refusals"], [])
        self.assertIn("let number", self.git("show", self.destination + ":src/lib.rs"))

    def test_released_pending_finding_still_requires_its_fresh_machine_proof(self) -> None:
        self.freeze_fresh_judgment()
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            before = self.batch.advance("bulk_sample", self.machine_plan, self.commands,
                                        bulk.check_available, fixture_execute)
        self.assertEqual(before["crates"]["bulk_sample"]["stage"], "waiting for reservations")
        self.assertEqual(before["crates"]["bulk_sample"].get("machine_judgments"), [])
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(())):
            result = self.batch.advance("bulk_sample", self.machine_plan, self.commands,
                                       bulk.check_available, fixture_execute)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(len(result["crates"]["bulk_sample"].get("machine_judgments", [])), 1)

    def test_released_pending_finding_can_still_receive_a_terminal_ledger_refusal(self) -> None:
        self.freeze_fresh_judgment()
        # A changed rule invalidates the still-applicable-looking stale parameter.
        # It remains a terminal refusal when it reaches the production planner.
        selected = self.batch.read()["crates"]["bulk_sample"]["selected"]
        identity = selected[0]["id"]
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            before = self.batch.advance("bulk_sample", self.machine_plan, self.commands,
                                        bulk.check_available, fixture_execute)
        self.assertEqual(before["crates"]["bulk_sample"]["stage"], "waiting for reservations")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(())), mock.patch.object(
                self.authority, "rename_judgment_site", return_value=ledger_api.NeedsJudgment("rule changed since judged")):
            result = self.batch.advance("bulk_sample", self.machine_plan, self.commands,
                                       bulk.check_available, fixture_execute)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.ABANDONED)
        self.assertEqual(result["refusals"], [{"finding_id": identity,
                                             "reason": "changed since judged: rule changed since judged"}])
        self.assertIsInstance(self.authority.named_held((identity,))[0].build_ticket, ledger_api.NoBuildTicket)

    def test_terminal_refusal_is_published_without_releasing_a_pending_sibling(self) -> None:
        second = "src/second.rs"
        _ = (self.repo / second).write_bytes((self.repo / "src/lib.rs").read_bytes())
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "independent finding")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(shared_proposal=True, paths=("src/lib.rs", second))
        def terminal(_checkout: Path, _ledger: ledger_api.Ledger, _base: str,
                     selected: run.Selection, _availability: run.PlanningFileAvailability,
                     _definition: rules.Rule) -> run.PlannedEdits:
            return run.PlannedEdits((), tuple(bulk.Refusal(item.finding_id, "changed since judged: removed site")
                                             for item in selected.findings))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            result = self.batch.advance("bulk_sample", terminal, self.commands, bulk.check_available, fixture_execute)
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], "waiting for reservations")
        self.assertEqual(len(result["refusals"]), 1)
        assert "tickets" in record
        by_path = {item.path: item for item in self.authority.named_held(record["tickets"])}
        self.assertIsInstance(by_path[second].build_ticket, ledger_api.NoBuildTicket)
        self.assertIsInstance(by_path["src/lib.rs"].build_ticket, ledger_api.BuildTicket)
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(())):
            resumed = self.reservation_advance()
        self.assertEqual(resumed["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(resumed["refusals"], result["refusals"])
        self.assertIn("let old", self.git("show", self.destination + ":" + second))

    def test_crate_keeps_a_shared_ticket_uncommitted_until_every_pending_member_is_ready(self) -> None:
        second = "src/ready.rs"
        _ = (self.repo / second).write_bytes((self.repo / "src/lib.rs").read_bytes())
        with (self.repo / "src/lib.rs").open("a") as source:
            _ = source.write("mod ready;\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "two files under one proposal")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(shared_proposal=True, paths=("src/lib.rs", second))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((self.other_writer,))):
            waiting = self.reservation_advance()["crates"]["bulk_sample"]
        self.assertEqual(waiting["stage"], "waiting for reservations")
        assert "tickets" in waiting
        tickets = set(waiting["tickets"].values())
        self.assertEqual(len(tickets), 1)
        ticket = next(item for item in self.authority.proposal_tickets() if item["id"] in tickets)
        self.assertEqual((ticket["state"], ticket["checkpoint"]), ("assigned", ""))
        checkout = Path(waiting["checkout"])
        self.assertEqual(run.git(checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(run.git(checkout, "status", "--porcelain"), "")
        self.assertIn("let old", (checkout / second).read_text())
        self.assertFalse(Path(waiting["result_path"]).exists())
        self.assertEqual(self.commands.calls, [])
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(())), mock.patch(
                __name__ + ".fixture_execute", wraps=fixture_execute) as execute:
            result = self.reservation_advance()
            again = self.reservation_advance()
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(result, again)
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(record.get("tickets"), waiting["tickets"])
        evidence = bulk.object_fields(cast(object, json.loads(Path(record["result_path"]).read_text())))
        self.assertEqual(set(cast(list[str], evidence["findings"])), set(waiting["tickets"]))
        ticket = next(item for item in self.authority.proposal_tickets() if item["id"] in tickets)
        self.assertEqual((ticket["state"], ticket["checkpoint"]), ("checkpointed", evidence["commit"]))
        for path in ("src/lib.rs", second):
            self.assertIn("let number", self.git("show", self.destination + ":" + path))

    def test_terminal_atomic_group_refusal_removes_its_reservation_pending_outcome(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        def atomic(checkout: Path, ledger: ledger_api.Ledger, base: str,
                   selected: run.Selection, availability: run.PlanningFileAvailability,
                   definition: rules.Rule) -> run.PlannedEdits:
            planned = renamed(checkout, ledger, base, selected, availability, definition)
            file = planned.files[0]
            assert isinstance(file, bulk.FileRenames)
            paths = frozenset((file.path, "src/writer.rs", "src/unavailable.rs"))
            return run.PlannedEdits(tuple(replace(file, path=path, dependencies=paths)
                                         for path in (file.path, "src/writer.rs", "src/unavailable.rs")), ())
        writer = replace(self.other_writer, scopes=("src/writer.rs",))
        def boundary(repo: Path, path: str) -> None:
            if path == "src/unavailable.rs":
                raise bulk.BulkRenameError("source is unavailable")
            bulk.check_available(repo, path)
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((writer,))):
            result = self.batch.advance("bulk_sample", atomic, self.commands, boundary, fixture_execute)
        record = result["crates"]["bulk_sample"]
        identity = record["selected"][0]["id"]
        self.assertEqual(result["refusals"], [{"finding_id": identity, "reason": "source is unavailable"}])
        self.assertEqual(record.get("pending", []), [])
        self.assertEqual(record["stage"], crates.CrateStage.ABANDONED)
        self.assertIsInstance(self.authority.named_held((identity,))[0].build_ticket, ledger_api.NoBuildTicket)
        assert "tickets" in record
        self.assertEqual(len(self.authority.bulk_refusals(record["tickets"][identity])), 1)
        self.assertEqual(self.commands.calls, [])

    def test_writer_arriving_at_execution_keeps_the_claim_and_rolls_back_private_edits(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        reads = iter((berth_board.Board(()),))
        def writer_arrives(_repo: Path) -> berth_board.Board:
            return next(reads, berth_board.Board((self.other_writer,)))
        with mock.patch.object(berth_board, "read_board", side_effect=writer_arrives):
            result = self.reservation_advance()
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], "waiting for reservations")
        self.assertEqual(result["refusals"], [])
        self.assertIn("let old", (Path(record["checkout"]) / "src/lib.rs").read_text())
        self.assertEqual(run.git(Path(record["checkout"]), "status", "--porcelain"), "")
        self.assertTrue(all(isinstance(item.build_ticket, ledger_api.BuildTicket)
                            for item in self.authority.named_held(frozen["id"] for frozen in record["selected"])))
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board(())):
            resumed = self.reservation_advance()
        self.assertEqual(resumed["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)

    def freeze_fresh_judgment(self) -> None:
        _, tip = machine_rename_fixture(self.authority, self.definition, self.repo, self.root / "fresh.md")
        self.definition = next(rule for rule in rules.load_rules(self.repo) if rule.rule_id == self.definition.rule_id)
        self.base = tip
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, tip)
        self.batch.create(self.definition, self.tree, self.destination)

    def machine_plan(self, checkout: Path, ledger: ledger_api.Ledger, base: str,
                     selected: run.Selection, availability: run.PlanningFileAvailability,
                     definition: rules.Rule) -> run.PlannedEdits:
        server = ScriptedServer(lambda text, _offset, new: word_edits(text, "item", new))
        return run.plan_rule(checkout, ledger, base, selected, definition, server, availability.available)

    def stop_with_frozen_judgment(self) -> crates.FrozenMachineJudgment:
        self.freeze_fresh_judgment()
        def stopped(checkout: Path, _base: str, planned: run.PlannedEdits,
                    _intent: run.WorkspaceIntent, _runner: run.Runner,
                    _boundary: Callable[[Path, str], None]) -> crates.ValidatedEdits:
            frozen = self.batch.read()["crates"]["bulk_sample"].get("machine_judgments", [])
            self.assertEqual(len(frozen), 1)
            self.assertEqual(frozen[0], crates.freeze_machine_judgment(planned.judgments[0]))
            self.assertEqual(run.git(checkout, "status", "--porcelain"), "")
            raise SystemExit("before execution")
        with self.assertRaisesRegex(SystemExit, "before execution"):
            _ = self.batch.advance("bulk_sample", self.machine_plan, self.commands, available, stopped)
        return self.batch.read()["crates"]["bulk_sample"].get("machine_judgments", [])[0]

    def test_crate_resume_executes_the_frozen_machine_judgment_without_rejudging(self) -> None:
        frozen = self.stop_with_frozen_judgment()
        with mock.patch.object(run, "fresh_machine_judgment", side_effect=AssertionError("must use frozen judgment")):
            result = self.batch.advance("bulk_sample", self.machine_plan, self.commands, available, fixture_execute)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(result["crates"]["bulk_sample"].get("machine_judgments", []), [frozen])
        self.assertIn("impl_item: &ImplItem", self.git("show", self.destination + ":src/lib.rs"))

    def test_crate_resume_refuses_a_changed_finding_row_without_replacing_its_judgment(self) -> None:
        frozen = self.stop_with_frozen_judgment()
        with closing(sqlite3.connect(self.authority.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET after='different' WHERE finding_id=?", (frozen["finding_id"],))
        with self.assertRaisesRegex(bulk.BulkRenameError, "frozen finding"):
            _ = self.batch.advance("bulk_sample", self.machine_plan, self.commands, available, fixture_execute)
        self.assertEqual(self.batch.read()["crates"]["bulk_sample"].get("machine_judgments", []), [frozen])

    def test_crate_resume_refuses_a_changed_rule_without_replacing_its_judgment(self) -> None:
        frozen = self.stop_with_frozen_judgment()
        _ = self.definition.path.write_text(self.definition.path.read_text() + "Changed rule requirement\n")
        with self.assertRaisesRegex(bulk.BulkRenameError, "rule"):
            _ = self.batch.advance("bulk_sample", self.machine_plan, self.commands, available, fixture_execute)
        self.assertEqual(self.batch.read()["crates"]["bulk_sample"].get("machine_judgments", []), [frozen])

    def test_crate_resume_refuses_a_judgment_for_another_tip(self) -> None:
        frozen = self.stop_with_frozen_judgment()
        manifest = self.batch.read()
        manifest["crates"]["bulk_sample"].get("machine_judgments", [])[0]["commit"] = self.git("rev-parse", self.base + "^")
        self.batch.save(manifest)
        result = self.batch.advance("bulk_sample", self.machine_plan, self.commands, available, fixture_execute)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.ABANDONED)
        self.assertIn("tip changed", result["crates"]["bulk_sample"].get("error", ""))
        self.assertEqual(result["crates"]["bulk_sample"].get("machine_judgments", [])[0]["finding_identity"], frozen["finding_identity"])

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
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for args in self.commands.calls if args[0] == "bash"],
                         ["final", "lint", "final --saved"] * 2)
        self.assertTrue(all(args[2:4] == ("--workspace", "--all-targets")
                            for args in self.commands.calls if args[0] == "cargo" and args[1] in {"check", "clippy"}))
        for ticket in set(record["tickets"].values()):
            self.assertEqual(self.authority.ticket_base(ticket), self.base)

    def test_two_crates_integrate_serially_without_reselecting(self) -> None:
        self.freeze()
        with (mock.patch.object(copies, "standing", wraps=copies.standing) as standing,
              mock.patch.object(run, "validate", wraps=run.validate) as validate):
            first = self.advance("bulk_sample")
        first_record = first["crates"]["bulk_sample"]
        self.assertTrue(Path(first_record["result_path"]).exists())
        checkpoint = self.batch.result(first_record)["commit"]
        assert "integration" in first_record
        first_candidate = first_record["integration"]["commit"]
        first_sources = [call.args[2] for call in standing.call_args_list]
        first_checkout = cast(run.WorkspaceEdits, validate.call_args_list[-1].args[0]).repo
        with (mock.patch.object(run, "select", side_effect=AssertionError("must not reselect")),
              mock.patch.object(copies, "standing", wraps=copies.standing) as standing,
              mock.patch.object(run, "validate", wraps=run.validate) as validate):
            result = self.advance("beta")
        second_record = result["crates"]["beta"]
        self.assertTrue(Path(second_record["result_path"]).exists())
        second = self.batch.result(second_record)["commit"]
        assert "integration" in second_record
        second_candidate = second_record["integration"]["commit"]
        second_sources = [call.args[2] for call in standing.call_args_list]
        second_checkout = cast(run.WorkspaceEdits, validate.call_args_list[-1].args[0]).repo
        for record, candidate, sources, checkout, reused in (
            (first_record, first_candidate, first_sources, first_checkout, True),
            (second_record, second_candidate, second_sources, second_checkout, False),
        ):
            with self.subTest(directory=record["directory"]):
                self.assertEqual(copies.CommittedSource(candidate) not in sources, reused)
                self.assertEqual(checkout == Path(record["checkout"]), reused)
                self.assertEqual(self.git("rev-parse", candidate + "^{tree}")
                                 == self.batch.result(record)["tree"], reused)
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
        self.assertEqual(result["crates"]["beta"].get("error"), "no selected findings")
        self.assertEqual(self.commands.calls, [])

    def test_empty_selection_names_holding_tickets_and_finding_count_per_crate(self) -> None:
        self.freeze()
        findings = self.authority.held_findings(ledger_api.AllHeldFindings())
        primary = next(finding for finding in findings if finding.path == "src/lib.rs")
        assert isinstance(primary.state, ledger_api.Filed)
        self.authority.ticket_add("1002", ticket_owner_id("read"), "read", "names", ".", [primary.path],
                                  [self.definition.rule_id])
        self.authority.ticket_assign("1002", "main")
        _ = self.authority.ticket_base("1002")
        source = (self.repo / primary.path).read_text().splitlines()
        second = self.authority.hold(self.definition.rule_id, primary.path, 3, source[2],
                                     "rename", "old", "value", self.definition.blob, "1002")
        file_blocked = self.authority.hold(self.definition.rule_id, primary.path, 1, source[0],
                                           "rename", "run", "execute", self.definition.blob, "1002")
        for finding in (second, file_blocked):
            self.authority.file_held(finding.finding_id, primary.state.proposal)
        self.authority.ticket_drop("1002")
        other = next(finding for finding in findings if finding.path == "beta/src/lib.rs")
        for ticket, unit, finding in (("42001", "holder-a", primary), ("43001", "holder-b", second), ("23001", "other-crate", other)):
            filed = self.authority.named_held((finding.finding_id,))[0]
            assert isinstance(filed.state, ledger_api.Filed)
            self.authority.ticket_add(ticket, ticket_owner_id(unit), "proposal", "names", ".",
                                      [filed.path], [filed.rule], proposal=str(filed.state.proposal),
                                      finding_ids=(filed.finding_id,))
        batch = crates.CrateRun(self.authority, "held")
        batch.create(self.definition, self.tree, self.destination)
        # Abandonment consumes the frozen selection evidence, not another ledger query.
        with mock.patch.object(self.authority, "proposal_tickets", side_effect=AssertionError("must reuse skip data")):
            result = batch.advance("bulk_sample")
            other_result = batch.advance("beta")
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["selected"], [])
        self.assertEqual(record["stage"], crates.CrateStage.ABANDONED)
        self.assertEqual(record.get("error"),
                         "no selected findings: 3 findings held by open tickets: 42001, 43001")
        self.assertEqual(other_result["crates"]["beta"].get("error"),
                         "no selected findings: 1 finding held by open tickets: 23001")

    def test_queue_refusal_keeps_the_claimed_run_and_its_tickets(self) -> None:
        self.freeze()
        config = self.root / "lint.conf"
        key = "program_first_peak_gib.cargo"
        self.queue.refusals["cargo"] = f"{config}: {key} is missing or invalid"
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
            _ = self.batch.advance("bulk_sample", renamed, run.run_command, available)
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn(str(config), errors.getvalue())
        self.assertIn(key, errors.getvalue())
        record = self.batch.read()["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.CLAIMED)
        checkout = Path(record["checkout"])
        self.assertEqual(run.git(checkout, "status", "--porcelain"), "")
        self.assertEqual((checkout / "src/lib.rs").read_bytes(), (self.repo / "src/lib.rs").read_bytes())
        self.assertNotIn("error", record)
        assert "tickets" in record and "owner" in record
        self.assertTrue(record["tickets"])
        for identity, ticket in record["tickets"].items():
            finding, = self.authority.named_held((identity,))
            self.assertEqual(finding.build_ticket, ledger_api.BuildTicket(ticket))
            self.assertIsInstance(finding.state, ledger_api.Filed)
            self.assertEqual(self.authority.bulk_refusals(ticket), ())
            self.assertEqual(self.authority.ticket_batch_owner(ticket), ledger_api.BatchOwner(record["owner"]))
        self.assertTrue(all(ticket["state"] == "assigned" for ticket in self.authority.proposal_tickets()
                            if ticket["id"] in record["tickets"].values()))
        self.assertEqual(self.queue.places, [])

    def test_repeated_refusal_after_format_restores_claimed_record_and_bytes(self) -> None:
        self.freeze()
        install_refusal_tools(self.root)
        config = self.root / "lint.conf"
        key = "program_first_peak_gib.cargo"
        _ = config.write_text("[memory]\n")
        original = (self.repo / "src/lib.rs").read_bytes()
        checks: list[bytes] = []

        def refuse_check(checkout: Path, arguments: Sequence[str]) -> None:
            if "check" in arguments and key not in config.read_text():
                formatted = (checkout / "src/lib.rs").read_bytes()
                self.assertTrue(formatted.startswith(b"\n"))
                self.assertIn(b"let number", formatted)
                checks.append(formatted)
                raise queue_place.QueuePlaceRefused(f"{config}: {key} is missing or invalid")

        self.queue.before_start = refuse_check
        preserved: crates.CrateRecord = self.batch.read()["crates"]["bulk_sample"]
        tickets: tuple[ledger_api.TicketRecord, ...] = ()
        claims: tuple[ledger_api.HeldFinding, ...] = ()
        ownership: dict[str, object] = {}
        for attempt in range(2):
            with ledger_api.Ledger(self.repo, self.authority.database) as fresh:
                batch = crates.CrateRun(fresh, self.batch.run_id)
                with redirect_stderr(io.StringIO()), self.assertRaises(bulk.StyleAdmissionStopped) as stopped:
                    _ = batch.advance("bulk_sample", renamed, run.run_command, available)
                self.assertEqual(stopped.exception.code, 2)
                record = batch.read()["crates"]["bulk_sample"]
                self.assertEqual(record["stage"], crates.CrateStage.CLAIMED)
                assert "tickets" in record and "owner" in record
                current_tickets = fresh.proposal_tickets()
                current_claims = fresh.named_held(record["tickets"])
                checkout = Path(record["checkout"])
                intent = run.WorkspaceIntent(checkout,
                    f"bulk_run --run {batch.run_id} --crate bulk_sample --repo {self.repo}")
                journal = bulk.object_fields(cast(object, json.loads(intent.path.read_text())))
                current_ownership = {name: journal[name] for name in
                                     ("batch", "operation", "base", "branch", "tickets", "findings")}
                if attempt == 0:
                    preserved, tickets, claims, ownership = record, current_tickets, current_claims, current_ownership
                else:
                    self.assertEqual(record, preserved)
                    self.assertEqual(current_tickets, tickets)
                    self.assertEqual(current_claims, claims)
                    self.assertEqual(current_ownership, ownership)
                self.assertEqual((checkout / "src/lib.rs").read_bytes(), original)
                self.assertEqual(run.git(checkout, "status", "--porcelain"), "")
                self.assertTrue(all(place.closed for place in self.queue.places))
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[0], checks[1])
        _ = config.write_text(f"[memory]\n{key}=12\n")
        with ledger_api.Ledger(self.repo, self.authority.database) as fresh:
            batch = crates.CrateRun(fresh, self.batch.run_id)
            with mock.patch.object(run, "claim", side_effect=AssertionError("retry must keep claims")):
                result = batch.advance("bulk_sample", renamed, run.run_command, available)
            record = result["crates"]["bulk_sample"]
            self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)
            self.assertEqual(record.get("tickets"), preserved.get("tickets"))
            self.assertEqual(record.get("owner"), preserved.get("owner"))

    def test_failed_refusal_recovery_never_abandons_crate_on_retry(self) -> None:
        self.freeze()
        install_refusal_tools(self.root)
        unknown = b"pub fn outside_change() {}\n"

        def refuse_changed(checkout: Path, arguments: Sequence[str]) -> None:
            if "check" in arguments:
                _ = (checkout / "src/lib.rs").write_bytes(unknown)
                raise queue_place.QueuePlaceRefused("place lost after unknown change")

        self.queue.before_start = refuse_changed
        with redirect_stderr(io.StringIO()), self.assertRaises(bulk.StyleAdmissionStopped):
            _ = self.batch.advance("bulk_sample", renamed, run.run_command, available)
        record = self.batch.read()["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.CLAIMED)
        assert "tickets" in record
        tickets = self.authority.proposal_tickets()
        claims = self.authority.named_held(record["tickets"])
        with ledger_api.Ledger(self.repo, self.authority.database) as fresh:
            batch = crates.CrateRun(fresh, self.batch.run_id)
            with self.assertRaisesRegex(bulk.BulkRenameError, "bytes do not match the recorded edit"):
                _ = batch.advance("bulk_sample", renamed, run.run_command, available)
            self.assertEqual(batch.read()["crates"]["bulk_sample"], record)
            self.assertEqual(fresh.proposal_tickets(), tickets)
            self.assertEqual(fresh.named_held(record["tickets"]), claims)
        checkout = Path(record["checkout"])
        self.assertEqual((checkout / "src/lib.rs").read_bytes(), unknown)
        self.assertTrue(run.WorkspaceIntent(checkout, "read journal path").path.exists())

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
        self.assertEqual([("final --saved" if args[2:4] == ("final", "--saved") else args[2]) for args in self.commands.calls[before:] if args[0] == "bash"],
                         ["final", "lint", "final --saved"])

    def test_destination_moving_during_combined_validation_refuses_cas_and_keeps_pending_claims(self) -> None:
        self.freeze()
        moved = self.git("commit-tree", self.tree, "-p", self.base, "-m", "destination moved")
        proof = run.test_tree
        calls = 0
        def testing(repo: Path, commit: str, tree: str, runner: run.Runner,
                    times: dict[str, float], *, owner: str = "") -> run.CommandResult:
            nonlocal calls
            calls += 1
            result = proof(repo, commit, tree, runner, times, owner=owner)
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

    def test_cross_package_mount_shares_only_its_module_descendants(self) -> None:
        for path, text in {
            "src/mounted.rs": 'mod child; #[path="explicit.rs"] mod renamed; mod inline { mod nested; }',
            "src/child.rs": "mod leaf;",
            "src/child/leaf.rs": "pub fn leaf() {}",
            "src/explicit.rs": "pub fn explicit() {}",
            "src/inline/nested.rs": "pub fn nested() {}",
            "src/unrelated.rs": "pub fn unrelated() {}",
            "beta/src/lib.rs": '#[path="../../src/mounted.rs"] mod shared;',
        }.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text(text + "\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "mount one file with module descendants")
        owners = self.ownership()
        for path in ("src/mounted.rs", "src/child.rs", "src/child/leaf.rs",
                     "src/explicit.rs", "src/inline/nested.rs"):
            with self.subTest(path=path):
                self.assertEqual(owners.owners[path], {".", "beta"})
                with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                    owners.require(".", (path,))
        owners.require(".", ("src/lib.rs", "src/unrelated.rs"))
        # A second propagation (as target discovery performs) must preserve flat-child lookup.
        owners.propagate()
        self.assertEqual(dict(owners.uncertain), {})
        owners.require(".", ("src/unrelated.rs",))

    def test_unproved_mounted_descendants_refuse_only_the_declaring_file(self) -> None:
        for source in ('#[cfg_attr(feature="alternate", path="alternate.rs")] mod child;',
                       '#[path=env!("CHILD")] mod child;',
                       'mod child;'):
            with self.subTest(source=source):
                _ = (self.repo / "src/mounted.rs").write_text(source + "\n")
                _ = (self.repo / "src/unrelated.rs").write_text("pub fn unrelated() {}\n")
                _ = (self.repo / "beta/src/lib.rs").write_text(
                    '#[path="../../src/mounted.rs"] mod shared;\n')
                _ = self.git("add", ".")
                _ = self.git("commit", "-qm", "unproved mounted descendants")
                owners = self.ownership()
                self.assertIn("src/mounted.rs", owners.uncertain)
                self.assertNotIn("src/unrelated.rs", owners.uncertain)
                with self.assertRaisesRegex(bulk.BulkRenameError, "ambiguous shared-source ownership"):
                    owners.require(".", ("src/mounted.rs",))
                owners.require(".", ("src/lib.rs", "src/unrelated.rs"))

    def test_unproved_sibling_keeps_every_proved_shared_descendant(self) -> None:
        for failed in ('#[cfg(any())] mod missing;',
                       '#[cfg_attr(feature="alternate", path="alternate.rs")] mod missing;',
                       '#[path=env!("CHILD")] mod missing;'):
            for declarations in ("mod child; " + failed, failed + " mod child;"):
                with self.subTest(declarations=declarations):
                    for path, source in {
                        "src/lib.rs": "mod child;",
                        "src/mounted.rs": declarations,
                        "src/child.rs": "mod leaf;",
                        "src/child/leaf.rs": "pub fn leaf() {}",
                        "beta/src/lib.rs": '#[path="../../src/mounted.rs"] mod shared;',
                    }.items():
                        target = self.repo / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _ = target.write_text(source + "\n")
                    _ = self.git("add", ".")
                    _ = self.git("commit", "-qm", "mixed module evidence")
                    owners = self.ownership()
                    self.assertIn("src/mounted.rs", owners.uncertain)
                    for path in ("src/child.rs", "src/child/leaf.rs"):
                        self.assertEqual(owners.owners[path], {".", "beta"})
                        with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                            owners.require(".", (path,))

    def test_inline_path_mount_shares_only_its_actual_child_routes(self) -> None:
        for path, source in {
            "src/lib.rs": "",
            "src/shared/child.rs": "mod leaf;",
            "src/shared/child/leaf.rs": "pub fn leaf() {}",
            "src/shared/explicit.rs": "pub fn explicit() {}",
            "src/shared/unrelated.rs": "pub fn unrelated() {}",
            "beta/src/lib.rs": ('#[path="../../src/shared"] mod shared { '
                                + 'mod child; #[path="explicit.rs"] mod renamed; }'),
        }.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text(source + "\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "inline mount with shared child routes")
        owners = self.ownership()
        for path in ("src/shared/child.rs", "src/shared/child/leaf.rs", "src/shared/explicit.rs"):
            self.assertIn(path, owners.reachable(("beta/src/lib.rs",)))
            self.assertEqual(owners.owners[path], {".", "beta"})
            with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                owners.require(".", (path,))
        self.assertEqual(dict(owners.uncertain), {})
        owners.require(".", ("src/lib.rs", "src/shared/unrelated.rs"))
        # The rename-layout owner still refuses inline paths in its strict mode.
        with self.assertRaisesRegex(bulk.BulkRenameError, "out-of-line module"):
            _ = bulk.module_file_paths(Path("beta/src/lib.rs"), owners.sources["beta/src/lib.rs"])
        # A conditional route cannot leave its potential loaded targets editable.
        _ = (self.repo / "beta/src/lib.rs").write_text(
            '#[cfg_attr(feature="shared", path="../../src/shared")] mod shared { mod child; }\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "unproved inline mount")
        with self.assertRaisesRegex(bulk.BulkRenameError, "unproved inline mount"):
            self.ownership().require(".", ("src/shared/child.rs",))

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

    def test_invoked_builtin_include_alias_preserves_shared_source_edge(self) -> None:
        for imported in ("std::include_str as source", "core::{include_bytes as source}",
                         "std::include as source"):
            with self.subTest(imported=imported):
                _ = (self.repo / "beta/src/lib.rs").write_text(
                    f'use {imported}; const CODE: &str = source!("../../src/lib.rs");\n')
                _ = self.git("add", ".")
                _ = self.git("commit", "-qm", "invoked builtin loader alias")
                owners = self.ownership()
                self.assertIn("src/lib.rs", owners.edges["beta/src/lib.rs"])
                self.assertEqual(owners.owners["src/lib.rs"], {".", "beta"})
                with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                    owners.require(".", ("src/lib.rs",))
        _ = (self.repo / "beta/src/lib.rs").write_text(
            'use std::include_str as source; const CODE: &str = source!(dynamic);\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "nonliteral invoked alias")
        with self.assertRaisesRegex(bulk.BulkRenameError, "nonliteral include_str"):
            self.ownership().require("beta", ("beta/src/lib.rs",))

    def test_cfg_predicate_path_data_is_not_a_loader(self) -> None:
        _ = (self.repo / "beta/src/lib.rs").write_text("\n".join((
            '#[cfg(path = "not_a_source.rs")] pub fn f() {}',
            '#[cfg_attr(path = "also_data.rs", allow(dead_code))] pub fn g() {}',
            '#[tool::metadata(path = "metadata.rs")] pub fn h() {}', "")))
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "path predicates are data")
        owners = self.ownership()
        self.assertEqual(dict(owners.uncertain), {})
        self.assertEqual(owners.edges, {})
        owners.require("beta", ("beta/src/lib.rs",))

    def test_qualified_and_chained_loader_imports_keep_literal_edges(self) -> None:
        for source in (
            'mod loaders { pub use std::include_str as source; } '
            + 'const CODE: &str = loaders::source!("../../src/lib.rs");',
            'use std::include_str; use include_str as source; '
            + 'const CODE: &str = source!("../../src/lib.rs");',
            'mod loaders { pub use core::{include_bytes as source}; } '
            + 'use self::loaders as renamed; use renamed::source as code; '
            + 'const CODE: &[u8] = code!("../../src/lib.rs");',
            'mod loaders { pub use std::include as source; } '
            + 'crate::loaders::source!("../../src/lib.rs");',
            'use ::std as library; use library::include_str as source; '
            + 'const CODE: &str = source!("../../src/lib.rs");',
            'mod loaders { pub use std as system; } '
            + 'const CODE: &str = loaders::system::include_str!("../../src/lib.rs");',
            'mod loaders { pub use std as system; } '
            + 'const CODE: &str = self::loaders::system::include_str!("../../src/lib.rs");',
        ):
            with self.subTest(source=source):
                _ = (self.repo / "beta/src/lib.rs").write_text(source + "\n")
                _ = self.git("add", ".")
                _ = self.git("commit", "-qm", "qualified or chained source loader")
                owners = self.ownership()
                self.assertIn("src/lib.rs", owners.edges["beta/src/lib.rs"])
                self.assertEqual(owners.owners["src/lib.rs"], {".", "beta"})
                with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                    owners.require(".", ("src/lib.rs",))
                _ = (self.repo / "beta/src/lib.rs").write_text(
                    source.replace('"../../src/lib.rs"', 'dynamic') + "\n")
                _ = self.git("add", ".")
                _ = self.git("commit", "-qm", "unproved qualified or chained source loader")
                with self.assertRaisesRegex(bulk.BulkRenameError, "nonliteral include"):
                    self.ownership().require("beta", ("beta/src/lib.rs",))

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

    def test_admission_refusal_survives_later_planning_abort(self) -> None:
        def abort(_checkout: Path, _ledger: ledger_api.Ledger, _base: str,
                  selected: run.Selection, _availability: run.PlanningFileAvailability,
                  _definition: rules.Rule) -> run.PlannedEdits:
            self.assertEqual([finding.path for finding in selected.findings], ["src/lib.rs"])
            raise bulk.BulkRenameError("later planning abort")
        self.assert_admission_refusal_survives_abort(abort, fixture_execute, "later planning abort")

    def test_admission_refusal_survives_later_validation_abort(self) -> None:
        with mock.patch.object(run, "validate", side_effect=bulk.BulkRenameError("later validation abort")):
            self.assert_admission_refusal_survives_abort(renamed, fixture_execute, "later validation abort")

    def test_planner_refusal_survives_post_plan_base_abort_before_claim_release(self) -> None:
        self.freeze(paths=("src/lib.rs",))
        identity = self.batch.read()["crates"]["bulk_sample"]["selected"][0]["id"]
        reason = "planner refused bad"
        expected = [{"finding_id": identity, "reason": reason}]

        patching = mock.patch.object(run, "require_base",
                                     side_effect=bulk.BulkRenameError("canonical base moved"))

        def planned(_checkout: Path, _ledger: ledger_api.Ledger, _base: str,
                    selected: run.Selection, _availability: run.PlanningFileAvailability,
                    _definition: rules.Rule) -> run.PlannedEdits:
            self.assertEqual([finding.finding_id for finding in selected.findings], [identity])
            # The next base check fails only after the planner has returned its refusal.
            _ = patching.start()
            return run.PlannedEdits((), (bulk.Refusal(identity, reason),))

        drop = self.authority.ticket_drop_batch
        def dropped(ticket: str, owner: ledger_api.BatchOwner) -> None:
            self.assertEqual(self.batch.read()["refusals"], expected)
            self.assertEqual([(row.finding_id, row.reason)
                              for row in self.authority.bulk_refusals(ticket)],
                             [(identity, reason)])
            drop(ticket, owner)
        try:
            with mock.patch.object(
                    self.authority, "ticket_drop_batch", side_effect=dropped) as dropping:
                result = self.batch.advance(
                    "bulk_sample", planned, self.commands, available, fixture_execute)
        finally:
            _ = patching.stop()
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.ABANDONED)
        self.assertEqual(record.get("error"), "canonical base moved")
        self.assertEqual(result["refusals"], expected)
        self.assertEqual(self.batch.read()["refusals"], expected)
        self.assertEqual(dropping.call_count, 1)
        assert "tickets" in record
        ticket = record["tickets"][identity]
        self.assertEqual([(row.finding_id, row.reason)
                              for row in self.authority.bulk_refusals(ticket)],
                         [(identity, reason)])

    def assert_admission_refusal_survives_abort(self, planner: crates.Planner,
                                               executor: crates.PlanExecutor, error: str) -> None:
        _ = (self.repo / "src/uncertain.rs").write_text(
            (self.repo / "src/lib.rs").read_text() + 'include!(dynamic);\n')
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "early refusal then abort")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze(shared_proposal=True, paths=("src/lib.rs", "src/uncertain.rs"))
        frozen = self.batch.read()["crates"]["bulk_sample"]
        identity = next(item["id"] for item in frozen["selected"] if item["path"] == "src/uncertain.rs")
        reason = "ambiguous shared-source ownership: src/uncertain.rs: nonliteral include source loader"
        expected = [{"finding_id": identity, "reason": reason}]
        drop = self.authority.ticket_drop_batch
        def dropped(ticket: str, owner: ledger_api.BatchOwner) -> None:
            self.assertEqual(self.batch.read()["refusals"], expected)
            self.assertEqual([(row.finding_id, row.reason) for row in self.authority.bulk_refusals(ticket)],
                             [(identity, reason)])
            drop(ticket, owner)
        with mock.patch.object(self.authority, "ticket_drop_batch", side_effect=dropped) as dropping:
            result = self.batch.advance("bulk_sample", planner, self.commands, available, executor)
        record = result["crates"]["bulk_sample"]
        self.assertEqual(record["stage"], crates.CrateStage.ABANDONED)
        self.assertEqual(record.get("error"), error)
        self.assertEqual(result["refusals"], expected)
        self.assertEqual(self.batch.read()["refusals"], expected)
        self.assertEqual(dropping.call_count, 1)
        assert "tickets" in record
        ticket = record["tickets"][identity]
        self.assertEqual([(row.finding_id, row.reason) for row in self.authority.bulk_refusals(ticket)],
                         [(identity, reason)])

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

    def test_target_folder_gitlink_refuses_package_sources_without_crashing(self) -> None:
        members = rules.workspace_members({"Cargo.toml": (self.repo / "Cargo.toml").read_text(),
                                           "beta/Cargo.toml": (self.repo / "beta/Cargo.toml").read_text()})
        for folder in ("src/bin", "examples", "tests", "benches"):
            with self.subTest(folder=folder):
                _ = self.git("update-index", "--add", "--cacheinfo", "160000", self.base, folder)
                tree = self.git("write-tree")
                owners = crates.SourceOwnership(self.repo, tree, members)
                with self.assertRaisesRegex(bulk.BulkRenameError,
                                            "target folder " + folder + " is not a directory"):
                    owners.require(".", ("src/lib.rs",))
                owners.require("beta", ("beta/src/lib.rs",))
                self.assertEqual(dict(crates.SourceOwnership(self.repo, tree, members).uncertain),
                                 dict(owners.uncertain))
                _ = self.git("update-index", "--force-remove", folder)

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
        checkout = self.root / "publication"
        _ = self.git("worktree", "add", str(checkout),
                     self.destination.removeprefix("refs/heads/"))
        original = (checkout / "src/lib.rs").read_bytes()
        index = run.git(checkout, "write-tree")
        with self.assertRaisesRegex(bulk.BulkRenameError, "destination is checked out"):
            _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        self.assert_pending(record)
        assert "integration" in record
        candidate = record["integration"]["commit"]
        self.assertEqual(record["integration"]["stage"], crates.IntegrationStage.TESTED)
        assert "error" in record
        self.assertIn("checkout owner must integrate validated candidate", record["error"])
        self.assertIn(candidate, record["error"])
        self.assertIn(self.batch.read()["integration_ref"], record["error"])
        self.assertEqual(self.git("rev-parse", self.destination), self.base)
        self.assertEqual(self.git("rev-parse", self.batch.read()["integration_ref"]), candidate)
        self.assertEqual((checkout / "src/lib.rs").read_bytes(), original)
        self.assertEqual(run.git(checkout, "write-tree"), index)
        self.assertEqual(run.git(checkout, "status", "--porcelain"), "")
        self.assertIn("let number", self.git("show", candidate + ":src/lib.rs"))
        calls = len(self.commands.calls)
        pending = self.batch.read()
        with self.assertRaisesRegex(bulk.BulkRenameError, "checkout owner must integrate"):
            _ = self.advance("bulk_sample")
        self.assertEqual(self.batch.read(), pending)
        self.assertEqual(len(self.commands.calls), calls)
        _ = self.git("worktree", "remove", str(checkout))
        result = self.advance("bulk_sample")
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertNotIn("error", result["crates"]["bulk_sample"])
        self.assertEqual(len(self.commands.calls), calls)

    def test_combined_test_failure_retains_pending_claims_for_resume(self) -> None:
        self.freeze()
        proof = run.test_tree
        def failed(repo: Path, commit: str, tree: str, runner: run.Runner,
                   times: dict[str, float], *, owner: str = "") -> run.CommandResult:
            if any(record.get("integration", {}).get("stage") == crates.IntegrationStage.PREPARED
                   for record in self.batch.read()["crates"].values()):
                return run.CommandResult(1, "combined proof failure")
            return proof(repo, commit, tree, runner, times, owner=owner)
        with mock.patch.object(run, "test_tree", side_effect=failed):
            with self.assertRaisesRegex(bulk.BulkRenameError, "combined-tree proof failed"):
                _ = self.advance("bulk_sample")
        record = self.batch.read()["crates"]["bulk_sample"]
        self.assert_pending(record)
        assert "integration" in record
        self.assertEqual(record["integration"]["stage"], crates.IntegrationStage.PREPARED)
        worker = Path(record["checkout"])
        source = worker / "src/lib.rs"
        original = source.read_text()
        commit = self.batch.result(record)["commit"]
        for prepared, edit in ((True, "dirty"), (False, "head")):
            with self.subTest(prepared=prepared, edit=edit):
                if not prepared:
                    manifest = self.batch.read()
                    _ = manifest["crates"]["bulk_sample"].pop("integration", None)
                    self.batch.save(manifest)
                moved = commit
                if edit == "dirty":
                    _ = source.write_text(original + "// private edit\n")
                else:
                    moved = run.git(worker, "commit-tree", self.batch.result(record)["tree"],
                                    "-p", commit, "-m", "private worker commit")
                    _ = run.git(worker, "update-ref", record["branch"], moved, commit)
                with mock.patch.object(run, "test_tree", side_effect=failed):
                    with self.assertRaisesRegex(bulk.BulkRenameError, "combined-tree proof failed"):
                        _ = self.advance("bulk_sample")
                self.assert_pending(self.batch.read()["crates"]["bulk_sample"])
                if edit == "dirty":
                    self.assertEqual(source.read_text(), original + "// private edit\n")
                    _ = source.write_text(original)
                else:
                    self.assertEqual(run.git(worker, "rev-parse", "HEAD"), moved)
                    _ = run.git(worker, "update-ref", record["branch"], commit, moved)
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

    def test_planning_reads_only_private_base_after_main_moves_and_rules_change(self) -> None:
        self.freeze()
        _ = (self.repo / "src/lib.rs").write_text("pub fn moved() {}\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "main product work")
        docs = self.repo / "docs/style"
        docs.mkdir(parents=True)
        _ = (docs / "invalid.md").write_text("invalid local rule\n")
        _ = (self.repo / "src/lib.rs").write_text("pub fn dirty() {}\n")
        def private(checkout: Path, ledger: ledger_api.Ledger, base: str,
                    selected: run.Selection, availability: run.PlanningFileAvailability,
                    definition: rules.Rule) -> run.PlannedEdits:
            sources = run.RenameSources()
            for finding in selected.findings:
                self.assertIsInstance(run.current_rename(ledger, finding, base, sources, checkout),
                                      bulk.RenameFinding)
                self.assertIn("let old", sources.current[finding.path])
            return renamed(checkout, ledger, base, selected, availability, definition)
        load = rules.load_rules
        def private_rules(repo_root: Path | None) -> tuple[rules.Rule, ...]:
            self.assertNotEqual(repo_root, self.repo)
            return load(repo_root)
        with mock.patch.object(rules, "load_rules", side_effect=private_rules):
            # Resuming opens a fresh canonical ledger, too: construction must not scan main.
            with ledger_api.Ledger(self.repo, self.authority.database) as reopened:
                resumed = crates.CrateRun(reopened, self.batch.run_id)
                result = resumed.advance("bulk_sample", private, self.commands,
                                         available, fixture_execute)
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual((self.repo / "src/lib.rs").read_text(), "pub fn dirty() {}\n")
        self.assertIn("let number", self.git("show", self.destination + ":src/lib.rs"))

    def test_moved_destination_rebuilds_combined_proof_and_resume_is_idempotent(self) -> None:
        self.freeze()
        with mock.patch.object(self.batch, "integrate", side_effect=SystemExit("checkpoint")):
            with self.assertRaises(SystemExit):
                _ = self.advance("bulk_sample")
        _ = (self.repo / "product.txt").write_text("product change\n")
        _ = self.git("add", "product.txt")
        tree = self.git("write-tree")
        moved = self.git("commit-tree", tree, "-p", self.base, "-m", "product change")
        _ = self.git("reset", "--hard", self.base)
        _ = self.git("update-ref", self.destination, moved, self.base)
        with mock.patch.object(run, "test_tree", wraps=run.test_tree) as proof:
            result = self.advance("bulk_sample")
            self.assertEqual(proof.call_count, 1)
            calls = len(self.commands.calls)
            self.assertEqual(self.advance("bulk_sample"), result)
            self.assertEqual(proof.call_count, 1)
            self.assertEqual(len(self.commands.calls), calls)
        tip = self.git("rev-parse", self.destination)
        self.assertEqual(self.git("rev-parse", tip + "^1"), moved)
        self.assertEqual(result["expected_tip"], tip)
        self.assertEqual(self.git("show", tip + ":product.txt"), "product change")
        self.assertIn("let number", self.git("show", tip + ":src/lib.rs"))

    def test_moved_destination_invalidates_prepared_combined_proof(self) -> None:
        self.freeze()
        proof = run.test_tree
        def interrupted(repo: Path, commit: str, tree: str, runner: run.Runner,
                        times: dict[str, float], *, owner: str = "") -> run.CommandResult:
            if any(record.get("integration", {}).get("stage") == crates.IntegrationStage.PREPARED
                   for record in self.batch.read()["crates"].values()):
                raise RuntimeError("combined interrupted")
            return proof(repo, commit, tree, runner, times, owner=owner)
        with mock.patch.object(run, "test_tree", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "combined interrupted"):
                _ = self.advance("bulk_sample")
        before = self.batch.read()["crates"]["bulk_sample"]
        assert "integration" in before
        old_candidate = before["integration"]["commit"]
        moved = self.git("commit-tree", self.tree, "-p", self.base, "-m", "destination moved")
        _ = self.git("update-ref", self.destination, moved, self.base)
        with (mock.patch.object(run, "test_tree", wraps=run.test_tree) as proving,
              mock.patch.object(copies, "standing", wraps=copies.standing) as standing):
            result = self.advance("bulk_sample")
        self.assertEqual(proving.call_count, 1)
        record = result["crates"]["bulk_sample"]
        assert "integration" in record
        self.assertIn(copies.CommittedSource(record["integration"]["commit"]),
                      [call.args[2] for call in standing.call_args_list])
        self.assertNotEqual(record["integration"]["commit"], old_candidate)
        self.assertEqual(self.git("rev-parse", self.destination + "^1"), moved)
        self.assertEqual(record["stage"], crates.CrateStage.INTEGRATED)

    def test_other_crate_refresh_cannot_publish_stale_tested_candidate(self) -> None:
        self.freeze()
        checkout = self.root / "publication"
        _ = self.git("worktree", "add", str(checkout),
                     self.destination.removeprefix("refs/heads/"))
        with self.assertRaisesRegex(bulk.BulkRenameError, "destination is checked out"):
            _ = self.advance("bulk_sample")
        first = self.batch.read()["crates"]["bulk_sample"]
        assert "integration" in first
        stale = first["integration"]["commit"]
        self.assertEqual(first["integration"]["stage"], crates.IntegrationStage.TESTED)
        _ = self.git("worktree", "remove", str(checkout))

        _ = (self.repo / "product.txt").write_text("product change\n")
        _ = self.git("add", "product.txt")
        moved = self.git("commit-tree", self.git("write-tree"), "-p", self.base,
                         "-m", "independent destination change")
        _ = self.git("reset", "--hard", self.base)
        _ = self.git("update-ref", self.destination, moved, self.base)
        proof = run.test_tree
        def failed(repo: Path, commit: str, tree: str, runner: run.Runner,
                   times: dict[str, float], *, owner: str = "") -> run.CommandResult:
            if any(record.get("integration", {}).get("stage") == crates.IntegrationStage.PREPARED
                   for record in self.batch.read()["crates"].values()):
                return run.CommandResult(1, "combined proof failure")
            return proof(repo, commit, tree, runner, times, owner=owner)
        with mock.patch.object(run, "test_tree", side_effect=failed):
            with self.assertRaisesRegex(bulk.BulkRenameError, "combined-tree proof failed"):
                _ = self.advance("beta")
        pending = self.batch.read()
        self.assertEqual(pending["expected_tip"], moved)
        retained = pending["crates"]["bulk_sample"]
        assert "integration" in retained
        self.assertEqual(retained["integration"]["commit"], stale)
        self.assertEqual(self.git("rev-parse", self.destination), moved)

        with mock.patch.object(run, "test_tree", wraps=proof) as proving:
            result = self.advance("bulk_sample")
            self.assertEqual(proving.call_count, 1)
            calls = len(self.commands.calls)
            self.assertEqual(self.advance("bulk_sample"), result)
            self.assertEqual(proving.call_count, 1)
            self.assertEqual(len(self.commands.calls), calls)
        record = result["crates"]["bulk_sample"]
        assert "integration" in record
        self.assertNotEqual(record["integration"]["commit"], stale)
        self.assertEqual(record["integration"]["destination_tip"], moved)
        self.assertEqual(self.git("rev-parse", self.destination + "^1"), moved)
        self.assertEqual(self.git("show", self.destination + ":product.txt"), "product change")
        self.assertIn("let number", self.git("show", self.destination + ":src/lib.rs"))
        with mock.patch.object(run, "test_tree", wraps=proof) as proving:
            result = self.advance("beta")
            self.assertEqual(proving.call_count, 1)
        self.assertEqual(result["crates"]["beta"]["stage"], crates.CrateStage.INTEGRATED)
        self.assertEqual(self.git("show", self.destination + ":product.txt"), "product change")
        for path in ("src/lib.rs", "beta/src/lib.rs"):
            self.assertIn("let number", self.git("show", self.destination + ":" + path))

    def test_moved_destination_clean_overlap_is_merged_and_reproved(self) -> None:
        _ = (self.repo / "src/lib.rs").write_text(
            (self.repo / "src/lib.rs").read_text() + "\n" * 8 + "pub fn product() {}\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "separate hunk fixture")
        self.base, self.tree = self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("update-ref", self.destination, self.base)
        self.authority.set_tip(self.base)
        self.freeze()
        _ = (self.repo / "src/lib.rs").write_text(
            (self.repo / "src/lib.rs").read_text().replace("fn product()", "fn shipped()"))
        _ = self.git("add", ".")
        moved = self.git("commit-tree", self.git("write-tree"), "-p", self.base,
                         "-m", "product hunk")
        _ = self.git("reset", "--hard", self.base)
        _ = self.git("update-ref", self.destination, moved, self.base)
        result = self.advance("bulk_sample")
        self.assertEqual(result["crates"]["bulk_sample"]["stage"], crates.CrateStage.INTEGRATED)
        source = self.git("show", self.destination + ":src/lib.rs")
        self.assertIn("let number", source)
        self.assertIn("fn shipped()", source)

    def test_glob_and_out_of_line_loader_imports_preserve_shared_edges(self) -> None:
        for invocation, module in (
            ('mod loaders { pub use std::include_str as source; } use loaders::*; '
             + 'const CODE: &str = source!("../../src/lib.rs");', ""),
            ('use std::*; const CODE: &str = include_str!("../../src/lib.rs");', ""),
            ('mod loaders; const CODE: &str = loaders::source!("../../src/lib.rs");',
             'pub use std::include_str as source;'),
            ('mod loaders; use loaders::*; const CODE: &str = source!("../../src/lib.rs");',
             'pub use std::include_str as source;'),
        ):
            with self.subTest(invocation=invocation):
                _ = (self.repo / "beta/src/lib.rs").write_text(invocation + "\n")
                _ = (self.repo / "beta/src/loaders.rs").write_text(module + "\n")
                _ = self.git("add", ".")
                _ = self.git("commit", "-qm", "glob or external loader import")
                owners = self.ownership()
                self.assertIn("src/lib.rs", owners.edges["beta/src/lib.rs"])
                self.assertEqual(owners.owners["src/lib.rs"], {".", "beta"})
                with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                    owners.require(".", ("src/lib.rs",))
                edges = dict(owners.edges)
                owners.propagate()
                self.assertEqual(owners.edges, edges)

    def test_conditional_inline_mount_protects_escaping_loaded_descendants(self) -> None:
        for path, source in {
            "src/lib.rs": "mod elsewhere; mod included; mod unrelated;",
            "src/shared/child.rs": '#[path="../elsewhere.rs"] mod leaf; '
                                   + 'include!("../included.rs");',
            "src/elsewhere.rs": "mod grandchild;",
            "src/grandchild.rs": "pub fn grandchild() {}",
            "src/included.rs": "pub fn included() {}",
            "src/unrelated.rs": "pub fn unrelated() {}",
            "beta/src/lib.rs": '#[cfg_attr(feature="shared", path="../../src/shared")] '
                               + 'mod shared { mod child; }',
        }.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text(source + "\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "conditional mount escaping descendants")
        owners = self.ownership()
        for path in ("src/shared/child.rs", "src/elsewhere.rs",
                     "src/grandchild.rs", "src/included.rs"):
            with self.subTest(path=path):
                self.assertTrue(any("unproved inline mount" in reason
                                    for reason in owners.uncertain[path]))
                with self.assertRaisesRegex(bulk.BulkRenameError, "unproved inline mount"):
                    owners.require(".", (path,))
        owners.require(".", ("src/unrelated.rs",))
        uncertain = {path: list(reasons) for path, reasons in owners.uncertain.items()}
        owners.propagate()
        self.assertEqual(dict(owners.uncertain), uncertain)

    def test_cross_file_anchored_loader_aliases_keep_all_module_instances(self) -> None:
        for anchor, declaration in (
            ("crate::loaders", "mod caller; mod loaders;"),
            ("super::loaders", "mod caller; mod loaders;"),
            ("super::loaders",
             'mod first { #[path="../caller.rs"] mod caller; '
             + 'mod loaders { pub use std::include_str as source; } } '
             + 'mod second { #[path="../caller.rs"] mod caller; '
             + 'mod loaders { pub use core::include_bytes as source; } }'),
        ):
            with self.subTest(anchor=anchor, declaration=declaration):
                for folder in ("first", "second"):
                    (self.repo / "beta/src" / folder).mkdir(exist_ok=True)
                _ = (self.repo / "src/lib.rs").write_text("pub fn shared() {}\n")
                _ = (self.repo / "beta/src/lib.rs").write_text(declaration + "\n")
                _ = (self.repo / "beta/src/loaders.rs").write_text(
                    "pub use std::include_str as source;\n")
                _ = (self.repo / "beta/src/caller.rs").write_text(
                    f'use {anchor}::source; const CODE: &str = source!("../../src/lib.rs");\n')
                _ = self.git("add", ".")
                _ = self.git("commit", "-qm", "anchored cross-file loader aliases")
                owners = self.ownership()
                self.assertIn("src/lib.rs", owners.edges["beta/src/caller.rs"])
                self.assertEqual(owners.owners["src/lib.rs"], {".", "beta"})
                self.assertNotIn("beta/src/caller.rs", owners.uncertain)
                with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
                    owners.require(".", ("src/lib.rs",))
                if "mod first" in declaration:
                    self.assertEqual(owners.module_parents["beta/src/caller.rs"], {
                        ("beta/src/lib.rs", ("first", "caller")),
                        ("beta/src/lib.rs", ("second", "caller")),
                    })
                edges = dict(owners.edges)
                owners.propagate()
                self.assertEqual(owners.edges, edges)

    def test_unproved_anchored_loader_protects_possible_target_closure(self) -> None:
        for path, source in {
            "src/lib.rs": "mod child;",
            "src/child.rs": "pub fn child() {}",
            "beta/src/orphan.rs": 'use crate::loaders::source; '
                                  + 'const CODE: &str = source!("../../src/lib.rs");',
        }.items():
            _ = (self.repo / path).write_text(source + "\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "unproved anchored loader closure")
        owners = self.ownership()
        self.assertIn("src/lib.rs", owners.edges["beta/src/orphan.rs"])
        for path in ("beta/src/orphan.rs", "src/lib.rs", "src/child.rs"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(bulk.BulkRenameError, "unproved anchored loader module instance"):
                    owners.require(owners.home(path), (path,))

    def test_included_root_named_files_protect_anchored_loader_targets(self) -> None:
        for name in ("mod.rs", "lib.rs", "main.rs", "part.rs"):
            with self.subTest(name=name):
                included = "beta/src/nested/" + name
                for path, source in {
                    "src/lib.rs": "pub fn shared() {}",
                    "src/unrelated.rs": "pub fn unrelated() {}",
                    "beta/src/lib.rs": 'mod loaders { pub use std::include_str as source; } '
                                       + f'include!("nested/{name}");',
                    included: 'use crate::loaders::source; '
                              + 'const CODE: &str = source!("../../../src/lib.rs");',
                }.items():
                    target = self.repo / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _ = target.write_text(source + "\n")
                _ = self.git("add", ".")
                _ = self.git("commit", "-qm", "included file is not a Cargo root")
                owners = self.ownership()
                self.assertNotIn(included, owners.crate_roots)
                self.assertIn(included, owners.edges["beta/src/lib.rs"])
                self.assertIn("src/lib.rs", owners.edges[included])
                self.assertEqual(owners.owners["src/lib.rs"], {".", "beta"})
                for path in (included, "src/lib.rs"):
                    with self.assertRaisesRegex(
                            bulk.BulkRenameError, "unproved anchored loader module instance"):
                        owners.require(owners.home(path), (path,))
                owners.require(".", ("src/unrelated.rs",))
                edges = dict(owners.edges)
                uncertain = {path: list(reasons) for path, reasons in owners.uncertain.items()}
                owners.propagate()
                self.assertEqual(owners.edges, edges)
                self.assertEqual(dict(owners.uncertain), uncertain)

    def test_configured_root_identity_uses_frozen_manifest_and_sources(self) -> None:
        for path, source in {
            "src/lib.rs": "pub fn shared() {}",
            "beta/Cargo.toml": '[package]\nname="beta"\nversion="0.1.0"\n'
                               + '[lib]\npath="src/entry.rs"',
            "beta/src/entry.rs": "mod caller; mod loaders;",
            "beta/src/caller.rs": 'use crate::loaders::source; '
                                  + 'const CODE: &str = source!("../../src/lib.rs");',
            "beta/src/loaders.rs": "pub use std::include_str as source;",
        }.items():
            _ = (self.repo / path).write_text(source + "\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "configured Cargo root")
        _ = (self.repo / "beta/Cargo.toml").write_text("invalid mutable manifest")
        (self.repo / "beta/src/entry.rs").unlink()
        members = rules.workspace_members({
            path: self.git("show", "HEAD:" + path)
            for path in ("Cargo.toml", "beta/Cargo.toml")})
        owners = crates.SourceOwnership(self.repo, self.git("rev-parse", "HEAD"), members)
        self.assertEqual(owners.crate_roots, {"src/lib.rs", "beta/src/entry.rs"})
        self.assertIn("src/lib.rs", owners.edges["beta/src/caller.rs"])
        self.assertEqual(owners.owners["src/lib.rs"], {".", "beta"})
        self.assertNotIn("beta/src/caller.rs", owners.uncertain)
        with self.assertRaisesRegex(bulk.BulkRenameError, "shared or cross-crate"):
            owners.require(".", ("src/lib.rs",))
