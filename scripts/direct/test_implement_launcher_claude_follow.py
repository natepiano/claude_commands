"""Claude launcher follow integration coverage."""

from __future__ import annotations

import unittest
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
from scripts.usage import agents_log
from scripts.direct.implement_launcher_claude_test_support import ClaudeLauncherFixture


class ImplementLauncherWorkerTests(ClaudeLauncherFixture):
    def test_claude_follow_records_the_dispatch_role_before_delivery(self) -> None:
        session_dir = self.start_phase("claude-role-change")
        transcript = self.claude_worker(session_dir)
        sender = self.root / "send"
        original = sender.read_text()
        probe = "\n".join((
            "import json, os, sys",
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})",
            "from pathlib import Path",
            "from scripts.agents import codex_mesh",
            "from scripts.direct import workers",
            "from scripts.production import staff",
            "identity = 'abc12345-2222-4333-8444-555555555555'",
            "member = staff.by_current_session(identity)",
            "assert member is not None",
            "role = workers._worker_task_role({'staff_id': member.id}, staff.local_snapshot(), codex_mesh.task_assignments())",
            f"Path({str(self.root / 'delivered-role.json')!r}).write_text(json.dumps(role))",
        ))
        _ = sender.write_text(original.replace("import sys", probe + "\nimport sys", 1))
        with patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.root / "usage")}):
            agents_log.append("abc12345-2222-4333-8444-555555555555", "fix", None, datetime.now(timezone.utc))
            result = self.claude_follow(session_dir, transcript, "complete")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.root / "delivered-role.json").read_text()), "impl")
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")

    def test_claude_follow_counts_turn_completed_before_first_poll(self) -> None:
        session_dir = self.start_phase("claude-short-turn")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "complete")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")
        self.assertIn("impl_summary_impl.txt", (
            self.root / "delivered_message.txt"
        ).read_text(encoding="utf-8"))

    def test_claude_follow_retries_unreadable_agents_listing(self) -> None:
        session_dir = self.start_phase("claude-unreadable")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "unreadable_once")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")

    def test_claude_queued_send_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-queued")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "queued")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")
        self.assertIn("blocked:", (
            session_dir / "board.log"
        ).read_text(encoding="utf-8"))

    def test_claude_worker_gone_after_delivery_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-gone")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "gone")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")


if __name__ == "__main__":
    _ = unittest.main()
