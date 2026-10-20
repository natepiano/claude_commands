#!/usr/bin/env python3
"""Send one send.py message later: `send_later.py (--in SPAN | --at "YYYY-MM-DD HH:MM") -- <send.py args>`.

A transient systemd user timer on natedev runs send.py once at that time. On the Mac the timer is
made on natedev over ssh, so a delayed message survives the Mac's sleep. SPAN is a systemd time
span (10min, 1h30min); --at is natedev's local time (America/New_York).
"""
from __future__ import annotations

import argparse
import platform
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import cast
from pathlib import Path

Runner = Callable[[Sequence[str]], int]
SEND = Path.home() / ".claude/scripts/message/send.py"
PY = Path.home() / ".claude/scripts/lib/py"


def timer_argv(span: str | None, at: str | None, send_args: Sequence[str], stamp: int) -> list[str]:
    """The systemd-run command that runs send.py once, at `at` or `span` from now."""
    when = f"--on-active={span}" if span else f"--on-calendar={at}"
    return ["systemd-run", "--user", "--collect", f"--unit=send-later-{stamp}", when,
            "--timer-property=AccuracySec=1s", str(PY), str(SEND), *send_args]


def run(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def main(argv: Sequence[str] | None = None, runner: Runner = run, system: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    when = parser.add_mutually_exclusive_group(required=True)
    _ = when.add_argument("--in", dest="span", help="a systemd time span: 10min, 1h30min")
    _ = when.add_argument("--at", help='natedev local time: "YYYY-MM-DD HH:MM"')
    _ = parser.add_argument("send_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    span, at = cast(str | None, options.span), cast(str | None, options.at)
    send_args = cast(list[str], options.send_args)
    if send_args[:1] == ["--"]:
        send_args = send_args[1:]
    if not send_args:
        parser.error("give the send.py arguments after --")
    command = timer_argv(span, at, send_args, time.time_ns())
    if (system or platform.system()) == "Darwin":
        # The Mac's ~ is not natedev's: natedev expands its own paths.
        remote = [part.replace(str(Path.home()), "$HOME") for part in command]
        command = ["ssh", "natedev", " ".join(part if part.startswith("$HOME") else shlex.quote(part) for part in remote)]
    return runner(command)


if __name__ == "__main__":
    sys.exit(main())
