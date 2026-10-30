"""Regression tests for shutdown status rendering and account resolution."""

from __future__ import annotations

from scripts.whoami.note_file import note_name

from unit_staff_test_support import legacy_unit_host
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from test_support import ShutdownSendLeaf, ShutdownTestCase
import shutdown
import settle
import inventory
from scripts.accounts import accounts
import shutdown_record as record_store
from account import UnreadableAccount
from inventory import Inventory
from shutdown_record import SessionShutdown
from test_shutdown_record import LegacyRecordFixture, write_old, move_old


LOGIN = "owner@example.com"


def action_line(label: str, verb: str, names: str, machine: str = "natedev") -> str:
    identities = ",".join(name + "-id" for name in names.split(", "))
    return (f"  {label}: in a terminal on {machine}: ~/.claude/scripts/lib/py "
        f"~/.claude/scripts/shutdown/shutdown.py {verb} {LOGIN} --here --only {identities} · {names}")


def resume_conductor_line(name: str, machine: str = "natedev") -> str:
    identities = ",".join(item + "-id" for item in name.split(", "))
    return (f"  no conductor running; in a terminal on {machine}: ~/.claude/scripts/lib/py "
        f"~/.claude/scripts/shutdown/shutdown.py down {LOGIN} --here --only {identities}")


def empty_inventory(machine: str = "natedev") -> Inventory:
    return {
        "machine": machine,
        "login": LOGIN,
        "label": "claude2",
        "sessions": [],
        "unattributed": [],
    }


def empty_recovery_inventory() -> shutdown.RecoveryStatusInventory:
    return shutdown.RecoveryStatusInventory(**empty_inventory(), recovery=[],
        snapshot=shutdown.NoSnapshotOutcomeRecorded(kind="no outcome recorded"))


def complete_session(*, host: object) -> dict[str, object]:
    return {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "config_dir": str(Path.home() / ".claude2"),
        "pid": 1234,
        "proc_start": "process-start",
        "name": "shutdown",
        "cwd": "/tmp/checkout",
        "kind": "top-level",
        "probe": False,
        "status": "idle",
        "updated_at": 0,
        "host": host,
        "model": {"kind": "model", "name": "opus"},
        "checkout": {
            "kind": "git",
            "path": "/tmp/checkout",
            "head": {"kind": "branch", "name": "main"},
            "upstream": {"kind": "tracking", "ahead": 0},
            "dirty": [],
        },
        "run_dirs": [],
        "codex_servers": [],
        "timers": [],
    }


def complete_record(machine: str = "natedev") -> LegacyRecordFixture:
    return cast(
        LegacyRecordFixture,
        cast(
            object,
            {
                "login": LOGIN,
                "label": "claude2",
                "machine": machine,
                "state": "settling",
                "requested_at": "2026-10-09T21:49:10+00:00",
                "requested_by": {"kind": "terminal"},
                "scope": {"kind": "all account sessions"},
                "conductor": {"kind": "not started"},
                "force": "wait for ready",
                "entries": [
                    {
                        "session": complete_session(host={"kind": "unknown"}),
                        "timers": [],
                        "settle_message": {"kind": "not sent"},
                        "where": {"kind": "not said"},
                        "progress": {"kind": "waiting"},
                        "stop_issues": [],
                    }
                ],
                "stop_issues": [],
            },
        ),
    )


def session_shutdown(machine: str = "natedev", name: str = "shutdown") -> SessionShutdown:
    old = complete_record(machine)
    item = old["entries"][0]
    item["session"]["name"] = name
    item["session"]["session_id"] = name + "-id"
    return SessionShutdown(
        login=LOGIN, label="claude2", machine=machine,
        requested_at=old["requested_at"], requested_by=old["requested_by"], request_id="request-one",
        hold="account", then="stop", force=old["force"], alerts="user", probe=False,
        session=item["session"], workers=[], timers=item["timers"],
        settle_message=item["settle_message"], where=item["where"],
        progress=cast(record_store.SessionProgress, item["progress"]), stop_issues=[], trail=[],
    )


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


