"""Cargo routing checks use a temporary PATH, hold state and user bus."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from typing import override


SCRIPTS = Path(__file__).resolve().parent
SCOPE_FLAGS = [
    "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
    "--slice=builds.slice", "--",
]
CARGO_STUB = """import json, os, sys
from pathlib import Path
Path(os.environ['CARGO_LOG']).write_text(json.dumps({
    'argv': sys.argv[1:], 'cwd': os.getcwd(),
    'env': os.environ['CARGO_TEST_VALUE'], 'stdin': sys.stdin.read(),
}))
print('cargo stdout')
print('cargo stderr', file=sys.stderr)
sys.exit(int(os.environ.get('CARGO_EXIT', '0')))
"""
SCOPE_STUB = """import json, os, sys
from pathlib import Path
Path(os.environ['SCOPE_LOG']).write_text(json.dumps(sys.argv[1:]))
command = sys.argv[sys.argv.index('--') + 1:]
os.execv(command[0], command)
"""
PYTHON_STUB = """import os, sys
from pathlib import Path
Path(os.environ['PYTHON_LOG']).write_text('started')
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
"""
HOLD_STUB = """import os, sys
from pathlib import Path
Path(os.environ['HOLD_LOG']).write_text('checked')
status = int(os.environ.get('HOLD_EXIT', '0'))
if status == 0:
    print('natedev builds are held by another session')
elif status == 2:
    print('hold state cannot be read', file=sys.stderr)
