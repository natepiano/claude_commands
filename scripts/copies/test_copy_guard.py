"""Copy guard checks use real repository paths and executable planted scripts."""
from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from typing import ClassVar, cast, override

from scripts.copies import copies, copy_guard
from scripts.production.cli_fixture import ImmutableGitSeed


@dataclass(frozen=True)
class ShellPolicyCase:
    command: str
    operation: str


def shell_policy_cases() -> tuple[ShellPolicyCase, ...]:
    """The same paired policy cases exercise both registered hook fronts."""
    return tuple(ShellPolicyCase(command, operation) for command, operation in (
        ("echo '$(date)'", ""),
        ("echo '$(git clone a b)'", ""),
        ('printf %s "$(date)"', ""),
        (r'echo "\$(git clone a b)"', ""),
        ("printf '%s\n' ';' git clone a b", ""),
        ("cat <<'DOC'\necho $(git clone a b)\nDOC", ""),
        ("bash -c 'git status'", ""),
        ('echo "$(cat <<< "$HOME")"', ""),
        ('printf %s "$((2 << 1))"', ""),
        ('echo "$(git clone a b)"', "git clone"),
        ('printf %s "$(git worktree add dst HEAD)"', "git worktree add"),
        ("true; git clone a b", "git clone"),
        ("cat <<DOC\n$(git clone a b)\nDOC", "git clone"),
        ("bash -c 'git clone a b'", "git clone"),
        (">output git clone a b", "git clone"),
        ('echo "$(printf %s "$(git clone a b)")"', "git clone"),
        ("cat <(git clone a b)", "git clone"),
        ("cat > >(git worktree add dst HEAD)", "git worktree add"),
        ("echo `git clone a b`", "git clone"),
        ("git -C clone status", ""),
        ("git -c alias.clone=show status", ""),
        ("git show clone", ""),
        ("git -C repo worktree add dst HEAD", "git worktree add"),
        ('copies.py run --repo "$(git clone a b)" --commit HEAD -- true', "git clone"),
        ('value=$(git clone a b)', "git clone"),
        ('cat >"$(git clone a b)"', "git clone"),
        ("bash <<'DOC'\ngit clone a b\nDOC", "git clone"),
        ("RUN=(git status); \"${RUN[@]}\"", ""),
        ("RUN=(\n# a reader's literal apostrophe\ngit status\n); \"${RUN[@]}\"", ""),
        ("echo \"$(cat <<'DOC'\ntext with ) and ' unmatched quote\nDOC\n)\"", ""),
        ("RUN=(git clone a b); \"${RUN[@]}\"", "git clone"),
        ("RUN=(git); RUN+=(clone a b); printf %s \"${RUN[@]}\"", ""),
        ("RUN=(git); RUN+=(clone a b); \"${RUN[@]}\"", "git clone"),
        ("$'git' clone a b", "git clone"),
        ("$'echo' '$(git clone a b)'", ""),
        ("cat <<DOC\n<(git clone a b)\nDOC", ""),
        ("cat <<'DOC'\n<(git clone a b)\nDOC", ""),
        ("cat <<DOC\n${missing:-<(git clone a b)}\nDOC", ""),
        ("bash -c 'cat' <<'DOC'\ngit clone a b\nDOC", ""),
        ("bash -O extglob -c 'git clone a b'", "git clone"),
        ("bash -O extglob -ec 'git worktree add dst HEAD'", "git worktree add"),
        ("bash -s <<< 'git clone a b'", "git clone"),
        ("bash -s <<< 'git status'", ""),
        ("bash -s 3<<'DOC' 0<<< 'git status'\ngit clone a b\nDOC", ""),
        ("bash -s 0<<< 'git clone a b' 0<<< 'git status'", ""),
        ("bash -s 0<<< 'git status' 0<<< 'git clone a b'", "git clone"),
        ("bash -s 3<<< 'git clone a b' 0<&3", "git clone"),
        ("bash -s 0<&3 3<<< 'git clone a b' 0<<< 'git status'", ""),
        ("bash -c 'cat' 3>\"$(git clone a b)\"", "git clone"),
        ("bash -- script.sh <<'DOC'\ngit clone a b\nDOC", ""),
        ("'git' clone a b", "git clone"),
        ('echo "$(case x in x) date;; esac)"', ""),
        ('echo "$(case x in x) git clone a b;; esac)"', "git clone"),
        (shlex.join(["python3", "-c", "import subprocess; subprocess.run(['git','clone','a','b'])"]), "git clone"),
        (shlex.join(["python3", "-IW", "error", "-c", "import subprocess; subprocess.run(['git','status'])"]), ""),
        *((shlex.join(["python3", "-c", program]), operation) for program, operation in python_block_cases()),
    ))


def python_block_cases() -> tuple[tuple[str, str], ...]:
    """Post-block bindings shared by scanner and both registered-front fixtures."""
    source = str(Path(__file__).resolve().parents[2])
    return (
        ('import subprocess\nwith open("input"):\n argv = ["git", "clone", "a", "b"]\nsubprocess.run(argv)', "git clone"),
        ('import subprocess\nif condition:\n argv = ["git", "status"]\nelse:\n argv = ["git", "clone", "a", "b"]\nsubprocess.run(argv)', "git clone"),
        ('import subprocess\nfor item in (1, 2):\n argv = ["git", "worktree", "add", "dst", "HEAD"]\nsubprocess.run(argv)', "git worktree add"),
        ('import subprocess\nwhile condition:\n argv = ["git", "clone", "a", "b"]\nsubprocess.run(argv)', "git clone"),
        ('import subprocess\ntry:\n argv = ["git", "status"]\nexcept Exception:\n argv = ["git", "clone", "a", "b"]\nsubprocess.run(argv)', "git clone"),
        ('import subprocess\ntry:\n pass\nexcept Exception:\n argv = ["git", "clone", "a", "b"]\nelse:\n argv = ["git", "status"]\nsubprocess.run(argv)', "git clone"),
        ('import subprocess\nsubprocess.run(["bash", "-s"], input="git clone a b", text=True)', "git clone"),
        (f'import shutil\nwith open("input"):\n source = {source!r}\nshutil.copytree(source, "dst")', "copytree of repository"),
        ('import subprocess\nargv = ["git", "status"]\nif condition:\n argv = ["git", "clone", "a", "b"]\nelse:\n subprocess.run(argv)', ""),
    )


