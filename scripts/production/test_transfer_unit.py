"""Transfer units between disposable productions with real Git worktrees."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, final
from unittest import mock
from collections.abc import Callable
from contextlib import ExitStack

from scripts.astra import design_roles
from scripts.direct import workers
from scripts.production import staff
from scripts.production.rename_test_support import legacy_staff_journal, stop_at_staff_write
from scripts.production.rename_rerun_test_support import final_files, saved_fixture
from scripts.production.staff_test_support import SOURCES_SCRIPT, PROCESS_TIMEOUT
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production.transfer_unit_test_support import (
    UnitTransferFixture, SCRIPT, TEST_HANG_GUARD_SECS,
)
import transfer_unit
from rename_state import write_text
import fake_showrunner
import fake_unit_host


@final
class UnitTransferTests(UnitTransferFixture):
    def test_unfinished_transfer_refusal_names_its_exact_resume_command(self) -> None:
        _ = self.pause_before_source_commit()
        before = final_files(self.root)
        refused = self.transfer("apply", "beta-unit", self.giving / "docs/plans/demo-production.md")
        command = f"transfer_unit.py apply demo alpha-unit {self.receiving / 'docs/plans/other-production.md'}"
        self.assert_refused(refused, f"the unfinished transfer is alpha-unit to other; resume it first with {command}")
        self.assertEqual(final_files(self.root), before)

    def test_completed_transfer_allows_another_destination_and_its_own_rerun(self) -> None:
        self.assert_ok(self.transfer("apply"))
        self.add_transfer_unit("beta")
        third = self.add_destination("third")
        self.assert_ok(self.transfer("apply", "beta-unit", third))
        before = final_files(self.root)
        repeated = self.transfer("apply")
        self.assert_ok(repeated)
        self.assertIn("done", repeated.stdout)
        self.assertEqual(final_files(self.root), before)
        rows = staff.snapshot()
        alpha, beta = rows.by_path("other/alpha"), rows.by_path("third/beta")
        assert alpha is not None and beta is not None
        self.assertEqual(alpha.name, "alpha")
        self.assertEqual(beta.name, "beta")

    def test_legacy_journal_resumes_after_the_destination_row_changed(self) -> None:
        path = self.pause_before_source_commit()
        legacy_staff_journal(path)
        self.assert_transfer_resumed(path)
        fields = cast(dict[str, object], json.loads(path.read_text()))
        self.assertTrue(fields["staff_id"])
        self.assertTrue(fields["parent_staff_id"])

    def test_completed_legacy_journal_migrates_once_and_reports_done(self) -> None:
        self.assert_ok(self.transfer("apply"))
        expected = final_files(self.root)
        legacy_staff_journal(self.root / "state/showrunner/demo/transfer_unit.json")
        finished = self.transfer("apply")
        self.assert_ok(finished)
        self.assertIn("done", finished.stdout)
        self.assertEqual(final_files(self.root), expected)

    def test_transfer_keeps_staff_ids_and_workers(self) -> None:
        row = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
        parent = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "other")
        worker = staff.ensure("worker", name="impl1", parent=row.id)
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(row.id, "unit", "alpha", parent.id))
        self.assertEqual(staff.snapshot().by_id(worker.id), worker)
        saved = cast(dict[str, object], json.loads((self.root / "state/showrunner/demo/transfer_unit.json").read_text()))
        self.assertEqual(saved["staff_id"], row.id)

    def test_taken_staff_name_refuses_before_sources_change(self) -> None:
        parent = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "other")
        _ = staff.ensure("unit", name="alpha", parent=parent.id)
        before = final_files(self.root)
        failed = self.transfer("apply")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertIn("StaffRefused", failed.stderr)
        self.assertEqual(final_files(self.root), before)

    def test_missing_staff_member_refuses_before_sources_change(self) -> None:
        row = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
        _ = staff.retire(row.id)
        before = final_files(self.root)
        failed = self.transfer("apply")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertEqual(final_files(self.root), before)

    def test_transfer_unit_rerun(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        with saved_fixture(self.root) as restore:
            self.assert_ok(self.transfer("apply"))
            expected = final_files(self.root)
            for after in (False, True):
                with self.subTest(after=after):
                    restore()
                    with stop_at_staff_write("move", after=after):
                        failed = self.transfer("apply")
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("stopped at staff step", failed.stderr)
                    self.assert_ok(self.transfer("apply"))
                    self.assertEqual(final_files(self.root), expected)
                    finished = self.transfer("apply")
                    self.assert_ok(finished)
                    self.assertIn("done", finished.stdout)
                    self.assertEqual(final_files(self.root), expected)

    def test_seed_waits_between_staff_move_and_units_update(self) -> None:
        row = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
        parent = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "other")
        commit_to = cast(Callable[[transfer_unit.UnitTransfer, Callable[[], None]], None], getattr(transfer_unit, "_commit_to"))
        pending: list[subprocess.Popen[str]] = []

        lock = self.staff_file.parent / "operation.lock"
        with short_socket_directory() as directory, ExitStack() as contexts:
            with mock.patch.object(self, "root", Path(directory)):
                listener = contexts.enter_context(self.lock_notice(lock))
            def pause(transfer: transfer_unit.UnitTransfer, save: Callable[[], None]) -> None:
                self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(row.id, "unit", "alpha", parent.id))
                process = self.start_script(SOURCES_SCRIPT, "seed")
                pending.append(process)
                self.assert_operation_blocks(process, listener)
                commit_to(transfer, save)

            steps = tuple(step._replace(run=pause) if step.run is commit_to else step for step in transfer_unit.STEPS)
            notices = {name: os.environ[name] for name in ("STAFF_TEST_LOCK_NOTICE", "STAFF_TEST_LOCK_TARGET")}
            with mock.patch.object(transfer_unit, "STEPS", steps), mock.patch.dict(self.env, notices):
                self.assert_ok(self.transfer("apply"))
            output, errors = pending[0].communicate(timeout=PROCESS_TIMEOUT)
        self.assertEqual(pending[0].returncode, 0, output + errors)
        self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(row.id, "unit", "alpha", parent.id))

    def test_rerun_finishes_each_uncommitted_document_write(self) -> None:
        snapshot = Path(self.enterContext(tempfile.TemporaryDirectory())) / "before"
        _ = shutil.copytree(self.root, snapshot)
        self.assert_ok(self.transfer("apply"))
        checkouts = (self.giving, self.receiving, self.unit)
        expected_trees = [self.git("rev-parse", "HEAD^{tree}", cwd=path) for path in checkouts]
        documents = (self.receiving / "docs/plans/other-production.md",
                     self.giving / "docs/plans/demo-production.md")
        expected_docs = [path.read_text() for path in documents]
        for document in documents:
            with self.subTest(document=document.name):
                shutil.rmtree(self.root)
                _ = shutil.copytree(snapshot, self.root)

                def stop_after_write(path: Path, text: str) -> None:
                    write_text(path, text)
                    if path == document:
                        raise OSError("fixture stop after document write")

                with mock.patch.object(transfer_unit, "write_text", wraps=stop_after_write) as writes:
                    self.assert_refused(self.transfer("apply"), "fixture stop after document write")
                self.assertEqual(sum(call.args[0] == document for call in writes.call_args_list), 1)
                self.assertNotEqual(self.git("status", "--porcelain", "--", str(document),
                                             cwd=document.parents[2]), "")
                # Recovery must not absorb an unrelated worktree or index edit.
                expected = document.read_text()
                head = self.git("rev-parse", "HEAD", cwd=document.parents[2])
                self.write(document, expected + "unrelated edit\n")
                self.assert_refused(self.transfer("apply"), f"{document} has uncommitted changes")
                _ = self.git("add", str(document), cwd=document.parents[2])
                self.write(document, expected)
                self.assert_refused(self.transfer("apply"), f"{document} has uncommitted changes")
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=document.parents[2]), head)
                self.assertIn("unrelated edit", self.git("diff", "--cached", cwd=document.parents[2]))
                _ = self.git("reset", "HEAD", "--", str(document), cwd=document.parents[2])
                if document == documents[0]:
                    # A failed commit may already have staged the intended edit.
                    _ = self.git("add", str(document), cwd=document.parents[2])
                self.assert_transfer_resumed(self.root / "state/showrunner/demo/transfer_unit.json")
                self.assertEqual([self.git("rev-parse", "HEAD^{tree}", cwd=path) for path in checkouts],
                                 expected_trees)
                self.assertEqual([path.read_text() for path in documents], expected_docs)
                for checkout in checkouts:
                    self.assertEqual(self.git("status", "--porcelain", cwd=checkout), "")
                tips = [self.git("rev-parse", "HEAD", cwd=path) for path in checkouts]
                self.assert_ok(self.transfer("verify"))
                self.assertEqual([self.git("rev-parse", "HEAD", cwd=path) for path in checkouts], tips)

    def test_verify_ignores_leftover_process_marks(self) -> None:
        self.moved_unit_with_leftover_marks()
        self.assert_ok(self.transfer("verify"))

    def test_verify_names_the_live_holder_of_a_former_mark(self) -> None:
        self.moved_unit_with_leftover_marks()
        pid = self.live_pid()
        old = fake_showrunner.write_session(self.root / "state/sessions", "old", "sid-old", pid)
        self.addCleanup(old.close)
        fake_unit_host.place_process(self.env, pid, "w3:p3Y")
        self.assert_refused(self.transfer("verify"),
                            f"a session still carries the marks demo alpha-unit: pid {pid} (claude) in herdr:old:w3:p3Y")

    def test_resume_looks_up_the_moved_giving_checkout(self) -> None:
        path = self.pause_before_source_commit()
        self.move_giving_checkout()
        self.assert_transfer_resumed(path)

    def test_old_journal_ignores_stale_locations_for_both_productions(self) -> None:
        path = self.pause_before_source_commit()
        saved = cast(dict[str, object], json.loads(path.read_text()))
        saved.update(from_doc=str(self.giving / "docs/plans/demo-production.md"), from_checkout=str(self.giving),
                     to_doc=str(self.receiving / "docs/plans/other-production.md"), to_checkout=str(self.receiving))
        self.write(path, json.dumps(saved))
        self.move_giving_checkout()
        old = self.receiving
        self.receiving = old.with_name("claude-other-relocated")
        _ = self.git("worktree", "move", str(old), str(self.receiving))
        doc = self.receiving / "docs/plans/other-production.md"
        self.write(doc, doc.read_text().replace(str(old), str(self.receiving)))
        _ = self.git("add", str(doc), cwd=self.receiving)
        _ = self.git("commit", "-m", "relocate receiving checkout", cwd=self.receiving)
        self.assertFalse(old.exists())
        self.assert_transfer_resumed(path)
        saved = cast(dict[str, object], json.loads(path.read_text()))
        for name in ("from_doc", "to_doc", "from_checkout", "to_checkout"):
            self.assertNotIn(name, saved)

    def test_a_stopped_unit_relaunches_from_a_record_that_names_the_receiving_production(self) -> None:
        log = self.seed_record()
        result = self.transfer("apply")
        # The fake restart starts nothing, so verify finds the unit not running and holds the journal open.
        self.assert_refused(result, "alpha-unit is not running under other")
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["restart", "--only", "sid-alpha", "--here"]])
        path = self.root / "home/.local/state/shutdown/owner@example.com/sessions/sid-alpha.json"
        saved = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        host = cast(dict[str, object], cast(dict[str, object], saved["session"])["host"])
        receiving = self.receiving / "docs/plans/other-production.md"
        self.assertEqual((host["production"], host["doc"], host["unit"], host["session_name"]),
                         ("other", str(receiving), "alpha-unit", "other-alpha"))
        note = cast(str, saved["renamed"])
        self.assertIn(f"moved you to production other: your production doc is now {receiving}", note)
        self.assertIn(f"Your worktree {self.unit}, your branch demo-alpha and its commits stay.", note)

    def test_plan_lists_the_steps_and_changes_nothing(self) -> None:
        before = self.git("rev-parse", "demo", "other", "demo-alpha")
        result = self.transfer("plan")
        self.assert_ok(result)
        self.assertIn("stop alpha-unit and its Codex workers through the shutdown path", result.stdout)
        self.assertIn("8. verify: alpha-unit shows under other and nowhere under demo", result.stdout)
        self.assertEqual(self.git("rev-parse", "demo", "other", "demo-alpha"), before)

    def test_worker_migration_conflict_prints_a_refusal_without_a_traceback(self) -> None:
        result = subprocess.run([sys.executable, "-c", 'import runpy, sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[2]))\nfrom scripts.direct import workers\ndef unavailable(*args, **kwargs):\n    raise workers.WorkerUnavailable("worker ownership changed; rerun apply")\nworkers.move_unit = unavailable\nworkers.rename_unit = unavailable\nworkers.end_unit = unavailable\nsys.argv = sys.argv[1:]\nsys.path.insert(0, str(Path(sys.argv[0]).parent))\nrunpy.run_path(sys.argv[0], run_name="__main__")\n', str(SCRIPT), "apply", "demo", "alpha-unit",
            str(self.receiving / "docs/plans/other-production.md"), "--scratch", str(self.root / "scratch")],
            cwd=self.giving, env=self.env, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("worker ownership changed; rerun apply", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_transfer_rebinds_the_writer_ledger_without_changing_its_thread_or_owner(self) -> None:
        run = self.root / "runs" / "carried"
        run.mkdir(parents=True)
        state = run / "progress_history_state.json"
        self.write(state, json.dumps({"production": "demo", "unit": "alpha-unit", "working_dir": str(self.unit)}))
        with mock.patch.dict(os.environ, self.env):
            store = workers.UnitWorkerLedger(workers.ledger_path("demo", "alpha-unit"))
            worker = workers.UnitWorker("w7", "impl", "codex", "kept-model", "xhigh", {},
                workers.CodexThread("original-thread", str(self.root / "codex")), workers.Paused(),
                [workers.WorkerEvent("opened", "fixture", str(run), 1_793_188_800.0, {})])
            with store.locked() as ledger:
                ledger.workers[worker.name] = worker
        roster = run / "mesh_roster.json"
        self.write(roster, json.dumps({"w7": {"thread_id": "original-thread", "role": "impl",
            "status": "paused", "unit_ledger": str(store.path)}}))
        record = run / "mesh_runs" / "kept-command.json"
        with subprocess.Popen([sys.executable, "-c", "import sys; from pathlib import Path; sys.stdin.read(); Path('command-finished').write_text(str(Path.cwd()))",
                               "mesh_runs.py", "supervise", str(record)],
                              cwd=self.unit, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as command:
            self.write(record, json.dumps({"worker": "w7", "status": "running", "pid": command.pid, "cwd": str(self.unit)}))
            self.assert_ok(self.transfer("apply"))
            self.assertIsNone(command.poll(), "transfer ended the paused worker's detached command")
            _output, errors = command.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(command.returncode, 0, errors.decode())
            self.assertEqual((self.unit / "command-finished").read_text(), str(self.unit))
            self.assertTrue(record.exists())
        with mock.patch.dict(os.environ, self.env):
            target = workers.ledger_for_run(run)
            self.assertEqual(target.path, workers.ledger_path("other", "alpha-unit"))
            with target.locked() as ledger:
                self.assertEqual(ledger.workers["w7"], worker)
        self.assertFalse(store.path.exists())
        self.assertEqual(json.loads(roster.read_text())["w7"]["unit_ledger"], str(target.path))

    def test_apply_moves_the_row_header_and_state_and_keeps_the_branch(self) -> None:
        tip = self.git("rev-parse", "demo-alpha")
        self.assert_ok(self.transfer("apply"))
        receiving = (self.receiving / "docs/plans/other-production.md").read_text(encoding="utf-8")
        self.assertIn(f"| alpha-unit | docs/plans/alpha.md | `{self.unit}` | demo-alpha | — | — |", receiving)
        giving = (self.giving / "docs/plans/demo-production.md").read_text(encoding="utf-8")
        self.assertNotIn("alpha-unit", giving)
        self.assertIn("beta-unit", giving)
        self.assertEqual(self.git("log", "-1", "--format=%s", "other"), "chore(other): transfer unit alpha-unit from demo")
        self.assertEqual(self.git("log", "-1", "--format=%s", "demo"), "chore(demo): transfer unit alpha-unit to other")
        plan = (self.unit / "docs/plans/alpha.md").read_text(encoding="utf-8")
        self.assertIn("> **Production: other** — unit `alpha-unit`; production doc `docs/plans/other-production.md`", plan)
        self.assertIn("alpha prose stays.", plan)
        # The branch keeps its commits and gains only the header commit, which is pushed.
        self.assertEqual(self.git("rev-parse", "demo-alpha^"), tip)
        self.assertEqual(self.git("rev-parse", "origin/demo-alpha"), self.git("rev-parse", "demo-alpha"))
        self.assertTrue(self.unit.is_dir())
        self.assertFalse(self.stall_path("demo", "alpha-unit").exists())
        self.assertEqual(self.stall_path("other", "alpha-unit").read_text(encoding="utf-8"), '{"stretch": 17}\n')
        pool = (self.root / "state/review-pool/demo/pool.json").read_text(encoding="utf-8")
        self.assertNotIn("alpha-unit", pool)
        self.assert_ok(self.transfer("verify"))

    def test_a_held_role_moves_to_the_receiving_holder_and_keeps_its_thread_and_paths(self) -> None:
        config = self.root / "design_roles.conf"
        self.write(config, "[ownership]\nhost=fixture-host\n")
        self.env.update({"DESIGN_ROLES_CONFIG": str(config), "DESIGN_ROLES_HOST": "fixture-host",
                         "DESIGN_ROLES_STATE": str(self.root / "state/astra/design-roles"),
                         "DESIGN_OWNERSHIP_STATE": str(self.root / "state/astra/design-ownership")})
        document = str(self.unit / "docs/plans/alpha.md")
        with mock.patch.dict(os.environ, self.env):
            design_roles.register_repository("fixture-repo", {"fixture-host": str(self.unit)}, inventory_complete=True)
            placement = design_roles.authority_request("reserve", {"repository_id": "fixture-repo",
                "document": "docs/plans/alpha.md", "host": "fixture-host", "request_id": "transfer-fixture"})
            design = design_roles.HeldDesign(design_roles.DesignPlacement(str(placement["design_id"]), "fixture-host", 1),
                "fixture-repo", str(self.unit), document, "design", "2026-10-22T12:00:00+00:00",
                design_roles.UnitHolder("demo", "alpha-unit"), design_roles.DesignUnbound(),
                (design_roles.RoleInstance("designer", "demo-design-astra-designer", design_roles.RoleOpen("designer-thread")),
                 design_roles.RoleInstance("reviewer", "demo-design-astra-reviewer", design_roles.RoleNotOpened())))
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            mesh = design_roles.mesh_directory(design.placement.design_id)
        self.write(mesh / "mesh_roster.json", json.dumps({"demo-design-astra-designer": {
            "thread_id": "designer-thread", "status": "done", "cwd": str(self.unit)}}))
        self.assert_ok(self.transfer("apply"))
        with mock.patch.dict(os.environ, self.env):
            moved = design_roles.read_design(design.placement.design_id)
        self.assertEqual(moved.holder, design_roles.UnitHolder("other", "alpha-unit"))
        self.assertEqual((moved.repository, moved.document, moved.roles), (design.repository, design.document, design.roles))
        self.assertEqual(moved.placement.generation, 2)
        roster = cast(dict[str, dict[str, str]], json.loads((mesh / "mesh_roster.json").read_text(encoding="utf-8")))
        self.assertEqual(roster["demo-design-astra-designer"]["cwd"], str(self.unit))
        self.assert_ok(self.transfer("verify"))

    def test_work_in_progress_stays_and_only_the_header_is_committed(self) -> None:
        # showrunner, 2026-10-21: hierarchies-unit had its plan and other files edited, not committed.
        plan = self.unit / "docs/plans/alpha.md"
        self.write(plan, plan.read_text(encoding="utf-8") + "A new step, not committed.\n")
        self.write(self.unit / "measure.py", "staged = True\n")
        _ = self.git("add", "measure.py", cwd=self.unit)
        tip = self.git("rev-parse", "demo-alpha")
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(self.git("rev-parse", "demo-alpha^"), tip)
        self.assertEqual(self.git("diff", "--name-only", "demo-alpha^", "demo-alpha"), "docs/plans/alpha.md")
        committed = self.git("show", "demo-alpha:docs/plans/alpha.md")
        self.assertIn("> **Production: other**", committed)
        self.assertNotIn("A new step", committed)
        text = plan.read_text(encoding="utf-8")
        self.assertIn("> **Production: other**", text)
        self.assertIn("A new step, not committed.", text)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.unit), "M docs/plans/alpha.md\nA  measure.py")
        self.assertEqual(self.git("diff", "--cached", "--name-only", cwd=self.unit), "measure.py")

    def test_plan_refuses_a_receiving_doc_with_uncommitted_changes_before_any_stop(self) -> None:
        doc = self.receiving / "docs/plans/other-production.md"
        self.write(doc, doc.read_text(encoding="utf-8") + "edited\n")
        self.assert_refused(self.transfer("plan"), "other-production.md has uncommitted changes")

    def test_a_merge_in_progress_is_refused(self) -> None:
        self.write(self.unit / "NOTES.md", "unit side\n")
        _ = self.git("add", "NOTES.md", cwd=self.unit)
        _ = self.git("commit", "-m", "unit side", cwd=self.unit)
        self.write(self.receiving / "NOTES.md", "receiving side\n")
        _ = self.git("add", "NOTES.md", cwd=self.receiving)
        _ = self.git("commit", "-m", "receiving side", cwd=self.receiving)
        merged = subprocess.run(["git", "merge", "--no-edit", "demo-alpha"], cwd=self.receiving, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(merged.returncode, 0)
        self.assert_refused(self.transfer("plan"), "a merge is in progress")

    def test_giving_only_commits_are_refused(self) -> None:
        self.write(self.giving / "LATER.md", "giving only\n")
        _ = self.git("add", "LATER.md")
        _ = self.git("commit", "-m", "giving only")
        _ = self.git("push", "origin", "demo")
        _ = self.git("merge", "--no-edit", "demo", cwd=self.unit)
        self.assert_refused(self.transfer("plan"), "commit(s) that other lacks")

    def test_another_repository_is_refused(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        _ = self.git("init", "-b", "far", cwd=elsewhere)
        self.write(elsewhere / "docs/plans/far-production.md", self.production_doc(
            "far", elsewhere, "| Unit | Plan | Worktree | Branch |", "| --- | --- | --- | --- |"))
        self.assert_refused(self.transfer("plan", to=elsewhere / "docs/plans/far-production.md"),
                            "another repository")

    def test_a_unit_without_a_worktree_here_is_refused(self) -> None:
        self.assert_refused(self.transfer("plan", unit="beta-unit"), "a Mac unit is not moved")


if __name__ == "__main__":
    _ = unittest.main()
