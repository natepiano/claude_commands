"""Codex hooks give queued messages to the native session before they remove rows."""

from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override
from unittest import main, mock

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import send
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase


HOOK = Path(__file__).with_name("codex-queue-drain.py")
HOOK_MAIN = cast(Callable[[], int], runpy.run_path(str(HOOK))["main"])
THREAD = "00000000-0000-4000-8000-000000000011"
SENDER = "00000000-0000-4000-8000-000000000012"


class CodexQueueDrainTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"), "CODEX_THREAD_ID": "inherited-thread",
            "CLAUDE_CODE_SESSION_ID": "", "HERDR_BIN": "/nonexistent/herdr",
            "CONF_DIR": str(HOOK.parents[2] / "config"),
        })))
        _ = self.enterContext(mock.patch.object(send, "STATE", self.root / "state/message"))
        member = staff.StaffRow(staff.StaffId(THREAD), "producer", "Agent", staff.StaffRoot(),
                               current_session=staff.StaffId(THREAD))
        _ = self.write_document({"schema": 2, "rows": [member.to_json()]})

    def keep(self, thread: str, text: str) -> Path:
        send.enqueue(send.Message(f"session:{thread}", "builder", "Review", text, text,
                                  "unit · builder", stamp=send.CodexSender(SENDER)),
                     "no live session", datetime(2026, 11, 7, tzinfo=timezone.utc),
                     recipient=send.StableRecipient(f"staff:{thread}"), order=send.QueueOrder(1))
        return send.queue_path(f"session-{thread}")

    def invoke(self, event: str) -> str:
        payload = {"hook_event_name": event, "session_id": THREAD}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(HOOK_MAIN(), 0)
        return output.getvalue()

    def process_hook(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HOOK)],
                              input=json.dumps({"hook_event_name": "PostToolUse", "session_id": THREAD}),
                              env=dict(os.environ), capture_output=True, text=True, check=False,
                              timeout=TEST_HANG_GUARD_SECS)

    def test_both_events_use_the_payload_identity_and_keep_the_sender_stamp(self) -> None:
        inherited = self.keep("inherited-thread", "Keep the parent's message.")
        before = inherited.read_bytes()
        for event in ("SessionStart", "PostToolUse"):
            with self.subTest(event=event):
                _ = self.keep(THREAD, "Read the review.")
                output = cast(dict[str, dict[str, str]], json.loads(self.invoke(event)))
                context = output["hookSpecificOutput"]
                self.assertEqual(context["hookEventName"], event)
                self.assertIn("Read the review.", context["additionalContext"])
                self.assertIn("Sender native id: codex:" + SENDER, context["additionalContext"])
                self.assertIn("Shown sender: unit · builder", context["additionalContext"])
                self.assertNotIn("Keep the parent's message.", context["additionalContext"])
                self.assertEqual(inherited.read_bytes(), before)
                self.assertEqual(self.invoke(event), "")

    def test_read_or_output_failure_keeps_every_message_for_a_retry(self) -> None:
        for operation in ("write", "flush", "read_text"):
            with self.subTest(operation=operation):
                queue = self.keep(THREAD, "First review.")
                _ = self.keep(THREAD, "Second review.")
                other = self.keep("inherited-thread", "Another review.")
                before = {path: path.read_bytes() for path in (queue, other)}
                payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": THREAD})
                output = io.StringIO()
                with (mock.patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(output),
                      mock.patch.object(Path if operation == "read_text" else output, operation,
                                        side_effect=PermissionError("read or output unavailable")),
                      self.assertRaises(OSError)):
                    _ = HOOK_MAIN()
                self.assertEqual({path: path.read_bytes() for path in before}, before)
                retried = self.invoke("PostToolUse")
                self.assertEqual(retried.count("First review."), 1)
                self.assertEqual(retried.count("Second review."), 1)
                self.assertEqual(self.invoke("PostToolUse"), "")

    def test_concurrent_hook_processes_consume_the_queue_once(self) -> None:
        _ = self.keep(THREAD, "One review.")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(self.process_hook), pool.submit(self.process_hook))
            results = [future.result() for future in futures]
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        delivered = [result.stdout for result in results if result.stdout]
        self.assertEqual(len(delivered), 1)
        output = cast(dict[str, dict[str, str]], json.loads(delivered[0]))
        self.assertEqual(output["hookSpecificOutput"]["additionalContext"].count("One review."), 1)
        self.assertEqual(self.invoke("PostToolUse"), "")


if __name__ == "__main__":
    _ = main()