@final
class ShutdownStatusTests(ShutdownTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.notes = Path()
        self.state = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.notes = self.root / "agents"
        self.notes.mkdir()
        self.state = self.root / "shutdown-state"
        self.state.mkdir()
        binary_root = self.root / "bin"
        binary_root.mkdir()
        ssh = binary_root / "ssh"
        _ = ssh.write_text(
            f"""#!{sys.executable}
import os
import sys

command = sys.argv[-1]
if " records --json --here" in command:
    if os.environ.get("SHUTDOWN_STATUS_RECORD_TRANSPORT") == "255":
        raise SystemExit(255)
    print(os.environ.get("SHUTDOWN_STATUS_RECORD_OUTPUT", "[]"))
    print("rc=" + os.environ.get("SHUTDOWN_STATUS_RECORD_RC", "0"))
elif " conductor-status " in command:
    print(os.environ.get("SHUTDOWN_CONDUCTOR_OUTPUT", '{{"kind": "not running"}}'))
    print("rc=0")
else:
    print(os.environ["SHUTDOWN_STATUS_INVENTORY_OUTPUT"])
    print("rc=0")
""",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        environment = {
            **os.environ,
            "HOME": str(self.root),
            "AGENT_NOTES_DIR": str(self.notes),
            "PATH": str(binary_root) + os.pathsep + os.environ.get("PATH", ""),
            "SHUTDOWN_STATE_DIR": str(self.state),
            "SHUTDOWN_STATUS_INVENTORY_OUTPUT": json.dumps(
                empty_inventory(machine="Mac")
            ),
        }
        environment_context: object = cast(
            object,
            self.enterContext(patch.dict(os.environ, environment, clear=True)),
        )
        del environment_context

    def write_note(self, label: str, login: str) -> None:
        _ = (self.notes / note_name(label)).write_text(
            f"---\nlogin: {login}\nsessions: 1\n---\n",
            encoding="utf-8",
        )

    def test_status_actions_and_usage_lines_name_where_commands_run(self) -> None:
        saved = session_shutdown(name="Place")
        self.assertTrue(record_store.create_session(saved))
        status_output, usage_output = io.StringIO(), io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), redirect_stdout(status_output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        with redirect_stdout(usage_output), self.assertRaises(SystemExit) as exit_status:
            _ = shutdown.main(["--help"])
        self.assertEqual(exit_status.exception.code, 0)
        command_lines = [line for text in (status_output.getvalue(), usage_output.getvalue())
            for line in text.splitlines() if any(token in line for token in (
                "/shutdown", "shutdown.py", "accounts.py", "github-warm"))]
        self.assertGreaterEqual(len(command_lines), 4)
        for line in command_lines:
            with self.subTest(line=line):
                self.assertTrue(any(phrase in line.casefold() for phrase in (
                    "in this terminal", "in a terminal on ", "in any claude session on ")), line)
        self.assertIn(f"now {LOGIN} --here --only Place-id", status_output.getvalue())

    def run_status(
        self, status: int, output: str, *, as_json: bool = True
    ) -> tuple[int, str, str]:
        standard_output = io.StringIO()
        standard_error = io.StringIO()
        arguments = ["status", LOGIN]
        if as_json:
            arguments.append("--json")
        with (
            patch.object(shutdown, "inventory", return_value=empty_inventory()),
            patch.object(shutdown, "run_remote", return_value=(status, output)),
            patch.object(shutdown, "other_machine", return_value="Mac"),
            redirect_stdout(standard_output),
            redirect_stderr(standard_error),
        ):
            result = shutdown.main(arguments)
        return result, standard_output.getvalue(), standard_error.getvalue()

    def run_plain_status_with_ssh(self) -> tuple[int, str, str]:
        self.allow_leaf(ShutdownSendLeaf.RUN_REMOTE)
        standard_output = io.StringIO()
        standard_error = io.StringIO()
        with (
            patch.object(shutdown, "inventory", return_value=empty_inventory()),
            patch.object(shutdown, "other_machine", return_value="Mac"),
            redirect_stdout(standard_output),
            redirect_stderr(standard_error),
        ):
            result = shutdown.main(["status", LOGIN])
        return result, standard_output.getvalue(), standard_error.getvalue()

    def test_empty_inventory_renders_no_sessions_for_the_account(self) -> None:
        standard_output = io.StringIO()
        with (
            patch.object(shutdown, "inventory", return_value=empty_inventory()),
            redirect_stdout(standard_output),
        ):
            result = shutdown.main(["status", LOGIN, "--here"])

        self.assertEqual(result, 0)
        self.assertIn("natedev:\n  no sessions on claude2\n", standard_output.getvalue())

    def test_account_label_resolution_accepts_only_claude_notes(self) -> None:
        self.write_note("claude2", LOGIN)
        self.write_note("codex2", "codex@example.com")

        def inventory_for(login: str) -> Inventory:
            self.assertEqual(login, LOGIN)
            return empty_inventory()

        with (
            patch.object(shutdown, "inventory", side_effect=inventory_for),
            redirect_stdout(io.StringIO()),
        ):
            result = shutdown.main(["status", "claude2", "--json", "--here"])
        self.assertEqual(result, 0)

        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch.object(
                shutdown,
                "inventory",
                side_effect=AssertionError("inventory must not run"),
            ),
        ):
            result = shutdown.main(["status", "codex2", "--here"])
        self.assertEqual(result, 2)
        self.assertIn("unknown account codex2", errors.getvalue())

    def test_login_argument_is_accepted_without_a_note(self) -> None:
        def inventory_for(login: str) -> Inventory:
            self.assertEqual(login, "person@example.com")
            return empty_inventory()

        with (
            patch.object(shutdown, "inventory", side_effect=inventory_for),
            redirect_stdout(io.StringIO()),
        ):
            result = shutdown.main(
                ["status", "person@example.com", "--json", "--here"]
            )
        self.assertEqual(result, 0)

    def test_unknown_account_name_exits_two_with_guidance(self) -> None:
        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch.object(
                shutdown,
                "inventory",
                side_effect=AssertionError("inventory must not run"),
            ),
        ):
            result = shutdown.main(["status", "claude2", "--here"])

        self.assertEqual(result, 2)
        self.assertEqual(
            errors.getvalue(),
            "shutdown: unknown account claude2: give a note label such as claude2, or a login\n",
        )

    def test_unreadable_own_account_exits_two(self) -> None:
        errors = io.StringIO()
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "current-session"}),
            redirect_stderr(errors),
            patch.object(
                shutdown,
                "own_claude_account",
                side_effect=UnreadableAccount(
                    "this process's Claude account is unreadable"
                ),
            ),
            patch.object(
                shutdown,
                "inventory",
                side_effect=AssertionError("inventory must not run"),
            ),
        ):
            result = shutdown.main(["status", "--here"])

        self.assertEqual(result, 2)
        self.assertEqual(
            errors.getvalue(),
            "shutdown: this process's Claude account is unreadable\n",
        )

    def test_remote_inventory_missing_unattributed_is_unavailable_with_reason(self) -> None:
        response = json.dumps(
            {
                "machine": "Mac",
                "login": LOGIN,
                "label": "claude2",
                "sessions": [],
            }
        )

        result, output, error = self.run_status(0, response)

        expected = {
            "machine": "Mac",
            "status": "unavailable",
            "rc": 0,
            "reason": "inventory.unattributed is missing",
        }
        self.assertEqual(result, 0)
        self.assertEqual(
            output,
            json.dumps([empty_recovery_inventory(), expected], sort_keys=True) + "\n",
        )
        self.assertEqual(error, "")

    def test_remote_inventory_with_malformed_session_is_unavailable_with_reason(self) -> None:
        response = json.dumps(
            {
                "machine": "Mac",
                "login": LOGIN,
                "label": "claude2",
                "sessions": [complete_session(host={"kind": 7})],
                "unattributed": [],
            }
        )

        result, output, error = self.run_status(0, response)

        expected = {
            "machine": "Mac",
            "status": "unavailable",
            "rc": 0,
            "reason": "inventory.sessions[0].host.kind must be a string",
        }
        self.assertEqual(result, 0)
        self.assertEqual(
            output,
            json.dumps([empty_recovery_inventory(), expected], sort_keys=True) + "\n",
        )
        self.assertEqual(error, "")

    def test_unattributed_session_renders_its_reason(self) -> None:
        report = empty_inventory()
        report["unattributed"] = [
            {"pid": 1234, "name": "mystery", "reason": "account unreadable"}
        ]
        output = io.StringIO()
        with (
            patch.object(shutdown, "inventory", return_value=report),
            redirect_stdout(output),
        ):
            result = shutdown.main(["status", LOGIN, "--here"])

        self.assertEqual(result, 0)
        self.assertIn(
            "  unknown account · 1234 mystery · account unreadable\n",
            output.getvalue(),
        )

    def test_malformed_local_shutdown_record_reports_unreadable_and_status_succeeds(
        self,
    ) -> None:
        account_state = self.state / LOGIN.casefold()
        account_state.mkdir()
        record_path = account_state / "record.json"
        _ = record_path.write_text("not json", encoding="utf-8")

        output = io.StringIO()
        with (
            patch.object(shutdown, "inventory", return_value=empty_inventory()),
            redirect_stdout(output),
        ):
            result = shutdown.main(["status", LOGIN, "--here"])

        self.assertEqual(result, 0)
        self.assertIn(
            f"natedev: shutdown record unreadable: {record_path} is not valid JSON\n",
            output.getvalue(),
        )

    def test_remote_shutdown_record_transport_failure_reports_unreachable(self) -> None:
        os.environ["SHUTDOWN_STATUS_RECORD_TRANSPORT"] = "255"

        result, output, error = self.run_plain_status_with_ssh()

        self.assertEqual(result, 0)
        self.assertIn(
            "Mac: shutdown record not reached (unreachable)\n",
            output,
        )
        self.assertEqual(error, "")

    def test_remote_shutdown_record_failure_reports_unavailable_status(self) -> None:
        os.environ["SHUTDOWN_STATUS_RECORD_RC"] = "7"
        os.environ["SHUTDOWN_STATUS_RECORD_OUTPUT"] = "record command failed"

        result, output, error = self.run_plain_status_with_ssh()

        self.assertEqual(result, 0)
        self.assertIn(
            "Mac: shutdown record not reached (unavailable, rc 7)\n",
            output,
        )
        self.assertEqual(error, "")

    def test_unparseable_remote_shutdown_records_report_unreadable(self) -> None:
        os.environ["SHUTDOWN_STATUS_RECORD_OUTPUT"] = "not json"

        result, output, error = self.run_plain_status_with_ssh()

        self.assertEqual(result, 0)
        self.assertIn(
            "Mac: shutdown record unreadable: session records is not valid JSON\n",
            output,
        )
        self.assertEqual(error, "")

    def test_valid_remote_shutdown_record_renders_its_state(self) -> None:
        os.environ["SHUTDOWN_STATUS_RECORD_OUTPUT"] = json.dumps(
            [session_shutdown(machine="Mac")]
        )

        result, output, error = self.run_plain_status_with_ssh()

        self.assertEqual(result, 0)
        self.assertNotIn("Mac: shutdown settling\n", output)
        self.assertIn("  shutdown: waiting\n", output)
        self.assertEqual(error, "")

    def test_status_renders_canonical_remote_stop_issues_under_their_sessions(self) -> None:
        at = "2026-10-09T21:49:10+00:00"
        local = complete_record()
        local["entries"][0]["session"]["name"] = "local"
        local["entries"][0]["stop_issues"] = [
            record_store.NotReadyToStop(kind="not ready to stop", at=at, status="busy", progress="ready"),
            record_store.StillRunningAfterStop(kind="still running", at=at, reason="alive after two SIGTERMs"),
            record_store.AccountUnreadableAtStop(kind="account unreadable", at=at),
            {"kind": "worker still live", "at": at},
        ]
        remote_record = session_shutdown(machine="Mac", name="remote")
        remote_record["stop_issues"] = [
            record_store.CodexServerLeftRunning(
                kind="codex server left running", at=at, run_dir="/tmp/run", cause="stop not confirmed",
            ),
            record_store.LegacyTmuxSessionLeftRunning(
                kind="legacy tmux session left running", at=at,
                session_name="demo-unit", cause="owner identity lost",
            ),
        ]
        self.assertEqual(remote_record["stop_issues"][-1]["kind"], "legacy tmux session left running")
        write_old(local)
        os.environ["SHUTDOWN_STATUS_RECORD_OUTPUT"] = json.dumps([remote_record])
        result, output, error = self.run_plain_status_with_ssh()
        self.assertEqual((result, error), (0, ""))
        expected = [
            "  local: waiting",
            "    not stopped: busy, ready",
            "    still running: alive after two SIGTERMs",
            "    not stopped: account unreadable",
            "  remote: waiting",
            "    Codex server /tmp/run left running: stop not confirmed",
            "    legacy tmux session demo-unit left running: owner identity lost; tmux kill-session -t =demo-unit",
        ]
        self.assertEqual([line for line in output.splitlines() if line in expected], expected)

    def test_legacy_hosts_and_leftovers_show_manual_commands_in_status_and_now(self) -> None:
        saved = session_shutdown(name="old session")
        saved["session"] = cast(inventory.TopLevelSession, cast(object, {
            **saved["session"], "kind": "top-level", "host": inventory.LegacyTmuxHost(
                kind="legacy tmux", session_name="old:name")}))
        saved["progress"] = record_store.SessionStopped(kind="stopped", at=saved["requested_at"])
        saved["stop_issues"] = [record_store.LegacyTmuxSessionLeftRunning(
            kind="legacy tmux session left running", at=saved["requested_at"],
            session_name="old:name", cause="stop not confirmed")]
        self.assertTrue(record_store.create_session(saved))
        for verb in ("status", "now"):
            with self.subTest(verb=verb), patch.object(shutdown, "inventory", return_value=empty_inventory()), \
                 patch.object(settle, "_launch_conductor") as conductor, redirect_stdout(io.StringIO()) as output:
                status = shutdown.main([verb, LOGIN, "--here", "--only", "old session-id"])
                self.assertEqual(status, 1 if verb == "now" else 0)
                self.assertIn("tmux kill-session -t =old:name", output.getvalue())
                self.assertIn("--session-id 'old session-id'", output.getvalue())
                conductor.assert_not_called()
        remaining = record_store.find_session(LOGIN, "old session-id")
        assert remaining is not None
        self.assertEqual(remaining["session"], saved["session"])
        self.assertEqual(remaining["stop_issues"], saved["stop_issues"])
        self.assertEqual(remaining["progress"], saved["progress"])

    def test_every_legacy_cleanup_cause_holds_status_and_now(self) -> None:
        for index, cause in enumerate(cast(tuple[record_store.StopLeftRunningCause, ...],
                ("stop not confirmed", "owner identity lost"))):
            saved = session_shutdown(name=f"leftover-{index}")
            saved["progress"] = record_store.SessionNeedsManualRestart(kind="manual restart", command="old command")
            saved["stop_issues"] = [record_store.LegacyTmuxSessionLeftRunning(
                kind="legacy tmux session left running", at=saved["requested_at"], session_name="old:name", cause=cause)]
            self.assertTrue(record_store.create_session(saved))
            for verb in ("status", "now"):
                with self.subTest(cause=cause, verb=verb), patch.object(shutdown, "inventory", return_value=empty_inventory()), \
                     patch.object(settle, "_launch_conductor") as conductor, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(shutdown.main([verb, LOGIN, "--here", "--only", saved["session"]["session_id"]]),
                        1 if verb == "now" else 0)
                    self.assertIn("manual restart", output.getvalue())
                    self.assertIn("tmux kill-session -t =old:name", output.getvalue())
                    conductor.assert_not_called()
            self.assertEqual(record_store.find_session(LOGIN, saved["session"]["session_id"]), saved)

    def test_legacy_unit_without_leftovers_status_and_now_print_no_manual_command(self) -> None:
        self.write_note("claude3", LOGIN)
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": LOGIN}}))
        self.assertEqual(accounts.setup("claude3"), [])
        _ = (self.root / ".claude3/.claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": LOGIN}}))
        group_file = accounts.groups_file()
        group_file.parent.mkdir(parents=True, exist_ok=True)
        _ = group_file.write_text(json.dumps({"units": "claude3", "showrunners": "claude3"}))
        saved = session_shutdown(name="old unit")
        saved["session"] = cast(inventory.UnitSession, cast(object, {**saved["session"], "kind": "unit",
            "host": legacy_unit_host(kind="legacy tmux unit", production="demo", unit="old",
                doc="/tmp/demo.md", session_name="old-destination")}))
        saved["progress"] = record_store.SessionStopped(kind="stopped", at=saved["requested_at"])
        self.assertTrue(record_store.create_session(saved))
        for verb in ("status", "now"):
            with self.subTest(verb=verb), patch.object(shutdown, "inventory", return_value=empty_inventory()), \
                 patch.object(settle, "_launch_conductor") as conductor, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main([verb, LOGIN, "--here", "--only", saved["session"]["session_id"]]),
                    1 if verb == "now" else 0)
                # A legacy host alone restarts as a shared-session tab; only tmux leftovers need the user.
                self.assertNotIn("manual restart", output.getvalue())
                self.assertNotIn("--session old-destination", output.getvalue())
                conductor.assert_not_called()
        self.assertEqual(record_store.find_session(LOGIN, saved["session"]["session_id"]), saved)

    def test_unresolved_legacy_claude_and_codex_snapshots_list_without_manual_commands(self) -> None:
        owner = cast(inventory.TopLevelSession, cast(object, {
            **session_shutdown(name="snapshot claude")["session"], "kind": "top-level",
            "host": inventory.LegacyTmuxHost(kind="legacy tmux", session_name="old-claude")}))
        claude = record_store.SnapshotClaudeSession(agent="claude", session=owner, workers=[], timers=[],
            last_seen="2026-10-09T21:49:10+00:00", state="unresolved")
        codex_owner = cast(inventory.TopLevelSession, cast(object, {
            **owner, "name": "snapshot codex", "session_id": "codex-id",
            "host": inventory.LegacyTmuxHost(kind="legacy tmux", session_name="old-codex")}))
        codex = record_store.SnapshotCodexSession(agent="codex", session=codex_owner, workers=[], timers=[],
            last_seen=claude["last_seen"], state="unresolved", codex=inventory.CodexLaunch(
                home=str(self.root / "recorded-codex"), resume=inventory.CodexSessionId(kind="session id", session_id="recorded-id")))
        snapshot = record_store.RecoverySnapshot(machine="natedev", login=LOGIN, label="claude2",
            taken_at=claude["last_seen"], sessions=[claude, codex])
        record_store.write_snapshot(snapshot)
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn("snapshot claude: unresolved", output.getvalue())
        self.assertIn("snapshot codex: unresolved", output.getvalue())
        self.assertNotIn("manual restart", output.getvalue())
        self.assertEqual(record_store.read_snapshot(LOGIN), snapshot)
        self.assertEqual(record_store.session_records(LOGIN), [])

    def test_legacy_live_inventory_uses_the_same_fresh_manual_resume_command(self) -> None:
        owner = cast(inventory.TopLevelSession, cast(object, {
            **session_shutdown(name="live old")["session"], "kind": "top-level",
            "host": inventory.LegacyTmuxHost(kind="legacy tmux", session_name="old-destination")}))
        report = empty_inventory()
        report["sessions"] = [owner]
        with patch.object(shutdown, "inventory", return_value=report), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn("--session-id 'live old-id'", output.getvalue())
        self.assertNotIn("--session old-destination", output.getvalue())

    def test_status_lists_progress_details_and_message_delivery_per_session(self) -> None:
        failed = session_shutdown(name="failed launch")
        failed["progress"] = record_store.SessionRestartFailed(
            kind="restart failed", at="2026-10-09T21:49:10+00:00", reason="window did not open",
        )
        pending = session_shutdown(name="pending timers")
        pending["progress"] = record_store.SessionRestoredTimersPending(
            kind="timers pending", at="2026-10-09T21:49:10+00:00",
            timers=[record_store.PendingTimer(instance="showrunner-demo", reason="notifier unavailable")],
        )
        manual = session_shutdown(name="manual session")
        manual["progress"] = record_store.SessionNeedsManualRestart(
            kind="manual restart", command="claude --resume session-id",
        )
        ready = session_shutdown(name="ready session")
        ready["progress"] = record_store.SessionReadyToStop(kind="ready", at=ready["requested_at"])
        ready["where"] = record_store.WhereSaid(kind="said", text="finish the launch tests", at=ready["requested_at"])
        ready["settle_message"] = record_store.SettleMessageSent(kind="sent", at=ready["requested_at"])
        waiting = session_shutdown(name="waiting session")
        waiting["settle_message"] = record_store.SettleMessageQueued(
            kind="queued", at=waiting["requested_at"], reason="no live session",
        )
        for item in (failed, pending, manual, ready, waiting):
            self.assertTrue(record_store.create_session(item))
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        expected = [
            "  failed launch: restart failed — window did not open",
            "  manual session: manual restart — in a terminal on natedev: claude --resume session-id",
            "  pending timers: timers pending — showrunner-demo: notifier unavailable",
            "  ready session: ready — finish the launch tests · message sent",
            "  waiting session: waiting · message queued: no live session",
        ]
        self.assertEqual([line for line in output.getvalue().splitlines() if line in expected], expected)
        self.assertNotIn("shutdown restart partial", output.getvalue())

    def test_status_prints_observed_readiness_causes_for_waiting_and_ready_sessions(self) -> None:
        for kind in ("waiting", "ready"):
            with self.subTest(kind=kind):
                saved = session_shutdown(name=kind)
                if kind == "ready":
                    saved["progress"] = {"kind": "ready", "at": saved["requested_at"]}
                self.assertTrue(record_store.create_session(saved))
                observed = inventory.parse_session({**saved["session"], "status": "busy",
                    "checkout": {"kind": "git", "path": "/tmp/checkout", "head": {"kind": "branch", "name": "work"},
                                 "upstream": {"kind": "tracking", "ahead": 2}, "dirty": []},
                    "codex_servers": [{"run_dir": "/tmp/run", "pid": 5000, "busy_workers": ["worker"]}]}, "observed")
                report = empty_inventory()
                report["sessions"] = [observed]
                output = io.StringIO()
                with patch.object(shutdown, "inventory", return_value=report), patch.object(settle, "_showing_form", return_value=True), patch.object(settle, "merge_in_progress", return_value=True), redirect_stdout(output):
                    self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
                self.assertIn(f"  {kind}: {kind} — busy, showing a form, ahead 2 on work, merge in progress, Codex worker worker is still running", output.getvalue())
                record_store.archive_session(LOGIN, saved["session"]["session_id"])

    def test_status_keeps_unobserved_sessions_without_a_readiness_detail(self) -> None:
        saved = session_shutdown(name="missing")
        self.assertTrue(record_store.create_session(saved))
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), patch.object(settle, "_showing_form", side_effect=RuntimeError("cannot observe")), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn("  missing: waiting\n", output.getvalue())

    def test_status_usage_limit_held_owner_shows_busy_worker_without_form_and_offers_actions(self) -> None:
        saved = session_shutdown(name="Held")
        saved["progress"] = record_store.SessionHeldByUsageLimit(kind="held by usage limit", at=saved["requested_at"])
        saved["session"]["status"] = "idle"
        self.assertTrue(record_store.create_session(saved))
        worker = inventory.parse_session({**saved["session"], "kind": "worker", "session_id": "worker",
            "name": "worker", "status": "busy", "host": {"kind": "unknown"},
            "owner": {"kind": "director", "session_id": saved["session"]["session_id"]}}, "worker")
        report = empty_inventory()
        report["sessions"] = [saved["session"], worker]
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=report), patch.object(settle, "_showing_form", return_value=True) as form, patch.object(settle, "merge_in_progress", return_value=False), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn("  Held: held by usage limit — Codex worker worker is still running\n", output.getvalue())
        self.assertNotIn("showing a form", output.getvalue())
        form.assert_not_called()
        self.assertIn(resume_conductor_line(saved["session"]["name"]) + "\n", output.getvalue())
        self.assertIn(action_line("take out", "cancel", "Held") + "\n", output.getvalue())
        self.assertIn(action_line("stop now", "now", "Held") + "\n", output.getvalue())
        self.assertNotIn("  bring back:", output.getvalue())

    def test_status_reports_owned_busy_workers_and_a_queued_message_once(self) -> None:
        saved = session_shutdown(name="owner")
        saved["settle_message"] = {"kind": "queued", "at": saved["requested_at"], "reason": "delivery pending"}
        self.assertTrue(record_store.create_session(saved))
        worker = inventory.parse_session({**saved["session"], "kind": "worker", "session_id": "worker",
            "name": "worker", "status": "busy", "host": {"kind": "unknown"},
            "owner": {"kind": "director", "session_id": saved["session"]["session_id"]}}, "worker")
        report = empty_inventory()
        report["sessions"] = [saved["session"], worker]
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=report), patch.object(settle, "_showing_form", return_value=False), patch.object(settle, "merge_in_progress", return_value=False), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        line = next(line for line in output.getvalue().splitlines() if line.startswith("  owner: waiting"))
        self.assertIn("Codex worker worker is still running", line)
        self.assertEqual(line.count("message queued: delivery pending"), 1)

    def test_status_uses_reported_peer_activity_without_local_process_probes(self) -> None:
        saved = session_shutdown(machine="Mac", name="peer")
        observed = inventory.parse_session({**saved["session"], "status": "busy"}, "peer")
        peer = empty_inventory("Mac")
        peer["sessions"] = [observed]
        def remote(arguments: list[str]) -> tuple[int, str]:
            return 0, json.dumps(peer if arguments[0] == "status" else [saved])
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), patch.object(shutdown, "run_remote", side_effect=remote), patch.object(settle, "_showing_form", return_value=False) as form, patch.object(settle, "merge_in_progress", return_value=False) as merge, redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN]), 0)
        self.assertIn("  peer: waiting — busy", output.getvalue())
        form.assert_not_called()
        merge.assert_not_called()

    def test_status_and_records_read_old_partial_and_slot_sessions_oldest_first(self) -> None:
        unfinished = complete_record()
        unfinished["state"] = "restart partial"
        unfinished["entries"][0]["session"]["name"] = "failed launch"
        unfinished["entries"][0]["progress"] = record_store.SessionRestartFailed(
            kind="restart failed", at=unfinished["requested_at"], reason="window did not open",
        )
        write_old(unfinished)
        _ = move_old(LOGIN)
        newer = complete_record()
        newer["entries"][0]["session"]["session_id"] = "another-session"
        newer["requested_at"] = "2026-10-10T01:02:03+00:00"
        write_old(newer)
        status_output = io.StringIO()
        records_output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()):
            with redirect_stdout(status_output):
                self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
            with redirect_stdout(records_output):
                self.assertEqual(shutdown.main(["records", "--here"]), 0)
        expected = [
            "  failed launch: restart failed — window did not open", "  shutdown: waiting",
        ]
        self.assertEqual([line for line in status_output.getvalue().splitlines() if line in expected], expected)
        self.assertEqual(records_output.getvalue(),
            "natedev claude2 failed launch: restart failed since 2026-10-09 14:49:10 PDT\n"
            + "natedev claude2 shutdown: waiting since 2026-10-09 18:02:03 PDT\n",
        )

    def test_status_filters_remote_records_to_the_selected_account(self) -> None:
        matching = session_shutdown(machine="Mac", name="mine")
        matching["login"] = LOGIN.upper()
        other = session_shutdown(machine="Mac", name="other account")
        other["login"] = "other@example.com"
        os.environ["SHUTDOWN_STATUS_RECORD_OUTPUT"] = json.dumps([other, matching])
        result, output, error = self.run_plain_status_with_ssh()
        self.assertEqual((result, error), (0, ""))
        self.assertIn("  mine: waiting\n", output)
        self.assertNotIn("other account", output)

    def test_records_json_reads_old_sessions_without_changing_the_slot(self) -> None:
        old = complete_record()
        second = complete_record()["entries"][0]
        second["session"]["session_id"] = "second-session"
        second["session"]["name"] = "second"
        old["entries"].append(second)
        write_old(old)
        path = self.state / LOGIN / "record.json"
        before = path.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["records", "--json", "--here"]), 0)
        self.assertEqual(json.loads(output.getvalue()), record_store.session_records(LOGIN))
        self.assertEqual(len(record_store.session_records(LOGIN)), 2)
        self.assertEqual(path.read_bytes(), before)

    def test_records_text_uses_los_angeles_time(self) -> None:
        output = io.StringIO()
        with (
            patch.object(shutdown, "all_session_records", return_value=[session_shutdown()]),
            redirect_stdout(output),
        ):
            result = shutdown.main(["records", "--here"])

        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue(),
            "natedev claude2 shutdown: waiting since 2026-10-09 14:49:10 PDT\n",
        )

    def test_records_json_here_prints_local_records(self) -> None:
        record = session_shutdown()
        output = io.StringIO()
        with (
            patch.object(shutdown, "all_session_records", return_value=[record]),
            redirect_stdout(output),
        ):
            result = shutdown.main(["records", "--json", "--here"])

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), [record])

    def test_records_lists_one_object_per_session_on_both_machines(self) -> None:
        local = session_shutdown(name="local")
        remote = session_shutdown(machine="Mac", name="remote")
        self.assertTrue(record_store.create_session(local))
        local["trail"] = [record_store.ProgressStep(at=local["requested_at"], kind="waiting", detail="")]
        remote_wire = {key: value for key, value in remote.items() if key != "trail"}
        output = io.StringIO()
        with (
            patch.object(shutdown, "run_remote", return_value=(0, json.dumps([remote_wire]))) as read_remote,
            redirect_stdout(output),
        ):
            self.assertEqual(shutdown.main(["records", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), [local, remote])
        read_remote.assert_called_once_with(["records", "--json", "--here"])

    def test_records_refuses_an_unreadable_local_session(self) -> None:
        broken = self.state / LOGIN / "sessions" / "broken.json"
        broken.parent.mkdir(parents=True)
        _ = broken.write_text("{", encoding="utf-8")
        output = io.StringIO()
        error = io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(shutdown.main(["records", "--here"]), 3)
        self.assertEqual(output.getvalue(), "")
        self.assertIn(str(broken), error.getvalue())

    def test_help_hides_internal_records_while_records_still_runs(self) -> None:
        help_output = io.StringIO()
        with (
            redirect_stdout(help_output),
            self.assertRaises(SystemExit) as exit_context,
        ):
            _ = shutdown.main(["--help"])

        self.assertEqual(exit_context.exception.code, 0)
        self.assertNotIn("records", help_output.getvalue())
        self.assertNotIn("holds", help_output.getvalue())
        self.assertNotIn("SUPPRESS", help_output.getvalue())

        records_output = io.StringIO()
        with (
            patch.object(shutdown, "all_session_records", return_value=[]),
            redirect_stdout(records_output),
        ):
            result = shutdown.main(["records", "--here"])

        self.assertEqual(result, 0)
        self.assertEqual(records_output.getvalue(), "")

    def test_reachable_remote_failure_is_unavailable_not_unreachable(self) -> None:
        result, output, error = self.run_status(3, "remote inventory failed")

        expected = {
            "machine": "Mac",
            "status": "unavailable",
            "rc": 3,
            "reason": "remote inventory failed",
        }
        self.assertEqual(result, 0)
        self.assertEqual(
            output,
            json.dumps([empty_recovery_inventory(), expected], sort_keys=True) + "\n",
        )
        self.assertEqual(error, "")

        _, plain, _ = self.run_status(3, "remote inventory failed", as_json=False)
        self.assertIn("Mac: unavailable (rc 3)\n", plain)

    def test_transport_failure_is_unreachable(self) -> None:
        result, output, error = self.run_status(255, "")

        self.assertEqual(result, 0)
        self.assertEqual(
            output,
            json.dumps(
                [empty_recovery_inventory(), {"machine": "Mac", "unreachable": True}],
                sort_keys=True,
            )
            + "\n",
        )
        self.assertEqual(error, "")

        _, plain, _ = self.run_status(255, "", as_json=False)
        self.assertIn("Mac: unreachable\n", plain)


    def test_status_adds_actions_and_no_conductor_line_for_waiting_owner(self) -> None:
        saved = session_shutdown(name="Owner")
        self.assertTrue(record_store.create_session(saved))
        report = empty_inventory()
        report["sessions"] = [saved["session"]]
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=report), patch.object(settle, "_showing_form", return_value=False), patch.object(settle, "merge_in_progress", return_value=False), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn("  Owner: waiting\n", output.getvalue())
        self.assertIn(resume_conductor_line(saved["session"]["name"]) + "\n", output.getvalue())
        self.assertIn(action_line("take out", "cancel", "Owner") + "\n", output.getvalue())
        self.assertIn(action_line("stop now", "now", "Owner") + "\n", output.getvalue())
        self.assertNotIn(action_line("bring back", "restart", "Owner") + "\n", output.getvalue())
        self.assertFalse((self.state / LOGIN / "conduct.lock").exists())

    def test_status_running_conductor_has_actions_without_resume_warning(self) -> None:
        saved = session_shutdown(name="Ready")
        saved["progress"] = {"kind": "ready", "at": saved["requested_at"]}
        self.assertTrue(record_store.create_session(saved))
        output = io.StringIO()
        with record_store.conductor_lock(LOGIN), patch.object(shutdown, "inventory", return_value=empty_inventory()), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertNotIn("no conductor running", output.getvalue())
        self.assertIn(action_line("take out", "cancel", "Ready") + "\n", output.getvalue())
        self.assertIn(action_line("stop now", "now", "Ready") + "\n", output.getvalue())

    def test_status_released_conductor_lock_is_not_a_running_conductor(self) -> None:
        saved = session_shutdown(name="Owner")
        self.assertTrue(record_store.create_session(saved))
        with record_store.conductor_lock(LOGIN):
            pass
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn("  no conductor running; in a terminal on ", output.getvalue())

    def test_status_unreadable_conductor_lock_reports_unavailable_without_resume_claim(self) -> None:
        saved = session_shutdown(name="Owner")
        self.assertTrue(record_store.create_session(saved))
        (self.state / LOGIN / "conduct.lock").mkdir()
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn("conductor ownership unavailable", output.getvalue())
        self.assertNotIn("no conductor running", output.getvalue())

    def test_status_stopped_and_failed_owners_offer_only_applicable_actions(self) -> None:
        stopped, failed, stopping = [session_shutdown(name=name) for name in ("Stopped", "Failed", "Stopping")]
        stopped["progress"] = {"kind": "stopped", "at": stopped["requested_at"]}
        failed["progress"] = {"kind": "stop failed", "at": failed["requested_at"], "reason": "still live"}
        stopping["progress"] = {"kind": "stopping", "at": stopping["requested_at"]}
        for saved in (stopped, failed, stopping):
            self.assertTrue(record_store.create_session(saved))
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertEqual([line for line in output.getvalue().splitlines() if any(label in line for label in ("  take out:", "  stop now:", "  bring back:"))], [
            action_line("take out", "cancel", "Failed"),
            action_line("stop now", "now", "Failed"),
            action_line("bring back", "restart", "Failed, Stopped"),
        ])

    def test_status_peer_ownership_observes_held_released_absent_and_unreadable_locks(self) -> None:
        for state in ("held", "released", "absent", "unreadable"):
            with self.subTest(state=state), ExitStack() as contexts:
                peer_state = self.root / state
                saved = session_shutdown(machine="Mac", name=f"Peer_{state}")
                with patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(peer_state)}):
                    self.assertTrue(record_store.create_session(saved))
                    if state == "held":
                        _ = contexts.enter_context(record_store.conductor_lock(LOGIN))
                    elif state == "released":
                        with record_store.conductor_lock(LOGIN):
                            pass
                    elif state == "unreadable":
                        (peer_state / LOGIN / "conduct.lock").mkdir()
                report = empty_inventory("Mac")
                report["sessions"] = [saved["session"]]
                def remote(arguments: list[str]) -> tuple[int, str]:
                    output = io.StringIO()
                    with patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(peer_state)}), patch.object(shutdown, "inventory", return_value=report), redirect_stdout(output):
                        status = shutdown.main(arguments)
                    return status, output.getvalue()
                output = io.StringIO()
                with patch.object(shutdown, "inventory", return_value=empty_inventory()), patch.object(shutdown, "run_remote", side_effect=remote), patch.object(shutdown, "other_machine", return_value="Mac"), redirect_stdout(output):
                    self.assertEqual(shutdown.main(["status", LOGIN]), 0)
                lines = output.getvalue()
                self.assertIn(action_line("take out", "cancel", f"Peer_{state}", "Mac") + "\n", lines)
                if state in {"absent", "released"}:
                    self.assertIn("  no conductor running; in a terminal on ", lines)
                else:
                    self.assertNotIn("no conductor running", lines)
                if state == "unreadable":
                    self.assertIn("conductor ownership unavailable", lines)
                if state == "absent":
                    self.assertFalse((peer_state / LOGIN / "conduct.lock").exists())

    def test_status_unreachable_peer_ownership_is_unavailable_even_with_readable_records(self) -> None:
        saved = session_shutdown(machine="Mac", name="Peer")
        report = empty_inventory("Mac")
        report["sessions"] = [saved["session"]]
        def remote(arguments: list[str]) -> tuple[int, str]:
            if arguments[0] == "status":
                return 0, json.dumps(report)
            if arguments[0] == "records":
                return 0, json.dumps([saved])
            return 255, ""
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), patch.object(shutdown, "run_remote", side_effect=remote), patch.object(shutdown, "other_machine", return_value="Mac"), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN]), 0)
        self.assertIn("conductor ownership unavailable", output.getvalue())
        self.assertNotIn("no conductor running", output.getvalue())

    def test_status_actions_end_each_machines_block(self) -> None:
        local, peer = session_shutdown(name="Local"), session_shutdown(machine="Mac", name="Peer")
        self.assertTrue(record_store.create_session(local))
        report = empty_inventory("Mac")
        def remote(arguments: list[str]) -> tuple[int, str]:
            if arguments[0] == "status":
                return 0, json.dumps(report)
            if arguments[0] == "records":
                return 0, json.dumps([peer])
            return 255, ""
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), patch.object(shutdown, "run_remote", side_effect=remote), patch.object(shutdown, "other_machine", return_value="Mac"), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN]), 0)
        lines = output.getvalue().splitlines()
        self.assertLess(lines.index(action_line("stop now", "now", "Local")), lines.index("Mac:"))
        self.assertGreater(lines.index(action_line("stop now", "now", "Peer", "Mac")), lines.index("Mac:"))

    def test_status_whole_machine_blocks_group_actions_and_report_conductor_once(self) -> None:
        local: list[SessionShutdown] = []
        remote: list[SessionShutdown] = []
        for machine, records in (("natedev", local), ("Mac", remote)):
            for name in ("bevy", "failed", "hana", "nixos"):
                saved = session_shutdown(machine=machine, name=name)
                if name == "bevy":
                    saved["progress"] = {"kind": "stopped", "at": saved["requested_at"]}
                elif name == "failed":
                    saved["progress"] = {"kind": "stop failed", "at": saved["requested_at"], "reason": "still live"}
                records.append(saved)
                if machine == "natedev":
                    self.assertTrue(record_store.create_session(saved))
        def peer(arguments: list[str]) -> tuple[int, str]:
            if arguments[0] == "records":
                return 0, json.dumps(remote)
            if arguments[0] == "status":
                return 0, json.dumps(empty_inventory("Mac"))
            self.assertEqual(arguments, ["conductor-status", LOGIN, "--json", "--here"])
            return 0, json.dumps({"kind": "not running"})
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), patch.object(shutdown, "run_remote", side_effect=peer), patch.object(shutdown, "other_machine", return_value="Mac"), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN]), 0)
        expected = [LOGIN + " · " + LOGIN]
        for machine in ("natedev", "Mac"):
            expected.extend([
                machine + ":",
                "  no sessions on claude2",
                "  bevy: stopped",
                *(["    14:49:10 stopped"] if machine == "natedev" else []),
                "  failed: stop failed — still live",
                *(["    14:49:10 stop failed still live"] if machine == "natedev" else []),
                "  hana: waiting",
                *(["    14:49:10 waiting"] if machine == "natedev" else []),
                "  nixos: waiting",
                *(["    14:49:10 waiting"] if machine == "natedev" else []),
                resume_conductor_line("hana, nixos", machine),
                action_line("take out", "cancel", "failed, hana, nixos", machine),
                action_line("stop now", "now", "failed, hana, nixos", machine),
                action_line("bring back", "restart", "bevy, failed", machine),
                *(["  recovery snapshot: no successful snapshot recorded"] if machine == "natedev" else []),
            ])
        self.assertEqual(output.getvalue().splitlines(), expected)

    def test_status_unavailable_ownership_is_printed_once_before_grouped_actions(self) -> None:
        for name in ("hana", "nixos"):
            self.assertTrue(record_store.create_session(session_shutdown(name=name)))
        output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()), patch.object(shutdown, "conductor_status", return_value=record_store.ConductorUnavailable("cannot read lock")), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            LOGIN + " · " + LOGIN,
            "natedev:",
            "  no sessions on claude2",
            "  hana: waiting",
            "    14:49:10 waiting",
            "  nixos: waiting",
            "    14:49:10 waiting",
            "  conductor ownership unavailable: cannot read lock",
            action_line("take out", "cancel", "hana, nixos"),
            action_line("stop now", "now", "hana, nixos"),
            "  recovery snapshot: no successful snapshot recorded",
        ])


if __name__ == "__main__":
    _ = unittest.main()
