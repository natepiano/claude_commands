"""Check worker lint scope with synthetic Cargo metadata and real lint policy."""

from __future__ import annotations

from scripts.lib.checkout_test_support import write_python_tool, write_stdlib_tool

from scripts.build_queue.admission_test_support import caller_admission
from scripts.lib.py_floor_test_support import seed_interpreter_cache

from scripts.lint.memory_test_support import config_environment, write_config_fixture, write_pressure_reading

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import ClassVar, TypedDict, cast, override
import unittest
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.shell_test_support import shell_functions


VERIFY = Path(__file__).with_name("verify.sh")
SCRIPTS = VERIFY.parent.parent
TAPLO_RESULTS_FILE = "taplo-results.jsonl"


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
version_tools = {
    ("mend", "--version"): "MEND",
    ("+nightly", "fmt", "--version"): "RUSTFMT",
    ("clippy", "--version"): "CLIPPY",
}
version_tool = version_tools.get(tuple(arguments))
# Existing scope tests also record the rustfmt availability check.
if not version_tool or version_tool == "RUSTFMT":
    with Path(os.environ["TEST_CARGO_EVENTS"]).open("a", encoding="utf-8") as output:
        output.write(json.dumps({"args": arguments}) + "\n")
if version_tool:
    if "TEST_VERSION_EVENTS" in os.environ:
        with Path(os.environ["TEST_VERSION_EVENTS"]).open("a", encoding="utf-8") as output:
            output.write(version_tool + "\n")
    print(os.environ.get("TEST_VERSION_" + version_tool, version_tool + " fixture"))
    raise SystemExit(0)
if arguments[:1] == ["metadata"]:
    print(os.environ["TEST_METADATA"])
elif arguments[:1] not in (["mend"], ["clippy"], ["doc"], ["+nightly"]):
    raise SystemExit("unexpected cargo invocation: " + repr(arguments))
# A file changes during mend or judgment, after any clippy fix.
if arguments[:1] == [os.environ.get("TEST_EDIT_DURING", "")] and (
    arguments[0] != "clippy" or "--fix" not in arguments
):
    with Path(os.environ["PACKAGE_ROOT"], "src", "lib.rs").open("a", encoding="utf-8") as source:
        source.write("// changed during " + arguments[0] + "\n")
if arguments[:1] == ["clippy"]:
    source = Path(os.environ["PACKAGE_ROOT"], os.environ.get("TEST_FIX_FILE", "src/lib.rs"))
    if "--fix" in arguments:
        if "TEST_FIX_SOURCE" in os.environ:
            source.write_text(os.environ["TEST_FIX_SOURCE"], encoding="utf-8")
        steps = json.loads(os.environ.get("TEST_FIX_STEPS", "{}"))
        before = source.read_text(encoding="utf-8") if source.exists() else ""
        if before in steps:
            source.write_text(steps[before], encoding="utf-8")
        if "TEST_FIX_APPEND" in os.environ:
            with source.open("a", encoding="utf-8") as output:
                output.write(os.environ["TEST_FIX_APPEND"])
        raise SystemExit(int(os.environ.get("TEST_CLIPPY_FIX_STATUS", "0")))
    if "TEST_CLIPPY_EXPECT_SOURCE" in os.environ:
        if source.read_text(encoding="utf-8") != os.environ["TEST_CLIPPY_EXPECT_SOURCE"]:
            raise SystemExit(101)
    if "TEST_CLIPPY_STATUSES" in os.environ:
        judgments = [json.loads(line)["args"] for line in
                     Path(os.environ["TEST_CARGO_EVENTS"]).read_text(encoding="utf-8").splitlines()]
        judgment_count = sum(args[:1] == ["clippy"] and "--fix" not in args for args in judgments)
        raise SystemExit(json.loads(os.environ["TEST_CLIPPY_STATUSES"])[judgment_count - 1])
    raise SystemExit(int(os.environ.get("TEST_CLIPPY_STATUS", "0")))
