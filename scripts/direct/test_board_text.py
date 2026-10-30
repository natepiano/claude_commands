"""The board's text protocol preserves Unicode without admitting extra records."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


BOARD = Path(__file__).resolve().with_name("board.sh")


class BoardTextTests(unittest.TestCase):
    def test_printable_unicode_survives_while_control_characters_cannot_forge_a_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            environment = {**os.environ, "UNIT_SYNC_WRITES": "0", "UNIT_NOW_EPOCH": "1793232000"}
            message = "production · unité · 设计 reviewer 🚀\nnext\rline\ttab\x1b\x07\x7f"
            posted = subprocess.run(["bash", str(BOARD), "post", str(run), "w7", "status", message],
                                    env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(posted.returncode, 0, posted.stdout + posted.stderr)
            lines = (run / "board.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertEqual(lines[0].split("] status: ", 1)[1],
                             "production · unité · 设计 reviewer 🚀 next line tab")
            read = subprocess.run(["bash", str(BOARD), "read", str(run), "--from", "w7"],
                                  env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
            self.assertIn(lines[0], read.stdout)

    def test_the_message_limit_counts_characters_without_splitting_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            message = "界🚀é·" * 300
            posted = subprocess.run(["bash", str(BOARD), "post", str(run), "w7", "status", message],
                                    capture_output=True, timeout=10)
            self.assertEqual(posted.returncode, 0, posted.stdout + posted.stderr)
            line = (run / "board.log").read_bytes().decode("utf-8", errors="strict")
            body = line.rstrip("\n").split("] status: ", 1)[1]
            self.assertEqual(len(body), 900)
            self.assertEqual(body, message[:900])
            self.assertEqual(line.count("\n"), 1)
