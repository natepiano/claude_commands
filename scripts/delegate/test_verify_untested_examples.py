#!/usr/bin/env python3
"""Exercise verify.sh's example gate through real command routing and metadata."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override


VERIFY = Path(__file__).with_name("verify.sh")
REAL_CARGO = shutil.which("cargo")


class UntestedExampleTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    calls: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    metadata_calls: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.assertIsNotNone(REAL_CARGO, "cargo is required for real metadata")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.calls = self.root / "cargo-calls"
        self.metadata_calls = self.root / "metadata-calls"
        stubs = self.root / "stubs"
        stubs.mkdir()
        cargo = stubs / "cargo"
        _ = cargo.write_text(r'''#!/bin/sh
if [ "$1" = metadata ]; then
    printf "metadata\n" >> "$METADATA_CALLS"
    exec "$REAL_CARGO" "$@"
fi
printf "%s\n" "$*" >> "$CARGO_CALLS"
case "$1 $2" in
    "nextest --version"|"nextest run"|"+nightly fmt") exit 0 ;;
    *) exit 97 ;;
esac
''')
        cargo.chmod(0o755)
        git = stubs / "git"
        _ = git.write_text(r'''#!/bin/sh
case " $* " in
    *"rev-parse --show-toplevel"*) printf "%s\n" "$PACKAGE_ROOT" ;;
    *"rev-parse HEAD^{tree}"*) printf "fixed-tree\n" ;;
    *"status --porcelain"*) : ;;
    *) exit 1 ;;
esac
''')
        git.chmod(0o755)
        reader = self.root / "lint-config.sh"
        _ = reader.write_text('lint_config_enabled() { [ "$1" != sweep ]; }\n'
                              + 'lint_config_skip_notice() { :; }\n')
        for name in ("tmp", "runtime", "state", "cache", "session", "target", "cgroup"):
            (self.root / name).mkdir()
        self.environment = {
            **{
                name: value for name, value in os.environ.items()
                if name not in {"PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_TEAM_ROLE"}
            },
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "REAL_CARGO": str(REAL_CARGO),
            "PACKAGE_ROOT": str(self.root),
            "CARGO_CALLS": str(self.calls),
            "METADATA_CALLS": str(self.metadata_calls),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "LINT_CONFIG_READER": str(reader),
            "LINT_CONFIG_FILE": str(reader),
            "PLAN_DELEGATE_SESSION_DIR": str(self.root / "session"),
        }

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def package(self, name: str = "sample", *, manifest_extra: str = "", root: Path | None = None) -> Path:
        package_root = root or self.root
        package_root.mkdir(parents=True, exist_ok=True)
        manifest = package_root / "Cargo.toml"
        _ = manifest.write_text(
            f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
            + manifest_extra
        )
        source = package_root / "src" / "lib.rs"
        source.parent.mkdir(exist_ok=True)
        _ = source.write_text("pub fn value() -> u8 { 1 }\n")
        return manifest

    def example(self, name: str = "demo", *, root: Path | None = None, source: str) -> Path:
        package_root = root or self.root
        example = package_root / "examples" / f"{name}.rs"
        example.parent.mkdir(exist_ok=True)
        _ = example.write_text(source)
        return example

    def verify(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(VERIFY), *args],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def calls_made(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def assert_refused(self, result: subprocess.CompletedProcess[str], name: str, source: Path, line: int) -> str:
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn(
            f"example {name} holds tests that never run: {source}:{line} has #[cfg(test)], "
            + "and cargo builds examples with test = false.",
            output,
        )
        self.assertEqual(self.calls_made(), [], output)
        self.assertEqual(self.metadata_calls.read_text().splitlines(), ["metadata"])
        return output

    def test_example_without_manifest_entry_is_refused_before_build_or_format(self) -> None:
        manifest = self.package()
        example = self.example(source="fn main() {}\n\n#[cfg(test)]\nmod tests {}\n")

        output = self.assert_refused(self.verify("test", "sample"), "demo", example, 3)

        self.assertIn(f'in {manifest}:\n[[example]]\nname = "demo"\ntest = true', output)

    def test_existing_manifest_entry_gets_its_one_line_fix(self) -> None:
        manifest = self.package(manifest_extra='\n[[example]]\nname = "demo"\npath = "examples/demo.rs"\n')
        example = self.example(source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")

        output = self.assert_refused(self.verify("test", "sample"), "demo", example, 1)

        self.assertIn(f"in {manifest}: add test = true to its [[example]] entry", output)
        self.assertNotIn('name = "demo"\ntest = true', output)

    def test_directory_example_scans_module_files(self) -> None:
        _ = self.package()
        directory = self.root / "examples" / "demo"
        directory.mkdir(parents=True)
        _ = (directory / "main.rs").write_text("mod hidden;\nfn main() {}\n")
        module = directory / "hidden.rs"
        _ = module.write_text("// first line\n  #[cfg(test)]\nmod tests {}\n")

        _ = self.assert_refused(self.verify("test", "sample"), "demo", module, 2)

    def test_enabled_example_tests_pass_the_gate(self) -> None:
        _ = self.package(manifest_extra='\n[[example]]\nname = "demo"\ntest = true\n')
        _ = self.example(source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.calls_made()))
        self.assertEqual(self.metadata_calls.read_text().splitlines(), ["metadata"])

    def test_example_without_inline_tests_passes_the_gate(self) -> None:
        _ = self.package()
        _ = self.example(source="fn main() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.calls_made()))

    def test_filter_and_named_integration_target_are_feedback_runs(self) -> None:
        _ = self.package()
        _ = self.example(source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")
        integration = self.root / "tests" / "smoke.rs"
        integration.parent.mkdir()
        _ = integration.write_text("#[test]\nfn smoke() {}\n")

        for args in (("test", "sample", "--filter", "smoke"), ("test", "sample", "smoke")):
            with self.subTest(args=args):
                result = self.verify(*args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("holds tests that never run", result.stdout + result.stderr)
        self.assertEqual(sum(call.startswith("nextest run ") for call in self.calls_made()), 2)

    def test_cached_pass_does_not_hide_new_inline_tests(self) -> None:
        _ = self.package()
        example = self.example(source="fn main() {}\n")
        first = self.verify("test", "sample")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(len(list((self.root / "session" / "verify_cache").glob("*.pass"))), 1)
        first_calls = self.calls_made()
        _ = example.write_text("#[cfg(test)]\nmod tests {}\nfn main() {}\n")

        result = self.verify("test", "sample")

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn(
            f"example demo holds tests that never run: {example}:1 has #[cfg(test)], "
            + "and cargo builds examples with test = false.",
            output,
        )
        self.assertNotIn("PASS (recorded)", output)
        self.assertEqual(self.calls_made(), first_calls)
        self.assertEqual(self.metadata_calls.read_text().splitlines(), ["metadata", "metadata"])

    def test_final_lists_each_workspace_members_offender_before_format(self) -> None:
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers = ["alpha", "beta"]\nresolver = "2"\n')
        alpha_root = self.root / "alpha"
        beta_root = self.root / "beta"
        alpha_manifest = self.package("alpha", root=alpha_root)
        beta_manifest = self.package("beta", root=beta_root)
        alpha = self.example("first", root=alpha_root, source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")
        beta = self.example("second", root=beta_root, source="fn main() {}\n#[cfg(test)]\nmod tests {}\n")

        result = self.verify("final")
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 2, output)
        for name, source, line, manifest in (
            ("first", alpha, 1, alpha_manifest),
            ("second", beta, 2, beta_manifest),
        ):
            self.assertIn(
                f"example {name} holds tests that never run: {source}:{line} has #[cfg(test)], "
                + "and cargo builds examples with test = false.",
                output,
            )
            self.assertIn(f'in {manifest}:\n[[example]]\nname = "{name}"\ntest = true', output)
        self.assertIn(
            'name = "first"\ntest = true\n\nexample second holds tests that never run:',
            output,
        )
        self.assertEqual(self.calls_made(), [], output)
        self.assertEqual(self.metadata_calls.read_text().splitlines(), ["metadata"])


if __name__ == "__main__":
    _ = unittest.main()
