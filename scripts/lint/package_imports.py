#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Add the phase 8n package_root.py block to entry files."""
from __future__ import annotations

import ast
from pathlib import Path
import sys


def add_bootstrap(file: Path) -> bool:
    """Add the package bootstrap to an entry file without changing other bytes."""
    if file.name.startswith("test_") or file.name.endswith("_test_support.py"):
        return False
    source = file.read_bytes()
    tree = ast.parse(source)
    entry = source.startswith(b"#!") or any(
        isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
        and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
        for node in tree.body)
    import_functions = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_functions.update(
                f"{alias.asname or alias.name}.import_module" for alias in node.names
                if alias.name == "importlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib" and not node.level:
            import_functions.update(alias.asname or alias.name for alias in node.names
                                    if alias.name == "import_module")
    package_import = any(
        isinstance(node, ast.Import) and any(
            alias.name == "scripts" or alias.name.startswith("scripts.") for alias in node.names)
        or isinstance(node, ast.ImportFrom) and not node.level
        and (node.module == "scripts" or (node.module or "").startswith("scripts."))
        or isinstance(node, ast.Call) and ast.unparse(node.func) in import_functions
        and node.args and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and (node.args[0].value == "scripts" or node.args[0].value.startswith("scripts."))
        for node in ast.walk(tree))
    if not entry or not package_import:
        return False
    depth = next(index for index, parent in enumerate(file.parents) if parent.name == "scripts")
    line = ('    with open(__file__[:__file__.rfind("/") + 1] + "' + "../" * depth
            + 'lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})').encode()
    lines = source.splitlines(keepends=True)
    block = [b"try:", line, b"except FileNotFoundError:", b"    pass"]
    stripped = [existing.rstrip(b"\r\n") for existing in lines]
    if any(stripped[index:index + len(block)] == block for index in range(len(lines))):
        return False
    lines = [existing for existing in lines
             if b'lib/package_root.py") as _package_root: exec(' not in existing]
    tree = ast.parse(b"".join(lines))
    position = tree.body[0].lineno - 1 if tree.body else len(lines)
    for index, node in enumerate(tree.body):
        if (index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                or isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            position = node.end_lineno or node.lineno
    newline = b"\r\n" if lines and lines[0].endswith(b"\r\n") else b"\n"
    prefix = newline if position and not lines[position - 1].endswith(b"\n") else b""
    lines.insert(position, prefix + newline.join(block) + newline)
    _ = file.write_bytes(b"".join(lines))
    return True


def _bootstrap_tree(root: Path) -> int:
    return sum(add_bootstrap(file) for file in sorted((root / "scripts").rglob("*.py")))


if __name__ == "__main__":
    if sys.argv[1:2] != ["bootstrap"] or len(sys.argv) != 3:
        raise SystemExit("usage: package_imports.py bootstrap <checkout>")
    print(f"bootstrap: {_bootstrap_tree(Path(sys.argv[2]).resolve())} entry files")
