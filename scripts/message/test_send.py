"""send.py's keys, queue, log and relay judgement, with delivery stubbed."""

from __future__ import annotations

import io
import json
import os
import socket
import shlex
import sys
import tempfile
import unittest
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import send
import codex_daemon
import record as shutdown_record
import restart
import roster
import settle
from user_action import REFUSED_BEFORE_SENDING, ActionRequired, NoActionRequired


def stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


INIT: dict[str, object] = {"type": "system", "subtype": "init", "permissionMode": "auto"}


def call(text: str, tool_id: str = "t1") -> dict[str, object]:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "SendMessage", "input": {"to": "x", "message": text}}]}}


def answer(body: str, tool_id: str = "t1", error: bool = False) -> dict[str, object]:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "is_error": error, "content": [{"type": "text", "text": body}]}]}}


OK = '{"success":true,"message":"queued there","msg_id":"m1"}'


def local_options(arguments: list[str]) -> send.Options:
    """Keep pre-peer transport cases on the machine their fixtures describe."""
    return send.parse([*([] if "--machine" in arguments else ["--here"]), *arguments])


class SendTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.relayed: list[send.Message] = []
        self.outcome: send.Result = send.Result("sent", "ok")

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in (("STATE", self.root), ("relay", self.relay)):
            patcher = mock.patch.object(send, name, value)
            _ = patcher.start()
            self.addCleanup(patcher.stop)
        binaries = self.root / "bin"
        binaries.mkdir()
        fake_ssh = binaries / "ssh"
        _ = fake_ssh.write_text(
            f"#!{sys.executable}\n"
            + "import json, os, sys\n"
            + "with open(os.environ['SSH_RECORD'], 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + "print(os.environ['SSH_SNAPSHOT'])\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "HOME": str(self.root), "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "SSH_RECORD": str(self.root / "ssh.jsonl"),
            "SSH_SNAPSHOT": roster.RosterSnapshot(roster.Roster([], [])).encode(),
        })))
        # No test reads the machine's own session records, or passes for the session that runs it.
        (self.root / "sessions").mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                                                       "CLAUDE_CODE_SESSION_ID": ""}))
        _ = self.enterContext(
            mock.patch.object(roster, "roster", return_value=roster.Roster([], []))
        )

    def session(self, session_id: str, name: str, *former: str) -> None:
        """Record a live Claude session, or its rename: this test process stands in for it."""
        path = self.root / f"{session_id}.sock"
        if not path.exists():
            held = socket.socket(socket.AF_UNIX)
            held.bind(str(path))
            self.addCleanup(held.close)
        _ = (self.root / "sessions" / f"{session_id}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": session_id, "name": name, "formerNames": list(former),
            "messagingSocketPath": str(path), "updatedAt": 1}))

    def relay(self, message: send.Message, timeout: float) -> send.Result:
        del timeout
        self.relayed.append(message)
        return self.outcome

    def send(self, *args: str) -> send.Result:
        return send.send(local_options(["--to", "bogus", "--from", "test", *args]))

    def log(self) -> list[dict[str, object]]:
        return [send.as_dict(send.loads(line)) for line in (self.root / "log.jsonl").read_text().splitlines()]

    def test_repeat_window_is_per_key_and_recipient(self) -> None:
        self.assertEqual(self.send("--key", "k", "--repeat-minutes", "30", "--text", "one").outcome, "sent")
        skipped = self.send("--key", "k", "--repeat-minutes", "30", "--text", "two")
        self.assertEqual(skipped.outcome, "skipped")
        self.assertEqual(send.send(local_options(["--to", "other", "--key", "k", "--repeat-minutes", "30",
                                               "--text", "three"])).outcome, "sent")
        later = send.now() + timedelta(minutes=31)
        with mock.patch.object(send, "now", return_value=later):
            self.assertEqual(self.send("--key", "k", "--repeat-minutes", "30", "--text", "four").outcome, "sent")
        self.assertEqual([entry["outcome"] for entry in self.log()], ["sent", "skipped", "sent", "sent"])

    def test_log_keeps_full_text_for_every_outcome(self) -> None:
        sent = "sent heading\nsent detail"
        skipped = "skipped heading\nskipped detail"
        queued = "queued heading\nqueued detail"
        failed = "failed heading\nfailed detail"
        self.assertEqual(self.send("--key", "k", "--repeat-minutes", "30", "--text", sent).outcome, "sent")
        self.assertEqual(self.send("--key", "k", "--repeat-minutes", "30", "--text", skipped).outcome, "skipped")
        self.outcome = send.Result("queued", "relay unavailable")
        self.assertEqual(self.send("--text", queued).outcome, "queued")
        self.outcome = send.Result("failed", "relay failed")
        self.assertEqual(self.send("--text", failed).outcome, "failed")
        for entry, text, outcome in zip(self.log(), (sent, skipped, queued, failed),
                                        ("sent", "skipped", "queued", "failed"), strict=True):
            self.assertEqual((entry["from"], entry["to"], entry["outcome"], entry["text"]),
                             ("test", "bogus", outcome, text))

    def test_ack_skips_until_reopen(self) -> None:
        _ = send.acknowledge("k")
        self.assertIn("already acknowledged", send.acknowledge("k"))
        self.assertEqual(self.send("--key", "k", "--text", "hello").outcome, "skipped")
        self.assertEqual(self.relayed, [])
        _ = send.reopen("k")
        self.assertEqual(self.send("--key", "k", "--text", "hello").outcome, "sent")
        self.assertIn("nothing to reopen", send.reopen("never"))

    def test_undelivered_is_queued_latest_per_key_and_pending_clears(self) -> None:
        self.outcome = send.Result("queued", "no agent named bogus")
        _ = self.send("--key", "k", "--text", "first\nbody")
        _ = self.send("--key", "k", "--text", "second")
        _ = self.send("--text", "unkeyed")
        with mock.patch("sys.stdout"):
            self.assertEqual(send.main(["--to", "bogus", "--text", "again"]), 1)
        text = send.pending("bogus")
        self.assertNotIn("first", text)
        self.assertIn("Queued message 1 of 3, from test", text)
        self.assertIn("second", text)
        self.assertIn("again", text)
        self.assertEqual(send.pending("bogus"), "")
        entry = self.log()[0]
        self.assertEqual((entry["to"], entry["key"], entry["summary"], entry["outcome"]),
                         ("bogus", "k", "first", "queued"))

    def test_what_did_not_arrive_is_kept_by_session_id_so_a_rename_loses_nothing(self) -> None:
        self.session("id-1", "old")
        self.outcome = send.Result("queued", "relay timed out after 40 s")
        result = send.send(local_options(["--to", "old", "--from", "test", "--text", "by name"]))
        self.assertEqual(result, send.Result("queued", "relay timed out after 40 s"))
        _ = send.send(local_options(["--to", f"uds:{self.root / 'id-1.sock'}", "--from", "test", "--text", "by socket"]))
        self.assertEqual([path.name for path in (self.root / "queue").iterdir()], ["session-id-1.jsonl"])
        self.session("id-1", "new", "old")
        # A send to the name the session no longer has is still kept for that session.
        _ = send.send(local_options(["--to", "old", "--from", "test", "--text", "by the old name"]))
        self.assertEqual(send.pending("someone-else"), "")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "id-1"}):
            text = send.pending(None)
        for sent in ("Queued message 1 of 3", "by name", "by socket", "by the old name"):
            self.assertIn(sent, text)
        self.assertEqual(send.pending("new"), "")

    def test_what_was_kept_under_a_name_before_is_found_by_the_session_that_had_it(self) -> None:
        entry = {"time": "2026-10-07T10:00:00+00:00", "from": "a", "to": "old", "key": None, "summary": "s",
                 "text": "kept before the change", "reason": "offline"}
        path = send.queue_path("old")
        path.parent.mkdir()
        _ = path.write_text(json.dumps(entry) + "\n")
        self.session("id-1", "new", "old")
        # Another live session has the name now: what is kept under it is that session's, not the renamed one's.
        self.session("id-2", "old")
        self.assertEqual(send.pending("new"), "")
        self.assertIn("kept before the change", send.pending("old"))
        _ = path.write_text(json.dumps(entry) + "\n")
        (self.root / "sessions/id-2.json").unlink()
        self.assertIn("kept before the change", send.pending("new"))
        self.assertFalse(path.exists())

    def test_a_send_no_live_session_answers_to_says_so(self) -> None:
        self.outcome = send.Result("queued", "No agent named bogus")
        result = self.send("--text", "anyone there")
        self.assertEqual(result.outcome, "queued")
        self.assertIn("no live session answers to bogus, so it is kept under that name", result.detail)
        with self.assertRaises(SystemExit) as stopped, mock.patch("sys.stderr"):
            _ = send.main(["pending"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("anyone there", send.pending("bogus"))

    def test_live_session_id_relays_to_its_socket_and_logs_the_requested_address(self) -> None:
        self.session("id-1", "recipient")

        result = send.send(local_options([
            "--to", "session:id-1", "--from", "test", "--text", "hello by id",
        ]))

        self.assertEqual(result, send.Result("sent", "ok"))
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.relayed[0].to, f"uds:{self.root / 'id-1.sock'}")
        self.assertEqual(self.relayed[0].text, "hello by id")
        self.assertEqual(self.log()[0]["to"], "session:id-1")

    def test_session_id_without_a_live_session_queues_without_relaying(self) -> None:
        result = send.send(local_options([
            "--to", "session:gone", "--from", "test", "--text", "wait for me",
        ]))

        self.assertEqual(result.outcome, "queued")
        self.assertIn("no live session answers to session:gone", result.detail)
        self.assertEqual(self.relayed, [])
        self.session("gone", "returned")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "gone"}):
            self.assertIn("wait for me", send.pending(None))
            self.assertEqual(send.pending(None), "")

    def test_a_delivered_keyed_send_retires_its_queued_copy(self) -> None:
        self.session("id-1", "recipient")
        self.outcome = send.Result("queued", "relay unavailable")
        first = send.send(local_options([
            "--to", "session:id-1", "--from", "shutdown", "--key", "shutdown-owner-id-1",
            "--text", "settle first",
        ]))
        self.assertEqual(first.outcome, "queued")

        self.outcome = send.Result("sent", "ok")
        delivered = send.send(local_options([
            "--to", "session:id-1", "--from", "shutdown", "--key", "shutdown-owner-id-1",
            "--text", "cancel instead",
        ]))

        self.assertEqual(delivered.outcome, "sent")
        self.assertEqual(send.pending("recipient"), "")

    def test_successful_keyed_send_by_roster_name_retires_queue_under_given_name(
        self,
    ) -> None:
        self.outcome = send.Result("queued", "relay unavailable")
        queued = send.send(local_options([
            "--to", "recipient", "--from", "shutdown", "--key", "shutdown-owner-id-1",
            "--text", "settle first",
        ]))
        self.assertEqual(queued.outcome, "queued")

        self.session("id-1", "recipient")
        recipient = roster.RosterEntry(
            "machine", "claude", "top-level", "recipient", "running",
            roster.Messageable("session:id-1"), "/work", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(),
        )
        self.outcome = send.Result("sent", "ok")
        with mock.patch.object(
            roster, "roster", return_value=roster.Roster([recipient], []),
        ):
            delivered = send.send(local_options([
                "--to", "recipient", "--from", "shutdown", "--key", "shutdown-owner-id-1",
                "--text", "cancel instead",
            ]))

        self.assertEqual(delivered.outcome, "sent")
        self.assertEqual(send.pending("recipient"), "")

    def test_retire_removes_only_the_shutdown_messages_for_one_address(self) -> None:
        key = "shutdown-owner-id-1"
        messages = (
            ("session:id-1", "shutdown", key, "keyed settle"),
            ("session:id-1", "shutdown", None, "old unkeyed cancel"),
            ("session:id-1", "showrunner", None, "keep same address"),
            ("session:id-2", "shutdown", key, "keep other address"),
        )
        for address, sender, message_key, text in messages:
            argv = ["--to", address, "--from", sender, "--text", text]
            if message_key is not None:
                argv[4:4] = ["--key", message_key]
            self.assertEqual(send.send(local_options(argv)).outcome, "queued")

        output = io.StringIO()
        with redirect_stdout(output):
            code = send.main([
                "retire", "--to", "session:id-1", "--from", "shutdown", "--key", key,
            ])

        self.assertEqual(code, 0)
        self.assertIn("2", output.getvalue())
        self.session("id-1", "one")
        self.session("id-2", "two")
        same_address = send.pending("one")
        self.assertNotIn("settle", same_address)
        self.assertNotIn("cancel", same_address)
        self.assertIn("keep same address", same_address)
        self.assertIn("keep other address", send.pending("two"))

    def test_keyed_shutdown_result_survives_restart_retire_and_replaces_older_result(
        self,
    ) -> None:
        address = "session:id-1"
        result_key = "shutdown-result-owner@example.com"
        restart_key = "shutdown-owner@example.com-id-1"
        messages = (
            (result_key, "first stop result"),
            (None, "old unkeyed stop result"),
            (result_key, "latest stop result"),
        )
        for key, text in messages:
            arguments = [
                "--to",
                address,
                "--from",
                "shutdown",
                "--text",
                text,
            ]
            if key is not None:
                arguments[4:4] = ["--key", key]
            self.assertEqual(
                send.send(local_options(arguments)).outcome,
                "queued",
            )

        self.assertEqual(
            send.main(
                [
                    "retire",
                    "--to",
                    address,
                    "--from",
                    "shutdown",
                    "--key",
                    restart_key,
                ]
            ),
            0,
        )

        self.session("id-1", "returned")
        pending = send.pending("returned")
        self.assertNotIn("first stop result", pending)
        self.assertNotIn("old unkeyed stop result", pending)
        self.assertIn("latest stop result", pending)

    def test_real_cancel_and_restart_senders_retire_old_settle_instructions(self) -> None:
        real_send = Path(__file__).resolve().parent / "send.py"
        child_state = self.root / "child-state"

        def shutdown_fixture(
            session_id: str,
        ) -> tuple[shutdown_record.ShutdownRecord, shutdown_record.ShutdownSessionEntry]:
            session = cast(
                object,
                {"session_id": session_id, "name": session_id, "kind": "top-level"},
            )
            current_entry = cast(
                shutdown_record.ShutdownSessionEntry,
                cast(object, {
                    "session": session,
                    "timers": [],
                    "settle_message": shutdown_record.SettleMessageNotSent(kind="not sent"),
                }),
            )
            current = cast(
                shutdown_record.ShutdownRecord,
                cast(object, {
                    "login": "owner@example.com",
                    "label": "claude 2",
                    "requested_at": "2026-10-09T21:49:10+00:00",
                    "entries": [current_entry],
                }),
            )
            return current, current_entry

        with (
            mock.patch.object(send, "STATE", child_state / "message"),
            mock.patch.dict(
                os.environ,
                {
                    "XDG_STATE_HOME": str(child_state),
                    "SHUTDOWN_SEND": str(real_send),
                },
            ),
        ):
            cancelled, cancel_entry = shutdown_fixture("cancelled")
            settle._send_settle_message(cancelled, cancel_entry)  # pyright: ignore[reportPrivateUsage]
            old_cancel = settle.send_message(
                "session:cancelled",
                "old settle",
                "Old unkeyed settle instruction: run /shutdown ready.",
            )
            self.assertEqual(old_cancel["kind"], "queued")
            settle._restore_record(cancelled)  # pyright: ignore[reportPrivateUsage]
            self.session("cancelled", "cancel-recipient")
            cancel_pending = send.pending("cancel-recipient")
            self.assertNotIn("settle instruction", cancel_pending)
            self.assertIn("cancelled by the user", cancel_pending)

            restarted, restart_entry = shutdown_fixture("restarted")
            settle._send_settle_message(restarted, restart_entry)  # pyright: ignore[reportPrivateUsage]
            old_restart = settle.send_message(
                "session:restarted",
                "old settle",
                "Old unkeyed settle instruction: run /shutdown ready.",
            )
            self.assertEqual(old_restart["kind"], "queued")
            restart._retire(restarted, restart_entry, dry_run=False)  # pyright: ignore[reportPrivateUsage]
            delivery = restart._send_restart_note(  # pyright: ignore[reportPrivateUsage]
                restarted,
                restart_entry,
                "Restarted; continue.",
                dry_run=False,
            )
            self.assertEqual(delivery["kind"], "queued")
            self.session("restarted", "restart-recipient")
            restart_pending = send.pending("restart-recipient")
            self.assertNotIn("settle instruction", restart_pending)
            self.assertIn("Restarted; continue.", restart_pending)

    def test_unique_claude_roster_name_delivers_to_its_session_address(self) -> None:
        self.session("id-1", "recipient")
        recipient = roster.RosterEntry(
            "machine", "claude", "top-level", "recipient", "running",
            roster.Messageable("session:id-1"), "/work", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(),
        )

        with mock.patch.object(
            roster, "roster", return_value=roster.Roster([recipient], []),
        ):
            result = send.send(local_options([
                "--to", "recipient", "--from", "test", "--text", "hello by name",
            ]))

        self.assertEqual(result, send.Result("sent", "ok"))
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.relayed[0].to, f"uds:{self.root / 'id-1.sock'}")
        self.assertEqual(self.log()[0]["to"], "session:id-1")

    def test_same_seat_name_in_two_runs_resolves_by_address_and_is_ambiguous_by_name(
        self,
    ) -> None:
        first_address = "seat:11111111-1111-1111-1111-111111111111/shared"
        second_address = "seat:22222222-2222-2222-2222-222222222222/shared"
        first_dir = self.root / "11111111-1111-1111-1111-111111111111"
        second_dir = self.root / "22222222-2222-2222-2222-222222222222"
        entries = [
            roster.RosterEntry(
                "machine", "codex", "worker", "shared", "running",
                roster.Messageable(first_address), "/work/one", roster.NoShowrunner(),
                roster.NoDirector(), roster.WorkerAssignment("impl", str(first_dir)),
            ),
            roster.RosterEntry(
                "machine", "codex", "worker", "shared", "running",
                roster.Messageable(second_address), "/work/two", roster.NoShowrunner(),
                roster.NoDirector(), roster.WorkerAssignment("test", str(second_dir)),
            ),
        ]
        delivered: list[tuple[send.Message, str]] = []

        def deliver(
            message: send.Message, session_dir: str, timeout: float,
        ) -> send.Result:
            del timeout
            delivered.append((message, session_dir))
            return send.Result("sent", "queued on worker")

        fixture = roster.Roster(entries, [])
        with (
            mock.patch.object(roster, "roster", return_value=fixture),
            mock.patch.object(send, "codex", side_effect=deliver),
        ):
            for address in (first_address, second_address):
                result = send.send(local_options([
                    "--to", address, "--from", "test", "--text", "hello",
                ]))
                self.assertEqual(result.outcome, "sent")

            errors = io.StringIO()
            with self.assertRaises(SystemExit) as raised, redirect_stderr(errors):
                _ = send.main(["--here",
                    "--to", "shared", "--from", "test", "--text", "hello",
                ])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(
            [(message.to, session_dir) for message, session_dir in delivered],
            [("shared", str(first_dir)), ("shared", str(second_dir))],
        )
        self.assertIn("send.py: shared matches 2 sessions:", errors.getvalue())
        self.assertIn(f"{first_address} (worker)", errors.getvalue())
        self.assertIn(f"{second_address} (worker)", errors.getvalue())

    def test_blind_reviewer_by_name_cannot_send_and_names_its_director(self) -> None:
        reviewer = roster.RosterEntry(
            "machine", "codex", "worker", "blind review", "reviewing",
            roster.CannotReceiveMessages("blind reviewer"), "/review",
            roster.UnderShowrunner("production", "runner", "session:runner"),
            roster.UnderDirector("unit", "director", "session:director"),
            roster.WorkerAssignment("review (adversary)", str(self.root / "run")),
        )
        errors = io.StringIO()
        with (
            mock.patch.object(
                roster, "roster", return_value=roster.Roster([reviewer], []),
            ),
            mock.patch.object(send, "codex") as codex_send,
            mock.patch.object(send, "relay") as claude_send,
            mock.patch.object(codex_daemon, "queue") as daemon_send,
            self.assertRaises(SystemExit) as raised,
            redirect_stderr(errors),
        ):
            _ = send.main(["--here",
                "--to", "blind review", "--from", "test", "--text", "hello",
            ])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(
            errors.getvalue(),
            "send.py: blind review cannot take messages (blind reviewer); "
            + "contact its unit director director or wait\n",
        )
        codex_send.assert_not_called()
        claude_send.assert_not_called()
        daemon_send.assert_not_called()

    def test_nonmessageable_recipient_without_running_director_is_usage_error(
        self,
    ) -> None:
        worker = roster.RosterEntry(
            "machine", "codex", "worker", "paused worker", "waiting",
            roster.CannotReceiveMessages("seat between turns; no unit director is running"), "/work",
            roster.UnderShowrunner("production", "runner", "session:runner"),
            roster.NoDirector(),
            roster.WorkerAssignment("impl", str(self.root / "run")),
        )
        errors = io.StringIO()
        with (
            mock.patch.object(
                roster, "roster", return_value=roster.Roster([worker], []),
            ),
            mock.patch.object(send, "codex") as codex_send,
            mock.patch.object(send, "relay") as claude_send,
            mock.patch.object(codex_daemon, "queue") as daemon_send,
            mock.patch.object(send, "enqueue") as enqueue,
            self.assertRaises(SystemExit) as raised,
            redirect_stderr(errors),
        ):
            _ = send.main(["--here",
                "--to", "paused worker", "--from", "test", "--text", "hello",
            ])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(
            errors.getvalue(),
            "send.py: paused worker cannot take messages (seat between turns; no unit director is running)\n",
        )
        codex_send.assert_not_called()
        claude_send.assert_not_called()
        daemon_send.assert_not_called()
        enqueue.assert_not_called()

    def test_ambiguous_name_describes_a_recipient_that_cannot_take_messages(self) -> None:
        entries = [
            roster.RosterEntry(
                "machine", "claude", "top-level", "duplicate", "running",
                roster.Messageable("session:messageable"), "/work", roster.NoShowrunner(),
                roster.NoDirector(), roster.NotAWorker(),
            ),
            roster.RosterEntry(
                "machine", "codex", "worker", "duplicate", "reviewing",
                roster.CannotReceiveMessages("blind reviewer"), "/review",
                roster.UnderShowrunner("production", "runner", "session:runner"),
                roster.UnderDirector("unit", "director", "session:director"),
                roster.WorkerAssignment("review", str(self.root / "run")),
            ),
        ]
        errors = io.StringIO()
        with (
            mock.patch.object(
                roster, "roster", return_value=roster.Roster(entries, []),
            ),
            self.assertRaises(SystemExit) as raised,
            redirect_stderr(errors),
        ):
            _ = send.main(["--here",
                "--to", "duplicate", "--from", "test", "--text", "hello",
            ])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("  session:messageable (top-level)\n", errors.getvalue())
        self.assertIn(
            "  (cannot take messages: blind reviewer) (worker)\n",
            errors.getvalue(),
        )

    def test_roster_problem_is_printed_while_a_usable_seat_still_succeeds(self) -> None:
        address = "seat:33333333-3333-3333-3333-333333333333/worker"
        run = self.root / "33333333-3333-3333-3333-333333333333"
        worker = roster.RosterEntry(
            "machine", "codex", "worker", "worker", "running",
            roster.Messageable(address), "/work", roster.NoShowrunner(),
            roster.NoDirector(), roster.WorkerAssignment("impl", str(run)),
        )
        errors = io.StringIO()
        delivered: list[tuple[send.Message, str]] = []

        def deliver(
            message: send.Message, session_dir: str, timeout: float,
        ) -> send.Result:
            del timeout
            delivered.append((message, session_dir))
            return send.Result("sent", "queued on worker")

        with (
            mock.patch.object(
                roster, "roster",
                return_value=roster.Roster([worker], ["one run was unreadable"]),
            ),
            mock.patch.object(send, "codex", side_effect=deliver),
            redirect_stderr(errors),
        ):
            result = send.send(local_options([
                "--to", address, "--from", "test", "--text", "hello",
            ]))

        self.assertEqual(result, send.Result("sent", "queued on worker"))
        self.assertEqual(errors.getvalue(), "send.py: roster: one run was unreadable\n")
        self.assertEqual(
            [(message.to, session_dir) for message, session_dir in delivered],
            [("worker", str(run))],
        )

    def test_unique_seat_name_resolves_to_its_run(self) -> None:
        address = "seat:55555555-5555-5555-5555-555555555555/only-worker"
        run = self.root / "55555555-5555-5555-5555-555555555555"
        worker = roster.RosterEntry(
            "machine", "codex", "worker", "only-worker", "running",
            roster.Messageable(address), "/work", roster.NoShowrunner(),
            roster.NoDirector(), roster.WorkerAssignment("impl", str(run)),
        )
        delivered: list[tuple[send.Message, str]] = []

        def deliver(
            message: send.Message, session_dir: str, timeout: float,
        ) -> send.Result:
            del timeout
            delivered.append((message, session_dir))
            return send.Result("sent", "queued on worker")

        with (
            mock.patch.object(
                roster, "roster", return_value=roster.Roster([worker], []),
            ),
            mock.patch.object(send, "codex", side_effect=deliver),
        ):
            result = send.send(local_options([
                "--to", "only-worker", "--from", "test", "--text", "hello",
            ]))

        self.assertEqual(result, send.Result("sent", "queued on worker"))
        self.assertEqual(
            [(message.to, session_dir) for message, session_dir in delivered],
            [("only-worker", str(run))],
        )

    def test_unreadable_roster_keeps_bare_claude_fallback_but_seat_address_fails(
        self,
    ) -> None:
        unavailable = roster.Roster([], ["cannot read the delegate root"])
        bare_errors = io.StringIO()
        with (
            mock.patch.object(roster, "roster", return_value=unavailable),
            redirect_stderr(bare_errors),
        ):
            bare = send.send(local_options([
                "--to", "unknown-claude", "--from", "test", "--text", "hello",
            ]))

        self.assertEqual(bare, send.Result("sent", "ok"))
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(
            bare_errors.getvalue(),
            "send.py: roster: cannot read the delegate root\n",
        )

        seat_errors = io.StringIO()
        with (
            mock.patch.object(roster, "roster", return_value=unavailable),
            mock.patch.object(send, "codex") as codex_send,
            self.assertRaises(SystemExit) as raised,
            redirect_stderr(seat_errors),
        ):
            _ = send.main(["--here",
                "--to", "seat:missing/worker", "--from", "test", "--text", "hello",
            ])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("send.py: roster: cannot read the delegate root", seat_errors.getvalue())
        self.assertIn("seat:missing/worker", seat_errors.getvalue())
        codex_send.assert_not_called()

    def test_codex_thread_address_queues_after_one_local_roster_read(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(roster, "roster", return_value=roster.Roster([], [])) as read_roster,
            mock.patch.object(
                codex_daemon, "queue", return_value=codex_daemon.Queued(),
            ) as queue,
            redirect_stdout(output),
        ):
            code = send.main(["--here",
                "--to", "codex:thread-one", "--from", "test", "--text", "hello",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "SENT: queued on Codex session thread-one\n")
        queue.assert_called_once_with("thread-one", "hello", timeout=send.TIMEOUT)
        read_roster.assert_called_once()

    def test_named_codex_session_uses_its_display_name_in_the_outcome(self) -> None:
        session = roster.RosterEntry(
            "machine", "codex", "freestanding", "Codex window", "idle",
            roster.Messageable("codex:thread-window"), "/work",
            roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker(),
        )
        with (
            mock.patch.object(
                roster, "roster", return_value=roster.Roster([session], []),
            ),
            mock.patch.object(
                codex_daemon, "queue", return_value=codex_daemon.Queued(),
            ) as queue,
        ):
            result = send.send(local_options([
                "--to", "Codex window", "--from", "test", "--text", "hello",
            ]))

        self.assertEqual(result, send.Result("sent", "queued on Codex session Codex window"))
        queue.assert_called_once_with("thread-window", "hello", timeout=send.TIMEOUT)

    def test_codex_queue_failure_and_timeout_are_failed_and_never_kept(self) -> None:
        failures = (
            (codex_daemon.QueueFailed("thread is closed"), "thread is closed"),
            (codex_daemon.QueueTimedOut(0.5), "timed out after 0.5 s"),
        )
        for queue_result, detail in failures:
            output = io.StringIO()
            with self.subTest(queue_result=queue_result), mock.patch.object(
                codex_daemon, "queue", return_value=queue_result,
            ), redirect_stdout(output):
                code = send.main(["--here",
                    "--to", "codex:thread-one", "--from", "test", "--text", "hello",
                ])
            self.assertEqual(code, 3)
            self.assertEqual(output.getvalue(), f"FAILED: {detail}\n")
        self.assertEqual(send.pending("codex:thread-one"), "")

    def test_local_roster_is_read_once_for_every_roster_address_and_bare_name(self) -> None:
        address = "seat:44444444-4444-4444-4444-444444444444/worker"
        worker = roster.RosterEntry(
            "machine", "codex", "worker", "worker", "running",
            roster.Messageable(address), "/work", roster.NoShowrunner(),
            roster.NoDirector(), roster.WorkerAssignment("impl", str(self.root / "run")),
        )
        for recipient, expected_reads in (
            (address, 1),
            ("worker", 1),
            ("codex:thread-one", 1),
            ("session:claude-one", 1),
            (f"uds:{self.root / 'claude.sock'}", 0),
            ("user", 0),
        ):
            with self.subTest(recipient=recipient):
                read_roster = mock.Mock(return_value=roster.Roster([worker], []))
                options = ["--to", recipient, "--from", "test", "--text", "hello"]
                if recipient == "user":
                    options[4:4] = ["--summary", "note", "--no-action"]
                with (
                    mock.patch.object(roster, "roster", read_roster),
                    mock.patch.object(
                        send, "codex", return_value=send.Result("sent", "seat"),
                    ),
                    mock.patch.object(
                        codex_daemon, "queue", return_value=codex_daemon.Queued(),
                    ),
                    mock.patch.object(
                        send, "relay", return_value=send.Result("sent", "claude"),
                    ),
                    mock.patch.object(
                        send, "user", return_value=send.Result("sent", "user"),
                    ),
                ):
                    _ = send.send(local_options(options))
                self.assertEqual(read_roster.call_count, expected_reads)

    def test_seat_address_with_action_is_a_usage_error(self) -> None:
        errors = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stderr(errors):
            _ = local_options([
                "--to", "seat:run/worker", "--action", "reply", "--text", "hello",
            ])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("go with --to user", errors.getvalue())

    def test_help_description_and_to_option_name_every_recipient_form(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
            _ = local_options(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        for recipient_form in (
            "a roster name", "uds:<path>", "session:<id>",
            "seat:<run id>/<seat name>", "codex:<thread id>", "user",
        ):
            self.assertGreaterEqual(help_text.count(recipient_form), 2)

    def test_codex_failure_is_failed_not_queued(self) -> None:
        with mock.patch.object(send, "run", return_value=(1, "", "codex_mesh: no delegate named 'bogus'")):
            result = self.send("--codex", "--session-dir", str(self.root), "--text", "hi")
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(send.pending("bogus"), "")

    def test_remote_reports_its_outcome_and_ssh_failure_is_failed(self) -> None:
        commands: list[list[str]] = []

        def remote_run(command: list[str], stdin: str, timeout: float) -> tuple[int | None, str, str]:
            del stdin, timeout
            commands.append(command)
            return 1, "QUEUED: no agent named bogus\n", ""

        with mock.patch.object(send, "run", remote_run):
            result = self.send("--machine", "mac", "--key", "k", "--text", "hi")
        self.assertEqual(result, send.Result("queued", "on mac: no agent named bogus"))
        self.assertEqual(commands[0][-2], "mac")
        self.assertIn("--key k", commands[0][-1])
        self.assertIn("--here", commands[0][-1])
        self.assertEqual(send.pending("bogus"), "")
        with mock.patch.object(send, "run", return_value=(255, "", "ssh: connect to host mac: timed out")):
            self.assertEqual(self.send("--machine", "mac", "--text", "hi").outcome, "failed")

    def test_the_user_is_reached_with_a_title_and_a_need_and_never_queued(self) -> None:
        commands: list[list[str]] = []

        def channel(command: list[str], stdin: str, timeout: float) -> tuple[int | None, str, str]:
            del stdin, timeout
            commands.append(command)
            return (0, "", "") if len(commands) == 1 else (1, "", "refused")

        told = ["--to", "user", "--from", "test", "--summary", "natedev: disk", "--need", "blocked",
                "--action", "free disk space", "--text", "full"]
        with mock.patch.object(send, "run", channel):
            self.assertEqual(send.send(local_options(told)), send.Result("sent", "to the user"))
            self.assertEqual(send.send(local_options(told)).outcome, "failed")
        self.assertEqual(commands[0][2:], ["--priority", "2", "--source", "test", "--action", "free disk space",
                                           "natedev: disk", "full"])
        self.assertEqual(self.relayed, [])
        self.assertEqual(send.pending("user"), "")
        self.assertEqual([entry["to"] for entry in self.log()], ["user", "user"])

    def test_a_remote_send_to_the_user_carries_its_need(self) -> None:
        commands: list[list[str]] = []

        def remote_run(command: list[str], stdin: str, timeout: float) -> tuple[int | None, str, str]:
            del stdin, timeout
            commands.append(command)
            return 0, "SENT: to the user\n", ""

        with mock.patch.object(send, "run", remote_run):
            result = send.send(local_options(["--to", "user", "--summary", "mac: login", "--need", "decision",
                                           "--action", "log in", "--machine", "natedev", "--text", "hi"]))
        self.assertEqual(result, send.Result("sent", "on natedev: to the user"))
        self.assertIn("--need decision", commands[0][-1])
        self.assertIn("--here", commands[0][-1])
        self.assertIn("--action 'log in'", commands[0][-1])

    def test_pushover_refusal_exits_2_and_keeps_nothing(self) -> None:
        refused = f"pushover: {REFUSED_BEFORE_SENDING}: priority 2 requires --action"
        output = io.StringIO()
        arguments = ["--to", "user", "--from", "test", "--summary", "natedev: disk", "--no-action",
                     "--text", "full"]
        with mock.patch.object(send, "run", return_value=(2, "", refused)), redirect_stdout(output):
            code = send.main(arguments)
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), f"REFUSED: {refused}\n")
        self.assertEqual(send.pending("user"), "")
        self.assertEqual(self.log()[0]["outcome"], "refused")

    def test_pushover_exit_2_for_missing_keys_is_failed(self) -> None:
        keys_missing = "pushover: PUSHOVER_USER and PUSHOVER_TOKEN must both be set in /missing/env"
        output = io.StringIO()
        arguments = ["--to", "user", "--from", "test", "--summary", "news", "--no-action", "--text", "done"]
        with mock.patch.object(send, "run", return_value=(2, "", keys_missing)), redirect_stdout(output):
            code = send.main(arguments)
        self.assertEqual(code, 3)
        self.assertEqual(output.getvalue(), f"FAILED: the user was not reached: {keys_missing}\n")
        self.assertEqual(send.pending("user"), "")
        self.assertEqual(self.log()[0]["outcome"], "failed")

    def test_user_options_hold_one_typed_action(self) -> None:
        required = local_options(["--to", "user", "--summary", "choose", "--action", "pick one", "--text", "a or b"])
        none = local_options(["--to", "user", "--summary", "news", "--no-action", "--text", "done"])
        self.assertEqual(required.action, ActionRequired("pick one"))
        self.assertEqual(none.action, NoActionRequired())

    def test_non_user_options_have_no_user_recipient_state(self) -> None:
        options = local_options(["--to", "natedev", "--text", "hello"])
        self.assertEqual(options.action, send.NoUserRecipient())

    def test_action_flags_are_only_for_user_messages(self) -> None:
        for action in (["--no-action"], ["--action", "reply"]):
            errors = io.StringIO()
            with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr", errors):
                _ = local_options(["--to", "natedev", *action, "--text", "hello"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("go with --to user", errors.getvalue())

    def test_usage_errors_exit_2(self) -> None:
        for argv in (["--to", "x", "--repeat-minutes", "5", "--text", "t"], ["--to", "x", "--codex", "--text", "t"],
                     ["--to", "x", "--text", "  "], ["ack"], ["--to", "user", "--text", "t"],
                     ["--to", "user", "--summary", "s", "--action", "none", "--text", "t"],
                     ["--to", "user", "--summary", "s", "--action", "do it", "--no-action", "--text", "t"],
                     ["--to", "user", "--summary", "s", "--need", "decision", "--no-action", "--text", "t"],
                     ["--to", "x", "--need", "decision", "--text", "t"]):
            with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr"):
                _ = send.main(argv)
            self.assertEqual(raised.exception.code, 2, argv)


class PeerSendTests(unittest.TestCase):
    root: Path = Path()
    local: roster.Roster = roster.Roster([], [])
    ssh_log: Path = Path()
    relayed: list[send.Message] = []
    mesh_calls: list[tuple[send.Message, str]] = []
    daemon_calls: list[str] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        binaries = self.root / "bin"
        binaries.mkdir()
        (self.root / "sessions").mkdir()
        self.ssh_log = self.root / "ssh.jsonl"
        ssh = binaries / "ssh"
        _ = ssh.write_text(
            f"#!{sys.executable}\n"
            + "import json, os, sys\n"
            + "with open(os.environ['SSH_RECORD'], 'a') as log: log.write(json.dumps({'args': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')\n"
            + "if 'roster.py' in sys.argv[-1]:\n"
            + "    print(os.environ['SSH_SNAPSHOT'])\n"
            + "    raise SystemExit(int(os.environ.get('SSH_ROSTER_STATUS', '0')))\n"
            + "print(os.environ.get('SSH_SEND_RESULT', 'SENT: fixture recipient'))\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "HOME": str(self.root), "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": "",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "SSH_RECORD": str(self.ssh_log),
            "SSH_SNAPSHOT": roster.RosterSnapshot(roster.Roster([], [])).encode(),
        }, clear=True)))
        self.local = roster.Roster([], [])
        self.relayed, self.mesh_calls, self.daemon_calls = [], [], []
        _ = self.enterContext(mock.patch.object(send, "STATE", self.root / "state"))
        _ = self.enterContext(mock.patch.object(roster, "roster", side_effect=self.read_local))
        _ = self.enterContext(mock.patch.object(send, "relay", side_effect=self.relay))
        _ = self.enterContext(mock.patch.object(send, "codex", side_effect=self.mesh))
        _ = self.enterContext(mock.patch.object(codex_daemon, "queue", side_effect=self.daemon))
        _ = self.enterContext(mock.patch.object(send, "user", side_effect=AssertionError("never reach a real user channel")))

    def read_local(self, _environment: object, scope: roster.RosterScope) -> roster.Roster:
        self.assertEqual(scope, roster.ThisMachine())
        return self.local

    def relay(self, message: send.Message, _timeout: float) -> send.Result:
        self.relayed.append(message)
        return send.Result("sent", "fixture Claude")

    def mesh(self, message: send.Message, directory: str, _timeout: float) -> send.Result:
        self.mesh_calls.append((message, directory))
        return send.Result("sent", "fixture mesh")

    def daemon(self, thread: str, _text: str, **_options: object) -> codex_daemon.QueueResult:
        self.daemon_calls.append(thread)
        return codex_daemon.Queued()

    def row(self, name: str, address: str, *, machine: str = "mac") -> roster.RosterEntry:
        kind = "claude" if address.startswith("session:") else "codex"
        worker = address.startswith("seat:")
        return roster.RosterEntry(
            machine, kind, "worker" if worker else "freestanding", name, "working",
            roster.Messageable(address), "/fixture", roster.NoShowrunner(), roster.NoDirector(),
            roster.WorkerAssignment("impl", "/fixture/run") if worker else roster.NotAWorker(),
            roster.CodexThread(address.removeprefix("codex:")) if kind == "codex" else roster.NoCodexThread(),
            roster.ClaudeSession(address.removeprefix("session:")) if kind == "claude" else roster.NoClaudeSession(),
        )

    def peer(self, rows: list[roster.RosterEntry], *, problems: tuple[str, ...] = ()) -> None:
        os.environ["SSH_SNAPSHOT"] = roster.RosterSnapshot(roster.Roster(rows, list(problems))).encode()

    def ssh_calls(self) -> list[dict[str, object]]:
        if not self.ssh_log.exists():
            return []
        return [cast(dict[str, object], json.loads(line)) for line in self.ssh_log.read_text().splitlines()]

    def forwarded(self) -> list[list[str]]:
        calls = [cast(list[str], call["args"]) for call in self.ssh_calls()]
        return [shlex.split(call[-1]) for call in calls if "send.py" in call[-1]]

    def run_main(self, to: str, *options: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                status = send.main(["--to", to, "--from", "fixture sender", *options, "--text", "fixture text"])
            except SystemExit as error:
                status = int(cast(int, error.code))
        return status, output.getvalue(), errors.getvalue()

    def test_remote_only_claude_mesh_and_daemon_recipients_forward_their_addresses(self) -> None:
        for address in ("session:peer", "seat:peer-run/impl", "codex:peer-thread"):
            with self.subTest(address=address):
                self.peer([self.row("peer name", address)])
                status, output, errors = self.run_main("peer name")
                self.assertEqual((status, errors), (0, ""), output)
                forwarded = self.forwarded()[-1]
                self.assertEqual(forwarded[forwarded.index("--to") + 1], address)
                self.assertIn("--here", forwarded)
                self.assertNotIn("--action", forwarded)
                self.assertNotIn("--no-action", forwarded)
        self.assertEqual(len(self.forwarded()), 3)
        self.assertEqual(self.relayed, [])
        self.assertEqual(self.mesh_calls, [])
        self.assertEqual(self.daemon_calls, [])

    def test_remote_only_addresses_are_looked_up_before_local_transport_fallback(self) -> None:
        for address in ("session:peer", "seat:peer-run/impl", "codex:peer-thread"):
            with self.subTest(address=address):
                self.peer([self.row("peer", address)])
                status, output, errors = self.run_main(address)
                self.assertEqual((status, errors), (0, ""), output)
                forwarded = self.forwarded()[-1]
                self.assertEqual(forwarded[forwarded.index("--to") + 1], address)
                self.assertIn("--here", forwarded)
        self.assertEqual(len(self.forwarded()), 3)
        self.assertEqual(self.relayed, [])
        self.assertEqual(self.mesh_calls, [])
        self.assertEqual(self.daemon_calls, [])

    def test_a_name_on_both_machines_delivers_here_without_ssh(self) -> None:
        self.local = roster.Roster([self.row("shared", "codex:local", machine="natedev")], [])
        self.peer([self.row("shared", "codex:peer")])
        status, output, errors = self.run_main("shared")
        self.assertEqual((status, errors), (0, ""), output)
        self.assertEqual(self.daemon_calls, ["local"])
        self.assertEqual(self.ssh_calls(), [])

    def test_here_and_uds_never_ask_the_other_machine(self) -> None:
        self.peer([self.row("remote", "codex:peer")])
        for target, flags in (("remote", ["--here"]), ("uds:/fixture/local.sock", []),
                              ("uds:/fixture/local.sock", ["--machine", "mac"])):
            with self.subTest(target=target):
                status, output, errors = self.run_main(target, *flags)
                self.assertEqual((status, errors), (0, ""), output)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(len(self.relayed), 3)

    def test_parse_names_each_resolution_scope_and_each_recipient_selection(self) -> None:
        for flags, expected_scope in (([], send.LocalThenPeer()), (["--here"], send.LocalOnly()),
                                      (["--machine", "mac"], send.NamedMachine("mac"))):
            for selection_flags, expected_selection in (
                ([], send.RosterLookup("recipient")),
                (["--codex", "--session-dir", "/fixture/run"], send.MeshSeat("/fixture/run", "recipient")),
            ):
                with self.subTest(scope=expected_scope, selection=expected_selection):
                    options = send.parse(["--to", "recipient", *flags, *selection_flags, "--text", "hello"])
                    self.assertEqual(options.scope, expected_scope)
                    self.assertEqual(options.selection, expected_selection)
        with self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
            _ = send.parse(["--to", "x", "--here", "--machine", "mac", "--text", "hello"])
        self.assertEqual(raised.exception.code, 2)

    def test_mesh_selection_stays_here_for_both_local_scopes_and_forwards_only_when_named(self) -> None:
        for flags in ([], ["--here"], ["--machine", "mac"]):
            with self.subTest(flags=flags):
                before = len(self.ssh_calls())
                status, output, errors = self.run_main("impl", *flags, "--codex", "--session-dir", "/fixture/run")
                self.assertEqual((status, errors), (0, ""), output)
                if "--machine" in flags:
                    self.assertEqual(len(self.ssh_calls()), before + 1)
                    forwarded = self.forwarded()[-1]
                    self.assertIn("--here", forwarded)
                    self.assertEqual(forwarded[forwarded.index("--session-dir") + 1], "/fixture/run")
                else:
                    self.assertEqual(len(self.ssh_calls()), before)
        self.assertEqual(len(self.mesh_calls), 2)

    def test_named_machine_skips_local_and_peer_roster_resolution(self) -> None:
        self.local = roster.Roster([], ["local source unavailable"])
        status, output, errors = self.run_main("bare name", "--machine", "mac")
        self.assertEqual((status, errors), (0, ""), output)
        self.assertEqual(len(self.ssh_calls()), 1)
        self.assertEqual(self.forwarded()[0][self.forwarded()[0].index("--to") + 1], "bare name")
        self.assertIn("--here", self.forwarded()[0])

    def test_clean_peer_misses_keep_each_forms_local_outcome(self) -> None:
        for target, expected in (("session:gone", 1), ("codex:gone", 0), ("seat:gone/impl", 2), ("bare missing", 0)):
            with self.subTest(target=target):
                before = len(self.ssh_calls())
                status, output, errors = self.run_main(target)
                self.assertEqual(status, expected, output + errors)
                self.assertEqual(len(self.ssh_calls()), before + 1)
                if target.startswith("session:"):
                    self.assertIn("fixture text", send.queue_path("session-gone").read_text())
                elif target.startswith("seat:"):
                    self.assertIn("no roster session has address", errors)
        self.assertEqual(self.daemon_calls, ["gone"])
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.forwarded(), [])

    def test_local_problems_prevent_unproven_bare_name_fallback_to_the_peer(self) -> None:
        self.local = roster.Roster([], ["delegate source unreadable"])
        self.peer([self.row("absent here", "codex:peer")])
        status, output, errors = self.run_main("absent here")
        self.assertEqual(status, 2, output + errors)
        self.assertIn("delegate source unreadable", errors)
        self.assertIn("--machine", errors)
        self.assertIn("address", errors)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.relayed + self.mesh_calls + self.daemon_calls, [])

    def test_an_address_can_resolve_on_peer_even_when_a_local_source_failed(self) -> None:
        self.local = roster.Roster([], ["delegate source unreadable"])
        self.peer([self.row("peer", "codex:peer")])
        status, output, errors = self.run_main("codex:peer")
        self.assertEqual(status, 0, output + errors)
        self.assertIn("delegate source unreadable", errors)
        self.assertEqual(len(self.forwarded()), 1)

    def test_remote_between_turns_seat_address_keeps_no_roster_address_error(self) -> None:
        row = self.row("impl", "seat:remote/impl")
        row = roster.RosterEntry(row.machine, row.kind, row.role, row.name, row.status,
            roster.CannotReceiveMessages("between turns; its unit director resumes it"), row.cwd,
            row.showrunner, row.director, row.assignment, row.thread, row.session)
        self.peer([row])
        status, output, errors = self.run_main("seat:remote/impl")
        self.assertEqual(status, 2, output + errors)
        self.assertIn("no roster session has address seat:remote/impl", errors)
        self.assertEqual(self.forwarded(), [])

    def test_ambiguous_peer_name_lists_every_address_before_sending(self) -> None:
        self.peer([self.row("shared", "codex:one"), self.row("shared", "seat:run/two")])
        status, output, errors = self.run_main("shared")
        self.assertEqual(status, 2, output + errors)
        self.assertIn("codex:one", errors)
        self.assertIn("seat:run/two", errors)
        self.assertEqual(self.forwarded(), [])

    def test_unreachable_peer_reports_one_problem_and_fails_the_send(self) -> None:
        os.environ["SSH_SNAPSHOT"], os.environ["SSH_ROSTER_STATUS"] = "ssh: connection refused", "255"
        status, output, errors = self.run_main("only peer")
        self.assertEqual(status, 3, output + errors)
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertIn("not reached", errors)
        self.assertIn("FAILED:", output)
        self.assertEqual(self.forwarded(), [])

    def test_refusal_advice_matches_worker_ownership_and_socketless_claude_state(self) -> None:
        for role, kind, relationship, reason, expected_tail in (
            ("worker", "codex", roster.UnderDirector("unit", "director", "session:director"), "blind reviewer",
             "; contact its unit director director or wait"),
            ("worker", "codex", roster.NoDirector(), "between turns; no unit director is running", ""),
            ("freestanding", "claude", roster.NoDirector(), "no messaging socket",
             "; wait for it to open its messaging socket, then read the roster again and send again"),
        ):
            for remote in (False, True):
                with self.subTest(role=role, remote=remote):
                    row = roster.RosterEntry("mac" if remote else "natedev", kind, role, "recipient", "waiting",
                        roster.CannotReceiveMessages(reason), "/fixture", roster.NoShowrunner(), relationship,
                        roster.WorkerAssignment("review", "/run") if role == "worker" else roster.NotAWorker())
                    self.local = roster.Roster([] if remote else [row], [])
                    self.peer([row] if remote else [])
                    before = len(self.forwarded())
                    status, output, errors = self.run_main("recipient")
                    self.assertEqual(status, 2, output + errors)
                    self.assertIn(f"recipient cannot take messages ({reason}){expected_tail}\n", errors)
                    self.assertEqual(len(self.forwarded()), before)


