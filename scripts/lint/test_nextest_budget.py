#!/usr/bin/env python3
"""Local nextest runs share the machine's test threads, in temporary directories only."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, override

import nextest_budget as budget

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if TYPE_CHECKING:
    from ..build_hold.override_guard import require_checkout
else:
    from scripts.build_hold.override_guard import require_checkout

require_checkout(Path(__file__))


INVOKE = Path(__file__).with_name("invoke.sh")
HELPER = Path(__file__).with_name("nextest_budget.py")


class SharedWidthTests(unittest.TestCase):
    def test_width_divides_by_runs_with_floor_and_full_width_cap(self) -> None:
        cases = ((32, 1, 32), (32, 2, 16), (32, 3, 10), (32, 8, 4), (32, 12, 4), (12, 4, 4), (2, 3, 2))
        for full, runs, width in cases:
            with self.subTest(full=full, runs=runs):
                self.assertEqual(budget.shared_width(full, runs), width)


class BudgetCase(unittest.TestCase):
    root: Path = Path()
    directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "buildlog" / "admission" / "nextest"
        self.directory.mkdir(parents=True)

    def open_marker(self, name: str) -> int:
        fd = os.open(self.directory / f"{name}.new", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        self.addCleanup(close_quietly, fd)
        return fd

    def hold(self, name: str, full: int = 32) -> tuple[int, int]:
        """Claim as a live run would; the returned fd keeps it in flight."""
        fd = self.open_marker(name)
        return fd, budget.claim(self.directory, name, full, fd)

    def markers(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir() if path.name != "lock")


def close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class ClaimTests(BudgetCase):
    def test_claims_accumulate_and_a_closed_run_stops_counting(self) -> None:
        first, width = self.hold("a")
        self.assertEqual(width, 32)
        self.assertEqual(self.hold("b")[1], 16)
        self.assertEqual(self.hold("c")[1], 10)
        os.close(first)
        self.assertEqual(self.hold("d")[1], 10)
        self.assertEqual(self.markers(), ["b.run", "c.run", "d.run"])

    def test_killed_run_is_not_counted_and_its_marker_is_removed(self) -> None:
        script = (
            'exec 9>>"$1/killed.new"\n'
            + 'python3 "$2" claim "$1" killed 32 9 || exit 1\n'
            + "echo claimed\n"
            + "exec sleep 60\n"
        )
        child = subprocess.Popen(
            ["bash", "-c", script, "killed-run", str(self.directory), str(HELPER)],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        assert child.stdout is not None
        self.assertEqual(child.stdout.readline(), "32\n")
        self.assertEqual(child.stdout.readline(), "claimed\n")
        self.assertEqual(self.hold("live")[1], 16)
        child.kill()
        _ = child.wait()
        child.stdout.close()
        self.assertEqual(self.hold("next")[1], 16)
        self.assertEqual(self.markers(), ["live.run", "next.run"])

    def test_pending_marker_is_removed_only_after_the_grace(self) -> None:
        _ = (self.directory / "young.new").write_text("")
        old = self.directory / "old.new"
        _ = old.write_text("")
        past = time.time() - budget.PENDING_GRACE_S - 1
        os.utime(old, (past, past))
        _ = self.hold("run")
        self.assertEqual(self.markers(), ["run.run", "young.new"])


class ShellTests(BudgetCase):
    def run_nextest(self, mode: str, *arguments: str) -> str:
        config_reader = self.root / "lint_config.sh"
        _ = config_reader.write_text("lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n")
        script = f"""
set -euo pipefail
source "$1"
shift
require_nextest() {{ :; }}
# run_once claims the share once the memory gate admits the step; this stand-in admits at once.
run() {{
    if [[ -n "${{LINT_NEXTEST_SHARE:-}}" ]]; then
        nextest_share_claim "$@"
        set -- "${{LINT_NEXTEST_SHARED_ARGV[@]}}"
    fi
    printf '<%s>' "$@"
    printf '\\n'
}}
# Bash ignores assignments to EPOCHSECONDS, and core counts come from the
# machine: fix the trial arm and both widths so the case never reads either.
nextest_width_arm() {{ LINT_TEST_WIDTH_ARM=logical; }}
nextest_logical_cores() {{ LINT_LOGICAL_CORES=32; }}
nextest_physical_cores() {{ LINT_PHYSICAL_CORES=16; }}
LINT_TEST_WIDTH={mode}
run_nextest "$@"
"""
        environment = {
            **os.environ,
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_CALL_ID": "",
            "BUILDLOG_CALLER": "",
            # Every lint step waits for natedev's hold: a real hold must not stop the case.
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "LINT_CONFIG_READER": str(config_reader),
        }
        result = subprocess.run(
            ["bash", "-c", script, "nextest-budget-test", str(INVOKE), *arguments],
            capture_output=True, text=True, check=False, env=environment, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_run_alone_takes_the_full_width_and_leaves_no_marker(self) -> None:
        output = self.run_nextest("logical", "--workspace")
        self.assertEqual(output, "<cargo><nextest><run><--test-threads><32><--workspace>\n")
        self.assertEqual(self.markers(), [])

    def test_runs_in_flight_divide_the_width(self) -> None:
        _ = self.hold("a")
        _ = self.hold("b")
        output = self.run_nextest("logical", "--workspace")
        self.assertEqual(output, "nextest: 10 of 32 test threads, shared with the runs in flight\n"
                         + "<cargo><nextest><run><--test-threads><10><--workspace>\n")
        self.assertEqual(self.markers(), ["a.run", "b.run"])

    def test_caller_width_wins_over_runs_in_flight(self) -> None:
        _ = self.hold("a")
        cases = (("--test-threads", "3"), ("-j4",))
        for arguments in cases:
            with self.subTest(arguments=arguments):
                output = self.run_nextest("logical", *arguments)
                rendered = "".join(f"<{argument}>" for argument in arguments)
                self.assertEqual(output, f"<cargo><nextest><run>{rendered}\n")
                self.assertEqual(self.markers(), ["a.run"])

    def test_trial_mode_keeps_the_arm_width_for_width_trial(self) -> None:
        _ = self.hold("a")
        output = self.run_nextest("trial", "--workspace")
        self.assertEqual(output, "<cargo><nextest><run><--test-threads><32><--workspace>\n")


if __name__ == "__main__":
    _ = unittest.main()
