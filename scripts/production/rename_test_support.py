"""Private Git fixtures and in-process production rename commands."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar, Literal, cast, override
from unittest import mock
from collections.abc import Callable, Generator, Sequence, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from types import SimpleNamespace


from scripts.production import staff, unit_config, fake_unit_host
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, StaffTestCase, isolated_source_readers
from scripts.lib.atomic_file import write_text_atomic
from scripts.herdr import fake_herdr
from scripts.herdr import herdr
from scripts.shutdown import pane_record, remote
from scripts.direct import progress_history
from scripts.message import sessions
from scripts.message import sessions as stop_sessions
from scripts.direct.recorder_state import STATE_FILENAME
from scripts.production import merge_checkpoint
from scripts.production.production_doc import Production, production_field, read_production
from scripts.production import rename
from scripts.production import showrunners
from scripts.production import tidy_wrapped
from scripts.production import unit_move
from scripts.production import unit_names
from scripts.production import unit_lookup
from scripts.production.cli_fixture import invoke_cli, process_boundary_for_case
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SCRIPT = Path(__file__).with_name("rename.py")
READ_HERDR_PANES = herdr.Herdr.panes


class RenameDisplays:
    """Keep private display state while the real retitle methods run."""

    def __init__(self, row: staff.StaffRow, *, live: bool = True, backend: str = "claude") -> None:
        self.row: staff.StaffRow = row
        self.live: bool = live
        self.backend: str = backend
        self.pane: str = "previous"
        self.tab: str = "previous"
        self.workspace: str = "previous"
        self.commands: list[tuple[str, ...]] = []

    def listed(self, kind: str, *_arguments: str, **_keywords: object) -> list[dict[str, object]]:
        if kind == "pane":
            return ([{"pane_id": "probe-pane", "tab_id": "probe-tab", "workspace_id": "probe-space",
                      "terminal_id": "probe-terminal", "label": self.pane,
                      "agent_session": {"value": self.row.current_session}}] if self.live else [])
        if kind == "tab":
            return [{"tab_id": "probe-tab", "workspace_id": "probe-space", "label": self.tab}]
        if kind == "workspace":
            return [{"workspace_id": "probe-space", "label": self.workspace}]
        raise AssertionError(f"unexpected listing: {kind}")

    def run(self, *arguments: str, **_keywords: object) -> str:
        self.commands.append(arguments)
        if len(arguments) == 4 and arguments[1] == "rename":
            if arguments[0] == "pane":
                self.pane = arguments[3]
            elif arguments[0] == "tab":
                self.tab = arguments[3]
            elif arguments[0] == "workspace":
                self.workspace = arguments[3]
            else:
                raise AssertionError(arguments)
            return ""
        raise AssertionError(arguments)

    def result(self, *arguments: str, **_keywords: object) -> dict[str, object]:
        if arguments[:2] != ("pane", "process-info"):
            raise AssertionError(arguments)
        return {"process_info": {"shell_pid": 998040, "foreground_process_group_id": 998041,
                "foreground_processes": [{"pid": 998041, "name": self.backend, "argv": [self.backend]}]}}

    def native_result(self, arguments: list[str], **_keywords: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"result": self.result(*arguments[3:])}), "")

    @contextmanager
    def installed(self) -> Generator[None]:
        """Replace backend reads and writes, and retain the real retitle behavior."""
        from scripts.herdr import herdr

        with (mock.patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}),
              mock.patch.object(herdr.Herdr, "panes", READ_HERDR_PANES),
              mock.patch.object(herdr.Herdr, "listed", side_effect=self.listed),
              mock.patch.object(herdr.Herdr, "run", side_effect=self.run),
              mock.patch.object(herdr.Herdr, "result", side_effect=self.result),
              mock.patch.object(herdr, "run_program", side_effect=self.native_result),
              mock.patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.RolloutThread(self.row.current_session) if self.backend == "codex" else
                  herdr.EnvironmentThread(self.row.current_session), "probe-generation", {})),
              mock.patch.object(herdr, "_LayoutLock"),
              mock.patch.object(herdr.Herdr, "order_tabs", return_value=False),
              mock.patch.object(herdr.Herdr, "save_layout")):
            yield



class FolderMoveBackend:
    """A private backend keeps the terminal while its session stops and resumes."""

    def __init__(self, fixture: "FolderMoveFixture", kind: Literal["claude", "codex"], live: bool) -> None:
        self.fixture: FolderMoveFixture = fixture
        self.kind: Literal["claude", "codex"] = kind
        self.live: bool = live
        self.pid: int = 998041
        self.shell_pid: int = 998040
        self.starts: list[tuple[str, list[str]]] = []
        self.stops: list[str] = []
        self.session: str = "agents"
        self.cwd: Path = fixture.old_folder
        self.config: Path = fixture.root / "home" / (".claude4" if kind == "claude" else ".codex2")

    def publish_process_state(self) -> None:
        """Expose the process generation and its current Codex rollout."""
        environment = {"HERDR_PROC": str(self.fixture.root / "folder-processes")}
        fake_unit_host.process(environment, self.shell_pid, 1, ["zsh"], {}, cwd=self.cwd)
        arguments = [self.kind] if self.kind == "claude" else [self.kind, "resume", self.fixture.owner.current_session]
        fake_unit_host.process(environment, self.pid, self.shell_pid if self.live else 1, arguments if self.live else [],
                               self.environment(self.pid), cwd=self.cwd)
        process = Path(environment["HERDR_PROC"]) / str(self.pid)
        descriptors = process / "fd"
        descriptors.mkdir(exist_ok=True)
        descriptor = descriptors / "4"
        descriptor.unlink(missing_ok=True)
        if self.live and self.kind == "codex":
            record = self.fixture.root / "rollout-folder-current.jsonl"
            _ = record.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.fixture.owner.current_session}}) + "\n")
            descriptor.symlink_to(record)

    def pane_row(self, pane: str) -> herdr.PaneIdentity | herdr.NoPane:
        return next((row for row in self.panes() if row["pane_id"] == pane), herdr.NoPane(pane))

    def panes(self) -> list[herdr.PaneIdentity]:
        return [
            {"pane_id": "w1:p1", "terminal_id": "kept-terminal", "workspace_id": "w1",
             "label": "kept-name", "session_id": self.fixture.owner.current_session if self.live else None},
            {"pane_id": "w1:p2", "terminal_id": "neighbour-terminal", "workspace_id": "w1",
             "label": "neighbour", "session_id": fixture_runtime_id("folder-move-neighbour")},
        ]

    def agents(self) -> list[herdr.AgentOccupant]:
        return ([{"pane_id": "w1:p1", "agent": self.kind, "agent_status": herdr.AgentStatus.RUNNING,
                  "agent_session": herdr.NoReportedAgentSession()}]
                if self.live else [])

    def observed_processes(self, **_options: object) -> list[herdr.HerdrProcess]:
        if not self.live:
            return []
        return [herdr.HerdrProcess(self.pid, herdr.HerdrPlace(self.session, "w1:p1"),
            self.environment(self.pid), self.kind + " resume " + self.fixture.owner.current_session, str(self.cwd))]

    def pane_activity(self, pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
        entry = pane_record.read(self.session)[pane]
        self.fixture.assertIn(entry["session_id"], (self.fixture.owner.id, self.fixture.owner.current_session))
        return herdr.PaneRunning((self.kind,)) if self.live else herdr.PaneAtShell()

    def result(self, *arguments: str) -> dict[str, object]:
        self.fixture.assertEqual(arguments, ("pane", "process-info", "--pane", "w1:p1"))
        argv = ([self.kind] if self.kind == "claude" else [self.kind, "resume", self.fixture.owner.current_session]) if self.live else ["zsh"]
        foreground: list[dict[str, object]] = [{"pid": self.pid if self.live else self.shell_pid, "argv": argv, "cwd": str(self.cwd)}]
        return {"process_info": {"shell_pid": self.shell_pid, "cwd": str(self.cwd),
                                 "foreground_process_group_id": self.pid if self.live else self.shell_pid,
                                 "foreground_processes": foreground}}

    def saved_row_tab(self, row_id: str, title: str, fallback_workspace: str) -> herdr.ExistingPane:
        _ = fallback_workspace
        self.fixture.assertEqual((row_id, title), (str(self.fixture.owner.id), self.fixture.owner.name))
        return herdr.ExistingPane("w1:p1", "kept-terminal")

    def remember_row_tab(self, row_id: str, pane: str) -> None:
        self.fixture.assertEqual((row_id, pane), (str(self.fixture.owner.id), "w1:p1"))

    def focus_tab_of(self, pane: str) -> None:
        self.fixture.assertEqual(pane, "w1:p1")

    def run_in_pane(self, pane: str, command: list[str], *, terminal: str = "") -> None:
        self.fixture.assertEqual((pane, terminal), ("w1:p1", "kept-terminal"))
        self.fixture.assertFalse(self.live, "a live session must not start again")
        self.fixture.assertEqual(self.agents(), [])
        self.fixture.assertEqual(command[0], "eval")
        words = shlex.split(command[1])
        self.cwd = Path(words[words.index("cd") + 2])
        self.fixture.assertTrue(self.cwd.is_dir())

    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                    timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
        self.fixture.assertFalse(self.live, "a live session must not start again")
        self.fixture.assertEqual((name, pane, kind), (self.fixture.owner.name, "w1:p1", self.kind))
        self.fixture.assertIn(self.fixture.owner.current_session, agent_arguments)
        self.fixture.assertIsInstance(opening, herdr.NoOpeningPrompt)
        self.fixture.assertGreater(timeout_ms, 0)
        self.starts.append((pane, agent_arguments.copy()))
        entry = pane_record.launch(pane, self.fixture.owner.current_session, str(self.cwd),
            self.environment(self.pid), [kind, *agent_arguments], kind=self.kind)
        entry["name"] = name
        pane_record.write(self.session, entry)
        self.live = True
        self.publish_process_state()
        return herdr.PromptSubmissionNotRequired()

    def close_tab_confirmed(self, pane: str) -> None:
        raise AssertionError(f"a folder resume must keep its pane: {pane}")

    def environment(self, pid: int) -> dict[str, str]:
        self.fixture.assertEqual(pid, self.pid)
        return ({"CLAUDE_CONFIG_DIR": str(self.config)} if self.kind == "claude" else
                {"CODEX_HOME": str(self.config), "CODEX_THREAD_ID": self.fixture.owner.current_session})


class FolderMoveFixture(StaffTestCase):
    """Keep a folder move's Git, staff, launch, and run records in scratch state."""

    checkout: Path = Path()
    old_folder: Path = Path()
    new_folder: Path = Path()
    run_dir: Path = Path()
    history: Path = Path()
    records: Path = Path()
    owner: staff.StaffRow = staff.StaffRow(
        staff.StaffId("019b3a40-4250-7000-8000-000000000101"), "unit", "fixture", staff.StaffRoot(),
        current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000101"))

    @override
    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.root / "checkout"
        self.old_folder = self.root / "worktrees/source-folder"
        self.new_folder = self.root / "elsewhere/destination-folder"
        self.run_dir = self.root / "sessions/move-run"
        self.history = self.root / "history/runs/move-run.jsonl"
        self.records = self.root / "pane-records"
        self.checkout.mkdir()
        self.run_dir.mkdir(parents=True)
        self.history.parent.mkdir(parents=True)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "UNIT_HISTORY_DIR": str(self.history.parent.parent),
            "UNIT_RUN_ROOT": str(self.root / "sessions"),
            "PROGRESS_SESSION_ROOTS": str(self.run_dir.parent),
            "HERDR_PANE_RECORD_DIR": str(self.records),
            "HERDR_BIN": "/nonexistent/herdr",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        })))
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.records))
        _ = self.enterContext(mock.patch.object(progress_history, "SESSION_ROOTS", (self.run_dir.parent,)))
        _ = self.folder_git(self.checkout, "init", "-b", "kept-branch")
        _ = self.folder_git(self.checkout, "config", "user.name", "Folder Move Test")
        _ = self.folder_git(self.checkout, "config", "user.email", "move@example.invalid")
        write_text_atomic(self.checkout / "plan.md", "# Unit plan\n")
        _ = self.folder_git(self.checkout, "add", "plan.md")
        _ = self.folder_git(self.checkout, "commit", "-m", "Create the fixture")
        _ = self.folder_git(self.checkout, "worktree", "add", "-b", "unit-branch", str(self.old_folder))
        parent = fixture_member("showrunner", name="production")
        fixed = fixture_member("unit", name="kept-name", parent=parent.id,
                               worktree=staff.PresentWorktree(remote.local_machine(), self.old_folder))
        self.owner = staff.update_current_session(fixed.current_session,
            staff.native_id(fixture_runtime_id("folder-move-current")))
        launch = pane_record.launch("w1:p1", self.owner.id, str(self.old_folder),
            {"CLAUDE_CONFIG_DIR": str(self.root / "home/.claude4")},
            ["claude", "-n", "kept-name", "--model", "opus"])
        launch["ended"] = True
        pane_record.write("agents", launch)
        sibling = pane_record.launch("w1:p2", fixture_runtime_id("folder-move-neighbour"),
            str(self.root / "neighbour"), {}, ["claude", "-n", "neighbour"])
        pane_record.write("agents", sibling)
        fields: dict[str, object] = {
            "run_id": "move-run", "main_agent": {"session_id": self.owner.current_session},
            "branch": "unit-branch", "plan_doc": "plan.md",
            "history_file": str(self.history), "status": "active", "production": "production", "unit": "kept-name",
        }
        write_text_atomic(self.history, json.dumps({**fields, "event_type": "run_started", "run_started_at": 1}) + "\n")
        write_text_atomic(self.run_dir / STATE_FILENAME, json.dumps(fields) + "\n")

    def folder_git(self, checkout: Path, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(checkout), *arguments],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def folder_backend(self, kind: Literal["claude", "codex"] = "claude", *, live: bool = False) -> FolderMoveBackend:
        backend = FolderMoveBackend(self, kind, live)
        backend.publish_process_state()
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(
            os.environ, {"HERDR_PROC": str(self.root / "folder-processes"), "SHUTDOWN_PROOF_WORKSPACE": "w1"})))
        account_key = "CLAUDE_CONFIG_DIR" if kind == "claude" else "CODEX_HOME"
        launch = pane_record.launch("w1:p1", self.owner.id, str(self.old_folder),
            {account_key: str(backend.config)}, [kind, "-n", "kept-name", "--model", "opus"], kind=kind)
        launch["ended"] = not live
        pane_record.write("agents", launch)

        def session_liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            self.assertEqual(identity, self.owner.current_session)
            return (sessions.LiveSessionProcess(backend.pid) if backend.live and kind == "claude"
                    else sessions.SessionNotLive())

        def process_liveness(pid: int, started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            self.assertEqual((pid, started), (backend.pid, "fixture-start"))
            return sessions.LiveSessionProcess(pid) if backend.live else sessions.SessionNotLive()

        def stop_process_liveness(pid: int, started: str) -> stop_sessions.LiveSessionProcess | stop_sessions.SessionNotLive:
            self.assertEqual((pid, started), (backend.pid, "fixture-start"))
            return (stop_sessions.LiveSessionProcess(pid) if backend.live
                    else stop_sessions.SessionNotLive())

        def signal_process(pid: int, signal: int) -> None:
            import signal as signals

            self.assertEqual((pid, signal), (backend.pid, signals.SIGTERM))
            self.assertTrue(backend.live)
            self.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), True)
            backend.stops.append(self.owner.current_session)
            backend.live = False
            backend.publish_process_state()

        for target, attribute, replacement in (
            (herdr, "Herdr", mock.Mock(return_value=backend)),
            (herdr, "shared_session", mock.Mock(return_value="agents")),
            (pane_record, "seed", mock.Mock(return_value=0)),
            (herdr, "herdr_processes", backend.observed_processes),
            (herdr, "process_environment", backend.environment),
            (sessions, "session_liveness", session_liveness),
            (sessions, "process_liveness", process_liveness),
            (sessions, "process_start", mock.Mock(return_value="fixture-start")),
            (stop_sessions, "process_liveness", stop_process_liveness),
            (stop_sessions, "process_start", mock.Mock(return_value="fixture-start")),
            (staff, "runtime_id", mock.Mock(return_value=self.owner.current_session)),
            (os, "kill", signal_process),
        ):
            _ = self.enterContext(mock.patch.object(target, attribute, replacement))
        return backend


