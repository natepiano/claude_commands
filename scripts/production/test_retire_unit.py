#!/usr/bin/env python3
"""Run retire_unit.py in process against temporary repositories.
Shutdown, the Codex mesh and build_hold are recording stand-ins. Claude
sessions are fixture records; nothing real stops."""
from __future__ import annotations

import io
import json
import math
import os
import signal
import shutil
import time
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.astra import design_roles
from scripts.astra.test_design_roles import damage_binding_repository
from scripts.astra.design_role_test_support import write_empty_binding_record
from scripts.direct import workers  # noqa: E402

import add_unit
import herdr
import retire_unit
import production_units
import unit_lookup
import unit_move
from herdr import HerdrPlace
from production_units import Production, read_production
from scripts.production.fake_unit_host import PaneShellFixture

if TYPE_CHECKING:
    from scripts.message import sessions
    from scripts.message.sessions import SessionRecord
else:
    # retire_unit puts the message and shutdown folders on the path.
    import sessions
    from sessions import SessionRecord

from scripts.production.staff_test_support import StaffTestCase
from scripts.lib import process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.shutdown import stop_resume
import pane_record
from scripts.production.staff import StaffId
from scripts.production import staff, unit_config
from scripts.production.rename_rerun_test_support import saved_fixture
from scripts.lib import conf
from scripts.agents import worker_lifecycle

