"""The scheduled fix launcher fails closed without platform-specific flock."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override


REPOSITORY = Path(__file__).resolve().parents[3]
FIX_TRIGGER = REPOSITORY / "scripts/fix/fix-trigger.sh"
LAUNCH_PERMISSION = REPOSITORY / "scripts/shutdown/launch_permission.py"


@final
class FixTriggerShutdownTests(unittest.TestCase):
    root = Path()
    home = Path()
    state = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        scripts = self.home / ".claude/scripts"
        (scripts / "lib").mkdir(parents=True)
        (scripts / "shutdown").mkdir()
        (scripts / "fix").mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        binary_root = self.root / "bin"
        binary_root.mkdir()

        python = scripts / "lib/py"
        _ = python.write_text(
            f"#!/bin/sh\nexec {shlex_quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        python.chmod(0o755)
        _ = shutil.copy2(LAUNCH_PERMISSION, scripts / "shutdown/launch_permission.py")

        shutdown = scripts / "shutdown/shutdown.py"
        _ = shutdown.write_text(
            f"""#!{sys.executable}
import os
from pathlib import Path
import sys

state = Path(os.environ["STUB_STATE"])
(state / "launch-blocked-ran").write_text(" ".join(sys.argv[1:]))
status = int(os.environ["STUB_STATUS"])
if status == 0:
    print("claude 2 is held by a shutdown (down since 2026-10-09 14:49 PDT)")
elif status == 3:
    print("shutdown state unreadable: malformed record", file=sys.stderr)
raise SystemExit(status)
""",
            encoding="utf-8",
        )
        shutdown.chmod(0o755)

        fix = scripts / "fix/fix.sh"
        _ = fix.write_text(
            f"""#!{sys.executable}
import fcntl
import os
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
lock_path = Path(os.environ["SHUTDOWN_STATE_DIR"]) / "launch.lock"
with lock_path.open("a+") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
(state / "fix-ran").write_text("FIX_SCHEDULED=" + os.environ.get("FIX_SCHEDULED", ""))
""",
            encoding="utf-8",
        )
        fix.chmod(0o755)

        pgrep = binary_root / "pgrep"
        _ = pgrep.write_text(
            f"""#!{sys.executable}
import os
from pathlib import Path
import sys

state = Path(os.environ["STUB_STATE"])
(state / "pgrep-ran").write_text(" ".join(sys.argv[1:]))
raise SystemExit(0 if os.environ.get("PGRP_MATCH") == "1" else 1)
""",
            encoding="utf-8",
        )
        pgrep.chmod(0o755)

        self.environment = {
            "HOME": str(self.home),
            "PATH": str(binary_root),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown-state"),
            "STUB_STATE": str(self.state),
            "STUB_STATUS": "1",
            "PGRP_MATCH": "0",
        }

    def run_trigger(
        self, status: int, *, pgrep_match: bool = False
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            **self.environment,
            "STUB_STATUS": str(status),
            "PGRP_MATCH": "1" if pgrep_match else "0",
        }
        return subprocess.run(
            ["/bin/bash", str(FIX_TRIGGER)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_allowed_run_executes_fix_with_marker_and_released_barrier(self) -> None:
        result = self.run_trigger(1)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            (self.state / "launch-blocked-ran").read_text(), "launch-blocked"
        )
        self.assertEqual((self.state / "fix-ran").read_text(), "FIX_SCHEDULED=1")
        self.assertFalse(shutil.which("flock", path=self.environment["PATH"]))

    def test_down_account_skips_fix_and_reports_reason(self) -> None:
        result = self.run_trigger(0)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout,
            "fix-trigger: skipped: claude 2 is held by a shutdown"
            + " (down since 2026-10-09 14:49 PDT)\n",
        )
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.state / "fix-ran").exists())

    def test_unreadable_state_skips_fix_with_one_prefix(self) -> None:
        result = self.run_trigger(3)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout,
            "fix-trigger: skipped: shutdown state unreadable: malformed record\n",
        )
        self.assertEqual(
            result.stdout.count("shutdown state unreadable: "), 1
        )
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.state / "fix-ran").exists())

    def test_existing_fix_process_exits_before_shutdown_check(self) -> None:
        result = self.run_trigger(3, pgrep_match=True)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertTrue((self.state / "pgrep-ran").exists())
        self.assertFalse((self.state / "launch-blocked-ran").exists())
        self.assertFalse((self.state / "fix-ran").exists())


def shlex_quote(value: str) -> str:
    """Quote the interpreter path for the tiny POSIX launcher."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    _ = unittest.main()
