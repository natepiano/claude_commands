#!/usr/bin/env python3
"""Launch results in Claude transcripts become build steps once."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override

import launches
from test_index import Record, point_root_at

CLI = Path(__file__).with_name("cli.py")
ENDED = "2026-10-02T16:40:18+00:00"


def result_line(session: str, target: str, worktree: Path, *, duration_ms: int = 600_000,
                profile: str = "debug", tool: str = "brp_launch", ended_at: str = ENDED,
                binary_path: str = "", launched_as: str = "example") -> str:
    # The nested keys follow a real brp_launch transcript result from widget-examples.
    result: Record = {
        "status": "success",
        "call_info": {"mcp_tool": tool},
        "metadata": {
            "target_name": target,
            "working_directory": str(worktree / "crates" / "app"),
            "profile": profile,
            "launch_duration_ms": duration_ms,
            "launch_timestamp": ended_at,
            "workspace": "misleading-folder-name",
            "launched_as": launched_as,
        },
        "parameters": {"target_name": target, "path": str(worktree)},
    }
    if binary_path:
        cast(Record, result["metadata"])["binary_path"] = binary_path
    encoded = json.dumps(result)
    line = {
        "type": "user",
        "message": {"role": "user", "content": [{"tool_use_id": f"tool-{target}", "type": "tool_result", "content": encoded}]},
        "toolUseResult": encoded,
        "mcpMeta": {"structuredContent": result},
        "sessionId": session,
        "timestamp": ended_at,
    }
    return json.dumps(line, separators=(",", ":")) + "\n"


class LaunchTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    transcript: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    worktree: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = base / "buildlog"
        point_root_at(self, self.root)
        transcripts = base / "transcripts"
        self.transcript = transcripts / "project" / "session-one.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.worktree = base / "real-worktree"
        self.worktree.mkdir()
        self.environment = {
            **os.environ,
            "BUILDLOG_DIR": str(self.root),
            "BUILDLOG_TRANSCRIPTS": str(transcripts),
        }

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args], env=self.environment, capture_output=True,
            text=True, check=False, timeout=30,
        )

    def records(self) -> list[Record]:
        return [cast(Record, json.loads(line)) for path in self.root.glob("*/*.jsonl")
                for line in path.read_text().splitlines()]

    def test_launches_record_nested_result_and_only_new_complete_lines(self) -> None:
        first = result_line("session-one", "font_features", self.worktree, duration_ms=1_468_425)
        ignored = result_line("session-one", "other", self.worktree, tool="other_tool")
        _ = self.transcript.write_text(first + ignored)

        first_run = self.run_cli("launches")
        self.assertEqual(first_run.returncode, 0, first_run.stderr)
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["kind"], "step")
        self.assertEqual(record["step"], "build")
        self.assertEqual(record["caller"], "brp-launch")
        self.assertEqual(record["session"], "session-one")
        self.assertEqual(record["id"], f"session-one:{ENDED}")
        self.assertEqual(record["ended_at"], ENDED)
        self.assertEqual(datetime.fromisoformat(str(record["started_at"])),
                         datetime.fromisoformat(ENDED) - timedelta(milliseconds=1_468_425))
        self.assertAlmostEqual(float(str(record["duration_s"])), 1468.425)
        self.assertEqual(record["cwd"], str(self.worktree / "crates" / "app"))
        self.assertEqual(record["worktree"], str(self.worktree))
        self.assertEqual(record["argv"], ["cargo", "build", "--workspace", "--example", "font_features", "--message-format=json"])
        self.assertFalse(any("mem" in key for key in record))
        state = cast(dict[str, dict[str, object]], json.loads((self.root / "launches_state.json").read_text()))
        self.assertEqual(state[str(self.transcript)]["offset"], len((first + ignored).encode()))

        repeat = self.run_cli("launches")
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(len(self.records()), 1)

        second = result_line("session-one", "sizes", self.worktree, duration_ms=719_108,
                             profile="release", ended_at="2026-10-02T16:54:57+00:00")
        with self.transcript.open("a") as handle:
            _ = handle.write(second[:len(second) // 2])
        partial = self.run_cli("launches")
        self.assertEqual(partial.returncode, 0, partial.stderr)
        self.assertEqual(len(self.records()), 1)
        with self.transcript.open("a") as handle:
            _ = handle.write(second[len(second) // 2:])
        resumed = self.run_cli("launches")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["id"] for record in records}), 2)
        release = next(record for record in records if "sizes" in cast(list[str], record["argv"]))
        self.assertIn("--release", cast(list[str], release["argv"]))

    def test_report_collects_launch_in_same_invocation(self) -> None:
        _ = self.transcript.write_text(result_line("session-one", "sizes", self.worktree))
        day = datetime.fromisoformat(ENDED).astimezone().date().isoformat()

        result = self.run_cli("report", day)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("example launches (brp)", result.stdout)
        self.assertEqual(len(self.records()), 1)

    def test_app_launch_records_binary_target(self) -> None:
        app_binary = str(self.worktree / "target" / "debug" / "editor")
        _ = self.transcript.write_text(result_line(
            "session-one", "editor", self.worktree, binary_path=app_binary, launched_as="app",
        ))

        result = self.run_cli("launches")

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = cast(list[str], self.records()[0]["argv"])
        self.assertEqual(argv, ["cargo", "build", "--workspace", "--bin", "editor", "--message-format=json"])
        self.assertNotIn("--example", argv)

    def test_example_with_binary_path_records_example_target(self) -> None:
        example_binary = str(self.worktree / "target" / "debug" / "examples" / "font_features")
        _ = self.transcript.write_text(result_line(
            "session-one", "font_features", self.worktree,
            binary_path=example_binary, launched_as="example",
        ))

        result = self.run_cli("launches")

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = cast(list[str], self.records()[0]["argv"])
        self.assertEqual(argv, ["cargo", "build", "--workspace", "--example", "font_features", "--message-format=json"])

    def test_background_task_notification_records_launch_once(self) -> None:
        ended_at = "2026-10-04T23:40:18.006934801+00:00"
        launch: Record = {
            "status": "success",
            "message": "Successfully launched 1 instance(s) of font_features on ports 15741",
            "call_info": {"mcp_tool": "brp_launch"},
            "metadata": {
                "target_name": "font_features",
                "working_directory": str(self.worktree / "crates" / "hana_diegetic"),
                "profile": "debug",
                "binary_path": str(self.worktree / "target" / "debug" / "examples" / "font_features"),
                "launch_duration_ms": 1_468_425,
                "launch_timestamp": ended_at,
                "workspace": "widget-examples",
                "package_name": "hana_diegetic",
                "launched_as": "example",
            },
            "parameters": {
                "target_name": "font_features",
                "search_order": "example",
                "path": str(self.worktree),
                "package_name": "hana_diegetic",
                "port": 15741,
                "instance_count": 1,
            },
        }
        notification = (
            "<task-notification>\n"
            "<task-id>kgxfn6tr1</task-id>\n"
            "<status>completed</status>\n"
            "<summary>MCP task kgxfn6tr (brp/brp_launch) completed.</summary>\n"
            f"<result>\n{json.dumps(launch)}\n</result>\n"
            "</task-notification>"
        )
        user_line: Record = {
            "type": "user", "message": {"role": "user", "content": notification},
            "sessionId": "session-one",
        }
        queued_line: Record = {
            "type": "queue-operation", "operation": "enqueue", "content": notification,
            "sessionId": "session-one",
        }
        self.assertEqual(launches.result_from_line(user_line), launch)
        self.assertEqual(launches.result_from_line(queued_line), launch)
        _ = self.transcript.write_text(
            json.dumps(user_line, separators=(",", ":")) + "\n"
            + json.dumps(queued_line, separators=(",", ":")) + "\n"
        )

        first = self.run_cli("launches")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("1 added", first.stdout)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["ended_at"], ended_at)
        self.assertAlmostEqual(float(str(records[0]["duration_s"])), 1468.425)
        self.assertEqual(records[0]["argv"], [
            "cargo", "build", "--workspace", "--example", "font_features", "--message-format=json",
        ])

        second = self.run_cli("launches")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("0 added", second.stdout)
        self.assertEqual(len(self.records()), 1)


if __name__ == "__main__":
    _ = unittest.main()
