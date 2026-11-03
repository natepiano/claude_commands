"""Restart staff rows without a session journal."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, override
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory
import herdr
import sessions
import restart
from unit_staff_test_support import unit_host
from scripts.production import staff
from scripts.shutdown import shutdown_record, stop_resume, pane_record
from test_support import ShutdownTestCase


def checkout() -> inventory.GitCheckout:
    return inventory.GitCheckout(
        kind="git",
        path="/tmp/checkout",
        head=inventory.OnBranch(kind="branch", name="work"),
        upstream=inventory.Tracking(kind="tracking", ahead=0),
        dirty=[],
    )


def common_session(
    session_id: str,
    name: str | None = None,
    *,
    cwd: str | None = None,
    model: inventory.LastModel | None = None,
    run_dirs: list[str] | None = None,
) -> inventory.SessionFields:
    return {
        "session_id": session_id,
        "config_dir": str(Path.home() / ".claude2"),
        "pid": 1000 + len(session_id),
        "proc_start": f"start-{session_id}",
        "name": name or session_id,
        "cwd": cwd or f"/tmp/{session_id}",
        "status": "idle",
        "updated_at": 0,
        "probe": False,
        "model": model or inventory.NoReplyYet(kind="no reply yet"),
        "checkout": checkout(),
        "run_dirs": list(run_dirs or []),
        "codex_servers": [],
        "timers": [],
    }


def top_level(
    session_id: str,
    *,
    name: str | None = None,
    cwd: str | None = None,
    model: inventory.LastModel | None = None,
    host: inventory.SessionHost | None = None,
    run_dirs: list[str] | None = None,
    status: str = "idle",
) -> inventory.TopLevelSession:
    result = inventory.TopLevelSession(
        **common_session(
            session_id,
            name,
            cwd=cwd,
            model=model,
            run_dirs=run_dirs,
        ),
        kind="top-level",
        host=host or inventory.UnknownHost(kind="unknown"),
    )
    result["status"] = status
    return result


def showrunner(
    session_id: str,
    *,
    host: inventory.SessionHost | None = None,
) -> inventory.ShowrunnerSession:
    return inventory.ShowrunnerSession(
        **common_session(session_id),
        kind="showrunner",
        host=host or inventory.HerdrHost(kind="herdr", herdr_session=f"herdr-{session_id}", pane_id="w1:p1", presentation="detached"),
        production="demo",
        doc="/tmp/demo-production.md",
    )


def unit(
    session_id: str,
    *,
    plan: inventory.UnitPlan | None = None,
    session_name: str | None = None,
    session_host: Literal["herdr"] = "herdr",
    run_dirs: list[str] | None = None,
    doc: str = "/tmp/demo production.md",
) -> inventory.UnitSession:
    return inventory.UnitSession(
        **common_session(session_id, run_dirs=run_dirs),
        kind="unit",
        host=unit_host(
            kind="unit",
            production="demo",
            unit=f"{session_id}-unit",
            doc=doc,
            session_name=session_name or f"herdr-{session_id}",
            session_host=session_host,
            plan=plan or inventory.PlanFromRunRecord(kind="plan", path=f"/tmp/{session_id} plan.md"),
        ),
    )


def worker_session(
    session_id: str,
    *,
    owner: str | None,
    run_dirs: list[str] | None = None,
) -> inventory.WorkerSession:
    worker_owner: inventory.WorkerOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.WorkerSession(
        **common_session(session_id, run_dirs=run_dirs),
        kind="worker",
        host=inventory.UnknownHost(kind="unknown"),
        owner=worker_owner,
    )


class SessionLaunchTests(ShutdownTestCase):
    def test_restart_reads_plugin_directories_from_the_shutdown_pane_record(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        directories = [str(root / "postmaster"), str(root / "second plugin")]
        entry = pane_record.launch("w1:p2", "saved", str(root), {},
            ["claude", "--plugin-dir", directories[0], "--plugin-dir=" + directories[1]])
        with (patch.object(pane_record, "RECORD_DIR", root / "panes"),
              patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"})):
            pane_record.write("agents", entry)
            kind, environment, arguments = restart.session_launch("saved")
        self.assertEqual(kind, "claude")
        self.assertEqual(environment, {"CLAUDE_CONFIG_DIR": "/account"})
        parsed = pane_record.parse_claude_arguments(["claude", *arguments])
        self.assertEqual(parsed.plugin_directories, tuple(directories))
        self.assertEqual(parsed.values["--resume"], "saved")
        self.assertEqual(arguments.count("--plugin-dir"), 2)


class RestartTests(ShutdownTestCase):
    root: Path = Path()
    rows: tuple[staff.StaffRow, ...] = ()
    running: set[str] = set()
    launches: list[tuple[str, herdr.NewTab | herdr.SplitPane | herdr.ExistingPane]] = []
    stops: list[str] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root),
                                                     "XDG_STATE_HOME": str(self.root)})))
        self.rows = tuple(staff.StaffRow(staff.StaffId(f"row-{n}"), "unit", f"unit-{n}", None,
            current_session=staff.StaffId(f"conversation-{n}"),
            worktree=staff.PresentWorktree("natedev", self.root)) for n in range(3))
        self.running = {str(row.current_session) for row in self.rows}
        self.launches = []
        self.stops = []
        record = staff.StaffSnapshot(self.rows)
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=record))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.liveness))
        _ = self.enterContext(patch.object(stop_resume, "stop", side_effect=self.stop))
        _ = self.enterContext(patch.object(restart, "session_launch", side_effect=self.session_launch))
        _ = self.enterContext(patch.object(herdr, "prepare_session_environment",
            return_value=herdr.PreparedAgentEnvironment(self.root, {})))
        _ = self.enterContext(patch.object(herdr, "launch_prepared_agent", side_effect=self.launch))
        _ = self.enterContext(patch.object(herdr, "restore_server"))
        _ = self.enterContext(patch.object(herdr, "running_codex_threads", return_value=set()))
        _ = self.enterContext(patch.object(herdr.Herdr, "save_layout"))
        _ = self.enterContext(patch.object(herdr.Herdr, "remember_row_tab"))
        _ = self.enterContext(patch.object(herdr.Herdr, "tab_sessions", return_value=[]))
        _ = self.enterContext(patch.object(herdr.Herdr, "result", side_effect=self.layout_result))
        _ = self.enterContext(patch.object(herdr.Herdr, "panes", side_effect=self.panes))
        _ = self.enterContext(patch.object(herdr.Herdr, "native_session", side_effect=self.native))
        _ = self.enterContext(redirect_stderr(io.StringIO()))
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "saved", "seen": 1,
            "tabs": [{"name": row.name, "seen": 1, "row_id": str(row.id)} for row in reversed(self.rows)]}]}
        _ = self.enterContext(patch.object(herdr, "saved_layout", return_value=layout))

    @staticmethod
    def session_launch(identity: str) -> tuple[Literal["claude"], dict[str, str], list[str]]:
        return "claude", {}, ["--resume", identity]

    def layout_result(self, *arguments: str) -> dict[str, object]:
        if arguments == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "saved", "label": "saved"}]}
        if arguments == ("tab", "list"):
            return {"tabs": []}
        raise AssertionError(arguments)

    def liveness(self, identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return sessions.LiveSessionProcess(123) if identity in self.running else sessions.SessionNotLive()

    def panes(self) -> list[herdr.PaneIdentity]:
        return [{"pane_id": f"w1:p{n}", "workspace_id": "w1", "session_id": row.current_session,
                 "terminal_id": f"terminal-{n}"} for n, row in enumerate(self.rows)
                if row.current_session in self.running]

    def native(self, pane: str, _kind: str, _timeout: float) -> str:
        return str(self.rows[int(pane.rsplit("p", 1)[1])].current_session)

    def stop(self, identity: str) -> stop_resume.Stopped:
        row = next(row for row in self.rows if row.current_session == identity)
        self.stops.append(str(row.id))
        self.running.discard(str(row.current_session))
        return stop_resume.Stopped(str(row.current_session), 123)

    def launch(self, _name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
               _prepared: herdr.PreparedAgentEnvironment, _kind: str, arguments: list[str],
               _opening: herdr.NoOpeningPrompt, _timeout: int, *, agent: str = "") -> herdr.AgentLaunched:
        row = next(row for row in self.rows if row.name == agent)
        self.assertEqual(arguments, ["--resume", str(row.current_session), "--name", row.name])
        self.assertIsInstance(_opening, herdr.NoOpeningPrompt)
        self.assertEqual(_prepared.directory, self.root)
        self.launches.append((agent, destination))
        self.running.add(str(row.current_session))
        return herdr.AgentLaunched(f"w1:p{self.rows.index(row)}", herdr.PromptSubmissionNotRequired())

    def test_restart_all_dry_run_lists_every_live_row_with_its_tab_position(self) -> None:
        self.rows = (
            replace(self.rows[0], name="unit"),
            replace(self.rows[1], kind="worker", name="codex-worker", parent=self.rows[0].id,
                    worktree=staff.WORKTREE_UNFILLED),
            replace(self.rows[2], kind="agent", name="morty", parent=None,
                    worktree=staff.WORKTREE_UNFILLED, doc=staff.LiveDoc("natedev", self.root / "morty.md")),
        )
        record = staff.StaffSnapshot(self.rows)
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "agents", "seen": 1,
            "tabs": [{"name": row.name, "seen": 1, "row_id": str(row.id)} for row in self.rows]}]}
        self.running.remove(str(self.rows[1].current_session))
        shutdown_record.save([str(self.rows[2].id)])
        before = shutdown_record.record_path().read_bytes()
        for saved in (True, False):
            with self.subTest(saved=saved):
                with (patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(herdr, "running_codex_threads", return_value={str(self.rows[1].current_session)}),
                      patch.object(herdr, "saved_layout", return_value=layout if saved else {"at": 1, "workspaces": []}),
                      patch.object(staff, "row_operation") as lock,
                      patch.object(herdr.Herdr, "save_layout") as save,
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
                self.assertEqual(output.getvalue().splitlines(), [
                    f"{row.name}: {row.id}, {row.kind}, " + (
                        f"workspace agents, tab {index} ({row.name})" if saved else "no saved tab")
                    for index, row in enumerate(self.rows, 1)])
                lock.assert_not_called()
                save.assert_not_called()
                self.assertEqual(shutdown_record.record_path().read_bytes(), before)
                self.assertEqual(self.stops, [])
                self.assertEqual(self.launches, [])
                self.assertEqual(restart.row_folder(self.rows[2], record), ("natedev", self.root))
                self.assertEqual(restart.row_folder(replace(self.rows[2],
                    worktree=staff.PresentWorktree("natedev", self.root / "own")), record),
                    ("natedev", self.root / "own"))

    def test_restart_lock_refusal_leaves_the_record_unchanged(self) -> None:
        reference = str(self.rows[0].id)
        for stop_all in (False, True):
            for saved in ([], [reference, "another-pending-row"]):
                with self.subTest(stop_all=stop_all, saved=saved):
                    shutdown_record.record_path().unlink(missing_ok=True)
                    if saved:
                        shutdown_record.save(saved)
                    path = shutdown_record.record_path()
                    before = path.read_bytes() if path.exists() else None
                    with (patch.object(staff, "row_operation", side_effect=staff.StaffRefused("row is locked")),
                          patch.object(restart, "start_row") as start,
                          redirect_stderr(io.StringIO()) as errors):
                        self.assertEqual(restart.restart(stop_all=stop_all, only=(reference,)), 0)
                    self.assertEqual(path.read_bytes() if path.exists() else None, before)
                    start.assert_not_called()
                    expected = self.rows if stop_all else self.rows[:1]
                    self.assertEqual(errors.getvalue().splitlines(),
                        [f"{row.name}: restart anomaly: row is locked" for row in expected])

    def test_restart_record_delta_keeps_another_runs_failed_row(self) -> None:
        self.running.clear()
        other = "another-failed-row"

        def start(_row: staff.StaffRow, _record: staff.StaffSnapshot, _client: herdr.Herdr, *, present: bool) -> None:
            _ = present
            shutdown_record.update(started=[], pending=[other])

        shutdown_record.save([str(self.rows[0].id), str(self.rows[1].id)])
        with patch.object(restart, "start_row", side_effect=start):
            self.assertEqual(restart.restart(only=(str(self.rows[0].id),)), 0)
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id), other])

    def test_started_row_leaves_pending_when_layout_save_fails(self) -> None:
        self.running.clear()
        shutdown_record.save([str(self.rows[0].id)])
        with (patch.object(herdr.Herdr, "remember_row_tab", side_effect=herdr.HerdrFailure("layout unavailable")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertIn(str(self.rows[0].current_session), self.running)
        self.assertEqual(errors.getvalue(), f"{self.rows[0].name}: restart anomaly: layout unavailable\n")

    def test_saved_tab_reuses_only_the_targets_shell_and_splits_beside_a_sibling(self) -> None:
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "saved", "seen": 1,
            "tabs": [{"name": "unit-0", "seen": 1, "row_id": "row-0", "terminal_id": "own-terminal"}]}]}

        def result(*arguments: str) -> dict[str, object]:
            if arguments == ("workspace", "list"):
                return {"workspaces": [{"workspace_id": "w1", "label": "saved"}]}
            if arguments == ("tab", "list"):
                return {"tabs": [{"workspace_id": "w1", "tab_id": "t1", "label": "unit-0"}]}
            if arguments == ("pane", "list"):
                return {"panes": [{"pane_id": "w1:sibling", "tab_id": "t1", "terminal_id": "sibling-terminal"},
                                  {"pane_id": "w1:own", "tab_id": "t1", "terminal_id": "own-terminal"}]}
            raise AssertionError(arguments)

        client = herdr.Herdr("fixture")
        with (patch.object(herdr, "saved_layout", return_value=layout),
              patch.object(client, "result", side_effect=result),
              patch.object(client, "pane_activity", return_value=herdr.PaneAtShell()) as activity):
            self.assertEqual(client.saved_row_tab("row-0", "unit-0"), herdr.ExistingPane("w1:own", "own-terminal"))
            activity.assert_called_once_with("w1:own")
            activity.return_value = herdr.PaneRunning(("codex",))
            self.assertEqual(client.saved_row_tab("row-0", "unit-0"), herdr.SplitPane("w1:sibling", "right", 0.5, "unit-0"))
            layout["workspaces"][0]["tabs"][0]["terminal_id"] = "missing-terminal"
            activity.reset_mock()
            self.assertEqual(client.saved_row_tab("row-0", "unit-0"), herdr.SplitPane("w1:sibling", "right", 0.5, "unit-0"))
            activity.assert_not_called()

    def test_restart_all_starts_each_row_in_its_saved_tab(self) -> None:
        self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(self.stops, [str(row.id) for row in self.rows])
        self.assertEqual(self.launches, [(row.name, herdr.NewTab("saved", row.name)) for row in self.rows])
        self.assertEqual(self.running, {str(row.current_session) for row in self.rows})

    def test_restart_all_rerun_starts_only_the_rows_that_failed(self) -> None:
        launch = self.launch
        def fail_one(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                     prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                     opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "") -> herdr.AgentLaunched:
            if agent == self.rows[1].name:
                raise OSError("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent)
        with patch.object(herdr, "launch_prepared_agent", side_effect=fail_one):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.launches.clear()
        self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.launches, [(self.rows[1].name, herdr.NewTab("saved", self.rows[1].name))])
        self.launches.clear()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.launches, [])
        self.assertEqual(output.getvalue(), "")

    def test_restart_all_record_keeps_only_rows_that_failed_to_start(self) -> None:
        self.running.remove(str(self.rows[2].current_session))
        launch = self.launch

        def fail_one(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                     prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                     opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "") -> herdr.AgentLaunched:
            if agent == self.rows[1].name:
                raise OSError("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent)

        with patch.object(herdr, "launch_prepared_agent", side_effect=fail_one):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id)])
        self.assertEqual(self.stops, [str(row.id) for row in self.rows[:2]])
        self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertEqual(self.running, {str(row.current_session) for row in self.rows[:2]})

    def test_restart_all_writes_only_live_row_ids(self) -> None:
        self.running.remove(str(self.rows[1].current_session))
        self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
        self.assertFalse(shutdown_record.record_path().exists())
        self.assertEqual(self.stops, [])
        self.assertEqual(self.launches, [])
        with patch.object(restart, "start_row", side_effect=OSError("fixture launch failed")):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(json.loads(shutdown_record.record_path().read_text()), [self.rows[0].id, self.rows[2].id])

    def test_restart_all_skips_a_stop_when_the_pane_runs_another_conversation(self) -> None:
        with (patch.object(herdr.Herdr, "native_session", return_value="another-conversation"),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.launches, [])
        self.assertIn("pane runs another conversation; no signal sent", errors.getvalue())
