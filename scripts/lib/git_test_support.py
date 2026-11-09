"""Configure disposable Git fixtures and commit changes with one synthetic identity."""
from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def write_fixture_git_config(directory: Path) -> Path:
    """Write a private Git configuration and return its path."""
    # Disable detached maintenance so Git cannot remove objects during snapshot copies.
    path = directory / "git-config"
    _ = path.write_text("[maintenance]\n\tauto = false\n", encoding="utf-8")
    return path


def commit_repository(repository: Path, message: str, *paths: str, allow_empty: bool = False,
                      env: Mapping[str, str] | None = None) -> str:
    """Stage selected paths (all by default), commit, and return the new HEAD."""
    for arguments in (
        ["add", "--", *(paths or (".",))],
        ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", *(["--allow-empty"] if allow_empty else []), "-qm", message],
    ):
        _ = subprocess.run(["git", "-C", str(repository), *arguments], check=True,
                           capture_output=True, timeout=TEST_HANG_GUARD_SECS, env=env)
    return subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"],
                                   text=True, timeout=TEST_HANG_GUARD_SECS, env=env).strip()
