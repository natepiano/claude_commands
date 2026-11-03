#!/usr/bin/env python3
"""Tests for remove_run_scratch.py on a fabricated run folder."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.direct import remove_run_scratch, workers
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SCRIPT = Path(__file__).parent / "remove_run_scratch.py"


class RemoveRunScratchTests(unittest.TestCase):
    def make_run(self) -> tuple[Path, Path]:
        """A run folder with copies at several depths, and a folder outside it."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        run = root / "run"
        (run / "verify_cache").mkdir(parents=True)
        _ = (run / "verify_cache" / "pass").write_text("ok\n", encoding="utf-8")
        _ = (run / "workers").write_text("id\tname\n", encoding="utf-8")
        _ = (run / "heartbeat.log").write_text("beat\n", encoding="utf-8")
        _ = (run / "board.json").write_text("{}", encoding="utf-8")
        # A gate copy: a clone with its own .git folder and a target inside it.
        (run / "phases" / "p4" / "gate" / ".git").mkdir(parents=True)
        (run / "phases" / "p4" / "gate" / "target").mkdir()
        _ = (run / "phases" / "p4" / "gate" / "target" / "CACHEDIR.TAG").write_text("tag\n", encoding="utf-8")
        _ = (run / "phases" / "p4" / "review.diff").write_text("diff\n", encoding="utf-8")
        # A worktree checkout: .git is a file.
        (run / "scratch" / "export").mkdir(parents=True)
        _ = (run / "scratch" / "export" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        # A finished cargo target with no tag.
        (run / "p26_before" / "debug").mkdir(parents=True)
        _ = (run / "p26_before" / ".rustc_info.json").write_text("{}", encoding="utf-8")
        # A link to a checkout outside the run folder.
        outside = root / "outside"
        (outside / ".git").mkdir(parents=True)
        (run / "scratch" / "linked").symlink_to(outside)
        return run, outside

    def test_cleanup_keeps_the_checkout_holding_a_paused_workers_live_command(self) -> None:
        run, _ = self.make_run()
        checkout = run / "phases" / "p4" / "gate"
        command_cwd = checkout / "target"
        _ = (run / "progress_history_state.json").write_text(json.dumps({"production": "demo", "unit": "builders"}))
        records = run / "mesh_runs"
        records.mkdir()
        record = records / "verify.json"
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                                  "mesh_runs.py", "supervise", str(record)], stdin=subprocess.PIPE)
        self.addCleanup(lambda: child.communicate(timeout=TEST_HANG_GUARD_SECS))
        _ = record.write_text(json.dumps({"status": "running", "pid": child.pid, "worker": "w7", "cwd": str(command_cwd)}))
        with mock.patch.dict(os.environ, {"UNIT_WORKERS_STATE": str(run.parent / "unit-workers")}):
            store = workers.UnitWorkerLedger(workers.ledger_path("demo", "builders"))
            with store.locked() as ledger:
                ledger.workers["w7"] = workers.UnitWorker("w7", "impl", "codex", "sol", "high", {},
                    workers.CodexThread("kept-thread", str(run.parent / "codex")), workers.Paused(),
                    [workers.WorkerEvent("opened", "fixture", str(run), 1_793_188_800.0, {})])
            result = subprocess.run([sys.executable, str(SCRIPT), str(run)], capture_output=True,
                                    text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(command_cwd.is_dir(), result.stdout)
            self.assertFalse((run / "scratch" / "export").exists())
            self.assertTrue(record.exists())
            _ = record.write_text(json.dumps({"status": "finished", "pid": os.getpid(), "worker": "w7", "cwd": str(command_cwd)}))
            result = subprocess.run([sys.executable, str(SCRIPT), str(run)], capture_output=True,
                                    text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(checkout.exists())

    def test_old_gate_copies_beside_a_protected_branch_are_still_removed(self) -> None:
        run, _ = self.make_run()
        protected = run / "phases" / "p5" / "gate"
        (protected / ".git").mkdir(parents=True)
        cwd = protected / "build"
        cwd.mkdir()
        old_target = run / "phases" / "p3" / "p3_before"
        old_target.mkdir(parents=True)
        _ = (old_target / ".rustc_info.json").write_text("{}")
        with (mock.patch.object(workers, "protected_working_directories", return_value=(cwd,)),
              mock.patch.object(sys, "argv", [str(SCRIPT), str(run)])):
            self.assertEqual(remove_run_scratch.main(), 0)
        self.assertTrue(cwd.is_dir())
        self.assertFalse((run / "phases" / "p4" / "gate").exists())
        self.assertFalse(old_target.exists())
        self.assertTrue((run / "phases" / "p4" / "review.diff").is_file())

    def test_checkouts_and_targets_go_at_any_depth_and_the_rest_stays(self) -> None:
        run, outside = self.make_run()
        found = remove_run_scratch.scratch_folders(run)
        self.assertEqual(
            sorted(found),
            sorted([run / "verify_cache", run / "phases" / "p4" / "gate", run / "scratch" / "export",
                    run / "p26_before"]),
        )

        result = subprocess.run([sys.executable, str(SCRIPT), str(run)], check=False, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"removed 4 scratch folders from {run}", result.stdout)
        for gone in found:
            self.assertFalse(gone.exists(), gone)
        for kept in ("workers", "heartbeat.log", "board.json", "phases/p4/review.diff"):
            self.assertTrue((run / kept).is_file(), kept)
        self.assertTrue((run / "scratch" / "linked").is_symlink())
        self.assertTrue((outside / ".git").is_dir())

    def test_the_run_folder_itself_stays_when_it_is_a_checkout(self) -> None:
        run, _ = self.make_run()
        (run / ".git").mkdir()
        self.assertNotIn(run, remove_run_scratch.scratch_folders(run))
        self.assertIn(run / "p26_before", remove_run_scratch.scratch_folders(run))

    def test_a_linked_run_folder_and_a_missing_one_are_left_alone(self) -> None:
        run, _ = self.make_run()
        linked = run.parent / "linked-run"
        linked.symlink_to(run)
        self.assertEqual(remove_run_scratch.scratch_folders(linked), [])
        self.assertEqual(remove_run_scratch.scratch_folders(run.parent / "missing"), [])

        result = subprocess.run([sys.executable, str(SCRIPT), str(linked)], check=False, capture_output=True, text=True)

        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertTrue((run / "verify_cache").is_dir())

    def worktree_run(self) -> tuple[Path, Path, Path]:
        """A source repo, and a run folder holding a linked worktree of its branch copy."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        source, run = root / "source", root / "run"
        def git(*arguments: str) -> None:
            _ = subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                               check=True, capture_output=True)
        source.mkdir()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "start")
        git("branch", "copy")
        run.mkdir()
        git("worktree", "add", "-q", str(run / "phases" / "p4" / "gate"), "copy")
        return source, run, run / "phases" / "p4" / "gate"

    def worktrees(self, source: Path) -> list[str]:
        listing = subprocess.run(["git", "-C", str(source), "worktree", "list", "--porcelain"],
                                 check=True, capture_output=True, text=True).stdout
        return [line.removeprefix("worktree ") for line in listing.splitlines() if line.startswith("worktree ")]

    def test_a_worktree_copy_leaves_no_entry_in_its_source_repo(self) -> None:
        # A stale entry keeps the branch "already checked out", so the next worktree add of it refuses.
        source, run, gate = self.worktree_run()

        result = subprocess.run([sys.executable, str(SCRIPT), str(run)], check=False, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(gate.exists())
        self.assertEqual(self.worktrees(source), [str(source)])
        _ = subprocess.run(["git", "-C", str(source), "worktree", "add", "-q", str(gate), "copy"],
                           check=True, capture_output=True)

    def test_a_worktree_the_source_cannot_remove_is_deleted_and_the_source_pruned(self) -> None:
        source, _, gate = self.worktree_run()
        real = remove_run_scratch.git
        def refuse_remove(common: Path, *arguments: str) -> bool:
            return False if arguments[0] == "remove" else real(common, *arguments)

        with mock.patch.object(remove_run_scratch, "git", side_effect=refuse_remove) as git:
            remove_run_scratch.remove(gate)

        self.assertEqual([call.args[1] for call in git.call_args_list], ["remove", "prune"])
        self.assertFalse(gate.exists())
        self.assertEqual(self.worktrees(source), [str(source)])

    def test_a_worktree_whose_source_is_gone_is_still_deleted(self) -> None:
        source, run, gate = self.worktree_run()
        _ = subprocess.run(["rm", "-rf", str(source)], check=True)
        self.assertIsNone(remove_run_scratch.worktree_source(gate))

        result = subprocess.run([sys.executable, str(SCRIPT), str(run)], check=False, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(gate.exists())


if __name__ == "__main__":
    _ = unittest.main()
