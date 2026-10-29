"""Tests for git_filters_retire: a retired clean filter leaves no stale index entry and no driver."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final, override

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

TABLE = Path(__file__).with_name("git_filters_table.sh")
BASH = shutil.which("bash") or "/bin/bash"
DEFAULTS = "[direct.codex]\nimpl=gpt-a:xhigh\n"
LOCAL = "[direct.codex]\nimpl=gpt-b:high   # this machine's row, longer than the default\n"
# The clean filter the repo used to run on config/agents.conf: it pinned the index blob.
PIN = """#!/usr/bin/env bash
git rev-parse --verify --quiet :config/agents.conf >/dev/null || exec cat
cat >/dev/null
exec git cat-file blob :config/agents.conf
"""


@final
class RetireTests(unittest.TestCase):
    root: Path = Path()
    conf: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.conf = self.root / "config" / "agents.conf"
        _ = self.git("init", "-q")
        pin = self.root / "pin.sh"
        _ = pin.write_text(PIN)
        pin.chmod(0o755)
        _ = self.git("config", "filter.claude-agents-conf.clean", str(pin))
        _ = self.git("config", "filter.claude-agents-conf.smudge", "cat")
        _ = self.git("config", "filter.claude-agents-conf.required", "true")
        _ = (self.root / ".gitattributes").write_text("config/agents.conf filter=claude-agents-conf\n")
        self.conf.parent.mkdir()
        _ = self.conf.write_text(DEFAULTS)
        _ = self.git("add", ".gitattributes", "config/agents.conf")
        _ = self.git("commit", "-q", "-m", "defaults")
        # The local rows the filter hid. Adding them stages the pinned blob again and records
        # their stat data in the index.
        self.write_old(self.conf, LOCAL)
        _ = self.git("add", "config/agents.conf")
        self.assertEqual(self.status(), "")
        # The commit that retires the filter.
        _ = (self.root / ".gitattributes").write_text("")
        _ = self.git("commit", "-q", "-am", "retire the filter")

    def git(self, *arguments: str) -> str:
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                       "GIT_COMMITTER_EMAIL": "t@t"}
        done = subprocess.run(["git", "-C", str(self.root), *arguments], capture_output=True, text=True,
                              env=environment, check=True, timeout=TEST_HANG_GUARD_SECS)
        return done.stdout

    def write_old(self, path: Path, text: str) -> None:
        """Write `text` with an mtime well before the index, so git never treats the entry as racy."""
        _ = path.write_text(text)
        os.utime(path, (path.stat().st_atime - 100, path.stat().st_mtime - 100))

    def status(self) -> str:
        return self.git("status", "--porcelain", "--", "config/agents.conf").strip()

    def retire(self) -> str:
        done = subprocess.run([BASH, "-c", 'source "$0" && git_filters_retire "$1"', str(TABLE), str(self.root)],
                              capture_output=True, text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                                                                  "GIT_CONFIG_NOSYSTEM": "1"},
                              check=True, timeout=TEST_HANG_GUARD_SECS)
        return done.stdout

    def test_local_rows_the_filter_hid_show_as_modified_and_checkout_resets_them(self) -> None:
        self.assertEqual(self.status(), "")
        self.assertIn("removed the retired filter.claude-agents-conf", self.retire())
        self.assertEqual(self.status(), "M config/agents.conf")
        self.assertNotIn("filter", (self.root / ".git" / "config").read_text())
        _ = self.git("checkout", "--", "config/agents.conf")
        self.assertEqual(self.conf.read_text(), DEFAULTS)
        self.assertEqual(self.status(), "")

    def test_a_file_reset_to_the_commit_reads_clean(self) -> None:
        reset = self.root / "reset.tmp"
        self.write_old(reset, DEFAULTS)
        _ = reset.replace(self.conf)
        self.assertEqual(self.status(), "M config/agents.conf")
        self.assertEqual(self.git("diff", "--", "config/agents.conf"), "")
        _ = self.retire()
        self.assertEqual(self.status(), "")
        self.assertEqual(self.retire(), "")


if __name__ == "__main__":
    _ = unittest.main()
