"""Git moves, planning and refusals for one production unit."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, final
from scripts.production import staff

sys.path.insert(0, str(Path(__file__).resolve().parent))



from scripts.production.rename_unit_test_support import UnitRenameFixture, SCRIPT, SLUG
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class UnitRenameTests(UnitRenameFixture):
    def test_rename_unit_preserves_staff_and_worker_identity(self) -> None:
        row = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
        worker = staff.ensure("worker", name="impl1", parent=row.id)
        self.assert_ok(self.rename("apply"))
        self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(row.id, "unit", "gamma", row.parent))
        self.assertEqual(staff.snapshot().by_id(worker.id), worker)
        self.assertEqual(self.journal()["staff_id"], row.id)

    def test_taken_staff_name_refuses_before_sources_change(self) -> None:
        row = staff.snapshot().find(kind="showrunner")[0]
        _ = staff.ensure("unit", name="gamma", parent=row.id)
        before = self.snapshot()
        failed = self.rename("apply")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertIn("StaffRefused", failed.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_missing_staff_member_refuses_before_sources_change(self) -> None:
        row = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
        _ = staff.retire(row.id)
        before = self.snapshot()
        failed = self.rename("apply")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertEqual(self.snapshot(), before)


    def test_worker_migration_conflict_prints_a_refusal_without_a_traceback(self) -> None:
        result = subprocess.run([sys.executable, "-c", 'import runpy, sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[2]))\nfrom scripts.direct import workers\ndef unavailable(*args, **kwargs):\n    raise workers.WorkerUnavailable("worker ownership changed; rerun apply")\nworkers.move_unit = unavailable\nworkers.rename_unit = unavailable\nworkers.end_unit = unavailable\nsys.argv = sys.argv[1:]\nsys.path.insert(0, str(Path(sys.argv[0]).parent))\nrunpy.run_path(sys.argv[0], run_name="__main__")\n', str(SCRIPT), "apply", SLUG, "alpha", "gamma",
            "--scratch", str(self.root / "scratch")], cwd=self.checkout, env=self.env,
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("worker ownership changed; rerun apply", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_plan_writes_nothing_and_names_each_step(self) -> None:
        before = self.snapshot()
        result = self.rename("plan")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f"branch demo-alpha -> demo-gamma; worktree {self.rust / 'claude-demo-alpha'} -> "
                      + str(self.rust / "claude-demo-gamma"), result.stdout)
        self.assertIn("'chore(demo): rename unit alpha-unit to gamma-unit'", result.stdout)

    def test_apply_moves_every_place(self) -> None:
        records = self.home / ".local/state/herdr-panes"
        for session, panes in (("agents", {"w1:p2": "alpha-unit", "w1:p3": "beta-unit"}),
                               ("work", {"w2:p1": "alpha-unit"})):
            self.write(records / f"{session}.json", json.dumps(
                {pane: {"session_id": f"session-{unit}", "written_at": "2026-10-30T09:00:00+00:00",
                        "environment": {"SHOWRUNNER_UNIT_ID": unit}} for pane, unit in panes.items()}))
        self.assert_ok(self.rename("apply", "alpha-unit", "gamma-unit"))
        # The old pane's entry would resume the old name; the relaunched unit records its new pane.
        self.assertEqual(json.loads((records / "agents.json").read_text()),
                         {"w1:p3": {"session_id": "session-beta-unit", "written_at": "2026-10-30T09:00:00+00:00",
                                    "environment": {"SHOWRUNNER_UNIT_ID": "beta-unit"}}})
        self.assertEqual(json.loads((records / "work.json").read_text()), {})
        new = self.rust / "claude-demo-gamma"
        self.assertFalse((self.rust / "claude-demo-alpha").exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=new), "demo-gamma")
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "demo-alpha"), "")
        self.assertEqual(self.git("branch", "--list", "demo-alpha"), "")
        unit_tip = self.git("rev-parse", "demo-gamma")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/demo-gamma").split()[0], unit_tip)
        # The unit took the later merge-branch work and the rename commit.
        self.assertTrue((new / "NOTES.md").exists())
        self.assertEqual(self.git("show", "-s", "--format=%s", "demo"), "chore(demo): rename unit alpha-unit to gamma-unit")
        _ = self.git("merge-base", "--is-ancestor", "demo", "demo-gamma")
        self.assertEqual(self.git("rev-parse", "demo"), self.git("rev-parse", "origin/demo"))
        doc = self.doc.read_text()
        self.assertIn(f"| gamma-unit | docs/plans/alpha.md | `{new}` | demo-gamma |", doc)
        self.assertIn("| `docs/alpha-unit.md` | gamma-unit | beta-unit (after gamma-unit Phase 2) |", doc)
        self.assertIn("| alpha-unit gate | beta-unit | gamma-unit Phase 2 | alpha-unit merges |", doc)
        self.assertIn("| old gate | alpha-unit | beta-unit | cleared 10-01 when alpha-unit merged |", doc)
        self.assertIn("History mentions alpha-unit and stays.", doc)
        plan = (self.checkout / "docs/plans/alpha.md").read_text()
        self.assertIn("> **Production:** demo (gamma-unit)", plan)
        self.assertIn("alpha-unit history stays in prose.", plan)
        self.assertFalse(self.stall_path("alpha-unit").exists())
        self.assertTrue(self.stall_path("gamma-unit").exists())
        pool = cast(dict[str, dict[str, dict[str, object]]],
                    json.loads((self.root / "state/review-pool/demo/pool.json").read_text()))
        self.assertEqual(pool["reviewers"]["r1"]["lease"], {"unit": "gamma-unit", "ordinal": 3})
        self.assertEqual(pool["reviewers"]["r1"]["thread_id"], "thread-r1")
        self.assertEqual(cast(dict[str, object], pool["reviewers"]["r2"]["lease"])["unit"], "mul_add-unit")
        started = cast(dict[str, object], json.loads((self.root / "history/runs/run-1.jsonl").read_text().splitlines()[0]))
        self.assertEqual((started["working_dir"], started["branch"], started["plan_doc"]),
                         (str(new), "demo-gamma", f"{new}/docs/plans/alpha.md"))
        recorder = cast(dict[str, object], json.loads((self.root / "sessions/s1/progress_history_state.json").read_text()))
        self.assertEqual((recorder["working_dir"], recorder["worktree"]), (str(new), new.name))
        self.assertIn('unit: "gamma-unit"', (self.root / "issues/mine.md").read_text())
        self.assertIn("unit: alpha-unit", (self.root / "issues/other-production.md").read_text())
        self.assertIn("unit: gamma-unit", (self.root / "vault/showrunners/demo-sr/alpha-unit.md").read_text())
        self.assertIn("unit: alpha-unit", (self.root / "vault/showrunners/other-sr/alpha-unit.md").read_text())
        self.assertIs(self.journal()["done"], True)

    def test_apply_renames_a_note_owned_by_a_former_production_slug(self) -> None:
        self.write(self.doc, self.doc.read_text().replace(
            "## Production Context\n",
            "## Production Context\n\n- **Formerly:** `old-name`\n",
            1,
        ))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record the former production name")
        note = self.root / "vault/showrunners/demo-sr/alpha-unit.md"
        self.write(note, note.read_text().replace(
            "production: demo",
            'production_file: "https://github.com/example/project/blob/main/docs/plans/old-name-production.md"',
        ))
        before = note.read_bytes()
        unrelated = self.root / "vault/showrunners/other-sr/alpha-unit.md"
        unrelated_before = unrelated.read_bytes()

        result = self.rename("apply", "alpha-unit", "gamma-unit")

        self.assert_ok(result)
        self.assertEqual(note.read_bytes(), before.replace(b"unit: alpha-unit\n", b"unit: gamma-unit\n"))
        self.assertEqual(unrelated.read_bytes(), unrelated_before)
        self.assert_ok(self.rename("verify", "alpha-unit", "gamma-unit"))

    def test_mul_add_naming_keeps_the_underscore_id(self) -> None:
        self.assert_ok(self.rename("apply", "mul_add-unit", "fma"))
        self.assertIn(f"| fma-unit | docs/plans/mul-add.md | `{self.rust / 'claude-demo-fma'}` | demo-fma |",
                      self.doc.read_text())

    def test_refusals(self) -> None:
        cases = [
            (("alpha", "nightly-review-unit"), "is known to code"),
            (("alpha", "mul_add"), "already in the Units table"),
            (("alpha", "bad name"), "letters, numbers"),
            (("missing", "gamma"), "expected one Units row"),
        ]
        for (old, new), text in cases:
            with self.subTest(old=old, new=new):
                self.assert_refused(self.rename("apply", old, new), text)
        self.assertFalse((self.state / SLUG / "rename_unit.json").exists())

    def test_an_older_run_keeps_the_old_path_as_history(self) -> None:
        old = self.rust / "claude-demo-alpha"
        older = self.root / "history/runs/run-0.jsonl"
        self.write(older, json.dumps({"event_type": "run_started", "run_started_at": 0, "working_dir": str(old)})
                   + "\n" + json.dumps({"event_type": "run_finished"}) + "\n")
        self.assert_ok(self.rename("apply"))
        self.assertIn(str(old), older.read_text())
        newest = cast(dict[str, object], json.loads((self.root / "history/runs/run-1.jsonl").read_text().splitlines()[0]))
        self.assertEqual(newest["working_dir"], str(self.rust / "claude-demo-gamma"))
        self.assert_ok(self.rename("verify"))

    def test_untracked_logs_in_the_checkout_do_not_block_and_stay_out_of_the_commit(self) -> None:
        for name in ("demo-log.md", "demo-log-recovered.md"):
            self.write(self.checkout / "docs/plans" / name, "alpha-unit merged phase 2\n")
        self.write(self.doc, self.doc.read_text() + "a tracked edit\n")
        self.assert_refused(self.rename("apply"), "showrunner checkout must be clean")
        _ = self.git("checkout", "--", str(self.doc))
        self.assert_ok(self.rename("apply"))
        self.assertEqual(self.git("ls-files", "--others", "--exclude-standard").split(),
                         ["docs/plans/demo-log-recovered.md", "docs/plans/demo-log.md"])
        self.assertNotIn("demo-log", self.git("show", "--name-only", "--format=", "HEAD"))
        self.assertEqual((self.checkout / "docs/plans/demo-log.md").read_text(), "alpha-unit merged phase 2\n")

    def test_a_mid_phase_rename_keeps_the_commits_and_the_uncommitted_work(self) -> None:
        # User, 2026-10-18: a unit is renamed at any time, not only between phases.
        _ = self.seed_record()
        worktree = self.rust / "claude-demo-alpha"
        self.write(worktree / "work.md", "phase work\n")
        _ = self.git("add", "work.md", cwd=worktree)
        _ = self.git("commit", "-m", "checkpoint(demo-alpha): phase 1", cwd=worktree)
        checkpoint = self.git("rev-parse", "HEAD", cwd=worktree)
        self.write(worktree / "work.md", "phase work\nmore, not committed\n")
        self.write(worktree / "draft.md", "a new file\n")
        run = self.root / "history/runs/run-1.jsonl"
        self.write(run, run.read_text() + json.dumps(
            {"event_type": "phase_started", "phase_instance_id": "p2", "phase_id": "2"}) + "\n")

        self.assert_refused(self.rename("apply"), "gamma-unit is not running")

        new = self.rust / "claude-demo-gamma"
        self.assertEqual((new / "work.md").read_text(), "phase work\nmore, not committed\n")
        self.assertEqual((new / "draft.md").read_text(), "a new file\n")
        # The branch kept its tip: no merge went into work in progress.
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=new), checkpoint)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/demo-gamma").split()[0], checkpoint)
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "demo-alpha"), "")
        self.assertEqual(self.journal()["kept"], "phase 2 is open")
        self.assertIn("| gamma-unit | docs/plans/alpha.md |", self.doc.read_text())
        started = cast(dict[str, object], json.loads(run.read_text().splitlines()[0]))
        self.assertEqual(started["working_dir"], str(new))
        self.assertIn("Your work was in progress (phase 2 is open), so your branch kept its commits and took no "
                      + "merge: the rename commit comes with your next merge of demo.", cast(str, self.record()["renamed"]))

    def test_uncommitted_work_between_phases_also_keeps_the_tip(self) -> None:
        worktree = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=worktree)
        self.write(worktree / "draft.md", "a new file\n")
        self.assert_ok(self.rename("apply"))
        new = self.rust / "claude-demo-gamma"
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=new), tip)
        self.assertEqual((new / "draft.md").read_text(), "a new file\n")
        self.assertEqual(self.journal()["kept"], "the worktree has uncommitted work")

    def test_refuses_an_unfinished_production_rename(self) -> None:
        self.write(self.state / SLUG / "rename.json", '{"done": false}\n')
        self.assert_refused(self.rename("apply"), "finish the production rename first")
        self.assertFalse((self.state / SLUG / "rename_unit.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
