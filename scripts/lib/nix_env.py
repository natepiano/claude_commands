#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Load and prove a worktree's nix environment before starting a process.

Envrc files below ~/rust, ~/worktrees or ~/.claude*, and in linked worktrees
registered by repositories below those roots, are allowed automatically.
Registered scratch copies also qualify when their regular top-level envrc
matches the recorded source commit in a repository below those roots.
Other directories keep direnv's explicit approval boundary.
No envrc means the caller's environment is retained, without running direnv.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Mapping
    from scripts.lib.conf import ConfigRead

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


def _remaining_timeout(cap: float) -> float:
    """Load deadline support only when a subprocess needs a timeout."""
    from scripts.lib import operation_deadline

    return operation_deadline.remaining(cap)


def _settings() -> ConfigRead:
    from scripts.lib import conf

    return conf.read("nix_env")


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
    settings = _settings()
    return bool(relative.parts and (relative.parts[0] in settings.words("trusted_home_folders")
                                  or relative.parts[0].startswith(settings.text("trusted_home_prefix"))))


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


def _git_environment() -> dict[str, str]:
    """Keep caller overrides from choosing git's repository or object store."""
    return {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}


def registered_copy(envrc: Path) -> bool:
    """Prove a copy's envrc matches its recorded commit in a trusted source."""
    # Keep copy_record and subprocess off the no-envrc and trusted-root paths.
    import subprocess

    try:
        if not stat.S_ISREG(envrc.lstat().st_mode):
            return False
        folder = envrc.parent.resolve(strict=True)
        from scripts.lib import copy_record

        record_path = copy_record.record_path(folder)
        if envrc.name != ".envrc" or record_path is None:
            return False
        record = copy_record.read_record(record_path)
        if not isinstance(record, copy_record.Registered):
            return False
        source_path = record.fields.get("source_path")
        source_commit = record.fields.get("source_commit")
        if not isinstance(source_path, str) or not source_path or not isinstance(source_commit, str) or not source_commit:
            return False
        source = Path(source_path).resolve(strict=True)
        if not source.is_dir() or not trusted_root(source):
            return False
        timeout = _settings().number("command_timeout_seconds")
        environment = _git_environment()
        expected = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{source_commit}:.envrc"],
            cwd=source, env=environment, capture_output=True, text=True, timeout=_remaining_timeout(timeout), check=True)
        actual = subprocess.run(
            ["git", "hash-object", "--no-filters", "--", str(folder / ".envrc")],
            cwd=source, env=environment, capture_output=True, text=True, timeout=_remaining_timeout(timeout), check=True)
        return bool(expected.stdout.strip()) and expected.stdout.strip() == actual.stdout.strip()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


def generated_source(root: Path) -> bool:
    """Accept a scratch record that marks files written by our code."""
    try:
        folder = root.resolve(strict=True)
        from scripts.lib import copy_record

        if folder.parent != copy_record.root().resolve():
            return False
        record = copy_record.read_record(folder / copy_record.RECORD)
        return isinstance(record, copy_record.Registered) and record.fields.get("generated_source") is True
    except (OSError, ValueError, RuntimeError):
        return False


def trusted_envrc(envrc: Path) -> bool:
    """Approval belongs to the file's resolved location, never a symlink alias."""
    try:
        resolved = envrc.resolve(strict=True)
        return trusted_root(resolved) or registered_worktree(resolved.parent) or registered_copy(envrc)
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

        timeout = _settings().number("command_timeout_seconds")
        trusted = trusted_envrc(envrc)
        commands = (["direnv", "allow", str(envrc)], ["direnv", "export", "json"]) if trusted else (
            ["direnv", "export", "json"],)
        exported = ""
        try:
            for command in commands:
                result = subprocess.run(command, cwd=directory, env=environment, capture_output=True,
                                        text=True, timeout=_remaining_timeout(timeout), check=False)
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
            return Refused(f"nix_env: {directory}: " + last_line(stderr, f"direnv timed out after {timeout:g} s"))
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
        from scripts.lib.models_registry import agent_environment

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
            os.execvpe(program, arguments[1:], agent_environment(outcome.env))
        except OSError as error:
            print("nix_env: " + last_line(str(error), type(error).__name__), file=sys.stderr)
            return 1
    print("nix_env: usage: nix_env.py ensure DIR | exec ABSOLUTE_PROGRAM [ARGS...]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
