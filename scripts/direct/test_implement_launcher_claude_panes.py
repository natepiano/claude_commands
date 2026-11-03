"""Claude launcher panes integration coverage."""

from __future__ import annotations

import unittest
from scripts.direct.implement_launcher_claude_test_support import ClaudeLauncherFixture
from typing import cast
import json


class ImplementLauncherWorkerTests(ClaudeLauncherFixture):
    def test_claude_control_only_pane_uses_no_summary_line(self) -> None:
        session_dir = self.start_phase("pane-controls-only")
        _ = (self.root / "pane.bin").write_bytes(
            b"\x1b[31m\x1b]0;Title\x07\x1b]1;Other\x1b\\\x1b7\r\b\x07"
        )

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = (session_dir / "impl_summary_impl.txt").read_text(encoding="utf-8")
        arguments = cast("list[str]", json.loads(
            (self.root / "launch_args.json").read_text(encoding="utf-8")
        ))
        name = arguments[arguments.index("--name") + 1]
        self.assertEqual(
            summary, f"The background agent {name} produced no summary.\n",
        )

    def test_claude_pane_fallback_keeps_newline_and_tab_after_a_stray_escape(self) -> None:
        session_dir = self.start_phase("pane-stray-escape")
        _ = (self.root / "pane.bin").write_bytes(
            "Before\x1b\nMiddle\x1b\tTail\x1b(Bend\n".encode("utf-8")
        )

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "impl_summary_impl.txt").read_text(encoding="utf-8"),
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
        session_dir = self.start_phase("own-summary")
        original = b"Worker summary\r\n\x1b[31mcolour\x1b[0m\n"
        _ = (self.root / "own_summary.bin").write_bytes(original)
        _ = (self.root / "pane.bin").write_bytes(b"Other pane output\n")

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "impl_summary_impl.txt").read_bytes(), original)


if __name__ == "__main__":
    _ = unittest.main()
