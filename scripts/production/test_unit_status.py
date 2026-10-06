#!/usr/bin/env python3
"""Tests for unit status tick health with an isolated active run."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("unit_status.sh")
SESSION_ID = "test-session"


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


class UnitStatusTests(unittest.TestCase):
    def run_status(
        self, marker: str | None, health_text: str, health_exit: int, *,
        processes: str = "100 1 tmux pane\n200 100 zsh\n12345 200 claude --remote-control stalls\n",
    ) -> tuple[str, str | None]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "scripts" / "production" / "unit_status.sh"
            script.parent.mkdir(parents=True)
            _ = shutil.copy2(SCRIPT, script)
            _write_executable(
                root / "scripts" / "lib" / "py",
                '#!/bin/sh\nexec python3 "$@"\n',
            )
            _ = (root / "scripts" / "message").mkdir(parents=True)
            _ = (root / "scripts" / "message" / "sessions.py").write_text(
                f'import sys\nprint("{SESSION_ID}" if sys.argv[-1] == "12345" else "")\n', encoding="utf-8"
            )
            _write_executable(
                root / "scripts" / "message" / "notifier.sh",
                """print -r -- "$*" >> "$NOTIFIER_CALL_LOG"
print -r -- "$NOTIFIER_HEALTH_TEXT"
exit "$NOTIFIER_HEALTH_EXIT"
""",
            )
            _ = (root / "scripts" / "hooks").mkdir(parents=True)
            _ = (root / "scripts" / "hooks" / "delegate_run.py").write_text(
                "raise SystemExit(1)\n", encoding="utf-8"
            )
            bin_dir = root / "bin"
            process_file = root / "processes.txt"
            _ = process_file.write_text(processes, encoding="utf-8")
            _write_executable(
                bin_dir / "tmux",
                """#!/bin/sh
case "$1" in
  has-session) exit 0 ;;
  display-message) printf '100\\n' ;;
  capture-pane) printf '%s\\n' '— holding: waiting on x' ;;
  *) exit 2 ;;
esac
""",
            )
            _write_executable(bin_dir / "ps", '#!/bin/sh\ncat "$TEST_PROCESS_FILE"\n')
            _write_executable(bin_dir / "pgrep", "#!/bin/sh\nexit 2\n")
            active_dir = root / "active"
            active_dir.mkdir()
            if marker is not None:
                _ = (active_dir / SESSION_ID).write_text(marker, encoding="utf-8")
            call_log = root / "notifier_calls"
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(root),
                    "PATH": f"{bin_dir}:{environment.get('PATH', '')}",
                    "PLAN_DELEGATE_ACTIVE_DIR": str(active_dir),
                    "NOTIFIER_STATE_DIR": str(root / "notifier"),
                    "NOTIFIER_NOW_EPOCH": "20100",
                    "NOTIFIER_CALL_LOG": str(call_log),
                    "NOTIFIER_HEALTH_TEXT": health_text,
                    "NOTIFIER_HEALTH_EXIT": str(health_exit),
                    "TEST_PROCESS_FILE": str(process_file),
                }
            )
            zsh = shutil.which("zsh")
            if zsh is None:
                raise RuntimeError("zsh is required for unit status tests")
            result = subprocess.run(
                [zsh, str(script), str(root / "status"), "America/Los_Angeles", "hook"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            calls = call_log.read_text(encoding="utf-8") if call_log.exists() else None
            return result.stdout, calls

    def test_idle_run_reports_failed_tick_health(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "failing: no instance", 1)
        self.assertIn("TICKS FAILING (no instance)", output)
        self.assertEqual(calls, "health delegate-test-run\n")

    def test_healthy_run_has_no_tick_warning(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0)
        self.assertNotIn("TICKS FAILING", output)
        self.assertEqual(calls, "health delegate-test-run\n")

    def test_missing_active_marker_skips_health(self) -> None:
        output, calls = self.run_status(None, "failing: no instance", 1)
        self.assertNotIn("TICKS FAILING", output)
        self.assertIsNone(calls)

    def test_empty_active_marker_skips_health(self) -> None:
        output, calls = self.run_status("", "failing: no instance", 1)
        self.assertNotIn("TICKS FAILING", output)
        self.assertIsNone(calls)

    def test_claude_is_found_under_the_pane_even_with_a_different_remote_name(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0)
        self.assertNotIn("CLAUDE NOT RUNNING", output)
        self.assertEqual(calls, "health delegate-test-run\n")

    def test_no_claude_descendant_reports_not_running(self) -> None:
        processes = "100 1 tmux pane\n200 100 zsh\n12345 1 claude --remote-control stalls\n"
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0, processes=processes)
        self.assertIn("CLAUDE NOT RUNNING", output)
        self.assertIsNone(calls)
