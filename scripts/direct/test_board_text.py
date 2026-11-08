"""The board's text protocol preserves Unicode without admitting extra records."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


BOARD = Path(__file__).resolve().with_name("board.sh")


class BoardTextTests(unittest.TestCase):
    directory: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in ("UNIT_RUN_DIR", "PLAN_DELEGATE_BOARD_DIR")}
        self.environment.update({"UNIT_SYNC_WRITES": "0", "UNIT_NOW_EPOCH": "1793232000"})

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(BOARD), *arguments], env=self.environment, cwd=self.directory,
                              capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)

    def test_printable_unicode_survives_while_control_characters_cannot_forge_a_record(self) -> None:
        message = "production · unité · 设计 reviewer 🚀\nnext\rline\ttab\x1b\x07\x7f"
        posted = self.command("post", str(self.directory), "w7", "status", message)
        self.assertEqual(posted.returncode, 0, posted.stdout + posted.stderr)
        lines = (self.directory / "board.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0].split("] status: ", 1)[1],
                         "production · unité · 设计 reviewer 🚀 next line tab")
        read = self.command("read", str(self.directory), "--from", "w7")
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        self.assertIn(lines[0], read.stdout)

    def test_the_message_limit_counts_characters_without_splitting_utf8(self) -> None:
        message = "界🚀é·" * 300
        posted = self.command("post", str(self.directory), "w7", "status", message)
        self.assertEqual(posted.returncode, 0, posted.stdout + posted.stderr)
        line = (self.directory / "board.log").read_bytes().decode("utf-8", errors="strict")
        body = line.rstrip("\n").split("] status: ", 1)[1]
        self.assertEqual(len(body), 900)
        self.assertEqual(body, message[:900])
        self.assertEqual(line.count("\n"), 1)

    def test_post_read_and_role_use_the_run_environment_without_a_folder_argument(self) -> None:
        self.environment["UNIT_RUN_DIR"] = str(self.directory)
        posted = self.command("post", "w7", "status", "phase 16 tests")
        self.assertEqual(posted.returncode, 0, posted.stdout + posted.stderr)
        line = (self.directory / "board.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(line), 1)
        self.assertTrue(line[0].endswith("[w7] status: phase 16 tests"))
        role = self.command("role", "w7", "test", "test the current run")
        self.assertEqual(role.returncode, 0, role.stdout + role.stderr)
        read = self.command("read", "--from", "w7")
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        self.assertIn(line[0], read.stdout)
        self.assertIn("[w7] handoff: role=test test the current run", read.stdout)

    def test_folder_free_commands_without_a_run_exit_two_with_one_error_and_no_writes(self) -> None:
        before = sorted(self.directory.rglob("*"))
        for arguments in (("post", "w7", "status", "phase 16 tests"),
                          ("read", "--from", "w7"),
                          ("role", "w7", "test", "test the current run")):
            with self.subTest(command=arguments[0]):
                result = self.command(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertEqual(sorted(self.directory.rglob("*")), before)

    def test_folder_argument_selects_the_named_run_over_the_environment(self) -> None:
        self.environment["UNIT_RUN_DIR"] = str(self.directory / "other-run")
        posted = self.command("post", str(self.directory), "w7", "status", "named run")
        self.assertEqual(posted.returncode, 0, posted.stdout + posted.stderr)
        line = (self.directory / "board.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(line), 1)
        self.assertTrue(line[0].endswith("[w7] status: named run"))
        self.assertFalse((self.directory / "other-run").exists())
