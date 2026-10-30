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

CHECKOUT = Path(__file__).resolve().parents[2]
STAFF_SCRIPT = Path(__file__).with_name("staff.py")
SOURCES_SCRIPT = Path(__file__).with_name("staff_sources.py")
PROCESS_TIMEOUT = 30
FixtureMachine = Literal["natedev", "mac"]


def source_environment(source: Path) -> dict[str, str]:
    return {key: str(source / folder) for key, folder in (
        ("NOTIFIER_STATE_DIR", "notifier"), ("SHOWRUNNER_STATE_DIR", "showrunners"),
        ("HERDR_PANE_RECORD_DIR", "panes"), ("NOTIFIER_SESSIONS_DIR", "sessions"),
        ("XDG_CONFIG_HOME", "config"),
    )}


OWNER_CLI = """import os, runpy, sys
sys.path.insert(0, os.environ['STAFF_TEST_CHECKOUT'])
from scripts.production.staff_test_support import isolated_source_readers, observe_operation_lock
observe_operation_lock()
sys.platform = os.environ.get('STAFF_TEST_PLATFORM', 'linux')
script = sys.argv.pop(1)
sys.argv[0] = script
if script.endswith('staff_sources.py'):
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

    def report_then_lock(lock_file: int | IOBase, operation: int) -> None:
        if operation == fcntl.LOCK_EX and watched.exists():
            descriptor = lock_file if isinstance(lock_file, int) else lock_file.fileno()
            opened, target = os.fstat(descriptor), watched.stat()
            if (opened.st_dev, opened.st_ino) == (target.st_dev, target.st_ino):
                with socket.socket(socket.AF_UNIX) as signal:
                    signal.connect(notice)
                    signal.sendall(b"operation")
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
        self.mac_file = self.root / "mac" / "staff.json"
        self.ssh_log = self.root / "ssh.jsonl"
        binaries = self.root / "bin"
        binaries.mkdir()
        ssh = binaries / "ssh"
        write_text_atomic(ssh, f"#!{sys.executable}\n{FAKE_SSH}")
        ssh.chmod(0o755)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "STAFF_FILE": str(self.staff_file),
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
            "STAFF_FILE": str(self.staff_file if machine == "natedev" else self.mac_file),
            "STAFF_TEST_SOURCE_ROOT": str(source),
            "STAFF_TEST_PLATFORM": "linux" if machine == "natedev" else "darwin",
            "HERDR_SESSION": "agents",
        })
        with ExitStack() as contexts:
            _ = contexts.enter_context(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
            _ = contexts.enter_context(mock.patch(
                "scripts.shutdown.remote.other_machine", return_value="mac" if machine == "natedev" else "natedev",
            ))
            _ = contexts.enter_context(isolated_source_readers())
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
