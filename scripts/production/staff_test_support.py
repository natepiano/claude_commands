"""The shared owner/Mac staff fixture, source builders and real fake-SSH transport."""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from io import IOBase
from pathlib import Path
from typing import Literal, cast, override
from unittest import mock

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.shutdown import pane_record

CHECKOUT = Path(__file__).resolve().parents[2]
STAFF_SCRIPT = Path(__file__).with_name("staff.py")
SOURCES_SCRIPT = Path(__file__).with_name("staff_sources.py")
PROCESS_TIMEOUT = 30
FixtureMachine = Literal["natedev", "mac"]


@contextmanager
def isolated_staff_file(path: Path, environment: dict[str, str]) -> Generator[None]:
    """Use the same private record in this process and its fixture children."""
    environment["STAFF_FILE"] = str(path)
    with ExitStack() as contexts:
        _ = contexts.enter_context(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {"STAFF_FILE": str(path)})))
        if "HERDR_PANE_RECORD_DIR" in environment:
            _ = contexts.enter_context(mock.patch.object(
                pane_record, "RECORD_DIR", Path(environment["HERDR_PANE_RECORD_DIR"]),
            ))
        yield


def showrunner_relationship(name: str, parent: str = "") -> staff.StaffRow:
    """Find fixture identities by their short names, then set the desired edge."""
    with staff.operation():
        rows = {row.name: row for row in staff.initial_snapshot().find(kind="showrunner")}

        def intended_row(short_name: str) -> staff.StaffRow:
            if short_name not in rows:
                rows[short_name] = staff.ensure("showrunner", name=short_name)
            return rows[short_name]

        child = intended_row(name)
        parent_id = intended_row(parent).id if parent else None
        return staff.move(child.id, parent_id) if child.parent != parent_id else child


def showrunner_launch(row: staff.StaffRow, session_id: str, environment: dict[str, str]) -> None:
    """Pin timer identities when several fixture timers share one production doc."""
    launch = pane_record.launch(f"fixture:{session_id}", session_id, str(CHECKOUT), {}, ["claude", "-n", row.name])
    launch["environment"]["STAFF_ID"] = row.id
    with mock.patch.object(pane_record, "RECORD_DIR", Path(environment["HERDR_PANE_RECORD_DIR"])):
        pane_record.write("agents", launch)


def unit_membership(showrunner: str, unit: str) -> staff.StaffRow:
    """Add an intended fixture member using its displayed short name."""
    from scripts.production import unit_names

    parents = [row for row in staff.initial_snapshot().find(kind="showrunner") if row.name == showrunner]
    parent = parents[0] if parents else staff.ensure("showrunner", name=showrunner)
    return staff.ensure("unit", name=unit_names.name(unit), parent=parent.id)


def production_membership(showrunner: str, *units: str) -> None:
    """Seed the current members of a fixture production, including an empty one."""
    _ = showrunner_relationship(showrunner)
    for unit in units:
        _ = unit_membership(showrunner, unit)


def fixture_showrunner_name(slug: str, doc: Path) -> str:
    """Keep the real doc's workspace name; extra fixture productions use their slugs."""
    from scripts.production import showrunners, production_units

    return showrunners.herdr_space(slug, str(doc)) if slug == production_units.read_production(doc).slug else slug


def source_environment(source: Path) -> dict[str, str]:
    return {key: str(source / folder) for key, folder in (
        ("NOTIFIER_STATE_DIR", "notifier"), ("SHOWRUNNER_STATE_DIR", "showrunners"),
        ("HERDR_PANE_RECORD_DIR", "panes"), ("NOTIFIER_SESSIONS_DIR", "sessions"),
        ("XDG_CONFIG_HOME", "config"),
        ("UNIT_WORKERS_STATE", "unit-workers"), ("ROSTER_RUN_ROOT", "runs"),
        ("REVIEW_POOL_STATE", "review-pool"), ("ASTRA_ROLES_STATE", "astra-roles"),
        ("DESIGN_ROLES_STATE", "design-roles"), ("DESIGN_OWNERSHIP_STATE", "design-ownership"),
    )}


