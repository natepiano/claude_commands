"""Check the repository's own scripts for retired design role names."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from scripts.lib.code_kind import is_test_code


class LiveDesignRoleNamesTests(unittest.TestCase):
    def test_live_modules_do_not_import_the_retired_package_or_read_its_environment(self) -> None:
        root = Path(__file__).resolve().parents[2]
        violations: list[str] = []
        for path in sorted((root / "scripts").rglob("*")):
            if not path.is_file() or is_test_code(path) or path.suffix not in (".py", ".sh"):
                continue
            relative = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".sh":
                violations.extend(f"{relative}:{number}: {line.strip()}"
                                  for number, line in enumerate(text.splitlines(), 1)
                                  if "GUILD_" in line or "scripts.guilds" in line)
                continue
            for node in ast.walk(ast.parse(text, filename=relative)):
                imports: tuple[str, ...] = ()
                if isinstance(node, ast.Import):
                    imports = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports = (node.module or "",)
                    if node.module == "scripts":
                        imports += tuple("scripts." + alias.name for alias in node.names)
                if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                        name == "scripts.guilds" or name.startswith("scripts.guilds.") for name in imports):
                    violations.append(f"{relative}:{node.lineno}: retired package import")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "GUILD_" in node.value or "scripts.guilds" in node.value:
                        violations.append(f"{relative}:{node.lineno}: retired name in a runtime string")
        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    _ = unittest.main()
