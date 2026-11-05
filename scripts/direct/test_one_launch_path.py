"""Require every worker launch command to pass through assign.sh."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shlex
import tempfile
import unittest

from scripts.lib.code_kind import is_test_code


ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = {"scripts/direct/assign.sh", "scripts/direct/assign.py"}
GATED_HOPS = {"scripts/direct/implement.sh", "scripts/direct/review.sh"}
MESH_STARTS = {"start", "follow", "reopen", "fork", "adopt", "prime", "resume-account-move"}
NO_REFERENCES: frozenset[str] = frozenset()


def starts_worker(words: list[str]) -> bool:
    for index, word in enumerate(words):
        name = Path(word).name
        if name in {"implement.sh", "review.sh"}:
            return True
        if name == "codex_mesh.py" and index + 1 < len(words) and words[index + 1] in MESH_STARTS:
            return True
        if name == "agent_bg.sh" and (index + 1 == len(words) or words[index + 1] not in {"stop", "status", "--stop", "--status"}):
            return True
    return False


def python_commands(source: str) -> list[tuple[int, list[str]]]:
    tree = ast.parse(source)
    assignments = {target.id: node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   for target in node.targets if isinstance(target, ast.Name)}

    def word(node: ast.AST, seen: frozenset[str] = NO_REFERENCES) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in assignments and node.id not in seen:
            return word(assignments[node.id], seen | {node.id})
        if isinstance(node, ast.BinOp):
            return word(node.left, seen) + ("/" if isinstance(node.op, ast.Div) else "") + word(node.right, seen)
        if isinstance(node, ast.Call) and node.args:
            return word(node.args[0], seen)
        if isinstance(node, ast.JoinedStr):
            return "".join(word(part, seen) for part in node.values)
        return ""

    return [(node.lineno, [word(element) for element in node.elts])
            for node in ast.walk(tree) if isinstance(node, (ast.List, ast.Tuple))]


def shell_commands(source: str) -> list[tuple[int, list[str]]]:
    aliases: dict[str, str] = {}
    commands: list[tuple[int, list[str]]] = []
    pending = ""
    first_line = 1
    for number, line in enumerate(source.splitlines(), 1):
        if not pending:
            first_line = number
        pending += line.removesuffix("\\")
        if line.endswith("\\"):
            continue
        line, pending = pending, ""
        if re.match(r"\s*(?:#|echo\b|printf\b)", line):
            continue
        try:
            words: list[str] = shlex.split(line, comments=True)
        except ValueError:
            continue
        while words and (assignment := re.fullmatch(r"(\w+)=(.*)", words[0])):
            aliases[assignment[1]] = assignment[2]
            _ = words.pop(0)
        for name, value in aliases.items():
            words = [word.replace("${" + name + "}", value).replace("$" + name, value) for word in words]
        commands.append((first_line, words))
    return commands


def forbidden_launches(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".py", ".sh"} or not path.is_file() or is_test_code(path) or path.stem.endswith("_fixture"):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in LAUNCHERS or any(part in {"tests", "fixtures", "node_modules", ".git"} for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8")
        commands = python_commands(source) if path.suffix == ".py" else shell_commands(source)
        starts = [line for line, words in commands if starts_worker(words)]
        gates = [line for line, words in commands if len(words) > 1
                 and Path(words[0]).name in {"py", "python", "python3"}
                 and Path(words[1]).name == "assign_gate.py"]
        if relative in GATED_HOPS and starts and gates and min(gates) < min(starts):
            continue
        found.extend(f"{relative}:{line}" for line in starts)
    return sorted(set(found))


class OneLaunchPathTests(unittest.TestCase):
    def test_only_assign_starts_or_follows_workers(self) -> None:
        found = forbidden_launches(ROOT)
        self.assertFalse(found, "Worker launch commands must use assign.sh:\n" + "\n".join(found))

    def test_scan_catches_each_worker_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for verb in sorted(MESH_STARTS):
                with self.subTest(verb=verb):
                    _ = (root / "caller.py").write_text(
                        f'MESH = ROOT / "scripts/agents/codex_mesh.py"\ncommand = [python, str(MESH), "{verb}"]\n')
                    _ = (root / "caller.sh").write_text(f'MESH="${{DIR}}/codex_mesh.py"\n"$PY" "$MESH" {verb}\n')
                    self.assertEqual(forbidden_launches(root), ["caller.py:2", "caller.sh:2"])
            for launcher in ("implement.sh", "review.sh", "agent_bg.sh"):
                with self.subTest(launcher=launcher):
                    _ = (root / "caller.py").write_text(f'command = ["bash", str(ROOT / "{launcher}")]\n')
                    _ = (root / "caller.sh").write_text(f'AGENT_BG_ROLE=impl bash "${{DIR}}/{launcher}" --to worker\n')
                    self.assertEqual(forbidden_launches(root), ["caller.py:1", "caller.sh:1"])

    def test_hops_require_the_gate_before_the_first_start(self) -> None:
        gate = '"/repo/scripts/lib/py" "/repo/scripts/direct/assign_gate.py"\n'
        for relative in (*sorted(GATED_HOPS), "other.sh"):
            for start in ('"$PY" "/repo/scripts/agents/codex_mesh.py" start\n',
                          'AGENT_BG_ROLE=impl bash "/repo/scripts/agents/agent_bg.sh" worker\n',
                          'AGENT_BG_ROLE=impl bash "/repo/scripts/agents/agent_bg.sh" --attach worker\n'):
                with self.subTest(path=relative, start=start), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _ = path.write_text(gate + start)
                    self.assertEqual(forbidden_launches(root), [] if relative in GATED_HOPS else [f"{relative}:2"])
                    _ = path.write_text(start)
                    self.assertEqual(forbidden_launches(root), [f"{relative}:1"])
                    _ = path.write_text(start + gate + start)
                    self.assertEqual(forbidden_launches(root), [f"{relative}:1", f"{relative}:3"])
                    _ = path.write_text('# ' + gate + start)
                    self.assertEqual(forbidden_launches(root), [f"{relative}:2"])
                    _ = path.write_text('echo "assign_gate.py"\n' + start)
                    self.assertEqual(forbidden_launches(root), [f"{relative}:2"])

    def test_claude_stop_and_status_do_not_start_workers(self) -> None:
        for verb in ("stop", "status", "--stop", "--status"):
            with self.subTest(verb=verb), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _ = (root / "caller.py").write_text(f'command = ["bash", "scripts/agents/agent_bg.sh", "{verb}", "worker"]\n')
                _ = (root / "caller.sh").write_text(f'BG="scripts/agents/agent_bg.sh"\nbash "$BG" {verb} worker\n')
                self.assertEqual(forbidden_launches(root), [])

    def test_scan_excludes_tests_launcher_and_nonlaunch_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (*LAUNCHERS, "scripts/test_launch.py", "scripts/fake_launch.py",
                             "scripts/launch_test_support.py", "scripts/recorder_fixture.py"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text('command = ["implement.sh"]\n')
            _ = (root / "inspect.py").write_text(
                '# codex_mesh.py start\ntext = "implement.sh --to worker"\n' +
                'command = ["codex_mesh.py", "end"]\n')
            _ = (root / "inspect.sh").write_text(
                '# codex_mesh.py follow\necho "codex_mesh.py start"\n"$PY" codex_mesh.py send --to worker\n')
            self.assertEqual(forbidden_launches(root), [])


if __name__ == "__main__":
    _ = unittest.main()
