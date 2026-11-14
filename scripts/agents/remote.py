#!/usr/bin/env python3
"""Run the shutdown CLI on the other workstation over Tailscale SSH."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal, TypedDict

from scripts.lib import conf


@dataclass(frozen=True)
class Workstation:
    alias: str
    platform: str
    host_names: tuple[str, ...]


@dataclass(frozen=True)
class MachineAlias:
    alias: str


@dataclass(frozen=True)
class MachineNameRefused:
    reason: str


def _workstations() -> tuple[Workstation, ...]:
    configured = conf.read("machines")
    return tuple(Workstation(alias, configured.text(f"{alias}.platform"), configured.words(f"{alias}.hosts"))
                 for alias in configured.words("aliases"))


def machine_alias(name: str) -> MachineAlias | MachineNameRefused:
    """Resolve a configured alias or host name without regard to case."""
    workstations = _workstations()
    for workstation in workstations:
        if name.casefold() in {value.casefold() for value in (workstation.alias, *workstation.host_names)}:
            return MachineAlias(workstation.alias)
    accepted = dict.fromkeys(value for workstation in workstations
                             for value in (workstation.alias, *workstation.host_names))
    return MachineNameRefused(f"unknown machine {name!r}; accepted names: {', '.join(accepted)}")


class TimeLimit(TypedDict):
    kind: Literal["time limit"]
    seconds: float


class WhileLinkAlive(TypedDict):
    kind: Literal["while link alive"]


RemoteCallLimit = TimeLimit | WhileLinkAlive
STANDARD_TIME_LIMIT: TimeLimit = {"kind": "time limit", "seconds": 120.0}


def local_machine() -> str:
    """Return this machine's shutdown-record name."""
    name = socket.gethostname().split(".", 1)[0]
    return name or ("mac" if sys.platform == "darwin" else "natedev")


def this_machine() -> str:
    """Return the SSH alias of this workstation."""
    platform = "darwin" if sys.platform == "darwin" else "linux"
    return next(workstation.alias for workstation in _workstations() if workstation.platform == platform)


def other_machine() -> str:
    """Return the SSH alias of the workstation other than this one."""
    platform = "darwin" if sys.platform == "darwin" else "linux"
    return next(workstation.alias for workstation in _workstations() if workstation.platform != platform)


def run_remote(
    args: list[str],
    stdin: str = "",
    limit: RemoteCallLimit = STANDARD_TIME_LIMIT,
) -> tuple[int, str]:
    """Run shutdown.py remotely, trusting its explicit trailer rather than ssh's status."""
    arguments = " ".join(shlex.quote(argument) for argument in args)
    remote_command = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/agents/shutdown.py"'
    if arguments:
        remote_command += f" {arguments}"
    remote_command += '; printf "rc=%s\\n" $?'
    try:
        command = ["ssh", "-o", "BatchMode=yes", other_machine(), remote_command]
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
