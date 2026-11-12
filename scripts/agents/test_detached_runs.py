"""Tests for detached_runs.py: detached runs that message the worker who started them."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import math
import multiprocessing
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, current_thread
from types import SimpleNamespace
from typing import Literal, cast, override
from unittest.mock import MagicMock, patch

from scripts.build_hold.owning_session import NoOwningSession
from scripts.production.staff_test_support import isolated_staff_file
from scripts.lib.signal_test_support import install_signal_pid_guard
from scripts.agents import detached_run_process, detached_runs
from scripts.agents.detached_runs import deliver_in_fresh_process
from scripts.buildlog import treekey
from scripts.build_hold import build_hold, hold_enforcer
from scripts.direct import progress_history
from scripts.lib import conf, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.herdr import turn_end, worker_panes
from scripts.herdr.worker_panes import worker_run
from scripts.herdr import test_worker_panes as pane_support
from scripts.message import send
from scripts.lib.test_hang_guard import LauncherHangGuard
from scripts.lib.fork_test_support import ForkChild, ForkChildren
from scripts.lib.socket_test_support import short_socket_directory
from scripts.message.repeat_guard_test_support import install_guard_test_budget
from scripts.production import staff, session_staff, test_worker, worker_admission
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id, fixture_session

WORKER = "unit-impl"
THREAD_ONE = fixture_runtime_id("detached-run-thread-one")
THREAD_TWO = fixture_runtime_id("detached-run-thread-two")
LASTING_THREAD = fixture_runtime_id("detached-run-lasting-thread")
ADOPTED_THREAD = fixture_runtime_id("detached-run-adopted-thread")
SHARED_THREAD = fixture_runtime_id("detached-run-shared-thread")
DEAD_PID = 2 ** 22 + 12345


def setUpModule() -> None:
    install_signal_pid_guard()
    install_guard_test_budget()


def private_supervisor(test: unittest.TestCase, path: Path) -> int:
    """Keep a private supervisor alive until the test closes its input."""
    guard = LauncherHangGuard("detached supervisor startup and cleanup")
    stand_in = subprocess.Popen([sys.executable, "-c",
                                 "import sys; print('ready', flush=True); sys.stdin.read()",
                                 "detached_runs.py", "supervise", str(path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    test.addCleanup(lambda: stand_in.wait(timeout=guard.remaining()))
    test.addCleanup(stand_in.kill)
    assert stand_in.stdin is not None and stand_in.stdout is not None
    test.addCleanup(stand_in.stdin.close)
    test.addCleanup(stand_in.stdout.close)
    test.assertTrue(select.select([stand_in.stdout], [], [], guard.remaining())[0], "supervisor did not start")
    test.assertEqual(stand_in.stdout.readline(), "ready\n")
    return stand_in.pid


def deliver_in_fixture(path: Path) -> int:
    """Keep fake pane and sender calls inside the test process."""
    return detached_runs.main(["deliver", str(path)])


class DetachedRunsTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    stack: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]
    sent: list[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    unit: staff.StaffRow  # pyright: ignore[reportUninitializedInstanceVariable]
    worker_id: staff.StaffId  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.stack = contextlib.ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        _ = self.stack.enter_context(isolated_staff_file(self.root / "staff.json", {}))
        self.session = self.root / "unit" / "run-1"
        self.session.mkdir(parents=True)
        _ = self.stack.enter_context(patch.object(detached_runs, "RUN_ROOTS", (self.root / "unit",)))
        self.stack.enter_context(patch.dict(os.environ, {
                                                        "HOME": str(self.root),
                                                        "XDG_STATE_HOME": str(self.root / "state"),
                                                        "DESIGN_ROLES_STATE": str(self.root / "astra/design-roles"),
                                                        "UNIT_RUN_DIR": str(self.session),
                                                        "BUILDLOG_SCOPE": "0"}))
        config = self.root / "config"
        config.mkdir()
        for name in ("detached_runs", "send", "postmaster", "codex_daemon", "direct"):
            _ = (config / f"{name}.conf").write_text(
                (Path(__file__).resolve().parents[2] / f"config/{name}.conf").read_text())
        self.stack.enter_context(patch.dict(os.environ, {"CONF_DIR": str(config)}))
        self.sent = []
        _ = self.stack.enter_context(patch.object(treekey, "tree_key", return_value="key-1"))

        fixture = StaffTestCase()
        fixture.setUp()
        _ = self.stack.callback(fixture.doCleanups)
        _, self.unit, _ = fixture.tree()
        self.unit = staff.set_worktree(self.unit.id, staff.PresentWorktree("natedev", self.root), expected=self.unit.worktree)
        self.stack.enter_context(patch.dict(os.environ, {
            "DESIGN_ROLES_STATE": str(self.root / "astra/design-roles"),
        }))
        write_text_atomic(self.session / unit_runs.STATE_FILE, json.dumps({"owner_staff_id": self.unit.id}))
        _ = self.stack.enter_context(patch("scripts.herdr.worker_panes.worker_run", return_value=self.session))
        self.worker_id = self.record_worker(WORKER, THREAD_ONE).id

        def deliver_fixture(member: staff.StaffRow, membership: staff.StaffSnapshot, text: str,
                            **options: object) -> send.Result:
            del membership, options
            path = session_staff.inbox_path(member.id)
            previous = cast(dict[str, object], json.loads(path.read_text())) if path.exists() else {}
            messages = cast(list[str], previous.get("messages", []))
            messages.append(text.split("\n", 1)[1])
            self.sent.append(text)
            write_text_atomic(path, json.dumps({"messages": messages}))
            return send.Result("queued", "private fixture")

        _ = self.stack.enter_context(patch.object(send, "deliver_worker_text", side_effect=deliver_fixture))
        _ = self.stack.enter_context(patch.object(detached_runs, "deliver_in_fresh_process",
                                                 side_effect=deliver_in_fixture))

    @override
    def tearDown(self) -> None:
        self.stack.close()


    def record_worker(self, name: str, thread_id: str) -> staff.StaffRow:
        fixture_session(thread_id, staff.CodexBackend(self.root / "codex-home"))
        rows = [row for row in staff.find(kind="worker", parent=self.unit.id) if row.name == name]
        if rows:
            return staff.update_current_session(rows[0].current_session, staff.native_id(thread_id))
        return staff.ensure_worker(staff.native_id(thread_id), name, self.unit.id)

    def pending(self) -> dict[str, object]:
        path = session_staff.inbox_path(self.worker_id)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return cast(dict[str, object], json.loads(text)) if text.strip() else {}

    def record(self, command: list[str], **extra: str) -> Path:
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        record: detached_runs.RunRecord = {
            "run_id": "r1", "status": "running", "session_dir": str(self.session), "worker": WORKER,
            "staff_id": self.worker_id,
            "label": "verify.sh check", "command": command,
            "log": str(runs / "r1.log"), "started_at": time.time(), "pid": os.getpid(),
            "director_dir": str(self.session), "done_message_file": extra.get("done", ""),
        }
        path = runs / "r1.json"
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def live_run(self, command: list[str], pid: int = 0) -> Path:
        """A running record from this folder at tree key key-1, with a stand-in supervisor whose
        command line names detached_runs.py supervise and the record, unless pid names another process."""
        path = self.record(command)
        if not pid:
            pid = private_supervisor(self, path)
        self.update_record(path, tree_key="key-1", pid=pid)
        return path

    def seat_runs(self, count: int) -> list[Path]:
        """Create live supervisors for distinct runs for the fixture worker."""
        source = self.record(["fixture", "run"])
        template = source.read_text()
        source.unlink()
        paths: list[Path] = []
        for index in range(count):
            path = source.with_name(f"live-{index}.json")
            _ = path.write_text(template)
            guard = LauncherHangGuard("live-run limit fixture")
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()",
                 "detached_runs.py", "supervise", str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(child.wait, timeout=guard.remaining())
            self.addCleanup(child.kill)
            assert child.stdin is not None and child.stdout is not None
            self.addCleanup(child.stdin.close)
            self.addCleanup(child.stdout.close)
            self.assertTrue(select.select([child.stdout], [], [], guard.remaining())[0])
            self.assertEqual(child.stdout.readline(), "ready\n")
            self.update_record(path, run_id=f"live-{index}", label=f"check {index}",
                               command=["fixture", str(index)], pid=child.pid,
                               tree_key="key-1")
            paths.append(path)
        return paths

    def update_record(self, path: Path, **fields: object) -> None:
        record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        record.update(fields)
        _ = path.write_text(json.dumps(record), encoding="utf-8")

    def test_resume_context_names_each_live_run_and_its_read_command(self) -> None:
        paths = self.seat_runs(2)
        text = detached_runs.resume_context(paths[0].parent, WORKER)
        for index, path in enumerate(paths):
            record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
            log = record["log"]
            self.assertIn(f"Detached run live-{index} remains live.", text)
            self.assertIn(f"Result: {path}; log: {log}", text)
            self.assertIn(f"`cat -- {path} {log}`", text)
        self.update_record(paths[0], status="finished")
        text = detached_runs.resume_context(paths[0].parent, WORKER)
        self.assertNotIn("Detached run live-0", text)
        self.assertIn("Detached run live-1", text)

    def start(self, command: list[str], to: str = WORKER) -> tuple[MagicMock, str]:
        """Start command for seat `to`; the Popen mock and what start printed."""
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        with patch.object(subprocess, "Popen", popen), contextlib.redirect_stdout(io.StringIO()) as output:
            code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", to, "--", *command])
        self.assertEqual(code, 0)
        return popen, output.getvalue()

    def supervise(self, path: Path) -> detached_runs.RunRecord:
        self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        return cast(detached_runs.RunRecord, json.loads(path.read_text(encoding="utf-8")))

    # Criterion 1: start returns at once with a detached supervisor and a running record.
    def test_retired_worker_finishes_its_orphan_without_blocking_other_runs(self) -> None:
        path = self.record(["true"])
        self.update_record(path, pid=DEAD_PID, started_at=time.time() - 600)
        _ = staff.retire(self.worker_id)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["status"], "finished")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(self.sent, [])

    def test_start_detaches_a_supervisor_and_records_the_run(self) -> None:
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        environment = {"CODEX_THREAD_ID": THREAD_ONE, "CLAUDE_CODE_SESSION_ID": "director-session",
                       "UNIT_TEAM_ROLE": "test"}
        with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = detached_runs.main(["start", "--label", "verify.sh check", "--", "bash", "verify.sh", "check"])
        self.assertEqual(code, 0)
        kwargs = cast(dict[str, object], popen.call_args.kwargs)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertNotIn(kwargs["stdout"], (None, sys.stdout))
        records = list((self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        record = cast(dict[str, object], json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual((record["status"], record["pid"], record["worker"], record["staff_id"]),
                         ("running", 4321, WORKER, self.worker_id))
        self.assertNotIn("thread_id", record)
        self.assertEqual(record["command"], ["bash", "verify.sh", "check"])
        self.assertNotIn("port", record)
        self.assertIn("next message", output.getvalue())
        records[0].unlink()
        with contextlib.chdir(self.root), \
             patch.object(detached_runs, "caller_from_environment", return_value=detached_runs.NotDetachedWorker()), \
             patch.object(subprocess, "Popen", popen), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(detached_runs.main([
                "start", "--session-dir", str(self.session.relative_to(self.root)), "--to", WORKER,
                "--", "bash", "verify.sh", "check",
            ]), 0)
        arguments = cast(list[str], popen.call_args.args[0])
        path = Path(arguments[-1])
        self.assertTrue(path.is_absolute())
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["session_dir"], str(self.session))
        self.assertTrue(Path(cast(str, record["log"])).is_absolute())

    def test_run_uses_the_start_folder_instead_of_the_staff_worktree(self) -> None:
        start_folder = self.root / "repair-checkout"
        start_folder.mkdir()
        result_file = self.root / "command-folder.txt"
        command = [sys.executable, "-c",
                   "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(str(Path.cwd()))",
                   str(result_file)]
        with contextlib.chdir(start_folder):
            supervisor, output = self.start(command)
        self.assertIn("started detached", output)
        records = list((self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        with contextlib.chdir(cast(str, supervisor.call_args.kwargs["cwd"])):
            record = self.supervise(records[0])
        self.assertEqual(record.get("exit_code"), 0)
        self.assertEqual(result_file.read_text(), str(start_folder))

    def test_start_refuses_at_the_live_run_limit_without_a_record_or_supervisor(self) -> None:
        paths = self.seat_runs(3)
        before = {path: path.read_bytes() for path in paths}
        with patch.object(subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
                                   "--label", "fourth check", "--", "new-check"])
        self.assertEqual(code, 4)
        popen.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(set(paths[0].parent.glob("*.json")), set(paths))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(list(paths[0].parent.glob("*.log")), [])
        self.assertIn("live-run limit 3", err.getvalue())
        for index in range(3):
            self.assertIn(f"live-{index}: check {index}", err.getvalue())
        self.assertIn("next message", err.getvalue())
        self.assertIn("more files in one run", err.getvalue())

    def test_a_finished_run_frees_a_place_for_the_next_start(self) -> None:
        paths = self.seat_runs(3)
        self.update_record(paths[0], status="finished", exit_code=0)
        popen, _ = self.start(["new-check"])
        popen.assert_called_once()
        self.assertEqual(len(list(paths[0].parent.glob("*.json"))), 4)

    def test_a_gone_supervisor_holds_no_place_at_the_limit(self) -> None:
        paths = self.seat_runs(3)
        self.update_record(paths[0], pid=DEAD_PID)
        popen, _ = self.start(["new-check"])
        popen.assert_called_once()
        self.assertEqual(json.loads(paths[0].read_text())["status"], "finished")

    def test_a_live_twin_still_wins_at_the_live_run_limit(self) -> None:
        paths = self.seat_runs(3)
        popen, output = self.start(["fixture", "1"])
        popen.assert_not_called()
        self.assertIn("run live-1 is already running", output)
        self.assertEqual(set(paths[0].parent.glob("*.json")), set(paths))

    def test_start_reads_the_live_run_limit_from_config(self) -> None:
        paths = self.seat_runs(1)
        config = self.root / "config/detached_runs.conf"
        _ = config.write_text("# whole: 1..\nLIVE_RUN_LIMIT=1\n")
        with patch.object(subprocess, "Popen") as popen, contextlib.redirect_stderr(io.StringIO()) as err:
            code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
                                   "--", "new-check"])
        self.assertEqual(code, 4)
        popen.assert_not_called()
        self.assertIn("live-run limit 1", err.getvalue())
        _ = config.write_text("# whole: 1..\nLIVE_RUN_LIMIT=2\n")
        accepted, _ = self.start(["new-check"])
        accepted.assert_called_once()
        self.assertEqual(len(list(paths[0].parent.glob("*.json"))), 2)

    def test_another_seat_does_not_use_this_seats_live_run_places(self) -> None:
        paths = self.seat_runs(3)
        reviewer = self.record_worker("unit-review", fixture_runtime_id("another-seat-review"))
        popen, _ = self.start(["new-check"], to=reviewer.name)
        popen.assert_called_once()
        self.assertEqual(len(list(paths[0].parent.glob("*.json"))), 4)

    def test_parallel_starts_cannot_exceed_one_configured_place(self) -> None:
        _ = (self.root / "config/detached_runs.conf").write_text("# whole: 1..\nLIVE_RUN_LIMIT=1\n")
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir()
        context = multiprocessing.get_context("fork")
        ready = context.Barrier(5)
        guard = LauncherHangGuard("parallel detached starts")

        def start(index: int, announcement: int) -> int:
            _ = os.write(announcement, b"ready\n")
            os.close(announcement)
            _ = ready.wait(timeout=guard.remaining())
            with (patch.object(worker_panes, "worker_run", worker_run),
                  patch.object(detached_run_process, "supervises", return_value=True),
                  patch.object(treekey, "tree_key", return_value="fixture-tree"),
                  patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=4321))):
                return detached_runs.main(["start", "--session-dir", str(self.session),
                                           "--to", WORKER, "--", "check", str(index)])

        with ForkChildren() as children, (runs / ".start.lock").open("a") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            processes: list[ForkChild] = []
            announcements: list[int] = []
            for index in range(4):
                reader, writer = children.pipe()
                processes.append(children.start(
                    lambda index=index, writer=writer: start(index, writer),
                    close_fds=(admission.fileno(),), pass_fds=(writer,)))
                announcements.append(reader)
            for announcement in announcements:
                self.assertTrue(select.select([announcement], [], [], guard.remaining())[0])
                self.assertEqual(os.read(announcement, 6).decode(), "ready\n")
            _ = ready.wait(timeout=guard.remaining())
            fcntl.flock(admission, fcntl.LOCK_UN)
            results = [child.result(timeout=guard.remaining()) for child in processes]
        self.assertCountEqual([result.returncode for result in results], [0, 4, 4, 4],
                              [(result.stdout, result.stderr) for result in results])
        self.assertEqual(len(list(runs.glob("*.json"))), 1)

    def test_start_and_reap_preserve_an_unpublished_record_under_the_start_lock(self) -> None:
        _ = (self.root / "config/detached_runs.conf").write_text("# whole: 1..\nLIVE_RUN_LIMIT=1\n")
        script = """