def argv_policy_cases() -> tuple[tuple[list[str], str], ...]:
    return ((["bash", "-c", "git clone a b"], "git clone"),
            (["bash", "-O", "extglob", "-c", "git worktree add dst HEAD"], "git worktree add"),
            (["python3", "-c", "import subprocess; subprocess.run(['git', 'clone', 'a', 'b'])"], "git clone"),
            (["bash", "--", "script.sh"], ""))


def unresolved_policy_cases() -> tuple[tuple[str, str], ...]:
    return (("bash -s", "cannot resolve interpreter stdin"),
            ("bash -c \"$(printf 'git status')\"", "cannot resolve interpreter program"),
            ("bash <<DOC\necho $DYNAMIC\nDOC", "cannot resolve expanded interpreter stdin"),
            ("bash -s 3<<'DOC'\ngit status\nDOC", "cannot resolve interpreter stdin"))


@dataclass(frozen=True)
class RecordedPolicyCase:
    name: str
    provenance: str
    command: str | list[str]
    expected: str


def ordinary_agent_corpus() -> tuple[RecordedPolicyCase, ...]:
    """Frozen tool payloads, never executed; manually assigned copy-policy results.

    Source paths are normalized to /workspace without changing word boundaries.
    Each front runs the same payload in its private fixture cwd. No case depends
    on environment expansion or a repository at the normalized path. Rejected
    commands stay in the corpus, including the intentionally unresolved input.
    """
    return (
        RecordedPolicyCase("python-version-long", "closure3 F030 rejected combined front", "python3 --version", ""),
        RecordedPolicyCase("python-version-short", "closure3 F030 rejected combined front", "python3 -V", ""),
        RecordedPolicyCase("bash-version", "closure3 F030 rejected combined front", "bash --version", ""),
        RecordedPolicyCase("array-keyword", "closure3 F031 rejected combined front",
                           'words=(if); printf \'%s\\n\' "${words[@]}"', ""),
        RecordedPolicyCase("typecheck-vector", "copies-test round3 run 20261031T134606-2867258-e3d6 argv",
                           ["basedpyright", "--venvpath", "/workspace/style", "scripts/copies/copy_guard.py"], ""),
        RecordedPolicyCase("unittest-vector", "copies-test round3 run 20261031T134255-1332727-f031 argv",
                           ["env", "-u", "PYTHONPATH", "-u", "PYTHONUNBUFFERED", "-u", "REFUSAL_WATCH_INVENTORY",
                            "-u", "STAFF_OPERATION_FD", "python3", "-m", "unittest", "discover", "-s", "scripts/copies", "-p", "test_copy_guard.py"], ""),
        RecordedPolicyCase("source-read", "copies-test round4 source inspection Bash payload",
                           "sed -n '90,290p' scripts/copies/shell_syntax.py", ""),
        RecordedPolicyCase("literal-substitution", "round3 paired case via Claude and Codex fronts",
                           "echo '$(git clone a b)'", ""),
        RecordedPolicyCase("heredoc-text", "round3 paired case via Claude and Codex fronts",
                           "cat <<'DOC'\n<(git clone a b)\nDOC", ""),
        RecordedPolicyCase("clone-refused", "round3 paired case via Claude and Codex fronts",
                           'echo "$(git clone a b)"', "git clone"),
        RecordedPolicyCase("generated-program-unresolved", "round3 unresolved case via both fronts; intentional policy limit",
                           'bash -c "$(printf \'git status\')"',
                           "unresolved relevant operation: cannot resolve interpreter program"),
    )


