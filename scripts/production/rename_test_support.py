"""Private Git fixtures and in-process production rename commands."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import ClassVar, Literal, cast, override
from unittest import mock
from collections.abc import Callable, Generator, Sequence, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.astra import design_roles
from scripts.direct import workers
from scripts.astra.design_role_test_support import designer_fixture
from scripts.production import staff, unit_config
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, unit_membership, StaffTestCase, isolated_source_readers
from scripts.lib.atomic_file import write_text_atomic
import fake_herdr
import herdr
from scripts.shutdown import pane_record, remote
from scripts.direct import progress_history
from scripts.message import sessions
from scripts.direct.recorder_state import STATE_FILENAME
import merge_checkpoint
from scripts.production.production_doc import Production, read_production
import rename
from scripts.production import showrunners
import stall_watch
import tidy_wrapped
import unit_move
import unit_names
import unit_lookup
from cli_fixture import invoke_cli
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SCRIPT = Path(__file__).with_name("rename.py")


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
        """Expose the private process and an empty rollout descriptor directory."""
        process = self.fixture.root / "folder-processes" / str(self.pid)
        (process / "fd").mkdir(parents=True, exist_ok=True)
        arguments = [self.kind] if self.kind == "claude" else [self.kind, "resume", self.fixture.owner.current_session]
        _ = (process / "cmdline").write_bytes("\0".join(arguments).encode() + b"\0" if self.live else b"")

    def panes(self) -> list[herdr.PaneIdentity]:
        return [
            {"pane_id": "w1:p1", "terminal_id": "kept-terminal", "workspace_id": "w1",
             "label": "kept-name", "session_id": self.fixture.owner.current_session if self.live else None},
            {"pane_id": "w1:p2", "terminal_id": "neighbour-terminal", "workspace_id": "w1",
             "label": "neighbour", "session_id": fixture_runtime_id("folder-move-neighbour")},
        ]

    def agents(self) -> list[herdr.AgentOccupant]:
        return ([{"pane_id": "w1:p1", "agent": self.kind, "agent_status": herdr.AgentStatus.RUNNING}]
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
        argv = [self.kind] if self.live else [sys.executable, "/fixture/accounts.py", "exec", self.config.name]
        foreground: list[dict[str, object]] = [{"pid": self.pid, "argv": argv, "cwd": str(self.cwd)}]
        return {"process_info": {"shell_pid": self.shell_pid, "cwd": str(self.cwd),
                                 "foreground_processes": foreground}}

    def run_in_pane(self, pane: str, command: list[str]) -> None:
        self.fixture.assertEqual(pane, "w1:p1")
        self.fixture.assertFalse(self.live, "a live session must not start again")
        self.fixture.assertEqual(self.agents(), [])
        self.cwd = Path(command[command.index("sh") + 4])
        self.fixture.assertTrue(self.cwd.is_dir())
        self.starts.append((pane, command.copy()))
        self.live = True
        self.publish_process_state()

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
        _ = self.enterContext(mock.patch("pane_record.RECORD_DIR", self.records))
        _ = self.enterContext(mock.patch.object(progress_history, "SESSION_ROOTS", (self.run_dir.parent,)))
        _ = self.enterContext(mock.patch("progress_history.SESSION_ROOTS", (self.run_dir.parent,)))
        _ = self.folder_git(self.checkout, "init", "-b", "kept-branch")
        _ = self.folder_git(self.checkout, "config", "user.name", "Folder Move Test")
        _ = self.folder_git(self.checkout, "config", "user.email", "move@example.invalid")
        write_text_atomic(self.checkout / "plan.md", "# Unit plan\n")
        _ = self.folder_git(self.checkout, "add", "plan.md")
        _ = self.folder_git(self.checkout, "commit", "-m", "Create the fixture")
        _ = self.folder_git(self.checkout, "worktree", "add", "-b", "unit-branch", str(self.old_folder))
        parent = fixture_member("showrunner", name="production")
        fixed = fixture_member("unit", name="kept-name", parent=parent.id,
                               worktree=staff.PresentWorktree("natedev", self.old_folder))
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
            os.environ, {"HERDR_PROC": str(self.root / "folder-processes")})))
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
            (herdr, "herdr_processes", backend.observed_processes),
            (herdr, "process_environment", backend.environment),
            (sessions, "session_liveness", session_liveness),
            (sessions, "process_liveness", process_liveness),
            (sessions, "process_start", mock.Mock(return_value="fixture-start")),
            (staff, "runtime_id", mock.Mock(return_value=self.owner.current_session)),
            (os, "kill", signal_process),
        ):
            _ = self.enterContext(mock.patch.object(target, attribute, replacement))
        return backend


OLD = "old"

NEW = "new"

UNITS = ("alpha-unit", "beta-unit")

# The staff fixture suppresses live discovery. These lifecycle fixtures provide
# their own fake host and session records, which the original readers must see.
FIXTURE_SOCKET_FOR = showrunners.socket_for
FIXTURE_MARKED_UNITS = unit_lookup.marked_units

FAIL_STATE_MOVE = '''import os, runpy, sys
from pathlib import Path

state = Path(os.environ["SHOWRUNNER_STATE_DIR"])
blocked = state / "review-paused" / "old.json"
def refusing_move(original):
    def move(source, destination, *args, **kwargs):
        if Path(source) == blocked:
            raise OSError("simulated step 4 state move failure")
        return original(source, destination, *args, **kwargs)
    return move
os.rename = refusing_move(os.rename)
os.replace = refusing_move(os.replace)
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''

CHECK_TIMER_HANDOFF = '''import os, runpy, subprocess, sys
from pathlib import Path

sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parents[2]))
from scripts.production import showrunners

notifier = Path(os.environ["NOTIFIER_STATE_DIR"])
old_timer = notifier / "showrunner-old"
new_timer = notifier / "showrunner-new"
seen = []
def readable_timer(instance):
    checked = showrunners.checked_doc(instance)
    assert isinstance(checked, showrunners.CheckedDoc), checked
    context = checked.path.read_text().split("## Production Context", 1)[1].split("\\n## ", 1)[0]
    assert "- **Formerly:** `old`" in context, context
    assert instance.name.removeprefix("showrunner-") in showrunners.timer_slugs()
original_run = subprocess.run
def observing_run(command, *args, **kwargs):
    moving_doc = len(command) > 3 and command[0] == "git" and command[3] == "mv"
    if moving_doc:
        readable_timer(old_timer)
    result = original_run(command, *args, **kwargs)
    if moving_doc:
        readable_timer(old_timer)
        seen.append("doc")
    return result
original_rename = os.rename
def observing_rename(source, destination, *args, **kwargs):
    moving_timer = Path(source) == old_timer
    if moving_timer:
        readable_timer(old_timer)
    result = original_rename(source, destination, *args, **kwargs)
    if moving_timer:
        readable_timer(new_timer)
        assert showrunners.canonical_slug("old") == "new"
        seen.append("timer")
    return result
subprocess.run = observing_run
os.rename = observing_rename
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exit:
    if exit.code == 0:
        assert seen == ["doc", "timer"], seen
    raise
'''

FAIL_PLAN_REWRITE = '''import os, runpy, sys
from pathlib import Path

original_replace = os.replace
def refusing_replace(source, destination, *args, **kwargs):
    if Path(destination).name == "alpha-unit.md":
        raise OSError("simulated unit header rewrite failure")
    return original_replace(source, destination, *args, **kwargs)
os.replace = refusing_replace
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''

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
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''

# load 136 on natedev, 2026-10-29

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

    with ExitStack() as scope:
        for module, fields in (
            (showrunners, {"CONFIG": Path(environment["SHOWRUNNERS_CONFIG"]),
                           "NOTIFIER_STATE_DIR": Path(environment["NOTIFIER_STATE_DIR"]),
                           "SHOWRUNNER_STATE_DIR": Path(environment["SHOWRUNNER_STATE_DIR"])}),
            (tidy_wrapped, {"STATE_DIR": Path(environment["SHOWRUNNER_STATE_DIR"]),
                            "NOTIFIER_STATE_DIR": Path(environment["NOTIFIER_STATE_DIR"])}),
            (stall_watch, {"STATE_DIR": Path(environment["STALL_WATCH_STATE_DIR"])}),
        ):
            for name, value in fields.items():
                _ = scope.enter_context(mock.patch.object(module, name, value))
        _ = scope.enter_context(mock.patch.object(subprocess, "run", side_effect=run))
        _ = scope.enter_context(mock.patch.object(unit_move, "time", SimpleNamespace(sleep=trust_readback)))
        # Failure injection scripts assign these names just as they do in a CLI
        # subprocess. Each call restores them before another test can observe it.
        for module, names in ((os, ("rename", "replace")),
                              (workers, ("end_unit",))):
            for name in names:
                _ = scope.enter_context(mock.patch.object(module, name, cast(object, getattr(module, name))))

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
    _ = case.enterContext(mock.patch("progress_history.SESSION_ROOTS", roots))
    for name, attribute, reader in (
        ("scripts.production.showrunners", "socket_for", FIXTURE_SOCKET_FOR),
        ("unit_lookup", "marked_units", FIXTURE_MARKED_UNITS),
        ("scripts.production.unit_lookup", "marked_units", FIXTURE_MARKED_UNITS),
    ):
        module = sys.modules.get(name)
        if module is not None:
            _ = case.enterContext(cast(AbstractContextManager[object], mock.patch.object(module, attribute, reader)))
    if members:
        state = Path(environment["SHOWRUNNER_STATE_DIR"])
        with mock.patch.object(tidy_wrapped, "STATE_DIR", state), unit_move.production_lock():
            pass
    productions: dict[str, Production] = {}
    for source in showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.RAISE):
        if source.timer.slug in members:
            production = read_production(source.timer.doc)
            productions[source.timer.slug] = production
            with merge_checkpoint.merge_lock(production.checkout, source.timer.slug):
                pass
    for slug in members.keys() - productions.keys():
        candidates = [production for path in sorted(case.root.rglob(f"{slug}-production.md"))
                      if (production := read_production(path)).doc.is_relative_to(production.checkout)]
        case.assertEqual(len(candidates), 1, f"{slug}: expected one production document in its checkout")
        production = candidates[0]
        productions[slug] = production
        with merge_checkpoint.merge_lock(production.checkout, slug):
            pass
    with staff.operation():
        for slug, units in members.items():
            production = productions[slug]
            row = fixture_member("showrunner", name=slug,
                                 doc=staff.LiveDoc(remote.local_machine(), production.doc))
            for unit in units:
                name = unit_names.name(unit)
                tree = locations[(slug, unit)]
                _ = fixture_member("unit", name=name, parent=row.id,
                                   worktree=staff.PresentWorktree(remote.local_machine(), tree))
                if not tree.is_dir():
                    continue
                plan = plans.get((slug, unit))
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
        self.stall: Path = Path()
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
        bind_staff_fixture(self, self.env, {OLD: UNITS},
                           locations={(OLD, unit): self.worktrees / unit for unit in UNITS},
                           plans={(OLD, unit): Path(f"docs/plans/{unit}.md") for unit in UNITS})
        production = next(row for row in staff.find(kind="showrunner") if row.name == OLD)
        _ = fixture_member("showrunner", name="child", parent=production.id)

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        self.home = self.root / "home"
        self.checkout = self.root / "checkout"
        self.origin = self.root / "origin.git"
        self.doc = self.checkout / "docs/plans/old-production.md"
        self.new_doc = self.doc.with_name("new-production.md")
        self.notifier = self.root / "state/notifier"
        self.state = self.root / "state/showrunner"
        self.stall = self.root / "state/stall-watch"
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
            "STALL_WATCH_STATE_DIR": str(self.stall),
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
        for path in (self.home, self.checkout, self.notifier, self.stall, self.worktrees):
            path.mkdir(parents=True)
        config = self.home / ".claude"
        config.mkdir()
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
        for unit in UNITS:
            header = f"> **Production: {OLD}**" if unit == "alpha-unit" else f"> **Production:** {OLD}"
            self.write(self.plan_path(unit),
                       f"# {unit}\n\n{header} — unit `{unit}`; "
                       + "production doc `docs/plans/old-production.md`\n\n"
                       + "Historical example: old stays in ordinary prose.\n")
        self.write(self.checkout / "README.md", "A production whose history says old.\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "merge(old-alpha): fixture history")
        _ = self.git("push", "-u", "origin", OLD)
        self.old_tip = self.git("rev-parse", "HEAD")
        for unit in UNITS:
            _ = self.git("worktree", "add", "-b", f"old-{unit}", str(self.worktrees / unit), OLD)
        self.write(self.notifier / "showrunner-old/conf",
                   "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", str(self.doc)])
                   + "\nPROMPT_FILE=unchanged-prompt\n")
        self.write(self.notifier / "showrunner-old/prompt", "Historical prompt mentions old.\n")
        for relative, content in self.saved_state().items():
            self.write(self.state / relative, content)
        self.write(self.state / "child/parent", "old\n")
        self.write(self.state / "other/parent", "oldish\n")
        for unit in UNITS:
            self.write(self.stall_path(OLD, unit), json.dumps({"unit": unit, "stretch": 17}) + "\n")
        self.write(self.stall_path("unrelated", "alpha-unit"), '{"stretch": 99}\n')

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, content)

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", *arguments], cwd=self.checkout, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def plan_path(self, unit: str) -> Path:
        return self.checkout / "docs/plans" / f"{unit}.md"

    def stall_path(self, slug: str, unit: str) -> Path:
        digest = hashlib.sha256(f"{slug}\0{unit}".encode()).hexdigest()
        return self.stall / f"{digest}.json"

    def production_doc(self) -> str:
        return "\n".join([
            "# Production — old", "", "> **Status: PRODUCTION — running.**", "",
            "## Production Context", "", "- **Merge branch:** `old`",
            f"- **Showrunner checkout:** `{self.checkout}`", "- **Log:** `production.log`", "",
            "## Production rules", "", "History mentions old and stays unchanged.", "",
        ])

    def saved_state(self) -> dict[str, str]:
        return {"footers-off/old": "", "review-paused/old.json": '{"reason": "old review"}\n',
                "outstanding/old.json": '{"question": "old question"}\n',
                "old/parent": "top\n", "old/log-backups/phase.log": "merge(old): retained log\n"}

    def run_rename(self, action: str, destination: str = NEW, *, fail_state: bool = False,
                   source: str = OLD) -> subprocess.CompletedProcess[str]:
        if self._testMethodName == "test_apply_moves_state_and_commits_documents_on_the_new_merge_branch":
            return subprocess.run([sys.executable, str(SCRIPT), action, source, destination],
                                  cwd=self.checkout, env=self.env, capture_output=True,
                                  text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        return self.run_command(action, source, destination, wrapper=FAIL_STATE_MOVE if fail_state else "")

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and path != self.root / "herdr.jsonl"}

    def unit_worktree(self, slug: str, unit: str) -> Path:
        return self.worktrees / f"repo.name_with_punctuation-{slug}-{unit}"

    def prepare_unit_moves(self) -> None:
        for unit in UNITS:
            old = self.worktrees / unit
            destination = self.unit_worktree(OLD, unit)
            _ = self.git("worktree", "move", str(old), str(destination))
            row = unit_membership(OLD, unit)
            _ = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), destination), expected=row.worktree)
            _ = self.git("push", "origin", f"old-{unit}")

    def run_command(self, *arguments: str, wrapper: str = "", **environment: str) -> subprocess.CompletedProcess[str]:
        return run_rename(list(arguments), {**self.env, **environment}, self.checkout, wrapper)


    def assert_unit_moved(self, unit: str) -> None:
        old, new = self.unit_worktree(OLD, unit), self.unit_worktree(NEW, unit)
        self.assertFalse(old.exists())
        self.assertTrue((new / ".git").is_file())
        self.assertEqual(self.git("-C", str(new), "branch", "--show-current"), f"new-{unit}")
        self.assertEqual(self.git("rev-parse", f"new-{unit}"), self.old_tip)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", f"refs/heads/old-{unit}"), "")
        self.assertIn(f"worktree {new}\n", self.git("worktree", "list", "--porcelain") + "\n")
        self.assertEqual(unit_membership(NEW, unit).worktree, staff.PresentWorktree("natedev", new))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def assert_applied(self) -> None:
        self.assertFalse(self.doc.exists())
        text = self.new_doc.read_text(encoding="utf-8")
        context = text.split("## Production Context", 1)[1].split("\n## ", 1)[0]
        self.assertRegex(context, r"(?m)^- \*\*Formerly:\*\*.*`old`")
        self.assertIn("- **Merge branch:** `new`", context)
        self.assertIn("History mentions old and stays unchanged.", text)
        for unit in UNITS:
            plan = self.plan_path(unit).read_text(encoding="utf-8")
            self.assertRegex(plan, r"(?m)^> \*\*Production:(?: new\*\*|\*\* new) — unit")
            self.assertIn("production doc `docs/plans/new-production.md`", plan)
            self.assertIn("Historical example: old stays in ordinary prose.", plan)
            self.assertEqual(self.git("rev-parse", f"old-{unit}"), self.old_tip)
            self.assertTrue((self.worktrees / unit / ".git").exists())
            self.assertFalse(self.stall_path(OLD, unit).exists())
            stretch = cast(object, json.loads(self.stall_path(NEW, unit).read_text()))
            self.assertEqual(stretch, {"unit": unit, "stretch": 17})
        self.assertFalse((self.notifier / "showrunner-old").exists())
        conf = (self.notifier / "showrunner-new/conf").read_text(encoding="utf-8")
        self.assertIn(str(self.new_doc), conf)
        self.assertNotIn(str(self.doc), conf)
        self.assertIn("TARGET=session:test-showrunner\n", conf)
        self.assertEqual((self.notifier / "showrunner-new/prompt").read_text(),
                         "Historical prompt mentions old.\n")
        for relative, content in self.saved_state().items():
            old_path = self.state / relative
            new_path = self.state / relative.replace("old", "new", 1)
            self.assertFalse(old_path.exists(), str(old_path))
            self.assertEqual(new_path.read_text(), content)
        self.assertEqual((self.state / "child/parent").read_text(), "new\n")
        self.assertEqual((self.state / "other/parent").read_text(), "oldish\n")
        self.assertEqual(self.stall_path("unrelated", "alpha-unit").read_text(), '{"stretch": 99}\n')
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", f"refs/heads/{OLD}"), "")
        self.assertEqual(self.git("rev-parse", "new^"), self.old_tip)
        self.assertEqual(self.git("log", "-1", "--format=%s", NEW), "chore(new): rename production from old")
        committed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", NEW).splitlines()
        self.assertEqual(set(committed), {"docs/plans/old-production.md", "docs/plans/new-production.md",
                                         *(f"docs/plans/{unit}.md" for unit in UNITS)})
        new_tip = self.git("rev-parse", NEW)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/new"), f"{new_tip}\trefs/heads/new")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/old"), f"{self.old_tip}\trefs/heads/old")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def held_design(self, repository_id: str, checkout: Path, holder: design_roles.Holder) -> design_roles.HeldDesign:
        """A design `holder` holds, with an open designer whose roster entry runs in `checkout`."""
        with mock.patch.dict(os.environ, self.env):
            design_roles.register_repository(repository_id, {"fixture-host": str(checkout)}, inventory_complete=True)
            placement = design_roles.authority_request("reserve", {"repository_id": repository_id, "document": "design.md",
                                                            "host": "fixture-host", "request_id": repository_id})
            row = designer_fixture(holder, f"old-{repository_id}-astra-designer", self.root / "codex-home")
            self.env["CODEX_HOME"] = str(self.root / "codex-home")
            design = design_roles.HeldDesign(
                design_roles.DesignPlacement(str(placement["design_id"]), "fixture-host", 1), repository_id, str(checkout),
                str(checkout / "design.md"), "design", "2026-10-22T12:00:00+00:00", holder, design_roles.DesignUnbound(),
                (design_roles.RoleInstance("designer", f"old-{repository_id}-astra-designer", design_roles.RoleOpen(row.id)),
                 design_roles.RoleInstance("reviewer", f"old-{repository_id}-astra-reviewer", design_roles.RoleNotOpened())))
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            self.write(design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json", json.dumps(
                {design.roles[0].name: {"staff_id": row.id, "status": "done"}}))
        return design

    def read_held(self, design: design_roles.HeldDesign) -> tuple[design_roles.HeldDesign, str]:
        """Read the current design and its repository."""
        with mock.patch.dict(os.environ, self.env):
            current = design_roles.read_design(design.placement.design_id)
        return current, current.repository
