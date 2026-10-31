"""rename_checkout.move on a real linked worktree, with every state root in a temporary folder."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typing import cast, override


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import rename_checkout
from scripts.shutdown import stop_resume
from scripts.production import showrunners
import unit_move
from rename_state import RenameRefused


class MoveTest(unittest.TestCase):
    root: Path = Path()
    old: Path = Path()
    new: Path = Path()

    def git(self, where: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(where), "-c", "user.name=t", "-c", "user.email=t@t",
                               "-c", "commit.gpgsign=false", *arguments],
                              capture_output=True, text=True, check=True).stdout.strip()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        home = self.root / "home"
        (home / ".claude/projects").mkdir(parents=True)
        main = self.root / "main"
        main.mkdir()
        origin = self.root / "origin.git"
        _ = self.git(self.root, "init", "-q", "--bare", str(origin))
        _ = self.git(main, "init", "-q", "-b", "trunk")
        _ = self.git(main, "commit", "-q", "--allow-empty", "-m", "base")
        _ = self.git(main, "remote", "add", "origin", str(origin))
        self.old, self.new = self.root / "claude-old-trunk", self.root / "claude-demo-trunk"
        _ = self.git(main, "worktree", "add", "-q", "-b", "work", str(self.old))
        doc = self.old / "docs/demo-production.md"
        doc.parent.mkdir(parents=True)
        _ = doc.write_text(f"# demo\n\n## Production Context\n\n- **Formerly:** `old`\n- **Merge branch:** `work`\n"
                           + f"- **Showrunner checkout:** `{self.old}`\n- **Log:** `docs/demo-log.md`\n"
                           + "\n## Units\n\n| Unit | Plan |\n| --- | --- |\n", encoding="utf-8")
        _ = self.git(self.old, "add", "docs")
        _ = self.git(self.old, "commit", "-q", "-m", "doc")
        _ = self.git(self.old, "push", "-q", "origin", "work")
        notifier = self.root / "notifier"
        (notifier / "showrunner-demo").mkdir(parents=True)
        _ = (notifier / "showrunner-demo/conf").write_text(
            f"TARGET=session:s-1\nCHECK=production_check.sh {doc}\nEVERY=60\n", encoding="utf-8")
        child = self.root / "child-production.md"
        _ = child.write_text(f"- **Parent production:** {doc}\n", encoding="utf-8")
        panes = home / ".local/state/herdr-panes"
        panes.mkdir(parents=True)
        _ = (panes / "s.json").write_text(json.dumps({"p1": {"cwd": str(self.old), "name": "demo"}}), encoding="utf-8")
        project = home / ".claude/projects" / unit_move.project_folder(str(self.old))
        project.mkdir()
        _ = (home / ".claude.json").write_text(json.dumps({"projects": {str(self.old): {"trusted": True}}}), encoding="utf-8")
        for patch in (mock.patch.dict(os.environ, {"HOME": str(home), "XDG_STATE_HOME": str(home / ".local/state")}),
                      mock.patch.object(Path, "home", return_value=home),
                      mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier),
                      mock.patch.object(showrunners, "timer_docs", side_effect=lambda: {"demo": self.doc(), "child": child}),
                      mock.patch.object(unit_move, "processes_inside", return_value=[]),
                      mock.patch("time.sleep"),
                      mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "user.name",
                                                   "GIT_CONFIG_VALUE_0": "t", "GIT_CONFIG_KEY_1": "user.email",
                                                   "GIT_CONFIG_VALUE_1": "t@t", "GIT_CONFIG_KEY_2": "commit.gpgsign",
                                                   "GIT_CONFIG_VALUE_2": "false"})):
            _ = patch.start()
            self.addCleanup(patch.stop)

    def doc(self) -> Path:
        return (self.new if (self.new / ".git").exists() else self.old) / "docs/demo-production.md"

    def test_move_carries_git_claude_timers_children_and_panes_and_repeats(self) -> None:
        for _ in range(2):
            rename_checkout.move(self.old, self.new)
        self.assertFalse(self.old.exists())
        self.assertIn(str(self.new), self.git(self.new, "worktree", "list"))
        home = Path.home()
        self.assertTrue((home / ".claude/projects" / unit_move.project_folder(str(self.new))).is_dir())
        trust = cast(dict[str, dict[str, object]], json.loads((home / ".claude.json").read_text()))
        self.assertIn(str(self.new), trust["projects"])
        conf = (showrunners.NOTIFIER_STATE_DIR / "showrunner-demo/conf").read_text()
        self.assertIn(f"{self.new}/docs/demo-production.md", conf)
        self.assertIn(f"{self.new}/docs/demo-production.md", (self.root / "child-production.md").read_text())
        panes = cast(dict[str, dict[str, str]], json.loads((home / ".local/state/herdr-panes/s.json").read_text()))
        self.assertEqual(panes["p1"]["cwd"], str(self.new))
        self.assertIn(f"- **Showrunner checkout:** `{self.new}`", self.git(self.new, "show", "HEAD:docs/demo-production.md"))
        self.assertEqual(self.git(self.new, "status", "--porcelain"), "")

    def test_checkout_stops_the_showrunner_with_the_move_and_a_rerun_pushes_the_doc_commit(self) -> None:
        def stop(identity: str) -> stop_resume.Stopped:
            self.assertEqual(identity, "s-1")
            self.assertTrue(self.old.exists())
            return stop_resume.Stopped(identity, 1)

        def resume(identity: str, *, why: str) -> stop_resume.Resumed:
            self.assertEqual(identity, "s-1")
            self.assertEqual(why, "showrunner checkout moved")
            self.assertFalse(self.old.exists())
            self.assertTrue(self.new.exists())
            return stop_resume.Resumed(identity, "w1:p1", "claude2")

        with (mock.patch.object(stop_resume, "stop", side_effect=stop) as stopped,
              mock.patch.object(stop_resume, "resume", side_effect=resume) as resumed,
              mock.patch.object(rename_checkout, "_session_records", return_value=[])):
            rename_checkout.checkout("demo")
        stopped.assert_called_once_with("s-1")
        resumed.assert_called_once_with("s-1", why="showrunner checkout moved")
        rename_checkout.checkout("demo")
        self.assertEqual(self.git(self.new, "rev-parse", "HEAD"), self.git(self.new, "rev-parse", "origin/work"))

    def test_checkout_ignores_its_coordinator_but_still_resumes_after_the_move(self) -> None:
        with (mock.patch.object(unit_move, "processes_inside", return_value=[os.getpid()]),
              mock.patch.object(rename_checkout, "_session_records", return_value=[]),
              mock.patch.object(stop_resume, "stop", return_value=stop_resume.Stopped("s-1", 1)),
              mock.patch.object(stop_resume, "resume", return_value=stop_resume.Resumed("s-1", "p1", "claude2")) as resumed):
            rename_checkout.checkout("demo")
        self.assertFalse(self.old.exists())
        self.assertTrue(self.new.exists())
        resumed.assert_called_once_with("s-1", why="showrunner checkout moved")

    def test_checkout_refuses_when_the_shutdown_record_lacks_the_move(self) -> None:
        with (mock.patch.object(stop_resume, "stop") as stop,
              mock.patch.object(rename_checkout, "_session_records", return_value=[{"session": {"session_id": "s-1"}}])):
            with self.assertRaisesRegex(RenameRefused, "already has a shutdown in progress"):
                rename_checkout.checkout("demo")
        stop.assert_not_called()

    def test_a_refusal_before_the_git_move_keeps_the_old_checkout_whole(self) -> None:
        with mock.patch.object(unit_move, "processes_inside", return_value=[4242]):
            with self.assertRaisesRegex(RenameRefused, "processes still work"):
                rename_checkout.move(self.old, self.new)
        self.assertTrue((self.old / ".git").exists())
        self.assertFalse(self.new.exists())


if __name__ == "__main__":
    _ = unittest.main()
