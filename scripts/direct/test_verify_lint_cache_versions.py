"""Invalidate lint verdicts when any lint tool version changes."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from typing import override
from unittest import mock

from scripts.direct import tree_watch
from scripts.direct.tree_watch_test_support import TreeWatchFixture
from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB, VERIFY
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyLintToolVersionTests(VerifyLintCacheFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.environment.update({
            "ROSTER_RUN_ROOT": str(self.root / "roster-runs"),
            "UNIT_RUN_ROOT": str(self.root / "unit-runs"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
        })

    def invoke_cache_functions(self) -> subprocess.CompletedProcess[str]:
        """Run the cache decisions with the fixture's real tool queries."""
        self.events.unlink(missing_ok=True)
        self.taplo_events.unlink(missing_ok=True)
        functions = self.root / "cache-functions.sh"
        _ = functions.write_text(shell_functions(
            VERIFY, "verify_helper", "hash_stream", "lint_code_key", "tree_key", "set_verdict_keys",
            "cache_lookup", "user_clock", "cache_record",
        ) + shell_functions(VERIFY.parent.parent / "lint/invoke.sh", "invoke_clippy"), encoding="utf-8")
        script = r'''
set -eo pipefail
source "$1"
source "$HOME/.claude/scripts/lint/lint_config.sh"
run() { "$@"; }
note_event() { :; }
CMD=lint
ARGS=(sample)
FILTER_RUN=0
NO_CACHE=0
LOCAL_ONLY=0
MAC_ONLY=0
HOLD_WAIT_DIR=""
TREE_WATCH_SCRIPT="$HOME/.claude/scripts/direct/tree_watch.py"
GATE_METADATA="$TEST_METADATA"
CACHE_DIR="$UNIT_RUN_DIR/verify_cache"
LOOKUP_STATUS=0
export VERIFY_LINT_VERSIONS_FILE="$PACKAGE_ROOT/direct-lint-versions"
export VERIFY_LINT_RUSTFMT_VERSION_FILE="$PACKAGE_ROOT/direct-rustfmt-version"
rm -f "$VERIFY_LINT_VERSIONS_FILE" "$VERIFY_LINT_RUSTFMT_VERSION_FILE"
LINT_LOADED_CODE_KEY="$(lint_code_key)"
if cache_lookup; then
    exit "$LOOKUP_STATUS"
fi
mkdir -p "$CACHE_DIR"
set_verdict_keys
RUN_LOG="$PACKAGE_ROOT/direct-lint.log"
EXIT_STATUS=0
invoke_clippy --workspace --all-targets --all-features --keep-going > "$RUN_LOG" 2>&1 || EXIT_STATUS=$?
cat "$RUN_LOG"
kind=pass
if (( EXIT_STATUS != 0 )); then kind=fail; fi
cache_record "$kind" 0 || true
exit "$EXIT_STATUS"
'''
        return subprocess.run(
            ["bash", "-c", script, "cache-functions", str(functions)], cwd=self.root,
            env={**self.environment, "PY": sys.executable}, capture_output=True,
            text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def check_version_change(self, tool: str) -> None:
        _ = self.dirty_cache()
        for old_status, new_status in ((0, 43), (43, 0)):
            with self.subTest(cached_status=old_status):
                self.environment["TEST_VERSION_" + tool] = f"{tool} before-{old_status}"
                self.environment["TEST_CLIPPY_STATUS"] = str(old_status)
                first = self.invoke_cache_functions()
                self.assertEqual(first.returncode, old_status, first.stdout + first.stderr)
                self.assertTrue(self.clippy_ran())
                repeated = self.invoke_cache_functions()
                self.assertEqual(repeated.returncode, old_status, repeated.stdout + repeated.stderr)
                self.assertIn("(recorded)", repeated.stdout)
                self.assertFalse(self.clippy_ran())

                self.environment["TEST_VERSION_" + tool] = f"{tool} after-{old_status}"
                self.environment["TEST_CLIPPY_STATUS"] = str(new_status)
                changed = self.invoke_cache_functions()
                self.assertEqual(changed.returncode, new_status, changed.stdout + changed.stderr)
                self.assertNotIn("(recorded)", changed.stdout)
                self.assertTrue(self.clippy_ran())

    def test_mend_version_invalidates_passes_and_failures(self) -> None:
        self.check_version_change("MEND")

    def test_rustfmt_version_invalidates_passes_and_failures(self) -> None:
        self.check_version_change("RUSTFMT")

    def test_clippy_version_invalidates_passes_and_failures(self) -> None:
        self.check_version_change("CLIPPY")

    def test_taplo_version_invalidates_passes_and_failures(self) -> None:
        self.check_version_change("TAPLO")

    def test_unknown_version_prevents_cache_reuse_and_publication(self) -> None:
        cache = self.dirty_cache()
        _ = self.verify("lint", "sample")
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)
        for status in (0, 1):
            with self.subTest(version_status=status):
                self.write_executable(self.root / "bin/cargo", (
                    "import sys\n"
                    "if sys.argv[1:] == ['mend', '--version']:\n"
                    f"    raise SystemExit({status})\n"
                ) + CARGO_STUB)
                for attempt in range(2):
                    result = (self.invoke_verify("lint", "sample", cwd=self.root)
                              if status == 0 and attempt == 0 else self.invoke_cache_functions())
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("(recorded)", result.stdout)
                    self.assertTrue(self.clippy_ran())
                    self.assertEqual(len(list(cache.glob("*.pass"))), 1)


class LintVersionKeyTests(TreeWatchFixture):
    def test_unknown_versions_preserve_the_input_watch(self) -> None:
        metadata = json.dumps(self.metadata)
        _, watched = tree_watch.TreeInputs(["--", "lint"], metadata).keys(cache=False)
        for error in (OSError("tool missing"), ValueError("empty version"),
                      subprocess.CalledProcessError(1, "cargo"), subprocess.TimeoutExpired("cargo", 10)):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(tree_watch, "lint_tool_versions", side_effect=error):
                inputs = tree_watch.TreeInputs(["--", "lint"], metadata)
                self.assertEqual(inputs.keys(), ("", watched))

    def test_test_keys_and_input_watches_skip_lint_tool_queries(self) -> None:
        metadata = json.dumps(self.metadata)
        with mock.patch.object(tree_watch, "lint_tool_versions", side_effect=AssertionError("unexpected version query")):
            inputs = tree_watch.TreeInputs(["--package", "sample", "--", "test", "sample"], metadata)
            key, watched = inputs.keys()
            self.assertRegex(key, r"^[0-9a-f]{64}$")
            self.assertRegex(watched, r"^[0-9a-f]{64}$")
            _, lint_watch = tree_watch.TreeInputs(["--", "lint"], metadata).keys(cache=False)
            self.assertRegex(lint_watch, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    _ = unittest.main()
