"""Run synchronous CLI cases without borrowing the caller's process state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import argparse
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from types import ModuleType
from typing import cast
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class _Parsed(Exception):
    """Stop a real CLI immediately after argument parsing."""


def parse_arguments(main: Callable[[list[str]], int], arguments: list[str]) -> argparse.Namespace | None:
    """Run only the CLI's real parser; return its options without running a handler."""
    parse = argparse.ArgumentParser.parse_args
    options: argparse.Namespace | None = None
    def parse_only(parser: argparse.ArgumentParser, args: list[str] | None = None,
                   namespace: argparse.Namespace | None = None) -> argparse.Namespace:
        nonlocal options
        options = parse(parser, args, namespace)
        raise _Parsed
    with patch.object(argparse.ArgumentParser, "parse_args", parse_only), redirect_stderr(io.StringIO()):
        try:
            _ = main(arguments)
        except _Parsed:
            return options
        except SystemExit:
            return None
    return None


def invoke_cli(module: ModuleType, main: Callable[[], int], arguments: list[str],
               environment: Mapping[str, str], cwd: Path, *, combined_output: bool = False,
               production_state: bool = False) -> subprocess.CompletedProcess[str]:
    """Keep the real parser, handlers and child commands; isolate their boundaries.

    These commands do not mutate containers in their module globals. Restore
    bindings as well as the environment, cwd, argv, import path and signal state.
    Import-time production paths are rebound under the fixture HOME explicitly.
    """
    # Cold imports must compute their defaults in the caller's environment too;
    # patch.object will then restore caller paths rather than fixture paths.
    if production_state:
        with patch.object(sys, "path", sys.path.copy()):
            import dailies_render
            from scripts.production import showrunners

    before = vars(module).copy()
    directory = Path.cwd()
    stdout = io.StringIO()
    stderr = stdout if combined_output else io.StringIO()
    handlers = {sig: signal.getsignal(sig) for sig in
                (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)}
    try:
        with ExitStack() as scope:
            _ = scope.enter_context(cast(AbstractContextManager[object], patch.dict(os.environ, environment, clear=True)))
            _ = scope.enter_context(patch.object(sys, "argv", [str(module.__file__), *arguments]))
            _ = scope.enter_context(patch.object(sys, "path", sys.path.copy()))
            _ = scope.enter_context(redirect_stdout(stdout))
            _ = scope.enter_context(redirect_stderr(stderr))
            os.chdir(cwd)
            if production_state:
                # These imports only retrieve the caller-initialized modules.
                import dailies_render
                from scripts.production import showrunners
                from scripts.whoami import note_file

                home = Path.home()
                for name, value in {
                    "CHART_CONF": home / ".local/state/showrunner/dailies.conf",
                    "READINGS_LOG": home / ".local/state/agent-notes/readings.jsonl",
                }.items():
                    _ = scope.enter_context(patch.object(dailies_render, name, value))
                _ = scope.enter_context(patch.object(note_file, "AGENTS_DIR",
                                                    home / "rust/hanadocs/agents"))
                for name, value in {
                    "CONFIG": Path(environment.get("SHOWRUNNERS_CONFIG") or
                                   str(Path(showrunners.__file__).parents[2] / "config/showrunners.json")),
                    "NOTIFIER_STATE_DIR": Path(environment.get("NOTIFIER_STATE_DIR") or
                                               str(home / ".local/state/notifier")),
                    "SESSIONS_DIR": Path(environment.get("NOTIFIER_SESSIONS_DIR") or str(home / ".claude/sessions")),
                    "SHOWRUNNER_STATE_DIR": Path(environment.get("SHOWRUNNER_STATE_DIR") or
                                                 str(home / ".local/state/showrunner")),
                    "SESSIONS": Path(environment.get("SHOWRUNNERS_SESSIONS") or
                                     str(Path(showrunners.__file__).parents[1] / "message/sessions.py")),
                    "SEND": Path(environment.get("SHOWRUNNERS_SEND") or
                                 str(Path(showrunners.__file__).parents[1] / "message/send.py")),
                }.items():
                    _ = scope.enter_context(patch.object(showrunners, name, value))
            try:
                status = main()
            except SystemExit as exit_status:
                status = exit_status.code if isinstance(exit_status.code, int) else 0 if exit_status.code is None else 1
                if isinstance(exit_status.code, str):
                    print(exit_status.code, file=sys.stderr)
    finally:
        os.chdir(directory)
        for sig, handler in handlers.items():
            _ = signal.signal(sig, handler)
        vars(module).clear()
        vars(module).update(before)
    return subprocess.CompletedProcess([sys.executable, str(module.__file__), *arguments],
                                       status, stdout.getvalue(), "" if combined_output else stderr.getvalue())


@dataclass(frozen=True)
class ImmutableGitSeed:
    """Initialized once, then copied with private refs, config, index and origin."""

    checkout: Path
    origin: Path

    @classmethod
    def create(cls, root: Path, branch: str, author: str, email: str,
               files: Mapping[str, str]) -> ImmutableGitSeed:
        checkout, origin = root / "checkout", root / "origin.git"
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        commands = [["init", "--bare", str(origin)], ["init", "-b", branch, str(checkout)]]
        for arguments in commands:
            _ = subprocess.run(["git", *arguments], env=environment, capture_output=True, check=True)
        for arguments in (["config", "user.name", author], ["config", "user.email", email],
                          ["remote", "add", "origin", str(origin)]):
            _ = subprocess.run(["git", *arguments], cwd=checkout, env=environment, capture_output=True, check=True)
        for relative, content in files.items():
            path = checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(content, encoding="utf-8")
        if files:
            for arguments in (["add", "."], ["commit", "-m", "initial"], ["push", "-u", "origin", branch]):
                _ = subprocess.run(["git", *arguments], cwd=checkout, env=environment, capture_output=True, check=True)
        return cls(checkout, origin)

    def copy_to(self, checkout: Path, origin: Path) -> None:
        # copytree copies files, including objects: no shared writable metadata or refs.
        _ = shutil.copytree(self.checkout, checkout)
        _ = shutil.copytree(self.origin, origin)
        config = checkout / ".git/config"
        _ = config.write_text(config.read_text().replace(str(self.origin), str(origin)))
