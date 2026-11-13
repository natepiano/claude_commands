"""Keep production files untracked through the repository's shared exclude file."""
from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

from scripts.lib import git_files
from scripts.lib.atomic_file import write_text_atomic


class ExcludeRefused(ValueError):
    """Git cannot keep the requested file untracked and ignored."""


def _git(checkout: Path, *arguments: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(checkout), *arguments],
                              input=input_text, capture_output=True, text=True, check=False)
    except OSError as error:
        raise ExcludeRefused(f"{checkout}: cannot read git: {error}") from error


def require_untracked(checkout: Path, path: str) -> None:
    tracked = _git(checkout, "ls-files", "--error-unmatch", "--", path)
    if tracked.returncode == 0:
        raise ExcludeRefused(f"{checkout / path}: tracked by git; no file was published")
    if tracked.returncode != 1:
        raise ExcludeRefused(f"{checkout / path}: cannot check tracked files: {tracked.stderr.strip()}")


def _exclude_path(checkout: Path) -> Path:
    try:
        return git_files.locate(checkout).common_dir / "info" / "exclude"
    except git_files.NotACheckout as error:
        raise ExcludeRefused(f"{checkout}: cannot find git exclude file: {error}") from error


def _read_exclude(exclude: Path) -> str:
    try:
        return exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    except (OSError, UnicodeError) as error:
        raise ExcludeRefused(f"{exclude}: cannot read exclude entries: {error}") from error


def _with_entry(existing: str, pattern: str) -> str:
    if pattern in existing.splitlines():
        return existing
    separator = "" if not existing or existing.endswith("\n") else "\n"
    return existing + separator + pattern + "\n"


def require_untracked_ignored(checkout: Path, path: str, *, planned_pattern: str = "") -> None:
    """Check the index and ignore rules, optionally previewing a shared exclude entry."""
    require_untracked(checkout, path)
    if planned_pattern:
        if planned_pattern != f"/{path}" or any(character in path for character in "\r\n*?[\\"):
            raise ExcludeRefused(f"{checkout}: cannot preview exclude pattern {planned_pattern!r}")
        existing = _read_exclude(_exclude_path(checkout))
        if _with_entry(existing, planned_pattern) == existing:
            # The writer keeps these rules unchanged, including later negations.
            planned_pattern = ""
    if planned_pattern:
        # An appended info/exclude entry beats earlier shared and global rules,
        # but a worktree .gitignore negation still has higher priority.
        matching = _git(checkout, "check-ignore", "-v", "--stdin", "-z", input_text=path + "\0")
        if matching.returncode == 1:
            return
        if matching.returncode == 0:
            source, _, pattern, _ = matching.stdout.removesuffix("\0").split("\0")
            if not pattern.startswith("!") or Path(source).is_absolute() or Path(source).name != ".gitignore":
                return
            raise ExcludeRefused(f"{checkout / path}: not ignored by the effective git rules; no file was published")
        raise ExcludeRefused(f"{checkout / path}: cannot check ignore rules: {matching.stderr.strip()}")
    ignored = _git(checkout, "check-ignore", "-q", "--", path)
    if ignored.returncode:
        reason = ignored.stderr.strip() or "not ignored by the effective git rules"
        raise ExcludeRefused(f"{checkout / path}: {reason}; no file was published")


def ensure_excluded(checkout: Path, pattern: str) -> None:
    """Add a shared exclude entry once and verify that git applies it."""
    if not pattern or any(character in pattern for character in "\r\n"):
        raise ExcludeRefused(f"{checkout}: invalid exclude pattern {pattern!r}")
    path = pattern.removeprefix("/")
    require_untracked(checkout, path)
    exclude = _exclude_path(checkout)
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.with_name("exclude.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing = _read_exclude(exclude)
            planned = _with_entry(existing, pattern)
            if planned != existing:
                write_text_atomic(exclude, planned)
    except (OSError, UnicodeError) as error:
        raise ExcludeRefused(f"{exclude}: cannot write exclude entry: {error}") from error
    require_untracked_ignored(checkout, path)
