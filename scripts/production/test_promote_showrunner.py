#!/usr/bin/env python3
"""promote_showrunner.py in process, with recording stand-ins for herdr, the unit's process, the session
lookups and add_showrunner's start: nothing real stops or starts. Git runs in temporary repositories."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member
from scripts.lib.atomic_file import write_text_atomic

import io
import json
import math
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, cast, final, override
from unittest import mock


from scripts.production import add_showrunner
from scripts.production import promote_showrunner
from scripts.production import showrunners, worker
from scripts.agents import stop_resume
from scripts.production import showrunner_tree
from scripts.production import unit_effort
from scripts.production import unit_lookup
from scripts.herdr import herdr
from scripts.message import sessions
from scripts.production.production_doc import ProductionDocument, Refusal, read_new_production, read_production
from scripts.accounts import accounts
from scripts.herdr import herdr as trust_reader, worker_panes
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.whoami import account

from scripts.production.staff_test_support import StaffTestCase
from scripts.production import staff, staff_sources, unit_config
from scripts.production.fake_unit_host import process, private_process_registry

CHILD_PREFLIGHT = add_showrunner.checked

SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PID = 4242
PANE = "w7:p1"
# add_showrunner.checked as written: setUp replaces it with a recording stand-in.
ORIGINAL_CHECKED = add_showrunner.checked
START_SHOWRUNNER = add_showrunner.main


def pane_identity(pane: str, label: str) -> herdr.PaneIdentity:
    return herdr.PaneIdentity(pane_id=pane, label=label, workspace_id=pane.split(":", 1)[0], session_id=None)


def setUpModule() -> None:
    """The retired row's date is in the user's zone; the zone setting must not change these tests."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


def doc_text(checkout: Path) -> str:
    return ("# Production\n\n"
            + "- **Merge branch:** main\n"
            + f"- **Showrunner checkout:** {checkout}\n"
            + "- **Log:** log.md\n")


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for command in (["init", "-q", "-b", "main"], ["config", "user.email", "test@example.invalid"],
                    ["config", "user.name", "test"], ["config", "commit.gpgsign", "false"]):
        _ = subprocess.run(["git", "-C", str(path), *command], check=True, capture_output=True)
    return path


def commit_all(path: Path) -> None:
    _ = subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    _ = subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True, capture_output=True)


