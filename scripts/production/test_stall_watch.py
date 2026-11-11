"""Stall watcher tests with isolated notifier, sessions, processes and relays."""

from __future__ import annotations

import fcntl
import hashlib
import io
from contextlib import redirect_stderr
import json
import os
import runpy
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest
from threading import current_thread, main_thread
from types import ModuleType
from unittest import mock
from datetime import datetime
from pathlib import Path

from typing import cast, override
from zoneinfo import ZoneInfo

from scripts.agents import detached_runs
from scripts.lib import unit_runs
from scripts.lib.test_models_registry import write_registry
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import showrunners, staff
from scripts.agents.remote import local_machine
from scripts.production.staff_test_support import isolated_staff_file, showrunner_relationship, unit_membership, fixture_member, fixture_runtime_id
from scripts.production import fake_unit_host
from scripts.production.fake_unit_host import HostedUnit
from scripts.herdr import fake_herdr
from scripts.herdr import herdr, worker_panes
from scripts.production import stall_watch
from scripts.production.session_pane import HerdrFailure, read_place, agent_status
from scripts.hooks import context_usage
from scripts.message import rca
from scripts.whoami import account, usage_store
from scripts.whoami.agent_accounts import UsageReadOutcome
from scripts.production.cli_fixture import invoke_cli
from scripts.production.test_showrunners import stalled_session_reader
from scripts.production import unit_lookup


SCRIPT = Path(__file__).with_name("stall_watch.py")
START = int(datetime(2026, 10, 6, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())
PANE = "Ready for the next step.\n"
EDIT_PROMPT = ("— holding: waiting on the seats\n\n Edit file\n scripts/herdr/panes.py\n"
               + " Do you want to make this edit to panes.py?\n ❯ 1. Yes\n   2. No\n")
BASH_PROMPT = (" Bash command\n\n   sed -n '5,$p' /tmp/saved.txt\n   Read lines 5 to the end of a saved output\n\n"
               + " Do you want to proceed?\n ❯ 1. Yes\n   3. No, and tell Claude what to do differently (esc)\n")
AUTO_DENY_PROMPT = (" Bash command\n\n   sed -n '5,$p' /tmp/saved.txt\n   Read lines 5 to the end of a saved output\n"
                    + " ⚠ Claude Code will automatically deny this request in 0:41, to avoid blocking progress on an"
                    + " unattended session.\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n")

# Claude Code 2.1.296 shows the description first, then the command, then the warning, each wrapped to the pane.
SAVED = "/home/natepiano/.claude1/projects/-home-natepiano-rust-cargo-liner-style-types/3d951fc8/tool-results"
OUTSIDE_PROMPT = (" Bash command\n\n   Write the 212 enrolled state; confirm the prompt names\n   the worktree\n"
                  + "   printf '{\"kind\":\"enrolled\"}' > /tmp/claude/unit/state.json; rm -f\n"
                  + f"   {SAVED}/b1e4yc8b9.txt\n\n"
                  + "   rm -f deletes a path that resolves to\n"
                  + f"   {SAVED.replace('.claude1', '.claude')}/b1e4yc8b9.txt, which is outside the\n"
                  + "   allowed working directories.\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n")
DELETE_CALL = {"command": f"rm -f {SAVED}/b1e4yc8b9.txt",
               "description": "Write the 212 enrolled state; confirm the prompt names the worktree"}

DELETE_REQUEST = "delete its saved tool output b1e4yc8b9.txt, which is outside its allowed folders"
# unit-one's prompt alert key in the showrunner production, and the retire that cancels its repeats.
PROMPT_KEY = "stall-watch:prompt:" + fixture_runtime_id("stall-unit:unit-one")
RETIRE = {"command": "retire", "to": "user", "from": "stall-watch", "key": PROMPT_KEY}


def unit_session(name: str) -> str:
    """The private Claude backend identity for this unit."""
    return fixture_runtime_id("stall-unit:" + name)


def tool_call(name: str, arguments: dict[str, str], at: float, call_id: str = "toolu_1") -> str:
    """A transcript's assistant entry that makes one tool call, as Claude Code writes it."""
    stamp = datetime.fromtimestamp(at, ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": "assistant", "timestamp": stamp, "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": arguments}]}}) + "\n"


def tool_result(call_id: str = "toolu_1") -> str:
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call_id}]}}) + "\n"


def executable(path: Path, source: str) -> None:
    _ = path.write_text(source)
    path.chmod(0o755)


# A marked worker is python, not sleep: the Mac hides a system binary's environment from ps.
MARKED_SLEEPER = [sys.executable, "-c", f"import time; time.sleep({TEST_HANG_GUARD_SECS})"]

def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


class StallWatchTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.watches: dict[str, stall_watch.WatchedUnit] = {}
        self.bin: Path = Path()
        self.notifier: Path = Path()
        self.sessions: Path = Path()
        self.sessions_script: Path = Path()
        self.config: Path = Path()
        self.projects: Path = Path()
        self.state: Path = Path()
        self.ps_file: Path = Path()
        self.send_log: Path = Path()
        self.rca_log: Path = Path()
        self.fail_file: Path = Path()
        self.environment: dict[str, str] = {}
        self.panes: dict[str, dict[str, str | int]] = {}
        self.process_rows: list[str] = []
        self.children: list[subprocess.Popen[bytes]] = []
        # The retired units and the state mark on a unit's herdr pane, where a test sets one.
        self.retired: set[str] = set()
        self.states: dict[str, str] = {}
        self.run_records: dict[str, list[dict[str, object]]] = {}
        self.unheld_runs: set[str] = set()
        self.director_overrides: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(short_socket_directory()))
        _ = cast(object, self.enterContext(mock.patch.multiple(
            unit_runs, RUN_ROOT=self.root / "held", FORMER_RUN_ROOT=self.root / "former-held")))
        self.environment["HERDR_PANE_RECORD_DIR"] = str(self.root / "pane-records")
        self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.notifier = self.root / "notifier"
        self.notifier.mkdir()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.sessions_script = self.root / "sessions.py"
        _ = self.sessions_script.write_text("""import json, os, pathlib, sys
records = [json.loads(path.read_text()) for path in pathlib.Path(os.environ['NOTIFIER_SESSIONS_DIR']).glob('*.json')]
command, target = sys.argv[1:]
errors = json.loads(os.environ.get('STALL_TEST_SESSION_ERRORS', '[]'))
overrides = json.loads(os.environ.get('STALL_TEST_SESSION_OVERRIDES', '{}'))
if command == 'socket' and target in errors:
    raise SystemExit(3)
if command == 'id' and os.environ.get('STALL_TEST_ID_ERROR'):
    print('sessions: one or more registry files could not be read', file=sys.stderr)
    raise SystemExit(3)
if command == 'socket' and target in overrides:
    print(overrides[target])
    raise SystemExit(0)
for record in records:
    if not record['running']:
        continue
    if command == 'socket' and target in (record['name'], 'session:' + record['sessionId']):
        print(record['messagingSocketPath'])
        raise SystemExit(0)
    if command == 'id' and target == str(record['pid']):
        print(record['sessionId'])
        raise SystemExit(0)
raise SystemExit(1)
""")
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.state = self.root / "stall-state"
        self.ps_file = self.root / "ps.txt"
        self.send_log = self.root / "sends.jsonl"
        self.rca_log = self.root / "rca-sends.jsonl"
        self.fail_file = self.root / "fail-targets.json"
        _ = self.fail_file.write_text("[]")
        _ = (self.root / "stall_watch.conf").write_text("prompt_grace_seconds=0\n")
        self.config = self.root / "showrunners.conf"
        self.configure({"showrunner": ["unit-one"]})
        self.addCleanup(self.close_processes)
        executable(self.bin / "ps", "#!/bin/sh\ncat \"$STALL_TEST_PS_FILE\"\n")
        executable(self.bin / "send.py", """#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
retire = args[:1] == ['retire']
record = {name: args[args.index('--' + name) + 1] for name in ('to', 'from', 'key', 'text', 'text-id', 'need')
          if '--' + name in args}
if retire:
    record['command'] = 'retire'
if '--value' in args:
    record['values'] = dict(args[i + 1].split('=', 1) for i, arg in enumerate(args) if arg == '--value')
line = (json.dumps(record) + '\\n').encode()
fd = os.open(os.environ['STALL_TEST_SEND_LOG'], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
os.write(fd, line)
os.close(fd)
expected = int(os.environ.get('STALL_TEST_SEND_TOGETHER', '0'))
deadline = time.monotonic() + float(os.environ['STALL_TEST_HANG_GUARD_SECS'])
while expected and len(open(os.environ['STALL_TEST_SEND_LOG']).readlines()) < expected:
    if time.monotonic() >= deadline:
        raise SystemExit('send barrier did not receive every concurrent delivery')
    time.sleep(0.01)
fail = json.load(open(os.environ['STALL_TEST_FAIL_FILE']))
# A failed retire is send.py's FAILED, exit 3; "retire" in the fail list fails only retires.
if retire:
    raise SystemExit(3 if 'retire' in fail else 0)
raise SystemExit(1 if record['to'] in fail else 0)
""")
        # rca.py's own sends: the owner message and its reminder.
        executable(self.bin / "rca-send.py", """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
record = {name: args[args.index('--' + name) + 1] for name in ('to', 'from', 'key', 'text') if '--' + name in args}
with open(os.environ['STALL_TEST_RCA_LOG'], 'a') as log:
    log.write(json.dumps(record) + '\\n')
""")
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "SHOWRUNNERS_CONFIG": str(self.config),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "HERDR_PANE_RECORD_DIR": str(self.root / "pane-records"),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "SHOWRUNNERS_SESSIONS": str(self.sessions_script),
            "STALL_WATCH_STATE_DIR": str(self.state),
            "STALL_WATCH_CONFIG": str(self.root / "stall_watch.conf"),
            "STALL_WATCH_SESSIONS": str(self.sessions_script),
            "STALL_WATCH_PROJECTS_DIR": str(self.projects),
            "STALL_WATCH_PS": "ps",
            "STALL_WATCH_SEND": str(self.bin / "send.py"),
            "STALL_TEST_PS_FILE": str(self.ps_file),
            "STALL_TEST_SEND_LOG": str(self.send_log),
            "XDG_STATE_HOME": str(self.root / "state"),
            "RCA_SEND": str(self.bin / "rca-send.py"),
            "STALL_TEST_RCA_LOG": str(self.rca_log),
            "STALL_TEST_FAIL_FILE": str(self.fail_file),
            "STALL_TEST_HANG_GUARD_SECS": str(TEST_HANG_GUARD_SECS),
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "HERDR_PLATFORM": "linux",
            # herdr reads a Mac process's marks with the real ps; the fake one answers only the watcher's list.
            "HERDR_PS": shutil.which("ps") or "ps",
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
        }
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({
            "default": {"code": 0, "stdout": '{"sessions": []}'}}))
        _ = self.record_session(os.getpid(), "54c9fdb8-6dfc-55dd-8242-a523c2e9f81f", "showrunner")
        _ = self.record_session(os.getpid() + 1_000_000, "fault-id", "natedev")
        _ = self.unit("unit-one")

    def watch_settings(self, **values: str) -> Path:
        """Write only the test overrides; production supplies omitted status vocabulary."""
        path = self.root / "stall_watch.conf"
        _ = path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        return path

    def test_prompt_alert_seconds_reads_changed_config(self) -> None:
        path = self.root / "production.conf"
        self.environment["CONF_DIR"] = str(self.root)
        self.panes["unit-one"].update(pane=EDIT_PROMPT, agent_status="blocked")
        _ = path.write_text("# range: 0..\nPROMPT_ALERT_SECONDS=120\n")
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 60).returncode, 0)
        self.assertEqual(self.sent(), [])
        _ = path.write_text("# range: 0..\nPROMPT_ALERT_SECONDS=30\n")
        self.assertEqual(self.tick(START + 61).returncode, 0)
        self.assertTrue(any(item.get("to") == "user" for item in self.sent()))

    def test_migration_retries_interrupted_alert_and_preserves_cancellation(self) -> None:
        row = unit_membership("showrunner", "unit-one")
        self.state.mkdir()
        digest = hashlib.sha256(b"showrunner\0unit-one").hexdigest()
        stretch = self.state / f"{digest}.json"
        _ = stretch.write_text('{"since": 17}\n')
        source = self.state / f"{digest}.prompt.json"
        old_key = "stall-watch:prompt:showrunner:unit-one"
        prompt = stall_watch.PromptWait(session_id=unit_session("unit-one"), created=START,
            key=old_key, since=START - 90, alerted=True, tool="tool", retiring=False)
        stall_watch.save_prompt(source, prompt)
        with (mock.patch.multiple(stall_watch, STATE_DIR=self.state),
              mock.patch.object(subprocess, "run") as send):
            send.side_effect = [subprocess.CompletedProcess([], 0), OSError("interrupted")]
            with self.assertRaisesRegex(OSError, "interrupted"):
                _ = stall_watch.migrate_staff_ids()
            target = self.state / f"{row.id}.prompt.json"
            self.assertFalse(source.exists())
            self.assertEqual(stall_watch.saved_prompt(target), prompt)
            send.reset_mock(side_effect=True)
            self.assertEqual(stall_watch.migrate_staff_ids(), 0)
            self.assertEqual(send.call_count, 2)
            commands = [cast(list[str], call.args[0]) for call in send.call_args_list]
            self.assertIn(old_key, commands[0])
            self.assertIn(PROMPT_KEY, commands[1])
            expected = {**prompt, "key": PROMPT_KEY}
            self.assertEqual(stall_watch.saved_prompt(target), expected)
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.state.iterdir()}
            send.reset_mock()
            self.assertEqual(stall_watch.migrate_staff_ids(), 0)
            send.assert_not_called()
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                              for path in self.state.iterdir()}, before)
            self.assertEqual((self.state / f"{row.id}.json").read_text(), '{"since": 17}\n')
            watch = stall_watch.watched_staff_unit("showrunner", "unit-one", row)
            self.assertIsNotNone(stall_watch.end_prompt(watch))
            retire = stall_watch.end_prompt(watch)
            assert retire is not None
            self.assertIn(PROMPT_KEY, retire.command)

    def test_migration_lists_an_unmatched_file(self) -> None:
        self.state.mkdir()
        orphan = self.state / "unknown.json"
        _ = orphan.write_text("{}\n")
        errors = io.StringIO()
        with mock.patch.object(stall_watch, "STATE_DIR", self.state), redirect_stderr(errors):
            self.assertEqual(stall_watch.migrate_staff_ids(), 1)
        self.assertIn(str(orphan), errors.getvalue())
        self.assertEqual(orphan.read_text(), "{}\n")

    def test_store_keys_survive_a_rename_and_transfer(self) -> None:
        row = unit_membership("showrunner", "unit-one")
        parent = showrunner_relationship("receiver")
        with mock.patch.object(stall_watch, "STATE_DIR", self.state):
            watch = stall_watch.watched_staff_unit("showrunner", "unit-one", row)
            before = (watch.stretch_path, watch.prompt_path, PROMPT_KEY)
            _ = staff.rename(row.id, "renamed")
            moved = staff.move(row.id, staff.ReportsTo(parent.id))
            watch = stall_watch.watched_staff_unit("receiver", "renamed", moved)
            self.assertEqual((watch.stretch_path, watch.prompt_path, watch.prompt_key), before)

    def configure(self, runners: dict[str, list[str]], *, stall_minutes: int = 5) -> None:
        """Give each showrunner an update timer, a production doc and staff rows for its units. Use the staff name."""
        _ = self.config.write_text((f"""# range: 0..
threshold_percent=1
# range: 0..
repeat_minutes=30
# range: 0..
stall_minutes={stall_minutes}
faults_to=natedev
always=natedev
"""))
        for session, units in runners.items():
            parent = showrunner_relationship(session)
            live_units = [unit for unit in units if unit not in self.retired]
            rows = [fixture_member("unit", name=unit, parent=parent.id,
                                   runtime_id=unit_session(unit), worktree=staff.PresentWorktree(
                                       local_machine(), self.root / "worktrees" / unit)) for unit in live_units]
            intended = {row.id for row in rows}
            with mock.patch.object(stall_watch, "STATE_DIR", self.state):
                self.watches.update((unit, stall_watch.watched_staff_unit(session, unit, row))
                                    for unit, row in zip(live_units, rows))
            for row in staff.snapshot().find(kind="unit", parent=parent.id):
                if row.id not in intended:
                    _ = staff.set_worktree(row.id, staff.RemovedWorktree(
                        local_machine(), Path(f"/tmp/no-worktree-of-{row.name}")), expected=row.worktree)
            # A showrunner is recorded by its update timer alone, addressed to its Claude session id.
            self.production(session, tuple(units), doc=self.root / f"{session}-production.md")
            _ = (self.root / f"{session}-production.md").write_text("- **User zone:** America/Los_Angeles\n")

    def configure_registered(self, runners: dict[str, list[tuple[str, str]]],
                             *, stall_minutes: int = 5) -> None:
        """As `configure`, and each unit's run records say the state given."""
        for units in runners.values():
            self.states.update(dict(units))
        self.configure({session: [unit for unit, _ in units] for session, units in runners.items()},
                       stall_minutes=stall_minutes)

    def close_processes(self) -> None:
        for child in self.children:
            child.terminate()
            try:
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
            except subprocess.TimeoutExpired:
                _ = child.kill()
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def record_session(self, pid: int, session_id: str, name: str, *, running: bool = True) -> Path:
        path = self.root / f"{session_id}.sock"
        record = {"pid": pid, "sessionId": session_id, "name": name,
                  "messagingSocketPath": str(path), "updatedAt": START, "running": running}
        _ = (self.sessions / f"{pid}.json").write_text(json.dumps(record))
        return path

    def production(self, production_name: str, units: tuple[str, ...], *, doc: Path) -> None:
        owner = showrunner_relationship(production_name)
        owner = staff.set_doc(owner.id, staff.LiveDoc(local_machine(), doc))
        directory = self.notifier / f"showrunner-{owner.id}"
        directory.mkdir(exist_ok=True)
        prompt = directory / "prompt"
        _ = prompt.write_text("Run `zsh ~/.claude/scripts/production/unit_status.sh /tmp/status "
                              + f"America/Los_Angeles {' '.join(units)} | cut -c1-400`.\n")
        check = f"CHECK=zsh /scripts/production/production_check.sh {owner.id}\n"
        _ = (directory / "conf").write_text(f"TARGET=session:{owner.id}\nPROMPT_FILE={prompt}\n{check}")

    def unit(self, name: str) -> tuple[int, Path]:
        child = subprocess.Popen(["sleep", str(TEST_HANG_GUARD_SECS)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        pane_pid = 10_000 + len(self.panes)
        self.process_rows += [f"{pane_pid} 1 herdr pane", f"{child.pid} {pane_pid} claude --remote-control other-name"]
        pane_id = len(self.panes) + 1
        self.panes[name] = {"pane": PANE, "id": pane_id, "pid": child.pid, "name": "showrunner", "label": f"label-of-{name}"}
        socket_path = self.record_session(child.pid, unit_session(name), "different-remote-name")
        # The lookup reads the record itself: it needs the pane the session runs in, and a real socket.
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(socket_path))
        self.addCleanup(held.close)
        record_path = self.sessions / f"{child.pid}.json"
        record = cast(dict[str, object], json.loads(record_path.read_text()))
        _ = record_path.write_text(json.dumps({**record, "tmux": ""}))
        transcript = self.projects / "project" / f"{unit_session(name)}.jsonl"
        transcript.parent.mkdir(exist_ok=True)
        _ = transcript.write_text("{}\n")
        os.utime(transcript, (START - 3600, START - 3600))
        return child.pid, socket_path

    def tick(self, at: float, *, together: int = 0, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        _ = self.ps_file.write_text("\n".join(self.process_rows) + "\n")
        units = [
            HostedUnit(str(data.get("session", "agents")), f"w{data['id']}:p1",
                       {"CODEX_THREAD_ID": unit_session(name)}, str(data["pane"]),
                       str(data.get("agent_status", "idle")), label=str(data["label"]))
            for name, data in self.panes.items()]
        fake_unit_host.write(self.environment, units)
        # Supply the workspace field required by the shared pane snapshot parser.
        responses_path = Path(self.environment["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, dict[str, object]], json.loads(responses_path.read_text()))
        for session in {unit.session for unit in units}:
            response = responses[f"--session {session} pane list"]
            payload = cast(dict[str, dict[str, list[dict[str, object]]]], json.loads(cast(str, response["stdout"])))
            for pane in payload["result"]["panes"]:
                pane["workspace_id"] = "w1"
            response["stdout"] = json.dumps(payload)
        _ = responses_path.write_text(json.dumps(responses))
        for unit, data in zip(units, self.panes.values()):
            fake_unit_host.place_process(self.environment, int(data["pid"]), unit.pane, exec_shell=True)
        # Staff sessions identify the directors in these temporary run records.
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        for record in runs.iterdir():
            record.unlink()
        for name in self.panes:
            state = self.states.get(name, "") or "running"
            if state == "standing-by":
                continue
            events = [{"event_type": "run_started", "run_started_at": 1.0,
                       "working_dir": str(Path(f"/tmp/no-worktree-of-{name}").resolve()),
                       "main_agent": {"session_id": unit_session(name)}},
                      *([{"event_type": "run_finished"}] if state == "run-finished" else [])]
            _ = (runs / f"{name}.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
        for run_id, events in self.run_records.items():
            _ = (runs / f"{run_id}.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
        for history in runs.iterdir():
            folder = self.root / "held" / history.stem
            if history.stem in self.unheld_runs:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            events = self.run_records.get(history.stem, [])
            first = events[0] if events else {}
            main_agent = first.get("main_agent", {})
            session = self.director_overrides.get(history.stem, unit_session(history.stem))
            if history.stem not in self.director_overrides and isinstance(main_agent, dict):
                candidate = cast(dict[str, object], main_agent).get("session_id")
                if isinstance(candidate, str):
                    session = candidate
            membership = staff.local_snapshot()
            owner = membership.by_current_session(session)
            run_state: dict[str, object] = {"history_file": str(history)}
            if owner is not None and isinstance(owner.parent, staff.ReportsTo):
                parent = membership.by_id(owner.parent.id)
                assert parent is not None
                run_state[unit_runs.OWNER_FIELD] = owner.id
            _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps(run_state))
            if owner is not None:
                history_events = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                if any(event.get("event_type") == "run_finished" for event in history_events):
                    if isinstance(owner.run, staff.OpenRun):
                        _ = staff.clear_run(owner.id, owner.run.path)
                else:
                    _ = staff.set_run(owner.id, staff.OpenRun(folder))

        environment = {**self.environment, "STALL_WATCH_NOW_EPOCH": str(at),
                       "STALL_TEST_SEND_TOGETHER": str(together)}
        if subprocess_cli:
            wrapper = ("import runpy,sys; from pathlib import Path; from scripts.lib import unit_runs; "
                       + "unit_runs.RUN_ROOT=Path(sys.argv[1]); unit_runs.FORMER_RUN_ROOT=Path(sys.argv[2]); "
                       + "runpy.run_path(sys.argv[3],run_name='__main__')")
            return subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", wrapper, str(self.root / "held"),
                                   str(self.root / "former-held"), str(SCRIPT)], env=environment,
                                  capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        # Keep herdr.run_program, its sanitized environment, and every parser.
        # Only the recording fake's interpreter startup is removed.
        real_run = subprocess.run
        session_module = ModuleType("stall_watch_fixture_sessions")
        session_module.__file__ = str(self.sessions_script)

        def sessions_main() -> int:
            _ = runpy.run_path(str(self.sessions_script), run_name="__main__")
            return 0

        def run(command: list[str], *, capture_output: bool, text: bool, check: bool,
                timeout: float | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if command[0] == self.environment["HERDR_BIN"]:
                self.assertTrue(capture_output and text)
                self.assertFalse(check)
                self.assertIsNotNone(env, "herdr must pass its sanitized environment")
                assert env is not None
                return invoke_cli(fake_herdr, lambda: fake_herdr.main(command[1:]), command[1:], env, Path.cwd())
            if command[:2] == [sys.executable, str(self.sessions_script)] and current_thread() is main_thread():
                return invoke_cli(session_module, sessions_main, command[2:],
                                  dict(os.environ) if env is None else env, Path.cwd())
            return real_run(command, capture_output=capture_output, text=text, check=check, timeout=timeout, env=env)

        with (mock.patch.object(subprocess, "run", run),
              mock.patch.object(account, "process_config_dir",
                                return_value=account.ConfiguredClaudeFolder(self.root / ".claude")),
              mock.patch.object(usage_store, "store_file", return_value=self.root / "usage.json"),
              mock.patch.multiple(stall_watch, STATE_DIR=self.state,
                                  PROJECTS_DIR=self.projects, SESSIONS=self.sessions_script,
                                  SEND=self.bin / "send.py", PS="ps"),
              mock.patch.multiple(rca, STATE=self.root / "state/message/rca",
                                  SEND=self.bin / "rca-send.py", PROJECTS=self.root / ".claude/projects")):
            return invoke_cli(stall_watch, stall_watch.main, [], environment, self.root, production_state=True)

    def stop_unit(self) -> Path:
        """Stop the fixture's unit through the real cancellation boundary; return its isolated launch file."""
        from scripts.agents import stop_resume
        from scripts.agents import pane_record
        with (mock.patch.object(pane_record, "RECORD_DIR", self.root / "launches"),
              mock.patch("scripts.production.stall_watch.STATE_DIR", self.state),
              mock.patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning("204f532d-a97a-59e7-a6a7-caf86af3f5ee"))):
            launch = pane_record.launch("w1:p1", "204f532d-a97a-59e7-a6a7-caf86af3f5ee", str(self.root),
                {('' + 'SHOWRUNNER_' + 'UNIT'): "showrunner", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "unit-one"}, ["claude", "-n", "unit-one"])
            pane_record.write("agents", launch)
            self.assertIsInstance(stop_resume.stop("204f532d-a97a-59e7-a6a7-caf86af3f5ee"), stop_resume.NotRunning)
            return pane_record.record_path("agents")

    def owner_messages(self) -> list[dict[str, str]]:
        """RCA owner deliveries have completed when tick returns."""
        return ([cast(dict[str, str], json.loads(line)) for line in self.rca_log.read_text().splitlines()]
                if self.rca_log.exists() else [])

    def prompt_notices(self) -> list[dict[str, str]]:
        return [item for item in self.sent() if ":owner:" in item.get("key", "")]

    def prompt_followups(self) -> list[dict[str, str]]:
        """Alert, retire and idle effects after the separately checked owner notice."""
        return [item for item in self.sent() if ":owner:" not in item.get("key", "")]

    def last_alert_values(self) -> dict[str, str]:
        """Owner and alert subprocesses may finish in either order."""
        return cast(dict[str, str], cast(object, next(item["values"] for item in reversed(self.sent())
                                                     if "values" in item)))

    def tell_prompt_owner(self, at: float) -> None:
        before = len(self.prompt_notices())
        self.assertEqual(self.tick(at).returncode, 0)
        notices = self.prompt_notices()
        self.assertEqual(len(notices), before + 1)
        self.assertEqual(notices[-1]["to"], f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}")
        self.assertIn("unit-one on ", notices[-1]["text"])
        self.assertIn("waits at a permission prompt:", notices[-1]["text"])

    def sent(self) -> list[dict[str, str]]:
        if not self.send_log.exists():
            return []
        return [json.loads(line) for line in self.send_log.read_text().splitlines()]

    def moved_director_run(self, *, finished: bool = False) -> Path:
        """Keep stale staff and document paths while the held run uses another worktree."""
        self.states["unit-one"] = "run-finished"
        worktree = self.root / "current-worktree"
        row = next(row for row in staff.find(kind="unit") if row.current_session == unit_session("unit-one"))
        self.run_records["active-run"] = [
            {"event_type": "run_started", "run_started_at": 2.0, "working_dir": str(worktree),
             "main_agent": {"session_id": row.current_session}},
            *([{"event_type": "run_finished"}] if finished else []),
        ]
        return worktree

    def test_staff_director_in_another_worktree_is_bumped_once_per_stretch(self) -> None:
        current = self.moved_director_run()
        doc = self.root / "showrunner-production.md"
        self.assertNotIn(str(current), doc.read_text())
        row = next(row for row in staff.find(kind="unit") if row.current_session == unit_session("unit-one"))
        self.assertNotEqual(row.worktree, staff.PresentWorktree(local_machine(), current))
        self.assertEqual(self.tick(START).returncode, 0)
        result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
        self.assertTrue(any("idle since" in item["text"] and "nothing running; bumped" in item["text"]
                            for item in self.sent()))
        self.assertIn("inspected showrunner/unit-one: idle bump requested", result.stdout)
        saved = {path: path.read_bytes() for path in self.state.glob("*.json")}
        again = self.tick(START + 660)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual({path: path.read_bytes() for path in self.state.glob("*.json")}, saved)

    def test_running_work_of_the_staff_director_resets_the_stretch(self) -> None:
        _ = self.moved_director_run()
        pid = int(self.panes["unit-one"]["pid"])
        self.assertEqual(self.tick(START).returncode, 0)
        self.process_rows.append(f"900001 {pid} bash compile-in-current-worktree")
        result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sent(), [])
        self.assertIn("inspected showrunner/unit-one: running", result.stdout)
        saved = cast(dict[str, object], json.loads(
            self.watches["unit-one"].stretch_path.read_text()))
        self.assertEqual(saved["since"], START + 600)
        _ = self.process_rows.pop()
        self.assertEqual(self.tick(START + 601).returncode, 0)
        self.assertEqual(self.sent(), [])

    def test_finished_staff_director_run_is_not_eligible(self) -> None:
        _ = self.moved_director_run(finished=True)
        self.assertEqual(self.tick(START).returncode, 0)
        result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("inspected showrunner/unit-one:", result.stdout)
        self.assertEqual(self.sent(), [])
        self.assertFalse(self.watches["unit-one"].stretch_path.exists())

    def test_replacement_director_uses_the_recorded_director_of_the_held_run(self) -> None:
        _ = self.moved_director_run()
        old_session = unit_session("unit-old")
        self.run_records["active-run"][0]["main_agent"] = {"session_id": old_session}
        self.director_overrides["active-run"] = unit_session("unit-one")
        _ = self.unit("unit-old")
        self.states["unit-old"] = "standing-by"
        self.configure({"showrunner": ["unit-one", "unit-old"]})
        self.assertEqual(self.tick(START).returncode, 0)
        result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("inspected showrunner/unit-one: idle bump requested", result.stdout)
        self.assertNotIn("inspected showrunner/unit-old:", result.stdout)
        self.assertEqual(len(self.sent()), 2)

    def test_staff_director_without_a_held_run_is_not_eligible(self) -> None:
        self.unheld_runs.add("unit-one")
        self.assertEqual(self.tick(START).returncode, 0)
        result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("inspected showrunner/unit-one:", result.stdout)
        self.assertEqual(self.sent(), [])

    def test_damaged_held_history_preserves_only_its_units_state(self) -> None:
        _ = self.unit("unit-two")
        self.configure({"showrunner": ["unit-one", "unit-two"]})
        self.assertEqual(self.tick(START).returncode, 0)
        path = self.watches["unit-one"].stretch_path
        saved = path.read_bytes()
        lookup = unit_lookup.HeldRunLookup.unfinished_held_run
        damaged = self.root / "history/runs/unit-one.jsonl"

        def damage(runs: unit_lookup.HeldRunLookup, session: str) -> unit_lookup.HeldRun | unit_lookup.HeldRunUnknown | unit_lookup.NoUnfinishedRun:
            if session == unit_session("unit-one"):
                _ = damaged.write_bytes(b"\xff\n")
            return lookup(runs, session)

        with mock.patch.object(unit_lookup.HeldRunLookup, "unfinished_held_run", damage):
            result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), saved)
        lines = [line for line in result.stdout.splitlines() if "unit-one" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn(str(damaged), lines[0])
        self.assertIn("run unknown", lines[0])
        self.assertIn("inspected showrunner/unit-two: idle bump requested", result.stdout)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual({item["key"] for item in self.sent()},
                         {f"stall-watch:{unit_session('unit-two')}:{START}:bump",
                          f"stall-watch:{unit_session('unit-two')}:{START}:tell"})

    def test_one_director_read_per_folder_serves_multiple_units(self) -> None:
        _ = self.unit("unit-two")
        self.configure({"showrunner": ["unit-one", "unit-two"]})
        with mock.patch.object(unit_runs, "director", wraps=unit_runs.director) as directors:
            result = self.tick(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["inspected showrunner/unit-one: fresh",
                                                     "inspected showrunner/unit-two: fresh"])
        self.assertEqual(directors.call_count, 2)
        self.assertEqual({call.args[0] for call in directors.call_args_list},
                         {self.root / "held/unit-one", self.root / "held/unit-two"})

    def test_bad_unheld_history_header_and_tail_do_not_affect_units(self) -> None:
        scan = unit_runs.held_by_session

        def damage() -> dict[str, list[str]]:
            folders = scan()
            _ = (self.root / "history/runs/unheld.jsonl").write_bytes(b"not JSON\n\xff\n")
            return folders

        with (mock.patch.object(unit_runs, "held_by_session", side_effect=damage),
              mock.patch.object(unit_lookup, "read_run_states", side_effect=AssertionError("eager history read"))):
            result = self.tick(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["inspected showrunner/unit-one: fresh"])
        self.assertEqual(self.sent(), [])

    def test_inspection_deadline_prevents_later_history_reads_and_state_changes(self) -> None:
        _ = self.unit("unit-two")
        self.configure({"showrunner": ["unit-one", "unit-two"]})
        self.assertEqual(self.tick(START).returncode, 0)
        saved = {path: path.read_bytes() for path in self.state.glob("*.json")}
        (self.state / "inspection.cursor").unlink(missing_ok=True)
        clock = [0.0]
        original = unit_lookup.HeldRunLookup.unfinished_held_run
        sessions: list[str] = []

        def lookup(runs: unit_lookup.HeldRunLookup, session: str) -> unit_lookup.HeldRun | unit_lookup.HeldRunUnknown | unit_lookup.NoUnfinishedRun:
            sessions.append(session)
            found = original(runs, session)
            clock[0] = 100.0
            return found

        with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(showrunners, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(unit_lookup.HeldRunLookup, "unfinished_held_run", lookup),
              mock.patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as scan,
              mock.patch.object(stall_watch, "read_place") as panes):
            result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        scan.assert_not_called()
        self.assertEqual(sessions, [unit_session("unit-one")])
        panes.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)
        self.assertEqual(self.sent(), [])

    def test_one_staff_snapshot_supplies_discovery_and_unit_membership(self) -> None:
        with (mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshots,
              mock.patch.object(showrunners, "registered_showrunners", wraps=showrunners.registered_showrunners) as discover):
            result = self.tick(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshots.assert_called_once_with()
        self.assertIsInstance(cast(object, discover.call_args.kwargs["membership"]), staff.StaffSnapshot)

    def test_staff_unit_without_a_document_row_is_still_inspected(self) -> None:
        doc = self.root / "showrunner-production.md"
        _ = doc.write_text("\n".join(line for line in doc.read_text().splitlines() if "| unit-one |" not in line))
        self.assertEqual(self.tick(START).returncode, 0)
        result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("inspected showrunner/unit-one: idle bump requested", result.stdout)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})

    def test_inspection_log_names_each_unit_without_sending(self) -> None:
        _ = self.unit("unit-two")
        self.configure({"showrunner": ["unit-one", "unit-two"]})
        with (mock.patch.object(stall_watch, "send_all") as sends,
              mock.patch.object(stall_watch, "delivery") as delivery):
            result = self.tick(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["inspected showrunner/unit-one: fresh",
                                                     "inspected showrunner/unit-two: fresh"])
        delivery.assert_not_called()
        sends.assert_called_once_with([], START, deadline=mock.ANY,
            limits=stall_watch.sender_limits(stall_watch.watch_config(packaged_only=True)))
        self.assertEqual(self.sent(), [])

    def test_one_tick_reads_run_records_once_across_productions_and_units(self) -> None:
        from scripts.production import stall_watch
        from scripts.production import showrunners
        from scripts.production import unit_lookup as lookup
        runners: list[showrunners.Showrunner] = []
        marked: dict[str, dict[str, lookup.LabeledSession]] = {}
        for production_name in ("first", "second"):
            doc = self.root / f"{production_name}-production.md"
            _ = doc.write_text(f"# Production\n- **Production name:** {production_name}\n")
            runners.append(showrunners.Showrunner(session=production_name, socket="/fixture.sock",
                            name=production_name, zone="UTC", doc=str(doc)))
            marked[production_name] = {}
            for name in ("alpha", "beta"):
                row = unit_membership(production_name, name)
                marked[production_name][name] = lookup.LabeledSession(name, lookup.ClaudeNotRunning(), lookup.Place("fixture", name),
                                                     identity=lookup.LiveSession(row.current_session))

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(stall_watch, "processes", return_value=[]),
              mock.patch.object(stall_watch, "STATE_DIR", self.state),
              mock.patch.object(showrunners, "load_settings", return_value={}),
              mock.patch.object(showrunners, "registered_showrunners", return_value=showrunners.ShowrunnerDiscovery(runners)),
              mock.patch.object(lookup, "every_labeled_session", return_value=lookup.LabeledProductions(marked)) as scans,
              mock.patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as reads,
              mock.patch.object(stall_watch, "forget", return_value=None) as forget,
              mock.patch.object(stall_watch, "send_all") as send):
            _ = stall_watch.tick(START)
            scans.assert_called_once_with(record=mock.ANY)
            reads.assert_not_called()
            self.assertEqual(forget.call_count, 4)
            send.assert_called_once_with(
                [], START, deadline=mock.ANY,
                limits=stall_watch.sender_limits(stall_watch.watch_config(packaged_only=True)))

    def test_a_failed_session_scan_ends_the_tick_and_the_next_tick_scans_again(self) -> None:
        with (mock.patch.object(unit_lookup, "every_labeled_session", side_effect=OSError("fixture scan failed")) as scans,
              mock.patch.object(showrunners, "registered_showrunners") as discovery):
            first = self.tick(START)
            second = self.tick(START + 60)
        self.assertEqual(scans.call_count, 2)
        discovery.assert_not_called()
        for result in (first, second):
            self.assertIn("fixture scan failed", result.stderr)
            self.assertNotIn("inspected ", result.stdout)
        self.assertEqual(self.sent(), [])

    def test_a_stalled_discovery_keeps_prompt_work_and_returns_by_the_pass_deadline(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        saved = self.prompt_state().read_text()
        reader = stalled_session_reader(self.root)
        errors = io.StringIO()
        began = time.monotonic()
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(stall_watch, "STATE_DIR", self.state),
              mock.patch.object(stall_watch, "processes", return_value=[]),
              mock.patch.multiple(showrunners, SESSIONS=reader, CONFIG=self.config,
                                  NOTIFIER_STATE_DIR=self.notifier),
              mock.patch.object(unit_lookup, "read_run_states", return_value=unit_lookup.RunStates({})),
              redirect_stderr(errors)):
            _ = stall_watch.tick(START + 60, deadline=began + 0.15)
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertEqual(errors.getvalue().count("showrunners: skipping showrunner-showrunner:"), 1)
        self.assertEqual(self.prompt_state().read_text(), saved)
        self.assertEqual(self.sent(), [])

    def test_failed_production_discovery_still_alerts_the_healthy_production(self) -> None:
        self.assertEqual(self.tick(START).returncode, 0)
        self.configure({"aaa-stalled": [], "showrunner": ["unit-one"]})
        stalled = showrunner_relationship("aaa-stalled")
        original = showrunners.socket_for

        def lookup(target: str, *, deadline: float) -> str | None:
            if target == f"session:{stalled.current_session}":
                raise TimeoutError("fixture lookup timed out; retained")
            return original(target, deadline=deadline)

        with mock.patch.object(showrunners, "socket_for", side_effect=lookup):
            result = self.tick(START + 300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr.count("fixture lookup timed out; retained"), 1)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual({item["to"] for item in self.sent()},
                         {f"uds:{self.root / '204f532d-a97a-59e7-a6a7-caf86af3f5ee.sock'}", f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}"})

    def test_slow_discovery_and_inspection_keep_skipped_state_for_the_next_pass(self) -> None:
        _ = self.unit("unit-two")
        self.configure({"showrunner": ["unit-one", "unit-two"]})
        for pane in self.panes.values():
            pane.update(pane=EDIT_PROMPT, agent_status="blocked")
        self.assertEqual(self.tick(START).returncode, 0)
        saved = {path: path.read_bytes() for path in self.state.glob("*.json")}
        original = showrunners.registered_showrunners
        for stage in ("text", "status"):
            (self.state / "inspection.cursor").unlink(missing_ok=True)
            clock = [0.0]

            def discover(*, deadline: float, membership: staff.StaffSnapshot) -> showrunners.ShowrunnerDiscovery:
                found = original(deadline=deadline, membership=membership)
                clock[0] = 98.0
                return found

            def inspect(place: unit_lookup.Place, *arguments: object, **keywords: object) -> str:
                self.assertEqual(place.pane, "w1:p1")
                self.assertTrue(arguments)
                self.assertEqual(keywords["deadline"], 100.0)
                if stage == "text":
                    clock[0] = 100.0
                    raise HerdrFailure("fixture pane deadline exhausted")
                return EDIT_PROMPT

            def status(place: unit_lookup.Place, *, deadline: float) -> str:
                self.assertEqual(place.pane, "w1:p1")
                self.assertEqual(deadline - clock[0], 2.0)
                clock[0] = deadline
                raise HerdrFailure("fixture status deadline exhausted")

            with (self.subTest(stage=stage),
                  mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
                  mock.patch.object(showrunners, "monotonic", side_effect=lambda: clock[0]),
                  mock.patch.object(showrunners, "registered_showrunners", side_effect=discover),
                  mock.patch.object(stall_watch, "read_place", side_effect=inspect) as reads,
                  mock.patch.object(stall_watch, "agent_status", side_effect=status) as statuses):
                result = self.tick(START + 60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertLessEqual(clock[0], 110.0)
                self.assertEqual(reads.call_count, 1)
                self.assertEqual(statuses.call_count, int(stage == "status"))
                self.assertEqual(result.stderr.count("stall-watch: unit-one:"), 1)
                self.assertEqual(result.stderr.count("stall-watch: unit-two:"), 1)
                self.assertEqual({path: path.read_bytes() for path in self.state.glob("*.json")}, saved)
                self.assertEqual(self.sent(), [])
        with (mock.patch.object(stall_watch, "read_place", wraps=read_place) as reads,
              mock.patch.object(stall_watch, "agent_status", wraps=agent_status) as statuses):
            result = self.tick(START + 61)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(statuses.call_count, 2)
        self.assertEqual(len(self.prompt_notices()), 2)
        self.assertEqual(len(self.prompt_followups()), 2)

    def test_exhausted_first_attempt_rotates_without_changing_alert_state(self) -> None:
        _ = self.unit("unit-two")
        self.configure({"showrunner": ["unit-one", "unit-two"]})
        self.assertEqual(self.tick(START).returncode, 0)
        saved = {path: path.read_bytes() for path in self.state.glob("*.json")}
        original = showrunners.registered_showrunners
        for stage in ("text", "status", "text-failure", "status-failure"):
            with self.subTest(stage=stage):
                for path, content in saved.items():
                    _ = path.write_bytes(content)
                _ = self.send_log.write_text("")
                (self.state / "inspection.cursor").unlink(missing_ok=True)
                clock = [0.0]
                reads: list[str] = []

                def discover(*, deadline: float, membership: staff.StaffSnapshot) -> showrunners.ShowrunnerDiscovery:
                    found = original(deadline=deadline, membership=membership)
                    clock[0] = 98.0
                    return found

                def inspect(place: unit_lookup.Place, *arguments: object, **keywords: object) -> str:
                    del arguments
                    reads.append(place.pane)
                    if place.pane == "w1:p1" and stage.startswith("text"):
                        clock[0] = cast(float, keywords["deadline"])
                        if stage.endswith("failure"):
                            raise HerdrFailure("fixture text deadline exhausted")
                    return PANE

                def status(place: unit_lookup.Place, *, deadline: float) -> str:
                    if place.pane == "w1:p1" and stage.startswith("status"):
                        clock[0] = deadline
                        if stage.endswith("failure"):
                            raise HerdrFailure("fixture status deadline exhausted")
                    return "idle"

                for pass_number in range(2):
                    clock[0] = 0.0
                    reads.clear()
                    with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
                          mock.patch.object(showrunners, "monotonic", side_effect=lambda: clock[0]),
                          mock.patch.object(showrunners, "registered_showrunners", side_effect=discover),
                          mock.patch.object(stall_watch, "read_place", side_effect=inspect),
                          mock.patch.object(stall_watch, "agent_status", side_effect=status)):
                        result = self.tick(START + 300 + pass_number)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertLessEqual(clock[0], 100.0)
                    slow = self.state / (unit_session("unit-one") + ".json")
                    self.assertEqual(slow.read_bytes(), saved[slow])
                    if pass_number == 0:
                        self.assertEqual(reads, ["w1:p1"])
                        self.assertEqual({path: path.read_bytes() for path in saved}, saved)
                        self.assertEqual(self.sent(), [])
                    else:
                        self.assertEqual(reads[0], "w2:p1")
                        self.assertEqual(len(self.sent()), 2, "a found alert must use this pass's reserve")
                        self.assertIn(f"uds:{self.root / '3764fee6-1374-598d-bb7d-1511c0d8b8e9.sock'}", {item["to"] for item in self.sent()})

    def test_persistently_slow_leading_panes_cannot_starve_later_alerts(self) -> None:
        names = ["unit-one", *(f"unit-{number}" for number in range(2, 14))]
        for name in names[1:]:
            _ = self.unit(name)
        self.configure({"showrunner": names})
        self.assertEqual(self.tick(START).returncode, 0)
        clock = [0.0]
        inspected: list[str] = []
        ordered_panes = [f"w{names.index(name) + 1}:p1" for name in sorted(names)]
        previous_first = ordered_panes[0]

        def slow(place: unit_lookup.Place, *arguments: object, **keywords: object) -> str:
            del arguments, keywords
            inspected.append(place.pane)
            clock[0] += 10.0
            return PANE

        for pass_number in range(len(names)):
            clock[0] = 0.0
            inspected.clear()
            with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
                  mock.patch.object(showrunners, "monotonic", side_effect=lambda: clock[0]),
                  mock.patch.object(stall_watch, "read_place", side_effect=slow),
                  mock.patch.object(stall_watch, "agent_status", return_value="idle")):
                result = self.tick(START + 300 + pass_number)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(clock[0], 100.0)
            if pass_number == 0:
                self.assertEqual(len(self.sent()), 18, "alerts found in this pass must use its reserve")
            expected = ordered_panes[(ordered_panes.index(previous_first) + 1) % len(names)]
            self.assertEqual(inspected[0], expected, "the last pass's first attempt yields to its successor")
            previous_first = inspected[0]
        self.assertEqual(len(self.sent()), len(names) * 2)
        self.assertEqual({item["to"] for item in self.sent() if item["to"] != f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}"},
                         {f"uds:{self.root / (unit_session(name) + '.sock')}" for name in names})

    def test_failed_leading_reads_do_not_starve_a_healthy_pane(self) -> None:
        names = ["unit-one", *(f"unit-{number}" for number in range(2, 14))]
        for name in names[1:]:
            _ = self.unit(name)
        self.configure({"showrunner": names})
        self.assertEqual(self.tick(START).returncode, 0)
        clock = [0.0]

        def inspect(place: unit_lookup.Place, *arguments: object, **keywords: object) -> str:
            del arguments, keywords
            clock[0] += 10.0
            if place.pane != "w13:p1":
                raise HerdrFailure("fixture reader unavailable")
            return PANE

        for pass_number in range(len(names)):
            clock[0] = 0.0
            with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
                  mock.patch.object(showrunners, "monotonic", side_effect=lambda: clock[0]),
                  mock.patch.object(stall_watch, "read_place", side_effect=inspect),
                  mock.patch.object(stall_watch, "agent_status", return_value="idle")):
                self.assertEqual(self.tick(START + 300 + pass_number).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.assertIn(f"uds:{self.root / 'fdf3198c-88d1-5942-8de0-ccb90fa5c7b6.sock'}", {item["to"] for item in self.sent()})

    def test_a_herdr_unit_is_watched_through_a_joined_400_line_read(self) -> None:
        self.panes["unit-one"].update(session="native", label="native")
        first = self.tick(START, subprocess_cli=True)
        second = self.tick(START + 300, subprocess_cli=True)
        self.assertEqual((first.returncode, second.returncode), (0, 0), first.stderr + second.stderr)
        self.assertEqual(len(self.sent()), 2)
        calls = (self.root / "herdr.log").read_text().splitlines()
        self.assertIn(json.dumps(["--session", "native", "pane", "read", "w1:p1", "--source",
                                  "recent-unwrapped", "--lines", "400"]), calls)

    def test_reported_unit_is_not_bumped_again_after_its_session_is_renamed(self) -> None:
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 300).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        # A rename changes the herdr label and the Claude name. The mark, and so the unit, stays.
        self.panes["unit-one"].update(session="renamed-by-the-user", label="renamed-by-the-user")
        record_path = self.sessions / f"{self.children[0].pid}.json"
        record = cast(dict[str, object], json.loads(record_path.read_text()))
        _ = record_path.write_text(json.dumps({**record, "name": "renamed-by-the-user"}))
        self.assertEqual(self.tick(START + 301).returncode, 0)
        self.assertEqual(len(self.sent()), 2)

    def test_stalled_unit_waits_at_gate_then_bumps_after_own_holding_returns(self) -> None:
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tick(START + 300).returncode, 0)
        first = self.sent()
        self.assertEqual(len(first), 2)
        self.assertEqual({item["from"] for item in first}, {"stall-watch"})
        bump = next(item for item in first if item["key"].endswith(":bump"))
        tell = next(item for item in first if item["key"].endswith(":tell"))
        self.assertEqual(bump["to"], f"uds:{self.root / '204f532d-a97a-59e7-a6a7-caf86af3f5ee.sock'}")
        self.assertEqual(tell["to"], f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}")
        self.assertIn("idle since 12:00 PDT", bump["text"])
        self.assertIn("Continue your run", bump["text"])
        self.assertIn("Last status: none on screen", tell["text"])
        self.assertEqual(self.tick(START + 360).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.panes["unit-one"]["pane"] = "reply text\n" + PANE
        self.assertEqual(self.tick(START + 361).returncode, 0)
        self.assertEqual(self.tick(START + 661).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.panes["unit-one"]["pane"] = "— gate: ready for next step\n"
        self.assertEqual(self.tick(START + 662).returncode, 0)
        self.assertEqual(self.tick(START + 962).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.panes["unit-one"]["pane"] += "— holding: ready for next task\n"
        self.assertEqual(self.tick(START + 963).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.tick(START + 1263).returncode, 0)
        second = self.sent()[2:]
        self.assertEqual(len(second), 2)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in second}, {"bump", "tell"})
        self.assertIn("nothing running; bumped", next(item["text"] for item in second if item["key"].endswith(":tell")))
        self.assertNotEqual({item["key"] for item in first}, {item["key"] for item in second})

    def test_stalled_unit_bumps_again_when_its_status_names_only_own_work(self) -> None:
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tick(START + 300).returncode, 0)
        first = self.sent()
        self.assertEqual(len(first), 2)
        self.assertEqual(self.tick(START + 360).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.panes["unit-one"]["pane"] = "reply text\n" + PANE
        self.assertEqual(self.tick(START + 361).returncode, 0)
        self.assertEqual(self.tick(START + 661).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.panes["unit-one"]["pane"] = "— holding: ready for next step\n"
        self.assertEqual(self.tick(START + 662).returncode, 0)
        self.assertEqual(self.tick(START + 962).returncode, 0)
        second = self.sent()[2:]
        self.assertEqual(len(second), 2)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in second}, {"bump", "tell"})
        self.assertIn("nothing running; bumped", next(item["text"] for item in second if item["key"].endswith(":tell")))
        self.assertNotEqual({item["key"] for item in first}, {item["key"] for item in second})

    def test_a_unit_at_a_permission_prompt_alerts_the_user_once_and_is_not_bumped(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.sent(), [])
        # A failed alert is sent again on the next tick.
        _ = self.fail_file.write_text('["user"]')
        failed = self.tick(START + 60)
        self.assertIn("alert", failed.stderr)
        _ = self.fail_file.write_text("[]")
        self.assertEqual(self.tick(START + 120).returncode, 0)
        self.assertEqual(self.tick(START + 900).returncode, 0)
        alerts = self.prompt_followups()
        self.assertEqual(len(alerts), 2, alerts)
        self.assertEqual(alerts[-1]["to"], "user")
        self.assertEqual(alerts[-1]["text-id"], "stall_watch.permission_prompt")
        self.assertEqual(alerts[-1]["need"], "blocked")
        values = self.last_alert_values()
        self.assertEqual((values["unit"], values["request"], values["since"]), ("unit-one", "edit panes.py", "12:00 PDT"))
        # The alert has the unit's key, which a retire cancels it by.
        self.assertEqual(alerts[-1]["key"], PROMPT_KEY)
        # No bump while it waits; its owner has received exactly one notice.
        self.assertEqual(len(self.prompt_notices()), 1)
        self.assertNotIn("uds:", " ".join(item["to"] for item in alerts))
        # The prompt is answered: the alert is retired, the idle clock starts again, and a new prompt alerts again.
        self.panes["unit-one"]["agent_status"] = "idle"
        self.panes["unit-one"]["pane"] = "— holding: ready for next step\n"
        self.assertEqual(self.tick(START + 960).returncode, 0)
        self.assertEqual(self.prompt_followups()[2:], [RETIRE])
        self.assertFalse(self.prompt_state().exists())
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.assertEqual(self.tick(START + 1000).returncode, 0)
        self.tell_prompt_owner(START + 1060)
        self.assertEqual(self.tick(START + 1060).returncode, 0)
        self.assertEqual([(item["to"], item["key"], item.get("command")) for item in self.prompt_followups()[3:]],
                         [("user", PROMPT_KEY, None)])
        self.assertEqual(len(self.prompt_notices()), 2)

    def test_a_prompt_tells_its_owner_once_then_alerts_only_after_the_configured_grace(self) -> None:
        config = self.root / "stall_watch.conf"
        _ = config.write_text("prompt_grace_seconds=120\n")
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 59).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.tell_prompt_owner(START + 60)
        self.assertIn("edit panes.py", self.prompt_notices()[0]["text"])
        prompt = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        told = prompt.get("told", 0)
        self.assertEqual(told, START + 60)
        self.assertFalse(prompt["alerted"])
        self.assertEqual(self.tick(told + 119).returncode, 0)
        self.assertEqual(self.prompt_followups(), [])
        self.assertEqual(self.tick(told + 120).returncode, 0)
        self.assertEqual([item["to"] for item in self.prompt_followups()], ["user"])
        self.assertEqual(self.tick(START + 900).returncode, 0)
        self.assertEqual(len(self.prompt_notices()), 1)
        self.assertEqual(len(self.prompt_followups()), 1)

    def test_unknown_agent_state_does_not_retire_a_pending_permission_prompt(self) -> None:
        self.panes["unit-one"].update(agent_status="blocked", pane=EDIT_PROMPT)
        self.assertEqual(self.tick(START).returncode, 0)
        saved = self.prompt_state().read_bytes()
        self.panes["unit-one"]["agent_status"] = "future-state"
        with mock.patch.object(stall_watch, "end_prompt", side_effect=AssertionError("unknown is not settled")):
            self.assertEqual(self.tick(START + 10).returncode, 0)
        self.assertEqual(self.prompt_state().read_bytes(), saved)
        self.assertEqual(self.prompt_followups(), [])

    def test_a_prompt_answered_during_the_owner_grace_never_pushes(self) -> None:
        _ = (self.root / "stall_watch.conf").write_text("prompt_grace_seconds=120\n")
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        self.tell_prompt_owner(START + 60)
        self.panes["unit-one"]["agent_status"] = "idle"
        self.panes["unit-one"]["pane"] = "— done: permission answered\n"
        self.assertEqual(self.tick(START + 90).returncode, 0)
        self.assertEqual(self.tick(START + 900).returncode, 0)
        self.assertEqual(len(self.prompt_notices()), 1)
        self.assertEqual(self.prompt_followups(), [])
        self.assertFalse(self.prompt_state().exists())

    def test_the_grace_is_reread_from_the_person_edited_file(self) -> None:
        config = self.root / "stall_watch.conf"
        _ = config.write_text("prompt_grace_seconds=600\n")
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        self.tell_prompt_owner(START + 60)
        told = START + 60
        self.assertEqual(self.tick(told + 120).returncode, 0)
        self.assertEqual(self.prompt_followups(), [])
        _ = config.write_text("prompt_grace_seconds=120\n")
        self.assertEqual(self.tick(told + 120).returncode, 0)
        self.assertEqual([item["to"] for item in self.prompt_followups()], ["user"])
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_a_permanently_failed_owner_notice_cannot_delay_the_user_alert(self) -> None:
        _ = (self.root / "stall_watch.conf").write_text("prompt_grace_seconds=120\n")
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        _ = self.fail_file.write_text(json.dumps([f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}"]))
        for offset in (60, 179):
            failed = self.tick(START + offset)
            self.assertIn("prompt-owner", failed.stderr)
            self.assertEqual(self.prompt_followups(), [])
        self.assertEqual(self.tick(START + 180).returncode, 0)
        self.assertEqual([item["to"] for item in self.prompt_followups()], ["user"])
        prompt = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        self.assertNotIn("told", prompt)
        _ = self.tick(START + 240)
        self.assertEqual(len(self.prompt_notices()), 4)
        self.assertEqual(len(self.prompt_followups()), 1)

    def test_invalid_or_missing_grace_alerts_a_still_blocked_prompt(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        for setting in ("prompt_grace_seconds=invalid\n", "missing"):
            with self.subTest(setting=setting):
                self.prompt_state().unlink(missing_ok=True)
                _ = self.tick(START)
                config = self.root / "stall_watch.conf"
                if setting == "missing":
                    config.unlink(missing_ok=True)
                else:
                    _ = config.write_text(setting)
                before = len(self.prompt_followups())
                result = self.tick(START + 60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("using zero prompt grace", result.stderr)
                self.assertEqual(len(self.prompt_followups()), before + 1)
                self.assertEqual(self.prompt_followups()[-1]["to"], "user")
        self.assertEqual(len(self.prompt_notices()), 2)

    def test_stop_retires_a_transferred_units_current_production_key(self) -> None:
        from scripts.agents import pane_record
        from scripts.agents import pane_record as staff_launches

        self.configure({"showrunner": ["unit-one"], "receiver": []})
        row = unit_membership("showrunner", "unit-one")
        receiver = showrunner_relationship("receiver")
        _ = staff.move(row.id, staff.ReportsTo(receiver.id))
        launches = self.root / "launches"
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(pane_record, "RECORD_DIR", launches),
              mock.patch.object(staff_launches, "RECORD_DIR", launches),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
              mock.patch.object(herdr, "shared_session", return_value="agents"),
              mock.patch.multiple(stall_watch, STATE_DIR=self.state, SEND=self.bin / "send.py")):
            launch = pane_record.launch("w1:p1", "204f532d-a97a-59e7-a6a7-caf86af3f5ee", str(self.root),
                {('' + 'SHOWRUNNER_' + 'UNIT'): "showrunner", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "unit-one", "STAFF_ID": row.id},
                ["claude", "-n", "unit-one"])
            pane_record.write("agents", launch)
            self.state.mkdir()
            path = stall_watch.watched_staff_unit("receiver", "unit-one", row).prompt_path
            stall_watch.save_prompt(path, stall_watch.PromptWait(
                session_id="204f532d-a97a-59e7-a6a7-caf86af3f5ee", created=START, key=PROMPT_KEY,
                since=START, alerted=True, tool="tool", retiring=False))
            stall_watch.retire_session_prompt("204f532d-a97a-59e7-a6a7-caf86af3f5ee")
            self.assertTrue(path.exists())
            self.assertEqual(self.sent(), [])
            pending = stall_watch.cancellation_requests()
            self.assertEqual(len(pending.deliveries), 1)
            stall_watch.send_all(pending.deliveries, START)
            stall_watch.finish_cancellations(pending.requests)
            self.assertFalse(path.exists())
        self.assertEqual(self.sent(), [{**RETIRE, "key": PROMPT_KEY}])

    def test_a_resume_during_a_running_tick_retires_the_observation_then_observes_the_new_prompt(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = ""
        self.panes["unit-one"].update(pane=EDIT_PROMPT, agent_status="blocked")
        transcript = self.projects / "project/204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        _ = transcript.write_text(tool_call("Edit", {"file_path": "note.txt"}, START - 30))
        from scripts.agents import stop_resume
        wall = [float(START)]
        # Observation starts before stop/resume, even when pane reading finishes afterwards.
        def resumed(session: str, **options: object) -> stop_resume.ClaudeUp:
            self.assertEqual(session, "204f532d-a97a-59e7-a6a7-caf86af3f5ee")
            self.assertEqual(options["why"], "account move")
            wall[0] = START + 2
            return stop_resume.ClaudeUp(session, "w1:p1", "claude2")
        def resume_during_inspection(*arguments: object, **keywords: object) -> str:
            self.assertTrue(arguments)
            self.assertTrue(keywords)
            wall[0] = START + 1
            self.assertIsInstance(stop_resume.move("204f532d-a97a-59e7-a6a7-caf86af3f5ee", "claude2", why="account move"),
                                  stop_resume.ClaudeUp)
            return EDIT_PROMPT
        with (mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
              mock.patch.object(time, "time", side_effect=lambda: wall[0]),
              mock.patch("scripts.production.stall_watch.STATE_DIR", self.state),
              mock.patch.object(stop_resume, "_record_launches"),
              mock.patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning("204f532d-a97a-59e7-a6a7-caf86af3f5ee")),
              mock.patch.object(stop_resume, "resume", side_effect=resumed),
              mock.patch.object(stall_watch, "read_place", side_effect=resume_during_inspection)):
            self.assertEqual(self.tick(START).returncode, 0)
        prompt = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        self.assertEqual((prompt["session_id"], prompt["created"], prompt["since"], prompt["key"]),
                         ("204f532d-a97a-59e7-a6a7-caf86af3f5ee", START, START - 30, PROMPT_KEY))
        self.assertEqual(self.tick(START + 60).returncode, 0)
        self.assertFalse(self.prompt_state().exists(), "the observation that straddled stop is retired")
        with mock.patch.object(time, "time", return_value=START + 61):
            self.assertEqual(self.tick(START + 61).returncode, 0)
        prompt = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        self.assertFalse(prompt["retiring"])
        self.assertEqual(prompt["created"], START + 61, "the next pass observes the resumed prompt afresh")
        self.assertEqual(list(self.state.glob("*.prompt.cancel.json")), [])
        self.assertEqual([item for item in self.sent() if item.get("command") == "retire"], [RETIRE])
        self.assertEqual(len(self.prompt_notices()), 1)
        self.assertEqual(len(self.prompt_followups()), 2)

    def test_a_stop_after_status_read_retires_the_observation_even_after_labels_and_launch_disappear(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = ""
        self.panes["unit-one"].update(pane=EDIT_PROMPT, agent_status="blocked")
        transcript = self.projects / "project/204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        _ = transcript.write_text(tool_call("Edit", {"file_path": "note.txt"}, START - 300))
        wall = [float(START)]

        def stop_after_status(*arguments: object, **keywords: object) -> str:
            self.assertTrue(arguments)
            self.assertTrue(keywords)
            wall[0] = START + 1
            self.stop_unit().unlink()
            self.panes.clear()
            return "blocked"

        with (mock.patch.object(time, "time", side_effect=lambda: wall[0]),
              mock.patch.object(stall_watch, "agent_status", side_effect=stop_after_status)):
            self.assertEqual(self.tick(START + 200).returncode, 0)
        self.assertTrue(self.prompt_state().exists())
        self.assertEqual(self.tick(START + 201).returncode, 0)
        self.assertFalse(self.prompt_state().exists())
        self.assertEqual(list(self.state.glob("*.prompt.cancel.json")), [])
        self.assertEqual([item["key"] for item in self.prompt_followups()], [PROMPT_KEY, PROMPT_KEY])
        self.assertEqual(self.prompt_followups()[-1], RETIRE)
        self.assertEqual(len(self.prompt_notices()), 1)
        before = self.sent()
        self.assertEqual(self.tick(START + 202).returncode, 0)
        self.assertEqual(self.sent(), before)

    def test_a_legacy_orphan_cannot_block_a_stopped_units_retirement_after_launch_deletion(self) -> None:
        self.panes["unit-one"].update(pane=EDIT_PROMPT, agent_status="blocked")
        self.assertEqual(self.tick(START).returncode, 0)
        orphan = self.state / "aaa-orphan.prompt.json"
        orphan_key = "stall-watch:prompt:retired:orphan"
        _ = orphan.write_text(json.dumps({"since": START, "alerted": True, "key": orphan_key}))
        launch = self.stop_unit()
        launch.unlink()
        self.panes.clear()
        result = self.tick(START + 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count(str(orphan)), 1)
        self.assertCountEqual(self.prompt_followups(), [RETIRE, {**RETIRE, "key": orphan_key}])
        self.assertFalse(orphan.exists(), "the orphan itself must be retired, not just skipped")
        self.assertFalse(self.prompt_state().exists())
        self.assertEqual(list(self.state.glob("*.prompt.cancel.json")), [])
        before = self.sent()
        self.assertEqual(self.tick(START + 2).returncode, 0)
        self.assertEqual(self.sent(), before)
        self.assertEqual(len(self.prompt_notices()), 0)

    def test_an_unreadable_matching_prompt_keeps_its_cancellation_until_the_next_readable_pass(self) -> None:
        self.panes["unit-one"].update(pane=EDIT_PROMPT, agent_status="blocked")
        self.assertEqual(self.tick(START).returncode, 0)
        launch = self.stop_unit()
        path = self.prompt_state()
        read = Path.read_text

        def unreadable(saved: Path, *arguments: object, **options: object) -> str:
            if saved == path:
                raise PermissionError("fixture prompt temporarily unreadable")
            self.assertFalse(arguments)
            encoding = options.get("encoding")
            return read(saved, encoding=encoding if isinstance(encoding, str) else None)

        with mock.patch.object(Path, "read_text", unreadable):
            result = self.tick(START + 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count(str(path)), 1, "one warning despite repeated cancellation scans")
        self.assertTrue(list(self.state.glob("*.prompt.cancel.json")))
        self.assertEqual(self.sent(), [])
        launch.unlink()
        self.panes.clear()
        self.assertEqual(self.tick(START + 2).returncode, 0)
        self.assertFalse(path.exists())
        self.assertEqual(self.prompt_followups(), [RETIRE])
        self.assertEqual(list(self.state.glob("*.prompt.cancel.json")), [])
        self.assertEqual(len(self.prompt_notices()), 0)

    def test_malformed_prompt_shapes_and_nonfinite_clocks_cannot_stop_other_alerts(self) -> None:
        self.panes["unit-one"].update(pane=EDIT_PROMPT, agent_status="blocked")
        self.state.mkdir(exist_ok=True)
        bad = [self.state / f"bad-{number}.prompt.json" for number in range(7)]
        records: list[object] = [[], None,
            {"since": float("nan"), "alerted": True, "key": "bad-nan"},
            {"since": START, "created": float("inf"), "alerted": True, "key": "bad-inf", "session_id": "old"},
            {"since": START, "created": START, "alerted": True, "key": "bad-told", "session_id": "old", "told": float("nan")},
            {"since": START, "alerted": True}]
        for path, record in zip(bad, records):
            _ = path.write_text(json.dumps(record))
        _ = bad[-1].write_text("{")
        result = self.tick(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in bad:
            self.assertEqual(result.stderr.count(str(path)), 1)
            self.assertFalse(path.exists())
        self.assertCountEqual([item["key"] for item in self.prompt_followups()], ["bad-nan", "bad-inf", "bad-told"])
        self.assertEqual(self.tick(START + 60).returncode, 0)
        self.assertEqual(len(self.prompt_notices()), 1)
        self.assertEqual([item["key"] for item in self.prompt_followups() if item.get("command") != "retire"], [PROMPT_KEY])

    def test_legacy_identified_state_is_migrated_before_cancellation_matching(self) -> None:
        self.state.mkdir(exist_ok=True)
        path = self.state / "legacy.prompt.json"
        _ = path.write_text(json.dumps({"since": START, "session_id": "sid-old", "key": "legacy-key", "alerted": True}))
        os.utime(path, (START, START))
        with mock.patch.object(stall_watch, "STATE_DIR", self.state):
            stall_watch.retire_session_prompt("sid-old")
        result = self.tick(START + 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count(str(path)), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.prompt_followups(), [{**RETIRE, "key": "legacy-key"}])
        self.assertEqual(list(self.state.glob("*.prompt.cancel.json")), [])
        self.assertEqual(len(self.prompt_notices()), 0)

    def test_failed_atomic_prompt_or_cancel_replace_leaves_no_temporary_file(self) -> None:
        self.state.mkdir(exist_ok=True)
        path = self.prompt_state()
        prompt = stall_watch.PromptWait(session_id="unit", created=START, key=PROMPT_KEY,
            since=START, alerted=False, tool="", retiring=False)
        with mock.patch.object(stall_watch, "STATE_DIR", self.state):
            stall_watch.save_prompt(path, prompt)
            before = path.read_bytes()
            with mock.patch.object(os, "replace", side_effect=OSError("fixture failed replace")):
                with self.assertRaisesRegex(OSError, "fixture failed replace"):
                    stall_watch.retire_session_prompt("unit")
                with self.assertRaisesRegex(OSError, "fixture failed replace"):
                    stall_watch.save_prompt(path, {**prompt, "alerted": True})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(self.state.iterdir()), [path])
            stall_watch.retire_session_prompt("unit")
            stall_watch.retire_session_prompt("unit")
            self.assertEqual(len(list(self.state.glob("*.prompt.cancel.json"))), 2)

    def test_an_oversized_cancellation_timestamp_cannot_stop_other_requests_or_alerts(self) -> None:
        self.panes["unit-one"].update(pane=EDIT_PROMPT, agent_status="blocked")
        self.assertEqual(self.tick(START).returncode, 0)
        # An old-schema oversized since and a current-schema oversized at must both be isolated.
        bad_since = self.state / "aaa.prompt.cancel.json"
        bad_at = self.state / "aab.prompt.cancel.json"
        _ = bad_since.write_text(json.dumps({"key": PROMPT_KEY, "since": 10 ** 400}))
        _ = bad_at.write_text(json.dumps({"session_id": "sid-old", "at": 10 ** 400}))
        path = self.state / "old.prompt.json"
        with mock.patch.object(stall_watch, "STATE_DIR", self.state):
            stall_watch.save_prompt(path, stall_watch.PromptWait(session_id="sid-old", created=0,
                key="stall-watch:prompt:old:unit", since=0, alerted=True, tool="old", retiring=False))
            stall_watch.retire_session_prompt("sid-old")
        result = self.tick(START + 60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr.count(str(bad_since)), 1)
        self.assertEqual(result.stderr.count(str(bad_at)), 1)
        self.assertIn("retained for retry", result.stderr)
        self.assertTrue(bad_since.exists())
        self.assertTrue(bad_at.exists())
        self.assertFalse(path.exists())
        self.assertIn({**RETIRE, "key": "stall-watch:prompt:old:unit"}, self.sent())
        self.assertEqual([item["key"] for item in self.prompt_followups() if item.get("command") != "retire"],
                         [PROMPT_KEY])
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_slow_inspection_does_not_move_the_prompts_escalation_deadline(self) -> None:
        _ = (self.root / "stall_watch.conf").write_text("prompt_grace_seconds=120\n")
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        elapsed = [0.0]
        original = stall_watch.processes

        def slow_scan(*, deadline: float, limits: stall_watch.SenderLimits | None = None
                      ) -> stall_watch.ProcessRows | stall_watch.ProcessSnapshotFailure:
            found = original(deadline=deadline, limits=limits)
            elapsed[0] = 90.0
            return found

        began = time.monotonic()
        with (mock.patch.object(time, "monotonic", side_effect=lambda: began + elapsed[0]),
              mock.patch.object(stall_watch, "processes", side_effect=slow_scan)):
            self.tell_prompt_owner(START + 60)
        prompt = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        self.assertEqual(prompt.get("told", 0), START + 60)
        self.assertEqual(self.tick(START + 179).returncode, 0)
        self.assertEqual(self.prompt_followups(), [])
        self.panes["unit-one"]["agent_status"] = "idle"
        self.panes["unit-one"]["pane"] = "— done: answered\n"
        _ = self.tick(START + 200)
        self.assertEqual(self.prompt_followups(), [])
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_invalid_grace_still_retires_cleared_alerts_and_recovers_pending_cases(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        with mock.patch.object(rca, "save", side_effect=OSError("case disk unavailable")):
            _ = self.tick(START + 60)
        self.panes["unit-one"]["agent_status"] = "idle"
        self.panes["unit-one"]["pane"] = "— done: answered\n"
        for setting in ("prompt_grace_seconds=invalid\n", "missing"):
            with self.subTest(setting=setting):
                config = self.root / "stall_watch.conf"
                if setting == "missing":
                    config.unlink()
                else:
                    _ = config.write_text(setting)
                result = self.tick(START + 120)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("using zero prompt grace", result.stderr)
                self.assertFalse(self.prompt_state().exists())
                self.assertEqual(self.prompt_followups()[1:], [RETIRE])
                self.assertEqual([item["key"] for item in self.owner_messages()], ["rca-261006-1"])
        result = self.tick(START + 120 + 1800)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item["key"] for item in self.owner_messages()], ["rca-261006-1", "rca-261006-1-remind"])
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_a_unit_at_a_prompt_with_the_auto_deny_banner_gets_no_alert_and_no_bump(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = AUTO_DENY_PROMPT
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 60).returncode, 0)
        self.assertEqual(self.tick(START + 900).returncode, 0)
        self.assertEqual(self.sent(), [])
        # The same unit at a prompt without the banner waits on the user, and its alert time starts then.
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        self.assertEqual(self.tick(START + 901).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.tell_prompt_owner(START + 961)
        self.assertEqual(self.tick(START + 961).returncode, 0)
        self.assertEqual([item["to"] for item in self.prompt_followups()], ["user"])
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_the_request_is_read_from_the_prompt_at_the_bottom_of_the_pane(self) -> None:
        from scripts.production import stall_watch
        self.assertEqual(stall_watch.prompt_request(EDIT_PROMPT), "edit panes.py")
        self.assertEqual(stall_watch.prompt_request(BASH_PROMPT),
                         "run a shell command to read lines 5 to the end of a saved output")
        self.assertEqual(stall_watch.prompt_request(" Do you want to create notes.md?\n ❯ 1. Yes\n"), "create notes.md")
        self.assertEqual(stall_watch.prompt_request("Bash command\n\n   ls\n\n Do you want to proceed?\n"),
                         "run a shell command")
        self.assertEqual(stall_watch.prompt_request("— holding: waiting\n"), "go on with its work")

    def test_the_request_skips_the_auto_deny_banner(self) -> None:
        from scripts.production import stall_watch
        self.assertEqual(stall_watch.prompt_request(AUTO_DENY_PROMPT),
                         "run a shell command to read lines 5 to the end of a saved output")
        self.assertTrue(stall_watch.auto_denied(AUTO_DENY_PROMPT))
        # The banner quoted in the transcript above a prompt is not the prompt's own banner.
        quoted = " The push said ⚠ Claude Code will automatically deny this request in 0:41.\n\n" + EDIT_PROMPT
        self.assertFalse(stall_watch.auto_denied(quoted))

    def test_the_request_comes_from_the_pending_tool_call_in_the_transcript(self) -> None:
        from scripts.production import stall_watch
        call = stall_watch.PendingTool("toolu_1", "Bash", dict(DELETE_CALL), START)
        # A Bash call reads as its command with each path shown by its file name, never as its description,
        # also when the screen shows a description or the outside warning.
        for pane in ("", BASH_PROMPT, OUTSIDE_PROMPT):
            self.assertEqual(stall_watch.prompt_request(pane, call), "run rm -f b1e4yc8b9.txt")
        edit = stall_watch.PendingTool("toolu_1", "Edit", {"file_path": "/home/natepiano/rust/x/src/panes.py"}, START)
        self.assertEqual(stall_watch.prompt_request("", edit), "edit panes.py")
        # A tool the transcript cannot word falls back to the prompt on screen.
        other = stall_watch.PendingTool("toolu_1", "WebFetch", {"url": "https://example.com"}, START)
        self.assertEqual(stall_watch.prompt_request(EDIT_PROMPT, other), "edit panes.py")

    def test_a_wrapped_prompt_with_the_outside_warning_reads_as_a_whole_sentence(self) -> None:
        from scripts.production import stall_watch
        self.assertEqual(stall_watch.prompt_request(OUTSIDE_PROMPT),
                         DELETE_REQUEST)
        # Without the warning, the wrapped description is the request, joined into one sentence.
        unwarned = OUTSIDE_PROMPT[:OUTSIDE_PROMPT.index("\n\n   rm -f deletes")] + "\n\n Do you want to proceed?\n"
        self.assertEqual(stall_watch.prompt_request(unwarned),
                         "run a shell command to write the 212 enrolled state and confirm the prompt names the worktree")

    def test_the_wait_starts_at_the_pending_tool_call_before_herdr_reports_blocked(self) -> None:
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        made = START - 3 * 3600
        _ = transcript.write_text(tool_call("Bash", DELETE_CALL, made))
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = OUTSIDE_PROMPT
        # An old pending call tells the owner at once; a zero-grace fixture alerts next tick.
        self.tell_prompt_owner(START)
        self.assertEqual(self.tick(START).returncode, 0)
        alerts = self.prompt_followups()
        self.assertEqual([item["to"] for item in alerts], ["user"])
        values = self.last_alert_values()
        self.assertEqual(values["since"], "09:00 PDT")
        self.assertIn("b1e4yc8b9.txt", values["request"])
        # The same prompt gets no second alert.
        self.assertEqual(self.tick(START + 60).returncode, 0)
        self.assertEqual(self.tick(START + 900).returncode, 0)
        self.assertEqual(len(self.prompt_followups()), 1)
        # The call is answered and the next call waits at a new prompt, with herdr blocked the whole time:
        # the first alert is retired, and the new prompt gets its own alert.
        _ = transcript.write_text(tool_call("Bash", DELETE_CALL, made) + tool_result()
                                  + tool_call("Bash", DELETE_CALL, START + 950, "toolu_2"))
        self.assertEqual(self.tick(START + 960).returncode, 0)
        self.assertEqual(self.prompt_followups()[1:], [RETIRE])
        self.tell_prompt_owner(START + 1020)
        self.assertEqual(self.tick(START + 1020).returncode, 0)
        values = self.last_alert_values()
        self.assertEqual((len(self.prompt_followups()), self.prompt_followups()[-1]["key"], values["since"]), (3, PROMPT_KEY, "12:15 PDT"))
        self.assertEqual(len(self.prompt_notices()), 2)

    def prompt_state(self) -> Path:
        return self.watches["unit-one"].prompt_path

    def alerted_at_a_prompt(self) -> None:
        """A zero-grace fixture tells the owner and alerts at the same threshold."""
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        self.tell_prompt_owner(START + 60)
        self.assertEqual(self.tick(START + 60).returncode, 0)
        self.assertEqual([item["key"] for item in self.prompt_followups()], [PROMPT_KEY])

    def test_a_failed_retire_is_sent_again_next_tick_and_no_new_alert_goes(self) -> None:
        self.alerted_at_a_prompt()
        _ = self.fail_file.write_text('["retire"]')
        self.panes["unit-one"]["agent_status"] = "idle"
        failed = self.tick(START + 120)
        self.assertIn("stall-watch: retire:", failed.stderr)
        self.assertTrue(self.prompt_state().exists())
        # Blocked again while the retire still fails: the retire is sent again, and no alert.
        self.panes["unit-one"]["agent_status"] = "blocked"
        _ = self.tick(START + 180)
        _ = self.fail_file.write_text("[]")
        self.assertEqual(self.tick(START + 240).returncode, 0)
        self.assertEqual(self.prompt_followups()[1:], [RETIRE, RETIRE, RETIRE])
        self.assertFalse(self.prompt_state().exists())
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_an_auto_denied_prompt_after_its_alert_retires_the_alert(self) -> None:
        self.alerted_at_a_prompt()
        self.panes["unit-one"]["pane"] = AUTO_DENY_PROMPT
        self.assertEqual(self.tick(START + 120).returncode, 0)
        self.assertEqual(self.prompt_followups()[1:], [RETIRE])
        self.assertFalse(self.prompt_state().exists())
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_a_finished_run_after_a_prompt_alert_retires_the_alert(self) -> None:
        self.alerted_at_a_prompt()
        self.states["unit-one"] = "run-finished"
        self.assertEqual(self.tick(START + 120).returncode, 0)
        self.assertEqual(self.prompt_followups()[1:], [RETIRE])
        self.assertFalse(self.prompt_state().exists())
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_a_prompt_that_never_alerted_sends_no_retire(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        self.assertTrue(self.prompt_state().exists())
        self.panes["unit-one"]["agent_status"] = "idle"
        self.assertEqual(self.tick(START + 30).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertFalse(self.prompt_state().exists())

    def test_a_sent_prompt_alert_opens_one_rca_case_and_its_owner_is_reminded_once(self) -> None:
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        _ = transcript.write_text(tool_call("Bash", DELETE_CALL, START - 60))
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = OUTSIDE_PROMPT
        # A failed alert opens no case; the alert sent on the next tick opens one.
        _ = self.fail_file.write_text('["user"]')
        self.tell_prompt_owner(START)
        self.assertEqual(self.owner_messages(), [])
        _ = self.fail_file.write_text("[]")
        result = self.tick(START + 60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rca: opened 261006-1", result.stdout)
        case = cast(dict[str, object], json.loads((self.root / "state/message/rca/261006-1.json").read_text()))
        self.assertEqual((case["session"], case["production"], case["kind"], case["status"], case["commands"],
                          case["transcript"]),
                         ("unit-one", "showrunner", "prompt", "open", [DELETE_CALL["command"]], str(transcript)))
        # The case keeps when the wait began (the pending call's time), and the alert's sender and key,
        # which `rca.py fixed` retires it by.
        self.assertEqual((case["stopped"], case["alert_source"], case["alert_key"], case["waits"]),
                         (datetime.fromtimestamp(START - 60, ZoneInfo("UTC")).isoformat(), "stall-watch",
                          PROMPT_KEY, ""))
        owner = self.owner_messages()
        self.assertEqual([(item["to"], item["from"], item["key"]) for item in owner],
                         [(f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}", "rca", "rca-261006-1")])
        self.assertIn(DELETE_CALL["command"], owner[0]["text"])
        self.assertIn("rca.py start 261006-1 --owner", owner[0]["text"])
        # The same prompt opens nothing more; 30 minutes after the case opened its owner gets one reminder.
        _ = self.tick(START + 120)
        _ = self.tick(START + 60 + 1799)
        self.assertEqual(len(self.owner_messages()), 1)
        _ = self.tick(START + 60 + 1800)
        _ = self.tick(START + 60 + 1900)
        self.assertEqual([item["key"] for item in self.owner_messages()], ["rca-261006-1", "rca-261006-1-remind"])

    def test_a_pending_case_retries_without_resending_the_delivered_prompt_alert(self) -> None:
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        _ = transcript.write_text(tool_call("Bash", DELETE_CALL, START - 60))
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        with mock.patch.object(rca, "save", side_effect=OSError("case disk unavailable")):
            failed = self.tick(START)
        self.assertIn("rca: no case opened: OSError: case disk unavailable", failed.stderr)
        prompt = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        self.assertTrue(prompt["alerted"])
        self.assertIn("case_pending", prompt)
        self.assertEqual(self.owner_messages(), [])
        result = self.tick(START + 120)
        self.assertIn("rca: opened 261006-1", result.stdout)
        prompt = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        self.assertTrue(prompt["alerted"])
        self.assertNotIn("case_pending", prompt)
        case = self.root / "state/message/rca/261006-1.json"
        self.assertTrue(case.exists())
        saved = cast(dict[str, object], json.loads(case.read_text()))
        self.assertEqual(saved["commands"], [DELETE_CALL["command"]])
        self.assertEqual(saved["transcript"], str(transcript))
        _ = self.tick(START + 180)
        self.assertEqual([item["key"] for item in self.prompt_followups()], [PROMPT_KEY])
        self.assertEqual([item["key"] for item in self.owner_messages()], ["rca-261006-1"])
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_a_cleared_prompt_retires_its_alert_and_keeps_retrying_its_pending_case(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        with mock.patch.object(rca, "save", side_effect=OSError("case disk unavailable")):
            failed = self.tick(START + 60)
            self.assertIn("rca: no case opened: OSError: case disk unavailable", failed.stderr)
            prompt = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
            self.assertTrue(prompt["alerted"])
            self.assertIn("case_pending", prompt)
            self.assertEqual(self.owner_messages(), [])
            self.panes["unit-one"]["agent_status"] = "idle"
            cleared = self.tick(START + 120)
        self.assertIn("rca: no case opened: OSError: case disk unavailable", cleared.stderr)
        self.assertEqual(self.prompt_followups()[1:], [RETIRE])
        self.assertFalse(self.prompt_state().exists())
        pending = list(self.state.glob("*.case.json"))
        self.assertEqual(len(pending), 1)
        prompt = cast(stall_watch.PromptWait, json.loads(pending[0].read_text()))
        self.assertTrue(prompt["retiring"])
        self.assertIn("case_pending", prompt)
        self.assertEqual(self.owner_messages(), [])
        result = self.tick(START + 180)
        self.assertIn("rca: opened 261006-1", result.stdout)
        self.assertTrue((self.root / "state/message/rca/261006-1.json").exists())
        self.assertFalse(self.prompt_state().exists())
        self.assertFalse(pending[0].exists())
        _ = self.tick(START + 240)
        self.assertEqual(self.prompt_followups()[1:], [RETIRE])
        self.assertEqual([item["key"] for item in self.owner_messages()], ["rca-261006-1"])
        self.assertEqual(len(self.prompt_notices()), 1)

    def test_a_retired_alerts_pending_case_does_not_delay_a_new_prompt_alert(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        with mock.patch.object(rca, "save", side_effect=OSError("case disk unavailable")):
            failed = self.tick(START + 60)
            self.assertIn("rca: no case opened: OSError: case disk unavailable", failed.stderr)
            self.panes["unit-one"]["agent_status"] = "idle"
            _ = self.tick(START + 120)
            self.assertEqual(self.prompt_followups()[1:], [RETIRE])
            self.assertFalse(self.prompt_state().exists())
            old_cases = list(self.state.glob("*.case.json"))
            self.assertEqual(len(old_cases), 1)
            transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
            _ = transcript.write_text(tool_call("Bash", DELETE_CALL, START + 180))
            self.panes["unit-one"]["agent_status"] = "blocked"
            self.panes["unit-one"]["pane"] = BASH_PROMPT
            _ = self.tick(START + 180)
            _ = self.tick(START + 180 + stall_watch.prompt_alert_seconds() - 1)
            self.assertEqual(self.prompt_followups()[1:], [RETIRE])
            self.tell_prompt_owner(START + 180 + stall_watch.prompt_alert_seconds())
            retry = self.tick(START + 180 + stall_watch.prompt_alert_seconds())
        self.assertIn("rca: no case opened: OSError: case disk unavailable", retry.stderr)
        self.assertEqual([item["key"] for item in self.prompt_followups()], [PROMPT_KEY, PROMPT_KEY, PROMPT_KEY])
        self.assertEqual(self.prompt_followups()[1], RETIRE)
        self.assertEqual(self.prompt_followups()[2]["text-id"], "stall_watch.permission_prompt")
        self.assertTrue(old_cases[0].exists())
        current = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        self.assertTrue(current["alerted"])
        self.assertFalse(current["retiring"])
        self.assertIn("case_pending", current)
        self.assertEqual(self.owner_messages(), [])
        result = self.tick(START + 300)
        self.assertIn("rca: opened", result.stdout)
        self.assertFalse(old_cases[0].exists())
        self.assertEqual(list(self.state.glob("*.case.json")), [])
        current = cast(stall_watch.PromptWait, json.loads(self.prompt_state().read_text()))
        self.assertTrue(current["alerted"])
        self.assertNotIn("case_pending", current)
        _ = self.tick(START + 360)
        self.assertEqual([item["key"] for item in self.prompt_followups()], [PROMPT_KEY, PROMPT_KEY, PROMPT_KEY])
        self.assertEqual(len(self.owner_messages()), 2)
        self.assertEqual(len(self.prompt_notices()), 2)

    def test_a_helpers_pending_bash_call_names_its_command_cut_to_90_characters(self) -> None:
        from scripts.production import stall_watch
        from scripts.notify import push_texts
        helpers = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee" / "subagents"
        helpers.mkdir(parents=True)
        _ = (self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl").write_text(
            tool_call("Bash", {"command": "ls"}, START - 600) + tool_result())
        _ = (helpers / "agent-a1.jsonl").write_text(tool_call("Bash", {"command": "git status"}, START - 300, "toolu_a"))
        _ = (helpers / "agent-a2.jsonl").write_text(tool_call(
            "Bash", {"command": "cargo build -p cargo-handler", "description": "Build the handler crate"}, START - 60, "toolu_b"))
        with mock.patch.object(stall_watch, "PROJECTS_DIR", self.projects):
            pending = stall_watch.pending_tool("204f532d-a97a-59e7-a6a7-caf86af3f5ee")
        assert pending is not None
        self.assertEqual(pending.id, "toolu_b")
        # The command, never the description.
        self.assertEqual(stall_watch.prompt_request(BASH_PROMPT, pending), "run cargo build -p cargo-handler")
        long = stall_watch.PendingTool("x", "Bash", {"command": "echo " + "y" * 120}, START)
        self.assertEqual(stall_watch.prompt_request("", long), "run echo " + "y" * 84 + "…")
        rendered = push_texts.render(stall_watch.PROMPT_TEXT_ID, unit="handler", machine="natedev",
                                     since="09:01 AST", request="run cargo build -p cargo-handler")
        self.assertEqual(rendered.body, "handler asks to run cargo build -p cargo-handler. It has waited since 09:01 AST."
                         + "\n\nYes: it does that and goes on.\n\nNo: it does not, and asks you what to do instead.")

    def test_blocked_turn_end_waits_for_a_new_unblocked_status(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        for status in ("— blocked: waiting on the showrunner: G1 clears\n",
                       "— blocked: the 96-hour measurement window closes\n"):
            with self.subTest(status=status):
                self.panes["unit-one"]["pane"] = status
                self.assertEqual(self.tick(START).returncode, 0)
                self.assertEqual(self.tick(START + 600).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.panes["unit-one"]["pane"] = status + "— holding: ready for next task\n"
                self.assertEqual(self.tick(START + 601).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 1201).returncode, 0)
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                                 {"bump", "tell"})
                self.send_log.unlink()
                for path in self.state.glob("*.json"):
                    path.unlink()

    def write_context_usage(self, tokens: int, *, window: int = 300_000) -> None:
        """A local model registry, configured window and last billed context; no real settings."""
        registry = self.root / "models.conf"
        _ = write_registry(registry, {"claude.context": {"default": str(window)}}, replace_sections=("claude.context",))
        self.environment.update(MODELS_CONFIG_FILE=str(registry), CLAUDE_CODE_AUTO_COMPACT_WINDOW="400k")
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        entries = [{"type": "assistant", "message": {"model": "claude-test-1", "usage": usage}}
                   for usage in ({"input_tokens": 400_000},
                                 {"input_tokens": tokens // 3, "cache_creation_input_tokens": tokens // 3,
                                  "cache_read_input_tokens": tokens - 2 * (tokens // 3),
                                  "output_tokens": 400_000})]
        _ = transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        os.utime(transcript, (START - 3600, START - 3600))

    def test_only_blocked_turns_bump_near_compaction_and_other_waits_reset_the_stretch(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        blocked = "— blocked: my context is full; it compacts on the next turn"
        window = 283_800
        trigger = context_usage.trigger_tokens(window)
        threshold = context_usage.handoff_threshold(window)
        self.assertEqual(trigger, 250_800)
        # Blocked turns wake at the threshold, the accounts reading, the trigger and above the trigger.
        for status, tokens in [(blocked, tokens) for tokens in (threshold, 245_621, trigger, trigger + 1)] + [
                (f"— {kind}: waiting", threshold) for kind in ("done", "gate", "decision")] + [
                ("— holding: until 18:00", threshold),
                ("— holding: your confirmed values until 18:00", threshold)]:
            with self.subTest(status=status, tokens=tokens):
                pane = f"● {status}\n"
                self.panes["unit-one"]["pane"] = pane
                self.write_context_usage(tokens, window=window)
                self.state.mkdir(exist_ok=True)
                path = self.watches["unit-one"].stretch_path
                stretch = stall_watch.Stretch(pane_hash=hashlib.sha256(pane.strip().encode()).hexdigest(), since=START - 601,
                    bump_sent=False, tell_sent=False, reported_status="")
                _ = path.write_text(json.dumps(stretch))
                self.assertEqual(self.tick(START).returncode, 0)
                wakes = status == blocked
                self.assertEqual(len(self.sent()), 2 if wakes else 0)
                self.assertEqual(self.tick(START + 600).returncode, 0)
                sent = self.sent()
                self.assertEqual(len(sent), 2 if wakes else 0)
                if not wakes:
                    self.assertEqual(sent, [])
                else:
                    tell, = [item for item in sent if item["key"].endswith(":tell")]
                    bump, = [item for item in sent if item["key"].endswith(":bump")]
                    self.assertEqual(bump["key"].rsplit(":", 1)[-1], "bump")
                    self.assertIn("204f532d-a97a-59e7-a6a7-caf86af3f5ee.sock", bump["to"])
                    self.assertEqual(tell["key"].rsplit(":", 1)[-1], "tell")
                    self.assertIn("54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock", tell["to"])
                    self.assertIn(f"Last status: {status}", tell["text"])
                saved = cast(stall_watch.Stretch, json.loads(path.read_text()))
                self.assertEqual(saved["since"], START - 601 if wakes else START + 600)
                self.assertEqual(saved["reported_status"], status if wakes else "")
                self.assertEqual(saved["bump_sent"], wakes)
                self.assertEqual(saved["tell_sent"], wakes)
                self.send_log.unlink(missing_ok=True)
                path.unlink()

    def test_pauses_and_external_owners_stay_quiet_even_at_the_compaction_wake(self) -> None:
        statuses = (
            "holding: paused until the showrunner says resume",
            "— holding: waiting on the showrunner",
            "● — holding: waiting for the user",
            "⏺ — holding: held on another unit",
            "— holding: waiting on another session",
            "— holding: waiting on session:peer-id",
            "— holding: waiting on typesafe",
            "— blocked: waiting on the showrunner",
        )
        self.configure({"showrunner": ["unit-one", "typesafe"]})
        # A peer may be absent from herdr; the production's live rows still name its owner.
        window = 300_000
        threshold = context_usage.handoff_threshold(window)
        for tokens in (threshold - 1, threshold):
            for status in statuses:
                with self.subTest(status=status, tokens=tokens):
                    self.panes["unit-one"]["pane"] = status + "\n"
                    self.write_context_usage(tokens, window=window)
                    for at in (START, START + 3600):
                        result = self.tick(at)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(self.sent(), [])
                        path, = self.state.glob("*.json")
                        stretch = cast(stall_watch.Stretch, json.loads(path.read_text()))
                        self.assertEqual(stretch["since"], at)
                        self.assertFalse(stretch["bump_sent"])
                        self.assertFalse(stretch["tell_sent"])

    def test_latest_transcript_wait_on_natedev_overrides_a_stale_pane(self) -> None:
        _ = showrunner_relationship("natedev")
        self.panes["unit-one"]["pane"] = "— holding: waiting on the second blind code reviewer\n"
        transcript = self.projects / "project/204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        waiting = "— holding: no job: waiting on natedev for the next assignment"
        _ = transcript.write_text(json.dumps(self.said({"type": "text", "text": waiting})) + "\n")
        os.utime(transcript, (START - 3600, START - 3600))
        for at in (START, START + 600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.sent(), [])
        # An actual new assignment still recovers if its own work stops.
        _ = transcript.write_text(json.dumps(self.said({"type": "tool_use", "name": "Bash"})) + "\n")
        os.utime(transcript, (START - 3600, START - 3600))
        self.panes["unit-one"]["pane"] = PANE
        self.assertEqual(self.tick(START + 601).returncode, 0)
        self.assertEqual(self.tick(START + 901).returncode, 0)
        tell, = [item for item in self.sent() if item["key"].endswith(":tell")]
        self.assertIn("Last status: none on screen", tell["text"])
        self.assertEqual(len(self.sent()), 2)

    def test_a_resumed_holding_unit_gets_a_fresh_idle_stretch_before_its_bump(self) -> None:
        cases = (
            ("holding: paused until the showrunner says resume\n", "", "waiting"),
            ("holding: waiting on the unit tests\nLogin reached its time limit\n",
             "unit-one: the Claude login reached its time limit and needs /login.", "login limit"),
            ("holding: waiting on the unit tests\nLogin reached its time limit\n", "", "fresh"),
        )
        for pane, notice, decision in cases:
            with self.subTest(decision=decision):
                self.send_log.unlink(missing_ok=True)
                self.watches["unit-one"].stretch_path.unlink(missing_ok=True)
                self.panes["unit-one"]["pane"] = pane
                if decision == "fresh":
                    transcript = self.projects / "project" / f"{unit_session('unit-one')}.jsonl"
                    _ = transcript.write_text(json.dumps({"type": "system", "error": "Login reached its time limit"}) + "\n")
                    os.utime(transcript, (START - 60, START - 60))
                    at = datetime.fromtimestamp(START - 1, ZoneInfo("UTC")).isoformat()
                    _ = (self.root / "usage.json").write_text(json.dumps({
                        "_directory_logins": {usage_store.directory_key("Claude", str(self.root / ".claude")):
                                              "fixture@example.invalid"},
                        "fixture@example.invalid": {"read_outcome": UsageReadOutcome.OK,
                                                     "weekly": {"left": 50, "resets": "", "at": at}},
                    }))
                    result = self.tick(START)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("inspected showrunner/unit-one: fresh", result.stdout)
                    self.assertEqual(self.sent(), [])
                    self.assertEqual(self.tick(START + 300).returncode, 0)
                    resumed = self.sent()
                    self.assertEqual(len(resumed), 2)
                    self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in resumed}, {"bump", "tell"})
                    tell = next(item for item in resumed if item["key"].endswith(":tell"))
                    self.assertIn("nothing running; bumped", tell["text"])
                    continue
                notices = [notice] if notice else []
                for at in (START, START + 3600):
                    result = self.tick(at)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"inspected showrunner/unit-one: {decision}", result.stdout)
                    self.assertEqual([item["text"] for item in self.sent()], notices)
                if notice:
                    tell, = self.sent()
                    self.assertTrue(tell["key"].endswith(":tell"))
                    self.assertEqual(tell["to"], f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}")
                self.panes["unit-one"]["pane"] = pane + "holding: waiting on the unit tests\n"
                self.assertEqual(self.tick(START + 3601).returncode, 0)
                self.assertEqual([item["text"] for item in self.sent()], notices)
                self.assertEqual(self.tick(START + 3901).returncode, 0)
                resumed = self.sent()[len(notices):]
                self.assertEqual(len(resumed), 2)
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in resumed}, {"bump", "tell"})

    def test_status_vocabulary_is_reread_without_restarting_the_watcher(self) -> None:
        self.panes["unit-one"]["pane"] = "holding: budget nap\n"
        self.assertEqual(self.tick(START).returncode, 0)
        _ = self.watch_settings(prompt_grace_seconds="0", paused_status_pattern=r"\bbudget nap\b")
        self.assertEqual(self.tick(START + 300).returncode, 0)
        self.assertEqual(self.sent(), [])
        _ = self.watch_settings(prompt_grace_seconds="0")
        self.assertEqual(self.tick(START + 301).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tick(START + 601).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})

    def test_a_paused_turn_end_in_the_transcript_also_prevents_bumps(self) -> None:
        self.compacted(self.said({"type": "text", "text": "holding: paused until the showrunner says resume"}))
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 3600).returncode, 0)
        self.assertEqual(self.sent(), [])

    def test_an_invalid_status_rule_reports_the_problem_without_bumping(self) -> None:
        self.panes["unit-one"]["pane"] = "holding: paused until the showrunner says resume\n"
        _ = self.watch_settings(prompt_grace_seconds="0", paused_status_pattern="[")
        for at in (START, START + 3600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no bump: invalid status rule", result.stderr)
            self.assertEqual(self.sent(), [])

    def test_an_own_worker_wait_recovers_after_the_worker_stops_without_a_status_change(self) -> None:
        for status in ("holding: waiting on the Codex writer for ticket 49",
                       "holding: waiting on the Codex worker for ticket 49",
                       "holding: waiting on the reviewer",
                       "holding: waiting on the tests"):
            with self.subTest(status=status):
                self.panes["unit-one"]["pane"] = status + "\n"
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                # The recording ps fixture models the unit's worker launcher, then its termination.
                director_pid = self.children[0].pid
                self.process_rows.append(f"{director_pid + 1} {director_pid} bash scripts/agents/agent_bg.sh running")
                for at in (START, START + 300):
                    result = self.tick(at)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.sent(), [])
                path, = self.state.glob("*.json")
                stretch = cast(stall_watch.Stretch, json.loads(path.read_text()))
                self.assertEqual(stretch["since"], START + 300)
                self.assertFalse(stretch["bump_sent"])
                self.assertFalse(stretch["tell_sent"])
                _ = self.process_rows.pop()
                self.assertEqual(self.tick(START + 301).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 601).returncode, 0)
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
                self.assertEqual(len(self.sent()), 2)
                tell = next(item for item in self.sent() if item["key"].endswith(":tell"))
                self.assertIn(f"Last status: {status}", tell["text"])

    def test_malformed_owner_captures_preserve_other_units_and_pending_deliveries(self) -> None:
        self.configure({"showrunner": ["unit-one", "bad", "following"]})
        _ = self.unit("bad")
        _ = self.unit("following")
        for name in ("unit-one", "following"):
            self.panes[name]["pane"] = PANE
        self.panes["bad"]["pane"] = "holding: waiting on the showrunner\n"
        for pattern in (r"\bwaiting\b(?P<owner>never)?", r"\bwaiting\b(?P<owner>)", r"\bwaiting\b"):
            with self.subTest(pattern=pattern):
                _ = self.watch_settings(prompt_grace_seconds="0", external_wait_pattern=pattern)
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                for at in (START, START + 300):
                    result = self.tick(at)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("bad: no bump: invalid status rule", result.stderr)
                    self.assertIn("external_wait_pattern must capture a nonempty owner", result.stderr)
                sent = self.sent()
                self.assertEqual(len(sent), 4)
                self.assertEqual({item["to"] for item in sent if item["key"].endswith(":bump")},
                                 {f"uds:{self.root / (unit_session(name) + '.sock')}"
                                  for name in ("unit-one", "following")})
                self.assertEqual(len([item for item in sent if item["key"].endswith(":tell")]), 2)

    def test_a_waiting_unit_below_compaction_stays_quiet_and_resets_its_stretch(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        pane = "● — blocked: my context is full; it compacts on the next turn\n"
        self.panes["unit-one"]["pane"] = pane
        window = 283_800
        self.write_context_usage(context_usage.handoff_threshold(window) - 1, window=window)
        self.state.mkdir(exist_ok=True)
        path = self.watches["unit-one"].stretch_path
        stretch = stall_watch.Stretch(pane_hash=hashlib.sha256(pane.strip().encode()).hexdigest(), since=START - 601,
                                      bump_sent=False, tell_sent=False, reported_status="")
        _ = path.write_text(json.dumps(stretch))
        for at in (START, START + 600):
            self.assertEqual(self.tick(at).returncode, 0)
            self.assertEqual(self.sent(), [])
            path, = self.state.glob("*.json")
            stretch = cast(stall_watch.Stretch, json.loads(path.read_text()))
            self.assertEqual(stretch["since"], at)
            self.assertFalse(stretch["bump_sent"])
            self.assertFalse(stretch["tell_sent"])
            self.assertEqual(stretch["reported_status"], "")

    def test_a_compacted_waiting_unit_does_not_wake_from_its_discarded_usage(self) -> None:
        self.panes["unit-one"]["pane"] = "— blocked: my context is full; it compacts on the next turn\n"
        self.write_context_usage(context_usage.trigger_tokens(300_000))
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        with transcript.open("a") as file:
            _ = file.write(json.dumps({"type": "system", "compactMetadata": {"postTokens": 1000}}) + "\n")
        os.utime(transcript, (START - 3600, START - 3600))
        for at in (START, START + 600):
            self.assertEqual(self.tick(at).returncode, 0)
            self.assertEqual(self.sent(), [])
            path, = self.state.glob("*.json")
            stretch = cast(stall_watch.Stretch, json.loads(path.read_text()))
            self.assertEqual(stretch["since"], at)

    def test_one_line_blocked_reply_supersedes_older_holding_with_either_bullet(self) -> None:
        for bullet in ("●", "⏺"):
            with self.subTest(bullet=bullet):
                self.panes["unit-one"]["pane"] = (
                    "  — holding: waiting on the Codex writer for ticket 49\n"
                    + f"{bullet} — blocked: waiting on the showrunner: sync\n")
                for at in (START, START + 300, START + 600):
                    self.assertEqual(self.tick(at).returncode, 0)
                    self.assertEqual(self.sent(), [])
                    path, = self.state.glob("*.json")
                    stretch = cast(stall_watch.Stretch, json.loads(path.read_text()))
                    self.assertEqual(stretch["since"], at)
                    self.assertFalse(stretch["bump_sent"])
                    self.assertFalse(stretch["tell_sent"])
                    self.assertEqual(stretch["reported_status"], "")

    def test_one_line_own_holding_reply_resets_reported_stretch_and_bumps_without_bullet(self) -> None:
        old = "— holding: waiting on the Codex writer for ticket 49"
        self.panes["unit-one"]["pane"] = f"  {old}\n"
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 300).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        path, = self.state.glob("*.json")
        stretch = cast(stall_watch.Stretch, json.loads(path.read_text()))
        self.assertEqual(stretch["reported_status"], old)
        status = "— holding: ready for next task"
        self.panes["unit-one"]["pane"] += f"● {status}\n"
        self.assertEqual(self.tick(START + 301).returncode, 0)
        stretch = cast(stall_watch.Stretch, json.loads(path.read_text()))
        self.assertEqual(stretch["since"], START + 301)
        self.assertFalse(stretch["bump_sent"])
        self.assertFalse(stretch["tell_sent"])
        self.assertEqual(stretch["reported_status"], "")
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.tick(START + 601).returncode, 0)
        self.assertEqual(len(self.sent()), 4)
        tell = next(item for item in self.sent()[2:] if item["key"].endswith(":tell"))
        self.assertTrue(tell["text"].endswith(f"Last status: {status}"))
        self.assertNotIn("●", tell["text"])
        stretch = cast(stall_watch.Stretch, json.loads(path.read_text()))
        self.assertEqual(stretch["reported_status"], status)

    def test_turn_end_words_inside_sentences_do_not_match(self) -> None:
        for line in ("● I was holding: on the user", "※ recap: the unit said — holding: on the user"):
            with self.subTest(line=line):
                self.assertIsNone(stall_watch.TURN_END.search(line))

    def test_gate_turn_end_waits_until_a_later_holding_status_stalls(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        for status in ("— gate: the user runs the Mac Claude go-live and controls at a Mac terminal\n",
                       "gate: the user runs the Mac Claude go-live and controls at a Mac terminal\n"):
            with self.subTest(status=status):
                self.panes["unit-one"]["pane"] = status
                self.assertEqual(self.tick(START).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 600).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.panes["unit-one"]["pane"] = status + "— holding: ready for next task\n"
                self.assertEqual(self.tick(START + 601).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 1200).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 1201).returncode, 0)
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                                 {"bump", "tell"})
                self.send_log.unlink(missing_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()

    def test_decision_turn_end_waits_until_a_later_holding_status_stalls(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        for status in ("— decision: the user chooses whether to proceed\n",
                       "decision: the user chooses whether to proceed\n"):
            with self.subTest(status=status):
                self.panes["unit-one"]["pane"] = status
                self.assertEqual(self.tick(START).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 600).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.panes["unit-one"]["pane"] = status + "— holding: ready for next task\n"
                self.assertEqual(self.tick(START + 601).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 1200).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 1201).returncode, 0)
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                                 {"bump", "tell"})
                self.send_log.unlink(missing_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()

    def compacted(self, *entries: dict[str, object]) -> None:
        """The pane after an idle compaction, and a transcript whose last turn holds `entries`."""
        self.panes["unit-one"]["pane"] = "✻ Conversation compacted (ctrl+o for history)\n"
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        after = [{"type": "system", "subtype": "compact_boundary"},
                 {"type": "user", "message": {"content": "summary: the unit was holding: on the user"}},
                 {"type": "attachment"}]
        _ = transcript.write_text("".join(json.dumps(entry) + "\n" for entry in (*entries, *after)))
        os.utime(transcript, (START - 3600, START - 3600))

    @staticmethod
    def said(*blocks: dict[str, str]) -> dict[str, object]:
        return {"type": "assistant", "message": {"content": list(blocks)}}

    def test_a_compacted_pane_reads_a_blocked_turn_end_from_the_transcript(self) -> None:
        peer_prompt: dict[str, object] = {
            "type": "user", "message": {"content": "Peer update: the Ghostty check remains blocked."}}
        peer_result: dict[str, object] = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "peer-call", "content": "Peer update sent."}]}}
        for peer_entries in ([],
                             [peer_prompt, self.said({"type": "text", "text": "—"})],
                             [peer_prompt,
                              self.said({"type": "tool_use", "name": "Bash", "id": "peer-call"}),
                              peer_result,
                              self.said({"type": "text", "text": " \n—\n "})]):
            with self.subTest(peer_entries=peer_entries):
                self.send_log.unlink(missing_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.compacted(self.said({"type": "text", "text": "Ready for the check."}),
                               self.said({"type": "thinking", "thinking": ""}),
                               self.said({"type": "text", "text": "Waiting.\n\n— blocked: the user's Ghostty check"}),
                               *peer_entries)
                if peer_entries:
                    self.panes["unit-one"]["pane"] = "— holding: waiting on the tests\n"
                self.assertEqual(self.tick(START).returncode, 0)
                self.assertEqual(self.tick(START + 3600).returncode, 0)
                self.assertEqual(self.sent(), [])

    def test_a_compacted_pane_bumps_when_the_transcript_names_own_work_or_went_on(self) -> None:
        peer_prompt: dict[str, object] = {"type": "user", "message": {"content": [
            {"type": "text", "text": "Peer update: the Ghostty check remains blocked."}]}}
        for last, status in (([self.said({"type": "text", "text": "— holding: waiting on the tests"})],
                              "— holding: waiting on the tests"),
                             ([self.said({"type": "text", "text": "— blocked: the user's Ghostty check"}),
                               self.said({"type": "tool_use", "name": "Bash"})], "none on screen"),
                             ([self.said({"type": "text", "text": "— blocked: the user's Ghostty check"}),
                               self.said({"type": "text", "text": "I continue the tests."}),
                               self.said({"type": "text", "text": "—"})], "none on screen"),
                             ([self.said({"type": "text", "text": "— blocked: the user's Ghostty check"}),
                               self.said({"type": "text", "text": "—"}),
                               self.said({"type": "tool_use", "name": "Bash"})], "none on screen"),
                             ([self.said({"type": "text", "text": "— blocked: the user's Ghostty check"}),
                               self.said({"type": "tool_use", "name": "Bash"}),
                               peer_prompt,
                               self.said({"type": "text", "text": "—"})], "none on screen")):
            with self.subTest(status=status):
                self.compacted(*last)
                self.assertEqual(self.tick(START).returncode, 0)
                self.assertEqual(self.tick(START + 300).returncode, 0)
                tell = next(item for item in self.sent() if item["key"].endswith(":tell"))
                self.assertIn(f"Last status: {status}", tell["text"])
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                                 {"bump", "tell"})
                self.send_log.unlink()
                for path in self.state.glob("*.json"):
                    path.unlink()

    def test_holding_statuses_log_each_pass_without_any_delivery(self) -> None:
        for status in ("— holding: no work until the showrunner restarts it on your word",
                       "holding: waiting on your confirmed values"):
            with self.subTest(status=status):
                self.send_log.unlink(missing_ok=True)
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.panes["unit-one"]["pane"] = status + "\n"
                for at in (START, START + 300, START + 600):
                    result = self.tick(at)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if at >= START + 300:
                        self.assertIn("inspected showrunner/unit-one: holding", result.stdout)
                        self.assertEqual(self.sent(), [])
                        path = self.watches["unit-one"].stretch_path
                        self.assertFalse(stall_watch.saved_stretch(path)["tell_sent"])
                        self.assertFalse(stall_watch.saved_stretch(path)["bump_sent"])

    def test_own_work_order_and_own_names_do_not_hold_the_unit(self) -> None:
        self.configure({"showrunner": ["unit-one"], "sdlc": ["style"]})
        for status in ("— holding: … until the Codex worker work order goes out",
                       "holding: waiting on unit-one",
                       "holding: waiting on label-of-unit-one",
                       "holding: waiting on different-remote-name",
                       "— holding: waiting on the style review"):
            with self.subTest(status=status):
                self.send_log.unlink(missing_ok=True)
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.panes["unit-one"]["pane"] = status + "\n"
                for at in (START, START + 300, START + 600):
                    result = self.tick(at)
                    self.assertEqual(result.returncode, 0, result.stderr)
                sent = self.sent()
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in sent}, {"bump", "tell"})
                self.assertEqual(len(sent), 2)
                tell, = [item for item in sent if item["key"].endswith(":tell")]
                self.assertEqual(tell["text"],
                                 f"unit-one idle since 12:00 PDT, nothing running; bumped. Last status: {status}")

    def test_holding_names_a_peer_or_showrunner_anywhere_in_the_line(self) -> None:
        self.configure({"showrunner": ["unit-one", "typesafe"]})
        _ = showrunner_relationship("sdlc")
        row = unit_membership("showrunner", "unit-one")
        peer_row = unit_membership("showrunner", "typesafe")
        cases = (
            ("showrunner", "holding: typesafe must provide the next result", "holding"),
            ("showrunner", "holding: showrunner must provide the next result", "holding"),
            ("natedev", "— holding: waiting on sdlc's message that the result is ready", "waiting"),
        )
        for production, status, decision in cases:
            with self.subTest(production=production, status=status):
                owner = showrunner_relationship(production)
                _ = staff.move(row.id, staff.ReportsTo(owner.id))
                _ = staff.move(peer_row.id, staff.ReportsTo(owner.id))
                self.configure({production: ["unit-one", "typesafe"]})
                _ = self.record_session(os.getpid(), owner.current_session, production)
                self.send_log.unlink(missing_ok=True)
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.panes["unit-one"]["pane"] = status + "\n"
                for at in (START, START + 300, START + 600):
                    result = self.tick(at)
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.sent(), [])
                self.assertIn(f"inspected {production}/unit-one: {decision}", result.stdout)

    def test_own_shown_label_in_a_shared_session_does_not_hold_the_unit(self) -> None:
        self.panes["unit-one"].update(session="agents", label="build-unit",
                                      pane="holding: build-unit must dispatch its order\n")
        for at in (START, START + 300, START + 600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
        self.assertEqual(len(self.sent()), 2)
        calls = fake_unit_host.calls(self.environment)
        self.assertEqual(calls.count(["--session", "agents", "pane", "list"]), 3)

    def test_failed_label_read_keeps_the_permission_prompt_path(self) -> None:
        self.panes["unit-one"].update(session="agents", label="build-unit", agent_status="blocked", pane=EDIT_PROMPT)
        with mock.patch.object(herdr.Herdr, "panes", side_effect=HerdrFailure("fixture labels unavailable")):
            for at in (START, START + 60, START + 120):
                result = self.tick(at)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr.count("stall-watch: agents: pane labels unavailable"), 1, result.stderr)
        self.assertEqual(len(self.prompt_notices()), 1)
        alert, = self.prompt_followups()
        self.assertEqual(alert["text-id"], "stall_watch.permission_prompt")
        self.assertEqual(alert["to"], "user")
        self.assertTrue(self.prompt_state().exists())
        self.assertFalse(any(item["key"].endswith(":bump") for item in self.sent()))

    def test_a_multiword_own_label_does_not_hold_the_unit(self) -> None:
        self.panes["unit-one"].update(session="agents", label="build-unit director",
                                      pane="holding: build-unit director must dispatch its order\n")
        for at in (START, START + 300, START + 600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
        self.assertEqual(len(self.sent()), 2)

    def test_peer_occurrences_count_only_outside_complete_own_names(self) -> None:
        cases = (
            ("build-unit", "build-unit director", "build-unit director", True, True),
            ("build-unit director", "build-unit", "build-unit director", False, False),
            ("build-unit director", "build-unit", "build-unit director needs results from build-unit", True, False),
        )
        for own, peer, text, outside, deferred in cases:
            with self.subTest(own=own, peer=peer, text=text), mock.patch.dict(os.environ, self.environment):
                self.assertEqual(stall_watch.holding_names_outside(f"holding: {text}", (peer,), (own,)), outside)
                self.assertEqual(stall_watch.status_defers_bump(f"holding: waiting on {text}", (peer,), (own,)),
                                 deferred)

    def test_session_addresses_hold_only_when_they_name_another_session(self) -> None:
        for address in ("session:peer-id", f"session:{unit_session('unit-one')}"):
            for suffix in ("", "."):
                with self.subTest(address=address, suffix=suffix):
                    with mock.patch.dict(os.environ, self.environment):
                        self.assertEqual(stall_watch.status_defers_bump(
                            f"holding: waiting on {address}{suffix}", (), (f"session:{unit_session('unit-one')}",)),
                            address == "session:peer-id")
                    self.send_log.unlink(missing_ok=True)
                    self.state.mkdir(exist_ok=True)
                    for path in self.state.glob("*.json"):
                        path.unlink()
                    status = f"holding: results must arrive from {address}{suffix}"
                    self.panes["unit-one"]["pane"] = status + "\n"
                    for at in (START, START + 300, START + 600):
                        result = self.tick(at)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                                     set() if address == "session:peer-id" else {"bump", "tell"})
                    if address != "session:peer-id":
                        tell, = [item for item in self.sent() if item["key"].endswith(":tell")]
                        self.assertIn("nothing running; bumped", tell["text"])

    def test_waiting_on_user_interface_tests_does_not_defer_a_bump(self) -> None:
        self.panes["unit-one"]["pane"] = "holding: waiting on user-interface tests\n"
        for at in (START, START + 300, START + 600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
        self.assertEqual(len(self.sent()), 2)

    def test_hyphenated_own_work_words_do_not_name_an_outside_party(self) -> None:
        self.panes["unit-one"]["pane"] = "holding: my user-interface tests and your-own tests must finish\n"
        for at in (START, START + 300, START + 600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
        self.assertEqual(len(self.sent()), 2)

    def test_a_sentence_period_ends_a_peer_name_but_a_version_suffix_does_not(self) -> None:
        self.configure({"showrunner": ["unit-one", "typesafe"]})
        for suffix in (".", ".v2"):
            with self.subTest(suffix=suffix):
                self.send_log.unlink(missing_ok=True)
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.panes["unit-one"]["pane"] = f"holding: results must arrive from typesafe{suffix}\n"
                for at in (START, START + 300, START + 600):
                    result = self.tick(at)
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                                 set() if suffix == "." else {"bump", "tell"})
                if suffix != ".":
                    tell, = [item for item in self.sent() if item["key"].endswith(":tell")]
                    self.assertIn("nothing running; bumped", tell["text"])

    def test_a_sentence_period_ends_the_units_own_shown_label(self) -> None:
        self.panes["unit-one"].update(session="agents", label="metrics", pane="holding: metrics.\n")
        for at in (START, START + 300, START + 600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
        self.assertEqual(len(self.sent()), 2)

    def test_a_wait_on_own_name_with_extra_spaces_bumps_without_a_status_error(self) -> None:
        self.panes["unit-one"]["pane"] = "holding: waiting on  unit-one\n"
        for at in (START, START + 300, START + 600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("invalid status rule", result.stderr)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
        self.assertEqual(len(self.sent()), 2)

    def test_holding_party_vocabulary_is_read_at_use(self) -> None:
        self.panes["unit-one"]["pane"] = "holding: the executive must approve the result\n"
        self.assertEqual(self.tick(START).returncode, 0)
        _ = self.watch_settings(prompt_grace_seconds="0", holding_party_pattern=r"\bexecutive\b")
        self.assertEqual(self.tick(START + 300).returncode, 0)
        self.assertEqual(self.sent(), [])
        _ = self.watch_settings(prompt_grace_seconds="0")
        self.assertEqual(self.tick(START + 301).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})

    def test_holding_then_idle_in_the_same_stretch_keeps_the_bump_and_tell(self) -> None:
        self.panes["unit-one"]["pane"] = "holding: the executive must approve the result\n"
        _ = self.watch_settings(prompt_grace_seconds="0", holding_party_pattern=r"\bexecutive\b")
        for at in (START, START + 300, START + 600):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
        path = self.watches["unit-one"].stretch_path
        held = stall_watch.saved_stretch(path)
        self.assertEqual(self.sent(), [])
        self.assertFalse(held["tell_sent"])
        self.assertFalse(held["bump_sent"])
        _ = self.watch_settings(prompt_grace_seconds="0")
        result = self.tick(START + 601)
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.sent()
        self.assertEqual(len(sent), 2)
        bump, = [item for item in sent if item["key"].endswith(":bump")]
        tell, = [item for item in sent if item["key"].endswith(":tell")]
        self.assertIn("204f532d-a97a-59e7-a6a7-caf86af3f5ee.sock", bump["to"])
        self.assertIn("54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock", tell["to"])
        self.assertIn("idle since 12:00 PDT, nothing running; bumped", tell["text"])
        idle = stall_watch.saved_stretch(path)
        self.assertEqual(idle["since"], held["since"])
        self.assertTrue(idle["tell_sent"])
        self.assertTrue(idle["bump_sent"])
        self.assertEqual(self.tick(START + 602).returncode, 0)
        self.assertEqual(self.sent(), sent)

    def test_holding_turn_end_without_outside_party_bumps_idle_unit(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        self.panes["unit-one"]["pane"] = "— holding: waiting on a decision\n"
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})

    def test_a_hold_that_states_its_end_is_not_reported_before_then(self) -> None:
        """START is 12:00 PDT: a hold ending at 14:00, dated or not, is reported only once 14:00 is past."""
        for status in ("— holding: waiting on the re-measure window, which ends 2026-10-06 14:00 PDT",
                       "— holding: waiting on the re-measure window until 14:00 PDT",
                       "— holding: waiting on the window until 21:00 UTC",
                       "— holding: waiting on the window, which opens Tue 10-06 14:00 PDT"):
            with self.subTest(status=status):
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                self.panes["unit-one"]["pane"] = status + "\n"
                for at in (START, START + 300, START + 7199):
                    self.assertEqual(self.tick(at).returncode, 0)
                self.assertEqual(self.sent(), [])
                self.assertEqual(self.tick(START + 7199 + 300).returncode, 0)
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})

    def test_a_hold_naming_a_past_time_and_no_outside_party_bumps(self) -> None:
        """A time without a date is the nearest one: 11:50 read at 12:00 is this morning, not tonight."""
        for status in ("— holding: waiting on the tests since 11:50 PDT",
                       "— holding: waiting on the window, which ended 2026-10-06 11:00 PDT",
                       "— holding: waiting on the window, which closed Mon 10-05 14:00 PDT"):
            with self.subTest(status=status):
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                self.panes["unit-one"]["pane"] = status + "\n"
                self.assertEqual(self.tick(START).returncode, 0)
                self.assertEqual(self.tick(START + 300).returncode, 0)
                self.assertEqual(len(self.sent()), 2)
                self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})
                self.assertIn("nothing running; bumped", next(item["text"] for item in self.sent() if item["key"].endswith(":tell")))

    def test_shell_seen_then_gone_starts_a_new_idle_stretch(self) -> None:
        _ = self.tick(START)
        _ = self.tick(START + 300)
        self.assertEqual(len(self.sent()), 2)
        worker = subprocess.Popen(["sleep", str(TEST_HANG_GUARD_SECS)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(worker)
        self.process_rows.append(f"{worker.pid} {self.children[0].pid} bash running")
        _ = self.tick(START + 360)
        _ = self.process_rows.pop()
        _ = self.tick(START + 361)
        self.assertEqual(len(self.sent()), 2)
        _ = self.tick(START + 661)
        self.assertEqual(len(self.sent()), 4)

    def test_done_turn_end_is_never_stalled(self) -> None:
        self.panes["unit-one"]["pane"] = "— done: phase finished\n"
        _ = self.tick(START)
        _ = self.tick(START + 3600)
        self.assertEqual(self.sent(), [])

    def test_done_turn_ends_reported_stretch_before_same_holding_status_returns(self) -> None:
        self.panes["unit-one"]["pane"] = "— holding: waiting on the tests\n"
        _ = self.tick(START)
        _ = self.tick(START + 300)
        first_keys = {item["key"] for item in self.sent()}
        self.panes["unit-one"]["pane"] = "— done: phase finished\n"
        _ = self.tick(START + 360)
        self.panes["unit-one"]["pane"] = "— holding: waiting on the tests\n"
        _ = self.tick(START + 361)
        _ = self.tick(START + 661)
        second = self.sent()[2:]
        self.assertEqual(len(second), 2)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in second}, {"bump", "tell"})
        self.assertTrue(first_keys.isdisjoint({item["key"] for item in second}))

    def test_failed_showrunner_delivery_retries_without_rebumping_unit(self) -> None:
        _ = self.fail_file.write_text(json.dumps([f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}"]))
        _ = self.tick(START)
        _ = self.tick(START + 300)
        first = self.sent()
        self.assertEqual(len(first), 2)
        _ = self.tick(START + 360)
        retry = self.sent()[2:]
        self.assertEqual(len(retry), 1)
        self.assertTrue(retry[0]["key"].endswith(":tell"))
        self.assertEqual(retry[0]["key"], next(item["key"] for item in first if item["key"].endswith(":tell")))
        _ = self.fail_file.write_text("[]")
        _ = self.tick(START + 420)
        _ = self.tick(START + 480)
        self.assertEqual(len(self.sent()), 4)

    def test_running_shell_or_worker_launcher_prevents_a_bump(self) -> None:
        director = next(row for row in staff.find(kind="unit")
                        if row.current_session == unit_session("unit-one"))
        pane_worker = fixture_member("worker", name="test", parent=director.id)
        live_records: dict[tuple[Path, str], list[detached_runs.RunRecord]] = {}

        def read_live_runs(runs: Path, worker: str) -> list[detached_runs.RunRecord]:
            return live_records.get((runs, worker), [])

        cases = (
            ("shell", "bash", herdr.AgentStatus.IDLE, "", False, "running"),
            ("launcher", "bash scripts/agents/agent_bg.sh", herdr.AgentStatus.IDLE, "", False, "running"),
            ("busy pane", "", herdr.AgentStatus.WORKING, "", False, "running"),
            ("idle pane", "", herdr.AgentStatus.IDLE, "", False, "idle bump requested"),
            ("live detached run", "", herdr.AgentStatus.IDLE, "running", False, "running"),
            ("finished detached run", "", herdr.AgentStatus.IDLE, "finished", False, "idle bump requested"),
            ("live detached run outside the run roots", "", herdr.AgentStatus.IDLE, "running", True, "running"),
        )
        for label, command, status, detached_status, outside_roots, decision in cases:
            with (self.subTest(case=label),
                  mock.patch.object(detached_runs, "live_runs", side_effect=read_live_runs)):
                live_records.clear()
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                with mock.patch.object(worker_panes, "observe_parents", return_value={}):
                    initial = self.tick(START)
                self.assertEqual(initial.returncode, 0, initial.stderr)
                self.assertIn("inspected showrunner/unit-one: fresh", initial.stdout)
                held_folder = self.root / "held" / "unit-one"
                if outside_roots:
                    outside_folder = self.root / "outside" / "unit-one"
                    outside_folder.parent.mkdir()
                    _ = held_folder.rename(outside_folder)
                    held_folder = outside_folder
                    self.unheld_runs.add("unit-one")
                    _ = staff.set_run(director.id, staff.OpenRun(held_folder))
                detached_folder = held_folder / "detached_runs"
                if detached_status:
                    detached_folder.mkdir(parents=True, exist_ok=True)
                    record: detached_runs.RunRecord = {
                        "run_id": "worker-test", "status": "running" if detached_status == "running" else "finished",
                        "session_dir": str(detached_folder.parent), "worker": pane_worker.name,
                        "staff_id": pane_worker.id, "label": "worker tests", "command": ["test"],
                        "log": str(detached_folder / "worker-test.log"), "started_at": START,
                        "pid": 900001, "director_dir": str(self.root), "done_message_file": "",
                    }
                    _ = (detached_folder / "worker-test.json").write_text(json.dumps(record))
                    live_records[(detached_folder, pane_worker.name)] = [record] if detached_status == "running" else []
                if command:
                    worker = subprocess.Popen(["sleep", str(TEST_HANG_GUARD_SECS)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.children.append(worker)
                    claude_pid = self.children[0].pid
                    self.process_rows.append(f"{worker.pid} {claude_pid} {command} running")
                observed: dict[staff.StaffId, list[worker_panes.WorkerPane]] = {
                    director.id: [worker_panes.OpenWorker(pane_worker, "w2:p1", "worker-terminal", status)]}
                with mock.patch.object(worker_panes, "observe_parents", return_value=observed):
                    result = self.tick(START + 300)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"inspected showrunner/unit-one: {decision}", result.stdout)
                if command:
                    _ = self.process_rows.pop()
                if decision == "running":
                    self.assertEqual(self.sent(), [])
                    saved = cast(stall_watch.Stretch, json.loads(self.watches["unit-one"].stretch_path.read_text()))
                    self.assertEqual(saved["since"], START + 300)
                    live_records.clear()
                    with mock.patch.object(worker_panes, "observe_parents", return_value={}):
                        fresh = self.tick(START + 301)
                    self.assertEqual(fresh.returncode, 0, fresh.stderr)
                    self.assertIn("inspected showrunner/unit-one: fresh", fresh.stdout)
                    self.assertEqual(self.sent(), [])
                else:
                    sent = self.sent()
                    self.assertEqual(len(sent), 2)
                    self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in sent}, {"bump", "tell"})

    def test_work_the_unit_started_outside_its_tree_prevents_a_bump(self) -> None:
        """A build a unit backgrounded is reparented away from its Claude but keeps the unit's marks."""
        for unit, bumps in (("unit-one", 0), ("unit-two", 2)):
            with self.subTest(unit=unit):
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                _ = self.tick(START)
                marks = {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": unit_session(unit)}
                worker = subprocess.Popen(MARKED_SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          env={**os.environ, **marks})
                self.children.append(worker)
                self.process_rows.append(f"{worker.pid} 1 sleep 60")
                _ = self.tick(START + 300)
                self.assertEqual(len(self.sent()), bumps)
                _ = self.process_rows.pop()

    def test_detached_work_with_a_native_session_prevents_a_bump(self) -> None:
        worker = subprocess.Popen(MARKED_SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env={**self.environment, "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": unit_session("unit-one")})
        self.children.append(worker)
        self.process_rows.append(f"{worker.pid} 1 sleep 60")
        for at in (START, START + 300):
            result = self.tick(at)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sent(), [])
        _ = self.process_rows.pop()
        result = self.tick(START + 600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})

    def test_detached_processes_use_native_sessions_without_production_name_resolution(self) -> None:
        from scripts.production import stall_watch
        from scripts.production import showrunners

        rows = [stall_watch.Process(10, 1, "claude"), stall_watch.Process(20, 1, "build"),
                stall_watch.Process(21, 1, "build")]
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
              mock.patch.object(stall_watch, "process_session", return_value="native-unit-one"),
              mock.patch.object(showrunners, "_source", wraps=getattr(showrunners, "_source")) as reads):
            self.assertFalse(stall_watch.work_running(10, rows, "unrelated"))
            self.assertEqual(reads.call_count, 0)
            self.assertTrue(stall_watch.work_running(10, rows, "native-unit-one"))
            self.assertEqual(reads.call_count, 0)

    def test_recent_transcript_or_subagent_activity_prevents_a_bump(self) -> None:
        _ = self.tick(START)
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        os.utime(transcript, (START + 100, START + 100))
        _ = self.tick(START + 300)
        self.assertEqual(self.sent(), [])
        os.utime(transcript, (START - 3600, START - 3600))
        subagent = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee" / "subagents" / "worker.jsonl"
        subagent.parent.mkdir(parents=True)
        _ = subagent.write_text("{}\n")
        os.utime(subagent, (START + 100, START + 100))
        _ = self.tick(START + 301)
        self.assertEqual(self.sent(), [])
        _ = self.tick(START + 401)
        self.assertEqual(len(self.sent()), 2)

    def test_transcript_activity_moves_unreported_idle_start_and_delivery_key(self) -> None:
        _ = self.tick(START)
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        os.utime(transcript, (START + 100, START + 100))
        _ = self.tick(START + 300)
        self.assertEqual(self.sent(), [])
        _ = self.tick(START + 399)
        self.assertEqual(self.sent(), [])
        _ = self.tick(START + 400)
        sent = self.sent()
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(f":{START + 100}:" in item["key"] for item in sent))
        self.assertTrue(all("idle since 12:01 PDT" in item["text"] for item in sent))

    def test_transcript_reply_keeps_reported_stretch_and_failed_delivery_key(self) -> None:
        _ = self.fail_file.write_text(json.dumps([f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}"]))
        _ = self.tick(START)
        _ = self.tick(START + 300)
        first = self.sent()
        transcript = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl"
        os.utime(transcript, (START + 350, START + 350))
        _ = self.tick(START + 360)
        retry = self.sent()[2:]
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["key"], next(item["key"] for item in first if item["key"].endswith(":tell")))

    def test_stale_worker_status_does_not_count_as_running_work(self) -> None:
        status = self.root / "run" / "status"
        status.parent.mkdir()
        _ = status.write_text("implementing\n")
        _ = self.tick(START)
        _ = self.tick(START + 300)
        self.assertEqual(len(self.sent()), 2)

    def test_defunct_worker_process_does_not_count_as_running_work(self) -> None:
        _ = self.tick(START)
        claude_pid = self.children[0].pid
        self.process_rows.append(f"999999 {claude_pid} bash scripts/agents/agent_bg.sh <defunct>")
        _ = self.tick(START + 300)
        self.assertEqual(len(self.sent()), 2)

    def test_subagent_activity_counts_without_a_parent_transcript(self) -> None:
        _ = self.tick(START)
        (self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee.jsonl").unlink()
        subagent = self.projects / "project" / "204f532d-a97a-59e7-a6a7-caf86af3f5ee" / "subagents" / "worker.jsonl"
        subagent.parent.mkdir(parents=True)
        _ = subagent.write_text("{}\n")
        os.utime(subagent, (START + 100, START + 100))
        _ = self.tick(START + 300)
        self.assertEqual(self.sent(), [])

    def test_added_config_unit_is_watched_and_removed_unit_is_dropped(self) -> None:
        _ = self.tick(START)
        _ = self.unit("unit-two")
        self.configure({"showrunner": ["unit-one", "unit-two"]})
        _ = self.tick(START + 300)
        self.assertEqual(len(self.sent()), 2)
        _ = self.tick(START + 600)
        self.assertEqual(len(self.sent()), 4)
        self.assertEqual({item["key"] for item in self.sent()[2:]},
                         {f"stall-watch:{unit_session('unit-two')}:{START + 300}:bump",
                          f"stall-watch:{unit_session('unit-two')}:{START + 300}:tell"})
        self.configure({"showrunner": ["unit-one"]})
        self.panes["unit-two"]["pane"] = "— gate: changed\n"
        _ = self.tick(START + 901)
        _ = self.tick(START + 1201)
        self.assertEqual(len(self.sent()), 4)

    def test_standby_unit_is_skipped_until_its_mark_says_running(self) -> None:
        self.states["unit-one"] = "standing-by"
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertFalse(self.watches["unit-one"].stretch_path.exists())
        self.states["unit-one"] = "running"
        self.assertEqual(self.tick(START + 601).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tick(START + 901).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})

    def test_finished_registry_unit_has_no_notice_or_stretch(self) -> None:
        self.configure_registered({"showrunner": [("unit-one", "run-finished")]})
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertFalse(self.watches["unit-one"].stretch_path.exists())

    def test_a_retired_unit_is_skipped(self) -> None:
        self.retired.add("unit-one")
        self.configure({"showrunner": ["unit-one"]})
        first = self.tick(START)
        second = self.tick(START + 600)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.sent(), [])
        self.assertFalse(self.watches["unit-one"].stretch_path.exists())

    def test_a_unit_named_review_unit_is_bumped(self) -> None:
        self.configure_registered({"showrunner": [("review", "running")]})
        _ = self.unit("review")

        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        sent = self.sent()
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in sent}, {"bump", "tell"})
        self.assertTrue(all(item["key"].startswith(f"stall-watch:{unit_session('review')}:") for item in sent), sent)

    def test_finished_units_are_skipped_while_a_running_unit_is_bumped(self) -> None:
        self.configure_registered({"showrunner": [
            ("build-report", "run-finished"),
            ("hook", "running"),
            ("notifier", "run-finished"),
        ]})
        for name in ("build-report", "hook", "notifier"):
            _ = self.unit(name)

        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        sent = self.sent()
        self.assertEqual(len(sent), 2)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in sent}, {"bump", "tell"})
        self.assertTrue(all(item["key"].startswith(f"stall-watch:{unit_session('hook')}:") for item in sent))
        for name in ("build-report", "notifier"):
            self.assertFalse(self.watches[name].stretch_path.exists())

    def test_a_marked_session_of_a_unit_the_doc_does_not_list_is_not_watched(self) -> None:
        _ = self.panes.pop("unit-one")
        _ = self.unit("unit-one-extra")
        _ = self.tick(START)
        _ = self.tick(START + 600)
        self.assertEqual(self.sent(), [])

    def test_configured_showrunner_without_live_session_is_not_watched(self) -> None:
        showrunner = self.sessions / f"{os.getpid()}.json"
        record = cast(dict[str, object], json.loads(showrunner.read_text()))
        record["running"] = False
        _ = showrunner.write_text(json.dumps(record))
        _ = self.tick(START)
        _ = self.tick(START + 600)
        self.assertEqual(self.sent(), [])

    def test_unreadable_session_records_are_reported_and_the_unit_is_skipped(self) -> None:
        # The unit's own record cannot be read, which says nothing about whether its Claude runs.
        _ = (self.sessions / f"{self.children[0].pid}.json").write_text(
            json.dumps({"pid": "unreadable", "sessionId": "", "name": "", "running": False}))
        result = self.tick(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"stall-watch: unit-one: the session record of pid {self.children[0].pid} could not be read", result.stderr)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual(self.sent(), [])

    def test_a_showrunner_lookup_that_cannot_say_sends_nothing(self) -> None:
        self.environment["STALL_TEST_SESSION_ERRORS"] = json.dumps(["session:54c9fdb8-6dfc-55dd-8242-a523c2e9f81f"])
        # The script's exception handler, outside main(), formats this failure.
        _ = self.tick(START, subprocess_cli=True)
        result = self.tick(START + 600, subprocess_cli=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot tell whether session:54c9fdb8-6dfc-55dd-8242-a523c2e9f81f is running", result.stderr)
        self.assertEqual(self.sent(), [])

    def test_a_renamed_showrunner_is_still_told_of_its_stalled_unit(self) -> None:
        _ = self.record_session(os.getpid(), "54c9fdb8-6dfc-55dd-8242-a523c2e9f81f", "renamed-since-it-registered")
        _ = self.tick(START)
        _ = self.tick(START + 600)
        self.assertEqual([one["to"] for one in self.sent() if "idle since" in one["text"] and "bumped" in one["text"]],
                         [f"uds:{self.root / '54c9fdb8-6dfc-55dd-8242-a523c2e9f81f.sock'}"])

    def test_locked_tick_exits_without_sending(self) -> None:
        _ = self.tick(START)
        with (self.state / "lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.tick(START + 300)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.sent(), [])

    def test_failed_process_snapshot_keeps_pane_state_and_sends_pending_work_by_deadline(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        self.assertEqual(self.tick(START).returncode, 0)
        saved = self.prompt_state().read_bytes()
        idle = next(path for path in self.state.glob("*.json") if path.suffixes == [".json"])
        saved_idle = idle.read_bytes()
        old = self.state / "old.prompt.json"
        stall_watch.save_prompt(old, stall_watch.PromptWait(session_id="stopped", created=START,
            key="old-alert", since=START, alerted=True, tool="", retiring=True))
        _ = self.watch_settings(prompt_grace_seconds="0", delivery_reserve_seconds="0.6",
                            sender_cleanup_seconds="0.2", sender_batch_seconds="1")
        relay = self.state / "pending.json"
        original = stall_watch.tick
        for fault in ("timeout", "exit"):
            with self.subTest(fault=fault):
                executable(self.bin / "ps", "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n"
                           if fault == "timeout" else "#!/bin/sh\nexit 7\n")
                began = time.monotonic()
                deadline = began + 1.5

                def bounded_tick(now: float, *, deadline: float) -> int:
                    return original(now, deadline=min(deadline, began + 1.5))

                stall_watch.save_stretch(relay, stall_watch.Stretch(pane_hash="pending", since=START,
                    bump_sent=False, tell_sent=False, reported_status=""))
                before = len(self.sent())
                pending = [stall_watch.Delivery(old, "retire", [sys.executable,
                    str(self.bin / "send.py"), "retire", "--key", "old-alert"]),
                    stall_watch.Delivery(relay, "bump", [sys.executable, str(self.bin / "send.py"),
                        "--key", "pending-relay", "--to", "fake", "--text", "pending"])]
                # The pending retire is independent of process liveness; inspection is not.
                with (mock.patch.object(stall_watch, "tick", side_effect=bounded_tick),
                      mock.patch.object(stall_watch, "reconcile_prompts", return_value=pending)):
                    result = self.tick(START + 300)
                self.assertLessEqual(time.monotonic(), deadline)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.count("stall-watch: process snapshot:"), 1, result.stderr)
                self.assertEqual(self.prompt_state().read_bytes(), saved)
                self.assertEqual(idle.read_bytes(), saved_idle)
                self.assertFalse(old.exists())
                deliveries = {item["key"]: item for item in self.sent()[before:]}
                self.assertEqual(set(deliveries), {"old-alert", "pending-relay"})
                self.assertEqual(deliveries["old-alert"]["command"], "retire")
                self.assertTrue(stall_watch.saved_stretch(relay)["bump_sent"])
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(stall_watch, "PS", str(self.bin / "missing-ps"))):
            snapshot = stall_watch.processes(deadline=time.monotonic() + 1)
        self.assertIsInstance(snapshot, stall_watch.ProcessSnapshotFailure)

    def test_failed_commands_report_both_output_streams(self) -> None:
        script = self.root / "failed-command"
        executable(script, "#!/usr/bin/env python3\nimport sys\n"
                   + "print('output evidence')\nprint('error evidence', file=sys.stderr)\nsys.exit(7)\n")
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(stall_watch, "PS", str(script))):
            snapshot = stall_watch.processes(deadline=time.monotonic() + 5)
        self.assertIsInstance(snapshot, stall_watch.ProcessSnapshotFailure)
        if not isinstance(snapshot, stall_watch.ProcessSnapshotFailure):
            self.fail("a failed command must not become process rows")
        self.assertIn("ps exited 7:", snapshot.reason)
        self.assertIn("stderr: error evidence", snapshot.reason)
        self.assertIn("stdout: output evidence", snapshot.reason)
        for kind in ("bump", "retire"):
            with self.subTest(kind=kind):
                state = self.root / "failed.prompt.json"
                stall_watch.save_prompt(state, stall_watch.PromptWait(session_id="stopped", created=START,
                    key="failed-alert", since=START, alerted=True, tool="", retiring=True))
                saved = state.read_bytes()
                errors = io.StringIO()
                with (mock.patch.dict(os.environ, self.environment), redirect_stderr(errors),
                      mock.patch.object(stall_watch, "STATE_DIR", self.root)):
                    stall_watch.send_all([stall_watch.Delivery(state, kind, [str(script)])], START,
                                         deadline=time.monotonic() + 5)
                self.assertIn(f"stall-watch: {kind}: stderr: error evidence", errors.getvalue())
                self.assertIn("stdout: output evidence", errors.getvalue())
                self.assertEqual(state.read_bytes(), saved)

    def test_sender_timeout_kills_a_grandchild_holding_the_output_pipe(self) -> None:
        script = self.root / "relay.py"
        pid_file = self.root / "grandchild.pid"
        _ = script.write_text("import os, signal, subprocess, sys, time\n"
            + "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            + "open(sys.argv[1], 'w').write(str(child.pid))\n"
            + "def reap(sig, frame):\n    child.wait(); raise SystemExit(0)\n"
            + "signal.signal(signal.SIGCHLD, reap)\n"
            + "time.sleep(60)\n")
        state = self.root / "retiring.prompt.json"
        _ = state.write_text("saved retire state\n")
        _ = self.watch_settings(sender_batch_seconds="0.4", sender_cleanup_seconds="0.4")
        errors = io.StringIO()
        processes: list[subprocess.Popen[str]] = []
        original = subprocess.Popen

        def start(command: list[str], **keywords: object) -> subprocess.Popen[str]:
            # Keep the actual process group and inherited pipes; only record the owned pid.
            child = original(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=bool(keywords["start_new_session"]))
            processes.append(child)
            return child

        began = time.monotonic()
        deadline = began + 1.5
        try:
            with (mock.patch.dict(os.environ, self.environment), redirect_stderr(errors),
                  mock.patch.object(subprocess, "Popen", side_effect=start),
                  mock.patch.object(stall_watch, "retry_cases")):
                stall_watch.send_all([stall_watch.Delivery(state, "retire",
                    [sys.executable, str(script), str(pid_file)])], START, deadline=deadline)
            self.assertLessEqual(time.monotonic(), deadline)
            self.assertEqual(errors.getvalue(), "stall-watch: retire: send timeout\n")
            self.assertEqual(state.read_text(), "saved retire state\n")
            self.assertTrue(pid_file.exists(), "the relay must start its pipe-holding descendant")
            pid = int(pid_file.read_text())
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                pass
            else:
                try:
                    _ = select.select([descriptor], [], [], max(0, deadline - time.monotonic()))
                finally:
                    os.close(descriptor)
            # A zombie is dead, awaiting the machine's init reaper, and cannot hold pipes.
            status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertTrue(not status.stdout.strip() or status.stdout.strip().startswith("Z"), status.stdout)
            self.assertIsNotNone(processes[0].returncode)
        finally:
            for child in processes:
                try:
                    send_process_group(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_invalid_sender_limits_use_packaged_limits_and_deliver_pending_work(self) -> None:
        for key in ("sender_cleanup_seconds", "sender_batch_seconds"):
            for value in ("oops", "nan", "0"):
                with self.subTest(key=key, value=value):
                    _ = self.watch_settings(prompt_grace_seconds="120", delivery_reserve_seconds="0.5",
                                            **{key: value})
                    retiring = self.state / "old.prompt.json"
                    stall_watch.save_prompt(retiring, stall_watch.PromptWait(session_id="stopped",
                        created=START, key="old-alert", since=START, alerted=True, tool="", retiring=True))
                    relay = self.state / "pending.json"
                    stall_watch.save_stretch(relay, stall_watch.Stretch(pane_hash="pending", since=START,
                        bump_sent=False, tell_sent=False, reported_status=""))
                    pending = [stall_watch.Delivery(retiring, "retire", [sys.executable,
                        str(self.bin / "send.py"), "retire", "--key", "old-alert"]),
                        stall_watch.Delivery(relay, "bump", [sys.executable, str(self.bin / "send.py"),
                            "--key", "pending-relay", "--to", "fake", "--text", "pending"])]
                    original = stall_watch.tick
                    began = time.monotonic()
                    deadline = began + 5

                    def bounded_tick(now: float, *, deadline: float) -> int:
                        return original(now, deadline=min(deadline, began + 5))

                    before = len(self.sent())
                    with (mock.patch.object(stall_watch, "tick", side_effect=bounded_tick),
                          mock.patch.object(stall_watch, "reconcile_prompts", return_value=pending),
                          mock.patch.object(stall_watch, "sender_configuration",
                                            wraps=stall_watch.sender_configuration) as configuration):
                        result = self.tick(START)
                    self.assertLessEqual(time.monotonic(), deadline)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(configuration.call_count, 1)
                    self.assertEqual(result.stderr.count("stall-watch: sender config:"), 1, result.stderr)
                    self.assertIn(key, result.stderr)
                    self.assertNotIn("process snapshot:", result.stderr)
                    self.assertFalse(retiring.exists())
                    self.assertTrue(stall_watch.saved_stretch(relay)["bump_sent"])
                    self.assertEqual({item["key"] for item in self.sent()[before:]},
                                     {"old-alert", "pending-relay"})

    def test_sender_batch_and_cleanup_budgets_are_reread_from_config(self) -> None:
        script = self.root / "short-relay.py"
        _ = script.write_text("import time\ntime.sleep(0.2)\n")
        for batch, cleanup, delivered in (("0.1", "0.1", False), ("1", "0.1", True),
                                          ("1", "0.75", False)):
            with self.subTest(batch=batch, cleanup=cleanup):
                _ = self.watch_settings(sender_batch_seconds=batch, sender_cleanup_seconds=cleanup)
                state = self.root / "retire-me.prompt.json"
                stall_watch.save_prompt(state, stall_watch.PromptWait(session_id="retired", created=START,
                    key="old", since=START, alerted=True, tool="", retiring=True))
                errors = io.StringIO()
                began = time.monotonic()
                deadline = began + 0.8
                with (mock.patch.dict(os.environ, self.environment), redirect_stderr(errors),
                      mock.patch.object(stall_watch, "retry_cases")):
                    stall_watch.send_all([stall_watch.Delivery(state, "retire",
                        [sys.executable, str(script)])], START, deadline=deadline)
                self.assertLessEqual(time.monotonic(), deadline)
                self.assertEqual(state.exists(), not delivered)
                self.assertEqual(errors.getvalue(), "" if delivered else "stall-watch: retire: send timeout\n")

    def test_three_stalled_units_start_slow_sends_together(self) -> None:
        _ = self.unit("unit-two")
        _ = self.unit("unit-three")
        self.configure({"showrunner": ["unit-one", "unit-two", "unit-three"]})
        _ = self.tick(START)
        result = self.tick(START + 300, together=6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "", "every sender must complete the concurrency barrier")
        self.assertEqual(len(self.sent()), 6)


if __name__ == "__main__":
    _ = unittest.main()