class ReadRelayTests(unittest.TestCase):
    def test_delivered_verbatim(self) -> None:
        result = send.read_relay(stream(INIT, call("hello\n"), answer(OK)), "hello")
        self.assertEqual(result.outcome, "sent")
        self.assertEqual(result.detail, "mode auto; verbatim; queued there (msg_id m1)")

    def test_reworded_is_sent_but_flagged(self) -> None:
        self.assertIn("NOT VERBATIM", send.read_relay(stream(INIT, call("hi"), answer(OK)), "hello").detail)

    def test_error_result_is_not_delivered(self) -> None:
        result = send.read_relay(stream(INIT, call("hello"), answer("No agent named bogus", error=True)), "hello")
        self.assertEqual(result, send.Result("queued", "No agent named bogus"))
        refused = answer('{"success":false,"message":"No agent named \'bogus\' is reachable.\\nUse ListAgents."}')
        result = send.read_relay(stream(INIT, call("hello"), refused), "hello")
        self.assertEqual(result, send.Result("queued", "No agent named 'bogus' is reachable. Use ListAgents."))

    def test_retry_after_listagents_counts(self) -> None:
        events = stream(INIT, call("hello"), answer("matched two sessions", error=True),
                        call("hello", "t2"), answer(OK, "t2"))
        result = send.read_relay(events, "hello")
        self.assertEqual(result.outcome, "sent")
        self.assertIn("2 sends", result.detail)

    def test_no_call_and_wrong_mode(self) -> None:
        events = stream({"type": "system", "subtype": "init", "permissionMode": "default"},
                        {"type": "result", "result": "I cannot do that"})
        result = send.read_relay(events, "hello")
        self.assertEqual(result.outcome, "queued")
        self.assertIn("MODE default, NOT auto", result.detail)
        self.assertIn("I cannot do that", result.detail)


if __name__ == "__main__":
    _ = unittest.main()
