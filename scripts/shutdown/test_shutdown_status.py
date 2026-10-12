"""Regression tests for shutdown status rendering and account resolution."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from test_support import ShutdownSendLeaf, ShutdownTestCase
import shutdown
import record as record_store
from account import UnreadableAccount
from inventory import Inventory
from record import SessionShutdown, ShutdownRecord


LOGIN = "owner@example.com"


def empty_inventory(machine: str = "natedev") -> Inventory:
    return {
        "machine": machine,
        "login": LOGIN,
        "label": "claude 2",
        "sessions": [],
        "unattributed": [],
    }


def complete_session(*, host: object) -> dict[str, object]:
    return {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "pid": 1234,
        "proc_start": "process-start",
        "name": "shutdown",
        "cwd": "/tmp/checkout",
        "kind": "top-level",
        "status": "idle",
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


def complete_record(machine: str = "natedev") -> ShutdownRecord:
    return cast(
        ShutdownRecord,
        cast(
            object,
            {
                "login": LOGIN,
                "label": "claude 2",
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
        login=LOGIN, label="claude 2", machine=machine,
        requested_at=old["requested_at"], requested_by=old["requested_by"],
        hold="account", then="stop", force=old["force"], alerts="user",
        session=item["session"], seats=[], timers=item["timers"],
        settle_message=item["settle_message"], where=item["where"],
        progress=item["progress"], stop_issues=item["stop_issues"],
    )


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
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
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
else:
    print(os.environ["SHUTDOWN_STATUS_INVENTORY_OUTPUT"])
    print("rc=0")
""",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        environment = {
            **os.environ,
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
        _ = (self.notes / f"{label}.md").write_text(
            f"---\nlogin: {login}\nstate: active\n---\n",
            encoding="utf-8",
        )

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
        self.assertIn("natedev:\n  no sessions on claude 2\n", standard_output.getvalue())

    def test_account_label_resolution_accepts_only_claude_notes(self) -> None:
        self.write_note("claude 2", LOGIN)
        self.write_note("codex 2", "codex@example.com")

        def inventory_for(login: str) -> Inventory:
            self.assertEqual(login, LOGIN)
            return empty_inventory()

        with (
            patch.object(shutdown, "inventory", side_effect=inventory_for),
            redirect_stdout(io.StringIO()),
        ):
            result = shutdown.main(["status", "claude 2", "--json", "--here"])
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
            result = shutdown.main(["status", "codex 2", "--here"])
        self.assertEqual(result, 2)
        self.assertIn("unknown account codex 2", errors.getvalue())

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
            "shutdown: unknown account claude2: give a note label such as claude 2, or a login\n",
        )

    def test_unreadable_own_account_exits_two(self) -> None:
        errors = io.StringIO()
        with (
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
                "label": "claude 2",
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
            json.dumps([empty_inventory(), expected], sort_keys=True) + "\n",
        )
        self.assertEqual(error, "")

    def test_remote_inventory_with_malformed_session_is_unavailable_with_reason(self) -> None:
        response = json.dumps(
            {
                "machine": "Mac",
                "login": LOGIN,
                "label": "claude 2",
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
            json.dumps([empty_inventory(), expected], sort_keys=True) + "\n",
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

    def test_status_renders_local_and_remote_stop_issues_under_their_sessions(self) -> None:
        at = "2026-10-09T21:49:10+00:00"
        local = complete_record()
        local["entries"][0]["session"]["name"] = "local"
        local["entries"][0]["stop_issues"] = [
            record_store.NotReadyToStop(kind="not ready to stop", at=at, status="busy", progress="ready"),
            record_store.StillRunningAfterStop(kind="still running", at=at, reason="alive after two SIGTERMs"),
            record_store.AccountUnreadableAtStop(kind="account unreadable", at=at),
            record_store.SeatStillLive(kind="seat still live", at=at),
        ]
        remote_record = session_shutdown(machine="Mac", name="remote")
        remote_record["stop_issues"] = [
            record_store.CodexServerLeftRunning(
                kind="codex server left running", at=at, run_dir="/tmp/run", cause="stop not confirmed",
            ),
            record_store.UnitTmuxLeftRunning(
                kind="unit tmux session left running", at=at, tmux_session="demo-unit", cause="owner identity lost",
            ),
        ]
        record_store.create(local)
        os.environ["SHUTDOWN_STATUS_RECORD_OUTPUT"] = json.dumps([remote_record])
        result, output, error = self.run_plain_status_with_ssh()
        self.assertEqual((result, error), (0, ""))
        expected = [
            "  local: waiting",
            "    not stopped: busy, ready",
            "    still running: alive after two SIGTERMs",
            "    not stopped: account unreadable",
            "    not stopped: owner or seat still live",
            "  remote: waiting",
            "    Codex server /tmp/run left running: stop not confirmed",
            "    tmux session demo-unit left running: owner identity lost",
        ]
        self.assertEqual(output.splitlines()[-len(expected):], expected)

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
        self.assertEqual(output.getvalue().splitlines()[-5:], [
            "  failed launch: restart failed — window did not open",
            "  manual session: manual restart — run: claude --resume session-id",
            "  pending timers: timers pending — showrunner-demo: notifier unavailable",
            "  ready session: ready — finish the launch tests · message sent",
            "  waiting session: waiting · message queued: no live session",
        ])
        self.assertNotIn("shutdown restart partial", output.getvalue())

    def test_status_and_records_read_old_parked_and_slot_sessions_oldest_first(self) -> None:
        unfinished = complete_record()
        unfinished["state"] = "restart partial"
        unfinished["entries"][0]["session"]["name"] = "failed launch"
        unfinished["entries"][0]["progress"] = record_store.SessionRestartFailed(
            kind="restart failed", at=unfinished["requested_at"], reason="window did not open",
        )
        record_store.create(unfinished)
        _ = record_store.park(LOGIN)
        newer = complete_record()
        newer["entries"][0]["session"]["session_id"] = "another-session"
        newer["requested_at"] = "2026-10-10T01:02:03+00:00"
        record_store.create(newer)
        status_output = io.StringIO()
        records_output = io.StringIO()
        with patch.object(shutdown, "inventory", return_value=empty_inventory()):
            with redirect_stdout(status_output):
                self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
            with redirect_stdout(records_output):
                self.assertEqual(shutdown.main(["records", "--here"]), 0)
        self.assertEqual(status_output.getvalue().splitlines()[-2:], [
            "  failed launch: restart failed — window did not open", "  shutdown: waiting",
        ])
        self.assertEqual(records_output.getvalue(),
            "natedev claude 2 failed launch: restart failed since 2026-10-09 14:49:10 PDT\n"
            + "natedev claude 2 shutdown: waiting since 2026-10-09 18:02:03 PDT\n",
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
        record_store.create(old)
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
            "natedev claude 2 shutdown: waiting since 2026-10-09 14:49:10 PDT\n",
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
        output = io.StringIO()
        with (
            patch.object(shutdown, "run_remote", return_value=(0, json.dumps([remote]))) as read_remote,
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
            json.dumps([empty_inventory(), expected], sort_keys=True) + "\n",
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
                [empty_inventory(), {"machine": "Mac", "unreachable": True}],
                sort_keys=True,
            )
            + "\n",
        )
        self.assertEqual(error, "")

        _, plain, _ = self.run_status(255, "", as_json=False)
        self.assertIn("Mac: unreachable\n", plain)


if __name__ == "__main__":
    _ = unittest.main()
