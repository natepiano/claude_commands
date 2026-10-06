"""Fixture tests for transcript call classification."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, cast

from scripts.shot_report.episodes import split_episodes
from scripts.shot_report.transcripts import _classify, scan_calls  # pyright: ignore[reportPrivateUsage]


class ShellCases(TypedDict):
    piped_hana: str
    piped_curl: str
    search_hana: str
    search_brp: str
    heredoc: str
    move: str
    skip: str


CASES = cast(ShellCases, json.loads(
    (Path(__file__).parent / "fixtures" / "classify" / "shell_cases.json").read_text(encoding="utf-8")
))
EXECUTION_CASES = cast(dict[str, list[str]], json.loads(
    (Path(__file__).parent / "fixtures" / "classify" / "execution_cases.json").read_text(encoding="utf-8")
))
START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _claude_case(path: Path, calls: list[tuple[str, str, object]], cwd: str = "/fictional/studio") -> None:
    rows: list[dict[str, object]] = []
    for number, (name, command, result) in enumerate(calls):
        start = START + timedelta(seconds=number * 30)
        for stamp, kind, content in (
            (start, "tool_use", {"type": "tool_use", "id": str(number), "name": name, "input": {"command": command}}),
            (start + timedelta(seconds=5), "tool_result", {"type": "tool_result", "tool_use_id": str(number), "content": result}),
        ):
            rows.append({
                "timestamp": stamp.isoformat(), "cwd": cwd, "sessionId": path.stem,
                "message": {"content": [content]}, "type": kind,
            })
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _codex_case(path: Path, commands: list[str], packed: bool = False) -> None:
    rows: list[dict[str, object]] = [{
        "timestamp": START.isoformat(), "type": "session_meta",
        "payload": {"id": path.stem, "cwd": "/fictional/studio"},
    }]
    for number, command in enumerate(commands):
        stamp = START + timedelta(seconds=number * 30)
        wrapper = 'const r=await tools.exec_command({cmd:' + json.dumps(command) + '});text(r.output);'
        call_input: dict[str, object] = {"arguments": json.dumps({"input": wrapper})} if packed else {"input": wrapper}
        rows.extend((
            {"timestamp": stamp.isoformat(), "type": "response_item", "payload": {
                "type": "function_call", "name": "functions.exec", "call_id": str(number), **call_input,
            }},
            {"timestamp": (stamp + timedelta(seconds=5)).isoformat(), "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": str(number), "output": "done",
            }},
        ))
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class TranscriptTest(unittest.TestCase):
    def _assert_shell_cases(self, group: str, kind: str, source: str) -> None:
        for command in EXECUTION_CASES[group]:
            with self.subTest(command=command):
                classified = _classify("Bash", {"command": command})
                self.assertIsNotNone(classified)
                if classified is not None:
                    self.assertEqual(classified[:2], (kind, source))

    def test_positional_method_arguments_count(self) -> None:
        self._assert_shell_cases("positional", "shot", "bash_brp")

    def test_python_call_literal_counts(self) -> None:
        self._assert_shell_cases("python_call", "shot", "bash_brp")

    def test_interpreter_heredoc_and_written_script_run_count(self) -> None:
        self._assert_shell_cases("heredoc", "shot", "bash_brp")

    def test_compound_shell_commands_count(self) -> None:
        self._assert_shell_cases("compound", "shot", "bash_brp")

    def test_wrapped_and_nested_commands_count(self) -> None:
        for command in EXECUTION_CASES["wrapped"]:
            with self.subTest(command=command):
                classified = _classify("Bash", {"command": command})
                self.assertIsNotNone(classified)
                if classified is not None:
                    self.assertEqual(classified[0], "shot")

    def test_text_and_unrun_script_mentions_do_not_count(self) -> None:
        for command in EXECUTION_CASES["text_only"]:
            with self.subTest(command=command):
                self.assertIsNone(_classify("Bash", {"command": command}))

    def test_other_positional_and_python_methods_are_related_calls(self) -> None:
        self._assert_shell_cases("other", "other", "bash_brp")

    def test_codex_exec_patch_without_shell_call_is_not_a_command(self) -> None:
        patch = "const patch=\"*** Begin Patch\\n+rpc('brp_extras/screenshot', {})\"; await tools.apply_patch(patch)"
        self.assertIsNone(_classify("functions.exec", {"input": patch}))

    def test_later_screenshot_takes_priority_over_related_call(self) -> None:
        for command in EXECUTION_CASES["later_shot"]:
            with self.subTest(command=command):
                classified = _classify("Bash", {"command": command})
                self.assertIsNotNone(classified)
                if classified is not None:
                    self.assertEqual(classified[0], "shot")

    def test_variable_program_can_pass_positional_method(self) -> None:
        self._assert_shell_cases("variable_runner", "shot", "bash_brp")

    def test_compound_heredoc_and_written_script_run_count(self) -> None:
        self._assert_shell_cases("compound_heredoc", "shot", "bash_brp")

    def test_assigned_request_sent_by_curl_counts(self) -> None:
        self._assert_shell_cases("assigned_request", "shot", "bash_brp")

    def test_interpreter_options_import_and_parameter_expansion_count(self) -> None:
        self._assert_shell_cases("script_execution", "shot", "bash_brp")

    def test_piped_hana_shot_counts_with_distinct_result_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_case(root / "claude" / "hana.jsonl", [(
                "Bash", CASES["piped_hana"],
                "saved /tmp/a.png and /tmp/b.png; again /tmp/a.png and scene.png",
            )])
            calls = scan_calls(root / "claude", root / "codex")
            self.assertEqual([(call.source, call.image_count) for call in calls], [("hana_shot", 3)])

    def test_piped_curl_shot_counts_one_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_case(root / "claude" / "curl.jsonl", [(
                "Bash", CASES["piped_curl"], "saved /tmp/shot.png",
            )])
            calls = scan_calls(root / "claude", root / "codex")
            self.assertEqual([(call.source, call.image_count) for call in calls], [("bash_brp", 1)])

    def test_searches_do_not_take_screenshots_in_shell_or_codex_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            searches = [CASES["search_hana"], CASES["search_brp"]]
            _claude_case(root / "claude" / "search.jsonl", [("Bash", command, "matches") for command in searches])
            _codex_case(root / "codex" / "search.jsonl", searches)
            _codex_case(root / "codex" / "packed.jsonl", searches, packed=True)
            self.assertEqual(scan_calls(root / "claude", root / "codex"), [])

    def test_codex_exec_wrapper_counts_only_its_shell_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _codex_case(root / "codex" / "shot.jsonl", [CASES["piped_curl"]], packed=True)
            calls = scan_calls(root / "claude", root / "codex")
            self.assertEqual([(call.source, call.image_count) for call in calls], [("bash_brp", 1)])

    def test_search_segment_does_not_hide_a_later_shot(self) -> None:
        command = CASES["search_brp"] + " | head -3; cd /tmp && env MODE=shot python3 /tools/hana_shot.py shot --view main"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_case(root / "claude" / "segments.jsonl", [("Bash", command, "saved /tmp/a.png")])
            calls = scan_calls(root / "claude", root / "codex")
            self.assertEqual([call.source for call in calls], ["hana_shot"])

    def test_heredoc_body_does_not_take_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_case(root / "claude" / "heredoc.jsonl", [("Bash", CASES["heredoc"], "ok")])
            self.assertEqual(scan_calls(root / "claude", root / "codex"), [])

    def test_python_heredoc_posting_brp_method_is_a_shot(self) -> None:
        command = "python3 - <<'PY'\nimport requests\nrequests.post('http://localhost:15702', json={'method': 'brp_extras/screenshot'})\nPY"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_case(root / "claude" / "python.jsonl", [("Bash", command, "saved /tmp/a.png")])
            calls = scan_calls(root / "claude", root / "codex")
            self.assertEqual([(call.kind, call.source) for call in calls], [("shot", "bash_brp")])

    def test_bash_move_starts_episode_before_curl_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_case(root / "claude" / "move.jsonl", [
                ("Bash", CASES["move"], "ok"),
                ("Bash", CASES["skip"], "ok"),
                ("Bash", CASES["piped_curl"], "saved /tmp/shot.png"),
            ])
            calls = scan_calls(root / "claude", root / "codex")
            self.assertEqual([(call.kind, call.source) for call in calls], [
                ("other", "bash_brp"), ("shot", "bash_brp"),
            ])
            episodes = split_episodes(calls, 300)
            self.assertEqual(len(episodes), 1)
            self.assertEqual(episodes[0].start, START)
            self.assertEqual(episodes[0].other_call_count, 1)

    def test_browser_snapshot_alone_makes_no_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_case(root / "claude" / "browser.jsonl", [
                ("mcp__playwright__browser_snapshot", "", "accessibility tree"),
            ])
            calls = scan_calls(root / "claude", root / "codex")
            self.assertEqual([(call.kind, call.image_count) for call in calls], [("other", 0)])
            self.assertEqual(split_episodes(calls, 300), [])

    def test_deleted_worktree_uses_live_repository_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "studio"
            repo.mkdir()
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            missing = root / "studio-feature-old"
            _claude_case(root / "claude" / "missing.jsonl", [
                ("mcp__brp__brp_extras_screenshot", "", "saved /tmp/a.png"),
            ], str(missing))
            _claude_case(root / "claude" / "live.jsonl", [
                ("mcp__brp__brp_extras_screenshot", "", "saved /tmp/b.png"),
            ], str(repo))
            calls = scan_calls(root / "claude", root / "codex")
            self.assertEqual({call.project for call in calls}, {"studio"})

    def test_deleted_worktree_uses_longest_live_repository_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("studio", "studio-feature"):
                repo = root / name
                repo.mkdir()
                _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
                _claude_case(root / "claude" / f"{name}.jsonl", [
                    ("mcp__brp__brp_extras_screenshot", "", "saved /tmp/a.png"),
                ], str(repo))
            _claude_case(root / "claude" / "missing.jsonl", [
                ("mcp__brp__brp_extras_screenshot", "", "saved /tmp/b.png"),
            ], str(root / "studio-feature-old"))
            calls = scan_calls(root / "claude", root / "codex")
            missing = next(call for call in calls if Path(call.transcript_path).stem == "missing")
            self.assertEqual(missing.project, "studio-feature")


if __name__ == "__main__":
    _ = unittest.main()
