"""The tool-output read guard refuses a sed or awk read, or a change or copy, of saved tool output that would prompt."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import TypedDict, cast
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


HOOK = Path(__file__).with_name("pre-tool-use-tool-output-read.py")
SAVED = "/home/someone/.claude3/projects/-etc-nixos/0a1b/tool-results/b1z6o3fxk.txt"


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


def run_hook(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
    )


def bash_call(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


class ToolOutputReadTests(unittest.TestCase):
    def assert_refused(self, command: str, program: str) -> None:
        result = run_hook(bash_call(command))
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(HookResponse, json.loads(result.stdout))["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        reason = decision["permissionDecisionReason"]
        self.assertIn(f"this {program} read of saved tool output asks for permission", reason)
        for rewrite in ("the Read tool", "grep -n PATTERN FILE", "tail -n +N FILE", "sed -n 'N,Mp' FILE"):
            self.assertIn(rewrite, reason)

    def assert_change_refused(self, command: str, program: str) -> None:
        result = run_hook(bash_call(command))
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(HookResponse, json.loads(result.stdout))["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        reason = decision["permissionDecisionReason"]
        self.assertIn(f"this {program} on saved tool output asks for permission", reason)
        self.assertIn("Leave saved output alone", reason)
        self.assertIn("`cat <file> > /tmp/claude/<name>`", reason)

    def assert_allowed(self, payload: str) -> None:
        result = run_hook(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_sed_scripts_that_prompt_are_refused(self) -> None:
        for script in ("5,$p", "$p", "/start/,$p", "/start/,/end/p", "s/a/b/p"):
            with self.subTest(script=script):
                self.assert_refused(f"sed -n '{script}' {SAVED}", "sed")
        self.assert_refused(f"sed -n -e '1,5p' -e '/x/p' {SAVED}", "sed")
        self.assert_refused(f"sed -n --expression='$p' {SAVED}", "sed")

    def test_awk_is_refused_also_after_a_pipe_or_a_newline(self) -> None:
        self.assert_refused(f"awk 'NR>=5' {SAVED}", "awk")
        self.assert_refused(f"cat {SAVED} | /usr/bin/gawk '{{print $1}}'", "gawk")
        self.assert_refused(f"wc -l {SAVED}\nawk 'NR>=5' {SAVED}", "awk")

    def test_changes_and_copies_of_saved_output_are_refused(self) -> None:
        for command, program in ((f"rm {SAVED}", "rm"), (f"/bin/rm -f {SAVED}", "rm"),
                                 (f"cp {SAVED} /tmp/claude/board.json", "cp"), (f"mv {SAVED} /tmp/x", "mv"),
                                 (f"echo x | tee -a {SAVED}", "tee"), (f"echo x > {SAVED}", ">"),
                                 (f"echo x>>{SAVED}", ">>"), (f"ls; date &> {SAVED}", "&>")):
            with self.subTest(command=command):
                self.assert_change_refused(command, program)

    def test_a_copy_through_cat_and_other_writes_pass(self) -> None:
        for command in (f"cat {SAVED} > /tmp/claude/board.json", f"grep -n x {SAVED} >> /tmp/claude/hits",
                        f"wc -l {SAVED}; rm /tmp/claude/old", f"cp {SAVED.replace('tool-results', 'notes')} /tmp/x"):
            with self.subTest(command=command):
                self.assert_allowed(bash_call(command))

    def test_reads_that_run_with_no_prompt_pass(self) -> None:
        for command in (f"sed -n '1,5p' {SAVED}", f"sed -n 12p {SAVED}", f"sed -n -e '3,9p' {SAVED}",
                        f"grep -n -A3 awk {SAVED}", f"tail -n +5 {SAVED}", f"head -40 {SAVED} | tail -5",
                        f"cat {SAVED}", f"echo 'sed and awk' {SAVED}"):
            with self.subTest(command=command):
                self.assert_allowed(bash_call(command))

    def test_other_files_and_other_tools_pass(self) -> None:
        self.assert_allowed(bash_call("sed -n '5,$p' /tmp/notes.txt"))
        self.assert_allowed(bash_call("awk 'NR>=5' ~/.claude/projects/x/session.jsonl"))
        self.assert_allowed(json.dumps({"tool_name": "Read", "tool_input": {"file_path": SAVED}}))

    def test_input_it_cannot_read_passes(self) -> None:
        self.assert_allowed("not json")
        self.assert_allowed(json.dumps(["Bash"]))
        self.assert_allowed(bash_call(f"sed -n '5,$p {SAVED}"))


if __name__ == "__main__":
    _ = unittest.main()
