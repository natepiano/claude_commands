"""Isolated script tests: local Git only, disposable HOME, one-shot failures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from typing import override


SCRIPTS = Path(__file__).resolve().parents[1]


def initialize_repository(path: Path, remote: Path, environment: Mapping[str, str]) -> None:
    """Initialize every fixture repository, including generated stub projects."""
    for arguments in (
        ("init", "--quiet", "-b", "main"),
        ("config", "commit.gpgsign", "false"),
        ("remote", "add", "origin", str(remote)),
    ):
        _ = subprocess.run(
            ["git", "-C", str(path), *arguments], env=environment,
            text=True, capture_output=True, check=True, timeout=30,
        )


class ScriptRepositoryTestCase(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    remote: Path = Path()
    home: Path = Path()
    bin: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        for directory in (self.repo, self.home, self.bin):
            directory.mkdir()
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git is required")
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "CARGO_", "RUST", "VALIDATE_"))
        }
        self.environment.update(
            HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="rerun test", GIT_AUTHOR_EMAIL="rerun@example.invalid",
            GIT_COMMITTER_NAME="rerun test", GIT_COMMITTER_EMAIL="rerun@example.invalid",
            PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            TEST_REAL_GIT=real_git, TEST_GIT_FAILURE=str(self.root / "git-failure.json"),
        )
        _ = self.git("init", "--bare", "--quiet", str(self.remote))
        self.initialize_repository(self.repo)
        _ = self.stub("git", '''import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
failure = Path(os.environ["TEST_GIT_FAILURE"])
stop = False
after = False
if failure.exists():
    spec = json.loads(failure.read_text())
    stop = args[:len(spec["args"])] == spec["args"]
    after = spec["after"]
    if stop:
        failure.unlink()
if stop and not after:
    sys.exit(86)
code = subprocess.call([os.environ["TEST_REAL_GIT"], *args])
sys.exit(86 if stop else code)
''')
        for command in ("gh", "cargo", "cargo-generate", "cargo-mend", "curl", "wget", "ssh", "taplo", "rustup"):
            _ = self.stub(command, 'raise SystemExit("unexpected external command")\n')

    def write(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")
        return path

    def stub(self, name: str, source: str) -> Path:
        path = self.write(self.bin / name, f"#!{sys.executable}\n" + source)
        path.chmod(0o755)
        return path

    def run_in_repo(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, cwd=self.repo, env=self.environment,
            text=True, capture_output=True, check=False, timeout=30,
        )

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = self.run_in_repo("git", *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def seed(self) -> None:
        _ = self.write(self.repo / "seed.txt", "seed\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "--quiet", "-m", "seed")
        _ = self.git("push", "--quiet", "-u", "origin", "main")

    def initialize_repository(self, path: Path) -> None:
        initialize_repository(path, self.remote, self.environment)

    def fail_git(self, *prefix: str, after: bool = False) -> None:
        _ = self.write(
            Path(self.environment["TEST_GIT_FAILURE"]),
            json.dumps({"args": prefix, "after": after}),
        )

    def stop_command(self, name: str, *, target: str, after: bool = False, kill_parent: bool = False) -> None:
        """Stop once at a chosen command's last argument, before or after it runs."""
        executable = shutil.which(name)
        if executable is None:
            self.fail(f"{name} is required")
        flag = self.write(self.root / f"stop-{name}", "stop\n")
        _ = self.stub(name, f'''import os, signal, subprocess, sys
from pathlib import Path
flag = Path({str(flag)!r})
stop = flag.exists() and sys.argv[-1] == {target!r}
if stop:
    flag.unlink()
if stop and not {after!r}:
    sys.exit(86)
code = subprocess.call([{executable!r}, *sys.argv[1:]])
if stop:
    if {kill_parent!r}:
        os.kill(os.getppid(), signal.SIGKILL)
    sys.exit(86)
sys.exit(code)
''')

    def script(self, relative: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.run_in_repo("bash", str(SCRIPTS / relative), *arguments)
