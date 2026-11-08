"""Test the agents CLI with private staff rows and temporary repositories."""
from __future__ import annotations

import io
import json
import math
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, MutableMapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from multiprocessing.connection import Connection
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.agents import worker_lifecycle
from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment, fake_remote_removal, publish_mirror
from scripts.copies.remote_test_support import local_remote_transport
from scripts.direct import workers
from scripts.herdr import herdr, worker_panes
from scripts.herdr.herdr import HerdrPlace
from scripts.lib import process_group, atomic_file, conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.lib.conf import UseConfiguredValue
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import sessions, sessions as retirement_sessions
from scripts.message.sessions import SessionRecord
from scripts.production import unit_lookup, unit_move, staff, unit_config, add_unit, completion_receipt, worker
from scripts.production.production_doc import Production, read_production
from scripts.production.staff import StaffId
from scripts.production.staff_test_support import (
    foreign_hostname,
    fixture_member,
    fixture_worker,
    StaffTestCase,
    fixture_runtime_id,
    CHECKOUT,
    unit_membership,
)
from scripts.shutdown import shutdown, remote, stop_resume, restart, pane_record as staff_pane_record, pane_record


class AgentsUsageTests(unittest.TestCase):
    def test_status_excludes_a_retired_unit_whose_process_still_runs(self) -> None:
        from scripts.shutdown.test_inventory import InventoryTests, TARGET_LOGIN, UNIT_ID, DESKTOP_ID
        from scripts.whoami.account import Account

        fixture = InventoryTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        row = staff.snapshot().by_current_session(UNIT_ID)
        assert row is not None and isinstance(row.worktree, staff.PresentWorktree)
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(row.worktree.machine, row.worktree.path),
                               expected=row.worktree)
        with mock.patch.object(shutdown, "_selected_account", return_value=Account("claude", TARGET_LOGIN, "claude2")):
            code, output, errors = self.invoke(["status", "--here", "--json"])
            self.assertEqual(code, 0, errors)
            report = cast(dict[str, object], json.loads(output))
            observed = cast(list[dict[str, object]], report["sessions"])
            self.assertNotIn(UNIT_ID, {item["session_id"] for item in observed})
            self.assertIn(DESKTOP_ID, {item["session_id"] for item in observed})
            code, output, errors = self.invoke(["status", "--here"])
            self.assertEqual(code, 0, errors)
            self.assertNotRegex(output, rf"(?m)^  \S+ {re.escape(row.name)} · ")
        self.assertIn(UNIT_ID, {item["sessionId"] for item in sessions.live_sessions()})

    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                status = shutdown.main(arguments)
            except SystemExit as error:
                self.assertIsInstance(error.code, int)
                status = int(str(error.code))
        return status, output.getvalue(), errors.getvalue()

    def test_no_arguments_print_the_help_usage_and_exit_zero(self) -> None:
        empty = self.invoke([])
        self.assertEqual(empty[0], 0)
        self.assertEqual(empty, self.invoke(["help"]))
        self.assertTrue(empty[1].startswith("usage: agents"))
        self.assertEqual(empty[2], "")

    def test_help_lists_only_public_commands_with_their_arguments(self) -> None:
        status, output, errors = self.invoke(["help"])
        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        self.assertEqual([line.split()[1] for line in output.splitlines()[1:]],
                         ["help", "status", "pause", "exit", "pause", "resume", "resume", "retire", "restart", "stop"])
        self.assertIn("resume NAME", output)
        self.assertIn("stop SESSION", output)
        self.assertIn("[ACCOUNT]", output)
        self.assertIn("--dry-run", output)
        for command in ("records", "held-sessions", "remote-control"):
            self.assertNotIn(command, output)

    def test_help_option_prints_the_same_public_usage(self) -> None:
        option = self.invoke(["--help"])
        self.assertEqual(option[0], 0)
        self.assertTrue(option[1].startswith("usage: agents"))
        self.assertEqual(option, self.invoke(["help"]))

    def test_unknown_command_prints_usage_and_exits_two(self) -> None:
        status, output, errors = self.invoke(["unknown-command"])
        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        usage = self.invoke(["help"])[1]
        self.assertTrue(usage.startswith("usage: agents"))
        self.assertIn(usage, errors)


DIRECTOR = "11111111-1111-1111-1111-111111111111"

RUN_COMMAND = shutdown.retirement_run

SEAT = "22222222-2222-2222-2222-222222222222"

STOP_SESSION = stop_resume.stop

def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))

