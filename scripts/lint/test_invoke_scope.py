#!/usr/bin/env python3
"""Behavior of the shell command run inside each build step's systemd scope."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast


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

    def test_scope_writes_cgroup_sidecar_before_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = root / "fake-cgroup"
            scope.mkdir()
            _ = (scope / "memory.peak").write_text("123\n")
            _ = (scope / "memory.pressure").write_text("some avg10=0 total=0\n")
            marker = root / "peak"
            command = scope_command().replace(
                '"/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"', f'"{scope}"'
            )
            result = subprocess.run(
                ["/bin/sh", "-c", 'exec 3>&2; exec /bin/sh -c "$@"',
                 "scope-test", command, str(marker), "sh", "-c",
                 'test -f "$1.cgroup" && cat "$1.cgroup"', "step", str(marker)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), str(scope))
            self.assertEqual((root / "peak.cgroup").read_text().strip(), str(scope))

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

    def test_sampler_exits_when_its_parent_shell_dies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shell: subprocess.Popen[str] = subprocess.Popen(
                ["bash", "-c", 'source "$1"; parent=$BASHPID; buildlog_sample_anon "$2" "$3" "$parent" & printf "%s\\n" "$!"; wait',
                 "sampler-test", str(INVOKE), str(root / "scope.cgroup"), str(root / "maximum")],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                env={**os.environ, "HOME": directory, "LINT_CONFIG_READER": str(root / "missing-config")},
            )
            sampler_pid = 0
            try:
                assert shell.stdout is not None
                sampler_pid = int(cast(str, shell.stdout.readline()).strip())
                os.kill(shell.pid, signal.SIGKILL)
                _ = shell.wait(timeout=3)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and self.process_running(sampler_pid):
                    time.sleep(0.05)
                self.assertFalse(self.process_running(sampler_pid), "sampler survived its parent shell")
            finally:
                if shell.poll() is None:
                    shell.kill()
                    _ = shell.wait(timeout=3)
                if sampler_pid and self.process_running(sampler_pid):
                    os.kill(sampler_pid, signal.SIGKILL)
                if shell.stdout is not None:
                    shell.stdout.close()

    @staticmethod
    def process_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            try:
                return stat.read_text().rsplit(") ", 1)[1][0] != "Z"
            except FileNotFoundError:
                return False
        return True

    def test_unreserved_scoped_step_removes_cgroup_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = root / "fake-cgroup"
            scope.mkdir()
            _ = (scope / "memory.peak").write_text("123\n")
            _ = (scope / "memory.pressure").write_text("some avg10=0 total=0\n")
            command = scope_command().replace(
                '"/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"', f'"{scope}"'
            )
            shell = (
                'source "$1"; BUILDLOG_SCOPE_SH=$2; '
                'buildlog_begin() { BUILDLOG_PEAK="$TMPDIR/buildlog.test.peak"; BUILDLOG_START=""; }; '
                'sweep_after_step() { :; }; '
                'systemd-run() { while [[ "$1" != -- ]]; do shift; done; shift; "$@"; }; '
                'run_once sh -c \'test -f "$1.cgroup"\' step "$TMPDIR/buildlog.test.peak" "$TMPDIR/sweep.py"'
            )
            result = subprocess.run(
                ["bash", "-c", shell, "sidecar-test", str(INVOKE), command],
                capture_output=True, text=True, check=False,
                env={**os.environ, "HOME": directory, "TMPDIR": directory,
                     "LINT_CONFIG_READER": str(root / "missing-config")},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(root.glob("*.peak.cgroup")), [])


if __name__ == "__main__":
    _ = unittest.main()
