#!/usr/bin/env python3
"""The nightly review: two read-only agents, one improvement proposal each, queued for the user.

`launch` runs from the nightly-review timer on natedev (/etc/nixos/modules/linux/nightly-review.nix).
It starts `/nightly_review config` and `/nightly_review rust` in named, detached tmux sessions with
Remote Control under the same names, replacing the previous night's unless the user is attached to
it. A session under that name that no nightly review started is left running, and that review skips
the night. Each agent writes NIGHT/<mode>.md and messages natedev, which distills the proposals into
NIGHT/digest.md and the ledger (/watcher). The night is skipped while the active Claude account has
less than MIN_REMAINING percent of its weekly usage left.

Nothing offers the proposals: the ledger's queue grows until the user asks for /nightly_next
(user, 2026-10-10).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# The quota readers live with the agent notes, one directory over.
SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS / "whoami"))
sys.path.insert(0, str(SCRIPTS / "shutdown"))
from launch_permission import (  # noqa: E402
    BlockedByShutdown,
    NewWorkLaunchPurpose,
    ShutdownStateUnreadable,
    launch_barrier,
    launch_permission,
)
from account import NAMED_ACCOUNT_VARIABLES  # noqa: E402
from agent_accounts import ClaudeAccountSelection  # noqa: E402
import note_file  # noqa: E402
from quota_alert import ahead, remaining  # noqa: E402

ROOT = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "nightly-review"
MODES = {"config": Path("/etc/nixos"), "rust": Path.home() / "rust"}
SETTINGS = Path(__file__).with_name("settings.json")
MIN_REMAINING = 10.0


@dataclass(frozen=True)
class QuotaClear:
    """The active Claude account has enough weekly quota to launch work."""


@dataclass(frozen=True)
class QuotaBelowFloor:
    """Tonight's run must wait for weekly quota."""

    reason: str


QuotaPermission = QuotaClear | QuotaBelowFloor


def night_dir(day: date) -> Path:
    return ROOT / day.isoformat()


def session(mode: str) -> str:
    return f"nightly-{mode}"


def tmux(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(["tmux", *args], 127, "", "tmux is not installed")


def running(mode: str) -> bool:
    return tmux("has-session", "-t", f"={session(mode)}").returncode == 0


def quota_permission() -> QuotaPermission:
    from scripts.accounts import accounts

    return quota_for_selection(accounts.claude_account_selection())


def quota_for_selection(selection: ClaudeAccountSelection) -> QuotaPermission:
    """Whether the default Claude login has enough weekly quota to run."""
    for note in note_file.read_notes():
        left = remaining(note)
        launch_account = (
            selection.is_default_login(note.get("login") or "")
            if selection.multiple_accounts else note.get("state") == "active"
        )
        if (note.tool == "claude" and launch_account and left is not None
                and left < MIN_REMAINING and ahead(note.get("resets"))):
            reason = (
                f"{note.path.stem} has {left:g}% of its weekly usage left, under "
                + f"the {MIN_REMAINING:g}% floor"
            )
            return QuotaBelowFloor(reason)
    return QuotaClear()


def start(mode: str) -> str:
    from scripts.accounts import accounts

    return start_for_selection(mode, accounts.claude_account_selection())


def start_for_selection(mode: str, selection: ClaudeAccountSelection) -> str:
    name = session(mode)
    if running(mode):
        if tmux("list-clients", "-t", f"={name}").stdout.strip():
            return f"{name}: you are attached, so last night's session was left running"
        # A session promoted out of a nightly review keeps its tmux name.
        if f"/nightly_review {mode}" not in tmux("list-panes", "-F", "#{pane_start_command}", "-t", f"={name}:").stdout:
            return f"{name}: another session holds this name, so tonight's review did not start"
        _ = tmux("kill-session", "-t", f"={name}")
    claude = shlex.join(["claude", "--remote-control", name, "-n", name, "--add-dir", str(Path.home() / ".claude"),
                         "--settings", str(SETTINGS), f"/nightly_review {mode}"])
    # A scope of its own, so a tmux server this starts outlives the timer's oneshot unit.
    command = f"ENABLE_TOOL_SEARCH=true command {claude}; exec zsh"
    if selection.multiple_accounts:
        # tmux may supply a named account from its server environment. Clear
        # its variables and the token inside the command before Claude runs.
        command = f"unset {' '.join(NAMED_ACCOUNT_VARIABLES)} CLAUDE_CODE_OAUTH_TOKEN; " + command
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "tmux", "new-session", "-d", "-s", name,
            "-c", str(MODES[mode]), "zsh", "-ic", command]
    if selection.multiple_accounts:
        started = subprocess.run(argv, capture_output=True, text=True, check=False,
                                 env=launch_environment(selection))
    else:
        started = subprocess.run(argv, capture_output=True, text=True, check=False)
    return f"{name}: started" if started.returncode == 0 else f"{name}: failed to start: {started.stderr.strip()}"


def launch_environment(selection: ClaudeAccountSelection) -> dict[str, str]:
    """The default account's launch environment, retaining legacy inheritance."""
    return {key: value for key, value in os.environ.items()
            if not selection.multiple_accounts or not key.startswith("CLAUDE_")}


def launch(today: date) -> list[str]:
    from scripts.accounts import accounts

    selection = accounts.claude_account_selection()
    directory = night_dir(today)
    directory.mkdir(parents=True, exist_ok=True)
    barrier = launch_barrier()
    try:
        _ = barrier.__enter__()
    except OSError as error:
        lines = [
            f"skipped: shutdown state unreadable: launch barrier: {error}"
        ]
    else:
        try:
            permission = launch_permission(launch_environment(selection), NewWorkLaunchPurpose())
            if isinstance(permission, BlockedByShutdown):
                lines = [f"skipped: {permission.reason}"]
            elif isinstance(permission, ShutdownStateUnreadable):
                lines = [
                    f"skipped: shutdown state unreadable: {permission.detail}"
                ]
            else:
                quota = quota_for_selection(selection)
                lines = (
                    [f"skipped: {quota.reason}"]
                    if isinstance(quota, QuotaBelowFloor)
                    else [start_for_selection(mode, selection) for mode in MODES]
                )
        finally:
            _ = barrier.__exit__(None, None, None)
    _ = (directory / "launch.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main() -> None:
    match sys.argv[1:]:
        case ["launch"]:
            for line in launch(date.today()):
                print(line)
        case _:
            sys.exit("usage: nightly_review.py launch")


if __name__ == "__main__":
    main()
