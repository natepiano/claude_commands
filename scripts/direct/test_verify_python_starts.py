"""Bound helper starts without changing lint, test or watched input identities."""

from __future__ import annotations

from collections import Counter
import json
import shutil
import subprocess
import sys
from typing import override
import unittest

from scripts.direct.shell_function_test_support import shell_functions
from scripts.direct.tree_watch_test_support import TreeWatchFixture
from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.direct.verify_lint_scope_test_support import VERIFY, TEST_HANG_GUARD_SECS


class VerifyPythonStartsTests(VerifyLintCacheFixture):
    def test_lint_trace_has_one_start_per_key_pair_and_no_unnamed_helper(self) -> None:
        _ = self.enable_cache()
        trace = self.root / "python-starts.tsv"
        self.environment["VERIFY_PY_TRACE"] = str(trace)
        result = self.verify("lint", "sample")
        self.assert_fix_and_judgment_calls()
        rows = [line.split("\t") for line in trace.read_text().splitlines()]
        self.assertTrue(rows, result.stdout + result.stderr)
        for row in rows:
            self.assertEqual(len(row), 3, row)
            self.assertGreaterEqual(int(row[1]), 0)
            self.assertGreaterEqual(int(row[2]), 0)
        counts = Counter(row[0] for row in rows)
        print("lint helper starts:", dict(sorted(counts.items())), "total:", len(rows), flush=True)
        self.assertNotIn("INLINE_PY", counts, rows)
        self.assertNotIn("STDIN_PY", counts, rows)
        # Baseline: 14 tree starts, including four lint pairs. The fifth paired site is Mac-only.
        self.assertEqual(counts["tree_watch.py"], 10, rows)
        self.assertEqual(len(rows), 41, counts)  # Baseline 47, minus four pairs and two code hashes.

    def test_lint_code_key_tracks_inputs_and_clears_unreadable_keys_with_both_hash_tools(self) -> None:
        home = self.root / "key-home"
        invoke = home / ".claude/scripts/lint/invoke.sh"
        invoke.parent.mkdir(parents=True)
        _ = invoke.write_text("# private code input\n")
        harness = self.root / "code-key.sh"
        _ = harness.write_text(shell_functions(VERIFY, "hash_stream", "lint_code_key"))
        environment = {**self.environment, "HOME": str(home), "LINT_CONFIG_FILE": str(self.config),
                       "PY": str(self.root / "python-must-not-start")}
        bash = shutil.which("bash")
        assert bash is not None

        def key(*, remove: str = "", unreadable: bool = False) -> str:
            result = subprocess.run(
                [bash, "-euc", 'source "$1"; if [[ -n "$2" ]]; then rm -- "$2"; fi; lint_code_key',
                 "code-key", str(harness), remove], cwd=self.root, env=environment,
                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if unreadable:
                self.assertEqual(result.stdout, "\n")
            else:
                self.assertRegex(result.stdout, r"\A[0-9a-f]{64}\n\Z")
            return result.stdout

        original = key()
        for path in (harness, invoke, self.config):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                _ = path.write_bytes(before + b"\n# changed input\n")
                self.assertNotEqual(key(), original)
                _ = path.write_bytes(before)
                absent = key(remove=str(path))
                self.assertNotEqual(absent, original)
                # A present file containing the absence marker is still present.
                if path != harness:
                    _ = path.write_bytes(b"absent")
                    self.assertNotEqual(key(), absent)
                _ = path.write_bytes(before)
                self.assertEqual(key(), original)
        tools = self.root / "hash-tools"
        tools.mkdir()
        for name in ("cat", "shasum"):
            tool = shutil.which(name)
            assert tool is not None
            (tools / name).symlink_to(tool)
        unreadable = self.root / "unreadable.conf"
        _ = unreadable.write_text("# no read permission\n")
        unreadable.chmod(0)
        self.addCleanup(unreadable.chmod, 0o600)
        self.assertTrue(unreadable.is_file(), "regression input must pass the shell's -f check")
        read = subprocess.run(
            ["cat", "--", str(unreadable)], capture_output=True,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertNotEqual(read.returncode, 0, "regression input must fail a real read")
        for path in (environment["PATH"], str(tools)):
            with self.subTest(path=path):
                environment["PATH"] = path  # The restricted PATH forces the real shasum fallback.
                self.assertEqual(key(), original)
                environment["LINT_CONFIG_FILE"] = str(unreadable)
                _ = key(unreadable=True)
                environment["LINT_CONFIG_FILE"] = str(self.config)


class PairedTreeKeyTests(TreeWatchFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        session = self.root / "session"
        session.mkdir(exist_ok=True)
        _ = (session / "lint.conf").write_text("# key input\n")
        harness = session / "keys.sh"
        _ = harness.write_text('set -euo pipefail\nCMD=$1; shift; ARGS=("$@")\n'
                               + 'GATE_METADATA=$TEST_METADATA\n'
                               + shell_functions(VERIFY, "tree_key", "set_verdict_keys") + '''
tree_key
tree_key --tree-content
set_verdict_keys --watch-start "$UNIT_SESSION_DIR/start"
printf '%s\\n' "$VERDICT_KEY" "$VERDICT_TREE_KEY"
''')
        self.environment.update({
            "PY": sys.executable, "TEST_METADATA": json.dumps(self.metadata),
            "TREE_WATCH_SCRIPT": str(VERIFY.with_name("tree_watch.py")),
            "ASSERTION_GUARD_SCRIPT": str(VERIFY.with_name("assertion_guard.py")),
            "VERIFY_ASSERTION_BASE": "HEAD", "LINT_CONFIG_FILE": str(session / "lint.conf"),
            "HOME": str(self.root), "FILTER_RUN": "0", "HOLD_WAIT_DIR": str(session),
        })

    def assert_pair(self, *words: str) -> None:
        result = subprocess.run(
            ["bash", str(self.root / "session/keys.sh"), *words],
            cwd=self.root, env=self.environment, capture_output=True, text=True,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        keys = result.stdout.splitlines()
        self.assertEqual(len(keys), 4, result.stdout + result.stderr)
        for key in keys:
            self.assertRegex(key, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(keys[:2], keys[2:], "paired keys diverged from separate calls")
        self.assertNotEqual(keys[0], keys[1], "cache key lost script/guard inputs")
        self.assertEqual((self.root / "session/start").read_text().strip(),
                         self.key("--workspace" if words[0] == "lint" else "sample"))

    def test_lint_pair_matches_separate_keys_and_watch_start_on_changed_inputs(self) -> None:
        self.assert_pair("lint", "sample")
        _ = (self.root / "sample/src/new.rs").write_text("// new source\n")
        self.environment["RUSTFLAGS"] = "-C opt-level=1"
        self.assert_pair("lint", "--workspace", "--check")
        _ = (self.root / "session/lint.conf").write_text("# changed config\n")
        self.assert_pair("lint", "sample")

    def test_test_pair_preserves_package_filters_environment_and_watch_start(self) -> None:
        self.environment["FILTER_RUN"] = "1"
        self.environment["NEXTEST_RETRIES"] = "2"
        self.assert_pair("test", "sample", "--filter", "z", "--filter", "a", "--filter", "z")
        source = self.root / "sample/src/lib.rs"
        _ = source.write_text(source.read_text() + "// changed input\n")
        self.assert_pair("test", "sample", "--features", "a", "--filter", "render")


if __name__ == "__main__":
    _ = unittest.main()
