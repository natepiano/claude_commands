"""main_sync fast-forwards a main checkout to origin/main only when that loses nothing."""

from __future__ import annotations

import io
import os
import shutil
import socket
import sys
import subprocess
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path
from typing import override

from scripts.lib import main_sync
from scripts.message import session_send


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
        _ = self.enterContext(redirect_stdout(io.StringIO()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(unittest.mock.patch.dict(os.environ, {main_sync.STATE_ENV: str(self.root / "state")}))
        config = self.root / "config"
        config.mkdir()
        _ = (config / "send.conf").write_text("# range: 0.001..\nmain_sync_repeat_minutes=17\n")
        binaries = self.root / "bin"
        binaries.mkdir()
        _ = (binaries / "git").symlink_to(shutil.which("git") or "/usr/bin/git")
        warm_status = binaries / "github-warm-status"
        _ = warm_status.write_text("#!/bin/sh\nexit 1\n")
        warm_status.chmod(0o755)
        self.enterContext(unittest.mock.patch.dict(os.environ, {
            "CONF_DIR": str(config), "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}))
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

        _ = (self.writer / "a").write_text("updated a")
        _ = git(self.writer, "add", "a")
        _ = git(self.writer, "rm", "b")
        tip = self.commit("c")
        _ = (self.checkout / "a").write_text("updated a")
        _ = (self.checkout / "c").write_text("c")
        before = git(self.checkout, "rev-parse", "HEAD")
        outcome = main_sync.sync(self.checkout)
        self.assertEqual(outcome.state, main_sync.ADVANCED)
        self.assertIn(f"{before[:8]}..{tip[:8]}", outcome.detail)
        self.assertIn("finished an interrupted update", outcome.detail)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
        self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((self.checkout / "a").read_text(), "updated a")
        self.assertEqual((self.checkout / "c").read_text(), "c")
        self.assertEqual((self.checkout / "session-state.json").read_text(), "{}")

        with self.subTest(case="retry after restore"):
            _ = (self.writer / "a").write_text("next a")
            _ = git(self.writer, "add", "a")
            tip = self.commit("new-file")
            _ = (self.checkout / "new-file").write_text("new-file")
            self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, main_sync.ADVANCED)
            self.assertIn("finished an interrupted update", outcome.detail)
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
            self.assertEqual((self.checkout / "a").read_text(), "next a")
            self.assertEqual((self.checkout / "new-file").read_text(), "new-file")
            self.assertEqual((self.checkout / "session-state.json").read_text(), "{}")

        with self.subTest(case="directory becomes a file"):
            (self.writer / "d").mkdir()
            _ = self.commit("d/a")
            _ = git(self.checkout, "fetch", "-q", "origin")
            _ = git(self.checkout, "merge", "--ff-only", "-q", "origin/main")
            _ = git(self.writer, "rm", "d/a")
            tip = self.commit("d")
            (self.checkout / "d/a").unlink()
            (self.checkout / "d").rmdir()
            _ = (self.checkout / "d").write_text("d")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, main_sync.ADVANCED)
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
            self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
            self.assertEqual((self.checkout / "d").read_text(), "d")
            self.assertEqual((self.checkout / "session-state.json").read_text(), "{}")

    def test_each_blocked_case_leaves_the_checkout_alone(self) -> None:
        _ = self.commit("b")
        _ = git(self.checkout, "fetch", "-q", "origin")
        _ = git(self.checkout, "merge", "--ff-only", "-q", "origin/main")
        _ = self.commit("c")
        before = git(self.checkout, "rev-parse", "HEAD")

        with self.subTest(case="clean status with a blocked untracked addition"):
            _ = (self.checkout / "c").write_text("local addition")
            self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, "dirty")
            self.assertEqual(outcome.detail, f"{self.checkout} has local changes that block the update (c)")
            self.assertEqual((self.checkout / "c").read_text(), "local addition")
            (self.checkout / "c").unlink()

        with self.subTest(case="untracked file blocks an incoming directory"):
            (self.writer / "dir").mkdir()
            _ = self.commit("dir/file")
            _ = (self.checkout / "dir").write_text("local file")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, "dirty")
            self.assertEqual(outcome.detail, f"{self.checkout} has local changes that block the update (dir)")
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), before)
            self.assertEqual((self.checkout / "dir").read_text(), "local file")
            (self.checkout / "dir").unlink()

        _ = (self.checkout / "a").write_text("local edit")
        self.assertEqual(main_sync.sync(self.checkout).state, "dirty")
        self.assertEqual((self.checkout / "a").read_text(), "local edit")
        _ = (self.writer / "a").write_text("updated a")
        _ = git(self.writer, "add", "a")
        _ = self.commit("d")
        _ = (self.checkout / "a").write_text("updated a")
        _ = (self.checkout / "b").write_text("local edit")
        index_before = git(self.checkout, "ls-files", "--stage")
        self.assertEqual(main_sync.sync(self.checkout).state, "dirty")
        self.assertEqual((self.checkout / "a").read_text(), "updated a")
        self.assertEqual((self.checkout / "b").read_text(), "local edit")
        self.assertEqual(git(self.checkout, "ls-files", "--stage"), index_before)
        _ = git(self.checkout, "checkout", "-q", "b")

        with self.subTest(case="staged edit differs from both commits"):
            _ = (self.checkout / "a").write_text("staged edit")
            _ = git(self.checkout, "add", "a")
            _ = (self.checkout / "a").write_text("updated a")
            index_before = git(self.checkout, "ls-files", "--stage")
            self.assertEqual(main_sync.sync(self.checkout).state, "dirty")
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), before)
            self.assertEqual(git(self.checkout, "ls-files", "--stage"), index_before)
            self.assertEqual(git(self.checkout, "show", ":a"), "staged edit")
            self.assertEqual((self.checkout / "a").read_text(), "updated a")
            _ = git(self.checkout, "reset", "-q", "HEAD", "--", "a")

        with self.subTest(case="two blocked files and one matching file"):
            _ = (self.checkout / "b").write_text("local edit")
            _ = (self.checkout / "c").write_text("local addition")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, "dirty")
            self.assertEqual(outcome.detail, f"{self.checkout} has local changes that block the update (b, c)")
            _ = git(self.checkout, "checkout", "-q", "b")
            (self.checkout / "c").unlink()

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

    def test_a_good_run_between_blocked_runs_writes_only_the_log(self) -> None:
        sent: list[str] = []

        def notify(_name: str, outcome: main_sync.Outcome) -> None:
            sent.append(outcome.state)

        with unittest.mock.patch.object(main_sync, "notify", notify):
            _ = self.commit("b")
            _ = (self.checkout / "a").write_text("local edit")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
            self.assertEqual(sent, ["dirty", "dirty"])
            _ = git(self.checkout, "checkout", "-q", "a")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 0)
            self.assertEqual(list((self.root / "state").iterdir()), [self.root / "state" / main_sync.SYNC_LOG])
            _ = self.commit("c")
            _ = (self.checkout / "b").write_text("edit again")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
        self.assertEqual(sent, ["dirty", "dirty", "dirty"])
        self.assertEqual(list((self.root / "state").iterdir()), [self.root / "state" / main_sync.SYNC_LOG])
        log = (self.root / "state" / main_sync.SYNC_LOG).read_text().splitlines()
        self.assertEqual(len(log), 4)

    def test_each_blocked_run_uses_the_host_state_key_and_current_config_window(self) -> None:
        with (unittest.mock.patch.object(main_sync, "sync", return_value=main_sync.Outcome("dirty", "local edit")),
              unittest.mock.patch.object(socket, "gethostname", return_value="fixture-host"),
              unittest.mock.patch.object(subprocess, "run") as send):
            for minutes in (17, 23):
                _ = (self.root / "config/send.conf").write_text(f"# range: 0.001..\nmain_sync_repeat_minutes={minutes}\n")
                self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
                send.assert_called_with(
                    [sys.executable, str(session_send.SEND_SCRIPT), "--to", "natedev", "--from", "main-sync",
                     "--text", "main sync on fixture-host: local edit. Commands there stay old until this is "
                     + "cleared; the job retries every 15 minutes.", "--key", "main-sync-fixture-host-dirty",
                     "--repeat-minutes", str(minutes)], check=False, capture_output=True)
            self.assertEqual(send.call_count, 2)

    def test_fetch_failure_advice_uses_the_credential_status(self) -> None:
        _ = git(self.checkout, "remote", "set-url", "origin", str(self.root / "missing.git"))
        status = self.root / "bin/github-warm-status"
        for code in (0, 1):
            with self.subTest(status=code):
                _ = status.write_text(f"#!/bin/sh\nexit {code}\n")
                outcome = main_sync.sync(self.checkout)
                self.assertEqual(outcome.state, "fetch-failed")
                if code == 0:
                    self.assertIn("GitHub credentials are warm", outcome.detail)
                    self.assertIn("GitHub refused", outcome.detail)
                    self.assertIn("likely a GitHub incident", outcome.detail)
                    self.assertIn("the job retries", outcome.detail)
                    self.assertNotIn("github-warmup", outcome.detail)
                else:
                    self.assertIn("run github-warmup at a terminal on", outcome.detail)
                    self.assertNotIn("credentials are warm", outcome.detail)
        status.unlink()
        with unittest.mock.patch.dict(os.environ, {"PATH": str(self.root / "bin")}):
            outcome = main_sync.sync(self.checkout)
        self.assertEqual(outcome.state, "fetch-failed")
        self.assertIn("run github-warmup at a terminal on", outcome.detail)

    def test_a_credential_probe_timeout_uses_the_cold_advice(self) -> None:
        results = [subprocess.CompletedProcess(["git"], 0, "main\n", ""),
                   subprocess.CompletedProcess(["git"], 0, str(self.checkout / ".git"), ""),
                   subprocess.CompletedProcess(["git"], 1, "", "fetch refused")]
        with (unittest.mock.patch.object(main_sync, "_git", side_effect=results),
              unittest.mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(
                  ["github-warm-status"], 5)) as probe):
            outcome = main_sync.sync(self.checkout)
        probe.assert_called_once_with(["github-warm-status"], capture_output=True, check=False, timeout=5)
        self.assertEqual(outcome.state, "fetch-failed")
        self.assertIn("run github-warmup at a terminal on", outcome.detail)
        self.assertNotIn("credentials are warm", outcome.detail)


if __name__ == "__main__":
    _ = unittest.main()
