"""Regression tests for shutdown status rendering and account resolution."""

from __future__ import annotations

import sys
from pathlib import Path


from scripts.whoami.note_file import note_name

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import cast, final, override
from collections.abc import Generator
from zoneinfo import ZoneInfo
from unittest.mock import patch

from scripts.agents.test_support import ShutdownSendLeaf, ShutdownTestCase
from scripts.production.staff_test_support import fixture_member, fixture_worker
from scripts.agents import shutdown, restart, shutdown_record, stop_resume, inventory
from scripts.herdr import herdr, worker_panes
from scripts.production import staff, session_staff, completion_receipt, unit_config, worker
from scripts.production.production_doc import Production
from scripts.whoami.account import UnreadableAccount
from scripts.agents.inventory import Inventory
from scripts.agents.stop_results import ClaudeStartState


LOGIN = "owner@example.com"


def action_line(label: str, verb: str, names: str, machine: str = "natedev") -> str:
    identities = ",".join(name + "-id" for name in names.split(", "))
    return (f"  {label}: in a terminal on {machine}: ~/.claude/scripts/lib/py "
        f"~/.claude/scripts/agents/shutdown.py {verb} {LOGIN} --here --only {identities} · {names}")


def empty_inventory(machine: str = "natedev") -> Inventory:
    return {
        "machine": machine,
        "login": LOGIN,
        "label": "claude2",
        "sessions": [],
        "unattributed": [],
    }


def empty_recovery_inventory() -> inventory.Inventory:
    return empty_inventory()


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
        "timers": [],
    }


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


