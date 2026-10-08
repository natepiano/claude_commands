"""Stall watcher tests with isolated notifier, sessions, processes and relays."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, override
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).with_name("stall_watch.py")
START = int(datetime(2026, 10, 6, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())
PANE = "— holding: waiting on a decision\n"


def executable(path: Path, source: str) -> None:
    _ = path.write_text(source)
    path.chmod(0o755)


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
        self.tmux_file: Path = Path()
        self.send_log: Path = Path()
        self.fail_file: Path = Path()
        self.environment: dict[str, str] = {}
        self.panes: dict[str, dict[str, str | int]] = {}
        self.process_rows: list[str] = []
        self.children: list[subprocess.Popen[bytes]] = []
        # A unit's Plan cell and the state mark on its tmux session, where a test sets one.
        self.plans: dict[str, str] = {}
        self.states: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
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
        self.tmux_file = self.root / "tmux.json"
        self.send_log = self.root / "sends.jsonl"
        self.fail_file = self.root / "fail-targets.json"
        _ = self.fail_file.write_text("[]")
        self.config = self.root / "showrunners.json"
        self.configure({"showrunner": ["unit-one"]})
        self.addCleanup(self.close_processes)
        # Each pane is one tmux session marked as the unit it is keyed by.
        executable(self.bin / "tmux", """#!/usr/bin/env python3
import json, os, sys
panes = json.load(open(os.environ['STALL_TEST_TMUX_FILE']))
args = sys.argv[1:]
if args[0] == 'list-panes':
    for unit, data in panes.items():
        print(f"${data['id']}\\t%{data['id']}\\t{data['label']}")
    raise SystemExit(0)
target = args[args.index('-t') + 1]
matches = [unit for unit, data in panes.items() if target in (f"${data['id']}", f"%{data['id']}")]
if len(matches) != 1:
    raise SystemExit(1)
data = panes[matches[0]]
if args[0] == 'show-environment':
    print(f"SHOWRUNNER_UNIT={data['slug']}\\nSHOWRUNNER_UNIT_ID={matches[0]}")
else:
    print(data['pane'])
""")
        executable(self.bin / "ps", "#!/bin/sh\ncat \"$STALL_TEST_PS_FILE\"\n")
        executable(self.bin / "send.py", """#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
