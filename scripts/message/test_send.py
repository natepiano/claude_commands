"""send.py's keys, queue, log and relay judgement, with delivery stubbed."""

from __future__ import annotations

import io
import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import shlex
import sys
import tempfile
import time
import unittest
from collections.abc import Generator
from contextlib import AbstractContextManager, closing, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.socket_test_support import ShortUnixSocket, short_socket_directory
import send
from scripts.lib import names
from scripts.lib.test_names import fixture_names
import rca
import review
import push_texts
import postmaster
import silence
from test_postmaster import FakeClock
import codex_daemon
from codex_test_support import FakeDaemon, daemon_session_row, receive_frame, reply, write_codex_account
import shutdown_record
import restart
import roster
import restart_support
if TYPE_CHECKING:
    from ..accounts import accounts
else:
    from scripts.accounts import accounts
from test_support import ShutdownTestCase, ShutdownSendLeaf
from user_zone import user_zone
from user_action import REFUSED_BEFORE_SENDING, ActionRequired, NoActionRequired, first_line
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


@contextmanager
def interrupt_replacement(target: Path, *, after: bool = False, skip: int = 0) -> Generator[None]:
    """Stop before or after one chosen replacement, after skipping earlier writes to that target."""
    replace = os.replace
    matched = 0
    def checkpoint(source: str | Path, destination: str | Path) -> None:
        nonlocal matched
        chosen = Path(destination) == target
        stop = chosen and matched == skip
        if chosen:
            matched += 1
        if stop and not after:
            raise KeyboardInterrupt
        replace(source, destination)
        if stop:
            raise KeyboardInterrupt
    with mock.patch.object(os, "replace", checkpoint):
        yield


INIT: dict[str, object] = {"type": "system", "subtype": "init", "permissionMode": "auto"}


class RelayAccountTests(unittest.TestCase):
    def test_relay_publishes_its_sender_before_exec_and_keeps_the_socket_owners_pid(self) -> None:
        with short_socket_directory() as directory:
            root = Path(directory)
            executable = root / "fake-claude"
            _ = executable.write_text(
                f"#!{sys.executable}\n"
                + "import os, pathlib, socket, sys\n"
                + "root = pathlib.Path(sys.argv[1])\n"
                + "assert (root / 'relay_senders' / str(os.getpid())).read_text() == 'verify'\n"
                + "with socket.socket(socket.AF_UNIX) as listener:\n"
                + "    listener.bind(str(root / (str(os.getpid()) + '.sock')))\n"
                + "    print(os.getpid())\n")
            executable.chmod(0o755)
            completed = subprocess.run([sys.executable, "-c", send.RELAY_EXEC,
                str(root / "relay_senders"), "verify", str(executable), str(root)],
                capture_output=True, text=True, check=True)
            self.assertEqual((root / "relay_senders" / completed.stdout.strip()).read_text(), "verify")
            self.assertEqual(list((root / "relay_senders").glob(".*.tmp")), [])

    def test_an_interrupted_relay_record_publish_retries_before_starting_claude(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = ["-c", str(root), "verify", "/fixture/claude", "-p"]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(os, "getpid", return_value=4242), \
                    mock.patch.object(os, "execvpe", side_effect=KeyboardInterrupt) as launch:
                with mock.patch.object(os, "replace", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                    exec(send.RELAY_EXEC, {})
                launch.assert_not_called()
                self.assertFalse((root / "4242").exists())
                with self.assertRaises(KeyboardInterrupt):
                    exec(send.RELAY_EXEC, {})
                self.assertEqual((root / "4242").read_text(), "verify")
                launch.assert_called_once_with("/fixture/claude", ["/fixture/claude", "-p"], os.environ)
                self.assertFalse((root / ".4242.tmp").exists())

    def test_relay_delivers_captured_sender_display_and_checks_that_payload_verbatim(self) -> None:
        original = "Message from sender-address: Preview\n\nbody"
        displayed = "Message from sender-address: Preview\nShown sender: sdlc · roles · worker test\n\nbody"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            _ = (home / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": "fixture@example.invalid"}}))
            for delivered, status in ((displayed, "verbatim"), (original, "NOT VERBATIM")):
                with (
                    self.subTest(status=status),
                    mock.patch.dict(os.environ, {"HOME": str(home)}, clear=True),
                    mock.patch.object(send, "claude_binary", return_value="claude"),
                    mock.patch.object(send, "STATE", home / "message-state"),
                    mock.patch.object(send, "run", return_value=(0, stream(INIT, call(delivered), answer(OK)), "")) as launch,
                ):
                    result = send.relay(send.Message("uds:/recipient", "sender-address", "Preview", original, None,
                                                    "sdlc · roles · worker test"), 10)
                self.assertEqual(result.outcome, "sent")
                self.assertIn(f"; {status};", result.detail)
                command = cast(list[str], launch.call_args.args[0])
                self.assertEqual(command[command.index("-n") + 1], "sender-address")
                self.assertIn(displayed, cast(str, launch.call_args.args[1]))

    def test_relay_before_cutover_keeps_home_login_without_requiring_numbered_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            _ = (home / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": "legacy@example.invalid"}}))
            with (mock.patch.dict(os.environ, {"HOME": str(home), "CLAUDE_CODE_SESSION_ID": "caller",
                                              "CLAUDE_CODE_REMOTE_MEMORY_DIR": "inherited-memory"}, clear=True),
                  mock.patch.object(send, "claude_binary", return_value="claude"),
                  mock.patch.object(send, "STATE", home / "message-state"),
                  mock.patch.object(send, "run", return_value=(0, "", "")) as launch):
                _ = send.relay(send.Message("uds:/unused", "test", "hello", "body", None), 10)
            launch.assert_called_once()
            environment = cast(dict[str, str], launch.call_args.args[3])
            self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
            self.assertNotIn("CLAUDE_CODE_REMOTE_MEMORY_DIR", environment)
            self.assertNotIn("CLAUDE_CODE_SESSION_ID", environment)
            self.assertEqual(list(home.glob(".claude[0-9]*")), [])

    def test_relay_uses_the_caller_account_or_showrunners_without_inheriting_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            notes = home / "notes"
            notes.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home), "AGENT_NOTES_DIR": str(notes)}, clear=True):
                (home / ".claude").mkdir()
                _ = (home / ".claude.json").write_text("{}")
                for label in ("claude1", "claude3"):
                    _ = (notes / f"{label}.md").write_text(f"---\nlogin: {label}@example.invalid\n---\n")
                    self.assertEqual(accounts.setup(label), [])
                    _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({
                        "oauthAccount": {"emailAddress": f"{label}@example.invalid"}}))
                groups = accounts.groups_file()
                groups.parent.mkdir(parents=True, exist_ok=True)
                _ = groups.write_text(json.dumps({"showrunners": "claude3"}))
                for caller, expected in (("claude1", "claude1"), ("", "claude3")):
                    with self.subTest(caller=caller):
                        if caller:
                            os.environ["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(caller))
                        else:
                            _ = os.environ.pop("CLAUDE_CONFIG_DIR", None)
                        os.environ["CLAUDE_CODE_SESSION_ID"] = "caller-session"
                        with (mock.patch.object(send, "claude_binary", return_value="claude"),
                              mock.patch.object(send, "STATE", home / "message-state"),
                              mock.patch.object(send, "run", return_value=(0, "", "")) as launch):
                            _ = send.relay(send.Message("uds:/unused", "test", "hello", "body", None), 10)
                        environment = cast(dict[str, str], launch.call_args.args[3])
                        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(accounts.account_dir(expected)))
                        self.assertEqual(environment["CLAUDE_CODE_REMOTE_MEMORY_DIR"], str(home / ".claude"))
                        self.assertNotIn("CLAUDE_CODE_SESSION_ID", environment)


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


