"""Native SendMessage misses point to the shared account-aware recipient lookup."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.lib.socket_test_support import short_socket_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
from codex_test_support import loaded_session_daemon, write_codex_account

HOOK = Path(__file__).with_name("post-tool-use-sendmessage-miss.py")


class SendMessageMissTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(short_socket_directory()))
        self.home = self.root / ".codex2"
        write_codex_account(self.root / ".codex", self.root / "agents", "codex1")
        write_codex_account(self.home, self.root / "agents", "codex2")
        (self.home / "app-server-control").mkdir()
        (self.root / "sessions").mkdir()
        binaries = self.root / "bin"
        binaries.mkdir()
        for command in ("ps", "ssh", "codex", "claude"):
            executable = binaries / command
            _ = executable.write_text("#!/bin/sh\nexit 91\n")
            executable.chmod(0o755)
        _ = (binaries / "ps").write_text("#!/bin/sh\nexit 0\n")
        self.environment = {
            "HOME": str(self.root), "PATH": str(binaries), "AGENT_NOTES_DIR": str(self.root / "agents"),
            "PROCESS_TABLE_PS": str(binaries / "ps"), "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"), "ROSTER_RUN_ROOT": str(self.root / "runs"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"), "DESIGN_ROLES_STATE": str(self.root / "roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "ownership"), "REVIEW_POOL_STATE": str(self.root / "pool"),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
        }
        self.peer_snapshot()

    def peer_snapshot(self, *, name: str = "", problems: tuple[str, ...] = ()) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            import codex_daemon
            import roster
            from codex_test_support import daemon_session_row

            rows = [replace(daemon_session_row(codex_daemon.CodexDaemon(
                self.home, self.home / "app-server-control/app-server-control.sock"), "peer-thread", name),
                machine="peer", account=roster.AccountLabel("codex2"))] if name else []
            snapshot = roster.RosterSnapshot(roster.Roster(rows, list(problems))).encode()
        _ = (self.root / "peer.json").write_text(snapshot)
        _ = (self.root / "bin/ssh").write_text(
            f"#!/bin/sh\nprintf '%s' {shlex.quote(snapshot)}\n"
            + f"printf contacted >> {shlex.quote(str(self.root / 'peer-read'))}\n")

    def run_hook(self, name: str = "morty", *, success: bool = False,
                 message: str = "", tool: str = "SendMessage", encoded: bool = False) -> subprocess.CompletedProcess[str]:
        response: object = {"success": success, "message": message or f"No agent named '{name}' is reachable. Did you mean: port?"}
        if encoded:
            response = json.dumps(response)
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps({
            "tool_name": tool, "tool_input": {"to": name, "message": "a question"}, "tool_response": response,
        }), text=True, capture_output=True, check=False, env=self.environment, cwd=self.root, timeout=300)

    def additional_context(self, result: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        output = cast(dict[str, object], json.loads(result.stdout))
        self.assertNotIn("decision", output)
        fields = cast(dict[str, object], output["hookSpecificOutput"])
        self.assertEqual(fields["hookEventName"], "PostToolUse")
        return cast(str, fields["additionalContext"])

    def assert_silent_without_roster(self, *, name: str = "morty", success: bool = False,
                                    message: str = "", tool: str = "SendMessage") -> None:
        with loaded_session_daemon(self.home, {"thread": "morty"}, expect_contact=False):
            result = self.run_hook(name, success=success, message=message, tool=tool)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse((self.root / "peer-read").exists())

    def test_a_native_miss_names_the_loaded_codex_account_and_send_command(self) -> None:
        with loaded_session_daemon(self.home, {"thread": "morty"}):
            text = self.additional_context(self.run_hook())
        self.assertIn("codex freestanding, account codex2", text)
        self.assertIn("codex:thread", text)
        self.assertIn("sandbox off", text)
        self.assertIn('python3 ~/.claude/scripts/message/send.py --to morty --from "<your session name>"', text)
        self.assertIn('--summary "<one line>" --file <message file>', text)
        self.assertFalse((self.root / "peer-read").exists())

    def test_a_native_miss_matches_the_codex_name_without_regard_to_case(self) -> None:
        with loaded_session_daemon(self.home, {"thread": "morty"}):
            text = self.additional_context(self.run_hook("Morty", encoded=True))
        self.assertIn("morty: codex", text)
        self.assertIn("--to Morty", text)

    def test_a_success_never_reads_the_roster(self) -> None:
        self.assert_silent_without_roster(success=True)

    def test_a_clean_miss_checks_the_peer_before_reporting_no_session(self) -> None:
        text = self.additional_context(self.run_hook())
        self.assertIn("no session named 'morty' on either machine either", text)
        self.assertTrue((self.root / "peer-read").exists())

    def test_another_failure_never_reads_the_roster(self) -> None:
        self.assert_silent_without_roster(message="Permission denied")

    def test_a_socket_miss_never_reads_the_roster(self) -> None:
        self.assert_silent_without_roster(name="uds:/temporary/socket")

    def test_another_tool_never_reads_the_roster(self) -> None:
        self.assert_silent_without_roster(tool="Bash")

    def test_a_peer_match_names_its_address_and_machine(self) -> None:
        self.peer_snapshot(name="morty")
        text = self.additional_context(self.run_hook("Morty"))
        self.assertIn("codex:peer-thread", text)
        self.assertIn("peer", text)
        self.assertIn("--to Morty", text)

    def test_multiple_matches_list_addresses_instead_of_choosing_one(self) -> None:
        with loaded_session_daemon(self.home, {"one": "Morty", "two": "morty"}):
            text = self.additional_context(self.run_hook("MORTY"))
        self.assertIn("codex:one", text)
        self.assertIn("codex:two", text)
        self.assertIn("send to one listed address", text)
        self.assertIn('--to "<one listed address>"', text)

    def test_a_roster_error_is_context_and_never_blocks(self) -> None:
        self.peer_snapshot(problems=("fixture peer failure",))
        text = self.additional_context(self.run_hook())
        self.assertIn("send.py roster error:", text)
        self.assertIn("fixture peer failure", text)
