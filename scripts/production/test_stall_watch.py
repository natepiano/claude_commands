"""Stall watcher tests with isolated notifier, sessions, processes and relays."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import time
import unittest
from types import ModuleType
from unittest import mock
from datetime import datetime
from pathlib import Path

from typing import cast, override
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.test_agents_registry import write_registry
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdr"))

from scripts.lib.socket_test_support import short_socket_directory
import fake_unit_host
from fake_unit_host import HostedUnit
import fake_herdr
import stall_watch
import context_usage
import rca
from cli_fixture import invoke_cli


SCRIPT = Path(__file__).with_name("stall_watch.py")
START = int(datetime(2026, 10, 6, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())
PANE = "— holding: waiting on a decision\n"
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
PROMPT_KEY = "stall-watch:prompt:showrunner:unit-one"
RETIRE = {"command": "retire", "to": "user", "from": "stall-watch", "key": PROMPT_KEY}


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
        # A unit's Plan cell and the state mark on its herdr pane, where a test sets one.
        self.plans: dict[str, str] = {}
        self.states: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(short_socket_directory()))
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
        self.config = self.root / "showrunners.json"
        self.configure({"showrunner": ["unit-one"]})
        self.addCleanup(self.close_processes)
        # Runs before the temporary folder is removed, so no detached owner message writes into it after.
        self.addCleanup(self.owner_messages)
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
            "UNIT_HISTORY_DIR": str(self.root / "history"),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "SHOWRUNNERS_SESSIONS": str(self.sessions_script),
            "STALL_WATCH_STATE_DIR": str(self.state),
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
        _ = self.record_session(os.getpid(), "show-id", "showrunner")
        _ = self.record_session(os.getpid() + 1_000_000, "fault-id", "natedev")
        _ = self.unit("unit-one")

    def configure(self, runners: dict[str, list[str]], *, stall_minutes: int = 5,
                  review_pool: bool = False) -> None:
        """Give each showrunner an update timer and a production doc that lists its units. The slug is its name."""
        _ = self.config.write_text(json.dumps({
            "threshold_percent": 1, "repeat_minutes": 30, "stall_minutes": stall_minutes,
            "faults_to": "natedev", "always": ["natedev"],
        }))
        for session, units in runners.items():
            # A showrunner is recorded by its update timer alone, addressed to its Claude session id.
            self.production(session, tuple(units), doc=self.root / f"{session}-production.md")
            rows = [f"| {unit} | {self.plans.get(unit, '`docs/plan.md`')} | /tmp/no-worktree-of-{unit} | {unit} | — | — |"
                    for unit in units]
            _ = (self.root / f"{session}-production.md").write_text("\n".join((
                "- **User zone:** America/Los_Angeles",
                *(["- **Review pool:** natedev"] if review_pool else []), "",
                "## Units", "", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                "| --- | --- | --- | --- | --- | --- |", *rows, "")))

    def configure_registered(self, runners: dict[str, list[tuple[str, str]]],
                             *, stall_minutes: int = 5, review_pool: bool = False) -> None:
        """As `configure`, and each unit's run records say the state given."""
        for units in runners.values():
            self.states.update(dict(units))
        self.configure({session: [unit for unit, _ in units] for session, units in runners.items()},
                       stall_minutes=stall_minutes, review_pool=review_pool)

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

    def production(self, slug: str, units: tuple[str, ...], *, target: str = "session:show-id",
                   doc: Path | None = None) -> None:
        directory = self.notifier / f"showrunner-{slug}"
        directory.mkdir(exist_ok=True)
        prompt = directory / "prompt"
        _ = prompt.write_text("Run `zsh ~/.claude/scripts/production/unit_status.sh /tmp/status "
                              + f"America/Los_Angeles {' '.join(units)} | cut -c1-400`.\n")
        check = f"CHECK=zsh /scripts/production/production_check.sh {doc}\n" if doc is not None else ""
        _ = (directory / "conf").write_text(f"TARGET={target}\nPROMPT_FILE={prompt}\n{check}")

    def production_plan(self, plan: str) -> Path:
        """Give `unit-one` this Plan cell in the showrunner's production doc."""
        self.plans["unit-one"] = plan
        self.configure({"showrunner": ["unit-one"]})
        return self.root / "showrunner-production.md"

    def unit(self, name: str) -> tuple[int, Path]:
        child = subprocess.Popen(["sleep", str(TEST_HANG_GUARD_SECS)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        pane_pid = 10_000 + len(self.panes)
        self.process_rows += [f"{pane_pid} 1 herdr pane", f"{child.pid} {pane_pid} claude --remote-control other-name"]
        pane_id = len(self.panes) + 1
        self.panes[name] = {"pane": PANE, "id": pane_id, "pid": child.pid, "slug": "showrunner", "label": f"label-of-{name}"}
        socket_path = self.record_session(child.pid, f"sid-{name}", "different-remote-name")
        # The lookup reads the record itself: it needs the pane the session runs in, and a real socket.
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(socket_path))
        self.addCleanup(held.close)
        record_path = self.sessions / f"{child.pid}.json"
        record = cast(dict[str, object], json.loads(record_path.read_text()))
        _ = record_path.write_text(json.dumps({**record, "tmux": ""}))
        transcript = self.projects / "project" / f"sid-{name}.jsonl"
        transcript.parent.mkdir(exist_ok=True)
        _ = transcript.write_text("{}\n")
        os.utime(transcript, (START - 3600, START - 3600))
        return child.pid, socket_path

    def tick(self, at: int, *, together: int = 0, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        _ = self.ps_file.write_text("\n".join(self.process_rows) + "\n")
        units = [
            HostedUnit(str(data["label"]), f"w{data['id']}:p1",
                       {"SHOWRUNNER_UNIT": str(data["slug"]), "SHOWRUNNER_UNIT_ID": name}, str(data["pane"]),
                       str(data.get("agent_status", "idle")))
            for name, data in self.panes.items()]
        fake_unit_host.write(self.environment, units)
        for unit, data in zip(units, self.panes.values()):
            fake_unit_host.place_process(self.environment, int(data["pid"]), unit.pane)
        # A unit's run state is read from the record of the newest /unit:direct run in its worktree.
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        for record in runs.iterdir():
            record.unlink()
        for name in self.panes:
            state = self.states.get(name, "") or "running"
            if state == "standing-by":
                continue
            events = [{"event_type": "run_started", "run_started_at": 1.0,
                       "working_dir": str(Path(f"/tmp/no-worktree-of-{name}").resolve())},
                      *([{"event_type": "run_finished"}] if state == "run-finished" else [])]
            _ = (runs / f"{name}.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
        environment = {**self.environment, "STALL_WATCH_NOW_EPOCH": str(at),
                       "STALL_TEST_SEND_TOGETHER": str(together)}
        if subprocess_cli:
            return subprocess.run([sys.executable, str(SCRIPT)], env=environment,
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
            if command[:2] == [sys.executable, str(self.sessions_script)]:
                return invoke_cli(session_module, sessions_main, command[2:],
                                  dict(os.environ) if env is None else env, Path.cwd())
            return real_run(command, capture_output=capture_output, text=text, check=check, timeout=timeout, env=env)

        with (mock.patch.object(subprocess, "run", run),
              mock.patch.multiple(stall_watch, STATE_DIR=self.state, SESSIONS_DIR=self.sessions,
                                  PROJECTS_DIR=self.projects, SESSIONS=self.sessions_script,
                                  SEND=self.bin / "send.py", PS="ps"),
              mock.patch.multiple(rca, STATE=self.root / "state/message/rca",
                                  SEND=self.bin / "rca-send.py", PROJECTS=self.root / ".claude/projects")):
            return invoke_cli(stall_watch, stall_watch.main, [], environment, self.root, production_state=True)

    def owner_messages(self) -> list[dict[str, str]]:
        """rca.py's owner messages, once each one it started has landed.

        rca.py starts each owner message detached and writes "<time> <key>" to owner.log first, so the
        stub's log is read after it has one line per started key, within the hang guard.
        """
        started = self.root / "state/message/rca/owner.log"
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while True:
            keys = [line for line in started.read_text().splitlines()
                    if line.partition(" ")[2].startswith("rca-")] if started.exists() else []
            lines = self.rca_log.read_text().splitlines() if self.rca_log.exists() else []
            if len(lines) >= len(keys) or time.monotonic() >= deadline:
                return [cast(dict[str, str], json.loads(line)) for line in lines]
            time.sleep(0.01)

    def sent(self) -> list[dict[str, str]]:
        if not self.send_log.exists():
            return []
        return [json.loads(line) for line in self.send_log.read_text().splitlines()]

    def test_one_tick_reads_run_records_once_across_productions_and_units(self) -> None:
        import stall_watch
        from scripts.production import showrunners
        import unit_lookup as lookup
        runners: list[showrunners.Showrunner] = []
        for slug in ("first", "second"):
            doc = self.root / f"{slug}-production.md"
            _ = doc.write_text("\n".join(("# Production", f"- **Production slug:** {slug}", "## Units",
                               "| Unit | Plan | Worktree |", "| --- | --- | --- |",
                               f"| alpha | docs/alpha.md | {self.root / 'alpha'} |",
                               f"| beta | docs/beta.md | {self.root / 'beta'} |", "")))
            runners.append(showrunners.Showrunner(session=slug, socket="/fixture.sock",
                            slug=slug, zone="UTC", doc=str(doc)))
        marked = {name: lookup.MarkedUnit(name, lookup.ClaudeNotRunning(), lookup.Place("fixture", name))
                  for name in ("alpha", "beta")}
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(stall_watch, "processes", return_value=[]),
              mock.patch.object(showrunners, "load_settings", return_value={}),
              mock.patch.object(showrunners, "registered_showrunners", return_value=runners),
              mock.patch.object(lookup, "marked_units", return_value=marked),
              mock.patch.object(lookup, "read_run_states", wraps=lookup.read_run_states) as reads,
              mock.patch.object(stall_watch, "forget", return_value=None) as forget,
              mock.patch.object(stall_watch, "send_all") as send):
            stall_watch.tick(START)
            reads.assert_called_once_with()
            self.assertEqual(forget.call_count, 4)
            send.assert_called_once_with([], START)

    def test_a_herdr_unit_is_watched_through_a_joined_400_line_read(self) -> None:
        self.panes["unit-one"]["label"] = "native"
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
        self.panes["unit-one"]["label"] = "renamed-by-the-user"
        record_path = self.sessions / f"{self.children[0].pid}.json"
        record = cast(dict[str, object], json.loads(record_path.read_text()))
        _ = record_path.write_text(json.dumps({**record, "name": "renamed-by-the-user"}))
        self.assertEqual(self.tick(START + 301).returncode, 0)
        self.assertEqual(len(self.sent()), 2)

    def test_stalled_unit_waits_at_gate_then_bumps_after_holding_returns(self) -> None:
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tick(START + 300).returncode, 0)
        first = self.sent()
        self.assertEqual(len(first), 2)
        self.assertEqual({item["from"] for item in first}, {"stall-watch"})
        bump = next(item for item in first if item["key"].endswith(":bump"))
        tell = next(item for item in first if item["key"].endswith(":tell"))
        self.assertEqual(bump["to"], f"uds:{self.root / 'sid-unit-one.sock'}")
        self.assertEqual(tell["to"], f"uds:{self.root / 'show-id.sock'}")
        self.assertIn("idle since 12:00 PDT", bump["text"])
        self.assertIn("Continue your run", bump["text"])
        self.assertIn("Last status: — holding: waiting on a decision", tell["text"])
        self.assertEqual(self.tick(START + 360).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.panes["unit-one"]["pane"] = "reply text\n— holding: waiting on a decision\n"
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
        self.assertNotEqual({item["key"] for item in first}, {item["key"] for item in second})

    def test_stalled_unit_is_bumped_and_told_once_until_latest_turn_end_changes(self) -> None:
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tick(START + 300).returncode, 0)
        first = self.sent()
        self.assertEqual(len(first), 2)
        self.assertEqual(self.tick(START + 360).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.panes["unit-one"]["pane"] = "reply text\n— holding: waiting on a decision\n"
        self.assertEqual(self.tick(START + 361).returncode, 0)
        self.assertEqual(self.tick(START + 661).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.panes["unit-one"]["pane"] = "— holding: ready for next step\n"
        self.assertEqual(self.tick(START + 662).returncode, 0)
        self.assertEqual(self.tick(START + 962).returncode, 0)
        second = self.sent()[2:]
        self.assertEqual(len(second), 2)
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
        alerts = self.sent()
        self.assertEqual(len(alerts), 2, alerts)
        self.assertEqual(alerts[-1]["to"], "user")
        self.assertEqual(alerts[-1]["text-id"], "stall_watch.permission_prompt")
        self.assertEqual(alerts[-1]["need"], "blocked")
        values = cast(dict[str, dict[str, str]], json.loads(self.send_log.read_text().splitlines()[-1]))["values"]
        self.assertEqual((values["unit"], values["request"], values["since"]), ("unit-one", "edit panes.py", "12:00 PDT"))
        # The alert has the unit's key, which a retire cancels it by.
        self.assertEqual(alerts[-1]["key"], PROMPT_KEY)
        # No bump and no tell while it waits on the user.
        self.assertNotIn("uds:", " ".join(item["to"] for item in alerts))
        # The prompt is answered: the alert is retired, the idle clock starts again, and a new prompt alerts again.
        self.panes["unit-one"]["agent_status"] = "idle"
        self.panes["unit-one"]["pane"] = "— holding: ready for next step\n"
        self.assertEqual(self.tick(START + 960).returncode, 0)
        self.assertEqual(self.sent()[2:], [RETIRE])
        self.assertFalse(self.prompt_state().exists())
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.assertEqual(self.tick(START + 1000).returncode, 0)
        self.assertEqual(self.tick(START + 1060).returncode, 0)
        self.assertEqual([(item["to"], item["key"], item.get("command")) for item in self.sent()[3:]],
                         [("user", PROMPT_KEY, None)])

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
        self.assertEqual(self.tick(START + 961).returncode, 0)
        self.assertEqual([item["to"] for item in self.sent()], ["user"])

    def test_the_request_is_read_from_the_prompt_at_the_bottom_of_the_pane(self) -> None:
        import stall_watch
        self.assertEqual(stall_watch.prompt_request(EDIT_PROMPT), "edit panes.py")
        self.assertEqual(stall_watch.prompt_request(BASH_PROMPT),
                         "run a shell command to read lines 5 to the end of a saved output")
        self.assertEqual(stall_watch.prompt_request(" Do you want to create notes.md?\n ❯ 1. Yes\n"), "create notes.md")
        self.assertEqual(stall_watch.prompt_request("Bash command\n\n   ls\n\n Do you want to proceed?\n"),
                         "run a shell command")
        self.assertEqual(stall_watch.prompt_request("— holding: waiting\n"), "go on with its work")

    def test_the_request_skips_the_auto_deny_banner(self) -> None:
        import stall_watch
        self.assertEqual(stall_watch.prompt_request(AUTO_DENY_PROMPT),
                         "run a shell command to read lines 5 to the end of a saved output")
        self.assertTrue(stall_watch.auto_denied(AUTO_DENY_PROMPT))
        # The banner quoted in the transcript above a prompt is not the prompt's own banner.
        quoted = " The push said ⚠ Claude Code will automatically deny this request in 0:41.\n\n" + EDIT_PROMPT
        self.assertFalse(stall_watch.auto_denied(quoted))

    def test_the_request_comes_from_the_pending_tool_call_in_the_transcript(self) -> None:
        import stall_watch
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
        import stall_watch
        self.assertEqual(stall_watch.prompt_request(OUTSIDE_PROMPT),
                         DELETE_REQUEST)
        # Without the warning, the wrapped description is the request, joined into one sentence.
        unwarned = OUTSIDE_PROMPT[:OUTSIDE_PROMPT.index("\n\n   rm -f deletes")] + "\n\n Do you want to proceed?\n"
        self.assertEqual(stall_watch.prompt_request(unwarned),
                         "run a shell command to write the 212 enrolled state and confirm the prompt names the worktree")

    def test_the_wait_starts_at_the_pending_tool_call_before_herdr_reports_blocked(self) -> None:
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
        made = START - 3 * 3600
        _ = transcript.write_text(tool_call("Bash", DELETE_CALL, made))
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = OUTSIDE_PROMPT
        # herdr first reports the pane blocked three hours after the call: the alert goes at once.
        self.assertEqual(self.tick(START).returncode, 0)
        alerts = self.sent()
        self.assertEqual([item["to"] for item in alerts], ["user"])
        values = cast(dict[str, dict[str, str]], json.loads(self.send_log.read_text().splitlines()[-1]))["values"]
        self.assertEqual(values["since"], "09:00 PDT")
        self.assertIn("b1e4yc8b9.txt", values["request"])
        # The same prompt gets no second alert.
        self.assertEqual(self.tick(START + 60).returncode, 0)
        self.assertEqual(self.tick(START + 900).returncode, 0)
        self.assertEqual(len(self.sent()), 1)
        # The call is answered and the next call waits at a new prompt, with herdr blocked the whole time:
        # the first alert is retired, and the new prompt gets its own alert.
        _ = transcript.write_text(tool_call("Bash", DELETE_CALL, made) + tool_result()
                                  + tool_call("Bash", DELETE_CALL, START + 950, "toolu_2"))
        self.assertEqual(self.tick(START + 960).returncode, 0)
        self.assertEqual(self.sent()[1:], [RETIRE])
        self.assertEqual(self.tick(START + 1020).returncode, 0)
        values = cast(dict[str, dict[str, str]], json.loads(self.send_log.read_text().splitlines()[-1]))["values"]
        self.assertEqual((len(self.sent()), self.sent()[-1]["key"], values["since"]), (3, PROMPT_KEY, "12:15 PDT"))

    def prompt_state(self) -> Path:
        import stall_watch
        with mock.patch.object(stall_watch, "STATE_DIR", self.state):
            return stall_watch.prompt_path("showrunner", "unit-one")

    def alerted_at_a_prompt(self) -> None:
        """unit-one waits at an edit prompt, and its alert goes out at START + 60."""
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        self.assertEqual(self.tick(START + 60).returncode, 0)
        self.assertEqual([item["key"] for item in self.sent()], [PROMPT_KEY])

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
        self.assertEqual(self.sent()[1:], [RETIRE, RETIRE, RETIRE])
        self.assertFalse(self.prompt_state().exists())

    def test_an_auto_denied_prompt_after_its_alert_retires_the_alert(self) -> None:
        self.alerted_at_a_prompt()
        self.panes["unit-one"]["pane"] = AUTO_DENY_PROMPT
        self.assertEqual(self.tick(START + 120).returncode, 0)
        self.assertEqual(self.sent()[1:], [RETIRE])
        self.assertFalse(self.prompt_state().exists())

    def test_a_finished_run_after_a_prompt_alert_retires_the_alert(self) -> None:
        self.alerted_at_a_prompt()
        self.states["unit-one"] = "run-finished"
        self.assertEqual(self.tick(START + 120).returncode, 0)
        self.assertEqual(self.sent()[1:], [RETIRE])
        self.assertFalse(self.prompt_state().exists())

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
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
        _ = transcript.write_text(tool_call("Bash", DELETE_CALL, START - 60))
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = OUTSIDE_PROMPT
        # A failed alert opens no case; the alert sent on the next tick opens one.
        _ = self.fail_file.write_text('["user"]')
        _ = self.tick(START)
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
                          "stall-watch:prompt:showrunner:unit-one", ""))
        owner = self.owner_messages()
        self.assertEqual([(item["to"], item["from"], item["key"]) for item in owner],
                         [(f"uds:{self.root / 'show-id.sock'}", "rca", "rca-261006-1")])
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
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
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
        self.assertEqual([item["key"] for item in self.sent()], [PROMPT_KEY])
        self.assertEqual([item["key"] for item in self.owner_messages()], ["rca-261006-1"])

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
        self.assertEqual(self.sent()[1:], [RETIRE])
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
        self.assertEqual(self.sent()[1:], [RETIRE])
        self.assertEqual([item["key"] for item in self.owner_messages()], ["rca-261006-1"])

    def test_a_retired_alerts_pending_case_does_not_delay_a_new_prompt_alert(self) -> None:
        self.panes["unit-one"]["agent_status"] = "blocked"
        self.panes["unit-one"]["pane"] = EDIT_PROMPT
        _ = self.tick(START)
        with mock.patch.object(rca, "save", side_effect=OSError("case disk unavailable")):
            failed = self.tick(START + 60)
            self.assertIn("rca: no case opened: OSError: case disk unavailable", failed.stderr)
            self.panes["unit-one"]["agent_status"] = "idle"
            _ = self.tick(START + 120)
            self.assertEqual(self.sent()[1:], [RETIRE])
            self.assertFalse(self.prompt_state().exists())
            old_cases = list(self.state.glob("*.case.json"))
            self.assertEqual(len(old_cases), 1)
            transcript = self.projects / "project" / "sid-unit-one.jsonl"
            _ = transcript.write_text(tool_call("Bash", DELETE_CALL, START + 180))
            self.panes["unit-one"]["agent_status"] = "blocked"
            self.panes["unit-one"]["pane"] = BASH_PROMPT
            _ = self.tick(START + 180)
            _ = self.tick(START + 180 + stall_watch.PROMPT_ALERT_SECONDS - 1)
            self.assertEqual(self.sent()[1:], [RETIRE])
            retry = self.tick(START + 180 + stall_watch.PROMPT_ALERT_SECONDS)
        self.assertIn("rca: no case opened: OSError: case disk unavailable", retry.stderr)
        self.assertEqual([item["key"] for item in self.sent()], [PROMPT_KEY, PROMPT_KEY, PROMPT_KEY])
        self.assertEqual(self.sent()[1], RETIRE)
        self.assertEqual(self.sent()[2]["text-id"], "stall_watch.permission_prompt")
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
        self.assertEqual([item["key"] for item in self.sent()], [PROMPT_KEY, PROMPT_KEY, PROMPT_KEY])
        self.assertEqual(len(self.owner_messages()), 2)

    def test_a_helpers_pending_bash_call_names_its_command_cut_to_90_characters(self) -> None:
        import stall_watch
        import push_texts
        helpers = self.projects / "project" / "sid-unit-one" / "subagents"
        helpers.mkdir(parents=True)
        _ = (self.projects / "project" / "sid-unit-one.jsonl").write_text(
            tool_call("Bash", {"command": "ls"}, START - 600) + tool_result())
        _ = (helpers / "agent-a1.jsonl").write_text(tool_call("Bash", {"command": "git status"}, START - 300, "toolu_a"))
        _ = (helpers / "agent-a2.jsonl").write_text(tool_call(
            "Bash", {"command": "cargo build -p cargo-handler", "description": "Build the handler crate"}, START - 60, "toolu_b"))
        with mock.patch.object(stall_watch, "PROJECTS_DIR", self.projects):
            pending = stall_watch.pending_tool("sid-unit-one")
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
        registry = self.root / "agents.conf"
        _ = write_registry(registry, {"claude.context": {"default": str(window)}}, replace_sections=("claude.context",))
        self.environment.update(AGENTS_CONFIG_FILE=str(registry), CLAUDE_CODE_AUTO_COMPACT_WINDOW="400k")
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
        entries = [{"type": "assistant", "message": {"model": "claude-test-1", "usage": usage}}
                   for usage in ({"input_tokens": 400_000},
                                 {"input_tokens": tokens // 3, "cache_creation_input_tokens": tokens // 3,
                                  "cache_read_input_tokens": tokens - 2 * (tokens // 3),
                                  "output_tokens": 400_000})]
        _ = transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        os.utime(transcript, (START - 3600, START - 3600))

    def test_an_idle_waiting_unit_near_compaction_is_bumped_and_told_once(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        blocked = "— blocked: my context is full; it compacts on the next turn"
        window = 283_800
        trigger = context_usage.trigger_tokens(window)
        threshold = context_usage.handoff_threshold(window)
        self.assertEqual(trigger, 250_800)
        # The shared boundary, the real accounts reading, the trigger and over it; every waiting kind.
        for status, tokens in [(blocked, tokens) for tokens in (threshold, 245_621, trigger, trigger + 1)] + [
                (f"— {kind}: waiting", threshold) for kind in ("done", "gate", "decision")] + [
                ("— holding: until 18:00", threshold)]:
            with self.subTest(status=status, tokens=tokens):
                pane = f"● {status}\n"
                self.panes["unit-one"]["pane"] = pane
                self.write_context_usage(tokens, window=window)
                self.state.mkdir(exist_ok=True)
                path = self.state / stall_watch.stretch_path("showrunner", "unit-one").name
                stretch = stall_watch.Stretch(pane_hash=hashlib.sha256(pane.strip().encode()).hexdigest(), since=START - 601,
                    bump_sent=False, tell_sent=False, reported_status="")
                _ = path.write_text(json.dumps(stretch))
                self.assertEqual(self.tick(START).returncode, 0)
                self.assertEqual(len(self.sent()), 2)
                self.assertEqual(self.tick(START + 600).returncode, 0)
                sent = self.sent()
                self.assertEqual(len(sent), 2)
                bump, = [item for item in sent if item["key"].endswith(":bump")]
                tell, = [item for item in sent if item["key"].endswith(":tell")]
                self.assertEqual(bump["key"].rsplit(":", 1)[-1], "bump")
                self.assertIn("sid-unit-one.sock", bump["to"])
                self.assertEqual(tell["key"].rsplit(":", 1)[-1], "tell")
                self.assertIn("show-id.sock", tell["to"])
                self.assertIn(f"Last status: {status}", tell["text"])
                saved = cast(stall_watch.Stretch, json.loads(path.read_text()))
                self.assertEqual(saved["since"], START - 601)
                self.assertEqual(saved["reported_status"], status)
                self.assertTrue(saved["bump_sent"])
                self.assertTrue(saved["tell_sent"])
                self.send_log.unlink()
                path.unlink()

    def test_a_waiting_unit_below_compaction_stays_quiet_and_resets_its_stretch(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        pane = "● — blocked: my context is full; it compacts on the next turn\n"
        self.panes["unit-one"]["pane"] = pane
        window = 283_800
        self.write_context_usage(context_usage.handoff_threshold(window) - 1, window=window)
        self.state.mkdir(exist_ok=True)
        path = self.state / stall_watch.stretch_path("showrunner", "unit-one").name
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
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
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

    def test_one_line_holding_reply_resets_reported_stretch_and_tells_without_bullet(self) -> None:
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
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
        after = [{"type": "system", "subtype": "compact_boundary"},
                 {"type": "user", "message": {"content": "summary: the unit was holding: on the user"}},
                 {"type": "attachment"}]
        _ = transcript.write_text("".join(json.dumps(entry) + "\n" for entry in (*entries, *after)))
        os.utime(transcript, (START - 3600, START - 3600))

    @staticmethod
    def said(*blocks: dict[str, str]) -> dict[str, object]:
        return {"type": "assistant", "message": {"content": list(blocks)}}

    def test_a_compacted_pane_reads_a_blocked_turn_end_from_the_transcript(self) -> None:
        self.compacted(self.said({"type": "text", "text": "Ready for the check."}),
                       self.said({"type": "thinking", "thinking": ""}),
                       self.said({"type": "text", "text": "Waiting.\n\n— blocked: the user's Ghostty check"}))
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 3600).returncode, 0)
        self.assertEqual(self.sent(), [])

    def test_a_compacted_pane_bumps_when_the_transcript_turn_holds_or_went_on(self) -> None:
        for last, status in (([self.said({"type": "text", "text": "— holding: waiting on the tests"})],
                              "— holding: waiting on the tests"),
                             ([self.said({"type": "text", "text": "— blocked: the user's Ghostty check"}),
                               self.said({"type": "tool_use", "name": "Bash"})], "none on screen")):
            with self.subTest(status=status):
                self.compacted(*last)
                self.assertEqual(self.tick(START).returncode, 0)
                self.assertEqual(self.tick(START + 300).returncode, 0)
                tell = next(item for item in self.sent() if item["key"].endswith(":tell"))
                self.assertIn(f"Last status: {status}", tell["text"])
                self.send_log.unlink()
                for path in self.state.glob("*.json"):
                    path.unlink()

    def test_holding_turn_end_still_bumps_idle_unit(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        self.panes["unit-one"]["pane"] = "— holding: waiting on a decision\n"
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})

    def test_a_hold_that_states_its_end_is_not_bumped_before_then(self) -> None:
        """START is 12:00 PDT: a hold ending at 14:00, dated or not, bumps only once 14:00 is past."""
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

    def test_a_hold_naming_a_past_time_still_bumps(self) -> None:
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
        _ = self.tick(START)
        _ = self.tick(START + 300)
        first_keys = {item["key"] for item in self.sent()}
        self.panes["unit-one"]["pane"] = "— done: phase finished\n"
        _ = self.tick(START + 360)
        self.panes["unit-one"]["pane"] = PANE
        _ = self.tick(START + 361)
        _ = self.tick(START + 661)
        second = self.sent()[2:]
        self.assertEqual(len(second), 2)
        self.assertTrue(first_keys.isdisjoint({item["key"] for item in second}))

    def test_failed_showrunner_delivery_retries_without_rebumping_unit(self) -> None:
        _ = self.fail_file.write_text(json.dumps([f"uds:{self.root / 'show-id.sock'}"]))
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
        for command in ("bash", "implement.sh"):
            with self.subTest(command=command):
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                _ = self.tick(START)
                worker = subprocess.Popen(["sleep", str(TEST_HANG_GUARD_SECS)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.children.append(worker)
                claude_pid = self.children[0].pid
                self.process_rows.append(f"{worker.pid} {claude_pid} {command} running")
                _ = self.tick(START + 300)
                self.assertEqual(self.sent(), [])
                _ = self.process_rows.pop()

    def test_work_the_unit_started_outside_its_tree_prevents_a_bump(self) -> None:
        """A build a unit backgrounded is reparented away from its Claude but keeps the unit's marks."""
        for unit, bumps in (("unit-one", 0), ("unit-two", 2)):
            with self.subTest(unit=unit):
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                _ = self.tick(START)
                marks = {"SHOWRUNNER_UNIT": "showrunner", "SHOWRUNNER_UNIT_ID": unit}
                worker = subprocess.Popen(MARKED_SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          env={**os.environ, **marks})
                self.children.append(worker)
                self.process_rows.append(f"{worker.pid} 1 sleep 60")
                _ = self.tick(START + 300)
                self.assertEqual(len(self.sent()), bumps)
                _ = self.process_rows.pop()

    def test_detached_work_with_a_former_production_mark_prevents_a_bump(self) -> None:
        doc = self.root / "showrunner-production.md"
        _ = doc.write_text("## Production Context\n\n- **Formerly:** `old-showrunner`\n\n" + doc.read_text())
        self.panes["unit-one"]["slug"] = "old-showrunner"
        worker = subprocess.Popen(MARKED_SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env={**self.environment, "SHOWRUNNER_UNIT": "old-showrunner",
                                       "SHOWRUNNER_UNIT_ID": "unit-one"})
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

    def test_detached_processes_share_one_slug_resolution_per_work_check(self) -> None:
        import stall_watch
        from scripts.production import showrunners

        doc = self.root / "showrunner-production.md"
        _ = doc.write_text("## Production Context\n\n- **Formerly:** `old-showrunner`\n\n" + doc.read_text())
        rows = [stall_watch.Process(10, 1, "claude"), stall_watch.Process(20, 1, "build"),
                stall_watch.Process(21, 1, "build")]
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
              mock.patch.object(stall_watch, "unit_marks", return_value=("old-showrunner", "unit-one")),
              mock.patch.object(showrunners, "_source", wraps=getattr(showrunners, "_source")) as reads):
            self.assertFalse(stall_watch.work_running(10, rows, ("unrelated", "unit-one")))
            self.assertEqual(reads.call_count, 1)
            self.assertTrue(stall_watch.work_running(10, rows, ("showrunner", "unit-one")))
            self.assertEqual(reads.call_count, 2)

    def test_recent_transcript_or_subagent_activity_prevents_a_bump(self) -> None:
        _ = self.tick(START)
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
        os.utime(transcript, (START + 100, START + 100))
        _ = self.tick(START + 300)
        self.assertEqual(self.sent(), [])
        os.utime(transcript, (START - 3600, START - 3600))
        subagent = self.projects / "project" / "sid-unit-one" / "subagents" / "worker.jsonl"
        subagent.parent.mkdir(parents=True)
        _ = subagent.write_text("{}\n")
        os.utime(subagent, (START + 100, START + 100))
        _ = self.tick(START + 301)
        self.assertEqual(self.sent(), [])
        _ = self.tick(START + 401)
        self.assertEqual(len(self.sent()), 2)

    def test_transcript_activity_moves_unreported_idle_start_and_delivery_key(self) -> None:
        _ = self.tick(START)
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
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
        _ = self.fail_file.write_text(json.dumps([f"uds:{self.root / 'show-id.sock'}"]))
        _ = self.tick(START)
        _ = self.tick(START + 300)
        first = self.sent()
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
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
        self.process_rows.append(f"999999 {claude_pid} implement.sh <defunct>")
        _ = self.tick(START + 300)
        self.assertEqual(len(self.sent()), 2)

    def test_subagent_activity_counts_without_a_parent_transcript(self) -> None:
        _ = self.tick(START)
        (self.projects / "project" / "sid-unit-one.jsonl").unlink()
        subagent = self.projects / "project" / "sid-unit-one" / "subagents" / "worker.jsonl"
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
        self.assertTrue(any("unit-two" in item["key"] for item in self.sent()))
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
        import stall_watch
        self.assertFalse((self.state / stall_watch.stretch_path("showrunner", "unit-one").name).exists())
        self.states["unit-one"] = "running"
        self.assertEqual(self.tick(START + 601).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tick(START + 901).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})

    def test_run_done_words_in_live_plan_do_not_stop_watching(self) -> None:
        import stall_watch
        _ = self.production_plan("run done appears here only as a description")
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})
        self.assertTrue((self.state / stall_watch.stretch_path("showrunner", "unit-one").name).exists())

    def test_finished_registry_unit_has_no_notice_or_stretch(self) -> None:
        import stall_watch
        self.configure_registered({"showrunner": [("unit-one", "run-finished")]})
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertFalse((self.state / stall_watch.stretch_path("showrunner", "unit-one").name).exists())

    def test_retired_plan_without_run_done_is_skipped(self) -> None:
        import stall_watch
        _ = self.production_plan("(retired by the user 2026-10-07, worktree removed)")
        first = self.tick(START)
        second = self.tick(START + 600)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.sent(), [])
        self.assertFalse((self.state / stall_watch.stretch_path("showrunner", "unit-one").name).exists())

    def test_a_unit_named_review_unit_beside_a_review_pool_is_bumped(self) -> None:
        self.configure_registered({"showrunner": [("review-unit", "running")]}, review_pool=True)
        _ = self.unit("review-unit")

        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        sent = self.sent()
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in sent}, {"bump", "tell"})
        self.assertTrue(all(item["key"].startswith("stall-watch:review-unit:") for item in sent), sent)

    def test_finished_units_are_skipped_while_a_running_unit_is_bumped(self) -> None:
        import stall_watch
        self.plans["build-report-unit"] = "`docs/as-built/build-report-session.md` (run done; as-built 2b4d952)"
        self.configure_registered({"showrunner": [
            ("build-report-unit", "run-finished"),
            ("hook-unit", "running"),
            ("notifier-unit", "run-finished"),
        ]})
        for name in ("build-report-unit", "hook-unit", "notifier-unit"):
            _ = self.unit(name)

        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        sent = self.sent()
        self.assertEqual(len(sent), 2)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in sent}, {"bump", "tell"})
        self.assertTrue(all(item["key"].startswith("stall-watch:hook-unit:") for item in sent))
        for name in ("build-report-unit", "notifier-unit"):
            self.assertFalse((self.state / stall_watch.stretch_path("showrunner", name).name).exists())

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
        self.environment["STALL_TEST_SESSION_ERRORS"] = json.dumps(["session:show-id"])
        # The script's exception handler, outside main(), formats this failure.
        _ = self.tick(START, subprocess_cli=True)
        result = self.tick(START + 600, subprocess_cli=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot tell whether session:show-id is running", result.stderr)
        self.assertEqual(self.sent(), [])

    def test_a_renamed_showrunner_is_still_told_of_its_stalled_unit(self) -> None:
        _ = self.record_session(os.getpid(), "show-id", "renamed-since-it-registered")
        _ = self.tick(START)
        _ = self.tick(START + 600)
        self.assertEqual([one["to"] for one in self.sent() if "idle since" in one["text"] and "bumped" in one["text"]],
                         [f"uds:{self.root / 'show-id.sock'}"])

    def test_locked_tick_exits_without_sending(self) -> None:
        _ = self.tick(START)
        with (self.state / "lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.tick(START + 300)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.sent(), [])

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
