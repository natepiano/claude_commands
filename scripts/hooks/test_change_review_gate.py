"""A checkpoint commit without a Change-Review line is refused before it runs; other commits pass.

Every test stages files in a temporary git repository; none commits or reads the real list.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast, override

from scripts.lib.file_module import load_file_module
from scripts.production import change_review
from scripts.hooks import checkpoint_gate
from unittest import mock


HOOK = Path(__file__).with_name("pre-tool-use-change-review-gate.py")
module = load_file_module("change_review_gate", HOOK)
spec = module.__spec__
assert spec is not None and spec.loader is not None


class Gate(Protocol):
    def refusal(self, command: str, cwd: Path) -> str: ...


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

    def test_merge_messages_use_one_rule_for_all_subjects_and_trailers(self) -> None:
        message_file = self.repo / "merge-message"
        for subject in ("checkpoint(u): merge", "Merge topic"):
            for trailer in (SEE, change_review.COVERS + ": not-a-hash"):
                message = subject + "\n\n" + trailer
                _ = message_file.write_text(message)
                for form in ("-m " + shlex.quote(message), "--no-ff -m " + shlex.quote(message),
                             "-F " + shlex.quote(str(message_file)), "--no-commit -m " + shlex.quote(message)):
                    with self.subTest(subject=subject, form=form):
                        self.assertEqual(self.refused("git merge topic " + form),
                                         change_review.merge_review_problem(message, is_merge=True))
        self.assertEqual(self.refused("git merge --squash topic -m " + shlex.quote(SEE)), "")
        self.assertEqual(self.refused("git merge topic -m 'Merge topic'"), "")

    def test_merge_with_a_none_line_passes(self) -> None:
        self.assertEqual(self.refused('git merge -m "checkpoint(x): merge" '
                                     + '-m "Change-Review: none — merge only" topic'), "")

    def test_message_words_do_not_become_options_and_parser_errors_refuse(self) -> None:
        self.assertIn("Merge commits cannot carry", self.refused("git merge topic -m '--squash' -m " + shlex.quote(SEE)))
        self.assertEqual(self.refused("git commit -m 'fix: --amend'"), "")
        self.assertIn("shell expansion", self.refused('git merge topic -m "checkpoint(u): merge" -m "' + SEE + '"'))
        with mock.patch.object(checkpoint_gate, "git_flags", side_effect=ValueError("broken parser")):
            self.assertIn("broken parser", self.refused("git commit -m 'checkpoint(u): grid'"))
        self.assertEqual(self.refused('git commit -m "$MESSAGE"'), "")
        self.assertIn(checkpoint_gate.OWN_CALL_FIX, self.refused("git add . && git commit -m 'checkpoint(u): grid'"))

    def test_ordinary_chained_and_unreadable_commits_pass_without_cargo(self) -> None:
        for command in ("git status && git commit -m 'fix: repair'", "git commit",
                        'git commit -m "$MSG"', "echo $'it\\'s'", "git commit -F - <<'EOF'",
                        "git commit --fixup -a", "git commit -t message", "git status && git merge topic"):
            with self.subTest(command=command):
                self.assertEqual(self.refused(command), "")
        for flag in ("--abort", "--quit"):
            self.assertEqual(self.refused("git status && GIT_DIR=other git merge " + flag), "")
        _ = (self.repo / "Cargo.toml").write_text("[package]\n")
        self.assertIn("shell expansion", self.refused('git commit -m "$MSG"'))
        self.assertIn("no Change-Review line", self.refused("git commit -m 'checkpoint(u) grid'"))

    def test_one_quoted_stdin_heredoc_reads_all_file_forms(self) -> None:
        message = "checkpoint(u): grid\n\nChange-Review: none — scripts only"
        for option in ("-F -", "--file -", "-F-", "-aF -"):
            for delimiter in ("<<'EOF'", '<<"EOF"', "<<-'EOF'"):
                with self.subTest(option=option, delimiter=delimiter):
                    self.assertEqual(self.refused("git commit " + option + " " + delimiter
                                                  + "\n" + message + "\nEOF"), "")
        command = "git merge topic -m \"$(cat <<'EOF'\nMerge topic\n\n" + SEE + "\nEOF\n)\""
        self.assertEqual(self.refused(command), change_review.merge_review_problem("Merge topic\n" + SEE, is_merge=True))

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
        for command in ('git commit -m "checkpoint(u): grid" -m ' + shlex.quote(SEE),
                        "git commit -F - <<'EOF'\ncheckpoint(u): grid\n\nChange-Review: none — scripts only\nEOF"):
            self.assertEqual(self.refused(command), "", command)

    def test_help_passes_each_invocation_without_reading_the_repository(self) -> None:
        for cargo in (False, True):
            if cargo:
                _ = (self.repo / "Cargo.toml").write_text("[package]\n")
            for merging in (False, True):
                if merging:
                    _ = (self.repo / ".git/MERGE_HEAD").write_text("unknown\n")
                for subcommand in ("commit", "merge"):
                    for option in ("-h", "--help"):
                        with self.subTest(cargo=cargo, merging=merging, subcommand=subcommand, option=option):
                            with mock.patch.object(checkpoint_gate, "commit_directory", side_effect=AssertionError("read repository")):
                                self.assertEqual(self.refused("git " + subcommand + " " + option), "")
                if merging:
                    (self.repo / ".git/MERGE_HEAD").unlink()
        self.stage("a.py")
        self.assertEqual(checkpoint_gate.OWN_CALL_FIX, self.refused("git commit -h; git commit -m 'checkpoint(u): grid'"))
        self.assertIn("Merge commits cannot carry", self.refused("git merge -m '-h' -m " + shlex.quote(SEE)))

    def test_trailers_do_not_replace_checkpoint_amend_or_reuse_messages(self) -> None:
        self.stage("a.py")
        self.git("commit", "-qm", "checkpoint(u): grid")
        for option in ("--trailer ", "--trailer="):
            for trailer in (SEE, "Change-Review=Visible | Try | See", "Reviewed-by: Person", "Change-Review: none — tests"):
                argument = option + shlex.quote(trailer)
                for message in ("-m 'checkpoint(u): grid'", "--amend", "-C HEAD", "--reuse-message=HEAD"):
                    for arguments in (argument + " " + message, message + " " + argument):
                        with self.subTest(arguments=arguments):
                            self.assertEqual(self.refused("git commit " + arguments), checkpoint_gate.TRAILER_FIX)
        self.assertEqual(checkpoint_gate.commit_message("--amend --trailer 'Reviewed-by: Person'", self.repo, True),
                         checkpoint_gate.ReadableCommitMessage("checkpoint(u): grid"))
        self.assertEqual(self.refused("git commit -m 'fix: ordinary' --trailer 'Reviewed-by: Person'"), "")
        for command in ("git commit -m '$MSG'", 'git commit -m "$MSG"', "git commit -F missing", "git commit --amend"):
            self.assertEqual(self.refused("git status && " + command + " --trailer 'Reviewed-by: Person'"), checkpoint_gate.TRAILER_FIX)
        for command in ("git merge topic", "git merge --squash topic"):
            self.assertEqual(self.refused(command + " --trailer 'Reviewed-by: Person'"), checkpoint_gate.TRAILER_FIX)
        self.assertEqual(self.refused("git -C missing commit -m 'fix: ordinary' --trailer 'Reviewed-by: Person'"), checkpoint_gate.TRAILER_FIX)

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


class MergeInProgressFixture(unittest.TestCase):
    """A repository with an incoming branch merged and not committed."""

    root: Path = Path()
    repo: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _ = self.git("init", "-q", "-b", "unit")
        for branch, name in (("unit", "base.txt"), ("incoming", "incoming.txt"), ("unit", "local.txt")):
            if branch == "incoming":
                _ = self.git("checkout", "-q", "-b", "incoming")
            elif name == "local.txt":
                _ = self.git("checkout", "-q", "unit")
            _ = (self.repo / name).write_text(name + "\n", encoding="utf-8")
            _ = self.git("add", name)
            _ = self.git("commit", "-q", "-m", name)
        _ = self.git("merge", "-q", "--no-ff", "--no-commit", "incoming")

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments],
            capture_output=True, text=True, check=True, timeout=30,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        return result.stdout.strip()


class MergeCommitAgreementTests(MergeInProgressFixture):
    def test_pending_merge_refuses_covers_and_plain_commit_accepts_the_same_lines(self) -> None:
        message = "checkpoint(unit): merge incoming\n\n" + SEE + "\n" + change_review.COVERS + ": bad-hash"
        command = "git commit -m " + shlex.quote(message)
        self.assertEqual(gate.refusal(command, self.repo),
                         change_review.merge_review_problem(message, is_merge=True))
        _ = self.git("commit", "-qm", "Merge incoming")
        self.assertEqual(gate.refusal(command, self.repo), "")
        self.assertEqual(gate.refusal("git commit --amend -m " + shlex.quote(message), self.repo),
                         change_review.merge_review_problem(message, is_merge=True))

    def test_pending_merge_reads_both_trailer_forms(self) -> None:
        for trailer in (SEE, change_review.COVERS + ": bad-hash", "Reviewed-by=Person", "Change-Review=Visible | Try | See"):
            for option in ("--trailer ", "--trailer="):
                with self.subTest(trailer=trailer, option=option):
                    command = "git commit -m 'Merge incoming' " + option + shlex.quote(trailer)
                    self.assertEqual(gate.refusal(command, self.repo), checkpoint_gate.TRAILER_FIX)
        _ = self.git("commit", "-qm", "Merge incoming")
        for option in ("--trailer ", "--trailer="):
            self.assertEqual(gate.refusal("git commit -m 'checkpoint(unit): grid' "
                                         + option + shlex.quote(SEE), self.repo), checkpoint_gate.TRAILER_FIX)
            self.assertEqual(gate.refusal("git commit --amend -m 'fix: merge' "
                                         + option + "'Reviewed-by: Person'", self.repo), checkpoint_gate.TRAILER_FIX)

    def test_linked_worktree_uses_its_pending_merge_file(self) -> None:
        linked = self.root / "linked"
        _ = self.git("worktree", "add", "--detach", str(linked), "HEAD")
        _ = self.git("-C", str(linked), "merge", "--no-ff", "--no-commit", "incoming")
        self.assertTrue((linked / ".git").is_file())
        message = change_review.COVERS + ": malformed"
        self.assertEqual(gate.refusal("git commit -m " + shlex.quote(message), linked),
                         change_review.merge_review_problem(message, is_merge=True))

    def test_merge_continue_reads_the_saved_message(self) -> None:
        _ = (self.repo / ".git/MERGE_MSG").write_text(SEE + "\n")
        self.assertEqual(gate.refusal("git merge --continue", self.repo),
                         change_review.merge_review_problem(SEE, is_merge=True))
        for option in ("--abort", "--quit"):
            self.assertEqual(gate.refusal("git merge " + option, self.repo), "")


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
