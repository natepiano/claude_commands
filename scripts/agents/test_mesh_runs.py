"""Tests for mesh_runs.py: detached runs that message the worker who started them."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import math
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, NamedTuple, cast, override
from unittest.mock import ANY, MagicMock, patch

from scripts.production.staff_test_support import isolated_staff_file
from scripts.lib.signal_test_support import install_signal_pid_guard
from scripts.agents import codex_mesh, mesh_run_process, mesh_runs
from scripts.buildlog import treekey
from scripts.direct import progress_history
from scripts.astra import design_roles
from scripts.lib import conf, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.herdr import test_worker_panes as pane_support
from scripts.message import send
from scripts.lib.test_hang_guard import LauncherHangGuard
from scripts.lib.socket_test_support import short_socket_directory
from scripts.message.repeat_guard_test_support import install_guard_test_budget
from scripts.production import staff, session_staff, worker_admission
from scripts.usage import agents_log
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id, fixture_worker, fixture_session

WORKER = "unit-impl"
THREAD_ONE = fixture_runtime_id("mesh-run-thread-one")
THREAD_TWO = fixture_runtime_id("mesh-run-thread-two")
LASTING_THREAD = fixture_runtime_id("mesh-run-lasting-thread")
ADOPTED_THREAD = fixture_runtime_id("mesh-run-adopted-thread")
SHARED_THREAD = fixture_runtime_id("mesh-run-shared-thread")
DEAD_PID = 2 ** 22 + 12345


def setUpModule() -> None:
    install_signal_pid_guard()
    install_guard_test_budget()


class DirectorNotice(NamedTuple):
    session_id: str
    summary: str
    text: str


def private_supervisor(test: unittest.TestCase, path: Path) -> int:
    """Keep a private supervisor alive until the test closes its input."""
    guard = LauncherHangGuard("detached supervisor startup and cleanup")
    stand_in = subprocess.Popen([sys.executable, "-c",
                                 "import sys; print('ready', flush=True); sys.stdin.read()",
                                 "mesh_runs.py", "supervise", str(path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    test.addCleanup(lambda: stand_in.wait(timeout=guard.remaining()))
    test.addCleanup(stand_in.kill)
    assert stand_in.stdin is not None and stand_in.stdout is not None
    test.addCleanup(stand_in.stdin.close)
    test.addCleanup(stand_in.stdout.close)
    test.assertTrue(select.select([stand_in.stdout], [], [], guard.remaining())[0], "supervisor did not start")
    test.assertEqual(stand_in.stdout.readline(), "ready\n")
    return stand_in.pid


class MeshRunsTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    stack: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]
    sent: list[DirectorNotice]  # pyright: ignore[reportUninitializedInstanceVariable]
    unit: staff.StaffRow  # pyright: ignore[reportUninitializedInstanceVariable]
    worker_id: staff.StaffId  # pyright: ignore[reportUninitializedInstanceVariable]
    director_sender: Callable[[str, str, str], Literal["sent", "failed"] | mesh_runs.DirectorNoticeQueued]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.stack = contextlib.ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        _ = self.stack.enter_context(isolated_staff_file(self.root / "staff.json", {}))
        self.session = self.root / "unit" / "run-1"
        self.session.mkdir(parents=True)
        write_text_atomic(self.session / "worker_transport", "mesh\n")
        _ = self.stack.enter_context(patch.object(mesh_runs, "RUN_ROOTS", (self.root / "unit",)))
        self.stack.enter_context(patch.dict(os.environ, {
                                                        "HOME": str(self.root),
                                                        "XDG_STATE_HOME": str(self.root / "state"),
                                                        "DESIGN_ROLES_STATE": str(self.root / "astra/design-roles"),
                                                        "UNIT_RUN_DIR": str(self.session),
                                                        "BUILDLOG_SCOPE": "0"}))
        config = self.root / "config"
        config.mkdir()
        for name in ("mesh_runs", "send", "postmaster", "codex_daemon", "direct"):
            _ = (config / f"{name}.conf").write_text(
                (Path(__file__).resolve().parents[2] / f"config/{name}.conf").read_text())
        self.stack.enter_context(patch.dict(os.environ, {"CONF_DIR": str(config)}))
        self.sent = []
        _ = self.stack.enter_context(patch.object(treekey, "tree_key", return_value="key-1"))

        def send_to_director(session_id: str, summary: str, text: str) -> Literal["sent"]:
            self.sent.append(DirectorNotice(session_id, summary, text))
            return "sent"

        self.director_sender = mesh_runs.send_to_director
        _ = self.stack.enter_context(patch.object(mesh_runs, "send_to_director", send_to_director))
        fixture = StaffTestCase()
        fixture.setUp()
        _ = self.stack.callback(fixture.doCleanups)
        _, self.unit, _ = fixture.tree()
        self.unit = staff.set_worktree(self.unit.id, staff.PresentWorktree("natedev", self.root), expected=self.unit.worktree)
        self.stack.enter_context(patch.dict(os.environ, {
            "DESIGN_ROLES_STATE": str(self.root / "astra/design-roles"),
        }))
        self.write_roster(THREAD_ONE, "running", os.getpid(), port=4100)
        self.worker_id = self.record_worker(WORKER, THREAD_ONE).id

    @override
    def tearDown(self) -> None:
        self.stack.close()

    def test_current_runtime_selects_the_fixed_staff_member_without_a_roster_thread_copy(self) -> None:
        fixture = StaffTestCase()
        fixture.setUp()
        _ = self.stack.callback(fixture.doCleanups)
        _, unit, _ = fixture.tree()
        worker = fixture_worker("impl2", unit.id,
                                backend=staff.CodexBackend(self.root / "codex-home"))
        current = staff.native_id(fixture_runtime_id("detached-replaced-thread"))
        fixture_session(current, staff.CodexBackend(self.root / "codex-home"))
        worker = staff.update_current_session(worker.current_session, current)
        path = self.session / codex_mesh.ROSTER_FILE
        _ = path.write_text(json.dumps({worker.name: {
            "staff_id": worker.id, "status": "running", "launcher_pid": os.getpid(),
        }}))
        before = fixture.staff_file.read_bytes(), path.read_bytes()

        with patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as read:
            caller = mesh_runs.caller_from_environment({"CODEX_THREAD_ID": current})

        self.assertEqual(read.call_count, 1)
        self.assertIsInstance(caller, mesh_runs.MeshCaller)
        assert isinstance(caller, mesh_runs.MeshCaller)
        self.assertEqual((caller.session_dir, caller.name), (str(self.session), worker.name))
        self.assertEqual((fixture.staff_file.read_bytes(), path.read_bytes()), before)
        self.assertNotIn("thread_id", path.read_text())
        self.assertIsInstance(mesh_runs.caller_from_environment({"CODEX_THREAD_ID": worker.id}),
                              mesh_runs.NotMeshWorker)

    def test_durable_role_is_found_after_its_run_folder_is_gone(self) -> None:
        role_root = self.root / "astra/design-roles"
        mesh = role_root / "design-one/mesh"
        mesh.mkdir(parents=True)
        write_text_atomic(mesh / "worker_transport", "mesh\n")
        worker = self.record_worker("lasting-designer", LASTING_THREAD)
        _ = (mesh / codex_mesh.ROSTER_FILE).write_text(json.dumps({"lasting-designer": {
            "staff_id": worker.id, "status": "idle", "launcher_pid": 0}}))
        with patch.dict(os.environ, {"DESIGN_ROLES_STATE": str(role_root)}):
            caller = mesh_runs.caller_from_environment({"CODEX_THREAD_ID": LASTING_THREAD})
        self.assertEqual(caller, mesh_runs.MeshCaller(str(mesh), "lasting-designer", LASTING_THREAD))
        with patch.dict(os.environ, {"DESIGN_ROLES_STATE": str(role_root), "CODEX_THREAD_ID": LASTING_THREAD}), \
                patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=4321)), \
                contextlib.redirect_stdout(io.StringIO()):
            code = mesh_runs.main(["start", "--label", "role fixture", "--", sys.executable,
                                   "-c", "print('durable result')"])
        self.assertEqual(code, 0)
        records = list((mesh / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        record = self.supervise(records[0])
        self.assertEqual((record["status"], record.get("exit_code")), ("finished", 0))
        pending = cast(object, json.loads(session_staff.worker_inbox_path(str(mesh), "lasting-designer").read_text()))
        self.assertIn("durable result", str(pending))
        self.assertEqual(self.sent, [])

    def test_an_adopted_unit_reviewer_starts_detached_runs_from_its_current_owner(self) -> None:
        worker = self.record_worker("unit-review", ADOPTED_THREAD)
        _ = (self.session / codex_mesh.ROSTER_FILE).write_text(json.dumps({"unit-review": {
            "staff_id": worker.id, "status": "running", "role": "review",
            "launcher_pid": os.getpid(), "unit_ledger": str(self.root / "unit-workers/ledger.json"),
        }}))
        caller = mesh_runs.caller_from_environment({"CODEX_THREAD_ID": ADOPTED_THREAD})
        self.assertEqual(caller, mesh_runs.MeshCaller(str(self.session), "unit-review", ADOPTED_THREAD))

    def test_detached_run_caller_uses_the_current_owner_instead_of_an_earlier_adopted_entry(self) -> None:
        previous = self.session.parent / "a-former-owner"
        previous.mkdir()
        ledger = str(self.root / "unit-workers/ledger.json")
        worker = self.record_worker("current-review", SHARED_THREAD)
        _ = (previous / codex_mesh.ROSTER_FILE).write_text(json.dumps({"old-review": {
            "staff_id": worker.id, "status": "done", "unit_ledger": ledger,
            "launcher_pid": 0,
        }}))
        _ = (self.session / codex_mesh.ROSTER_FILE).write_text(json.dumps({"current-review": {
            "staff_id": worker.id, "status": "running", "unit_ledger": ledger,
            "launcher_pid": os.getpid(),
        }}))

        caller = mesh_runs.caller_from_environment({"CODEX_THREAD_ID": SHARED_THREAD})

        self.assertEqual(caller, mesh_runs.MeshCaller(str(self.session), "current-review", SHARED_THREAD))

    def test_detached_result_stays_pending_for_the_held_roles_next_authorized_turn(self) -> None:
        design_id = "00000000-0000-4000-8000-000000000076"
        worker = self.record_worker(WORKER, THREAD_ONE)
        design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 1), "fixture-repo",
            str(self.root), str(self.root / "design.md"), "design", "2026-10-21T12:00:00+00:00",
            design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "unused", design_roles.RoleNotOpened()),
             design_roles.RoleInstance("reviewer", WORKER, design_roles.RoleOpen(worker.id))))
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        roster = {WORKER: {"staff_id": worker.id, "status": "done", "held_design_id": design_id,
                           "held_role": "reviewer", "launcher_pid": 0}}
        _ = (self.session / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        path = self.record([sys.executable, "-c", "print('saved verification evidence')"])
        with patch.object(codex_mesh, "command_follow", side_effect=AssertionError("result woke the role")), \
                patch.object(codex_mesh, "command_send", side_effect=AssertionError("result sent a turn")), \
                patch.object(design_roles, "dispatch_turn", side_effect=AssertionError("result dispatched a role")):
            record = self.supervise(path)
            repeated = mesh_runs.deliver(record, 0)
        messages = design_roles.pending_messages(design_id, "reviewer")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].source, "mesh_runs:r1")
        self.assertIn("saved verification evidence", messages[0].text)
        self.assertIn("queued for held role", repeated)
        self.assertEqual(self.sent, [])
        self.assertEqual(cast(object, json.loads((self.session / codex_mesh.ROSTER_FILE).read_text())), roster)
        self.assertFalse(session_staff.worker_inbox_path(str(self.session), WORKER).exists())

    def test_repeated_run_results_are_guarded_once_before_any_worker_delivery(self) -> None:
        record = cast(mesh_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        outcomes: list[str] = []
        for index in range(4):
            with self.subTest(copy=index + 1):
                outcomes.append(mesh_runs.deliver(record, 0))
        self.assertTrue(all("held for" in outcome for outcome in outcomes[:2]))
        self.assertTrue(all("held repeated run result" in outcome for outcome in outcomes[2:]))
        self.assertEqual(len(cast(list[str], self.pending()["messages"])), 2)
        self.assertEqual(self.sent, [])

    def test_run_and_direct_delivery_count_the_same_thread_and_do_not_count_director_fallback_as_worker(self) -> None:
        record = cast(mesh_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        text = mesh_runs.result_text(record, 0)
        self.assertIn("held for", mesh_runs.deliver(record, 0))
        outcome = codex_mesh.hold_message(str(self.session), WORKER, text, without_launcher=True, sender="mesh-runs")
        self.assertIsInstance(outcome, codex_mesh.HeldForLauncher)
        self.assertIn("held repeated run result", mesh_runs.deliver(record, 0))
        self.write_roster(THREAD_ONE, "done", DEAD_PID, port=4100)
        active = self.root / "unit/active"
        active.mkdir()
        _ = (active / "director-session").write_text(str(self.session))
        self.assertIn("sent to director", mesh_runs.deliver(record, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].session_id, self.unit.current_session)

    def record_worker(self, name: str, thread_id: str) -> staff.StaffRow:
        fixture_session(thread_id, staff.CodexBackend(self.root / "codex-home"))
        rows = [row for row in staff.find(kind="worker", parent=self.unit.id) if row.name == name]
        if rows:
            return staff.update_current_session(rows[0].current_session, staff.native_id(thread_id))
        return staff.ensure_worker(staff.native_id(thread_id), name, self.unit.id)

    def write_roster(self, thread_id: str, status: str, launcher_pid: int, port: int) -> None:
        worker = self.record_worker(WORKER, thread_id)
        roster = {WORKER: {"staff_id": worker.id, "status": status, "launcher_pid": launcher_pid,
                           "port": port, "turn_id": "turn-1"}}
        _ = (self.session / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster), encoding="utf-8")

    def pending(self) -> dict[str, object]:
        path = session_staff.worker_inbox_path(str(self.session), WORKER)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return cast(dict[str, object], json.loads(text)) if text.strip() else {}

    def record(self, command: list[str], **extra: str) -> Path:
        runs = self.session / codex_mesh.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        record: mesh_runs.RunRecord = {
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
        command line names mesh_runs.py supervise and the record, unless pid names another process."""
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
                 "mesh_runs.py", "supervise", str(path)],
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

    def start(self, command: list[str], to: str = WORKER) -> tuple[MagicMock, str]:
        """Start command for seat `to`; the Popen mock and what start printed."""
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        with patch.object(subprocess, "Popen", popen), contextlib.redirect_stdout(io.StringIO()) as output:
            code = mesh_runs.main(["start", "--session-dir", str(self.session), "--to", to, "--", *command])
        self.assertEqual(code, 0)
        return popen, output.getvalue()

    def supervise(self, path: Path) -> mesh_runs.RunRecord:
        self.assertEqual(mesh_runs.main(["supervise", str(path)]), 0)
        return cast(mesh_runs.RunRecord, json.loads(path.read_text(encoding="utf-8")))

    # Criterion 1: start returns at once with a detached supervisor and a running record.
    def test_retired_worker_finishes_its_orphan_without_blocking_other_runs(self) -> None:
        path = self.record(["true"])
        self.update_record(path, pid=DEAD_PID, started_at=time.time() - 600)
        _ = staff.retire(self.worker_id)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(mesh_runs.reap_orphans(str(self.session)), ["r1"])
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["status"], "finished")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertEqual(mesh_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(self.sent, [])

    def test_start_detaches_a_supervisor_and_records_the_run(self) -> None:
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        environment = {"CODEX_THREAD_ID": THREAD_ONE, "CLAUDE_CODE_SESSION_ID": "director-session",
                       "UNIT_TEAM_ROLE": "test"}
        with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = mesh_runs.main(["start", "--label", "verify.sh check", "--", "bash", "verify.sh", "check"])
        self.assertEqual(code, 0)
        kwargs = cast(dict[str, object], popen.call_args.kwargs)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertNotIn(kwargs["stdout"], (None, sys.stdout))
        records = list((self.session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        record = cast(dict[str, object], json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual((record["status"], record["pid"], record["worker"], record["staff_id"]),
                         ("running", 4321, WORKER, self.worker_id))
        self.assertNotIn("thread_id", record)
        self.assertEqual(record["command"], ["bash", "verify.sh", "check"])
        self.assertNotIn("port", record)
        self.assertIn("next message", output.getvalue())

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
        records = list((self.session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
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
            code = mesh_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
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
        config = self.root / "config/mesh_runs.conf"
        _ = config.write_text("# whole: 1..\nLIVE_RUN_LIMIT=1\n")
        with patch.object(subprocess, "Popen") as popen, contextlib.redirect_stderr(io.StringIO()) as err:
            code = mesh_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
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
        roster_path = self.session / codex_mesh.ROSTER_FILE
        entries = cast(dict[str, object], json.loads(roster_path.read_text()))
        entries[reviewer.name] = {"staff_id": reviewer.id, "status": "running", "launcher_pid": os.getpid()}
        _ = roster_path.write_text(json.dumps(entries))
        popen, _ = self.start(["new-check"], to=reviewer.name)
        popen.assert_called_once()
        self.assertEqual(len(list(paths[0].parent.glob("*.json"))), 4)

    def test_parallel_starts_cannot_exceed_one_configured_place(self) -> None:
        _ = (self.root / "config/mesh_runs.conf").write_text("# whole: 1..\nLIVE_RUN_LIMIT=1\n")
        runs = self.session / codex_mesh.DETACHED_RUNS_DIR
        runs.mkdir()
        script = """
import sys
from types import SimpleNamespace
from unittest.mock import patch
from scripts.agents import mesh_run_process, mesh_runs
print('ready', flush=True)
input()
with (patch.object(mesh_run_process, 'supervises', return_value=True),
      patch.object(mesh_runs.treekey, 'tree_key', return_value='fixture-tree'),
      patch.object(mesh_runs.subprocess, 'Popen', return_value=SimpleNamespace(pid=4321))):
    code = mesh_runs.main(['start', '--session-dir', sys.argv[1], '--to', sys.argv[2],
                           '--', 'check', sys.argv[3]])
sys.exit(code)
"""
        guard = LauncherHangGuard("parallel detached starts")
        with contextlib.ExitStack() as children, (runs / ".start.lock").open("a") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            processes = [children.enter_context(subprocess.Popen(
                [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", script, str(self.session), WORKER, str(index)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                for index in range(4)]
            for child in processes:
                assert child.stdin is not None and child.stdout is not None
                self.assertTrue(select.select([child.stdout], [], [], guard.remaining())[0])
                self.assertEqual(child.stdout.readline(), "ready\n")
                _ = child.stdin.write("start\n")
                child.stdin.flush()
            fcntl.flock(admission, fcntl.LOCK_UN)
            for child in processes:
                _ = child.communicate(timeout=guard.remaining())
        self.assertCountEqual([child.returncode for child in processes], [0, 4, 4, 4])
        self.assertEqual(len(list(runs.glob("*.json"))), 1)

    def test_start_and_reap_preserve_an_unpublished_record_under_the_start_lock(self) -> None:
        _ = (self.root / "config/mesh_runs.conf").write_text("# whole: 1..\nLIVE_RUN_LIMIT=1\n")
        script = """
import fcntl
import sys
from types import SimpleNamespace
from unittest.mock import patch
from scripts.agents import mesh_runs
flock = fcntl.flock
def lock(fd, operation):
    print('locking', flush=True)
    return flock(fd, operation)
with (patch.object(fcntl, 'flock', lock),
      patch.object(mesh_runs, 'deliver', return_value='fixture'),
      patch.object(mesh_runs.treekey, 'tree_key', return_value='key-1'),
      patch.object(mesh_runs.subprocess, 'Popen', return_value=SimpleNamespace(pid=4321))):
    args = [sys.argv[1], '--session-dir', sys.argv[2]]
    if sys.argv[1] == 'start':
        args += ['--to', sys.argv[3], '--', 'new-check']
    code = mesh_runs.main(args)
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
        _ = (self.root / "config/mesh_runs.conf").write_text("# whole: 1..\nLIVE_RUN_LIMIT=1\n")
        script = """
import os
import sys
from unittest.mock import patch
from scripts.agents import mesh_runs
write_record = mesh_runs._write_record
def publish(path, record):
    if record['pid']:
        os._exit(99)
    write_record(path, record)
with (patch.object(mesh_runs, '_write_record', publish),
      patch.object(mesh_runs.treekey, 'tree_key', return_value='key-1')):
    mesh_runs.main(sys.argv[1:])
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
                paths = list((self.session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
                self.assertEqual(len(paths), 1)
                record = cast(mesh_runs.RunRecord, json.loads(paths[0].read_text()))
                self.assertGreater(record["pid"], 0)
                self.assertTrue(mesh_run_process.supervises(record["pid"], paths[0]))
                supervisor = os.pidfd_open(record["pid"])
                try:
                    with patch.object(subprocess, "Popen") as popen, \
                            contextlib.redirect_stderr(io.StringIO()) as errors:
                        code = mesh_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
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
        self.assertEqual(len(list((self.session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))), 1)

    def test_a_start_after_the_files_changed_or_with_no_tree_key_starts_a_new_run(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        _ = self.live_run(command)
        for key in ("key-2", None):
            with patch.object(treekey, "tree_key", return_value=key):
                popen, _ = self.start(command)
            popen.assert_called_once()
        records = [cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
                   for path in (self.session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json")]
        self.assertEqual(sorted(str(record.get("tree_key")) for record in records), ["", "key-1", "key-2"])

    def test_another_command_seat_or_folder_starts_a_new_run(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        _ = self.live_run(command)
        reviewer = self.record_worker("unit-review", fixture_runtime_id("mesh-run-other-reviewer"))
        roster_path = self.session / codex_mesh.ROSTER_FILE
        entries = cast(dict[str, object], json.loads(roster_path.read_text()))
        entries[reviewer.name] = {"staff_id": reviewer.id, "status": "running", "launcher_pid": os.getpid()}
        _ = roster_path.write_text(json.dumps(entries))
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
        record = cast(mesh_runs.RunRecord, json.loads(path.read_text(encoding="utf-8")))
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
        self.assertEqual(mesh_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(len(cast(list[str], self.pending()["messages"])), 1)

    def test_a_dead_run_that_a_newer_run_of_its_command_answers_delivers_nothing(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        older = self.record(command)
        self.update_record(older, pid=DEAD_PID, started_at=time.time() - 600)
        newer = older.with_name("r2.json")
        _ = newer.write_text(older.read_text(encoding="utf-8"), encoding="utf-8")
        self.update_record(newer, run_id="r2", started_at=time.time(), pid=DEAD_PID)
        self.assertEqual(mesh_runs.reap_orphans(str(self.session), WORKER), ["r1", "r2"])
        older_record = cast(mesh_runs.RunRecord, json.loads(older.read_text(encoding="utf-8")))
        self.assertIn("a newer run of the same command answers", older_record.get("delivery", ""))
        messages = cast(list[str], self.pending()["messages"])
        self.assertEqual(len(messages), 1, "only the newest dead run reports")

    def test_the_launcher_gets_a_dead_runs_failure_before_it_closes_its_inbox(self) -> None:
        session, thread = str(self.session), THREAD_ONE
        path = self.record(["bash", "verify.sh", "check"])
        self.update_record(path, pid=DEAD_PID)
        codex_mesh._end_dead_runs(session, "unit-other")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.pending(), {}, "another worker's launcher ends nothing of this one")
        codex_mesh._end_dead_runs(session, WORKER)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("FAIL (no exit status)", cast(list[str], self.pending()["messages"])[0])
        self.assertFalse(codex_mesh._close_messages_if_empty(  # pyright: ignore[reportPrivateUsage]
            session, WORKER, codex_mesh.RosterActiveLauncher(thread, (os.getpid(),))))

    def test_a_supervisor_for_a_different_record_is_not_trusted(self) -> None:
        path = self.live_run(["echo", "x"])
        original = cast(mesh_runs.RunRecord, json.loads(path.read_text()))
        self.assertTrue(mesh_run_process.supervises(original["pid"], path))
        another = path.with_name("another.json")
        _ = another.write_text(path.read_text())
        self.assertFalse(mesh_run_process.supervises(original["pid"], another))
        self.assertFalse(mesh_run_process.supervises(os.getpid(), path))

    def test_a_live_supervisor_is_not_ended(self) -> None:
        path = self.live_run(["bash", "verify.sh", "check"])
        self.assertEqual(mesh_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "running")

    def test_start_exits_3_for_a_caller_that_is_not_a_mesh_worker(self) -> None:
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        for environment in ({"CODEX_THREAD_ID": "thread-unknown"}, {"CODEX_THREAD_ID": ""}):
            with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen):
                self.assertEqual(mesh_runs.main(["start", "--", "true"]), mesh_runs.NOT_A_MESH_WORKER)
        self.write_roster(THREAD_ONE, "ended", 0, port=4100)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_ONE}), patch.object(subprocess, "Popen", popen):
            self.assertEqual(mesh_runs.main(["start", "--", "true"]), mesh_runs.NOT_A_MESH_WORKER)
        popen.assert_not_called()

    # Criterion 2: the result is held for the seat, then the record is marked finished.
    def test_supervise_delivers_before_marking_finished(self) -> None:
        path = self.record([sys.executable, "-c", "import os; print('lint clean', os.environ['UNIT_TEAM_ROLE'])"])
        hold = codex_mesh.hold_message
        statuses: list[object] = []

        def watched(session_dir: str, name: str, text: str, *, without_launcher: bool, sender: str) -> codex_mesh.HoldOutcome:
            stored = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            statuses.append(stored["status"])
            return hold(session_dir, name, text, without_launcher=without_launcher, sender=sender)

        with patch.object(codex_mesh, "hold_message", watched):
            record = self.supervise(path)
        self.assertEqual(statuses, ["running"])
        self.assertEqual((record["status"], record.get("exit_code")), ("finished", 0))
        messages = cast(list[str], self.pending()["messages"])
        self.assertEqual(len(messages), 1)
        self.assertIn("Run finished: verify.sh check\nResult: PASS (exit 0)", messages[0])
        self.assertIn("lint clean impl", messages[0])
        self.assertEqual(self.sent, [])

    def test_scope_prefix_needs_a_user_manager_and_honours_the_off_switch(self) -> None:
        runtime = Path(self.stack.enter_context(short_socket_directory())) / "runtime"
        (runtime / "systemd").mkdir(parents=True)
        marker = self.root / "r1.scope"
        on = {"XDG_RUNTIME_DIR": str(runtime)}
        with patch("shutil.which", return_value="/bin/systemd-run"):
            self.assertEqual(mesh_runs.scope_prefix(marker, on), [])
            with socket.socket(socket.AF_UNIX) as manager:
                manager.bind(str(runtime / "systemd" / "private"))
                prefix = mesh_runs.scope_prefix(marker, on)
                self.assertEqual(mesh_runs.scope_prefix(marker, {**on, "BUILDLOG_SCOPE": "0"}), [])
        self.assertEqual(prefix[:2], ["systemd-run", "--user"])
        self.assertIn("--slice=builds.slice", prefix)
        self.assertEqual(prefix[-4:], ["/bin/sh", "-c", mesh_runs.SCOPE_SCRIPT, str(marker)])
        with patch("shutil.which", return_value=None):
            self.assertEqual(mesh_runs.scope_prefix(marker, on), [])

    def test_a_scope_runs_the_command_once_and_a_failed_scope_runs_it_plainly(self) -> None:
        count = self.root / "count"
        command = ["/bin/sh", "-c", f'echo run >> "{count}"']
        scope = self.session / codex_mesh.DETACHED_RUNS_DIR / "r1.scope"
        # A scope without systemd-run: the same script, so the marker proves the command started.
        with patch.object(mesh_runs, "scope_prefix", return_value=["/bin/sh", "-c", mesh_runs.SCOPE_SCRIPT, str(scope)]):
            record = self.supervise(self.record(command))
        self.assertEqual((record.get("exit_code"), count.read_text(encoding="utf-8")), (0, "run\n"))
        self.assertFalse(scope.exists())
        count.unlink()
        (session_staff.worker_inbox_path(str(self.session), WORKER)).unlink()
        # A scope that fails before the command starts leaves no marker.
        with patch.object(mesh_runs, "scope_prefix", return_value=["/bin/sh", "-c", "exit 1", "scope"]):
            record = self.supervise(self.record(command))
        self.assertEqual((record.get("exit_code"), count.read_text(encoding="utf-8")), (0, "run\n"))

    def test_failure_and_done_message_rules(self) -> None:
        path = self.record([sys.executable, "-c", "raise SystemExit(4)"])
        record = self.supervise(path)
        self.assertIn("Result: FAIL (exit 4)", cast(list[str], self.pending()["messages"])[0])
        self.assertEqual(record.get("exit_code"), 4)
        done = self.root / "done.txt"
        _ = done.write_text("Stage 2: go", encoding="utf-8")
        (session_staff.worker_inbox_path(str(self.session), WORKER)).unlink()
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(1)"], done=str(done)))
        self.assertEqual(self.pending(), {})
        self.assertIn("nothing delivered", record.get("delivery", ""))
        _ = self.supervise(self.record([sys.executable, "-c", "pass"], done=str(done)))
        self.assertEqual(self.pending()["messages"], ["Stage 2: go"])

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
        limits = conf.read("mesh_runs")
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
        record = cast(mesh_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        record["label"] = "verification " * 1_000
        _ = Path(record["log"]).write_text("line " * 1_000 + "\n" + ("x" * 5_000 + "\n") * 20 + "last line\n")

        text = mesh_runs.result_text(record, 1)

        self.assertLessEqual(len(text), 2_000)
        self.assertIn("Result: FAIL (exit 1)", text)
        self.assertIn(f"Log: {record['log']}", text)
        self.assertTrue(text.endswith("\nlast line"))

        config = self.root / "config/mesh_runs.conf"
        _ = config.write_text(config.read_text().replace("RESULT_LINE_CHARS=300", "RESULT_LINE_CHARS=100")
                              .replace("RESULT_TEXT_CHARS=2000", "RESULT_TEXT_CHARS=1200"))
        text = mesh_runs.result_text(record, 1)
        self.assertLessEqual(len(text), 1_200)
        self.assertIn(f"{'x' * 100} [cut: {5_000 - 100} more characters]", text)
        self.assertIn("Result: FAIL (exit 1)", text)
        self.assertIn(f"Log: {record['log']}", text)
        self.assertTrue(text.endswith("\nlast line"))

    def test_result_text_collapses_consecutive_memory_wait_lines_before_taking_the_tail(self) -> None:
        record = cast(mesh_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        lines = ["first diagnostic", *(f"still waiting for memory: {n}" for n in range(30)),
                 "second diagnostic", "still waiting for memory: retry", "still waiting for memory: ready", "last line"]
        _ = Path(record["log"]).write_text("\n".join(lines))

        text = mesh_runs.result_text(record, 0)

        self.assertIn("first diagnostic", text)
        self.assertIn("second diagnostic", text)
        self.assertEqual([line for line in text.splitlines() if line.startswith("still waiting for memory")],
                         ["still waiting for memory: 29", "still waiting for memory: ready"])
        self.assertTrue(text.endswith("\nlast line"))

    def test_queued_director_notice_records_the_reason_without_a_second_worker_copy(self) -> None:
        self.write_roster(THREAD_ONE, "done", DEAD_PID, port=4100)
        outcome = subprocess.CompletedProcess(["send.py"], 1, stdout="QUEUED: recipient socket is closed\n", stderr="")
        with patch.object(mesh_runs, "send_to_director", self.director_sender), \
                patch.object(subprocess, "run", return_value=outcome) as send, \
                patch.object(codex_mesh, "hold_message", wraps=codex_mesh.hold_message) as hold:
            record = self.supervise(self.record([sys.executable, "-c", "pass"]))

        self.assertEqual(record.get("delivery"), f"queued for session {self.unit.current_session}: recipient socket is closed")
        send.assert_called_once()
        hold.assert_called_once_with(str(self.session), WORKER, ANY, without_launcher=False, sender="mesh-runs")
        self.assertEqual(self.pending(), {})

    # Criterion 3: the server and thread change between start and finish.
    def test_result_reaches_the_seats_current_thread_after_a_server_change(self) -> None:
        path = self.record([sys.executable, "-c", "pass"])
        self.write_roster(THREAD_TWO, "running", os.getpid(), port=4200)
        _ = (self.session / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4200, "pid": os.getpid()}))
        _ = self.supervise(path)
        self.assertEqual(session_staff.worker_inbox_path(str(self.session), WORKER).name.removesuffix(".pending.json"), self.worker_id)
        self.assertEqual(len(cast(list[str], self.pending()["messages"])), 1)

    # Criterion 4: no launcher sends the result to the director; an attached launcher holds it.
    def test_no_launcher_sends_the_result_to_the_director_and_holds_nothing(self) -> None:
        active = self.root / "unit" / "active"
        active.mkdir()
        _ = (active / "director-session").write_text(f"{self.session}\n", encoding="utf-8")
        self.write_roster(THREAD_ONE, "done", DEAD_PID, port=4100)
        path = self.record([sys.executable, "-c", "raise SystemExit(2)"])
        with patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.root / "usage")}):
            agents_log.append(THREAD_ONE, "test", None, datetime.now(timezone.utc))
            record = self.supervise(path)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].session_id, self.unit.current_session)
        for part in (f"Worker {WORKER}'s run", f"Worker: {WORKER}",
                     f"Dispatch the worker (assign.sh --role test --to {WORKER})",
                     "Command: ", "FAIL (exit 2)", "Log: "):
            self.assertIn(part, self.sent[0].text)
        self.assertEqual(self.pending(), {})
        self.assertIn("sent to director", record.get("delivery", ""))

    def test_director_hint_uses_the_current_role_after_the_worker_changes_roles(self) -> None:
        self.write_roster(THREAD_ONE, "done", DEAD_PID, port=4100)
        with patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.root / "usage")}):
            agents_log.append(THREAD_ONE, "impl", None, datetime.now(timezone.utc))
            with (patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=4321)),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(mesh_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
                                                "--", sys.executable, "-c", "pass"]), 0)
            path = next((self.session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
            agents_log.append(THREAD_ONE, "review", "contract", datetime.now(timezone.utc))
            record = self.supervise(path)
        self.assertNotIn("role", record)
        self.assertIn(f"Dispatch the worker (assign.sh --role review --to {WORKER})", self.sent[0].text)

    def test_a_run_started_for_the_seats_role_reaches_the_seat(self) -> None:
        # tool-based-ui-trunk-box started its runs with `--to box` (2026-10-20): each was lost.
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        with patch.object(subprocess, "Popen", popen), contextlib.redirect_stdout(io.StringIO()):
            code = mesh_runs.main(["start", "--session-dir", str(self.session), "--to", "impl", "--", "true"])
        self.assertEqual(code, 0)
        path = next((self.session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))["worker"], WORKER)
        record = self.supervise(path)
        self.assertIn(f"held for {WORKER}'s attached launcher", record.get("delivery", ""))
        self.assertEqual(len(cast(list[str], self.pending()["messages"])), 1)
        # A name the roster does not hold yet stays as given.
        self.assertEqual(mesh_runs.seat_name(str(self.session), "reviewer-1"), "reviewer-1")

    def test_a_director_whose_marker_names_another_run_still_hears_the_result(self) -> None:
        active = self.root / "unit" / "active"
        active.mkdir()
        other = self.root / "unit" / "run-2"
        other.mkdir()
        _ = (active / "director-session").write_text(f"{other}\n", encoding="utf-8")
        _ = (self.session / "director").write_text("director-session\n", encoding="utf-8")
        self.write_roster(THREAD_ONE, "done", DEAD_PID, port=4100)
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(100)"]))
        self.assertEqual([notice.session_id for notice in self.sent], [self.unit.current_session])
        self.assertEqual(record.get("delivery"), f"sent to director {self.unit.current_session}")

    def test_closed_inbox_goes_to_the_director(self) -> None:
        active = self.root / "unit" / "active"
        active.mkdir()
        _ = (active / "director-session").write_text(f"{self.session}\n", encoding="utf-8")
        with codex_mesh._pending_file(str(self.session), WORKER) as handle:  # pyright: ignore[reportPrivateUsage]
            codex_mesh._write_pending(handle, [], accepting_messages=False)  # pyright: ignore[reportPrivateUsage]
        _ = self.supervise(self.record([sys.executable, "-c", "pass"]))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.pending()["messages"], [])

    def test_live_run_keeps_the_launcher_attached_until_the_result_is_held(self) -> None:
        session, thread = str(self.session), THREAD_ONE
        path = self.record([sys.executable, "-c", "pass"])
        self.assertFalse(codex_mesh._close_messages_if_empty(  # pyright: ignore[reportPrivateUsage]
            session, WORKER, codex_mesh.RosterActiveLauncher(thread, (os.getpid(),))))
        self.assertTrue(codex_mesh._waiting_on_detached_run(session, WORKER))  # pyright: ignore[reportPrivateUsage]
        _ = self.supervise(path)
        self.assertFalse(codex_mesh._detached_run_live(session, WORKER))  # pyright: ignore[reportPrivateUsage]
        # The held result now opens the next turn: the launcher refuses to close on it.
        self.assertFalse(codex_mesh._close_messages_if_empty(  # pyright: ignore[reportPrivateUsage]
            session, WORKER, codex_mesh.RosterActiveLauncher(thread, (os.getpid(),))))
        self.assertFalse(codex_mesh._waiting_on_detached_run(session, WORKER))  # pyright: ignore[reportPrivateUsage]
        session_staff.worker_inbox_path(session, WORKER).unlink()
        self.assertTrue(codex_mesh._close_messages_if_empty(  # pyright: ignore[reportPrivateUsage]
            session, WORKER, codex_mesh.RosterActiveLauncher(thread, (os.getpid(),))))

    def test_a_record_without_a_pid_counts_as_live_only_at_first(self) -> None:
        path = self.record(["true"])
        record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        record["pid"] = 0
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        self.assertTrue(codex_mesh._detached_run_live(str(self.session), WORKER))  # pyright: ignore[reportPrivateUsage]
        record["started_at"] = time.time() - 120
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        self.assertFalse(codex_mesh._detached_run_live(str(self.session), WORKER))  # pyright: ignore[reportPrivateUsage]

    def test_an_interrupted_turn_with_held_work_is_not_a_failure(self) -> None:
        session = str(self.session)
        params: dict[str, object] = {"turn": {"id": "turn-1", "status": "interrupted"}}
        check = codex_mesh._interrupted_for_held_work  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(check("turn/completed", params, session, WORKER))
        _ = codex_mesh.hold_message(session, WORKER, "next", without_launcher=True)
        self.assertTrue(check("turn/completed", params, session, WORKER))
        failed: dict[str, object] = {"turn": {"id": "turn-1", "status": "interrupted", "error": {"message": "boom"}}}
        self.assertFalse(check("turn/completed", failed, session, WORKER))

    def test_interrupt_holds_the_message_then_interrupts_the_turn(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class FakeClient:
            def __init__(self, _port: int, _name: str) -> None:
                pass

            def call(self, method: str, params: dict[str, object], timeout: float = 0.0) -> codex_mesh.RpcMessage:
                del timeout
                calls.append((method, params))
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "active"}, "turns": []}}}
                if method == "thread/turns/list":
                    return {"result": {"data": [{"id": "turn-9", "status": "inProgress"}]}}
                return {"result": {}}

            def close(self) -> None:
                pass

        with patch.object(codex_mesh, "Client", FakeClient), patch.object(codex_mesh, "_worker_port", return_value=1), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = codex_mesh.main(["interrupt", "--session-dir", str(self.session), "--to", WORKER,
                                    "--message", "Stop; the plan changed."])
        self.assertEqual(code, 0)
        self.assertEqual(self.pending()["messages"], ["Stop; the plan changed."])
        self.assertIn(("turn/interrupt", {"threadId": THREAD_ONE, "turnId": "turn-9"}), calls)
        self.assertIn("interrupted", output.getvalue())


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
        for name in ("mesh_runs", "send", "postmaster", "codex_daemon"):
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
        write_text_atomic(self.run_dir / "worker_transport", "herdr\n")
        write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps({"owner_staff_id": self.fixture.parent.id}))
        marker = unit_runs.marker(self.fixture.parent.current_session)
        marker.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(marker, str(self.run_dir) + "\n")
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

    @property
    def run_dir(self) -> Path:
        return self.fixture.root / "run"

    def record(self, row: staff.StaffRow, run_id: str = "pane-result") -> mesh_runs.RunRecord:
        runs = self.run_dir / codex_mesh.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        log = runs / f"{run_id}.log"
        write_text_atomic(log, "private verification output\n")
        record: mesh_runs.RunRecord = {"run_id": run_id, "status": "running", "session_dir": str(self.run_dir),
                "worker": row.name, "staff_id": row.id, "label": "fixture gate", "command": ["true"],
                "log": str(log), "started_at": time.time(), "pid": 0,
                "director_dir": str(self.run_dir), "done_message_file": ""}
        write_text_atomic(log.with_suffix(".json"), json.dumps(record))
        return record

    def assert_holder_result(self, record: mesh_runs.RunRecord, reason: str) -> None:
        result = mesh_runs.deliver(record, 1)
        self.assertIn("sent", result)
        self.assertEqual(self.inputs, [])
        self.assertEqual(len(self.notices), 1)
        target, text = self.notices[0]
        self.assertEqual(target, f"session:{self.fixture.parent.current_session}")
        self.assertIn(reason, text)
        self.assertIn(record["run_id"], text)
        self.assertIn("private verification output", text)

    def test_detached_callers_use_current_native_sessions_without_a_mesh_roster(self) -> None:
        families: tuple[tuple[Literal["claude", "codex"], str], ...] = (
            ("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"))
        for family, variable in families:
            with self.subTest(family=family):
                row = self.fixture.worker("worker-" + family, family=family)
                with patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}):
                    caller = mesh_runs.caller_from_environment({variable: row.current_session, "UNIT_RUN_DIR": str(self.run_dir)})
                self.assertIsInstance(caller, mesh_runs.PaneCaller)
                assert isinstance(caller, mesh_runs.PaneCaller)
                self.assertEqual((caller.session_dir, caller.name, caller.staff_id), (str(self.run_dir), row.name, row.id))
                with patch.dict(os.environ, {"CODEX_THREAD_ID": row.id}):
                    self.assertIsInstance(mesh_runs.caller_from_environment({variable: row.id}), mesh_runs.NotMeshWorker)

    def test_run_started_for_a_pane_slot_stays_live_and_delivers_to_its_worker(self) -> None:
        _ = self.fixture.worker_run("a-first", "unit", self.fixture.parent.current_session,
                                transport="herdr", director_run=True)
        self.fixture.world.add(self.fixture.worker("test", family="codex"), "w1:p3", family="codex")
        cases = (("detached-command-owner", "w1:p1", False), ("test2", "w1:p2", False),
                 ("implicit-command-owner", "w1:p4", True))
        for name, pane, implicit in cases:
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
                runs = self.run_dir / codex_mesh.DETACHED_RUNS_DIR
                before = set(runs.glob("*.json"))
                input_count = len(self.inputs)
                output = io.StringIO()
                route = [] if implicit else ["--session-dir", str(self.run_dir), "--to", "test"]
                with (patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}),
                      patch.object(treekey, "tree_key", return_value="private-tree"),
                      patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)),
                      contextlib.redirect_stdout(output)):
                    code = mesh_runs.main(["start", *route, "--", "true"])
                self.assertEqual(code, 0, output.getvalue())
                path, = set(runs.glob("*.json")) - before
                record = cast(mesh_runs.RunRecord, json.loads(path.read_text()))
                self.assertEqual((record["worker"], record["staff_id"]), (row.name, row.id))
                record["pid"] = private_supervisor(self, path)
                write_text_atomic(path, json.dumps(record))
                self.assertEqual(mesh_runs.live_runs(runs, row.name), [record])
                self.assertEqual(mesh_runs.worker_runs(runs, row.name), [record])
                self.assertEqual(mesh_runs.live_runs(runs, "test"), [])
                write_text_atomic(Path(record["log"]), "Private command completed.\n")
                self.assertIn("sent", mesh_runs.deliver(record, 0))
                self.assertEqual(len(self.inputs), input_count + 1)
                self.assertEqual(self.inputs[-1][0], pane)
                self.assertIn(f"Detached run: {record['run_id']}", self.inputs[-1][1])
                self.assertEqual(self.notices, [])
                active = progress_history.active_dispatch(row.id, self.run_dir)
                self.assertIsInstance(active, progress_history.ActiveDispatch)
                assert isinstance(active, progress_history.ActiveDispatch)
                self.assertEqual(active.instance_id, dispatch_id)

    def test_native_caller_ignores_inherited_session_and_run(self) -> None:
        row = self.fixture.worker(family="claude")
        sibling = self.fixture.worker("sibling")
        registry = self.fixture.session_record(row.current_session, row.name)
        os.rename(registry, registry.with_name(f"{os.getpid()}.json"))
        stale_run = self.fixture.root / "stale-run"
        stale_run.mkdir()
        write_text_atomic(stale_run / "worker_transport", "herdr\n")
        for inherited in ({"CLAUDE_CODE_SESSION_ID": sibling.current_session,
                           "UNIT_RUN_DIR": str(stale_run)}, {}):
            with self.subTest(inherited=bool(inherited)), patch.dict(os.environ, {
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": inherited.get("CLAUDE_CODE_SESSION_ID", ""),
                    "UNIT_RUN_DIR": str(stale_run)}):
                caller = mesh_runs.caller_from_environment(inherited)
                self.assertIsInstance(caller, mesh_runs.PaneCaller)
                assert isinstance(caller, mesh_runs.PaneCaller)
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
            self.assertEqual(mesh_runs.main(["supervise", str(path)]), 1)
            pending = cast(mesh_runs.RunRecord, json.loads(path.read_text()))
            self.assertEqual(pending["status"], "running")
            self.assertEqual(mesh_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(mesh_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
        finished = cast(mesh_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(len(self.notices), 1)
        self.assertIn("FAIL (exit 9)", self.notices[0][1])
        self.assertEqual(mesh_runs.reap_orphans(str(self.run_dir)), [])

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
            futures = (pool.submit(mesh_runs.deliver, record, 0), pool.submit(mesh_runs.deliver, record, 0))
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

        with contextlib.suppress(mesh_runs.RunDeliveryFailed):
            _ = mesh_runs.deliver(record, 0)
        recovered = mesh_runs.reap_orphans(str(self.run_dir))

        self.assertEqual(len(self.inputs), 2)
        self.assertEqual(recovered, [record["run_id"]])
        finished = cast(mesh_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(len(self.pending_transcripts), 2)

    def test_result_queued_during_a_turn_finishes_without_a_transcript_receipt(self) -> None:
        row = self.fixture.worker(family="claude")
        self.fixture.world.add(row, "w1:p1")
        self.fixture.world.locations["w1:p1"]["agent_status"] = "working"
        record = self.record(row)
        path = Path(record["log"]).with_suffix(".json")
        self.defer_transcript = True

        self.assertEqual(mesh_runs.main(["supervise", str(path)]), 0)
        finished = cast(mesh_runs.RunRecord, json.loads(path.read_text()))
        result = mesh_runs.deliver(finished, 0)

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
                saved = cast(mesh_runs.RunRecord, json.loads(text))
                if saved["status"] == "finished":
                    raise SystemExit("interrupted before the finished record")
            write_text_atomic(target, text)

        with patch("scripts.agents.mesh_runs.write_text_atomic", interrupt_finish):
            with self.assertRaisesRegex(SystemExit, "interrupted before the finished record"):
                _ = mesh_runs.main(["supervise", str(path)])

        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(mesh_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
        self.assertEqual(len(self.inputs), 2)
        self.assertEqual(self.inputs[0], self.inputs[1])
        finished = cast(mesh_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(mesh_runs.reap_orphans(str(self.run_dir)), [])
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
                patch.object(mesh_runs, "subprocess", SimpleNamespace(
                    Popen=start_supervisor, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT)):
            _ = os.environ.pop("UNIT_RUN_DIR", None)
            _ = os.environ.pop("PLAN_DELEGATE_BOARD_DIR", None)
            self.assertEqual(mesh_runs.main(["start", "--label", "private result", "--",
                                             sys.executable, "-c", "print('result after launcher exit')"]), 0)
        paths = list((self.run_dir / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(paths), 1)
        record = cast(mesh_runs.RunRecord, json.loads(paths[0].read_text()))
        self.assertEqual(record["staff_id"], row.id)
        self.assertEqual(record["session_dir"], str(self.run_dir))
        self.assertIn("supervise", str(supervisors))

        self.assertEqual(mesh_runs.main(["supervise", str(paths[0])]), 0)

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

        result = mesh_runs.deliver(record, 0)

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
            _ = mesh_runs.deliver(record, 0)
        self.interrupt_after_input = False

        result = mesh_runs.deliver(record, 0)

        self.assertIn("already delivered", result)
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.notices, [])

    def test_concurrent_deliveries_type_one_result(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        record = self.record(row)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(mesh_runs.deliver, record, code) for code in (0, 0)]
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
            _ = mesh_runs.deliver(record, 0)
            _ = mesh_runs.deliver(record, 0)
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
