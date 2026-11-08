"""Run the remote copy commands against private local hosts."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from importlib import import_module
import os
from pathlib import Path
import runpy
import select
import shlex
import signal
import subprocess
import sys
from types import SimpleNamespace
from typing import cast, final
from unittest import mock

from scripts.lint.memory_test_support import config_environment
from scripts.copies.copies_test_support import copies_environment
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.mac_test import floor_claims


@final
class ClaimServer:
    """Run the claim server with warm imports and its real pipe lifetime."""

    def __init__(self, command: list[str], **options: object) -> None:
        incoming, sending = os.pipe()
        reading, outgoing = os.pipe()
        self.ended, ending = os.pipe()
        _ = sys.stdout.flush()
        _ = sys.stderr.flush()
        self.pid = os.fork()
        if self.pid == 0:
            _ = os.dup2(incoming, 0)
            _ = os.dup2(outgoing, 1)
            with open(os.devnull, "w") as errors:
                _ = os.dup2(errors.fileno(), 2)
            os.closerange(3, ending)
            os.closerange(ending + 1, os.sysconf("SC_OPEN_MAX"))
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
                if signal.getsignal(sig) != signal.SIG_IGN:
                    _ = signal.signal(sig, signal.SIG_DFL)
            sys.stdin = open(0, encoding="utf-8")
            sys.stdout = open(1, "w", encoding="utf-8")
            sys.stderr = open(2, "w", encoding="utf-8")
            status = 1
            try:
                if command[0] == "ssh":
                    os.environ["MAC_TEST_STATE_DIR"] = os.environ["REMOTE_CLAIM_STATE"]
                    arguments = shlex.split(command[-1])
                    exec(arguments[-1], {"__name__": "__main__"})
                else:
                    _ = runpy.run_path(command[1], run_name="__main__")
                status = 0
            except SystemExit as result:
                status = result.code if isinstance(result.code, int) else 1
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(status)
        os.close(incoming)
        os.close(outgoing)
        os.close(ending)
        encoding = cast(str, options.get("encoding", "utf-8"))
        self.stdin = os.fdopen(sending, "w", encoding=encoding)
        self.stdout = os.fdopen(reading, encoding=encoding)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
                os.close(self.ended)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.poll() is None:
            if not select.select([self.ended], [], [], timeout)[0]:
                raise subprocess.TimeoutExpired("claim server", cast(float, timeout))
            _, status = os.waitpid(self.pid, 0)
            self.returncode = os.waitstatus_to_exitcode(status)
            os.close(self.ended)
        return cast(int, self.returncode)

    def terminate(self) -> None:
        os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        os.kill(self.pid, signal.SIGKILL)


@contextmanager
def warm_claim_servers() -> Generator[None]:
    """Keep the real claim commands, locks and replies without cold imports."""
    _ = import_module("scripts.mac_test.mac_test")
    transport = SimpleNamespace(Popen=ClaimServer, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
                                TimeoutExpired=subprocess.TimeoutExpired)
    with mock.patch.object(floor_claims, "subprocess", transport):
        yield


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
    seed_interpreter_cache(root / "cache")
    with mock.patch.dict(os.environ, {
        **config_environment(root), **copies_environment(root),
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(repository), "CLAUDE_PYTHON": "", "XDG_CACHE_HOME": str(root / "cache"),
        "MAC_TEST_STATE_DIR": str(root / "local-claims"),
        "REMOTE_CLAIM_STATE": str(root / "remote-claims"),
    }):
        yield