OWNER_CLI = """import os, runpy, sys
sys.path.insert(0, os.environ['STAFF_TEST_CHECKOUT'])
from scripts.production.staff_test_support import isolated_source_readers, observe_operation_lock
observe_operation_lock()
sys.platform = os.environ.get('STAFF_TEST_PLATFORM', 'linux')
script = sys.argv.pop(1)
sys.argv[0] = script
if script.endswith('staff_sources.py') or '--live' in sys.argv:
    from scripts.production import staff_sources
    with isolated_source_readers():
        runpy.run_path(script, run_name='__main__')
else:
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
    with ExitStack() as contexts:
        for module_name, attributes in (
            ("showrunners", {
                "NOTIFIER_STATE_DIR": Path(os.environ["NOTIFIER_STATE_DIR"]),
                "SHOWRUNNER_STATE_DIR": Path(os.environ["SHOWRUNNER_STATE_DIR"]),
                "SESSIONS_DIR": Path(os.environ["NOTIFIER_SESSIONS_DIR"]),
            }),
            ("pane_record", {"RECORD_DIR": Path(os.environ["HERDR_PANE_RECORD_DIR"])}),
        ):
            qualified = "scripts.production.showrunners" if module_name == "showrunners" else "scripts.shutdown.pane_record"
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
                _ = contexts.enter_context(mock.patch.object(module, "marked_units", return_value={}))
        yield


# Execute the real CLI against a different temp file: this is the owner, not a
# canned response, so remote reads, writes, and error mapping must all agree.
FAKE_SSH = """import json, os, runpy, shlex, sys
from pathlib import Path
sys.path.insert(0, os.environ['STAFF_TEST_CHECKOUT'])
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.staff_test_support import source_environment
arguments = sys.argv[1:]
log = Path(os.environ['STAFF_TEST_SSH_LOG'])
previous = log.read_text() if log.exists() else ''
write_text_atomic(log, previous + json.dumps(arguments) + '\\n')
if os.environ.get('STAFF_TEST_SSH_FAIL'):
    reason = os.environ.get('STAFF_TEST_SSH_REASON', 'ssh: connection refused')
    if reason:
        print(reason, file=sys.stderr)
    raise SystemExit(int(os.environ.get('STAFF_TEST_SSH_EXIT', '255')))
if len(arguments) < 6 or arguments[:5] != ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'natedev']:
    raise SystemExit('unexpected SSH target or options')
command = arguments[5:]
if len(command) == 1:
    command = shlex.split(command[0])
if command[:2] != ['~/.claude/scripts/lib/py', '~/.claude/scripts/production/staff.py']:
    raise SystemExit('unexpected remote Python or staff script')
if '--json' not in command[2:]:
    raise SystemExit('remote staff call requires --json')