def git(where: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(where), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()

def session(session_id: str, name: str, pid: int) -> SessionRecord:
    return SessionRecord(pid=pid, sessionId=session_id, name=name, messagingSocketPath="/nonexistent.sock",
                         updatedAt=0, tmux="", formerNames=[])

@final
class RetireUnitTests(unittest.TestCase):
    def test_retirement_retains_the_identity_and_excludes_it_from_active_views(self) -> None:
        from scripts.direct import board_json
        from scripts.production import session_staff, showrunners

        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        record = staff.snapshot()
        self.assertEqual(record.by_id(row.id), replace(row,
            worktree=staff.RemovedWorktree(remote.local_machine(), self.worktree)))
        self.assertNotIn(row.id, {item.id for item in staff.active(record, "unit")})
        self.assertEqual(session_staff.production_members(record)["alpha"], ())
        runner: showrunners.Showrunner = {"name": "alpha", "doc": str(self.doc),
            "zone": "America/New_York", "session": "", "socket": ""}
        self.assertEqual(board_json.build(runner, record=record, pane_rows={})["units"], [])


    def test_retirement_continues_after_a_member_stop_raises(self) -> None:
        with (mock.patch.object(stop_resume, "stop", side_effect=[OSError("stop read failed"), stop_resume.NotRunning(SEAT)]) as stops,
              mock.patch.object(shutdown, "retirement_refusals", return_value=[]),
              mock.patch.object(shutdown, "retirement_remove", return_value=[]) as remove):
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
              mock.patch.object(shutdown, "retirement_alive", return_value=False)):
            left = shutdown.retirement_terminate([1234], {1234: "fixture"}, settings=UseConfiguredValue.CURRENT)
        self.assertEqual(left, [])
        send.assert_called_once_with(1234, signal.SIGTERM)
        reads.assert_called_once_with("production")

    def test_sigterm_wait_reads_the_retirement_settings_once(self) -> None:
        with (mock.patch.object(conf, "read", wraps=conf.read) as reads,
              mock.patch.object(os, "kill") as send,
              mock.patch.object(shutdown, "retirement_alive", return_value=False)):
            self.assertEqual(shutdown.retirement_terminate([1234], {1234: "fixture"}), [])
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
              mock.patch.object(shutdown, "retirement_alive", return_value=True),
              worker_lifecycle.operation_deadline(deadline=100.25)):
            with self.assertRaises(worker_lifecycle.OperationDeadlineExpired):
                _ = shutdown.retirement_terminate([1234], {1234: "fixture"})
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
                          mock.patch.object(shutdown, "retirement_alive", return_value=True),
                          mock.patch.object(time, "monotonic", side_effect=[0, 0, wait - 0.1, wait]),
                          mock.patch.object(time, "sleep") as sleep):
                        left = shutdown.retirement_terminate([1234], {1234: "fixture"})
                    send_signal.assert_called_once_with(1234, signal.SIGTERM)
                    self.assertEqual(len(sleep.call_args_list), 2)
                    self.assertEqual(sleep.call_args_list[0].args, (poll,))
                    self.assertAlmostEqual(cast(float, sleep.call_args_list[1].args[0]), 0.1)
                    self.assertEqual(left, ["fixture: still runs after SIGTERM"])

    def test_an_explicit_shorter_wait_caps_the_configured_term_wait(self) -> None:
        path = self.limits(poll="1", wait="10")
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(os, "kill"),
              mock.patch.object(shutdown, "retirement_alive", return_value=True),
              mock.patch.object(time, "monotonic", side_effect=[0, 0, 0.1]),
              mock.patch.object(time, "sleep") as sleep):
            _ = shutdown.retirement_terminate([1234], {1234: "fixture"}, wait=0.1)
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
        _ = self.enterContext(mock.patch.object(shutdown, "MAC_CLAIMS", self.root / "claims"))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_production_of", production_of))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_run", self.fake_run))
        _ = self.enterContext(mock.patch.object(stop_resume, "stop", self.stop_session))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", nothing_inside))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_reservations", no_reservations))
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
            code = shutdown.main(["retire", str(self.unit_staff_id), *arguments])
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
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        for stdout, stderr in (("already has a final disposition", ""),
                               ("", "already has a final disposition")):
            with self.subTest(stdout=stdout, stderr=stderr):
                output = io.StringIO()
                with (mock.patch.object(shutdown, "retirement_reservations", return_value=("reservation-1",)),
                      mock.patch.object(shutdown, "retirement_run", return_value=subprocess.CompletedProcess(
                          ["cargo-berth"], 5, stdout, stderr)), redirect_stdout(output)):
                    shutdown.retirement_release_reservations(unit, settings=conf.read("production"))
                self.assertEqual(output.getvalue(), "")
        output = io.StringIO()
        with (mock.patch.object(shutdown, "retirement_reservations", return_value=("reservation-1",)),
              mock.patch.object(shutdown, "retirement_run", return_value=subprocess.CompletedProcess(
                  ["cargo-berth"], 5, "", "reservation could not be released")), redirect_stdout(output)):
            shutdown.retirement_release_reservations(unit, settings=conf.read("production"))
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
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        with (mock.patch.object(process_group, "send_process_group") as send,
              mock.patch.object(shutdown, "retirement_alive", return_value=False)):
            self.assertEqual(shutdown.retirement_stop_orphan(unit, child.pid, DIRECTOR, settings=conf.read("production")), [])
        send.assert_called_once_with(child.pid, signal.SIGTERM)
        self.assertIsNone(child.poll(), "the test patches the signal; the child still runs")

    def test_unowned_or_live_sessions_process_is_named_and_never_signalled(self) -> None:
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        for session_id, live_owner in ((SEAT, False), (DIRECTOR, True)):
            with self.subTest(session_id=session_id, live_owner=live_owner):
                child = self.orphan_child(session_id)
                self.claude = [session(DIRECTOR, "alpha", 4101)] if live_owner else []
                with mock.patch.object(process_group, "send_process_group") as send:
                    left = shutdown.retirement_stop_orphan(unit, child.pid, DIRECTOR, settings=conf.read("production"))
                send.assert_not_called()
                self.assertEqual(len(left), 1, left)
                for detail in (f"unowned process {child.pid}, parent ", "command ", "start ", str(self.worktree)):
                    self.assertIn(detail, left[0])

    def test_orphan_start_time_change_before_signal_refuses_without_sending(self) -> None:
        child = self.orphan_child(DIRECTOR)
        self.claude = []
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        birth = shutdown.retirement_proc_start(child.pid)
        with (mock.patch.object(shutdown, "retirement_proc_start", side_effect=[birth, birth, birth, "reused"]),
              mock.patch.object(process_group, "send_process_group") as send):
            left = shutdown.retirement_stop_orphan(unit, child.pid, DIRECTOR, settings=conf.read("production"))
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

        unit = shutdown.retirement_find_unit("alpha", "alpha")
        self.assertEqual(shutdown.retirement_refusals(unit, settings=conf.read("production")), [])
        # A line a person wrote beside it is still uncommitted work.
        _ = plan.write_text(recorded + "a note\n", encoding="utf-8")
        code, output = self.retire()
        self.assertEqual(code, 1)
        self.assertIn(f"refused: uncommitted work in {self.worktree}: work.txt", output)


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

