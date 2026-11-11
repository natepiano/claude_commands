"""The shared owner/Mac staff fixture, source builders and real fake-SSH transport."""
from __future__ import annotations

import fcntl
import io
import json
import os
import pwd
import runpy
import shlex
import shutil
import signal
import socket
import subprocess
from subprocess import Popen as start_staff_owner
from subprocess import run as run_fixture_git
import sys
import tempfile
import time
import traceback
import unittest
from collections.abc import Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager, redirect_stderr, redirect_stdout
from io import IOBase
from datetime import UTC, datetime
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Literal, TextIO, TypedDict, cast, override
from unittest import mock
from uuid import NAMESPACE_URL, uuid5

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib import process_group
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import staff
from scripts.herdr import pane_launch
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.agents import pane_record, remote

_LOGIN_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
_FIXTURE_ACCOUNTS = tempfile.TemporaryDirectory(prefix="staff-test-accounts-")


@contextmanager
def foreign_hostname() -> Generator[None]:
    """Use one foreign hostname in the test and its Python children."""
    with tempfile.TemporaryDirectory() as directory:
        startup = Path(directory) / "sitecustomize.py"
        _ = startup.write_text('import os, socket\nsocket.gethostname = lambda: os.environ.get("GATE_TEST_HOST", "gate-fixture")\n')
        pythonpath = os.pathsep.join((directory, os.environ.get("PYTHONPATH", "")))
        from scripts.production import production_lifecycle, update_registration
        from scripts.agents import shutdown

        with (mock.patch.object(shutdown, "local_machine", side_effect=lambda: remote.local_machine()),
              mock.patch.object(production_lifecycle, "local_machine", side_effect=lambda: remote.local_machine()),
              mock.patch.object(update_registration, "local_machine", side_effect=lambda: remote.local_machine()),
              mock.patch.object(socket, "gethostname", return_value="gate-fixture"),
              mock.patch.dict(os.environ, {"PYTHONPATH": pythonpath})):
            yield


def fixture_account_home(home: Path) -> Path:
    """Keep fixture sessions outside the login user's real account folders."""
    for candidate, login in ((Path(os.path.abspath(home)), _LOGIN_HOME), (home.resolve(), _LOGIN_HOME.resolve())):
        if candidate.parent == login and candidate.name.startswith((".claude", ".codex")):
            return Path(_FIXTURE_ACCOUNTS.name) / candidate.name
    return home


def _redirect_fixture_accounts() -> None:
    for variable, folder in (("CLAUDE_CONFIG_DIR", ".claude"), ("CODEX_HOME", ".codex")):
        home = Path(os.environ.get(variable) or Path.home() / folder)
        private_home = fixture_account_home(home)
        if private_home != home:
            os.environ[variable] = str(private_home)


_redirect_fixture_accounts()

FIXTURE_GIT: str = shutil.which("git") or "git"
CHECKOUT = Path(__file__).resolve().parents[2]
STAFF_SCRIPT = Path(__file__).with_name("staff.py")
SOURCES_SCRIPT = Path(__file__).with_name("staff_sources.py")
FixtureMachine = Literal["natedev", "mac"]
UNFILLED_WORKTREE = staff.WorktreeUnfilled()
UNFILLED_DOC = staff.DocUnfilled()
CLAUDE_BACKEND = staff.ClaudeBackend()


@contextmanager
def production_staff_record() -> Generator[None, None, None]:
    """Mark each fixture snapshot as production within one alert test."""
    read_snapshot = staff.snapshot

    def snapshot() -> staff.StaffSnapshot:
        return replace(read_snapshot(), production=True)

    with mock.patch.object(staff, "snapshot", side_effect=snapshot):
        yield


def fixture_runtime_id(label: str) -> str:
    """A fake backend returns one repeatable native id for this fixture label."""
    return str(uuid5(NAMESPACE_URL, "staff-test-backend:" + label))


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _fixture_session_path(identity: str, backend: staff.ClaudeBackend | staff.CodexBackend) -> Path:
    home = fixture_account_home(backend.codex_home if isinstance(backend, staff.CodexBackend) else _claude_home())
    return home / "sessions" / f"{identity}.jsonl" if isinstance(backend, staff.CodexBackend) else home / "projects/fixture" / f"{identity}.jsonl"


def _prepare_fixture_session(identity: str, backend: staff.ClaudeBackend | staff.CodexBackend) -> Path:
    if isinstance(backend, staff.ClaudeBackend):
        home = _claude_home()
        private_home = fixture_account_home(home)
        if private_home != home:
            os.environ["CLAUDE_CONFIG_DIR"] = str(private_home)
    path = _fixture_session_path(identity, backend)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def fixture_session(identity: str, backend: staff.ClaudeBackend | staff.CodexBackend) -> None:
    """Create the private session file that supplies the worker family."""
    path = _prepare_fixture_session(identity, backend)
    path.touch(exist_ok=True)
    if isinstance(backend, staff.CodexBackend):
        home = fixture_account_home(backend.codex_home)
        os.environ["CODEX_HOME"] = str(home)
        row = staff.initial_snapshot().by_current_session(identity)
        if row is None:
            entry = pane_launch.launch("fixture-" + identity, identity, str(home),
                                       {"CODEX_HOME": str(home)}, ["codex"], kind="codex")
            pane_launch.write(pane_launch.record_directory() / ("fixture-" + identity + ".json"), entry)
        elif row.home != staff.SessionHome(home):
            _ = staff.set_home(row.id, staff.SessionHome(home))