@final
class PromoteShowrunnerTests(unittest.TestCase):
    def test_promotion_closes_retained_shells_only_after_every_stop_proves_success(self) -> None:
        unit = self.staff_unit()
        retained = fixture_member("worker", name="impl", parent=unit.id)
        promotion = promote_showrunner.checked("demo", "child", self.child_doc)
        for stopped_director, unresolved, close_result in ((False, [], True), (True, [], True), (False, [], False),
                (False, [], OSError("worker close failed")), (False, ["impl: not proved stopped"], True)):
            world = PaneWorld()
            world.add(unit, PANE, live=not stopped_director)
            world.add(retained, "w7:p2", live=False)
            before = staff.snapshot()
            def stop(session: str, *, deadline: float) -> stop_resume.Stopped:
                _ = world.foreground.pop(PANE, None)
                return self.stop_member(session, deadline=deadline)
            def close(pane: str, *, target: str = "", terminal: str = "") -> bool:
                self.assertEqual((target, terminal), (SESSION, "") if pane == PANE else ("", "terminal-w7:p2"))
                if pane != PANE:
                    if isinstance(close_result, OSError):
                        raise close_result
                    if not close_result:
                        return False
                _ = world.locations.pop(pane)
                _ = world.foreground.pop(pane, None)
                return True
            with (self.subTest(stopped_director=stopped_director, unresolved=unresolved, close_result=close_result),
                  mock.patch.object(worker_panes, "_client", return_value=world),
                  mock.patch.object(herdr.Herdr, "panes", side_effect=world.panes),
                  mock.patch.object(herdr.Herdr, "pane_activity", side_effect=world.pane_activity),
                  mock.patch.object(stop_resume, "stop", side_effect=stop),
                  mock.patch.object(herdr, "native_process", side_effect=world.native_process),
                  mock.patch.object(herdr, "_startup_processes", side_effect=world.descendants),
                  mock.patch.object(stop_resume, "stop_unit_workers", return_value=unresolved),
                  mock.patch.object(herdr.Herdr, "close_tab_confirmed", side_effect=close) as closed,
                  redirect_stderr(io.StringIO()) as errors):
                state = worker_panes.observe(unit.id, before)[0]
                self.assertIsInstance(state, worker_panes.ClosedWorker if stopped_director else worker_panes.StoppedWorker)
                self.assertTrue(promote_showrunner.stop(promotion))
                if unresolved:
                    closed.assert_not_called()
                    self.assertEqual(set(world.locations), {PANE, "w7:p2"})
                else:
                    self.assertEqual(closed.call_count, 2)
                    self.assertEqual(closed.call_args_list, [mock.call("w7:p2", terminal="terminal-w7:p2"),
                                                           mock.call(PANE, target=SESSION)])
                    self.assertIsInstance(worker_panes.observe(unit.id, staff.snapshot())[0], worker_panes.ClosedWorker)
                self.assertEqual(staff.snapshot().rows, before.rows)
                if not unresolved and close_result is not True:
                    self.assertEqual(set(world.locations), {"w7:p2"})
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertIn("anomaly: impl:", errors.getvalue())
                else:
                    self.assertEqual(errors.getvalue(), "")

    def test_promotion_keeps_a_worker_program_and_a_shell_in_another_tab(self) -> None:
        unit = self.staff_unit()
        retained = fixture_member("worker", name="impl", parent=unit.id)
        promotion = promote_showrunner.checked("demo", "child", self.child_doc)
        world = PaneWorld()
        world.add(unit, PANE, live=False)
        world.add(retained, "w7:p2")
        world.add(retained, "w7:p3", live=False, tab="w1:t2")
        before = staff.snapshot()
        with (mock.patch.object(worker_panes, "_client", return_value=world),
              mock.patch.object(herdr.Herdr, "panes", side_effect=world.panes),
              mock.patch.object(herdr.Herdr, "pane_activity", side_effect=world.pane_activity),
              mock.patch.object(herdr.Herdr, "close_tab_confirmed", return_value=True) as close):
            self.assertTrue(promote_showrunner.stop(promotion))
        close.assert_called_once_with(PANE, target=SESSION)
        self.assertEqual(staff.snapshot().rows, before.rows)
        self.assertEqual(set(world.locations), {PANE, "w7:p2", "w7:p3"})

    def test_promotion_continues_after_a_failed_confirmed_close(self) -> None:
        promotion = promote_showrunner.checked("demo", "child", self.child_doc)
        with (mock.patch.object(herdr.Herdr, "close_tab_confirmed", return_value=False),
              redirect_stderr(io.StringIO()) as errors):
            self.assertTrue(promote_showrunner.stop(promotion))
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("anomaly:", errors.getvalue())

    def test_promotion_continues_after_a_failed_pane_read(self) -> None:
        promotion = promote_showrunner.checked("demo", "child", self.child_doc)
        with (mock.patch.object(stop_resume, "stop_unit_workers", return_value=[]),
              mock.patch.object(herdr.Herdr, "panes", side_effect=herdr.HerdrFailure("unreadable")),
              mock.patch.object(herdr.Herdr, "close_tab_confirmed", return_value=True) as close,
              redirect_stderr(io.StringIO()) as errors):
            self.assertTrue(promote_showrunner.stop(promotion))
        close.assert_called_once_with(PANE, target=SESSION)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("anomaly:", errors.getvalue())

    def test_promote_restores_retained_workers(self) -> None:
        unit = self.staff_unit()
        retained = fixture_member("worker", name="impl", parent=unit.id)
        with mock.patch.object(worker, "restore", return_value=worker.ExitCode.OK) as restore:
            code, output, error = self.run_main()
        self.assertEqual(code, 0, output + error)
        restore.assert_called_once_with(unit.id)
        self.assertEqual(staff.by_id(retained.id), retained)

    def test_remote_director_location_refuses_before_the_unit_stops(self) -> None:
        row = self.staff_unit()
        _ = staff.set_worktree(row.id, staff.PresentWorktree("mac", self.worktree), expected=row.worktree)
        before = self.parent_doc.read_bytes(), self.child_doc.read_bytes()
        code, output, errors = self.run_main()
        self.assertNotEqual(code, 0, output + errors)
        self.assertIn("mac", output + errors)
        self.assert_nothing_stopped()
        self.assertEqual((self.parent_doc.read_bytes(), self.child_doc.read_bytes()), before)

    staff: StaffTestCase = StaffTestCase()
    root: Path = Path()
    parent_doc: Path = Path()
    child_doc: Path = Path()
    worktree: Path = Path()
    events: list[str] = []
    alive: list[bool] = []
    started: int = 0
    marked: dict[str, unit_lookup.LabeledSession] = {}
    unit_account: accounts.ClaudeLaunchAccount = accounts.NamedAccount("claude2", Path())
    # What the stand-ins answer; a refusal case changes one of them.
    runs_production: bool = True
    pane: unit_effort.Ready | unit_effort.Skipped = unit_effort.Ready()
    child_refusal: str = ""

    @override
    def setUp(self) -> None:
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, path in {
            "NOTIFIER_STATE_DIR": self.root / "notifier",
            "SHOWRUNNER_STATE_DIR": self.root / "showrunner-state",
            "CONFIG": self.root / "showrunners.json",
        }.items():
            _ = self.enterContext(mock.patch.object(showrunners, name, path))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner-state"),
        })))
        _ = self.staff.production("parent")
        parent = repo(self.root / "parent")
        self.parent_doc = parent / "parent-production.md"
        _ = self.parent_doc.write_text(doc_text(parent))
        _ = (parent / "log.md").write_text("log\n")
        commit_all(parent)
        self.worktree = repo(self.root / "demo")
        self.child_doc = self.worktree / "future-production.md"
        _ = self.child_doc.write_text(doc_text(self.worktree))
        commit_all(self.worktree)
        self.staff.register_production("parent", self.parent_doc)
        parent_row = fixture_member("showrunner", name="parent", doc=staff.LiveDoc("natedev", self.parent_doc))
        _ = fixture_member("unit", name="demo", parent=parent_row.id, runtime_id=SESSION,
                           worktree=staff.PresentWorktree("natedev", self.worktree))
        _ = unit_config.write(self.worktree, unit_config.UnitConfig(
            unit_config.PlanFile(Path("docs/demo.md")), unit_config.NoAppPort(), ()))
        other = fixture_member("unit", name="other", parent=parent_row.id)
        _ = staff.set_worktree(other.id, staff.RemovedWorktree("natedev", self.root / "other"), expected=other.worktree)
        self.staff.pane("fixture:parent/demo-unit", session_id=SESSION, environment={
            ('' + 'SHOWRUNNER_' + 'UNIT'): "parent", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "demo"})
        session_records = self.root / "sessions"
        session_records.mkdir()
        _ = (session_records / f"{PID}.json").write_text(json.dumps({"pid": PID, "sessionId": SESSION, "name": "demo",
            "messagingSocketPath": "", "updatedAt": 1, "status": "idle", "procStart": "1"}))
        self.events, self.alive, self.started = [], [True, True, False], 0
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"code": 0, "stdout": ""}}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HERDR_BIN": str(Path(__file__).parents[1] / "herdr/fake_herdr.py"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "conversation-pause"),
        })))
        self.marked = {"demo": unit_lookup.LabeledSession(
            "demo", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID), herdr.HerdrPlace("demo", PANE))}
        self.unit_account = accounts.NamedAccount("claude2", self.root / ".claude2")
        (self.root / ".claude").mkdir()
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "default@example.invalid"}}))
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "claude2.md").write_text("---\nlogin: default@example.invalid\n---\n")
        directory = self.root / ".claude2"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "default@example.invalid"}}))
        self.runs_production, self.pane, self.child_refusal = True, unit_effort.Ready(), ""
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(notes), "NOTIFIER_SESSIONS_DIR": str(session_records)}))
        self.assertEqual(accounts.setup("claude2"), [])
        _ = self.enterContext(mock.patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HERDR_PROC": str(self.root / "proc")})))
        _ = self.enterContext(private_process_registry())
        process(os.environ, PID, 998040, ["claude"], {}, cwd=self.worktree)
        def pane_result(client: herdr.Herdr, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
            del client, deadline
            if arguments[:2] == ("pane", "list"):
                if "demo" not in self.marked:
                    return {"panes": []}
                place = self.marked["demo"].place
                assert isinstance(place, herdr.HerdrPlace)
                return {"panes": [{"pane_id": place.pane, "terminal_id": "unit-terminal",
                                   "workspace_id": place.pane.split(":")[0], "tab_id": "w7:t1"}]}
            if arguments[:2] == ("agent", "list"):
                return {"agents": []}
            if arguments[:2] == ("pane", "process-info"):
                return {"process_info": {"shell_pid": 998040, "foreground_process_group_id": PID,
                    "foreground_processes": [{"pid": PID, "name": "claude", "argv": ["claude"]}]}}
            raise AssertionError(arguments)
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "result", pane_result))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "composer", return_value=herdr.ComposerInput("")))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "close_tab_confirmed", return_value=True))
        for module, name, stand_in in (
                (showrunners, "own_name", self.own_name), (unit_lookup, "labeled_session", self.labeled_session),
                (stop_resume, "stop", self.stop_member), (stop_resume, "stop_unit_workers", mock.Mock(return_value=[])),
                (worker, "restore", mock.Mock(return_value=worker.ExitCode.OK)), (add_showrunner, "checked", self.child_checked),
                (add_showrunner, "main", self.start), (accounts, "role_account", self.role_account),
                (unit_effort, "verdict", self.verdict)):
            _ = self.enterContext(mock.patch.object(module, name, stand_in))

    def ready_showrunner_account(self) -> accounts.NamedAccount:
        notes = self.root / "notes"
        notes.mkdir(exist_ok=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)})))
        _ = (notes / "claude3.md").write_text("---\nlogin: showrunner@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude3"), [])
        directory = accounts.account_dir("claude3")
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "showrunner@example.invalid"}}))
        return accounts.NamedAccount("claude3", directory)

    def own_name(self) -> str:
        return "parent" if self.runs_production else ""

    def verdict(self, _capture: str) -> unit_effort.Ready | unit_effort.Skipped:
        return self.pane

    def labeled_session(self, _production_name: str, name: str) -> unit_lookup.LabeledSession | None:
        return self.marked.get(name)

    def role_account(self, _role: str) -> accounts.ClaudeLaunchAccount:
        return self.unit_account

    def stop_member(self, session: str, *, deadline: float) -> stop_resume.Stopped:
        del deadline
        self.events.append(f"stop member {session}")
        return stop_resume.Stopped(session, PID)


    def child_checked(self, _child: ProductionDocument, caller: str, ending: add_showrunner.EndingTab = add_showrunner.NO_ENDING_TAB, start_in: Path | None = None, *, config_dir: Path | None = None) -> None:
        self.events.append(f"check child for {caller}, ending {ending}, starting in {start_in}")
        self.assertEqual(config_dir, self.unit_account.dir)
        if self.child_refusal:
            raise Refusal(f"{self.child_refusal} (ending {ending})")

    def start(self, argv: list[str]) -> int:
        self.events.append("start " + " ".join(argv))
        return self.started

    def run_main(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = promote_showrunner.main(["demo", "--production", "child", "--doc", str(self.child_doc),
                                            "--model", "opus", "--effort", "high", *extra])
        return code, out.getvalue(), err.getvalue()

    def assert_nothing_stopped(self) -> None:
        self.assertFalse(any(event.startswith(("prompt herdr ", "stop member ", "start")) for event in self.events))

    def staff_unit(self) -> staff.StaffRow:
        parent = fixture_member("showrunner", name="parent")
        unit = fixture_member("unit", name="demo", parent=parent.id, runtime_id=SESSION)
        self.staff.pane("w7:p1", session_id=SESSION, environment={
            "CLAUDE_CODE_SESSION_ID": unit.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "parent", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "demo"})
        return unit

    def assert_native_promotion(self, unit: staff.StaffRow) -> None:
        retained = staff.by_id(unit.id)
        assert retained is not None
        self.assertEqual((retained.id, retained.parent, retained.kind, retained.name, retained.doc, retained.worktree),
                         (unit.id, unit.parent, "showrunner", "child", staff.LiveDoc("natedev", self.child_doc),
                          staff.WORKTREE_UNFILLED))
        self.assertEqual([row for row in staff.snapshot().rows if row.id == unit.id], [retained])
        self.assertEqual([row for row in staff.find(kind="showrunner") if row.name == "child"], [retained])

    def test_promote_keeps_worker_rows_after_restore(self) -> None:
        unit = self.staff_unit()
        worker = fixture_member("worker", name="impl1", parent=unit.id)
        code, output, errors = self.run_main()
        self.assertEqual(code, 0, output + errors)
        self.assert_native_promotion(unit)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertTrue(any(row.name == "child" for row in staff.find(kind="showrunner")))

    def test_promote_showrunner_rerun(self) -> None:
        unit = self.staff_unit()
        original_ensure = staff_sources.ensure_staff_showrunner
        for after_ensure in (False, True):
            def interrupted_ensure(production: ProductionDocument, caller: str = "", *,
                                   runtime_id: str = "") -> staff.StaffRow:
                self.assertEqual(production.doc, self.parent_doc)
                if after_ensure:
                    _ = original_ensure(production, caller, runtime_id=runtime_id)
                raise RuntimeError("stopped at staff ensure")

            with mock.patch.object(staff_sources, "ensure_staff_showrunner",
                                   side_effect=interrupted_ensure):
                with self.assertRaisesRegex(RuntimeError, "stopped at staff ensure"):
                    _ = self.run_main()
            self.assert_nothing_stopped()
        original = staff.promote_unit
        for after in (False, True):
            before = self.staff.staff_file.read_bytes()

            def interrupted(identity: staff.StaffId, *, name: str, parent: staff.StaffId,
                            doc: staff.LiveDoc) -> staff.StaffRow:
                self.assertEqual(identity, unit.id)
                if after:
                    _ = original(identity, name=name, parent=parent, doc=doc)
                raise RuntimeError("stopped at native promotion")

            with mock.patch.object(staff, "promote_unit", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "stopped at native promotion"):
                    _ = self.run_main()
            if not after:
                self.assertEqual(self.staff.staff_file.read_bytes(), before)
                self.assertEqual(staff.by_id(unit.id), unit)
            else:
                self.assert_native_promotion(unit)
            self.assertFalse(any(event.startswith("start") for event in self.events))
            self.marked.clear()
        code, output, errors = self.run_main()
        self.assertEqual(code, 0, output + errors)
        self.assert_native_promotion(unit)
        self.assertTrue(any(row.name == "child" for row in staff.find(kind="showrunner")))
        data, document = self.staff.staff_file.read_bytes(), self.parent_doc.read_bytes()
        code, output, errors = self.run_main()
        self.assertEqual(code, 0, output + errors)
        self.assertIn("already promoted", output)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.parent_doc.read_bytes()), (data, document))

    def test_promotion_resumes_the_saved_conversation_after_stop(self) -> None:
        with mock.patch.object(add_showrunner, "main", side_effect=RuntimeError("stopped after unit exit")):
            with self.assertRaises(RuntimeError):
                _ = self.run_main()
        self.marked.clear()
        pending = promote_showrunner.promotion_record(read_production("parent"), "demo")
        self.assertTrue(pending.exists())
        self.events.clear()
        code, out, err = self.run_main()
        self.assertEqual(code, 0, (out, err))
        self.assertIn("demo is now the showrunner of child", out)
        start = next(event for event in self.events if event.startswith("start "))
        self.assertIn(f"--resume {SESSION}", start)
        self.assertIn(f"--cwd {self.worktree}", start)
        self.assertFalse(any(event.startswith("prompt herdr") for event in self.events))
        self.assertFalse(pending.exists())

    def test_retired_showrunner_receipt_cannot_complete_a_new_promotion(self) -> None:
        code, output, errors = self.run_main()
        self.assertEqual(code, 0, output + errors)
        child = staff.by_path("child")
        assert child is not None
        _ = staff.set_doc(child.id, staff.WrappedDoc("natedev", self.child_doc))
        code, output, errors = self.run_main()
        self.assertEqual(code, 2, output + errors)
        self.assertNotIn("already promoted", output)

    def test_started_child_without_timer_finishes_registration_without_second_start(self) -> None:
        submitted = herdr.PromptSubmitted(datetime.now(UTC))

        def start_child(_child: ProductionDocument, _model: str, _effort: str, resume: str | None,
                        cwd: Path, _selected: accounts.ClaudeLaunchAccount,
                        *, identity: add_showrunner.ShowrunnerLaunch,
                        before_prompt: Callable[[herdr.SessionTranscript], object]) -> herdr.PromptSubmitted:
            self.assertEqual((resume, cwd), (SESSION, self.worktree))
            self.assertEqual(identity.session_id, SESSION)
            _ = before_prompt(herdr.SessionTranscript(_selected.dir, SESSION))
            row = staff.by_id(staff.native_id(SESSION))
            assert row is not None
            self.assertEqual((row.kind, row.name), ("showrunner", "child"))
            self.marked.clear()
            return submitted

        with (mock.patch.object(add_showrunner, "main", START_SHOWRUNNER),
              mock.patch("scripts.message.sessions.live_sessions", return_value=[]),
              mock.patch.object(add_showrunner, "launch", side_effect=start_child) as started,
              mock.patch.object(add_showrunner, "showrunner_appeared", side_effect=RuntimeError("stopped before timer"))):
            with self.assertRaisesRegex(RuntimeError, "stopped before timer"):
                _ = self.run_main()
        started.assert_called_once()
        pending = promote_showrunner.promotion_record(read_production("parent"), "demo")
        self.assertTrue(pending.exists())
        place = herdr.HerdrPlace("agents", "w8:p1")
        with (mock.patch.object(showrunners, "current_name", return_value=""),
              mock.patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(5151)),
              mock.patch.object(herdr, "herdr_place_of", return_value=place),
              mock.patch.object(herdr.Herdr, "panes", return_value=[pane_identity(place.pane, "child")]),
              mock.patch.object(herdr, "submitted_prompt", return_value=submitted),
              mock.patch.object(herdr.Herdr, "submit_opening_prompt") as prompt,
              mock.patch.object(add_showrunner, "showrunner_appeared", return_value=True) as appeared,
              mock.patch.object(showrunners, "timer_names", return_value={"parent", "child"}),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[]),
              mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery(("sent child", "sent parent"))),
              mock.patch.object(add_showrunner, "main") as restart):
            code, out, err = self.run_main()
        self.assertEqual((code, err), (0, "unchanged\n"), out)
        appeared.assert_called_once_with("child")
        restart.assert_not_called()
        prompt.assert_not_called()
        self.assertIn("sent child", out)
        self.assertIn("sent parent", out)
        self.assertEqual(showrunner_tree.parent_of("child"), "parent")
        self.assertFalse((self.root / "showrunner-state" / staff.native_id(SESSION) / "parent").exists())
        self.assertFalse(pending.exists())

    def test_saved_parent_after_interrupted_attach_still_sends_routing_notices(self) -> None:
        def start_and_attach(_argv: list[str]) -> int:
            self.marked.clear()
            return showrunners.main(["attach", "child", "parent"])

        pending = promote_showrunner.promotion_record(read_production("parent"), "demo")

        def send_notices(texts: dict[str, str], key: str) -> showrunners.NoticeDelivery:
            self.assertTrue(pending.exists())
            self.assertEqual(set(texts), {"parent", "child"})
            self.assertEqual(key, "showrunner-tree:child")
            return showrunners.NoticeDelivery(("sent child", "sent parent"))

        with (mock.patch.object(showrunners, "timer_names", return_value={"parent", "child"}),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[])):
            with (mock.patch.object(add_showrunner, "main", side_effect=start_and_attach),
                  mock.patch.object(showrunners, "tell_others", side_effect=RuntimeError("stopped before notices"))):
                with self.assertRaisesRegex(RuntimeError, "stopped before notices"):
                    _ = self.run_main()
            self.assertEqual(showrunner_tree.parent_of("child"), "parent")
            self.assertFalse((self.root / "showrunner-state" / staff.native_id(SESSION) / "parent").exists())
            self.assertTrue(pending.exists())
            with (mock.patch.object(showrunners, "current_name", return_value="child"),
                  mock.patch.object(showrunners, "tell_others", side_effect=send_notices) as notices,
                  mock.patch.object(add_showrunner, "main") as restart):
                code, out, err = self.run_main()
        self.assertEqual((code, err), (0, "unchanged\n"), out)
        restart.assert_not_called()
        notices.assert_called_once()
        self.assertIn("sent child", out)
        self.assertIn("sent parent", out)
        self.assertFalse(pending.exists())

    def test_each_refusal_names_its_cause_and_stops_nothing(self) -> None:
        def no_production() -> None:
            self.runs_production = False

        def child_taken() -> None:
            self.child_refusal = "child already has a running showrunner"

        def mid_turn() -> None:
            path = self.root / "sessions" / f"{PID}.json"
            data = cast(dict[str, object], json.loads(path.read_text()))
            data["status"] = "busy"
            _ = path.write_text(json.dumps(data))

        def form() -> None:
            self.pane = unit_effort.Skipped("a form, prompt or menu is open")

        def run_open() -> None:
            row = staff.local_snapshot().by_current_session(SESSION)
            assert row is not None
            _ = staff.set_run(row.id, staff.OpenRun(self.root / "run"))

        cases = {
            "this session runs no production": no_production,
            "demo has no live session under parent": lambda: self.marked.clear(),
            "demo is mid-turn": mid_turn,
            "demo's pane: a form, prompt or menu is open": form,
            "demo has a /unit:direct run open": run_open,
            "has uncommitted work; it checkpoints first": lambda: (self.worktree / "draft.py").write_text("x"),
            f"child already has a running showrunner (ending {add_showrunner.EndingUnitTab(PANE)})": child_taken,
        }
        for cause, arrange in cases.items():
            with self.subTest(cause=cause):
                self.setUp()
                _ = arrange()
                code, _out, err = self.run_main()
                self.assertEqual(code, 2, err)
                self.assertIn(cause, err)
                self.assert_nothing_stopped()

    def test_an_uncommitted_child_doc_is_refused(self) -> None:
        outside = repo(self.root / "elsewhere")
        _ = (outside / "seed").write_text("seed")
        commit_all(outside)
        self.child_doc = outside / "child-production.md"
        _ = self.child_doc.write_text(doc_text(outside))
        code, _out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is not committed; the unit commits it first", err)
        self.assert_nothing_stopped()

    def test_a_retired_or_unlisted_unit_is_refused(self) -> None:
        for unit, cause in (("ghost", "ghost has no staff row under parent"), ("other", "other is retired")):
            with self.subTest(unit=unit):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = promote_showrunner.main([unit, "--production", "child", "--doc", str(self.child_doc),
                                                    "--model", "opus", "--effort", "high"])
                self.assertEqual(code, 2)
                self.assertIn(cause, err.getvalue())

    def test_default_document_reads_valid_markdown_with_any_name(self) -> None:
        self.child_doc.unlink()
        first = self.worktree / "docs/child.md"
        write_text_atomic(first, doc_text(self.worktree))
        for ignored in (".git/ignored.md", "target/ignored.md", "docs/incomplete-production.md"):
            write_text_atomic(self.worktree / ignored,
                "- **Showrunner checkout:** invalid\n" if "incomplete" in ignored else doc_text(self.worktree))
        second = self.worktree / "other.md"
        for count in (1, 2):
            with self.subTest(documents=count):
                if count == 2:
                    write_text_atomic(second, doc_text(self.worktree))
                commit_all(self.worktree)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                    code = promote_showrunner.main(["demo", "--model", "opus", "--effort", "high", "--check"])
                if count == 1:
                    self.assertEqual(code, 0, errors.getvalue())
                else:
                    self.assertEqual(code, 2)
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertIn(str(first), errors.getvalue())
                    self.assertIn(str(second), errors.getvalue())
                self.assert_nothing_stopped()

    def test_promote_requires_the_showrunner_model_and_effort(self) -> None:
        for flags in (("--model", "opus"), ("--effort", "high"), ()):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as refused:
                    _ = promote_showrunner.main(["demo", "--check", *flags])
                self.assertEqual(refused.exception.code, 2)
                self.assert_nothing_stopped()

    def test_name_only_uses_the_recorded_production_document(self) -> None:
        config = unit_config.UnitConfig(unit_config.PlanFile(self.child_doc.relative_to(self.worktree)),
                                        unit_config.NoAppPort(), ())
        _ = unit_config.write(self.worktree, config)
        for another_document in (False, True):
            if another_document:
                write_text_atomic(self.worktree / "other-production.md", doc_text(self.worktree))
                commit_all(self.worktree)
            with (self.subTest(another_document=another_document),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                code = promote_showrunner.main(["demo", "--model", "opus", "--effort", "high", "--check"])
            self.assertEqual(code, 0, errors.getvalue())
            self.assert_nothing_stopped()

    def test_name_only_without_a_production_document_reports_one_refusal_line(self) -> None:
        self.child_doc.unlink()
        with redirect_stderr(io.StringIO()) as errors:
            code = promote_showrunner.main(["demo", "--model", "opus", "--effort", "high", "--check"])
        self.assertEqual(code, 2)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("doc", errors.getvalue())
        self.assertIn(str(self.worktree), errors.getvalue())
        self.assert_nothing_stopped()

    def test_check_stops_nothing(self) -> None:
        code, out, err = self.run_main("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("check: ok — would stop demo (herdr demo), resume it as the showrunner of child", out)
        self.assert_nothing_stopped()

    def test_the_unit_is_gone_before_it_restarts(self) -> None:
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        ended_at = self.events.index("stop member " + SESSION)
        started_at = next(index for index, event in enumerate(self.events) if event.startswith("start"))
        self.assertLess(ended_at, started_at)
        self.assertIn(f"--resume {SESSION} --cwd {self.worktree}", self.events[started_at])
        self.assertIn("--model opus --effort high", self.events[started_at])
        self.assertIn("demo is now the showrunner of child", out)

    def test_unready_showrunner_names_the_owed_prompt(self) -> None:
        """cbc14765, 2026-10-20: the session opened, its prompt was refused, and the row stayed a live unit."""
        self.started = add_showrunner.SESSION_NOT_READY
        with mock.patch.object(promote_showrunner, "restore_line") as restore:
            code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""))
        restore.assert_not_called()
        self.assertIn("demo is open as the session child, but its showrunner prompt is not proven to have run.", out)
        self.assertIn("herdr session attach agents, workspace child, tab child", out)
        self.assertIn(f"/showrunner:produce {self.child_doc} resume", out)
        self.assertNotIn("add_unit.py", out)

    def test_promoted_unit_staff_retires_while_its_opening_prompt_is_still_owed(self) -> None:
        unit = self.staff_unit()
        self.started = add_showrunner.SESSION_NOT_READY
        code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""), out)
        self.assert_native_promotion(unit)
        self.assertIsNotNone(staff.by_path("child"))

    def test_promoted_unit_staff_retires_while_attachment_is_still_owed(self) -> None:
        unit = self.staff_unit()
        self.started = 1
        code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""), out)
        self.assert_native_promotion(unit)

    def test_prompted_showrunner_not_yet_attached_reports_the_promotion(self) -> None:
        self.started = 1
        code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""))
        self.assertIn("demo is now the showrunner of child", out)
        self.assertNotIn("add_unit.py", out)

    def test_promotion_refuses_unconfigured_showrunners_before_stopping_unit(self) -> None:
        """F004: promotion does not inherit the unit's previous account."""
        with mock.patch.object(accounts, "role_account", side_effect=accounts.AccountsError(
                "showrunners: not configured — accounts.py move showrunners <claudeN>")):
            code, _, error = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("accounts.py move showrunners <claudeN>", error)
        self.assert_nothing_stopped()
        self.assertFalse(accounts.groups_file().exists())

    def test_promotion_uses_showrunners_group_instead_of_retained_unit_account(self) -> None:
        selected = self.ready_showrunner_account()
        directory = selected.dir
        def checked(_child: ProductionDocument, _caller: str, ending: add_showrunner.EndingTab = add_showrunner.NO_ENDING_TAB, start_in: Path | None = None,
                    *, config_dir: Path | None = None) -> None:
            self.assertEqual(config_dir, directory)
            self.assertEqual((ending, start_in), (add_showrunner.EndingUnitTab(PANE), self.worktree))
            self.events.append("check showrunner account")
            return None
        def resolved(role: str) -> accounts.ClaudeLaunchAccount:
            return selected if role == "showrunner" else accounts.NamedAccount("claude2", self.root / ".claude2")
        with (mock.patch.object(accounts, "role_account", side_effect=resolved) as resolve,
              mock.patch.object(add_showrunner, "checked", side_effect=checked)):
            code, _, error = self.run_main()
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(resolve.call_args_list)
        self.assertIn(mock.call("showrunner"), resolve.call_args_list)
        self.assertNotIn(mock.call("unit"), resolve.call_args_list)
        start = next(event for event in self.events if event.startswith("start"))
        self.assertIn(str(directory), start)
        self.assertNotIn("--config-dir " + str(self.root / ".claude") + " ", start)
        self.assertLess(self.events.index("check showrunner account"),
            next(index for index, event in enumerate(self.events) if event.startswith("stop member ")))

    def test_promotion_resolves_showrunners_account_before_stopping_and_passes_it_to_resume(self) -> None:
        """F004: promotion uses group configuration before the stop begins."""
        selected = self.ready_showrunner_account()
        def resolved(role: str) -> accounts.ClaudeLaunchAccount:
            self.events.append("resolve " + role)
            return selected if role == "showrunner" else accounts.NamedAccount("claude2", self.root / ".claude2")
        def checked(_child: ProductionDocument, _caller: str, ending: add_showrunner.EndingTab = add_showrunner.NO_ENDING_TAB, start_in: Path | None = None,
                    *, config_dir: Path | None = None) -> None:
            self.assertEqual(config_dir, selected.dir)
            self.assertEqual((ending, start_in), (add_showrunner.EndingUnitTab(PANE), self.worktree))
            return None
        with (mock.patch.object(accounts, "role_account", side_effect=resolved),
              mock.patch.object(add_showrunner, "checked", side_effect=checked)):
            code, _, error = self.run_main()
        self.assertEqual((code, error), (0, ""))
        kill_at = next(index for index, event in enumerate(self.events) if event.startswith("stop member "))
        start_at = next(index for index, event in enumerate(self.events) if event.startswith("start"))
        self.assertLess(self.events.index("resolve showrunner"), kill_at)
        self.assertLess(kill_at, start_at)
        self.assertIn("--config-dir " + str(selected.dir), self.events[start_at])

    def test_failed_promotion_recovery_defers_account_to_units_configuration(self) -> None:
        """F004: returning to unit work also obeys units configuration."""
        selected = self.ready_showrunner_account()
        self.started = 2
        def checked(_child: ProductionDocument, _caller: str, ending: add_showrunner.EndingTab = add_showrunner.NO_ENDING_TAB, start_in: Path | None = None,
                    *, config_dir: Path | None = None) -> None:
            self.assertEqual(config_dir, selected.dir)
            self.assertEqual((ending, start_in), (add_showrunner.EndingUnitTab(PANE), self.worktree))
            return None
        def resolved(role: str) -> accounts.ClaudeLaunchAccount:
            return selected if role == "showrunner" else accounts.NamedAccount("claude2", self.root / ".claude2")
        with (mock.patch.object(accounts, "role_account", side_effect=resolved),
              mock.patch.object(add_showrunner, "checked", side_effect=checked)):
            code, output, error = self.run_main()
        self.assertEqual((code, error), (1, ""))
        start = next(event for event in self.events if event.startswith("start"))
        self.assertIn("--config-dir " + str(selected.dir), start)
        self.assertIn("add_unit.py", output)
        self.assertIn("--resume " + SESSION, output)
        self.assertNotIn("--config-dir", output)
        self.assertNotIn("--account", output)


    def test_a_unit_still_running_after_exit_is_not_restarted(self) -> None:
        with mock.patch.object(stop_resume, "stop", return_value=stop_resume.StopFailed(SESSION, "still runs")):
            code, out, _err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("stop is not proved", out)
        self.assertNotIn("stop member demo", self.events)
        self.assertFalse(any(event.startswith("start") for event in self.events))

    def native_unit(self) -> None:
        self.marked["demo"] = unit_lookup.LabeledSession(
            "demo", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID),
            herdr.HerdrPlace("native", "w1:p1"))
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"code": 0, "stdout": ""}}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HERDR_BIN": str(Path(__file__).parents[1] / "herdr/fake_herdr.py"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
        })))
        _ = cast(object, self.enterContext(mock.patch.object(
            account, "process_config_dir", return_value=accounts.main_config_dir())))

    def test_herdr_promotion_starts_on_the_showrunners_account_on_herdr(self) -> None:
        self.native_unit()
        selected = self.ready_showrunner_account()
        self.unit_account = selected
        with mock.patch.object(herdr, "stop_session") as stopped:
            code, _out, err = self.run_main()
        self.assertEqual(code, 0, err)
        stopped.assert_not_called()
        start = next(event for event in self.events if event.startswith("start"))
        self.assertIn(str(selected.dir), start)

    def test_herdr_promotion_refuses_unconfigured_showrunners_without_stopping(self) -> None:
        self.native_unit()
        with mock.patch.object(accounts, "role_account", side_effect=accounts.AccountsError(
                "showrunners: not configured — accounts.py move showrunners <claudeN>")), \
                mock.patch.object(herdr, "stop_session") as stopped:
            code, _out, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn("accounts.py move showrunners <claudeN>", err)
        stopped.assert_not_called()
        self.assert_nothing_stopped()


    def test_another_herdr_unit_does_not_exempt_the_childs_live_destination(self) -> None:
        self.marked["demo"] = unit_lookup.LabeledSession(
            "demo", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID),
            herdr.HerdrPlace("demo", PANE))
        with mock.patch.object(add_showrunner, "checked", CHILD_PREFLIGHT), \
                mock.patch.object(showrunner_tree, "descendants_of", return_value=[]), \
                mock.patch.object(add_showrunner, "running", return_value=False), \
                mock.patch.object(add_showrunner, "herdr_tab_open", return_value=True) as live, \
                mock.patch.object(trust_reader, "claude_trust", return_value=trust_reader.Trusted(
                    self.worktree, self.root / ".claude.json", self.worktree)):
            code, _out, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn("a tab named child is already open in herdr session agents", err)
        live.assert_called_once_with("child", add_showrunner.EndingUnitTab(PANE))
        self.assert_nothing_stopped()
        self.assertFalse(any(event.startswith("stop member ") for event in self.events))

    def test_promotion_check_exempts_its_own_tab_in_the_shared_session(self) -> None:
        self.marked["demo"] = unit_lookup.LabeledSession(
            "demo", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID),
            herdr.HerdrPlace("agents", PANE))
        with (mock.patch.object(add_showrunner, "checked", CHILD_PREFLIGHT),
              mock.patch.object(showrunner_tree, "descendants_of", return_value=[]),
              mock.patch.object(add_showrunner, "running", return_value=False),
              mock.patch.object(herdr, "shared_session", return_value="agents"),
              mock.patch.object(herdr, "live_sessions", return_value=[herdr.SessionEntry(
                  name="agents", running=True, default=False, session_dir="/fixture", socket_path="/sock")]),
              mock.patch.object(herdr.Herdr, "panes", return_value=[{"pane_id": PANE, "label": "child"}]),
              mock.patch.object(trust_reader, "claude_trust", return_value=trust_reader.Trusted(
                  self.worktree, self.root / ".claude.json", self.worktree))):
            code, out, err = self.run_main("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("check: ok", out)
        self.assert_nothing_stopped()

    def test_a_failed_restart_prints_the_way_back_and_leaves_the_row(self) -> None:
        self.started = 2
        code, out, _err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("demo is stopped and did not start as a showrunner.", out)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/add_unit.py --production "
                      + f"parent demo --plan docs/demo.md --resume {SESSION} --cwd {self.worktree}", out)

    def child_with_open_tabs(self, panes: list[herdr.PaneIdentity]) -> ProductionDocument:
        child_doc = self.worktree / "builds-production.md"
        _ = child_doc.write_text(doc_text(self.worktree))
        child = read_new_production(child_doc, "child")
        _ = self.enterContext(mock.patch.object(add_showrunner, "running", return_value=False))
        _ = self.enterContext(mock.patch.object(showrunner_tree, "descendants_of", return_value=[]))
        _ = self.enterContext(mock.patch.object(herdr, "shared_session", return_value="agents"))
        _ = self.enterContext(mock.patch.object(herdr, "live_sessions", return_value=[herdr.SessionEntry(
            name="agents", running=True, default=False, session_dir="/fixture", socket_path="/sock")]))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "panes", return_value=panes))
        return child

    def test_the_childs_check_lets_the_units_own_tab_carry_its_name(self) -> None:
        ending_place = herdr.HerdrPlace("agents", PANE)
        child = self.child_with_open_tabs([pane_identity(ending_place.pane, "child")])
        ORIGINAL_CHECKED(child, "parent", ending=add_showrunner.EndingUnitTab(ending_place.pane),
                         config_dir=self.unit_account.dir)

    def test_the_childs_check_refuses_another_same_named_tab_beside_the_ending_unit(self) -> None:
        child = self.child_with_open_tabs([
            pane_identity(PANE, "child"),
            pane_identity("w8:p2", "child"),
        ])
        with self.assertRaisesRegex(Refusal, "^a tab named child is already open in herdr session agents$"):
            ORIGINAL_CHECKED(child, "parent", ending=add_showrunner.EndingUnitTab(PANE),
                             config_dir=self.unit_account.dir)

    def test_the_childs_check_is_for_the_units_worktree(self) -> None:
        self.marked["demo"] = unit_lookup.LabeledSession(
            "demo", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID),
            herdr.HerdrPlace("agents", PANE))
        code, _out, err = self.run_main("--check")
        self.assertEqual(code, 0, err)
        self.assertIn(f"check child for parent, ending {add_showrunner.EndingUnitTab(PANE)}, "
                      + f"starting in {self.worktree}", self.events)


if __name__ == "__main__":
    _ = unittest.main()
