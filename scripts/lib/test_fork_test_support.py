"""Forked test children release their resources after interrupted startup."""

from __future__ import annotations

import os
import select
import signal
import unittest
from contextlib import ExitStack
from multiprocessing.context import ForkProcess
from unittest.mock import patch

from scripts.lib import fork_test_support
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class StartupInterrupted(Exception):
    """Stop the parent before it can establish the child's process group."""


class ForkChildrenTests(unittest.TestCase):
    def test_cleanup_reaps_a_child_before_it_sets_its_process_group(self) -> None:
        pids: list[int] = []
        setpgid = os.setpgid
        start = ForkProcess.start

        with ExitStack() as pipes:
            entered_read, entered_write = os.pipe()
            _ = pipes.callback(os.close, entered_read)
            _ = pipes.callback(os.close, entered_write)
            release_read, release_write = os.pipe()
            _ = pipes.callback(os.close, release_read)
            _ = pipes.callback(os.close, release_write)

            def defer_group(pid: int, group: int) -> None:
                if pid == 0:
                    os.close(entered_read)
                    os.close(release_write)
                    _ = os.write(entered_write, b"ready")
                    _ = os.read(release_read, 1)
                setpgid(pid, group)

            def interrupt_start(process: ForkProcess) -> None:
                start(process)
                pid = process.pid
                assert pid is not None
                pids.append(pid)
                self.assertTrue(select.select([entered_read], [], [], TEST_HANG_GUARD_SECS)[0])
                raise StartupInterrupted

            try:
                with (self.assertRaises(StartupInterrupted),
                      patch.object(os, "setpgid", defer_group),
                      patch.object(ForkProcess, "start", interrupt_start)):
                    with fork_test_support.ForkChildren() as children:
                        _ = children.start(lambda: 0)
                self.assertEqual(len(pids), 1)
                with self.assertRaises(ChildProcessError):
                    _ = os.waitpid(pids[0], os.WNOHANG)
            finally:
                for pid in pids:
                    try:
                        waited, _ = os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        continue
                    if waited == 0:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        _ = os.waitpid(pid, 0)

    def test_a_failed_start_closes_the_ready_pipe_and_output_files(self) -> None:
        before = set(os.listdir("/proc/self/fd"))
        with fork_test_support.ForkChildren() as children:
            reader, writer = children.pipe()
            with (self.assertRaisesRegex(OSError, "start interrupted"),
                  patch.object(ForkProcess, "start", side_effect=OSError("start interrupted"))):
                _ = children.start(lambda: 0, pass_fds=(writer,))
        for descriptor in (reader, writer):
            with self.assertRaises(OSError):
                _ = os.fstat(descriptor)
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)


if __name__ == "__main__":
    _ = unittest.main()
