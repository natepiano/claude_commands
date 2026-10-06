#!/usr/bin/env python3
"""Behavior of the shell command run inside each build step's systemd scope."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


INVOKE = Path(__file__).with_name("invoke.sh")
OOM_SCORE_ADJ = "/proc/self/oom_score_adj"


def scope_command() -> str:
    source = INVOKE.read_text(encoding="utf-8")
    match = re.search(r"^BUILDLOG_SCOPE_SH='([^'\n]*)'$", source, re.MULTILINE)
    if match is None:
        raise AssertionError("BUILDLOG_SCOPE_SH is missing or no longer a single-quoted string")
    return match.group(1)


def require_score_below_500(test: unittest.TestCase) -> None:
    if not Path(OOM_SCORE_ADJ).exists():
        test.skipTest(f"{OOM_SCORE_ADJ} is unavailable")
    if int(Path(OOM_SCORE_ADJ).read_text(encoding="utf-8")) >= 500:
        test.skipTest("this process already runs at oom_score_adj 500 or more, so a 500 in the step proves nothing")


class BuildScopeTests(unittest.TestCase):
    def run_scope(
        self, step: list[str], command: str | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "peak"
            return subprocess.run(
                [
                    "/bin/sh",
                    "-c",
                    'exec 3>&2; exec /bin/sh -c "$@"',
                    "scope-test",
                    command if command is not None else scope_command(),
                    str(marker),
                    *step,
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

    def test_step_has_oom_score_adj_500(self) -> None:
        require_score_below_500(self)
        result = self.run_scope(["sh", "-c", f"cat {OOM_SCORE_ADJ}"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "500\n")

    def test_child_of_step_inherits_oom_score_adj_500(self) -> None:
        require_score_below_500(self)
        result = self.run_scope(["sh", "-c", f"sh -c 'cat {OOM_SCORE_ADJ}'"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "500\n")

    def test_step_exit_status_passes_through(self) -> None:
        result = self.run_scope(["sh", "-c", "exit 7"])
        self.assertEqual(result.returncode, 7)

    def test_failed_score_write_is_silent_and_step_runs_under_exported_errexit(self) -> None:
        readonly_score = Path("/proc/self/status")
        if not readonly_score.exists():
            self.skipTest(f"{readonly_score} is unavailable")
        command = scope_command()
        self.assertIn(OOM_SCORE_ADJ, command)
        result = self.run_scope(
            ["sh", "-c", "printf 'ran\\n'"],
            command.replace(OOM_SCORE_ADJ, str(readonly_score)),
            {**os.environ, "SHELLOPTS": "errexit"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ran\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    _ = unittest.main()
