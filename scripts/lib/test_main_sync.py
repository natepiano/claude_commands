"""main_sync fast-forwards a main checkout to origin/main only when that loses nothing."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import override

from scripts.lib import main_sync


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                          check=True, capture_output=True, text=True).stdout.strip()


class MainSyncTests(unittest.TestCase):
    root: Path = Path()
    origin: Path = Path()
    writer: Path = Path()
    checkout: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(unittest.mock.patch.dict(os.environ, {main_sync.STATE_ENV: str(self.root / "state")}))
        self.origin = self.root / "origin.git"
        _ = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.writer = self.root / "writer"
        _ = subprocess.run(["git", "clone", "-q", str(self.origin), str(self.writer)], check=True, capture_output=True)
        _ = self.commit("a")
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(self.origin), str(self.checkout)], check=True, capture_output=True)

    def commit(self, name: str) -> str:
        _ = (self.writer / name).write_text(name)
        _ = git(self.writer, "add", name)
        _ = git(self.writer, "commit", "-q", "-m", name)
        _ = git(self.writer, "push", "-q", "origin", "HEAD:main")
        return git(self.writer, "rev-parse", "HEAD")

    def test_it_fast_forwards_and_leaves_untracked_files(self) -> None:
        self.assertEqual(main_sync.sync(self.checkout).state, main_sync.CURRENT)
        tip = self.commit("b")
        _ = (self.checkout / "session-state.json").write_text("{}")
        self.assertEqual(main_sync.sync(self.checkout).state, main_sync.ADVANCED)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
        self.assertTrue((self.checkout / "session-state.json").exists())

    def test_each_blocked_case_leaves_the_checkout_alone(self) -> None:
        _ = self.commit("b")
        before = git(self.checkout, "rev-parse", "HEAD")
        _ = (self.checkout / "a").write_text("local edit")
        self.assertEqual(main_sync.sync(self.checkout).state, "dirty")
        self.assertEqual((self.checkout / "a").read_text(), "local edit")
        _ = git(self.checkout, "checkout", "-q", "a")
        _ = git(self.checkout, "commit", "-q", "--allow-empty", "-m", "local")
        self.assertEqual(main_sync.sync(self.checkout).state, "diverged")
        _ = git(self.checkout, "reset", "-q", "--hard", before)
        _ = git(self.checkout, "checkout", "-q", "-b", "other")
        self.assertEqual(main_sync.sync(self.checkout).state, "not-main")
        _ = git(self.checkout, "checkout", "-q", "main")
        _ = git(self.checkout, "remote", "set-url", "origin", str(self.root / "missing.git"))
        self.assertEqual(main_sync.sync(self.checkout).state, "fetch-failed")
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), before)

    def test_a_blocked_state_is_sent_once_and_clears_after_a_sync(self) -> None:
        sent: list[str] = []

        def notify(_name: str, outcome: main_sync.Outcome) -> None:
            sent.append(outcome.state)

        with unittest.mock.patch.object(main_sync, "notify", notify):
            _ = self.commit("b")
            _ = (self.checkout / "a").write_text("local edit")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
            self.assertEqual(sent, ["dirty"])
            _ = git(self.checkout, "checkout", "-q", "a")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 0)
            self.assertFalse((self.root / "state" / main_sync.BLOCKED_FILE).exists())
            _ = (self.checkout / "b").write_text("edit again")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 0)
        self.assertEqual(sent, ["dirty"])
        log = (self.root / "state" / main_sync.SYNC_LOG).read_text().splitlines()
        self.assertEqual(len(log), 4)


if __name__ == "__main__":
    _ = unittest.main()
