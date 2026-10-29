#!/usr/bin/env python3
"""Checkpoint permission follows the committed Git tree and durable rustdoc verdict."""
from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from datetime import datetime, timezone
import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import ClassVar, cast, override
import unittest
from unittest import mock

HOOK = Path(__file__).with_name("pre-tool-use-checkpoint-doc-gate.py")
CHECKPOINT = 'git commit -m "checkpoint(efficiency): phase 1"'
DOC_COMMAND = "~/.claude/scripts/direct/verify.sh doc"
# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300
DECIDE = cast(Callable[[str], str], runpy.run_path(str(HOOK))["decide"])


class CheckpointDocGateTests(unittest.TestCase):
    seed: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.seed = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="checkpoint-doc-seed-"))) / "repo"
        cls.seed.mkdir()
        _ = (cls.seed / "Cargo.toml").write_text('[package]\nname = "sample"\nversion = "0.1.0"\n')
        _ = (cls.seed / "lib.rs").write_text("pub fn base() {}\n")
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        commands = (["init", "-q", "-b", "test"], ["add", "-A"],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "base"])
        for arguments in commands:
            _ = subprocess.run(["git", "-C", str(cls.seed), *arguments], env=environment,
                               capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
        _ = (cls.seed / "lib.rs").write_text("pub fn staged() {}\n")
        _ = subprocess.run(["git", "-C", str(cls.seed), "add", "lib.rs"], env=environment,
                           capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)

    root: Path = Path()
    repo: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "cargo repo"
        _ = shutil.copytree(self.seed, self.repo)
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

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], env=self.environment,
                              capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()

    def record(self, *, repo: str = "", tree: str = "") -> Path:
        judged_tree = tree or self.git("write-tree")
        path = self.records / f"{judged_tree}.json"
        _ = path.write_text(json.dumps({"repo_top": repo or str(self.repo), "tree_id": judged_tree,
                                        "time": datetime.now(timezone.utc).isoformat(),
                                        "call_id": "fixture-doc-call"}),
                            encoding="utf-8")
        return path

    def decision(self, command: str, *, cwd: Path | None = None, subprocess_cli: bool = False) -> dict[str, str]:
        payload = {"tool_name": "Bash", "cwd": str(cwd or self.repo),
                   "tool_input": {"command": command}}
        if not subprocess_cli:
            with mock.patch.dict(os.environ, self.environment, clear=True):
                reason = DECIDE(json.dumps(payload))
            return ({"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
                    if reason else {})
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                                env=self.environment, capture_output=True, text=True, check=False,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if not result.stdout.strip():
            return {}
        response = cast(dict[str, dict[str, str]], json.loads(result.stdout))
        return response.get("hookSpecificOutput", {})

    def assert_allowed(self, command: str, *, cwd: Path | None = None, subprocess_cli: bool = False) -> None:
        decision = self.decision(command, cwd=cwd, subprocess_cli=subprocess_cli)
        self.assertNotEqual(decision.get("permissionDecision"), "deny", decision)

    def assert_denied(self, command: str, *, cwd: Path | None = None, subprocess_cli: bool = False) -> str:
        decision = self.decision(command, cwd=cwd, subprocess_cli=subprocess_cli)
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
        reason = self.assert_denied("GIT_DIR=.git " + CHECKPOINT)
        self.assertIn("first command of its own Bash call", reason)
        self.assertIn("In a repo with a Cargo.toml at its top", reason)
        self.assertNotIn("as the last command before the commit", reason)
        self.assert_allowed('echo ready && git commit -m "$SUBJECT"')

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
        self.assert_allowed(CHECKPOINT, subprocess_cli=True)
        self.assertEqual((self.repo / ".git/index").read_bytes(), before)

    def test_staged_subset_requires_its_tree_instead_of_the_whole_working_tree(self) -> None:
        staged_tree = self.git("write-tree")
        _ = (self.repo / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.2.0"\n', encoding="utf-8",
        )
        _ = (self.repo / "notes.md").write_text("unstaged documentation\n", encoding="utf-8")
        index = self.repo / ".git/index"
        before = index.read_bytes()
        _ = self.git("add", "-A")
        working_tree = self.git("write-tree")
        _ = index.write_bytes(before)
        self.assertNotEqual(working_tree, staged_tree)
        whole_tree_record = self.record(tree=working_tree)
        _ = self.assert_denied(CHECKPOINT)
        whole_tree_record.unlink()
        _ = self.record(tree=staged_tree)
        self.assert_allowed(CHECKPOINT)
        self.assertEqual(index.read_bytes(), before)

    def test_selected_path_commits_require_the_tree_git_actually_commits(self) -> None:
        for name in ("selected.txt", "omitted.txt"):
            _ = (self.repo / name).write_text("base\n", encoding="utf-8")
        _ = self.git("add", "-A")
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "path selection base")
        for name in ("selected.txt", "omitted.txt"):
            _ = (self.repo / name).write_text("staged\n", encoding="utf-8")
        _ = self.git("add", "-A")
        _ = (self.repo / "selected.txt").write_text("working selection\n", encoding="utf-8")
        index = self.repo / ".git/index"
        before = index.read_bytes()
        parent = self.git("rev-parse", "HEAD")
        for selection in (("--only", "--", "selected.txt"), ("--", "selected.txt"),
                          ("-i", "--", "selected.txt")):
            command = CHECKPOINT + " " + shlex.join(selection)
            with self.subTest(selection=selection):
                _ = self.assert_denied(command)
                # Git itself supplies the expected commit tree, independently of the gate.
                _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                             "commit", "-q", "-m", "checkpoint(efficiency): phase 1", *selection)
                committed_tree = self.git("rev-parse", "HEAD^{tree}")
                _ = self.git("reset", "--soft", parent)
                _ = index.write_bytes(before)
                record = self.record(tree=committed_tree)
                self.assert_allowed(command)
                self.assertEqual(index.read_bytes(), before)
                record.unlink()

    def test_interactive_checkpoint_selection_is_refused_even_with_a_record(self) -> None:
        _ = self.record()
        for option in ("-p", "--patch", "--interactive"):
            with self.subTest(option=option):
                _ = self.assert_denied(CHECKPOINT + " " + option)

    def test_quoted_arguments_and_heredoc_bodies_are_not_git_commands(self) -> None:
        for command in (
            "send.py --text " + shlex.quote(CHECKPOINT),
            "echo " + shlex.quote(CHECKPOINT),
            "cat > note.txt <<'EOF'\n" + CHECKPOINT + "\nEOF",
            'echo "check""point(x): example" && git commit -m "fix: ordinary"',
            'echo "checkpoint(efficiency): example" && git commit -m "fix: ordinary"',
            'git commit -m "fix: ordinary" && echo "checkpoint(efficiency): example"',
            "send.py --text " + shlex.quote(CHECKPOINT) + ' && git commit -m "fix: ordinary"',
        ):
            with self.subTest(command=command):
                self.assertEqual(self.decision(command), {})

    def test_merge_checkpoint_script_with_git_log_substitution_passes(self) -> None:
        command = (
            'cd /home/natepiano/worktrees/cargo-liner && S=/tmp/checkpoint-session; '
            '$HOME/.claude/scripts/lib/py '
            '$HOME/.claude/scripts/production/merge_checkpoint.py '
            '--session-dir "$S" --phase 8 --commit 719c9b28 '
            '--started "$(git log -1 --format=%cI 719c9b28)"'
        )
        self.assertEqual(self.decision(command), {})

    def test_expanded_non_git_paths_and_argument_text_do_not_trigger_the_gate(self) -> None:
        checkpoint = shlex.quote(CHECKPOINT)
        commands = (
            'echo "checkpoint(x): commit message"',
            'echo "checkpoint(x): git commit message"',
            '$HOME/.claude/scripts/lib/py send.py --text ' + checkpoint,
            '"$HOME/.claude/scripts/lib/py" merge_checkpoint.py --text ' + checkpoint,
            '${HOME}/.claude/scripts/lib/py commit.py --text ' + checkpoint,
            '"${TOOLS}/py" merge.py --text ' + checkpoint,
            '$(which python3)/py --text ' + checkpoint,
            '$HOME/tools/git/helper.py --text ' + checkpoint,
            '$HOME/tools/git-status.py --text ' + checkpoint,
            '$PY script.py --text ' + checkpoint,
            'send.py --text "$MESSAGE" --title ' + checkpoint,
            'send.py --text "$(git log -1 --format=%s)" --title ' + checkpoint,
            '$HOME/.claude/scripts/lib/py send.py --text "$(git status)" --title ' + checkpoint,
        )
        for cwd in (self.repo, self.root):
            for command in commands:
                with self.subTest(cwd=cwd, command=command):
                    self.assertEqual(self.decision(command, cwd=cwd), {})

    def test_possible_expanded_git_words_and_eval_still_refuse_checkpoints(self) -> None:
        commands = (
            '$GIT commit -m "checkpoint(x): p"',
            '${GIT} commit -m "checkpoint(x): p"',
            '$PROGRAM commit -m "checkpoint(x): p"',
            '"$(command -v git)" commit -m "checkpoint(x): p"',
            '`command -v git` merge topic -m "checkpoint(x): p"',
            '$HOME/bin/git commit -m "checkpoint(x): p"',
            'git${SUFFIX} commit -m "checkpoint(x): p"',
            'eval "git commit -m \\"checkpoint(x): p\\""',
        )
        for cwd in (self.repo, self.root):
            for command in commands:
                with self.subTest(cwd=cwd, command=command):
                    reason = self.assert_denied(command, cwd=cwd)
                    self.assertIn("command word is expanded or eval-generated", reason)

    def test_expanded_non_git_command_keeps_real_git_substitutions_checked(self) -> None:
        for command in (
            '$HOME/.claude/scripts/lib/py send.py --text "$(' + CHECKPOINT + ')"',
            '$HOME/.claude/scripts/lib/py send.py --text `' + CHECKPOINT + '`',
            '$HOME/.claude/scripts/lib/py send.py --text ready; ' + CHECKPOINT,
        ):
            with self.subTest(command=command):
                reason = self.assert_denied(command)
                self.assertIn("first command of its own Bash call", reason)

    def test_literal_git_file_message_still_requires_the_doc_record(self) -> None:
        message = self.repo / "msg"
        _ = message.write_text("checkpoint(x): commit message\n", encoding="utf-8")
        reason = self.assert_denied("git commit -F msg")
        self.assertIn("no doc pass record", reason)
        _ = self.record()
        self.assertEqual(self.decision("git commit -F msg"), {})

    def test_real_git_command_words_are_checked_through_shell_prefixes(self) -> None:
        target = shlex.quote(str(self.repo))
        for command in (
            f'git -C {target} commit -m "checkpoint(efficiency): phase 1"',
            "FOO=1 " + CHECKPOINT,
            "cd x && " + CHECKPOINT,
            "$(" + CHECKPOINT + ")",
            "echo \"$(" + CHECKPOINT + ")\"",
            "`" + CHECKPOINT + "`",
            "env " + CHECKPOINT,
            "command " + CHECKPOINT,
            "exec " + CHECKPOINT,
            "sudo " + CHECKPOINT,
            "nice " + CHECKPOINT,
            "time " + CHECKPOINT,
            "echo ready; " + CHECKPOINT,
            "false || " + CHECKPOINT,
            "printf ready | " + CHECKPOINT,
            "(" + CHECKPOINT + ")",
            'git -c user.name=fixture --no-pager commit -m "checkpoint(efficiency): phase 1"',
        ):
            with self.subTest(command=command):
                _ = self.assert_denied(command)

    def test_eval_and_variable_command_words_fail_closed_even_with_a_record(self) -> None:
        _ = self.record()
        for command in ("eval " + shlex.quote(CHECKPOINT),
                        '$GIT commit -m "checkpoint(efficiency): phase 1"'):
            with self.subTest(command=command):
                _ = self.assert_denied(command)

    def test_opaque_commands_only_refuse_possible_cargo_checkpoints(self) -> None:
        opaque = ("eval " + shlex.quote(CHECKPOINT),
                  '$GIT commit -m "checkpoint(efficiency): phase 1"',
                  'eval \'git commit -m "$SUBJECT"\'')
        for command in opaque:
            with self.subTest(cargo=True, command=command):
                _ = self.assert_denied(command)
        for command in ('$EDITOR file', 'eval "printf ready"', '$PY script.py'):
            with self.subTest(cargo=True, unrelated=command):
                self.assert_allowed(command)
        (self.repo / "Cargo.toml").unlink()
        for command in opaque[:2]:
            with self.subTest(cargo=False, explicit_checkpoint=command):
                _ = self.assert_denied(command)
        for command in (opaque[2], '$EDITOR file', 'eval "printf ready"'):
            with self.subTest(cargo=False, command=command):
                self.assert_allowed(command)

    def test_merge_after_cd_is_gated_only_in_cargo_repositories(self) -> None:
        command = 'cd x && git merge y'
        # A default merge subject is ordinary in either kind of repository.
        self.assert_allowed(command)
        (self.repo / "Cargo.toml").unlink()
        self.assert_allowed(command)

    def test_opaque_explicit_checkpoints_refuse_with_an_unknown_target_in_every_cwd(self) -> None:
        target = shlex.quote(str(self.repo))
        text = f'git -C {target} commit -m "checkpoint(x): p"'
        commands = (
            "eval " + shlex.quote(text),
            '$GIT commit -m "checkpoint(x): p"',
            '"$(printf git)" commit -m "checkpoint(x): p"',
            "sh -c " + shlex.quote(text),
            "bash -lc " + shlex.quote(text),
            "/bin/zsh -c " + shlex.quote(text),
            "env --split-string=" + shlex.quote(text),
            "env --unknown " + text,
            "coproc worker " + text,
            "select item in one; do echo " + shlex.quote(text) + "; done",
            "[[ " + shlex.quote(text) + " == value ]]",
            "(( " + shlex.quote(text) + " ))",
            "for ((i=0; i<1; i++)); do echo " + shlex.quote(text) + "; done",
        )
        _ = self.record()
        for cwd in (self.repo, self.root):
            for command in commands:
                with self.subTest(cwd=cwd, command=command):
                    _ = self.assert_denied(command, cwd=cwd)

    def test_opaque_without_checkpoint_uses_only_its_own_words_and_cargo_cwd(self) -> None:
        for command in (
            "eval 'git commit'", "bash -c 'git merge topic -m \"$SUBJECT\"'",
            "env --unknown git commit", "select x in one; do echo 'git commit'; done",
            "[[ 'git commit' == value ]]", "(( 'git commit' ))",
            "for ((i=0; i<1; i++)); do echo 'git commit'; done",
        ):
            with self.subTest(command=command):
                _ = self.assert_denied(command)
                self.assert_allowed(command, cwd=self.root)
        for command in (
            '$EDITOR file', 'eval "$(direnv hook zsh)"', "bash -c 'printf ready'",
            "env --unknown printf ready", "coproc echo ready",
            "echo " + shlex.quote(CHECKPOINT) + "; eval 'printf ready'",
            "eval 'printf ready'; echo " + shlex.quote(CHECKPOINT),
            'eval "checkpoint(x): p"',
        ):
            for cwd in (self.repo, self.root):
                with self.subTest(cwd=cwd, command=command):
                    self.assert_allowed(command, cwd=cwd)

    def test_env_chdir_targets_the_repo_and_composes_with_git_chdir(self) -> None:
        target = shlex.quote(str(self.repo))
        prefixes = (f'env -C {target}', f'env --chdir={target}', f'env --chdir {target}',
                    f'env -C{target}', f'env -C {shlex.quote(str(self.root))}')
        commands = [prefix + " " + CHECKPOINT for prefix in prefixes[:-1]]
        commands.append(prefixes[-1] + ' git -C "cargo repo" commit -m "checkpoint(x): p"')
        for command in commands:
            with self.subTest(command=command):
                _ = self.assert_denied(command, cwd=self.root)
        _ = self.record()
        for command in commands:
            with self.subTest(record=True, command=command):
                self.assert_allowed(command, cwd=self.root)
        self.assert_allowed('env -C ' + shlex.quote(str(self.root)) + " " + CHECKPOINT)
        for directory in ('"$REPO"', 'missing-directory', '"cargo*"', 'cargo*'):
            with self.subTest(unreadable=directory):
                _ = self.assert_denied('env -C ' + directory + " " + CHECKPOINT, cwd=self.root)

    def test_merge_message_rules_in_cargo_and_non_cargo_repositories(self) -> None:
        message = self.root / "merge-message"
        _ = message.write_text("checkpoint(x): p\n", encoding="utf-8")
        _ = self.record()
        _ = (self.repo / ".git/MERGE_MSG").write_text("checkpoint(x): stale\n", encoding="utf-8")
        for cargo in (True, False):
            if not cargo:
                (self.repo / "Cargo.toml").unlink()
            for command in ('git merge topic', 'cd x && git merge topic',
                            'git merge topic --log', 'git merge topic --log=5',
                            'git merge topic -m "fix: ordinary"',
                            'cd x && git merge topic -m "fix: ordinary"'):
                with self.subTest(cargo=cargo, ordinary=command):
                    self.assert_allowed(command)
            for command in ('git merge topic -m "checkpoint(x): p"',
                            'git merge topic -F ' + shlex.quote(str(message))):
                with self.subTest(cargo=cargo, checkpoint=command):
                    if cargo:
                        _ = self.assert_denied(command)
                    else:
                        self.assert_allowed(command)
            reason = self.assert_denied('cd x && git merge topic -m "checkpoint(x): p"')
            self.assertIn("first command", reason)
            for command in ('git merge topic -F "$MESSAGE"', 'git merge topic -e',
                            'git merge topic --edit -m "fix: ordinary"',
                            'cd x && git merge topic -e'):
                with self.subTest(cargo=cargo, unreadable=command):
                    if cargo:
                        _ = self.assert_denied(command)
                    else:
                        self.assert_allowed(command)

    def test_modeled_constructs_never_promote_quoted_arguments_to_commands(self) -> None:
        text = shlex.quote(CHECKPOINT)
        echo = "echo " + text
        forms = (
            f'if true; then {echo}; elif false; then {echo}; else {echo}; fi',
            f'while false; do {echo}; done', f'until true; do {echo}; done',
            f'for item in one; do {echo}; done', f'for item in {text}; do echo ready; done',
            f'case item in one) {echo};; two|three) {echo};; esac',
            f'case {text} in *) echo ready;; esac',
            f'{{ {echo}; }}', f'({echo})', f'! {echo}', f'time {echo}',
            f'worker() {{ {echo}; }}', f'function worker {{ {echo}; }}',
            f'function worker() {{ {echo}; }}',
        )
        for command in forms:
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_reserved_shell_positions_cannot_hide_checkpoints(self) -> None:
        forms = (
            f'if true; then {CHECKPOINT}; fi',
            f'if false; then true; else {CHECKPOINT}; fi',
            f'if false; then true; elif {CHECKPOINT}; then true; fi',
            f'if {CHECKPOINT}; then true; fi',
            f'while true; do {CHECKPOINT}; done',
            f'while {CHECKPOINT}; do true; done',
            f'until {CHECKPOINT}; do true; done',
            f'for item in one; do {CHECKPOINT}; done',
            f'{{ {CHECKPOINT}; }}',
            f'{{ true; }}; {CHECKPOINT}',
            f'! {CHECKPOINT}',
            f'time {CHECKPOINT}',
            f'case item in item) {CHECKPOINT};; esac',
            f'run_checkpoint() {{ {CHECKPOINT}; }}',
            f'function run_checkpoint {{ {CHECKPOINT}; }}',
        )
        for command in forms:
            with self.subTest(command=command):
                _ = self.assert_denied(command)

    def test_unmodeled_shell_constructs_fail_closed_for_possible_cargo_checkpoints(self) -> None:
        command = 'coproc worker ' + CHECKPOINT
        _ = self.record()
        _ = self.assert_denied(command)
        (self.repo / "Cargo.toml").unlink()
        _ = self.assert_denied(command)

    def test_autosquash_words_after_double_dash_are_selected_paths(self) -> None:
        for name in ("--fixup", "--squash"):
            path = self.repo / name
            _ = path.write_text("base path\n", encoding="utf-8")
            _ = self.git("add", "--", name)
        _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "path base")
        parent = self.git("rev-parse", "HEAD")
        for name in ("--fixup", "--squash"):
            with self.subTest(path=name):
                _ = (self.repo / name).write_text("working path change\n", encoding="utf-8")
                command = CHECKPOINT + " -- " + name
                _ = self.assert_denied(command)
                index = self.repo / ".git/index"
                before = index.read_bytes()
                _ = self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                             "commit", "-q", "-m", "fixture path tree", "--", name)
                tree = self.git("rev-parse", "HEAD^{tree}")
                _ = self.git("reset", "--soft", parent)
                _ = index.write_bytes(before)
                _ = self.record(tree=tree)
                self.assert_allowed(command)
                self.assertEqual(index.read_bytes(), before)

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

    def test_commit_short_message_clusters_read_attached_and_separate_values(self) -> None:
        message = self.root / "cluster-message"
        _ = message.write_text("checkpoint(x): p\n", encoding="utf-8")
        forms = (
            '-m "checkpoint(x): p"', '-m"checkpoint(x): p"',
            '-am"checkpoint(x): p"', '-qam "checkpoint(x): p"',
            '-F' + shlex.quote(str(message)), '-qF ' + shlex.quote(str(message)),
        )
        for options in forms:
            with self.subTest(options=options):
                reason = self.assert_denied("git commit " + options)
                self.assertIn("no doc pass record", reason)
                self.assertNotIn("Subject is unknown", reason)
        _ = self.record()
        for options in forms:
            with self.subTest(recorded_options=options):
                self.assert_allowed("git commit " + options)

    def test_F022_attached_cluster_checkpoint_obeys_own_call_in_non_cargo_repo(self) -> None:
        (self.repo / "Cargo.toml").unlink()
        reason = self.assert_denied('echo ready && git commit -am"checkpoint(x): p"')
        self.assertIn("first command of its own Bash call", reason)
        self.assertNotIn("Subject is unknown", reason)
        self.assert_allowed('git commit -am"checkpoint(x): p"')

    def test_F023_merge_clusters_stop_before_message_text_and_editor_letters(self) -> None:
        for options in ('-qm "checkpoint(x): p"', '-m"checkpoint(x): eee"',
                        '-qvm"checkpoint(x): p"'):
            with self.subTest(options=options):
                reason = self.assert_denied("git merge topic " + options)
                self.assertIn("merge's resulting tree", reason)
                self.assertNotIn("Subject is unknown", reason)
        # The e in message text and strategy values is never an editor flag.
        for options in ('-m"fix: eee"', '-qm "fix: eee"',
                        '-srecursive -Xtheirs -m"fix: eee"',
                        '-qs recursive -X -e -m"fix: eee"'):
            with self.subTest(ordinary_options=options):
                self.assert_allowed("git merge topic " + options)
        (self.repo / "Cargo.toml").unlink()
        reason = self.assert_denied('echo ready && git merge topic -m"checkpoint(x): eee"')
        self.assertNotIn("Subject is unknown", reason)

    def test_merge_short_file_clusters_read_attached_and_separate_values(self) -> None:
        message = self.root / "cluster-message"
        _ = message.write_text("checkpoint(x): p\n", encoding="utf-8")
        for options in ('-F' + shlex.quote(str(message)), '-qF ' + shlex.quote(str(message))):
            with self.subTest(options=options):
                reason = self.assert_denied("git merge topic " + options)
                self.assertIn("merge's resulting tree", reason)
                self.assertNotIn("Subject is unknown", reason)

    def test_short_cluster_values_do_not_change_tree_selection(self) -> None:
        _ = self.record()
        _ = (self.repo / "lib.rs").write_text("pub fn unstaged() {}\n", encoding="utf-8")
        for options in ('-qSmop', '-quall', '-S', '-u', '-qt template', '-qU 3'):
            with self.subTest(options=options):
                self.assert_allowed('git commit ' + options + ' -m "checkpoint(x): p"')
        reason = self.assert_denied('git commit -am"checkpoint(x): p"')
        self.assertIn("no doc pass record", reason)
        # Values consume the remainder even when it contains interactive flags.
        self.assert_allowed('git commit -m"checkpoint(x): pio"')

    def test_unknown_short_cluster_letters_are_unreadable_under_existing_scope(self) -> None:
        for subcommand in ("commit", "merge topic"):
            command = 'git ' + subcommand + ' -qJm"checkpoint(x): p"'
            with self.subTest(command=command):
                reason = self.assert_denied(command)
                self.assertIn("unknown git", reason)
                self.assertIn("short option -J", reason)
        _ = self.record()
        _ = self.assert_denied('git commit -qJ -m "checkpoint(x): p"')
        (self.repo / "Cargo.toml").unlink()
        self.assert_allowed('git commit -qJ -m "checkpoint(x): p"')

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
        # Unreadable messages count as checkpoints only in a Cargo repo.
        self.assert_allowed('echo ready && git commit -m "$VAR"', cwd=self.root)

    def test_verify_header_describes_the_stable_locked_checkout(self) -> None:
        header = HOOK.parents[1].joinpath("direct/verify.sh").read_text(encoding="utf-8").split("set -", 1)[0]
        self.assertIn("stable, locked checkout under the repo's target directory", header)
        self.assertNotIn("checked out in a temporary directory under /tmp", header)

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
                                    env=self.environment, capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS)
            if command != CHECKPOINT:
                self.assertEqual(result.stdout, "")
            else:
                response = cast(dict[str, dict[str, str]], json.loads(result.stdout))
                self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("FileNotFoundError", response["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_later_commits_all_flag_does_not_change_the_checkpoint_tree(self) -> None:
        _ = self.record()
        _ = (self.repo / "lib.rs").write_text("pub fn unstaged() {}\n", encoding="utf-8")
        self.assert_allowed(CHECKPOINT + ' && git commit -am "fix: later"')

    def test_invalid_or_unreadable_matching_record_fails_closed(self) -> None:
        path = self.record()
        _ = path.write_text("not json", encoding="utf-8")
        reason = self.assert_denied(CHECKPOINT, subprocess_cli=True)
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
                                  check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
        _ = git.write_text(
            "#!/bin/sh\nfor argument do\n"
            + "  if [ \"$argument\" = write-tree ]; then\n"
            + "    echo 'fixture index read failed' >&2\n    exit 42\n  fi\ndone\n"
            + "exec " + shlex.quote(real_git) + ' "$@"\n', encoding="utf-8")
        git.chmod(0o755)
        self.environment["PATH"] = f"{tools}{os.pathsep}{os.environ['PATH']}"
        reason = self.assert_denied(CHECKPOINT)
        self.assertIn("fixture index read failed", reason)


if __name__ == "__main__":
    _ = unittest.main()
