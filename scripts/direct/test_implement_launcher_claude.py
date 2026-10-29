"""test implement launcher claude integration coverage."""

from __future__ import annotations

import unittest
from scripts.direct import implement_launcher_test_support as support
from scripts.accounts import accounts
from typing import cast
import json
import os
from unittest.mock import patch


class ImplementLauncherWorkerTests(support.ImplementLauncherFixture):
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

    def test_claude_repair_in_the_impl_slot_keeps_a_distinct_worker_identity(self) -> None:
        run = self.start_phase("claude-repair-identity")
        first = self.claude_launch(run, "sonnet:low")
        self.assertEqual(first.returncode, 0, first.stderr)
        original = (run / "impl_worker_impl.json").read_bytes()
        self.open_repair_round(run)
        repair = self.claude_launch(run, "sonnet:low", kind="fix")
        self.assertEqual(repair.returncode, 0, repair.stderr)
        self.assertEqual((run / "impl_worker_impl.json").read_bytes(), original)
        identity = cast(dict[str, object], json.loads((run / "impl_worker_fix.json").read_text()))
        original_identity = cast(dict[str, object], json.loads(original))
        self.assertNotEqual(identity["name"], original_identity["name"])
        self.assertEqual(identity["family"], "claude")
        self.assertFalse((run / "mesh_roster.json").exists())

    def test_claude_worker_gone_after_delivery_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-gone")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "gone")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")

    def test_claude_written_summary_is_preserved_byte_for_byte(self) -> None:
        session_dir = self.start_phase("own-summary")
        original = b"Worker summary\r\n\x1b[31mcolour\x1b[0m\n"
        _ = (self.root / "own_summary.bin").write_bytes(original)
        _ = (self.root / "pane.bin").write_bytes(b"Other pane output\n")

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "impl_summary_impl.txt").read_bytes(), original)

    def test_copied_claude_launcher_keeps_legacy_home_then_uses_showrunners_after_cutover(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            self.assertEqual(accounts.setup("claude2"), [])
            _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": "worker@example.invalid"},
            }))
            groups = accounts.groups_file()
            groups.parent.mkdir(parents=True, exist_ok=True)
            _ = groups.write_text(json.dumps({"showrunners": "claude2"}))
        for cutover in (False, True):
            with self.subTest(cutover=cutover):
                if cutover:
                    _ = (self.root / ".claude.json").write_text("{}")
                session_dir = self.start_phase(f"copied-account-{cutover}")

                result = self.claude_launch(session_dir, "sonnet:low")

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((self.root / "launch_environment.json").read_text()), {
                    "CLAUDE_CONFIG_DIR": str(self.root / ".claude2") if cutover else "",
                    "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.root / ".claude") if cutover else "",
                })

    def test_new_claude_worker_uses_registry_effort(self) -> None:
        for row, expected_effort in (("sonnet:low", "low"), ("sonnet", "")):
            with self.subTest(row=row):
                session_dir = self.start_phase(f"effort-{row.replace(':', '-')}")
                result = self.claude_launch(
                    session_dir, row, inherited_effort="xhigh"
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = cast("list[str]", json.loads(
                    (self.root / "launch_args.json").read_text(encoding="utf-8")
                ))
                self.assertEqual(arguments[arguments.index("--model") + 1], "sonnet")
                if expected_effort:
                    self.assertEqual(
                        arguments[arguments.index("--effort") + 1], expected_effort
                    )
                else:
                    self.assertNotIn("--effort", arguments)
                self.assertEqual(
                    (session_dir / "impl_bg_id_impl").read_text(encoding="utf-8"),
                    "abc12345\n",
                )
                self.assertIn(
                    "abc12345\t", (session_dir / "workers").read_text(encoding="utf-8")
                )


if __name__ == "__main__":
    _ = unittest.main()
