"""A queued program owns one reservation from before launch through finalization."""
from __future__ import annotations

from scripts.lib import atomic_file, build_folders, cancellation

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Unpack, cast
from unittest import mock

from scripts.build_queue import queue_place as queue, admission_client as client, admission_service as service, admission_protocol as wire
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.queue_fixture import write_process
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.build_queue import admission_records as records, step_estimator
from scripts.lint import memory_admit as gate

from scripts.buildlog import store

from scripts.lint.memory_test_support import commit_config_fixture, fake_memory_readings, gate_isolation_environment
from scripts.build_queue.queue_fixture import write_memory_config

GIB = records.GIB
CHILD = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.exit(int(sys.argv[1]))"
LIFELINE_WAIT = "os.read(int(os.environ['QUEUE_TEST_LIFELINE']), 1)"
BLOCKING_CHILD = "import os,sys; print('ready', flush=True); " + LIFELINE_WAIT
TERM_RESISTANT_CHILD = ("import os,signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        + "print('ready',flush=True); " + LIFELINE_WAIT)
DESCENDANT_CHILD = """
import os, signal, sys
read_fd, write_fd = os.pipe()
descendant = os.fork()
if descendant == 0:
    os.close(read_fd)
    if len(sys.argv) > 2 and sys.argv[2] == 'detach':
        os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(write_fd, b'ready')
    os.close(write_fd)
    os.read(int(os.environ["QUEUE_TEST_LIFELINE"]), 1)
    os._exit(0)
os.close(write_fd)
os.read(read_fd, 5)
os.close(read_fd)
print(descendant, flush=True)
if sys.argv[1] == 'exit':
    sys.exit(0)
os.read(int(os.environ["QUEUE_TEST_LIFELINE"]), 1)
"""
BEFORE_SCOPE_START = """
import os, sys
ready_fd, continue_fd = map(int, sys.argv[1:3])
os.write(ready_fd, str(os.getpid()).encode())
os.close(ready_fd)
resume = os.read(continue_fd, 1)
os.close(continue_fd)
if resume != b'G':
    sys.exit(98)
os.execvp(sys.argv[3], sys.argv[3:])
"""
UNSAMPLED_DESCENDANT = """
import os, signal, sys
fork_fd, exit_fd = map(int, sys.argv[1:3])
print(os.getpid(), flush=True)
if os.read(fork_fd, 1) != b'F':
    sys.exit(98)
os.close(fork_fd)
ready_read, ready_write = os.pipe()
descendant = os.fork()
if descendant == 0:
    os.close(ready_read)
    os.close(exit_fd)
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(ready_write, b'R')
    os.close(ready_write)
    os.read(int(os.environ["QUEUE_TEST_LIFELINE"]), 1)
    os._exit(0)
os.close(ready_write)
os.read(ready_read, 1)
os.close(ready_read)
print(descendant, flush=True)
if os.read(exit_fd, 1) != b'E':
    sys.exit(98)
os.close(exit_fd)
os._exit(0)
"""


def helper_lifeline_owner() -> None:
    """Start helpers with one pipe writer owned by this process."""
    read_fd, write_fd = os.pipe()
    os.environ["QUEUE_TEST_LIFELINE"] = str(read_fd)
    children: list[subprocess.Popen[bytes]] = []
    pids: list[int] = []
    for source, arguments in ((BLOCKING_CHILD, []), (TERM_RESISTANT_CHILD, []),
                              (DESCENDANT_CHILD, ["term", "group"]),
                              (DESCENDANT_CHILD, ["term", "detach"]),
                              (DESCENDANT_CHILD, ["exit", "detach"])):
        child = subprocess.Popen([sys.executable, "-c", source, *arguments],
                                 pass_fds=(read_fd,), stdout=subprocess.PIPE)
        children.append(child)
        assert child.stdout is not None
        if not select.select([child.stdout], [], [], 3)[0]:
            raise AssertionError("helper did not become ready")
        ready = cast(bytes, child.stdout.readline())
        if source in (BLOCKING_CHILD, TERM_RESISTANT_CHILD):
            pids.append(child.pid)
        else:
            pids.append(int(ready))
            if arguments[0] == "exit":
                _ = child.wait(timeout=3)
            else:
                pids.append(child.pid)
    fork_read, fork_write = os.pipe()
    exit_read, exit_write = os.pipe()
    child = subprocess.Popen([sys.executable, "-c", UNSAMPLED_DESCENDANT, str(fork_read), str(exit_read)],
                             pass_fds=(read_fd, fork_read, exit_read), stdout=subprocess.PIPE)
    children.append(child)
    assert child.stdout is not None
    _ = cast(bytes, child.stdout.readline())
    _ = os.write(fork_write, b"F")
    pids.append(int(cast(bytes, child.stdout.readline())))
    _ = os.write(exit_write, b"E")
    _ = child.wait(timeout=3)
    for descriptor in (fork_read, fork_write, exit_read, exit_write):
        os.close(descriptor)
    print(json.dumps(pids), flush=True)
    os.close(read_fd)
    # Keep the only writer open until the regression kills this process.
    _ = os.read(sys.stdin.fileno(), 1)
    os.close(write_fd)


class HelperLifelineTests(unittest.TestCase):
    def test_helpers_exit_after_the_test_process_is_killed(self) -> None:
        owner = subprocess.Popen([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c",
            "from scripts.build_queue.test_queue_place import helper_lifeline_owner; helper_lifeline_owner()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        pids: list[int] = []
        descriptors: list[int] = []
        try:
            assert owner.stdout is not None
            self.assertTrue(select.select([owner.stdout], [], [], 10)[0], "helper owner did not become ready")
            pids = cast(list[int], json.loads(cast(bytes, owner.stdout.readline())))
            descriptors = [os.pidfd_open(pid) for pid in pids]
            owner.kill()
            self.assertEqual(owner.wait(timeout=3), -signal.SIGKILL)
            pending = set(descriptors)
            deadline = time.monotonic() + 3
            while pending and (remaining := deadline - time.monotonic()) > 0:
                ready, _, _ = select.select(list(pending), [], [], remaining)
                pending.difference_update(ready)
            self.assertFalse(pending, "helpers survived the test process")
        finally:
            if owner.poll() is None:
                owner.kill()
            _ = owner.wait(timeout=3)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    _ = os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            for descriptor in descriptors:
                os.close(descriptor)
            for stream in (owner.stdin, owner.stdout):
                if stream is not None:
                    stream.close()


def private_scope_start() -> None:
    """Use the real startup rule with a private kernel path at its input boundary."""
    private_slice, action, sidecar, holder, start, record, *argv = sys.argv[1:]
    if action != "--scope-start":
        raise AssertionError("the private scope wrapper received another action")
    cgroup_path = Path("/proc/self/cgroup")
    actual = cgroup_path.read_text()
    if not any(private_slice in Path(line[3:]).parts for line in actual.splitlines() if line.startswith("0::")):
        raise AssertionError("the program did not enter the private test slice")
    read_text = Path.read_text
    publish = atomic_file.atomic_sample

    def kernel_path(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == cgroup_path:
            return actual.replace("0::/", "0::/builds.slice/")
        return read_text(path, encoding=encoding, errors=errors)

    def publish_actual_path(path: Path, value: str) -> None:
        publish(path, value.replace("/sys/fs/cgroup/builds.slice/", "/sys/fs/cgroup/", 1))

    with mock.patch.object(Path, "read_text", new=kernel_path), \
         mock.patch.object(atomic_file, "atomic_sample", side_effect=publish_actual_path):
        queue.scope_start(Path(sidecar), int(holder), start, argv, record=Path(record))


class QueuePlaceFixture(ExitStack):
    """Private config, ledger and memory files shared by all helper scenarios."""

    def __init__(self, *, private_slice: str = "") -> None:
        super().__init__()
        self.private_slice: str = private_slice or f"queueplacetest{os.getpid()}{time.monotonic_ns()}.slice"
        self.enter_context(LauncherHangGuard("queue place fixture").protect())
        self.lifeline_read: int
        self.lifeline_write: int
        self.lifeline_read, self.lifeline_write = os.pipe()
        _ = self.callback(os.close, self.lifeline_read)
        _ = self.callback(os.close, self.lifeline_write)
        self.root: Path = Path(self.enter_context(tempfile.TemporaryDirectory(
            prefix="queue-place-test-", dir=os.environ.get("TMPDIR"))))
        self.log: Path = self.root / "log"
        self.proc: Path = self.root / "proc"
        self.proc.mkdir()
        self.config: Path = self.root / "build_admission.conf"
        write_memory_config(self.config, **{"program_first_peak_gib.rust-analyzer": "18"})
        self.enter_context(mock.patch.dict(os.environ, {
            **gate_isolation_environment(self.root), **fake_memory_readings(self.root),
            "LINT_CONFIG_FILE": str(self.config.with_name("lint.conf")), "BUILDLOG_DIR": str(self.log),
            "BUILDLOG_PROC": str(self.proc),
            "BUILDLOG_SCOPE": "0", "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "UNIT_SESSION_DIR": "", "UNIT_RUN_DIR": "", "CLAUDE_CODE_SESSION_ID": "queue-place-test",
            "QUEUE_TEST_LIFELINE": str(self.lifeline_read),
        }))
        self.admission: AdmissionFixture = self.enter_context(AdmissionFixture(root=self.root))
        self.admission.clock.epoch = time.time()
        _ = self.enter_context(self.admission.serving())
        self.children: list[subprocess.Popen[bytes]] = []
        self.complete_scoped_launch: bool = False
        self.supply_process(os.getpid(), 0, parent=1)
        spawn = subprocess.Popen

        def start(argv: list[str], **kwargs: Unpack[queue.PopenArguments]) -> subprocess.Popen[bytes]:
            if "--scope-start" in argv:
                index = argv.index("--scope-start") - 2
                argv = [*argv[:index], str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c",
                        "from scripts.build_queue.test_queue_place import private_scope_start; private_scope_start()",
                        self.private_slice, *argv[index + 2:]]
            if argv[:1] != ["git"]:
                kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), self.lifeline_read)
            child = spawn(argv, **kwargs)
            # Git reads config history and does not start the queued program.
            if argv[:1] == ["git"]:
                return child
            self.children.append(child)
            self.supply_process(child.pid, GIB)
            if self.complete_scoped_launch and argv[0] == "systemd-run":
                _ = child.wait(timeout=3)
            return child

        _ = self.enter_context(mock.patch.object(subprocess, "Popen", side_effect=start))
        _ = self.callback(self.stop_children)

    def use_private_scope(self) -> None:
        """Keep the real user manager outside the live build slice."""
        prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                  "--expand-environment=no", f"--slice={self.private_slice}", "--"]
        probe = subprocess.run([*prefix, "true"], capture_output=True, timeout=10, check=False)
        if probe.returncode:
            raise unittest.SkipTest("the user manager cannot create a private test slice")
        _ = self.enter_context(mock.patch.object(queue, "probe_scope_prefix", return_value=prefix))
        holder_scope = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                            if line.startswith("0::"))
        if self.private_slice not in Path(holder_scope).parts:
            _ = self.callback(subprocess.run, ["systemctl", "--user", "stop", self.private_slice],
                              capture_output=True, timeout=3, check=False)

    @property
    def directory(self) -> Path:
        return gate.admission_dir(self.log)

    def supply_process(self, pid: int, used: int, *, parent: int | None = None) -> None:
        write_process(self.proc, pid, used)
        path = self.proc / str(pid) / "stat"
        fields = path.read_text().rsplit(") ", 1)[1].split()
        fields[1] = str(os.getpid() if parent is None else parent)
        fields[19] = records.process_start_time(pid) or "gone"
        _ = path.write_text(f"{pid} (fixture) {' '.join(fields)}\n")

    def stop_children(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            _ = child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def reservation(self) -> tuple[Path, records.Reservation]:
        record, = self.admission.server.reservations.values()
        path, row = Path(record.path), service.reservation_row(record)
        if not row.get("reservation_id"):
            raise AssertionError("place has no finalization identity")
        return path, row

    def history(self) -> list[dict[str, object]]:
        path = self.directory / "anon_peaks.jsonl"
        return list(step_estimator.anonymous_history_rows(path))

    def seed(self, values: list[int], *, program: str = "rust-analyzer", host: str = "") -> None:
        with (self.directory / "anon_peaks.jsonl").open("a") as output:
            for value in values:
                _ = output.write(json.dumps({"step": program, "host": host or store.host_name(),
                    "anon_peak_bytes": value, "ended_at": datetime.now(UTC).isoformat()}) + "\n")

    def assert_cleaned(self, test: unittest.TestCase) -> None:
        test.assertFalse(self.admission.server.reservations)
        test.assertFalse(self.admission.server.waiters)
        for suffix in ("reservation", "waiting", "max", "wave"):
            test.assertEqual(list(self.directory.glob(f"*.{suffix}")), [], suffix)
        tripwire = Path(os.environ["BUILDLOG_LIVE_MEMORY_TRIPWIRE"])
        test.assertFalse(tripwire.exists() and tripwire.read_text(), "live memory tripwire fired")

    def assert_released(self, test: unittest.TestCase, reservation_id: str, *, status: int | None = None,
                        outcome: str | None = None) -> None:
        self.assert_cleaned(test)
        rows = [row for row in self.history() if row.get("reservation_id") == reservation_id]
        test.assertEqual(len(rows), 1, rows)
        if status is not None:
            test.assertEqual(rows[0].get("status"), status)
        if outcome is not None:
            test.assertEqual(rows[0].get("outcome"), outcome)

    def start_blocked(self, place: queue.QueuePlace) -> subprocess.Popen[bytes]:
        child = place.popen([sys.executable, "-c", BLOCKING_CHILD], stdout=subprocess.PIPE)
        assert child.stdout is not None
        if not select.select([child.stdout], [], [], 3)[0] or child.stdout.readline() != b"ready\n":
            raise AssertionError("short program never became ready")
        return child

    def supply_scope_memory(self, anon_bytes: int, file_bytes: int = 0) -> None:
        read_text = Path.read_text
        guard = gate.check_memory_reading
        readings = {"memory.stat": f"anon {anon_bytes}\nshmem 0\nfile {file_bytes}\ninactive_file 0\n",
                    "memory.current": str(anon_bytes + file_bytes), "memory.swap.current": "0"}

        def supplied_text(path: Path, *args: object, **kwargs: object) -> str:
            if path.is_relative_to("/sys/fs/cgroup") and path.name in readings:
                return readings[path.name]
            return read_text(path, *args, **kwargs)  # pyright: ignore[reportArgumentType]

        def membership_or_private_sample(path: Path) -> None:
            if path.is_relative_to("/sys/fs/cgroup") and (path.name in readings or path.name == "cgroup.procs"):
                return
            guard(path)

        _ = self.enter_context(mock.patch.object(Path, "read_text", autospec=True, side_effect=supplied_text))
        _ = self.enter_context(mock.patch.object(gate, "check_memory_reading", side_effect=membership_or_private_sample))


class QueuePlaceTests(unittest.TestCase):
    def test_fixture_removes_child_folders_after_exit_term_int_and_kill(self) -> None:
        source = """
from pathlib import Path
import sys
from scripts.build_queue.queue_place import queue_place
with queue_place('rust-analyzer', Path(sys.argv[1])) as place:
    print(place.sidecar.parent, flush=True)
    sys.stdin.buffer.read(1)
"""
        for ending in (0, signal.SIGTERM, signal.SIGINT, signal.SIGKILL):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory(prefix="queue-caller-") as directory:
                caller = Path(directory)
                with mock.patch.dict(os.environ, {"TMPDIR": str(caller)}):
                    with QueuePlaceFixture() as fixture:
                        child = subprocess.Popen([sys.executable, "-c", source, str(fixture.root)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                        assert child.stdout is not None
                        self.assertTrue(select.select([child.stdout], [], [], TEST_HANG_GUARD_SECS)[0])
                        folder = Path(cast(bytes, child.stdout.readline()).decode().strip())
                        self.assertTrue(folder.is_relative_to(fixture.admission.temporary))
                        self.assertTrue(folder.is_dir())
                        if ending:
                            child.send_signal(ending)
                        else:
                            assert child.stdin is not None
                            _ = child.stdin.write(b"G")
                            child.stdin.flush()
                        status = child.wait(timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(status, -ending if ending in (signal.SIGTERM, signal.SIGKILL)
                                         else -signal.SIGINT if ending == signal.SIGINT else 0)
                        self.assertEqual(folder.exists(), ending in (signal.SIGTERM, signal.SIGKILL))
                        with queue.queue_place("rust-analyzer", fixture.root):
                            pass
                        self.assertFalse(folder.exists(), "the next start kept a dead holder's folder")
                    self.assertEqual(list(caller.iterdir()), [])

    def test_scope_sampler_uses_the_shared_anonymous_reader_and_skips_unavailable_use(self) -> None:
        from scripts.lint.memory_test_support import admission_configuration_fixture

        class SingleScopeSample(queue.QueuePlace):
            def sample_once(self) -> None:
                stopped = threading.Event()
                stopped.set()
                self._sample(queue.ScopedProgram(), stopped)

        with tempfile.TemporaryDirectory(prefix="scope-sample-test-") as directory:
            root = Path(directory)
            scope = root / "program.scope"
            scope.mkdir()
            _ = (scope / "memory.current").write_text(str(11 * GIB))
            _ = (scope / "memory.swap.current").write_text(str(2 * GIB))
            sidecar = root / "program.cgroup"
            _ = sidecar.write_text(str(scope) + "\n")
            for available in (True, False):
                with self.subTest(anonymous_available=available):
                    contents = f"file {7 * GIB}\ninactive_file {GIB}\n"
                    if available:
                        contents += f"anon {3 * GIB}\nshmem 0\n"
                    _ = (scope / "memory.stat").write_text(contents)
                    samples = root / str(available)
                    samples.mkdir()
                    place = SingleScopeSample(samples / "program", sidecar,
                                              configuration=admission_configuration_fixture())
                    with mock.patch.object(gate, "slice_working_set", wraps=gate.slice_working_set) as reader, \
                         mock.patch.object(place, "observe", wraps=place.observe) as observe:
                        place.sample_once()
                    reader.assert_called_once_with(scope)
                    if available:
                        observe.assert_called_once_with(3 * GIB, 3 * GIB)
                        self.assertEqual(place.peak_bytes, 3 * GIB)
                        self.assertEqual(int(place.path.with_suffix(".max").read_text()), 3 * GIB)
                    else:
                        observe.assert_not_called()
                        self.assertEqual(place.peak_bytes, 0)
                        self.assertEqual(list(samples.iterdir()), [])

    def test_program_admission_carries_the_unit_override(self) -> None:
        with QueuePlaceFixture() as fixture, mock.patch.dict(os.environ, {records.BUILDLOG_UNIT: "queue-unit"}):
            with queue.queue_place("rust-analyzer", fixture.root):
                _, row = fixture.reservation()
                self.assertIn("queue_waiter", row)
                assert "queue_waiter" in row
                waiter = row["queue_waiter"]
                self.assertEqual(waiter.get("unit"), "queue-unit")
                self.assertEqual(waiter.get("session_id"), records.waiter_identity("", "")["session_id"])

    def analyzer(self, fixture: QueuePlaceFixture, source: str) -> Path:
        executable = fixture.root / "rust-analyzer"
        _ = executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o755)
        return executable

    def test_version_probe_starts_without_admission(self) -> None:
        with QueuePlaceFixture() as fixture:
            executable = self.analyzer(fixture, "print('private analyzer version')\n")
            with fixture.admission.loop_turn():
                fixture.admission.free(0)
                older = fixture.admission.request(age=900)
                _ = fixture.admission.server.handle_request(older)
                self.assertTrue(fixture.admission.server.waiters)
                saved = fixture.admission.state_text()
            with mock.patch.object(client, "admit", side_effect=AssertionError("probe requested admission")), \
                 mock.patch.object(client, "release", side_effect=AssertionError("probe released admission")), \
                 mock.patch.object(queue, "probe_scope_prefix", side_effect=AssertionError("probe requested a scope")), \
                 mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}):
                argv = [str(executable), "--version"]
                started = time.monotonic()
                with queue.queue_place(argv, fixture.root) as place:
                    process = place.popen(argv, stdout=subprocess.PIPE)
                    output, _ = process.communicate(timeout=2)
                    self.assertEqual(process.returncode, 0)
                    self.assertEqual(output, b"private analyzer version\n")
                    self.assertEqual(place.peak_bytes, 0)
                    self.assertFalse(place.path.with_suffix(".max").exists())
                self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(fixture.admission.state_text(), saved)
            self.assertFalse(fixture.admission.server.reservations)
            self.assertEqual(fixture.history(), [])

    def test_probe_arguments_are_exact_and_read_from_configuration(self) -> None:
        with QueuePlaceFixture() as fixture:
            for argument in ("--version", "--help", "-V", "-h"):
                self.assertEqual(queue.program_purpose(["/tools/rust-analyzer", argument]), queue.ProgramPurpose.PROBE)
            for argv in (["rust-analyzer", "--version", "analysis-stats"],
                         ["rust-analyzer", "analysis-stats", "--help"],
                         ["rust-analyzer", "--version=other"], ["cargo", "--version"]):
                self.assertEqual(queue.program_purpose(argv), queue.ProgramPurpose.BATCH)
            write_memory_config(fixture.config, **{"program_probe_arguments.rust-analyzer": "--private-probe"})
            self.assertEqual(queue.program_purpose(["rust-analyzer", "--private-probe"]), queue.ProgramPurpose.PROBE)
            self.assertEqual(queue.program_purpose(["rust-analyzer", "--version"]), queue.ProgramPurpose.BATCH)
            self.assertEqual(queue.program_purpose(["rust-analyzer"]), queue.ProgramPurpose.EDITOR_SERVER)
            self.assertEqual(queue.program_purpose(["rust-analyzer"], batch=True), queue.ProgramPurpose.BATCH)
            with self.assertRaisesRegex(queue.QueuePlaceRefused, "empty"):
                _ = queue.program_purpose([])

    def test_editor_server_starts_while_builds_wait(self) -> None:
        with QueuePlaceFixture() as fixture:
            fixture.use_private_scope()
            fixture.supply_scope_memory(2 * GIB)
            with fixture.admission.loop_turn():
                fixture.admission.free(0)
                older = fixture.admission.request(age=900)
                _ = fixture.admission.server.handle_request(older)
                self.assertTrue(fixture.admission.server.waiters)
                _ = (fixture.root / "pressure").write_text(
                    "some avg10=50.0 avg60=50.0 avg300=50.0 total=1\n"
                    + "full avg10=50.0 avg60=50.0 avg300=50.0 total=1\n")
                # Editor startup does not require a configured fallback.
                write_memory_config(fixture.config, **{"program_first_peak_gib.rust-analyzer": None})
            executable = self.analyzer(fixture, BLOCKING_CHILD)
            argv = [str(executable)]
            with mock.patch.object(client, "admit", side_effect=AssertionError("editor joined the FIFO")), \
                 mock.patch.object(client, "release", side_effect=AssertionError("editor released a reservation")), \
                 mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}):
                with queue.queue_place(argv, fixture.root) as place:
                    started = time.monotonic()
                    child = place.popen(argv, stdout=subprocess.PIPE)
                    assert child.stdout is not None
                    self.assertTrue(select.select([child.stdout], [], [], 2)[0], "editor startup exceeded two seconds")
                    self.assertEqual(child.stdout.readline(), b"ready\n")
                    self.assertLess(time.monotonic() - started, 2)
                    scope = Path(place.sidecar.read_text().strip())
                    self.assertIn(child.pid, gate.scope_members(scope))
                    self.assertFalse(fixture.admission.server.reservations)
                    place.observe(2 * GIB, 2 * GIB)
                    self.assertEqual(place.peak_bytes, 2 * GIB)
            self.assertIsNotNone(child.poll())
            self.assertEqual(gate.scope_members(scope), set())
            self.assertEqual(fixture.history(), [])

    def test_probe_history_does_not_size_server(self) -> None:
        with QueuePlaceFixture() as fixture:
            executable = self.analyzer(fixture, "print('probe')\n")
            fixture.seed([4 * GIB] * 5)
            history = fixture.history()
            argv = [str(executable), "--version"]
            with queue.queue_place(argv, fixture.root) as place:
                process = place.popen(argv, stdout=subprocess.DEVNULL)
                self.assertEqual(process.wait(timeout=2), 0)
            self.assertEqual(fixture.history(), history)
            with mock.patch.object(step_estimator, "program_peak_estimate", side_effect=AssertionError("editor sized from history")):
                with queue.queue_place([str(executable)], fixture.root) as place:
                    self.assertEqual(place.purpose, queue.ProgramPurpose.EDITOR_SERVER)
                    self.assertFalse(fixture.admission.server.reservations)
            with queue.queue_place([str(executable), "analysis-stats"], fixture.root) as place:
                _, row = fixture.reservation()
                self.assertEqual(row["need"], 4 * GIB)
                self.assertEqual(place.arguments, (str(executable), "analysis-stats"))

    def test_batch_admission_receives_the_complete_command(self) -> None:
        with QueuePlaceFixture() as fixture:
            argv = ["rust-analyzer", "analysis-stats", ".", "--no-sysroot"]
            with mock.patch.object(client, "step_request", wraps=client.step_request) as requests:
                with queue.queue_place(argv, fixture.root) as place:
                    _, row = fixture.reservation()
                    self.assertEqual(row["need"], 18 * GIB)
                    self.assertEqual(place.purpose, queue.ProgramPurpose.BATCH)
                    with self.assertRaisesRegex(queue.QueuePlaceRefused, "arguments changed"):
                        _ = place.popen(["rust-analyzer", "--version"])
            self.assertTrue(requests.call_args_list)
            self.assertTrue(all(call.args[0] == argv for call in requests.call_args_list))

    def require_transient_scope(self) -> None:
        if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
            reason = "transient scope test needs cgroup v2 (not available on the Mac)"
            print(reason, file=sys.stderr)
            self.skipTest(reason)
        with AdmissionFixture() as support:
            _, _, stop = support.transient_scope()
            stop()

    def test_scope_cleanup_and_release_finish_after_configuration_becomes_unusable(self) -> None:
        with QueuePlaceFixture() as fixture:
            fixture.use_private_scope()
            fixture.supply_scope_memory(2 * GIB)
            with mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                    mock.patch.object(queue, "in_builds_slice", return_value=False):
                with queue.queue_place("rust-analyzer", fixture.root) as place:
                    child = fixture.start_blocked(place)
                    sidecar = place.sidecar
                    scope = Path(sidecar.read_text().strip())
                    self.assertIn(child.pid, gate.scope_members(scope))
                    write_memory_config(fixture.config, program_stop_seconds="invalid")
            self.assertIsNotNone(child.poll())
            self.assertEqual(gate.scope_members(scope), set())
            fixture.assert_cleaned(self)
            self.assertFalse(sidecar.exists())

    def test_failed_scope_probe_refuses_before_starting_a_program(self) -> None:
        failures = (FileNotFoundError("systemd-run is unavailable"),
                    subprocess.TimeoutExpired("systemd-run", 10),
                    subprocess.CompletedProcess[bytes](["systemd-run"], 1, stdout=b"", stderr=b""))
        for failure in failures:
            with self.subTest(failure=failure), QueuePlaceFixture() as fixture:
                marker = fixture.root / "program-ran"
                with mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                     mock.patch.object(queue, "in_builds_slice", return_value=False), \
                     mock.patch.object(subprocess, "run") as probe, redirect_stderr(io.StringIO()):
                    if isinstance(failure, subprocess.CompletedProcess):
                        probe.return_value = failure
                    else:
                        probe.side_effect = failure
                    with queue.queue_place("rust-analyzer", fixture.root) as place:
                        with self.assertRaises(queue.QueuePlaceRefused):
                            _ = place.popen([sys.executable, "-c",
                                "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)])
                        self.assertFalse(marker.exists(), "the refused program executed")
                        self.assertEqual(fixture.children, [], "the failed probe still started a program")
                        _ = fixture.reservation()
                fixture.assert_cleaned(self)
                self.assertEqual(fixture.history(), [], "the refused program recorded a measured peak")

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "nested scope exit checks need Linux pidfds")
    def test_nested_scope_stops_an_unsampled_setsid_descendant_before_release(self) -> None:
        self.assert_nested_scope_cleanup(fork_during_scope_read=False)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "nested scope exit checks need Linux pidfds")
    def test_scope_empties_before_release_when_a_parent_forks_during_cleanup(self) -> None:
        self.assert_nested_scope_cleanup(fork_during_scope_read=True)

    def assert_nested_scope_cleanup(self, *, fork_during_scope_read: bool) -> None:
        self.require_transient_scope()
        private_slice = f"queueplacetest{os.getpid()}{time.monotonic_ns()}.slice"
        unit = f"queue-place-holder-{os.getpid()}-{time.monotonic_ns()}.scope"
        command = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                   "--expand-environment=no", f"--slice={private_slice}", f"--unit={unit}", "--",
                   str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", "from scripts.build_queue.test_queue_place import QueuePlaceTests; "
                   + "QueuePlaceTests().assert_nested_scope_stops_unsampled_descendant("
                   + f"fork_during_scope_read={fork_during_scope_read!r}, private_slice={private_slice!r})"]
        try:
            result = subprocess.run(command, capture_output=True, timeout=35, check=False,
                                    cwd=Path(__file__).resolve().parents[2])
            self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        finally:
            _ = subprocess.run(["systemctl", "--user", "stop", unit, private_slice],
                               capture_output=True, timeout=3, check=False)

    def assert_nested_scope_stops_unsampled_descendant(self, *, fork_during_scope_read: bool = False,
                                                      private_slice: str) -> None:
        holder_scope = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                            if line.startswith("0::"))
        self.assertIn(private_slice, Path(holder_scope).parts, "the holder did not enter its private test slice")
        with QueuePlaceFixture(private_slice=private_slice) as fixture:
            fixture.use_private_scope()
            fixture.supply_scope_memory(3 * GIB)
            write_memory_config(fixture.config, program_stop_seconds="0.1",
                                **{"program_first_peak_gib.rust-analyzer": "18"})
            sampler_started = threading.Event()
            fork_read, fork_write = os.pipe()
            exit_read, exit_write = os.pipe()
            leader_descriptor = -1
            descendant_descriptor = -1
            descendant = 0
            scope_name = ""
            raced_scope_read = False
            release = client.release
            read_text = Path.read_text

            def hold_sampler(_tracking: object, stopped: threading.Event) -> None:
                sampler_started.set()
                if not stopped.wait(15):
                    raise AssertionError("the sampler barrier exceeded its test deadline")

            def release_after_descendant_exit(path: str, status: int, configuration: records.MemoryConfig | wire.CleanupTiming) -> None:
                if fork_during_scope_read:
                    self.assertTrue(raced_scope_read, "cleanup did not read the scope before release")
                    scope_pids = {int(pid) for procs in Path(scope_name).rglob("cgroup.procs")
                                  for pid in read_text(procs).split()}
                    self.assertFalse(scope_pids, "the reservation ended while the scope held a process")
                if descendant_descriptor >= 0:
                    self.assertTrue(select.select([descendant_descriptor], [], [], 0)[0],
                                    "the reservation ended while its detached descendant still ran")
                _ = release(path, status, configuration)

            def fork_and_exit() -> None:
                nonlocal descendant, descendant_descriptor
                assert child.stdout is not None
                _ = os.write(fork_write, b"F")
                self.assertTrue(select.select([child.stdout], [], [], 3)[0])
                descendant = int(cast(bytes, child.stdout.readline()))
                descendant_descriptor = os.pidfd_open(descendant)
                self.assertEqual(os.getsid(descendant), descendant)
                self.assertNotEqual(os.getpgid(descendant), leader)
                _ = os.write(exit_write, b"E")
                self.assertTrue(select.select([leader_descriptor], [], [], 3)[0],
                                "the parent did not exit before the first sample")
                self.assertFalse(select.select([descendant_descriptor], [], [], 0)[0])

            def fork_after_scope_snapshot(path: Path, encoding: str | None = None,
                                          errors: str | None = None) -> str:
                nonlocal raced_scope_read
                snapshot = read_text(path, encoding=encoding, errors=errors)
                if path == Path(scope_name) / "cgroup.procs" and not raced_scope_read:
                    raced_scope_read = True
                    self.assertIn(str(leader), snapshot.split(), "the snapshot did not contain the parent")
                    fork_and_exit()
                    self.assertEqual(child.wait(timeout=3), 0, "the leader did not preserve its exit status")
                    self.assertNotIn(str(descendant), snapshot.split(), "the snapshot already contained the child")
                    self.assertIsNone(records.process_start_time(leader), "the parent still had a process identity")
                return snapshot

            try:
                with mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                     mock.patch.object(queue.QueuePlace, "_sample", side_effect=hold_sampler), \
                     mock.patch.object(client, "release", side_effect=release_after_descendant_exit) as released:
                    with queue.queue_place("rust-analyzer", fixture.root) as place:
                        child = place.popen([sys.executable, "-c", UNSAMPLED_DESCENDANT,
                                             str(fork_read), str(exit_read)],
                                            pass_fds=(fork_read, exit_read), stdout=subprocess.PIPE)
                        self.assertTrue(sampler_started.wait(3), "the sampler never reached its barrier")
                        assert child.stdout is not None
                        self.assertTrue(select.select([child.stdout], [], [], 3)[0])
                        leader = int(cast(bytes, child.stdout.readline()))
                        leader_descriptor = os.pidfd_open(leader)
                        _, row = fixture.reservation()
                        scope_name = Path(row["sidecar"]).read_text().strip()
                        directory = Path(scope_name)
                        self.assertTrue(directory.is_relative_to("/sys/fs/cgroup"))
                        self.assertIn(fixture.private_slice, Path(scope_name).parts)
                        self.assertNotEqual(directory, Path("/sys/fs/cgroup") / holder_scope.lstrip("/"))
                        scope_pids = {int(pid) for path in directory.rglob("cgroup.procs")
                                      for pid in path.read_text().split()}
                        self.assertIn(leader, scope_pids)
                        if not fork_during_scope_read:
                            fork_and_exit()
                            self.assertIn(str(descendant), (directory / "cgroup.procs").read_text().split())
                        self.assertFalse(place.path.with_suffix(".max").exists(), "a sample escaped the barrier")
                        memory = gate.reservation_memory(place.path, row, datetime.now(UTC),
                            gate.process_table(fixture.proc), fixture.proc, records.read_memory_config().policy)
                        self.assertIsInstance(memory, records.MeasuredMemory)
                        assert isinstance(memory, records.MeasuredMemory)
                        self.assertEqual(memory.bytes, 3 * GIB)
                        place.observe(3 * GIB, 3 * GIB)
                        reservation_id = str(row.get("reservation_id", ""))
                        if fork_during_scope_read:
                            _ = fixture.enter_context(mock.patch.object(Path, "read_text",
                                new=fork_after_scope_snapshot))
                    released.assert_called_once()
                fixture.assert_released(self, reservation_id)
            finally:
                for descriptor in (fork_read, fork_write, exit_read, exit_write,
                                   leader_descriptor, descendant_descriptor):
                    if descriptor >= 0:
                        os.close(descriptor)
                if descendant:
                    try:
                        os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if scope_name:
                    _ = subprocess.run(["systemctl", "--user", "stop", Path(scope_name).name],
                                       capture_output=True, timeout=3, check=False)

    def test_normal_exit_records_the_exit_code_and_one_peak(self) -> None:
        with QueuePlaceFixture() as fixture:
            for status in (0, 7):
                with self.subTest(status=status):
                    with queue.queue_place("rust-analyzer", fixture.root) as place:
                        child = place.popen([sys.executable, "-c", CHILD, str(status)],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                        output, _ = child.communicate(b"queued protocol\x00\xff", timeout=3)
                        self.assertEqual(output, b"queued protocol\x00\xff")
                        _, row = fixture.reservation()
                        place.observe(GIB, GIB)
                        reservation_id = str(row.get("reservation_id", ""))
                    self.assertEqual(place.status, status)
                    fixture.assert_released(self, reservation_id, status=status)

    def test_exception_after_a_successful_program_still_releases_with_failure(self) -> None:
        with QueuePlaceFixture() as fixture:
            places: list[queue.QueuePlace] = []
            reservation_id = ""
            with self.assertRaisesRegex(RuntimeError, "holder failed"):
                with queue.queue_place("rust-analyzer", fixture.root) as place:
                    places.append(place)
                    child = place.popen([sys.executable, "-c", "pass"])
                    self.assertEqual(child.wait(timeout=3), 0)
                    _, row = fixture.reservation()
                    reservation_id = str(row.get("reservation_id", ""))
                    place.observe(GIB, GIB)
                    raise RuntimeError("holder failed")
            self.assertEqual(places[0].status, 1)
            fixture.assert_released(self, reservation_id, status=1)

    def test_sampler_start_failure_stops_and_reaps_the_program_before_release(self) -> None:
        with QueuePlaceFixture() as fixture, mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "0"}):
            release = client.release

            def release_after_reaping(path: str, status: int, configuration: records.MemoryConfig | wire.CleanupTiming) -> None:
                self.assertEqual(len(fixture.children), 1)
                child = fixture.children[0]
                self.assertIsNotNone(child.returncode)
                with self.assertRaises(ChildProcessError):
                    _ = os.waitpid(child.pid, os.WNOHANG)
                _ = release(path, status, configuration)

            with mock.patch.object(client, "release", side_effect=release_after_reaping) as released:
                with queue.queue_place("rust-analyzer", fixture.root) as place:
                    with self.assertRaisesRegex(RuntimeError, "sampler start failed"), \
                            mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("sampler start failed")):
                        _ = place.popen([sys.executable, "-c", BLOCKING_CHILD], stdout=subprocess.PIPE)
            released.assert_called_once()
            self.assertIsInstance(place.program, queue.StartedProgram)
            self.assertEqual(place.status, 1)
            fixture.assert_cleaned(self)

    def test_running_program_is_stopped_before_its_place_is_released(self) -> None:
        with QueuePlaceFixture() as fixture:
            release = client.release
            def release_after_stop(path: str, status: int, configuration: records.MemoryConfig | wire.CleanupTiming) -> None:
                self.assertIsNotNone(child.poll(), "reservation released while its program still ran")
                _ = release(path, status, configuration)
            with mock.patch.object(client, "release", side_effect=release_after_stop) as released:
                with queue.queue_place("rust-analyzer", fixture.root) as place:
                    child = fixture.start_blocked(place)
                    _, row = fixture.reservation()
                    reservation_id = str(row.get("reservation_id", ""))
                    place.observe(GIB, GIB)
                    self.assertIsNone(child.poll())
                    self.assertFalse(released.called)
                self.assertIsNotNone(child.poll())
                self.assertTrue(released.called)
            fixture.assert_released(self, reservation_id)

    def test_term_resistant_program_is_killed_before_release(self) -> None:
        with QueuePlaceFixture() as fixture:
            with queue.queue_place("rust-analyzer", fixture.root) as place:
                child = place.popen([sys.executable, "-c",
                    TERM_RESISTANT_CHILD],
                    stdout=subprocess.PIPE)
                assert child.stdout is not None
                self.assertTrue(select.select([child.stdout], [], [], 3)[0])
                self.assertEqual(child.stdout.readline(), b"ready\n")
                _, row = fixture.reservation()
                reservation_id = str(row.get("reservation_id", ""))
                place.observe(GIB, GIB)
            self.assertEqual(child.returncode, -signal.SIGKILL)
            fixture.assert_released(self, reservation_id)

    def assert_descendant_stopped_before_release(self, *, leader_exits: bool, descendant_detaches: bool = False) -> None:
        with QueuePlaceFixture() as fixture:
            write_memory_config(fixture.config, program_stop_seconds="0.1", program_sample_seconds="0.02",
                                **{"program_first_peak_gib.rust-analyzer": "18"})
            release = client.release
            observe = queue.QueuePlace.observe
            sampled = threading.Event()
            descriptor = -1
            descendant = 0

            def observed(place: queue.QueuePlace, peak: int, current: int) -> None:
                observe(place, peak, current)
                if current == 3 * GIB:
                    sampled.set()

            def release_after_descendant_exit(path: str, status: int, configuration: records.MemoryConfig | wire.CleanupTiming) -> None:
                self.assertTrue(select.select([descriptor], [], [], 0)[0],
                                "reservation released while its descendant still ran")
                _ = release(path, status, configuration)

            try:
                with mock.patch.object(client, "release", side_effect=release_after_descendant_exit) as released, \
                     mock.patch.object(queue.QueuePlace, "observe", autospec=True, side_effect=observed):
                    with queue.queue_place("rust-analyzer", fixture.root) as place:
                        leader = place.popen([sys.executable, "-c", DESCENDANT_CHILD,
                                              "exit" if leader_exits else "term",
                                              "detach" if descendant_detaches else "group"], stdout=subprocess.PIPE)
                        assert leader.stdout is not None
                        self.assertTrue(select.select([leader.stdout], [], [], 3)[0], "descendant never became ready")
                        descendant = int(cast(bytes, leader.stdout.readline()))
                        descriptor = os.pidfd_open(descendant)
                        if descendant_detaches:
                            self.assertEqual(os.getsid(descendant), descendant)
                            self.assertEqual(os.getpgid(descendant), descendant)
                            self.assertNotEqual(os.getpgid(descendant), leader.pid)
                            fixture.supply_process(descendant, 2 * GIB, parent=leader.pid)
                            self.assertTrue(sampled.wait(3), "sampler never recorded the detached descendant")
                        else:
                            self.assertEqual(os.getpgid(descendant), leader.pid)
                        self.assertFalse(select.select([descriptor], [], [], 0)[0])
                        if leader_exits:
                            self.assertEqual(leader.wait(timeout=3), 0)
                        else:
                            self.assertIsNone(leader.poll())
                        _, row = fixture.reservation()
                        reservation_id = str(row.get("reservation_id", ""))
                        place.observe(GIB, GIB)
                    released.assert_called_once()
                fixture.assert_released(self, reservation_id)
            finally:
                if descendant:
                    try:
                        os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if descriptor >= 0:
                    os.close(descriptor)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "descendant exit checks need Linux pidfds")
    def test_descendant_of_an_exited_leader_is_stopped_before_release(self) -> None:
        self.assert_descendant_stopped_before_release(leader_exits=True)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "descendant exit checks need Linux pidfds")
    def test_descendant_of_a_term_stopped_leader_is_stopped_before_release(self) -> None:
        self.assert_descendant_stopped_before_release(leader_exits=False)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "descendant exit checks need Linux pidfds")
    def test_unscoped_setsid_descendant_is_stopped_before_release(self) -> None:
        with mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "0"}):
            self.assert_descendant_stopped_before_release(leader_exits=False, descendant_detaches=True)

    def test_one_place_cannot_launch_a_second_program(self) -> None:
        with QueuePlaceFixture() as fixture:
            with queue.queue_place("rust-analyzer", fixture.root) as place:
                child = place.popen([sys.executable, "-c", "pass"])
                _ = child.wait(timeout=3)
                with self.assertRaises(queue.QueuePlaceRefused):
                    _ = place.popen([sys.executable, "-c", "raise SystemExit(99)"])
                self.assertEqual(len(fixture.children), 1)

    def test_unscoped_program_is_charged_by_its_process_tree(self) -> None:
        with QueuePlaceFixture() as fixture:
            with queue.queue_place("rust-analyzer", fixture.root) as place:
                child = fixture.start_blocked(place)
                path, row = fixture.reservation()
                self.assertEqual(Path(row["sidecar"]).read_text(), "")
                self.assertIsInstance(gate.reservation_lifetime(row, True), gate.LiveOwnerProcess)
                memory = gate.reservation_memory(path, row, datetime.now(UTC), gate.process_table(fixture.proc),
                                                 fixture.proc, records.read_memory_config().policy)
                self.assertIsInstance(memory, records.MeasuredMemory)
                assert isinstance(memory, records.MeasuredMemory)
                self.assertEqual(memory.bytes, GIB)
                self.assertIsNone(child.poll())
                place.observe(GIB, GIB)
                reservation_id = str(row.get("reservation_id", ""))
            fixture.assert_released(self, reservation_id)

    def test_configured_first_peak_and_history_are_specific_to_program_and_host(self) -> None:
        with QueuePlaceFixture() as fixture:
            fixture.seed([50 * GIB] * 5, program="another-program")
            fixture.seed([40 * GIB] * 5, host="another-host")
            for values, expected, source, runs in (([], 18 * GIB, "fallback", 0),
                                                  ([n * GIB for n in (2, 4, 6, 8, 10)], 10 * GIB, "measured", 5)):
                with self.subTest(source=source):
                    fixture.seed(values)
                    with queue.queue_place("rust-analyzer", fixture.root):
                        _, row = fixture.reservation()
                        self.assertEqual((row["step"], row["need"], row.get("queue_waiter", {}).get("need_source"), row.get("queue_waiter", {}).get("need_runs")),
                                         ("rust-analyzer", expected, source, runs))

    def test_missing_first_peak_refuses_before_admission_or_launch(self) -> None:
        with QueuePlaceFixture() as fixture:
            fixture.seed([GIB] * 5, program="unconfigured")
            with mock.patch.object(client, "admit", wraps=client.admit) as checked:
                with self.assertRaisesRegex(queue.QueuePlaceRefused,
                    r"no first peak for unconfigured in build_admission.conf \[memory\]"):
                    with queue.queue_place("unconfigured", fixture.root):
                        self.fail("unconfigured program was admitted")
                checked.assert_not_called()
            self.assertEqual(fixture.children, [])
            self.assertEqual(list(fixture.directory.glob("*.reservation")), [])

    def test_invalid_first_peak_reports_unavailable_size_and_refuses_the_program(self) -> None:
        with QueuePlaceFixture() as fixture:
            write_memory_config(fixture.config, **{"program_first_peak_gib.rust-analyzer": "abc"})
            diagnostics = io.StringIO()
            with mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()), \
                 mock.patch.object(client, "admit", wraps=client.admit) as checked, redirect_stderr(diagnostics):
                for _ in range(2):
                    with self.assertRaises(queue.QueuePlaceRefused) as refused:
                        with queue.queue_place("rust-analyzer", fixture.root):
                            self.fail("program with an invalid first peak was admitted")
                    self.assertIn(str(fixture.config), str(refused.exception))
                    self.assertIn("program_first_peak_gib.rust-analyzer", str(refused.exception))
                    self.assertIn("no usable committed value", str(refused.exception))
                    self.assertIn("program_first_peak_gib.rust-analyzer='abc'", str(refused.exception))
                    self.assertNotIn("using 0", str(refused.exception))
                checked.assert_not_called()
            self.assertEqual(diagnostics.getvalue(), "")
            self.assertEqual(fixture.children, [])
            self.assertEqual(list(fixture.directory.glob("*.reservation")), [])

    def test_missing_or_invalid_program_peak_error_names_the_config_file_and_key(self) -> None:
        key = "program_first_peak_gib.cargo"
        cases: tuple[dict[str, str | None], ...] = ({key: None}, {key: "invalid"})
        for values in cases:
            with self.subTest(values=values), QueuePlaceFixture() as fixture:
                write_memory_config(fixture.config, **values)
                with mock.patch.object(client, "admit", side_effect=AssertionError("admission must not start")) as checked:
                    with self.assertRaises(queue.QueuePlaceRefused) as refused:
                        with queue.queue_place("cargo", fixture.root):
                            self.fail("an invalid program peak allowed a child")
                    checked.assert_not_called()
                self.assertIn(str(fixture.config), str(refused.exception))
                self.assertIn(key, str(refused.exception))
                self.assertEqual(fixture.children, [])
                self.assertEqual(list(fixture.directory.glob("*.reservation")), [])


    def test_invalid_config_warns_once_and_does_not_corrupt_child_protocol(self) -> None:
        with QueuePlaceFixture() as fixture:
            commit_config_fixture(fixture.config)
            write_memory_config(fixture.config, estimate_refresh_seconds="abc", **{"program_first_peak_gib.rust-analyzer": "18"})
            diagnostics, output = io.StringIO(), io.StringIO()
            with redirect_stderr(diagnostics), redirect_stdout(output):
                for _ in range(2):
                    with queue.queue_place("rust-analyzer", fixture.root) as place:
                        child = place.popen([sys.executable, "-c", CHILD, "0"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                        echoed, _ = child.communicate(b"Content-Length: 4\r\n\r\n{}\x00\xff", timeout=3)
                        self.assertEqual(echoed, b"Content-Length: 4\r\n\r\n{}\x00\xff")
            self.assertIn("estimate_refresh_seconds='abc'", diagnostics.getvalue())
            self.assertIn(str(fixture.config), diagnostics.getvalue())
            self.assertEqual(output.getvalue(), "")

    def test_waiting_checks_the_hold_before_each_attempt_and_never_starts_early(self) -> None:
        with QueuePlaceFixture() as fixture:
            fixture.admission.free(0)
            fixture.admission.hold_starts()
            events: list[str] = []
            admit = client.admit
            def observed(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None],
                         refresh: Callable[[], wire.AdmitRequest], *,
                         _attempt: Callable[[], None]) -> wire.Admitted | wire.Refused | client.LeftQueue:
                def held(reply: wire.Wait) -> None:
                    self.assertEqual(fixture.children, [])
                    self.assertEqual(len(fixture.admission.server.waiters), 1)
                    events.append("wait")
                    on_wait(reply)
                    fixture.admission.free(60 * GIB)
                return admit(request, held, refresh, _attempt=_attempt)
            with mock.patch.object(client, "admit", side_effect=observed), \
                 redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as stdout:
                with queue.queue_place("rust-analyzer", fixture.root,
                                       before_check=lambda: events.append("hold")):
                    self.assertEqual(fixture.children, [])
            self.assertEqual(events, ["hold", "wait", "hold"])
            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(fixture.admission.server.waiters)

    def test_cleanup_mask_is_restored_when_blocking_is_interrupted(self) -> None:
        change_mask = signal.pthread_sigmask
        previous = change_mask(signal.SIG_BLOCK, {signal.SIGUSR1})
        expected = previous | {signal.SIGUSR1}

        def interrupted_block(how: int, numbers: Iterable[int]) -> set[int | signal.Signals]:
            mask = set(numbers)
            result = change_mask(how, mask)
            if how == signal.SIG_BLOCK and signal.SIGINT in mask:
                raise KeyboardInterrupt
            return result

        try:
            with mock.patch.object(signal, "pthread_sigmask", side_effect=interrupted_block):
                with self.assertRaises(KeyboardInterrupt):
                    with cancellation.defer_cleanup_signals():
                        self.fail("The block step must interrupt entry")
            self.assertEqual(change_mask(signal.SIG_BLOCK, set()), expected)
        finally:
            _ = change_mask(signal.SIG_SETMASK, previous)

    @unittest.skipUnless(hasattr(os, "fork"), "holder signal lifecycle needs fork")
    def test_holder_signals_release_and_restore_handlers(self) -> None:
        for number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=number), QueuePlaceFixture() as fixture:
                read_fd, write_fd = os.pipe()
                holder = os.fork()
                if holder == 0:
                    os.close(fixture.lifeline_write)
                    _ = signal.alarm(TEST_HANG_GUARD_SECS)
                    os.close(read_fd)
                    status = 99
                    try:
                        fixture.supply_process(os.getpid(), 0, parent=1)
                        with queue.queue_place("rust-analyzer", fixture.root) as place:
                            _ = fixture.start_blocked(place)
                            place.observe(GIB, GIB)
                            _ = os.write(write_fd, b"ready")
                            _ = os.read(fixture.lifeline_read, 1)
                    except SystemExit as error:
                        status = cast(int, error.code)
                    except KeyboardInterrupt:
                        status = 128 + signal.SIGINT
                    finally:
                        os._exit(status)
                os.close(write_fd)
                try:
                    self.assertTrue(select.select([read_fd], [], [], 5)[0], "holder never admitted")
                    self.assertEqual(os.read(read_fd, 5), b"ready")
                    _, row = fixture.reservation()
                    os.kill(holder, number)
                    descriptor = os.pidfd_open(holder)
                    try:
                        self.assertTrue(select.select([descriptor], [], [], 8)[0], "signal cleanup did not finish")
                    finally:
                        os.close(descriptor)
                    _, status = os.waitpid(holder, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status),
                                     -number if number == signal.SIGTERM else 128 + number)
                    if number == signal.SIGTERM:
                        shutil.rmtree(fixture.proc / str(holder))
                        with queue.queue_place("rust-analyzer", fixture.root):
                            pass
                        fixture.assert_released(self, str(row.get("reservation_id", "")), outcome="owner_gone")
                    else:
                        fixture.assert_released(self, str(row.get("reservation_id", "")), status=128 + number)
                finally:
                    os.close(read_fd)
                    try:
                        os.kill(holder, signal.SIGKILL)
                        _ = os.waitpid(holder, 0)
                    except ProcessLookupError:
                        pass
        with QueuePlaceFixture() as fixture:
            before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
            with mock.patch.object(signal, "signal", side_effect=AssertionError("installed a signal handler")), \
                    mock.patch.object(build_folders, "defer_signals", side_effect=AssertionError("deferred folder signals")):
                with queue.queue_place("rust-analyzer", fixture.root):
                    pass
            self.assertEqual({number: signal.getsignal(number) for number in before}, before)

    @unittest.skipUnless(hasattr(os, "fork"), "start interruption needs fork")
    def test_start_window_interrupts_leave_atomic_records_for_the_next_start(self) -> None:
        for checkpoint in ("popen-return", "on-started"):
            for number in (signal.SIGTERM, signal.SIGINT, signal.SIGKILL):
                with self.subTest(checkpoint=checkpoint, signal=number), QueuePlaceFixture() as fixture:
                    ready_read, ready_write = os.pipe()
                    resume_read, resume_write = os.pipe()
                    caught_read, caught_write = os.pipe()
                    exit_read, exit_write = os.pipe()
                    holder = os.fork()
                    if holder == 0:
                        os.close(fixture.lifeline_write)
                        os.close(ready_read)
                        os.close(resume_write)
                        os.close(caught_read)
                        os.close(exit_write)
                        _ = signal.alarm(TEST_HANG_GUARD_SECS)
                        status = 99
                        try:
                            fixture.supply_process(os.getpid(), 0, parent=1)
                            spawn = subprocess.Popen
                            def pause(process: subprocess.Popen[bytes]) -> None:
                                assert process.stdout is not None
                                if not select.select([process.stdout], [], [], 5)[0]:
                                    raise AssertionError("the program did not start")
                                if process.stdout.readline() != b"ready\n":
                                    raise AssertionError("the program did not announce readiness")
                                _ = os.write(ready_write, str(process.pid).encode())
                                _ = os.read(resume_read, 1)
                            def start(argv: list[str], **options: Unpack[queue.PopenArguments]) -> subprocess.Popen[bytes]:
                                process = spawn(argv, **options)
                                if "--program-start" in argv and checkpoint == "popen-return":
                                    pause(process)
                                return process
                            with queue.queue_place("rust-analyzer", fixture.root) as place:
                                if checkpoint == "on-started":
                                    def on_started() -> None:
                                        assert isinstance(place.program, queue.StartedProgram)
                                        pause(place.program.process)
                                    place.on_started = on_started
                                with mock.patch.object(subprocess, "Popen", side_effect=start), \
                                        mock.patch.object(signal, "signal", side_effect=AssertionError("installed a signal handler")), \
                                        mock.patch.object(build_folders, "defer_signals", side_effect=AssertionError("deferred start signals")):
                                    _ = place.popen([sys.executable, "-c", TERM_RESISTANT_CHILD], stdout=subprocess.PIPE)
                        except KeyboardInterrupt:
                            status = 128 + signal.SIGINT
                            if checkpoint == "popen-return":
                                _ = os.write(caught_write, b"C")
                                _ = os.read(exit_read, 1)
                        finally:
                            os._exit(status)
                    os.close(ready_write)
                    os.close(resume_read)
                    os.close(caught_write)
                    os.close(exit_read)
                    program_pid = 0
                    try:
                        self.assertTrue(select.select([ready_read], [], [], 8)[0], "the start window never opened")
                        program_pid = int(os.read(ready_read, 64))
                        saved = list(queue.program_starts().glob("*.json"))
                        self.assertEqual(len(saved), 1)
                        record = wire.parse_record(saved[0].read_text(), queue.ProgramStart)
                        self.assertEqual(record.holder.pid, holder)
                        self.assertEqual(record.program.pid, program_pid)
                        descriptor = os.pidfd_open(holder)
                        try:
                            os.kill(holder, number)
                            if number != signal.SIGKILL:
                                _ = os.write(resume_write, b"G")
                            if number == signal.SIGINT and checkpoint == "popen-return":
                                self.assertTrue(select.select([caught_read], [], [], 8)[0], "the caller did not catch INT")
                                self.assertEqual(os.read(caught_read, 1), b"C")
                                self.assertIsNotNone(records.process_start_time(holder), "the caller did not remain alive")
                                self.assertIsNone(records.process_start_time(program_pid), "INT released a running program")
                                self.assertEqual(list(queue.program_starts().iterdir()), [])
                                _ = os.write(exit_write, b"E")
                            self.assertTrue(select.select([descriptor], [], [], 8)[0], "the holder did not end")
                        finally:
                            os.close(descriptor)
                        _, status = os.waitpid(holder, 0)
                        self.assertEqual(os.waitstatus_to_exitcode(status),
                                         128 + signal.SIGINT if number == signal.SIGINT else -number)
                        for path in queue.program_starts().iterdir():
                            self.assertEqual(path.suffix, ".json", "a partial record escaped publication")
                            remaining = wire.parse_record(path.read_text(), queue.ProgramStart)
                            self.assertEqual(remaining.program, record.program)
                        shutil.rmtree(fixture.proc / str(holder))
                        with queue.queue_place("rust-analyzer", fixture.root):
                            pass
                        self.assertIsNone(records.process_start_time(program_pid), "the next start left an untracked program")
                        self.assertEqual(list(queue.program_starts().iterdir()), [])
                        fixture.assert_cleaned(self)
                    finally:
                        for fd in (ready_read, resume_write, caught_read, exit_write):
                            os.close(fd)
                        try:
                            os.kill(holder, signal.SIGKILL)
                            _ = os.waitpid(holder, 0)
                        except (ProcessLookupError, ChildProcessError):
                            pass
                        if program_pid:
                            try:
                                send_process_group(program_pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass

    @unittest.skipUnless(hasattr(os, "fork"), "owner death lifecycle needs fork")
    def test_owner_gone_keeps_a_populated_scope_then_finalizes_once_when_empty(self) -> None:
        with QueuePlaceFixture() as fixture:
            scope = fixture.root / "scope"
            scope.mkdir()
            for name, value in {"cgroup.procs": "123\n", "memory.current": str(GIB),
                                "memory.swap.current": "0", "memory.stat": f"anon {GIB}\nshmem 0\ninactive_file 0\n"}.items():
                _ = (scope / name).write_text(value)
            read_fd, write_fd = os.pipe()
            holder = os.fork()
            if holder == 0:
                os.close(fixture.lifeline_write)
                _ = signal.alarm(TEST_HANG_GUARD_SECS)
                os.close(read_fd)
                try:
                    fixture.supply_process(os.getpid(), 0, parent=1)
                    with queue.queue_place("rust-analyzer", fixture.root) as place:
                        child = fixture.start_blocked(place)
                        _ = (scope / "cgroup.procs").write_text(str(child.pid))
                        _ = place.sidecar.write_text(str(scope))
                        place.observe(GIB, GIB)
                        _ = os.write(write_fd, b"ready")
                        _ = os.read(fixture.lifeline_read, 1)
                finally:
                    os._exit(99)
            os.close(write_fd)
            try:
                self.assertTrue(select.select([read_fd], [], [], 5)[0])
                self.assertEqual(os.read(read_fd, 5), b"ready")
                _, row = fixture.reservation()
                program_pid = int((scope / "cgroup.procs").read_text())
                os.kill(holder, signal.SIGKILL)
                _ = os.waitpid(holder, 0)
                shutil.rmtree(fixture.proc / str(holder))
                def check() -> None:
                    _ = fixture.admission.run_pass()
                check()
                self.assertTrue(any(str(saved.get("reservation_id", "")) == str(row.get("reservation_id", ""))
                                    for saved in (service.reservation_row(record) for record in fixture.admission.server.reservations.values())))
                self.assertEqual(fixture.history(), [])
                os.kill(program_pid, 0)  # The program still lives after its holder died.
                send_process_group(program_pid, signal.SIGKILL)
                _ = (scope / "cgroup.procs").write_text("")
                check()
                check()
                fixture.assert_released(self, str(row.get("reservation_id", "")), outcome="owner_gone")
            finally:
                os.close(read_fd)
                try:
                    os.kill(holder, signal.SIGKILL)
                    _ = os.waitpid(holder, 0)
                except ProcessLookupError:
                    pass

    def assert_scope_survives_holder_death(self, *, leader_exits: bool) -> None:
        self.require_transient_scope()
        with QueuePlaceFixture() as fixture:
            fixture.use_private_scope()
            fixture.supply_scope_memory(GIB)
            fixture.complete_scoped_launch = leader_exits
            with mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                 mock.patch.object(queue, "in_builds_slice", return_value=False), \
                 mock.patch.object(queue.QueuePlace, "_sample", return_value=None):
                read_fd, write_fd = os.pipe()
                holder = os.fork()
                if holder == 0:
                    os.close(fixture.lifeline_write)
                    _ = signal.alarm(TEST_HANG_GUARD_SECS)
                    os.close(read_fd)
                    try:
                        fixture.supply_process(os.getpid(), 0, parent=1)
                        with queue.queue_place("rust-analyzer", fixture.root) as place:
                            program = ("import os; pid=os.fork(); os._exit(0) if pid else " + LIFELINE_WAIT
                                       if leader_exits else "import os; " + LIFELINE_WAIT)
                            child = place.popen([sys.executable, "-c", program],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            scope_at_return = place.sidecar.read_text().strip()
                            place.observe(GIB, GIB)
                            _ = os.write(write_fd, json.dumps({"pid": child.pid, "scope": scope_at_return,
                                                               "status": child.poll()}).encode())
                            _ = os.read(fixture.lifeline_read, 1)
                    finally:
                        os._exit(99)
                os.close(write_fd)
                program_pid = 0
                scope_name = ""
                try:
                    self.assertTrue(select.select([read_fd], [], [], 10)[0], "holder never returned from popen")
                    message = cast(dict[str, object], json.loads(os.read(read_fd, 4096)))
                    program_pid = cast(int, message["pid"])
                    scope_name = cast(str, message["scope"])
                    self.assertTrue(scope_name, "popen returned before the scope reached the sidecar")
                    directory = Path(scope_name)
                    self.assertTrue(directory.is_relative_to("/sys/fs/cgroup"))
                    self.assertIn(fixture.private_slice, Path(scope_name).parts)
                    self.assertEqual(message["status"], 0 if leader_exits else None)
                    _, row = fixture.reservation()
                    self.assertEqual(Path(row["sidecar"]).read_text().strip(), scope_name)
                    os.kill(holder, signal.SIGKILL)
                    _ = os.waitpid(holder, 0)
                    shutil.rmtree(fixture.proc / str(holder))
                    self.assertIsInstance(gate.reservation_lifetime(row, False), gate.PopulatedStepScope)
                    def check() -> None:
                        _ = fixture.admission.run_pass()

                    check()
                    reservation_id = str(row.get("reservation_id", ""))
                    self.assertTrue(any(saved.get("reservation_id") == reservation_id
                                        for saved in (service.reservation_row(record) for record in fixture.admission.server.reservations.values())))
                    self.assertEqual(fixture.history(), [])
                    self.assertTrue((directory / "cgroup.procs").read_text().split())
                    stopped = subprocess.run(["systemctl", "--user", "stop", directory.name],
                                             capture_output=True, timeout=3, check=False)
                    self.assertEqual(stopped.returncode, 0, stopped.stderr)
                    check()
                    check()
                    fixture.assert_released(self, reservation_id, outcome="owner_gone")
                finally:
                    os.close(read_fd)
                    try:
                        os.kill(holder, signal.SIGKILL)
                        _ = os.waitpid(holder, 0)
                    except ProcessLookupError:
                        pass
                    if scope_name:
                        _ = subprocess.run(["systemctl", "--user", "stop", Path(scope_name).name],
                                           capture_output=True, timeout=3, check=False)
                    if program_pid:
                        try:
                            send_process_group(program_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_scope_is_published_at_popen_return_and_survives_holder_death(self) -> None:
        self.assert_scope_survives_holder_death(leader_exits=False)

    def test_scope_survives_holder_death_after_its_leader_exits_before_popen_returns(self) -> None:
        self.assert_scope_survives_holder_death(leader_exits=True)

    def test_holder_death_before_scope_publication_prevents_program_execution(self) -> None:
        self.require_transient_scope()
        with QueuePlaceFixture() as fixture:
            fixture.use_private_scope()
            fixture.supply_scope_memory(GIB)
            marker = fixture.root / "program-ran"
            ready_read, ready_write = os.pipe()
            resume_read, resume_write = os.pipe()
            spawned_read, spawned_write = os.pipe()
            spawn = subprocess.Popen

            def start_before_publication(argv: list[str], **kwargs: Unpack[queue.PopenArguments]) -> subprocess.Popen[bytes]:
                if "--scope-start" not in argv:
                    return spawn(argv, **kwargs)
                start = argv.index("--scope-start") - 2
                delayed = [*argv[:start], sys.executable, "-c", BEFORE_SCOPE_START,
                           str(ready_write), str(resume_read), *argv[start:]]
                kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), ready_write, resume_read)
                child = spawn(delayed, **kwargs)
                _ = os.write(spawned_write, str(child.pid).encode())
                _ = os.read(fixture.lifeline_read, 1)
                return child

            with mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                 mock.patch.object(queue, "in_builds_slice", return_value=False), \
                 mock.patch.object(subprocess, "Popen", side_effect=start_before_publication):
                holder = os.fork()
                if holder == 0:
                    os.close(fixture.lifeline_write)
                    _ = signal.alarm(TEST_HANG_GUARD_SECS)
                    os.close(ready_read)
                    os.close(resume_write)
                    os.close(spawned_read)
                    try:
                        fixture.supply_process(os.getpid(), 0, parent=1)
                        with queue.queue_place("rust-analyzer", fixture.root) as place:
                            place.observe(GIB, GIB)
                            _ = place.popen([sys.executable, "-c",
                                "from pathlib import Path; import os,sys; Path(sys.argv[1]).touch(); " + LIFELINE_WAIT,
                                str(marker)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    finally:
                        os._exit(99)
                os.close(ready_write)
                os.close(resume_read)
                os.close(spawned_write)
                descriptor = -1
                program_pid = 0
                scope_name = ""
                try:
                    self.assertTrue(select.select([spawned_read], [], [], 10)[0], "OS Popen never returned")
                    program_pid = int(os.read(spawned_read, 64))
                    self.assertTrue(select.select([ready_read], [], [], 10)[0], "scope startup never reached its barrier")
                    wrapper_pid = int(os.read(ready_read, 64))
                    descriptor = os.pidfd_open(wrapper_pid)
                    cgroup = Path(f"/proc/{wrapper_pid}/cgroup").read_text()
                    scope_name = next(line[3:] for line in cgroup.splitlines() if line.startswith("0::"))
                    self.assertIn(fixture.private_slice, Path(scope_name).parts)
                    _, row = fixture.reservation()
                    sidecar = Path(row["sidecar"])
                    reservation_id = str(row.get("reservation_id", ""))
                    self.assertEqual(sidecar.read_text(), "")
                    self.assertFalse(marker.exists())
                    os.kill(holder, signal.SIGKILL)
                    _ = os.waitpid(holder, 0)
                    shutil.rmtree(fixture.proc / str(holder))
                    def check_and_release_observer() -> None:
                        _ = fixture.admission.run_pass()

                    check_and_release_observer()
                    fixture.assert_released(self, reservation_id, outcome="owner_gone")
                    self.assertFalse(select.select([descriptor], [], [], 0)[0], "wrapper exited before the barrier opened")
                    _ = os.write(resume_write, b"G")
                    exited = bool(select.select([descriptor], [], [], 10)[0])
                    self.assertFalse(marker.exists(), "program ran after its reservation was finalized")
                    self.assertTrue(exited, "startup left a process without a reservation")
                    self.assertFalse(sidecar.exists() and sidecar.read_text().strip(), "startup recreated a finalized sidecar")
                    check_and_release_observer()
                    fixture.assert_released(self, reservation_id, outcome="owner_gone")
                finally:
                    for fd in (ready_read, resume_write, spawned_read):
                        os.close(fd)
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        os.kill(holder, signal.SIGKILL)
                        _ = os.waitpid(holder, 0)
                    except ProcessLookupError:
                        pass
                    if scope_name:
                        _ = subprocess.run(["systemctl", "--user", "stop", Path(scope_name).name],
                                           capture_output=True, timeout=3, check=False)
                    if program_pid:
                        try:
                            send_process_group(program_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_int_before_scope_publication_stops_the_published_scope_with_a_live_caller(self) -> None:
        self.require_transient_scope()
        with QueuePlaceFixture() as fixture:
            fixture.use_private_scope()
            fixture.supply_scope_memory(GIB)
            ready_read, ready_write = os.pipe()
            resume_read, resume_write = os.pipe()
            caught_read, caught_write = os.pipe()
            exit_read, exit_write = os.pipe()
            spawn = subprocess.Popen

            def delay_scope(argv: list[str], **options: Unpack[queue.PopenArguments]) -> subprocess.Popen[bytes]:
                if "--scope-start" in argv:
                    start = argv.index("--scope-start") - 2
                    argv = [*argv[:start], sys.executable, "-c", BEFORE_SCOPE_START,
                            str(ready_write), str(resume_read), *argv[start:]]
                    options["pass_fds"] = (*options.get("pass_fds", ()), ready_write, resume_read)
                return spawn(argv, **options)

            with mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                    mock.patch.object(subprocess, "Popen", side_effect=delay_scope):
                holder = os.fork()
                if holder == 0:
                    os.close(fixture.lifeline_write)
                    for fd in (ready_read, resume_write, caught_read, exit_write):
                        os.close(fd)
                    _ = signal.alarm(TEST_HANG_GUARD_SECS)
                    scope = ""
                    try:
                        fixture.supply_process(os.getpid(), 0, parent=1)
                        with queue.queue_place("rust-analyzer", fixture.root) as place:
                            def started() -> None:
                                nonlocal scope
                                scope = place.sidecar.read_text().strip()
                                assert isinstance(place.program, queue.StartedProgram)
                                stream = place.program.process.stdout
                                assert stream is not None
                                if not select.select([stream], [], [], 5)[0] or stream.readline() != b"ready\n":
                                    raise AssertionError("the scoped program never became active")
                            place.on_started = started
                            _ = place.popen([sys.executable, "-c", BLOCKING_CHILD], stdout=subprocess.PIPE)
                    except KeyboardInterrupt:
                        _ = os.write(caught_write, scope.encode())
                        _ = os.read(exit_read, 1)
                        os._exit(0)
                    finally:
                        os._exit(99)
                for fd in (ready_write, resume_read, caught_write, exit_read):
                    os.close(fd)
                try:
                    self.assertTrue(select.select([ready_read], [], [], 10)[0], "the scope did not reach its barrier")
                    _ = int(os.read(ready_read, 64))
                    _, reservation = fixture.reservation()
                    self.assertEqual(Path(reservation["sidecar"]).read_text(), "")
                    os.kill(holder, signal.SIGINT)
                    _ = os.write(resume_write, b"G")
                    self.assertTrue(select.select([caught_read], [], [], 8)[0], "INT cleanup waited for a live scope")
                    scope = Path(os.read(caught_read, 4096).decode())
                    self.assertTrue(scope.is_absolute())
                    self.assertIsNotNone(records.process_start_time(holder), "the caller did not remain alive")
                    self.assertEqual(gate.scope_members(scope), set())
                    self.assertEqual(list(queue.program_starts().iterdir()), [])
                    fixture.assert_released(self, str(reservation.get("reservation_id", "")), status=128 + signal.SIGINT)
                    _ = os.write(exit_write, b"E")
                    self.assertEqual(os.waitpid(holder, 0)[1], 0)
                finally:
                    for fd in (ready_read, resume_write, caught_read, exit_write):
                        os.close(fd)
                    try:
                        os.kill(holder, signal.SIGKILL)
                        _ = os.waitpid(holder, 0)
                    except (ProcessLookupError, ChildProcessError):
                        pass

    def test_the_program_does_not_inherit_the_start_window_signal_mask(self) -> None:
        with QueuePlaceFixture() as fixture:
            source = ("import signal; mask=signal.pthread_sigmask(signal.SIG_BLOCK,set()); "
                      "print(int(signal.SIGTERM in mask),int(signal.SIGINT in mask),flush=True)")
            with queue.queue_place("rust-analyzer", fixture.root) as place:
                process = place.popen([sys.executable, "-c", source], stdout=subprocess.PIPE)
                output, _ = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(output, b"0 0\n")

    def test_transient_scope_sidecar_and_sampler_use_the_program_scope(self) -> None:
        self.require_transient_scope()
        with QueuePlaceFixture() as fixture:
            fixture.use_private_scope()
            fixture.supply_scope_memory(3 * GIB, 9 * GIB)
            sampled = threading.Event()
            observe = queue.QueuePlace.observe
            def observed(place: queue.QueuePlace, peak: int, current: int) -> None:
                observe(place, peak, current)
                sampled.set()
            with mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                 mock.patch.object(queue, "in_builds_slice", return_value=False), \
                 mock.patch.object(queue.QueuePlace, "observe", autospec=True, side_effect=observed):
                with queue.queue_place("rust-analyzer", fixture.root) as place:
                    child = fixture.start_blocked(place)
                    self.assertTrue(sampled.wait(4), "sampler never found the transient scope")
                    _, row = fixture.reservation()
                    directory = Path(Path(row["sidecar"]).read_text().strip())
                    self.assertTrue(directory.is_relative_to("/sys/fs/cgroup"))
                    self.assertIn(fixture.private_slice, directory.parts)
                    self.assertIsInstance(gate.reservation_lifetime(row, True), gate.PopulatedStepScope)
                    self.assertEqual(int(place.path.with_suffix(".max").read_text()), 3 * GIB)
                    self.assertIsNone(child.poll())
                    reservation_id = str(row.get("reservation_id", ""))
            fixture.assert_released(self, reservation_id)