if arguments[:2] == ["+nightly", "fmt"] and "--version" not in arguments:
    events = [json.loads(line)["args"] for line in
              Path(os.environ["TEST_CARGO_EVENTS"]).read_text(encoding="utf-8").splitlines()]
    if any(args[:1] == ["clippy"] and "--fix" in args for args in events):
        if "TEST_FMT_SOURCE" in os.environ:
            Path(os.environ["PACKAGE_ROOT"], "src", "lib.rs").write_text(
                os.environ["TEST_FMT_SOURCE"], encoding="utf-8",
            )
'''


TAPLO_STUB = r'''
import json
import os
from pathlib import Path
import sys

if sys.argv[1:] == ["--version"]:
    if "TEST_VERSION_EVENTS" in os.environ:
        with Path(os.environ["TEST_VERSION_EVENTS"]).open("a", encoding="utf-8") as output:
            output.write("TAPLO\n")
    print(os.environ.get("TEST_VERSION_TAPLO", "taplo fixture"))
    raise SystemExit(0)
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
case " $* " in
    *" rev-parse "*)
        case " $* " in
            *" --show-toplevel "*)
                case " $* " in *" --git-common-dir "*) printf '%s/.git\n' "$PACKAGE_ROOT" ;; esac
                printf '%s\n' "$PACKAGE_ROOT"
                case " $* " in *" --abbrev-ref "*) printf 'main\n' ;; esac ;;
            *" HEAD^{tree} "*) printf 'fixed-tree\n' ;;
            *" --short "*) printf 'abc1234\n' ;;
            *) exit 1 ;;
        esac ;;
    *" diff "*)
        case " $* " in *" --name-only "*) printf 'src/lib.rs\000' ;; *) exit 1 ;; esac ;;
    *" status "*)
        [ -z "${TEST_GIT_DIRTY:-}" ] || printf ' M src/lib.rs\000'
        if [ -n "${TEST_GIT_UNTRACKED:-}" ] && [ -e "$PACKAGE_ROOT/$TEST_GIT_UNTRACKED" ]; then
            printf '?? %s\000' "$TEST_GIT_UNTRACKED"
        fi ;;
    *" ls-files "*) printf 'Cargo.toml\000taplo.toml\000' ;;
    *) exit 1 ;;
esac
# An empty status still succeeds.
exit 0
'''


class VerifyLintScopeFixture(unittest.TestCase):
    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="lint-scope-test-")))
        builder = cls()
        builder.root = directory / "case"
        builder.root.mkdir()
        builder.build_fixture()
        cls.fixture_root = builder.root
        cls.fixture_seed = directory / "seed"
        cls.fixture_environment = builder.environment.copy()
        _ = shutil.copytree(builder.root, cls.fixture_seed, symlinks=True)
        shutil.rmtree(builder.root)

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.config: Path = Path()
        self.events: Path = Path()
        self.taplo_events: Path = Path()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        # A few feature tests use this fixture directly, without a class lifecycle.
        if "fixture_seed" not in type(self).__dict__ or not self.fixture_seed.is_dir():
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
            self.build_fixture()
            _ = caller_admission(self, self.root, self.environment)
            return
        self.root = self.fixture_root
        self.addCleanup(shutil.rmtree, self.root)
        _ = shutil.copytree(self.fixture_seed, self.root, symlinks=True)
        self.config = self.root / "lint.conf"
        self.events = self.root / "cargo-events.jsonl"
        self.taplo_events = self.root / "taplo-events.jsonl"
        self.environment = self.fixture_environment.copy()
        _ = caller_admission(self, self.root, self.environment)

    def build_fixture(self) -> None:
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
        write_stdlib_tool(bin_directory / "cargo", CARGO_STUB)
        write_stdlib_tool(bin_directory / "taplo", TAPLO_STUB)
        self.write_shell(bin_directory / "git", GIT_STUB)
        self.write_shell(bin_directory / "rustc", 'printf "rustc fixture\\n"\n')
        targets = self.root / "build-targets.py"
        self.write_executable(targets, '''import runpy
