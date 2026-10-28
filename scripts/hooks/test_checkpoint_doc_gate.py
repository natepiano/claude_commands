#!/usr/bin/env python3
"""Checkpoint permission follows the committed Git tree and durable rustdoc verdict."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import cast, override
import unittest

HOOK = Path(__file__).with_name("pre-tool-use-checkpoint-doc-gate.py")
CHECKPOINT = 'git commit -m "checkpoint(efficiency): phase 1"'
DOC_COMMAND = "~/.claude/scripts/direct/verify.sh doc"


class CheckpointDocGateTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "cargo repo"
        self.repo.mkdir()
        home = self.root / "home"
        home.mkdir()
        self.records = home / ".local/state/verify/doc_pass"
        self.records.mkdir(parents=True)
        self.environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith("GIT_")
        }
        self.environment.update({"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
                                 "XDG_STATE_HOME": str(self.root / "xdg-state"),
                                 "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        _ = (self.repo / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\n', encoding="utf-8",
        )
        _ = (self.repo / "lib.rs").write_text("pub fn base() {}\n", encoding="utf-8")
        _ = self.git("init", "-q", "-b", "test")
        _ = self.git("add", "-A")
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "base")
        _ = (self.repo / "lib.rs").write_text("pub fn staged() {}\n", encoding="utf-8")
        _ = self.git("add", "lib.rs")

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], env=self.environment,
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()

    def record(self, *, repo: str = "", tree: str = "") -> Path:
        judged_tree = tree or self.git("write-tree")
        path = self.records / f"{judged_tree}.json"
        _ = path.write_text(json.dumps({"repo_top": repo or str(self.repo), "tree_id": judged_tree,
                                        "time": datetime.now(timezone.utc).isoformat(),
                                        "call_id": "fixture-doc-call"}),
                            encoding="utf-8")
        return path

    def decision(self, command: str, *, cwd: Path | None = None) -> dict[str, str]:
        payload = {"tool_name": "Bash", "cwd": str(cwd or self.repo),
                   "tool_input": {"command": command}}
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                                env=self.environment, capture_output=True, text=True, check=False,
                                timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if not result.stdout.strip():
            return {}
        response = cast(dict[str, dict[str, str]], json.loads(result.stdout))
        return response.get("hookSpecificOutput", {})

    def assert_allowed(self, command: str, *, cwd: Path | None = None) -> None:
        decision = self.decision(command, cwd=cwd)
        self.assertNotEqual(decision.get("permissionDecision"), "deny", decision)

    def assert_denied(self, command: str, *, cwd: Path | None = None) -> str:
        decision = self.decision(command, cwd=cwd)
        self.assertEqual(decision.get("permissionDecision"), "deny", decision)
        self.assertEqual(decision.get("hookEventName"), "PreToolUse")
        reason = decision.get("permissionDecisionReason", "")
        self.assertIn(DOC_COMMAND, reason)
        return reason

    def test_other_commits_and_commands_bypass_broken_record_storage(self) -> None:
        self.records.rmdir()
        _ = self.records.write_text("not a directory", encoding="utf-8")
        for command in ('git commit -m "fix: update documentation"', "git status"):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_checkpoint_outside_a_cargo_repository_bypasses_record_storage(self) -> None:
        (self.repo / "Cargo.toml").unlink()
        self.records.rmdir()
        _ = self.records.write_text("not a directory", encoding="utf-8")
        self.assert_allowed(CHECKPOINT)
        self.assert_allowed(CHECKPOINT, cwd=self.root)

    def test_non_cargo_checkpoint_requires_its_own_call_without_a_doc_record(self) -> None:
        _ = self.git("rm", "Cargo.toml")
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "remove Cargo manifest")
        _ = (self.repo / "x").write_text("staged later\n", encoding="utf-8")
        reason = self.assert_denied("git add x && " + CHECKPOINT)
        self.assertIn("first command of its own Bash call", reason)
        self.assertIn("In a repo with a Cargo.toml at its top", reason)
        self.assertNotIn("as the last command before the commit", reason)
        self.assertEqual(list(self.records.iterdir()), [])
        self.assertEqual(self.decision(CHECKPOINT), {})

    def test_uncertain_repository_refusals_make_doc_guidance_conditional(self) -> None:
        (self.repo / "Cargo.toml").unlink()
        commands = (
            "GIT_DIR=.git " + CHECKPOINT,
            'echo ready && git commit -m "$SUBJECT"',
        )
        for command in commands:
            with self.subTest(command=command):
                reason = self.assert_denied(command)
                self.assertIn("first command of its own Bash call", reason)
                self.assertIn("In a repo with a Cargo.toml at its top", reason)
                self.assertNotIn("as the last command before the commit", reason)
                if "$SUBJECT" in command:
                    self.assertIn("or, in a repo with a Cargo.toml at its top, run " + DOC_COMMAND, reason)

    def test_unknown_commit_directory_only_adds_conditional_doc_guidance(self) -> None:
        (self.repo / "Cargo.toml").unlink()
        reason = self.assert_denied('git -C "$REPO" commit -m "checkpoint(efficiency): phase 1"')
        self.assertIn("use a literal directory", reason)
        self.assertIn("In a repo with a Cargo.toml at its top, also run " + DOC_COMMAND + " in an earlier call.", reason)
        self.assertNotIn("first command of its own Bash call", reason)
        self.assertNotIn("as the last command before the commit", reason)

    def test_checkpoint_with_a_pass_for_the_index_tree_is_allowed(self) -> None:
        _ = self.record()
        before = (self.repo / ".git/index").read_bytes()
        self.assert_allowed(CHECKPOINT)
        self.assertEqual((self.repo / ".git/index").read_bytes(), before)

    def test_checkpoint_without_a_record_is_denied(self) -> None:
        reason = self.assert_denied(CHECKPOINT)
        self.assertIn("tree", reason.lower())
        self.assertIn("Run " + DOC_COMMAND + " as the last command before the commit.", reason)

    def test_pass_for_a_different_tree_does_not_authorize_the_index(self) -> None:
        _ = self.record(tree=self.git("rev-parse", "HEAD^{tree}"))
        _ = self.assert_denied(CHECKPOINT)

    def test_matching_tree_record_from_another_repository_is_denied(self) -> None:
        _ = self.record(repo=str(self.root / "another-repo"))
        _ = self.assert_denied(CHECKPOINT)

    def test_commit_all_requires_the_tracked_working_tree_without_mutating_the_index(self) -> None:
        _ = self.record()
        _ = (self.repo / "lib.rs").write_text("pub fn unstaged() {}\n", encoding="utf-8")
        before = (self.repo / ".git/index").read_bytes()
        for flag in ("-a", "--all", "-am"):
            command = (f'git commit {flag} "checkpoint(efficiency): phase 1"' if flag == "-am"
                       else f'git commit {flag} -m "checkpoint(efficiency): phase 1"')
            with self.subTest(flag=flag):
                _ = self.assert_denied(command)
                self.assertEqual((self.repo / ".git/index").read_bytes(), before)

    def test_commit_all_accepts_the_exact_tracked_tree_including_deletions(self) -> None:
        _ = (self.repo / "lib.rs").write_text("pub fn unstaged() {}\n", encoding="utf-8")
        # A second tracked file is deleted; Cargo.toml must remain for the gate to apply.
        _ = (self.repo / "obsolete.txt").write_text("old\n", encoding="utf-8")
        _ = self.git("add", "obsolete.txt")
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "add obsolete file")
        (self.repo / "obsolete.txt").unlink()
        _ = self.git("add", "-u")
        _ = self.record()
        _ = self.git("reset", "-q", "HEAD")
        before = (self.repo / ".git/index").read_bytes()
        self.assert_allowed('git commit --all -m "checkpoint(efficiency): phase 1"')
        self.assertEqual((self.repo / ".git/index").read_bytes(), before)

    def test_omitted_untracked_or_unstaged_files_are_named_in_the_fix(self) -> None:
        for name in ("lib.rs", "new-plan.md"):
            with self.subTest(path=name):
                _ = (self.repo / name).write_text("not staged\n", encoding="utf-8")
                reason = self.assert_denied(CHECKPOINT).lower()
                self.assertIn("commit", reason)
                self.assertIn("remove", reason)
                self.assertTrue("untracked" in reason or "unstaged" in reason, reason)

    def test_omitted_files_do_not_invalidate_a_matching_index_record(self) -> None:
        _ = self.record()
        _ = (self.repo / "lib.rs").write_text("pub fn unstaged() {}\n", encoding="utf-8")
        _ = (self.repo / "new-plan.md").write_text("untracked\n", encoding="utf-8")
        self.assert_allowed(CHECKPOINT)

    def test_message_forms_and_git_C_target_the_commit_repository(self) -> None:
        message = self.root / "commit message.txt"
        _ = message.write_text("checkpoint(efficiency): phase 1\n\nbody\n", encoding="utf-8")
        prefix = f"git -C {shlex.quote(str(self.repo))} commit"
        commands = (
            f'{prefix} -m "checkpoint(efficiency): phase 1"',
            f"{prefix} -F {shlex.quote(str(message))}",
            f"{prefix} -F - <<'EOF'\ncheckpoint(efficiency): phase 1\n\nbody\nEOF",
            f'{prefix} -m "$(cat <<\'EOF\'\ncheckpoint(efficiency): phase 1\n\nbody\nEOF\n)"',
        )
        for command in commands:
            with self.subTest(command=command):
                _ = self.assert_denied(command, cwd=self.root)
        _ = self.record()
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(command, cwd=self.root)

    def test_a_checkpoint_in_the_second_command_is_checked(self) -> None:
        for separator in (" && ", "; ", "\n"):
            command = 'git commit -m "fix: first command"' + separator + CHECKPOINT
            with self.subTest(separator=separator):
                _ = self.assert_denied(command)
        _ = self.record()
        _ = self.assert_denied("git status && " + CHECKPOINT)

    def test_staging_before_a_checkpoint_is_denied_even_with_an_index_record(self) -> None:
        _ = self.record()
        _ = (self.repo / "lib.rs").write_text("pub fn untested() {}\n", encoding="utf-8")
        for prefix in ("git add -A && ", "git add lib.rs; "):
            with self.subTest(prefix=prefix):
                reason = self.assert_denied(prefix + CHECKPOINT)
                self.assertIn("first command of its own Bash call", reason)
                self.assertIn("earlier calls", reason)

    def test_directory_changes_before_checkpoints_are_denied_outside_cargo(self) -> None:
        _ = self.record()
        for prefix in (f"cd {shlex.quote(str(self.repo))} && ", "echo ready; "):
            with self.subTest(prefix=prefix):
                reason = self.assert_denied(prefix + CHECKPOINT, cwd=self.root)
                self.assertIn("first command of its own Bash call", reason)

    def test_commands_after_a_checked_checkpoint_are_allowed(self) -> None:
        _ = self.record()
        self.assert_allowed(CHECKPOINT + " && git push")

    def test_attached_git_directory_options_check_the_target_repository(self) -> None:
        alias = self.root / "cargo-repo"
        alias.symlink_to(self.repo, target_is_directory=True)
        for option in (f'-C"{self.repo}"', "-C" + shlex.quote(str(self.repo))):
            command = "git " + option + ' commit -m "checkpoint(efficiency): phase 1"'
            with self.subTest(option=option):
                _ = self.assert_denied(command, cwd=self.root)
        _ = self.assert_denied(f'git -C{alias} commit -m "checkpoint(efficiency): phase 1"', cwd=self.root)
        _ = self.record()
        self.assert_allowed(f'git -C"{self.repo}" commit -m "checkpoint(efficiency): phase 1"', cwd=self.root)

    def test_concatenated_directory_words_check_the_complete_target_path(self) -> None:
        alias = self.root / "ab"
        alias.symlink_to(self.repo, target_is_directory=True)
        commands = (
            f'git -C"{self.root}/cargo "repo commit -m "checkpoint(efficiency): phase 1"',
            'git -C\'a\'"b" commit -m "checkpoint(efficiency): phase 1"',
            'git -C \'a\'"b" commit -m "checkpoint(efficiency): phase 1"',
            f'git -C{shlex.quote(str(self.root))} -C "cargo "repo commit -m "checkpoint(efficiency): phase 1"',
        )
        for command in commands:
            with self.subTest(command=command):
                _ = self.assert_denied(command, cwd=self.root)
        _ = self.record()
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(command, cwd=self.root)

    def test_repeated_directory_options_are_relative_to_the_previous_directory(self) -> None:
        parent = self.root / "dir1"
        parent.mkdir()
        (parent / "sub").symlink_to(self.repo, target_is_directory=True)
        command = 'git -C dir1 -C sub commit -m "checkpoint(efficiency): phase 1"'
        _ = self.assert_denied(command, cwd=self.root)
        _ = self.record()
        self.assert_allowed(command, cwd=self.root)

    def test_tilde_directory_options_check_the_expanded_repository(self) -> None:
        home = Path(self.environment["HOME"])
        (home / "repo").symlink_to(self.repo, target_is_directory=True)
        account = pwd.getpwuid(os.getuid())
        relative = os.path.relpath(self.repo, account.pw_dir)
        commands = (
            'git -C ~/repo commit -m "checkpoint(efficiency): phase 1"',
            f'git -C ~{account.pw_name}/{shlex.quote(relative)} commit -m "checkpoint(efficiency): phase 1"',
        )
        for command in commands:
            with self.subTest(command=command):
                _ = self.assert_denied(command, cwd=self.root)
        _ = self.record()
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(command, cwd=self.root)

    def test_quoted_or_attached_tildes_cannot_use_an_expanded_directory_record(self) -> None:
        home = Path(self.environment["HOME"])
        (home / "repo").symlink_to(self.repo, target_is_directory=True)
        _ = self.record()
        for option in ('-C "~/repo"', "-C '~'/repo", "-C~/repo"):
            with self.subTest(option=option):
                reason = self.assert_denied("git " + option + ' commit -m "checkpoint(efficiency): phase 1"',
                                            cwd=self.root)
                self.assertIn("literal directory", reason)

    def test_unquoted_directory_expansions_refuse_checkpoints_even_with_a_record(self) -> None:
        _ = self.record()
        for directory in ("cargo*", "cargo?", "[c]argo", "{cargo,other}", '"cargo"*',
                          '"cargo"?', '"cargo"[a]', '"cargo"{a,b}', "$REPO", "`pwd`"):
            for option in ("-C " + directory, "-C" + directory):
                with self.subTest(option=option):
                    reason = self.assert_denied("git " + option + ' commit -m "checkpoint(efficiency): phase 1"',
                                                cwd=self.root)
                    self.assertIn("literal directory", reason)
                    _ = self.assert_denied("git " + option + ' commit -m "$SUBJECT"', cwd=self.root)
                    self.assert_allowed("git " + option + ' commit -m "fix: ordinary"', cwd=self.root)

    def test_quoted_or_escaped_directory_globs_are_literal_paths(self) -> None:
        alias = self.root / "cargo*?[{"
        alias.symlink_to(self.repo, target_is_directory=True)
        _ = self.record()
        for option in ("-C 'cargo*?[{'", '-C"cargo*?[{"', r"-C cargo\*\?\[\{"):
            with self.subTest(option=option):
                self.assert_allowed("git " + option + ' commit -m "checkpoint(efficiency): phase 1"',
                                    cwd=self.root)

    def test_unparseable_directory_options_refuse_checkpoints_and_unknown_messages(self) -> None:
        _ = self.record()
        for message in ('-m "checkpoint(efficiency): phase 1"', '-m "$SUBJECT"'):
            command = 'git -C "unterminated commit ' + message
            with self.subTest(message=message):
                reason = self.assert_denied(command, cwd=self.root)
                self.assertIn("directory options could not be parsed", reason)

    def test_checkpoint_text_on_any_message_line_requires_the_record(self) -> None:
        checkpoint = "checkpoint(efficiency): phase 1"
        message = self.root / "multiline-message.txt"
        _ = message.write_text("\nfix: heading\n\n  " + checkpoint + "\n", encoding="utf-8")
        commands = (
            'git commit -F - <<\'EOF\'\n\n' + checkpoint + '\nEOF',
            'git commit -m "\n' + checkpoint + '"',
            'git commit -m "fix: heading" -m "  ' + checkpoint + '"',
            'git commit --cleanup=verbatim -m "\n\n' + checkpoint + '"',
            'git commit --cleanup verbatim -m "\n\n' + checkpoint + '"',
            'git commit -F ' + shlex.quote(str(message)),
            'git commit -m "$(cat <<\'EOF\'\n\nfix: heading\n\n  ' + checkpoint + '\nEOF\n)"',
        )
        for command in commands:
            with self.subTest(command=command):
                _ = self.assert_denied(command)
        _ = self.record()
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_quote_removal_checkpoint_forms_require_a_matching_record(self) -> None:
        commands = (
            'git commit -m "check""point(x): phase 1"',
            "git commit -m 'check'\"point(x): phase 1\"",
            r"git commit -m check\point\(x\):",
            'git commit -m "check\\\npoint(x): phase 1"',
        )
        for command in commands:
            with self.subTest(command=command):
                _ = self.assert_denied(command)
        _ = self.record()
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_quote_removed_checkpoint_text_in_an_echo_gates_the_whole_call(self) -> None:
        _ = self.record()
        command = 'echo "check""point(x): example" && git commit -m "fix: ordinary"'
        reason = self.assert_denied(command)
        self.assertIn("first command of its own Bash call", reason)

    def test_checkpoint_text_in_either_of_two_heredocs_requires_the_record(self) -> None:
        checkpoint = "checkpoint(efficiency): phase 1"
        commands = (
            "git commit -F - <<'FIRST' <<'SECOND'\nfix: ordinary\nFIRST\n" + checkpoint + "\nSECOND",
            "git commit -F - <<'FIRST' <<'SECOND'\n" + checkpoint + "\nFIRST\nfix: ordinary\nSECOND",
        )
        for command in commands:
            with self.subTest(command=command):
                _ = self.assert_denied(command)
        _ = self.record()
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_checkpoint_text_in_an_earlier_echo_requires_its_own_commit_call(self) -> None:
        _ = self.record()
        command = 'echo "checkpoint(efficiency): example" && git commit -m "fix: ordinary"'
        for directory in (self.repo, self.root):
            with self.subTest(cwd=directory):
                reason = self.assert_denied(command, cwd=directory)
                self.assertIn("first command of its own Bash call", reason)

    def test_checkpoint_text_after_a_commit_still_requires_its_record(self) -> None:
        command = 'git commit -m "fix: ordinary" && echo "checkpoint(efficiency): example"'
        _ = self.assert_denied(command)
        _ = self.record()
        self.assert_allowed(command)

    def test_repository_and_index_overrides_are_named_and_denied(self) -> None:
        _ = self.record()
        commands = (
            f'git --git-dir="{self.repo / ".git"}" commit -m "checkpoint(efficiency): phase 1"',
            f'git --work-tree="{self.repo}" commit -m "checkpoint(efficiency): phase 1"',
            "GIT_DIR=.git " + CHECKPOINT,
            "GIT_INDEX_FILE=.git/index " + CHECKPOINT,
            "GIT_WORK_TREE=. " + CHECKPOINT,
        )
        for command in commands:
            with self.subTest(command=command):
                reason = self.assert_denied(command)
                self.assertIn("--git-dir", reason)
                self.assertIn("--work-tree", reason)
                self.assertIn("GIT_INDEX_FILE", reason)

    def test_expanded_git_directory_cannot_hide_a_cargo_checkpoint(self) -> None:
        reason = self.assert_denied('git -C"$REPO" commit -m "checkpoint(efficiency): phase 1"', cwd=self.root)
        self.assertIn("literal directory", reason)

    def test_attached_message_and_revision_text_do_not_enable_commit_all(self) -> None:
        _ = self.record()
        _ = (self.repo / "lib.rs").write_text("pub fn unstaged() {}\n", encoding="utf-8")
        _ = self.git("branch", "a-checkpoint", "HEAD")
        self.assert_allowed('git commit -m"checkpoint(efficiency): a phase"')
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "--amend", "--no-verify", "-q", "-m", "checkpoint(efficiency): previous")
        _ = self.git("branch", "-f", "a-checkpoint", "HEAD")
        self.assert_allowed("git commit -Ca-checkpoint")

    def test_shell_expanded_subjects_require_a_record_and_explain_literal_forms(self) -> None:
        commands = (
            "git commit -m $'checkpoint(efficiency): phase 1'",
            'git commit -m "$VAR"',
            'git commit -m "`printf checkpoint`"',
            'git commit -m `printf checkpoint`',
            'git commit --message="$VAR"',
            'git commit -F "$MESSAGE_FILE"',
            'git commit',
        )
        for command in commands:
            with self.subTest(command=command):
                reason = self.assert_denied(command)
                self.assertIn("literal -m", reason)
                self.assertIn("-F", reason)
                self.assertIn("heredoc", reason)
        _ = self.record()
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_single_quoted_dollars_and_backticks_are_literal_subjects(self) -> None:
        self.assert_allowed("git commit -m 'fix: $VAR and `example`'")
        _ = self.assert_denied("git commit -m 'checkpoint(efficiency): $VAR and `example`'")

    def test_unreadable_subject_after_another_command_is_denied_without_cargo(self) -> None:
        reason = self.assert_denied('echo ready && git commit -m "$VAR"', cwd=self.root)
        self.assertIn("first command of its own Bash call", reason)

    def test_message_file_after_another_command_cannot_hide_a_checkpoint(self) -> None:
        message = self.root / "message.txt"
        _ = message.write_text("fix: current text\n", encoding="utf-8")
        reason = self.assert_denied(f'echo changed > {message} && git commit -F {message}')
        self.assertIn("first command of its own Bash call", reason)

    def test_amend_and_reused_messages_check_checkpoint_subjects(self) -> None:
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "--amend", "--no-verify", "-q", "-m", "checkpoint(efficiency): previous")
        commands = ("git commit --amend --no-edit", "git commit --amend", "git commit -C HEAD",
                    "git commit -c HEAD", "git commit --reuse-message=HEAD", "git commit --reedit-message HEAD")
        for command in commands:
            with self.subTest(command=command):
                _ = self.assert_denied(command)
        _ = self.record()
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_amending_and_reusing_a_shrink_subject_bypass_records(self) -> None:
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "--amend", "--no-verify", "-q", "-m", "shrink(efficiency): previous")
        self.assert_allowed("git commit --amend --no-edit")
        self.assert_allowed("git commit -C HEAD")

    def test_merge_and_squash_message_files_supply_the_checkpoint_subject(self) -> None:
        for name in ("MERGE_MSG", "SQUASH_MSG"):
            path = self.repo / ".git" / name
            _ = path.write_text("\nfix: heading\n\n  checkpoint(efficiency): merge result\n", encoding="utf-8")
            for command in ("git commit --no-edit", "git commit"):
                with self.subTest(file=name, command=command):
                    _ = self.assert_denied(command)
            path.unlink()
        _ = self.record()
        _ = (self.repo / ".git/MERGE_MSG").write_text("checkpoint(efficiency): merge result\n", encoding="utf-8")
        self.assert_allowed("git commit --no-edit")

    def test_autosquash_subjects_are_not_checkpoints(self) -> None:
        for option in ("--fixup=HEAD", "--squash HEAD"):
            self.assert_allowed("git commit " + option)
        _ = self.assert_denied(CHECKPOINT + ' -m "--fixup=HEAD"')

    def test_missing_parser_only_blocks_calls_that_might_commit(self) -> None:
        isolated = self.root / "isolated-hook.py"
        _ = shutil.copyfile(HOOK, isolated)
        for command in ("ls", "echo commit", CHECKPOINT):
            payload = {"tool_name": "Bash", "cwd": str(self.root / "commit-cache"), "tool_input": {"command": command}}
            result = subprocess.run([sys.executable, str(isolated)], input=json.dumps(payload),
                                    env=self.environment, capture_output=True, text=True, check=True, timeout=20)
            if command != CHECKPOINT:
                self.assertEqual(result.stdout, "")
            else:
                response = cast(dict[str, dict[str, str]], json.loads(result.stdout))
                self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("FileNotFoundError", response["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_later_commits_all_flag_does_not_change_the_checkpoint_tree(self) -> None:
        _ = self.record()
        _ = (self.repo / "lib.rs").write_text("pub fn unstaged() {}\n", encoding="utf-8")
        # Raw checkpoint text now gates every commit. The first commit still
        # matches the index record; the second must be made in its own call.
        reason = self.assert_denied(CHECKPOINT + ' && git commit -am "fix: later"')
        self.assertIn("first command of its own Bash call", reason)

    def test_invalid_or_unreadable_matching_record_fails_closed(self) -> None:
        path = self.record()
        _ = path.write_text("not json", encoding="utf-8")
        reason = self.assert_denied(CHECKPOINT)
        self.assertTrue("record" in reason.lower() or "json" in reason.lower(), reason)
        path.unlink()
        path.mkdir()
        reason = self.assert_denied(CHECKPOINT)
        self.assertTrue("record" in reason.lower() or "directory" in reason.lower(), reason)

    def test_git_failure_in_a_cargo_repository_fails_closed_with_its_cause(self) -> None:
        tools = self.root / "bin"
        tools.mkdir()
        git = tools / "git"
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True,
                                  check=True, timeout=10).stdout.strip()
        _ = git.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            + "if 'write-tree' in sys.argv:\n"
            + "    print('fixture index read failed', file=sys.stderr)\n"
            + "    raise SystemExit(42)\n"
            + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n", encoding="utf-8",
        )
        git.chmod(0o755)
        self.environment["PATH"] = f"{tools}{os.pathsep}{os.environ['PATH']}"
        reason = self.assert_denied(CHECKPOINT)
        self.assertIn("fixture index read failed", reason)


if __name__ == "__main__":
    _ = unittest.main()