def transcript_fixture(row: staff.StaffRow, backend: staff.ClaudeBackend | staff.CodexBackend, *,
                       turns: int = 1, malformed: bool = False) -> tuple[Path, tuple[list[str], list[str], list[str]]]:
    """Write source evidence with private tool data between assignments and final replies."""
    path = _prepare_fixture_session(row.current_session, backend)
    family = backend.family
    assignments = [f"Assignment {index}: preserve this complete requirement. " + "detail " * 90
                   for index in range(turns)]
    replies = [f"Final {index}: completed the required change. " + "proof " * 90
               for index in range(turns)]
    files = [f"src/changed-{index}.py" for index in range(turns)]
    records: list[object] = []
    moved: list[str] = []
    for index, (assignment, reply, file) in enumerate(zip(assignments, replies, files, strict=True)):
        if family == "claude":
            records.extend([
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": assignment}]}},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "write", "name": "Edit",
                     "input": {"file_path": file, "content": "PRIVATE_TOOL_REQUEST"}}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "write", "content": "PRIVATE_TOOL_OUTPUT"}]}},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": reply}], "stop_reason": "end_turn"}},
            ])
        else:
            moved.append(file + ".moved")
            patch = f"*** Begin Patch\n*** Update File: {file}\n*** Move to: {moved[-1]}\n+PRIVATE_TOOL_REQUEST\n*** End Patch"
            tool, arguments = ("apply_patch", patch) if index % 2 == 0 else (
                "exec", f"const example = 'tools.apply_patch(\"PRIVATE_TOOL_REQUEST\")'; await tools.apply_patch({json.dumps(patch)});")
            records.extend([
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": assignment}]}},
                {"type": "response_item", "payload": {"type": "custom_tool_call", "name": tool, "input": arguments}},
                {"type": "response_item", "payload": {"type": "custom_tool_call_output",
                    "output": "PRIVATE_TOOL_OUTPUT"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": reply}},
            ])
    lines = [json.dumps(record) for record in records]
    if malformed:
        lines.insert(1, '{"broken":')
        lines.insert(3, json.dumps({"type": "assistant", "message": None}))
        lines.insert(5, json.dumps(["unexpected record"]))
        lines.append('{"torn":')
    _ = path.write_text("\n".join(lines) + "\n")
    return path, (assignments, replies, files + moved)


def stopped_worker_module(directory: Path) -> Path:
    """Install a child-process lifecycle stub that records the source row."""
    directory.mkdir()
    _ = (directory / "sitecustomize.py").write_text('''import json, os
from pathlib import Path
from scripts.production import staff
from scripts.agents import stop_resume
def stop(reference, kind=""):
    row = staff.by_id(staff.StaffId(reference))
    if os.environ.get("FIXTURE_STOPPED"):
        Path(os.environ["FIXTURE_STOPPED"]).write_text(json.dumps(row.to_json()))
    return stop_resume.Stopped(row.current_session, 987654)
stop_resume.stop = stop
''')
    return directory


def switch_backend_stub() -> str:
    """Create a native destination and check the staff row at dispatch."""
    return r'''import os, sys
from pathlib import Path
from unittest.mock import patch
from scripts.production import staff, worker_admission as admission
from scripts.production.staff_test_support import fixture_runtime_id, fixture_session
from scripts.agents import stop_resume
args = sys.argv[1:]
run = Path(args[args.index("--session-dir") + 1])
allowed = admission.admit(args[args.index("--name") + 1], run)
row = allowed.slot.row
def start(handoff):
    identity = fixture_runtime_id("destination:" + row.id + "codex")
    fixture_session(identity, staff.CodexBackend(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))))
    return identity
def dispatch(published):
    assert staff.by_id(row.id) == published.row
    return admission.SwitchState.ASSIGNMENT_ACCEPTED
with patch.object(stop_resume, "stop", return_value=stop_resume.Stopped(row.current_session, 987654)):
    admission.switch_worker(allowed, "codex", start=start, dispatch=dispatch, cleanup=lambda identity: admission.NoDestinationToStop())
'''


def fixture_member(kind: staff.Kind, *, name: str,
                   parent: staff.StaffId | staff.ReportsTo | staff.StaffRoot | None = None,
                   worktree: staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled = UNFILLED_WORKTREE,
                   doc: staff.LiveDoc | staff.WrappedDoc | staff.DocUnfilled = UNFILLED_DOC,
                   runtime_id: str = "",
                   backend: staff.ClaudeBackend | staff.CodexBackend = CLAUDE_BACKEND) -> staff.StaffRow:
    """Publish the identity returned by the private fixture backend, never live evidence."""
    relationship = (parent if isinstance(parent, (staff.ReportsTo, staff.StaffRoot)) else
                    staff.ReportsTo(parent) if parent is not None else staff.StaffRoot())
    parent_id = relationship.id if isinstance(relationship, staff.ReportsTo) else None
    existing = [row for row in staff.initial_snapshot().find(kind=kind, parent=parent_id)
                if row.name == name and row.parent == relationship]
    identity = runtime_id or (existing[0].current_session if len(existing) == 1 else
                              fixture_runtime_id(f"{kind}:{parent_id or ''}:{name}"))
    if kind == "worker":
        if parent_id is None:
            raise staff.StaffRefused("a fixture worker requires a parent")
        row = staff.ensure_worker(staff.native_id(identity), name, parent_id)
        fixture_session(identity, backend)
        return staff.by_id(row.id) or row
    return staff.ensure(kind, name=name, parent=relationship,
                        worktree=worktree, doc=doc, runtime_id=identity)


def fixture_worker(name: str, parent: staff.StaffId, *, runtime_id: str = "",
                   backend: staff.ClaudeBackend | staff.CodexBackend = CLAUDE_BACKEND) -> staff.StaffRow:
    """Publish the fake worker backend's native identity through the real writer."""
    return fixture_member("worker", name=name, parent=parent, runtime_id=runtime_id, backend=backend)


def fixture_roster(entries: dict[str, object], parent: staff.StaffId,
                   codex_home: Path) -> dict[str, object]:
    """Publish private workers before a reader receives their roster references."""
    result: dict[str, object] = {}
    for name, value in entries.items():
        if not isinstance(value, dict):
            result[name] = value
            continue
        entry = dict(cast(dict[str, object], value))
        identity = entry.get("thread_id")
        if not isinstance(identity, str) or not identity:
            result[name] = entry
            continue
        try:
            native = staff.native_id(identity)
        except staff.StaffRefused:
            result[name] = entry
            continue
        fixture_session(native, staff.CodexBackend(codex_home))
        record = staff.initial_snapshot()
        row = record.by_current_session(native)
        if row is None:
            former = next((item for item in record.find(kind="worker", parent=parent)
                           if item.name == name), None)
            row = (staff.update_current_session(former.current_session, native)
                   if former is not None else
                   fixture_worker(name, parent, runtime_id=native,
                                  backend=staff.CodexBackend(codex_home)))
        entry["staff_id"] = row.id
        result[name] = entry
    from scripts.production.session_staff import stored_worker_records
    return stored_worker_records(result)


def fixture_worker_records(entries: dict[str, object], membership: staff.StaffSnapshot) -> dict[str, object]:
    """Read runtime identities for assertions from the current staff rows."""
    from scripts.production import session_staff

    result: dict[str, object] = {}
    for name, raw in entries.items():
        if not isinstance(raw, dict):
            result[name] = raw
            continue
        entry = dict(cast(dict[str, object], raw))
        member = session_staff.worker_row(entry, membership)
        if member is not None:
            entry.update(thread_id=member.current_session, session_id=member.current_session,
                         codex_home=session_staff.worker_home(entry, membership))
        result[name] = entry
    return result


def record_fixture_claude_start(arguments: list[str], *, remote_control: bool = True, messaging_socket: str = "") -> None:
    """The private backend records its own native identity before opening work."""
    from scripts.message import sessions

    identity = (arguments[arguments.index("--resume") + 1] if "--resume" in arguments
                else os.environ["FAKE_CLAUDE_SESSION_ID"])
    fixture_session(identity, CLAUDE_BACKEND)
    pid = int(os.environ["FAKE_CLAUDE_PID"])
    path = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{pid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({
        "pid": pid, "procStart": sessions.process_start(pid), "sessionId": identity,
        "name": "fixture Claude", "updatedAt": 1, "formerNames": [], "tmux": "",
        "entrypoint": "cli", "messagingSocketPath": messaging_socket,
        "bridgeSessionId": "session_bridge" if remote_control else "",
    }) + "\n")


class ClaudeBackendFixture:
    """Observe launcher inputs and supply a native id before its opening callback."""

    def __init__(self, identity: str, configuration: Path) -> None:
        self.identity: str = identity
        self.configuration: Path = configuration
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.before_opening: staff.StaffSnapshot = staff.StaffSnapshot(())

    def launch(self, name: str, space: str, title: str, cwd: str, environment: dict[str, str],
               kind: str, agent_arguments: list[str], opening: object, timeout_ms: int, *,
               agent: str) -> object:
        from scripts.herdr import herdr

        del name, space, title, cwd, kind, timeout_ms, agent
        if not isinstance(opening, herdr.OpeningPrompt):
            raise AssertionError("new Claude admission needs an opening prompt")
        self.arguments = list(agent_arguments)
        self.environment = dict(environment)
        fixture_session(self.identity, CLAUDE_BACKEND)
        _ = opening.before_prompt(herdr.SessionTranscript(self.configuration, self.identity))
        self.before_opening = staff.snapshot()
        return herdr.AgentLaunched("w1:p1", herdr.PromptSubmitted(datetime.fromtimestamp(0, UTC)))


@contextmanager
def isolated_staff_file(path: Path, environment: dict[str, str]) -> Generator[None]:
    """Use private staff and registry paths in this process and its fixture children."""
    environment["STAFF_FILE"] = str(path)
    _ = environment.setdefault("CLAUDE_CONFIG_DIR", str(path.parent / "claude"))
    _ = environment.setdefault("NOTIFIER_STATE_DIR", str(path.parent / "notifier"))
    for key, folder in (
        ("SHOWRUNNER_STATE_DIR", "showrunners"),
        ("NOTIFIER_SESSIONS_DIR", "sessions"),
        ("HERDR_PANE_RECORD_DIR", "panes"),
        ("XDG_CONFIG_HOME", "config"),
    ):
        _ = environment.setdefault(key, str(path.parent / folder))
    environment["STAFF_CONFIG"] = os.environ.get("STAFF_CONFIG", str(CHECKOUT / "config/staff.conf"))
    with ExitStack() as contexts:
        _ = contexts.enter_context(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "STAFF_FILE": str(path),
            "CLAUDE_CONFIG_DIR": environment["CLAUDE_CONFIG_DIR"],
            "NOTIFIER_STATE_DIR": environment["NOTIFIER_STATE_DIR"],
            "SHOWRUNNER_STATE_DIR": environment["SHOWRUNNER_STATE_DIR"],
            "NOTIFIER_SESSIONS_DIR": environment["NOTIFIER_SESSIONS_DIR"],
            "HERDR_PANE_RECORD_DIR": environment["HERDR_PANE_RECORD_DIR"],
            "XDG_CONFIG_HOME": environment["XDG_CONFIG_HOME"],
            "CODEX_HOME": environment.get("CODEX_HOME", os.environ.get("CODEX_HOME", "")),
            "STAFF_CONFIG": os.environ.get("STAFF_CONFIG", str(CHECKOUT / "config/staff.conf")),
        })))
        for name in ("showrunners", "scripts.production.showrunners"):
            module = sys.modules.get(name)
            if module is not None:
                for key in ("NOTIFIER_STATE_DIR", "SHOWRUNNER_STATE_DIR"):
                    _ = contexts.enter_context(mock.patch.object(module, key, Path(environment[key])))
        for name in ("pane_record", "scripts.agents.pane_record"):
            module = sys.modules.get(name)
            if module is not None:
                _ = contexts.enter_context(mock.patch.object(
                    module, "RECORD_DIR", Path(environment["HERDR_PANE_RECORD_DIR"]),
                ))
        yield


