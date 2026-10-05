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
                binary_path: str = "", launched_as: str = "example", include_path: bool = True,
                include_working_directory: bool = True) -> str:
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
    if not include_path:
        _ = cast(Record, result["parameters"]).pop("path")
    if not include_working_directory:
        _ = cast(Record, result["metadata"]).pop("working_directory")
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
        previous_transcripts = os.environ.get("BUILDLOG_TRANSCRIPTS")
        os.environ["BUILDLOG_TRANSCRIPTS"] = str(transcripts)

        def restore_transcripts() -> None:
            if previous_transcripts is None:
                _ = os.environ.pop("BUILDLOG_TRANSCRIPTS", None)
            else:
                os.environ["BUILDLOG_TRANSCRIPTS"] = previous_transcripts

        self.addCleanup(restore_transcripts)
        self.transcript = transcripts / "project" / "session-one.jsonl"
        self.transcript.parent.mkdir(parents=True)
        repository = base / "repository"
        _ = subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repository)], check=True)
        _ = subprocess.run([
            "git", "-C", str(repository), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "--quiet", "--allow-empty", "-m", "initial",
        ], check=True)
        self.worktree = base / "real-worktree"
        _ = subprocess.run([
            "git", "-C", str(repository), "worktree", "add", "--quiet", "-b", "fixture",
            str(self.worktree),
        ], check=True)
        (self.worktree / "crates" / "app").mkdir(parents=True)
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

    def save_phase_nine_state(self) -> None:
        raw = self.transcript.read_bytes()
        self.root.mkdir(parents=True, exist_ok=True)
        state = {
            str(self.transcript): {
                "offset": len(raw),
                "head": raw[:launches.MARK_BYTES].hex(),
                "tail": raw[max(0, len(raw) - launches.MARK_BYTES):].hex(),
            },
        }
        _ = (self.root / launches.STATE_NAME).write_text(json.dumps(state))

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

    def test_pathless_launch_uses_working_directory_worktree_once(self) -> None:
        _ = self.transcript.write_text(result_line(
            "session-one", "font_features", self.worktree, include_path=False,
        ))

        first = self.run_cli("launches")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(self.records()), 1)
        launch = self.records()[0]
        self.assertEqual(launch["worktree"], str(self.worktree))
        self.assertEqual(launch["repo_path"], str(self.worktree.parent / "repository"))
        self.assertEqual(launch["cwd"], str(self.worktree / "crates" / "app"))
        self.assertEqual(launch["branch"], "fixture")
        self.assertIsInstance(launch["sha"], str)

        second = self.run_cli("launches")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_path_parameter_takes_priority_over_working_directory(self) -> None:
        line = cast(Record, json.loads(result_line("session-one", "font_features", self.worktree)))
        repository = self.worktree.parent / "repository"
        result = cast(Record, cast(Record, line["mcpMeta"])["structuredContent"])
        cast(Record, result["parameters"])["path"] = str(repository)
        line["toolUseResult"] = json.dumps(result)
        _ = self.transcript.write_text(json.dumps(line) + "\n")

        collected = self.run_cli("launches")

        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertEqual(self.records()[0]["worktree"], str(repository))

    def test_launch_record_names_each_outcome_and_collector_counts_them(self) -> None:
        recorded = cast(Record, json.loads(result_line(
            "session-one", "font_features", self.worktree, include_path=False,
        )))
        no_location = cast(Record, json.loads(result_line(
            "session-one", "sizes", self.worktree, include_path=False,
            include_working_directory=False,
        )))
        not_a_launch = cast(Record, json.loads(result_line(
            "session-one", "other", self.worktree, tool="other_tool",
        )))
        not_a_launch["note"] = "brp_launch"
        no_result = cast(Record, json.loads(result_line(
            "session-one", "incomplete", self.worktree,
        )))
        incomplete = cast(Record, cast(Record, no_result["mcpMeta"])["structuredContent"])
        _ = cast(Record, incomplete["metadata"]).pop("launch_duration_ms")
        outcome = launches.launch_record(recorded, self.transcript)
        self.assertIsInstance(outcome, launches.RecordedLaunch)
        self.assertEqual(cast(launches.RecordedLaunch, outcome).outcome, launches.LaunchOutcome.RECORDED)
        self.assertEqual(launches.launch_record(no_location, self.transcript),
                         launches.LaunchOutcome.NO_LOCATION)
        self.assertEqual(launches.launch_record(not_a_launch, self.transcript),
                         launches.LaunchOutcome.NOT_A_LAUNCH)
        self.assertEqual(launches.launch_record(no_result, self.transcript),
                         launches.LaunchOutcome.NO_RESULT)
        _ = self.transcript.write_text("".join(
            json.dumps(line, separators=(",", ":")) + "\n"
            for line in (recorded, no_location, not_a_launch, no_result)
        ))

        counts = launches.collect()

        self.assertEqual(counts.recorded, 1)
        self.assertEqual(counts.not_a_launch, 1)
        self.assertEqual(counts.no_result, 1)
        self.assertEqual(counts.no_location, 1)
        self.assertEqual(counts.unresolved, 0)
        self.assertEqual(counts.added, 1)
        self.assertEqual(len(self.records()), 1)

    def test_removed_worktree_launch_is_recorded_with_unknown_location(self) -> None:
        _ = self.transcript.write_text(result_line(
            "session-one", "font_features", self.worktree, include_path=False,
        ))
        _ = subprocess.run([
            "git", "-C", str(self.worktree.parent / "repository"), "worktree", "remove", "--force",
            str(self.worktree),
        ], check=True)

        counts = launches.collect()

        self.assertEqual(counts.recorded, 1)
        self.assertEqual(counts.unresolved, 1)
        self.assertEqual(counts.added, 1)
        self.assertEqual(len(self.records()), 1)
        launch = self.records()[0]
        self.assertIsNone(launch["worktree"])
        self.assertIsNone(launch["repo_path"])
        self.assertIsNone(launch["branch"])
        self.assertIsNone(launch["sha"])

    def test_phase_nine_state_revisits_pathless_launch_once(self) -> None:
        initial = result_line("session-one", "font_features", self.worktree)
        missed = result_line(
            "session-one", "sizes", self.worktree, ended_at="2026-10-03T16:54:57+00:00",
            include_path=False,
        )
        _ = self.transcript.write_text(initial)
        initial_run = self.run_cli("launches")
        self.assertEqual(initial_run.returncode, 0, initial_run.stderr)
        self.assertEqual(len(self.records()), 1)
        _ = self.transcript.write_text(initial + missed)
        self.save_phase_nine_state()

        replay = self.run_cli("launches")

        self.assertEqual(replay.returncode, 0, replay.stderr)
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual(len({row["id"] for row in records}), 2)
        state = cast(Record, json.loads((self.root / launches.STATE_NAME).read_text()))
        self.assertIs(cast(Record, state[str(self.transcript)])["reread_complete"], True)

        repeat = self.run_cli("launches")
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(len(self.records()), 2)

    def test_interrupted_phase_nine_replay_keeps_one_record(self) -> None:
        _ = self.transcript.write_text(result_line(
            "session-one", "font_features", self.worktree, include_path=False,
        ))
        self.save_phase_nine_state()

        first = self.run_cli("launches")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(self.records()), 1)
        self.save_phase_nine_state()

        resumed = self.run_cli("launches")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(len(self.records()), 1)
        state = cast(Record, json.loads((self.root / launches.STATE_NAME).read_text()))
        self.assertIs(cast(Record, state[str(self.transcript)])["reread_complete"], True)

    def test_report_collects_launch_in_same_invocation(self) -> None:
        _ = self.transcript.write_text(result_line("session-one", "sizes", self.worktree))
        day = datetime.fromisoformat(ENDED).astimezone().date().isoformat()

        result = self.run_cli("report", day)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("example launches (brp)", result.stdout)
        self.assertEqual(len(self.records()), 1)

    def test_launches_prints_fixture_counts(self) -> None:
        recorded = result_line("session-one", "sizes", self.worktree)
        other = cast(Record, json.loads(result_line(
            "session-one", "other", self.worktree, tool="other_tool",
        )))
        other["note"] = "brp_launch"
        _ = self.transcript.write_text(recorded + json.dumps(other, separators=(",", ":")) + "\n")

        result = self.run_cli("launches")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, (
            "buildlog launches: 1 launches found: 1 recorded "
            "(1 new, 0 with an unknown worktree), 0 without a location; "
            "skipped 0 results without launch facts and 1 other tool results\n"
        ))

    def test_report_prints_launch_counts_on_stderr_only(self) -> None:
        _ = self.transcript.write_text(result_line("session-one", "sizes", self.worktree))
        day = datetime.fromisoformat(ENDED).astimezone().date().isoformat()

        result = self.run_cli("report", day)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("buildlog launches: 1 launches found: 1 recorded ", result.stderr)
        self.assertIn("(1 new, 0 with an unknown worktree), 0 without a location;", result.stderr)
        self.assertNotIn("buildlog launches:", result.stdout)
        self.assertIn("example launches (brp)", result.stdout)

    def test_collector_counts_distinct_launch_results_only(self) -> None:
        result = cast(Record, json.loads(result_line("session-one", "sizes", self.worktree)))
        result_body = cast(Record, cast(Record, result["mcpMeta"])["structuredContent"])
        call: Record = {"type": "assistant", "message": {"content": "brp_launch"}}
        notification: Record = {
            "sessionId": "session-one",
            "message": {"content": (
                "<task-notification><result>" + json.dumps(result_body)
                + "</result></task-notification>"
            )},
        }
        other = cast(Record, json.loads(result_line(
            "session-one", "other", self.worktree, tool="other_tool",
        )))
        other["note"] = "brp_launch"
        unrelated = cast(Record, json.loads(result_line(
            "session-one", "unrelated", self.worktree, tool="world_query",
        )))
        plain: Record = {"type": "assistant", "message": {"content": "nothing relevant"}}
        _ = self.transcript.write_text("".join(
            json.dumps(line, separators=(",", ":")) + "\n"
            for line in (call, result, notification, other, unrelated, plain)
        ))

        counts = launches.collect()

        self.assertEqual(counts.recorded, 1)
        self.assertEqual(counts.added, 1)
        self.assertEqual(counts.no_result, 0)
        self.assertEqual(counts.not_a_launch, 1)
        self.assertEqual(counts.unresolved, 0)

    def test_replay_blanks_old_branch_but_keeps_appended_branch(self) -> None:
        old = result_line("session-one", "sizes", self.worktree, include_path=False)
        new = result_line(
            "session-one", "font_features", self.worktree, include_path=False,
            ended_at="2026-10-03T16:54:57+00:00",
        )
        _ = self.transcript.write_text(old)
        self.save_phase_nine_state()
        with self.transcript.open("a") as handle:
            _ = handle.write(new)

        counts = launches.collect()

        self.assertEqual(counts.recorded, 2)
        records = {str(row["ended_at"]): row for row in self.records()}
        self.assertIsNone(records[ENDED]["branch"])
        self.assertIsNone(records[ENDED]["sha"])
        appended = records["2026-10-03T16:54:57+00:00"]
        self.assertEqual(appended["branch"], "fixture")
        self.assertIsInstance(appended["sha"], str)

    def test_path_outside_repository_has_unknown_worktree(self) -> None:
        outside = self.root.parent / "outside"
        outside.mkdir()
        line = cast(Record, json.loads(result_line("session-one", "sizes", self.worktree)))
        result = cast(Record, cast(Record, line["mcpMeta"])["structuredContent"])
        cast(Record, result["parameters"])["path"] = str(outside)
        _ = self.transcript.write_text(json.dumps(line) + "\n")

        counts = launches.collect()

        self.assertEqual(counts.recorded, 1)
        self.assertEqual(counts.added, 1)
        self.assertEqual(counts.unresolved, 1)
        self.assertIsNone(self.records()[0]["worktree"])
        self.assertIsNone(self.records()[0]["repo_path"])

    def test_path_inside_worktree_records_top_level(self) -> None:
        line = cast(Record, json.loads(result_line("session-one", "sizes", self.worktree)))
        result = cast(Record, cast(Record, line["mcpMeta"])["structuredContent"])
        cast(Record, result["parameters"])["path"] = str(self.worktree / "crates" / "app")
        _ = self.transcript.write_text(json.dumps(line) + "\n")

        counts = launches.collect()

        self.assertEqual(counts.unresolved, 0)
        self.assertEqual(self.records()[0]["worktree"], str(self.worktree))
        self.assertEqual(self.records()[0]["repo_path"], str(self.worktree.parent / "repository"))

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
        self.assertIn("1 launches found: 1 recorded (1 new", first.stdout)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["ended_at"], ended_at)
        self.assertAlmostEqual(float(str(records[0]["duration_s"])), 1468.425)
        self.assertEqual(records[0]["argv"], [
            "cargo", "build", "--workspace", "--example", "font_features", "--message-format=json",
        ])

        second = self.run_cli("launches")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("0 launches found: 0 recorded (0 new", second.stdout)
        self.assertEqual(len(self.records()), 1)


if __name__ == "__main__":
    _ = unittest.main()
