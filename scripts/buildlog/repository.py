"""Read repository identities without loading build record writers."""

from __future__ import annotations

from pathlib import Path
import subprocess

if __package__:
    from . import store
else:
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
