"""Run the remote copy commands against private local hosts."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
import sys
from unittest import mock

from scripts.lint.memory_test_support import config_environment
from scripts.copies.copies_test_support import copies_environment


@contextmanager
def local_remote_transport(root: Path) -> Generator[None]:
    repository = Path(__file__).resolve().parents[2]
    tools = root / "remote-tools"
    tools.mkdir(exist_ok=True)
    scripts = root / ".claude/scripts"
    scripts.parent.mkdir(exist_ok=True)
    scripts.symlink_to(repository / "scripts", target_is_directory=True)
    ssh = tools / "ssh"
    _ = ssh.write_text(f"#!{sys.executable}\n" + '''import os, sys
environment = dict(os.environ)
if sys.argv[-2] == 'natedev':
    environment['MAC_TEST_STATE_DIR'] = environment['REMOTE_CLAIM_STATE']
os.execve('/bin/bash', ['bash', '-c', sys.argv[-1]], environment)
''')
    ssh.chmod(0o755)
    with mock.patch.dict(os.environ, {
        **config_environment(root), **copies_environment(root),
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(repository), "CLAUDE_PYTHON": sys.executable,
        "MAC_TEST_STATE_DIR": str(root / "local-claims"),
        "REMOTE_CLAIM_STATE": str(root / "remote-claims"),
    }):
        yield
