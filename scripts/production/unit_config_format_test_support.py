"""Use real taplo checks with private repository configurations."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@dataclass(frozen=True)
class TaploInvocations:
    log: Path

    def count(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0


@contextmanager
def count_taplo(test: unittest.TestCase, root: Path) -> Generator[TaploInvocations]:
    """Count subprocess calls through a wrapper that executes the real taplo."""
    binary = require_taplo(test)
    directory = root / "taplo-wrapper"
    directory.mkdir()
    log = directory / "calls"
    wrapper = directory / "taplo"
    write_text_atomic(wrapper, f"#!{sys.executable}\n"
                      + "import os, sys\nfrom pathlib import Path\n"
                      + f'with open(Path({str(Path(__file__).resolve().parents[2])!r}) / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {{"__file__": package_root.name}})\n'
                      + "from scripts.lib.atomic_file import write_text_atomic\n"
                      + f"log = Path({str(log)!r})\n"
                      + "previous = log.read_text() if log.exists() else ''\n"
                      + "write_text_atomic(log, previous + repr(sys.argv[1:]) + '\\n')\n"
                      + f"os.execv({binary!r}, [{binary!r}, *sys.argv[1:]])\n")
    wrapper.chmod(0o700)
    with patch.dict(os.environ, {"PATH": str(directory) + os.pathsep + os.environ.get("PATH", "")}):
        yield TaploInvocations(log)


def require_taplo(test: unittest.TestCase) -> str:
    binary = shutil.which("taplo")
    if binary is None:
        test.skipTest("The taplo binary is absent.")
    return binary


def write_aligning_config(repository: Path) -> Path:
    path = repository / "taplo.toml"
    write_text_atomic(path, "[formatting]\nalign_entries = true\nreorder_keys = true\n")
    return path


def remove_assignment_spacing(path: Path) -> None:
    text = re.sub(r"^([ \t]*[A-Za-z_][A-Za-z0-9_-]*)[ \t]*=[ \t]*", r"\1=", path.read_text(), flags=re.MULTILINE)
    write_text_atomic(path, text)


def check_format(binary: str, path: Path, repository: Path, *, defaults: bool = False) -> subprocess.CompletedProcess[str]:
    options = ["--no-auto-config"] if defaults else ["--config", str(repository / "taplo.toml")]
    return subprocess.run([binary, "fmt", *options, "--check", str(path)], cwd=repository,
                          text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
