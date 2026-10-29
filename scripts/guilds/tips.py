#!/usr/bin/env python3
"""Read committed guild tips and publish one head's prepared edit under a Git lock."""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

GUILDS = ("designers", "reviewers", "showrunners", "units")
GIT_TIMEOUT = 60
LOAD_LOCK_TIMEOUT = 10.0
GUILD_BLOCK = 'Your guild is `{guild}`. Your head is the `{guild}` unit director in the `guilds` production on natedev. At skill start, use the Tips lock/pull rule for `~/rust/nate_style` and load `guilds/common/` and `guilds/{guild}/`; a Codex holder does this for its worker. Report to your head when the user corrects you, a phase needs repair round 4, a finding meets the Events repeat rule, you use a workaround for a tool fault or slow rule, or a method saves measured time. Send one report of at most 100 words, with evidence and result. Resolve the head from `message/roster.py --json` by production `guilds` and unit `{guild}`; send with `message/send.py` or SendMessage. A Codex worker puts the report in its reply for its holder to send. Send only to the head. Never wake an idle Codex member for guild advice.'


def guild_block(guild: str) -> str:
    if guild not in GUILDS:
        raise ValueError(f"unknown guild {guild}")
    return GUILD_BLOCK.replace("{guild}", guild)


class BaselineMissing(ValueError):
    """No committed guild tips are available to start a member."""


def _repo() -> Path:
    return Path(os.environ.get("GUILD_TIPS_REPO") or "~/rust/nate_style").expanduser().resolve()


def _git(repo: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True,
                                check=False, timeout=GIT_TIMEOUT,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_MERGE_AUTOEDIT": "no"})
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"git {' '.join(arguments)} timed out") from error
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ValueError(f"git {' '.join(arguments)}: {message}")
    return result.stdout


