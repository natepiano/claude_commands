"""Test fixtures for shell loading, without processes or import-time patches."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType


@dataclass(frozen=True)
class FakeLoadedShell:
    env: Mapping[str, str]
    envrc: Path


@dataclass(frozen=True)
class FakeNoEnvrc:
    env: Mapping[str, str]


@dataclass(frozen=True)
class FakeShellRefusal:
    reason: str


def fake_nix_loader() -> ModuleType:
    """Return the helper contract without running direnv or nix."""
    def ensure(_directory: Path, base_env: Mapping[str, str]) -> FakeNoEnvrc:
        return FakeNoEnvrc(dict(base_env))

    module = ModuleType("scripts.lib.nix_env")
    for name, value in (("Loaded", FakeLoadedShell), ("NoEnvrc", FakeNoEnvrc),
                        ("Refused", FakeShellRefusal), ("ensure", ensure)):
        setattr(module, name, value)
    return module


def imports_with_nix_loader(loader: ModuleType) -> Callable[[str], ModuleType]:
    """Replace only the lazy helper import. Leave other imports unchanged."""
    def load(name: str) -> ModuleType:
        return loader if name == "scripts.lib.nix_env" else import_module(name)

    return load


def installed_program(home: Path, tool: str) -> str:
    """Create an executable fixture. Never run it."""
    program = home / ".local" / "bin" / tool
    program.parent.mkdir(parents=True, exist_ok=True)
    _ = program.write_text("#!/bin/sh\nexit 99\n")
    program.chmod(0o700)
    return str(program)
