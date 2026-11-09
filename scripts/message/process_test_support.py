"""End and reap the processes that an isolated test starts."""

from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.descendants import adopt_descendants
from scripts.message import darwin_test_processes as darwin


TestChild = subprocess.Popen[str] | subprocess.Popen[bytes]
OWNERS = "MESSAGE_TEST_PROCESS_OWNERS"
USE_PIDFD = sys.platform == "linux"


def adopt_test_descendants() -> None:
    """Let the test reap descendants after their immediate parents exit."""
    _ = adopt_descendants()


def direct_children() -> set[int]:
    result: set[int] = set()
    if sys.platform != "linux":
        return darwin.child_processes()
    for task in Path("/proc/self/task").iterdir():
        try:
            result.update(int(value) for value in (task / "children").read_text().split())
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


@dataclass
class OwnedTestProcesses:
    """Publish ownership before launch, then signal through pinned process handles."""

    children: list[TestChild] = field(default_factory=list)
    markers: list[Path] = field(default_factory=list)
    group_markers: list[Path] = field(default_factory=list)
    token: str = field(default_factory=lambda: uuid4().hex)
    handles: dict[int, int] = field(default_factory=dict)
    previous_children: set[int] = field(default_factory=direct_children)
    reaped: set[int] = field(default_factory=set)
    identities: dict[int, darwin.OwnedProcessIdentity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        adopt_test_descendants()

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Install the ownership marker before Popen can create a process."""
        values = dict(os.environ if base is None else base)
        values[OWNERS] = values.get(OWNERS, "") + ":" + self.token
        return values

    def track(self, child: TestChild, *, group: bool = False) -> None:
        del group  # A process group number does not prove process ownership.
        self.children.append(child)
        if not USE_PIDFD:
            current = darwin.identity(child.pid) if child.returncode is None else None
            if current is not None and child.poll() is None:
                self.identities[child.pid] = current
            return
        if child.returncode is not None:
            return
        try:
            descriptor = os.pidfd_open(child.pid)
            try:
                # An unreaped child cannot have a reused PID.
                _ = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except BaseException:
                os.close(descriptor)
                raise
        except (ProcessLookupError, ChildProcessError):
            return  # communicate() already reaped this child.
        previous = self.handles.get(child.pid)
        if previous is not None:
            os.close(previous)
        self.handles[child.pid] = descriptor

    def pin_descendants(self) -> bool:
        """Find inherited markers, including children started before PID publication."""
        if not USE_PIDFD:
            added = False
            for pid in darwin.user_processes():
                if pid == os.getpid() or pid in self.identities:
                    continue
                current = darwin.identity(pid)
                if current is not None and darwin.has_owner(pid, self.token):
                    after = darwin.identity(pid)
                    if after is not None and after.unique_id == current.unique_id:
                        self.identities[pid] = after
                        added = True
            return added
        added = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) in self.handles:
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                descriptor = os.pidfd_open(pid)
            except (ProcessLookupError, PermissionError):
                continue
            try:
                values = (entry / "environ").read_bytes().split(b"\0")
                owned = any(value.startswith((OWNERS + "=").encode())
                            and self.token.encode() in value.split(b"=", 1)[1].split(b":")
                            for value in values)
                if owned:
                    self.handles[pid] = descriptor
                    added = True
                    descriptor = -1
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass
            finally:
                if descriptor != -1:
                    os.close(descriptor)
        return added

    def signal_owned(self, signum: int) -> None:
        if not USE_PIDFD:
            for owned in tuple(self.identities.values()):
                darwin.signal_identity(owned, signum)
            return
        for descriptor in self.handles.values():
            try:
                signal.pidfd_send_signal(descriptor, signum)
            except ProcessLookupError:
                pass

    def wait_stopped(self) -> None:
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        if not USE_PIDFD:
            for owned in tuple(self.identities.values()):
                while (state := darwin.process_state(owned)) and state[0] not in ("T", "Z"):
                    if time.monotonic() >= deadline:
                        raise AssertionError(f"test process {owned.pid} did not stop")
            return
        for pid, descriptor in self.handles.items():
            while not select.select([descriptor], [], [], 0)[0]:
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if state in ("T", "t", "Z"):
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"test process {pid} did not stop")
                os.sched_yield()

    def finish(self) -> None:
        """Defer SIGINT until every owned process is killed and reaped."""
        interrupted = False

        def defer_interrupt(signum: int, frame: object) -> None:
            nonlocal interrupted
            del signum, frame
            interrupted = True

        main_thread = threading.current_thread() is threading.main_thread()
        previous = signal.getsignal(signal.SIGINT)
        if main_thread:
            _ = signal.signal(signal.SIGINT, defer_interrupt)
        try:
            while True:
                try:
                    if USE_PIDFD:
                        self.finish_pinned()
                    else:
                        self.finish_darwin()
                    break
                except KeyboardInterrupt:
                    interrupted = True
        finally:
            if main_thread:
                _ = signal.signal(signal.SIGINT, previous)
        if interrupted:
            raise KeyboardInterrupt

    def finish_darwin(self) -> None:
        """Use audit identities on macOS, where launchd reaps orphan descendants."""
        self.stop_and_kill()
        self.reap_children()
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        for owned in tuple(self.identities.values()):
            while darwin.process_state(owned):
                try:
                    pid, _ = os.waitpid(owned.pid, os.WNOHANG)
                    if pid:
                        self.reaped.add(pid)
                        break
                except ChildProcessError:
                    pass  # launchd reaps orphan descendants on macOS.
                if time.monotonic() >= deadline:
                    raise AssertionError(f"test process {owned.pid} remains after SIGKILL")
            self.reaped.add(owned.pid)
        for pid in direct_children() - self.previous_children:
            try:
                ended, _ = os.waitpid(pid, os.WNOHANG)
                if ended:
                    self.reaped.add(ended)
            except ChildProcessError:
                pass
        self.identities.clear()
        self.clear_children()

    def stop_and_kill(self) -> None:
        # Stop the writers before collecting descendants. Repeat until none can fork.
        self.signal_owned(signal.SIGSTOP)
        self.wait_stopped()
        while self.pin_descendants():
            self.signal_owned(signal.SIGSTOP)
            self.wait_stopped()
        self.signal_owned(signal.SIGKILL)

    def reap_children(self) -> None:
        for child in self.children:
            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
            self.reaped.add(child.pid)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def clear_children(self) -> None:
        self.children.clear()
        self.markers.clear()
        self.group_markers.clear()

    def finish_pinned(self) -> None:
        self.stop_and_kill()
        self.reap_children()
        for pid, descriptor in self.handles.items():
            ready, _, _ = select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)
            if not ready:
                raise AssertionError(f"test process {pid} survived SIGKILL")
        # All parents have exited, so their remaining descendants are now adopted.
        for pid in self.handles:
            if pid not in self.reaped:
                try:
                    _ = os.waitpid(pid, 0)
                    self.reaped.add(pid)
                except ChildProcessError:
                    # A fixture's parent or communicate() can reap it first.
                    if Path(f"/proc/{pid}").exists():
                        raise AssertionError(f"test descendant {pid} remains unreaped") from None
        # A child can exit before its marker write. Reap adopted zombies too.
        for pid in direct_children() - self.previous_children:
            try:
                result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                if Path(f"/proc/{pid}").exists():
                    raise AssertionError(f"test child {pid} was not reaped") from None
                continue
            if result is not None:
                _ = os.waitpid(pid, 0)
                self.reaped.add(pid)
        for descriptor in self.handles.values():
            os.close(descriptor)
        self.handles.clear()
        self.clear_children()
