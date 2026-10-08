"""Tmux names follow live Claude session names without touching a real server."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast, override
from unittest import mock

import tmux_names
import showrunners

SCRIPT = Path(__file__).with_name("tmux_names.py")
TEST_TMUX_SOCKET = "/tmp/tmux-test/default"


def registered(session: str, status: str = "running") -> dict[str, str]:
    return {"session": session, "status": status}


class TmuxNamesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.config: Path = Path()
        self.sessions: Path = Path()
        self.proc: Path = Path()
        self.tmux: Path = Path()
        self.environment: dict[str, str] = {}
        self.children: list[subprocess.Popen[bytes]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "showrunners.json"
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.tmux = self.root / "tmux.json"
        _ = self.tmux.write_text(json.dumps({"old": ["%1"]}))
        self.children = []
        self.addCleanup(self.close_children)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "tmux"
        _ = stub.write_text("""#!/usr/bin/env python3
import json, os, sys
path = os.environ['TEST_TMUX_STATE']
try:
    state = json.load(open(path))
except FileNotFoundError:
    raise SystemExit(1)
args = sys.argv[1:]
if args[0] == 'display-message':
    print(os.environ['TEST_TMUX_SOCKET'])
elif args[0] == 'list-sessions':
    for name in state:
        print(name)
elif args[0] == 'list-panes':
    for name, panes in state.items():
        for pane in panes:
            print(name + '\\t' + pane)
elif args[0] == 'rename-session':
    if os.path.exists(os.environ.get('TEST_TMUX_RENAME_FAILURE', '')):
        print('injected rename failure', file=sys.stderr)
        raise SystemExit(1)
    old, new = args[args.index('-t') + 1][1:], args[-1]
    if old not in state or new in state:
        raise SystemExit(1)
    state[new] = state.pop(old)
    with open(path, 'w') as out:
        json.dump(state, out)
else:
    raise SystemExit(2)
""")
        stub.chmod(0o755)
        sender = bin_dir / "send.py"
        _ = sender.write_text("""#!/usr/bin/env python3
import os, sys
with open(os.environ['TEST_FAULTS'], 'a') as out:
    out.write(' '.join(sys.argv[1:]) + '\\n')
