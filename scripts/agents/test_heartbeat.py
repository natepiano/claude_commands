"""Tests for heartbeat log bounds and watcher shutdown."""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast

HEARTBEAT = Path(__file__).resolve().parent / "heartbeat.sh"
WATCHER = HEARTBEAT.with_name("heartbeat_watch.sh")
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


class HeartbeatWatcherTests(unittest.TestCase):
    def test_stop_signals_close_the_output_pipes_and_reap_the_sleep(self) -> None:
        sleep = shutil.which("sleep")
        assert sleep is not None
        for stop_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=stop_signal), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                # Publish the child's pid before exec so the signal is sent during
                # the interval, without guessing how long startup takes.
                sleep_probe = root / "sleep"
                _ = sleep_probe.write_text(
                    '#!/usr/bin/env bash\nprintf "%s\\n" "$$"\nexec "$REAL_SLEEP" "$@"\n',
                    encoding="utf-8",
                )
                sleep_probe.chmod(0o755)
                environment = dict(os.environ, REAL_SLEEP=sleep)
                environment["PATH"] = str(root) + os.pathsep + environment["PATH"]
                watcher: subprocess.Popen[str] = subprocess.Popen(
                    ["bash", str(WATCHER), str(root / "heartbeat.log"), "fixture",
                     str(os.getpid()), str(root / "agent.log"), "30"],
                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True,
                )
                try:
                    assert watcher.stdout is not None
                    self.assertTrue(select.select([watcher.stdout], [], [], 5)[0],
                                    "the watcher's sleep child never started")
                    sleep_pid = int(cast(str, watcher.stdout.readline()))
                    started = time.monotonic()
                    watcher.send_signal(stop_signal)
                    output, errors = watcher.communicate(timeout=5)
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertEqual(watcher.returncode, 0, output + errors)
                    with self.assertRaises(ProcessLookupError,
                                           msg="the watcher's sleep child survived shutdown"):
                        os.kill(sleep_pid, 0)
                finally:
                    # On a regression, kill only this disposable process group,
                    # including a child that still owns the output pipes.
                    try:
                        os.killpg(watcher.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _ = watcher.communicate(timeout=5)


if __name__ == "__main__":
    _ = unittest.main()
