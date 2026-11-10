"""Read repository identities without loading build record writers."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from scripts.buildlog import store

GIT_TIMEOUT_S = 5

def output(args: list[str], cwd: str, timeout: float) -> str | None:
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def remote_identity(remote: str, directory: str) -> str:
    """Normalize a remote URL or path to the repository key."""
    if "://" not in remote and ":" in remote and not remote.startswith(("/", ".")):
        host, path = remote.split(":", 1)
        hostname = host.rsplit("@", 1)[-1]
        if (hostname and (host == hostname or host == f"git@{hostname}")
                and hostname.isascii() and all(letter.isalnum() or letter in ".-" for letter in hostname)
                and not any(letter in path for letter in "?#\r\n\t")):
            return f"https://{hostname.lower()}{('/' + path).rstrip('/').removesuffix('.git')}"
        remote = f"ssh://{host}/{path}"
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(remote)
    if parsed.hostname:
        path = parsed.path.rstrip("/").removesuffix(".git")
        port = f":{parsed.port}" if parsed.port and parsed.port not in (22, 443) else ""
        return urlunsplit(("https", parsed.hostname.lower() + port, path, "", ""))
    path = Path(parsed.path if parsed.scheme == "file" else remote).expanduser()
    if not path.is_absolute():
        path = Path(directory) / path
    return str(path.resolve()).rstrip("/").removesuffix(".git")


def repository_key(directory: str) -> str:
    """Read the remote identity or the first root commit of a directory."""
    remote = output(["git", "-C", directory, "remote", "get-url", "origin"], directory, GIT_TIMEOUT_S)
    if remote and remote.strip():
        return remote_identity(remote.strip(), directory)
    roots = output(["git", "-C", directory, "rev-list", "--max-parents=0", "--reverse", "HEAD"],
                   directory, GIT_TIMEOUT_S)
    return "git-root:" + roots.strip().splitlines()[0] if roots and roots.strip() else store.UNKNOWN_IDENTITY


GIT_DIR = ".git"
GITDIR_PREFIX = "gitdir:"
COMMONDIR_FILE = "commondir"
ALTERNATES_FILE = os.path.join("objects", "info", "alternates")


def repo_name(directory: str) -> str | None:
    """Read the repository name from its source Git folder.

    Worktrees share the same name. A local clone uses its source repository name.
    Read the Git files without a Git command.
    """
    common = git_common_dir(directory)
    if common is None:
        return None
    return os.path.basename(os.path.dirname(source_git_dir(common))) or None


def git_common_dir(directory: str) -> str | None:
    """Return the Git folder that the repository worktrees share."""
    current = os.path.abspath(directory)
    while not os.path.lexists(os.path.join(current, GIT_DIR)):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    common = os.path.join(current, GIT_DIR)
    if os.path.isfile(common):
        # The .git file names the private folder. Its commondir file names the shared folder.
        try:
            with open(common, encoding="utf-8") as handle:
                pointer = handle.readline().strip()
        except OSError:
            return None
        if not pointer.startswith(GITDIR_PREFIX):
            return None
        common = os.path.join(current, pointer[len(GITDIR_PREFIX) :].strip())
        try:
            with open(os.path.join(common, COMMONDIR_FILE), encoding="utf-8") as handle:
                common = os.path.join(common, handle.readline().strip())
        except FileNotFoundError:
            pass
        except OSError:
            return None
    return os.path.normpath(common)


def source_git_dir(common: str) -> str:
    """Follow local clone sources to the original Git folder.

    A shared clone records borrowed objects in alternates. A local clone records a local origin.
    Return the current folder when neither source exists.
    """
    seen = {common}
    while True:
        source = borrowed_git_dir(common) or local_origin_git_dir(common)
        if source is None or source in seen:
            return common
        seen.add(source)
        common = source


def borrowed_git_dir(common: str) -> str | None:
    try:
        with open(os.path.join(common, ALTERNATES_FILE), encoding="utf-8") as handle:
            objects = handle.readline().strip()
    except OSError:
        return None
    if not objects:
        return None
    return os.path.dirname(os.path.normpath(os.path.join(common, "objects", objects)))


def local_origin_git_dir(common: str) -> str | None:
    """Return the shared Git folder when origin names a local directory."""
    try:
        with open(os.path.join(common, "config"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    in_origin = False
    for line in lines:
        text = line.strip()
        if text.startswith("["):
            in_origin = text == '[remote "origin"]'
        elif in_origin and text.partition("=")[0].strip() == "url":
            url = text.partition("=")[2].strip().removeprefix("file://")
            if not os.path.isabs(url) or not os.path.isdir(url):
                return None
            return git_common_dir(url)
    return None
