"""Check executable launch sites without running any launcher.

Python argv assignments are followed within each function. Shell launch lines
are scanned after comments are removed. A script must load nix_env or delegate
to the guarded herdr launch function. Account exec is owned by accounts-unit.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
import unittest


@dataclass(frozen=True)
class UnguardedLaunch:
    path: str
    line: int


# The sole ownership exception: accounts-unit is wiring accounts.py exec.
ACCOUNT_EXEC_OWNER = "accounts/accounts.py"
PROCESS_CALLS = {"Popen", "run", "call", "check_call", "check_output", "execvp", "execvpe", "execv", "execve", "execl", "execlp", "system"}
NO_ARGV_REFERENCES: frozenset[str] = frozenset()
NON_START_COMMANDS = {"agents", "logs", "queue", "stop", "auth", "login", "--version", "--help"}


def argv_strings(node: ast.AST, assignments: dict[str, ast.AST], visited: frozenset[str] = NO_ARGV_REFERENCES) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in assignments and node.id not in visited:
        return argv_strings(assignments[node.id], assignments, visited | {node.id})
    if isinstance(node, (ast.List, ast.Tuple)):
        return [word for item in node.elts for word in argv_strings(item, assignments, visited)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return argv_strings(node.left, assignments, visited) + argv_strings(node.right, assignments, visited)
    if isinstance(node, ast.Call):
        return [word for item in node.args for word in argv_strings(item, assignments, visited)]
    return []


def python_launches(path: Path, source: str, relative: str) -> list[UnguardedLaunch]:
    tree = ast.parse(source, filename=str(path))
    found: list[UnguardedLaunch] = []
    scopes: list[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef] = [tree]
    scopes.extend(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for scope in scopes:
        nodes: list[ast.AST] = []
        pending: list[ast.AST] = [scope]
        while pending:
            node = pending.pop()
            if node is not scope and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            nodes.append(node)
            pending.extend(ast.iter_child_nodes(node))
        assignments: dict[str, ast.AST] = {target.id: node.value for node in nodes if isinstance(node, ast.Assign)
                       for target in node.targets if isinstance(target, ast.Name)}
        guarded = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and isinstance(node.func.value, ast.Name) and node.func.value.id == "nix_env"
                      and node.func.attr == "ensure" for node in nodes)
        guarded = guarded or any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                                 and "nix_env.py" in node.value for node in nodes)
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
            if name == "start_agent" and any(keyword.arg == "kind" and isinstance(keyword.value, ast.Constant)
                                              and keyword.value.value in {"claude", "codex"} for keyword in node.keywords):
                if not guarded:
                    found.append(UnguardedLaunch(relative, node.lineno))
                continue
            if name not in PROCESS_CALLS or not node.args:
                continue
            words = argv_strings(node.args[1] if name.startswith("execv") and len(node.args) > 1 else node.args[0], assignments)
            agents = [index for index, word in enumerate(words) if Path(word).name in {"claude", "codex"}]
            starts = any(index + 1 >= len(words) or words[index + 1] not in NON_START_COMMANDS for index in agents)
            starts = starts or ("agent" in words and "start" in words)
            if starts and not guarded:
                found.append(UnguardedLaunch(relative, node.lineno))
    return list(dict.fromkeys(found))


def shell_launches(source: str, relative: str) -> list[UnguardedLaunch]:
    guarded = "nix_env.py" in source or "nix_env.ensure(" in source
    found: list[UnguardedLaunch] = []
    for number, line in enumerate(source.splitlines(), 1):
        code = line.lstrip()
        if not code or code.startswith("#"):
            continue
        starts = bool(re.search(r"\bexec\s+(?:claude|codex)\b|\bclaude\s+(?:-p\b|--print\b|--bg\b)|\bcodex\s+exec\b|\bherdr\b.*\bagent\s+start\b", code))
        if starts and not guarded:
            found.append(UnguardedLaunch(relative, number))
    return found


def unguarded_launches(scripts: Path) -> list[UnguardedLaunch]:
    found: list[UnguardedLaunch] = []
    for path in sorted(scripts.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".sh"} or path.name.startswith(("test_", "fake_")):
            continue
        relative = path.relative_to(scripts).as_posix()
        if relative == ACCOUNT_EXEC_OWNER:
            continue
        source = path.read_text(encoding="utf-8")
        found.extend(python_launches(path, source, relative) if path.suffix == ".py" else shell_launches(source, relative))
    return found


class ClaudeLaunchScanTests(unittest.TestCase):
    def test_guard_in_another_function_does_not_cover_an_unguarded_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ('import nix_env, subprocess\n'
                      'def guarded(cwd):\n'
                      '    result = nix_env.ensure(cwd)\n'
                      '    subprocess.Popen(["claude", "--print"], env=result.env)\n'
                      'def unguarded():\n'
                      '    subprocess.Popen(["codex", "exec", "prompt"])\n'
                      'def restore():\n'
                      '    herdr.start_agent("session", "pane", kind="codex")\n')
            _ = (root / "mixed.py").write_text(source)
            self.assertEqual(unguarded_launches(root), [UnguardedLaunch("mixed.py", 6), UnguardedLaunch("mixed.py", 8)])

    def test_repository_launches_go_through_nix_env(self) -> None:
        self.assertEqual(unguarded_launches(Path(__file__).resolve().parents[1]), [])

    def test_planted_python_shell_and_herdr_starts_fail_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = {"print.py": 'import subprocess\nsubprocess.run(["claude", "--print", "prompt"])\n',
                     "codex.py": 'import subprocess\ndef start():\n    argv = ["codex", "exec", "prompt"]\n    subprocess.Popen(argv)\n',
                     "exec.py": 'import os\nos.execvpe("claude", ["claude", "-p", "prompt"], {})\n',
                     "launch.sh": '#!/bin/sh\nexec claude --bg prompt\n',
                     "herdr.py": 'import subprocess\nsubprocess.run(["herdr", "agent", "start", "worker", "--kind", "claude"])\n'}
            for name, source in cases.items():
                _ = (root / name).write_text(source)
            self.assertEqual({item.path for item in unguarded_launches(root)}, set(cases))

    def test_import_alone_does_not_guard_a_start_and_only_accounts_is_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "accounts").mkdir()
            source = 'import nix_env, subprocess\nsubprocess.Popen(["codex", "exec", "prompt"])\n'
            _ = (root / "accounts/accounts.py").write_text(source)
            _ = (root / "accounts/other.py").write_text(source)
            self.assertEqual([item.path for item in unguarded_launches(root)], ["accounts/other.py"])

    def test_guarded_script_and_non_launch_queries_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / "guard.sh").write_text('exec python3 nix_env.py exec /absolute/claude --print prompt\n')
            _ = (root / "query.py").write_text('import subprocess\nsubprocess.run(["claude", "agents", "--json"])\n')
            self.assertEqual(unguarded_launches(root), [])
