"""Private seeded hold fixtures and real CLI calls without interpreter startup."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import threading
import time
from typing import TYPE_CHECKING, ClassVar, TypedDict, cast, override
import unittest
from unittest import mock

import build_hold
from override_guard import require_checkout

require_checkout(Path(__file__))

if TYPE_CHECKING:
    from ..lib.socket_test_support import short_socket_directory
    from ..lint.memory_test_support import fake_memory_readings
else:
    from scripts.lib.socket_test_support import short_socket_directory
    from scripts.lint.memory_test_support import fake_memory_readings

_finish_writers: ContextVar[Callable[[], None] | None] = ContextVar("finish_hold_writers", default=None)


def invoke_hold(arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    output, errors = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(output), redirect_stderr(errors):
        try:
            status = build_hold.main(arguments)
        except SystemExit as error:
            status = error.code if isinstance(error.code, int) else 0 if error.code is None else 1
    return subprocess.CompletedProcess(["python3", str(SCRIPT), *arguments], status, output.getvalue(), errors.getvalue())


class HoldEventRecord(TypedDict):
    target: str
    event: str
    hold_key: str
    holder: str
    reason: str
    at: str
    until: str


# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("build_hold.py")
MEMORY_GATE = Path(__file__).resolve().parent.parent / "lint" / "memory_gate.sh"
# Loaded by every Python process a test starts. It opens a writer on the test's
# FIFO and passes it to each child it starts, so a detached child (the CLI's
# hold-event recorder) holds a writer until it exits, and the test's cleanup
# reads EOF only once nothing it started can still write into its scratch
# directory.
CHILD_LIFETIME = r'''import os
import subprocess

try:
    writer = os.open(os.environ["BUILD_HOLD_TEST_LIFETIME_FIFO"], os.O_WRONLY | os.O_NONBLOCK)
except (KeyError, OSError):
    writer = -1
if writer >= 0:
    original_popen = subprocess.Popen

    class LifetimeChild(original_popen):
        def __init__(self, *args, **kwargs):
            kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), writer)
            super().__init__(*args, **kwargs)

    subprocess.Popen = LifetimeChild
'''


def write_holder(folder: Path, name: str, since: str, purpose: str, release: str = "unknown") -> Path:
    path = folder / name
    _ = path.write_text(json.dumps({"holder": name, "since": since, "for": purpose, "release_eta": release}) + "\n")
    return path


def recorded_hold_events(root: Path, expected: int) -> list[HoldEventRecord]:
    # Completion, not a polling interval, tells us all records are available.
    # The assertions at the call sites still check their expected contents/count.
    _ = expected
    finish = _finish_writers.get()
    if finish is not None:
        finish()
    rows = [cast(HoldEventRecord, json.loads(line))
            for path in root.rglob("[0-9][0-9][0-9][0-9]-*.jsonl") for line in path.read_text().splitlines()]
    return sorted(rows, key=lambda row: (row["at"], row["event"] != "began"))


class IsolatedBuildHoldTest(unittest.TestCase):
    scratch: Path = Path()
    socket_dir: Path = Path()
    lifetime_reader: int = -1
    lifetime_writer: int = -1

    seed: ClassVar[Path]
    case: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.seed = Path(temporary.name) / "seed"
        cls.case = Path(temporary.name) / "case"
        (cls.seed / "proc").mkdir(parents=True)
        (cls.seed / "child-lifetime").mkdir()
        _ = (cls.seed / "child-lifetime/sitecustomize.py").write_text(CHILD_LIFETIME)
        (cls.seed / "tools").mkdir()
        systemctl = cls.seed / "tools/systemctl"
        _ = systemctl.write_text("#!/bin/sh\nexit 0\n")
        systemctl.chmod(0o755)

    @override
    def setUp(self) -> None:
        self.scratch = self.case
        _ = shutil.copytree(self.seed, self.scratch)
        self.addCleanup(shutil.rmtree, self.scratch)
        self.socket_dir = Path(self.enterContext(short_socket_directory()))
        proc = self.scratch / "proc"
        _ = self.enterContext(mock.patch.object(build_hold, "PROC_ROOT", proc))
        lifetime = self.scratch / "child-lifetime"
        tools = self.scratch / "tools"
        fifo = lifetime / "writers"
        os.mkfifo(fifo)
        self.lifetime_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        # Held until cleanup, so the FIFO has had a writer and reports EOF
        # once the last one closes, even when a test starts no child.
        self.lifetime_writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch),
            "BUILDLOG_DIR": str(self.scratch / "buildlog"),
            "BUILD_HOLD_DIR": str(self.scratch / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
            "BUILD_HOLD_ENFORCER_DIR": str(self.scratch / "enforcer"),
            "BUILD_HOLD_CGROUP_ROOT": str(self.scratch / "cgroup"),
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "MAC_TEST_STATE_DIR": str(self.scratch / "mac-state"),
            **fake_memory_readings(self.scratch),
            # No test reads the machine's own session records, or passes for the session that runs it.
            "NOTIFIER_SESSIONS_DIR": str(self.scratch / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "",
            "UNIT_ACTIVE_DIR": str(self.scratch / "active"),
            "UNIT_RUN_DIR": "",
            "UNIT_SESSION_DIR": "",
            "BUILD_HOLD_TEST_LIFETIME_FIFO": str(fifo),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(lifetime), os.environ.get("PYTHONPATH", "")))),
        }))
        token = _finish_writers.set(self.finish_writers)
        self.addCleanup(_finish_writers.reset, token)
        self.addCleanup(self.reap_recorders)
        self.addCleanup(self.wait_for_detached_writers)

    def finish_writers(self) -> None:
        self.reap_recorders()
        self.wait_for_detached_writers()
        fifo = self.scratch / "child-lifetime/writers"
        self.lifetime_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.lifetime_writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)

    def wait_for_detached_writers(self) -> None:
        """Wait until no process a test started holds its FIFO writer.

        A child opens its writer only when its interpreter loads
        sitecustomize. Under load the FIFO can report EOF before a slow child
        gets there, and the read then finds that child's writer and no data,
        so wait again until a read returns EOF.
        """
        os.close(self.lifetime_writer)
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        try:
            while True:
                remaining = max(deadline - time.monotonic(), 0)
                readable, _, _ = select.select([self.lifetime_reader], [], [], remaining)
                self.assertTrue(readable, "Detached children did not finish before scratch cleanup")
                try:
                    if os.read(self.lifetime_reader, 1) == b"":
                        return
                except BlockingIOError:
                    pass
        finally:
            os.close(self.lifetime_reader)

    def reap_recorders(self) -> None:
        for thread in threading.enumerate():
            if thread.name == "hold-event-recorder":
                # A recorder runs a short subprocess; under load it can take far longer than its usual second.
                thread.join(timeout=TEST_HANG_GUARD_SECS)
                self.assertFalse(thread.is_alive())