def invocation_semantic_cases() -> tuple[RecordedPolicyCase, ...]:
    """Independent expected results at interpreter, word and descriptor boundaries."""
    programs: tuple[tuple[str, str | list[str], str], ...] = (
        ("python-double-version", ["python3", "-VV"], ""),
        ("python-help-short", ["python3", "-h"], ""),
        ("python-help-question", ["python3", "-?"], ""),
        ("python-help-env", ["python3", "--help-env"], ""),
        ("python-help-xoptions", ["python3", "--help-xoptions"], ""),
        ("python-help-all", ["python3", "--help-all"], ""),
        ("python-warning-operand", ["python3", "-W", "--help", "-c", "import subprocess; subprocess.run(['git','clone','a','b'])"], "git clone"),
        ("python-xoption-operand", ["python3", "-X", "--version", "-c", "import subprocess; subprocess.run(['git','clone','a','b'])"], "git clone"),
        ("python-command-boundary", ["python3", "-c", "import subprocess; subprocess.run(['git','clone','a','b'])", "--version"], "git clone"),
        ("python-script-boundary", ["python3", "--", "--version"], ""),
        ("python-information-expansion", 'python3 --version "$(git clone a b)"', "git clone"),
        ("bash-help", ["bash", "--help"], ""),
        ("zsh-version", ["zsh", "--version"], ""),
        ("zsh-help", ["zsh", "--help"], ""),
        ("bash-command-boundary", ["bash", "-c", "git clone a b", "--help"], "git clone"),
        ("bash-option-operand", ["bash", "--rcfile", "--help", "-c", "git clone a b"], "git clone"),
        ("bash-short-option-operand", ["bash", "-O", "--version", "-c", "git clone a b"], "git clone"),
        ("bash-lone-dash-script", ["bash", "-", "./script.sh"], ""),
        ("bash-script-dash-version", ["bash", "--", "--version"], ""),
        ("bash-short-h-is-not-help", ["bash", "-h", "-c", "git clone a b"], "git clone"),
        ("array-keywords", 'words=(if case for while); printf \'%s\\n\' "${words[@]}"', ""),
        ("array-arithmetic", 'words=("$((1 << 2))")', ""),
        ("array-nested-arithmetic", 'words=("$(printf %s "$((1 << 2))")")', ""),
        ("array-process-case", 'words=(<(case x in x) printf ok;; esac))', ""),
        ("array-quoted-data", "words=('$(git clone a b)')", ""),
        ("array-command-substitution", 'words=("$(git clone a b)")', "git clone"),
        ("array-process-forbidden", 'words=(<(case x in x) git clone a b;; esac))', "git clone"),
        ("array-arithmetic-substitution", 'words=("$((1 + $(git clone a b)))")', "git clone"),
        ("parameter-nested-arithmetic", 'printf %s "${unset:-$((1 << 2))}"', ""),
        ("nested-case-safe", 'echo "$(case x in x) date;; esac)"', ""),
        ("nested-case-forbidden", 'echo "$(case x in x) git clone a b;; esac)"', "git clone"),
        ("output-duplication", "bash -s 0<<< 'git status' 3<<< 'git clone a b' 0>&3", "git clone"),
        ("output-duplication-safe", "bash -s 0<<< 'git clone a b' 3<<< 'git status' 0>&3", ""),
        ("duplication-replacement", "bash -s 3<<< 'git clone a b' 0>&3 0<<< 'git status'", ""),
        ("duplication-replaced-before-read", "bash -s 3<<< 'git status' 3<<< 'git clone a b' 0>&3", "git clone"),
        ("output-move", "bash -s 3<<< 'git clone a b' 0>&3-", "git clone"),
        ("input-move", "bash -s 3<<< 'git clone a b' 0<&3-", "git clone"),
        ("moved-input-replaced", "bash -s 3<<< 'git clone a b' 0>&3- 0<<< 'git status'", ""),
        ("closed-input", "bash -s 0<<< 'git status' 0>&-", "unresolved relevant operation: interpreter stdin is closed"),
        ("closed-duplicate", "bash -s 3<<< 'git status' 3<&- 0>&3", "unresolved relevant operation: interpreter stdin is closed"),
        ("moved-source-closed", "bash -s 3<<< 'git status' 0>&3- 0<&3", "unresolved relevant operation: interpreter stdin is closed"),
        ("output-invalidates-input", "bash -s 0<<< 'git status' 0>file", "unresolved relevant operation: cannot resolve redirected interpreter stdin"),
        ("file-invalidates-input", "bash -s 0<<< 'git status' 0<file", "unresolved relevant operation: cannot resolve redirected interpreter stdin"),
        ("read-write-invalidates-input", "bash -s 0<<< 'git status' 0<>file", "unresolved relevant operation: cannot resolve redirected interpreter stdin"),
        ("bash-long-after-short", ["bash", "-l", "--version"], "unresolved relevant operation: unsupported shell long option position"),
        ("posix-sh-no-python-help", ["sh", "-h", "-c", "git clone a b"], "git clone"),
    )
    return tuple(RecordedPolicyCase(name, "Astra r4 semantic matrix (manual expectation)", command, expected)
                 for name, command, expected in programs)


def repair_policy_cases() -> tuple[RecordedPolicyCase, ...]:
    return ordinary_agent_corpus() + invocation_semantic_cases()


class CopyGuardTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    seed: ClassVar[ImmutableGitSeed]
    transitional: ClassVar[frozenset[str]] = frozenset({"mac_test/offload.py", "production/mac_run.sh", "direct/verify.sh"})

    @classmethod
    @override
    def setUpClass(cls) -> None:
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory())).resolve()
        cls.seed = ImmutableGitSeed.create(root, "main", "Guard Tests", "guard@example.invalid",
                                           {"tracked": "source\n"})

    @classmethod
    @cache
    def repository_findings(cls) -> tuple[copy_guard.TreeCreation, ...]:
        """Share the strict scan of the unchanged repository between both policy tests."""
        scripts = Path(__file__).resolve().parents[1]
        settings = copies.load_config()
        return tuple(copy_guard.scan_scripts(scripts, allow_list={
            path: reason for path, reason in settings.allow_list.items() if path not in cls.transitional}))

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "checkout"
        self.seed.copy_to(self.repo, self.root / "origin.git")

    def test_hook_refuses_each_repository_tree_making_form(self) -> None:
        source = shlex.quote(str(self.repo))
        commands = [
            f"git clone {source} destination",
            f"git -c advice.detachedHead=false clone --shared {source} destination",
            f"git -C {source} worktree add destination HEAD",
            f"env MODE=test command git clone {source} destination",
            f"bash -lc 'git clone {source} destination'",
            f"cp -a {source} destination", f"cp -r {source} destination",
            f"cp -R {source}/. destination", f"cp --archive {source} destination",
            f"cp -a -t destination {source}", f"rsync -a {source}/ destination/",
            f"cd {source} && cp -a . destination",
            f'repo={source}; cp -a "$repo" destination',
        ]
        for command in commands:
            with self.subTest(command=command):
                reason = copy_guard.hook_refusal(json.dumps({
                    "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.root),
                }))
                self.assertIn("copies.py run", reason)
                self.assertIn("Example:", reason)

    def test_worktree_git_pointer_also_counts_as_repository_source(self) -> None:
        linked = self.root / "linked worktree"
        _ = subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--detach", str(linked)],
                           check=True, capture_output=True, timeout=10)
        self.assertTrue((linked / ".git").is_file())
        self.assertIn("copies.py run", copy_guard.refusal(f"cp -a {shlex.quote(str(linked))} destination", self.root))

    def test_original_worktree_helper_is_allowed_but_bare_worktree_add_is_refused(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        helper = scripts / "worktree/make_worktree.sh"
        checkout = self.root / "original checkout"
        command = shlex.join(["bash", str(helper), str(checkout), "original-work"])
        self.assertEqual(copy_guard.refusal(command, self.repo), "")
        self.assertIn("copies.py run", copy_guard.refusal(
            shlex.join(["git", "worktree", "add", str(checkout), "-b", "original-work"]), self.repo))
        refusal = copy_guard.hook_refusal(json.dumps({
            "tool_name": "Bash", "cwd": str(self.repo),
            "tool_input": {"command": shlex.join(["git", "worktree", "add", str(checkout), "-b", "original-work"])},
        }))
        self.assertIn("copies.py run", refusal)
        self.assertIn("make_worktree.sh <path> <branch>", refusal)
        result = subprocess.run(["bash", str(helper), str(checkout), "original-work"],
                                cwd=self.repo, text=True, capture_output=True, timeout=10, check=False,
                                env={**os.environ, "WORKTREES_STATE_DIR": str(self.root / "worktree state")})
        self.assertEqual(result.returncode, 0, result.stderr)
        branch = subprocess.run(["git", "-C", str(checkout), "branch", "--show-current"],
                                text=True, capture_output=True, timeout=10, check=True).stdout.strip()
        self.assertEqual(branch, "original-work")
        self.assertTrue((checkout / ".git").is_file())

    def test_control_flow_and_substitutions_cannot_hide_tree_creation(self) -> None:
        source = shlex.quote(str(self.repo))
        makers = (f"git clone {source} destination", f"git -C {source} worktree add destination HEAD",
                  f"cp -a {source} destination", f"rsync -a {source}/ destination/")
        wrappers = ("if true; then {}; fi", "true && {}", "{{ {}; }}", "({})",
                    "case yes in yes) {};; esac", "for item in yes; do {}; done",
                    "while false; do {}; done", "until true; do {}; done", "echo $({})",
                    'echo "$({})"', "echo `{} `", "eval '{}'", "bash -c 'if true; then {}; fi'",
                    "function make_tree {{ {}; }}", "time -p {}", "coproc {}",
                    "coproc make_tree {{ {}; }}")
        for maker in makers:
            for wrapper in wrappers:
                command = wrapper.format(maker)
                with self.subTest(command=command):
                    self.assertIn("copies.py run", copy_guard.refusal(command, self.root))

    def test_conditional_cd_and_assignment_do_not_hide_the_real_repository_source(self) -> None:
        ordinary = self.root / "documents"
        ordinary.mkdir()
        source = shlex.quote(str(self.repo))
        documents = shlex.quote(str(ordinary))
        for command in (f"(cd {documents}); cp -a . destination",
                        f"if false; then cd {documents}; fi; cp -a . destination",
                        f'repo={source}; if false; then repo={documents}; fi; cp -a "$repo" destination'):
            with self.subTest(command=command):
                self.assertIn("copies.py run", copy_guard.refusal(command, self.repo))

    def test_unparseable_shell_is_refused_instead_of_silently_allowed(self) -> None:
        for command in ('git clone "unterminated', '"$(cat command-file)" source destination',
                        "$unknown source destination", "if true; then git status"):
            with self.subTest(command=command):
                self.assertIn("copies.py run", copy_guard.refusal(command, self.root))

    def test_passes_the_service_and_non_repository_copy_commands(self) -> None:
        ordinary = self.root / "documents"
        ordinary.mkdir()
        commands = [
            f"python3 ~/.claude/scripts/copies/copies.py run --repo {self.repo} --commit HEAD -- true",
            "copies.py run --repo . --commit HEAD -- true",
            "python3 -m scripts.copies.copies run --repo . --commit HEAD -- true",
            f"cp -a {ordinary} destination", f"rsync -a {ordinary}/ destination/",
            f"cp {self.repo}/tracked destination", "git worktree list", "git status",
            "echo 'git clone source destination'", "# git clone source destination\ngit status",
            "cat <<'DOC'\ngit clone source destination\nDOC",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(copy_guard.refusal(command, self.root), "")

    def test_service_mention_does_not_exempt_an_unrelated_clone(self) -> None:
        for command in (
            "echo copies.py; git clone source destination",
            "copies.py list && git clone source destination",
            "python3 helper.py copies.py; git clone source destination",
        ):
            with self.subTest(command=command):
                self.assertIn("copies.py run", copy_guard.refusal(command, self.root))

    def test_non_bash_and_invalid_payloads_do_not_change_other_hooks(self) -> None:
        for payload in (
            "not json", "[]", "{}", json.dumps({"tool_name": "Write", "tool_input": {"command": "git clone a b"}}),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(copy_guard.hook_refusal(payload), "")
        self.assertIn("copies.py run", copy_guard.hook_refusal(json.dumps({
            "tool_name": "Bash", "tool_input": {"command": ["git", "clone", "a", "b"]}, "cwd": str(self.root),
        })))

    def test_scan_fails_on_planted_python_shell_and_copytree_repository_calls(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        planted = {
            "clone.py": 'import subprocess\nsubprocess.run(["git", "clone", "--shared", "repo", "destination"])\n',
            "worktree.sh": '#!/bin/sh\ngit -C "$repo" worktree add "$tree" HEAD\n',
            "copy.py": 'import shutil\nshutil.copytree(repository, destination)\n',
            "copy.sh": '#!/bin/sh\ncp -a "$repo" "$destination"\n',
            "safe.py": 'import shutil\nshutil.copytree(state_directory, destination)\n',
        }
        for name, content in planted.items():
            _ = (scripts / name).write_text(content)
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual({finding.path.name for finding in findings}, {"clone.py", "worktree.sh", "copy.py", "copy.sh"})
        self.assertTrue(all(finding.line >= 1 for finding in findings))
        self.assertTrue(all("copies.py run" in str(finding) for finding in findings))

    def test_scan_finds_qualified_style_git_makers_without_refusing_git_reads(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        _ = (scripts / "crate_worker.py").write_text(
            'import bulk_run as run\n'
            + 'run.git(repo, "clone", "--shared", str(repo), str(checkout))\n'
            + 'run.git(repo, "worktree", "add", str(checkout), "HEAD")\n'
            + 'run.git(repo, "show", "clone")\n')
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual([(finding.line, finding.operation) for finding in findings],
                         [(2, "git clone"), (3, "git worktree add")])

    def assert_import_policy(self, program: str) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        _ = (scripts / "maker.py").write_text(program)
        expected = [(line, text.partition("# refused: ")[2])
                    for line, text in enumerate(program.splitlines(), 1) if "# refused: " in text]
        self.assertTrue(expected)
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual([(finding.line, finding.operation) for finding in findings], expected)

    def test_scan_tracks_dotted_module_imports(self) -> None:
        for module in ("scripts.style.bulk_run", "scripts.copies.copies"):
            with self.subTest(module=module):
                self.assert_import_policy(
                    f'import {module}\n'
                    + f'{module}.git(repo, "clone", "a", "b")  # refused: git clone\n'
                    + f'{module}.git(repo, "worktree", "add", "b", "HEAD")  # refused: git worktree add\n'
                    + f'{module}.git(repo, "show", "clone")\n'
                    + 'def shadow(scripts):\n'
                    + f'    {module}.git(repo, "clone", "a", "b")\n')

    def test_scan_tracks_from_import_module_aliases(self) -> None:
        for package, module in (("scripts.style", "bulk_run"), ("scripts.copies", "copies")):
            with self.subTest(module=module):
                self.assert_import_policy(
                    f'from {package} import {module} as run\n'
                    + 'run.git(repo, "clone", "a", "b")  # refused: git clone\n'
                    + 'run.git(repo, "worktree", "add", "b", "HEAD")  # refused: git worktree add\n'
                    + 'run.git(repo, "status")\n'
                    + 'def shadow(run):\n    run.git(repo, "clone", "a", "b")\n')

    def test_scan_tracks_function_local_imports(self) -> None:
        for statement, call in (
                ("import bulk_run as run", "run.git"),
                ("from scripts.style import bulk_run as run", "run.git"),
                ("import scripts.copies.copies", "scripts.copies.copies.git"),
                ("from scripts.copies.copies import git as make", "make")):
            with self.subTest(statement=statement):
                self.assert_import_policy(
                    'def maker():\n'
                    + f'    {statement}\n'
                    + f'    {call}(repo, "clone", "a", "b")  # refused: git clone\n'
                    + f'    {call}(repo, "worktree", "add", "b", "HEAD")  # refused: git worktree add\n'
                    + f'    {call}(repo, "status")\n'
                    + 'def sibling():\n'
                    + f'    {call}(repo, "clone", "a", "b")\n')

    def test_scan_restores_aliases_after_reimports(self) -> None:
        for module in ("bulk_run", "scripts.copies.copies"):
            with self.subTest(module=module):
                self.assert_import_policy(
                    f'import {module} as run\n'
                    + 'run = unrelated\n'
                    + 'run.git(repo, "clone", "a", "b")\n'
                    + f'import {module} as run\n'
                    + 'run.git(repo, "clone", "a", "b")  # refused: git clone\n'
                    + 'alias = run\n'
                    + 'alias.git(repo, "worktree", "add", "b", "HEAD")  # refused: git worktree add\n'
                    + f'from {module} import git as make\n'
                    + 'make(repo, "clone", "a", "b")  # refused: git clone\n'
                    + 'make = unrelated\n'
                    + 'make(repo, "clone", "a", "b")\n')

    def test_scan_resolves_global_binding_after_later_module_import(self) -> None:
        program = '''def make():
    global run
    run.git(repo, "clone", "a", "b")

from scripts.style import bulk_run as run
make()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(3, "git clone")])

    def test_scan_resolves_nonlocal_binding_after_later_enclosing_import(self) -> None:
        program = '''run = unrelated
def outer():
    def make():
        nonlocal run
        run.git(repo, "clone", "a", "b")

    from scripts.style import bulk_run as run
    make()
outer()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(5, "git clone")])

    def test_scan_keeps_local_shadowing_clean_after_later_imports(self) -> None:
        program = '''def parameter(run):
    run.git(repo, "clone", "a", "b")

def local():
    run.git(repo, "clone", "a", "b")
    run = unrelated

def outer():
    def make(run):
        run.git(repo, "clone", "a", "b")

    from scripts.style import bulk_run as run
    make(unrelated)

from scripts.style import bulk_run as run
parameter(unrelated)
local()
outer()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual(findings, [])

    def test_scan_finds_global_clone_called_before_module_rebinding(self) -> None:
        program = '''from scripts.style import bulk_run as run

def make(repo):
    global run
    run.git(repo, "clone", "source", "copy")

make(repo)
run = object()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(5, "git clone")])

    def test_scan_keeps_global_rebinding_before_definition_clean(self) -> None:
        program = '''from scripts.style.bulk_run import git as run
run = lambda *args: None

def make(repo):
    global run
    run(repo, "clone", "source", "copy")

make("repo")
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual(findings, [])

    def test_scan_keeps_global_rebinding_before_only_call_clean(self) -> None:
        program = '''from scripts.style.bulk_run import git as run

def make(repo):
    global run
    run(repo, "clone", "source", "copy")

run = lambda *args: None
make("repo")
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual(findings, [])

    def test_scan_finds_nonlocal_clone_called_before_enclosing_rebinding(self) -> None:
        program = '''def outer():
    from scripts.style import bulk_run as run

    def make(repo):
        nonlocal run
        run.git(repo, "clone", "source", "copy")

    make("repo")
    run = object()
outer()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(6, "git clone")])

    def test_scan_keeps_nonlocal_rebinding_before_only_call_clean(self) -> None:
        program = '''def outer():
    from scripts.style.bulk_run import git as run

    def make(repo):
        nonlocal run
        run(repo, "clone", "source", "copy")

    run = lambda *args: None
    make("repo")
outer()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual(findings, [])

    def test_scan_uses_each_module_call_binding_without_duplicate_findings(self) -> None:
        program = '''run = object()

def make(repo):
    global run
    run.git(repo, "clone", "source", "copy")

make("repo")
from scripts.style import bulk_run as run
make("repo")
make("repo")
run = object()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(5, "git clone")])

    def test_scan_finds_global_clone_at_module_call_in_branch(self) -> None:
        program = '''run = object()

def make(repo):
    global run
    run.git(repo, "clone", "source", "copy")

if selected:
    from scripts.style import bulk_run as run
    make("repo")
run = object()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(5, "git clone")])

    def test_scan_permits_imported_git_reads_before_module_rebinding(self) -> None:
        program = '''from scripts.style import bulk_run as run

def read(repo):
    global run
    run.git(repo, "show", "clone")

read("repo")
run = object()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual(findings, [])

    def test_scan_uses_final_global_binding_for_outside_callers(self) -> None:
        program = '''run = lambda *args: None