@final
class ResumeUnitTests(unittest.TestCase):
    def test_retired_resume_passes_the_selected_account_to_the_launcher(self) -> None:
        code, output = self.resume("--to", "claude3")
        self.assertEqual(code, 0, output)
        command = self.calls[-1]
        self.assertIn("--account", command)
        self.assertEqual(command[command.index("--account") + 1], "claude3")

    def test_resume_of_a_retired_unit_requires_the_retired_option_before_dispatch(self) -> None:
        row = unit_membership("alpha", "alpha")
        before = self.staff.staff_file.read_bytes(), self.parked_facts().read_bytes()
        for name in (row.name, str(row.id), row.current_session):
            with self.subTest(name=name), mock.patch.object(stop_resume, "resume") as start:
                output = io.StringIO()
                with redirect_stderr(output):
                    code = shutdown.main(["resume", name])
                self.assertEqual(code, 2)
                self.assertIn(f"agents resume {row.name} --retired", output.getvalue())
                start.assert_not_called()
        self.assertEqual((self.staff.staff_file.read_bytes(), self.parked_facts().read_bytes()), before)
        self.assertEqual(self.calls, [])

    def test_resume_restores_retained_workers(self) -> None:
        from scripts.production import worker

        with mock.patch.object(worker, "restore", return_value=worker.ExitCode.OK) as restore:
            code, output = self.resume()
        self.assertEqual(code, 0, output)
        restore.assert_called_once()

    def test_uncertain_worker_restore_keeps_the_director_resume_complete(self) -> None:
        row = unit_membership("alpha", "alpha")
        with mock.patch.object(worker, "restore", side_effect=[
                worker.ExitCode.UNCERTAIN, worker.ExitCode.OK]) as restore:
            code, output = self.resume()
            self.assertEqual(code, 8, output)
            self.assertIn("alpha resumed", output)
            self.assertFalse(unit_config.parked_path(row.id).exists())
            unit = shutdown.recovery_find_unit("alpha", "alpha")
            self.assertIsInstance(unit, shutdown.NoRecoveryRequired)
            receipt = completion_receipt.read_receipt(
                shutdown.recovery_resume_record(unit.production, unit.name).with_suffix(".done"), staff.snapshot())
            self.assertIsInstance(receipt, completion_receipt.OwnedReceipt)
            assert isinstance(receipt, completion_receipt.OwnedReceipt)
            self.assertEqual(receipt.owner.id, row.id)
            self.assertEqual(receipt.fields["session_id"], row.current_session)
            code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertEqual(output, "alpha already resumed\n")
        self.assertEqual(restore.call_args_list, [mock.call(row.id), mock.call(row.id)])
        self.assertEqual(len(self.calls), 1)


    def test_resume_restores_the_staff_location(self) -> None:
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertTrue((self.worktree / ".git").is_file())
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        row = unit_membership("alpha", "alpha")
        self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), self.worktree))
        self.assertIsInstance(shutdown.recovery_find_unit("alpha", "alpha"), shutdown.NoRecoveryRequired)


    staff: StaffTestCase = StaffTestCase()
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()
    configs: list[Path] = []
    calls: list[list[str]] = []
    launch_code: int = 0

    def test_resume_command_uses_the_row_name_and_current_native_session(self) -> None:
        row = unit_membership("alpha", "alpha")
        self.assertNotEqual(row.id, row.current_session)
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        self.assertEqual(unit.branch, row.name)
        command = shutdown.recovery_launch_command(replace(unit, name="stale-table"))
        self.assertEqual(command[command.index("--production") + 2], row.name)
        self.assertEqual(command[command.index("--resume") + 1], row.current_session)
        self.assertNotIn("STAFF_ID", " ".join(command))

    def test_recovery_refuses_an_empty_session_id(self) -> None:
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        with self.assertRaisesRegex(shutdown.RecoveryRefused, "^alpha: recovery requires a session id$"):
            _ = replace(unit, session_id=" ")

    def test_recovery_requires_each_retirement_fact(self) -> None:
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        for fact in ("branch", "session_id", "plan"):
            fields = dict(vars(unit))
            del fields[fact]
            with self.subTest(fact=fact), self.assertRaises(TypeError):
                _ = cast(Callable[..., object], shutdown.RetiredUnitRecovery)(**fields)

    def test_neither_config_refuses_with_the_backfill_instruction_before_any_change(self) -> None:
        _ = self.staff.record_production("alpha", self.doc)
        row = unit_membership("alpha", "alpha")
        row = staff.update_current_session(row.current_session, staff.StaffId(DIRECTOR))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        unit_config.discard_parked(row.id)
        before = self.doc.read_bytes(), self.staff.staff_file.read_bytes(), git(self.checkout, "rev-parse", "HEAD")
        for flags in ((),):
            with self.subTest(flags=flags):
                code, output = self.resume(*flags)
                self.assertEqual(code, 1, output)
                self.assertEqual(output, f"alpha: no .unit.toml in {self.worktree} or "
                                 + f"~/.local/state/staff/units/{row.id}.toml; ask the showrunner\n")
                self.assertFalse(self.worktree.exists())
                self.assertEqual(unit_config.place(row.id, self.worktree), unit_config.Nowhere())
                self.assertEqual(self.calls, [])
                self.assertEqual((self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                                  git(self.checkout, "rev-parse", "HEAD")), before)


    @override
    def setUp(self) -> None:
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        for name in ("shutdown", "production", "direct"):
            write_text_atomic(self.root / f"{name}.conf", (CHECKOUT / f"config/{name}.conf").read_text())
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
        _ = self.doc.write_text("# Production\n\n- **Merge branch:** main\n"
                                + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n", encoding="utf-8")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "production")
        _ = git(self.checkout, "push", "-q", "origin", "main")
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.root / "pane-launches"))
        self.configs = [self.root / "claude1", self.root / "claude2"]
        self.transcript(self.configs[0])

        self.calls = []
        self.launch_code = 0
        _ = self.staff.record_production("alpha", self.doc)
        production = read_production("alpha")

        def production_of(_production_name: str, *, record: staff.StaffSnapshot) -> Production:
            del record
            return production

        def not_marked(_production_name: str, _name: str) -> unit_lookup.LabeledSession | None:
            # The real pane writer retains a fixed owner; the live process resumes
            # that owner's current conversation, which can differ after /clear.
            owner = staff.by_current_session(DIRECTOR)
            entries = [entry for entry in pane_record.launches()
                       if owner is not None and entry["session_id"] == owner.id]
            return (unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123),
                                          HerdrPlace("agents", entries[-1]["pane_id"]))
                    if entries else None)

        _ = self.enterContext(mock.patch.object(shutdown, "retirement_production_of", production_of))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_run", self.fake_run))
        _ = self.enterContext(mock.patch.object(unit_lookup, "labeled_session", not_marked))
        _ = self.enterContext(mock.patch.object(sessions, "config_dirs", lambda: self.configs))
        _ = self.enterContext(mock.patch.object(retirement_sessions, "config_dirs", lambda: self.configs))
        _ = cast(MutableMapping[str, str], self.enterContext(
            mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                                       "HERDR_PANE_RECORD_DIR": str(self.root / "pane-launches")})))
        _ = self.staff.record_production("alpha", self.doc)
        row = unit_membership("alpha", "alpha")
        row = staff.update_current_session(row.current_session, staff.StaffId(DIRECTOR))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        self.retained_config()

    def retained_config(self, *, plan: str = "docs/plans/alpha.md") -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        unit_config.discard_parked(row.id)
        _ = unit_config.write_parked(row.id, unit_config.UnitConfig(unit_config.PlanFile(Path(plan)),
            unit_config.AppPort(1), ("scripts",),
            unit_config.RetiredSession("2026-10-20", row.current_session, unit_config.Branch(row.name))))

    def transcript(self, config: Path, cwd: Path | None = None) -> None:
        cwd = self.worktree if cwd is None else cwd
        folder = config / "projects" / unit_move.project_folder(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / f"{DIRECTOR}.jsonl").write_text(json.dumps({"entrypoint": "cli", "cwd": str(cwd)}) + "\n", encoding="utf-8")

    def fake_run(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Git runs for real; add_unit.py is recorded and answered."""
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        self.calls.append([Path(argv[1]).name, *argv[2:]])
        if self.launch_code == 0:
            pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
                {}, ["claude", "--resume", DIRECTOR]))
        return subprocess.CompletedProcess(argv, self.launch_code, "", "launch failed" if self.launch_code else "")

    def resume(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = shutdown.main(["resume", "alpha", "--retired", *arguments])
        return code, output.getvalue()

    def assert_resumed(self, code: int, output: str) -> None:
        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls[-1], ["add_unit.py", "--production", "alpha", "alpha", "--resume", DIRECTOR,
                                          "--cwd", str(self.worktree), "--plan", "docs/plans/alpha.md"])
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), git(self.checkout, "rev-parse", "main"))
        self.assertIn("alpha resumed", output)

    def test_retired_resume_rerun(self) -> None:
        _ = self.staff.record_production("alpha", self.doc)
        row = unit_membership("alpha", "alpha")
        row = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        saved = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
            unit_config.AppPort(1), ("scripts",),
            unit_config.RetiredSession("2026-10-20", DIRECTOR, unit_config.Branch("alpha")))
        _ = unit_config.write_parked(row.id, saved)
        parked = unit_config.parked_path(row.id)
        retained = parked.read_bytes(), parked.stat().st_mtime_ns
        original = staff.restore_unit
        for after in (False, True):
            def interrupted(identity: staff.StaffId,
                            worktree: staff.PresentWorktree) -> staff.StaffRow:
                if after:
                    _ = original(identity, worktree)
                raise RuntimeError("stopped at staff step")
            with mock.patch.object(staff, "restore_unit", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "stopped at staff step"):
                    _ = self.resume()
            self.assertTrue(self.worktree.is_dir())
            self.assertEqual(unit_config.read(self.worktree),
                             unit_config.UnitConfig(saved.plan, saved.port, saved.responsibilities))
            self.assertEqual((parked.read_bytes(), parked.stat().st_mtime_ns), retained)
            self.assertEqual(self.calls, [])
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        rows = staff.find(kind="unit")
        self.assertEqual([row.name for row in rows], ["alpha"])
        data, document = self.staff.staff_file.read_bytes(), self.doc.read_bytes()
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertIn("already resumed", output)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.doc.read_bytes()), (data, document))
        self.assertEqual(len(self.calls), 1)

    def test_retire_then_resume_reuses_the_staff_identity_at_the_add_unit_boundary(self) -> None:
        retirement = RetireUnitTests()
        self.addCleanup(retirement.doCleanups)
        retirement.setUp()
        _ = self.enterContext(mock.patch.object(sessions, "config_dirs", lambda: [retirement.config]))
        unit = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        tip = git(retirement.checkout, "rev-parse", "main")
        code, output = retirement.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree(remote.local_machine(), retirement.worktree)))
        restore = self.enterContext(mock.patch.object(
            worker, "restore", return_value=worker.ExitCode.OK))
        launched_ids: list[staff.StaffId] = []
        commands: list[list[str]] = []

        def launch(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            if len(argv) > 1 and Path(argv[1]).name == "add_unit.py":
                commands.append(argv)
                production = read_production(argv[argv.index("--production") + 1])
                row = add_unit.ensure_staff_unit(production, argv[4])
                launched_ids.append(row.id)
                retirement.marked = unit_lookup.LabeledSession("alpha",
                    unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123), HerdrPlace("agents", "w1:p1"))
                pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(retirement.worktree),
                    {}, ["claude", "--resume", DIRECTOR]))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return retirement.fake_run(argv, cwd)

        resumed = io.StringIO()
        with mock.patch.object(shutdown, "retirement_run", side_effect=launch), redirect_stdout(resumed):
            code = shutdown.main(["resume", "alpha", "--retired"])
        self.assertEqual(code, 0, resumed.getvalue())
        restore.assert_called_once_with(unit.id)
        self.assertEqual(launched_ids, [unit.id])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][commands[0].index("--resume") + 1], DIRECTOR)
        self.assertEqual(staff.by_id(unit.id), unit)
        assert isinstance(unit.parent, staff.ReportsTo)
        self.assertEqual(staff.find(kind="unit", parent=unit.parent.id), [unit])
        self.assertEqual(git(retirement.worktree, "rev-parse", "HEAD"), tip)
        before = (retirement.staff.staff_file.read_bytes(), retirement.doc.read_bytes(),
                  git(retirement.checkout, "for-each-ref"))
        with mock.patch.object(shutdown, "retirement_run", side_effect=launch), redirect_stdout(io.StringIO()):
            code = shutdown.main(["resume", "alpha", "--retired"])
        self.assertEqual(code, 0)
        self.assertEqual(restore.call_args_list, [mock.call(unit.id), mock.call(unit.id)])
        self.assertEqual(len(commands), 1)
        self.assertEqual((retirement.staff.staff_file.read_bytes(), retirement.doc.read_bytes(),
                          git(retirement.checkout, "for-each-ref")), before)


    def parked_facts(self) -> Path:
        return unit_config.parked_path(next(row for row in staff.find(kind="unit") if row.name == "alpha").id)


    def test_successful_launch_keeps_parked_facts_until_its_staff_pane_is_proven(self) -> None:
        row = staff.find(kind="unit")[0]
        saved = unit_config.parked_path(row.id).read_bytes()
        document = self.doc.read_bytes()
        with mock.patch.object(shutdown, "recovery_resumed_session_proven", return_value=False):
            code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("resumed native session identity is not proven", output)
        self.assertEqual(unit_config.parked_path(row.id).read_bytes(), saved)
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(len(self.calls), 1)
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(unit_config.parked_path(row.id).exists())

    def test_resume_proof_requires_the_exact_pane_owner_and_current_conversation(self) -> None:
        row = staff.find(kind="unit")[0]
        pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
            {}, ["claude", "--resume", DIRECTOR]))
        before = (self.staff.staff_file.read_bytes(), pane_record.record_path("agents").read_bytes(),
                  unit_config.parked_path(row.id).read_bytes())
        with self.subTest(evidence="fixed owner for the live current conversation"):
            self.assertTrue(shutdown.recovery_resumed_session_proven("alpha", "alpha", DIRECTOR, row))
        current = unit_lookup.LabeledSession("alpha",
            unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123), HerdrPlace("agents", "w1:p1"))
        other_session = fixture_runtime_id("unrelated-resume-conversation")
        for marked, expected_session in (
            (replace(current, place=HerdrPlace("agents", "w1:other")), DIRECTOR),
            (replace(current, claude=unit_lookup.LiveClaude("alpha", other_session, "fixture", 123)), DIRECTOR),
            (current, row.id),
        ):
            with self.subTest(marked=marked, expected_session=expected_session), mock.patch.object(
                    unit_lookup, "labeled_session", return_value=marked):
                self.assertFalse(shutdown.recovery_resumed_session_proven("alpha", "alpha", expected_session, row))
        self.assertEqual((self.staff.staff_file.read_bytes(), pane_record.record_path("agents").read_bytes(),
                          unit_config.parked_path(row.id).read_bytes()), before)

    def test_retired_unit_receipt_cannot_complete_a_new_incarnation(self) -> None:
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        unit = staff.find(kind="unit")[0]
        self.staff.forget_rows(unit.id)
        replacement = fixture_member("unit", name=unit.name, parent=unit.parent,
                                     runtime_id=fixture_runtime_id("replacement-unit-backend"))
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertNotIn("already resumed", output)
        self.assertEqual(staff.by_id(replacement.id), replacement)
        self.assertEqual(len(self.calls), 1)

    def test_resume_child_joins_the_operation_until_the_parked_facts_are_discarded(self) -> None:
        source = self.staff.production("alpha", "alpha")
        source.unlink()
        source.symlink_to(self.doc)
        _ = self.staff.record_production("alpha", source)
        row = unit_membership("alpha", "alpha")
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        wrapper = self.root / "resume-launch.py"
        # The inner launcher boundary supplies no live pane. Its public main and
        # staff step are real, including the inherited operation lock.
        write_text_atomic(wrapper, "\n".join([
            "import sys", "from unittest.mock import patch",
            f"sys.path.insert(0, {str(CHECKOUT / 'scripts/production')!r})",
            f"sys.path.insert(0, {str(CHECKOUT)!r})",
            "from scripts.production import add_unit", "from scripts.production.production_doc import read_production",
            "def launch(arguments):",
            "    name = arguments[arguments.index('--production') + 1]",
            "    row = add_unit.ensure_staff_unit(read_production(name), arguments[2])",
            "    import pane_record",
            f"    pane_record.write('agents', pane_record.launch('w1:p1', {DIRECTOR!r}, {str(self.worktree)!r}, {{}}, ['claude']))",
            "    print(row.id)", "    return 0",
            "with patch.object(add_unit, '_main', side_effect=launch):",
            "    raise SystemExit(add_unit.main(sys.argv[1:]))", "",
        ]))
        context = multiprocessing.get_context("fork")
        first, second = context.Pipe()
        parent = cast(Connection, cast(object, first))
        child = cast(Connection, cast(object, second))
        original_discard = unit_config.discard_parked
        def finish(unit: staff.StaffId) -> None:
            child.send((staff.snapshot().find(kind="unit")[0].id, bool(staff.operation_fds())))
            _ = cast(object, child.recv())
            original_discard(unit)
        def run() -> None:
            parent.close()
            try:
                with mock.patch.object(shutdown, "retirement_run", RUN_COMMAND), mock.patch.object(
                        shutdown, "ADD_UNIT", wrapper), mock.patch.object(unit_config, "discard_parked", side_effect=finish):
                    child.send(self.resume())
            finally:
                child.close()
        process = context.Process(target=run)
        writer: subprocess.Popen[str] | None = None
        try:
            with self.staff.lock_notice(self.staff.staff_file.parent / "operation.lock") as listener:
                process.start()
                child.close()
                self.assertTrue(parent.poll(TEST_HANG_GUARD_SECS), "nested add_unit did not join resume's lock")
                identity, lock_held = cast(tuple[str, bool], parent.recv())
                self.assertIsInstance(identity, str, f"resume exited before discard: {(identity, lock_held)!r}")
                self.assertIsInstance(lock_held, bool)
                staff_id = staff.StaffId(identity)
                self.assertTrue(lock_held, "resume released its operation lock before discarding the parked facts")
                self.assertTrue(unit_config.parked_path(staff_id).exists())
                writer = self.staff.start_script(CHECKOUT / "scripts/production/staff.py", "rename", identity, "--name", "alpha")
                self.staff.assert_operation_blocks(writer, listener)
                parent.send("finish")
                self.assertTrue(parent.poll(TEST_HANG_GUARD_SECS), "resume did not discard the parked facts")
                code, output = cast(tuple[int, str], parent.recv())
                self.assertEqual(code, 0, output)
                process.join(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.exitcode, 0)
                output, errors = writer.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(writer.returncode, 0, output + errors)
            self.assertFalse(unit_config.parked_path(staff_id).exists())
            row = staff.by_id(staff_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual((row.kind, row.name), ("unit", "alpha"))
            self.assertEqual(staff.find(kind="unit"), [row])
        finally:
            parent.close()
            child.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=TEST_HANG_GUARD_SECS)
            process.close()
            if writer is not None and writer.returncode is None:
                writer.kill()
                _ = writer.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_resumes_the_recorded_session_in_a_worktree_made_from_the_merge_branch(self) -> None:
        self.assert_resumed(*self.resume())

    def test_retired_as_built_without_a_header_is_passed_to_the_resume_launch(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n\nThe plan is complete.\n", encoding="utf-8")
        self.retained_config(plan=plan)
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "condensed plan")

        code, output = self.resume()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls, [["add_unit.py", "--production", "alpha", "alpha",
                                      "--resume", DIRECTOR, "--cwd", str(self.worktree), "--plan", plan]])

    def test_a_transcript_under_another_account_resumes(self) -> None:
        # The units account moved since the retire: the transcript is under the other config dir.
        self.configs.reverse()
        self.assert_resumed(*self.resume())

    def test_a_failed_launch_keeps_the_parked_facts_and_a_rerun_resumes(self) -> None:
        document = self.doc.read_bytes()
        head = git(self.checkout, "rev-parse", "HEAD")
        self.launch_code = 1
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("refused: add_unit.py --resume: launch failed", output)
        self.assertTrue(unit_config.parked_path(staff.find(kind="unit")[0].id).exists())
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        self.assertEqual(len(self.calls), 1)
        worktree_head = git(self.worktree, "rev-parse", "HEAD")
        self.launch_code = 0
        code, output = self.resume()
        self.assert_resumed(code, output)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), worktree_head)
        self.assertNotIn("made: worktree", output)

    def test_transcript_and_branch_refusals_change_nothing(self) -> None:
        self.configs = [self.root / "claude3"]
        _ = git(self.checkout, "branch", "alpha", "main")
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn(f"refused: alpha: no account has the transcript of session {DIRECTOR}", output)
        self.assertIn("refused: branch alpha still exists; retire did not finish", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "log", "-1", "--format=%s"), "production")


    def test_missing_session_refuses_launch_when_no_evidence_exists(self) -> None:
        self.configs = [self.root / "empty-config"]
        unit_config.discard_parked(staff.find(kind="unit")[0].id)
        before = self.doc.read_bytes()
        for flags in ((),):
            with self.subTest(flags=flags):
                code, output = self.resume(*flags)
                self.assertEqual(code, 1, output)
                self.assertIn("; ask the showrunner", output)
                self.assertNotIn("would resume:", output)
                self.assertNotIn("resumed:", output)
                self.assertEqual(self.calls, [])
                self.assertFalse(self.worktree.exists())
                self.assertEqual(self.doc.read_bytes(), before)

    def test_parked_session_survives_newer_worker_transcripts(self) -> None:
        older = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / "older.jsonl"
        _ = older.write_text(json.dumps({"entrypoint": "cli", "cwd": str(self.worktree)}) + "\n")
        os.utime(older, (1, 1))
        worker = older.with_name("worker.jsonl")
        _ = worker.write_text(json.dumps({"entrypoint": "sdk-py", "cwd": str(self.worktree)}) + "\n")
        self.assert_resumed(*self.resume())

    def test_parked_session_survives_stale_retained_pane_launches(self) -> None:
        for pane, identity, at in (("w1:p1", "older", "2026-10-27T23:00:00+00:00"),
                                    ("w1:p2", DIRECTOR, "2026-10-28T23:00:00+00:00")):
            launch = pane_record.launch(pane, identity, str(self.worktree),
                {('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha", "CLAUDE_CONFIG_DIR": str(self.configs[0])}, ["--resume", identity])
            launch["written_at"] = at
            pane_record.write("agents", launch)
        current_backend = unit_lookup.labeled_session

        def observed_backend(production_name: str, name: str) -> unit_lookup.LabeledSession | None:
            # Retained pane files are historical evidence. Only the fake launch
            # establishes a live backend for this test.
            return current_backend(production_name, name) if self.calls else None

        with mock.patch.object(unit_lookup, "labeled_session", side_effect=observed_backend):
            self.assert_resumed(*self.resume())

    def external_transcript(self) -> Path:
        for transcript in (self.configs[0] / "projects").glob(f"*/{DIRECTOR}.jsonl"):
            transcript.unlink()
        folder = self.root / "original-checkout"
        folder.mkdir()
        self.transcript(self.configs[0], folder)
        return folder

    def test_resume_finds_the_session_transcript_outside_the_staff_worktree(self) -> None:
        _ = self.external_transcript()
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertIn(["--cwd", str(self.worktree)], [call[index:index + 2] for call in self.calls for index in range(len(call) - 1)])
        self.assertTrue(self.worktree.is_dir())

    def test_renamed_transcript_keeps_the_staff_resume_folder(self) -> None:
        transcript = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        old = self.root / "alpha-old-wt"
        _ = transcript.write_text("\n".join(json.dumps({"cwd": str(cwd)})
                                            for cwd in (old, old, self.worktree, self.worktree)) + "\n")
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        self.assertEqual(unit.worktree, self.worktree)
        self.assertNotIn(str(old), shutdown.recovery_launch_command(unit))
        self.assert_resumed(*self.resume())
        self.assertEqual(len(self.calls), 1)

    def test_resume_ignores_the_transcript_folder_and_recorded_cwd(self) -> None:
        transcript = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        _ = transcript.write_text(json.dumps({"cwd": str(self.root / "alpha-old-wt")}) + "\n")
        before = transcript.read_bytes(), transcript.stat().st_mtime_ns
        self.assert_resumed(*self.resume())
        self.assertEqual((transcript.read_bytes(), transcript.stat().st_mtime_ns), before)

    def test_duplicate_transcript_locations_do_not_change_the_staff_resume_folder(self) -> None:
        _ = self.external_transcript()
        self.transcript(self.configs[1], self.root / "another-checkout")
        self.assert_resumed(*self.resume())

    def test_missing_session_transcript_keeps_the_retirement_record(self) -> None:
        for config in self.configs:
            for transcript in (config / "projects").glob(f"*/{DIRECTOR}.jsonl"):
                transcript.unlink()
        before = self.doc.read_bytes()
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn(f"no account has the transcript of session {DIRECTOR}", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.doc.read_bytes(), before)

    def test_resume_uses_a_changed_staff_folder_without_rewriting_retirement_facts(self) -> None:
        row = unit_membership("alpha", "alpha")
        parked = unit_config.parked_path(row.id)
        before = parked.read_bytes(), parked.stat().st_mtime_ns
        destination = self.root / "changed-staff-folder"
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), destination), expected=row.worktree)
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        command = shutdown.recovery_launch_command(unit)
        self.assertEqual(command[command.index("--cwd") + 1], str(destination))
        self.assertTrue(shutdown.recovery_has_transcript(unit))
        self.assertEqual((parked.read_bytes(), parked.stat().st_mtime_ns), before)

    def test_shared_projects_symlink_is_one_transcript_location(self) -> None:
        self.configs[1].mkdir()
        (self.configs[1] / "projects").symlink_to(self.configs[0] / "projects", target_is_directory=True)
        self.assert_resumed(*self.resume())
