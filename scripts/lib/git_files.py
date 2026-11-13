"""Read Git facts that live in plain files, with no Git start.

This is the one reader of `.git`, `commondir` and `HEAD`. A `.git` folder is
the Git folder. A `.git` file names the Git folder on its `gitdir:` line, and
that folder's `commondir` file names the shared Git folder.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_BRANCH_PREFIX = "ref: refs/heads/"


class GitFilesError(ValueError):
    """The Git files give no checkout."""


class NotACheckout(GitFilesError):
    """No `.git` entry marks the folder as a checkout."""


class UnreadableGitFiles(GitFilesError):
    """A `.git` entry exists, but it or the files it names cannot be read."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path: Path = path
        self.reason: str = reason


@dataclass(frozen=True)
class Checkout:
    """The folders of one checkout, resolved, as `git rev-parse` prints them in absolute form."""

    toplevel: Path
    git_dir: Path
    common_dir: Path


@dataclass(frozen=True)
class OnBranch:
    name: str


@dataclass(frozen=True)
class DetachedHead:
    """HEAD names a commit, or holds a ref outside refs/heads."""

    commit: str


def _read_line(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error


def _folder(path: Path, named_by: Path) -> Path:
    try:
        found = path.resolve()
    except (OSError, RuntimeError) as error:
        raise UnreadableGitFiles(named_by, f"cannot resolve {path}: {error}") from error
    if not found.is_dir():
        raise UnreadableGitFiles(named_by, f"{found} is not a folder")
    return found


def at(toplevel: Path) -> Checkout:
    """Read the checkout whose `.git` entry is in this folder, with no search of parent folders."""
    dot_git = toplevel / ".git"
    if dot_git.is_dir():
        git_dir = _folder(dot_git, dot_git)
    elif dot_git.is_file():
        line = _read_line(dot_git)
        location = line.removeprefix("gitdir:").strip()
        if not line.startswith("gitdir:") or not location:
            raise UnreadableGitFiles(dot_git, "expected a gitdir: line")
        git_dir = _folder(toplevel / location, dot_git)
    else:
        raise NotACheckout(f"{toplevel}: no .git entry")
    common = git_dir / "commondir"
    if not common.is_file():
        return Checkout(toplevel.resolve(), git_dir, git_dir)
    location = _read_line(common)
    if not location:
        raise UnreadableGitFiles(common, "empty commondir path")
    return Checkout(toplevel.resolve(), git_dir, _folder(git_dir / location, common))


def locate(folder: Path) -> Checkout:
    """Find the checkout that holds the folder, as `git -C <folder>` finds it."""
    start = folder.expanduser().resolve()
    if not start.is_dir():
        raise NotACheckout(f"{folder}: not a folder")
    for toplevel in (start, *start.parents):
        if (toplevel / ".git").exists():
            return at(toplevel)
    raise NotACheckout(f"{folder}: not in a Git checkout")


def head(checkout: Checkout) -> OnBranch | DetachedHead:
    """Read HEAD in the checkout's own Git folder."""
    text = _read_line(checkout.git_dir / "HEAD")
    return OnBranch(text.removeprefix(_BRANCH_PREFIX)) if text.startswith(_BRANCH_PREFIX) else DetachedHead(text)


def current_branch(folder: Path) -> str:
    """The checked-out branch, or "" for a detached HEAD, as `git branch --show-current` prints it."""
    found = head(locate(folder))
    return found.name if isinstance(found, OnBranch) else ""
