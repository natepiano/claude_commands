"""The tree key: one hash that names what a worktree holds.

The key covers HEAD's tree, then every path that differs from it -- staged,
unstaged, deleted, or new and not ignored -- sorted, each as its path, a NUL
and its content: the sha256 of its bytes, `link <target>` for a symlink,
`absent` when deleted. Those are the content rules of verify.sh's pass-record
key (TREE_KEY_PY), without its worktree path, rustc, script hashes, command
words or environment: this key names the tree alone, and the build log matches
the worktree and the argv on columns of their own. Two clones holding the same
files have the same key.

Every git call passes --no-optional-locks: git must never refresh the index of
a worktree another session is using, since an index.lock collision breaks
that session's git. That is why the changed paths come from `git status`,
which honours the flag, and not from `git diff HEAD` plus `git ls-files
--others` as in verify.sh: porcelain diff rewrites a stat-dirty index even
with the flag (git 2.54, 2026-10-02), and plumbing diff-index lists a
stat-dirty file as changed, so the key would move whenever another session's
git refreshed the index. No git objects are written either. Both git calls
run at once; any failure (not a repository, an unborn HEAD, a timeout) gives
None.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time

GIT_TIMEOUT_S = 10


def git(directory: str, *args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["git", "--no-optional-locks", "-C", directory, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def finish(processes: list[subprocess.Popen[bytes]]) -> list[bytes] | None:
    """Each process's stdout, or None when any fails or the shared deadline passes."""
    deadline = time.monotonic() + GIT_TIMEOUT_S
    outputs: list[bytes] = []
    try:
        for process in processes:
            stdout, _ = process.communicate(timeout=max(0.0, deadline - time.monotonic()))
            if process.returncode != 0:
                return None
            outputs.append(stdout)
    except subprocess.TimeoutExpired:
        return None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                _ = process.wait()
            if process.stdout is not None:
                process.stdout.close()
    return outputs


def content(path: bytes) -> bytes:
    if os.path.islink(path):
        return b"link " + os.readlink(path)
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").digest()
    return b"absent"


def tree_key(directory: str) -> str | None:
    try:
        processes = [
            git(directory, "rev-parse", "--show-toplevel", "HEAD^{tree}"),
            # Staged, unstaged, deleted and untracked alike, every untracked file
            # by name; porcelain names paths from the top in any folder.
            git(directory, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"),
        ]
    except OSError:
        return None
    outputs = finish(processes)
    if outputs is None:
        return None
    heads, status = outputs
    lines = heads.splitlines()
    if len(lines) != 2:
        return None
    top, tree = lines
    key = hashlib.sha256(tree + b"\n")
    # Each entry is "XY PATH"; a path both staged and untracked (git rm --cached) comes twice.
    for path in sorted({entry[3:] for entry in status.split(b"\0") if len(entry) > 3}):
        key.update(path + b"\0" + content(os.path.join(top, path)))
    return key.hexdigest()


def worktree(directory: str) -> str | None:
    """The top folder of the worktree holding directory, as the build log's worktree column names it."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", directory, "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = result.stdout.strip()
    return top if result.returncode == 0 and top else None
