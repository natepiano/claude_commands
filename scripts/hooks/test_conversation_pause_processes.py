"""Conversation-pause alarms, executable exits, and concurrent processes."""

from __future__ import annotations

import fcntl
from pathlib import Path
import subprocess
import sys
import unittest
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

from scripts.hooks.conversation_pause_test_support import (
    LIBRARY, PROMPT_HOOK, QUESTION, TEST_HANG_GUARD_SECS, ConversationPauseFixture,
    asked_read, cross_session,
)


class ConversationPauseProcessTests(ConversationPauseFixture):
    run_in_subprocess: ClassVar[bool] = True

    def test_prompt_hook_invalid_json_reports_one_line_and_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, str(PROMPT_HOOK)],
            input="not json",
            capture_output=True,
            text=True,
            check=False,
            env=self.environment,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("conversation-pause: JSONDecodeError:"))

    def test_prompt_hook_budget_expires_while_record_lock_is_held(self) -> None:
        _ = self.create_instance("unit-abc")
        self.pause_root.mkdir(parents=True)
        with (self.pause_root / ".lock").open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            result = self.run_prompt(
                "hello",
                extra_env={"CONVERSATION_PAUSE_HOOK_BUDGET": "1"},
                timeout=TEST_HANG_GUARD_SECS,
            )
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)

    def test_stop_hook_budget_expires_while_record_lock_is_held(self) -> None:
        _ = self.write_record({"kind": "replying", "user_wrote_at": 50})
        with (self.pause_root / ".lock").open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            result = self.run_stop(
                extra_env={"CONVERSATION_PAUSE_HOOK_BUDGET": "1"},
                timeout=TEST_HANG_GUARD_SECS,
            )
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)

    def test_prompt_hook_budget_expires_during_notifier_stop(self) -> None:
        instance = self.create_instance("unit-abc")
        result = self.run_prompt(
            "hello",
            extra_env={
                "CONVERSATION_PAUSE_HOOK_BUDGET": "1",
                "STOP_HOLD": "1",
            },
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_tick_releases_record_lock_during_session_lookup(self) -> None:
        marker = self.root / "lookup-started"
        release = self.root / "lookup-release"
        sessions = self.root / "sessions-slow.py"
        _ = sessions.write_text(
            "".join((
                "#!/usr/bin/env python3\n",
                "import os, sys\n",
                f"sys.path.insert(0, {str(LIBRARY.parents[2])!r})\n",
                "from scripts.direct.wait_for_test_state import wait_until\n",
                "from pathlib import Path\n",
                "Path(os.environ['LOOKUP_MARKER']).touch()\n",
                "release = Path(os.environ['LOOKUP_RELEASE'])\n",
                f"wait_until(release.exists, release.parent, {TEST_HANG_GUARD_SECS})\n",
                "print(os.environ['SESSION_SOCKET'])\n",
            ))
        )
        sessions.chmod(0o755)
        _ = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100}
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_SESSIONS": str(sessions),
            "CONVERSATION_PAUSE_NOW_EPOCH": "200",
            "LOOKUP_MARKER": str(marker),
            # The lookup holds until the lock is tried, so a loaded machine cannot finish it first.
            "LOOKUP_RELEASE": str(release),
        }
        process = subprocess.Popen(
            [sys.executable, str(LIBRARY), "tick"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            self.wait_for_path(marker)
            with (self.pause_root / ".lock").open("w") as lock_file:
                fcntl.flock(
                    lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            release.touch()
            _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, stderr)

    def test_prompt_before_send_return_stays_read_and_yes_resumes(self) -> None:
        instance = self.create_instance("unit-abc", enabled=False, paused=True)
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=(instance.name,),
        )

        tick = self.run_tick_with_prompt_during_send(
            cross_session("conversation-pause", QUESTION),
            now=150,
        )

        self.assertEqual(tick.returncode, 0, tick.stderr)
        self.assertEqual(self.record()["phase"], asked_read(150, 0))
        stopped = self.run_stop(now=160)
        self.assertEqual(
            (stopped.returncode, stopped.stdout, stopped.stderr),
            (0, "", ""),
        )
        self.assertEqual(self.record()["phase"], asked_read(150, 1))
        answer = self.parsed_reply(self.run_prompt("yes", now=170))
        self.assertEqual(
            answer["systemMessage"],
            "Automatic updates are back on: status reports.",
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_prompt_before_send_return_reserves_yes_after_newer_reply(self) -> None:
        instance = self.create_instance("unit-abc", enabled=False, paused=True)
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=(instance.name,),
        )
        tick = self.run_tick_with_prompt_during_send(
            cross_session("conversation-pause", QUESTION),
            now=150,
        )
        self.assertEqual(tick.returncode, 0, tick.stderr)

        for ended_at in (160, 170):
            stopped = self.run_stop(now=ended_at)
            self.assertEqual(
                (stopped.returncode, stopped.stdout, stopped.stderr),
                (0, "", ""),
            )
        asked = asked_read(150, 2)
        self.assertEqual(self.record()["phase"], asked)

        answer = self.run_prompt("yes", now=180)
        self.assertEqual(
            (answer.returncode, answer.stdout, answer.stderr),
            (0, "", ""),
        )
        self.assertEqual(self.record()["phase"], asked)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_delivery_does_not_overwrite_newer_replying_phase(self) -> None:
        marker = self.root / "send-started"
        release = self.root / "send-release"
        send = self.root / "send-slow.py"
        _ = send.write_text(
            "".join((
                "#!/usr/bin/env python3\n",
                "import os, sys\n",
                f"sys.path.insert(0, {str(LIBRARY.parents[2])!r})\n",
                "from scripts.direct.wait_for_test_state import wait_until\n",
                "from pathlib import Path\n",
                "Path(os.environ['SEND_MARKER']).touch()\n",
                "release = Path(os.environ['SEND_RELEASE'])\n",
                f"wait_until(release.exists, release.parent, {TEST_HANG_GUARD_SECS})\n",
                "print('SENT: delivered')\n",
            ))
        )
        send.chmod(0o755)
        instance = self.create_instance("unit-abc", enabled=False, paused=True)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=("unit-abc",),
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_SEND": str(send),
            "CONVERSATION_PAUSE_NOW_EPOCH": "150",
            "SEND_MARKER": str(marker),
            # The send holds until the typed prompt is done, however slowly a loaded machine runs it.
            "SEND_RELEASE": str(release),
        }
        process = subprocess.Popen(
            [sys.executable, str(LIBRARY), "tick"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            self.wait_for_path(marker)
            typed = self.run_prompt("new topic", now=160)
            release.touch()
            self.assertEqual(
                (typed.returncode, typed.stdout, typed.stderr), (0, "", "")
            )
            _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "replying", "user_wrote_at": 160}
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_missing_session_registry_keeps_pause_record_and_report_off(self) -> None:
        instance = self.create_instance("unit-abc", enabled=False, paused=True)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100},
            instances=(instance.name,),
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_NOW_EPOCH": "1000",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "missing-sessions"),
        }
        _ = environment.pop("CONVERSATION_PAUSE_SESSIONS")
        result = subprocess.run(
            [sys.executable, str(LIBRARY), "tick"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "question_pending", "due_at": 1_000}
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))


if __name__ == "__main__":
    _ = unittest.main()