import sys
from pathlib import Path
def main():
    if sys.argv[1] == "eligible":
        print("natedev_only test target\\nnatedev\\teligible\\t-\\t-\\t-")
    else:
        if sys.argv[1] == "constraints":
            sys.argv.extend(("--local", "test target"))
        runpy.run_path(str(Path.home() / ".claude/scripts/build_targets/build_targets.py"), run_name="__main__")

if __name__ == "__main__":
    main()
''')
        for name in ("ssh", "rsync", "scp", "gh", "systemd-run"):
            self.write_shell(bin_directory / name, "exit 99\n")
        for directory in ("tmp", "state", "cache", "runtime", "target", "cargo-home", ".git", "cgroup"):
            (self.root / directory).mkdir()
        seed_interpreter_cache(self.root / "cache")
        pressure = self.root / "memory-pressure"
        write_pressure_reading(pressure)
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
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
            if not name.startswith(("BUILD", "UNIT_", "LINT_", "MAC_TEST_",
                                    "CARGO_", "RUST", "NEXTEST_", "CLAUDE_CODE_", "CODEX_"))
        }
        self.environment.update({
            # The fixture supplies its complete toolchain; Nix discovery is unrelated
            # to lint scope, formatting, fix convergence or cache assertions.
            "IN_NIX_SHELL": "pure",
            "HOME": str(home),
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_PYTHON": "",
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
            "VERIFY_STEPS_DIR": str(self.root / "verify-steps"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_MEMORY_PRESSURE": str(pressure),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "BUILD_TARGETS_SCRIPT": str(targets),
            "LINT_CONFIG_READER": str(SCRIPTS / "lint" / "lint_config.sh"),
            "LINT_CONFIG_FILE": str(self.config), **config_environment(self.root),
        })

    def write_executable(self, path: Path, source: str) -> None:
        write_python_tool(path, source)

    def write_shell(self, path: Path, source: str) -> None:
        _ = path.write_text("#!/bin/sh\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def use_real_toml_tools(self) -> None:
        """Keep the lint fixture, replacing only its Git and Taplo stubs."""
        for name in ("git", "taplo"):
            executable = shutil.which(name)
            if executable is None:
                self.fail(f"fixture requires {name}")
            source = f"import os, sys\nos.execv({executable!r}, [{executable!r}, *sys.argv[1:]])\n"
            if name == "taplo":
                results = self.root / TAPLO_RESULTS_FILE
                _ = results.write_bytes(b"")
                source = f"executable = {executable!r}\nresults = {str(results)!r}\n" + r'''
import json
from pathlib import Path
import subprocess
import sys

result = subprocess.run([executable, *sys.argv[1:]], stderr=subprocess.PIPE, check=False)
signal = -result.returncode if result.returncode < 0 else 0
with Path(results).open("a", encoding="utf-8") as output:
    output.write(json.dumps({
        "args": sys.argv[1:],
        "returncode": result.returncode,
        "signal": signal,
        "stderr": result.stderr.decode("utf-8", errors="backslashreplace"),
    }) + "\n")
