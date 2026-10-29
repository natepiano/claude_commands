"""Check executable launch sites without running any launcher.

Python argv assignments are followed within each function. Shell launch lines
are scanned after comments are removed. A script must load nix_env or delegate
to the guarded herdr launch function. Top-level same-module guard functions
call nix_env.ensure (including a dynamically imported loader) or another guard
by bare name; nested functions and methods do not establish module guards.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
import unittest

from scripts.lib.code_kind import is_test_code


@dataclass(frozen=True)
class UnguardedLaunch:
    path: str
    line: int


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


def scope_nodes(scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = [scope]
    while pending:
        node = pending.pop()
        if node is not scope and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nodes.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def calls_guard(nodes: list[ast.AST], guards: set[str]) -> bool:
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id in guards for node in nodes)


def module_guard_functions(tree: ast.Module) -> set[str]:
    functions = {node.name: scope_nodes(node) for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    guards: set[str] = set()
    for name, nodes in functions.items():
        loaders = {"nix_env"}
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                targets = [node.target.id]
                value = node.value
            else:
                continue
            if any(isinstance(part, ast.Call) and isinstance(part.func, ast.Name)
                   and part.func.id == "import_module" and part.args
                   and isinstance(part.args[0], ast.Constant) and isinstance(part.args[0].value, str)
                   and part.args[0].value.endswith("nix_env") for part in ast.walk(value)):
                loaders.update(targets)
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and isinstance(node.func.value, ast.Name) and node.func.value.id in loaders
               and node.func.attr == "ensure" for node in nodes):
            guards.add(name)
    while True:
        discovered = {name for name, nodes in functions.items() if calls_guard(nodes, guards)}
        if discovered <= guards:
            return guards
        guards.update(discovered)


def python_launches(path: Path, source: str, relative: str) -> list[UnguardedLaunch]:
    tree = ast.parse(source, filename=str(path))
    guards = module_guard_functions(tree)
    found: list[UnguardedLaunch] = []
    scopes: list[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef] = [tree]
    scopes.extend(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for scope in scopes:
        nodes = scope_nodes(scope)
        assignments: dict[str, ast.AST] = {target.id: node.value for node in nodes if isinstance(node, ast.Assign)
                       for target in node.targets if isinstance(target, ast.Name)}
        guarded = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and isinstance(node.func.value, ast.Name) and node.func.value.id == "nix_env"
                      and node.func.attr == "ensure" for node in nodes)
        guarded = guarded or any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                                 and "nix_env.py" in node.value for node in nodes)
        guarded = guarded or calls_guard(nodes, guards)
        guarded = guarded or (scope in tree.body and scope.name in guards)
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
        if not path.is_file() or path.suffix not in {".py", ".sh"} or is_test_code(path):
            continue
        relative = path.relative_to(scripts).as_posix()
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
            for name in ("test_launch.py", "fake_launch.py", "launch_test_support.py"):
                _ = (root / name).write_text(cases["print.py"])
            self.assertEqual({item.path for item in unguarded_launches(root)}, set(cases))

    def test_same_module_ensure_guard_covers_its_caller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ('import nix_env, subprocess\n'
                      'def start():\n'
                      '    prepare_shell(".")\n'
                      '    subprocess.Popen(["claude", "--print"])\n'
                      'def prepare_shell(cwd):\n'
                      '    return nix_env.ensure(cwd)\n')
            _ = (root / "launch.py").write_text(source)
            self.assertEqual(unguarded_launches(root), [])

    def test_dynamic_import_guard_and_indirect_caller_cover_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ('from importlib import import_module\n'
                      'from typing import cast\n'
                      'import subprocess\n'
                      'def start():\n'
                      '    prepare_shell(".")\n'
                      '    subprocess.Popen(["claude", "--print"])\n'
                      'def prepare_shell(cwd):\n'
                      '    return launch_shell_environment(cwd)\n'
                      'def direct_start():\n'
                      '    launch_shell_environment(".")\n'
                      '    subprocess.Popen(["codex", "exec"])\n'
                      'def launch_shell_environment(cwd):\n'
                      '    loader = cast(X, cast(object, import_module("scripts.lib.nix_env")))\n'
                      '    return loader.ensure(cwd)\n')
            _ = (root / "launch.py").write_text(source)
            self.assertEqual(unguarded_launches(root), [])

    def test_import_without_ensure_does_not_establish_a_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for load in ('import nix_env', 'loader = cast(X, import_module("scripts.lib.nix_env"))'):
                with self.subTest(load=load):
                    source = ('def prepare_shell(cwd):\n'
                              f'    {load}\n'
                              'def start():\n'
                              '    prepare_shell(".")\n'
                              '    subprocess.Popen(["claude", "--print"])\n')
                    _ = (root / "launch.py").write_text(source)
                    self.assertEqual(unguarded_launches(root), [UnguardedLaunch("launch.py", 5)])

    def test_other_module_attribute_call_does_not_establish_a_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ('def launch_shell_environment(cwd):\n'
                      '    return nix_env.ensure(cwd)\n'
                      'def start():\n'
                      '    other.launch_shell_environment(".")\n'
                      '    subprocess.Popen(["codex", "exec"])\n')
            _ = (root / "launch.py").write_text(source)
            self.assertEqual(unguarded_launches(root), [UnguardedLaunch("launch.py", 5)])

    def test_accounts_has_no_unguarded_start_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "accounts").mkdir()
            source = 'import nix_env, subprocess\nsubprocess.Popen(["codex", "exec", "prompt"])\n'
            _ = (root / "accounts/accounts.py").write_text(source)
            _ = (root / "accounts/other.py").write_text(source)
            self.assertEqual(unguarded_launches(root), [UnguardedLaunch("accounts/accounts.py", 2),
                                                       UnguardedLaunch("accounts/other.py", 2)])

    def test_nested_functions_and_methods_do_not_establish_module_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for definition in ('def container():\n    def prepare_shell(cwd):\n        nix_env.ensure(cwd)\n',
                               'class Shell:\n    def prepare_shell(self, cwd):\n        nix_env.ensure(cwd)\n',
                               'def prepare_shell(cwd):\n    def unused():\n        nix_env.ensure(cwd)\n',
                               'def prepare_shell(cwd):\n    class Unused:\n        value = nix_env.ensure(cwd)\n'):
                with self.subTest(definition=definition):
                    source = definition + ('def start():\n'
                                           '    prepare_shell(".")\n'
                                           '    subprocess.Popen(["claude", "--print"])\n')
                    _ = (root / "launch.py").write_text(source)
                    self.assertEqual(unguarded_launches(root), [UnguardedLaunch("launch.py", 6)])

    def test_guard_cycle_without_ensure_does_not_cover_a_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ('def first(cwd):\n'
                      '    second(cwd)\n'
                      'def second(cwd):\n'
                      '    first(cwd)\n'
                      'def start():\n'
                      '    first(".")\n'
                      '    subprocess.Popen(["codex", "exec"])\n')
            _ = (root / "launch.py").write_text(source)
            self.assertEqual(unguarded_launches(root), [UnguardedLaunch("launch.py", 7)])

    def test_guarded_script_and_non_launch_queries_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / "guard.sh").write_text('exec python3 nix_env.py exec /absolute/claude --print prompt\n')
            _ = (root / "query.py").write_text('import subprocess\nsubprocess.run(["claude", "agents", "--json"])\n')
            self.assertEqual(unguarded_launches(root), [])