record = {name: args[args.index('--' + name) + 1] for name in ('to', 'from', 'key', 'text')}
line = (json.dumps(record) + '\\n').encode()
fd = os.open(os.environ['STALL_TEST_SEND_LOG'], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
os.write(fd, line)
os.close(fd)
time.sleep(float(os.environ.get('STALL_TEST_SEND_DELAY', '0')))
fail = json.load(open(os.environ['STALL_TEST_FAIL_FILE']))
raise SystemExit(1 if record['to'] in fail else 0)
""")
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "SHOWRUNNERS_CONFIG": str(self.config),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "PLAN_DELEGATE_HISTORY_DIR": str(self.root / "history"),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "SHOWRUNNERS_SESSIONS": str(self.sessions_script),
            "STALL_WATCH_STATE_DIR": str(self.state),
            "STALL_WATCH_SESSIONS": str(self.sessions_script),
            "STALL_WATCH_PROJECTS_DIR": str(self.projects),
            "STALL_WATCH_TMUX": "tmux",
            "UNIT_LOOKUP_TMUX": str(self.bin / "tmux"),
            "STALL_WATCH_PS": "ps",
            "STALL_WATCH_SEND": str(self.bin / "send.py"),
            "STALL_TEST_TMUX_FILE": str(self.tmux_file),
            "STALL_TEST_PS_FILE": str(self.ps_file),
            "STALL_TEST_SEND_LOG": str(self.send_log),
            "STALL_TEST_FAIL_FILE": str(self.fail_file),
        }
        _ = self.record_session(os.getpid(), "show-id", "showrunner")
        _ = self.record_session(os.getpid() + 1_000_000, "fault-id", "natedev")
        _ = self.unit("unit-one")

    def configure(self, runners: dict[str, list[str]], *, stall_minutes: int = 5) -> None:
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
                "- **User zone:** America/Los_Angeles", "",
                "## Units", "", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                "| --- | --- | --- | --- | --- | --- |", *rows, "")))

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
                _ = child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _ = child.kill()
                _ = child.wait(timeout=2)
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
        child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        pane_pid = 10_000 + len(self.panes)
        self.process_rows += [f"{pane_pid} 1 tmux pane", f"{child.pid} {pane_pid} claude --remote-control other-name"]
        pane_id = len(self.panes) + 1
        self.panes[name] = {"pane": PANE, "id": pane_id, "slug": "showrunner", "label": f"label-of-{name}"}
        socket_path = self.record_session(child.pid, f"sid-{name}", "different-remote-name")
        # The lookup reads the record itself: it needs the pane the session runs in, and a real socket.
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(socket_path))
        self.addCleanup(held.close)
        record_path = self.sessions / f"{child.pid}.json"
        record = cast(dict[str, object], json.loads(record_path.read_text()))
        _ = record_path.write_text(json.dumps({**record, "tmux": f"name-at-start:@1.%{pane_id}"}))
        transcript = self.projects / "project" / f"sid-{name}.jsonl"
        transcript.parent.mkdir(exist_ok=True)
        _ = transcript.write_text("{}\n")
        os.utime(transcript, (START - 3600, START - 3600))
        return child.pid, socket_path

    def tick(self, at: int, *, delay: int = 0) -> subprocess.CompletedProcess[str]:
        _ = self.ps_file.write_text("\n".join(self.process_rows) + "\n")
        _ = self.tmux_file.write_text(json.dumps({
            name: dict(pane) for name, pane in self.panes.items()}))
        # A unit's run state is read from the record of the newest /unit:delegate run in its worktree.
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
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env={**self.environment, "STALL_WATCH_NOW_EPOCH": str(at), "STALL_TEST_SEND_DELAY": str(delay)},
            capture_output=True, text=True, check=False,
        )

    def sent(self) -> list[dict[str, str]]:
        if not self.send_log.exists():
            return []
        return [json.loads(line) for line in self.send_log.read_text().splitlines()]

    def test_reported_unit_is_not_bumped_again_after_its_session_is_renamed(self) -> None:
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 300).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        # A rename changes the tmux label and the Claude name. The mark, and so the unit, stays.
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

    def test_holding_turn_end_still_bumps_idle_unit(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        self.panes["unit-one"]["pane"] = "— holding: waiting on a decision\n"
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})

    def test_shell_seen_then_gone_starts_a_new_idle_stretch(self) -> None:
        _ = self.tick(START)
        _ = self.tick(START + 300)
        self.assertEqual(len(self.sent()), 2)
        worker = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    def test_running_shell_or_delegate_launcher_prevents_a_bump(self) -> None:
        for command in ("bash", "implement.sh"):
            with self.subTest(command=command):
                self.state.mkdir(exist_ok=True)
                for path in self.state.glob("*.json"):
                    path.unlink()
                self.send_log.unlink(missing_ok=True)
                _ = self.tick(START)
                worker = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.children.append(worker)
                claude_pid = self.children[0].pid
                self.process_rows.append(f"{worker.pid} {claude_pid} {command} running")
                _ = self.tick(START + 300)
                self.assertEqual(self.sent(), [])
                _ = self.process_rows.pop()

    def test_recent_transcript_or_subagent_activity_prevents_a_bump(self) -> None:
        _ = self.tick(START)
        transcript = self.projects / "project" / "sid-unit-one.jsonl"
        os.utime(transcript, (START + 100, START + 100))
        _ = self.tick(START + 300)
        self.assertEqual(self.sent(), [])
        os.utime(transcript, (START - 3600, START - 3600))
        subagent = self.projects / "project" / "sid-unit-one" / "subagents" / "seat.jsonl"
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

    def test_stale_delegate_status_does_not_count_as_running_work(self) -> None:
        status = self.root / "delegate" / "status"
        status.parent.mkdir()
        _ = status.write_text("implementing\n")
        _ = self.tick(START)
        _ = self.tick(START + 300)
        self.assertEqual(len(self.sent()), 2)

    def test_defunct_delegate_process_does_not_count_as_running_work(self) -> None:
        _ = self.tick(START)
        claude_pid = self.children[0].pid
        self.process_rows.append(f"999999 {claude_pid} implement.sh <defunct>")
        _ = self.tick(START + 300)
        self.assertEqual(len(self.sent()), 2)

    def test_subagent_activity_counts_without_a_parent_transcript(self) -> None:
        _ = self.tick(START)
        (self.projects / "project" / "sid-unit-one.jsonl").unlink()
        subagent = self.projects / "project" / "sid-unit-one" / "subagents" / "seat.jsonl"
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
        self.assertIn("stall-watch: unit-one: one or more session records could not be read", result.stderr)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual(self.sent(), [])

    def test_a_showrunner_lookup_that_cannot_say_sends_nothing(self) -> None:
        self.environment["STALL_TEST_SESSION_ERRORS"] = json.dumps(["session:show-id"])
        _ = self.tick(START)
        result = self.tick(START + 600)
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
        started = time.monotonic()
        result = self.tick(START + 300, delay=20)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.sent()), 6)
        self.assertLess(elapsed, 35)


if __name__ == "__main__":
    _ = unittest.main()
