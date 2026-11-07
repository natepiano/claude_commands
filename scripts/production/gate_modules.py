"""Find gate test modules and define GateRefused without loading the gate runner."""
from __future__ import annotations

import os
from pathlib import Path


class GateRefused(Exception):
    """A gate precondition or operation failed; no later step may run."""


def test_module_name(repo: Path, suite: str, path: Path) -> str:
    """Name a module by its path within its suite; standalone jobs keep their file name."""
    root = repo / "scripts" / suite
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name


def is_test_module(path: str) -> bool:
    name = Path(path).name
    return name.startswith("test_") and name.endswith(".py")


def discover_test_modules(repo: Path) -> list[tuple[str, Path]]:
    """Find modules in path order; each suite is the first folder under scripts."""
    scripts = repo / "scripts"
    modules: list[tuple[str, ...]] = []
    for folder, dirnames, filenames in os.walk(scripts):
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
        parent = Path(folder)
        parts = parent.relative_to(scripts).parts
        for name in filenames:
            if is_test_module(name) and (parent / name).is_file():
                modules.append((*parts, name))
    found: list[tuple[str, Path]] = []
    for parts in sorted(modules):
        path = scripts.joinpath(*parts)
        if len(parts) == 1:
            raise GateRefused(f"test module must be in a suite folder under scripts/: {parts[0]}")
        found.append((parts[0], path))
    return found
