#!/usr/bin/env python3
"""The nightly review: one read-only session, one improvement proposal per mode, queued for the user.

`launch` runs from the nightly-review timer on natedev (/etc/nixos/modules/linux/nightly-review.nix).
It starts `/nightly_review config rust` in one named, detached tmux session with Remote Control under
the same name, replacing the previous night's unless the user is attached to it. A session under that
name that no nightly review started is left running, and the review skips the night. The session runs
on the account the unit role names, started as a unit is, or on the default login when no role is set.
It does the modes in order; for each it writes NIGHT/<mode>.md and messages natedev, which distills
the proposals into NIGHT/digest.md and the ledger (/watcher). The night is skipped while that account
is shut down, is not fit to launch, or has less than MIN_REMAINING percent of its weekly usage left.

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
MODES = ("config", "rust")
SESSION = "nightly-review"
# The two sessions a night had before one session ran every mode.
LEGACY_SESSIONS = tuple(f"nightly-{mode}" for mode in MODES)
PROMPT = "/nightly_review " + " ".join(MODES)
SETTINGS = Path(__file__).with_name("settings.json")
MIN_REMAINING = 10.0


@dataclass(frozen=True)
class QuotaClear:
    """The account the review launches on has enough weekly quota to launch work."""


@dataclass(frozen=True)
class QuotaBelowFloor:
    """Tonight's run must wait for weekly quota."""

    reason: str


QuotaPermission = QuotaClear | QuotaBelowFloor


def night_dir(day: date) -> Path:
    return ROOT / day.isoformat()


def tmux(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(["tmux", *args], 127, "", "tmux is not installed")


def running(name: str) -> bool:
    return tmux("has-session", "-t", f"={name}").returncode == 0


def quota_permission() -> QuotaPermission:
    from scripts.accounts import accounts

    return quota_for_selection(accounts.claude_account_selection())


def quota_for_selection(selection: ClaudeAccountSelection) -> QuotaPermission:
    """Whether the account the review launches on has enough weekly quota to run."""
    for note in note_file.read_notes():
        left = remaining(note)
        launch_account = (
            "unit directors" in selection.roles_for_login(note.get("login") or "")
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


@dataclass(frozen=True)
class LeftRunning:
    """A session under a review name stays, with the line that says why."""

    line: str


@dataclass(frozen=True)
class NameFree:
    """No session holds the name now."""


def retire(name: str, attached: str, foreign: str) -> LeftRunning | NameFree:
    """End a past night's session unless the user is attached or no review started it."""
    if not running(name):
        return NameFree()
    if tmux("list-clients", "-t", f"={name}").stdout.strip():
        return LeftRunning(f"{name}: {attached}")
    # A session promoted out of a nightly review keeps its tmux name.
    if "/nightly_review " not in tmux("list-panes", "-F", "#{pane_start_command}", "-t", f"={name}:").stdout:
        return LeftRunning(f"{name}: {foreign}")
    _ = tmux("kill-session", "-t", f"={name}")
    return NameFree()


def start(selection: ClaudeAccountSelection, prefix: str) -> list[str]:
    """Replace last night's session with tonight's; `prefix` selects a named account."""
    lines = [
        left.line for name in LEGACY_SESSIONS
        if isinstance(left := retire(
            name, "you are attached, so it was left running",
            "another session holds this name, so it was left running",
        ), LeftRunning)
    ]
    last_night = retire(
        SESSION, "you are attached, so last night's session was left running",
        "another session holds this name, so tonight's review did not start",
    )
    if isinstance(last_night, LeftRunning):
        return [*lines, last_night.line]
    # The session works in ~/rust; the config mode also reads /etc/nixos and ~/.claude.
    home = Path.home()
    argv = ["claude", "--remote-control", SESSION, "-n", SESSION,
            "--add-dir", "/etc/nixos", "--add-dir", str(home / ".claude"),
            "--settings", str(SETTINGS), PROMPT]
    # A scope of its own, so a tmux server this starts outlives the timer's oneshot unit.
    command = f"{prefix}ENABLE_TOOL_SEARCH=true command {shlex.join(argv)}; exec zsh"
    if selection.multiple_accounts:
        # tmux may supply another account from its server environment. Clear
        # its variables and the token inside the command before Claude runs.
        command = f"unset {' '.join(NAMED_ACCOUNT_VARIABLES)} CLAUDE_CODE_OAUTH_TOKEN; " + command
    spawn = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "tmux", "new-session", "-d", "-s", SESSION,
             "-c", str(home / "rust"), "zsh", "-ic", command]
    if selection.multiple_accounts:
        started = subprocess.run(spawn, capture_output=True, text=True, check=False,
                                 env=launch_environment(selection))
    else:
        started = subprocess.run(spawn, capture_output=True, text=True, check=False)
    outcome = "started" if started.returncode == 0 else f"failed to start: {started.stderr.strip()}"
    return [*lines, f"{SESSION}: {outcome}"]


def launch_environment(selection: ClaudeAccountSelection) -> dict[str, str]:
    """The launch environment without an inherited account, retaining legacy inheritance."""
    return {key: value for key, value in os.environ.items()
            if not selection.multiple_accounts or not key.startswith("CLAUDE_")}


def tonight(selection: ClaudeAccountSelection) -> list[str]:
    """Start the review on the unit role's account, or give the one line that says why not."""
    from scripts.accounts import accounts

    selected = accounts.role_account("unit")
    environment = launch_environment(selection)
    if isinstance(selected, accounts.NamedAccount):
        problems = accounts.check(selected.label)
        if problems:
            return [f"skipped: unit account {selected.label}: {problems[0]}"]
        # The shutdown check reads the account the review will run on.
        environment["CLAUDE_CONFIG_DIR"] = str(selected.dir)
    permission = launch_permission(environment, NewWorkLaunchPurpose())
    if isinstance(permission, BlockedByShutdown):
        return [f"skipped: {permission.reason}"]
    if isinstance(permission, ShutdownStateUnreadable):
        return [f"skipped: shutdown state unreadable: {permission.detail}"]
    quota = quota_for_selection(selection)
    if isinstance(quota, QuotaBelowFloor):
        return [f"skipped: {quota.reason}"]
    return start(selection, accounts.account_prefix(selected))


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
            lines = tonight(selection)
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
