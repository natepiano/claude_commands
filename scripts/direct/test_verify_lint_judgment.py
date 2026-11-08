"""Judge first; repair Cargo 101 unless a kill or lint hold prevents it."""

from __future__ import annotations

import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.lint.memory_test_support import write_config_fixture
from scripts.direct.verify_lint_diagnostics_test_support import (
    CLIPPY_DENY_ATTRIBUTE,
    CLIPPY_DENY_WARNINGS,
    RUSTC_DENY_ATTRIBUTE,
)
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB, SCRIPTS, VERIFY
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


LINT_FINDING = CLIPPY_DENY_WARNINGS


class FirstJudgmentRepairTests(unittest.TestCase):
    def repair_decision(
        self, status: int, diagnostic: str, *, hold_lint: int = 0, killed: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "clippy.log"
            _ = log.write_text(diagnostic, encoding="utf-8")
            script = "set -eu\n" + shell_functions(VERIFY, "first_judgment_permits_repair")
            script += '\nHOLD_LINT=$3\nRUN_LAST_ATTEMPT_KILLED=$4\nfirst_judgment_permits_repair "$1" "$2"\n'
            return subprocess.run(
                ["bash", "-c", script, "judgment", str(status), str(log), str(hold_lint), str(killed)],
                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )

    def test_nonlint_status_never_permits_repair(self) -> None:
        for status in (1, 2, 3, 10, 75, 76, 77, 125, 130, 137, 143):
            with self.subTest(status=status):
                result = self.repair_decision(status, LINT_FINDING)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_lint_finding_permits_repair(self) -> None:
        result = self.repair_decision(101, LINT_FINDING)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_compile_error_permits_repair(self) -> None:
        result = self.repair_decision(101, "error[E0308]: mismatched types\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_lint_hold_prevents_repair(self) -> None:
        result = self.repair_decision(101, LINT_FINDING, hold_lint=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_killed_attempt_prevents_repair(self) -> None:
        result = self.repair_decision(101, LINT_FINDING, killed=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_signal_report_prevents_repair(self) -> None:
        result = self.repair_decision(
            101, LINT_FINDING + "process didn't exit successfully: `rustc` (signal: 9, SIGKILL: kill)\n",
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


# Supply the first tool result when a test requests it.
# The shared tool fixture supplies all later results.
FIRST_JUDGMENT = r'''
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
events = Path(os.environ["TEST_CARGO_EVENTS"])
if arguments[:1] == ["clippy"] and "--fix" not in arguments and "--version" not in arguments:
    calls = [json.loads(line)["args"] for line in events.read_text().splitlines()] if events.exists() else []
    judgments = [args for args in calls if args[:1] == ["clippy"] and "--fix" not in args]
    if not judgments and "TEST_CLIPPY_FIRST_STATUS" in os.environ:
        with events.open("a") as output:
            output.write(json.dumps({"args": arguments}) + "\n")
        if os.environ.get("TEST_JUDGMENT_HOLD") == "1":
            Path(os.environ["BUILDLOG_HOLD_WAITER_FILE"]).with_name("lint-held").touch()
        print(os.environ.get("TEST_CLIPPY_FIRST_DIAGNOSTIC", ""), file=sys.stderr)
        raise SystemExit(int(os.environ["TEST_CLIPPY_FIRST_STATUS"]))
'''


class VerifyLintJudgmentFixture(VerifyLintCacheFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.environment.update({
            "ROSTER_RUN_ROOT": str(self.root / "roster-runs"),
            "UNIT_RUN_ROOT": str(self.root / "unit-runs"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
        })

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        self.write_executable(self.root / "bin" / "cargo", FIRST_JUDGMENT + CARGO_STUB)
        self.environment["TEST_CLIPPY_FIRST_DIAGNOSTIC"] = LINT_FINDING

    @override
    def assert_fix_and_judgment_calls(self, *, fix_passes: int = 1) -> None:
        scope = ["--workspace", "--all-targets", "--all-features", "--keep-going"]
        judgment = [*scope, "--", "-D", "warnings"]
        fix = ["--jobs", "8", "--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs", *judgment]
        self.assertEqual(self.calls("clippy"), [judgment, *([fix] * fix_passes), judgment])


class VerifyLintJudgmentTests(VerifyLintJudgmentFixture):
    def invoke_lint_functions(self, *, killed: int = 0) -> subprocess.CompletedProcess[str]:
        """Run the lint sequence with real tools and private state."""
        self.events.unlink(missing_ok=True)
        self.taplo_events.unlink(missing_ok=True)
        functions = shell_functions(
            VERIFY, "first_judgment_permits_repair", "read_metadata", "load_metadata", "workspace_taplo",
            "tree_key", "set_verdict_keys", "after_tree_change", "stop_tree_watch", "cancel_tree_run",
            "start_tree_watch", "finish_tree_watch", "run_lint_judgment", "run_lint_held_sequence", "run_lint_sequence",
        )
        functions += shell_functions(SCRIPTS / "lint/invoke.sh", "fmt_cargo", "invoke_clippy", "invoke_clippy_fix", "invoke_mend")
        changed_members = VERIFY.read_text(encoding="utf-8").split("CHANGED_MEMBERS_PY='", 1)[1].split("\n'\n", 1)[0]
        script = "set -eo pipefail\n" + functions
        script += "\nCHANGED_MEMBERS_PY=" + shlex.quote(changed_members) + "\n"
        script += r'''
source "$HOME/.claude/scripts/lint/lint_config.sh"
run() {
    local status=0
    if [[ -n "${CLIPPY_LOG:-}" ]]; then
        "$@" 2>&1 | tee "$CLIPPY_LOG"
        status=${PIPESTATUS[0]}
    else
        "$@" || status=$?
    fi
    RUN_LAST_ATTEMPT_KILLED=$TEST_RUNNER_KILLED_FLAG
    return "$status"
}
CMD=lint
ARGS=(sample)
PKG=sample
HOLD_LINT=0
CHECK_LINT=0
METADATA_READY=0
CLIPPY_FIX_JOBS=8
RUN_LAST_ATTEMPT_KILLED=0
CLIPPY_FIX_LIMIT_REACHED=0
FEATURE_FLAGS=()
RUN_LOG=""
CACHE_DIR=""
FILTER_RUN=0
TREE_CHANGED_STATUS=77
TREE_WATCH_PID=""
TREE_WATCH_DIR=""
HOLD_WAIT_DIR="$PACKAGE_ROOT/hold-wait"
mkdir -p "$HOLD_WAIT_DIR"
export BUILDLOG_HOLD_WAITER_FILE="$HOLD_WAIT_DIR/waiter"
VERIFY_METADATA_SCRIPT="$HOME/.claude/scripts/direct/verify_metadata.py"
TREE_WATCH_SCRIPT="$HOME/.claude/scripts/direct/tree_watch.py"
BUILD_FOLDERS_SCRIPT="$HOME/.claude/scripts/lib/build_folders.py"
GATE_METADATA="$TEST_METADATA"
trap 'stop_tree_watch' EXIT
run_lint_sequence
'''
        return subprocess.run(
            ["bash", "-c", script], cwd=self.root,
            env={**self.environment, "PY": sys.executable, "TEST_RUNNER_KILLED_FLAG": str(killed)},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def test_explicit_clippy_deny_attribute_repairs(self) -> None:
        self.environment.update({
            "TEST_CLIPPY_FIRST_STATUS": "101",
            "TEST_CLIPPY_FIRST_DIAGNOSTIC": CLIPPY_DENY_ATTRIBUTE,
        })
        result = self.invoke_lint_functions()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_fix_and_judgment_calls()

    def test_explicit_rustc_deny_attribute_repairs(self) -> None:
        self.environment.update({
            "TEST_CLIPPY_FIRST_STATUS": "101",
            "TEST_CLIPPY_FIRST_DIAGNOSTIC": RUSTC_DENY_ATTRIBUTE,
        })
        result = self.invoke_lint_functions()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_fix_and_judgment_calls()

    def test_mixed_lint_and_signalled_compiler_never_repairs(self) -> None:
        # The lint diagnostic is real compiler output. Only Cargo's process
        # termination report is simulated; no compiler needs to be killed.
        diagnostic = CLIPPY_DENY_WARNINGS + """error: could not compile `other` (lib)

Caused by:
  process didn't exit successfully: `rustc --crate-name other` (signal: 9, SIGKILL: kill)
"""
        # Prove the signal-text veto independently of run()'s killed flag.
        self.environment.update({
            "TEST_CLIPPY_FIRST_STATUS": "101",
            "TEST_CLIPPY_FIRST_DIAGNOSTIC": diagnostic,
        })
        result = self.invoke_lint_functions()
        self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
        self.assertEqual(len(self.calls("clippy")), 1)
        self.assertNotIn("--fix", self.calls("clippy")[0])

    def test_killed_attempt_flag_without_signal_text_never_repairs(self) -> None:
        self.environment["TEST_CLIPPY_FIRST_STATUS"] = "101"
        result = self.invoke_lint_functions(killed=1)
        self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
        self.assertEqual(len(self.calls("clippy")), 1)
        self.assertNotIn("--fix", self.calls("clippy")[0])

    def test_hold_started_during_judgment_suppresses_repairs(self) -> None:
        self.environment.update({"TEST_CLIPPY_FIRST_STATUS": "101", "TEST_JUDGMENT_HOLD": "1"})
        result = self.invoke_lint_functions()
        self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
        self.assertEqual(len(self.calls("clippy")), 1)
        self.assertNotIn("--fix", self.calls("clippy")[0])

    def test_passing_first_judgment_runs_once_without_fix(self) -> None:
        _ = self.verify("lint", "sample")
        self.assertEqual(self.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings",
        ]])
        self.assertEqual([args for args in self.calls("+nightly") if "--version" not in args],
                         [["fmt", "-p", "sample"]])

    def test_first_judgment_nonlint_status_never_repairs(self) -> None:
        # Lint-shaped output never turns a failed first judgment into a repair.
        # Status 1 takes the plain failure path; 143 also takes the hold-stop checks in run_step.
        for status in (1, 143):
            with self.subTest(status=status):
                self.environment["TEST_CLIPPY_FIRST_STATUS"] = str(status)
                result = (self.invoke_verify("lint", "sample", cwd=self.root) if status == 143
                          else self.invoke_lint_functions())
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertEqual(len(self.calls("clippy")), 1)
                self.assertNotIn("--fix", self.calls("clippy")[0])

    def test_compile_errors_and_unknown_101_repair_and_return_final_judgment(self) -> None:
        self.environment["TEST_CLIPPY_FIRST_STATUS"] = "101"
        for diagnostic, final_status in (
            ("", 0),
            ("error[E0308]: mismatched types\n", 101),
            ("error: proc macro panicked\n", 0),
            (LINT_FINDING + "error[E0425]: cannot find value `missing` in this scope\n", 101),
            ("error: proc macro panicked\n" + LINT_FINDING, 0),
        ):
            with self.subTest(diagnostic=diagnostic, final_status=final_status):
                self.environment.update({
                    "TEST_CLIPPY_FIRST_DIAGNOSTIC": diagnostic,
                    "TEST_CLIPPY_STATUS": str(final_status),
                })
                result = self.invoke_lint_functions()
                self.assertEqual(result.returncode, final_status, result.stdout + result.stderr)
                self.assert_fix_and_judgment_calls()
                self.assertEqual(self.lint_stages(), [
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[0]],
                    ["clippy", *self.calls("clippy")[1]],
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[2]],
                ])

    def test_lint_finding_repairs_formats_and_returns_final_judgment(self) -> None:
        self.environment["TEST_CLIPPY_FIRST_STATUS"] = "101"
        self.environment["TEST_CLIPPY_FIX_STATUS"] = "41"
        for status in (0, 43, 101):
            with self.subTest(final_status=status):
                self.environment["TEST_CLIPPY_STATUS"] = str(status)
                result = self.invoke_lint_functions()
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assert_fix_and_judgment_calls()
                self.assertEqual(self.lint_stages(), [
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[0]],
                    ["clippy", *self.calls("clippy")[1]],
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[2]],
                ])

    def test_fix_limit_is_read_from_config(self) -> None:
        self.environment.update({
            "TEST_CLIPPY_FIRST_STATUS": "101", "TEST_CLIPPY_STATUS": "101",
            "TEST_FIX_APPEND": "// fix pass\n", "TEST_GIT_DIRTY": "1",
        })
        for limit in (1, 2, 4):
            with self.subTest(limit=limit):
                self.write_config(clippy="on")
                _ = write_config_fixture(self.config, f"clippy=on\nclippy_fix_pass_limit={limit}\n")
                result = self.invoke_lint_functions()
                self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
                self.assert_fix_and_judgment_calls(fix_passes=limit)


if __name__ == "__main__":
    _ = unittest.main()