def make(repo):
    global run
    run(repo, "clone", "source", "copy")

make("repo")
from scripts.style.bulk_run import git as run
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(5, "git clone")])

    def test_scan_finds_indirect_global_clone_before_module_rebinding(self) -> None:
        program = '''from scripts.style import bulk_run as run

def make(repo):
    global run
    run.git(repo, "clone", "source", "copy")

def invoke():
    make("repo")

invoke()
run = object()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(5, "git clone")])

    def test_scan_finds_indirect_nonlocal_clone_before_enclosing_rebinding(self) -> None:
        program = '''def outer():
    from scripts.style import bulk_run as run

    def make():
        nonlocal run
        run.git("repo", "clone", "source", "copy")

    def invoke():
        make()

    invoke()
    run = object()

outer()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(6, "git clone")])

    def test_scan_keeps_indirect_nonlocal_rebinding_before_call_clean(self) -> None:
        program = '''def outer():
    from scripts.style import bulk_run as run

    def make():
        nonlocal run
        run.git("repo", "clone", "source", "copy")

    def invoke():
        make()

    run = object()
    invoke()

outer()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual(findings, [])

    def test_scan_finds_global_clone_through_two_level_call_chain(self) -> None:
        program = '''from scripts.style import bulk_run as run

def make():
    global run
    run.git("repo", "clone", "source", "copy")

