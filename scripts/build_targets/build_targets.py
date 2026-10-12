#!/usr/bin/env python3
"""Show the recorded state of each configured build target."""

from __future__ import annotations

import argparse
from datetime import datetime
import sys

from targets import (
    ConfigError,
    ExpiresAt,
    Free,
    ReleaseEstimated,
    Running,
    SshTarget,
    StateUnreadable,
    TargetState,
    Unreachable,
    read_targets,
    target_state,
)
import offload


class CliArguments(argparse.Namespace):
    """Arguments accepted by the build-target status command."""

    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.probe: bool = False


def local_time(instant: datetime) -> str:
    return instant.astimezone().strftime("%H:%M %Z")


def display_field(value: str) -> str:
    return " ".join(value.split())


def format_state(name: str, state: TargetState) -> str:
    name = display_field(name)
    if isinstance(state, Free):
        return f"{name}: free"
    if isinstance(state, Running):
        return f"{name}: running {display_field(state.what)} since {local_time(state.since)}"
    if isinstance(state, Unreachable):
        return f"{name}: unreachable — {display_field(state.reason)}"
    if isinstance(state, StateUnreadable):
        return f"{name}: state unreadable — {display_field(state.reason)}"
    line = f"{name}: held by {display_field(state.holder)} since {local_time(state.since)}"
    if isinstance(state.end, ExpiresAt):
        line += f" until {local_time(state.end.when)}"
    elif isinstance(state.end, ReleaseEstimated):
        line += f" release expected {local_time(state.end.when)}"
    reason = display_field(state.reason)
    return f"{line} — {reason}" if reason else line


def probe_target(target: SshTarget) -> TargetState:
    config = offload.read_config(target.settings)
    result = offload.probe_reachability(offload.ProcessController(), config)
    if isinstance(result, offload.UnreachableHost):
        return Unreachable(result.reason)
    return target_state(target)


def command_status(probe: bool = False) -> int:
    try:
        targets = read_targets()
    except ConfigError as error:
        print(f"build targets: {error}", file=sys.stderr)
        return 2
    for target in targets:
        state = (
            probe_target(target)
            if probe and isinstance(target, SshTarget)
            else target_state(target)
        )
        print(format_state(target.name, state))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    _ = status.add_argument("--probe", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args(namespace=CliArguments())
    return command_status(args.probe)


if __name__ == "__main__":
    raise SystemExit(main())
