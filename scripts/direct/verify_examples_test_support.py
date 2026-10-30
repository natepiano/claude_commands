"""Private Cargo workspaces and seeded tools for verify example and pass tests."""

from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import ClassVar, cast, override

from scripts.direct.verify_mac_offload_test_support import stub_build_tool_path
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


VERIFY = Path(__file__).with_name("verify.sh")
FIXED_RUSTC = "rustc 1.90.0 (1159e78c4 2025-09-14)"


def rust_tool_path(directory: Path) -> str:
    """Allow version reads; all other Rust tool calls must have a test stub."""
    directory.mkdir(parents=True, exist_ok=True)
    tool_path = stub_build_tool_path(directory)
    compiler = directory / "rustc"
    _ = compiler.write_text(
        '#!/bin/sh\n'
        + 'case "$*" in\n'
        + f'  -V|"+nightly -V") printf "%s\\n" "{FIXED_RUSTC}" ;;\n'
        + f'  -vV) printf "%s\\nhost: x86_64-unknown-linux-gnu\\n" "{FIXED_RUSTC}" ;;\n'
        + '  *) echo "Unexpected rustc call: $*" >&2; exit 99 ;;\n'
        + 'esac\n'
    )
    compiler.chmod(0o755)
    return tool_path


def workspace_metadata(root: Path) -> dict[str, object]:
    """Metadata for the small manifests and source files made by these tests."""
    manifest = cast(dict[str, object], tomllib.loads((root / "Cargo.toml").read_text()))
    workspace = cast(dict[str, object], manifest.get("workspace", {}))
    manifests = [root / "Cargo.toml"] if "package" in manifest else []
    manifests += [root / member / "Cargo.toml" for member in cast(list[str], workspace.get("members", []))]
    packages: list[dict[str, object]] = []
    for path in manifests:
        fields = cast(dict[str, object], tomllib.loads(path.read_text()))
        package = cast(dict[str, object], fields["package"])
        name = str(package["name"])
        folder = path.parent
        targets: list[dict[str, object]] = []
        for source, kind, target_name in ((folder / "src/lib.rs", "lib", name),
                                          (folder / "src/main.rs", "bin", name)):
            if source.exists():
                targets.append({"name": target_name, "kind": [kind], "src_path": str(source), "test": True})
        examples = cast(list[dict[str, object]], fields.get("example", []))
        named_examples = {str(example["name"]) for example in examples}
        for source in sorted((folder / "examples").glob("*.rs")):
            if source.stem not in named_examples:
                examples.append({"name": source.stem, "path": str(source.relative_to(folder))})
        for source in sorted((folder / "examples").glob("*/main.rs")):
            has_path = any(example.get("path") == str(source.relative_to(folder)) for example in examples)
            if source.parent.name not in named_examples and not has_path:
                examples.append({"name": source.parent.name, "path": str(source.relative_to(folder))})
        for example in examples:
            source = folder / str(example.get("path", f"examples/{example['name']}.rs"))
            targets.append({"name": example["name"], "kind": ["example"], "src_path": str(source),
                            "test": example.get("test", False)})
        for source in sorted((folder / "tests").glob("*.rs")):
            targets.append({"name": source.stem, "kind": ["test"], "src_path": str(source), "test": True})
        dependencies: list[dict[str, object]] = []
        for dependency, value in cast(dict[str, object], fields.get("dependencies", {})).items():
            if isinstance(value, dict):
                detail = cast(dict[str, object], value)
                if "path" in detail:
                    dependencies.append({"name": dependency, "path": str((folder / str(detail["path"])).resolve())})
        packages.append({"name": name, "id": name, "manifest_path": str(path), "targets": targets,
                         "features": {}, "dependencies": dependencies})
    return {"workspace_root": str(root), "target_directory": str(root / "target"),
            "workspace_members": [package["id"] for package in packages], "packages": packages}


