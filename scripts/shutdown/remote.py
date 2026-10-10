#!/usr/bin/env python3
"""Run the shutdown CLI on the other workstation over Tailscale SSH."""

from __future__ import annotations

import shlex
import subprocess
import sys
from typing import Literal, TypedDict


class TimeLimit(TypedDict):
    kind: Literal["time limit"]
    seconds: float


class WhileLinkAlive(TypedDict):
    kind: Literal["while link alive"]


RemoteCallLimit = TimeLimit | WhileLinkAlive
STANDARD_TIME_LIMIT: TimeLimit = {"kind": "time limit", "seconds": 120.0}


def other_machine() -> str:
    """Return the SSH alias of the workstation other than this one."""
    return "natedev" if sys.platform == "darwin" else "mac"


def run_remote(
    args: list[str],
    stdin: str = "",
    limit: RemoteCallLimit = STANDARD_TIME_LIMIT,
) -> tuple[int, str]:
    """Run shutdown.py remotely, trusting its explicit trailer rather than ssh's status."""
    arguments = " ".join(shlex.quote(argument) for argument in args)
    remote_command = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/shutdown/shutdown.py"'
    if arguments:
        remote_command += f" {arguments}"
    remote_command += '; printf "rc=%s\\n" $?'
    try:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
        ]
        if limit["kind"] == "while link alive":
            command.extend(
                ("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4")
            )
        command.extend((other_machine(), remote_command))
        if limit["kind"] == "time limit":
            completed = subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=limit["seconds"],
                check=False,
            )
        else:
            completed = subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return 255, ""
    except OSError:
        return 255, ""

    output = completed.stdout
    if completed.returncode == 255:
        return 255, output.rstrip("\n")
    lines = output.splitlines()
    trailer = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].startswith("rc=")),
        None,
    )
    if trailer is None:
        return 255, output.rstrip("\n")
    try:
        status = int(lines[trailer].removeprefix("rc="))
    except ValueError:
        return 255, "\n".join(lines[:trailer])
    return status, "\n".join(lines[:trailer])
