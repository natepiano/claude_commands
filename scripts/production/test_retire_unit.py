#!/usr/bin/env python3
"""retire_unit.py in process. Git runs in temporary repositories; shutdown, the Codex mesh, the review
pool and build_hold are recording stand-ins, and the unit's Claude sessions are records the test
writes: nothing real stops."""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.lib import process_start
from scripts.lib.process_start_test_support import write_stat
from scripts.astra import design_roles
from scripts.direct import workers  # noqa: E402

import add_unit
import herdr
import retire_unit
import unit_lookup
import unit_move
from herdr import HerdrPlace
from production_units import Production, read_production
from scripts.production.fake_unit_host import PaneShellFixture, stopped_record

if TYPE_CHECKING:
    from scripts.message import sessions
    from scripts.message.sessions import SessionRecord
    from scripts.shutdown import shutdown, shutdown_record
    from scripts.shutdown.shutdown_record import SessionShutdown
else:
    # retire_unit puts the message and shutdown folders on the path.
    import shutdown
    import shutdown_record
    import sessions
    from sessions import SessionRecord

from scripts.production.staff_test_support import StaffTestCase
from scripts.production import staff

DIRECTOR = "11111111-1111-1111-1111-111111111111"
# The fixture replaces the lookup; a test of the lookup itself puts it back.
CLAUDE_SESSIONS = retire_unit.claude_sessions
SEAT = "22222222-2222-2222-2222-222222222222"


def setUpModule() -> None:
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))


