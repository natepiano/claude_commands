#!/usr/bin/env python3
"""Tests for the shared process-table snapshot and Codex-agent classification."""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import process_table


MESH = "/run/current-system/sw/bin/python3 /home/u/.claude/scripts/delegate/../agents/codex_mesh.py"


class ProcessTableTests(unittest.TestCase):
    def test_snapshot_reads_the_configured_process_table(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        listing = folder / "ps"
        _ = listing.write_text(
            "10 1 claude claude --resume session\n"
            + "20 10 python3 python3 /tools/worker.py --task one two\n"
            + "30 10 sleep\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"PROCESS_TABLE_PS": f"cat {listing}"}):
            _ = importlib.reload(process_table)
            found: dict[int, process_table.Process] = process_table.snapshot()
        _ = importlib.reload(process_table)

        self.assertEqual(found, {
            10: process_table.Process(10, 1, "claude", "claude --resume session"),
            20: process_table.Process(20, 10, "python3", "python3 /tools/worker.py --task one two"),
            30: process_table.Process(30, 10, "sleep", ""),
        })

    def test_a_mesh_turn_and_a_plain_exec_are_agents(self) -> None:
        self.assertTrue(process_table.is_codex_agent(
            process_table.Process(1, 0, "python3", f"{MESH} start --name a"),
        ))
        self.assertTrue(process_table.is_codex_agent(
            process_table.Process(1, 0, "python3", f"{MESH} follow --to a"),
        ))
        self.assertTrue(process_table.is_codex_agent(
            process_table.Process(1, 0, "codex", "codex exec --json"),
        ))

    def test_servers_wrappers_and_claude_seats_are_not_agents(self) -> None:
        for command, arguments in (
            ("codex", "codex app-server --listen ws://127.0.0.1:1"),
            ("zsh", f"zsh -c {MESH} start --name a"),
            ("python3", f"{MESH} can-follow --session-dir d"),
            ("bash", "bash /home/u/.claude/scripts/agents/agent_bg.sh a"),
        ):
            with self.subTest(arguments=arguments):
                self.assertFalse(process_table.is_codex_agent(
                    process_table.Process(1, 0, command, arguments),
                ))


if __name__ == "__main__":
    _ = unittest.main()
