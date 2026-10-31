#!/usr/bin/env python3
"""Load and prove a worktree's nix environment before starting a process.

Envrc files below ~/rust, ~/worktrees or ~/.claude*, and in linked worktrees
registered by repositories below those roots, are allowed automatically.
Other directories keep direnv's explicit approval boundary.
No envrc means the caller's environment is retained, without running direnv.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

if __name__ == "nix_env":
    _ = sys.modules.setdefault("scripts.lib.nix_env", sys.modules[__name__])
elif __name__ == "scripts.lib.nix_env":
    _ = sys.modules.setdefault("nix_env", sys.modules[__name__])

TIMEOUT_SECONDS = 600
# Internal, one-session handoff from the healer to shutdown restart. These are
# consumed before opening a pane and never exported to the resumed agent.
HEAL_SESSION_ENV = "NIX_HEAL_PREPARED_SESSION"
HEAL_DIRECTORY_ENV = "NIX_HEAL_PREPARED_DIRECTORY"


class Loaded(NamedTuple):
    env: dict[str, str]
    envrc: Path


class NoEnvrc(NamedTuple):
    env: dict[str, str]


class Refused(NamedTuple):
    reason: str


def nearest_envrc(directory: Path) -> Path | None:
    """Find the nearest envrc, including the directory itself."""
    directory = directory.expanduser().resolve()
    return next((parent / ".envrc" for parent in (directory, *directory.parents)
                 if (parent / ".envrc").is_file()), None)


def trusted_root(path: Path) -> bool:
    """Whether the resolved path belongs to an automatically approved root."""
    try:
        relative = path.resolve().relative_to(Path.home().resolve())
    except (OSError, ValueError, RuntimeError):
        return False
    return bool(relative.parts and (relative.parts[0] in {"rust", "worktrees"}
                                  or relative.parts[0].startswith(".claude")))


def registered_worktree(folder: Path) -> bool:
    """Prove registration by a trusted repository using only git's pointer files."""
    try:
        folder = folder.resolve(strict=True)
        git_file = folder / ".git"
        if not stat.S_ISREG(git_file.lstat().st_mode):
            return False
        lines = git_file.read_text(encoding="utf-8").splitlines()
        if len(lines) != 1 or not lines[0].startswith("gitdir: "):
            return False
        pointer = lines[0][len("gitdir: "):]
        if not pointer:
            return False
        entry = (folder / pointer).resolve(strict=True)
        if not entry.is_dir() or entry.parent.name != "worktrees":
            return False
        common_text = (entry / "commondir").read_text(encoding="utf-8").strip()
        back_text = (entry / "gitdir").read_text(encoding="utf-8").strip()
        if not common_text or not back_text or len(common_text.splitlines()) != 1 or len(back_text.splitlines()) != 1:
            return False
        common = (entry / common_text).resolve(strict=True)
        back_pointer = (entry / back_text).resolve(strict=True)
        return (common.is_dir() and common == entry.parent.parent
                and trusted_root(common) and back_pointer == git_file)
    except (OSError, ValueError, RuntimeError):
        return False


def trusted_envrc(envrc: Path) -> bool:
    """Approval belongs to the file's resolved location, never a symlink alias."""
    try:
        envrc = envrc.resolve(strict=True)
        return trusted_root(envrc) or registered_worktree(envrc.parent)
    except (OSError, ValueError, RuntimeError):
        return False


def last_line(text: str, fallback: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1].strip() if lines else fallback


def ensure(directory: Path, base_env: Mapping[str, str] = os.environ) -> Loaded | NoEnvrc | Refused:
    """Apply direnv's export diff and require proof that a nix shell loaded."""
    try:
        directory = directory.expanduser().resolve()
        if not directory.is_dir():
            return Refused(f"nix_env: {directory}: directory does not exist")
        envrc = nearest_envrc(directory)
        environment = dict(base_env)
        if envrc is None:
            return NoEnvrc(environment)
        # Avoid subprocess's startup cost when no envrc needs loading.
        import subprocess

        trusted = trusted_envrc(envrc)
        commands = (["direnv", "allow", str(envrc)], ["direnv", "export", "json"]) if trusted else (
            ["direnv", "export", "json"],)
        exported = ""
        try:
            for command in commands:
                result = subprocess.run(command, cwd=directory, env=environment, capture_output=True,
                                        text=True, timeout=TIMEOUT_SECONDS, check=False)
                if result.returncode:
                    reason = last_line(result.stderr, f"direnv exit {result.returncode}")
                    if not trusted and any(word in result.stderr.lower() for word in ("not allowed", "blocked", "denied")):
                        # Pay shlex's startup cost only for an explicit approval hint.
                        import shlex

                        reason += "; run " + shlex.join(["direnv", "allow", str(envrc)])
                    return Refused(f"nix_env: {directory}: {reason}")
                exported = result.stdout
        except subprocess.TimeoutExpired as error:
            detail = error.stderr
            stderr = detail.decode(errors="replace") if isinstance(detail, bytes) else detail or ""
            return Refused(f"nix_env: {directory}: " + last_line(stderr, "direnv timed out after 600 s"))
        # Pay JSON's startup cost only when decoding an envrc export.
        import json

        # direnv's no-update path succeeds without emitting an export object.
        diff: object = json.loads(exported) if exported.strip() else {}
        if not isinstance(diff, dict):
            return Refused(f"nix_env: {directory}: direnv export is not a JSON object")
        for key, value in cast(dict[str, object], diff).items():
            if not key or "=" in key or "\0" in key:
                return Refused(f"nix_env: {directory}: direnv export has an invalid variable name")
            if value is None:
                if key in environment:
                    del environment[key]
            elif isinstance(value, str) and "\0" not in value:
                environment[key] = value
            else:
                return Refused(f"nix_env: {directory}: direnv export has an invalid value for {key}")
        if not environment.get("IN_NIX_SHELL"):
            return Refused(f"nix_env: {directory}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)")
        return Loaded(environment, envrc)
    except (OSError, ValueError) as error:
        return Refused(f"nix_env: {directory}: " + last_line(str(error), type(error).__name__))


def main(arguments: list[str]) -> int:
    if len(arguments) == 2 and arguments[0] == "ensure" and arguments[1]:
        outcome = ensure(Path(arguments[1]))
        if isinstance(outcome, Refused):
            print(outcome.reason, file=sys.stderr)
            return 1
        # Pay JSON's startup cost only when the CLI serializes an environment.
        import json

        print(json.dumps(outcome.env))
        return 0
    if len(arguments) >= 2 and arguments[0] == "exec":
        program = arguments[1]
        if not Path(program).is_absolute():
            print("nix_env: exec requires an absolute program path", file=sys.stderr)
            return 2
        outcome = ensure(Path.cwd())
        if isinstance(outcome, Refused):
            print(outcome.reason, file=sys.stderr)
            return 1
        if isinstance(outcome, Loaded):
            outcome.env["NIX_ENV_ENVRC"] = str(outcome.envrc.resolve())
        else:
            _ = outcome.env.pop("NIX_ENV_ENVRC", None)
        try:
            os.execvpe(program, arguments[1:], outcome.env)
        except OSError as error:
            print("nix_env: " + last_line(str(error), type(error).__name__), file=sys.stderr)
            return 1
    print("nix_env: usage: nix_env.py ensure DIR | exec ABSOLUTE_PROGRAM [ARGS...]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
