#!/usr/bin/env python3
"""Check delegate lint scope with synthetic Cargo metadata and real lint policy."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import TypedDict, cast, final, override
import unittest


VERIFY = Path(__file__).with_name("verify.sh")
SCRIPTS = VERIFY.parent.parent


class CargoInvocation(TypedDict):
    args: list[str]


class TaploInvocation(TypedDict):
    args: list[str]
    cwd: str


CargoTargetMetadata = TypedDict("CargoTargetMetadata", {
    "name": str,
    "kind": list[str],
    "test": bool,
    "src_path": str,
    "required-features": list[str],
})


class CargoPackageMetadata(TypedDict):
    name: str
    id: str
    features: dict[str, list[str]]
    manifest_path: str
    targets: list[CargoTargetMetadata]


class CargoWorkspaceMetadata(TypedDict):
    workspace_root: str
    workspace_members: list[str]
    packages: list[CargoPackageMetadata]


CARGO_STUB = r'''
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
with Path(os.environ["TEST_CARGO_EVENTS"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps({"args": arguments}) + "\n")
if arguments[:1] == ["metadata"]:
    print(os.environ["TEST_METADATA"])
elif arguments[:1] not in (["mend"], ["clippy"], ["doc"], ["+nightly"]):
    raise SystemExit("unexpected cargo invocation: " + repr(arguments))
# A file changes while this stage runs: mend's own rewrite, or a writer's edit.
if arguments[:1] == [os.environ.get("TEST_EDIT_DURING", "")]:
    with Path(os.environ["PACKAGE_ROOT"], "src", "lib.rs").open("a", encoding="utf-8") as source:
        source.write("// changed during " + arguments[0] + "\n")
if arguments[:1] == ["clippy"]:
    raise SystemExit(int(os.environ.get("TEST_CLIPPY_STATUS", "0")))
'''


TAPLO_STUB = r'''
import json
import os
from pathlib import Path
import sys

with Path(os.environ["TEST_TAPLO_EVENTS"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps({"args": sys.argv[1:], "cwd": str(Path.cwd())}) + "\n")
status = int(os.environ["TEST_TAPLO_STATUS"])
if status == 101:
    print("thread 'main' panicked", file=sys.stderr)
elif status:
    print("error: Cargo.toml is not formatted", file=sys.stderr)
raise SystemExit(status)
'''


GIT_STUB = r'''
import os
import sys

arguments = sys.argv[1:]
if "rev-parse" in arguments:
    if "--show-toplevel" in arguments:
        if "--git-common-dir" in arguments:
            print(os.environ["PACKAGE_ROOT"] + "/.git")
        print(os.environ["PACKAGE_ROOT"])
        if "--abbrev-ref" in arguments:
            print("main")
    elif "HEAD^{tree}" in arguments:
        print("fixed-tree")
    elif "--short" in arguments:
        print("abc1234")
    else:
        raise SystemExit(1)
elif "diff" in arguments and "--name-only" in arguments:
    sys.stdout.write("src/lib.rs\0")
elif "status" in arguments:
    if os.environ.get("TEST_GIT_DIRTY"):
        sys.stdout.write(" M src/lib.rs\0")
elif "ls-files" not in arguments:
    raise SystemExit(1)
'''


@final
class VerifyLintScopeTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.config = Path()
        self.events = Path()
        self.taplo_events = Path()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = self.root / "home"
        claude = home / ".claude"
        claude.mkdir(parents=True)
        (claude / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        self.events = self.root / "cargo-events.jsonl"
        self.taplo_events = self.root / "taplo-events.jsonl"
        self.config = self.root / "lint.conf"
        self.write_config(clippy="on")
        self.write_executable(bin_directory / "cargo", CARGO_STUB)
        self.write_executable(bin_directory / "taplo", TAPLO_STUB)
        self.write_executable(bin_directory / "git", GIT_STUB)
        self.write_executable(bin_directory / "rustc", 'print("rustc fixture")\n')
        targets = self.root / "build-targets.py"
        self.write_executable(targets, "raise SystemExit(0)\n")
        for name in ("ssh", "rsync", "scp", "gh", "systemd-run"):
            self.write_executable(bin_directory / name, "raise SystemExit(99)\n")
        for directory in ("tmp", "state", "cache", "runtime", "target", "cargo-home", ".git"):
            (self.root / directory).mkdir()
        _ = (self.root / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n',
            encoding="utf-8",
        )
        _ = (self.root / "taplo.toml").write_text(
            'include = ["**/*.toml"]\n', encoding="utf-8",
        )
        source = self.root / "src" / "lib.rs"
        source.parent.mkdir()
        _ = source.write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
        metadata: CargoWorkspaceMetadata = {
            "workspace_root": str(self.root),
            "workspace_members": ["sample-id"],
            "packages": [{
                "name": "sample",
                "id": "sample-id",
                "features": {"default": [], "a": []},
                "manifest_path": str(self.root / "Cargo.toml"),
                "targets": [
                    {"name": "sample", "kind": ["lib"], "test": True,
                     "src_path": str(source), "required-features": []},
                    {"name": "plain", "kind": ["example"], "test": False,
                     "src_path": str(self.root / "examples" / "plain.rs"),
                     "required-features": []},
                    {"name": "featured", "kind": ["example"], "test": False,
                     "src_path": str(self.root / "examples" / "featured.rs"),
                     "required-features": ["a"]},
                ],
            }],
        }
        self.environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith(("BUILD", "PLAN_DELEGATE_", "LINT_", "MAC_TEST_",
                                    "CARGO_", "RUST", "NEXTEST_", "CLAUDE_CODE_", "CODEX_"))
        }
        self.environment.update({
            "HOME": str(home),
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PACKAGE_ROOT": str(self.root),
            "TEST_CARGO_EVENTS": str(self.events),
            "TEST_TAPLO_EVENTS": str(self.taplo_events),
            "TEST_TAPLO_STATUS": "0",
            "TEST_METADATA": json.dumps(metadata),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILDLOG_MEMINFO": str(self.root / "missing-meminfo"),
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "BUILD_TARGETS_SCRIPT": str(targets),
            "LINT_CONFIG_READER": str(SCRIPTS / "lint" / "lint_config.sh"),
            "LINT_CONFIG_FILE": str(self.config),
        })

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def write_config(self, *, clippy: str) -> None:
        _ = self.config.write_text(
            f"mend=on\nfmt=on\nclippy={clippy}\ndoc=on\nsweep=off\n",
            encoding="utf-8",
        )

    def enable_cache(self) -> Path:
        session = self.root / "session"
        session.mkdir()
        self.environment["PLAN_DELEGATE_SESSION_DIR"] = str(session)
        return session / "verify_cache"

    def isolate_path_without_taplo(self) -> None:
        tools = self.root / "tools"
        tools.mkdir()
        for name in (
            "bash", "python3", "awk", "cat", "cut", "date", "dirname", "env", "grep",
            "head", "mkdir", "mktemp", "mv", "ps", "rm", "sed", "sleep",
            "sort", "tail", "tee", "tr", "uname", "wc",
        ):
            executable = shutil.which(name)
            if executable is None:
                self.fail(f"fixture requires {name}")
            (tools / name).symlink_to(executable)
        (self.root / "bin" / "taplo").unlink()
        self.environment["PATH"] = f"{self.root / 'bin'}{os.pathsep}{tools}"

    def verify(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = self.invoke_verify(*arguments, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def invoke_verify(
        self, *arguments: str, cwd: Path, taplo_status: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        self.events.unlink(missing_ok=True)
        self.taplo_events.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", str(VERIFY), *arguments], cwd=cwd,
            env={**self.environment, "TEST_TAPLO_STATUS": str(taplo_status)},
            capture_output=True, text=True, check=False, timeout=30,
        )

    def taplo_calls(self) -> list[TaploInvocation]:
        if not self.taplo_events.exists():
            return []
        return [cast(TaploInvocation, json.loads(line))
                for line in self.taplo_events.read_text(encoding="utf-8").splitlines()]

    def assert_workspace_taplo_check(self) -> None:
        self.assertEqual(self.taplo_calls(), [{"args": ["fmt", "--check"], "cwd": str(self.root)}])

    def calls(self, command: str) -> list[list[str]]:
        events = [cast(CargoInvocation, json.loads(line))
                  for line in self.events.read_text(encoding="utf-8").splitlines()]
        return [event["args"][1:] for event in events if event["args"][:1] == [command]]

    def test_lint_matches_ci_and_preserves_other_stages(self) -> None:
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(self.calls("clippy"), [
            ["--workspace", "--all-targets", "--all-features", "--", "-D", "warnings"]
        ])
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "-p", "sample"]])
        self.assertEqual(self.calls("doc"), [["--no-deps", "--jobs", "8", "-p", "sample"]])

    def test_features_reach_mend_and_doc_but_do_not_narrow_clippy(self) -> None:
        _ = self.verify("lint", "sample", "--features", "a")
        self.assert_workspace_taplo_check()
        self.assertEqual(self.calls("clippy"), [
            ["--workspace", "--all-targets", "--all-features", "--", "-D", "warnings"]
        ])
        self.assertEqual(self.calls("mend"), [[
            "--all-targets", "--workspace", "--fix", "--features", "sample/a"
        ]])
        self.assertEqual(self.calls("doc"), [[
            "--no-deps", "--jobs", "8", "-p", "sample", "--features", "sample/a"
        ]])
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "-p", "sample"]])

    def test_example_keeps_its_single_target_and_required_features(self) -> None:
        for example, features in (("plain", []), ("featured", ["--features", "sample/a"])):
            with self.subTest(example=example):
                _ = self.verify("example", "sample", example)
                self.assertEqual(self.calls("clippy"), [[
                    "--workspace", "--example", example, *features, "--", "-D", "warnings"
                ]])
                self.assertEqual(self.calls("mend"), [])
                self.assertEqual(self.calls("doc"), [])
                self.assertEqual(self.calls("+nightly"), [])
                self.assertEqual(self.taplo_calls(), [])

    def test_clippy_off_names_the_full_scope_without_running_it(self) -> None:
        self.write_config(clippy="off")
        for features in ([], ["--features", "a"]):
            with self.subTest(features=features):
                result = self.verify("lint", "sample", *features)
                skip_lines = [line for line in result.stdout.splitlines()
                              if line.startswith("SKIPPED:") and "clippy" in line]
                self.assertEqual(skip_lines, [
                    "SKIPPED: cargo clippy --workspace --all-targets --all-features"
                    + f" — clippy=off in {self.config}"
                ])
                self.assertEqual(self.calls("clippy"), [])
                self.assertEqual(len(self.calls("mend")), 1)
                self.assertEqual(len(self.calls("doc")), 1)
                self.assert_workspace_taplo_check()

    def test_taplo_uses_workspace_config_when_lint_starts_in_a_member_directory(self) -> None:
        result = self.invoke_verify("lint", "sample", cwd=self.root / "src")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_workspace_taplo_check()

    def test_taplo_failure_fails_lint_and_names_the_unformatted_file(self) -> None:
        result = self.invoke_verify("lint", "sample", cwd=self.root / "src", taplo_status=7)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_workspace_taplo_check()
        self.assertIn("Cargo.toml is not formatted", result.stdout + result.stderr)

    def test_missing_taplo_fails_before_stages_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.isolate_path_without_taplo()
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines(), ["taplo is required"])
        self.assertEqual(self.calls("mend"), [])
        self.assertEqual(self.calls("+nightly"), [])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(self.calls("doc"), [])
        self.assertEqual(self.taplo_calls(), [])
        self.assertEqual(list(cache.glob("*.fail")), [])

        self.write_executable(self.root / "bin" / "taplo", TAPLO_STUB)
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_taplo_panic_on_linux_is_a_tooling_failure_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.write_executable(self.root / "bin" / "uname", 'print("Linux")\n')
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=101)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("taplo panicked", result.stderr)
        self.assertIn("tooling failure", result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(list(cache.glob("*.fail")), [])

        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_taplo_panic_on_macos_requests_unsandboxed_retry_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.write_executable(self.root / "bin" / "uname", 'print("Darwin")\n')
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=101)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("taplo panicked", result.stderr)
        self.assertIn("macOS sandbox", result.stderr)
        self.assertIn("Re-run the same command with the sandbox disabled", result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(list(cache.glob("*.fail")), [])

        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_unformatted_toml_remains_a_cached_tree_failure(self) -> None:
        cache = self.enable_cache()
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Cargo.toml is not formatted", result.stdout + result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.fail"))), 1)

        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL (recorded)", result.stdout)
        self.assertEqual(self.taplo_calls(), [])

    def dirty_cache(self) -> Path:
        """Turn the cache on with src/lib.rs uncommitted, so its content is in the tree key."""
        self.environment["TEST_GIT_DIRTY"] = "1"
        return self.enable_cache()

    def clippy_ran(self) -> bool:
        return self.events.exists() and bool(self.calls("clippy"))

    def test_edit_while_clippy_fails_records_nothing_and_the_next_lint_runs(self) -> None:
        cache = self.dirty_cache()
        self.environment.update({"TEST_EDIT_DURING": "clippy", "TEST_CLIPPY_STATUS": "101"})
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
        self.assertIn("the tree changed while lint ran, so this result was not recorded", result.stdout)
        self.assertEqual(sorted(path.name for path in cache.iterdir()), [])

        self.environment.update({"TEST_EDIT_DURING": "", "TEST_CLIPPY_STATUS": "0"})
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("(recorded)", result.stdout)
        self.assertTrue(self.clippy_ran())
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_edit_while_clippy_passes_records_nothing(self) -> None:
        cache = self.dirty_cache()
        self.environment["TEST_EDIT_DURING"] = "clippy"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the tree changed while lint ran", result.stdout)
        self.assertEqual(sorted(path.name for path in cache.iterdir()), [])

    def test_mend_rewrite_is_recorded_under_the_tree_it_leaves(self) -> None:
        cache = self.dirty_cache()
        self.environment["TEST_EDIT_DURING"] = "mend"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("the tree changed", result.stdout)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        self.environment["TEST_EDIT_DURING"] = ""
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())


if __name__ == "__main__":
    _ = unittest.main()
