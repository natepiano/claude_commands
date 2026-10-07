"""Fixture tests for transcript call classification."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, cast

from scripts.shot_report.episodes import split_episodes
from scripts.shot_report.transcripts import RememberedScript, ToolCall, _classify, _remember, scan_calls  # pyright: ignore[reportPrivateUsage]


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
CLASSIFY = Path(__file__).parent / "fixtures" / "classify"


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


def _claude_tools(path: Path, tools: list[tuple[str, dict[str, object], bool]], cwd: str) -> None:
    rows: list[dict[str, object]] = []
    for number, (name, value, error) in enumerate(tools):
        for seconds, kind, part in (
            (0, "tool_use", {"type": "tool_use", "id": str(number), "name": name, "input": value}),
            (5, "tool_result", {"type": "tool_result", "tool_use_id": str(number),
                                "content": "done", "is_error": error}),
        ):
            rows.append({"timestamp": (START + timedelta(seconds=number * 30 + seconds)).isoformat(),
                         "cwd": cwd, "sessionId": path.stem, "message": {"content": [part]}, "type": kind})
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class TranscriptTest(unittest.TestCase):
    def _script_calls(self, session: str) -> list[ToolCall]:
        calls = scan_calls(CLASSIFY / "claude" / "projects", CLASSIFY / "codex" / "sessions")
        return [call for call in calls if call.session_id == session]

    def test_source_mention_does_not_enter_script_name_filter(self) -> None:
        scripts: dict[str, RememberedScript] = {}
        _remember("/fictional/studio/main.py", 'print("world.query")\n', scripts)
        self.assertEqual(scripts, {})
        _remember("/fictional/studio/HANDOFF.md", "```sh\ngrim /tmp/view.png\n```\n", scripts)
        self.assertEqual(scripts, {})
        _remember("/fictional/studio/shot.sh", "grim /tmp/view.png\n", scripts)
        _remember("/fictional/studio/wrapper.sh", "bash shot.sh\n", scripts)
        self.assertEqual(set(scripts), {"/fictional/studio/shot.sh", "/fictional/studio/wrapper.sh"})
        _remember("/fictional/studio/wrapper.sh", "echo done\n", scripts)
        self.assertEqual(set(scripts), {"/fictional/studio/shot.sh"})
        _remember("/fictional/studio/shot.sh", "grim /tmp/view.png\necho changed\n", scripts)
        self.assertIn("/fictional/studio/shot.sh", scripts)
        _remember("/fictional/studio/shot.sh", 'echo "world.query"\n', scripts)
        self.assertEqual(scripts, {})

    def test_status_subcommand_of_shot_helper_is_related_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = 'import sys\nif sys.argv[1] == "shot":\n    rpc("brp_extras/screenshot")\nelse:\n    rpc("world.get")\n'
            _claude_tools(root / "claude" / "dispatch.jsonl", [
                ("Write", {"file_path": "helper.py", "content": content}, False),
                ("Bash", {"command": "python3 helper.py status"}, False),
                ("Bash", {"command": "python3 helper.py shot"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([(call.kind, call.image_count) for call in calls], [("other", 0), ("shot", 1)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shell = 'case "$1" in\nshot) grim /tmp/view.png ;;\nquery) curl -d \'{"method":"world.get"}\' localhost ;;\nesac\n'
            _claude_tools(root / "claude" / "shell-dispatch.jsonl", [
                ("Write", {"file_path": "helper.sh", "content": shell}, False),
                ("Bash", {"command": "bash helper.sh query"}, False),
                ("Bash", {"command": "bash helper.sh shot"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.kind for call in calls], ["other", "shot"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = ('import sys\n'
                      'def shot():\n    rpc("brp_extras/screenshot")\n'
                      'def shutdown():\n    rpc("brp_extras/shutdown")\n'
                      'if __name__ == "__main__":\n    exec(sys.argv[1])\n')
            _claude_tools(root / "claude" / "expression.jsonl", [
                ("Write", {"file_path": "helper.py", "content": helper}, False),
                ("Bash", {"command": "python3 helper.py 'shutdown()'"}, False),
                ("Bash", {"command": "python3 helper.py 'shot()'"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.kind for call in calls], ["shot"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = ('# usage: pose_shot <path>\n'
                       'rpc() { curl -d "$2" localhost; }\n'
                       'shot() { rpc brp_extras/screenshot "{}"; }\n'
                       'query() { rpc world.query "{}"; }\n'
                       'pose_shot() {\n  query\n  shot\n}\n')
            _claude_tools(root / "claude" / "sourced.jsonl", [
                ("Write", {"file_path": "brp.sh", "content": library}, False),
                ("Bash", {"command": "source brp.sh; query"}, False),
                ("Bash", {"command": "source brp.sh; shot"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.kind for call in calls], ["other", "shot"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = ('import argparse\nparser = argparse.ArgumentParser()\n'
                      'parser.add_argument("action", choices=["shot", "status"])\n'
                      'args = parser.parse_args()\n'
                      'if __name__ == "__main__":\n'
                      '    if args.action == "shot":\n'
                      '        rpc("brp_extras/screenshot")\n'
                      '    else:\n'
                      '        rpc("world.get")\n')
            _claude_tools(root / "claude" / "argparse.jsonl", [
                ("Write", {"file_path": "helper.py", "content": python}, False),
                ("Bash", {"command": "python3 helper.py status"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.kind for call in calls], ["other"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = ('import sys\n'
                      'def shot():\n    rpc("brp_extras/screenshot")\n'
                      'def query():\n    rpc("world.get")\n'
                      'def main():\n'
                      '    args = sys.argv[1:]\n'
                      '    command = args[0]\n'
                      '    if command == "shot":\n        shot()\n'
                      '    elif command == "query":\n        query()\n'
                      'if __name__ == "__main__":\n    main()\n')
            _claude_tools(root / "claude" / "functions.jsonl", [
                ("Write", {"file_path": "helper.py", "content": helper}, False),
                ("Bash", {"command": "python3 helper.py query"}, False),
                ("Bash", {"command": "python3 helper.py shot"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.kind for call in calls], ["other", "shot"])

    def test_option_values_shell_positions_and_command_words_select_branches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = ('import argparse\nparser = argparse.ArgumentParser()\n'
                      'parser.add_argument("--mode", "-m")\nargs = parser.parse_args()\n'
                      'if args.mode == "shot":\n    rpc("brp_extras/screenshot")\n'
                      'else:\n    rpc("world.get")\n')
            _claude_tools(root / "claude" / "options.jsonl", [
                ("Write", {"file_path": "helper.py", "content": python}, False),
                *(("Bash", {"command": f"python3 helper.py {option}"}, False)
                  for option in ("--mode shot", "-m shot", "--mode=shot", "--mode status", "-m status", "--mode=status")),
            ], "/fictional/studio")
            shell = ('if [[ "$2" == shot ]]; then\n'
                     '  grim /tmp/view.png\nelse\n'
                     '  curl -d \'{"method":"world.get"}\' localhost\nfi\n')
            _claude_tools(root / "claude" / "positions.jsonl", [
                ("Write", {"file_path": "helper.sh", "content": shell}, False),
                ("Bash", {"command": "bash helper.sh ignored status"}, False),
                ("Bash", {"command": "bash helper.sh ignored shot"}, False),
                ("Write", {"file_path": "single.sh", "content":
                           'if [ $1 = shot ]; then grim /tmp/view.png; else curl -d \'{"method":"world.get"}\' localhost; fi'}, False),
                ("Bash", {"command": "bash single.sh status"}, False),
                ("Bash", {"command": "bash single.sh shot"}, False),
                ("Write", {"file_path": "double.sh", "content":
                           'if [[ $1 = "shot" ]]; then grim /tmp/view.png; else curl -d \'{"method":"world.get"}\' localhost; fi'}, False),
                ("Bash", {"command": "bash double.sh status"}, False),
                ("Bash", {"command": "bash double.sh shot"}, False),
            ], "/fictional/studio")
            library = ('shot() { grim /tmp/view.png; }\n'
                       'query() { curl -d \'{"method":"world.get"}\' localhost; }\n')
            _claude_tools(root / "claude" / "library.jsonl", [
                ("Write", {"file_path": "brp.sh", "content": library}, False),
                ("Bash", {"command": "source brp.sh; echo shot; query"}, False),
                ("Bash", {"command": "source brp.sh; shot"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        by_session = {session: [call.kind for call in calls if call.session_id == session]
                      for session in ("options", "positions", "library")}
        self.assertEqual(by_session, {
            "options": ["shot", "shot", "shot", "other", "other", "other"],
            "positions": ["other", "shot", "other", "shot", "other", "shot"],
            "library": ["other", "shot"],
        })

    def test_each_script_run_contributes_images_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shot = "curl -d '{\"method\":\"brp_extras/screenshot\"}' localhost\n"
            _claude_tools(root / "claude" / "multiple.jsonl", [
                ("Write", {"file_path": "a.sh", "content": shot}, False),
                ("Write", {"file_path": "b.sh", "content": shot}, False),
                ("Bash", {"command": "bash a.sh; bash b.sh; bash a.sh"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([(call.image_count, call.script_paths) for call in calls], [
            (3, ("/fictional/studio/a.sh", "/fictional/studio/b.sh", "/fictional/studio/a.sh")),
        ])

    def test_scratchpad_decodes_punctuation_and_missing_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "hana_catalyst"
            repo.mkdir()
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            missing = root / "hana_catalyst-feature"
            encoded_live = "-" + "-".join(repo.parts[1:]).replace("_", "-")
            encoded_missing = "-" + "-".join(missing.parts[1:]).replace("_", "-")
            for label, encoded in (("live", encoded_live), ("missing", encoded_missing)):
                _claude_case(root / "claude" / f"{label}.jsonl", [
                    ("mcp__brp__brp_extras_screenshot", "", "saved x.png"),
                ], f"{root}/claude-1/{encoded}/session/scratchpad")
            _claude_case(root / "claude" / "main.jsonl", [
                ("mcp__brp__brp_extras_screenshot", "", "saved y.png"),
            ], str(repo))
            calls = scan_calls(root / "claude", root / "codex")
        by_name = {Path(call.transcript_path).stem: call for call in calls}
        self.assertEqual(by_name["live"].project, "hana_catalyst")
        self.assertEqual(by_name["live"].project_attribution, "scratchpad")
        self.assertEqual(by_name["missing"].project, "hana_catalyst")
        self.assertEqual(by_name["missing"].project_attribution, "removed_worktree")

    def test_scratchpad_removed_worktree_matches_repository_outside_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories = root / "repositories"
            worktrees = root / "worktrees"
            repositories.mkdir()
            worktrees.mkdir()
            for name in ("hana_catalyst", "render.engine"):
                repo = repositories / name
                repo.mkdir()
                _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
                _claude_case(root / "claude" / f"repo-{name}.jsonl", [
                    ("mcp__brp__brp_extras_screenshot", "", "saved a.png"),
                ], str(repo))
            for name in ("hana_catalyst-feature", "render.engine-feature", "absent-project-feature"):
                target = worktrees / name
                encoded = re.sub(r"[^A-Za-z0-9]", "-", str(target))
                _claude_case(root / "claude" / f"missing-{name}.jsonl", [
                    ("mcp__brp__brp_extras_screenshot", "", "saved b.png"),
                ], f"{root}/claude-1/{encoded}/session/scratchpad")
            calls = scan_calls(root / "claude", root / "codex")
        by_name = {Path(call.transcript_path).stem: call for call in calls}
        self.assertEqual((by_name["missing-hana_catalyst-feature"].project,
                          by_name["missing-hana_catalyst-feature"].project_attribution),
                         ("hana_catalyst", "removed_worktree"))
        self.assertEqual((by_name["missing-render.engine-feature"].project,
                          by_name["missing-render.engine-feature"].project_attribution),
                         ("render.engine", "removed_worktree"))
        self.assertEqual((by_name["missing-absent-project-feature"].project,
                          by_name["missing-absent-project-feature"].project_attribution),
                         ("absent-project-feature", "scratchpad_target_missing"))

    def test_python_wrapper_runs_remembered_shell_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_tools(root / "claude" / "nested.jsonl", [
                ("Write", {"file_path": "shot.sh", "content": "curl -d '{\"method\":\"brp_extras/screenshot\"}' localhost"}, False),
                ("Write", {"file_path": "wrapper.py", "content": 'import subprocess\nsubprocess.run(["bash", "shot.sh"])\n'}, False),
                ("Bash", {"command": "python3 wrapper.py"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([(call.kind, call.image_count) for call in calls], [("shot", 1)])

    def test_exec_family_and_subprocess_run_link_script_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrappers = [
                *(f'os.{name}("/bin/bash", ["bash", "shot.sh"]{", {}" if name.endswith("e") else ""})'
                  for name in ("execv", "execve", "execvp", "execvpe")),
                *(f'os.{name}("/bin/bash", "bash", "shot.sh"{", {}" if name.endswith("e") else ""})'
                  for name in ("execl", "execle", "execlp", "execlpe")),
                'subprocess.run(["bash", "shot.sh"])',
            ]
            tools: list[tuple[str, dict[str, object], bool]] = [
                ("Write", {"file_path": "shot.sh", "content": "grim /tmp/view.png"}, False),
            ]
            for index, wrapper in enumerate(wrappers):
                tools.extend((
                    ("Write", {"file_path": f"wrapper{index}.py", "content": f"import os, subprocess\n{wrapper}\n"}, False),
                    ("Bash", {"command": f"python3 wrapper{index}.py"}, False),
                ))
            _claude_tools(root / "claude" / "exec-family.jsonl", tools, "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([(call.kind, call.image_count) for call in calls], [("shot", 1)] * len(wrappers))

    def test_heredoc_write_after_cd_uses_new_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_case(root / "claude" / "cd-write.jsonl", [
                ("Bash", "cd shots && cat > shot.sh <<'EOF'\ncurl -d '{\"method\":\"brp_extras/screenshot\"}' localhost\nEOF", "done"),
                ("Bash", "bash shots/shot.sh", "done"),
            ])
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.script_path for call in calls], ["/fictional/studio/shots/shot.sh"])

    def test_failed_edit_keeps_last_successful_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = "curl -d '{\"method\":\"brp_extras/screenshot\"}' localhost"
            _claude_tools(root / "claude" / "failed-edit.jsonl", [
                ("Write", {"file_path": "shot.sh", "content": content}, False),
                ("Edit", {"file_path": "shot.sh", "old_string": content, "new_string": "echo done"}, True),
                ("Bash", {"command": "bash shot.sh"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([(call.kind, call.image_count) for call in calls], [("shot", 1)])

    def test_only_executed_calls_inside_loops_multiply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shell = "# brp_extras/screenshot\nfor i in 1 2 3; do echo ready; done\ncurl -d '{\"method\":\"brp_extras/screenshot\"}' localhost\n"
            python = 'for i in [1, 2, 3]:\n    rpc("brp_extras/screenshot")\n'
            _claude_tools(root / "claude" / "loops.jsonl", [
                ("Write", {"file_path": "one.sh", "content": shell}, False),
                ("Write", {"file_path": "three.py", "content": python}, False),
                ("Bash", {"command": "bash one.sh"}, False),
                ("Bash", {"command": "python3 three.py"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.image_count for call in calls], [1, 3])

    def test_python_ranges_and_called_functions_set_image_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bodies = [
                'for _ in range(3):\n    rpc("brp_extras/screenshot")\n',
                'for _ in range(2, 5):\n    rpc("brp_extras/screenshot")\n',
                'for _ in range(1, 7, 2):\n    rpc("brp_extras/screenshot")\n',
                'for _ in range(limit):\n    rpc("brp_extras/screenshot")\n',
                'def shot():\n    rpc("brp_extras/screenshot")\nshot()\nshot()\n',
                'def unused():\n    rpc("brp_extras/screenshot")\nprint("ready")\n',
            ]
            tools: list[tuple[str, dict[str, object], bool]] = []
            for index, body in enumerate(bodies):
                tools.extend((
                    ("Write", {"file_path": f"count{index}.py", "content": body}, False),
                    ("Bash", {"command": f"python3 count{index}.py"}, False),
                ))
            _claude_tools(root / "claude" / "python-counts.jsonl", tools, "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.image_count for call in calls], [3, 3, 3, 1, 2])

    def test_python_function_references_and_decorators_count_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shot = 'def shoot():\n    rpc("brp_extras/screenshot")\n'
            bodies = [
                shot + 'threading.Thread(target=shoot).start()\n',
                shot + 'for _ in range(3):\n    executor.submit(shoot, view)\n',
                shot + 'list(map(shoot, views))\n',
                shot + 'handler = shoot\n',
                '@app.command()\n' + shot,
            ]
            tools: list[tuple[str, dict[str, object], bool]] = []
            for index, body in enumerate(bodies):
                tools.extend((
                    ("Write", {"file_path": f"reference{index}.py", "content": body}, False),
                    ("Bash", {"command": f"python3 reference{index}.py"}, False),
                ))
            _claude_tools(root / "claude" / "references.jsonl", tools, "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.image_count for call in calls], [1, 3, 1, 1, 1])

    def test_written_executable_with_other_suffix_is_remembered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_tools(root / "claude" / "suffix.jsonl", [
                ("Write", {"file_path": "shot.command", "content": "grim /tmp/view.png"}, False),
                ("Bash", {"command": "./shot.command"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([(call.kind, call.script_path) for call in calls], [("shot", "/fictional/studio/shot.command")])

    def test_existing_non_repository_prefix_keeps_last_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "hana"
            repo.mkdir()
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            notes = root / "hana-notes"
            notes.mkdir()
            for name, cwd in (("repo", repo), ("notes", notes)):
                _claude_case(root / "claude" / f"{name}.jsonl", [
                    ("mcp__brp__brp_extras_screenshot", "", "saved x.png"),
                ], str(cwd))
            calls = scan_calls(root / "claude", root / "codex")
        notes_call = next(call for call in calls if Path(call.transcript_path).stem == "notes")
        self.assertEqual((notes_call.project, notes_call.project_attribution), ("hana-notes", "last_path"))

    def test_shell_rm_forgets_remembered_script_after_cd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _claude_tools(root / "claude" / "removed.jsonl", [
                ("Write", {"file_path": "shots/shot.sh", "content": "grim /tmp/view.png"}, False),
                ("Bash", {"command": "cd shots && rm -f shot.sh"}, False),
                ("Bash", {"command": "bash shots/shot.sh"}, False),
            ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual(calls, [])

    def test_same_command_removal_precedes_later_script_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for session, command in (("before", "rm shot.sh; bash shot.sh"),
                                     ("after", "bash shot.sh; rm shot.sh")):
                _claude_tools(root / "claude" / f"{session}.jsonl", [
                    ("Write", {"file_path": "shot.sh", "content": "grim /tmp/view.png"}, False),
                    ("Bash", {"command": command}, False),
                ], "/fictional/studio")
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([(call.session_id, call.kind) for call in calls], [("after", "shot")])

    def test_claude_write_then_later_relative_python_run_counts(self) -> None:
        calls = self._script_calls("write-run")
        self.assertEqual([(call.kind, call.source, call.image_count) for call in calls], [
            ("shot", "bash_brp", 1),
        ])
        self.assertEqual([call.script_path for call in calls], ["/fictional/studio/shot.py"])

    def test_cat_heredoc_then_later_direct_run_counts(self) -> None:
        calls = self._script_calls("cat-run")
        self.assertEqual([(call.kind, call.source) for call in calls], [("shot", "bash_brp")])

    def test_codex_apply_patch_then_later_python_run_counts(self) -> None:
        calls = self._script_calls("patch-run")
        self.assertEqual([(call.agent, call.kind, call.source) for call in calls], [
            ("Codex", "shot", "bash_brp"),
        ])

    def test_script_sourcing_remembered_screenshot_script_counts(self) -> None:
        calls = self._script_calls("source-run")
        self.assertEqual([(call.kind, call.source) for call in calls], [("shot", "bash_brp")])

    def test_written_script_without_run_does_not_count(self) -> None:
        self.assertEqual(self._script_calls("write-only"), [])

    def test_edit_removing_screenshot_call_prevents_later_run(self) -> None:
        self.assertEqual(self._script_calls("edit-remove"), [])

    def test_cat_append_adds_screenshot_call_to_later_run(self) -> None:
        calls = self._script_calls("cat-append")
        self.assertEqual([(call.kind, call.source) for call in calls], [("shot", "bash_brp")])

    def test_write_replacement_removes_screenshot_call(self) -> None:
        self.assertEqual(self._script_calls("write-replace"), [])

    def test_patch_deletion_removes_screenshot_call(self) -> None:
        self.assertEqual(self._script_calls("patch-delete"), [])

    def test_loop_with_three_screenshot_calls_counts_three_images(self) -> None:
        calls = self._script_calls("loop-three")
        self.assertEqual([(call.kind, call.image_count) for call in calls], [("shot", 3)])

    def test_script_result_with_two_distinct_png_paths_counts_two_images(self) -> None:
        calls = self._script_calls("two-png")
        self.assertEqual([(call.kind, call.image_count) for call in calls], [("shot", 2)])

    def test_script_with_two_screenshot_calls_and_no_paths_counts_two_images(self) -> None:
        calls = self._script_calls("two-in-script")
        self.assertEqual([(call.kind, call.image_count) for call in calls], [("shot", 2)])

    def test_later_run_of_related_brp_script_is_related_call(self) -> None:
        calls = self._script_calls("related-run")
        self.assertEqual([(call.kind, call.source, call.image_count) for call in calls], [
            ("other", "bash_brp", 0),
        ])

    def test_second_pending_id_on_multi_result_line_is_read(self) -> None:
        calls = self._script_calls("multi-result")
        self.assertEqual([(call.kind, call.image_count) for call in calls], [("shot", 1)])

    def test_scratchpad_cwd_uses_encoded_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "widget"
            worktree = root / "widget-enhancements"
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _ = subprocess.run([
                "git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-q", "--allow-empty", "-m", "initial",
            ], check=True)
            _ = subprocess.run([
                "git", "-C", str(repo), "worktree", "add", "-q", "-b", "enhancements", str(worktree),
            ], check=True)
            encoded = re.sub(r"[^A-Za-z0-9]", "-", str(worktree))
            cwd = f"/tmp/claude-1000/{encoded}/session/scratchpad/run"
            _claude_case(root / "claude" / "scratchpad.jsonl", [
                ("mcp__brp__brp_extras_screenshot", "", "saved /tmp/pad.png"),
            ], cwd)
            calls = scan_calls(root / "claude", root / "codex")
        self.assertEqual([call.project for call in calls], ["widget"])
        self.assertEqual([call.project_attribution for call in calls], ["scratchpad"])

    def test_timeout_wrapper_runs_remembered_script(self) -> None:
        calls = self._script_calls("wrapper-run")
        self.assertEqual([(call.kind, call.source) for call in calls], [("shot", "bash_brp")])

    def test_scripted_scan_saves_episodes_and_survey(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            result = subprocess.run(
                [
                    sys.executable, str(Path(__file__).parent / "shot_report.py"), "scan",
                    "--state-dir", str(state_dir),
                    "--claude-root", str(CLASSIFY / "claude" / "projects"),
                    "--codex-root", str(CLASSIFY / "codex" / "sessions"),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            episodes = [cast(dict[str, object], json.loads(line)) for line in
                        (state_dir / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
            survey = cast(dict[str, list[int]], json.loads(
                (state_dir / "survey.json").read_text(encoding="utf-8")
            ))
        by_session = {
            str(episode["session_id"]): episode for episode in episodes
            if episode["split"] == 300
        }
        self.assertEqual(by_session["write-run"]["screenshot_count"], 1)
        self.assertEqual(by_session["patch-run"]["screenshot_count"], 1)
        self.assertEqual(by_session["loop-three"]["image_count"], 3)
        self.assertEqual(by_session["two-png"]["image_count"], 2)
        self.assertNotIn("write-only", by_session)
        self.assertNotIn("edit-remove", by_session)
        self.assertEqual(survey["bash_brp"], [10, 10, 1])

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
            live = next(call for call in calls if Path(call.transcript_path).stem == "live")
            self.assertEqual(live.project_attribution, "repository")

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
