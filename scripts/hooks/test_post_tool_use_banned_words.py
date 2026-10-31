"""The PostToolUse hook scans authored text without scanning tool responses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

if TYPE_CHECKING:
    from tool_call_text import ToolInput


HOOK = Path(__file__).with_name("post-tool-use-banned-words.py")


class PostToolUseBannedWordsTests(unittest.TestCase):
    home: Path = Path()
    counters: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        guide = self.home / "rust/nate_style/rust/forbidden-words.md"
        guide.parent.mkdir(parents=True)
        _ = guide.write_text('### "zorblatt"\n\nDo not use it.\n', encoding="utf-8")
        state = self.home / ".claude/state"
        state.mkdir(parents=True)
        self.counters = state / "forbidden-word-counts.json"
        self.environment = {**os.environ, "HOME": str(self.home)}

    def run_hook(self, tool: str, tool_input: ToolInput,
                 **response: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": tool, "tool_input": tool_input,
                              "tool_response": response}),
            env=self.environment, cwd=self.home, capture_output=True, text=True,
            check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def assert_silent(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.counters.exists())

    def assert_blocked(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        verdict = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(verdict["decision"], "block")
        self.assertIn("zorblatt", str(verdict["reason"]))
        self.assertIn('"count": 1', self.counters.read_text(encoding="utf-8"))

    def test_tool_response_words_do_not_block_or_create_counters(self) -> None:
        for field in ("stdout", "output", "stderr"):
            with self.subTest(field=field):
                self.assert_silent(self.run_hook("Bash", {"command": "printf clean"},
                                                **{field: "zorblatt"}))

    def test_tool_response_words_leave_existing_counters_unchanged(self) -> None:
        original = '{"version": 1, "words": {"zorblatt": {"count": 7}}}\n'
        _ = self.counters.write_text(original, encoding="utf-8")
        result = self.run_hook("Bash", {"command": "printf clean"}, stdout="zorblatt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.counters.read_text(encoding="utf-8"), original)

    def test_a_shell_command_writing_the_word_blocks(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {"command": "printf 'zorblatt\\n' > note.txt"}))

    def test_written_file_content_blocks(self) -> None:
        self.assert_blocked(self.run_hook("Write", {"content": "zorblatt", "file_path": "note.txt"}))

    def test_replacement_text_blocks(self) -> None:
        self.assert_blocked(self.run_hook("Edit", {"new_string": "zorblatt"}))

    def test_a_commit_message_is_scanned_beside_a_log_command(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {
            "command": 'git commit -m "zorblatt" && git log -1',
        }, stdout="clean"))

    def test_a_search_pattern_and_its_results_do_not_block(self) -> None:
        self.assert_silent(self.run_hook("Bash", {"command": "grep 'zorblatt' note.txt"},
                                        stdout="zorblatt"))

    def test_inspection_paths_do_not_block(self) -> None:
        self.assert_silent(self.run_hook("Bash", {"command": "cat zorblatt.txt"}))

    def test_inspection_commands_do_not_exempt_written_descriptions(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {"command": "cat note.txt",
                                                  "description": "zorblatt"}))

    def test_a_reader_does_not_exempt_a_writer_in_the_same_command(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {
            "command": "cat note.txt && printf 'zorblatt\\n'",
        }))

    def test_counter_report_output_does_not_exempt_written_text(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {"cmd": "printf zorblatt"},
                                         output="Counter state: forbidden-word-counts.json"))

    def test_search_masking_does_not_exempt_a_word_written_later(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {
            "command": "grep zorblatt note.txt; printf zorblatt",
        }, stdout="zorblatt"))

    def test_multiedit_replacement_text_blocks(self) -> None:
        self.assert_blocked(self.run_hook("MultiEdit", {"edits": [{"new_string": "zorblatt"}]}))

    def test_notebook_inserted_source_blocks(self) -> None:
        self.assert_blocked(self.run_hook("NotebookEdit", {"new_source": "zorblatt"}))

    def test_notebook_deletion_does_not_scan_removed_source(self) -> None:
        self.assert_silent(self.run_hook("NotebookEdit", {"edit_mode": "delete",
                                                          "new_source": "zorblatt"}))

    def test_read_only_tools_do_not_scan_search_text_or_output(self) -> None:
        self.assert_silent(self.run_hook("Grep", {"content": "zorblatt"}, output="zorblatt"))

    def test_scripted_removal_does_not_scan_the_removed_text(self) -> None:
        self.assert_silent(self.run_hook("Bash", {"command": "sed -i 's/zorblatt/clean/' note.txt"}))

    def test_scripted_replacement_scans_the_inserted_text(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {"command": "sed -i 's/clean/zorblatt/' note.txt"}))

    def test_a_shell_substitution_is_not_exempted_by_its_reader(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {"command": 'cat "$(printf zorblatt)"'}))

    def test_inspection_tools_with_authored_format_strings_are_scanned(self) -> None:
        self.assert_blocked(self.run_hook("Bash", {"command": "stat --printf=zorblatt note.txt"}))

    def test_guide_reproduction_remains_exempt(self) -> None:
        self.assert_silent(self.run_hook("Write", {
            "content": "forbidden-words.md lists zorblatt.", "file_path": "guide-note.txt",
        }))


if __name__ == "__main__":
    _ = unittest.main()