def b():
    make()

def a():
    b()

a()
run = object()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(5, "git clone")])

    def test_scan_ends_nested_recursion_and_reports_indirect_nonlocal_clone(self) -> None:
        program = '''def outer():
    from scripts.style import bulk_run as run

    def make():
        nonlocal run
        run.git("repo", "clone", "source", "copy")

    def invoke():
        def again():
            if repeat:
                invoke()
            make()
        again()

    invoke()
    run = object()

outer()
'''
        findings = copy_guard.PythonPolicy(self.root / "maker.py").scan(ast.parse(program))
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(6, "git clone")])

    def test_scan_tracks_global_bindings_despite_local_assignments(self) -> None:
        for module in ("bulk_run", "scripts.copies.copies"):
            with self.subTest(module=module):
                self.assert_import_policy(
                    f'import {module} as run\n'
                    + 'def outer():\n'
                    + '    run = unrelated\n'
                    + '    def maker():\n'
                    + '        global run\n'
                    + '        run.git(repo, "clone", "a", "b")  # refused: git clone\n'
                    + '        run = unrelated\n'
                    + '        run.git(repo, "clone", "a", "b")\n'
                    + f'        import {module} as run\n'
                    + '        run.git(repo, "worktree", "add", "b", "HEAD")  # refused: git worktree add\n'
                    + '    run.git(repo, "clone", "a", "b")\n')

    def test_scan_tracks_nonlocal_bindings_despite_local_assignments(self) -> None:
        for module in ("bulk_run", "scripts.copies.copies"):
            with self.subTest(module=module):
                self.assert_import_policy(
                    'run = unrelated\n'
                    + 'def outer():\n'
                    + f'    import {module} as run\n'
                    + '    def maker():\n'
                    + '        nonlocal run\n'
                    + '        run.git(repo, "clone", "a", "b")  # refused: git clone\n'
                    + '        run = unrelated\n'
                    + '        run.git(repo, "clone", "a", "b")\n'
                    + f'        import {module} as run\n'
                    + '        run.git(repo, "worktree", "add", "b", "HEAD")  # refused: git worktree add\n'
                    + 'run.git(repo, "clone", "a", "b")\n')

    def test_scan_respects_parameters_and_local_bindings_that_shadow_git_imports(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        _ = (scripts / "calls.py").write_text(
            'import bulk_run as run\n'
            + 'run.git(repo, "clone", "a", "b")\n'
            + 'def parameter(run):\n    run.git(repo, "clone", "a", "b")\n'
            + 'def local():\n    run.git(repo, "clone", "a", "b")\n    run = unrelated\n'
            + 'def imported():\n    run.git(repo, "clone", "a", "b")\n')
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual([(finding.line, finding.operation) for finding in findings],
                         [(2, "git clone"), (9, "git clone")])

    def test_scan_respects_exception_variables_that_shadow_git_imports(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        _ = (scripts / "calls.py").write_text(
            'import bulk_run as run\n'
            + 'run.git(repo, "clone", "a", "b")\n'
            + 'def caught():\n'
            + '    try:\n        operation()\n'
            + '    except Problem as run:\n        run.git(repo, "clone", "a", "b")\n')
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual([(finding.line, finding.operation) for finding in findings], [(2, "git clone")])

    def test_scan_honors_only_configured_originals_and_isolated_test_inputs(self) -> None:
        scripts = self.root / "scripts"
        allowed = scripts / "production/add_unit.py"
        allowed.parent.mkdir(parents=True)
        program = 'import subprocess\nsubprocess.run(["git", "clone", "source", "destination"])\n'
        _ = allowed.write_text(program)
        _ = (scripts / "test_fixture.py").write_text(program)
        _ = (scripts / "unlisted.py").write_text(program)
        findings = copy_guard.scan_scripts(scripts, allow_list={"production/add_unit.py": "Unit original worktree."})
        self.assertEqual([finding.path.name for finding in findings], ["unlisted.py"])
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual({finding.path.name for finding in findings}, {"add_unit.py", "unlisted.py"})

    def test_scan_resolves_keyword_variable_and_multiline_argv_without_losing_git_options(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        programs = {
            "keyword.py": 'subprocess.run(args=["git", "clone", source, destination])\n',
            "assigned.py": 'argv = ["git", "clone", source, destination]\nsubprocess.run(argv)\n',
            "multiline.py": 'subprocess.run([\n "git", "-C", source,\n "worktree", "add", destination, "HEAD",\n])\n',
            "tuple.py": 'argv = ("git", "-C", source, "worktree", "add", destination)\nsubprocess.check_call(args=argv)\n',
            "concat.py": 'prefix = ["git", "-C", source]\nargv = prefix + ["worktree", "add", destination]\nsubprocess.run(argv)\n',
            "copy.py": f'shutil.copytree(src={str(self.repo)!r}, dst=destination)\n',
            "copy_variable.py": f'source = {str(self.repo)!r}\nshutil.copytree(source, destination)\n',
            "copy_attribute.py": 'shutil.copytree(self.repo, destination)\n',
            "recipe.md": 'Run this:\n```bash\nif true; then git clone source destination; fi\n```\n',
        }
        for name, program in programs.items():
            _ = (scripts / name).write_text(program)
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual({finding.path.name for finding in findings}, set(programs))

    def test_literal_text_and_safe_expansions_pass_while_nested_operations_are_identified(self) -> None:
        for case in shell_policy_cases():
            with self.subTest(command=case.command):
                outcome = copy_guard.inspect_shell(case.command, self.root)
                if case.operation:
                    self.assertEqual(outcome, copy_guard.ForbiddenOperation(case.operation))
                    self.assertTrue(copy_guard.refusal(case.command, self.root).startswith(case.operation + ":"))
                else:
                    self.assertEqual(outcome, copy_guard.NoForbiddenOperation())
                    self.assertEqual(copy_guard.refusal(case.command, self.root), "")

    def test_shell_script_scan_retains_the_same_literal_and_executable_distinction(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        expected: dict[str, str] = {}
        for index, case in enumerate(shell_policy_cases()):
            name = f"case_{index}.sh"
            _ = (scripts / name).write_text(case.command + "\n")
            if case.operation:
                expected[name] = case.operation
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual({finding.path.name: finding.operation for finding in findings}, expected)

    def test_malformed_syntax_and_unresolved_executables_have_distinct_outcomes(self) -> None:
        for command in ('echo "unterminated', 'if true; then git status', '$unknown a b', '"$(cat command-file)" a b'):
            with self.subTest(command=command):
                self.assertIsInstance(copy_guard.inspect_shell(command, self.root), copy_guard.UnresolvedOperation)
                self.assertTrue(copy_guard.refusal(command, self.root).startswith("unresolved relevant operation:"))

    def test_git_helpers_preserve_context_and_python_bindings_use_call_scope_and_position(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        program = '''
import os, subprocess

git(repo, "clone", "--shared", str(repo), str(checkout))
git(repo, "worktree", "add", str(checkout), "HEAD")
git(repo, "show", "clone")
argv = ["git", "clone", "a", "b"]
subprocess.run(args=argv)
argv = ["git", "status"]
subprocess.run(argv)

def forbidden():
    argv = ["git", "-C", repo] + ["worktree", "add", checkout, "HEAD"]
    subprocess.run(argv)

def allowed():
    argv = ["git", "show", "clone"]
    subprocess.run(argv)

subprocess.run("echo '$(git clone a b)'", shell=True)
os.system('echo "$(git clone a b)"')
subprocess.run("git clone a b")  # argv semantics: not a shell program
subprocess.run(["git", "-C", "clone", "status"])
subprocess.run(["git", *unknown])

inherited = ["git", "clone", "a", "b"]
def enclosing():
    subprocess.run(inherited)
    def nested():
        inherited = ["git", "status"]
        subprocess.run(inherited)

def git_at(repository, *arguments):
    return subprocess.run(["git", "-C", str(repository), *arguments])
git_at(repo, "worktree", "add", checkout, "HEAD")
git_at(repo, "show", "clone")
'''
        path = scripts / "operations.py"
        _ = path.write_text(program)
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        detected = {(finding.line, finding.operation) for finding in findings
                    if not finding.operation.startswith("unresolved relevant operation:")}
        lines = cast(Sequence[str], program.splitlines())
        expected = {(lines.index(call) + 1, operation) for call, operation in (
            ('git(repo, "clone", "--shared", str(repo), str(checkout))', "git clone"),
            ('git(repo, "worktree", "add", str(checkout), "HEAD")', "git worktree add"),
            ("subprocess.run(args=argv)", "git clone"),
            ("    subprocess.run(inherited)", "git clone"),
            ('git_at(repo, "worktree", "add", checkout, "HEAD")', "git worktree add"),
            ("    subprocess.run(argv)", "git worktree add"),
            ('os.system(\'echo "$(git clone a b)"\')', "git clone"),
        )}
        self.assertEqual(detected, expected)
        unresolved = [finding for finding in findings if finding.operation.startswith("unresolved relevant operation:")]
        self.assertEqual(len(unresolved), 1)
        self.assertIn("git subcommand", unresolved[0].operation)

    def test_interpreter_argv_payloads_and_python_vectors_share_input_decoding(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        expected: dict[str, str] = {}
        for index, (command, operation) in enumerate(argv_policy_cases()):
            with self.subTest(command=command):
                reason = copy_guard.hook_refusal(json.dumps({
                    "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.root)}))
                self.assertEqual(reason.split(":", 1)[0] if reason else "", operation)
                name = f"invocation_{index}.py"
                _ = (scripts / name).write_text(f"import subprocess\nsubprocess.run({command!r})\n")
                if operation:
                    expected[name] = operation
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual({finding.path.name: finding.operation for finding in findings}, expected)

    def test_post_block_python_values_retain_vectors_sources_and_sibling_isolation(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        expected: dict[str, str] = {}
        for index, (program, operation) in enumerate(python_block_cases()):
            name = f"block_{index}.py"
            _ = (scripts / name).write_text(program)
            if operation:
                expected[name] = operation
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual({finding.path.name: finding.operation for finding in findings}, expected)

    def test_unresolved_interpreter_input_has_its_specific_category(self) -> None:
        for command, reason in unresolved_policy_cases():
            with self.subTest(command=command):
                self.assertEqual(copy_guard.inspect_shell(command, self.root), copy_guard.UnresolvedOperation(reason))

    def test_recorded_agent_commands_and_semantic_boundaries_have_exact_outcomes(self) -> None:
        for case in repair_policy_cases():
            with self.subTest(case=case.name, provenance=case.provenance):
                reason = copy_guard.hook_refusal(json.dumps({
                    "tool_name": "Bash", "tool_input": {"command": case.command}, "cwd": str(self.root)}))
                self.assertEqual(reason.split(": " + copy_guard.REFUSAL, 1)[0] if reason else "", case.expected)
                command = shlex.join(case.command) if isinstance(case.command, list) else case.command
                outcome = copy_guard.inspect_shell(command, self.root)
                expected = (copy_guard.UnresolvedOperation(case.expected.removeprefix("unresolved relevant operation: "))
                            if case.expected.startswith("unresolved relevant operation: ")
                            else copy_guard.ForbiddenOperation(case.expected) if case.expected
                            else copy_guard.NoForbiddenOperation())
                self.assertEqual(outcome, expected)

    def test_semantic_cases_reach_shell_and_python_repository_scanners(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        expected: dict[str, str] = {}
        for case in repair_policy_cases():
            if case.expected.startswith("unresolved relevant operation:"):
                continue  # Static abstract interpreter inputs intentionally defer to runtime.
            command = shlex.join(case.command) if isinstance(case.command, list) else case.command
            programs = {case.name + ".sh": command,
                        case.name + ".py": f"import subprocess\nsubprocess.run({case.command!r}"
                        + (')' if isinstance(case.command, list) else ', shell=True)')}
            for name, program in programs.items():
                _ = (scripts / name).write_text(program)
                if case.expected:
                    expected[name] = case.expected
        findings = copy_guard.scan_scripts(scripts, allow_list={})
        self.assertEqual({finding.path.name: finding.operation for finding in findings}, expected)

    def test_repository_scripts_use_the_single_checkout_service(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        # The shared service loader owns both gate settings and the allow-list data.
        settings = copies.load_config()
        findings = [finding for finding in self.repository_findings()
                    if finding.path.relative_to(scripts).as_posix() not in settings.allow_list]
        self.assertEqual(findings, [], "\n".join(str(finding) for finding in findings))

    def test_repository_build_copy_makers_have_no_transitional_exemptions(self) -> None:
        settings = copies.load_config()
        transitional = self.transitional
        findings = list(self.repository_findings())
        self.assertEqual(findings, [], "\n".join(str(finding) for finding in findings))
        self.assertEqual(set(settings.allow_list) & transitional, set())


if __name__ == "__main__":
    _ = unittest.main()
