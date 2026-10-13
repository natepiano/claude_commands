"""A fallback uses the failed reviewed text once, within ten minutes."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import time
import sys
import subprocess
import shlex
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
if TYPE_CHECKING:
    from ..notify import review, pushover
    from ..notify.user_action import ActionRequired
else:
    import review
    import pushover
    from user_action import ActionRequired

spec = importlib.util.spec_from_file_location("push_gate", Path(__file__).with_name("pre-tool-use-push-gate.py"))
assert spec is not None and spec.loader is not None
class PushGate(Protocol):
    DENIAL: str

    def main(self) -> None: ...


module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
push_gate = cast(PushGate, cast(object, module))


class PushInput(TypedDict):
    message: str
    status: str


class HookPayload(TypedDict):
    session_id: str
    tool_name: str
    tool_input: PushInput


class Marker(TypedDict):
    time: float
    title: str
    action: str | None
    body: str
    message: str
    fallback: str
    held_source: NotRequired[str]
    held_identity: NotRequired[str]
    held_machine: NotRequired[str]


class Decision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class Response(TypedDict):
    hookSpecificOutput: Decision


class PushGateTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.root)})))
        _ = self.enterContext(mock.patch.object(time, "time", return_value=2000.0))

    def marker(self, *, stamp: float = 1999.0, action: str | None = "Choose retry.") -> Path:
        directory = self.root / "failed"
        directory.mkdir(exist_ok=True)
        message = ("Action: " + action if action is not None else "No action needed.") + "\nThe build stopped."
        record: Marker = {"time": stamp, "title": "Build choice", "action": action,
                          "body": "The build stopped.", "message": message,
                          "fallback": "Build choice — " + message.partition("\n")[0]}
        path = directory / "writer.json"
        _ = path.write_text(json.dumps(record))
        return path

    def call(self, *, message: str = "Build choice — Action: Choose retry.",
             session: str = "writer", raw: str = "") -> tuple[str, str]:
        payload: HookPayload = {"session_id": session, "tool_name": "PushNotification",
                               "tool_input": {"message": message, "status": "proactive"}}
        with mock.patch("sys.stdin", io.StringIO(raw or json.dumps(payload))), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            push_gate.main()
        return out.getvalue(), err.getvalue()

    def assert_denied(self, output: str) -> None:
        response = cast(Response, json.loads(output))
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(response["hookSpecificOutput"]["permissionDecisionReason"], push_gate.DENIAL)

    def test_matching_recent_failure_allows_and_is_consumed_once(self) -> None:
        path = self.marker()
        self.assertEqual(self.call(), ("", ""))
        self.assertFalse(path.exists())
        output, errors = self.call()
        self.assert_denied(output)
        self.assertEqual(errors, "")

    def test_whitespace_changes_are_allowed_for_action_and_no_action(self) -> None:
        for action in ("Choose retry.", None):
            path = self.marker(action=action)
            header = "Action: Choose retry." if action is not None else "No action needed."
            self.assertEqual(self.call(message="  Build\tchoice —  " + header + " "), ("", ""))
            self.assertFalse(path.exists())

    def test_missing_expired_and_other_session_markers_deny(self) -> None:
        self.assert_denied(self.call()[0])
        for stamp in (1400.0, 1399.0, 2001.0):
            path = self.marker(stamp=stamp)
            self.assert_denied(self.call()[0])
            self.assertTrue(path.exists())
        path = self.marker()
        self.assert_denied(self.call(session="other")[0])
        self.assertTrue(path.exists())
        path = self.marker(stamp=1400.001)
        self.assertEqual(self.call(), ("", ""))
        self.assertFalse(path.exists())

    def test_mismatched_message_denies_and_keeps_marker(self) -> None:
        path = self.marker()
        for message in ("Different title — Action: Choose retry.", "Build choice — Action: Stop.", "The build stopped."):
            self.assert_denied(self.call(message=message)[0])
            self.assertTrue(path.exists())
        self.assertEqual(self.call(), ("", ""))

    def test_failed_revision_fallback_releases_hold_before_expiry(self) -> None:
        """F004: an allowed fallback prevents later delivery of the restored hold."""
        draft = review.Draft("writer source", "1", "Build choice", ActionRequired("Choose retry."),
                             "The build stopped.", True)
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")
        with mock.patch.object(review, "schedule"):
            review.hold(review.Held(draft.source, 1000, draft, draft, ["Which build?"]))
        with (mock.patch.object(review, "review", return_value=review.Approved(draft)),
              mock.patch.object(pushover, "LOG_FILE", self.root / "push.jsonl"),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO())):
            self.assertEqual(pushover.send(["--source", draft.source, "--priority", "1", "--answerable",
                                           "--action", "Choose retry.", draft.title, draft.body],
                                          keys, lambda _u, _d: (500, b'{"status":0}')), 1)
        record = cast(dict[str, object], json.loads(output.getvalue().splitlines()[0].removeprefix("pushover: reviewed JSON: ")))
        path = self.marker()
        marker = cast(Marker, json.loads(path.read_text()))
        marker["held_source"] = cast(str, record["held_source"])
        marker["held_identity"] = cast(str, record["held_identity"])
        marker["held_machine"] = ""
        _ = path.write_text(json.dumps(marker))
        self.assertIsInstance(review.held_for(draft.source), review.LiveHold)
        self.assertEqual(self.call(), ("", ""))
        self.assertIsInstance(review.held_for(draft.source), review.NoHold)
        with mock.patch.object(pushover, "http_post", side_effect=AssertionError("real post")):
            self.assertEqual(review.due(post=lambda _u, _d: self.fail("expired duplicate"), keys_file=keys), 0)

    def test_remote_revision_fallback_releases_the_hold_on_its_machine(self) -> None:
        """F004: remote holds are released where they were written."""
        path = self.marker()
        marker = cast(Marker, json.loads(path.read_text()))
        marker["held_source"], marker["held_machine"] = "writer source", "natedev"
        marker["held_identity"] = "original-hold-hash"
        _ = path.write_text(json.dumps(marker))
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(self.call(), ("", ""))
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[-2], "natedev")
        self.assertIn("review.py release --identity original-hold-hash -- 'writer source'", command[-1])
        self.assertFalse(path.exists())

    def test_hold_release_error_still_allows_and_reports_the_error(self) -> None:
        """F004: a release fault follows the hook's allow-on-error contract."""
        path = self.marker()
        marker = cast(Marker, json.loads(path.read_text()))
        marker["held_source"] = "writer source"
        marker["held_identity"] = "original-hold-hash"
        _ = path.write_text(json.dumps(marker))
        with mock.patch.object(review, "release_if_matches", side_effect=OSError("cannot release")):
            output, errors = self.call()
        self.assertEqual(output, "")
        self.assertIn("cannot release", errors)

    def remote_release(self, argv: list[str], *, capture_output: bool, text: bool,
                       check: bool, timeout: int) -> subprocess.CompletedProcess[str]:
        del capture_output, text, check, timeout
        # Replace SSH with the real remote CLI parser, using only temporary state.
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(review.main(shlex.split(argv[-1])[2:]), 0)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_old_revision_fallback_preserves_a_replacement_hold_locally_and_remotely(self) -> None:
        """F004: cleanup identifies the restored hold, not just its source."""
        draft = review.Draft("writer source", "1", "Build choice", ActionRequired("Choose retry."),
                             "The build stopped.", True)
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")
        arguments = ["--source", draft.source, "--priority", "1", "--answerable",
                     "--action", "Choose retry.", draft.title, draft.body]
        for machine in ("", "natedev"):
            with (self.subTest(machine=machine), mock.patch.object(review, "schedule"),
                  mock.patch.object(review, "review", return_value=review.Approved(draft)),
                  mock.patch.object(pushover, "LOG_FILE", self.root / "push.jsonl"),
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO())):
                review.hold(review.Held(draft.source, 1000, draft, draft, ["Which build?"]))
                self.assertEqual(pushover.send(arguments, keys, lambda _u, _d: (500, b'{"status":0}')), 1)
                record = cast(dict[str, object], json.loads(output.getvalue().splitlines()[0].removeprefix("pushover: reviewed JSON: ")))
                path = self.marker()
                marker = cast(Marker, json.loads(path.read_text()))
                marker["held_source"] = cast(str, record["held_source"])
                marker["held_identity"] = cast(str, record["held_identity"])
                marker["held_machine"] = machine
                _ = path.write_text(json.dumps(marker))
                self.assertEqual(pushover.send(arguments, keys, lambda _u, _d: (200, b'{"status":1}')), 0)
                replacement = review.Held(draft.source, 2000, draft, draft, ["Which terminal?"])
                review.hold(replacement)
                with mock.patch.object(subprocess, "run", side_effect=self.remote_release) as run:
                    self.assertEqual(self.call(), ("", ""))
                self.assertEqual(run.call_count, int(bool(machine)))
                self.assertFalse(path.exists())
                found = review.held_for(draft.source)
                self.assertIsInstance(found, review.LiveHold)
                assert isinstance(found, review.LiveHold)
                self.assertEqual(found.hold, replacement)
                review.release(draft.source)

    def test_remote_release_accepts_a_source_starting_with_a_dash(self) -> None:
        """F009: the SSH command passes the source after the option separator."""
        draft = review.Draft("--writer", "1", "Build choice", ActionRequired("Choose retry."),
                             "The build stopped.", True)
        held = review.Held(draft.source, 1000, draft, draft, ["Which build?"])
        with mock.patch.object(review, "schedule"):
            review.hold(held)
        path = self.marker()
        marker = cast(Marker, json.loads(path.read_text()))
        marker["held_source"] = draft.source
        marker["held_identity"] = review.hold_identity(held)
        marker["held_machine"] = "natedev"
        _ = path.write_text(json.dumps(marker))
        with mock.patch.object(subprocess, "run", side_effect=self.remote_release) as run:
            self.assertEqual(self.call(), ("", ""))
        self.assertIn(" -- --writer", cast(list[str], run.call_args.args[0])[-1])
        self.assertIsInstance(review.held_for(draft.source), review.NoHold)
        self.assertFalse(path.exists())

    def test_hook_errors_allow_and_report_the_error(self) -> None:
        for raw in ("bad JSON", "[]", '{"tool_name":"PushNotification"}'):
            output, errors = self.call(raw=raw)
            self.assertEqual(output, "")
            self.assertIn("push gate:", errors)
        path = self.marker()
        _ = path.write_text("bad JSON")
        output, errors = self.call()
        self.assertEqual(output, "")
        self.assertIn("JSONDecodeError", errors)
        self.assertTrue(path.exists())
        _ = self.marker()
        with mock.patch.object(Path, "unlink", side_effect=OSError("cannot consume")):
            output, errors = self.call()
        self.assertEqual(output, "")
        self.assertIn("cannot consume", errors)
        for session in ("../writer", "..", ""):
            output, errors = self.call(session=session)
            self.assertEqual(output, "")
            self.assertIn("invalid session_id", errors)


if __name__ == "__main__":
    _ = unittest.main()