import fcntl
import sys
from types import SimpleNamespace
from unittest.mock import patch
from scripts.agents import detached_runs
flock = fcntl.flock
def lock(fd, operation):
    print('locking', flush=True)
    return flock(fd, operation)
with (patch.object(fcntl, 'flock', lock),
      patch.object(detached_runs, 'deliver', return_value='fixture'),
      patch.object(detached_runs.treekey, 'tree_key', return_value='key-1'),
      patch.object(detached_runs.subprocess, 'Popen', return_value=SimpleNamespace(pid=4321))):
    args = [sys.argv[1], '--session-dir', sys.argv[2]]
    if sys.argv[1] == 'start':
        args += ['--to', sys.argv[3], '--', 'new-check']
    code = detached_runs.main(args)
sys.exit(code)
"""
        for verb, expected in (("start", 4), ("reap", 0)):
            with self.subTest(verb=verb):
                path = self.live_run(["fixture", "publishing"])
                supervisor = cast(int, json.loads(path.read_text())["pid"])
                guard = LauncherHangGuard("reap during supervisor publication")
                with contextlib.ExitStack() as children, (path.parent / ".start.lock").open("a") as admission:
                    fcntl.flock(admission, fcntl.LOCK_EX)
                    try:
                        self.update_record(path, pid=0, started_at=time.time() - 600)
                        unpublished = path.read_bytes()
                        child = children.enter_context(subprocess.Popen(
                            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", script, verb, str(self.session), WORKER],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                        assert child.stdout is not None
                        self.assertTrue(select.select([child.stdout], [], [], guard.remaining())[0])
                        self.assertEqual(child.stdout.readline(), "locking\n")
                        self.assertEqual(path.read_bytes(), unpublished)
                        self.update_record(path, pid=supervisor)
                    finally:
                        fcntl.flock(admission, fcntl.LOCK_UN)
                    output, errors = child.communicate(timeout=guard.remaining())
                self.assertEqual(child.returncode, expected, output + errors)
                self.assertEqual(json.loads(path.read_text())["status"], "running")
                self.assertEqual(list(path.parent.glob("*.json")), [path])

    def test_a_supervisor_holds_a_place_when_start_exits_before_its_pid_write(self) -> None:
        _ = (self.root / "config/detached_runs.conf").write_text("# whole: 1..\nLIVE_RUN_LIMIT=1\n")
        script = """