@final
class ShutdownStatusTests(ShutdownTestCase):
    def test_readiness_names_a_running_worker_and_waits_until_its_turn_ends(self) -> None:
        parent = fixture_member("unit", name="readiness-unit",
                                parent=fixture_member("showrunner", name="readiness-owner").id)
        row = fixture_worker("test-worker", parent.id)
        record = staff.local_snapshot()
        busy = worker_panes.OpenWorker(row, "w1:p1", "agents", herdr.AgentStatus.WORKING)
        idle = worker_panes.OpenWorker(row, "w1:p1", "agents", herdr.AgentStatus.IDLE)
        with (patch.object(session_staff, "pane_parents", return_value=[parent]),
              patch.object(worker_panes, "observe_parents", side_effect=[{parent.id: [busy]}, {parent.id: [idle]}]),
              patch("scripts.agents.shutdown.time.sleep") as delay,
              redirect_stdout(io.StringIO()) as output):
            shutdown.wait_for_workers(record)
        delay.assert_called_once_with(1)
        self.assertEqual(output.getvalue().splitlines(),
                         [f"{row.name}: {row.current_session}: waiting for worker turn"])

    def test_stop_reports_each_unresolved_worker_and_the_director_result(self) -> None:
        parent = staff.StaffRow(staff.StaffId("director"), "unit", "director", staff.STAFF_ROOT,
                                current_session=staff.StaffId("director-session"), unit_type=staff.UnitDetails("direct"))
        row = staff.StaffRow(staff.StaffId("worker"), "worker", "impl", staff.ReportsTo(parent.id),
                             current_session=staff.StaffId("worker-session"))
        record = staff.StaffSnapshot((parent, row))
        def observe(_parent: staff.StaffId, _record: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            print("anomaly: impl: terminal changed", file=sys.stderr)
            return [worker_panes.UncertainWorker(row, "terminal changed")]
        for result, expected in ((stop_resume.StopFailed("director-session", "director stop failed"), "director stop failed"),
                                 (stop_resume.NotRunning("director-session"), "unchanged")):
            with (self.subTest(result=result), patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(worker_panes, "worker_run", return_value=self.root / "run"),
                  patch.object(worker_panes, "observe", side_effect=observe),
                  patch.object(stop_resume, "stop", return_value=result),
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(shutdown.main(["stop", "director"]), 1)
            text = output.getvalue() + errors.getvalue()
            self.assertEqual(text.count("anomaly: impl: terminal changed"), 1, text)
            self.assertIn(expected, text)

    def test_live_record_pruning_precedes_worker_window(self) -> None:
        row = fixture_member("producer", name="held-live")
        record = staff.StaffSnapshot((row,))
        shutdown_record.save([str(row.id), "down-row"])

        def window(selected: staff.StaffSnapshot) -> None:
            self.assertIs(selected, record)
            self.assertEqual(shutdown_record.read(), ["down-row"])

        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (123,))),
              patch.object(shutdown, "wait_for_workers", side_effect=window)):
            self.assertEqual(shutdown.main(["held-sessions"]), 0)

    def test_scoped_held_sessions_merges_selected_rows(self) -> None:
        first = fixture_member("producer", name="first")
        other = fixture_member("producer", name="other")
        record = staff.StaffSnapshot((first, other))
        shutdown_record.save([str(other.id), "earlier"])
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([first])) as live,
              patch.object(shutdown, "wait_for_workers")):
            self.assertEqual(shutdown.main(["held-sessions", "--only", str(first.id)]), 0)
        self.assertEqual(shutdown_record.read(), [str(other.id), "earlier", str(first.id)])
        self.assertEqual(live.call_args.kwargs["selection"], restart.SelectedRestartRows(frozenset({first.id})))

    def test_direct_resume_removes_only_the_confirmed_live_row(self) -> None:
        row = fixture_member("producer", name="resume-target")
        for reference in (row.name, str(row.id), str(row.current_session)):
            for state in (ClaudeStartState.STARTED, ClaudeStartState.ALREADY_RUNNING):
                with self.subTest(reference=reference, state=state):
                    shutdown_record.save(["earlier", str(row.id)])
                    result = stop_resume.ClaudeUp(row.current_session, "w1:p1", "claude2", start_state=state)
                    with patch.object(stop_resume, "_resume_once", return_value=result):
                        self.assertEqual(stop_resume.resume(reference), result)
                    self.assertEqual(shutdown_record.read(), ["earlier"])
        shutdown_record.save(["earlier", str(row.id)])
        failed = stop_resume.ResumeFailed(row.current_session, "fixture failure")
        with patch.object(stop_resume, "_resume_once", return_value=failed):
            self.assertEqual(stop_resume.resume(str(row.id)), failed)
        self.assertEqual(shutdown_record.read(), ["earlier", str(row.id)])

    def test_named_resume_cleanup_precedes_a_stop_after_the_row_lock_releases(self) -> None:
        row = fixture_member("producer", name="resume-race")
        shutdown_record.save([str(row.id)])
        operation = staff.row_operation
        stopped_again: list[str] = []
        depth = 0

        @contextmanager
        def lock(reference: str) -> Generator[None]:
            nonlocal depth
            depth += 1
            with operation(reference):
                yield
            depth -= 1
            if depth == 0 and not stopped_again:
                stopped_again.append(reference)
                with operation(reference):
                    shutdown_record.update(started=[], pending=[reference])

        with (patch.object(stop_resume, "_resume_once", return_value=
                           stop_resume.ClaudeUp(row.current_session, "w1:p1", "claude2")),
              patch.object(staff, "row_operation", side_effect=lock)):
            self.assertEqual(shutdown.main(["resume", str(row.id)]), 0)
        self.assertEqual(stopped_again, [str(row.id)])
        self.assertEqual(shutdown_record.read(), [str(row.id)])

    def test_retired_resume_clears_only_a_proven_native_session(self) -> None:
        owner = fixture_member("showrunner", name="recovery-owner")
        row = fixture_member("unit", name="recovery-unit", parent=owner.id,
                             worktree=staff.PresentWorktree("natedev", self.root / "unit"))
        production = Production(self.root / "production.md", owner.name, "production", self.root,
                                self.root / "log.md", ZoneInfo("UTC"), owner)
        unit = shutdown.RetiredUnitRecovery(production, row.name, self.root / "unit", row.name,
                                            unit_config.StandbyPlan(), row.current_session)
        receipt = completion_receipt.OwnedReceipt(row, {"session_id": row.current_session,
                                                       "doc": str(production.doc), "unit": row.name})
        for already, proven in ((False, False), (False, True), (True, True)):
            with self.subTest(already=already, proven=proven):
                shutdown_record.save(["earlier", str(row.id)])
                with (patch.object(shutdown, "recovery_find_unit", return_value=
                                   shutdown.NoRecoveryRequired(production, row.name) if already else unit),
                      patch.object(shutdown, "recovery_resume_record", return_value=self.root / "resume"),
                      patch.object(completion_receipt, "read_receipt", return_value=receipt),
                      patch.object(shutdown, "recovery_refusals", return_value=[]),
                      patch.object(shutdown, "recovery_make_worktree"),
                      patch.object(unit_config, "restore"), patch.object(unit_config, "discard_parked"),
                      patch.object(staff, "restore_unit", return_value=row),
                      patch.object(shutdown, "recovery_launch"),
                      patch.object(shutdown, "recovery_resumed_session_proven", return_value=proven),
                      patch.object(completion_receipt, "write_receipt"),
                      patch.object(worker, "restore", return_value=worker.ExitCode.OK)):
                    self.assertEqual(shutdown.main(["resume", str(row.id), "--retired"]), 0 if proven else 1)
                self.assertEqual(shutdown_record.read(), ["earlier"] if proven else ["earlier", str(row.id)])

    def test_paused_resume_removes_the_row_only_after_a_delivered_continuation(self) -> None:
        owner = fixture_member("showrunner", name="resume-owner")
        row = fixture_member("unit", name="resume-unit", parent=owner.id)
        for delivered in (True, False):
            with self.subTest(delivered=delivered):
                _ = staff.pause(str(row.id), "maintenance")
                shutdown_record.save(["earlier", str(row.id)])
                outcome = {"kind": "sent"} if delivered else {"kind": "unconfirmed", "reason": "fixture failure"}
                with (patch.object(stop_resume, "running_on_account",
                                   return_value=stop_resume.ClaudeUp(row.current_session, "w1:p1", "claude2")),
                      patch.object(shutdown, "_message", return_value=outcome)):
                    self.assertEqual(shutdown.main(["resume", str(row.id)]), 0 if delivered else 1)
                self.assertEqual(shutdown_record.read(), ["earlier"] if delivered else ["earlier", str(row.id)])


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


if __name__ == "__main__":
    _ = unittest.main()
