#!/usr/bin/env python3
"""One standalone assertion guard for verifier fixtures."""
from __future__ import annotations

from pathlib import Path
import shutil


GuardError: type[Exception] = RuntimeError


def commit_bases(repo: Path, selected_bases: tuple[str, ...] = ("HEAD",), *,
                 head: str = "", staged: bool = True) -> tuple[str, ...]:
    del repo, head, staged
    return selected_bases or ("fixture-base",)


def git(repo: Path, *arguments: str) -> str:
    del repo
    return "fixture-head\n" if arguments[0] == "rev-parse" else ""


def read_rulings(sources: list[str], messages: str) -> dict[tuple[str, str], str]:
    del sources, messages
    return {}


def write_guard(path: Path) -> None:
    """Copy the same CLI and cache-key provider into a private fixture."""
    _ = shutil.copyfile(__file__, path)
    path.chmod(0o755)


if __name__ == "__main__":
    raise SystemExit(0)
