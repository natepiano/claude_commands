#!/usr/bin/env python3
"""Read the machine's process table and identify running Codex agents."""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import NamedTuple

PS = shlex.split(os.environ.get("PROCESS_TABLE_PS") or "ps -eo pid=,ppid=,comm=,args=")


class Process(NamedTuple):
    pid: int
    parent: int
    command: str
    arguments: str


def snapshot() -> dict[int, Process]:
    listing = subprocess.run(PS, capture_output=True, text=True, check=True)
    table: dict[int, Process] = {}
    for line in listing.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) >= 3:
            table[int(fields[0])] = Process(
                int(fields[0]),
                int(fields[1]),
                fields[2],
                fields[3] if len(fields) > 3 else "",
            )
    return table


def is_codex_agent(process: Process) -> bool:
    words = process.arguments.split()
    if process.command == "codex":
        return words[1:2] == ["exec"]
    if not process.command.startswith("python"):
        return False
    mesh = next(
        (index for index, word in enumerate(words) if word.endswith("/codex_mesh.py")),
        None,
    )
    return mesh is not None and words[mesh + 1 : mesh + 2] in (["start"], ["follow"])
