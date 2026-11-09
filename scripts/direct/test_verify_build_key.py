"""Exercise test build input keys with private Git refs."""

from __future__ import annotations

import os
import json
import shlex
import subprocess
import sys
import unittest
from typing import cast, override

from scripts.direct import verify_lint_scope_test_support as support
from scripts.lib.shell_test_support import shell_function
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyBuildKeyTests(support.VerifyLintScopeFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        (self.root / "bin/git").unlink()
        (self.root / ".git").rmdir()
        self.environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        _ = self.git("init", "-q", "-b", "feature")
        # Every changing tool log/cache/home file belongs to this fixture, not
        # the Rust tree. Only the explicitly added source files affect its key.
        _ = (self.root / ".gitignore").write_text("*\n", encoding="utf-8")
        _ = (self.root / "src/lib.rs").write_text(
            "#[test]\nfn test_value() {\n    assert_eq!(actual, expected);\n}\n", encoding="utf-8")
        _ = self.git("add", "-f", ".gitignore", "Cargo.toml", "src/lib.rs")
        _ = self.git("commit", "-q", "-m", "fixture source")
        _ = self.enable_cache()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=fixture", "-c", "user.email=fixture@test",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments],
            env=self.environment, capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
        )
        return result.stdout.strip()

    def calculate_build_keys(self, *arguments: str, extra_environment: dict[str, str] | None = None,
                        target: str = "", command: str = "test") -> tuple[str, str, str]:
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
             str(support.SCRIPTS / "lint/invoke.sh"), str(self.config), "--", command, "sample", *arguments],
            input=json.dumps(metadata), cwd=self.root,
            env={**self.environment, "TEST_METADATA": json.dumps(metadata), "VERIFY_COMPUTE_TEST_BUILD": "1",
                 "VERIFY_TEST_BUILD_INPUTS_FILE": str(output), **(extra_environment or {})},
            capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
        )
        lines = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^[0-9a-f]{64}$")
        self.assertEqual(lines[2], lines[1])
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

    def test_result_environment_changes_verdicts_without_rebuilding(self) -> None:
        _ = self.environment.pop("NO_COLOR", None)
        verdict, build, _ = self.calculate_build_keys()
        for name in ("NO_COLOR", "CLICOLOR", "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LC_MESSAGES",
                     "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "LC_ADDRESS", "LC_IDENTIFICATION",
                     "LC_MEASUREMENT", "LC_NAME", "LC_PAPER", "LC_TELEPHONE", "HANA_TEST_MODE", "WGPU_BACKEND",
                     "BEVY_TEST_MODE", "INSTA_UPDATE", "TRACE_MODE"):
            with self.subTest(name=name):
                changed, same_build, _ = self.calculate_build_keys(extra_environment={name: "fixture-value"})
                self.assertNotEqual(verdict, changed)
                self.assertEqual(build, same_build)
        empty, same_build, _ = self.calculate_build_keys(extra_environment={"NO_COLOR": ""})
        self.assertNotEqual(verdict, empty)
        self.assertEqual(build, same_build)
        forwarded, forwarded_build, _ = self.calculate_build_keys("--env", "SNAPSHOT", extra_environment={"SNAPSHOT": "one"})
        changed, changed_build, _ = self.calculate_build_keys("--env", "SNAPSHOT", extra_environment={"SNAPSHOT": "two"})
        self.assertNotEqual(forwarded, changed)
        self.assertEqual(forwarded_build, changed_build)
        self.assertEqual(build, forwarded_build)

    def test_check_key_ignores_test_result_environment(self) -> None:
        _ = self.environment.pop("NO_COLOR", None)
        verdict, build, _ = self.calculate_build_keys(command="check")
        unchanged, same_build, _ = self.calculate_build_keys(command="check", extra_environment={"NO_COLOR": "1"})
        self.assertEqual(verdict, unchanged)
        self.assertEqual(build, same_build)

    def test_fixed_terminal_configuration_changes_only_the_verdict_key(self) -> None:
        verdict, build, _ = self.calculate_build_keys()
        unchanged, same_build, _ = self.calculate_build_keys(extra_environment={
            "TERM": "other", "COLORTERM": "other", "TERM_PROGRAM": "other", "COLUMNS": "123", "LINES": "77",
            "LC_TERMINAL": "other", "LC_TERMINAL_VERSION": "other",
        })
        self.assertEqual(verdict, unchanged)
        self.assertEqual(build, same_build)
        configuration = self.config.parent / "direct.conf"
        _ = configuration.write_text(configuration.read_text().replace("TERM_PROGRAM=verify", "TERM_PROGRAM=fixture"))
        changed, same_build, _ = self.calculate_build_keys()
        self.assertNotEqual(verdict, changed)
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
        _ = probe.write_text(f"""import json, os
from datetime import UTC, datetime
from pathlib import Path
with open({str(support.SCRIPTS / 'lib/package_root.py')!r}) as package_root: exec(package_root.read(), {{"__file__": package_root.name}})
from scripts.build_queue import step_estimator
from scripts.buildlog import store
from scripts.lib.cargo_output import CargoOutputRoots
output_roots = CargoOutputRoots(Path.cwd(), Path(os.environ['BUILDLOG_TEST_TARGET_DIR']),
    Path(os.environ['BUILDLOG_TEST_BUILD_DIR']))
fresh = step_estimator.is_fresh_test(Path(os.environ['BUILDLOG_DIR']), store.host_name(),
    os.getcwd(), 'sample', 'filtered', os.environ['BUILDLOG_TEST_BUILD_KEY'],
    datetime.now(UTC), output_roots)
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
  VERIFY_TEST_BUILD_INPUTS_FILE="$HOLD_WAIT_DIR/test-build-inputs" "$PY" "$TREE_WATCH_SCRIPT" --key "$@" --package sample "$VERIFY" "$HOME/.claude/scripts/lint/invoke.sh" "$LINT_CONFIG_FILE" -- test sample --filter native_rate <<< "$TEST_METADATA"
}
build_hold_wait_local() {
  printf "// edited during hold\n" >> src/lib.rs
  : > "$PWD/hold-released"
}
build_hold_applies() { return 0; }
build_hold_nothing_held() { test -f "$PWD/hold-released"; }
step_state() { :; }
''',
            shell_function(source, "set_verdict_keys"), hold_wrapper, key_setup,
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


if __name__ == "__main__":
    _ = unittest.main()
