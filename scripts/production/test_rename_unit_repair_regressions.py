"""Repairs reconcile physical paths, row cells and names without unrelated git work."""
from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
from typing import cast
from unittest import mock

import rename_state
import rename_unit
import unit_move
from scripts.production import staff
from scripts.production.rename_rerun_test_support import saved_fixture
from scripts.production.rename_unit_test_support import SLUG, UnitRenameFixture


class UnitRenameRepairRegressions(UnitRenameFixture):
    def canonical_alpha(self) -> Path:
        self.assert_ok(self.rename("apply", "alpha", "alpha"))
        saved = self.journal()
        saved.update(old_branch="alpha", old_worktree=saved["new_worktree"], old_session="alpha")
        self.write(self.state / SLUG / "rename_unit.json", json.dumps(saved))
        return self.home / "worktrees/alpha"

    def test_plan_and_apply_refuse_another_productions_alias_without_writes(self) -> None:
        other = staff.ensure("showrunner", name="other")
        _ = staff.ensure("unit", name="reserved_name", parent=other.id)
        before = self.snapshot()
        for action in ("plan", "apply"):
            with self.subTest(action=action):
                result = self.rename(action, "alpha", "reserved-name")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unit name already exists: reserved-name", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_name_only_verify_accepts_an_unpushed_branch(self) -> None:
        destination = self.canonical_alpha()
        _ = self.git("push", "origin", "--delete", "alpha")
        tip = self.git("rev-parse", "HEAD", cwd=destination)
        result = self.rename("verify", "alpha", "alpha")
        self.assertNotIn("origin alpha is not at the local tip", result.stderr)
        self.assert_ok(result)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination), tip)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/alpha"), "")

    def test_name_only_verify_leaves_unrelated_live_text_unswept(self) -> None:
        _ = self.canonical_alpha()
        unrelated = self.root / "state/other/live.json"
        self.write(unrelated, '{"subject": "unrelated live state"}\n')
        scratch = self.root / "scratch/showrunner_state.json"
        self.write(scratch, json.dumps({"note": str(self.root / "state")}))
        before = unrelated.read_bytes()
        self.assert_ok(self.rename("verify", "alpha", "alpha"))
        self.assertEqual(unrelated.read_bytes(), before)
        self.assertEqual(json.loads(scratch.read_text()), {"note": str(self.root / "state")})

    def test_underscore_folder_repair_reuses_transcripts_and_updates_literal_paths(self) -> None:
        old = self.home / "worktrees/mul-add"
        new = self.home / "worktrees/mul_add"
        old.parent.mkdir(parents=True)
        _ = self.git("branch", "-m", "demo-mul-add", "mul_add")
        _ = self.git("worktree", "move", str(self.rust / "claude-demo-mul-add"), str(old))
        self.write(self.doc, self.doc.read_text().replace(str(self.rust / "claude-demo-mul-add"), str(old))
                   .replace("| demo-mul-add |", "| mul_add |"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "legacy underscore folder")
        transcripts: list[Path] = []
        configs = [self.home / ".claude.json", self.home / ".claude-other/.claude.json"]
        for config in configs:
            directory = self.home / ".claude" if config == self.home / ".claude.json" else config.parent
            transcript = directory / "projects" / unit_move.project_folder(str(old)) / "session.jsonl"
            self.write(transcript, "transcript history\n")
            transcripts.append(transcript)
            self.write(config, json.dumps({"projects": {str(old): {"hasTrustDialogAccepted": True}}, "theme": "dark"}))
        run = self.root / "history/runs/mul.jsonl"
        self.write(run, json.dumps({"event_type": "run_started", "run_started_at": 2,
            "working_dir": str(old), "worktree": old.name, "branch": "mul_add"}) + "\n")
        recorder = self.root / "sessions/mul/progress_history_state.json"
        self.write(recorder, json.dumps({"history_file": str(run), "working_dir": str(old),
                                        "worktree": old.name, "branch": "mul_add"}))
        self.assert_ok(self.rename("plan", "mul_add", "mul_add"))
        self.assert_ok(self.rename("apply", "mul_add", "mul_add"))
        self.assertFalse(old.exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=new), "mul_add")
        for transcript in transcripts:
            self.assertEqual(transcript.read_text(), "transcript history\n")
        for config in configs:
            fields = cast(dict[str, object], json.loads(config.read_text()))
            projects = cast(dict[str, object], fields["projects"])
            self.assertEqual(projects[str(new)], projects[str(old)])
            self.assertEqual(fields["theme"], "dark")
        self.assertEqual(json.loads(recorder.read_text())["working_dir"], str(new))
        before = self.snapshot()
        self.assert_ok(self.rename("apply", "mul_add", "mul_add"))
        self.assertEqual(self.snapshot(), before)

    def test_unchanged_apply_clears_an_ignored_unfinished_legacy_claim(self) -> None:
        _ = self.canonical_alpha()
        saved = self.journal()
        del saved["staff_id"]
        saved.update(old="retired-unit", new="retired-next-unit", step=0, done=False, claimed=True)
        path = self.state / SLUG / "rename_unit.json"
        self.write(path, json.dumps(saved))
        before = self.snapshot()
        self.assert_ok(self.rename("plan", "alpha", "alpha"))
        self.assertEqual(self.snapshot(), before)
        self.assert_ok(self.rename("apply", "alpha", "alpha"))
        self.assertFalse(path.exists())
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(rename_state.rename_claim(SLUG), frozenset())

    def test_same_name_plan_describes_actual_changes_without_identity_arrows(self) -> None:
        planned = self.rename("plan", "alpha", "alpha")
        self.assert_ok(planned)
        self.assertNotIn("alpha-unit -> alpha-unit", planned.stdout)
        self.assertNotIn("rename unit alpha-unit to alpha-unit", planned.stdout)
        self.assertIn("Units row", planned.stdout)
        self.assertIn("demo-alpha -> alpha", planned.stdout)
        self.assertIn("10. already matches: no shutdown record to relaunch", planned.stdout)
        _ = self.canonical_alpha()
        unchanged = self.rename("plan", "alpha", "alpha")
        self.assert_ok(unchanged)
        self.assertTrue(all("already matches" in line for line in unchanged.stdout.splitlines()), unchanged.stdout)

    def test_live_same_name_plan_includes_shutdown_record_path_repairs(self) -> None:
        _ = self.mark_live("alpha-unit", "sid-alpha", name="alpha")
        before = self.snapshot()
        planned = self.rename("plan", "alpha", "alpha")
        self.assert_ok(planned)
        record_step = next(line for line in planned.stdout.splitlines() if line.startswith("9. "))
        self.assertIn("shutdown record: Claude session already matches; paths", record_step)
        self.assertIn(f"{self.rust / 'claude-demo-alpha'} -> {self.home / 'worktrees/alpha'}", record_step)
        self.assertEqual(self.snapshot(), before)

    def test_same_name_repairs_stale_row_cells_without_moving_git(self) -> None:
        destination = self.canonical_alpha()
        with saved_fixture(self.root) as restore:
            for column in ("Branch", "Worktree", "both", "existing-worktree", "interrupted-commit"):
                with self.subTest(column=column):
                    restore()
                    current = self.doc.read_text()
                    stale = current
                    other = self.home / "worktrees/beta"
                    if column == "existing-worktree":
                        _ = self.git("worktree", "add", "-b", "beta", str(other), SLUG)
                        self.write(other / "beta-work.txt", "beta's uncommitted work\n")
                    if column in ("Branch", "both", "interrupted-commit"):
                        stale = stale.replace("| alpha |", "| stale-branch |")
                    if column in ("Worktree", "both", "existing-worktree"):
                        stale = stale.replace(str(destination), str(other if column == "existing-worktree"
                                                                    else self.root / "gone-worktree"))
                    self.write(self.doc, stale)
                    _ = self.git("add", str(self.doc))
                    _ = self.git("commit", "-m", "stale row cells")
                    tip = self.git("rev-parse", "HEAD", cwd=destination)
                    branch_tips = self.git("show-ref", "--heads")
                    before = self.snapshot()
                    planned = self.rename("plan", "alpha", "alpha")
                    self.assert_ok(planned)
                    self.assertIn("Units row", planned.stdout)
                    self.assertIn("stale-branch" if column in ("Branch", "interrupted-commit") else
                                  str(other) if column == "existing-worktree" else "gone-worktree", planned.stdout)
                    self.assertEqual(self.snapshot(), before)
                    if column == "interrupted-commit":
                        checkout_tip = self.git("rev-parse", "HEAD")
                        write = cast(Callable[[Path, str], None], getattr(rename_unit, "write_text"))

                        def interrupted(path: Path, content: str) -> None:
                            write(path, content)
                            if path == self.doc:
                                raise OSError("interrupted after same-name document write")

                        with mock.patch.object(rename_unit, "write_text", side_effect=interrupted):
                            self.assert_refused(self.rename("apply", "alpha", "alpha"),
                                                "interrupted after same-name document write")
                        self.assertEqual(self.journal()["doc_commit"], "")
                        self.assertEqual(self.doc.read_text(), current)
                        self.assertEqual(self.git("rev-parse", "HEAD"), checkout_tip)
                        self.assertEqual(self.git("show", f"HEAD:{self.doc.relative_to(self.checkout)}"), stale.strip())
                    self.assert_ok(self.rename("apply", "alpha", "alpha"))
                    self.assertEqual(self.doc.read_text(), current)
                    self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination), tip)
                    self.assertEqual(self.git("show", f"HEAD:{self.doc.relative_to(self.checkout)}"), current.strip())
                    self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
                    if column == "existing-worktree":
                        self.assertEqual(self.git("branch", "--show-current", cwd=other), "beta")
                        self.assertEqual((other / "beta-work.txt").read_text(), "beta's uncommitted work\n")
                        self.assertEqual(self.git("rev-parse", "beta"),
                                         next(line.split()[0] for line in branch_tips.splitlines()
                                              if line.endswith(" refs/heads/beta")))
                    matched = self.snapshot()
                    self.assert_ok(self.rename("apply", "alpha", "alpha"))
                    self.assertEqual(self.snapshot(), matched)
