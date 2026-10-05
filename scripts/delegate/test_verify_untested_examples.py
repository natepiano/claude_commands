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
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
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
            "BUILDLOG_MEMINFO": str(meminfo),
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

    def metadata_count(self) -> int:
        return len(self.metadata_calls.read_text().splitlines()) if self.metadata_calls.exists() else 0

    def refusal(self, name: str, source: Path, line: int, attribute: str, package: str = "sample") -> str:
        return (
            f"example {name} holds a test: {source}:{line} has {attribute}, and examples carry no tests.\n"
            f"move it into {package}'s src/ or tests/, or delete it."
        )

    def assert_refused(
        self,
        result: subprocess.CompletedProcess[str],
        name: str,
        source: Path,
        line: int,
        attribute: str,
        *,
        package: str = "sample",
        metadata_count: int = 1,
    ) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, self.refusal(name, source, line, attribute, package) + "\n")
        self.assertNotIn("test = true", result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), metadata_count)

    def test_false_example_with_test_module_is_refused(self) -> None:
        _ = self.package(manifest_extra='\n[[example]]\nname = "demo"\ntest = false\n')
        example = self.example(source="fn main() {}\n\n#[cfg(test)]\nmod tests {}\n")

        self.assert_refused(self.verify("test", "sample"), "demo", example, 3, "#[cfg(test)]")

    def test_true_example_with_test_module_is_refused(self) -> None:
        _ = self.package(manifest_extra='\n[[example]]\nname = "demo"\ntest = true\n')
        example = self.example(source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")

        self.assert_refused(
            self.verify("test", "sample", "--features", "default"), "demo", example, 1, "#[cfg(test)]"
        )

    def test_plain_test_attribute_is_refused(self) -> None:
        _ = self.package()
        example = self.example(source="fn main() {}\n  #[test]  \nfn check() {}\n")

        self.assert_refused(self.verify("test", "sample"), "demo", example, 2, "#[test]")

    def test_namespaced_test_attribute_is_refused(self) -> None:
        _ = self.package()
        example = self.example(source="#[tokio::test]\nasync fn check() {}\nfn main() {}\n")

        self.assert_refused(self.verify("test", "sample"), "demo", example, 1, "#[tokio::test]")

    def test_directory_example_scans_module_files(self) -> None:
        _ = self.package()
        directory = self.root / "examples" / "demo"
        directory.mkdir(parents=True)
        _ = (directory / "main.rs").write_text("mod hidden;\nfn main() {}\n")
        module = directory / "hidden.rs"
        _ = module.write_text("// first line\n  #[cfg(test)]\nmod tests {}\n")

        self.assert_refused(self.verify("test", "sample"), "demo", module, 2, "#[cfg(test)]")

    def test_explicit_example_name_scans_its_directory(self) -> None:
        _ = self.package(
            manifest_extra='\n[[example]]\nname = "renamed"\npath = "examples/demo/main.rs"\n'
        )
        directory = self.root / "examples" / "demo"
        directory.mkdir(parents=True)
        _ = (directory / "main.rs").write_text("mod hidden;\nfn main() {}\n")
        module = directory / "hidden.rs"
        _ = module.write_text("// first line\n#[test]\nfn check() {}\n")

        self.assert_refused(self.verify("test", "sample"), "renamed", module, 2, "#[test]")

    def test_two_offenders_have_separate_blocks(self) -> None:
        _ = self.package()
        first = self.example("first", source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")
        second = self.example("second", source="fn main() {}\n#[test]\nfn check() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertCountEqual(
            result.stderr.rstrip("\n").split("\n\n"),
            [
                self.refusal("first", first, 1, "#[cfg(test)]"),
                self.refusal("second", second, 2, "#[test]"),
            ],
        )
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 1)

    def test_commented_attribute_is_not_an_offender(self) -> None:
        _ = self.package()
        _ = self.example(source="// #[cfg(test)]\nfn main() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.calls_made()))
        self.assertEqual(self.metadata_count(), 1)

    def test_clean_package_passes_gate(self) -> None:
        _ = self.package()
        _ = self.example(source="fn main() {}\n")

        result = self.verify("test", "sample", "--features", "default")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.calls_made()))
        self.assertEqual(self.metadata_count(), 1)

    def test_filter_and_named_target_skip_gate(self) -> None:
        _ = self.package()
        _ = self.example(source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")
        integration = self.root / "tests" / "smoke.rs"
        integration.parent.mkdir()
        _ = integration.write_text("#[test]\nfn smoke() {}\n")

        for args in (("test", "sample", "--filter", "smoke"), ("test", "sample", "smoke")):
            with self.subTest(args=args):
                result = self.verify(*args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("holds a test", result.stdout + result.stderr)
        self.assertEqual(sum(call.startswith("nextest run ") for call in self.calls_made()), 2)

    def test_recorded_pass_does_not_hide_new_test(self) -> None:
        _ = self.package()
        example = self.example(source="fn main() {}\n")
        first = self.verify("test", "sample")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(len(list((self.root / "session" / "verify_cache").glob("*.pass"))), 1)
        first_calls = self.calls_made()
        _ = example.write_text("#[cfg(test)]\nmod tests {}\nfn main() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, self.refusal("demo", example, 1, "#[cfg(test)]") + "\n")
        self.assertNotIn("PASS (recorded)", result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), first_calls)
        self.assertEqual(self.metadata_count(), 2)

    def test_final_checks_every_workspace_member_before_format(self) -> None:
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers = ["alpha", "beta"]\nresolver = "2"\n')
        alpha_root = self.root / "alpha"
        beta_root = self.root / "beta"
        _ = self.package("alpha", root=alpha_root)
        _ = self.package("beta", root=beta_root)
        alpha = self.example("first", root=alpha_root, source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")
        beta = self.example("second", root=beta_root, source="fn main() {}\n#[test]\nfn check() {}\n")

        result = self.verify("final")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertCountEqual(
            result.stderr.rstrip("\n").split("\n\n"),
            [
                self.refusal("first", alpha, 1, "#[cfg(test)]", package="alpha"),
                self.refusal("second", beta, 2, "#[test]", package="beta"),
            ],
        )
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 1)

    def test_removed_example_test_runs_no_cargo(self) -> None:
        _ = self.package()
        _ = self.example(source="fn main() {}\n")

        result = self.verify("example-test", "sample", "demo")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(
            result.stderr,
            "verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).\n",
        )
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 0)


if __name__ == "__main__":
    _ = unittest.main()
