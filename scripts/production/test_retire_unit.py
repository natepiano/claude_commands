#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Run retire_unit.py in process against temporary repositories.
Shutdown, the Codex mesh and build_hold are recording stand-ins. Claude
sessions are fixture records; nothing real stops."""
from __future__ import annotations

from scripts.shutdown import remote

from scripts.production.staff_test_support import fixture_member, fixture_worker
from scripts.lint.memory_test_support import write_config_fixture

import io
import json
import math
import os
import signal
import time
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, final, override
from unittest import mock


from scripts.herdr import herdr
from scripts.production import retire_unit
from scripts.production import unit_lookup
from scripts.production import unit_move
from scripts.herdr.herdr import HerdrPlace
from scripts.production.production_doc import Production, read_production

from scripts.message import sessions
from scripts.message.sessions import SessionRecord

from scripts.production.staff_test_support import StaffTestCase
from scripts.lib import process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.shutdown import stop_resume, restart
from scripts.herdr import worker_panes
from scripts.shutdown import pane_record as staff_pane_record
from scripts.shutdown import pane_record
from scripts.production.staff import StaffId
from scripts.production import staff, unit_config
from scripts.lib.conf import UseConfiguredValue
from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment, fake_remote_removal, publish_mirror
from scripts.copies.remote_test_support import local_remote_transport
from scripts.lib import atomic_file, conf
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.agents import worker_lifecycle
from scripts.direct import workers

DIRECTOR = "11111111-1111-1111-1111-111111111111"
# The fixture replaces the lookup; a test of the lookup itself puts it back.
RUN_COMMAND = retire_unit.run
SEAT = "22222222-2222-2222-2222-222222222222"
STOP_SESSION = stop_resume.stop


def setUpModule() -> None:
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))


def git(where: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(where), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


def session(session_id: str, name: str, pid: int) -> SessionRecord:
    return SessionRecord(pid=pid, sessionId=session_id, name=name, messagingSocketPath="/nonexistent.sock",
                         updatedAt=0, tmux="", formerNames=[])


@final
class RetireUnitTests(unittest.TestCase):


    def test_retirement_continues_after_a_member_stop_raises(self) -> None:
        with (mock.patch.object(stop_resume, "stop", side_effect=[OSError("stop read failed"), stop_resume.NotRunning(SEAT)]) as stops,
              mock.patch.object(retire_unit, "refusals", return_value=[]),
              mock.patch.object(retire_unit, "remove", return_value=[]) as remove):
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(stops.call_count, 2)
        self.assertEqual(output.count("stop read failed"), 1)
        remove.assert_called_once()

    def test_retire_keeps_stopped_worker_rows(self) -> None:
        row = staff.snapshot().find(kind="worker", parent=self.unit_staff_id)[0]
        states: list[worker_panes.WorkerPane] = [worker_panes.OpenWorker(row, "outside-pane", "same-terminal", herdr.AgentStatus.WORKING)]
        self.marked = None
        self.claude = [item for item in self.claude if item["sessionId"] != DIRECTOR]
        atomic_file.write_text_atomic(self.run_folder / "worker_transport", "herdr\n")
        def observe(_parent: staff.StaffId, _record: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            return list(states)
        def stop(member: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
            del client
            self.assertEqual(member, row)
            self.assertGreater(deadline, time.monotonic())
            states[0] = worker_panes.StoppedWorker(row, "outside-pane", "same-terminal")
            return True
        with (mock.patch.object(worker_panes, "worker_run", return_value=self.run_folder),
              mock.patch.object(worker_panes, "observe", side_effect=observe),
              mock.patch.object(restart, "stop_row", side_effect=stop) as stops):
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(stops.call_count, 1)
        self.assertEqual(staff.by_id(row.id), row)
        unit = staff.by_id(self.unit_staff_id)
        assert unit is not None
        self.assertIsInstance(unit.worktree, staff.RemovedWorktree)
        self.assertEqual(states, [worker_panes.StoppedWorker(row, "outside-pane", "same-terminal")])

    def test_retirement_uses_the_staff_worktree(self) -> None:
        plan = self.worktree / "docs/plans/alpha.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        _ = plan.write_text("# Plan\n")
        _ = git(self.worktree, "add", str(plan))
        _ = git(self.worktree, "commit", "-m", "Retain the unit plan")
        _ = git(self.worktree, "push", "origin", "alpha-branch")
        _ = git(self.checkout, "merge", "--no-ff", "-m", "Merge the unit plan", "alpha-branch")
        director = self.root / "director-folder"
        _ = git(self.worktree, "checkout", "--detach")
        _ = git(self.checkout, "worktree", "add", str(director), "alpha-branch")
        _ = unit_config.write(director, unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
                                                             unit_config.AppPort(1), ("scripts",)))
        _ = staff.set_worktree(self.unit_staff_id, staff.PresentWorktree(remote.local_machine(), director), expected=staff.PresentWorktree(remote.local_machine(), self.worktree))
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(director.exists())
        self.assertTrue(self.worktree.exists())
        row = staff.snapshot().by_id(self.unit_staff_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIsInstance(row.worktree, staff.RemovedWorktree)


    staff: StaffTestCase = StaffTestCase()
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()
    run_folder: Path = Path()
    calls: list[list[str]] = []
    claude: list[SessionRecord] = []
    marked: unit_lookup.LabeledSession | None = None
    config: Path = Path()
    unit_staff_id: StaffId = StaffId("")

    def limits(self, *, poll: str = "0.2", wait: str = "10", command: str = "900") -> Path:
        path = self.root / "production.conf"
        _ = path.write_text(f"# range: 5e-324..\nRETIRE_POLL_SECONDS={poll}\n# range: 5e-324..\nRETIRE_TERM_WAIT_SECONDS={wait}\n"
                            + f"# range: 5e-324..\nRETIRE_COMMAND_TIMEOUT_SECONDS={command}\n")
        return path


    def test_retire_removes_only_the_units_idle_trees_and_workspaces(self) -> None:
        _ = write_config_fixture(self.root / "scratch.conf")
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as tree:
            pass
        with copies.standing("other-unit", self.checkout, copies.CommittedSource("HEAD")) as other:
            pass
        with copies.workspace(
            str(self.unit_staff_id), self.checkout, "HEAD", name="worker", branch="worker", task="gate:retire-worker",
        ) as workspace:
            pass
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(tree.exists())
        self.assertFalse(workspace.exists())
        self.assertTrue(other.exists())

    def test_retire_keeps_a_same_named_units_copy_in_another_production(self) -> None:
        from scripts.production.staff_test_support import fixture_member

        parent = fixture_member("showrunner", name="other-production")
        other = fixture_member("unit", name="alpha", parent=parent.id)
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as mine:
            pass
        with copies.standing(str(other.id), self.checkout, copies.CommittedSource("HEAD")) as theirs:
            pass
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(mine.exists())
        self.assertEqual(git(theirs, "rev-parse", "HEAD"), git(self.checkout, "rev-parse", "HEAD"))

    def test_retire_without_a_build_cache_has_no_standing_tree_to_remove(self) -> None:
        _ = write_config_fixture(self.root / "scratch.conf")
        _ = git(self.worktree, "rm", "Cargo.toml")
        _ = git(self.worktree, "commit", "-qm", "remove the root build manifest")
        _ = git(self.worktree, "push", "-q", "origin", "alpha-branch")
        _ = git(self.checkout, "merge", "-q", "--no-ff", "-m", "Merge manifest removal", "alpha-branch")
        with copies.standing(str(self.unit_staff_id), self.worktree, copies.CommittedSource("HEAD")) as tree:
            self.assertTrue(tree.is_relative_to(Path(os.environ["SCRATCH_ROOT"])))
            self.assertEqual(copies.owned_trees(str(self.unit_staff_id)).standing, ())
            self.assertEqual(git(tree, "rev-parse", "HEAD"), git(self.worktree, "rev-parse", "HEAD"))
        self.assertFalse(tree.exists())
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(copies.owned_trees(str(self.unit_staff_id)).standing, ())

    def test_retire_keeps_a_busy_tree_and_still_retires_the_unit(self) -> None:
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as tree:
            code, output = self.retire()
            self.assertEqual(code, 0, output)
            self.assertIn("busy", output)
            self.assertTrue(tree.exists())
        self.assertFalse(self.worktree.exists())

    def test_retirement_retry_removes_a_tree_kept_while_busy(self) -> None:
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as tree:
            code, output = self.retire()
            self.assertEqual(code, 0, output)
            self.assertIn("busy", output)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertIn("unchanged", output)
        self.assertFalse(tree.exists())

    def test_retirement_dry_run_lists_trees_and_workspaces_without_changes(self) -> None:
        _ = write_config_fixture(self.root / "scratch.conf")
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as tree:
            pass
        with copies.workspace(str(self.unit_staff_id), self.checkout, "HEAD", name="worker", branch="worker") as workspace:
            pass
        before = {path: path.read_bytes() for folder in (tree, workspace)
                  for path in folder.rglob("*") if path.is_file()}
        code, output = self.retire("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would remove: checkout {tree}", output.splitlines())
        self.assertIn(f"would remove: checkout {workspace}", output.splitlines())
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_one_retirement_shares_settings_through_validation_and_cleanup(self) -> None:
        with mock.patch.object(conf, "read", wraps=conf.read) as reads:
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual([call.args[0] for call in reads.call_args_list if call.args[0] == "production"],
                         ["production"])
        self.assertFalse(self.worktree.exists())

    def test_external_command_reads_current_settings_for_the_named_request(self) -> None:
        with mock.patch.object(conf, "read", wraps=conf.read) as reads:
            result = RUN_COMMAND([sys.executable, "-c", "print('fixture command')"],
                                 settings=UseConfiguredValue.CURRENT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "fixture command\n")
        reads.assert_called_once_with("production")

    def test_external_termination_reads_current_settings_for_the_named_request(self) -> None:
        with (mock.patch.object(conf, "read", wraps=conf.read) as reads,
              mock.patch.object(os, "kill") as send,
              mock.patch.object(retire_unit, "_alive", return_value=False)):
            left = retire_unit.terminate([1234], {1234: "fixture"}, settings=UseConfiguredValue.CURRENT)
        self.assertEqual(left, [])
        send.assert_called_once_with(1234, signal.SIGTERM)
        reads.assert_called_once_with("production")

    def test_sigterm_wait_reads_the_retirement_settings_once(self) -> None:
        with (mock.patch.object(conf, "read", wraps=conf.read) as reads,
              mock.patch.object(os, "kill") as send,
              mock.patch.object(retire_unit, "_alive", return_value=False)):
            self.assertEqual(retire_unit.terminate([1234], {1234: "fixture"}), [])
        send.assert_called_once_with(1234, signal.SIGTERM)
        reads.assert_called_once_with("production")

    def test_retirement_commands_use_only_the_remaining_operation_budget(self) -> None:
        path = self.limits(command="900")
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(time, "monotonic", return_value=100.0),
              worker_lifecycle.operation_deadline(deadline=100.25),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run):
            _ = RUN_COMMAND(["fixture-command"])
            self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(time, "monotonic", return_value=100.0),
              worker_lifecycle.operation_deadline(deadline=99.0),
              mock.patch.object(subprocess, "run") as run):
            with self.assertRaises(worker_lifecycle.OperationDeadlineExpired):
                _ = RUN_COMMAND(["fixture-command"])
            run.assert_not_called()

    def test_retirement_sigterm_wait_stops_at_the_shared_operation_deadline(self) -> None:
        path = self.limits(poll="1", wait="10")
        now = 100.0
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            nonlocal now
            sleeps.append(seconds)
            now += seconds

        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(time, "monotonic", side_effect=lambda: now),
              mock.patch.object(time, "sleep", side_effect=sleep),
              mock.patch.object(os, "kill") as send,
              mock.patch.object(retire_unit, "_alive", return_value=True),
              worker_lifecycle.operation_deadline(deadline=100.25)):
            with self.assertRaises(worker_lifecycle.OperationDeadlineExpired):
                _ = retire_unit.terminate([1234], {1234: "fixture"})
        send.assert_called_once_with(1234, signal.SIGTERM)
        self.assertEqual(sleeps, [0.25])

    def test_changed_command_timeout_is_read_at_each_use_without_reloading_the_module(self) -> None:
        path = self.limits(command="17")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            _ = RUN_COMMAND(["fixture-command"])
            self.assertEqual(run.call_args.kwargs["timeout"], 17)
            _ = self.limits(command="23")
            _ = RUN_COMMAND(["fixture-command"])
            self.assertEqual(run.call_args.kwargs["timeout"], 23)

    def test_changed_poll_and_term_wait_control_real_sigterm_loop_and_cap_the_last_sleep(self) -> None:
        path = self.limits()
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}):
            for poll, wait in ((0.3, 0.8), (0.7, 1.2)):
                with self.subTest(poll=poll, wait=wait):
                    _ = self.limits(poll=str(poll), wait=str(wait))
                    with (mock.patch.object(os, "kill") as send_signal,
                          mock.patch.object(retire_unit, "_alive", return_value=True),
                          mock.patch.object(time, "monotonic", side_effect=[0, 0, wait - 0.1, wait]),
                          mock.patch.object(time, "sleep") as sleep):
                        left = retire_unit.terminate([1234], {1234: "fixture"})
                    send_signal.assert_called_once_with(1234, signal.SIGTERM)
                    self.assertEqual(len(sleep.call_args_list), 2)
                    self.assertEqual(sleep.call_args_list[0].args, (poll,))
                    self.assertAlmostEqual(cast(float, sleep.call_args_list[1].args[0]), 0.1)
                    self.assertEqual(left, ["fixture: still runs after SIGTERM"])

    def test_an_explicit_shorter_wait_caps_the_configured_term_wait(self) -> None:
        path = self.limits(poll="1", wait="10")
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(os, "kill"),
              mock.patch.object(retire_unit, "_alive", return_value=True),
              mock.patch.object(time, "monotonic", side_effect=[0, 0, 0.1]),
              mock.patch.object(time, "sleep") as sleep):
            _ = retire_unit.terminate([1234], {1234: "fixture"}, wait=0.1)
        sleep.assert_called_once_with(0.1)


    def test_a_missing_retirement_config_file_refuses_with_its_path_and_key(self) -> None:
        path = self.root / "absent" / "production.conf"
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}), self.assertRaises(conf.Unusable) as error:
            _ = RUN_COMMAND(["fixture-command"])
        self.assertIn(str(path), str(error.exception))
        self.assertIn("RETIRE_COMMAND_TIMEOUT_SECONDS", str(error.exception))

    @override
    def setUp(self) -> None:
        _ = self.enterContext(fake_cargo_output_roots(children=True))
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = self.limits()
        atomic_file.write_text_atomic(self.root / "shutdown.conf", (Path(__file__).resolve().parents[2] / "config/shutdown.conf").read_text())
        self.enterContext(mock.patch.dict(os.environ, copies_environment(self.root)))
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.root)}))
        origin = self.root / "origin.git"
        _ = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(origin), str(self.checkout)], check=True,
                           capture_output=True)
        for setting in (("user.email", "test@example.invalid"), ("user.name", "test"), ("commit.gpgsign", "false")):
            _ = git(self.checkout, "config", *setting)
        _ = (self.checkout / "Cargo.toml").write_text("[workspace]\n")
        self.worktree = self.root / "alpha-wt"
        self.doc = self.checkout / "alpha-production.md"
        _ = self.doc.write_text(
            "# Production\n\n- **Merge branch:** main\n"
            + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n",
            encoding="utf-8")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "production")
        _ = git(self.checkout, "push", "-q", "origin", "main")
        _ = git(self.checkout, "worktree", "add", "-q", "-b", "alpha-branch", str(self.worktree))
        (self.worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
        _ = (self.worktree / "work.txt").write_text("done\n", encoding="utf-8")
        _ = git(self.worktree, "add", "work.txt")
        _ = git(self.worktree, "commit", "-q", "-m", "checkpoint(alpha): the work")
        _ = git(self.worktree, "push", "-q", "origin", "alpha-branch")
        _ = git(self.checkout, "merge", "-q", "--no-ff", "-m", "Merge alpha", "alpha-branch")

        runs = self.root / "runs"
        self.run_folder = runs / "run-1"
        self.run_folder.mkdir(parents=True)
        atomic_file.write_text_atomic(self.run_folder / "worker_transport", "mesh\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.run_folder)})))
        # The test's own pid stands in for the app-server: the mesh stand-in never signals it.
        _ = (self.run_folder / "mesh_server.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        holds = self.root / "holds"
        holds.mkdir()
        _ = (holds / DIRECTOR).write_text("{}", encoding="utf-8")

        self.calls = []
        self.claude = [session(DIRECTOR, "alpha", 4101), session(SEAT, "alpha-seat", 4102)]
        _ = self.staff.record_production("alpha", self.doc)
        parent = fixture_member("showrunner", name="alpha")
        self.unit_staff_id = fixture_member("unit", name="alpha", parent=parent.id, runtime_id=DIRECTOR,
                         worktree=staff.PresentWorktree(remote.local_machine(), self.worktree)).id
        _ = unit_config.write(self.worktree, unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
                                                               unit_config.AppPort(1), ("scripts",)))
        _ = fixture_member("worker", name="seat", parent=self.unit_staff_id, runtime_id=SEAT)
        production = read_production("alpha")

        def production_of(_production_name: str, *, record: staff.StaffSnapshot) -> Production:
            del record
            return production

        def nothing_inside(_worktree: Path) -> list[int]:
            return []

        def no_reservations(_worktree: Path) -> tuple[str, ...]:
            return ()

        def session_processes(session_id: str) -> sessions.SessionProcesses:
            return sessions.SessionProcesses(tuple(item["pid"] for item in self.claude if item["sessionId"] == session_id))

        def session_liveness(session_id: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            owner = next((item for item in self.claude if item["sessionId"] == session_id), None)
            if owner:
                return sessions.LiveSessionProcess(owner["pid"])
            marked = unit_lookup.labeled_session("alpha", "alpha")
            if marked and isinstance(marked.claude, unit_lookup.LiveClaude) and marked.claude.session_id == session_id:
                return sessions.LiveSessionProcess(marked.claude.pid)
            return sessions.SessionNotLive()

        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": str(runs),
                                                       "UNIT_WORKERS_STATE": str(self.root / "unit-workers"),
                                                       "BUILD_HOLD_DIR": str(holds),
                                                       "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                                                       "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership")}))
        roster: dict[str, object] = {}
        home = self.root / "codex"
        store = workers.UnitWorkerLedger(workers.ledger_path("alpha", "alpha"))
        with store.locked() as ledger:
            for name, status in (("alpha-impl", "running"), ("alpha-old", "ended")):
                row = fixture_worker(name, self.unit_staff_id, backend=staff.CodexBackend(home))
                roster[name] = {"staff_id": row.id, "status": status}
                ledger.workers[name] = workers.UnitWorker(name, "impl", "codex", "", "", {},
                    workers.CodexThread(row.current_session, str(home)), workers.Paused(), [], staff_id=row.id)
        _ = (self.run_folder / "mesh_roster.json").write_text(json.dumps(roster), encoding="utf-8")
        _ = self.enterContext(mock.patch.object(retire_unit, "MAC_CLAIMS", self.root / "claims"))
        _ = self.enterContext(mock.patch.object(retire_unit, "production_of", production_of))
        _ = self.enterContext(mock.patch.object(retire_unit, "run", self.fake_run))
        _ = self.enterContext(mock.patch.object(stop_resume, "stop", self.stop_session))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", nothing_inside))
        _ = self.enterContext(mock.patch.object(retire_unit, "reservations", no_reservations))
        _ = self.enterContext(mock.patch.object(sessions, "session_processes", session_processes))
        _ = self.enterContext(mock.patch.object(sessions, "session_liveness", session_liveness))
        self.marked = unit_lookup.LabeledSession(
            "alpha", unit_lookup.LiveClaude("alpha", DIRECTOR, "/nonexistent.sock", 4101), HerdrPlace("alpha", "w1:p1"))

        def labeled_session(_production_name: str, _name: str) -> unit_lookup.LabeledSession | None:
            return self.marked

        self.config = self.root / "config"
        self.enterContext(mock.patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": str(self.root / "panes")}))
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        _ = self.enterContext(mock.patch.object(staff_pane_record, "RECORD_DIR", self.root / "panes"))
        pane_record.write("alpha", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
            {"CLAUDE_CONFIG_DIR": str(self.config), ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha"}, ["claude", "-n", "alpha"]))
        def process_info(*_arguments: str, deadline: float = math.inf) -> dict[str, object]:
            del deadline
            # Existing cases launch Claude with exec: it replaces the pane's shell.
            pid = self.marked.claude.pid if self.marked and isinstance(self.marked.claude, unit_lookup.LiveClaude) else 4101
            return {"process_info": {"shell_pid": pid}}

        _ = self.enterContext(mock.patch.object(unit_lookup, "labeled_session", labeled_session))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "result", side_effect=process_info))
        _ = self.enterContext(mock.patch.object(sessions, "config_dirs", lambda: [self.config]))
        self.transcript(self.worktree)

    def transcript(self, cwd: Path) -> None:
        folder = self.config / "projects" / unit_move.project_folder(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / f"{DIRECTOR}.jsonl").write_text(json.dumps({"entrypoint": "cli", "cwd": str(cwd)}) + "\n")

    def fake_run(self, argv: list[str], cwd: Path | None = None, *,
                 settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> subprocess.CompletedProcess[str]:
        """Git runs for real; every other command is recorded and answered."""
        del settings
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        call = [Path(argv[1]).name, *argv[2:]] if argv[0] == sys.executable else argv
        self.calls.append(call)
        if call[:2] == ["codex_mesh.py", "stop"]:
            (Path(call[call.index("--session-dir") + 1]) / "mesh_server.json").unlink(missing_ok=True)
        elif call[:2] == ["build_hold.py", "release"]:
            (Path(os.environ["BUILD_HOLD_DIR"]) / call[call.index("--holder") + 1]).unlink(missing_ok=True)
        stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def stop_session(self, identity: str, *, deadline: float = math.inf) -> stop_resume.Stopped | stop_resume.NotRunning:
        del deadline
        self.calls.append(["stop_resume.stop", identity])
        owner = next((item for item in self.claude if item["sessionId"] == identity), None)
        self.claude = [item for item in self.claude if item["sessionId"] != identity]
        if self.marked and isinstance(self.marked.claude, unit_lookup.LiveClaude) and self.marked.claude.session_id == identity:
            self.marked = None
        return stop_resume.Stopped(identity, owner["pid"]) if owner else stop_resume.NotRunning(identity)

    def retire(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = retire_unit.main(["alpha", "alpha", *arguments])
        return code, output.getvalue()

    def stops(self) -> list[list[str]]:
        return self.calls.copy()

    def unit_worktree(self) -> staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled:
        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        return row.worktree


    def test_retirement_without_saved_work_still_records_the_staff_identity(self) -> None:
        unit = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(git(self.checkout, "ls-remote", "origin", f"refs/retired/{unit.id}"), "")
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree(remote.local_machine(), self.worktree)))


    def test_a_finished_berth_reservation_is_released_on_replay(self) -> None:
        unit = retire_unit.find_unit("alpha", "alpha")
        for stdout, stderr in (("already has a final disposition", ""),
                               ("", "already has a final disposition")):
            with self.subTest(stdout=stdout, stderr=stderr):
                output = io.StringIO()
                with (mock.patch.object(retire_unit, "reservations", return_value=("reservation-1",)),
                      mock.patch.object(retire_unit, "run", return_value=subprocess.CompletedProcess(
                          ["cargo-berth"], 5, stdout, stderr)), redirect_stdout(output)):
                    retire_unit.release_reservations(unit, settings=conf.read("production"))
                self.assertEqual(output.getvalue(), "")
        output = io.StringIO()
        with (mock.patch.object(retire_unit, "reservations", return_value=("reservation-1",)),
              mock.patch.object(retire_unit, "run", return_value=subprocess.CompletedProcess(
                  ["cargo-berth"], 5, "", "reservation could not be released")), redirect_stdout(output)):
            retire_unit.release_reservations(unit, settings=conf.read("production"))
        self.assertIn("reservation could not be released", output.getvalue())


    def orphan_child(self, session_id: str) -> subprocess.Popen[bytes]:
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE, cwd=self.worktree, start_new_session=True,
                                 env={**os.environ, "CLAUDE_CODE_SESSION_ID": session_id})
        def cleanup() -> None:
            if child.stdin is not None:
                child.stdin.close()
            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(cleanup)
        return child

    def test_dead_sessions_orphan_group_is_signalled_after_identity_checks(self) -> None:
        child = self.orphan_child(DIRECTOR)
        self.claude = []
        unit = retire_unit.find_unit("alpha", "alpha")
        with (mock.patch.object(process_group, "send_process_group") as send,
              mock.patch.object(retire_unit, "_alive", return_value=False)):
            self.assertEqual(retire_unit.stop_orphan(unit, child.pid, DIRECTOR, settings=conf.read("production")), [])
        send.assert_called_once_with(child.pid, signal.SIGTERM)
        self.assertIsNone(child.poll(), "the test patches the signal; the child still runs")

    def test_unowned_or_live_sessions_process_is_named_and_never_signalled(self) -> None:
        unit = retire_unit.find_unit("alpha", "alpha")
        for session_id, live_owner in ((SEAT, False), (DIRECTOR, True)):
            with self.subTest(session_id=session_id, live_owner=live_owner):
                child = self.orphan_child(session_id)
                self.claude = [session(DIRECTOR, "alpha", 4101)] if live_owner else []
                with mock.patch.object(process_group, "send_process_group") as send:
                    left = retire_unit.stop_orphan(unit, child.pid, DIRECTOR, settings=conf.read("production"))
                send.assert_not_called()
                self.assertEqual(len(left), 1, left)
                for detail in (f"unowned process {child.pid}, parent ", "command ", "start ", str(self.worktree)):
                    self.assertIn(detail, left[0])

    def test_orphan_start_time_change_before_signal_refuses_without_sending(self) -> None:
        child = self.orphan_child(DIRECTOR)
        self.claude = []
        unit = retire_unit.find_unit("alpha", "alpha")
        birth = retire_unit.proc_start(child.pid)
        with (mock.patch.object(retire_unit, "proc_start", side_effect=[birth, birth, birth, "reused"]),
              mock.patch.object(process_group, "send_process_group") as send):
            left = retire_unit.stop_orphan(unit, child.pid, DIRECTOR, settings=conf.read("production"))
        send.assert_not_called()
        self.assertTrue(any("identity changed before signal" in line for line in left), left)


    def test_refuses_uncommitted_work_and_an_unmerged_checkpoint_and_changes_nothing(self) -> None:
        _ = (self.worktree / "late.txt").write_text("late\n", encoding="utf-8")
        _ = git(self.worktree, "add", "late.txt")
        _ = git(self.worktree, "commit", "-q", "-m", "checkpoint(alpha): the late work")
        _ = (self.worktree / "draft.txt").write_text("draft\n", encoding="utf-8")
        before = self.doc.read_text(encoding="utf-8")

        code, output = self.retire()

        self.assertEqual(code, 1)
        self.assertIn(f"refused: uncommitted work in {self.worktree}: draft.txt", output)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.doc.read_text(encoding="utf-8"), before)

    def test_the_recorders_project_started_line_is_not_uncommitted_work(self) -> None:
        plan = self.worktree / "work.txt"
        recorded = plan.read_text(encoding="utf-8") + "- **Project started:** 2026-10-18T22:00:00-04:00\n"
        _ = plan.write_text(recorded, encoding="utf-8")

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        self.assertIn(f"would remove: worktree {self.worktree}", output.splitlines())
        # A line a person wrote beside it is still uncommitted work.
        _ = plan.write_text(recorded + "a note\n", encoding="utf-8")
        code, output = self.retire("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn(f"refused: uncommitted work in {self.worktree}: work.txt", output)


    def test_dry_run_of_merged_work_has_no_save(self) -> None:
        before = self.doc.read_bytes()
        tip = git(self.worktree, "rev-parse", "HEAD")

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        self.assertFalse(any(line.startswith("would save:") for line in output.splitlines()), output)
        self.assertIn(f"would remove: worktree {self.worktree}", output.splitlines())
        self.assertIn("would delete: branch alpha-branch here and on origin", output.splitlines())
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual(self.stops(), [])


    def test_retires_a_unit_with_a_seat_a_worker_and_a_reviewer(self) -> None:
        code, output = self.retire()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.stops(), [
            ["stop_resume.stop", DIRECTOR],
            ["stop_resume.stop", SEAT],
            ["build_hold.py", "release", "--holder", DIRECTOR],
        ])
        self.assertIn("alpha retired", output)
        self.assertEqual(self.unit_worktree(), staff.RemovedWorktree(remote.local_machine(), self.worktree))
        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        self.assertEqual(row.current_session, DIRECTOR)
        self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "branch", "--list", "alpha-branch"), "")
        self.assertEqual(git(self.checkout, "ls-remote", "--heads", "origin", "alpha-branch"), "")


    def outside_director(self) -> Path:
        self.claude = []
        old = self.config / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        old.unlink()
        folder = self.root / "original-checkout"
        self.transcript(folder)
        return folder

    def test_retirement_keeps_the_transcript_in_its_original_folder(self) -> None:
        folder = self.outside_director()
        transcript = self.config / "projects" / unit_move.project_folder(str(folder)) / f"{DIRECTOR}.jsonl"
        old = self.root / "old-checkout"
        _ = transcript.write_text("\n".join(json.dumps({"cwd": str(cwd)})
                                            for cwd in (old, old, folder, folder)) + "\n")
        before = transcript.read_bytes(), transcript.stat().st_mtime_ns
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        self.assertEqual(row.current_session, DIRECTOR)
        self.assertFalse(self.worktree.exists())
        self.assertEqual((transcript.read_bytes(), transcript.stat().st_mtime_ns), before)


    def test_retirement_removes_a_worktree_with_only_the_project_started_line(self) -> None:
        plan = self.worktree / "work.txt"
        _ = plan.write_text(plan.read_text() + "- **Project started:** 2026-10-18T22:00:00-04:00\n")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.unit_worktree(), staff.RemovedWorktree(remote.local_machine(), self.worktree))

    def test_retirement_removes_the_gate_copy_owned_by_the_unit_worktree(self) -> None:
        owner = copies.source_owner(self.worktree)
        with copies.standing(owner, self.worktree, copies.CommittedSource("HEAD")) as checkout:
            self.assertTrue(checkout.exists())
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(checkout.exists())

    def test_retirement_removes_only_its_remote_mirrors(self) -> None:
        mine = copies.RemoteMirror(str(self.unit_staff_id), copies.SelectedRepository("hana"), "mac", "mirror/alpha", "alpha-session")
        other = copies.RemoteMirror("beta-unit", copies.SelectedRepository("hana"), "mac", "mirror/beta", "beta-session")
        with fake_remote_removal((mine, other)) as remove:
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        remove.assert_called_once_with(mine)
        self.assertEqual(copies.owned_remote_mirrors("beta-unit"), [other])

    def test_retirement_refuses_a_busy_remote_mirror_before_worktree_removal(self) -> None:
        mirror = copies.RemoteMirror(str(self.unit_staff_id), copies.SelectedRepository("hana"), "mac", "mirror/alpha", "alpha-session")
        copies.register_remote(mirror)
        with copies.remote_lease(mirror):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("alpha-session", output)
        self.assertIn("mirror/alpha", output)
        self.assertTrue(self.worktree.exists())
        self.assertTrue(copies.remote_record_path(mirror).exists())

    def test_retirement_removes_an_idle_remote_mirror_through_the_remote_command(self) -> None:
        mirror = copies.RemoteMirror(str(self.unit_staff_id), copies.SelectedRepository("hana"), "mac",
                                     str(self.root / "mirrors/alpha"), "alpha-session")
        with local_remote_transport(self.root):
            self.assertEqual(publish_mirror(mirror), 0)
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(Path(mirror.path).exists())
        self.assertFalse(copies.remote_record_path(mirror).exists())


if __name__ == "__main__":
    _ = unittest.main()
