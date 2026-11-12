"""Claude pane summaries and launcher artifact coverage."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
import unittest
from scripts.direct import claude_worker
from scripts.direct.assign_claude_native_test_support import ClaudeLauncherFixture


WRITE_SUMMARY = cast(Callable[[claude_worker.ClaudeTurn], None], vars(claude_worker)["_summary"])


class AssignClaudeLauncherWorkerTests(ClaudeLauncherFixture):
    def summary_turn(self) -> claude_worker.ClaudeTurn:
        return claude_worker.ClaudeTurn(
            name="fixture-impl", run=self.root, cwd=self.working_dir, prompt=self.prompt_file,
            summary=self.root / "impl_summary_impl.txt", log=self.root / "impl_agent_impl.log",
            model="sonnet", effort="low", role="impl", staff_id="", workers=False, env={},
        )

    def test_claude_control_only_pane_uses_no_summary_line(self) -> None:
        turn = self.summary_turn()
        raw = b"\x1b[31m\x1b]0;Title\x07\x1b]1;Other\x1b\\\x1b7\r\b\x07"
        _ = turn.log.write_bytes(raw)

        WRITE_SUMMARY(turn)

        self.assertEqual(turn.log.read_bytes(), raw)
        summary = turn.summary.read_text(encoding="utf-8")
        self.assertEqual(
            summary, f"The background agent {turn.name} produced no summary.\n",
        )

    def test_claude_pane_fallback_keeps_newline_and_tab_after_a_stray_escape(self) -> None:
        turn = self.summary_turn()
        raw = "Before\x1b\nMiddle\x1b\tTail\x1b(Bend\n".encode("utf-8")
        _ = turn.log.write_bytes(raw)

        WRITE_SUMMARY(turn)

        self.assertEqual(turn.log.read_bytes(), raw)
        self.assertEqual(
            turn.summary.read_text(encoding="utf-8"),
            "Before\nMiddle\tTailend\n",
        )

    def test_claude_pane_fallback_strips_controls_before_character_cut(self) -> None:
        session_dir = self.start_phase("pane-cleanup")
        raw = (
            "é" * 4050 + "\x1b]0;Title\x07\x1b]1;Other\x1b\\"
            + "\x1b[31mVisible\r text\b\x1b[2K\x1b[0m\x1b7\nNext line\n"
        )
        _ = (self.root / "pane.bin").write_bytes(raw.encode("utf-8"))

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = (session_dir / "impl_summary_impl.txt").read_text(encoding="utf-8")
        expected = ("é" * 4050 + "Visible text\nNext line\n")[-4000:]
        self.assertEqual(summary, expected)
        self.assertEqual(len(summary), 4000)
        self.assertNotIn("\x1b", summary)
        self.assertNotIn("\r", summary)
        self.assertEqual((session_dir / "impl_agent_impl.log").read_bytes(), raw.encode("utf-8"))

    def test_claude_written_summary_is_preserved_byte_for_byte(self) -> None:
        turn = self.summary_turn()
        original = b"Worker summary\r\n\x1b[31mcolour\x1b[0m\n"
        _ = turn.summary.write_bytes(original)
        raw = b"Other pane output\n"
        _ = turn.log.write_bytes(raw)

        WRITE_SUMMARY(turn)

        self.assertEqual(turn.log.read_bytes(), raw)
        self.assertEqual(turn.summary.read_bytes(), original)


if __name__ == "__main__":
    _ = unittest.main()