@contextmanager
def _lock(repo: Path, timeout: float) -> Generator[None]:
    common = Path(_git(repo, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = repo / common
    lock_path = common / "guild-tips.lock"
    with lock_path.open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f"timed out waiting for lock {lock_path}") from error
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _committed_tips(repo: Path, guild: str) -> str:
    try:
        commit = _git(repo, "rev-parse", "--verify", "HEAD").strip()
    except ValueError as error:
        raise BaselineMissing("restore the guild tips baseline before starting") from error
    files = _git(repo, "ls-tree", "-r", "--name-only", commit, "--", "guilds/").splitlines()
    if not files:
        raise BaselineMissing("restore the guild tips baseline before starting")
    selected = sorted(path for path in files if PurePosixPath(path).parent.as_posix() in (
        "guilds/common", f"guilds/{guild}") and path.endswith(".md"))
    parts = [f"commit {commit}"]
    for path in selected:
        parts.append(f"## {path}\n" + _git(repo, "show", f"{commit}:{path}"))
    return "\n\n".join(parts) + "\n"


def load(guild: str) -> str:
    """Pull once and read one commit; a failed lock or pull uses committed local tips."""
    if guild not in GUILDS:
        raise ValueError(f"unknown guild {guild}")
    repo = _repo()
    try:
        with _lock(repo, LOAD_LOCK_TIMEOUT):
            try:
                _ = _git(repo, "pull", "--ff-only")
            except (OSError, ValueError) as error:
                text = _committed_tips(repo, guild)
                _warn(f"{error}; using local {text.splitlines()[0]}")
                return text
            return _committed_tips(repo, guild)
    except BaselineMissing:
        raise
    except (OSError, ValueError) as error:
        text = _committed_tips(repo, guild)
        _warn(f"{error}; using local {text.splitlines()[0]}")
        return text


def _warn(message: str) -> None:
    print("tips: " + message.replace("\n", " ").replace("\r", " "), file=sys.stderr)


def _without_yaml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif quote:
            if quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif character in ("'", '"'):
            quote = character
        elif character == "#":
            return line[:index].rstrip()
    return line


def _owned_common(text: str, head: str) -> bool:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return False
    tags: list[str] = []
    in_tags = False
    for raw_line in lines[1:]:
        line = _without_yaml_comment(raw_line)
        if line == "---":
            break
        if line.startswith("tags:"):
            in_tags = True
            tags.extend(re.findall(r"[A-Za-z0-9_/-]+", line.removeprefix("tags:")))
        elif in_tags and line.lstrip().startswith("- "):
            tags.append(line.lstrip()[2:].strip(" \"'"))
        elif line.strip() and not line.startswith(" "):
            in_tags = False
    owners = {tag for tag in tags if tag.startswith("head/")}
    return owners == {f"head/{head}"}


def _prepared(repo: Path, folder: Path, head: str) -> dict[str, bytes]:
    if not folder.is_dir():
        raise ValueError(f"no prepared tips folder at {folder}")
    prepared: dict[str, bytes] = {}
    for source in sorted(folder.rglob("*")):
        if source.is_symlink():
            raise ValueError(f"prepared path is a symlink: {source}")
        if not source.is_file():
            continue
        relative = source.relative_to(folder)
        path = "guilds/" + relative.as_posix()
        target = repo / path
        parents = target.relative_to(repo).parents
        if (not target.resolve().is_relative_to(repo) or target.is_symlink()
                or any((repo / parent).is_symlink() for parent in parents)):
            raise ValueError(f"target leaves the checkout: {path}")
        content = source.read_bytes()
        if relative.parts[0] not in (head, f"{head}_base"):
            if relative.parent != Path("common") or not _owned_common(content.decode("utf-8"), head):
                raise ValueError(f"head {head} does not own {path}")
            if target.exists() and not _owned_common(target.read_text(encoding="utf-8"), head):
                raise ValueError(f"head {head} does not own existing {path}")
        prepared[path] = content
    if not prepared:
        raise ValueError("prepared tips folder has no files")
    return prepared


def _check_targets(repo: Path, prepared: dict[str, bytes]) -> None:
    staged = _git(repo, "diff", "--cached", "--name-only").strip()
    if staged:
        raise ValueError(f"staged index needs its owner: {staged}")
    dirty = _git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all", "--", *prepared).strip()
    if dirty:
        raise ValueError(f"uncommitted target edit needs its owner: {dirty}")


def _unpublished(repo: Path) -> set[str]:
    return set(_git(repo, "rev-list", "origin/main..HEAD").splitlines())


def _rebase_edit(repo: Path, owned_commits: set[str]) -> None:
    _ = _git(repo, "fetch", "origin")
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        raise ValueError(f"dirty tracked work needs its owner before rebase: {dirty}")
    foreign = _unpublished(repo) - owned_commits
    if foreign:
        details = _git(repo, "log", "--oneline", "--name-only", "--no-walk", *sorted(foreign)).strip()
        raise ValueError(f"foreign unpublished work needs its owner: {details}")
    try:
        _ = _git(repo, "rebase", "origin/main")
    except (OSError, ValueError) as error:
        try:
            _ = _git(repo, "rebase", "--abort")
        except (OSError, ValueError) as abort_error:
            raise ValueError(f"{error}; abort failed: {abort_error}; report to the guilds showrunner") from error
        raise ValueError(f"{error}; local commit kept; report to the guilds showrunner") from error


def publish(head: str, folder: str | Path) -> str:
    """Publish only prepared paths the named head owns; never include another edit."""
    if head not in GUILDS:
        raise ValueError(f"unknown guild head {head}")
    repo = _repo()
    source = Path(folder).expanduser().resolve()
    with _lock(repo, GIT_TIMEOUT):
        if _git(repo, "branch", "--show-current").strip() != "main":
            raise ValueError("publish needs branch main")
        prepared = _prepared(repo, source, head)
        _check_targets(repo, prepared)
        try:
            _ = _git(repo, "pull", "--ff-only")
        except (OSError, ValueError):
            _rebase_edit(repo, set())
        foreign = _unpublished(repo)
        if foreign:
            details = _git(repo, "log", "--oneline", "--name-only", "origin/main..HEAD").strip()
            raise ValueError(f"foreign unpublished work needs its owner: {details}")
        # Pull can change a common tip's owner or introduce a target edit.
        prepared = _prepared(repo, source, head)
        _check_targets(repo, prepared)
        for path, content in prepared.items():
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(content)
        _ = _git(repo, "add", "--", *prepared)
        _ = _git(repo, "commit", "-m", f"Update {head} guild tips", "--", *prepared)
        commit = _git(repo, "rev-parse", "HEAD").strip()
        try:
            _ = _git(repo, "push", "origin", "main")
        except (OSError, ValueError):
            _rebase_edit(repo, {commit})
            _ = _git(repo, "push", "origin", "main")
        return _git(repo, "rev-parse", "HEAD").strip()


@dataclass
class TipsArguments(argparse.Namespace):
    command: str = ""
    guild: str = ""
    head: str = ""
    source: str = ""


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    loading = commands.add_parser("load")
    _ = loading.add_argument("--guild", choices=GUILDS, required=True)
    publishing = commands.add_parser("publish")
    _ = publishing.add_argument("--head", choices=GUILDS, required=True)
    _ = publishing.add_argument("--from", dest="source", required=True)
    args = parser.parse_args(arguments, namespace=TipsArguments())
    try:
        if args.command == "load":
            print(load(args.guild), end="")
        else:
            print(publish(args.head, args.source))
    except BaselineMissing as error:
        _warn(str(error))
        return 2
    except (OSError, UnicodeError, ValueError) as error:
        _warn(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