def showrunner_relationship(name: str, parent: str = "") -> staff.StaffRow:
    """Find fixture identities by their short names, then set the desired edge."""
    with staff.operation():
        rows = {row.name: row for row in staff.initial_snapshot().find(kind="showrunner")}

        def intended_row(short_name: str) -> staff.StaffRow:
            if short_name not in rows:
                rows[short_name] = fixture_member("showrunner", name=short_name)
            return rows[short_name]

        child = intended_row(name)
        destination = staff.ReportsTo(intended_row(parent).id) if parent else staff.StaffRoot()
        return staff.move(child.id, destination) if child.parent != destination else child


def recorded_showrunner(name: str, doc: Path, parent: str = "") -> staff.StaffRow:
    """Record the document that identifies one private production owner."""
    rows = [row for row in staff.initial_snapshot().find(kind="showrunner") if row.name == name]
    row = rows[0] if rows and not parent else showrunner_relationship(name, parent)
    location = staff.LiveDoc(remote.local_machine(), doc)
    return row if row.doc == location else staff.set_doc(row.id, location)


def fixture_unit_location(owner: staff.StaffRow, unit: str, location: Path) -> staff.StaffRow:
    """Record a canonical unit and its explicit local location without changing files."""
    from scripts.production import unit_names

    row = fixture_member("unit", name=unit_names.name(unit), parent=owner.id)
    return staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), location), expected=row.worktree)


