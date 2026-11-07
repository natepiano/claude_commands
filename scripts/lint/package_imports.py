#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Convert repository imports and their callers without changing formatting."""
from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
import re
import sys
from typing import NamedTuple


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


def _bootstrap_tree(root: Path, excluded: Sequence[Path] = ()) -> int:
    return sum(add_bootstrap(file) for file in sorted((root / "scripts").rglob("*.py"))
               if file not in excluded)


def module_name(root: Path, path: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")


class FileRelativePath(NamedTuple):
    path: Path


def path_value(node: ast.AST, names: dict[str, FileRelativePath], file: Path) -> FileRelativePath:
    """Read paths derived from __file__ without running repository code."""
    if isinstance(node, ast.Name):
        return FileRelativePath(file) if node.id == "__file__" else names[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"Path", "str"} and node.args:
        return path_value(node.args[0], names, file)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"resolve", "absolute", "expanduser"} and not node.args):
        return path_value(node.func.value, names, file)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return FileRelativePath(path_value(node.value, names, file).path.parent)
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents"
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int)):
        return FileRelativePath(path_value(node.value.value, names, file).path.parents[node.slice.value])
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
        return FileRelativePath(path_value(node.left, names, file).path / node.right.value)
    raise ValueError("dynamic path")


def rewrite(root: Path, file: Path, modules: dict[str, list[Path]], selected: bool,
            tied: set[Path]) -> tuple[str, set[Path], list[str]]:
    original_source = source = file.read_text()
    if file == root / "scripts/lib/package_root.py":
        return source, set(), []
    tree = ast.parse(source)
    data = source.encode()
    lines = data.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits: list[tuple[int, int, bytes]] = []
    problems: list[str] = []
    converted: set[Path] = set()
    names: dict[str, FileRelativePath] = {}
    folders = [file.parent]
    removed_names: set[str] = set()
    path_changes: list[ast.stmt] = []

    def issue(node: ast.expr | ast.stmt, reason: str) -> None:
        problems.append(f"{file.relative_to(root)}:{node.lineno}: {reason}")

    def replace(node: ast.expr | ast.stmt, value: str, whole: bool = False) -> None:
        start = offsets[node.lineno - 1] + (0 if whole else node.col_offset)
        last = node.end_lineno or node.lineno
        end = offsets[last] if whole else offsets[last - 1] + (node.end_col_offset or 0)
        edits.append((start, end, value.encode()))

    def segment(node: ast.expr | ast.stmt) -> str:
        return data[offsets[node.lineno - 1] + node.col_offset:offsets[(node.end_lineno or node.lineno) - 1] + (node.end_col_offset or 0)].decode()

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                names[node.targets[0].id] = path_value(node.value, names, file)
            except (ValueError, KeyError, TypeError, IndexError):
                _ = names.pop(node.targets[0].id, None)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Expr, ast.Assign, ast.AugAssign)):
            continue
        value = node.value
        change = (isinstance(node, ast.Expr) and isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                  and value.func.attr in {"insert", "append", "extend"} and ast.unparse(value.func.value) == "sys.path")
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        change = change or any(ast.unparse(target) == "sys.path"
                               or isinstance(target, ast.Subscript) and ast.unparse(target.value) == "sys.path"
                               for target in targets)
        if not change:
            continue
        if isinstance(node, ast.Expr) and isinstance(value, ast.Call):
            added = value.args[-1:]
            if isinstance(value.func, ast.Attribute) and value.func.attr == "extend" and added:
                argument = added[0]
                added = argument.elts if isinstance(argument, (ast.List, ast.Tuple)) else [argument]
        else:
            added = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for directory in added:
            try:
                folders.append(path_value(directory, names, file).path)
            except (ValueError, KeyError, TypeError, IndexError):
                issue(node, "dynamic sys.path change")
                return original_source, set(), problems
        if selected:
            removed_names.update(child.id for child in ast.walk(node) if isinstance(child, ast.Name))
            path_changes.append(node)
    if selected:
        statements = [child for child in ast.walk(tree) if isinstance(child, ast.stmt)]
        str_bound = any(
            isinstance(child, ast.Name) and child.id == "str" and isinstance(child.ctx, (ast.Store, ast.Del))
            or isinstance(child, ast.arg) and child.arg == "str"
            or isinstance(child, ast.alias) and (child.asname or child.name.split(".")[0]) in {"str", "*"}
            or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == "str"
            or isinstance(child, ast.ExceptHandler) and child.name == "str"
            or isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name == "str"
            or isinstance(child, ast.MatchMapping) and child.rest == "str"
            for child in ast.walk(tree))

        def path_guard(test: ast.expr) -> bool:
            if not (isinstance(test, ast.Compare) and len(test.ops) == 1
                    and isinstance(test.ops[0], ast.NotIn) and len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Attribute)
                    and isinstance(test.comparators[0].value, ast.Name)
                    and test.comparators[0].value.id == "sys" and test.comparators[0].attr == "path"):
                return False
            path = test.left
            if (isinstance(path, ast.Call) and isinstance(path.func, ast.Name) and path.func.id == "str"
                    and not str_bound and len(path.args) == 1 and not path.keywords):
                path = path.args[0]
            return isinstance(path, ast.Name) and path.id in names

        for node in reversed(statements):
            if not isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.TryStar)):
                continue
            bodies = [node.body]
            bodies += [node.orelse] if node.orelse else []
            if isinstance(node, (ast.Try, ast.TryStar)):
                bodies += [node.finalbody] if node.finalbody else []
                bodies += [handler.body for handler in node.handlers]
            empty = [body for body in bodies if all(child in path_changes for child in body)]
            if not empty:
                continue
            if len(empty) != len(bodies):
                issue(node, "path removal empties a compound body that has another body with code")
                return source, set(), problems
            headers = [child for child in ast.iter_child_nodes(node)
                       if not isinstance(child, (ast.stmt, ast.ExceptHandler))]
            if isinstance(node, ast.If) and path_guard(node.test):
                headers = []
            bound = {child.id for header in headers for child in ast.walk(header)
                     if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)}
            if isinstance(node, (ast.Try, ast.TryStar)):
                headers += [handler.type for handler in node.handlers if handler.type is not None]
                bound.update(handler.name for handler in node.handlers if handler.name is not None)
            inside = set(ast.walk(node))
            if (any(isinstance(child, ast.Call) for header in headers for child in ast.walk(header))
                    or any(isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
                           and child.id in bound and child not in inside for child in ast.walk(tree))):
                issue(node, "path removal would delete a header call or a binding used outside the compound")
                return source, set(), problems
            removed_names.update(child.id for child in inside if isinstance(child, ast.Name))
            path_changes = [child for child in path_changes if all(child not in body for body in bodies)] + [node]
        deleted_statements = {child for node in path_changes for child in ast.walk(node) if isinstance(child, ast.stmt)}
        for node in path_changes:
            for other in statements:
                if other in deleted_statements:
                    continue
                if node in ast.walk(other):
                    shared = other.lineno == node.lineno
                else:
                    shared = max(node.lineno, other.lineno) <= min(
                        node.end_lineno or node.lineno, other.end_lineno or other.lineno)
                if shared:
                    issue(node, "path removal shares a line with a retained statement or header")
                    return source, set(), problems
            replace(node, "", True)

    bindings = {target.id: node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)}
    bindings.update({node.target.id: node.iter for node in ast.walk(tree)
                     if isinstance(node, ast.For) and isinstance(node.target, ast.Name)})

    def patch_strings(node: ast.AST, seen: tuple[str, ...] = ()) -> list[ast.Constant]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node]
        if isinstance(node, ast.Name) and node.id in bindings and node.id not in seen:
            return patch_strings(bindings[node.id], (*seen, node.id))
        if isinstance(node, (ast.List, ast.Tuple)):
            return [value for child in node.elts for value in patch_strings(child, seen)]
        return []

    targets = {id(value) for node in ast.walk(tree) if isinstance(node, ast.Call)
               and ast.unparse(node.func).split(".")[-1] == "patch" and node.args
               for value in patch_strings(node.args[0])}

    def resolve(name: str, node: ast.expr | ast.stmt) -> Path | None:
        choices = modules.get(name.split(".")[0], [])
        for folder in folders:
            matches = [path for path in choices if path.parent == folder]
            if len(matches) == 1:
                return matches[0]
        if len(choices) == 1:
            return choices[0]
        if choices and (selected or any(path in tied for path in choices)):
            issue(node, f"ambiguous module {name}")
        return None

    def qualified(name: str, node: ast.expr | ast.stmt) -> str:
        path = resolve(name, node)
        if path is not None and (selected or path in tied):
            converted.add(path)
            return module_name(root, path) + name[len(name.split('.')[0]):]
        return name

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports: list[str] = []
            changed = False
            for alias in node.names:
                name = qualified(alias.name, node)
                changed |= name != alias.name
                if name != alias.name and "." not in alias.name:
                    package, _, member = name.rpartition(".")
                    imports.append(f"from {package} import {member}" + (f" as {alias.asname}" if alias.asname else ""))
                else:
                    imports.append("import " + name + (f" as {alias.asname}" if alias.asname else ""))
            if changed:
                replace(node, ("\n" + " " * node.col_offset).join(imports))
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level and selected:
                package = list(file.parent.relative_to(root).parts)
                package = package[:len(package) - node.level + 1]
                name = ".".join(package + ([name] if name else []))
            elif not node.level:
                name = qualified(name, node)
            if name != (node.module or ""):
                original = segment(node)
                replace(node, re.sub(r"^from\s+\S+\s+import", f"from {name} import", original, count=1))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) in targets:
            name = qualified(node.value, node)
            if name != node.value:
                original = segment(node)
                replace(node, original.replace(node.value, name, 1))
        elif isinstance(node, ast.Call):
            if ast.unparse(node.func) in {"__import__", "importlib.import_module", "import_module"} and node.args:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    name = qualified(argument.value, node)
                    if name != argument.value and "." not in argument.value:
                        if isinstance(node.func, ast.Name) and node.func.id == "__import__" and len(node.args) < 4 and not any(keyword.arg == "fromlist" for keyword in node.keywords):
                            replace(node, segment(node).replace(segment(argument), repr(name), 1)[:-1] + ", fromlist=['*'])")
                        else:
                            replace(argument, repr(name))
                elif selected:
                    issue(node, "dynamic module name")
    try:
        for start, end, replacement in sorted(edits, reverse=True):
            data = data[:start] + replacement + data[end:]
        source = data.decode()
        if selected:
            tree = ast.parse(source)
            lines = source.splitlines(keepends=True)
            for node in ast.walk(tree):
                if isinstance(node, ast.If) and ast.dump(ast.Module(node.body, [])) == ast.dump(ast.Module(node.orelse, [])):
                    removed_names.update(child.id for child in ast.walk(node.test) if isinstance(child, ast.Name))
                    first, last = node.body[0].lineno - 1, node.body[-1].end_lineno or node.body[-1].lineno
                    body = [line[:node.col_offset] + line[node.body[0].col_offset:] if line.strip() else line
                            for line in lines[first:last]]
                    lines[node.lineno - 1:node.end_lineno] = body + [""] * ((node.end_lineno or node.lineno) - node.lineno + 1 - len(body))
            source = "".join(lines)
            # Remove only bindings that the deleted path changes used.
            while True:
                tree = ast.parse(source)
                used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
                lines = source.splitlines(keepends=True)
                deleted = False
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign) and all(isinstance(target, ast.Name) and target.id in removed_names - used for target in node.targets):
                        removed_names.update(child.id for child in ast.walk(node.value) if isinstance(child, ast.Name))
                    elif isinstance(node, (ast.Import, ast.ImportFrom)):
                        unused = [alias for alias in node.names if (alias.asname or alias.name) in removed_names - used]
                        if not unused:
                            continue
                        if len(unused) != len(node.names):
                            last = node.end_lineno or node.lineno
                            original = "".join(lines[node.lineno - 1:last])
                            for alias in unused:
                                name = re.escape(ast.unparse(alias))
                                changed = re.sub(name + r"\s*,[ \t]*", "", original, count=1)
                                original = re.sub(r",[ \t]*" + name, "", original, count=1) if changed == original else changed
                            lines[node.lineno - 1:last] = [original] + [""] * (last - node.lineno)
                            deleted = True
                            continue
                    else:
                        continue
                    last = node.end_lineno or node.lineno
                    lines[node.lineno - 1:last] = [""] * (last - node.lineno + 1)
                    deleted = True
                source = "".join(lines)
                if not deleted:
                    break
        _ = ast.parse(source)
    except SyntaxError as error:
        problems.append(f"{file.relative_to(root)}:{error.lineno or 1}: path removal leaves invalid syntax: {error.msg}")
        return original_source, set(), problems
    return source, converted, problems