sys.exit(status)
"""


class CargoWrapperTest(unittest.TestCase):
    root: Path = Path()
    wrapper: Path = Path()
    installer: Path = Path()
    front: Path = Path()
    real_bin: Path = Path()
    tools: Path = Path()
    home: Path = Path()
    cwd: Path = Path()
    hold: Path = Path()
    runtime: Path = Path()
    real_cargo: Path = Path()
    cgroup: Path = Path()
    cargo_log: Path = Path()
    scope_log: Path = Path()
    hold_log: Path = Path()
    python_log: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix="cargo-test-", dir="/tmp",
        )))
        self.wrapper = self.root / "scripts" / "cargo"
        self.wrapper.parent.mkdir()
        _ = shutil.copy2(SCRIPTS / "cargo", self.wrapper)
        self.installer = self.wrapper.with_name("install_cargo_wrapper.sh")
        _ = shutil.copy2(SCRIPTS / self.installer.name, self.installer)
        _ = self.wrapper.with_name("hold_applies.py").write_text(HOLD_STUB)
        self.front = self.root / "front"
        self.real_bin = self.root / "real"
        self.tools = self.root / "tools"
        self.home = self.root / "home"
        self.cwd = self.root / "working directory"
        self.hold = self.root / "hold"
        self.runtime = self.root / "run"
        for directory in (
            self.front, self.real_bin, self.tools, self.home,
            self.cwd, self.hold, self.runtime,
        ):
            directory.mkdir()
        (self.front / "cargo").symlink_to(self.wrapper)
        self.real_cargo = self.real_bin / "cargo"
        self.write_program(self.real_cargo, CARGO_STUB)
        self.write_program(self.tools / "systemd-run", SCOPE_STUB)
        self.write_program(self.tools / "python3", PYTHON_STUB)
        bash = shutil.which("bash")
        readlink = shutil.which("readlink")
        assert bash is not None and readlink is not None
        (self.tools / "bash").symlink_to(bash)
        (self.tools / "readlink").symlink_to(readlink)
        self.cgroup = self.root / "cgroup"
        _ = self.cgroup.write_text("0::/user.slice/app-agents.slice/session.scope\n")
        bus = self.enterContext(socket.socket(socket.AF_UNIX))
        bus.bind(str(self.runtime / "bus"))
        self.cargo_log = self.root / "cargo.json"
        self.scope_log = self.root / "scope.json"
        self.hold_log = self.root / "hold.log"
        self.python_log = self.root / "python.log"
        self.env = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join(map(str, (self.front, self.real_bin, self.tools))),
            "OSTYPE": "linux-gnu",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "BUILD_HOLD_DIR": str(self.hold),
            "CARGO_WRAPPER_CGROUP_FILE": str(self.cgroup),
            "CARGO_LOG": str(self.cargo_log),
            "SCOPE_LOG": str(self.scope_log),
            "HOLD_LOG": str(self.hold_log),
            "PYTHON_LOG": str(self.python_log),
            "CARGO_TEST_VALUE": "spaces and $variables ${HOME} $$",
        }

    @staticmethod
    def write_program(path: Path, source: str) -> None:
        _ = path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)

    def invoke(self, *args: str) -> subprocess.CompletedProcess[str]:
        for log in (self.cargo_log, self.scope_log, self.hold_log, self.python_log):
            log.unlink(missing_ok=True)
        return subprocess.run(
            [str(self.front / "cargo"), *args], env=self.env, cwd=self.cwd,
            input="cargo input\n", capture_output=True, text=True, timeout=10,
            check=False,
        )

    def assert_cargo(self, args: list[str]) -> None:
        self.assertEqual(self.cargo_log.read_text(), json.dumps({
            "argv": args, "cwd": str(self.cwd),
            "env": self.env["CARGO_TEST_VALUE"], "stdin": "cargo input\n",
        }))

    def assert_scope(self, args: list[str]) -> None:
        self.assertEqual(self.scope_log.read_text(), json.dumps([
            *SCOPE_FLAGS, str(self.real_cargo), *args,
        ]))

    def test_non_building_commands_skip_scope_and_python(self) -> None:
        _ = (self.hold / "holder").write_text("held")
        for args in (
            [], ["metadata"], ["fmt"], ["tree"], ["--version"], ["--help"],
            ["custom-command"], ["+nightly", "metadata"],
            ["--config", "test", "metadata"], ["--color", "build", "tree"],
        ):
            with self.subTest(args=args):
                done = self.invoke(*args)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assert_cargo(args)
                self.assertFalse(self.scope_log.exists())
                self.assertFalse(self.hold_log.exists())
                self.assertFalse(self.python_log.exists())

    def test_building_commands_use_builds_slice(self) -> None:
        for command in (
            "build", "b", "check", "c", "clippy", "test", "t", "nextest",
            "run", "r", "bench", "doc", "d", "rustc", "rustdoc", "fix",
            "install", "miri", "mend",
        ):
            with self.subTest(command=command):
                args = [command, "--", "$HOME", "${HOME}", "$$", "two words", ""]
                done = self.invoke(*args)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assert_cargo(args)
                self.assert_scope(args)
                self.assertFalse(self.python_log.exists())
                self.assertEqual(done.stdout, "cargo stdout\n")
                self.assertEqual(done.stderr, "cargo stderr\n")

    def test_toolchain_and_options_before_subcommand(self) -> None:
        for args in (
            ["+nightly", "build"], ["-C", "dir", "test"],
            ["--config", "build", "--color", "auto", "check"],
            ["-Z", "unstable-options", "--manifest-path", "Cargo.toml", "clippy"],
            ["--config=build", "-Cdir", "-Zunstable-options", "test"],
            ["--target-dir", "metadata", "--profile", "dev", "build"],
        ):
            with self.subTest(args=args):
                done = self.invoke(*args)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assert_cargo(args)
                self.assert_scope(args)

    def test_existing_build_scope_skips_hold_and_new_scope(self) -> None:
        _ = self.cgroup.write_text("0::/user.slice/builds.slice/run-42.scope\n")
        _ = (self.hold / "holder").write_text("held")
        done = self.invoke("test")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_cargo(["test"])
        self.assertFalse(self.scope_log.exists())
        self.assertFalse(self.hold_log.exists())
        self.assertFalse(self.python_log.exists())

    def test_hold_refuses_build(self) -> None:
        _ = (self.hold / "holder").write_text("held")
        for command in ("build", "mend"):
            with self.subTest(command=command):
                done = self.invoke(command)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, "")
                self.assertEqual(done.stderr, "natedev builds are held by another session\n")
                self.assertTrue(self.hold_log.exists())
                self.assertTrue(self.python_log.exists())
                self.assertFalse(self.cargo_log.exists())
                self.assertFalse(self.scope_log.exists())

    def test_hold_allows_build(self) -> None:
        # A hidden entry must also cause a hold check.
        _ = (self.hold / ".holder").write_text("held")
        self.env["HOLD_EXIT"] = "1"
        done = self.invoke("test")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "cargo stderr\n")
        self.assertTrue(self.hold_log.exists())
        self.assert_cargo(["test"])
        self.assert_scope(["test"])

    def test_unreadable_hold_prints_one_note_and_builds(self) -> None:
        _ = (self.hold / "holder").write_text("held")
        self.env["HOLD_EXIT"] = "2"
        done = self.invoke("build")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stderr, "cargo: note: hold state cannot be read\ncargo stderr\n")
        self.assertTrue(self.hold_log.exists())
        self.assert_cargo(["build"])
        self.assert_scope(["build"])

    def test_empty_or_absent_hold_directory_starts_no_python(self) -> None:
        for hold_exists in (True, False):
            with self.subTest(hold_exists=hold_exists):
                if not hold_exists:
                    self.hold.rmdir()
                done = self.invoke("build")
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assert_cargo(["build"])
                self.assertFalse(self.hold_log.exists())
                self.assertFalse(self.python_log.exists())

    def test_exit_status_passes_through(self) -> None:
        self.env["CARGO_EXIT"] = "37"
        for command in ("test", "metadata"):
            with self.subTest(command=command):
                done = self.invoke(command)
                self.assertEqual(done.returncode, 37, done.stderr)
                self.assertEqual(done.stdout, "cargo stdout\n")
                self.assertEqual(done.stderr, "cargo stderr\n")

    def test_missing_bus_runs_cargo_directly(self) -> None:
        (self.runtime / "bus").unlink()
        done = self.invoke("build")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_cargo(["build"])
        self.assertFalse(self.scope_log.exists())

    def test_missing_systemd_run_runs_cargo_directly(self) -> None:
        (self.tools / "systemd-run").unlink()
        done = self.invoke("build")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_cargo(["build"])
        self.assertFalse(self.scope_log.exists())

    def test_macos_skips_hold_and_scope(self) -> None:
        self.env["OSTYPE"] = "darwin"
        _ = (self.hold / "holder").write_text("held")
        done = self.invoke("build")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_cargo(["build"])
        self.assertFalse(self.scope_log.exists())
        self.assertFalse(self.python_log.exists())

    def test_skips_all_paths_that_resolve_to_wrapper(self) -> None:
        alias = self.root / "alias"
        alias.mkdir()
        (alias / "cargo").symlink_to(self.front / "cargo")
        self.env["PATH"] = os.pathsep.join(map(str, (
            self.front, self.wrapper.parent, alias, self.real_bin, self.tools,
        )))
        done = self.invoke("metadata")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_cargo(["metadata"])

    def test_missing_real_cargo_exits_127(self) -> None:
        self.real_cargo.unlink()
        done = self.invoke("metadata")
        self.assertEqual(done.returncode, 127)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr, "cargo: no other cargo executable on PATH\n")
        self.assertFalse(self.cargo_log.exists())

    def test_empty_path_entry_uses_current_directory(self) -> None:
        _ = self.real_cargo.rename(self.cwd / "cargo")
        self.env["PATH"] = os.pathsep.join(map(str, (self.front, self.tools))) + os.pathsep
        done = self.invoke("metadata")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_cargo(["metadata"])

    def install(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.installer)], env={**os.environ, "HOME": str(self.home),
                                       "OSTYPE": self.env["OSTYPE"]},
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_installer_creates_link_and_accepts_repeat(self) -> None:
        target = self.home / ".local" / "bin" / "cargo"
        for _ in range(2):
            done = self.install()
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.readlink(), self.wrapper)

    def test_installer_refuses_existing_paths(self) -> None:
        target = self.home / ".local" / "bin" / "cargo"
        target.parent.mkdir(parents=True)
        for kind in ("file", "symlink", "directory"):
            with self.subTest(kind=kind):
                if kind == "file":
                    _ = target.write_text("keep this cargo")
                elif kind == "symlink":
                    target.symlink_to(self.root / "absent")
                else:
                    target.mkdir()
                done = self.install()
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stderr, f"cargo wrapper: refusing to replace {target}\n")
                if kind == "file":
                    self.assertEqual(target.read_text(), "keep this cargo")
                elif kind == "symlink":
                    self.assertEqual(target.readlink(), self.root / "absent")
                if kind == "directory":
                    target.rmdir()
                else:
                    target.unlink()

    def test_installer_macos_does_not_create_link(self) -> None:
        self.env["OSTYPE"] = "darwin"
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "cargo wrapper is natedev only\n")
        self.assertFalse((self.home / ".local").exists())


if __name__ == "__main__":
    _ = unittest.main()
