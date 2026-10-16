"""Tests for heartbeat.sh's bound on the shared heartbeat log."""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

HEARTBEAT = Path(__file__).resolve().parent / "heartbeat.sh"
MAX_BYTES = 256 * 1024
KEEP_BYTES = 128 * 1024
BEAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+ \[wrapper\] ")
HEADER = "---- 2026-10-15T09:00:00-0400 [impl:impl (codex/gpt-6.1-sol:xhigh)] ----\nwork order\nat prompt.md\n"


def beat_line(index: int) -> str:
    return f"2026-10-15T09:{index // 60 % 60:02d}:{index % 60:02d}-0400 [wrapper] impl agent running {index}s — {'x' * 80}\n"


def beat(log: Path, message: str) -> str:
    _ = subprocess.run(["bash", str(HEARTBEAT), str(log), "wrapper", message], check=True)
    return log.read_text(encoding="utf-8")


class HeartbeatCapTests(unittest.TestCase):
    def test_a_log_under_the_cap_only_gains_the_beat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "heartbeat.log"
            before = HEADER + "".join(beat_line(index) for index in range(100))
            _ = log.write_text(before, encoding="utf-8")
            text = beat(log, "newest")
        self.assertTrue(text.startswith(before))
        self.assertTrue(text.endswith("[wrapper] newest\n"))

    def test_a_log_over_the_cap_keeps_its_header_and_newest_whole_entries(self) -> None:
        beats = [beat_line(index) for index in range(MAX_BYTES // 100)]
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "heartbeat.log"
            _ = log.write_text(HEADER + "".join(beats), encoding="utf-8")
            text = beat(log, "newest")
            self.assertEqual(list(Path(directory).iterdir()), [log], "no temp file is left")
        self.assertTrue(text.startswith(HEADER), "the opening header block survives")
        self.assertLessEqual(len(text.encode()), len(HEADER) + KEEP_BYTES)
        rest = text[len(HEADER):].splitlines(keepends=True)
        self.assertTrue(all(BEAT.match(line) for line in rest), "no partial line survives the cut")
        self.assertEqual(rest[-1].split("] ", 1)[1], "newest\n")
        self.assertEqual(rest[:-1], beats[-len(rest) + 1:], "the kept beats are the newest, in order")


if __name__ == "__main__":
    _ = unittest.main()
