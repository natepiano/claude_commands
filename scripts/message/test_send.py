"""send.py's keys, queue, log and relay judgement, with delivery stubbed."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import override
from unittest import mock

import send


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

    def relay(self, message: send.Message, timeout: float) -> send.Result:
        del timeout
        self.relayed.append(message)
        return self.outcome

    def send(self, *args: str) -> send.Result:
        return send.send(send.parse(["--to", "bogus", "--from", "test", *args]))

    def log(self) -> list[dict[str, object]]:
        return [send.as_dict(send.loads(line)) for line in (self.root / "log.jsonl").read_text().splitlines()]

    def interrupted_queue_move(self) -> tuple[Path, Path]:
        old_path = send.queue_path("old")
        new_path = send.queue_path("new")
        old_path.parent.mkdir(parents=True)
        old_entries: list[dict[str, object]] = [
            {"time": "2026-10-07T10:00:00+00:00", "from": "a", "to": "old", "key": None,
             "summary": "free", "text": "free", "reason": "offline"},
            {"time": "2026-10-07T10:01:00+00:00", "from": "a", "to": "old", "key": "named",
             "summary": "named", "text": "named", "reason": "offline"},
        ]
        new_entries = [{**entry, "to": "new"} for entry in old_entries]
        _ = old_path.write_text("".join(json.dumps(entry) + "\n" for entry in old_entries))
        _ = new_path.write_text("".join(json.dumps(entry) + "\n" for entry in new_entries))
        return old_path, new_path

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
