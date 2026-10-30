"""Keep test hang limits and helpers in the shared guard module."""

from __future__ import annotations

import ast
from pathlib import Path
import tempfile
from typing import final
import unittest


def violations(repository: Path) -> list[str]:
    found: list[str] = []
    for path in sorted((repository / "scripts").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "TEST_HANG_GUARD_SECS" not in source and "LauncherHangGuard" not in source:
            continue
        relative = path.relative_to(repository)
        tree = ast.parse(source, filename=str(relative))
        if relative != Path("scripts/lib/test_hang_guard.py"):
            module_scope: list[ast.AST] = list(tree.body)
            while module_scope:
                statement = module_scope.pop()
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                if (
                    isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and isinstance(statement.value, ast.Name)
                    and statement.value.id in ("TEST_HANG_GUARD_SECS", "LauncherHangGuard")
                ):
                    found.append(
                        f"{relative}:{statement.lineno}: module alias for {statement.value.id}; "
                        + "import it from scripts/lib/test_hang_guard.py"
                    )
                module_scope.extend(ast.iter_child_nodes(statement))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if relative != Path("scripts/lib/test_hang_guard.py") and any(
                    isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
                    and target.id == "TEST_HANG_GUARD_SECS"
                    for assignment in targets for target in ast.walk(assignment)
                ):
                    found.append(
                        f"{relative}:{node.lineno}: local TEST_HANG_GUARD_SECS; "
                        + "import it from scripts/lib/test_hang_guard.py"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module == "scripts.lib.test_hang_guard" and node.level == 0:
                    continue
                for name in node.names:
                    if name.name in ("TEST_HANG_GUARD_SECS", "LauncherHangGuard"):
                        found.append(
                            f"{relative}:{node.lineno}: {name.name} imported from {node.module}; "
                            + "import it from scripts/lib/test_hang_guard.py"
                        )
    return found


@final
class OneHangGuardTests(unittest.TestCase):
    def test_repository_uses_one_hang_guard(self) -> None:
        self.assertEqual(violations(Path(__file__).resolve().parents[2]), [])

    def test_temporary_tree_refuses_local_copies_and_reexports(self) -> None:
        samples = (
            "TEST_HANG_GUARD_SECS = 300\n",
            "def helper():\n    TEST_HANG_GUARD_SECS: float = 300\n",
            "TEST_HANG_GUARD_SECS, other = 300, 1\n",
            "from sibling_support import TEST_HANG_GUARD_SECS\n",
            "from sibling_support import LauncherHangGuard as Guard\n",
            "from .scripts.lib.test_hang_guard import LauncherHangGuard\n",
            "HANG_SECONDS = TEST_HANG_GUARD_SECS\n",
            "Guard: type = LauncherHangGuard\n",
            "if True:\n    HANG_SECONDS = TEST_HANG_GUARD_SECS\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            scripts = repository / "scripts"
            scripts.mkdir()
            source = scripts / "test_copy.py"
            for sample in samples:
                with self.subTest(source=sample):
                    _ = source.write_text(sample, encoding="utf-8")
                    self.assertEqual(len(found := violations(repository)), 1)
                    self.assertRegex(
                        found[0], r"^scripts/test_copy.py:\d+: .*"
                        + r"import it from scripts/lib/test_hang_guard.py$"
                    )

    def test_shared_guard_and_fixture_script_text_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            library = repository / "scripts" / "lib"
            library.mkdir(parents=True)
            _ = (library / "test_hang_guard.py").write_text(
                "TEST_HANG_GUARD_SECS = 600\n", encoding="utf-8"
            )
            _ = (library / "test_fixture.py").write_text(
                "from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS, LauncherHangGuard\n"
                + "limits = {}\nlimits[TEST_HANG_GUARD_SECS] = 'fixture'\n"
                + "def helper():\n    timeout = TEST_HANG_GUARD_SECS\n    return timeout\n"
                + "fixture = 'TEST_HANG_GUARD_SECS = __TEST_HANG_GUARD_SECS__'\n",
                encoding="utf-8",
            )
            self.assertEqual(violations(repository), [])


if __name__ == "__main__":
    _ = unittest.main()
