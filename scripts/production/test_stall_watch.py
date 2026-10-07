"""Stall watcher tests with isolated notifier, sessions, processes and relays."""

from __future__ import annotations

import fcntl
import json
import os
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
        executable(self.bin / "tmux", """#!/usr/bin/env python3
import json, os, sys
panes = json.load(open(os.environ['STALL_TEST_TMUX_FILE']))
args = sys.argv[1:]
target = args[args.index('-t') + 1]
matches = [unit for unit in panes if target == ('=' + unit if args[0] == 'has-session' else '=' + unit + ':')]
if len(matches) != 1:
    raise SystemExit(1)
unit = matches[0]
if args[0] == 'has-session':
    raise SystemExit(0)
print(panes[unit]['pane_pid'] if args[0] == 'display-message' else panes[unit]['pane'])
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
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "SHOWRUNNERS_SESSIONS": str(self.sessions_script),
            "STALL_WATCH_STATE_DIR": str(self.state),
            "STALL_WATCH_SESSIONS": str(self.sessions_script),
            "STALL_WATCH_PROJECTS_DIR": str(self.projects),
            "STALL_WATCH_TMUX": "tmux",
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
        _ = self.config.write_text(json.dumps({
            "threshold_percent": 1, "repeat_minutes": 30, "stall_minutes": stall_minutes,
            "faults_to": "natedev", "always": ["natedev"], "showrunners": [
                {"session": session, "zone": "America/Los_Angeles", "units": units}
                for session, units in runners.items()
            ],
        }))

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

    def production(self, slug: str, units: tuple[str, ...], *, target: str = "session:show-id") -> None:
        directory = self.notifier / f"showrunner-{slug}"
        directory.mkdir()
        prompt = directory / "prompt"
        _ = prompt.write_text("Run `zsh ~/.claude/scripts/production/unit_status.sh /tmp/status "
                              + f"America/Los_Angeles {' '.join(units)} | cut -c1-400`.\n")
        _ = (directory / "conf").write_text(f"TARGET={target}\nPROMPT_FILE={prompt}\n")

    def production_plan(self, plan: str) -> Path:
        self.production("example", ("unit-one",))
        doc = self.root / "example-production.md"
        self.set_production_plan(doc, plan)
        conf = self.notifier / "showrunner-example" / "conf"
        _ = conf.write_text(conf.read_text() + f"CHECK=zsh /scripts/production/production_check.sh {doc}\n")
        return doc

    def set_production_plan(self, doc: Path, plan: str) -> None:
        _ = doc.write_text("## Units\n\n| Unit | Plan | Worktree |\n| --- | --- | --- |\n" +
                           f"| unit-one | {plan} | /tmp/unit-one |\n")

    def unit(self, name: str) -> tuple[int, Path]:
        child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        pane_pid = 10_000 + len(self.panes)
        self.process_rows += [f"{pane_pid} 1 tmux pane", f"{child.pid} {pane_pid} claude --remote-control other-name"]
        self.panes[name] = {"pane_pid": pane_pid, "pane": PANE}
        socket_path = self.record_session(child.pid, f"sid-{name}", "different-remote-name")
        transcript = self.projects / "project" / f"sid-{name}.jsonl"
        transcript.parent.mkdir(exist_ok=True)
        _ = transcript.write_text("{}\n")
        os.utime(transcript, (START - 3600, START - 3600))
        return child.pid, socket_path

    def tick(self, at: int, *, delay: int = 0) -> subprocess.CompletedProcess[str]:
        _ = self.ps_file.write_text("\n".join(self.process_rows) + "\n")
        _ = self.tmux_file.write_text(json.dumps(self.panes))
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env={**self.environment, "STALL_WATCH_NOW_EPOCH": str(at), "STALL_TEST_SEND_DELAY": str(delay)},
            capture_output=True, text=True, check=False,
        )

    def sent(self) -> list[dict[str, str]]:
        if not self.send_log.exists():
            return []
        return [json.loads(line) for line in self.send_log.read_text().splitlines()]

    def test_rename_preserves_reported_unit_and_showrunner_stretches(self) -> None:
        import stall_watch
        old_state = stall_watch.STATE_DIR
        stall_watch.STATE_DIR = self.state
        try:
            self.state.mkdir()
            old = stall_watch.stretch_path("showrunner", "unit-one")
            _ = old.write_text('{"reported_status": "holding", "bump_sent": true, "tell_sent": true}')
            stall_watch.rename_state("unit-one", "new-unit", "showrunner", "showrunner", ["new-unit"])
            self.assertFalse(old.exists())
            self.assertTrue(stall_watch.stretch_path("showrunner", "new-unit").exists())
            stall_watch.rename_state("showrunner", "new-showrunner", "showrunner", "new-showrunner", ["new-unit"])
            self.assertTrue(stall_watch.stretch_path("new-showrunner", "new-unit").exists())
        finally:
            stall_watch.STATE_DIR = old_state

    def test_reported_unit_is_not_bumped_again_after_rename(self) -> None:
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 300).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        renamed = subprocess.run([sys.executable, str(SCRIPT.with_name("showrunners.py")),
                                  "rename", "unit-one", "new-unit"], env=self.environment,
                                 capture_output=True, text=True, check=False)
        self.assertEqual(renamed.returncode, 0, renamed.stderr)
        self.panes["new-unit"] = self.panes.pop("unit-one")
        self.assertEqual(self.tick(START + 301).returncode, 0)
        self.assertEqual(len(self.sent()), 2)

    def test_stalled_unit_is_bumped_and_told_once_until_latest_turn_end_changes(self) -> None:
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

    def test_holding_turn_end_still_bumps_idle_unit(self) -> None:
        self.configure({"showrunner": ["unit-one"]}, stall_minutes=10)
        self.panes["unit-one"]["pane"] = "— holding: waiting on a decision\n"
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})

    def test_pane_process_running_claude_is_bumped_and_told(self) -> None:
        pid = self.children[0].pid
        self.panes["unit-one"]["pane_pid"] = pid
        self.process_rows = [f"{pid} 1 claude --remote-control unit-one"]
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(len(list(self.state.glob("*.json"))), 1)
        self.assertEqual(self.tick(START + 300).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()}, {"bump", "tell"})

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

    def test_standby_unit_is_skipped_until_ready(self) -> None:
        registry = SCRIPT.with_name("showrunners.py")
        added = subprocess.run([sys.executable, str(registry), "add", "showrunner",
                                "--zone", "America/Los_Angeles", "--unit", "unit-one",
                                "--standby"], env=self.environment, capture_output=True,
                               text=True, check=False)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual(self.sent(), [])
        import stall_watch
        self.assertFalse((self.state / stall_watch.stretch_path("showrunner", "unit-one").name).exists())
        ready = subprocess.run([sys.executable, str(registry), "ready", "showrunner",
                                "--unit", "unit-one"], env=self.environment,
                               capture_output=True, text=True, check=False)
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertEqual(self.tick(START + 601).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tick(START + 901).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})

    def test_finished_run_has_no_bump_notice_or_stretch_and_live_plan_is_bumped(self) -> None:
        import stall_watch
        doc = self.production_plan("`docs/as-built/example.md` (run done; as-built merged)")
        self.assertEqual(self.tick(START).returncode, 0)
        self.assertEqual(self.tick(START + 600).returncode, 0)
        self.assertEqual(self.sent(), [])
        self.assertFalse((self.state / stall_watch.stretch_path("showrunner", "unit-one").name).exists())

        self.set_production_plan(doc, "`docs/plans/example.md`")
        self.assertEqual(self.tick(START + 601).returncode, 0)
        self.assertEqual(self.tick(START + 901).returncode, 0)
        self.assertEqual({item["key"].rsplit(":", 1)[-1] for item in self.sent()},
                         {"bump", "tell"})

        self.set_production_plan(doc, "`docs/as-built/example.md` (run done; as-built merged)")
        self.assertEqual(self.tick(START + 902).returncode, 0)
        self.assertEqual(len(self.sent()), 2)
        self.assertFalse((self.state / stall_watch.stretch_path("showrunner", "unit-one").name).exists())

    def test_extended_tmux_session_name_does_not_match_missing_unit(self) -> None:
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

    def test_live_unconfigured_showrunner_fault_retries_and_rearms_after_removal(self) -> None:
        missing = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(missing)
        _ = self.record_session(missing.pid, "missing-id", "missing director")
        self.production("missing", ("extra-unit",), target="session:missing-id")
        fault_socket = f"uds:{self.root / 'fault-id.sock'}"
        _ = self.fail_file.write_text(json.dumps([fault_socket]))
        _ = self.tick(START)
        first = self.sent()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["to"], fault_socket)
        self.assertIn("showrunner missing", first[0]["text"])
        self.assertIn("session missing director", first[0]["text"])
        self.assertIn("showrunners.py add", first[0]["text"])
        self.assertIn("--unit extra-unit", first[0]["text"])
        _ = self.tick(START + 10)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.sent()[0]["key"], self.sent()[1]["key"])
        _ = self.fail_file.write_text("[]")
        _ = self.tick(START + 20)
        _ = self.tick(START + 30)
        self.assertEqual(len(self.sent()), 3)
        self.configure({"showrunner": ["unit-one"], "missing director": []})
        _ = self.tick(START + 40)
        self.configure({"showrunner": ["unit-one"]})
        _ = self.tick(START + 50)
        self.assertEqual(len(self.sent()), 4)
        self.assertNotEqual(self.sent()[0]["key"], self.sent()[3]["key"])

    def test_live_showrunner_with_unreadable_prompt_sends_placeholder_fault(self) -> None:
        missing = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(missing)
        _ = self.record_session(missing.pid, "missing-id", "missing director")
        self.production("missing", ("extra-unit",), target="session:missing-id")
        (self.notifier / "showrunner-missing" / "prompt").unlink()
        result = self.tick(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.sent()
        self.assertEqual(len(sent), 1)
        self.assertIn("--zone <zone> --unit <tmux session>", sent[0]["text"])
        self.assertIn("prompt could not be read", sent[0]["text"])

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
