"""Production rename: workers behavior."""

from __future__ import annotations

import json
import os
import unittest
from typing import cast, final
from unittest import mock

from scripts.astra import design_roles
from scripts.direct import workers
from scripts.production import rename_test_support as support
from scripts.production.rename_test_support import NEW, OLD


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_worker_migration_conflict_prints_a_refusal_without_a_traceback(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            ledger = workers.UnitWorkerLedger(workers.ledger_path(OLD, "alpha-unit"))
            with ledger.locked():
                pass
        result = self.run_command("apply", OLD, NEW, wrapper='import runpy, sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[2]))\nfrom scripts.direct import workers\ndef unavailable(*args, **kwargs):\n    raise workers.WorkerUnavailable("worker ownership changed; rerun apply")\nworkers.move_unit = unavailable\nworkers.rename_unit = unavailable\nworkers.end_unit = unavailable\nsys.argv = sys.argv[1:]\nsys.path.insert(0, str(Path(sys.argv[0]).parent))\nrunpy.run_path(sys.argv[0], run_name="__main__")\n')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("worker ownership changed; rerun apply", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_production_rename_moves_every_unit_worker_ledger_with_its_backend(self) -> None:
        originals: dict[str, workers.UnitWorker] = {}
        with mock.patch.dict(os.environ, self.env):
            for unit in ("alpha-unit", "beta-unit", "showrunner"):
                store = workers.UnitWorkerLedger(workers.ledger_path(OLD, unit))
                worker = workers.UnitWorker("w7", "impl", "codex", "kept-model", "xhigh", {},
                    workers.CodexThread("thread-" + unit, str(self.home / "codex")), workers.Paused(), [])
                with store.locked() as ledger:
                    ledger.workers[worker.name] = worker
                originals[unit] = worker
        self.assert_success(self.run_rename("apply"))
        with mock.patch.dict(os.environ, self.env):
            for unit, worker in originals.items():
                self.assertFalse(workers.ledger_path(OLD, unit).exists())
                with workers.UnitWorkerLedger(workers.ledger_path(NEW, unit)).locked() as ledger:
                    self.assertEqual(ledger.workers["w7"], worker)

    def test_unit_move_repoints_paused_workers_and_the_next_run_adopts_them(self) -> None:
        self.prepare_unit_moves()
        run = self.seed_paused_unit_worker()
        self.assert_success(self.run_rename("apply"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        new = self.unit_worktree(NEW, "alpha-unit")
        state = cast(dict[str, object], json.loads((run / workers.STATE).read_text()))
        self.assertEqual((state["working_dir"], state["plan_doc"], state["production"], state["unit"]),
                         (str(new), str(new / "docs/plans/alpha-unit.md"), NEW, "alpha-unit"))
        roster = cast(dict[str, dict[str, object]], json.loads((run / workers.ROSTER).read_text()))
        self.assertEqual((roster["w7"]["cwd"], roster["w7"]["status"]), (str(new / "subdir"), "paused"))
        before = self.snapshot()
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assertEqual(self.snapshot(), before)
        destination = self.root / "runs/after-move"
        self.write(destination / workers.STATE, json.dumps({"status": "active", "working_dir": str(new),
            "plan_doc": "docs/plans/alpha-unit.md", "run_id": destination.name}))
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(workers.adopt(run, destination, "w7"), "w7")
            target = workers.ledger_for_run(destination)
            self.assertEqual(target.path, workers.ledger_for_run(run).path)
            with target.locked() as ledger:
                self.assertEqual(ledger.workers["w7"].backend,
                                 workers.CodexThread("kept-thread", str(self.root / "codex-home")))

    def test_repair_workers_fixes_old_moves_and_dry_run_and_second_run_change_nothing(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        # Old code completed the worktree move without changing worker state.
        run = self.seed_paused_unit_worker()
        journal = self.journal()
        record = cast(dict[str, dict[str, object]], journal["unit_moves"])["alpha-unit"]
        _ = record.pop("workers_repointed")
        self.write(self.state / "new/rename.json", json.dumps(journal))
        before = self.snapshot()
        preview = self.run_command("repair-workers", NEW, "--dry-run")
        self.assert_success(preview)
        self.assertIn("would change", preview.stdout)
        self.assertEqual(self.snapshot(), before)
        fixed = self.run_command("repair-workers", NEW)
        self.assert_success(fixed)
        self.assertIn("changed worker ledger", fixed.stdout)
        self.assertIn("changed worker", fixed.stdout)
        state = cast(dict[str, object], json.loads((run / workers.STATE).read_text()))
        new = self.unit_worktree(NEW, "alpha-unit")
        self.assertEqual((state["working_dir"], state["production"], state["unit"]),
                         (str(new), NEW, "alpha-unit"))
        before = self.snapshot()
        again = self.run_command("repair-workers", NEW)
        self.assert_success(again)
        self.assertIn("alpha-unit: workers have nothing to change\n", again.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_repair_uses_worker_cwd_when_run_worktree_is_already_current(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        run = self.seed_paused_unit_worker()
        journal = self.journal()
        del journal["unit_moves"]
        self.write(self.state / "new/rename.json", json.dumps(journal))
        new = self.unit_worktree(NEW, "alpha-unit")
        state = cast(dict[str, object], json.loads((run / workers.STATE).read_text()))
        state["working_dir"] = str(new)
        state["plan_doc"] = "docs/plans/alpha-unit.md"
        self.write(run / workers.STATE, json.dumps(state))
        # Both rows name the same folder. The plan header selects alpha-unit.
        self.write(self.new_doc, self.new_doc.read_text().replace(
            str(self.unit_worktree(OLD, "beta-unit")), str(new)))
        fixed = self.run_command("repair-workers", NEW)
        self.assert_success(fixed)
        self.assertNotIn("ambiguous unit", fixed.stdout)
        roster = cast(dict[str, dict[str, object]], json.loads((run / workers.ROSTER).read_text()))
        self.assertEqual(roster["w7"]["cwd"], str(new / "subdir"))
        self.assertEqual(roster["w7"]["unit_ledger"], str(self.root / "state/unit-workers/new/alpha-unit.json"))

    def test_unit_retry_after_worker_writes_finishes_without_changing_workers_again(self) -> None:
        self.prepare_unit_moves()
        run = self.seed_paused_unit_worker()
        self.assert_success(self.run_rename("apply"))
        wrapper = '''import runpy, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[2]))
from scripts.direct import workers
repoint = workers.repoint_unit_worktree
def interrupt(*args, **kwargs):
    repoint(*args, **kwargs)
    raise OSError("stop after worker writes")
workers.repoint_unit_worktree = interrupt
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        stopped = self.run_command("unit", NEW, "alpha-unit", wrapper=wrapper)
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("stop after worker writes", stopped.stderr)
        record = cast(dict[str, dict[str, object]], self.journal()["unit_moves"])["alpha-unit"]
        self.assertEqual(record["step"], 2)
        self.assertNotIn("workers_repointed", record)
        before = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assert_unit_moved("alpha-unit")

    def test_held_roles_move_to_the_new_slug_with_the_showrunner_and_each_unit(self) -> None:
        self.prepare_unit_moves()
        config = self.root / "design_roles.conf"
        self.write(config, "[ownership]\nhost=fixture-host\n")
        self.env.update({"DESIGN_ROLES_CONFIG": str(config), "DESIGN_ROLES_HOST": "fixture-host",
                         "DESIGN_ROLES_STATE": str(self.root / "state/astra/design-roles"),
                         "DESIGN_OWNERSHIP_STATE": str(self.root / "state/astra/design-ownership")})
        old_unit, new_unit = self.unit_worktree(OLD, "alpha-unit"), self.unit_worktree(NEW, "alpha-unit")
        showrunner = self.held_design("showrunner-repo", self.checkout, design_roles.ShowrunnerHolder(OLD))
        unit = self.held_design("unit-repo", old_unit, design_roles.UnitHolder(OLD, "alpha-unit"))
        self.assert_success(self.run_rename("apply"))
        current, cwd = self.read_held(showrunner)
        self.assertEqual(current.holder, design_roles.ShowrunnerHolder(NEW))
        self.assertEqual((current.repository, current.roles, cwd), (str(self.checkout), showrunner.roles, str(self.checkout)))
        self.assertEqual(self.read_held(unit)[0].holder, design_roles.UnitHolder(OLD, "alpha-unit"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")
        current, cwd = self.read_held(unit)
        self.assertEqual(current.holder, design_roles.UnitHolder(NEW, "alpha-unit"))
        self.assertEqual((current.repository, current.document, cwd), (str(new_unit), str(new_unit / "design.md"), str(new_unit)))
        self.assertEqual((current.roles, current.placement.generation), (unit.roles, 2))


if __name__ == "__main__":
    _ = unittest.main()
