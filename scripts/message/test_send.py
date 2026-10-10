"""send.py's keys, queue, log and relay judgement, with delivery stubbed."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import send
import record as shutdown_record
import restart
import settle


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
        # No test reads the machine's own session records, or passes for the session that runs it.
        (self.root / "sessions").mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                                                       "CLAUDE_CODE_SESSION_ID": ""}))

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
        return send.send(send.parse(["--to", "bogus", "--from", "test", *args]))

    def log(self) -> list[dict[str, object]]:
        return [send.as_dict(send.loads(line)) for line in (self.root / "log.jsonl").read_text().splitlines()]

    def test_repeat_window_is_per_key_and_recipient(self) -> None:
        self.assertEqual(self.send("--key", "k", "--repeat-minutes", "30", "--text", "one").outcome, "sent")
        skipped = self.send("--key", "k", "--repeat-minutes", "30", "--text", "two")
        self.assertEqual(skipped.outcome, "skipped")
        self.assertEqual(send.send(send.parse(["--to", "other", "--key", "k", "--repeat-minutes", "30",
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
        result = send.send(send.parse(["--to", "old", "--from", "test", "--text", "by name"]))
        self.assertEqual(result, send.Result("queued", "relay timed out after 40 s"))
        _ = send.send(send.parse(["--to", f"uds:{self.root / 'id-1.sock'}", "--from", "test", "--text", "by socket"]))
        self.assertEqual([path.name for path in (self.root / "queue").iterdir()], ["session-id-1.jsonl"])
        self.session("id-1", "new", "old")
        # A send to the name the session no longer has is still kept for that session.
        _ = send.send(send.parse(["--to", "old", "--from", "test", "--text", "by the old name"]))
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

        result = send.send(send.parse([
            "--to", "session:id-1", "--from", "test", "--text", "hello by id",
        ]))

        self.assertEqual(result, send.Result("sent", "ok"))
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.relayed[0].to, f"uds:{self.root / 'id-1.sock'}")
        self.assertEqual(self.relayed[0].text, "hello by id")
        self.assertEqual(self.log()[0]["to"], "session:id-1")

    def test_session_id_without_a_live_session_queues_without_relaying(self) -> None:
        result = send.send(send.parse([
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
        first = send.send(send.parse([
            "--to", "session:id-1", "--from", "shutdown", "--key", "shutdown-owner-id-1",
            "--text", "settle first",
        ]))
        self.assertEqual(first.outcome, "queued")

        self.outcome = send.Result("sent", "ok")
        delivered = send.send(send.parse([
            "--to", "session:id-1", "--from", "shutdown", "--key", "shutdown-owner-id-1",
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
            self.assertEqual(send.send(send.parse(argv)).outcome, "queued")

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
            settle._send_settle_message(cancelled, cancel_entry)
            old_cancel = settle.send_message(
                "session:cancelled",
                "old settle",
                "Old unkeyed settle instruction: run /shutdown ready.",
            )
            self.assertEqual(old_cancel["kind"], "queued")
            settle._restore_record(cancelled)
            self.session("cancelled", "cancel-recipient")
            cancel_pending = send.pending("cancel-recipient")
            self.assertNotIn("settle instruction", cancel_pending)
            self.assertIn("cancelled by the user", cancel_pending)

            restarted, restart_entry = shutdown_fixture("restarted")
            settle._send_settle_message(restarted, restart_entry)
            old_restart = settle.send_message(
                "session:restarted",
                "old settle",
                "Old unkeyed settle instruction: run /shutdown ready.",
            )
            self.assertEqual(old_restart["kind"], "queued")
            restart._retire(restarted, restart_entry, dry_run=False)
            delivery = restart._send_restart_note(
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

    def test_session_name_still_relays_with_its_name(self) -> None:
        self.session("id-1", "recipient")

        result = send.send(send.parse([
            "--to", "recipient", "--from", "test", "--text", "hello by name",
        ]))

        self.assertEqual(result, send.Result("sent", "ok"))
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.relayed[0].to, "recipient")

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
        self.assertEqual(send.pending("bogus"), "")
        with mock.patch.object(send, "run", return_value=(255, "", "ssh: connect to host mac: timed out")):
            self.assertEqual(self.send("--machine", "mac", "--text", "hi").outcome, "failed")

    def test_the_user_is_reached_with_a_title_and_a_need_and_never_queued(self) -> None:
        commands: list[list[str]] = []

        def channel(command: list[str], stdin: str, timeout: float) -> tuple[int | None, str, str]:
            del stdin, timeout
            commands.append(command)
            return (0, "", "") if len(commands) == 1 else (1, "", "refused")

        told = ["--to", "user", "--from", "test", "--summary", "natedev: disk", "--need", "blocked", "--text", "full"]
        with mock.patch.object(send, "run", channel):
            self.assertEqual(send.send(send.parse(told)), send.Result("sent", "to the user"))
            self.assertEqual(send.send(send.parse(told)).outcome, "failed")
        self.assertEqual(commands[0][2:], ["--priority", "2", "natedev: disk", "full"])
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
            result = send.send(send.parse(["--to", "user", "--summary", "mac: login", "--need", "decision",
                                           "--machine", "natedev", "--text", "hi"]))
        self.assertEqual(result, send.Result("sent", "on natedev: to the user"))
        self.assertIn("--need decision", commands[0][-1])

    def test_usage_errors_exit_2(self) -> None:
        for argv in (["--to", "x", "--repeat-minutes", "5", "--text", "t"], ["--to", "x", "--codex", "--text", "t"],
                     ["--to", "x", "--text", "  "], ["ack"], ["--to", "user", "--text", "t"],
                     ["--to", "x", "--need", "decision", "--text", "t"]):
            with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr"):
                _ = send.main(argv)
            self.assertEqual(raised.exception.code, 2, argv)


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
