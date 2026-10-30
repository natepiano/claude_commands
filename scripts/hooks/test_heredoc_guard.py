"""The heredoc guard refuses an unquoted heredoc whose body would run a command."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import TypedDict, cast
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


HOOK = Path(__file__).with_name("pre-tool-use-heredoc-guard.py")


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


def bash_call(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def run_hook(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOK)], input=payload, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
    )


class HeredocGuardTests(unittest.TestCase):
    def assert_refused(self, command: str, delimiter: str) -> str:
        result = run_hook(bash_call(command))
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn(f"unquoted heredoc delimiter {delimiter}", decision["permissionDecisionReason"])
        return decision["permissionDecisionReason"]

    def assert_allowed(self, payload: str) -> None:
        result = run_hook(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_tonights_merge_abort_note_is_refused(self) -> None:
        command = (
            "cat >> docs/unit-notes.md <<EOF\n"
            + "## Merge in progress\n"
            + "NO EDITS until `git merge --abort` has run\n"
            + "EOF"
        )
        reason = self.assert_refused(command, "<<EOF")
        self.assertEqual(
            reason,
            "Refused: unquoted heredoc delimiter <<EOF; the shell would run the backticked "
            + "or $( ) text in the body as a command.\n"
            + "Quote the delimiter (<<'EOF') to keep the body literal, "
            + "or move the command out of the heredoc if it really is meant to run.",
        )

    def test_dollar_paren_in_unquoted_body_is_refused(self) -> None:
        _ = self.assert_refused("cat > notes.md <<NOTE\nwritten at $(date)\nNOTE", "<<NOTE")

    def test_dash_operator_with_tab_indented_terminator_is_refused(self) -> None:
        reason = self.assert_refused("cat <<-EOF\n\tuser `id -un`\n\tEOF\necho done", "<<-EOF")
        self.assertIn("(<<-'EOF')", reason)

    def test_heredoc_inside_command_substitution_is_refused(self) -> None:
        _ = self.assert_refused("body=$(cat <<EOF\n$(hostname)\nEOF\n)", "<<EOF")

    def test_later_heredoc_is_checked_after_a_quoted_one(self) -> None:
        command = "cat <<'A' > a.txt\n`kept literal`\nA\ncat <<B > b.txt\n`runs`\nB"
        reason = self.assert_refused(command, "<<B")
        self.assertNotIn("<<A", reason)

    def test_unterminated_unquoted_body_is_refused(self) -> None:
        _ = self.assert_refused("cat <<EOF\n`id`", "<<EOF")

    def test_quoted_delimiters_keep_substitutions_literal(self) -> None:
        for word in ("'EOF'", '"EOF"', "\\EOF", 'E"OF"'):
            with self.subTest(word=word):
                command = f"cat > notes.md <<{word}\nNO EDITS until `git merge --abort` or $(reset)\nEOF"
                self.assert_allowed(bash_call(command))

    def test_plain_variable_expansion_is_allowed(self) -> None:
        self.assert_allowed(bash_call("cat > env.txt <<EOF\nhome=$HOME user=${USER}\nEOF"))

    def test_escaped_substitution_and_arithmetic_are_allowed(self) -> None:
        for body in ("price \\$(not run) and \\`literal\\`", "next=$((count + 1))"):
            with self.subTest(body=body):
                self.assert_allowed(bash_call(f"cat <<EOF\n{body}\nEOF"))

    def test_here_string_is_allowed(self) -> None:
        self.assert_allowed(bash_call('grep -c modified <<< "$(git status)"'))

    def test_shift_operators_outside_a_heredoc_are_allowed(self) -> None:
        commands = (
            'echo "a<<b"\necho `date`',
            "echo 'x<<EOF'\necho $(date)\nEOF",
            "echo $((1<<3))\necho $(date)",
            "((mask = 1<<3))\necho `date`",
            "echo hi # <<EOF\necho $(date)\nEOF",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(bash_call(command))

    def test_other_tools_and_unparseable_input_are_allowed(self) -> None:
        body = "cat <<EOF\n`id`\nEOF"
        payloads = (
            json.dumps({"tool_name": "Write", "tool_input": {"file_path": "x.sh", "content": body}}),
            "not json",
            json.dumps([body]),
            json.dumps({"tool_name": "Bash", "tool_input": None}),
            json.dumps({"tool_name": "Bash", "tool_input": {"command": [body]}}),
            bash_call("cat <<\n`id`\n"),
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_allowed(payload)


if __name__ == "__main__":
    _ = unittest.main()
