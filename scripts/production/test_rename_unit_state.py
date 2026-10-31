"""Worker ownership, live references and resumable unit rename state."""

from __future__ import annotations

import json
import os
import shlex
import sys
import unittest
from pathlib import Path
from typing import cast, final
from unittest import mock
from scripts.astra import design_roles
from scripts.direct import workers

sys.path.insert(0, str(Path(__file__).resolve().parent))



from scripts.production.rename_unit_test_support import UnitRenameFixture, SLUG
from rename_state import rename_claim


@final
class UnitRenameStateTests(UnitRenameFixture):

    def test_resume_looks_up_the_checkout_even_with_legacy_journal_paths(self) -> None:
        self.assert_refused(self.rename("apply", refuse="push origin demo:refs/heads/demo"), "simulated failure")
        saved = self.journal()
        for name in ("doc", "checkout"):
            self.assertNotIn(name, saved)
        saved.update(doc=str(self.doc), checkout=str(self.checkout))
        self.write(self.state / SLUG / "rename_unit.json", json.dumps(saved))
        old = self.checkout
        self.checkout = old.with_name("claude-demo-relocated")
        _ = old.rename(self.checkout)
        self.doc = self.checkout / "docs/plans/demo-production.md"
        _ = self.git("worktree", "repair")
        self.write(self.doc, self.doc.read_text().replace(str(old), str(self.checkout)))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "relocate showrunner checkout")
        conf = self.root / "state/notifier/showrunner-demo/conf"
        self.write(conf, conf.read_text().replace(str(old), str(self.checkout)))
        self.assertFalse(old.exists())
        self.assert_ok(self.rename("apply"))
        self.assertIs(self.journal()["done"], True)
        self.assertIn("| gamma-unit |", self.doc.read_text())
        self.assert_ok(self.rename("verify"))
        for name in ("doc", "checkout"):
            self.assertNotIn(name, self.journal())

    def test_rename_keeps_the_worker_address_and_rebinds_the_ledger_context(self) -> None:
        run = self.root / "runs" / "carried"
        run.mkdir(parents=True)
        old_worktree = self.rust / "claude-demo-alpha"
        self.write(run / "progress_history_state.json", json.dumps({"production": SLUG,
            "unit": "alpha-unit", "working_dir": str(old_worktree)}))
        with mock.patch.dict(os.environ, self.env):
            store = workers.UnitWorkerLedger(workers.ledger_path(SLUG, "alpha-unit"))
            worker = workers.UnitWorker("w7", "impl", "codex", "kept-model", "xhigh", {},
                workers.CodexThread("original-thread", str(self.root / "codex")), workers.Paused(),
                [workers.WorkerEvent("opened", "fixture", str(run), 1_793_188_800.0, {})])
            with store.locked() as ledger:
                ledger.workers[worker.name] = worker
        self.write(run / "mesh_roster.json", json.dumps({"w7": {"thread_id": "original-thread", "role": "impl",
            "status": "paused", "unit_ledger": str(store.path)}}))
        result = self.rename("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with mock.patch.dict(os.environ, self.env):
            target = workers.ledger_for_run(run)
            self.assertEqual(target.path, workers.ledger_path(SLUG, "gamma-unit"))
            with target.locked() as ledger:
                self.assertEqual(ledger.workers["w7"], worker)
        self.assertFalse(store.path.exists())
        self.assertEqual(json.loads((run / "mesh_roster.json").read_text())["w7"]["unit_ledger"], str(target.path))

    def test_verify_fails_on_an_unclassified_stale_reference(self) -> None:
        self.assert_ok(self.rename("apply"))
        self.write(self.root / "state/nightly-review/2026-10-16/report.md", "alpha-unit reviewed\n")
        self.write(self.root / "state/other/names.json", '{"unit": "claude-demo-alphabet"}\n')
        self.write(self.root / "state/other/__pycache__/names.cpython-313.pyc", "alpha-unit\n")
        self.write(self.root / "state/other/names.pyc", "alpha-unit\n")
        self.write(self.root / "state/conversation-pause/scheduled-prompts/sid.json",
                   '["Renamed from alpha-unit to gamma-unit."]\n')
        self.assert_ok(self.rename("verify"))
        stale = self.root / "state/other/cache.json"
        self.write(stale, '{"unit": "alpha-unit"}\n')
        self.write(self.root / "scratch/showrunner_state.json", '{"units": {"alpha-unit": {}}}\n')
        result = self.rename("verify")
        self.assert_refused(result, f"unclassified: {stale} still holds an old name")
        self.assertIn(f"unclassified: {self.root / 'scratch/showrunner_state.json'} still holds", result.stderr)
        self.assertNotIn("nightly-review", result.stderr)
        self.assertNotIn("names.json", result.stderr)

    def test_the_live_lists_take_the_new_name_and_verify_passes(self) -> None:
        # sdlc, 2026-10-21: verify called the showrunner's own `next` text unclassified after a rename.
        self.write(self.root / "scratch/showrunner_state.json", json.dumps({
            "units": [{"unit": "alpha-unit"}], "next": ["merge alpha-unit from demo-alpha", "keep the per-unit gate"],
            "open_for_user": [{"text": "alpha-unit asks", "question": "Does alpha-unit ship?"}]}))
        outstanding = self.state / "outstanding/demo.json"
        self.write(outstanding, json.dumps([{"since": "2026-10-21T09:00", "text": "alpha-unit asks",
                                             "question": "Does alpha-unit ship?"}]))
        self.assert_ok(self.rename("apply"))
        state = cast(dict[str, object], json.loads((self.root / "scratch/showrunner_state.json").read_text()))
        self.assertEqual(state["next"], ["merge gamma-unit from gamma", "keep the per-unit gate"])
        self.assertEqual(state["open_for_user"], [{"text": "gamma-unit asks", "question": "Does gamma-unit ship?"}])
        self.assertEqual(cast(list[dict[str, str]], json.loads(outstanding.read_text()))[0]["text"], "gamma-unit asks")
        self.assert_ok(self.rename("verify"))

    def test_other_showrunners_lists_take_the_new_name_unless_their_production_has_the_id(self) -> None:
        """cargo-liner-style, 2026-10-21: the parent production's showrunner state named the unit."""
        states: dict[str, Path] = {}
        for slug, row in (("parent", "| other-unit | p.md | `/x` | other |"), ("twin", "| alpha-unit | t.md | `/y` | alpha |"),
                          ("orphan", "")):
            doc = self.root / f"{slug}/docs/{slug}-production.md"
            self.write(doc, f"# {slug}\n\n## Units\n\n| Unit | Plan | Worktree | Branch |\n|---|---|---|---|\n{row}\n")
            if slug != "orphan":
                self.write(self.root / f"state/notifier/showrunner-{slug}/conf",
                           "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                           + shlex.join(["zsh", "/scripts/production_check.sh", str(doc)]) + "\n")
            states[slug] = self.state / slug / "session/showrunner_state.json"
            self.write(states[slug], json.dumps({"next": ["wait for alpha-unit"],
                                                 "open_for_user": [{"text": "alpha-unit asks", "question": "Q?"}]}))
        outstanding = self.state / "outstanding/parent.json"
        self.write(outstanding, json.dumps([{"since": "2026-10-21T09:00", "text": "alpha-unit asks", "question": "Q?"}]))
        self.assert_ok(self.rename("apply"))
        parent = cast(dict[str, object], json.loads(states["parent"].read_text()))
        self.assertEqual(parent["next"], ["wait for gamma-unit"])
        self.assertEqual(parent["open_for_user"], [{"text": "gamma-unit asks", "question": "Q?"}])
        self.assertEqual(cast(list[dict[str, str]], json.loads(outstanding.read_text()))[0]["text"], "gamma-unit asks")
        # twin has its own alpha-unit, and orphan's production cannot be read: both keep their text.
        for slug in ("twin", "orphan"):
            self.assertIn("wait for alpha-unit", states[slug].read_text())
        self.assert_ok(self.rename("verify"))

    def test_resume_after_an_interrupted_merge_branch_push(self) -> None:
        self.assert_refused(self.rename("apply", refuse="push origin demo:refs/heads/demo"), "simulated failure")
        journal = self.journal()
        self.assertEqual((journal["step"], journal["done"]), (4, False))
        self.assertEqual(journal["doc_commit"], self.git("rev-parse", "demo"))
        refused = self.rename("apply", "alpha", "delta")
        self.assert_refused(refused, "the unfinished rename is alpha-unit to gamma-unit")
        before = self.snapshot()
        self.assert_refused(self.rename("plan", "beta", "delta"), "resume it first")
        self.assertEqual(self.snapshot(), before)
        self.assert_ok(self.rename("apply"))
        subjects = self.git("log", "--format=%s", "demo").splitlines()
        self.assertEqual(subjects.count("chore(demo): rename unit alpha-unit to gamma-unit"), 1)
        self.assertEqual(self.git("rev-parse", "demo"), self.git("rev-parse", "origin/demo"))

    def test_busy_held_role_blocks_rename_state_then_retry_updates_live_routes(self) -> None:
        old = self.rust / "claude-demo-alpha"
        new = self.home / "worktrees/gamma"
        config = self.root / "design_roles.conf"
        self.write(config, "[ownership]\nhost=fixture-host\n")
        self.env.update({"DESIGN_ROLES_CONFIG": str(config), "DESIGN_ROLES_HOST": "fixture-host"})
        design = design_roles.HeldDesign(design_roles.DesignPlacement("00000000-0000-4000-8000-000000000056", "fixture-host", 1),
            "fixture-repo", str(old), str(old / "docs/plans/alpha.md"), "design", "2026-10-21T12:00:00+00:00",
            design_roles.UnitHolder("demo", "alpha-unit"), design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "original-designer-alias", design_roles.RoleOpen("original-designer-thread")),
             design_roles.RoleInstance("reviewer", "original-reviewer-alias", design_roles.RoleOpen("original-reviewer-thread"))))
        with mock.patch.dict(os.environ, self.env):
            design_roles.register_repository("fixture-repo", {"fixture-host": str(old)}, inventory_complete=True)
            placement = design_roles.authority_request("reserve", {"repository_id": "fixture-repo",
                "document": "docs/plans/alpha.md", "host": "fixture-host", "request_id": "rename-fixture"})
            design = design_roles.HeldDesign(design_roles.DesignPlacement(str(placement["design_id"]), "fixture-host", 1),
                design.repository_id, design.repository, design.document, design.area, design.created_at_utc,
                design.holder, design.binding, design.roles)
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            mesh = design_roles.mesh_directory(design.placement.design_id)
        mesh.mkdir(parents=True)
        roster = {instance.name: {"thread_id": cast(design_roles.RoleOpen, instance.lifecycle).thread_id,
                                  "status": "running" if instance.role == "designer" else "done", "cwd": str(old)}
                  for instance in design.roles}
        self.write(mesh / "mesh_roster.json", json.dumps(roster))
        result = self.rename("apply")
        self.assert_refused(result, "busy")
        self.assertFalse(self.journal()["done"])
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(design_roles.read_design(design.placement.design_id).holder, design.holder)
        roster["original-designer-alias"]["status"] = "done"
        self.write(mesh / "mesh_roster.json", json.dumps(roster))
        self.assert_ok(self.rename("apply"))
        with mock.patch.dict(os.environ, self.env):
            renamed = design_roles.read_design(design.placement.design_id)
        self.assertEqual(renamed.holder, design_roles.UnitHolder("demo", "gamma-unit"))
        self.assertEqual((renamed.repository, renamed.document), (str(new), str(new / "docs/plans/alpha.md")))
        self.assertEqual([(instance.name, instance.lifecycle) for instance in renamed.roles],
                         [(instance.name, instance.lifecycle) for instance in design.roles])
        self.assertEqual(renamed.placement.generation, 2)
        self.assertEqual({entry["cwd"] for entry in cast(dict[str, dict[str, str]], json.loads((mesh / "mesh_roster.json").read_text())).values()},
                         {str(new)})

    def test_add_unit_refuses_a_unit_being_renamed(self) -> None:
        self.assert_refused(self.rename("apply", refuse="push origin demo:refs/heads/demo"), "simulated failure")
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(" ".join(sorted(rename_claim(SLUG))), "alpha-unit gamma-unit")


if __name__ == "__main__":
    _ = unittest.main()
