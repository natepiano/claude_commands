"""Claude launcher follow integration coverage."""

from __future__ import annotations

import unittest
from scripts.direct.implement_launcher_claude_test_support import ClaudeLauncherFixture


class ImplementLauncherWorkerTests(ClaudeLauncherFixture):
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
