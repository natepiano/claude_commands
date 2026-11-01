"""Exercise lint cache invalidation with the real guard and private Git refs."""

from __future__ import annotations

import os
import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, override

from scripts.direct import verify_lint_scope_test_support as support
from scripts.direct import assertion_guard_test_support as guard_support
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyLintGuardKeyTests(support.VerifyLintScopeFixture):
    strong: str = ""
    weak: str = ""

    @override
    def setUp(self) -> None:
        super().setUp()
        (self.root / "bin/git").unlink()
        (self.root / ".git").rmdir()
        self.environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        _ = self.environment.pop("VERIFY_ASSERTION_BASE", None)
        self.environment["VERIFY_ASSERTION_GUARD_SCRIPT"] = str(
            support.VERIFY.with_name("assertion_guard.py"))
        _ = self.git("init", "-q", "-b", "feature")
        _ = self.git("init", "--bare", "-q", "-b", "main", str(self.root / "remote.git"))
        _ = self.git("remote", "add", "origin", str(self.root / "remote.git"))
        # Every changing tool log/cache/home file belongs to this fixture, not
        # the Rust tree. Only the explicitly added source files affect its key.
        _ = (self.root / ".gitignore").write_text("*\n", encoding="utf-8")
        _ = (self.root / "src/lib.rs").write_text(
            "#[test]\nfn test_value() {\n    assert_eq!(actual, expected);\n}\n", encoding="utf-8")
        _ = self.git("add", "-f", ".gitignore", "Cargo.toml", "src/lib.rs")
        _ = self.git("commit", "-q", "-m", "strong assertions")
        self.strong = self.git("rev-parse", "HEAD")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/strict")
        _ = (self.root / "src/lib.rs").write_text(
            "#[test]\nfn test_value() {\n    assert!(actual <= expected);\n}\n", encoding="utf-8")
        _ = self.git("add", "-f", "src/lib.rs")
        _ = self.git("commit", "-q", "-m", "branch assertion change")
        self.weak = self.git("rev-parse", "HEAD")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/accepted")
        _ = self.git("branch", "--set-upstream-to=origin/accepted")
        _ = self.enable_cache()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=fixture", "-c", "user.email=fixture@test",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments],
            env=self.environment, capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
        )
        return result.stdout.strip()

    def cache_passing_lint(self) -> None:
        first = self.verify("lint", "sample")
        self.assertNotIn("PASS (recorded)", first.stdout)
        self.assertTrue(self.clippy_ran())
        cached = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", cached.stdout)
        self.assertFalse(self.clippy_ran())

    def assert_guard_reruns_and_blocks(self) -> None:
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("test_value weaker:", result.stdout)
        self.assertNotIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())

    def calculate_build_keys(self, *arguments: str, extra_environment: dict[str, str] | None = None,
                        target: str = "") -> tuple[str, str, str]:
        """Run the shared production calculator without starting Cargo work."""
        metadata = cast(dict[str, object], json.loads(self.environment["TEST_METADATA"]))
        packages = cast(list[dict[str, object]], metadata["packages"])
        packages[0]["dependencies"] = []
        metadata["target_directory"] = target or str(self.root / "configured-target")
        output = self.root / "test-build-inputs"
        output.unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, str(support.VERIFY.with_name("tree_watch.py")), "--key",
             "--package", "sample", str(support.VERIFY),
             str(support.SCRIPTS / "lint/invoke.sh"), str(self.config), "--", "test", "sample", *arguments],
            input=json.dumps(metadata), cwd=self.root,
            env={**self.environment, "VERIFY_COMPUTE_TEST_BUILD": "1",
                 "VERIFY_TEST_BUILD_INPUTS_FILE": str(output), **(extra_environment or {})},
            capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
        )
        lines = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^[0-9a-f]{64}$")
        return result.stdout.strip(), lines[0], lines[1]

    def test_build_input_key_changes_with_features_profile_flags_and_toolchain(self) -> None:
        arguments = ("--filter", "native_rate", "--features", "a")
        _, original, _ = self.calculate_build_keys(*arguments)
        _, other_features, _ = self.calculate_build_keys("--filter", "native_rate", "--features", "b")
        self.assertNotEqual(original, other_features)
        for name, value in (("RUSTFLAGS", "-C opt-level=2"),
                            ("CARGO_ENCODED_RUSTFLAGS", "-C\x1fopt-level=2"),
                            ("RUSTUP_TOOLCHAIN", "nightly-fixture"),
                            ("CARGO_PROFILE_TEST_OPT_LEVEL", "2"),
                            ("NEXTEST_CARGO_PROFILE", "release")):
            with self.subTest(input=name):
                _, changed, _ = self.calculate_build_keys(*arguments, extra_environment={name: value})
                self.assertNotEqual(original, changed)
        self.write_shell(self.root / "bin/rustc", 'printf "rustc changed fixture\\n"\n')
        _, new_compiler, _ = self.calculate_build_keys(*arguments)
        self.assertNotEqual(original, new_compiler)

    def test_build_input_key_ignores_filter_names_and_nextest_run_only_environment(self) -> None:
        verdict, build, _ = self.calculate_build_keys("--filter", "native_rate", "--features", "a")
        other_verdict, other_build, _ = self.calculate_build_keys(
            "--filter", "render", "--filter", "audio", "--features", "a")
        self.assertNotEqual(verdict, other_verdict)
        self.assertEqual(build, other_build)
        for name, value in (("NEXTEST_TEST_THREADS", "1"), ("NEXTEST_RETRIES", "3"),
                            ("NEXTEST_STATUS_LEVEL", "all")):
            with self.subTest(input=name):
                changed_verdict, same_build, _ = self.calculate_build_keys(
                    "--filter", "native_rate", "--features", "a", extra_environment={name: value})
                self.assertNotEqual(verdict, changed_verdict)
                self.assertEqual(build, same_build)

    def test_build_input_key_uses_metadata_target_and_package_source(self) -> None:
        _, build, target = self.calculate_build_keys("--filter", "native_rate")
        self.assertEqual(target, str(self.root / "configured-target"))
        self.assertNotEqual(target, self.environment["CARGO_TARGET_DIR"])
        _, changed_target, recorded_target = self.calculate_build_keys(
            "--filter", "native_rate", target=str(self.root / "another-target"))
        self.assertNotEqual(build, changed_target)
        self.assertEqual(recorded_target, str(self.root / "another-target"))
        with (self.root / "src/lib.rs").open("a", encoding="utf-8") as source:
            _ = source.write("// new build input\n")
        _, changed_source, _ = self.calculate_build_keys("--filter", "native_rate")
        self.assertNotEqual(build, changed_source)

    def test_source_change_during_hold_refreshes_admission_and_recorded_build_inputs(self) -> None:
        source = support.VERIFY.read_text(encoding="utf-8")
        # Use the actual hold wrapper and pre-run key setup, including their
        # ordering relative to invoke.sh's run_once and buildlog_begin.
        hold_wrapper = "# A build hold stops every natedev build," + source.split(
            "# A build hold stops every natedev build,", 1)[1].split(
                "# Again with the token held:", 1)[0]
        key_setup = source.split('TOKEN_WAIT_FILE="$(mktemp)"\n', 1)[1].split(
            'if [[ -n "${CACHE_DIR}" ]]', 1)[0]
        metadata = cast(dict[str, object], json.loads(self.environment["TEST_METADATA"]))
        packages = cast(list[dict[str, object]], metadata["packages"])
        packages[0]["dependencies"] = []
        target = self.root / "configured-target"
        target.mkdir()
        metadata["target_directory"] = str(target)
        probe = self.root / "probe-freshness.py"
        _ = probe.write_text(f"""import json, os, sys
from datetime import UTC, datetime
from pathlib import Path
sys.path.insert(0, {str(support.SCRIPTS / 'lint')!r})
import memory_admit as gate
fresh = gate.is_fresh_test(Path(os.environ['BUILDLOG_DIR']), gate.store.host_name(),
    os.getcwd(), 'sample', 'filtered', os.environ['BUILDLOG_TEST_BUILD_KEY'],
    datetime.now(UTC), os.environ['BUILDLOG_TEST_TARGET_DIR'])
Path('admission.json').write_text(json.dumps({{'fresh': fresh,
    'key': os.environ['BUILDLOG_TEST_BUILD_KEY']}}))
""", encoding="utf-8")
        harness = self.root / "hold-regression.sh"
        _ = harness.write_text("\n".join((
            'set -euo pipefail',
            f'source {shlex.quote(str(support.SCRIPTS / "lint/invoke.sh"))}',
            f'PY={shlex.quote(sys.executable)}',
            f'VERIFY={shlex.quote(str(support.VERIFY))}',
            f'TREE_WATCH_SCRIPT={shlex.quote(str(support.VERIFY.with_name("tree_watch.py")))}',
            r'''CMD=test
HOLD_WAIT_DIR="$PWD/tmp"
tree_key() {
  VERIFY_TEST_BUILD_INPUTS_FILE="$HOLD_WAIT_DIR/test-build-inputs" "$PY" "$TREE_WATCH_SCRIPT" --key --package sample "$VERIFY" "$HOME/.claude/scripts/lint/invoke.sh" "$LINT_CONFIG_FILE" -- test sample --filter native_rate <<< "$TEST_METADATA"
}
build_hold_wait_local() {
  printf "// edited during hold\n" >> src/lib.rs
  : > "$PWD/hold-released"
}
build_hold_applies() { return 0; }
build_hold_nothing_held() { test -f "$PWD/hold-released"; }
step_state() { :; }
''',
            hold_wrapper, key_setup,
            # Seed a finished build of A before the hold changes it to B.
            r'''printf "%s\n" "$BUILDLOG_TEST_BUILD_KEY" > old-build-key
buildlog_begin cargo nextest run -E "package(sample) & test(native_rate)"
buildlog_end 0 "" 1 cargo nextest run -E "package(sample) & test(native_rate)"
"$PY" probe-freshness.py
mv admission.json before-hold.json
buildlog_wait_for_memory() {
  BUILDLOG_MEM_OUTCOME=Granted BUILDLOG_MEM_WAIT_S=0
  "$PY" probe-freshness.py
}
build_hold_check_local() { return 0; }
buildlog_exec() { :; }
nextest_share_end() { :; }
buildlog_release_memory() { :; }
sweep_after_step() { :; }
run_once cargo nextest run -E "package(sample) & test(native_rate)"
''',
        )), encoding="utf-8")
        result = subprocess.run(
            ["bash", str(harness)], cwd=self.root,
            env={**self.environment, "TEST_METADATA": json.dumps(metadata),
                 "BUILDLOG_OFF": "0", "BUILDLOG_SYNC": "1"},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before_hold = cast(dict[str, object], json.loads((self.root / "before-hold.json").read_text()))
        self.assertTrue(before_hold["fresh"])
        admission = cast(dict[str, object], json.loads((self.root / "admission.json").read_text()))
        self.assertFalse(admission["fresh"])
        old_key = (self.root / "old-build-key").read_text().strip()
        _, new_key, _ = self.calculate_build_keys("--filter", "native_rate", target=str(target))
        self.assertNotEqual(new_key, old_key)
        self.assertEqual(admission["key"], new_key)
        rows = [cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["test_build_key"], old_key)
        self.assertEqual(rows[1]["test_build_key"], new_key)
        self.assertIsNotNone(rows[1]["tree_key"])
        self.assertNotEqual(rows[0]["tree_key"], rows[1]["tree_key"])
        self.assertFalse(rows[1]["tree_changed"])

    def test_upstream_change_invalidates_lint_without_a_source_tree_change(self) -> None:
        self.cache_passing_lint()
        before = self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("branch", "--set-upstream-to=origin/strict")
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), before)
        self.assert_guard_reruns_and_blocks()

    def test_private_origin_head_move_changes_excluded_branch_and_invalidates_lint(self) -> None:
        _ = self.git("branch", "--unset-upstream")
        # These symbolic refs live only in this fixture's private repository.
        _ = self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/strict")
        self.cache_passing_lint()
        before = self.git("rev-parse", "HEAD^{tree}")
        _ = self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/accepted")
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), before)
        self.assert_guard_reruns_and_blocks()

    def test_new_prompt_without_assertion_rulings_keeps_recorded_lint(self) -> None:
        self.cache_passing_lint()
        prompt = Path(self.environment["UNIT_SESSION_DIR"]) / "next-prompt.md"
        _ = prompt.write_text("Please continue with the next task.\n", encoding="utf-8")
        result = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())

    def test_prose_changes_around_unchanged_ruling_keep_recorded_lint(self) -> None:
        _ = self.git("branch", "--set-upstream-to=origin/strict")
        ruling = Path(self.environment["UNIT_SESSION_DIR"]) / "approval.md"
        approval = "Assertion-Change: src/lib.rs::test_value — Accepted bound contract.\n"
        _ = ruling.write_text(approval, encoding="utf-8")
        self.cache_passing_lint()
        _ = ruling.write_text("Updated task notes.\n\n" + approval + "\nNext task is pending.\n",
                              encoding="utf-8")
        result = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())

    def test_explicit_base_ref_movement_invalidates_lint(self) -> None:
        _ = self.git("tag", "guard-base", self.weak)
        self.environment["VERIFY_ASSERTION_BASE"] = "guard-base"
        self.cache_passing_lint()
        _ = self.git("tag", "-f", "guard-base", self.strong)
        self.assert_guard_reruns_and_blocks()

    def test_removed_session_ruling_invalidates_lint(self) -> None:
        _ = self.git("branch", "--set-upstream-to=origin/strict")
        ruling = Path(self.environment["UNIT_SESSION_DIR"]) / "approval.md"
        _ = ruling.write_text(
            "Assertion-Change: src/lib.rs::test_value — Accepted bound contract.\n", encoding="utf-8")
        self.cache_passing_lint()
        ruling.unlink()
        self.assert_guard_reruns_and_blocks()

    def test_run_folder_ruling_allows_removed_assertion_without_session_folder(self) -> None:
        run_folder = Path(self.environment.pop("UNIT_SESSION_DIR"))
        self.environment["UNIT_RUN_DIR"] = str(run_folder)
        _ = self.git("branch", "--set-upstream-to=origin/strict")
        _ = (self.root / "src/lib.rs").write_text("#[test]\nfn test_value() {}\n", encoding="utf-8")
        _ = (run_folder / "approval.md").write_text(
            "Assertion-Change: src/lib.rs::test_value — Removed redundant check.\n", encoding="utf-8")

        result = self.verify("lint", "sample")

        self.assertIn("ruled: Removed redundant check.", result.stdout)
        self.assertTrue(self.clippy_ran())

    def test_adding_run_folder_ruling_retries_cached_assertion_failure(self) -> None:
        run_folder = Path(self.environment.pop("UNIT_SESSION_DIR"))
        self.environment["UNIT_RUN_DIR"] = str(run_folder)
        _ = self.git("branch", "--set-upstream-to=origin/strict")
        _ = (self.root / "src/lib.rs").write_text("#[test]\nfn test_value() {}\n", encoding="utf-8")
        failed = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("test_value removed:", failed.stdout)
        cached = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(cached.returncode, 1, cached.stdout + cached.stderr)
        self.assertIn("FAIL (recorded)", cached.stdout)
        _ = (run_folder / "approval.md").write_text(
            "Assertion-Change: src/lib.rs::test_value — Removed redundant check.\n", encoding="utf-8")

        result = self.verify("lint", "sample")

        self.assertIn("ruled: Removed redundant check.", result.stdout)
        self.assertNotIn("FAIL (recorded)", result.stdout)
        self.assertNotIn("PASS (recorded)", result.stdout)
        self.assertTrue(self.clippy_ran())

    def test_commit_ruling_change_invalidates_lint_when_tree_is_identical(self) -> None:
        _ = self.git("branch", "--set-upstream-to=origin/strict")
        _ = self.git("commit", "-q", "--allow-empty", "-m",
                     "Assertion-Change: src/lib.rs::test_value — Accepted bound contract.")
        self.cache_passing_lint()
        _ = self.git("reset", "--soft", self.weak)
        self.assert_guard_reruns_and_blocks()

    def test_merge_other_parent_change_invalidates_lint_with_identical_tree(self) -> None:
        _ = self.git("branch", "--set-upstream-to=origin/strict")
        tree = self.git("rev-parse", self.weak + "^{tree}")
        other_strong = self.git("commit-tree", self.strong + "^{tree}", "-p", self.strong,
                                "-m", "other parent keeps strong assertions")
        inherited = self.git("commit-tree", tree, "-p", self.strong, "-p", self.weak,
                             "-m", "merge inherited assertion change")
        _ = self.git("reset", "--hard", inherited)
        self.cache_passing_lint()
        own_removal = self.git("commit-tree", tree, "-p", self.strong, "-p", other_strong,
                               "-m", "merge removes assertion against both parents")
        _ = self.git("reset", "--hard", own_removal)
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), tree)
        self.assert_guard_reruns_and_blocks()

    def test_fetching_a_contained_production_base_invalidates_cached_green_lint(self) -> None:
        strong_tree = self.git("rev-parse", self.strong + "^{tree}")
        weak_tree = self.git("rev-parse", self.weak + "^{tree}")
        incoming = self.git("commit-tree", strong_tree, "-p", self.strong,
                            "-m", "independent production work keeps strong assertion")
        merged = self.git("commit-tree", strong_tree, "-p", self.strong, "-p", incoming,
                          "-m", "merge production without changing assertions")
        previous_production = self.git("commit-tree", weak_tree, "-p", merged,
                                       "-m", "production accepts weaker assertion")
        production = self.git("commit-tree", strong_tree, "-p", previous_production,
                              "-m", "production restores strong assertion")
        head = self.git("commit-tree", weak_tree, "-p", production,
                        "-m", "unit weakens assertion after production")
        _ = self.git("reset", "--hard", head)
        _ = self.git("push", "-q", "origin", previous_production + ":refs/heads/builds")
        _ = self.git("push", "-q", "origin", production + ":refs/heads/incoming")
        _ = self.git("branch", "--set-upstream-to=origin/strict")
        registry = guard_support.GitRepositoryFixture()
        registry.root = self.root
        registry.repo = self.root
        registry.notifier_dir = self.root / "notifier"
        registry.notifier_dir.mkdir()
        _ = registry.register_production("builds", branch="feature")
        self.environment["NOTIFIER_STATE_DIR"] = str(registry.notifier_dir)
        self.cache_passing_lint()

        _ = self.git("--git-dir", str(self.root / "remote.git"),
                     "update-ref", "refs/heads/builds", production)
        _ = self.git("fetch", "-q", "origin")

        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), weak_tree)
        self.assert_guard_reruns_and_blocks()

    def test_guard_script_change_invalidates_lint(self) -> None:
        guard = self.root / "private-assertion-guard.py"
        _ = guard.write_text(support.VERIFY.with_name("assertion_guard.py").read_text(encoding="utf-8"),
                             encoding="utf-8")
        self.environment["VERIFY_ASSERTION_GUARD_SCRIPT"] = str(guard)
        self.cache_passing_lint()
        _ = guard.write_text("print('fixture: revised assertion guard')\nraise SystemExit(7)\n", encoding="utf-8")
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("fixture: revised assertion guard", result.stdout)
        self.assertNotIn("PASS (recorded)", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