def git(where: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(where), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


def session(session_id: str, name: str, pid: int) -> SessionRecord:
    return SessionRecord(pid=pid, sessionId=session_id, name=name, messagingSocketPath="/nonexistent.sock",
                         updatedAt=0, tmux="", formerNames=[])



class _Parsed(Exception):
    """Raised after shutdown.py's parser accepts a command line, before anything runs."""


def shutdown_parses(arguments: list[str]) -> bool:
    """Whether shutdown.py's own parser takes this command line; nothing runs."""
    parse = argparse.ArgumentParser.parse_args

    def parse_only(parser: argparse.ArgumentParser, args: list[str] | None = None,
                   namespace: argparse.Namespace | None = None) -> argparse.Namespace:
        _ = parse(parser, args, namespace)
        raise _Parsed

    with mock.patch.object(argparse.ArgumentParser, "parse_args", parse_only), redirect_stderr(io.StringIO()):
        try:
            _ = shutdown.main(arguments)
        except _Parsed:
            return True
        except SystemExit:
            return False
    return False

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
    pool_lines: str = ""
    marked: unit_lookup.MarkedUnit | None = None
    config: Path = Path()
    records: list[SessionShutdown] = []

    @override
    def setUp(self) -> None:
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
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
        self.records = []
        self.claude = [session(DIRECTOR, "alpha", 4101), session(SEAT, "alpha-seat", 4102)]
        self.pool_lines = "code-1\tcode\talpha-unit:1\tidle\t3\ncode-2\tcode\tbeta-unit:1\tidle\t1\n"
        self.staff.register_production("alpha", self.doc)
        parent = staff.ensure("showrunner", name="alpha")
        _ = staff.ensure("unit", name="alpha", parent=parent.id)
        production = read_production(self.doc)

        def production_of(_slug: str) -> Production:
            return production

        def claude_sessions(_ids: frozenset[str]) -> tuple[SessionRecord, ...]:
            return tuple(self.claude)

        def nothing_inside(_worktree: Path) -> list[int]:
            return []

        def no_reservations(_worktree: Path) -> tuple[str, ...]:
            return ()

        def no_records() -> list[SessionShutdown]:
            return self.records

        def session_processes(session_id: str) -> sessions.SessionProcesses:
            return sessions.SessionProcesses(tuple(item["pid"] for item in self.claude if item["sessionId"] == session_id))

        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": str(runs),
                                                       "UNIT_WORKERS_STATE": str(self.root / "unit-workers"),
                                                       "BUILD_HOLD_DIR": str(holds),
                                                       "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                                                       "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership")}))
        _ = self.enterContext(mock.patch.object(retire_unit, "MAC_CLAIMS", self.root / "claims"))
        _ = self.enterContext(mock.patch.object(retire_unit, "production_of", production_of))
        _ = self.enterContext(mock.patch.object(retire_unit, "run", self.fake_run))
        _ = self.enterContext(mock.patch.object(retire_unit, "claude_sessions", claude_sessions))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", nothing_inside))
        _ = self.enterContext(mock.patch.object(retire_unit, "reservations", no_reservations))
        _ = self.enterContext(mock.patch.object(shutdown_record, "all_session_records", no_records))
        _ = self.enterContext(mock.patch.object(sessions, "session_processes", session_processes))
        self.marked = unit_lookup.MarkedUnit(
            "alpha-unit", unit_lookup.LiveClaude("alpha", DIRECTOR, "/nonexistent.sock", 4101), HerdrPlace("alpha", "w1:p1"))

        def marked_unit(_slug: str, _name: str) -> unit_lookup.MarkedUnit | None:
            return self.marked

        def process_info(*_arguments: str) -> dict[str, object]:
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

    def shutdown_state(self, kind: Literal["stopping", "stopped"]) -> SessionShutdown:
        # Only the record fields read by the stop/finished checks at this patched boundary.
        return cast("SessionShutdown", cast(object, {"session": {"session_id": DIRECTOR, "pid": 4101,
                    "name": "alpha", "kind": "unit", "host": {"production": "alpha", "unit": "alpha-unit"}},
                    "requested_at": "2026-10-28T23:00:00+00:00",
                    "progress": {"kind": kind}, "force": "wait for ready", "then": "stop", "stop_issues": []}))

    def fake_run(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Git runs for real; every other command is recorded and answered."""
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        call = [Path(argv[1]).name, *argv[2:]] if argv[0] == sys.executable else argv
        self.calls.append(call)
        stdout = ""
        if call[:2] == ["review_pool.py", "status"]:
            stdout = self.pool_lines
        elif call[:2] == ["review_pool.py", "retire"]:
            stdout = "".join(line.split("\t")[0] + "\n" for line in self.pool_lines.splitlines()
                             if "\talpha-unit:" in line and "\tidle\t" in line)
        elif call[:2] == ["shutdown.py", "down"]:
            self.claude = []
            self.marked = None
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def retire(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = retire_unit.main(["alpha", "alpha", *arguments])
        return code, output.getvalue()

    def stops(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] != ["review_pool.py", "status"]]

    def test_claim_cleanup_keeps_unknown_and_never_signals_a_reused_or_gone_pid(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            stat = write_stat(root, 41, start_ticks=456)
            native_check = process_start.check_owner

            def check_owner(pid: int, start: process_start.ProcessStart) -> process_start.OwnerCheck:
                return native_check(pid, start, root)

            with mock.patch.object(os, "kill") as signal:
                for mode in ("reused", "gone", "unknown"):
                    if mode == "gone":
                        stat.unlink()
                    elif mode == "unknown":
                        stat.parent.mkdir(exist_ok=True)
                        _ = stat.write_bytes(b"broken")
                    with mock.patch.object(process_start, "check_owner", side_effect=check_owner):
                        remaining = retire_unit.terminate([41], {41: "claim"}, wait=0,
                                                         recorded={41: process_start.ProcessStartTime(123)})
                    self.assertEqual(bool(remaining), mode == "unknown")
                signal.assert_not_called()

    def pane_shell_completes(self, *, live: bool) -> None:
        pane = PaneShellFixture(self, self.worktree, self.root / "proc", live=live, slug="alpha")
        pane.found = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.LiveClaude(
            "alpha", DIRECTOR, "socket", pane.director) if live else unit_lookup.ClaudeNotRunning(), pane.place)
        saved = stopped_record(self.root, self.worktree)
        saved["session"]["session_id"] = DIRECTOR
        host = saved["session"]["host"]
        assert host["kind"] == "unit"
        self.records = []
        self.claude = [session(DIRECTOR,"alpha",pane.director)] if live else []
        self.enterContext(mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")}))
        def clean_stop() -> None:
            pane.stopped()
            self.claude = []
            self.assertTrue(shutdown_record.create_session(saved))
            self.records = [saved]
        original = self.fake_run
        def run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            result = original(argv,cwd)
            if str(retire_unit.SHUTDOWN) in argv and "down" in argv:
                clean_stop()
            return result
        if not live:
            clean_stop()
        with mock.patch.object(retire_unit, "run", side_effect=run):
            code, output = self.retire("--wait-minutes", "0")
        self.assertEqual(code, 0, output)
        self.assertEqual(pane.close_calls(), [["--session","agents","pane","close","w1:p1"]])
        self.assertFalse(self.worktree.exists())
        self.assertIn("retired", self.doc.read_text())
        self.assertEqual(sum(call[:2] == ["shutdown.py","down"] for call in self.calls), int(live))

    def test_live_director_in_pane_shell_stops_closes_and_retires(self) -> None:
        self.pane_shell_completes(live=True)

    def staff_unit(self) -> tuple[staff.StaffRow, staff.StaffRow]:
        runner = staff.ensure("showrunner", name="tools")
        unit = staff.ensure("unit", name="panes", parent=runner.id)
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

    def test_retire_unit_rerun(self) -> None:
        runner, unit = self.staff_unit()
        original = staff.retire
        for after in (False, True):
            def interrupted(staff_id: staff.StaffId) -> staff.StaffRow | None:
                if after:
                    _ = original(staff_id)
                raise RuntimeError("stopped at staff retirement")
            with mock.patch.object(staff, "retire", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "stopped at staff retirement"):
                    _ = self.retire()
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertIsNone(staff.by_id(unit.id))
        self.assertEqual(staff.by_id(runner.id), runner)
        data, document = self.staff.staff_file.read_bytes(), self.doc.read_bytes()
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.doc.read_bytes()), (data, document))

    def test_exited_directors_pane_shell_closes_and_retires(self) -> None:
        self.pane_shell_completes(live=False)

    def test_unowned_cwd_visitor_or_other_units_shell_blocks_without_signals(self) -> None:
        for marked_pane in (False,True):
            for visitor_unit in ("", "beta-unit"):
                with self.subTest(marked_pane=marked_pane, visitor_unit=visitor_unit):
                    self.claude = []
                    self.marked = None
                    unit = retire_unit.find_unit("alpha", "alpha")
                    self.records = []
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
            self.assertEqual(CLAUDE_SESSIONS(frozenset()), ())

    def test_process_gone_after_owned_cleanup_does_not_remain_a_cwd_blocker(self) -> None:
        unit = retire_unit.find_unit("alpha", "alpha")
        found = retire_unit.Found((), (), (), (), (), (), (991551,))
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
        self.assertIn("refused: alpha-branch has a checkpoint not merged: checkpoint(alpha): the late work", output)
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

    def test_dry_run_lists_the_seat_the_worker_and_the_reviewer_and_changes_nothing(self) -> None:
        before = self.doc.read_text(encoding="utf-8")

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        for line in (f"would stop: Claude session alpha ({DIRECTOR}, pid 4101), through shutdown down",
                     f"would stop: Claude session alpha-seat ({SEAT}, pid 4102), through shutdown down",
                     f"would stop: app-server pid {os.getpid()} of run run-1; workers alpha-impl",
                     "would retire: reviewer code-1",
                     f"would release: build hold of {DIRECTOR}",
                     f"would remove: worktree {self.worktree}",
                     "would delete: branch alpha-branch here and on origin"):
            self.assertIn(line, output.splitlines())
        self.assertNotIn("code-2", output)
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
        self.assertIn(f"would stop: Claude session alpha ({DIRECTOR}, pid 4101), through shutdown down", output.splitlines())
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

    def test_every_shutdown_call_retire_makes_parses(self) -> None:
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        calls = [call[1:] for call in self.calls if call[0] == "shutdown.py"]
        self.assertIn("down", [call[0] for call in calls])
        for arguments in calls:
            with self.subTest(arguments=arguments):
                self.assertTrue(shutdown_parses(arguments), f"shutdown.py refuses {arguments}")

    def test_retires_a_unit_with_a_seat_a_worker_and_a_reviewer(self) -> None:
        code, output = self.retire()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.stops(), [
            ["shutdown.py", "down", "--here", "--only", f"{DIRECTOR},{SEAT}"],
            ["shutdown.py", "forget", DIRECTOR],
            ["shutdown.py", "forget", SEAT],
            ["codex_mesh.py", "stop", "--session-dir", str(self.run_folder)],
            ["review_pool.py", "retire", "--production", "alpha", "--unit", "alpha-unit"],
            ["build_hold.py", "release", "--holder", DIRECTOR],
        ])
        self.assertIn("stopped: reviewer code-1", output)
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
        claude, marked = list(self.claude), self.marked
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        expected_tree = git(self.checkout, "rev-parse", "HEAD^{tree}")
        expected_doc = self.doc.read_text()
        shutil.rmtree(self.root)
        _ = shutil.copytree(snapshot, self.root)
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

    def test_a_reviewer_still_reviewing_keeps_the_unit_from_retiring(self) -> None:
        self.pool_lines = "code-1\tcode\talpha-unit:1\tbusy\t3\n"

        code, output = self.retire()

        self.assertEqual(code, 1)
        self.assertIn("left running: reviewer code-1: it is reviewing", output)
        self.assertIn("alpha-unit is not retired: rerun once these stop", output)
        self.assertTrue(self.worktree.is_dir())
        self.assertNotIn("(retired", self.doc.read_text(encoding="utf-8"))

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
        self.assertIn(["shutdown.py", "down", "--here", "--only", DIRECTOR], self.calls)
        self.assertIn(["shutdown.py", "forget", DIRECTOR], self.calls)
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

    def test_forget_waits_for_the_shutdown_record_after_the_process_exits(self) -> None:
        events: list[str] = []
        def command(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            result = self.fake_run(argv, cwd)
            if argv[1:3] == [str(retire_unit.SHUTDOWN), "down"]:
                self.records = [self.shutdown_state("stopping")]
                events.append("process exits")
            elif argv[1:3] == [str(retire_unit.SHUTDOWN), "forget"]:
                self.assertEqual(self.records[0]["progress"]["kind"], "stopped")
                events.append("forget")
            return result
        def finish_stop(_seconds: float) -> None:
            events.append("stop finishes")
            self.records = [self.shutdown_state("stopped")]
        with (mock.patch.object(retire_unit, "run", command),
              mock.patch.object(time, "sleep", finish_stop)):
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(events, ["process exits", "stop finishes", "forget", "forget"])

    def test_failed_forget_keeps_the_unit_and_reports_failure(self) -> None:
        before = self.doc.read_bytes()
        def command(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == [str(retire_unit.SHUTDOWN), "forget"]:
                return subprocess.CompletedProcess(argv, 1, "", "cannot forget shutdown in progress (stopping)")
            return self.fake_run(argv, cwd)
        with mock.patch.object(retire_unit, "run", command):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("left running:", output)
        self.assertIn("cannot forget shutdown in progress (stopping)", output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.exists())

    def test_final_check_refuses_a_shutdown_record_that_is_still_in_progress(self) -> None:
        before = self.doc.read_bytes()
        def pending_stop(_slug: str, _unit: str) -> None:
            self.records = [self.shutdown_state("stopping")]
        with mock.patch.object(design_roles, "end_holder", side_effect=pending_stop):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn(f"session {DIRECTOR}, pid 4101: shutdown stopping", output)
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

    def test_unfinished_stop_exhausts_the_same_deadline_without_forgetting(self) -> None:
        before = self.doc.read_bytes()
        def command(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            result = self.fake_run(argv, cwd)
            if argv[1:3] == [str(retire_unit.SHUTDOWN), "down"]:
                self.records = [self.shutdown_state("stopping")]
            return result
        with mock.patch.object(retire_unit, "run", command):
            code, output = self.retire("--wait-minutes", "0")
        self.assertEqual(code, 1, output)
        self.assertIn(f"{DIRECTOR}, pid 4101): still stopping after 0 minutes", output)
        self.assertNotIn(["shutdown.py", "forget", DIRECTOR], self.calls)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.exists())

    def test_rerun_forgets_the_exited_directors_retained_restore_before_marking(self) -> None:
        folder = self.outside_director()
        reject_forget = True
        def command(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == [str(retire_unit.SHUTDOWN), "forget"] and reject_forget:
                return subprocess.CompletedProcess(argv, 1, "", "forget storage failed")
            result = self.fake_run(argv, cwd)
            if argv[1:3] == [str(retire_unit.SHUTDOWN), "down"]:
                self.records = [self.shutdown_state("stopped")]
            elif argv[1:3] == [str(retire_unit.SHUTDOWN), "forget"]:
                self.records = []
            return result
        with mock.patch.object(retire_unit, "run", command):
            code, output = self.retire()
            self.assertEqual(code, 1, output)
            self.assertIn("forget storage failed", output)
            self.assertNotIn("(retired", self.doc.read_text())
            self.assertTrue(self.worktree.exists())
            self.assertIsNone(self.marked)
            reject_forget = False
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertIn(f"session {DIRECTOR}, folder {folder})", self.doc.read_text())
        self.assertFalse(self.worktree.exists())
        self.assertEqual(len([call for call in self.calls if call[:2] == ["shutdown.py", "down"]]), 1)
        self.assertEqual(self.records, [])


if __name__ == "__main__":
    _ = unittest.main()
