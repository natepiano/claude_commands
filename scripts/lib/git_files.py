"""Read Git facts that live in plain files, with no Git start.

The reader finds a checkout the way `git -C <folder>` does: it searches the
folder and its parents for `.git`. A `.git` folder is the Git folder. A `.git`
file names the Git folder on its `gitdir:` line, and that folder's `commondir`
file names the shared Git folder.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_BRANCH_PREFIX = "ref: refs/heads/"


class NotACheckout(ValueError):
    """The folder is in no Git checkout, or its Git files cannot be read."""


@dataclass(frozen=True)
class Checkout:
    """The folders of one checkout, as `git rev-parse` prints them in absolute form."""

    toplevel: Path
    git_dir: Path
    common_dir: Path


def _read_line(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise NotACheckout(f"{path}: cannot read: {error}") from error


def _named_git_dir(dot_git: Path) -> Path:
    line = _read_line(dot_git)
    if not line.startswith("gitdir:"):
        raise NotACheckout(f"{dot_git}: no gitdir line")
    git_dir = (dot_git.parent / line.removeprefix("gitdir:").strip()).resolve()
    if not (git_dir / "HEAD").is_file():
        raise NotACheckout(f"{dot_git}: {git_dir} is not a Git folder")
    return git_dir


def locate(folder: Path) -> Checkout:
    """Find the checkout that holds the folder, as `git -C <folder>` finds it."""
    start = folder.expanduser().resolve()
    if not start.is_dir():
        raise NotACheckout(f"{folder}: not a folder")
    for toplevel in (start, *start.parents):
        dot_git = toplevel / ".git"
        if dot_git.is_file():
            git_dir = _named_git_dir(dot_git)
        elif (dot_git / "HEAD").is_file():
            git_dir = dot_git.resolve()
        else:
            continue
        common = git_dir / "commondir"
        common_dir = (git_dir / _read_line(common)).resolve() if common.is_file() else git_dir
        return Checkout(toplevel, git_dir, common_dir)
    raise NotACheckout(f"{folder}: not in a Git checkout")


def current_branch(folder: Path) -> str:
    """The checked-out branch, or "" for a detached HEAD, as `git branch --show-current` prints it."""
    head = _read_line(locate(folder).git_dir / "HEAD")
    return head.removeprefix(_BRANCH_PREFIX) if head.startswith(_BRANCH_PREFIX) else ""
