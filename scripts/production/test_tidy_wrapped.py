#!/usr/bin/env python3
"""Tests for tidy_wrapped.py: only a wrapped production with no update timer loses its state files."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override

SCRIPT = Path(__file__).with_name("tidy_wrapped.py")


def production(status: str) -> str:
    return f"# Example\n\n> **Status: PRODUCTION — {status}.** One line.\n\n## Units\n"


class TidyWrappedTests(unittest.TestCase):
    root: Path = Path()
    state: Path = Path()
    notifier: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "showrunner"
        self.notifier = self.root / "notifier"
        self.notifier.mkdir()

    def write(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")
        return path

    def doc(self, slug: str, status: str) -> Path:
        return self.write(self.root / "docs" / f"{slug}-production.md", production(status))

    def state_files(self, slug: str) -> tuple[Path, ...]:
        return tuple(self.write(path, "[]\n") for path in (
            self.state / "footers-off" / slug, self.state / "review-paused" / f"{slug}.json",
            self.state / "outstanding" / f"{slug}.json", self.state / slug / "log-backups" / "log-1.md"))

    def run_tidy(self, *docs: Path) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SHOWRUNNER_STATE_DIR": str(self.state), "NOTIFIER_STATE_DIR": str(self.notifier)}
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, docs)], env=env,
                              capture_output=True, text=True, check=False)

    def test_a_wrapped_production_loses_its_own_files_and_keeps_the_others(self) -> None:
        own = self.state_files("done")
        other = self.state_files("live")
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "done: removed 4 state files\n")
        self.assertFalse(any(path.exists() for path in own))
        self.assertFalse((self.state / "done").exists())
        self.assertTrue(all(path.exists() for path in other))

    def test_a_running_production_keeps_every_file(self) -> None:
        own = self.state_files("live")
        result = self.run_tidy(self.doc("live", "running"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("kept: ", result.stderr)
        self.assertIn("live-production.md: not wrapped", result.stderr)
        self.assertTrue(all(path.exists() for path in own))

    def test_wrap_removes_its_answered_history_and_keeps_another_productions(self) -> None:
        answered = self.write(self.state / "outstanding/done.answered.jsonl", '{"text":"answered question"}\n')
        other = self.write(self.state / "outstanding/live.answered.jsonl", '{"text":"keep this answer"}\n')
        before = other.read_bytes()
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done: removed 1 state file\n", ""))
        self.assertFalse(answered.exists())
        self.assertEqual(other.read_bytes(), before)

    def test_a_wrapped_doc_whose_timer_remains_keeps_every_file(self) -> None:
        own = self.state_files("done")
        (self.notifier / "showrunner-done").mkdir()
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("done still has an update timer", result.stderr)
        self.assertTrue(all(path.exists() for path in own))

    def test_a_slug_naming_a_shared_folder_deletes_nothing(self) -> None:
        own = self.state_files("live")
        result = self.run_tidy(self.doc("outstanding", "wrapped"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("'outstanding' is not a production slug", result.stderr)
        self.assertTrue(all(path.exists() for path in own))

    def test_a_refused_doc_does_not_stop_the_next(self) -> None:
        kept = self.state_files("live")
        removed = self.state_files("done")
        result = self.run_tidy(self.doc("live", "running"), self.doc("done", "wrapped"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "done: removed 4 state files\n")
        self.assertTrue(all(path.exists() for path in kept))
        self.assertFalse(any(path.exists() for path in removed))

    def test_a_wrapped_production_with_nothing_left_says_so(self) -> None:
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done: no state files\n", ""))


if __name__ == "__main__":
    _ = unittest.main()
