#!/usr/bin/env python3
"""Tests for remove_run_scratch.py on a fabricated run folder."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.direct import remove_run_scratch

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


if __name__ == "__main__":
    _ = unittest.main()
