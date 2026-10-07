"""Synthetic transcript tests for the director request extractor."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from model_study import extract
from turns import load_roster, read_session, resolve_session_files


class CacheCreation(TypedDict, total=False):
    ephemeral_5m_input_tokens: int
    ephemeral_1h_input_tokens: int


class OutputTokenDetails(TypedDict, total=False):
    thinking_tokens: int


class TranscriptUsage(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cache_creation: CacheCreation
    output_tokens_details: OutputTokenDetails
    speed: str


class ContentBlock(TypedDict):
    type: str
    content: NotRequired[str]
    text: NotRequired[str]
    input: NotRequired[dict[str, str]]


class TranscriptMessage(TypedDict):
    role: str
    content: str | list[ContentBlock]
    model: NotRequired[str]
    usage: NotRequired[TranscriptUsage]
    stop_reason: NotRequired[str]


class CompactMetadata(TypedDict):
    trigger: str
    preTokens: int
    postTokens: int
    durationMs: int


class TranscriptRecord(TypedDict):
    type: str
    timestamp: str
    message: NotRequired[TranscriptMessage]
    uuid: NotRequired[str]
    requestId: NotRequired[str]
    isSidechain: NotRequired[bool]
    turnOrigin: NotRequired[str]
    perTurnEffort: NotRequired[str]
    isCompactSummary: NotRequired[bool]
    subtype: NotRequired[str]
    compactMetadata: NotRequired[CompactMetadata]


class ExtractedTurn(TypedDict):
    request_id: str
    switch_turn: bool
    after_compact: int


class ExtractedSession(TypedDict):
    by_model: dict[str, int]
    dropped: dict[str, int]


class ExtractionResult(TypedDict):
    sessions: list[ExtractedSession]


def user(
    at: str,
    content: str | list[ContentBlock],
    *,
    origin: str | None = None,
    compact: bool = False,
) -> TranscriptRecord:
    record: TranscriptRecord = {
        "type": "user",
        "timestamp": at,
        "message": {"role": "user", "content": content},
    }
    if origin is not None:
        record["turnOrigin"] = origin
    if compact:
        record["isCompactSummary"] = True
    return record


def assistant(
    at: str,
    request_id: str,
    *,
    model: str = "claude-opus-4-1",
    usage: TranscriptUsage | None = None,
    effort: str | None = None,
    stop: str = "end_turn",
    sidechain: bool = False,
) -> TranscriptRecord:
    message: TranscriptMessage = {
        "role": "assistant",
        "content": [{"type": "text", "text": "private assistant reply"}],
        "model": model,
        "usage": usage or {},
        "stop_reason": stop,
    }
    record: TranscriptRecord = {
        "type": "assistant",
        "timestamp": at,
        "message": message,
        "requestId": request_id,
        "uuid": f"block-{request_id}-{at}",
    }
    if effort is not None:
        record["perTurnEffort"] = effort
    if sidechain:
        record["isSidechain"] = True
    return record


def write_transcript(directory: Path, records: list[TranscriptRecord], name: str = "session") -> Path:
    path = directory / f"{name}.jsonl"
    _ = path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


class RequestExtractionTests(unittest.TestCase):
    def test_multiblock_usage_triggers_kinds_and_private_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret_prompt = "SECRET_PROMPT_FOR_EXTRACTOR"
            secret_argument = "SECRET_TOOL_ARGUMENT_FOR_EXTRACTOR"
            secret_path = "/private/SECRET_TRANSCRIPT_PATH"
            records: list[TranscriptRecord] = [
                user("2026-10-06T23:00:00Z", secret_prompt, origin="human"),
                # File order alone cannot make this later-stamped attachment a trigger.
                {
                    "type": "attachment",
                    "timestamp": "2026-10-06T23:00:09Z",
                    "message": {"role": "attachment", "content": secret_path},
                },
                assistant(
                    "2026-10-06T23:00:02Z",
                    "first",
                    effort="high",
                    usage={
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 40,
                        "cache_creation_input_tokens": 10,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 3,
                            "ephemeral_1h_input_tokens": 4,
                        },
                        "output_tokens_details": {"thinking_tokens": 7},
                        "speed": "fast",
                    },
                    stop="tool_use",
                ),
                assistant(
                    "2026-10-06T23:00:06Z",
                    "first",
                    effort="high",
                    usage={
                        "input_tokens": 90,
                        "output_tokens": 30,
                        "cache_read_input_tokens": 35,
                        "cache_creation_input_tokens": 12,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 4,
                            "ephemeral_1h_input_tokens": 6,
                        },
                        "output_tokens_details": {"thinking_tokens": 9},
                        "speed": "fast",
                    },
                ),
                user(
                    "2026-10-06T23:00:10Z",
                    [{"type": "tool_result", "content": secret_argument}],
                ),
                assistant("2026-10-06T23:00:13Z", "second", stop="tool_use"),
                {
                    "type": "attachment",
                    "timestamp": "2026-10-06T23:00:20Z",
                    "message": {"role": "attachment", "content": secret_path},
                },
                assistant("2026-10-06T23:00:22Z", "third", model="claude-sonnet-4-5"),
                user("2026-10-06T23:00:30Z", "peer reply", origin="peer"),
                assistant("2026-10-06T23:00:31Z", "fourth"),
                user("2026-10-06T23:00:40Z", "notification", origin="task_notification"),
                assistant("2026-10-06T23:00:41Z", "fifth"),
                user("2026-10-06T23:00:50Z", [], origin="peer"),
                assistant("2026-10-06T23:00:51Z", "sixth"),
            ]
            result = read_session(write_transcript(directory, records), "fixture", None)

            self.assertEqual(len(result.turns), 6)
            first, second, third, fourth, fifth, sixth = result.turns
            self.assertEqual(first.seconds, 6.0)
            self.assertEqual(first.kind, "human")
            self.assertEqual(first.stop, "end_turn")
            self.assertEqual(first.effort, "high")
            self.assertEqual(first.speed, "fast")
            self.assertEqual(first.input, 100)
            self.assertEqual(first.output, 30)
            self.assertEqual(first.thinking, 9)
            self.assertEqual(first.cache_read, 40)
            self.assertEqual(first.write_1h, 6)
            self.assertEqual(first.write_5m, 6)
            self.assertEqual(first.context, 152)
            self.assertEqual(second.seconds, 3.0)
            self.assertEqual(second.kind, "continuation")
            self.assertEqual(second.stop, "tool_use")
            self.assertEqual(third.seconds, 2.0)
            self.assertEqual(fourth.kind, "peer")
            self.assertEqual(fifth.kind, "task_notification")
            self.assertEqual(sixth.kind, "other")  # Non-string user content.
            self.assertEqual(
                [turn.switch_turn for turn in result.turns],
                [True, False, True, True, False, False],
            )
            self.assertEqual(result.dropped.no_trigger, 0)
            serialized = json.dumps([turn.to_json() for turn in result.turns])
            for secret in (secret_prompt, secret_argument, secret_path):
                self.assertNotIn(secret, serialized)

    def test_director_start_precedence_and_whole_session_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cases: list[tuple[str, list[TranscriptRecord], str | None, list[str]]] = [
                (
                    "command",
                    [
                        user("2026-10-06T23:00:00Z", "setup", origin="human"),
                        assistant("2026-10-06T23:00:01Z", "before"),
                        user(
                            "2026-10-06T23:00:10Z",
                            "<command-name>/unit:delegate</command-name>",
                        ),
                        assistant("2026-10-06T23:00:11Z", "after"),
                    ],
                    None,
                    ["after"],
                ),
                (
                    "human",
                    [
                        user("2026-10-06T23:00:00Z", "setup", origin="human"),
                        assistant("2026-10-06T23:00:01Z", "before"),
                        user("2026-10-06T23:00:10Z", "run /unit:delegate", origin="human"),
                        assistant("2026-10-06T23:00:11Z", "after"),
                    ],
                    None,
                    ["after"],
                ),
                (
                    "roster",
                    [
                        user("2026-10-06T23:00:00Z", "setup", origin="human"),
                        assistant("2026-10-06T23:00:01Z", "before"),
                        user("2026-10-06T23:00:10Z", "continue", origin="human"),
                        assistant("2026-10-06T23:00:11Z", "after"),
                    ],
                    "2026-10-06T16:00:05",
                    ["after"],
                ),
                (
                    "whole",
                    [
                        user("2026-10-06T23:00:00Z", "hello", origin="human"),
                        assistant("2026-10-06T23:00:01Z", "first"),
                        user("2026-10-06T23:00:10Z", "next", origin="peer"),
                        assistant("2026-10-06T23:00:11Z", "second"),
                    ],
                    None,
                    ["first", "second"],
                ),
            ]
            for case_name, records, director_from, expected in cases:
                with self.subTest(case=case_name):
                    result = read_session(
                        write_transcript(directory, records, case_name), "fixture", director_from
                    )
                    self.assertEqual([turn.request_id for turn in result.turns], expected)
                    self.assertEqual(
                        result.dropped.before_director, len(records) // 2 - len(expected)
                    )

    def test_compaction_marks_ten_following_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            records: list[TranscriptRecord] = [
                user("2026-10-06T23:00:00Z", "start", origin="human"),
                assistant("2026-10-06T23:00:01Z", "before"),
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "timestamp": "2026-10-06T23:00:10Z",
                    "compactMetadata": {
                        "trigger": "manual",
                        "preTokens": 1000,
                        "postTokens": 200,
                        "durationMs": 125,
                    },
                },
            ]
            for index in range(11):
                minute = index + 1
                records.append(
                    user(
                        f"2026-10-06T23:{minute:02d}:00Z",
                        "summary" if index == 0 else "continue",
                        compact=index == 0,
                    )
                )
                records.append(
                    assistant(f"2026-10-06T23:{minute:02d}:01Z", f"after-{index}")
                )
            result = read_session(write_transcript(directory, records), "fixture", None)

            self.assertEqual(len(result.compactions), 1)
            compact = result.compactions[0]
            self.assertEqual(compact.trigger, "manual")
            self.assertEqual(compact.pre_tokens, 1000)
            self.assertEqual(compact.post_tokens, 200)
            self.assertEqual(compact.duration_ms, 125)
            self.assertEqual(compact.model, "claude-opus-4-1")
            self.assertEqual(result.turns[1].kind, "compact")
            self.assertEqual([turn.after_compact for turn in result.turns], [0, *range(1, 11), 0])

    def test_sidechain_and_invalid_gaps_keep_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            records = [
                assistant("2026-10-06T23:00:00Z", "orphan", usage={"output_tokens": 5}),
                assistant("2026-10-06T23:00:01Z", "side", sidechain=True),
                user("2026-10-06T23:00:10Z", "negative", origin="human"),
                assistant("2026-10-06T23:00:12Z", "backward", usage={"output_tokens": 6}),
                assistant("2026-10-06T23:00:09Z", "backward", usage={"output_tokens": 7}),
                user("2026-10-06T23:01:00Z", "long", origin="human"),
                assistant("2026-10-06T23:31:01Z", "late", usage={"output_tokens": 8}),
            ]
            result = read_session(write_transcript(directory, records), "fixture", None)

            self.assertEqual([turn.request_id for turn in result.turns], ["orphan", "backward", "late"])
            self.assertEqual([turn.seconds for turn in result.turns], [None, None, None])
            self.assertEqual([turn.output for turn in result.turns], [5, 7, 8])
            self.assertEqual(result.dropped.sidechain, 1)
            self.assertEqual(result.dropped.no_trigger, 1)
            self.assertEqual(result.dropped.negative, 1)
            self.assertEqual(result.dropped.over_limit, 1)

    def test_uuid_fallback_and_unknown_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            reply = assistant("2026-10-06T23:00:02Z", "unused", stop="max_tokens")
            del reply["requestId"]
            reply["uuid"] = "reply-uuid"
            records = [user("2026-10-06T23:00:00Z", "hello", origin="human"), reply]
            result = read_session(write_transcript(directory, records), "fixture", None)

            self.assertEqual(len(result.turns), 1)
            turn = result.turns[0]
            self.assertEqual(turn.request_id, "reply-uuid")
            self.assertEqual(turn.effort, "unknown")
            self.assertEqual(turn.speed, "unknown")
            self.assertEqual(turn.stop, "other")
            self.assertEqual(
                datetime.fromisoformat(turn.started),
                datetime(2026, 10, 6, 23, 0, 2, tzinfo=timezone.utc),
            )

    def test_final_block_with_max_tokens_has_other_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records = [
                user("2026-10-06T23:00:00Z", "start", origin="human"),
                assistant("2026-10-06T23:00:01Z", "one", stop="tool_use"),
                assistant("2026-10-06T23:00:02Z", "one", stop="max_tokens"),
            ]
            result = read_session(write_transcript(Path(temporary), records), "fixture", None)

            self.assertEqual(len(result.turns), 1)
            self.assertEqual(result.turns[0].stop, "other")

    def test_extract_switches_only_when_director_model_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / "projects" / "project-a"
            project.mkdir(parents=True)
            _ = write_transcript(project, [
                user("2026-10-06T23:00:10Z", "continue", origin="human"),
                assistant("2026-10-06T23:00:11Z", "later-opus"),
                user("2026-10-06T23:00:20Z", "switch", origin="human"),
                assistant("2026-10-06T23:00:21Z", "sonnet", model="claude-sonnet-4-5"),
            ], "roster-id")
            _ = write_transcript(project, [
                user("2026-10-06T23:00:00Z", "start", origin="human"),
                assistant("2026-10-06T23:00:01Z", "earlier-opus"),
            ], "registry-id")
            roster = directory / "roster.json"
            _ = roster.write_text(json.dumps([{
                "name": "fixture",
                "session_ids": ["roster-id"],
                "switched_by_pdt": None,
                "production": "build-followups",
                "director_from_pdt": None,
            }]), encoding="utf-8")
            registry = directory / "registry"
            registry.mkdir()
            _ = (registry / "director.json").write_text(
                json.dumps({"name": "fixture", "sessionId": "registry-id"}), encoding="utf-8"
            )
            state = directory / "state"
            with redirect_stdout(StringIO()):
                _ = extract(roster, directory / "projects", registry, state)

            turns = [
                cast(ExtractedTurn, json.loads(line))
                for line in (state / "turns.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [turn["request_id"] for turn in turns],
                ["earlier-opus", "later-opus", "sonnet"],
            )
            self.assertEqual([turn["switch_turn"] for turn in turns], [True, False, True])

    def test_extract_drops_synthetic_without_advancing_model_or_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / "projects" / "project-a"
            project.mkdir(parents=True)
            _ = write_transcript(project, [
                user("2026-10-06T22:59:58Z", "setup", origin="human"),
                assistant("2026-10-06T22:59:59Z", "synthetic-before", model="<synthetic>"),
                user("2026-10-06T23:00:00Z", "<command-name>/unit:delegate</command-name>"),
                assistant("2026-10-06T23:00:01Z", "opus"),
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "timestamp": "2026-10-06T23:00:10Z",
                    "compactMetadata": {
                        "trigger": "manual",
                        "preTokens": 1000,
                        "postTokens": 200,
                        "durationMs": 125,
                    },
                },
                user("2026-10-06T23:00:11Z", "local reply", origin="human"),
                assistant("2026-10-06T23:00:12Z", "synthetic", model="<synthetic>"),
                user("2026-10-06T23:00:13Z", "continue", origin="human"),
                assistant("2026-10-06T23:00:14Z", "opus-again"),
            ], "fixture-session")
            roster = directory / "roster.json"
            _ = roster.write_text(json.dumps([{
                "name": "fixture",
                "session_ids": ["fixture-session"],
                "switched_by_pdt": None,
                "production": "build-followups",
                "director_from_pdt": None,
            }]), encoding="utf-8")
            registry = directory / "registry"
            registry.mkdir()
            state = directory / "state"
            output = StringIO()
            with redirect_stdout(output):
                _ = extract(roster, directory / "projects", registry, state)

            turns = [
                cast(ExtractedTurn, json.loads(line))
                for line in (state / "turns.jsonl").read_text().splitlines()
            ]
            self.assertEqual([turn["request_id"] for turn in turns], ["opus", "opus-again"])
            self.assertEqual([turn["switch_turn"] for turn in turns], [True, False])
            self.assertEqual([turn["after_compact"] for turn in turns], [0, 1])
            summary = cast(ExtractionResult, json.loads((state / "extract.json").read_text()))["sessions"][0]
            self.assertEqual(summary["by_model"], {"claude-opus-4-1": 2})
            self.assertEqual(summary["dropped"]["before_director"], 1)
            self.assertEqual(summary["dropped"]["synthetic"], 1)
            self.assertIn("synthetic:1", output.getvalue())

    def test_roster_name_adds_registry_id_and_ignores_subagent_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            roster_path = directory / "roster.json"
            _ = roster_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "fixture",
                            "session_ids": ["old-id", "missing-id"],
                            "switched_by_pdt": None,
                            "production": "build-followups",
                            "director_from_pdt": None,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            projects_dir = directory / "projects"
            project = projects_dir / "project-a"
            project.mkdir(parents=True)
            old_path = write_transcript(project, [], "old-id")
            live_path = write_transcript(project, [], "live-id")
            subagents = project / "missing-id" / "subagents"
            subagents.mkdir(parents=True)
            _ = write_transcript(subagents, [], "missing-id")
            registry_dir = directory / "registry"
            registry_dir.mkdir()
            _ = (registry_dir / "entry.json").write_text(
                json.dumps({"name": "fixture", "sessionId": "live-id"}), encoding="utf-8"
            )

            entry = load_roster(roster_path)[0]
            self.assertEqual(entry.name, "fixture")
            self.assertEqual(list(entry.session_ids), ["old-id", "missing-id"])
            self.assertEqual(set(resolve_session_files(entry, projects_dir, registry_dir)), {old_path, live_path})

    def test_extract_files_and_stdout_contain_no_transcript_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            projects_dir = directory / "projects"
            project = projects_dir / "project-a"
            project.mkdir(parents=True)
            secret_prompt = "SECRET_PROMPT_FOR_EXTRACT_OUTPUT"
            secret_argument = "SECRET_TOOL_ARGUMENT_FOR_EXTRACT_OUTPUT"
            secret_path = "/private/SECRET_PATH_FOR_EXTRACT_OUTPUT"
            _ = write_transcript(
                project,
                [
                    user("2026-10-06T23:00:00Z", secret_prompt, origin="human"),
                    assistant("2026-10-06T23:00:01Z", "one"),
                    user(
                        "2026-10-06T23:00:10Z",
                        [{"type": "tool_result", "content": secret_argument}],
                    ),
                    assistant("2026-10-06T23:00:11Z", "two"),
                    {
                        "type": "attachment",
                        "timestamp": "2026-10-06T23:00:20Z",
                        "message": {"role": "attachment", "content": secret_path},
                    },
                    assistant("2026-10-06T23:00:21Z", "three"),
                ],
                "fixture-session",
            )
            roster_path = directory / "roster.json"
            _ = roster_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "fixture",
                            "session_ids": ["fixture-session"],
                            "switched_by_pdt": None,
                            "production": "build-followups",
                            "director_from_pdt": None,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            registry_dir = directory / "registry"
            registry_dir.mkdir()
            state_dir = directory / "state"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("model_study.py")),
                    "extract",
                    "--state-dir",
                    str(state_dir),
                    "--projects-dir",
                    str(projects_dir),
                    "--registry-dir",
                    str(registry_dir),
                    "--roster",
                    str(roster_path),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            output = completed.stdout + completed.stderr
            for filename in ("turns.jsonl", "compactions.jsonl", "extract.json"):
                output += (state_dir / filename).read_text(encoding="utf-8")
            for secret in (secret_prompt, secret_argument, secret_path):
                self.assertNotIn(secret, output)


if __name__ == "__main__":
    _ = unittest.main()
