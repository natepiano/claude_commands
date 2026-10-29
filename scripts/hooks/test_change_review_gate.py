"""A checkpoint commit without a Change-Review line is refused before it runs; other commits pass.

Every test stages files in a temporary git repository; none commits or reads the real list.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast, override

HOOK = Path(__file__).with_name("pre-tool-use-change-review-gate.py")
spec = importlib.util.spec_from_file_location("change_review_gate", HOOK)
assert spec is not None and spec.loader is not None


class Gate(Protocol):
    def refusal(self, command: str, cwd: Path) -> str: ...


module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
gate = cast(Gate, cast(object, module))
SEE = ("Change-Review: The grid fades more gently | Run `hana` in a natedev terminal"
       + " | The grid lines fade out farther away")
FORM = "<what changed for you> | <how to try it: where and what to type> | <what you see>"


class RefusalTest(unittest.TestCase):
    repo: Path = Path()

    def git(self, *arguments: str) -> None:
        _ = subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                            "-c", "commit.gpgsign=false", *arguments], capture_output=True, check=True,
                           env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def stage(self, *names: str) -> None:
        for name in names:
            _ = (self.repo / name).write_text(name, encoding="utf-8")
        self.git("add", *names)

    @override
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.repo = Path(root.name)
        self.git("init", "-q", "-b", "unit")
        self.stage("base.py")
        self.git("commit", "-q", "-m", "base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def refused(self, command: str) -> str:
        return gate.refusal(command, self.repo)

    def test_a_checkpoint_without_a_line_is_refused_in_every_message_form(self) -> None:
        self.stage("a.py")
        message = self.repo / "message.txt"
        _ = message.write_text("checkpoint(u): grid\n\nbody\n", encoding="utf-8")
        for command in ('git commit -m "checkpoint(u): grid" -m "body"',
                        "git commit -F - <<'EOF'\ncheckpoint(u): grid\n\nbody\nEOF",
                        "git commit -m \"$(cat <<'EOF'\ncheckpoint(u): grid\n\nbody\nEOF\n)\"",
                        f"git -C {self.repo} commit -F {message}"):
            self.assertIn("no Change-Review line", self.refused(command), command)

    def test_a_malformed_line_is_refused(self) -> None:
        self.stage("a.py")
        self.assertIn(FORM, self.refused('git commit -m "checkpoint(u): grid" -m "Change-Review: the grid"'))

    def test_a_two_part_line_is_refused_with_the_three_part_form(self) -> None:
        self.stage("a.py")
        refused = self.refused('git commit -m "checkpoint(u): grid" -m "Change-Review: The grid fades | Look across the floor"')
        self.assertIn("needs three parts", refused)
        self.assertIn(FORM, refused)

    def test_a_checkpoint_without_any_line_names_the_three_part_form(self) -> None:
        self.stage("a.py")
        self.assertIn(FORM, self.refused('git commit -m "checkpoint(u): grid"'))

    def test_a_checkpoint_with_a_line_passes(self) -> None:
        self.stage("a.py")
        for command in (f'git commit -m "checkpoint(u): grid" -m "{SEE}"',
                        "git commit -F - <<'EOF'\ncheckpoint(u): grid\n\nChange-Review: none — scripts only\nEOF"):
            self.assertEqual(self.refused(command), "", command)

    def test_a_markdown_only_checkpoint_and_other_subjects_pass(self) -> None:
        self.stage("notes.md")
        self.assertEqual(self.refused('git commit -m "checkpoint(u): notes"'), "")
        self.stage("a.py")
        for command in ('git commit -m "as-built(u): docs"', 'git commit -m "merge(u): origin/main, after checkpoint(u): x"',
                        'git commit -m "ci(u): fix lint"', "git status", 'echo "checkpoint(u): x"'):
            self.assertEqual(self.refused(command), "", command)

    def test_commit_all_counts_the_tracked_changes_it_takes(self) -> None:
        self.stage("notes.md")
        self.git("commit", "-q", "-m", "notes")
        _ = (self.repo / "base.py").write_text("changed", encoding="utf-8")
        self.assertIn("no Change-Review line", self.refused('git commit -am "checkpoint(u): grid"'))

    def test_attached_directory_options_review_the_target_index(self) -> None:
        target = self.repo / "target-repo"
        target.mkdir()
        _ = subprocess.run(["git", "-C", str(target), "init", "-q"], capture_output=True, check=True)
        _ = (target / "notes.md").write_text("notes", encoding="utf-8")
        _ = subprocess.run(["git", "-C", str(target), "add", "notes.md"], capture_output=True, check=True)
        for option in (f'-C"{target}"', f"-C'{target}'", f"-C{target}"):
            self.assertEqual(self.refused(f'git {option} commit -m "checkpoint(u): notes"'), "")
        _ = (target / "code.py").write_text("changed", encoding="utf-8")
        _ = subprocess.run(["git", "-C", str(target), "add", "code.py"], capture_output=True, check=True)
        self.assertIn("no Change-Review line", self.refused(f'git -C"{target}" commit -m "checkpoint(u): code"'))

    def weaken_assertion(self, *, stage: bool = True) -> None:
        source = self.repo / "test_value.py"
        _ = source.write_text("def test_bottom(self):\n    self.assertEqual(bottom, expected)\n", encoding="utf-8")
        self.git("add", "test_value.py")
        self.git("commit", "-q", "-m", "test baseline")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        _ = source.write_text("def test_bottom(self):\n    self.assertLessEqual(bottom, expected)\n", encoding="utf-8")
        if stage:
            self.git("add", "test_value.py")

    def test_checkpoint_with_weaker_assertion_is_refused_after_change_review_passes(self) -> None:
        self.weaken_assertion()
        reason = self.refused('git commit -m "checkpoint(u): bounds" -m "Change-Review: none — tests only"')
        self.assertIn("test_value.py", reason)
        self.assertIn("test_bottom", reason)
        self.assertIn("weaker", reason)

    def test_checkpoint_with_assertion_ruling_in_message_passes(self) -> None:
        self.weaken_assertion()
        command = ('git commit -m "checkpoint(u): bounds" -m "Change-Review: none — tests only"'
                   + ' -m "Assertion-Change: test_value.py::test_bottom — Approved bound contract."')
        self.assertEqual(self.refused(command), "")

    def test_non_checkpoint_commit_does_not_check_weaker_assertions(self) -> None:
        self.weaken_assertion()
        self.assertEqual(self.refused('git commit -m "fix: bounds"'), "")

    def test_checkpoint_all_checks_unstaged_assertion_changes(self) -> None:
        self.weaken_assertion(stage=False)
        message = ' -m "checkpoint(u): bounds" -m "Change-Review: none — tests only"'
        self.assertEqual(self.refused("git commit" + message), "")
        self.assertIn("weaker", self.refused("git commit -a" + message))

    def test_checkpoint_guard_failure_refuses_when_no_default_base_exists(self) -> None:
        self.weaken_assertion()
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        reason = self.refused('git commit -m "checkpoint(u): bounds" -m "Change-Review: none — tests only"')
        self.assertIn("assertion guard could not run", reason)


class HookTest(unittest.TestCase):
    """Run as Claude Code runs it: a refusal is a deny, and a broken payload never blocks."""

    def run_hook(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, check=True)

    def test_a_refusal_is_a_deny_decision(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            payload = {"tool_name": "Bash", "cwd": root, "tool_input": {"command": 'git commit -m "checkpoint(u): grid"'}}
            response = cast(dict[str, dict[str, str]], json.loads(self.run_hook(json.dumps(payload)).stdout))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("Change-Review", decision["permissionDecisionReason"])

    def test_another_tool_and_a_broken_payload_pass(self) -> None:
        payload = {"tool_name": "Read", "tool_input": {"command": 'git commit -m "checkpoint(u): grid"'}}
        self.assertEqual(self.run_hook(json.dumps(payload)).stdout, "")
        broken = self.run_hook("not json")
        self.assertEqual(broken.stdout, "")
        self.assertIn("change review gate", broken.stderr)


if __name__ == "__main__":
    _ = unittest.main()
