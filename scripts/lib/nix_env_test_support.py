"""Isolated command fixtures for the nix launch contract; never evaluate nix."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import cast, override
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/lib/nix_env.py"

DIRENV = '''import json, os, sys
from pathlib import Path
with Path(os.environ["DIRENV_LOG"]).open("a") as stream:
    stream.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
if sys.argv[1] == "allow":
    Path(os.environ["DIRENV_APPROVAL"]).touch()
    raise SystemExit(0)
if os.environ.get("DIRENV_DENY") and not Path(os.environ["DIRENV_APPROVAL"]).exists():
    print("direnv: noise\\ndirenv: .envrc is blocked", file=sys.stderr)
    raise SystemExit(1)
if os.environ.get("DIRENV_FAIL"):
    print("first detail\\nlast failure detail", file=sys.stderr)
    raise SystemExit(7)
sys.stdout.write(os.environ.get("DIRENV_DIFF", '{"IN_NIX_SHELL":"impure","NIX_LDFLAGS":"-Lfixture","PKG_CONFIG_PATH":"/fixture/pkg"}'))
'''


class NixFixture(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    work: Path = Path()
    bin: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.work = self.home / "worktrees/project"
        self.work.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {"HOME": str(self.home), "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", os.defpath),
                    "XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CONFIG_HOME": str(self.root / "config"),
                    "DIRENV_LOG": str(self.root / "direnv.jsonl"),
                    "DIRENV_APPROVAL": str(self.root / "approved"),
                    "KEEP": "caller", "REMOVE": "old"}
        self.script(self.bin / "direnv", DIRENV)
        _ = cast(object, self.enterContext(patch.dict(os.environ, self.env, clear=True)))

    def script(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def envrc(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".envrc"
        _ = path.write_text("use flake\n", encoding="utf-8")
        return path

    def direnv_calls(self) -> list[dict[str, object]]:
        path = Path(self.env["DIRENV_LOG"])
        if not path.exists():
            return []
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