""")
        sender.chmod(0o755)
        sessions_script = self.root / "sessions.py"
        _ = sessions_script.write_text("import sys\n"
                                   + "print('/tmp/fault.sock') if sys.argv[1:] == ['socket', 'natedev'] "
                                   + "else sys.exit(1)\n")
        _ = self.config.write_text(json.dumps({"threshold_percent": 2, "repeat_minutes": 30,
                                           "stall_minutes": 5, "faults_to": "natedev", "always": [],
                                           "showrunners": [{"session": "director", "zone": "America/Los_Angeles",
                                                            "units": [registered("old")]}]}))
        self.environment = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                            "SHOWRUNNERS_CONFIG": str(self.config),
                            "SHOWRUNNERS_SESSIONS": str(sessions_script),
                            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
                            "STALL_WATCH_STATE_DIR": str(self.root / "stall-state"),
                            "TMUX_NAMES_PROC_DIR": str(self.proc),
                            "TMUX_NAMES_TMUX": "tmux", "TMUX_NAMES_SEND": str(sender),
                            "TMUX_NAMES_FAULT_STATE_DIR": str(self.root / "fault-state"),
                            "TEST_FAULTS": str(self.root / "faults"),
                            "TEST_TMUX_STATE": str(self.tmux),
                            "TEST_TMUX_SOCKET": TEST_TMUX_SOCKET,
                            "TEST_TMUX_RENAME_FAILURE": str(self.root / "rename-failure")}

    def close_children(self) -> None:
        for child in self.children:
            child.terminate()
            _ = child.wait(timeout=3)

    def session(self, name: str, pane: str, *, source: str = "user",
                tmux_socket: str = TEST_TMUX_SOCKET) -> None:
        child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        _ = (self.sessions / f"{child.pid}.json").write_text(json.dumps({
            "name": name, "nameSource": source, "sessionId": str(child.pid)}))
        directory = self.proc / str(child.pid)
        directory.mkdir()
        _ = (directory / "environ").write_bytes(
            f"TMUX={tmux_socket},{child.pid},0\0TMUX_PANE={pane}\0".encode()
        )

    def tick(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT)], env=self.environment,
                              capture_output=True, text=True, check=False)

    def names(self) -> dict[str, list[str]]:
        return cast(dict[str, list[str]], json.loads(self.tmux.read_text()))

    def entries(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], json.loads(self.config.read_text())["showrunners"])

    def test_unit_rename_changes_the_tmux_label_and_leaves_the_registry_alone(self) -> None:
        before = self.config.read_bytes()
        self.session("new", "%1")
        result = self.tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.names(), {"new": ["%1"]})
        # The registry holds no unit names, so a unit's rename leaves it as it was.
        self.assertEqual(self.config.read_bytes(), before)

    def test_showrunner_rename_changes_config_session(self) -> None:
        _ = self.tmux.write_text(json.dumps({"director": ["%1"]}))
        self.session("new director", "%1")
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.entries()[0]["session"], "new director")
        self.assertEqual(self.names(), {"new director": ["%1"]})

    def test_equal_name_does_nothing(self) -> None:
        self.session("old", "%1")
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.names(), {"old": ["%1"]})

    def test_derived_and_peer_names_do_not_rename_tmux(self) -> None:
        for source in ("derived", "peer"):
            with self.subTest(source=source):
                self.session("nixos-45", "%1", source=source)
                self.assertEqual(self.tick().returncode, 0)
                self.assertEqual(self.names(), {"old": ["%1"]})
                self.assertEqual(self.entries()[0]["units"], [registered("old")])

    def test_rename_works_before_registry_has_been_created(self) -> None:
        self.config.unlink()
        self.session("new", "%1")
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.names(), {"new": ["%1"]})

    def test_tmux_failure_is_finished_next_tick(self) -> None:
        self.session("new", "%1")
        _ = (self.root / "rename-failure").touch()
        first = self.tick()
        self.assertIn("tmux session still names", first.stderr)
        self.assertEqual(self.names(), {"old": ["%1"]})
        (self.root / "rename-failure").unlink()
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.names(), {"new": ["%1"]})

    def test_rename_session_does_not_change_another_pane_session(self) -> None:
        _ = self.tmux.write_text(json.dumps({"old": ["%1"], "other": ["%2"]}))
        self.session("new", "%1")
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.names(), {"new": ["%1"], "other": ["%2"]})

    def test_shared_pane_and_taken_name_are_skipped_and_fault_once(self) -> None:
        self.session("new", "%1")
        self.session("another", "%1")
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.names(), {"old": ["%1"]})
        self.assertEqual(len((self.root / "faults").read_text().splitlines()), 1)
        _ = self.tmux.write_text(json.dumps({"old": ["%1"], "new": ["%2"]}))
        (self.sessions / f"{self.children[-1].pid}.json").unlink()
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(len((self.root / "faults").read_text().splitlines()), 2)

    def test_a_fault_waits_for_the_next_tick_when_the_session_records_cannot_be_read(self) -> None:
        settings = showrunners.ShowrunnerSettings(threshold_percent=90.0, repeat_minutes=1.0, stall_minutes=1.0,
                                                  faults_to="director", always=[], showrunners=[])
        errors = io.StringIO()
        state = self.root / "fault-state"
        refused = OSError("cannot tell whether director is running: records unreadable")
        with mock.patch.object(tmux_names, "FAULT_STATE_DIR", state), \
                mock.patch.object(showrunners, "socket_for", side_effect=refused), \
                redirect_stderr(errors):
            tmux_names.fault("name taken", "old", "new", settings)
        self.assertIn("tmux-names: cannot tell whether director is running", errors.getvalue())
        self.assertEqual(list(state.iterdir()), [])

    def test_live_sessions_require_the_tmux_server_socket_to_match(self) -> None:
        self.session("local", "%0")
        self.session("stranger", "%0", tmux_socket="/tmp/tmux-test/other")
        self.session("missing tmux", "%2")
        without_tmux = self.children[-1]
        _ = (self.proc / str(without_tmux.pid) / "environ").write_bytes(b"TMUX_PANE=%2\0")
        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(tmux_names, "SESSIONS_DIR", self.sessions), \
                mock.patch.object(tmux_names, "PROC_DIR", self.proc), \
                mock.patch.object(tmux_names, "TMUX", "tmux"):
            sessions = tmux_names.live_sessions()
        if isinstance(sessions, tmux_names.TmuxServerUnavailable):
            self.fail(sessions.reason)
        self.assertEqual([session.name for session in sessions], ["local"])

    def test_tick_uses_only_claude_session_on_its_tmux_server(self) -> None:
        _ = self.tmux.write_text(json.dumps({"old": ["%0"]}))
        self.session("local", "%0")
        self.session("stranger", "%0", tmux_socket="/tmp/tmux-test/other")
        result = self.tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.names(), {"local": ["%0"]})
        self.assertFalse((self.root / "faults").exists())

    def test_tick_does_not_rename_for_session_on_another_tmux_server(self) -> None:
        _ = self.tmux.write_text(json.dumps({"old": ["%0"]}))
        self.session("stranger", "%0", tmux_socket="/tmp/tmux-test/other")
        result = self.tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.names(), {"old": ["%0"]})
        self.assertFalse((self.root / "faults").exists())

    def test_no_tmux_server_exits_zero(self) -> None:
        self.tmux.unlink()
        self.session("new", "%1")
        self.assertEqual(self.tick().returncode, 0)
