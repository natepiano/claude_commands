"""Tests for mesh_runs.py: detached runs that message the worker who started them."""

from __future__ import annotations

import contextlib
import io
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, cast, override
from unittest.mock import MagicMock, patch

from scripts.agents import codex_mesh, mesh_runs
from scripts.buildlog import treekey
from scripts.guilds import roles
from scripts.lib.socket_test_support import short_socket_directory

WORKER = "unit-impl"
DEAD_PID = 2 ** 22 + 12345


class DirectorNotice(NamedTuple):
    session_id: str
    summary: str
    text: str


class MeshRunsTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    stack: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]
    sent: list[DirectorNotice]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.stack = contextlib.ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.session = self.root / "unit" / "run-1"
        self.session.mkdir(parents=True)
        _ = self.stack.enter_context(patch.object(mesh_runs, "RUN_ROOTS", (self.root / "unit",)))
        self.stack.enter_context(patch.dict(os.environ, {"REVIEW_POOL_STATE": str(self.root / "pool"),
                                                        "GUILD_ROLES_STATE": str(self.root / "guilds/roles"),
                                                        "BUILDLOG_SCOPE": "0"}))
        self.sent = []
        _ = self.stack.enter_context(patch.object(treekey, "tree_key", return_value="key-1"))

        def send_to_director(session_id: str, summary: str, text: str) -> bool:
            self.sent.append(DirectorNotice(session_id, summary, text))
            return True

        _ = self.stack.enter_context(patch.object(mesh_runs, "send_to_director", send_to_director))
        self.write_roster("thread-1", "running", os.getpid(), port=4100)

    @override
    def tearDown(self) -> None:
        self.stack.close()

    def test_durable_role_is_found_after_its_run_folder_is_gone(self) -> None:
        role_root = self.root / "guilds/roles"
        mesh = role_root / "design-one/mesh"
        mesh.mkdir(parents=True)
        _ = (mesh / codex_mesh.ROSTER_FILE).write_text(json.dumps({"lasting-designer": {
            "thread_id": "lasting-thread", "status": "idle", "launcher_pid": 0}}))
        with patch.dict(os.environ, {"GUILD_ROLES_STATE": str(role_root)}):
            caller = mesh_runs.caller_from_environment({"CODEX_THREAD_ID": "lasting-thread"})
        self.assertEqual(caller, mesh_runs.MeshCaller(str(mesh), "lasting-designer", "lasting-thread"))
        with patch.dict(os.environ, {"GUILD_ROLES_STATE": str(role_root), "CODEX_THREAD_ID": "lasting-thread"}), \
                patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=4321)), \
                contextlib.redirect_stdout(io.StringIO()):
            code = mesh_runs.main(["start", "--label", "role fixture", "--", sys.executable,
                                   "-c", "print('durable result')"])
        self.assertEqual(code, 0)
        records = list((mesh / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        record = self.supervise(records[0])
        self.assertEqual((record["status"], record.get("exit_code")), ("finished", 0))
        pending = cast(object, json.loads((mesh / "lasting-designer.pending.json").read_text()))
        self.assertIn("durable result", str(pending))
        self.assertEqual(self.sent, [])

    def test_detached_result_stays_pending_for_the_held_roles_next_authorized_turn(self) -> None:
        design_id = "00000000-0000-4000-8000-000000000076"
        design = roles.HeldDesign(roles.DesignPlacement(design_id, "fixture-host", 1), "fixture-repo",
            str(self.root), str(self.root / "design.md"), "design", "2026-10-21T12:00:00+00:00",
            roles.UnitHolder("demo", "design-unit"), roles.DesignUnbound(),
            (roles.RoleInstance("designer", "unused", roles.RoleNotOpened()),
             roles.RoleInstance("reviewer", WORKER, roles.RoleOpen("thread-1"))))
        roles._save(design)  # pyright: ignore[reportPrivateUsage]
        roster = {WORKER: {"thread_id": "thread-1", "status": "done", "held_design_id": design_id,
                           "held_role": "reviewer", "launcher_pid": 0}}
        _ = (self.session / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        path = self.record([sys.executable, "-c", "print('saved verification evidence')"])
        with patch.object(codex_mesh, "command_follow", side_effect=AssertionError("result woke the role")), \
                patch.object(codex_mesh, "command_send", side_effect=AssertionError("result sent a turn")), \
                patch.object(roles, "dispatch_turn", side_effect=AssertionError("result dispatched a role")):
            record = self.supervise(path)
            repeated = mesh_runs.deliver(record, 0)
        messages = roles.pending_messages(design_id, "reviewer")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].source, "mesh_runs:r1")
        self.assertIn("saved verification evidence", messages[0].text)
        self.assertIn("queued for held role", repeated)
        self.assertEqual(self.sent, [])
        self.assertEqual(cast(object, json.loads((self.session / codex_mesh.ROSTER_FILE).read_text())), roster)
        self.assertFalse((self.session / (WORKER + ".pending.json")).exists())

    def write_roster(self, thread_id: str, status: str, launcher_pid: int, port: int) -> None:
        roster = {WORKER: {"thread_id": thread_id, "status": status, "launcher_pid": launcher_pid,
                           "port": port, "turn_id": "turn-1"}}
        _ = (self.session / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster), encoding="utf-8")

    def pending(self) -> dict[str, object]:
        path = self.session / f"{WORKER}.pending.json"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return cast(dict[str, object], json.loads(text)) if text.strip() else {}

    def record(self, command: list[str], **extra: str) -> Path:
        runs = self.session / codex_mesh.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        record: mesh_runs.RunRecord = {
            "run_id": "r1", "status": "running", "session_dir": str(self.session), "worker": WORKER,
            "thread_id": "thread-1", "label": "verify.sh check", "command": command, "cwd": str(self.root),
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
            stand_in = subprocess.Popen([sys.executable, "-c",
                                         "import sys; print('ready', flush=True); sys.stdin.read()",
                                         "mesh_runs.py", "supervise", str(path)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(stand_in.wait)
            self.addCleanup(stand_in.kill)
            assert stand_in.stdin is not None and stand_in.stdout is not None
            self.addCleanup(stand_in.stdin.close)
            self.addCleanup(stand_in.stdout.close)
            # Wait for Python to start before the test reads the child's command line.
            self.assertTrue(select.select([stand_in.stdout], [], [], 300)[0], "supervisor did not start")
            self.assertEqual(stand_in.stdout.readline(), "ready\n")
            pid = stand_in.pid
        self.update_record(path, cwd=os.getcwd(), tree_key="key-1", pid=pid)
        return path

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
    def test_start_detaches_a_supervisor_and_records_the_run(self) -> None:
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        environment = {"CODEX_THREAD_ID": "thread-1", "CLAUDE_CODE_SESSION_ID": "director-session",
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
        self.assertEqual((record["status"], record["pid"], record["worker"], record["thread_id"]),
                         ("running", 4321, WORKER, "thread-1"))
        self.assertEqual(record["command"], ["bash", "verify.sh", "check"])
        self.assertNotIn("port", record)
        self.assertIn("next message", output.getvalue())

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
        path = self.live_run(command)
        for other_command, seat in ((["bash", "verify.sh", "check", "hana"], WORKER), (command, "unit-review")):
            popen, _ = self.start(other_command, seat)
            popen.assert_called_once()
        self.update_record(path, cwd=str(self.root))
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
        session, thread = str(self.session), "thread-1"
        path = self.record(["bash", "verify.sh", "check"])
        self.update_record(path, pid=DEAD_PID)
        codex_mesh._end_dead_runs(session, "unit-other")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.pending(), {}, "another worker's launcher ends nothing of this one")
        codex_mesh._end_dead_runs(session, WORKER)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("FAIL (no exit status)", cast(list[str], self.pending()["messages"])[0])
        self.assertFalse(codex_mesh._close_messages_if_empty(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]

    def test_a_supervisor_for_a_different_record_is_not_trusted(self) -> None:
        path = self.live_run(["echo", "x"])
        original = cast(mesh_runs.RunRecord, json.loads(path.read_text()))
        self.assertTrue(mesh_runs.supervises(original["pid"], path))
        another = path.with_name("another.json")
        _ = another.write_text(path.read_text())
        self.assertFalse(mesh_runs.supervises(original["pid"], another))
        self.assertFalse(mesh_runs.supervises(os.getpid(), path))

    def test_a_live_supervisor_is_not_ended(self) -> None:
        path = self.live_run(["bash", "verify.sh", "check"])
        self.assertEqual(mesh_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "running")

    def test_start_exits_3_for_a_caller_that_is_not_a_mesh_worker(self) -> None:
        popen = MagicMock()
        for environment in ({"CODEX_THREAD_ID": "thread-unknown"}, {"CODEX_THREAD_ID": ""}):
            with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen):
                self.assertEqual(mesh_runs.main(["start", "--", "true"]), mesh_runs.NOT_A_MESH_WORKER)
        self.write_roster("thread-1", "ended", 0, port=4100)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-1"}), patch.object(subprocess, "Popen", popen):
            self.assertEqual(mesh_runs.main(["start", "--", "true"]), mesh_runs.NOT_A_MESH_WORKER)
        popen.assert_not_called()

    # Criterion 2: the result is held for the seat, then the record is marked finished.
    def test_supervise_delivers_before_marking_finished(self) -> None:
        path = self.record([sys.executable, "-c", "import os; print('lint clean', os.environ['UNIT_TEAM_ROLE'])"])
        hold = codex_mesh.hold_message
        statuses: list[object] = []

        def watched(session_dir: str, name: str, text: str, *, without_launcher: bool) -> codex_mesh.HoldOutcome:
            stored = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            statuses.append(stored["status"])
            return hold(session_dir, name, text, without_launcher=without_launcher)

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
        (self.session / f"{WORKER}.pending.json").unlink()
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
        (self.session / f"{WORKER}.pending.json").unlink()
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
        script = "print('x' * 1_100_000)\nfor n in range(30): print(str(n) * 5_000)\nprint('last line')"
        _ = self.supervise(self.record([sys.executable, "-c", script]))
        message = cast(list[str], self.pending()["messages"])[0]
        self.assertLess(len(message), mesh_runs.RESULT_TAIL_CHARS + 1_000)
        self.assertIn(f"{'29' * 1_000} [cut: 8000 more characters]", message)
        self.assertTrue(message.endswith("\nlast line"))
        _ = self.supervise(self.record([sys.executable, "-c", "print('x' * 1_100_000)"]))
        message = cast(list[str], self.pending()["messages"])[-1]
        self.assertIn(f"{'x' * mesh_runs.RESULT_LINE_CHARS} [cut: 1098000 more characters]", message)
        self.assertLess(len(message), mesh_runs.RESULT_TAIL_CHARS)

    # Criterion 3: the server and thread change between start and finish.
    def test_result_reaches_the_seats_current_thread_after_a_server_change(self) -> None:
        path = self.record([sys.executable, "-c", "pass"])
        self.write_roster("thread-2", "running", os.getpid(), port=4200)
        _ = (self.session / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4200, "pid": os.getpid()}))
        _ = self.supervise(path)
        self.assertEqual(self.pending()["thread_id"], "thread-2")
        self.assertEqual(len(cast(list[str], self.pending()["messages"])), 1)

    # Criterion 4: no launcher sends the result to the director; an attached launcher holds it.
    def test_no_launcher_sends_the_result_to_the_director_and_holds_nothing(self) -> None:
        active = self.root / "unit" / "active"
        active.mkdir()
        _ = (active / "director-session").write_text(f"{self.session}\n", encoding="utf-8")
        self.write_roster("thread-1", "done", DEAD_PID, port=4100)
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(2)"]))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].session_id, "director-session")
        for part in (f"Worker {WORKER}'s run", f"Worker: {WORKER}",
                     f"Dispatch the worker (implement.sh --to {WORKER})",
                     "Command: ", "FAIL (exit 2)", "Log: "):
            self.assertIn(part, self.sent[0].text)
        self.assertEqual(self.pending(), {})
        self.assertIn("sent to director", record.get("delivery", ""))

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
        self.write_roster("thread-1", "done", DEAD_PID, port=4100)
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(100)"]))
        self.assertEqual([notice.session_id for notice in self.sent], ["director-session"])
        self.assertEqual(record.get("delivery"), "sent to director director-session")

    def test_closed_inbox_goes_to_the_director(self) -> None:
        active = self.root / "unit" / "active"
        active.mkdir()
        _ = (active / "director-session").write_text(f"{self.session}\n", encoding="utf-8")
        with codex_mesh._pending_file(str(self.session), WORKER) as handle:  # pyright: ignore[reportPrivateUsage]
            codex_mesh._write_pending(handle, "thread-1", [], accepting_messages=False)  # pyright: ignore[reportPrivateUsage]
        _ = self.supervise(self.record([sys.executable, "-c", "pass"]))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.pending()["messages"], [])

    def test_live_run_keeps_the_launcher_attached_until_the_result_is_held(self) -> None:
        session, thread = str(self.session), "thread-1"
        path = self.record([sys.executable, "-c", "pass"])
        self.assertFalse(codex_mesh._close_messages_if_empty(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(codex_mesh._waiting_on_detached_run(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]
        _ = self.supervise(path)
        self.assertFalse(codex_mesh._detached_run_live(session, WORKER))  # pyright: ignore[reportPrivateUsage]
        # The held result now opens the next turn: the launcher refuses to close on it.
        self.assertFalse(codex_mesh._close_messages_if_empty(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(codex_mesh._waiting_on_detached_run(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]
        (self.session / f"{WORKER}.pending.json").unlink()
        self.assertTrue(codex_mesh._close_messages_if_empty(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]

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
        self.assertFalse(check("turn/completed", params, session, WORKER, "thread-1"))
        _ = codex_mesh.hold_message(session, WORKER, "next", without_launcher=True)
        self.assertTrue(check("turn/completed", params, session, WORKER, "thread-1"))
        failed: dict[str, object] = {"turn": {"id": "turn-1", "status": "interrupted", "error": {"message": "boom"}}}
        self.assertFalse(check("turn/completed", failed, session, WORKER, "thread-1"))

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
        self.assertIn(("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-9"}), calls)
        self.assertIn("interrupted", output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