OLD = "old"

NEW = "new"

# The staff fixture suppresses live discovery. These lifecycle fixtures provide
# their own fake host and session records, which the original readers must see.
FIXTURE_SOCKET_FOR = showrunners.socket_for
FIXTURE_LABELED_SESSIONS = unit_lookup.labeled_sessions

FAIL_GIT_MOVE_INDEX = '''import runpy, subprocess, sys
from pathlib import Path

original_run = subprocess.run
def interrupted_move(command, *args, **kwargs):
    if len(command) > 3 and command[0] == "git" and command[3] == "mv":
        checkout = Path(command[2])
        (checkout / command[-2]).replace(checkout / command[-1])
        return subprocess.CompletedProcess(command, 1, "", "simulated git mv index failure")
    return original_run(command, *args, **kwargs)
subprocess.run = interrupted_move
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''

def run_rename(arguments: list[str], environment: dict[str, str], cwd: Path,
               wrapper: str = "") -> subprocess.CompletedProcess[str]:
    """Keep real Git and filesystem work; restore every binding the wrappers replace."""
    real_run = subprocess.run

    def run(argv: Sequence[str], *, capture_output: bool = False, text: bool = False, check: bool = False,
            timeout: float | None = None, env: dict[str, str] | None = None,
            cwd: str | Path | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
        if argv[0] == str(Path(fake_herdr.__file__)):
            return invoke_cli(fake_herdr, lambda: fake_herdr.main(list(argv[1:])), list(argv[1:]),
                              dict(os.environ) if env is None else env,
                              Path.cwd() if cwd is None else Path(cwd))
        return real_run(argv, capture_output=capture_output, text=text, check=check,
                        timeout=timeout, env=env, cwd=cwd, input=input)

    def trust_readback(seconds: float) -> None:
        # The fixtures have no concurrent Claude writer. The overwrite wrapper
        # injects that writer at each verification attempt and counts all retries.
        if seconds != 2:
            raise AssertionError(f"unexpected rename wait: {seconds}")

    # Restore file operations after each failure injection.
    with (ExitStack() as scope,
          mock.patch.object(os, "rename", os.rename),
          mock.patch.object(os, "replace", os.replace)):
        for module, fields in (
            (showrunners, {"CONFIG": Path(environment["SHOWRUNNERS_CONFIG"]),
                           "NOTIFIER_STATE_DIR": Path(environment["NOTIFIER_STATE_DIR"]),
                           "SHOWRUNNER_STATE_DIR": Path(environment["SHOWRUNNER_STATE_DIR"])}),
            (tidy_wrapped, {"STATE_DIR": Path(environment["SHOWRUNNER_STATE_DIR"]),
                            "NOTIFIER_STATE_DIR": Path(environment["NOTIFIER_STATE_DIR"])}),
        ):
            for name, value in fields.items():
                _ = scope.enter_context(mock.patch.object(module, name, value))
        _ = scope.enter_context(mock.patch.object(subprocess, "run", side_effect=run))
        _ = scope.enter_context(mock.patch.object(unit_move, "time", SimpleNamespace(sleep=trust_readback, monotonic=time.monotonic)))

        def main() -> int:
            if wrapper:
                with mock.patch.object(sys, "argv", ["-c", str(rename.__file__), *arguments]):
                    exec(compile(wrapper, "<rename failure injection>", "exec"), {"__name__": "__main__"})
                return 0
            return rename.main(arguments)

        return invoke_cli(rename, main, arguments, environment, cwd)


@contextmanager
def stop_at_staff_write(verb: Literal["rename", "move"], *, after: bool) -> Generator[None]:
    """Stop one writer just before or just after its real staff transaction."""
    original = cast(Callable[..., staff.StaffRow], getattr(staff, verb))

    def stop(*args: object, **kwargs: object) -> staff.StaffRow:
        if after:
            _ = original(*args, **kwargs)
        raise OSError("stopped at staff step")

    with mock.patch.object(staff, verb, side_effect=stop):
        yield


EMPTY_LOCATIONS: Mapping[tuple[str, str], Path] = {}


def bind_staff_fixture(case: StaffTestCase, environment: dict[str, str], members: dict[str, tuple[str, ...]],
                       *, locations: Mapping[tuple[str, str], Path] = EMPTY_LOCATIONS,
                       plans: Mapping[tuple[str, str], Path] = EMPTY_LOCATIONS) -> None:
    """Bind legacy source fixtures to the one staff fixture and seed their members."""
    case.staff_file = case.root / "state/staff/staff.json"
    environment.update(STAFF_FILE=str(case.staff_file), STAFF_TEST_OWNER_FILE=str(case.staff_file))
    _ = case.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
    _ = case.enterContext(isolated_source_readers())
    roots = tuple(Path(root) for root in environment.get(
        "PROGRESS_SESSION_ROOTS", environment.get("UNIT_RUN_ROOT", str(case.root / "runs"))).split(":"))
    _ = case.enterContext(mock.patch.object(progress_history, "SESSION_ROOTS", roots))
    for name, attribute, reader in (
        ("scripts.production.showrunners", "socket_for", FIXTURE_SOCKET_FOR),
        ("unit_lookup", "labeled_sessions", FIXTURE_LABELED_SESSIONS),
        ("scripts.production.unit_lookup", "labeled_sessions", FIXTURE_LABELED_SESSIONS),
    ):
        module = sys.modules.get(name)
        if module is not None:
            _ = case.enterContext(cast(AbstractContextManager[object], mock.patch.object(module, attribute, reader)))
    if members:
        state = Path(environment["SHOWRUNNER_STATE_DIR"])
        with mock.patch.object(tidy_wrapped, "STATE_DIR", state), unit_move.production_lock():
            pass
    productions: dict[str, Production] = {}
    for production_name in members:
        candidates = [path for path in sorted(case.root.rglob(f"{production_name}-production.md"))
                      if path.is_relative_to(Path(production_field(
                          path.read_text().splitlines(), "Showrunner checkout")).expanduser().resolve())]
        case.assertEqual(len(candidates), 1, f"{production_name}: expected one production document")
        _ = fixture_member("showrunner", name=production_name,
                           doc=staff.LiveDoc(remote.local_machine(), candidates[0]))
        production = read_production(production_name)
        productions[production_name] = production
        with merge_checkpoint.merge_lock(production.checkout, production_name):
            pass
    with staff.operation():
        for production_name, units in members.items():
            production = productions[production_name]
            row = fixture_member("showrunner", name=production_name,
                                 doc=staff.LiveDoc(remote.local_machine(), production.doc))
            for unit in units:
                name = unit_names.name(unit)
                tree = locations[(production_name, unit)]
                _ = fixture_member("unit", name=name, parent=row.id,
                                   worktree=staff.PresentWorktree(remote.local_machine(), tree))
                if not tree.is_dir():
                    continue
                plan = plans.get((production_name, unit))
                mode = unit_config.StandbyPlan() if plan is None else unit_config.PlanFile(plan)
                _ = unit_config.write(tree, unit_config.UnitConfig(mode, unit_config.NoAppPort(), ()))


class ProductionRenameFixture(StaffTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.home: Path = Path()
        self.checkout: Path = Path()
        self.origin: Path = Path()
        self.doc: Path = Path()
        self.new_doc: Path = Path()
        self.notifier: Path = Path()
        self.state: Path = Path()
        self.worktrees: Path = Path()
        self.env: dict[str, str] = {}
        self.old_tip: str = ""

    fixture_root: ClassVar[Path]

    fixture_snapshot: ClassVar[Path]

    fixture_tip: ClassVar[str]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="rename-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name).resolve() / "case"
        cls.fixture_snapshot = cls.fixture_root.with_name("snapshot")
        builder = cls()
        builder.bind_fixture(cls.fixture_root)
        builder.build_fixture()
        cls.fixture_tip = builder.old_tip
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        super().setUp()
        # Linked worktree metadata and the committed documents name absolute
        # paths. Restore private files, refs and origin at the seed's same path.
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.bind_fixture(self.fixture_root)
        self.old_tip = self.fixture_tip
        self.staff_file: Path = self.root / "state/staff/staff.json"
        self.env.update(STAFF_FILE=str(self.staff_file), STAFF_TEST_OWNER_FILE=str(self.staff_file))
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, self.env)))
        _ = self.enterContext(isolated_source_readers())
        _ = fixture_member("showrunner", name=OLD, doc=staff.LiveDoc(remote.local_machine(), self.doc))
        with staff.operation(), unit_move.production_lock():
            pass

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        self.home = self.root / "home"
        self.checkout = self.root / "checkout"
        self.origin = self.root / "origin.git"
        self.doc = self.checkout / "docs/plans/old-production.md"
        self.new_doc = self.doc.with_name("new-production.md")
        self.notifier = self.root / "state/notifier"
        self.state = self.root / "state/showrunner"
        self.worktrees = self.home / "worktrees"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "UNIT_WORKERS_STATE": str(self.root / "state/unit-workers"),
            "RETIRE_UNIT_RUN_ROOT": str(self.root / "runs"),
            "UNIT_RUN_ROOT": str(self.root / "runs"),
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "SHUTDOWN_STATE_DIR": str(self.home / ".local/state/shutdown"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "state/sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.state),
            "SHOWRUNNERS_CONFIG": str(self.home / ".claude/showrunners.json"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
            "PHASE_TABLE_VAULT": str(self.root / "vault/showrunners"),
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "HERDR_PLATFORM": "linux",
            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "CLAUDE_CODE_SESSION_ID": "", "USER_ZONE": "UTC",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        })

    def build_fixture(self) -> None:
        for path in (self.home, self.checkout, self.notifier, self.worktrees):
            path.mkdir(parents=True)
        config = self.home / ".claude"
        config.mkdir()
        helper = config / "scripts/lib/py"
        helper.parent.mkdir(parents=True)
        helper.symlink_to(SCRIPT.parents[1] / "lib/py")
        (self.root / "state/sessions").mkdir(parents=True)
        self.write(self.root / "herdr-responses.json",
                   json.dumps({"default": {"stdout": '{"sessions": []}', "code": 0},
                               "--session agents pane list": {"stdout": '{"result": {"panes": []}}', "code": 0}}))
        _ = self.git("init", "--bare", str(self.origin))
        _ = self.git("init", "-b", OLD)
        _ = self.git("config", "user.name", "Production Rename Test")
        _ = self.git("config", "user.email", "rename@example.invalid")
        # The snapshots compare every file, .git included: git's own background gc or maintenance
        # writing a temporary pack file mid-test is not the rename's doing (seen at load 92).
        for repo in (self.checkout, self.origin):
            for key, value in (("gc.auto", "0"), ("maintenance.auto", "false")):
                _ = self.git("-C", str(repo), "config", key, value)
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.write(self.doc, self.production_doc())
        self.write(self.checkout / "README.md", "A production whose history says old.\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "merge(old-alpha): fixture history")
        _ = self.git("push", "-u", "origin", OLD)
        self.old_tip = self.git("rev-parse", "HEAD")
        self.write(self.notifier / "showrunner-old/conf",
                   "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", fixture_runtime_id(f"showrunner::{OLD}")])
                   + "\nPROMPT_FILE=unchanged-prompt\n")
        self.write(self.notifier / "showrunner-old/prompt", "Historical prompt mentions old.\n")

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, content)

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", *arguments], cwd=self.checkout, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def production_doc(self) -> str:
        return "\n".join([
            "# Production — old", "", "> **Status: PRODUCTION — running.**", "",
            "## Production Context", "", "- **Merge branch:** `old`",
            f"- **Showrunner checkout:** `{self.checkout}`", "- **Log:** `production.log`", "",
            "## Production rules", "", "History mentions old and stays unchanged.", "",
        ])

    def run_rename(self, action: str, destination: str = NEW, *,
                   source: str = OLD) -> subprocess.CompletedProcess[str]:
        if process_boundary_for_case(self):
            return subprocess.run([sys.executable, str(SCRIPT), action, source, destination],
                                  cwd=self.checkout, env=self.env, capture_output=True,
                                  text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        return self.run_command(action, source, destination)

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and path != self.root / "herdr.jsonl"}

    def run_command(self, *arguments: str, wrapper: str = "", **environment: str) -> subprocess.CompletedProcess[str]:
        return run_rename(list(arguments), {**self.env, **environment}, self.checkout, wrapper)