def recorded_unit(showrunner: str, unit: str, worktree: Path, doc: Path, *,
                  plan: str = "standby") -> staff.StaffRow:
    """Record private unit locations and configuration through the shared fixture writers."""
    _ = recorded_showrunner(showrunner, doc)
    row = unit_membership(showrunner, unit)
    row = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), worktree), expected=row.worktree)
    worktree.mkdir(parents=True, exist_ok=True)
    if not (worktree / ".git").exists():
        _ = run_fixture_git([FIXTURE_GIT, "init", "-b", "fixture", str(worktree)],
                            check=True, capture_output=True)
    head = run_fixture_git([FIXTURE_GIT, "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
                           check=False, capture_output=True)
    if head.returncode:
        _ = run_fixture_git([FIXTURE_GIT, "-C", str(worktree), "-c", "user.name=Fixture",
                             "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture"],
                            check=True, capture_output=True)
    write_text_atomic(worktree / ".unit.toml", f"plan = {json.dumps(plan)}\nresponsibilities = []\n")
    return row


def showrunner_launch(row: staff.StaffRow, session_id: str, environment: dict[str, str]) -> None:
    """Pin timer identities when several fixture timers share one production doc."""
    launch = pane_record.launch(f"fixture:{session_id}", session_id, str(CHECKOUT), {}, ["claude", "-n", row.name])
    with mock.patch.object(pane_record, "RECORD_DIR", Path(environment["HERDR_PANE_RECORD_DIR"])):
        pane_record.write("agents", launch)


def unit_membership(showrunner: str, unit: str) -> staff.StaffRow:
    """Add an intended fixture member using its displayed short name."""
    from scripts.production import unit_names

    parents = [row for row in staff.initial_snapshot().find(kind="showrunner") if row.name == showrunner]
    parent = parents[0] if parents else fixture_member("showrunner", name=showrunner)
    return fixture_member("unit", name=unit_names.name(unit), parent=parent.id)


def production_membership(showrunner: str, *units: str) -> None:
    """Seed the current members of a fixture production, including an empty one."""
    _ = showrunner_relationship(showrunner)
    for unit in units:
        _ = unit_membership(showrunner, unit)


def source_environment(source: Path) -> dict[str, str]:
    return {key: str(source / folder) for key, folder in (
        ("NOTIFIER_STATE_DIR", "notifier"), ("SHOWRUNNER_STATE_DIR", "showrunners"),
        ("HERDR_PANE_RECORD_DIR", "panes"), ("NOTIFIER_SESSIONS_DIR", "sessions"),
        ("XDG_CONFIG_HOME", "config"),
        ("UNIT_WORKERS_STATE", "unit-workers"), ("ROSTER_RUN_ROOT", "runs"),
        ("ASTRA_ROLES_STATE", "astra-roles"), ("ASTRA_REVIEW_DIR", "astra-runs"),
        ("DESIGN_ROLES_STATE", "design-roles"), ("DESIGN_OWNERSHIP_STATE", "design-ownership"),
    )}


OWNER_CLI = """import os, runpy, sys
from pathlib import Path
with open(Path(os.environ['STAFF_TEST_CHECKOUT']) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
from scripts.production.staff_test_support import STAFF_SCRIPT, isolated_source_readers, observe_operation_lock
observe_operation_lock()
# Import platform-dependent Python support before selecting fake Mac transport.
from scripts.production import staff_sources
from scripts.agents import remote
from unittest import mock
sys.platform = os.environ.get('STAFF_TEST_PLATFORM', 'linux')
script = sys.argv.pop(1)
sys.argv[0] = script
# Retire/check verbs discover processes too, even without --live.
with isolated_source_readers(), mock.patch.object(
    remote, 'local_machine', return_value='mac' if sys.platform == 'darwin' else 'natedev',
):
    if script == str(STAFF_SCRIPT):
        from scripts.production import staff
        raise SystemExit(staff.main())
    runpy.run_path(script, run_name='__main__')
"""


def observe_operation_lock() -> None:
    """Let a lock test know a child has reached the real operation flock call."""
    notice = os.environ.get("STAFF_TEST_LOCK_NOTICE", "")
    if not notice:
        return
    watched = Path(os.environ["STAFF_TEST_LOCK_TARGET"])
    original = fcntl.flock
    reported = False

    def report_then_lock(lock_file: int | IOBase, operation: int) -> None:
        nonlocal reported
        if not reported and operation & fcntl.LOCK_EX and watched.exists():
            descriptor = lock_file if isinstance(lock_file, int) else lock_file.fileno()
            opened, target = os.fstat(descriptor), watched.stat()
            if (opened.st_dev, opened.st_ino) == (target.st_dev, target.st_ino):
                with socket.socket(socket.AF_UNIX) as signal:
                    signal.connect(notice)
                    signal.sendall(b"operation")
                reported = True
        original(lock_file, operation)

    _ = mock.patch("fcntl.flock", side_effect=report_then_lock).start()


@contextmanager
def isolated_source_readers() -> Generator[None]:
    """Redirect import-bound source paths and prevent live session discovery."""
    from scripts.lib import unit_runs
    from scripts.message import process_table

    with ExitStack() as contexts:
        _ = contexts.enter_context(mock.patch.object(unit_runs, "ASTRA_RUN_ROOT", Path(os.environ["ASTRA_REVIEW_DIR"])))
        # Process-evidence cases install their own private snapshot inside this boundary.
        _ = contexts.enter_context(mock.patch.object(process_table, "snapshot", return_value={}))
        for module_name, attributes in (
            ("showrunners", {
                "NOTIFIER_STATE_DIR": Path(os.environ["NOTIFIER_STATE_DIR"]),
                "SHOWRUNNER_STATE_DIR": Path(os.environ["SHOWRUNNER_STATE_DIR"]),
            }),
            ("pane_record", {"RECORD_DIR": Path(os.environ["HERDR_PANE_RECORD_DIR"])}),
        ):
            qualified = "scripts.production.showrunners" if module_name == "showrunners" else "scripts.agents.pane_record"
            for name in (module_name, qualified):
                module = sys.modules.get(name)
                if module is not None:
                    for attribute, value in attributes.items():
                        _ = contexts.enter_context(mock.patch.object(module, attribute, value))
                    if module_name == "showrunners":
                        _ = contexts.enter_context(mock.patch.object(module, "socket_for", return_value=None))
        for name in ("unit_lookup", "scripts.production.unit_lookup"):
            module = sys.modules.get(name)
            if module is not None:
                _ = contexts.enter_context(mock.patch.object(module, "labeled_sessions", return_value={}))
        yield


# Execute the real CLI against a different temp file: this is the owner, not a
# canned response, so remote reads, writes, and error mapping must all agree.
FAKE_SSH = """import json, os, runpy, shlex, sys
from pathlib import Path
with open(Path(os.environ['STAFF_TEST_CHECKOUT']) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
from scripts.lib import process_group
from scripts.lib.atomic_file import write_text_atomic
arguments = sys.argv[1:]
log = Path(os.environ['STAFF_TEST_SSH_LOG'])
previous = log.read_text() if log.exists() else ''
write_text_atomic(log, previous + json.dumps(arguments) + '\\n')
if os.environ.get('STAFF_TEST_SSH_FAIL'):
    reason = os.environ.get('STAFF_TEST_SSH_REASON', 'ssh: connection refused')
    if reason:
        print(reason, file=sys.stderr)
    raise SystemExit(int(os.environ.get('STAFF_TEST_SSH_EXIT', '255')))
if (len(arguments) < 6 or arguments[:3] != ['-o', 'BatchMode=yes', '-o']
        or not arguments[3].startswith('ConnectTimeout=') or arguments[4] != 'natedev'
        or int(arguments[3].split('=', 1)[1]) <= 0):
    raise SystemExit('unexpected SSH target or options')
command = arguments[5:]
if len(command) == 1:
    command = shlex.split(command[0])
if command[:2] != ['~/.claude/scripts/lib/py', '~/.claude/scripts/production/staff.py']:
    raise SystemExit('unexpected remote Python or staff script')
if '--json' not in command[2:]:
    raise SystemExit('remote staff call requires --json')
if os.environ.get('STAFF_TEST_OWNER_PORT') and not os.environ.get('STAFF_TEST_LOCK_NOTICE'):
    import socket
    request = dict(arguments=[os.environ['STAFF_TEST_SCRIPT'], *command[2:]], environment=dict(os.environ),
                   directory=os.getcwd(), stdin=sys.stdin.read())
    with socket.create_connection(('127.0.0.1', int(os.environ['STAFF_TEST_OWNER_PORT']))) as connection:
        with connection.makefile('rwb') as channel:
            channel.write(json.dumps(request).encode() + b'\\n')
            channel.flush()
            result = json.loads(channel.readline())
    sys.stdout.write(result['stdout'])
    sys.stderr.write(result['stderr'])
    raise SystemExit(result['status'])
from scripts.production.staff_test_support import source_environment
os.environ['STAFF_FILE'] = os.environ['STAFF_TEST_OWNER_FILE']
source = Path(os.environ['STAFF_TEST_OWNER_SOURCE'])
os.environ.update(source_environment(source))
sys.platform = 'linux'
sys.argv = [os.environ['STAFF_TEST_SCRIPT'], *command[2:]]
from scripts.production.staff_test_support import isolated_source_readers, observe_operation_lock
from scripts.production import staff_sources
observe_operation_lock()
with isolated_source_readers():
    runpy.run_path(sys.argv[0], run_name='__main__')
"""


@dataclass(frozen=True)
class RowProductionFixture:
    """The row locations differ from the timer name."""

    runner: staff.StaffRow
    unit: staff.StaffRow
    worker: staff.StaffRow
    doc: Path
    worktree: Path
    name: str


class StaffOwnerRequest(TypedDict):
    arguments: list[str]
    environment: dict[str, str]
    directory: str
    stdin: str


def run_staff_owner_request(connection: socket.socket, request: StaffOwnerRequest) -> None:
    """Run one CLI request in its own child of the warm owner."""
    environment = request["environment"]
    environment["STAFF_FILE"] = environment["STAFF_TEST_OWNER_FILE"]
    environment.update(source_environment(Path(environment["STAFF_TEST_OWNER_SOURCE"])))
    os.environ.clear()
    os.environ.update(environment)
    tempfile.tempdir = None
    os.chdir(request["directory"])
    sys.argv = request["arguments"]
    sys.stdin = io.StringIO(request["stdin"])
    sys.platform = "linux"
    output, errors = io.StringIO(), io.StringIO()
    status = 0
    with redirect_stdout(output), redirect_stderr(errors), isolated_source_readers():
        observe_operation_lock()
        try:
            _ = runpy.run_path(sys.argv[0], run_name="__main__")
        except SystemExit as result:
            status = result.code if isinstance(result.code, int) else 0 if result.code is None else 1
            if isinstance(result.code, str):
                print(result.code, file=sys.stderr)
        except BaseException:
            traceback.print_exc()
            status = 1
    with connection.makefile("wb") as channel:
        _ = channel.write(json.dumps(dict(status=status, stdout=output.getvalue(), stderr=errors.getvalue())).encode() + b"\n")
        channel.flush()


STAFF_OWNER_SERVER = r'''
import json
import os
import socket
import sys
from pathlib import Path

with open(Path(os.environ['STAFF_TEST_CHECKOUT']) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
children = []
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    print(listener.getsockname()[1], flush=True)
    while True:
        with listener.accept()[0] as connection:
            with connection.makefile('rb') as channel:
                request = json.loads(channel.readline())
            if request is None:
                break
            from scripts.production.staff_test_support import run_staff_owner_request
            from scripts.production import staff_sources
            pid = os.fork()
            if pid == 0:
                listener.close()
                try:
                    run_staff_owner_request(connection, request)
                finally:
                    connection.close()
                    os._exit(0)
            children.append(pid)
for pid in children:
    os.waitpid(pid, 0)
'''


class SharedStaffOwner:
    """Own one lazy import cache and all request children for a test."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.lifetime: ExitStack = ExitStack()

    @cached_property
    def port(self) -> str:
        script = self.root / "fake-staff-owner.py"
        write_text_atomic(script, STAFF_OWNER_SERVER)
        errors = self.lifetime.enter_context((self.root / "fake-staff-owner.log").open("w"))
        process = self.lifetime.enter_context(start_staff_owner(
            [sys.executable, str(script)], cwd=self.root,
            env={**os.environ, "TMPDIR": str(self.root)},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors, text=True,
            start_new_session=True,
        ))
        port = cast(TextIO, process.stdout).readline().strip()

        def stop() -> None:
            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=TEST_HANG_GUARD_SECS) as connection:
                    connection.sendall(b"null\n")
                _ = process.wait(timeout=max(0, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process_group.send_process_group(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _ = process.wait()

        _ = self.lifetime.callback(stop)
        return port

    def close(self) -> None:
        self.lifetime.close()


class StaffTestCase(unittest.TestCase):
    root: Path = Path()
    staff_file: Path = Path()
    mac_file: Path = Path()
    ssh_log: Path = Path()

    @cached_property
    def shared_owner(self) -> SharedStaffOwner:
        owner = SharedStaffOwner(self.root)
        self.addCleanup(owner.close)
        return owner

    def staff_state(self) -> tuple[bytes, int]:
        return self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns

    def assert_only_current_session_changed(self, before: bytes, old: staff.StaffRow, identity: str) -> None:
        self.assertEqual(self.staff_file.read_bytes(), before.replace(
            f'"current_session": "{old.current_session}"'.encode(),
            f'"current_session": "{identity}"'.encode(), 1))

    @override
    def setUp(self) -> None:
        _ = self.enterContext(redirect_stdout(io.StringIO()))
        _ = self.enterContext(redirect_stderr(io.StringIO()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.staff_file = self.root / "owner" / "staff.json"
        self.mac_file = self.root / "mac" / ".local/state/staff/staff.json"
        self.ssh_log = self.root / "ssh.jsonl"
        binaries = self.root / "bin"
        binaries.mkdir()
        ssh = binaries / "ssh"
        write_text_atomic(ssh, f"#!{sys.executable}\n{FAKE_SSH}")
        ssh.chmod(0o755)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            **interpreter_cache_environment(self.root / "cache"),
            "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "STAFF_TEST_SSH_LOG": str(self.ssh_log),
            "STAFF_TEST_OWNER_FILE": str(self.staff_file),
            "STAFF_TEST_CHECKOUT": str(CHECKOUT),
            "STAFF_TEST_SCRIPT": str(STAFF_SCRIPT),
            "STAFF_TEST_SSH_FAIL": "", "STAFF_TEST_OWNER_PORT": "",
            "STAFF_TEST_SSH_EXIT": "255",
            "STAFF_TEST_SSH_REASON": "ssh: connection refused",
            "STAFF_TEST_PLATFORM": "linux",
            "STAFF_TEST_OWNER_SOURCE": str(self.root / "natedev-sources"),
            "STAFF_TEST_LOCK_NOTICE": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "STAFF_CONFIG": str(CHECKOUT / "config/staff.conf"),
        })))
        _ = os.environ.pop("CLAUDE_PYTHON", None)
        _ = self.enterContext(isolated_staff_file(self.staff_file, {}))
        _ = self.enterContext(self.machine("natedev"))

    @property
    def source_root(self) -> Path:
        return Path(os.environ["STAFF_TEST_SOURCE_ROOT"])

    @contextmanager
    def machine(self, machine: FixtureMachine) -> Generator[None]:
        """Select this machine's source folders and lock; SSH retains owner state."""
        source = self.root / f"{machine}-sources"
        environment = source_environment(source)
        for folder in environment.values():
            Path(folder).mkdir(parents=True, exist_ok=True)
        environment.update({
            "STAFF_TEST_SOURCE_ROOT": str(source),
            "STAFF_TEST_PLATFORM": "linux" if machine == "natedev" else "darwin",
            "HERDR_SESSION": "agents",
        })
        if machine == "mac":
            environment["STAFF_TEST_OWNER_PORT"] = self.shared_owner.port
        with ExitStack() as contexts:
            _ = contexts.enter_context(isolated_staff_file(
                self.staff_file if machine == "natedev" else self.mac_file, environment,
            ))
            _ = contexts.enter_context(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
            _ = contexts.enter_context(mock.patch(
                "scripts.agents.remote.other_machine", return_value="mac" if machine == "natedev" else "natedev",
            ))
            for module in {remote, sys.modules.get("remote", remote)}:
                _ = contexts.enter_context(mock.patch.object(module, "local_machine", return_value=machine))
            _ = contexts.enter_context(isolated_source_readers())
            if machine == "mac":
                _ = os.environ.pop("STAFF_FILE", None)
                os.environ["HOME"] = str(self.root / "mac")
            yield

    def tree(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        showrunner = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=showrunner.id)
        worker = fixture_member("worker", name="impl1", parent=unit.id)
        return showrunner, unit, worker

    def cli(self, *arguments: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        return self.script(STAFF_SCRIPT, *arguments, code=code)

    def script(self, script: Path, *arguments: str, code: int = 0, input: str | None = None) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", OWNER_CLI, str(script), *arguments],
            cwd=CHECKOUT, input=input, capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(completed.returncode, code, completed.stderr)
        return completed

    def write_document(self, document: object) -> bytes:
        self.write_json(self.staff_file, document)
        return self.staff_file.read_bytes()

    def staff_config(self, *, lock_seconds: float = 0.2,
                     remote_seconds: float = 2.0) -> Path:
        """Write private limits that the staff writer reads at each use."""
        path = self.root / "staff.conf"
        write_text_atomic(path, "".join(
            f"# range: 5e-324..\n{key}={value}\n" for key, value in (
                ("OPERATION_LOCK_SECONDS", lock_seconds),
                ("REMOTE_SECONDS", remote_seconds),
            )
        ))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"STAFF_CONFIG": str(path)})))
        return path

    def forget_rows(self, *identities: staff.StaffId) -> None:
        """Model a missing legacy identity; production retirement deliberately keeps owners."""
        record = staff.snapshot()
        _ = self.write_document({"schema": record.schema,
            "rows": [row.to_json() for row in record.rows if row.id not in identities],
            "pending_retire": [identity for identity in record.pending_retire if identity not in identities]})

    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")

    def production(self, production_name: str, *units: str, workspace: str = "", parent: str = "") -> Path:
        """Write real timer/doc sources; each listed unit has a staff row and a folder."""
        doc = self.source_root / f"{production_name}-production.md"
        self.write_production_doc(doc, workspace=workspace or production_name)
        self.register_production(production_name, doc, name=workspace or production_name)
        for unit in units:
            _ = self.worktree(unit)
            owner = fixture_runtime_id("showrunner::" + (workspace or production_name))
            from scripts.production import unit_names

            identity = fixture_runtime_id("unit:" + owner + ":" + unit_names.name(unit))
            # A test can make or rename the row first; keep that row.
            if staff.initial_snapshot().by_id(staff.StaffId(identity)) is None:
                _ = unit_membership(workspace or production_name, unit)
            self.pane(f"fixture:{production_name}/{unit}", session_id=identity,
                      environment={"CLAUDE_CODE_SESSION_ID": identity},
                      written_at="2026-10-01T00:00:00+00:00")
        if parent:
            parent_file = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / production_name / "parent"
            parent_file.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(parent_file, parent + "\n")
        return doc

    def register_production(self, production_name: str, doc: Path, *, name: str = "") -> None:
        """Register an existing fixture document without making a second production."""
        row = recorded_showrunner(name or production_name, doc)
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / f"showrunner-{row.id}" / "conf"
        timer.parent.mkdir(parents=True, exist_ok=True)
        identity = row.id
        write_text_atomic(timer, f"TARGET=session:{identity}\nCHECK=zsh /fixture/production_check.sh {row.id}\n")

    def record_production(self, production_name: str, doc: Path) -> staff.StaffRow:
        """Register the timer and record its private owner document."""
        row = recorded_showrunner(production_name, doc)
        self.register_production(production_name, doc, name=row.name)
        return row

    def write_production_doc(self, doc: Path, *, workspace: str) -> None:
        del workspace
        write_text_atomic(doc, "# Fixture production\n")

    def worktree(self, unit: str) -> Path:
        folder = self.source_root / "worktrees" / unit
        folder.mkdir(parents=True, exist_ok=True)
        write_text_atomic(folder / ".git", "gitdir: /fixture/linked-worktree\n")
        return folder

    def fixture_git(self, folder: Path, *arguments: str) -> str:
        """Run Git only inside one of this fixture's private repositories."""
        self.assertTrue(folder.is_relative_to(self.root))
        result = subprocess.run(
            ["git", "-C", str(folder), *arguments], capture_output=True, text=True,
            check=True, timeout=TEST_HANG_GUARD_SECS,
        )
        return result.stdout.strip()

    def fill_production(self, production_name: str, *units: str) -> Path:
        """Build a timer doc and real linked worktrees for the live fill."""
        repository = self.source_root / "repositories" / production_name
        repository.mkdir(parents=True)
        _ = self.fixture_git(repository, "init", "-b", "main")
        _ = self.fixture_git(repository, "config", "user.email", "fixture@example.invalid")
        _ = self.fixture_git(repository, "config", "user.name", "Fixture")
        _ = self.fixture_git(repository, "commit", "--allow-empty", "-m", "fixture")
        for unit in units:
            folder = self.source_root / "worktrees" / unit
            folder.parent.mkdir(parents=True, exist_ok=True)
            _ = self.fixture_git(repository, "worktree", "add", "-b", unit, str(folder))
            write_text_atomic(folder / ".unit.toml", 'plan = "standby"\nresponsibilities = []\n')
        doc = repository / "docs" / f"{production_name}-production.md"
        doc.parent.mkdir()
        self.write_production_doc(doc, workspace=production_name)
        self.register_production(production_name, doc)
        production_membership(production_name, *units)
        for unit in units:
            row = unit_membership(production_name, unit)
            _ = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), self.source_root / "worktrees" / unit),
                                   expected=row.worktree)
        # Do not import the checkout's real showrunner registry into the fill.
        registry = self.source_root / "showrunners.json"
        self.write_json(registry, {"always": []})
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {"SHOWRUNNERS_CONFIG": str(registry)})))
        return doc

    def row_production(self) -> RowProductionFixture:
        """Create private git evidence and conflicting legacy metadata."""
        production_name = "timer-alias"
        repository = self.source_root / "repositories" / production_name
        repository.mkdir(parents=True)
        _ = self.fixture_git(repository, "init", "-b", "merge-from-git")
        _ = self.fixture_git(repository, "config", "user.email", "fixture@example.invalid")
        _ = self.fixture_git(repository, "config", "user.name", "Fixture")
        _ = self.fixture_git(repository, "commit", "--allow-empty", "-m", "fixture")
        folder = self.source_root / "worktrees" / "folder-is-not-the-unit-name"
        folder.parent.mkdir(parents=True)
        _ = self.fixture_git(repository, "worktree", "add", "-b", "branch-from-git", str(folder))
        write_text_atomic(folder / ".unit.toml", 'plan = "docs/plans/from-config.md"\n'
                          + 'port = 4317\nresponsibilities = ["shared-fixture"]\n')
        doc = repository / "docs" / "row-production.md"
        doc.parent.mkdir()
        write_text_atomic(doc, "# Production\n")
        runner = fixture_member("showrunner", name="row-owner", doc=staff.LiveDoc(remote.local_machine(), doc))
        unit = fixture_member("unit", name="member", parent=runner.id,
                              worktree=staff.PresentWorktree(remote.local_machine(), folder))
        worker = fixture_worker("reader", unit.id)
        runner = staff.update_current_session(runner.current_session, staff.native_id(fixture_runtime_id("row-owner-current")))
        unit = staff.update_current_session(unit.current_session, staff.native_id(fixture_runtime_id("row-member-current")))
        current = staff.native_id(fixture_runtime_id("row-worker-current"))
        fixture_session(current, CLAUDE_BACKEND)
        worker = staff.update_current_session(worker.current_session, current)
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / f"showrunner-{runner.id}" / "conf"
        timer.parent.mkdir(parents=True)
        write_text_atomic(timer, f"TARGET=session:{runner.id}\n"
                          + f"CHECK=zsh /fixture/production_check.sh {runner.id}\n")
        return RowProductionFixture(runner, unit, worker, doc, folder, runner.name)

    def separated_unit_folders(self) -> RowProductionFixture:
        """Give the production doc its checkout fields beside the unit's own folder."""
        fixture = self.row_production()
        repository = fixture.doc.parents[1]
        text = fixture.doc.read_text().replace("# Production\n", "# Production\n"
                            + f"- **Repository:** `{repository}`\n"
                            + f"- **Showrunner checkout:** `{repository}`\n"
                            + "- **Merge branch:** `merge-from-git`\n"
                            + "- **Log:** `production.log`\n")
        _ = fixture.doc.write_text(text)
        return fixture

    def pane(self, pane_id: str, *, session_id: str, environment: dict[str, str], written_at: str = "2026-10-30T09:00:00+00:00") -> None:
        path = Path(os.environ["HERDR_PANE_RECORD_DIR"]) / "agents.json"
        entries = cast(dict[str, object], json.loads(path.read_text())) if path.exists() else {}
        entries[pane_id] = {
            "pane_id": pane_id, "session_id": session_id, "config_dir": "/fixture/account", "cwd": str(self.source_root),
            "name": "old-tab-name", "model": "", "remote_control": False, "environment": environment,
            "flags_read": True, "ended": False, "written_at": written_at,
        }
        self.write_json(path, entries)

    def session_transcript(self, session_id: str, folder: Path, *, home: Path | None = None) -> Path:
        """Write one interactive transcript under its actual project directory."""
        from scripts.production import unit_move

        path = (home or Path.home()) / ".claude/projects" / unit_move.project_folder(str(folder)) / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({"cwd": str(folder), "entrypoint": "cli"}) + "\n")
        return path

    def session_record(self, session_id: str, name: str) -> Path:
        """A registry record tied to this test's real, private process identity."""
        from scripts.message import sessions

        path = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{session_id}.json"
        self.write_json(path, {
            "sessionId": session_id, "name": name, "pid": os.getpid(),
            "procStart": sessions.process_start(os.getpid()), "messagingSocketPath": "",
            "updatedAt": 1, "formerNames": [], "tmux": "", "entrypoint": "cli",
        })
        return path

    def worker_run(self, production_name: str, unit: str, director: str, *,
                   passes: tuple[dict[str, object], ...] = (), director_run: bool = False) -> Path:
        """Create private run evidence for a director and its worker passes."""
        run = Path(os.environ["ROSTER_RUN_ROOT"]) / f"{production_name}-{unit}"
        run.mkdir(parents=True, exist_ok=True)
        self.write_json(run / "progress_history_state.json", {
            "production": production_name, "unit": unit,
            "main_agent": {"family": "claude", "session_id": director},
            "pass": {str(passed["worker_name"]): passed for passed in passes},
        })
        write_text_atomic(run / "director", director + "\n")
        if director_run:
            owner = staff.local_snapshot().by_current_session(director)
            assert owner is not None
            _ = staff.set_run(owner.id, staff.OpenRun(run))
        return run

    @contextmanager
    def lock_notice(self, lock: Path) -> Generator[socket.socket]:
        with short_socket_directory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = Path(directory) / "operation-notice.sock"
            listener.bind(str(path))
            listener.listen()
            listener.settimeout(TEST_HANG_GUARD_SECS)
            with mock.patch.dict(os.environ, {"STAFF_TEST_LOCK_NOTICE": str(path), "STAFF_TEST_LOCK_TARGET": str(lock)}):
                yield listener

    def start_script(self, script: Path, *arguments: str) -> subprocess.Popen[str]:
        pending = subprocess.Popen(
            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", OWNER_CLI, str(script), *arguments], cwd=CHECKOUT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def clean_up() -> None:
            if pending.returncode is None:
                pending.terminate()
                _ = pending.communicate(timeout=TEST_HANG_GUARD_SECS)

        self.addCleanup(clean_up)
        return pending

    def assert_operation_blocks(self, pending: subprocess.Popen[str], listener: socket.socket) -> None:
        connection = listener.accept()[0]
        with connection:
            self.assertEqual(connection.recv(9), b"operation")
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = pending.communicate(timeout=0.1)

    def ssh_commands(self) -> list[list[str]]:
        commands: list[list[str]] = []
        for line in self.ssh_log.read_text().splitlines():
            arguments = cast(list[str], json.loads(line))
            self.assertEqual(arguments[:3], ["-o", "BatchMode=yes", "-o"])
            self.assertEqual(arguments[4], "natedev")
            self.assertRegex(arguments[3], r"^ConnectTimeout=[1-9][0-9]*$")
            command = shlex.split(arguments[5]) if len(arguments) == 6 else arguments[5:]
            self.assertEqual(command[:2], ["~/.claude/scripts/lib/py", "~/.claude/scripts/production/staff.py"])
            self.assertIn("--json", command[2:])
            commands.append(command[2:])
        return commands