class SendTests(ShutdownTestCase):
    def test_outcome_parser_preserves_first_header_payload_and_acknowledgement(self) -> None:
        acknowledged = send.acknowledged_skip_reason("shutdown-fixture", "2026-10-30T12:00:00+00:00")
        self.assertEqual(acknowledged, "key 'shutdown-fixture' acknowledged at 2026-10-30T12:00:00+00:00; `send.py reopen` to resume")
        window = "key 'shutdown-fixture' last sent to Unit at 2026-10-30T12:00:00+00:00, inside its 60-minute window"
        cases: tuple[tuple[str, send.Outcome, str, str, bool], ...] = (
            ("SENT: to the user\n", "sent", "to the user", "", False),
            ("SKIPPED: " + window + "\n", "skipped", window, "", False),
            ("SKIPPED: " + acknowledged + "\n", "skipped", acknowledged, "", True),
            ("KEPT: until silence ends\n", "kept", "until silence ends", "", False),
            ("REPLACED: newer push\n", "replaced", "newer push", "", False),
            ("QUEUED: recipient offline\n", "queued", "recipient offline", "", False),
            ("HELD: review refused\n", "held", "review refused", "", False),
            ("FAILED: transport failed\n", "failed", "transport failed", "", False),
            ("REFUSED: action required\n", "refused", "action required", "", False),
            ("reviewing draft\nunknown: progress\nSENT: delivered\n", "sent", "delivered", "", False),
            ("SENT: rewritten:\nnew body\nFAILED: quoted earlier result\n\n", "sent", "rewritten:", "new body\nFAILED: quoted earlier result\n", False),
            ("SENT", "sent", "", "", False),
        )
        for output, outcome, detail, payload, is_acknowledged in cases:
            with self.subTest(output=output):
                parsed = send.parse_outcome(output)
                self.assertIsInstance(parsed, send.OutcomeHeader)
                assert isinstance(parsed, send.OutcomeHeader)
                self.assertEqual((parsed.outcome, parsed.detail, parsed.payload), (outcome, detail, payload))
                self.assertEqual(send.is_acknowledged_skip(parsed.detail), is_acknowledged)
        for output in ("", "reviewing draft\nunknown: still working\n", "sent: not a protocol header\n"):
            with self.subTest(output=output):
                self.assertIsInstance(send.parse_outcome(output), send.NoOutcomeHeader)

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.relayed: list[send.Message] = []
        self.outcome: send.Result = send.Result("sent", "ok")
        self.cases: list[rca.Report] = []
        # What the fake push review gives; None approves the draft. Each review's draft and time limit.
        self.verdict: review.ReviewOutcome | None = None
        self.reviews: list[tuple[review.Draft, float]] = []

    @override
    def setUp(self) -> None:
        _ = self.enterContext(mock.patch.object(names, "load_names", return_value=fixture_names()))
        super().setUp()
        directory = short_socket_directory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # No test opens a real rca.py case: it would write the machine's cases and message its owner.
        _ = self.enterContext(mock.patch.object(rca, "open_case", self.open_case))
        # No test runs the real push review (claude -p): an auto_mode.refused send reviews its waits sentence.
        _ = self.enterContext(mock.patch.object(review, "review", side_effect=self.review))
        for name, value in (("STATE", self.root), ("SESSIONS", self.root / "sessions"), ("relay", self.relay)):
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
                                                       "CLAUDE_CODE_SESSION_ID": "", "NOTIFY_STATE_DIR": str(self.root / "notify"), "CLAUDE_PID": ""}))
        _ = self.enterContext(
            mock.patch.object(roster, "roster", return_value=roster.Roster([], []))
        )

    def daemon_owner(self) -> codex_daemon.CodexDaemon:
        home = self.root / ".codex2"
        return codex_daemon.CodexDaemon(home, home / "app-server-control/app-server-control.sock")

    def daemon_row(self, thread: str, name: str = "") -> roster.RosterEntry:
        return daemon_session_row(self.daemon_owner(), thread, name or thread)

    def claude_row(self, session_id: str, name: str) -> roster.RosterEntry:
        self.session(session_id, name)
        return roster.RosterEntry(
            "machine", "claude", "freestanding", name, "running",
            roster.Messageable(f"session:{session_id}"), "/work", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(),
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

    def open_case(self, report: rca.Report, at: object = None) -> str:
        del at
        self.cases.append(report)
        return "rca: opened 261023-1"

    def relay(self, message: send.Message, timeout: float) -> send.Result:
        del timeout
        self.relayed.append(message)
        return self.outcome

    def review(self, draft: review.Draft, revision: bool, *, limit: float = 0) -> review.ReviewOutcome:
        del revision
        self.reviews.append((draft, limit))
        return self.verdict or review.Approved(draft)

    def send(self, *args: str) -> send.Result:
        return send.send(local_options(["--to", "bogus", "--from", "test", *args]))

    def log(self) -> list[dict[str, object]]:
        return [send.as_dict(send.loads(line)) for line in (self.root / "log.jsonl").read_text().splitlines()]

    def drain(self) -> tuple[int, list[str]]:
        """Capture checkpoint reports together with the deferred reports that main prints."""
        output = io.StringIO()
        with redirect_stdout(output):
            code, lines = send.drain_socket_queues()
        return code, [*output.getvalue().splitlines(), *lines]

    def postmaster_clock(self) -> FakeClock:
        clock = FakeClock()
        _ = self.enterContext(mock.patch.object(time, "time", clock.time))
        _ = self.enterContext(mock.patch.object(time, "monotonic", clock.monotonic))
        _ = self.enterContext(mock.patch.object(time, "sleep", clock.sleep))
        return clock

    def postmaster_heartbeat(self, clock: FakeClock, identity: str = "worker") -> None:
        path = self.root / "postmaster" / "heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(postmaster.Heartbeat(session_id=identity,
            at_ms=int(clock.time() * 1000), version="0.1.0", run="1800000000000-test-worker")))

    def postmaster_mod(self, clock: FakeClock, texts: list[str], *, matching: bool = True) -> None:
        def poll() -> None:
            for path in (self.root / "postmaster" / "outbox").glob("*.json"):
                value = send.as_dict(send.loads(path.read_text()))
                text = value["text"]
                assert isinstance(text, str)
                texts.append(text)
                identity = path.stem
                result = postmaster.OutboxResult(id=identity, outcome="delivered", detail="",
                    sha256=hashlib.sha256(text.encode()).hexdigest() if matching else "0" * 64,
                    claimed_ms=int(clock.time() * 1000), finished_ms=int(clock.time() * 1000))
                target = self.root / "postmaster" / "results" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_text(json.dumps(result))
        clock.after_sleep = poll

    def test_postmaster_delivery_prints_sent_logs_elapsed_time_and_never_relays(self) -> None:
        clock = self.postmaster_clock()
        self.session("recipient", "Recipient")
        self.session("worker", "Postmaster")
        self.session("sender", "Sender")
        self.postmaster_heartbeat(clock)
        texts: list[str] = []
        self.postmaster_mod(clock, texts)
        original_poll = clock.after_sleep
        def poll() -> None:
            clock.elapsed = 0.125
            original_poll()
        clock.after_sleep = poll
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(send.main(["--here", "--to", "session:recipient", "--from", "Sender", "--summary", "Question", "--text", "body"]), 0)
        self.assertEqual(output.getvalue(), "SENT: postmaster; verbatim; session:recipient\n")
        self.assertEqual(self.relayed, [])
        self.assertEqual(len(texts), 1)
        self.assertRegex(texts[0], r"^Message from Sender via postmaster: Question \[[a-f0-9]{8}\]\n\nbody$")
        entry = self.log()[0]
        self.assertEqual((entry["via"], entry["postmaster"], entry["seconds"], entry["text"]), ("postmaster", "delivered", 0.125, "body"))

    def test_postmaster_shows_sender_job_and_keeps_reply_and_log_identity(self) -> None:
        clock = self.postmaster_clock()
        self.session("recipient", "Recipient")
        self.session("worker", "Postmaster")
        self.session("sender", "fixed-sender-address")
        self.postmaster_heartbeat(clock)
        texts: list[str] = []
        self.postmaster_mod(clock, texts)
        sender = roster.RosterEntry(
            machine="fixture", kind="claude", role="worker", name="fixed-sender-address", status="working",
            reach=roster.Messageable("session:sender"), cwd="/fixture",
            showrunner=roster.UnderShowrunner("sdlc", "runner", "session:runner"),
            director=roster.UnderDirector("roles-unit", "director", "session:director"),
            assignment=roster.WorkerAssignment("test", "/fixture/run", "test", "test"),
            session=roster.ClaudeSession("sender"),
        )
        with mock.patch.object(roster, "roster", return_value=roster.Roster([sender], [])):
            result = send.send(local_options(["--to", "session:recipient", "--from", "fixed-sender-address",
                                               "--summary", "Question", "--text", "body"]))
        self.assertEqual(result.outcome, "sent")
        self.assertEqual(len(texts), 1)
        self.assertIn("sdlc · roles · worker test", texts[0])
        self.assertRegex(texts[0], r"^Message from fixed-sender-address via postmaster: Question \[[a-f0-9]{8}\]")
        self.assertIn("body", texts[0])
        logged = self.log()[0]
        self.assertEqual(logged["from"], "fixed-sender-address")
        self.assertEqual(logged["to"], "session:recipient")
        self.assertEqual(self.relayed, [])

    def test_postmaster_hash_mismatch_is_reported_as_not_verbatim(self) -> None:
        clock = self.postmaster_clock()
        self.session("recipient", "Recipient")
        self.session("worker", "Postmaster")
        self.postmaster_heartbeat(clock)
        self.postmaster_mod(clock, [], matching=False)
        result = send.send(local_options(["--to", "session:recipient", "--from", "Sender", "--text", "body"]))
        self.assertEqual(result, send.Result("sent", "postmaster; NOT VERBATIM; session:recipient"))
        self.assertEqual(self.relayed, [])

    def sender_entry(self, production: str = "sdlc") -> roster.RosterEntry:
        return roster.RosterEntry(
            machine="fixture", kind="claude", role="worker", name="sender-address", status="working",
            reach=roster.Messageable("session:sender"), cwd="/fixture",
            showrunner=roster.UnderShowrunner(production, "runner", "session:runner"),
            director=roster.UnderDirector("roles-unit", "director", "session:director"),
            assignment=roster.WorkerAssignment("test", "/fixture/run", "test", "test"),
            session=roster.ClaudeSession("sender"),
        )

    def test_name_config_rename_changes_new_senders_but_keeps_queued_and_forwarded_display(self) -> None:
        options = local_options(["--to", "session:absent", "--from", "sender-address", "--text", "before rename"])
        with mock.patch.object(roster, "roster", return_value=roster.Roster([self.sender_entry()], [])):
            self.assertEqual(send.send(options).outcome, "queued")
            with mock.patch.object(names, "runtime_names", return_value=fixture_names(worker="builder")):
                newer = local_options(["--to", "session:absent", "--from", "sender-address", "--text", "after rename"])
                self.assertEqual(send.send(newer).outcome, "queued")
        queued = send.read_queue(send.queue_for("session:absent"))
        self.assertEqual([row.get("shown_sender") for row in queued],
                         ["sdlc · roles · worker test", "sdlc · roles · builder test"])
        self.assertEqual([row["from"] for row in queued], ["sender-address", "sender-address"])
        self.session("absent", "Returned recipient")
        with mock.patch.object(names, "runtime_names", side_effect=AssertionError("replay must keep the captured display")):
            replay = send.pending("session:absent")
        self.assertIn("Shown sender: sdlc · roles · worker test", replay)
        self.assertIn("Shown sender: sdlc · roles · builder test", replay)
        received = send.parse(["--here", "--to", "codex:remote-thread", "--from", "sender-address",
                               "--forwarded-shown-sender", "sdlc · roles · worker test", "--summary", "Preview", "--text", "body"])
        with mock.patch.object(names, "runtime_names", side_effect=AssertionError("receiver must not apply its names")), \
                mock.patch.object(codex_daemon, "queue", return_value=codex_daemon.Queued()) as queue, \
                mock.patch.object(roster, "roster", return_value=roster.Roster([self.daemon_row("remote-thread")], [])):
            self.assertEqual(send.send(received).outcome, "sent")
        queue.assert_called_once_with(self.daemon_owner(), "remote-thread", "body\nShown sender: sdlc · roles · worker test", timeout=send.TIMEOUT)

    def test_queued_sender_display_survives_roster_changes_and_legacy_records_use_raw_sender(self) -> None:
        queued_at = datetime(2026, 10, 20, tzinfo=timezone.utc)
        with mock.patch.object(roster, "roster", return_value=roster.Roster([self.sender_entry()], [])) as read_roster:
            for keyed in (False, True):
                options = local_options([
                    "--to", "session:absent", "--from", "sender-address", "--text", "Preview\nbody",
                    *(["--key", "message-key"] if keyed else []),
                ])
                with mock.patch.object(send, "now", return_value=queued_at + timedelta(seconds=int(keyed))):
                    self.assertEqual(send.send(options).outcome, "queued")
            self.assertEqual(read_roster.call_count, 2)
        path = send.queue_for("session:absent")
        queued = send.read_queue(path)
        self.assertEqual([entry["from"] for entry in queued], ["sender-address", "sender-address"])
        self.assertEqual([entry.get("shown_sender") for entry in queued], ["sdlc · roles · worker test"] * 2)
        legacy = dict(queued[0])
        del legacy["shown_sender"]
        legacy["text"] = "legacy body"
        with path.open("a", encoding="utf-8") as target:
            _ = target.write(json.dumps(legacy) + "\n")
        self.session("absent", "Returned recipient")
        with mock.patch.object(roster, "roster", side_effect=AssertionError("replay must not resolve the sender")):
            replay = send.pending("session:absent")
        self.assertEqual(replay.count("Shown sender: sdlc · roles · worker test"), 2)
        self.assertRegex(replay, r"(?:\A|\n\n)Queued message \d+ of 3, from sender-address at "
                         + r"[^\n]+:\nlegacy body(?:\n\n|\Z)")
        self.assertEqual([entry["from"] for entry in self.log()], ["sender-address", "sender-address"])

    def test_mesh_and_daemon_payloads_show_sender_without_changing_address_thread_or_log(self) -> None:
        original = "Message from sender-address: Preview\n\nbody"
        expected = "Message from sender-address: Preview\nShown sender: sdlc · roles · worker test\n\nbody"
        with (
            mock.patch.object(roster, "roster", return_value=roster.Roster([self.sender_entry(), self.daemon_row("thread-address")], [])),
            mock.patch.object(send, "run", return_value=(0, "sent", "")) as launch,
            mock.patch.object(codex_daemon, "queue", return_value=codex_daemon.Queued()) as queue,
        ):
            self.assertEqual(send.send(local_options([
                "--to", "mesh-address", "--codex", "--session-dir", "/fixture/run",
                "--from", "sender-address", "--text", original,
            ])).outcome, "sent")
            self.assertEqual(send.send(local_options([
                "--to", "codex:thread-address", "--from", "sender-address", "--text", original,
            ])).outcome, "sent")
        command = cast(list[str], launch.call_args.args[0])
        self.assertEqual(command[command.index("--to") + 1], "mesh-address")
        self.assertEqual(command[command.index("--session-dir") + 1], "/fixture/run")
        self.assertEqual(command[command.index("--message") + 1], expected)
        queue.assert_called_once_with(self.daemon_owner(), "thread-address", expected, timeout=send.TIMEOUT)
        self.assertEqual([(row["from"], row["to"], row["text"]) for row in self.log()], [
            ("sender-address", "mesh-address", original), ("sender-address", "codex:thread-address", original),
        ])

    def test_relay_fallback_preserves_shown_sender_when_session_address_becomes_socket(self) -> None:
        self.session("recipient", "Recipient")
        with mock.patch.object(roster, "roster", return_value=roster.Roster([self.sender_entry()], [])) as read_roster:
            self.assertEqual(send.send(local_options([
                "--to", "session:recipient", "--from", "sender-address", "--summary", "Preview",
                "--text", "Message from sender-address: Preview\n\nbody",
            ])).outcome, "sent")
        read_roster.assert_called_once()
        relayed = self.relayed[0]
        self.assertEqual(relayed.to, f"uds:{self.root / 'recipient.sock'}")
        self.assertEqual(relayed.sender, "sender-address")
        prompt = send.relay_prompt(relayed)
        self.assertIn("----- BEGIN MESSAGE -----\nMessage from sender-address: Preview\n"
                      + "Shown sender: sdlc · roles · worker test\n\nbody\n----- END MESSAGE -----", prompt)
        self.assertEqual(self.log()[0]["from"], "sender-address")

    def test_remote_forwarding_keeps_sending_machine_display_despite_receiver_name_collision(self) -> None:
        original = "Message from sender-address: Preview\n\nbody"
        for sender_rows, display in (
            ([self.sender_entry()], "sdlc · roles · worker test"),
            ([], "sender-address"),
            ([self.sender_entry(), self.sender_entry("ambiguous")], "sender-address"),
        ):
            with self.subTest(display=display, matches=len(sender_rows)):
                with (
                    mock.patch.object(roster, "roster", return_value=roster.Roster(sender_rows, [])) as source_read,
                    mock.patch.object(send, "run", return_value=(0, "SENT: forwarded\n", "")) as ssh,
                ):
                    self.assertEqual(send.send(send.parse([
                        "--machine", "mac", "--to", "codex:remote-thread", "--from", "sender-address",
                        "--key", "reply-key", "--text", original,
                    ])).outcome, "sent")
                source_read.assert_called_once()
                command = cast(list[str], ssh.call_args.args[0])
                forwarded = shlex.split(command[-1])
                arguments = forwarded[forwarded.index("--here"):]
                self.assertEqual(arguments[arguments.index("--from") + 1], "sender-address")
                self.assertEqual(arguments[arguments.index("--forwarded-shown-sender") + 1], display)
                self.assertEqual(ssh.call_args.args[1], original)
                received = send.parse([*arguments, "--text", original])
                with (
                    mock.patch.object(roster, "roster", return_value=roster.Roster([self.sender_entry("wrong-host"), self.daemon_row("remote-thread")], [])),
                    mock.patch.object(send, "sender_shown_name", side_effect=AssertionError("receiver must not resolve sender")),
                    mock.patch.object(codex_daemon, "queue", return_value=codex_daemon.Queued()) as queue,
                ):
                    self.assertEqual(send.send(received).outcome, "sent")
                expected = (original if display == "sender-address" else
                            "Message from sender-address: Preview\nShown sender: sdlc · roles · worker test\n\nbody")
                queue.assert_called_once_with(self.daemon_owner(), "remote-thread", expected, timeout=send.TIMEOUT)
                logged = self.log()[-1]
                self.assertEqual((logged["from"], logged["to"], logged["key"], logged["text"]),
                                 ("sender-address", "codex:remote-thread", "reply-key", original))

    def test_forwarded_postmaster_display_does_not_read_receiver_roster_for_sender(self) -> None:
        clock = self.postmaster_clock()
        self.session("recipient", "Recipient")
        self.session("worker", "Postmaster")
        self.postmaster_heartbeat(clock)
        texts: list[str] = []
        self.postmaster_mod(clock, texts)
        with mock.patch.object(roster, "roster", side_effect=AssertionError("explicit socket needs no receiver roster")):
            result = send.send(send.parse([
                "--here", "--to", f"uds:{self.root / 'recipient.sock'}", "--from", "sender-address",
                "--forwarded-shown-sender", "sdlc · roles · worker test", "--summary", "Preview", "--text", "body",
            ]))
        self.assertEqual(result.outcome, "sent")
        self.assertRegex(texts[0], r"^Message from sender-address via postmaster: Preview \[[a-f0-9]{8}\]\n"
                         + r"Shown sender: sdlc · roles · worker test\n\nbody$")

    def test_forwarded_sender_display_requires_local_delivery_and_nonempty_text(self) -> None:
        for arguments in (["--forwarded-shown-sender", "display"],
                          ["--here", "--forwarded-shown-sender", ""]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _ = send.parse(["--to", "codex:thread", "--from", "sender-address", "--text", "body", *arguments])

    def test_each_postmaster_miss_relays_once_with_unchanged_message_and_reason(self) -> None:
        self.session("recipient", "Recipient")
        reasons: list[postmaster.PostmasterMiss] = ["absent", "stale", "self", "unclaimed", "outbox-error", "failed", "expired", "claimed-no-result", "relay-only-sender"]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.relayed.clear()
                with mock.patch.object(postmaster, "attempt", return_value=postmaster.RelayRequired(reason, 0.0)) as attempt:
                    result = send.send(local_options(["--to", "session:recipient", "--from", "Sender", "--summary", "Question", "--text", "body"]))
                self.assertEqual(result, self.outcome)
                self.assertEqual(self.relayed, [send.Message(f"uds:{self.root / 'recipient.sock'}", "Sender", "Question", "body", None)])
                attempt.assert_called_once()
                self.assertEqual((self.log()[-1]["via"], self.log()[-1]["postmaster"]), ("relay", reason))

    def test_postmaster_self_and_shutdown_use_real_client_then_relay_once(self) -> None:
        clock = self.postmaster_clock()
        self.session("recipient", "Recipient")
        self.session("worker", "Postmaster")
        self.postmaster_heartbeat(clock)
        for to, sender, reason in (("session:worker", "Sender", "self"), ("session:recipient", "shutdown", "relay-only-sender")):
            with self.subTest(reason=reason):
                self.relayed.clear()
                result = send.send(local_options(["--to", to, "--from", sender, "--text", "body"]))
                self.assertEqual(result, self.outcome)
                self.assertEqual(len(self.relayed), 1)
                self.assertEqual(self.relayed[0].text, "body")
                self.assertEqual((self.log()[-1]["via"], self.log()[-1]["postmaster"]), ("relay", reason))
                self.assertFalse((self.root / "postmaster" / "outbox").exists())

    def test_postmaster_write_error_relays_once(self) -> None:
        clock = self.postmaster_clock()
        self.session("recipient", "Recipient")
        self.session("worker", "Postmaster")
        self.postmaster_heartbeat(clock)
        original = os.replace
        def replace(source: str | Path, target: str | Path) -> None:
            if Path(target).parent.name == "outbox":
                raise OSError("outbox is unavailable")
            original(source, target)
        with mock.patch.object(os, "replace", replace):
            result = send.send(local_options(["--to", "session:recipient", "--from", "Sender", "--text", "body"]))
        self.assertEqual(result, self.outcome)
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.log()[-1]["postmaster"], "outbox-error")

    def test_queued_relay_after_postmaster_miss_keeps_session_key(self) -> None:
        self.session("recipient", "Recipient")
        self.outcome = send.Result("queued", "relay unavailable")
        with mock.patch.object(postmaster, "attempt", return_value=postmaster.RelayRequired("failed", 0.0)):
            result = send.send(local_options(["--to", "session:recipient", "--from", "Sender", "--key", "k", "--text", "body"]))
        self.assertEqual(result, self.outcome)
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual([path.name for path in (self.root / "queue").iterdir()], ["session-recipient.jsonl"])
        self.assertEqual((self.log()[-1]["via"], self.log()[-1]["postmaster"]), ("relay", "failed"))

    def test_queued_relay_reports_recipient_that_exits_during_delivery(self) -> None:
        self.session("recipient", "Recipient")
        def relay(message: send.Message, timeout: float) -> send.Result:
            del timeout
            self.relayed.append(message)
            (self.root / "sessions" / "recipient.json").unlink()
            return send.Result("queued", "relay unavailable")
        output = io.StringIO()
        with mock.patch.object(send, "relay", relay), mock.patch.object(
            postmaster, "attempt", return_value=postmaster.RelayRequired("failed", 0.0)
        ), redirect_stdout(output):
            self.assertEqual(send.main(["--here", "--to", "session:recipient", "--from", "Sender", "--text", "body"]), 1)
        self.assertEqual(output.getvalue(),
            "QUEUED: relay unavailable; no live session answers to session:recipient,"
            + " so it is kept under that name\n")
        self.assertEqual(len(self.relayed), 1)

    def test_relay_timeout_uses_remaining_postmaster_budget_with_one_second_minimum(self) -> None:
        self.session("recipient", "Recipient")
        for seconds, timeout in ((5.0, 25.0), (35.0, 1.0)):
            with self.subTest(seconds=seconds), mock.patch.object(
                postmaster, "attempt", return_value=postmaster.RelayRequired("claimed-no-result", seconds)
            ), mock.patch.object(send, "relay", return_value=self.outcome) as relay:
                result = send.send(local_options(["--to", "session:recipient", "--from", "Sender",
                    "--text", "body", "--timeout", "30"]))
                self.assertEqual(result, self.outcome)
                relay.assert_called_once_with(
                    send.Message(f"uds:{self.root / 'recipient.sock'}", "Sender", "body", "body", None), timeout)
                self.assertEqual(self.log()[-1]["postmaster"], "claimed-no-result")

    def test_recipient_outside_registry_keeps_full_relay_timeout(self) -> None:
        with mock.patch.object(postmaster, "attempt", side_effect=AssertionError("no registered recipient")), mock.patch.object(
            send, "relay", return_value=self.outcome
        ) as relay:
            self.assertEqual(self.send("--text", "body", "--timeout", "30"), self.outcome)
        relay.assert_called_once_with(send.Message("bogus", "test", "body", "body", None), 30.0)
        self.assertEqual(self.log()[-1]["postmaster"], "unresolved")

    def test_postmaster_success_retires_queued_copy_for_key(self) -> None:
        clock = self.postmaster_clock()
        self.session("recipient", "Recipient")
        self.session("worker", "Postmaster")
        options = local_options(["--to", "session:recipient", "--from", "Sender", "--key", "k", "--text", "body"])
        self.outcome = send.Result("queued", "relay unavailable")
        self.assertEqual(send.send(options).outcome, "queued")
        self.postmaster_heartbeat(clock)
        self.postmaster_mod(clock, [])
        self.assertEqual(send.send(options).outcome, "sent")
        self.assertEqual(send.pending("session:recipient"), "")
        self.assertEqual(len(self.relayed), 1)

    def test_equal_messages_have_different_postmaster_tags(self) -> None:
        clock = self.postmaster_clock()
        self.session("recipient", "Recipient")
        self.session("worker", "Postmaster")
        self.postmaster_heartbeat(clock)
        texts: list[str] = []
        self.postmaster_mod(clock, texts)
        options = local_options(["--to", "session:recipient", "--from", "Sender", "--summary", "Question", "--text", "body"])
        self.assertEqual(send.send(options).outcome, "sent")
        self.assertEqual(send.send(options).outcome, "sent")
        self.assertEqual(len(texts), 2)
        self.assertNotEqual(texts[0], texts[1])
        self.assertEqual(self.relayed, [])

    def test_registry_miss_relays_and_logs_unresolved_without_attempt(self) -> None:
        with mock.patch.object(postmaster, "attempt", side_effect=AssertionError("unresolved registry attempt")):
            self.assertEqual(self.send("--text", "body"), self.outcome)
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual((self.log()[-1]["via"], self.log()[-1]["postmaster"]), ("relay", "unresolved"))

    def test_absent_session_address_queues_and_logs_queue_without_a_relay(self) -> None:
        with mock.patch.object(postmaster, "attempt", side_effect=AssertionError("no live recipient")):
            result = send.send(local_options(["--to", "session:gone", "--from", "Sender", "--text", "body"]))
        self.assertEqual(result.outcome, "queued")
        self.assertEqual(self.relayed, [])
        self.assertEqual((self.log()[-1]["via"], self.log()[-1]["postmaster"]), ("queue", "unresolved"))

    def test_skipped_message_has_no_claude_route_fields(self) -> None:
        _ = send.acknowledge("route-key")
        self.assertEqual(self.send("--key", "route-key", "--text", "body").outcome, "skipped")
        self.assertTrue({"via", "postmaster", "seconds"}.isdisjoint(self.log()[-1]))
        self.assertEqual(self.relayed, [])

    def test_user_delivery_has_no_claude_route_fields(self) -> None:
        with mock.patch.object(send, "user", return_value=send.Result("sent", "phone")):
            result = send.send(local_options(["--to", "user", "--from", "Sender", "--summary", "Question", "--no-action", "--text", "body"]))
        self.assertEqual(result, send.Result("sent", "phone"))
        self.assertTrue({"via", "postmaster", "seconds"}.isdisjoint(self.log()[-1]))
        self.assertEqual(self.relayed, [])

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

    def test_a_failed_keyed_session_send_retries_but_a_delivered_send_keeps_its_window(self) -> None:
        options = local_options(["--to", "Recipient", "--from", "Sender", "--key", "result",
                                 "--repeat-minutes", "30", "--text", "The unit is back."])
        started = send.now()
        self.outcome = send.Result("failed", "transport refused the message")
        with mock.patch.object(send, "now", return_value=started):
            self.assertEqual(send.send(options).outcome, "failed")
        self.outcome = send.Result("sent", "delivered")
        with mock.patch.object(send, "now", return_value=started + timedelta(seconds=1)):
            self.assertEqual(send.send(options).outcome, "sent")
        with mock.patch.object(send, "now", return_value=started + timedelta(seconds=2)):
            self.assertEqual(send.send(options).outcome, "skipped")
        self.assertEqual([message.text for message in self.relayed], [options.text, options.text])
        self.assertEqual([entry["outcome"] for entry in self.log()], ["failed", "sent", "skipped"])

    def test_a_queued_keyed_session_send_keeps_one_copy_and_its_window(self) -> None:
        self.session("recipient", "Recipient")
        options = local_options(["--to", "session:recipient", "--from", "Sender", "--key", "result",
                                 "--repeat-minutes", "30", "--text", "The unit is back."])
        started = send.now()
        self.outcome = send.Result("queued", "relay unavailable")
        with mock.patch.object(send, "now", return_value=started):
            self.assertEqual(send.send(options).outcome, "queued")
        self.outcome = send.Result("sent", "delivered")
        with mock.patch.object(send, "now", return_value=started + timedelta(seconds=1)):
            self.assertEqual(send.send(options).outcome, "skipped")
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(send.pending("session:recipient").count("The unit is back."), 1)
        self.assertEqual(send.pending("session:recipient"), "")

    def test_a_failed_send_does_not_clear_a_later_runs_window(self) -> None:
        started = send.now()
        options = local_options(["--to", "bogus", "--from", "Sender", "--key", "result",
                                 "--repeat-minutes", "30", "--text", "The unit is back."])
        def overlap(message: send.Message, timeout: float) -> send.Result:
            del message, timeout
            with (mock.patch.object(send, "now", return_value=started + timedelta(minutes=31)),
                  mock.patch.object(send, "relay", return_value=send.Result("sent", "later delivery"))):
                self.assertEqual(send.send(options).outcome, "sent")
            return send.Result("failed", "earlier delivery failed")
        with mock.patch.object(send, "now", return_value=started), mock.patch.object(send, "relay", overlap):
            self.assertEqual(send.send(options).outcome, "failed")
        with mock.patch.object(send, "now", return_value=started + timedelta(minutes=32)):
            self.assertEqual(send.send(options).outcome, "skipped")
        self.assertEqual([entry["outcome"] for entry in self.log()], ["sent", "failed", "skipped"])

    @staticmethod
    def emergency(*extra: str) -> list[str]:
        return ["--here", "--to", "user", "--from", "hana", "--summary", "GitHub credentials are cold", "--need", "blocked",
                "--clears-when", "true", "--action", "Run github-warmup on natedev", "--text", "The push waits.", *extra]

    def emergency_at(self, at: datetime, *extra: str) -> send.Result:
        with mock.patch.object(send, "now", return_value=at):
            return send.send(send.parse(self.emergency(*extra)))

    def test_a_repeat_of_an_emergency_push_is_skipped_while_the_first_is_live(self) -> None:
        sent = datetime(2026, 10, 27, 15, 26, 37, tzinfo=timezone.utc)
        zone = user_zone()
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as channel:
            self.assertEqual(self.emergency_at(sent, "--key", "github-cold").outcome, "sent")
            with mock.patch.object(send, "now", return_value=sent + timedelta(seconds=23)), \
                 redirect_stdout(io.StringIO()) as output:
                code = send.main(self.emergency("--key", "github-cold"))
        self.assertEqual(code, 0)
        self.assertEqual(channel.call_count, 1)
        self.assertEqual(output.getvalue(), (
            f"SKIPPED: an emergency push with key github-cold went to the user at {sent.astimezone(zone):%H:%M:%S %Z}"
            + f" and is still live until {(sent + timedelta(hours=3)).astimezone(zone):%H:%M:%S %Z}; to send a new one,"
            + " retire it first: send.py retire --to user --from hana --key github-cold\n"))

    def test_an_emergency_push_with_no_key_takes_one_from_its_sender_and_title(self) -> None:
        sent = send.now()
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as channel:
            self.assertEqual(self.emergency_at(sent).outcome, "sent")
            self.assertEqual(self.emergency_at(sent + timedelta(seconds=23)).outcome, "skipped")
        self.assertEqual(channel.call_count, 1)
        key = send.user_emergency_key("hana", "GitHub credentials are cold")
        self.assertRegex(key, r"^user-emergency:hana:[0-9a-f]{12}$")
        self.assertEqual([entry["key"] for entry in self.log()], [key, key])
        command = cast(list[str], channel.call_args.args[0])
        self.assertEqual(command[command.index("--key") + 1], key)

    def test_after_a_retire_or_the_push_lifetime_an_emergency_push_with_the_key_goes_out(self) -> None:
        sent = send.now()
        with (mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as channel,
              mock.patch.object(silence, "drop", return_value=0), redirect_stdout(io.StringIO())):
            self.assertEqual(self.emergency_at(sent, "--key", "github-cold").outcome, "sent")
            self.assertEqual(send.main(["retire", "--to", "user", "--from", "hana", "--key", "github-cold"]), 0)
            self.assertEqual(self.emergency_at(sent + timedelta(seconds=30), "--key", "github-cold").outcome, "sent")
            lifetime = send.emergency_lifetime()
            self.assertEqual(self.emergency_at(sent + lifetime, "--key", "github-cold").outcome, "skipped")
            self.assertEqual(self.emergency_at(sent + timedelta(seconds=30) + lifetime, "--key", "github-cold").outcome,
                             "sent")
        # Two pushes, the retire's cancel, and the push after the lifetime.
        self.assertEqual(channel.call_count, 4)

    def test_an_emergency_push_that_did_not_go_out_does_not_skip_its_resend(self) -> None:
        sent = send.now()
        with mock.patch.object(send, "run", return_value=(1, "", "pushover: could not reach Pushover\n")):
            self.assertEqual(self.emergency_at(sent, "--key", "github-cold").outcome, "failed")
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(self.emergency_at(sent + timedelta(seconds=23), "--key", "github-cold").outcome, "sent")

    def test_an_explicit_repeat_window_wins_over_the_emergency_lifetime(self) -> None:
        sent = send.now()
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(self.emergency_at(sent, "--key", "k", "--repeat-minutes", "1").outcome, "sent")
            inside = self.emergency_at(sent + timedelta(seconds=30), "--key", "k", "--repeat-minutes", "1")
            self.assertEqual(self.emergency_at(sent + timedelta(minutes=2), "--key", "k", "--repeat-minutes", "1").outcome,
                             "sent")
        self.assertEqual(inside, send.Result("skipped", f"key 'k' last sent to user at {sent.isoformat()}, inside its 1-minute window"))

    def test_a_note_to_the_user_with_one_key_has_no_window_without_repeat_minutes(self) -> None:
        note = ["--here", "--to", "user", "--from", "hana", "--summary", "Build done", "--key", "build", "--no-action",
                "--text", "The build passed."]
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as channel:
            self.assertEqual([send.send(send.parse(note)).outcome for _ in range(2)], ["sent", "sent"])
        self.assertEqual(channel.call_count, 2)

    def test_a_remote_emergency_push_holds_its_window_on_this_host_only(self) -> None:
        with mock.patch.object(send, "run", return_value=(0, "SENT: to the user\n", "")) as run:
            self.assertEqual(send.send(send.parse(self.emergency()[1:] + ["--machine", "mac"])).outcome, "sent")
        forwarded = shlex.split(cast(list[str], run.call_args.args[0])[-1])
        self.assertEqual(forwarded[forwarded.index("--repeat-minutes") + 1], "0")
        self.assertEqual(forwarded[forwarded.index("--key") + 1], send.user_emergency_key("hana", "GitHub credentials are cold"))
        # This host keeps the --clears-when record; the far side is told the push clears.
        self.assertIn("--self-clearing", forwarded)
        self.assertNotIn("--clears-when", forwarded)
        self.assertEqual([record["machine"] for record in self.emergencies()], ["mac"])

    # --- --clears-when and clear-check ---

    def emergencies(self) -> list[send.Emergency]:
        with send.locked():
            return send.read_emergencies()

    def keyed(self, key: str) -> bool:
        with send.key_states() as states:
            return key in states

    def clear_check(self, at: datetime | None = None, timeout: float = 0) -> tuple[int, list[str]]:
        with mock.patch.object(send, "now", return_value=at or send.now()):
            return send.clear_check(timeout or send.CHECK_TIMEOUT_S)

    def test_an_emergency_push_with_no_clear_step_is_refused(self) -> None:
        bare = [argument for argument in self.emergency("--key", "github-cold") if argument not in ("--clears-when", "true")]
        with (mock.patch.object(send, "run") as channel, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(send.main(bare), 2)
            self.assertEqual(send.main([*bare[:-2], "--priority", "2", *bare[-2:]]), 2)
        channel.assert_not_called()
        self.assertEqual(output.getvalue(), f"REFUSED: {send.NO_CLEAR_STEP}\n" * 2)
        self.assertIn('--clears-when "<command that exits 0 when fixed>"', send.NO_CLEAR_STEP)
        self.assertEqual(self.emergencies(), [])
        # A refusal claims nothing: the resend with a clear step goes out at once.
        self.assertFalse(self.keyed("github-cold"))
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(send.send(send.parse(self.emergency("--key", "github-cold"))).outcome, "sent")

    def test_clears_when_goes_only_with_a_blocked_push_to_the_user(self) -> None:
        note = ["--to", "user", "--from", "hana", "--summary", "Build done", "--no-action", "--text", "Done."]
        for arguments in ([*note, "--clears-when", "true"], [*note, "--self-clearing"],
                          [*self.emergency(), "--self-clearing"], [*self.emergency()[:-2], "--clears-when", " ", "--text", "x"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                _ = send.parse(arguments)
            self.assertEqual(raised.exception.code, 2)

    def test_a_sent_push_with_clears_when_is_recorded(self) -> None:
        sent = datetime(2026, 10, 27, 15, 26, 37, tzinfo=timezone.utc)
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(self.emergency_at(sent, "--key", "github-cold").outcome, "sent")
        self.assertEqual(self.emergencies(), [send.Emergency(
            key="github-cold", sender="hana", check="true", machine=send.machine(), sent=sent.isoformat(),
            expires=(sent + send.emergency_lifetime()).isoformat())])
        # A push that does not go out is not recorded.
        with mock.patch.object(send, "run", return_value=(1, "", "pushover: could not reach Pushover\n")):
            self.assertEqual(self.emergency_at(sent, "--key", "other").outcome, "failed")
        self.assertEqual([record["key"] for record in self.emergencies()], ["github-cold"])

    def record(self, check: str, key: str = "github-cold", host: str = "", sent: datetime | None = None) -> None:
        at = sent or send.now()
        send.record_emergency(send.Emergency(key=key, sender="hana", check=check, machine=host or send.machine(),
                                             sent=at.isoformat(), expires=(at + send.emergency_lifetime()).isoformat()))

    def test_clear_check_retires_a_push_whose_check_passes(self) -> None:
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(send.send(send.parse(self.emergency("--key", "github-cold"))).outcome, "sent")
        self.assertTrue(self.keyed("github-cold"))
        with (mock.patch.object(send, "run", return_value=(0, "pushover: cancelled 1\n", "")) as channel,
              mock.patch.object(silence, "drop", return_value=0)):
            code, lines = self.clear_check()
        self.assertEqual(code, 0)
        self.assertEqual(cast(list[str], channel.call_args.args[0]),
                         [sys.executable, str(send.USER_CHANNEL), "cancel", "--source", "hana", "github-cold"])
        self.assertEqual(lines, ["retired: github-cold from hana: its check passed;"
                                 + " cancelled: the emergency push for github-cold stopped"])
        self.assertEqual(self.emergencies(), [])
        self.assertFalse((self.root / send.EMERGENCIES).exists())
        self.assertFalse(self.keyed("github-cold"))

    def test_clear_check_keeps_a_push_whose_check_fails(self) -> None:
        self.record("false")
        with mock.patch.object(send, "run") as channel:
            self.assertEqual(self.clear_check(), (0, ["kept: github-cold from hana: its check exits 1"]))
        channel.assert_not_called()
        self.assertEqual([record["check"] for record in self.emergencies()], ["false"])

    def test_clear_check_drops_an_expired_record_without_running_its_check(self) -> None:
        sent = datetime(2026, 10, 27, 15, 26, 37, tzinfo=timezone.utc)
        marker = self.root / "ran"
        self.record(f"touch {shlex.quote(str(marker))}", sent=sent)
        expires = sent + send.emergency_lifetime()
        with mock.patch.object(send, "run") as channel:
            code, lines = self.clear_check(expires)
        self.assertEqual((code, lines), (0, [f"dropped: github-cold from hana: its push expired at"
                                             + f" {expires.astimezone(user_zone()):%H:%M:%S %Z}"]))
        channel.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(self.emergencies(), [])

    def test_clear_check_stops_a_check_that_hangs_and_keeps_its_push(self) -> None:
        self.record("sleep 30")
        started = time.monotonic()
        with mock.patch.object(send, "run") as channel:
            self.assertEqual(self.clear_check(timeout=0.2),
                             (0, ["kept: github-cold from hana: its check ran past 0.2 s and was stopped"]))
        self.assertLess(time.monotonic() - started, 10)
        channel.assert_not_called()
        self.assertEqual(len(self.emergencies()), 1)

    def test_clear_check_with_no_records_does_nothing(self) -> None:
        with mock.patch.object(send, "run") as channel, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(send.main(["clear-check"]), 0)
            _ = (self.root / send.EMERGENCIES).write_text("[]\n", encoding="utf-8")
            self.assertEqual(send.main(["clear-check"]), 0)
        channel.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        # The empty file goes, so the notifier tick stops calling clear-check.
        self.assertFalse((self.root / send.EMERGENCIES).exists())

    def test_clear_check_retires_a_push_posted_on_another_host_there(self) -> None:
        with mock.patch.object(send, "run", return_value=(0, "SENT: to the user\n", "")):
            self.assertEqual(send.send(send.parse(self.emergency("--key", "github-cold")[1:] + ["--machine", "natedev"]))
                             .outcome, "sent")
        far = "retired 0 queued messages\ndropped 0 kept pushes\ncancelled: the emergency push for github-cold stopped\n"
        with mock.patch.object(send, "run", return_value=(0, far, "")) as channel, \
             mock.patch.object(send, "machine", return_value="macbook"):
            code, lines = self.clear_check()
        command = cast(list[str], channel.call_args.args[0])
        self.assertEqual(command[:-1], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "natedev"])
        self.assertEqual(command[-1], f"{send.REMOTE} retire --to user --from hana --key github-cold")
        self.assertEqual((code, lines), (0, ["retired: github-cold from hana: its check passed; on natedev:"
                                             + " cancelled: the emergency push for github-cold stopped"]))
        self.assertEqual(self.emergencies(), [])
        self.assertFalse(self.keyed("github-cold"))

    def test_a_failed_retire_keeps_the_record_and_exits_3(self) -> None:
        self.record("true")
        with (mock.patch.object(send, "run", return_value=(1, "", "pushover: could not reach Pushover\n")),
              mock.patch.object(silence, "drop", return_value=0), redirect_stdout(io.StringIO()) as output):
            self.assertEqual(send.main(["clear-check"]), 3)
        self.assertEqual(output.getvalue(), "FAILED: github-cold from hana: its check passed, but the emergency push for"
                         + " github-cold may still repeat: pushover: could not reach Pushover\n")
        self.assertEqual(len(self.emergencies()), 1)

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

    def test_ack_cli_persists_acknowledgement_without_loading_session_discovery(self) -> None:
        program = r'''import importlib.abc, runpy, sys
class NoDiscovery(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "roster":
            raise AssertionError("ack must not start session discovery")
sys.meta_path.insert(0, NoDiscovery())
sys.argv.pop(0)
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        state = self.root / "cli-state"
        result = subprocess.run([sys.executable, "-c", program, str(Path(send.__file__)), "ack", "notice"],
                                env={**os.environ, "XDG_STATE_HOME": str(state)},
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("notice", result.stdout)
        with mock.patch.object(send, "STATE", state / "message"):
            with send.key_states() as saved:
                self.assertIn("acknowledged", saved["notice"])

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

    def relay_record(self, pid: int, name: str, *, stale: bool = False) -> str:
        directory = self.root / "relay_senders"
        directory.mkdir(exist_ok=True)
        _ = (directory / str(pid)).write_text(name)
        path = self.root / "cc-socks" / f"{pid}.sock"
        if stale:
            path.parent.mkdir(exist_ok=True)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
        return f"uds:{path}"

    def socket_queue(self, address: str, *entries: tuple[datetime, str], name: str = "") -> Path:
        path = send.queue_path(address)
        path.parent.mkdir(exist_ok=True)
        values = [{"time": at.isoformat(), "from": "survey", "to": address, "key": None,
                   "summary": "reply", "text": text, "reason": "gone", "to_name": name}
                  for at, text in entries]
        _ = path.write_text("".join(json.dumps(value) + "\n" for value in values))
        return path

    def test_a_dead_relay_socket_delivers_to_its_named_live_session(self) -> None:
        self.session("verify-session", "verify")
        address = self.relay_record(42, "verify")
        result = send.send(local_options(["--to", address, "--from", "survey", "--text", "reply"]))
        self.assertEqual(result.outcome, "sent")
        self.assertEqual([(message.to, message.text) for message in self.relayed],
                         [(f"uds:{self.root / 'verify-session.sock'}", "reply")])
        self.assertFalse((self.root / "queue").exists())

    def test_a_dead_socket_without_a_live_named_recipient_fails_loudly_and_never_queues(self) -> None:
        address = f"uds:{self.root / 'cc-socks' / '42.sock'}"
        for name in ("", "verify"):
            with self.subTest(name=name):
                if name:
                    _ = self.relay_record(42, name)
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    code = send.main(["--here", "--to", address, "--from", "survey", "--text", "reply"])
                self.assertEqual(code, 3)
                self.assertIn("FAILED:", output.getvalue())
                self.assertIn(address, errors.getvalue())
                self.assertIn(name or "(unknown)", errors.getvalue())
                self.assertFalse((self.root / "queue").exists())
        self.assertEqual(self.relayed, [])

    def test_a_refused_relay_socket_reaches_the_named_session_for_sends_and_drains(self) -> None:
        self.session("verify-session", "verify")
        address = self.relay_record(42, "verify", stale=True)
        attempts: list[str] = []
        def relay(message: send.Message, timeout: float) -> send.Result:
            attempts.append(message.to)
            return send.Result("queued", "ECONNREFUSED") if message.to == address else self.relay(message, timeout)
        with mock.patch.object(send, "relay", relay):
            result = send.send(local_options(["--to", address, "--from", "survey", "--text", "reply"]))
            self.assertEqual(result.outcome, "sent")
            path = self.socket_queue(address, (send.now(), "stuck reply"))
            self.assertEqual(self.drain()[0], 0)
        destination = f"uds:{self.root / 'verify-session.sock'}"
        self.assertEqual(attempts, [address, destination, address, destination])
        self.assertEqual([message.to for message in self.relayed], [destination, destination])
        self.assertFalse(path.exists())

    def test_a_refused_socket_without_a_live_named_session_fails_loudly(self) -> None:
        address = self.relay_record(42, "verify", stale=True)
        self.outcome = send.Result("queued", "ECONNREFUSED")
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = send.main(["--here", "--to", address, "--from", "survey", "--text", "reply"])
        self.assertEqual(code, 3)
        self.assertIn("FAILED:", output.getvalue())
        self.assertIn(address, errors.getvalue())
        self.assertIn("verify", errors.getvalue())
        self.assertIn("ECONNREFUSED", errors.getvalue())
        self.assertFalse((self.root / "queue").exists())
        self.assertEqual(len(self.relayed), 1)

    def test_a_recipient_that_exits_after_the_queue_decision_keeps_its_session_key(self) -> None:
        self.session("recipient", "Recipient")
        address = f"uds:{self.root / 'recipient.sock'}"
        self.outcome = send.Result("queued", "relay timed out")
        enqueue = send.enqueue
        def leave(message: send.Message, reason: str, at: datetime) -> None:
            self.assertEqual(message.to, "session:recipient")
            (self.root / "sessions/recipient.json").unlink()
            enqueue(message, reason, at)
        with mock.patch.object(send, "enqueue", leave):
            result = send.send(local_options(["--to", address, "--from", "survey", "--text", "reply"]))
        self.assertEqual(result.outcome, "queued")
        self.assertEqual([path.name for path in (self.root / "queue").iterdir()], ["session-recipient.jsonl"])
        self.assertEqual(send.read_queue(send.queue_path("session-recipient"))[0]["text"], "reply")

    def test_enqueue_rejects_an_unregistered_raw_socket_at_the_queue_boundary(self) -> None:
        address = self.relay_record(42, "verify")
        with self.assertRaisesRegex(ValueError, "dead socket.*verify.*message not queued"):
            send.enqueue(send.Message(address, "survey", "reply", "reply", None), "failed", send.now())
        self.assertFalse((self.root / "queue").exists())

    def test_a_socket_that_dies_during_relay_fails_instead_of_creating_a_raw_socket_queue(self) -> None:
        self.session("recipient", "Recipient")
        address = f"uds:{self.root / 'recipient.sock'}"
        def relay(message: send.Message, timeout: float) -> send.Result:
            del message, timeout
            (self.root / "sessions/recipient.json").unlink()
            return send.Result("queued", "ENOENT")
        with mock.patch.object(send, "relay", relay):
            result = send.send(local_options(["--to", address, "--from", "survey", "--text", "reply"]))
        self.assertEqual(result.outcome, "failed")
        self.assertIn("ENOENT", result.detail)
        self.assertFalse((self.root / "queue").exists())

    def test_socket_drain_delivers_young_replies_with_their_original_times(self) -> None:
        self.session("verify-session", "verify")
        at = send.now() - timedelta(hours=1)
        known = self.socket_queue(self.relay_record(42, "verify"), (at, "record reply"))
        legacy = self.socket_queue(f"uds:{self.root / 'cc-socks/43.sock'}", (at, "legacy reply"), name="verify")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(send.main(["drain-sockets"]), 0)
        self.assertEqual([message.text for message in self.relayed],
            [f"Stuck message from {at.isoformat()}:\nrecord reply", f"Stuck message from {at.isoformat()}:\nlegacy reply"])
        self.assertTrue(all(message.to == f"uds:{self.root / 'verify-session.sock'}" for message in self.relayed))
        self.assertEqual(output.getvalue().count("SENT: stuck reply"), 2)
        self.assertFalse(known.exists())
        self.assertFalse(legacy.exists())
        self.assertEqual(self.drain(), (0, []))
        self.assertEqual(len(self.relayed), 2)

    def test_socket_drain_archives_old_files_and_reports_their_count_and_date_range(self) -> None:
        at = send.now()
        oldest, newest = at - timedelta(days=3), at - timedelta(hours=24)
        first = self.socket_queue("uds:/fixture/cc-socks/42.sock", (oldest, "old reply"))
        second = self.socket_queue("uds:/fixture/cc-socks/43.sock", (newest, "another old reply"))
        originals = {path.name: path.read_bytes() for path in (first, second)}
        with mock.patch.object(send, "now", return_value=at):
            code, lines = self.drain()
        self.assertEqual(code, 0)
        self.assertEqual(lines, [f"archiving 2 socket queue files: oldest {oldest.isoformat()}, newest {newest.isoformat()}"])
        self.assertEqual({path.name: path.read_bytes() for path in (self.root / "socket-queue-archive").iterdir()}, originals)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(self.drain(), (0, []))

    def test_an_interrupted_socket_drain_retries_without_duplicate_or_lost_messages(self) -> None:
        self.session("verify-session", "verify")
        at = send.now()
        path = self.socket_queue(self.relay_record(42, "verify"), (at, "reply one"), (at, "reply two"))
        with interrupt_replacement(path, skip=1), self.assertRaises(KeyboardInterrupt):
            _ = self.drain()
        self.assertTrue(path.exists())
        self.assertEqual(len(send.read_queue(path)), 2)
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.drain()[0], 0)
        self.assertEqual([message.text for message in self.relayed],
                         [f"Stuck message from {at.isoformat()}:\nreply one", f"Stuck message from {at.isoformat()}:\nreply two"])
        self.assertFalse(path.exists())
        self.assertEqual(self.drain(), (0, []))
        self.assertEqual(len(self.relayed), 2)

    def test_a_socket_reply_stopped_before_its_receipt_is_archived_without_resending(self) -> None:
        self.session("verify-session", "verify")
        at = send.now()
        path = self.socket_queue(self.relay_record(42, "verify"), (at, "reply"))
        with mock.patch.object(send, "log", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            _ = self.drain()
        self.assertEqual(len(self.relayed), 1)
        self.assertFalse((self.root / "log.jsonl").exists())
        attempts = (self.root / "socket-recovery-attempts.jsonl").read_text().splitlines()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(send.as_dict(send.loads(attempts[0]))["key"], send.read_queue(path)[0].get("socket_recovery"))
        code, lines = self.drain()
        self.assertEqual(code, 3)
        self.assertEqual(lines, [f"UNCONFIRMED: stuck reply from {at.isoformat()} to verify may have arrived;"
                                 + " archived, not resent"])
        archived = list((self.root / "socket-queue-archive").glob("*.jsonl"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(send.read_queue(archived[0])[0]["text"], "reply")
        self.assertFalse(path.exists())
        self.assertEqual(self.drain(), (0, []))
        self.assertEqual(len(self.relayed), 1)

    def test_an_interrupted_uncertain_archive_checkpoint_keeps_other_replies_without_copying_the_archive(self) -> None:
        self.session("verify-session", "verify")
        at = send.now()
        path = self.socket_queue(self.relay_record(42, "verify"), (at, "uncertain"), (at, "not attempted"))
        with mock.patch.object(send, "log", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            _ = self.drain()
        with interrupt_replacement(path), self.assertRaises(KeyboardInterrupt):
            _ = self.drain()
        self.assertEqual(len(send.read_queue(path)), 2)
        self.assertEqual(self.drain()[0], 3)
        self.assertEqual([message.text for message in self.relayed],
                         [f"Stuck message from {at.isoformat()}:\nuncertain",
                          f"Stuck message from {at.isoformat()}:\nnot attempted"])
        archived = list((self.root / "socket-queue-archive").glob("*.jsonl"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(send.read_queue(archived[0])[0]["text"], "uncertain")
        self.assertFalse(path.exists())
        self.assertEqual(self.drain(), (0, []))

    def test_an_unconfirmed_reply_is_reported_before_its_queue_checkpoint_completes(self) -> None:
        self.session("verify-session", "verify")
        at = send.now()
        path = self.socket_queue(self.relay_record(42, "verify"), (at, "uncertain"), (at, "next reply"))
        with mock.patch.object(send, "log", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            _ = self.drain()
        output = io.StringIO()
        with redirect_stdout(output), interrupt_replacement(path, after=True), self.assertRaises(KeyboardInterrupt):
            _ = send.main(["drain-sockets"])
        self.assertEqual(output.getvalue(), f"UNCONFIRMED: stuck reply from {at.isoformat()} to verify may have arrived;"
                         + " archived, not resent\n")
        self.assertEqual([entry["text"] for entry in send.read_queue(path)], ["next reply"])
        self.assertEqual(len(list((self.root / "socket-queue-archive").glob("*.jsonl"))), 1)
        self.assertEqual(len(self.relayed), 1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(send.main(["drain-sockets"]), 0)
        self.assertEqual(output.getvalue(), f"SENT: stuck reply from {at.isoformat()} to verify\n")
        self.assertEqual([message.text for message in self.relayed],
                         [f"Stuck message from {at.isoformat()}:\nuncertain",
                          f"Stuck message from {at.isoformat()}:\nnext reply"])
        self.assertEqual(self.drain(), (0, []))

    def test_a_sent_reply_is_reported_before_its_queue_checkpoint_completes(self) -> None:
        self.session("verify-session", "verify")
        at = send.now()
        path = self.socket_queue(self.relay_record(42, "verify"), (at, "reply one"), (at, "reply two"))
        output = io.StringIO()
        with redirect_stdout(output), interrupt_replacement(path, after=True, skip=1), \
                self.assertRaises(KeyboardInterrupt):
            _ = send.main(["drain-sockets"])
        self.assertEqual(output.getvalue(), f"SENT: stuck reply from {at.isoformat()} to verify\n")
        self.assertEqual([entry["text"] for entry in send.read_queue(path)], ["reply two"])
        self.assertEqual(self.drain(), (0, [f"SENT: stuck reply from {at.isoformat()} to verify"]))
        self.assertEqual(len(self.relayed), 2)
        self.assertEqual(self.drain(), (0, []))

    def test_an_archive_summary_is_reported_before_any_old_file_is_moved(self) -> None:
        at = send.now()
        oldest, newest = at - timedelta(days=3), at - timedelta(hours=24)
        first = self.socket_queue("uds:/fixture/cc-socks/42.sock", (oldest, "old reply"))
        second = self.socket_queue("uds:/fixture/cc-socks/43.sock", (newest, "next old reply"))
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(send, "now", return_value=at), \
                interrupt_replacement(self.root / "socket-queue-archive" / first.name, after=True), \
                self.assertRaises(KeyboardInterrupt):
            _ = send.main(["drain-sockets"])
        self.assertEqual(output.getvalue(), f"archiving 2 socket queue files: oldest {oldest.isoformat()},"
                         + f" newest {newest.isoformat()}\n")
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        with mock.patch.object(send, "now", return_value=at):
            self.assertEqual(self.drain(), (0, [f"archiving 1 socket queue files: oldest {newest.isoformat()},"
                                               + f" newest {newest.isoformat()}"]))
        self.assertEqual(len(list((self.root / "socket-queue-archive").iterdir())), 2)
        self.assertEqual(self.drain(), (0, []))
        self.assertEqual(self.relayed, [])

    def test_a_socket_recovery_attempt_and_receipt_count_as_one_message_result(self) -> None:
        self.session("verify-session", "verify")
        _ = self.socket_queue(self.relay_record(42, "verify"), (send.now(), "reply"))
        self.assertEqual(self.drain()[0], 0)
        attempts = (self.root / "socket-recovery-attempts.jsonl").read_text().splitlines()
        self.assertEqual(len(attempts), 1)
        identity = send.as_dict(send.loads(attempts[0]))["key"]
        self.assertEqual([(entry["key"], entry["outcome"]) for entry in self.log()], [(identity, "sent")])
        self.assertEqual(len(self.relayed), 1)

    def test_an_unfinished_retry_is_uncertain_even_when_its_time_matches_the_failed_attempt(self) -> None:
        self.session("verify-session", "verify")
        at = send.now()
        path = self.socket_queue(self.relay_record(42, "verify"), (at, "reply"))
        with mock.patch.object(send, "now", return_value=at):
            self.outcome = send.Result("queued", "relay unavailable")
            self.assertEqual(self.drain()[0], 3)
            self.outcome = send.Result("sent", "ok")
            with mock.patch.object(send, "log", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                _ = self.drain()
        code, lines = self.drain()
        self.assertEqual(code, 3)
        self.assertEqual(lines, [f"UNCONFIRMED: stuck reply from {at.isoformat()} to verify may have arrived;"
                                 + " archived, not resent"])
        self.assertFalse(path.exists())
        self.assertEqual(len(self.relayed), 2)
        self.assertEqual([entry["outcome"] for entry in self.log()], ["queued"])
        self.assertEqual(self.drain(), (0, []))

    def test_an_interrupted_attempt_write_does_not_start_delivery(self) -> None:
        self.session("verify-session", "verify")
        path = self.socket_queue(self.relay_record(42, "verify"), (send.now(), "reply"))
        with mock.patch.object(os, "fsync", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            _ = self.drain()
        self.assertEqual(self.relayed, [])
        self.assertEqual(self.drain()[0], 3)
        self.assertFalse(path.exists())
        self.assertEqual(self.relayed, [])

    def test_an_unreadable_socket_queue_does_not_stop_the_next_files_delivery(self) -> None:
        self.session("verify-session", "verify")
        first = self.socket_queue(self.relay_record(42, "verify"), (send.now(), "unreadable"))
        _ = first.write_bytes(b"\xff\xfe\n")
        second = self.socket_queue(self.relay_record(43, "verify"), (send.now(), "deliverable"))
        code, lines = self.drain()
        self.assertEqual(code, 3)
        self.assertTrue(any(line.startswith(f"unresolved socket queue {first}: unreadable (")
                            and line.endswith("); left in place") for line in lines))
        self.assertEqual(first.read_bytes(), b"\xff\xfe\n")
        self.assertFalse(second.exists())
        self.assertEqual(len(self.relayed), 1)
        self.assertTrue(self.relayed[0].text.endswith("\ndeliverable"))

    def test_socket_drain_leaves_unresolved_files_in_place_and_names_each_file(self) -> None:
        at = send.now()
        paths = [self.socket_queue(self.relay_record(42, "verify"), (at, "known but offline")),
                 self.socket_queue("uds:/fixture/cc-socks/43.sock", (at, "unknown"))]
        before = [path.read_bytes() for path in paths]
        for _ in range(2):
            code, lines = self.drain()
            self.assertEqual(code, 3)
            for path in paths:
                self.assertTrue(any(str(path) in line for line in lines))
            self.assertEqual([path.read_bytes() for path in paths], before)

    def test_socket_drain_retries_a_failed_delivery_without_an_extra_queued_copy(self) -> None:
        self.session("verify-session", "verify")
        path = self.socket_queue(self.relay_record(42, "verify"), (send.now(), "reply"))
        self.outcome = send.Result("queued", "relay unavailable")
        code, lines = self.drain()
        self.assertEqual(code, 3)
        self.assertIn("relay unavailable", lines[0])
        self.assertTrue(path.exists())
        self.assertEqual(list((self.root / "queue").iterdir()), [path])
        self.assertEqual([entry["outcome"] for entry in self.log()], ["queued"])
        self.outcome = send.Result("sent", "ok")
        self.assertEqual(self.drain()[0], 0)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.relayed), 2)
        self.assertEqual([entry["outcome"] for entry in self.log()], ["queued", "sent"])
        self.assertEqual(len((self.root / "socket-recovery-attempts.jsonl").read_text().splitlines()), 2)
        self.assertEqual(self.drain(), (0, []))
        self.assertEqual(len(self.relayed), 2)

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
            "--text", "restart_support first",
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
            "--text", "restart_support first",
        ]))
        self.assertEqual(queued.outcome, "queued")

        self.session("id-1", "recipient")
        recipient = roster.RosterEntry(
            "machine", "claude", "freestanding", "recipient", "running",
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
            ("session:id-1", "shutdown", key, "keyed restart_support"),
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
        self.assertNotIn("restart_support", same_address)
        self.assertNotIn("cancel", same_address)
        self.assertIn("keep same address", same_address)
        self.assertIn("keep other address", send.pending("two"))

    def test_retire_to_user_cancels_the_emergency_push_for_the_key(self) -> None:
        cases = (((0, "pushover: cancelled 1\n", ""), 0, "cancelled: the emergency push for prompt-1 stopped"),
                 ((0, "pushover: nothing to cancel\n", ""), 0,
                  "nothing to cancel: no emergency push for prompt-1 is repeating"),
                 ((1, "", "pushover: could not reach Pushover: timed out\n"), 3,
                  "FAILED: the emergency push for prompt-1 may still repeat: pushover: could not reach Pushover: timed out"))
        # A push kept during silence with the key goes too, so it never goes out when silence ends.
        kept = {"source": "stall_watch", "priority": "2", "title": "t", "message": "m", "detail": "", "key": "prompt-1"}
        (self.root / "notify").mkdir()
        _ = (self.root / "notify/silence-kept.jsonl").write_text(json.dumps(kept) + "\n"
                                                                 + json.dumps(kept | {"key": "prompt-2"}) + "\n")
        for (response, expected, line), dropped in zip(cases, ("1 kept push", "0 kept pushes", "0 kept pushes")):
            with (self.subTest(line=line), mock.patch.object(send, "run", return_value=response) as run,
                  redirect_stdout(io.StringIO()) as output):
                code = send.main(["retire", "--to", "user", "--from", "stall_watch", "--key", "prompt-1"])
            self.assertEqual(code, expected)
            self.assertEqual(output.getvalue(), f"retired 0 queued messages\ndropped {dropped}\n{line}\n")
            self.assertEqual(cast(list[str], run.call_args.args[0])[1:],
                             [str(send.USER_CHANNEL), "cancel", "--source", "stall_watch", "prompt-1"])
        self.assertIn('"key": "prompt-2"', (self.root / "notify/silence-kept.jsonl").read_text())

    def test_registry_argv_key_reaches_the_user_send(self) -> None:
        rendered = push_texts.render("nixos.ups_shutdown")
        options = send.parse(push_texts.user_send_argv(rendered, key="ups-shutdown")[2:])
        self.assertEqual((options.key, options.need), ("ups-shutdown", "blocked"))

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
        # The subprocess needs its own roster fixture; parent patches do not cross exec.
        real_send = self.root / "real-send.py"
        _ = real_send.write_text(
            f"#!{sys.executable}\n"
            + "import sys\n"
            + f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            + "import roster, send\n"
            + "from scripts.lib import names\nfrom scripts.lib.test_names import fixture_names\n"
            + "names.load_names = lambda: fixture_names()\n"
            + "roster.roster = lambda environment, scope: roster.Roster([], [])\n"
            + "roster.peer_roster = lambda environment: roster.Roster([], [])\n"
            + "raise SystemExit(send.main(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        real_send.chmod(0o755)
        child_state = self.root / "child-state"

        def shutdown_fixture(session_id: str) -> shutdown_record.SessionShutdown:
            from test_restart import entry, top_level

            saved = entry(top_level(session_id), state=shutdown_record.SessionWaiting(kind="waiting"))
            self.assertTrue(shutdown_record.create_session(saved))
            return saved

        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        self.allow_leaf(ShutdownSendLeaf.RETIRE_SHUTDOWN_MESSAGES)
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)

        with (
            mock.patch.object(send, "STATE", child_state / "message"),
            mock.patch.dict(
                os.environ,
                {
                    "XDG_STATE_HOME": str(child_state),
                    "SHUTDOWN_SEND": str(real_send),
                    "SHUTDOWN_STATE_DIR": str(child_state / "shutdown"),
                },
            ),
        ):
            restarted = shutdown_fixture("restarted")
            old_restart = restart_support.send_message(
                "session:restarted",
                "old shutdown",
                "Old unkeyed shutdown instruction.",
            )
            self.assertEqual(old_restart["kind"], "queued")
            restart._retire(restarted, dry_run=False)  # pyright: ignore[reportPrivateUsage]
            delivery = restart._send_restart_note(  # pyright: ignore[reportPrivateUsage]
                restarted,
                "Restarted; continue.",
                dry_run=False,
            )
            self.assertEqual(delivery["kind"], "queued")
            self.session("restarted", "restart-recipient")
            restart_pending = send.pending("restart-recipient")
            self.assertNotIn("shutdown instruction", restart_pending)
            self.assertIn("Restarted; continue.", restart_pending)

    def test_unique_claude_roster_name_delivers_to_its_session_address(self) -> None:
        recipient = self.claude_row("id-1", "recipient")

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

    def test_a_claude_name_with_different_case_routes_to_its_session_address(self) -> None:
        recipient = self.claude_row("id-1", "morty")
        with mock.patch.object(roster, "roster", return_value=roster.Roster([recipient], [])):
            output = io.StringIO()
            with redirect_stdout(output):
                code = send.main(["--here", "--to", "Morty", "--from", "test", "--text", "hello by name"])
        self.assertEqual(code, 0)
        self.assertTrue(output.getvalue().startswith("SENT:"), output.getvalue())
        self.assertEqual([message.to for message in self.relayed], [f"uds:{self.root / 'id-1.sock'}"])
        self.assertEqual(self.log()[0]["to"], "session:id-1")
        self.assertFalse(send.queue_path("Morty").exists())

    def test_same_worker_name_in_two_runs_resolves_by_address_and_is_ambiguous_by_name(
        self,
    ) -> None:
        first_address = "worker:11111111-1111-1111-1111-111111111111/shared"
        second_address = "worker:22222222-2222-2222-2222-222222222222/shared"
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
            roster.CannotReceiveMessages("worker between turns; no unit director is running"), "/work",
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
            "send.py: paused worker cannot take messages (worker between turns; no unit director is running)\n",
        )
        codex_send.assert_not_called()
        claude_send.assert_not_called()
        daemon_send.assert_not_called()
        enqueue.assert_not_called()

    def test_ambiguous_name_describes_a_recipient_that_cannot_take_messages(self) -> None:
        entries = [
            roster.RosterEntry(
                "machine", "claude", "freestanding", "duplicate", "running",
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
        self.assertIn("  session:messageable (freestanding)\n", errors.getvalue())
        self.assertIn(
            "  (cannot take messages: blind reviewer) (worker)\n",
            errors.getvalue(),
        )

    def test_roster_problem_is_printed_while_a_usable_worker_still_succeeds(self) -> None:
        address = "worker:33333333-3333-3333-3333-333333333333/worker"
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

    def test_unique_worker_name_resolves_to_its_run(self) -> None:
        address = "worker:55555555-5555-5555-5555-555555555555/only-worker"
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

    def test_unreadable_roster_keeps_bare_claude_fallback_but_worker_address_fails(
        self,
    ) -> None:
        unavailable = roster.Roster([], ["cannot read the worker root"])
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
            "send.py: roster: cannot read the worker root\n",
        )

        worker_errors = io.StringIO()
        with (
            mock.patch.object(roster, "roster", return_value=unavailable),
            mock.patch.object(send, "codex") as codex_send,
            self.assertRaises(SystemExit) as raised,
            redirect_stderr(worker_errors),
        ):
            _ = send.main(["--here",
                "--to", "worker:missing/worker", "--from", "test", "--text", "hello",
            ])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("send.py: roster: cannot read the worker root", worker_errors.getvalue())
        self.assertIn("worker:missing/worker", worker_errors.getvalue())
        codex_send.assert_not_called()

    def test_codex_thread_address_queues_after_one_local_roster_read(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(roster, "roster", return_value=roster.Roster([self.daemon_row("thread-one")], [])) as read_roster,
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
        queue.assert_called_once_with(self.daemon_owner(), "thread-one", "hello", timeout=send.TIMEOUT)
        read_roster.assert_called_once()

    def test_named_codex_session_uses_its_display_name_in_the_outcome(self) -> None:
        session = self.daemon_row("thread-window", "Codex window")
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
        queue.assert_called_once_with(self.daemon_owner(), "thread-window", "hello", timeout=send.TIMEOUT)

    def test_codex_queue_failure_and_timeout_are_failed_and_never_kept(self) -> None:
        failures = (
            (codex_daemon.QueueFailed("thread is closed"), "thread is closed"),
            (codex_daemon.QueueTimedOut(0.5), "timed out after 0.5 s"),
        )
        for queue_result, detail in failures:
            output = io.StringIO()
            with self.subTest(queue_result=queue_result), mock.patch.object(
                codex_daemon, "queue", return_value=queue_result,
            ), mock.patch.object(roster, "roster", return_value=roster.Roster([self.daemon_row("thread-one")], [])), redirect_stdout(output):
                code = send.main(["--here",
                    "--to", "codex:thread-one", "--from", "test", "--text", "hello",
                ])
            self.assertEqual(code, 3)
            self.assertEqual(output.getvalue(), f"FAILED: {detail}\n")
        self.assertEqual(send.pending("codex:thread-one"), "")

    def test_local_roster_is_read_once_for_every_roster_address_and_bare_name(self) -> None:
        address = "worker:44444444-4444-4444-4444-444444444444/worker"
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
            (f"uds:{self.root / 'claude.sock'}", 1),
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
                        send, "codex", return_value=send.Result("sent", "worker"),
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

    def test_worker_address_with_action_is_a_usage_error(self) -> None:
        errors = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stderr(errors):
            _ = local_options([
                "--to", "worker:run/worker", "--action", "reply", "--text", "hello",
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
            "worker:<run id>/<worker name>", "codex:<thread id>", "user",
        ):
            self.assertGreaterEqual(help_text.count(recipient_form), 2)

    def test_codex_failure_is_failed_not_queued(self) -> None:
        with mock.patch.object(send, "run", return_value=(1, "", "codex_mesh: no worker named 'bogus'")):
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

        told = ["--to", "user", "--from", "test", "--summary", "natedev: disk", "--need", "blocked", "--self-clearing",
                "--action", "free disk space", "--text", "full"]
        with mock.patch.object(send, "run", channel):
            self.assertEqual(send.send(local_options(told)), send.Result("sent", "to the user"))
            # Another title is another event: the first emergency push does not skip it.
            told[told.index("natedev: disk")] = "natedev: memory"
            self.assertEqual(send.send(local_options(told)).outcome, "failed")
        self.assertEqual(commands[0][2:], ["--priority", "2", "--source", "test",
                                           "--key", send.user_emergency_key("test", "natedev: disk"),
                                           "--action", "free disk space", "natedev: disk", "full"])
        self.assertEqual(self.relayed, [])
        self.assertEqual(send.pending("user"), "")
        self.assertEqual([entry["to"] for entry in self.log()], ["user", "user"])

    def test_registry_argv_preserves_values_and_blocked_need_across_remote_rendering(self) -> None:
        rendered = push_texts.RenderedPush("test", "script", "note", "Build stopped", ActionRequired("Choose retry."), "The build stopped.", {"name": "builder=one two"})
        with mock.patch.object(push_texts, "render", return_value=rendered):
            options = send.parse(push_texts.user_send_argv(rendered, machine="mac", priority="2")[2:])
            self.assertEqual(options.need, "blocked")
            message = send.Message("user", "script", rendered.title, rendered.body, None)
            with mock.patch.object(send, "run", return_value=(0, "SENT: to the user", "")) as run:
                self.assertEqual(send.remote(message, "mac", options).outcome, "sent")
            command = cast(list[str], run.call_args.args[0])[-1]
            forwarded = shlex.split(command)[3:]
            remote_options = send.parse(forwarded)
        self.assertEqual(remote_options.need, "blocked")
        self.assertEqual(remote_options.text_id, "test")
        self.assertEqual(remote_options.values, ("name=builder=one two",))
        self.assertEqual(remote_options.text, rendered.body)

    def test_review_rejection_keeps_held_provenance_locally_and_after_remote_stdout_loss(self) -> None:
        record = {"title": "Build choice", "action": "Choose retry.", "body": "The build stopped.", "held": True, "reviewed": True, "need": "decision"}
        for remote in (False, True):
            _ = (self.root / "sessions/900.json").write_text(json.dumps({"sessionId": "held-test"}))
            arguments = ["--to", "user", "--from", "test", "--summary", "Build choice", "--need", "decision", "--action", "Choose retry.", "--text", "The build stopped."]
            if remote:
                arguments.extend(["--machine", "natedev"])
            response = (6, "", "send.py: failed push JSON: " + json.dumps(record)) if remote else (6, "pushover: reviewed JSON: " + json.dumps(record), "pushover: not sent: reader-starts-cold")
            with (mock.patch.dict(os.environ, {"CLAUDE_PID": "900"}), mock.patch.object(send, "run", return_value=response)):
                result = send.send(local_options(arguments))
            self.assertEqual(result.outcome, "held")
            marker = cast(dict[str, object], json.loads((self.root / "notify/failed/held-test.json").read_text()))
            self.assertIs(marker["held"], True)
            self.assertIs(marker["reviewed"], True)
            self.assertNotIn("fallback text:", result.detail)

    def test_template_fix_notice_runs_once_per_id_per_day_without_delaying_the_push(self) -> None:
        record: dict[str, object] = {"text_id": "quota", "title": "Choose an account", "action": "Choose retry.", "body": "The work stopped.", "failed_rules": ["reader-starts-cold"], "findings": [{"rule": "reader-starts-cold", "quote": "an account"}], "sent_despite_failed_review": True}
        with mock.patch.object(subprocess, "Popen") as launch:
            send.template_fix_notice(record)
            send.template_fix_notice(record)
            send.template_fix_notice(dict(record, text_id="disk"))
            self.assertEqual(launch.call_count, 2)
            command = cast(list[str], launch.call_args.args[0])
            self.assertEqual(command[2:5], ["template-fix-deliver", "disk", "natedev"])
            self.assertIn("reader-starts-cold", command[-1])
            self.assertIn("The work stopped.", command[-1])
        tomorrow = send.now() + timedelta(days=1)
        with mock.patch.object(send, "now", return_value=tomorrow), mock.patch.object(subprocess, "Popen") as launch:
            send.template_fix_notice(record)
            launch.assert_called_once()

    def test_a_user_specified_template_gets_no_fix_notice(self) -> None:
        record: dict[str, object] = {"text_id": "nate.adhoc", "title": "natedev it is ready", "body": "It is ready.\n\nadhoc",
                                     "failed_rules": ["reader-starts-cold"], "sent_despite_failed_review": True}
        with mock.patch.object(subprocess, "Popen") as launch:
            send.template_fix_notice(record)
            launch.assert_not_called()

    def test_template_fix_launch_failure_does_not_change_a_successful_registry_delivery(self) -> None:
        record: dict[str, object] = {"text_id": "quota", "title": "Choose an account", "action": "Choose retry.", "body": "The work stopped.", "failed_rules": ["reader-starts-cold"], "sent_despite_failed_review": True}
        message = send.Message("user", "script", "Choose an account", "The work stopped.", None)
        with (mock.patch.object(send, "run", return_value=(0, "pushover: reviewed JSON: " + json.dumps(record), "")),
              mock.patch.object(subprocess, "Popen", side_effect=OSError("session unavailable")), redirect_stderr(io.StringIO())):
            result = send.user(message, "decision", ActionRequired("Choose retry."), 145, text_id="quota")
        self.assertEqual(result.outcome, "sent")
        self.assertIn("session unavailable", (self.root / "notify/template-fix-errors.jsonl").read_text())

    def test_user_passes_the_key_to_the_channel_in_both_forms(self) -> None:
        for key, text_id in ((None, ""), ("subject", ""), ("subject", "quota")):
            message = send.Message("user", "hana", "Build done", "The build passed.", key)
            with mock.patch.object(send, "run", return_value=(0, "", "")) as run:
                _ = send.user(message, "note", NoActionRequired(), 145, text_id=text_id)
            command = cast(list[str], run.call_args.args[0])
            if key is None:
                self.assertNotIn("--key", command)
            else:
                self.assertEqual(command[command.index("--key") + 1], key)
                self.assertLess(command.index("--key"), command.index("--text-id" if text_id else "--no-action"))

    def test_busy_notice_claim_does_not_wait_or_change_successful_push_delivery(self) -> None:
        record: dict[str, object] = {"text_id": "quota", "sent_despite_failed_review": True}
        directory = review.state_path() / "template-fixes"
        directory.mkdir(parents=True)
        message = send.Message("user", "script", "Choose an account", "The work stopped.", None)
        lock_name = hashlib.sha256(b"quota").hexdigest() + ".lock"
        with (directory / lock_name).open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            real_flock = fcntl.flock
            def require_nonblocking(fd: TextIO, operation: int) -> None:
                self.assertTrue(operation & fcntl.LOCK_NB, "notice claim could block the delivered push")
                real_flock(fd, operation)
            with (mock.patch.object(fcntl, "flock", side_effect=require_nonblocking),
                  mock.patch.object(send, "run", return_value=(0, "pushover: reviewed JSON: " + json.dumps(record), "")),
                  mock.patch.object(subprocess, "Popen") as launch):
                result = send.user(message, "decision", ActionRequired("Choose retry."), 145, text_id="quota")
            launch.assert_not_called()
        self.assertEqual(result.outcome, "sent")
        self.assertEqual([path.name for path in directory.iterdir()], [lock_name])

    def test_busy_template_claim_does_not_drop_a_different_templates_notice(self) -> None:
        directory = review.state_path() / "template-fixes"
        real_flock = fcntl.flock
        contended = False
        def compete_during_claim(fd: TextIO, operation: int) -> None:
            nonlocal contended
            self.assertTrue(operation & fcntl.LOCK_NB)
            real_flock(fd, operation)
            if not contended:
                contended = True
                send.template_fix_notice({"text_id": "disk"})
                send.template_fix_notice({"text_id": "quota"})
        with (mock.patch.object(fcntl, "flock", side_effect=compete_during_claim),
              mock.patch.object(subprocess, "Popen") as launch):
            send.template_fix_notice({"text_id": "quota"})
            send.template_fix_notice({"text_id": "disk"})
            send.template_fix_notice({"text_id": "quota"})
            commands = [cast(list[str], call.args[0]) for call in launch.call_args_list]
            self.assertEqual([command[2:5] for command in commands], [
                ["template-fix-deliver", "disk", "natedev"],
                ["template-fix-deliver", "quota", "natedev"],
            ])
        stamps = [path.read_text() for path in directory.iterdir() if path.suffix != ".lock"]
        self.assertCountEqual(stamps, ["disk", "quota"])

    def test_notice_recipient_refusals_are_recorded_with_the_template_id_once(self) -> None:
        for reason in ("send.py: no roster session has address session:old", "send.py: natedev matches 2 sessions:"):
            with self.subTest(reason=reason), mock.patch.object(send, "run", return_value=(2, "", reason)) as deliver:
                self.assertEqual(send.main(["template-fix-deliver", "quota", "natedev", "Fix the quota push today."]), 0)
                deliver.assert_called_once()
        entries = [cast(send.TemplateFixErrorRecord, json.loads(line)) for line in (self.root / "notify/template-fix-errors.jsonl").read_text().splitlines()]
        self.assertEqual([entry["text_id"] for entry in entries], ["quota", "quota"])
        self.assertIn("no roster session", entries[0]["error"])
        self.assertIn("matches 2 sessions", entries[1]["error"])

    def test_notice_worker_records_transport_timeout_without_a_retry(self) -> None:
        with mock.patch.object(send, "run", return_value=(None, "", "")) as deliver:
            send.deliver_template_fix("quota", "natedev", "Fix the quota push today.")
            deliver.assert_called_once()
        entry = cast(send.TemplateFixErrorRecord, json.loads((self.root / "notify/template-fix-errors.jsonl").read_text()))
        self.assertEqual((entry["text_id"], entry["error"]), ("quota", "timed out"))

    def test_probe_user_send_exits_successfully_before_review_delivery_or_logging(self) -> None:
        arguments = ["--to", "user", "--from", "probe", "--summary", "Throwaway",
                     "--no-action", "--text", "A probe must never reach the phone."]
        with (mock.patch.dict(os.environ, {"CLAUDE_PROBE_SESSION": "1"}),
              mock.patch.object(send, "run", side_effect=AssertionError("probe invoked a transport")) as transport,
              mock.patch.object(send, "log", side_effect=AssertionError("probe wrote a message log")) as log,
              mock.patch.object(review, "review_timeout_seconds", side_effect=AssertionError("probe began review")) as reviewed,
              redirect_stdout(output := io.StringIO()), redirect_stderr(error := io.StringIO())):
            self.assertEqual(send.main(arguments), 0)
        self.assertIn("not sent: probe sessions never push to the phone", output.getvalue())
        self.assertEqual(error.getvalue(), "")
        transport.assert_not_called()
        log.assert_not_called()
        reviewed.assert_not_called()
        self.assertFalse((self.root / "log.jsonl").exists())
        self.assertEqual(self.relayed, [])

    def test_user_delivery_wait_uses_the_review_limit_locally_and_across_machines(self) -> None:
        arguments = ["--to", "user", "--from", "script", "--summary", "Build choice",
                     "--action", "Choose retry.", "--text", "The build stopped."]
        with mock.patch.object(review, "review_timeout_seconds", return_value=90):
            for extra, expected in (([], 205), (["--timeout", "5"], 205),
                                    (["--timeout", "250"], 250), (["--machine", "natedev"], 245)):
                with self.subTest(extra=extra), mock.patch.object(send, "run", return_value=(0, "SENT: to the user\n", "")) as run:
                    self.assertEqual(send.send(local_options([*arguments, *extra])).outcome, "sent")
                self.assertEqual(run.call_args.args[2], expected)
                if "--machine" in extra:
                    self.assertIn("--timeout 205", cast(list[str], run.call_args.args[0])[-1])
            with mock.patch.object(send, "relay", return_value=send.Result("sent", "fake")) as relay:
                _ = self.send("--text", "body")
            self.assertEqual(relay.call_args.args[1], 40)

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
        self.assertEqual(output.getvalue(), f"FAILED: the user was not reached: {keys_missing} {send.UNREVIEWED_FALLBACK}\n")
        self.assertEqual(send.pending("user"), "")
        self.assertEqual(self.log()[0]["outcome"], "failed")

    def writer_session(self, *, named: bool = True) -> None:
        record = {"sessionId": "writer-id", "name": "Build writer" if named else ""}
        _ = (self.root / "sessions/123.json").write_text(json.dumps(record))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_PID": "123"})))

    def user_arguments(self, *extra: str) -> list[str]:
        return ["--to", "user", "--summary", "Build stopped", "--action", "Choose in natedev terminal.",
                "--text", "Choose whether to retry.", *extra]

    def failure_marker(self) -> Path:
        return self.root / "notify/failed/writer-id.json"

    def auto_mode_arguments(self, *extra: str, commands: tuple[str, ...] = ("implement.sh --to modules-impl",
                            "review.sh --to modules-test"), **changed: str) -> list[str]:
        values = {"session": "modules", "machine": "natedev", "waits": "The P542 build waits for them"} | changed
        return ["--to", "user", "--text-id", "auto_mode.refused",
                *(item for key, value in values.items() for item in ("--value", f"{key}={value}")),
                *(item for command in commands for item in ("--value", f"command={command}")), *extra]

    def test_auto_mode_template_takes_its_sender_from_this_session_and_shows_each_refused_command(self) -> None:
        _ = (self.root / "sessions/123.json").write_text(json.dumps({"sessionId": "writer-id", "name": "cargo-liner-style"}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_PID": "123"})))
        options = local_options(self.auto_mode_arguments())
        self.assertEqual(options.summary, "cargo-liner-style: auto mode stopped modules")
        self.assertEqual(options.sender, "cargo-liner-style")
        self.assertEqual(options.need, "decision")
        self.assertIn("sender=cargo-liner-style", options.values)
        self.assertIn("rule_line=", options.values)
        self.assertIn("Auto mode refused 2 shell commands in the modules session. The P542 build waits for them.\n"
                      + "implement.sh --to modules-impl\nreview.sh --to modules-test\n\n"
                      + "Approve: the work goes on now, and auto mode can stop it on these commands again.", options.text or "")
        self.assertTrue((options.text or "").endswith("Allow rule: the work goes on now, and auto mode lets these commands run from then on."))
        one = local_options(self.auto_mode_arguments(commands=("git push origin build-followups-messaging",),
                                                     waits="The merge waits for it.", rule="Bash(git push:*)"))
        self.assertIn("Auto mode refused a shell command in the modules session. The merge waits for it.\n"
                      + "git push origin build-followups-messaging\n\n"
                      + "Approve: the work goes on now, and auto mode can stop it on this command again.", one.text or "")
        self.assertTrue((one.text or "").endswith("lets this command run from then on. The rule: Bash(git push:*)"))
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as run:
            self.assertEqual(send.send(options).outcome, "sent")
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--text-id") + 1], "auto_mode.refused")
        self.assertIn("sender=cargo-liner-style", command)
        for arguments in (self.auto_mode_arguments("--value", "sender=someone"), self.auto_mode_arguments("--from", "someone"),
                          self.auto_mode_arguments("--machine", "mac"), self.auto_mode_arguments(commands=()),
                          self.auto_mode_arguments(commands=("implement.sh", " ")), self.auto_mode_arguments(count="2"),
                          self.auto_mode_arguments(does="dispatch the writer"), self.auto_mode_arguments(colour="red"),
                          self.auto_mode_arguments("--value", "waits=twice")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _ = send.parse(arguments)
        with mock.patch.dict(os.environ, {"CLAUDE_PID": ""}), redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            _ = send.parse(["--here", *self.auto_mode_arguments()])
        self.assertIn("no session record", errors.getvalue())

    def test_an_auto_mode_push_that_is_sent_or_kept_opens_a_case_for_the_stopped_session(self) -> None:
        self.writer_session()
        long = "cargo test --workspace --all-features " + "b" * 60
        options = local_options(self.auto_mode_arguments(commands=("implement.sh --to modules-impl", long)))
        replies = {"sent": (0, "pushover: sent\n", ""), "kept": (0, "pushover: kept: silence is on\n", ""),
                   "held": (4, "", "pushover: held\n"), "failed": (1, "", "pushover: unreachable\n")}
        for outcome, reply in replies.items():
            self.cases.clear()
            with self.subTest(outcome=outcome), mock.patch.object(send, "run", return_value=reply):
                self.assertEqual(send.send(options).outcome, outcome)
                # The case names the stopped session, not the sender, and each command in full.
                expected = [rca.Report("modules", "natedev", "auto_mode", ("implement.sh --to modules-impl", long),
                                       waits="The P542 build waits for them", source="Build writer",
                                       key=send.auto_mode_key("modules", ("implement.sh --to modules-impl", long)))]
                self.assertEqual(self.cases, expected if outcome in ("sent", "kept") else [])
        self.cases.clear()
        with mock.patch.object(send, "run", return_value=replies["sent"]):
            self.assertEqual(send.send(local_options(self.needs_go_arguments())).outcome, "sent")
        self.assertEqual(self.cases, [])

    def test_the_rca_pushes_are_not_held_as_a_free_text_auto_mode_refusal(self) -> None:
        values = {"sender": "cargo-liner", "session": "handler", "time": "09:01 AST",
                  "stop": "auto mode refused a shell command", "cause": "a helper ran a build command that needs your permission",
                  "fix": "helpers use the build command that is always allowed"}
        rendered = push_texts.render(rca.FIXED_TEXT, **values)
        self.assertTrue(send.reports_auto_mode_refusal(rendered.title + "\n" + rendered.body))
        for text_id in (rca.STARTED_TEXT, rca.FIXED_TEXT, rca.REWRITTEN_TEXT):
            with self.subTest(text_id=text_id):
                options = local_options(["--to", "user", "--summary", rendered.title, "--no-action", "--text", rendered.body])
                self.assertEqual(send.wrong_template(options._replace(text_id=text_id), rendered.body), None)
                self.assertEqual(send.wrong_template(options, rendered.body), send.AUTO_MODE_HELD)

    def test_auto_mode_cuts_a_long_refused_command_to_90_characters(self) -> None:
        self.writer_session()
        exact, long = "a" * 90, "cargo test --workspace --all-features " + "b" * 60
        options = local_options(self.auto_mode_arguments(commands=(exact, long)))
        self.assertIn(f"The P542 build waits for them.\n{exact}\n{long[:89]}…\n\nApprove", options.text or "")

    def test_auto_mode_waits_says_what_waits_and_gives_no_instruction(self) -> None:
        self.writer_session()
        # The tile push that reached the phone: its waits gave a second action in a second session.
        tile = "Type ! kill -TERM -3137969 -3138422 in the cargo-liner session."
        for waits in (tile, "The build waits; type: /resume", "The merge waits in the cargo-liner pane"):
            with self.subTest(waits=waits), redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
                _ = send.parse(self.auto_mode_arguments(session="tile", commands=("cargo test -p tile",), waits=waits))
            self.assertIn("waits says only what waits", errors.getvalue())
        options = local_options(self.auto_mode_arguments(waits="The merge waits for them.."))
        self.assertIn(" in the modules session. The merge waits for them.\nimplement.sh", options.text or "")

    def test_auto_mode_keeps_its_waits_sentence_only_when_the_review_approves_it_within_30_seconds(self) -> None:
        self.writer_session()
        options = local_options(self.auto_mode_arguments())
        withheld = review.Withheld(review.Draft("s", "1", "t", NoActionRequired(), "b", False), "asks for nothing")
        for verdict, kept in ((None, True), (review.Unreviewed("timeout"), False), (review.Unreviewed("review unavailable"), False),
                              (review.Failed(review.Draft("s", "1", "t", NoActionRequired(), "b", False), ["plain-words"]), False),
                              (review.Rewritten(review.Draft("s", "1", "t", NoActionRequired(), "b", False), ["plain-words"]), False),
                              (withheld, False)):
            self.verdict, self.reviews, self.cases = verdict, [], []
            with (self.subTest(verdict=verdict), mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as run,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(send.send(options).outcome, "sent")
            # The review reads the whole alert with its waits sentence, and holds it back at most 30 s.
            [(draft, limit)] = self.reviews
            self.assertEqual(limit, send.WAITS_REVIEW_SECONDS)
            self.assertEqual(send.WAITS_REVIEW_SECONDS, 30)
            self.assertIn("Auto mode refused 2 shell commands in the modules session. The P542 build waits for them.\n", draft.body)
            pushed = cast(list[str], run.call_args.args[0])
            self.assertIn("waits_line= The P542 build waits for them." if kept else "waits_line=", pushed)
            text = cast(str, self.log()[-1]["text"])
            self.assertIn("in the modules session. The P542 build waits for them.\nimplement.sh" if kept else
                          "Auto mode refused 2 shell commands in the modules session.\nimplement.sh", text)
            self.assertEqual(self.cases[0].waits, "The P542 build waits for them" if kept else "")
            self.assertEqual("goes without its waits sentence" in errors.getvalue(), not kept)
        # With the review off, or no waits sentence, nothing is reviewed.
        self.verdict, self.reviews = None, []
        (self.root / "notify").mkdir(exist_ok=True)
        _ = (self.root / "notify/review_off").write_text("")
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(send.send(options).outcome, "sent")
        self.assertEqual(self.reviews, [])

    def test_auto_mode_goes_with_one_key_per_session_and_set_of_commands_unless_the_session_gives_one(self) -> None:
        self.writer_session()
        key = local_options(self.auto_mode_arguments()).key
        self.assertEqual(key, send.auto_mode_key("modules", ("implement.sh --to modules-impl", "review.sh --to modules-test")))
        assert key is not None
        self.assertRegex(key, r"^auto-mode:modules:[0-9a-f]{12}$")
        swapped = local_options(self.auto_mode_arguments(commands=("review.sh  --to modules-test", "implement.sh --to modules-impl")))
        self.assertEqual(swapped.key, key)
        self.assertNotEqual(local_options(self.auto_mode_arguments(session="tile")).key, key)
        self.assertEqual(local_options([*self.auto_mode_arguments(), "--key", "auto"]).key, "auto")

    def test_a_registered_value_that_gives_a_second_action_is_refused(self) -> None:
        self.writer_session()
        arguments = self.needs_go_arguments(does="removes it; then type /resume in the cargo-liner session")
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            _ = send.parse(arguments)
        self.assertIn('session.needs_go gives a second action: the body says to type "/resume"', errors.getvalue())
        self.assertIn("the body names the cargo-liner pane or session", errors.getvalue())

    def test_only_auto_mode_takes_a_repeated_command_value(self) -> None:
        for arguments in (["--to", "user", "--text-id", "nate.adhoc", "--value", "command=a", "--value", "command=b"],
                          self.needs_go_arguments("--value", "command=a", "--value", "command=b")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
                _ = send.parse(arguments)
            self.assertIn("--value needs distinct key=value pairs", errors.getvalue())

    def test_sender_and_session_values_lose_the_unit_suffix_but_free_text_keeps_per_unit(self) -> None:
        _ = (self.root / "sessions/123.json").write_text(json.dumps({"sessionId": "writer-id", "name": "cargo-liner-unit"}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_PID": "123"})))
        options = local_options(self.auto_mode_arguments(session="types-unit", commands=("save_counts.py --per-unit",),
                                                         waits="The trunk-unit plan waits for it"))
        self.assertEqual(options.summary, "cargo-liner: auto mode stopped types")
        self.assertEqual(options.sender, "cargo-liner")
        self.assertEqual(options.action, ActionRequired("On natedev, in the types pane, approve the prompt or add an allow rule"))
        self.assertIn("Auto mode refused a shell command in the types session. The trunk-unit plan waits for it.\n"
                      + "save_counts.py --per-unit", options.text or "")

    def test_a_free_text_auto_mode_refusal_is_held_with_the_template_call(self) -> None:
        self.writer_session()
        for title, text in (("types: auto mode stopped", "It waits."), ("Build stopped", "Auto-mode DENIED two commands in types.")):
            with (self.subTest(title=title), mock.patch.object(send, "run", side_effect=AssertionError("pushed")),
                  redirect_stdout(io.StringIO()) as output):
                code = send.main(["--here", "--to", "user", "--summary", title, "--need", "decision",
                                  "--action", "Approve in the types pane", "--key", "auto", "--repeat-minutes", "30",
                                  "--text", text])
            self.assertEqual(code, 4)
            self.assertTrue(output.getvalue().startswith("HELD: not sent: this push reports an auto-mode refusal."))
            self.assertIn("--text-id auto_mode.refused --value session=<session>", output.getvalue())
            self.assertFalse(self.failure_marker().exists())
            self.assertEqual(self.log()[-1]["outcome"], "held")
        # The hold claims no repeat window: the resend with the template goes out.
        _ = (self.root / "sessions/123.json").write_text(json.dumps({"sessionId": "writer-id", "name": "types"}))
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(send.send(local_options([*self.auto_mode_arguments(), "--key", "auto", "--repeat-minutes", "30"])).outcome, "sent")
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(send.send(local_options(self.user_arguments("--key", "plain"))).outcome, "sent")

    def test_help_lists_the_auto_mode_template_and_when_to_use_it(self) -> None:
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
            _ = send.parse(["--help"])
        self.assertIn("auto_mode.refused: auto mode refused a command and the session waits for the user.", output.getvalue())
        self.assertIn("quota.paid_credits: an account now spends paid credits; a script with no Claude session names itself with --from.", output.getvalue())
        self.assertIn("quota.paid_credits_queued: an account now spends paid credits and the user queued a workers move;", output.getvalue())
        self.assertIn("session.needs_go: a session is stopped until the user types a go in its pane.", output.getvalue())
        self.assertIn("permission.rca_started: a unit starts to fix why a session stopped for a permission. Sent by"
                      + f" rca.py start; sessions run rca.py, not this: {rca.CALL} start <id> --owner <unit>", output.getvalue())
        self.assertIn("permission.rca_fixed: the fix merged and the session does not stop for that permission again."
                      + f" Sent by rca.py fixed; sessions run rca.py, not this: {rca.CALL} fixed <id>", output.getvalue())

    def needs_go_arguments(self, *extra: str, **changed: str) -> list[str]:
        values = {"session": "trunk", "machine": "natedev", "go": "Go: remove arrange_jacks/ from phases/130/files; 140 owns it",
                  "why": "Steps 130 and 140 both claim the arrange_jacks folder, and your permission check refused the fix.",
                  "does": "removes it from step 130, and trunk goes on"} | changed
        return ["--to", "user", "--text-id", "session.needs_go",
                *(item for key, value in values.items() for item in ("--value", f"{key}={value}")), *extra]

    def test_needs_go_template_takes_its_sender_from_this_session(self) -> None:
        _ = (self.root / "sessions/123.json").write_text(json.dumps({"sessionId": "writer-id", "name": "natedev"}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_PID": "123"})))
        options = local_options(self.needs_go_arguments())
        self.assertEqual(options.summary, "natedev: trunk waits for your go")
        self.assertEqual(options.sender, "natedev")
        self.assertEqual(options.need, "decision")
        self.assertEqual(options.action, ActionRequired(
            "On natedev, in the trunk pane, type: Go: remove arrange_jacks/ from phases/130/files; 140 owns it"))
        # The template gives why its period: a period in the value is not doubled.
        self.assertEqual(options.text, "Session: trunk\n"
                         + "Steps 130 and 140 both claim the arrange_jacks folder, and your permission check refused the fix.\n"
                         + "Your go removes it from step 130, and trunk goes on.")
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as run:
            self.assertEqual(send.send(options).outcome, "sent")
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--text-id") + 1], "session.needs_go")
        self.assertIn("sender=natedev", command)
        for arguments in (self.needs_go_arguments("--value", "sender=someone"), self.needs_go_arguments("--from", "someone"),
                          self.needs_go_arguments("--machine", "mac"), self.needs_go_arguments(colour="red")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _ = send.parse(arguments)
        with mock.patch.dict(os.environ, {"CLAUDE_PID": ""}), redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            _ = send.parse(["--here", *self.needs_go_arguments()])
        self.assertIn("no session record", errors.getvalue())

    def test_a_free_text_push_that_asks_for_a_go_is_held_with_the_template_call(self) -> None:
        self.writer_session()
        for title, text, action in (("trunk waits", "Steps 130 and 140 clash.", "In the trunk pane, type: Go: remove arrange_jacks/"),
                                    ("trunk stopped", "It does no work until you type the go.", "Answer in the trunk pane"),
                                    ("trunk waits for your go", "Steps 130 and 140 clash.", "Answer in the trunk pane")):
            with (self.subTest(title=title, text=text), mock.patch.object(send, "run", side_effect=AssertionError("pushed")),
                  redirect_stdout(io.StringIO()) as output):
                code = send.main(["--here", "--to", "user", "--summary", title, "--need", "decision",
                                  "--action", action, "--key", "go", "--repeat-minutes", "30", "--text", text])
            self.assertEqual(code, 4)
            self.assertTrue(output.getvalue().startswith("HELD: not sent: this push asks the user to type a go."))
            self.assertIn("--text-id session.needs_go --value session=<session> --value machine=<machine>", output.getvalue())
            self.assertFalse(self.failure_marker().exists())
            self.assertEqual(self.log()[-1]["outcome"], "held")
        # The hold claims no repeat window: the resend with the template goes out.
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(send.send(local_options([*self.needs_go_arguments(), "--key", "go", "--repeat-minutes", "30"])).outcome,
                             "sent")

    def test_nate_adhoc_uses_free_text_template_holds(self) -> None:
        for description, expected in (("Codex workers now spend paid credits.", send.PAID_CREDITS_HELD),
                                      ("The trunk session waits for your go.", send.NEEDS_GO_HELD)):
            with self.subTest(description=description):
                options = local_options(["--to", "user", "--text-id", "nate.adhoc", "--value", "sender=writer",
                                         "--value", "title=work stopped", "--value", f"description={description}"])
                self.assertEqual(send.wrong_template(options, description), expected)
                self.assertIsNone(send.wrong_template(options._replace(text_id="quota.paid_credits"), description))

    def paid_credits_arguments(self, *extra: str) -> list[str]:
        values = {"account": "codex2", "machine": "natedev", "credits": "25,345", "refill": "Tue 10-27 02:33 EDT",
                  "next_reset": "Sat 10-24 16:40 EDT"}
        return ["--to", "user", "--text-id", "quota.paid_credits",
                *(item for key, value in values.items() for item in ("--value", f"{key}={value}")), *extra]

    def test_paid_credits_template_takes_its_sender_from_this_session_else_the_script_name(self) -> None:
        _ = (self.root / "sessions/123.json").write_text(json.dumps({"sessionId": "writer-id", "name": "natedev"}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_PID": "123"})))
        # The session record wins over a name the caller gives.
        options = local_options(self.paid_credits_arguments("--value", "sender=quota_alert"))
        self.assertEqual(options.summary, "natedev: codex2 now uses paid credits")
        self.assertEqual(options.need, "decision")
        self.assertIn("sender=natedev", options.values)
        self.assertEqual(options.action, ActionRequired(
            "To stop it, set use_credits: off in ~/rust/hanadocs/agents/codex2.md on natedev"))
        self.assertTrue((options.text or "").startswith("codex2 has 25,345 paid credits left. Its plan refills Tue 10-27 02:33 EDT."))
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as run:
            self.assertEqual(send.send(options).outcome, "sent")
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--text-id") + 1], "quota.paid_credits")
        self.assertIn("sender=natedev", command)
        with mock.patch.dict(os.environ, {"CLAUDE_PID": ""}):
            script = local_options(self.paid_credits_arguments("--from", "quota_alert"))
            self.assertEqual(script.summary, "quota_alert: codex2 now uses paid credits")
            self.assertEqual(local_options(self.paid_credits_arguments("--value", "sender=quota_alert")).summary,
                             "quota_alert: codex2 now uses paid credits")
            with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
                _ = local_options(self.paid_credits_arguments())
            self.assertIn("needs a sender", errors.getvalue())

    def test_paid_credits_queued_template_fills_its_sender_as_the_paid_credits_one_does(self) -> None:
        values = {"account": "codex2", "machine": "natedev", "credits": "25,345", "refill": "Tue 10-27 02:33 AST"}
        arguments = ["--to", "user", "--text-id", "quota.paid_credits_queued",
                     *(item for key, value in values.items() for item in ("--value", f"{key}={value}"))]
        _ = (self.root / "sessions/123.json").write_text(json.dumps({"sessionId": "writer-id", "name": "natedev"}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_PID": "123"})))
        options = local_options([*arguments, "--value", "sender=quota_alert"])
        self.assertEqual(options.summary, "natedev: codex2 now uses paid credits")
        self.assertEqual(options.need, "decision")
        self.assertEqual(options.action, ActionRequired(
            "To stop it, set use_credits: off in ~/rust/hanadocs/agents/codex2.md on natedev"))
        self.assertTrue((options.text or "").endswith("Turn off: workers stop and wait for your queued move."))
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as run:
            self.assertEqual(send.send(options).outcome, "sent")
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--text-id") + 1], "quota.paid_credits_queued")
        self.assertIn("sender=natedev", command)
        with mock.patch.dict(os.environ, {"CLAUDE_PID": ""}):
            self.assertEqual(local_options([*arguments, "--from", "quota_alert"]).summary,
                             "quota_alert: codex2 now uses paid credits")
            with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
                _ = local_options(arguments)
            self.assertIn("quota.paid_credits_queued needs a sender", errors.getvalue())

    def test_a_free_text_paid_credit_push_is_held_with_the_template_call(self) -> None:
        self.writer_session()
        for title, text, action in (("codex2 now spends paid credits", "It refills Tue.", "Choose in natedev terminal."),
                                    ("Codex workers", "They spend credits now.", "Set use_credits: off in codex2.md"),
                                    ("Codex workers", "They now use Paid-Credit time.", "Choose in natedev terminal.")):
            with (self.subTest(title=title, text=text), mock.patch.object(send, "run", side_effect=AssertionError("pushed")),
                  redirect_stdout(io.StringIO()) as output):
                code = send.main(["--here", "--to", "user", "--summary", title, "--need", "decision",
                                  "--action", action, "--key", "credits", "--repeat-minutes", "30", "--text", text])
            self.assertEqual(code, 4)
            self.assertTrue(output.getvalue().startswith("HELD: not sent: this push reports paid credit use."))
            self.assertIn("--text-id quota.paid_credits --value account=<account>", output.getvalue())
            self.assertIn("When a workers move is queued, send quota.paid_credits_queued:", output.getvalue())
            self.assertIn("--text-id quota.paid_credits_queued --value account=<account>", output.getvalue())
            self.assertFalse(self.failure_marker().exists())
            self.assertEqual(self.log()[-1]["outcome"], "held")
        # The hold claims no repeat window, and registered pushes about credits are not held.
        with mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")):
            self.assertEqual(send.send(local_options(
                [*self.paid_credits_arguments(), "--key", "credits", "--repeat-minutes", "30"])).outcome, "sent")
            self.assertEqual(send.send(local_options(
                ["--to", "user", "--text-id", "quota.codex_credits_forecast", "--value", "account=codex1",
                 "--value", "forecast=Workers used 120 paid credits.", "--value", "destination=", "--value", "horizon=x"])).outcome,
                "sent")

    def test_cancelled_user_revision_reports_replaced_locally_and_remotely_without_a_fallback(self) -> None:
        self.writer_session()
        reason = "a newer version replaced this push; nothing was sent"
        for extra, code, reply, detail in (([], 5, "pushover: replaced: " + reason + "\n", reason),
                                          (["--machine", "natedev"], 0, "REPLACED: " + reason + "\n",
                                           "on natedev: " + reason)):
            with (self.subTest(extra=extra), mock.patch.object(send, "run", return_value=(code, reply, "")),
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(send.main([*([] if extra else ["--here"]), *self.user_arguments(*extra)]), 0)
            self.assertEqual(output.getvalue(), "REPLACED: " + detail + "\n")
            self.assertEqual(errors.getvalue(), "")
            self.assertFalse(self.failure_marker().exists())
            self.assertEqual(self.log()[-1]["outcome"], "replaced")
        self.assertEqual(send.pending("user"), "")

    def test_a_push_kept_during_silence_reports_kept_locally_and_remotely_without_a_fallback(self) -> None:
        self.writer_session()
        detail = "for the user: silence until 07:00 EDT; it goes out when silence ends"
        for extra, reply, printed in (([], 'pushover: reviewed JSON: {}\npushover: kept: silence until 07:00 EDT\n', "KEPT: " + detail),
                                      (["--machine", "natedev"], "KEPT: " + detail + "\n", "KEPT: on natedev: " + detail)):
            with (self.subTest(extra=extra), mock.patch.object(send, "run", return_value=(0, reply, "")),
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(send.main([*([] if extra else ["--here"]), *self.user_arguments(*extra)]), 0)
            self.assertEqual(output.getvalue(), printed + "\n")
            self.assertEqual(errors.getvalue(), "")
            self.assertFalse(self.failure_marker().exists())
            self.assertEqual(self.log()[-1]["outcome"], "kept")

    def test_only_a_named_session_without_from_is_answerable(self) -> None:
        self.writer_session()
        for extra, expected in (([], True), (["--from", "script"], False), (["--from", "script", "--answerable"], True)):
            with self.subTest(extra=extra), mock.patch.object(send, "run", return_value=(0, "pushover: sent\n", "")) as run:
                self.assertEqual(send.send(local_options(self.user_arguments(*extra))).outcome, "sent")
            self.assertEqual("--answerable" in run.call_args.args[0], expected)
        self.writer_session(named=False)
        with mock.patch.object(send, "run", return_value=(0, "", "")) as run:
            _ = send.send(local_options(self.user_arguments()))
        self.assertNotIn("--answerable", cast(list[str], run.call_args.args[0]))
        with mock.patch.dict(os.environ, {"CLAUDE_PID": ""}), mock.patch.object(send, "run", return_value=(0, "", "")) as run:
            _ = send.send(local_options(self.user_arguments()))
        self.assertNotIn("--answerable", cast(list[str], run.call_args.args[0]))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _ = send.parse(["--to", "agent", "--answerable", "--text", "body"])

    def test_held_questions_and_rewritten_text_are_printed_without_truncation(self) -> None:
        questions = "pushover: held for review: answer these within 5 minutes:\n1. " + "Which build? " * 40 + "\n2. Which terminal?"
        rewritten = "Build decision\nAction: Choose in natedev terminal.\n" + "Details. " * 80 + "\nLast line.\n\n"
        for code, out, err, expected in (
            (4, "", questions + "\n", "HELD: " + questions + "\n"),
            (0, "pushover: sent\npushover: sent as rewritten:\n" + rewritten + "\n", "",
             "SENT: to the user, rewritten:\n" + rewritten + "\n"),
        ):
            with self.subTest(code=code), mock.patch.object(send, "run", return_value=(code, out, err)), redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(send.main(["--here", *self.user_arguments("--from", "script")]), code)
            self.assertEqual(printed.getvalue(), expected)
            self.assertFalse(self.failure_marker().exists())

    def test_reviewed_failure_keeps_session_before_action_locally_and_remotely(self) -> None:
        original = send.FailedPush("Owner is blocked", ActionRequired("stop Owner"), "Reason: waiting.")
        record = {"title": original.title, "action": "stop Owner", "body": original.body, "session": "Owner", "reviewed": True}
        for prefix, remote_record in (("pushover: reviewed JSON: ", False), ("send.py: failed push JSON: ", True)):
            with self.subTest(remote=remote_record):
                failed = send.reviewed_failure(prefix + json.dumps(record), original, prefix=prefix, remote_record=remote_record)
                self.assertEqual(failed.message, "Session: Owner\nAction: stop Owner\nReason: waiting.")

    def test_catalog_session_is_kept_when_delivery_fails_before_review(self) -> None:
        import push_texts
        entry = push_texts.PushText("test", "shutdown", "decision", "Owner is blocked",
            push_texts.ActionTemplate("stop Owner"), "Reason: waiting.",
            (push_texts.PushExample("typical", {}),), purpose="Test.", session=push_texts.SessionTemplate("Owner"))
        with mock.patch.object(push_texts, "catalog", return_value=(entry,)), mock.patch.object(send, "run", return_value=(1, "", "offline")):
            result = send.send(local_options(["--to", "user", "--text-id", "test"]))
        self.assertIsInstance(result.failure, send.FailedPush)
        assert isinstance(result.failure, send.FailedPush)
        self.assertEqual(result.failure.message, "Session: Owner\nAction: stop Owner\nReason: waiting.")

    def test_every_user_failure_keeps_the_reviewed_text_for_this_session(self) -> None:
        self.writer_session()
        reviewed = {"title": "Retry needs your choice", "action": "Choose retry.\nUse natedev.", "body": "The build stopped.\nChoose retry or stop."}
        stdout = "pushover: reviewed JSON: " + json.dumps(reviewed) + "\n"
        expected_message = "Action: Choose retry.\nUse natedev.\nThe build stopped.\nChoose retry or stop."
        for code in (1, 2, None):
            with self.subTest(code=code), mock.patch.object(send, "run", return_value=(code, stdout, "post unavailable")), mock.patch.object(time, "time", return_value=1234567890.0):
                result = send.send(local_options(self.user_arguments()))
            self.assertEqual(result.outcome, "failed")
            self.assertTrue(result.detail.endswith("fallback text:\nRetry needs your choice — Action: Choose retry. Use natedev."))
            self.assertEqual(send.as_dict(send.loads(self.failure_marker().read_text()))["fallback"],
                             "Retry needs your choice — Action: Choose retry. Use natedev.")
            marker = send.as_dict(send.loads(self.failure_marker().read_text()))
            self.assertIs(marker["reviewed"], True)
            self.assertEqual(marker["need"], "note")
            self.assertEqual((marker["title"], marker["action"], marker["body"], marker["message"]),
                             (reviewed["title"], reviewed["action"], reviewed["body"], expected_message))
            self.assertEqual(marker["time"], 1234567890.0)
            self.failure_marker().unlink()
        with mock.patch.object(send, "run", return_value=(None, "", "")):
            result = send.send(local_options(self.user_arguments()))
        self.assertTrue(result.detail.endswith(send.UNREVIEWED_FALLBACK))
        self.assertIs(send.as_dict(send.loads(self.failure_marker().read_text()))["reviewed"], False)

    def test_refusals_and_non_session_failures_write_no_marker(self) -> None:
        self.writer_session()
        with mock.patch.object(send, "run", return_value=(2, "", "pushover: refused before sending: invalid action")):
            self.assertEqual(send.send(local_options(self.user_arguments())).outcome, "refused")
        self.assertFalse(self.failure_marker().exists())
        with mock.patch.dict(os.environ, {"CLAUDE_PID": ""}), mock.patch.object(send, "run", return_value=(1, "", "failed")):
            self.assertEqual(send.send(local_options(self.user_arguments())).outcome, "failed")
        self.assertFalse(self.failure_marker().exists())

    def test_unreviewed_failures_offer_fallback_only_for_blocked_locally_and_remotely(self) -> None:
        self.writer_session()
        for remote in (False, True):
            for need in ("note", "decision", "blocked"):
                for code in (1, None):
                    extra = ["--need", need, *(["--self-clearing"] if need == "blocked" else []),
                             *(["--machine", "natedev"] if remote else ["--here"])]
                    with (self.subTest(remote=remote, need=need, code=code),
                          mock.patch.object(send, "run", return_value=(code, "", "delivery unavailable")),
                          redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                        self.assertEqual(send.main(self.user_arguments(*extra)), 3)
                    marker = send.as_dict(send.loads(self.failure_marker().read_text()))
                    self.assertIs(marker["reviewed"], False)
                    self.assertEqual(marker["need"], need)
                    self.assertEqual("fallback text:" in output.getvalue(), need == "blocked")
                    self.assertEqual(send.UNREVIEWED_FALLBACK in output.getvalue(), need != "blocked")
                    transport = send.as_dict(send.loads(errors.getvalue().split("send.py: failed push JSON: ")[1]))
                    self.assertIs(transport["reviewed"], False)
                    self.assertEqual(transport["need"], need)

    def test_remote_failure_preserves_review_status_instead_of_assuming_json_means_reviewed(self) -> None:
        self.writer_session()
        for reviewed in (False, True):
            record: send.FailedPushTransportRecord = {"title": "Reviewed choice", "action": "Choose retry.",
                "body": "The build stopped.", "reviewed": reviewed, "need": "decision"}
            with (self.subTest(reviewed=reviewed),
                  mock.patch.object(send, "run", return_value=(3, "FAILED: post failed\n",
                    "send.py: failed push JSON: " + json.dumps(record))),
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(send.main(self.user_arguments("--machine", "natedev", "--need", "decision")), 3)
            marker = send.as_dict(send.loads(self.failure_marker().read_text()))
            self.assertIs(marker["reviewed"], reviewed)
            self.assertEqual(marker["need"], "decision")
            self.assertEqual("fallback text:" in output.getvalue(), reviewed)
            transport = send.as_dict(send.loads(errors.getvalue().split("send.py: failed push JSON: ")[1]))
            self.assertIs(transport["reviewed"], reviewed)

    def test_remote_reviewed_failure_retains_metadata_when_stdout_header_is_lost(self) -> None:
        self.writer_session()
        for need in ("note", "decision", "blocked"):
            record: send.FailedPushTransportRecord = {"title": "Reviewed choice", "action": "Choose retry.",
                "body": "The reviewed build stopped.", "reviewed": True, "need": need,
                "held_source": "writer source", "held_identity": "original-hold-hash"}
            with (self.subTest(need=need), mock.patch.object(send, "run", return_value=(255, "",
                  "send.py: failed push JSON: " + json.dumps(record))),
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(send.main(self.user_arguments("--machine", "natedev", "--need", "decision")), 3)
            marker = send.as_dict(send.loads(self.failure_marker().read_text()))
            self.assertIs(marker["reviewed"], True)
            self.assertEqual(marker["need"], need)
            self.assertEqual((marker["title"], marker["action"], marker["body"]),
                             (record["title"], record["action"], record["body"]))
            self.assertEqual((marker["held_source"], marker["held_identity"], marker["held_machine"]),
                             ("writer source", "original-hold-hash", "natedev"))
            self.assertIn("fallback text:\nReviewed choice — Action: Choose retry.", output.getvalue())
            self.assertNotIn(send.UNREVIEWED_FALLBACK, output.getvalue())
            transport = send.as_dict(send.loads(errors.getvalue().split("send.py: failed push JSON: ")[1]))
            self.assertIs(transport["reviewed"], True)
            self.assertEqual(transport["need"], need)

    def test_invalid_review_record_does_not_authorize_original_text_as_reviewed(self) -> None:
        self.writer_session()
        for record in ("invalid JSON", '{"title":3,"body":"text"}'):
            with (self.subTest(record=record), mock.patch.object(send, "run", return_value=(1,
                  "pushover: reviewed JSON: " + record, "post failed"))):
                result = send.send(local_options(self.user_arguments()))
            self.assertTrue(result.detail.endswith(send.UNREVIEWED_FALLBACK))
            self.assertIs(send.as_dict(send.loads(self.failure_marker().read_text()))["reviewed"], False)

    def test_remote_outcomes_keep_payload_and_forward_answerability(self) -> None:
        self.writer_session()
        for header, payload, code in (
            ("HELD: answer these", "1. Which build?\n2. Which machine? " + "Details " * 60, 4),
            ("SENT: to the user, rewritten:", "Build choice\nAction: Choose.\n" + "Details " * 60 + "\n\n", 0),
            ("FAILED: post failed fallback text:", "Build stopped — Action: Choose in natedev terminal.", 3),
        ):
            with self.subTest(header=header), mock.patch.object(send, "run", return_value=(code, "remote banner\n" + header + "\n" + payload + "\n", "")) as run:
                result = send.send(send.parse(self.user_arguments("--machine", "natedev")))
            if code == 3:
                self.assertEqual(result.detail, "on natedev: post failed " + send.UNREVIEWED_FALLBACK)
            else:
                self.assertEqual(result.detail, "on natedev: " + header.partition(": ")[2] + "\n" + payload)
            self.assertIn("--answerable", cast(list[str], run.call_args.args[0])[-1])
            self.assertTrue(cast(list[str], run.call_args.args[0])[-1].startswith("CLAUDE_PID= "))
            if code == 3:
                marker = send.as_dict(send.loads(self.failure_marker().read_text()))
                self.assertEqual(marker["fallback"], payload)
        with mock.patch.object(send, "run", return_value=(255, "", "ssh unavailable")):
            result = send.send(send.parse(self.user_arguments("--machine", "natedev")))
        self.assertIn(send.UNREVIEWED_FALLBACK, result.detail)
        self.assertTrue(self.failure_marker().exists())

    def test_remote_failure_retains_multiline_action_metadata(self) -> None:
        self.writer_session()
        record: send.FailedPushTransportRecord = {"reviewed": True, "need": "note", "title": "Build choice", "action": "Choose retry.\nUse natedev.",
                                          "body": "The build stopped.\nChoose retry or stop."}
        payload = "Build choice — Action: Choose retry. Use natedev."
        with mock.patch.object(send, "run", return_value=(3, "FAILED: post failed fallback text:\n" + payload + "\n",
                                                         "send.py: failed push JSON: " + json.dumps(record) + "\n")):
            result = send.send(send.parse(self.user_arguments("--machine", "natedev")))
        self.assertTrue(result.detail.endswith(payload))
        marker = send.as_dict(send.loads(self.failure_marker().read_text()))
        self.assertEqual(marker["action"], record["action"])
        self.assertEqual(marker["body"], record["body"])
        self.assertEqual(marker["fallback"], payload)

    def test_no_action_failure_marker_and_unwritable_marker_keep_fallback_output(self) -> None:
        self.writer_session()
        arguments = ["--to", "user", "--summary", "Update ready", "--no-action", "--text", "Update is ready."]
        reviewed = "pushover: reviewed JSON: " + json.dumps({"title": "Update ready", "action": None, "body": "Update is ready."})
        with mock.patch.object(send, "run", return_value=(1, reviewed, "post failed")):
            result = send.send(local_options(arguments))
        self.assertTrue(result.detail.endswith("fallback text:\nUpdate ready — No action needed."))
        marker = send.as_dict(send.loads(self.failure_marker().read_text()))
        self.assertIsNone(marker["action"])
        self.assertEqual(marker["message"], "No action needed.\nUpdate is ready.")
        with (mock.patch.object(send, "run", return_value=(1, reviewed, "post failed")),
              mock.patch.object(send, "write_atomic", side_effect=OSError("state unavailable")),
              redirect_stderr(io.StringIO()) as errors):
            # The message log uses a direct append, so only the failure marker write fails.
            result = send.send(local_options(arguments))
        self.assertEqual(result.outcome, "failed")
        self.assertIn("fallback text:", result.detail)
        self.assertIn("could not write fallback marker", errors.getvalue())

    def test_remote_uses_the_first_outcome_header_and_no_far_side_marker(self) -> None:
        self.writer_session()
        options = send.parse(self.user_arguments("--from", "script", "--machine", "natedev"))
        message = send.Message("user", "script", "title", "body", None)
        with mock.patch.object(send, "run", return_value=(4, "SENT: earlier\nHELD: current\n1. Which build?\n", "")) as run:
            result = send.remote(message, "natedev", options)
        self.assertEqual(result, send.Result("sent", "on natedev: earlier\nHELD: current\n1. Which build?"))
        self.assertNotIn("--answerable", cast(list[str], run.call_args.args[0])[-1])
        with mock.patch.dict(os.environ, {"CLAUDE_PID": ""}), mock.patch.object(send, "run", return_value=(1, "", "post failed")):
            _ = send.send(local_options(self.user_arguments("--from", "script")))
        self.assertFalse(self.failure_marker().exists())

    def test_fallback_is_one_line_and_truncates_at_a_word_boundary(self) -> None:
        """F001: the built-in push has only a short message."""
        for action in (ActionRequired("Choose retry.\nUse natedev."), NoActionRequired()):
            failed = send.FailedPush("Build stopped", action, "Body is excluded.")
            self.assertNotIn("\n", failed.text)
            self.assertNotIn("Body is excluded.", failed.text)
            self.assertEqual(failed.text, "Build stopped — " + " ".join(first_line(action).split()))
        failed = send.FailedPush("Build " * 60, ActionRequired("Choose retry."), "body")
        self.assertLessEqual(len(failed.text), 200)
        self.assertTrue(failed.text.endswith("…"))
        self.assertTrue(("Build " * 60).startswith(failed.text[:-1] + " "))

    def test_remote_payload_headers_never_change_the_delivery_outcome(self) -> None:
        """F002: payload text cannot become an outcome header."""
        self.writer_session()
        for header, payload, outcome, marker in (
            ("FAILED: post failed", "SENT: previous repair completed.", "failed", True),
            ("SENT: to the user, rewritten:", "FAILED: previous repair failed.", "sent", False),
        ):
            with self.subTest(outcome=outcome), mock.patch.object(send, "run", return_value=(
                    3 if marker else 0, header + "\n" + payload + "\n", "")):
                result = send.send(send.parse(self.user_arguments("--machine", "natedev")))
            self.assertEqual(result.outcome, outcome)
            self.assertIn(payload, result.detail)
            self.assertEqual(self.failure_marker().exists(), marker)
            self.failure_marker().unlink(missing_ok=True)

    def test_quoted_review_record_cannot_replace_the_first_machine_record(self) -> None:
        """F003: a quoted body record cannot replace the reviewed version."""
        self.writer_session()
        record: send.FailedPushTransportRecord = {"reviewed": True, "need": "note", "title": "Reviewed choice", "action": "Choose retry.", "body": "Real body."}
        fake = {"title": "Wrong choice", "action": "Stop.", "body": "Wrong body."}
        for prefix, remote in (("pushover: reviewed JSON: ", False), ("send.py: failed push JSON: ", True)):
            records = prefix + json.dumps(record) + "\n" + prefix + json.dumps(fake) + "\n"
            reply = (3, "FAILED: post failed\n", records) if remote else (1, records, "post failed")
            arguments = self.user_arguments("--machine", "natedev") if remote else self.user_arguments("--here")
            with self.subTest(remote=remote), mock.patch.object(send, "run", return_value=reply):
                result = send.send(send.parse(arguments))
            self.assertTrue(result.detail.endswith("fallback text:\nReviewed choice — Action: Choose retry."))
            marker = send.as_dict(send.loads(self.failure_marker().read_text()))
            self.assertEqual(marker["title"], "Reviewed choice")
            self.assertEqual(marker["body"], "Real body.")

    def test_failed_revision_marker_retains_source_and_delivery_machine(self) -> None:
        """F004: the fallback can release the revision's restored hold."""
        self.writer_session()
        record: send.FailedPushTransportRecord = {"reviewed": True, "need": "note", "title": "Build choice", "action": "Choose retry.",
                                          "body": "The build stopped.", "held_source": "writer source",
                                          "held_identity": "original-hold-hash"}
        for remote in (False, True):
            reply = (3, "FAILED: post failed\n", "send.py: failed push JSON: " + json.dumps(record)) if remote else (
                1, "pushover: reviewed JSON: " + json.dumps(record), "post failed")
            arguments = self.user_arguments("--machine", "natedev") if remote else self.user_arguments("--here")
            with self.subTest(remote=remote), mock.patch.object(send, "run", return_value=reply):
                _ = send.send(send.parse(arguments))
            marker = send.as_dict(send.loads(self.failure_marker().read_text()))
            self.assertEqual(marker["held_source"], "writer source")
            self.assertEqual(marker["held_identity"], "original-hold-hash")
            self.assertEqual(marker["held_machine"], "natedev" if remote else "")

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


class PeerSendTests(ShutdownTestCase):
    root: Path = Path()
    local: roster.Roster = roster.Roster([], [])
    ssh_log: Path = Path()
    relayed: list[send.Message] = []
    mesh_calls: list[tuple[send.Message, str]] = []
    daemon_calls: list[str] = []

    @override
    def setUp(self) -> None:
        super().setUp()
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

    def daemon(self, owner: codex_daemon.CodexDaemon, thread: str, _text: str, **_options: object) -> codex_daemon.QueueResult:
        self.assertIsInstance(owner, codex_daemon.CodexDaemon)
        self.daemon_calls.append(thread)
        return codex_daemon.Queued()

    def row(self, name: str, address: str, *, machine: str = "mac") -> roster.RosterEntry:
        kind = "claude" if address.startswith("session:") else "codex"
        worker = address.startswith("worker:")
        return roster.RosterEntry(
            machine, kind, "worker" if worker else "freestanding", name, "working",
            roster.Messageable(address), "/fixture", roster.NoShowrunner(), roster.NoDirector(),
            roster.WorkerAssignment("impl", "/fixture/run") if worker else roster.NotAWorker(),
            roster.CodexThread(address.removeprefix("codex:")) if kind == "codex" else roster.NoCodexThread(),
            roster.ClaudeSession(address.removeprefix("session:")) if kind == "claude" else roster.NoClaudeSession(),
            daemon=(codex_daemon.CodexDaemon(self.root / ".codex2", self.root / "daemon.sock")
                    if address.startswith("codex:") else roster.NotDaemonSession()),
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
        for address in ("session:peer", "worker:peer-run/impl", "codex:peer-thread"):
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
        for address in ("session:peer", "worker:peer-run/impl", "codex:peer-thread"):
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
        with short_socket_directory() as directory, socket.socket(socket.AF_UNIX) as listener:
            address = f"uds:{directory}/local.sock"
            listener.bind(address.removeprefix("uds:"))
            for target, flags in (("remote", ["--here"]), (address, []), (address, ["--machine", "mac"])):
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
                (["--codex", "--session-dir", "/fixture/run"], send.MeshWorker("/fixture/run", "recipient")),
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
        for target, expected in (("session:gone", 1), ("codex:gone", 3), ("worker:gone/impl", 2), ("bare missing", 0)):
            with self.subTest(target=target):
                before = len(self.ssh_calls())
                status, output, errors = self.run_main(target)
                self.assertEqual(status, expected, output + errors)
                self.assertEqual(len(self.ssh_calls()), before + 1)
                if target.startswith("session:"):
                    self.assertIn("fixture text", send.queue_path("session-gone").read_text())
                elif target.startswith("worker:"):
                    self.assertIn("no roster session has address", errors)
                elif target.startswith("codex:"):
                    self.assertIn("FAILED: no live Codex daemon has thread gone loaded; checked ~/.codex", output)
        self.assertEqual(self.daemon_calls, [])
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.forwarded(), [])

    def test_local_problems_prevent_unproven_bare_name_fallback_to_the_peer(self) -> None:
        self.local = roster.Roster([], ["worker source unreadable"])
        self.peer([self.row("absent here", "codex:peer")])
        status, output, errors = self.run_main("absent here")
        self.assertEqual(status, 2, output + errors)
        self.assertIn("worker source unreadable", errors)
        self.assertIn("--machine", errors)
        self.assertIn("address", errors)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.relayed + self.mesh_calls + self.daemon_calls, [])

    def test_an_address_can_resolve_on_peer_even_when_a_local_source_failed(self) -> None:
        self.local = roster.Roster([], ["worker source unreadable"])
        self.peer([self.row("peer", "codex:peer")])
        status, output, errors = self.run_main("codex:peer")
        self.assertEqual(status, 0, output + errors)
        self.assertIn("worker source unreadable", errors)
        self.assertEqual(len(self.forwarded()), 1)

    def test_remote_between_turns_worker_address_keeps_no_roster_address_error(self) -> None:
        row = self.row("impl", "worker:remote/impl")
        row = roster.RosterEntry(row.machine, row.kind, row.role, row.name, row.status,
            roster.CannotReceiveMessages("between turns; its unit director resumes it"), row.cwd,
            row.showrunner, row.director, row.assignment, row.thread, row.session)
        self.peer([row])
        status, output, errors = self.run_main("worker:remote/impl")
        self.assertEqual(status, 2, output + errors)
        self.assertIn("no roster session has address worker:remote/impl", errors)
        self.assertEqual(self.forwarded(), [])

    def test_ambiguous_peer_name_lists_every_address_before_sending(self) -> None:
        self.peer([self.row("shared", "codex:one"), self.row("shared", "worker:run/two")])
        status, output, errors = self.run_main("shared")
        self.assertEqual(status, 2, output + errors)
        self.assertIn("codex:one", errors)
        self.assertIn("worker:run/two", errors)
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
                    row = roster.RosterEntry("mac" if remote else "natedev", kind, cast(roster.Role, role), "recipient", "waiting",
                        roster.CannotReceiveMessages(reason), "/fixture", roster.NoShowrunner(), relationship,
                        roster.WorkerAssignment("review", "/run") if role == "worker" else roster.NotAWorker())
                    self.local = roster.Roster([] if remote else [row], [])
                    self.peer([row] if remote else [])
                    before = len(self.forwarded())
                    status, output, errors = self.run_main("recipient")
                    self.assertEqual(status, 2, output + errors)
                    self.assertIn(f"recipient cannot take messages ({reason}){expected_tail}\n", errors)
                    self.assertEqual(len(self.forwarded()), before)


class ReadRelayTests(ShutdownTestCase):
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


class AccountDaemonRoutingTests(unittest.TestCase):
    root: Path = Path()
    homes: tuple[Path, Path] = (Path(), Path())
    queue_contents: dict[Path, bytes] = {}
    queued: list[dict[str, object]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(short_socket_directory()))
        self.homes = (self.root / ".codex", self.root / ".codex2")
        notes = self.root / "agents"
        self.queue_contents, self.queued = {}, []
        for home, label in zip(self.homes, ("codex1", "codex2")):
            write_codex_account(home, notes, label)
            (home / "app-server-control").mkdir()
            queue = home / "queue_1.sqlite"
            with closing(sqlite3.connect(queue)) as database, database:
                _ = database.execute("CREATE TABLE sentinel (message TEXT)")
                _ = database.execute("INSERT INTO sentinel VALUES (?)", (label,))
            self.queue_contents[queue] = queue.read_bytes()
        (self.homes[0] / "sessions").mkdir()
        (self.homes[1] / "sessions").symlink_to(self.homes[0] / "sessions", target_is_directory=True)
        (self.root / "claude-sessions").mkdir()
        binaries = self.root / "bin"
        binaries.mkdir()
        ps = binaries / "ps"
        _ = ps.write_text("#!/bin/sh\nexit 0\n")
        ps.chmod(0o755)
        for command in ("ssh", "codex", "claude"):
            blocked = binaries / command
            _ = blocked.write_text("#!/bin/sh\necho 'unexpected external tool' >&2\nexit 91\n")
            blocked.chmod(0o755)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "PATH": str(binaries), "AGENT_NOTES_DIR": str(notes),
            "PROCESS_TABLE_PS": str(ps), "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "claude-sessions"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "ROSTER_RUN_ROOT": str(self.root / "runs"),
            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership"),
            "REVIEW_POOL_STATE": str(self.root / "review-pool"),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
        }, clear=True)))

    def serve_loaded_thread(self, connection: socket.socket, *, name: str = "Account window") -> None:
        initialize = receive_frame(connection)
        self.assertEqual(initialize["method"], "initialize")
        reply(connection, initialize, {})
        self.assertEqual(receive_frame(connection)["method"], "initialized")
        while True:
            try:
                request = receive_frame(connection)
            except EOFError:
                return
            method = request["method"]
            params = cast(dict[str, object], request["params"])
            if method == "thread/loaded/list":
                reply(connection, request, {"data": ["account-thread"]})
            elif method == "thread/read":
                reply(connection, request, {"thread": {
                    "id": "account-thread", "name": name, "cwd": str(self.root),
                    "status": {"type": "idle"},
                }})
            elif method == "thread/queue/add":
                self.queued.append(params)
                reply(connection, request, {"queuedSubmission": {"id": "reply"}})
            else:
                self.fail(f"unexpected daemon method: {method}")

    def daemon(self, home: Path, *, name: str = "Account window") -> FakeDaemon:
        return FakeDaemon(home / "app-server-control/app-server-control.sock",
                          partial(self.serve_loaded_thread, name=name), repeat=True)

    def command(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(Path(send.__file__).with_name(script)), *arguments],
                              capture_output=True, text=True, check=False, timeout=30)

    def assert_queues_unchanged(self) -> None:
        for queue, before in self.queue_contents.items():
            self.assertEqual(queue.read_bytes(), before, str(queue))

    def test_a_codex_name_with_different_case_reaches_its_loaded_account_daemon(self) -> None:
        with self.daemon(self.homes[1], name="morty"):
            delivered = self.command("send.py", "--here", "--to", "Morty", "--from", "sdlc", "--text", "hello by name")
        self.assertEqual(delivered.returncode, 0, delivered.stdout + delivered.stderr)
        self.assertEqual(delivered.stdout, "SENT: queued on Codex session morty\n")
        self.assertEqual([(row["threadId"], row["input"]) for row in self.queued],
                         [("account-thread", [{"type": "text", "text": "hello by name"}])])
        self.assertFalse((self.root / "state/message/queue/Morty.jsonl").exists())
        self.assert_queues_unchanged()

    def test_names_that_differ_only_in_case_are_ambiguous_even_with_an_exact_match(self) -> None:
        with self.daemon(self.homes[0], name="Morty"), self.daemon(self.homes[1], name="morty"):
            for target in ("MORTY", "Morty", "morty"):
                with self.subTest(target=target):
                    delivered = self.command("send.py", "--here", "--to", target, "--from", "sdlc", "--text", "hello")
                    self.assertEqual(delivered.returncode, 2, delivered.stdout + delivered.stderr)
                    self.assertIn(f"{target} matches 2 sessions:", delivered.stderr)
                    for label, home in zip(("codex1", "codex2"), self.homes):
                        self.assertIn(f"codex:account-thread (freestanding, {label}, {home})", delivered.stderr)
        self.assertEqual(self.queued, [])
        self.assertFalse((self.root / "state/message/queue").exists())
        self.assert_queues_unchanged()

    def test_shared_history_routes_a_named_session_through_its_owning_account_daemon(self) -> None:
        with self.daemon(self.homes[1]):
            listed = self.command("roster.py", "--here", "--snapshot")
            self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
            found = roster.RosterSnapshot.decode(listed.stdout).result
            self.assertEqual(found.problems, [])
            self.assertEqual(len(found.entries), 1)
            entry = found.entries[0]
            self.assertEqual(entry.account, roster.AccountLabel("codex2"))
            self.assertEqual(entry.daemon, codex_daemon.CodexDaemon(
                self.homes[1], self.homes[1] / "app-server-control/app-server-control.sock"))
            delivered = self.command("send.py", "--here", "--to", entry.name, "--from", "sdlc", "--text", "named reply")
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        self.assertEqual(delivered.stdout, "SENT: queued on Codex session Account window\n")
        self.assertEqual([(row["threadId"], row["input"]) for row in self.queued],
                         [("account-thread", [{"type": "text", "text": "named reply"}])])
        self.assert_queues_unchanged()

    def test_an_unloaded_thread_fails_and_names_every_account_without_writing_a_queue(self) -> None:
        delivered = self.command("send.py", "--here", "--to", "codex:account-thread", "--from", "sdlc", "--text", "reply")
        self.assertEqual(delivered.returncode, 3, delivered.stderr)
        self.assertEqual(delivered.stdout, "FAILED: no live Codex daemon has thread account-thread loaded; checked ~/.codex, ~/.codex2\n")
        self.assertEqual(self.queued, [])
        self.assert_queues_unchanged()

    def test_a_thread_loaded_in_two_accounts_is_refused_with_both_account_names(self) -> None:
        with self.daemon(self.homes[0]), self.daemon(self.homes[1]):
            delivered = self.command("send.py", "--here", "--to", "codex:account-thread", "--from", "sdlc", "--text", "reply")
        self.assertEqual(delivered.returncode, 2)
        self.assertIn("codex:account-thread matches 2 sessions:", delivered.stderr)
        for label in ("codex1", "codex2"):
            self.assertIn(label, delivered.stderr)
        self.assertEqual(self.queued, [])
        self.assert_queues_unchanged()

    def test_a_process_reply_without_a_process_home_reaches_the_loaded_account_socket(self) -> None:
        with self.daemon(self.homes[1]):
            delivered = self.command("send.py", "--to", "codex:account-thread", "--from", "sdlc", "--text", "survey reply")
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        self.assertEqual(delivered.stdout, "SENT: queued on Codex session Account window\n")
        self.assertEqual([(row["threadId"], row["input"]) for row in self.queued],
                         [("account-thread", [{"type": "text", "text": "survey reply"}])])
        self.assert_queues_unchanged()

    def test_a_process_message_to_a_nondefault_account_receives_a_reply_at_the_senders_socket(self) -> None:
        (self.root / ".claude").mkdir()
        _ = (self.root / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "sender@example.invalid"}}))
        executable = self.root / ".local/bin/claude"
        executable.parent.mkdir(parents=True)
        _ = executable.write_text(
            f"#!{sys.executable}\n"
            + "import json, socket, sys\n"
            + f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            + "from test_send import INIT, OK, answer, call, stream\n"
            + "prompt = sys.stdin.read()\n"
            + "target = json.loads(next(line[6:] for line in prompt.splitlines() if line.startswith('- to: ')))\n"
            + "text = prompt.split('----- BEGIN MESSAGE -----\\n', 1)[1].rsplit('\\n----- END MESSAGE -----', 1)[0]\n"
            + "with socket.socket(socket.AF_UNIX) as connection:\n"
            + "    connection.connect(target.removeprefix('uds:'))\n"
            + "    connection.sendall(text.encode())\n"
            + "print(stream(INIT, call(text), answer(OK)), end='')\n")
        executable.chmod(0o755)
        question, response = "Can you confirm the survey?", "The survey is confirmed."
        with ShortUnixSocket() as sender, self.daemon(self.homes[1]):
            address = f"uds:{sender.bind_socket()}"
            sender.listen(1)
            sender.settimeout(5)
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": address}):
                delivered = self.command("send.py", "--to", "codex:account-thread", "--from", "sdlc", "--text", question)
            self.assertEqual(delivered.returncode, 0, delivered.stderr)
            self.assertEqual(delivered.stdout, "SENT: queued on Codex session Account window\n")
            self.assertEqual([(row["threadId"], row["input"]) for row in self.queued],
                             [("account-thread", [{"type": "text", "text": question}])])
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "account-thread"}):
                returned = self.command("send.py", "--to", address, "--from", "Account window", "--text", response)
            self.assertEqual(returned.returncode, 0, returned.stderr)
            self.assertTrue(returned.stdout.startswith("SENT:"), returned.stdout)
            with sender.accept()[0] as connection:
                connection.settimeout(5)
                with connection.makefile("r", encoding="utf-8") as inbox:
                    self.assertEqual(inbox.read(), response + "\nShown sender: codex:account-thread")
        self.assert_queues_unchanged()


if __name__ == "__main__":
    _ = unittest.main()