def convert(root: Path, paths: Sequence[Path], excluded: Sequence[Path] = ()) -> None:
    files = sorted((root / "scripts").rglob("*.py"))
    modules: dict[str, list[Path]] = defaultdict(list)
    for file in files:
        modules[file.stem].append(file)
    for folder in (root / "scripts").iterdir():
        if folder.is_dir() and folder.name.isidentifier() and folder.name not in modules:
            modules[folder.name].append(folder)
    selected = {file for path in paths for file in (path.rglob("*.py") if path.is_dir() else [path])} - set(excluded)
    tied = set(selected)
    pending: dict[Path, str] = {}
    problems: list[str] = []
    refused: set[Path] = set()
    for file in sorted(selected):
        source, converted, issues = rewrite(root, file, modules, True, tied)
        pending[file] = source
        if issues and not converted and source == file.read_text():
            refused.add(file)
        tied.update(converted)
        problems.extend(issues)
    for file in files:
        if file not in selected and file not in excluded:
            source, converted, issues = rewrite(root, file, modules, False, tied)
            pending[file] = source
            if issues and not converted and source == file.read_text():
                refused.add(file)
            problems.extend(issues)
    changed = [file for file, source in pending.items() if source != file.read_text()]
    for file in changed:
        _ = file.write_text(pending[file])
        if file not in selected:
            print(f"outside: {file.relative_to(root)}")
    _ = _bootstrap_tree(root, tuple(refused))
    print(f"converted: {len(changed)} files; {len(tied)} modules; {len(set(changed) - selected)} outside files")
    for problem in sorted(set(problems)):
        print(problem)


if __name__ == "__main__":
    if sys.argv[1] == "bootstrap":
        checkout = Path(sys.argv[2]).resolve()
        count = _bootstrap_tree(checkout)
        print(f"bootstrap: {count} entry files")
    else:
        checkout = Path(sys.argv[1]).resolve()
        convert(checkout, [(checkout / argument).resolve() for argument in sys.argv[2:]])