import os
import sys
from unittest.mock import patch
from scripts.agents import detached_runs
write_record = detached_runs._write_record
def publish(path, record):
    if record['pid']:
        os._exit(99)
    write_record(path, record)
with (patch.object(detached_runs, '_write_record', publish),
      patch.object(detached_runs.treekey, 'tree_key', return_value='key-1')):
    detached_runs.main(sys.argv[1:])
"""
        command = """
import socket
import sys
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(float(sys.argv[2]))
    connection.connect(sys.argv[1])
    connection.sendall(b'ready')
    connection.recv(1)
sys.exit(1)
"""
        guard = LauncherHangGuard("supervisor publication after start exits")
        address = str(self.root / "ready.sock")
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(address)
            listener.listen()
            listener.settimeout(guard.remaining())
            started = subprocess.run(
                [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", script, "start", "--session-dir", str(self.session), "--to", WORKER,
                 "--done-message-file", str(self.root / "unused-done-message"), "--", sys.executable,
                 "-c", command, address, str(guard.remaining())],
                capture_output=True, text=True, timeout=guard.remaining(), check=False)
            self.assertEqual(started.returncode, 99, started.stdout + started.stderr)
            connection = listener.accept()[0]
            with connection:
                connection.settimeout(guard.remaining())
                self.assertEqual(connection.recv(5), b"ready")
                paths = list((self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))
                self.assertEqual(len(paths), 1)
                record = cast(detached_runs.RunRecord, json.loads(paths[0].read_text()))
                self.assertGreater(record["pid"], 0)
                self.assertTrue(detached_run_process.supervises(record["pid"], paths[0]))
                supervisor = os.pidfd_open(record["pid"])
                try:
                    with patch.object(subprocess, "Popen") as popen, \
                            contextlib.redirect_stderr(io.StringIO()) as errors:
                        code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
                                               "--", "another-check"])
                    self.assertEqual(code, 4, errors.getvalue())
                    popen.assert_not_called()
                    self.assertEqual(list(paths[0].parent.glob("*.json")), paths)
                    connection.sendall(b"x")
                    self.assertTrue(select.select([supervisor], [], [], guard.remaining())[0])
                finally:
                    os.close(supervisor)

    # hana_catalyst-inspectors started one lint run three times after an interrupt (2026-10-21).
    def test_a_second_identical_start_names_the_live_run_and_starts_nothing(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        _ = self.live_run(command)
        popen, output = self.start(command)
        popen.assert_not_called()
        self.assertIn(f"run r1 is already running for {WORKER}", output)
        self.assertIn("next message", output)
        self.assertEqual(len(list((self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))), 1)

    def test_a_start_after_the_files_changed_or_with_no_tree_key_starts_a_new_run(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        _ = self.live_run(command)
        for key in ("key-2", None):
            with patch.object(treekey, "tree_key", return_value=key):
                popen, _ = self.start(command)
            popen.assert_called_once()
        records = [cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
                   for path in (self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json")]
        self.assertEqual(sorted(str(record.get("tree_key")) for record in records), ["", "key-1", "key-2"])

    def test_another_command_seat_or_folder_starts_a_new_run(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        _ = self.live_run(command)
        _ = self.record_worker("unit-review", fixture_runtime_id("detached-run-other-reviewer"))
        for other_command, seat in ((["bash", "verify.sh", "check", "hana"], WORKER), (command, "unit-review")):
            popen, _ = self.start(other_command, seat)
            popen.assert_called_once()
        owner = staff.by_id(self.unit.id)
        assert owner is not None
        _ = staff.set_worktree(owner.id, staff.PresentWorktree("natedev", self.root), expected=owner.worktree)
        with patch.object(treekey, "tree_key", return_value="key-after-folder-move"):
            popen, _ = self.start(command)
        popen.assert_called_once()

    def test_a_run_whose_supervisor_is_dead_or_another_process_starts_a_new_run(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        for pid in (DEAD_PID, os.getpid()):
            _ = self.live_run(command, pid)
            popen, _ = self.start(command)
            popen.assert_called_once()

    # On 2026-10-22 a unit stopped 23 queued runs by their process group: a supervisor that dies
    # delivers nothing, and its worker waits for a result that never comes.
    def test_a_start_ends_a_run_whose_supervisor_is_gone_with_a_failure_result(self) -> None:
        path = self.record(["bash", "verify.sh", "test", "hana"])
        self.update_record(path, pid=DEAD_PID)
        popen, _ = self.start(["bash", "verify.sh", "lint", "hana"])
        popen.assert_called_once()
        record = cast(detached_runs.RunRecord, json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual((record["status"], record.get("ended"), record.get("exit_code")),
                         ("finished", "supervisor gone", None))
        messages = cast(list[str], self.pending()["messages"])
        self.assertEqual(len(messages), 1)
        self.assertIn("Run ended with no result: verify.sh check\nResult: FAIL (no exit status)", messages[0])
        self.assertIn("Start the run again", messages[0])
        # The new run's supervisor is a mock; drop it, then a second pass finds nothing to end.
        for started in path.parent.glob("*.json"):
            if started != path:
                started.unlink()
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(len(cast(list[str], self.pending()["messages"])), 1)

    def test_a_dead_run_that_a_newer_run_of_its_command_answers_delivers_nothing(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        older = self.record(command)
        self.update_record(older, pid=DEAD_PID, started_at=time.time() - 600)
        newer = older.with_name("r2.json")
        _ = newer.write_text(older.read_text(encoding="utf-8"), encoding="utf-8")
        self.update_record(newer, run_id="r2", started_at=time.time(), pid=DEAD_PID)
        self.assertEqual(detached_runs.reap_orphans(str(self.session), WORKER), ["r1", "r2"])
        older_record = cast(detached_runs.RunRecord, json.loads(older.read_text(encoding="utf-8")))
        self.assertIn("a newer run of the same command answers", older_record.get("delivery", ""))
        messages = cast(list[str], self.pending()["messages"])
        self.assertEqual(len(messages), 1, "only the newest dead run reports")


    def test_a_supervisor_for_a_different_record_is_not_trusted(self) -> None:
        path = self.live_run(["echo", "x"])
        original = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertTrue(detached_run_process.supervises(original["pid"], path))
        another = path.with_name("another.json")
        _ = another.write_text(path.read_text())
        self.assertFalse(detached_run_process.supervises(original["pid"], another))
        self.assertFalse(detached_run_process.supervises(os.getpid(), path))
        alias = self.root / "record-alias.json"
        alias.symlink_to(path)
        for recorded in (alias, Path(os.path.relpath(path))):
            with self.subTest(recorded=recorded):
                with patch.object(detached_run_process, "process_args",
                                  return_value=["python", "detached_runs.py", "supervise", str(recorded)]):
                    self.assertTrue(detached_run_process.supervises(4321, path))
        command = ["assign.py", "impl", "prompt with spaces"]
        for arguments, matches in ((["python", "/fixture/assign.py", *command[1:]], True),
                                   ([], False),
                                   (["python", "/fixture/other.py", *command[1:]], False),
                                   (["python", "/fixture/assign.py", "review", command[2]], False)):
            with self.subTest(arguments=arguments):
                with patch.object(detached_run_process, "process_args", return_value=arguments):
                    self.assertEqual(detached_run_process.runs_command(4321, command), matches)
        for argument in ("prompt with spaces", "prompt  with  spaces"):
            with self.subTest(argument=argument):
                output = subprocess.CompletedProcess(["ps"], 0, f"python /fixture/assign.py impl {argument}\n", "")
                with (patch.object(Path, "exists", return_value=False),
                      patch.object(subprocess, "run", return_value=output)):
                    self.assertTrue(detached_run_process.runs_command(4321, ["assign.py", "impl", argument]))
        record = self.root / "run folder/r1.json"
        for tail, matches in (("", True), (" backup.json", False)):
            with self.subTest(tail=tail):
                output = subprocess.CompletedProcess(["ps"], 0, f"python detached_runs.py supervise {record}{tail}\n", "")
                with (patch.object(Path, "exists", return_value=False),
                      patch.object(subprocess, "run", return_value=output)):
                    self.assertEqual(detached_run_process.supervises(4321, record), matches)

    def test_a_live_supervisor_is_not_ended(self) -> None:
        path = self.live_run(["bash", "verify.sh", "check"])
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "running")

    def test_start_exits_3_for_a_caller_without_a_registered_worker(self) -> None:
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        for environment in ({"CODEX_THREAD_ID": "thread-unknown"}, {"CODEX_THREAD_ID": ""}):
            with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen):
                self.assertEqual(detached_runs.main(["start", "--", "true"]), detached_runs.NOT_A_DETACHED_WORKER)
        _ = staff.retire(self.worker_id)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_ONE}), patch.object(subprocess, "Popen", popen):
            self.assertEqual(detached_runs.main(["start", "--", "true"]), detached_runs.NOT_A_DETACHED_WORKER)
        popen.assert_not_called()

    # Criterion 2: the result is held for the seat, then the record is marked finished.

    def test_scope_prefix_needs_a_user_manager_and_honours_the_off_switch(self) -> None:
        runtime = Path(self.stack.enter_context(short_socket_directory())) / "runtime"
        (runtime / "systemd").mkdir(parents=True)
        marker = self.root / "r1.scope"
        on = {"XDG_RUNTIME_DIR": str(runtime)}
        with patch("shutil.which", return_value="/bin/systemd-run"):
            self.assertEqual(detached_runs.scope_prefix(marker, on), [])
            with socket.socket(socket.AF_UNIX) as manager:
                manager.bind(str(runtime / "systemd" / "private"))
                prefix = detached_runs.scope_prefix(marker, on)
                self.assertEqual(detached_runs.scope_prefix(marker, {**on, "BUILDLOG_SCOPE": "0"}), [])
        self.assertEqual(prefix[:2], ["systemd-run", "--user"])
        self.assertIn("--slice=builds-detached.slice", prefix)
        self.assertEqual(prefix[-4:], ["/bin/sh", "-c", detached_runs.SCOPE_SCRIPT, str(marker)])
        with patch("shutil.which", return_value=None):
            self.assertEqual(detached_runs.scope_prefix(marker, on), [])

    def test_command_receives_the_recorded_staff_owner_without_a_session_environment(self) -> None:
        command = [sys.executable, "-c", "import os; print(os.environ['BUILDLOG_OWNER_STAFF_ID'])"]
        path = self.record(command)
        _ = self.record_worker(WORKER, THREAD_TWO)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
                                     "BUILDLOG_OWNER_STAFF_ID": "unrelated-owner"}):
            record = self.supervise(path)
        self.assertEqual(record.get("exit_code"), 0)
        self.assertEqual(Path(record["log"]).read_text().strip(), str(self.worker_id))

    def test_a_scope_runs_the_command_once_and_a_failed_scope_runs_it_plainly(self) -> None:
        count = self.root / "count"
        command = ["/bin/sh", "-c", f'echo run >> "{count}"']
        scope = self.session / detached_runs.DETACHED_RUNS_DIR / "r1.scope"
        # A scope without systemd-run: the same script, so the marker proves the command started.
        with patch.object(detached_runs, "scope_prefix", return_value=["/bin/sh", "-c", detached_runs.SCOPE_SCRIPT, str(scope)]):
            record = self.supervise(self.record(command))
        self.assertEqual((record.get("exit_code"), count.read_text(encoding="utf-8")), (0, "run\n"))
        self.assertFalse(scope.exists())
        count.unlink()
        (session_staff.inbox_path(self.worker_id)).unlink()
        # A scope that fails before the command starts leaves no marker.
        with patch.object(detached_runs, "scope_prefix", return_value=["/bin/sh", "-c", "exit 1", "scope"]):
            record = self.supervise(self.record(command))
        self.assertEqual((record.get("exit_code"), count.read_text(encoding="utf-8")), (0, "run\n"))

    def test_failure_and_done_message_rules(self) -> None:
        path = self.record([sys.executable, "-c", "raise SystemExit(4)"])
        record = self.supervise(path)
        self.assertIn("Result: FAIL (exit 4)", cast(list[str], self.pending()["messages"])[0])
        self.assertEqual(record.get("exit_code"), 4)
        done = self.root / "done.txt"
        _ = done.write_text("Stage 2: go", encoding="utf-8")
        reason = "stopped for service deploy; run again after the release"
        (session_staff.inbox_path(self.worker_id)).unlink()
        path = self.record([
            sys.executable, "-c",
            "import json, sys; from pathlib import Path; path = Path(sys.argv[1]); "
            + "record = json.loads(path.read_text()); record['stop_reason'] = sys.argv[2]; "
            + "path.write_text(json.dumps(record)); raise SystemExit(143)",
            str(self.session / detached_runs.DETACHED_RUNS_DIR / "r1.json"), reason,
        ], done=str(done))
        record = self.supervise(path)
        message, = cast(list[str], self.pending()["messages"])
        self.assertIn("Result: FAIL (exit 143)", message)
        self.assertIn(reason, message)
        self.assertEqual(record.get("stop_reason"), reason)
        (session_staff.inbox_path(self.worker_id)).unlink()
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(1)"], done=str(done)))
        self.assertEqual(self.pending(), {})
        self.assertIn("nothing delivered", record.get("delivery", ""))
        _ = self.supervise(self.record([sys.executable, "-c", "pass"], done=str(done)))
        self.assertEqual(self.pending()["messages"], ["Stage 2: go"])

        with self.subTest(delivery="fresh Python process"):
            startup = self.root / "delivery-startup"
            startup.mkdir()
            sender_pid = self.root / "sender-pid"
            _ = (startup / "sitecustomize.py").write_text('''
import os
from pathlib import Path
from scripts.message import send
from scripts.production import staff

def fake_sender(*arguments, **options):
    Path(os.environ["FIXTURE_DELIVERY_PID"]).write_text(str(os.getpid()))
    return send.Result("queued", "private fresh process")

staff.local_snapshot = staff.initial_snapshot
send.deliver_worker_text = fake_sender
''')
            path = self.record([sys.executable, "-c", "pass"])
            with (patch.dict(os.environ, {
                    "PYTHONPATH": str(startup) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                    "FIXTURE_DELIVERY_PID": str(sender_pid),
                  }),
                  patch.object(detached_runs, "deliver_in_fresh_process", deliver_in_fresh_process)):
                record = self.supervise(path)
            self.assertTrue(sender_pid.is_file(), "The fresh process did not call the fake sender.")
            self.assertNotEqual(int(sender_pid.read_text()), record["pid"])
            self.assertNotEqual(int(sender_pid.read_text()), os.getpid())
            self.assertEqual(record.get("delivery"), "queued: private fresh process")

    def test_stop_reason_after_supervisor_completion_preserves_the_saved_result(self) -> None:
        path = self.record([sys.executable, "-c", "raise SystemExit(4)"])
        writer_progress, writer_finished = Event(), Event()
        guard = LauncherHangGuard("stop reason after supervisor completion")
        reason = "stopped for service deploy; run again after the release"
        flock = fcntl.flock
        completed = cast(dict[str, object], json.loads(path.read_text()))
        completed.update(status="finished", exit_code=4, finished_at="2026-11-10T21:00:00+00:00", delivery="fixture")

        def observe_lock(descriptor: int, operation: int) -> None:
            if current_thread().name.startswith("stop-reason"):
                writer_progress.set()
            flock(descriptor, operation)

        def save_reason() -> None:
            try:
                self.assertTrue(detached_runs.stop_with_reason(
                    path, reason, still_current=lambda: True, signal_run=lambda: None))
            finally:
                writer_finished.set()
                writer_progress.set()

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop-reason") as executor:
            with detached_runs.record_lock(path), patch.object(fcntl, "flock", side_effect=observe_lock):
                writer = executor.submit(save_reason)
                self.assertTrue(writer_progress.wait(timeout=guard.remaining()))
                self.assertFalse(writer_finished.is_set())
                self.assertFalse(writer.done())
                write_text_atomic(path, json.dumps(completed))
                self.assertFalse(writer_finished.is_set())
            writer.result(timeout=guard.remaining())
        saved = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(saved["status"], "finished")
        self.assertEqual(saved["stop_reason"], reason)
        for field in ("exit_code", "finished_at", "delivery"):
            with self.subTest(field=field):
                self.assertEqual(saved[field], completed[field])

    def test_cancelled_stop_reason_is_absent_from_the_delivered_result(self) -> None:
        reason = "stopped for service deploy; run again after the release"
        record_lock = detached_runs.record_lock
        for timing in ("before publication", "during recheck"):
            with self.subTest(cancellation=timing):
                session_staff.inbox_path(self.worker_id).unlink(missing_ok=True)
                path = self.record([sys.executable, "-c", "pass"])
                cancelled = Event()
                if timing == "before publication":
                    self.assertTrue(detached_runs.stop_with_reason(
                        path, reason, still_current=lambda: True, signal_run=lambda: None))

                    @contextlib.contextmanager
                    def cancel_before_save(path: Path) -> Generator[None]:
                        if not cancelled.is_set():
                            cancelled.set()
                            self.assertFalse(detached_runs.stop_with_reason(
                                path, reason, still_current=lambda: False,
                                signal_run=lambda: self.fail("a cancelled stop must not signal")))
                        with record_lock(path):
                            yield

                    with patch.object(detached_runs, "record_lock", side_effect=cancel_before_save):
                        record = self.supervise(path)
                else:
                    done = self.root / "done.txt"
                    _ = done.write_text("Result: PASS (exit 0)\nStage 2: go")
                    self.update_record(path, done_message_file=str(done))
                    proc = self.root / "proc"
                    for pid, parent, start in ((100, 1, "1000"), (10, 100, "2000")):
                        directory = proc / str(pid)
                        directory.mkdir(parents=True)
                        _ = (directory / "stat").write_text(
                            f"{pid} (gate) S {parent} " + "0 " * 17 + start)
                    supervisor = build_hold.AuditProcess(
                        build_hold.ProcessIdentity(100, "1000"), 1, "python3",
                        ("python3", "detached_runs.py", "supervise", str(path)), str(self.root), {}, False)
                    gate = build_hold.AuditProcess(
                        build_hold.ProcessIdentity(10, "2000"), 100, "bash",
                        ("bash", "verify.sh", "check"), str(self.root), {}, False)
                    holder = build_hold.Holder("holder", datetime.now(timezone.utc), "service deploy",
                                               build_hold.UnknownReleaseEta(), "holder")
                    run = hold_enforcer.WaitingRun(gate, NoOwningSession())
                    guard = LauncherHangGuard("cancelled stop blocks supervisor publication")
                    rechecking, supervisor_record_section = Event(), Event()
                    write_attempted, supervisor_blocked = Event(), Event()
                    flock = fcntl.flock
                    read_start = build_hold.process_start_time
                    checks = 0

                    @contextlib.contextmanager
                    def observe_record_lock(path: Path) -> Generator[None]:
                        if current_thread().name.startswith("supervisor-race"):
                            supervisor_record_section.set()
                        with record_lock(path):
                            yield

                    def observe_flock(descriptor: int, operation: int) -> None:
                        if (current_thread().name.startswith("supervisor-race")
                                and supervisor_record_section.is_set()):
                            try:
                                flock(descriptor, operation | fcntl.LOCK_NB)
                            except BlockingIOError:
                                supervisor_blocked.set()
                                write_attempted.set()
                                flock(descriptor, operation)
                            else:
                                write_attempted.set()
                        else:
                            flock(descriptor, operation)

                    def reuse_at_recheck(pid: int, proc_root: Path = build_hold.PROC_ROOT) -> str:
                        nonlocal checks
                        if pid == 10:
                            checks += 1
                            if checks == 2:
                                rechecking.set()
                                self.assertTrue(write_attempted.wait(timeout=guard.remaining()))
                                self.assertTrue(supervisor_blocked.is_set(), "the drain must hold the record lock")
                                _ = (proc / "10/stat").write_text("10 (reused) S 100 " + "0 " * 17 + "3000")
                                cancelled.set()
                        return read_start(pid, proc_root)

                    def supervise_at_recheck() -> detached_runs.RunRecord:
                        self.assertTrue(rechecking.wait(timeout=guard.remaining()))
                        return self.supervise(path)

                    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="supervisor-race") as executor, \
                         patch.object(detached_runs, "record_lock", side_effect=observe_record_lock), \
                         patch.object(fcntl, "flock", side_effect=observe_flock), \
                         patch.object(build_hold, "process_start_time", side_effect=reuse_at_recheck), \
                         patch.object(hold_enforcer, "record_build"), patch.object(hold_enforcer, "set_stop_status"), \
                         patch.object(detached_runs, "_run", return_value=0):
                        publication = executor.submit(supervise_at_recheck)
                        try:
                            result = hold_enforcer.stop_waiting_run(
                                run, holder, {10: gate, 100: supervisor}, proc,
                                supervisor_args=lambda pid, proc_root: supervisor.argv,
                                signal_process=lambda pid, signum: self.fail("a cancelled stop must not signal"),
                                signal_group=lambda pid, signum: self.fail("a cancelled stop must not signal"))
                            self.assertIsInstance(result, hold_enforcer.RunIdentityChanged)
                        finally:
                            rechecking.set()
                        record = publication.result(timeout=guard.remaining())
                message, = cast(list[str], self.pending()["messages"])
                self.assertTrue(cancelled.is_set())
                self.assertIn("Result: PASS (exit 0)", message)
                self.assertNotIn(reason, message)
                self.assertNotIn("stop_reason", record)
                if timing == "during recheck":
                    self.assertIn("Stage 2: go", message)

    def test_a_superseded_run_delivers_a_result_that_is_not_a_failure(self) -> None:
        done = self.root / "done.txt"
        _ = done.write_text("Stage 2: go", encoding="utf-8")
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(76)"], done=str(done)))
        message = cast(list[str], self.pending()["messages"])[0]
        self.assertIn("Result: SUPERSEDED (exit 76)", message)
        self.assertIn("nothing needs a repair", message)
        self.assertNotIn("FAIL", message)
        self.assertNotIn("Stage 2: go", message)
        self.assertEqual(record.get("exit_code"), 76)

    def test_a_line_over_codexs_input_limit_delivers_cut_under_the_cap(self) -> None:
        limits = conf.read("detached_runs")
        line_chars = limits.whole("RESULT_LINE_CHARS")
        text_chars = limits.whole("RESULT_TEXT_CHARS")
        script = "print('x' * 1_100_000)\nfor n in range(30): print(str(n) * 5_000)\nprint('last line')"
        _ = self.supervise(self.record([sys.executable, "-c", script]))
        message = cast(list[str], self.pending()["messages"])[0]
        self.assertLessEqual(len(message), text_chars)
        self.assertIn(f"{'29' * (line_chars // 2)} [cut: {10_000 - line_chars} more characters]", message)
        self.assertTrue(message.endswith("\nlast line"))
        _ = self.supervise(self.record([sys.executable, "-c", "print('x' * 1_100_000)"]))
        message = cast(list[str], self.pending()["messages"])[-1]
        self.assertIn(f"{'x' * line_chars} [cut: {1_100_000 - line_chars} more characters]", message)
        self.assertLessEqual(len(message), text_chars)

    def test_result_text_caps_the_total_size_including_the_header(self) -> None:
        record = cast(detached_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        record["label"] = "verification " * 1_000
        _ = Path(record["log"]).write_text("line " * 1_000 + "\n" + ("x" * 5_000 + "\n") * 20 + "last line\n")

        text = detached_runs.result_text(record, detached_runs.CompletedCommand(1))

        self.assertLessEqual(len(text), 2_000)
        self.assertIn("Result: FAIL (exit 1)", text)
        self.assertIn(f"Log: {record['log']}", text)
        self.assertTrue(text.endswith("\nlast line"))

        config = self.root / "config/detached_runs.conf"
        _ = config.write_text(config.read_text().replace("RESULT_LINE_CHARS=300", "RESULT_LINE_CHARS=100")
                              .replace("RESULT_TEXT_CHARS=2000", "RESULT_TEXT_CHARS=1200"))
        text = detached_runs.result_text(record, detached_runs.CompletedCommand(1))
        self.assertLessEqual(len(text), 1_200)
        self.assertIn(f"{'x' * 100} [cut: {5_000 - 100} more characters]", text)
        self.assertIn("Result: FAIL (exit 1)", text)
        self.assertIn(f"Log: {record['log']}", text)
        self.assertTrue(text.endswith("\nlast line"))

    def test_result_text_collapses_consecutive_memory_wait_lines_before_taking_the_tail(self) -> None:
        record = cast(detached_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        lines = ["first diagnostic", *(f"still waiting for memory: {n}" for n in range(30)),
                 "second diagnostic", "still waiting for memory: retry", "still waiting for memory: ready", "last line"]
        _ = Path(record["log"]).write_text("\n".join(lines))

        text = detached_runs.result_text(record, detached_runs.CompletedCommand(0))

        self.assertIn("first diagnostic", text)
        self.assertIn("second diagnostic", text)
        self.assertEqual([line for line in text.splitlines() if line.startswith("still waiting for memory")],
                         ["still waiting for memory: 29", "still waiting for memory: ready"])
        self.assertTrue(text.endswith("\nlast line"))


    # Criterion 3: the server and thread change between start and finish.

    # Criterion 4: no launcher sends the result to the director; an attached launcher holds it.


    def test_a_record_without_a_pid_counts_as_live_only_at_first(self) -> None:
        path = self.record(["true"])
        record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        record["pid"] = 0
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        record["started_at"] = time.time() - 120
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])


class PaneRunDeliveryTests(unittest.TestCase):
    fixture: pane_support.WorkerPaneTests = pane_support.WorkerPaneTests()
    inputs: list[tuple[str, str]] = []
    notices: list[tuple[str, str]] = []
    interrupt_after_input: bool = False
    defer_transcript: bool = False
    pending_transcripts: list[tuple[Path, str]] = []

    @override
    def setUp(self) -> None:
        self.fixture = pane_support.WorkerPaneTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.inputs = []
        self.notices = []
        self.defer_transcript = False
        self.pending_transcripts = []
        config = self.fixture.root / "conf"
        for name in ("detached_runs", "send", "postmaster", "codex_daemon"):
            write_text_atomic(config / f"{name}.conf",
                              (Path(__file__).resolve().parents[2] / f"config/{name}.conf").read_text())
        for name, keys in (("send", {"delivery_timeout", "kill_grace", "remote_transport_seconds",
                                     "waits_review_seconds", "caller_margin_seconds"}),
                           ("postmaster", {"wait_seconds", "claim_grace_seconds", "poll_seconds"}),
                           ("codex_daemon", {"timeout_seconds"})):
            path = config / f"{name}.conf"
            write_text_atomic(path, "\n".join(line.split("=", 1)[0] + "=0.001"
                                             if line.split("=", 1)[0] in keys else line
                                             for line in path.read_text().splitlines()) + "\n")
        self.run_dir.mkdir()
        write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps({"owner_staff_id": self.fixture.parent.id}))
        _ = staff.set_run(self.fixture.parent.id, staff.OpenRun(self.run_dir))
        server_run = self.fixture.world.run

        def input_line(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("agent", "prompt"):
                pane, text = arguments[2:4]
                pid = self.fixture.world.foreground[pane][0]
                identity = self.fixture.world.processes[pid].current_identity
                row = staff.snapshot().by_current_session(identity)
                assert row is not None
                backend = staff.backend(row)
                assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
                source = worker_admission.transcript_source(row, backend)
                entry = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
                if self.defer_transcript:
                    self.pending_transcripts.append((source, entry))
                else:
                    write_text_atomic(source, source.read_text() + entry)
                self.inputs.append((pane, text))
                if self.interrupt_after_input:
                    raise SystemExit("delivery interrupted after pane input")
            return server_run(*arguments, deadline=deadline)

        def holder_notice(options: send.Options, *, membership: staff.StaffSnapshot | None = None) -> send.Result:
            del membership
            self.notices.append((options.to, options.text or ""))
            return send.Result("sent", "holder received the result")

        _ = self.enterContext(patch.object(self.fixture.world, "run", input_line))
        _ = self.enterContext(patch.object(send, "_send", holder_notice))
        _ = self.enterContext(patch.object(detached_runs, "deliver_in_fresh_process",
                                          side_effect=deliver_in_fixture))

    @property
    def run_dir(self) -> Path:
        return self.fixture.root / "run"

    def record(self, row: staff.StaffRow, run_id: str = "pane-result") -> detached_runs.RunRecord:
        runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        log = runs / f"{run_id}.log"
        write_text_atomic(log, "private verification output\n")
        record: detached_runs.RunRecord = {"run_id": run_id, "status": "running", "session_dir": str(self.run_dir),
                "worker": row.name, "staff_id": row.id, "label": "fixture gate", "command": ["true"],
                "log": str(log), "started_at": time.time(), "pid": 0,
                "director_dir": str(self.run_dir), "done_message_file": ""}
        write_text_atomic(log.with_suffix(".json"), json.dumps(record))
        return record

    def assert_holder_result(self, record: detached_runs.RunRecord, reason: str) -> None:
        result = detached_runs.deliver(record, detached_runs.CompletedCommand(1))
        self.assertIn("sent", result)
        self.assertEqual(self.inputs, [])
        self.assertEqual(len(self.notices), 1)
        target, text = self.notices[0]
        self.assertEqual(target, f"session:{self.fixture.parent.current_session}")
        self.assertIn(reason, text)
        self.assertIn(record["run_id"], text)
        self.assertIn("private verification output", text)

    def test_detached_callers_use_current_native_sessions_from_staff(self) -> None:
        families: tuple[tuple[Literal["claude", "codex"], str], ...] = (
            ("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"))
        for family, variable in families:
            with self.subTest(family=family):
                row = self.fixture.worker("worker-" + family, family=family)
                before = self.fixture.staff_state()
                with patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}), \
                        patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as read:
                    caller = detached_runs.caller_from_environment({variable: row.current_session, "UNIT_RUN_DIR": str(self.run_dir)})
                self.assertEqual(read.call_count, 1)
                self.assertEqual(self.fixture.staff_state(), before)
                self.assertIsInstance(caller, detached_runs.PaneCaller)
                assert isinstance(caller, detached_runs.PaneCaller)
                self.assertEqual((caller.session_dir, caller.name, caller.staff_id), (str(self.run_dir), row.name, row.id))
                with patch.dict(os.environ, {"CODEX_THREAD_ID": row.id}):
                    self.assertIsInstance(detached_runs.caller_from_environment({variable: row.id}), detached_runs.NotDetachedWorker)

    def test_a_pane_workers_run_keeps_its_owner_when_options_name_another_worker(self) -> None:
        row = self.fixture.worker("TEST-command-owner", family="codex")
        other = self.fixture.worker("TEST-impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        self.fixture.world.add(other, "w1:p2", family="codex")
        other_run = self.fixture.root / "other-run"
        other_run.mkdir()
        write_text_atomic(other_run / unit_runs.STATE_FILE, json.dumps({"owner_staff_id": self.fixture.parent.id}))
        # The staff row names the current run.
        _ = staff.set_run(self.fixture.parent.id, staff.OpenRun(self.run_dir))
        runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
        for requested_run in (self.run_dir, other_run):
            with self.subTest(session_dir=requested_run):
                before = set(runs.glob("*.json"))
                with (patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}),
                      patch.object(treekey, "tree_key", return_value="private-tree"),
                      patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)) as spawn,
                      contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO()) as errors):
                    code = detached_runs.main(["start", "--session-dir", str(requested_run), "--to", other.name,
                                           "--", "private-check"])
                self.assertEqual(code, 0, errors.getvalue())
                spawn.assert_called_once()
                path, = set(runs.glob("*.json")) - before
                record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                self.assertEqual((record["session_dir"], record["worker"], record["staff_id"]),
                                 (str(self.run_dir), row.name, row.id))
                self.assertEqual(errors.getvalue().splitlines(),
                    [f"detached_runs: --to {other.name} ignored; run belongs to worker {row.name}"])
        self.assertFalse((other_run / detached_runs.DETACHED_RUNS_DIR).exists())

    def test_run_started_for_a_pane_slot_stays_live_and_delivers_to_its_worker(self) -> None:
        _ = self.fixture.worker_run("a-first", "unit", self.fixture.parent.current_session,
                                director_run=True)
        self.fixture.world.add(self.fixture.worker("test", family="codex"), "w1:p3", family="codex")
        cases = (("detached-command-owner", "w1:p1", False, False), ("test2", "w1:p2", False, False),
                 ("implicit-command-owner", "w1:p4", True, False), ("completed-owner", "w1:p5", False, True))
        for name, pane, implicit, completed in cases:
            with self.subTest(name=name, implicit=implicit):
                row = self.fixture.worker(name, family="codex")
                self.fixture.world.add(row, pane, family="codex")
                prompt = self.fixture.root / "assignment.txt"
                write_text_atomic(prompt, "Run the private command.")
                write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps({
                    "owner_staff_id": self.fixture.parent.id,
                    "history_file": str(self.fixture.root / "history.jsonl"),
                    "phase": {"id": "40", "instance_id": "private-phase", "status": "active"},
                }))
                dispatch_id = progress_history.begin_dispatch(self.run_dir, slot="test", phase="40", role="test",
                    worker_name=row.name, worker_id=row.id, launcher_command=["private-launcher"], prompt=prompt,
                    kind="test", lens="")
                runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
                before = set(runs.glob("*.json"))
                input_count = len(self.inputs)
                output = io.StringIO()
                route = [] if implicit else ["--session-dir", str(self.run_dir), "--to", "test"]
                with (patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}),
                      patch.object(treekey, "tree_key", return_value="private-tree"),
                      patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)),
                      contextlib.redirect_stdout(output)):
                    code = detached_runs.main(["start", *route, "--", "true"])
                self.assertEqual(code, 0, output.getvalue())
                path, = set(runs.glob("*.json")) - before
                record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                self.assertEqual((record["worker"], record["staff_id"]), (row.name, row.id))
                record["pid"] = private_supervisor(self, path)
                write_text_atomic(path, json.dumps(record))
                self.assertEqual(detached_runs.live_runs(runs, row.name), [record])
                self.assertEqual(detached_runs.worker_runs(runs, row.name), [record])
                self.assertEqual(detached_runs.live_runs(runs, "test"), [])
                write_text_atomic(Path(record["log"]), "Private command completed.\n")
                self.assertIn("sent", detached_runs.deliver(record, detached_runs.CompletedCommand(0)))
                self.assertEqual(len(self.inputs), input_count + 1)
                self.assertEqual(self.inputs[-1][0], pane)
                self.assertIn(f"Detached run: {record['run_id']}", self.inputs[-1][1])
                self.assertEqual(self.notices, [])
                active = progress_history.active_dispatch(row.id, self.run_dir)
                self.assertIsInstance(active, progress_history.ActiveDispatch)
                assert isinstance(active, progress_history.ActiveDispatch)
                self.assertEqual(active.instance_id, dispatch_id)
                if completed:
                    agent = test_worker.DispatchAgent(self.fixture)
                    agent.pane_commands(self)
                    line = f"Read the unchanged prompt file {json.dumps(str(prompt))}. End your final reply with DONE {dispatch_id}."
                    reply = self.fixture.root / "finished-reply.txt"
                    progress_history.save_dispatch_delivery(self.run_dir, dispatch_id,
                        progress_history.DispatchDelivery(line, dispatch_id, str(reply)))
                    agent.append(pane, "user", line)
                    agent.append(pane, "assistant", "The command is complete.\nDONE " + dispatch_id)
                    self.fixture.world.locations[pane]["agent_status"] = "done"
                    with patch.object(subprocess, "run", side_effect=agent.run), patch.dict(os.environ, {"BUILDLOG_SCOPE": "0"}):
                        self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
                    self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.NoActiveDispatch)
                    self.assertTrue(reply.read_text().endswith(f"DONE {dispatch_id}\n"))
                    self.assertEqual(len(agent.notices), 1)
                    self.assertIn("phase 40: done", agent.notices[0])
                    self.assertIn(f"; pass {dispatch_id}", agent.notices[0])
                    self.assertNotIn(pane, self.fixture.world.locations)

    def test_native_caller_ignores_inherited_session_and_run(self) -> None:
        row = self.fixture.worker(family="claude")
        sibling = self.fixture.worker("sibling")
        registry = self.fixture.session_record(row.current_session, row.name)
        os.rename(registry, registry.with_name(f"{os.getpid()}.json"))
        stale_run = self.fixture.root / "stale-run"
        stale_run.mkdir()
        for inherited in ({"CLAUDE_CODE_SESSION_ID": sibling.current_session,
                           "UNIT_RUN_DIR": str(stale_run)}, {}):
            with self.subTest(inherited=bool(inherited)), patch.dict(os.environ, {
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": inherited.get("CLAUDE_CODE_SESSION_ID", ""),
                    "UNIT_RUN_DIR": str(stale_run)}):
                caller = detached_runs.caller_from_environment(inherited)
                self.assertIsInstance(caller, detached_runs.PaneCaller)
                assert isinstance(caller, detached_runs.PaneCaller)
                self.assertEqual((caller.staff_id, caller.session_dir), (row.id, str(self.run_dir)))

    def test_failed_result_delivery_recovers_the_original_command_result(self) -> None:
        row = self.fixture.worker()
        record = self.record(row)
        record["command"] = [sys.executable, "-c", "raise SystemExit(9)"]
        record["pid"] = DEAD_PID
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))

        def failed_holder(options: send.Options, *, membership: staff.StaffSnapshot | None = None) -> send.Result:
            del options, membership
            return send.Result("failed", "daemon unavailable")

        with patch.object(send, "_send", failed_holder):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 1)
            pending = cast(detached_runs.RunRecord, json.loads(path.read_text()))
            self.assertEqual(pending["status"], "running")
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
        finished = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(len(self.notices), 1)
        self.assertIn("FAIL (exit 9)", self.notices[0][1])
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])

    def test_a_failed_completion_event_keeps_other_orphans_and_retries_next_pass(self) -> None:
        row = self.fixture.worker(family="codex")
        records = [self.record(row, name) for name in ("first-result", "second-result")]
        for record in records:
            record["pid"] = DEAD_PID
            record["command"] = ["echo", record["run_id"]]
            write_text_atomic(Path(record["log"]).with_suffix(".json"), json.dumps(record))
        complete = turn_end.run_finished
        calls: list[str] = []

        def fail_once(thread: str) -> None:
            calls.append(thread)
            if len(calls) == 1:
                raise ValueError("director unavailable")
            complete(thread)

        with patch.object(turn_end, "run_finished", side_effect=fail_once), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), ["first-result", "second-result"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(errors.getvalue().count("anomaly:"), 1)
            self.assertIn("director unavailable", errors.getvalue())
            with patch.object(detached_run_process, "supervises", return_value=True) as supervises:
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                self.assertEqual(len(calls), 2)
                supervises.assert_called_once_with(DEAD_PID, Path(records[0]["log"]).with_suffix(".json"))
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(len(calls), 3)
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.notices), 2)
        for record in records:
            finished = cast(dict[str, object], json.loads(Path(record["log"]).with_suffix(".json").read_text()))
            self.assertEqual(finished["status"], "finished")
            self.assertFalse(finished["completion_pending"])

    def test_delayed_transcript_publication_retypes_without_a_receipt(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        record = self.record(row)
        self.defer_transcript = True
        read_entries = worker_admission.transcript_entries

        def delayed_entries(source: Path, changed: set[str]) -> Iterator[str | worker_admission.TornTranscriptRecord]:
            entries = list(read_entries(source, changed))
            for pending_source, entry in self.pending_transcripts:
                write_text_atomic(pending_source, pending_source.read_text() + entry)
            self.pending_transcripts.clear()
            yield from entries

        with patch.object(worker_admission, "transcript_entries", delayed_entries), ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(detached_runs.deliver, record, detached_runs.CompletedCommand(0)), pool.submit(detached_runs.deliver, record, detached_runs.CompletedCommand(0)))
            results = [future.result() for future in futures]
        self.assertEqual(len(self.inputs), 2)
        self.assertEqual(sum("already delivered" in result for result in results), 0)

    def test_receipt_timeout_recovery_retypes_without_a_receipt(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        record = self.record(row)
        record["pid"] = DEAD_PID
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.defer_transcript = True

        with contextlib.suppress(detached_runs.RunDeliveryFailed):
            _ = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        recovered = detached_runs.reap_orphans(str(self.run_dir))

        self.assertEqual(len(self.inputs), 2)
        self.assertEqual(recovered, [record["run_id"]])
        finished = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(len(self.pending_transcripts), 2)

    def test_result_queued_during_a_turn_finishes_without_a_transcript_receipt(self) -> None:
        row = self.fixture.worker(family="claude")
        self.fixture.world.add(row, "w1:p1")
        self.fixture.world.locations["w1:p1"]["agent_status"] = "working"
        record = self.record(row)
        path = Path(record["log"]).with_suffix(".json")
        self.defer_transcript = True

        statuses: list[object] = []
        run = self.fixture.world.run

        def delivery(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("agent", "prompt"):
                statuses.append(cast(dict[str, object], json.loads(path.read_text()))["status"])
            return run(*arguments, deadline=deadline)

        with patch.object(self.fixture.world, "run", side_effect=delivery):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        self.assertEqual(statuses, ["running"])
        finished = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        result = detached_runs.deliver(finished, detached_runs.CompletedCommand(0))

        self.assertEqual(finished["status"], "finished")
        self.assertIn("sent", result)
        self.assertEqual(len(self.inputs), 2)
        self.assertEqual(len(self.pending_transcripts), 2)
        self.assertEqual(self.notices, [])

    def test_interrupted_result_record_retypes_without_a_transcript_receipt_and_finishes(self) -> None:
        row = self.fixture.worker(family="claude")
        self.fixture.world.add(row, "w1:p1")
        record = self.record(row)
        record["pid"] = DEAD_PID
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.defer_transcript = True

        def interrupt_finish(target: Path, text: str) -> None:
            if target == path:
                saved = cast(detached_runs.RunRecord, json.loads(text))
                if saved["status"] == "finished":
                    raise SystemExit("interrupted before the finished record")
            write_text_atomic(target, text)

        with patch("scripts.agents.detached_runs.write_text_atomic", interrupt_finish):
            with self.assertRaisesRegex(SystemExit, "interrupted before the finished record"):
                _ = detached_runs.main(["supervise", str(path)])

        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
        self.assertEqual(len(self.inputs), 2)
        self.assertEqual(self.inputs[0], self.inputs[1])
        finished = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(self.notices, [])

    def test_pane_result_arrives_after_the_launcher_exits(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        supervisors: list[object] = []

        def start_supervisor(*arguments: object, **options: object) -> SimpleNamespace:
            del options
            supervisors.extend(arguments)
            return SimpleNamespace(pid=998901)

        with patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session, "BUILDLOG_SCOPE": "0"}), \
                patch.object(detached_runs, "subprocess", SimpleNamespace(
                    Popen=start_supervisor, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT)):
            _ = os.environ.pop("UNIT_RUN_DIR", None)
            _ = os.environ.pop("PLAN_DELEGATE_BOARD_DIR", None)
            self.assertEqual(detached_runs.main(["start", "--label", "private result", "--",
                                             sys.executable, "-c", "print('result after launcher exit')"]), 0)
        paths = list((self.run_dir / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(paths), 1)
        record = cast(detached_runs.RunRecord, json.loads(paths[0].read_text()))
        self.assertEqual(record["staff_id"], row.id)
        self.assertEqual(record["session_dir"], str(self.run_dir))
        self.assertIn("supervise", str(supervisors))

        self.assertEqual(detached_runs.main(["supervise", str(paths[0])]), 0)

        self.assertEqual([pane for pane, _ in self.inputs], ["w1:p1"])
        self.assertIn("result after launcher exit", self.inputs[0][1])
        self.assertEqual(self.notices, [])

    def test_result_follows_the_current_session_after_rename_and_transfer_during_work(self) -> None:
        row = self.fixture.worker(family="codex")
        record = self.record(row)
        row = staff.rename(row.id, "renamed-worker")
        self.fixture.world.add(row, "w2:p7", family="codex", label="old-label", workspace="w2", tab="w2:t2")
        self.fixture.world.locations["w2:p7"]["agent_status"] = "working"
        sibling = self.fixture.worker("sibling", family="codex")
        self.fixture.world.add(sibling, "w1:p2", family="codex")

        result = detached_runs.deliver(record, detached_runs.CompletedCommand(0))

        self.assertIn("w2:p7", result)
        self.assertEqual([pane for pane, _ in self.inputs], ["w2:p7"])
        self.assertIn(record["run_id"], self.inputs[0][1])
        self.assertIn("private verification output", self.inputs[0][1])
        self.assertEqual(self.notices, [])

    def test_delivery_rerun_after_type_types_nothing(self) -> None:
        row = self.fixture.worker(family="claude")
        self.fixture.world.add(row, "w1:p1")
        record = self.record(row)
        self.interrupt_after_input = True
        with self.assertRaisesRegex(SystemExit, "interrupted after pane input"):
            _ = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        self.interrupt_after_input = False

        result = detached_runs.deliver(record, detached_runs.CompletedCommand(0))

        self.assertIn("already delivered", result)
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.notices, [])

    def test_concurrent_deliveries_type_one_result(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        record = self.record(row)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(detached_runs.deliver, record, detached_runs.CompletedCommand(code)) for code in (0, 0)]
            results = [future.result() for future in futures]
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(sum("already delivered" in result for result in results), 1)
        self.assertEqual(self.notices, [])

    def test_distinct_run_ids_deliver_the_same_done_message_separately(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        done = self.fixture.root / "done.txt"
        write_text_atomic(done, "Read the verification report.")
        for run_id in ("first", "second"):
            record = self.record(row, run_id)
            record["done_message_file"] = str(done)
            _ = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
            _ = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        self.assertEqual(len(self.inputs), 2)
        self.assertIn("Detached run: first", self.inputs[0][1])
        self.assertIn("Detached run: second", self.inputs[1][1])
        self.assertTrue(all("Read the verification report." in text for _, text in self.inputs))

    def test_stopped_worker_result_reaches_its_holder(self) -> None:
        row = self.fixture.worker()
        self.fixture.world.add(row, "w1:p1", live=False)
        self.assert_holder_result(self.record(row), "worker is stopped")

    def test_closed_worker_result_reaches_its_holder(self) -> None:
        row = self.fixture.worker()
        self.assert_holder_result(self.record(row), "worker pane is closed")

    def test_unknown_process_result_reaches_its_holder_with_one_anomaly(self) -> None:
        row = self.fixture.worker()
        self.fixture.world.add(row, "w1:p1")
        self.fixture.world.unknown["w1:p1"] = "private process read failed"
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assert_holder_result(self.record(row), "private process read failed")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_duplicate_native_identity_result_reaches_its_holder(self) -> None:
        row = self.fixture.worker()
        self.fixture.world.add(row, "w1:p1")
        self.fixture.world.add(row, "w2:p2", tab="w2:t2", workspace="w2")
        self.assert_holder_result(self.record(row), "duplicate native identities")

    def test_blocked_worker_result_reaches_its_holder(self) -> None:
        row = self.fixture.worker()
        self.fixture.world.add(row, "w1:p1")
        self.fixture.world.locations["w1:p1"]["agent_status"] = "blocked"
        self.assert_holder_result(self.record(row), "worker is blocked")

    def test_unreadable_transcript_result_reaches_its_holder_without_input(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        source = worker_admission.transcript_source(row, staff.CodexBackend(self.fixture.root / "codex"))
        source.unlink()
        self.assert_holder_result(self.record(row), "worker transcript is unavailable")


if __name__ == "__main__":
    _ = unittest.main()