class ExampleWorkspaceFixture(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    calls: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    metadata_calls: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.calls = self.root / "cargo-calls"
        self.metadata_calls = self.root / "metadata-calls"
        stubs = self.root / "stubs"
        tool_path = rust_tool_path(stubs)
        cargo = stubs / "cargo"
        _ = cargo.write_text(r'''#!/bin/sh
if [ "$1" = metadata ]; then
    printf "metadata\n" >> "$METADATA_CALLS"
    [ "$*" = "metadata --no-deps --format-version 1" ] || { echo "Unexpected cargo metadata call: $*" >&2; exit 99; }
    printf "%s\n" "$TEST_METADATA"
    exit 0
fi
printf "%s\n" "$*" >> "$CARGO_CALLS"
if [ "$1 $2" = "nextest run" ]; then
    case " $* " in
        *" --lib "*)
            python3 - <<'PY' || exit $?
import json
import os
import sys

metadata = json.loads(os.environ["TEST_METADATA"])
members = [package for package in metadata["packages"] if package["id"] in metadata["workspace_members"]]
if not any(
    set(target["kind"]) & {"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"}
    for package in members for target in package["targets"]
):
    print(f"error: no library targets found in package `{members[0]['name']}`", file=sys.stderr)
    sys.exit(101)
PY
            ;;
    esac
fi
if [ "$1 $2" = "nextest run" ] && [ -n "$EDIT_DURING_TEST" ]; then
    printf "// changed during the test run\n" >> "$PACKAGE_ROOT/${EDIT_DURING_TEST_PATH:-src/lib.rs}"
fi
case "$1 $2" in
    "check --workspace") exit "$FAKE_CHECK_EXIT" ;;
    "nextest --version"|"nextest run"|"+nightly fmt") exit 0 ;;
    *) echo "Unexpected cargo call: $*" >&2; exit 99 ;;
esac
''')
        cargo.chmod(0o755)
        git = stubs / "git"
        git.unlink()
        _ = git.write_text(r'''#!/bin/sh
case " $* " in
    *"rev-parse --show-toplevel"*) printf "%s\n" "$PACKAGE_ROOT" ;;
    *"rev-parse HEAD^{tree}"*) printf "fixed-tree\n" ;;
    *"status --porcelain"*) [ -z "$TREE_DIRTY" ] || printf " M %s\000" ${TREE_DIRTY_PATHS:-src/lib.rs} ;;
    *" ls-tree "*) : ;;
    *) exit 1 ;;
esac
''')
        git.chmod(0o755)
        reader = self.root / "lint-config.sh"
        _ = reader.write_text('lint_config_enabled() { [ "$1" != sweep ]; }\n'
                              + 'lint_config_skip_notice() { :; }\n')
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        claude = self.root / ".claude"
        claude.mkdir()
        (claude / "scripts").symlink_to(VERIFY.parent.parent, target_is_directory=True)
        for name in ("tmp", "runtime", "state", "cache", "session", "target", "cgroup"):
            (self.root / name).mkdir()
        # This workspace's memory fixture must not inherit the host's PSI.
        pressure = self.root / "memory-pressure"
        _ = pressure.write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.environment = {
            **{
                name: value for name, value in os.environ.items()
                if name not in {"UNIT_RUN_DIR", "UNIT_TEAM_ROLE", "CODEX_THREAD_ID",
                                "CODEX_SESSION_ID", "UNIT_DETACHED_RUN"}
            },
            "PATH": tool_path,
            "HOME": str(self.root),
            "RUSTUP_HOME": str(self.root / "rustup"),
            "PACKAGE_ROOT": str(self.root),
            "CARGO_CALLS": str(self.calls),
            "METADATA_CALLS": str(self.metadata_calls),
            "FAKE_CHECK_EXIT": "0",
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "VERIFY_STEPS_DIR": str(self.root / "verify-steps"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_MEMORY_PRESSURE": str(pressure),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_RUN_DIR": "",
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILD_TARGETS_SCRIPT": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_ADMISSION_FILE": "",
            "BUILDLOG_HOLD_STARTED_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_HOLD_REGISTRATION_FILE": str(self.root / "registration"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "VERIFY_MAC_RUNNER": str(self.root / "no-mac-runner"),
            "LINT_CONFIG_READER": str(reader),
            "LINT_CONFIG_FILE": str(reader),
            "UNIT_SESSION_DIR": str(self.root / "session"),
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
        self.environment["TEST_METADATA"] = json.dumps(workspace_metadata(self.root))
        return subprocess.run(
            ["bash", str(VERIFY), *args],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=TEST_HANG_GUARD_SECS,
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

    def enabled_example_refusal(self, name: str, manifest: Path) -> str:
        return (
            f"example {name} sets test = true in {manifest}, and examples carry no tests.\n"
            "remove test = true from its [[example]] entry."
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


class SeededExampleWorkspaceFixture(ExampleWorkspaceFixture):
    """Restore one private tool seed at its original paths for each example test."""

    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]
    seeded: bool = False
    root: Path = Path()
    calls: Path = Path()
    metadata_calls: Path = Path()
    environment: dict[str, str] = {}

    @classmethod
    @override
    def setUpClass(cls) -> None:
        builder = ExampleWorkspaceFixture()
        builder.setUp()
        cls.addClassCleanup(builder.tearDown)
        cls.fixture_root = builder.root
        seed = cls.enterClassContext(tempfile.TemporaryDirectory(prefix="example-workspace-seed-"))
        cls.fixture_seed = Path(seed) / "seed"
        cls.fixture_environment = builder.environment.copy()
        # The fixture supplies the entire toolchain and 64 GiB of free memory.
        # Host Nix discovery and Python version probing are separate contracts.
        cls.fixture_environment.update({"IN_NIX_SHELL": "pure", "BUILDLOG_BUILD_BUDGET_GIB": "64"})
        _ = cls.fixture_environment.pop("CLAUDE_PYTHON", None)
        cache = builder.root / "cache/claude/python3-3.11"
        cache.parent.mkdir(parents=True)
        _ = cache.write_text(sys.executable + "\n")
        _ = shutil.copytree(builder.root, cls.fixture_seed, symlinks=True)
        shutil.rmtree(builder.root)

    @override
    def setUp(self) -> None:
        # Some existing suites create an UntestedExampleTests instance directly.
        # They keep the original standalone lifecycle and environment.
        self.seeded = hasattr(type(self), "fixture_seed") and self.fixture_seed.exists()
        if not self.seeded:
            super().setUp()
            return
        self.root = self.fixture_root
        _ = shutil.copytree(self.fixture_seed, self.root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.calls = self.root / "cargo-calls"
        self.metadata_calls = self.root / "metadata-calls"
        self.environment = self.fixture_environment.copy()

    @override
    def tearDown(self) -> None:
        if not self.seeded:
            super().tearDown()

    def side_by_side_phases(self, edited: str) -> subprocess.CompletedProcess[str]:
        """Test alpha, which depends on gamma, while another phase edits `edited`."""
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers = ["alpha", "beta", "gamma"]\nresolver = "2"\n')
        _ = self.package("alpha", root=self.root / "alpha",
                         manifest_extra='\n[dependencies]\ngamma = { path = "../gamma" }\n')
        _ = self.package("beta", root=self.root / "beta")
        _ = self.package("gamma", root=self.root / "gamma")
        self.environment.update({
            "TREE_DIRTY": "1", "TREE_DIRTY_PATHS": "beta/src/lib.rs gamma/src/lib.rs",
            "EDIT_DURING_TEST": "1", "EDIT_DURING_TEST_PATH": edited,
        })
        return self.verify("test", "alpha")
