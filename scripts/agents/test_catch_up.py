"""The catch-up reader prints the last blocks of a saved conversation of either family."""
from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.agents import catch_up

CODEX_ID = "01a18dbb-0000-7000-8000-00000000c0de"
CLAUDE_ID = "c27e8199-0000-4000-8000-0000000c1a0d"


def codex_item(minute: int, payload: dict[str, object], kind: str = "response_item") -> dict[str, object]:
    return {"timestamp": f"2026-11-13T21:{minute:02d}:00.000Z", "type": kind, "payload": payload}


def codex_message(minute: int, role: str, text: str) -> dict[str, object]:
    part = "output_text" if role == "assistant" else "input_text"
    return codex_item(minute, {"type": "message", "role": role, "content": [{"type": part, "text": text}]})


def claude_record(minute: int, kind: str, content: object, **fields: object) -> dict[str, object]:
    return {"timestamp": f"2026-11-13T22:{minute:02d}:00.000Z", "type": kind,
            "message": {"role": kind, "content": content}, **fields}


CODEX_RECORDS: list[dict[str, object]] = [
    codex_message(1, "user", "Track item 35."),
    codex_item(2, {"replacement_history": [{"type": "compaction", "encrypted_content": "sealed"}]}, "compacted"),
    codex_message(3, "developer", "Hidden instructions."),
    codex_item(4, {"type": "reasoning", "encrypted_content": "sealed"}),
    codex_message(5, "assistant", "I will read the list."),
    codex_item(6, {"type": "custom_tool_call", "name": "exec", "input": "cat\n  current.md"}),
    codex_item(7, {"type": "custom_tool_call_output", "output": [{"type": "input_text", "text": "next-item-number: 92"}]}),
    codex_item(8, {"type": "function_call", "name": "wait", "arguments": '{"cell_id":"382"}'}),
    codex_item(9, {"type": "function_call_output", "output": "Script completed"}),
    codex_item(10, {"type": "token_count"}, "event_msg"),
    codex_message(11, "assistant", "Item 35 stays open."),
]

CLAUDE_RECORDS: list[dict[str, object]] = [
    claude_record(1, "user", "This session is being continued.", isCompactSummary=True),
    claude_record(2, "user", [{"type": "text", "text": "Continue the transfer."}]),
    claude_record(3, "user", "Skill text.", isMeta=True),
    claude_record(4, "assistant", [{"type": "thinking", "thinking": "private"},
                                   {"type": "text", "text": "I will read the row."},
                                   {"type": "tool_use", "name": "Bash", "input": {"command": "staff.py show"}}]),
    claude_record(5, "user", [{"type": "tool_result", "content": [{"type": "text", "text": "morty row"}]}]),
    claude_record(6, "assistant", [{"type": "text", "text": "A helper answer."}], isSidechain=True),
    {"timestamp": "2026-11-13T22:07:00.000Z", "type": "system", "subtype": "compact_boundary"},
    claude_record(8, "assistant", [{"type": "text", "text": "The row is correct."}]),
]


class CatchUpTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home: Path = Path()
        self.codex: Path = Path()
        self.claude: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        environment = {key: value for key, value in os.environ.items() if key not in ("CODEX_HOME", "CLAUDE_CONFIG_DIR")}
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(
            os.environ, {**environment, "HOME": str(self.home), "USER_ZONE": "UTC", "CONF_DIR": str(self.home / "config")},
            clear=True)))
        (self.home / "config").mkdir()
        _ = (self.home / "config/catch_up.conf").write_text(
            "".join(f"# whole: 1..1073741824\n{key}={value}\n" for key, value in
                    (("blocks", 150), ("message_chars", 4000), ("tool_chars", 200), ("window_bytes", 8388608))),
            encoding="utf-8")
        self.codex = self.write(".codex/sessions/2026/11/13", f"rollout-2026-11-13T12-00-00-{CODEX_ID}.jsonl", CODEX_RECORDS)
        self.claude = self.write(".claude5/projects/-home-fixture", f"{CLAUDE_ID}.jsonl", CLAUDE_RECORDS)

    def write(self, folder: str, name: str, records: list[dict[str, object]]) -> Path:
        path = self.home / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("".join(json.dumps(record) + "\n" for record in records) + "not json\n")
        return path

    def labels(self, path: Path, count: int = 50, window: int = 8388608) -> list[tuple[str, str]]:
        return [(block.label, block.text) for block in catch_up.last_blocks(path, count, window)]

    def test_codex_blocks_keep_messages_tools_and_the_compaction_time(self) -> None:
        self.assertEqual(self.labels(self.codex), [
            ("user", "Track item 35."),
            ("compaction", "The Codex summary is encrypted."),
            ("assistant", "I will read the list."),
            ("tool exec", "cat\n  current.md"),
            ("result", "next-item-number: 92"),
            ("tool wait", '{"cell_id":"382"}'),
            ("result", "Script completed"),
            ("assistant", "Item 35 stays open."),
        ])

    def test_claude_blocks_keep_the_summary_and_skip_helper_and_command_text(self) -> None:
        self.assertEqual(self.labels(self.claude), [
            ("compaction summary", "This session is being continued."),
            ("user", "Continue the transfer."),
            ("assistant", "I will read the row."),
            ("tool Bash", '{"command": "staff.py show"}'),
            ("result", "morty row"),
            ("assistant", "The row is correct."),
        ])

    def test_a_small_window_reads_the_same_last_blocks(self) -> None:
        whole = self.labels(self.codex)
        for count in (1, 3, len(whole), len(whole) + 5):
            with self.subTest(count=count):
                self.assertEqual(self.labels(self.codex, count, window=40), whole[-count:])

    def test_the_address_kind_selects_the_family_and_an_id_alone_takes_the_newest(self) -> None:
        shared = "00000000-0000-4000-8000-00000000beef"
        codex = self.write(".codex/archived_sessions", f"rollout-2026-11-01T00-00-00-{shared}.jsonl", CODEX_RECORDS)
        claude = self.write(".claude/projects/-home-fixture", f"{shared}.jsonl", CLAUDE_RECORDS)
        os.utime(codex, (1000, 1000))
        os.utime(claude, (2000, 2000))
        self.assertEqual(catch_up.find_transcript(f"codex:{shared}"), codex.resolve())
        self.assertEqual(catch_up.find_transcript(f"session:{shared}"), claude.resolve())
        self.assertEqual(catch_up.find_transcript(shared), claude.resolve())
        self.assertEqual(catch_up.find_transcript(f"codex:{CODEX_ID}"), self.codex.resolve())
        with self.assertRaises(catch_up.NoSavedTranscript):
            _ = catch_up.find_transcript(f"session:{CODEX_ID}")
        for address in ("worker:unit/name", "codex:", "codex:*", "session:../x"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                _ = catch_up.find_transcript(address)

    def test_one_codex_transcript_reached_through_two_account_homes_is_one_file(self) -> None:
        (self.home / ".codex1").mkdir()
        (self.home / ".codex1/sessions").symlink_to(self.home / ".codex/sessions", target_is_directory=True)
        self.assertEqual(catch_up.find_transcript(CODEX_ID), self.codex.resolve())

    def test_output_shortens_tool_lines_and_long_messages(self) -> None:
        long_call = codex_item(12, {"type": "custom_tool_call", "name": "exec", "input": "x" * 300})
        long_message = codex_message(13, "assistant", "y" * 50)
        path = self.write(".codex/sessions/2026/11/14", f"rollout-2026-11-14T00-00-00-{CODEX_ID}.jsonl",
                          [*CODEX_RECORDS, long_call, long_message])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(catch_up.main([f"codex:{CODEX_ID}", "--blocks", "3", "--chars", "20"]), 0)
        self.assertEqual(output.getvalue(), (
            f"{path.resolve()}: last 3 blocks, times in UTC\n"
            + "\n[11-13 21:11:00] assistant\nItem 35 stays open.\n"
            + f"[11-13 21:12:00] tool exec: {'x' * 200}…\n"
            + f"\n[11-13 21:13:00] assistant\n{'y' * 20}\n[30 more characters]\n"))

    def test_a_missing_transcript_prints_one_line_and_fails(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(catch_up.main(["codex:00000000-0000-4000-8000-000000000000"]), 1)
        self.assertEqual(errors.getvalue(),
                         "catch_up.py: no saved transcript for codex:00000000-0000-4000-8000-000000000000\n")


if __name__ == "__main__":
    _ = unittest.main()