DIRECTOR = "11111111-1111-1111-1111-111111111111"
# The fixture replaces the lookup; a test of the lookup itself puts it back.
CLAUDE_SESSIONS = retire_unit.claude_sessions
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
    staff: StaffTestCase = StaffTestCase()
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()
    run_folder: Path = Path()
    calls: list[list[str]] = []
    claude: list[SessionRecord] = []
    marked: unit_lookup.MarkedUnit | None = None
    config: Path = Path()
    unit_staff_id: StaffId = StaffId("")

    def limits(self, *, poll: str = "0.2", wait: str = "10", command: str = "900") -> Path:
        path = self.root / "production.conf"
        _ = path.write_text(f"# range: 5e-324..\nRETIRE_POLL_SECONDS={poll}\n# range: 5e-324..\nRETIRE_TERM_WAIT_SECONDS={wait}\n"
                            + f"# range: 5e-324..\nRETIRE_COMMAND_TIMEOUT_SECONDS={command}\n")
        return path

    def prepared_retirement(self) -> staff.StaffRow:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        filled = staff.set_worktree(row.id, staff.PresentWorktree("natedev", self.worktree))
        unit_config.write(self.worktree, unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
                                                               unit_config.AppPort(1), ("scripts",)))
        return filled

    def test_a_failed_park_keeps_the_worktree_branch_and_present_staff_row(self) -> None:
        row = self.prepared_retirement()
        document = self.doc.read_bytes()
        with mock.patch.object(unit_config, "park", side_effect=unit_config.ConfigRefused("fixture park failed")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("fixture park failed", output)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(git(self.checkout, "branch", "--list", "alpha-branch"), "+ alpha-branch")
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(self.doc.read_bytes(), document)

    def test_missing_conversation_evidence_refuses_before_park_stop_or_removal(self) -> None:
        row = self.prepared_retirement()
        self.marked = None
        self.claude = []
        shutil.rmtree(self.config / "projects")
        _ = pane_record.forget_unit("alpha-unit")
        before = self.doc.read_bytes(), self.staff.staff_file.read_bytes()
        with mock.patch.object(unit_config, "park", side_effect=AssertionError("park before session proof")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(staff.by_id(row.id), row)
        self.assertFalse(unit_config.parked_path(row.id).exists())
        self.assertEqual(self.stops(), [])
        self.assertEqual((self.doc.read_bytes(), self.staff.staff_file.read_bytes()), before)

    def test_a_units_table_branch_disagreeing_with_git_refuses_before_park_or_stop(self) -> None:
        row = self.prepared_retirement()
        _ = self.doc.write_text(self.doc.read_text().replace("| alpha-branch |", "| stale-branch |"))
        with mock.patch.object(unit_config, "park") as park:
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("alpha-branch", output)
        self.assertIn("stale-branch", output)
        park.assert_not_called()
        self.assertEqual(self.stops(), [])
        self.assertEqual(staff.by_id(row.id), row)
        self.assertTrue(self.worktree.is_dir())

    def test_failed_worktree_removal_leaves_the_row_present_and_preserves_parked_session_facts(self) -> None:
        row = self.prepared_retirement()
        with mock.patch.object(retire_unit, "remove", return_value=["fixture removal refused"]):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("fixture removal refused", output)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertTrue(self.worktree.is_dir())
        parked = unit_config.read_parked(row.id)
        assert isinstance(parked, unit_config.UnitConfig)
        assert isinstance(parked.retired, unit_config.RetiredSession)
        self.assertEqual(parked.retired.session_id, DIRECTOR)
        self.assertEqual(parked.retired.branch, unit_config.Branch("alpha-branch"))

    def test_retirement_recovers_at_each_removal_boundary_across_a_date_change_without_rewriting_facts(self) -> None:
        row = self.prepared_retirement()
        park = unit_config.park
        remove = retire_unit.remove
        field = staff.set_worktree
        with saved_fixture(self.root) as restore, saved_fixture(self.staff.root) as restore_staff:
            for boundary in ("park", "worktree", "branch", "remove", "field"):
                with self.subTest(boundary=boundary):
                    restore()
                    restore_staff()
                    self.claude = []
                    self.marked = None

                    def parked_then_stop(unit: staff.StaffId, worktree: Path, facts: unit_config.RetiredSession) -> None:
                        park(unit, worktree, facts)
                        raise OSError("fixture interrupted after park")

                    def removed_then_stop(unit: retire_unit.Unit) -> list[str]:
                        errors = remove(unit)
                        self.assertEqual(errors, [])
                        raise OSError("fixture interrupted after remove")

                    def field_then_stop(identity: staff.StaffId,
                                        value: staff.PresentWorktree | staff.RemovedWorktree) -> staff.StaffRow:
                        _ = field(identity, value)
                        raise OSError("fixture interrupted after field")

                    def command_then_stop(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                        result = self.fake_run(argv, cwd)
                        operation = argv[3:] if argv[:1] == ["git"] else []
                        stopped = (boundary == "worktree" and operation[:2] == ["worktree", "remove"]
                                   or boundary == "branch" and operation[:2] == ["branch", "-D"])
                        if stopped and result.returncode == 0:
                            raise OSError(f"fixture interrupted after {boundary}")
                        return result

                    target = mock.patch.object(unit_config, "park", side_effect=parked_then_stop) if boundary == "park" else (
                        mock.patch.object(retire_unit, "remove", side_effect=removed_then_stop) if boundary == "remove" else
                        mock.patch.object(staff, "set_worktree", side_effect=field_then_stop) if boundary == "field" else
                        mock.patch.object(retire_unit, "run", side_effect=command_then_stop))
                    with target, mock.patch.object(retire_unit, "datetime", wraps=datetime) as clock:
                        clock.now = mock.Mock(return_value=datetime(2026, 10, 31, 12, tzinfo=UTC))
                        code, output = self.retire()
                    self.assertEqual(code, 1, output)
                    self.assertIn("fixture interrupted", output)
                    parked_path = unit_config.parked_path(row.id)
                    parked = parked_path.read_bytes(), parked_path.stat().st_mtime_ns
                    with mock.patch.object(retire_unit, "datetime", wraps=datetime) as clock:
                        clock.now = mock.Mock(return_value=datetime(2026, 11, 1, 12, tzinfo=UTC))
                        code, output = self.retire()
                    self.assertEqual(code, 0, output)
                    retained = staff.by_id(row.id)
                    assert retained is not None
                    self.assertEqual((retained.name, retained.parent, retained.worktree),
                                     (row.name, row.parent, staff.RemovedWorktree("natedev", self.worktree)))
                    self.assertFalse(self.worktree.exists())
                    self.assertEqual((parked_path.read_bytes(), parked_path.stat().st_mtime_ns), parked)
                    config = unit_config.read_parked(row.id)
                    assert isinstance(config, unit_config.UnitConfig)
                    assert isinstance(config.retired, unit_config.RetiredSession)
                    self.assertEqual(config.retired.date, "2026-10-31")
                    self.assertIn("retired 2026-10-31", self.doc.read_text())
                    calls, data, doc = self.calls.copy(), self.staff.staff_file.read_bytes(), self.doc.read_bytes()
                    code, output = self.retire()
                    self.assertEqual(code, 0, output)
                    self.assertEqual(len(output.splitlines()), 1)
                    self.assertEqual((self.calls, self.staff.staff_file.read_bytes(), self.doc.read_bytes()), (calls, data, doc))

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

    def test_missing_invalid_or_nonfinite_retirement_limits_refuse_before_stopping_or_removal(self) -> None:
        path = self.limits()
        before = self.doc.read_bytes(), self.staff.staff_file.read_bytes()
        for key in ("RETIRE_POLL_SECONDS", "RETIRE_TERM_WAIT_SECONDS", "RETIRE_COMMAND_TIMEOUT_SECONDS"):
            for value in (None, "broken", "nan", "inf", "0", "-1"):
                with self.subTest(key=key, value=value):
                    _ = self.limits()
                    lines = path.read_text().splitlines()
                    _ = path.write_text("\n".join(line if not line.startswith(key + "=") else
                                                (f"{key}={value}" if value is not None else "") for line in lines) + "\n")
                    with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}):
                        code, output = self.retire()
                    self.assertEqual(code, 1, output)
                    self.assertIn(str(path), output)
                    self.assertIn(key, output)
                    self.assertTrue(self.worktree.exists())
                    self.assertEqual(self.stops(), [])
                    self.assertEqual((self.doc.read_bytes(), self.staff.staff_file.read_bytes()), before)

    def test_a_missing_retirement_config_file_refuses_with_its_path_and_key(self) -> None:
        path = self.root / "absent" / "production.conf"
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}), self.assertRaises(conf.Unusable) as error:
            _ = RUN_COMMAND(["fixture-command"])
        self.assertIn(str(path), str(error.exception))
        self.assertIn("RETIRE_COMMAND_TIMEOUT_SECONDS", str(error.exception))

    @override
    def setUp(self) -> None:
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = self.limits()
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.root)}))
        origin = self.root / "origin.git"
        _ = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(origin), str(self.checkout)], check=True,
                           capture_output=True)
        for setting in (("user.email", "test@example.invalid"), ("user.name", "test"), ("commit.gpgsign", "false")):
            _ = git(self.checkout, "config", *setting)
        self.worktree = self.root / "alpha-wt"
        self.doc = self.checkout / "alpha-production.md"
        _ = self.doc.write_text(
            "# Production\n\n- **Merge branch:** main\n"
            + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n\n## Units\n\n"
            + "| Unit | Plan | Worktree | Branch | Port | Owns |\n| --- | --- | --- | --- | --- | --- |\n"
            + f"| alpha-unit | docs/plans/alpha.md | {self.worktree} | alpha-branch | 1 | scripts |\n",
            encoding="utf-8")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "production")
        _ = git(self.checkout, "push", "-q", "origin", "main")
        _ = git(self.checkout, "worktree", "add", "-q", "-b", "alpha-branch", str(self.worktree))
        _ = (self.worktree / "work.txt").write_text("done\n", encoding="utf-8")
        _ = git(self.worktree, "add", "work.txt")
        _ = git(self.worktree, "commit", "-q", "-m", "checkpoint(alpha): the work")
        _ = git(self.worktree, "push", "-q", "origin", "alpha-branch")
        _ = git(self.checkout, "merge", "-q", "--no-ff", "-m", "Merge alpha", "alpha-branch")

        runs = self.root / "runs"
        self.run_folder = runs / "run-1"
        self.run_folder.mkdir(parents=True)
        _ = (self.run_folder / "progress_history_state.json").write_text(
            json.dumps({"working_dir": str(self.worktree)}), encoding="utf-8")
        _ = (self.run_folder / "director").write_text(DIRECTOR + "\n", encoding="utf-8")
        # The test's own pid stands in for the app-server: the mesh stand-in never signals it.
        _ = (self.run_folder / "mesh_server.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        _ = (self.run_folder / "mesh_roster.json").write_text(
            json.dumps({"alpha-impl": {"status": "running"}, "alpha-old": {"status": "ended"}}), encoding="utf-8")
        holds = self.root / "holds"
        holds.mkdir()
        _ = (holds / DIRECTOR).write_text("{}", encoding="utf-8")

        self.calls = []
        self.claude = [session(DIRECTOR, "alpha", 4101), session(SEAT, "alpha-seat", 4102)]
        self.staff.register_production("alpha", self.doc)
        parent = staff.ensure("showrunner", name="alpha")
        self.unit_staff_id = staff.ensure("unit", name="alpha", parent=parent.id,
                         worktree=staff.PresentWorktree("natedev", self.worktree)).id
        unit_config.write(self.worktree, unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
                                                               unit_config.AppPort(1), ("scripts",)))
        production = read_production(self.doc)

        def production_of(_slug: str) -> Production:
            return production

        def claude_sessions(_worktree: Path, _ids: frozenset[str]) -> tuple[SessionRecord, ...]:
            return tuple(self.claude)

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
            marked = unit_lookup.marked_unit("alpha", "alpha-unit")
            if marked and isinstance(marked.claude, unit_lookup.LiveClaude) and marked.claude.session_id == session_id:
                return sessions.LiveSessionProcess(marked.claude.pid)
            return sessions.SessionNotLive()

        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": str(runs),
                                                       "UNIT_WORKERS_STATE": str(self.root / "unit-workers"),
                                                       "BUILD_HOLD_DIR": str(holds),
                                                       "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                                                       "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership")}))
        _ = self.enterContext(mock.patch.object(retire_unit, "MAC_CLAIMS", self.root / "claims"))
        _ = self.enterContext(mock.patch.object(retire_unit, "production_of", production_of))
        _ = self.enterContext(mock.patch.object(retire_unit, "run", self.fake_run))
        _ = self.enterContext(mock.patch.object(stop_resume, "stop", self.stop_session))
        _ = self.enterContext(mock.patch.object(retire_unit, "claude_sessions", claude_sessions))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", nothing_inside))
        _ = self.enterContext(mock.patch.object(retire_unit, "reservations", no_reservations))
        _ = self.enterContext(mock.patch.object(sessions, "session_processes", session_processes))
        _ = self.enterContext(mock.patch.object(sessions, "session_liveness", session_liveness))
        self.marked = unit_lookup.MarkedUnit(
            "alpha-unit", unit_lookup.LiveClaude("alpha", DIRECTOR, "/nonexistent.sock", 4101), HerdrPlace("alpha", "w1:p1"))

        def marked_unit(_slug: str, _name: str) -> unit_lookup.MarkedUnit | None:
            return self.marked

        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        pane_record.write("alpha", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
            {"CLAUDE_CONFIG_DIR": str(self.config), "SHOWRUNNER_UNIT_ID": "alpha-unit"}, ["claude", "-n", "alpha"]))
        def process_info(*_arguments: str, deadline: float = math.inf) -> dict[str, object]:
            del deadline
            # Existing cases launch Claude with exec: it replaces the pane's shell.
            pid = self.marked.claude.pid if self.marked and isinstance(self.marked.claude, unit_lookup.LiveClaude) else 4101
            return {"process_info": {"shell_pid": pid}}

        self.config = self.root / "config"
        _ = self.enterContext(mock.patch.object(unit_lookup, "marked_unit", marked_unit))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "result", side_effect=process_info))
        _ = self.enterContext(mock.patch.object(unit_move, "config_dirs", lambda: [self.config]))
        self.transcript(self.worktree)

    def transcript(self, cwd: Path) -> None:
        folder = self.config / "projects" / unit_move.project_folder(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / f"{DIRECTOR}.jsonl").write_text(json.dumps({"entrypoint": "cli", "cwd": str(cwd)}) + "\n")

    def fake_run(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Git runs for real; every other command is recorded and answered."""
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

    def stop_session(self, identity: str) -> stop_resume.Stopped | stop_resume.NotRunning:
        self.calls.append(["stop_resume.stop", identity])
        owner = next((item for item in self.claude if item["sessionId"] == identity), None)
        self.claude = [item for item in self.claude if item["sessionId"] != identity]
        if self.marked and isinstance(self.marked.claude, unit_lookup.LiveClaude) and self.marked.claude.session_id == identity:
            self.marked = None
        return stop_resume.Stopped(identity, owner["pid"]) if owner else stop_resume.NotRunning(identity)

    def retire(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = retire_unit.main(["alpha", "alpha", *arguments])
        return code, output.getvalue()

    def stops(self) -> list[list[str]]:
        return self.calls.copy()

    def pane_shell_completes(self, *, live: bool) -> None:
        pane = PaneShellFixture(self, self.worktree, self.root / "proc", live=live, slug="alpha")
        pane.found = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude(
            "alpha", DIRECTOR, "socket", pane.director) if live else unit_lookup.ClaudeNotRunning(), pane.place)
        self.claude = [session(DIRECTOR, "alpha", pane.director)] if live else []
        def clean_stop() -> None:
            pane.stopped()
            self.claude = []
        original = self.stop_session
        def stop(identity: str) -> stop_resume.Stopped | stop_resume.NotRunning:
            result = original(identity)
            clean_stop()
            return result
        if not live:
            clean_stop()
        with mock.patch.object(stop_resume, "stop", side_effect=stop):
            code, output = self.retire("--wait-minutes", "0")
        self.assertEqual(code, 0, output)
        self.assertEqual(pane.close_calls(), [["--session","agents","pane","close","w1:p1"]])
        self.assertFalse(self.worktree.exists())
        self.assertIn("retired", self.doc.read_text())
        self.assertEqual(sum(call == ["stop_resume.stop", DIRECTOR] for call in self.calls), 1 if live else 0)

    def test_live_director_in_pane_shell_stops_closes_and_retires(self) -> None:
        self.pane_shell_completes(live=True)

    def staff_unit(self) -> tuple[staff.StaffRow, staff.StaffRow]:
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "alpha")
        original = next(row for row in staff.find(kind="unit", parent=parent.id) if row.name == "alpha")
        runner = staff.rename(parent.id, "tools")
        unit = staff.rename(original.id, "panes")
        self.staff.pane("w1:runner", session_id="session-alpha", environment={"STAFF_ID": runner.id})
        self.staff.pane("w1:p1", session_id=DIRECTOR, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "alpha", "SHOWRUNNER_UNIT_ID": "alpha-unit"})
        return runner, unit

    def test_retire_unit_keeps_staff_workers_and_reports_each_one(self) -> None:
        runner, unit = self.staff_unit()
        worker = staff.ensure("worker", name="impl1", parent=unit.id)
        code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn(worker.id, output)
        self.assertIn(worker.name, output)
        self.assertEqual(staff.by_id(unit.id), unit)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(staff.by_id(runner.id), runner)
        self.assertFalse(self.worktree.exists())

    def test_retirement_keeps_the_staff_identity_that_names_its_saved_ref(self) -> None:
        unit = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        ref, tip = self.unmerged_work()
        self.assertEqual(ref, f"refs/retired/{unit.id}")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assert_saved_retirement(ref, tip)
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree("natedev", self.worktree)))
        self.assertIn("alpha-unit", unit_lookup.retired_units(self.doc.read_text().splitlines(), "alpha"))

    def test_retired_unit_name_refuses_new_admission_and_retirement_rerun_keeps_the_row(self) -> None:
        old = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        retained = staff.by_id(old.id)
        assert retained is not None
        self.assertEqual(retained.worktree, staff.RemovedWorktree("natedev", self.worktree))
        new_doc = self.staff.production("receiving")
        _ = new_doc.write_text("- **Merge branch:** main\n"
                               + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n"
                               + new_doc.read_text())
        receiving = staff.ensure("showrunner", name="receiving")
        before = self.staff.staff_file.read_bytes(), self.staff.staff_file.stat().st_mtime_ns
        with self.assertRaisesRegex(production_units.Refusal, "resume.*remove.*free the name"):
            _ = add_unit.ensure_staff_unit(read_production(self.staff.source_root / "receiving-production.md"),
                                          "alpha-unit")
        self.assertEqual(staff.by_id(old.id), retained)
        self.assertEqual(staff.by_id(receiving.id), receiving)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.staff.staff_file.stat().st_mtime_ns), before)
        self.assertFalse(self.marked and self.marked.staff_id)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(staff.by_id(old.id), retained)

    def test_retire_unit_rerun(self) -> None:
        runner, unit = self.staff_unit()
        original = staff.set_worktree
        for after in (False, True):
            def interrupted(staff_id: staff.StaffId, value: staff.PresentWorktree | staff.RemovedWorktree) -> staff.StaffRow:
                if after:
                    _ = original(staff_id, value)
                raise RuntimeError("stopped at staff retirement")
            with mock.patch.object(staff, "set_worktree", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "stopped at staff retirement"):
                    _ = self.retire()
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        retained = staff.by_id(unit.id)
        assert retained is not None
        self.assertEqual((retained.id, retained.parent, retained.worktree),
                         (unit.id, unit.parent, staff.RemovedWorktree("natedev", self.worktree)))
        self.assertEqual(staff.by_id(runner.id), runner)
        data, document = self.staff.staff_file.read_bytes(), self.doc.read_bytes()
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.doc.read_bytes()), (data, document))

    def test_exited_directors_pane_shell_closes_and_retires(self) -> None:
        self.pane_shell_completes(live=False)

    def test_fully_retired_rerun_prints_one_line_without_a_commit_or_push(self) -> None:
        ref, tip = self.unmerged_work()
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assert_saved_retirement(ref, tip)
        before = (git(self.checkout, "rev-parse", "HEAD"), git(self.checkout, "for-each-ref"),
                  git(self.checkout, "ls-remote", "origin"), self.doc.read_bytes(),
                  self.doc.stat().st_mtime_ns, self.staff.staff_file.read_bytes(),
                  self.staff.staff_file.stat().st_mtime_ns)
        commit_hook = self.checkout / ".git/hooks/pre-commit"
        push_hook = self.root / "origin.git/hooks/pre-receive"
        for hook in (commit_hook, push_hook):
            _ = hook.write_text('#!/bin/sh\ntouch "$0.called"\nexit 0\n')
            hook.chmod(0o755)

        code, output = self.retire()

        self.assertEqual(code, 0, output)
        self.assertEqual(output.splitlines(), ["alpha-unit already retired"])
        self.assertEqual((git(self.checkout, "rev-parse", "HEAD"), git(self.checkout, "for-each-ref"),
                          git(self.checkout, "ls-remote", "origin"), self.doc.read_bytes(),
                          self.doc.stat().st_mtime_ns, self.staff.staff_file.read_bytes(),
                          self.staff.staff_file.stat().st_mtime_ns), before)
        for hook in (commit_hook, push_hook):
            self.assertFalse(Path(str(hook) + ".called").exists(), f"rerun invoked {hook.name}")

    def test_retirement_without_saved_work_still_records_the_staff_identity(self) -> None:
        unit = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertNotIn(", ref ", self.doc.read_text())
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree("natedev", self.worktree)))
        self.assertIn("alpha-unit", unit_lookup.retired_units(self.doc.read_text().splitlines(), "alpha"))

    def test_dead_mac_claim_does_not_prevent_the_already_retired_line(self) -> None:
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        claim = retire_unit.MAC_CLAIMS / "finished.json"
        claim.parent.mkdir(parents=True)
        _ = claim.write_text(json.dumps({"worktree": str(self.worktree), "pid": 991552,
                                       "proc_start": "old-start", "what": "finished run"}))
        before = (claim.read_bytes(), self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                  git(self.checkout, "for-each-ref"), git(self.checkout, "ls-remote", "origin"))
        with mock.patch.object(retire_unit, "_alive", return_value=False), \
                mock.patch.object(retire_unit, "terminate", wraps=retire_unit.terminate) as terminate:
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(output.splitlines(), ["alpha-unit already retired"])
        terminate.assert_called_once_with([], {})
        self.assertEqual((claim.read_bytes(), self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                          git(self.checkout, "for-each-ref"), git(self.checkout, "ls-remote", "origin")), before)

    def test_gone_session_with_a_resolvable_tab_writes_no_stop_or_cancellation_record(self) -> None:
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        launch = self.root / "unexpected-launch"
        cancellation = self.root / "unexpected-cancellation"

        def record_launches(selected: list[str]) -> None:
            _ = launch.write_text("\n".join(selected))

        def cancel_prompt(session_id: str) -> None:
            _ = cancellation.write_text(session_id)

        before = (self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                  git(self.checkout, "for-each-ref"), git(self.checkout, "ls-remote", "origin"))
        with mock.patch.object(stop_resume, "stop", wraps=STOP_SESSION) as stop, \
                mock.patch.object(stop_resume, "_tab_of", return_value=herdr.TabSession(
                    "w1:p1", "alpha", "alpha", DIRECTOR)), \
                mock.patch.object(stop_resume, "_record_launches", side_effect=record_launches), \
                mock.patch.object(stop_resume, "_retire_prompt", side_effect=cancel_prompt), \
                mock.patch.object(os, "kill") as kill:
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(output.splitlines(), ["alpha-unit already retired"])
        stop.assert_not_called()
        kill.assert_not_called()
        self.assertFalse(launch.exists())
        self.assertFalse(cancellation.exists())
        self.assertEqual((self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                          git(self.checkout, "for-each-ref"), git(self.checkout, "ls-remote", "origin")), before)

    def test_mark_line_uses_the_commit_result_despite_an_unrelated_commit(self) -> None:
        unit = retire_unit.find_unit("alpha", "alpha")
        output = io.StringIO()

        def concurrent_commit(production: Production, paths: list[Path], message: str) -> bool:
            del production, paths, message
            _ = (self.checkout / "unrelated.txt").write_text("another writer's commit\n")
            _ = git(self.checkout, "add", "unrelated.txt")
            _ = git(self.checkout, "commit", "-q", "-m", "unrelated writer")
            return False

        before = git(self.checkout, "rev-parse", "HEAD")
        with mock.patch.object(add_unit, "commit_paths", side_effect=concurrent_commit), redirect_stdout(output):
            _ = retire_unit.mark_retired(unit, retire_unit.NoDirectorSession())
        self.assertNotEqual(git(self.checkout, "rev-parse", "HEAD"), before)
        self.assertNotIn("marked:", output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            _ = retire_unit.mark_retired(unit, retire_unit.NoDirectorSession())
        self.assertEqual(output.getvalue().splitlines(), ["marked: the retired Units row, committed on main"])

    def test_first_retirement_reports_each_save_stop_and_mark_once(self) -> None:
        ref, tip = self.unmerged_work()
        pane = PaneShellFixture(self, self.worktree, self.root / "action-proc", live=True, slug="alpha")
        pane.found = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude(
            "alpha", DIRECTOR, "socket", pane.director), pane.place)
        self.claude = [session(DIRECTOR, "alpha", pane.director), session(SEAT, "alpha-seat", 4102)]

        def stop(identity: str) -> stop_resume.Stopped | stop_resume.NotRunning:
            outcome = self.stop_session(identity)
            if identity == DIRECTOR:
                pane.stopped()
            return outcome

        with mock.patch.object(stop_resume, "stop", side_effect=stop):
            code, output = self.retire("--wait-minutes", "0")

        self.assertEqual(code, 0, output)
        lines = output.splitlines()
        self.assertEqual([line for line in lines if line.startswith("saved:")], [f"saved: {ref} at {tip}"])
        self.assertCountEqual([line for line in lines if line.startswith("stopped:")], [
            f"stopped: {DIRECTOR}", f"stopped: {SEAT}",
            f"stopped: app-server pid {os.getpid()} of run run-1; workers alpha-impl"])
        self.assertEqual([line for line in lines if line.startswith("marked:")],
                         ["marked: the retired Units row, committed on main"])
        self.assertEqual([line for line in lines if line.startswith("removed:")],
                         [f"removed: worktree {self.worktree}"])
        self.assertEqual([line for line in lines if line.startswith("deleted:")],
                         ["deleted: branch alpha-branch", "deleted: origin alpha-branch"])
        self.assertEqual(pane.close_calls(), [["--session", "agents", "pane", "close", "w1:p1"]])
        self.assertIn(["stop_resume.stop", DIRECTOR], self.calls)
        self.assertEqual(sum(call == ["stop_resume.stop", DIRECTOR] for call in self.calls), 1)
        self.assert_saved_retirement(ref, tip)

    def test_already_gone_session_has_no_stopped_action_line(self) -> None:
        self.claude = []
        self.marked = None
        (self.run_folder / "mesh_server.json").unlink()

        code, output = self.retire()

        self.assertEqual(code, 0, output)
        self.assertNotIn(["stop_resume.stop", DIRECTOR], self.calls)
        self.assertFalse(any(line.startswith("stopped:") for line in output.splitlines()), output)
        self.assertFalse(self.worktree.exists())

    def test_dry_run_closes_only_the_proven_pane_shell_and_keeps_other_blockers(self) -> None:
        self.claude = []
        pane = PaneShellFixture(self, self.worktree, self.root / "preview-proc", live=False, slug="alpha")
        pane.stopped()
        before = (self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                  git(self.checkout, "for-each-ref"), git(self.checkout, "ls-remote", "origin"))

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        self.assertIn(f"would close: pane {pane.place.pane} (shell {pane.shell})", output.splitlines())
        self.assertNotIn("blocks retirement:", output)
        pane.add_occupant(991551, marked=False)

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        self.assertIn(f"would close: pane {pane.place.pane} (shell {pane.shell})", output.splitlines())
        self.assertEqual([line for line in output.splitlines() if line.startswith("blocks retirement:")],
                         ["blocks retirement: unowned process 991551"])
        self.assertEqual(pane.close_calls(), [])
        self.assertEqual(self.stops(), [])
        self.assertEqual((self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                          git(self.checkout, "for-each-ref"), git(self.checkout, "ls-remote", "origin")), before)

    def unmerged_work(self) -> tuple[str, str]:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        _ = (self.worktree / "late.txt").write_text("unmerged work\n")
        _ = git(self.worktree, "add", "late.txt")
        _ = git(self.worktree, "commit", "-q", "-m", "checkpoint(alpha): unmerged work")
        return f"refs/retired/{row.id}", git(self.worktree, "rev-parse", "HEAD")

    def assert_saved_retirement(self, ref: str, tip: str) -> None:
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), f"{tip}\t{ref}")
        self.assertIn(f", ref {ref}, tip {tip}", self.doc.read_text())
        self.assertIn(f"session {DIRECTOR}", self.doc.read_text())
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "branch", "--list", "alpha-branch"), "")
        self.assertEqual(git(self.checkout, "ls-remote", "--heads", "origin", "alpha-branch"), "")

    def test_retirement_saves_unmerged_work_on_origin_before_removal_and_rerun_is_unchanged(self) -> None:
        ref, tip = self.unmerged_work()
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assert_saved_retirement(ref, tip)
        before = (self.doc.read_bytes(), self.doc.stat().st_mtime_ns,
                  self.staff.staff_file.read_bytes(), git(self.checkout, "rev-parse", "HEAD"))
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual((self.doc.read_bytes(), self.doc.stat().st_mtime_ns,
                          self.staff.staff_file.read_bytes(), git(self.checkout, "rev-parse", "HEAD")), before)
        self.assert_saved_retirement(ref, tip)

    def test_failed_saved_ref_push_keeps_the_note_branch_and_worktree(self) -> None:
        ref, tip = self.unmerged_work()
        before = self.doc.read_bytes()
        hook = self.root / "origin.git/hooks/pre-receive"
        _ = hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), "")
        self.assertEqual(self.calls, [])

    def commit_later_work(self, checkout: Path) -> str:
        _ = (checkout / "later.txt").write_text("work committed after preservation\n")
        _ = git(checkout, "add", "later.txt")
        _ = git(checkout, "commit", "-q", "-m", "checkpoint(alpha): later work")
        return git(checkout, "rev-parse", "HEAD")

    def clone_writer(self) -> Path:
        writer = self.root / "late-writer"
        _ = git(self.checkout, "clone", "-q", "--branch", "alpha-branch", str(self.root / "origin.git"), str(writer))
        _ = git(writer, "config", "user.email", "test@example.invalid")
        _ = git(writer, "config", "user.name", "test")
        _ = git(writer, "config", "commit.gpgsign", "false")
        return writer

    def interrupt_before_removal(self) -> None:
        with mock.patch.object(retire_unit, "remove", side_effect=OSError("stop before removal")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("stop before removal", output)
        self.assertTrue(self.worktree.is_dir())
        self.assertIn("retired", self.doc.read_text())

    def test_retirement_preserves_work_committed_during_shutdown(self) -> None:
        ref, first_tip = self.unmerged_work()
        later_tip = ""
        def stop(identity: str) -> stop_resume.Stopped | stop_resume.NotRunning:
            nonlocal later_tip
            if not later_tip:
                self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), f"{first_tip}\t{ref}")
                later_tip = self.commit_later_work(self.worktree)
            return self.stop_session(identity)
        with mock.patch.object(stop_resume, "stop", side_effect=stop):
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertNotEqual(later_tip, first_tip)
        self.assert_saved_retirement(ref, later_tip)
        self.assertEqual(git(self.checkout, "show", f"{later_tip}:later.txt"), "work committed after preservation")

    def test_retirement_refreshes_saved_work_after_the_note_before_removal(self) -> None:
        ref, first_tip = self.unmerged_work()
        mark = retire_unit.mark_retired
        later_tip = ""
        def marked(unit: retire_unit.Unit, director: retire_unit.DirectorSession | retire_unit.NoDirectorSession,
                   saved: retire_unit.SavedBranch | retire_unit.NoSavedBranch = retire_unit.NO_SAVED_BRANCH,
                   *, date: str = "") -> bool:
            nonlocal later_tip
            committed = mark(unit, director, saved, date=date)
            if not later_tip:
                later_tip = self.commit_later_work(self.worktree)
            return committed
        with mock.patch.object(retire_unit, "mark_retired", side_effect=marked):
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertNotEqual(later_tip, first_tip)
        self.assert_saved_retirement(ref, later_tip)

    def test_retirement_retry_preserves_a_new_local_descendant(self) -> None:
        ref, first_tip = self.unmerged_work()
        self.interrupt_before_removal()
        later_tip = self.commit_later_work(self.worktree)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertNotEqual(later_tip, first_tip)
        self.assert_saved_retirement(ref, later_tip)

    def test_retirement_retry_preserves_a_new_origin_descendant(self) -> None:
        ref, first_tip = self.unmerged_work()
        _ = git(self.worktree, "push", "-q", "origin", "alpha-branch")
        self.interrupt_before_removal()
        writer = self.clone_writer()
        later_tip = self.commit_later_work(writer)
        _ = git(writer, "push", "-q", "origin", "alpha-branch")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertNotEqual(later_tip, first_tip)
        self.assert_saved_retirement(ref, later_tip)

    def test_retirement_retry_keeps_a_divergent_branch_and_the_saved_tip(self) -> None:
        ref, first_tip = self.unmerged_work()
        self.interrupt_before_removal()
        _ = git(self.worktree, "checkout", "-q", "-B", "alpha-branch", f"{first_tip}^")
        divergent_tip = self.commit_later_work(self.worktree)
        before = self.doc.read_bytes()
        code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("diverg", output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), divergent_tip)
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), f"{first_tip}\t{ref}")

    def test_unconfirmed_saved_ref_keeps_the_note_branch_and_worktree(self) -> None:
        ref, tip = self.unmerged_work()
        before = self.doc.read_bytes()
        def unconfirmed(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            result = self.fake_run(argv, cwd)
            if "ls-remote" in argv and ref in argv and result.returncode == 0:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return result
        with mock.patch.object(retire_unit, "run", side_effect=unconfirmed):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), f"{tip}\t{ref}")
        self.assertEqual(self.calls, [])

    def test_retire_recovers_a_saved_ref_when_stopped_before_the_note(self) -> None:
        ref, tip = self.unmerged_work()
        before = self.doc.read_bytes()
        with mock.patch.object(retire_unit, "mark_retired", side_effect=OSError("stop before note")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), f"{tip}\t{ref}")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assert_saved_retirement(ref, tip)

    def test_a_finished_berth_reservation_is_released_on_replay(self) -> None:
        unit = retire_unit.find_unit("alpha", "alpha")
        found = retire_unit.Found((), (), (), ("reservation-1",), (), ())
        for stdout, stderr in (("already has a final disposition", ""),
                               ("", "already has a final disposition")):
            with self.subTest(stdout=stdout, stderr=stderr):
                with mock.patch.object(retire_unit, "run", return_value=subprocess.CompletedProcess(
                        ["cargo-berth"], 5, stdout, stderr)):
                    self.assertEqual(retire_unit.stop_rest(unit, found), [])
        with mock.patch.object(retire_unit, "run", return_value=subprocess.CompletedProcess(
                ["cargo-berth"], 5, "", "reservation could not be released")):
            self.assertTrue(any("reservation-1" in line for line in retire_unit.stop_rest(unit, found)))

    def test_final_stop_check_excludes_only_the_units_exact_pane_shell(self) -> None:
        self.claude = []
        pane = PaneShellFixture(self, self.worktree, self.root / "pane-proc", live=False, slug="alpha")
        pane.stopped()
        unit = retire_unit.find_unit("alpha", "alpha")
        self.assertEqual(retire_unit.final_stop_check(unit, frozenset()), [])
        pane.add_occupant(991551, unit="beta-unit")
        left = retire_unit.final_stop_check(unit, frozenset())
        self.assertTrue(any("991551" in line for line in left), left)
        self.assertFalse(any(f"process {pane.shell}," in line for line in left), left)

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
            self.assertEqual(retire_unit.stop_orphan(unit, child.pid, DIRECTOR), [])
        send.assert_called_once_with(child.pid, signal.SIGTERM)
        self.assertIsNone(child.poll(), "the test patches the signal; the child still runs")

    def test_unowned_or_live_sessions_process_is_named_and_never_signalled(self) -> None:
        unit = retire_unit.find_unit("alpha", "alpha")
        for session_id, live_owner in ((SEAT, False), (DIRECTOR, True)):
            with self.subTest(session_id=session_id, live_owner=live_owner):
                child = self.orphan_child(session_id)
                self.claude = [session(DIRECTOR, "alpha", 4101)] if live_owner else []
                with mock.patch.object(process_group, "send_process_group") as send:
                    left = retire_unit.stop_orphan(unit, child.pid, DIRECTOR)
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
            left = retire_unit.stop_orphan(unit, child.pid, DIRECTOR)
        send.assert_not_called()
        self.assertTrue(any("identity changed before signal" in line for line in left), left)

    def test_unowned_cwd_visitor_or_other_units_shell_blocks_without_signals(self) -> None:
        for marked_pane in (False,True):
            for visitor_unit in ("", "beta-unit"):
                with self.subTest(marked_pane=marked_pane, visitor_unit=visitor_unit):
                    self.claude = []
                    self.marked = None
                    unit = retire_unit.find_unit("alpha", "alpha")
                    pane = PaneShellFixture(self, self.worktree, self.root / "visitor-proc", live=False, slug="alpha")
                    pane.add_occupant(991551, unit=visitor_unit, marked=bool(visitor_unit))
                    if not marked_pane:
                        pane.found = None
                    before = self.doc.read_bytes()
                    with (mock.patch.object(retire_unit, "terminate", return_value=[]) as signals,
                          mock.patch.object(retire_unit, "_alive", return_value=True)):
                        remaining = retire_unit.gather(unit)
                        left = retire_unit.stop_rest(unit, remaining)
                    self.assertTrue(any("991551" in line for line in left), left)
                    self.assertFalse(any(991551 in call.args[0] for call in signals.call_args_list), "cwd is no stop authority")
                    self.assertEqual(self.doc.read_bytes(), before)
                    self.assertTrue(self.worktree.is_dir())
                    self.assertEqual(pane.close_calls(), [])
                    with mock.patch.object(retire_unit, "terminate", return_value=[]) as signals:
                        code, output = self.retire("--wait-minutes", "0")
                    self.assertEqual(code, 1, output)
                    self.assertFalse(any(991551 in call.args[0] for call in signals.call_args_list))
                    self.assertEqual(self.doc.read_bytes(), before)
                    self.assertTrue(self.worktree.is_dir())
                    self.assertEqual(pane.close_calls(), [])

    def test_registered_claude_in_the_checkout_is_not_owned_by_cwd(self) -> None:
        visitor = session("33333333-3333-3333-3333-333333333333", "visitor", 991551)
        with (mock.patch.object(sessions, "live_sessions", return_value=[visitor]),
              mock.patch.object(unit_move, "processes_inside", return_value=[991551])):
            self.assertEqual(CLAUDE_SESSIONS(self.worktree, frozenset()), ())

    def test_process_gone_after_owned_cleanup_does_not_remain_a_cwd_blocker(self) -> None:
        unit = retire_unit.find_unit("alpha", "alpha")
        found = retire_unit.Found((), (), (), (), (), (991551,))
        with (mock.patch.object(unit_move, "processes_inside", return_value=[]),
              mock.patch.object(retire_unit, "terminate", return_value=[]) as signals):
            self.assertEqual(retire_unit.stop_rest(unit, found), [])
        self.assertFalse(any(991551 in call.args[0] for call in signals.call_args_list))

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

    def test_dry_run_names_the_unmerged_tip_before_removal_without_saving_it(self) -> None:
        ref, tip = self.unmerged_work()
        before = (self.doc.read_bytes(), self.doc.stat().st_mtime_ns,
                  git(self.checkout, "for-each-ref"), self.staff.staff_file.read_bytes())
        fetch_head = self.checkout / ".git/FETCH_HEAD"
        before_fetch = fetch_head.read_bytes() if fetch_head.exists() else b""

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        lines = output.splitlines()
        save = f"would save: {tip} to origin {ref}"
        self.assertIn(save, lines)
        self.assertLess(lines.index(save), lines.index(f"would remove: worktree {self.worktree}"))
        self.assertLess(lines.index(save), lines.index("would delete: branch alpha-branch here and on origin"))
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), "")
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual((self.doc.read_bytes(), self.doc.stat().st_mtime_ns,
                          git(self.checkout, "for-each-ref"), self.staff.staff_file.read_bytes()), before)
        self.assertEqual(fetch_head.read_bytes() if fetch_head.exists() else b"", before_fetch)
        self.assertEqual(self.stops(), [])

    def test_dry_run_retired_note_includes_the_ref_and_full_tip(self) -> None:
        ref, tip = self.unmerged_work()

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        mark = [line for line in output.splitlines() if line.startswith("would mark:")]
        self.assertEqual(len(mark), 1, output)
        self.assertIn(f", ref {ref}, tip {tip}", mark[0])
        self.assertIn(f"session {DIRECTOR}", mark[0])

    def test_dry_run_saves_an_unfetched_origin_tip_without_changing_local_state(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        ref = f"refs/retired/{row.id}"
        _ = git(self.checkout, "fetch", "-q", "origin")
        writer = self.clone_writer()
        tip = self.commit_later_work(writer)
        _ = git(writer, "push", "-q", "origin", "alpha-branch")
        unseen = subprocess.run(["git", "-C", str(self.checkout), "cat-file", "-e", tip],
                                capture_output=True)
        self.assertNotEqual(unseen.returncode, 0)
        fetch_head = self.checkout / ".git/FETCH_HEAD"
        before = (git(self.checkout, "for-each-ref"), fetch_head.read_bytes(),
                  fetch_head.stat().st_mtime_ns, self.doc.read_bytes(), self.doc.stat().st_mtime_ns,
                  self.staff.staff_file.read_bytes(), self.staff.staff_file.stat().st_mtime_ns,
                  git(self.worktree, "rev-parse", "HEAD"))

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        lines = output.splitlines()
        save = f"would save: {tip} to origin {ref}"
        self.assertIn(save, lines)
        self.assertLess(lines.index(save), lines.index(f"would remove: worktree {self.worktree}"))
        self.assertIn(f", ref {ref}, tip {tip}", output)
        self.assertEqual((git(self.checkout, "for-each-ref"), fetch_head.read_bytes(),
                          fetch_head.stat().st_mtime_ns, self.doc.read_bytes(), self.doc.stat().st_mtime_ns,
                          self.staff.staff_file.read_bytes(), self.staff.staff_file.stat().st_mtime_ns,
                          git(self.worktree, "rev-parse", "HEAD")), before)
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), "")
        self.assertEqual(self.stops(), [])

    def test_dry_run_and_retire_refuse_an_unfetched_divergent_retired_tip(self) -> None:
        ref, tip = self.unmerged_work()
        _ = git(self.checkout, "fetch", "-q", "origin")
        writer = self.clone_writer()
        divergent_tip = self.commit_later_work(writer)
        _ = git(writer, "push", "-q", "origin", f"{divergent_tip}:{ref}")
        unseen = subprocess.run(["git", "-C", str(self.checkout), "cat-file", "-e", divergent_tip],
                                capture_output=True)
        self.assertNotEqual(unseen.returncode, 0)
        fetch_head = self.checkout / ".git/FETCH_HEAD"
        before = (git(self.checkout, "for-each-ref"), fetch_head.read_bytes(),
                  fetch_head.stat().st_mtime_ns, self.doc.read_bytes(), self.doc.stat().st_mtime_ns,
                  self.staff.staff_file.read_bytes(), self.staff.staff_file.stat().st_mtime_ns)

        dry_code, dry_output = self.retire("--dry-run")

        self.assertEqual(dry_code, 1, dry_output)
        refusal = "refused: alpha-branch: branch and retired ref diverge; keep both before retiring"
        self.assertIn(refusal, dry_output.splitlines())
        self.assertEqual((git(self.checkout, "for-each-ref"), fetch_head.read_bytes(),
                          fetch_head.stat().st_mtime_ns, self.doc.read_bytes(), self.doc.stat().st_mtime_ns,
                          self.staff.staff_file.read_bytes(), self.staff.staff_file.stat().st_mtime_ns), before)
        real_code, real_output = self.retire()
        self.assertEqual(real_code, 1, real_output)
        self.assertIn(refusal, real_output.splitlines())
        self.assertEqual(self.doc.read_bytes(), before[3])
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), f"{divergent_tip}\t{ref}")
        self.assertEqual(self.stops(), [])

    def test_dry_run_refuses_a_divergent_retired_ref_like_a_real_retirement(self) -> None:
        ref, tip = self.unmerged_work()
        _ = git(self.worktree, "checkout", "-q", "-B", "alpha-branch", f"{tip}^")
        divergent_tip = self.commit_later_work(self.worktree)
        _ = git(self.worktree, "push", "-q", "origin", f"{divergent_tip}:{ref}")
        _ = git(self.worktree, "checkout", "-q", "-B", "alpha-branch", tip)
        before = self.doc.read_bytes()

        dry_code, dry_output = self.retire("--dry-run")
        real_code, real_output = self.retire()

        self.assertEqual(dry_code, 1, dry_output)
        self.assertEqual(real_code, 1, real_output)
        refusal = "refused: alpha-branch: branch and retired ref diverge; keep both before retiring"
        self.assertIn(refusal, dry_output.splitlines())
        self.assertIn(refusal, real_output.splitlines())
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual(git(self.checkout, "ls-remote", "origin", ref), f"{divergent_tip}\t{ref}")
        self.assertEqual(self.stops(), [])

    def test_dry_run_of_merged_work_has_no_save_or_saved_ref_note(self) -> None:
        before = self.doc.read_bytes()
        tip = git(self.worktree, "rev-parse", "HEAD")

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        self.assertFalse(any(line.startswith("would save:") for line in output.splitlines()), output)
        mark = [line for line in output.splitlines() if line.startswith("would mark:")]
        self.assertEqual(len(mark), 1, output)
        self.assertNotIn(", ref ", mark[0])
        self.assertNotIn(", tip ", mark[0])
        self.assertIn(f"would remove: worktree {self.worktree}", output.splitlines())
        self.assertIn("would delete: branch alpha-branch here and on origin", output.splitlines())
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual(self.stops(), [])

    def test_dry_run_lists_the_seat_the_worker_and_the_reviewer_and_changes_nothing(self) -> None:
        before = self.doc.read_text(encoding="utf-8")

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        for line in (f"would stop: Claude session alpha ({DIRECTOR}, pid 4101), through stop_resume.stop",
                     f"would stop: Claude session alpha-seat ({SEAT}, pid 4102), through stop_resume.stop",
                     f"would stop: app-server pid {os.getpid()} of run run-1; workers alpha-impl",
                     f"would release: build hold of {DIRECTOR}",
                     f"would remove: worktree {self.worktree}",
                     "would delete: branch alpha-branch here and on origin"):
            self.assertIn(line, output.splitlines())
        self.assertEqual(self.stops(), [])
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.doc.read_text(encoding="utf-8"), before)

    def test_a_run_director_working_outside_the_worktree_is_still_stopped(self) -> None:
        director = session(DIRECTOR, "alpha", 4101)
        elsewhere = session("33333333-3333-3333-3333-333333333333", "elsewhere", 4103)
        with (mock.patch.object(retire_unit, "claude_sessions", CLAUDE_SESSIONS),
              mock.patch.object(sessions, "live_sessions", return_value=[director, elsewhere])):
            code, output = self.retire("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would stop: Claude session alpha ({DIRECTOR}, pid 4101), through stop_resume.stop", output.splitlines())
        self.assertNotIn("elsewhere", output)

    def test_worker_cancellation_conflict_prints_a_refusal_without_a_traceback(self) -> None:
        with mock.patch.object(workers, "end_unit", side_effect=workers.WorkerUnavailable(
                "worker ownership changed; rerun apply")):
            code, output = self.retire()
        self.assertEqual(code, 1)
        self.assertIn("refused:", output)
        self.assertIn("worker ownership changed; rerun apply", output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(self.worktree.exists())

    def test_retirement_ends_the_unit_workers_and_reports_quarantine_removal(self) -> None:
        store = workers.UnitWorkerLedger(workers.ledger_path("alpha", "alpha-unit"))
        with store.locked() as ledger:
            ledger.workers["w7"] = workers.UnitWorker("w7", "impl", "codex", "sol", "high", {},
                workers.CodexThread("retiring-thread", str(self.root / "codex")), workers.Paused(), [])
            ledger.quarantined_workers["damaged"] = {"reason": "invalid backend", "record": {}}
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(store.path.exists())
        self.assertIn("w7", output)
        self.assertIn("damaged", output)
        self.assertIn("invalid backend", output)
        self.assertIn(str(store.path), output)

    def test_retirement_forgets_its_pane_launch_without_calling_shutdown_cli(self) -> None:
        self.assertEqual(pane_record.read("alpha")["w1:p1"]["session_id"], DIRECTOR)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(pane_record.read("alpha"), {})
        self.assertFalse(any(call[0] == "shutdown.py" for call in self.calls))

    def test_retires_a_unit_with_a_seat_a_worker_and_a_reviewer(self) -> None:
        code, output = self.retire()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.stops(), [
            ["stop_resume.stop", DIRECTOR],
            ["stop_resume.stop", SEAT],
            ["codex_mesh.py", "stop", "--session-dir", str(self.run_folder)],
            ["build_hold.py", "release", "--holder", DIRECTOR],
        ])
        self.assertIn("alpha-unit retired", output)
        row = next(line for line in self.doc.read_text(encoding="utf-8").splitlines() if line.startswith("| alpha-unit"))
        self.assertRegex(row, r"^\| alpha-unit \| \(retired \d{4}-\d{2}-\d{2}, worktree removed, session "
                         + DIRECTOR + r"\) docs/plans/alpha\.md \|")
        self.assertEqual(git(self.checkout, "log", "-1", "--format=%s"), "production(alpha): retire alpha-unit")
        self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "branch", "--list", "alpha-branch"), "")
        self.assertEqual(git(self.checkout, "ls-remote", "--heads", "origin", "alpha-branch"), "")

    def test_rerun_commits_the_retired_note_before_removing_the_worktree(self) -> None:
        snapshot = Path(self.enterContext(tempfile.TemporaryDirectory())) / "before"
        _ = shutil.copytree(self.root, snapshot)
        staff_record = self.staff.staff_file.read_bytes()
        claude, marked = list(self.claude), self.marked
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        expected_tree = git(self.checkout, "rev-parse", "HEAD^{tree}")
        expected_doc = self.doc.read_text()
        shutil.rmtree(self.root)
        _ = shutil.copytree(snapshot, self.root)
        _ = self.staff.staff_file.write_bytes(staff_record)
        self.claude, self.marked, self.calls = claude, marked, []
        before = git(self.checkout, "rev-parse", "HEAD")

        with mock.patch.object(add_unit, "commit_paths",
                               side_effect=OSError("fixture stop after retired note")) as commit:
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("fixture stop after retired note", output)
        commit.assert_called_once()
        self.assertEqual(self.doc.read_text(), expected_doc)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), before)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(git(self.checkout, "branch", "--list", "--format=%(refname:short)", "alpha-branch"), "alpha-branch")

        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD^{tree}"), expected_tree)
        self.assertEqual(git(self.checkout, "status", "--porcelain"), "")
        self.assertEqual(git(self.checkout, "log", "-1", "--format=%s"), "production(alpha): retire alpha-unit")
        self.assertEqual(self.doc.read_text(), expected_doc)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "branch", "--list", "alpha-branch"), "")
        self.assertEqual(git(self.checkout, "ls-remote", "--heads", "origin", "alpha-branch"), "")
        head = git(self.checkout, "rev-parse", "HEAD")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), head)

    def test_role_cleanup_refusal_keeps_the_unit_row_worktree_and_branch(self) -> None:
        before = self.doc.read_bytes()
        with mock.patch.object(design_roles, "end_holder", side_effect=design_roles.RoleRefused("designer turn is still running")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("held role cleanup", output)
        self.assertIn("designer turn is still running", output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(git(self.checkout, "branch", "--list", "--format=%(refname:short)", "alpha-branch"), "alpha-branch")

    def test_a_damaged_role_held_by_the_unit_refuses_retirement_by_record_and_field(self) -> None:
        design = design_roles.HeldDesign(
            design_roles.DesignPlacement("00000000-0000-4000-8000-000000000071", "natedev", 1),
            "fixture-repo", str(self.worktree), str(self.worktree / "work.txt"), "design",
            "2026-10-31T12:00:00+00:00", design_roles.UnitHolder("alpha", "alpha-unit"),
            design_roles.DesignBoundToPhase(str(self.run_folder), "1", str(self.worktree), ("work.txt",)),
            (design_roles.RoleInstance("designer", "alpha-designer", design_roles.RoleNotOpened()),
             design_roles.RoleInstance("reviewer", "alpha-reviewer", design_roles.RoleNotOpened())))
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        path = damage_binding_repository(design)
        before = self.doc.read_bytes(), path.read_bytes()

        code, output = self.retire()

        self.assertEqual(code, 1, output)
        self.assertIn(f"held role cleanup: {path}: missing or invalid repository", output)
        self.assertEqual((self.doc.read_bytes(), path.read_bytes()), before)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(git(self.checkout, "branch", "--list", "--format=%(refname:short)", "alpha-branch"),
                         "alpha-branch")
    def test_bad_unrelated_role_record_warns_once_and_does_not_stop_retirement(self) -> None:
        bad = write_empty_binding_record(self.checkout, design_roles.UnitHolder('other', 'other-unit'),
                                         lifecycle=design_roles.RoleOpen('unrelated-thread'))
        errors = io.StringIO()
        with redirect_stderr(errors):
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(errors.getvalue().splitlines(), [f'warning: {bad}: missing or invalid repository'])
        self.assertFalse(self.worktree.exists())
        self.assertTrue(bad.is_file())

    def test_bad_live_role_for_this_unit_preserves_the_unit_for_recovery(self) -> None:
        bad = write_empty_binding_record(self.checkout, design_roles.UnitHolder('alpha', 'alpha-unit'),
                                         lifecycle=design_roles.RoleOpen('held-thread'))
        before = self.doc.read_bytes()
        record = bad.read_bytes()
        code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn(f'held role cleanup: {bad}: missing or invalid repository', output)
        self.assertIn('live held role needs recovery', output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(bad.read_bytes(), record)
        self.assertTrue(self.worktree.is_dir())

    def test_a_stopped_director_is_found_by_its_newest_interactive_transcript(self) -> None:
        self.marked = None
        folder = self.config / "projects" / unit_move.project_folder(str(self.worktree))
        folder.mkdir(parents=True, exist_ok=True)
        for name, entrypoint, mtime in (("older", "cli", 100), (DIRECTOR, "cli", 200), (SEAT, "sdk-cli", 300)):
            transcript = folder / f"{name}.jsonl"
            _ = transcript.write_text('{"type": "summary"}\n' + json.dumps({"entrypoint": entrypoint, "cwd": str(self.worktree)}) + "\n",
                                      encoding="utf-8")
            os.utime(transcript, (mtime, mtime))

        code, output = self.retire()

        self.assertEqual(code, 0, output)
        self.assertIn(f"worktree removed, session {DIRECTOR}) docs/plans/alpha.md", self.doc.read_text(encoding="utf-8"))


    def outside_director(self) -> Path:
        (self.run_folder / "director").unlink()
        self.claude = []
        old = self.config / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        old.unlink()
        folder = self.root / "original-checkout"
        self.transcript(folder)
        return folder

    def test_dry_run_finds_a_marked_director_outside_the_worktree_without_run_history(self) -> None:
        folder = self.outside_director()
        code, output = self.retire("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would stop: Claude session alpha ({DIRECTOR}, pid 4101)", output)
        self.assertNotIn("nothing to stop", output)
        self.assertIn(f"session {DIRECTOR}, folder {folder})", output)
        self.assertEqual(self.stops(), [])

    def test_retires_the_marked_director_outside_the_worktree_and_records_its_start_folder(self) -> None:
        folder = self.outside_director()
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertIn(["stop_resume.stop", DIRECTOR], self.calls)
        self.assertEqual(pane_record.read("alpha"), {})
        self.assertIn(f"session {DIRECTOR}, folder {folder})", self.doc.read_text())
        self.assertFalse(self.worktree.exists())

    def test_retired_note_uses_the_folder_holding_a_renamed_directors_transcript(self) -> None:
        folder = self.outside_director()
        transcript = self.config / "projects" / unit_move.project_folder(str(folder)) / f"{DIRECTOR}.jsonl"
        old = self.root / "old-checkout"
        _ = transcript.write_text("\n".join(json.dumps({"cwd": str(cwd)})
                                            for cwd in (old, old, folder, folder)) + "\n")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertIn(f"session {DIRECTOR}, folder {folder})", self.doc.read_text())
        self.assertNotIn(str(old), self.doc.read_text())
        self.assertFalse(self.worktree.exists())

    def test_transcript_without_a_matching_cwd_refuses_retire_before_stopping(self) -> None:
        transcript = self.config / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        _ = transcript.write_text(json.dumps({"cwd": str(self.root / "old-checkout")}) + "\n")
        before = self.doc.read_bytes()
        code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn(f"no cwd matching its project folder {transcript.parent.name}", output)
        self.assertEqual(self.stops(), [])
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.exists())

    def test_final_live_mark_prevents_marking_and_removal_including_an_already_retired_row(self) -> None:
        for retired in (False, True):
            with self.subTest(already_retired=retired):
                if retired:
                    _ = self.doc.write_text(self.doc.read_text().replace("| docs/plans/alpha.md |",
                        f"| (retired 2026-10-28, worktree removed, session {DIRECTOR}) docs/plans/alpha.md |"))
                before = self.doc.read_bytes()
                def return_director(_slug: str, _unit: str) -> None:
                    self.marked = unit_lookup.MarkedUnit("alpha-unit",
                        unit_lookup.LiveClaude("alpha", DIRECTOR, "/nonexistent.sock", 5101), HerdrPlace("alpha", "w1:p1"))
                with mock.patch.object(design_roles, "end_holder", side_effect=return_director):
                    code, output = self.retire()
                self.assertEqual(code, 1, output)
                self.assertIn(f"left running: marked session {DIRECTOR}, pid 5101", output)
                self.assertEqual(output.splitlines()[-1], "alpha-unit is not retired: rerun once these stop")
                self.assertEqual(self.doc.read_bytes(), before)
                self.assertTrue(self.worktree.exists())
                self.assertEqual(git(self.checkout, "branch", "--list", "--format=%(refname:short)", "alpha-branch"), "alpha-branch")
                self.marked = None

    def test_unreadable_mark_refuses_without_stopping_or_removing(self) -> None:
        before = self.doc.read_bytes()
        with mock.patch.object(unit_lookup, "marked_unit", side_effect=OSError("herdr mark unreadable")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("cannot read unit mark: herdr mark unreadable", output)
        self.assertEqual(self.stops(), [])
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.exists())


    def test_failed_forget_keeps_the_unit_and_reports_failure(self) -> None:
        before = self.doc.read_bytes()
        with mock.patch.object(pane_record, "forget_unit", side_effect=OSError("pane entry storage failed")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("pane entry storage failed", output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.exists())

    def test_failed_stop_retains_the_pane_launch_and_unit(self) -> None:
        before = self.doc.read_bytes()
        with mock.patch.object(stop_resume, "stop", return_value=stop_resume.StopFailed(DIRECTOR, "still alive")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("still alive", output)
        self.assertEqual(pane_record.read("alpha")["w1:p1"]["session_id"], DIRECTOR)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.exists())

    def test_final_check_refuses_an_unmarked_session_id_that_reappears(self) -> None:
        before = self.doc.read_bytes()
        def relaunch(_slug: str, _unit: str) -> None:
            self.claude = [session(SEAT, "returned-worker", 5202)]
        with mock.patch.object(design_roles, "end_holder", side_effect=relaunch):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn(f"session {SEAT}, pid 5202", output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.exists())

    def test_final_check_refuses_unreadable_process_evidence(self) -> None:
        before = self.doc.read_bytes()
        with mock.patch.object(sessions, "session_processes",
                               return_value=sessions.SessionLivenessUnknown("process identity unreadable")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn(f"session {DIRECTOR}: process identity unreadable", output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.exists())


    def test_rerun_forgets_the_exited_directors_pane_launch_before_marking(self) -> None:
        folder = self.outside_director()
        with mock.patch.object(pane_record, "forget_unit", side_effect=OSError("pane entry storage failed")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("pane entry storage failed", output)
        self.assertTrue(self.worktree.exists())
        self.assertIsNone(self.marked)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertIn(f"session {DIRECTOR}, folder {folder})", self.doc.read_text())
        self.assertFalse(self.worktree.exists())
        self.assertEqual(len([call for call in self.calls if call == ["stop_resume.stop", DIRECTOR]]), 1)
        self.assertEqual(pane_record.read("alpha"), {})

if __name__ == "__main__":
    _ = unittest.main()
