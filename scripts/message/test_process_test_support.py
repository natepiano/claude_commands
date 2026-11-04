"""Check process ownership before signals and reap adopted test descendants."""

from __future__ import annotations

import os
import ctypes
import errno
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from typing import cast

from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.message.process_test_support import OwnedTestProcesses
from scripts.message import process_test_support as support
from scripts.message import darwin_test_processes as darwin


class OwnedTestProcessesTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "The fixture simulates Linux process files.")
    def test_wait_stopped_accepts_a_process_that_exits_during_the_state_read(self) -> None:
        for error in (FileNotFoundError(), ProcessLookupError()):
            with self.subTest(error=type(error).__name__):
                owned = OwnedTestProcesses()
                owned.handles[123] = 456
                with (patch.object(support, "USE_PIDFD", True),
                      patch("select.select", return_value=([], [], [])),
                      patch.object(Path, "read_text", side_effect=error)):
                    owned.wait_stopped()

    @unittest.skipUnless(sys.platform == "linux", "The fixture simulates Linux process files.")
    def test_direct_children_accepts_a_task_that_exits_during_the_read(self) -> None:
        for error in (FileNotFoundError(), ProcessLookupError()):
            with self.subTest(error=type(error).__name__):
                with (patch.object(Path, "iterdir", return_value=iter([Path("/proc/self/task/123")])),
                      patch.object(Path, "read_text", side_effect=error)):
                    self.assertEqual(support.direct_children(), set())

    def test_darwin_state_accepts_a_process_that_exits_during_either_read(self) -> None:
        identity = darwin.OwnedProcessIdentity(123, 10, 1)
        for stage in ("identity", "state"):
            with self.subTest(stage=stage):
                owned = OwnedTestProcesses()
                owned.identities[identity.pid] = identity
                with (patch.object(darwin, "identity", return_value=identity,
                                   side_effect=ProcessLookupError() if stage == "identity" else None),
                      patch.object(darwin, "libproc", side_effect=ProcessLookupError()),
                      patch.object(support, "USE_PIDFD", False)):
                    self.assertEqual(darwin.process_state(identity), "")
                    owned.wait_stopped()

    def test_darwin_state_reads_libproc_without_starting_a_process(self) -> None:
        owned = darwin.OwnedProcessIdentity(123, 10, 1)
        status = darwin.SSTOP
        calls: list[tuple[int, int, int, int]] = []

        class Library:
            def proc_pidinfo(self, pid: int, flavor: int, argument: int,
                             buffer: object, size: int) -> int:
                calls.append((pid, flavor, argument, size))
                info = ctypes.cast(cast(ctypes.c_void_p, buffer),
                                   ctypes.POINTER(darwin.ProcessStateInfo)).contents
                info.status = status
                return size

        self.assertEqual(ctypes.sizeof(darwin.ProcessStateInfo), 136)
        with (patch.object(darwin, "identity", return_value=owned),
              patch.object(darwin, "libproc", return_value=Library()),
              patch.object(subprocess, "Popen", side_effect=AssertionError("cleanup started a process"))):
            for status, expected in ((darwin.SSTOP, "T"), (darwin.SZOMB, "Z"), (2, "R")):
                with self.subTest(status=status):
                    self.assertEqual(darwin.process_state(owned), expected)
        self.assertEqual(calls, [(123, darwin.PROC_PIDTBSDINFO, 1, 136)] * 3)

    def test_darwin_wait_stopped_uses_an_identity_snapshot(self) -> None:
        first = darwin.OwnedProcessIdentity(123, 10, 1)
        second = darwin.OwnedProcessIdentity(456, 11, 1)
        owned = OwnedTestProcesses()
        owned.identities[first.pid] = first

        def state(identity: darwin.OwnedProcessIdentity) -> str:
            self.assertEqual(identity, first)
            owned.identities[second.pid] = second
            return "T"

        with (patch.object(support, "USE_PIDFD", False),
              patch.object(darwin, "process_state", state)):
            owned.wait_stopped()
        self.assertEqual(set(owned.identities), {first.pid, second.pid})

    @unittest.skipUnless(sys.platform == "linux", "The fixture uses Linux pidfds for Darwin signals.")
    def test_darwin_cleanup_kills_and_reaps_with_popen_patched(self) -> None:
        owned = OwnedTestProcesses()
        self.addCleanup(owned.finish)
        children: list[subprocess.Popen[str]] = []
        for _ in range(2):
            child: subprocess.Popen[str] = subprocess.Popen(
                [sys.executable, "-c", "import signal; signal.pause()"],
                text=True, env=owned.environment())
            owned.track(child)
            children.append(child)
        identities = {child.pid: darwin.OwnedProcessIdentity(child.pid, child.pid, 1)
                      for child in children}
        owned.identities.update(identities)
        signals: list[tuple[int, int]] = []

        def identity(pid: int) -> darwin.OwnedProcessIdentity | None:
            return identities.get(pid) if Path(f"/proc/{pid}").exists() else None

        def send(process: darwin.OwnedProcessIdentity, signum: int) -> None:
            signal.pidfd_send_signal(owned.handles[process.pid], signum)
            signals.append((process.pid, signum))

        class Library:
            def proc_pidinfo(self, pid: int, flavor: int, argument: int,
                             buffer: object, size: int) -> int:
                self_test.assertEqual((flavor, argument), (darwin.PROC_PIDTBSDINFO, 1))
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except (FileNotFoundError, ProcessLookupError):
                    return 0
                info = ctypes.cast(cast(ctypes.c_void_p, buffer),
                                   ctypes.POINTER(darwin.ProcessStateInfo)).contents
                info.status = darwin.SSTOP if state in ("T", "t") else darwin.SZOMB if state == "Z" else 2
                return size

        self_test = self
        with (patch.object(support, "USE_PIDFD", False),
              patch.object(darwin, "identity", identity),
              patch.object(darwin, "user_processes", return_value=list(identities)),
              patch.object(darwin, "signal_identity", send),
              patch.object(darwin, "libproc", return_value=Library()),
              patch.object(subprocess, "Popen", side_effect=AssertionError("cleanup started a process"))):
            owned.finish()
        for child in children:
            self.assertIn((child.pid, signal.SIGSTOP), signals)
            self.assertIn((child.pid, signal.SIGKILL), signals)
            self.assertEqual(child.returncode, -signal.SIGKILL)
            self.assertFalse(Path(f"/proc/{child.pid}").exists())
            with self.assertRaises(ChildProcessError):
                _ = os.waitpid(child.pid, os.WNOHANG)
        self.assertFalse(owned.identities)

    def test_cleanup_never_signals_a_numeric_pid_after_communicate_reaps_it(self) -> None:
        owned = OwnedTestProcesses()
        self.addCleanup(owned.finish)
        child = subprocess.Popen([sys.executable, "-c", "pass"],
                                 text=True, env=owned.environment())
        owned.track(child)
        _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)
        with (patch.object(os, "kill", side_effect=AssertionError("numeric PID signal")),
              patch.object(os, "killpg", side_effect=AssertionError("numeric group signal"))):
            owned.finish()
        self.assertEqual(child.returncode, 0)
        self.assertFalse(Path(f"/proc/{child.pid}").exists())

    def test_cleanup_finds_a_detached_child_before_its_pid_is_recorded(self) -> None:
        owned = OwnedTestProcesses()
        self.addCleanup(owned.finish)
        child = subprocess.Popen([sys.executable, "-c",
            "import signal; print('ready', flush=True); signal.pause()"],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
            env=owned.environment())
        # An interrupt can prevent track() and the PID file write.
        try:
            with LauncherHangGuard("unrecorded test child").protect():
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline(), "ready\n")
        finally:
            owned.finish()
            if child.stdout is not None:
                child.stdout.close()
        self.assertIn(child.pid, owned.reaped)
        self.assertFalse(Path(f"/proc/{child.pid}").exists())
        _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_cleanup_finds_a_detached_worker_before_its_parent_records_the_pid(self) -> None:
        owned = OwnedTestProcesses()
        self.addCleanup(owned.finish)
        program = """import signal, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
                         start_new_session=True)
print(child.pid, flush=True)
# The parent stops before it can write the worker PID file.
signal.pause()
"""
        parent: subprocess.Popen[str] = subprocess.Popen([sys.executable, "-c", program],
            stdout=subprocess.PIPE, text=True, env=owned.environment())
        owned.track(parent)
        try:
            with LauncherHangGuard("worker before PID publication").protect():
                assert parent.stdout is not None
                output = cast(str, parent.stdout.readline())
                descendant = int(output)
        finally:
            owned.finish()
        self.assertIn(descendant, owned.reaped)
        self.assertFalse(Path(f"/proc/{descendant}").exists())
        self.assertFalse(Path(f"/proc/{parent.pid}").exists())

    def test_cleanup_reaps_a_detached_grandchild_after_its_parent_exits(self) -> None:
        owned = OwnedTestProcesses()
        self.addCleanup(owned.finish)
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "child"
            program = """import subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
                         start_new_session=True)
Path(sys.argv[1]).write_text(str(child.pid))
"""
            parent = subprocess.Popen([sys.executable, "-c", program, str(marker)],
                                      text=True, env=owned.environment())
            owned.track(parent)
            _ = parent.communicate(timeout=TEST_HANG_GUARD_SECS)
            descendant = int(marker.read_text())
            owned.finish()
            self.assertIn(descendant, owned.reaped)
            self.assertFalse(Path(f"/proc/{descendant}").exists())
            with self.assertRaises(ChildProcessError):
                _ = os.waitpid(descendant, os.WNOHANG)
        self.assertEqual(parent.returncode, 0)

    def test_sigint_during_cleanup_kills_and_reaps_before_it_raises(self) -> None:
        for explicit_exception in (False, True):
            with self.subTest(explicit_exception=explicit_exception):
                owned = OwnedTestProcesses()
                self.addCleanup(owned.finish)
                child: subprocess.Popen[str] = subprocess.Popen(
                    [sys.executable, "-c", "import signal; signal.pause()"],
                    text=True, env=owned.environment())
                owned.track(child)
                wait_stopped = owned.wait_stopped
                interrupted = False

                def interrupt_after_stop() -> None:
                    nonlocal interrupted
                    wait_stopped()
                    if not interrupted:
                        interrupted = True
                        if explicit_exception:
                            raise KeyboardInterrupt
                        signal.raise_signal(signal.SIGINT)

                previous = signal.getsignal(signal.SIGINT)
                with patch.object(owned, "wait_stopped", interrupt_after_stop):
                    with self.assertRaises(KeyboardInterrupt):
                        owned.finish()
                self.assertEqual(child.returncode, -signal.SIGKILL)
                self.assertEqual(signal.getsignal(signal.SIGINT), previous)
                with self.assertRaises(ChildProcessError):
                    _ = os.waitpid(child.pid, os.WNOHANG)
                self.assertFalse(owned.handles)

    @unittest.skipUnless(sys.platform == "linux", "The fixture uses a Linux pidfd to simulate Darwin signals.")
    def test_darwin_cleanup_kills_and_reaps_its_tracked_child(self) -> None:
        owned = OwnedTestProcesses()
        self.addCleanup(owned.finish)
        child: subprocess.Popen[str] = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            text=True, env=owned.environment())
        descriptor = os.pidfd_open(child.pid)
        self.addCleanup(os.close, descriptor)
        self.addCleanup(owned.finish)
        identity = darwin.OwnedProcessIdentity(child.pid, 10, 1)
        signals: list[int] = []

        def send(owned_identity: darwin.OwnedProcessIdentity, signum: int) -> None:
            self.assertEqual(owned_identity, identity)
            signal.pidfd_send_signal(descriptor, signum)
            signals.append(signum)

        def state(owned_identity: darwin.OwnedProcessIdentity) -> str:
            self.assertEqual(owned_identity, identity)
            return "" if child.returncode is not None else "T"

        with (patch.object(support, "USE_PIDFD", False),
              patch.object(darwin, "identity", return_value=identity),
              patch.object(darwin, "user_processes", return_value=[child.pid]),
              patch.object(darwin, "signal_identity", send),
              patch.object(darwin, "process_state", state)):
            owned.track(child)
            owned.finish()
        self.assertEqual(signals, [signal.SIGSTOP, signal.SIGKILL])
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertFalse(owned.identities)
        with self.assertRaises(ChildProcessError):
            _ = os.waitpid(child.pid, os.WNOHANG)

    def test_darwin_construction_does_not_use_linux_process_apis(self) -> None:
        with (patch.object(sys, "platform", "darwin"),
              patch.object(support, "USE_PIDFD", False),
              patch.object(darwin, "child_processes", return_value=set()),
              patch.object(Path, "iterdir", side_effect=AssertionError("Linux process scan")),
              patch.object(ctypes, "CDLL", side_effect=AssertionError("Linux prctl"))):
            owned = OwnedTestProcesses()
            self.assertEqual(owned.previous_children, set())

    def test_darwin_signal_rejects_a_reused_pid(self) -> None:
        owned = darwin.OwnedProcessIdentity(123, 10, 1)
        with (patch.object(darwin, "identity", return_value=darwin.OwnedProcessIdentity(123, 11, 1)),
              patch.object(darwin, "libproc", side_effect=AssertionError("signal to reused PID"))):
            darwin.signal_identity(owned, signal.SIGKILL)

    def test_darwin_signal_uses_the_pid_version_and_retries_an_exec(self) -> None:
        calls: list[tuple[int, int, int]] = []

        class Library:
            def proc_signal_with_audittoken(self, token: object, signum: int) -> int:
                values = ctypes.cast(cast(ctypes.c_void_p, token), ctypes.POINTER(ctypes.c_uint32))
                calls.append((values[5], values[7], signum))
                return errno.ESRCH if len(calls) == 1 else 0

        first = darwin.OwnedProcessIdentity(123, 10, 1)
        second = darwin.OwnedProcessIdentity(123, 10, 2)
        with (patch.object(darwin, "identity", side_effect=[first, second, second]),
              patch.object(darwin, "libproc", return_value=Library())):
            darwin.signal_identity(first, signal.SIGKILL)
        self.assertEqual(calls, [(123, 1, signal.SIGKILL), (123, 2, signal.SIGKILL)])


if __name__ == "__main__":
    _ = unittest.main()