os.environ['STAFF_FILE'] = os.environ['STAFF_TEST_OWNER_FILE']
source = Path(os.environ['STAFF_TEST_OWNER_SOURCE'])
os.environ.update(source_environment(source))
sys.platform = 'linux'
sys.path.insert(0, os.environ['STAFF_TEST_CHECKOUT'])
sys.argv = [os.environ['STAFF_TEST_SCRIPT'], *command[2:]]
from scripts.production.staff_test_support import observe_operation_lock
observe_operation_lock()
runpy.run_path(sys.argv[0], run_name='__main__')
"""


class StaffTestCase(unittest.TestCase):
    root: Path = Path()
    staff_file: Path = Path()
    mac_file: Path = Path()
    ssh_log: Path = Path()

    @override
    def setUp(self) -> None:
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
            "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "STAFF_TEST_SSH_LOG": str(self.ssh_log),
            "STAFF_TEST_OWNER_FILE": str(self.staff_file),
            "STAFF_TEST_CHECKOUT": str(CHECKOUT),
            "STAFF_TEST_SCRIPT": str(STAFF_SCRIPT),
            "STAFF_TEST_SSH_FAIL": "",
            "STAFF_TEST_SSH_EXIT": "255",
            "STAFF_TEST_SSH_REASON": "ssh: connection refused",
            "STAFF_TEST_PLATFORM": "linux",
            "STAFF_TEST_OWNER_SOURCE": str(self.root / "natedev-sources"),
            "STAFF_TEST_LOCK_NOTICE": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        })))
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
        with ExitStack() as contexts:
            _ = contexts.enter_context(isolated_staff_file(
                self.staff_file if machine == "natedev" else self.mac_file, environment,
            ))
            _ = contexts.enter_context(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
            _ = contexts.enter_context(mock.patch(
                "scripts.shutdown.remote.other_machine", return_value="mac" if machine == "natedev" else "natedev",
            ))
            _ = contexts.enter_context(isolated_source_readers())
            if machine == "mac":
                _ = os.environ.pop("STAFF_FILE", None)
                os.environ["HOME"] = str(self.root / "mac")
            yield

    def tree(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        showrunner = staff.ensure("showrunner", name="sdlc")
        unit = staff.ensure("unit", name="models", parent=showrunner.id)
        worker = staff.ensure("worker", name="impl1", parent=unit.id)
        return showrunner, unit, worker

    def cli(self, *arguments: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        return self.script(STAFF_SCRIPT, *arguments, code=code)

    def script(self, script: Path, *arguments: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, "-c", OWNER_CLI, str(script), *arguments],
            cwd=CHECKOUT, capture_output=True, text=True, check=False,
            timeout=PROCESS_TIMEOUT,
        )
        self.assertEqual(completed.returncode, code, completed.stderr)
        return completed

    def write_document(self, document: object) -> bytes:
        self.write_json(self.staff_file, document)
        return self.staff_file.read_bytes()

    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")

    def production(self, slug: str, *units: str, workspace: str = "", parent: str = "") -> Path:
        """Write real timer/doc sources; each listed unit initially has a worktree."""
        doc = self.source_root / f"{slug}-production.md"
        self.units_doc(doc, units, workspace=workspace or slug)
        self.register_production(slug, doc)
        for unit in units:
            _ = self.worktree(unit)
        if parent:
            parent_file = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / slug / "parent"
            parent_file.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(parent_file, parent + "\n")
        return doc

    def register_production(self, slug: str, doc: Path) -> None:
        """Register an existing fixture document without making a second production."""
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / f"showrunner-{slug}" / "conf"
        timer.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(timer, f"TARGET=session:session-{slug}\nCHECK=zsh /fixture/production_check.sh {shlex.quote(str(doc))}\n")

    def units_doc(self, doc: Path, units: tuple[str, ...], *, workspace: str, retired: tuple[str, ...] = ()) -> None:
        rows = [f"| `{unit}` | `{self.source_root / 'worktrees' / unit}` | {'retired' if unit in retired else 'active'} |" for unit in units]
        write_text_atomic(doc, "\n".join([
            "# Fixture production", f"- **Showrunner session:** `{workspace}`", "", "## Units", "",
            "| Unit | Worktree | Plan |", "| --- | --- | --- |", *rows, "",
        ]))

    def worktree(self, unit: str) -> Path:
        folder = self.source_root / "worktrees" / unit
        folder.mkdir(parents=True, exist_ok=True)
        write_text_atomic(folder / ".git", "gitdir: /fixture/linked-worktree\n")
        return folder

    def pane(self, pane_id: str, *, session_id: str, environment: dict[str, str], written_at: str = "2026-10-30T09:00:00+00:00") -> None:
        path = Path(os.environ["HERDR_PANE_RECORD_DIR"]) / "agents.json"
        entries = cast(dict[str, object], json.loads(path.read_text())) if path.exists() else {}
        entries[pane_id] = {
            "pane_id": pane_id, "session_id": session_id, "config_dir": "/fixture/account", "cwd": str(self.source_root),
            "name": "old-tab-name", "model": "", "remote_control": False, "environment": environment,
            "flags_read": True, "ended": False, "written_at": written_at,
        }
        self.write_json(path, entries)

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

    def worker_run(self, slug: str, unit: str, director: str) -> Path:
        """A unit run with actual state, roster, server and director records."""
        run = Path(os.environ["ROSTER_RUN_ROOT"]) / f"{slug}-{unit}"
        run.mkdir(parents=True, exist_ok=True)
        self.write_json(run / "progress_history_state.json", {
            "production": slug, "unit": unit,
            "main_agent": {"family": "claude", "session_id": director},
        })
        write_text_atomic(run / "director", director + "\n")
        self.write_json(run / "mesh_roster.json", {})
        self.write_json(run / "mesh_server.json", {"pid": os.getpid(), "codex_home": str(self.root / "codex")})
        return run

    def ledger_worker(self, run: Path, slug: str, unit: str, name: str, role: str, identity: str, *,
                      family: str = "codex", retired: bool = False, staff_id: str = "") -> Path:
        """Write one unit-ledger entry and its run projection through the shared JSON writer."""
        path = Path(os.environ["UNIT_WORKERS_STATE"]) / slug / f"{unit}.json"
        ledger: dict[str, object] = (cast(dict[str, object], json.loads(path.read_text()))
                                   if path.exists() else {"version": 1, "workers": {}})
        entries = cast(dict[str, object], ledger["workers"])
        backend = ({"kind": "CodexThread", "thread_id": identity, "codex_home": str(self.root / "codex")}
                   if family == "codex" else {"kind": "ClaudeSession", "session_id": identity})
        entries[name] = {
            "name": name, "role": role, "family": family, "model": "", "effort": "",
            "backend": backend, "ownership": {"kind": "Retired"} if retired else {"kind": "HeldBy", "run": str(run)},
            "access_policy": {}, "events": [], **({"staff_id": staff_id} if staff_id else {}),
        }
        self.write_json(path, ledger)
        _ = self.roster_worker(run, name, role, identity, status="ended" if retired else "done",
                               family=family, staff_id=staff_id)
        return path

    def pool_reviewer(self, slug: str, name: str, thread: str, *, unit: str = "", busy_pid: int = 0) -> Path:
        """A legacy pool source whose holder is its production, independent of its lease."""
        path = Path(os.environ["REVIEW_POOL_STATE"]) / slug / "pool.json"
        self.write_json(path, {
            "generation": 1, "templates": {}, "next_index": {"code": 2},
            "reviewers": {name: {
                "type": "code", "thread_id": thread, "prime_hash": "fixture", "reviews": 0,
                "lease": {"unit": unit, "ordinal": 1} if unit else None,
                "busy_pid": busy_pid or None, "last_allocated": 1,
            }},
        })
        return path

    def astra_worker(self, run: Path, name: str, thread: str, manager: str) -> Path:
        """A live mesh Astra hold, using the same run roster as ordinary workers."""
        from scripts.astra import astra_roles

        _ = self.roster_worker(run, name, "review", thread)
        _ = astra_roles.hold("reviewer", session_dir=run, worker=name, manager=manager)
        path = Path(os.environ["ASTRA_ROLES_STATE"]) / f"{thread}.json"
        return path

    def roster_worker(self, run: Path, name: str, role: str, identity: str, *, status: str = "running",
                      family: str = "codex", staff_id: str = "") -> Path:
        """Write the mesh producer's standalone roster shape, without a unit ledger."""
        path = run / "mesh_roster.json"
        roster = cast(dict[str, object], json.loads(path.read_text()))
        roster[name] = {"role": role, "status": status, "family": family,
                        "thread_id" if family == "codex" else "worker_id": identity,
                        "launcher_pid": os.getpid(), "port": 12345,
                        **({"staff_id": staff_id} if staff_id else {})}
        self.write_json(path, roster)
        return path

    def legacy_claude_worker(self, run: Path, session_id: str, *, prefix: str,
                             role: str, kind: str = "impl") -> str:
        """Use the launcher's name and board registration with agent_bg's TSV ledger."""
        direct = CHECKOUT / "scripts/direct"
        named = subprocess.run(
            ["bash", str(direct / "worker_name.sh"), str(self.root), "impl", kind, prefix],
            capture_output=True, text=True, check=True, timeout=PROCESS_TIMEOUT,
        )
        name = named.stdout.strip()
        _ = subprocess.run(
            ["bash", str(direct / "board.sh"), "post", str(run), "impl", "register",
             f"mesh={name}"], capture_output=True, text=True, check=True, timeout=PROCESS_TIMEOUT,
        )
        if role:
            _ = subprocess.run(
                ["bash", str(direct / "board.sh"), "role", str(run), "impl", role],
                capture_output=True, text=True, check=True, timeout=PROCESS_TIMEOUT,
            )
        write_text_atomic(run / "workers", f"{session_id[:8]}\t{name}\n")
        return name

    def worker_design(self, slug: str, name: str, thread: str) -> Path:
        """Persist the durable design producer's tagged record, without a ledger."""
        from scripts.astra import design_roles

        design_id = "12345678-1234-5678-1234-567812345678"
        design = design_roles.HeldDesign(
            design_roles.DesignPlacement(design_id, design_roles.local_host(), 1),
            "fixture", str(self.source_root), str(self.source_root / "design.md"), "fixture",
            "2026-10-31T00:00:00+00:00", design_roles.ShowrunnerHolder(slug), design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", name, design_roles.RoleOpen(thread)),
             design_roles.RoleInstance("reviewer", "sdlc-design-astra-reviewer", design_roles.RoleNotOpened())),
        )
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        return design_roles.design_directory(design_id) / "design.json"

    @contextmanager
    def lock_notice(self, lock: Path) -> Generator[socket.socket]:
        path = self.root / "operation-notice.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            listener.listen()
            listener.settimeout(PROCESS_TIMEOUT)
            with mock.patch.dict(os.environ, {"STAFF_TEST_LOCK_NOTICE": str(path), "STAFF_TEST_LOCK_TARGET": str(lock)}):
                yield listener

    def start_script(self, script: Path, *arguments: str) -> subprocess.Popen[str]:
        pending = subprocess.Popen(
            [sys.executable, "-c", OWNER_CLI, str(script), *arguments], cwd=CHECKOUT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def clean_up() -> None:
            if pending.returncode is None:
                pending.terminate()
                _ = pending.communicate(timeout=PROCESS_TIMEOUT)

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
            self.assertEqual(arguments[:5], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "natedev"])
            command = shlex.split(arguments[5]) if len(arguments) == 6 else arguments[5:]
            self.assertEqual(command[:2], ["~/.claude/scripts/lib/py", "~/.claude/scripts/production/staff.py"])
            self.assertIn("--json", command[2:])
            commands.append(command[2:])
        return commands