sys.stderr.buffer.write(result.stderr)
raise SystemExit(128 + signal if signal else result.returncode)
'''
            self.write_executable(
                self.root / "bin" / name,
                source,
            )
        shutil.rmtree(self.root / ".git")
        self.run_git("init", "--quiet", "--initial-branch=main")
        # Logs and caches change during lint. Keep the untracked unit config
        # visible, while explicitly adding every source/config used by a test.
        _ = (self.root / ".git/info/exclude").write_text("/*\n!/.unit.toml\n", encoding="utf-8")
        self.run_git("add", "--force", "--", "Cargo.toml", "taplo.toml", "src/lib.rs")
        self.run_git("-c", "user.name=Lint fixture", "-c", "user.email=lint@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")

    def run_git(self, *arguments: str) -> None:
        _ = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(self.root), *arguments], env=self.environment,
            capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
        )

    def toml_glob_neighbors(self, *, untracked_text: str) -> tuple[Path, Path]:
        self.use_real_toml_tools()
        tracked = self.root / "part[1].toml"
        untracked = self.root / "part1.toml"
        _ = tracked.write_text("value=1\n", encoding="utf-8")
        _ = untracked.write_text(untracked_text, encoding="utf-8")
        self.run_git("add", "--force", "--", tracked.name)
        with self.assertRaises(subprocess.CalledProcessError) as missing:
            self.run_git("ls-files", "--error-unmatch", "--", untracked.name)
        self.assertEqual(missing.exception.returncode, 1)
        return tracked, untracked

    def invoke_workspace_taplo(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Exercise the production formatter without simulating a remote build hold."""
        return self.invoke_taplo_at(self.root, *arguments)

    def invoke_taplo_at(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Call the real formatter from the selected directory with the fixture tools."""
        formatter = shell_functions(VERIFY, "workspace_taplo")
        return subprocess.run(
            ["bash", "-c", 'set -euo pipefail\nrun() { "$@"; }\n' + formatter
             + 'workspace_taplo "$@"', "taplo-fixture", *arguments],
            cwd=cwd, env={**self.environment, "WORKSPACE_ROOT": str(self.root)},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def write_config(self, *, clippy: str) -> None:
        _ = write_config_fixture(self.config, f"mend=on\nfmt=on\nclippy={clippy}\ndoc=on\nsweep=off\n")

    def enable_cache(self) -> Path:
        session = self.root / "session"
        session.mkdir()
        self.environment["UNIT_SESSION_DIR"] = str(session)
        return session / "verify_cache"

    def isolate_path_without_taplo(self) -> None:
        tools = self.root / "tools"
        tools.mkdir()
        hash_tool = "sha256sum" if shutil.which("sha256sum") else "shasum"
        for name in (
            "bash", "python3", "awk", "cat", "cmp", "cut", "date", "dirname", "env", "grep",
            "head", "mkdir", "mktemp", "mv", "ps", "rm", "sed", "sleep",
            "sort", "tail", "tee", "tr", "uname", "wc", hash_tool,
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
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def taplo_calls(self) -> list[TaploInvocation]:
        if not self.taplo_events.exists():
            return []
        return [cast(TaploInvocation, json.loads(line))
                for line in self.taplo_events.read_text(encoding="utf-8").splitlines()]

    def assert_workspace_taplo_check(self) -> None:
        self.assertEqual(self.taplo_calls(), [{
            "args": ["fmt", "--check", "--", "Cargo.toml", "taplo.toml"], "cwd": str(self.root),
        }])

    def calls(self, command: str) -> list[list[str]]:
        return [args[1:] for args in self.cargo_calls() if args[:1] == [command]]

    def cargo_calls(self) -> list[list[str]]:
        if not self.events.exists():
            return []
        events = [cast(CargoInvocation, json.loads(line))
                  for line in self.events.read_text(encoding="utf-8").splitlines()]
        return [event["args"] for event in events]

    def assert_fix_and_judgment_calls(self, *, fix_passes: int = 1) -> None:
        scope = ["--workspace", "--all-targets", "--all-features", "--keep-going"]
        self.assertEqual(self.calls("clippy"), [[*scope, "--", "-D", "warnings"]] + [
            ["--jobs", "8", "--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs",
             *scope, "--", "-D", "warnings"],
        ] * fix_passes + [[*scope, "--", "-D", "warnings"]])

    def lint_stages(self) -> list[list[str]]:
        return [args for args in self.cargo_calls()
                if args[0] in ("clippy", "doc") or
                (args[:2] == ["+nightly", "fmt"] and "--version" not in args)]

    def dirty_cache(self) -> Path:
        """Turn the cache on with src/lib.rs uncommitted, so its content is in the tree key."""
        self.environment["TEST_GIT_DIRTY"] = "1"
        return self.enable_cache()

    def clippy_ran(self) -> bool:
        return self.events.exists() and bool(self.calls("clippy"))


