#!/usr/bin/env python3
"""Print only project trust keys and coverage of immediate ~/worktrees folders.

Project rows: account, key, accepted, kind.
Worktree rows: account, folder, own accepted, toplevel accepted, main accepted.
All rows are tab separated; no account identity or other configuration is shown.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TypedDict, cast


class ProjectTrustEntry(TypedDict):
    hasTrustDialogAccepted: bool


def project_trust(account_file: Path) -> dict[str, ProjectTrustEntry]:
    try:
        configuration = cast(object, json.loads(account_file.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError(f"{account_file}: {type(error).__name__}") from None
    if not isinstance(configuration, dict):
        return {}
    projects = cast(dict[str, object], configuration).get("projects", {})
    if not isinstance(projects, dict):
        return {}
    return {
        key: {"hasTrustDialogAccepted": isinstance(entry, dict)
              and cast(dict[str, object], entry).get("hasTrustDialogAccepted") is True}
        for key, entry in cast(dict[str, object], projects).items()
    }


def git_path(folder: Path, flag: str) -> Path:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    result = subprocess.run(
        ["git", "-C", str(folder), "rev-parse", "--path-format=absolute", flag],
        env=environment, capture_output=True, text=True, check=False, timeout=10,
    )
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else folder


def repository_keys(folder: Path) -> tuple[Path, Path]:
    root = git_path(folder, "--show-toplevel")
    common = git_path(folder, "--git-common-dir")
    return root, common.parent if common != folder else folder


def descendant_repositories(folder: Path) -> int:
    count = 0
    for current, directories, files in os.walk(folder):
        if ".git" in directories or ".git" in files:
            if Path(current) != folder:
                count += 1
        directories[:] = [name for name in directories if name not in {".git", "target", "node_modules"}]
    return count


def project_kind(folder: Path, root: Path, main: Path) -> str:
    if (root / ".git").exists():
        return f"worktree of {main}" if root != main else "main checkout"
    repositories = descendant_repositories(folder) if folder.is_dir() else 0
    return f"ancestor of {repositories} repos" if repositories else "plain folder"


def accepted(projects: dict[str, ProjectTrustEntry], key: Path) -> bool:
    return projects.get(str(key), {"hasTrustDialogAccepted": False})["hasTrustDialogAccepted"]


def main() -> None:
    home = Path.home()
    account_files = [home / ".claude.json", *sorted(
        directory / ".claude.json" for directory in home.glob(".claude[0-9]") if directory.is_dir()
    )]
    accounts = [(account_file, project_trust(account_file)) for account_file in account_files]
    kinds: dict[str, str] = {}
    for account_file, projects in accounts:
        for key, entry in sorted(projects.items()):
            if key not in kinds:
                folder = Path(key)
                root, main_checkout = repository_keys(folder)
                kinds[key] = project_kind(folder, root, main_checkout)
            print(f"{account_file}\t{key}\t{entry['hasTrustDialogAccepted']}\t{kinds[key]}")
    worktrees = home / "worktrees"
    for folder in sorted(worktrees.iterdir()) if worktrees.is_dir() else []:
        if not folder.is_dir():
            continue
        root, main_checkout = repository_keys(folder)
        for account_file, projects in accounts:
            print(f"{account_file}\t{folder}\t{accepted(projects, folder)}"
                  + f"\t{accepted(projects, root)}\t{accepted(projects, main_checkout)}")


if __name__ == "__main__":
    main()
